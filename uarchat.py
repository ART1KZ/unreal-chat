#!/usr/bin/env python3
"""uachat - terminal chat client for the unreal-agent harness.

One prompt = one `unreal-agent-runner` process. The conversation continues
through a persisted session id, and the runner's JSONL session records are
rendered as they arrive. Rendering is event-driven: the harness does not emit
partial model tokens (`include_partial_messages` is accepted but ignored
upstream), so text appears when a model item completes, not token by token.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid

try:  # POSIX only; absent on Windows
    import readline
except ImportError:  # pragma: no cover
    readline = None

# Optional client-side modules shipped next to this file.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import editor as editor_module
except ImportError:  # pragma: no cover - editor.py absent
    editor_module = None
try:
    import models as models_module
except ImportError:  # pragma: no cover - models.py absent
    models_module = None
try:
    import providers as providers_module
except ImportError:  # pragma: no cover - providers.py absent
    providers_module = None

VERSION = "0.3"

CONFIG_PATH = os.path.expanduser("~/.config/uachat/env")
HISTORY_PATH = os.path.expanduser("~/.config/uachat/history")
BRIDGE_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "bridge.py")
UPDATE_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "update-core.sh")
REPAIR_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "repair-session.py")
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "uachat"
)
CHECK_STAMP = os.path.join(STATE_DIR, "last-check")
NOTE_PATH = os.path.join(STATE_DIR, "pending-note")
SESSION_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "unreal-agent",
    "sessions",
)

RESET = "\033[0m"
BOLD = "1"
ITALIC = "3"

# 256-colour palettes. Keys: title, label, user, agent, tool, result, dim, warn, error, ok, accent.
THEMES: dict[str, dict[str, str]] = {
    "midnight": {
        "title": "38;5;111", "label": "38;5;245", "user": "38;5;117", "agent": "38;5;150",
        "tool": "38;5;180", "result": "38;5;246", "dim": "38;5;240", "warn": "38;5;179",
        "error": "38;5;203", "ok": "38;5;150", "accent": "38;5;176",
    },
    "nord": {
        "title": "38;5;110", "label": "38;5;245", "user": "38;5;109", "agent": "38;5;108",
        "tool": "38;5;139", "result": "38;5;250", "dim": "38;5;243", "warn": "38;5;222",
        "error": "38;5;174", "ok": "38;5;108", "accent": "38;5;110",
    },
    "gruvbox": {
        "title": "38;5;208", "label": "38;5;246", "user": "38;5;109", "agent": "38;5;142",
        "tool": "38;5;214", "result": "38;5;223", "dim": "38;5;243", "warn": "38;5;214",
        "error": "38;5;167", "ok": "38;5;142", "accent": "38;5;175",
    },
    "neon": {
        "title": "38;5;213", "label": "38;5;245", "user": "38;5;51", "agent": "38;5;120",
        "tool": "38;5;227", "result": "38;5;252", "dim": "38;5;242", "warn": "38;5;227",
        "error": "38;5;197", "ok": "38;5;120", "accent": "38;5;207",
    },
    "paper": {
        "title": "38;5;25", "label": "38;5;240", "user": "38;5;24", "agent": "38;5;28",
        "tool": "38;5;130", "result": "38;5;238", "dim": "38;5;246", "warn": "38;5;136",
        "error": "38;5;160", "ok": "38;5;28", "accent": "38;5;90",
    },
    "mono": {
        "title": "1", "label": "2", "user": "1", "agent": "0", "tool": "1",
        "result": "2", "dim": "2", "warn": "1", "error": "1", "ok": "0", "accent": "1",
    },
}

COMMANDS = (
    "/help", "/new", "/sessions", "/resume", "/session",
    "/provider", "/model", "/thinking", "/theme", "/themes", "/copy", "/dump", "/repair", "/exit", "/quit",
)
COMMAND_HELP = {
    "/help": "this text",
    "/new": "start a fresh session",
    "/sessions": "list recent sessions",
    "/resume": "switch to an existing session",
    "/session": "print the current session id",
    "/provider": "pick the provider (opencode-go, openrouter, openai, ...)",
    "/model": "pick the model",
    "/thinking": "pick the reasoning effort",
    "/theme": "pick a colour theme",
    "/themes": "list themes",
    "/copy": "copy the last answer to the clipboard",
    "/dump": "write the transcript to a file",
    "/repair": "copy a session without duplicate tool outputs (gateway rejects)",
    "/exit": "leave",
    "/quit": "leave",
}
MAX_RESULT_LINES = 24
MAX_ARG_CHARS = 160
# Ctrl-C twice within this window (at the prompt, or right after an interrupted
# turn) leaves the client.
DOUBLE_INTERRUPT_WINDOW = 4.0


# --------------------------------------------------------------------------- config


def load_config(path: str = CONFIG_PATH) -> dict[str, str]:
    """Load KEY=VALUE defaults from the uachat env file; process env wins."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        values[name] = value
        # WSLENV from the Windows shim can inject these names as empty values;
        # an empty variable must not shadow the configured default.
        if not os.environ.get(name):
            os.environ[name] = value
    return values


def save_config_value(name: str, value: str, path: str = CONFIG_PATH) -> None:
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        lines = []
    replaced = False
    for index, line in enumerate(lines):
        head = line.split("=", 1)[0].strip()
        if head == name:
            lines[index] = f"{name}={value}\n"
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{name}={value}\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------- theme


class Theme:
    def __init__(self, name: str, enabled: bool) -> None:
        self.name = name if name in THEMES else "midnight"
        self.enabled = enabled
        self.palette = THEMES[self.name]

    def paint(self, text: str, key: str, style: str = "") -> str:
        if not self.enabled or not text:
            return text
        code = self.palette.get(key, "")
        if not code:
            return text
        params = f"{style};{code}" if style else code
        return f"\033[{params}m{text}{RESET}"

    def names(self) -> list[str]:
        return list(THEMES)


# --------------------------------------------------------------------------- helpers


def short(text: str, limit: int = MAX_ARG_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def session_path(name: str) -> str:
    return os.path.join(SESSION_DIR, f"{name}.session.jsonl")


def list_sessions(limit: int = 15) -> list[dict[str, object]]:
    try:
        entries = [
            os.path.join(SESSION_DIR, item)
            for item in os.listdir(SESSION_DIR)
            if item.endswith(".session.jsonl")
        ]
    except OSError:
        return []
    entries.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    sessions = []
    for path in entries[:limit]:
        name = os.path.basename(path)[: -len(".session.jsonl")]
        sessions.append(
            {
                "name": name,
                "mtime": os.path.getmtime(path),
                "size": os.path.getsize(path),
                "preview": session_preview(path),
            }
        )
    return sessions


def session_preview(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _ in range(200):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # On disk each record is wrapped: {"type":"item","data":{"Item":{...}}}
                if record.get("type") == "item":
                    record = (record.get("data") or {}).get("Item") or {}
                data = record.get("Data") or {}
                if record.get("Kind") == "input" and data.get("Kind") == "external":
                    payload = data.get("Payload")
                    if isinstance(payload, str):
                        return short(payload, 60)
                    if isinstance(payload, dict):
                        return short(str(payload.get("Text") or payload), 60)
    except OSError:
        pass
    return ""


def valid_session_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


_MODEL_CACHE: dict[str, list[str]] = {}

DEFAULT_PROVIDER = "opencode-go"
FALLBACK_PROVIDER_SPEC = {
    "id": DEFAULT_PROVIDER,
    "label": "OpenCode Go",
    "harness": "openai",
    "base_url": "https://opencode.ai/zen/go/v1",
    "bridge": True,
    "needs_key": True,
}


def provider_ids() -> list[str]:
    if providers_module is not None:
        return list(getattr(providers_module, "PROVIDER_IDS", [DEFAULT_PROVIDER]))
    return [DEFAULT_PROVIDER]


def provider_spec(provider_id: str) -> dict:
    if providers_module is not None:
        try:
            return dict(providers_module.spec(provider_id))
        except Exception:
            pass
    return dict(FALLBACK_PROVIDER_SPEC, id=provider_id, label=provider_id)


def provider_key(provider_id: str) -> str:
    if providers_module is not None:
        try:
            return providers_module.key_for(provider_id) or ""
        except Exception:
            return ""
    if provider_id == DEFAULT_PROVIDER:
        return os.environ.get("UACHAT_BRIDGE_KEY", "")
    return ""


def credential_state(provider_id: str) -> str:
    if providers_module is not None:
        try:
            return str(providers_module.auth_state(provider_id))
        except Exception:
            pass
    if provider_key(provider_id):
        return "key"
    return "keyless" if not provider_spec(provider_id).get("needs_key", True) else "no key"


PROVIDER_DEFAULT_MODELS = {
    "opencode-go": "deepseek-v4.1-flash",
    "openrouter": "deepseek/deepseek-v4.1-flash",
    "openai": "gpt-6-astra",
    "openai-codex": "gpt-5.6-sol",
    "fireworks": "",
    "ollama": "llama3",
}


def choose_model(provider_id: str, ids: list[str], prefer: str = "") -> str:
    """Pick a usable model when switching providers: keep the current one if possible."""
    if prefer and (not ids or prefer in ids):
        return prefer
    tail = prefer.split("/")[-1]
    if tail:
        for item in ids:
            if item.split("/")[-1] == tail:
                return item
    wanted = PROVIDER_DEFAULT_MODELS.get(provider_id, "")
    if wanted:
        if not ids or wanted in ids:
            return wanted
        wanted_tail = wanted.split("/")[-1]
        for item in ids:
            if item.split("/")[-1] == wanted_tail:
                return item
    return ids[0] if ids else ""


def available_models(provider: str = "", refresh: bool = False) -> list[str]:
    """Model ids for a provider (cached), with the built-in fallback for the default one."""
    provider = provider or os.environ.get("UACHAT_PROVIDER") or DEFAULT_PROVIDER
    if not refresh and _MODEL_CACHE.get(provider):
        return _MODEL_CACHE[provider]
    ids: list[str] = []
    if providers_module is not None and provider in provider_ids():
        try:
            ids = list(providers_module.models(provider))
        except Exception:
            ids = []
    if not ids and provider == DEFAULT_PROVIDER:
        if models_module is not None:
            try:
                source, key = models_module.models_source(os.environ)
                if source:
                    ids = list(models_module.list_models(source, key))
            except Exception:
                ids = []
        if not ids:
            ids = fallback_models()
    _MODEL_CACHE[provider] = ids
    return ids

THINKING_GLYPHS = {
    "minimal": "○", "low": "◔", "medium": "◑", "high": "◒", "xhigh": "◕", "max": "◉", "": "·",
}


def thinking_glyph(level: str) -> str:
    return THINKING_GLYPHS.get((level or "").strip().lower(), "◑")


def notify(title: str, body: str) -> None:
    """Terminal toast (OSC 9 + BEL) plus a desktop notification when available."""
    value = (os.environ.get("UACHAT_NOTIFY") or "on").strip().lower()
    if value in ("off", "0", "false", "no"):
        return
    try:
        sys.stdout.write(f"\x1b]9;{title}: {body}\x07\a")
        sys.stdout.flush()
    except Exception:
        pass
    sender = shutil.which("notify-send")
    if sender:
        try:
            subprocess.Popen(
                [sender, "--app-name=uachat", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


def copy_to_clipboard(text: str) -> None:
    """OSC 52 works through WSL/SSH without extra tooling."""
    import base64

    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sys.stdout.write(f"\x1b]52;c;{encoded}\x07")
    sys.stdout.flush()


def dump_transcript(session: str, entries: list[tuple[str, str]]) -> str:
    directory = os.path.join(STATE_DIR, "transcripts")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{session}-{time.strftime('%Y%m%d-%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"# uachat transcript · session {session}\n\n")
        for role, text in entries:
            handle.write(f"## {role}\n\n{text}\n\n")
    return path


def fallback_models() -> list[str]:
    if models_module is not None:
        return list(getattr(models_module, "FALLBACK_MODELS", []))
    return []


def effort_levels() -> list[str]:
    if models_module is not None:
        return list(models_module.effort_options())
    return ["low", "medium", "high", "xhigh", "max"]


def command_suggestions(text: str, theme: "Theme", provider: str = "") -> list[tuple[str, str]]:
    """(replacement token, description) pairs for live hints above the input.

    The editor inserts the token at the cursor, so entries are token-relative:
    before the space it completes command names, after it completes arguments.
    """
    if not text.startswith("/"):
        return []
    head, _, rest = text.partition(" ")
    rest = rest.strip()
    if " " not in text:  # completing the command itself
        return [(name, COMMAND_HELP.get(name, "")) for name in COMMANDS if name.startswith(text)]
    if head == "/model":
        return [
            (item, provider or DEFAULT_PROVIDER)
            for item in available_models(provider)
            if not rest or rest.lower() in item.lower()
        ][:10]
    if head == "/provider":
        return [
            (item, provider_spec(item).get("label", item))
            for item in provider_ids()
            if not rest or item.startswith(rest)
        ]
    if head == "/thinking":
        return [
            (level, "reasoning effort")
            for level in effort_levels()
            if not rest or level.startswith(rest)
        ]
    if head == "/theme":
        return [
            (name, "theme")
            for name in theme.names()
            if not rest or name.startswith(rest)
        ]
    if head == "/resume":
        out: list[tuple[str, str]] = []
        for item in list_sessions():
            name = str(item["name"])
            if not rest or name.startswith(rest):
                out.append((name, str(item["preview"])[:44] or "session"))
        return out[:10]
    return []


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def auto_update_enabled(config: dict[str, str]) -> bool:
    value = (os.environ.get("UACHAT_AUTO_UPDATE") or config.get("UACHAT_AUTO_UPDATE") or "on").strip().lower()
    return value not in ("off", "0", "false", "no", "never")


def kick_off_update_check(config: dict[str, str], interval_hours: float = 24.0) -> None:
    """Check upstream for a newer harness release in the background, once per interval."""
    if not auto_update_enabled(config) or not os.path.exists(UPDATE_PATH):
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        last = float(open(CHECK_STAMP, encoding="utf-8").read().strip() or 0)
    except (OSError, ValueError):
        last = 0.0
    if time.time() - last < interval_hours * 3600:
        return
    try:
        with open(CHECK_STAMP, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError:
        return
    subprocess.Popen(
        ["/usr/bin/env", "bash", UPDATE_PATH, "--quiet"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def flush_core_note(theme: Theme) -> None:
    try:
        note = open(NOTE_PATH, encoding="utf-8").read().strip()
    except OSError:
        return
    if not note:
        return
    print(theme.paint("  " + note, "ok"))
    try:
        os.remove(NOTE_PATH)
    except OSError:
        pass


class Bridge:
    """Runs bridge.py so providers that need custom headers look plain to the harness."""

    def __init__(self, target: str, key: str, session: str) -> None:
        self.target = target
        self.key = key
        self.session = session
        self.process: subprocess.Popen | None = None

    def start(self) -> str:
        port = free_port()
        environment = os.environ.copy()
        # Keep the upstream key out of `ps`: the child reads it from the environment.
        environment["UACHAT_BRIDGE_KEY"] = self.key
        # Let the bridge notice when this client dies instead of lingering.
        environment["UACHAT_PARENT_PID"] = str(os.getpid())
        self.process = subprocess.Popen(
            [
                sys.executable,
                BRIDGE_PATH,
                "--listen",
                f"127.0.0.1:{port}",
                "--target",
                self.target,
                "--session",
                self.session,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return f"http://127.0.0.1:{port}/v1"
            except OSError:
                if self.process.poll() is not None:
                    raise RuntimeError(f"bridge exited with code {self.process.returncode}")
                time.sleep(0.05)
        raise RuntimeError("bridge did not start in time")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None


class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, theme: Theme, enabled: bool) -> None:
        self.theme = theme
        self.enabled = enabled
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.started_at = 0.0
        self.lock = threading.Lock()

    def start(self) -> None:
        if not self.enabled or self.thread is not None:
            return
        self.stop_event.clear()
        self.started_at = time.monotonic()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def _spin(self) -> None:
        index = 0
        with self.lock:
            while not self.stop_event.is_set():
                frame = self.FRAMES[index % len(self.FRAMES)]
                elapsed = time.monotonic() - self.started_at
                line = self.theme.paint(f"  {frame} ", "accent") + self.theme.paint(f"working {elapsed:5.1f}s", "dim")
                sys.stdout.write("\r\033[K" + line)
                sys.stdout.flush()
                index += 1
                self.stop_event.wait(0.1)

    def stop(self) -> None:
        if self.thread is None:
            return
        self.stop_event.set()
        self.thread.join()
        self.thread = None
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


# --------------------------------------------------------------------------- rendering


class Renderer:
    def __init__(self, theme: Theme) -> None:
        self.theme = theme
        self.width = shutil.get_terminal_size((100, 24)).columns
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.started_at = time.monotonic()
        self.printed_operations: set[str] = set()
        self.last_assistant = ""

    # --- events

    def begin_turn(self) -> None:
        self.started_at = time.monotonic()

    def handle(self, record: dict) -> None:
        kind = record.get("Kind")
        if kind == "model_response":
            self.model_response(record.get("Data") or {})
        elif kind == "tool_call_status":
            self.tool_call_status(record.get("Data") or {})
        elif kind is None and record.get("type") == "error":
            self.error(str(record.get("message") or ""))

    def model_response(self, data: dict) -> None:
        response = data.get("Response") or {}
        failure = response.get("Failure")
        if failure:
            self.error(f"model failure: {failure.get('Message') or failure}")
        for item in response.get("Output") or []:
            item_type = item.get("Type")
            payload = item.get("Data") or {}
            if item_type == "message":
                self.assistant_message(str(payload.get("Text") or ""))
            elif item_type == "reasoning":
                summary = short(" ".join(payload.get("Summary") or []), 200)
                if summary:
                    print("  " + self.theme.paint(f"· {summary}", "dim", ITALIC))
            elif item_type == "tool_call":
                name, detail = describe_call(payload)
                marker = self.theme.paint("⏵ ", "tool")
                print(f"  {marker}{self.theme.paint(name, 'tool', BOLD)} {self.theme.paint(detail, 'dim')}")
        usage = response.get("Usage") or {}
        self.input_tokens += int(usage.get("InputTokens") or 0)
        self.output_tokens += int(usage.get("OutputTokens") or 0)
        self.cached_tokens += int(usage.get("CachedInputTokens") or 0)

    def assistant_message(self, text: str) -> None:
        text = text.strip("\n")
        if not text:
            return
        self.last_assistant = text
        print()
        print("  " + self.theme.paint("⏺ ", "agent") + self.theme.paint("agent", "label"))
        gutter = self.theme.paint("  │ ", "dim")
        for raw in text.split("\n"):
            if not raw.strip():
                print()
                continue
            for line in textwrap.wrap(raw, width=max(20, self.width - 6)) or [""]:
                print(gutter + line)
        print()

    def tool_call_status(self, data: dict) -> None:
        status = data.get("Status") or {}
        if status.get("Error"):
            self.error(f"tool call rejected: {status['Error']}")
        for operation in data.get("Operations") or []:
            operation_id = str(operation.get("ID") or "")
            state = operation.get("State") or {}
            result = state.get("Result") or {}
            status_name = operation.get("Status")
            if operation_id in self.printed_operations:
                continue
            if status_name == "completed":
                self.printed_operations.add(operation_id)
                self.tool_result(state, result)
            elif status_name in ("failed", "canceled"):
                self.printed_operations.add(operation_id)
                self.error(f"operation {status_name}: {state.get('TerminalError') or operation_id}")

    def tool_result(self, state: dict, result: dict) -> None:
        output = str(result.get("Out") or "").rstrip("\n")
        error_output = str(result.get("Err") or "").rstrip("\n")
        combined = output
        if error_output.strip():
            combined = (combined + "\n" + error_output).strip("\n")
        lines = combined.split("\n") if combined else []
        for index, line in enumerate(lines[:MAX_RESULT_LINES]):
            prefix = "    ⎿ " if index == 0 else "      "
            print(self.theme.paint(prefix + short(line, self.width - 8), "result"))
        if len(lines) > MAX_RESULT_LINES:
            path = state.get("OutPath") or ""
            print(self.theme.paint(f"      … {len(lines) - MAX_RESULT_LINES} more lines ({path})", "dim"))
        exit_code = result.get("ExitCode")
        if exit_code not in (0, None):
            self.error(f"exit code {exit_code}")

    def error(self, message: str) -> None:
        print("  " + self.theme.paint("✖ ", "error") + self.theme.paint(message, "error"))

    def end_turn(self, code: int) -> None:
        elapsed = time.monotonic() - self.started_at
        parts = []
        if self.input_tokens or self.output_tokens:
            cached = f" · cached {self.cached_tokens}" if self.cached_tokens else ""
            parts.append(f"in {self.input_tokens}{cached} · out {self.output_tokens}")
        parts.append(f"{elapsed:.1f}s")
        print(self.theme.paint("  ╵ " + " · ".join(parts), "dim"))
        if code not in (0, 130):
            self.error(f"runner exited with code {code}")


def describe_call(payload: dict) -> tuple[str, str]:
    name = str(payload.get("Name") or "?")
    raw = str(payload.get("Arguments") or "")
    try:
        arguments = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return name, short(raw)
    if isinstance(arguments, dict) and "command" in arguments:
        return "Bash", short(str(arguments["command"]))
    if isinstance(arguments, dict) and set(arguments) == {"path"}:
        return name, short(str(arguments["path"]))
    return name, short(json.dumps(arguments, ensure_ascii=False))


# --------------------------------------------------------------------------- turn execution


def terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_turn(binary: str, workspace: str, request: dict, renderer: Renderer, spinner: Spinner) -> int:
    try:
        proc = subprocess.Popen(
            [binary, "-workspace", workspace],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except FileNotFoundError:
        renderer.error(f"runner binary not found: {binary}")
        return 127
    renderer.begin_turn()
    try:
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(request, ensure_ascii=False))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        assert proc.stdout is not None
        spinner.start()
        for line in proc.stdout:
            spinner.stop()
            line = line.strip()
            if line:
                try:
                    renderer.handle(json.loads(line))
                except json.JSONDecodeError:
                    renderer.error(f"runner: {line}")
            spinner.start()
        spinner.stop()
        code = proc.wait()
    except KeyboardInterrupt:
        spinner.stop()
        terminate(proc)
        print()
        print(renderer.theme.paint("  [interrupted]", "warn"))
        return 130
    renderer.end_turn(code)
    return code


# --------------------------------------------------------------------------- input


def setup_readline(theme: Theme) -> None:
    """Tab completion and history when running on a terminal."""
    if readline is None or not sys.stdin.isatty():
        return
    import atexit

    try:
        readline.read_history_file(HISTORY_PATH)
    except OSError:
        pass
    readline.set_history_length(500)
    atexit.register(_save_history)

    def completer(text: str, state_index: int) -> str | None:
        buffer = readline.get_line_buffer()
        matches: list[str] = []
        if buffer.startswith("/theme ") or buffer.startswith("/themes "):
            matches = [name for name in theme.names() if name.startswith(text)]
        elif buffer.startswith("/model "):
            matches = [item for item in available_models() if text.lower() in item.lower()][:10]
        elif buffer.startswith("/thinking "):
            matches = [level for level in effort_levels() if level.startswith(text)]
        elif buffer.startswith("/resume "):
            matches = [
                str(item["name"]) for item in list_sessions()
                if str(item["name"]).startswith(text)
            ]
        elif text.startswith("/"):
            matches = [command for command in COMMANDS if command.startswith(text)]
        return matches[state_index] if state_index < len(matches) else None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")


def _save_history() -> None:
    if readline is None:
        return
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        readline.write_history_file(HISTORY_PATH)
    except OSError:
        pass


def banner(theme: Theme, workspace: str, session: str, model: str, endpoint: str, provider: str = "") -> str:
    rows = [
        ("workspace", workspace),
        ("session", f"{session}   /sessions · /new [name]"),
        ("provider", f"{provider}   /provider · /model"),
        ("model", model),
        ("endpoint", endpoint),
        ("theme", f"{theme.name}   /themes · /theme <name>"),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [theme.paint(f"  uachat {VERSION}", "title", BOLD) + theme.paint(" · unreal-agent harness client", "dim")]
    for label, value in rows:
        lines.append("  " + theme.paint(f"{label:<{width}}", "label") + "  " + value)
    lines.append(theme.paint("  /help for commands · Tab completes · /exit to leave", "dim"))
    return "\n".join(lines)


HELP = """commands:
  /help            this text
  /new [name]      start a fresh session (named or generated)
  /sessions        list recent sessions with their first prompt
  /resume <name>   switch to an existing session
  /session         print the current session id
  /provider [id]   show or switch the provider (opencode-go, openrouter, openai, fireworks, ollama)
  /model [id]      show or switch the model (/model refresh re-reads the provider)
  /thinking [lvl]  show or switch the reasoning effort (/thinking next cycles)
  /themes          list themes
  /theme <name>    switch theme and remember it
  /copy            copy the last answer to the clipboard (OSC 52)
  /dump            write the transcript to ~/.local/state/uachat/transcripts
  /repair [name]   copy the session without duplicate tool outputs, then /resume <name>-rep
  /exit, /quit     leave
anything else is sent to the agent as a prompt.
Ctrl-C interrupts the current turn; Ctrl-C twice at the prompt leaves."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uachat",
        description="Chat client for the unreal-agent harness (one runner process per prompt).",
    )
    parser.add_argument("-p", "--prompt", help="send one prompt and exit instead of starting a chat")
    parser.add_argument("-w", "--workspace", default=".", help="agent workspace (default: current directory)")
    parser.add_argument("-s", "--session", help="session id to create or resume")
    parser.add_argument("-m", "--model", help="model id (sets the request's model field)")
    parser.add_argument("-t", "--thinking", choices=["low", "medium", "high", "xhigh", "max"], help="thinking level")
    parser.add_argument("--theme", help="colour theme (see --themes)")
    parser.add_argument("--themes", action="store_true", help="list themes and exit")
    parser.add_argument("--version", action="store_true", help="print the client version and exit")
    parser.add_argument("--list", action="store_true", help="list recent sessions and exit")
    parser.add_argument("--provider", help="sets UNREAL_HARNESS_LLM_PROVIDER")
    parser.add_argument("--binary", default="unreal-agent-runner", help="runner binary path")
    parser.add_argument("--update-core", action="store_true", help="update the unreal-agent runner from upstream and exit")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto", help="colour output")
    parser.add_argument("--no-color", action="store_true", help="same as --color never")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # An explicitly exported base URL (outside ~/.config/uachat/env) wins and
    # keeps the bridge out of the way, e.g. when pointing at a local mock.
    explicit_base_url = os.environ.get("UNREAL_HARNESS_LLM_BASE_URL", "").strip()
    config = load_config()
    if args.no_color:
        args.color = "never"
    if args.color == "auto":
        override = (os.environ.get("UACHAT_COLOR") or "").strip().lower()
        if override in ("always", "never", "auto"):
            args.color = override
        elif os.environ.get("NO_COLOR"):
            args.color = "never"
    if args.color == "always":
        use_color = True
    elif args.color == "never":
        use_color = False
    else:
        use_color = sys.stdout.isatty()

    theme = Theme(args.theme or config.get("UACHAT_THEME") or "midnight", use_color)
    if args.themes:
        for name in theme.names():
            marker = " (current)" if name == theme.name else ""
            print(f"{name}{marker}")
        return 0
    if args.version:
        modules = [
            name for name, module in (("editor", editor_module), ("models", models_module), ("providers", providers_module))
            if module is not None
        ]
        print(f"uachat {VERSION} · modules: {', '.join(modules) or 'none'}")
        return 0
    if args.list:
        for item in list_sessions():
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(item["mtime"])))
            print(f"{item['name']:<28} {stamp}  {item['preview']}")
        return 0
    if args.update_core:
        return subprocess.call(["/usr/bin/env", "bash", UPDATE_PATH])

    workspace = os.path.abspath(args.workspace)
    session_id = args.session or uuid.uuid4().hex[:12]
    provider = (os.environ.get("UACHAT_PROVIDER") or config.get("UACHAT_PROVIDER") or DEFAULT_PROVIDER).strip()
    if args.provider:
        os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = args.provider

    bridge: Bridge | None = None

    def apply_environment(current_session: str) -> str:
        """Point the harness at the selected provider (through the bridge when needed)."""
        nonlocal bridge
        if bridge is not None:
            bridge.stop()
            bridge = None
        if explicit_base_url:
            return explicit_base_url
        spec = provider_spec(provider)
        key = provider_key(provider)
        if spec.get("bridge"):
            if not key:
                return f"(no key for {provider})"
            bridge = Bridge(spec["base_url"], key, current_session)
            base_url = bridge.start()
            os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = spec.get("harness", "openai")
            os.environ["UNREAL_HARNESS_LLM_BASE_URL"] = base_url
            os.environ["UNREAL_HARNESS_LLM_API_KEY"] = "bridge"
            return f"{base_url} → {spec['base_url']}"
        os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = spec.get("harness", provider)
        os.environ["UNREAL_HARNESS_LLM_BASE_URL"] = spec.get("base_url", "")
        if key:
            os.environ["UNREAL_HARNESS_LLM_API_KEY"] = key
        else:
            os.environ.pop("UNREAL_HARNESS_LLM_API_KEY", None)
        # OAuth providers (codex/ChatGPT) hand the harness an auth file instead of a key.
        if providers_module is not None:
            try:
                for name, value in providers_module.auth_env(provider).items():
                    os.environ[name] = value
            except Exception as error:
                return f"({provider}: {error})"
        return spec.get("base_url", "(provider default)")

    def build_request(prompt: str) -> dict:
        request: dict = {"prompt": prompt, "session_id": session_id}
        if args.model:
            request["model"] = args.model
        thinking = args.thinking or os.environ.get("UACHAT_THINKING")
        if thinking:
            request["thinking_level"] = thinking
        return request

    spinner_on = use_color and sys.stdout.isatty()
    try:
        endpoint = apply_environment(session_id)
        model = args.model or os.environ.get("UNREAL_HARNESS_LLM_MODEL") or "(provider default)"
        thinking = args.thinking or os.environ.get("UACHAT_THINKING") or ""
        model_line = f"{model} · {thinking_glyph(thinking)} {thinking}" if thinking else str(model)

        if args.prompt:
            kick_off_update_check(config)
            renderer = Renderer(theme)
            spinner = Spinner(theme, spinner_on)
            return run_turn(args.binary, workspace, build_request(args.prompt), renderer, spinner)

        print(banner(theme, workspace, session_id, model_line, str(endpoint), provider))
        flush_core_note(theme)
        kick_off_update_check(config)
        setup_readline(theme)

        state = {"model": args.model or os.environ.get("UNREAL_HARNESS_LLM_MODEL", ""), "thinking": thinking}
        # The provider catalogue moves: keep a stale configured model from breaking the first turn.
        known = available_models(provider, refresh=True)
        if known and state["model"] and state["model"] not in known:
            picked = choose_model(provider, known, state["model"])
            if picked:
                print(theme.paint(f"  model {state['model']} is gone from {provider}; using {picked}", "warn"))
                state["model"] = picked
                os.environ["UNREAL_HARNESS_LLM_MODEL"] = picked
                save_config_value("UNREAL_HARNESS_LLM_MODEL", picked)
        transcript: list[tuple[str, str]] = []
        reader = None
        if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
            try:
                reader = editor_module.Editor(
                    history_path=HISTORY_PATH,
                    suggestions=lambda text: command_suggestions(text, theme, provider),
                    paint=lambda text, key: theme.paint(text, key),
                )
            except Exception:
                reader = None
        last_interrupt = 0.0
        while True:
            try:
                if reader is not None:
                    line = reader.read_line(theme.paint("› ", "user", BOLD))
                else:
                    line = input(theme.paint("› ", "user", BOLD))
                last_interrupt = 0.0
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                now = time.monotonic()
                if now - last_interrupt <= DOUBLE_INTERRUPT_WINDOW:
                    print(theme.paint("  bye", "dim"))
                    break
                last_interrupt = now
                print(theme.paint("  Ctrl-C again to exit", "warn"))
                continue
            prompt = line.strip()
            if not prompt:
                continue
            if prompt in ("/exit", "/quit"):
                break
            if prompt == "/help":
                print(HELP)
                continue
            if prompt == "/session":
                print(session_id)
                continue
            if prompt == "/themes":
                print(", ".join(f"*{name}*" if name == theme.name else name for name in theme.names()))
                continue
            if prompt.startswith("/provider"):
                _, _, value = prompt.partition(" ")
                value = value.strip()
                known = provider_ids()
                if not value:
                    if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
                        p_items = [
                            (item, f"{provider_spec(item).get('base_url', '')} [{credential_state(item)}]")
                            for item in known
                        ]
                        chosen = editor_module.pick(
                            p_items,
                            title="Select provider",
                            current=provider,
                            theme_paint=theme.paint,
                            filterable=False,
                        )
                        if not chosen:
                            continue
                        value = chosen
                    else:
                        print(f"current provider: {provider}")
                        for item in known:
                            item_spec = provider_spec(item)
                            mark = "*" if item == provider else " "
                            print(f" {mark} {item:<14} {item_spec.get('base_url', ''):<40} {credential_state(item)}")
                        print(theme.paint("   /provider <id> · /model lists that provider's models", "dim"))
                        continue
                if value not in known:
                    print(theme.paint(f"unknown provider {value}; known: {', '.join(known)}", "error"))
                    continue
                provider = value
                os.environ["UACHAT_PROVIDER"] = value
                save_config_value("UACHAT_PROVIDER", value)
                try:
                    endpoint = apply_environment(session_id)
                    print(f"provider: {value} → {endpoint}")
                except RuntimeError as error:
                    print(theme.paint(f"error: {error}", "error"))
                ids = available_models(provider, refresh=True)
                current = state["model"]
                if ids and current and current not in ids:
                    picked = choose_model(provider, ids, current)
                    if picked:
                        state["model"] = picked
                        os.environ["UNREAL_HARNESS_LLM_MODEL"] = picked
                        save_config_value("UNREAL_HARNESS_LLM_MODEL", picked)
                        print(f"model: {picked} (default for {value})")
                elif not ids:
                    print(theme.paint(f"   note: no model list for {value} (key? server?)", "warn"))
                continue
            if prompt.startswith("/model"):
                _, _, value = prompt.partition(" ")
                value = value.strip()
                if not value:
                    ids = available_models(provider)
                    if editor_module is not None and getattr(editor_module, "AVAILABLE", False) and ids:
                        m_items = [(item, provider) for item in ids]
                        chosen = editor_module.pick(
                            m_items,
                            title=f"Select model for {provider}",
                            current=state["model"],
                            theme_paint=theme.paint,
                            filterable=True,
                            max_rows=12,
                        )
                        if not chosen:
                            continue
                        value = chosen
                    else:
                        print(f"provider: {provider} · current model: {state['model'] or '(provider default)'}")
                        for index, item in enumerate(ids[:16], start=1):
                            mark = "*" if item == state["model"] else " "
                            print(f" {mark} {item}")
                        if len(ids) > 16:
                            print(theme.paint(f"   … {len(ids) - 16} more (/model <substring>)", "dim"))
                        print(theme.paint("   /model <id> · /model refresh", "dim"))
                        continue
                if value == "refresh":
                    print(f"models: {len(available_models(provider, refresh=True))}")
                    continue
                known = available_models(provider)
                state["model"] = value
                os.environ["UNREAL_HARNESS_LLM_MODEL"] = value
                save_config_value("UNREAL_HARNESS_LLM_MODEL", value)
                suffix = "" if not known or value in known else theme.paint(" (not in the provider list)", "warn")
                print(f"model: {value}{suffix}")
                if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
                    t_items = [(lvl, f"effort {thinking_glyph(lvl)}") for lvl in effort_levels()]
                    chosen_thinking = editor_module.pick(
                        t_items,
                        title=f"Select reasoning effort for {value}",
                        current=state["thinking"],
                        theme_paint=theme.paint,
                        filterable=False,
                    )
                    if chosen_thinking:
                        state["thinking"] = chosen_thinking
                        os.environ["UACHAT_THINKING"] = chosen_thinking
                        save_config_value("UACHAT_THINKING", chosen_thinking)
                        print(f"thinking: {thinking_glyph(chosen_thinking)} {chosen_thinking}")
                continue
            if prompt.startswith("/thinking"):
                _, _, value = prompt.partition(" ")
                value = value.strip()
                levels = effort_levels()
                if not value:
                    if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
                        t_items = [(lvl, f"effort {thinking_glyph(lvl)}") for lvl in levels]
                        chosen = editor_module.pick(
                            t_items,
                            title="Select reasoning effort",
                            current=state["thinking"],
                            theme_paint=theme.paint,
                            filterable=False,
                        )
                        if not chosen:
                            continue
                        value = chosen
                    else:
                        print(f"current thinking: {state['thinking'] or '(provider default)'}")
                        print("   " + ", ".join(f"*{level}*" if level == state["thinking"] else level for level in levels))
                        print(theme.paint("   /thinking <level> · /thinking next", "dim"))
                        continue
                if value == "next":
                    current = state["thinking"] if state["thinking"] in levels else levels[-1]
                    value = levels[(levels.index(current) + 1) % len(levels)]
                elif value not in levels:
                    print(theme.paint(f"unknown level {value}; use one of {', '.join(levels)}", "error"))
                    continue
                state["thinking"] = value
                os.environ["UACHAT_THINKING"] = value
                save_config_value("UACHAT_THINKING", value)
                print(f"thinking: {thinking_glyph(value)} {value}")
                continue
            if prompt == "/copy" or prompt.startswith("/copy "):
                text = transcript[-1][1] if transcript else ""
                if not text:
                    print(theme.paint("nothing to copy yet", "warn"))
                else:
                    copy_to_clipboard(text)
                    print(f"copied {len(text)} characters")
                continue
            if prompt == "/dump" or prompt.startswith("/dump "):
                if not transcript:
                    print(theme.paint("nothing to dump yet", "warn"))
                else:
                    path = dump_transcript(session_id, transcript)
                    print(f"transcript: {path}")
                continue
            if prompt == "/repair" or prompt.startswith("/repair "):
                _, _, value = prompt.partition(" ")
                value = value.strip() or session_id
                try:
                    completed = subprocess.run(
                        [sys.executable, REPAIR_PATH, value],
                        capture_output=True, text=True, timeout=180,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    print(theme.paint(f"repair failed: {error}", "error"))
                    continue
                output = (completed.stdout or completed.stderr or "").strip()
                print("\n".join("  " + line for line in output.splitlines()))
                continue
            if prompt.startswith("/theme"):
                _, _, name = prompt.partition(" ")
                name = name.strip()
                if not name:
                    if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
                        th_items = [(t_name, "colour theme") for t_name in theme.names()]
                        chosen = editor_module.pick(
                            th_items,
                            title="Select colour theme",
                            current=theme.name,
                            theme_paint=theme.paint,
                            filterable=False,
                        )
                        if not chosen:
                            continue
                        name = chosen
                    else:
                        print(f"current theme: {theme.name}; available: {', '.join(theme.names())}")
                        continue
                if name not in THEMES:
                    print(theme.paint(f"unknown theme {name}; available: {', '.join(theme.names())}", "error"))
                else:
                    theme = Theme(name, use_color)
                    save_config_value("UACHAT_THEME", name)
                    print(f"theme: {name} (saved)")
                continue
            if prompt == "/sessions":
                items_raw = list_sessions()
                if not items_raw:
                    print("no sessions yet")
                    continue
                if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
                    s_items = []
                    for item in items_raw:
                        stamp = time.strftime("%m-%d %H:%M", time.localtime(float(item["mtime"])))
                        desc = f"{stamp}  {item['preview']}" if item["preview"] else stamp
                        s_items.append((str(item["name"]), desc))
                    chosen = editor_module.pick(
                        s_items,
                        title="Select session to resume",
                        current=session_id,
                        theme_paint=theme.paint,
                        filterable=True,
                        max_rows=10,
                    )
                    if not chosen:
                        continue
                    session_id = chosen
                    try:
                        apply_environment(session_id)
                    except RuntimeError as error:
                        print(theme.paint(f"error: {error}", "error"))
                    print(f"session: {session_id}")
                    continue
                else:
                    for item in items_raw:
                        stamp = time.strftime("%m-%d %H:%M", time.localtime(float(item["mtime"])))
                        marker = "*" if item["name"] == session_id else " "
                        print(f"{marker} {item['name']:<28} {stamp}  {item['preview']}")
                    continue
            if prompt.startswith("/resume"):
                _, _, name = prompt.partition(" ")
                name = name.strip()
                if not name:
                    if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
                        items_raw = list_sessions()
                        s_items = []
                        for item in items_raw:
                            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(item["mtime"])))
                            desc = f"{stamp}  {item['preview']}" if item["preview"] else stamp
                            s_items.append((str(item["name"]), desc))
                        chosen = editor_module.pick(
                            s_items,
                            title="Select session to resume",
                            current=session_id,
                            theme_paint=theme.paint,
                            filterable=True,
                            max_rows=10,
                        )
                        if not chosen:
                            continue
                        name = chosen
                if not valid_session_name(name) or not os.path.exists(session_path(name)):
                    print(theme.paint(f"no such session: {name or '(none given)'}", "error"))
                    continue
                session_id = name
                try:
                    apply_environment(session_id)
                except RuntimeError as error:
                    print(theme.paint(f"error: {error}", "error"))
                print(f"session: {session_id}")
                continue
            if prompt.startswith("/new"):
                _, _, name = prompt.partition(" ")
                name = name.strip() or uuid.uuid4().hex[:12]
                if not valid_session_name(name):
                    print(theme.paint("session name cannot contain slashes", "error"))
                    continue
                session_id = name
                try:
                    apply_environment(session_id)
                except RuntimeError as error:
                    print(theme.paint(f"error: {error}", "error"))
                print(f"new session: {session_id}")
                continue
            renderer = Renderer(theme)
            spinner = Spinner(theme, spinner_on)
            code = run_turn(args.binary, workspace, build_request(prompt), renderer, spinner)
            transcript.append(("you", prompt))
            if renderer.last_assistant.strip():
                transcript.append(("agent", renderer.last_assistant.strip()))
            if code == 0:
                notify("uachat", "turn complete")
            elif code == 130:
                # A second Ctrl-C right after an interrupt leaves the client.
                last_interrupt = time.monotonic()
                notify("uachat", "turn interrupted")
            else:
                notify("uachat", f"turn failed (exit {code})")
        return 0
    finally:
        if bridge is not None:
            bridge.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Ctrl-C landed outside the prompt loop (e.g. between turns): leave cleanly.
        print("\n  bye")
        raise SystemExit(130)

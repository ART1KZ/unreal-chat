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
import secrets
import select
from collections import deque
import tempfile
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from typing import Callable
from terminal_ui import safe_text, terminal_size, SessionMetrics, TerminalFooter, item_record, count, positive_int, tokens_label, normalize_event, wrap_text
from configio import save_value, file_lock, LockBusyError
from contextlib import ExitStack

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

class ShutdownRequested(BaseException):
    def __init__(self, signum):
        self.signum = signum


def request_shutdown(signum, frame):
    raise ShutdownRequested(signum)


VERSION = "0.5.1"

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
STREAM_STATE_PATH = os.path.join(STATE_DIR, "stream-state.json")


def read_stream_state() -> dict | None:
    try:
        with open(STREAM_STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and time.monotonic() - float(data.get("updated_at", 0)) < 3.0:
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None
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
    "/doctor", "/usage", "/accounts", "/rotation", "/status", "/context", "/login", "/logout", "/auth", "/help", "/new", "/sessions", "/resume", "/session",
    "/provider", "/model", "/thinking", "/theme", "/themes", "/rtk", "/copy", "/dump", "/repair", "/exit", "/quit",
)
COMMAND_HELP = {
    "/doctor": "read-only auth/browser diagnostics (no token values)",
    "/usage": "Codex subscription quotas and resets",
    "/accounts": "list saved Codex accounts",
    "/rotation": "automatic account rotation on/off",
    "/status": "current model, effort and measured context",
    "/context": "set context limit in tokens, or auto",
    "/login": "OAuth login (--headless supported)",
    "/logout": "remove uachat Codex credentials",
    "/auth": "show authorization status",
    "/help": "this text",
    "/new": "start a fresh session",
    "/sessions": "list recent sessions",
    "/resume": "switch to an existing session",
    "/session": "print the current session id",
    "/rtk": "show RTK token-saving status and stats",
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
    values = models_module.load_env(path) if models_module else {}
    for name, value in values.items():
        if not os.environ.get(name):
            os.environ[name] = value
    return values


def save_config_value(name: str, value: str, path: str = CONFIG_PATH) -> None:
    try:
        save_value(path, name, value)
    except (OSError, ValueError) as error:
        print(f"  warning: could not save setting {name}: {safe_text(error)}", file=sys.stderr)


# --------------------------------------------------------------------------- theme


class Theme:
    def __init__(self, name: str, enabled: bool) -> None:
        self.name = name if name in THEMES else "midnight"
        self.enabled = enabled
        self.palette = THEMES[self.name]

    def paint(self, text: str, key: str, style: str = "") -> str:
        text = safe_text(text)
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
    if not valid_session_name(name):
        raise ValueError("invalid session id")
    return os.path.join(SESSION_DIR, f"{name}.session.jsonl")


def iter_session_records(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    rec = normalize_event(json.loads(line))
                except ValueError:
                    continue
                if rec:
                    yield rec
    except OSError:
        return


def list_sessions(limit: int = 15) -> list[dict[str, object]]:
    try:
        names = os.listdir(SESSION_DIR)
    except OSError:
        return []
    entries = []
    suffix = ".session.jsonl"
    for filename in names:
        if not filename.endswith(suffix): continue
        name = filename[:-len(suffix)]
        if not valid_session_name(name): continue
        path = session_path(name)
        try:
            info = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path): continue
        entries.append((info.st_mtime, name, path, info.st_size))
    entries.sort(reverse=True)
    return [{"name":name, "mtime":stamp, "size":size, "preview":session_preview(path)}
            for stamp, name, path, size in entries[:limit]]


def session_preview(path: str) -> str:
    for index, record in enumerate(iter_session_records(path)):
        if index >= 200: break
        data = record.get("Data") or {}
        if record.get("Kind") == "input" and data.get("Kind") == "external":
            payload = data.get("Payload")
            if isinstance(payload, dict): payload = payload.get("Text") or ""
            return short(safe_text(payload or ""), 60)
    return ""

def clear_screen() -> None:
    """Clear the terminal screen and scrollback buffer when in a terminal."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[3J\x1b[H\x1b[2J")
        sys.stdout.flush()


def replay_session(session_id: str, theme: Theme, model: str = "", thinking: str = "", max_turns: int = 5) -> list[tuple[str, str]]:
    """Display past turns from a resumed session and return the transcript entries."""
    path = session_path(session_id)
    if not os.path.isfile(path):
        return []
    turns = deque(maxlen=max(1, max_turns))
    total = 0
    current_turn = {"user":None, "tools":[], "agent":None}
    for rec in iter_session_records(path):
        kind, data = rec.get("Kind"), rec.get("Data") or {}
        if kind == "input" and data.get("Kind") == "external":
            if current_turn["user"] is not None or current_turn["agent"] is not None:
                turns.append(current_turn)
                total += 1
                current_turn = {"user":None, "tools":[], "agent":None}
            payload = data.get("Payload")
            current_turn["user"] = safe_text(payload.get("Text", "") if isinstance(payload, dict) else payload or "")
        elif kind == "model_response":
            for output in data["Response"]["Output"]:
                payload = output["Data"]
                if output.get("Type") == "message":
                    text = safe_text(payload.get("Text") or "")
                    current_turn["agent"] = ((current_turn["agent"]+"\n\n") if current_turn["agent"] else "")+text
                elif output.get("Type") == "tool_call" and len(current_turn["tools"]) < 50:
                    current_turn["tools"].append(safe_text(payload.get("Name") or "tool"))
    if current_turn["user"] is not None or current_turn["agent"] is not None:
        turns.append(current_turn)
        total += 1

    resumed_transcript: list[tuple[str, str]] = []
    for t in turns:
        if t["user"]:
            resumed_transcript.append(("you", t["user"]))
        if t["agent"]:
            resumed_transcript.append(("agent", t["agent"]))

    if not turns:
        return resumed_transcript

    shown = list(turns)
    omitted = total - len(shown)

    print()
    if omitted > 0:
        print(theme.paint(f"  ─── ({omitted} earlier turns omitted) ───", "dim"))
    for t in shown:
        if t["user"]:
            for row in list(wrap_text(t["user"], terminal_size().columns-4))[:8]:
                print("  " + theme.paint("› ", "user", BOLD) + row)
        if t["tools"]:
            tool_str = ", ".join(t["tools"])
            print("    " + theme.paint(f"⏵ {tool_str}", "tool"))
        if t["agent"]:
            print()
            hdr = "agent · replay"  # Historical model/effort are not reliably recorded by the runner.
            print("  " + theme.paint("⏺ ", "agent") + theme.paint(hdr, "label"))
            lines = list(wrap_text(t["agent"].strip(), terminal_size().columns-4))
            for line in lines[:8]:
                print("  " + theme.paint("│ ", "dim") + line)
            if len(lines) > 8:
                print("  " + theme.paint(f"  │ … ({len(lines) - 8} more lines)", "dim"))
        print()
    print(theme.paint(f"  ─── resumed session {session_id} ({total} turns loaded) ───\n", "dim"))
    return resumed_transcript


def valid_session_name(name: str) -> bool:
    return bool(name) and len(name) <= 128 and name not in (".", "..") and not any(c in name for c in "/\\") and all(ord(c) >= 32 and ord(c) != 127 for c in name)


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
    "google-antigravity": "gemini-3-flash",
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
    if not refresh and provider in _MODEL_CACHE:
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
    if value in ("off", "0", "false", "no") or not sys.stdout.isatty():
        return
    title, body = safe_text(title), safe_text(body)
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


def session_transcript(session):
    for rec in iter_session_records(session_path(session)):
        data = rec.get("Data") or {}
        if rec.get("Kind") == "input" and data.get("Kind") == "external":
            payload = data.get("Payload")
            yield "you", safe_text(payload.get("Text", "") if isinstance(payload, dict) else payload or "")
        elif rec.get("Kind") == "model_response":
            for output in data["Response"]["Output"]:
                if output.get("Type") == "message":
                    yield "agent", safe_text(output["Data"].get("Text") or "")


def dump_transcript(session: str, entries: list[tuple[str, str]]) -> str:
    # Stream the authoritative session file, not just the bounded replay buffer.
    path_on_disk = session_path(session)
    directory = os.path.join(STATE_DIR, "transcripts")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, f"{session}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.md")
    fd, temporary = tempfile.mkstemp(prefix=".transcript-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"# uachat transcript · session {session}\n\n")
            source = session_transcript(session) if os.path.isfile(path_on_disk) else entries
            for role, text in source:
                handle.write(f"## {role}\n\n{text}\n\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
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
            for item in (_MODEL_CACHE.get(provider or DEFAULT_PROVIDER) or
                         (providers_module._DEFINITIONS.get(provider, {}).get("models", []) if providers_module else []) or
                         (fallback_models() if provider == DEFAULT_PROVIDER else []))
            if not rest or rest.lower() in item.lower()
        ][:10]
    if head in ("/login", "/logout", "/auth"):
        return [(item, "OAuth provider") for item in ("openai-codex", "google-antigravity") if not rest or item.startswith(rest)]
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
        environment = os.environ.copy()
        environment["UACHAT_BRIDGE_KEY"] = self.key
        self.local_key = secrets.token_urlsafe(32)
        environment["UACHAT_BRIDGE_LOCAL_KEY"] = self.local_key
        environment["UACHAT_PARENT_PID"] = str(os.getpid())
        self.process = subprocess.Popen(
            [sys.executable, BRIDGE_PATH, "--listen", "127.0.0.1:0", "--target", self.target, "--session", self.session],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", env=environment, start_new_session=True)
        try:
            assert self.process.stdout is not None
            if select.select([self.process.stdout], [], [], 5)[0]:
                line = self.process.stdout.readline().strip()
                match = re.fullmatch(r"listening on 127\.0\.0\.1:(\d+)", line)
                if match:
                    return f"http://127.0.0.1:{match[1]}/v1"
            raise RuntimeError("bridge did not start (check URL/configuration)")
        except BaseException:
            self.stop()
            raise
        finally:
            if self.process and self.process.stdout:
                self.process.stdout.close()

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process = None


class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, theme: Theme, enabled: bool, footer=None) -> None:
        self.theme = theme
        self.enabled = enabled
        self.footer = footer
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.started_at = 0.0
        self.phase = "model"
        self.lock = threading.Lock()

    def set_phase(self, phase: str) -> None:
        self.phase = phase

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
                info = read_stream_state()
                status_text = ""
                if info:
                    state_kind = info.get("state")
                    if state_kind == "streaming":
                        tok_s = float(info.get("tok_s") or 0)
                        toks = int(info.get("tokens") or 0)
                        tok_disp = f"{toks / 1000:.1f}k" if toks >= 1000 else str(toks)
                        status_text = f"generating · ~{tok_s:.0f} tok/s · {tok_disp} tok"
                    elif state_kind == "awaiting":
                        status_text = f"thinking / awaiting model ({elapsed:4.1f}s)"
                if not status_text:
                    if self.phase == "thinking":
                        if elapsed < 5.0:
                            status_text = f"thinking ({elapsed:4.1f}s)"
                        elif elapsed < 20.0:
                            status_text = f"deep reasoning ({elapsed:4.1f}s)"
                        elif elapsed < 40.0:
                            status_text = f"deep reasoning in progress · model active ({elapsed:4.1f}s)"
                        else:
                            status_text = f"model taking longer than usual ({elapsed:4.1f}s)"
                    elif self.phase.startswith("executing"):
                        status_text = f"{self.phase} ({elapsed:4.1f}s)"
                    else:
                        if elapsed < 35.0:
                            status_text = f"working {elapsed:4.1f}s"
                        else:
                            status_text = f"working longer than usual ({elapsed:4.1f}s)"
                line = (
                    self.theme.paint(f"  {frame} ", "accent")
                    + self.theme.paint(status_text, "dim")
                    + self.theme.paint(" · Esc to interrupt", "dim")
                )
                if editor_module:
                    line = editor_module._fit(line, max(1, terminal_size().columns-1))
                sys.stdout.write("\r\033[K" + line)
                sys.stdout.flush()
                if self.footer:
                    self.footer.draw()
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
    def __init__(self, theme: Theme, model: str = "", thinking: str = "", on_usage=None) -> None:
        self.theme = theme
        self.on_usage = on_usage
        self.model = model
        self.thinking = thinking
        self.width = shutil.get_terminal_size((100, 24)).columns
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.started_at = time.monotonic()
        self.was_started = False
        self.printed_operations: set[str] = set()
        self.seen_responses: set[str] = set()
        self.last_assistant = ""
        self.last_error = ""
    # --- events

    def begin_turn(self) -> None:
        self.was_started = True
        self.started_at = time.monotonic()

    def handle(self, record: dict) -> None:
        record = normalize_event(record)
        kind = record.get("Kind")
        if kind == "model_response":
            self.model_response(record.get("Data") or {})
        elif kind == "tool_call_status":
            self.tool_call_status(record.get("Data") or {})
        elif kind is None and record.get("type") == "error":
            self.error(str(record.get("message") or ""))

    def model_response(self, data: dict) -> None:
        response = data.get("Response") or {}
        response_id = response.get("ID")
        if isinstance(response_id, str) and response_id:
            if response_id in self.seen_responses:
                return
            self.seen_responses.add(response_id)
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
        self.input_tokens += count(usage.get("InputTokens"))
        self.output_tokens += count(usage.get("OutputTokens"))
        self.cached_tokens += count(usage.get("CachedInputTokens"))
        if self.on_usage:
            self.on_usage(usage)

    def assistant_message(self, text: str) -> None:
        text = safe_text(text).strip("\n")
        self.width = terminal_size().columns
        if not text:
            return
        self.last_assistant += ("\n\n" if self.last_assistant else "") + text
        model_tag = self.model or "agent"
        if self.thinking:
            header = f"{model_tag} ({thinking_glyph(self.thinking)} {self.thinking})"
        else:
            header = model_tag
        print()
        print("  " + self.theme.paint("⏺ ", "agent") + self.theme.paint(header, "label"))
        gutter = self.theme.paint("  │ ", "dim")
        for raw in text.split("\n"):
            if not raw.strip():
                print()
                continue
            for line in wrap_text(raw, self.width - 6):
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
        self.width = terminal_size().columns
        output = safe_text(result.get("Out") or "").rstrip("\n")
        error_output = safe_text(result.get("Err") or "").rstrip("\n")
        combined = output
        if error_output.strip():
            combined = (combined + "\n" + error_output).strip("\n")
        raw_lines = combined.split("\n") if combined else []
        # Filter out noisy progress meters (curl / wget Dload/Upload bars)
        lines = []
        for line in raw_lines:
            s = line.strip()
            if not s:
                continue
            if "% Total" in s and "% Received" in s:
                continue
            if "Dload" in s and "Upload" in s and "Speed" in s:
                continue
            if re.match(r"^([\d\.\-kMG]+\s+)+([\d\.\-kMG]+|--:--:--|\d+:\d+:\d+)\s*$", s):
                continue
            lines.append(line)
        if not lines and combined.strip() and result.get("ExitCode") in (0, None):
            # All output was just a progress bar that finished cleanly
            print(self.theme.paint("    ⎿ (done)", "dim"))
        else:
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
        message = safe_text(message)
        if not self.last_error or not message.startswith("runner exited with code"):
            self.last_error = message
        print("  " + self.theme.paint("✖ ", "error") + self.theme.paint(message, "error"))

    def end_turn(self, code: int) -> None:
        elapsed = time.monotonic() - self.started_at
        parts = []
        if self.input_tokens or self.output_tokens:
            cached = f" · cached {self.cached_tokens}" if self.cached_tokens else ""
            out_part = f"out {self.output_tokens}"
            if elapsed > 0.3 and self.output_tokens > 0:
                speed = self.output_tokens / elapsed
                out_part += f" (~{speed:.0f} tok/s)"
            parts.append(f"in {self.input_tokens}{cached} · {out_part}")
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
    return safe_text(name), short(safe_text(json.dumps(arguments, ensure_ascii=False)))


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
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

class InterruptWatcher:
    """Raw input watcher; interrupts and preserves typed/pasted next-turn drafts."""

    def __init__(self, on_interrupt: Callable[[], None]) -> None:
        self.on_interrupt = on_interrupt
        self._pending = bytearray()
        self._paste = False
        self.overflow = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_term = None
        self._fd: int | None = None
        if os.name == "posix" and sys.stdin.isatty():
            try:
                import termios
                self._fd = sys.stdin.fileno()
                self._old_term = termios.tcgetattr(self._fd)
            except Exception:
                self._fd = None

    def start(self) -> None:
        if self._fd is None:
            return
        try:
            import termios, tty
            tty.setraw(self._fd, termios.TCSADRAIN)
            attributes = termios.tcgetattr(self._fd)
            attributes[1] = self._old_term[1]  # renderer print() still needs output CRLF processing
            termios.tcsetattr(self._fd, termios.TCSADRAIN, attributes)
            sys.stdout.write("\033[?2004h")
            sys.stdout.flush()
        except Exception:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _queue(self, data):
        if self.overflow:
            return
        self._pending.extend(data)
        if len(self._pending) > 8*1024*1024:
            self._pending.clear()
            self.overflow = True

    @property
    def pending_text(self):
        return safe_text(self._pending.decode("utf-8", "replace"))

    def _escape(self):
        sequence = bytearray()
        while len(sequence) < 32 and not self._stop.is_set():
            if not select.select([self._fd], [], [], .04 if not sequence else .02)[0]:
                break
            chunk = os.read(self._fd, 1)
            if not chunk: break
            sequence.extend(chunk)
            if sequence[0] not in (ord("["), ord("O")): break
            if len(sequence) > 1 and (chr(sequence[-1]).isalpha() or sequence[-1] in b"~@"):
                break
        return bytes(sequence)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not select.select([self._fd], [], [], .05)[0]:
                    continue
                ch = os.read(self._fd, 1)
                if not ch: break
                if ch == b"\x03":
                    self.on_interrupt()
                    break
                if ch == b"\x1b":
                    sequence = self._escape()
                    if sequence == b"[200~": self._paste = True
                    elif sequence == b"[201~": self._paste = False
                    elif not sequence and not self._paste and not self._stop.is_set():
                        self.on_interrupt()
                        break
                    elif sequence in (b"\r", b"\n"):
                        self._queue(b"\n")
                    continue
                if not self._paste and ch in (b"\x7f", b"\x08"):
                    index = len(self._pending)-1
                    while index > 0 and self._pending[index] & 0xc0 == 0x80:
                        index -= 1
                    if index >= 0: del self._pending[index:]
                elif not self._paste and ch == b"\x15":
                    self._pending.clear()
                    self.overflow = False
                elif ch in (b"\r", b"\n"):
                    if self._pending or self._paste: self._queue(b"\n")
                elif ch == b"\t":
                    self._queue(b"    ")
                elif ch[0] >= 32:
                    self._queue(ch)
            except (OSError, ValueError):
                break

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.4)
            self._thread = None
        if self._old_term is not None and self._fd is not None:
            try:
                sys.stdout.write("\033[?2004l")
                sys.stdout.flush()
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_term)
            except Exception:
                pass


def run_turn(binary: str, workspace: str, request: dict, renderer: Renderer, spinner: Spinner) -> int:
    env = os.environ.copy()
    rtk_shell = "/usr/local/bin/rtk-shell"
    if (os.environ.get("UACHAT_RTK") or "on").lower() not in ("off", "0", "false", "no") and os.path.isfile(rtk_shell):
        env["SHELL"] = rtk_shell
    try:
        proc = subprocess.Popen(
            [binary, "-workspace", workspace, "-session-directory", SESSION_DIR],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            start_new_session=True,
        )
    except OSError as error:
        renderer.error(f"cannot launch runner {safe_text(binary)}: {safe_text(error)}")
        return 127 if isinstance(error, FileNotFoundError) else 126
    renderer.begin_turn()
    interrupted_by_esc = False

    def on_esc():
        nonlocal interrupted_by_esc
        interrupted_by_esc = True
        terminate(proc)

    watcher = InterruptWatcher(on_esc)
    watcher.start()
    thinking_level = request.get("thinking_level")
    model_phase = "thinking" if thinking_level in ("high", "max", "xhigh") else "model"
    spinner.set_phase(model_phase)
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
                    record = normalize_event(json.loads(line))
                    renderer.handle(record)
                    kind = record.get("Kind")
                    if kind == "model_response":
                        outputs = (record.get("Data") or {}).get("Response", {}).get("Output") or []
                        tool_names = [o.get("Data", {}).get("Name") for o in outputs if o.get("Type") == "tool_call"]
                        if tool_names:
                            spinner.set_phase(f"executing {tool_names[0]}")
                        else:
                            spinner.set_phase(model_phase)
                    elif kind == "tool_call_status":
                        operations = record["Data"].get("Operations", [])
                        if not any(op.get("Status") not in ("completed", "failed", "canceled") for op in operations):
                            spinner.set_phase(model_phase)
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
    finally:
        spinner.stop()
        watcher.stop()
        renderer.pending_input = watcher.pending_text
        if watcher.overflow:
            renderer.error("queued draft exceeded 8 MiB and was discarded")
        if proc.poll() is None:
            terminate(proc)
        if proc.stdout:
            proc.stdout.close()
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()

    if interrupted_by_esc:
        spinner.stop()
        print()
        print(renderer.theme.paint("  [interrupted by Esc]", "warn"))
        return 130

    renderer.end_turn(code)
    return code


# --------------------------------------------------------------------------- input


def setup_readline(theme: Theme) -> None:
    """Tab completion and history when running on a terminal."""
    if readline is None or not sys.stdin.isatty() or (editor_module is not None and editor_module.AVAILABLE):
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
    return (theme.paint(f"  uachat {VERSION}", "title", BOLD)
            + theme.paint(" · unreal-agent harness client", "dim") + "\n"
            + theme.paint("  " + safe_text(workspace), "label") + "\n"
            + theme.paint("  /help · Tab completes · Alt+Enter newline · /exit", "dim"))


def status_line(theme, state, metrics, provider, session, context_limit=None):
    limit = context_limit
    if not limit and models_module:
        limit = models_module.context_window(provider_spec(provider).get("base_url", ""), state.get("model", ""))
    effort = state.get("thinking") or "high"
    parts = [safe_text(state.get("model") or "model default"),
             f"{thinking_glyph(effort)} {effort}", metrics.context_label(limit),
             safe_text(provider), "s:"+safe_text(session)]
    if provider == "openai-codex":
        if state.get("quota_usage") is not None:
            import codex_pool
            quota = codex_pool.quota_label(state["quota_usage"],state.get("model", ""))
            if state.get("quota_error_model") == state.get("model"):
                quota = "quota: лимит достигнут"
            parts.insert(3,quota)
    if metrics.output_tokens:
        parts.append("out "+tokens_label(metrics.output_tokens))
    if metrics.cached_tokens:
        parts.append("cached "+tokens_label(metrics.cached_tokens))
    if metrics.elapsed is not None:
        parts.append(f"{metrics.elapsed:.1f}s")
    style = "warn" if limit and metrics.context_tokens and metrics.context_tokens / limit >= .8 else "dim"
    return theme.paint(" " + " · ".join(parts), style)


HELP = """commands:
  /doctor [provider] read-only auth/browser diagnostics; never prints tokens
  /usage [--all] [--cached]  subscription quotas, reset times, credits (Codex)
  /accounts [use EMAIL_OR_ID] saved Codex accounts / switch account
  /rotation on|off automatic rotation between saved Codex accounts
  /status          current model, effort, context and usage
  /context [tokens|auto]   explicit context limit (no guessed percentages)
  /login [provider] [--headless]   standalone Codex OAuth
  /logout          remove uachat Codex credentials
  /auth            authorization status
  /help            this text
  /new [name]      start a fresh session (named or generated)
  /sessions        list recent sessions with their first prompt
  /resume <name>   switch to an existing session
  /session         print the current session id
  /provider [id]   show or switch the provider (includes Codex and experimental Antigravity)
  /model [id]      show or switch the model (/model refresh re-reads the provider)
  /thinking [lvl]  show or switch the reasoning effort (/thinking next cycles)
  /themes          list themes
  /theme <name>    switch theme and remember it
  /copy            copy the last answer to the clipboard (OSC 52)
  /dump            write the transcript to ~/.local/state/uachat/transcripts
  /rtk             show RTK token-saving status and stats
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
    parser.add_argument("--context-window", type=int, help="explicit context limit in tokens (otherwise catalogue metadata, never guessed)")
    parser.add_argument("--theme", help="colour theme (see --themes)")
    parser.add_argument("--themes", action="store_true", help="list themes and exit")
    parser.add_argument("--version", action="store_true", help="print the client version and exit")
    parser.add_argument("--list", action="store_true", help="list recent sessions and exit")
    parser.add_argument("--provider", help="select a client provider (e.g. openai-codex, google-antigravity)")
    parser.add_argument("--binary", default="unreal-agent-runner", help="runner binary path")
    parser.add_argument("--update-core", action="store_true", help="update the unreal-agent runner from upstream and exit")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto", help="colour output")
    parser.add_argument("--no-color", action="store_true", help="same as --color never")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Common appearance flags also work before auth subcommands.
    prefix, offset = [], 0
    while offset < len(raw):
        if raw[offset] == "--no-color":
            prefix.append(raw[offset]); offset += 1
        elif raw[offset] in ("--color", "--theme") and offset+1 < len(raw):
            prefix.extend(raw[offset:offset+2]); offset += 2
        else:
            break
    if offset and offset < len(raw) and raw[offset] in ("login", "logout", "auth", "usage", "doctor"):
        raw = raw[offset:]+prefix
    if raw and raw[0] in ("login", "logout", "auth", "usage", "doctor"):
        import native_auth
        action = raw.pop(0)
        if action == "auth":
            action = raw.pop(0) if raw else "status"
        return native_auth.main([action] + raw)
    args = build_parser().parse_args(raw)

    # An explicitly exported base URL (outside ~/.config/uachat/env) wins and
    # keeps the bridge out of the way, e.g. when pointing at a local mock.
    explicit_base_url = os.environ.get("UNREAL_HARNESS_LLM_BASE_URL", "").strip()
    explicit_provider = os.environ.get("UACHAT_PROVIDER") or os.environ.get("UNREAL_HARNESS_LLM_PROVIDER")
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

    if args.prompt is not None and not args.prompt.strip():
        print("prompt must not be empty", file=sys.stderr)
        return 2
    workspace = os.path.abspath(args.workspace)
    session_id = args.session or uuid.uuid4().hex[:12]
    if not valid_session_name(session_id):
        print("invalid session id", file=sys.stderr)
        return 2
    if not os.path.isdir(workspace):
        print(f"workspace is not a directory: {safe_text(workspace)}", file=sys.stderr)
        return 2
    context_limit = positive_int(args.context_window or os.environ.get("UACHAT_CONTEXT_WINDOW"))
    if args.context_window is not None and args.context_window <= 0:
        print("--context-window must be positive", file=sys.stderr)
        return 2
    provider = (args.provider or explicit_provider or config.get("UACHAT_PROVIDER") or DEFAULT_PROVIDER).strip()
    if args.provider:
        os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = args.provider
        if not args.model and provider in PROVIDER_DEFAULT_MODELS:
            os.environ["UNREAL_HARNESS_LLM_MODEL"] = PROVIDER_DEFAULT_MODELS[provider]

    if provider not in provider_ids():
        print(f"unknown provider: {safe_text(provider)}", file=sys.stderr)
        return 2
    state = {"model": args.model or os.environ.get("UNREAL_HARNESS_LLM_MODEL", ""),
             "thinking": args.thinking or os.environ.get("UACHAT_THINKING") or "high"}
    metrics = SessionMetrics()
    metrics.load(session_path(session_id))
    global STREAM_STATE_PATH
    STREAM_STATE_PATH = os.path.join(STATE_DIR, f"stream-state-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
    os.environ["UACHAT_STREAM_STATE_PATH"] = STREAM_STATE_PATH
    auth_status_signature = None
    footer = TerminalFooter(lambda: live_status_line(), enabled=not args.prompt)
    bridge: Bridge | None = None

    def apply_environment(current_session: str) -> str:
        """Point the harness at the selected provider (through the bridge when needed)."""
        nonlocal bridge
        if bridge is not None:
            bridge.stop()
            bridge = None
        os.environ.pop("OPENAI_CODEX_AUTH_FILE", None)
        if explicit_base_url:
            if provider == "openai-codex":
                import codex_auth
                os.environ["OPENAI_CODEX_AUTH_FILE"] = os.path.expanduser(codex_auth.CODEX_AUTH_PATH)
            os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = provider_spec(provider).get("harness", provider)
            os.environ["UNREAL_HARNESS_LLM_BASE_URL"] = explicit_base_url
            return explicit_base_url
        spec = provider_spec(provider)
        os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = spec.get("harness", provider)
        os.environ["UNREAL_HARNESS_LLM_BASE_URL"] = spec.get("base_url", "")
        os.environ.pop("UNREAL_HARNESS_LLM_API_KEY", None)
        key = provider_key(provider)
        if provider == "google-antigravity":
            from antigravity import AntigravityBridge
            bridge = AntigravityBridge(current_session)
            base_url = bridge.start()
            os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = "openai"
            os.environ["UNREAL_HARNESS_LLM_BASE_URL"] = base_url
            os.environ["UNREAL_HARNESS_LLM_API_KEY"] = bridge.key
            os.environ.pop("OPENAI_CODEX_AUTH_FILE", None)
            return f"{base_url} → {spec['base_url']} (experimental)"
        if spec.get("bridge"):
            if not key:
                return f"(no key for {provider})"
            bridge = Bridge(spec["base_url"], key, current_session)
            base_url = bridge.start()
            os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = spec.get("harness", "openai")
            os.environ["UNREAL_HARNESS_LLM_BASE_URL"] = base_url
            os.environ["UNREAL_HARNESS_LLM_API_KEY"] = bridge.local_key
            return f"{base_url} → {spec['base_url']}"
        os.environ["UNREAL_HARNESS_LLM_PROVIDER"] = spec.get("harness", provider)
        os.environ["UNREAL_HARNESS_LLM_BASE_URL"] = spec.get("base_url", "")
        if key:
            os.environ["UNREAL_HARNESS_LLM_API_KEY"] = key
        else:
            os.environ.pop("UNREAL_HARNESS_LLM_API_KEY", None)
        # OAuth providers (codex/ChatGPT) hand the harness an auth file instead of a key.
        if provider == "openai-codex":
            import codex_auth
            os.environ["OPENAI_CODEX_AUTH_FILE"] = os.path.expanduser(codex_auth.CODEX_AUTH_PATH)
            return spec.get("base_url", "")
        if providers_module is not None:
            try:
                for name, value in providers_module.auth_env(provider).items():
                    os.environ[name] = value
            except Exception as error:
                return f"({provider}: {error})"
        return spec.get("base_url", "(provider default)")

    def refresh_auth() -> None:
        if not explicit_base_url and provider_spec(provider).get("needs_key") and not provider_key(provider):
            raise RuntimeError(f"no API key configured for {provider}")
        if not explicit_base_url and providers_module is not None and providers_module.is_oauth(provider):
            os.environ.update(providers_module.auth_env(provider))

    def sync_auth_status(force=False):
        nonlocal auth_status_signature
        if provider != "openai-codex":
            return
        import codex_pool
        try:
            info = os.stat(codex_pool.paths()[1])
            file_signature = (info.st_ino,info.st_mtime_ns,info.st_size)
        except OSError:
            file_signature = None
        signature = (file_signature,session_id,state["model"])
        if not force and signature == auth_status_signature:
            return
        auth_status_signature = signature
        state["quota_usage"] = None
        state["quota_error_model"] = None
        if file_signature is None:
            return
        try:
            cached = codex_pool.cached_status(session_id,state["model"])
            state["quota_usage"] = cached["usage"]
            if cached["limited"]: state["quota_error_model"] = state["model"]
        except (OSError,ValueError):
            pass

    def live_status_line():
        sync_auth_status()
        return status_line(theme,state,metrics,provider,session_id,context_limit)

    def execute_turn(prompt, renderer, spinner):
        with ExitStack() as guard:
            try:
                guard.enter_context(file_lock(os.path.join(SESSION_DIR,session_id+".client"),timeout=0))
            except LockBusyError:
                raise LockBusyError("Эта сессия уже выполняет ход в другом клиенте. Дождись завершения или /new. Запрос не отправлен.") from None
            return execute_turn_unlocked(prompt,renderer,spinner)

    def execute_turn_unlocked(prompt, renderer, spinner):
        if provider != "openai-codex" or explicit_base_url:
            refresh_auth()
            return run_turn(args.binary, workspace, build_request(prompt), renderer, spinner)
        import codex_pool
        def selected(info):
            state["quota_usage"] = info["usage"]
            state["quota_error_model"] = None
            if info["rotated"]:
                print(theme.paint("  Codex: аккаунт переключён на "+safe_text(info["email"]), "ok"))
            if info["usage_error"]:
                print(theme.paint("  Codex usage недоступен ("+info["usage_error"]+"); квота не считается исчерпанной", "warn"))
        print(theme.paint("  Codex: проверяю квоты и готовлю аккаунт…", "dim"))
        with codex_pool.turn(session_id,state["model"],on_select=selected) as (snapshot,info):
            os.environ["OPENAI_CODEX_AUTH_FILE"] = snapshot
            code = run_turn(args.binary,workspace,build_request(prompt),renderer,spinner)
            if code not in (0,130) and codex_pool.note_error(info["key"],renderer.last_error,state["model"]):
                state["quota_error_model"] = state["model"]
                print(theme.paint("  Лимит Codex достигнут. Следующий ход выберет доступный аккаунт; текущий ход НЕ повторяется автоматически.","warn"))
            return code

    def prefetch_catalogue():
        # Metadata can arrive without blocking the editor or changing its model.
        if explicit_base_url:
            return
        spec = provider_spec(provider)
        if spec.get("needs_key") and not provider_key(provider):
            return
        selected = provider
        threading.Thread(target=lambda: available_models(selected, refresh=True), daemon=True).start()

    def build_request(prompt: str) -> dict:
        request: dict = {"prompt": prompt, "session_id": session_id}
        if state["model"]:
            request["model"] = state["model"]
        thinking = state["thinking"]
        if thinking:
            request["thinking_level"] = thinking
        return request

    spinner_on = sys.stdout.isatty()
    try:
        endpoint = apply_environment(session_id)
        model = state["model"] or "(provider default)"
        thinking = state["thinking"]
        model_line = f"{model} · {thinking_glyph(thinking)} {thinking}" if thinking else str(model)

        if args.prompt:
            kick_off_update_check(config)
            renderer = Renderer(theme, model=model, thinking=thinking)
            spinner = Spinner(theme, spinner_on, footer=footer)
            try:
                return execute_turn(args.prompt,renderer,spinner)
            except Exception as error:
                print(theme.paint(str(error), "error"))
                return 1

        sync_auth_status()
        footer.start()
        prefetch_catalogue()
        print(banner(theme, workspace, session_id, model_line, str(endpoint), provider))
        flush_core_note(theme)
        transcript: list[tuple[str, str]] = []
        if os.path.isfile(session_path(session_id)):
            transcript = replay_session(session_id, theme, model=model, thinking=thinking)
        kick_off_update_check(config)
        setup_readline(theme)

        # An incomplete/stale catalogue must not silently replace an explicit model.
        reader = None
        if editor_module is not None and getattr(editor_module, "AVAILABLE", False):
            try:
                reader = editor_module.Editor(
                    history_path=HISTORY_PATH,
                    footer=footer,
                    suggestions=lambda text: command_suggestions(text, theme, provider),
                    paint=lambda text, key: theme.paint(text, key),
                )
            except Exception:
                reader = None
        last_interrupt = 0.0
        next_draft = ""
        while True:
            try:
                if reader is not None:
                    draft, next_draft = next_draft, ""
                    line = reader.read_line(theme.paint("› ", "user", BOLD), initial_text=draft)
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
            if prompt.startswith("/") and prompt.split()[0] not in COMMANDS:
                print(theme.paint("unknown command; /help lists commands", "error"))
                continue
            if not prompt:
                continue
            if prompt in ("/exit", "/quit"):
                break
            if prompt.split()[0] in ("/login", "/logout", "/auth", "/usage", "/accounts", "/rotation", "/doctor"):
                import native_auth
                import shlex
                try:
                    parts = shlex.split(prompt)
                    action = parts.pop(0)[1:]
                    if action == "auth" and parts and parts[0] in ("accounts", "use", "rotation", "usage", "status", "doctor"):
                        native_auth.main(parts,theme=theme)
                        sync_auth_status()
                        continue
                    if action in ("accounts", "rotation"):
                        if action == "accounts" and len(parts)==2 and parts[0] == "use":
                            native_auth.main(["use","--account",parts[1]],theme=theme)
                        else:
                            native_auth.main([action]+parts, theme=theme)
                        sync_auth_status()
                        continue
                    if not parts or parts[0].startswith("--"):
                        if providers_module is not None and providers_module.is_oauth(provider):
                            parts.insert(0, provider)
                        elif action == "doctor":
                            parts.insert(0,"openai-codex")
                        elif action == "auth":
                            print(f"{provider}: {credential_state(provider)}")
                            continue
                        elif action == "logout":
                            print("specify an OAuth provider: /logout openai-codex or google-antigravity")
                            continue
                    if action in ("auth", "usage") and parts and parts[0] == "openai-codex" and not any(flag in parts for flag in ("--all", "--account")):
                        import codex_pool
                        if os.path.exists(codex_pool.paths()[1]):
                            data = codex_pool._read()
                            selected = data["sessions"].get(session_id)
                            if selected in data["accounts"]:
                                parts.extend(["--account",selected])
                    native_auth.main(["status" if action == "auth" else action] + parts, theme=theme)
                    sync_auth_status()
                except (ValueError, SystemExit) as error:
                    if isinstance(error, ValueError):
                        print(theme.paint(str(error), "error"))
                if providers_module is not None:
                    providers_module._AUTH_STATE_CACHE.clear()
                continue
            if prompt == "/status":
                print(live_status_line())
                print(theme.paint("  ctx uses the last request + output, not cumulative token billing; ~ means approximate", "dim"))
                continue
            if prompt == "/context" or prompt.startswith("/context "):
                value = prompt.partition(" ")[2].strip()
                if not value:
                    print("context limit: " + (str(context_limit) if context_limit else "catalogue / unknown"))
                elif value == "auto":
                    context_limit = None
                    save_config_value("UACHAT_CONTEXT_WINDOW", "")
                elif positive_int(value):
                    context_limit = positive_int(value)
                    save_config_value("UACHAT_CONTEXT_WINDOW", str(context_limit))
                else:
                    print(theme.paint("context must be a positive token count or auto", "error"))
                continue
            if prompt == "/help":
                print(HELP)
                continue
            if prompt == "/session":
                print(session_id)
                continue
            if prompt == "/themes":
                print(", ".join(f"*{name}*" if name == theme.name else name for name in theme.names()))
                continue
            if prompt == "/provider" or prompt.startswith("/provider "):
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
                    picked = PROVIDER_DEFAULT_MODELS.get(provider, "")
                    state["model"] = picked
                    os.environ["UNREAL_HARNESS_LLM_MODEL"] = picked
                    print(theme.paint(f"no model catalogue for {provider}; choose /model explicitly", "warn"))
                elif not current:
                    picked = choose_model(provider, ids)
                    if picked:
                        state["model"] = picked
                        os.environ["UNREAL_HARNESS_LLM_MODEL"] = picked
                continue
            if prompt == "/model" or prompt.startswith("/model "):
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
            if prompt == "/thinking" or prompt.startswith("/thinking "):
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
                text = next((text for role, text in reversed(transcript) if role == "agent"), "")
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
            if prompt == "/rtk" or prompt.startswith("/rtk "):
                rtk_shell = "/usr/local/bin/rtk-shell"
                rtk_bin = shutil.which("rtk") or "/usr/local/bin/rtk"
                if not os.path.exists(rtk_bin):
                    print(theme.paint("rtk is not installed", "error"))
                else:
                    active = "active (SHELL wrapped)" if os.path.exists(rtk_shell) else "installed (wrapper inactive)"
                    print(f"rtk: {active} · {rtk_bin}")
                    res = subprocess.run([rtk_bin, "gain"], capture_output=True, text=True)
                    if res.stdout.strip():
                        print("\n".join("  " + l for l in res.stdout.strip().splitlines()))
                    else:
                        res2 = subprocess.run([rtk_bin, "--version"], capture_output=True, text=True)
                        print("  " + (res2.stdout or res2.stderr).strip())
                continue
            if prompt == "/theme" or prompt.startswith("/theme "):
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
                    metrics.load(session_path(session_id))
                    try:
                        endpoint = apply_environment(session_id)
                    except RuntimeError as error:
                        print(theme.paint(f"error: {error}", "error"))
                    clear_screen()
                    model_line = f"{state.get('model', '')} · {thinking_glyph(state.get('thinking', ''))} {state.get('thinking', '')}" if state.get("thinking") else str(state.get("model", ""))
                    print(banner(theme, workspace, session_id, model_line, str(endpoint), provider))
                    transcript = replay_session(session_id, theme, model=state.get("model", ""), thinking=state.get("thinking", ""))
                    continue
                else:
                    for item in items_raw:
                        stamp = time.strftime("%m-%d %H:%M", time.localtime(float(item["mtime"])))
                        marker = "*" if item["name"] == session_id else " "
                        print(f"{marker} {item['name']:<28} {stamp}  {item['preview']}")
                    continue
            if prompt == "/resume" or prompt.startswith("/resume "):
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
                metrics.load(session_path(session_id))
                try:
                    endpoint = apply_environment(session_id)
                except RuntimeError as error:
                    print(theme.paint(f"error: {error}", "error"))
                clear_screen()
                model_line = f"{state.get('model', '')} · {thinking_glyph(state.get('thinking', ''))} {state.get('thinking', '')}" if state.get("thinking") else str(state.get("model", ""))
                print(banner(theme, workspace, session_id, model_line, str(endpoint), provider))
                transcript = replay_session(session_id, theme, model=state.get("model", ""), thinking=state.get("thinking", ""))
                continue
            if prompt == "/new" or prompt.startswith("/new "):
                _, _, name = prompt.partition(" ")
                name = name.strip() or uuid.uuid4().hex[:12]
                if not valid_session_name(name):
                    print(theme.paint("session name cannot contain slashes", "error"))
                    continue
                if os.path.exists(session_path(name)):
                    print(theme.paint("session already exists; use /resume", "error"))
                    continue
                metrics.reset()
                session_id = name
                try:
                    endpoint = apply_environment(session_id)
                except RuntimeError as error:
                    print(theme.paint(f"error: {error}", "error"))
                clear_screen()
                transcript.clear()
                model_line = f"{state.get('model', '')} · {thinking_glyph(state.get('thinking', ''))} {state.get('thinking', '')}" if state.get("thinking") else str(state.get("model", ""))
                print(banner(theme, workspace, session_id, model_line, str(endpoint), provider))
                print(f"new session: {session_id}\n")
                continue
            renderer = Renderer(theme, model=state.get("model", ""), thinking=state.get("thinking", ""), on_usage=metrics.observe)
            spinner = Spinner(theme, spinner_on, footer=footer)
            try:
                code = execute_turn(prompt,renderer,spinner)
            except KeyboardInterrupt:
                print(theme.paint("Подготовка отменена; запрос не отправлен.", "warn"))
                if reader is not None and not renderer.was_started:
                    next_draft = prompt
                last_interrupt = time.monotonic()
                continue
            except Exception as error:
                print(theme.paint(str(error), "error"))
                if reader is not None and not renderer.was_started:
                    next_draft = prompt
                    print(theme.paint("Текст сохранён в черновике; ход не был запущен.", "dim"))
                continue
            next_draft = getattr(renderer, "pending_input", "")
            metrics.elapsed = time.monotonic() - renderer.started_at
            footer.draw()
            transcript.append(("you", prompt))
            if renderer.last_assistant.strip():
                transcript.append(("agent", renderer.last_assistant.strip()))
            del transcript[:-100]
            if code == 0:
                notify("uachat", "turn complete")
            elif code == 130:
                # A second Ctrl-C right after an interrupt leaves the client.
                last_interrupt = time.monotonic()
                notify("uachat", "turn interrupted")
            else:
                err_hint = short(renderer.last_error, 40) if renderer.last_error else f"exit {code}"
                notify("uachat", f"failed: {err_hint}")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(theme.paint("client error: " + safe_text(error), "error"), file=sys.stderr)
        return 1
    finally:
        footer.close()
        if bridge is not None:
            bridge.stop()
        try:
            os.unlink(STREAM_STATE_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, request_shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, request_shutdown)
    try:
        raise SystemExit(main())
    except ShutdownRequested as error:
        raise SystemExit(128 + error.signum)
    except KeyboardInterrupt:
        # Ctrl-C landed outside the prompt loop (e.g. between turns): leave cleanly.
        print("\n  bye")
        raise SystemExit(130)

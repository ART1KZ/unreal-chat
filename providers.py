#!/usr/bin/env python3
"""Provider catalogue for uachat (stdlib only).

Normal operation reads keys from environment/uachat config, OAuth credentials
from uachat's private store. Legacy OMP migration helpers are opt-in only;
no external application or store is required. Keys are never printed.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# models.py ships next to this file and knows how to talk to an OpenAI-style
# catalogue; without it this module still reports specs but no model lists.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import models as models_module
except ImportError:  # pragma: no cover - models.py absent
    models_module = None

PROVIDER_IDS: list[str] = ["opencode-go", "openai-codex", "google-antigravity", "openrouter", "openai", "fireworks", "ollama"]

# openai-codex speaks the ChatGPT backend, which has no public /models route:
# its catalogue is fixed and bundled here.
FALLBACK_CODEX_MODELS: list[str] = sorted(
    (
        "gpt-5.5",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-6-astra",
        "gpt-6-luna",
        "gpt-6-sol",
        "gpt-6.1-sol",
    )
)

# Where auth_env() materialises the harness-ready auth file for oauth providers.
# codex_auth owns the location; this is only a fallback.
def _codex_auth_path() -> str:
    fallback = os.path.expanduser("~/.local/state/uachat/codex-auth.json")
    try:
        import codex_auth  # optional module, imported lazily like _codex_auth()
    except ImportError:
        return fallback
    configured = getattr(codex_auth, "CODEX_AUTH_PATH", "")
    return os.path.expanduser(configured) if configured else fallback

# provider_id -> definition. `harness` is the value for
# UNREAL_HARNESS_LLM_PROVIDER, `bridge` marks providers that need the local
# Responses bridge (custom headers), `env` names the key's environment variable,
# and `auth` is one of "key", "keyless", "oauth".
_DEFINITIONS: dict[str, dict] = {
    "google-antigravity": {
        "label": "Google Antigravity (experimental, standalone)", "harness": "openai",
        "base_url": "https://daily-cloudcode-pa.googleapis.com", "bridge": True,
        "needs_key": False, "env": "", "auth": "oauth",
        "models": ["gemini-3-flash", "gemini-3.6-flash", "gemini-3.5-pro", "claude-sonnet-4-6"],
    },
    "opencode-go": {
        "label": "OpenCode Go (zen gateway)",
        "harness": "openai",
        "base_url": "https://opencode.ai/zen/go/v1",
        "bridge": True,
        "needs_key": True,
        "env": "UACHAT_BRIDGE_KEY",
        "auth": "key",
    },
    "openai-codex": {
        "label": "Codex (ChatGPT)",
        "harness": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "bridge": False,
        "needs_key": False,
        "env": "",
        "auth": "oauth",
        "models": FALLBACK_CODEX_MODELS,
    },
    "openrouter": {
        "label": "OpenRouter",
        "harness": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "bridge": False,
        "needs_key": True,
        "env": "OPENROUTER_API_KEY",
        "auth": "key",
    },
    "openai": {
        "label": "OpenAI",
        "harness": "openai",
        "base_url": "https://api.openai.com/v1",
        "bridge": False,
        "needs_key": True,
        "env": "OPENAI_API_KEY",
        "auth": "key",
    },
    "fireworks": {
        "label": "Fireworks AI",
        "harness": "fireworks",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "bridge": False,
        "needs_key": True,
        "env": "FIREWORKS_API_KEY",
        "auth": "key",
    },
    "ollama": {
        "label": "Ollama (local server)",
        "harness": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "bridge": False,
        "needs_key": False,
        "env": "",
        "auth": "keyless",
    },
}

_FIELDS = ("label", "harness", "base_url", "bridge", "needs_key", "auth")

# Per-process caches: model ids fetched by models(), resolved keys, and the
# credential state computed by auth_state().
_MODEL_CACHE: dict[str, list[str]] = {}
_KEY_CACHE: dict[str, str | None] = {}
_STORE_CACHE: dict[str, str] | None = None
_AUTH_STATE_CACHE: dict[str, str] = {}


def spec(provider_id: str) -> dict:
    """Return {id, label, harness, base_url, bridge, needs_key, auth} for a provider.

    Unknown ids raise ValueError.
    """
    definition = _definition(provider_id)
    return {"id": provider_id, **{field: definition[field] for field in _FIELDS}}


def key_for(provider_id: str) -> str | None:
    """Resolve the provider API key from environment/uachat config only."""
    _definition(provider_id)
    return _env_key(provider_id)


def has_key(provider_id: str) -> bool:
    """Whether an API key is available; the value itself is never exposed."""
    return bool(key_for(provider_id))


def is_oauth(provider_id: str) -> bool:
    """Whether the provider authenticates with an OAuth token instead of an API key."""
    return str(_definition(provider_id).get("auth", "")).lower() == "oauth"


def models(provider_id: str, timeout: float = 10.0) -> list[str]:
    """Model ids from `{base_url}/models`, sorted; [] when the call fails.

    Providers with a bundled catalogue (openai-codex) skip the network entirely.
    The result feeds the process-local cache that describe() reads.
    """
    static = _definition(provider_id).get("models")
    if static is not None:
        _MODEL_CACHE[provider_id] = list(static)
        return list(static)
    base_url = spec(provider_id)["base_url"]
    key = key_for(provider_id)
    identifiers: list[str] = []
    if models_module is not None:
        try:
            identifiers = models_module.list_models(base_url, key, timeout)
        except Exception:  # unreachable host, HTTP error, unusable payload
            identifiers = []
    _MODEL_CACHE[provider_id] = identifiers
    return identifiers


def describe(provider_id: str) -> str:
    """One status line: id, base URL, credential state, cached model count."""
    info = spec(provider_id)
    if info["auth"] == "oauth":
        key_text = auth_state(provider_id)
    elif not info["needs_key"]:
        key_text = "n/a"
    else:
        key_text = "yes" if has_key(provider_id) else "no"
    cached = _MODEL_CACHE.get(provider_id)
    models_text = str(len(cached)) if provider_id in _MODEL_CACHE else "unknown"
    return f"{provider_id}  base_url={info['base_url']}  key={key_text}  models={models_text}"


def auth_env(provider_id: str) -> dict[str, str]:
    """Environment variables the harness needs to authenticate this provider.

    For openai-codex an auth file is written through codex_auth.prepare() and
    its path is returned as OPENAI_CODEX_AUTH_FILE. Every other provider returns
    {} because its credentials travel in UNREAL_HARNESS_LLM_API_KEY instead.
    """
    if provider_id == "google-antigravity":
        import antigravity
        antigravity.credentials()
        return {}
    if not is_oauth(provider_id):
        return {}
    codex_auth = _codex_auth()
    path = _codex_auth_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        prepared = codex_auth.prepare(path)
    except Exception as error:
        raise RuntimeError(f"openai-codex: could not prepare credentials: {error}") from error
    target = path
    if isinstance(prepared, dict):
        candidate = prepared.get("path")
        if isinstance(candidate, str) and candidate.strip():
            target = candidate.strip()
    if not os.path.isfile(target):
        raise RuntimeError(f"openai-codex: credential file {target} was not created")
    _tighten(target)
    return {"OPENAI_CODEX_AUTH_FILE": target}


def auth_state(provider_id: str) -> str:
    """"oauth" | "oauth expired" | "key" | "keyless" | "no key", without network calls.

    The answer is computed at most once per process; expiry comes from the live
    tokens codex_auth.accounts() reports.
    """
    definition = _definition(provider_id)
    if provider_id == "google-antigravity":
        import antigravity
        payload = antigravity.codex_auth._read_json(os.path.expanduser(antigravity.AUTH_PATH))
        state = "oauth" if isinstance(payload, dict) and payload.get("expires_ms", 0) > time.time()*1000 else "oauth expired"
    elif str(definition.get("auth", "")).lower() == "oauth":
        state = "oauth" if _oauth_live() else "oauth expired"
    elif not definition["needs_key"]:
        state = "keyless"
    elif has_key(provider_id):
        state = "key"
    else:
        state = "no key"
    _AUTH_STATE_CACHE[provider_id] = state
    return state


def _definition(provider_id: str) -> dict:
    definition = _DEFINITIONS.get(provider_id)
    if definition is None:
        raise ValueError(f"unknown provider {provider_id!r} (known: {', '.join(PROVIDER_IDS)})")
    return definition


def _env_key(provider_id: str) -> str | None:
    """Key from the process environment, falling back to the uachat env file."""
    name = _definition(provider_id)["env"]
    if not name:
        return None
    value = (os.environ.get(name) or "").strip()
    if not value and models_module is not None:
        value = (models_module.load_env().get(name) or "").strip()
    return value or None


def _codex_auth():
    """Import codex_auth.py (next to this file); a clear error when it is absent."""
    try:
        import codex_auth
    except ImportError as error:
        raise RuntimeError(
            "openai-codex needs codex_auth.py next to providers.py to read ChatGPT credentials "
            f"({error})"
        ) from error
    return codex_auth


def _tighten(path: str) -> None:
    """Force owner-only permissions on a credential file; failure is not fatal here."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _oauth_live() -> bool:
    """Whether codex_auth reports at least one unexpired ChatGPT token."""
    try:
        accounts = _codex_auth().accounts()
    except Exception:  # module absent, store unreadable, unexpected payload
        return False
    now = time.time()
    for account in _iter_accounts(accounts):
        expires = _account_expiry(account)
        if expires is not None and expires > now:
            return True
    return False


def _iter_accounts(accounts: object) -> list:
    """Normalise codex_auth.accounts() into a list of account mappings."""
    if isinstance(accounts, dict):
        nested = accounts.get("accounts")
        if isinstance(nested, (list, tuple)):
            return list(nested)
        return [accounts]
    if isinstance(accounts, (list, tuple)):
        return list(accounts)
    return []


def _account_expiry(account: object) -> float | None:
    """Expiry of one account as a POSIX timestamp; None when it cannot be read."""
    if not isinstance(account, dict):
        return None
    for field in ("expires_ms", "expires", "expires_at", "expiresAt", "expiry", "exp"):
        if field not in account:
            continue
        value = account[field]
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            seconds = float(value)
            return seconds / 1000.0 if seconds > 1e11 else seconds
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.replace(".", "", 1).isdigit():
                return _account_expiry({field: float(text)})
            try:
                stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            return stamp.timestamp()
    return None


def _omp_db() -> str | None:
    """Path to the omp credential store, or None when it is not installed."""
    override = os.environ.get("UACHAT_OMP_DB")
    if override:
        return override if os.path.exists(override) else None
    native = os.path.expanduser("~/.omp/agent/agent.db")
    if os.path.exists(native):
        return native
    for candidate in sorted(glob.glob("/mnt/c/Users/*/.omp/agent/agent.db")):
        return candidate
    return None


def _store_keys() -> dict[str, str]:
    """api_key credentials from the omp store, keyed by provider id."""
    global _STORE_CACHE
    if _STORE_CACHE is None:
        source = _omp_db()
        if not source:
            return {}
        _STORE_CACHE = _read_store(source)
    return _STORE_CACHE


def _read_store(source: str) -> dict[str, str]:
    """Copy the store next to its WAL and read the newest key per provider."""
    try:
        with tempfile.TemporaryDirectory() as work:
            target = os.path.join(work, "agent.db")
            for suffix in ("", "-wal", "-shm"):
                try:
                    shutil.copyfile(source + suffix, target + suffix)
                except OSError:
                    continue
            connection = sqlite3.connect(target)
            try:
                rows = connection.execute(
                    "select provider, data from auth_credentials "
                    "where credential_type = ? order by updated_at desc",
                    ("api_key",),
                ).fetchall()
            finally:
                connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return {}
    keys: dict[str, str] = {}
    for provider, data in rows:
        key = _credential_key(data)
        if key and provider not in keys:
            keys[provider] = key
    return keys


def _credential_key(data: object) -> str | None:
    """Pull the secret out of a credential row's JSON payload."""
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for field in ("key", "api_key", "token"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _missing_hint(provider_id: str) -> str:
    """Why the model list is empty, in one line."""
    info = spec(provider_id)
    if info["auth"] == "oauth":
        return f"no models: no live ChatGPT credentials (state: {auth_state(provider_id)})"
    if not info["needs_key"]:
        return f"no models: is the local server running at {info['base_url'].removesuffix('/v1')}?"
    if not has_key(provider_id):
        return f"no models: no API key (set {_definition(provider_id)['env']} in the environment or uachat config)"
    return f"no models: {info['base_url']}/models did not answer"


def main() -> int:
    for provider_id in PROVIDER_IDS:
        identifiers = models(provider_id)
        print(describe(provider_id))
        if not identifiers:
            print(f"    {_missing_hint(provider_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

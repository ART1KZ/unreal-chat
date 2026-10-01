#!/usr/bin/env python3
"""Provider catalogue for the uachat client.

Describes every LLM provider uachat can target: its harness name, base URL,
whether the local Responses bridge must front it, and how to find its API key.
Keys come from the environment first, then from the omp credential store
(`auth_credentials`), which is read through a temporary copy: WAL-mode SQLite
under /mnt/c is not reliably readable from WSL in place.

Standalone (stdlib only): `python3 providers.py` prints one status line per
provider and fetches `{base_url}/models` to fill the report. Keys are never
printed — only whether one was found.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile

# models.py ships next to this file and knows how to talk to an OpenAI-style
# catalogue; without it this module still reports specs but no model lists.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import models as models_module
except ImportError:  # pragma: no cover - models.py absent
    models_module = None

PROVIDER_IDS: list[str] = ["opencode-go", "openrouter", "openai", "fireworks", "ollama"]

# provider_id -> definition. `harness` is the value for
# UNREAL_HARNESS_LLM_PROVIDER, `bridge` marks providers that need the local
# Responses bridge (custom headers), `env` names the key's environment variable.
_DEFINITIONS: dict[str, dict] = {
    "opencode-go": {
        "label": "OpenCode Go (zen gateway)",
        "harness": "openai",
        "base_url": "https://opencode.ai/zen/go/v1",
        "bridge": True,
        "needs_key": True,
        "env": "UACHAT_BRIDGE_KEY",
    },
    "openrouter": {
        "label": "OpenRouter",
        "harness": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "bridge": False,
        "needs_key": True,
        "env": "OPENROUTER_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "harness": "openai",
        "base_url": "https://api.openai.com/v1",
        "bridge": False,
        "needs_key": True,
        "env": "OPENAI_API_KEY",
    },
    "fireworks": {
        "label": "Fireworks AI",
        "harness": "fireworks",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "bridge": False,
        "needs_key": True,
        "env": "FIREWORKS_API_KEY",
    },
    "ollama": {
        "label": "Ollama (local server)",
        "harness": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "bridge": False,
        "needs_key": False,
        "env": "",
    },
}

_FIELDS = ("label", "harness", "base_url", "bridge", "needs_key")

# Per-process caches: model ids fetched by models(), and resolved keys.
_MODEL_CACHE: dict[str, list[str]] = {}
_KEY_CACHE: dict[str, str | None] = {}
_STORE_CACHE: dict[str, str] | None = None


def spec(provider_id: str) -> dict:
    """Return {id, label, harness, base_url, bridge, needs_key} for a provider.

    Unknown ids raise ValueError.
    """
    definition = _definition(provider_id)
    return {"id": provider_id, **{field: definition[field] for field in _FIELDS}}


def key_for(provider_id: str) -> str | None:
    """Resolve the provider's API key: environment first, then the omp store."""
    _definition(provider_id)
    if provider_id not in _KEY_CACHE:
        _KEY_CACHE[provider_id] = _env_key(provider_id) or _store_keys().get(provider_id)
    return _KEY_CACHE[provider_id]


def has_key(provider_id: str) -> bool:
    """Whether an API key is available; the value itself is never exposed."""
    return bool(key_for(provider_id))


def models(provider_id: str, timeout: float = 10.0) -> list[str]:
    """Model ids from `{base_url}/models`, sorted; [] when the call fails.

    The result feeds the process-local cache that describe() reads.
    """
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
    """One status line: id, base URL, key present or not, cached model count."""
    info = spec(provider_id)
    if not info["needs_key"]:
        key_text = "n/a"
    else:
        key_text = "yes" if has_key(provider_id) else "no"
    cached = _MODEL_CACHE.get(provider_id)
    models_text = str(len(cached)) if provider_id in _MODEL_CACHE else "unknown"
    return f"{provider_id}  base_url={info['base_url']}  key={key_text}  models={models_text}"


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
    if not info["needs_key"]:
        return f"no models: is the local server running at {info['base_url'].removesuffix('/v1')}?"
    if not has_key(provider_id):
        return f"no models: no API key (set {_definition(provider_id)['env']} or add one to the omp store)"
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

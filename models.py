#!/usr/bin/env python3
"""Model catalogue and reasoning-effort helpers for the uachat client.

Standalone (stdlib only): `python3 models.py` prints where the catalogue comes
from and the model ids the gateway reports, falling back to a bundled list when
it is unreachable. The API key is never printed.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/uachat/env")
USER_AGENT = "uarchat/0.1"

EFFORT_OPTIONS = ("low", "medium", "high", "xhigh", "max")

# Catalogue of the OpenCode Go gateway; ids without a provider prefix.
FALLBACK_MODELS: list[str] = sorted(
    (
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-v4-pro",
        "deepseek-v4.1-flash",
        "glm-5",
        "glm-5.1",
        "glm-5.2",
        "glm-5.3",
        "glm-5.3-flash",
        "gpt-5.6-luna",
        "gpt-6-luna",
        "kimi-k2.7-code",
        "kimi-k3",
        "minimax-m2.7",
        "minimax-m3",
        "qwen3.7-plus",
        "qwen3.8-flash",
        "qwen3.8-max",
        "grok-4.5",
        "grok-4.6",
        "grok-4.7",
        "hy3",
        "muse-spark-1.3",
        "mimo-v2.6-flash",
        "mimo-v2.6-pro",
    )
)


class ModelListError(RuntimeError):
    """The model catalogue could not be fetched from the gateway."""


def effort_options() -> list[str]:
    """Reasoning-effort levels accepted by the harness, cheapest first."""
    return list(EFFORT_OPTIONS)


def list_models(base_url: str, api_key: str | None = None, timeout: float = 10.0) -> list[str]:
    """Fetch model ids from `{base_url}/models`, sorted; any failure raises."""
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise ModelListError(f"{url}: HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except ModelListError:
        raise
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise ModelListError(f"{url}: {error}") from error
    identifiers = _extract_ids(payload)
    if not identifiers:
        raise ModelListError(f"{url}: response carries no model ids")
    return sorted(identifiers)


def load_env(path: str | None = None) -> dict[str, str]:
    """Read KEY=VALUE pairs from the uachat env file; comments and blanks are skipped."""
    values: dict[str, str] = {}
    try:
        with open(path or CONFIG_PATH, encoding="utf-8") as handle:
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def models_source(env: dict[str, str]) -> tuple[str, str | None]:
    """Return (base_url, api_key) for the model catalogue; the bridge wins when set."""
    target = (env.get("UACHAT_BRIDGE_TARGET") or "").strip()
    if target:
        key = (env.get("UACHAT_BRIDGE_KEY") or "").strip()
        return target.rstrip("/"), key or None
    base = (env.get("UNREAL_HARNESS_LLM_BASE_URL") or "").strip()
    key = (env.get("UNREAL_HARNESS_LLM_API_KEY") or "").strip()
    return base.rstrip("/"), key or None


def _extract_ids(payload: object) -> list[str]:
    """Pull ids out of an OpenAI-style catalogue or a bare list."""
    if isinstance(payload, list):
        entries: object = payload
    elif isinstance(payload, dict):
        entries = payload.get("data") or payload.get("models") or []
    else:
        return []
    if not isinstance(entries, list):
        return []
    identifiers: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            ident = entry
        elif isinstance(entry, dict):
            ident = entry.get("id") or entry.get("name")
        else:
            ident = None
        if isinstance(ident, str) and ident.strip():
            identifiers.append(ident.strip())
    return identifiers


def main(argv: list[str] | None = None) -> int:
    env = load_env()
    base_url, api_key = models_source(env)
    if not base_url:
        print("warning: no UACHAT_BRIDGE_TARGET or UNREAL_HARNESS_LLM_BASE_URL configured", file=sys.stderr)
        print("source: fallback catalogue")
        for ident in FALLBACK_MODELS:
            print(ident)
        return 1
    print(f"source: {base_url}/models ({'authenticated' if api_key else 'anonymous'})")
    try:
        identifiers = list_models(base_url, api_key)
    except ModelListError as error:
        print(f"warning: {error}", file=sys.stderr)
        print("source: fallback catalogue")
        for ident in FALLBACK_MODELS:
            print(ident)
        return 1
    for ident in identifiers:
        print(ident)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

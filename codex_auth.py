#!/usr/bin/env python3
"""Native uachat ChatGPT/Codex credentials and optional migration helpers.

Login and refresh live in native_auth.py. Normal discovery reads only uachat's
own private auth.json. External OMP/Codex stores are read only on explicit
`uachat login --import-existing`; neither application is required.
Stdlib only. No token values are printed.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# Where the harness will be told to look for our credentials.
CODEX_AUTH_PATH = "~/.config/uachat/codex/auth.json"

OMP_PROVIDER = "openai-codex"
OMP_CREDENTIAL_TYPE = "oauth"

_HARNESS_AUTH_MODE = "chatgpt"
_JWT_AUTH_CLAIM = "https://api.openai.com/auth"
_JWT_PROFILE_CLAIM = "https://api.openai.com/profile"

SOURCE_OMP = "omp"
SOURCE_CODEX = "codex"

EXPIRED_HINT = (
    "все сохранённые токены Codex истекли; "
    "выполните uachat login openai-codex"
)

_TABLE_HEADERS = ("source", "email", "expires_h", "valid", "token_len")
_TABLE_ALIGN = ("<", "<", ">", "<", ">")

# Last native snapshot; accounts() rereads so other clients can update auth.
_ACCOUNTS: list[dict] | None = None


def credential_identity(payload):
    """Account and known user identity, never raw token material."""
    tokens = payload.get("tokens", {})
    if not isinstance(tokens, dict): return None, None
    access = _jwt_claims(tokens.get("access_token"))
    identity = _jwt_claims(tokens.get("id_token"))
    auth = access.get(_JWT_AUTH_CLAIM, {})
    if not isinstance(auth, dict): auth = {}
    user = auth.get("chatgpt_user_id") or auth.get("user_id") or access.get("sub") or identity.get("sub")
    return tokens.get("account_id"), user if isinstance(user,str) and user else None


def accounts() -> list[dict]:
    """The native uachat Codex account, if logged in.

    Each entry is {source, email, account_id, expires_ms, access, refresh,
    valid}; `valid` marks an unexpired access token. Entries with an unreadable
    or absent expiry sort last and are never valid.
    """
    global _ACCOUNTS
    payload = _read_json(os.path.expanduser(CODEX_AUTH_PATH))
    found = []
    if isinstance(payload, dict) and isinstance(payload.get("tokens"), dict):
        tokens = payload["tokens"]
        access = _text(tokens.get("access_token"))
        claims = _jwt_claims(access)
        found.append(_account("uachat", payload.get("email") or _claim_email(claims),
            tokens.get("account_id"), _epoch_ms(payload.get("expires_ms")) or _expires_from_token(access),
            access, tokens.get("refresh_token")))
    found.sort(key=lambda item: item["expires_ms"] or 0, reverse=True)
    _ACCOUNTS = found
    return [dict(item) for item in _ACCOUNTS]


def pick(email: str | None = None) -> dict:
    """The newest valid account, optionally restricted to one email address.

    Raises ValueError when every candidate has expired, or when the requested
    email is unknown.
    """
    candidates = accounts()
    if email is not None:
        wanted = email.strip().lower()
        candidates = [item for item in candidates if (item["email"] or "").lower() == wanted]
        if not candidates:
            known = ", ".join(sorted({item["email"] for item in accounts() if item["email"]}))
            raise ValueError(
                f"unknown account email {email!r}; known accounts: {known or 'none'}"
            )
    live = [item for item in candidates if item["valid"]]
    if not live:
        raise ValueError(EXPIRED_HINT)
    return live[0]


def prepare(path: str = CODEX_AUTH_PATH, email: str | None = None) -> dict:
    """Write the harness auth file for the newest valid account (0600 regular file).

    Returns {path, email, expires_in_hours, source}; token values are never
    returned or printed.
    """
    from native_auth import ensure_fresh
    target = os.path.expanduser(path)
    payload = ensure_fresh(target)
    global _ACCOUNTS
    _ACCOUNTS = None
    return {"path": target, "email": payload.get("email"), "source": "uachat",
            "expires_in_hours": _hours_left(payload.get("expires_ms"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex_auth.py",
        description="List ChatGPT/Codex accounts and write the uachat harness auth file.",
    )
    parser.add_argument(
        "--use",
        metavar="EMAIL",
        help="write the auth file for this account email (default: newest valid account)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the accounts only; do not write the auth file",
    )
    args = parser.parse_args(argv)

    found = accounts()
    _print_table(found)
    if args.check:
        if any(item["valid"] for item in found):
            return 0
        print(f"error: {EXPIRED_HINT}", file=sys.stderr)
        return 1

    try:
        written = prepare(email=args.use)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    hours = written["expires_in_hours"]
    hours_text = f"{hours:.1f} h" if hours is not None else "unknown"
    print(
        f"wrote {written['path']} (mode 0600) for {written['email'] or '-'} "
        f"from {written['source']}; expires in {hours_text}"
    )
    return 0


# --- omp credential store -------------------------------------------------


def _omp_accounts() -> list[dict]:
    """Codex OAuth rows of the omp store, one account per row."""
    source = _omp_db_path()
    if not source:
        return []
    found: list[dict] = []
    for data in _read_omp_rows(source):
        payload = _json_object(data)
        if payload is None:
            continue
        access = _text(payload.get("access"))
        refresh = _text(payload.get("refresh"))
        expires_ms = _epoch_ms(payload.get("expires")) or _expires_from_token(access)
        account_id = _text(payload.get("accountId")) or _claim_account_id(_jwt_claims(access))
        found.append(
            _account(SOURCE_OMP, _text(payload.get("email")), account_id, expires_ms, access, refresh)
        )
    return found


def _omp_db_path() -> str | None:
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


def _read_omp_rows(source: str) -> list[object]:
    """Credential payloads for Codex OAuth accounts, newest row first."""
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
                    "select data from auth_credentials "
                    "where provider = ? and credential_type = ? "
                    "and (disabled_cause is null or disabled_cause = '') "
                    "order by updated_at desc",
                    (OMP_PROVIDER, OMP_CREDENTIAL_TYPE),
                ).fetchall()
            finally:
                connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return []
    return [row[0] for row in rows]


# --- Codex CLI auth file --------------------------------------------------


def _codex_accounts() -> list[dict]:
    """Accounts found in Codex CLI's auth.json files, one per file."""
    found: list[dict] = []
    for path in _codex_files():
        payload = _read_json(path)
        if not isinstance(payload, dict) or payload.get("auth_mode") != _HARNESS_AUTH_MODE:
            continue
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            continue
        access = _text(tokens.get("access_token"))
        if not access:
            continue
        claims = _jwt_claims(access)
        id_claims = _jwt_claims(_text(tokens.get("id_token")))
        found.append(
            _account(
                SOURCE_CODEX,
                _claim_email(claims) or _claim_email(id_claims),
                _text(tokens.get("account_id"))
                or _claim_account_id(claims)
                or _claim_account_id(id_claims),
                _expires_from_token(access),
                access,
                _text(tokens.get("refresh_token")),
            )
        )
    return found


def _codex_files() -> list[str]:
    """Existing Codex auth.json paths, $CODEX_HOME first, without duplicates.

    Codex CLI often runs on Windows, so a WSL home is backed by the same glob
    the omp store lookup uses for `/mnt/c/Users/*`.
    """
    candidates: list[str] = []
    home = (os.environ.get("CODEX_HOME") or "").strip()
    if home:
        candidates.append(os.path.join(os.path.expanduser(home), "auth.json"))
    candidates.append(os.path.expanduser("~/.codex/auth.json"))
    candidates.extend(sorted(glob.glob("/mnt/c/Users/*/.codex/auth.json")))
    paths: list[str] = []
    for candidate in candidates:
        if candidate not in paths and os.path.isfile(candidate):
            paths.append(candidate)
    return paths


# --- shared helpers -------------------------------------------------------


def _account(
    source: str,
    email: str | None,
    account_id: str | None,
    expires_ms: int | None,
    access: str | None,
    refresh: str | None,
) -> dict:
    return {
        "source": source,
        "email": email,
        "account_id": account_id,
        "expires_ms": expires_ms,
        "access": access,
        "refresh": refresh,
        "valid": bool(expires_ms and access and account_id) and expires_ms > _now_ms(),
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hours_left(expires_ms: int | None) -> float | None:
    if not expires_ms:
        return None
    return (expires_ms - _now_ms()) / 3_600_000


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _epoch_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _json_object(raw: object) -> dict | None:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_json(path: str) -> object:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _jwt_claims(token: object) -> dict:
    """Decode a JWT payload without verifying the signature; {} when unusable."""
    if not isinstance(token, str):
        return {}
    parts = token.strip().split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _expires_from_token(token: object) -> int | None:
    """Epoch-ms expiry from the JWT `exp` claim (seconds), or None."""
    exp = _jwt_claims(token).get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return int(exp * 1000)


def _claim_account_id(claims: dict) -> str | None:
    claim = claims.get(_JWT_AUTH_CLAIM)
    if isinstance(claim, dict):
        return _text(claim.get("chatgpt_account_id"))
    return None


def _claim_email(claims: dict) -> str | None:
    profile = claims.get(_JWT_PROFILE_CLAIM)
    if isinstance(profile, dict):
        email = _text(profile.get("email"))
        if email:
            return email
    return _text(claims.get("email"))


def _write_private_json(path: str, payload: dict) -> None:
    """Atomically write a 0600 regular file holding `payload`."""
    parent = os.path.dirname(path) or "."
    created = not os.path.isdir(parent)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if created:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    handle, temporary = tempfile.mkstemp(dir=parent, prefix=".auth-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _print_table(found: list[dict]) -> None:
    if not found:
        print("not logged in; run uachat login openai-codex")
        return
    rows: list[tuple[str, ...]] = []
    for account in found:
        hours = _hours_left(account["expires_ms"])
        rows.append(
            (
                account["source"],
                account["email"] or "-",
                f"{hours:.1f}" if hours is not None else "-",
                "yes" if account["valid"] else "no",
                str(len(account["access"] or "")),
            )
        )
    widths = [len(header) for header in _TABLE_HEADERS]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print(_table_line(_TABLE_HEADERS, widths))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(_table_line(row, widths))


def _table_line(cells: tuple[str, ...], widths: list[int]) -> str:
    return "  ".join(
        cell.rjust(widths[index]) if _TABLE_ALIGN[index] == ">" else cell.ljust(widths[index])
        for index, cell in enumerate(cells)
    )


if __name__ == "__main__":
    raise SystemExit(main())

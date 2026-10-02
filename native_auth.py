"""Standalone Codex OAuth (stdlib only); no OMP/Codex CLI dependency.

Protocol references: openai/codex login server and device_code_auth implementations.
This implementation uses the public Codex OAuth client, PKCE and device login.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager

import codex_auth

ISSUER = 'https://auth.openai.com'
CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
REDIRECT = 'http://localhost:1455/auth/callback'


def _post(path: str, data: dict, *, form: bool = False) -> dict:
    body = (urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode())
    request = urllib.request.Request(ISSUER + path, data=body, headers={
        'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError('invalid OAuth response')
    return result


def _exchange(code: str, verifier: str, redirect: str) -> dict:
    return _post('/oauth/token', {'grant_type': 'authorization_code',
        'client_id': CLIENT_ID, 'code': code, 'code_verifier': verifier,
        'redirect_uri': redirect}, form=True)


@contextmanager
def _lock(path: str):
    import fcntl
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(path + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save(response: dict, path: str, previous: dict | None = None) -> None:
    tokens = dict(previous or {})
    for name in ('access_token', 'refresh_token', 'id_token'):
        if response.get(name):
            tokens[name] = response[name]
    claims = codex_auth._jwt_claims(tokens.get('access_token'))
    id_claims = codex_auth._jwt_claims(tokens.get('id_token'))
    tokens['account_id'] = (codex_auth._claim_account_id(claims)
                            or codex_auth._claim_account_id(id_claims)
                            or tokens.get('account_id'))
    if not tokens.get('access_token') or not tokens.get('account_id'):
        raise ValueError('OAuth response lacks access token or ChatGPT account id')
    expiry = codex_auth._expires_from_token(tokens['access_token'])
    if expiry is None and response.get('expires_in'):
        expiry = int((time.time() + float(response['expires_in'])) * 1000)
    codex_auth._write_private_json(path, {'auth_mode': 'chatgpt', 'tokens': tokens,
        'expires_ms': expiry, 'email': codex_auth._claim_email(claims)
        or codex_auth._claim_email(id_claims)})
    codex_auth._ACCOUNTS = None


def ensure_fresh(path: str) -> dict:
    """Refresh under a process lock; never discard usable rotating credentials."""
    with _lock(path):
        payload = codex_auth._read_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get('tokens'), dict):
            raise ValueError('not logged in; run uachat login openai-codex')
        tokens = payload['tokens']
        expiry = payload.get('expires_ms') or codex_auth._expires_from_token(tokens.get('access_token'))
        if not expiry or expiry <= (time.time() + 120) * 1000:
            if not tokens.get('refresh_token'):
                raise ValueError('token expired; run uachat login openai-codex')
            response = _post('/oauth/token', {'grant_type': 'refresh_token',
                'client_id': CLIENT_ID, 'refresh_token': tokens['refresh_token']}, form=True)
            _save(response, path, tokens)
            payload = codex_auth._read_json(path)
        return payload


def browser_login() -> dict:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # callback URL contains a secret code

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(url.query)
            valid = (url.path == '/auth/callback'
                     and secrets.compare_digest(values.get('state', [''])[0], state))
            if not valid:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid OAuth callback.')
                return
            if values.get('error'):
                result['error'] = 'OAuth authorization was denied'
            elif values.get('code'):
                result['code'] = values['code'][0]
            else:
                result['error'] = 'OAuth callback has no authorization code'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'Authorization received. Return to uachat.')

    with http.server.HTTPServer(('127.0.0.1', 1455), Callback) as server:
        server.timeout = 1
        url = ISSUER + '/oauth/authorize?' + urllib.parse.urlencode({
            'response_type': 'code', 'client_id': CLIENT_ID, 'redirect_uri': REDIRECT,
            'scope': 'openid profile email offline_access', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'id_token_add_organizations': 'true', 'codex_cli_simplified_flow': 'true',
            'originator': 'codex_cli_rs'})
        print('Open this URL in your browser (Ctrl-C cancels):\n' + url, flush=True)
        webbrowser.open(url)
        deadline = time.monotonic() + 900
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise ValueError('OAuth login timed out')
    if result.get('error'):
        raise ValueError(result['error'])
    return _exchange(result['code'], verifier, REDIRECT)


def device_login() -> dict:
    device = _post('/api/accounts/deviceauth/usercode', {'client_id': CLIENT_ID})
    code = device.get('user_code') or device.get('usercode')
    if not code or not device.get('device_auth_id'):
        raise ValueError('invalid device authorization response')
    print(f'Open {ISSUER}/codex/device\nEnter code: {code}\n'
          'Only continue if you initiated this login. Ctrl-C cancels.', flush=True)
    interval = max(1, min(60, int(device.get('interval') or 5)))
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            result = _post('/api/accounts/deviceauth/token', {
                'device_auth_id': device['device_auth_id'], 'user_code': code})
            return _exchange(result['authorization_code'], result['code_verifier'],
                             ISSUER + '/deviceauth/callback')
        except urllib.error.HTTPError as error:
            if error.code not in (403, 404):
                raise
            error.close()
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise ValueError('device login timed out after 15 minutes')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='uachat auth')
    parser.add_argument('action', choices=['login', 'logout', 'status'])
    parser.add_argument('provider', nargs='?', default='openai-codex', choices=['openai-codex', 'google-antigravity'])
    parser.add_argument('--headless', action='store_true', help='device-code login (Codex) or manual callback (Google)')
    parser.add_argument('--import-existing', action='store_true', help='explicitly import from Codex CLI or OMP')
    args = parser.parse_args(argv)
    path = os.path.expanduser(codex_auth.CODEX_AUTH_PATH)
    try:
        if args.provider == 'google-antigravity':
            import antigravity
            if args.import_existing:
                raise ValueError('external import is only supported for Codex')
            antigravity.auth(args.action, args.headless)
            return 0
        if args.action == 'login':
            if args.import_existing:
                found = codex_auth._omp_accounts() + codex_auth._codex_accounts()
                live = sorted((a for a in found if a['valid']), key=lambda a: a['expires_ms'], reverse=True)
                if not live:
                    raise ValueError('no usable credentials to import')
                a = live[0]
                response = {'access_token': a['access'], 'refresh_token': a['refresh']}
                with _lock(path):
                    _save(response, path, {'account_id': a['account_id']})
            else:
                response = device_login() if args.headless else browser_login()
                with _lock(path):
                    _save(response, path)
            print('Logged in to openai-codex.')
        elif args.action == 'logout':
            with _lock(path):
                if os.path.exists(path):
                    os.unlink(path)
            codex_auth._ACCOUNTS = None
            print('Logged out of uachat; external credentials are untouched.')
        else:
            payload = codex_auth._read_json(path)
            if not isinstance(payload, dict):
                print('openai-codex: not logged in')
            else:
                expiry = payload.get('expires_ms') or codex_auth._expires_from_token(payload.get('tokens', {}).get('access_token'))
                state = 'valid' if expiry and expiry > time.time() * 1000 else 'expired (refresh on next turn)'
                print(f"openai-codex: {state} · {payload.get('email') or 'account'}")
        return 0
    except KeyboardInterrupt:
        print('\nAuthorization cancelled.')
        return 130
    except urllib.error.HTTPError as error:
        print(f'OAuth HTTP {error.code}; try login again (device login may need enabling in ChatGPT settings).')
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f'Authorization failed: {error}')
        return 1

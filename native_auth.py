"""Standalone Codex OAuth (stdlib only); no OMP/Codex CLI dependency.

Protocol references: openai/codex login server and device_code_auth implementations.
This implementation uses the public Codex OAuth client, PKCE and device login.
"""
from __future__ import annotations

import argparse
import sys
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
from secure_http import urlopen as secure_urlopen
from auth_ui import AuthUI, open_browser
from contextlib import contextmanager

import codex_auth
from terminal_ui import positive_int, safe_text

ISSUER = 'https://auth.openai.com'
CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
REDIRECT = 'http://localhost:1455/auth/callback'


def _post(path: str, data: dict, *, form: bool = False) -> dict:
    body = (urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode())
    request = urllib.request.Request(ISSUER + path, data=body, headers={
        'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
        'Accept': 'application/json',
    })
    with secure_urlopen(request, timeout=30) as response:
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
    from configio import file_lock
    with file_lock(path):
        yield


def valid_account_id(value):
    return isinstance(value,str) and bool(value) and all(33 <= ord(c) <= 126 for c in value)


def build_payload(response: dict, previous: dict | None = None) -> dict:
    access = response.get('access_token')
    if not isinstance(access, str) or not access or access.startswith('sk-'):
        raise ValueError('OAuth response has no usable subscription access token')
    if previous is None and (not isinstance(response.get("refresh_token"), str) or not response["refresh_token"]):
        raise ValueError("OAuth response lacks a refresh token")
    tokens = dict(previous or {})
    for name in ('access_token', 'refresh_token', 'id_token'):
        if response.get(name) is not None and not isinstance(response.get(name),str):
            raise ValueError('OAuth response has an invalid token field')
        if response.get(name):
            tokens[name] = response[name]
    claims = codex_auth._jwt_claims(tokens.get('access_token'))
    id_claims = codex_auth._jwt_claims(tokens.get('id_token'))
    tokens['account_id'] = (codex_auth._claim_account_id(claims)
                            or codex_auth._claim_account_id(id_claims)
                            or tokens.get('account_id'))
    if not valid_account_id(tokens.get('account_id')):
        raise ValueError('OAuth response lacks access token or ChatGPT account id')
    expiry = codex_auth._expires_from_token(tokens['access_token'])
    if expiry is None and positive_int(response.get('expires_in')):
        expiry = int((time.time() + positive_int(response['expires_in'])) * 1000)
    if not expiry or expiry <= time.time()*1000:
        raise ValueError('OAuth response contains an expired token or no expiry')
    payload = {'auth_mode': 'chatgpt', 'tokens': tokens, 'expires_ms': expiry,
               'email': codex_auth._claim_email(claims) or codex_auth._claim_email(id_claims)}
    if previous:
        old_account, old_user = codex_auth.credential_identity({'tokens':previous})
        new_account, new_user = codex_auth.credential_identity(payload)
        if old_account and old_account != new_account or old_user and new_user and old_user != new_user:
            raise ValueError('refresh returned credentials for a different account; existing credentials unchanged')
    return payload


def _save(response: dict, path: str, previous: dict | None = None) -> None:
    codex_auth._write_private_json(path, build_payload(response, previous))
    codex_auth._ACCOUNTS = None


def ensure_fresh(path: str, force: bool = False) -> dict:
    """Refresh under a process lock; never discard usable rotating credentials."""
    with _lock(path):
        payload = codex_auth._read_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get('tokens'), dict):
            raise ValueError('not logged in; run uachat login openai-codex')
        tokens = payload['tokens']
        if not isinstance(tokens.get('access_token'), str) or not tokens['access_token'] or tokens['access_token'].startswith('sk-') or not valid_account_id(tokens.get('account_id')):
            raise ValueError('invalid auth file; run uachat login openai-codex')
        expiry = positive_int(payload.get('expires_ms')) or codex_auth._expires_from_token(tokens.get('access_token'))
        if force or not expiry or expiry <= (time.time() + 120) * 1000:
            if not tokens.get('refresh_token'):
                raise ValueError('token expired; run uachat login openai-codex')
            response = _post('/oauth/token', {'grant_type': 'refresh_token',
                'client_id': CLIENT_ID, 'refresh_token': tokens['refresh_token']}, form=True)
            _save(response, path, tokens)
            payload = codex_auth._read_json(path)
        return payload


def browser_login(ui=None, *, port=1455) -> dict:
    ui = ui or AuthUI()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

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

    with http.server.ThreadingHTTPServer(('127.0.0.1', port), Callback) as server:
        redirect = f'http://localhost:{server.server_port}/auth/callback'
        server.timeout = 1
        url = ISSUER + '/oauth/authorize?' + urllib.parse.urlencode({
            'response_type': 'code', 'client_id': CLIENT_ID, 'redirect_uri': redirect,
            'scope': 'openid profile email offline_access', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'id_token_add_organizations': 'true', 'codex_cli_simplified_flow': 'true',
            'originator': 'codex_cli_rs'})
        ui.browser(url, 'Codex / ChatGPT')
        deadline = time.monotonic() + 900
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise ValueError('OAuth login timed out')
    if result.get('error'):
        raise ValueError(result['error'])
    return _exchange(result['code'], verifier, redirect)


def device_login(ui=None) -> dict:
    ui = ui or AuthUI()
    device = _post('/api/accounts/deviceauth/usercode', {'client_id': CLIENT_ID})
    code = device.get('user_code') or device.get('usercode')
    if not code or not device.get('device_auth_id'):
        raise ValueError('invalid device authorization response')
    ui.device(ISSUER+'/codex/device', code)
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


def main(argv=None, theme=None) -> int:
    parser = argparse.ArgumentParser(prog='uachat auth')
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ['rotation'] and len(raw)>1 and raw[1] in ('on','off'):
        raw = [raw[0],'--rotate',raw[1]]+raw[2:]
    parser.add_argument('action', choices=['login', 'logout', 'status', 'usage', 'accounts', 'use', 'rotation', 'doctor'])
    parser.add_argument('provider', nargs='?', default='openai-codex', choices=['openai-codex', 'google-antigravity'])
    parser.add_argument('--account', help='saved account email or unambiguous id')
    parser.add_argument('--all', action='store_true', help='all accounts (usage/logout)')
    parser.add_argument('--refresh', action='store_true', help='force fresh usage data')
    parser.add_argument('--cached', action='store_true', help='allow recent quota cache')
    parser.add_argument('--rotate', choices=['on','off'])
    parser.add_argument('--theme', help='login/usage colour theme')
    parser.add_argument('--color', choices=['auto','always','never'], default='auto')
    parser.add_argument('--no-color', action='store_true')
    parser.add_argument('--print-url', action='store_true', help='also show the full browser OAuth URL')
    parser.add_argument('--headless', action='store_true', help='device-code login (Codex) or manual callback (Google)')
    parser.add_argument('--import-existing', action='store_true', help='explicitly import from Codex CLI or OMP')
    args = parser.parse_args(raw)
    path = os.path.expanduser(codex_auth.CODEX_AUTH_PATH)
    ui = AuthUI(theme=theme, color='never' if args.no_color else args.color, print_url=args.print_url, theme_name=args.theme)
    try:
        if args.action == 'doctor':
            import diagnostics
            return diagnostics.report(args.provider,ui)
        if args.provider == 'google-antigravity':
            if args.action not in ('login','logout','status'):
                raise ValueError('usage/rotation аккаунтов Google пока не реализованы; эти команды поддерживают Codex')
            import antigravity
            if args.import_existing:
                raise ValueError('external import is only supported for Codex')
            antigravity.auth(args.action, args.headless, ui=ui)
            return 0
        import codex_pool
        if args.action == 'login':
            codex_pool.adopt_legacy()
            if args.import_existing:
                found = codex_auth._omp_accounts() + codex_auth._codex_accounts()
                live = sorted((a for a in found if a['valid']), key=lambda a: a['expires_ms'], reverse=True)
                if not live:
                    raise ValueError('no usable credentials to import')
                a = live[0]
                response = {'access_token': a['access'], 'refresh_token': a['refresh']}
                payload = build_payload(response, {'account_id': a['account_id']})
            else:
                response = device_login(ui) if args.headless else browser_login(ui)
                payload = build_payload(response)
            ui.line('Авторизация получена · сохраняю аккаунт…')
            key = codex_pool.register(payload)
            ui.line('Готово · '+str(payload.get('email') or key[:8])+' · аккаунт сохранён.', 'ok')
            ui.line('Повторный login добавляет аккаунт, а не удаляет предыдущий.')
            rotation = codex_pool.entries()[0].get('auto_rotate',True)
            ui.line('Авторотация Codex: '+('включена' if rotation else 'выключена')+' · /rotation on|off')
        elif args.action == 'logout':
            codex_pool.remove(args.account, all_accounts=args.all)
            ui.line('Аккаунт удалён из uachat. Внешние credentials не изменены.', 'ok')
        elif args.action == 'usage':
            if not codex_pool.show_usage(ui,all_accounts=args.all,refresh=args.refresh or not args.cached,selector=args.account):
                return 1
        elif args.action == 'accounts':
            codex_pool.show_accounts(ui)
        elif args.action == 'use':
            if not args.account: raise ValueError('используй auth use --account EMAIL_OR_ID')
            key = codex_pool.choose(args.account)
            ui.line('Активный аккаунт: '+key[:8], 'ok')
        elif args.action == 'rotation':
            if args.rotate: codex_pool.set_rotation(args.rotate == 'on')
            codex_pool.show_accounts(ui)
        else:
            selected_path = codex_pool.account_path(codex_pool.resolve(args.account)) if args.account else path
            payload = codex_auth._read_json(selected_path)
            if not isinstance(payload, dict):
                ui.line('Codex: вход не выполнен. login openai-codex', 'warn')
            else:
                tokens = payload.get('tokens') if isinstance(payload.get('tokens'), dict) else {}
                expiry = positive_int(payload.get('expires_ms')) or codex_auth._expires_from_token(tokens.get('access_token'))
                state = 'valid' if expiry and expiry > time.time() * 1000 else 'expired (refresh on next turn)'
                ui.line(f"Codex: {state} · {safe_text(payload.get('email') or 'account')}", "ok" if state == "valid" else "warn")
        return 0
    except KeyboardInterrupt:
        ui.line('Вход отменён.', 'warn')
        return 130
    except urllib.error.HTTPError as error:
        error.close()
        ui.line(f'OAuth HTTP {error.code}; повтори вход. Для device login может потребоваться разрешение в настройках ChatGPT.', 'error')
        return 1
    except (OSError, ValueError, KeyError) as error:
        if isinstance(error,OSError) and getattr(error,'errno',None) == 98:
            ui.line('Порт входа уже занят. Отмени предыдущий login (Ctrl-C) или используй --headless.', 'warn')
        ui.line(f'Ошибка авторизации: {safe_text(error)}', 'error')
        return 1

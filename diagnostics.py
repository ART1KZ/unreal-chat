"""Read-only auth diagnostics: no login, browser launch, token refresh or HTTP."""
from __future__ import annotations
import os
import shutil
import socket
import stat
import time

import auth_ui
import codex_auth
from terminal_ui import positive_int


def callback_port(port):
    try:
        with socket.socket() as connection:
            connection.bind(('127.0.0.1',port))
        return True
    except OSError:
        return False


def report(provider, ui):
    ui.title(provider+' · диагностика (без сетевых запросов)')
    core = shutil.which('unreal-agent-runner')
    ui.line('Runner: '+('найден' if core else 'не найден; установи core или укажи --binary'),'ok' if core else 'warn')
    wsl = auth_ui.is_wsl()
    ui.line('Среда: '+('WSL' if wsl else 'POSIX / desktop'))
    if wsl:
        opener = shutil.which('powershell.exe') or shutil.which('wslview')
        ui.line('Windows browser launcher: '+('найден (запуск не выполнялся)' if opener else 'не найден; используй --headless'),'ok' if opener else 'warn')
        ui.line('Browser callback требует Windows→WSL localhost forwarding; headless его не требует.')
    else:
        gui = bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
        ui.line('Графическое окружение: '+('есть' if gui else 'не обнаружено; для SSH используй --headless'))
    port = 1455 if provider == 'openai-codex' else 51121
    free = callback_port(port)
    ui.line(f'Callback 127.0.0.1:{port}: '+('свободен сейчас' if free else 'занят/недоступен; другой login не завершается автоматически'),'ok' if free else 'warn')
    proxy = any(os.environ.get(k) for k in ('HTTPS_PROXY','https_proxy','HTTP_PROXY','http_proxy'))
    ui.line('Прокси в окружении: '+('настроен (значение скрыто)' if proxy else 'не задан'))

    files = []
    if provider == 'openai-codex':
        import codex_pool
        legacy,index = codex_pool.paths()
        if os.path.exists(index):
            try:
                data = codex_pool._read()
                files = [codex_pool.account_path(key) for key in data['accounts']]
                ui.line('Сохранённых аккаунтов: '+str(len(files)))
                ui.line('Авторотация: '+('on' if data.get('auto_rotate') else 'off'))
            except ValueError:
                ui.line('Индекс аккаунтов повреждён; credentials не удалялись. Восстанови accounts.json.','error')
                return 1
        elif os.path.exists(legacy):
            files = [legacy]
            ui.line('Найден собственный legacy auth.json; миграция сейчас не выполнялась.')
        if os.path.exists(index): files.append(index)
    else:
        import antigravity
        path = os.path.expanduser(antigravity.AUTH_PATH)
        if os.path.exists(path): files = [path]
        try:
            antigravity.oauth_client()
            ui.line('Google OAuth app parameters: настроены (значения скрыты)','ok')
        except ValueError:
            ui.line('Google OAuth app parameters: не настроены','warn')
        ui.line('Google usage/rotation пока не поддерживаются.','warn')

    credential_files = [p for p in files if not p.endswith('accounts.json')]
    if not credential_files: ui.line('Вход не выполнен; login '+provider,'warn')
    bad_permissions = 0
    expired = 0
    refreshable = 0
    for path in files:
        try:
            if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                bad_permissions += 1
        except OSError:
            ui.line('Один из сохранённых файлов недоступен.','warn')
            continue
        payload = codex_auth._read_json(path)
        if path.endswith('accounts.json'): continue
        if not isinstance(payload,dict):
            ui.line('Один из credential-файлов повреждён.','error')
            continue
        tokens = payload.get('tokens') if isinstance(payload.get('tokens'),dict) else payload
        expiry = positive_int(payload.get('expires_ms')) or codex_auth._expires_from_token(tokens.get('access_token'))
        if not expiry or expiry <= time.time()*1000: expired += 1
        if tokens.get('refresh_token'): refreshable += 1
    ui.line(f'Истёк/неизвестен локальный срок access: {expired}; refresh token есть у {refreshable}.')
    if bad_permissions:
        ui.line(f'Небезопасные права у {bad_permissions} файлов: нужны 0600. Значения секретов не выводились.','warn')
    else:
        ui.line('Проверенные credential-файлы не доступны группе/остальным.','ok')
    ui.line('Подлинность токенов и доступность сервиса без HTTP не проверяются. Для квот: /usage.')
    return 0

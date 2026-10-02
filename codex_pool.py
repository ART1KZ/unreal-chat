"""Private Codex account pool, quota reader and safe between-turn rotation.

WHAM usage protocol: openai/codex backend-client/client/rate_limit_resets.rs.
No credit purchases, limit-reset redemption or automatic replay of agent turns.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, ExitStack

import codex_auth
from configio import file_lock, LockBusyError
from secure_http import urlopen
from terminal_ui import safe_text, positive_int

USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
CACHE_SECONDS = 60


def paths():
    legacy = os.path.expanduser(codex_auth.CODEX_AUTH_PATH)
    return legacy, os.path.join(os.path.dirname(legacy),'accounts.json')


def key_for(payload):
    account, user = codex_auth.credential_identity(payload)
    subject = user or payload.get('email') or account
    raw = str(account)+'\0'+str(subject)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def account_path(key):
    if not re.fullmatch(r'[a-f0-9]{32}',key): raise ValueError('invalid account id')
    return os.path.join(os.path.dirname(paths()[0]),'accounts',key+'.json')


def _read():
    _, index = paths()
    data = codex_auth._read_json(index)
    if not isinstance(data,dict) or not isinstance(data.get('accounts'),dict):
        if os.path.exists(index):
            raise ValueError('Повреждён Codex accounts.json; сохранённые credentials не удалены. Восстанови индекс из резервной копии.')
        return {'version':1,'active':None,'auto_rotate':True,'accounts':{},'sessions':{}}
    data['accounts'] = {k:v for k,v in data['accounts'].items() if re.fullmatch(r'[a-f0-9]{32}',k) and isinstance(v,dict)}
    sessions = data.get('sessions')
    data['sessions'] = {s:k for s,k in sessions.items() if isinstance(s,str) and isinstance(k,str) and k in data['accounts']} if isinstance(sessions,dict) else {}
    if not isinstance(data.get('active'),str) or data.get('active') not in data['accounts']: data['active'] = next(iter(data['accounts']),None)
    if not isinstance(data.get('auto_rotate'),bool): data['auto_rotate'] = True
    return data


def _write(data):
    codex_auth._write_private_json(paths()[1],data)


def adopt_legacy():
    """One-time migration of uachat's OWN file, never any third-party store."""
    legacy, index = paths()
    with file_lock(index):
        if os.path.exists(index): return
        data = _read()
        payload = codex_auth._read_json(legacy)
        if isinstance(payload,dict) and isinstance(payload.get('tokens'),dict) and payload['tokens'].get('account_id'):
            key = key_for(payload)
            codex_auth._write_private_json(account_path(key),payload)
            data['accounts'][key] = {'email':payload.get('email'),'added_at':time.time()}
            data['active'] = key
        _write(data)


def register(payload):
    if not isinstance(payload,dict) or not payload.get('tokens',{}).get('account_id'):
        raise ValueError('invalid Codex credentials')
    adopt_legacy()
    key = key_for(payload)
    path = account_path(key)
    # Lease precedes index lock everywhere, avoiding refresh/selection deadlocks.
    with file_lock(path+'.lease',timeout=5), file_lock(paths()[1]):
        data = _read()
        codex_auth._write_private_json(path,payload)
        data['accounts'][key] = {'email':payload.get('email'),'added_at':time.time()}
        data['active'] = key
        data['sessions'] = {}
        _write(data)
        codex_auth._write_private_json(paths()[0],payload)
    return key


def entries():
    adopt_legacy()
    with file_lock(paths()[1]):
        data = _read()
        return data, [(key,dict(value)) for key,value in data['accounts'].items() if isinstance(value,dict)]


def resolve(selector=None):
    data, rows = entries()
    if selector is None:
        key = data.get('active')
        if key in data['accounts']: return key
        raise ValueError('Нет сохранённых аккаунтов; uachat login openai-codex')
    matches = [key for key,item in rows if key == selector or key.startswith(selector) or
               (item.get('email') or '').lower() == selector.lower()]
    if len(matches) != 1: raise ValueError('Аккаунт не найден или имя неоднозначно; смотри auth accounts')
    return matches[0]


def choose(selector):
    key = resolve(selector)
    import native_auth
    with file_lock(account_path(key)+'.lease',timeout=5):
        payload = native_auth.ensure_fresh(account_path(key))
        with file_lock(paths()[1]):
            data = _read()
            if key not in data['accounts']: raise ValueError('account was removed')
            data['active'], data['sessions'] = key, {}
            _write(data)
            codex_auth._write_private_json(paths()[0],payload)
    return key


def remove(selector=None, all_accounts=False):
    data, rows = entries()
    if not rows:
        try: os.unlink(paths()[0])
        except FileNotFoundError: pass
        return
    keys = [key for key,_ in rows] if all_accounts else [resolve(selector)]
    for key in keys:
        with file_lock(account_path(key)+'.lease',timeout=5), file_lock(paths()[1]):
            data = _read()
            data['accounts'].pop(key,None)
            data['sessions'] = {s:k for s,k in data['sessions'].items() if k != key}
            if data.get('active') == key: data['active'] = next(iter(data['accounts']),None)
            _write(data)
            try: os.unlink(account_path(key))
            except FileNotFoundError: pass
    # The compatibility file is not a live runner credential: turns use snapshots.
    with file_lock(paths()[1]):
        data = _read()
        current = codex_auth._read_json(account_path(data['active'])) if data.get('active') else None
        if current: codex_auth._write_private_json(paths()[0],current)
        else:
            try: os.unlink(paths()[0])
            except FileNotFoundError: pass
    codex_auth._ACCOUNTS = None


def set_rotation(enabled):
    adopt_legacy()
    with file_lock(paths()[1]):
        data = _read()
        data['auto_rotate'] = bool(enabled)
        _write(data)


def fetch_usage(payload):
    tokens = payload['tokens']
    req = urllib.request.Request(USAGE_URL,headers={'Authorization':'Bearer '+tokens['access_token'],
        'ChatGPT-Account-ID':tokens['account_id'],'User-Agent':'codex-cli','Originator':'codex_cli_rs','Accept':'application/json'})
    with urlopen(req,timeout=12) as response:
        raw = response.read(1024*1024+1)
    if len(raw)>1024*1024: raise ValueError('usage payload exceeds 1 MiB')
    data = json.loads(raw)
    if not isinstance(data,dict) or not isinstance(data.get('rate_limit'),dict):
        raise ValueError('usage response has no quota information')
    return data


def cached_usage(item, now=None):
    cached = item.get('usage')
    now = time.time() if now is None else now
    checked = positive_int(item.get('checked_at')) or 0
    if not isinstance(cached,dict) or not 0 <= now-checked < CACHE_SECONDS:
        return None
    for _,group in quota_groups(cached):
        for field in ('primary_window','secondary_window'):
            window = group.get(field)
            reset = positive_int(window.get('reset_at')) if isinstance(window,dict) else None
            if reset and checked < reset <= now: return None
    return cached


def _usage_locked(key, refresh=False):
    """Caller owns account lease; OAuth refresh and runtime snapshot cannot race."""
    import native_auth
    data, _ = entries()
    item = data['accounts'].get(key,{})
    cached = cached_usage(item)
    if not refresh and cached is not None:
        return cached
    payload = native_auth.ensure_fresh(account_path(key))
    try:
        usage = fetch_usage(payload)
    except urllib.error.HTTPError as error:
        if error.code != 401: raise
        error.close()
        payload = native_auth.ensure_fresh(account_path(key),force=True)
        usage = fetch_usage(payload)
    with file_lock(paths()[1]):
        data = _read()
        if key in data['accounts']:
            entry = data['accounts'][key]
            entry.update(usage=usage,checked_at=time.time())
            error = entry.get('last_error')
            if isinstance(error,dict) and error.get('kind') == 'quota':
                blocked = block_until(usage,str(error.get('model') or ''))
                entry.update(cooldown_until=blocked or 0,force_check=False)
                if not blocked: entry['last_error'] = None
            elif isinstance(error,dict) and error.get('kind') == 'refresh':
                entry['last_error'] = None
            _write(data)
    return usage


def usage_for(key, refresh=False):
    if not refresh:
        data, _ = entries()
        cached = cached_usage(data['accounts'].get(key,{}))
        if cached is not None: return cached
    with file_lock(account_path(key)+'.lease',timeout=0):
        return _usage_locked(key,refresh)


def quota_groups(usage, model=''):
    base = usage.get('rate_limit')
    groups = [('Codex',base if isinstance(base,dict) else {})]
    for item in usage.get('additional_rate_limits') or []:
        if not isinstance(item,dict): continue
        details = item.get('details') if isinstance(item.get('details'),dict) else item
        group = details.get('rate_limit')
        name = details.get('limit_name') or details.get('metered_feature') or item.get('normal_model_slug') or 'additional'
        slug = item.get('normal_model_slug')
        matches = any(str(value).lower().replace('_','-') == model.lower() for value in
                      (slug, details.get('limit_name'),details.get('metered_feature')) if value)
        if isinstance(group,dict) and (not model or matches): groups.append((str(name),group))
    return groups


def percent(window):
    try:
        value = float(window.get('used_percent'))
        return value if 0 <= value <= 10000 else None
    except (ValueError,TypeError,AttributeError): return None


def block_until(usage,model=''):
    now = time.time()
    resets = []
    denied = False
    for _,group in quota_groups(usage,model):
        # The service can permit requests using included/extra credits even at
        # 100% of a displayed window. Explicit allowed=True is authoritative.
        if group.get('allowed') is True:
            continue
        if group.get('allowed') is False or group.get('limit_reached') is True: denied = True
        for field in ('primary_window','secondary_window'):
            window = group.get(field)
            if not isinstance(window,dict): continue
            used = percent(window)
            if used is not None and used>=100:
                denied = True
                reset = positive_int(window.get('reset_at')) or (now+(positive_int(window.get('reset_after_seconds')) or 300))
                if reset>now: resets.append(reset)
    return max(resets,default=now+300) if denied else None


def duration(seconds):
    seconds = positive_int(seconds)
    if not seconds: return 'window'
    if seconds % 86400 == 0: return str(seconds//86400)+'d'
    if seconds % 3600 == 0: return str(seconds//3600)+'h'
    return str(seconds//60)+'m' if seconds>=60 else str(seconds)+'s'


def quota_label(usage, model=''):
    groups = quota_groups(usage,model)
    labels = []
    for name,group in groups:
        for field in ('primary_window','secondary_window'):
            window = group.get(field)
            used = percent(window)
            if used is not None:
                prefix = (name+' ') if name != 'Codex' else ''
                labels.append(prefix+duration(window.get('limit_window_seconds'))+' '+f'{max(0,100-used):g}% left')
    return 'quota '+', '.join(labels) if labels else 'quota ?'


def _mark(key,**values):
    with file_lock(paths()[1]):
        data = _read()
        if key in data['accounts']:
            data['accounts'][key].update(values)
            _write(data)


def note_error(key,message,model=''):
    # Do not confuse model quota with 403 region/security denial or every 429.
    if re.search(r'usage_limit_reached|insufficient_quota|quota_exceeded|usage limit (?:has been )?reached|quota exceeded',message,re.I):
        _mark(key,cooldown_until=time.time()+300,force_check=True,last_error={'kind':'quota','model':model,'at':time.time()})
        return True
    return False


def cleanup_runtime(directory):
    for name in os.listdir(directory):
        match = re.fullmatch(r'turn-([0-9]+)-[a-zA-Z0-9_]+\.json',name)
        if not match: continue
        pid = int(match[1])
        if pid <= 1: continue
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            try: os.unlink(os.path.join(directory,name))
            except FileNotFoundError: pass
        except PermissionError:
            pass


@contextmanager
def turn(session, model='', on_select=None):
    adopt_legacy()
    data, rows = entries()
    first = data['sessions'].get(session) or data.get('active')
    ordered = ([first] if first in data['accounts'] else [])+[k for k,_ in rows if k != first]
    if not data.get('auto_rotate',True): ordered = ordered[:1]
    rejected = []
    busy = []
    for key in ordered:
        path = account_path(key)
        with ExitStack() as lease:
            try:
                lease.enter_context(file_lock(path+'.lease',timeout=0))
            except LockBusyError:
                busy.append(key)
                continue
            # Recheck membership after acquiring a potentially contended lease.
            current, _ = entries()
            item = current['accounts'].get(key)
            if not item: continue
            import native_auth
            try:
                payload = native_auth.ensure_fresh(path)
            except (OSError,ValueError) as error:
                code = error.code if isinstance(error,urllib.error.HTTPError) else None
                _mark(key,last_error={'kind':'refresh','http_status':code,'at':time.time()})
                if isinstance(error,urllib.error.HTTPError): error.close()
                rejected.append(key)
                continue
            cached = item.get('usage') if isinstance(item.get('usage'),dict) else {}
            usage_error = None
            try:
                usage = _usage_locked(key,refresh=bool(item.get('force_check')))
                blocked = block_until(usage,model)
                _mark(key,cooldown_until=blocked or 0,force_check=False,last_error=None)
            except (OSError,ValueError) as error:
                usage_error = f'HTTP {error.code}' if isinstance(error,urllib.error.HTTPError) else 'usage unavailable'
                if isinstance(error,urllib.error.HTTPError): error.close()
                usage = cached
                cooldown = positive_int(item.get('cooldown_until')) or 0
                blocked = cooldown if cooldown>time.time() else None
                # A network failure is NOT proof of exhausted quota.
            if blocked:
                rejected.append(key)
                continue
            with file_lock(paths()[1]):
                current = _read()
                current['active'] = key
                current['sessions'][session] = key
                while len(current['sessions'])>256: current['sessions'].pop(next(iter(current['sessions'])))
                _write(current)
            payload = native_auth.ensure_fresh(path)
            with file_lock(paths()[1]):
                if _read().get('active') == key:
                    codex_auth._write_private_json(paths()[0],payload)
            info = {'key':key,'email':item.get('email') or key[:8],'usage':usage,'usage_error':usage_error,
                    'rotated': bool(first and first != key)}
            directory = os.path.join(os.path.dirname(paths()[0]),'runtime')
            os.makedirs(directory,mode=0o700,exist_ok=True)
            cleanup_runtime(directory)
            fd,snapshot = tempfile.mkstemp(prefix=f'turn-{os.getpid()}-',suffix='.json',dir=directory)
            os.close(fd)
            codex_auth._write_private_json(snapshot,payload)
            try:
                if on_select: on_select(info)
                yield snapshot, info
            finally:
                updated = codex_auth._read_json(snapshot)
                # The runner may refresh during a long turn: save rotation back.
                if isinstance(updated,dict) and isinstance(updated.get('tokens'),dict) and updated['tokens'] != payload['tokens']:
                    if updated['tokens'].get('account_id') == payload['tokens'].get('account_id') and key_for(updated) == key:
                        try:
                            native_auth._save(updated['tokens'],path,payload['tokens'])
                            with file_lock(paths()[1]):
                                if _read().get('active') == key:
                                    codex_auth._write_private_json(paths()[0],codex_auth._read_json(path))
                        except (OSError,ValueError): pass
                try: os.unlink(snapshot)
                except FileNotFoundError: pass
            return
    if busy:
        raise LockBusyError("Аккаунт занят другим ходом. Выбери другой /accounts use или дождись завершения. Ход не запущен.")
    if rejected:
        raise ValueError('Сохранённые аккаунты недоступны/исчерпали лимиты. /usage --all покажет квоты; /login добавляет аккаунт. Ход не запущен.')
    raise ValueError('Нет аккаунтов Codex; /login openai-codex')


def cached_status(session, model=''):
    """Public cache-only session status; no migration, refresh, locks or HTTP."""
    data = _read()
    key = data['sessions'].get(session) or data.get('active')
    item = data['accounts'].get(key,{})
    usage = item.get('usage') if isinstance(item.get('usage'),dict) else {}
    error = item.get('last_error')
    limited = isinstance(error,dict) and error.get('kind') == 'quota' and error.get('model') in ('',model)
    return {'key':key,'usage':usage,'limited':limited,'checked_at':item.get('checked_at')}


def show_accounts(ui):
    data,rows = entries()
    ui.title('Codex · сохранённые аккаунты')
    if not rows: ui.line('Пока нет аккаунтов. /login openai-codex','warn')
    for key,item in rows:
        mark = '*' if key == data.get('active') else ' '
        ui.line(f'{mark} {key[:8]} · {item.get("email") or "account"}', 'accent')
        if item.get('usage'): ui.line('    '+quota_label(item['usage']))
        error = item.get('last_error')
        if isinstance(error,dict) and error.get('kind') == 'refresh':
            ui.line('    последний refresh не удался'+(' · HTTP '+str(error['http_status']) if error.get('http_status') else '')+' · повторный /login может помочь','warn')
    ui.line('Авторотация: '+('включена' if data.get('auto_rotate',True) else 'выключена'))


def show_usage(ui, all_accounts=False, refresh=True, selector=None):
    data,rows = entries()
    keys = [k for k,_ in rows] if all_accounts else [resolve(selector)]
    ui.title('Codex · лимиты подписки (не счётчик токенов контекста)')
    if not keys:
        ui.line('Нет аккаунтов. login openai-codex', 'warn')
        return False
    successes = 0
    for key in keys:
        item = data['accounts'][key]
        ui.line((item.get('email') or key[:8])+(' · активный' if key == data.get('active') else ''),'accent')
        try:
            usage = usage_for(key,refresh=refresh)
            successes += 1
            ui.line('План: '+str(usage.get('plan_type') or '?'))
            for name,group in quota_groups(usage):
                ui.line(name+(' · лимит достигнут' if group.get('limit_reached') else ''))
                for field in ('primary_window','secondary_window'):
                    window = group.get(field)
                    used = percent(window)
                    if used is None: continue
                    reset = positive_int(window.get('reset_at'))
                    try:
                        reset_label = time.strftime('%Y-%m-%d %H:%M %Z',time.localtime(reset)) if reset else 'неизвестен'
                    except (OSError,OverflowError,ValueError):
                        reset_label = 'неизвестен'
                    ui.line(f'  {duration(window.get("limit_window_seconds"))}: использовано {used:g}% · осталось {max(0,100-used):g}% · сброс {reset_label}', 'warn' if used>=90 else 'ok')
            credits = usage.get('credits')
            if isinstance(credits,dict):
                ui.line('Кредиты: '+('unlimited' if credits.get('unlimited') else str(credits.get('balance') if credits.get('balance') is not None else '?')))
        except (OSError,ValueError) as error:
            if isinstance(error,LockBusyError):
                ui.line('Аккаунт занят активным ходом; используй /usage --cached или проверь после завершения.','warn')
            elif isinstance(error,urllib.error.HTTPError):
                ui.line(f'Квоты недоступны: HTTP {error.code}. Это не означает 100% usage.','warn')
                error.close()
            else: ui.line('Не удалось получить квоты. Сеть/авторизация/формат ответа.','warn')

    return successes > 0

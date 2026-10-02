"""Experimental standalone Cloud Code transport, stdlib only.

Protocol informed by ART1KZ/omp-antigravity-pro (MIT) and can1357/oh-my-pi
(MIT). No imports, processes or credential stores from either are used.
"""
from __future__ import annotations
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

import codex_auth
from native_auth import _lock

ENDPOINT = 'https://daily-cloudcode-pa.googleapis.com'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REDIRECT = 'http://localhost:51121/oauth-callback'
SCOPES = ['cloud-platform', 'userinfo.email', 'userinfo.profile', 'cclog', 'experimentsandconfigs']
AUTH_PATH = '~/.config/uachat/antigravity/auth.json'
METADATA = {'ideType': 'ANTIGRAVITY', 'platform': 'PLATFORM_UNSPECIFIED', 'pluginType': 'GEMINI'}
USER_AGENT = 'antigravity/hub/2.8.0 (aidev_client; os_type=darwin; arch=arm64; cl=963137146)'


def request(url, payload=None, access=None, form=False):
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if access:
        headers['Authorization'] = 'Bearer ' + access
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        body = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
    return urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=60)


def oauth_client():
    import models
    config = models.load_env()
    client_id = os.environ.get('UACHAT_ANTIGRAVITY_CLIENT_ID') or config.get('UACHAT_ANTIGRAVITY_CLIENT_ID')
    client_secret = os.environ.get('UACHAT_ANTIGRAVITY_CLIENT_SECRET') or config.get('UACHAT_ANTIGRAVITY_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise ValueError('Configure UACHAT_ANTIGRAVITY_CLIENT_ID and UACHAT_ANTIGRAVITY_CLIENT_SECRET in ~/.config/uachat/env before Google login')
    return client_id, client_secret


def token_request(data):
    client_id, client_secret = oauth_client()
    with request(TOKEN_URL, {**data, 'client_id': client_id, 'client_secret': client_secret}, form=True) as r:
        return json.load(r)


def discover_project(access):
    """Daily routing + consumer fallback for unavailable/ineligible onboarding."""
    try:
        with request(ENDPOINT+'/v1internal:loadCodeAssist', {'metadata': METADATA}, access) as r:
            data = json.load(r)
        project = data.get('cloudaicompanionProject')
        if isinstance(project, dict):
            project = project.get('id')
        if project:
            return project
        tiers = data.get('allowedTiers', [])
        if data.get('ineligibleTiers') and not tiers:
            return 'aicode-consumers'
        tier = next((t['id'] for t in tiers if t.get('isDefault')), (tiers[0].get('id') if tiers else 'standard-tier'))
        with request(ENDPOINT+'/v1internal:onboardUser', {'tierId': tier, 'metadata': METADATA}, access) as r:
            operation = json.load(r)
        project = operation.get('response', {}).get('cloudaicompanionProject')
        if isinstance(project, dict):
            project = project.get('id')
        return project or 'aicode-consumers'
    except urllib.error.HTTPError as error:
        raw = error.read().decode('utf-8', 'replace')
        if 'VALIDATION_REQUIRED' in raw:
            try:
                details = json.loads(raw[raw.index('{'):]).get('error', {}).get('details', [])
                url = next(d['metadata']['validation_url'] for d in details if d.get('reason') == 'VALIDATION_REQUIRED')
            except (ValueError, KeyError, StopIteration):
                url = 'your Google account verification page'
            raise ValueError('Account verification required: visit ' + url) from None
        if error.code == 401:
            raise ValueError('Google rejected the OAuth token') from None
        return 'aicode-consumers'
    except OSError:
        return 'aicode-consumers'


def credentials(force=False):
    path = os.path.expanduser(AUTH_PATH)
    with _lock(path):
        data = codex_auth._read_json(path)
        if not isinstance(data, dict) or not data.get('refresh_token'):
            raise ValueError('run uachat login google-antigravity')
        if force or data.get('expires_ms', 0) < (time.time()+120)*1000:
            result = token_request({'grant_type': 'refresh_token', 'refresh_token': data['refresh_token']})
            data.update(access_token=result['access_token'], expires_ms=int((time.time()+result['expires_in'])*1000))
            if result.get('refresh_token'):
                data['refresh_token'] = result['refresh_token']
            codex_auth._write_private_json(path, data)
        return data


def auth(action, headless=False):
    path = os.path.expanduser(AUTH_PATH)
    if action == 'status':
        data = codex_auth._read_json(path)
        print('google-antigravity: ' + ('not logged in' if not data else
              ('valid' if data.get('expires_ms', 0)>time.time()*1000 else 'refresh needed')))
        return
    if action == 'logout':
        with _lock(path):
            if os.path.exists(path):
                os.unlink(path)
        print('Logged out of google-antigravity.')
        return
    client_id, _ = oauth_client()
    state = secrets.token_urlsafe(32)
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': client_id, 'redirect_uri': REDIRECT, 'response_type':'code',
        'scope': ' '.join('https://www.googleapis.com/auth/'+s for s in SCOPES),
        'state':state, 'access_type':'offline', 'prompt':'consent'})
    if headless:
        print('Open this URL in a browser:\n'+url)
        # Never read callback secrets through the persistent chat editor/history.
        callback = input('Paste the full redirected localhost URL here: ').strip()
        parsed = urllib.parse.urlsplit(callback)
        if parsed.hostname != 'localhost' or parsed.port != 51121 or parsed.path != '/oauth-callback':
            raise ValueError('invalid callback URL')
        values = urllib.parse.parse_qs(parsed.query)
        if not secrets.compare_digest(values.get('state',[''])[0],state):
            raise ValueError('OAuth state mismatch')
        code = values.get('code',[''])[0]
    else:
        result = {}
        class Callback(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                values = urllib.parse.parse_qs(parsed.query)
                valid = parsed.path == '/oauth-callback' and secrets.compare_digest(values.get('state',[''])[0],state)
                self.send_response(200 if valid else 400)
                self.end_headers()
                self.wfile.write(b'Return to uachat.' if valid else b'Invalid callback.')
                if valid:
                    result['code'] = values.get('code',[''])[0]
        with http.server.HTTPServer(('127.0.0.1',51121),Callback) as server:
            server.timeout = 1
            print('Open this URL in a browser:\n'+url,flush=True)
            webbrowser.open(url)
            deadline = time.monotonic()+900
            while not result and time.monotonic()<deadline:
                server.handle_request()
        code = result.get('code','')
    if not code:
        raise ValueError('login denied or timed out')
    data = token_request({'grant_type':'authorization_code','code':code,'redirect_uri':REDIRECT})
    data['expires_ms'] = int((time.time()+data['expires_in'])*1000)
    data['project_id'] = discover_project(data['access_token'])
    if not data.get('refresh_token'):
        raise ValueError('Google returned no refresh token; revoke app consent and retry')
    with _lock(path):
        codex_auth._write_private_json(path,data)
    print('Logged in to google-antigravity.')


def schema(value):
    """Strip JSON Schema fields Cloud Code does not accept."""
    if isinstance(value,list):
        return [schema(v) for v in value]
    if isinstance(value,dict):
        return {k:schema(v) for k,v in value.items() if k not in
                ('$schema','$id','additionalProperties','default','examples')}
    return value


def text_key(text):
    return 'text:'+hashlib.sha256(text.encode()).hexdigest()


def translate(payload, saved, session):
    contents, systems, names = [], [], {}
    def add(role, parts):
        if not parts: return
        if contents and contents[-1]['role'] == role:
            contents[-1]['parts'].extend(parts)
        else:
            contents.append({'role':role,'parts':parts})
    items = payload.get('input',[])
    if isinstance(items,str): items = [{'role':'user','content':items}]
    for item in items:
        kind = item.get('type','message')
        if kind == 'function_call':
            call_id = item.get('call_id') or item.get('id')
            names[call_id] = item['name']
            part = saved.get(call_id) or {'functionCall':{'name':item['name'],'args':json.loads(item.get('arguments') or '{}')}}
            add('model', saved.get('prefix:'+call_id, []) + [part])
        elif kind == 'function_call_output':
            call_id = item.get('call_id')
            name = names.get(call_id) or saved.get(call_id,{}).get('functionCall',{}).get('name')
            if not name: raise ValueError('tool result has no matching call')
            add('user',[{'functionResponse':{'name':name,'response':{'output':item.get('output','')}}}])
        elif kind == 'message':
            content = item.get('content',[])
            if isinstance(content,str): content = [{'text':content}]
            parts = []
            for c in content:
                if 'text' in c:
                    saved_part = saved.get(text_key(c['text']))
                    parts.extend(saved_part if isinstance(saved_part, list) else [saved_part or {'text':c['text']}])
                else:
                    raise ValueError('Antigravity bridge currently supports text input only')
            role = item.get('role','user')
            if role in ('system','developer'):
                systems.extend(parts)
            else:
                add('model' if role == 'assistant' else 'user',parts)
        elif kind == 'reasoning':
            # Reasoning without provider signatures is not safe to replay.
            pass
        else:
            raise ValueError('unsupported Responses input item: '+kind)
    model = payload.get('model','gemini-3-flash')
    effort = payload.get('reasoning',{}).get('effort','high')
    tier = 'low' if effort in ('minimal','low') else ('medium' if effort=='medium' else 'high')
    budget = {'low':1000,'medium':4000,'high':10000}[tier]
    if 'flash' in model:
        import re
        model = re.sub(r'-(low|medium|high)$','',model)+'-'+tier
    config = {'thinkingConfig':{'includeThoughts':True,'thinkingBudget':budget}}
    if payload.get('max_output_tokens'): config['maxOutputTokens'] = payload['max_output_tokens']
    req = {'contents':contents,'generationConfig':config,'sessionId':session}
    if systems: req['systemInstruction'] = {'parts':systems}
    tools = []
    for tool in payload.get('tools',[]):
        if tool.get('type') != 'function': raise ValueError('only function tools are supported')
        tools.append({'name':tool['name'],'description':tool.get('description',''),
                      'parameters':schema(tool.get('parameters',{'type':'object','properties':{}}))})
    if tools: req['tools'] = [{'functionDeclarations':tools}]
    return {'model':model,'request':req,'userAgent':'antigravity','requestType':'agent',
            'requestId':str(uuid.uuid4())}


class AntigravityBridge:
    def __init__(self, session):
        self.session = session
        self.key = secrets.token_urlsafe(32)
        digest = hashlib.sha256(session.encode()).hexdigest()
        self.state_path = os.path.expanduser('~/.local/state/uachat/antigravity/'+digest+'.json')
        self.saved = codex_auth._read_json(self.state_path) or {}
        self.server = None

    def start(self):
        bridge = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+bridge.key):
                    self.send_response(401)
                    self.end_headers()
                    return
                sent = False
                try:
                    if self.path != '/v1/responses': raise ValueError('unknown bridge path')
                    from bridge import sanitize
                    size = int(self.headers.get('Content-Length',0))
                    if size > 64*1024*1024: raise ValueError('request too large')
                    raw,_ = sanitize(self.rfile.read(size))
                    payload = json.loads(raw)
                    body = translate(payload,bridge.saved,bridge.session)
                    auth_data = credentials()
                    body['project'] = auth_data.get('project_id') or 'aicode-consumers'
                    try:
                        upstream = request(ENDPOINT+'/v1internal:streamGenerateContent?alt=sse',body,auth_data['access_token'])
                    except urllib.error.HTTPError as error:
                        if error.code != 401: raise
                        error.close()
                        auth_data = credentials(force=True)
                        upstream = request(ENDPOINT+'/v1internal:streamGenerateContent?alt=sse',body,auth_data['access_token'])
                    with upstream:
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream')
                        self.send_header('Connection','close')
                        self.end_headers()
                        sent = True
                        def emit(event):
                            self.wfile.write(b'data: '+json.dumps(event,ensure_ascii=False).encode()+b'\n\n')
                            self.wfile.flush()
                        output, text, thought, usage = [], '', '', {}
                        signature = None
                        thought_parts = []
                        last_part = None
                        for line in upstream:
                            if not line.startswith(b'data:'): continue
                            data = line[5:].strip()
                            if data == b'[DONE]': break
                            event = json.loads(data)
                            if event.get('error'): raise ValueError('Cloud Code stream error: '+str(event['error'].get('status','unknown')))
                            response = event.get('response',event)
                            usage.update(response.get('usageMetadata',{}))
                            for candidate in response.get('candidates',[]):
                                if candidate.get('finishReason') in ('SAFETY','RECITATION','PROHIBITED_CONTENT'):
                                    raise ValueError('generation blocked: '+candidate['finishReason'])
                                for part in candidate.get('content',{}).get('parts',[]):
                                    if part.get('thoughtSignature') and len(part) == 1 and last_part is not None:
                                        last_part['thoughtSignature'] = part['thoughtSignature']
                                        continue
                                    last_part = part
                                    if part.get('functionCall'):
                                        call = part['functionCall']
                                        cid = call.get('id') or 'call_'+uuid.uuid4().hex
                                        bridge.saved[cid] = part
                                        if thought_parts:
                                            bridge.saved['prefix:'+cid] = list(thought_parts)
                                            thought_parts.clear()
                                        output.append({'type':'function_call','id':'fc_'+uuid.uuid4().hex,'call_id':cid,
                                                       'name':call['name'],'arguments':json.dumps(call.get('args',{})),'status':'completed'})
                                    elif 'text' in part:
                                        if part.get('thought'):
                                            thought += part['text']
                                            thought_parts.append(part)
                                        else:
                                            text += part['text']
                                            emit({'type':'response.output_text.delta','delta':part['text'],'output_index':0})
                                            if part.get('thoughtSignature'): signature = part['thoughtSignature']
                        if thought:
                            output.insert(0,{'type':'reasoning','id':'rs_'+uuid.uuid4().hex,
                                             'summary':[{'type':'summary_text','text':thought}]})
                        if text:
                            part = {'text':text}
                            if signature: part['thoughtSignature'] = signature
                            bridge.saved[text_key(text)] = thought_parts + [part]
                            output.append({'type':'message','id':'msg_'+uuid.uuid4().hex,'role':'assistant',
                                           'content':[{'type':'output_text','text':text}]})
                        if not output: raise ValueError('empty Antigravity response')
                        codex_auth._write_private_json(bridge.state_path,bridge.saved)
                        emit({'type':'response.completed','response':{'id':'resp_'+uuid.uuid4().hex,'status':'completed',
                            'output':output,'usage':{'input_tokens':usage.get('promptTokenCount',0),
                            'output_tokens':usage.get('candidatesTokenCount',0)+usage.get('thoughtsTokenCount',0),
                            'input_tokens_details':{'cached_tokens':usage.get('cachedContentTokenCount',0)},
                            'output_tokens_details':{'reasoning_tokens':usage.get('thoughtsTokenCount',0)}}}})
                        self.wfile.write(b'data: [DONE]\n\n')
                except (BrokenPipeError,ConnectionResetError):
                    pass
                except Exception as error:
                    if isinstance(error,urllib.error.HTTPError):
                        message = f'Cloud Code HTTP {error.code} (quota/access/region); check account eligibility'
                    else:
                        message = str(error)
                    if sent:
                        try:
                            self.wfile.write(b'data: '+json.dumps({'type':'error','message':message}).encode()+b'\n\n')
                        except OSError: pass
                    else:
                        self.send_response(502)
                        self.send_header('Content-Type','application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({'error':{'message':message}}).encode())
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

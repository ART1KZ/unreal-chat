"""Discovery, selector and native SkillUse regressions; local mock only."""
import http.server
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import editor
import skills
import uarchat
from mock_responses import sse, message_item, response_obj

ROOT = Path(__file__).resolve().parent
BINARY = skills.adapter_binary()


class SelectorTests(unittest.TestCase):
    def catalog(self):
        catalog = object.__new__(skills.Catalog)
        catalog.skills = [dict(name='review',description='Review code',path='/original/review/SKILL.md',source='user',auto=True,user=True),
                          dict(name='deploy',description='Manual deployment',path='/original/deploy/SKILL.md',source='user',auto=False,user=True),
                          dict(name='hidden',description='Background',path='/original/hidden/SKILL.md',source='user',auto=True,user=False)]
        catalog.diagnostics = []
        return catalog

    def test_mentions_and_explicit_invocation(self):
        catalog = self.catalog()
        self.assertEqual(catalog.suggestions('Please use $rev'), [('$review','Review code')])
        self.assertEqual(catalog.suggestions('/skill dep'), [('deploy','Manual deployment')])
        self.assertEqual(catalog.suggestions('$hid'), [])
        self.assertEqual(catalog.invoke('/skill review inspect this'), '$review inspect this')
        with self.assertRaises(ValueError): catalog.invoke('/skill missing')
        with self.assertRaises(ValueError): catalog.invoke('/skill hidden')
        self.assertIn('manual only',catalog.display('deploy'))
        self.assertIn('/original/deploy/SKILL.md',catalog.display('deploy'))

    def test_slash_skills_merge_with_commands_and_respect_collisions(self):
        catalog = self.catalog()
        theme = uarchat.Theme('mono',False)
        rows = uarchat.command_suggestions('/',theme,skills=catalog)
        self.assertIn('/help',[name for name,_ in rows])
        self.assertIn('/review',[name for name,_ in rows])
        self.assertIn('/deploy',[name for name,_ in rows])
        self.assertNotIn('/hidden',[name for name,_ in rows])
        self.assertEqual(uarchat.command_suggestions('/rev',theme,skills=catalog),[('/review','skill · Review code')])
        self.assertEqual(catalog.invoke('/review inspect\nthis change',uarchat.COMMANDS),'$review inspect\nthis change')
        self.assertEqual(catalog.invoke('/deploy staging',uarchat.COMMANDS),'$deploy staging')
        self.assertFalse(catalog.is_slash_skill('/hidden',uarchat.COMMANDS))
        collision=dict(catalog.skills[0],name='help')
        catalog.skills.append(collision)
        rows=uarchat.command_suggestions('/he',theme,skills=catalog)
        self.assertEqual([name for name,_ in rows],['/help'])
        self.assertEqual(catalog.invoke('/help',uarchat.COMMANDS),'/help')
        self.assertEqual(catalog.invoke('/skill help task',uarchat.COMMANDS),'$help task')

    def test_live_composer_displays_skill_hints(self):
        from live import Composer
        catalog = self.catalog()
        composer = Composer(suggestions=catalog.suggestions)
        composer.feed_key('text','$rev')
        self.assertTrue(any('$review' in line for line in composer._menu_lines(100)))
        composer.feed_key('key','tab')
        self.assertEqual(composer._buffer,'$review')

    def test_editor_completion_in_middle_and_multiline(self):
        catalog = self.catalog()
        editor_instance = editor.Editor(suggestions=catalog.suggestions)
        editor_instance._buffer = 'first line\nUse $rev here'
        editor_instance._pos = len(editor_instance._buffer)
        for _ in range(len(' here')): editor_instance.feed_key('key','left')
        editor_instance._accept()
        self.assertEqual(editor_instance._buffer,'first line\nUse $review here')


@unittest.skipUnless(BINARY,'adapter not installed')
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root/'workspace'
        self.workspace.mkdir()
        self.extra = self.root/'personal'
        path = self.extra/'review/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: fixture-review\ndescription: >-\n  Review the fixture.\n---\nBODY_PROBE_789\nRead references/context.md relative to this skill.\n')
        manual = self.extra/'manual/SKILL.md'
        manual.parent.mkdir(parents=True)
        manual.write_text('---\nname: fixture-manual\ndescription: Manual only.\ndisable-model-invocation: true\n---\nBODY_MANUAL_789\n')
        reference = self.extra/'review/references/context.md'
        reference.parent.mkdir(parents=True)
        reference.write_text('RESOURCE_PROBE_789')
        self.requests = []
        requests = self.requests
        self.gate = {'hold':False}
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        gate, first_started, release_first = self.gate, self.first_started, self.release_first
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                if len(requests)==1 and gate['hold']:
                    first_started.set()
                    release_first.wait(10)
                if len(requests)==1:
                    name = 'fixture-manual' if '$fixture-manual' in json.dumps(request) else 'fixture-review'
                    items = [{'type':'function_call','id':'fc_probe','call_id':'call_probe','name':'SkillUse',
                              'arguments':json.dumps({'name':name}),'status':'completed'}]
                elif '$fixture-manual' in json.dumps(request) and 'BODY_MANUAL_789' not in json.dumps(request):
                    items = [{'type':'function_call','id':'fc_manual','call_id':'call_manual','name':'SkillUse',
                              'arguments':json.dumps({'name':'fixture-manual'}),'status':'completed'}]
                elif len(requests)==2 and 'BODY_PROBE_789' in json.dumps(request):
                    import shlex
                    items = [{'type':'function_call','id':'fc_resource','call_id':'call_resource','name':'Bash',
                              'arguments':json.dumps({'command':'cat '+shlex.quote(str(reference))}),'status':'completed'}]
                else: items = [message_item('Offline skill loaded.')]
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                self.wfile.write(sse({'type':'response.completed','response':response_obj(items,rid='response-'+str(len(requests)))}))
                self.wfile.write(b'data: [DONE]\n\n')
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('UACHAT_','UNREAL_HARNESS_','OPENAI_','XDG_','SANDBOX_'))}
        self.env.update(HOME=str(self.root),UACHAT_SKILL_DIRS=str(self.extra),UACHAT_WINDOWS_HOME='off',UACHAT_LIVE_BINARY=BINARY,
                        UNREAL_HARNESS_LLM_PROVIDER='ollama',UACHAT_PROVIDER='ollama',UNREAL_HARNESS_LLM_MODEL='mock',
                        UNREAL_HARNESS_LLM_BASE_URL=f'http://127.0.0.1:{self.server.server_port}/v1',
                        UNREAL_HARNESS_LLM_MAX_ATTEMPTS='1',XDG_STATE_HOME=str(self.root/'state'),
                        UACHAT_AUTO_UPDATE='off',UACHAT_NOTIFY='off',UACHAT_RTK='off',SHELL='/bin/bash')

    def tearDown(self):
        self.release_first.set()
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()

    def run_adapter(self,live=False,prompt='Review the fixture.',blocked=False):
        self.requests.clear()
        request = dict(prompt=prompt,session_id='fixture-'+str(time.monotonic_ns()),model='mock',max_attempts=1)
        if blocked: request['disallowed_tools']=['SkillUse']
        payload = dict(type='start',protocol=1,request=request) if live else request
        result = subprocess.run([BINARY,*([] if live else ['--once']),'-workspace',str(self.workspace),'-session-directory',str(self.root/'sessions')],
                                input=json.dumps(payload)+'\n',text=True,capture_output=True,env=self.env,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        return result

    def test_global_skill_in_both_transports_and_original_path(self):
        for live in (False,True):
            with self.subTest(live=live):
                self.run_adapter(live)
                first = json.dumps(self.requests[0])
                self.assertIn('<name>fixture-review</name>',first)
                self.assertIn(str(self.extra/'review/SKILL.md'),first)
                self.assertNotIn('<name>fixture-manual</name>',first)
                self.assertNotIn('BODY_PROBE_789',first)
                self.assertIn('BODY_PROBE_789',json.dumps(self.requests[1:]))
                self.assertIn('RESOURCE_PROBE_789',json.dumps(self.requests[2:]))
                self.assertFalse((self.workspace/'.harness').exists())

    def test_explicit_manual_and_canonical_prompt(self):
        result = self.run_adapter(prompt='$fixture-manual perform task')
        self.assertIn('BODY_MANUAL_789',json.dumps(self.requests[1:]))
        self.assertNotIn('<name>fixture-manual</name>',json.dumps(self.requests[0]))
        stored = '\n'.join(path.read_text() for path in (self.root/'sessions').glob('*.session.jsonl'))
        self.assertNotIn('Explicit skill selection',stored)

    def test_disallowed_skill_tool_removes_catalog(self):
        self.run_adapter(blocked=True)
        request = self.requests[0]
        self.assertNotIn('SkillUse',[tool.get('name') for tool in request.get('tools',[])])
        self.assertNotIn('<available_skills>',json.dumps(request))

    def test_client_one_shot_and_readonly_listing(self):
        self.requests.clear()
        result = subprocess.run([sys.executable,str(ROOT/'uarchat.py'),'-w',str(self.workspace),'--no-color','-p','/fixture-review inspect'],
                                env=self.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr+'\n'+result.stdout)
        self.assertIn('Offline skill loaded.',result.stdout)
        self.assertIn('BODY_PROBE_789',json.dumps(self.requests[1:]))
        self.requests.clear()
        result = subprocess.run([sys.executable,str(ROOT/'uarchat.py'),'-w',str(self.workspace),'--skills','--no-color'],
                                env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('fixture-review',result.stdout)
        self.assertIn(str(self.extra/'review/SKILL.md'),result.stdout)
        self.assertEqual(self.requests,[])

    def test_live_skill_hint_and_manual_supplement(self):
        self.gate['hold']=True
        master,slave=pty.openpty()
        process=subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'-w',str(self.workspace),'--live','on',
                                  '--live-binary',BINARY,'--no-color'],stdin=slave,stdout=slave,stderr=slave,env=self.env,close_fds=True)
        os.close(slave)
        output=b''
        def until(marker,timeout=8):
            nonlocal output
            deadline=time.monotonic()+timeout
            while marker not in output and time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]: output+=os.read(master,65536)
            self.assertIn(marker,output[-20000:])
        try:
            until(b'\x1b[?2004h')
            os.write(master,b'live skill probe\r')
            self.assertTrue(self.first_started.wait(5))
            until('Enter дополнение'.encode())
            os.write(master,b'/fixture-man')
            until(b'Manual only.')
            os.write(master,b'\t supplement\r')
            until('✓ дополнение принято'.encode())
            self.release_first.set()
            until(b'Offline skill loaded.')
            self.assertIn('BODY_MANUAL_789',json.dumps(self.requests))
            stored='\n'.join(path.read_text() for path in (self.root/'state/unreal-agent/sessions').glob('*.session.jsonl'))
            self.assertIn('$fixture-manual supplement',stored)
            self.assertNotIn('Explicit skill selection',stored)
        finally:
            self.release_first.set()
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill();process.wait()
            os.close(master)

    def test_real_pty_skill_completion(self):
        master,slave=pty.openpty()
        process=subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'-w',str(self.workspace),'--live','off','--no-color'],
                                 stdin=slave,stdout=slave,stderr=slave,env=self.env,close_fds=True)
        os.close(slave)
        output=b''
        try:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and b'\x1b[?2004h' not in output:
                if select.select([master],[],[],.1)[0]: output+=os.read(master,65536)
            self.assertIn(b'Tab completes',output)
            os.write(master,b'/fixture-rev\t inspect\r')
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and b'Offline skill loaded.' not in output:
                if select.select([master],[],[],.1)[0]: output+=os.read(master,65536)
            self.assertIn(b'Offline skill loaded.',output)
            stored='\n'.join(path.read_text() for path in (self.root/'state/unreal-agent/sessions').glob('*.session.jsonl'))
            self.assertIn('$fixture-review inspect',stored)
        finally:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill();process.wait()
            os.close(master)


if __name__=='__main__': unittest.main()

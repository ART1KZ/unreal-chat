"""End-to-end CLI tests with the installed runner and an isolated mock backend."""
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mock_responses import sse, message_item, function_call_item, response_obj

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which('unreal-agent-runner'), 'runner binary not installed')
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = self.tmp.name
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('UACHAT_','UNREAL_HARNESS_','OPENAI_','XDG_'))}
        self.env.update(HOME=tmp, UACHAT_WINDOWS_HOME='off', XDG_STATE_HOME=tmp+'/state', UACHAT_PROVIDER='ollama',
                        UNREAL_HARNESS_LLM_PROVIDER='ollama', UNREAL_HARNESS_LLM_MODEL='mock',
                        UNREAL_HARNESS_LLM_MAX_ATTEMPTS='1', UACHAT_AUTO_UPDATE='off', UACHAT_NOTIFY='off')
        if os.environ.get("UACHAT_LIVE_BINARY"):
            self.env["UACHAT_LIVE_BINARY"] = os.environ["UACHAT_LIVE_BINARY"]
        self.requests = []
        requests = self.requests
        class Mock(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(data)
                if 'slow' in json.dumps(data): time.sleep(3)
                follow = any(i.get('type') == 'function_call_output' for i in data.get('input',[]) if isinstance(i,dict))
                output = [message_item('Done. Offline tool roundtrip.')] if follow else [function_call_item('echo offline-tool-output')]
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                try:
                    self.wfile.write(sse({'type':'response.completed','response':response_obj(output)}))
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError: pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Mock)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.env['UNREAL_HARNESS_LLM_BASE_URL'] = f'http://127.0.0.1:{self.server.server_port}/v1'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def command(self,*args):
        return [sys.executable,str(ROOT/'uarchat.py'),'-w',self.tmp.name,'--no-color',*args]

    def test_two_turns_resume_and_no_escape_sequences_in_pipe(self):
        run = subprocess.run(self.command('-s','fixture','--context-window','10000'),
            input='first turn\nsecond turn\n/status\n/exit\n',text=True,capture_output=True,env=self.env,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('offline-tool-output',run.stdout)
        self.assertIn('Done. Offline tool roundtrip.',run.stdout)
        self.assertNotIn('\x1b',run.stdout)
        self.assertTrue((Path(self.tmp.name)/'state/unreal-agent/sessions/fixture.session.jsonl').exists())
        resumed = subprocess.run(self.command('-s','fixture','--context-window','10000'),
            input='/status\n/exit\n',text=True,capture_output=True,env=self.env,timeout=10)
        self.assertEqual(resumed.returncode,0,resumed.stderr)
        self.assertIn('resumed session fixture',resumed.stdout)
        self.assertIn('ctx ~12.9%',resumed.stdout)

    def test_sigint_cancels_running_turn(self):
        proc = subprocess.Popen(self.command('-s','interrupt','-p','slow turn'), stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=self.env)
        try:
            deadline = time.monotonic()+5
            while not self.requests and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue(self.requests)
            proc.send_signal(signal.SIGINT)
            output,error = proc.communicate(timeout=7)
            self.assertEqual(proc.returncode,130,error)
            self.assertIn('interrupted',output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_unreachable_provider_does_not_crash_chat(self):
        self.env['UNREAL_HARNESS_LLM_BASE_URL'] = 'http://127.0.0.1:1/v1'
        run = subprocess.run(self.command(),input='test\n/exit\n',text=True,capture_output=True,env=self.env,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('runner exited with code 1',run.stdout)
        self.assertNotIn('Traceback',run.stderr)


if __name__ == '__main__': unittest.main()

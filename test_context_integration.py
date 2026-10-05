"""Real pinned coordinator, isolated sessions and local Responses server."""
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from mock_responses import sse, message_item, response_obj

BINARY=os.environ.get('UACHAT_LIVE_BINARY') or shutil.which('uachat-live-runner')

@unittest.skipUnless(BINARY,'adapter unavailable')
class ContextIntegrationTests(unittest.TestCase):
    def test_manual_compact_preserves_journal_and_restart_uses_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            requests=[]
            class Server(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_POST(self):
                    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(body)
                    is_summary='Produce a compact continuation checkpoint' in json.dumps(body)
                    text='Goal: keep user task. Progress: completed. Next steps: continue.' if is_summary else 'Mock answer.'
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                    self.wfile.write(sse({'type':'response.completed','response':response_obj([message_item(text)])}))
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Server)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            env={k:v for k,v in os.environ.items() if not k.startswith(('UNREAL_','OPENAI_','UACHAT_'))}
            env.update(HOME=tmp,UACHAT_WINDOWS_HOME='off',UNREAL_HARNESS_LLM_PROVIDER='openai',UNREAL_HARNESS_LLM_API_KEY='test',UNREAL_HARNESS_LLM_BASE_URL=f'http://127.0.0.1:{server.server_port}',UNREAL_HARNESS_LLM_MAX_ATTEMPTS='1')
            def run(extra):
                request={'session_id':'context-fixture','model':'mock','thinking_level':'low','context':{'window':50000,'auto':False,'native':False},**extra}
                result=subprocess.run([BINARY,'--once','-workspace',tmp,'-session-directory',tmp+'/sessions'],input=json.dumps(request),text=True,capture_output=True,env=env,timeout=15)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                return result
            try:
                for n in range(4):run({'prompt':f'Historical request {n}: '+('detail '*250)})
                journal=Path(tmp+'/sessions/context-fixture.session.jsonl')
                original=journal.read_bytes()
                run({'compact_only':True})
                self.assertEqual(journal.read_bytes(),original)
                self.assertTrue(Path(tmp+'/sessions/context/context-fixture/checkpoint.json').exists())
                run({'prompt':'LATEST NEW REQUEST'})
                latest=json.dumps(requests[-1])
                self.assertIn('Historical continuation checkpoint',latest)
                self.assertIn('LATEST NEW REQUEST',latest)
                self.assertNotIn('Historical request 0',latest)
            finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()

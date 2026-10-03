"""Live inbox integration against the real Go coordinator and local mock LLM."""
import http.server
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import editor
from live import Outbox
from mock_responses import sse, message_item, function_call_item, response_obj

ROOT=Path(__file__).resolve().parent
BINARY=os.environ.get('UACHAT_LIVE_BINARY') or shutil.which('uachat-live-runner')


class EditingTests(unittest.TestCase):
    def test_bracketed_multiline_text_does_not_submit(self):
        e=editor.Editor()
        self.assertFalse(e.feed_key('text','first\nsecond'))
        self.assertEqual(e._buffer,'first\nsecond')
        self.assertTrue(e.feed_key('key','enter'))

    def test_outbox_uuid_survives_restart_and_manual_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            box=Outbox(tmp+'/outbox.json');key=box.enqueue('addition')
            second=Outbox(tmp+'/outbox.json')
            self.assertEqual(second.enqueue('addition'),key)
            self.assertEqual(second.accepted(key),'addition')
            self.assertEqual(Outbox(tmp+'/outbox.json').items,{})
            self.assertEqual(os.stat(tmp+'/outbox.json').st_mode & 0o777,0o600)

    def test_corrupt_outbox_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'outbox.json';path.write_text('broken JSON')
            with self.assertRaises(ValueError): Outbox(str(path))
            self.assertEqual(path.read_text(),'broken JSON')


@unittest.skipUnless(BINARY,'live adapter not installed')
class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.work=self.tmp.name
        self.requests=[]
        requests=self.requests
        work=self.work
        class Mock(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                text=json.dumps(request)
                follow=any(i.get('type')=='function_call_output' for i in request.get('input',[]) if isinstance(i,dict))
                if 'addition-marker' in text:
                    output=[message_item('Saw addition-marker in the running session.')]
                elif follow:
                    output=[message_item('Tool completed without supplement.')]
                else:
                    command='echo started > started; sleep 1.2; echo completed > completed; echo tool-output'
                    if 'long-operation' in text:command='echo started > started; sleep 30; echo should-not-exist > completed'
                    output=[function_call_item(command)]
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                try:
                    self.wfile.write(sse({'type':'response.completed','response':response_obj(output,rid='response-'+str(uuid.uuid4()))}))
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Mock)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.env={k:v for k,v in os.environ.items() if not k.startswith(('UACHAT_','UNREAL_HARNESS_','OPENAI_','XDG_'))}
        self.env.update(HOME=self.work,XDG_STATE_HOME=self.work+'/state',UACHAT_PROVIDER='ollama',
            UNREAL_HARNESS_LLM_PROVIDER='ollama',UNREAL_HARNESS_LLM_MODEL='mock',
            UNREAL_HARNESS_LLM_BASE_URL=f'http://127.0.0.1:{self.server.server_port}/v1',
            UNREAL_HARNESS_LLM_MAX_ATTEMPTS='1',UACHAT_AUTO_UPDATE='off',UACHAT_NOTIFY='off',SHELL='/bin/bash')

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()

    def spawn(self):
        proc=subprocess.Popen([BINARY,'-workspace',self.work,'-session-directory',self.work+'/sessions'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1,env=self.env)
        self.addCleanup(lambda:self.cleanup(proc))
        events=__import__('queue').Queue()
        def reader():
            for line in proc.stdout:
                events.put(json.loads(line))
            events.put({'type':'eof'})
        threading.Thread(target=reader,daemon=True).start()
        return proc,events

    @staticmethod
    def cleanup(proc):
        if proc.poll() is None:proc.kill()
        proc.wait(timeout=5)
        if proc.stdin and not proc.stdin.closed:proc.stdin.close()
        if proc.stdout:proc.stdout.close()
        if proc.stderr:proc.stderr.close()

    def send(self,proc,frame):
        proc.stdin.write(json.dumps(frame)+'\n');proc.stdin.flush()

    def wait(self,events,predicate,seconds=8):
        deadline=time.monotonic()+seconds;seen=[]
        while time.monotonic()<deadline:
            event=events.get(timeout=max(.01,deadline-time.monotonic()));seen.append(event)
            if predicate(event):return event
            if event.get('type')=='eof':self.fail('unexpected EOF: '+str(seen[-4:]))
        self.fail('event not found: '+str(seen[-4:]))

    def started(self):
        deadline=time.monotonic()+5
        while not Path(self.work+'/started').exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(Path(self.work+'/started').exists())

    def start(self,proc,prompt='start work',sid='live-test'):
        self.send(proc,{'type':'start','protocol':1,'request':{'prompt':prompt,'session_id':sid,'model':'mock','thinking_level':'high'}})

    def test_input_joins_same_running_coordinator_tool_not_restarted(self):
        proc,events=self.spawn();self.start(proc)
        self.wait(events,lambda e:e.get('type')=='live.ready');self.started()
        identity=str(uuid.uuid4())
        self.send(proc,{'type':'input','id':identity,'text':'addition-marker please use this correction'})
        self.wait(events,lambda e:e.get('type')=='live.accepted' and e.get('id')==identity)
        self.wait(events,lambda e:e.get('type')=='live.finished')
        proc.wait(timeout=5);self.assertEqual(proc.returncode,0)
        self.assertTrue(Path(self.work+'/completed').exists())
        # Only one original tool call, and the same process handled supplementation.
        self.assertGreaterEqual(len(self.requests),2)
        self.assertTrue(any('addition-marker' in json.dumps(request) for request in self.requests[1:]))
        with open(self.work+'/sessions/live-test.session.jsonl') as f:log=f.read()
        self.assertEqual(log.count(identity),1)
        from terminal_ui import item_record
        records=[item_record(json.loads(line)) for line in log.splitlines()]
        calls=[o for rec in records if rec.get('Kind')=='model_response' for o in rec['Data']['Response']['Output'] if o.get('Type')=='tool_call']
        self.assertEqual(len(calls),1)

    def test_duplicate_uuid_is_persisted_once_and_receipted(self):
        proc,events=self.spawn();self.start(proc);self.started()
        identity=str(uuid.uuid4())
        frame={'type':'input','id':identity,'text':'addition-marker'}
        self.send(proc,frame)
        self.wait(events,lambda e:e.get('type')=='live.accepted' and e.get('id')==identity)
        self.send(proc,frame)
        receipt=self.wait(events,lambda e:e.get('type')=='live.accepted' and e.get('id')==identity)
        self.assertTrue(receipt.get('duplicate'))
        self.wait(events,lambda e:e.get('type')=='live.finished');proc.wait(timeout=5)
        with open(self.work+'/sessions/live-test.session.jsonl') as f:log=f.read()
        self.assertEqual(log.count(identity),1)

    def test_multiple_supplements_preserve_order_while_tool_runs(self):
        proc,events=self.spawn();self.start(proc);self.started()
        ids=[str(uuid.uuid4()) for _ in range(3)]
        texts=['addition-marker one','addition-marker two','addition-marker three']
        for key,text in zip(ids,texts):self.send(proc,{'type':'input','id':key,'text':text})
        accepted=[]
        while len(accepted)<3:
            event=self.wait(events,lambda e:e.get('type')=='live.accepted' and e.get('id') in ids)
            accepted.append(event['id'])
        self.assertEqual(accepted,ids)
        self.wait(events,lambda e:e.get('type')=='live.finished');proc.wait(timeout=5)
        from terminal_ui import item_record
        with open(self.work+'/sessions/live-test.session.jsonl') as stream:
            inputs=[r for line in stream if (r:=item_record(json.loads(line))).get('Kind')=='input' and r['Data'].get('Kind')=='external']
        self.assertEqual([r['Data']['ID'] for r in inputs[-3:]],ids)
        self.assertTrue(any(all(text in json.dumps(req) for text in texts) for req in self.requests))

    def test_parent_disappearance_stops_live_worker_and_tool(self):
        owner=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        proc=subprocess.Popen([BINARY,'-workspace',self.work,'-session-directory',self.work+'/sessions',
                               '-parent-pid',str(owner.pid)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=self.env)
        try:
            self.start(proc,'long-operation');self.started()
            owner.terminate();owner.wait(timeout=3)
            proc.wait(timeout=5)
            self.assertFalse(Path(self.work+'/completed').exists())
        finally:
            if owner.poll() is None:owner.kill();owner.wait()
            self.cleanup(proc)

    def test_uuid_dedup_after_restart_uses_canonical_store(self):
        identity=str(uuid.uuid4())
        proc,events=self.spawn();self.start(proc);self.started()
        self.send(proc,{'type':'input','id':identity,'text':'addition-marker'})
        self.wait(events,lambda e:e.get('type')=='live.accepted' and e.get('id')==identity)
        self.wait(events,lambda e:e.get('type')=='live.finished');proc.wait(timeout=5)
        second,again=self.spawn()
        self.send(second,{'type':'start','protocol':1,'request':{'session_id':'live-test','model':'mock',
            'messages':[{'role':'user','content':'addition-marker','message_id':identity}]}})
        receipt=self.wait(again,lambda e:e.get('type')=='live.accepted' and e.get('id')==identity)
        self.assertTrue(receipt.get('duplicate'))
        self.wait(again,lambda e:e.get('type')=='live.finished');second.wait(timeout=5)
        with open(self.work+'/sessions/live-test.session.jsonl') as f:log=f.read()
        self.assertEqual(log.count(identity),1)

    def test_bad_uuid_and_unknown_frames_do_not_corrupt_session(self):
        proc,events=self.spawn();self.start(proc);self.started()
        self.send(proc,{'type':'input','id':'bad','text':'addition-marker'})
        rejected=self.wait(events,lambda e:e.get('type')=='live.rejected')
        self.assertIn('UUID',rejected['message'])
        self.send(proc,{'type':'no-such-command'})
        self.wait(events,lambda e:e.get('type')=='live.rejected')
        self.wait(events,lambda e:e.get('type')=='live.finished');proc.wait(timeout=5)
        self.assertFalse(any('addition-marker' in json.dumps(request) for request in self.requests))

    def test_hard_stop_cancels_tool_without_replay(self):
        proc,events=self.spawn();self.start(proc,'long-operation');self.started()
        self.send(proc,{'type':'stop','mode':'hard'})
        proc.wait(timeout=5)
        self.assertFalse(Path(self.work+'/completed').exists())
        self.assertLessEqual(len(self.requests),2)

    def test_tty_escape_keeps_draft_and_restores_terminal(self):
        import termios
        master,slave=pty.openpty();old=termios.tcgetattr(slave)
        proc=subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'-w',self.work,'--live','on',
                               '--live-binary',BINARY,'--no-color'],stdin=slave,stdout=slave,stderr=slave,env=self.env)
        data=bytearray()
        def until(needle):
            start=len(data);deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if needle in data[start:]:return
                if select.select([master],[],[],.05)[0]:data.extend(os.read(master,65536))
            self.fail(f'PTY missing {needle!r}: {bytes(data[-2000:])!r}')
        try:
            until(b'live inbox:');os.write(master,b'long-operation\r');self.started()
            os.write(master,b'unsent draft')
            until(b'unsent draft')
            os.write(master,b'\x1b')
            until(b'live turn interrupted')
            time.sleep(.2);os.write(master,b'\x15/exit\r');proc.wait(timeout=5)
            self.assertEqual(proc.returncode,0)
            self.assertEqual(termios.tcgetattr(slave),old)
            self.assertFalse(Path(self.work+'/completed').exists())
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
            os.close(master);os.close(slave)

    def test_tty_composer_live_submit_paste_and_terminal_restore(self):
        import termios
        master,slave=pty.openpty();old=termios.tcgetattr(slave)
        proc=subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'-w',self.work,'-s','tty-live','--live','on',
            '--live-binary',BINARY,'--no-color'],stdin=slave,stdout=slave,stderr=slave,env=self.env)
        data=bytearray()
        def until(needle,seconds=8):
            start=len(data);deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                if needle in data[start:]:return
                if select.select([master],[],[],.05)[0]:data.extend(os.read(master,65536))
            self.fail(f'PTY missing {needle!r}: {bytes(data[-2000:])!r}')
        try:
            until(b'live inbox:')
            os.write(master,b'start work\r');self.started()
            os.write(master,b'\x1b[200~addition-marker\nsecond line\x1b[201~')
            time.sleep(.05)
            # A pasted newline is not a submit key; only explicit Enter delivers.
            self.assertFalse(any('addition-marker' in json.dumps(request) for request in self.requests))
            os.write(master,b'\r')
            until('дополнение принято в сессию'.encode())
            until(b'Saw addition-marker')
            # Wait for completed operation and editor handoff before typing /exit.
            deadline=time.monotonic()+5
            while not Path(self.work+'/completed').exists() and time.monotonic()<deadline:time.sleep(.01)
            time.sleep(.4)
            os.write(master,b'/exit\r');proc.wait(timeout=5)
            self.assertEqual(proc.returncode,0)
            self.assertEqual(termios.tcgetattr(slave),old)
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
            os.close(master);os.close(slave)


if __name__=='__main__':unittest.main()

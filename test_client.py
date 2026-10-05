"""Isolated client/footer/security regressions (stdlib only, no real auth)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import configio
import editor
import models
import terminal_ui as ui
import uarchat as client

ROOT = Path(__file__).resolve().parent


class StatusTests(unittest.TestCase):
    def test_context_latest_not_cumulative_or_cache_subtracted(self):
        m = ui.SessionMetrics()
        m.observe({'InputTokens':1000, 'OutputTokens':100, 'CachedInputTokens':900})
        m.observe({'InputTokens':1200, 'OutputTokens':200})
        self.assertEqual(m.context_tokens,1400)
        self.assertEqual(m.input_tokens,2200)
        self.assertIn('~14.0%',m.context_label(10000))
        self.assertIn('/ ?',m.context_label(None))
        m.reset()
        self.assertEqual(m.context_label(10000),'ctx —/10.0k')

    def test_metadata_only_no_fictional_model_limits(self):
        self.assertIsNone(models.context_window('test','unknown-model'))
        models.MODEL_METADATA[('test','model')] = {'context_length':123456}
        self.assertEqual(models.context_window('test','model'),123456)
        del models.MODEL_METADATA[('test','model')]

    def test_footer_actual_bottom_and_restores_cursor_after_region(self):
        output = io.StringIO()
        output.isatty = lambda:True
        with patch('sys.stdout',output), patch('terminal_ui.terminal_size',return_value=os.terminal_size((40,8))):
            footer = ui.TerminalFooter(lambda:'model · high · ctx ?')
            footer.start()
            self.assertEqual(footer.reserved_rows(),1)
            self.assertIn('\x1b[8;1H',output.getvalue())
            self.assertIn('\x1b[1;7r',output.getvalue())
            footer.close()
            self.assertTrue(output.getvalue().endswith('\x1b[r\x1b8\x1b[?25h'))

    def test_footer_resize_and_tiny_screen(self):
        output = io.StringIO()
        output.isatty = lambda:True
        with patch('sys.stdout',output), patch('terminal_ui.terminal_size') as size:
            size.return_value = os.terminal_size((80,24))
            footer = ui.TerminalFooter(lambda:'model · high · ctx ?')
            footer.start()
            size.return_value = os.terminal_size((20,3))
            footer.draw()
            self.assertEqual(footer.reserved_rows(),0)
            self.assertIn('\x1b7\x1b[r\x1b8',output.getvalue())
            footer.close()

    def test_display_width_wrapping(self):
        lines = list(ui.wrap_text('你你你好 world',6))
        self.assertTrue(all(editor._plain_width(line) <= 6 for line in lines))
        self.assertEqual(list(ui.wrap_text('    code\nnext',20)),['    code','next'])

    def test_safe_terminal_text(self):
        text = 'hello\x1b]52;c;attacker\x07\x1b[2J\x1b[31mworld\x1b[0m\x00\x9b'
        self.assertEqual(ui.safe_text(text),'helloworld')
        theme = client.Theme('mono',False)
        self.assertEqual(theme.paint(text,'dim'),'helloworld')


class ClientTests(unittest.TestCase):
    def env(self, tmp):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('UACHAT_','UNREAL_HARNESS_','OPENAI_','XDG_'))}
        env.update(HOME=tmp, UACHAT_WINDOWS_HOME='off', XDG_STATE_HOME=tmp+'/state', UACHAT_PROVIDER='ollama',
                   UNREAL_HARNESS_LLM_PROVIDER='ollama', UACHAT_AUTO_UPDATE='off', UACHAT_NOTIFY='off',
                   UNREAL_HARNESS_LLM_BASE_URL='http://127.0.0.1:1/v1', REQUEST_LOG=tmp+'/requests.jsonl')
        return env

    def runner(self, tmp):
        path = Path(tmp)/'runner'
        path.write_text('''#!/usr/bin/env python3
import json,os,sys,time
request=json.load(sys.stdin)
with open(os.environ['REQUEST_LOG'],'a') as f: f.write(json.dumps(request)+'\\n')
if request['prompt']=='slow': time.sleep(.8)
if request['prompt']=='bad-records':
    print('[1,2]')
    print(json.dumps({'Kind':'model_response','Data':{'Response':{'Output':[None, {'Type':'reasoning','Data':{'Summary':[1]}}], 'Usage':{'InputTokens':'bad'}}}}))
print(json.dumps({'Kind':'model_response','Data':{'Response':{'Output':[{'Type':'message','Data':{'Text':'offline answer'}}], 'Usage':{'InputTokens':1000,'OutputTokens':100,'CachedInputTokens':800}}}}))
''')
        path.chmod(0o700)
        return str(path)

    def test_cli_flags_do_not_override_slash_state_and_status_is_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.env(tmp)
            run = subprocess.run([sys.executable,str(ROOT/'uarchat.py'),'--binary',self.runner(tmp),
                '-m','original','-t','low','--context-window','10000','--no-color'],
                input='/model changed\n/thinking max\nhello\n/status\n/exit\n',text=True,capture_output=True,env=env,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            request = json.loads((Path(tmp)/'requests.jsonl').read_text())
            self.assertEqual(request['model'],'changed')
            self.assertEqual(request['thinking_level'],'max')
            self.assertIn('ctx ~11.0%',run.stdout)
            self.assertNotIn('\x1b',run.stdout)
            self.assertNotIn('  endpoint ',run.stdout)
            self.assertNotIn('  model ',run.stdout)

    def test_malformed_runner_events_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable,str(ROOT/'uarchat.py'),'--binary',self.runner(tmp),'-p','bad-records','--no-color'],
                capture_output=True,text=True,env=self.env(tmp),timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('offline answer',run.stdout)

    def test_bad_workspace_session_provider_and_context_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for options in (['-s','../escape'],['-w',tmp+'/missing'],['--provider','bad'],['--context-window','-1']):
                run = subprocess.run([sys.executable,str(ROOT/'uarchat.py'),*options,'-p','test'],
                    capture_output=True,text=True,env=self.env(tmp),timeout=10)
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertNotIn('Traceback',run.stderr)

    def test_unknown_slash_does_not_match_command_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable,str(ROOT/'uarchat.py'),'--binary',self.runner(tmp)],
                input='/modelxyz bad\n/exit\n',text=True,capture_output=True,env=self.env(tmp),timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('unknown command',run.stdout)
            self.assertFalse((Path(tmp)/'requests.jsonl').exists())

    def test_replay_bounded_export_complete_and_private(self):
        with tempfile.TemporaryDirectory() as tmp, patch('uarchat.SESSION_DIR',tmp), patch('uarchat.STATE_DIR',tmp):
            with open(client.session_path('fixture'),'w') as stream:
                stream.write('not JSON\n[1]\n')
                for index in range(30):
                    for record in ({'Kind':'input','Data':{'Kind':'external','Payload':f'user-{index}'}},
                                   {'Kind':'model_response','Data':{'Response':{'Output':[{'Type':'message','Data':{'Text':f'answer-{index}'}}], 'Usage':{'InputTokens':index*100,'OutputTokens':10}}}}):
                        stream.write(json.dumps({'type':'item','data':{'Item':record}})+'\n')
            with patch('sys.stdout',new_callable=io.StringIO):
                entries = client.replay_session('fixture',client.Theme('mono',False))
            self.assertEqual(len(entries),10)
            metrics = ui.SessionMetrics()
            metrics.load(client.session_path('fixture'))
            self.assertEqual(metrics.context_tokens,2910)
            path = client.dump_transcript('fixture',entries)
            dump = Path(path).read_text()
            self.assertIn('answer-0',dump)
            self.assertIn('answer-29',dump)
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)

    def test_raw_editor_does_not_register_readline_history_writer(self):
        with patch('uarchat.editor_module.AVAILABLE',True), patch('uarchat.atexit',create=True) as unused:
            with patch('uarchat.readline.read_history_file') as read:
                client.setup_readline(client.Theme('mono',False))
                read.assert_not_called()

    def test_atomic_config_quoting_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp+'/env'
            Path(path).write_text('UACHAT_THEME="nord"\nA=old\nA=duplicate\n')
            configio.save_value(path,'A','new')
            self.assertEqual(models.load_env(path),{'UACHAT_THEME':'nord','A':'new'})
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            with self.assertRaises(ValueError): configio.save_value(path,'A','bad\nKEY=other')

    def test_history_merge_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp+'/history'
            first, second = editor.Editor(path), editor.Editor(path)
            first._remember('first\nline')
            second._remember('second')
            first._remember('third')
            self.assertEqual(editor.Editor(path).history,['first\nline','second','third'])



class ClientTTYTests(unittest.TestCase):
    def test_footer_updates_resize_and_terminal_restoration(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        helper = ClientTests()
        with tempfile.TemporaryDirectory() as tmp:
            master, slave = pty.openpty()
            original = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH',12,60,0,0))
            proc = subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'--binary',helper.runner(tmp),
                                    '-m','original','-t','low','--context-window','10000','--no-color'],
                                    stdin=slave,stdout=slave,stderr=slave,env=helper.env(tmp))
            data = bytearray()
            def until(needle,timeout=5):
                start = len(data)
                deadline = time.monotonic()+timeout
                while time.monotonic()<deadline:
                    if needle in data[start:]: return bytes(data[start:])
                    if select.select([master],[],[],.05)[0]:
                        try: chunk = os.read(master,65536)
                        except OSError: break
                        if not chunk: break
                        data.extend(chunk)
                self.fail(f'PTY missing {needle!r}: {bytes(data[-2000:])!r}')
            try:
                until('ctx —/10.0k'.encode())
                self.assertIn(b'\x1b[12;1H',data)
                os.write(master,b'/thinking max\r')
                until('◉ max'.encode())
                os.write(master,b'hello\r')
                until(b'ctx ~11.0%')
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',7,45,0,0))
                until(b'\x1b[7;1H')
                os.write(master,b'/exit\r')
                proc.wait(timeout=5)
                self.assertEqual(proc.returncode,0)
                self.assertEqual(termios.tcgetattr(slave),original)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)
                os.close(slave)

    def test_sigterm_restores_raw_terminal_and_footer(self):
        import pty
        import select
        import signal
        import termios
        import time
        helper = ClientTests()
        with tempfile.TemporaryDirectory() as tmp:
            master, slave = pty.openpty()
            original = termios.tcgetattr(slave)
            proc = subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'--no-color'],
                                    stdin=slave,stdout=slave,stderr=slave,env=helper.env(tmp))
            data = bytearray()
            try:
                deadline = time.monotonic()+5
                while b'ctx ?' not in data and time.monotonic()<deadline:
                    if select.select([master],[],[],.05)[0]: data.extend(os.read(master,65536))
                self.assertIn(b'ctx ?',data)
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
                self.assertEqual(proc.returncode,143)
                self.assertEqual(termios.tcgetattr(slave),original)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)
                os.close(slave)

    def test_paste_during_turn_is_reviewable_draft_not_lost_or_sent(self):
        import pty
        import select
        import time
        helper = ClientTests()
        with tempfile.TemporaryDirectory() as tmp:
            master, slave = pty.openpty()
            proc = subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'--binary',helper.runner(tmp),'--no-color'],
                                    stdin=slave,stdout=slave,stderr=slave,env=helper.env(tmp))
            data = bytearray()
            def until(needle):
                start = len(data)
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    if needle in data[start:]: return
                    if select.select([master],[],[],.05)[0]: data.extend(os.read(master,65536))
                self.fail(f'PTY missing {needle!r}: {bytes(data[-1000:])!r}')
            try:
                until(b'ctx ?')
                os.write(master,b'slow\r')
                until(b'Esc to interrupt')
                os.write(master,b'\x1b[200~next\nline\x1b[201~')
                until(b'  line')
                os.write(master,b'\x15/exit\r')
                proc.wait(timeout=5)
                self.assertEqual(proc.returncode,0)
                requests = (Path(tmp)/'requests.jsonl').read_text().splitlines()
                self.assertEqual(len(requests),1)
                self.assertEqual(json.loads(requests[0])['prompt'],'slow')
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)
                os.close(slave)


if __name__ == '__main__': unittest.main()

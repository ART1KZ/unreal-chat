"""Multi-process leases, transactional login, identity and read-only diagnostics."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_auth
import codex_pool as pool
import diagnostics
import native_auth
import uarchat
from auth_ui import AuthUI
from configio import file_lock, LockBusyError
from test_auth_pool import token, quota
from uarchat import Theme


class AuthHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name+'/auth.json'
        self.patch = patch('codex_auth.CODEX_AUTH_PATH',self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def account(self,name):
        payload = native_auth.build_payload({'access_token':token(name),'refresh_token':'test-'+name})
        return pool.register(payload)

    def holder(self,path):
        proc = subprocess.Popen([sys.executable,'-c',
            "import os,fcntl,sys,time; f=os.open(sys.argv[1]+'.lock',os.O_RDWR|os.O_CREAT,0o600); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)",path],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.assertEqual(proc.stdout.readline().strip(),'ready')
        self.addCleanup(lambda: self.stop(proc))
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None: proc.terminate()
        proc.communicate(timeout=3)

    def test_busy_account_in_other_process_rotates_to_free_sibling(self):
        first,second = self.account('first'),self.account('second')
        self.holder(pool.account_path(second)+'.lease')
        started = time.monotonic()
        with patch('codex_pool.fetch_usage',return_value=quota(10)) as fetch:
            with pool.turn('session') as (snapshot,info):
                self.assertEqual(info['key'],first)
                self.assertTrue(info['rotated'])
                self.assertEqual(fetch.call_count,1)
        self.assertLess(time.monotonic()-started,2)

    def test_busy_pinned_account_fails_fast_without_network(self):
        key = self.account('first'); pool.set_rotation(False)
        self.holder(pool.account_path(key)+'.lease')
        with patch('codex_pool.fetch_usage') as fetch:
            with self.assertRaises(LockBusyError):
                with pool.turn('session'): self.fail('turn must not launch')
            fetch.assert_not_called()

    def test_recent_cached_usage_is_available_during_another_turn(self):
        key = self.account('first')
        pool._mark(key,usage=quota(25),checked_at=time.time())
        self.holder(pool.account_path(key)+'.lease')
        with patch('codex_pool.fetch_usage') as fetch:
            self.assertEqual(pool.usage_for(key)['rate_limit']['primary_window']['used_percent'],25)
            with self.assertRaises(LockBusyError): pool.usage_for(key,refresh=True)
            fetch.assert_not_called()

    def test_timed_file_lock_does_not_wait_indefinitely(self):
        self.holder(self.tmp.name+'/resource')
        started = time.monotonic()
        with self.assertRaises(LockBusyError):
            with file_lock(self.tmp.name+'/resource',timeout=.05): pass
        self.assertLess(time.monotonic()-started,1)

    def test_failed_registration_does_not_overwrite_compatible_active_credentials(self):
        key = self.account('first')
        before = Path(self.path).read_bytes()
        with patch('native_auth.browser_login',return_value={'access_token':token('second'),'refresh_token':'new-test'}), patch('codex_pool._write',side_effect=OSError('disk full')), patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(native_auth.main(['login'],theme=Theme('mono',False)),1)
        self.assertEqual(Path(self.path).read_bytes(),before)
        self.assertEqual(pool.resolve(),key)

    def test_refresh_different_account_and_user_leave_original_file_intact(self):
        key = self.account('first')
        path = pool.account_path(key)
        before = Path(path).read_bytes()
        wrong_user = token('first').split('.')
        import base64
        claims = json.loads(base64.urlsafe_b64decode(wrong_user[1]+'='*(-len(wrong_user[1])%4)))
        claims['sub'] = 'different-user'
        wrong_user[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        for access in (token('second'),'.'.join(wrong_user)):
            with patch('native_auth._post',return_value={'access_token':access,'refresh_token':'wrong-owner-test'}):
                with self.assertRaises(ValueError): native_auth.ensure_fresh(path,force=True)
            self.assertEqual(Path(path).read_bytes(),before)

    def test_malformed_refresh_token_field_is_rejected_before_write(self):
        key = self.account('first'); path = pool.account_path(key)
        before = Path(path).read_bytes()
        with patch('native_auth._post',return_value={'access_token':token('first'),'refresh_token':['not','a','token']}):
            with self.assertRaises(ValueError): native_auth.ensure_fresh(path,force=True)
        self.assertEqual(Path(path).read_bytes(),before)

    def test_refresh_failure_is_reported_without_secret_error_body(self):
        key = self.account('first')
        with patch('native_auth.ensure_fresh',side_effect=ValueError('secret must not be recorded')):
            with self.assertRaises(ValueError):
                with pool.turn('session'): pass
        data,_ = pool.entries()
        self.assertEqual(data['accounts'][key]['last_error']['kind'],'refresh')
        self.assertNotIn('secret',json.dumps(data))
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            pool.show_accounts(AuthUI(theme=Theme('mono',False)))
            self.assertIn('refresh не удался',output.getvalue())

    def test_cached_session_status_is_owned_by_session_not_global_active(self):
        first,second = self.account('first'),self.account('second')
        with file_lock(pool.paths()[1]):
            data = pool._read()
            data['sessions']['mine'] = first
            data['accounts'][first].update(usage=quota(25),checked_at=time.time())
            data['accounts'][second].update(usage=quota(70),checked_at=time.time())
            pool._write(data)
        self.assertEqual(pool.cached_status('mine')['key'],first)
        self.assertEqual(pool.cached_status('other')['key'],second)
        pool.note_error(first,'usage_limit_reached','model-a')
        self.assertTrue(pool.cached_status('mine','model-a')['limited'])
        self.assertFalse(pool.cached_status('mine','model-b')['limited'])
        with patch('codex_pool.fetch_usage',return_value=quota(20)):
            pool.usage_for(first,refresh=True)
        self.assertFalse(pool.cached_status('mine','model-a')['limited'])

    def test_same_session_in_other_process_never_launches_runner(self):
        self.holder(self.tmp.name+'/session.client')
        env = {'PATH':os.environ['PATH'],'HOME':self.tmp.name,'UACHAT_AUTO_UPDATE':'off',
               'UACHAT_PROVIDER':'ollama','UNREAL_HARNESS_LLM_PROVIDER':'ollama',
               'UNREAL_HARNESS_LLM_BASE_URL':'http://127.0.0.1:1/v1'}
        with patch.dict(os.environ,env,clear=True),patch('uarchat.load_config',return_value={}),patch('uarchat.STATE_DIR',self.tmp.name),patch('uarchat.SESSION_DIR',self.tmp.name),patch('uarchat.run_turn') as runner,patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(uarchat.main(['-s','session','-w',self.tmp.name,'-p','do work','--no-color']),1)
            runner.assert_not_called()
            self.assertIn('сессия уже выполняет ход',output.getvalue())

    def test_preflight_rejection_keeps_editable_draft_and_never_runs_tools(self):
        import pty
        import select
        from test_client import ClientTests, ROOT
        helper = ClientTests()
        folder = Path(self.tmp.name)/'state/unreal-agent/sessions'
        folder.mkdir(parents=True)
        self.holder(str(folder/'shared.client'))
        master,slave = pty.openpty()
        proc = subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'--binary',helper.runner(self.tmp.name),
                                 '-s','shared','--no-color'],stdin=slave,stdout=slave,stderr=slave,env=helper.env(self.tmp.name))
        data = bytearray()
        def until(needle):
            start = len(data)
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                if needle in data[start:]: return
                if select.select([master],[],[],.05)[0]: data.extend(os.read(master,65536))
            self.fail(f'PTY missing {needle!r}: {bytes(data[-1500:])!r}')
        try:
            until(b'ctx ?')
            os.write(master,b'keep this draft\r')
            until('Текст сохранён в черновике'.encode())
            # Repaint can arrive in the same read as the error message.
            self.assertIn(b'keep this draft',data)
            os.write(master,b'\x15/exit\r')
            proc.wait(timeout=5)
            self.assertEqual(proc.returncode,0)
            self.assertFalse((Path(self.tmp.name)/'requests.jsonl').exists())
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            os.close(master); os.close(slave)

    def test_idle_footer_picks_up_external_quota_cache_change_without_network(self):
        import pty
        import select
        from test_client import ClientTests, ROOT
        helper = ClientTests()
        canonical = self.tmp.name+'/.config/uachat/codex/auth.json'
        with patch('codex_auth.CODEX_AUTH_PATH',canonical):
            key = self.account('first')
            pool._mark(key,usage=quota(25),checked_at=time.time())
            env = helper.env(self.tmp.name)
            env.update(UACHAT_PROVIDER='openai-codex',UNREAL_HARNESS_LLM_PROVIDER='openai-codex')
            master,slave = pty.openpty()
            proc = subprocess.Popen([sys.executable,str(ROOT/'uarchat.py'),'-m','gpt-test','--no-color'],
                                    stdin=slave,stdout=slave,stderr=slave,env=env)
            data = bytearray()
            def until(needle):
                start = len(data); deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    if needle in data[start:]: return
                    if select.select([master],[],[],.05)[0]: data.extend(os.read(master,65536))
                self.fail(f'PTY missing {needle!r}: {bytes(data[-1500:])!r}')
            try:
                until(b'5h 75% left')
                pool._mark(key,usage=quota(80),checked_at=time.time())
                until(b'5h 20% left')
                os.write(master,b'/exit\r')
                proc.wait(timeout=5)
                self.assertEqual(proc.returncode,0)
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
                os.close(master); os.close(slave)

    def test_doctor_is_read_only_and_never_leaks_tokens_or_proxy_credentials(self):
        key = self.account('first')
        before = {str(path):path.read_bytes() for path in Path(self.tmp.name).rglob('*') if path.is_file()}
        with patch('diagnostics.callback_port',return_value=True),patch('auth_ui.open_browser',side_effect=AssertionError('no browser')),patch('native_auth.ensure_fresh',side_effect=AssertionError('no refresh')),patch('codex_pool.fetch_usage',side_effect=AssertionError('no HTTP')),patch.dict(os.environ,{'HTTPS_PROXY':'http://private-user:private-password@proxy.invalid'}),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(native_auth.main(['doctor'],theme=Theme('mono',False)),0)
            text = output.getvalue()
            self.assertIn('без сетевых запросов',text)
            self.assertNotIn('private-password',text)
            self.assertNotIn('test-first',text)
            stored = codex_auth._read_json(pool.account_path(key))['tokens']['access_token']
            self.assertNotIn(stored,text)
        after = {str(path):path.read_bytes() for path in Path(self.tmp.name).rglob('*') if path.is_file()}
        self.assertEqual(before,after)

    def test_corrupt_selection_shape_is_sanitized_without_losing_accounts(self):
        key = self.account('first')
        with file_lock(pool.paths()[1]):
            data = pool._read(); data['active'] = []; data['sessions'] = {'broken':[]}
            pool._write(data)
        self.assertEqual(pool.resolve(),key)
        self.assertEqual(pool.cached_status('broken')['key'],key)


if __name__ == '__main__': unittest.main()

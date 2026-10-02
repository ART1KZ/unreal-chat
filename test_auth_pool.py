"""Browser/WSL presentation, real quota schema and multi-account rotation tests."""
import base64
import io
import json
import os
import tempfile
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import auth_ui
import codex_auth
import codex_pool as pool
import native_auth
from uarchat import Theme


def token(account, expires=None):
    claims = {'exp':expires or time.time()+3600,'sub':'user-'+account,'email':account+'@test.invalid',
              'https://api.openai.com/auth':{'chatgpt_account_id':account}}
    return 'test.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.test'


def quota(used,reset=None):
    return {'plan_type':'plus','rate_limit':{'allowed':used<100,'limit_reached':used>=100,
        'primary_window':{'used_percent':used,'limit_window_seconds':18000,'reset_at':reset or int(time.time()+3600)},
        'secondary_window':{'used_percent':30,'limit_window_seconds':604800,'reset_at':int(time.time()+86400)}},
        'credits':{'has_credits':False,'unlimited':False,'balance':'0'}}


class BrowserTests(unittest.TestCase):
    def test_wsl_uses_windows_powershell_safely_and_quietly(self):
        url = 'https://auth.openai.com/oauth/authorize?state=abc&code=def'
        with patch('auth_ui.is_wsl',return_value=True), patch('auth_ui.shutil.which',side_effect=lambda name:'powershell.exe' if name=='powershell.exe' else None), patch('auth_ui.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(auth_ui.open_browser(url),'Windows browser')
            args = run.call_args.args[0]
            self.assertEqual(args[0],'powershell.exe')
            self.assertIn('-EncodedCommand',args)
            script = base64.b64decode(args[-1]).decode('utf-16le')
            self.assertNotIn(url,script)  # no executable URL interpolation or shell
            self.assertIn(base64.b64encode(url.encode()).decode(),script)
            self.assertEqual(run.call_args.kwargs['stderr'],auth_ui.subprocess.DEVNULL)

    def test_failed_opener_is_friendly_without_gio_or_fake_success(self):
        with patch('auth_ui.is_wsl',return_value=True), patch('auth_ui.shutil.which',return_value=None), patch('sys.stdout',new_callable=io.StringIO) as output:
            ui = auth_ui.AuthUI(theme=Theme('nord',True))
            self.assertIsNone(ui.browser('https://auth.openai.com/test','Codex'))
            self.assertIn('вручную',output.getvalue())
            self.assertIn('\x1b[',output.getvalue())
            self.assertNotIn('gio:',output.getvalue())
            self.assertIn('--headless',output.getvalue())

    def test_successful_tty_opener_shows_short_clickable_link(self):
        output = io.StringIO(); output.isatty = lambda:True
        with patch('auth_ui.open_browser',return_value='Windows browser'), patch('sys.stdout',output), patch.dict(os.environ,{'TERM':'xterm'}):
            auth_ui.AuthUI(theme=Theme('nord',True)).browser('https://auth.openai.com/test?state=one','Codex')
            self.assertIn('Открыть страницу',output.getvalue())
            self.assertIn('\x1b]8;;https://',output.getvalue())
            self.assertEqual(output.getvalue().count('https://auth.openai.com/test?state=one'),1)

    def test_non_tty_no_colors_or_osc(self):
        with patch('sys.stdout',new_callable=io.StringIO) as output, patch('auth_ui.open_browser',return_value=None):
            auth_ui.AuthUI(theme=Theme('nord',False)).browser('https://auth.openai.com/test','Codex')
            self.assertNotIn('\x1b',output.getvalue())


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name+'/auth.json'
        self.patch = patch('codex_auth.CODEX_AUTH_PATH',self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def account(self,name):
        native_auth._save({'access_token':token(name),'refresh_token':'test-refresh-'+name},self.path)
        return pool.register(codex_auth._read_json(self.path))

    def test_second_login_keeps_first_account(self):
        first,second = self.account('first'), self.account('second')
        data,entries = pool.entries()
        self.assertEqual(len(entries),2)
        self.assertEqual(data['active'],second)
        self.assertTrue(Path(pool.account_path(first)).exists())
        self.assertEqual(os.stat(pool.paths()[1]).st_mode & 0o777,0o600)

    def test_login_same_identity_with_new_id_token_does_not_duplicate(self):
        key = self.account('first')
        payload = codex_auth._read_json(pool.account_path(key))
        claims = {'sub':'id-token-subject','email':'first@test.invalid'}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        payload['tokens']['id_token'] = 'test.'+encoded+'.test'
        self.assertEqual(pool.register(payload),key)
        self.assertEqual(len(pool.entries()[1]),1)

    def test_rotation_sticky_session_and_private_snapshot_not_shared_file(self):
        first,second = self.account('first'),self.account('second')
        def usage(payload):
            return quota(100 if payload['tokens']['account_id']=='second' else 40)
        with patch('codex_pool.fetch_usage',side_effect=usage):
            with pool.turn('session','gpt-test') as (snapshot,info):
                self.assertEqual(info['key'],first)
                self.assertTrue(info['rotated'])
                self.assertNotEqual(snapshot,self.path)
                self.assertEqual(os.stat(snapshot).st_mode & 0o777,0o600)
                self.assertEqual(codex_auth._read_json(snapshot)['tokens']['account_id'],'first')
            self.assertFalse(Path(snapshot).exists())
            with pool.turn('session','gpt-test') as (_,info): self.assertEqual(info['key'],first)
            self.assertEqual(codex_auth._read_json(self.path)['tokens']['account_id'],'first')

    def test_orphan_cleanup_does_not_delete_live_turn_credentials(self):
        directory = self.tmp.name+'/runtime'
        os.makedirs(directory)
        Path(directory+'/turn-54321-dead.json').write_text('test')
        Path(directory+'/turn-12345-live.json').write_text('test')
        def check(pid,signal):
            if pid == 54321: raise ProcessLookupError
        with patch('codex_pool.os.kill',side_effect=check): pool.cleanup_runtime(directory)
        self.assertFalse(Path(directory+'/turn-54321-dead.json').exists())
        self.assertTrue(Path(directory+'/turn-12345-live.json').exists())

    def test_rotation_off_and_all_exhausted_do_not_start_turn(self):
        self.account('first'); self.account('second'); pool.set_rotation(False)
        with patch('codex_pool.fetch_usage',return_value=quota(100)):
            with self.assertRaises(ValueError):
                with pool.turn('session'): self.fail('exhausted turn must not run')
        self.assertFalse(list(Path(self.tmp.name).glob('runtime/*.json')))

    def test_usage_unavailable_not_invented_exhaustion(self):
        active = self.account('first')
        with patch('codex_pool.fetch_usage',side_effect=OSError('offline')):
            with pool.turn('session') as (_,info):
                self.assertEqual(info['key'],active)
                self.assertIsNotNone(info['usage_error'])
                self.assertEqual(pool.quota_label(info['usage']),'quota ?')

    def test_reset_invalidates_fresh_cache_and_limit_changes_are_used(self):
        key = self.account('first')
        pool._mark(key,usage=quota(100,reset=int(time.time()-1)),checked_at=time.time()-5)
        with patch('codex_pool.fetch_usage',return_value=quota(5)) as fetch:
            with pool.turn('session') as (_,info):
                self.assertIsNone(pool.block_until(info['usage']))
            self.assertEqual(fetch.call_count,1)

    def test_backend_allowed_true_does_not_invent_block_at_100_percent(self):
        usage = quota(100)
        usage['rate_limit']['allowed'] = True
        usage['rate_limit']['limit_reached'] = False
        self.assertIsNone(pool.block_until(usage,'gpt-test'))

    def test_unrelated_model_quota_does_not_rotate(self):
        usage = quota(30)
        usage['additional_rate_limits'] = [{'limit_name':'other-model','rate_limit':quota(100)['rate_limit']}]
        self.assertIsNone(pool.block_until(usage,'gpt-test'))
        self.assertIsNotNone(pool.block_until(usage,'other-model'))

    def test_generic_403_or_429_does_not_mark_quota_exhausted(self):
        key = self.account('first')
        self.assertFalse(pool.note_error(key,'HTTP 403 unsupported country'))
        self.assertFalse(pool.note_error(key,'HTTP 429 too many requests'))
        self.assertTrue(pool.note_error(key,'usage_limit_reached'))

    def test_runner_token_refresh_saved_back_under_account_lease(self):
        key = self.account('first')
        with patch('codex_pool.fetch_usage',return_value=quota(30)):
            with pool.turn('session') as (snapshot,_):
                payload = codex_auth._read_json(snapshot)
                payload['tokens']['refresh_token'] = 'rotated-by-runner'
                payload['tokens']['access_token'] = token('first',time.time()+7200)
                codex_auth._write_private_json(snapshot,payload)
        self.assertEqual(codex_auth._read_json(pool.account_path(key))['tokens']['refresh_token'],'rotated-by-runner')

    def test_usage_display_and_headers_no_token_leak(self):
        key = self.account('first')
        with patch('codex_pool.urlopen',return_value=io.BytesIO(json.dumps(quota(25)).encode())) as fetch:
            payload = codex_auth._read_json(pool.account_path(key))
            self.assertEqual(pool.fetch_usage(payload)['plan_type'],'plus')
            req = fetch.call_args.args[0]
            self.assertEqual(req.full_url,pool.USAGE_URL)
            self.assertEqual(req.get_header('Chatgpt-account-id'),'first')
        with patch('codex_pool.fetch_usage',return_value=quota(25)),patch('sys.stdout',new_callable=io.StringIO) as output:
            pool.show_usage(auth_ui.AuthUI(theme=Theme('nord',False)),all_accounts=True)
            self.assertIn('5h',output.getvalue())
            self.assertIn('7d',output.getvalue())
            self.assertIn('осталось 75%',output.getvalue())
            self.assertNotIn(payload['tokens']['access_token'],output.getvalue())
            self.assertNotIn('test-refresh-',output.getvalue())

    def test_quota_cli_integration_does_not_replay_failed_turn(self):
        import uarchat
        first,second = self.account('first'),self.account('second')
        log = self.tmp.name+'/requests'
        runner = Path(self.tmp.name)/'fake-runner'
        runner.write_text("""#!/usr/bin/env python3
import json,os,sys
request=json.load(sys.stdin)
with open(os.environ['OPENAI_CODEX_AUTH_FILE']) as f: account=json.load(f)['tokens']['account_id']
with open(os.environ['POOL_TEST_LOG'],'a') as f: f.write(account+'\\n')
if account=='second':
    print(json.dumps({'type':'error','message':'usage_limit_reached'}))
    sys.exit(1)
print(json.dumps({'Kind':'model_response','Data':{'Response':{'Output':[{'Type':'message','Data':{'Text':'success with first'}}]}}}))
""")
        runner.chmod(0o700)
        calls = []
        def usage(payload):
            account = payload['tokens']['account_id']
            calls.append(account)
            return quota(100 if account=='second' and calls.count('second')>1 else 10)
        env = {'PATH':os.environ['PATH'],'HOME':self.tmp.name,'UACHAT_AUTO_UPDATE':'off','UACHAT_NOTIFY':'off','POOL_TEST_LOG':log}
        with patch.dict(os.environ,env,clear=True), patch('uarchat.load_config',return_value={}), patch('uarchat.STATE_DIR',self.tmp.name), patch('uarchat.SESSION_DIR',self.tmp.name+'/sessions'), patch('codex_pool.fetch_usage',side_effect=usage), patch('sys.stdout',new_callable=io.StringIO):
            args = ['--provider','openai-codex','--binary',str(runner),'-w',self.tmp.name,'-s','safe-retry','-p','do work','--no-color']
            self.assertEqual(uarchat.main(args),1)
            self.assertEqual(Path(log).read_text().splitlines(),['second'])
            # A fresh CLI process does not inherit env mutations made by main().
            os.environ.pop('UNREAL_HARNESS_LLM_BASE_URL',None)
            self.assertEqual(uarchat.main(args),0)
            self.assertEqual(Path(log).read_text().splitlines(),['second','first'])

    def test_usage_401_refreshes_once_and_snapshot_uses_new_token(self):
        import urllib.error
        key = self.account('first')
        error = urllib.error.HTTPError(pool.USAGE_URL,401,'expired',{},io.BytesIO())
        fresh = token('first',time.time()+7200)
        with patch('codex_pool.fetch_usage',side_effect=[error,quota(20)]) as fetch, patch('native_auth._post',return_value={'access_token':fresh,'refresh_token':'after-401'}):
            with pool.turn('session') as (snapshot,_):
                self.assertEqual(codex_auth._read_json(snapshot)['tokens']['access_token'],fresh)
            self.assertEqual(fetch.call_count,2)

    def test_logout_one_keeps_siblings_logout_all_removes_private_credentials(self):
        first,second = self.account('first'),self.account('second')
        pool.remove('second@test.invalid')
        self.assertEqual(pool.resolve(),first)
        self.assertFalse(Path(pool.account_path(second)).exists())
        pool.remove(all_accounts=True)
        self.assertEqual(pool.entries()[1],[])
        self.assertFalse(Path(self.path).exists())


if __name__ == '__main__': unittest.main()

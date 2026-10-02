"""Offline regression tests. Run: python3 -m unittest -v test_editor_auth."""
import base64
import io
import json
import os
import pty
import tempfile
import time
import unittest
import threading
import urllib.parse
import urllib.request
import urllib.error
from unittest.mock import patch

import editor
import codex_auth
import native_auth
import providers


def token(**claims):
    return 'test.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.test'


class EditorTests(unittest.TestCase):
    def test_wrapping_and_cursor(self):
        e = editor.Editor()
        e._buffer = 'абвгд' * 20
        e._pos = 45
        lines, row, col = e._layout('› ', 20)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(editor._visible_width(line) <= 19 for line in lines))
        self.assertEqual((row, col), (2, 13))
        e._navigate(-1)
        self.assertEqual(e._pos, 28)

    def test_wide_combining_and_newlines(self):
        e = editor.Editor()
        e._buffer = '你e\u0301\nhello\n'
        e._pos = len(e._buffer)
        lines, row, col = e._layout('› ', 8)
        self.assertEqual(lines, ['› 你e\u0301', '  hello', '  '])
        self.assertEqual((row, col), (2, 2))

    def test_large_viewport_cursor_visible(self):
        e = editor.Editor()
        e._buffer = 'long paste\n' * 1000
        e._pos = len(e._buffer)
        with patch('editor.shutil.get_terminal_size', return_value=os.terminal_size((30, 8))), patch('sys.stdout', new_callable=io.StringIO) as out:
            e._render('› ')
            self.assertLessEqual(e._rows, 7)
            self.assertTrue(out.getvalue().endswith(editor.SHOW_CURSOR))
            e._pos = 0
            e._render('› ')
            self.assertEqual(e._cursor_row, 1)  # viewport status occupies the first row
            e._teardown('› ', False)
            self.assertIn(editor.PASTE_OFF, out.getvalue())

    def test_bracketed_paste_is_one_edit_not_enter(self):
        master, slave = pty.openpty()
        try:
            editor.tty.setraw(slave)
            os.write(master, b'\x1b[200~hello\r\nworld\x1b[31m\x1b[201~\r')
            self.assertEqual(editor.read_raw_key(slave), ('text', 'hello\nworld[31m'))
            self.assertEqual(editor.read_raw_key(slave), ('key', 'enter'))
        finally:
            os.close(master)
            os.close(slave)

    def test_oversized_paste_is_rejected_without_sending(self):
        master, slave = pty.openpty()
        try:
            editor.tty.setraw(slave)
            os.write(master,b'\x1b[200~123456789\x1b[201~\r')
            with patch('editor.MAX_PASTE_BYTES',4):
                self.assertEqual(editor.read_raw_key(slave),('key','paste-too-large'))
            self.assertEqual(editor.read_raw_key(slave),('key','enter'))
        finally:
            os.close(master)
            os.close(slave)

    def test_multiline_history_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + '/history'
            e = editor.Editor(history_path=path)
            e._remember('first\nsecond')
            e._remember('"quoted"')
            self.assertEqual(editor.Editor(history_path=path).history, e.history)


class AuthTests(unittest.TestCase):
    def test_refresh_rotates_and_preserves_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + '/auth.json'
            old = {'access_token': token(exp=1), 'refresh_token': 'old-test', 'account_id': 'test-account'}
            codex_auth._write_private_json(path, {'tokens': old})
            with patch('native_auth._post', return_value={'access_token': token(exp=time.time()+3600), 'refresh_token': 'rotated-test'}) as post:
                result = native_auth.ensure_fresh(path)
                self.assertEqual(result['tokens']['refresh_token'], 'rotated-test')
                self.assertEqual(result['tokens']['account_id'], 'test-account')
                native_auth.ensure_fresh(path)
                self.assertEqual(post.call_count, 1)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_failed_refresh_preserves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + '/auth.json'
            codex_auth._write_private_json(path, {'tokens': {'access_token': token(exp=1), 'refresh_token': 'test'}})
            with open(path) as f:
                before = f.read()
            with patch('native_auth._post', side_effect=ValueError('offline')):
                with self.assertRaises(ValueError):
                    native_auth.ensure_fresh(path)
            with open(path) as f:
                self.assertEqual(f.read(), before)

    def test_native_storage_no_external_discovery(self):
        with tempfile.TemporaryDirectory() as tmp, patch('codex_auth.CODEX_AUTH_PATH', tmp+'/auth.json'), patch('codex_auth._omp_accounts', side_effect=AssertionError('OMP must not be read')):
            codex_auth._ACCOUNTS = None
            self.assertEqual(codex_auth.accounts(), [])
            access = token(exp=time.time()+3600, **{'https://api.openai.com/auth': {'chatgpt_account_id':'test'}})
            native_auth._save({'access_token': access, 'refresh_token': 'test'}, tmp+'/auth.json')
            self.assertEqual(codex_auth.accounts()[0]['source'], 'uachat')
            with patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(native_auth.main(['logout']), 0)
            self.assertFalse(os.path.exists(tmp+'/auth.json'))
            self.assertEqual(codex_auth.accounts(), [])
        codex_auth._ACCOUNTS = None

    def test_browser_pkce_and_state_validation(self):
        threads = []
        def open_browser(url):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(params['code_challenge_method'], ['S256'])
            def callback():
                bad = native_auth.REDIRECT + '?state=wrong&code=bad'
                try:
                    urllib.request.urlopen(bad, timeout=5)
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 400)
                    error.close()
                good = native_auth.REDIRECT + '?' + urllib.parse.urlencode({'state':params['state'][0], 'code':'test-code'})
                with urllib.request.urlopen(good,timeout=5):
                    pass
            t = threading.Thread(target=callback)
            t.start()
            threads.append(t)
            return True
        with patch('native_auth.webbrowser.open', side_effect=open_browser), patch('native_auth._exchange', return_value={'access_token':'test'}) as exchange, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(native_auth.browser_login(), {'access_token':'test'})
            self.assertEqual(exchange.call_args.args[0], 'test-code')
            self.assertGreaterEqual(len(exchange.call_args.args[1]), 43)
        for t in threads:
            t.join(5)
            self.assertFalse(t.is_alive())

    def test_keys_do_not_discover_external_store(self):
        providers._KEY_CACHE.clear()
        with patch('providers._env_key', return_value='test-key'), patch('providers._store_keys', side_effect=AssertionError('external store')):
            self.assertEqual(providers.key_for('openrouter'), 'test-key')
        providers._KEY_CACHE.clear()

    def test_device_exchange_protocol(self):
        replies = [{'device_auth_id':'test-id', 'user_code':'TEST', 'interval':'1'},
                   {'authorization_code':'test-code', 'code_verifier':'test-verifier'},
                   {'access_token':'test-access'}]
        with patch('native_auth._post', side_effect=replies) as post, patch('sys.stdout', new_callable=io.StringIO):
            native_auth.device_login()
        self.assertEqual(post.call_args_list[2].args[0], '/oauth/token')
        self.assertEqual(post.call_args_list[2].args[1]['redirect_uri'], native_auth.ISSUER+'/deviceauth/callback')


if __name__ == '__main__':
    unittest.main()

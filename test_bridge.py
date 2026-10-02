"""Real loopback transport and subprocess readiness/security regression tests."""
import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
import bridge as proxy
import uarchat


class BridgeTests(unittest.TestCase):
    def test_port_zero_handshake_auth_and_sanitize(self):
        received = []
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                received.append((dict(self.headers),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.completed","response":{"id":"test","output":[]}}\n\n')
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'UACHAT_STREAM_STATE_PATH':tmp+'/stream.json','UACHAT_BRIDGE_DEBUG':'off'}):
            child = uarchat.Bridge(f'http://127.0.0.1:{server.server_port}/v1','test-upstream-key','test-session')
            try:
                base = child.start()
                def send(path, key, body):
                    return urllib.request.urlopen(urllib.request.Request(base+path, data=json.dumps(body).encode(), headers={'Authorization':'Bearer '+key}),timeout=5)
                for route,key,code in (('/responses','wrong',401),('/unknown',child.local_key,404)):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        send(route,key,{'input':'test'})
                    self.assertEqual(raised.exception.code,code)
                    raised.exception.close()
                self.assertEqual(received,[])
                body = {'input':[{'type':'function_call_output','call_id':'test','output':'pending'},
                                 {'type':'function_call_output','call_id':'test','output':'done'}]}
                with send('/responses',child.local_key,body) as response:
                    self.assertIn(b'response.completed',response.read())
                self.assertEqual(received[0][1]['input'][0]['output'],'done')
                self.assertEqual(len(received[0][1]['input']),1)
                headers = {k.lower():v for k,v in received[0][0].items()}
                self.assertEqual(headers['authorization'],'Bearer test-upstream-key')
                self.assertEqual(headers['x-opencode-session'],'test-session')
                with open(tmp+'/stream.json') as stream:
                    self.assertEqual(json.load(stream)['state'],'idle')
            finally:
                child.stop()
                server.shutdown()
                server.server_close()

    def test_off_origin_credentials_are_not_forwarded(self):
        import secure_http
        class Sink(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.server.leaked = True
                self.send_response(200)
                self.end_headers()
        sink = http.server.ThreadingHTTPServer(('127.0.0.1',0),Sink)
        sink.leaked = False
        class Redirect(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location',f'http://127.0.0.1:{sink.server_port}/steal')
                self.end_headers()
        redirect = http.server.ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        for server in (sink,redirect):
            threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{redirect.server_port}/api',headers={'Authorization':'Bearer test'})
            with self.assertRaises(urllib.error.HTTPError) as raised:
                secure_http.urlopen(req,timeout=3)
            raised.exception.close()
            self.assertFalse(sink.leaked)
        finally:
            for server in (sink,redirect):
                server.shutdown()
                server.server_close()

    def test_private_debug_write(self):
        with tempfile.TemporaryDirectory() as tmp, patch('bridge.os.path.expanduser',return_value=tmp), patch.dict(os.environ,{'UACHAT_BRIDGE_DEBUG':'on'}):
            proxy.dump_request(b'{"input":"private prompt"}')
            self.assertEqual(os.stat(tmp+'/bridge-last-request.json').st_mode & 0o777,0o600)


if __name__ == '__main__': unittest.main()

"""Offline Antigravity transport contracts; never touches real credentials."""
import io
import json
import os
import tempfile
import shutil
import subprocess
import unittest
import urllib.request
from unittest.mock import patch
import antigravity as a


class AntigravityTests(unittest.TestCase):
    def test_tool_signature_and_output_replay(self):
        saved = {'test-call': {'functionCall':{'name':'Bash','args':{'command':'ls'}}, 'thoughtSignature':'test-signature'}}
        payload = {'model':'gemini-3.6-flash','reasoning':{'effort':'max'}, 'input':[
            {'role':'system','content':'Be useful'}, {'role':'user','content':'list files'},
            {'type':'function_call','call_id':'test-call','name':'Bash','arguments':'{"command":"ls"}'},
            {'type':'function_call_output','call_id':'test-call','output':'file.txt'}],
            'tools':[{'type':'function','name':'Bash','parameters':{'type':'object','additionalProperties':False}}]}
        body = a.translate(payload,saved,'test-session')
        self.assertEqual(body['model'],'gemini-3.6-flash-high')
        self.assertEqual(body['request']['generationConfig']['thinkingConfig']['thinkingBudget'],10000)
        self.assertEqual(body['request']['contents'][1]['parts'][0]['thoughtSignature'],'test-signature')
        self.assertEqual(body['request']['contents'][2]['parts'][0]['functionResponse']['name'],'Bash')
        self.assertNotIn('additionalProperties',body['request']['tools'][0]['functionDeclarations'][0]['parameters'])

    def test_consumer_fallback_and_daily_route(self):
        with patch('antigravity.request', return_value=io.BytesIO(json.dumps({'ineligibleTiers':[{}]}).encode())) as post:
            self.assertEqual(a.discover_project('test-access'),'aicode-consumers')
            self.assertEqual(post.call_args.args[0], a.ENDPOINT+'/v1internal:loadCodeAssist')

    def test_http_bridge_persists_signature_and_cached_usage(self):
        part = {'functionCall':{'name':'Bash','args':{'command':'echo test'}},'thoughtSignature':'test-signature'}
        stream = b'data: '+json.dumps({'response':{'candidates':[{'content':{'parts':[part]}}],
            'usageMetadata':{'promptTokenCount':100,'cachedContentTokenCount':40,'candidatesTokenCount':10}}}).encode()+b'\n\n'
        with tempfile.TemporaryDirectory() as tmp, patch('antigravity.credentials', return_value={'access_token':'test-access','project_id':'test-project'}), patch('antigravity.request', side_effect=lambda *args,**kwargs:io.BytesIO(stream)):
            bridge = a.AntigravityBridge('test-offline')
            bridge.state_path = tmp+'/state.json'
            bridge.saved = {}
            base = bridge.start()
            try:
                with urllib.request.urlopen(urllib.request.Request(base+'/responses', data=json.dumps({'model':'gemini-3-flash','input':'test'}).encode(), headers={'Authorization':'Bearer '+bridge.key})) as r:
                    events = [json.loads(line[6:]) for line in r if line.startswith(b'data: {')]
                response = events[-1]['response']
                self.assertEqual(response['usage']['input_tokens_details']['cached_tokens'],40)
                call = response['output'][0]
                self.assertEqual(bridge.saved[call['call_id']]['thoughtSignature'],'test-signature')
                with open(bridge.state_path) as f:
                    self.assertIn(call['call_id'],json.load(f))
            finally:
                bridge.stop()

    @unittest.skipUnless(shutil.which('unreal-agent-runner'), 'runner binary not installed')
    def test_real_runner_tool_roundtrip_and_resume(self):
        def upstream(url, body, access):
            parts = [p for c in body['request']['contents'] for p in c['parts']]
            follow = any('functionResponse' in p for p in parts)
            if follow:
                self.assertTrue(any(p.get('thoughtSignature') == 'test-signature' for p in parts))
            result = ([{'text':'Bridge tool roundtrip completed.'}] if follow else
                      [{'functionCall':{'name':'Bash','args':{'command':'echo offline-ok'}},
                        'thoughtSignature':'test-signature'}])
            return io.BytesIO(b'data: '+json.dumps({'response':{'candidates':[{'content':{'parts':result}}],
                'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':12}}}).encode()+b'\n\n')
        with tempfile.TemporaryDirectory() as tmp, patch('antigravity.credentials', return_value={'access_token':'test','project_id':'test'}), patch('antigravity.request', side_effect=upstream):
            for turn in range(2):
                bridge = a.AntigravityBridge('offline-test-runner')
                bridge.state_path = tmp+'/state.json'
                bridge.saved = a.codex_auth._read_json(bridge.state_path) or {}
                base = bridge.start()
                try:
                    env = os.environ.copy()
                    env.update(UNREAL_HARNESS_LLM_PROVIDER='openai', UNREAL_HARNESS_LLM_BASE_URL=base,
                               UNREAL_HARNESS_LLM_API_KEY=bridge.key, SHELL='/bin/bash')
                    run = subprocess.run(['unreal-agent-runner','-workspace',tmp,'-session-directory',tmp+'/sessions'],
                        input=json.dumps({'prompt':'run echo' if turn == 0 else 'continue',
                            'model':'gemini-3-flash','session_id':'offline-test-runner','max_attempts':1}),
                        text=True,capture_output=True,env=env,timeout=30)
                    self.assertEqual(run.returncode,0,run.stderr)
                    self.assertIn('Bridge tool roundtrip completed.',run.stdout)
                    if turn == 0: self.assertIn('offline-ok',run.stdout)
                finally:
                    bridge.stop()


if __name__ == '__main__': unittest.main()

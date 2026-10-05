import unittest
import model_limits
from terminal_ui import SessionMetrics

class ContextLimitsTests(unittest.TestCase):
    def test_provider_identity_and_unknown_models(self):
        self.assertEqual(model_limits.resolve('openai-codex','gpt-6-luna')['context'],272000)
        self.assertEqual(model_limits.resolve('opencode-go','gpt-6-luna')['context'],1050000)
        self.assertIsNone(model_limits.resolve('opencode-go','invented-gpt-6-luna'))

    def test_route_metadata_wins_over_snapshot(self):
        limit = model_limits.resolve('opencode-go','gpt-6-luna',{'context_window':123456})
        self.assertEqual(limit['context'],123456)
        self.assertEqual(limit['source'],'provider catalogue')

    def test_known_window_is_visible_before_first_response(self):
        self.assertIn('272.0k',SessionMetrics().context_label(272000))

if __name__=='__main__': unittest.main()

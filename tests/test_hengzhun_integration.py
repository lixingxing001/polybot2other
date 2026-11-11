"""The research sidecar cannot stop the existing strategy lifecycle or trade."""
from types import SimpleNamespace
import json
import threading
import unittest
import urllib.request
from unittest.mock import Mock

from polybot2other.bot import PaperTradingBot
from polybot2other.config import Settings
from polybot2other.models import MarketRound
from polybot2other.paper_execution import ExecutionPolicy
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.web import DashboardServer, Handler


class ResearchIsolationTests(unittest.TestCase):
    def bot(self, research):
        bot = PaperTradingBot.__new__(PaperTradingBot)
        bot.hengzhun = research
        bot.hengzhun_error = None
        bot.settings = SimpleNamespace(hengzhun_enabled=True)
        return bot

    def test_partial_start_failure_is_visible_and_stopped_without_propagating(self):
        research = SimpleNamespace(start=Mock(side_effect=OSError('synthetic disk error')), stop=Mock())
        bot = self.bot(research)
        bot._start_hengzhun()
        research.stop.assert_called_once_with()
        self.assertIsNone(bot.hengzhun)
        self.assertIn('OSError', bot.hengzhun_snapshot()['error'])
        self.assertFalse(bot.hengzhun_snapshot()['live_execution_enabled'])

    def test_stop_failure_does_not_interrupt_old_account_shutdown(self):
        research = SimpleNamespace(stop=Mock(side_effect=RuntimeError('synthetic')))
        bot = self.bot(research)
        bot._stop_hengzhun()
        self.assertIn('RuntimeError', bot.hengzhun_error)

    def test_status_failure_keeps_research_mode_and_never_claims_live(self):
        research = SimpleNamespace(mode='PAPER', snapshot=Mock(side_effect=ValueError('synthetic')))
        snapshot = self.bot(research).hengzhun_snapshot()
        self.assertEqual(snapshot['mode'], 'PAPER')
        self.assertFalse(snapshot['live_execution_enabled'])
        self.assertIn('ValueError', snapshot['error'])

    def test_disabled_sidecar_has_no_lifecycle_action(self):
        bot = self.bot(None)
        bot.settings.hengzhun_enabled = False
        bot._start_hengzhun()
        bot._stop_hengzhun()
        self.assertFalse(bot.hengzhun_snapshot()['enabled'])

    def test_derived_baseline_observation_never_changes_main_frame(self):
        research = SimpleNamespace(capture=Mock())
        bot = self.bot(research); bot.settings = Settings()
        frame = MarketInputs(None, {}, {}, {}, 1800000000.0, 'fixture', 'WAIT')
        bot._shared_market_provider = Mock(return_value=frame)
        self.assertIs(bot._capture_shared_market_inputs(), frame)
        research.capture.assert_called_once()
        payload = research.capture.call_args.args[1]
        self.assertTrue(payload['derived'])
        self.assertEqual(payload['record_type'], 'B1MarketInput')
        research.capture.side_effect = OSError('synthetic observer failure')
        bot._hengzhun_baseline_at = 0
        self.assertIs(bot._capture_shared_market_inputs(), frame)

    def test_research_get_endpoint_is_read_only(self):
        research = SimpleNamespace(snapshot=Mock(return_value={'enabled': True, 'mode': 'RECORD',
                                                               'live_execution_enabled': False}))
        bot = self.bot(research)
        server = DashboardServer(('127.0.0.1', 0), Handler, bot)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{server.server_port}/api/hengzhun', timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)['mode'], 'RECORD')
            research.snapshot.assert_called_once_with()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_baseline_captures_actual_policy_and_cached_info_without_refetch(self):
        research = SimpleNamespace(capture=Mock())
        bot = self.bot(research); bot.settings = Settings()
        market = MarketRound('test', 'BTC', 1800000000, 1800000300, 100)
        frame = MarketInputs(market, {}, {}, {}, 1800000001.0, 'fixture', None)
        bot._shared_market_provider = Mock(return_value=frame)
        policy = ExecutionPolicy(confirmation_seconds=12.043, confirmation_samples=14)
        info = {'fee_rate': .07, 'fee_exponent': 1, 'tick_size': '0.01'}
        bot.paper_execution = SimpleNamespace(policy=policy, info_cache={'test': info})
        rule = {'stake_dollars': 3, 'max_open_trades': 5}
        bot.managed_live = SimpleNamespace(store=SimpleNamespace(config=Mock(return_value={'strategies': {'MAIN': rule}})))
        self.assertIs(bot._capture_shared_market_inputs(), frame)
        payload = research.capture.call_args.args[1]
        self.assertEqual(payload['execution_policy']['confirmation_seconds'], 12.043)
        self.assertEqual(payload['market_info'], info)
        self.assertEqual(payload['main_rule'], rule)


if __name__ == '__main__':
    unittest.main()

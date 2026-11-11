from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from polybot2other.config import Settings
from polybot2other.models import MarketRound
from polybot2other.optimized_reference import SPOT
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.managed_live_signals import LiveSignals
from polybot2other.hengzhun.baseline import FrozenMainBaseline, SIGNAL_FIELDS, capture_main_frame
from polybot2other.hengzhun.cli import freeze_baseline


class FrozenBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'frozen'
        self.settings = Settings(min_time_left_seconds=30, max_time_left_seconds=90)
        self.settings_dict = {k: getattr(self.settings, k) for k in SIGNAL_FIELDS}
        freeze_baseline(Path(__file__).parents[1], self.path, {'signal_settings': self.settings_dict})
        self.now = 1800000240.0
        self.market = MarketRound('synthetic', 'BTC', self.now-240, self.now+60, 100,
                                  condition_id='condition', up_token='up', down_token='down')
        self.reference = {'ready': True, 'protocol_verified': True,
                          'latest': {SPOT: {'price': 100.2, 'source_ms': self.now*1000-100}}}
        self.price = {'chainlink': 100.2, 'chainlink_updated_ms': self.now*1000-100}
        self.quotes = {side: {'best_bid': bid, 'best_ask': ask, 'ask_size': 100,
                             'source_ms': self.now*1000-100, 'received_ms': self.now*1000-90,
                             'updated_at_ms': self.now*1000-100, 'token_id': token}
                       for side, token, bid, ask in [('Up', 'up', .59, .60), ('Down', 'down', .39, .40)]}
        self.frame = MarketInputs(self.market, self.reference, self.price, self.quotes, self.now, 'frame', None)

    def test_frozen_actual_signal_equals_main_without_network_or_wallet(self):
        info = {'fee_rate': .07, 'fee_exponent': 1}
        expected, limit = LiveSignals.evaluate(SimpleNamespace(settings=self.settings), 'MAIN',
            {'stake_dollars': 5, 'max_open_trades': 1, 'max_daily_loss': 10},
            self.market, self.reference, self.quotes, self.now, info, market_input=self.frame)
        with patch('httpx.Client.get', side_effect=AssertionError('NO NETWORK')):
            baseline = FrozenMainBaseline(self.path)
            observed = baseline.evaluate(capture_main_frame(self.frame, self.settings), int(self.now*1000), **info)
        for key, value in asdict(expected).items():
            self.assertEqual(observed[key], value)
        self.assertEqual(observed['limit_price'], limit)
        self.assertEqual(observed['signal_code'], 'FROZEN_ACTUAL_MAIN')

    def test_late_or_changed_settings_cannot_be_used_as_frozen_baseline(self):
        baseline = FrozenMainBaseline(self.path)
        recorded = capture_main_frame(self.frame, self.settings)
        with self.assertRaisesRegex(ValueError, 'NOT_VISIBLE'):
            baseline.evaluate(recorded, recorded['observed_at']-1, fee_rate=.07, fee_exponent=1)
        recorded['signal_settings']['min_edge'] += .01
        with self.assertRaisesRegex(ValueError, 'CONFIG_CHANGED'):
            baseline.evaluate(recorded, recorded['observed_at'], fee_rate=.07, fee_exponent=1)

    def test_source_mutation_invalidates_frozen_baseline(self):
        source = self.path/'src/polybot2other/strategy.py'
        source.write_text(source.read_text()+'\n# altered\n')
        with self.assertRaisesRegex(ValueError, 'SOURCE_HASH_MISMATCH'):
            FrozenMainBaseline(self.path)

    def test_recorded_inputs_are_copied_and_new_decision_rechecks_staleness(self):
        recorded = capture_main_frame(self.frame, self.settings)
        recorded['input']['price']['chainlink'] = 999
        self.assertEqual(self.frame.price['chainlink'], 100.2)
        result = FrozenMainBaseline(self.path).evaluate(capture_main_frame(self.frame, self.settings),
            int(self.now*1000)+10000, fee_rate=.07, fee_exponent=1)
        self.assertEqual(result['side'], 'NO_TRADE')


if __name__ == '__main__': unittest.main()

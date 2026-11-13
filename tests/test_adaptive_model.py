from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest

from polybot2other.adaptive_model import AdaptiveModel, MAX_ADJUSTMENT
from polybot2other.models import MarketRound, Signal
from polybot2other.storage import TradeStore


class AdaptiveModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = time.time()

    def add_settled_trade(self, store, index, *, outcome="Up", side="Up", confidence=.8, move_bps=20.):
        round_id = f"btc-updown-5m-adaptive-{index}"
        market = MarketRound(
            round_id,
            "BTC",
            self.now + index,
            self.now + index + 300,
            100.0,
            up_token=f"up-{index}",
            down_token=f"down-{index}",
        )
        store.upsert_round(market)
        signal = Signal("BTC", side, confidence, .4, move_bps, "adaptive test")
        trade_id = store.place_trade(SimpleNamespace(market=market, signal=signal, stake_dollars=1.0))
        store.settle_round_outcome(round_id, outcome, self.now + index + 301)
        return trade_id

    def test_seed_samples_are_read_only_and_insufficient_data_falls_back(self):
        seed = TradeStore(self.root / "main_optimized.sqlite3", 100)
        adaptive = TradeStore(self.root / "main_adaptive.sqlite3", 100)
        self.addCleanup(seed.conn.close)
        self.addCleanup(adaptive.conn.close)
        self.add_settled_trade(seed, 1)
        model = AdaptiveModel(
            self.root / "main_adaptive.model.sqlite3",
            seed.db_path,
            adaptive.db_path,
        )
        self.addCleanup(model.close)

        snapshot = model.snapshot()
        self.assertEqual(snapshot["seed_samples"], 1)
        self.assertEqual(snapshot["adaptive_samples"], 0)
        self.assertEqual(snapshot["state"], "WARMING")
        prediction = {"up_score": .8, "projected_final_twap": 100.2}
        adjusted = model.adjust_prediction(
            prediction,
            SimpleNamespace(target_price=100.0),
            self.now,
        )
        self.assertEqual(adjusted["adaptive_state"], "BASELINE_FALLBACK")
        self.assertEqual(adjusted["up_score"], prediction["up_score"])
        self.assertEqual(seed.recent_trade_count(), 1)

    def test_settled_seed_and_adaptive_rows_adjust_with_a_bounded_delta(self):
        seed = TradeStore(self.root / "main_optimized.sqlite3", 100)
        adaptive = TradeStore(self.root / "main_adaptive.sqlite3", 100)
        self.addCleanup(seed.conn.close)
        self.addCleanup(adaptive.conn.close)
        for index in range(2):
            self.add_settled_trade(seed, index)
        for index in range(2, 12):
            self.add_settled_trade(adaptive, index, outcome="Up" if index % 3 else "Down")

        model = AdaptiveModel(
            self.root / "main_adaptive.model.sqlite3",
            seed.db_path,
            adaptive.db_path,
        )
        self.addCleanup(model.close)
        prediction = model.adjust_prediction(
            {"up_score": .8, "projected_final_twap": 100.2},
            SimpleNamespace(target_price=100.0),
            self.now,
        )
        self.assertEqual(prediction["adaptive_samples"], 12)
        self.assertIn(prediction["adaptive_state"], {"CALIBRATING", "ADAPTING"})
        self.assertLessEqual(abs(prediction["adaptive_delta"]), MAX_ADJUSTMENT)
        self.assertGreaterEqual(prediction["adaptive_score"], .5)
        self.assertLessEqual(prediction["adaptive_score"], .9)


if __name__ == "__main__":
    unittest.main()

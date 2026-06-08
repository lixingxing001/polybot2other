import unittest
import time
from polybot2other.models import MarketRound, Signal
from polybot2other.signal_filters import aggressive_edge_v2_risk_note, aggressive_edge_v2_risk_report


class V2ShadowTest(unittest.TestCase):
    def test_aggressive_edge_v2_risk_report(self):
        market = MarketRound("test_round", "BTC", time.time() - 100, time.time() + 100, 100.0)
        signal = Signal("BTC", "Up", 0.8, 0.5, 10.0, "test")
        now_ms = int(time.time() * 1000)
        price = {
            "chainlink": 101.0,
            "chainlink_updated_ms": now_ms,
            "binance": 101.5,
            "binance_updated_ms": now_ms,
        }
        quote = {
            "best_bid": 0.45,
            "best_ask": 0.50,
            "bid_size": 1000,
            "ask_size": 500,
            "bids": [{"price": 0.45, "size": 1000}, {"price": 0.44, "size": 500}],
            "asks": [{"price": 0.50, "size": 400}, {"price": 0.51, "size": 200}],
        }
        before60_tick = {"price": 100.0}
        before30_tick = {"price": 100.12}

        report = aggressive_edge_v2_risk_report(
            market,
            signal,
            price=price,
            quote=quote,
            signal_at=time.time(),
            before60_tick=before60_tick,
            before30_tick=before30_tick,
        )
        self.assertIsNotNone(report)
        self.assertIn("risk_score", report)
        self.assertIn(report["risk_level"], {"LOW", "MEDIUM", "HIGH"})
        self.assertAlmostEqual(report["features"]["top_level_skew"], 0.6667, places=4)
        self.assertAlmostEqual(report["features"]["depth_skew"], 0.7143, places=4)
        self.assertEqual(report["features"]["spread"], 0.05)
        self.assertAlmostEqual(report["features"]["momentum_decay_bps"], 14.0, places=4)
        note = aggressive_edge_v2_risk_note(report)
        self.assertIn("V2_SHADOW", note)
        self.assertIn("risk=", note)
        self.assertIn("depth_skew=0.7143", note)


if __name__ == '__main__':
    unittest.main()

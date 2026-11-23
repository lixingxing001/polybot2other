from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from polybot2other.forecast_paper import ForecastPaperPolicy
from polybot2other.models import MarketRound
from polybot2other.optimized_reference import ReferenceCache, SPOT, TWAP
from polybot2other.shared_market_inputs import MarketInputs


class ForecastPaperPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.refs = ReferenceCache(Path(temp.name) / "refs.db")
        self.addCleanup(self.refs.conn.close)
        self.policy = ForecastPaperPolicy(self.refs)
        self.at = 3000.
        self.market = MarketRound("r", "BTC", self.at - 240, self.at + 60, 100.,
                                  up_token="u", down_token="d")
        stamp = self.at * 1000
        self.latest = {channel: {"price": 100.1, "source_ms": stamp, "received_ms": stamp}
                       for channel in (SPOT, TWAP)}
        self.quotes = {
            side: {"token_id": token, "source_ms": stamp, "received_ms": stamp,
                   "best_ask": .63, "best_bid": .61, "asks": [{"price": .63, "size": 50.}]}
            for side, token in (("Up", "u"), ("Down", "d"))
        }
        self.frame = MarketInputs(self.market, {"ready": True, "protocol_verified": True,
                                                 "latest": self.latest}, {}, self.quotes,
                                  self.at, "snapshot", None)
        self.info = {"fee_rate": .07, "fee_exponent": 1, "tick_size": "0.01"}
        self.held = {"side": "Up", "round_id": "r"}

    def evaluate(self, up_score, frame=None, **kwargs):
        projected = {"up_score": up_score, "projected_final_twap": 100.1}
        with patch("polybot2other.forecast_paper.forecast_twap", return_value=projected):
            return self.policy.evaluate(frame or self.frame, self.info, **kwargs)

    def test_reversing_direction_and_holding_are_independent_of_entry(self):
        for score, side in [(0.75, "Up"), (.25, "Down")]:
            with self.subTest(side=side):
                buy = self.evaluate(score)
                self.assertEqual((buy.action, buy.as_signal().side, buy.forecast_direction), ("BUY", side, side))
                self.assertAlmostEqual(buy.limit, .64)
                self.assertGreater(buy.all_in_cost, buy.limit)
        held = self.evaluate(.75, position=self.held, net_bid=.59)
        self.assertEqual((held.action, held.side, held.as_signal().side), ("HOLD", "Up", "NO_TRADE"))
        sell = self.evaluate(.30, position=self.held, net_bid=.21)
        self.assertEqual((sell.action, sell.side, sell.forecast_direction), ("SELL", "Up", "Down"))
        self.assertIsNone(sell.min_net_bid)
        self.assertEqual(sell.as_signal().side, "NO_TRADE")
        self.assertEqual(sell.as_features()["up_score"], .30)

    def test_exit_premium_uses_executable_net_amount(self):
        self.quotes["Up"].update(best_bid=.74, best_ask=.75)
        held = self.evaluate(.70, position=self.held, net_bid=.719)
        sold = self.evaluate(.70, position=self.held, net_bid=.724)
        self.assertEqual(held.action, "HOLD")
        self.assertEqual(sold.action, "SELL")
        self.assertAlmostEqual(sold.min_net_bid, .72)

    def test_neutral_and_fee_gate_keep_raw_direction_evidence(self):
        flat = self.evaluate(.54)
        self.assertEqual((flat.action, flat.up_score, flat.forecast_direction), ("WAIT", .54, None))
        # .69 clears the limit by five cents, but the official fee consumes enough of that margin.
        fee_block = self.evaluate(.69)
        self.assertEqual((fee_block.action, fee_block.forecast_direction), ("WAIT", "Up"))
        self.info["fee_rate"] = 0.
        self.assertEqual(self.evaluate(.69).action, "BUY")

    def test_suppresses_actions_on_missing_or_wrong_reference_and_quotes(self):
        for bad_frame in (replace(self.frame, error="stale"), replace(self.frame, market=None),
                          replace(self.frame, reference={**self.frame.reference, "protocol_verified": False})):
            self.assertEqual(self.evaluate(.10, bad_frame, position=self.held, net_bid=.60).action, "HOLD")
            self.assertIsNone(self.evaluate(.90, bad_frame).up_score)
        self.quotes["Up"]["token_id"] = "old-round"
        self.assertEqual(self.evaluate(.20, position=self.held, net_bid=.60).action, "HOLD")
        self.assertEqual(self.evaluate(.90).action, "WAIT")
        self.quotes["Up"]["token_id"] = "u"
        self.latest[SPOT]["received_ms"] += 1
        self.assertIsNone(self.evaluate(.90).up_score)

    def test_requires_valid_net_bid_and_matching_position(self):
        for value in (None, 0., True, float("nan"), .62):
            self.assertEqual(self.evaluate(.10, position=self.held, net_bid=value).action, "HOLD")
        other = self.evaluate(.1, position={"side": "Up", "round_id": "different"}, net_bid=.5)
        self.assertEqual(other.action, "HOLD")
        self.assertIsNone(other.up_score)

    def test_time_spread_depth_and_expiry_rules(self):
        for seconds in (181, 19):
            at_edge = replace(self.frame, market=replace(self.market, ends_at=self.at + seconds))
            decision = self.evaluate(.85, at_edge)
            self.assertEqual((decision.action, decision.forecast_direction), ("WAIT", "Up"))
        end = replace(self.frame, market=replace(self.market, ends_at=self.at + 1))
        self.assertEqual(self.evaluate(.1, end, position=self.held, net_bid=.58).action, "HOLD")
        expired = replace(self.frame, market=replace(self.market, ends_at=self.at))
        self.assertIsNone(self.evaluate(.90, expired).up_score)
        self.quotes["Up"]["best_bid"] = .58
        self.assertEqual(self.evaluate(.85).action, "WAIT")
        self.quotes["Up"].update(best_bid=.61, asks=[])
        self.assertEqual(self.evaluate(.85).action, "WAIT")

    def test_real_model_excludes_later_backfill_and_reports_last_minute_target(self):
        # Every source time is in the past, but all history arrives after the first observation.
        rows = [(SPOT, int((self.at - offset) * 1000), 100.1, "100.1", self.at * 1000 + 50, 1)
                for offset in range(121, -1, -1)]
        with self.refs.conn:
            self.refs.conn.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,?,?)", rows)
        frame = replace(self.frame, market=replace(self.market, ends_at=self.at + 20))
        self.assertIsNone(self.policy.evaluate(frame, self.info).up_score)
        visible = self.policy.evaluate(replace(frame, observed_at=self.at + .1), self.info)
        self.assertGreater(visible.up_score, .62)
        self.assertAlmostEqual(visible.remaining_average_to_tie, 99.8)
        self.assertEqual(visible.forecast_source_ms, self.at * 1000)

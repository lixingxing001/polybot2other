import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from polybot2other.forecast_entry_trace import ForecastEntryTrace, ask_depth
from polybot2other.managed_live_client import fee_amount
from polybot2other.models import MarketRound
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.storage import TradeStore


class EntryTraceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TradeStore(Path(tmp.name) / "early.sqlite3", 100)
        self.addCleanup(self.store.conn.close)
        self.tracker = ForecastEntryTrace(self.store)
        self.market = MarketRound("one", "BTC", 100, 400, 60_000, up_token="u", down_token="d")
        self.info = {"fee_rate": .05, "fee_exponent": 1, "tick_size": ".01"}

    def observe(self, at, action="WAIT", direction="Up", price=.55, cost=None, stale=False, market=None):
        market = market or self.market
        quotes = {direction or "Up": {"token_id": "d" if direction == "Down" else "u",
                         "source_ms": (at - 5 if stale else at) * 1000, "received_ms": at * 1000,
                         "best_bid": price - .01, "best_ask": price, "asks":
                         [{"price": price, "size": 12}, {"price": round(price + .01, 4), "size": 3}]}}
        frame = MarketInputs(market, {}, {}, quotes, at, f"{market.round_id}:{at}", None)
        features = {"action": action, "forecast_direction": direction, "score": .74 if direction else None,
                    "reason": "policy", "all_in_cost": cost, "limit": None if cost is None else round(price + .01, 4)}
        self.tracker.observe(frame, features, self.info)

    def row(self):
        return self.tracker.summary()["rows"][0]

    def test_records_first_in_window_and_eligible_buy_from_exact_frame(self):
        self.observe(159, "BUY", cost=.5)
        self.observe(160, direction=None)
        self.observe(161, price=.90)
        self.observe(162, "BUY", cost=.57)
        self.observe(163, "BUY", price=.60, cost=.62)
        self.observe(381, "BUY", price=.66, cost=.68)
        row = self.row()
        first = row["first_direction"]
        self.assertEqual((row["window_seen"]["at"], first["at"], first["snapshot_id"]), (160, 161, "one:161"))
        self.assertEqual(row["first_priced_direction"], first)
        self.assertFalse(row["window_seen"]["late_start"])
        self.assertEqual((first["side"], first["ask"], first["ask_size_at_best"]), ("Up", .90, 12))
        self.assertIsNone(first["all_in_cost"])
        self.assertIsNone(first["limit_price"])
        self.assertAlmostEqual(first["ask_edge"], .74 - (.90 + fee_amount(1, .90, .05, 1)))
        buy = row["first_buy"]
        self.assertEqual((buy["at"], buy["ask_size_in_limit"]), (162, 15))
        self.assertAlmostEqual(buy["edge"], .74 - .57)
        self.assertGreater(row["observation_gap"]["seconds"], 5)
        self.assertEqual(self.tracker.summary()["counts"]["first_buy"], 1)

    def test_stale_on_first_direction_is_kept_and_does_not_borrow_later_book(self):
        self.observe(200, stale=True)
        self.observe(201, "WAIT", price=.51, direction="Down")
        self.observe(202, "BUY", cost=.57)
        row = self.row()
        self.assertTrue(row["window_seen"]["late_start"])
        for key in ("bid", "ask", "ask_all_in_cost", "ask_size_at_best", "limit_price", "edge"):
            self.assertIsNone(row["first_direction"][key])
        self.assertTrue(row["first_direction"]["quote_error"])
        self.assertEqual(row["first_buy"]["ask"], .55)
        priced = row["first_priced_direction"]
        self.assertEqual((priced["at"], priced["side"], priced["ask"]), (201, "Down", .51))
        self.assertEqual(row["first_direction"]["side"], "Up")

    def test_legacy_round_keeps_first_priced_direction_empty(self):
        self.observe(160, "BUY", cost=.57)
        with self.store.conn:
            self.store.conn.execute("DELETE FROM forecast_entry_trace WHERE stage='first_priced_direction'")
        self.tracker = ForecastEntryTrace(self.store)
        second = replace(self.market, round_id="two", started_at=400, ends_at=700)
        self.observe(460, direction="Down", market=second)
        new, old = self.tracker.summary()["rows"]
        self.assertIsNone(old["first_priced_direction"])
        self.assertIsNotNone(old["first_buy"])
        self.assertEqual(new["first_priced_direction"]["side"], "Down")

    def test_only_flat_calls_and_coverage_handles_rounds_without_a_call(self):
        self.observe(160, "HOLD")
        self.observe(161, "SELL")
        self.observe(162, direction=None)
        second = replace(self.market, round_id="two", started_at=400, ends_at=700)
        self.observe(461, action="BUY", cost=.57, market=second)
        result = self.tracker.summary()
        self.assertEqual((result["started_at"], result["counts"]["window_seen"], result["counts"]["first_direction"]), (160, 2, 1))
        self.assertEqual([row["round_id"] for row in result["rows"]], ["two", "one"])
        self.assertIsNone(result["rows"][1]["first_buy"])

    def test_busy_ledger_keeps_original_intent_and_does_not_touch_trading_transaction(self):
        self.observe(160, direction=None)
        self.store.conn.execute("BEGIN IMMEDIATE")
        self.store.conn.execute("UPDATE account SET cash_balance=80")
        start = time.monotonic()
        self.observe(161, price=.60)
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()
        with patch("polybot2other.forecast_entry_trace.time.monotonic", return_value=time.monotonic() + 2):
            self.observe(162, "BUY", price=.55, cost=.57)
        row = self.row()
        self.assertEqual((row["first_direction"]["at"], row["first_direction"]["ask"], row["first_buy"]["at"]), (161, .60, 162))
        self.assertEqual(self.store.account()["cash_balance"], 100)

    def test_restarts_and_execution_retries_never_replace_first_event(self):
        self.observe(160, "BUY", cost=.57)
        self.tracker.record("pre_submit_reject", "one", 161, {"reason": "too expensive"})
        self.tracker.record("arrival_fill", "one", 165, {"order_id": 3})
        self.tracker.record("arrival_empty", "one", 168, {"order_id": 4})
        self.tracker = ForecastEntryTrace(self.store)
        self.observe(175, "BUY", price=.51, cost=.53)
        self.tracker.record("pre_submit_reject", "one", 180, {"reason": "changed"})
        self.tracker.record("arrival_fill", "one", 182, {"order_id": 8})
        row = self.row()
        self.assertEqual((row["first_buy"]["at"], row["pre_submit_reject"]["at"], row["arrival_fill"]["order_id"], row["arrival_empty"]["order_id"]),
                         (160, 161, 3, 4))
        self.assertIsNotNone(row["observation_gap"])

    def test_depth_and_unavailable_database(self):
        self.assertEqual(ask_depth({"best_ask": .5, "asks": [{"price": .51, "size": 5}]}, .7), (None, None))
        self.assertEqual(ask_depth({"best_ask": .5, "asks": [{"price": .5, "size": 5}]}, .4), (5, 0))
        absent = ForecastEntryTrace(SimpleNamespace(db_path=self.store.db_path.with_name("absent.sqlite3")))
        self.assertFalse(absent.summary()["available"])
        absent.record("arrival_empty", "one", 170, {"reason": "empty"})
        self.assertFalse(absent.path.exists())

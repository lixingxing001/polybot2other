"""Paper integration regressions for delayed premium exits."""
from dataclasses import replace
from types import SimpleNamespace
import threading
import unittest

import test_forecast_execution as fixtures

from polybot2other.config import Settings
from polybot2other.forecast_confirm_trial import ForecastConfirmTrialExecution
from polybot2other.paper_execution import ExecutionPolicy
from polybot2other.storage import TradeStore


class ForecastConfirmTrialTests(unittest.TestCase):
    def setUp(self):
        self.market_data = h = fixtures.ForecastExecutionTests()
        self.addCleanup(h.doCleanups)
        h.setUp()
        self.trial_store = TradeStore(h.store.db_path.with_name("trial.sqlite3"), 100)
        self.addCleanup(self.trial_store.conn.close)
        self.trial_store.upsert_round(h.market)
        self.source_ms = None
        regular_book = h.book

        def book(token):
            b = regular_book(token)
            if self.source_ms is not None:
                b["source_ms"] = self.source_ms
            return b
        h.book = book
        self.trial = self.make_trial()

    def make_trial(self):
        h = self.market_data
        key = "FORECAST_CONFIRM_PAPER"
        settings = Settings(db_path=self.trial_store.db_path, live_trading_runtime_enabled=False)
        def frame():
            return replace(h.frame(), snapshot_id=f"capture-{h.now}")
        bot = SimpleNamespace(store=self.trial_store, settings=settings, _lock=threading.RLock(),
                              _sync_shared_market_inputs=frame, paper_trading_paused=False)
        return ForecastConfirmTrialExecution(bot, key, settings, frame_reader=frame,
                info_reader=lambda market:h.info, book_reader=h.book, feed=SimpleNamespace(references=h.refs),
                config_reader=lambda:{"revision":1, "strategies":{key:h.rule}},
                clock=lambda:h.now, policy=ExecutionPolicy(confirmation_seconds=4))

    def buy(self):
        h = self.market_data
        engines = (h.engine, self.trial)
        for e in engines:
            e.decide()
            self.assertEqual(e.pending()[0]["action"], "BUY")
        for seconds in (1.1, 3):
            h.now += seconds
            for e in engines:
                e.poll()
        self.assertEqual(h.store.account()["cash_balance"], self.trial_store.account()["cash_balance"])
        h.now += 1
        h.bid, h.ask = .96, .97

    def rows(self):
        return self.trial_store.conn.execute("SELECT * FROM forecast_premium_checks ORDER BY id").fetchall()

    def test_repeated_capture_waits_for_new_exchange_quote_and_rechecks_before_submit(self):
        self.buy()
        h = self.market_data
        h.engine.decide()
        self.assertEqual(h.engine.pending()[0]["action"], "SELL")
        self.source_ms = h.now*1000
        self.trial.decide()
        h.now += .6
        self.trial.decide()
        self.assertFalse(self.trial.pending())
        wait, = self.rows()
        self.assertEqual(wait["outcome"], "waiting")
        self.assertIsNone(wait["confirmed_at"])

        self.source_ms = h.now*1000
        self.trial.decide()
        sell, = self.trial.pending()
        self.assertEqual(sell["action"], "SELL")
        check, = self.rows()
        self.assertEqual((check["outcome"], check["order_id"]), ("submitted", sell["order_id"]))
        self.assertGreater(check["confirmed_source_ms"], check["first_source_ms"])
        self.assertEqual(sell["forecast_decision"]["premium_check_id"], check["id"])
        self.assertEqual(sell["forecast_decision"]["premium_first_source_ms"], wait["first_source_ms"])
        self.assertIsNotNone(sell["forecast_decision"]["min_net_bid"])
        self.assertFalse(h.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='forecast_premium_checks'").fetchone())

    def test_reversal_does_not_wait_even_when_a_premium_check_is_active(self):
        self.buy()
        h = self.market_data
        self.trial.decide()
        h.now += 1
        h.spot = 98.
        h.bid, h.ask = .32, .34
        self.trial.decide()
        order, = self.trial.pending()
        self.assertEqual(order["action"], "SELL")
        self.assertIn("转向", order["exit_reason"])
        self.assertIsNone(order["forecast_decision"]["premium_check_id"])
        self.assertEqual(self.rows()[0]["outcome"], "withdrawn")

    def test_gap_unpriced_position_and_changed_position_restart_the_wait(self):
        self.buy()
        h = self.market_data
        self.trial.decide()
        h.now += 3.2
        self.trial.decide()
        self.assertEqual([r["outcome"] for r in self.rows()],["expired","waiting"])
        self.assertFalse(self.trial.pending())
        h.now += .1
        h.sell_depth = 0.
        self.trial.decide()
        self.assertEqual(self.rows()[-1]["outcome"], "invalidated")
        h.sell_depth = 500.
        self.trial.decide()
        pos = self.trial_store.open_trades()[0]
        with self.trial_store.conn:
            self.trial_store.conn.execute("UPDATE trades SET shares=shares-1 WHERE id=?", (pos["id"],))
        h.now += .2
        self.trial.decide()
        self.assertFalse(self.trial.pending())
        self.assertEqual([r["outcome"] for r in self.rows()][-2:], ["expired", "waiting"])
        h.frame_error = "参考价断开"
        self.trial.decide()
        self.assertEqual(self.rows()[-1]["outcome"], "invalidated")
        h.frame_error = None
        self.trial.decide()
        self.assertFalse(self.trial.pending())

    def test_restart_and_retrograde_preflight_book_cannot_confirm(self):
        self.buy()
        h = self.market_data
        first_ms = h.now*1000
        self.trial.decide()
        self.trial = self.make_trial()
        self.assertEqual(self.rows()[0]["outcome"], "interrupted")
        h.now += .1
        self.trial.decide()
        self.assertFalse(self.trial.pending())
        self.trial.book_reader = lambda token: {**h.book(token), "source_ms": first_ms}
        h.now += .1
        self.trial.decide()
        self.assertFalse(self.trial.pending())
        self.assertEqual(self.rows()[-1]["outcome"], "invalidated")
        self.assertIsNotNone(self.rows()[-1]["confirmed_at"])
        self.assertIsNone(self.rows()[-1]["order_id"])

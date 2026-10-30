import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import threading
from dataclasses import replace
from unittest.mock import patch

from polybot2other.config import Settings
from polybot2other.execution import simulate_fak_buy
from polybot2other.models import MarketRound, Signal, TradeIntent
from polybot2other.storage import TradeStore
from polybot2other.main_exit import MainExitBot
from polybot2other.main_exit_store import MainExitStore
from polybot2other.main_exit_policy import checked_book, sweep_sell


class MainExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = int(time.time()) // 300 * 300 + 30.0
        self.market = MarketRound(f"btc-updown-5m-{int(self.now)-30}", "BTC", self.now-30,
                                  self.now+270, 100, up_token="up", down_token="down")
        self.base = TradeStore(self.root/"main.sqlite3", 100)
        self.addCleanup(self.base.conn.close)
        self.base.upsert_round(self.market)
        self.settings = Settings(db_path=self.root/"exit.sqlite3", strategy_experiments_enabled=False,
                                 live_trading_runtime_enabled=False, market_scout_enabled=False,
                                 llm_super_agent_enabled=False)
        self.store = MainExitStore(self.settings.db_path, 100)
        self.addCleanup(self.store.conn.close)
        with patch("polybot2other.main_exit.time.time", return_value=self.now):
            self.bot = MainExitBot(self.settings, self.store, self.base.db_path)
        self.addCleanup(self.bot.baseline.close)

    def entry(self, side="Up", opened=None):
        opened = self.now+.1 if opened is None else opened
        signal = Signal("BTC", side, .8, .5, 10, "original entry")
        intent = TradeIntent(self.market, signal, 5)
        fill = simulate_fak_buy(intent, {"best_ask": .5, "asks": [{"price": .5, "size": 1000}]}).fills[0]
        with patch("polybot2other.storage.time.time", return_value=opened):
            parent_id = self.base.place_fills([fill])[0]
        self.bot.exit_tick(opened+.1)
        return parent_id

    def quote(self, bid, at, size=1000, side="Up"):
        quote = {"token_id": side.lower(), "best_bid": bid, "best_ask": min(.999, bid+.01),
                 "bids": [{"price": bid, "size": size}], "asks": [{"price": min(.999, bid+.01), "size": 1000}],
                 "source_ms": at*1000, "received_ms": at*1000, "updated_at_ms": at*1000}
        self.bot.paper_quotes[side] = quote
        return quote

    def sell_at(self, parent_id, bid, at=None, size=1000):
        at = self.now+1 if at is None else at
        self.quote(bid, at, size)
        self.bot.exit_tick(at)
        self.quote(bid, at+.6, size)
        self.bot.exit_tick(at+.6)
        return self.store.position(parent_id)

    def test_exact_entry_cost_and_read_only_parent_and_deduplication(self):
        parent_id = self.entry()
        before = dict(self.base.conn.execute("SELECT * FROM trades WHERE id=?", (parent_id,)).fetchone())
        self.bot.exit_tick(self.now+.3)
        self.bot.exit_tick(self.now+.4)
        self.assertEqual(self.store.recent_trade_count(), 1)
        own = self.store.position(parent_id)
        for key in ("stake", "shares", "entry_price", "opened_at", "side"):
            self.assertEqual(own[key], before[key])
        self.assertEqual(self.store.account()["cash_balance"], 95)
        with self.assertRaises(sqlite3.OperationalError):
            self.bot.baseline.execute("UPDATE account SET cash_balance=0")
        self.assertEqual(dict(self.base.conn.execute("SELECT * FROM trades WHERE id=?", (parent_id,)).fetchone()), before)

    def test_take_profit_uses_net_cost_and_waits_for_latency(self):
        parent_id = self.entry()
        self.quote(.61, self.now+1)  # Raw contract price rose 22%; net gain is below 20%.
        self.bot.exit_tick(self.now+1)
        self.assertFalse(self.bot._pending)
        self.quote(.67, self.now+2)
        self.bot.exit_tick(self.now+2)
        self.assertEqual(self.bot._pending[parent_id]["reason"], "TP")
        self.bot.exit_tick(self.now+2.3)
        self.assertIsNotNone(self.store.position(parent_id))
        self.quote(.67, self.now+2.6)
        self.bot.exit_tick(self.now+2.6)
        self.assertIsNone(self.store.position(parent_id))
        row = self.store.recent_trades(1)[0]
        self.assertGreater(row["pnl"], 1)
        self.assertEqual(row["settlement_source"], "early_exit")
        sale = self.store.conn.execute("SELECT * FROM exit_sales").fetchone()
        self.assertGreater(sale["fee"], 0)
        self.assertAlmostEqual(sale["gross"]-sale["fee"]-5, row["pnl"], places=6)
        self.assertAlmostEqual(self.store.account()["cash_balance"], 100+row["pnl"], places=6)
        self.assertEqual(self.base.open_trade_count(), 1)

    def test_stop_loss_sells_before_official_outcome(self):
        parent_id = self.entry()
        self.sell_at(parent_id, .25)
        self.assertIsNone(self.store.position(parent_id))
        sale = self.store.conn.execute("SELECT * FROM exit_sales").fetchone()
        self.assertEqual(sale["reason"], "SL")
        self.assertLess(sale["pnl"], -2)
        self.assertEqual(self.store.comparison()["paired_settled"], 0)

    def test_selected_side_freshness_token_and_depth_are_enforced(self):
        self.entry()
        for change in ({"source_ms": (self.now-10)*1000}, {"received_ms": (self.now-10)*1000},
                       {"source_ms": (self.now+10)*1000}, {"token_id": "another-market"},
                       {"bids": [{"price": .95, "size": 1000}]}, {"best_ask": .3}):
            with self.subTest(change=change):
                q = self.quote(.8, self.now+1)
                q.update(change)
                self.quote(.8, self.now+1, side="Down")
                self.bot.exit_tick(self.now+1)
                self.assertFalse(self.bot._pending)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM exit_sales").fetchone()[0], 0)

    def test_price_gap_cannot_fill_at_old_trigger_price(self):
        parent_id = self.entry()
        self.quote(.8, self.now+1)
        self.bot.exit_tick(self.now+1)
        self.quote(.45, self.now+1.6)
        self.bot.exit_tick(self.now+1.6)
        self.assertIsNotNone(self.store.position(parent_id))
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM exit_sales").fetchone()[0], 0)
        self.assertIn('"event": "exit_unfilled"', self.bot.journal.read_text())

    def test_partial_fill_keeps_remainder_and_does_not_reuse_same_depth(self):
        parent_id = self.entry()
        initial = self.store.position(parent_id)
        remaining = self.sell_at(parent_id, .8, size=2)
        self.assertAlmostEqual(remaining["shares"], initial["shares"]-1, places=6)
        for second in (2.3, 3, 4):
            self.quote(.8, self.now+second, size=2)
            self.bot.exit_tick(self.now+second)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM exit_sales").fetchone()[0], 1)
        self.assertEqual(self.store.metrics()["settled_trades"], 0)
        self.store.resolve(self.market.round_id, {"outcome": "Up"}, self.market.ends_at+1)
        rows = self.store.recent_trades(10)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(x["shares"] for x in rows), initial["shares"], places=6)
        self.assertAlmostEqual(sum(x["stake"] for x in rows), 5, places=6)
        self.assertEqual(self.store.metrics()["settled_trades"], 1)
        self.assertEqual(self.store.comparison()["paired_settled"], 1)
        self.assertAlmostEqual(self.store.account()["cash_balance"], 100+sum(x["pnl"] for x in rows), places=6)

    def test_dust_is_never_silently_sold_or_discarded(self):
        parent_id = self.entry()
        original = self.store.position(parent_id)
        target = original["shares"]-.001
        self.sell_at(parent_id, .8, size=target*2)
        remaining = self.store.position(parent_id)
        self.assertIsNotNone(remaining)
        self.assertAlmostEqual(remaining["shares"], .001, places=6)
        self.assertGreater(remaining["stake"], 0)
        self.store.resolve(self.market.round_id, {"outcome": "Up"}, self.market.ends_at+1)
        self.assertAlmostEqual(sum(r["stake"] for r in self.store.recent_trades(10)), 5, places=6)

    def test_early_profit_survives_final_loss_and_comparison_waits(self):
        parent_id = self.entry()
        self.sell_at(parent_id, .8)
        pnl = self.store.recent_trades(1)[0]["pnl"]
        self.assertEqual(self.store.comparison()["paired_settled"], 0)
        # Continue collecting the original holding trajectory after the early sale.
        self.quote(.4, self.now+3)
        self.bot.exit_tick(self.now+3)
        self.assertGreater(self.store.watchers()[0]["observations"], 1)
        self.store.resolve(self.market.round_id, {"outcome": "Down"}, self.market.ends_at+1)
        self.store.resolve(self.market.round_id, {"outcome": "Down"}, self.market.ends_at+2)
        self.assertEqual(self.store.recent_trades(1)[0]["pnl"], pnl)
        stats = self.store.comparison()
        self.assertEqual(stats["saved_losses"], 1)
        self.assertEqual(stats["hold_pnl"], -5)
        self.assertAlmostEqual(stats["difference"], pnl+5, places=6)

    def test_winner_stopped_out_is_counted_as_cost(self):
        parent_id = self.entry()
        self.sell_at(parent_id, .25)
        self.store.resolve(self.market.round_id, {"outcome": "Up"}, self.market.ends_at+1)
        stats = self.store.comparison()
        self.assertEqual(stats["winners_stopped_out"], 1)
        self.assertLess(stats["difference"], 0)
        self.assertGreater(stats["profit_given_up_on_winners"], 0)

    def test_only_official_resolution_and_no_exit_after_expiry(self):
        parent_id = self.entry()
        self.quote(.8, self.market.ends_at-.1)
        self.bot.exit_tick(self.market.ends_at-.1)
        self.quote(.8, self.market.ends_at+.6)
        self.bot.exit_tick(self.market.ends_at+.6)
        self.assertIsNotNone(self.store.position(parent_id))
        self.base.settle_round_outcome(self.market.round_id, "Up", self.market.ends_at+1, settlement_source="chainlink_fallback")
        with patch.object(self.bot.polymarket, "get_resolution", return_value=None):
            self.bot.resolve_due(self.market.ends_at+2)
        self.assertIsNotNone(self.store.position(parent_id))
        with patch.object(self.bot.polymarket, "get_resolution", return_value={"outcome": "Down"}):
            self.bot.resolve_due(self.market.ends_at+3)
        self.assertEqual(self.store.recent_trades(1)[0]["pnl"], -5)

    def test_pause_cancels_pending_and_skips_paused_entries(self):
        parent_id = self.entry()
        self.quote(.8, self.now+1)
        self.bot.exit_tick(self.now+1)
        self.bot._set_paper_pause_state(True, self.now+1.1)
        self.assertFalse(self.bot._pending)
        self.quote(.8, self.now+1.7)
        self.bot.exit_tick(self.now+1.7)
        self.assertIsNotNone(self.store.position(parent_id))
        second = self.entry("Down", self.now+2)
        self.assertIsNone(self.store.position(second))
        self.assertEqual(self.store.comparison()["skipped_entries"], 1)

    def test_restart_does_not_reset_balance_policy_or_reenter(self):
        parent_id = self.entry()
        self.sell_at(parent_id, .8)
        balance = self.store.account()["cash_balance"]
        other_store = MainExitStore(self.settings.db_path, 500)
        self.addCleanup(other_store.conn.close)
        other = MainExitBot(self.settings, other_store, self.base.db_path)
        self.addCleanup(other.baseline.close)
        other.exit_tick(self.now+3)
        self.assertEqual(other_store.account()["cash_balance"], balance)
        self.assertEqual(other_store.account()["initial_balance"], 100)
        self.assertEqual(other_store.comparison()["matched_entries"], 1)
        self.assertFalse(other._pending)

    def test_mirror_transaction_rolls_back_financial_and_dedup_state(self):
        signal = Signal("BTC", "Up", .8, .5, 10, "test")
        fill = simulate_fak_buy(TradeIntent(self.market, signal, 5), {"best_ask": .5, "asks": [{"price": .5, "size": 1000}]}).fills[0]
        self.store.upsert_round(self.market)
        parent = {"id": 77, "round_id": self.market.round_id, "side": "Up", "stake": 5,
                  "shares": fill.shares, "opened_at": self.now+.1}
        with patch.object(self.store, "_insert_paper_order_for_fill", side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                self.store.mirror(parent, fill, self.now+.2)
        self.assertEqual(self.store.account()["cash_balance"], 100)
        self.assertEqual(self.store.recent_trade_count(), 0)
        self.assertEqual(self.store.run()["cursor"], 0)
        self.assertEqual(self.store.watchers(), [])
        self.assertIsNotNone(self.store.mirror(parent, fill, self.now+.3)["trade_id"])

    def test_late_entries_are_skipped_without_fabricated_fill(self):
        signal = Signal("BTC", "Up", .8, .5, 10, "late")
        fill = simulate_fak_buy(TradeIntent(self.market, signal, 5), {"best_ask": .5, "asks": [{"price": .5, "size": 1000}]}).fills[0]
        with patch("polybot2other.storage.time.time", return_value=self.now+.1):
            self.base.place_fills([fill])
        self.bot.exit_tick(self.now+20)
        self.assertEqual(self.store.recent_trade_count(), 0)
        self.assertEqual(self.store.comparison()["skipped_entries"], 1)

    def test_both_directions_follow_independent_original_entries(self):
        self.entry("Up")
        self.entry("Down", self.now+.3)
        self.assertEqual(self.store.comparison()["matched_entries"], 2)
        self.assertEqual({x["side"] for x in self.store.open_trades()}, {"Up", "Down"})

    def test_price_depth_sweep_respects_limit_and_actual_available_shares(self):
        q = self.quote(.8, self.now+1, size=2)
        q["bids"].append({"price": .7, "size": 10})
        book, error = checked_book(q, "up", self.now+1, self.bot.policy)
        self.assertIsNone(error)
        fill = sweep_sell(book, 10, self.bot.policy, .75)
        self.assertEqual(fill["shares"], 1)
        self.assertAlmostEqual(fill["avg_price"], .79)
        self.assertFalse(fill["full"])

    def test_live_tick_checks_quote_age_after_database_reads(self):
        parent_id = self.entry()
        with patch("polybot2other.main_exit.time.time", return_value=self.now+1) as clock:
            def delayed_read(*args, **kwargs):
                self.quote(.8, self.now+1.1)
                clock.return_value = self.now+1.2
            with patch.object(self.bot, "_sync_entries", side_effect=delayed_read):
                self.bot.exit_tick()
        self.assertEqual(self.bot._pending[parent_id]["reason"], "TP")
        self.assertEqual(self.bot._pending[parent_id]["at"], self.now+1.2)

    def test_entry_committed_during_read_is_not_misclassified_as_future(self):
        signal = Signal("BTC", "Up", .8, .5, 10, "concurrent entry")
        fill = simulate_fak_buy(TradeIntent(self.market, signal, 5), {"best_ask": .5, "asks": [{"price": .5, "size": 1000}]}).fills[0]
        with patch("polybot2other.storage.time.time", return_value=self.now+1.05):
            parent_id = self.base.place_fills([fill])[0]
        with patch("polybot2other.main_exit.time.time", return_value=self.now+1.1):
            self.bot._sync_entries(self.now+1, live_clock=True)
        self.assertIsNotNone(self.store.position(parent_id))
        self.assertEqual(self.store.comparison()["skipped_entries"], 0)

    def test_old_positions_and_initial_profits_are_not_imported(self):
        self.entry(opened=self.now-3)
        self.assertEqual(self.base.open_trade_count(), 1)
        self.assertEqual(self.store.recent_trade_count(), 0)
        self.assertEqual(self.store.account()["cash_balance"], 100)

    def test_live_configuration_rejected_before_parent_initialization(self):
        with patch("polybot2other.main_exit.PaperTradingBot.__init__", side_effect=AssertionError("must not initialize")):
            with self.assertRaisesRegex(ValueError, "Paper-only"):
                MainExitBot(replace(self.settings, live_trading_runtime_enabled=True), self.store, self.base.db_path)

    def test_stop_loss_partial_remainder_continues_exit_after_rebound(self):
        parent_id = self.entry()
        self.sell_at(parent_id, .25, size=2)
        self.assertEqual(self.store.position(parent_id)["exit_latch"], "SL")
        self.sell_at(parent_id, .8, at=self.now+3)
        self.assertIsNone(self.store.position(parent_id))
        self.assertEqual([x[0] for x in self.store.conn.execute("SELECT reason FROM exit_sales")], ["SL", "SL"])

    def test_fast_pause_resume_has_one_new_generation_of_workers(self):
        started = []
        lock = threading.Lock()
        def worker():
            with lock:
                started.append(threading.current_thread().name)
            self.bot._stop.wait(3)
        with patch.object(self.bot, "_exit_loop", side_effect=worker), patch.object(self.bot, "_book_loop", side_effect=worker), patch.object(self.bot, "_resolution_loop", side_effect=worker):
            self.bot.start()
            deadline = time.monotonic()+2
            while len(started)<3 and time.monotonic()<deadline:
                time.sleep(.01)
            self.bot.request_pause()
            self.bot.start()
            deadline = time.monotonic()+2
            while len(started)<6 and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(len(started), 6)
            self.assertFalse(self.bot._stop.is_set())
            self.bot.stop()
            self.assertTrue(self.bot._stop.is_set())


if __name__ == "__main__":
    unittest.main()

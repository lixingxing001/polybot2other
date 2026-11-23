"""The shared live manager runs against a local wallet ledger and a fake exchange."""
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

import test_managed_live as fixtures
from polybot2other.managed_live import ManagedLiveAccounts
from polybot2other.managed_live_signals import RealBtcFiveMinuteStrategy
from polybot2other.managed_live_store import ManagedLiveStore
from polybot2other.models import Signal
from polybot2other.optimized_reference import SPOT


STRATEGY = "MAIN_FIRST_STRONG"


class FirstStrongLiveTests(unittest.TestCase):
    def setUp(self):
        fixtures.ManagedLiveTests.setUp(self)
        del self.manager.signals.evaluate  # Exercise the real routing, gates and pre-submit check.
        observe = self.feed.observation

        def market_observation():
            market, ref, books, now = observe()
            ref["latest"] = {SPOT: {"price": 100.12, "source_ms": now * 1000}}
            return market, ref, books, now

        self.feed.observation = market_observation
        self.signal = Signal("BTC", "Up", .65, .4, 12, "fixture main signal")
        patcher = patch.object(RealBtcFiveMinuteStrategy, "signal", return_value=self.signal)
        self.score = patcher.start()
        self.addCleanup(patcher.stop)

    def enable(self):
        self.manager.update_settings({"enabled": True, "strategies": {STRATEGY: {"enabled": True}}})

    def trade(self):
        self.manager.tick()
        self.assertTrue(self.store.active_orders(STRATEGY), self.manager.last_signal.get(STRATEGY))
        return self.store.active_orders(STRATEGY)[0]

    def config(self):
        return self.store.config()["strategies"][STRATEGY]

    def test_migration_and_maximum_slots(self):
        old = self.store.config()
        old["strategies"].pop(STRATEGY)
        old["enabled"] = True
        old["strategies"]["FORECAST_EXIT_SETTLE"].update(enabled=True, stake_dollars=10)
        with self.store.conn:
            self.store.conn.execute("UPDATE live_config SET json=? WHERE id=1", (json.dumps(old),))
        reopened = ManagedLiveStore(self.store.path)
        self.addCleanup(reopened.conn.close)
        migrated = reopened.config()
        strategy = migrated["strategies"].pop(STRATEGY)
        self.assertEqual(migrated["strategies"], old["strategies"])
        self.assertTrue(migrated["enabled"])
        self.assertEqual((strategy["enabled"], strategy["stake_dollars"], strategy["max_open_trades"],
                          strategy["max_daily_loss"]), (False, 5, 2, 15))
        with self.assertRaises(ValueError):
            self.manager.update_settings({"strategies": {STRATEGY: {"max_open_trades": 3}}})
        self.manager.update_settings({"strategies": {STRATEGY: {"max_open_trades": 1}}})
        self.assertEqual(self.config()["max_open_trades"], 1)
        self.assertFalse(self.config()["enabled"])
        self.assertFalse(reopened.config()["strategies"][STRATEGY]["enabled"])

    def test_direction_and_score_must_still_qualify_at_submit(self):
        self.manager.tick()
        self.assertFalse(self.exchange.prepared)
        self.enable()
        self.score.return_value = replace(self.signal, confidence=.6499)
        self.manager.tick()
        self.assertFalse(self.exchange.prepared)
        self.score.side_effect = [self.signal, replace(self.signal, confidence=.6499)]
        self.manager.tick()
        self.assertFalse(self.exchange.submitted)
        self.score.side_effect = [self.signal, replace(self.signal, side="Down")]
        self.manager.tick()
        self.assertFalse(self.exchange.submitted)
        self.score.side_effect = [self.signal, self.signal]
        self.trade()
        self.assertEqual([(o.amount, o.action) for o in self.exchange.submitted], [(5, "BUY")])

    def test_same_main_score_only_gates_the_new_strategy(self):
        self.manager.update_settings({"enabled": True, "strategies": {
            "MAIN": {"enabled": True}, STRATEGY: {"enabled": True},
        }})
        self.score.return_value = replace(self.signal, confidence=.64)
        self.manager.tick()
        self.assertEqual([o["strategy_id"] for o in self.store.active_orders()], ["MAIN"])
        main_order = self.store.active_orders("MAIN")[0]
        fixtures.ManagedLiveTests.confirm(self, main_order, 10, .4)
        self.score.return_value = self.signal
        self.trade()
        self.assertEqual(len(self.store.positions("MAIN")), 1)
        self.assertEqual(len(self.store.active_orders(STRATEGY)), 1)

    def test_turning_off_during_preparation_prevents_submission(self):
        self.enable()
        self.exchange.prepare_callback = lambda: self.manager.update_settings({
            "strategies": {STRATEGY: {"enabled": False}},
        })
        self.manager.tick()
        self.assertTrue(self.exchange.prepared)
        self.assertFalse(self.exchange.submitted)
        self.assertFalse(self.config()["enabled"])
        self.assertFalse(self.store.active_orders(STRATEGY))

    def test_pending_and_partial_buys_lock_both_sides_while_proven_zero_may_retry(self):
        self.enable()
        now = time.time()
        order = self.trade()
        self.score.return_value = replace(self.signal, side="Down")
        with patch("time.time", return_value=now + 5):
            self.manager.tick()
            self.assertEqual(len(self.exchange.submitted), 1)
            self.exchange.responses[order["external_id"]] = {"fills": [], "terminal": True, "no_fill": True}
            self.manager.reconcile()
            self.assertEqual(self.store.order(order["id"])["state"], "CANCELED")
            retry = self.trade()
            self.assertEqual(retry["outcome"], "Down")
            self.assertEqual(len(self.exchange.submitted), 2)
            fixtures.ManagedLiveTests.confirm(self, retry, 4, .4, terminal=False)
            self.assertEqual(self.store.positions(STRATEGY)[0]["initial_shares"], 4)
            self.assertTrue(self.manager._entry_restriction(STRATEGY, self.config(), self.market, "Up"))
            fixtures.ManagedLiveTests.confirm(self, retry, 4, .4, terminal=True)
        with patch("time.time", return_value=now + 10):
            self.score.return_value = self.signal
            self.manager.tick()
        self.assertEqual(len(self.exchange.submitted), 2)
        self.assertTrue(self.manager._entry_restriction(STRATEGY, self.config(), self.market, "Up"))
        # A fresh process has no in-memory retry/round locks. It must consult the persisted fill.
        self.manager.process_lock.release()
        reopened = ManagedLiveStore(self.store.path)
        self.addCleanup(reopened.conn.close)
        restored = ManagedLiveAccounts(self.settings, feed=self.feed, exchange=self.exchange, store=reopened)
        self.assertIsNone(restored.process_lock.acquire())
        self.addCleanup(restored.stop)
        restored.wallet, restored.geo, restored.open_orders_at = self.manager.wallet, self.manager.geo, now
        restored.tick()
        self.assertEqual(len(self.exchange.submitted), 2)
        self.assertTrue(restored._entry_restriction(STRATEGY, self.config(), self.market, "Up"))

    def test_full_fill_waits_for_official_resolution_and_has_its_own_curve(self):
        self.enable()
        order = self.trade()
        fixtures.ManagedLiveTests.confirm(self, order, 10, .4)
        self.assertTrue(self.manager._entry_restriction(STRATEGY, self.config(), self.market, "Down"))
        self.manager.update_settings({"enabled": False})
        self.feed.bid = self.exchange.bid = .03
        self.feed.ask = self.exchange.ask = .04
        self.manager.tick()
        self.assertEqual([o.action for o in self.exchange.submitted], ["BUY"])
        with patch("time.time", return_value=self.market.ends_at + 5):
            self.manager.reconcile()
            self.assertTrue(self.store.positions(STRATEGY, open_only=True))
            self.feed.polymarket.get_resolution.return_value = {
                "outcome": "Up", "market_slug": self.market.round_id, "price_source": "Gamma:outcomePrices",
            }
            self.manager.reconcile()
        position = self.store.positions(STRATEGY)[0]
        self.assertAlmostEqual(position["realized_pnl"], 5.832)
        variants = {v["variant_id"]: v for v in self.store.snapshot()["variants"]}
        self.assertEqual(variants["FORECAST_EXIT_SETTLE"]["settled_pnl_curve"], [])
        self.assertEqual(variants[STRATEGY]["settled_pnl_curve"], [
            {"created_at": position["opened_at"], "realized_pnl": 0},
            {"created_at": position["closed_at"], "realized_pnl": position["realized_pnl"]},
        ])

    def test_pending_and_open_from_different_markets_consume_both_slots(self):
        self.enable()
        first = self.trade()
        fixtures.ManagedLiveTests.confirm(self, first, 10, .4)
        self.feed.market = replace(self.market, round_id="second", up_token="up2", down_token="down2")
        self.trade()
        usage = self.store.risk_usage(STRATEGY)
        self.assertEqual((usage["slots"], usage["pending_slots"]), (2, 1))
        self.feed.market = replace(self.market, round_id="third", up_token="up3", down_token="down3")
        self.manager.tick()
        self.assertEqual([o.action for o in self.exchange.submitted], ["BUY", "BUY"])

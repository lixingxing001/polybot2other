"""Manual loss-window resets use temporary ledgers and a fake exchange only."""
from dataclasses import replace
from http.server import ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

from polybot2other.managed_live_store import ManagedLiveStore
from polybot2other.web import Handler
import test_managed_live as fixtures


class LossLimitResetTests(unittest.TestCase):
    setUp = fixtures.ManagedLiveTests.setUp

    def position(self, key, at):
        serial = len(self.store.positions()) + 1
        market = replace(self.market, round_id=f"reset-test-{serial}", started_at=at,
                         ends_at=at+300, up_token=f"up-{serial}", down_token=f"down-{serial}")
        self.store.save_market(market)
        order = self.store.prepare_order(f"reset-buy-{serial}", key, market, market.up_token,
                                        "Up", "BUY", .5, cash=5, now=at)
        self.store.apply_confirmed_fill(order["id"], f"buy-{serial}", 10, .4, .1, "test", now=at, notional=4)
        self.store.set_order_state(order["id"], "FILLED")
        return self.store.positions(key)[-1], market

    def loss(self, key, at):
        position, market = self.position(key, at)
        self.store.resolve(market.round_id, "Down", now=at+10)
        return position, market

    def test_default_window_stays_rolling_24_hours(self):
        now = time.time()
        self.loss("MAIN_OPTIMIZED", now-86500)
        self.loss("MAIN_OPTIMIZED", now-100)
        status = self.store.loss_limit_status("MAIN_OPTIMIZED", now)
        self.assertIsNone(status["reset_at"])
        self.assertEqual(status["window_start"], now-86400)
        self.assertAlmostEqual(status["realized_pnl"], -4.1)

    def test_reset_is_scoped_persistent_and_preserves_ledger_and_limit(self):
        now = time.time()
        self.loss("MAIN_OPTIMIZED", now-100)
        self.loss("MAIN", now-100)
        self.store.update_config({"strategies": {"MAIN_OPTIMIZED": {"max_daily_loss": 70}}})
        before = self.store.config()
        positions = self.store.positions()
        rows = {table: list(self.store.conn.execute(f"SELECT * FROM {table}")) for table in ("orders", "fills")}
        self.store.reset_loss_limit("MAIN_OPTIMIZED", now=now)
        self.assertEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now), 0)
        self.assertAlmostEqual(self.store.daily_pnl("MAIN", now), -4.1)
        self.assertEqual(self.store.positions(), positions)
        for table, saved in rows.items():
            self.assertEqual(list(self.store.conn.execute(f"SELECT * FROM {table}")), saved)
        after = self.store.config()
        self.assertEqual(after["strategies"], before["strategies"])
        self.assertEqual(after["enabled"], before["enabled"])
        self.assertEqual(after["revision"], before["revision"]+1)
        reopened = ManagedLiveStore(self.store.path)
        self.addCleanup(reopened.conn.close)
        self.assertEqual(reopened.loss_limit_status("MAIN_OPTIMIZED", now)["reset_at"], now)
        self.assertEqual(reopened.daily_pnl("MAIN_OPTIMIZED", now), 0)
        snapshot = next(v for v in reopened.snapshot()["variants"] if v["variant_id"] == "MAIN_OPTIMIZED")
        self.assertAlmostEqual(snapshot["metrics"]["realized_pnl"], -4.1)
        self.assertEqual(snapshot["loss_limit"]["realized_pnl"], 0)
        self.assertEqual(reopened.conn.execute("SELECT count(*) FROM events WHERE kind='loss_limit_reset'").fetchone()[0], 1)

    def test_pre_reset_partial_exit_is_not_recounted_after_settlement(self):
        now = time.time()
        position, market = self.position("MAIN_OPTIMIZED", now-100)
        sell = self.store.prepare_order("partial-exit", "MAIN_OPTIMIZED", market, market.up_token,
                                        "Up", "SELL", .2, shares=5, position_id=position["id"], now=now-50)
        self.store.apply_confirmed_fill(sell["id"], "partial", 5, .2, .05, "test", now=now-50, notional=1)
        self.store.set_order_state(sell["id"], "FILLED")
        self.assertAlmostEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now), -1.1)
        self.store.reset_loss_limit("MAIN_OPTIMIZED", now=now)
        self.assertEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now), 0)
        self.assertAlmostEqual(self.store.risk_usage("MAIN_OPTIMIZED")["loss_exposure"], 2.05)
        self.store.resolve(market.round_id, "Down", now=now+10)
        self.assertAlmostEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now+10), -2.05)
        self.assertAlmostEqual(self.store.position(position["id"])["realized_pnl"], -3.15)

    def test_existing_holdings_and_uncertain_budget_still_block_entry(self):
        now = time.time()
        self.loss("MAIN_OPTIMIZED", now-200)
        self.position("MAIN_OPTIMIZED", now-100)
        pending_market = replace(self.market, round_id="pending-reset", up_token="pending-up")
        self.store.save_market(pending_market)
        pending = self.store.prepare_order("pending-reset-order", "MAIN_OPTIMIZED", pending_market,
                                           "pending-up", "Up", "BUY", .5, cash=5)
        self.store.set_order_state(pending["id"], "QUARANTINED")
        self.store.update_config({"strategies": {"MAIN_OPTIMIZED": {"max_daily_loss": 10, "max_open_trades": 3}}})
        risk = self.store.risk_usage("MAIN_OPTIMIZED")
        snapshot = self.manager.reset_loss_limit("MAIN_OPTIMIZED")
        self.assertEqual(self.store.daily_pnl("MAIN_OPTIMIZED"), 0)
        self.assertEqual(self.store.risk_usage("MAIN_OPTIMIZED"), risk)
        self.assertAlmostEqual(risk["loss_exposure"], 9.1)
        self.assertEqual(risk["slots"], 2)
        self.assertEqual(self.store.order(pending["id"])["state"], "QUARANTINED")
        rule = self.store.config()["strategies"]["MAIN_OPTIMIZED"]
        self.assertIn("剩余风控额度不足", self.manager._entry_limits("MAIN_OPTIMIZED", rule))
        self.assertEqual(self.exchange.submitted, [])
        self.assertIsNotNone(next(v for v in snapshot["variants"] if v["variant_id"] == "MAIN_OPTIMIZED")["loss_limit"]["reset_at"])

    def test_reset_releases_old_loss_block_but_later_loss_counts(self):
        now = time.time()
        self.loss("MAIN_OPTIMIZED", now-100)
        self.store.update_config({"strategies": {"MAIN_OPTIMIZED": {"max_daily_loss": 5}}})
        rule = self.store.config()["strategies"]["MAIN_OPTIMIZED"]
        self.assertIn("剩余风控额度不足", self.manager._entry_limits("MAIN_OPTIMIZED", rule))
        self.manager.reset_loss_limit("MAIN_OPTIMIZED")
        self.assertIsNone(self.manager._entry_limits("MAIN_OPTIMIZED", rule))
        self.loss("MAIN_OPTIMIZED", time.time())
        self.assertAlmostEqual(self.store.daily_pnl("MAIN_OPTIMIZED", time.time()+20), -4.1)
        self.assertIn("剩余风控额度不足", self.manager._entry_limits("MAIN_OPTIMIZED", rule))
        self.assertEqual(self.exchange.submitted, [])

    def test_repeat_reset_and_window_after_24_hours(self):
        now = time.time()
        self.store.reset_loss_limit("MAIN_OPTIMIZED", now=now)
        self.loss("MAIN_OPTIMIZED", now+10)
        self.assertAlmostEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now+30), -4.1)
        self.store.reset_loss_limit("MAIN_OPTIMIZED", now=now+30)
        self.assertEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now+30), 0)
        self.loss("MAIN_OPTIMIZED", now+40)
        self.assertAlmostEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now+60), -4.1)
        later = now+86500
        self.loss("MAIN_OPTIMIZED", later-100)
        status = self.store.loss_limit_status("MAIN_OPTIMIZED", later)
        self.assertEqual(status["window_start"], later-86400)
        self.assertEqual(status["reset_at"], now+30)
        self.assertAlmostEqual(status["realized_pnl"], -4.1)

    def test_same_timestamp_closed_loss_is_cleared(self):
        now = time.time()
        self.loss("MAIN_OPTIMIZED", now-10)
        self.store.reset_loss_limit("MAIN_OPTIMIZED", now=now)
        self.assertEqual(self.store.daily_pnl("MAIN_OPTIMIZED", now), 0)

    def test_reset_endpoint_requires_control_token_and_valid_strategy(self):
        self.loss("MAIN_OPTIMIZED", time.time()-100)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.bot = SimpleNamespace(managed_live=self.manager)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        valid = {"Content-Type": "application/json", "X-Live-Control": self.manager.control_token, "Origin": base}
        invalid_requests = [
            ({"strategy_id": "MAIN_OPTIMIZED"}, {"Content-Type": "application/json"}, 403),
            ({"strategy_id": "MAIN_OPTIMIZED"}, {**valid, "Origin": "https://unrelated.example"}, 403),
            ({"strategy_id": "MAIN_OPTIMIZED"}, {**valid, "Host": "unrelated.example"}, 403),
            ({"strategy_id": "MAIN_OPTIMIZED", "max_daily_loss": 100}, valid, 400),
            ({"strategy_id": "MAIN_TP_SL"}, valid, 400),
            ({"strategy_id": ["MAIN_OPTIMIZED"]}, valid, 400),
            ([], valid, 400),
        ]
        for payload, headers, code in invalid_requests:
            request = urllib.request.Request(base+"/api/managed-live/reset-loss-limit",
                                             data=json.dumps(payload).encode(), headers=headers, method="POST")
            with self.subTest(payload=payload, code=code), self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(request)
            self.assertEqual(caught.exception.code, code)
            self.assertIsNone(self.store.loss_limit_status("MAIN_OPTIMIZED")["reset_at"])
        before = self.store.config()
        request = urllib.request.Request(base+"/api/managed-live/reset-loss-limit",
                                         data=b'{"strategy_id":"MAIN_OPTIMIZED"}', headers=valid, method="POST")
        with opener.open(request) as response:
            result = json.load(response)
        selected = next(v for v in result["variants"] if v["variant_id"] == "MAIN_OPTIMIZED")
        self.assertEqual(selected["loss_limit"]["realized_pnl"], 0)
        self.assertIsNotNone(selected["loss_limit"]["reset_at"])
        self.assertAlmostEqual(selected["metrics"]["realized_pnl"], -4.1)
        self.assertEqual(result["settings"]["strategies"], before["strategies"])
        self.assertEqual(result["enabled"], before["enabled"])
        self.assertEqual(self.exchange.submitted, [])

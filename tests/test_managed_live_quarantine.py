"""Uncertain real-order handling with fake clients and temporary ledgers only."""
from dataclasses import replace
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from py_clob_client_v2.exceptions import PolyApiException

from polybot2other.managed_live import ManagedLiveAccounts
from polybot2other.managed_live_client import ManagedExchange, OrderConfirmationPending
from polybot2other.managed_live_store import ManagedLiveStore
import test_managed_live as fixtures

exchange_info=fixtures.exchange_info


class QuarantineTests(unittest.TestCase):
    setUp=fixtures.ManagedLiveTests.setUp
    enable=fixtures.ManagedLiveTests.enable
    buy=fixtures.ManagedLiveTests.buy
    confirm=fixtures.ManagedLiveTests.confirm

    def set_time(self,now):
        if not hasattr(self,"clock"):
            replacement=patch("time.time",return_value=now)
            self.clock=replacement.start();self.addCleanup(replacement.stop)
        else:self.clock.return_value=now
        self.manager.wallet.update(ready=True,checked_at=now,read_started_at=now)
        self.manager.geo.update(ready=True,blocked=False,checked_at=now)
        self.manager.open_orders_at=now

    def pending(self):
        self.enable();self.manager.tick()
        return self.store.active_orders()[0]

    def isolate(self,order):
        self.set_time(max(time.time(),order["submitted_at"]+61))
        self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")

    def next_round(self):
        now=max(time.time()+1,self.market.ends_at+1);self.set_time(now)
        self.market=replace(self.market,round_id=f"btc-updown-5m-{int(now)}",started_at=now-1,
                            ends_at=now+240,up_token=f"up-{int(now)}",down_token=f"down-{int(now)}")
        self.feed.market=self.market

    def adapter(self):
        exchange=ManagedExchange(self.settings);self.addCleanup(exchange.http.close)
        sdk=exchange.legacy._sdk();client=Mock()
        client.get_order.return_value=None;client.get_trades.return_value=[];client.get_open_orders.return_value=[]
        client.get_balance_allowance.return_value={"balance":"0","allowance":"0"}
        return exchange,sdk,client

    def test_isolation_keeps_budget_and_blocks_same_market_for_all_strategies(self):
        order=self.pending();self.isolate(order)
        self.assertEqual(self.manager.available_cash(),95)
        self.assertIsNone(self.manager._access_block(require_wallet=True))
        self.manager.update_settings({"strategies":{"MAIN_OPTIMIZED":{"enabled":True}}})
        self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),1)
        self.assertIn("本市场存在隔离订单",self.manager.last_signal["MAIN"]["reason"])
        self.assertIn("本市场存在隔离订单",self.manager.last_signal["MAIN_OPTIMIZED"]["reason"])
        with self.assertRaisesRegex(ValueError,"隔离订单"):
            self.store.prepare_order("another-id","VALUE_CONFIRM_V1",self.market,self.market.up_token,"Up","BUY",.5,cash=5)

    def test_other_market_can_enter_within_existing_limits(self):
        old=self.pending();self.isolate(old);self.next_round();self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),2)
        self.assertEqual(self.store.order(old["id"])["state"],"QUARANTINED")
        risk=self.store.risk_usage("MAIN")
        self.assertEqual(risk["slots"],2);self.assertEqual(risk["loss_exposure"],10)
        self.assertEqual(risk["quarantined_cash"],5);self.assertEqual(self.manager.available_cash(),90)
        self.assertEqual(self.store.positions(),[])

    def test_two_uncertain_orders_exhaust_owner_slots_but_not_other_strategy(self):
        first=self.pending();self.isolate(first);self.next_round();self.manager.tick()
        second=next(o for o in self.store.active_orders() if o["id"]!=first["id"])
        self.isolate(second);self.next_round()
        self.manager.update_settings({"strategies":{"MAIN_OPTIMIZED":{"enabled":True}}})
        self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),3)
        self.assertEqual(self.store.risk_usage("MAIN")["slots"],2)
        self.assertIn("持仓数量上限",self.manager.last_signal["MAIN"]["reason"])
        self.assertEqual(len(self.store.active_orders("MAIN_OPTIMIZED")),1)

    def test_unknown_budget_counts_against_daily_limit(self):
        order=self.pending();self.isolate(order);self.next_round()
        self.manager.update_settings({"strategies":{"MAIN":{"max_daily_loss":6}}})
        self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),1)
        self.assertIn("剩余风控额度不足",self.manager.last_signal["MAIN"]["reason"])

    def test_limits_are_rechecked_after_signing(self):
        order=self.pending();self.isolate(order);self.next_round()
        def consume_slot():
            extra=self.store.prepare_order("0x"+"b"*64,"MAIN",replace(self.market,round_id="other-pending-market"),self.market.up_token,"Up","BUY",.5,cash=5)
            self.store.set_order_state(extra["id"],"QUARANTINED")
        self.exchange.prepare_callback=consume_slot
        self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),1)
        self.assertIn("持仓数量上限",self.manager.last_signal["MAIN"]["reason"])

    def test_unhealthy_wallet_or_open_orders_cannot_enable_isolation(self):
        order=self.pending();self.set_time(order["submitted_at"]+61)
        self.manager.wallet["ready"]=False;self.manager.reconcile()
        self.assertNotEqual(self.store.order(order["id"])["state"],"QUARANTINED")
        self.manager.wallet["ready"]=True;self.manager.open_orders_at=time.time()-30
        self.manager.reconcile();self.assertNotEqual(self.store.order(order["id"])["state"],"QUARANTINED")
        self.manager.open_orders_at=time.time();self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")

    def test_short_confirmation_grace_still_blocks_before_sixty_seconds(self):
        order=self.pending()
        with patch.object(self.exchange,"reconcile",side_effect=OrderConfirmationPending("等待确认")):
            self.set_time(order["submitted_at"]+59.9);self.manager.reconcile()
            self.assertEqual(self.store.order(order["id"])["state"],"UNKNOWN")
            self.assertIsNotNone(self.manager._access_block(require_wallet=True))
            self.set_time(order["submitted_at"]+60);self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")
        self.assertIsNone(self.manager._access_block(require_wallet=True))

    def test_integrity_failure_never_becomes_quarantined(self):
        order=self.pending();self.set_time(order["submitted_at"]+61)
        with patch.object(self.exchange,"reconcile",side_effect=ValueError("成交的资产与本地订单不一致")):
            self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"UNKNOWN")
        self.assertIn("资产",self.manager._fault)

    def test_partial_fill_uses_one_slot_and_one_full_budget(self):
        order=self.pending();self.confirm(order,4,.4,terminal=False)
        risk=self.store.risk_usage("MAIN")
        self.assertEqual(risk["slots"],1);self.assertEqual(risk["loss_exposure"],5)
        self.assertAlmostEqual(risk["unconfirmed_budget"],3.3328)
        self.isolate(order)
        self.exchange.responses[order["external_id"]]={"fills":[
            {"trade_id":"trade-1","shares":4,"price":.4,"fee":.0672,"fee_source":"official_market_fee_schedule"},
            {"trade_id":"trade-2","shares":6,"price":.4,"fee":.1008,"fee_source":"official_market_fee_schedule"}],
            "terminal":True,"no_fill":False}
        self.manager.reconcile();self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"FILLED")
        risk=self.store.risk_usage("MAIN")
        self.assertEqual(risk["slots"],1);self.assertEqual(risk["quarantined_count"],0)
        self.assertAlmostEqual(risk["loss_exposure"],4.168)
        self.assertEqual(self.store.positions()[0]["initial_shares"],10)

    def test_confirmed_no_fill_releases_isolated_risk_only_on_evidence(self):
        order=self.pending();self.isolate(order)
        self.exchange.responses[order["external_id"]]={"fills":[],"terminal":True,"no_fill":True}
        self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"CANCELED")
        self.assertEqual(self.store.risk_usage("MAIN")["slots"],0)
        self.assertEqual(self.manager.available_cash(),100)

    def test_late_confirmed_fill_settles_once_without_inventing_cash(self):
        order=self.pending();self.isolate(order);self.next_round()
        self.feed.polymarket.get_resolution.return_value={"outcome":"Up"}
        self.confirm(order,10,.4)
        self.manager.reconcile()
        position=self.store.positions()[0]
        self.assertEqual(position["status"],"REDEEMABLE")
        self.assertEqual(position["claim_due"],10)
        self.assertAlmostEqual(position["realized_pnl"],5.832)
        self.assertEqual(self.store.risk_usage("MAIN")["slots"],0)
        self.assertEqual(self.manager.wallet["balance"],100)

    def test_restart_keeps_isolation_timestamp_budget_and_existing_columns(self):
        order=self.pending();self.isolate(order);original=self.store.order(order["id"])
        with self.store.conn:self.store.conn.execute("ALTER TABLE orders ADD COLUMN source_signal_id TEXT")
        reopened=ManagedLiveStore(self.store.path);self.addCleanup(reopened.conn.close)
        other=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=reopened)
        same=reopened.order(order["id"])
        self.assertEqual(same["state"],"QUARANTINED")
        self.assertEqual(same["quarantined_at"],original["quarantined_at"])
        self.assertEqual(reopened.reserved_cash(time.time()+1),5)
        self.assertEqual(reopened.risk_usage("MAIN")["slots"],1)
        self.assertIn("source_signal_id",same);self.assertEqual(len(self.exchange.submitted),1)

    def test_old_isolated_order_stays_visible_beyond_recent_fifty(self):
        order=self.pending();self.isolate(order)
        for i in range(51):
            new=self.store.prepare_order(f"historical-{i}","MAIN",replace(self.market,round_id=f"other-{i}"),"up","Up","BUY",.5,cash=5)
            self.store.set_order_state(new["id"],"REJECTED")
        row=self.store.snapshot()["variants"][0]
        self.assertIn(order["id"],{o["id"] for o in row["orders"]})
        self.assertEqual(len(self.store.snapshot()["quarantined_orders"]),1)

    def test_quarantined_sell_is_not_resubmitted_or_counted_twice(self):
        position=self.buy();sale=self.manager.close_position("MAIN",position["id"])
        self.isolate(sale)
        self.assertEqual(self.store.risk_usage("MAIN")["slots"],1)
        self.assertEqual(self.store.risk_usage("MAIN")["quarantined_cash"],0)
        self.manager.close_position("MAIN",position["id"])
        self.assertEqual(len(self.exchange.submitted),2)

    def test_missing_order_still_reads_other_sources_without_inventing_no_fill(self):
        e,sdk,c=self.adapter();order={"external_id":"0x"+"c"*64,"token_id":"up","created_at":time.time()}
        with patch.object(e,"client",return_value=(sdk,c)):
            result=e.reconcile(order,self.market,exchange_info())
        c.get_trades.assert_called_once();c.get_open_orders.assert_called_once();c.get_balance_allowance.assert_called_once()
        self.assertFalse(result["terminal"]);self.assertFalse(result["no_fill"])
        self.assertEqual(result["evidence"]["token_balance"],0)
        self.assertFalse(result["evidence"]["matching_open_order"])

    def test_failed_cross_checks_are_reported_as_unavailable_not_empty(self):
        e,sdk,c=self.adapter();order={"external_id":"0x"+"c"*64,"token_id":"up","created_at":time.time()}
        c.get_trades.side_effect=TimeoutError;c.get_open_orders.side_effect=TimeoutError
        with patch.object(e,"client",return_value=(sdk,c)):
            result=e.reconcile(order,self.market,exchange_info())
        self.assertEqual(result["evidence"]["trades"],"unavailable")
        self.assertEqual(result["evidence"]["open_orders"],"unavailable")
        self.assertFalse(result["terminal"])

    def test_incomplete_open_orders_do_not_validate_wallet_reservations(self):
        e,sdk,c=self.adapter();c.get_open_orders.return_value=[{}]
        with patch.object(e,"client",return_value=(sdk,c)),self.assertRaisesRegex(ValueError,"字段不完整"):
            e.read_open_orders()

    def test_missing_lookup_accounts_confirmed_fills_but_waits_without_ack(self):
        e,sdk,c=self.adapter();order={"external_id":"0x"+"d"*64,"token_id":"up","created_at":time.time()}
        c.get_trades.return_value=[{"id":"receipt","taker_order_id":order["external_id"],"asset_id":"up","status":"CONFIRMED","size":"10","price":".4"}]
        with patch.object(e,"client",return_value=(sdk,c)):
            result=e.reconcile(order,self.market,exchange_info())
        self.assertEqual(result["fills"][0]["shares"],10)
        self.assertFalse(result["terminal"])

    def test_saved_fak_ack_and_all_final_receipts_can_close_missing_lookup(self):
        e,sdk,c=self.adapter();order={"external_id":"0x"+"e"*64,"token_id":"up","created_at":time.time(),
                                     "submission_json":json.dumps({"accepted":True,"status":"matched","trade_ids":["one","two"]})}
        receipt=lambda ident,status:{"id":ident,"taker_order_id":order["external_id"],"asset_id":"up","status":status,"size":"5","price":".4"}
        with patch.object(e,"client",return_value=(sdk,c)):
            c.get_trades.return_value=[receipt("one","CONFIRMED"),receipt("two","MINED")]
            self.assertFalse(e.reconcile(order,self.market,exchange_info())["terminal"])
            c.get_trades.return_value=[receipt("one","CONFIRMED"),receipt("two","CONFIRMED")]
            result=e.reconcile(order,self.market,exchange_info())
        self.assertTrue(result["terminal"]);self.assertFalse(result["no_fill"])
        self.assertEqual(sum(f["shares"] for f in result["fills"]),10)

    def test_definite_fak_rejection_releases_budget_and_saves_sanitized_cause(self):
        e,sdk,c=self.adapter();self.manager.exchange=e
        order=self.store.prepare_order("0x"+"f"*64,"MAIN",self.market,"up","Up","BUY",.5,cash=5)
        c.post_order.side_effect=PolyApiException(httpx.Response(400,json={"error":"no orders found to match with FAK order; private-test-marker"}))
        with patch.object(e,"client",return_value=(sdk,c)),patch.dict("os.environ",{"POLYBOT2OTHER_LIVE_API_KEY":"private-test-marker"}):
            self.manager._submit(order,SimpleNamespace(external_id=order["external_id"],signed=object()))
        current=self.store.order(order["id"])
        self.assertEqual(current["state"],"REJECTED");self.assertEqual(self.manager.available_cash(),100)
        self.assertEqual(json.loads(current["submission_json"])["error_code"],"FAK_NO_MATCH")
        self.assertNotIn("private-test-marker",current["submission_json"])
        c.post_order.assert_called_once()

    def test_ambiguous_errors_and_duplicate_responses_never_release_budget(self):
        for code,body in [(500,{"error":"no orders found to match with FAK order"}),(400,{"error":"unknown failure"}),
                          (400,{"success":False,"error":"duplicate order"}),(429,{"error":"Too Many Requests"})]:
            with self.subTest(code=code,body=body):
                self.assertIsNone(ManagedExchange._definite_rejection(body,code))
        self.assertTrue(ManagedExchange._definite_rejection({"error":"order timed out"},500)["rejected"])
        self.assertIsNone(ManagedExchange._definite_rejection({"error":"order timed out after matching"},500))


if __name__=="__main__":unittest.main()

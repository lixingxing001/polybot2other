"""Live paths are tested only with a fake exchange; no funded orders are submitted."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import math
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from polybot2other.config import Settings
from polybot2other.models import MarketRound, Signal
from polybot2other.managed_live import ManagedLiveAccounts
from polybot2other.managed_live_client import ManagedExchange, ManagedClobClient, OrderConfirmationPending, fee_amount, maximum_fee_ratio, fixed_collateral
from polybot2other.managed_live_store import ManagedLiveStore
from polybot2other.web import Handler


class FakeFeed:
    def __init__(self, market):
        self.market=market;self.bid=.39;self.ask=.40;self.age=0
        self.polymarket=SimpleNamespace(get_resolution=Mock(return_value=None))
        self.references=SimpleNamespace()
    def observation(self):
        now=time.time()
        quotes={side:{"token_id":token,"best_bid":self.bid,"best_ask":self.ask,
                      "source_ms":now*1000-self.age,"received_ms":now*1000-self.age,
                      "bids":[{"price":self.bid,"size":1000}],"asks":[{"price":self.ask,"size":1000}]}
                for side,token in (("Up",self.market.up_token),("Down",self.market.down_token))}
        return self.market,{"ready":True,"protocol_verified":True,"latest":{}},quotes,now
    def start(self):pass
    def stop(self):pass
    def request_pause(self):pass


class FakeExchange:
    identity="test-wallet-never-real"
    def __init__(self):
        self.prepared=[];self.submitted=[];self.fail_post=False;self.prepare_callback=None
        self.responses={};self.token_shares=1000;self.bid=.39;self.ask=.4
    def market_info(self, market, force=False):
        return {"fee_rate":.07,"fee_exponent":1,"tick_size":"0.01","neg_risk":False}
    def rest_book(self, token):
        now=time.time()
        return {"token_id":token,"best_bid":self.bid,"best_ask":self.ask,"source_ms":now*1000,"received_ms":now*1000,
                "asks":[{"price":self.ask,"size":1000}],"bids":[{"price":self.bid,"size":1000}],"min_order_size":5}
    def valuation_book(self, token):
        return self.rest_book(token)
    def prepare(self, market, token, action, amount, limit, info):
        record=SimpleNamespace(external_id="0x"+format(len(self.prepared)+1,"064x"),token_id=token,action=action,amount=amount,limit=limit)
        self.prepared.append(record)
        if self.prepare_callback:self.prepare_callback()
        return record
    def submit(self, prepared):
        self.submitted.append(prepared)
        if self.fail_post:raise TimeoutError("lost acknowledgement")
        return {"accepted":True,"external_id":prepared.external_id,"rejected":False}
    def reconcile(self, order, market, info):
        return self.responses.get(order["external_id"],{"fills":[],"terminal":False,"no_fill":False,"status":"MATCHED"})
    def token_balance(self, token):
        return {"balance":self.token_shares,"allowance":1000}


class ManagedLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.settings=Settings(db_path=self.root/"paper.sqlite3",strategy_experiments_enabled=False,
            market_scout_enabled=False,llm_super_agent_enabled=False,managed_live_enabled=True,
            managed_live_dir=self.root/"live",live_trading_runtime_enabled=True)
        now=time.time();start=int(now)//300*300
        self.market=MarketRound(f"btc-updown-5m-{start}","BTC",start,now+240,100,
                               condition_id="condition",up_token="up",down_token="down")
        self.exchange=FakeExchange();self.feed=FakeFeed(self.market)
        self.store=ManagedLiveStore(self.root/"live/accounts.sqlite3");self.addCleanup(self.store.conn.close)
        self.manager=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=self.store)
        self.assertIsNone(self.manager.process_lock.acquire());self.addCleanup(self.manager.process_lock.release)
        self.manager.wallet={"balance":100.,"allowance":1000.,"portfolio_value":20.,"total_equity":120.,
                             "ready":True,"checked_at":now,"read_started_at":now-1}
        self.manager.geo={"ready":True,"blocked":False,"checked_at":now};self.manager.open_orders_at=now
        self.manager.signals.evaluate=Mock(return_value=(Signal("BTC","Up",.8,.4,10,"test signal"),.5))
        self.store.save_market(self.market)

    def enable(self, *ids):
        self.manager.update_settings({"enabled":True,"strategies":{k:{"enabled":True} for k in ids or ("MAIN",)}})

    def buy(self, key="MAIN", shares=10, price=.4):
        self.enable(key);self.manager.tick()
        order=next(o for o in self.store.active_orders() if o["strategy_id"]==key)
        self.confirm(order,shares,price)
        return self.store.positions(key)[0]

    def confirm(self, order, shares, price, trade_id="trade-1", terminal=True):
        self.exchange.responses[order["external_id"]]={"fills":[{"trade_id":trade_id,"shares":shares,"price":price,
            "fee":fee_amount(shares,price,.07,1),"fee_source":"official_market_fee_schedule"}],
            "terminal":terminal,"no_fill":False,"status":"MATCHED"}
        self.manager.reconcile()

    def test_defaults_off_and_status_has_no_paper_capital_or_control_token(self):
        s=self.manager.snapshot();self.assertFalse(s["enabled"])
        self.assertTrue(all(not r["settings"]["enabled"] for r in s["variants"]))
        self.assertEqual(s["wallet"]["balance"],100)
        self.assertTrue(all(r["metrics"]["realized_pnl"]==0 for r in s["variants"]))
        self.assertNotIn("control_token",s)
        self.manager.tick();self.assertEqual(self.exchange.submitted,[])

    def test_master_and_strategy_switches_both_required(self):
        self.manager.update_settings({"strategies":{"MAIN":{"enabled":True}}})
        self.manager.tick();self.assertEqual(len(self.exchange.prepared),0)
        self.manager.update_settings({"enabled":True})
        self.manager.tick();self.assertEqual(len(self.exchange.submitted),1)

    def test_stake_changes_reach_real_order_budget(self):
        self.manager.update_settings({"strategies":{"MAIN":{"stake_dollars":3}}})
        self.enable();self.manager.tick()
        self.assertEqual(self.exchange.prepared[0].amount,3)
        self.assertEqual(self.store.active_orders()[0]["requested_cash"],3)

    def test_invalid_config_is_atomic(self):
        original=self.store.config()
        for payload in ({"enabled":True,"strategies":{"MAIN":{"stake_dollars":float('nan')}}},
                        {"strategies":{"MAIN":{"stake_dollars":True}}},{"strategies":{"OTHER":{"enabled":True}}},
                        {"enabled":"true"},{"strategies":{"MAIN":{"stake_dollars":22}}}):
            with self.subTest(payload=payload),self.assertRaises(ValueError):self.store.update_config(payload)
            self.assertEqual(self.store.config(),original)

    def test_no_accounting_from_post_acknowledgement(self):
        self.enable();self.manager.tick()
        self.assertEqual(len(self.store.active_orders()),1)
        self.assertEqual(self.store.positions(),[])
        self.assertEqual(self.manager.available_cash(),95)

    def test_confirmed_fills_are_idempotent_and_fee_inclusive(self):
        p=self.buy()
        self.assertAlmostEqual(p["total_cost"],4.168)
        order=self.store.order(p["entry_order"])
        self.assertFalse(self.store.apply_confirmed_fill(order["id"],"trade-1",10,.4,.168,"official_market_fee_schedule"))
        self.assertEqual(self.store.position(p["id"])["initial_shares"],10)
        self.assertEqual(self.manager.wallet["balance"],100)  # Ledger arithmetic never invents wallet cash.

    def test_partial_buy_keeps_one_position_and_waits_for_more_confirmations(self):
        self.enable();self.manager.tick();order=self.store.active_orders()[0]
        self.confirm(order,4,.4,terminal=False)
        self.assertEqual(len(self.store.positions()),1);self.assertEqual(self.store.positions()[0]["initial_shares"],4)
        self.manager.tick();self.assertEqual(len(self.exchange.submitted),1)
        self.exchange.responses[order["external_id"]]={"fills":[
            {"trade_id":"trade-1","shares":4,"price":.4,"fee":.0672,"fee_source":"official_market_fee_schedule"},
            {"trade_id":"trade-2","shares":6,"price":.4,"fee":.1008,"fee_source":"official_market_fee_schedule"}],
            "terminal":True,"no_fill":False,"status":"MATCHED"}
        self.manager.reconcile();self.assertEqual(len(self.store.positions()),1)
        self.assertEqual(self.store.positions()[0]["initial_shares"],10)

    def test_unknown_post_is_not_retried_and_reserves_budget(self):
        self.exchange.fail_post=True;self.enable();self.manager.tick()
        self.assertEqual(self.store.active_orders()[0]["state"],"UNKNOWN")
        self.manager.tick();self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),1);self.assertEqual(self.manager.available_cash(),95)

    def test_unexpected_official_order_id_is_retained_and_halts_new_entries(self):
        official="0x"+"f"*64
        self.exchange.submit=Mock(return_value={"accepted":True,"external_id":official,"identity_mismatch":True})
        self.enable();self.manager.tick()
        order=self.store.active_orders()[0]
        self.assertEqual(order["ack_id"],official);self.assertEqual(order["state"],"UNKNOWN")
        self.manager.tick();self.assertEqual(self.exchange.submit.call_count,1)

    def test_off_during_preparation_prevents_post(self):
        self.enable();self.exchange.prepare_callback=lambda:self.manager.update_settings({"enabled":False})
        self.manager.tick();self.assertEqual(len(self.exchange.prepared),1)
        self.assertEqual(self.exchange.submitted,[]);self.assertEqual(self.store.active_orders(),[])

    def test_amount_change_during_preparation_invalidates_old_budget(self):
        self.enable();self.exchange.prepare_callback=lambda:self.manager.update_settings({"strategies":{"MAIN":{"stake_dollars":2}}})
        self.manager.tick();self.assertEqual(self.exchange.submitted,[])

    def test_wallet_reservation_is_shared_between_strategies(self):
        self.manager.wallet["balance"]=5.5
        self.enable("MAIN","MAIN_OPTIMIZED");self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),1)
        self.assertAlmostEqual(self.manager.available_cash(),.5)

    def test_low_wallet_balance_never_uses_paper_balance(self):
        self.manager.wallet["balance"]=.771923
        self.enable();self.manager.tick();self.assertEqual(self.exchange.submitted,[])
        self.assertIn("真实可用余额不足",self.manager.last_signal["MAIN"]["reason"])

    def test_read_error_is_unknown_not_zero(self):
        self.manager.wallet={"balance":None,"ready":False,"error":"timeout"}
        state=self.manager.snapshot();self.assertIsNone(state["wallet"]["balance"])
        self.assertIsNone(state["wallet"]["available"])
        with self.assertRaises(ValueError):self.manager.update_settings({"enabled":True})

    def test_geoblock_and_runtime_kill_switch_prevent_buy(self):
        self.manager.geo["blocked"]=True
        with self.assertRaises(ValueError):self.enable()
        self.manager.geo["blocked"]=False;self.enable()
        self.manager.settings=replace(self.settings,live_trading_runtime_enabled=False)
        self.manager.tick();self.assertEqual(self.exchange.submitted,[])

    def test_manual_exit_works_when_master_and_strategy_off(self):
        p=self.buy();self.manager.update_settings({"enabled":False,"strategies":{"MAIN":{"enabled":False}}})
        order=self.manager.close_position("MAIN",p["id"])
        self.assertEqual(order["action"],"SELL")
        self.assertEqual(self.exchange.submitted[-1].amount,10)
        self.assertEqual(self.store.position(p["id"])["remaining_shares"],10)

    def test_stop_profit_manages_existing_position_when_switches_off(self):
        p=self.buy("MAIN_TP_SL")
        self.feed.bid=.79;self.feed.ask=.8
        self.exchange.bid=.79;self.exchange.ask=.8
        self.manager.update_settings({"enabled":False,"strategies":{"MAIN_TP_SL":{"enabled":False}}})
        self.manager.tick()
        self.assertEqual(self.exchange.submitted[-1].action,"SELL")
        self.assertEqual(self.store.active_orders()[0]["position_id"],p["id"])

    def test_partial_sale_and_official_settlement_preserve_remaining_shares(self):
        p=self.buy();order=self.manager.close_position("MAIN",p["id"])
        self.confirm(order,5,.7,"sale-1")
        current=self.store.position(p["id"])
        self.assertEqual(current["remaining_shares"],5)
        self.assertAlmostEqual(current["remaining_cost"],2.084)
        self.assertAlmostEqual(current["realized_pnl"],1.3425)
        self.store.resolve(self.market.round_id,"Up")
        closed=self.store.position(p["id"])
        self.assertEqual(closed["status"],"REDEEMABLE");self.assertEqual(closed["claim_due"],5)
        self.assertAlmostEqual(closed["realized_pnl"],4.2585)
        self.assertEqual(self.manager.wallet["balance"],100)

    def test_never_sell_another_strategy_position_or_more_than_owned(self):
        p=self.buy()
        with self.assertRaises(ValueError):self.manager.close_position("MAIN_OPTIMIZED",p["id"])
        with self.assertRaises(ValueError):self.store.prepare_order("0x"+"a"*64,"MAIN",self.market,"up","Up","SELL",.5,shares=11,position_id=p["id"])
        self.exchange.token_shares=9
        with self.assertRaises(ValueError):self.manager.close_position("MAIN",p["id"])
        self.assertEqual(len(self.exchange.submitted),1)

    def test_marks_for_wrong_position_or_quantity_are_not_displayed(self):
        p=self.buy()
        good={"value":8,"shares":10,"as_of":time.time(),"valid_until":time.time()+2,
              "token_id":p["token_id"],"round_id":p["round_id"]}
        for mark in ({**good,"shares":5},{**good,"token_id":"another-token"},
                     {**good,"round_id":"another-round"},{**good,"as_of":time.time()+30}):
            row=next(r for r in self.store.snapshot({p["id"]:mark})["variants"] if r["variant_id"]=="MAIN")
            self.assertIsNone(row["metrics"]["unrealized_pnl"])

    def test_display_survives_quote_gap_with_age_and_recovers(self):
        self.buy();self.manager.refresh_valuations()
        initial=self.manager.snapshot()["variants"][0]["metrics"]
        self.assertEqual(initial["valuation"]["status"],"fresh")
        self.assertIsNotNone(initial["total_pnl"])
        # Freeze on whole seconds so a millisecond round-trip cannot move a
        # synthetic quote a floating-point fraction into the future.
        now=int(time.time())+1;self.feed.age=10000
        self.exchange.valuation_book=Mock(side_effect=TimeoutError)
        for seconds,status in ((10,"cached"),(65,"stale")):
            with patch("time.time",return_value=now+seconds):
                self.manager.refresh_valuations()
                row=self.manager.snapshot()["variants"][0]["metrics"]
            self.assertEqual(row["total_pnl"],initial["total_pnl"])
            self.assertEqual(row["unrealized_pnl"],initial["unrealized_pnl"])
            self.assertEqual(row["valuation"]["status"],status)
            self.assertGreaterEqual(row["valuation"]["age_seconds"],seconds)
        with patch("time.time",return_value=now+66):
            self.feed.age=0;self.feed.bid=.5;self.feed.ask=.51
            self.manager.refresh_valuations()
            refreshed=self.manager.snapshot()["variants"][0]["metrics"]
        self.assertEqual(refreshed["valuation"]["status"],"fresh")
        self.assertGreater(refreshed["total_pnl"],initial["total_pnl"])

    def test_display_fallback_is_read_only_and_cannot_trigger_stale_exit(self):
        p=self.buy("MAIN_TP_SL");self.manager.update_settings({"enabled":False})
        self.feed.age=10000;self.exchange.bid=.79;self.exchange.ask=.8
        self.exchange.valuation_book=Mock(wraps=self.exchange.valuation_book)
        self.manager.refresh_valuations()
        row=next(r for r in self.manager.snapshot()["variants"] if r["variant_id"]=="MAIN_TP_SL")
        self.assertGreater(row["metrics"]["unrealized_pnl"],0)
        self.exchange.valuation_book.assert_called_once_with(p["token_id"])
        self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),1)
        self.assertEqual(self.store.position(p["id"])["remaining_shares"],10)

    def test_display_cache_follows_old_round_until_official_settlement(self):
        p=self.buy();self.manager.refresh_valuations()
        initial=self.manager.snapshot()["variants"][0]["metrics"]["total_pnl"]
        with patch("time.time",return_value=self.market.ends_at+1):
            self.feed.market=replace(self.market,round_id="next-round",started_at=self.market.ends_at,
                                     ends_at=self.market.ends_at+300,up_token="next-up",down_token="next-down")
            self.manager.refresh_valuations()
            row=self.manager.snapshot()["variants"][0]
            self.assertEqual(row["metrics"]["valuation"]["status"],"pending")
            self.assertEqual(row["metrics"]["total_pnl"],initial)
            self.assertEqual(row["open_trades"][0]["round_id"],p["round_id"])
            self.feed.polymarket.get_resolution.return_value={"outcome":"Up"}
            self.manager.reconcile();self.manager.refresh_valuations()
            settled=self.manager.snapshot()["variants"][0]["metrics"]
        self.assertEqual(settled["valuation"]["status"],"empty")
        self.assertEqual(settled["total_pnl"],settled["realized_pnl"])
        self.assertEqual(settled["unrealized_pnl"],0)
        self.assertEqual(self.store.valuation_books(),{})
        self.assertEqual(self.manager.wallet["balance"],100)

    def test_cached_depth_revalues_partial_sale_using_remaining_cost(self):
        p=self.buy();market,reference,quotes,now=self.feed.observation()
        quotes["Up"].update(best_bid=.79,best_ask=.8,bids=[{"price":.79,"size":3},{"price":.5,"size":7}])
        with patch.object(self.feed,"observation",return_value=(market,reference,quotes,now)):
            self.manager.refresh_valuations()
        order=self.manager.close_position("MAIN",p["id"])
        self.confirm(order,5,.7,"partial-sale")
        with patch("time.time",return_value=now+10):
            row=self.manager.snapshot()["variants"][0]
        expected=3*.79+2*.5-fee_amount(3,.79,.07,1)-fee_amount(2,.5,.07,1)
        self.assertAlmostEqual(row["open_trades"][0]["mark_value"],expected,places=6)
        self.assertAlmostEqual(row["metrics"]["unrealized_pnl"],expected-2.084,places=6)
        self.assertAlmostEqual(row["metrics"]["total_pnl"],1.3425+expected-2.084,places=6)

    def test_display_cache_survives_restart_without_network_or_paper_balance(self):
        self.buy();self.manager.refresh_valuations()
        before=self.manager.snapshot()["variants"][0]["metrics"]["total_pnl"]
        restored=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=self.store)
        self.exchange.valuation_book=Mock(side_effect=AssertionError("snapshot must not use the network"))
        row=restored.snapshot()["variants"][0]
        self.assertEqual(row["metrics"]["total_pnl"],before)
        self.exchange.valuation_book.assert_not_called()

    def test_first_missing_or_insufficient_quote_is_not_zero_profit(self):
        p=self.buy()
        row=self.manager.snapshot()["variants"][0]
        self.assertIsNone(row["metrics"]["total_pnl"])
        self.assertEqual(row["metrics"]["valuation"]["status"],"missing")
        self.manager.refresh_valuations()
        self.manager._valuation_books[p["token_id"]]["bids"]=[{"price":.39,"size":9}]
        self.assertIsNone(self.manager.snapshot()["variants"][0]["metrics"]["total_pnl"])

    def test_thin_book_cannot_clear_previously_complete_estimate(self):
        p=self.buy();self.manager.refresh_valuations()
        before=self.manager.snapshot()["variants"][0]["metrics"]
        now=time.time()
        with patch("time.time",return_value=now+10):
            market,reference,quotes,observed=self.feed.observation()
            quotes["Up"]["bids"][0]["size"]=1
            self.exchange.valuation_book=Mock(return_value=quotes["Up"])
            with patch.object(self.feed,"observation",return_value=(market,reference,quotes,observed)):
                self.manager.refresh_valuations()
            after=self.manager.snapshot()["variants"][0]["metrics"]
        self.exchange.valuation_book.assert_called_once_with(p["token_id"])
        self.assertEqual(after["total_pnl"],before["total_pnl"])
        self.assertEqual(after["valuation"]["as_of"],before["valuation"]["as_of"])
        self.assertEqual(after["valuation"]["status"],"cached")

    def test_pending_sell_is_not_double_submitted(self):
        p=self.buy();self.manager.close_position("MAIN",p["id"])
        self.manager.close_position("MAIN",p["id"])
        self.assertEqual(len(self.exchange.submitted),2)

    def test_profit_signal_is_rechecked_against_real_rest_book(self):
        p=self.buy("MAIN_TP_SL")
        self.manager.update_settings({"enabled":False})
        self.feed.bid=.79;self.feed.ask=.8  # Streaming book suggests a profit; REST has already moved.
        self.manager.tick()
        self.assertEqual(len(self.exchange.submitted),1)
        self.assertEqual(self.store.position(p["id"])["remaining_shares"],10)

    def test_take_profit_limit_preserves_net_profit_threshold(self):
        p=self.buy("MAIN_TP_SL")
        self.exchange.bid=.52;self.exchange.ask=.53
        self.manager.close_position("MAIN_TP_SL",p["id"],reason="止盈")
        limit=self.exchange.prepared[-1].limit
        self.assertGreaterEqual(10*limit-fee_amount(10,limit,.07,1),p["remaining_cost"]*1.2-.000001)

    def test_crossed_streaming_quote_cannot_trigger_exit(self):
        p=self.buy("MAIN_TP_SL");self.manager.update_settings({"enabled":False})
        self.feed.bid=.9;self.feed.ask=.2
        self.manager.tick();self.assertEqual(len(self.exchange.submitted),1)

    def test_resolving_waits_for_pending_execution(self):
        p=self.buy();self.manager.close_position("MAIN",p["id"])
        self.assertFalse(self.store.resolve(self.market.round_id,"Down"))
        self.assertEqual(self.store.position(p["id"])["remaining_shares"],10)

    def test_order_and_config_survive_restart_without_reposting(self):
        self.exchange.fail_post=True;self.enable();self.manager.tick()
        self.manager.process_lock.release()
        new_store=ManagedLiveStore(self.store.path);self.addCleanup(new_store.conn.close)
        reopened=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=new_store)
        self.assertTrue(new_store.config()["enabled"])
        self.assertEqual(new_store.active_orders()[0]["state"],"UNKNOWN")
        self.assertEqual(len(self.exchange.submitted),1)
        self.assertNotEqual(reopened.control_token,self.manager.control_token)

    def test_only_unsubmitted_prepared_order_is_canceled_on_restart(self):
        self.store.prepare_order("0x"+"a"*64,"MAIN",self.market,"up","Up","BUY",.5,cash=5)
        other=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=self.store)
        self.assertEqual(other.store.active_orders(),[])

    def test_wallet_identity_cannot_change_on_existing_ledger(self):
        with self.assertRaises(ValueError):self.store.bind_wallet("another-wallet")

    def test_fixed_collateral_and_fee_budget_bounds(self):
        self.assertEqual(fixed_collateral("771923"),.771923)
        self.assertEqual(fixed_collateral("0"),0)
        for exponent in (1,2):
            ratio=maximum_fee_ratio(.07,exponent,.01,.72)
            for point in range(1,73):
                p=point/100
                self.assertLessEqual(.07*(p*(1-p))**exponent/p,ratio+1e-10)
        with self.assertRaises(ValueError):maximum_fee_ratio(.07,0,.01,.72)

    def test_rest_quote_and_signing_do_not_create_fake_positions(self):
        self.enable();self.exchange.prepare=Mock(side_effect=ValueError("签名前检查失败"))
        self.manager.tick()
        self.assertEqual(self.store.positions(),[]);self.assertEqual(self.exchange.submitted,[])

    def test_native_sdk_v2_hash_is_known_before_submission(self):
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds, MarketOrderArgs, CreateOrderOptions
        fake=ClobClient("https://example.invalid",chain_id=137,key="0x"+"11"*32,
                        signature_type=0,creds=ApiCreds("fake-api","ZmFrZQ==","fake-pass"))
        signed=fake.builder.build_market_order(MarketOrderArgs(token_id="1",amount=1,price=.4,side="BUY"),
                                               CreateOrderOptions(tick_size="0.01",neg_risk=False),version=2)
        exchange=ManagedExchange(self.settings);self.addCleanup(exchange.http.close)
        sdk=exchange.legacy._sdk()
        with patch.object(exchange,"client",return_value=(sdk,fake)),patch.object(exchange.legacy,"readiness_errors",return_value=[]),patch.object(fake,"create_market_order",return_value=signed) as create,patch.object(fake,"post_order",side_effect=AssertionError("must not post")):
            prepared=exchange.prepare(self.market,"1","BUY",5,.5,exchange_info())
        self.assertTrue(prepared.external_id.startswith("0x"));self.assertEqual(len(prepared.external_id),66)
        self.assertLessEqual(create.call_args.args[0].amount*1.0693,5)

    def test_new_protocol_book_cannot_use_legacy_signing_domain(self):
        exchange=ManagedExchange(self.settings);self.addCleanup(exchange.http.close)
        response=Mock();response.json.return_value={"asset_id":"up","version":"v2"}
        with patch.object(exchange.http,"get",return_value=response),self.assertRaisesRegex(ValueError,"新的交易协议"):
            exchange.rest_book("up")

    def test_display_book_can_be_delayed_without_relaxing_execution_freshness(self):
        exchange=ManagedExchange(self.settings);self.addCleanup(exchange.http.close)
        response=Mock();raw={"asset_id":"up","timestamp":str(time.time()*1000-10000),
                           "bids":[{"price":".39","size":"50"}],"asks":[{"price":".4","size":"50"}]}
        response.json.return_value=raw
        with patch.object(exchange.http,"get",return_value=response):
            with self.assertRaisesRegex(ValueError,"过期"):exchange.rest_book("up")
            self.assertEqual(exchange.valuation_book("up")["best_bid"],.39)
            raw["timestamp"]=str(time.time()*1000-31000)
            with self.assertRaisesRegex(ValueError,"过期"):exchange.valuation_book("up")
            raw["timestamp"]=str(time.time()*1000);raw["asset_id"]="wrong-token"
            with self.assertRaisesRegex(ValueError,"标识不匹配"):exchange.valuation_book("up")
            raw["asset_id"]="up";raw["asks"]=[]
            self.assertEqual(exchange.valuation_book("up")["best_bid"],.39)
            with self.assertRaisesRegex(ValueError,"缺失"):exchange.rest_book("up")
            raw["bids"]=[]
            with self.assertRaisesRegex(ValueError,"缺失"):exchange.valuation_book("up")

    def test_portfolio_read_failure_does_not_become_zero(self):
        exchange=ManagedExchange(self.settings);self.addCleanup(exchange.http.close)
        with patch.object(exchange.http,"get",side_effect=TimeoutError),self.assertRaises(ValueError):
            exchange.portfolio_value("0x"+"a"*40)
        missing=Mock();missing.raise_for_status.side_effect=ValueError("unavailable")
        legacy=Mock();legacy.json.return_value=[{"user":"0x"+"a"*40,"value":0}]
        with patch.object(exchange.http,"get",side_effect=[missing,legacy]):
            self.assertEqual(exchange.portfolio_value("0x"+"a"*40),0)

    def test_transport_posts_exact_signed_bytes_once_on_timeout(self):
        import httpx
        transport=Mock();transport.request.side_effect=httpx.ReadTimeout("network")
        client=ManagedClobClient(transport,host="https://example.invalid",chain_id=137)
        with self.assertRaises(httpx.ReadTimeout):client._post("https://example.invalid/order",headers={"test":"yes"},data='{"signed":"payload"}')
        self.assertEqual(transport.request.call_count,1)
        self.assertEqual(transport.request.call_args.kwargs["content"],b'{"signed":"payload"}')

    def test_reconciliation_requires_confirmed_official_trade(self):
        exchange=ManagedExchange(self.settings);self.addCleanup(exchange.http.close)
        sdk=exchange.legacy._sdk();fake=Mock()
        ident="0x"+"a"*64
        order={"external_id":ident,"token_id":"up","created_at":time.time()}
        fake.get_order.return_value={"id":ident,"status":"MATCHED","size_matched":"10"}
        trade={"id":"receipt","taker_order_id":ident,"asset_id":"up","status":"MATCHED","size":"10","price":"0.4"}
        fake.get_trades.return_value=[trade]
        with patch.object(exchange,"client",return_value=(sdk,fake)):
            pending=exchange.reconcile(order,self.market,exchange_info())
            self.assertFalse(pending["terminal"]);self.assertEqual(pending["fills"],[])
            trade["status"]="CONFIRMED"
            confirmed=exchange.reconcile(order,self.market,exchange_info())
            self.assertTrue(confirmed["terminal"]);self.assertAlmostEqual(confirmed["fills"][0]["fee"],.168)
            trade["status"]="FAILED"
            failed=exchange.reconcile(order,self.market,exchange_info())
            self.assertTrue(failed["terminal"]);self.assertTrue(failed["no_fill"]);self.assertEqual(failed["fills"],[])

    def test_official_fee_curve_is_applied_to_optimized_signal(self):
        from polybot2other.managed_live_signals import LiveSignals
        signals=LiveSignals(self.settings,self.store,self.feed,self.manager.available_cash,lambda:{})
        reference={"ready":True,"protocol_verified":True,"latest":{"price.crypto":{"price":100.2,"source_ms":time.time()*1000}}}
        info={**exchange_info(),"fee_rate":.25,"fee_exponent":2}
        candidate=Signal("BTC","Up",.54,.5,10,"test")
        with patch("polybot2other.managed_live_signals.OptimizedMainBot.evaluate",return_value=(candidate,{"limit_price":.51})):
            result,_=signals.evaluate("MAIN_OPTIMIZED",self.store.config()["strategies"]["MAIN_OPTIMIZED"],self.market,reference,{},time.time(),info)
        self.assertEqual(result.side,"NO_TRADE")

    def test_pending_real_budget_is_not_silently_released_on_api_failure(self):
        self.enable();self.manager.tick();original=self.manager.available_cash()
        self.exchange.reconcile=Mock(side_effect=TimeoutError("read timeout"))
        self.manager.reconcile();self.assertEqual(self.manager.available_cash(),original)
        self.assertEqual(len(self.store.active_orders()),1)

    def test_temporary_confirmation_failure_recovers_without_repost(self):
        self.enable();self.manager.tick();order=self.store.active_orders()[0]
        with patch.object(self.exchange,"reconcile",side_effect=OrderConfirmationPending("官方订单暂未同步，等待确认")):
            self.manager.reconcile();self.manager.reconcile()
        self.assertIsNone(self.manager._fault)
        self.assertEqual(self.store.order(order["id"])["state"],"UNKNOWN")
        self.assertEqual(self.manager.available_cash(),95)
        self.manager.tick()
        waiting=self.manager.snapshot()["variants"][0]["execution"]
        self.assertEqual(waiting["state"],"blocked");self.assertEqual(waiting["recovery"],"auto")
        self.assertEqual(len(self.exchange.submitted),1)
        self.confirm(order,10,.4)
        self.assertEqual(self.store.order(order["id"])["state"],"FILLED")
        self.assertIsNone(self.store.order(order["id"])["error"])
        self.assertIsNone(self.manager._access_block(require_wallet=True))
        self.assertNotEqual(self.manager.snapshot()["variants"][0]["execution"]["state"],"blocked")
        self.assertEqual(len(self.exchange.submitted),1)
        kinds=[r[0] for r in self.store.conn.execute("SELECT kind FROM events")]
        self.assertEqual(kinds.count("reconcile_wait"),1);self.assertEqual(kinds.count("reconcile_recovered"),1)

    def test_confirmation_can_resume_after_network_error_and_restart(self):
        self.enable();self.manager.tick();order=self.store.active_orders()[0]
        with patch.object(self.exchange,"reconcile",side_effect=TimeoutError):self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"UNKNOWN")
        other_store=ManagedLiveStore(self.store.path);self.addCleanup(other_store.conn.close)
        other=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=other_store)
        self.exchange.responses[order["external_id"]]={"fills":[{"trade_id":"confirmed-after-restart","shares":10,"price":.4,"fee":.168,"fee_source":"official_market_fee_schedule"}],"terminal":True,"no_fill":False}
        other.reconcile()
        self.assertEqual(other_store.order(order["id"])["state"],"FILLED")
        self.assertIsNone(other._fault);self.assertEqual(len(self.exchange.submitted),1)

    def test_confirmation_waits_for_all_unknown_orders_and_all_fills(self):
        self.enable();self.manager.tick();first=self.store.active_orders()[0]
        second=self.store.prepare_order("0x"+"b"*64,"MAIN_OPTIMIZED",self.market,"up","Up","BUY",.5,cash=5)
        self.store.set_order_state(second["id"],"PENDING")
        with patch.object(self.exchange,"reconcile",side_effect=OrderConfirmationPending("等待官方订单同步")):
            self.manager.reconcile()
        self.confirm(first,4,.4,terminal=False)
        self.assertEqual(self.store.order(first["id"])["state"],"UNKNOWN")
        self.assertIsNotNone(self.manager._access_block(require_wallet=True))
        self.exchange.responses[first["external_id"]]={"fills":[{"trade_id":"trade-1","shares":4,"price":.4,"fee":.0672,"fee_source":"official_market_fee_schedule"},{"trade_id":"trade-2","shares":6,"price":.4,"fee":.1008,"fee_source":"official_market_fee_schedule"}],"terminal":True,"no_fill":False}
        self.manager.reconcile()
        self.assertIsNotNone(self.manager._access_block(require_wallet=True))
        self.confirm(second,10,.4,"second-order")
        self.assertIsNone(self.manager._access_block(require_wallet=True))
        self.assertEqual(self.store.positions("MAIN")[0]["initial_shares"],10)

    def test_confirmed_no_fill_releases_only_that_reservation(self):
        self.enable();self.manager.tick();order=self.store.active_orders()[0]
        with patch.object(self.exchange,"reconcile",side_effect=OrderConfirmationPending("等待确认")):self.manager.reconcile()
        self.exchange.responses[order["external_id"]]={"fills":[],"terminal":True,"no_fill":True}
        self.manager.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"CANCELED")
        self.assertEqual(self.manager.available_cash(),100)
        self.assertIsNone(self.manager._access_block(require_wallet=True))

    def test_reconcile_never_queries_an_inflight_post(self):
        original_submit=self.exchange.submit
        def while_posting(prepared):
            self.assertEqual(self.store.active_orders()[0]["state"],"SUBMITTING")
            self.manager.reconcile()
            return original_submit(prepared)
        with patch.object(self.exchange,"reconcile",side_effect=AssertionError("POST still in flight")) as read,patch.object(self.exchange,"submit",side_effect=while_posting):
            self.enable();self.manager.tick();read.assert_not_called()
        self.assertEqual(self.store.active_orders()[0]["state"],"PENDING")
        self.assertEqual(self.manager._inflight_submissions,set());self.assertIsNone(self.manager._fault)

    def test_crashed_submitting_order_is_reconciled_without_reposting(self):
        order=self.store.prepare_order("0x"+"c"*64,"MAIN",self.market,"up","Up","BUY",.5,cash=5)
        self.store.set_order_state(order["id"],"SUBMITTING")
        other=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=self.store)
        self.exchange.responses[order["external_id"]]={"fills":[],"terminal":True,"no_fill":True}
        other.reconcile()
        self.assertEqual(self.store.order(order["id"])["state"],"CANCELED")
        self.assertEqual(self.exchange.submitted,[])

    def test_integrity_fault_survives_later_reads_and_restart(self):
        self.enable();self.manager.tick();order=self.store.active_orders()[0]
        with patch.object(self.exchange,"reconcile",side_effect=ValueError("成交的资产与本地订单不一致")):self.manager.reconcile()
        self.assertIn("资产",self.manager._fault)
        with patch.object(self.exchange,"reconcile") as read:
            self.manager.reconcile();read.assert_not_called()
        reopened_store=ManagedLiveStore(self.store.path);self.addCleanup(reopened_store.conn.close)
        other=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=reopened_store)
        self.assertIn("资产",other._fault)
        self.assertEqual(other.snapshot()["variants"][0]["execution"]["recovery"],"manual")
        self.assertEqual(other.store.order(order["id"])["state"],"UNKNOWN")
        self.assertEqual(len(self.exchange.submitted),1)

    def test_overbudget_confirmed_fill_remains_a_hard_stop(self):
        self.enable();self.manager.tick();order=self.store.active_orders()[0]
        self.confirm(order,10,.55)
        self.assertIn("超预算",self.manager._fault)
        self.manager.reconcile();self.assertIsNotNone(self.manager._access_block(require_wallet=True))
        other=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=self.store)
        self.assertIn("超预算",other._fault)

    def test_entry_failure_status_retains_reason_during_retry_then_recovers(self):
        self.manager.wallet["balance"]=2;self.enable();self.manager.tick()
        first=self.manager.snapshot()["variants"][0]["execution"]
        self.assertEqual(first["state"],"blocked");self.assertIn("真实可用余额不足",first["reason"])
        self.assertEqual(first["recovery"],"auto");self.assertGreater(first["retry_at"],time.time())
        self.manager.tick()
        self.assertEqual(self.manager.snapshot()["variants"][0]["execution"]["since"],first["since"])
        self.manager.wallet["balance"]=100
        with patch("time.time",return_value=time.time()+4):self.manager.tick()
        self.assertEqual(self.manager.snapshot()["variants"][0]["execution"]["state"],"confirming")
        self.assertEqual(len(self.exchange.submitted),1)

    def test_signal_wait_is_distinct_from_error_and_disabled_strategy(self):
        self.enable();self.manager.signals.evaluate=Mock(return_value=(Signal("BTC","NO_TRADE",0,0,0,"入场价格高于上限"),None))
        self.manager.tick();snapshot=self.manager.snapshot()
        self.assertEqual(snapshot["variants"][0]["execution"]["state"],"waiting")
        self.assertEqual(snapshot["variants"][0]["execution"]["reason"],"入场价格高于上限")
        self.assertEqual(snapshot["variants"][1]["execution"]["state"],"off")

    def test_incomplete_order_read_is_retryable_but_wrong_identity_is_not(self):
        exchange=ManagedExchange(self.settings);self.addCleanup(exchange.http.close)
        sdk=exchange.legacy._sdk();client=Mock();ident="0x"+"d"*64
        order={"external_id":ident,"token_id":"up","created_at":time.time()}
        client.get_trades.return_value=[];client.get_open_orders.return_value=[]
        client.get_balance_allowance.return_value={"balance":"0","allowance":"0"}
        with patch.object(exchange,"client",return_value=(sdk,client)):
            for response in (None,{},[],{"id":ident},{"id":ident,"status":"MATCHED"}):
                client.get_order.return_value=response
                with self.subTest(response=response):
                    state=exchange.reconcile(order,self.market,exchange_info())
                    self.assertFalse(state["terminal"]);self.assertFalse(state["no_fill"])
                    self.assertEqual(state["evidence"]["trades"],"checked")
            self.assertEqual(client.get_trades.call_count,5)
            client.get_order.return_value={"id":"0x"+"e"*64,"status":"MATCHED","size_matched":"0"}
            with self.assertRaisesRegex(ValueError,"编号"):exchange.reconcile(order,self.market,exchange_info())
            client.get_order.return_value={"id":ident,"status":"MATCHED","size_matched":"10"}
            client.get_trades.return_value=None
            state=exchange.reconcile(order,self.market,exchange_info())
            self.assertFalse(state["terminal"]);self.assertEqual(state["evidence"]["trades"],"unavailable")

    def test_managed_mode_does_not_construct_legacy_live_runners(self):
        from polybot2other.bot import PaperTradingBot
        from polybot2other.storage import TradeStore
        paper=TradeStore(self.settings.db_path,100);self.addCleanup(paper.conn.close)
        with patch("polybot2other.managed_live.ManagedLiveAccounts",return_value=self.manager),patch("polybot2other.bot.LiveStrategyRunner",side_effect=AssertionError("legacy runner must stay off")),patch("polybot2other.bot.LivePaperStrategyRunner",side_effect=AssertionError("legacy runner must stay off")):
            bot=PaperTradingBot(self.settings,paper)
        self.assertIsNone(bot.live_trading);self.assertIsNone(bot.live_paper_trading)
        self.assertIs(bot.managed_live,self.manager);self.assertEqual(paper.recent_trade_count(),0)

    def test_control_endpoint_rejects_cross_origin_missing_token_and_bad_host(self):
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.bot=SimpleNamespace(managed_live=self.manager,managed_live_error=None)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f"http://127.0.0.1:{server.server_port}"
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base+"/api/managed-live") as response:token=json.load(response)["control_token"]
        for headers in ({"Content-Type":"application/json"},
                        {"Content-Type":"application/json","X-Live-Control":token,"Origin":"https://unrelated.example"},
                        {"Content-Type":"application/json","X-Live-Control":token,"Host":"unrelated.example"}):
            request=urllib.request.Request(base+"/api/managed-live/settings",data=b'{"enabled":true}',headers=headers,method="POST")
            with self.subTest(headers=list(headers)),self.assertRaises(urllib.error.HTTPError) as caught:opener.open(request)
            self.assertEqual(caught.exception.code,403)
            self.assertFalse(self.store.config()["enabled"])
        request=urllib.request.Request(base+"/api/managed-live/settings",data=b'{"enabled":true}',
            headers={"Content-Type":"application/json","X-Live-Control":token,"Origin":base},method="POST")
        with opener.open(request) as response:result=json.load(response)
        self.assertTrue(result["enabled"]);self.assertEqual(self.exchange.submitted,[])


def exchange_info():
    return {"fee_rate":.07,"fee_exponent":1,"tick_size":"0.01","neg_risk":False}


if __name__=="__main__":unittest.main()

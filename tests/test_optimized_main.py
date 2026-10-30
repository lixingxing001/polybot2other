import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from polybot2other.config import Settings
from polybot2other.models import MarketRound, Signal
from polybot2other.storage import TradeStore
from polybot2other.optimized_reference import ReferenceCache, SPOT, TWAP
from polybot2other.optimized_main import OptimizedMainBot, OptimizedTradeStore, StrictTwapMarketClient, forecast_twap, trusted_book_update
from polybot2other.clob_ws import ClobMarketOrderBook
from polybot2other.optimized_comparison import MainComparison
from polybot2other.bot import PaperTradingBot


class OptimizedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=float(int(time.time()))
        self.settings=Settings(db_path=self.root/"optimized.sqlite3",
                               strategy_experiments_enabled=False,market_scout_enabled=False,
                               live_trading_runtime_enabled=False)
        self.store=OptimizedTradeStore(self.settings.db_path,100)
        self.addCleanup(self.store.conn.close)
        self.bot=OptimizedMainBot(self.settings,self.store)
        self.addCleanup(self.bot.references.conn.close)
        self.market=MarketRound("btc-updown-5m-"+str(int(self.now)-120),"BTC",
                               self.now-120,self.now+180,100,up_token="up",down_token="down")
        self.populate(self.bot.references)
        self.quotes={
            "Up":{"token_id":"up","best_bid":.39,"best_ask":.4,
                  "source_ms":self.now*1000,"received_ms":self.now*1000,
                  "asks":[{"price":.4,"size":1000}]},
            "Down":{"token_id":"down","best_bid":.59,"best_ask":.6,
                    "source_ms":self.now*1000,"received_ms":self.now*1000,
                    "asks":[{"price":.6,"size":1000}]},
        }

    def frame(self,channel,stamp,value,seq,snapshot=False):
        payload={"symbol":"btcusd","source":"chainlink"}
        if channel==TWAP:
            payload["window_seconds"]=60
        if snapshot:
            payload["data"]=[{"timestamp":stamp,"full_accuracy_value":str(value)}]
        else:
            payload.update(timestamp=stamp,full_accuracy_value=str(value))
        return {"channel":channel,"seq":seq,"snapshot":snapshot,"payload":payload}

    def populate(self,cache):
        cache.reset_connection()
        for channel in (SPOT,TWAP):
            frame=self.frame(channel,int((self.now-1)*1000),100.2 if channel==SPOT else 100.05,1,True)
            frame["payload"]["data"]=[
                {"timestamp":int((self.now-seconds)*1000),"full_accuracy_value":str(100.2 if channel==SPOT else 100)}
                for seconds in range(125,0,-1)]
            cache.ingest(frame,self.now*1000)
            cache.ingest(self.frame(channel,int(self.now*1000),100.2 if channel==SPOT else 100.05,2),self.now*1000)
        self.assertTrue(cache.verify("historical-proof",self.market.started_at,100))

    def evaluate(self):
        return self.bot.evaluate(self.market,self.bot.references.snapshot(self.now*1000),self.quotes,self.now)

    def test_snapshot_is_history_and_cannot_establish_live_freshness(self):
        cache=ReferenceCache(self.root/"snapshot.sqlite3")
        self.addCleanup(cache.conn.close)
        for channel in (SPOT,TWAP):
            cache.ingest(self.frame(channel,int(self.now*1000),100,1,True),self.now*1000)
        state=cache.snapshot(self.now*1000)
        self.assertFalse(state["ready"])
        self.assertEqual(state["latest"],{})

    def test_duplicate_source_timestamp_never_refreshes_receive_age(self):
        cache=self.bot.references
        before=cache.snapshot(self.now*1000)["latest"][TWAP]["received_ms"]
        cache.ingest(self.frame(TWAP,int(self.now*1000),100.05,3),self.now*1000+5000)
        after=cache.snapshot(self.now*1000+5000)
        self.assertEqual(after["latest"][TWAP]["received_ms"],before)
        self.assertFalse(after["ready"])

    def test_sequence_gap_invalidates_reference_readiness(self):
        with self.assertRaisesRegex(ValueError,"SEQUENCE_GAP"):
            self.bot.references.ingest(self.frame(TWAP,int((self.now+1)*1000),100.1,9),self.now*1000+1100)
        self.assertFalse(self.bot.references.snapshot(self.now*1000+1100)["ready"])

    def test_wrong_provider_or_window_is_rejected(self):
        f=self.frame(TWAP,int((self.now+1)*1000),100.1,3)
        f["payload"]["source"]="unverified"
        self.assertFalse(self.bot.references.ingest(f,self.now*1000+1000))
        f["payload"]["source"]="chainlink";f["payload"]["window_seconds"]=30
        self.assertFalse(self.bot.references.ingest(f,self.now*1000+1000))

    def test_opening_requires_exact_source_boundary(self):
        self.assertEqual(self.bot.references.opening(self.market.started_at),100)
        self.assertIsNone(self.bot.references.opening(self.market.started_at+.1))
        self.assertFalse(self.bot.references.verify("bad-open",self.market.started_at,101))
        signal,_=self.evaluate()
        self.assertIn("REFERENCE_VALIDATION",signal.reason)

    def test_selected_book_age_is_independent_of_opposite_side(self):
        self.quotes["Up"]["source_ms"]-=60000
        signal,_=self.evaluate()
        self.assertEqual(signal.side,"NO_TRADE")
        self.assertIn("QUOTE_STALE",signal.reason)

    def test_stale_twap_is_not_masked_by_fresh_spot(self):
        self.bot.references.latest[TWAP]["source_ms"]-=60000
        signal,_=self.evaluate()
        self.assertIn("REFERENCE_STALE",signal.reason)

    def test_valid_signal_uses_correct_twap_and_bounded_paper_execution(self):
        signal,features=self.evaluate()
        self.assertEqual(signal.side,"Up")
        self.assertLessEqual(signal.confidence,.9)
        self.assertGreater(features["fee_adjusted_edge"],.02)
        self.store.upsert_round(self.market)
        ids=self.bot._maybe_place_trade(self.market,signal,self.quotes)
        self.assertEqual(len(ids),1)
        row=self.store.conn.execute("SELECT execution_mode,cash_spent,limit_price FROM paper_orders").fetchone()
        self.assertEqual(row["execution_mode"],"PAPER")
        self.assertLessEqual(row["cash_spent"],5)
        self.assertAlmostEqual(row["limit_price"],.41)
        self.assertIsNone(self.bot.live_trading)

    def test_only_declared_early_price_combination_is_filtered(self):
        self.market=MarketRound(self.market.round_id,"BTC",self.now-40,self.now+260,100,up_token="up",down_token="down")
        self.quotes["Up"].update(best_bid=.54,best_ask=.55,asks=[{"price":.55,"size":1000}])
        signal,_=self.evaluate()
        self.assertIn("EARLY_MID_PRICE",signal.reason)
        self.quotes["Up"].update(best_bid=.39,best_ask=.4,asks=[{"price":.4,"size":1000}])
        self.assertEqual(self.evaluate()[0].side,"Up")

    def test_visible_ask_must_match_depth_used_for_fill(self):
        self.quotes["Up"]["asks"]=[{"price":.3,"size":1000}]
        signal,_=self.evaluate()
        self.assertEqual(signal.side,"NO_TRADE")
        self.assertIn("DEPTH_OUT_OF_SYNC",signal.reason)

    def test_book_requires_initial_snapshot_and_rejects_out_of_order_state(self):
        book=ClobMarketOrderBook.for_market(self.market);initialized=set();stamps={}
        delta={"event_type":"price_change","price_changes":[{"asset_id":"up","side":"SELL","price":".3","size":"100","best_ask":".3","best_bid":".29"}]}
        with self.assertRaisesRegex(ValueError,"INITIAL_BOOK_MISSING"):
            trusted_book_update(book,delta,1000,1000,initialized,stamps)
        snapshot={"event_type":"book","asset_id":"up","bids":[{"price":".39","size":"100"}],"asks":[{"price":".4","size":"100"}]}
        changed=trusted_book_update(book,snapshot,2000,2000,initialized,stamps)
        self.assertEqual(changed["Up"]["best_ask"],.4)
        self.assertEqual(trusted_book_update(book,delta,1000,3000,initialized,stamps),{})
        self.assertEqual(book.quotes["Up"]["best_ask"],.4)
        top={"event_type":"best_bid_ask","asset_id":"up","best_ask":".2","best_bid":".19"}
        self.assertEqual(trusted_book_update(book,top,4000,4000,initialized,stamps),{})
        self.assertEqual(book.quotes["Up"]["best_ask"],.4)

    def test_original_buy_rules_and_same_period_reverse_are_preserved(self):
        self.store.upsert_round(self.market)
        first=Signal("BTC","Up",.8,.4,10,"test")
        second=Signal("BTC","Down",.8,.4,-10,"test")
        quote={"token_id":"down","best_bid":.39,"best_ask":.4,"asks":[{"price":.4,"size":1000}]}
        self.assertEqual(len(self.bot._maybe_place_trade(self.market,first,self.quotes)),1)
        self.assertEqual(len(self.bot._maybe_place_trade(self.market,second,{"Down":quote})),1)
        self.assertEqual(self.store.open_trade_count("BTC"),2)

    def test_peak_to_trough_drawdown_survives_restart(self):
        with self.store.conn:
            self.store.conn.executemany(
                "INSERT INTO equity_curve(cash_balance,open_risk,realized_pnl,total_equity,created_at) VALUES(?,0,?,?,?)",
                [(130,30,130,self.now),(90,-10,90,self.now+1),(110,10,110,self.now+2)])
        self.assertAlmostEqual(self.store.metrics()["max_drawdown"],40)
        reopened=OptimizedTradeStore(self.settings.db_path,500)
        try:
            self.assertEqual(reopened.account()["initial_balance"],100)
            self.assertAlmostEqual(reopened.metrics()["max_drawdown"],40)
        finally:
            reopened.conn.close()

    def test_strict_market_never_uses_generic_open_price(self):
        event={"slug":self.market.round_id,"resolutionSource":"https://data.chain.link/streams/btc-usd-twap-60s-streams",
               "markets":[{"slug":self.market.round_id,"outcomes":["Up","Down"],"clobTokenIds":["up","down"]}]}
        client=StrictTwapMarketClient(self.settings,self.bot.references)
        with patch.object(client,"_get_event_by_slug",return_value=event),patch.object(client,"_get_text",side_effect=AssertionError("unexpected fallback")):
            market=client.read_market(self.market.started_at)
        self.assertEqual(market.target_price,100)

    def test_non_twap_contract_is_not_silently_accepted(self):
        client=StrictTwapMarketClient(self.settings,self.bot.references)
        with patch.object(client,"_get_event_by_slug",return_value={"resolutionSource":"spot"}):
            self.assertIsNone(client.read_market(self.market.started_at))

    def test_existing_ledger_and_value_confirm_remain_separate(self):
        base=TradeStore(self.root/"main.sqlite3",100)
        self.addCleanup(base.conn.close)
        settings=Settings(db_path=base.db_path,strategy_experiments_enabled=True,
                          strategy_experiments_db_dir=self.root/"experiments",
                          market_scout_enabled=False,live_trading_runtime_enabled=False)
        base.upsert_round(self.market)
        base.place_trade(SimpleNamespace(market=self.market,signal=Signal("BTC","Up",.8,.5,10,"history"),stake_dollars=5))
        base.settle_round_outcome(self.market.round_id,"Up",self.now)
        before=base.recent_trades(10)
        parent=PaperTradingBot(settings,base)
        optimized=parent.strategy_experiments._bots["MAIN_OPTIMIZED"]
        value=parent.strategy_experiments._bots["VALUE_CONFIRM_V1"]
        self.addCleanup(optimized.store.conn.close);self.addCleanup(optimized.references.conn.close);self.addCleanup(value.store.conn.close)
        self.assertEqual(before,base.recent_trades(10))
        self.assertEqual(optimized.store.account()["initial_balance"],base.metrics()["total_equity"])
        self.assertEqual(optimized.store.account()["realized_pnl"],0)
        self.assertEqual(optimized.store.recent_trade_count(),0)
        self.assertEqual(value.store.account()["initial_balance"],100)
        with patch.object(optimized,"_run_strategy_from_state",side_effect=AssertionError("parent must not drive independent bot")):
            parent.strategy_experiments.run_from_state(self.market,{},{})

    def test_comparison_excludes_old_profits_and_pending_settlement(self):
        base=TradeStore(self.root/"baseline.sqlite3",100)
        self.addCleanup(base.conn.close)
        base.upsert_round(self.market)
        base.place_trade(SimpleNamespace(market=self.market,signal=Signal("BTC","Up",.8,.5,10,"old"),stake_dollars=5))
        comparison=MainComparison(base,self.store,self.root/"comparison.json")
        start=comparison.manifest["started_at"]
        base.settle_round_outcome(self.market.round_id,"Up")
        self.assertEqual(comparison.snapshot()["baseline"]["realized_pnl"],0)
        later=replace_market(self.market,self.market.round_id+"-new")
        for store in (base,self.store):
            store.upsert_round(later)
            store.place_trade(SimpleNamespace(market=later,signal=Signal("BTC","Up",.8,.5,10,"new"),stake_dollars=5))
        base.settle_round_outcome(later.round_id,"Up",settlement_source="chainlink_fallback")
        self.store.settle_round_outcome(later.round_id,"Up")
        comparison._cached_at=0
        s=comparison.snapshot()
        self.assertEqual(s["baseline"]["settled"],0)
        self.assertEqual(s["optimized"]["settled"],1)
        self.assertEqual(s["common_settled_markets"],0)
        restarted=MainComparison(base,self.store,self.root/"comparison.json")
        self.assertEqual(restarted.manifest["started_at"],start)

    def test_fast_pause_resume_waits_for_old_workers_and_rewarms(self):
        release=threading.Event();started=threading.Event();calls=[]
        def worker(*args):
            calls.append(1);started.set()
            self.bot._stop.wait(3)
            release.wait(3)
        with patch.object(self.bot.reference_feed,"run",side_effect=worker), patch.object(self.bot,"_book_loop",side_effect=worker), patch.object(self.bot,"_optimized_loop",side_effect=worker):
            try:
                self.bot.start()
                self.assertTrue(started.wait(1))
                self.bot.request_pause()
                self.bot.start()
                self.assertTrue(self.bot._stop.is_set())
                release.set()
                deadline=time.monotonic()+2
                while time.monotonic()<deadline and len(calls)<6:
                    time.sleep(.01)
                self.assertEqual(len(calls),6)
                self.assertFalse(self.bot._stop.is_set())
                self.assertFalse(self.bot.references.snapshot()["ready"])
            finally:
                release.set()
                self.bot.stop()


def replace_market(m, round_id):
    return MarketRound(round_id,m.symbol,m.started_at,m.ends_at,m.target_price,up_token=m.up_token,down_token=m.down_token)

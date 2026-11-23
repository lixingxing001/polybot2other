"""FAST uses its own ledger and the existing Paper/live execution contract.

All exchanges are absent or mocked; no funded order is submitted.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from polybot2other.config import Settings
from polybot2other.experiments import selected_strategy_variants
from polybot2other.managed_live_signals import LiveSignals
from polybot2other.managed_live_store import ManagedLiveStore
from polybot2other.models import MarketRound, Signal
from polybot2other.optimized_fast import POLICY_VERSION, assess_fast_cost
from polybot2other.paper_execution import PaperExecution, ExecutionPolicy
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.storage import TradeStore

FAST = "MAIN_OPTIMIZED_FAST"


class FastIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1800000100.0
        clock = patch("time.time", side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.market = MarketRound("btc-updown-5m-1800000000", "BTC", 1800000000., 1800000300., 100.,
                                  condition_id="condition", up_token="up", down_token="down")
        self.settings = Settings(db_path=self.root/"base.sqlite3",market_scout_enabled=False,
            llm_super_agent_enabled=False, live_trading_runtime_enabled=False)
        self.info = {"fee_rate":.07,"fee_exponent":1,"tick_size":"0.01","neg_risk":False}
        self.live = ManagedLiveStore(self.root/"live.sqlite3")
        self.addCleanup(self.live.conn.close)
        self.feed = SimpleNamespace(references=SimpleNamespace(points=lambda *a:[]))

    def features(self, *, score=.8, side="Up"):
        return {"round_id":self.market.round_id,"decision_at":self.now,"candidate_side":side,
            "score":score,"base_selected_score":score,"entry_price":.4,"limit_price":.41,
            "all_in_cost":.43,"min_edge":.02,"policy_version":POLICY_VERSION,"fast_allowed":True,
            "fee_source":"official-market","code":"ALLOW"}

    def book(self, token="up"):
        return {"token_id":token,"best_bid":.39,"best_ask":.4,"source_ms":self.now*1000,
            "received_ms":self.now*1000,"tick_size":"0.01","min_order_size":5,
            "asks":[{"price":.4,"size":1000}],"bids":[{"price":.39,"size":1000}]}

    def frame(self):
        return MarketInputs(self.market,{"ready":True,"protocol_verified":True,"latest":{}},
            {},{"Up":self.book(),"Down":self.book("down")},self.now,"fast-test",None)

    def signals(self, mode="live"):
        return LiveSignals(self.settings,self.live,self.feed,lambda:100,lambda:{},execution_mode=mode)

    def paper(self):
        store = TradeStore(self.root/"fast-paper.sqlite3",100)
        self.addCleanup(store.conn.close)
        bot=SimpleNamespace(store=store,settings=replace(self.settings,db_path=store.db_path),
            _lock=threading.RLock(),paper_trading_paused=False,last_signal={},last_decision={},
            _sync_shared_market_inputs=self.frame)
        engine=PaperExecution(bot,FAST,self.settings,frame_reader=self.frame,info_reader=lambda *a:self.info,
            book_reader=self.book,config_reader=self.live.config,feed=self.feed,
            policy=ExecutionPolicy(confirmation_seconds=2,redemption_seconds=60),clock=lambda:self.now)
        def evaluate(*args,**kwargs):
            engine.signals.last_features=self.features(score=.84)
            engine.signals.last_strategy_id=FAST
            return Signal("BTC","Up",.84,.4,15,"fresh fast"),.41
        engine.signals.evaluate=evaluate
        return engine

    def test_migration_adds_disabled_strategy_without_resetting_old_settings(self):
        old=self.live.config();old["strategies"].pop(FAST)
        old["enabled"]=True;old["revision"]=87
        old["strategies"]["MAIN_OPTIMIZED"].update(enabled=True,stake_dollars=7,max_open_trades=4,max_daily_loss=63)
        with self.live.conn:self.live.conn.execute("UPDATE live_config SET json=? WHERE id=1",(json.dumps(old),))
        self.live._migrate_config();new=self.live.config()
        self.assertEqual(new["enabled"],old["enabled"]);self.assertEqual(new["revision"],87)
        for key,value in old["strategies"].items():self.assertEqual(new["strategies"][key],value)
        self.assertEqual({k:new["strategies"][FAST][k] for k in ("enabled","stake_dollars","max_open_trades","max_daily_loss")},
                         {"enabled":False,"stake_dollars":5.,"max_open_trades":2,"max_daily_loss":21.})

    def test_registration_is_explicit_and_original_selection_does_not_change(self):
        self.assertEqual([v.variant_id for v in selected_strategy_variants("MAIN_OPTIMIZED")],["MAIN_OPTIMIZED"])
        variant=selected_strategy_variants(FAST)[0]
        self.assertEqual(variant.combo,"主账户（优化极速赚钱版）")
        self.assertEqual(variant.signal_filter_mode,FAST)

    def test_realistic_paper_wires_fast_to_isolated_hundred_dollar_ledger(self):
        from polybot2other.bot import PaperTradingBot
        settings=replace(self.settings,managed_live_enabled=True,paper_realistic_execution=True,
            managed_live_dir=self.root/"managed",strategy_experiments_enabled=True,
            strategy_experiments_variants="MAIN_OPTIMIZED,"+FAST,
            strategy_experiments_db_dir=self.root/"experiments",btc_runtime_settings_path=self.root/"btc.json")
        base=TradeStore(settings.db_path,150)
        self.addCleanup(base.conn.close)
        before=self.live.config()
        unavailable=MarketInputs(None,{}, {},{},self.now,"none","等待行情")
        references=SimpleNamespace(points=lambda *a:[],snapshot=lambda *a:{},completed_openings=lambda *a:[])
        exchange=SimpleNamespace(market_info=Mock(side_effect=AssertionError("not needed")),
            rest_book=Mock(side_effect=AssertionError("not needed")),
            prepare=Mock(side_effect=AssertionError("Paper cannot sign")),
            submit=Mock(side_effect=AssertionError("Paper cannot submit")))
        manager=SimpleNamespace(store=self.live,exchange=exchange,market_input_snapshot=lambda:unavailable,
            feed=SimpleNamespace(references=references,polymarket=SimpleNamespace(get_resolution=lambda *a:None)))
        with patch("polybot2other.managed_live.ManagedLiveAccounts",return_value=manager):
            parent=PaperTradingBot(settings,base)
        for child in parent.strategy_experiments._bots.values():self.addCleanup(child.store.conn.close)
        engines=parent.paper_execution_service.engines
        self.assertEqual(set(engines),{"MAIN","MAIN_OPTIMIZED",FAST})
        fast=parent.strategy_experiments._bots[FAST]
        optimized=parent.strategy_experiments._bots["MAIN_OPTIMIZED"]
        self.assertEqual(fast.store.account()["initial_balance"],100)
        self.assertEqual(optimized.store.account()["initial_balance"],150)
        self.assertEqual(base.account()["initial_balance"],150)
        self.assertNotEqual(fast.store.db_path,optimized.store.db_path)
        self.assertIs(fast.references,references)
        engines[FAST].decide();engines[FAST].poll()
        self.assertEqual(engines[FAST].snapshot()["strategy_id"],FAST)
        self.assertEqual(self.live.config(),before)
        self.assertEqual(self.live.conn.execute("SELECT count(*) FROM orders").fetchone()[0],0)
        exchange.prepare.assert_not_called();exchange.submit.assert_not_called()

    def test_signal_branch_uses_own_risk_view_and_official_cost(self):
        signals=self.signals();signal=Signal("BTC","Up",.8,.4,15,"fast")
        with (patch("polybot2other.managed_live_signals.FastMainBot.evaluate",return_value=(signal,self.features())) as fast,
              patch("polybot2other.managed_live_signals.OptimizedMainBot.evaluate",side_effect=AssertionError("wrong strategy"))):
            result,limit=signals.evaluate(FAST,self.live.config()["strategies"][FAST],self.market,{}, {},self.now,self.info,market_input=self.frame())
        context=fast.call_args.args[0]
        self.assertIs(context.store,signals.views[FAST]);self.assertIsNot(context.store,signals.views["MAIN_OPTIMIZED"])
        self.assertEqual(context.settings.paper_taker_fee_rate,0)
        self.assertEqual(result.side,"Up");self.assertEqual(limit,.41)
        self.assertAlmostEqual(signals.last_features["all_in_cost"],.41+.07*.41*.59)
        self.assertEqual(signals.last_features["fee_source"],"official-market")

    def test_official_fee_can_reject_candidate_after_zero_fee_evaluation(self):
        signals=self.signals();signal=Signal("BTC","Up",.8,.4,15,"fast")
        with patch("polybot2other.managed_live_signals.FastMainBot.evaluate",return_value=(signal,self.features())):
            result,limit=signals.evaluate(FAST,self.live.config()["strategies"][FAST],self.market,{}, {},self.now,
                {**self.info,"fee_rate":2.0},market_input=self.frame())
        self.assertEqual(result.side,"NO_TRADE");self.assertIsNone(limit)
        self.assertEqual(signals.last_features["code"],"FAST_COST_EDGE")
        self.assertFalse(signals.last_features["fast_allowed"])

    def test_final_snapshot_rechecks_cost_and_market_and_excludes_opaque_values(self):
        signals=self.signals();signals.last_features={**self.features(),"secret":"never-persist","quotes":{"large":True}}
        evidence=signals.fast_snapshot(market=self.market,limit=.41,info=self.info)
        self.assertNotIn("secret",evidence);self.assertNotIn("quotes",evidence)
        self.assertIn("submission_expected_roi",evidence)
        with self.assertRaises(ValueError):signals.fast_snapshot(market=replace(self.market,round_id="different"),limit=.41,info=self.info)
        with self.assertRaises(ValueError):signals.fast_snapshot(market=self.market,limit=.79,info=self.info)

    def test_live_order_keeps_final_snapshot_and_rejects_foreign_evidence(self):
        decision=self.features()
        with self.assertRaisesRegex(ValueError,"快照"):
            self.live.prepare_order("missing",FAST,self.market,"up","Up","BUY",.41,cash=5)
        with self.assertRaisesRegex(ValueError,"不一致"):
            self.live.prepare_order("wrong",FAST,self.market,"down","Down","BUY",.41,cash=5,fast_decision=decision)
        order=self.live.prepare_order("hash",FAST,self.market,"up","Up","BUY",.41,cash=5,fast_decision=decision)
        self.assertEqual(json.loads(order["fast_decision_json"])["policy_version"],POLICY_VERSION)
        self.live.record_submission(order["id"],{"opaque":"response"})
        self.assertEqual(self.live.order(order["id"])["fast_decision_json"],order["fast_decision_json"])

    def test_live_pending_blocks_duplicate_and_rejected_empty_can_retry(self):
        first=self.live.prepare_order("first",FAST,self.market,"up","Up","BUY",.41,cash=5,fast_decision=self.features())
        with self.assertRaisesRegex(ValueError,"待确认"):
            self.live.prepare_order("second",FAST,self.market,"up","Up","BUY",.41,cash=5,fast_decision=self.features())
        self.live.set_order_state(first["id"],"REJECTED")
        retry=self.live.prepare_order("retry",FAST,self.market,"up","Up","BUY",.41,cash=5,fast_decision=self.features())
        self.assertEqual(retry["state"],"PREPARED")

    def test_live_filled_round_forbids_reversal_only_for_fast(self):
        order=self.live.prepare_order("fill",FAST,self.market,"up","Up","BUY",.41,cash=5,fast_decision=self.features())
        self.live.apply_confirmed_fill(order["id"],"trade",10,.4,.1,"official",notional=4)
        self.live.set_order_state(order["id"],"FILLED")
        with self.assertRaisesRegex(ValueError,"反手"):
            self.live.prepare_order("reverse",FAST,self.market,"down","Down","BUY",.41,cash=5,fast_decision=self.features(side="Down"))
        other=self.live.prepare_order("optimized","MAIN_OPTIMIZED",self.market,"down","Down","BUY",.41,cash=5)
        self.assertEqual(other["strategy_id"],"MAIN_OPTIMIZED")

    def test_paper_uses_fresh_score_and_preserves_order_provenance(self):
        engine=self.paper();rule=self.live.config()["strategies"][FAST]
        old=Signal("BTC","Up",.8,.4,15,"old")
        engine.enqueue_buy(self.market,old,.41,rule,0,self.info)
        payload=engine.pending()[0]
        self.assertEqual(payload["signal"]["confidence"],.84)
        self.assertEqual(payload["fast_decision"]["score"],.84)
        self.assertEqual(payload["fast_decision"]["policy_version"],POLICY_VERSION)
        self.assertLess(engine.store.account()["cash_balance"],100)
        self.now+=1;engine.poll();self.now+=2;engine.poll()
        self.assertEqual(len(engine.store.open_trades()),1)
        self.assertIn("反手",engine._entry_block(self.market,"Down",rule))
        self.assertEqual(self.live.positions(FAST),[])

    def test_paper_wait_state_clears_previous_decision_score(self):
        engine=self.paper();engine.signals.last_features=self.features();engine.signals.last_strategy_id=FAST
        engine.event("DATA_WAIT","本次行情未就绪")
        self.assertNotIn("score",engine.bot.last_decision)
        self.assertFalse(engine.bot.last_decision["fast_allowed"])
        self.assertEqual(engine.bot.last_signal["reason"],"本次行情未就绪")


if __name__ == "__main__":
    unittest.main()

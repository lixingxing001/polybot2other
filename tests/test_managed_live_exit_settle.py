"""The promoted candidate uses the real forecast/decision code with a fake exchange."""
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

import test_managed_live as live_fixtures
from polybot2other.forecast_paper import ForecastPaperPolicy
from polybot2other.managed_live_store import EXIT_SETTLE_LIVE, ManagedLiveStore, _settlement_curve_points
from polybot2other.optimized_reference import ReferenceCache, SPOT, TWAP
from polybot2other.strategy_league_policies import POLICIES, entry_decision, sample_frame


class ExitSettlementLiveTests(unittest.TestCase):
    def setUp(self):
        live_fixtures.ManagedLiveTests.setUp(self)
        del self.manager.signals.evaluate  # Use production evaluation, including before signing/submission.
        self.now=time.time()
        self.market=replace(self.market, started_at=self.now-210, ends_at=self.now+90)
        self.feed.market=self.market
        self.refs=ReferenceCache(self.root/"forecast-references.db")
        self.addCleanup(self.refs.conn.close)
        self.feed.references=self.refs
        stamp=int(self.now*1000)
        with self.refs.conn:
            self.refs.conn.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,?,?)", [
                (SPOT,stamp-i*1000,100.12,"100.12",stamp-i*1000,1) for i in range(121,-1,-1)])
        self.reference={"ready":True,"protocol_verified":True,"latest":{
            key:{"price":100.12,"source_ms":stamp,"received_ms":stamp} for key in (SPOT,TWAP)}}
        base_book=self.exchange.rest_book
        self.exchange.rest_book=lambda token:{**base_book(token),"tick_size":"0.01"}
        def observation():
            quotes={side:self.exchange.rest_book(token) for side,token in (("Up","up"),("Down","down"))}
            return self.market,self.reference,quotes,time.time()
        self.feed.observation=observation

    def enable(self):
        self.manager.update_settings({"enabled":True,"strategies":{EXIT_SETTLE_LIVE:{"enabled":True}}})

    def production_signal(self,frame,info):
        return self.manager.signals.evaluate(EXIT_SETTLE_LIVE,self.store.config()["strategies"][EXIT_SETTLE_LIVE],
            frame.market,frame.reference,frame.quotes,frame.observed_at,info,market_input=frame)

    def confirm_order(self, order, *, shares, notional, fee):
        tx_id = "polygon:" + order["external_id"]
        self.exchange.responses[order["external_id"]] = {
            "fills":[{"trade_id":tx_id,"shares":shares,"notional":notional,"price":notional/shares,
                      "fee":fee,"fee_source":"polygon_order_filled_v2"}],
            "evidence":{"receipt_groups":{tx_id:["verified-trade"]},"matched_trade_ids":["verified-trade"]},
            "terminal":True,"no_fill":False,"status":"MATCHED"}
        self.manager.reconcile()

    def test_old_config_and_disabled_default(self):
        old=self.store.config()
        del old["strategies"][EXIT_SETTLE_LIVE]
        old["enabled"]=True
        old["strategies"]["MAIN"]["enabled"]=True
        old["strategies"]["MAIN_OPTIMIZED"]["stake_dollars"]=8.
        with self.store.conn:
            self.store.conn.execute("UPDATE live_config SET json=? WHERE id=1",(json.dumps(old),))
        reopened=ManagedLiveStore(self.store.path)
        self.addCleanup(reopened.conn.close)
        config=reopened.config()
        self.assertTrue(config["enabled"])
        new=config["strategies"].pop(EXIT_SETTLE_LIVE)
        self.assertEqual(config["strategies"],old["strategies"])
        self.assertEqual((new["enabled"],new["stake_dollars"],new["max_open_trades"],new["max_daily_loss"]),
                         (False,5.,1,15.))

    def test_signal_matches_candidate_across_direction_and_rejection_cases(self):
        fixture=self.manager.market_input_snapshot()
        info=self.exchange.market_info(self.market)
        policy=next(p for p in POLICIES if p.key=="exit_settle")
        for up,ask,spread,left,stale in [(.65,.58,.01,50,False),(.30,.57,.02,180,False),
                (.60,.48,.02,65,False),(.65,.60,.01,50,False),(.65,.59,.05,100,False),
                (.62,.59,.01,20,False),(.9,.3,.02,181,False),(.9,.3,.02,19,False),(.85,.40,.01,70,True)]:
            with self.subTest(up=up,ask=ask,spread=spread,left=left,stale=stale):
                frame=replace(fixture, market=replace(fixture.market, ends_at=fixture.observed_at+left))
                quotes={side:{**q,"best_ask":ask,"best_bid":ask-spread,
                              "asks":[{"price":ask,"size":100}],"bids":[{"price":ask-spread,"size":100}]}
                        for side,q in frame.quotes.items()}
                if stale:quotes["Down"]["source_ms"]-=10000
                frame=replace(frame,quotes=quotes)
                prediction={"up_score":up,"projected_final_twap":100.12}
                with patch("polybot2other.forecast_paper.forecast_twap",return_value=prediction):
                    forecast=ForecastPaperPolicy(self.refs).evaluate(frame,info)
                    candidate=entry_decision(policy,sample_frame(frame,forecast,info),())
                    signal,limit=self.production_signal(frame,info)
                self.assertEqual((signal.side,limit),(candidate.side,candidate.limit) if candidate.action=="BUY"
                                 else ("NO_TRADE",None))
                if candidate.action=="BUY":self.assertEqual(signal.confidence,max(up,1-up))

    def test_normal_entry_uses_confirmed_real_ledger_and_waits_for_official_outcome(self):
        row = next(v for v in self.store.snapshot()["variants"] if v["variant_id"] == EXIT_SETTLE_LIVE)
        self.assertEqual(row["settled_pnl_curve"],[])
        self.manager.tick()
        self.assertFalse(self.exchange.submitted)
        self.enable()
        self.manager.tick()
        self.assertEqual([(o.action,o.amount,o.limit) for o in self.exchange.submitted],[("BUY",5.,.41)])
        order=self.store.active_orders(EXIT_SETTLE_LIVE)[0]
        self.assertIn("方向评分",order["reason"])
        self.confirm_order(order,shares=10.,notional=4.,fee=.168)
        self.assertEqual(self.store.risk_usage(EXIT_SETTLE_LIVE)["slots"],1)
        self.exchange.bid,self.exchange.ask=.06,.07
        self.manager.tick()
        self.manager.update_settings({"enabled":False})
        self.manager.tick()
        self.assertEqual([o.action for o in self.exchange.submitted],["BUY"])
        with patch("time.time",return_value=self.market.ends_at+5):
            self.manager.reconcile()
            self.assertEqual(len(self.store.positions(EXIT_SETTLE_LIVE,open_only=True)),1)
            self.feed.polymarket.get_resolution.return_value={
                "outcome":"Up","market_slug":self.market.round_id,"price_source":"Gamma:outcomePrices"}
            self.manager.reconcile()
        position=self.store.positions(EXIT_SETTLE_LIVE)[0]
        self.assertAlmostEqual(position["realized_pnl"],5.832)
        row=next(r for r in self.store.snapshot()["variants"] if r["variant_id"]==EXIT_SETTLE_LIVE)
        self.assertEqual(len(row["settled_pnl_curve"]),2)
        self.assertEqual(row["settled_pnl_curve"][0],{"created_at":position["opened_at"],"realized_pnl":0.})
        self.assertEqual(row["settled_pnl_curve"][-1],{"created_at":position["closed_at"],"realized_pnl":5.832})
        self.assertIn("本局已经买过",self.manager._entry_restriction(
            EXIT_SETTLE_LIVE,self.store.config()["strategies"][EXIT_SETTLE_LIVE],self.market,"Down"))

    def test_manual_sale_keeps_ownership_and_uses_net_receipt(self):
        self.enable()
        self.manager.tick()
        buy=self.store.active_orders(EXIT_SETTLE_LIVE)[0]
        self.confirm_order(buy,shares=10.,notional=4.,fee=.16)
        position=self.store.positions(EXIT_SETTLE_LIVE)[0]
        with self.assertRaisesRegex(ValueError,"当前策略没有"):
            self.manager.close_position("MAIN",position["id"])
        self.manager.update_settings({"enabled":False})
        self.manager.close_position(EXIT_SETTLE_LIVE,position["id"])
        sale=self.store.active_orders(EXIT_SETTLE_LIVE)[0]
        self.confirm_order(sale,shares=10.,notional=3.80,fee=.10)
        new_row=next(v for v in self.store.snapshot()["variants"] if v["variant_id"]==EXIT_SETTLE_LIVE)
        self.assertEqual(new_row["settled_pnl_curve"][-1]["realized_pnl"],-.46)
        self.assertEqual([order.action for order in self.exchange.submitted],["BUY","SELL"])
        with patch("time.time",return_value=self.now+10*86400):
            aged=next(v for v in self.store.snapshot()["variants"] if v["variant_id"]==EXIT_SETTLE_LIVE)
        self.assertEqual(aged["settled_pnl_curve"],new_row["settled_pnl_curve"])
        self.assertEqual(aged["settled_pnl_curve"][0],{"created_at":position["opened_at"],"realized_pnl":0.})

    def test_full_period_display_keeps_extrema_and_endpoints(self):
        points=[{"created_at":self.now+i*300,"realized_pnl":(i%11)-5} for i in range(5000)]
        points[123]["realized_pnl"]=-100
        points[4321]["realized_pnl"]=250
        display=_settlement_curve_points(points)
        self.assertLessEqual(len(display),1200)
        self.assertEqual((display[0],display[-1]),(points[0],points[-1]))
        self.assertIn(points[123],display);self.assertIn(points[4321],display)
        self.assertEqual(display,sorted(display,key=lambda point:point["created_at"]))

    def test_pre_submit_rechecks_both_public_market_rules_and_switch(self):
        self.enable()
        rules=self.exchange.rest_book
        def invalid_rules(token):
            book=rules(token)
            if token=="down":book["min_order_size"]=0
            return book
        self.exchange.rest_book=invalid_rules
        self.manager.tick()
        self.assertEqual(self.exchange.submitted,[])
        self.exchange.rest_book=rules
        self.exchange.prepare_callback=lambda:self.manager.update_settings({"strategies":{EXIT_SETTLE_LIVE:{"enabled":False}}})
        self.manager.tick()
        self.assertFalse(self.store.config()["strategies"][EXIT_SETTLE_LIVE]["enabled"])
        self.assertEqual(self.exchange.submitted,[])

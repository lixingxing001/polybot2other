from dataclasses import replace
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from polybot2other.forecast_paper import ForecastDecision, ForecastPaperPolicy
from polybot2other.optimized_reference import SPOT, TWAP
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.strategy_league_policies import (
    CONTROL_KEY, POLICIES, LeagueSample, SideQuote, entry_decision, exit_decision, sample_frame,
)


def book(ask=.44, bid=.43, bid_qty=100, ask_qty=100):
    return SideQuote(bid, ask, round(ask+.01, 3), ask+.02, bid_qty, ask_qty)


def sample(**changes):
    original = LeagueSample(500., "r", 45., .69, 100.01, 100., 100.03, 99.9, book(), book(.58,.57))
    return replace(original, **changes)


def path(old, recent):
    return [replace(recent if offset <= 10 else old, at=500.-offset) for offset in range(32,0,-2)]


P = {p.key:p for p in POLICIES}


class LeaguePoliciesTests(unittest.TestCase):
    def test_normalization_uses_aligned_two_sided_data_and_known_fees(self):
        m = SimpleNamespace(round_id="r",started_at=400.,ends_at=545.,target_price=100.,up_token="u",down_token="d")
        quotes = {side:{"token_id":token,"source_ms":500000,"received_ms":500000,
                        "best_bid":.58,"best_ask":.59,"bids":[{"price":.58,"size":3},{"price":.50,"size":1000}],
                        "asks":[{"price":.59,"size":7},{"price":.60,"size":3}]}
                  for side,token in (("Up","u"),("Down","d"))}
        latest = {channel:{"price":100.02,"source_ms":500000,"received_ms":500000} for channel in (SPOT,TWAP)}
        frame=MarketInputs(m, {"ready":True,"protocol_verified":True,"latest":latest},{},quotes,500.,"s",None)
        info={"tick_size":".01","fee_rate":.07,"fee_exponent":1}
        decision=ForecastDecision("WAIT","",m.round_id,500.,"s",up_score=.8,projected_final_twap=100.1)
        self.assertIsNone(sample_frame(frame,decision,info))  # unsupported precision notation
        info["tick_size"]="0.01"
        result=sample_frame(frame,decision,info)
        self.assertAlmostEqual(result.up.limit,.60)
        self.assertGreater(result.up.cost,result.up.limit)
        self.assertEqual((result.up.bid_depth,result.up.ask_depth),(3,10))
        self.assertIsNone(sample_frame(frame,replace(decision,snapshot_id="past"),info))
        quotes["Down"]["source_ms"]-=4000
        self.assertIsNone(sample_frame(frame,decision,info))
        quotes["Down"]["source_ms"]=500000
        quotes["Up"]["best_ask"]=.57
        self.assertIsNone(sample_frame(frame,decision,info))

    def test_control_matches_standard_060_signal_with_two_valid_books(self):
        market = SimpleNamespace(round_id="r", started_at=300., ends_at=600., target_price=100.,
                                 up_token="u", down_token="d")
        baseline = ForecastPaperPolicy(None, max_order_limit=.60)
        for favor, score, ask, spread, tick, left, rate in (
                ("Up", .74, .59, .02, "0.01", 180, .07),
                ("Down", .74, .59, .02, "0.01", 20, .07),
                ("Down", .74, .60, .02, "0.01", 20, .07),
                ("Up", .62, .57, .03, "0.01", 80, 0.),
                ("Up", .62, .57, .03, "0.01", 80, .07),
                ("Down", .80, .53, .04, "0.01", 80, .07),
                ("Up", .59, .44, .02, "0.01", 80, .07),
                ("Down", .74, .59, .01, "0.01", 181, .07),
                ("Up", .74, .599, .002, "0.001", 19, .07),
                ("Up", .74, .599, .002, "0.001", 20, .07)):
            with self.subTest(favor=favor, score=score, ask=ask, spread=spread, tick=tick, left=left, rate=rate):
                at = market.ends_at-left
                latest = {channel:{"price":100., "source_ms":1000*at, "received_ms":1000*at} for channel in (SPOT,TWAP)}
                quotes = {}
                for side,token in (("Up","u"),("Down","d")):
                    a = ask if side == favor else .5
                    b = round(a-spread, 4)
                    quotes[side] = dict(token_id=token, source_ms=at*1000, received_ms=at*1000,
                                        best_ask=a, best_bid=b, asks=[dict(price=a,size=100)], bids=[dict(price=b,size=100)])
                frame=MarketInputs(market,dict(ready=True,protocol_verified=True,latest=latest),{},quotes,at,"s",None)
                info=dict(tick_size=tick,fee_rate=rate,fee_exponent=1)
                up_score = score if favor == "Up" else 1-score
                with patch("polybot2other.forecast_paper.forecast_twap",return_value=dict(up_score=up_score,projected_final_twap=100.02)):
                    original=baseline.evaluate(frame,info)
                shared=sample_frame(frame,original,info)
                result=entry_decision(P[CONTROL_KEY],shared,[])
                self.assertEqual(result.action,original.action)
                if result.action == "BUY":
                    self.assertEqual((result.side,result.limit),(original.side,original.limit))

    def test_all_24_hypotheses_reachable_and_rules_declared(self):
        now=sample()
        old=sample()
        cases={
            "value_half":(now,[]),
            "value_dispute":(now,[]),
            "value_persist":(now,path(old,old)),
            "value_catchup":(now,path(replace(old,up_score=.55),old)),
            "trend_twoleg":(replace(now,spot=100.20),path(replace(old,spot=100),replace(old,spot=100.05))),
            "trend_accel":(replace(now,spot=100.20),path(replace(old,spot=100),replace(old,spot=100.02))),
            "trend_break":(replace(now,spot=100.20),path(old,old)),
            "trend_cross":(replace(now,spot=100.05),path(old,replace(old,spot=99.98))),
            "rev_book":(now,path(replace(old,up=book(.51,.50)),old)),
            "rev_spot":(replace(now,spot=100.10),path(replace(old,spot=100.10),replace(old,spot=99.99))),
            "rev_return":(replace(now,spot=99.97),path(replace(old,spot=99.94),old)),
            "rev_model":(now,path(replace(old,up_score=.41),old)),
            "book_backing":(replace(now,up=book(bid_qty=200),down=book(.58,.57,bid_qty=40)),[]),
            "book_refill":(replace(now,up=book(bid_qty=200)),path(old,old)),
            "book_thin":(replace(now,up=book(.47,.46,ask_qty=40)),path(old,old)),
            "book_tight":(now,path(replace(old,up=book(.48,.42)),old)),
            "close_cushion":(replace(now,spot=100.1),[]),
            "close_hurdle":(now,[]),
            "close_diverge":(replace(now,spot=99.9),[]),
            "close_quiet":(now,path(old,old)),
        }
        for key in ("exit_scratch","exit_cash","exit_confirm","exit_settle",CONTROL_KEY):
            cases[key]=(now,[])
        self.assertEqual(set(cases),set(P))
        self.assertEqual(len(P),25)
        for key,(current,history) in cases.items():
            with self.subTest(key=key):
                p=P[key]; self.assertTrue(p.description and p.exit_rule and p.weakness)
                entry=entry_decision(p,current,history)
                self.assertEqual((entry.action,entry.side,entry.limit),("BUY","Up",current.up.limit))
                self.assertEqual(entry_decision(p,replace(current,left=5),history).action,"WAIT")
                self.assertEqual(entry_decision(p,None,history).action,"WAIT")

    def test_reject_lookahead_cross_round_and_history_gaps(self):
        now=sample();past=sample(up_score=.51)
        policy=P["value_catchup"]
        hist=path(past,past)
        self.assertEqual(entry_decision(policy,now,hist).action,"BUY")
        for invalid in ([replace(x,at=550+x.at) for x in hist],
                        [replace(x,round_id="other") for x in hist], hist[:5]+hist[-2:]):
            self.assertEqual(entry_decision(policy,now,invalid).action,"WAIT")

    def test_independent_spot_direction_can_oppose_model_and_price_gate_still_applies(self):
        past=sample(spot=100.15)
        now=sample(up_score=.47,spot=99.88,down=book(.44,.43))
        choice=entry_decision(P["trend_twoleg"],now,path(past,sample(spot=100.)))
        self.assertEqual(choice.side,"Down")
        self.assertEqual(choice.action,"WAIT")  # per-side score .53 is short of the .54 gate
        now=replace(now,up_score=.45)
        self.assertEqual(entry_decision(P["trend_twoleg"],now,path(past,sample(spot=100.))).action,"BUY")
        # A reversion can buy the lower-scored side if the actual price still clears the model cost test.
        now=sample(up_score=.49,spot=99.97)
        outcome=entry_decision(P["rev_return"],now,path(sample(spot=99.94),past))
        self.assertEqual((outcome.action,outcome.side),("WAIT","Up"))
        now=replace(now,up=book(.41,.40))
        self.assertEqual(entry_decision(P["rev_return"],now,path(sample(spot=99.94),past)).action,"BUY")
        for q in (book(.60,.58),book(.55,.45),replace(book(),cost=.68)):
            self.assertEqual(entry_decision(P[CONTROL_KEY],sample(up=q),[]).action,"WAIT")

    def test_exit_rules_and_net_price_floor(self):
        now=sample(up_score=.47,up=book(.58,.56))
        self.assertEqual(exit_decision(P["exit_scratch"],now,[],"Up",.46).action,"SELL")
        self.assertEqual(exit_decision(P[CONTROL_KEY],now,[],"Up",.46).action,"HOLD")
        soft=sample(up_score=.70,up=book(.58,.56))
        lock=exit_decision(P["exit_cash"],soft,[],"Up",.55,.45)
        self.assertEqual(lock.action,"SELL")
        self.assertAlmostEqual(lock.min_net_bid,.53)
        self.assertEqual(exit_decision(P["exit_cash"],soft,[],"Up",None,.45).action,"HOLD")
        weak=replace(now,up_score=.3)
        self.assertEqual(exit_decision(P["exit_confirm"],weak,[],"Up",.29).action,"HOLD")
        self.assertEqual(exit_decision(P["exit_confirm"],weak,path(weak,weak),"Up",.29).action,"SELL")
        self.assertEqual(exit_decision(P["exit_settle"],weak,[],"Up",.29).action,"HOLD")
        self.assertEqual(exit_decision(P[CONTROL_KEY],replace(weak,left=1),[],"Up",.29).action,"HOLD")


if __name__=="__main__":
    unittest.main()

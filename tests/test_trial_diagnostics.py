from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from polybot2other.forecast_paper import ForecastDecision
from polybot2other.models import MarketRound
from polybot2other.optimized_reference import SPOT, TWAP
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.strategy_league import LeagueEngine
from polybot2other.strategy_league_policies import POLICIES
from polybot2other.trial_diagnostics import CandidateDiagnostics


START = 1800000000


class CandidateDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.league = self.path / "league.sqlite3"
        self.create_observer()
        self.market = MarketRound("btc-updown-5m-" + str(START), "BTC", START, START + 300, 100,
                                  condition_id="test", up_token="up", down_token="down")
        self.rules = {side: {"token_id": side.lower(), "min_order_size": 5, "tick_size": "0.01"}
                      for side in ("Up", "Down")}
        self.info = {"tick_size": "0.01", "fee_rate": .07, "fee_exponent": 1}

    def create_observer(self):
        self.observer = CandidateDiagnostics(self.path / 'diag.sqlite3', self.league, start_at=START)
        self.addCleanup(self.observer.close)

    def input(self, t, spot=100.5, score=.85):
        quotes = {s: {"token_id": s.lower(), "best_bid": b, "best_ask": a, "source_ms": t * 1000,
                      "received_ms": t * 1000, "asks": [{"price": a, "size": 100}],
                      "bids": [{"price": b, "size": 32}]}
                  for s, b, a in (("Up", .55, .56), ("Down", .44, .45))}
        tick = {"price": spot, "source_ms": t * 1000, "received_ms": t * 1000}
        reference = {"ready": True, "protocol_verified": True, "latest": {SPOT: tick, TWAP: tick}}
        frame = MarketInputs(self.market, reference, {}, quotes, t, str(t), None)
        decision = ForecastDecision("WAIT", "test", self.market.round_id, t, str(t), up_score=score,
                                    projected_final_twap=100.2)
        return frame, decision

    def observe(self, t, **kw):
        f, d = self.input(t, **kw)
        self.observer.observe(f, d, self.info, self.rules, now=t)

    def counts(self):
        return {(r["band"], r["subject"], r["reason"]): r["samples"] for r in self.observer.report()["counts"]}

    def test_gates_quality_deduplication_and_reopen(self):
        for seconds in range(130, 149):
            self.observe(START + seconds)
        self.observe(START + 149, spot=100.2, score=.44)
        counts = self.counts()
        self.assertEqual(counts["body", "quality", "行情合格"], 20)
        self.assertEqual(counts["body", "rev_return", "形态缺少连续12秒历史"], 12)
        self.assertEqual(counts["body", "rev_return", "形态及成本条件均满足"], 1)
        self.assertIn(("body", "close_diverge", "入场时窗外"), counts)
        t = START + 290
        f, d = self.input(t)
        f.quotes["Up"]["source_ms"] -= 4000
        f.quotes["Down"]["bids"] = []
        self.observer.observe(f, d, self.info, self.rules, now=t)
        counts = self.counts()
        self.assertEqual(counts["tail", "quality", "采样总数"], 1)
        self.assertEqual(counts["tail", "quality", "涨方盘口来源时间缺失或不新鲜"], 1)
        self.assertEqual(counts["tail", "quality", "跌方买盘缺少有效深度"], 1)
        self.assertEqual(self.observer.report()["observer_gaps"], 1)
        # The separate missing source ledger is never created by observation.
        self.assertFalse(self.league.exists())
        other = CandidateDiagnostics(self.path / "diag.sqlite3", self.league, start_at=t)
        self.addCleanup(other.close)
        other.observe(f, d, self.info, self.rules, now=t+.3)
        self.assertEqual(other.report()["counts"], self.observer.report()["counts"])
        self.assertEqual(other.start_at, START)

    def test_original_order_and_observer_depth_have_separate_provenance(self):
        now = START + 130
        engine = LeagueEngine(self.league, start_at=START, policies=(POLICIES[0],), clock=lambda: now)
        self.addCleanup(engine.close)
        def step(seconds, score):
            nonlocal now
            now = START + seconds
            frame, decision = self.input(now, score=score)
            self.observer.observe(frame, decision, self.info, self.rules, now=now)
            engine.step(frame, decision, self.info, self.rules)
        step(130, .85)
        step(131.1, .85)
        step(136, .3)
        step(137.1, .3)
        step(140, .3)
        result = self.observer.report()
        self.assertEqual(result["sell_count"], 1)
        sell = result["recent_sells"][0]
        self.assertEqual((sell["status"], sell["reason"]), ("FILLED", "持仓方向评分转弱"))
        self.assertEqual(sell["observer_at_submit"]["displayed_shares_at_limit"], 32)
        self.assertEqual(sell["observer_at_finish"]["displayed_shares_at_limit"], 32)
        self.assertEqual(sell["observer_at_finish"]["offset_ms"], 0)
        self.assertTrue(sell["fills"])
        self.assertEqual(self.observer.db.execute("select count(*) from diagnostic_sells").fetchone()[0], 1)
        # A later observer that missed the event cannot invent earlier quote depth.
        late = CandidateDiagnostics(self.path / "late.sqlite3", self.league, start_at=START)
        self.addCleanup(late.close)
        frame, decision = self.input(START + 150)
        late.observe(frame, decision, self.info, self.rules, now=START + 150)
        missed = late.report()["recent_sells"][0]
        self.assertIsNone(missed["observer_at_submit"])
        self.assertIsNone(missed["observer_at_finish"])
        self.assertEqual(missed["fills"],sell["fills"])

    def test_ledger_alias_rejected_and_missing_market_recorded(self):
        with self.assertRaises(ValueError):
            CandidateDiagnostics(self.path / 'diag.sqlite3', self.path / 'diag.sqlite3')
        now = START+299
        frame, decision = self.input(now)
        self.observer.observe(replace(frame, market=None), decision, self.info, self.rules, now=now)
        self.assertEqual(self.counts()["tail", "quality", "当前市场缺失或轮次不匹配"], 1)

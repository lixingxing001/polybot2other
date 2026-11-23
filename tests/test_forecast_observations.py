from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from polybot2other.forecast_observations import ForecastObservations, forecast_snapshot
from polybot2other.models import MarketRound
from polybot2other.storage import TradeStore


class ForecastObservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TradeStore(Path(tmp.name) / 'forecast.sqlite3', 100)
        self.addCleanup(self.store.conn.close)
        self.market = MarketRound("test", "BTC", 100, 400, 60000)
        self.store.upsert_round(self.market)
        self.tracker = ForecastObservations(self.store)

    def observe(self, at, side, valid_book=True):
        frame = SimpleNamespace(market=self.market, observed_at=at, error=None,
                                quotes={"Up": {"best_bid": .7, "best_ask": .72},
                                        "Down": {"best_bid": .28, "best_ask": .30}},
                                quote_error=lambda side: None if valid_book else "stale")
        self.tracker.observe(frame, {"forecast_direction": side, "up_score": .72 if side else None})

    def test_only_early_forecast_exposes_entry_trace(self):
        execution = SimpleNamespace(forecast_observations=self.tracker,
                                    entry_trace=SimpleNamespace(summary=lambda: {"available": True, "rows": []}))
        account = SimpleNamespace(paper_execution=execution)
        runner = SimpleNamespace(_lock=threading.Lock(), _bots={"FORECAST_PAPER": account, "FORECAST_EARLY_PAPER": account})
        bot = SimpleNamespace(strategy_experiments=runner)
        self.assertEqual(forecast_snapshot(bot, "FORECAST_EARLY_PAPER")["entry_trace"]["rows"], [])
        self.assertNotIn("entry_trace", forecast_snapshot(bot, "FORECAST_PAPER"))
        execution.entry_trace = None
        self.assertFalse(forecast_snapshot(bot, "FORECAST_EARLY_PAPER")["entry_trace"]["available"])

    def test_countdown_cannot_be_backfilled_later_and_comparison_is_paired(self):
        self.observe(218, "Down")
        self.observe(219, "Up")
        self.observe(221, "Down")
        self.observe(339, "Down", valid_book=False)
        self.observe(379, None)
        self.store.settle_round_outcome("test", "Up", 410, settlement_source="polymarket_official")
        a,b,c = self.tracker.snapshot()["checkpoints"]
        self.assertEqual((a["correct"], a["paired_correct"], a["book_correct"], a["comparable"]), (1,1,1,1))
        self.assertEqual((b["graded"], b["correct"], b["comparable"]), (1,0,0))
        self.assertEqual((c["tracked"], c["calls"], c["graded"], c["comparable"]), (1,0,0,0))

    def test_restart_and_unofficial_or_missing_results(self):
        self.observe(220, "Up")
        self.tracker = ForecastObservations(self.store)
        self.observe(225, "Down")
        self.store.settle_round_outcome("test", "Down", 410, settlement_source="other")
        rows = self.tracker.snapshot()["checkpoints"]
        self.assertEqual([(r["tracked"],r["graded"]) for r in rows], [(1,0)] * 3)
        actual = self.store.conn.execute("SELECT observed_at,direction FROM forecast_checkpoints WHERE seconds_left=180").fetchone()
        self.assertEqual(tuple(actual), (220, "Up"))

    def test_early_exit_compares_only_shares_sold_and_official_outcome(self):
        self.observe(219, "Up")
        with self.store.conn:
            self.store.conn.executemany("""INSERT INTO trades(round_id,symbol,side,stake,
              entry_price,shares,confidence,move_bps,status,opened_at,reason,payout,settlement_source)
              VALUES('test','BTC',?,2,.5,4,.6,0,'SETTLED',120,'sell',?,'early_exit')""",
              [("Up", 2.6), ("Down", 1.9)])
        self.store.settle_round_outcome("test", "Up", 410, settlement_source="polymarket_official")
        self.assertEqual(self.tracker.snapshot()["early_exits"], {"exits": 2, "graded": 2, "relative_pnl": .5})

    def test_skip_diagnostics_dedupe_for_each_round_inside_its_own_window(self):
        from polybot2other.forecast_observations import recent_entry_reasons
        frame = SimpleNamespace(market=self.market, observed_at=160)
        self.tracker.observe_entry_result(frame, "买价偏高")
        self.assertEqual(self.store.conn.execute("select count(*) from forecast_entry_notes").fetchone()[0], 0)
        tracker = ForecastObservations(self.store, checkpoints=(240,180,60,20))
        for _ in range(3):
            tracker.observe_entry_result(frame, "买价偏高")
        tracker.observe_entry_result(frame, "出现过买入信号")
        tracker.observe_entry_result(frame, "提交前复查未通过")
        self.assertEqual(recent_entry_reasons(self.store.conn, 399)["rounds"], 0)
        stats = recent_entry_reasons(self.store.conn, 401)
        self.assertEqual((stats["rounds"],stats["no_entry"]), (1,1))
        self.assertEqual({(r["reason"], r["rounds"]) for r in stats["reasons"]},
                         {("买价偏高",1), ("提交前复查未通过",1)})
        self.store.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,
            confidence,move_bps,status,opened_at,reason) VALUES('test','BTC','Up',5,.5,10,.7,3,'OPEN',170,'test')""")
        self.assertEqual(recent_entry_reasons(self.store.conn, 401)["no_entry"], 0)
        self.assertEqual(recent_entry_reasons(self.store.conn, 401)["reasons"], [])

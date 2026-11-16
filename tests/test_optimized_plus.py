import copy
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from polybot2other.config import Settings
from polybot2other.models import MarketRound
from polybot2other.optimized_main import OptimizedMainBot
from polybot2other.optimized_plus import (
    POLICY_VERSION, PlusDecisionJournal, PlusMainBot, assess_plus_cost,
    compact_plus_decision, momentum30_context, paper_comparison, read_momentum30,
)
from polybot2other.optimized_reference import ReferenceCache, SPOT, TWAP


NOW = 1800000120.0


def history(momentum=0.0, *, end_ms=NOW * 1000, count=36):
    """A deterministic source path with exactly the requested 30-second log return."""
    return [(end_ms - i * 1000, 100 * math.exp(-momentum / 10000 * i / 30),
             end_ms - i * 1000) for i in range(count - 1, -1, -1)]


class PlusMomentumTests(unittest.TestCase):
    def context(self, rows, *, decision_at=NOW, forecast_source_ms=NOW * 1000):
        return momentum30_context(rows, decision_at=decision_at,
                                  forecast_source_ms=forecast_source_ms)

    def test_uses_thirty_second_log_return_not_simple_percentage(self):
        result = self.context(history(100))
        self.assertTrue(result["momentum_available"])
        self.assertAlmostEqual(result["momentum_30_bps"], 100)
        self.assertEqual(result["momentum_span_seconds"], 30)
        self.assertGreater(abs((math.exp(.01) - 1) * 10000 - result["momentum_30_bps"]), .5)

    def test_forecast_snapshot_source_upper_bound_excludes_newer_received_tick(self):
        rows = history(1, end_ms=NOW * 1000 - 1000)
        rows.append((NOW * 1000, 200, NOW * 1000))
        result = self.context(rows, forecast_source_ms=NOW * 1000 - 1000)
        self.assertTrue(result["momentum_available"])
        self.assertAlmostEqual(result["momentum_30_bps"], 1)
        self.assertEqual(result["momentum_source_ms"], NOW * 1000 - 1000)

    def test_future_source_and_not_yet_received_prices_cannot_affect_decision(self):
        baseline = history(0)
        rows = baseline + [(NOW * 1000 + 1000, 200, NOW * 1000 - 100)]
        rows += [(NOW * 1000 - 500, 200, NOW * 1000 + 1)]
        self.assertEqual(self.context(rows), self.context(baseline))

    def test_anchor_first_received_after_decision_is_not_backfilled(self):
        rows = [r for r in history(0) if r[0] > NOW * 1000 - 30000]
        rows.append((NOW * 1000 - 30000, 100, NOW * 1000 + 1))
        self.assertFalse(self.context(rows)["momentum_available"])

    def test_duplicate_sources_keep_earliest_received_observation(self):
        rows = history(1)
        rows.extend([(NOW * 1000 - 30000, 5, NOW * 1000 - 1000)] * 4)
        self.assertAlmostEqual(self.context(rows)["momentum_30_bps"], 1)
        duplicates_only = [(NOW * 1000, 100, NOW * 1000)] * 40
        self.assertFalse(self.context(duplicates_only)["momentum_available"])

    def test_stale_insufficient_and_gapped_source_paths_wait(self):
        cases = [history(end_ms=NOW * 1000 - 4000), history(count=30),
                 [r for r in history() if not NOW * 1000 - 20000 < r[0] < NOW * 1000 - 10000]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.assertFalse(self.context(rows)["momentum_available"])

    def test_invalid_values_do_not_create_a_momentum_signal(self):
        for value in (None, True, float("nan"), float("inf"), "1800000120000"):
            with self.subTest(value=value):
                self.assertFalse(self.context(history(), forecast_source_ms=value)["momentum_available"])
        self.assertFalse(self.context([(NOW * 1000, 0, NOW * 1000)])["momentum_available"])

    def test_sqlite_reader_has_same_causal_cutoff_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ReferenceCache(Path(directory) / "reference.sqlite3")
            try:
                rows = history(1, end_ms=NOW * 1000 - 1000)
                rows.extend([(NOW * 1000, 200, NOW * 1000),
                             (NOW * 1000 - 500, 200, NOW * 1000 + 1)])
                with cache.conn:
                    cache.conn.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,?,0)",
                        [(SPOT, t, p, str(p), received) for t, p, received in rows])
                changes = cache.conn.total_changes
                result = read_momentum30(cache, decision_at=NOW, forecast_source_ms=NOW * 1000 - 1000)
                self.assertAlmostEqual(result["momentum_30_bps"], 1)
                self.assertEqual(cache.conn.total_changes, changes)
            finally:
                cache.conn.close()


class PlusDecisionTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.prior = []
        self.store = SimpleNamespace(trades_for_round=lambda _: self.prior,
            daily_realized_pnl=lambda: 0, open_trade_count=lambda _: 0,
            account=lambda: {"cash_balance": 100})
        self.context = SimpleNamespace(settings=self.settings, references=object(),
            store=self.store, paper_trading_paused=False)
        self.market = MarketRound("btc-updown-5m-1800000000", "BTC", NOW - 120,
            NOW + 180, 100, up_token="up", down_token="down")
        self.reference = {"ready": True, "protocol_verified": True, "latest": {
            SPOT: {"source_ms": NOW * 1000, "price": 100.2},
            TWAP: {"source_ms": NOW * 1000, "price": 100.1}}}
        self.quotes = {side: {"token_id": side.lower(), "best_bid": .54,
            "best_ask": .55, "source_ms": NOW * 1000, "received_ms": NOW * 1000,
            "asks": [{"price": .55, "size": 1000}]} for side in ("Up", "Down")}

    def evaluate(self, *, score=.8, momentum=0, rows=None, base=False):
        prediction = {"up_score": score, "projected_final_twap": 100.2 if score >= .5 else 99.8,
                      "forecast_source_ms": NOW * 1000}
        movement = momentum30_context(history(momentum) if rows is None else rows,
                                      decision_at=NOW, forecast_source_ms=NOW * 1000)
        with patch("polybot2other.optimized_main.forecast_twap", return_value=prediction), \
             patch("polybot2other.optimized_plus.read_momentum30", return_value=movement):
            cls = OptimizedMainBot if base else PlusMainBot
            return cls.evaluate(self.context, self.market, self.reference, self.quotes, NOW)

    def test_up_threshold_below_equal_above_two_basis_points(self):
        for momentum, side in ((1.999, "Up"), (2, "Up"), (2.001, "NO_TRADE")):
            with self.subTest(momentum=momentum):
                signal, features = self.evaluate(momentum=momentum)
                self.assertEqual(signal.side, side)
                self.assertTrue(features["baseline_allowed"])
                self.assertEqual(features["up_chase_blocked"], momentum > 2)
                if momentum > 2:
                    self.assertEqual(features["code"], "UP_CHASE")

    def test_down_has_no_new_momentum_gate_even_when_history_is_missing(self):
        for rows in ([], history(20), history(-20)):
            with self.subTest(rows=rows):
                signal, features = self.evaluate(score=.2, rows=rows)
                self.assertEqual(signal.side, "Down")
                self.assertFalse(features["up_chase_blocked"])

    def test_missing_up_context_recovers_without_pause_or_consuming_market(self):
        signal, features = self.evaluate(rows=[])
        self.assertEqual(features["code"], "PLUS_CONTEXT_WAIT")
        self.assertEqual(signal.side, "NO_TRADE")
        self.assertEqual(self.evaluate()[0].side, "Up")
        self.assertFalse(self.context.paper_trading_paused)
        self.assertEqual(self.prior, [])

    def test_same_market_guard_and_chase_both_remain_visible(self):
        self.prior = [{"status": "SETTLED", "side": "Down"}]
        signal, features = self.evaluate(momentum=5)
        self.assertEqual(signal.side, "NO_TRADE")
        self.assertEqual(features["code"], "SAME_ROUND_ENTRY")
        self.assertTrue(features["same_round_blocked"])
        self.assertTrue(features["up_chase_blocked"])
        self.assertEqual(self.evaluate(score=.2)[1]["code"], "SAME_ROUND_ENTRY")

    def test_late_entry_is_only_observed_not_blocked(self):
        self.market = replace(self.market, started_at=NOW - 200, ends_at=NOW + 100)
        signal, features = self.evaluate()
        self.assertEqual(signal.side, "Up")
        self.assertTrue(features["late_entry"])

    def test_no_fast_score_gate_and_no_mutation_of_original_rules_or_inputs(self):
        before_settings = copy.deepcopy(self.settings)
        before_reference = copy.deepcopy(self.reference)
        before_quotes = copy.deepcopy(self.quotes)
        original_before = self.evaluate(score=.64, momentum=5, base=True)
        self.assertEqual(original_before[0].side, "Up")
        self.assertEqual(self.evaluate(score=.64, momentum=0)[0].side, "Up")
        self.assertEqual(self.evaluate(score=.64, momentum=5)[0].side, "NO_TRADE")
        self.assertEqual(self.evaluate(score=.64, momentum=5, base=True), original_before)
        self.assertEqual(self.settings, before_settings)
        self.assertEqual(self.reference, before_reference)
        self.assertEqual(self.quotes, before_quotes)

    def test_existing_stricter_settings_still_apply(self):
        self.context.settings = replace(self.settings, min_confidence=.85)
        self.assertEqual(self.evaluate()[1]["code"], "WEAK_SIGNAL")
        self.context.settings = replace(self.settings, max_spread=.005)
        self.assertEqual(self.evaluate()[1]["code"], "PRICE_SPREAD")
        self.context.settings = replace(self.settings, min_time_left_seconds=200)
        self.assertEqual(self.evaluate()[1]["code"], "TIME_WINDOW")

    def test_base_rejection_does_not_reuse_prior_allowed_decision(self):
        self.assertTrue(self.evaluate()[1]["plus_allowed"])
        self.reference["ready"] = False
        signal, features = self.evaluate()
        self.assertEqual(signal.side, "NO_TRADE")
        self.assertFalse(features["baseline_allowed"])
        self.assertFalse(features["plus_allowed"])
        self.assertEqual(features["code"], "REFERENCE_STALE")
        self.assertEqual(features["fee_source"], "not-evaluated")


class PlusCostAndJournalTests(unittest.TestCase):
    def test_cost_gate_keeps_original_edge_without_adding_fast_roi(self):
        self.assertTrue(assess_plus_cost(.72, .70)["allowed"])
        self.assertFalse(assess_plus_cost(.72, .700001)["allowed"])
        self.assertFalse(assess_plus_cost(.72, .70, min_edge=.03)["allowed"])
        self.assertNotIn("min_expected_roi", assess_plus_cost(.72, .70))

    def test_invalid_score_and_cost_are_rejected(self):
        for value in (None, True, float("nan"), float("inf"), -1, 1.1):
            with self.subTest(value=value):
                self.assertFalse(assess_plus_cost(value, .55)["allowed"])
                self.assertFalse(assess_plus_cost(.8, value)["allowed"])

    def test_invalid_minimum_edge_rejects_without_type_error_or_nan_default(self):
        for value in (True, False, float("nan"), float("inf"), -float("inf"), ".02", {}, []):
            with self.subTest(value=value):
                result = assess_plus_cost(.8, .55, min_edge=value)
                self.assertFalse(result["allowed"])
                self.assertIsNone(result["expected_edge"])
                self.assertIsNone(result["min_edge"])
        self.assertTrue(assess_plus_cost(.8, .55, min_edge=None)["allowed"])
        self.assertEqual(assess_plus_cost(.8, .55, min_edge=-1)["min_edge"], .02)

    def test_compact_journal_excludes_nested_or_nonfinite_and_bounds_strings(self):
        row = compact_plus_decision({"round_id": "r", "reason": "x" * 300,
            "score": float("nan"), "reference": {"token": "secret"},
            "policy_version": POLICY_VERSION, "plus_allowed": True}, mode="paper")
        self.assertEqual(len(row["reason"]), 240)
        self.assertNotIn("score", row)
        self.assertNotIn("reference", row)
        self.assertEqual(row["mode"], "paper")

    def test_journal_rotates_and_io_failure_is_visible_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "decisions.jsonl"
            journal = PlusDecisionJournal(path, "paper", max_bytes=1)
            for i in range(3):
                self.assertTrue(journal.record({"round_id": str(i), "code": "ALLOW"}, force=True))
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["decisions.jsonl", "decisions.jsonl.1"])
            self.assertEqual(json.loads(path.read_text())["round_id"], "2")
            blocker = root / "blocked"
            blocker.write_text("file")
            failed = PlusDecisionJournal(blocker / "decisions.jsonl", "paper")
            self.assertFalse(failed.record({"code": "ALLOW"}))
            self.assertIsNotNone(failed.error)


class PlusComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "baseline.sqlite3"
        self.plus = self.root / "plus.sqlite3"
        for path, started in ((self.base, 0), (self.plus, 100)):
            with sqlite3.connect(path) as conn:
                conn.executescript("""CREATE TABLE account(created_at REAL);
                    CREATE TABLE trades(id INTEGER PRIMARY KEY,status TEXT,opened_at REAL,
                    settled_at REAL,stake REAL,pnl REAL);""")
                conn.execute("INSERT INTO account VALUES(?)", (started,))

    def insert(self, path, rows):
        with sqlite3.connect(path) as conn:
            conn.executemany("INSERT INTO trades(status,opened_at,settled_at,stake,pnl) VALUES(?,?,?,?,?)", rows)

    def test_common_launch_window_excludes_old_open_late_settle_and_future(self):
        self.insert(self.base, [("SETTLED", 99, 120, 5, 99), ("SETTLED", 100, 150, 5, -5),
                               ("SETTLED", 130, 160, 10, 8), ("OPEN", 140, None, 5, None),
                               ("SETTLED", 180, 210, 5, 6), ("SETTLED", 201, 220, 5, 9)])
        self.insert(self.plus, [("SETTLED", 110, 150, 5, 3)])
        result = paper_comparison(self.plus, self.base, asof=200)
        base = result["baseline"]
        self.assertEqual(result["started_at"], 100)
        self.assertEqual(base["settled_trades"], 2)
        self.assertEqual(base["pnl"], 3)
        self.assertEqual(base["normalized_5_pnl"], -1)
        self.assertEqual(base["drawdown"], 5)
        self.assertEqual(base["open_trades"], 2)
        self.assertEqual(base["win_rate"], 50)
        self.assertEqual(result["variant"]["pnl"], 3)

    def test_no_settlements_is_no_win_rate_instead_of_zero(self):
        result = paper_comparison(self.plus, self.base, asof=200)
        for key in ("baseline", "variant"):
            self.assertIsNone(result[key]["win_rate"])
            self.assertEqual(result[key]["settled_trades"], 0)

    def test_missing_ledger_returns_unavailable_without_creating_file(self):
        missing = self.root / "missing.sqlite3"
        result = paper_comparison(missing, self.base, asof=200)
        self.assertIsNone(result["started_at"])
        self.assertFalse(result["variant"]["available"])
        self.assertFalse(missing.exists())

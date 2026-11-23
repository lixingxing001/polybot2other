"""Persistent MAIN-R1 risk limits use local SQLite and synthetic NAV only."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace
from unittest import TestCase

from polybot2other.main_r1_risk import MainR1Risk, PEAK_HALT_REASON, TIMEZONE


def timestamp(local):
    return datetime.fromisoformat(local).replace(tzinfo=TIMEZONE).timestamp()


class MainR1RiskTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "risk.sqlite3"
        self.now = timestamp("2026-11-23 12:00:00")
        self.store = self.open_store()
        self.risk = MainR1Risk(self.store, clock=lambda: self.now)

    def open_store(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return SimpleNamespace(conn=conn, _lock=RLock())

    def evaluate(self, **kwargs):
        inputs = dict(nav=1000, available_cash=1000, exposure=0, unresolved_rounds=0)
        inputs.update(kwargs)
        return self.risk.evaluate(**inputs)

    def count(self, table):
        return self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_budget_uses_every_cap_and_never_rounds_up(self):
        self.assertEqual(self.evaluate()["budget"], 5)
        self.assertEqual(self.evaluate(available_cash=Decimal("1.23456789"))["budget"], 1.234567)
        self.assertEqual(self.evaluate(exposure=Decimal("8.76543289"))["budget"], 1.234567)
        # A withdrawal is not a trading loss; NAV still controls the entry budget.
        low = self.evaluate(nav=100, available_cash=100, net_external_flow=-900)
        self.assertTrue(low["allowed"])
        self.assertEqual(low["budget"], .5)
        large = self.evaluate(nav=2000, available_cash=2000, net_external_flow=1000)
        self.assertEqual(large["budget"], 5)

    def test_cash_exposure_slots_and_dust_block_without_persistent_halt(self):
        cases = [({"available_cash": 0}, "no_available_cash"),
                 ({"exposure": 10}, "exposure_limit"),
                 ({"exposure": 12}, "exposure_limit"),
                 ({"unresolved_rounds": 2}, "unresolved_round_limit"),
                 ({"unresolved_rounds": 3}, "unresolved_round_limit"),
                 ({"available_cash": .0000009}, "budget_below_precision")]
        for inputs, reason in cases:
            with self.subTest(inputs=inputs):
                result = self.evaluate(**inputs)
                self.assertFalse(result["allowed"])
                self.assertEqual(result["budget"], 0)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["persistent_halts"], [])
        self.assertTrue(self.evaluate(unresolved_rounds=1)["allowed"])

    def test_daily_boundary_is_inclusive_and_recovery_cannot_reopen_same_day(self):
        self.evaluate()
        self.assertTrue(self.evaluate(nav=980.000001)["allowed"])
        self.now += 1
        stopped = self.evaluate(nav=980)
        self.assertTrue(stopped["day_halted"])
        self.assertEqual(stopped["reason"], "daily_loss_limit")
        self.now += 1
        recovered = self.evaluate(nav=1050)
        self.assertFalse(recovered["allowed"])
        self.assertEqual(recovered["high_watermark"], 1050)
        self.assertEqual(recovered["day_start_nav"], 1000)
        self.assertEqual(recovered["persistent_halts"], [])

    def test_initial_balance_does_not_hide_loss_before_first_sample(self):
        result = self.evaluate(nav=980)
        self.assertEqual(result["day_start_nav"], 1000)
        self.assertTrue(result["day_halted"])
        self.assertFalse(result["allowed"])

    def test_peak_boundary_survives_restart_day_change_and_recovery(self):
        self.evaluate(nav=1200)
        self.now += 1
        stopped = self.evaluate(nav=1104)
        self.assertIn(PEAK_HALT_REASON, stopped["persistent_halts"])
        reopened = MainR1Risk(self.open_store(), clock=lambda: self.now)
        self.assertFalse(reopened.snapshot()["allowed"])
        self.now = timestamp("2026-11-24 12:00:00")
        result = reopened.evaluate(nav=1300, available_cash=1300, exposure=0, unresolved_rounds=0)
        self.assertEqual(result["high_watermark"], 1300)
        self.assertFalse(result["day_halted"])
        self.assertFalse(result["allowed"])
        self.assertIn(PEAK_HALT_REASON, result["persistent_halts"])
        self.assertEqual(self.count("main_r1_risk_halts"), 1)

    def test_one_sample_above_peak_limit_is_not_persistent_stop(self):
        self.evaluate()
        result = self.evaluate(nav=920.000001)
        self.assertTrue(result["day_halted"])
        self.assertEqual(result["persistent_halts"], [])
        result = self.evaluate(nav=920)
        self.assertIn(PEAK_HALT_REASON, result["persistent_halts"])

    def test_verified_external_deposit_and_withdrawal_do_not_reset_performance(self):
        self.evaluate()
        deposit = self.evaluate(nav=1500, available_cash=1500, net_external_flow=500)
        self.assertEqual(deposit["adjusted_nav"], 1000)
        self.assertEqual(deposit["high_watermark"], 1000)
        withdrawal = self.evaluate(nav=600, available_cash=600, net_external_flow=-400)
        self.assertEqual(withdrawal["adjusted_nav"], 1000)
        self.assertTrue(withdrawal["allowed"])
        loss = self.evaluate(nav=580, available_cash=580, net_external_flow=-400)
        self.assertEqual(loss["adjusted_nav"], 980)
        self.assertTrue(loss["day_halted"])

    def test_missing_midnight_preserves_cross_midnight_loss_and_records_anchor(self):
        self.now = timestamp("2026-11-23 23:59:00")
        self.evaluate()
        self.now = timestamp("2026-11-24 00:01:00")
        result = self.evaluate(nav=975)
        self.assertEqual(result["day"], "2026-11-24")
        self.assertEqual(result["day_start_nav"], 1000)
        self.assertTrue(result["day_halted"])
        anchor = result["day_anchor"]
        self.assertEqual(anchor["kind"], "conservative_sampling_gap")
        self.assertFalse(anchor["midnight_observed"])
        self.assertEqual(anchor["previous_adjusted_nav"], 1000)
        self.assertEqual(anchor["gap_seconds"], 120)

    def test_new_day_can_raise_gap_anchor_but_not_lower_it(self):
        self.now = timestamp("2026-11-23 23:59:00")
        self.evaluate()
        self.now = timestamp("2026-11-24 00:01:00")
        result = self.evaluate(nav=1020)
        self.assertEqual(result["day_start_nav"], 1020)
        self.assertEqual(result["high_watermark"], 1020)
        self.assertTrue(result["allowed"])

    def test_exact_midnight_supplies_true_anchor_and_only_daily_stop_resets(self):
        self.now = timestamp("2026-11-23 23:58:00")
        self.evaluate()
        self.now += 1
        self.assertTrue(self.evaluate(nav=980)["day_halted"])
        self.now = timestamp("2026-11-24 00:00:00")
        result = self.evaluate(nav=975)
        self.assertEqual(result["day_start_nav"], 975)
        self.assertEqual(result["day_anchor"]["kind"], "exact_midnight")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["high_watermark"], 1000)

    def test_multi_day_gap_is_conservative_after_restart(self):
        self.evaluate(nav=1010)
        reopened = MainR1Risk(self.open_store(), clock=lambda: self.now)
        self.now = timestamp("2026-11-26 09:00:00")
        result = reopened.evaluate(nav=985, available_cash=985, exposure=0, unresolved_rounds=0)
        self.assertEqual(result["day_start_nav"], 1010)
        self.assertTrue(result["day_halted"])
        self.assertGreater(result["day_anchor"]["gap_seconds"], 2*86400)

    def test_halts_are_append_only_even_before_first_valuation(self):
        first = self.risk.halt("floor_drift")
        self.assertFalse(first["initialized"])
        self.assertEqual(first["persistent_halts"], ["floor_drift"])
        self.risk.halt("floor_drift")
        self.now += 1
        self.risk.halt("reconciliation_required")
        self.assertEqual(self.count("main_r1_risk_halts"), 3)
        self.now = timestamp("2026-11-24 12:00:00")
        result = self.evaluate(nav=1100)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["persistent_halts"], ["floor_drift", "reconciliation_required"])
        self.assertEqual(result["high_watermark"], 1100)

    def test_invalid_checkpoints_cannot_overwrite_state_or_create_samples(self):
        self.evaluate()
        before = self.risk.snapshot()
        bad = [dict(nav=None), dict(nav=float("nan")), dict(nav=float("inf")), dict(nav=-1),
               dict(nav=True), dict(nav="1000"), dict(available_cash=-1), dict(exposure=-1),
               dict(net_external_flow=float("nan")), dict(unresolved_rounds=1.5),
               dict(unresolved_rounds=True), dict(unresolved_rounds=-1),
               dict(now=float("inf")), dict(now=-1)]
        for inputs in bad:
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                self.evaluate(**inputs)
        self.assertEqual(self.risk.snapshot(), before)
        self.assertEqual(self.count("main_r1_risk_samples"), 1)

    def test_zero_nav_records_actual_zero_and_stops(self):
        result = self.evaluate(nav=0, available_cash=0)
        self.assertEqual(result["adjusted_nav"], 0)
        self.assertFalse(result["allowed"])
        self.assertIn(PEAK_HALT_REASON, result["persistent_halts"])
        self.assertEqual(result["budget"], 0)

    def test_clock_regression_is_rejected_for_sampling_and_manual_halts(self):
        self.evaluate()
        before = self.risk.snapshot()
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.evaluate(now=self.now-1)
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.risk.halt("late", now=self.now-1)
        self.assertEqual(self.risk.snapshot(), before)
        self.risk.halt("pending_review", now=self.now+10)
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.evaluate(now=self.now+9)

    def test_snapshot_is_read_only_even_after_midnight(self):
        self.evaluate(nav=980)
        before = self.risk.snapshot()
        changes = self.store.conn.total_changes
        self.now = timestamp("2026-11-24 12:00:00")
        self.assertEqual(self.risk.snapshot(), before)
        self.assertEqual(self.store.conn.total_changes, changes)
        self.assertEqual(self.count("main_r1_risk_samples"), 1)

    def test_every_valid_sample_is_audited_including_blocked_samples(self):
        self.evaluate()
        self.now += 2
        self.evaluate(nav=980)
        rows = self.store.conn.execute("SELECT * FROM main_r1_risk_samples ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["adjusted_nav"], 980)
        self.assertEqual(rows[1]["allowed"], 0)
        self.assertEqual(rows[1]["reason"], "daily_loss_limit")
        self.assertEqual(json.loads(rows[1]["day_anchor_json"])["baseline"], 1000)

    def test_mutation_refuses_caller_transaction_without_committing_or_rolling_it_back(self):
        self.store.conn.execute("CREATE TABLE unrelated (value INTEGER)")
        self.store.conn.execute("INSERT INTO unrelated VALUES (42)")
        self.assertTrue(self.store.conn.in_transaction)
        for action in (self.evaluate, lambda: self.risk.halt("halt"),
                       lambda: MainR1Risk(self.store)):
            with self.assertRaisesRegex(RuntimeError, "own transaction"):
                action()
            self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0], 0)
        self.assertEqual(self.count("main_r1_risk_samples"), 0)

    def test_two_connections_observe_persistent_stops_and_do_not_reset_peak(self):
        peer = MainR1Risk(self.open_store(), clock=lambda: self.now)
        self.evaluate(nav=1200)
        self.now += 1
        result = peer.evaluate(nav=1104, available_cash=1104, exposure=0, unresolved_rounds=0)
        self.assertEqual(result["high_watermark"], 1200)
        self.assertIn(PEAK_HALT_REASON, self.risk.snapshot()["persistent_halts"])
        self.now += 1
        self.assertFalse(self.evaluate(nav=1210)["allowed"])
        self.assertEqual(peer.snapshot()["high_watermark"], 1210)

    def test_sqlite_default_tuple_row_factory_is_supported(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        risk = MainR1Risk(SimpleNamespace(conn=conn, _lock=RLock()), clock=lambda: self.now)
        result = risk.evaluate(nav=1000, available_cash=1000, exposure=0, unresolved_rounds=0)
        self.assertTrue(result["allowed"])
        self.assertEqual(risk.snapshot()["budget"], 5)

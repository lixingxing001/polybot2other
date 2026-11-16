"""Chronology/accounting checks use disposable ledgers, never funded accounts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "evaluate_adaptive_v2", Path(__file__).resolve().parents[1] / "scripts/evaluate_adaptive_v2.py"
)
validation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation)


def row(index, *, round_id=None, opened=100., settled=150., score=.8,
        cost=5., payout=7., source="MAIN_OPTIMIZED", provenance="optimized_raw"):
    return {
        "source_key": f"{source}:{index}", "source_name": source, "trade_id": index,
        "round_id": round_id or f"market-{index}", "side": "Up", "base_score": score,
        "provenance": provenance, "opened_at": opened, "settled_at": settled,
        "is_settled": settled is not None,
        "pnl": payout - cost if settled is not None else None,
        "payout": payout if settled is not None else None,
        "won": int(payout > cost) if settled is not None else None,
        "stake": cost, "shares": 10., "entry_price": cost / 10.,
        "all_in_cost": cost / 10., "time_left_seconds": 180.,
        "settlement_source": "polymarket_official",
    }


def threshold_assessor(samples, **candidate):
    return {
        "adaptive_score": candidate["base_score"], "allowed": candidate["base_score"] >= .7,
        "state": "TEST_FIXED_POLICY", "model_version": "test-v2",
        "training_cutoff": max((sample["settled_at"] for sample in samples), default=None),
    }


class AdaptiveValidationTests(unittest.TestCase):
    def test_grouped_decisions_never_learn_current_round_or_equal_timestamp(self):
        records = [
            row(1, opened=10, settled=50),
            row(2, opened=20, settled=100),  # exactly the group clock is not available
            row(3, round_id="shared", opened=100, settled=120),
            row(4, round_id="shared", opened=140, settled=170),
            row(5, opened=300, settled=400),
        ]
        seen = []

        def capture(samples, **candidate):
            seen.append((deepcopy(samples), candidate.copy()))
            return threshold_assessor(samples, **candidate)

        result = validation.evaluate(records, cutoff=200, evaluation_start=100, assessor=capture)
        self.assertEqual(len(seen), 2)
        for samples, candidate in seen:
            self.assertEqual(candidate["now"], 100)
            self.assertEqual([sample["source_key"] for sample in samples], ["MAIN_OPTIMIZED:1"])
            self.assertNotIn("pnl", samples[0])
            self.assertTrue(all(sample["settled_at"] < candidate["now"] for sample in samples))
        self.assertEqual(result["evaluated_markets"], 1)
        self.assertTrue(result["chronology_audit"]["passed"])
        self.assertEqual(result["decisions"][0]["available_training_sha256"],
                         result["decisions"][1]["available_training_sha256"])

    def test_future_outcome_changes_do_not_change_existing_decisions(self):
        records = [row(1, opened=10, settled=50), row(2, opened=90, settled=500),
                   row(3, opened=100, settled=150)]
        # Use training-sensitive predictions, so leaking the future label would
        # observably change both probability and selected result.
        def learned(samples, **candidate):
            probability = sum(sample["won"] for sample in samples) / len(samples) if samples else .5
            return {**threshold_assessor(samples, **candidate), "adaptive_score": probability,
                    "allowed": probability >= .75}

        before = validation.evaluate(records, cutoff=200, evaluation_start=100, assessor=learned)
        changed = deepcopy(records)
        changed[1].update(won=0, pnl=-5, payout=0)
        changed.append(row(4, opened=900, settled=1000, payout=0))
        after = validation.evaluate(changed, cutoff=200, evaluation_start=100, assessor=learned)
        self.assertEqual(before["decisions"], after["decisions"])
        self.assertTrue(before["decisions"][0]["selected"])

    def test_real_v2_policy_is_invariant_to_later_labels(self):
        prior = [row(index, opened=100 + index * 100, settled=150 + index * 100,
                     score=.75, cost=6.5, payout=10 if index % 2 else 0) for index in range(24)]
        future_label = row(100, opened=19000, settled=25000, score=.75, cost=6.5, payout=0)
        candidate = row(101, opened=20000, settled=20500, score=.75, cost=6.5, payout=10)
        before = validation.evaluate(prior + [future_label, candidate], cutoff=21000, evaluation_start=20000)
        changed_future = {**future_label, "pnl": 3.5, "payout": 10, "won": 1}
        after = validation.evaluate(prior + [changed_future, candidate], cutoff=21000, evaluation_start=20000)
        self.assertEqual(before["decisions"], after["decisions"])
        decision = before["decisions"][0]
        self.assertLess(decision["predicted_probability"], candidate["base_score"])
        self.assertEqual(decision["available_training_rows"], 24)
        self.assertTrue(before["chronology_audit"]["passed"])

    def test_exact_and_normalized_accounting_exclude_pending_outcomes(self):
        records = [row(1, opened=100, settled=150, cost=2, payout=3, score=.8),
                   row(2, opened=200, settled=250, cost=5, payout=0, score=.6),
                   row(3, opened=300, settled=900, cost=7, payout=10, score=.9)]
        result = validation.evaluate(records, cutoff=500, split_fraction=0,
                                     unit_stake=5, assessor=threshold_assessor)
        baseline, selected = result["baseline_all_recorded"], result["v2_selected"]
        self.assertEqual((baseline["settled_count"], baseline["pending_count"]), (2, 1))
        self.assertEqual(baseline["historical_net_pnl"], -4)
        self.assertEqual(baseline["historical_stake_including_costs"], 7)
        self.assertEqual(baseline["normalized_net_pnl"], -2.5)
        self.assertEqual(baseline["max_realized_drawdown"], 5)
        self.assertEqual(baseline["pending_committed_cost"], 7)
        self.assertEqual(baseline["normalized_pending_committed_cost"], 5)
        self.assertEqual(selected["historical_net_pnl"], 1)
        self.assertEqual(selected["normalized_net_pnl"], 2.5)
        self.assertEqual(selected["pending_count"], 1)
        self.assertAlmostEqual(baseline["brier_raw"], ((.8 - 1) ** 2 + .6 ** 2) / 2)
        self.assertIsNone(result["decisions"][-1]["pnl"])
        self.assertIsNone(result["decisions"][-1]["won"])
        self.assertIsNone(result["decisions"][-1]["settled_at"])
        self.assertTrue(result["accounting_audit"]["passed"])

    def test_simultaneous_settlements_do_not_depend_on_row_order(self):
        records = [row(1, opened=100, settled=300, cost=5, payout=0),
                   row(2, opened=200, settled=300, cost=5, payout=10)]
        result = validation.evaluate(records, cutoff=400, split_fraction=0, assessor=threshold_assessor)
        self.assertEqual(result["baseline_all_recorded"]["max_realized_drawdown"], 0)

    def test_time_split_excludes_whole_market_straddling_start(self):
        records = [row(1, round_id="old", opened=100, settled=200),
                   row(2, round_id="old", opened=160, settled=200),
                   row(3, round_id="new", opened=250, settled=300)]
        result = validation.evaluate(records, cutoff=400, evaluation_start=120, assessor=threshold_assessor)
        self.assertEqual([item["trade_id"] for item in result["decisions"]], [3])
        self.assertEqual(result["decisions"][0]["available_training_markets"], 1)

    def test_bad_accounting_and_model_time_leak_are_reported(self):
        record = row(1)
        record["pnl"] = 99

        def invalid_cutoff(samples, **candidate):
            return {**threshold_assessor(samples, **candidate), "training_cutoff": candidate["now"]}

        result = validation.evaluate([record], cutoff=200, split_fraction=0, assessor=invalid_cutoff)
        self.assertFalse(result["chronology_audit"]["passed"])
        self.assertFalse(result["accounting_audit"]["passed"])

    def test_unverified_adaptive_score_never_enters_training(self):
        records = [row(1, opened=10, settled=50, source="MAIN_ADAPTIVE",
                       provenance="unverified_adaptive", score=None), row(2, opened=100, settled=150)]
        result = validation.evaluate(records, cutoff=200, split_fraction=0, assessor=threshold_assessor)
        self.assertEqual(result["excluded_adaptive_unverified"], 1)
        self.assertEqual(result["decisions"][0]["available_training_rows"], 0)

    def test_read_only_snapshot_hashes_and_adaptive_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adaptive.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE trades(id INTEGER,round_id TEXT,side TEXT,stake REAL,shares REAL,
                      entry_price REAL,confidence REAL,opened_at REAL,settled_at REAL,status TEXT,pnl REAL,payout REAL);
                    INSERT INTO trades VALUES(1,'m1','Up',5,8,.6,.77,100,200,'SETTLED',3,8);
                    INSERT INTO trades VALUES(2,'m2','Up',5,8,.6,.77,300,400,'SETTLED',3,8);
                    INSERT INTO trades VALUES(3,'m3','Up',5,8,.6,.77,500,600,'SETTLED',3,8);
                    CREATE TABLE paper_orders(id INTEGER,trade_id INTEGER);
                    CREATE TABLE paper_sim_orders(order_id INTEGER,payload TEXT);
                    INSERT INTO paper_orders VALUES(2,2),(3,3);
                """)
                for ident, side in ((2, "Up"), (3, "Down")):
                    payload = {"adaptive_decision": {"base_selected_score": .71, "candidate_side": side,
                               "decision_at": 250 if ident == 2 else 450,
                               "round_id": f"m{ident}", "model_version": "main-adaptive-calibration-v2"}}
                    connection.execute("INSERT INTO paper_sim_orders VALUES(?,?)", (ident, json.dumps(payload)))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot = validation.read_ledger(path, "MAIN_ADAPTIVE")
            again = validation.read_ledger(path, "MAIN_ADAPTIVE")
            normalized, rejected = validation.normalized_rows(snapshot)
            self.assertEqual(snapshot["content_sha256"], again["content_sha256"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertEqual([item["base_score"] for item in normalized], [None, .71, None])
            self.assertEqual(normalized[1]["provenance"], "adaptive_v2_raw")
            self.assertEqual(rejected["legacy_or_unverified_adaptive_base_score"], 2)
            missing = Path(directory) / "never-create.sqlite3"
            with self.assertRaises(FileNotFoundError):
                validation.read_ledger(missing, "MAIN_OPTIMIZED")
            self.assertFalse(missing.exists())

    def test_timestamp_requires_explicit_timezone(self):
        self.assertEqual(validation.timestamp("1970-01-01T08:00:00+08:00"), 0)
        with self.assertRaises(Exception):
            validation.timestamp("2026-11-15T00:00:00")


if __name__ == "__main__":
    unittest.main()

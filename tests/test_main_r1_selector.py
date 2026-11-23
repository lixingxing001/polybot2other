"""Causal selection tests: synthetic public frames, local DB, no SDK or network."""
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace
from unittest import TestCase

from polybot2other.main_r1_selector import CANDIDATES, MainR1Selector, SELECTOR_CONFIG


BASE = 1_800_000_000.0  # Whole 30-minute block and whole five-minute market.


class MainR1SelectorTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "selector.sqlite3"
        self.now = BASE + 5
        self.mode = "trend"
        self.history_calls = []
        self.history_transform = lambda rows: rows
        self.config = {"min_regime_cohorts": 6, "min_time_blocks": 3,
                       "min_active_entries": 3, "block_seconds": 600,
                       "max_training_cohorts": 6, "shrinkage_cohorts": 2,
                       "switch_confirmations": 2, "min_hold_rounds": 2}
        self.store = self.open_store()
        self.selector = self.build(self.store)

    def open_store(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return SimpleNamespace(conn=conn, _lock=RLock())

    def build(self, store=None, **overrides):
        return MainR1Selector(store or self.store, history_reader=self.history,
                              clock=lambda: self.now, config={**self.config, **overrides})

    def history(self, start_ms, end_ms):
        self.history_calls.append((start_ms, end_ms))
        if self.mode == "trend":
            prices = [100 * math.exp(i*.00001) for i in range(31)]
        elif self.mode == "range":
            prices = [100 * math.exp((i % 2)*.00001) for i in range(31)]
        elif self.mode == "high_vol":
            prices = [100 * math.exp((i % 2)*.01) for i in range(31)]
        else:
            prices = [100 * math.exp((i if i <= 20 else 40-i)*.00001) for i in range(31)]
        rows = [{"source_ms": start_ms+(end_ms-start_ms)*i/30,
                 "received_ms": start_ms+(end_ms-start_ms)*i/30, "price": p}
                for i, p in enumerate(prices)]
        return self.history_transform(rows)

    def frame(self):
        start = self.now // 300 * 300
        market = SimpleNamespace(round_id=f"round-{int(start)}", started_at=start,
                                 ends_at=start+300, up_token="up", down_token="down")
        quotes = {side: {"token_id": token, "source_ms": self.now*1000,
                        "received_ms": self.now*1000, "best_ask": .60, "best_bid": .59,
                        "asks": [{"price": .60, "size": 100}],
                        "bids": [{"price": .59, "size": 100}]}
                  for side, token in (("Up", "up"), ("Down", "down"))}
        return SimpleNamespace(market=market, observed_at=self.now, quotes=quotes,
                               reference={"ready": True, "protocol_verified": True}, error=None)

    def cohort(self, start, *, winner="MAIN", values=None, regime="trend"):
        pnl = {k: (.5 if k == winner else -1) for k in CANDIDATES}
        if values is not None:
            pnl.update(values)
        arms = {}
        for key, value in pnl.items():
            arms[key] = ({"status": "NO_TRADE", "requested_budget": 0, "cost": 0, "shares": 0, "pnl": 0}
                         if value is None else
                         {"status": "SETTLED", "requested_budget": 1,
                          "cost": 1, "shares": 1+max(.5, value), "pnl": value})
        return {"round_id": f"round-{int(start)}", "started_at": start, "ends_at": start+300,
                "regime": regime, "feature_time": start+5, "coverage_complete": True, "arms": arms}

    def prepare(self, cohorts):
        """Record causal decisions first; settle at a genuinely later test clock."""
        original_now, original_mode = self.now, self.mode
        for cohort in sorted(cohorts, key=lambda row: row["feature_time"]):
            self.now = cohort["feature_time"]
            self.mode = cohort["regime"]
            decision = self.selector.choose(self.frame())
            self.assertEqual(decision["regime"], cohort["regime"])
        self.now, self.mode = original_now, original_mode

    def prepare_seed(self, *, winner="MAIN", values=None, regime="trend", ending_at=None, extra_starts=()):
        end = BASE-1800 if ending_at is None else ending_at
        cohorts = [self.cohort(start, winner=winner, values=values, regime=regime)
                   for start in range(int(end-1800), int(end), 300)]
        self.prepare(cohorts+[self.cohort(start, winner=winner, values=values, regime=regime)
                              for start in extra_starts])
        return cohorts

    def seed(self, **kwargs):
        cohorts = self.prepare_seed(**kwargs)
        for cohort in cohorts:
            self.selector.observe(cohort)
        return cohorts

    def activate(self, winner="MAIN"):
        self.seed(winner=winner, extra_starts=(BASE-600,))
        self.now += 1
        first = self.selector.choose(self.frame())
        self.assertIsNone(first["candidate_key"])
        self.selector.observe(self.cohort(BASE-600, winner=winner))
        self.now = BASE+305
        selected = self.selector.choose(self.frame())
        self.assertEqual(selected["candidate_key"], winner)
        return selected

    def test_cold_start_abstains_and_freezes_the_whole_round_across_restart(self):
        cohorts = self.prepare_seed()
        frame = self.frame()
        decision = self.selector.choose(frame)
        self.assertIsNone(decision["candidate_key"])
        self.assertEqual(decision["regime"], "trend")
        calls = len(self.history_calls)
        for cohort in cohorts:
            self.selector.observe(cohort)
        self.now += 1
        self.mode = "high_vol"
        reopened = self.build(self.open_store())
        self.assertEqual(reopened.choose(self.frame()), decision)
        self.assertEqual(len(self.history_calls), calls)
        self.assertEqual(reopened.status()["decisions"], len(cohorts)+1)

    def test_real_tick_regimes_do_not_depend_on_date(self):
        for i, mode in enumerate(("trend", "range", "high_vol", "neutral")):
            with self.subTest(mode=mode):
                self.mode = mode
                self.now = BASE+5+i*86_400
                selected = self.selector.choose(self.frame())
                self.assertEqual(selected["regime"], mode)
                self.assertEqual(selected["features"]["feature_time"], self.now)
        self.assertEqual(self.history_calls[0], ((BASE+5)*1000-300_000, (BASE+5)*1000))

    def test_future_received_history_cannot_form_a_regime(self):
        self.history_transform = lambda rows: rows[:-1]+[{**rows[-1], "received_ms": rows[-1]["received_ms"]+1}]
        selected = self.selector.choose(self.frame())
        self.assertIsNone(selected["candidate_key"])
        self.assertIsNone(selected["regime"])
        self.assertIn("future_ticks", selected["features"]["unavailable_reason"])

    def test_missing_or_gapped_history_does_not_fake_flat_volatility(self):
        self.history_transform = lambda rows: rows[-5:]
        selected = self.selector.choose(self.frame())
        self.assertIsNone(selected["regime"])
        self.assertNotIn("realized_volatility_bps", selected["features"])
        self.now += 300
        self.history_transform = lambda rows: [r for i, r in enumerate(rows) if not 10 <= i <= 15]
        self.assertIsNone(self.selector.choose(self.frame())["regime"])

    def test_liquidity_failure_and_invalid_timestamp_are_locked_abstentions(self):
        frame = self.frame()
        frame.quotes["Down"]["asks"] = [{"price": .6, "size": 1}]
        result = self.selector.choose(frame)
        self.assertEqual(result["reason"], "features_unavailable")
        self.assertIn("thin_executable_depth", result["features"]["unavailable_reason"])
        self.assertEqual(self.selector.choose(self.frame()), result)
        self.now += 300
        frame = self.frame()
        frame.observed_at = float("nan")
        invalid = self.selector.choose(frame)
        self.assertIsNone(invalid["features"]["feature_time"])
        json.dumps(invalid, allow_nan=False)

    def test_only_past_observation_availability_can_train_and_caller_cannot_backdate(self):
        self.seed()
        first = self.selector.choose(self.frame())  # available_at == decision_at
        self.assertEqual(first["scores"]["MAIN"]["cohorts"], 0)
        self.now += 300
        second = self.selector.choose(self.frame())
        self.assertEqual(second["scores"]["MAIN"]["cohorts"], 6)
        cohort = self.cohort(BASE-300)
        cohort["available_at"] = BASE-1000
        with self.assertRaises(ValueError):
            self.selector.observe(cohort)

    def test_all_no_trade_common_cohorts_continue_learning_without_division_by_zero(self):
        self.seed(values={k: None for k in CANDIDATES})
        self.now += 1
        result = self.selector.choose(self.frame())
        self.assertEqual(self.selector.status()["cohorts"], 6)
        for score in result["scores"].values():
            self.assertEqual(score["cohorts"], 6)
            self.assertEqual(score["active_entries"], 0)
            self.assertEqual(score["mean"], 0)
        self.assertIsNone(result["candidate_key"])

    def test_zero_opportunities_remain_in_the_same_denominator(self):
        cohorts = [self.cohort(start, values={"MAIN": .5 if i % 2 else None})
                   for i, start in enumerate(range(int(BASE-3600), int(BASE-1800), 300))]
        self.prepare(cohorts)
        for cohort in cohorts:
            self.selector.observe(cohort)
        self.now += 1
        result = self.selector.choose(self.frame())["scores"]["MAIN"]
        self.assertEqual(result["cohorts"], 6)
        self.assertEqual(result["active_entries"], 3)
        self.assertAlmostEqual(result["mean"], (.5-.015)/1.015/2)

    def test_raw_profit_that_disappears_under_cost_stress_cannot_select_an_arm(self):
        self.seed(values={k: .005 for k in CANDIDATES})
        self.now += 1
        result = self.selector.choose(self.frame())
        self.assertIsNone(result["candidate_key"])
        self.assertEqual(result["reason"], "no_positive_cost_stressed_edge")
        self.assertGreater(result["scores"]["MAIN"]["mean_net_return"], 0)
        self.assertLess(result["scores"]["MAIN"]["mean"], 0)

    def test_activation_requires_new_closed_blocks_not_repeated_ticks_or_rounds(self):
        self.seed(extra_starts=(BASE-600,))
        self.now += 1
        first = self.selector.choose(self.frame())
        self.assertEqual(first["selection_state"]["pending_confirmations"], 1)
        self.now += 300
        second = self.selector.choose(self.frame())
        self.assertIsNone(second["candidate_key"])
        self.assertEqual(second["selection_state"]["pending_confirmations"], 1)
        self.selector.observe(self.cohort(BASE-600))
        self.now += 300
        third = self.selector.choose(self.frame())
        self.assertEqual(third["candidate_key"], "MAIN")
        self.assertEqual(third["reason"], "selected_after_block_confirmation")

    def test_pending_block_is_not_used_as_finished_confirmation(self):
        self.seed(extra_starts=(BASE-300,))
        self.selector.observe(self.cohort(BASE-300))
        self.now += 1
        result = self.selector.choose(self.frame())
        # Current 600-second block is [BASE,BASE+600); this completed market
        # belongs to the previous block and is therefore valid.
        self.assertEqual(result["scores"]["MAIN"]["cohorts"], 6)
        self.now = BASE+305
        cohort = self.cohort(BASE)
        cohort["feature_time"] = result["features"]["feature_time"]
        self.selector.observe(cohort)
        self.now += 1
        result = self.selector.choose(self.frame())
        self.assertLess(result["scores"]["MAIN"]["cohorts"], 6)
        self.assertLessEqual(result["features"]["training_latest_round_end"], BASE)

    def test_a_bad_incumbent_can_go_flat_without_forcing_hold(self):
        self.activate()
        self.now = BASE+9005
        self.seed(values={k: -1 for k in CANDIDATES}, ending_at=BASE+8400)
        self.now += 1
        result = self.selector.choose(self.frame())
        self.assertIsNone(result["candidate_key"])
        self.assertEqual(result["reason"], "no_positive_cost_stressed_edge")
        self.assertEqual(result["selection_state"]["incumbent"], "MAIN")

    def test_switch_needs_paired_advantage_and_fresh_block_confirmation(self):
        self.activate()
        self.now = BASE+9005
        self.seed(winner="PLUS", ending_at=BASE+8400, extra_starts=(BASE+8400,))
        self.now += 1
        waiting = self.selector.choose(self.frame())
        self.assertIsNone(waiting["candidate_key"])  # MAIN lost eligibility.
        self.assertEqual(waiting["selection_state"]["pending_candidate"], "PLUS")
        self.assertGreater(waiting["scores"]["PLUS"]["paired_vs_incumbent"]["lower_score"], .005)
        self.selector.observe(self.cohort(BASE+8400, winner="PLUS"))
        self.now += 600
        changed = self.selector.choose(self.frame())
        self.assertEqual(changed["candidate_key"], "PLUS")

    def test_tiny_new_champion_does_not_clear_paired_switch_margin(self):
        self.activate()
        self.now = BASE+9005
        self.seed(winner="PLUS", values={"MAIN": .5, "PLUS": .505}, ending_at=BASE+8400)
        self.now += 1
        result = self.selector.choose(self.frame())
        self.assertEqual(result["candidate_key"], "MAIN")
        self.assertEqual(result["reason"], "insufficient_paired_switch_advantage")
        self.assertLess(result["scores"]["PLUS"]["paired_vs_incumbent"]["lower_score"], .005)

    def test_positive_incumbent_respects_minimum_hold_after_challenger_confirmation(self):
        self.path = self.path.with_name("long-hold.sqlite3")
        self.store = self.open_store()
        self.selector = self.build(self.store, min_hold_rounds=100)
        self.activate()
        self.now = BASE+9005
        self.seed(winner="PLUS", values={"MAIN": .1}, ending_at=BASE+8400,
                  extra_starts=(BASE+8400,))
        self.now += 1
        self.assertEqual(self.selector.choose(self.frame())["candidate_key"], "MAIN")
        self.selector.observe(self.cohort(BASE+8400, winner="PLUS", values={"MAIN": .1}))
        self.now += 600
        held = self.selector.choose(self.frame())
        self.assertEqual(held["candidate_key"], "MAIN")
        self.assertEqual(held["reason"], "minimum_hold_hysteresis")
        self.now = BASE+30_305
        switched = self.selector.choose(self.frame())
        self.assertEqual(switched["candidate_key"], "PLUS")

    def test_history_from_another_regime_does_not_remove_cold_start(self):
        self.seed()
        self.now += 1
        self.mode = "range"
        result = self.selector.choose(self.frame())
        self.assertEqual(result["regime"], "range")
        self.assertIsNone(result["candidate_key"])
        self.assertEqual(result["scores"]["MAIN"]["cohorts"], 0)

    def test_partial_future_or_contradictory_cohorts_cannot_learn(self):
        valid = self.cohort(BASE-300)
        self.prepare([valid])
        invalid = []
        c = deepcopy(valid); c["coverage_complete"] = False; invalid.append(c)
        c = deepcopy(valid); del c["arms"]["PLUS"]; invalid.append(c)
        c = deepcopy(valid); c["arms"]["MAIN"]["status"] = "PENDING"; invalid.append(c)
        c = deepcopy(valid); c["arms"]["MAIN"]["pnl"] = float("nan"); invalid.append(c)
        c = deepcopy(valid); c["arms"]["MAIN"]["status"] = "NO_TRADE"; invalid.append(c)
        invalid.append(self.cohort(BASE))
        for cohort in invalid:
            with self.subTest(cohort=cohort), self.assertRaises(ValueError):
                self.selector.observe(cohort)
        self.assertEqual(self.selector.status()["cohorts"], 0)
        first = self.selector.observe(valid)
        self.now += 1
        repeat = self.selector.observe(deepcopy(valid))
        self.assertFalse(repeat["inserted"])
        self.assertEqual(first["available_at"], repeat["available_at"])
        conflict = deepcopy(valid)
        conflict["arms"]["MAIN"]["pnl"] += .1
        conflict["arms"]["MAIN"]["shares"] += .1
        with self.assertRaisesRegex(ValueError, "Contradictory"):
            self.selector.observe(conflict)
        self.assertEqual(self.selector.status()["cohorts"], 1)

    def test_missing_or_post_end_decision_cannot_backfill_a_regime(self):
        with self.assertRaisesRegex(ValueError, "pre-recorded"):
            self.selector.observe(self.cohort(BASE-300))
        frame = self.frame()
        self.now = BASE+301
        self.selector.choose(frame)  # An expired frame records only an abstention.
        with self.assertRaisesRegex(ValueError, "pre-recorded"):
            self.selector.observe(self.cohort(BASE))

    def test_observed_regime_must_match_the_original_frame_not_later_market(self):
        frame = self.frame()
        decision = self.selector.choose(frame)
        self.now += 301
        cohort = self.cohort(frame.market.started_at, regime=decision["regime"])
        cohort["feature_time"] = frame.observed_at
        self.selector.observe(cohort)
        other = deepcopy(cohort)
        other["round_id"] = f"round-{int(BASE+300)}"
        other["started_at"] += 300; other["ends_at"] += 300; other["feature_time"] += 300
        current = self.selector.choose(self.frame())
        self.now += 301
        other["feature_time"] = current["decision_at"]+1
        with self.assertRaises(ValueError):
            self.selector.observe(other)

    def test_config_is_sealed_and_status_does_not_sample_or_advance_state(self):
        self.selector.choose(self.frame())
        before = self.selector.status()
        changes = self.store.conn.total_changes
        self.now += 90000
        self.assertEqual(self.selector.status(), before)
        self.assertEqual(self.store.conn.total_changes, changes)
        with self.assertRaisesRegex(ValueError, "Frozen"):
            self.build(self.open_store(), switch_margin=.01)
        with self.assertRaises(TypeError):
            self.selector.config["switch_margin"] = .01

    def test_restart_clock_rollback_cannot_retrieve_a_future_decision(self):
        frame = self.frame()
        self.selector.choose(frame)
        reopened = self.build(self.open_store())
        self.now -= 1
        with self.assertRaisesRegex(ValueError, "backwards"):
            reopened.choose(frame)
        with self.assertRaisesRegex(ValueError, "backwards"):
            reopened.observe(self.cohort(BASE-300))

    def test_outer_transaction_is_neither_committed_nor_rolled_back(self):
        self.store.conn.execute("CREATE TABLE caller_work (n INTEGER)")
        self.store.conn.execute("INSERT INTO caller_work VALUES (1)")
        for action in (lambda: self.selector.choose(self.frame()),
                       lambda: self.selector.observe(self.cohort(BASE-300))):
            with self.assertRaisesRegex(RuntimeError, "own transaction"):
                action()
            self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 0)

    def test_default_parameters_are_research_constants_and_four_arms_are_fixed(self):
        self.assertEqual(set(CANDIDATES), {"MAIN", "MAIN_FLOOR_060", "FAST", "PLUS"})
        self.assertEqual(SELECTOR_CONFIG["stress_per_share"], .01)
        self.assertEqual(SELECTOR_CONFIG["min_regime_cohorts"], 60)
        self.assertEqual(SELECTOR_CONFIG["switch_confirmations"], 3)

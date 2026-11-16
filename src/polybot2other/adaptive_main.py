"""Isolated adaptive Paper strategy seeded from MAIN_OPTIMIZED history."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import math
import threading
import time

from .adaptive_v3_model import AdaptiveV3Model as AdaptiveModel
from .adaptive_features import FEATURE_FIELDS, current_reference_features
from .optimized_main import OptimizedMainBot
from .models import Signal


VARIANT_ID = "MAIN_ADAPTIVE"
POLICY_VERSION = "main-adaptive-20261116-v3"
DECISION_JOURNAL_BYTES = 4 * 1024 * 1024
_DECISION_FIELDS = frozenset({
    "decision_id", "decision_at", "observed_at", "round_id", "mode", "code", "state", "reason",
    "execution_state", "execution_reason",
    "candidate_side", "base_up_score", "base_selected_score", "up_score", "score",
    "adaptive_model_version", "adaptive_state", "adaptive_score", "adaptive_delta",
    "adaptive_expected_edge", "adaptive_effective_samples", "adaptive_samples",
    "adaptive_training_cutoff", "adaptive_bucket", "adaptive_allowed", "adaptive_reason",
    "adaptive_seed_samples", "adaptive_own_samples", "adaptive_fallback_reason",
    "adaptive_candidate_score", "adaptive_candidate_delta", "adaptive_candidate_allowed",
    "adaptive_candidate_expected_edge", "adaptive_validation_state", "adaptive_validation_reason",
    "adaptive_validation_samples", "adaptive_validation_effective_samples",
    "entry_price", "all_in_cost", "min_edge", "limit_price", "fee_rate", "fee_exponent",
    "fee_per_share", "ask", "bid", "quote_source_ms", "quote_received_ms",
    "time_left_seconds", "remaining_seconds", "projected_final_twap", "target_price",
    "move_bps", "snapshot_id", "order_id", "stage", "submission_limit",
    "submission_all_in_cost", "submission_expected_edge", "market_ends_at", "started_at", "fee_source",
    "v3_training_fingerprint", "v3_model_state", "v3_actual_correction", *FEATURE_FIELDS,
})


def compact_adaptive_decision(features, *, mode=None, **updates):
    """Copy only bounded scalar evidence, never references, wallet data or credentials."""
    values = {**(features if isinstance(features, dict) else {}), **updates}
    if mode is not None:
        values["mode"] = mode
    result = {}
    for key in _DECISION_FIELDS:
        value = values.get(key)
        if isinstance(value, bool) or value is None:
            if key in values: result[key] = value
        elif isinstance(value, (int, float)) and math.isfinite(value):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:240]
    return result


class AdaptiveDecisionJournal:
    """Two bounded local files, sampled at five seconds or a semantic state change."""
    def __init__(self, path, mode, max_bytes=DECISION_JOURNAL_BYTES):
        self.path, self.mode, self.max_bytes = Path(path), mode, max_bytes
        self.lock = threading.RLock()
        self._last_key = None
        self._last_at = 0.0
        self.error = None

    def record(self, features, *, force=False):
        row = compact_adaptive_decision(features, mode=self.mode)
        now = time.time()
        key = tuple(row.get(k) for k in ("round_id", "code", "state", "candidate_side", "adaptive_allowed", "stage"))
        with self.lock:
            if not force and key == self._last_key and now-self._last_at < 5:
                return False
            row["logged_at"] = now
            encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(encoded.encode("utf-8")) > self.max_bytes:
                    self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(encoded)
            except OSError as exc:
                # Diagnostic sampling must not become an entry suspension mechanism.
                # Required per-order provenance still commits with the order itself.
                self.error = type(exc).__name__
                return False
            self.error = None
            self._last_key, self._last_at = key, now
        return True



class AdaptiveMainBot(OptimizedMainBot):
    """Reuse the optimized execution contract with bounded score calibration."""

    strategy_label = "主账户自适应成长版"
    independent_runtime = True

    def __init__(self, settings, store):
        super().__init__(settings, store)
        seed_path = Path(settings.strategy_experiments_db_dir) / "main_optimized.sqlite3"
        if not seed_path.exists():
            adjacent = Path(settings.db_path).with_name("main_optimized.sqlite3")
            seed_path = adjacent if adjacent.exists() else seed_path
        model_path = Path(settings.strategy_experiments_db_dir) / "main_adaptive.model.sqlite3"
        self.adaptive_model = AdaptiveModel(model_path, seed_path, Path(settings.db_path),
            reference_path=Path(settings.managed_live_dir)/"market.references.sqlite3")
        self.policy_version = POLICY_VERSION
        self._adaptive_legacy_journal = AdaptiveDecisionJournal(
            model_path.parent / "main_adaptive.paper-legacy-decisions-v3.jsonl", "paper")

    def adjust_prediction(self, prediction, market, now):
        return self.adaptive_model.adjust_prediction(prediction, market, now)

    def evaluate(self, market, reference, quotes, now):
        """Keep the simpler Paper adapter usable, with explicitly labeled costs."""
        signal, features = super().evaluate(market, reference, quotes, now)
        features = {**features, "round_id": market.round_id, "decision_at": now, "observed_at": now,
                    "market_ends_at": market.ends_at, "started_at": market.started_at,
                    "time_left_seconds": market.ends_at-now, "mode": "paper",
                    "fee_source": "configured-paper"}
        if self.store.trades_for_round(market.round_id):
            reason = "成长版本期已有成交，禁止同一期反手或重复开仓"
            features.update(code="SAME_ROUND_ENTRY", reason=reason, adaptive_allowed=False, state="rejected")
            self._adaptive_legacy_journal.record(features)
            return Signal("BTC", "NO_TRADE", 0, 0, 0, reason), features
        limit = features.get("limit_price")
        if signal.side in ("Up", "Down") and limit is not None:
            fee = self.settings.paper_taker_fee_rate * limit * (1-limit)
            features.update(current_reference_features(getattr(self,"references",None),decision_at=now,
                ask=features.get("ask"),bid=features.get("bid"),target_price=market.target_price))
            assessment = self.adaptive_model.assess_entry(
                features, market, now, entry_price=signal.entry_price, all_in_cost=limit+fee,
                min_edge=max(.02, self.settings.min_edge))
            features.update(assessment, fee_rate=self.settings.paper_taker_fee_rate,
                            fee_exponent=1, fee_per_share=fee, move_bps=signal.move_bps)
            self.adaptive_model.record_observation(compact_adaptive_decision(features, mode="paper"))
            if assessment["adaptive_allowed"]:
                signal = replace(signal, confidence=round(assessment["adaptive_score"], 6),
                    reason="主账户自适应成长版：简化 Paper 配置费率，校准后优势 "
                           + format(assessment["adaptive_expected_edge"], ".4f"))
            else:
                signal = Signal("BTC", "NO_TRADE", 0, 0, 0,
                    "主账户自适应成长版 [ADAPTIVE_EDGE] " + assessment["adaptive_reason"])
                features["code"] = "ADAPTIVE_EDGE"
        if signal.side not in ("Up", "Down"):
            features["adaptive_allowed"] = False
        features.update(reason=signal.reason, state="candidate" if signal.side in ("Up", "Down") else "rejected")
        self._adaptive_legacy_journal.record(features)
        return signal, features

    def _audit(self, record):
        # The legacy engine used to append full reference histories without a cap.
        self._adaptive_legacy_journal.record(record, force=record.get("event") == "execution")

    def diagnostics(self):
        result = super().diagnostics()
        result["policy_version"] = POLICY_VERSION
        result["adaptive_learning"] = self.adaptive_model.snapshot()
        result["journal_path"] = str(self._adaptive_legacy_journal.path if self.paper_execution is None
                                     else self.paper_execution.signals.adaptive_journal.path)
        result["decision_journal_error"] = (self._adaptive_legacy_journal.error if self.paper_execution is None
                                             else self.paper_execution.signals.adaptive_journal.error)
        result["execution_cost_source"] = "official-market" if self.paper_execution is not None else "configured-paper"
        return result

    def learning_snapshot(self):
        snapshot = self.adaptive_model.snapshot()
        with self._lock:
            decision = dict(self.last_decision or {})
        snapshot.update(
            {
                "last_adjustment": decision.get("adaptive_delta"),
                "last_adaptive_score": decision.get("adaptive_score"),
                "last_adaptive_state": decision.get("adaptive_state"),
                "last_adaptive_bucket": decision.get("adaptive_bucket"),
                "last_adaptive_samples": decision.get("adaptive_samples"),
                "last_base_score": decision.get("base_selected_score"),
                "last_expected_edge": decision.get("adaptive_expected_edge"),
                "last_all_in_cost": decision.get("all_in_cost"),
                "last_effective_samples": decision.get("adaptive_effective_samples"),
                "last_training_cutoff": decision.get("adaptive_training_cutoff"),
                "last_entry_allowed": decision.get("adaptive_allowed"),
                "last_entry_reason": decision.get("execution_reason") or decision.get("adaptive_reason") or decision.get("reason"),
                "last_adaptive_reason": decision.get("execution_reason") or decision.get("adaptive_reason") or decision.get("reason"),
                "last_candidate_score": decision.get("adaptive_candidate_score"),
                "last_candidate_delta": decision.get("adaptive_candidate_delta"),
                "last_validation_state": decision.get("adaptive_validation_state"),
                "last_validation_reason": decision.get("adaptive_validation_reason"),
                "last_validation_samples": decision.get("adaptive_validation_samples"),
                "last_decision_at": decision.get("decision_at"),
            }
        )
        return snapshot

    def stop(self):
        try:
            self.adaptive_model.close()
        finally:
            super().stop()


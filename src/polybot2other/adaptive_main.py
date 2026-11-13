"""Isolated adaptive Paper strategy seeded from MAIN_OPTIMIZED history."""
from __future__ import annotations

from pathlib import Path

from .adaptive_model import AdaptiveModel
from .optimized_main import OptimizedMainBot


VARIANT_ID = "MAIN_ADAPTIVE"
POLICY_VERSION = "main-adaptive-20261112-v1"


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
        self.adaptive_model = AdaptiveModel(model_path, seed_path, Path(settings.db_path))
        self.policy_version = POLICY_VERSION

    def adjust_prediction(self, prediction, market, now):
        return self.adaptive_model.adjust_prediction(prediction, market, now)

    def diagnostics(self):
        result = super().diagnostics()
        result["policy_version"] = POLICY_VERSION
        result["adaptive_learning"] = self.adaptive_model.snapshot()
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
            }
        )
        return snapshot

    def stop(self):
        try:
            self.adaptive_model.close()
        finally:
            super().stop()


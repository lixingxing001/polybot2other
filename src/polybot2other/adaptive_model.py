"""Conservative online calibration for the isolated adaptive Paper strategy.

The adaptive strategy starts from settled trades copied from the optimized-main
ledger, then appends its own settled trades.  The source ledger is opened
read-only; the calibration database and the adaptive ledger remain isolated.
This module deliberately uses a small, explainable Bayesian-style calibration
instead of an opaque model or an unrestricted parameter optimizer.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any


MODEL_VERSION = "main-adaptive-calibration-v1"
MIN_EXACT_BUCKET_SAMPLES = 12
MIN_ADJUSTMENT_SAMPLES = 8
MAX_ADJUSTMENT = 0.04
MAX_BLEND_WEIGHT = 0.35
PRIOR_WINS = 6.0
PRIOR_LOSSES = 4.0


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _score_bucket(score: float) -> str:
    if score < 0.66:
        return "0.50-0.66"
    if score < 0.72:
        return "0.66-0.72"
    if score < 0.78:
        return "0.72-0.78"
    return "0.78+"


def _move_bucket(move_bps: float) -> str:
    distance = abs(move_bps)
    if distance < 5.0:
        return "0-5"
    if distance < 8.0:
        return "5-8"
    return "8+"


def _bucket(side: str, score: float, move_bps: float) -> str:
    return f"{side}|{_score_bucket(score)}|{_move_bucket(move_bps)}"


def _read_settled_rows(path: Path) -> list[dict[str, Any]]:
    """Read settled trades without ever writing to the source ledger."""

    if not path.exists():
        return []
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, round_id, side, confidence, move_bps, entry_price,
                       opened_at, settled_at, pnl, status
                FROM trades
                WHERE status = 'SETTLED'
                  AND settled_at IS NOT NULL
                ORDER BY settled_at ASC, id ASC
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    return [dict(row) for row in rows]


class AdaptiveModel:
    """Persistent, conservative calibration state for MAIN_ADAPTIVE."""

    def __init__(self, model_path: Path, seed_path: Path, adaptive_path: Path | None = None):
        self.path = Path(model_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seed_path = Path(seed_path)
        self.adaptive_path = Path(adaptive_path) if adaptive_path else None
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS training_samples (
                source_key TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                source_trade_id INTEGER NOT NULL,
                round_id TEXT NOT NULL,
                side TEXT NOT NULL,
                confidence REAL NOT NULL,
                move_bps REAL NOT NULL,
                entry_price REAL NOT NULL,
                opened_at REAL NOT NULL,
                settled_at REAL NOT NULL,
                pnl REAL NOT NULL,
                won INTEGER NOT NULL,
                imported_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        self._last_sync = 0.0
        self._rows: list[dict[str, Any]] = []
        self.sync(force=True)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _import_rows(self, path: Path, source_name: str) -> int:
        rows = _read_settled_rows(path)
        if not rows:
            return 0
        now = time.time()
        imported = 0
        with self.conn:
            for row in rows:
                try:
                    trade_id = int(row["id"])
                    round_id = str(row["round_id"])
                    side = str(row["side"])
                    confidence = _finite(row["confidence"])
                    move_bps = _finite(row["move_bps"])
                    entry_price = _finite(row["entry_price"])
                    opened_at = _finite(row["opened_at"])
                    settled_at = _finite(row["settled_at"])
                    pnl = _finite(row["pnl"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if (
                    not round_id
                    or side not in {"Up", "Down"}
                    or confidence is None
                    or move_bps is None
                    or entry_price is None
                    or opened_at is None
                    or settled_at is None
                    or pnl is None
                    or confidence < 0.5
                    or confidence > 1.0
                    or entry_price <= 0
                ):
                    continue
                key = f"{source_name}:{trade_id}"
                cursor = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO training_samples(
                        source_key, source_name, source_trade_id, round_id, side,
                        confidence, move_bps, entry_price, opened_at, settled_at,
                        pnl, won, imported_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        source_name,
                        trade_id,
                        round_id,
                        side,
                        confidence,
                        move_bps,
                        entry_price,
                        opened_at,
                        settled_at,
                        pnl,
                        int(pnl > 0),
                        now,
                    ),
                )
                imported += int(cursor.rowcount > 0)
        return imported

    def sync(self, *, force: bool = False) -> None:
        with self.lock:
            now = time.time()
            if not force and now - self._last_sync < 5.0:
                return
            # The optimized ledger is a read-only seed.  New source rows are
            # imported as they settle, so the adaptive strategy can keep
            # learning from the original strategy without sharing its ledger.
            self._import_rows(self.seed_path, "MAIN_OPTIMIZED")
            if self.adaptive_path and self.adaptive_path.resolve() != self.seed_path.resolve():
                self._import_rows(self.adaptive_path, "MAIN_ADAPTIVE")
            self._rows = [dict(row) for row in self.conn.execute(
                "SELECT * FROM training_samples ORDER BY settled_at ASC, source_key ASC"
            ).fetchall()]
            self._last_sync = now
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO model_meta(key,value) VALUES('model_version',?)",
                    (MODEL_VERSION,),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO model_meta(key,value) VALUES('last_sync_at',?)",
                    (str(now),),
                )

    def _stats(self, rows: list[dict[str, Any]]) -> tuple[int, int, float]:
        total = len(rows)
        wins = sum(int(row.get("won") or 0) for row in rows)
        posterior = (wins + PRIOR_WINS) / (total + PRIOR_WINS + PRIOR_LOSSES)
        return total, wins, posterior

    def _matching_rows(self, side: str, score: float, move_bps: float) -> tuple[list[dict[str, Any]], str]:
        exact = _bucket(side, score, move_bps)
        exact_rows = [
            row for row in self._rows
            if _bucket(str(row["side"]), float(row["confidence"]), float(row["move_bps"])) == exact
        ]
        if len(exact_rows) >= MIN_EXACT_BUCKET_SAMPLES:
            return exact_rows, exact
        score_key = _score_bucket(score)
        side_rows = [
            row for row in self._rows
            if str(row["side"]) == side and _score_bucket(float(row["confidence"])) == score_key
        ]
        if len(side_rows) >= MIN_ADJUSTMENT_SAMPLES:
            return side_rows, f"{side}|{score_key}|fallback"
        score_rows = [
            row for row in self._rows
            if _score_bucket(float(row["confidence"])) == score_key
        ]
        if len(score_rows) >= MIN_ADJUSTMENT_SAMPLES:
            return score_rows, f"ALL|{score_key}|fallback"
        if len(self._rows) >= MIN_ADJUSTMENT_SAMPLES:
            return list(self._rows), "GLOBAL|fallback"
        return [], "INSUFFICIENT"

    def adjust_prediction(self, prediction: dict[str, Any], market: Any, now: float) -> dict[str, Any]:
        """Return a bounded calibration of the base prediction."""

        self.sync()
        base_up = _finite(prediction.get("up_score"))
        projected = _finite(prediction.get("projected_final_twap"))
        target = _finite(getattr(market, "target_price", None))
        if base_up is None or projected is None or target is None or target <= 0:
            return prediction
        base_up = min(0.9, max(0.1, base_up))
        base_side = "Up" if base_up >= 0.5 else "Down"
        base_score = base_up if base_side == "Up" else 1.0 - base_up
        move_bps = (projected - target) / target * 10_000.0
        with self.lock:
            rows, bucket = self._matching_rows(base_side, base_score, move_bps)
            total, wins, posterior = self._stats(rows) if rows else (0, 0, 0.5)
        if total < MIN_ADJUSTMENT_SAMPLES:
            adjusted_score = base_score
            delta = 0.0
            state = "BASELINE_FALLBACK"
        else:
            # Start adapting only after a small independent sample, then cap
            # the influence so a short regime cannot rewrite the base model.
            reliability = min(MAX_BLEND_WEIGHT, max(0.0, (total - MIN_ADJUSTMENT_SAMPLES + 1) / 50.0))
            delta = (posterior - base_score) * reliability
            delta = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, delta))
            adjusted_score = min(0.9, max(0.5, base_score + delta))
            state = "CALIBRATING" if total < 60 else "ADAPTING"
        adjusted_up = adjusted_score if base_side == "Up" else 1.0 - adjusted_score
        result = dict(prediction)
        result.update(
            {
                "base_up_score": base_up,
                "base_selected_score": base_score,
                "up_score": adjusted_up,
                "adaptive_score": adjusted_score,
                "adaptive_delta": delta,
                "adaptive_posterior": posterior,
                "adaptive_samples": total,
                "adaptive_wins": wins,
                "adaptive_bucket": bucket,
                "adaptive_state": state,
                "adaptive_model_version": MODEL_VERSION,
                "adaptive_observed_at": now,
            }
        )
        return result

    def snapshot(self) -> dict[str, Any]:
        self.sync()
        with self.lock:
            source_counts = Counter(str(row["source_name"]) for row in self._rows)
            settled = [float(row["settled_at"]) for row in self._rows if _finite(row.get("settled_at")) is not None]
            return {
                "model_version": MODEL_VERSION,
                "model_path": str(self.path),
                "seed_path": str(self.seed_path),
                "adaptive_path": str(self.adaptive_path) if self.adaptive_path else None,
                "total_samples": len(self._rows),
                "seed_samples": int(source_counts.get("MAIN_OPTIMIZED", 0)),
                "adaptive_samples": int(source_counts.get("MAIN_ADAPTIVE", 0)),
                "latest_settled_at": max(settled) if settled else None,
                "last_sync_at": self._last_sync,
                "min_exact_bucket_samples": MIN_EXACT_BUCKET_SAMPLES,
                "min_adjustment_samples": MIN_ADJUSTMENT_SAMPLES,
                "max_adjustment": MAX_ADJUSTMENT,
                "state": "READY" if len(self._rows) >= MIN_ADJUSTMENT_SAMPLES else "WARMING",
            }


"""Independent efficiency experiment; its name is not a return guarantee.

The optimized TWAP estimator and original strategies remain unchanged. This
variant checks weak scores, repeated entries and transient forecasts explicitly.
No historical PnL or future settlement is read by the live decision function.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from .models import Signal
from .optimized_main import OptimizedMainBot, forecast_twap
from .optimized_reference import SPOT

VARIANT_ID = "MAIN_OPTIMIZED_FAST"
LABEL = "主账户（优化极速赚钱版）"
POLICY_VERSION = "main-optimized-fast-20261116-v1"
POLICY = {
    "min_confidence": .66, "min_edge": .02, "min_expected_roi": .03,
    "max_entry_price": .72, "max_spread": .03, "max_entry_slippage": .01,
    "min_time_left_seconds": 20, "max_time_left_seconds": 285,
    "min_depth_multiple": 1.0, "one_entry_per_market": True,
    "confirmation_samples": 3, "confirmation_seconds": 6,
}
_FIELDS = frozenset({
    "policy_version", "decision_id", "decision_at", "observed_at", "round_id", "mode",
    "code", "state", "reason", "stage", "execution_reason", "execution_state",
    "fast_allowed", "candidate_side", "score", "base_selected_score", "up_score",
    "entry_price", "ask", "bid", "all_in_cost", "limit_price", "min_edge",
    "min_expected_roi", "expected_edge", "expected_roi", "fee_rate", "fee_exponent",
    "fee_per_share", "fee_source", "fee_adjusted_edge", "quote_source_ms", "quote_received_ms",
    "time_left_seconds", "market_ends_at", "started_at", "target_price", "move_bps",
    "projected_final_twap", "forecast_source_ms", "snapshot_id", "order_id",
    "submission_limit", "submission_all_in_cost", "submission_expected_edge", "submission_expected_roi",
    "confirmation_samples", "confirmation_span_seconds", "confirmation_min_score",
    "confirmation_start_ms", "confirmation_end_ms", "confirmation_reason",
})


def compact_fast_decision(features, *, mode=None, **updates):
    values = {**(features if isinstance(features, dict) else {}), **updates}
    if mode is not None:
        values["mode"] = mode
    result = {}
    for key in _FIELDS:
        if key not in values:
            continue
        value = values[key]
        if value is None or isinstance(value, bool):
            result[key] = value
        elif isinstance(value, (float, int)) and math.isfinite(value):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:240]
    return result


class FastDecisionJournal:
    """Bounded diagnostic evidence; disk errors must not silently stop trading."""
    def __init__(self, path, mode, max_bytes=4*1024*1024):
        self.path, self.mode, self.max_bytes = Path(path), mode, max_bytes
        self.lock = threading.RLock()
        self._last_key, self._last_at, self.error = None, 0., None

    def record(self, features, *, force=False):
        row = compact_fast_decision(features, mode=self.mode)
        now = time.time()
        key = tuple(row.get(k) for k in ("round_id", "code", "state", "candidate_side", "fast_allowed", "stage"))
        with self.lock:
            if not force and key == self._last_key and now-self._last_at < 5:
                return False
            row["logged_at"] = now
            encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))+"\n"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size+len(encoded.encode()) > self.max_bytes:
                    self.path.replace(self.path.with_suffix(self.path.suffix+".1"))
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(encoded)
            except OSError as exc:
                self.error = type(exc).__name__
                return False
            self.error, self._last_key, self._last_at = None, key, now
            return True


def assess_fast_cost(score, all_in_cost, *, min_edge=None):
    edge_floor = max(POLICY["min_edge"], min_edge if min_edge is not None else POLICY["min_edge"])
    valid = all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (score, all_in_cost, edge_floor))
    valid = valid and .5 <= score <= 1 and 0 < all_in_cost < 1
    edge = score-all_in_cost if valid else None
    roi = score/all_in_cost-1 if valid else None
    allowed = bool(valid and edge+1e-12 >= edge_floor and roi+1e-12 >= POLICY["min_expected_roi"])
    return {"allowed": allowed, "min_edge": edge_floor, "min_expected_roi": POLICY["min_expected_roi"],
            "expected_edge": edge, "expected_roi": roi,
            "reason": (f"含费优势 {edge:.4f}，单位预算模型优势 {roi:.2%}" if valid else "报价成本或模型分数无效")}


def confirm_fast_prediction(references, market, latest, prediction, *, minimum_score):
    """Three causal forecasts from already available source history, not polling counts."""
    newest = prediction.get("forecast_source_ms")
    if not isinstance(newest, (int, float)) or not math.isfinite(newest):
        return {"confirmed": False, "confirmation_reason": "缺少可追溯的行情源时间"}
    history = references.points(SPOT, newest-10000, newest)
    anchors = []
    for offset in (6000, 3000, 0):
        target = newest-offset
        point = next(((stamp, price) for stamp, price in reversed(history) if stamp <= target), None)
        if point is None or target-point[0] > 2000 or point[0] < market.started_at*1000:
            return {"confirmed": False, "confirmation_reason": "等待连续的同源六秒确认数据"}
        anchors.append(point)
    if len({stamp for stamp, _ in anchors}) != 3 or anchors[-1][0]-anchors[0][0] < 6000:
        return {"confirmed": False, "confirmation_reason": "行情源时间未推进，重复推送不计确认"}
    side = "Up" if prediction["up_score"] >= .5 else "Down"
    scores = []
    for stamp, price in anchors:
        historical = dict(latest)
        historical[SPOT] = {**latest[SPOT], "price": price, "source_ms": stamp}
        previous = prediction if stamp == newest else forecast_twap(references, market, historical)
        if previous is None:
            return {"confirmed": False, "confirmation_reason": "历史预测所需的连续行情不足"}
        score = previous["up_score"] if side == "Up" else 1-previous["up_score"]
        scores.append(score)
    confirmed = all(math.isfinite(score) and score >= minimum_score for score in scores)
    return {"confirmed": confirmed, "confirmation_samples": 3,
            "confirmation_span_seconds": (anchors[-1][0]-anchors[0][0])/1000,
            "confirmation_start_ms": anchors[0][0], "confirmation_end_ms": anchors[-1][0],
            "confirmation_min_score": min(scores),
            "confirmation_reason": "连续三个预测同向通过" if confirmed else "六秒内方向或分数不稳定，继续观察最新行情"}


def fast_settings(settings):
    return replace(settings, min_confidence=max(settings.min_confidence, POLICY["min_confidence"]),
        min_edge=max(settings.min_edge, POLICY["min_edge"]),
        max_entry_price=min(settings.max_entry_price, POLICY["max_entry_price"]),
        max_spread=min(settings.max_spread, POLICY["max_spread"]),
        min_time_left_seconds=max(settings.min_time_left_seconds, POLICY["min_time_left_seconds"]),
        max_time_left_seconds=min(settings.max_time_left_seconds, POLICY["max_time_left_seconds"]))


class FastMainBot(OptimizedMainBot):
    strategy_label = LABEL
    policy_version = POLICY_VERSION

    def __init__(self, settings, store):
        super().__init__(settings, store)
        self._fast_legacy_journal = FastDecisionJournal(settings.db_path.with_suffix(".fast-decisions.jsonl"), "paper")
        self.last_signal = {"side": "NO_TRADE", "reason": LABEL+"等待同源行情"}

    def evaluate(self, market, reference, quotes, now):
        settings = fast_settings(self.settings)
        context = SimpleNamespace(settings=settings, references=self.references, store=self.store,
            paper_trading_paused=self.paper_trading_paused, strategy_label=LABEL,
            adjust_prediction=lambda prediction, _market, _now: prediction)
        signal, features = OptimizedMainBot.evaluate(context, market, reference, quotes, now)
        features.update(policy_version=POLICY_VERSION, decision_at=now, observed_at=now,
            market_ends_at=market.ends_at, started_at=market.started_at, fast_allowed=False,
            min_edge=settings.min_edge, min_expected_roi=POLICY["min_expected_roi"], fee_source="not-evaluated")
        def finish(value):
            features.update(reason=value.reason, fast_allowed=value.side in ("Up", "Down"),
                            state="candidate" if value.side in ("Up", "Down") else "rejected")
            return value, features
        if signal.side not in ("Up", "Down"):
            return finish(signal)
        features.update(candidate_side=signal.side, base_selected_score=features["score"],
                        entry_price=signal.entry_price, move_bps=signal.move_bps)
        def reject(code, reason):
            features["code"] = code
            return finish(Signal("BTC", "NO_TRADE", 0, 0, 0, LABEL+" ["+code+"] "+reason))
        if self.store.trades_for_round(market.round_id):
            return reject("SAME_ROUND_ENTRY", "本期已有成交或待确认，不重复开仓或反手")
        confirmation = confirm_fast_prediction(self.references, market, reference["latest"], features,
                                               minimum_score=settings.min_confidence)
        features.update({k:v for k,v in confirmation.items() if k != "confirmed"})
        if not confirmation["confirmed"]:
            return reject("FAST_STABILITY", confirmation["confirmation_reason"])
        limit = features["limit_price"]
        cost = limit+settings.paper_taker_fee_rate*limit*(1-limit)
        assessment = assess_fast_cost(features["score"], cost, min_edge=settings.min_edge)
        features.update({k:v for k,v in assessment.items() if k not in ("allowed", "reason")}, all_in_cost=cost,
                        fee_source=getattr(self, "execution_cost_source", "configured-paper"))
        if not assessment["allowed"]:
            return reject("FAST_COST", assessment["reason"])
        return finish(replace(signal, reason=LABEL+" [ALLOW] 连续同向确认；"+assessment["reason"]))

    def _entry_limit_price(self, signal):
        return min(self.settings.max_entry_price, POLICY["max_entry_price"], signal.entry_price+POLICY["max_entry_slippage"])

    def _audit(self, record):
        self._fast_legacy_journal.record({"policy_version": POLICY_VERSION, **record},
                                        force=record.get("event") == "execution")

    def fast_policy_snapshot(self):
        settings = fast_settings(self.settings)
        parameters = dict(POLICY)
        for key in ("min_confidence", "min_edge", "max_entry_price", "max_spread",
                    "min_time_left_seconds", "max_time_left_seconds"):
            parameters[key] = getattr(settings, key)
        with self._lock:
            decision = dict(self.last_decision or {})
        decision["reason"] = decision.get("execution_reason") or decision.get("reason")
        return {"policy_version": POLICY_VERSION, "state": "FORWARD_PAPER",
            "description": "独立效率实验：每期一次入场，过滤弱分数，六秒同向确认；沿用官方费用与真实成交模拟。无需新增训练等待，名称不代表收益保证。",
            "parameters": parameters, "last_decision": compact_fast_decision(decision)}

    def diagnostics(self):
        result = super().diagnostics()
        result["fast_policy"] = self.fast_policy_snapshot()
        journal = self._fast_legacy_journal if self.paper_execution is None else self.paper_execution.signals.fast_journal
        result.update(journal_path=str(journal.path), decision_journal_error=journal.error)
        return result

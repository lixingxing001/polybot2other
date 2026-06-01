from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .experiments import (
    ANTI_BOT_GUARD_MODE_ENABLED,
    MARKET_DATA_MODE_BASE,
    SINGLE_ENTRY_MODE_LEGACY,
    SINGLE_ENTRY_MODE_REVERSAL,
    SINGLE_ENTRY_MODE_STOP_AND_FLIP,
)
from .models import MarketRound
from .strategy import multi_source_price_context


LLM_ROUTE_NO_TRADE = "NO_TRADE"
LLM_ROUTE_SINGLE_FAK = "SINGLE_FAK"
LLM_ROUTE_SINGLE_FAK_STOP_AND_FLIP = "SINGLE_FAK_STOP_AND_FLIP"
LLM_ROUTE_SINGLE_FAK_REVERSAL = "SINGLE_FAK_REVERSAL"
LLM_ROUTE_SINGLE_FAK_ANTI_BOT_GUARD = "SINGLE_FAK_ANTI_BOT_GUARD"

LLM_ALLOWED_ROUTES = {
    LLM_ROUTE_NO_TRADE,
    LLM_ROUTE_SINGLE_FAK,
    LLM_ROUTE_SINGLE_FAK_STOP_AND_FLIP,
    LLM_ROUTE_SINGLE_FAK_REVERSAL,
    LLM_ROUTE_SINGLE_FAK_ANTI_BOT_GUARD,
}
LLM_TRADE_ROUTES = LLM_ALLOWED_ROUTES - {LLM_ROUTE_NO_TRADE}
LLM_MIN_CONFIDENCE_TO_TRADE = 0.58
LLM_MAX_PROMPT_CHARS = 10_000


@dataclass(frozen=True)
class LlmRouterDecision:
    """LLM 超级智能体路由结果；只允许路由到已有 Paper 策略。"""

    route: str
    allow_trade: bool
    confidence: float
    market_regime: str
    reason: str
    reason_codes: tuple[str, ...]
    source: str
    valid_until: float
    raw_response: dict[str, Any] | None = None
    error: str | None = None

    @property
    def decision_id(self) -> str:
        codes = ",".join(self.reason_codes[:6])
        return f"{self.source}:{self.route}:{self.market_regime}:{round(self.confidence, 3)}:{codes}"

    def to_record(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "allow_trade": self.allow_trade,
            "confidence": self.confidence,
            "market_regime": self.market_regime,
            "reason": self.reason,
            "reason_codes": list(self.reason_codes),
            "source": self.source,
            "valid_until": self.valid_until,
            "raw_response": self.raw_response,
            "error": self.error,
            "decision_id": self.decision_id,
        }


class LlmSuperAgentRouter:
    """非阻塞 LLM 策略路由器。

    实时 tick 始终先返回本地快脑决策；LLM 请求在后台刷新缓存，避免盘口线程等待网络。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._cached: LlmRouterDecision | None = None
        self._cached_round_id: str | None = None
        self._inflight = False
        self._last_call_at = 0.0
        self._last_error: str | None = None

    def decide(self, features: dict[str, Any], now: float | None = None) -> LlmRouterDecision:
        now = time.time() if now is None else now
        round_id = str(features.get("round_id") or "")
        local = local_llm_router_decision(features, now)
        cached = self._fresh_cached(round_id, now)
        if cached is not None:
            return cached
        self._maybe_start_refresh(features, now)
        with self._lock:
            pending = self._inflight
            last_error = self._last_error
        if pending:
            return _clone_decision(local, source="local_pending_llm")
        if last_error:
            return _clone_decision(local, source="local_llm_error", error=last_error)
        return local

    def _fresh_cached(self, round_id: str, now: float) -> LlmRouterDecision | None:
        with self._lock:
            cached = self._cached
            cached_round_id = self._cached_round_id
        if cached is None or cached_round_id != round_id or cached.valid_until <= now:
            return None
        return cached

    def _maybe_start_refresh(self, features: dict[str, Any], now: float) -> None:
        if not self.settings.llm_super_agent_enabled:
            return
        if not self.settings.llm_super_agent_api_key:
            return
        with self._lock:
            if self._inflight:
                return
            if now - self._last_call_at < self.settings.llm_super_agent_min_interval_seconds:
                return
            self._inflight = True
            self._last_call_at = now
        thread = threading.Thread(
            target=self._refresh_worker,
            args=(dict(features), now),
            name="polybot2other-llm-super-agent",
            daemon=True,
        )
        thread.start()

    def _refresh_worker(self, features: dict[str, Any], now: float) -> None:
        try:
            decision = self._call_llm(features, now)
            with self._lock:
                self._cached = decision
                self._cached_round_id = str(features.get("round_id") or "")
                self._last_error = None
        except Exception as exc:  # noqa: BLE001 - LLM 不能阻塞 Paper 采样。
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._inflight = False

    def _call_llm(self, features: dict[str, Any], now: float) -> LlmRouterDecision:
        base_url = self.settings.llm_super_agent_base_url.rstrip("/")
        url = f"{base_url}/chat/completions"
        body = {
            "model": self.settings.llm_super_agent_model,
            "messages": [
                {"role": "system", "content": _llm_system_prompt()},
                {"role": "user", "content": _llm_user_prompt(features)},
            ],
        }
        raw_body = json.dumps(body, ensure_ascii=True).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=raw_body,
            headers={
                "Authorization": f"Bearer {self.settings.llm_super_agent_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = max(0.2, float(self.settings.llm_super_agent_timeout_seconds))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = _chat_completion_content(payload)
        parsed = _extract_json_object(content)
        return validate_llm_router_payload(parsed, now, fallback_raw={"provider": payload})


def build_llm_market_features(
    market: MarketRound,
    price: dict[str, Any],
    quotes: dict[str, dict[str, Any]],
    *,
    open_sides: list[str],
    open_trade_count: int,
    active_order_count: int,
    daily_pnl: float,
    cash_balance: float,
    max_quote_age_ms: int,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    now_ms = int(now * 1000)
    up = quotes.get("Up") if isinstance(quotes.get("Up"), dict) else {}
    down = quotes.get("Down") if isinstance(quotes.get("Down"), dict) else {}
    chainlink = _maybe_float(price.get("chainlink"))
    fallback = _first_number(price, ("binance_market", "binance", "okx"))
    current = chainlink if chainlink is not None else fallback
    target = _maybe_float(market.target_price)
    signed_distance_bps = (
        (current - target) / target * 10_000.0
        if current is not None and target is not None and target > 0
        else None
    )
    quote_age_ms = _max_age_ms(now_ms, up.get("updated_at_ms"), down.get("updated_at_ms"))
    chainlink_age_ms = _age_ms(now_ms, price.get("chainlink_updated_ms"))
    fallback_updated = _first_number(price, ("binance_market_updated_ms", "binance_updated_ms", "okx_updated_ms"))
    fallback_age_ms = int(max(0, now_ms - fallback_updated)) if fallback_updated else None
    pair_cost = _sum_if_numbers(_maybe_float(up.get("best_ask")), _maybe_float(down.get("best_ask")))
    realtime_probability = price.get("realtime_probability") if isinstance(price.get("realtime_probability"), dict) else {}
    actor_probability = price.get("actor_probability") if isinstance(price.get("actor_probability"), dict) else {}
    multi_context = price.get("multi_context") if isinstance(price.get("multi_context"), dict) else multi_source_price_context(price, now_ms)
    side = "Up" if (signed_distance_bps or 0.0) >= 0 else "Down"
    side_quote = up if side == "Up" else down
    opposite_quote = down if side == "Up" else up
    return {
        "round_id": market.round_id,
        "symbol": market.symbol,
        "time_left_seconds": round(max(0.0, market.ends_at - now), 3),
        "target_price": target,
        "current_price": current,
        "current_price_source": "chainlink" if chainlink is not None else "fallback",
        "signed_distance_bps": signed_distance_bps,
        "direction_side": side if signed_distance_bps is not None else None,
        "chainlink_price": chainlink,
        "chainlink_age_ms": chainlink_age_ms,
        "fallback_price": fallback,
        "fallback_age_ms": fallback_age_ms,
        "okx_price": _maybe_float(price.get("okx")),
        "binance_price": _maybe_float(price.get("binance_market")) or _maybe_float(price.get("binance")),
        "quote_age_ms": quote_age_ms,
        "quote_max_age_ms": max_quote_age_ms,
        "up": _quote_features(up),
        "down": _quote_features(down),
        "side_quote": _quote_features(side_quote),
        "opposite_quote": _quote_features(opposite_quote),
        "pair_cost": pair_cost,
        "multi_context": _compact_multi_context(multi_context),
        "realtime_probability": _compact_probability(realtime_probability),
        "actor_probability": _compact_probability(actor_probability),
        "open_sides": list(open_sides),
        "open_trade_count": int(open_trade_count),
        "active_order_count": int(active_order_count),
        "daily_pnl": round(float(daily_pnl or 0.0), 6),
        "cash_balance": round(float(cash_balance or 0.0), 6),
    }


def local_llm_router_decision(features: dict[str, Any], now: float | None = None) -> LlmRouterDecision:
    now = time.time() if now is None else now
    time_left = _maybe_float(features.get("time_left_seconds"))
    quote_age_ms = _maybe_float(features.get("quote_age_ms"))
    max_quote_age_ms = _maybe_float(features.get("quote_max_age_ms")) or 3000.0
    signed_distance_bps = _maybe_float(features.get("signed_distance_bps"))
    side = str(features.get("direction_side") or "")
    side_quote = features.get("side_quote") if isinstance(features.get("side_quote"), dict) else {}
    side_ask = _maybe_float(side_quote.get("ask"))
    pair_cost = _maybe_float(features.get("pair_cost"))
    open_sides = [str(item) for item in (features.get("open_sides") or [])]
    reason_codes: list[str] = []
    if time_left is None or time_left < 25:
        return _decision(LLM_ROUTE_NO_TRADE, False, 0.2, "NO_TRADE", "临近结算或缺少剩余时间", ["time_left_low"], "local", now)
    if quote_age_ms is None or quote_age_ms > max_quote_age_ms:
        return _decision(LLM_ROUTE_NO_TRADE, False, 0.2, "NO_TRADE", "盘口报价过期", ["quote_stale"], "local", now)
    if signed_distance_bps is None or side not in {"Up", "Down"}:
        return _decision(LLM_ROUTE_NO_TRADE, False, 0.2, "NO_TRADE", "缺少可判断方向", ["missing_direction"], "local", now)
    distance = abs(signed_distance_bps)
    if distance < 2.0:
        return _decision(LLM_ROUTE_NO_TRADE, False, 0.35, "NEAR_TARGET_NOISY", "贴近目标价，噪声占比高", ["near_target"], "local", now)
    if pair_cost is not None and pair_cost <= 0.94 and time_left >= 55:
        return _decision(
            LLM_ROUTE_SINGLE_FAK_STOP_AND_FLIP,
            True,
            0.66,
            "DUAL_LEG_COST_FALLBACK_SINGLE",
            "双边 ask 合成成本较低，但双边组合已淘汰，回退单边 FAK 止损反手路径",
            ["synthetic_cost_low", "dual_leg_route_removed"],
            "local",
            now,
        )
    if side_ask is not None and side_ask > 0.72:
        return _decision(LLM_ROUTE_NO_TRADE, False, 0.42, "RICH_CONTRACT", "当前方向价格过贵", ["entry_too_high"], "local", now)
    if open_sides and any(item != side for item in open_sides):
        return _decision(
            LLM_ROUTE_SINGLE_FAK_STOP_AND_FLIP,
            True,
            0.7,
            "REVERSAL_RISK",
            "当前方向与已有持仓相反，使用止损反手候选",
            ["opposite_open_side", "stop_and_flip"],
            "local",
            now,
        )
    multi = features.get("multi_context") if isinstance(features.get("multi_context"), dict) else {}
    residuals = _ready_residuals_for_side(side, multi)
    if residuals and sum(1 for value in residuals if value < -1.5) >= 1:
        return _decision(
            LLM_ROUTE_SINGLE_FAK_ANTI_BOT_GUARD,
            True,
            0.62,
            "EXTERNAL_DISAGREE_GUARD",
            "外部交易所残差有反向风险，使用防守过滤组合",
            ["external_residual_disagree", "anti_bot_guard"],
            "local",
            now,
        )
    if distance >= 8.0:
        reason_codes.extend(["distance_clear", "stop_and_flip_default"])
        return _decision(
            LLM_ROUTE_SINGLE_FAK_STOP_AND_FLIP,
            True,
            min(0.82, 0.58 + distance / 80.0),
            "TREND_CLEAR",
            "方向距离目标价较清晰，优先使用可止损反手的单边 FAK",
            reason_codes,
            "local",
            now,
        )
    return _decision(
        LLM_ROUTE_SINGLE_FAK_REVERSAL,
        True,
        0.6,
        "MODERATE_EDGE_REVERSAL_ALLOWED",
        "方向不够强但仍有 edge，使用显式反转采样",
        ["moderate_distance", "reversal_allowed"],
        "local",
        now,
    )


def validate_llm_router_payload(
    payload: dict[str, Any],
    now: float | None = None,
    *,
    fallback_raw: dict[str, Any] | None = None,
) -> LlmRouterDecision:
    now = time.time() if now is None else now
    route = str(payload.get("recommended_strategy") or payload.get("route") or LLM_ROUTE_NO_TRADE).strip().upper()
    if route not in LLM_ALLOWED_ROUTES:
        route = LLM_ROUTE_NO_TRADE
    confidence = max(0.0, min(1.0, _maybe_float(payload.get("confidence")) or 0.0))
    allow_trade = _maybe_bool(payload.get("allow_trade")) and route in LLM_TRADE_ROUTES and confidence >= LLM_MIN_CONFIDENCE_TO_TRADE
    if route == LLM_ROUTE_NO_TRADE:
        allow_trade = False
    valid_ms = _maybe_float(payload.get("valid_until_ms")) or 10_000.0
    valid_ms = max(3_000.0, min(30_000.0, valid_ms))
    reason_codes = payload.get("reason_codes")
    if not isinstance(reason_codes, list):
        reason_codes = []
    raw_response = dict(payload)
    if fallback_raw:
        raw_response["_raw"] = fallback_raw
    return LlmRouterDecision(
        route=route,
        allow_trade=allow_trade,
        confidence=round(confidence, 4),
        market_regime=str(payload.get("market_regime") or "UNKNOWN")[:80],
        reason=str(payload.get("reason") or payload.get("summary") or "LLM router decision")[:500],
        reason_codes=tuple(str(item)[:60] for item in reason_codes[:10]),
        source="llm",
        valid_until=now + valid_ms / 1000.0,
        raw_response=raw_response,
    )


def route_execution_modes(route: str) -> dict[str, str]:
    route = str(route or "").upper()
    if route == LLM_ROUTE_SINGLE_FAK_STOP_AND_FLIP:
        return {
            "strategy_family": "SINGLE",
            "single_entry_mode": SINGLE_ENTRY_MODE_STOP_AND_FLIP,
            "market_data_mode": MARKET_DATA_MODE_BASE,
            "anti_bot_guard_mode": "",
        }
    if route == LLM_ROUTE_SINGLE_FAK_REVERSAL:
        return {
            "strategy_family": "SINGLE",
            "single_entry_mode": SINGLE_ENTRY_MODE_REVERSAL,
            "market_data_mode": MARKET_DATA_MODE_BASE,
            "anti_bot_guard_mode": "",
        }
    if route == LLM_ROUTE_SINGLE_FAK_ANTI_BOT_GUARD:
        return {
            "strategy_family": "SINGLE",
            "single_entry_mode": SINGLE_ENTRY_MODE_STOP_AND_FLIP,
            "market_data_mode": MARKET_DATA_MODE_BASE,
            "anti_bot_guard_mode": ANTI_BOT_GUARD_MODE_ENABLED,
        }
    return {
        "strategy_family": "SINGLE",
        "single_entry_mode": SINGLE_ENTRY_MODE_LEGACY,
        "market_data_mode": MARKET_DATA_MODE_BASE,
        "anti_bot_guard_mode": "",
    }


def _decision(
    route: str,
    allow_trade: bool,
    confidence: float,
    regime: str,
    reason: str,
    codes: list[str],
    source: str,
    now: float,
    error: str | None = None,
) -> LlmRouterDecision:
    return LlmRouterDecision(
        route=route,
        allow_trade=bool(allow_trade and route in LLM_TRADE_ROUTES and confidence >= LLM_MIN_CONFIDENCE_TO_TRADE),
        confidence=round(max(0.0, min(1.0, confidence)), 4),
        market_regime=regime,
        reason=reason,
        reason_codes=tuple(codes),
        source=source,
        valid_until=now + 8.0,
        error=error,
    )


def _clone_decision(decision: LlmRouterDecision, *, source: str, error: str | None = None) -> LlmRouterDecision:
    return LlmRouterDecision(
        route=decision.route,
        allow_trade=decision.allow_trade,
        confidence=decision.confidence,
        market_regime=decision.market_regime,
        reason=decision.reason,
        reason_codes=decision.reason_codes,
        source=source,
        valid_until=decision.valid_until,
        raw_response=decision.raw_response,
        error=error,
    )


def _llm_system_prompt() -> str:
    return (
        "You are a risk-controlled strategy router for BTC 5-minute Polymarket paper trading. "
        "Return JSON only. You must choose exactly one route from: "
        "NO_TRADE, SINGLE_FAK, SINGLE_FAK_STOP_AND_FLIP, SINGLE_FAK_REVERSAL, "
        "SINGLE_FAK_ANTI_BOT_GUARD. "
        "Never invent a new strategy. Prefer NO_TRADE when data is stale, near target, expensive, or uncertain. "
        "Output keys: market_regime, recommended_strategy, allow_trade, confidence, reason_codes, reason, valid_until_ms."
    )


def _llm_user_prompt(features: dict[str, Any]) -> str:
    payload = json.dumps(features, ensure_ascii=False, sort_keys=True)
    if len(payload) > LLM_MAX_PROMPT_CHARS:
        payload = payload[:LLM_MAX_PROMPT_CHARS] + "...TRUNCATED"
    return f"Route this market using the schema from the system prompt.\nFEATURES:\n{payload}"


def _chat_completion_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response missing choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        content = "\n".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response missing message content")
    return content.strip()


def _extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("LLM JSON must be an object")
    return payload


def _quote_features(quote: dict[str, Any]) -> dict[str, Any]:
    bid = _maybe_float(quote.get("best_bid"))
    ask = _maybe_float(quote.get("best_ask"))
    return {
        "bid": bid,
        "ask": ask,
        "spread": round(ask - bid, 6) if bid is not None and ask is not None else None,
        "bid_size": _maybe_float(quote.get("bid_size")),
        "ask_size": _maybe_float(quote.get("ask_size")),
    }


def _compact_multi_context(context: dict[str, Any]) -> dict[str, Any]:
    sources = context.get("sources") if isinstance(context.get("sources"), dict) else {}
    compact: dict[str, Any] = {"ready": bool(context.get("ready")), "sources": {}}
    for name, row in sources.items():
        if not isinstance(row, dict):
            continue
        compact["sources"][str(name)] = {
            "ready": bool(row.get("ready")),
            "age_ms": _maybe_float(row.get("age_ms")),
            "raw_bps": _maybe_float(row.get("raw_bps")),
            "median_bps": _maybe_float(row.get("median_bps")),
            "residual_bps": _maybe_float(row.get("residual_bps")),
            "samples": _maybe_float(row.get("samples")),
        }
    return compact


def _compact_probability(payload: dict[str, Any]) -> dict[str, Any]:
    keys = ("combined_up", "up", "down", "confidence", "status", "updated_at_ms", "checked_at")
    return {key: payload.get(key) for key in keys if key in payload}


def _ready_residuals_for_side(side: str, context: dict[str, Any]) -> list[float]:
    sources = context.get("sources") if isinstance(context.get("sources"), dict) else {}
    rows: list[float] = []
    for row in sources.values():
        if not isinstance(row, dict) or not row.get("ready"):
            continue
        residual = _maybe_float(row.get("residual_bps"))
        if residual is None:
            continue
        rows.append(residual if side == "Up" else -residual)
    return rows


def _max_age_ms(now_ms: int, *updated_values: Any) -> int | None:
    ages = [_age_ms(now_ms, value) for value in updated_values]
    ready = [age for age in ages if age is not None]
    return max(ready) if ready else None


def _age_ms(now_ms: int, updated: Any) -> int | None:
    parsed = _maybe_float(updated)
    if parsed is None or parsed <= 0:
        return None
    return int(max(0.0, now_ms - parsed))


def _first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _maybe_float(payload.get(key))
        if value is not None:
            return value
    return None


def _sum_if_numbers(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(left + right, 6)


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _maybe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .experiments import (
    ANTI_BOT_GUARD_MODE_ENABLED,
    ANTI_BOT_GUARD_MODE_NONE,
    MARKET_DATA_MODE_BASE,
    MARKET_DATA_MODE_MULTI_CONFIRM,
    MARKET_DATA_MODE_MULTI_LEAD,
    PRICE_SOURCE_MODE_CHAINLINK_ONLY,
    PRICE_SOURCE_MODE_FALLBACK_ONLY,
    PRICE_SOURCE_MODE_MIXED,
)
from .models import MarketRound, Signal


MULTI_SOURCE_KEYS = ("okx", "binance")
MULTI_SOURCE_MIN_SAMPLES = 5
MULTI_SOURCE_MAX_AGE_MS = 3_000
MULTI_CONFIRM_OPPOSITE_BPS = 1.5
MULTI_LEAD_CONFIDENCE_DIVISOR = 150.0
MULTI_LEAD_MAX_ADJUSTMENT = 0.04
ANTI_BOT_EXTERNAL_OPPOSITE_BPS = 2.0
ANTI_BOT_RESIDUAL_OPPOSITE_BPS = 1.5
ANTI_BOT_RICH_ENTRY_PRICE = 0.66
ANTI_BOT_WEAK_ANCHOR_BPS = 8.0
ANTI_BOT_NEAR_SETTLE_SECONDS = 75
ANTI_BOT_NEAR_SETTLE_ENTRY_PRICE = 0.62
ANTI_BOT_NEAR_SETTLE_WEAK_ANCHOR_BPS = 12.0
ANTI_BOT_THIN_EXPENSIVE_ASK_SIZE = 2.0
ANTI_BOT_THIN_EXPENSIVE_ENTRY_PRICE = 0.60


@dataclass(frozen=True)
class _SelectedPrice:
    price: float | None
    source: str
    age_ms: int | None
    block_reason: str | None = None


@dataclass(frozen=True)
class RealMarketInput:
    market: MarketRound
    current_price: float | None
    chainlink_price: float | None
    fallback_price: float | None
    binance_price: float | None
    okx_price: float | None
    up_bid: float | None
    up_ask: float | None
    up_ask_size: float | None
    down_bid: float | None
    down_ask: float | None
    down_ask_size: float | None
    quote_age_ms: int | None
    price_age_ms: int | None
    chainlink_age_ms: int | None
    fallback_age_ms: int | None
    now_ts: float | None = None
    multi_context: dict[str, Any] | None = None


class RealBtcFiveMinuteStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def signal(
        self,
        data: RealMarketInput,
        market_data_mode: str = MARKET_DATA_MODE_BASE,
        price_source_mode: str = PRICE_SOURCE_MODE_MIXED,
        anti_bot_guard_mode: str = ANTI_BOT_GUARD_MODE_NONE,
    ) -> Signal:
        now = time.time() if data.now_ts is None else data.now_ts
        market = data.market
        time_left = int(market.ends_at - now)
        target_price = market.target_price
        market_data_mode = _normalize_market_data_mode(market_data_mode)
        price_source_mode = _normalize_price_source_mode(price_source_mode)
        anti_bot_guard_mode = _normalize_anti_bot_guard_mode(anti_bot_guard_mode)

        if target_price <= 0:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "缺少官方目标价 priceToBeat")
        selected_price = _select_current_price(
            data,
            market_data_mode,
            price_source_mode,
            self.settings.max_quote_age_ms,
        )
        if selected_price.block_reason:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, selected_price.block_reason)
        current_price = selected_price.price
        if current_price is None or current_price <= 0:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "缺少 BTC 实时价格")
        if time_left < self.settings.min_time_left_seconds:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "临近结算停止开仓")
        if time_left > self.settings.max_time_left_seconds:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "市场刚开始，等待盘口稳定")
        if data.quote_age_ms is None or data.quote_age_ms > self.settings.max_quote_age_ms:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "盘口报价过期")
        if selected_price.age_ms is None or selected_price.age_ms > self.settings.max_quote_age_ms:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "价格流过期")

        signed_distance_bps = (current_price - target_price) / target_price * 10_000.0
        side = "Up" if signed_distance_bps >= 0 else "Down"
        distance_bps = abs(signed_distance_bps)
        if distance_bps < self.settings.min_price_distance_bps:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, signed_distance_bps, "当前价离目标价太近")

        entry_price = data.up_ask if side == "Up" else data.down_ask
        bid_price = data.up_bid if side == "Up" else data.down_bid
        ask_size = data.up_ask_size if side == "Up" else data.down_ask_size
        if entry_price is None:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, signed_distance_bps, f"缺少 {side} 卖一价")
        if entry_price > self.settings.max_entry_price:
            return Signal("BTC", "NO_TRADE", 0.0, entry_price, signed_distance_bps, "入场价格高于上限")
        if bid_price is not None and entry_price - bid_price > self.settings.max_spread:
            return Signal("BTC", "NO_TRADE", 0.0, entry_price, signed_distance_bps, "盘口价差过大")
        if ask_size is not None and ask_size < self.settings.min_ask_size:
            return Signal("BTC", "NO_TRADE", 0.0, entry_price, signed_distance_bps, "卖盘深度不足")

        confidence = self._estimate_confidence(distance_bps, time_left, side, data, market_data_mode)
        multi_note = ""
        if market_data_mode == MARKET_DATA_MODE_MULTI_CONFIRM:
            confirmation = _multi_confirmation(side, data.multi_context)
            if not confirmation["ready"]:
                return Signal(
                    "BTC",
                    "NO_TRADE",
                    confidence,
                    entry_price,
                    signed_distance_bps,
                    f"MULTI_CONFIRM 等待 OKX/Binance 基差样本: {confirmation['reason']}",
                )
            if confirmation["blocked"]:
                return Signal(
                    "BTC",
                    "NO_TRADE",
                    confidence,
                    entry_price,
                    signed_distance_bps,
                    f"MULTI_CONFIRM 反向残差过滤: {confirmation['reason']}",
                )
            multi_note = f", multi_confirm {confirmation['reason']}"
        elif market_data_mode == MARKET_DATA_MODE_MULTI_LEAD:
            lead = _multi_lead(side, data.multi_context)
            if not lead["ready"]:
                return Signal(
                    "BTC",
                    "NO_TRADE",
                    confidence,
                    entry_price,
                    signed_distance_bps,
                    f"MULTI_LEAD 等待 OKX/Binance 基差样本: {lead['reason']}",
                )
            if abs(float(lead["adjustment"])) > 0:
                multi_note = f", multi_lead {lead['reason']}, adj {float(lead['adjustment']):+.4f}"
        edge = confidence - entry_price
        if confidence < self.settings.min_confidence:
            return Signal("BTC", "NO_TRADE", confidence, entry_price, signed_distance_bps, "置信度未达阈值")
        if edge < self.settings.min_edge:
            return Signal("BTC", "NO_TRADE", confidence, entry_price, signed_distance_bps, "赔率优势不足")
        guard_note = ""
        if anti_bot_guard_mode == ANTI_BOT_GUARD_MODE_ENABLED:
            guard_block = _anti_bot_guard_block_reason(
                side,
                signed_distance_bps,
                distance_bps,
                entry_price,
                ask_size,
                time_left,
                data,
                target_price,
                self.settings.min_ask_size,
                self.settings.max_quote_age_ms,
            )
            if guard_block:
                return Signal("BTC", "NO_TRADE", confidence, entry_price, signed_distance_bps, guard_block)
            guard_note = f", anti_bot_guard {ANTI_BOT_GUARD_MODE_ENABLED}:PASS"

        price_source_note = "" if price_source_mode == PRICE_SOURCE_MODE_MIXED else f", price_source_mode {price_source_mode}"
        reason = (
            f"真实 BTC 5m {side}: {selected_price.source} {current_price:.2f} vs target {target_price:.2f}, "
            f"距离 {signed_distance_bps:.2f}bps, ask {entry_price:.2f}, edge {edge:.3f}"
            f"{multi_note}{price_source_note}{guard_note}"
        )
        return Signal("BTC", side, round(confidence, 4), round(entry_price, 4), signed_distance_bps, reason)

    def _estimate_confidence(
        self,
        distance_bps: float,
        time_left: int,
        side: str,
        data: RealMarketInput,
        market_data_mode: str,
    ) -> float:
        remaining_vol_bps = max(0.5, 12.0 * math.sqrt(max(time_left, 1) / 300.0))
        confidence = _normal_cdf(distance_bps / remaining_vol_bps)
        if market_data_mode == MARKET_DATA_MODE_BASE and data.binance_price and data.chainlink_price:
            lead_bps = (data.binance_price - data.chainlink_price) / data.chainlink_price * 10_000.0
            directional_lead = lead_bps if side == "Up" else -lead_bps
            confidence += max(-0.06, min(0.06, directional_lead / 150.0))
        elif market_data_mode == MARKET_DATA_MODE_MULTI_LEAD:
            lead = _multi_lead(side, data.multi_context)
            if lead["ready"]:
                confidence += float(lead["adjustment"])
        return max(0.01, min(0.99, confidence))


def input_from_snapshot(market: MarketRound, payload: dict[str, Any]) -> RealMarketInput:
    price = payload.get("price") if isinstance(payload.get("price"), dict) else {}
    quotes = payload.get("quotes") if isinstance(payload.get("quotes"), dict) else {}
    up = quotes.get("Up") if isinstance(quotes.get("Up"), dict) else {}
    down = quotes.get("Down") if isinstance(quotes.get("Down"), dict) else {}
    now_ms = int(time.time() * 1000)
    quote_updated_ms = max(_maybe_int(up.get("updated_at_ms")) or 0, _maybe_int(down.get("updated_at_ms")) or 0)
    price_updated_ms = max(
        _maybe_int(price.get("chainlink_updated_ms")) or 0,
        _maybe_int(price.get("binance_updated_ms")) or 0,
        _maybe_int(price.get("binance_market_updated_ms")) or 0,
        _maybe_int(price.get("okx_updated_ms")) or 0,
    )
    chainlink_updated_ms = _maybe_int(price.get("chainlink_updated_ms")) or 0
    fallback, fallback_updated_ms = _fallback_price_and_updated_ms(price)
    current = (
        _maybe_float(price.get("chainlink"))
        or fallback
    )
    return RealMarketInput(
        market=market,
        current_price=current,
        chainlink_price=_maybe_float(price.get("chainlink")),
        fallback_price=fallback,
        binance_price=_maybe_float(price.get("binance")),
        okx_price=_maybe_float(price.get("okx")),
        up_bid=_maybe_float(up.get("best_bid")),
        up_ask=_maybe_float(up.get("best_ask")),
        up_ask_size=_maybe_float(up.get("ask_size")),
        down_bid=_maybe_float(down.get("best_bid")),
        down_ask=_maybe_float(down.get("best_ask")),
        down_ask_size=_maybe_float(down.get("ask_size")),
        quote_age_ms=(now_ms - quote_updated_ms) if quote_updated_ms else None,
        price_age_ms=(now_ms - price_updated_ms) if price_updated_ms else None,
        chainlink_age_ms=(now_ms - chainlink_updated_ms) if chainlink_updated_ms else None,
        fallback_age_ms=(now_ms - fallback_updated_ms) if fallback_updated_ms else None,
        multi_context=price.get("multi_context")
        if isinstance(price.get("multi_context"), dict)
        else multi_source_price_context(price, now_ms),
    )


def multi_source_price_context(price: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    chainlink = _maybe_float(price.get("chainlink"))
    if not chainlink:
        return {"ready": False, "reason": "chainlink_missing", "sources": {}}
    sources: dict[str, dict[str, Any]] = {}
    for source in MULTI_SOURCE_KEYS:
        source_price = _multi_source_price(price, source)
        updated_ms = _multi_source_updated_ms(price, source)
        if not source_price or not updated_ms:
            sources[source] = {"ready": False, "reason": "missing"}
            continue
        age_ms = max(0, now_ms - updated_ms)
        raw_bps = (source_price - chainlink) / chainlink * 10_000.0
        median_bps = _maybe_float(price.get(f"{source}_basis_median_bps"))
        samples = _maybe_int(price.get(f"{source}_basis_samples")) or 0
        ready = age_ms <= MULTI_SOURCE_MAX_AGE_MS and median_bps is not None and samples >= MULTI_SOURCE_MIN_SAMPLES
        sources[source] = {
            "ready": ready,
            "reason": "ready" if ready else "warming_or_stale",
            "price": source_price,
            "age_ms": age_ms,
            "raw_bps": raw_bps,
            "median_bps": median_bps,
            "residual_bps": raw_bps - median_bps if median_bps is not None else None,
            "samples": samples,
        }
    return {"ready": any(row.get("ready") for row in sources.values()), "sources": sources}


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _multi_source_price(price: dict[str, Any], source: str) -> float | None:
    if source == "binance":
        return _maybe_float(price.get("binance_market")) or _maybe_float(price.get("binance"))
    return _maybe_float(price.get(source))


def _multi_source_updated_ms(price: dict[str, Any], source: str) -> int | None:
    if source == "binance":
        return _maybe_int(price.get("binance_market_updated_ms")) or _maybe_int(price.get("binance_updated_ms"))
    return _maybe_int(price.get(f"{source}_updated_ms"))


def _fallback_price_and_updated_ms(price: dict[str, Any]) -> tuple[float | None, int | None]:
    candidates = (
        ("binance", "binance_updated_ms"),
        ("binance_market", "binance_market_updated_ms"),
        ("okx", "okx_updated_ms"),
    )
    for value_key, updated_key in candidates:
        value = _maybe_float(price.get(value_key))
        if value is None:
            continue
        return value, _maybe_int(price.get(updated_key))
    return None, None


def _normalize_market_data_mode(value: str) -> str:
    normalized = str(value or MARKET_DATA_MODE_BASE).strip().upper()
    if normalized in {MARKET_DATA_MODE_MULTI_CONFIRM, MARKET_DATA_MODE_MULTI_LEAD}:
        return normalized
    return MARKET_DATA_MODE_BASE


def _normalize_price_source_mode(value: str) -> str:
    normalized = str(value or PRICE_SOURCE_MODE_MIXED).strip().upper()
    if normalized in {PRICE_SOURCE_MODE_CHAINLINK_ONLY, PRICE_SOURCE_MODE_FALLBACK_ONLY}:
        return normalized
    return PRICE_SOURCE_MODE_MIXED


def _normalize_anti_bot_guard_mode(value: str) -> str:
    normalized = str(value or ANTI_BOT_GUARD_MODE_NONE).strip().upper()
    if normalized == ANTI_BOT_GUARD_MODE_ENABLED:
        return ANTI_BOT_GUARD_MODE_ENABLED
    return ANTI_BOT_GUARD_MODE_NONE


def _select_current_price(
    data: RealMarketInput,
    market_data_mode: str,
    price_source_mode: str,
    max_age_ms: int,
) -> _SelectedPrice:
    if market_data_mode != MARKET_DATA_MODE_BASE and not data.chainlink_price:
        return _SelectedPrice(None, "Chainlink", None, f"{market_data_mode} 缺少 Chainlink 锚定价格")

    chainlink_fresh = (
        data.chainlink_price is not None
        and data.chainlink_price > 0
        and data.chainlink_age_ms is not None
        and data.chainlink_age_ms <= max_age_ms
    )

    if price_source_mode == PRICE_SOURCE_MODE_CHAINLINK_ONLY:
        if data.chainlink_price is None or data.chainlink_price <= 0:
            return _SelectedPrice(None, "Chainlink", None, "CHAINLINK_ONLY 缺少 Chainlink，禁止 fallback 开仓")
        if data.chainlink_age_ms is None or data.chainlink_age_ms > max_age_ms:
            return _SelectedPrice(
                None,
                "Chainlink",
                data.chainlink_age_ms,
                "CHAINLINK_ONLY Chainlink 价格过期",
            )
        return _SelectedPrice(data.chainlink_price, "Chainlink", data.chainlink_age_ms)

    if price_source_mode == PRICE_SOURCE_MODE_FALLBACK_ONLY:
        if chainlink_fresh:
            return _SelectedPrice(
                None,
                "fallback",
                None,
                "FALLBACK_ONLY 当前有新鲜 Chainlink，不采 fallback",
            )
        if data.fallback_price is None or data.fallback_price <= 0:
            return _SelectedPrice(None, "fallback", None, "FALLBACK_ONLY 缺少 fallback 价格")
        if data.fallback_age_ms is None or data.fallback_age_ms > max_age_ms:
            return _SelectedPrice(
                None,
                "fallback",
                data.fallback_age_ms,
                "FALLBACK_ONLY fallback 价格过期",
            )
        return _SelectedPrice(data.fallback_price, "fallback", data.fallback_age_ms)

    if data.chainlink_price is not None and data.chainlink_price > 0:
        return _SelectedPrice(data.chainlink_price, "Chainlink", data.price_age_ms)
    return _SelectedPrice(data.current_price, "fallback", data.price_age_ms)


def _anti_bot_guard_block_reason(
    side: str,
    signed_distance_bps: float,
    distance_bps: float,
    entry_price: float,
    ask_size: float | None,
    time_left: int,
    data: RealMarketInput,
    target_price: float,
    min_ask_size: float,
    max_age_ms: int,
) -> str | None:
    residual_block = _anti_bot_residual_block_reason(side, data.multi_context)
    if residual_block:
        return f"ANTI_BOT_GUARD external_residual_disagree: {residual_block}"

    external_block = _anti_bot_external_block_reason(side, data.multi_context, target_price, max_age_ms)
    if external_block:
        return f"ANTI_BOT_GUARD external_price_disagree: {external_block}"

    if entry_price >= ANTI_BOT_RICH_ENTRY_PRICE and distance_bps < ANTI_BOT_WEAK_ANCHOR_BPS:
        return (
            f"ANTI_BOT_GUARD rich_contract_weak_anchor: ask {entry_price:.4f}, "
            f"anchor {signed_distance_bps:+.2f}bps"
        )

    if (
        time_left <= ANTI_BOT_NEAR_SETTLE_SECONDS
        and entry_price >= ANTI_BOT_NEAR_SETTLE_ENTRY_PRICE
        and distance_bps < ANTI_BOT_NEAR_SETTLE_WEAK_ANCHOR_BPS
    ):
        return (
            f"ANTI_BOT_GUARD near_settlement_rich_weak_anchor: left {time_left}s, "
            f"ask {entry_price:.4f}, anchor {signed_distance_bps:+.2f}bps"
        )

    thin_limit = max(ANTI_BOT_THIN_EXPENSIVE_ASK_SIZE, min_ask_size * 2.0)
    if ask_size is not None and ask_size <= thin_limit and entry_price >= ANTI_BOT_THIN_EXPENSIVE_ENTRY_PRICE:
        return (
            f"ANTI_BOT_GUARD thin_expensive_top_ask: ask_size {ask_size:.4f}, "
            f"ask {entry_price:.4f}, anchor {signed_distance_bps:+.2f}bps"
        )
    return None


def _anti_bot_residual_block_reason(side: str, context: dict[str, Any] | None) -> str | None:
    rows = _ready_multi_rows(context)
    blocked: list[tuple[str, float]] = []
    for name, row in rows:
        directional = _directional_residual_bps(side, row)
        if directional < -ANTI_BOT_RESIDUAL_OPPOSITE_BPS:
            blocked.append((name, directional))
    if not blocked:
        return None
    return ", ".join(f"{name}:{value:+.2f}bps" for name, value in blocked)


def _anti_bot_external_block_reason(
    side: str,
    context: dict[str, Any] | None,
    target_price: float,
    max_age_ms: int,
) -> str | None:
    if target_price <= 0 or not isinstance(context, dict):
        return None
    sources = context.get("sources") if isinstance(context.get("sources"), dict) else {}
    blocked: list[tuple[str, float]] = []
    for name in MULTI_SOURCE_KEYS:
        row = sources.get(name)
        if not isinstance(row, dict):
            continue
        source_price = _maybe_float(row.get("price"))
        source_age_ms = _maybe_int(row.get("age_ms"))
        if source_price is None or source_age_ms is None or source_age_ms > max_age_ms:
            continue
        source_distance_bps = (source_price - target_price) / target_price * 10_000.0
        directional = source_distance_bps if side == "Up" else -source_distance_bps
        if directional < -ANTI_BOT_EXTERNAL_OPPOSITE_BPS:
            blocked.append((name, directional))
    if not blocked:
        return None
    return ", ".join(f"{name}:{value:+.2f}bps" for name, value in blocked)


def _multi_confirmation(side: str, context: dict[str, Any] | None) -> dict[str, Any]:
    rows = _ready_multi_rows(context)
    if not rows:
        return {"ready": False, "blocked": False, "reason": "ready source < 1"}
    directional = [(name, _directional_residual_bps(side, row)) for name, row in rows]
    blocked = [(name, value) for name, value in directional if value < -MULTI_CONFIRM_OPPOSITE_BPS]
    text = ", ".join(f"{name}:{value:+.2f}bps" for name, value in directional)
    if blocked:
        return {"ready": True, "blocked": True, "reason": text}
    return {"ready": True, "blocked": False, "reason": text}


def _multi_lead(side: str, context: dict[str, Any] | None) -> dict[str, Any]:
    rows = _ready_multi_rows(context)
    if not rows:
        return {"ready": False, "adjustment": 0.0, "reason": "ready source < 1"}
    directional = [_directional_residual_bps(side, row) for _, row in rows]
    avg_bps = sum(directional) / len(directional)
    adjustment = max(
        -MULTI_LEAD_MAX_ADJUSTMENT,
        min(MULTI_LEAD_MAX_ADJUSTMENT, avg_bps / MULTI_LEAD_CONFIDENCE_DIVISOR),
    )
    text = ", ".join(
        f"{name}:{_directional_residual_bps(side, row):+.2f}bps"
        for name, row in rows
    )
    return {"ready": True, "adjustment": adjustment, "reason": f"{text}, avg {avg_bps:+.2f}bps"}


def _ready_multi_rows(context: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(context, dict):
        return []
    sources = context.get("sources") if isinstance(context.get("sources"), dict) else {}
    rows: list[tuple[str, dict[str, Any]]] = []
    for name in MULTI_SOURCE_KEYS:
        row = sources.get(name)
        if not isinstance(row, dict) or not row.get("ready"):
            continue
        residual = _maybe_float(row.get("residual_bps"))
        if residual is None:
            continue
        rows.append((name, row))
    return rows


def _directional_residual_bps(side: str, row: dict[str, Any]) -> float:
    residual = _maybe_float(row.get("residual_bps")) or 0.0
    return residual if side == "Up" else -residual


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _maybe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

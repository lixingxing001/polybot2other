from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import MarketRound, Signal


@dataclass(frozen=True)
class RealMarketInput:
    market: MarketRound
    current_price: float | None
    chainlink_price: float | None
    binance_price: float | None
    up_bid: float | None
    up_ask: float | None
    up_ask_size: float | None
    down_bid: float | None
    down_ask: float | None
    down_ask_size: float | None
    quote_age_ms: int | None
    price_age_ms: int | None
    now_ts: float | None = None


class RealBtcFiveMinuteStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def signal(self, data: RealMarketInput) -> Signal:
        now = time.time() if data.now_ts is None else data.now_ts
        market = data.market
        time_left = int(market.ends_at - now)
        target_price = market.target_price
        current_price = data.chainlink_price or data.current_price

        if target_price <= 0:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "缺少官方目标价 priceToBeat")
        if current_price is None or current_price <= 0:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "缺少 BTC 实时价格")
        if time_left < self.settings.min_time_left_seconds:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "临近结算停止开仓")
        if time_left > self.settings.max_time_left_seconds:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "市场刚开始，等待盘口稳定")
        if data.quote_age_ms is None or data.quote_age_ms > self.settings.max_quote_age_ms:
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, "盘口报价过期")
        if data.price_age_ms is None or data.price_age_ms > self.settings.max_quote_age_ms:
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

        confidence = self._estimate_confidence(distance_bps, time_left, side, data)
        edge = confidence - entry_price
        if confidence < self.settings.min_confidence:
            return Signal("BTC", "NO_TRADE", confidence, entry_price, signed_distance_bps, "置信度未达阈值")
        if edge < self.settings.min_edge:
            return Signal("BTC", "NO_TRADE", confidence, entry_price, signed_distance_bps, "赔率优势不足")

        price_source = "Chainlink" if data.chainlink_price else "fallback"
        reason = (
            f"真实 BTC 5m {side}: {price_source} {current_price:.2f} vs target {target_price:.2f}, "
            f"距离 {signed_distance_bps:.2f}bps, ask {entry_price:.2f}, edge {edge:.3f}"
        )
        return Signal("BTC", side, round(confidence, 4), round(entry_price, 4), signed_distance_bps, reason)

    def _estimate_confidence(self, distance_bps: float, time_left: int, side: str, data: RealMarketInput) -> float:
        remaining_vol_bps = max(0.5, 12.0 * math.sqrt(max(time_left, 1) / 300.0))
        confidence = _normal_cdf(distance_bps / remaining_vol_bps)
        if data.binance_price and data.chainlink_price:
            lead_bps = (data.binance_price - data.chainlink_price) / data.chainlink_price * 10_000.0
            directional_lead = lead_bps if side == "Up" else -lead_bps
            confidence += max(-0.06, min(0.06, directional_lead / 150.0))
        return max(0.01, min(0.99, confidence))


def input_from_snapshot(market: MarketRound, payload: dict[str, Any]) -> RealMarketInput:
    price = payload.get("price") if isinstance(payload.get("price"), dict) else {}
    quotes = payload.get("quotes") if isinstance(payload.get("quotes"), dict) else {}
    up = quotes.get("Up") if isinstance(quotes.get("Up"), dict) else {}
    down = quotes.get("Down") if isinstance(quotes.get("Down"), dict) else {}
    now_ms = int(time.time() * 1000)
    quote_updated_ms = max(_maybe_int(up.get("updated_at_ms")) or 0, _maybe_int(down.get("updated_at_ms")) or 0)
    price_updated_ms = max(_maybe_int(price.get("chainlink_updated_ms")) or 0, _maybe_int(price.get("binance_updated_ms")) or 0)
    current = _maybe_float(price.get("chainlink")) or _maybe_float(price.get("binance"))
    return RealMarketInput(
        market=market,
        current_price=current,
        chainlink_price=_maybe_float(price.get("chainlink")),
        binance_price=_maybe_float(price.get("binance")),
        up_bid=_maybe_float(up.get("best_bid")),
        up_ask=_maybe_float(up.get("best_ask")),
        up_ask_size=_maybe_float(up.get("ask_size")),
        down_bid=_maybe_float(down.get("best_bid")),
        down_ask=_maybe_float(down.get("best_ask")),
        down_ask_size=_maybe_float(down.get("ask_size")),
        quote_age_ms=(now_ms - quote_updated_ms) if quote_updated_ms else None,
        price_age_ms=(now_ms - price_updated_ms) if price_updated_ms else None,
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


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

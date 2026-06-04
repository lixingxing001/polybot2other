from __future__ import annotations

import time
from typing import Any

from .models import MarketRound, Signal
from .strategy import MULTI_SOURCE_KEYS


SINGLE_AGGRESSIVE_EDGE_MARKER = "SINGLE_AGGRESSIVE_EDGE"
AGGRESSIVE_EDGE_LOW_ENTRY_MAX = 0.50
AGGRESSIVE_EDGE_MID_ENTRY_MIN = 0.50
AGGRESSIVE_EDGE_MID_ENTRY_MAX = 0.70
AGGRESSIVE_EDGE_HIGH_ENTRY_MIN = 0.70
AGGRESSIVE_EDGE_LOW_MIN_CONFIDENCE = 0.65
AGGRESSIVE_EDGE_LOW_MIN_EDGE = 0.12
AGGRESSIVE_EDGE_SWEET_MIN_BPS = 6.0
AGGRESSIVE_EDGE_SWEET_MAX_BPS = 8.0
AGGRESSIVE_EDGE_SWEET_MIN_CONFIDENCE = 0.70
AGGRESSIVE_EDGE_SWEET_MIN_EDGE = 0.04
AGGRESSIVE_EDGE_HIGH_MIN_CONFIDENCE = 0.75
AGGRESSIVE_EDGE_HIGH_MIN_EDGE = 0.02
AGGRESSIVE_EDGE_EXTERNAL_OPPOSITE_BPS = 2.0
AGGRESSIVE_EDGE_FALSE_BREAKOUT_ENTRY_MIN = 0.60
AGGRESSIVE_EDGE_FALSE_BREAKOUT_MIN_TIME_LEFT_SECONDS = 210.0
AGGRESSIVE_EDGE_FALSE_BREAKOUT_BEFORE60_MAX_BPS = 1.0
AGGRESSIVE_EDGE_FALSE_BREAKOUT_MIN_JUMP_60S_BPS = 6.0
AGGRESSIVE_EDGE_FALSE_BREAKOUT_MAX_BPS = 8.2
AGGRESSIVE_EDGE_PAPER_V2_HIGH_MIN_EDGE = 0.10
AGGRESSIVE_EDGE_PAPER_V2_HIGH_MAX_BPS = 12.0


def aggressive_edge_block_reason(
    market: MarketRound,
    signal: Signal,
    price: dict[str, Any],
    max_age_ms: int,
) -> str | None:
    """Aggressive Edge 入场过滤；Paper 和 REAL 共用同一份阈值。"""

    entry_price = _maybe_float(signal.entry_price)
    confidence = _maybe_float(signal.confidence)
    move_bps = _maybe_float(signal.move_bps)
    if entry_price is None or confidence is None or move_bps is None:
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 信号字段缺失，跳过"
    abs_bps = abs(move_bps)
    edge = confidence - entry_price
    external_block = _aggressive_edge_external_block_reason(market, signal.side, price, max_age_ms)
    if external_block:
        return external_block

    low_entry = (
        entry_price < AGGRESSIVE_EDGE_LOW_ENTRY_MAX
        and confidence >= AGGRESSIVE_EDGE_LOW_MIN_CONFIDENCE
        and edge >= AGGRESSIVE_EDGE_LOW_MIN_EDGE
        and abs_bps >= 2.0
    )
    sweet_move = (
        AGGRESSIVE_EDGE_SWEET_MIN_BPS <= abs_bps <= AGGRESSIVE_EDGE_SWEET_MAX_BPS
        and confidence >= AGGRESSIVE_EDGE_SWEET_MIN_CONFIDENCE
        and edge >= AGGRESSIVE_EDGE_SWEET_MIN_EDGE
    )
    high_confidence_high_entry = (
        entry_price >= AGGRESSIVE_EDGE_HIGH_ENTRY_MIN
        and confidence >= AGGRESSIVE_EDGE_HIGH_MIN_CONFIDENCE
        and edge >= AGGRESSIVE_EDGE_HIGH_MIN_EDGE
    )
    if low_entry or sweet_move or high_confidence_high_entry:
        return None

    if AGGRESSIVE_EDGE_MID_ENTRY_MIN <= entry_price < AGGRESSIVE_EDGE_MID_ENTRY_MAX:
        return (
            f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 过滤历史亏损价格带: "
            f"entry {entry_price:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}"
        )
    return (
        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 未命中激进入场样本区: "
        f"entry {entry_price:.4f}, confidence {confidence:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}"
    )


def aggressive_edge_pass_note(signal: Signal) -> str:
    entry_price = _maybe_float(signal.entry_price) or 0.0
    confidence = _maybe_float(signal.confidence) or 0.0
    abs_bps = abs(_maybe_float(signal.move_bps) or 0.0)
    edge = confidence - entry_price
    if entry_price < AGGRESSIVE_EDGE_LOW_ENTRY_MAX:
        branch = "low_entry_high_edge"
    elif AGGRESSIVE_EDGE_SWEET_MIN_BPS <= abs_bps <= AGGRESSIVE_EDGE_SWEET_MAX_BPS:
        branch = "sweet_move_6_8bps"
    else:
        branch = "high_confidence_high_entry"
    return (
        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} PASS {branch}: "
        f"entry {entry_price:.4f}, confidence {confidence:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}"
    )


def aggressive_edge_false_breakout_block_reason(
    market: MarketRound,
    signal: Signal,
    *,
    signal_at: float,
    before60_tick: dict[str, Any] | None,
) -> str | None:
    """根据输单记忆过滤 sweet Up 早段假突破，只给 Paper 调用。"""

    if signal.side != "Up" or before60_tick is None or market.target_price <= 0:
        return None
    entry_price = _maybe_float(signal.entry_price)
    confidence = _maybe_float(signal.confidence)
    move_bps = _maybe_float(signal.move_bps)
    before60_price = _maybe_float(before60_tick.get("price"))
    if entry_price is None or confidence is None or move_bps is None or before60_price is None:
        return None
    abs_bps = abs(move_bps)
    before60_bps = (before60_price - market.target_price) / market.target_price * 10_000.0
    jump_60s = move_bps - before60_bps
    time_left = float(market.ends_at or 0.0) - float(signal_at)
    if not (
        AGGRESSIVE_EDGE_SWEET_MIN_BPS <= abs_bps <= AGGRESSIVE_EDGE_FALSE_BREAKOUT_MAX_BPS
        and entry_price >= AGGRESSIVE_EDGE_FALSE_BREAKOUT_ENTRY_MIN
        and time_left >= AGGRESSIVE_EDGE_FALSE_BREAKOUT_MIN_TIME_LEFT_SECONDS
        and before60_bps <= AGGRESSIVE_EDGE_FALSE_BREAKOUT_BEFORE60_MAX_BPS
        and jump_60s >= AGGRESSIVE_EDGE_FALSE_BREAKOUT_MIN_JUMP_60S_BPS
    ):
        return None
    tick_age = float(signal_at) - (_maybe_float(before60_tick.get("created_at")) or signal_at)
    return (
        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 学习过滤早段假突破风险: "
        f"branch sweet_move_6_8bps, entry {entry_price:.4f}, confidence {confidence:.4f}, "
        f"move_bps {move_bps:.2f}, before60_bps {before60_bps:.2f}, "
        f"jump60 {jump_60s:.2f}bps, time_left {time_left:.1f}s, tick_age {tick_age:.1f}s"
    )


def aggressive_edge_paper_v2_block_reason(signal: Signal) -> str | None:
    """Paper 二代学习过滤，基于昨晚输单复盘收紧负期望分支。"""

    entry_price = _maybe_float(signal.entry_price)
    confidence = _maybe_float(signal.confidence)
    move_bps = _maybe_float(signal.move_bps)
    if entry_price is None or confidence is None or move_bps is None:
        return None
    abs_bps = abs(move_bps)
    edge = confidence - entry_price
    if (
        signal.side == "Up"
        and entry_price >= AGGRESSIVE_EDGE_LOW_ENTRY_MAX
        and AGGRESSIVE_EDGE_SWEET_MIN_BPS <= abs_bps <= AGGRESSIVE_EDGE_SWEET_MAX_BPS
    ):
        return (
            f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 学习过滤V2 sweet Up负期望分支: "
            f"entry {entry_price:.4f}, confidence {confidence:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}"
        )
    if entry_price >= AGGRESSIVE_EDGE_HIGH_ENTRY_MIN and (
        edge < AGGRESSIVE_EDGE_PAPER_V2_HIGH_MIN_EDGE
        or abs_bps >= AGGRESSIVE_EDGE_PAPER_V2_HIGH_MAX_BPS
    ):
        return (
            f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 学习过滤V2 高价安全边际不足: "
            f"entry {entry_price:.4f}, confidence {confidence:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}, "
            f"要求 edge>={AGGRESSIVE_EDGE_PAPER_V2_HIGH_MIN_EDGE:.2f} 且 abs_bps<{AGGRESSIVE_EDGE_PAPER_V2_HIGH_MAX_BPS:.2f}"
        )
    return None


def _aggressive_edge_external_block_reason(
    market: MarketRound,
    side: str,
    price: dict[str, Any],
    max_age_ms: int,
) -> str | None:
    if market.target_price <= 0:
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 缺少目标价，跳过"
    now_ms = int(time.time() * 1000)
    opposing: list[str] = []
    checked = 0
    for source in MULTI_SOURCE_KEYS:
        source_price = _multi_source_price_for_basis(price, source)
        updated_ms = _multi_source_updated_ms_for_basis(price, source)
        if source_price is None or updated_ms is None:
            continue
        age_ms = max(0, now_ms - updated_ms)
        if age_ms > max_age_ms:
            continue
        checked += 1
        distance_bps = (source_price - market.target_price) / market.target_price * 10_000.0
        directional_bps = distance_bps if side == "Up" else -distance_bps
        if directional_bps < -AGGRESSIVE_EDGE_EXTERNAL_OPPOSITE_BPS:
            opposing.append(f"{source}:{directional_bps:+.2f}bps")
    if opposing:
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 外部价格反向: {', '.join(opposing)}"
    if checked == 0 and not _aggressive_edge_chainlink_fresh(price, max_age_ms):
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 缺少新鲜 Chainlink 或外部确认价格，跳过"
    return None


def _aggressive_edge_chainlink_fresh(price: dict[str, Any], max_age_ms: int) -> bool:
    chainlink = _maybe_float(price.get("chainlink"))
    updated_ms = _maybe_int(price.get("chainlink_updated_ms"))
    if chainlink is None or chainlink <= 0 or updated_ms is None:
        return False
    return max(0, int(time.time() * 1000) - updated_ms) <= max_age_ms


def _multi_source_price_for_basis(price: dict[str, Any], source: str) -> float | None:
    if source == "binance":
        return _maybe_float(price.get("binance_market")) or _maybe_float(price.get("binance"))
    return _maybe_float(price.get(source))


def _multi_source_updated_ms_for_basis(price: dict[str, Any], source: str) -> int | None:
    if source == "binance":
        return _maybe_int(price.get("binance_market_updated_ms")) or _maybe_int(price.get("binance_updated_ms"))
    return _maybe_int(price.get(f"{source}_updated_ms"))


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

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
AGGRESSIVE_EDGE_ACTIVE_DOWN_SWEET_MIN_BPS = 4.5
AGGRESSIVE_EDGE_ACTIVE_DOWN_SWEET_MIN_EDGE = 0.025
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
    *,
    sweet_min_bps: float = AGGRESSIVE_EDGE_SWEET_MIN_BPS,
    sweet_min_edge: float = AGGRESSIVE_EDGE_SWEET_MIN_EDGE,
    profile_label: str = "",
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
        sweet_min_bps <= abs_bps <= AGGRESSIVE_EDGE_SWEET_MAX_BPS
        and confidence >= AGGRESSIVE_EDGE_SWEET_MIN_CONFIDENCE
        and edge >= sweet_min_edge
    )
    high_confidence_high_entry = (
        entry_price >= AGGRESSIVE_EDGE_HIGH_ENTRY_MIN
        and confidence >= AGGRESSIVE_EDGE_HIGH_MIN_CONFIDENCE
        and edge >= AGGRESSIVE_EDGE_HIGH_MIN_EDGE
    )
    if low_entry or sweet_move or high_confidence_high_entry:
        return None

    if AGGRESSIVE_EDGE_MID_ENTRY_MIN <= entry_price < AGGRESSIVE_EDGE_MID_ENTRY_MAX:
        if profile_label:
            return (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} {profile_label} 过滤历史亏损价格带: "
                f"entry {entry_price:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}, "
                f"sweet_min {sweet_min_bps:.2f}, sweet_edge {sweet_min_edge:.3f}"
            )
        return (
            f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 过滤历史亏损价格带: "
            f"entry {entry_price:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}"
        )
    if profile_label:
        return (
            f"{SINGLE_AGGRESSIVE_EDGE_MARKER} {profile_label} 未命中激进入场样本区: "
            f"entry {entry_price:.4f}, confidence {confidence:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}, "
            f"sweet_min {sweet_min_bps:.2f}, sweet_edge {sweet_min_edge:.3f}"
        )
    return (
        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} 未命中激进入场样本区: "
        f"entry {entry_price:.4f}, confidence {confidence:.4f}, edge {edge:.4f}, abs_bps {abs_bps:.2f}"
    )


def aggressive_edge_pass_note(
    signal: Signal,
    *,
    sweet_min_bps: float = AGGRESSIVE_EDGE_SWEET_MIN_BPS,
    profile_label: str = "",
) -> str:
    entry_price = _maybe_float(signal.entry_price) or 0.0
    confidence = _maybe_float(signal.confidence) or 0.0
    abs_bps = abs(_maybe_float(signal.move_bps) or 0.0)
    edge = confidence - entry_price
    if entry_price < AGGRESSIVE_EDGE_LOW_ENTRY_MAX:
        branch = "low_entry_high_edge"
    elif sweet_min_bps <= abs_bps <= AGGRESSIVE_EDGE_SWEET_MAX_BPS:
        branch = f"sweet_move_{sweet_min_bps:g}_8bps"
    else:
        branch = "high_confidence_high_entry"
    profile_note = f" {profile_label}" if profile_label else ""
    return (
        f"{SINGLE_AGGRESSIVE_EDGE_MARKER}{profile_note} PASS {branch}: "
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


def aggressive_edge_v2_risk_report(
    market: MarketRound,
    signal: Signal,
    *,
    price: dict[str, Any],
    quote: dict[str, Any],
    signal_at: float,
    before60_tick: dict[str, Any] | None,
    before30_tick: dict[str, Any] | None = None,
    max_age_ms: int = 60_000,
) -> dict[str, Any] | None:
    """计算 V2 影子模式的结构化反转风险报告。

    V2 只做影子评分，不直接拦截交易；字段会进入 reason 和输局复盘，方便后续按胜负样本统计。
    """

    entry_price = _maybe_float(signal.entry_price)
    confidence = _maybe_float(signal.confidence)
    move_bps = _maybe_float(signal.move_bps)
    if entry_price is None or confidence is None or move_bps is None:
        return None

    bid_size = _maybe_float(quote.get("bid_size")) or 0.0
    ask_size = _maybe_float(quote.get("ask_size")) or 0.0
    best_bid = _maybe_float(quote.get("best_bid"))
    best_ask = _maybe_float(quote.get("best_ask"))
    spread = round(best_ask - best_bid, 4) if best_bid is not None and best_ask is not None else None

    total_size = bid_size + ask_size
    top_level_skew = _ratio_or_default(bid_size, total_size, 0.5)
    bid_levels = _levels_from_quote(quote.get("bids"), reverse=True)
    ask_levels = _levels_from_quote(quote.get("asks"), reverse=False)
    bid_depth_shares = sum(level["size"] for level in bid_levels)
    ask_depth_shares = sum(level["size"] for level in ask_levels)
    bid_depth_notional = sum(level["price"] * level["size"] for level in bid_levels)
    ask_depth_notional = sum(level["price"] * level["size"] for level in ask_levels)
    depth_total = bid_depth_shares + ask_depth_shares
    depth_skew = _ratio_or_default(bid_depth_shares, depth_total, top_level_skew)

    before60_price = _maybe_float(before60_tick.get("price")) if before60_tick else None
    before30_price = _maybe_float(before30_tick.get("price")) if before30_tick else None
    before60_bps = _distance_bps(before60_price, market.target_price)
    before30_bps = _distance_bps(before30_price, market.target_price)
    momentum_60_to_30_bps = (
        round(before30_bps - before60_bps, 4) if before60_bps is not None and before30_bps is not None else None
    )
    momentum_30_to_now_bps = round(move_bps - before30_bps, 4) if before30_bps is not None else None
    momentum_decay_bps = (
        round(momentum_60_to_30_bps - momentum_30_to_now_bps, 4)
        if momentum_60_to_30_bps is not None and momentum_30_to_now_bps is not None
        else None
    )

    external_divergences: list[dict[str, Any]] = []
    external_divergence_count = 0
    now_ms = int(time.time() * 1000)
    for source in MULTI_SOURCE_KEYS:
        source_price = _multi_source_price_for_basis(price, source)
        updated_ms = _multi_source_updated_ms_for_basis(price, source)
        if source_price is None or updated_ms is None or market.target_price <= 0:
            continue
        age_ms = max(0, now_ms - updated_ms)
        if age_ms > max_age_ms:
            continue
        distance_bps = (source_price - market.target_price) / market.target_price * 10_000.0
        directional_bps = distance_bps if signal.side == "Up" else -distance_bps
        if directional_bps < -AGGRESSIVE_EDGE_EXTERNAL_OPPOSITE_BPS:
            external_divergence_count += 1
            external_divergences.append(
                {
                    "source": source,
                    "directional_bps": round(directional_bps, 4),
                    "age_ms": age_ms,
                }
            )

    depth_risk = _clamp01((0.5 - depth_skew) / 0.5)
    top_level_risk = _clamp01((0.5 - top_level_skew) / 0.5)
    spread_risk = _clamp01(((spread or 0.0) - 0.02) / 0.08)
    momentum_risk = _momentum_decay_risk(momentum_decay_bps, momentum_30_to_now_bps)
    external_risk = _clamp01(external_divergence_count / 2.0)
    entry_fragility = _clamp01((entry_price - 0.60) / 0.30)
    risk_score = round(
        _clamp01(
            depth_risk * 0.24
            + top_level_risk * 0.12
            + spread_risk * 0.14
            + momentum_risk * 0.26
            + external_risk * 0.18
            + entry_fragility * 0.06
        ),
        4,
    )
    if risk_score >= 0.65:
        risk_level = "HIGH"
    elif risk_score >= 0.35:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"
    risk_reasons: list[str] = []
    if depth_risk >= 0.35:
        risk_reasons.append("depth_weak")
    if spread_risk >= 0.35:
        risk_reasons.append("spread_wide")
    if momentum_risk >= 0.35:
        risk_reasons.append("momentum_decay")
    if external_divergence_count:
        risk_reasons.append("external_divergence")
    if entry_fragility >= 0.5:
        risk_reasons.append("entry_fragile")

    return {
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "features": {
            "entry_price": round(entry_price, 4),
            "confidence": round(confidence, 4),
            "edge": round(confidence - entry_price, 4),
            "move_bps": round(move_bps, 4),
            "best_bid": round(best_bid, 4) if best_bid is not None else None,
            "best_ask": round(best_ask, 4) if best_ask is not None else None,
            "spread": spread,
            "top_bid_size": round(bid_size, 6),
            "top_ask_size": round(ask_size, 6),
            "top_level_skew": round(top_level_skew, 4),
            "bid_depth_shares": round(bid_depth_shares, 6),
            "ask_depth_shares": round(ask_depth_shares, 6),
            "bid_depth_notional": round(bid_depth_notional, 6),
            "ask_depth_notional": round(ask_depth_notional, 6),
            "depth_skew": round(depth_skew, 4),
            "before60_bps": round(before60_bps, 4) if before60_bps is not None else None,
            "before30_bps": round(before30_bps, 4) if before30_bps is not None else None,
            "momentum_60_to_30_bps": momentum_60_to_30_bps,
            "momentum_30_to_now_bps": momentum_30_to_now_bps,
            "momentum_decay_bps": momentum_decay_bps,
            "external_divergence_count": external_divergence_count,
            "entry_fragility": round(entry_fragility, 4),
        },
        "components": {
            "depth_risk": round(depth_risk, 4),
            "top_level_risk": round(top_level_risk, 4),
            "spread_risk": round(spread_risk, 4),
            "momentum_risk": round(momentum_risk, 4),
            "external_risk": round(external_risk, 4),
            "entry_fragility": round(entry_fragility, 4),
        },
        "external_divergences": external_divergences,
    }


def aggressive_edge_v2_risk_note(report: dict[str, Any] | None) -> str | None:
    """把 V2 结构化报告压缩为交易 reason，便于列表和输局复盘直接查看。"""

    if not report:
        return None
    features = report.get("features") if isinstance(report.get("features"), dict) else {}
    components = report.get("components") if isinstance(report.get("components"), dict) else {}
    return (
        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V2_SHADOW "
        f"risk={_format_optional(report.get('risk_score'), 4)} "
        f"level={report.get('risk_level') or 'UNKNOWN'} "
        f"depth_skew={_format_optional(features.get('depth_skew'), 4)} "
        f"top_skew={_format_optional(features.get('top_level_skew'), 4)} "
        f"spread={_format_optional(features.get('spread'), 4)} "
        f"momentum_decay={_format_optional(features.get('momentum_decay_bps'), 4)} "
        f"ext_div={features.get('external_divergence_count') if features.get('external_divergence_count') is not None else '-'} "
        f"components=depth:{_format_optional(components.get('depth_risk'), 3)},"
        f"spread:{_format_optional(components.get('spread_risk'), 3)},"
        f"momentum:{_format_optional(components.get('momentum_risk'), 3)}"
    )


def aggressive_edge_v2_risk_score(
    market: MarketRound,
    signal: Signal,
    *,
    price: dict[str, Any],
    quote: dict[str, Any],
    signal_at: float,
    before60_tick: dict[str, Any] | None,
    before30_tick: dict[str, Any] | None = None,
) -> str | None:
    """兼容旧测试的 V2 reason 文本入口；新逻辑优先使用结构化 report。"""

    return aggressive_edge_v2_risk_note(
        aggressive_edge_v2_risk_report(
            market,
            signal,
            price=price,
            quote=quote,
            signal_at=signal_at,
            before60_tick=before60_tick,
            before30_tick=before30_tick,
        )
    )


def _levels_from_quote(value: Any, *, reverse: bool) -> list[dict[str, float]]:
    if not isinstance(value, list):
        return []
    levels: list[dict[str, float]] = []
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        price = _maybe_float(item.get("price"))
        size = _maybe_float(item.get("size"))
        if price is None or size is None or size <= 0:
            continue
        levels.append({"price": price, "size": size})
    levels.sort(key=lambda row: row["price"], reverse=reverse)
    return levels


def _distance_bps(price: float | None, target: float | None) -> float | None:
    if price is None or target is None or target <= 0:
        return None
    return (price - target) / target * 10_000.0


def _ratio_or_default(numerator: float, denominator: float, default: float) -> float:
    if denominator <= 0:
        return default
    return max(0.0, min(1.0, numerator / denominator))


def _momentum_decay_risk(momentum_decay_bps: float | None, momentum_30_to_now_bps: float | None) -> float:
    if momentum_decay_bps is None:
        return 0.0
    risk = _clamp01(momentum_decay_bps / 8.0)
    if momentum_30_to_now_bps is not None and momentum_30_to_now_bps < 0:
        risk = max(risk, _clamp01(abs(momentum_30_to_now_bps) / 4.0))
    return risk


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _format_optional(value: Any, digits: int) -> str:
    number = _maybe_float(value)
    if number is None:
        return "-"
    return f"{number:.{digits}f}"

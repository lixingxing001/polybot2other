from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import PaperFill, PaperFillLevel, TradeIntent


ORDER_TYPE_FAK = "FAK"
ORDER_TYPE_GTC = "GTC"
ORDER_TYPE_GTD = "GTD"
ORDER_TYPE_POST_ONLY = "POST_ONLY"
STATUS_FILLED = "FILLED"
STATUS_PARTIAL = "PARTIAL"
STATUS_PENDING = "PENDING"
STATUS_CANCELED = "CANCELED"
STATUS_EXPIRED = "EXPIRED"
STATUS_PARTIAL_RESTING = "PARTIAL_RESTING"
STATUS_RESTING = "RESTING"
STATUS_REJECTED = "REJECTED"
CRYPTO_TAKER_FEE_RATE = 0.07
EPSILON = 0.000001


@dataclass(frozen=True)
class ExecutionResult:
    """纸面执行结果；fills 为空时表示没有生成持仓。"""

    order_type: str
    status: str
    reason: str
    fills: list[PaperFill]
    limit_price: float | None = None
    requested_cash: float | None = None
    expires_at: float | None = None
    post_only: bool = False


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class BuySweep:
    shares: float
    notional: float
    fee: float
    cash_spent: float
    avg_price: float
    levels_used: int
    available_shares: float
    best_price: float | None
    levels: tuple[PaperFillLevel, ...]


def simulate_fak_buy(
    intent: TradeIntent,
    quote: dict[str, Any],
    *,
    taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE,
    min_shares: float = 0.0,
    limit_price: float | None = None,
) -> ExecutionResult:
    side = intent.signal.side
    limit_price = _clamp_price(limit_price if limit_price is not None else intent.signal.entry_price)
    budget = round(float(intent.stake_dollars), 6)
    if budget <= 0:
        return ExecutionResult(ORDER_TYPE_FAK, STATUS_REJECTED, "FAK 预算无效", [], limit_price, budget)
    levels = ask_levels_from_quote(quote)
    best_ask = levels[0].price if levels else None
    if best_ask is None:
        return ExecutionResult(ORDER_TYPE_FAK, STATUS_CANCELED, f"FAK 缺少 {side} 卖一价", [], limit_price, budget)
    if best_ask > limit_price + EPSILON:
        return ExecutionResult(
            ORDER_TYPE_FAK,
            STATUS_CANCELED,
            f"FAK 卖一价 {best_ask:.4f} 高于限价 {limit_price:.4f}",
            [],
            limit_price,
            budget,
        )
    if sum(level.size for level in levels if level.price <= limit_price + EPSILON) <= 0:
        return ExecutionResult(ORDER_TYPE_FAK, STATUS_CANCELED, f"FAK 缺少 {side} 卖盘深度", [], limit_price, budget)

    sweep = sweep_taker_buy_by_budget(
        quote,
        limit_price=limit_price,
        budget=budget,
        taker_fee_rate=taker_fee_rate,
    )
    if sweep.shares < max(min_shares, EPSILON):
        return ExecutionResult(ORDER_TYPE_FAK, STATUS_CANCELED, "FAK 可成交份额不足", [], limit_price, budget)

    fill = build_taker_buy_fill_from_sweep(
        intent,
        side=side,
        order_type=ORDER_TYPE_FAK,
        status=STATUS_FILLED if sweep.cash_spent >= budget - EPSILON else STATUS_PARTIAL,
        limit_price=limit_price,
        sweep=sweep,
    )
    return ExecutionResult(fill.order_type, fill.status, fill.reason, [fill], limit_price, budget)


def simulate_post_only_buy(intent: TradeIntent, quote: dict[str, Any]) -> ExecutionResult:
    side = intent.signal.side
    limit_price = _clamp_price(intent.signal.entry_price)
    ask_price = _maybe_float(quote.get("best_ask"))
    if ask_price is not None and limit_price >= ask_price - EPSILON:
        return ExecutionResult(
            ORDER_TYPE_POST_ONLY,
            STATUS_REJECTED,
            f"POST_ONLY {side} 限价 {limit_price:.4f} 会立即吃到卖一 {ask_price:.4f}",
            [],
            limit_price,
            float(intent.stake_dollars),
        )
    return ExecutionResult(
        ORDER_TYPE_POST_ONLY,
        STATUS_RESTING,
        f"POST_ONLY {side} 挂单等待成交",
        [],
        limit_price,
        float(intent.stake_dollars),
    )


def simulate_resting_buy(
    intent: TradeIntent,
    quote: dict[str, Any],
    *,
    order_type: str,
    limit_price: float,
    expires_at: float | None = None,
    post_only: bool = False,
) -> ExecutionResult:
    side = intent.signal.side
    normalized = normalize_order_type(order_type)
    if normalized == ORDER_TYPE_POST_ONLY:
        post_only = True
    if normalized not in {ORDER_TYPE_GTC, ORDER_TYPE_GTD, ORDER_TYPE_POST_ONLY}:
        return ExecutionResult(normalized, STATUS_REJECTED, f"{normalized} 不是可挂单类型", [], limit_price, intent.stake_dollars)
    limit_price = _clamp_price(limit_price)
    budget = round(float(intent.stake_dollars), 6)
    if budget <= 0:
        return ExecutionResult(normalized, STATUS_REJECTED, "挂单预算无效", [], limit_price, budget, expires_at, post_only)
    best_ask = _maybe_float(quote.get("best_ask"))
    if post_only and best_ask is not None and limit_price >= best_ask - EPSILON:
        return ExecutionResult(
            normalized,
            STATUS_REJECTED,
            f"POST_ONLY {side} 限价 {limit_price:.4f} 会立即吃到卖一 {best_ask:.4f}",
            [],
            limit_price,
            budget,
            expires_at,
            post_only,
        )
    label = "POST_ONLY" if post_only else normalized
    return ExecutionResult(
        normalized,
        STATUS_RESTING,
        f"{label} {side} 限价 {limit_price:.4f} 挂单等待成交",
        [],
        limit_price,
        budget,
        expires_at,
        post_only,
    )


def build_taker_buy_fill(
    intent: TradeIntent,
    *,
    side: str,
    order_type: str,
    status: str,
    limit_price: float,
    fill_price: float,
    shares: float,
    quote_size: float | None,
    taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE,
) -> PaperFill:
    notional = round(shares * fill_price, 6)
    fee = taker_fee(shares, fill_price, taker_fee_rate)
    cash_spent = round(notional + fee, 6)
    levels = (PaperFillLevel(round(fill_price, 4), round(shares, 6), notional, fee, cash_spent),)
    reason = (
        f"{order_type} {status}: fill {shares:.6f} @ {fill_price:.4f}, "
        f"notional {notional:.6f}, fee {fee:.6f}"
    )
    return PaperFill(
        market=intent.market,
        signal=intent.signal,
        side=side,
        order_type=order_type,
        status=status,
        limit_price=limit_price,
        fill_price=round(fill_price, 4),
        shares=shares,
        notional=notional,
        fee=fee,
        cash_spent=cash_spent,
        quote_size=quote_size,
        reason=reason,
        levels=levels,
        requested_cash=round(float(intent.stake_dollars), 6),
    )


def build_taker_buy_fill_from_sweep(
    intent: TradeIntent,
    *,
    side: str,
    order_type: str,
    status: str,
    limit_price: float,
    sweep: BuySweep,
) -> PaperFill:
    reason = (
        f"{order_type} {status}: fill {sweep.shares:.6f} @ avg {sweep.avg_price:.4f}, "
        f"notional {sweep.notional:.6f}, fee {sweep.fee:.6f}, "
        f"levels {sweep.levels_used}, limit {limit_price:.4f}"
    )
    return PaperFill(
        market=intent.market,
        signal=intent.signal,
        side=side,
        order_type=order_type,
        status=status,
        limit_price=limit_price,
        fill_price=round(sweep.avg_price, 4),
        shares=sweep.shares,
        notional=sweep.notional,
        fee=sweep.fee,
        cash_spent=sweep.cash_spent,
        quote_size=sweep.available_shares,
        reason=reason,
        levels=sweep.levels,
        requested_cash=round(float(intent.stake_dollars), 6),
    )


def sweep_taker_buy_by_budget(
    quote: dict[str, Any],
    *,
    limit_price: float,
    budget: float,
    taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE,
) -> BuySweep:
    return _sweep_taker_buy(
        ask_levels_from_quote(quote),
        limit_price=_clamp_price(limit_price),
        budget=max(0.0, float(budget)),
        target_shares=None,
        taker_fee_rate=taker_fee_rate,
    )


def sweep_taker_buy_by_shares(
    quote: dict[str, Any],
    *,
    limit_price: float,
    shares: float,
    taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE,
) -> BuySweep:
    return _sweep_taker_buy(
        ask_levels_from_quote(quote),
        limit_price=_clamp_price(limit_price),
        budget=None,
        target_shares=max(0.0, float(shares)),
        taker_fee_rate=taker_fee_rate,
    )


def ask_levels_from_quote(quote: dict[str, Any]) -> list[BookLevel]:
    levels: list[BookLevel] = []
    raw_levels = quote.get("asks")
    if isinstance(raw_levels, list):
        for row in raw_levels:
            if not isinstance(row, dict):
                continue
            price = _maybe_float(row.get("price"))
            size = _maybe_float(row.get("size"))
            if price is None or size is None or price <= 0 or price >= 1 or size <= 0:
                continue
            levels.append(BookLevel(_clamp_price(price), round(size, 6)))
    if not levels:
        price = _maybe_float(quote.get("best_ask"))
        size = _maybe_float(quote.get("ask_size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            levels.append(BookLevel(_clamp_price(price), round(size, 6)))

    by_price: dict[float, float] = {}
    for level in levels:
        by_price[level.price] = round(by_price.get(level.price, 0.0) + level.size, 6)
    return [BookLevel(price, size) for price, size in sorted(by_price.items()) if size > 0]


def taker_fee(shares: float, price: float, taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE) -> float:
    if shares <= 0 or price <= 0 or taker_fee_rate <= 0:
        return 0.0
    return round(float(shares) * taker_fee_per_share(price, taker_fee_rate), 6)


def taker_fee_per_share(price: float, taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE) -> float:
    price = _clamp_price(price)
    return float(taker_fee_rate) * price * (1.0 - price)


def normalize_order_type(value: str | None) -> str:
    normalized = str(value or ORDER_TYPE_FAK).strip().upper().replace("-", "_")
    if normalized in {ORDER_TYPE_FAK, ORDER_TYPE_GTC, ORDER_TYPE_GTD, ORDER_TYPE_POST_ONLY}:
        return normalized
    return ORDER_TYPE_FAK


def _clamp_price(value: float) -> float:
    return max(0.01, min(0.99, round(float(value), 4)))


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sweep_taker_buy(
    levels: list[BookLevel],
    *,
    limit_price: float,
    budget: float | None,
    target_shares: float | None,
    taker_fee_rate: float,
) -> BuySweep:
    remaining_budget = budget
    remaining_shares = target_shares
    shares = 0.0
    notional = 0.0
    fee = 0.0
    levels_used = 0
    available_shares = 0.0
    best_price = levels[0].price if levels else None
    fill_levels: list[PaperFillLevel] = []

    for level in levels:
        if level.price > limit_price + EPSILON:
            break
        available_shares = round(available_shares + level.size, 6)
        cash_per_share = level.price + taker_fee_per_share(level.price, taker_fee_rate)
        if cash_per_share <= 0:
            continue
        requested = level.size
        if remaining_shares is not None:
            requested = min(requested, remaining_shares)
        if remaining_budget is not None:
            requested = min(requested, remaining_budget / cash_per_share)
        if requested <= EPSILON:
            break

        take = requested
        level_notional = take * level.price
        level_fee = take * taker_fee_per_share(level.price, taker_fee_rate)
        level_cash_spent = level_notional + level_fee
        shares += take
        notional += level_notional
        fee += level_fee
        levels_used += 1
        fill_levels.append(
            PaperFillLevel(
                price=round(level.price, 4),
                shares=round(take, 6),
                notional=round(level_notional, 6),
                fee=round(level_fee, 6),
                cash_spent=round(level_cash_spent, 6),
            )
        )

        if remaining_budget is not None:
            remaining_budget -= level_notional + level_fee
            if remaining_budget <= EPSILON:
                break
        if remaining_shares is not None:
            remaining_shares -= take
            if remaining_shares <= EPSILON:
                break

    rounded_shares = round(shares, 6)
    rounded_notional = round(notional, 6)
    rounded_fee = round(fee, 6)
    cash_spent = round(rounded_notional + rounded_fee, 6)
    avg_price = round(rounded_notional / rounded_shares, 6) if rounded_shares > 0 else 0.0
    return BuySweep(
        shares=rounded_shares,
        notional=rounded_notional,
        fee=rounded_fee,
        cash_spent=cash_spent,
        avg_price=avg_price,
        levels_used=levels_used,
        available_shares=available_shares,
        best_price=best_price,
        levels=tuple(fill_levels),
    )

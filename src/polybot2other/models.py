from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PriceTick:
    """价格样本；price 单位为 USD，timestamp 单位为 Unix 秒。"""

    symbol: str
    price: float
    source: str
    timestamp: float


@dataclass(frozen=True)
class Signal:
    """交易信号；confidence 和 entry_price 取值范围为 0 到 1。"""

    symbol: str
    side: str
    confidence: float
    entry_price: float
    move_bps: float
    reason: str


@dataclass(frozen=True)
class MarketRound:
    """真实 Polymarket BTC 5 分钟 Up/Down 市场；target_price 单位为 USD。"""

    round_id: str
    symbol: str
    started_at: float
    ends_at: float
    target_price: float
    question: str = ""
    condition_id: str = ""
    up_token: str = ""
    down_token: str = ""
    slug: str = ""
    event_slug: str = ""
    url: str = ""


@dataclass(frozen=True)
class TradeIntent:
    """纸交易下单意图；stake_dollars 单位为 USD。"""

    market: MarketRound
    signal: Signal
    stake_dollars: float


@dataclass(frozen=True)
class PaperFillLevel:
    """纸面逐档成交；price 为合约价格，cash_spent 单位为 USD。"""

    price: float
    shares: float
    notional: float
    fee: float
    cash_spent: float


@dataclass(frozen=True)
class PaperFill:
    """纸面成交结果；cash_spent 单位为 USD，shares 单位为合约份额。"""

    market: MarketRound
    signal: Signal
    side: str
    order_type: str
    status: str
    limit_price: float
    fill_price: float
    shares: float
    notional: float
    fee: float
    cash_spent: float
    quote_size: float | None
    reason: str
    levels: tuple[PaperFillLevel, ...] = field(default_factory=tuple)
    requested_cash: float | None = None

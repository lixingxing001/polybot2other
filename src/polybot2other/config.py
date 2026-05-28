from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """运行配置；当前版本只允许纸交易，不包含实盘密钥。"""

    initial_balance: float = 100.0
    db_path: Path = Path("data/polybot2other-real-btc.sqlite3")
    tick_seconds: float = 2.0
    round_seconds: int = 300
    stake_dollars: float = 5.0
    max_open_trades: int = 2
    max_daily_loss: float = 20.0
    min_confidence: float = 0.62
    min_edge: float = 0.02
    max_entry_price: float = 0.72
    min_price_distance_bps: float = 1.5
    max_spread: float = 0.08
    min_time_left_seconds: int = 20
    max_time_left_seconds: int = 285
    min_ask_size: float = 1.0
    max_quote_age_ms: int = 3000
    live_snapshot_max_age_seconds: float = 8.0
    market_refresh_seconds: float = 2.0
    price_history_limit: int = 180
    request_timeout_seconds: float = 4.0
    paper_entry_order_type: str = "FAK"
    paper_taker_fee_rate: float = 0.07
    paper_gtd_seconds: float = 90.0
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"


def _float_env(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def load_settings() -> Settings:
    return Settings(
        initial_balance=_float_env("POLYBOT2OTHER_INITIAL_BALANCE", 100.0, 1.0),
        db_path=Path(os.environ.get("POLYBOT2OTHER_DB_PATH", "data/polybot2other-real-btc.sqlite3")),
        tick_seconds=_float_env("POLYBOT2OTHER_TICK_SECONDS", 2.0, 0.5),
        round_seconds=_int_env("POLYBOT2OTHER_ROUND_SECONDS", 300, 60),
        stake_dollars=_float_env("POLYBOT2OTHER_STAKE_DOLLARS", 5.0, 0.1),
        max_open_trades=_int_env("POLYBOT2OTHER_MAX_OPEN_TRADES", 2, 1),
        max_daily_loss=_float_env("POLYBOT2OTHER_MAX_DAILY_LOSS", 20.0, 0.0),
        min_confidence=_float_env("POLYBOT2OTHER_MIN_CONFIDENCE", 0.62, 0.5),
        min_edge=_float_env("POLYBOT2OTHER_MIN_EDGE", 0.02, -0.5),
        max_entry_price=_float_env("POLYBOT2OTHER_MAX_ENTRY_PRICE", 0.72, 0.01),
        min_price_distance_bps=_float_env("POLYBOT2OTHER_MIN_PRICE_DISTANCE_BPS", 1.5, 0.0),
        max_spread=_float_env("POLYBOT2OTHER_MAX_SPREAD", 0.08, 0.0),
        min_time_left_seconds=_int_env("POLYBOT2OTHER_MIN_TIME_LEFT_SECONDS", 20, 0),
        max_time_left_seconds=_int_env("POLYBOT2OTHER_MAX_TIME_LEFT_SECONDS", 285, 1),
        min_ask_size=_float_env("POLYBOT2OTHER_MIN_ASK_SIZE", 1.0, 0.0),
        max_quote_age_ms=_int_env("POLYBOT2OTHER_MAX_QUOTE_AGE_MS", 3000, 100),
        live_snapshot_max_age_seconds=_float_env("POLYBOT2OTHER_LIVE_SNAPSHOT_MAX_AGE_SECONDS", 8.0, 1.0),
        market_refresh_seconds=_float_env("POLYBOT2OTHER_MARKET_REFRESH_SECONDS", 2.0, 0.5),
        paper_entry_order_type=os.environ.get("POLYBOT2OTHER_PAPER_ENTRY_ORDER_TYPE", "FAK"),
        paper_taker_fee_rate=_float_env("POLYBOT2OTHER_PAPER_TAKER_FEE_RATE", 0.07, 0.0),
        paper_gtd_seconds=_float_env("POLYBOT2OTHER_PAPER_GTD_SECONDS", 90.0, 1.0),
        gamma_url=os.environ.get("POLYBOT2OTHER_GAMMA_URL", "https://gamma-api.polymarket.com"),
        clob_url=os.environ.get("POLYBOT2OTHER_CLOB_URL", "https://clob.polymarket.com"),
    )

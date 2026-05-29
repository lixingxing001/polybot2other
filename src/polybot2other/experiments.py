from __future__ import annotations

from dataclasses import dataclass

from .execution import ORDER_TYPE_FAK, ORDER_TYPE_GTC, ORDER_TYPE_GTD, ORDER_TYPE_POST_ONLY


STRATEGY_FAMILY_SINGLE = "SINGLE"
STRATEGY_FAMILY_PAIR = "PAIR"
SINGLE_ENTRY_MODE_LEGACY = "LEGACY"
SINGLE_ENTRY_MODE_STRICT = "STRICT"
SINGLE_ENTRY_MODE_REVERSAL = "REVERSAL"
SINGLE_ENTRY_MODE_STOP_AND_FLIP = "STOP_AND_FLIP"
MARKET_DATA_MODE_BASE = "BASE"
MARKET_DATA_MODE_MULTI_CONFIRM = "MULTI_CONFIRM"
MARKET_DATA_MODE_MULTI_LEAD = "MULTI_LEAD"
PRICE_SOURCE_MODE_MIXED = "MIXED"
PRICE_SOURCE_MODE_CHAINLINK_ONLY = "CHAINLINK_ONLY"
PRICE_SOURCE_MODE_FALLBACK_ONLY = "FALLBACK_ONLY"
ANTI_BOT_GUARD_MODE_NONE = "NONE"
ANTI_BOT_GUARD_MODE_ENABLED = "ANTI_BOT_GUARD"


@dataclass(frozen=True)
class StrategyVariant:
    """策略实验组合；用于隔离账户并行跑 Paper 对照组。"""

    variant_id: str
    strategy_family: str
    order_type: str
    target_code_completion: str
    target_report_alignment: str
    role: str
    single_entry_mode: str = SINGLE_ENTRY_MODE_LEGACY
    market_data_mode: str = MARKET_DATA_MODE_BASE
    price_source_mode: str = PRICE_SOURCE_MODE_MIXED
    anti_bot_guard_mode: str = ANTI_BOT_GUARD_MODE_NONE

    @property
    def combo(self) -> str:
        suffixes: list[str] = []
        if self.market_data_mode != MARKET_DATA_MODE_BASE:
            suffixes.append(self.market_data_mode)
        if self.price_source_mode != PRICE_SOURCE_MODE_MIXED:
            suffixes.append(self.price_source_mode)
        if self.anti_bot_guard_mode != ANTI_BOT_GUARD_MODE_NONE:
            suffixes.append(self.anti_bot_guard_mode)
        data_suffix = f" {' '.join(suffixes)}" if suffixes else ""
        if (
            self.strategy_family == STRATEGY_FAMILY_SINGLE
            and self.order_type == ORDER_TYPE_FAK
            and self.single_entry_mode != SINGLE_ENTRY_MODE_LEGACY
        ):
            return f"{self.strategy_family} + {self.order_type} {self.single_entry_mode}{data_suffix}"
        return f"{self.strategy_family} + {self.order_type}{data_suffix}"


STRATEGY_VARIANTS: tuple[StrategyVariant, ...] = (
    StrategyVariant("SINGLE_FAK", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_FAK, "85%", "25%-35%", "当前基线，对照组"),
    StrategyVariant(
        "SINGLE_FAK_CHAINLINK_ONLY",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "85%",
        "25%-35%",
        "Chainlink-only 价格源候选",
        price_source_mode=PRICE_SOURCE_MODE_CHAINLINK_ONLY,
    ),
    StrategyVariant(
        "SINGLE_FAK_ANTI_BOT_GUARD",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "采样",
        "待验证",
        "Paper-only bot-trap 防守采样，实盘禁止直接沿用",
        price_source_mode=PRICE_SOURCE_MODE_CHAINLINK_ONLY,
        anti_bot_guard_mode=ANTI_BOT_GUARD_MODE_ENABLED,
    ),
    StrategyVariant(
        "SINGLE_FAK_FALLBACK_ONLY",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "采样",
        "不适用",
        "Paper-only fallback 负面对照，实盘禁止",
        price_source_mode=PRICE_SOURCE_MODE_FALLBACK_ONLY,
    ),
    StrategyVariant(
        "SINGLE_FAK_MULTI_CONFIRM",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "85%",
        "25%-35%",
        "OKX/Binance 残差确认实验",
        market_data_mode=MARKET_DATA_MODE_MULTI_CONFIRM,
    ),
    StrategyVariant(
        "SINGLE_FAK_MULTI_LEAD",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "75%-80%",
        "30%-40%",
        "OKX/Binance 残差领先修正实验",
        market_data_mode=MARKET_DATA_MODE_MULTI_LEAD,
    ),
    StrategyVariant(
        "SINGLE_FAK_STRICT",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "85%",
        "25%-35%",
        "小资金实盘保守候选",
        SINGLE_ENTRY_MODE_STRICT,
    ),
    StrategyVariant(
        "SINGLE_FAK_REVERSAL",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "85%",
        "25%-35%",
        "显式反转双边实验",
        SINGLE_ENTRY_MODE_REVERSAL,
    ),
    StrategyVariant(
        "SINGLE_FAK_STOP_AND_FLIP",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "75%-80%",
        "30%-40%",
        "真实止损反手实验",
        SINGLE_ENTRY_MODE_STOP_AND_FLIP,
    ),
    StrategyVariant("SINGLE_GTC", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_GTC, "75%-80%", "30%-40%", "单边挂单实验"),
    StrategyVariant("SINGLE_GTD", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_GTD, "75%-80%", "30%-40%", "单边限时挂单实验"),
    StrategyVariant("SINGLE_POST_ONLY", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_POST_ONLY, "75%-80%", "35%-45%", "单边 maker 实验"),
    StrategyVariant("PAIR_FAK", STRATEGY_FAMILY_PAIR, ORDER_TYPE_FAK, "80%-85%", "55%-65%", "配对 taker / 补单 / 应急"),
    StrategyVariant(
        "PAIR_FAK_MULTI_CONFIRM",
        STRATEGY_FAMILY_PAIR,
        ORDER_TYPE_FAK,
        "80%-85%",
        "55%-65%",
        "OKX/Binance 残差确认配对实验",
        market_data_mode=MARKET_DATA_MODE_MULTI_CONFIRM,
    ),
    StrategyVariant(
        "PAIR_FAK_MULTI_LEAD",
        STRATEGY_FAMILY_PAIR,
        ORDER_TYPE_FAK,
        "75%-80%",
        "55%-65%",
        "OKX/Binance 残差领先配对实验",
        market_data_mode=MARKET_DATA_MODE_MULTI_LEAD,
    ),
    StrategyVariant("PAIR_GTC", STRATEGY_FAMILY_PAIR, ORDER_TYPE_GTC, "90%+", "85%-90%", "核心候选"),
    StrategyVariant("PAIR_GTD", STRATEGY_FAMILY_PAIR, ORDER_TYPE_GTD, "90%+", "85%-90%", "核心候选，尤其适合 5m 市场"),
    StrategyVariant("PAIR_POST_ONLY", STRATEGY_FAMILY_PAIR, ORDER_TYPE_POST_ONLY, "90%+", "90%+", "最核心目标"),
)


def selected_strategy_variants(raw_variant_ids: str | None) -> tuple[StrategyVariant, ...]:
    """按配置筛选实验组合；空值代表启用全部策略实验组合。"""

    raw = str(raw_variant_ids or "").strip()
    if not raw:
        return STRATEGY_VARIANTS
    wanted = {item.strip().upper().replace("-", "_") for item in raw.split(",") if item.strip()}
    if not wanted:
        return STRATEGY_VARIANTS
    by_id = {variant.variant_id: variant for variant in STRATEGY_VARIANTS}
    unknown = sorted(wanted.difference(by_id))
    if unknown:
        allowed = ", ".join(sorted(by_id))
        raise ValueError(f"unknown strategy experiment variants: {', '.join(unknown)}; allowed: {allowed}")
    return tuple(variant for variant in STRATEGY_VARIANTS if variant.variant_id in wanted)

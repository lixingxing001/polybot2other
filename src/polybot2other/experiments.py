from __future__ import annotations

from dataclasses import dataclass

from .execution import ORDER_TYPE_FAK, ORDER_TYPE_GTC, ORDER_TYPE_GTD, ORDER_TYPE_POST_ONLY


STRATEGY_FAMILY_SINGLE = "SINGLE"
STRATEGY_FAMILY_PAIR = "PAIR"
SINGLE_ENTRY_MODE_LEGACY = "LEGACY"
SINGLE_ENTRY_MODE_STRICT = "STRICT"
SINGLE_ENTRY_MODE_REVERSAL = "REVERSAL"
SINGLE_ENTRY_MODE_STOP_AND_FLIP = "STOP_AND_FLIP"


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

    @property
    def combo(self) -> str:
        if (
            self.strategy_family == STRATEGY_FAMILY_SINGLE
            and self.order_type == ORDER_TYPE_FAK
            and self.single_entry_mode != SINGLE_ENTRY_MODE_LEGACY
        ):
            return f"{self.strategy_family} + {self.order_type} {self.single_entry_mode}"
        return f"{self.strategy_family} + {self.order_type}"


STRATEGY_VARIANTS: tuple[StrategyVariant, ...] = (
    StrategyVariant("SINGLE_FAK", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_FAK, "85%", "25%-35%", "当前基线，对照组"),
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

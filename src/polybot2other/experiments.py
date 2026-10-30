from __future__ import annotations

from dataclasses import dataclass

from .execution import ORDER_TYPE_FAK


STRATEGY_FAMILY_SINGLE = "SINGLE"
STRATEGY_FAMILY_PAIR = "PAIR"
STRATEGY_FAMILY_REALTIME_MAKER = "REALTIME_MAKER"
STRATEGY_FAMILY_LLM_SUPER_AGENT = "LLM_SUPER_AGENT"
SINGLE_ENTRY_MODE_LEGACY = "LEGACY"
SINGLE_ENTRY_MODE_STRICT = "STRICT"
SINGLE_ENTRY_MODE_REVERSAL = "REVERSAL"
SINGLE_ENTRY_MODE_STOP_AND_FLIP = "STOP_AND_FLIP"
SIGNAL_SIDE_MODE_BASE = "BASE"
SIGNAL_SIDE_MODE_REVERSE = "REVERSE"
SIGNAL_FILTER_MODE_NONE = "NONE"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE = "AGGRESSIVE_EDGE"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1 = "AGGRESSIVE_EDGE_V1"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2 = "AGGRESSIVE_EDGE_V2"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3 = "AGGRESSIVE_EDGE_V3"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC = "AGGRESSIVE_EDGE_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC = "AGGRESSIVE_EDGE_V4_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC = "AGGRESSIVE_EDGE_V5_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC = "AGGRESSIVE_EDGE_V6_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC = "AGGRESSIVE_EDGE_V7_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC = "AGGRESSIVE_EDGE_V8_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC = "AGGRESSIVE_EDGE_V9_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC = "AGGRESSIVE_EDGE_V10_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC = "AGGRESSIVE_EDGE_V11_DIAGNOSTIC"
SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC = "AGGRESSIVE_EDGE_V12_DIAGNOSTIC"
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
    signal_side_mode: str = SIGNAL_SIDE_MODE_BASE
    signal_filter_mode: str = SIGNAL_FILTER_MODE_NONE
    market_data_mode: str = MARKET_DATA_MODE_BASE
    price_source_mode: str = PRICE_SOURCE_MODE_MIXED
    anti_bot_guard_mode: str = ANTI_BOT_GUARD_MODE_NONE

    @property
    def combo(self) -> str:
        if self.variant_id == "MAIN_TP_SL":
            return "主账户止盈止损"
        if self.variant_id == "MAIN_OPTIMIZED":
            return "主账户（优化版）"
        if self.variant_id == "VALUE_CONFIRM_V1":
            return "价值确认 V1"
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
            entry_suffix = f" {self.single_entry_mode}"
        else:
            entry_suffix = ""
        signal_suffix = " Reverse" if self.signal_side_mode == SIGNAL_SIDE_MODE_REVERSE else ""
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE:
            filter_suffix = " Aggressive Edge"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1:
            filter_suffix = " Aggressive Edge V1"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2:
            filter_suffix = " Aggressive Edge V2"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3:
            filter_suffix = " Aggressive Edge V3"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V4 Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V5 Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V6 Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V7 Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V8 Learning Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V9 M1 Guard Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V10 Up Reversal Guard Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V11 Depth Momentum Diagnostic"
        elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC:
            filter_suffix = " Aggressive Edge V12 Reversal Guard Diagnostic"
        else:
            filter_suffix = ""
        return f"{self.strategy_family} + {self.order_type}{entry_suffix}{data_suffix}{signal_suffix}{filter_suffix}"


VALUE_CONFIRM_VARIANT_ID = "VALUE_CONFIRM_V1"
SIGNAL_FILTER_MODE_VALUE_CONFIRM = "VALUE_CONFIRM_V1"

STRATEGY_VARIANTS: tuple[StrategyVariant, ...] = (
    StrategyVariant("MAIN_TP_SL", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_FAK,
                    "已实现", "待前向验证", "独立 Paper：沿用主账户入场，净收益 +20% / −40% 退出试验",
                    signal_filter_mode="MAIN_TP_SL"),
    StrategyVariant("MAIN_OPTIMIZED", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_FAK,
                    "已实现", "待前向验证", "独立 Paper：同源 TWAP、费用与波动约束",
                    signal_filter_mode="MAIN_OPTIMIZED"),
    StrategyVariant(VALUE_CONFIRM_VARIANT_ID, STRATEGY_FAMILY_SINGLE, ORDER_TYPE_FAK,
                    "已实现", "待前向验证", "独立 Paper：价值价带、方向确认、限额风控",
                    single_entry_mode=SINGLE_ENTRY_MODE_STRICT,
                    signal_filter_mode=SIGNAL_FILTER_MODE_VALUE_CONFIRM),
)

# Retired implementations have no runnable registration and cannot create accounts.
DEPRECATED_STRATEGY_VARIANT_IDS = frozenset()


def active_strategy_variants() -> tuple[StrategyVariant, ...]:
    """独立 Paper 策略；原主账户由原运行器管理。"""

    return tuple(variant for variant in STRATEGY_VARIANTS if variant.variant_id not in DEPRECATED_STRATEGY_VARIANT_IDS)


def selected_strategy_variants(raw_variant_ids: str | None) -> tuple[StrategyVariant, ...]:
    """按配置筛选实验组合；空值代表启用全部策略实验组合。"""

    raw = str(raw_variant_ids or "").strip()
    if not raw:
        return active_strategy_variants()
    wanted = {item.strip().upper().replace("-", "_") for item in raw.split(",") if item.strip()}
    if not wanted:
        return active_strategy_variants()
    # 已淘汰组合：为兼容旧配置，这里静默忽略历史 variant_id。
    wanted = wanted.difference(DEPRECATED_STRATEGY_VARIANT_IDS)
    if not wanted:
        return tuple()
    by_id = {variant.variant_id: variant for variant in STRATEGY_VARIANTS}
    unknown = sorted(wanted.difference(by_id))
    if unknown:
        allowed = ", ".join(sorted(by_id))
        raise ValueError(f"unknown strategy experiment variants: {', '.join(unknown)}; allowed: {allowed}")
    return tuple(variant for variant in STRATEGY_VARIANTS if variant.variant_id in wanted)

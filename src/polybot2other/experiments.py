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


STRATEGY_VARIANTS: tuple[StrategyVariant, ...] = (
    StrategyVariant("SINGLE_FAK", STRATEGY_FAMILY_SINGLE, ORDER_TYPE_FAK, "85%", "25%-35%", "当前基线，对照组"),
    StrategyVariant(
        "SINGLE_FAK_REVERSE",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "采样",
        "待验证",
        "Paper-only 基线信号反向下注对照",
        signal_side_mode=SIGNAL_SIDE_MODE_REVERSE,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "采样",
        "待验证",
        "Paper-only Aggressive Edge 基准组，只保留基础激进入场过滤",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V1",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "采样",
        "待验证",
        "Paper-only Aggressive Edge V1，迁移输单反思后的学习过滤，和基准组隔离对照",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V2",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "采样",
        "待验证",
        "Paper-only Aggressive Edge V2，影子计算盘口微观特征和动量衰竭得分",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V3",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "采样",
        "待验证",
        "Paper-only Aggressive Edge V3，读取历史输局指纹并在下注前做负期望守卫",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "诊断",
        "只采样",
        "Paper-only Aggressive Edge 诊断组合，只记录候选和输局指纹，不主动下注",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V4_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "诊断",
        "只采样",
        "Paper-only Aggressive Edge V4 诊断组合，按复盘出的反转结构记录 V4 候选，不主动下注",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "诊断",
        "只采样",
        "Paper-only Aggressive Edge V5 诊断组合，收紧 Down 反转风险和早晚时间桶，只记录不主动下注",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V6_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "诊断",
        "只采样",
        "Paper-only Aggressive Edge V6 诊断组合，继承 V5 后只放行低风险和非极端位移候选",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V7_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "诊断",
        "只采样",
        "Paper-only Aggressive Edge V7 诊断组合，继承 V6 后验证盘口深度支撑和 Down 入场价上限",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V8_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "学习采样",
        "只采样",
        "Paper-only Aggressive Edge V8 学习采样组合，放宽至基础候选并记录盘口和风险标签，不主动下注",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V9_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "实盘候选诊断",
        "只采样",
        "Paper-only Aggressive Edge V9 诊断组合，继承 V8 后屏蔽 V8 复盘亏损最集中的 m1 时间桶",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V10_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "实盘候选诊断",
        "只采样",
        "Paper-only Aggressive Edge V10 诊断组合，继承 V9 后拦截 Up 动能不足和顶层盘口支撑弱的反转风险",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V11_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "实盘候选诊断",
        "只采样",
        "Paper-only Aggressive Edge V11 诊断组合，用历史全样本筛出的 m2/m3 深盘口强动量规则验证实盘候选",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
    ),
    StrategyVariant(
        "SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC",
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "实盘候选诊断",
        "只采样",
        "Paper-only Aggressive Edge V12 诊断组合，继承 V11 REAL Guard 后拦截过度位移和 Down 顶层盘口不足的反转风险",
        signal_filter_mode=SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
    ),
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
)

DEPRECATED_STRATEGY_VARIANT_IDS: frozenset[str] = frozenset(
    {
        "PAIR_FAK",
        "PAIR_FAK_MULTI_CONFIRM",
        "PAIR_FAK_MULTI_LEAD",
        "PAIR_GTC",
        "PAIR_GTD",
        "PAIR_POST_ONLY",
        "REALTIME_MAKER_POST_ONLY",
        "SINGLE_POST_ONLY",
        "SINGLE_GTD",
        "LLM_SUPER_AGENT_PAPER",
        "SINGLE_GTC",
        "SINGLE_FAK_STRICT",
        "SINGLE_FAK_MULTI_LEAD",
        "SINGLE_FAK_MULTI_CONFIRM",
        "SINGLE_FAK_AGGRESSIVE_EDGE",
        "SINGLE_FAK_AGGRESSIVE_EDGE_V1",
        "SINGLE_FAK_AGGRESSIVE_EDGE_V2",
        "SINGLE_FAK_AGGRESSIVE_EDGE_V3",
    }
)


def active_strategy_variants() -> tuple[StrategyVariant, ...]:
    """默认运行组合；已证伪的 Aggressive Edge 交易版只保留历史数据，不再主动跑。"""

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

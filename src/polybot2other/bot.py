from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from .actor_analysis import PolymarketDataClient, build_actor_analysis
from .aggressive_edge_v3 import (
    aggressive_edge_v3_guard_note,
    aggressive_edge_v3_guard_report,
    aggressive_edge_v3_memory_summary,
)
from .clob_ws import (
    SPOT_WS_SOURCE_BINANCE,
    SPOT_WS_SOURCE_OKX,
    ClobMarketWebSocketFeed,
    RtdsChainlinkWebSocketFeed,
    SpotPriceWebSocketFeed,
)
from .config import (
    DEFAULT_BTC_RUNTIME_SETTINGS_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_MARKET_SCOUT_PAPER_DB_PATH,
    DEFAULT_MARKET_SCOUT_SETTINGS_PATH,
    Settings,
    reload_live_credential_env,
)
from .execution import (
    ORDER_TYPE_GTC,
    ORDER_TYPE_GTD,
    ORDER_TYPE_FAK,
    ORDER_TYPE_POST_ONLY,
    STATUS_PARTIAL_RESTING,
    STATUS_RESTING,
    ask_levels_from_quote,
    build_taker_buy_fill_from_sweep,
    normalize_order_type,
    simulate_fak_buy,
    simulate_resting_buy,
    sweep_taker_buy_by_shares,
    taker_fee,
)
from .evidence import MarketEvidenceScout
from .experiments import (
    ANTI_BOT_GUARD_MODE_NONE,
    MARKET_DATA_MODE_BASE,
    MARKET_DATA_MODE_MULTI_CONFIRM,
    MARKET_DATA_MODE_MULTI_LEAD,
    PRICE_SOURCE_MODE_MIXED,
    SIGNAL_SIDE_MODE_BASE,
    SIGNAL_SIDE_MODE_REVERSE,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_NONE,
    SINGLE_ENTRY_MODE_LEGACY,
    SINGLE_ENTRY_MODE_REVERSAL,
    SINGLE_ENTRY_MODE_STOP_AND_FLIP,
    SINGLE_ENTRY_MODE_STRICT,
    STRATEGY_FAMILY_LLM_SUPER_AGENT,
    STRATEGY_FAMILY_PAIR,
    STRATEGY_FAMILY_REALTIME_MAKER,
    StrategyVariant,
    selected_strategy_variants,
)
from .llm_agent import (
    LLM_MIN_CONFIDENCE_TO_TRADE,
    LLM_ROUTE_NO_TRADE,
    LlmSuperAgentRouter,
    _chat_completion_content,
    _extract_json_object,
    build_llm_market_features,
    route_execution_modes,
)
from .loss_replay import AggressiveEdgeLossReplayRecorder
from .live import (
    LIVE_COMBO,
    LIVE_PAPER_COMBO,
    LIVE_PAPER_STOP_WIN_COMBO,
    LIVE_PAPER_STOP_WIN_VARIANT_ID,
    LIVE_PAPER_VARIANT_ID,
    LIVE_VARIANT_ID,
    LivePaperStrategyRunner,
    LivePaperStopWinStrategyRunner,
    LiveStrategyRunner,
    _live_strategy_db_path,
    _live_strategy_meta,
    _tag_live_rows,
)
from .signal_filters import (
    SINGLE_AGGRESSIVE_EDGE_MARKER,
    aggressive_edge_block_reason,
    aggressive_edge_false_breakout_block_reason,
    aggressive_edge_paper_v2_block_reason,
    aggressive_edge_pass_note,
    aggressive_edge_v2_risk_note,
    aggressive_edge_v2_risk_report,
)
from .market import PublicPriceClient
from .models import MarketRound, Signal, TradeIntent
from .polymarket import PolymarketClient, market_to_payload
from .storage import (
    SETTLEMENT_SOURCE_CHAINLINK,
    SETTLEMENT_SOURCE_EARLY_EXIT,
    SETTLEMENT_SOURCE_POLYMARKET,
    TradeStore,
    normalize_paper_order_status_filter,
)
from .strategy import MULTI_SOURCE_KEYS, RealBtcFiveMinuteStrategy, input_from_snapshot, multi_source_price_context


logger = logging.getLogger(__name__)
LIVE_SNAPSHOT_MIN_INTERVAL_SECONDS = 0.5
BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS = 1.0
BACKEND_MARKET_DATA_REFRESH_RATIO = 0.5
LIVE_DIAGNOSTIC_HISTORY_LIMIT = 600
LIVE_TICK_PROFILE_HISTORY_LIMIT = 240
LIVE_SLOW_TICK_LOG_INTERVAL_SECONDS = 15.0
LIVE_SLOW_TICK_WARN_MS = 3_000.0
MARKET_LLM_TERMINAL_LOG_LIMIT = 240
MARKET_LLM_TERMINAL_HEARTBEAT_SECONDS = 15.0
MARKET_SCOUT_REJECT_LOG_LIMIT = 6
MARKET_SCOUT_DISPLAY_CANDIDATE_LIMIT = 20
MARKET_SCOUT_SCAN_LOG_CANDIDATE_LIMIT = 10
MARKET_SCOUT_ORDER_SKIP_LOG_LIMIT = 8
MARKET_SCOUT_PROMPT_CANDIDATE_LIMIT = 20
MARKET_SCOUT_PROMPT_MAX_CHARS = 28_000
MARKET_SCOUT_SYMBOL = "MARKET"
MARKET_SCOUT_LIVE_LOCKED_MESSAGE = "市场页实盘自动下注未接入，本版本只允许 Paper 自动下注"
BTC_RUNTIME_PAUSED_REASON = "BTC 数据业务已暂停，跳过行情采集和策略执行"
BTC_RUNTIME_PAUSED_MESSAGE = "BTC 数据采集已暂停"
BTC_RUNTIME_RUNNING_MESSAGE = "BTC 数据采集中"


def _resolve_btc_runtime_settings_path(settings: Settings) -> Path:
    """自定义数据库实例默认使用同目录运行态文件，避免不同实例共享暂停状态。"""

    if (
        settings.btc_runtime_settings_path == DEFAULT_BTC_RUNTIME_SETTINGS_PATH
        and settings.db_path != DEFAULT_DB_PATH
    ):
        return settings.db_path.parent / DEFAULT_BTC_RUNTIME_SETTINGS_PATH.name
    return settings.btc_runtime_settings_path


def _resolve_market_scout_settings_path(settings: Settings) -> Path:
    """自定义数据库实例默认使用同目录市场配置，避免测试或多实例共享运行态。"""

    if (
        settings.market_scout_settings_path == DEFAULT_MARKET_SCOUT_SETTINGS_PATH
        and settings.db_path != DEFAULT_DB_PATH
    ):
        return settings.db_path.parent / "market-scout-settings.json"
    return settings.market_scout_settings_path


def _resolve_market_scout_paper_db_path(settings: Settings) -> Path:
    """自定义数据库实例默认使用同目录市场 Paper 账本，保持 BTC 与市场账户隔离。"""

    if (
        settings.market_scout_paper_db_path == DEFAULT_MARKET_SCOUT_PAPER_DB_PATH
        and settings.db_path != DEFAULT_DB_PATH
    ):
        return settings.db_path.parent / "market-scout-paper.sqlite3"
    return settings.market_scout_paper_db_path


OFFICIAL_RECHECK_INTERVAL_SECONDS = 10.0
OFFICIAL_RECHECK_WINDOW_SECONDS = 24 * 60 * 60
OFFICIAL_RECHECK_LIMIT = 5
OFFICIAL_PRICE_BACKFILL_INTERVAL_SECONDS = 60.0
OFFICIAL_PRICE_BACKFILL_WINDOW_SECONDS = 24 * 60 * 60
OFFICIAL_PRICE_BACKFILL_LIMIT = 3
RECENT_TRADES_DEFAULT_LIMIT = 100
RECENT_TRADES_MAX_LIMIT = 500
ORDERS_DEFAULT_LIMIT = 20
ORDERS_MAX_LIMIT = 200
EQUITY_CURVE_DEFAULT_DAYS = 90
EQUITY_CURVE_DEFAULT_MAX_POINTS = 1200
EQUITY_CURVE_MAX_POINTS = 5000
TRADE_STATUS_PENDING_SETTLEMENT = "PENDING_SETTLEMENT"
PAIR_ENTRY_COST_THRESHOLD = 0.98
PAIR_EXIT_BID_THRESHOLD = 0.98
PAIR_ENTRY_MIN_SECONDS_LEFT = 45
PAIR_RESIDUAL_REDUCE_SECONDS_LEFT = 45
PAIR_FORCE_FLATTEN_SECONDS_LEFT = 30
PAIR_RESIDUAL_STOP_LOSS_PCT = -20.0
# Paper-only sampling guard: loosened to collect strategy-experiment samples.
# Do not reuse this threshold for live trading risk; live must use stricter loss controls.
PAIR_DAILY_LOSS_PCT = 10.0
PAIR_DAILY_LOSS_NOTE = "Paper采样阈值，实盘不得沿用"
PAIR_STOP_STREAK_LIMIT = 3
PAIR_EPSILON = 0.000001
PAIR_MULTI_READY_MARKER = "PAIR_MULTI"
SINGLE_STRICT_MARKER = "SINGLE_STRICT"
SINGLE_REVERSE_MARKER = "SINGLE_REVERSE"
SINGLE_AGGRESSIVE_EDGE_REPLAY_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE"
SINGLE_AGGRESSIVE_EDGE_REPLAY_COMBO = "SINGLE + FAK Aggressive Edge"
SINGLE_AGGRESSIVE_EDGE_REPLAY_FILE = "single_fak_aggressive_edge.jsonl"
AGGRESSIVE_EDGE_FILTER_MODES = frozenset(
    {
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
        SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
    }
)
# V5 只用于诊断采样。阈值来自 91 条 V4 已结算样本复盘，先验证再考虑交易化。
AGGRESSIVE_EDGE_V5_UP_MIN_BUCKET = 2
AGGRESSIVE_EDGE_V5_DOWN_ALLOWED_BUCKETS = frozenset({2, 3})
AGGRESSIVE_EDGE_V5_DOWN_MAX_ENTRY_PRICE = 0.70
AGGRESSIVE_EDGE_V5_DOWN_MIN_DEPTH_SKEW = 0.25
AGGRESSIVE_EDGE_V5_DOWN_MIN_TOP_LEVEL_SKEW = 0.25
AGGRESSIVE_EDGE_V5_DOWN_MAX_ABS_MOVE_BPS = 10.0
# V6 只用于诊断采样。阈值来自 58 条 V5 已结算样本复盘，先采样验证再考虑交易化。
AGGRESSIVE_EDGE_V6_MAX_RISK_SCORE = 0.25
AGGRESSIVE_EDGE_V6_MAX_ABS_MOVE_BPS = 8.0
# V7 只用于诊断采样。阈值来自 V6 35 条已结算放行样本复盘，目标是验证 Up 盘口支撑和 Down 赔率约束。
AGGRESSIVE_EDGE_V7_UP_M2_MIN_DEPTH_SKEW = 0.55
AGGRESSIVE_EDGE_V7_UP_M3_MIN_DEPTH_SKEW = 0.70
AGGRESSIVE_EDGE_V7_DOWN_MAX_ENTRY_PRICE = 0.68
# V8 只用于学习采样。它故意放宽到基础 Aggressive Edge 候选，用标签积累输赢结构，不能直接交易化。
AGGRESSIVE_EDGE_V8_MAX_RISK_SCORE = 0.90
AGGRESSIVE_EDGE_V8_MAX_ABS_MOVE_BPS = 20.0
AGGRESSIVE_EDGE_V8_ALLOWED_BUCKETS = frozenset({0, 1, 2, 3, 4})
# V9 是 V8 早期失败复盘后的实盘候选诊断版。m1 当前 12 单 5胜7负，先整桶排除再重新采样。
AGGRESSIVE_EDGE_V9_BLOCKED_BUCKETS = frozenset({1})
# V10 继承 V9。当前 V9 的亏损集中在 Up 反转，先拦截动能不足和顶层盘口支撑偏弱的 Up 候选。
AGGRESSIVE_EDGE_V10_UP_MIN_ABS_MOVE_BPS = 5.7
AGGRESSIVE_EDGE_V10_UP_MIN_TOP_LEVEL_SKEW = 0.20
# V11 来自 773 条原始会下注样本的三段验证，保留 m2/m3 的强波动、深盘口、低风险候选。
AGGRESSIVE_EDGE_V11_ALLOWED_BUCKETS = frozenset({2, 3})
AGGRESSIVE_EDGE_V11_MIN_ABS_MOVE_BPS = 5.5
AGGRESSIVE_EDGE_V11_MIN_DEPTH_SKEW = 0.35
AGGRESSIVE_EDGE_V11_MAX_RISK_SCORE = 0.25
# V12 是 V11 REAL Guard 的影子验证版：保留 V11 的强样本，只验证过度位移和 Down 顶层盘口不足风险。
AGGRESSIVE_EDGE_V12_MAX_ABS_MOVE_BPS = 8.0
AGGRESSIVE_EDGE_V12_UP_MIN_TOP_LEVEL_SKEW = 0.20
AGGRESSIVE_EDGE_V12_DOWN_MIN_TOP_LEVEL_SKEW = 0.30
SINGLE_REVERSAL_MARKER = "SINGLE_REVERSAL"
SINGLE_STOP_AND_FLIP_MARKER = "SINGLE_STOP_AND_FLIP"
REALTIME_MAKER_MARKER = "REALTIME_MAKER_PAPER"
REALTIME_MAKER_ENTRY_MIN_FAIR = 0.57
REALTIME_MAKER_ENTRY_MIN_EDGE = 0.03
REALTIME_MAKER_CANCEL_MIN_EDGE = 0.01
REALTIME_MAKER_ORDER_TTL_SECONDS = 35.0
REALTIME_MAKER_CANCEL_GRACE_SECONDS = 10.0
REALTIME_MAKER_BID_IMPROVEMENT = 0.01
REALTIME_MAKER_STOP_ENTRY_SECONDS_LEFT = 75.0
REALTIME_MAKER_REDUCE_SECONDS_LEFT = 45.0
REALTIME_MAKER_FORCE_EXIT_SECONDS_LEFT = 25.0
REALTIME_MAKER_TAKE_PROFIT = 0.04
REALTIME_MAKER_EDGE_GONE_SECONDS = 18.0
REALTIME_MAKER_EDGE_GONE_BUFFER = 0.01
REALTIME_MAKER_STOP_FAIR_DRAWDOWN = 0.04
REALTIME_MAKER_STOP_BID_DRAWDOWN = 0.06
REALTIME_MAKER_ACTOR_BLOCK_THRESHOLD = 0.48
PRICE_BASIS_MAX_SAMPLES = 180
POST_ONLY_MIN_REST_SECONDS = 8.0
POST_ONLY_CROSS_BUFFER = 0.005
POST_ONLY_QUEUE_INITIAL_FILL_RATIO = 0.25
POST_ONLY_QUEUE_MAX_FILL_RATIO = 0.75
POST_ONLY_QUEUE_FULL_SECONDS = 90.0
LIVE_ONCE_CONFIRM_PHRASE = "PLACE_REAL_ORDER"
LIVE_ONCE_WAITABLE_BLOCKERS = {"enabled", "market", "target_price", "signal", "orderbook_depth"}
LIVE_ONCE_AUDIT_DIR_NAME = "audit"
PAPER_PAUSE_REASON = "PAPER_PAUSED Paper 下单已暂停，行情和结算继续"
PAPER_PAUSE_CANCEL_REASON = "PAPER_PAUSED 暂停 Paper 下单，取消活跃挂单"
ACTOR_ANALYSIS_CACHE_SECONDS = 4.5
LIVE_ONCE_AUDIT_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "private_key",
    "raw",
    "raw_response",
    "secret",
    "signed_order",
    "signature",
}


class LiveOnceBlockedError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        message = str(payload.get("error") or "one-shot live run blocked")
        super().__init__(message)
        self.payload = payload


class PriceBasisTracker:
    """跟踪 OKX/Binance 相对 Chainlink 的短窗基差。"""

    def __init__(self, max_samples: int = PRICE_BASIS_MAX_SAMPLES) -> None:
        self.max_samples = max(10, int(max_samples))
        self._basis: dict[str, list[float]] = {source: [] for source in MULTI_SOURCE_KEYS}
        self._last_sample_keys: dict[str, tuple[int, int]] = {}

    def enrich(self, price: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
        enriched = dict(price)
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        for source in MULTI_SOURCE_KEYS:
            samples = self._basis.setdefault(source, [])
            if samples:
                enriched[f"{source}_basis_median_bps"] = _median(samples)
                enriched[f"{source}_basis_samples"] = len(samples)
        chainlink = _maybe_float(enriched.get("chainlink"))
        chainlink_updated_ms = _maybe_int(enriched.get("chainlink_updated_ms"))
        chainlink_age_ms = max(0, now_ms - chainlink_updated_ms) if chainlink_updated_ms else None
        if chainlink and chainlink > 0 and chainlink_updated_ms and chainlink_age_ms <= self.settings_max_age_ms:
            for source in MULTI_SOURCE_KEYS:
                source_price = _multi_source_price_for_basis(enriched, source)
                updated_ms = _multi_source_updated_ms_for_basis(enriched, source)
                sample_updated_ms = _multi_source_sample_updated_ms_for_basis(enriched, source)
                samples = self._basis.setdefault(source, [])
                if not source_price or not updated_ms:
                    continue
                age_ms = max(0, now_ms - updated_ms)
                if age_ms > self.settings_max_age_ms:
                    continue
                sample_key = (chainlink_updated_ms, sample_updated_ms or updated_ms)
                if self._last_sample_keys.get(source) == sample_key:
                    continue
                basis_bps = (source_price - chainlink) / chainlink * 10_000.0
                samples.append(basis_bps)
                self._last_sample_keys[source] = sample_key
                if len(samples) > self.max_samples:
                    del samples[: len(samples) - self.max_samples]
                enriched[f"{source}_basis_latest_bps"] = basis_bps
                enriched[f"{source}_basis_median_bps"] = _median(samples)
                enriched[f"{source}_basis_samples"] = len(samples)
        enriched["multi_context"] = multi_source_price_context(enriched, now_ms)
        return enriched

    @property
    def settings_max_age_ms(self) -> int:
        return 4_500


class PaperTradingBot:
    def __init__(self, settings: Settings, store: TradeStore) -> None:
        self.settings = settings
        self.store = store
        self.polymarket = PolymarketClient(settings.gamma_url, settings.clob_url, settings.request_timeout_seconds)
        self.actor_data = PolymarketDataClient(settings.data_api_url, settings.request_timeout_seconds)
        self.price_fallback = PublicPriceClient(settings.request_timeout_seconds)
        backend_market_data_timeout = min(
            settings.request_timeout_seconds,
            max(1.0, (settings.max_quote_age_ms / 1000.0) * BACKEND_MARKET_DATA_REFRESH_RATIO),
        )
        self.market_data_polymarket = PolymarketClient(
            settings.gamma_url,
            settings.clob_url,
            backend_market_data_timeout,
        )
        self.market_data_price_fallback = PublicPriceClient(backend_market_data_timeout)
        self.strategy = RealBtcFiveMinuteStrategy(settings)
        self._lock = threading.Lock()
        self._market_data_refresh_lock = threading.Lock()
        self._price_refresh_lock = threading.Lock()
        self._price_basis_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._market_data_thread: threading.Thread | None = None
        self._clob_ws_thread: threading.Thread | None = None
        self._rtds_ws_thread: threading.Thread | None = None
        self._okx_spot_ws_thread: threading.Thread | None = None
        self._binance_spot_ws_thread: threading.Thread | None = None
        self._price_refresh_thread: threading.Thread | None = None
        self._market_scout_thread: threading.Thread | None = None
        self._market_scout_stop = threading.Event()
        self.last_error: str | None = None
        self.last_tick_at: float | None = None
        self.last_signal: dict[str, Any] | None = None
        self.current_market = None
        self.latest_price: dict[str, Any] = {}
        self.latest_quotes: dict[str, dict[str, Any]] = {}
        self.paper_price: dict[str, Any] = {}
        self.paper_quotes: dict[str, dict[str, Any]] = {}
        self.execution_price: dict[str, Any] = {}
        self.execution_quotes: dict[str, dict[str, Any]] = {}
        # 保存最近一次后端 RTDS Chainlink 价格，避免市场切换或浏览器快照覆盖后丢失锚定价。
        self._latest_backend_chainlink_price: dict[str, Any] = {}
        self._last_live_snapshot_ingest_at = 0.0
        self._last_backend_market_data_refresh_at = 0.0
        self._last_backend_quote_refresh_at = 0.0
        self._last_backend_price_refresh_at = 0.0
        self._live_gate_diagnostics = deque(maxlen=LIVE_DIAGNOSTIC_HISTORY_LIMIT)
        self._tick_profile_history = deque(maxlen=LIVE_TICK_PROFILE_HISTORY_LIMIT)
        self._last_slow_tick_log_at = 0.0
        self._market_llm_terminal_logs = deque(maxlen=MARKET_LLM_TERMINAL_LOG_LIMIT)
        self._market_llm_terminal_seq = 0
        self._market_llm_terminal_last_heartbeat_at = 0.0
        self._market_paper_quote_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._market_scout_link_cache: dict[str, dict[str, str]] = {}
        self.market_evidence_scout = MarketEvidenceScout(settings)
        self.market_scout_settings_path = _resolve_market_scout_settings_path(settings)
        self.market_scout_paper_db_path = _resolve_market_scout_paper_db_path(settings)
        self._market_scout_runtime_settings = self._load_market_scout_runtime_settings()
        self.market_paper_store = TradeStore(
            self.market_scout_paper_db_path,
            float(self._market_scout_runtime_settings["paper_initial_balance"]),
        )
        self._market_scout_status: dict[str, Any] = {
            "state": "idle",
            "message": "等待 Market Scout 启动",
            "scanner_enabled": bool(settings.market_scout_enabled and self._market_scout_runtime_settings["scanner_enabled"]),
            "scanner_running": False,
            "llm_enabled": bool(
                self._market_scout_runtime_settings["llm_enabled"]
                and settings.llm_super_agent_enabled
                and settings.llm_super_agent_api_key
            ),
            "auto_order_enabled": bool(self._market_scout_runtime_settings["paper_auto_enabled"]),
            "paper_auto_enabled": bool(self._market_scout_runtime_settings["paper_auto_enabled"]),
            "live_auto_enabled": False,
            "live_locked": True,
            "last_scan_at": None,
            "last_llm_at": None,
            "candidate_count": 0,
            "analyzed_slug": "",
            "last_error": "",
            "last_auto_order": None,
        }
        self._market_scout_last_candidate_signature = ""
        self._market_scout_last_llm_at = 0.0
        self._market_scout_last_scan_log_at = 0.0
        self._market_scout_last_no_key_log_at = 0.0
        self._official_recheck_next_at: dict[str, float] = {}
        self._official_price_backfill_next_at: dict[str, float] = {}
        self.pair_strategy_enabled = False
        self.pair_stop_loss_streak = 0
        self.last_pair_event: dict[str, Any] | None = None
        self.single_entry_mode = SINGLE_ENTRY_MODE_LEGACY
        self.signal_side_mode = SIGNAL_SIDE_MODE_BASE
        self.signal_filter_mode = SIGNAL_FILTER_MODE_NONE
        self._aggressive_edge_loss_replay = AggressiveEdgeLossReplayRecorder(
            self.settings.db_path.parent / "loss-replays" / SINGLE_AGGRESSIVE_EDGE_REPLAY_FILE,
            variant_id=SINGLE_AGGRESSIVE_EDGE_REPLAY_VARIANT_ID,
            combo=SINGLE_AGGRESSIVE_EDGE_REPLAY_COMBO,
        )
        self.market_data_mode = MARKET_DATA_MODE_BASE
        self.price_source_mode = PRICE_SOURCE_MODE_MIXED
        self.anti_bot_guard_mode = ANTI_BOT_GUARD_MODE_NONE
        self.realtime_maker_enabled = False
        self.llm_super_agent_enabled = False
        self.llm_super_agent_router = LlmSuperAgentRouter(settings)
        self.llm_super_agent_variant_id = "MAIN"
        self._llm_super_agent_last_logged_key: str | None = None
        self.btc_runtime_settings_path = _resolve_btc_runtime_settings_path(settings)
        btc_runtime_payload = self._load_btc_runtime_settings()
        self.btc_runtime_paused = bool(btc_runtime_payload.get("paused", False))
        self.last_btc_runtime_event: dict[str, Any] | None = (
            dict(btc_runtime_payload.get("event"))
            if isinstance(btc_runtime_payload.get("event"), dict)
            else {
                "type": "BTC_RUNTIME_PAUSE" if self.btc_runtime_paused else "BTC_RUNTIME_RESUME",
                "paused": self.btc_runtime_paused,
                "message": BTC_RUNTIME_PAUSED_MESSAGE if self.btc_runtime_paused else BTC_RUNTIME_RUNNING_MESSAGE,
                "at": _maybe_float(btc_runtime_payload.get("updated_at")) or time.time(),
            }
        )
        self.paper_trading_paused = False
        self.last_paper_pause_event: dict[str, Any] | None = None
        self.price_basis_tracker = PriceBasisTracker()
        self.clob_ws_feed = ClobMarketWebSocketFeed(timeout_seconds=settings.request_timeout_seconds)
        self.rtds_chainlink_feed = RtdsChainlinkWebSocketFeed(timeout_seconds=settings.request_timeout_seconds)
        self.okx_spot_ws_feed = SpotPriceWebSocketFeed(
            SPOT_WS_SOURCE_OKX,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.binance_spot_ws_feed = SpotPriceWebSocketFeed(
            SPOT_WS_SOURCE_BINANCE,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self._actor_analysis_cache: dict[str, Any] | None = None
        self._actor_analysis_cache_key: str | None = None
        self._actor_analysis_cache_until = 0.0
        self.ws_status: dict[str, Any] = {
            "market": "waiting",
            "price": "waiting",
            "browser_feed_at": None,
            "backend_rest_fallback_at": None,
            "backend_rtds_ws": "waiting",
            "backend_rtds_ws_at": None,
            "backend_okx_ws": "waiting",
            "backend_okx_ws_at": None,
            "backend_binance_ws": "waiting",
            "backend_binance_ws_at": None,
        }
        self.strategy_experiments = (
            StrategyExperimentRunner(settings, self.polymarket, self.price_fallback)
            if settings.strategy_experiments_enabled
            else None
        )
        self.live_trading = LiveStrategyRunner(settings, self.polymarket) if settings.live_trading_runtime_enabled else None
        self.live_paper_trading = (
            LivePaperStrategyRunner(settings, self.polymarket) if settings.live_trading_runtime_enabled else None
        )
        self.live_paper_stop_win_trading = (
            LivePaperStopWinStrategyRunner(settings, self.polymarket) if settings.live_trading_runtime_enabled else None
        )
        self._append_market_llm_terminal_log(
            level="info",
            module="system",
            event_type="terminal_start",
            title="Market LLM Terminal",
            message="非 BTC 市场日志终端已启动",
            details=[
                f"候选市场扫描: {'已启用' if self._market_scout_effective_scanner_enabled_unlocked() else '已关闭'}",
                f"LLM 实时分析: {'已启用' if self._market_scout_effective_llm_enabled_unlocked() else '未配置或已关闭'}",
                f"LLM 模型: {self._market_scout_runtime_settings.get('llm_model') or self.settings.llm_super_agent_model}",
                f"Evidence Scout: {'已启用' if self._market_scout_runtime_settings.get('evidence_enabled') else '已关闭'}",
                f"Paper 自动下注: {'已启用' if self._market_scout_runtime_settings['paper_auto_enabled'] else '已关闭'}",
                f"Paper 探针: {'已启用' if self._market_scout_runtime_settings.get('paper_probe_enabled') else '已关闭'}",
                f"Live 自动下注: 已锁定，{MARKET_SCOUT_LIVE_LOCKED_MESSAGE}",
            ],
        )

    def configure_strategy_experiment_variant(self, variant: StrategyVariant) -> None:
        """绑定实验组合元数据；版本化组合必须隔离过滤路径和复盘文件。"""

        self.pair_strategy_enabled = variant.strategy_family == STRATEGY_FAMILY_PAIR
        self.realtime_maker_enabled = variant.strategy_family == STRATEGY_FAMILY_REALTIME_MAKER
        self.llm_super_agent_enabled = variant.strategy_family == STRATEGY_FAMILY_LLM_SUPER_AGENT
        self.llm_super_agent_variant_id = variant.variant_id
        self.single_entry_mode = variant.single_entry_mode
        self.signal_side_mode = variant.signal_side_mode
        self.signal_filter_mode = variant.signal_filter_mode
        self.market_data_mode = variant.market_data_mode
        self.price_source_mode = variant.price_source_mode
        self.anti_bot_guard_mode = variant.anti_bot_guard_mode
        if variant.signal_filter_mode in AGGRESSIVE_EDGE_FILTER_MODES:
            replay_file = f"{variant.variant_id.lower()}.jsonl"
            self._aggressive_edge_loss_replay = AggressiveEdgeLossReplayRecorder(
                self.settings.db_path.parent / "loss-replays" / replay_file,
                variant_id=variant.variant_id,
                combo=variant.combo,
            )

    def start(self) -> None:
        self._start_market_scout()
        if self.btc_runtime_is_paused():
            logger.info("BTC 数据业务处于暂停态，后台行情线程不启动")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._market_data_thread = threading.Thread(
            target=self._run_market_data,
            name="polybot2other-market-data-refresh",
            daemon=True,
        )
        self._market_data_thread.start()
        self._clob_ws_thread = threading.Thread(
            target=self._run_clob_ws,
            name="polybot2other-clob-ws",
            daemon=True,
        )
        self._clob_ws_thread.start()
        self._rtds_ws_thread = threading.Thread(
            target=self._run_rtds_chainlink_ws,
            name="polybot2other-rtds-chainlink-ws",
            daemon=True,
        )
        self._rtds_ws_thread.start()
        self._okx_spot_ws_thread = threading.Thread(
            target=self._run_okx_spot_ws,
            name="polybot2other-okx-spot-ws",
            daemon=True,
        )
        self._okx_spot_ws_thread.start()
        self._binance_spot_ws_thread = threading.Thread(
            target=self._run_binance_spot_ws,
            name="polybot2other-binance-spot-ws",
            daemon=True,
        )
        self._binance_spot_ws_thread.start()
        self._thread = threading.Thread(target=self._run, name="polybot2other-real-btc-paper-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._market_scout_stop.set()
        self._stop.set()
        self._join_btc_worker_threads()
        self._join_market_scout_thread()

    def _join_btc_worker_threads(self) -> None:
        current_thread = threading.current_thread()
        if self._thread:
            if self._thread is not current_thread:
                self._thread.join(timeout=3)
        if self._market_data_thread:
            if self._market_data_thread is not current_thread:
                self._market_data_thread.join(timeout=3)
        if self._clob_ws_thread:
            if self._clob_ws_thread is not current_thread:
                self._clob_ws_thread.join(timeout=3)
        if self._rtds_ws_thread:
            if self._rtds_ws_thread is not current_thread:
                self._rtds_ws_thread.join(timeout=3)
        if self._okx_spot_ws_thread:
            if self._okx_spot_ws_thread is not current_thread:
                self._okx_spot_ws_thread.join(timeout=3)
        if self._binance_spot_ws_thread:
            if self._binance_spot_ws_thread is not current_thread:
                self._binance_spot_ws_thread.join(timeout=3)
        if self._price_refresh_thread:
            if self._price_refresh_thread is not current_thread:
                self._price_refresh_thread.join(timeout=3)

    def _join_market_scout_thread(self) -> None:
        current_thread = threading.current_thread()
        if self._market_scout_thread and self._market_scout_thread is not current_thread:
            self._market_scout_thread.join(timeout=3)

    def _start_market_scout(self) -> None:
        if not self._market_scout_effective_scanner_enabled():
            self._set_market_scout_status(
                state="disabled",
                message="Market Scout 已关闭",
                scanner_running=False,
            )
            return
        if self._market_scout_thread and self._market_scout_thread.is_alive():
            return
        self._market_scout_stop.clear()
        self._market_scout_thread = threading.Thread(
            target=self._run_market_scout,
            name="polybot2other-market-llm-scout",
            daemon=True,
        )
        self._market_scout_thread.start()

    def _set_market_scout_status(self, **updates: Any) -> None:
        with self._lock:
            status = dict(self._market_scout_status)
            status.update(updates)
            runtime = self._market_scout_runtime_settings
            status["scanner_enabled"] = bool(self.settings.market_scout_enabled and runtime.get("scanner_enabled", True))
            status["scanner_running"] = bool(
                updates.get(
                    "scanner_running",
                    self._market_scout_thread is not None and self._market_scout_thread.is_alive(),
                )
            )
            status["llm_enabled"] = self._market_scout_effective_llm_enabled_unlocked()
            status["auto_order_enabled"] = bool(runtime.get("paper_auto_enabled", False))
            status["paper_auto_enabled"] = bool(runtime.get("paper_auto_enabled", False))
            status["live_auto_enabled"] = False
            status["live_locked"] = True
            status["settings"] = self._market_scout_public_settings_unlocked()
            self._market_scout_status = status

    def set_pair_strategy_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self.pair_strategy_enabled = bool(enabled)
            self.last_pair_event = {
                "type": "PAIR_TOGGLE",
                "message": "配对策略已开启" if enabled else "配对策略已关闭",
                "at": time.time(),
            }
            if not enabled:
                self.pair_stop_loss_streak = 0
        return self.snapshot()

    def set_btc_runtime_paused(self, paused: bool) -> dict[str, Any]:
        """切换 BTC 数据业务总开关；暂停请求只发停止信号，避免接口被后台网络线程拖慢。"""

        now = time.time()
        self._set_btc_runtime_pause_state(paused, now, persist=True)
        if paused:
            # HTTP 按钮必须快速返回；线程会在各自循环里看到 _stop 和暂停态后退出。
            self._stop.set()
            self._set_btc_runtime_paused_runtime_state(now)
            logger.info("BTC 数据业务已暂停，已发送后台线程停止信号 settings_path=%s", self.btc_runtime_settings_path)
        else:
            self._stop.clear()
            logger.info("BTC 数据业务已恢复，准备启动后台行情线程 settings_path=%s", self.btc_runtime_settings_path)
            self.start()
        return {"btc_runtime": self.btc_runtime()}

    def btc_runtime_is_paused(self) -> bool:
        with self._lock:
            return bool(self.btc_runtime_paused)

    def btc_runtime(self) -> dict[str, Any]:
        with self._lock:
            return self._btc_runtime_payload_locked()

    def _btc_runtime_payload_locked(self) -> dict[str, Any]:
        event = dict(self.last_btc_runtime_event or {})
        paused = bool(self.btc_runtime_paused)
        worker_running = bool(self._thread and self._thread.is_alive())
        market_data_running = bool(self._market_data_thread and self._market_data_thread.is_alive())
        return {
            "paused": paused,
            "message": event.get("message") or (BTC_RUNTIME_PAUSED_MESSAGE if paused else BTC_RUNTIME_RUNNING_MESSAGE),
            "updated_at": event.get("at"),
            "event": event,
            "settings_path": str(self.btc_runtime_settings_path),
            "worker_running": worker_running,
            "market_data_running": market_data_running,
        }

    def btc_runtime_paused_snapshot_response(self) -> dict[str, Any]:
        now = time.time()
        return {
            "ok": True,
            "ignored_snapshot": "btc_runtime_paused",
            "paused": True,
            "message": BTC_RUNTIME_PAUSED_REASON,
            "btc_runtime": self.btc_runtime(),
            "updated_at": now,
        }

    def _btc_runtime_stop_requested(self) -> bool:
        """主 tick 内部快速刹车；暂停或关服信号出现后跳过后续策略和实盘动作。"""

        if self._stop.is_set():
            if self.btc_runtime_is_paused():
                self._set_btc_runtime_paused_runtime_state(time.time())
            return True
        if self.btc_runtime_is_paused():
            self._set_btc_runtime_paused_runtime_state(time.time())
            return True
        return False

    def _set_btc_runtime_pause_state(self, paused: bool, now: float, *, persist: bool) -> None:
        message = BTC_RUNTIME_PAUSED_MESSAGE if paused else BTC_RUNTIME_RUNNING_MESSAGE
        event = {
            "type": "BTC_RUNTIME_PAUSE" if paused else "BTC_RUNTIME_RESUME",
            "paused": bool(paused),
            "message": message,
            "at": now,
        }
        with self._lock:
            self.btc_runtime_paused = bool(paused)
            self.last_btc_runtime_event = event
        if persist:
            self._save_btc_runtime_settings(event)

    def _load_btc_runtime_settings(self) -> dict[str, Any]:
        path = self.btc_runtime_settings_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 BTC 运行态配置失败 path=%s error=%s", path, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_btc_runtime_settings(self, event: dict[str, Any]) -> None:
        payload = {
            "paused": bool(event.get("paused")),
            "updated_at": _maybe_float(event.get("at")) or time.time(),
            "event": dict(event),
        }
        path = self.btc_runtime_settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _set_btc_runtime_paused_runtime_state(self, now: float) -> None:
        with self._lock:
            self.last_signal = {
                "symbol": "BTC",
                "side": "NO_TRADE",
                "confidence": 0.0,
                "entry_price": 0.0,
                "move_bps": 0.0,
                "reason": BTC_RUNTIME_PAUSED_REASON,
            }
            self.ws_status.update(
                {
                    "market": "paused",
                    "price": "paused",
                    "backend_clob_ws": "paused",
                    "backend_clob_ws_at": now,
                    "backend_rtds_ws": "paused",
                    "backend_rtds_ws_at": now,
                    "backend_okx_ws": "paused",
                    "backend_okx_ws_at": now,
                    "backend_binance_ws": "paused",
                    "backend_binance_ws_at": now,
                    "backend_market_data_loop_at": now,
                }
            )
            self.last_error = None
            self.last_tick_at = now

    def set_paper_trading_paused(self, paused: bool) -> dict[str, Any]:
        now = time.time()
        self._set_paper_pause_state(paused, now)
        main_cancel = (
            self._cancel_active_paper_orders_for_pause(now)
            if paused
            else {"canceled": [], "released_cash": 0.0, "orders": []}
        )
        experiment_cancel = (
            self.strategy_experiments.set_paper_trading_paused(paused, cancel_active=paused, now=now)
            if self.strategy_experiments is not None
            else {"canceled_count": 0, "released_cash": 0.0, "variants": {}}
        )
        if paused:
            self._set_paper_paused_signal()
        return {
            "paper_trading": {
                **self.paper_trading_runtime(),
                "main_cancel": _paper_cancel_summary(main_cancel),
                "strategy_experiments_cancel": experiment_cancel,
            },
            "snapshot": self.snapshot(),
        }

    def _set_paper_pause_state(self, paused: bool, now: float) -> None:
        message = "Paper 下单已暂停" if paused else "Paper 下单已恢复"
        with self._lock:
            self.paper_trading_paused = bool(paused)
            self.last_paper_pause_event = {
                "type": "PAPER_PAUSE" if paused else "PAPER_RESUME",
                "paused": bool(paused),
                "message": message,
                "at": now,
            }

    def _cancel_active_paper_orders_for_pause(self, now: float) -> dict[str, Any]:
        return self.store.cancel_active_paper_orders(
            symbol="BTC",
            reason=PAPER_PAUSE_CANCEL_REASON,
            now=now,
        )

    def paper_trading_runtime(self) -> dict[str, Any]:
        with self._lock:
            event = dict(self.last_paper_pause_event or {})
            paused = bool(self.paper_trading_paused)
        return {
            "paused": paused,
            "message": event.get("message") or ("Paper 下单已暂停" if paused else "Paper 下单运行中"),
            "updated_at": event.get("at"),
            "event": event,
        }

    def _append_market_llm_terminal_log(
        self,
        *,
        level: str,
        module: str,
        event_type: str,
        title: str,
        message: str,
        details: list[str] | None = None,
        code: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """追加非 BTC 市场终端日志；后续扫描器和 LLM 分析器统一写这里。"""

        at = time.time() if now is None else float(now)
        normalized_level = str(level or "info").strip().lower()
        if normalized_level not in {"info", "pass", "warn", "error"}:
            normalized_level = "info"
        event = {
            "seq": 0,
            "at": at,
            "at_ms": int(at * 1000),
            "level": normalized_level,
            "module": str(module or "market").strip() or "market",
            "event_type": str(event_type or "log").strip() or "log",
            "title": str(title or "市场日志"),
            "message": str(message or ""),
            "details": [str(item) for item in (details or []) if str(item or "").strip()],
            "code": str(code or ""),
        }
        with self._lock:
            self._market_llm_terminal_seq += 1
            event["seq"] = self._market_llm_terminal_seq
            self._market_llm_terminal_logs.append(event)
        return dict(event)

    def _append_market_llm_terminal_heartbeat(self, now: float) -> None:
        with self._lock:
            if now - self._market_llm_terminal_last_heartbeat_at < MARKET_LLM_TERMINAL_HEARTBEAT_SECONDS:
                return
            self._market_llm_terminal_last_heartbeat_at = now
            status = dict(self._market_scout_status)
        self._append_market_llm_terminal_log(
            level="info",
            module="operator",
            event_type="heartbeat",
            title="运行心跳",
            message=str(status.get("message") or "市场终端在线"),
            details=[
                f"候选市场扫描: {'运行中' if status.get('scanner_running') else ('已启用' if status.get('scanner_enabled') else '已关闭')}",
                f"LLM 实时分析: {'已启用' if status.get('llm_enabled') else '未配置或已关闭'}",
                f"LLM 模型: {status.get('settings', {}).get('llm_model') or self.settings.llm_super_agent_model}",
                f"Evidence Scout: {'已启用' if status.get('settings', {}).get('evidence_enabled') else '已关闭'}",
                f"Paper 自动下注: {'已启用' if status.get('paper_auto_enabled') else '已关闭'}",
                f"Paper 探针: {'已启用' if status.get('settings', {}).get('paper_probe_enabled') else '已关闭'}",
                f"Live 自动下注: 已锁定，{MARKET_SCOUT_LIVE_LOCKED_MESSAGE}",
            ],
            now=now,
        )

    def market_llm_terminal(self, *, limit: int = 80, after_seq: int = 0) -> dict[str, Any]:
        """返回市场页 LLM 终端日志；只读接口，不触发下单。"""

        now = time.time()
        self._append_market_llm_terminal_heartbeat(now)
        safe_limit = max(1, min(200, int(limit)))
        safe_after_seq = max(0, int(after_seq or 0))
        with self._lock:
            latest_seq = int(self._market_llm_terminal_seq)
            rows = [dict(row) for row in self._market_llm_terminal_logs if int(row.get("seq") or 0) > safe_after_seq]
            status = dict(self._market_scout_status)
        rows = rows[-safe_limit:]
        return {
            "ok": True,
            "updated_at": now,
            "latest_seq": latest_seq,
            "logs": rows,
            "status": status,
        }

    def market_scout_state(
        self,
        *,
        order_limit: int = 30,
        order_offset: int = 0,
        quote_refresh: bool = False,
    ) -> dict[str, Any]:
        """市场页只读状态；包含独立配置、Paper 账户、推荐、订单和实盘锁定说明。"""

        safe_limit = max(1, min(100, int(order_limit)))
        safe_offset = max(0, int(order_offset))
        force_quote_refresh = bool(quote_refresh)
        with self._lock:
            status = dict(self._market_scout_status)
            runtime_settings = self._market_scout_public_settings_unlocked()
        total_orders = self.market_paper_store.paper_order_count(MARKET_SCOUT_SYMBOL, "all")
        recent_orders = self.market_paper_store.recent_paper_orders(
            safe_limit,
            safe_offset,
            MARKET_SCOUT_SYMBOL,
            "all",
        )
        recent_trades = self.market_paper_store.recent_trades(20, 0, MARKET_SCOUT_SYMBOL)
        loaded = min(total_orders, safe_offset + len(recent_orders))
        return {
            "ok": True,
            "updated_at": time.time(),
            "status": status,
            "settings": runtime_settings,
            "paper_account": self.market_paper_store.metrics(),
            "paper_orders": self._decorate_market_paper_orders(
                recent_orders,
                force_quote_refresh=force_quote_refresh,
            ),
            "paper_orders_meta": {
                "limit": safe_limit,
                "offset": safe_offset,
                "loaded": loaded,
                "total": total_orders,
                "has_more": loaded < total_orders,
            },
            "paper_trades": self._decorate_recent_trades(recent_trades),
            "paper_order_summary": self.market_paper_store.paper_order_summary(MARKET_SCOUT_SYMBOL),
            "live": {
                "enabled": False,
                "locked": True,
                "auto_enabled": False,
                "message": MARKET_SCOUT_LIVE_LOCKED_MESSAGE,
                "orders": [],
            },
        }

    def market_scout_settings(self) -> dict[str, Any]:
        return self.market_scout_state()

    def update_market_scout_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """更新市场页运行配置；实盘自动下注保持锁定，所有真钱动作需要单独开发确认。"""

        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        now = time.time()
        with self._lock:
            current = dict(self._market_scout_runtime_settings)
            next_settings = _sanitize_market_scout_runtime_settings(payload, current, self.settings, now=now)
            previous_initial = float(current.get("paper_initial_balance") or self.settings.market_scout_default_paper_initial_balance)
            next_initial = float(next_settings.get("paper_initial_balance") or previous_initial)
            self._market_scout_runtime_settings = next_settings
            self._save_market_scout_runtime_settings(next_settings)
        self._set_market_scout_status(
            message="市场页配置已更新",
            scanner_enabled=bool(self.settings.market_scout_enabled and next_settings.get("scanner_enabled")),
        )
        if abs(previous_initial - next_initial) >= 0.000001:
            self.market_paper_store.rebase_initial_balance(next_initial)
        if self._market_scout_effective_scanner_enabled():
            self._start_market_scout()
        self._append_market_llm_terminal_log(
            level="info",
            module="settings",
            event_type="settings_update",
            title="市场配置更新",
            message="市场页控制参数已保存",
            details=[
                f"扫描: {'开启' if next_settings.get('scanner_enabled') else '关闭'}",
                f"LLM: {'开启' if next_settings.get('llm_enabled') else '关闭'}",
                f"LLM 模型: {next_settings.get('llm_model') or self.settings.llm_super_agent_model}",
                f"Paper 自动下注: {'开启' if next_settings.get('paper_auto_enabled') else '关闭'}",
                f"Paper 探针: {'开启' if next_settings.get('paper_probe_enabled') else '关闭'}",
                f"探针持仓上限: {int(next_settings.get('paper_probe_max_open_positions') or 1)}",
                f"Live 自动下注: 已锁定，{MARKET_SCOUT_LIVE_LOCKED_MESSAGE}",
                f"Paper 初始资金: {next_initial:.2f}",
                f"单笔预算: {float(next_settings.get('paper_stake_dollars') or 0.0):.2f}",
            ],
            now=now,
        )
        return self.market_scout_state()

    def _market_scout_effective_scanner_enabled(self) -> bool:
        with self._lock:
            return self._market_scout_effective_scanner_enabled_unlocked()

    def _market_scout_effective_scanner_enabled_unlocked(self) -> bool:
        return bool(self.settings.market_scout_enabled and self._market_scout_runtime_settings.get("scanner_enabled", True))

    def _market_scout_runtime_llm_enabled(self) -> bool:
        with self._lock:
            return bool(self._market_scout_runtime_settings.get("llm_enabled", True))

    def _market_scout_effective_llm_enabled_unlocked(self) -> bool:
        return bool(
            self._market_scout_runtime_settings.get("llm_enabled", True)
            and self.settings.llm_super_agent_enabled
            and self.settings.llm_super_agent_api_key
        )

    def _market_scout_llm_model(self) -> str:
        """返回市场页当前使用的 LLM 模型；页面配置为空时回退环境变量模型。"""

        with self._lock:
            raw_model = self._market_scout_runtime_settings.get("llm_model")
        return _sanitize_market_scout_model(raw_model, self.settings.llm_super_agent_model)

    def _market_scout_public_settings_unlocked(self) -> dict[str, Any]:
        settings = dict(self._market_scout_runtime_settings)
        settings["live_auto_enabled"] = False
        settings["live_locked"] = True
        settings["live_locked_message"] = MARKET_SCOUT_LIVE_LOCKED_MESSAGE
        settings["settings_path"] = str(self.market_scout_settings_path)
        settings["paper_db_path"] = str(self.market_scout_paper_db_path)
        return settings

    def _load_market_scout_runtime_settings(self) -> dict[str, Any]:
        defaults = _default_market_scout_runtime_settings(self.settings, now=time.time())
        path = self.market_scout_settings_path if hasattr(self, "market_scout_settings_path") else self.settings.market_scout_settings_path
        if not path.exists():
            return defaults
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取市场页运行配置失败 path=%s error=%s", path, exc)
            return defaults
        if not isinstance(payload, dict):
            return defaults
        return _sanitize_market_scout_runtime_settings(payload, defaults, self.settings, now=time.time())

    def _save_market_scout_runtime_settings(self, payload: dict[str, Any]) -> None:
        path = self.market_scout_settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _maybe_execute_market_scout_paper_order(
        self,
        decision: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            runtime_settings = dict(self._market_scout_runtime_settings)
        if not runtime_settings.get("paper_auto_enabled"):
            result = {
                "submitted": False,
                "reason": "Paper 自动下注开关关闭",
                "at": now,
            }
            self._set_market_scout_status(last_auto_order=result)
            return result
        result = self._build_market_scout_paper_order_result(decision, candidates, runtime_settings, now)
        self._set_market_scout_status(last_auto_order=result)
        if result.get("submitted"):
            is_probe = bool(result.get("probe"))
            skip_details = [
                f"候选跳过: {item}"
                for item in list(result.get("probe_skip_reasons") or [])[:MARKET_SCOUT_ORDER_SKIP_LOG_LIMIT]
            ]
            self._append_market_llm_terminal_log(
                level="warn" if is_probe else "pass",
                module="paper",
                event_type="paper_order_submitted",
                title="Paper 探针下注已执行" if is_probe else "Paper 自动下注已执行",
                message=(
                    f"探针 {result.get('outcome')} @ {result.get('entry_price')} stake={result.get('stake')}"
                    if is_probe
                    else f"{result.get('outcome')} @ {result.get('entry_price')} stake={result.get('stake')}"
                ),
                details=[
                    f"市场: {result.get('question') or result.get('slug')}",
                    f"订单ID: {result.get('order_id')}",
                    f"持仓ID: {result.get('trade_ids')}",
                    f"来源: {'LLM NO_TRADE 探针放行' if is_probe else 'LLM RECOMMEND'}",
                    f"原因: {result.get('reason')}",
                    *skip_details,
                ],
                now=now,
            )
        else:
            is_probe = bool(result.get("probe"))
            skip_details = [
                f"候选跳过: {item}"
                for item in list(result.get("probe_skip_reasons") or [])[:MARKET_SCOUT_ORDER_SKIP_LOG_LIMIT]
            ]
            open_details = [
                f"当前持仓: {item}"
                for item in list(result.get("open_position_summary") or [])[:MARKET_SCOUT_ORDER_SKIP_LOG_LIMIT]
            ]
            self._append_market_llm_terminal_log(
                level="warn",
                module="paper",
                event_type="paper_order_blocked",
                title="Paper 探针未执行" if is_probe else "Paper 自动下注未执行",
                message=str(result.get("reason") or "未知拦截"),
                details=[
                    f"市场: {result.get('question') or result.get('slug') or '-'}",
                    f"方向: {result.get('outcome') or '-'}",
                    f"置信度: {_format_optional_float(result.get('confidence'), 4)}",
                    f"入场价: {_format_optional_float(result.get('entry_price'), 4)}",
                    f"来源: {'LLM NO_TRADE 探针链路' if is_probe else result.get('source_decision') or result.get('decision') or '-'}",
                    *skip_details,
                    *open_details,
                ],
                now=now,
            )
        return result

    def _market_scout_open_position_context(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """汇总市场页 Paper 未结算暴露，供候选递补和同事件族去重使用。"""

        candidate_by_slug = {str(candidate.get("slug") or ""): candidate for candidate in candidates}
        open_slugs: set[str] = set()
        open_event_keys: set[str] = set()
        open_source_event_keys: set[str] = set()
        summaries: list[str] = []
        for row in self.market_paper_store.open_trades():
            if str(row.get("symbol") or "") != MARKET_SCOUT_SYMBOL:
                continue
            slug = str(row.get("round_id") or "").strip()
            if not slug:
                continue
            open_slugs.add(slug)
            candidate = candidate_by_slug.get(slug) or {
                "slug": slug,
                "question": row.get("question") or "",
                "condition_id": row.get("condition_id") or "",
                "event_title": row.get("question") or "",
            }
            event_key = str(candidate.get("event_key") or _market_scout_event_key(candidate)).strip().lower()
            source_event_key = str(candidate.get("source_event_key") or _market_scout_source_event_key(candidate)).strip().lower()
            if event_key:
                open_event_keys.add(event_key)
            if source_event_key:
                open_source_event_keys.add(source_event_key)
            summaries.append(
                f"{slug} side={row.get('side') or '-'} stake={_format_optional_float(row.get('stake'), 2)} "
                f"event={_market_scout_compact_key(event_key or source_event_key)}"
            )
        return {
            "open_count": len(open_slugs),
            "open_slugs": open_slugs,
            "open_event_keys": open_event_keys,
            "open_source_event_keys": open_source_event_keys,
            "summaries": summaries,
        }

    def _build_market_scout_paper_order_result(
        self,
        decision: dict[str, Any],
        candidates: list[dict[str, Any]],
        runtime_settings: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        probe_mode = False
        order_decision = dict(decision)
        exposure_context = self._market_scout_open_position_context(candidates)
        if order_decision.get("decision") != "RECOMMEND":
            probe_max_open_positions = int(runtime_settings.get("paper_probe_max_open_positions") or 1)
            if int(exposure_context.get("open_count") or 0) >= probe_max_open_positions:
                return {
                    "submitted": False,
                    "mode": "PAPER",
                    "decision": decision.get("decision"),
                    "source_decision": decision.get("decision"),
                    "slug": decision.get("selected_slug") or "",
                    "question": decision.get("question") or "",
                    "outcome": decision.get("outcome") or "",
                    "confidence": decision.get("confidence"),
                    "entry_price": None,
                    "stake": runtime_settings.get("paper_stake_dollars"),
                    "at": now,
                    "probe": True,
                    "reason": f"Paper 探针持仓数已达上限 {probe_max_open_positions}",
                    "open_position_summary": list(exposure_context.get("summaries") or []),
                    "probe_skip_reasons": [],
                }
            probe_decision, probe_block_reason, probe_skip_reasons = _market_scout_build_probe_decision(
                order_decision,
                candidates,
                runtime_settings,
                exposure_context=exposure_context,
            )
            if not probe_decision:
                return {
                    "submitted": False,
                    "mode": "PAPER",
                    "decision": decision.get("decision"),
                    "slug": decision.get("selected_slug") or "",
                    "question": decision.get("question") or "",
                    "outcome": decision.get("outcome") or "",
                    "confidence": decision.get("confidence"),
                    "entry_price": None,
                    "stake": runtime_settings.get("paper_stake_dollars"),
                    "at": now,
                    "probe": False,
                    "reason": probe_block_reason or "LLM 未给出 RECOMMEND",
                    "open_position_summary": list(exposure_context.get("summaries") or []),
                    "probe_skip_reasons": probe_skip_reasons,
                }
            order_decision = probe_decision
            probe_mode = True
        base_result = {
            "submitted": False,
            "mode": "PAPER",
            "decision": "PROBE" if probe_mode else order_decision.get("decision"),
            "source_decision": decision.get("decision"),
            "slug": order_decision.get("selected_slug") or "",
            "question": order_decision.get("question") or "",
            "outcome": order_decision.get("outcome") or "",
            "confidence": order_decision.get("confidence"),
            "entry_price": None,
            "stake": runtime_settings.get("paper_stake_dollars"),
            "at": now,
            "probe": probe_mode,
            "probe_reason": order_decision.get("probe_reason") or "",
            "probe_skip_reasons": list(order_decision.get("probe_skip_reasons") or []),
            "open_position_summary": list(exposure_context.get("summaries") or []),
        }
        candidate_by_slug = {str(candidate.get("slug") or ""): candidate for candidate in candidates}
        slug = str(order_decision.get("selected_slug") or "").strip()
        candidate = candidate_by_slug.get(slug)
        if not candidate:
            return {**base_result, "reason": "推荐市场不在本轮候选列表"}
        outcome = str(order_decision.get("outcome") or "").strip()
        outcome_index = _market_scout_outcome_index(candidate, outcome)
        if outcome_index < 0:
            return {**base_result, "question": candidate.get("question"), "reason": "推荐方向不在市场 outcomes 中"}
        quote = _market_scout_outcome_quote(candidate, outcome)
        entry_price = _market_scout_entry_price(candidate, outcome_index, quote)
        spread = _maybe_float(quote.get("spread")) if isinstance(quote, dict) else _maybe_float(candidate.get("spread"))
        result = {
            **base_result,
            "question": candidate.get("question"),
            "url": candidate.get("url"),
            "outcome": outcome,
            "entry_price": entry_price,
            "spread": spread,
        }
        confidence = _maybe_float(order_decision.get("confidence")) or 0.0
        min_confidence = float(
            runtime_settings.get("paper_probe_min_confidence" if probe_mode else "min_confidence")
            or 0.0
        )
        if confidence < min_confidence:
            return {**result, "reason": f"置信度 {confidence:.4f} 低于阈值 {min_confidence:.4f}"}
        if entry_price is None:
            return {**result, "reason": "缺少可执行买入价"}
        decision_max_entry = _maybe_float(order_decision.get("max_entry_price"))
        configured_max_entry = float(runtime_settings.get("max_entry_price") or 0.99)
        max_entry = min(configured_max_entry, decision_max_entry if decision_max_entry is not None else configured_max_entry)
        if entry_price > max_entry + 0.000001:
            return {**result, "reason": f"入场价 {entry_price:.4f} 高于上限 {max_entry:.4f}"}
        max_spread = float(runtime_settings.get("max_spread") or 1.0)
        if spread is not None and spread > max_spread:
            return {**result, "reason": f"盘口价差 {spread:.4f} 高于上限 {max_spread:.4f}"}
        exposure_block = _market_scout_candidate_exposure_block(candidate, exposure_context)
        if exposure_block:
            return {**result, "reason": exposure_block}
        max_open_positions = int(
            runtime_settings.get("paper_probe_max_open_positions" if probe_mode else "paper_max_open_positions")
            or 1
        )
        if int(exposure_context.get("open_count") or 0) >= max_open_positions:
            return {**result, "reason": f"Paper 持仓数已达上限 {max_open_positions}"}
        round_id = str(candidate.get("slug") or "").strip()
        if self.market_paper_store.open_trade_exists_for_round(round_id):
            return {**result, "reason": "该市场已有 Paper 持仓"}
        max_daily_loss = float(runtime_settings.get("paper_max_daily_loss") or 0.0)
        daily_pnl = self.market_paper_store.daily_realized_pnl()
        if max_daily_loss > 0 and daily_pnl <= -max_daily_loss:
            return {**result, "reason": f"Paper 24小时已实现亏损 {daily_pnl:.4f} 触达上限 {max_daily_loss:.4f}"}
        stake = round(float(runtime_settings.get("paper_stake_dollars") or 0.0), 6)
        account = self.market_paper_store.account()
        if stake <= 0:
            return {**result, "reason": "单笔预算无效"}
        if float(account.get("cash_balance") or 0.0) + 0.000001 < stake:
            return {**result, "reason": "Paper 隔离账户可用资金不足"}
        market = _market_scout_market_round(candidate, outcome_index, now)
        signal = Signal(
            symbol=MARKET_SCOUT_SYMBOL,
            side=outcome,
            confidence=confidence,
            entry_price=entry_price,
            move_bps=0.0,
            reason=_market_scout_order_reason(order_decision, probe_mode)[:900],
        )
        intent = TradeIntent(market=market, signal=signal, stake_dollars=stake)
        execution_quote = dict(quote) if isinstance(quote, dict) else {}
        if not execution_quote.get("best_ask"):
            execution_quote["best_ask"] = entry_price
        if not execution_quote.get("ask_size"):
            execution_quote["ask_size"] = max(stake / max(entry_price, 0.01) * 1.2, stake)
        execution = simulate_fak_buy(
            intent,
            execution_quote,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
            limit_price=entry_price,
        )
        self.market_paper_store.upsert_round(market)
        trade_ids = self.market_paper_store.place_execution_result(intent, execution)
        order_id = None
        recent = self.market_paper_store.recent_paper_orders(1, 0, MARKET_SCOUT_SYMBOL, "all")
        if recent:
            order_id = recent[0].get("id")
        submitted = bool(trade_ids)
        return {
            **result,
            "submitted": submitted,
            "order_id": order_id,
            "trade_ids": trade_ids,
            "stake": stake,
            "reason": execution.reason if submitted else f"执行模拟未成交: {execution.reason}",
            "status": execution.status,
        }

    def _run_market_scout(self) -> None:
        self._set_market_scout_status(
            state="running",
            message="Market Scout 已启动，准备扫描非 BTC 市场",
            scanner_running=True,
        )
        self._append_market_llm_terminal_log(
            level="info",
            module="scanner",
            event_type="scanner_start",
            title="扫描器启动",
            message="开始只读扫描非 BTC 活跃市场",
            details=[
                f"扫描数量上限: {self.settings.market_scout_scan_limit}",
                f"LLM 分析候选数: {self._market_scout_runtime_settings.get('analyze_top_n')}",
                f"LLM 模型: {self._market_scout_llm_model()}",
                f"Evidence Scout: {'已启用' if self._market_scout_runtime_settings.get('evidence_enabled') else '已关闭'}",
                f"Paper 自动下注: {'已启用' if self._market_scout_runtime_settings.get('paper_auto_enabled') else '已关闭'}",
                f"Paper 探针: {'已启用' if self._market_scout_runtime_settings.get('paper_probe_enabled') else '已关闭'}",
                f"Live 自动下注: 已锁定，{MARKET_SCOUT_LIVE_LOCKED_MESSAGE}",
            ],
        )
        while not self._market_scout_stop.is_set():
            try:
                if self._market_scout_effective_scanner_enabled():
                    self._run_market_scout_once()
                else:
                    self._set_market_scout_status(
                        state="disabled",
                        message="Market Scout 已通过市场页控制关闭",
                        scanner_running=True,
                    )
            except Exception as exc:  # noqa: BLE001 - 扫描线程不能因为单轮失败退出。
                now = time.time()
                error = f"{type(exc).__name__}: {exc}"
                self._set_market_scout_status(
                    state="error",
                    message="Market Scout 本轮扫描失败",
                    scanner_running=True,
                    last_error=error,
                    last_scan_at=now,
                )
                self._append_market_llm_terminal_log(
                    level="error",
                    module="scanner",
                    event_type="scan_error",
                    title="扫描失败",
                    message=error,
                    now=now,
                )
            with self._lock:
                interval = max(5.0, float(self._market_scout_runtime_settings.get("scan_interval_seconds") or self.settings.market_scout_interval_seconds))
            self._market_scout_stop.wait(interval)
        self._set_market_scout_status(
            state="stopped",
            message="Market Scout 已停止",
            scanner_running=False,
        )

    def _run_market_scout_once(self) -> None:
        now = time.time()
        scan_limit = max(20, min(500, int(self.settings.market_scout_scan_limit)))
        self._set_market_scout_status(
            state="scanning",
            message=f"正在扫描非 BTC 市场 limit={scan_limit}",
            scanner_running=True,
            last_error="",
        )
        rows = self.polymarket.get_active_markets(limit=scan_limit, order="volume24hr", ascending=False)
        candidates: list[dict[str, Any]] = []
        reject_counts: Counter[str] = Counter()
        reject_examples: dict[str, str] = {}
        for row in rows:
            candidate = _market_scout_candidate_from_raw(row, now=now)
            if candidate is None:
                reject_counts["parse_failed"] += 1
                continue
            reject_reason = _market_scout_reject_reason(candidate, self.settings)
            if reject_reason:
                reject_counts[reject_reason] += 1
                reject_examples.setdefault(reject_reason, str(candidate.get("question") or candidate.get("slug") or "")[:120])
                continue
            candidates.append(candidate)
        candidates.sort(key=lambda item: _maybe_float(item.get("score")) or 0.0, reverse=True)
        prepared_candidates = [
            _market_scout_prepare_candidate_for_selection(candidate, base_rank=index)
            for index, candidate in enumerate(candidates, start=1)
        ]
        prepared_candidates.sort(key=lambda item: _maybe_float(item.get("selection_score")) or 0.0, reverse=True)
        with self._lock:
            runtime_settings = dict(self._market_scout_runtime_settings)
            top_n = max(1, min(20, int(runtime_settings.get("analyze_top_n") or self.settings.market_scout_analyze_top_n)))
        display_candidates = prepared_candidates[:MARKET_SCOUT_DISPLAY_CANDIDATE_LIMIT]
        llm_seed_candidates = _market_scout_select_llm_candidates(prepared_candidates, top_n)
        llm_candidates = [self._market_scout_enrich_quotes(candidate) for candidate in llm_seed_candidates]
        llm_candidates, evidence_report = self._market_scout_enrich_evidence(llm_candidates, runtime_settings, now)
        llm_rank_by_slug = {str(candidate.get("slug") or ""): index for index, candidate in enumerate(llm_candidates, start=1)}
        evidence_by_slug = {
            str(candidate.get("slug") or ""): candidate.get("evidence")
            for candidate in llm_candidates
            if candidate.get("evidence")
        }
        display_candidates = [
            _market_scout_attach_candidate_evidence(
                _market_scout_mark_llm_selection(candidate, llm_rank_by_slug),
                evidence_by_slug,
            )
            for candidate in display_candidates
        ]
        llm_candidates = [
            _market_scout_mark_llm_selection(candidate, llm_rank_by_slug)
            for candidate in llm_candidates
        ]
        self._set_market_scout_status(
            state="scanned",
            message=f"扫描完成，候选 {len(candidates)} 个，送 LLM {len(llm_candidates)} 个",
            scanner_running=True,
            last_scan_at=now,
            candidate_count=len(candidates),
            display_candidate_count=len(display_candidates),
            llm_candidate_count=len(llm_candidates),
            evidence=evidence_report,
            rejected=dict(reject_counts),
            top_candidates=[_market_scout_candidate_public(candidate) for candidate in display_candidates],
            llm_candidates=[_market_scout_candidate_public(candidate) for candidate in llm_candidates],
        )
        self._append_market_llm_terminal_log(
            level="pass" if llm_candidates else "warn",
            module="scanner",
            event_type="scan_complete",
            title="候选扫描完成",
            message=f"扫描 {len(rows)} 个市场，保留 {len(candidates)} 个候选，展示 {len(display_candidates)} 个，送 LLM {len(llm_candidates)} 个",
            details=_market_scout_scan_details(display_candidates, reject_counts, reject_examples, llm_candidates=llm_candidates),
            now=now,
        )
        if not llm_candidates:
            return
        self._maybe_analyze_market_scout_candidates(llm_candidates, now)

    def _market_scout_enrich_evidence(
        self,
        candidates: list[dict[str, Any]],
        runtime_settings: dict[str, Any],
        now: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """在 LLM 前补充英文新闻证据；失败会写日志并保留原候选。"""

        if not candidates:
            return candidates, {
                "enabled": bool(runtime_settings.get("evidence_enabled")),
                "searched_count": 0,
                "ok_count": 0,
                "error_count": 0,
                "no_result_count": 0,
                "skipped_count": 0,
                "details": [],
            }
        if runtime_settings.get("evidence_enabled", True):
            self._set_market_scout_status(
                state="evidence",
                message=f"正在检索 Web 证据 {min(len(candidates), int(runtime_settings.get('evidence_max_markets') or 0))} 个市场",
                scanner_running=True,
            )
        started = time.perf_counter()
        enriched, report = self.market_evidence_scout.enrich_candidates(
            candidates,
            runtime_settings,
            now=now,
        )
        report = dict(report)
        report["elapsed_ms"] = round(_elapsed_ms(started), 3)
        if report.get("enabled"):
            self._append_market_llm_terminal_log(
                level="pass" if int(report.get("ok_count") or 0) else "warn",
                module="evidence",
                event_type="evidence_search",
                title="Web 证据检索完成",
                message=(
                    f"检索 {report.get('searched_count')} 个市场，"
                    f"命中 {report.get('ok_count')} 个，空结果 {report.get('no_result_count')} 个，错误 {report.get('error_count')} 个"
                ),
                details=[
                    f"搜索入口: {report.get('provider')}",
                    f"耗时: {_format_optional_float(report.get('elapsed_ms'), 1)}ms",
                    *[str(item) for item in list(report.get("details") or [])[:8]],
                ],
                now=now,
            )
        return enriched, report

    def _market_scout_enrich_quotes(self, candidate: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(candidate)
        outcomes = list(enriched.get("outcomes") or [])
        token_ids = list(enriched.get("token_ids") or [])
        quotes: dict[str, dict[str, Any]] = {}
        for outcome, token_id in zip(outcomes, token_ids):
            if not token_id:
                continue
            try:
                quote = self.polymarket.get_quote(str(token_id), str(outcome)).to_dict()
            except Exception as exc:  # noqa: BLE001 - 单个盘口失败只降低该候选质量。
                quotes[str(outcome)] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            quotes[str(outcome)] = {
                "best_bid": quote.get("best_bid"),
                "best_ask": quote.get("best_ask"),
                "bid_size": quote.get("bid_size"),
                "ask_size": quote.get("ask_size"),
                "spread": _spread_from_quote_payload(quote),
                "updated_at_ms": quote.get("updated_at_ms"),
            }
        if quotes:
            enriched["quotes"] = quotes
        return enriched

    def _maybe_analyze_market_scout_candidates(self, candidates: list[dict[str, Any]], now: float) -> None:
        if not self._market_scout_runtime_llm_enabled():
            self._set_market_scout_status(state="scanned", message="扫描完成，市场页 LLM 开关已关闭", scanner_running=True)
            self._append_market_scout_llm_unavailable(now, "市场页 LLM 开关已关闭")
            return
        if not self.settings.llm_super_agent_enabled:
            self._set_market_scout_status(state="scanned", message="扫描完成，LLM 配置已关闭", scanner_running=True)
            self._append_market_scout_llm_unavailable(now, "LLM 配置已关闭")
            return
        if not self.settings.llm_super_agent_api_key:
            self._set_market_scout_status(state="scanned", message="扫描完成，LLM API key 未配置", scanner_running=True)
            self._append_market_scout_llm_unavailable(now, "LLM API key 未配置")
            return
        signature = _market_scout_candidate_signature(candidates)
        if signature == self._market_scout_last_candidate_signature and now - self._market_scout_last_llm_at < self.settings.market_scout_llm_ttl_seconds:
            self._set_market_scout_status(
                state="scanned",
                message="候选未变化，沿用上一轮 LLM 分析",
                scanner_running=True,
            )
            return
        self._set_market_scout_status(
            state="analyzing",
            message=f"正在调用 LLM 分析 {len(candidates)} 个候选",
            scanner_running=True,
        )
        self._append_market_llm_terminal_log(
            level="info",
            module="llm",
            event_type="llm_request",
            title="LLM 分析开始",
            message=f"发送 {len(candidates)} 个非 BTC 候选市场",
            details=[
                f"LLM 模型: {self._market_scout_llm_model()}",
                *[_market_scout_candidate_line(candidate, index) for index, candidate in enumerate(candidates, start=1)],
            ],
            now=now,
        )
        started = time.perf_counter()
        try:
            raw = self._call_market_scout_llm(candidates, now)
            decision = _normalize_market_scout_llm_decision(raw, candidates)
        except Exception as exc:  # noqa: BLE001 - LLM 失败必须写日志，但不能停止扫描。
            error = f"{type(exc).__name__}: {exc}"
            self._set_market_scout_status(
                state="error",
                message="LLM 分析失败",
                scanner_running=True,
                last_error=error,
                last_llm_at=time.time(),
            )
            self._append_market_llm_terminal_log(
                level="error",
                module="llm",
                event_type="llm_error",
                title="LLM 分析失败",
                message=error,
                details=["本轮不会产生推荐，也不会触发任何下单动作"],
            )
            return
        elapsed_ms = _elapsed_ms(started)
        self._market_scout_last_candidate_signature = signature
        self._market_scout_last_llm_at = time.time()
        selected_slug = str(decision.get("selected_slug") or "")
        self._set_market_scout_status(
            state="analyzed",
            message=_market_scout_decision_status_message(decision),
            scanner_running=True,
            last_llm_at=self._market_scout_last_llm_at,
            analyzed_slug=selected_slug,
            last_decision=decision,
        )
        level = "pass" if decision.get("decision") == "RECOMMEND" and (_maybe_float(decision.get("confidence")) or 0.0) >= 0.7 else "info"
        if decision.get("decision") == "RECOMMEND" and (_maybe_float(decision.get("confidence")) or 0.0) < 0.7:
            level = "warn"
        self._append_market_llm_terminal_log(
            level=level,
            module="llm",
            event_type="llm_result",
            title="LLM 分析结果",
            message=_market_scout_decision_status_message(decision),
            details=_market_scout_decision_details(decision, elapsed_ms),
            code=json.dumps(decision.get("raw") or {}, ensure_ascii=False, indent=2)[:2_000],
        )
        self._maybe_execute_market_scout_paper_order(decision, candidates)

    def _append_market_scout_llm_unavailable(self, now: float, reason: str) -> None:
        if now - self._market_scout_last_no_key_log_at < 60.0:
            return
        self._market_scout_last_no_key_log_at = now
        self._append_market_llm_terminal_log(
            level="warn",
            module="llm",
            event_type="llm_unavailable",
            title="LLM 分析未运行",
            message=reason,
            details=[
                "候选扫描仍在运行",
                "当前不会产生 LLM 推荐",
                "自动下注执行: 当前没有 LLM 推荐可执行",
            ],
            now=now,
        )

    def _call_market_scout_llm(self, candidates: list[dict[str, Any]], now: float) -> dict[str, Any]:
        base_url = self.settings.llm_super_agent_base_url.rstrip("/")
        model = self._market_scout_llm_model()
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _market_scout_llm_system_prompt()},
                {"role": "user", "content": _market_scout_llm_user_prompt(candidates, now)},
            ],
            "temperature": 0.1,
        }
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.llm_super_agent_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = max(1.0, float(self.settings.market_scout_llm_timeout_seconds))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("LLM 认证失败 HTTP 401，请检查 POLYBOT2OTHER_LLM_API_KEY 或 HAOAI_API_KEY") from exc
            raise RuntimeError(f"LLM HTTP {exc.code}") from exc
        content = _chat_completion_content(payload)
        parsed = _extract_json_object(content)
        parsed["_provider_usage"] = payload.get("usage") if isinstance(payload, dict) else None
        return parsed

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.btc_runtime_is_paused():
                self._set_btc_runtime_paused_runtime_state(time.time())
                self._stop.wait(self.settings.tick_seconds)
                continue
            self.tick()
            self._stop.wait(self.settings.tick_seconds)

    def _run_market_data(self) -> None:
        while not self._stop.is_set():
            if self.btc_runtime_is_paused():
                self._set_btc_runtime_paused_runtime_state(time.time())
                self._stop.wait(BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS)
                continue
            self._refresh_backend_market_data_once()
            self._stop.wait(BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS)

    def _run_clob_ws(self) -> None:
        if self.btc_runtime_is_paused():
            return
        self.clob_ws_feed.run(
            self._stop,
            self._clob_ws_market,
            self._ingest_backend_clob_ws_quotes,
            self._set_backend_clob_ws_status,
        )

    def _run_rtds_chainlink_ws(self) -> None:
        if self.btc_runtime_is_paused():
            return
        self.rtds_chainlink_feed.run(
            self._stop,
            self._ingest_backend_rtds_price,
            self._set_backend_rtds_ws_status,
        )

    def _run_okx_spot_ws(self) -> None:
        if self.btc_runtime_is_paused():
            return
        self.okx_spot_ws_feed.run(
            self._stop,
            self._ingest_backend_spot_price,
            self._set_backend_spot_ws_status,
        )

    def _run_binance_spot_ws(self) -> None:
        if self.btc_runtime_is_paused():
            return
        self.binance_spot_ws_feed.run(
            self._stop,
            self._ingest_backend_spot_price,
            self._set_backend_spot_ws_status,
        )

    def _clob_ws_market(self) -> MarketRound | None:
        if self.btc_runtime_is_paused():
            return None
        with self._lock:
            return self.current_market

    def _refresh_backend_market_data_once(self) -> None:
        now = time.time()
        if self.btc_runtime_is_paused():
            self._set_btc_runtime_paused_runtime_state(now)
            return
        try:
            with self._lock:
                market = self.current_market
            if market is None or market.ends_at <= now + 0.5:
                market = self._refresh_market()
            if market is not None:
                if self._backend_quote_refresh_needed(now):
                    self._backend_quote_snapshot(market, blocking=False)
                if self._backend_price_refresh_needed(time.time()):
                    self._start_backend_price_refresh(market)
            with self._lock:
                self.ws_status["backend_market_data_loop_at"] = time.time()
                self.ws_status.pop("backend_market_data_error", None)
        except Exception as exc:  # noqa: BLE001 - keep dashboard and trading loop alive.
            with self._lock:
                self.ws_status["backend_market_data_loop_at"] = time.time()
                self.ws_status["backend_market_data_error"] = f"{type(exc).__name__}: {exc}"

    def tick(self) -> None:
        now = time.time()
        if self._btc_runtime_stop_requested():
            return
        tick_started = time.perf_counter()
        profile: dict[str, Any] = {"started_at": now, "status": "ok"}
        try:
            step_started = time.perf_counter()
            market = self._refresh_market()
            profile["refresh_market_ms"] = _elapsed_ms(step_started)
            if self._btc_runtime_stop_requested():
                profile["status"] = "btc_runtime_paused"
                return
            if market is None:
                profile["status"] = "market_unavailable"
                self._set_error("current_btc_5m_market_unavailable", now)
                return
            if self._backend_market_data_refresh_needed(now) and not self._market_data_loop_alive():
                step_started = time.perf_counter()
                self._backend_market_data_snapshot(market)
                profile["backend_market_data_snapshot_ms"] = _elapsed_ms(step_started)
            if self._btc_runtime_stop_requested():
                profile["status"] = "btc_runtime_paused"
                return
            step_started = time.perf_counter()
            self._settle_due(now)
            profile["settle_due_ms"] = _elapsed_ms(step_started)
            if self._btc_runtime_stop_requested():
                profile["status"] = "btc_runtime_paused"
                return
            step_started = time.perf_counter()
            self._reconcile_official_settlements(now)
            profile["official_reconcile_ms"] = _elapsed_ms(step_started)
            if self._btc_runtime_stop_requested():
                profile["status"] = "btc_runtime_paused"
                return
            step_started = time.perf_counter()
            self._backfill_official_final_prices(now)
            profile["official_backfill_ms"] = _elapsed_ms(step_started)
            if self._btc_runtime_stop_requested():
                profile["status"] = "btc_runtime_paused"
                return
            step_started = time.perf_counter()
            self._run_strategy_from_state()
            profile["strategy_and_live_ms"] = _elapsed_ms(step_started)
            if self._btc_runtime_stop_requested():
                profile["status"] = "btc_runtime_paused"
                return
            step_started = time.perf_counter()
            self.store.record_equity()
            if self.live_trading is not None:
                self.live_trading.store.record_equity()
            profile["equity_record_ms"] = _elapsed_ms(step_started)
            with self._lock:
                self.last_error = None
                self.last_tick_at = time.time()
        except Exception as exc:  # noqa: BLE001 - dashboard must keep running and expose the error.
            profile["status"] = "error"
            profile["error"] = f"{type(exc).__name__}: {exc}"
            self._set_error(f"{type(exc).__name__}: {exc}", now)
        finally:
            profile["total_ms"] = _elapsed_ms(tick_started)
            self._record_tick_profile(profile)

    def ingest_live_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        if self.btc_runtime_is_paused():
            return self.btc_runtime_paused_snapshot_response()
        client_market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
        with self._lock:
            cached_market = self.current_market
            last_ingest_at = self._last_live_snapshot_ingest_at
        if (
            cached_market is not None
            and client_market.get("slug") == cached_market.round_id
            and now - last_ingest_at < LIVE_SNAPSHOT_MIN_INTERVAL_SECONDS
        ):
            return {
                "ok": True,
                "throttled_snapshot": True,
                "market": market_to_payload(cached_market),
                "updated_at": now,
            }
        if cached_market is not None and client_market.get("slug") == cached_market.round_id:
            with self._lock:
                self._last_live_snapshot_ingest_at = now

        if cached_market is None:
            market = self._refresh_market()
            if market is None:
                return {
                    "ok": True,
                    "ignored_snapshot": "market_unavailable",
                    "updated_at": now,
                }
            cached_market = market
        if cached_market.ends_at <= now:
            return {
                "ok": True,
                "ignored_snapshot": "expired_market",
                "market": market_to_payload(cached_market),
                "updated_at": now,
            }
        if client_market.get("slug") and client_market.get("slug") != cached_market.round_id:
            return {
                "ok": True,
                "ignored_snapshot": "stale_market",
                "market": market_to_payload(cached_market),
                "updated_at": now,
            }
        market = cached_market

        price = payload.get("price") if isinstance(payload.get("price"), dict) else {}
        price = dict(price)
        if market.target_price > 0:
            price["target_price"] = market.target_price
            price["target_price_source"] = "market.target_price"
            price["target_price_fallback"] = False
        else:
            price.pop("target_price", None)
            price.pop("target_price_source", None)
            price.pop("target_price_fallback", None)
            price.pop("target_price_updated_ms", None)
        quotes = payload.get("quotes") if isinstance(payload.get("quotes"), dict) else {}
        cleaned_quotes = _clean_quotes(quotes)
        self.store.upsert_round(market)
        with self._lock:
            self.current_market = market
            self.latest_price = dict(price)
            self.latest_quotes = _merge_quote_depth(cleaned_quotes, self.latest_quotes)
            self.ws_status = {
                "market": str(payload.get("market_ws_status") or "browser"),
                "price": str(payload.get("price_ws_status") or "browser"),
                "browser_feed_at": now,
                "backend_rest_fallback_at": self.ws_status.get("backend_rest_fallback_at"),
                "backend_market_data_loop_at": self.ws_status.get("backend_market_data_loop_at"),
                "backend_market_data_error": self.ws_status.get("backend_market_data_error"),
                "backend_clob_ws": self.ws_status.get("backend_clob_ws"),
                "backend_clob_ws_at": self.ws_status.get("backend_clob_ws_at"),
                "backend_clob_ws_market": self.ws_status.get("backend_clob_ws_market"),
                "backend_clob_ws_event": self.ws_status.get("backend_clob_ws_event"),
                "backend_clob_ws_error": self.ws_status.get("backend_clob_ws_error"),
                "backend_rtds_ws": self.ws_status.get("backend_rtds_ws"),
                "backend_rtds_ws_at": self.ws_status.get("backend_rtds_ws_at"),
                "backend_rtds_ws_topic": self.ws_status.get("backend_rtds_ws_topic"),
                "backend_rtds_ws_error": self.ws_status.get("backend_rtds_ws_error"),
            }
            self._last_live_snapshot_ingest_at = now
        return {
            "ok": True,
            "market": market_to_payload(market),
            "updated_at": now,
            "display_quote_sides": sorted(cleaned_quotes),
            "market_data_scope": {
                "display": "browser_or_backend",
                "paper": "backend_only",
                "execution": "backend_only",
            },
        }

    def _refresh_market(self):
        if self.btc_runtime_is_paused():
            self._set_btc_runtime_paused_runtime_state(time.time())
            with self._lock:
                return self.current_market
        try:
            market = self.polymarket.find_current_btc_5m_market()
        except Exception:  # noqa: BLE001 - keep dashboard alive during short upstream failures.
            market = self._fallback_market_from_store(time.time())
        if market is None:
            market = self._fallback_market_from_store(time.time())
        if market is None:
            return None
        with self._lock:
            previous = self.current_market
            self.current_market = market
            if previous is None or previous.round_id != market.round_id:
                self.latest_quotes = {}
                self.latest_price = {}
                self.paper_quotes = {}
                self.paper_price = {}
                self.execution_quotes = {}
                self.execution_price = {}
                self.last_signal = None
        self.store.upsert_round(market)
        return market

    def _fallback_market_from_store(self, now: float) -> MarketRound | None:
        row = self.store.latest_active_round(now)
        if not row:
            return None
        return MarketRound(
            round_id=str(row.get("round_id") or ""),
            symbol=str(row.get("symbol") or "BTC"),
            started_at=float(row.get("started_at") or 0.0),
            ends_at=float(row.get("ends_at") or 0.0),
            target_price=float(row.get("target_price") or 0.0),
            question=str(row.get("question") or ""),
            condition_id=str(row.get("condition_id") or ""),
            up_token=str(row.get("up_token") or ""),
            down_token=str(row.get("down_token") or ""),
            slug=str(row.get("round_id") or ""),
            url=str(row.get("url") or ""),
        )

    def _rest_fallback_snapshot(self, market) -> None:
        self._refresh_backend_quotes(market)
        self._refresh_backend_prices(market)

    def _refresh_backend_quotes(self, market: MarketRound) -> None:
        if self.btc_runtime_is_paused():
            self._set_btc_runtime_paused_runtime_state(time.time())
            return
        quotes = self.market_data_polymarket.get_quotes(market)
        now = time.time()
        backend_quotes = {side: quote.to_dict() for side, quote in quotes.items()}
        with self._lock:
            self.latest_quotes = _copy_quotes(backend_quotes)
            self.paper_quotes = _copy_quotes(backend_quotes)
            self.execution_quotes = _copy_quotes(backend_quotes)
            self.ws_status["backend_rest_fallback_at"] = now
            fed_at = self.ws_status.get("browser_feed_at")
            if not fed_at or now - float(fed_at) > self.settings.live_snapshot_max_age_seconds:
                self.ws_status["market"] = "rest-fallback"
            self._last_backend_quote_refresh_at = now

    def _refresh_backend_prices(self, market: MarketRound) -> None:
        if self.btc_runtime_is_paused():
            self._set_btc_runtime_paused_runtime_state(time.time())
            return
        now = time.time()
        ticks = self.market_data_price_fallback.fetch_sources("BTC", now)
        fallback_tick = ticks.get("coinbase") or ticks.get("binance") or ticks.get("okx")
        if fallback_tick is None:
            fallback_tick = self.market_data_price_fallback.fetch_symbol("BTC", now)
        now_ms = int(time.time() * 1000)
        with self._lock:
            price = dict(self.execution_price or self.paper_price or {})
            price = self._merge_backend_chainlink_cache_locked(price, now_ms)
        price.update(
            {
                "binance": ticks.get("binance").price if ticks.get("binance") else fallback_tick.price,
                "binance_updated_ms": now_ms,
                "okx": ticks.get("okx").price if ticks.get("okx") else None,
                "okx_updated_ms": now_ms if ticks.get("okx") else None,
                "source": (
                    "backend-rtds-chainlink+rest-fallback"
                    if _maybe_float(price.get("chainlink"))
                    else f"{fallback_tick.source}-rest-fallback"
                ),
            }
        )
        price = self._backend_price_payload(market, price, now_ms)
        with self._lock:
            self.latest_price = dict(price)
            self.paper_price = dict(price)
            self.execution_price = dict(price)
            self.ws_status["backend_rest_fallback_at"] = time.time()
            fed_at = self.ws_status.get("browser_feed_at")
            if not fed_at or now - float(fed_at) > self.settings.live_snapshot_max_age_seconds:
                self.ws_status["price"] = "rest-fallback"
            else:
                self.ws_status["price"] = "rest-fallback"
            self._last_backend_price_refresh_at = time.time()
        self.store.save_price_tick("BTC", fallback_tick.price, fallback_tick.source, time.time())

    def _backend_price_payload(
        self,
        market: MarketRound | None,
        price: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        payload = dict(price)
        if market is not None and market.target_price > 0:
            payload["target_price"] = market.target_price
            payload["target_price_source"] = "market.target_price"
            payload["target_price_fallback"] = False
        with self._price_basis_lock:
            return self.price_basis_tracker.enrich(payload, now_ms)

    def _merge_backend_chainlink_cache_locked(
        self,
        price: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """把最近后端 Chainlink 缓存合并进价格负载；调用方必须已持有 bot 锁。"""

        merged = dict(price)
        existing_chainlink = _maybe_float(merged.get("chainlink"))
        existing_updated_ms = _maybe_int(merged.get("chainlink_updated_ms"))
        if existing_chainlink is not None and existing_chainlink > 0 and existing_updated_ms:
            return merged
        cached = dict(self._latest_backend_chainlink_price)
        cached_chainlink = _maybe_float(cached.get("chainlink"))
        cached_updated_ms = _maybe_int(cached.get("chainlink_updated_ms"))
        if cached_chainlink is None or cached_chainlink <= 0 or not cached_updated_ms:
            return merged
        merged["chainlink"] = cached_chainlink
        merged["chainlink_updated_ms"] = cached_updated_ms
        if cached.get("source") and not merged.get("source"):
            merged["source"] = cached["source"]
        age_ms = max(0, (int(time.time() * 1000) if now_ms is None else int(now_ms)) - cached_updated_ms)
        logger.debug(
            "后端价格合并 Chainlink 缓存 chainlink=%s age_ms=%s source=%s",
            cached_chainlink,
            age_ms,
            cached.get("source"),
        )
        return merged

    def _set_backend_rtds_ws_status(self, status: dict[str, Any]) -> None:
        state = str(status.get("state") or "unknown")
        with self._lock:
            self.ws_status["backend_rtds_ws"] = state
            self.ws_status["backend_rtds_ws_at"] = _maybe_float(status.get("at")) or time.time()
            if status.get("topic"):
                self.ws_status["backend_rtds_ws_topic"] = str(status.get("topic"))
            if status.get("error"):
                self.ws_status["backend_rtds_ws_error"] = str(status.get("error"))
            elif state in {"connected", "message", "connecting"}:
                self.ws_status.pop("backend_rtds_ws_error", None)

    def _ingest_backend_chainlink_price(
        self,
        price: dict[str, Any],
        status: dict[str, Any] | None = None,
    ) -> None:
        if self.btc_runtime_is_paused():
            return
        chainlink = _maybe_float(price.get("chainlink"))
        updated_ms = _maybe_int(price.get("chainlink_updated_ms"))
        if chainlink is None or chainlink <= 0 or not updated_ms:
            return
        now = time.time()
        now_ms = int(now * 1000)
        chainlink_payload = {
            "chainlink": chainlink,
            "chainlink_updated_ms": updated_ms,
            "source": str(price.get("source") or "polymarket-rtds-chainlink"),
        }
        with self._lock:
            market = self.current_market
            self._latest_backend_chainlink_price = dict(chainlink_payload)
            merged = self._merge_backend_chainlink_cache_locked(dict(self.execution_price or self.paper_price or {}), now_ms)
        merged.update(chainlink_payload)
        enriched = self._backend_price_payload(market, merged, now_ms)
        with self._lock:
            self.latest_price = dict(enriched)
            self.paper_price = dict(enriched)
            self.execution_price = dict(enriched)
            self._last_backend_market_data_refresh_at = max(self._last_backend_market_data_refresh_at, now)
            self.ws_status["price"] = "backend-rtds-chainlink"
            self.ws_status["backend_rtds_ws"] = str((status or {}).get("state") or "message")
            self.ws_status["backend_rtds_ws_at"] = _maybe_float((status or {}).get("at")) or now
            self.ws_status["backend_rtds_ws_topic"] = str((status or {}).get("topic") or "crypto_prices_chainlink")
            self.ws_status.pop("backend_rtds_ws_error", None)
        logger.debug(
            "后端 Chainlink 价格已缓存 chainlink=%s age_ms=%s source=%s",
            chainlink,
            max(0, now_ms - updated_ms),
            chainlink_payload["source"],
        )

    def _ingest_backend_rtds_price(
        self,
        price: dict[str, Any],
        status: dict[str, Any] | None = None,
    ) -> None:
        """接收 Polymarket RTDS 价格；Chainlink 和 crypto spot 共用同一条连接。"""

        if _maybe_float(price.get("chainlink")) is not None:
            self._ingest_backend_chainlink_price(price, status)
            return
        if _maybe_float(price.get("binance_market")) is not None:
            rtds_status = dict(status or {})
            rtds_status.setdefault("state", "message")
            rtds_status.setdefault("topic", "crypto_prices")
            self._set_backend_rtds_ws_status(rtds_status)
            spot_status = {
                "source": SPOT_WS_SOURCE_BINANCE,
                "state": str(rtds_status.get("state") or "message"),
                "at": _maybe_float(rtds_status.get("at")) or time.time(),
                "topic": str(rtds_status.get("topic") or "crypto_prices"),
                "transport": "polymarket-rtds",
            }
            self._ingest_backend_spot_price(price, spot_status)

    def _ingest_backend_spot_price(
        self,
        price: dict[str, Any],
        status: dict[str, Any] | None = None,
    ) -> None:
        """接收后端 OKX/Binance WS 现货价格；只更新后端行情作用域。"""

        if self.btc_runtime_is_paused():
            return
        okx = _maybe_float(price.get("okx"))
        binance = _maybe_float(price.get("binance_market"))
        if (okx is None or okx <= 0) and (binance is None or binance <= 0):
            return
        now = time.time()
        now_ms = int(now * 1000)
        source = (
            SPOT_WS_SOURCE_OKX
            if okx is not None and okx > 0
            else SPOT_WS_SOURCE_BINANCE
            if binance is not None and binance > 0
            else ""
        )
        merged_update = dict(price)
        if source == SPOT_WS_SOURCE_OKX and not _maybe_int(merged_update.get("okx_updated_ms")):
            merged_update["okx_updated_ms"] = now_ms
        if source == SPOT_WS_SOURCE_BINANCE and not _maybe_int(merged_update.get("binance_market_updated_ms")):
            merged_update["binance_market_updated_ms"] = now_ms
        with self._lock:
            market = self.current_market
            merged = dict(self.execution_price or self.paper_price or self.latest_price or {})
            merged = self._merge_backend_chainlink_cache_locked(merged, now_ms)
        merged.update(merged_update)
        merged["source"] = (
            "backend-rtds-chainlink+spot-ws"
            if _maybe_float(merged.get("chainlink"))
            else f"backend-{source}-spot-ws"
        )
        enriched = self._backend_price_payload(market, merged, now_ms)
        spot_status = dict(status or {})
        spot_status.setdefault("source", source)
        spot_status.setdefault("state", "message")
        exchange_updated_ms = _spot_exchange_updated_ms(enriched, source)
        if exchange_updated_ms:
            spot_status["exchange_age_ms"] = max(0, now_ms - exchange_updated_ms)
        with self._lock:
            self.latest_price = dict(enriched)
            self.paper_price = dict(enriched)
            self.execution_price = dict(enriched)
            self._last_backend_market_data_refresh_at = max(self._last_backend_market_data_refresh_at, now)
            self.ws_status["price"] = (
                "backend-rtds-chainlink+spot-ws"
                if _maybe_float(enriched.get("chainlink"))
                else f"backend-{source}-spot-ws"
            )
            self._apply_backend_spot_ws_status_locked(spot_status, now)

    def _set_backend_spot_ws_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            self._apply_backend_spot_ws_status_locked(status, time.time())

    def _apply_backend_spot_ws_status_locked(self, status: dict[str, Any], fallback_at: float) -> None:
        source = str(status.get("source") or "")
        if source not in {SPOT_WS_SOURCE_OKX, SPOT_WS_SOURCE_BINANCE}:
            return
        state = str(status.get("state") or "unknown")
        prefix = "backend_okx_ws" if source == SPOT_WS_SOURCE_OKX else "backend_binance_ws"
        self.ws_status[prefix] = state
        self.ws_status[f"{prefix}_at"] = _maybe_float(status.get("at")) or fallback_at
        if status.get("exchange_age_ms") is not None:
            self.ws_status[f"{prefix}_exchange_age_ms"] = _round_float(
                _maybe_float(status.get("exchange_age_ms")),
                3,
            )
        if status.get("error"):
            self.ws_status[f"{prefix}_error"] = str(status.get("error"))
        elif state in {"connected", "message", "connecting"}:
            self.ws_status.pop(f"{prefix}_error", None)

    def _set_backend_clob_ws_status(self, status: dict[str, Any]) -> None:
        state = str(status.get("state") or "unknown")
        with self._lock:
            self.ws_status["backend_clob_ws"] = state
            self.ws_status["backend_clob_ws_at"] = _maybe_float(status.get("at")) or time.time()
            if status.get("market"):
                self.ws_status["backend_clob_ws_market"] = str(status.get("market"))
            if status.get("event_type"):
                self.ws_status["backend_clob_ws_event"] = str(status.get("event_type"))
            if status.get("error"):
                self.ws_status["backend_clob_ws_error"] = str(status.get("error"))
            elif state in {"connected", "message", "resubscribe", "waiting_market", "connecting"}:
                self.ws_status.pop("backend_clob_ws_error", None)

    def _ingest_backend_clob_ws_quotes(
        self,
        market: MarketRound,
        quotes: dict[str, dict[str, Any]],
        status: dict[str, Any] | None = None,
    ) -> None:
        if self.btc_runtime_is_paused():
            return
        now = time.time()
        cleaned = _clean_quotes(quotes)
        if not cleaned:
            return
        with self._lock:
            current = self.current_market
            if current is None or current.round_id != market.round_id:
                return
            latest = _copy_quotes(self.latest_quotes)
            latest.update(_merge_quote_depth(cleaned, latest))
            paper = _copy_quotes(self.paper_quotes)
            paper.update(_merge_quote_depth(cleaned, paper))
            execution = _copy_quotes(self.execution_quotes)
            execution.update(_merge_quote_depth(cleaned, execution))
            quote_feed_stale = self._quotes_stale_for_strategy(execution, now)
            self.latest_quotes = latest
            self.paper_quotes = paper
            self.execution_quotes = execution
            if not quote_feed_stale:
                self._last_backend_quote_refresh_at = now
            self._last_backend_market_data_refresh_at = max(self._last_backend_market_data_refresh_at, now)
            self.ws_status["market"] = "clob-ws"
            self.ws_status["backend_clob_ws"] = str((status or {}).get("state") or "message")
            self.ws_status["backend_clob_ws_at"] = _maybe_float((status or {}).get("at")) or now
            self.ws_status["backend_clob_ws_market"] = market.round_id
            self.ws_status["backend_clob_ws_event"] = str((status or {}).get("event_type") or "")
            self.ws_status.pop("backend_clob_ws_error", None)

    def _backend_market_data_snapshot(self, market: MarketRound, *, blocking: bool = True) -> bool:
        acquired = self._market_data_refresh_lock.acquire(blocking=blocking)
        if not acquired:
            return False
        try:
            self._rest_fallback_snapshot(market)
            with self._lock:
                self._last_backend_market_data_refresh_at = time.time()
            return True
        finally:
            self._market_data_refresh_lock.release()

    def _backend_quote_snapshot(self, market: MarketRound, *, blocking: bool = True) -> bool:
        acquired = self._market_data_refresh_lock.acquire(blocking=blocking)
        if not acquired:
            return False
        try:
            self._refresh_backend_quotes(market)
            with self._lock:
                self._last_backend_market_data_refresh_at = max(
                    self._last_backend_market_data_refresh_at,
                    self._last_backend_quote_refresh_at,
                )
            return True
        finally:
            self._market_data_refresh_lock.release()

    def _start_backend_price_refresh(self, market: MarketRound) -> bool:
        acquired = self._price_refresh_lock.acquire(blocking=False)
        if not acquired:
            return False

        def _worker() -> None:
            try:
                self._refresh_backend_prices(market)
                with self._lock:
                    self._last_backend_market_data_refresh_at = max(
                        self._last_backend_market_data_refresh_at,
                        self._last_backend_price_refresh_at,
                    )
            except Exception as exc:  # noqa: BLE001 - price source failure must not stop quote refresh.
                with self._lock:
                    self.ws_status["backend_market_data_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                self._price_refresh_lock.release()

        self._price_refresh_thread = threading.Thread(
            target=_worker,
            name="polybot2other-price-refresh",
            daemon=True,
        )
        self._price_refresh_thread.start()
        return True

    def _market_data_loop_alive(self) -> bool:
        return bool(self._market_data_thread and self._market_data_thread.is_alive())

    def _backend_market_data_refresh_needed(self, now: float) -> bool:
        return self._backend_quote_refresh_needed(now) or self._backend_price_refresh_needed(now)

    def _backend_quote_refresh_needed(self, now: float) -> bool:
        if now - self._last_backend_quote_refresh_at < BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS:
            return False
        return self._quote_feed_stale_for_strategy(now)

    def _backend_price_refresh_needed(self, now: float) -> bool:
        if now - self._last_backend_price_refresh_at < BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS:
            return False
        if self._price_feed_stale(now):
            return True
        return self._price_feed_stale_for_strategy(now) or self._fallback_price_feed_stale_for_strategy(now)

    def _strategy_feed_refresh_age_seconds(self) -> float:
        return max(
            BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS,
            (self.settings.max_quote_age_ms / 1000.0) * BACKEND_MARKET_DATA_REFRESH_RATIO,
        )

    def _paper_market_data_locked(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
        price = dict(self.paper_price)
        quotes = _copy_quotes(self.paper_quotes)
        if not self.ws_status.get("browser_feed_at"):
            latest_price = dict(self.latest_price)
            latest_quotes = _copy_quotes(self.latest_quotes)
            if latest_price or latest_quotes:
                return latest_price, latest_quotes, "compat_latest_without_browser"
        if price or quotes or self.ws_status.get("browser_feed_at"):
            return price, quotes, "backend"
        return dict(self.latest_price), _copy_quotes(self.latest_quotes), "compat_latest_without_browser"

    def _paper_market_data(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
        with self._lock:
            return self._paper_market_data_locked()

    def _execution_market_data_locked(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
        price = dict(self.execution_price)
        quotes = _copy_quotes(self.execution_quotes)
        if (price or quotes) or self.ws_status.get("browser_feed_at"):
            return price, quotes, "backend"
        return dict(self.latest_price), _copy_quotes(self.latest_quotes), "compat_latest_without_browser"

    def _execution_market_data(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
        with self._lock:
            return self._execution_market_data_locked()

    def _quote_feed_stale_for_strategy(self, now: float) -> bool:
        with self._lock:
            _price, quotes, _source = self._execution_market_data_locked()
        return self._quotes_stale_for_strategy(quotes, now)

    def _quotes_stale_for_strategy(self, quotes: dict[str, dict[str, Any]], now: float) -> bool:
        max_age_seconds = self._strategy_feed_refresh_age_seconds()
        for side in ("Up", "Down"):
            quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
            if not quote:
                return True
            updated_ms = _maybe_int(quote.get("updated_at_ms")) or 0
            if not updated_ms:
                return True
            if now - updated_ms / 1000.0 > max_age_seconds:
                return True
        return False

    def _price_feed_stale_for_strategy(self, now: float) -> bool:
        with self._lock:
            price, _quotes, _source = self._execution_market_data_locked()
        latest_ms = max(
            _maybe_int(price.get("chainlink_updated_ms")) or 0,
            _maybe_int(price.get("binance_updated_ms")) or 0,
            _maybe_int(price.get("binance_market_updated_ms")) or 0,
            _maybe_int(price.get("okx_updated_ms")) or 0,
        )
        if not latest_ms:
            return True
        return now - latest_ms / 1000.0 > self._strategy_feed_refresh_age_seconds()

    def _fallback_price_feed_stale_for_strategy(self, now: float) -> bool:
        with self._lock:
            price, _quotes, _source = self._execution_market_data_locked()
        selected_sources = self._selected_live_fallback_price_sources()
        max_age_seconds = self._strategy_feed_refresh_age_seconds()
        for source in selected_sources:
            updated_ms = _fallback_source_updated_ms(price, source)
            if not updated_ms:
                return True
            if now - updated_ms / 1000.0 > max_age_seconds:
                return True
        return False

    def _selected_live_fallback_price_sources(self) -> tuple[str, ...]:
        if self.live_trading is None:
            return MULTI_SOURCE_KEYS
        selected = tuple(source for source in self.live_trading.config.fallback_sources if source in MULTI_SOURCE_KEYS)
        return selected or MULTI_SOURCE_KEYS

    def _live_feed_stale(self, now: float) -> bool:
        with self._lock:
            fed_at = self.ws_status.get("browser_feed_at")
        return not fed_at or now - float(fed_at) > self.settings.live_snapshot_max_age_seconds

    def _price_feed_stale(self, now: float) -> bool:
        with self._lock:
            price, _quotes, _source = self._execution_market_data_locked()
        latest_ms = max(_maybe_int(price.get("chainlink_updated_ms")) or 0, _maybe_int(price.get("binance_updated_ms")) or 0)
        if not latest_ms:
            return True
        return now - latest_ms / 1000.0 > self.settings.live_snapshot_max_age_seconds

    def _settle_due(self, now: float) -> None:
        open_trades = self.store.open_trades()
        due_slugs = sorted({row["round_id"] for row in open_trades if row["symbol"] == "BTC" and row["ends_at"] <= now})
        for slug in due_slugs:
            resolution = self.polymarket.get_resolution(slug)
            if resolution and resolution.get("outcome") in {"Up", "Down"}:
                final_price = _maybe_float(resolution.get("final_price"))
                target_price = _maybe_float(resolution.get("target_price"))
                self.store.settle_round_outcome(
                    slug,
                    str(resolution["outcome"]),
                    now,
                    final_price=final_price,
                    target_price=target_price,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )
                self._broadcast_official_resolution(slug, str(resolution["outcome"]), now, final_price, target_price)
                self._finalize_aggressive_edge_loss_replay(slug, str(resolution["outcome"]), now, final_price, target_price)
                if final_price is None:
                    with self._lock:
                        self._official_price_backfill_next_at[slug] = now + OFFICIAL_PRICE_BACKFILL_INTERVAL_SECONDS
                continue
        with self._lock:
            price, _quotes, _source = self._paper_market_data_locked()
        chainlink_price = _maybe_float(price.get("chainlink"))
        if chainlink_price:
            settled = self.store.settle_due_rounds({"BTC": chainlink_price}, now)
            if settled:
                with self._lock:
                    for row in settled:
                        round_id = str(row.get("round_id") or "")
                        if round_id:
                            self._official_recheck_next_at.setdefault(
                                round_id,
                                now + OFFICIAL_RECHECK_INTERVAL_SECONDS,
                            )

    def _reconcile_official_settlements(self, now: float) -> None:
        try:
            candidates = self.store.official_recheck_candidates(
                now,
                OFFICIAL_RECHECK_WINDOW_SECONDS,
                OFFICIAL_RECHECK_LIMIT,
                "BTC",
            )
        except Exception:  # noqa: BLE001 - official recheck must not stop trading ticks.
            return
        for row in candidates:
            round_id = str(row.get("round_id") or "")
            if not round_id:
                continue
            with self._lock:
                next_at = self._official_recheck_next_at.get(round_id, 0.0)
            if next_at > now:
                continue
            try:
                resolution = self.polymarket.get_resolution(round_id)
                outcome = resolution.get("outcome") if isinstance(resolution, dict) else None
                if outcome in {"Up", "Down"}:
                    final_price = _maybe_float(resolution.get("final_price"))
                    target_price = _maybe_float(resolution.get("target_price"))
                    self.store.reconcile_round_official_outcome(
                        round_id,
                        str(outcome),
                        now,
                        final_price=final_price,
                        target_price=target_price,
                    )
                    self._broadcast_official_resolution(round_id, str(outcome), now, final_price, target_price)
                    self._finalize_aggressive_edge_loss_replay(round_id, str(outcome), now, final_price, target_price)
                    with self._lock:
                        self._official_recheck_next_at.pop(round_id, None)
                        if final_price is None:
                            self._official_price_backfill_next_at[round_id] = now + OFFICIAL_PRICE_BACKFILL_INTERVAL_SECONDS
                else:
                    with self._lock:
                        self._official_recheck_next_at[round_id] = now + OFFICIAL_RECHECK_INTERVAL_SECONDS
            except Exception:  # noqa: BLE001 - retry later; dashboard exposes fatal errors elsewhere.
                with self._lock:
                    self._official_recheck_next_at[round_id] = now + OFFICIAL_RECHECK_INTERVAL_SECONDS

    def _backfill_official_final_prices(self, now: float) -> None:
        try:
            candidates = self.store.official_final_price_candidates(
                now,
                OFFICIAL_PRICE_BACKFILL_WINDOW_SECONDS,
                OFFICIAL_PRICE_BACKFILL_LIMIT,
                "BTC",
            )
        except Exception:  # noqa: BLE001 - missing price backfill must not stop trading ticks.
            return
        for row in candidates:
            round_id = str(row.get("round_id") or "")
            if not round_id:
                continue
            with self._lock:
                next_at = self._official_price_backfill_next_at.get(round_id, 0.0)
            if next_at > now:
                continue
            try:
                resolution = self.polymarket.get_resolution(round_id)
                if not isinstance(resolution, dict):
                    with self._lock:
                        self._official_price_backfill_next_at[round_id] = now + OFFICIAL_PRICE_BACKFILL_INTERVAL_SECONDS
                    continue
                outcome = resolution.get("outcome") or row.get("outcome")
                final_price = _maybe_float(resolution.get("final_price"))
                target_price = _maybe_float(resolution.get("target_price"))
                if outcome in {"Up", "Down"} and (final_price is not None or target_price is not None):
                    local_final_price = _maybe_float(row.get("final_price"))
                    if (
                        final_price is not None
                        and local_final_price is not None
                        and abs(final_price - local_final_price) > 0.000001
                    ):
                        logger.debug(
                            "官方最终价回填更新 round_id=%s local_final=%s official_final=%s local_target=%s official_target=%s",
                            round_id,
                            local_final_price,
                            final_price,
                            row.get("target_price"),
                            target_price,
                        )
                    self.store.reconcile_round_official_outcome(
                        round_id,
                        str(outcome),
                        now,
                        final_price=final_price,
                        target_price=target_price,
                    )
                    self._broadcast_official_resolution(round_id, str(outcome), now, final_price, target_price)
                    self._finalize_aggressive_edge_loss_replay(round_id, str(outcome), now, final_price, target_price)
                with self._lock:
                    if final_price is not None:
                        self._official_price_backfill_next_at.pop(round_id, None)
                    else:
                        self._official_price_backfill_next_at[round_id] = now + OFFICIAL_PRICE_BACKFILL_INTERVAL_SECONDS
            except Exception:  # noqa: BLE001 - retry later; dashboard exposes fatal errors elsewhere.
                with self._lock:
                    self._official_price_backfill_next_at[round_id] = now + OFFICIAL_PRICE_BACKFILL_INTERVAL_SECONDS

    def _broadcast_official_resolution(
        self,
        round_id: str,
        outcome: str,
        now: float,
        final_price: float | None = None,
        target_price: float | None = None,
    ) -> None:
        if self.strategy_experiments is not None:
            self.strategy_experiments.apply_official_resolution(
                round_id,
                outcome,
                now,
                final_price=final_price,
                target_price=target_price,
            )
        if self.live_trading is not None:
            self.live_trading.apply_official_resolution(
                round_id,
                outcome,
                now,
                final_price=final_price,
                target_price=target_price,
            )
        for runner in self._live_paper_runners():
            runner.apply_official_resolution(
                round_id,
                outcome,
                now,
                final_price=final_price,
                target_price=target_price,
            )

    def _run_strategy_from_state(self) -> None:
        if self._btc_runtime_stop_requested():
            return
        with self._lock:
            market = self.current_market
            price, quotes, _source = self._paper_market_data_locked()
            pair_enabled = self.pair_strategy_enabled
            paper_paused = self.paper_trading_paused
        if market is None:
            return
        has_paper_market_data = bool(price or quotes)
        price = self._backend_price_payload(market, price)
        if has_paper_market_data:
            with self._lock:
                self.paper_price = dict(price)
        if paper_paused:
            now = time.time()
            self._cancel_active_paper_orders_for_pause(now)
            self._set_paper_paused_signal()
            if self.strategy_experiments is not None:
                self.strategy_experiments.set_paper_trading_paused(True, cancel_active=True, now=now)
            if self._btc_runtime_stop_requested():
                return
            self._run_strategy_experiments(market, price, quotes)
            self._run_live_strategy(market, price, quotes)
            return
        self._manage_resting_orders(market, quotes)
        if self._btc_runtime_stop_requested():
            return
        if self.realtime_maker_enabled:
            self._run_realtime_maker_strategy_from_state(market, price, quotes)
            return
        if self.llm_super_agent_enabled:
            self._run_llm_super_agent_strategy_from_state(market, price, quotes)
            return
        if pair_enabled:
            self._run_pair_strategy_from_state(market, price, quotes)
            if self._btc_runtime_stop_requested():
                return
            self._run_strategy_experiments(market, price, quotes)
            self._run_live_strategy(market, price, quotes)
            return
        payload = {"price": price, "quotes": quotes}
        signal = self.strategy.signal(
            input_from_snapshot(market, payload),
            self.market_data_mode,
            self.price_source_mode,
            self.anti_bot_guard_mode,
        )
        signal = self._apply_signal_side_mode(market, signal, quotes)
        signal = self._apply_signal_filter_mode(market, signal, price, quotes)
        with self._lock:
            self.last_signal = {
                "symbol": signal.symbol,
                "side": signal.side,
                "confidence": signal.confidence,
                "entry_price": signal.entry_price,
                "move_bps": signal.move_bps,
                "reason": signal.reason,
            }
        if self._btc_runtime_stop_requested():
            return
        trade_ids = self._maybe_place_trade(market, signal, quotes)
        if self._btc_runtime_stop_requested():
            return
        replay_event = "entry_fill" if trade_ids else "strategy_tick"
        self._record_aggressive_edge_loss_replay_sample(
            market,
            price,
            quotes,
            signal,
            event=replay_event,
            force=bool(trade_ids),
            trade_ids=trade_ids,
        )
        self._run_strategy_experiments(market, price, quotes)
        self._run_live_strategy(market, price, quotes)

    def _run_strategy_experiments(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        if self._btc_runtime_stop_requested():
            return
        if self.strategy_experiments is None:
            return
        self.strategy_experiments.run_from_state(market, price, quotes)

    def _run_live_strategy(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        if self._btc_runtime_stop_requested():
            return
        live_paper_runners = self._live_paper_runners()
        if self.live_trading is None and not live_paper_runners:
            return
        try:
            live_price, live_quotes, _source = self._execution_market_data()
        except Exception as exc:  # noqa: BLE001 - 实盘错误必须暴露但不能阻塞 Paper 采样。
            if self.live_trading is not None:
                self.live_trading.last_error = f"live market data error {type(exc).__name__}: {exc}"
            for runner in live_paper_runners:
                runner.last_error = f"live paper market data error {type(exc).__name__}: {exc}"
            return
        if self.live_trading is not None:
            if self._btc_runtime_stop_requested():
                return
            live_started = time.perf_counter()
            live_error = None
            try:
                self.live_trading.run_from_state(market, live_price, live_quotes)
            except Exception as exc:  # noqa: BLE001 - 实盘错误必须暴露但不能阻塞 Paper 采样。
                live_error = f"{type(exc).__name__}: {exc}"
                self.live_trading.last_error = live_error
            finally:
                self._record_live_gate_diagnostic(
                    market,
                    self.live_trading,
                    live_price,
                    live_quotes,
                    duration_ms=_elapsed_ms(live_started),
                    error=live_error,
                )
        for runner in live_paper_runners:
            if self._btc_runtime_stop_requested():
                return
            try:
                with self._lock:
                    paper_paused = self.paper_trading_paused
                if paper_paused:
                    runner.last_error = PAPER_PAUSE_REASON
                    continue
                runner.run_from_state(
                    market,
                    live_price,
                    live_quotes,
                    live_config=self.live_trading.config if self.live_trading is not None else None,
                )
            except Exception as exc:  # noqa: BLE001 - 影子 Paper 不允许影响主循环和真实实盘。
                runner.last_error = f"{type(exc).__name__}: {exc}"

    def _record_tick_profile(self, profile: dict[str, Any]) -> None:
        """记录主 tick 耗时；只写内存诊断，不参与交易决策。"""

        total_ms = _maybe_float(profile.get("total_ms")) or 0.0
        profile = dict(profile)
        profile["recorded_at"] = time.time()
        with self._lock:
            self._tick_profile_history.append(profile)
            last_logged_at = self._last_slow_tick_log_at
            should_log = total_ms >= LIVE_SLOW_TICK_WARN_MS and profile["recorded_at"] - last_logged_at >= LIVE_SLOW_TICK_LOG_INTERVAL_SECONDS
            if should_log:
                self._last_slow_tick_log_at = profile["recorded_at"]
        if should_log:
            logger.warning(
                "实盘诊断: 主 tick 耗时偏高 total_ms=%.1f status=%s refresh_market_ms=%s "
                "backend_snapshot_ms=%s settle_ms=%s official_reconcile_ms=%s official_backfill_ms=%s "
                "strategy_and_live_ms=%s equity_ms=%s",
                total_ms,
                profile.get("status"),
                _format_optional_float(_maybe_float(profile.get("refresh_market_ms")), 1),
                _format_optional_float(_maybe_float(profile.get("backend_market_data_snapshot_ms")), 1),
                _format_optional_float(_maybe_float(profile.get("settle_due_ms")), 1),
                _format_optional_float(_maybe_float(profile.get("official_reconcile_ms")), 1),
                _format_optional_float(_maybe_float(profile.get("official_backfill_ms")), 1),
                _format_optional_float(_maybe_float(profile.get("strategy_and_live_ms")), 1),
                _format_optional_float(_maybe_float(profile.get("equity_record_ms")), 1),
            )

    def _record_live_gate_diagnostic(
        self,
        market: MarketRound,
        live_runner: LiveStrategyRunner,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        *,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        """记录 V11 REAL 最近阻断原因；只写内存诊断，不影响 live runner 下单路径。"""

        now = time.time()
        now_ms = int(now * 1000)
        signal = dict(live_runner.last_signal or {})
        price_selection = signal.get("price_selection") if isinstance(signal.get("price_selection"), dict) else {}
        reason = str(signal.get("reason") or error or "")
        category = _live_gate_category(signal, price_selection, error=error)
        diagnostic = {
            "at": now,
            "market": market.round_id,
            "seconds_left": round(max(0.0, market.ends_at - now), 3),
            "variant_id": live_runner.variant_id,
            "side": signal.get("side"),
            "entry_price": _round_float(_maybe_float(signal.get("entry_price")), 6),
            "move_bps": _round_float(_maybe_float(signal.get("move_bps")), 6),
            "category": category,
            "reason": reason[:500],
            "duration_ms": round(float(duration_ms), 3),
            "run_count": live_runner.run_count,
            "error": error,
            "price_ages_ms": _price_age_payload(price, now_ms),
            "quote_ages_ms": _quote_age_payload(quotes, now_ms),
            "price_selection": _compact_price_selection(price_selection),
        }
        with self._lock:
            self._live_gate_diagnostics.append(diagnostic)

    def live_health(self) -> dict[str, Any]:
        """轻量实盘健康快照；避免 /api/status 全量聚合给实盘进程加压。"""

        now = time.time()
        now_ms = int(now * 1000)
        with self._lock:
            live_runner = self.live_trading
            current_market = self.current_market
            execution_price = dict(self.execution_price)
            execution_quotes = _copy_quotes(self.execution_quotes)
            ws_status = dict(self.ws_status)
            last_tick_at = self.last_tick_at
            live_gate_rows = list(self._live_gate_diagnostics)
            tick_rows = list(self._tick_profile_history)
        if live_runner is None:
            return {
                "ok": False,
                "checked_at": now,
                "message": "live runner 未加载",
                "live_trading": {"enabled": False},
            }
        last_signal = dict(live_runner.last_signal or {})
        price_selection = (
            last_signal.get("price_selection") if isinstance(last_signal.get("price_selection"), dict) else {}
        )
        return {
            "ok": True,
            "checked_at": now,
            "live_trading": {
                "enabled": bool(live_runner.config.enabled),
                "variant_id": live_runner.variant_id,
                "combo": live_runner.combo,
                "run_count": live_runner.run_count,
                "last_run_at": live_runner.last_run_at,
                "last_run_age_s": _age_seconds(now, live_runner.last_run_at),
                "overlap_skip_count": live_runner.overlap_skip_count,
                "last_error": live_runner.last_error,
                "last_signal": {
                    "side": last_signal.get("side"),
                    "entry_price": last_signal.get("entry_price"),
                    "move_bps": last_signal.get("move_bps"),
                    "reason": str(last_signal.get("reason") or "")[:500],
                    "fak_quote_check": (
                        dict(last_signal.get("fak_quote_check"))
                        if isinstance(last_signal.get("fak_quote_check"), dict)
                        else None
                    ),
                },
                "last_gate_category": _live_gate_category(last_signal, price_selection, error=None),
            },
            "market": market_to_payload(current_market),
            "runtime": {
                "last_tick_at": last_tick_at,
                "last_tick_age_s": _age_seconds(now, last_tick_at),
                "backend_market_data_loop_age_s": _age_seconds(now, ws_status.get("backend_market_data_loop_at")),
                "backend_rest_fallback_age_s": _age_seconds(now, ws_status.get("backend_rest_fallback_at")),
                "backend_clob_ws_age_s": _age_seconds(now, ws_status.get("backend_clob_ws_at")),
                "backend_rtds_ws_age_s": _age_seconds(now, ws_status.get("backend_rtds_ws_at")),
                "backend_okx_ws_age_s": _age_seconds(now, ws_status.get("backend_okx_ws_at")),
                "backend_binance_ws_age_s": _age_seconds(now, ws_status.get("backend_binance_ws_at")),
                "ws_status": ws_status,
            },
            "market_data": {
                "price_ages_ms": _price_age_payload(execution_price, now_ms),
                "price_exchange_ages_ms": _price_exchange_age_payload(execution_price, now_ms),
                "quote_ages_ms": _quote_age_payload(execution_quotes, now_ms),
                "price": _compact_live_price(execution_price),
                "quotes": _compact_live_quotes(execution_quotes),
                "max_quote_age_ms": self.settings.max_quote_age_ms,
            },
            "gate_diagnostics": {
                "last": live_gate_rows[-1] if live_gate_rows else None,
                "last_12": live_gate_rows[-12:],
                "windows": {
                    "60s": _live_gate_window_summary(live_gate_rows, now, 60.0),
                    "300s": _live_gate_window_summary(live_gate_rows, now, 300.0),
                },
            },
            "tick_profile": {
                "last": tick_rows[-1] if tick_rows else None,
                "last_12": tick_rows[-12:],
                "windows": {
                    "60s": _tick_profile_window_summary(tick_rows, now, 60.0),
                    "300s": _tick_profile_window_summary(tick_rows, now, 300.0),
                },
            },
        }

    def _run_llm_super_agent_strategy_from_state(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        now = time.time()
        open_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        account = self.store.account()
        features = build_llm_market_features(
            market,
            price,
            self._quotes_with_depth(market, quotes),
            open_sides=[str(row.get("side") or "") for row in open_rows],
            open_trade_count=self.store.open_trade_count("BTC"),
            active_order_count=len(self.store.active_paper_orders("BTC", market.round_id)),
            daily_pnl=self.store.daily_realized_pnl(),
            cash_balance=float(account.get("cash_balance") or 0.0),
            max_quote_age_ms=self.settings.max_quote_age_ms,
            now=now,
        )
        decision = self.llm_super_agent_router.decide(features, now)
        self._record_llm_super_agent_decision(market, decision, features, now)
        note = _llm_super_agent_reason(decision)
        if not decision.allow_trade or decision.route == LLM_ROUTE_NO_TRADE:
            self._set_llm_super_agent_signal(market, decision, note)
            return
        if decision.confidence < LLM_MIN_CONFIDENCE_TO_TRADE:
            self._set_llm_super_agent_signal(market, decision, f"{note} | confidence below trade threshold")
            return
        modes = route_execution_modes(decision.route)
        family = modes.get("strategy_family")
        previous = {
            "single_entry_mode": self.single_entry_mode,
            "market_data_mode": self.market_data_mode,
            "anti_bot_guard_mode": self.anti_bot_guard_mode,
        }
        try:
            self.market_data_mode = str(modes.get("market_data_mode") or MARKET_DATA_MODE_BASE)
            self.anti_bot_guard_mode = str(modes.get("anti_bot_guard_mode") or ANTI_BOT_GUARD_MODE_NONE)
            if family == "PAIR":
                self._run_pair_strategy_from_state(market, price, quotes)
                self._append_last_signal_reason(note)
                return
            self.single_entry_mode = str(modes.get("single_entry_mode") or SINGLE_ENTRY_MODE_LEGACY)
            signal = self.strategy.signal(
                input_from_snapshot(market, {"price": price, "quotes": quotes}),
                self.market_data_mode,
                self.price_source_mode,
                self.anti_bot_guard_mode,
            )
            signal = replace(signal, reason=_append_reason_text(signal.reason, note))
            signal = self._apply_signal_side_mode(market, signal, quotes)
            signal = self._apply_signal_filter_mode(market, signal, price, quotes)
            with self._lock:
                self.last_signal = {
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "confidence": signal.confidence,
                    "entry_price": signal.entry_price,
                    "move_bps": signal.move_bps,
                    "reason": signal.reason,
                }
            trade_ids = self._maybe_place_trade(market, signal, quotes)
            replay_event = "entry_fill" if trade_ids else "strategy_tick"
            self._record_aggressive_edge_loss_replay_sample(
                market,
                price,
                quotes,
                signal,
                event=replay_event,
                force=bool(trade_ids),
                trade_ids=trade_ids,
            )
        finally:
            self.single_entry_mode = str(previous["single_entry_mode"])
            self.market_data_mode = str(previous["market_data_mode"])
            self.anti_bot_guard_mode = str(previous["anti_bot_guard_mode"])

    def _record_llm_super_agent_decision(
        self,
        market: MarketRound,
        decision: Any,
        features: dict[str, Any],
        now: float,
    ) -> None:
        key = f"{market.round_id}:{decision.decision_id}"
        if key == self._llm_super_agent_last_logged_key:
            return
        self._llm_super_agent_last_logged_key = key
        self.store.record_llm_decision(
            round_id=market.round_id,
            variant_id=self.llm_super_agent_variant_id,
            decision=decision.to_record(),
            features=features,
            created_at=now,
        )

    def _set_llm_super_agent_signal(self, market: MarketRound, decision: Any, reason: str) -> None:
        features_side = "LLM_WAIT" if decision.route == LLM_ROUTE_NO_TRADE else decision.route
        with self._lock:
            self.last_signal = {
                "symbol": market.symbol,
                "side": features_side,
                "confidence": decision.confidence,
                "entry_price": 0.0,
                "move_bps": 0.0,
                "reason": reason,
            }

    def _maybe_place_trade(
        self,
        market,
        signal,
        quotes: dict[str, dict[str, Any]] | None = None,
    ) -> list[int]:
        if signal.side not in {"Up", "Down"}:
            return []
        with self._lock:
            paper_paused = self.paper_trading_paused
        if paper_paused:
            self._append_last_signal_reason(PAPER_PAUSE_REASON)
            return []
        if self.store.daily_realized_pnl() <= -abs(self.settings.max_daily_loss):
            return []
        single_entry_mode = self.single_entry_mode
        round_open_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        same_side_open = any(row.get("side") == signal.side for row in round_open_rows)
        opposite_rows = [row for row in round_open_rows if row.get("side") != signal.side]
        closing_count = len(opposite_rows) if single_entry_mode == SINGLE_ENTRY_MODE_STOP_AND_FLIP else 0
        if self.store.open_trade_count("BTC") - closing_count >= self.settings.max_open_trades:
            return []
        if same_side_open:
            if single_entry_mode in {
                SINGLE_ENTRY_MODE_STRICT,
                SINGLE_ENTRY_MODE_REVERSAL,
                SINGLE_ENTRY_MODE_STOP_AND_FLIP,
            }:
                self._append_last_signal_reason(f"{single_entry_mode} 当前市场已有同方向持仓，跳过重复开仓")
            return []
        if single_entry_mode == SINGLE_ENTRY_MODE_STRICT and round_open_rows:
            existing_sides = _side_list_text(row.get("side") for row in round_open_rows)
            self._append_last_signal_reason(f"{SINGLE_STRICT_MARKER} 当前市场已有 {existing_sides} 持仓，禁止反向开仓")
            return []
        if self.store.active_paper_order_exists(market.round_id, signal.side):
            self._append_last_signal_reason("已有同方向挂单等待成交")
            return []
        if single_entry_mode == SINGLE_ENTRY_MODE_STRICT and self.store.active_paper_order_exists_for_round(market.round_id):
            self._append_last_signal_reason(f"{SINGLE_STRICT_MARKER} 当前市场已有挂单，禁止再次开仓")
            return []
        if single_entry_mode == SINGLE_ENTRY_MODE_STOP_AND_FLIP and self.store.active_paper_order_exists_for_round(market.round_id):
            self._append_last_signal_reason(f"{SINGLE_STOP_AND_FLIP_MARKER} 当前市场有活跃挂单，暂不止损反手")
            return []
        account = self.store.account()
        stake = min(self.settings.stake_dollars, float(account["cash_balance"]))
        if stake < 0.1:
            return []
        if quotes is None:
            with self._lock:
                _price, quotes, _source = self._paper_market_data_locked()
        quotes = _copy_quotes(quotes)
        quote = quotes.get(signal.side) if isinstance(quotes.get(signal.side), dict) else {}
        quote = self._quote_with_depth(market, signal.side, quote)
        if single_entry_mode == SINGLE_ENTRY_MODE_REVERSAL and opposite_rows:
            existing_sides = _side_list_text(row.get("side") for row in opposite_rows)
            note = f"{SINGLE_REVERSAL_MARKER} 双边反转开仓: 已有 {existing_sides} 持仓, 新开 {signal.side}"
            signal = replace(signal, reason=_append_reason_text(signal.reason, note))
            self._append_last_signal_reason(note)
        if single_entry_mode == SINGLE_ENTRY_MODE_STOP_AND_FLIP and opposite_rows:
            precheck_intent = TradeIntent(market=market, signal=signal, stake_dollars=stake)
            precheck = self._execute_entry_order(precheck_intent, quote)
            if not precheck.fills:
                self.store.place_execution_result(precheck_intent, precheck)
                self._append_last_signal_reason(
                    f"{SINGLE_STOP_AND_FLIP_MARKER} 新方向不可成交，保留旧仓 | {precheck.reason}"
                )
                return []
            exit_side = str(opposite_rows[0].get("side") or "")
            exit_quote = quotes.get(exit_side) if isinstance(quotes.get(exit_side), dict) else {}
            exit_quote = self._quote_with_bid(market, exit_side, exit_quote)
            exit_bid = _maybe_float(exit_quote.get("best_bid"))
            if exit_bid is None or exit_bid <= 0:
                self._append_last_signal_reason(f"{SINGLE_STOP_AND_FLIP_MARKER} 缺少 {exit_side} 买一价，保留旧仓")
                return []
            close_shares = sum(_maybe_float(row.get("shares")) or 0.0 for row in opposite_rows)
            now = time.time()
            close_reason = f"{SINGLE_STOP_AND_FLIP_MARKER} 平旧仓后反手 {exit_side}->{signal.side}"
            closed = self._close_side_shares(opposite_rows, exit_side, close_shares, exit_bid, now, close_reason)
            if not closed:
                self._append_last_signal_reason(f"{SINGLE_STOP_AND_FLIP_MARKER} 旧仓平仓失败，取消反手开仓")
                return []
            closed_pnl = sum(_maybe_float(row.get("pnl")) or 0.0 for row in closed)
            note = (
                f"{SINGLE_STOP_AND_FLIP_MARKER} 平旧仓后反手: {exit_side}->{signal.side}, "
                f"close_bid {exit_bid:.4f}, closed {len(closed)}, pnl {closed_pnl:.6f}"
            )
            signal = replace(signal, reason=_append_reason_text(signal.reason, note))
            self._append_last_signal_reason(note)
        intent = TradeIntent(market=market, signal=signal, stake_dollars=stake)
        result = self._execute_entry_order(intent, quote)
        trade_ids = self.store.place_execution_result(intent, result)
        if not result.fills:
            self._append_last_signal_reason(result.reason)
            return []
        if not trade_ids:
            self._append_last_signal_reason("执行结果未生成持仓")
            return []
        return trade_ids

    def _record_aggressive_edge_loss_replay_sample(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        signal: Signal,
        *,
        event: str,
        force: bool = False,
        trade_ids: list[int] | None = None,
    ) -> None:
        if self.signal_filter_mode not in AGGRESSIVE_EDGE_FILTER_MODES:
            return
        try:
            self._aggressive_edge_loss_replay.record_sample(
                market,
                price,
                quotes,
                signal,
                event=event,
                force=force,
                trade_ids=trade_ids,
            )
        except Exception as exc:  # noqa: BLE001 - 复盘记录不能影响交易主循环。
            logger.debug("Aggressive Edge 输局复盘采样失败 round_id=%s error=%s", market.round_id, exc)

    def _finalize_aggressive_edge_loss_replay(
        self,
        round_id: str,
        outcome: str,
        now: float,
        final_price: float | None,
        target_price: float | None,
    ) -> None:
        if self.signal_filter_mode not in AGGRESSIVE_EDGE_FILTER_MODES:
            return
        try:
            self.store.settle_aggressive_edge_v2_shadow_samples(
                round_id,
                outcome,
                now,
                final_price=final_price,
                target_price=target_price,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            trades = self.store.trades_for_round(round_id)
            packet = self._aggressive_edge_loss_replay.finalize_official_round(
                round_id,
                outcome,
                now=now,
                final_price=final_price,
                target_price=target_price,
                trades=trades,
            )
            if packet is not None:
                logger.debug(
                    "Aggressive Edge 输局复盘证据已记录 round_id=%s samples=%s path=%s",
                    round_id,
                    packet.get("sample_count"),
                    self._aggressive_edge_loss_replay.path,
                )
        except Exception as exc:  # noqa: BLE001 - 复盘落盘失败不能影响官方结算。
            logger.debug("Aggressive Edge 输局复盘落盘失败 round_id=%s error=%s", round_id, exc)

    def _apply_signal_side_mode(
        self,
        market: MarketRound,
        signal: Signal,
        quotes: dict[str, dict[str, Any]],
    ) -> Signal:
        """按实验组合改写信号方向；Reverse 只反向 Up/Down 有效信号。"""

        if self.signal_side_mode != SIGNAL_SIDE_MODE_REVERSE or signal.side not in {"Up", "Down"}:
            return signal

        reverse_side = "Down" if signal.side == "Up" else "Up"
        reverse_quote = quotes.get(reverse_side) if isinstance(quotes.get(reverse_side), dict) else {}
        reverse_quote = self._quote_with_depth(market, reverse_side, reverse_quote)
        reverse_ask = _maybe_float(reverse_quote.get("best_ask"))
        reverse_bid = _maybe_float(reverse_quote.get("best_bid"))
        reverse_ask_size = _maybe_float(reverse_quote.get("ask_size"))
        reverse_confidence = round(max(0.01, min(0.99, 1.0 - float(signal.confidence or 0.0))), 4)
        reverse_move_bps = -float(signal.move_bps or 0.0)
        base_note = (
            f"{SINGLE_REVERSE_MARKER} 原始信号 {signal.side}->反向下单 {reverse_side}, "
            f"原始入场价 {signal.entry_price:.4f}, 原始置信度 {signal.confidence:.4f}"
        )

        # 反向策略必须用实际下注方向的盘口做执行侧风控，避免拿原方向价格去买反方向合约。
        if reverse_ask is None or reverse_ask <= 0:
            return replace(
                signal,
                side="NO_TRADE",
                confidence=reverse_confidence,
                entry_price=0.0,
                move_bps=reverse_move_bps,
                reason=_append_reason_text(signal.reason, f"{base_note}, 缺少 {reverse_side} 卖一价"),
            )
        if reverse_ask > self.settings.max_entry_price:
            return replace(
                signal,
                side="NO_TRADE",
                confidence=reverse_confidence,
                entry_price=round(reverse_ask, 4),
                move_bps=reverse_move_bps,
                reason=_append_reason_text(
                    signal.reason,
                    f"{base_note}, 反向入场价格 {reverse_ask:.4f} 高于上限 {self.settings.max_entry_price:.4f}",
                ),
            )
        if reverse_bid is not None and reverse_ask - reverse_bid > self.settings.max_spread:
            return replace(
                signal,
                side="NO_TRADE",
                confidence=reverse_confidence,
                entry_price=round(reverse_ask, 4),
                move_bps=reverse_move_bps,
                reason=_append_reason_text(
                    signal.reason,
                    f"{base_note}, 反向盘口价差 {reverse_ask - reverse_bid:.4f} 过大",
                ),
            )
        if reverse_ask_size is not None and reverse_ask_size < self.settings.min_ask_size:
            return replace(
                signal,
                side="NO_TRADE",
                confidence=reverse_confidence,
                entry_price=round(reverse_ask, 4),
                move_bps=reverse_move_bps,
                reason=_append_reason_text(
                    signal.reason,
                    f"{base_note}, 反向卖盘深度 {reverse_ask_size:.4f} 不足",
                ),
            )

        note = (
            f"{base_note}, 反向入场价 {reverse_ask:.4f}, "
            f"反向置信度 {reverse_confidence:.4f}"
        )
        return replace(
            signal,
            side=reverse_side,
            confidence=reverse_confidence,
            entry_price=round(reverse_ask, 4),
            move_bps=reverse_move_bps,
            reason=_append_reason_text(signal.reason, note),
        )

    def _apply_signal_filter_mode(
        self,
        market: MarketRound,
        signal: Signal,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]] | None = None,
    ) -> Signal:
        """按实验组合过滤信号；默认 NONE 不改变旧策略行为。"""

        if self.signal_filter_mode not in AGGRESSIVE_EDGE_FILTER_MODES:
            return signal
        shadow_report: dict[str, Any] | None = None
        shadow_modes = {
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
        }
        v2_shadow_signal = self._aggressive_edge_v2_shadow_candidate(signal) if self.signal_filter_mode in shadow_modes else None
        if self.signal_filter_mode in shadow_modes and v2_shadow_signal is not None:
            v2_note, shadow_report = self._aggressive_edge_v2_shadow_report(market, v2_shadow_signal, price, quotes or {})
            if v2_note:
                signal = replace(signal, reason=_append_reason_text(signal.reason, v2_note))
            if shadow_report:
                self._record_aggressive_edge_v2_shadow_sample(market, signal, v2_shadow_signal, price, shadow_report)
        if signal.side not in {"Up", "Down"}:
            if (
                self.signal_filter_mode
                in {
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
                }
                and v2_shadow_signal is not None
            ):
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC:
                    guard_label = "V12_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V12"
                    block_reason = self._aggressive_edge_v12_reversal_guard_block_reason(market, v2_shadow_signal, shadow_report)
                elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC:
                    guard_label = "V11_DEPTH_MOMENTUM_GUARD_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V11"
                    block_reason = self._aggressive_edge_v11_depth_momentum_block_reason(market, v2_shadow_signal, shadow_report)
                elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC:
                    guard_label = "V10_UP_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V10"
                    block_reason = self._aggressive_edge_v10_up_reversal_guard_block_reason(market, v2_shadow_signal, shadow_report)
                elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC:
                    guard_label = "V9_M1_GUARD_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V9"
                    block_reason = self._aggressive_edge_v9_m1_guard_block_reason(market, v2_shadow_signal, shadow_report)
                elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC:
                    guard_label = "V8_LEARNING_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V8"
                    block_reason = self._aggressive_edge_v8_learning_block_reason(market, v2_shadow_signal, shadow_report)
                elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC:
                    guard_label = "V7_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V7"
                    block_reason = self._aggressive_edge_v7_block_reason(market, v2_shadow_signal, shadow_report)
                elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC:
                    guard_label = "V6_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V6"
                    block_reason = self._aggressive_edge_v6_block_reason(market, v2_shadow_signal, shadow_report)
                elif self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC:
                    guard_label = "V5_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V5"
                    block_reason = self._aggressive_edge_v5_block_reason(market, v2_shadow_signal, shadow_report)
                else:
                    guard_label = "V4_DIAGNOSTIC_NO_TRADE"
                    guard_name = "V4"
                    block_reason = self._aggressive_edge_v4_block_reason(market, v2_shadow_signal, shadow_report)
                if block_reason:
                    diagnostic_note = (
                        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} {guard_label}: "
                        f"基础信号已 NO_TRADE，影子候选只记录；{block_reason}"
                    )
                else:
                    diagnostic_note = (
                        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} {guard_label}: "
                        f"基础信号已 NO_TRADE，{guard_name} 守卫通过，基础不下注，只记录"
                    )
                return replace(signal, reason=_append_reason_text(signal.reason, diagnostic_note))
            return signal
        block_reason = aggressive_edge_block_reason(market, signal, price, self.settings.max_quote_age_ms)
        if block_reason:
            reason = _append_reason_text(signal.reason, block_reason)
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V4_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V4 候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V5_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V5 候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V6_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V6 候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V7_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V7 候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V8_LEARNING_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V8 学习样本只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V9_M1_GUARD_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V9 候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V10_UP_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V10 候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V11_DEPTH_MOMENTUM_GUARD_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V11 候选只记录不下注",
                )
            if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC:
                reason = _append_reason_text(
                    reason,
                    f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V12_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE: 基础过滤已拦截，V12 候选只记录不下注",
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=reason,
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1:
            learned_block_reason = self._aggressive_edge_learned_block_reason(market, signal, price)
            if learned_block_reason:
                logger.debug("Aggressive Edge V1 学习过滤拦截 round_id=%s reason=%s", market.round_id, learned_block_reason)
                return replace(
                    signal,
                    side="NO_TRADE",
                    reason=_append_reason_text(signal.reason, learned_block_reason),
                )
        v3_blocked = False
        if self.signal_filter_mode in {
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
        }:
            v3_note, v3_report = self._aggressive_edge_v3_guard_report(market, signal, shadow_report)
            if v3_note:
                signal = replace(signal, reason=_append_reason_text(signal.reason, v3_note))
            if v3_report and v3_report.get("block"):
                v3_blocked = True
                logger.debug(
                    "Aggressive Edge V3 直觉守卫拦截 round_id=%s report=%s",
                    market.round_id,
                    json.dumps(v3_report, ensure_ascii=False, sort_keys=True)[:1600],
                )
                if self.signal_filter_mode not in {
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
                    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
                }:
                    return replace(signal, side="NO_TRADE")
        note = aggressive_edge_pass_note(signal)
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC:
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} DIAGNOSTIC_NO_TRADE: "
                "旧 Aggressive Edge 交易版已暂停，当前候选只记录不下注"
            )
            return replace(signal, side="NO_TRADE", reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note))
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC:
            v4_block_reason = self._aggressive_edge_v4_block_reason(market, signal, shadow_report)
            if v4_block_reason:
                logger.debug("Aggressive Edge V4 诊断拦截 round_id=%s reason=%s", market.round_id, v4_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V4_DIAGNOSTIC_NO_TRADE: {v4_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V4_DIAGNOSTIC_NO_TRADE: "
                "V4 候选通过，只记录不下注"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V4 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC:
            v5_block_reason = self._aggressive_edge_v5_block_reason(market, signal, shadow_report)
            if v5_block_reason:
                logger.debug("Aggressive Edge V5 诊断拦截 round_id=%s reason=%s", market.round_id, v5_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V5_DIAGNOSTIC_NO_TRADE: {v5_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V5_DIAGNOSTIC_NO_TRADE: "
                "V5 候选通过，只记录不下注"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V5 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC:
            v6_block_reason = self._aggressive_edge_v6_block_reason(market, signal, shadow_report)
            if v6_block_reason:
                logger.debug("Aggressive Edge V6 诊断拦截 round_id=%s reason=%s", market.round_id, v6_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V6_DIAGNOSTIC_NO_TRADE: {v6_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V6_DIAGNOSTIC_NO_TRADE: "
                "V6 候选通过，只记录不下注"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V6 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC:
            v7_block_reason = self._aggressive_edge_v7_block_reason(market, signal, shadow_report)
            if v7_block_reason:
                logger.debug("Aggressive Edge V7 诊断拦截 round_id=%s reason=%s", market.round_id, v7_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V7_DIAGNOSTIC_NO_TRADE: {v7_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V7_DIAGNOSTIC_NO_TRADE: "
                "V7 候选通过，只记录不下注"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V7 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC:
            v8_block_reason = self._aggressive_edge_v8_learning_block_reason(market, signal, shadow_report)
            if v8_block_reason:
                logger.debug("Aggressive Edge V8 学习采样拦截 round_id=%s reason=%s", market.round_id, v8_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V8_LEARNING_DIAGNOSTIC_NO_TRADE: {v8_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            learning_tags = self._aggressive_edge_v8_learning_tags(market, signal, shadow_report)
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V8_LEARNING_DIAGNOSTIC_NO_TRADE: "
                f"V8 学习样本通过，只记录不下注；{learning_tags}"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V8 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC:
            v9_block_reason = self._aggressive_edge_v9_m1_guard_block_reason(market, signal, shadow_report)
            if v9_block_reason:
                logger.debug("Aggressive Edge V9 诊断拦截 round_id=%s reason=%s", market.round_id, v9_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V9_M1_GUARD_DIAGNOSTIC_NO_TRADE: {v9_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            learning_tags = self._aggressive_edge_v8_learning_tags(market, signal, shadow_report)
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V9_M1_GUARD_DIAGNOSTIC_NO_TRADE: "
                f"V9 候选通过，只记录不下注；{learning_tags}"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V9 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC:
            v10_block_reason = self._aggressive_edge_v10_up_reversal_guard_block_reason(market, signal, shadow_report)
            if v10_block_reason:
                logger.debug("Aggressive Edge V10 诊断拦截 round_id=%s reason=%s", market.round_id, v10_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V10_UP_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE: {v10_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            learning_tags = self._aggressive_edge_v8_learning_tags(market, signal, shadow_report)
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V10_UP_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE: "
                f"V10 候选通过，只记录不下注；{learning_tags}"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V10 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC:
            v11_block_reason = self._aggressive_edge_v11_depth_momentum_block_reason(market, signal, shadow_report)
            if v11_block_reason:
                logger.debug("Aggressive Edge V11 诊断拦截 round_id=%s reason=%s", market.round_id, v11_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V11_DEPTH_MOMENTUM_GUARD_DIAGNOSTIC_NO_TRADE: {v11_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            learning_tags = self._aggressive_edge_v8_learning_tags(market, signal, shadow_report)
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V11_DEPTH_MOMENTUM_GUARD_DIAGNOSTIC_NO_TRADE: "
                f"V11 候选通过，只记录不下注；{learning_tags}"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V11 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC:
            v12_block_reason = self._aggressive_edge_v12_reversal_guard_block_reason(market, signal, shadow_report)
            if v12_block_reason:
                logger.debug("Aggressive Edge V12 诊断拦截 round_id=%s reason=%s", market.round_id, v12_block_reason)
                diagnostic_note = f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V12_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE: {v12_block_reason}"
                return replace(signal, side="NO_TRADE", reason=_append_reason_text(signal.reason, diagnostic_note))
            learning_tags = self._aggressive_edge_v8_learning_tags(market, signal, shadow_report)
            diagnostic_note = (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V12_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE: "
                f"V12 候选通过，只记录不下注；{learning_tags}"
            )
            if v3_blocked:
                diagnostic_note = (
                    f"{diagnostic_note}；V3 直觉守卫已拦截，后续复盘需对比 V12 与 V3 的分歧"
                )
            return replace(
                signal,
                side="NO_TRADE",
                reason=_append_reason_text(_append_reason_text(signal.reason, note), diagnostic_note),
            )
        return replace(signal, reason=_append_reason_text(signal.reason, note))

    def _aggressive_edge_learned_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        price: dict[str, Any],
    ) -> str | None:
        # 只在 Aggressive Edge V1 执行二代学习过滤，基准组和 REAL 不调用该逻辑。
        v2_reason = aggressive_edge_paper_v2_block_reason(signal)
        if v2_reason:
            return v2_reason

        # 旧假突破规则继续作为补充证据，避免后续样本回到早段突拉形态时漏拦。
        signal_at = _updated_at_seconds(price.get("chainlink_updated_ms"), time.time())
        before60_tick = self.store.closest_price_tick(
            market.symbol or "BTC",
            signal_at - 60.0,
            max_distance_seconds=20.0,
            source_contains="chainlink",
        )
        return aggressive_edge_false_breakout_block_reason(
            market,
            signal,
            signal_at=signal_at,
            before60_tick=before60_tick,
        )

    def _aggressive_edge_v2_shadow_candidate(self, signal: Signal) -> Signal | None:
        """V2 影子采样候选：真实 Up/Down 信号直接采样，部分 NO_TRADE 按 bps 推断方向。"""

        if signal.side in {"Up", "Down"}:
            return signal
        entry_price = _maybe_float(signal.entry_price)
        move_bps = _maybe_float(signal.move_bps)
        if entry_price is None or entry_price <= 0 or move_bps is None or abs(move_bps) < 0.000001:
            return None
        side = "Up" if move_bps >= 0 else "Down"
        return replace(signal, side=side)

    def _aggressive_edge_sample_minute_bucket(self, market: MarketRound, now: float | None = None) -> int:
        """返回开局后的分钟桶，V4 用它区分抢第一波和等待确认的候选。"""

        current_ts = time.time() if now is None else float(now)
        seconds_from_start = max(0.0, current_ts - float(market.started_at or current_ts))
        return max(0, min(4, int(seconds_from_start // 60.0)))

    def _aggressive_edge_v2_shadow_report(
        self,
        market: MarketRound,
        signal: Signal,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> tuple[str | None, dict[str, Any] | None]:
        signal_at = _updated_at_seconds(price.get("chainlink_updated_ms"), time.time())
        before60_tick = self.store.closest_price_tick(
            market.symbol or "BTC",
            signal_at - 60.0,
            max_distance_seconds=20.0,
            source_contains="chainlink",
        )
        before30_tick = self.store.closest_price_tick(
            market.symbol or "BTC",
            signal_at - 30.0,
            max_distance_seconds=15.0,
            source_contains="chainlink",
        )
        quote = quotes.get(signal.side) if isinstance(quotes.get(signal.side), dict) else {}
        quote = self._quote_with_depth(market, signal.side, quote)
        report = aggressive_edge_v2_risk_report(
            market,
            signal,
            price=price,
            quote=quote,
            signal_at=signal_at,
            before60_tick=before60_tick,
            before30_tick=before30_tick,
            max_age_ms=self.settings.max_quote_age_ms,
        )
        return aggressive_edge_v2_risk_note(report), report

    def _aggressive_edge_v4_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V4 诊断守卫：用复盘出的反转结构判断候选是否只应记录，不应进入交易。"""

        if signal.side not in {"Up", "Down"}:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V4_GUARD BLOCK: 无有效方向候选"
        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
        entry_price = _maybe_float(signal.entry_price)
        if entry_price is None:
            entry_price = _maybe_float(features.get("entry_price"))
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        edge = _maybe_float(features.get("edge"))
        momentum_decay = _maybe_float(features.get("momentum_decay_bps"))
        depth_skew = _maybe_float(features.get("depth_skew"))
        top_level_skew = _maybe_float(features.get("top_level_skew"))

        reasons: list[str] = []
        # Up 前两分钟必须有明显加速，否则容易买在第一波假突破。
        up_strong_acceleration = (
            signal.side == "Up"
            and momentum_decay is not None
            and momentum_decay <= -10.0
            and edge is not None
            and edge >= 0.20
        )
        if signal.side == "Up" and minute_bucket < 2 and not up_strong_acceleration:
            reasons.append(f"V4_UP_WAIT_CONFIRM m{minute_bucket}: Up 前两分钟缺少强加速")
        if minute_bucket == 0 and entry_price is not None and entry_price >= 0.70:
            reasons.append(f"V4_FIRST_MINUTE_HIGH_ENTRY m0 entry={entry_price:.4f}")
        if abs_move_bps >= 15.0:
            reasons.append(f"V4_OVEREXTENDED_MOVE abs_move={abs_move_bps:.2f}bps")
        if (
            momentum_decay is not None
            and edge is not None
            and -10.0 <= momentum_decay < 5.0
            and edge >= 0.20
        ):
            reasons.append(f"V4_FLAT_DECAY_HIGH_EDGE decay={momentum_decay:.2f} edge={edge:.4f}")
        if (
            depth_skew is not None
            and top_level_skew is not None
            and edge is not None
            and depth_skew < 0.50
            and top_level_skew < 0.50
            and edge >= 0.20
        ):
            reasons.append(
                f"V4_WEAK_BOOK_HIGH_EDGE depth={depth_skew:.4f} top={top_level_skew:.4f} edge={edge:.4f}"
            )
        if not reasons:
            return None
        metrics = (
            f"m{minute_bucket} side={signal.side} entry={_format_optional_float(entry_price, 4)} "
            f"abs_move={abs_move_bps:.2f} edge={_format_optional_float(edge, 4)} "
            f"decay={_format_optional_float(momentum_decay, 2)} "
            f"depth={_format_optional_float(depth_skew, 4)} top={_format_optional_float(top_level_skew, 4)}"
        )
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V4_GUARD BLOCK: {'; '.join(reasons)} | {metrics}"

    def _aggressive_edge_v5_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V5 诊断守卫：复盘 V4 后收紧 Down 反转局，只做影子采样。"""

        v4_block_reason = self._aggressive_edge_v4_block_reason(market, signal, report, now=now)
        if v4_block_reason:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V5_GUARD BLOCK: V4 守卫未通过；{v4_block_reason}"
        if signal.side not in {"Up", "Down"}:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V5_GUARD BLOCK: 无有效方向候选"

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
        entry_price = _maybe_float(signal.entry_price)
        if entry_price is None:
            entry_price = _maybe_float(features.get("entry_price"))
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        depth_skew = _maybe_float(features.get("depth_skew"))
        top_level_skew = _maybe_float(features.get("top_level_skew"))

        reasons: list[str] = []
        # Up 的有效样本主要来自 m2 以后，开局抢跑继续留给 V4 历史对照。
        if signal.side == "Up" and minute_bucket < AGGRESSIVE_EDGE_V5_UP_MIN_BUCKET:
            reasons.append(f"V5_UP_WAIT_M2 m{minute_bucket}: Up 只验证 m2 以后候选")
        if signal.side == "Down":
            if minute_bucket not in AGGRESSIVE_EDGE_V5_DOWN_ALLOWED_BUCKETS:
                reasons.append(f"V5_DOWN_BUCKET_BLOCK m{minute_bucket}: Down 只验证 m2/m3")
            if entry_price is None:
                reasons.append("V5_DOWN_ENTRY_MISSING: Down 缺少入场价")
            elif entry_price >= AGGRESSIVE_EDGE_V5_DOWN_MAX_ENTRY_PRICE:
                reasons.append(
                    f"V5_DOWN_HIGH_ENTRY entry={entry_price:.4f}: 要求 entry<{AGGRESSIVE_EDGE_V5_DOWN_MAX_ENTRY_PRICE:.2f}"
                )
            if depth_skew is None:
                reasons.append("V5_DOWN_DEPTH_MISSING: Down 缺少 depth_skew")
            elif depth_skew < AGGRESSIVE_EDGE_V5_DOWN_MIN_DEPTH_SKEW:
                reasons.append(
                    f"V5_DOWN_WEAK_DEPTH depth={depth_skew:.4f}: 要求 depth>={AGGRESSIVE_EDGE_V5_DOWN_MIN_DEPTH_SKEW:.2f}"
                )
            if top_level_skew is None:
                reasons.append("V5_DOWN_TOP_MISSING: Down 缺少 top_level_skew")
            elif top_level_skew < AGGRESSIVE_EDGE_V5_DOWN_MIN_TOP_LEVEL_SKEW:
                reasons.append(
                    f"V5_DOWN_WEAK_TOP top={top_level_skew:.4f}: 要求 top>={AGGRESSIVE_EDGE_V5_DOWN_MIN_TOP_LEVEL_SKEW:.2f}"
                )
            if abs_move_bps >= AGGRESSIVE_EDGE_V5_DOWN_MAX_ABS_MOVE_BPS:
                reasons.append(
                    f"V5_DOWN_OVEREXTENDED abs_move={abs_move_bps:.2f}bps: 要求 abs_move<{AGGRESSIVE_EDGE_V5_DOWN_MAX_ABS_MOVE_BPS:.2f}bps"
                )

        if not reasons:
            return None
        metrics = (
            f"m{minute_bucket} side={signal.side} entry={_format_optional_float(entry_price, 4)} "
            f"abs_move={abs_move_bps:.2f} depth={_format_optional_float(depth_skew, 4)} "
            f"top={_format_optional_float(top_level_skew, 4)}"
        )
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V5_GUARD BLOCK: {'; '.join(reasons)} | {metrics}"

    def _aggressive_edge_v6_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V6 诊断守卫：继承 V5 后，只验证低风险且非极端位移候选。"""

        v5_block_reason = self._aggressive_edge_v5_block_reason(market, signal, report, now=now)
        if v5_block_reason:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V6_GUARD BLOCK: V5 守卫未通过；{v5_block_reason}"
        if signal.side not in {"Up", "Down"}:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V6_GUARD BLOCK: 无有效方向候选"

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        risk_score = _maybe_float(report.get("risk_score")) if isinstance(report, dict) else None
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)

        reasons: list[str] = []
        if risk_score is None:
            reasons.append("V6_RISK_SCORE_MISSING: 缺少 risk_score")
        elif risk_score >= AGGRESSIVE_EDGE_V6_MAX_RISK_SCORE:
            reasons.append(
                f"V6_RISK_SCORE_HIGH risk={risk_score:.4f}: 要求 risk<{AGGRESSIVE_EDGE_V6_MAX_RISK_SCORE:.2f}"
            )
        if abs_move_bps >= AGGRESSIVE_EDGE_V6_MAX_ABS_MOVE_BPS:
            reasons.append(
                f"V6_EXTREME_MOVE abs_move={abs_move_bps:.2f}bps: 要求 abs_move<{AGGRESSIVE_EDGE_V6_MAX_ABS_MOVE_BPS:.2f}bps"
            )

        if not reasons:
            return None
        metrics = (
            f"m{minute_bucket} side={signal.side} risk={_format_optional_float(risk_score, 4)} "
            f"abs_move={abs_move_bps:.2f}"
        )
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V6_GUARD BLOCK: {'; '.join(reasons)} | {metrics}"

    def _aggressive_edge_v7_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V7 诊断守卫：继承 V6 后，验证 Up 盘口深度支撑和 Down 赔率约束。"""

        v6_block_reason = self._aggressive_edge_v6_block_reason(market, signal, report, now=now)
        if v6_block_reason:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V7_GUARD BLOCK: V6 守卫未通过；{v6_block_reason}"
        if signal.side not in {"Up", "Down"}:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V7_GUARD BLOCK: 无有效方向候选"

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
        entry_price = _maybe_float(signal.entry_price)
        if entry_price is None:
            entry_price = _maybe_float(features.get("entry_price"))
        depth_skew = _maybe_float(features.get("depth_skew"))
        top_level_skew = _maybe_float(features.get("top_level_skew"))
        risk_score = _maybe_float(report.get("risk_score")) if isinstance(report, dict) else None
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)

        reasons: list[str] = []
        if signal.side == "Up":
            # V6 输单集中在 Up 低风险但盘口支撑不足的局，V7 按时间桶验证不同深度门槛。
            if depth_skew is None:
                reasons.append("V7_UP_DEPTH_MISSING: Up 缺少 depth_skew")
            elif minute_bucket == 2 and depth_skew < AGGRESSIVE_EDGE_V7_UP_M2_MIN_DEPTH_SKEW:
                reasons.append(
                    f"V7_UP_M2_WEAK_DEPTH depth={depth_skew:.4f}: 要求 depth>={AGGRESSIVE_EDGE_V7_UP_M2_MIN_DEPTH_SKEW:.2f}"
                )
            elif minute_bucket == 3 and depth_skew < AGGRESSIVE_EDGE_V7_UP_M3_MIN_DEPTH_SKEW:
                reasons.append(
                    f"V7_UP_M3_WEAK_DEPTH depth={depth_skew:.4f}: 要求 depth>={AGGRESSIVE_EDGE_V7_UP_M3_MIN_DEPTH_SKEW:.2f}"
                )
            elif minute_bucket not in {2, 3}:
                reasons.append(f"V7_UP_BUCKET_BLOCK m{minute_bucket}: Up 只验证 m2/m3")
        if signal.side == "Down":
            # Down 方向 V6 有正收益，但高买价压低赔率，V7 单独验证更低入场上限。
            if entry_price is None:
                reasons.append("V7_DOWN_ENTRY_MISSING: Down 缺少入场价")
            elif entry_price > AGGRESSIVE_EDGE_V7_DOWN_MAX_ENTRY_PRICE:
                reasons.append(
                    f"V7_DOWN_HIGH_ENTRY entry={entry_price:.4f}: 要求 entry<={AGGRESSIVE_EDGE_V7_DOWN_MAX_ENTRY_PRICE:.2f}"
                )

        if not reasons:
            return None
        metrics = (
            f"m{minute_bucket} side={signal.side} entry={_format_optional_float(entry_price, 4)} "
            f"risk={_format_optional_float(risk_score, 4)} abs_move={abs_move_bps:.2f} "
            f"depth={_format_optional_float(depth_skew, 4)} top={_format_optional_float(top_level_skew, 4)}"
        )
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V7_GUARD BLOCK: {'; '.join(reasons)} | {metrics}"

    def _aggressive_edge_v8_learning_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V8 学习采样守卫：只拦截极端或字段缺失样本，主要目标是提高复盘样本速度。"""

        if signal.side not in {"Up", "Down"}:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V8_LEARNING BLOCK: 无有效方向候选"

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
        entry_price = _maybe_float(signal.entry_price)
        if entry_price is None:
            entry_price = _maybe_float(features.get("entry_price"))
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        risk_score = _maybe_float(report.get("risk_score")) if isinstance(report, dict) else None

        reasons: list[str] = []
        # V8 要扩大样本，只有缺少核心字段或极端风险时才拦截；普通弱盘口保留为学习标签。
        if entry_price is None:
            reasons.append("V8_ENTRY_MISSING: 缺少入场价")
        if risk_score is None:
            reasons.append("V8_RISK_SCORE_MISSING: 缺少 risk_score")
        elif risk_score >= AGGRESSIVE_EDGE_V8_MAX_RISK_SCORE:
            reasons.append(
                f"V8_RISK_SCORE_EXTREME risk={risk_score:.4f}: 要求 risk<{AGGRESSIVE_EDGE_V8_MAX_RISK_SCORE:.2f}"
            )
        if abs_move_bps >= AGGRESSIVE_EDGE_V8_MAX_ABS_MOVE_BPS:
            reasons.append(
                f"V8_EXTREME_MOVE abs_move={abs_move_bps:.2f}bps: 要求 abs_move<{AGGRESSIVE_EDGE_V8_MAX_ABS_MOVE_BPS:.2f}bps"
            )
        if minute_bucket not in AGGRESSIVE_EDGE_V8_ALLOWED_BUCKETS:
            reasons.append(f"V8_BUCKET_INVALID m{minute_bucket}: 非 5 分钟局有效采样桶")

        if not reasons:
            return None
        metrics = self._aggressive_edge_v8_learning_tags(market, signal, report, now=now)
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V8_LEARNING BLOCK: {'; '.join(reasons)} | {metrics}"

    def _aggressive_edge_v8_learning_tags(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str:
        """生成 V8 复盘标签，帮助后续从输单里找反转共性。"""

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
        entry_price = _maybe_float(signal.entry_price)
        if entry_price is None:
            entry_price = _maybe_float(features.get("entry_price"))
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        risk_score = _maybe_float(report.get("risk_score")) if isinstance(report, dict) else None
        risk_level = str(report.get("risk_level") or "") if isinstance(report, dict) else ""
        depth_skew = _maybe_float(features.get("depth_skew"))
        top_level_skew = _maybe_float(features.get("top_level_skew"))
        momentum_decay = _maybe_float(features.get("momentum_decay_bps"))
        edge = _maybe_float(features.get("edge"))

        tags: list[str] = [f"m{minute_bucket}", f"side={signal.side}"]
        if minute_bucket == 0:
            tags.append("early_m0")
        if entry_price is not None and entry_price >= 0.70:
            tags.append("high_entry")
        if entry_price is not None and entry_price < 0.50:
            tags.append("low_entry")
        if depth_skew is not None and depth_skew < 0.35:
            tags.append("weak_depth")
        if top_level_skew is not None and top_level_skew < 0.35:
            tags.append("weak_top")
        if momentum_decay is not None and momentum_decay > 0:
            tags.append("momentum_decay")
        if edge is not None and edge < 0.06:
            tags.append("thin_edge")
        if abs_move_bps >= 10.0:
            tags.append("wide_move")
        if risk_level:
            tags.append(f"risk_level={risk_level}")
        if risk_score is not None:
            tags.append(f"risk={risk_score:.4f}")
        tags.append(f"entry={_format_optional_float(entry_price, 4)}")
        tags.append(f"abs_move={abs_move_bps:.2f}")
        tags.append(f"depth={_format_optional_float(depth_skew, 4)}")
        tags.append(f"top={_format_optional_float(top_level_skew, 4)}")
        tags.append(f"decay={_format_optional_float(momentum_decay, 2)}")
        return " ".join(tags)

    def _aggressive_edge_v9_m1_guard_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V9 诊断守卫：继承 V8 后屏蔽 m1，验证 V8 最大亏损桶是否应永久剔除。"""

        v8_block_reason = self._aggressive_edge_v8_learning_block_reason(market, signal, report, now=now)
        if v8_block_reason:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V9_M1_GUARD BLOCK: V8 守卫未通过；{v8_block_reason}"
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
        if minute_bucket in AGGRESSIVE_EDGE_V9_BLOCKED_BUCKETS:
            metrics = self._aggressive_edge_v8_learning_tags(market, signal, report, now=now)
            return (
                f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V9_M1_GUARD BLOCK: "
                f"V9_M1_BUCKET_BLOCK m{minute_bucket}: V8 复盘 m1 胜率和 ROI 明显劣化 | {metrics}"
            )
        return None

    def _aggressive_edge_v10_up_reversal_guard_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V10 诊断守卫：继承 V9 后拦截 Up 动能不足和顶层盘口支撑弱的反转候选。"""

        v9_block_reason = self._aggressive_edge_v9_m1_guard_block_reason(market, signal, report, now=now)
        if v9_block_reason:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V10_UP_REVERSAL_GUARD BLOCK: V9 守卫未通过；{v9_block_reason}"
        if signal.side != "Up":
            return None

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        top_level_skew = _maybe_float(features.get("top_level_skew"))
        reasons: list[str] = []
        if abs_move_bps < AGGRESSIVE_EDGE_V10_UP_MIN_ABS_MOVE_BPS:
            reasons.append(
                f"V10_UP_WEAK_MOVE abs_move={abs_move_bps:.2f}bps: 要求 abs_move>={AGGRESSIVE_EDGE_V10_UP_MIN_ABS_MOVE_BPS:.2f}bps"
            )
        if top_level_skew is None:
            reasons.append("V10_UP_TOP_SKEW_MISSING: 缺少顶层盘口支撑")
        elif top_level_skew < AGGRESSIVE_EDGE_V10_UP_MIN_TOP_LEVEL_SKEW:
            reasons.append(
                f"V10_UP_WEAK_TOP_SKEW top={top_level_skew:.4f}: 要求 top>={AGGRESSIVE_EDGE_V10_UP_MIN_TOP_LEVEL_SKEW:.2f}"
            )
        if not reasons:
            return None
        metrics = self._aggressive_edge_v8_learning_tags(market, signal, report, now=now)
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V10_UP_REVERSAL_GUARD BLOCK: {'; '.join(reasons)} | {metrics}"

    def _aggressive_edge_v11_depth_momentum_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V11 诊断守卫：用全样本验证过的 m2/m3 深盘口强波动低风险规则筛实盘候选。"""

        v8_block_reason = self._aggressive_edge_v8_learning_block_reason(market, signal, report, now=now)
        if v8_block_reason:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V11_DEPTH_MOMENTUM_GUARD BLOCK: V8 守卫未通过；{v8_block_reason}"

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        depth_skew = _maybe_float(features.get("depth_skew"))
        risk_score = _maybe_float(report.get("risk_score")) if isinstance(report, dict) else None

        reasons: list[str] = []
        if minute_bucket not in AGGRESSIVE_EDGE_V11_ALLOWED_BUCKETS:
            reasons.append(f"V11_BUCKET_BLOCK m{minute_bucket}: 只放行 m2/m3")
        if abs_move_bps < AGGRESSIVE_EDGE_V11_MIN_ABS_MOVE_BPS:
            reasons.append(
                f"V11_WEAK_MOVE abs_move={abs_move_bps:.2f}bps: 要求 abs_move>={AGGRESSIVE_EDGE_V11_MIN_ABS_MOVE_BPS:.2f}bps"
            )
        if depth_skew is None:
            reasons.append("V11_DEPTH_SKEW_MISSING: 缺少盘口深度偏斜")
        elif depth_skew < AGGRESSIVE_EDGE_V11_MIN_DEPTH_SKEW:
            reasons.append(
                f"V11_WEAK_DEPTH depth={depth_skew:.4f}: 要求 depth>={AGGRESSIVE_EDGE_V11_MIN_DEPTH_SKEW:.2f}"
            )
        if risk_score is None:
            reasons.append("V11_RISK_SCORE_MISSING: 缺少 risk_score")
        elif risk_score > AGGRESSIVE_EDGE_V11_MAX_RISK_SCORE:
            reasons.append(
                f"V11_RISK_TOO_HIGH risk={risk_score:.4f}: 要求 risk<={AGGRESSIVE_EDGE_V11_MAX_RISK_SCORE:.2f}"
            )
        if not reasons:
            return None
        metrics = self._aggressive_edge_v8_learning_tags(market, signal, report, now=now)
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V11_DEPTH_MOMENTUM_GUARD BLOCK: {'; '.join(reasons)} | {metrics}"

    def _aggressive_edge_v12_reversal_guard_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        report: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """V12 诊断守卫：继承 V11 后，验证高位移和 Down 顶层盘口不足是否属于可复用反转风险。"""

        v11_block_reason = self._aggressive_edge_v11_depth_momentum_block_reason(market, signal, report, now=now)
        if v11_block_reason:
            return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V12_REVERSAL_GUARD BLOCK: V11 守卫未通过；{v11_block_reason}"

        features = report.get("features") if isinstance(report, dict) and isinstance(report.get("features"), dict) else {}
        move_bps = _maybe_float(signal.move_bps)
        if move_bps is None:
            move_bps = _maybe_float(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        top_level_skew = _maybe_float(features.get("top_level_skew"))
        depth_skew = _maybe_float(features.get("depth_skew"))
        risk_score = _maybe_float(report.get("risk_score")) if isinstance(report, dict) else None
        minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)

        reasons: list[str] = []
        # V11 复盘显示 abs_move>=8bps 胜率明显回落，先作为 V12 影子候选验证，不直接替换实盘。
        if abs_move_bps >= AGGRESSIVE_EDGE_V12_MAX_ABS_MOVE_BPS:
            reasons.append(
                f"V12_OVEREXTENDED_MOVE abs_move={abs_move_bps:.2f}bps: 要求 abs_move<{AGGRESSIVE_EDGE_V12_MAX_ABS_MOVE_BPS:.2f}bps"
            )
        if signal.side == "Up":
            if top_level_skew is None:
                reasons.append("V12_UP_TOP_SKEW_MISSING: Up 缺少顶层盘口支撑")
            elif top_level_skew < AGGRESSIVE_EDGE_V12_UP_MIN_TOP_LEVEL_SKEW:
                reasons.append(
                    f"V12_UP_WEAK_TOP_SKEW top={top_level_skew:.4f}: 要求 top>={AGGRESSIVE_EDGE_V12_UP_MIN_TOP_LEVEL_SKEW:.2f}"
                )
        if signal.side == "Down":
            if top_level_skew is None:
                reasons.append("V12_DOWN_TOP_SKEW_MISSING: Down 缺少顶层盘口支撑")
            elif top_level_skew < AGGRESSIVE_EDGE_V12_DOWN_MIN_TOP_LEVEL_SKEW:
                reasons.append(
                    f"V12_DOWN_WEAK_TOP_SKEW top={top_level_skew:.4f}: 要求 top>={AGGRESSIVE_EDGE_V12_DOWN_MIN_TOP_LEVEL_SKEW:.2f}"
                )

        if not reasons:
            return None
        metrics = (
            f"m{minute_bucket} side={signal.side} abs_move={abs_move_bps:.2f} "
            f"depth={_format_optional_float(depth_skew, 4)} top={_format_optional_float(top_level_skew, 4)} "
            f"risk={_format_optional_float(risk_score, 4)}"
        )
        return f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V12_REVERSAL_GUARD BLOCK: {'; '.join(reasons)} | {metrics}"

    def _record_aggressive_edge_v2_shadow_sample(
        self,
        market: MarketRound,
        source_signal: Signal,
        shadow_signal: Signal,
        price: dict[str, Any],
        report: dict[str, Any],
    ) -> None:
        """持久化 V2 影子候选，结算后可反推当时下注会赢还是会输。"""

        if self.signal_filter_mode not in {
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
        }:
            return
        if market.target_price <= 0 or shadow_signal.side not in {"Up", "Down"}:
            return
        now = time.time()
        try:
            base_block_reason = aggressive_edge_block_reason(market, shadow_signal, price, self.settings.max_quote_age_ms)
            v1_block_reason = self._aggressive_edge_learned_block_reason(market, shadow_signal, price) if base_block_reason is None else None
            minute_bucket = self._aggressive_edge_sample_minute_bucket(market, now)
            base_would_trade = base_block_reason is None
            v1_would_trade = base_would_trade and v1_block_reason is None
            v4_block_reason = None
            v5_block_reason = None
            v6_block_reason = None
            v7_block_reason = None
            v8_block_reason = None
            v9_block_reason = None
            v10_block_reason = None
            v11_block_reason = None
            v12_block_reason = None
            if base_would_trade:
                v4_block_reason = self._aggressive_edge_v4_block_reason(market, shadow_signal, report, now=now)
                v5_block_reason = self._aggressive_edge_v5_block_reason(market, shadow_signal, report, now=now)
                v6_block_reason = self._aggressive_edge_v6_block_reason(market, shadow_signal, report, now=now)
                v7_block_reason = self._aggressive_edge_v7_block_reason(market, shadow_signal, report, now=now)
                v8_block_reason = self._aggressive_edge_v8_learning_block_reason(market, shadow_signal, report, now=now)
                v9_block_reason = self._aggressive_edge_v9_m1_guard_block_reason(market, shadow_signal, report, now=now)
                v10_block_reason = self._aggressive_edge_v10_up_reversal_guard_block_reason(market, shadow_signal, report, now=now)
                v11_block_reason = self._aggressive_edge_v11_depth_momentum_block_reason(market, shadow_signal, report, now=now)
                v12_block_reason = self._aggressive_edge_v12_reversal_guard_block_reason(market, shadow_signal, report, now=now)
            else:
                v4_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v5_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v6_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v7_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v8_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v9_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v10_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v11_block_reason = f"基础过滤已拦截: {base_block_reason}"
                v12_block_reason = f"基础过滤已拦截: {base_block_reason}"
            v4_would_trade = base_would_trade and v4_block_reason is None
            v5_would_trade = base_would_trade and v5_block_reason is None
            v6_would_trade = base_would_trade and v6_block_reason is None
            v7_would_trade = base_would_trade and v7_block_reason is None
            v8_would_trade = base_would_trade and v8_block_reason is None
            v9_would_trade = base_would_trade and v9_block_reason is None
            v10_would_trade = base_would_trade and v10_block_reason is None
            v11_would_trade = base_would_trade and v11_block_reason is None
            v12_would_trade = base_would_trade and v12_block_reason is None
            sample_key = f"m{minute_bucket}:{'pass' if base_would_trade else 'block'}"
            self.store.record_aggressive_edge_v2_shadow_sample(
                round_id=market.round_id,
                symbol=market.symbol or "BTC",
                sample_key=sample_key,
                side=shadow_signal.side,
                source_signal_side=source_signal.side,
                base_would_trade=base_would_trade,
                v1_would_trade=v1_would_trade,
                v2_would_trade=base_would_trade,
                v4_would_trade=v4_would_trade,
                v5_would_trade=v5_would_trade,
                v6_would_trade=v6_would_trade,
                v7_would_trade=v7_would_trade,
                v8_would_trade=v8_would_trade,
                v9_would_trade=v9_would_trade,
                v10_would_trade=v10_would_trade,
                v11_would_trade=v11_would_trade,
                v12_would_trade=v12_would_trade,
                entry_price=_maybe_float(shadow_signal.entry_price),
                confidence=_maybe_float(shadow_signal.confidence),
                move_bps=_maybe_float(shadow_signal.move_bps),
                report=report,
                base_block_reason=base_block_reason,
                v1_block_reason=v1_block_reason,
                v4_block_reason=v4_block_reason,
                v5_block_reason=v5_block_reason,
                v6_block_reason=v6_block_reason,
                v7_block_reason=v7_block_reason,
                v8_block_reason=v8_block_reason,
                v9_block_reason=v9_block_reason,
                v10_block_reason=v10_block_reason,
                v11_block_reason=v11_block_reason,
                v12_block_reason=v12_block_reason,
                signal_reason=source_signal.reason,
                created_at=now,
            )
        except Exception as exc:  # noqa: BLE001 - 影子采样不能影响策略主循环。
            logger.debug("Aggressive Edge V2 影子样本记录失败 round_id=%s error=%s", market.round_id, exc)

    def _aggressive_edge_v3_guard_report(
        self,
        market: MarketRound,
        signal: Signal,
        shadow_report: dict[str, Any] | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """读取历史输局指纹，判断当前候选是否属于负期望相似局。"""

        report = aggressive_edge_v3_guard_report(
            signal,
            shadow_report,
            source_db_paths=self._aggressive_edge_v3_source_db_paths(),
            loss_replay_paths=self._aggressive_edge_v3_loss_replay_paths(),
            symbol=market.symbol or "BTC",
        )
        return aggressive_edge_v3_guard_note(report), report

    def _aggressive_edge_v3_source_db_paths(self) -> list[Path]:
        """V3 的经验来源：历史 Aggressive Edge 系列实验库和自身后续积累。"""

        parent = self.settings.db_path.parent
        return [
            parent / "single_fak_aggressive_edge.sqlite3",
            parent / "single_fak_aggressive_edge_v1.sqlite3",
            parent / "single_fak_aggressive_edge_v2.sqlite3",
            parent / "single_fak_aggressive_edge_v3.sqlite3",
            parent / "single_fak_aggressive_edge_diagnostic.sqlite3",
            *(
                [parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3"]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v6_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v6_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v7_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v6_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v7_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v8_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v6_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v7_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v8_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v9_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v6_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v7_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v8_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v9_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v10_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v6_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v7_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v8_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v9_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v10_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v11_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC
                else []
            ),
            *(
                [
                    parent / "single_fak_aggressive_edge_v4_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v5_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v6_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v7_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v8_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v9_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v10_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v11_diagnostic.sqlite3",
                    parent / "single_fak_aggressive_edge_v12_diagnostic.sqlite3",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC
                else []
            ),
        ]

    def _aggressive_edge_v3_loss_replay_paths(self) -> list[Path]:
        """V3 的整盘输局证据来源，JSONL 只读加载并带文件签名缓存。"""

        replay_dir = self.settings.db_path.parent / "loss-replays"
        return [
            replay_dir / "single_fak_aggressive_edge.jsonl",
            replay_dir / "single_fak_aggressive_edge_v1.jsonl",
            replay_dir / "single_fak_aggressive_edge_v2.jsonl",
            replay_dir / "single_fak_aggressive_edge_v3.jsonl",
            replay_dir / "single_fak_aggressive_edge_diagnostic.jsonl",
            *(
                [replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl"]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v7_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v7_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v8_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v7_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v8_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v9_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v7_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v8_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v9_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v10_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v7_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v8_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v9_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v10_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v11_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC
                else []
            ),
            *(
                [
                    replay_dir / "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v7_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v8_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v9_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v10_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v11_diagnostic.jsonl",
                    replay_dir / "single_fak_aggressive_edge_v12_diagnostic.jsonl",
                ]
                if self.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC
                else []
            ),
        ]

    def _manage_resting_orders(self, market: MarketRound, quotes: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        self.store.expire_resting_orders(now)
        quotes = self._quotes_with_depth(market, quotes)
        for order in self.store.active_paper_orders("BTC"):
            if str(order.get("round_id") or "") != market.round_id:
                continue
            side = str(order.get("side") or "")
            quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
            fill = self._resting_order_fill(order, quote, now)
            if not fill:
                continue
            self.store.fill_resting_order(order, now=now, **fill)

    def _run_realtime_maker_strategy_from_state(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        now = time.time()
        quotes = self._quotes_with_depth(market, quotes)
        state = _realtime_maker_state(market, price, quotes, now, self.settings.max_quote_age_ms)
        if self._manage_realtime_maker_positions(market, state, quotes, now):
            return
        self._cancel_stale_realtime_maker_orders(market, state, quotes, now)
        if state.get("block_reason"):
            self._set_realtime_maker_signal(state, str(state["block_reason"]))
            return
        round_open_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        if round_open_rows:
            self._set_realtime_maker_signal(state, "REALTIME_MAKER_MANAGE 当前市场已有持仓，等待退出规则")
            return
        if self.store.active_paper_order_exists_for_round(market.round_id):
            self._set_realtime_maker_signal(state, "REALTIME_MAKER_WAIT 当前市场已有 maker 挂单")
            return
        reason = self._realtime_maker_entry_block_reason(market, state, now)
        if reason:
            self._set_realtime_maker_signal(state, reason)
            return
        self._place_realtime_maker_order(market, state, quotes, now)

    def _manage_realtime_maker_positions(
        self,
        market: MarketRound,
        state: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        now: float,
    ) -> bool:
        rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC"
            and row.get("round_id") == market.round_id
            and REALTIME_MAKER_MARKER in str(row.get("reason") or "")
        ]
        if not rows:
            return False
        time_left = market.ends_at - now
        managed = False
        for side in ("Up", "Down"):
            side_rows = [row for row in rows if row.get("side") == side]
            if not side_rows:
                continue
            side_fair = _realtime_maker_side_fair(state, side)
            quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
            bid = _maybe_float(quote.get("best_bid"))
            if bid is None or bid <= 0:
                continue
            shares = sum(_maybe_float(row.get("shares")) or 0.0 for row in side_rows)
            stake = sum(_maybe_float(row.get("stake")) or 0.0 for row in side_rows)
            entry_price = stake / shares if shares > PAIR_EPSILON else 0.0
            opened_at = min(_maybe_float(row.get("opened_at")) or now for row in side_rows)
            age = max(0.0, now - opened_at)
            exit_reason = _realtime_maker_exit_reason(side, side_fair, entry_price, bid, age, time_left)
            if not exit_reason:
                continue
            closed = self._close_side_shares(side_rows, side, shares, bid, now, exit_reason)
            if closed:
                managed = True
                self._set_realtime_maker_signal(state, exit_reason)
        return managed

    def _cancel_stale_realtime_maker_orders(
        self,
        market: MarketRound,
        state: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        now: float,
    ) -> None:
        for order in self.store.active_paper_orders("BTC", market.round_id):
            if REALTIME_MAKER_MARKER not in str(order.get("reason") or ""):
                continue
            side = str(order.get("side") or "")
            limit_price = _maybe_float(order.get("limit_price"))
            if side not in {"Up", "Down"} or limit_price is None:
                continue
            created_at = _maybe_float(order.get("created_at")) or now
            order_age = max(0.0, now - created_at)
            side_fair = _realtime_maker_side_fair(state, side)
            quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
            best_ask = _maybe_float(quote.get("best_ask"))
            reason = _realtime_maker_cancel_reason(
                market,
                state,
                side,
                side_fair,
                limit_price,
                best_ask,
                order_age,
                now,
            )
            if reason:
                self.store.cancel_paper_order(int(order["id"]), reason, now)

    def _realtime_maker_entry_block_reason(self, market: MarketRound, state: dict[str, Any], now: float) -> str | None:
        time_left = market.ends_at - now
        if market.target_price <= 0:
            return "REALTIME_MAKER_WAIT 缺少官方目标价"
        if self.store.daily_realized_pnl() <= -abs(self.settings.max_daily_loss):
            return "REALTIME_MAKER_WAIT 日内亏损达到停止线"
        if time_left <= REALTIME_MAKER_STOP_ENTRY_SECONDS_LEFT:
            return "REALTIME_MAKER_WAIT 临近结算停止新增 maker 挂单"
        if time_left > self.settings.max_time_left_seconds:
            return "REALTIME_MAKER_WAIT 市场刚开始，等待盘口稳定"
        if self.store.open_trade_count("BTC") >= self.settings.max_open_trades:
            return "REALTIME_MAKER_WAIT 持仓数达到上限"
        account = self.store.account()
        if float(account["cash_balance"]) < 0.1:
            return "REALTIME_MAKER_WAIT 纸交易可用资金不足"
        side = str(state.get("side") or "")
        side_fair = _maybe_float(state.get("side_fair"))
        limit_price = _maybe_float(state.get("limit_price"))
        best_ask = _maybe_float(state.get("best_ask"))
        actor_side_fair = _maybe_float(state.get("actor_side_fair"))
        if side not in {"Up", "Down"} or side_fair is None:
            return "REALTIME_MAKER_WAIT 实时 fair value 不足"
        if side_fair < REALTIME_MAKER_ENTRY_MIN_FAIR:
            return f"REALTIME_MAKER_WAIT fair {side_fair:.4f} 低于 {REALTIME_MAKER_ENTRY_MIN_FAIR:.2f}"
        if actor_side_fair is not None and actor_side_fair < REALTIME_MAKER_ACTOR_BLOCK_THRESHOLD:
            return f"REALTIME_MAKER_WAIT 地址修正反向 {actor_side_fair:.4f}"
        if limit_price is None or limit_price <= 0:
            return "REALTIME_MAKER_WAIT 缺少可挂限价"
        edge = side_fair - limit_price
        if edge < REALTIME_MAKER_ENTRY_MIN_EDGE:
            return f"REALTIME_MAKER_WAIT maker edge {edge:.4f} 低于 {REALTIME_MAKER_ENTRY_MIN_EDGE:.3f}"
        if best_ask is not None and limit_price >= best_ask - POST_ONLY_CROSS_BUFFER:
            return f"REALTIME_MAKER_WAIT POST_ONLY 限价 {limit_price:.4f} 接近卖一 {best_ask:.4f}"
        return None

    def _place_realtime_maker_order(
        self,
        market: MarketRound,
        state: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        now: float,
    ) -> None:
        side = str(state.get("side") or "")
        side_fair = _maybe_float(state.get("side_fair"))
        limit_price = _maybe_float(state.get("limit_price"))
        if side not in {"Up", "Down"} or side_fair is None or limit_price is None:
            return
        account = self.store.account()
        stake = min(self.settings.stake_dollars, float(account["cash_balance"]))
        if stake < 0.1:
            return
        quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
        expires_at = min(market.ends_at - REALTIME_MAKER_REDUCE_SECONDS_LEFT, now + REALTIME_MAKER_ORDER_TTL_SECONDS)
        if expires_at <= now + 1.0:
            self._set_realtime_maker_signal(state, "REALTIME_MAKER_WAIT maker 挂单有效期过短")
            return
        edge = side_fair - limit_price
        reason = (
            f"{REALTIME_MAKER_MARKER} POST_ONLY: side {side}, fair {side_fair:.4f}, "
            f"limit {limit_price:.4f}, edge {edge:.4f}, fair_up {state.get('fair_up')}, "
            f"actor_side {state.get('actor_side_fair')}, ttl {expires_at - now:.1f}s"
        )
        signal = Signal(
            "BTC",
            side,
            round(side_fair, 4),
            round(limit_price, 4),
            _maybe_float(state.get("distance_bps")) or 0.0,
            reason,
        )
        intent = TradeIntent(market, signal, stake)
        result = simulate_resting_buy(
            intent,
            quote,
            order_type=ORDER_TYPE_POST_ONLY,
            limit_price=limit_price,
            expires_at=expires_at,
            post_only=True,
        )
        self.store.place_execution_result(intent, result)
        if result.status not in {STATUS_RESTING, STATUS_PARTIAL_RESTING}:
            self._set_realtime_maker_signal(state, result.reason)
            return
        self._set_realtime_maker_signal(state, reason)

    def _set_realtime_maker_signal(self, state: dict[str, Any], reason: str) -> None:
        with self._lock:
            self.last_signal = {
                "symbol": "BTC",
                "side": state.get("side") or "REALTIME_MAKER_WAIT",
                "confidence": _maybe_float(state.get("side_fair")) or 0.0,
                "entry_price": _maybe_float(state.get("limit_price")) or 0.0,
                "move_bps": _maybe_float(state.get("distance_bps")) or 0.0,
                "reason": reason,
            }

    def _resting_order_fill(self, order: dict[str, Any], quote: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
        limit_price = _maybe_float(order.get("limit_price"))
        remaining_cash = _maybe_float(order.get("remaining_cash")) or 0.0
        if limit_price is None or limit_price <= 0 or remaining_cash <= 0:
            return None
        now = time.time() if now is None else now
        order_type = normalize_order_type(str(order.get("order_type") or ""))
        post_only = order_type == ORDER_TYPE_POST_ONLY or int(order.get("post_only") or 0) == 1
        created_at = _maybe_float(order.get("created_at")) or now
        age_seconds = max(0.0, now - created_at)
        eligible_limit = limit_price + PAIR_EPSILON
        fill_budget = remaining_cash
        reason_prefix = "RESTING_FILL"
        queue_ratio: float | None = None
        if post_only:
            if age_seconds < POST_ONLY_MIN_REST_SECONDS:
                return None
            eligible_limit = limit_price - POST_ONLY_CROSS_BUFFER
            queue_ratio = _post_only_queue_fill_ratio(age_seconds)
            fill_budget = remaining_cash * queue_ratio
            reason_prefix = "POST_ONLY_QUEUE_FILL"
        levels = [level for level in ask_levels_from_quote(quote) if level.price <= eligible_limit + PAIR_EPSILON]
        if not levels:
            return None
        available_shares = round(sum(level.size for level in levels), 6)
        if post_only and queue_ratio is not None:
            available_shares = round(available_shares * queue_ratio, 6)
        shares = round(min(available_shares, fill_budget / limit_price), 6)
        if shares <= PAIR_EPSILON:
            return None
        notional = round(shares * limit_price, 6)
        queue_text = f", age {age_seconds:.1f}s, queue_ratio {queue_ratio:.2f}" if queue_ratio is not None else ""
        return {
            "fill_price": limit_price,
            "shares": shares,
            "notional": notional,
            "fee": 0.0,
            "cash_spent": notional,
            "level_price": levels[0].price,
            "reason": (
                f"{reason_prefix} maker fill {shares:.6f} @ {limit_price:.4f}, "
                f"trigger_ask {levels[0].price:.4f}{queue_text}, fee 0.000000"
            ),
        }

    def _run_pair_strategy_from_state(self, market: MarketRound, price: dict[str, Any], quotes: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        quotes = self._quotes_with_depth(market, quotes)
        state = _pair_quote_state(quotes, now)
        multi_note = _pair_multi_note(self.market_data_mode, price)
        if multi_note:
            state["multi_note"] = multi_note
        managed = self._manage_pair_positions(market, price, state, now)
        if managed:
            reason = str(self.last_pair_event.get("message")) if self.last_pair_event else "配对策略持仓管理中"
            self._set_last_pair_signal("PAIR_MANAGE", state, reason)
            return
        open_rows = [row for row in self.store.open_trades() if row["symbol"] == "BTC" and row["round_id"] == market.round_id]
        if open_rows:
            self._set_last_pair_signal("PAIR_MANAGE", state, "配对策略持仓管理中")
            return

        multi_block = _pair_multi_entry_block_reason(self.market_data_mode, price)
        if multi_block:
            self._set_last_pair_signal("PAIR_WAIT", state, multi_block)
            return
        reason = self._pair_entry_block_reason(market, state, now)
        if reason:
            self._set_last_pair_signal("PAIR_WAIT", state, reason)
            return
        self._place_pair_trade(market, state, quotes, now)

    def _pair_entry_block_reason(self, market: MarketRound, state: dict[str, Any], now: float) -> str | None:
        time_left = market.ends_at - now
        if market.target_price <= 0:
            return "配对策略缺少官方目标价，停止开新仓"
        if self.store.daily_realized_pnl() <= -abs(self.settings.initial_balance * PAIR_DAILY_LOSS_PCT / 100.0):
            return f"配对策略日内回撤达到 {PAIR_DAILY_LOSS_PCT:g}%（{PAIR_DAILY_LOSS_NOTE}），停止开新仓"
        if self.pair_stop_loss_streak >= PAIR_STOP_STREAK_LIMIT:
            return "配对策略连续残余止损达到上限，停止开新仓"
        if time_left <= PAIR_ENTRY_MIN_SECONDS_LEFT:
            return "配对策略临近结算停止开新仓"
        if time_left > self.settings.max_time_left_seconds:
            return "配对策略等待盘口稳定"
        if state.get("quote_age_ms") is None or float(state["quote_age_ms"]) > self.settings.max_quote_age_ms:
            return "配对策略盘口报价过期"
        pair_cost = _maybe_float(state.get("pair_cost"))
        if pair_cost is None:
            return "配对策略缺少 Up/Down 双边卖一价"
        up_ask = _maybe_float(state.get("up_ask"))
        down_ask = _maybe_float(state.get("down_ask"))
        if pair_cost > PAIR_ENTRY_COST_THRESHOLD:
            return f"配对合成成本 {pair_cost:.4f} 高于 {PAIR_ENTRY_COST_THRESHOLD:.2f}"
        if up_ask is not None and down_ask is not None:
            net_pair_cost = pair_cost + self.settings.paper_taker_fee_rate * up_ask * (1.0 - up_ask) + self.settings.paper_taker_fee_rate * down_ask * (1.0 - down_ask)
            if net_pair_cost >= 1.0:
                return f"配对含费成本 {net_pair_cost:.4f} 已无正期望毛边"
        if self.store.open_trade_count("BTC") > self.settings.max_open_trades - 2:
            return "配对策略可用持仓槽不足"
        if self.store.open_trade_exists_for_round(market.round_id):
            return "当前市场已有持仓，等待处理完成"
        if self.store.active_paper_order_exists_for_round(market.round_id):
            return "当前市场已有配对挂单，等待成交或取消"
        up_ask_size = _maybe_float(state.get("up_ask_size"))
        down_ask_size = _maybe_float(state.get("down_ask_size"))
        if up_ask_size is not None and up_ask_size < self.settings.min_ask_size:
            return "Up 卖盘深度不足"
        if down_ask_size is not None and down_ask_size < self.settings.min_ask_size:
            return "Down 卖盘深度不足"
        account = self.store.account()
        if float(account["cash_balance"]) < 0.1:
            return "纸交易可用资金不足"
        return None

    def _place_pair_trade(self, market: MarketRound, state: dict[str, Any], quotes: dict[str, dict[str, Any]], now: float) -> None:
        pair_cost = _maybe_float(state.get("pair_cost"))
        up_ask = _maybe_float(state.get("up_ask"))
        down_ask = _maybe_float(state.get("down_ask"))
        if pair_cost is None or up_ask is None or down_ask is None or pair_cost <= 0:
            return
        order_type = normalize_order_type(self.settings.paper_entry_order_type)
        if order_type in {ORDER_TYPE_POST_ONLY, ORDER_TYPE_GTC, ORDER_TYPE_GTD}:
            self._place_pair_resting_orders(market, state, quotes, now, order_type)
            return
        account = self.store.account()
        total_budget = min(self.settings.stake_dollars, float(account["cash_balance"]))
        up_quote = quotes.get("Up") if isinstance(quotes.get("Up"), dict) else {}
        down_quote = quotes.get("Down") if isinstance(quotes.get("Down"), dict) else {}
        if not up_quote or not down_quote:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对策略缺少双边盘口深度")
            return
        up_limit = self.settings.max_entry_price
        down_limit = self.settings.max_entry_price
        shares = self._max_pair_sweep_shares(up_quote, down_quote, up_limit, down_limit, total_budget)
        if shares < self.settings.min_ask_size:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对策略可成交份额不足")
            return
        up_sweep = sweep_taker_buy_by_shares(
            up_quote,
            limit_price=up_limit,
            shares=shares,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
        )
        down_sweep = sweep_taker_buy_by_shares(
            down_quote,
            limit_price=down_limit,
            shares=shares,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
        )
        if up_sweep.shares < shares - PAIR_EPSILON or down_sweep.shares < shares - PAIR_EPSILON:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对策略多档深度不足")
            return
        gross_pair_cost = round((up_sweep.notional + down_sweep.notional) / shares, 6)
        net_pair_cost = round((up_sweep.cash_spent + down_sweep.cash_spent) / shares, 6)
        if net_pair_cost >= 1.0:
            self._set_last_pair_signal("PAIR_WAIT", state, f"配对多档含费成本 {net_pair_cost:.4f} 已无正期望毛边")
            return
        edge = 1.0 - net_pair_cost
        reason = (
            f"PAIR_OPEN 双边配对: avg_up {up_sweep.avg_price:.4f}, avg_down {down_sweep.avg_price:.4f}, "
            f"top_cost {pair_cost:.4f}, gross_cost {gross_pair_cost:.4f}, net_cost {net_pair_cost:.4f}, "
            f"fee {up_sweep.fee + down_sweep.fee:.6f}, edge {edge:.4f}, shares {shares:.6f}, "
            f"levels_up {up_sweep.levels_used}, levels_down {down_sweep.levels_used}"
        )
        if state.get("multi_note"):
            reason = _append_reason_text(reason, str(state["multi_note"]))
        confidence = round(max(0.0, min(1.0, edge)), 4)
        up_signal = Signal("BTC", "Up", confidence, up_sweep.avg_price, 0.0, reason)
        down_signal = Signal("BTC", "Down", confidence, down_sweep.avg_price, 0.0, reason)
        fill_status = "FILLED" if up_sweep.cash_spent + down_sweep.cash_spent >= total_budget - PAIR_EPSILON else "PARTIAL"
        fills = [
            build_taker_buy_fill_from_sweep(
                TradeIntent(market, up_signal, up_sweep.cash_spent),
                side="Up",
                order_type=ORDER_TYPE_FAK,
                status=fill_status,
                limit_price=up_limit,
                sweep=up_sweep,
            ),
            build_taker_buy_fill_from_sweep(
                TradeIntent(market, down_signal, down_sweep.cash_spent),
                side="Down",
                order_type=ORDER_TYPE_FAK,
                status=fill_status,
                limit_price=down_limit,
                sweep=down_sweep,
            ),
        ]
        self.store.place_fills(fills)
        self.pair_stop_loss_streak = 0
        self._set_pair_event(
            "PAIR_OPEN",
            f"配对开仓 net_cost={net_pair_cost:.4f}, shares={shares:.6f}",
            now,
            {
                "pair_cost": gross_pair_cost,
                "net_pair_cost": net_pair_cost,
                "shares": shares,
                "stake": round(sum(fill.cash_spent for fill in fills), 6),
            },
        )
        self._set_last_pair_signal("PAIR_OPEN", state, "配对策略已开双边仓")

    def _place_pair_resting_orders(
        self,
        market: MarketRound,
        state: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        now: float,
        order_type: str,
    ) -> None:
        account = self.store.account()
        total_budget = min(self.settings.stake_dollars, float(account["cash_balance"]))
        up_quote = quotes.get("Up") if isinstance(quotes.get("Up"), dict) else {}
        down_quote = quotes.get("Down") if isinstance(quotes.get("Down"), dict) else {}
        if total_budget < 0.1:
            self._set_last_pair_signal("PAIR_WAIT", state, "纸交易可用资金不足")
            return
        if not up_quote or not down_quote:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对挂单缺少双边盘口")
            return

        up_limit = self._pair_resting_limit_price(up_quote)
        down_limit = self._pair_resting_limit_price(down_quote)
        pair_limit_cost = round(up_limit + down_limit, 6)
        if pair_limit_cost <= 0:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对挂单限价无效")
            return
        if pair_limit_cost > PAIR_ENTRY_COST_THRESHOLD:
            self._set_last_pair_signal(
                "PAIR_WAIT",
                state,
                f"配对挂单成本 {pair_limit_cost:.4f} 高于 {PAIR_ENTRY_COST_THRESHOLD:.2f}",
            )
            return
        if pair_limit_cost >= 1.0:
            self._set_last_pair_signal("PAIR_WAIT", state, f"配对挂单成本 {pair_limit_cost:.4f} 已无正期望毛边")
            return

        shares = round(total_budget / pair_limit_cost, 6)
        if shares < self.settings.min_ask_size:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对挂单份额不足")
            return
        up_cash = round(shares * up_limit, 6)
        down_cash = round(shares * down_limit, 6)
        if up_cash <= 0 or down_cash <= 0 or up_cash + down_cash > float(account["cash_balance"]) + PAIR_EPSILON:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对挂单预算不足")
            return

        edge = round(1.0 - pair_limit_cost, 6)
        confidence = round(max(0.0, min(1.0, edge)), 4)
        reason = (
            f"PAIR_OPEN_RESTING {order_type}: up_limit {up_limit:.4f}, down_limit {down_limit:.4f}, "
            f"limit_cost {pair_limit_cost:.4f}, top_cost {state.get('pair_cost') or 0.0:.4f}, "
            f"edge {edge:.4f}, shares {shares:.6f}"
        )
        if state.get("multi_note"):
            reason = _append_reason_text(reason, str(state["multi_note"]))
        up_signal = Signal("BTC", "Up", confidence, up_limit, 0.0, reason)
        down_signal = Signal("BTC", "Down", confidence, down_limit, 0.0, reason)
        up_intent = TradeIntent(market, up_signal, up_cash)
        down_intent = TradeIntent(market, down_signal, down_cash)
        expires_at = self._resting_order_expires_at(market, order_type, now=now)
        post_only = order_type == ORDER_TYPE_POST_ONLY
        up_result = simulate_resting_buy(
            up_intent,
            up_quote,
            order_type=order_type,
            limit_price=up_limit,
            expires_at=expires_at,
            post_only=post_only,
        )
        down_result = simulate_resting_buy(
            down_intent,
            down_quote,
            order_type=order_type,
            limit_price=down_limit,
            expires_at=expires_at,
            post_only=post_only,
        )
        self.store.place_execution_result(up_intent, up_result)
        self.store.place_execution_result(down_intent, down_result)
        active_statuses = {STATUS_RESTING, STATUS_PARTIAL_RESTING}
        if up_result.status not in active_statuses or down_result.status not in active_statuses:
            self._set_last_pair_signal(
                "PAIR_WAIT",
                state,
                f"配对挂单未全部进入挂单: Up {up_result.status}, Down {down_result.status}",
            )
            return

        self.pair_stop_loss_streak = 0
        self._set_pair_event(
            "PAIR_RESTING",
            reason,
            now,
            {
                "order_type": order_type,
                "up_limit": up_limit,
                "down_limit": down_limit,
                "pair_limit_cost": pair_limit_cost,
                "shares": shares,
            },
        )
        self._set_last_pair_signal("PAIR_RESTING", state, reason)

    def _pair_resting_limit_price(self, quote: dict[str, Any]) -> float:
        best_bid = _maybe_float(quote.get("best_bid"))
        best_ask = _maybe_float(quote.get("best_ask"))
        if best_bid is not None and best_bid > 0:
            candidate = best_bid
        elif best_ask is not None:
            candidate = best_ask - 0.01
        else:
            candidate = self.settings.max_entry_price
        if best_ask is not None and candidate >= best_ask:
            candidate = best_ask - 0.01
        return round(max(0.01, min(self.settings.max_entry_price, candidate)), 4)

    def _max_pair_sweep_shares(
        self,
        up_quote: dict[str, Any],
        down_quote: dict[str, Any],
        up_limit: float,
        down_limit: float,
        total_budget: float,
    ) -> float:
        if total_budget <= 0:
            return 0.0
        high = max(self.settings.min_ask_size, total_budget)
        for _ in range(32):
            up_sweep = sweep_taker_buy_by_shares(
                up_quote,
                limit_price=up_limit,
                shares=high,
                taker_fee_rate=self.settings.paper_taker_fee_rate,
            )
            down_sweep = sweep_taker_buy_by_shares(
                down_quote,
                limit_price=down_limit,
                shares=high,
                taker_fee_rate=self.settings.paper_taker_fee_rate,
            )
            if up_sweep.shares < high - PAIR_EPSILON or down_sweep.shares < high - PAIR_EPSILON:
                break
            total_cash = up_sweep.cash_spent + down_sweep.cash_spent
            net_pair_cost = total_cash / high if high > 0 else 1.0
            if total_cash > total_budget + PAIR_EPSILON or net_pair_cost >= 1.0:
                break
            high *= 2.0

        low = 0.0
        for _ in range(40):
            mid = (low + high) / 2.0
            if mid <= PAIR_EPSILON:
                break
            up_sweep = sweep_taker_buy_by_shares(
                up_quote,
                limit_price=up_limit,
                shares=mid,
                taker_fee_rate=self.settings.paper_taker_fee_rate,
            )
            down_sweep = sweep_taker_buy_by_shares(
                down_quote,
                limit_price=down_limit,
                shares=mid,
                taker_fee_rate=self.settings.paper_taker_fee_rate,
            )
            if up_sweep.shares < mid - PAIR_EPSILON or down_sweep.shares < mid - PAIR_EPSILON:
                high = mid
                continue
            total_cash = up_sweep.cash_spent + down_sweep.cash_spent
            net_pair_cost = total_cash / mid if mid > 0 else 1.0
            if total_cash <= total_budget + PAIR_EPSILON and net_pair_cost < 1.0:
                low = mid
            else:
                high = mid
        return round(low, 6)

    def _manage_pair_positions(
        self,
        market: MarketRound,
        price: dict[str, Any],
        state: dict[str, Any],
        now: float,
    ) -> bool:
        rows = [row for row in self.store.open_trades() if row["symbol"] == "BTC" and row["round_id"] == market.round_id]
        if not rows:
            return False
        time_left = market.ends_at - now
        up_bid = _maybe_float(state.get("up_bid"))
        down_bid = _maybe_float(state.get("down_bid"))
        bid_sum = _maybe_float(state.get("bid_sum"))
        if time_left <= PAIR_FORCE_FLATTEN_SECONDS_LEFT:
            closed = self._close_all_current_trades(market.round_id, up_bid, down_bid, now, "PAIR_FORCE_FLATTEN 尾盘强制平仓")
            if closed:
                self.pair_stop_loss_streak += 1
                self._set_pair_event("PAIR_FORCE_FLATTEN", f"尾盘强制平仓 {len(closed)} 条持仓", now)
                return True
        summary = _position_summary(rows)
        paired_shares = min(summary["Up"]["shares"], summary["Down"]["shares"])
        if paired_shares > PAIR_EPSILON and bid_sum is not None and bid_sum >= PAIR_EXIT_BID_THRESHOLD:
            closed = []
            if up_bid is not None:
                closed.extend(self._close_side_shares(rows, "Up", paired_shares, up_bid, now, f"PAIR_EXIT bid_sum {bid_sum:.4f}"))
            if down_bid is not None:
                closed.extend(self._close_side_shares(rows, "Down", paired_shares, down_bid, now, f"PAIR_EXIT bid_sum {bid_sum:.4f}"))
            if closed:
                self.pair_stop_loss_streak = 0
                self._set_pair_event("PAIR_EXIT", f"配对提前平仓 bid_sum={bid_sum:.4f}", now)
                return True
        rows = [row for row in self.store.open_trades() if row["symbol"] == "BTC" and row["round_id"] == market.round_id]
        residual = _residual_inventory(rows, state)
        if not residual:
            return False
        exit_price = _maybe_float(residual.get("bid"))
        if exit_price is None:
            return False
        side = str(residual["side"])
        shares = float(residual["shares"])
        roi_pct = _maybe_float(residual.get("roi_pct"))
        confirmed = _price_confirms_residual(side, price, market.target_price, self.market_data_mode)
        if time_left <= PAIR_RESIDUAL_REDUCE_SECONDS_LEFT:
            closed = self._close_side_shares(rows, side, shares, exit_price, now, "PAIR_TIME_STOP 残余库存时间止损")
            if closed:
                self.pair_stop_loss_streak += 1
                self._set_pair_event("PAIR_TIME_STOP", f"残余 {side} 时间止损 shares={shares:.6f}", now)
                return True
        if roi_pct is not None and roi_pct <= PAIR_RESIDUAL_STOP_LOSS_PCT:
            closed = self._close_side_shares(rows, side, shares, exit_price, now, f"PAIR_STOP_LOSS 残余库存 {roi_pct:.2f}%")
            if closed:
                self.pair_stop_loss_streak += 1
                self._set_pair_event("PAIR_STOP_LOSS", f"残余 {side} 亏损 {roi_pct:.2f}% 平仓", now)
                return True
        if not confirmed:
            closed = self._close_side_shares(rows, side, shares, exit_price, now, "PAIR_PRICE_REJECT 残余方向未获 Chainlink 确认")
            if closed:
                self._set_pair_event("PAIR_PRICE_REJECT", f"残余 {side} 未获价格确认，已平仓", now)
                return True
        return False

    def _close_all_current_trades(
        self,
        round_id: str,
        up_bid: float | None,
        down_bid: float | None,
        now: float,
        reason: str,
    ) -> list[dict[str, Any]]:
        exit_prices: dict[str, float] = {}
        if up_bid is not None:
            exit_prices["Up"] = up_bid
        if down_bid is not None:
            exit_prices["Down"] = down_bid
        if not exit_prices:
            return []
        rows = [row for row in self.store.open_trades() if row["round_id"] == round_id]
        closed: list[dict[str, Any]] = []
        if up_bid is not None:
            up_shares = sum(_maybe_float(row.get("shares")) or 0.0 for row in rows if row.get("side") == "Up")
            closed.extend(self._close_side_shares(rows, "Up", up_shares, up_bid, now, reason))
        if down_bid is not None:
            down_shares = sum(_maybe_float(row.get("shares")) or 0.0 for row in rows if row.get("side") == "Down")
            closed.extend(self._close_side_shares(rows, "Down", down_shares, down_bid, now, reason))
        return closed

    def _close_side_shares(
        self,
        rows: list[dict[str, Any]],
        side: str,
        shares: float,
        exit_price: float,
        now: float,
        reason: str,
    ) -> list[dict[str, Any]]:
        remaining = shares
        closed: list[dict[str, Any]] = []
        for row in sorted([row for row in rows if row.get("side") == side], key=lambda item: (float(item.get("opened_at") or 0.0), int(item.get("id") or 0))):
            if remaining <= PAIR_EPSILON:
                break
            row_shares = _maybe_float(row.get("shares")) or 0.0
            close_shares = min(row_shares, remaining)
            fee = taker_fee(close_shares, exit_price, self.settings.paper_taker_fee_rate)
            item = self.store.close_trade_shares(int(row["id"]), close_shares, exit_price, now, reason, fee=fee)
            if item:
                closed.append(item)
            remaining = round(remaining - close_shares, 6)
        return closed

    def _execute_entry_order(self, intent: TradeIntent, quote: dict[str, Any]):
        order_type = normalize_order_type(self.settings.paper_entry_order_type)
        if order_type in {ORDER_TYPE_POST_ONLY, ORDER_TYPE_GTC, ORDER_TYPE_GTD}:
            limit_price = self._resting_entry_limit_price(intent.signal, quote)
            return simulate_resting_buy(
                intent,
                quote,
                order_type=order_type,
                limit_price=limit_price,
                expires_at=self._resting_order_expires_at(intent.market, order_type),
                post_only=order_type == ORDER_TYPE_POST_ONLY,
            )
        limit_price = self._entry_limit_price(intent.signal)
        return simulate_fak_buy(
            intent,
            quote,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
            min_shares=self.settings.min_ask_size,
            limit_price=limit_price,
        )

    def _quote_with_bid(self, market: MarketRound, side: str, quote: dict[str, Any]) -> dict[str, Any]:
        if _maybe_float(quote.get("best_bid")) is not None:
            return quote
        token_id = market.up_token if side == "Up" else market.down_token if side == "Down" else ""
        if not token_id:
            return quote
        try:
            fresh = self.polymarket.get_quote(token_id, side).to_dict()
        except Exception:  # noqa: BLE001 - missing stop-and-flip exit bid should not stop the bot loop.
            return quote
        if fresh:
            with self._lock:
                latest = dict(self.latest_quotes)
                latest[side] = fresh
                self.latest_quotes = latest
                paper = _copy_quotes(self.paper_quotes)
                paper[side] = fresh
                self.paper_quotes = paper
        return fresh or quote

    def _quote_with_depth(self, market: MarketRound, side: str, quote: dict[str, Any]) -> dict[str, Any]:
        if isinstance(quote.get("asks"), list) and quote.get("asks"):
            return quote
        token_id = market.up_token if side == "Up" else market.down_token if side == "Down" else ""
        if not token_id:
            return quote
        try:
            fresh = self.polymarket.get_quote(token_id, side).to_dict()
        except Exception:  # noqa: BLE001 - REST depth is an execution-quality fallback, not a reason to crash.
            return quote
        with self._lock:
            latest = dict(self.latest_quotes)
            latest[side] = fresh
            self.latest_quotes = latest
            paper = _copy_quotes(self.paper_quotes)
            paper[side] = fresh
            self.paper_quotes = paper
        return fresh

    def _quotes_with_depth(self, market: MarketRound, quotes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if all(isinstance(quotes.get(side), dict) and quotes[side].get("asks") for side in ("Up", "Down")):
            return quotes
        try:
            fresh_quotes = {side: quote.to_dict() for side, quote in self.polymarket.get_quotes(market).items()}
        except Exception:  # noqa: BLE001 - keep paper loop alive if CLOB REST briefly fails.
            return quotes
        merged = dict(quotes)
        for side, fresh in fresh_quotes.items():
            row = merged.get(side) if isinstance(merged.get(side), dict) else {}
            if not row.get("asks") and fresh.get("asks"):
                merged[side] = fresh
        with self._lock:
            latest = dict(self.latest_quotes)
            paper = _copy_quotes(self.paper_quotes)
            for side, fresh in fresh_quotes.items():
                if fresh.get("asks"):
                    latest[side] = fresh
                    paper[side] = fresh
            self.latest_quotes = latest
            self.paper_quotes = paper
        return merged

    def _entry_limit_price(self, signal: Signal) -> float:
        edge_preserving_limit = signal.confidence - self.settings.min_edge
        limit_price = max(signal.entry_price, edge_preserving_limit)
        return round(min(self.settings.max_entry_price, limit_price), 4)

    def _resting_entry_limit_price(self, signal: Signal, quote: dict[str, Any]) -> float:
        edge_preserving_limit = max(0.01, signal.confidence - self.settings.min_edge)
        best_bid = _maybe_float(quote.get("best_bid"))
        best_ask = _maybe_float(quote.get("best_ask"))
        if best_bid is not None and best_bid > 0:
            candidate = best_bid
        elif best_ask is not None:
            candidate = best_ask - 0.01
        else:
            candidate = signal.entry_price - 0.01
        limit_price = min(candidate, edge_preserving_limit, self.settings.max_entry_price)
        if best_ask is not None and limit_price >= best_ask:
            limit_price = best_ask - 0.01
        return round(max(0.01, min(0.99, limit_price)), 4)

    def _resting_order_expires_at(self, market: MarketRound, order_type: str, *, now: float | None = None) -> float:
        if order_type == ORDER_TYPE_GTD:
            now_value = time.time() if now is None else now
            return min(market.ends_at, now_value + self.settings.paper_gtd_seconds)
        return market.ends_at

    def _append_last_signal_reason(self, reason: str) -> None:
        with self._lock:
            signal = dict(self.last_signal or {})
            existing = str(signal.get("reason") or "")
            signal["reason"] = f"{existing} | {reason}" if existing else reason
            self.last_signal = signal

    def _set_paper_paused_signal(self) -> None:
        with self._lock:
            self.last_signal = {
                "symbol": "BTC",
                "side": "PAPER_PAUSED",
                "confidence": 0.0,
                "entry_price": 0.0,
                "move_bps": 0.0,
                "reason": PAPER_PAUSE_REASON,
            }

    def _set_last_pair_signal(self, side: str, state: dict[str, Any], reason: str) -> None:
        with self._lock:
            self.last_signal = {
                "symbol": "BTC",
                "side": side,
                "confidence": 0.0,
                "entry_price": state.get("pair_cost") or 0.0,
                "move_bps": 0.0,
                "reason": reason,
            }

    def _set_pair_event(self, event_type: str, message: str, at: float, extra: dict[str, Any] | None = None) -> None:
        event = {"type": event_type, "message": message, "at": at}
        if extra:
            event.update(extra)
        with self._lock:
            self.last_pair_event = event

    def _set_error(self, message: str, now: float) -> None:
        with self._lock:
            self.last_error = message
            self.last_tick_at = now

    def refresh_status_runtime_overlay(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """用当前内存行情覆盖过期 status cache，避免界面展示上一拍价格源诊断。"""

        payload = dict(snapshot)
        runtime = dict(payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {})
        live_snapshot = dict(
            runtime.get("live_trading") if isinstance(runtime.get("live_trading"), dict) else {}
        )
        with self._lock:
            current_market = self.current_market
            execution_price = dict(self.execution_price)
            execution_quotes = _copy_quotes(self.execution_quotes)
            btc_runtime = self._btc_runtime_payload_locked()
            runtime.update(
                {
                    "last_error": self.last_error,
                    "last_tick_at": self.last_tick_at,
                    "last_signal": dict(self.last_signal or {}),
                    "btc_runtime": btc_runtime,
                    "current_market": market_to_payload(current_market),
                    "latest_price": dict(self.latest_price),
                    "latest_quotes": dict(self.latest_quotes),
                    "paper_price": dict(self.paper_price),
                    "paper_quotes": _copy_quotes(self.paper_quotes),
                    "execution_price": execution_price,
                    "execution_quotes": execution_quotes,
                    "ws_status": dict(self.ws_status),
                }
            )
        if self.live_trading is not None:
            live_snapshot["last_signal"] = dict(self.live_trading.last_signal or {})
            live_snapshot["last_error"] = self.live_trading.last_error
            live_snapshot["gate_status"] = self.live_trading.gate_status(
                current_market,
                execution_price,
                execution_quotes,
                readiness=live_snapshot.get("readiness") if isinstance(live_snapshot.get("readiness"), dict) else None,
                official_open_orders=(
                    live_snapshot.get("open_orders") if isinstance(live_snapshot.get("open_orders"), dict) else None
                ),
            )
            runtime["live_trading"] = live_snapshot
        payload["runtime"] = runtime
        return payload

    def snapshot(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            live_runner = self.live_trading
            live_paper_runner = self.live_paper_trading
            live_paper_stop_win_runner = self.live_paper_stop_win_trading
            current_market = self.current_market
            execution_price = dict(self.execution_price)
            execution_quotes = _copy_quotes(self.execution_quotes)
            btc_runtime = self._btc_runtime_payload_locked()
            paper_pause_event = dict(self.last_paper_pause_event or {})
            paper_paused = bool(self.paper_trading_paused)
            runtime = {
                "paper_only": True,
                "running": bool(self._thread and self._thread.is_alive()),
                "paper_trading": {
                    "paused": paper_paused,
                    "message": paper_pause_event.get("message")
                    or ("Paper 下单已暂停" if paper_paused else "Paper 下单运行中"),
                    "updated_at": paper_pause_event.get("at"),
                    "event": paper_pause_event,
                },
                "btc_runtime": btc_runtime,
                "last_error": self.last_error,
                "last_tick_at": self.last_tick_at,
                "last_signal": dict(self.last_signal or {}),
                "current_market": market_to_payload(current_market),
                "latest_price": dict(self.latest_price),
                "latest_quotes": dict(self.latest_quotes),
                "paper_price": dict(self.paper_price),
                "paper_quotes": _copy_quotes(self.paper_quotes),
                "execution_price": execution_price,
                "execution_quotes": execution_quotes,
                "market_data_scope": {
                    "display": "browser_or_backend",
                    "paper": "backend_only",
                    "execution": "backend_only",
                },
                "ws_status": dict(self.ws_status),
                "pair_strategy": self._pair_strategy_runtime_locked(),
                "strategy_experiments": {"enabled": False, "variants": []},
                "live_trading": {},
                "live_paper_trading": {},
                "live_paper_stop_win_trading": {},
            }
        strategy_experiments_snapshot = self.strategy_experiments_snapshot()
        live_snapshot = live_runner.snapshot(refresh_external=False) if live_runner is not None else _disabled_live_snapshot()
        live_paper_snapshot = live_paper_runner.snapshot() if live_paper_runner is not None else _disabled_live_paper_snapshot()
        live_paper_stop_win_snapshot = (
            live_paper_stop_win_runner.snapshot()
            if live_paper_stop_win_runner is not None
            else _disabled_live_paper_snapshot(
                variant_id=LIVE_PAPER_STOP_WIN_VARIANT_ID,
                combo=LIVE_PAPER_STOP_WIN_COMBO,
            )
        )
        if live_runner is not None:
            live_snapshot["gate_status"] = live_runner.gate_status(
                current_market,
                execution_price,
                execution_quotes,
                readiness=live_snapshot.get("readiness") if isinstance(live_snapshot.get("readiness"), dict) else None,
                official_open_orders=(
                    live_snapshot.get("open_orders") if isinstance(live_snapshot.get("open_orders"), dict) else None
                ),
            )
        runtime["strategy_experiments"] = strategy_experiments_snapshot
        runtime["live_trading"] = live_snapshot
        runtime["live_paper_trading"] = live_paper_snapshot
        runtime["live_paper_stop_win_trading"] = live_paper_stop_win_snapshot
        if live_runner is not None:
            live_snapshot["open_trades"] = self._decorate_open_trades(live_runner.open_trades(), runtime)
            live_metrics = self._metrics_with_open_marks(
                live_runner.store.metrics(),
                live_snapshot["open_trades"],
            )
            live_variant = live_snapshot.get("variant") if isinstance(live_snapshot.get("variant"), dict) else None
            if live_variant is not None:
                live_variant["metrics"] = live_metrics
            live_variants = live_snapshot.get("variants") if isinstance(live_snapshot.get("variants"), list) else []
            for variant in live_variants:
                if isinstance(variant, dict) and variant.get("variant_id") == live_runner.variant_id:
                    variant["metrics"] = live_metrics
        if live_paper_runner is not None:
            live_paper_snapshot["open_trades"] = self._decorate_open_trades(
                live_paper_runner.open_trades(),
                {
                    "current_market": market_to_payload(current_market),
                    "latest_price": execution_price,
                    "latest_quotes": execution_quotes,
                },
            )
            live_paper_metrics = self._metrics_with_open_marks(
                live_paper_runner.store.metrics(),
                live_paper_snapshot["open_trades"],
            )
            live_paper_variant = (
                live_paper_snapshot.get("variant") if isinstance(live_paper_snapshot.get("variant"), dict) else None
            )
            if live_paper_variant is not None:
                live_paper_variant["metrics"] = live_paper_metrics
            live_paper_variants = (
                live_paper_snapshot.get("variants") if isinstance(live_paper_snapshot.get("variants"), list) else []
            )
            for variant in live_paper_variants:
                if isinstance(variant, dict) and variant.get("variant_id") == LIVE_PAPER_VARIANT_ID:
                    variant["metrics"] = live_paper_metrics
        if live_paper_stop_win_runner is not None:
            live_paper_stop_win_snapshot["open_trades"] = self._decorate_open_trades(
                live_paper_stop_win_runner.open_trades(),
                {
                    "current_market": market_to_payload(current_market),
                    "latest_price": execution_price,
                    "latest_quotes": execution_quotes,
                },
            )
            live_paper_stop_win_metrics = self._metrics_with_open_marks(
                live_paper_stop_win_runner.store.metrics(),
                live_paper_stop_win_snapshot["open_trades"],
            )
            live_paper_stop_win_variant = (
                live_paper_stop_win_snapshot.get("variant")
                if isinstance(live_paper_stop_win_snapshot.get("variant"), dict)
                else None
            )
            if live_paper_stop_win_variant is not None:
                live_paper_stop_win_variant["metrics"] = live_paper_stop_win_metrics
            live_paper_stop_win_variants = (
                live_paper_stop_win_snapshot.get("variants")
                if isinstance(live_paper_stop_win_snapshot.get("variants"), list)
                else []
            )
            for variant in live_paper_stop_win_variants:
                if isinstance(variant, dict) and variant.get("variant_id") == LIVE_PAPER_STOP_WIN_VARIANT_ID:
                    variant["metrics"] = live_paper_stop_win_metrics
        open_trades = self._decorate_open_trades(
            [row for row in self.store.open_trades() if row["symbol"] == "BTC"],
            runtime,
        )
        recent_page = self.recent_trades_page(RECENT_TRADES_DEFAULT_LIMIT, 0)
        recent_orders_page = self.orders_page(ORDERS_DEFAULT_LIMIT, 0)
        metrics = self._metrics_with_open_marks(self.store.metrics(), open_trades)
        payload = {
            "runtime": runtime,
            "settings": {
                "initial_balance": self.settings.initial_balance,
                "stake_dollars": self.settings.stake_dollars,
                "round_seconds": self.settings.round_seconds,
                "tick_seconds": self.settings.tick_seconds,
                "max_open_trades": self.settings.max_open_trades,
                "max_daily_loss": self.settings.max_daily_loss,
                "min_confidence": self.settings.min_confidence,
                "min_edge": self.settings.min_edge,
                "max_entry_price": self.settings.max_entry_price,
                "paper_entry_order_type": self.settings.paper_entry_order_type,
                "paper_taker_fee_rate": self.settings.paper_taker_fee_rate,
                "paper_gtd_seconds": self.settings.paper_gtd_seconds,
                "strategy_experiments": {
                    "enabled": self.settings.strategy_experiments_enabled,
                    "db_dir": str(self.settings.strategy_experiments_db_dir),
                    "variants": self.settings.strategy_experiments_variants,
                },
                "live_trading": _live_settings_from_snapshot(live_snapshot)
                if live_runner is not None
                else _disabled_live_settings(),
                "live_paper_trading": {
                    "enabled": bool(live_paper_snapshot.get("enabled")),
                    "variant_id": live_paper_snapshot.get("variant_id") or LIVE_PAPER_VARIANT_ID,
                    "combo": live_paper_snapshot.get("combo") or LIVE_PAPER_COMBO,
                    "db_path": live_paper_snapshot.get("db_path"),
                    "mirrors_variant_id": LIVE_VARIANT_ID,
                    "settings": live_paper_snapshot.get("settings"),
                },
                "live_paper_stop_win_trading": {
                    "enabled": bool(live_paper_stop_win_snapshot.get("enabled")),
                    "variant_id": live_paper_stop_win_snapshot.get("variant_id") or LIVE_PAPER_STOP_WIN_VARIANT_ID,
                    "combo": live_paper_stop_win_snapshot.get("combo") or LIVE_PAPER_STOP_WIN_COMBO,
                    "db_path": live_paper_stop_win_snapshot.get("db_path"),
                    "mirrors_variant_id": LIVE_VARIANT_ID,
                    "settings": live_paper_stop_win_snapshot.get("settings"),
                },
                "llm_super_agent": {
                    "enabled": self.settings.llm_super_agent_enabled,
                    "api_key_present": bool(self.settings.llm_super_agent_api_key),
                    "base_url": self.settings.llm_super_agent_base_url,
                    "model": self.settings.llm_super_agent_model,
                    "timeout_seconds": self.settings.llm_super_agent_timeout_seconds,
                    "min_interval_seconds": self.settings.llm_super_agent_min_interval_seconds,
                },
                "pair_strategy": {
                    "entry_cost_threshold": PAIR_ENTRY_COST_THRESHOLD,
                    "exit_bid_threshold": PAIR_EXIT_BID_THRESHOLD,
                    "entry_min_seconds_left": PAIR_ENTRY_MIN_SECONDS_LEFT,
                    "residual_reduce_seconds_left": PAIR_RESIDUAL_REDUCE_SECONDS_LEFT,
                    "force_flatten_seconds_left": PAIR_FORCE_FLATTEN_SECONDS_LEFT,
                    "residual_stop_loss_pct": PAIR_RESIDUAL_STOP_LOSS_PCT,
                    "daily_loss_pct": PAIR_DAILY_LOSS_PCT,
                    "daily_loss_note": PAIR_DAILY_LOSS_NOTE,
                    "stop_streak_limit": PAIR_STOP_STREAK_LIMIT,
                },
                "paper_only": not (self.live_trading and self.live_trading.config.enabled),
                "market_source": "Polymarket Gamma + CLOB market WebSocket + RTDS WebSocket",
            },
            "metrics": metrics,
            "open_trades": open_trades,
            "recent_trades": recent_page["recent_trades"],
            "recent_trades_meta": recent_page["recent_trades_meta"],
            "recent_trades_summary": recent_page["recent_trades_summary"],
            "recent_orders": recent_orders_page["recent_orders"],
            "recent_orders_meta": recent_orders_page["recent_orders_meta"],
            "equity_curve": self.store.equity_curve(120),
        }
        if extra:
            payload.update(extra)
        return payload

    def actor_analysis(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            market = self.current_market
            price = dict(self.latest_price)
            quotes = _copy_quotes(self.latest_quotes)
            cache_key = f"{market.round_id}:{market.condition_id}" if market is not None else "NO_MARKET"
            if (
                not force
                and self._actor_analysis_cache is not None
                and self._actor_analysis_cache_key == cache_key
                and now < self._actor_analysis_cache_until
            ):
                cached = dict(self._actor_analysis_cache)
                cached["cached"] = True
                return {"actor_analysis": cached}

        analysis = build_actor_analysis(market, price, quotes, self.actor_data, now=now)
        with self._lock:
            self._actor_analysis_cache = dict(analysis)
            self._actor_analysis_cache_key = cache_key
            self._actor_analysis_cache_until = now + ACTOR_ANALYSIS_CACHE_SECONDS
        return {"actor_analysis": analysis}

    def llm_decision_review(self, limit: int = 80) -> dict[str, Any]:
        if self.strategy_experiments is not None:
            return self.strategy_experiments.llm_decision_review(limit)
        if self.llm_super_agent_enabled:
            review = self.store.llm_decision_review(
                limit=limit,
                opportunity_stake=self.settings.stake_dollars,
                variant_id=self.llm_super_agent_variant_id,
            )
            return {"llm_review": {"status": review["status"], "primary": review, "variants": [review]}}
        return {
            "llm_review": {
                "status": "DISABLED",
                "generated_at": time.time(),
                "primary": None,
                "variants": [],
                "message": "LLM super agent strategy experiment is not enabled.",
            }
        }

    def strategy_experiments_snapshot(self) -> dict[str, Any]:
        if self.strategy_experiments is None:
            return {"enabled": False, "variants": []}
        return self.strategy_experiments.snapshot()

    def _live_paper_runners(self) -> list[LivePaperStrategyRunner]:
        return [
            runner
            for runner in (self.live_paper_trading, self.live_paper_stop_win_trading)
            if runner is not None
        ]

    def _live_paper_runner_for_variant(self, variant_id: str | None = None) -> LivePaperStrategyRunner:
        runners = self._live_paper_runners()
        if not runners:
            raise ValueError("live paper trading is disabled in this runtime")
        normalized = str(variant_id or LIVE_PAPER_VARIANT_ID).strip().upper()
        for runner in runners:
            if runner.variant_id.upper() == normalized:
                return runner
        allowed = ", ".join(runner.variant_id for runner in runners)
        raise ValueError(f"unknown live paper variant_id: {normalized or '-'}; allowed: {allowed}")

    def strategy_experiment_detail(
        self,
        variant_id: str,
        trade_limit: int = 50,
        order_limit: int = 50,
    ) -> dict[str, Any]:
        if self.strategy_experiments is None:
            return {"enabled": False, "variant": None, "recent_trades": [], "recent_orders": []}
        return self.strategy_experiments.detail(variant_id, trade_limit, order_limit)

    def aggressive_edge_sample_candidates(self, version: str = "V12", limit: int = 8, offset: int = 0) -> dict[str, Any]:
        """样本页当前版本候选分页；只读取统一诊断样本池，不影响任何交易策略。"""

        if self.strategy_experiments is None:
            return {
                "enabled": False,
                "version": str(version or "V12").upper(),
                "candidates": [],
                "meta": {
                    "version": str(version or "V12").upper(),
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                    "loaded": 0,
                    "total": 0,
                    "has_more": False,
                    "total_pages": 0,
                },
            }
        return self.strategy_experiments.aggressive_edge_sample_candidates(version, limit, offset)

    def live_settings(self) -> dict[str, Any]:
        if self.live_trading is None:
            return _disabled_live_settings()
        return self.live_trading.settings_payload()

    def update_live_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        settings_payload = self.live_trading.update_settings(payload)
        for runner in self._live_paper_runners():
            runner.sync_config(self.live_trading.config)
        return {"live_trading": settings_payload, "snapshot": self.snapshot()}

    def reload_live_credentials(self) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        env_files = reload_live_credential_env()
        self.live_trading.client.clear_cached_credentials()
        settings_payload = self.live_trading.settings_payload()
        settings_payload["credential_reload"] = {
            "reloaded_at": time.time(),
            "env_files": env_files,
        }
        return {"live_trading": settings_payload, "snapshot": self.snapshot()}

    def set_live_enabled(self, enabled: bool) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        settings_payload = self.live_trading.set_enabled(enabled)
        for runner in self._live_paper_runners():
            runner.sync_config(self.live_trading.config)
        return {"live_trading": settings_payload, "snapshot": self.snapshot()}

    def live_emergency_stop(self) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        result = self.live_trading.emergency_stop()
        for runner in self._live_paper_runners():
            runner.sync_config(self.live_trading.config)
        result["snapshot"] = self.snapshot()
        return result

    def live_open_orders(self, *, force: bool = True) -> dict[str, Any]:
        if self.live_trading is None:
            return {
                "live_open_orders": _disabled_live_open_orders(),
                "snapshot": self.snapshot(),
            }
        result = self.live_trading.open_orders_payload(force=force)
        return {"live_open_orders": result, "snapshot": self.snapshot()}

    def live_evidence(self, external_order_id: str | None = None, *, force: bool = True) -> dict[str, Any]:
        if self.live_trading is None:
            return {
                "live_evidence": _disabled_live_evidence(external_order_id),
                "snapshot": self.snapshot(),
            }
        result = self.live_trading.evidence_payload(external_order_id, force=force)
        return {"live_evidence": result, "snapshot": self.snapshot()}

    def live_preflight(self) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        with self._lock:
            market = self.current_market
            price, quotes, _source = self._execution_market_data_locked()
        return {
            "live_preflight": self.live_trading.preflight(market, price, quotes),
            "snapshot": self.snapshot(),
        }

    def refresh_live_preflight(self) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        market = self._refresh_market()
        if market is not None:
            self._rest_fallback_snapshot(market)
        return self.live_preflight()

    def run_live_once(
        self,
        *,
        confirm: str,
        max_stake_dollars: float,
        acknowledge_compliance: bool = False,
        disable_after: bool = True,
        refresh: bool = True,
        reconcile_wait_seconds: float = 0.0,
        reconcile_poll_seconds: float = 1.0,
        wait_ready_seconds: float = 0.0,
        ready_poll_seconds: float = 2.0,
        include_evidence: bool = True,
    ) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        if str(confirm or "").strip() != LIVE_ONCE_CONFIRM_PHRASE:
            raise ValueError(f"confirm must be {LIVE_ONCE_CONFIRM_PHRASE}")
        try:
            max_stake = float(max_stake_dollars)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_stake_dollars must be a number") from exc
        if max_stake <= 0:
            raise ValueError("max_stake_dollars must be positive")
        if self.live_trading.config.enabled:
            raise RuntimeError("one-shot live run requires the live switch to be off before starting")
        if acknowledge_compliance:
            self.live_trading.update_settings({"compliance_acknowledged": True})
        max_wait = max(0.0, min(1800.0, float(wait_ready_seconds or 0.0)))
        poll_seconds = max(0.25, min(30.0, float(ready_poll_seconds or 2.0)))
        deadline = time.time() + max_wait
        preflight_attempts = 0
        ready_wait_started_at = time.time()
        preflight: dict[str, Any] | None = None
        while True:
            preflight_attempts += 1
            if refresh:
                market = self._refresh_market()
                if market is not None:
                    self._rest_fallback_snapshot(market)
            with self._lock:
                market = self.current_market
                price, quotes, _source = self._execution_market_data_locked()
            if market is None:
                if max_wait <= 0 or time.time() >= deadline:
                    message = "current market unavailable for one-shot live run"
                    raise LiveOnceBlockedError(
                        _live_once_blocked_payload(
                            message,
                            variant_id=self.live_trading.variant_id,
                            combo=self.live_trading.combo,
                            blocked_keys=["market"],
                            fatal_keys=[],
                            waitable_keys=["market"],
                            preflight=None,
                            preflight_attempts=preflight_attempts,
                            wait_ready_seconds=max_wait,
                            ready_wait_started_at=ready_wait_started_at,
                        )
                    )
                time.sleep(min(poll_seconds, max(0.0, deadline - time.time())))
                continue
            preflight = self.live_trading.preflight(market, price, quotes)
            if preflight.get("ready") or preflight.get("arming_ready"):
                break
            blocked_keys = [str(row.get("key") or "") for row in preflight.get("blocked_checks") or []]
            one_shot_blocked_keys = [key for key in blocked_keys if key != "enabled"]
            fatal_keys = [key for key in one_shot_blocked_keys if key not in LIVE_ONCE_WAITABLE_BLOCKERS]
            if max_wait <= 0 or fatal_keys or time.time() >= deadline:
                blocked = ", ".join(one_shot_blocked_keys or blocked_keys)
                message = f"one-shot live preflight blocked: {blocked or 'unknown'}"
                waitable_keys = [key for key in one_shot_blocked_keys if key in LIVE_ONCE_WAITABLE_BLOCKERS]
                raise LiveOnceBlockedError(
                    _live_once_blocked_payload(
                        message,
                        variant_id=self.live_trading.variant_id,
                        combo=self.live_trading.combo,
                        blocked_keys=one_shot_blocked_keys or blocked_keys,
                        fatal_keys=fatal_keys,
                        waitable_keys=waitable_keys,
                        preflight=preflight,
                        preflight_attempts=preflight_attempts,
                        wait_ready_seconds=max_wait,
                        ready_wait_started_at=ready_wait_started_at,
                    )
                )
            time.sleep(min(poll_seconds, max(0.0, deadline - time.time())))
        result = self.live_trading.run_once_from_state(
            market,
            price,
            quotes,
            max_stake_dollars=max_stake,
            disable_after=disable_after,
            reconcile_wait_seconds=reconcile_wait_seconds,
            reconcile_poll_seconds=reconcile_poll_seconds,
        )
        result["preflight"] = preflight
        result["preflight_attempts"] = preflight_attempts
        result["wait_ready_seconds"] = round(max_wait, 3)
        result["waited_ready_seconds"] = round(max(0.0, time.time() - ready_wait_started_at), 3)
        if include_evidence:
            order_id = None
            if isinstance(result.get("last_order"), dict):
                order_id = result["last_order"].get("order_id")
            if not order_id and isinstance(result.get("reconcile"), dict):
                order_id = result["reconcile"].get("external_order_id")
            result["evidence"] = self.live_trading.evidence_payload(str(order_id or ""), force=True)
        try:
            audit_path = self._save_live_once_audit(result)
            if audit_path:
                result["audit"] = {
                    "saved": True,
                    "path": audit_path,
                    "sanitized": True,
                }
        except Exception as exc:  # noqa: BLE001 - 不能因为本地审计文件写入失败遮蔽真实订单结果。
            result["audit"] = {
                "saved": False,
                "error": f"{type(exc).__name__}: {exc}",
                "sanitized": True,
            }
        result["snapshot"] = self.snapshot()
        return {"live_once": result}

    def _save_live_once_audit(self, result: dict[str, Any]) -> str | None:
        if not isinstance(result, dict):
            return None
        last_order = result.get("last_order") if isinstance(result.get("last_order"), dict) else {}
        order_id = str((last_order or {}).get("order_id") or "").strip()
        if not result.get("submitted") and not order_id:
            return None
        audit_dir = self.settings.live_trading_db_path.parent / LIVE_ONCE_AUDIT_DIR_NAME
        audit_dir.mkdir(parents=True, exist_ok=True)
        created_at = time.time()
        created_tag = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(created_at))
        order_tag = _audit_filename_part(order_id or "no-order-id")
        path = audit_dir / f"live-once-{created_tag}-{int(created_at * 1000)}-{order_tag}.json"
        result["audit"] = {
            "saved": True,
            "path": str(path),
            "sanitized": True,
        }
        payload = {
            "created_at": created_at,
            "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at)),
            "purpose": f"{result.get('variant_id') or LIVE_VARIANT_ID} live one-shot audit",
            "live_once": _sanitize_live_audit_payload(result),
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
        return str(path)

    def strategy_experiments_retrospective(
        self,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        if start_at is not None and end_at is not None and end_at < start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
        if self.strategy_experiments is None:
            return {"enabled": False, "window": {"start_at": start_at, "end_at": end_at}, "variants": []}
        return self.strategy_experiments.retrospective(start_at, end_at)

    def strategy_experiments_tables(
        self,
        trade_limit: int = RECENT_TRADES_DEFAULT_LIMIT,
        order_limit: int = ORDERS_DEFAULT_LIMIT,
        order_status_filter: str = "all",
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        if start_at is not None and end_at is not None and end_at < start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
        if self.strategy_experiments is None:
            return {
                "enabled": False,
                "open_trades": [],
                "recent_orders": [],
                "recent_orders_meta": {"limit": order_limit, "loaded": 0, "total": 0, "has_more": False},
                "recent_trades": [],
                "recent_trades_summary": {},
                "recent_trades_meta": {"limit": trade_limit, "loaded": 0, "total": 0, "has_more": False},
            }
        return self.strategy_experiments.tables(
            trade_limit=trade_limit,
            order_limit=order_limit,
            order_status_filter=order_status_filter,
            start_at=start_at,
            end_at=end_at,
        )

    def recent_trades_page(
        self,
        limit: int = RECENT_TRADES_DEFAULT_LIMIT,
        offset: int = 0,
        start_at: float | None = None,
        end_at: float | None = None,
        account_scope: str = "main",
        variant_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(RECENT_TRADES_MAX_LIMIT, int(limit)))
        offset = max(0, int(offset))
        if start_at is not None and end_at is not None and end_at < start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
        scope = str(account_scope or "main").strip().lower().replace("-", "_")
        if scope == "live":
            if self.live_trading is None:
                raise ValueError("live trading is disabled in this runtime")
            page = self._live_strategy_recent_trades_page(variant_id, limit, offset, start_at, end_at)
            page["recent_trades"] = self._decorate_recent_trades(page["recent_trades"])
            return page
        if scope in {
            "live_paper",
            "paper_live",
            "single_fak_real_paper",
            "single_fak_real_paper_stop_win",
        }:
            runner_variant_id = LIVE_PAPER_STOP_WIN_VARIANT_ID if scope == "single_fak_real_paper_stop_win" else variant_id
            page = self._live_paper_runner_for_variant(runner_variant_id).recent_trades_page(
                limit,
                offset,
                start_at,
                end_at,
            )
            page["recent_trades"] = self._decorate_recent_trades(page["recent_trades"])
            return page
        if scope in {"strategy_experiment", "experiment", "strategy_experiments"}:
            if not variant_id:
                raise ValueError("variant_id is required for strategy_experiment scope")
            if self.strategy_experiments is None:
                raise ValueError("strategy experiments are not enabled")
            return self.strategy_experiments.recent_trades_page(variant_id, limit, offset, start_at, end_at)
        total = self.store.recent_trade_count("BTC", start_at, end_at)
        rows = self._decorate_recent_trades(self.store.recent_trades(limit, offset, "BTC", start_at, end_at))
        summary = self.store.recent_trade_summary("BTC", start_at, end_at)
        loaded = min(total, offset + len(rows))
        return {
            "recent_trades": rows,
            "recent_trades_summary": summary,
            "recent_trades_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "start_at": start_at,
                "end_at": end_at,
            },
        }

    def orders_page(
        self,
        limit: int = ORDERS_DEFAULT_LIMIT,
        offset: int = 0,
        status_filter: str = "all",
        account_scope: str = "main",
        variant_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(ORDERS_MAX_LIMIT, int(limit)))
        offset = max(0, int(offset))
        status_key = normalize_paper_order_status_filter(status_filter)
        scope = str(account_scope or "main").strip().lower().replace("-", "_")
        if scope == "live":
            if self.live_trading is None:
                raise ValueError("live trading is disabled in this runtime")
            return self._live_strategy_orders_page(variant_id, limit, offset, status_key)
        if scope in {
            "live_paper",
            "paper_live",
            "single_fak_real_paper",
            "single_fak_real_paper_stop_win",
        }:
            runner_variant_id = LIVE_PAPER_STOP_WIN_VARIANT_ID if scope == "single_fak_real_paper_stop_win" else variant_id
            return self._live_paper_runner_for_variant(runner_variant_id).orders_page(limit, offset, status_key)
        if scope in {"strategy_experiment", "experiment", "strategy_experiments"}:
            if not variant_id:
                raise ValueError("variant_id is required for strategy_experiment scope")
            if self.strategy_experiments is None:
                raise ValueError("strategy experiments are not enabled")
            return self.strategy_experiments.orders_page(variant_id, limit, offset, status_key)
        total = self.store.paper_order_count("BTC", status_key)
        rows = self.store.recent_paper_orders(limit, offset, "BTC", status_key)
        loaded = min(total, offset + len(rows))
        return {
            "recent_orders": rows,
            "recent_orders_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "status_filter": status_key,
            },
        }

    def order_fills(self, order_id: int, account_scope: str = "main", variant_id: str | None = None) -> dict[str, Any]:
        order_id = max(1, int(order_id))
        scope = str(account_scope or "main").strip().lower().replace("-", "_")
        if scope == "live":
            if self.live_trading is None:
                raise ValueError("live trading is disabled in this runtime")
            store, _meta, close_store = self._live_strategy_read_store(variant_id)
            try:
                fills = store.paper_order_fills(order_id)
            finally:
                self._close_read_store(store, close_store)
            return {
                "order_id": order_id,
                "fills": fills,
            }
        if scope in {
            "live_paper",
            "paper_live",
            "single_fak_real_paper",
            "single_fak_real_paper_stop_win",
        }:
            runner_variant_id = LIVE_PAPER_STOP_WIN_VARIANT_ID if scope == "single_fak_real_paper_stop_win" else variant_id
            return {
                "order_id": order_id,
                "fills": self._live_paper_runner_for_variant(runner_variant_id).store.paper_order_fills(order_id),
            }
        if scope in {"strategy_experiment", "experiment", "strategy_experiments"}:
            if not variant_id:
                raise ValueError("variant_id is required for strategy_experiment scope")
            if self.strategy_experiments is None:
                raise ValueError("strategy experiments are not enabled")
            return self.strategy_experiments.order_fills(variant_id, order_id)
        return {
            "order_id": order_id,
            "fills": self.store.paper_order_fills(order_id),
        }

    def sell_live_trade(self, trade_id: int) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        with self._lock:
            _price, quotes, _source = self._execution_market_data_locked()
        result = self.live_trading.sell_trade(max(1, int(trade_id)), quotes)
        result["snapshot"] = self.snapshot()
        return result

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        result = self.store.cancel_paper_order(max(1, int(order_id)), "manual")
        result.update(self.orders_page(ORDERS_DEFAULT_LIMIT, 0))
        return result

    def cancel_orders(self, scope: str = "current_market") -> dict[str, Any]:
        normalized_scope = str(scope or "current_market").strip().lower().replace("-", "_")
        if normalized_scope not in {"current_market", "all"}:
            raise ValueError("scope must be current_market or all")

        if normalized_scope == "current_market":
            with self._lock:
                market = self.current_market
            if market is None:
                result = {
                    "canceled": [],
                    "not_canceled": {"scope": "current market unavailable"},
                    "released_cash": 0.0,
                    "orders": [],
                }
            else:
                result = self.store.cancel_active_paper_orders(
                    round_id=market.round_id,
                    reason="manual current market",
                )
        else:
            result = self.store.cancel_active_paper_orders(reason="manual all")

        result["scope"] = normalized_scope
        result.update(self.orders_page(ORDERS_DEFAULT_LIMIT, 0))
        return result

    def equity_curve_window(
        self,
        days: int = EQUITY_CURVE_DEFAULT_DAYS,
        max_points: int = EQUITY_CURVE_DEFAULT_MAX_POINTS,
        account_scope: str = "main",
        variant_id: str | None = None,
    ) -> dict[str, Any]:
        days = max(1, min(365, int(days)))
        max_points = max(2, min(EQUITY_CURVE_MAX_POINTS, int(max_points)))
        normalized_scope = str(account_scope or "main").strip().lower().replace("-", "_")
        if normalized_scope in {"main", "main_account"}:
            rows = self.store.equity_curve_window(days, max_points)
            return {
                "equity_curve": rows,
                "equity_curve_meta": {
                    "account_scope": "main",
                    "label": "主账户",
                    "days": days,
                    "max_points": max_points,
                    "points": len(rows),
                    "initial_balance": self.settings.initial_balance,
                },
            }
        if normalized_scope in {"strategy_experiment", "experiment", "strategy_experiments"}:
            if self.strategy_experiments is None:
                raise ValueError("strategy experiments are not enabled")
            return self.strategy_experiments.equity_curve_window(variant_id, days, max_points)
        if normalized_scope == "live":
            if self.live_trading is None:
                raise ValueError("live trading is disabled in this runtime")
            return self._live_strategy_equity_curve_window(variant_id, days, max_points)
        if normalized_scope in {
            "live_paper",
            "paper_live",
            "single_fak_real_paper",
            "single_fak_real_paper_stop_win",
        }:
            runner_variant_id = (
                LIVE_PAPER_STOP_WIN_VARIANT_ID if normalized_scope == "single_fak_real_paper_stop_win" else variant_id
            )
            return self._live_paper_runner_for_variant(runner_variant_id).equity_curve_window(days, max_points)
        raise ValueError("account_scope must be main, strategy_experiment, live, or live_paper")

    def _live_strategy_read_store(self, variant_id: str | None) -> tuple[TradeStore, dict[str, Any], bool]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        meta = _live_strategy_meta(variant_id or self.live_trading.variant_id)
        normalized = str(meta["variant_id"])
        if normalized == self.live_trading.variant_id:
            return self.live_trading.store, meta, False
        return TradeStore(_live_strategy_db_path(self.settings, normalized), self.live_trading.config.initial_balance), meta, True

    @staticmethod
    def _close_read_store(store: TradeStore, close_store: bool) -> None:
        if close_store:
            store.conn.close()

    def _live_strategy_recent_trades_page(
        self,
        variant_id: str | None,
        limit: int,
        offset: int,
        start_at: float | None,
        end_at: float | None,
    ) -> dict[str, Any]:
        store, meta, close_store = self._live_strategy_read_store(variant_id)
        try:
            normalized = str(meta["variant_id"])
            combo = str(meta.get("combo") or normalized)
            total = store.recent_trade_count("BTC", start_at, end_at)
            rows = _tag_live_rows(
                store.recent_trades(limit, offset, "BTC", start_at, end_at),
                variant_id=normalized,
                combo=combo,
            )
            summary = store.recent_trade_summary("BTC", start_at, end_at)
        finally:
            self._close_read_store(store, close_store)
        loaded = min(total, offset + len(rows))
        return {
            "recent_trades": rows,
            "recent_trades_summary": summary,
            "recent_trades_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "start_at": start_at,
                "end_at": end_at,
            },
        }

    def _live_strategy_orders_page(
        self,
        variant_id: str | None,
        limit: int,
        offset: int,
        status_key: str,
    ) -> dict[str, Any]:
        store, meta, close_store = self._live_strategy_read_store(variant_id)
        try:
            normalized = str(meta["variant_id"])
            combo = str(meta.get("combo") or normalized)
            total = store.paper_order_count("BTC", status_key)
            rows = _tag_live_rows(
                store.recent_paper_orders(limit, offset, "BTC", status_key),
                variant_id=normalized,
                combo=combo,
            )
        finally:
            self._close_read_store(store, close_store)
        loaded = min(total, offset + len(rows))
        return {
            "recent_orders": rows,
            "recent_orders_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "status_filter": status_key,
            },
        }

    def _live_strategy_equity_curve_window(
        self,
        variant_id: str | None,
        days: int,
        max_points: int,
    ) -> dict[str, Any]:
        store, meta, close_store = self._live_strategy_read_store(variant_id)
        try:
            normalized = str(meta["variant_id"])
            combo = str(meta.get("combo") or normalized)
            rows = store.equity_curve_window(days, max_points)
            account = store.account()
        finally:
            self._close_read_store(store, close_store)
        return {
            "equity_curve": rows,
            "equity_curve_meta": {
                "account_scope": "live",
                "variant_id": normalized,
                "combo": combo,
                "label": combo,
                "days": days,
                "max_points": max_points,
                "points": len(rows),
                "initial_balance": float(account["initial_balance"]),
            },
        }

    def _pair_strategy_runtime_locked(self) -> dict[str, Any]:
        _price, quotes, _source = self._paper_market_data_locked()
        state = _pair_quote_state(quotes, time.time())
        return {
            "enabled": self.pair_strategy_enabled,
            "stop_loss_streak": self.pair_stop_loss_streak,
            "last_event": dict(self.last_pair_event or {}),
            "pair_cost": state.get("pair_cost"),
            "bid_sum": state.get("bid_sum"),
            "quote_age_ms": state.get("quote_age_ms"),
        }

    def _decorate_open_trades(self, rows: list[dict[str, Any]], runtime: dict[str, Any]) -> list[dict[str, Any]]:
        current_market = runtime.get("current_market") if isinstance(runtime.get("current_market"), dict) else {}
        latest_price = runtime.get("latest_price") if isinstance(runtime.get("latest_price"), dict) else {}
        latest_quotes = runtime.get("latest_quotes") if isinstance(runtime.get("latest_quotes"), dict) else {}
        current_price = _maybe_float(latest_price.get("chainlink")) or _maybe_float(latest_price.get("binance"))
        now = time.time()
        decorated: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            settlement_pending = _is_pending_settlement_trade(item, now)
            item["strategy_type"] = _strategy_type(row.get("reason"))
            item["exit_note"] = _exit_note(row.get("reason"))
            item["max_payout"] = _round_money(_maybe_float(row.get("shares")))
            item["max_profit"] = _round_money((_maybe_float(row.get("shares")) or 0.0) - (_maybe_float(row.get("stake")) or 0.0))
            item["max_loss"] = _round_money(_maybe_float(row.get("stake")) or 0.0)
            item["entry_probability_pct"] = _round_pct((_maybe_float(row.get("entry_price")) or 0.0) * 100.0)
            item["settlement_pending"] = settlement_pending
            item["position_state"] = TRADE_STATUS_PENDING_SETTLEMENT if settlement_pending else str(row.get("status") or "")
            item["position_state_label"] = "等待官方结算" if settlement_pending else ""
            item["is_current_market"] = current_market.get("round_id") == row.get("round_id")
            if item["is_current_market"] and not settlement_pending:
                item["current_price"] = current_price
                item["current_distance_bps"] = _distance_bps(current_price, _maybe_float(row.get("target_price")))
                quote = latest_quotes.get(str(row.get("side"))) if isinstance(latest_quotes.get(str(row.get("side"))), dict) else {}
                bid = _maybe_float(quote.get("best_bid"))
                ask = _maybe_float(quote.get("best_ask"))
                item["current_bid"] = bid
                item["current_ask"] = ask
                item["quote_source"] = quote.get("source")
                item["quote_updated_at_ms"] = _maybe_int(quote.get("updated_at_ms"))
                if bid is not None:
                    exit_value = (_maybe_float(row.get("shares")) or 0.0) * bid
                    unrealized_pnl = exit_value - (_maybe_float(row.get("stake")) or 0.0)
                    item["exit_value"] = _round_money(exit_value)
                    item["unrealized_pnl"] = _round_money(unrealized_pnl)
                    item["unrealized_roi_pct"] = _roi_pct(unrealized_pnl, _maybe_float(row.get("stake")))
            decorated.append(item)
        return decorated

    def _market_paper_order_quote(
        self,
        row: dict[str, Any],
        *,
        force_refresh: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """读取市场页 Paper 持仓盘口；用短缓存降低页面轮询带来的 CLOB 请求量。"""

        token_id = str(row.get("up_token") or "").strip()
        side = str(row.get("side") or "").strip()
        if not token_id or not side:
            return None, "missing_token_or_side"
        now = time.time()
        cache_key = f"{token_id}:{side}"
        cached = self._market_paper_quote_cache.get(cache_key)
        if not force_refresh and cached and now - cached[0] <= 8.0:
            return dict(cached[1]), "cache"
        try:
            quote = self.polymarket.get_quote(token_id, side).to_dict()
        except Exception as exc:  # noqa: BLE001 - 列表估值失败不能影响市场页状态接口。
            logger.debug(
                "market paper quote failed round=%s side=%s token=%s error=%s",
                row.get("round_id"),
                side,
                token_id,
                exc,
            )
            return None, f"{type(exc).__name__}: {exc}"
        self._market_paper_quote_cache[cache_key] = (now, dict(quote))
        if len(self._market_paper_quote_cache) > 120:
            expired_keys = [
                key
                for key, (cached_at, _payload) in self._market_paper_quote_cache.items()
                if now - cached_at > 30.0
            ]
            for key in expired_keys:
                self._market_paper_quote_cache.pop(key, None)
        return dict(quote), "clob"

    def _market_scout_order_link_payload(self, row: dict[str, Any]) -> dict[str, str]:
        """为历史 Paper 订单补正 Polymarket 链接，统一走官方 market 跳转入口。"""

        market_slug = str(row.get("round_id") or "").strip()
        stored_event_slug = str(row.get("event_slug") or "").strip()
        if not market_slug:
            return {"event_slug": stored_event_slug, "url": str(row.get("url") or "")}
        if stored_event_slug:
            return {
                "event_slug": stored_event_slug,
                "url": _market_scout_polymarket_url(market_slug, stored_event_slug),
            }
        cached = self._market_scout_link_cache.get(market_slug)
        if cached:
            return dict(cached)
        payload = {
            "event_slug": "",
            "url": _market_scout_polymarket_url(market_slug, ""),
        }
        try:
            raw = self.polymarket.get_market_raw_by_slug(market_slug)
        except Exception as exc:  # noqa: BLE001 - 链接兜底失败不能影响市场页状态接口。
            logger.debug("market scout link lookup failed slug=%s error=%s", market_slug, exc)
            raw = None
        if isinstance(raw, dict):
            event = _market_scout_first_event(raw)
            event_slug = str(event.get("slug") or "").strip() if event else ""
            payload = {
                "event_slug": event_slug,
                "url": _market_scout_polymarket_url(market_slug, event_slug),
            }
        self._market_scout_link_cache[market_slug] = payload
        return dict(payload)

    def _decorate_market_paper_orders(
        self,
        rows: list[dict[str, Any]],
        *,
        force_quote_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """补齐市场页 Paper 订单的结算和未结算估值，前端无需再猜测交易输赢。"""

        decorated: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            link_payload = self._market_scout_order_link_payload(item)
            item["event_slug"] = link_payload.get("event_slug") or item.get("event_slug") or ""
            item["url"] = link_payload.get("url") or item.get("url") or ""
            trade_status = str(item.get("trade_status") or "").upper()
            trade_id = item.get("trade_id")
            settled_at = _maybe_float(item.get("trade_settled_at"))
            pnl = _maybe_float(item.get("trade_pnl"))
            payout = _maybe_float(item.get("trade_payout"))
            spent = _maybe_float(item.get("cash_spent"))
            requested = _maybe_float(item.get("requested_cash"))
            trade_stake = _maybe_float(item.get("trade_stake"))
            shares = _maybe_float(item.get("trade_shares")) or _maybe_float(item.get("filled_shares"))
            stake_basis = trade_stake if trade_stake and trade_stake > 0 else spent if spent and spent > 0 else requested

            item["settled_at"] = settled_at
            item["payout"] = _round_money(payout)
            item["net_pnl"] = _round_money(pnl) if settled_at is not None or trade_status == "SETTLED" else None
            item["roi_pct"] = _roi_pct(pnl, stake_basis) if item["net_pnl"] is not None else None
            item["settlement_source"] = item.get("trade_settlement_source")
            item["max_payout"] = _round_money(shares)
            item["max_profit"] = _round_money((shares or 0.0) - (stake_basis or 0.0)) if shares is not None and stake_basis is not None else None
            item["max_loss"] = _round_money(stake_basis)
            item["current_bid"] = None
            item["current_ask"] = None
            item["exit_value"] = None
            item["unrealized_pnl"] = None
            item["unrealized_roi_pct"] = None
            item["valuation_source"] = ""
            item["valuation_error"] = ""
            item["quote_refresh_forced"] = force_quote_refresh

            if trade_id and item["net_pnl"] is None and shares and shares > 0 and stake_basis and stake_basis > 0:
                quote, quote_source = self._market_paper_order_quote(item, force_refresh=force_quote_refresh)
                item["valuation_source"] = quote_source
                if quote:
                    bid = _maybe_float(quote.get("best_bid"))
                    ask = _maybe_float(quote.get("best_ask"))
                    item["current_bid"] = _round_float(bid, 4)
                    item["current_ask"] = _round_float(ask, 4)
                    if bid is not None and bid > 0:
                        exit_value = shares * bid
                        unrealized_pnl = exit_value - stake_basis
                        item["exit_value"] = _round_money(exit_value)
                        item["unrealized_pnl"] = _round_money(unrealized_pnl)
                        item["unrealized_roi_pct"] = _roi_pct(unrealized_pnl, stake_basis)
                    else:
                        item["valuation_error"] = "缺少当前买一价"
                else:
                    item["valuation_error"] = quote_source

            if not trade_id:
                result = "NO_POSITION"
                label = "无持仓"
            elif item["net_pnl"] is None:
                result = "OPEN"
                label = "未结算"
            elif item["net_pnl"] > 0:
                result = "WIN"
                label = "赢"
            elif item["net_pnl"] < 0:
                result = "LOSS"
                label = "输"
            else:
                result = "FLAT"
                label = "走平"

            item["settlement_result"] = result
            item["settlement_result_label"] = label
            decorated.append(item)
        return decorated

    def _decorate_recent_trades(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        now = time.time()
        for row in rows:
            item = dict(row)
            item["strategy_type"] = _strategy_type(row.get("reason"))
            item["exit_note"] = _exit_note(row.get("reason"))
            stake = _maybe_float(row.get("stake"))
            pnl = _maybe_float(row.get("pnl"))
            shares = _maybe_float(row.get("shares"))
            item["max_payout"] = _round_money(shares)
            item["max_profit"] = _round_money((shares or 0.0) - (stake or 0.0))
            item["max_loss"] = _round_money(stake or 0.0)
            item["entry_probability_pct"] = _round_pct((_maybe_float(row.get("entry_price")) or 0.0) * 100.0)
            item["roi_pct"] = _roi_pct(pnl, stake)
            item["resolved_return"] = _round_money(_maybe_float(row.get("payout")))
            item["final_distance_bps"] = _distance_bps(_maybe_float(row.get("final_price")), _maybe_float(row.get("target_price")))
            if _is_pending_settlement_trade(item, now):
                item["settlement_pending"] = True
                item["status_display"] = TRADE_STATUS_PENDING_SETTLEMENT
                item["status_label"] = "等待官方结算"
            else:
                item["settlement_pending"] = False
                item["status_display"] = str(row.get("status") or "")
                item["status_label"] = ""
            item["settlement_source_label"] = _settlement_source_label(row.get("settlement_source"), item)
            decorated.append(item)
        return decorated

    def _metrics_with_open_marks(self, metrics: dict[str, Any], open_trades: list[dict[str, Any]]) -> dict[str, Any]:
        enriched = dict(metrics)
        open_mark_value = 0.0
        unrealized_pnl = 0.0
        for row in open_trades:
            stake = _maybe_float(row.get("stake")) or 0.0
            exit_value = _maybe_float(row.get("exit_value"))
            if exit_value is None:
                open_mark_value += stake
                continue
            open_mark_value += exit_value
            unrealized_pnl += exit_value - stake
        enriched["open_mark_value"] = _round_money(open_mark_value) or 0.0
        enriched["unrealized_pnl"] = _round_money(unrealized_pnl) or 0.0
        cash_balance = _maybe_float(enriched.get("cash_balance")) or 0.0
        initial_balance = _maybe_float(enriched.get("initial_balance")) or self.settings.initial_balance
        estimated_total_equity = cash_balance + open_mark_value
        estimated_total_pnl = estimated_total_equity - initial_balance
        enriched["estimated_total_equity"] = _round_money(estimated_total_equity) or 0.0
        enriched["estimated_total_pnl"] = _round_money(estimated_total_pnl) or 0.0
        enriched["estimated_total_pnl_pct"] = _round_pct(estimated_total_pnl / initial_balance * 100.0) if initial_balance else 0.0
        return enriched


class StrategyExperimentRunner:
    def __init__(self, settings: Settings, polymarket: PolymarketClient, price_fallback: PublicPriceClient) -> None:
        self.settings = settings
        self.polymarket = polymarket
        self.variants = selected_strategy_variants(settings.strategy_experiments_variants)
        self._lock = threading.RLock()
        self._bots: dict[str, PaperTradingBot] = {}
        self._errors: dict[str, str | None] = {}
        self._official_broadcast_errors: dict[str, str | None] = {}
        self.run_count = 0
        self.last_run_at: float | None = None
        self.official_broadcast_count = 0
        self.last_official_broadcast_at: float | None = None
        self._official_settlement_next_at: dict[str, float] = {}
        for variant in self.variants:
            variant_settings = self._settings_for_variant(settings, variant)
            store = TradeStore(variant_settings.db_path, variant_settings.initial_balance)
            bot = PaperTradingBot(variant_settings, store)
            bot.polymarket = polymarket
            bot.price_fallback = price_fallback
            bot.configure_strategy_experiment_variant(variant)
            self._bots[variant.variant_id] = bot
            self._errors[variant.variant_id] = None
            self._official_broadcast_errors[variant.variant_id] = None

    def _settings_for_variant(self, settings: Settings, variant: StrategyVariant) -> Settings:
        db_path = settings.strategy_experiments_db_dir / f"{variant.variant_id.lower()}.sqlite3"
        max_open_trades = (
            max(settings.max_open_trades, 2)
            if variant.strategy_family in {STRATEGY_FAMILY_PAIR, STRATEGY_FAMILY_LLM_SUPER_AGENT}
            else settings.max_open_trades
        )
        return replace(
            settings,
            db_path=db_path,
            paper_entry_order_type=variant.order_type,
            max_open_trades=max_open_trades,
            strategy_experiments_enabled=False,
            strategy_experiments_variants="",
            live_trading_runtime_enabled=False,
        )

    def set_paper_trading_paused(
        self,
        paused: bool,
        *,
        cancel_active: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            variants = tuple(self.variants)
        variant_results: dict[str, dict[str, Any]] = {}
        canceled_count = 0
        released_cash = 0.0
        for variant in variants:
            bot = self._bots[variant.variant_id]
            bot._set_paper_pause_state(paused, now)
            cancel_result = (
                bot._cancel_active_paper_orders_for_pause(now)
                if paused and cancel_active
                else {"canceled": [], "released_cash": 0.0, "orders": []}
            )
            if paused:
                bot._set_paper_paused_signal()
            summary = _paper_cancel_summary(cancel_result)
            variant_results[variant.variant_id] = summary
            canceled_count += int(summary.get("canceled_count") or 0)
            released_cash = round(released_cash + (_maybe_float(summary.get("released_cash")) or 0.0), 6)
        return {
            "paused": bool(paused),
            "canceled_count": canceled_count,
            "released_cash": released_cash,
            "variants": variant_results,
        }

    def run_from_state(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        now = time.time()
        with self._lock:
            self.run_count += 1
            self.last_run_at = now
            variants = tuple(self.variants)
        self._settle_pending_official_rounds(variants, now)
        for variant in variants:
            bot = self._bots[variant.variant_id]
            try:
                bot.store.upsert_round(market)
                self._save_price_tick(bot.store, price, now)
                with bot._lock:
                    bot.current_market = market
                    bot.latest_price = dict(price)
                    bot.latest_quotes = _copy_quotes(quotes)
                    bot.paper_price = dict(price)
                    bot.paper_quotes = _copy_quotes(quotes)
                    bot.ws_status = {
                        "market": "strategy-experiment",
                        "price": "strategy-experiment",
                        "browser_feed_at": now,
                        "backend_rest_fallback_at": None,
                    }
                self._settle_due_from_price(bot.store, price, now)
                bot._run_strategy_from_state()
                bot.store.record_equity()
                with self._lock:
                    self._errors[variant.variant_id] = None
            except Exception as exc:  # noqa: BLE001 - one experiment must not stop the main bot or other variants.
                with self._lock:
                    self._errors[variant.variant_id] = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            variants_meta = tuple(self.variants)
            errors = dict(self._errors)
            official_errors = dict(self._official_broadcast_errors)
            run_count = self.run_count
            last_run_at = self.last_run_at
            official_broadcast_count = self.official_broadcast_count
            last_official_broadcast_at = self.last_official_broadcast_at
        variants: list[dict[str, Any]] = []
        for variant in variants_meta:
            bot = self._bots[variant.variant_id]
            variants.append(
                self._variant_payload(
                    variant,
                    bot,
                    errors.get(variant.variant_id),
                    official_errors.get(variant.variant_id),
                )
            )
        return {
            "enabled": True,
            "db_dir": str(self.settings.strategy_experiments_db_dir),
            "run_count": run_count,
            "last_run_at": last_run_at,
            "official_broadcast_count": official_broadcast_count,
            "last_official_broadcast_at": last_official_broadcast_at,
            "decision_summary": _experiment_decision_summary(variants),
            "profit_summary": _experiment_profit_summary(variants),
            "variants": variants,
        }

    def detail(self, variant_id: str, trade_limit: int = 50, order_limit: int = 50) -> dict[str, Any]:
        variant, bot = self._variant_bot(variant_id)
        with self._lock:
            last_error = self._errors.get(variant.variant_id)
            official_broadcast_error = self._official_broadcast_errors.get(variant.variant_id)
        trade_limit = max(1, min(200, int(trade_limit)))
        order_limit = max(1, min(200, int(order_limit)))
        detail = self._variant_payload(
            variant,
            bot,
            last_error,
            official_broadcast_error,
        )
        return {
            "enabled": True,
            "variant": detail,
            "recent_trades_page": bot.recent_trades_page(trade_limit, 0),
            "recent_orders_page": bot.orders_page(order_limit, 0),
        }

    def aggressive_edge_sample_candidates(self, version: str = "V12", limit: int = 8, offset: int = 0) -> dict[str, Any]:
        """读取样本页候选分页；V4 到 V12 统一从 Diagnostic 数据池做横向对比。"""

        source_variant_id = "SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"
        variant, bot = self._variant_bot(source_variant_id)
        payload = bot.store.aggressive_edge_v2_shadow_candidates(
            "BTC",
            version,
            limit=limit,
            offset=offset,
        )
        payload.update(
            {
                "enabled": True,
                "variant_id": variant.variant_id,
                "combo": variant.combo,
            }
        )
        return payload

    def llm_decision_review(self, limit: int = 80) -> dict[str, Any]:
        limit = max(1, min(300, int(limit)))
        reviews: list[dict[str, Any]] = []
        with self._lock:
            variants_meta = tuple(self.variants)
        for variant in variants_meta:
            if variant.strategy_family != STRATEGY_FAMILY_LLM_SUPER_AGENT:
                continue
            bot = self._bots[variant.variant_id]
            review = bot.store.llm_decision_review(
                limit=limit,
                opportunity_stake=bot.settings.stake_dollars,
                variant_id=variant.variant_id,
            )
            review["combo"] = variant.combo
            review["role"] = variant.role
            reviews.append(review)
        primary = reviews[0] if reviews else None
        return {
            "llm_review": {
                "status": primary["status"] if primary else "DISABLED",
                "generated_at": time.time(),
                "primary": primary,
                "variants": reviews,
            }
        }

    def _variant_bot(self, variant_id: str) -> tuple[StrategyVariant, "PaperTradingBot"]:
        normalized = str(variant_id or "").strip().upper().replace("-", "_")
        by_id = {variant.variant_id: variant for variant in self.variants}
        variant = by_id.get(normalized)
        if variant is None:
            allowed = ", ".join(sorted(by_id))
            raise ValueError(f"variant_id must be one of {allowed}")
        return variant, self._bots[variant.variant_id]

    def recent_trades_page(
        self,
        variant_id: str,
        limit: int,
        offset: int,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        variant, bot = self._variant_bot(variant_id)
        total = bot.store.recent_trade_count("BTC", start_at, end_at)
        rows = _tag_variant_rows(
            bot._decorate_recent_trades(bot.store.recent_trades(limit, offset, "BTC", start_at, end_at)),
            _variant_tags(variant),
        )
        loaded = min(total, offset + len(rows))
        return {
            "recent_trades": rows,
            "recent_trades_summary": bot.store.recent_trade_summary("BTC", start_at, end_at),
            "recent_trades_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "start_at": start_at,
                "end_at": end_at,
            },
        }

    def orders_page(self, variant_id: str, limit: int, offset: int, status_filter: str = "all") -> dict[str, Any]:
        variant, bot = self._variant_bot(variant_id)
        status_key = normalize_paper_order_status_filter(status_filter)
        total = bot.store.paper_order_count("BTC", status_key)
        rows = _tag_variant_rows(
            bot.store.recent_paper_orders(limit, offset, "BTC", status_key),
            _variant_tags(variant),
        )
        loaded = min(total, offset + len(rows))
        return {
            "recent_orders": rows,
            "recent_orders_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "status_filter": status_key,
            },
        }

    def order_fills(self, variant_id: str, order_id: int) -> dict[str, Any]:
        _, bot = self._variant_bot(variant_id)
        return {
            "order_id": max(1, int(order_id)),
            "fills": bot.store.paper_order_fills(max(1, int(order_id))),
        }

    def equity_curve_window(self, variant_id: str | None, days: int, max_points: int) -> dict[str, Any]:
        variant, bot = self._variant_bot(variant_id or "")
        rows = bot.store.equity_curve_window(days, max_points)
        return {
            "equity_curve": rows,
            "equity_curve_meta": {
                "account_scope": "strategy_experiment",
                "variant_id": variant.variant_id,
                "combo": variant.combo,
                "label": variant.combo,
                "days": days,
                "max_points": max_points,
                "points": len(rows),
                "initial_balance": bot.settings.initial_balance,
            },
        }

    def tables(
        self,
        trade_limit: int = RECENT_TRADES_DEFAULT_LIMIT,
        order_limit: int = ORDERS_DEFAULT_LIMIT,
        order_status_filter: str = "all",
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        if start_at is not None and end_at is not None and end_at < start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
        trade_limit = max(1, min(RECENT_TRADES_MAX_LIMIT, int(trade_limit)))
        order_limit = max(1, min(ORDERS_MAX_LIMIT, int(order_limit)))
        order_status_filter = normalize_paper_order_status_filter(order_status_filter)
        with self._lock:
            variants_meta = tuple(self.variants)
        open_rows: list[dict[str, Any]] = []
        order_rows: list[dict[str, Any]] = []
        trade_rows: list[dict[str, Any]] = []
        trade_summaries: list[dict[str, Any]] = []
        total_orders = 0
        total_trades = 0
        for variant in variants_meta:
            bot = self._bots[variant.variant_id]
            variant_tags = _variant_tags(variant)
            open_rows.extend(self._variant_open_trades(bot, variant_tags))
            total_orders += bot.store.paper_order_count("BTC", order_status_filter)
            order_rows.extend(
                _tag_variant_rows(
                    bot.store.recent_paper_orders(order_limit, 0, "BTC", order_status_filter),
                    variant_tags,
                )
            )
            total_trades += bot.store.recent_trade_count("BTC", start_at, end_at)
            trade_rows.extend(
                _tag_variant_rows(
                    bot._decorate_recent_trades(bot.store.recent_trades(trade_limit, 0, "BTC", start_at, end_at)),
                    variant_tags,
                )
            )
            trade_summaries.append(bot.store.recent_trade_summary("BTC", start_at, end_at))
        open_rows.sort(key=lambda row: _maybe_float(row.get("opened_at")) or 0.0, reverse=True)
        order_rows.sort(key=lambda row: _maybe_float(row.get("created_at")) or 0.0, reverse=True)
        trade_rows.sort(
            key=lambda row: _maybe_float(row.get("settled_at")) or _maybe_float(row.get("opened_at")) or 0.0,
            reverse=True,
        )
        order_rows = order_rows[:order_limit]
        trade_rows = trade_rows[:trade_limit]
        return {
            "enabled": True,
            "scope": "strategy_experiments",
            "variant_count": len(variants_meta),
            "open_trades": open_rows,
            "recent_orders": order_rows,
            "recent_orders_meta": {
                "limit": order_limit,
                "offset": 0,
                "loaded": len(order_rows),
                "total": total_orders,
                "has_more": len(order_rows) < total_orders,
                "status_filter": order_status_filter,
            },
            "recent_trades": trade_rows,
            "recent_trades_summary": _merge_trade_summaries(trade_summaries, start_at, end_at),
            "recent_trades_meta": {
                "limit": trade_limit,
                "offset": 0,
                "loaded": len(trade_rows),
                "total": total_trades,
                "has_more": len(trade_rows) < total_trades,
                "start_at": start_at,
                "end_at": end_at,
            },
        }

    @staticmethod
    def _variant_open_trades(bot: "PaperTradingBot", variant_tags: dict[str, Any]) -> list[dict[str, Any]]:
        with bot._lock:
            runtime = {
                "current_market": market_to_payload(bot.current_market),
                "latest_price": dict(bot.latest_price),
                "latest_quotes": _copy_quotes(bot.latest_quotes),
            }
        rows = [row for row in bot.store.open_trades() if row["symbol"] == "BTC"]
        return _tag_variant_rows(bot._decorate_open_trades(rows, runtime), variant_tags)

    def retrospective(self, start_at: float | None = None, end_at: float | None = None) -> dict[str, Any]:
        if start_at is not None and end_at is not None and end_at < start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
        with self._lock:
            variants_meta = tuple(self.variants)
            errors = dict(self._errors)
            official_errors = dict(self._official_broadcast_errors)
            run_count = self.run_count
            last_run_at = self.last_run_at
            official_broadcast_count = self.official_broadcast_count
            last_official_broadcast_at = self.last_official_broadcast_at
        variants: list[dict[str, Any]] = []
        for variant in variants_meta:
            bot = self._bots[variant.variant_id]
            variants.append(
                self._variant_payload(
                    variant,
                    bot,
                    errors.get(variant.variant_id),
                    official_errors.get(variant.variant_id),
                    start_at=start_at,
                    end_at=end_at,
                )
            )
        return {
            "enabled": True,
            "window": {"start_at": start_at, "end_at": end_at},
            "db_dir": str(self.settings.strategy_experiments_db_dir),
            "run_count": run_count,
            "last_run_at": last_run_at,
            "official_broadcast_count": official_broadcast_count,
            "last_official_broadcast_at": last_official_broadcast_at,
            "decision_summary": _experiment_decision_summary(variants),
            "profit_summary": _experiment_profit_summary(variants),
            "variants": sorted(
                variants,
                key=lambda row: (
                    _maybe_float((row.get("recent_trades_summary") or {}).get("total_pnl")) or 0.0,
                    _maybe_float((row.get("recent_trades_summary") or {}).get("roi_pct")) or -999999.0,
                    int((row.get("recent_trades_summary") or {}).get("settled_count") or 0),
                ),
                reverse=True,
            ),
        }

    def _variant_payload(
        self,
        variant: StrategyVariant,
        bot: "PaperTradingBot",
        last_error: str | None,
        official_broadcast_error: str | None,
        *,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        order_summary = bot.store.paper_order_summary("BTC", start_at, end_at)
        open_trades = self._variant_open_trades(bot, _variant_tags(variant))
        metrics = bot._metrics_with_open_marks(bot.store.metrics(), open_trades)
        trade_summary = bot.store.recent_trade_summary("BTC", start_at, end_at)
        return {
            "variant_id": variant.variant_id,
            "combo": variant.combo,
            "strategy_family": variant.strategy_family,
            "order_type": variant.order_type,
            "single_entry_mode": variant.single_entry_mode,
            "signal_side_mode": variant.signal_side_mode,
            "signal_filter_mode": variant.signal_filter_mode,
            "market_data_mode": variant.market_data_mode,
            "price_source_mode": variant.price_source_mode,
            "anti_bot_guard_mode": variant.anti_bot_guard_mode,
            "target_code_completion": variant.target_code_completion,
            "target_report_alignment": variant.target_report_alignment,
            "role": variant.role,
            "db_path": str(bot.settings.db_path),
            "pair_strategy_enabled": bot.pair_strategy_enabled,
            "llm_super_agent_enabled": bot.llm_super_agent_enabled,
            "recent_llm_decisions": bot.store.recent_llm_decisions(3) if bot.llm_super_agent_enabled else [],
            "paper_trading_paused": bot.paper_trading_runtime().get("paused", False),
            "last_signal": dict(bot.last_signal or {}),
            "last_error": last_error,
            "official_broadcast_error": official_broadcast_error,
            "loss_replay_path": str(bot._aggressive_edge_loss_replay.path)
            if variant.signal_filter_mode in AGGRESSIVE_EDGE_FILTER_MODES
            else None,
            "active_orders": len(bot.store.active_paper_orders("BTC")),
            "window": {"start_at": start_at, "end_at": end_at},
            "review_score": _experiment_review_score(trade_summary, order_summary, last_error, official_broadcast_error),
            "order_summary": order_summary,
            "metrics": metrics,
            "recent_trades_summary": trade_summary,
            "single_reversal_summary": bot.store.trade_reason_summary(
                SINGLE_REVERSAL_MARKER,
                "BTC",
                start_at,
                end_at,
            ),
            "single_stop_and_flip_summary": bot.store.trade_reason_summary(
                SINGLE_STOP_AND_FLIP_MARKER,
                "BTC",
                start_at,
                end_at,
            ),
            "single_aggressive_edge_summary": bot.store.trade_reason_summary(
                SINGLE_AGGRESSIVE_EDGE_MARKER,
                "BTC",
                start_at,
                end_at,
            ),
            "aggressive_edge_v2_shadow_summary": bot.store.aggressive_edge_v2_shadow_summary("BTC")
            if variant.signal_filter_mode in {
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
            }
            else None,
            "aggressive_edge_v3_memory_summary": aggressive_edge_v3_memory_summary(
                source_db_paths=bot._aggressive_edge_v3_source_db_paths(),
                loss_replay_paths=bot._aggressive_edge_v3_loss_replay_paths(),
                symbol="BTC",
            )
            if variant.signal_filter_mode in {
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
            }
            else None,
        }

    def apply_official_resolution(
        self,
        round_id: str,
        outcome: str,
        now: float,
        *,
        final_price: float | None = None,
        target_price: float | None = None,
    ) -> None:
        normalized_round_id = str(round_id or "").strip()
        if not normalized_round_id or outcome not in {"Up", "Down"}:
            return
        with self._lock:
            variants = tuple(self.variants)
            self.official_broadcast_count += 1
            self.last_official_broadcast_at = now
        for variant in variants:
            bot = self._bots[variant.variant_id]
            try:
                bot.store.reconcile_round_official_outcome(
                    normalized_round_id,
                    outcome,
                    now,
                    final_price=final_price,
                    target_price=target_price,
                )
                bot.store.settle_round_outcome(
                    normalized_round_id,
                    outcome,
                    now,
                    final_price=final_price,
                    target_price=target_price,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )
                bot._finalize_aggressive_edge_loss_replay(
                    normalized_round_id,
                    outcome,
                    now,
                    final_price,
                    target_price,
                )
                with self._lock:
                    self._official_broadcast_errors[variant.variant_id] = None
            except Exception as exc:  # noqa: BLE001 - one experiment store must not block official broadcast to others.
                with self._lock:
                    self._official_broadcast_errors[variant.variant_id] = f"{type(exc).__name__}: {exc}"

    def _settle_pending_official_rounds(self, variants: tuple[StrategyVariant, ...], now: float) -> None:
        candidates = self._pending_official_round_ids(variants, now)
        checked = 0
        for round_id in candidates:
            with self._lock:
                next_at = self._official_settlement_next_at.get(round_id, 0.0)
            if next_at > now:
                continue
            if checked >= OFFICIAL_RECHECK_LIMIT:
                break
            checked += 1
            try:
                resolution = self.polymarket.get_resolution(round_id)
                outcome = resolution.get("outcome") if isinstance(resolution, dict) else None
                if outcome in {"Up", "Down"}:
                    final_price = _maybe_float(resolution.get("final_price"))
                    target_price = _maybe_float(resolution.get("target_price"))
                    self.apply_official_resolution(
                        round_id,
                        str(outcome),
                        now,
                        final_price=final_price,
                        target_price=target_price,
                    )
                    with self._lock:
                        self._official_settlement_next_at.pop(round_id, None)
                else:
                    with self._lock:
                        self._official_settlement_next_at[round_id] = now + OFFICIAL_RECHECK_INTERVAL_SECONDS
            except Exception:  # noqa: BLE001 - official settlement lag must not stop experiment ticks.
                with self._lock:
                    self._official_settlement_next_at[round_id] = now + OFFICIAL_RECHECK_INTERVAL_SECONDS

    def _pending_official_round_ids(self, variants: tuple[StrategyVariant, ...], now: float) -> list[str]:
        rounds: dict[str, float] = {}
        for variant in variants:
            bot = self._bots[variant.variant_id]
            try:
                rows = bot.store.open_trades()
            except Exception as exc:  # noqa: BLE001 - keep other experiment stores progressing.
                with self._lock:
                    self._errors[variant.variant_id] = f"{type(exc).__name__}: {exc}"
                continue
            for row in rows:
                if row.get("symbol") != "BTC" or not _is_pending_settlement_trade(row, now):
                    continue
                round_id = str(row.get("round_id") or "").strip()
                if not round_id:
                    continue
                rounds.setdefault(round_id, _maybe_float(row.get("ends_at")) or 0.0)
            try:
                # 诊断组合不会产生真实持仓，只能靠影子样本候选触发官方结果补偿。
                shadow_rows = bot.store.pending_aggressive_edge_shadow_official_rounds(
                    now,
                    OFFICIAL_RECHECK_WINDOW_SECONDS,
                    OFFICIAL_RECHECK_LIMIT,
                    "BTC",
                )
            except Exception as exc:  # noqa: BLE001 - 单个实验库损坏不能影响其它组合。
                with self._lock:
                    self._errors[variant.variant_id] = f"{type(exc).__name__}: {exc}"
                continue
            for row in shadow_rows:
                round_id = str(row.get("round_id") or "").strip()
                if not round_id:
                    continue
                rounds.setdefault(round_id, _maybe_float(row.get("ends_at")) or 0.0)
                logger.debug(
                    "实验影子样本等待官方结算 variant=%s round_id=%s unsettled_shadow_count=%s",
                    variant.variant_id,
                    round_id,
                    row.get("unsettled_shadow_count"),
                )
        return [
            round_id
            for round_id, _ends_at in sorted(
                rounds.items(),
                key=lambda item: (item[1], item[0]),
            )
        ]

    @staticmethod
    def _settle_due_from_price(store: TradeStore, price: dict[str, Any], now: float) -> None:
        chainlink_price = _maybe_float(price.get("chainlink"))
        if chainlink_price:
            store.settle_due_rounds({"BTC": chainlink_price}, now)

    @staticmethod
    def _save_price_tick(store: TradeStore, price: dict[str, Any], now: float) -> None:
        chainlink_price = _maybe_float(price.get("chainlink"))
        binance_price = _maybe_float(price.get("binance"))
        if chainlink_price:
            store.save_price_tick(
                "BTC",
                chainlink_price,
                "strategy-experiment-chainlink",
                _updated_at_seconds(price.get("chainlink_updated_ms"), now),
            )
        elif binance_price:
            store.save_price_tick("BTC", binance_price, "strategy-experiment-binance", now)


def _copy_quotes(quotes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    for side, row in quotes.items():
        if not isinstance(row, dict):
            continue
        item = dict(row)
        for key in ("asks", "bids"):
            levels = row.get(key)
            if isinstance(levels, list):
                item[key] = [dict(level) for level in levels if isinstance(level, dict)]
        copied[str(side)] = item
    return copied


def _paper_cancel_summary(result: dict[str, Any]) -> dict[str, Any]:
    canceled = result.get("canceled") if isinstance(result.get("canceled"), list) else []
    return {
        "canceled_count": len(canceled),
        "canceled": [int(item) for item in canceled],
        "released_cash": _round_money(_maybe_float(result.get("released_cash")) or 0.0) or 0.0,
    }


def _disabled_live_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "variant_id": LIVE_VARIANT_ID,
        "combo": LIVE_COMBO,
        "readiness": {"ready": False, "errors": ["live trading disabled in this runtime"]},
        "open_orders": _disabled_live_open_orders()["open_orders"],
    }


def _live_settings_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    settings = dict(snapshot.get("settings") if isinstance(snapshot.get("settings"), dict) else {})
    settings.update(
        {
            "variant_id": snapshot.get("variant_id") or LIVE_VARIANT_ID,
            "combo": snapshot.get("combo") or LIVE_COMBO,
            "live_strategy_id": snapshot.get("live_strategy_id") or settings.get("live_strategy_id") or LIVE_VARIANT_ID,
            "live_strategy": snapshot.get("live_strategy"),
            "live_strategy_options": snapshot.get("live_strategy_options"),
            "db_path": snapshot.get("db_path"),
            "process_lock_path": snapshot.get("process_lock_path"),
            "process_lock_acquired": snapshot.get("process_lock_acquired"),
            "process_lock": snapshot.get("process_lock"),
            "startup_rearmed": snapshot.get("startup_rearmed"),
            "startup_rearm_skipped_active_lock": snapshot.get("startup_rearm_skipped_active_lock"),
            "settings_file": snapshot.get("settings_file"),
            "readiness": snapshot.get("readiness"),
            "open_orders": snapshot.get("open_orders"),
        }
    )
    settings["enabled"] = bool(settings.get("enabled"))
    return settings


def _disabled_live_snapshot() -> dict[str, Any]:
    return {
        "enabled": False,
        "variant_id": LIVE_VARIANT_ID,
        "combo": LIVE_COMBO,
        "execution_mode": "LIVE",
        "last_signal": {},
        "last_error": "live trading disabled in this runtime",
        "open_trades": [],
        "readiness": {"ready": False, "errors": ["live trading disabled in this runtime"]},
        "open_orders": _disabled_live_open_orders()["open_orders"],
        "settings": {"enabled": False},
        "variants": [],
    }


def _disabled_live_paper_snapshot(
    *,
    variant_id: str = LIVE_PAPER_VARIANT_ID,
    combo: str = LIVE_PAPER_COMBO,
) -> dict[str, Any]:
    return {
        "enabled": False,
        "variant_id": variant_id,
        "combo": combo,
        "execution_mode": "PAPER",
        "account_scope": "live_paper",
        "mirrors_variant_id": LIVE_VARIANT_ID,
        "last_signal": {},
        "last_error": "live paper trading disabled in this runtime",
        "open_trades": [],
        "settings": {"enabled": False},
        "variants": [],
    }


def _disabled_live_open_orders() -> dict[str, Any]:
    return {
        "execution_mode": "LIVE",
        "variant_id": LIVE_VARIANT_ID,
        "combo": LIVE_COMBO,
        "open_orders": {
            "ready": False,
            "skipped": True,
            "errors": ["live trading disabled in this runtime"],
            "orders": [],
            "count": 0,
            "checked_at": time.time(),
        },
    }


def _disabled_live_evidence(external_order_id: str | None = None) -> dict[str, Any]:
    return {
        "checked_at": time.time(),
        "execution_mode": "LIVE",
        "variant_id": LIVE_VARIANT_ID,
        "combo": LIVE_COMBO,
        "enabled": False,
        "last_signal": {},
        "last_error": "live trading disabled in this runtime",
        "settings": {"enabled": False},
        "software_account": {},
        "readiness": {"ready": False, "errors": ["live trading disabled in this runtime"]},
        "wallet": None,
        "official_open_orders": _disabled_live_open_orders()["open_orders"],
        "open_trades": [],
        "pending_orders": [],
        "recent_orders": [],
        "recent_orders_meta": {"limit": 20, "offset": 0, "loaded": 0, "total": 0, "has_more": False, "status_filter": "all"},
        "recent_trades": [],
        "recent_trades_summary": {},
        "recent_trades_meta": {"limit": 20, "offset": 0, "loaded": 0, "total": 0, "has_more": False},
        "order": None,
        "requested_external_order_id": str(external_order_id or "").strip() or None,
    }


def _append_reason_text(existing: str, addition: str) -> str:
    existing_text = str(existing or "").strip()
    addition_text = str(addition or "").strip()
    if existing_text and addition_text:
        return f"{existing_text} | {addition_text}"
    return existing_text or addition_text


def _llm_super_agent_reason(decision: Any) -> str:
    codes = ",".join(str(item) for item in getattr(decision, "reason_codes", ())[:6])
    code_text = f", codes {codes}" if codes else ""
    error_text = f", error {decision.error}" if getattr(decision, "error", None) else ""
    return (
        f"LLM_SUPER_AGENT route {decision.route}, source {decision.source}, "
        f"regime {decision.market_regime}, conf {decision.confidence:.4f}, "
        f"allow {bool(decision.allow_trade)}{code_text}{error_text}, reason {decision.reason}"
    )


def _median(values: list[float]) -> float:
    cleaned = sorted(value for value in values if isinstance(value, (int, float)))
    if not cleaned:
        return 0.0
    middle = len(cleaned) // 2
    if len(cleaned) % 2:
        return float(cleaned[middle])
    return float((cleaned[middle - 1] + cleaned[middle]) / 2.0)


def _multi_source_price_for_basis(price: dict[str, Any], source: str) -> float | None:
    if source == "binance":
        return _maybe_float(price.get("binance_market")) or _maybe_float(price.get("binance"))
    return _maybe_float(price.get(source))


def _multi_source_updated_ms_for_basis(price: dict[str, Any], source: str) -> int | None:
    if source == "binance":
        return _maybe_int(price.get("binance_market_updated_ms")) or _maybe_int(price.get("binance_updated_ms"))
    return _maybe_int(price.get(f"{source}_updated_ms"))


def _multi_source_sample_updated_ms_for_basis(price: dict[str, Any], source: str) -> int | None:
    if source == "binance":
        return (
            _maybe_int(price.get("binance_market_exchange_updated_ms"))
            or _maybe_int(price.get("binance_market_updated_ms"))
            or _maybe_int(price.get("binance_updated_ms"))
        )
    return _maybe_int(price.get(f"{source}_exchange_updated_ms")) or _maybe_int(price.get(f"{source}_updated_ms"))


def _side_list_text(sides: Any) -> str:
    values = sorted({str(side) for side in sides if str(side or "") in {"Up", "Down"}})
    return ",".join(values) if values else "-"


def _variant_tags(variant: StrategyVariant) -> dict[str, Any]:
    return {
        "variant_id": variant.variant_id,
        "combo": variant.combo,
        "strategy_family": variant.strategy_family,
        "experiment_order_type": variant.order_type,
        "single_entry_mode": variant.single_entry_mode,
        "signal_side_mode": variant.signal_side_mode,
        "signal_filter_mode": variant.signal_filter_mode,
        "market_data_mode": variant.market_data_mode,
        "price_source_mode": variant.price_source_mode,
        "anti_bot_guard_mode": variant.anti_bot_guard_mode,
        "account_scope": "strategy_experiment",
    }


def _tag_variant_rows(rows: list[dict[str, Any]], variant_tags: dict[str, Any]) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(variant_tags)
        tagged.append(item)
    return tagged


def _merge_trade_summaries(
    summaries: list[dict[str, Any]],
    start_at: float | None = None,
    end_at: float | None = None,
) -> dict[str, Any]:
    numeric_int_keys = (
        "total_count",
        "settled_count",
        "open_count",
        "win_count",
        "loss_count",
        "breakeven_count",
        "official_count",
        "chainlink_count",
        "early_exit_count",
        "unknown_source_count",
    )
    numeric_float_keys = ("total_stake", "settled_stake", "open_risk", "total_payout", "total_pnl")
    merged: dict[str, Any] = {"start_at": start_at, "end_at": end_at}
    for key in numeric_int_keys:
        merged[key] = int(sum(int(summary.get(key) or 0) for summary in summaries))
    for key in numeric_float_keys:
        merged[key] = _round_money(sum(float(summary.get(key) or 0.0) for summary in summaries)) or 0.0
    pnl_values = [_maybe_float(summary.get("max_win")) for summary in summaries if _maybe_float(summary.get("max_win")) is not None]
    loss_values = [_maybe_float(summary.get("max_loss")) for summary in summaries if _maybe_float(summary.get("max_loss")) is not None]
    merged["max_win"] = _round_money(max(pnl_values)) if pnl_values else None
    merged["max_loss"] = _round_money(min(loss_values)) if loss_values else None
    settled_count = int(merged["settled_count"] or 0)
    settled_stake = _maybe_float(merged["settled_stake"]) or 0.0
    total_pnl = _maybe_float(merged["total_pnl"]) or 0.0
    win_count = int(merged["win_count"] or 0)
    merged["avg_pnl"] = _round_money(total_pnl / settled_count) if settled_count else None
    merged["roi_pct"] = round(total_pnl / settled_stake * 100.0, 4) if settled_stake else None
    merged["win_rate"] = round(win_count / settled_count * 100.0, 4) if settled_count else None
    return merged


def _experiment_review_score(
    trade_summary: dict[str, Any],
    order_summary: dict[str, Any],
    last_error: str | None,
    official_broadcast_error: str | None,
) -> dict[str, Any]:
    settled = int(trade_summary.get("settled_count") or 0)
    orders = int(order_summary.get("total_count") or 0)
    official = int(trade_summary.get("official_count") or 0)
    chainlink = int(trade_summary.get("chainlink_count") or 0)
    rejected = int(order_summary.get("rejected_count") or 0)
    expired = int(order_summary.get("expired_count") or 0)
    canceled = int(order_summary.get("canceled_count") or 0)
    fill_attempts = int(order_summary.get("fill_attempt_count") or 0)
    roi_pct = _maybe_float(trade_summary.get("roi_pct"))
    win_rate = _maybe_float(trade_summary.get("win_rate"))
    fill_rate = _maybe_float(order_summary.get("fill_rate"))
    pnl = _maybe_float(trade_summary.get("total_pnl")) or 0.0

    roi_score = _scale_score(roi_pct, -15.0, 20.0, 25.0)
    pnl_score = _scale_score(pnl, -10.0, 20.0, 15.0)
    win_score = _scale_score(win_rate, 40.0, 65.0, 15.0)
    fill_score = _scale_score(fill_rate, 0.0, 90.0, 18.0)
    official_ratio = official / settled * 100.0 if settled else None
    official_score = _scale_score(official_ratio, 0.0, 90.0, 12.0)
    sample_score = min(15.0, settled / 30.0 * 9.0 + orders / 60.0 * 6.0)
    bad_order_ratio = (rejected + expired + canceled) / orders * 100.0 if orders else 0.0
    bad_order_penalty = min(12.0, bad_order_ratio / 100.0 * 18.0)
    error_penalty = 20.0 if last_error or official_broadcast_error else 0.0
    score = max(
        0.0,
        min(
            100.0,
            roi_score + pnl_score + win_score + fill_score + official_score + sample_score - bad_order_penalty - error_penalty,
        ),
    )

    reasons: list[str] = []
    if settled < 30:
        reasons.append(f"结算样本不足 {settled}/30")
    if orders < 60:
        reasons.append(f"订单样本不足 {orders}/60")
    if fill_rate is not None and fill_rate < 35:
        reasons.append(f"成交率偏低 {fill_rate:.2f}%")
    if official_ratio is not None and official_ratio < 50:
        reasons.append(f"官方结算占比偏低 {official_ratio:.2f}%")
    if bad_order_ratio >= 30:
        reasons.append(f"取消/过期/拒绝偏高 {bad_order_ratio:.2f}%")
    if chainlink > 0:
        reasons.append(f"仍有 {chainlink} 笔兜底结算")
    if last_error:
        reasons.append("组合运行异常")
    if official_broadcast_error:
        reasons.append("官方广播异常")

    execution_disqualified = (orders >= 60 and fill_attempts == 0) or (
        orders >= 100 and (fill_rate is not None and fill_rate < 5.0) and settled < 5
    )
    if execution_disqualified:
        reasons.append("长期低成交，暂不纳入决胜")
        sample_status = "DISQUALIFIED"
        sample_label = "执行淘汰"
    elif settled < 10 or orders < 10:
        sample_status = "INSUFFICIENT"
        sample_label = "样本不足"
    elif settled < 30 or orders < 60:
        sample_status = "WARMING_UP"
        sample_label = "观察中"
    else:
        sample_status = "USABLE"
        sample_label = "可比较"

    if execution_disqualified:
        decision = "执行不可用"
        score = min(score, 25.0)
    elif sample_status != "USABLE":
        decision = "继续观察"
    elif score >= 75:
        decision = "优先候选"
    elif score >= 60:
        decision = "候选"
    elif score >= 40:
        decision = "谨慎观察"
    else:
        decision = "暂不优先"
    if not reasons:
        reasons.append("样本和执行质量暂未触发扣分提示")

    return {
        "score": round(score, 2),
        "decision": decision,
        "sample_status": sample_status,
        "sample_label": sample_label,
        "eligible_for_decision": sample_status == "USABLE",
        "disqualified": execution_disqualified,
        "disqualification_reason": "长期低成交" if execution_disqualified else None,
        "settled_required": 30,
        "orders_required": 60,
        "official_ratio": round(official_ratio, 4) if official_ratio is not None else None,
        "bad_order_ratio": round(bad_order_ratio, 4),
        "components": {
            "roi": round(roi_score, 4),
            "pnl": round(pnl_score, 4),
            "win_rate": round(win_score, 4),
            "execution": round(fill_score, 4),
            "settlement": round(official_score, 4),
            "sample": round(sample_score, 4),
            "bad_order_penalty": round(bad_order_penalty, 4),
            "error_penalty": round(error_penalty, 4),
        },
        "reasons": reasons,
    }


def _experiment_decision_summary(variants: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(variants)
    ranked = sorted(
        variants,
        key=lambda row: (
            _maybe_float((row.get("review_score") or {}).get("score")) or 0.0,
            _maybe_float((row.get("recent_trades_summary") or {}).get("total_pnl")) or 0.0,
        ),
        reverse=True,
    )
    current = ranked[0] if ranked else None
    eligible = [row for row in ranked if (row.get("review_score") or {}).get("eligible_for_decision")]
    disqualified = [row for row in ranked if (row.get("review_score") or {}).get("disqualified")]
    pending = [
        row
        for row in ranked
        if not (row.get("review_score") or {}).get("eligible_for_decision")
        and not (row.get("review_score") or {}).get("disqualified")
    ]
    ready_count = len(eligible)
    disqualified_count = len(disqualified)
    pending_count = len(pending)
    comparison_ready = total > 0 and pending_count == 0 and ready_count > 0
    recommended = eligible[0] if comparison_ready and eligible else None
    missing = [
        {
            "variant_id": row.get("variant_id"),
            "combo": row.get("combo"),
            "sample_status": (row.get("review_score") or {}).get("sample_status"),
            "sample_label": (row.get("review_score") or {}).get("sample_label"),
            "settled_count": (row.get("recent_trades_summary") or {}).get("settled_count"),
            "order_count": (row.get("order_summary") or {}).get("total_count"),
        }
        for row in pending
    ]
    disqualified_items = [
        {
            "variant_id": row.get("variant_id"),
            "combo": row.get("combo"),
            "reason": (row.get("review_score") or {}).get("disqualification_reason"),
            "score": (row.get("review_score") or {}).get("score"),
            "settled_count": (row.get("recent_trades_summary") or {}).get("settled_count"),
            "order_count": (row.get("order_summary") or {}).get("total_count"),
            "fill_rate": (row.get("order_summary") or {}).get("fill_rate"),
        }
        for row in disqualified
    ]

    if not total:
        status = "NO_DATA"
        status_label = "暂无数据"
        reason = "没有可比较的策略组合"
    elif comparison_ready:
        status = "READY"
        status_label = "可决胜"
        reason = "所有未淘汰组合达到样本阈值，可以按评分选择优先候选"
    elif ready_count == 0 and pending_count == 0:
        status = "NO_ELIGIBLE"
        status_label = "无候选"
        reason = "所有组合均因执行质量或异常被排除，不能给出盈利候选"
    else:
        status = "WAITING_FOR_SAMPLE"
        status_label = "继续观察"
        reason = f"还有 {pending_count} 个组合未达到样本阈值，{disqualified_count} 个组合已淘汰"

    return {
        "status": status,
        "status_label": status_label,
        "comparison_ready": comparison_ready,
        "ready_count": ready_count,
        "pending_count": pending_count,
        "disqualified_count": disqualified_count,
        "total_count": total,
        "reason": reason,
        "current_leader_variant_id": current.get("variant_id") if current else None,
        "current_leader_combo": current.get("combo") if current else None,
        "current_leader_score": (current.get("review_score") or {}).get("score") if current else None,
        "recommended_variant_id": recommended.get("variant_id") if recommended else None,
        "recommended_combo": recommended.get("combo") if recommended else None,
        "recommended_score": (recommended.get("review_score") or {}).get("score") if recommended else None,
        "missing_sample_variants": missing,
        "disqualified_variants": disqualified_items,
    }


def _experiment_profit_summary(variants: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(variants)
    ranked = sorted(
        variants,
        key=lambda row: (
            _maybe_float((row.get("recent_trades_summary") or {}).get("total_pnl")) or 0.0,
            _maybe_float((row.get("recent_trades_summary") or {}).get("roi_pct")) or -999999.0,
            int((row.get("recent_trades_summary") or {}).get("settled_count") or 0),
            _maybe_float((row.get("review_score") or {}).get("score")) or 0.0,
        ),
        reverse=True,
    )
    with_settled = [row for row in ranked if int((row.get("recent_trades_summary") or {}).get("settled_count") or 0) > 0]
    eligible = [row for row in ranked if (row.get("review_score") or {}).get("eligible_for_decision")]
    disqualified = [row for row in ranked if (row.get("review_score") or {}).get("disqualified")]
    pending = [
        row
        for row in ranked
        if not (row.get("review_score") or {}).get("eligible_for_decision")
        and not (row.get("review_score") or {}).get("disqualified")
    ]
    current = with_settled[0] if with_settled else None
    sample_ready = total > 0 and not pending and bool(eligible)
    best_eligible = eligible[0] if sample_ready else None
    best_eligible_pnl = _maybe_float((best_eligible.get("recent_trades_summary") or {}).get("total_pnl")) if best_eligible else None
    winner = best_eligible if best_eligible_pnl is not None and best_eligible_pnl > 0 else None
    if not total:
        status = "NO_DATA"
        status_label = "暂无数据"
        reason = "没有可复盘的策略组合"
    elif winner:
        status = "READY"
        status_label = "盈利可决胜"
        reason = "所有未淘汰组合达到样本阈值，已按净盈亏优先、ROI 次优先给出盈利胜出组合"
    elif sample_ready:
        status = "NO_PROFIT"
        status_label = "暂无盈利胜出"
        reason = "样本已可比较，但最高净盈亏未大于 0，暂不应切换主策略"
    elif not eligible and not pending:
        status = "NO_ELIGIBLE"
        status_label = "无盈利候选"
        reason = "所有组合均因执行质量或异常被排除，不能给出盈利胜出组合"
    else:
        status = "WAITING_FOR_SAMPLE"
        status_label = "等待盈利样本"
        reason = f"还有 {len(pending)} 个组合未达到样本阈值，当前盈利领先只作为观察信号"

    return {
        "status": status,
        "status_label": status_label,
        "comparison_ready": sample_ready,
        "profitable_winner_ready": bool(winner),
        "ready_count": len(eligible),
        "pending_count": len(pending),
        "disqualified_count": len(disqualified),
        "total_count": total,
        "reason": reason,
        "current_profit_leader_variant_id": current.get("variant_id") if current else None,
        "current_profit_leader_combo": current.get("combo") if current else None,
        "current_profit_leader_pnl": (current.get("recent_trades_summary") or {}).get("total_pnl") if current else None,
        "current_profit_leader_roi_pct": (current.get("recent_trades_summary") or {}).get("roi_pct") if current else None,
        "best_eligible_variant_id": best_eligible.get("variant_id") if best_eligible else None,
        "best_eligible_combo": best_eligible.get("combo") if best_eligible else None,
        "best_eligible_pnl": (best_eligible.get("recent_trades_summary") or {}).get("total_pnl") if best_eligible else None,
        "best_eligible_roi_pct": (best_eligible.get("recent_trades_summary") or {}).get("roi_pct") if best_eligible else None,
        "winner_variant_id": winner.get("variant_id") if winner else None,
        "winner_combo": winner.get("combo") if winner else None,
        "winner_pnl": (winner.get("recent_trades_summary") or {}).get("total_pnl") if winner else None,
        "winner_roi_pct": (winner.get("recent_trades_summary") or {}).get("roi_pct") if winner else None,
        "rankings": [
            {
                "rank": index + 1,
                "variant_id": row.get("variant_id"),
                "combo": row.get("combo"),
                "sample_status": (row.get("review_score") or {}).get("sample_status"),
                "sample_label": (row.get("review_score") or {}).get("sample_label"),
                "eligible_for_decision": (row.get("review_score") or {}).get("eligible_for_decision"),
                "disqualified": (row.get("review_score") or {}).get("disqualified"),
                "total_pnl": (row.get("recent_trades_summary") or {}).get("total_pnl"),
                "roi_pct": (row.get("recent_trades_summary") or {}).get("roi_pct"),
                "settled_count": (row.get("recent_trades_summary") or {}).get("settled_count"),
                "win_rate": (row.get("recent_trades_summary") or {}).get("win_rate"),
                "fill_rate": (row.get("order_summary") or {}).get("fill_rate"),
                "review_score": (row.get("review_score") or {}).get("score"),
            }
            for index, row in enumerate(ranked)
        ],
    }


def _scale_score(value: float | None, floor: float, ceiling: float, weight: float) -> float:
    if value is None or ceiling <= floor or weight <= 0:
        return 0.0
    normalized = (float(value) - floor) / (ceiling - floor)
    return max(0.0, min(1.0, normalized)) * weight


def _post_only_queue_fill_ratio(age_seconds: float) -> float:
    if age_seconds <= 0:
        return POST_ONLY_QUEUE_INITIAL_FILL_RATIO
    progress = min(1.0, age_seconds / POST_ONLY_QUEUE_FULL_SECONDS)
    ratio = POST_ONLY_QUEUE_INITIAL_FILL_RATIO + (
        POST_ONLY_QUEUE_MAX_FILL_RATIO - POST_ONLY_QUEUE_INITIAL_FILL_RATIO
    ) * progress
    return max(POST_ONLY_QUEUE_INITIAL_FILL_RATIO, min(POST_ONLY_QUEUE_MAX_FILL_RATIO, ratio))


def _clean_quotes(quotes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cleaned: dict[str, dict[str, Any]] = {}
    for side in ("Up", "Down"):
        row = quotes.get(side)
        if not isinstance(row, dict):
            continue
        item = {
            "token_id": str(row.get("token_id") or ""),
            "outcome": side,
            "best_bid": _maybe_float(row.get("best_bid")),
            "best_ask": _maybe_float(row.get("best_ask")),
            "bid_size": _maybe_float(row.get("bid_size")),
            "ask_size": _maybe_float(row.get("ask_size")),
            "bids": _clean_book_levels(row.get("bids"), reverse=True),
            "asks": _clean_book_levels(row.get("asks"), reverse=False),
            "updated_at_ms": _maybe_int(row.get("updated_at_ms")) or int(time.time() * 1000),
            "source": str(row.get("source") or "browser-ws"),
        }
        clob_received_ms = _maybe_int(row.get("clob_received_ms"))
        clob_event_updated_ms = _maybe_int(row.get("clob_event_updated_ms"))
        if clob_received_ms:
            item["clob_received_ms"] = clob_received_ms
        if clob_event_updated_ms:
            item["clob_event_updated_ms"] = clob_event_updated_ms
        cleaned[side] = item
    return cleaned


def _clean_book_levels(levels: Any, *, reverse: bool) -> list[dict[str, float]]:
    if not isinstance(levels, list):
        return []
    cleaned: list[dict[str, float]] = []
    for row in levels:
        if not isinstance(row, dict):
            continue
        price = _maybe_float(row.get("price"))
        size = _maybe_float(row.get("size"))
        if price is None or size is None or price <= 0 or price >= 1 or size <= 0:
            continue
        cleaned.append({"price": round(price, 4), "size": round(size, 6)})
    return sorted(cleaned, key=lambda item: item["price"], reverse=reverse)[:50]


def _merge_quote_depth(current: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for side, row in current.items():
        item = dict(row)
        previous_row = previous.get(side) if isinstance(previous.get(side), dict) else {}
        same_token = not item.get("token_id") or not previous_row.get("token_id") or item.get("token_id") == previous_row.get("token_id")
        if same_token and not item.get("asks") and _book_head_matches(previous_row.get("asks"), item.get("best_ask")):
            item["asks"] = previous_row.get("asks") or []
            if item.get("ask_size") is None and item["asks"]:
                item["ask_size"] = item["asks"][0].get("size")
        if same_token and not item.get("bids") and _book_head_matches(previous_row.get("bids"), item.get("best_bid")):
            item["bids"] = previous_row.get("bids") or []
            if item.get("bid_size") is None and item["bids"]:
                item["bid_size"] = item["bids"][0].get("size")
        merged[side] = item
    return merged


def _book_head_matches(levels: Any, price: Any) -> bool:
    if not isinstance(levels, list) or not levels:
        return False
    current_price = _maybe_float(price)
    head_price = _maybe_float(levels[0].get("price")) if isinstance(levels[0], dict) else None
    return current_price is not None and head_price is not None and abs(current_price - head_price) <= 0.000001


def _pair_quote_state(quotes: dict[str, dict[str, Any]], now: float) -> dict[str, Any]:
    now_ms = int(now * 1000)
    up = quotes.get("Up") if isinstance(quotes.get("Up"), dict) else {}
    down = quotes.get("Down") if isinstance(quotes.get("Down"), dict) else {}
    up_ask = _maybe_float(up.get("best_ask"))
    down_ask = _maybe_float(down.get("best_ask"))
    up_bid = _maybe_float(up.get("best_bid"))
    down_bid = _maybe_float(down.get("best_bid"))
    updated_ms = max(_maybe_int(up.get("updated_at_ms")) or 0, _maybe_int(down.get("updated_at_ms")) or 0)
    return {
        "up_ask": up_ask,
        "down_ask": down_ask,
        "up_bid": up_bid,
        "down_bid": down_bid,
        "up_ask_size": _maybe_float(up.get("ask_size")),
        "down_ask_size": _maybe_float(down.get("ask_size")),
        "pair_cost": round(up_ask + down_ask, 6) if up_ask is not None and down_ask is not None else None,
        "bid_sum": round(up_bid + down_bid, 6) if up_bid is not None and down_bid is not None else None,
        "quote_age_ms": (now_ms - updated_ms) if updated_ms else None,
    }


def _position_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    summary = {
        "Up": {"shares": 0.0, "stake": 0.0},
        "Down": {"shares": 0.0, "stake": 0.0},
    }
    for row in rows:
        side = str(row.get("side") or "")
        if side not in summary:
            continue
        summary[side]["shares"] = round(summary[side]["shares"] + (_maybe_float(row.get("shares")) or 0.0), 6)
        summary[side]["stake"] = round(summary[side]["stake"] + (_maybe_float(row.get("stake")) or 0.0), 6)
    return summary


def _residual_inventory(rows: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any] | None:
    summary = _position_summary(rows)
    up_shares = summary["Up"]["shares"]
    down_shares = summary["Down"]["shares"]
    diff = round(up_shares - down_shares, 6)
    if abs(diff) <= PAIR_EPSILON:
        return None
    side = "Up" if diff > 0 else "Down"
    residual_shares = abs(diff)
    side_shares = summary[side]["shares"]
    side_stake = summary[side]["stake"]
    if side_shares <= 0:
        return None
    residual_stake = round(side_stake * residual_shares / side_shares, 6)
    bid = _maybe_float(state.get("up_bid" if side == "Up" else "down_bid"))
    exit_value = round(residual_shares * bid, 6) if bid is not None else None
    roi_pct = None
    if exit_value is not None and residual_stake > 0:
        roi_pct = round((exit_value - residual_stake) / residual_stake * 100.0, 4)
    return {
        "side": side,
        "shares": residual_shares,
        "stake": residual_stake,
        "bid": bid,
        "exit_value": exit_value,
        "roi_pct": roi_pct,
    }


def _price_confirms_residual(
    side: str,
    price: dict[str, Any],
    target_price: float,
    market_data_mode: str = MARKET_DATA_MODE_BASE,
) -> bool:
    chainlink = _maybe_float(price.get("chainlink"))
    if chainlink is None or target_price <= 0:
        return False
    if market_data_mode in {MARKET_DATA_MODE_MULTI_CONFIRM, MARKET_DATA_MODE_MULTI_LEAD}:
        multi_block = _pair_multi_entry_block_reason(market_data_mode, price)
        if multi_block:
            return False
        if _pair_multi_directional_blocked(side, price):
            return False
    if side == "Up":
        return chainlink >= target_price
    if side == "Down":
        return chainlink <= target_price
    return False


def _pair_multi_entry_block_reason(market_data_mode: str, price: dict[str, Any]) -> str | None:
    if market_data_mode not in {MARKET_DATA_MODE_MULTI_CONFIRM, MARKET_DATA_MODE_MULTI_LEAD}:
        return None
    context = price.get("multi_context") if isinstance(price.get("multi_context"), dict) else {}
    if not context.get("ready"):
        return f"{PAIR_MULTI_READY_MARKER} 等待 OKX/Binance 基差样本，配对实验暂不开仓"
    return None


def _pair_multi_note(market_data_mode: str, price: dict[str, Any]) -> str:
    if market_data_mode not in {MARKET_DATA_MODE_MULTI_CONFIRM, MARKET_DATA_MODE_MULTI_LEAD}:
        return ""
    rows = _pair_multi_ready_rows(price)
    if not rows:
        return ""
    detail = ", ".join(f"{name}:{value:+.2f}bps" for name, value in rows)
    return f"{PAIR_MULTI_READY_MARKER} {market_data_mode} OKX/Binance 残差采样 {detail}"


def _pair_multi_directional_blocked(side: str, price: dict[str, Any]) -> bool:
    rows = _pair_multi_ready_rows(price)
    if not rows:
        return True
    for _, residual in rows:
        directional = residual if side == "Up" else -residual
        if directional < -1.5:
            return True
    return False


def _pair_multi_ready_rows(price: dict[str, Any]) -> list[tuple[str, float]]:
    context = price.get("multi_context") if isinstance(price.get("multi_context"), dict) else {}
    sources = context.get("sources") if isinstance(context.get("sources"), dict) else {}
    rows: list[tuple[str, float]] = []
    for source in MULTI_SOURCE_KEYS:
        item = sources.get(source)
        if not isinstance(item, dict) or not item.get("ready"):
            continue
        residual = _maybe_float(item.get("residual_bps"))
        if residual is None:
            continue
        rows.append((source, residual))
    return rows


def _realtime_maker_state(
    market: MarketRound,
    price: dict[str, Any],
    quotes: dict[str, dict[str, Any]],
    now: float,
    max_quote_age_ms: int,
) -> dict[str, Any]:
    fair_up = _realtime_maker_fair_up(market, price, quotes, now)
    actor_up = _realtime_maker_actor_up(price, now)
    adjusted_up = fair_up
    if adjusted_up is not None and actor_up is not None:
        adjusted_up = max(0.01, min(0.99, adjusted_up * 0.85 + actor_up * 0.15))
    side = None
    side_fair = None
    actor_side_fair = None
    if adjusted_up is not None:
        side = "Up" if adjusted_up >= 0.5 else "Down"
        side_fair = adjusted_up if side == "Up" else 1.0 - adjusted_up
        if actor_up is not None:
            actor_side_fair = actor_up if side == "Up" else 1.0 - actor_up
    quote = quotes.get(side) if side and isinstance(quotes.get(side), dict) else {}
    best_bid = _maybe_float(quote.get("best_bid"))
    best_ask = _maybe_float(quote.get("best_ask"))
    limit_price = _realtime_maker_limit_price(side_fair, best_bid, best_ask)
    quote_age_ms = _realtime_maker_quote_age_ms(quotes, now)
    block_reason = None
    if quote_age_ms is None or quote_age_ms > max_quote_age_ms:
        block_reason = "REALTIME_MAKER_WAIT 盘口报价过期"
    elif fair_up is None:
        block_reason = "REALTIME_MAKER_WAIT 缺少实时 fair value"
    elif side and (best_bid is None or best_ask is None):
        block_reason = f"REALTIME_MAKER_WAIT 缺少 {side} 买一/卖一"
    current_price = (
        _maybe_float(price.get("chainlink"))
        or _maybe_float(price.get("binance_market"))
        or _maybe_float(price.get("binance"))
        or _maybe_float(price.get("okx"))
    )
    distance_bps = (
        (current_price - market.target_price) / market.target_price * 10_000.0
        if current_price is not None and market.target_price > 0
        else None
    )
    return {
        "side": side,
        "fair_up": _round_float(fair_up, 4),
        "raw_fair_up": _round_float(fair_up, 4),
        "adjusted_fair_up": _round_float(adjusted_up, 4),
        "actor_up": _round_float(actor_up, 4),
        "actor_side_fair": _round_float(actor_side_fair, 4),
        "side_fair": _round_float(side_fair, 4),
        "limit_price": limit_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "quote_age_ms": quote_age_ms,
        "current_price": current_price,
        "distance_bps": distance_bps,
        "block_reason": block_reason,
    }


def _realtime_maker_fair_up(
    market: MarketRound,
    price: dict[str, Any],
    quotes: dict[str, dict[str, Any]],
    now: float,
) -> float | None:
    realtime = price.get("realtime_probability") if isinstance(price.get("realtime_probability"), dict) else {}
    fair = _maybe_float(realtime.get("combined_up"))
    if fair is not None:
        return max(0.01, min(0.99, fair))
    market_up = _realtime_maker_market_up(quotes)
    target_up = _realtime_maker_target_up(market, price, now)
    external_up = _realtime_maker_external_up(price)
    return _weighted_probability([(market_up, 0.50), (target_up, 0.40), (external_up, 0.10)])


def _realtime_maker_actor_up(price: dict[str, Any], now: float) -> float | None:
    actor = price.get("actor_probability") if isinstance(price.get("actor_probability"), dict) else {}
    status = str(actor.get("status") or "")
    if status and status not in {"READY", "PARTIAL"}:
        return None
    checked_at = _maybe_float(actor.get("checked_at"))
    if checked_at is not None and now - checked_at > 20.0:
        return None
    value = _maybe_float(actor.get("combined_up"))
    return max(0.01, min(0.99, value)) if value is not None else None


def _realtime_maker_market_up(quotes: dict[str, dict[str, Any]]) -> float | None:
    up_mid = _quote_mid_from_quotes(quotes, "Up")
    if up_mid is not None:
        return up_mid
    down_mid = _quote_mid_from_quotes(quotes, "Down")
    return 1.0 - down_mid if down_mid is not None else None


def _realtime_maker_target_up(market: MarketRound, price: dict[str, Any], now: float) -> float | None:
    current = (
        _maybe_float(price.get("chainlink"))
        or _maybe_float(price.get("binance_market"))
        or _maybe_float(price.get("binance"))
        or _maybe_float(price.get("okx"))
    )
    if current is None or current <= 0 or market.target_price <= 0:
        return None
    seconds_left = max(1.0, market.ends_at - now)
    distance_bps = (current - market.target_price) / market.target_price * 10_000.0
    scale = max(1.0, 12.0 * (seconds_left / 300.0) ** 0.5)
    return _logistic_probability(distance_bps, scale)


def _realtime_maker_external_up(price: dict[str, Any]) -> float | None:
    chainlink = _maybe_float(price.get("chainlink"))
    if chainlink is None or chainlink <= 0:
        return None
    residuals: list[float] = []
    okx = _maybe_float(price.get("okx"))
    binance = _maybe_float(price.get("binance_market")) or _maybe_float(price.get("binance"))
    if okx is not None:
        residuals.append((okx - chainlink) / chainlink * 10_000.0)
    if binance is not None:
        residuals.append((binance - chainlink) / chainlink * 10_000.0)
    if not residuals:
        return None
    return _logistic_probability(sum(residuals) / len(residuals), 6.0)


def _realtime_maker_limit_price(side_fair: float | None, best_bid: float | None, best_ask: float | None) -> float | None:
    if side_fair is None:
        return None
    if best_bid is not None and best_bid > 0:
        candidate = best_bid + REALTIME_MAKER_BID_IMPROVEMENT
    elif best_ask is not None:
        candidate = best_ask - 0.02
    else:
        return None
    candidate = min(candidate, side_fair - REALTIME_MAKER_ENTRY_MIN_EDGE)
    if best_ask is not None and candidate >= best_ask - POST_ONLY_CROSS_BUFFER:
        candidate = best_ask - 0.01
    return round(max(0.01, min(0.99, candidate)), 4)


def _realtime_maker_quote_age_ms(quotes: dict[str, dict[str, Any]], now: float) -> int | None:
    updated = [
        _maybe_int(row.get("updated_at_ms"))
        for side in ("Up", "Down")
        for row in [quotes.get(side) if isinstance(quotes.get(side), dict) else {}]
    ]
    updated = [item for item in updated if item]
    if not updated:
        return None
    return max(0, int(now * 1000) - max(updated))


def _quote_mid_from_quotes(quotes: dict[str, dict[str, Any]], side: str) -> float | None:
    quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
    bid = _maybe_float(quote.get("best_bid"))
    ask = _maybe_float(quote.get("best_ask"))
    if bid is not None and ask is not None:
        return max(0.01, min(0.99, (bid + ask) / 2.0))
    if bid is not None:
        return max(0.01, min(0.99, bid))
    if ask is not None:
        return max(0.01, min(0.99, ask))
    return None


def _realtime_maker_side_fair(state: dict[str, Any], side: str) -> float | None:
    fair_up = _maybe_float(state.get("adjusted_fair_up")) or _maybe_float(state.get("fair_up"))
    if fair_up is None:
        return None
    return fair_up if side == "Up" else 1.0 - fair_up


def _realtime_maker_exit_reason(
    side: str,
    side_fair: float | None,
    entry_price: float,
    bid: float,
    age: float,
    time_left: float,
) -> str | None:
    if side_fair is None or entry_price <= 0:
        return None
    prefix = f"{REALTIME_MAKER_MARKER}_EXIT {side}"
    if time_left <= REALTIME_MAKER_FORCE_EXIT_SECONDS_LEFT:
        return f"{prefix} FORCE_EXIT time_left {time_left:.1f}s bid {bid:.4f}"
    if time_left <= REALTIME_MAKER_REDUCE_SECONDS_LEFT and bid >= entry_price - 0.02:
        return f"{prefix} TIME_REDUCE time_left {time_left:.1f}s bid {bid:.4f} entry {entry_price:.4f}"
    if bid >= entry_price + REALTIME_MAKER_TAKE_PROFIT and side_fair - bid <= REALTIME_MAKER_CANCEL_MIN_EDGE:
        return f"{prefix} TAKE_PROFIT bid {bid:.4f} entry {entry_price:.4f} fair {side_fair:.4f}"
    if age >= REALTIME_MAKER_EDGE_GONE_SECONDS and side_fair <= entry_price + REALTIME_MAKER_EDGE_GONE_BUFFER:
        return f"{prefix} EDGE_GONE fair {side_fair:.4f} entry {entry_price:.4f}"
    if side_fair <= entry_price - REALTIME_MAKER_STOP_FAIR_DRAWDOWN:
        return f"{prefix} FAIR_STOP fair {side_fair:.4f} entry {entry_price:.4f}"
    if bid <= entry_price - REALTIME_MAKER_STOP_BID_DRAWDOWN:
        return f"{prefix} BID_STOP bid {bid:.4f} entry {entry_price:.4f}"
    return None


def _realtime_maker_cancel_reason(
    market: MarketRound,
    state: dict[str, Any],
    side: str,
    side_fair: float | None,
    limit_price: float,
    best_ask: float | None,
    order_age: float,
    now: float,
) -> str | None:
    time_left = market.ends_at - now
    if time_left <= REALTIME_MAKER_REDUCE_SECONDS_LEFT:
        return f"{REALTIME_MAKER_MARKER} reduce window"
    if side_fair is None:
        if order_age < REALTIME_MAKER_CANCEL_GRACE_SECONDS:
            return None
        return f"{REALTIME_MAKER_MARKER} fair unavailable"
    if best_ask is not None and limit_price >= best_ask - POST_ONLY_CROSS_BUFFER:
        return f"{REALTIME_MAKER_MARKER} post-only risk ask {best_ask:.4f}"
    edge = side_fair - limit_price
    if side != state.get("side"):
        if order_age < REALTIME_MAKER_CANCEL_GRACE_SECONDS and edge >= 0:
            return None
        return f"{REALTIME_MAKER_MARKER} direction changed to {state.get('side')}"
    if edge < REALTIME_MAKER_CANCEL_MIN_EDGE:
        if order_age < REALTIME_MAKER_CANCEL_GRACE_SECONDS and edge >= 0:
            return None
        return f"{REALTIME_MAKER_MARKER} edge decayed {edge:.4f}"
    return None


def _weighted_probability(values: list[tuple[float | None, float]]) -> float | None:
    available = [(value, weight) for value, weight in values if value is not None and weight > 0]
    if not available:
        return None
    total_weight = sum(weight for _, weight in available)
    if total_weight <= 0:
        return None
    return max(0.01, min(0.99, sum(float(value) * weight for value, weight in available) / total_weight))


def _logistic_probability(value: float, scale: float) -> float:
    normalized_scale = max(0.1, float(scale))
    return max(0.01, min(0.99, 1.0 / (1.0 + math.exp(-float(value) / normalized_scale))))


def _round_float(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _live_once_blocked_payload(
    message: str,
    *,
    variant_id: str = LIVE_VARIANT_ID,
    combo: str = LIVE_COMBO,
    blocked_keys: list[str],
    fatal_keys: list[str],
    waitable_keys: list[str],
    preflight: dict[str, Any] | None,
    preflight_attempts: int,
    wait_ready_seconds: float,
    ready_wait_started_at: float,
) -> dict[str, Any]:
    return {
        "error": message,
        "live_once": {
            "execution_mode": "LIVE",
            "variant_id": variant_id,
            "combo": combo,
            "submitted": False,
            "blocked": True,
            "blocked_keys": _unique_strings(blocked_keys),
            "fatal_blocked_keys": _unique_strings(fatal_keys),
            "waitable_blocked_keys": _unique_strings(waitable_keys),
            "preflight": preflight,
            "preflight_attempts": int(preflight_attempts),
            "wait_ready_seconds": round(max(0.0, float(wait_ready_seconds or 0.0)), 3),
            "waited_ready_seconds": round(max(0.0, time.time() - ready_wait_started_at), 3),
        },
    }


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _sanitize_live_audit_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key or "").strip().lower()
            if normalized in LIVE_ONCE_AUDIT_SENSITIVE_KEYS:
                continue
            sanitized[str(key)] = _sanitize_live_audit_payload(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_live_audit_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_live_audit_payload(item) for item in value]
    return value


def _default_market_scout_runtime_settings(settings: Settings, *, now: float) -> dict[str, Any]:
    """市场页运行配置默认值；页面配置优先，环境变量提供首次启动默认值。"""

    return {
        "scanner_enabled": bool(settings.market_scout_enabled),
        "llm_enabled": bool(settings.llm_super_agent_enabled),
        "llm_model": _sanitize_market_scout_model(settings.llm_super_agent_model, "openai/gpt-5.4-mini"),
        "paper_auto_enabled": False,
        "live_auto_enabled": False,
        "paper_initial_balance": round(float(settings.market_scout_default_paper_initial_balance), 2),
        "paper_stake_dollars": round(float(settings.market_scout_default_paper_stake_dollars), 2),
        "paper_max_open_positions": int(settings.market_scout_default_paper_max_open_positions),
        "paper_probe_enabled": bool(settings.market_scout_default_paper_probe_enabled),
        "paper_probe_max_open_positions": int(settings.market_scout_default_paper_probe_max_open_positions),
        "paper_probe_min_confidence": round(float(settings.market_scout_default_paper_probe_min_confidence), 4),
        "paper_probe_min_selection_score": round(float(settings.market_scout_default_paper_probe_min_selection_score), 3),
        "paper_max_daily_loss": round(float(settings.market_scout_default_paper_max_daily_loss), 2),
        "min_confidence": round(float(settings.market_scout_default_paper_min_confidence), 4),
        "max_entry_price": round(float(settings.market_scout_default_paper_max_entry_price), 4),
        "max_spread": round(float(settings.market_scout_default_paper_max_spread), 4),
        "scan_interval_seconds": round(float(settings.market_scout_interval_seconds), 3),
        "analyze_top_n": int(settings.market_scout_analyze_top_n),
        "evidence_enabled": bool(settings.market_scout_evidence_enabled),
        "evidence_max_markets": int(settings.market_scout_evidence_max_markets),
        "evidence_results_per_market": int(settings.market_scout_evidence_results_per_market),
        "evidence_timeout_seconds": round(float(settings.market_scout_evidence_timeout_seconds), 3),
        "evidence_ttl_seconds": round(float(settings.market_scout_evidence_ttl_seconds), 3),
        "updated_at": now,
    }


def _sanitize_market_scout_runtime_settings(
    payload: dict[str, Any],
    current: dict[str, Any],
    settings: Settings,
    *,
    now: float,
) -> dict[str, Any]:
    """清洗市场页配置，避免页面输入异常值破坏运行态。"""

    base = _default_market_scout_runtime_settings(settings, now=now)
    base.update({key: current.get(key, value) for key, value in base.items() if key in current})
    if "scanner_enabled" in payload:
        base["scanner_enabled"] = _market_scout_bool(payload.get("scanner_enabled"), bool(base["scanner_enabled"]))
    if "llm_enabled" in payload:
        base["llm_enabled"] = _market_scout_bool(payload.get("llm_enabled"), bool(base["llm_enabled"]))
    if "llm_model" in payload:
        base["llm_model"] = _sanitize_market_scout_model(payload.get("llm_model"), settings.llm_super_agent_model)
    else:
        base["llm_model"] = _sanitize_market_scout_model(base.get("llm_model"), settings.llm_super_agent_model)
    if "evidence_enabled" in payload:
        base["evidence_enabled"] = _market_scout_bool(payload.get("evidence_enabled"), bool(base["evidence_enabled"]))
    if "paper_auto_enabled" in payload:
        base["paper_auto_enabled"] = _market_scout_bool(payload.get("paper_auto_enabled"), bool(base["paper_auto_enabled"]))
    if "paper_probe_enabled" in payload:
        base["paper_probe_enabled"] = _market_scout_bool(payload.get("paper_probe_enabled"), bool(base["paper_probe_enabled"]))
    # 市场页实盘自动下注这版强制锁定，防止误把 LLM 推荐接到真钱接口。
    base["live_auto_enabled"] = False
    base["paper_initial_balance"] = _bounded_market_scout_float(
        payload.get("paper_initial_balance", base["paper_initial_balance"]),
        base["paper_initial_balance"],
        minimum=1.0,
        maximum=1_000_000.0,
        digits=2,
    )
    base["paper_stake_dollars"] = _bounded_market_scout_float(
        payload.get("paper_stake_dollars", base["paper_stake_dollars"]),
        base["paper_stake_dollars"],
        minimum=0.1,
        maximum=10_000.0,
        digits=2,
    )
    base["paper_max_open_positions"] = _bounded_market_scout_int(
        payload.get("paper_max_open_positions", base["paper_max_open_positions"]),
        int(base["paper_max_open_positions"]),
        minimum=1,
        maximum=50,
    )
    base["paper_probe_max_open_positions"] = _bounded_market_scout_int(
        payload.get("paper_probe_max_open_positions", base["paper_probe_max_open_positions"]),
        int(base["paper_probe_max_open_positions"]),
        minimum=1,
        maximum=10,
    )
    base["paper_probe_min_confidence"] = _bounded_market_scout_float(
        payload.get("paper_probe_min_confidence", base["paper_probe_min_confidence"]),
        base["paper_probe_min_confidence"],
        minimum=0.0,
        maximum=1.0,
        digits=4,
    )
    base["paper_probe_min_selection_score"] = _bounded_market_scout_float(
        payload.get("paper_probe_min_selection_score", base["paper_probe_min_selection_score"]),
        base["paper_probe_min_selection_score"],
        minimum=0.0,
        maximum=100.0,
        digits=3,
    )
    base["paper_max_daily_loss"] = _bounded_market_scout_float(
        payload.get("paper_max_daily_loss", base["paper_max_daily_loss"]),
        base["paper_max_daily_loss"],
        minimum=0.0,
        maximum=1_000_000.0,
        digits=2,
    )
    base["min_confidence"] = _bounded_market_scout_float(
        payload.get("min_confidence", base["min_confidence"]),
        base["min_confidence"],
        minimum=0.0,
        maximum=1.0,
        digits=4,
    )
    base["max_entry_price"] = _bounded_market_scout_float(
        payload.get("max_entry_price", base["max_entry_price"]),
        base["max_entry_price"],
        minimum=0.01,
        maximum=0.99,
        digits=4,
    )
    base["max_spread"] = _bounded_market_scout_float(
        payload.get("max_spread", base["max_spread"]),
        base["max_spread"],
        minimum=0.0,
        maximum=0.99,
        digits=4,
    )
    base["scan_interval_seconds"] = _bounded_market_scout_float(
        payload.get("scan_interval_seconds", base["scan_interval_seconds"]),
        base["scan_interval_seconds"],
        minimum=5.0,
        maximum=3600.0,
        digits=3,
    )
    base["analyze_top_n"] = _bounded_market_scout_int(
        payload.get("analyze_top_n", base["analyze_top_n"]),
        int(base["analyze_top_n"]),
        minimum=1,
        maximum=20,
    )
    base["evidence_max_markets"] = _bounded_market_scout_int(
        payload.get("evidence_max_markets", base["evidence_max_markets"]),
        int(base["evidence_max_markets"]),
        minimum=0,
        maximum=20,
    )
    base["evidence_results_per_market"] = _bounded_market_scout_int(
        payload.get("evidence_results_per_market", base["evidence_results_per_market"]),
        int(base["evidence_results_per_market"]),
        minimum=1,
        maximum=8,
    )
    base["evidence_timeout_seconds"] = _bounded_market_scout_float(
        payload.get("evidence_timeout_seconds", base["evidence_timeout_seconds"]),
        base["evidence_timeout_seconds"],
        minimum=1.0,
        maximum=30.0,
        digits=3,
    )
    base["evidence_ttl_seconds"] = _bounded_market_scout_float(
        payload.get("evidence_ttl_seconds", base["evidence_ttl_seconds"]),
        base["evidence_ttl_seconds"],
        minimum=30.0,
        maximum=86_400.0,
        digits=3,
    )
    base["updated_at"] = now
    return base


def _sanitize_market_scout_model(value: Any, default: str) -> str:
    """清洗页面输入的模型名，空值回退环境变量，避免空模型请求 LLM。"""

    fallback = str(default or "openai/gpt-5.4-mini").strip() or "openai/gpt-5.4-mini"
    text = str(value or "").strip()
    if not text:
        return fallback
    text = " ".join(text.split())
    if any(ch.isspace() for ch in text):
        return fallback
    return text[:120]


def _market_scout_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _bounded_market_scout_float(
    value: Any,
    default: Any,
    *,
    minimum: float,
    maximum: float,
    digits: int,
) -> float:
    parsed = _maybe_float(value)
    if parsed is None:
        parsed = _maybe_float(default)
    if parsed is None:
        parsed = minimum
    parsed = max(float(minimum), min(float(maximum), float(parsed)))
    return round(parsed, int(digits))


def _bounded_market_scout_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    parsed = _maybe_int(value)
    if parsed is None:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), int(parsed)))


def _market_scout_outcome_index(candidate: dict[str, Any], outcome: str) -> int:
    normalized = str(outcome or "").strip().lower()
    for index, item in enumerate(candidate.get("outcomes") or []):
        if str(item or "").strip().lower() == normalized:
            return index
    return -1


def _market_scout_outcome_quote(candidate: dict[str, Any], outcome: str) -> dict[str, Any]:
    quotes = candidate.get("quotes")
    if not isinstance(quotes, dict):
        return {}
    direct = quotes.get(outcome)
    if isinstance(direct, dict):
        return dict(direct)
    normalized = str(outcome or "").strip().lower()
    for key, value in quotes.items():
        if str(key or "").strip().lower() == normalized and isinstance(value, dict):
            return dict(value)
    return {}


def _market_scout_entry_price(
    candidate: dict[str, Any],
    outcome_index: int,
    quote: dict[str, Any],
) -> float | None:
    ask = _maybe_float(quote.get("best_ask")) if isinstance(quote, dict) else None
    if ask is not None and 0.0 < ask < 1.0:
        return round(ask, 4)
    prices = candidate.get("outcome_prices") if isinstance(candidate.get("outcome_prices"), list) else []
    if 0 <= outcome_index < len(prices):
        price = _maybe_float(prices[outcome_index])
        if price is not None and 0.0 < price < 1.0:
            return round(price, 4)
    return None


def _market_scout_compact_key(value: Any, limit: int = 72) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    safe_limit = max(12, int(limit))
    return text if len(text) <= safe_limit else f"{text[: safe_limit - 3]}..."


def _market_scout_candidate_log_name(candidate: dict[str, Any]) -> str:
    return _market_scout_compact_key(candidate.get("question") or candidate.get("slug") or "-", 96)


def _market_scout_candidate_exposure_block(
    candidate: dict[str, Any],
    exposure_context: dict[str, Any] | None,
) -> str:
    """识别候选是否已经有同市场或同事件族 Paper 暴露。"""

    if not exposure_context:
        return ""
    slug = str(candidate.get("slug") or "").strip()
    if slug and slug in set(exposure_context.get("open_slugs") or set()):
        return f"该市场已有 Paper 持仓 slug={slug}"
    event_key = str(candidate.get("event_key") or _market_scout_event_key(candidate)).strip().lower()
    source_event_key = str(candidate.get("source_event_key") or _market_scout_source_event_key(candidate)).strip().lower()
    open_event_keys = set(exposure_context.get("open_event_keys") or set())
    open_source_event_keys = set(exposure_context.get("open_source_event_keys") or set())
    if event_key and event_key in open_event_keys:
        return f"同事件族已有 Paper 持仓 event={_market_scout_compact_key(event_key)}"
    if source_event_key and source_event_key in open_source_event_keys:
        return f"同源事件已有 Paper 持仓 event={_market_scout_compact_key(source_event_key)}"
    return ""


def _market_scout_build_probe_decision(
    decision: dict[str, Any],
    candidates: list[dict[str, Any]],
    runtime_settings: dict[str, Any],
    *,
    exposure_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """LLM 给出 NO_TRADE 时构造小额 Paper 探针决策，只用于采样复盘。"""

    if not runtime_settings.get("paper_probe_enabled"):
        return None, "LLM 未给出 RECOMMEND，Paper 探针关闭", []
    if not candidates:
        return None, "LLM 未给出 RECOMMEND，探针没有候选市场", []
    max_entry = float(runtime_settings.get("max_entry_price") or 0.99)
    max_spread = float(runtime_settings.get("max_spread") or 1.0)
    min_score = float(runtime_settings.get("paper_probe_min_selection_score") or 0.0)
    min_confidence = float(runtime_settings.get("paper_probe_min_confidence") or 0.0)
    rejection_samples: list[str] = []
    for candidate in candidates:
        candidate_name = _market_scout_candidate_log_name(candidate)
        exposure_block = _market_scout_candidate_exposure_block(candidate, exposure_context)
        if exposure_block:
            rejection_samples.append(f"{candidate_name}: {exposure_block}")
            continue
        score = _maybe_float(candidate.get("selection_score")) or 0.0
        if score < min_score:
            rejection_samples.append(f"{candidate_name}: 分数 {score:.3f} 低于 {min_score:.3f}")
            continue
        choice = _market_scout_probe_outcome(candidate, decision, max_entry=max_entry, max_spread=max_spread)
        if choice is None:
            rejection_samples.append(f"{candidate_name}: 无可执行方向")
            continue
        outcome, entry_price, spread = choice
        probe_confidence = _market_scout_probe_confidence(candidate, entry_price, spread, min_score)
        if probe_confidence < min_confidence:
            rejection_samples.append(f"{candidate_name}: 探针置信度 {probe_confidence:.4f} 低于 {min_confidence:.4f}")
            continue
        reason = str(decision.get("reason") or "").strip()
        return {
            "decision": "RECOMMEND",
            "selected_slug": candidate.get("slug") or "",
            "question": candidate.get("question"),
            "url": candidate.get("url"),
            "outcome": outcome,
            "confidence": probe_confidence,
            "max_entry_price": min(max_entry, entry_price),
            "reason": (
                f"MARKET_SCOUT_PROBE Paper 探针放行。"
                f"LLM 原始决策={decision.get('decision') or 'NO_TRADE'}，"
                f"本地候选分={score:.3f}，价差={_format_optional_float(spread, 4)}，"
                f"原始理由={reason[:500]}"
            ),
            "risk_flags": list(decision.get("risk_flags") or [])[:8] + ["paper_probe_sample"],
            "news_checks_needed": list(decision.get("news_checks_needed") or [])[:8],
            "valid_for_seconds": decision.get("valid_for_seconds") or 60,
            "probe_reason": f"LLM NO_TRADE 探针放行，候选分 {score:.3f}",
            "probe_skip_reasons": rejection_samples[:MARKET_SCOUT_ORDER_SKIP_LOG_LIMIT],
            "raw": decision.get("raw") or {},
        }, "", rejection_samples
    suffix = "; ".join(rejection_samples[:3])
    return None, f"LLM 未给出 RECOMMEND，探针未满足阈值{': ' + suffix if suffix else ''}", rejection_samples


def _market_scout_probe_outcome(
    candidate: dict[str, Any],
    decision: dict[str, Any],
    *,
    max_entry: float,
    max_spread: float,
) -> tuple[str, float, float | None] | None:
    outcomes = [str(item) for item in (candidate.get("outcomes") or []) if str(item or "").strip()]
    if not outcomes:
        return None
    hinted = _market_scout_probe_hinted_outcomes(candidate, decision, outcomes)
    ordered_outcomes = hinted + [outcome for outcome in outcomes if outcome not in hinted]
    choices: list[tuple[int, float, str, float, float | None]] = []
    for outcome in ordered_outcomes:
        outcome_index = _market_scout_outcome_index(candidate, outcome)
        if outcome_index < 0:
            continue
        quote = _market_scout_outcome_quote(candidate, outcome)
        entry_price = _market_scout_entry_price(candidate, outcome_index, quote)
        if entry_price is None or entry_price > max_entry + 0.000001 or entry_price < 0.03:
            continue
        spread = _maybe_float(quote.get("spread")) if isinstance(quote, dict) else _maybe_float(candidate.get("spread"))
        if spread is not None and spread > max_spread:
            continue
        hinted_rank = 0 if outcome in hinted else 1
        choices.append((hinted_rank, entry_price, outcome, entry_price, spread))
    if not choices:
        return None
    choices.sort(key=lambda item: (item[0], item[1]))
    _, _, outcome, entry_price, spread = choices[0]
    return outcome, entry_price, spread


def _market_scout_probe_hinted_outcomes(
    candidate: dict[str, Any],
    decision: dict[str, Any],
    outcomes: list[str],
) -> list[str]:
    reason = str(decision.get("reason") or "").lower()
    if not reason:
        return []
    question = str(candidate.get("question") or "").lower()
    slug = str(candidate.get("slug") or "").lower()
    compact_question = re.sub(r"[^a-z0-9]+", " ", question).strip()
    probe_texts = [text for text in (question, slug, compact_question) if text]
    mentioned = any(text in reason for text in probe_texts)
    if not mentioned:
        words = compact_question.split()
        mentioned = len(words) >= 5 and " ".join(words[:5]) in reason
    if not mentioned:
        return []
    hinted: list[str] = []
    for outcome in outcomes:
        normalized = str(outcome or "").strip().lower()
        if normalized and re.search(rf"\b{re.escape(normalized)}\b", reason):
            hinted.append(outcome)
    return hinted


def _market_scout_probe_confidence(
    candidate: dict[str, Any],
    entry_price: float,
    spread: float | None,
    min_score: float,
) -> float:
    score = _maybe_float(candidate.get("selection_score")) or 0.0
    score_lift = max(0.0, score - min_score) * 0.018
    spread_penalty = max(0.0, spread or 0.0) * 1.2
    price_penalty = max(0.0, entry_price - 0.35) * 0.18
    confidence = 0.55 + score_lift - spread_penalty - price_penalty
    return round(max(0.0, min(0.68, confidence)), 4)


def _market_scout_order_reason(decision: dict[str, Any], probe_mode: bool) -> str:
    if probe_mode:
        return f"MARKET_SCOUT_PROBE Paper 探针放行 | {decision.get('reason') or ''}"
    return f"MARKET_SCOUT_LLM Paper 自动下注 | {decision.get('reason') or ''}"


def _market_scout_polymarket_url(market_slug: str, event_slug: str | None = "") -> str:
    """生成 Polymarket 官方页面链接；market 入口会重定向到当前 canonical 事件页。"""

    market = str(market_slug or "").strip().strip("/")
    if not market:
        return ""
    return f"https://polymarket.com/market/{market}"


def _market_scout_market_round(candidate: dict[str, Any], outcome_index: int, now: float) -> MarketRound:
    token_ids = list(candidate.get("token_ids") or [])
    selected_token = str(token_ids[outcome_index]) if 0 <= outcome_index < len(token_ids) else ""
    other_token = ""
    for index, token_id in enumerate(token_ids):
        if index != outcome_index:
            other_token = str(token_id or "")
            break
    ends_at = _maybe_float(candidate.get("end_ts")) or now + 3600.0
    return MarketRound(
        round_id=str(candidate.get("slug") or ""),
        symbol=MARKET_SCOUT_SYMBOL,
        started_at=now,
        ends_at=max(now + 1.0, float(ends_at)),
        target_price=0.0,
        question=str(candidate.get("question") or ""),
        condition_id=str(candidate.get("condition_id") or ""),
        up_token=selected_token,
        down_token=other_token,
        event_slug=str(candidate.get("event_slug") or ""),
        url=str(candidate.get("url") or ""),
    )


def _market_scout_candidate_from_raw(raw: dict[str, Any], *, now: float) -> dict[str, Any] | None:
    slug = str(raw.get("slug") or "").strip()
    question = str(raw.get("question") or "").strip()
    if not slug or not question:
        return None
    event = _market_scout_first_event(raw)
    event_title = str(event.get("title") or event.get("slug") or "").strip() if event else ""
    event_slug = str(event.get("slug") or "").strip() if event else ""
    event_metadata = event.get("eventMetadata") if isinstance(event.get("eventMetadata"), dict) else {}
    context_description = str(event_metadata.get("context_description") or "").strip() if event_metadata else ""
    base_description = str(raw.get("description") or (event.get("description") if event else "") or "").strip()
    description = _market_scout_join_description(base_description, context_description)
    outcomes = [str(item) for item in _jsonish_list_value(raw.get("outcomes"))]
    token_ids = [str(item) for item in _jsonish_list_value(raw.get("clobTokenIds"))]
    outcome_prices = [_maybe_float(item) for item in _jsonish_list_value(raw.get("outcomePrices"))]
    liquidity = _first_float(raw, ("liquidityNum", "liquidityClob", "liquidity"))
    volume_24h = _first_float(raw, ("volume24hr", "volume24hrClob"))
    volume = _first_float(raw, ("volumeNum", "volumeClob", "volume"))
    spread = _maybe_float(raw.get("spread"))
    best_bid = _maybe_float(raw.get("bestBid"))
    best_ask = _maybe_float(raw.get("bestAsk"))
    if spread is None and best_bid is not None and best_ask is not None:
        spread = max(0.0, best_ask - best_bid)
    end_ts = _parse_market_scout_ts(raw.get("endDateIso") or raw.get("endDate"))
    score = _market_scout_candidate_score(
        liquidity=liquidity,
        volume_24h=volume_24h,
        spread=spread,
        outcome_prices=outcome_prices,
    )
    return {
        "slug": slug,
        "question": question,
        "event_title": event_title,
        "event_slug": event_slug,
        "description": description[:1600],
        "event_context": context_description[:1200],
        "category": str(raw.get("category") or event.get("category") or "").strip() if event else str(raw.get("category") or "").strip(),
        "tags": _market_scout_tag_names(raw, event),
        "url": _market_scout_polymarket_url(slug, event_slug),
        "condition_id": str(raw.get("conditionId") or ""),
        "outcomes": outcomes,
        "token_ids": token_ids,
        "outcome_prices": outcome_prices,
        "liquidity": liquidity,
        "volume_24h": volume_24h,
        "volume": volume,
        "spread": spread,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "last_trade_price": _maybe_float(raw.get("lastTradePrice")),
        "one_day_price_change": _maybe_float(raw.get("oneDayPriceChange")),
        "end_ts": end_ts,
        "seconds_to_end": max(0.0, end_ts - now) if end_ts else None,
        "active": _truthy_value(raw.get("active")),
        "closed": _truthy_value(raw.get("closed")),
        "archived": _truthy_value(raw.get("archived")),
        "accepting_orders": _truthy_value(raw.get("acceptingOrders")),
        "enable_order_book": _truthy_value(raw.get("enableOrderBook")),
        "restricted": _truthy_value(raw.get("restricted")),
        "score": score,
    }


def _market_scout_reject_reason(candidate: dict[str, Any], settings: Settings) -> str:
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("slug", "question", "event_title", "description")
    ).lower()
    if "btc" in text or "bitcoin" in text:
        return "btc_market"
    if not candidate.get("active") or candidate.get("closed") or candidate.get("archived"):
        return "inactive"
    if not candidate.get("accepting_orders") or not candidate.get("enable_order_book"):
        return "orders_closed"
    outcomes = list(candidate.get("outcomes") or [])
    token_ids = list(candidate.get("token_ids") or [])
    if len(outcomes) != 2 or len(token_ids) != 2:
        return "non_binary"
    prices = [_maybe_float(item) for item in (candidate.get("outcome_prices") or [])]
    valid_prices = [price for price in prices if price is not None]
    if len(valid_prices) < 2:
        return "missing_prices"
    if max(valid_prices) >= 0.97 or min(valid_prices) <= 0.03:
        return "resolved_or_extreme_price"
    liquidity = _maybe_float(candidate.get("liquidity")) or 0.0
    if liquidity < settings.market_scout_min_liquidity:
        return "low_liquidity"
    volume_24h = _maybe_float(candidate.get("volume_24h")) or 0.0
    if volume_24h < settings.market_scout_min_volume_24h:
        return "low_volume_24h"
    spread = _maybe_float(candidate.get("spread"))
    if spread is not None and spread > 0.08:
        return "wide_spread"
    return ""


def _market_scout_prepare_candidate_for_selection(candidate: dict[str, Any], *, base_rank: int) -> dict[str, Any]:
    """生成 Market Scout 候选画像；用于页面展示和 LLM 输入前的多样化筛选。"""

    prepared = dict(candidate)
    profile = _market_scout_selection_profile(prepared)
    base_score = _maybe_float(prepared.get("score")) or 0.0
    selection_score = round(base_score + float(profile["score_delta"]), 6)
    prepared.update(
        {
            "base_rank": int(base_rank),
            "selection_score": selection_score,
            "selection_bucket": profile["bucket"],
            "selection_notes": profile["notes"],
            "llm_block_reason": profile["block_reason"],
            "event_key": _market_scout_event_key(prepared),
            "source_event_key": _market_scout_source_event_key(prepared),
            "llm_selected": False,
            "llm_rank": None,
        }
    )
    return prepared


def _market_scout_selection_profile(candidate: dict[str, Any]) -> dict[str, Any]:
    text = _market_scout_candidate_text(candidate)
    seconds_to_end = _maybe_float(candidate.get("seconds_to_end"))
    score_delta = 0.0
    notes: list[str] = []
    bucket = "general"
    block_reason = ""

    is_sports = _market_scout_is_sports_market(text)
    raw_event_driven = _market_scout_is_event_driven_market(text)
    is_event_driven = raw_event_driven and not is_sports
    is_outright = _market_scout_is_long_dated_outright(candidate, text)

    if is_sports:
        bucket = "sports"
        score_delta -= 1.6
        notes.append("sports")
    if is_event_driven:
        bucket = "event"
        score_delta += 2.35
        notes.append("event_driven")
    if is_outright:
        bucket = "outright"
        score_delta -= 2.5
        notes.append("long_dated_outright")
    if seconds_to_end is not None:
        if seconds_to_end <= 0:
            score_delta -= 1.5
            notes.append("end_time_stale")
            if is_sports:
                score_delta -= 8.0
                block_reason = "stale_sports_market"
        elif seconds_to_end <= 36 * 3600:
            score_delta += 0.4 if is_sports else 0.7
            notes.append("near_term")
        elif seconds_to_end >= 45 * 86400 and not is_event_driven:
            score_delta -= 0.6
            notes.append("long_horizon")
    if not notes:
        notes.append("base_liquidity_volume_score")

    return {
        "bucket": bucket,
        "score_delta": round(score_delta, 6),
        "notes": notes[:8],
        "block_reason": block_reason,
    }


def _market_scout_select_llm_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """从展示池里挑 LLM 输入；控制同事件占坑，并跳过明显过期的体育市场。"""

    safe_limit = max(1, min(20, int(limit)))
    eligible = [candidate for candidate in candidates if not candidate.get("llm_block_reason")]
    selected: list[dict[str, Any]] = []
    selected_slugs: set[str] = set()
    event_counts: Counter[str] = Counter()
    source_event_counts: Counter[str] = Counter()

    def add_candidates(*, event_cap: int) -> None:
        for candidate in eligible:
            if len(selected) >= safe_limit:
                return
            slug = str(candidate.get("slug") or "")
            if not slug or slug in selected_slugs:
                continue
            event_key = _market_scout_event_key(candidate)
            source_event_key = _market_scout_source_event_key(candidate)
            if event_key and event_counts[event_key] >= event_cap:
                continue
            if source_event_key and source_event_counts[source_event_key] >= event_cap:
                continue
            selected.append(candidate)
            selected_slugs.add(slug)
            if event_key:
                event_counts[event_key] += 1
            if source_event_key:
                source_event_counts[source_event_key] += 1

    add_candidates(event_cap=1)
    add_candidates(event_cap=2)
    if len(selected) < safe_limit:
        for candidate in eligible:
            if len(selected) >= safe_limit:
                break
            slug = str(candidate.get("slug") or "")
            if slug and slug not in selected_slugs:
                selected.append(candidate)
                selected_slugs.add(slug)

    if not selected:
        selected = candidates[:safe_limit]
    return selected[:safe_limit]


def _market_scout_mark_llm_selection(candidate: dict[str, Any], llm_rank_by_slug: dict[str, int]) -> dict[str, Any]:
    marked = dict(candidate)
    rank = llm_rank_by_slug.get(str(marked.get("slug") or ""))
    marked["llm_selected"] = rank is not None
    marked["llm_rank"] = rank
    return marked


def _market_scout_candidate_text(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("slug", "question", "event_title", "description", "event_context", "category"):
        value = candidate.get(key)
        if value:
            parts.append(str(value))
    tags = candidate.get("tags")
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags if str(tag or "").strip())
    return " ".join(parts).lower()


def _market_scout_event_key(candidate: dict[str, Any]) -> str:
    text = _market_scout_candidate_text(candidate)
    topic_key = _market_scout_topic_key(candidate)
    if topic_key and not _market_scout_is_sports_market(text):
        return topic_key
    return str(
        candidate.get("event_slug")
        or candidate.get("event_title")
        or candidate.get("condition_id")
        or candidate.get("slug")
        or ""
    ).strip().lower()


def _market_scout_source_event_key(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("event_slug")
        or candidate.get("event_title")
        or candidate.get("condition_id")
        or candidate.get("slug")
        or ""
    ).strip().lower()


def _market_scout_topic_key(candidate: dict[str, Any]) -> str:
    question = str(candidate.get("question") or "").lower()
    if not question:
        return ""
    text = re.sub(r"[?!.]", " ", question)
    month = r"january|february|march|april|may|june|july|august|september|october|november|december"
    text = re.sub(rf"\bby\s+end\s+of\s+({month})\b", "", text)
    text = re.sub(rf"\bby\s+({month})\s+\d{{1,2}}(,\s*\d{{4}})?\b", "", text)
    text = re.sub(r"\bby\s+20\d\d-\d\d-\d\d\b", "", text)
    text = re.sub(r"\bbefore\s+20\d\d\b", "before-year", text)
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text[:120] if len(text) >= 18 else ""


def _market_scout_is_sports_market(text: str) -> bool:
    if re.search(r"\bvs\.?\b|spread:|\bo/u\b|\bwin on 20\d\d-\d\d-\d\d\b", text):
        return True
    if re.search(r"\b(nba|wnba|nfl|mlb|nhl|ufc|fifa|soccer|tennis|match)\b", text):
        return True
    sports_phrases = (
        "world cup",
        "premier league",
        "champions league",
    )
    return any(term in text for term in sports_phrases)


def _market_scout_is_event_driven_market(text: str) -> bool:
    event_terms = (
        "iran",
        "israel",
        "hormuz",
        "ceasefire",
        "peace deal",
        "war",
        "missile",
        "nuclear",
        "trump",
        "fed",
        "rate cut",
        "interest rate",
        "cpi",
        "inflation",
        "gdp",
        "election",
        "tariff",
        "court",
        "sec",
        "lawsuit",
        "approval",
        "government",
        "shutdown",
        "strike",
        "etf",
        "earnings",
        "ipo",
        "bankruptcy",
        "oil",
        "opec",
        "ukraine",
        "russia",
        "china",
        "taiwan",
        "gaza",
        "traffic returns",
    )
    return any(term in text for term in event_terms)


def _market_scout_is_long_dated_outright(candidate: dict[str, Any], text: str) -> bool:
    seconds_to_end = _maybe_float(candidate.get("seconds_to_end"))
    long_horizon = seconds_to_end is not None and seconds_to_end >= 21 * 86400
    outright_terms = (
        "win the 2026 fifa world cup",
        "win the world cup",
        "champion",
        "championship winner",
        "tournament winner",
        "winner of",
    )
    return long_horizon and any(term in text for term in outright_terms)


def _market_scout_candidate_score(
    *,
    liquidity: float | None,
    volume_24h: float | None,
    spread: float | None,
    outcome_prices: list[float | None],
) -> float:
    liq = max(0.0, float(liquidity or 0.0))
    vol = max(0.0, float(volume_24h or 0.0))
    spread_penalty = max(0.0, min(1.0, float(spread or 0.0) / 0.08))
    prices = [price for price in outcome_prices if price is not None and 0.0 < price < 1.0]
    uncertainty = max((min(price, 1.0 - price) for price in prices), default=0.0)
    return round(
        math.log1p(liq) * 0.35
        + math.log1p(vol) * 0.45
        + uncertainty * 4.0
        + (1.0 - spread_penalty) * 2.0,
        6,
    )


def _market_scout_scan_details(
    candidates: list[dict[str, Any]],
    reject_counts: Counter[str],
    reject_examples: dict[str, str],
    *,
    llm_candidates: list[dict[str, Any]] | None = None,
) -> list[str]:
    details = [
        _market_scout_candidate_line(candidate, index)
        for index, candidate in enumerate(candidates[:MARKET_SCOUT_SCAN_LOG_CANDIDATE_LIMIT], start=1)
    ]
    if len(candidates) > MARKET_SCOUT_SCAN_LOG_CANDIDATE_LIMIT:
        details.append(f"展示候选还有 {len(candidates) - MARKET_SCOUT_SCAN_LOG_CANDIDATE_LIMIT} 个，页面可继续查看")
    if llm_candidates:
        llm_text = ", ".join(
            f"{index}.{candidate.get('question') or candidate.get('slug')}"
            for index, candidate in enumerate(llm_candidates, start=1)
        )
        details.append(f"送入 LLM: {llm_text}")
    if reject_counts:
        rejected = ", ".join(f"{key}={value}" for key, value in reject_counts.most_common(MARKET_SCOUT_REJECT_LOG_LIMIT))
        details.append(f"过滤统计: {rejected}")
        for key, example in list(reject_examples.items())[:3]:
            details.append(f"过滤样例 {key}: {example}")
    return details or ["没有通过过滤的候选市场"]


def _market_scout_candidate_line(candidate: dict[str, Any], index: int) -> str:
    prices = candidate.get("outcome_prices") if isinstance(candidate.get("outcome_prices"), list) else []
    outcomes = candidate.get("outcomes") if isinstance(candidate.get("outcomes"), list) else []
    price_text = " / ".join(
        f"{outcome}={_format_optional_float(price, 4)}"
        for outcome, price in zip(outcomes, prices)
    )
    route = f"LLM#{candidate.get('llm_rank')}" if candidate.get("llm_selected") else "display"
    notes = ",".join(str(item) for item in (candidate.get("selection_notes") or [])[:3])
    return (
        f"{index}. {candidate.get('question')} | {price_text or 'price=-'} | "
        f"liq={_format_optional_float(candidate.get('liquidity'), 0)} "
        f"vol24h={_format_optional_float(candidate.get('volume_24h'), 0)} "
        f"spread={_format_optional_float(candidate.get('spread'), 4)} "
        f"score={_format_optional_float(candidate.get('score'), 3)} "
        f"scout={_format_optional_float(candidate.get('selection_score'), 3)} "
        f"{route} bucket={candidate.get('selection_bucket') or '-'} notes={notes or '-'}"
    )


def _market_scout_candidate_signature(candidates: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for candidate in candidates[:MARKET_SCOUT_PROMPT_CANDIDATE_LIMIT]:
        prices = ",".join(_format_optional_float(price, 4) for price in candidate.get("outcome_prices") or [])
        parts.append(f"{candidate.get('slug')}:{prices}:{_format_optional_float(candidate.get('spread'), 4)}")
    return "|".join(parts)


def _market_scout_candidate_public(candidate: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "slug",
        "question",
        "url",
        "outcomes",
        "outcome_prices",
        "liquidity",
        "volume_24h",
        "spread",
        "score",
        "selection_score",
        "selection_bucket",
        "selection_notes",
        "llm_block_reason",
        "llm_selected",
        "llm_rank",
        "base_rank",
        "event_title",
        "event_slug",
        "event_key",
        "source_event_key",
        "seconds_to_end",
        "evidence_status",
        "evidence_result_count",
        "evidence",
    )
    return {key: candidate.get(key) for key in keys}


def _market_scout_attach_candidate_evidence(
    candidate: dict[str, Any],
    evidence_by_slug: dict[str, Any],
) -> dict[str, Any]:
    """把 LLM 候选证据同步到展示候选，保证详情面板能看到同一份 Web 证据。"""

    slug = str(candidate.get("slug") or "")
    evidence = evidence_by_slug.get(slug)
    if not isinstance(evidence, dict):
        return candidate
    merged = dict(candidate)
    merged["evidence"] = dict(evidence)
    merged["evidence_status"] = evidence.get("status")
    merged["evidence_result_count"] = evidence.get("result_count", 0)
    return merged


def _market_scout_llm_system_prompt() -> str:
    return (
        "You are a strict Polymarket non-BTC market analyst. Return JSON only. "
        "You receive active binary markets with liquidity, volume, prices, spread, description, orderbook quotes, and Evidence Scout news results when available. "
        "Select at most one market. Prefer NO_TRADE if evidence is stale, ambiguous, over-resolved, too spread-wide, or outside your confidence. "
        "You cannot browse the web inside this call. Use provided evidence only and list missing external checks in news_checks_needed. "
        "Do not place orders. Output schema: decision, selected_slug, outcome, confidence, max_entry_price, reason, risk_flags, news_checks_needed, valid_for_seconds."
    )


def _market_scout_llm_user_prompt(candidates: list[dict[str, Any]], now: float) -> str:
    payload = {
        "now": now,
        "instruction": (
            "Analyze these non-BTC Polymarket candidates. Return decision RECOMMEND or NO_TRADE. "
            "If RECOMMEND, choose one selected_slug and one exact outcome from that market. "
            "Use confidence 0-1. Keep max_entry_price conservative. "
            "Candidates were pre-ranked for diversity, event relevance, liquidity, volume, spread, and orderbook quality. "
            "This LLM call is advisory; backend Paper gates decide whether to simulate an order after the response."
        ),
        "candidates": [_market_scout_prompt_candidate(candidate) for candidate in candidates[:MARKET_SCOUT_PROMPT_CANDIDATE_LIMIT]],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) > MARKET_SCOUT_PROMPT_MAX_CHARS:
        text = text[:MARKET_SCOUT_PROMPT_MAX_CHARS] + "...TRUNCATED"
    return text


def _market_scout_prompt_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": candidate.get("slug"),
        "question": candidate.get("question"),
        "event_title": candidate.get("event_title"),
        "event_slug": candidate.get("event_slug"),
        "description": str(candidate.get("description") or "")[:900],
        "event_context": str(candidate.get("event_context") or "")[:900],
        "category": candidate.get("category"),
        "tags": candidate.get("tags"),
        "url": candidate.get("url"),
        "outcomes": candidate.get("outcomes"),
        "outcome_prices": candidate.get("outcome_prices"),
        "liquidity": candidate.get("liquidity"),
        "volume_24h": candidate.get("volume_24h"),
        "spread": candidate.get("spread"),
        "last_trade_price": candidate.get("last_trade_price"),
        "one_day_price_change": candidate.get("one_day_price_change"),
        "seconds_to_end": candidate.get("seconds_to_end"),
        "evidence": _market_scout_prompt_evidence(candidate.get("evidence")),
        "quotes": candidate.get("quotes"),
        "score": candidate.get("score"),
        "selection_score": candidate.get("selection_score"),
        "selection_bucket": candidate.get("selection_bucket"),
        "selection_notes": candidate.get("selection_notes"),
        "base_rank": candidate.get("base_rank"),
    }


def _market_scout_prompt_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    results = value.get("results") if isinstance(value.get("results"), list) else []
    return {
        "status": value.get("status"),
        "provider": value.get("provider"),
        "query": value.get("query"),
        "result_count": value.get("result_count"),
        "blocked_count": value.get("blocked_count"),
        "notes": list(value.get("notes") or [])[:5] if isinstance(value.get("notes"), list) else [],
        "results": [
            {
                "title": result.get("title"),
                "source": result.get("source"),
                "domain": result.get("domain"),
                "published_at": result.get("published_at"),
                "age_hours": result.get("age_hours"),
                "snippet": result.get("snippet"),
                "url": result.get("url"),
            }
            for result in results[:4]
            if isinstance(result, dict)
        ],
    }


def _normalize_market_scout_llm_decision(raw: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_by_slug = {str(candidate.get("slug") or ""): candidate for candidate in candidates}
    decision = str(raw.get("decision") or "NO_TRADE").strip().upper()
    if decision not in {"RECOMMEND", "NO_TRADE"}:
        decision = "NO_TRADE"
    selected_slug = str(raw.get("selected_slug") or "").strip()
    if selected_slug not in candidate_by_slug:
        if decision == "RECOMMEND":
            selected_slug = str(candidates[0].get("slug") or "") if candidates else ""
            decision = "NO_TRADE"
        else:
            selected_slug = ""
    selected = candidate_by_slug.get(selected_slug, candidates[0] if selected_slug and candidates else {})
    outcomes = {str(outcome).lower(): str(outcome) for outcome in selected.get("outcomes") or []}
    raw_outcome = str(raw.get("outcome") or "").strip()
    outcome = outcomes.get(raw_outcome.lower(), "")
    if decision == "RECOMMEND" and not outcome:
        decision = "NO_TRADE"
    confidence = max(0.0, min(1.0, _maybe_float(raw.get("confidence")) or 0.0))
    max_entry_price = _maybe_float(raw.get("max_entry_price"))
    risk_flags = raw.get("risk_flags")
    if not isinstance(risk_flags, list):
        risk_flags = []
    news_checks = raw.get("news_checks_needed")
    if not isinstance(news_checks, list):
        news_checks = []
    return {
        "decision": decision,
        "selected_slug": selected_slug,
        "question": selected.get("question"),
        "url": selected.get("url"),
        "outcome": outcome or None,
        "confidence": round(confidence, 4),
        "max_entry_price": _round_float(max_entry_price, 4),
        "reason": str(raw.get("reason") or "LLM 未提供理由")[:800],
        "risk_flags": [str(item)[:120] for item in risk_flags[:12]],
        "news_checks_needed": [str(item)[:160] for item in news_checks[:12]],
        "valid_for_seconds": max(10, min(600, _maybe_int(raw.get("valid_for_seconds")) or 60)),
        "raw": raw,
    }


def _market_scout_decision_status_message(decision: dict[str, Any]) -> str:
    if decision.get("decision") == "RECOMMEND":
        return (
            f"LLM 推荐 {decision.get('outcome')} @ {decision.get('confidence')} "
            f"市场={decision.get('selected_slug')}"
        )
    return f"LLM 暂无下注建议 市场={decision.get('selected_slug') or '-'}"


def _market_scout_decision_details(decision: dict[str, Any], elapsed_ms: float) -> list[str]:
    details = [
        f"市场: {decision.get('question') or decision.get('selected_slug')}",
        f"方向: {decision.get('outcome') or '-'}",
        f"置信度: {_format_optional_float(decision.get('confidence'), 4)}",
        f"最高入场价: {_format_optional_float(decision.get('max_entry_price'), 4)}",
        f"耗时: {_format_optional_float(elapsed_ms, 1)}ms",
        f"理由: {decision.get('reason')}",
    ]
    for flag in decision.get("risk_flags") or []:
        details.append(f"风险: {flag}")
    for check in decision.get("news_checks_needed") or []:
        details.append(f"需核查: {check}")
    details.append("自动下注执行: 按市场页 Paper 开关和本地闸门决定")
    return details


def _market_scout_first_event(raw: dict[str, Any]) -> dict[str, Any]:
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return events[0]
    return {}


def _market_scout_join_description(description: str, context_description: str) -> str:
    parts: list[str] = []
    for value in (description, context_description):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts)


def _market_scout_tag_names(raw: dict[str, Any], event: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for payload in (raw, event):
        raw_tags = payload.get("tags") if isinstance(payload, dict) else None
        for item in _jsonish_list_value(raw_tags):
            if isinstance(item, dict):
                text = str(item.get("label") or item.get("name") or item.get("slug") or "").strip()
            else:
                text = str(item or "").strip()
            if text and text not in tags:
                tags.append(text)
    return tags[:12]


def _jsonish_list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _first_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _maybe_float(payload.get(key))
        if value is not None:
            return value
    return None


def _parse_market_scout_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        from datetime import datetime, time as datetime_time, timezone

        # Polymarket 的 date-only endDateIso 表示自然日，按当天结束处理，避免整天机会被误判过期。
        return datetime.combine(datetime.fromisoformat(text).date(), datetime_time(23, 59, 59), tzinfo=timezone.utc).timestamp()
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _spread_from_quote_payload(quote: dict[str, Any]) -> float | None:
    bid = _maybe_float(quote.get("best_bid"))
    ask = _maybe_float(quote.get("best_ask"))
    if bid is None or ask is None:
        return None
    return round(max(0.0, ask - bid), 6)


def _audit_filename_part(value: Any) -> str:
    text = str(value or "unknown").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in text)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return (cleaned or "unknown")[:80]


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_optional_float(value: Any, digits: int) -> str:
    parsed = _maybe_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:.{int(digits)}f}"


def _maybe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _age_seconds(now: float, timestamp: Any) -> float | None:
    value = _maybe_float(timestamp)
    if value is None or value <= 0:
        return None
    return round(max(0.0, now - value), 3)


def _age_ms_from_updated(now_ms: int, value: Any) -> int | None:
    updated = _maybe_int(value)
    if updated is None or updated <= 0:
        return None
    return max(0, now_ms - updated)


def _price_age_payload(price: dict[str, Any], now_ms: int) -> dict[str, int | None]:
    return {
        "chainlink": _age_ms_from_updated(now_ms, price.get("chainlink_updated_ms")),
        "okx": _age_ms_from_updated(now_ms, price.get("okx_updated_ms")),
        "binance": _age_ms_from_updated(
            now_ms,
            price.get("binance_market_updated_ms") or price.get("binance_updated_ms"),
        ),
    }


def _price_exchange_age_payload(price: dict[str, Any], now_ms: int) -> dict[str, int | None]:
    return {
        "okx": _age_ms_from_updated(now_ms, price.get("okx_exchange_updated_ms")),
        "binance": _age_ms_from_updated(now_ms, price.get("binance_market_exchange_updated_ms")),
    }


def _quote_age_payload(quotes: dict[str, dict[str, Any]], now_ms: int) -> dict[str, int | None]:
    return {
        side: _age_ms_from_updated(now_ms, (quotes.get(side) or {}).get("updated_at_ms"))
        for side in ("Up", "Down")
    }


def _compact_live_price(price: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": price.get("source"),
        "chainlink": _round_float(_maybe_float(price.get("chainlink")), 8),
        "okx": _round_float(_maybe_float(price.get("okx")), 8),
        "binance": _round_float(
            _maybe_float(price.get("binance_market")) or _maybe_float(price.get("binance")),
            8,
        ),
        "target_price": _round_float(_maybe_float(price.get("target_price")), 8),
    }


def _compact_live_quotes(quotes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    compact: dict[str, dict[str, Any]] = {}
    for side in ("Up", "Down"):
        quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
        compact[side] = {
            "source": quote.get("source"),
            "best_bid": _round_float(_maybe_float(quote.get("best_bid")), 4),
            "best_ask": _round_float(_maybe_float(quote.get("best_ask")), 4),
            "bid_size": _round_float(_maybe_float(quote.get("bid_size")), 6),
            "ask_size": _round_float(_maybe_float(quote.get("ask_size")), 6),
            "updated_at_ms": _maybe_int(quote.get("updated_at_ms")),
            "clob_received_ms": _maybe_int(quote.get("clob_received_ms")),
            "clob_event_updated_ms": _maybe_int(quote.get("clob_event_updated_ms")),
        }
    return compact


def _compact_price_selection(price_selection: dict[str, Any]) -> dict[str, Any]:
    basis_rows = price_selection.get("basis") if isinstance(price_selection.get("basis"), list) else []
    compact_basis = []
    for row in basis_rows:
        if not isinstance(row, dict):
            continue
        compact_basis.append(
            {
                "source": row.get("source"),
                "ready": bool(row.get("ready")),
                "reason": row.get("reason"),
                "age_ms": row.get("age_ms"),
                "price": row.get("price"),
                "adjusted_price": row.get("adjusted_price"),
                "samples": row.get("samples"),
            }
        )
    return {
        "blocked": bool(price_selection.get("blocked")),
        "message": str(price_selection.get("message") or "")[:300],
        "selected_source": price_selection.get("selected_source"),
        "selected_age_ms": price_selection.get("selected_age_ms"),
        "basis": compact_basis,
    }


def _live_gate_category(
    signal: dict[str, Any],
    price_selection: dict[str, Any],
    *,
    error: str | None,
) -> str:
    if error:
        return "live_error"
    reason = str(signal.get("reason") or "")
    if "缺少官方目标价" in reason or "priceToBeat" in reason:
        return "target_missing"
    if price_selection.get("blocked") or any(
        marker in reason
        for marker in (
            "价格过期",
            "缺少实时价",
            "Chainlink 不可用",
            "价格流过期",
            "fallback 选择无可用价格源",
        )
    ):
        return "price_source_stale_or_missing"
    if "盘口报价过期" in reason or "盘口接近过期" in reason:
        return "quote_stale"
    if "入场价格高于上限" in reason:
        return "entry_above_max"
    if "V11_REAL_GUARD BLOCK" in reason:
        return "v11_guard_block"
    if "V8 前置守卫" in reason:
        return "v8_guard_block"
    if signal.get("side") in {"Up", "Down"}:
        return "signal_ready"
    return "other_no_trade"


def _live_gate_window_summary(rows: list[dict[str, Any]], now: float, seconds: float) -> dict[str, Any]:
    window = [row for row in rows if now - float(row.get("at") or 0.0) <= seconds]
    categories = Counter(str(row.get("category") or "unknown") for row in window)
    price_age_rows = [row.get("price_ages_ms") for row in window if isinstance(row.get("price_ages_ms"), dict)]
    quote_age_rows = [row.get("quote_ages_ms") for row in window if isinstance(row.get("quote_ages_ms"), dict)]
    return {
        "seconds": seconds,
        "count": len(window),
        "categories": dict(categories),
        "okx_age_ms": _numeric_summary(row.get("okx") for row in price_age_rows),
        "binance_age_ms": _numeric_summary(row.get("binance") for row in price_age_rows),
        "chainlink_age_ms": _numeric_summary(row.get("chainlink") for row in price_age_rows),
        "quote_age_ms": _numeric_summary(
            max(age for age in (row.get("Up"), row.get("Down")) if age is not None)
            for row in quote_age_rows
            if row.get("Up") is not None or row.get("Down") is not None
        ),
        "duration_ms": _numeric_summary(row.get("duration_ms") for row in window),
    }


def _tick_profile_window_summary(rows: list[dict[str, Any]], now: float, seconds: float) -> dict[str, Any]:
    window = [row for row in rows if now - float(row.get("recorded_at") or 0.0) <= seconds]
    statuses = Counter(str(row.get("status") or "unknown") for row in window)
    return {
        "seconds": seconds,
        "count": len(window),
        "statuses": dict(statuses),
        "total_ms": _numeric_summary(row.get("total_ms") for row in window),
        "refresh_market_ms": _numeric_summary(row.get("refresh_market_ms") for row in window),
        "backend_market_data_snapshot_ms": _numeric_summary(
            row.get("backend_market_data_snapshot_ms") for row in window
        ),
        "strategy_and_live_ms": _numeric_summary(row.get("strategy_and_live_ms") for row in window),
    }


def _numeric_summary(values: Any) -> dict[str, Any]:
    numbers = sorted(float(value) for value in values if _maybe_float(value) is not None)
    if not numbers:
        return {"count": 0, "min": None, "p50": None, "max": None}
    return {
        "count": len(numbers),
        "min": round(numbers[0], 3),
        "p50": round(numbers[len(numbers) // 2], 3),
        "max": round(numbers[-1], 3),
    }


def _fallback_source_updated_ms(price: dict[str, Any], source: str) -> int | None:
    if source == "binance":
        return _maybe_int(price.get("binance_market_updated_ms")) or _maybe_int(price.get("binance_updated_ms"))
    if source == "okx":
        return _maybe_int(price.get("okx_updated_ms"))
    return _maybe_int(price.get(f"{source}_updated_ms"))


def _spot_exchange_updated_ms(price: dict[str, Any], source: str) -> int | None:
    if source == SPOT_WS_SOURCE_BINANCE:
        return _maybe_int(price.get("binance_market_exchange_updated_ms"))
    if source == SPOT_WS_SOURCE_OKX:
        return _maybe_int(price.get("okx_exchange_updated_ms"))
    return None


def _updated_at_seconds(value: Any, fallback: float) -> float:
    parsed = _maybe_int(value)
    if parsed is None or parsed <= 0:
        return fallback
    return parsed / 1000.0 if parsed > 10_000_000_000 else float(parsed)


def _round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _round_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _roi_pct(pnl: float | None, stake: float | None) -> float | None:
    if pnl is None or not stake:
        return None
    return _round_pct(pnl / stake * 100.0)


def _distance_bps(price: float | None, target: float | None) -> float | None:
    if price is None or target is None or target <= 0:
        return None
    return round((price - target) / target * 10_000.0, 4)


def _is_pending_settlement_trade(row: dict[str, Any], now: float) -> bool:
    if str(row.get("status") or "").upper() != "OPEN":
        return False
    ends_at = _maybe_float(row.get("ends_at"))
    if ends_at is None or ends_at > now:
        return False
    return row.get("settled_at") in (None, "") and row.get("outcome") in (None, "")


def _strategy_type(reason: Any) -> str:
    text = str(reason or "")
    if "PAIR_" in text:
        return "PAIR"
    return "SINGLE"


def _exit_note(reason: Any) -> str:
    text = str(reason or "")
    parts = [part.strip() for part in text.split("|")]
    return parts[-1] if len(parts) > 1 else ""


def _settlement_source_label(source: Any, row: dict[str, Any]) -> str:
    if row.get("settlement_pending"):
        return "等待官方结算"
    normalized = str(source or "").strip()
    if normalized == SETTLEMENT_SOURCE_POLYMARKET:
        return "Polymarket官方"
    if normalized == SETTLEMENT_SOURCE_CHAINLINK:
        return "Chainlink兜底"
    if normalized == SETTLEMENT_SOURCE_EARLY_EXIT:
        return "提前平仓"
    if str(row.get("status") or "") == "SETTLED" and row.get("outcome") in (None, ""):
        return "提前平仓"
    return normalized or "-"

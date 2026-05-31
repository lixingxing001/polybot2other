from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import replace
from typing import Any

from .actor_analysis import PolymarketDataClient, build_actor_analysis
from .clob_ws import ClobMarketWebSocketFeed, RtdsChainlinkWebSocketFeed
from .config import Settings, reload_live_credential_env
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
from .experiments import (
    ANTI_BOT_GUARD_MODE_NONE,
    MARKET_DATA_MODE_BASE,
    MARKET_DATA_MODE_MULTI_CONFIRM,
    MARKET_DATA_MODE_MULTI_LEAD,
    PRICE_SOURCE_MODE_MIXED,
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
    build_llm_market_features,
    route_execution_modes,
)
from .live import LIVE_COMBO, LIVE_VARIANT_ID, LiveStrategyRunner
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


LIVE_SNAPSHOT_MIN_INTERVAL_SECONDS = 0.5
BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS = 1.0
BACKEND_MARKET_DATA_REFRESH_RATIO = 0.5
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
                samples = self._basis.setdefault(source, [])
                if not source_price or not updated_ms:
                    continue
                age_ms = max(0, now_ms - updated_ms)
                if age_ms > self.settings_max_age_ms:
                    continue
                sample_key = (chainlink_updated_ms, updated_ms)
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
        return 3_000


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
        self._price_refresh_thread: threading.Thread | None = None
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
        self._last_live_snapshot_ingest_at = 0.0
        self._last_backend_market_data_refresh_at = 0.0
        self._last_backend_quote_refresh_at = 0.0
        self._last_backend_price_refresh_at = 0.0
        self._official_recheck_next_at: dict[str, float] = {}
        self._official_price_backfill_next_at: dict[str, float] = {}
        self.pair_strategy_enabled = False
        self.pair_stop_loss_streak = 0
        self.last_pair_event: dict[str, Any] | None = None
        self.single_entry_mode = SINGLE_ENTRY_MODE_LEGACY
        self.market_data_mode = MARKET_DATA_MODE_BASE
        self.price_source_mode = PRICE_SOURCE_MODE_MIXED
        self.anti_bot_guard_mode = ANTI_BOT_GUARD_MODE_NONE
        self.realtime_maker_enabled = False
        self.llm_super_agent_enabled = False
        self.llm_super_agent_router = LlmSuperAgentRouter(settings)
        self.llm_super_agent_variant_id = "MAIN"
        self._llm_super_agent_last_logged_key: str | None = None
        self.paper_trading_paused = False
        self.last_paper_pause_event: dict[str, Any] | None = None
        self.price_basis_tracker = PriceBasisTracker()
        self.clob_ws_feed = ClobMarketWebSocketFeed(timeout_seconds=settings.request_timeout_seconds)
        self.rtds_chainlink_feed = RtdsChainlinkWebSocketFeed(timeout_seconds=settings.request_timeout_seconds)
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
        }
        self.strategy_experiments = (
            StrategyExperimentRunner(settings, self.polymarket, self.price_fallback)
            if settings.strategy_experiments_enabled
            else None
        )
        self.live_trading = LiveStrategyRunner(settings, self.polymarket) if settings.live_trading_runtime_enabled else None

    def start(self) -> None:
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
        self._thread = threading.Thread(target=self._run, name="polybot2other-real-btc-paper-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._market_data_thread:
            self._market_data_thread.join(timeout=3)
        if self._clob_ws_thread:
            self._clob_ws_thread.join(timeout=3)
        if self._rtds_ws_thread:
            self._rtds_ws_thread.join(timeout=3)
        if self._price_refresh_thread:
            self._price_refresh_thread.join(timeout=3)

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

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.settings.tick_seconds)

    def _run_market_data(self) -> None:
        while not self._stop.is_set():
            self._refresh_backend_market_data_once()
            self._stop.wait(BACKEND_MARKET_DATA_REFRESH_MIN_SECONDS)

    def _run_clob_ws(self) -> None:
        self.clob_ws_feed.run(
            self._stop,
            self._clob_ws_market,
            self._ingest_backend_clob_ws_quotes,
            self._set_backend_clob_ws_status,
        )

    def _run_rtds_chainlink_ws(self) -> None:
        self.rtds_chainlink_feed.run(
            self._stop,
            self._ingest_backend_chainlink_price,
            self._set_backend_rtds_ws_status,
        )

    def _clob_ws_market(self) -> MarketRound | None:
        with self._lock:
            return self.current_market

    def _refresh_backend_market_data_once(self) -> None:
        now = time.time()
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
        try:
            market = self._refresh_market()
            if market is None:
                self._set_error("current_btc_5m_market_unavailable", now)
                return
            if self._backend_market_data_refresh_needed(now) and not self._market_data_loop_alive():
                self._backend_market_data_snapshot(market)
            self._settle_due(now)
            self._reconcile_official_settlements(now)
            self._backfill_official_final_prices(now)
            self._run_strategy_from_state()
            self.store.record_equity()
            if self.live_trading is not None:
                self.live_trading.store.record_equity()
            with self._lock:
                self.last_error = None
                self.last_tick_at = time.time()
        except Exception as exc:  # noqa: BLE001 - dashboard must keep running and expose the error.
            self._set_error(f"{type(exc).__name__}: {exc}", now)

    def ingest_live_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
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

        if (
            cached_market is not None
            and cached_market.ends_at > now
            and client_market.get("slug") == cached_market.round_id
        ):
            market = cached_market
        else:
            market = self._refresh_market()
            if market is None:
                with self._lock:
                    cached_market = self.current_market
                if (
                    cached_market is not None
                    and cached_market.ends_at > now
                    and client_market.get("slug") == cached_market.round_id
                ):
                    market = cached_market
                else:
                    raise RuntimeError("current BTC 5m market unavailable")
        if client_market.get("slug") and client_market.get("slug") != market.round_id:
            return {
                "ok": True,
                "ignored_snapshot": "stale_market",
                "market": market_to_payload(market),
                "updated_at": now,
            }

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
        now = time.time()
        ticks = self.market_data_price_fallback.fetch_sources("BTC", now)
        fallback_tick = ticks.get("coinbase") or ticks.get("binance") or ticks.get("okx")
        if fallback_tick is None:
            fallback_tick = self.market_data_price_fallback.fetch_symbol("BTC", now)
        now_ms = int(time.time() * 1000)
        with self._lock:
            price = dict(self.execution_price or self.paper_price or {})
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
        chainlink = _maybe_float(price.get("chainlink"))
        updated_ms = _maybe_int(price.get("chainlink_updated_ms"))
        if chainlink is None or chainlink <= 0 or not updated_ms:
            return
        now = time.time()
        with self._lock:
            market = self.current_market
            merged = dict(self.execution_price or self.paper_price or {})
        merged["chainlink"] = chainlink
        merged["chainlink_updated_ms"] = updated_ms
        merged["source"] = str(price.get("source") or "polymarket-rtds-chainlink")
        enriched = self._backend_price_payload(market, merged, int(now * 1000))
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
            self.latest_quotes = latest
            self.paper_quotes = paper
            self.execution_quotes = execution
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
        if not quotes.get("Up") or not quotes.get("Down"):
            return True
        updated_ms = max(
            _maybe_int(quotes.get("Up", {}).get("updated_at_ms")) or 0,
            _maybe_int(quotes.get("Down", {}).get("updated_at_ms")) or 0,
        )
        if not updated_ms:
            return True
        return now - updated_ms / 1000.0 > self._strategy_feed_refresh_age_seconds()

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
        latest_ms = max(
            _maybe_int(price.get("binance_updated_ms")) or 0,
            _maybe_int(price.get("binance_market_updated_ms")) or 0,
            _maybe_int(price.get("okx_updated_ms")) or 0,
        )
        if not latest_ms:
            return True
        return now - latest_ms / 1000.0 > self._strategy_feed_refresh_age_seconds()

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
                    self.store.reconcile_round_official_outcome(
                        round_id,
                        str(outcome),
                        now,
                        final_price=final_price,
                        target_price=target_price,
                    )
                    self._broadcast_official_resolution(round_id, str(outcome), now, final_price, target_price)
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

    def _run_strategy_from_state(self) -> None:
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
            self._run_strategy_experiments(market, price, quotes)
            self._run_live_strategy(market, price, quotes)
            return
        self._manage_resting_orders(market, quotes)
        if self.realtime_maker_enabled:
            self._run_realtime_maker_strategy_from_state(market, price, quotes)
            return
        if self.llm_super_agent_enabled:
            self._run_llm_super_agent_strategy_from_state(market, price, quotes)
            return
        if pair_enabled:
            self._run_pair_strategy_from_state(market, price, quotes)
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
        with self._lock:
            self.last_signal = {
                "symbol": signal.symbol,
                "side": signal.side,
                "confidence": signal.confidence,
                "entry_price": signal.entry_price,
                "move_bps": signal.move_bps,
            "reason": signal.reason,
            }
        self._maybe_place_trade(market, signal, quotes)
        self._run_strategy_experiments(market, price, quotes)
        self._run_live_strategy(market, price, quotes)

    def _run_strategy_experiments(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        if self.strategy_experiments is None:
            return
        self.strategy_experiments.run_from_state(market, price, quotes)

    def _run_live_strategy(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        try:
            if self.live_trading is not None:
                live_price, live_quotes, _source = self._execution_market_data()
                self.live_trading.run_from_state(market, live_price, live_quotes)
        except Exception as exc:  # noqa: BLE001 - 实盘错误必须暴露但不能阻塞 Paper 采样。
            if self.live_trading is not None:
                self.live_trading.last_error = f"{type(exc).__name__}: {exc}"

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
            with self._lock:
                self.last_signal = {
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "confidence": signal.confidence,
                    "entry_price": signal.entry_price,
                    "move_bps": signal.move_bps,
                    "reason": signal.reason,
                }
            self._maybe_place_trade(market, signal, quotes)
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
    ) -> None:
        if signal.side not in {"Up", "Down"}:
            return
        with self._lock:
            paper_paused = self.paper_trading_paused
        if paper_paused:
            self._append_last_signal_reason(PAPER_PAUSE_REASON)
            return
        if self.store.daily_realized_pnl() <= -abs(self.settings.max_daily_loss):
            return
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
            return
        if same_side_open:
            if single_entry_mode in {
                SINGLE_ENTRY_MODE_STRICT,
                SINGLE_ENTRY_MODE_REVERSAL,
                SINGLE_ENTRY_MODE_STOP_AND_FLIP,
            }:
                self._append_last_signal_reason(f"{single_entry_mode} 当前市场已有同方向持仓，跳过重复开仓")
            return
        if single_entry_mode == SINGLE_ENTRY_MODE_STRICT and round_open_rows:
            existing_sides = _side_list_text(row.get("side") for row in round_open_rows)
            self._append_last_signal_reason(f"{SINGLE_STRICT_MARKER} 当前市场已有 {existing_sides} 持仓，禁止反向开仓")
            return
        if self.store.active_paper_order_exists(market.round_id, signal.side):
            self._append_last_signal_reason("已有同方向挂单等待成交")
            return
        if single_entry_mode == SINGLE_ENTRY_MODE_STRICT and self.store.active_paper_order_exists_for_round(market.round_id):
            self._append_last_signal_reason(f"{SINGLE_STRICT_MARKER} 当前市场已有挂单，禁止再次开仓")
            return
        if single_entry_mode == SINGLE_ENTRY_MODE_STOP_AND_FLIP and self.store.active_paper_order_exists_for_round(market.round_id):
            self._append_last_signal_reason(f"{SINGLE_STOP_AND_FLIP_MARKER} 当前市场有活跃挂单，暂不止损反手")
            return
        account = self.store.account()
        stake = min(self.settings.stake_dollars, float(account["cash_balance"]))
        if stake < 0.1:
            return
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
                return
            exit_side = str(opposite_rows[0].get("side") or "")
            exit_quote = quotes.get(exit_side) if isinstance(quotes.get(exit_side), dict) else {}
            exit_quote = self._quote_with_bid(market, exit_side, exit_quote)
            exit_bid = _maybe_float(exit_quote.get("best_bid"))
            if exit_bid is None or exit_bid <= 0:
                self._append_last_signal_reason(f"{SINGLE_STOP_AND_FLIP_MARKER} 缺少 {exit_side} 买一价，保留旧仓")
                return
            close_shares = sum(_maybe_float(row.get("shares")) or 0.0 for row in opposite_rows)
            now = time.time()
            close_reason = f"{SINGLE_STOP_AND_FLIP_MARKER} 平旧仓后反手 {exit_side}->{signal.side}"
            closed = self._close_side_shares(opposite_rows, exit_side, close_shares, exit_bid, now, close_reason)
            if not closed:
                self._append_last_signal_reason(f"{SINGLE_STOP_AND_FLIP_MARKER} 旧仓平仓失败，取消反手开仓")
                return
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
            return
        if not trade_ids:
            self._append_last_signal_reason("执行结果未生成持仓")

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

    def snapshot(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            live_snapshot = self.live_trading.snapshot() if self.live_trading is not None else _disabled_live_snapshot()
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
                "last_error": self.last_error,
                "last_tick_at": self.last_tick_at,
                "last_signal": dict(self.last_signal or {}),
                "current_market": market_to_payload(self.current_market),
                "latest_price": dict(self.latest_price),
                "latest_quotes": dict(self.latest_quotes),
                "paper_price": dict(self.paper_price),
                "paper_quotes": _copy_quotes(self.paper_quotes),
                "execution_price": dict(self.execution_price),
                "execution_quotes": _copy_quotes(self.execution_quotes),
                "market_data_scope": {
                    "display": "browser_or_backend",
                    "paper": "backend_only",
                    "execution": "backend_only",
                },
                "ws_status": dict(self.ws_status),
                "pair_strategy": self._pair_strategy_runtime_locked(),
                "strategy_experiments": self.strategy_experiments_snapshot(),
                "live_trading": live_snapshot,
            }
            if self.live_trading is not None:
                live_snapshot["gate_status"] = self.live_trading.gate_status(
                    self.current_market,
                    dict(self.execution_price),
                    _copy_quotes(self.execution_quotes),
                    readiness=live_snapshot.get("readiness") if isinstance(live_snapshot.get("readiness"), dict) else None,
                    official_open_orders=(
                        live_snapshot.get("open_orders") if isinstance(live_snapshot.get("open_orders"), dict) else None
                    ),
                )
        if self.live_trading is not None:
            live_snapshot["open_trades"] = self._decorate_open_trades(self.live_trading.open_trades(), runtime)
            live_metrics = self._metrics_with_open_marks(
                self.live_trading.store.metrics(),
                live_snapshot["open_trades"],
            )
            live_variant = live_snapshot.get("variant") if isinstance(live_snapshot.get("variant"), dict) else None
            if live_variant is not None:
                live_variant["metrics"] = live_metrics
            live_variants = live_snapshot.get("variants") if isinstance(live_snapshot.get("variants"), list) else []
            for variant in live_variants:
                if isinstance(variant, dict) and variant.get("variant_id") == LIVE_VARIANT_ID:
                    variant["metrics"] = live_metrics
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
                "live_trading": self.live_trading.settings_payload() if self.live_trading is not None else _disabled_live_settings(),
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

    def strategy_experiment_detail(
        self,
        variant_id: str,
        trade_limit: int = 50,
        order_limit: int = 50,
    ) -> dict[str, Any]:
        if self.strategy_experiments is None:
            return {"enabled": False, "variant": None, "recent_trades": [], "recent_orders": []}
        return self.strategy_experiments.detail(variant_id, trade_limit, order_limit)

    def live_settings(self) -> dict[str, Any]:
        if self.live_trading is None:
            return _disabled_live_settings()
        return self.live_trading.settings_payload()

    def update_live_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        settings_payload = self.live_trading.update_settings(payload)
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
        return {"live_trading": settings_payload, "snapshot": self.snapshot()}

    def live_emergency_stop(self) -> dict[str, Any]:
        if self.live_trading is None:
            raise ValueError("live trading is disabled in this runtime")
        result = self.live_trading.emergency_stop()
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
            "purpose": "SINGLE_FAK_REAL live one-shot audit",
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
            page = self.live_trading.recent_trades_page(limit, offset, start_at, end_at)
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
            return self.live_trading.orders_page(limit, offset, status_key)
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
            return {
                "order_id": order_id,
                "fills": self.live_trading.store.paper_order_fills(order_id),
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
            return self.live_trading.equity_curve_window(days, max_points)
        raise ValueError("account_scope must be main, strategy_experiment, or live")

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
        self.variants = selected_strategy_variants(settings.strategy_experiments_variants)
        self._lock = threading.RLock()
        self._bots: dict[str, PaperTradingBot] = {}
        self._errors: dict[str, str | None] = {}
        self._official_broadcast_errors: dict[str, str | None] = {}
        self.run_count = 0
        self.last_run_at: float | None = None
        self.official_broadcast_count = 0
        self.last_official_broadcast_at: float | None = None
        for variant in self.variants:
            variant_settings = self._settings_for_variant(settings, variant)
            store = TradeStore(variant_settings.db_path, variant_settings.initial_balance)
            bot = PaperTradingBot(variant_settings, store)
            bot.polymarket = polymarket
            bot.price_fallback = price_fallback
            bot.pair_strategy_enabled = variant.strategy_family == STRATEGY_FAMILY_PAIR
            bot.realtime_maker_enabled = variant.strategy_family == STRATEGY_FAMILY_REALTIME_MAKER
            bot.llm_super_agent_enabled = variant.strategy_family == STRATEGY_FAMILY_LLM_SUPER_AGENT
            bot.llm_super_agent_variant_id = variant.variant_id
            bot.single_entry_mode = variant.single_entry_mode
            bot.market_data_mode = variant.market_data_mode
            bot.price_source_mode = variant.price_source_mode
            bot.anti_bot_guard_mode = variant.anti_bot_guard_mode
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
                with self._lock:
                    self._official_broadcast_errors[variant.variant_id] = None
            except Exception as exc:  # noqa: BLE001 - one experiment store must not block official broadcast to others.
                with self._lock:
                    self._official_broadcast_errors[variant.variant_id] = f"{type(exc).__name__}: {exc}"

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
        cleaned[side] = {
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
            "variant_id": LIVE_VARIANT_ID,
            "combo": LIVE_COMBO,
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


def _maybe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
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

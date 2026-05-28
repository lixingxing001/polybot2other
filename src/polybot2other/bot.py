from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

from .config import Settings
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
from .experiments import STRATEGY_FAMILY_PAIR, StrategyVariant, selected_strategy_variants
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
from .strategy import RealBtcFiveMinuteStrategy, input_from_snapshot


LIVE_SNAPSHOT_MIN_INTERVAL_SECONDS = 0.5
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
PAIR_ENTRY_COST_THRESHOLD = 0.92
PAIR_EXIT_BID_THRESHOLD = 0.98
PAIR_ENTRY_MIN_SECONDS_LEFT = 45
PAIR_RESIDUAL_REDUCE_SECONDS_LEFT = 45
PAIR_FORCE_FLATTEN_SECONDS_LEFT = 30
PAIR_RESIDUAL_STOP_LOSS_PCT = -20.0
PAIR_DAILY_LOSS_PCT = 3.0
PAIR_STOP_STREAK_LIMIT = 3
PAIR_EPSILON = 0.000001


class PaperTradingBot:
    def __init__(self, settings: Settings, store: TradeStore) -> None:
        self.settings = settings
        self.store = store
        self.polymarket = PolymarketClient(settings.gamma_url, settings.clob_url, settings.request_timeout_seconds)
        self.price_fallback = PublicPriceClient(settings.request_timeout_seconds)
        self.strategy = RealBtcFiveMinuteStrategy(settings)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.last_tick_at: float | None = None
        self.last_signal: dict[str, Any] | None = None
        self.current_market = None
        self.latest_price: dict[str, Any] = {}
        self.latest_quotes: dict[str, dict[str, Any]] = {}
        self._last_live_snapshot_ingest_at = 0.0
        self._official_recheck_next_at: dict[str, float] = {}
        self._official_price_backfill_next_at: dict[str, float] = {}
        self.pair_strategy_enabled = False
        self.pair_stop_loss_streak = 0
        self.last_pair_event: dict[str, Any] | None = None
        self.ws_status: dict[str, Any] = {
            "market": "waiting",
            "price": "waiting",
            "browser_feed_at": None,
            "backend_rest_fallback_at": None,
        }
        self.strategy_experiments = (
            StrategyExperimentRunner(settings, self.polymarket, self.price_fallback)
            if settings.strategy_experiments_enabled
            else None
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="polybot2other-real-btc-paper-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

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

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.settings.tick_seconds)

    def tick(self) -> None:
        now = time.time()
        try:
            market = self._refresh_market()
            if market is None:
                self._set_error("current_btc_5m_market_unavailable", now)
                return
            if self._live_feed_stale(now) or self._price_feed_stale(now):
                self._rest_fallback_snapshot(market)
            self._settle_due(now)
            self._reconcile_official_settlements(now)
            self._backfill_official_final_prices(now)
            self._run_strategy_from_state()
            self.store.record_equity()
            with self._lock:
                self.last_error = None
                self.last_tick_at = now
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
            return self.snapshot(extra={"throttled_snapshot": True})
        if cached_market is not None and client_market.get("slug") == cached_market.round_id:
            with self._lock:
                self._last_live_snapshot_ingest_at = now

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
            return self.snapshot(extra={"ignored_snapshot": "stale_market"})

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
        chainlink_price = _maybe_float(price.get("chainlink"))
        binance_price = _maybe_float(price.get("binance"))
        if chainlink_price:
            self.store.save_price_tick("BTC", chainlink_price, "polymarket-rtds-chainlink", now)
        elif binance_price:
            self.store.save_price_tick("BTC", binance_price, "polymarket-rtds-binance", now)
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
            }
            self._last_live_snapshot_ingest_at = now
        self._settle_due(now)
        self._reconcile_official_settlements(now)
        self._backfill_official_final_prices(now)
        self._run_strategy_from_state()
        return self.snapshot()

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
        quotes = self.polymarket.get_quotes(market)
        now = time.time()
        tick = self.price_fallback.fetch_symbol("BTC", now)
        now_ms = int(time.time() * 1000)
        price = {
            "chainlink": None,
            "binance": tick.price,
            "binance_updated_ms": now_ms,
            "source": f"{tick.source}-rest-fallback",
        }
        if market.target_price > 0:
            price["target_price"] = market.target_price
            price["target_price_source"] = "market.target_price"
            price["target_price_fallback"] = False
        with self._lock:
            self.latest_quotes = {side: quote.to_dict() for side, quote in quotes.items()}
            self.latest_price = price
            self.ws_status["backend_rest_fallback_at"] = time.time()
            fed_at = self.ws_status.get("browser_feed_at")
            if not fed_at or now - float(fed_at) > self.settings.live_snapshot_max_age_seconds:
                self.ws_status["market"] = "rest-fallback"
                self.ws_status["price"] = "rest-fallback"
            else:
                self.ws_status["price"] = "rest-fallback"
        self.store.save_price_tick("BTC", tick.price, tick.source, time.time())

    def _live_feed_stale(self, now: float) -> bool:
        with self._lock:
            fed_at = self.ws_status.get("browser_feed_at")
        return not fed_at or now - float(fed_at) > self.settings.live_snapshot_max_age_seconds

    def _price_feed_stale(self, now: float) -> bool:
        with self._lock:
            price = dict(self.latest_price)
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
            price = dict(self.latest_price)
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
        if self.strategy_experiments is None:
            return
        self.strategy_experiments.apply_official_resolution(
            round_id,
            outcome,
            now,
            final_price=final_price,
            target_price=target_price,
        )

    def _run_strategy_from_state(self) -> None:
        with self._lock:
            market = self.current_market
            price = dict(self.latest_price)
            quotes = dict(self.latest_quotes)
            pair_enabled = self.pair_strategy_enabled
        if market is None:
            return
        self._manage_resting_orders(market, quotes)
        if pair_enabled:
            self._run_pair_strategy_from_state(market, price, quotes)
            self._run_strategy_experiments(market, price, quotes)
            return
        payload = {"price": price, "quotes": quotes}
        signal = self.strategy.signal(input_from_snapshot(market, payload))
        with self._lock:
            self.last_signal = {
                "symbol": signal.symbol,
                "side": signal.side,
                "confidence": signal.confidence,
                "entry_price": signal.entry_price,
                "move_bps": signal.move_bps,
                "reason": signal.reason,
            }
        self._maybe_place_trade(market, signal)
        self._run_strategy_experiments(market, price, quotes)

    def _run_strategy_experiments(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        if self.strategy_experiments is None:
            return
        self.strategy_experiments.run_from_state(market, price, quotes)

    def _maybe_place_trade(self, market, signal) -> None:
        if signal.side not in {"Up", "Down"}:
            return
        if self.store.daily_realized_pnl() <= -abs(self.settings.max_daily_loss):
            return
        if self.store.open_trade_count("BTC") >= self.settings.max_open_trades:
            return
        if self.store.open_trade_exists(market.round_id, signal.side):
            return
        if self.store.active_paper_order_exists(market.round_id, signal.side):
            self._append_last_signal_reason("已有同方向挂单等待成交")
            return
        account = self.store.account()
        stake = min(self.settings.stake_dollars, float(account["cash_balance"]))
        if stake < 0.1:
            return
        with self._lock:
            quotes = dict(self.latest_quotes)
        quote = quotes.get(signal.side) if isinstance(quotes.get(signal.side), dict) else {}
        quote = self._quote_with_depth(market, signal.side, quote)
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
            fill = self._resting_order_fill(order, quote)
            if not fill:
                continue
            self.store.fill_resting_order(order, now=now, **fill)

    def _resting_order_fill(self, order: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any] | None:
        limit_price = _maybe_float(order.get("limit_price"))
        remaining_cash = _maybe_float(order.get("remaining_cash")) or 0.0
        if limit_price is None or limit_price <= 0 or remaining_cash <= 0:
            return None
        levels = [level for level in ask_levels_from_quote(quote) if level.price <= limit_price + PAIR_EPSILON]
        if not levels:
            return None
        available_shares = round(sum(level.size for level in levels), 6)
        shares = round(min(available_shares, remaining_cash / limit_price), 6)
        if shares <= PAIR_EPSILON:
            return None
        notional = round(shares * limit_price, 6)
        return {
            "fill_price": limit_price,
            "shares": shares,
            "notional": notional,
            "fee": 0.0,
            "cash_spent": notional,
            "level_price": levels[0].price,
            "reason": (
                f"RESTING_FILL maker fill {shares:.6f} @ {limit_price:.4f}, "
                f"trigger_ask {levels[0].price:.4f}, fee 0.000000"
            ),
        }

    def _run_pair_strategy_from_state(self, market: MarketRound, price: dict[str, Any], quotes: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        quotes = self._quotes_with_depth(market, quotes)
        state = _pair_quote_state(quotes, now)
        managed = self._manage_pair_positions(market, price, state, now)
        if managed:
            reason = str(self.last_pair_event.get("message")) if self.last_pair_event else "配对策略持仓管理中"
            self._set_last_pair_signal("PAIR_MANAGE", state, reason)
            return
        open_rows = [row for row in self.store.open_trades() if row["symbol"] == "BTC" and row["round_id"] == market.round_id]
        if open_rows:
            self._set_last_pair_signal("PAIR_MANAGE", state, "配对策略持仓管理中")
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
            return "配对策略日内回撤达到 3%，停止开新仓"
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
        up_signal = Signal("BTC", "Up", confidence, up_limit, 0.0, reason)
        down_signal = Signal("BTC", "Down", confidence, down_limit, 0.0, reason)
        up_intent = TradeIntent(market, up_signal, up_cash)
        down_intent = TradeIntent(market, down_signal, down_cash)
        expires_at = self._resting_order_expires_at(market, order_type)
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
        confirmed = _price_confirms_residual(side, price, market.target_price)
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
            if not row.get("asks"):
                merged[side] = fresh
        with self._lock:
            latest = dict(self.latest_quotes)
            latest.update(fresh_quotes)
            self.latest_quotes = latest
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

    def _resting_order_expires_at(self, market: MarketRound, order_type: str) -> float:
        if order_type == ORDER_TYPE_GTD:
            return min(market.ends_at, time.time() + self.settings.paper_gtd_seconds)
        return market.ends_at

    def _append_last_signal_reason(self, reason: str) -> None:
        with self._lock:
            signal = dict(self.last_signal or {})
            existing = str(signal.get("reason") or "")
            signal["reason"] = f"{existing} | {reason}" if existing else reason
            self.last_signal = signal

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
            runtime = {
                "paper_only": True,
                "running": bool(self._thread and self._thread.is_alive()),
                "last_error": self.last_error,
                "last_tick_at": self.last_tick_at,
                "last_signal": dict(self.last_signal or {}),
                "current_market": market_to_payload(self.current_market),
                "latest_price": dict(self.latest_price),
                "latest_quotes": dict(self.latest_quotes),
                "ws_status": dict(self.ws_status),
                "pair_strategy": self._pair_strategy_runtime_locked(),
                "strategy_experiments": self.strategy_experiments_snapshot(),
            }
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
                "pair_strategy": {
                    "entry_cost_threshold": PAIR_ENTRY_COST_THRESHOLD,
                    "exit_bid_threshold": PAIR_EXIT_BID_THRESHOLD,
                    "entry_min_seconds_left": PAIR_ENTRY_MIN_SECONDS_LEFT,
                    "residual_reduce_seconds_left": PAIR_RESIDUAL_REDUCE_SECONDS_LEFT,
                    "force_flatten_seconds_left": PAIR_FORCE_FLATTEN_SECONDS_LEFT,
                    "residual_stop_loss_pct": PAIR_RESIDUAL_STOP_LOSS_PCT,
                    "daily_loss_pct": PAIR_DAILY_LOSS_PCT,
                    "stop_streak_limit": PAIR_STOP_STREAK_LIMIT,
                },
                "paper_only": True,
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
    ) -> dict[str, Any]:
        limit = max(1, min(RECENT_TRADES_MAX_LIMIT, int(limit)))
        offset = max(0, int(offset))
        if start_at is not None and end_at is not None and end_at < start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
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

    def orders_page(self, limit: int = ORDERS_DEFAULT_LIMIT, offset: int = 0, status_filter: str = "all") -> dict[str, Any]:
        limit = max(1, min(ORDERS_MAX_LIMIT, int(limit)))
        offset = max(0, int(offset))
        status_key = normalize_paper_order_status_filter(status_filter)
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

    def order_fills(self, order_id: int) -> dict[str, Any]:
        order_id = max(1, int(order_id))
        return {
            "order_id": order_id,
            "fills": self.store.paper_order_fills(order_id),
        }

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
    ) -> dict[str, Any]:
        days = max(1, min(365, int(days)))
        max_points = max(2, min(EQUITY_CURVE_MAX_POINTS, int(max_points)))
        rows = self.store.equity_curve_window(days, max_points)
        return {
            "equity_curve": rows,
            "equity_curve_meta": {
                "days": days,
                "max_points": max_points,
                "points": len(rows),
                "initial_balance": self.settings.initial_balance,
            },
        }

    def _pair_strategy_runtime_locked(self) -> dict[str, Any]:
        state = _pair_quote_state(self.latest_quotes, time.time())
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
        decorated: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["strategy_type"] = _strategy_type(row.get("reason"))
            item["exit_note"] = _exit_note(row.get("reason"))
            item["max_payout"] = _round_money(_maybe_float(row.get("shares")))
            item["max_profit"] = _round_money((_maybe_float(row.get("shares")) or 0.0) - (_maybe_float(row.get("stake")) or 0.0))
            item["max_loss"] = _round_money(_maybe_float(row.get("stake")) or 0.0)
            item["entry_probability_pct"] = _round_pct((_maybe_float(row.get("entry_price")) or 0.0) * 100.0)
            item["current_price"] = current_price
            item["current_distance_bps"] = _distance_bps(current_price, _maybe_float(row.get("target_price")))
            if current_market.get("round_id") == row.get("round_id"):
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
            item["settlement_source_label"] = _settlement_source_label(row.get("settlement_source"), row)
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
            self._bots[variant.variant_id] = bot
            self._errors[variant.variant_id] = None
            self._official_broadcast_errors[variant.variant_id] = None

    def _settings_for_variant(self, settings: Settings, variant: StrategyVariant) -> Settings:
        db_path = settings.strategy_experiments_db_dir / f"{variant.variant_id.lower()}.sqlite3"
        max_open_trades = max(settings.max_open_trades, 2) if variant.strategy_family == STRATEGY_FAMILY_PAIR else settings.max_open_trades
        return replace(
            settings,
            db_path=db_path,
            paper_entry_order_type=variant.order_type,
            max_open_trades=max_open_trades,
            strategy_experiments_enabled=False,
            strategy_experiments_variants="",
        )

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
        normalized = str(variant_id or "").strip().upper().replace("-", "_")
        by_id = {variant.variant_id: variant for variant in self.variants}
        variant = by_id.get(normalized)
        if variant is None:
            allowed = ", ".join(sorted(by_id))
            raise ValueError(f"variant_id must be one of {allowed}")
        bot = self._bots[variant.variant_id]
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
        metrics = bot.store.metrics()
        trade_summary = bot.store.recent_trade_summary("BTC", start_at, end_at)
        return {
            "variant_id": variant.variant_id,
            "combo": variant.combo,
            "strategy_family": variant.strategy_family,
            "order_type": variant.order_type,
            "target_code_completion": variant.target_code_completion,
            "target_report_alignment": variant.target_report_alignment,
            "role": variant.role,
            "db_path": str(bot.settings.db_path),
            "pair_strategy_enabled": bot.pair_strategy_enabled,
            "last_signal": dict(bot.last_signal or {}),
            "last_error": last_error,
            "official_broadcast_error": official_broadcast_error,
            "active_orders": len(bot.store.active_paper_orders("BTC")),
            "window": {"start_at": start_at, "end_at": end_at},
            "review_score": _experiment_review_score(trade_summary, order_summary, last_error, official_broadcast_error),
            "order_summary": order_summary,
            "metrics": metrics,
            "recent_trades_summary": trade_summary,
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
            store.save_price_tick("BTC", chainlink_price, "strategy-experiment-chainlink", now)
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


def _variant_tags(variant: StrategyVariant) -> dict[str, Any]:
    return {
        "variant_id": variant.variant_id,
        "combo": variant.combo,
        "strategy_family": variant.strategy_family,
        "experiment_order_type": variant.order_type,
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


def _price_confirms_residual(side: str, price: dict[str, Any], target_price: float) -> bool:
    chainlink = _maybe_float(price.get("chainlink"))
    if chainlink is None or target_price <= 0:
        return False
    if side == "Up":
        return chainlink >= target_price
    if side == "Down":
        return chainlink <= target_price
    return False


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

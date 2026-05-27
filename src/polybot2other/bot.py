from __future__ import annotations

import threading
import time
from typing import Any

from .config import Settings
from .market import PublicPriceClient
from .models import MarketRound, Signal, TradeIntent
from .polymarket import PolymarketClient, market_to_payload
from .storage import TradeStore
from .strategy import RealBtcFiveMinuteStrategy, input_from_snapshot


LIVE_SNAPSHOT_MIN_INTERVAL_SECONDS = 0.5
RECENT_TRADES_DEFAULT_LIMIT = 100
RECENT_TRADES_MAX_LIMIT = 500
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
        self.pair_strategy_enabled = False
        self.pair_stop_loss_streak = 0
        self.last_pair_event: dict[str, Any] | None = None
        self.ws_status: dict[str, Any] = {
            "market": "waiting",
            "price": "waiting",
            "browser_feed_at": None,
            "backend_rest_fallback_at": None,
        }

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
            self.latest_quotes = _clean_quotes(quotes)
            self.ws_status = {
                "market": str(payload.get("market_ws_status") or "browser"),
                "price": str(payload.get("price_ws_status") or "browser"),
                "browser_feed_at": now,
                "backend_rest_fallback_at": self.ws_status.get("backend_rest_fallback_at"),
            }
            self._last_live_snapshot_ingest_at = now
        self._settle_due(now)
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
                self.store.settle_round_outcome(slug, str(resolution["outcome"]), now)
                continue
        with self._lock:
            price = dict(self.latest_price)
        chainlink_price = _maybe_float(price.get("chainlink"))
        if chainlink_price:
            self.store.settle_due_rounds({"BTC": chainlink_price}, now)

    def _run_strategy_from_state(self) -> None:
        with self._lock:
            market = self.current_market
            price = dict(self.latest_price)
            quotes = dict(self.latest_quotes)
            pair_enabled = self.pair_strategy_enabled
        if market is None:
            return
        if pair_enabled:
            self._run_pair_strategy_from_state(market, price, quotes)
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

    def _maybe_place_trade(self, market, signal) -> None:
        if signal.side not in {"Up", "Down"}:
            return
        if self.store.daily_realized_pnl() <= -abs(self.settings.max_daily_loss):
            return
        if self.store.open_trade_count("BTC") >= self.settings.max_open_trades:
            return
        if self.store.open_trade_exists(market.round_id, signal.side):
            return
        account = self.store.account()
        stake = min(self.settings.stake_dollars, float(account["cash_balance"]))
        if stake < 0.1:
            return
        self.store.place_trade(TradeIntent(market=market, signal=signal, stake_dollars=stake))

    def _run_pair_strategy_from_state(self, market: MarketRound, price: dict[str, Any], quotes: dict[str, dict[str, Any]]) -> None:
        now = time.time()
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
        self._place_pair_trade(market, state, now)

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
        if pair_cost > PAIR_ENTRY_COST_THRESHOLD:
            return f"配对合成成本 {pair_cost:.4f} 高于 {PAIR_ENTRY_COST_THRESHOLD:.2f}"
        if self.store.open_trade_count("BTC") > self.settings.max_open_trades - 2:
            return "配对策略可用持仓槽不足"
        if self.store.open_trade_exists_for_round(market.round_id):
            return "当前市场已有持仓，等待处理完成"
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

    def _place_pair_trade(self, market: MarketRound, state: dict[str, Any], now: float) -> None:
        pair_cost = _maybe_float(state.get("pair_cost"))
        up_ask = _maybe_float(state.get("up_ask"))
        down_ask = _maybe_float(state.get("down_ask"))
        if pair_cost is None or up_ask is None or down_ask is None or pair_cost <= 0:
            return
        account = self.store.account()
        total_budget = min(self.settings.stake_dollars, float(account["cash_balance"]))
        up_ask_size = _maybe_float(state.get("up_ask_size"))
        down_ask_size = _maybe_float(state.get("down_ask_size"))
        max_quote_shares = min(
            up_ask_size if up_ask_size is not None else total_budget / pair_cost,
            down_ask_size if down_ask_size is not None else total_budget / pair_cost,
        )
        shares = round(min(total_budget / pair_cost, max_quote_shares), 6)
        if shares < self.settings.min_ask_size:
            self._set_last_pair_signal("PAIR_WAIT", state, "配对策略可成交份额不足")
            return
        up_stake = round(shares * up_ask, 6)
        down_stake = round(shares * down_ask, 6)
        edge = 1.0 - pair_cost
        reason = (
            f"PAIR_OPEN 双边配对: ask_up {up_ask:.4f}, ask_down {down_ask:.4f}, "
            f"cost {pair_cost:.4f}, edge {edge:.4f}, shares {shares:.6f}"
        )
        confidence = round(max(0.0, min(1.0, edge)), 4)
        self.store.place_trades(
            [
                TradeIntent(market, Signal("BTC", "Up", confidence, up_ask, 0.0, reason), up_stake),
                TradeIntent(market, Signal("BTC", "Down", confidence, down_ask, 0.0, reason), down_stake),
            ]
        )
        self.pair_stop_loss_streak = 0
        self._set_pair_event(
            "PAIR_OPEN",
            f"配对开仓 cost={pair_cost:.4f}, shares={shares:.6f}",
            now,
            {"pair_cost": pair_cost, "shares": shares, "stake": round(up_stake + down_stake, 6)},
        )
        self._set_last_pair_signal("PAIR_OPEN", state, "配对策略已开双边仓")

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
        return self.store.close_all_open_trades_for_round(round_id, exit_prices, now, reason)

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
            item = self.store.close_trade_shares(int(row["id"]), close_shares, exit_price, now, reason)
            if item:
                closed.append(item)
            remaining = round(remaining - close_shares, 6)
        return closed

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
            }
        open_trades = self._decorate_open_trades(
            [row for row in self.store.open_trades() if row["symbol"] == "BTC"],
            runtime,
        )
        recent_page = self.recent_trades_page(RECENT_TRADES_DEFAULT_LIMIT, 0)
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
            "equity_curve": self.store.equity_curve(120),
        }
        if extra:
            payload.update(extra)
        return payload

    def recent_trades_page(self, limit: int = RECENT_TRADES_DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(RECENT_TRADES_MAX_LIMIT, int(limit)))
        offset = max(0, int(offset))
        total = self.store.recent_trade_count("BTC")
        rows = self._decorate_recent_trades(self.store.recent_trades(limit, offset, "BTC"))
        loaded = min(total, offset + len(rows))
        return {
            "recent_trades": rows,
            "recent_trades_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
            },
        }

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
            "updated_at_ms": _maybe_int(row.get("updated_at_ms")) or int(time.time() * 1000),
            "source": str(row.get("source") or "browser-ws"),
        }
    return cleaned


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

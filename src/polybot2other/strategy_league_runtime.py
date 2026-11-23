"""Run the prospective Paper league against one shared BTC feed and public market rules."""
from concurrent.futures import ThreadPoolExecutor
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time

from .forecast_paper import ForecastPaperPolicy
from .paper_execution import checked_info
from .strategy_league import LeagueEngine


logger = logging.getLogger(__name__)


class StrategyLeagueRunner:
    def __init__(self, owner, manager):
        self.owner, self.manager = owner, manager
        self.path = Path(owner.store.db_path).resolve().parent / "strategy-league.sqlite3"
        self.forecast = ForecastPaperPolicy(manager.feed.references)
        self.stop_event = threading.Event()
        self.thread = None
        self.status = {"state": "starting", "last_step_at": None, "round_id": None}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="btc-paper-strategy-league", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=6)

    def health(self):
        return dict(self.status)

    def _public_rules(self, market):
        exchange = self.manager.exchange
        info = checked_info(exchange.market_info(market))
        rules = {}
        for side, token in (("Up", market.up_token), ("Down", market.down_token)):
            try:
                book = exchange.rest_book(token)
                minimum = float(book.get("min_order_size") or 0)
                if (book["token_id"] == token and book["tick_size"] == info["tick_size"]
                        and math.isfinite(minimum) and minimum > 0):
                    rules[side] = {"token_id": token, "tick_size": book["tick_size"], "min_order_size": minimum}
            except (KeyError, TypeError, ValueError, OSError):
                # A thin or old book may block one side. Recheck its public rules on the next retry.
                continue
        return market.round_id, info, rules

    def _resolution(self, rid):
        shared = self.owner.store.get_round(rid) or {}
        if shared.get("settlement_source") == "polymarket_official" and shared.get("outcome") in ("Up", "Down"):
            return shared["outcome"]
        result = self.owner.polymarket.get_resolution(rid) or {}
        if result.get("market_slug") == rid and result.get("price_source") == "Gamma:outcomePrices":
            return result.get("outcome")

    def _run(self):
        # Keep the execution loop independent of REST latency and the nine existing Paper accounts.
        engine = None
        try:
            while not self.stop_event.is_set():
                try:
                    engine = LeagueEngine(self.path, start_at=math.ceil(time.time() / 300) * 300)
                    break
                except (OSError, sqlite3.OperationalError) as exc:
                    self.status = {**self.status, "state": "retrying", "last_error": type(exc).__name__}
                    self.stop_event.wait(5)
            if engine is None:
                return
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="paper-league-public") as pool:
                self._observe(engine, pool)
        except Exception as exc:
            self.status = {**self.status, "state": "error", "last_error": type(exc).__name__}
            logger.exception("Paper league worker stopped")
        finally:
            if engine is not None:
                try:
                    engine.cancel_pending(time.time())
                finally:
                    engine.close()
            if self.stop_event.is_set():
                self.status = {**self.status, "state": "stopped"}

    def _observe(self, engine, pool):
        rid = None
        info, rules = {}, {}
        refresh_at = valid_until = resolution_at = 0.
        metadata = resolution = None
        retry_resolution = {}
        paused = False
        while not self.stop_event.is_set():
            now = time.time()
            try:
                if resolution is not None and resolution.done():
                    try:
                        outcome = resolution.result()
                    finally:
                        resolution = None
                    if outcome in ("Up", "Down"):
                        engine.settle(resolving, outcome, now, source="polymarket_official")
                        retry_resolution.pop(resolving, None)
                if resolution is None and now >= resolution_at:
                    resolution_at = now + 10
                    due = list(engine.pending_rounds(now))
                    if due:
                        resolving = min(due, key=lambda key: retry_resolution.get(key, 0))
                        if retry_resolution.get(resolving, 0) <= now:
                            retry_resolution[resolving] = now + 45
                            resolution = pool.submit(self._resolution, resolving)
                if self.owner.btc_runtime_is_paused() or self.owner.paper_trading_paused:
                    if not paused:
                        engine.cancel_pending(now)
                    paused = True
                    self.status = {**self.status, "state": "paused"}
                    self.stop_event.wait(1)
                    continue
                paused = False
                frame = self.manager.market_input_snapshot()
                current = frame.market.round_id if frame.market else None
                if rid != current:
                    rid, info, rules = current, {}, {}
                    refresh_at = valid_until = 0.
                if metadata is not None and metadata.done():
                    try:
                        rule_round, fetched_info, fetched_rules = metadata.result()
                    finally:
                        metadata = None
                    if rule_round == rid:
                        info, rules = fetched_info, fetched_rules
                        valid_until = now + 65
                        refresh_at = now + (40 if len(rules) == 2 else 4)
                if metadata is None and frame.market and now >= refresh_at:
                    refresh_at = now + 5
                    metadata = pool.submit(self._public_rules, frame.market)
                if now > valid_until:
                    info, rules = {}, {}
                decision = self.forecast.evaluate(frame, info)
                engine.step(frame, decision, info, rules)
                self.status = {"state": "collecting" if now >= engine.starts_at else "waiting",
                               "starts_at": engine.starts_at, "last_step_at": now, "round_id": rid,
                               "model_ready": decision.up_score is not None, "rule_sides": sorted(rules)}
            except Exception as exc:
                if self.status.get("last_error") != type(exc).__name__:
                    logger.warning("Paper league will retry (%s)", type(exc).__name__, exc_info=True)
                self.status = {**self.status, "state": "retrying", "last_error": type(exc).__name__}
            self.stop_event.wait(0.8)

"""Matched-entry Paper experiment. The original main database is opened read-only."""
from dataclasses import fields
import json
from pathlib import Path
import sqlite3
import threading
import time

from .bot import PaperTradingBot
from .models import MarketRound, Signal, PaperFill, PaperFillLevel
from .optimized_main import OptimizedMainBot
from .main_exit_policy import ExitPolicy, checked_book, exit_signal, sweep_sell

VARIANT_ID = "MAIN_TP_SL"


def market_from_row(row):
    names = {f.name for f in fields(MarketRound)}
    return MarketRound(**{k: v for k, v in dict(row).items() if k in names and v is not None})


class MainExitBot(PaperTradingBot):
    independent_runtime = True
    # Reuse the existing ordered, full-depth public CLOB reader without changing it.
    _book_loop = OptimizedMainBot._book_loop

    def __init__(self, settings, store, baseline_path):
        if settings.live_trading_runtime_enabled or settings.strategy_experiments_enabled:
            raise ValueError("Exit experiment must be isolated and Paper-only")
        if Path(baseline_path).resolve() == Path(settings.db_path).resolve():
            raise ValueError("Exit experiment cannot write the main database")
        super().__init__(settings, store)
        self.baseline_path = Path(baseline_path).resolve()
        self.baseline = None
        self._baseline_lock = threading.RLock()
        self.policy = ExitPolicy(fee_rate=settings.paper_taker_fee_rate)
        self._book_thread = self._resolution_thread = self._resume_thread = None
        self._book_ws = None
        self.book_state = "waiting"
        self._lifecycle_lock = threading.RLock()
        self._decision_lock = threading.RLock()
        self._desired_running = False
        self._pending = {}
        self._retry_after = {}
        self._last_observation = 0.0
        self._journal_lock = threading.Lock()
        self.journal = settings.db_path.with_suffix(".observations.jsonl")
        self.last_signal = {"side": "NO_TRADE", "reason": "等待主账户新成交；原账户不受影响"}
        self._connect_baseline()

    def _connect_baseline(self):
        with self._baseline_lock:
            if self.baseline is not None:
                return True
            conn = None
            try:
                conn = sqlite3.connect(self.baseline_path.as_uri() + "?mode=ro", uri=True,
                                       timeout=.2, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                cursor = conn.execute("SELECT coalesce(max(id),0) FROM trades").fetchone()[0]
            except sqlite3.Error:
                if conn is not None:
                    conn.close()
                self.last_error = "等待原主账户数据库"
                return False
            run = self.store.initialize_run(self.baseline_path, cursor, self.policy)
            self.policy = ExitPolicy(**json.loads(run["policy_json"]))
            self.baseline = conn
            return True

    def _audit(self, event, **payload):
        row = {"policy_version": self.policy.version, "observed_at": time.time(), "event": event, **payload}
        with self._journal_lock:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    def start(self):
        with self._lifecycle_lock:
            self._desired_running = True
            if self._resume_thread and self._resume_thread.is_alive():
                return
            workers = (self._thread, self._book_thread, self._resolution_thread)
            if any(w and w.is_alive() for w in workers):
                if not self._stop.is_set():
                    return
                self._resume_thread = threading.Thread(target=self._resume_after_pause, args=(workers,), daemon=True)
                self._resume_thread.start()
                return
            self._start_workers()

    def _resume_after_pause(self, workers):
        for worker in workers:
            if worker and worker is not threading.current_thread():
                worker.join()
        with self._lifecycle_lock:
            self._resume_thread = None
            if self._desired_running:
                self._start_workers()

    def _start_workers(self):
        self._stop.clear()
        self.btc_runtime_paused = False
        with self._lock:
            self.paper_quotes = {}; self.latest_quotes = {}; self.execution_quotes = {}
        self._pending.clear()
        self._book_thread = (None if self._shared_inputs_required else
                             threading.Thread(target=self._book_loop, name="main-exit-clob", daemon=True))
        self._resolution_thread = threading.Thread(target=self._resolution_loop, name="main-exit-resolution", daemon=True)
        self._thread = threading.Thread(target=self._exit_loop, name="main-exit-paper", daemon=True)
        if self._book_thread:self._book_thread.start()
        self._resolution_thread.start(); self._thread.start()

    def request_pause(self):
        with self._lifecycle_lock:
            self._desired_running = False
            self.btc_runtime_paused = True
            self._stop.set()
        if self._book_ws:
            try:
                self._book_ws.close()
            except OSError:
                pass

    def stop(self):
        self.request_pause()
        for worker in (self._thread, self._book_thread, self._resolution_thread):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=3)

    def _set_paper_pause_state(self, paused, now):
        # Serialize pause with fills; an old intent never executes after resume.
        with self._decision_lock:
            super()._set_paper_pause_state(paused, now)
            if paused:
                self._pending.clear()

    def _exit_loop(self):
        while not self._stop.is_set():
            try:
                self.exit_tick()
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self.last_signal = {"side": "NO_TRADE", "reason": "止盈止损账户暂时异常，等待恢复"}
            self.last_tick_at = time.time()
            self._stop.wait(.25)

    def _sync_market(self, now):
        if self._shared_inputs_required:
            self._sync_shared_market_inputs();self.book_state="shared-live";return
        slug = "btc-updown-5m-" + str(int(now) // 300 * 300)
        with self._baseline_lock:
            row = self.baseline.execute("SELECT * FROM market_rounds WHERE round_id=?", (slug,)).fetchone()
        if row is None or not row["up_token"] or not row["down_token"]:
            return
        with self._lock:
            if self.current_market and self.current_market.round_id == slug:
                return
            self.current_market = market_from_row(row)
            self.paper_quotes = {}; self.execution_quotes = {}; self.latest_quotes = {}
        self.store.upsert_round(self.current_market)

    def _sync_entries(self, now, *, live_clock=False):
        run = self.store.run()
        with self._baseline_lock:
            parents = [dict(x) for x in self.baseline.execute(
                "SELECT * FROM trades WHERE id>? ORDER BY id LIMIT 100", (run["cursor"],))]
        for parent in parents:
            if parent["opened_at"] < run["started_at"]:
                self.store.advance_cursor(parent["id"])
                continue
            with self._baseline_lock:
                row = self.baseline.execute("SELECT * FROM market_rounds WHERE round_id=?", (parent["round_id"],)).fetchone()
                orders = [dict(x) for x in self.baseline.execute("SELECT * FROM paper_orders WHERE trade_id=? AND filled_shares>0", (parent["id"],))]
                levels = [dict(x) for x in self.baseline.execute("SELECT * FROM paper_fills WHERE trade_id=? ORDER BY id", (parent["id"],))]
            if live_clock:
                now = time.time()
            skip = None
            fill = None
            if self.paper_trading_paused:
                skip = "Paper 已暂停，未补录暂停期间的交易"
            elif parent["status"] != "OPEN" or row is None or row["ends_at"] <= now:
                skip = "原入场已结束，未事后补单"
            elif not 0 <= now-parent["opened_at"] <= self.policy.mirror_max_delay_seconds:
                skip = "跟踪延迟超过 10 秒，未追溯成交"
            elif len(orders) != 1 or orders[0]["execution_mode"] != "PAPER" or orders[0]["order_type"] != "FAK":
                skip = "仅对应原主账户实际 Paper FAK 成交"
            else:
                order = orders[0]
                if abs(order["cash_spent"]-parent["stake"]) > 1e-5 or abs(order["filled_shares"]-parent["shares"]) > 1e-5:
                    skip = "原入场份额已变化，不能按同一成本对照"
                else:
                    market = market_from_row(row)
                    self.store.upsert_round(market)
                    signal = Signal(parent["symbol"], parent["side"], parent["confidence"], parent["entry_price"], parent["move_bps"], parent["reason"])
                    fill = PaperFill(market, signal, parent["side"], "FAK", order["status"], order["limit_price"],
                                     order["avg_fill_price"], order["filled_shares"], order["notional"], order["fee"],
                                     order["cash_spent"], None, "原主账户入场配对；退出另行模拟",
                                     tuple(PaperFillLevel(x["price"],x["shares"],x["notional"],x["fee"],x["cash_spent"]) for x in levels))
            result = self.store.mirror(parent, fill, now, skip)
            self._audit("entry" if result["trade_id"] else "skipped_entry", parent_id=parent["id"],
                        trade_id=result["trade_id"], round_id=parent["round_id"], reason=result["skip_reason"],
                        inherited_cost=result["original_stake"], inherited_shares=result["original_shares"],
                        parent_opened_at=parent["opened_at"], mirrored_at=now)

    def exit_tick(self, now=None):
        live_clock = now is None
        now = time.time() if now is None else now
        with self._decision_lock:
            if self._stop.is_set() or not self._connect_baseline():
                return
            self._sync_market(now)
            self._sync_entries(now, live_clock=live_clock)
            if self._shared_inputs_required:self._sync_shared_market_inputs()
            with self._lock:
                market = self.current_market
                quotes = {k: dict(v) for k, v in self.paper_quotes.items()}
            # A quote may arrive while SQLite is being read. Check its age against
            # the time AFTER capturing it, never against the beginning of the tick.
            if live_clock:
                now = time.time()
            if market is None or now >= market.ends_at:
                self.last_signal = {"side": "NO_TRADE", "reason": "等待本期市场或官方结算"}
                return
            watchers = self.store.watchers(market.round_id)
            observe = now-self._last_observation >= .5
            observations = []
            message = "等待主账户新成交；不会自行改变入场方向"
            for link in watchers:
                if self._stop.is_set():
                    return
                parent_id = link["parent_id"]
                token = market.up_token if link["side"] == "Up" else market.down_token
                book, error = checked_book(quotes.get(link["side"], {}), token, now, self.policy)
                if observe:
                    mark = sweep_sell(book, link["original_shares"], self.policy) if book else None
                    roi = mark["net"]/link["original_stake"]-1 if mark and mark["full"] else None
                    if roi is not None:
                        self.store.note_observation(parent_id, roi, now)
                    raw_quote = quotes.get(link["side"], {})
                    observations.append({"parent_id": parent_id, "side": link["side"], "book": book,
                                         "quote_clock": {k: raw_quote.get(k) for k in ("token_id", "source_ms", "received_ms", "best_bid", "best_ask")},
                                         "net_roi": roi, "full_depth": bool(mark and mark["full"]), "error": error})
                position = self.store.position(parent_id)
                if position is None:
                    continue  # Keep observing after an early sale until this market expires.
                if self.paper_trading_paused:
                    self._pending.pop(parent_id, None)
                    message = "Paper 已暂停；继续记录行情，暂停自动买卖"
                    continue
                intent = self._pending.get(parent_id)
                if intent and now-intent["at"] > 2:
                    self._audit("exit_expired", parent_id=parent_id, intent=intent)
                    self._pending.pop(parent_id, None)
                    intent = None
                if not book:
                    message = error
                    continue
                if position["consumed_book"] == book["fingerprint"]:
                    message = "部分成交后等待新的买盘深度，未重复使用同一盘口"
                    continue
                if intent:
                    if now-intent["at"] < self.policy.latency_seconds:
                        message = "退出已触发，等待模拟执行延迟"
                        continue
                    sweep = sweep_sell(book, position["shares"], self.policy, intent["limit"])
                    result = self.store.sell(parent_id, sweep, book, intent, now)
                    self._pending.pop(parent_id, None)
                    self._retry_after[parent_id] = now+.5
                    self._audit("exit_fill" if result else "exit_unfilled", parent_id=parent_id,
                                intent=intent, result=result, book=book, sweep=sweep)
                    message = ("止盈" if intent["reason"] == "TP" else "止损") + ("已模拟卖出" if result else "未成交，等待新报价重试")
                    continue
                if now < self._retry_after.get(parent_id, 0):
                    continue
                sweep = sweep_sell(book, position["shares"], self.policy)
                reason, roi = exit_signal(sweep, position["stake"], position["shares"], self.policy)
                if position["exit_latch"] == "SL" and sweep["shares"] > 0:
                    reason = "SL"
                message = f"持仓净退出收益 {roi*100:+.1f}%；等待 +{self.policy.take_profit*100:.0f}% / −{self.policy.stop_loss*100:.0f}%" if roi is not None else "买盘不足，等待可成交深度"
                if reason:
                    if reason == "SL":
                        self.store.latch_stop(parent_id)
                    intent = {"reason": reason, "at": now, "roi": roi,
                              "limit": max(.001, round(book["best_bid"]-self.policy.limit_slippage, 4))}
                    self._pending[parent_id] = intent
                    self._audit("exit_trigger", parent_id=parent_id, intent=intent, book=book)
            if observe and observations:
                self._audit("observation", round_id=market.round_id, sampled_at=now, positions=observations)
                self._last_observation = now
            self.last_signal = {"side": "NO_TRADE", "reason": message}

    def _resolution_loop(self):
        while not self._stop.is_set():
            try:
                self.resolve_due()
            except Exception as exc:
                self._audit("resolution_error", error=type(exc).__name__)
            self._stop.wait(10)

    def resolve_due(self, now=None):
        now = time.time() if now is None else now
        if not self._connect_baseline():
            return
        due = {x["round_id"] for x in self.store.watchers()}
        for slug in sorted(due):
            with self._baseline_lock:
                row = self.baseline.execute("SELECT * FROM market_rounds WHERE round_id=?", (slug,)).fetchone()
            if row is None or row["ends_at"] > now:
                continue
            resolution = dict(row) if row["settlement_source"] == "polymarket_official" and row["outcome"] in ("Up", "Down") else self.polymarket.get_resolution(slug)
            if resolution and resolution.get("outcome") in ("Up", "Down"):
                with self._decision_lock:
                    self.store.resolve(slug, resolution, now)
                self._audit("official_settlement", round_id=slug, outcome=resolution["outcome"])

    def _decorate_open_trades(self, rows, runtime):
        decorated = super()._decorate_open_trades(rows, runtime)
        now = time.time()
        for item in decorated:
            for key in ("exit_value", "unrealized_pnl", "unrealized_roi_pct"):
                item.pop(key, None)
            if item.get("settlement_pending") or not item.get("is_current_market"):
                continue
            token = item["up_token"] if item["side"] == "Up" else item["down_token"]
            book, _ = checked_book(runtime.get("latest_quotes", {}).get(item["side"], {}), token, now, self.policy)
            sweep = sweep_sell(book, item["shares"], self.policy) if book else None
            if sweep and sweep["full"]:
                item.update(exit_value=sweep["net"], unrealized_pnl=round(sweep["net"]-item["stake"],6),
                            unrealized_roi_pct=round((sweep["net"]/item["stake"]-1)*100,4))
        return decorated

    def diagnostics(self):
        run = self.store.run()
        return {"policy": self.policy.payload(), "running": bool(self._thread and self._thread.is_alive()),
                "started_at": run["started_at"] if run else None,
                "initial_balance": self.store.account()["initial_balance"], "book_state": self.book_state,
                "last_tick_at": self.last_tick_at, "last_error": self.last_error,
                "comparison": self.store.comparison(), "journal_path": str(self.journal),
                "entry_mode": "沿用主账户未来实际入场的成本和份额；退出独立模拟，不追溯旧仓",
                "scope": "同单持有到官方结算对照；尚未结算的配对不计入提升。止损是触发阈值，不保证成交损失上限。"}

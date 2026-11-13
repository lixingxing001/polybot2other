"""Delayed, durable Paper execution. Only public/read-only market callbacks enter here.

The live signal implementation is reused against isolated Paper accounting. No
signer, submission, cancellation or redemption callback is accepted by this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .managed_live_client import fee_amount, maximum_fee_ratio
from .models import MarketRound, Signal
from .shared_market_inputs import quote_error


VERSION = "realistic-paper-v1"
ACTIVE_PHASES = ("IN_FLIGHT", "CONFIRMING")
NAMES = {"MAIN": "主账户", "MAIN_TP_SL": "主账户止盈止损",
         "MAIN_OPTIMIZED": "主账户（优化版）", "MAIN_ADAPTIVE": "主账户自适应成长版",
         "VALUE_CONFIRM_V1": "价值确认 V1"}


@dataclass(frozen=True)
class ExecutionPolicy:
    arrival_floor_seconds: float = 1.0
    confirmation_seconds: float = 12.0
    confirmation_samples: int = 0
    depth_fraction: float = .5
    redemption_seconds: float = 60.0
    max_poll_lateness_seconds: float = 2.0

    @classmethod
    def from_history(cls, path):
        """Read confirmation latency, NOT matching latency; never open a writer."""
        try:
            with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
                rows = conn.execute("""SELECT min(f.confirmed_at)-o.submitted_at AS delay
                    FROM orders o JOIN fills f ON f.order_id=o.id
                    WHERE o.submitted_at IS NOT NULL GROUP BY o.id
                    ORDER BY o.submitted_at DESC LIMIT 200""").fetchall()
            samples = sorted(float(r[0]) for r in rows if r[0] is not None and 0 < r[0] <= 300)
            if len(samples) >= 10:
                return cls(confirmation_seconds=round(samples[math.ceil(len(samples)*.9)-1], 3),
                           confirmation_samples=len(samples))
        except (OSError, sqlite3.Error, ValueError):
            pass
        return cls()


def floor_to(value, places):
    factor = 10 ** places
    return math.floor(value*factor+1e-8)/factor


def checked_info(info):
    for key in ("fee_rate", "fee_exponent", "tick_size"):
        if key not in info or not math.isfinite(float(info[key])):
            raise ValueError("缺少官方费用或价格精度")
    if not 0 <= float(info["fee_rate"]) <= 1 or not 1 <= float(info["fee_exponent"]) <= 4:
        raise ValueError("暂不支持该市场的手续费曲线")
    if str(info["tick_size"]) not in {"0.1", "0.01", "0.001", "0.0001"}:
        raise ValueError("当前 SDK 不支持该市场价格精度")
    return dict(info)


def buy_terms(amount, ceiling, info, book):
    """Same fee reserve, tick rounding and minimum-size gate as managed live."""
    info = checked_info(info)
    tick = float(info["tick_size"])
    if str(book.get("tick_size")) != str(info["tick_size"]):
        raise ValueError("市场价格精度正在更新，等待同步")
    minimum = float(book.get("min_order_size", 0))
    if not math.isfinite(minimum) or minimum <= 0:
        raise ValueError("缺少官方最小下单量，等待盘口补齐")
    limit = round(math.floor(min(ceiling, 1-tick)/tick+1e-9)*tick, 6)
    if limit <= 0 or float(book["best_ask"]) > limit+1e-8:
        raise ValueError("提交前盘口已超过策略限价")
    ratio = maximum_fee_ratio(info["fee_rate"], info["fee_exponent"], tick, limit)
    budget = amount/(1+ratio)
    if budget/limit+1e-8 < minimum:
        raise ValueError(f"预算 ${amount:g} 按限价最多约 {budget/limit:.2f} 份，低于最小 {minimum:g} 份")
    depth = sum(float(x["price"])*float(x["size"]) for x in book["asks"] if float(x["price"]) <= limit+1e-8)
    if depth+1e-8 < budget:
        raise ValueError("提交前真实卖盘深度不足本笔预算")
    notional = floor_to(budget, 2)  # V2 SDK BUY amount precision.
    if notional < 1:
        raise ValueError("扣除费用预留后不足 $1 最小市价买单金额")
    return {"limit": limit, "notional_budget": notional, "minimum_shares": minimum,
            "amount": amount, "info": info}


def sweep_book(book, action, limit, info, *, notional_budget=0, shares=0, fraction=1, consumed=None):
    """Finite depth at ARRIVAL, per-level fees and no invented mid-price fills."""
    levels = book.get("asks" if action == "BUY" else "bids") or []
    levels = sorted(levels, key=lambda x: float(x["price"]), reverse=action == "SELL")
    remaining = notional_budget if action == "BUY" else shares
    result = []
    for level in levels:
        p, size = float(level["price"]), float(level["size"])
        if not math.isfinite(p) or not math.isfinite(size) or not 0 < p < 1 or size <= 0:
            raise ValueError("盘口档位无效")
        if (action == "BUY" and p > limit+1e-8) or (action == "SELL" and p < limit-1e-8):
            break
        size = max(0., size*fraction-(consumed or {}).get(str(p), 0.))
        quantity = floor_to(min(size, remaining/p if action == "BUY" else remaining), 6)
        if quantity <= 0:
            continue
        notional = round(quantity*p, 6)
        fee = fee_amount(quantity, p, info["fee_rate"], info["fee_exponent"])
        result.append({"price": p, "shares": quantity, "notional": notional, "fee": fee,
                       "cash_spent": round(notional+fee if action == "BUY" else notional-fee, 6)})
        remaining -= quantity*p if action == "BUY" else quantity
        if remaining <= .000001:
            break
    quantity = round(sum(x["shares"] for x in result), 6)
    gross = round(sum(x["notional"] for x in result), 6)
    fee = round(sum(x["fee"] for x in result), 6)
    return {"shares": quantity, "notional": gross, "fee": fee,
            "cash": round(gross+fee if action == "BUY" else gross-fee, 6),
            "price": gross/quantity if quantity else 0., "levels": result,
            "full": remaining <= .00001}


class PaperRiskView:
    """Minimal ledger adapter used by the unchanged LiveSignals evaluator."""
    def __init__(self, engine):
        self.engine = engine
        self.path = engine.store.db_path

    def positions(self, strategy_id):
        with self.engine.store._lock:
            return [dict(r, realized_pnl=r["pnl"] or 0) for r in
                    self.engine.store.conn.execute("SELECT * FROM trades")]

    def active_orders(self, strategy_id=None, round_id=None):
        return [x for x in self.engine.pending() if round_id is None or x["round_id"] == round_id]

    def daily_pnl(self, strategy_id):
        return self.engine.store.daily_realized_pnl()

    def risk_usage(self, strategy_id):
        trades = self.engine.store.open_trades()
        pending = [o for o in self.engine.pending() if o["action"] == "BUY"]
        return {"slots": len(trades)+len(pending),
                "loss_exposure": sum(t["stake"] for t in trades)+sum(o["amount"] for o in pending)}


class PaperExecution:
    def __init__(self, bot, strategy_id, base_settings, *, frame_reader, info_reader,
                 book_reader, config_reader, feed, policy=None, clock=time.time):
        self.bot, self.store, self.strategy_id = bot, bot.store, strategy_id
        self.frame_reader, self.info_reader, self.book_reader = frame_reader, info_reader, book_reader
        self.config_reader, self.feed, self.clock = config_reader, feed, clock
        self.policy = policy or ExecutionPolicy()
        self.lock = threading.RLock()
        self.last_event = {"code": "WARMUP", "message": "等待同源行情", "at": clock()}
        self.last_attempt = 0.
        self.info_cache = {}
        self.mark_cache = {}
        self.rules = {}
        self._create_schema()
        self.store.paper_execution_ledger = self
        from .managed_live_signals import LiveSignals
        self.risk = PaperRiskView(self)
        self.signals = LiveSignals(
            base_settings,
            self.risk,
            feed,
            lambda: self.store.account()["cash_balance"],
            lambda: {},
            adaptive_model=getattr(bot, "adaptive_model", None),
        )
        self.recover()

    def _create_schema(self):
        with self.store._lock, self.store.conn:
            self.store.conn.executescript("""
                CREATE TABLE IF NOT EXISTS paper_sim_run(id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL,
                    version TEXT, policy_json TEXT);
                CREATE TABLE IF NOT EXISTS paper_sim_orders(order_id INTEGER PRIMARY KEY REFERENCES paper_orders(id),
                    phase TEXT NOT NULL, due_at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_sim_events(id INTEGER PRIMARY KEY, at REAL, code TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS paper_sim_claims(trade_id INTEGER PRIMARY KEY, amount REAL NOT NULL,
                    available_at REAL NOT NULL, released_at REAL);
                CREATE TABLE IF NOT EXISTS paper_sim_consumed(token TEXT, action TEXT, fingerprint TEXT, levels TEXT,
                    PRIMARY KEY(token,action));
            """)
            self.store.conn.execute("INSERT OR IGNORE INTO paper_sim_run VALUES(1,?,?,?)",
                                    (self.clock(), VERSION, json.dumps(asdict(self.policy))))
            # A restart continues the recorded execution assumptions for this run.
            row = self.store.conn.execute("SELECT policy_json FROM paper_sim_run WHERE id=1").fetchone()
            self.policy = ExecutionPolicy(**json.loads(row[0]))

    def event(self, code, message):
        now = self.clock()
        changed = (code, message) != (self.last_event["code"], self.last_event["message"])
        self.last_event = {"code": code, "message": message, "at": now}
        if changed:
            with self.store._lock, self.store.conn:
                self.store.conn.execute("INSERT INTO paper_sim_events(at,code,message) VALUES(?,?,?)", (now, code, message))

    def pending(self):
        with self.store._lock:
            return [{**json.loads(r["payload"]), "order_id": r["order_id"], "phase": r["phase"], "due_at": r["due_at"]}
                    for r in self.store.conn.execute("SELECT * FROM paper_sim_orders WHERE phase IN ('IN_FLIGHT','CONFIRMING')")]

    def protects_order(self, ident):
        with self.store._lock:
            return self.store.conn.execute("SELECT 1 FROM paper_sim_orders WHERE order_id=? AND phase IN ('IN_FLIGHT','CONFIRMING')", (ident,)).fetchone() is not None

    def settlement_blocked(self, round_id):
        return any(x["round_id"] == round_id for x in self.pending())

    def defer_payout(self, trade_id, amount, now):
        # Called within TradeStore's settlement transaction and lock.
        if amount > 0:
            self.store.conn.execute("INSERT OR IGNORE INTO paper_sim_claims VALUES(?,?,?,NULL)",
                                    (trade_id, amount, now+self.policy.redemption_seconds))

    def pending_cash(self):
        with self.store._lock:
            return float(self.store.conn.execute("SELECT coalesce(sum(amount),0) FROM paper_sim_claims WHERE released_at IS NULL").fetchone()[0])

    def recover(self):
        # There is no historical order book at the hypothetical arrival after a
        # shutdown. Do not fabricate a fill; saved matches remain confirmable.
        for order in self.pending():
            if order["phase"] == "IN_FLIGHT":
                self._finish_empty(order, "服务中断，缺少到达时盘口；取消模拟订单并释放预算")

    def _info(self, market):
        if market.round_id not in self.info_cache:
            self.info_cache = {market.round_id: checked_info(self.info_reader(market))}
        return self.info_cache[market.round_id]

    def _record_signal(self, signal):
        with self.bot._lock:
            self.bot.last_signal = asdict(signal)
            features = getattr(self.signals, "last_features", None)
            if isinstance(features, dict):
                self.bot.last_decision = dict(features)

    def _entry_block(self, market, side, rule):
        if self.pending():
            return "已有模拟订单传输或等待确认，预算保持占用"
        with self.store._lock:
            rows = self.store.conn.execute("SELECT side FROM trades WHERE round_id=?", (market.round_id,)).fetchall()
        if any(self.strategy_id == "VALUE_CONFIRM_V1" or r[0] == side for r in rows):
            return "本期该方向已有成交，不重复开仓"
        risk = self.risk.risk_usage(self.strategy_id)
        daily = self.store.daily_realized_pnl()
        if risk["slots"] >= rule["max_open_trades"]:
            return f"持仓和待确认订单达到 {rule['max_open_trades']} 笔上限"
        loss = rule["max_daily_loss"]
        if loss and (daily <= -loss or rule["stake_dollars"] > loss+min(0, daily)-risk["loss_exposure"]+.000001):
            return "本策略剩余亏损额度不足以覆盖本笔预算和已有持仓风险"
        if self.store.account()["cash_balance"]+.000001 < rule["stake_dollars"]:
            return "模拟可用资金不足整笔预算（待确认和待领取资金不可复用）"
        return None

    def decide(self):
        with self.lock:
            frame = self.bot._sync_shared_market_inputs()
            if self.bot.paper_trading_paused:
                self.event("PAUSED", "Paper 已暂停新交易；已提交模拟订单继续确认")
                return frame
            if frame.error:
                self.event("DATA_WAIT", frame.error)
                return frame
            try:
                config = self.config_reader()
                rule = dict(config["strategies"][self.strategy_id])
                self.rules = rule
                info_round = frame.market.round_id
                info = self._info(frame.market)
                # Market info is an external read: recapture inputs afterwards.
                frame = self.bot._sync_shared_market_inputs()
                if frame.error:
                    self.event("DATA_WAIT", frame.error)
                    return frame
                if frame.market.round_id != info_round:
                    self.event("DATA_WAIT", "市场已切换，重新读取本期参数")
                    return frame
                self.manage_exits()
                signal, ceiling = self.signals.evaluate(self.strategy_id, rule, frame.market, frame.reference,
                    frame.quotes, frame.observed_at, info, market_input=frame)
                self._record_signal(signal)
                if signal.side not in ("Up", "Down"):
                    if not self.pending(): self.event("SIGNAL_WAIT", signal.reason)
                    return frame
                block = self._entry_block(frame.market, signal.side, rule)
                if block:
                    self.event("ENTRY_LIMIT", block)
                    return frame
                if self.clock()-self.last_attempt < 3:
                    return frame
                self.last_attempt = self.clock()
                self.enqueue_buy(frame.market, signal, ceiling, rule, config["revision"], info)
            except Exception as exc:
                message = str(exc)[:180] if isinstance(exc, ValueError) else f"模拟执行读取失败（{type(exc).__name__}），自动重试"
                self.event("EXECUTION_WAIT", message)
            return frame

    def enqueue_buy(self, market, signal, ceiling, rule, revision, info):
        started = self.clock()
        token = market.up_token if signal.side == "Up" else market.down_token
        book = self.book_reader(token)
        error = quote_error(market, {signal.side: book}, signal.side, self.clock())
        if error: raise ValueError(error)
        if str(book.get("tick_size")) != str(info["tick_size"]):
            info = checked_info(self.info_reader(market, force=True))
            self.info_cache[market.round_id] = info
        terms = buy_terms(rule["stake_dollars"], ceiling, info, book)
        current = self.config_reader()
        if current["revision"] != revision:
            raise ValueError("金额或风险设置已变化，按新设置重新检查")
        if self.bot.paper_trading_paused or (getattr(self.bot, "_stop", None) and self.bot._stop.is_set()):
            raise ValueError("Paper 已暂停或服务停止，取消本次提交")
        fresh = self.frame_reader()
        if fresh.error or not fresh.market or fresh.market.round_id != market.round_id:
            raise ValueError(fresh.error or "市场已切换")
        again, new_limit = self.signals.evaluate(self.strategy_id, rule, fresh.market, fresh.reference,
            fresh.quotes, fresh.observed_at, info, market_input=fresh)
        if (again.side != signal.side or again.entry_price > terms["limit"] or new_limit is None
                or terms["limit"] > new_limit+.000001 or self.clock() >= market.ends_at-self.signals.settings.min_time_left_seconds):
            raise ValueError("提交前复查已不满足原策略入场条件")
        block = self._entry_block(market, signal.side, rule)
        if block: raise ValueError(block)
        return self._queue(market, signal, "BUY", terms, revision,
                           max(self.policy.arrival_floor_seconds, self.clock()-started))

    def _queue(self, market, signal, action, terms, revision, delay, trade_id=None, exit_reason=None):
        now = self.clock()
        payload = {**terms, "action": action, "market": asdict(market), "round_id": market.round_id,
                   "token": market.up_token if signal.side == "Up" else market.down_token,
                   "side": signal.side, "signal": asdict(signal), "submitted_at": now,
                   "arrival_delay": delay, "revision": revision, "position_id": trade_id,
                   "exit_reason": exit_reason, "version": VERSION}
        amount = terms.get("amount", 0) if action == "BUY" else 0
        with self.store._lock, self.store.conn:
            if now >= market.ends_at: raise ValueError("市场已结束，取消本次提交")
            if self.pending(): raise ValueError("已有模拟订单等待确认")
            if amount > self.store.account()["cash_balance"]+.000001:
                raise ValueError("模拟可用资金不足")
            if action == "SELL":
                position = self.store.conn.execute("SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
                if not position or position["shares"]+.000001 < terms["shares"]:
                    raise ValueError("模拟持仓已变化，取消本次卖出")
            self.store.upsert_round(market)
            ident = self.store._insert_paper_order(market=market, side=signal.side,
                order_type="FAK" if action == "BUY" else "FAK_SELL", status="PENDING", limit_price=terms["limit"],
                post_only=False, expires_at=None, requested_cash=amount, reserved_cash=amount, remaining_cash=amount,
                filled_shares=0, avg_fill_price=None, notional=0, fee=0, cash_spent=0, trade_id=trade_id,
                confidence=signal.confidence, move_bps=signal.move_bps,
                reason=f"{VERSION} · 模拟{action}提交 · {exit_reason or signal.reason}", now=now)
            self.store.conn.execute("UPDATE account SET cash_balance=cash_balance-?,updated_at=? WHERE id=1", (amount, now))
            self.store.conn.execute("INSERT INTO paper_sim_orders VALUES(?,'IN_FLIGHT',?,?)", (ident, now+delay, json.dumps(payload)))
        self.store.record_equity()
        self.event("IN_FLIGHT", f"模拟{action}已提交，约 {delay:.2f} 秒后按新盘口撮合")
        return ident

    def poll(self):
        with self.lock:
            now = self.clock()
            with self.store._lock, self.store.conn:
                claims = self.store.conn.execute("SELECT * FROM paper_sim_claims WHERE released_at IS NULL AND available_at<=?", (now,)).fetchall()
                if claims:
                    self.store.conn.execute("UPDATE account SET cash_balance=cash_balance+?,updated_at=? WHERE id=1", (sum(x["amount"] for x in claims), now))
                    self.store.conn.execute("UPDATE paper_sim_claims SET released_at=? WHERE released_at IS NULL AND available_at<=?", (now, now))
            if claims: self.store.record_equity()
            for order in self.pending():
                if now < order["due_at"]: continue
                if order["phase"] == "CONFIRMING": self._confirm(order)
                elif now-order["due_at"] > self.policy.max_poll_lateness_seconds:
                    self._finish_empty(order, "处理延迟过长，缺少到达时盘口；未虚构成交")
                else: self._arrive(order)

    def _arrive(self, order):
        frame = self.frame_reader()
        market = frame.market
        now = self.clock()
        if frame.error or not market or market.round_id != order["round_id"] or now >= market.ends_at:
            self._finish_empty(order, frame.error or "订单到达时市场已结束或切换")
            return
        error = frame.quote_error(order["side"], now)
        book = frame.quotes.get(order["side"], {})
        if error:
            self._finish_empty(order, "订单到达时"+error)
            return
        # A pre-submission cached quote is not evidence of liquidity at arrival.
        if min(book.get("source_ms", 0), book.get("received_ms", 0)) < order["submitted_at"]*1000:
            self._finish_empty(order, "到达时没有提交后的新盘口，未沿用旧价成交")
            return
        if book.get("tick_size") and str(book["tick_size"]) != str(order["info"]["tick_size"]):
            self._finish_empty(order, "传输期间市场价格精度已变化")
            return
        action = order["action"]
        raw = book.get("asks" if action == "BUY" else "bids") or []
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        with self.store._lock:
            previous = self.store.conn.execute("SELECT * FROM paper_sim_consumed WHERE token=? AND action=?", (order["token"], action)).fetchone()
        consumed = json.loads(previous["levels"]) if previous and previous["fingerprint"] == fingerprint else {}
        fill = sweep_book(book, action, order["limit"], order["info"],
            notional_budget=order.get("notional_budget", 0), shares=order.get("shares", 0),
            fraction=self.policy.depth_fraction, consumed=consumed)
        if fill["shares"] <= 0:
            self._finish_empty(order, "FAK 到达时限价内没有可用深度，未成交部分取消")
            return
        if action == "BUY" and fill["cash"] > order["amount"]+.000001:
            raise ValueError("模拟费用超过已预留预算")
        for level in fill["levels"]:
            key = str(level["price"])
            consumed[key] = consumed.get(key, 0)+level["shares"]
        payload = {**order, "fill": fill, "matched_at": now,
                   "arrival_book": {k: book.get(k) for k in ("source_ms", "received_ms", "best_bid", "best_ask", "bids", "asks")}}
        due = max(now+.1, order["submitted_at"]+self.policy.confirmation_seconds)
        with self.store._lock, self.store.conn:
            self.store.conn.execute("UPDATE paper_sim_orders SET phase='CONFIRMING',due_at=?,payload=? WHERE order_id=? AND phase='IN_FLIGHT'", (due, json.dumps(payload), order["order_id"]))
            self.store.conn.execute("INSERT OR REPLACE INTO paper_sim_consumed VALUES(?,?,?,?)", (order["token"], action, fingerprint, json.dumps(consumed)))
            self.store.conn.execute("UPDATE paper_orders SET external_status='SIM_MATCHED_AWAITING_CONFIRMATION',reason=reason||?,updated_at=? WHERE id=?",
                (f" | 已模拟撮合 {fill['shares']:.6f} 份，等待确认；余额尚未释放", now, order["order_id"]))
        self.event("CONFIRMING", f"已模拟撮合，约 {max(0,due-now):.1f} 秒后确认；资金/份额保持占用")

    def _finish_empty(self, order, reason):
        now = self.clock()
        with self.store._lock, self.store.conn:
            current = self.store.conn.execute("SELECT * FROM paper_orders WHERE id=?", (order["order_id"],)).fetchone()
            if current is None or current["status"] != "PENDING": return
            self.store.conn.execute("UPDATE account SET cash_balance=cash_balance+?,updated_at=? WHERE id=1", (current["remaining_cash"], now))
            self.store.conn.execute("UPDATE paper_orders SET status='CANCELED',remaining_cash=0,reason=reason||?,updated_at=? WHERE id=?", (" | "+reason, now, order["order_id"]))
            self.store.conn.execute("UPDATE paper_sim_orders SET phase='CANCELED' WHERE order_id=?", (order["order_id"],))
        self.store.record_equity()
        self.event("UNFILLED", reason)

    def _confirm(self, order):
        fill, now = order["fill"], self.clock()
        frame = self.frame_reader()
        if not frame.market or not any(frame.quote_error(side, now) is None for side in ("Up", "Down")):
            self.event("CONFIRMATION_WAIT", "模拟撮合已记录；盘口连接恢复后确认，预算保持占用，不重复提交")
            return
        status = "FILLED" if fill["full"] else "PARTIAL"
        with self.store._lock, self.store.conn:
            current = self.store.conn.execute("SELECT * FROM paper_sim_orders WHERE order_id=?", (order["order_id"],)).fetchone()
            if not current or current["phase"] != "CONFIRMING": return
            if order["action"] == "BUY":
                signal = order["signal"]
                cur = self.store.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,
                    confidence,move_bps,status,opened_at,reason) VALUES(?,'BTC',?,?,?,?,?,?,'OPEN',?,?)""",
                    (order["round_id"], order["side"], fill["cash"], fill["price"], fill["shares"],
                     signal["confidence"], signal["move_bps"], order["matched_at"], VERSION+" | "+signal["reason"]))
                trade_id = cur.lastrowid
                self.store.conn.execute("UPDATE account SET cash_balance=cash_balance+?,updated_at=? WHERE id=1", (round(order["amount"]-fill["cash"], 6), now))
            else:
                trade_id = self._sell_fill(order, now)
            for i, level in enumerate(fill["levels"], 1):
                self.store.conn.execute("INSERT INTO paper_fills(order_id,trade_id,level_index,price,shares,notional,fee,cash_spent,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (order["order_id"], trade_id, i, level["price"], level["shares"], level["notional"], level["fee"],
                     level["cash_spent"], order["matched_at"]))
            self.store.conn.execute("""UPDATE paper_orders SET status=?,remaining_cash=0,filled_shares=?,avg_fill_price=?,
                notional=?,fee=?,cash_spent=?,trade_id=?,external_status='SIM_CONFIRMED',reason=reason||?,updated_at=? WHERE id=?""",
                (status, fill["shares"], fill["price"], fill["notional"], fill["fee"], fill["cash"] if order["action"] == "BUY" else 0,
                 trade_id, " | 确认完成"+("；FAK 剩余数量取消" if status == "PARTIAL" else ""), now, order["order_id"]))
            self.store.conn.execute("UPDATE paper_sim_orders SET phase=?,payload=? WHERE order_id=?",
                (status, json.dumps({**order, "confirmed_at": now, "trade_id": trade_id}), order["order_id"]))
        self.store.record_equity()
        self.event(status, f"模拟{order['action']}确认：{fill['shares']:.6f} 份，均价 {fill['price']:.4f}，费用 ${fill['fee']:.4f}")

    def _sell_fill(self, order, now):
        """Called inside the confirmation transaction; partial exits conserve basis."""
        fill = order["fill"]
        row = self.store.conn.execute("SELECT * FROM trades WHERE id=? AND status='OPEN'", (order["position_id"],)).fetchone()
        if row is None or fill["shares"] > row["shares"]+.000001:
            raise ValueError("模拟卖出份额与持仓不一致")
        basis = round(row["stake"]*fill["shares"]/row["shares"], 6)
        payout = fill["cash"]
        pnl = round(payout-basis, 6)
        reason = row["reason"]+f" | {order['exit_reason']}；真实盘口分档模拟退出"
        if row["shares"]-fill["shares"] <= .000001:
            trade_id = row["id"]
            self.store.conn.execute("UPDATE trades SET status='SETTLED',settled_at=?,exit_price=?,payout=?,pnl=?,settlement_source='early_exit',reason=? WHERE id=?",
                (order["matched_at"], fill["price"], payout, pnl, reason, trade_id))
        else:
            self.store.conn.execute("UPDATE trades SET shares=round(shares-?,6),stake=round(stake-?,6) WHERE id=?", (fill["shares"], basis, row["id"]))
            cur = self.store.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,confidence,
                move_bps,status,opened_at,settled_at,exit_price,payout,pnl,settlement_source,reason)
                VALUES(?,?,?,?,?,?,?,?,'SETTLED',?,?,?,?,?,'early_exit',?)""",
                (row["round_id"], row["symbol"], row["side"], basis, row["entry_price"], fill["shares"], row["confidence"],
                 row["move_bps"], row["opened_at"], order["matched_at"], fill["price"], payout, pnl, reason))
            trade_id = cur.lastrowid
        self.store.conn.execute("UPDATE account SET cash_balance=cash_balance+?,realized_pnl=realized_pnl+?,updated_at=? WHERE id=1", (payout, pnl, now))
        return trade_id

    def manage_exits(self):
        if self.strategy_id != "MAIN_TP_SL" or self.bot.paper_trading_paused or self.pending(): return
        frame = self.frame_reader()
        if frame.error or not frame.market or self.clock() >= frame.market.ends_at: return
        rule = self.config_reader()["strategies"][self.strategy_id]
        info = self._info(frame.market)
        for row in self.store.open_trades():
            if row["round_id"] != frame.market.round_id or frame.quote_error(row["side"], self.clock()): continue
            quote = frame.quotes[row["side"]]
            mark = sweep_book(quote, "SELL", 0, info, shares=row["shares"])
            if mark["shares"] <= 0: continue
            basis = row["stake"]*mark["shares"]/row["shares"]
            roi = (mark["cash"]/basis-1)*100
            reason = "止盈" if roi >= rule["take_profit_pct"] else "止损" if roi <= -rule["stop_loss_pct"] else None
            if reason and self.clock()-self.last_attempt >= 3:
                self.last_attempt = self.clock()
                self.enqueue_sell(row, frame.market, reason)
                break

    def enqueue_sell(self, position, market, reason="手动卖出"):
        with self.lock:
            if self.pending(): raise ValueError("已有模拟订单等待确认")
            started = self.clock()
            info = self._info(market)
            token = market.up_token if position["side"] == "Up" else market.down_token
            book = self.book_reader(token)
            error = quote_error(market, {position["side"]: book}, position["side"], self.clock())
            if error: raise ValueError(error)
            if self.clock() >= market.ends_at: raise ValueError("市场已结束，等待官方结算")
            minimum = float(book.get("min_order_size", 0))
            if minimum <= 0 or position["shares"] < minimum-.000001:
                raise ValueError("剩余份额低于官方最小卖单要求，保留至结算")
            if str(book.get("tick_size")) != str(info["tick_size"]): raise ValueError("市场价格精度正在更新")
            tick = float(info["tick_size"])
            limit = max(tick, math.floor((book["best_bid"]-.02)/tick+1e-9)*tick)
            quantity = floor_to(position["shares"], 2)  # V2 market SELL precision.
            estimate = sweep_book(book, "SELL", limit, info, shares=quantity)
            if estimate["shares"] <= 0 or estimate["cash"] <= 0: raise ValueError("卖出限价内没有可用买盘")
            config = self.config_reader()
            rule = config["strategies"][self.strategy_id]
            basis = position["stake"]*estimate["shares"]/position["shares"]
            roi = (estimate["cash"]/basis-1)*100
            if reason == "止盈":
                if roi < rule["take_profit_pct"]: raise ValueError("最新盘口已不满足止盈")
                target = position["stake"]/position["shares"]*(1+rule["take_profit_pct"]/100)
                lo, hi = tick, 1-tick
                if hi-fee_amount(1, hi, info["fee_rate"], info["fee_exponent"]) < target: raise ValueError("价格精度无法满足止盈净回款")
                for _ in range(40):
                    mid = (lo+hi)/2
                    if mid-fee_amount(1, mid, info["fee_rate"], info["fee_exponent"]) < target: lo = mid
                    else: hi = mid
                limit = max(limit, math.ceil(hi/tick-1e-9)*tick)
                if limit > book["best_bid"]+.000001: raise ValueError("止盈限价暂无可成交买盘")
            elif reason == "止损" and roi > -rule["stop_loss_pct"]:
                raise ValueError("最新盘口已恢复，未触发止损")
            signal = Signal("BTC", position["side"], position["confidence"], position["entry_price"], position["move_bps"], reason)
            return self._queue(market, signal, "SELL", {"limit": round(limit, 6), "shares": quantity, "info": info, "amount": 0},
                config["revision"], max(self.policy.arrival_floor_seconds, self.clock()-started), position["id"], reason)

    def mark(self, row, quote, market_payload):
        """Read-only net liquidation estimate; never value all shares at best bid."""
        market = self.frame_reader().market
        info = self.info_cache.get(row["round_id"])
        key = (row["id"], row["shares"], row["stake"])
        cached = self.mark_cache.get(key)
        def unavailable(message):
            if cached:
                return {**cached, "valuation_note": message+"；展示上次净退出估值", "valuation_status": "cached"}
            return {"exit_value": None, "unrealized_pnl": None, "unrealized_roi_pct": None,
                    "valuation_note": message, "valuation_status": "missing"}
        if (not market or market.round_id != row["round_id"] or not info
                or self.clock() >= market.ends_at or quote_error(market, {row["side"]: quote}, row["side"], self.clock())):
            return unavailable("等待新鲜盘口或官方结算")
        fill = sweep_book(quote, "SELL", 0, info, shares=row["shares"], fraction=self.policy.depth_fraction)
        if not fill["full"]:
            return unavailable("可见买盘不足以卖出全部持仓")
        pnl = round(fill["cash"]-row["stake"], 6)
        result = {"exit_value": fill["cash"], "unrealized_pnl": pnl,
                "unrealized_roi_pct": round(pnl/row["stake"]*100, 4) if row["stake"] else 0,
                "valuation_note": "按保守可用买盘逐档估值，已扣官方费用；最终以成交为准",
                "valuation_status": "fresh", "valuation_at": self.clock()}
        self.mark_cache[key] = result
        if len(self.mark_cache) > 100:
            self.mark_cache = dict(list(self.mark_cache.items())[-50:])
        return result

    def snapshot(self):
        with self.store._lock:
            run = dict(self.store.conn.execute("SELECT * FROM paper_sim_run WHERE id=1").fetchone())
            counts = dict(self.store.conn.execute("SELECT phase,count(*) FROM paper_sim_orders GROUP BY phase").fetchall())
            events = [dict(x) for x in self.store.conn.execute("SELECT * FROM paper_sim_events ORDER BY id DESC LIMIT 8")]
        rules = self.config_reader()["strategies"][self.strategy_id]
        return {"enabled": True, "strategy_id": self.strategy_id, "name": NAMES[self.strategy_id], "version": VERSION,
                "started_at": run["started_at"], "policy": asdict(self.policy), "rules": rules,
                "last_event": dict(self.last_event), "counts": counts, "events": events,
                "pending": [{k: x.get(k) for k in ("order_id", "action", "phase", "due_at", "amount", "limit")} for x in self.pending()],
                "pending_settlement_cash": self.pending_cash(), "capital_scope": "独立 $100 模拟钱包；不与其他策略共用资金"}


class PaperExecutionService:
    def __init__(self, owner, manager):
        self.owner = owner
        self.stop_event = threading.Event()
        self.thread = None
        policy = ExecutionPolicy.from_history(manager.store.path)
        bots = {"MAIN": owner, **(owner.strategy_experiments._bots if owner.strategy_experiments else {})}
        self.engines = {}
        for key, bot in bots.items():
            if key not in NAMES: continue
            def config_reader(strategy_id=key, strategy_bot=bot):
                config = manager.store.config()
                if strategy_id in config.get("strategies", {}):
                    return config
                # MAIN_ADAPTIVE is Paper-only.  Keep its amount and risk rules
                # on the isolated strategy settings instead of adding a live
                # wallet switch or a live-account row for it.
                strategy_settings = strategy_bot.settings
                strategies = dict(config.get("strategies", {}))
                strategies[strategy_id] = {
                    "enabled": True,
                    "stake_dollars": strategy_settings.stake_dollars,
                    "max_open_trades": strategy_settings.max_open_trades,
                    "max_daily_loss": strategy_settings.max_daily_loss,
                    "take_profit_pct": 20.0,
                    "stop_loss_pct": 40.0,
                }
                return {**config, "strategies": strategies}
            engine = PaperExecution(bot, key, owner.settings, frame_reader=manager.market_input_snapshot,
                info_reader=manager.exchange.market_info, book_reader=manager.exchange.rest_book,
                config_reader=config_reader, feed=manager.feed, policy=policy)
            bot.paper_execution = engine
            self.engines[key] = engine

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="paper-execution-confirmations")
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            for engine in self.engines.values():
                # A slow REST preflight for one strategy must not delay another
                # strategy's already submitted hypothetical arrival/confirmation.
                if not engine.lock.acquire(blocking=False): continue
                try:
                    engine.poll()
                except Exception as exc:
                    message = str(exc)[:180] if isinstance(exc, ValueError) else f"模拟执行恢复中（{type(exc).__name__}）"
                    engine.event("EXECUTION_WAIT", message)
                finally:
                    engine.lock.release()
            self.stop_event.wait(.1)

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(timeout=8)

    def snapshot(self):
        return {"enabled": True, "version": VERSION, "accounts": [x.snapshot() for x in self.engines.values()],
                "limitations": "使用可见盘口和保守深度；传输与领取延迟为估计，无法复刻隐藏订单、真实排队、链上失败及多策略共用钱包竞争。"}

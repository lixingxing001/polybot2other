"""One qualifying MAIN buy per BTC market, settled only by its official outcome."""
from dataclasses import asdict
import json
import math

from .paper_execution import PaperExecution, VERSION
from .first_strong_rules import KEY, LABEL, SCORE_FLOOR, BUDGET, SLOTS, qualified


class FirstStrongExecution(PaperExecution):
    """Share MAIN preflight and delayed matching, while locking slots in a private ledger."""

    def _create_schema(self):
        super()._create_schema()
        with self.store._lock, self.store.conn:
            self.store.conn.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS first_strong_round_fill ON trades(round_id);
                CREATE UNIQUE INDEX IF NOT EXISTS first_strong_round_pending ON paper_orders(round_id)
                    WHERE status='PENDING';
                CREATE TABLE IF NOT EXISTS first_strong_rounds(
                    round_id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    samples INTEGER NOT NULL, fresh_samples INTEGER NOT NULL, peak_entry_score REAL,
                    first_strong_at REAL, last_code TEXT, last_reason TEXT, last_in_window_reason TEXT);
            """)
        self.round_note = None
        self.last_note_write = 0.

    def _record_signal(self, signal):
        super()._record_signal(signal)
        if signal.side in ("Up", "Down") and math.isfinite(signal.confidence):
            self._cycle_peak = max(signal.confidence, getattr(self, "_cycle_peak", 0.))

    def decide(self):
        with self.lock:
            before = self.last_event
            self._cycle_peak = 0.
            frame = super().decide()
            if self.last_event is not before:
                code, reason = self.last_event["code"], self.last_event["message"]
            elif self._cycle_peak:
                code, reason = "RECHECK_WAIT", "等待下一次提交检查"
            else:
                signal = getattr(self.bot, "last_signal", {}) or {}
                code, reason = "SIGNAL_WAIT", signal.get("reason") or "等待本局方向信号"
            peak = self._cycle_peak
        if frame.market:
            self._observe_round(frame, peak, code, reason)
        return frame

    def _observe_round(self, frame, peak, code, reason):
        rid, at = frame.market.round_id, frame.observed_at
        new_round = not self.round_note or self.round_note["round_id"] != rid
        if new_round:
            self.flush_round()
            with self.store._lock:
                saved = self.store.conn.execute("SELECT * FROM first_strong_rounds WHERE round_id=?", (rid,)).fetchone()
            self.round_note = dict(saved) if saved else {
                "round_id": rid, "first_seen": at, "samples": 0, "fresh_samples": 0,
                "peak_entry_score": None, "first_strong_at": None, "last_in_window_reason": None,
            }
        row = self.round_note
        row.update(last_seen=at, last_code=code, last_reason=reason)
        row["samples"] += 1
        row["fresh_samples"] += int(not frame.error and bool(frame.valid_quotes(at)))
        first_strong = row["first_strong_at"] is None and peak >= SCORE_FLOOR
        if first_strong:
            row["first_strong_at"] = at
        if peak:
            row["peak_entry_score"] = max(row["peak_entry_score"] or 0., peak)
        left = frame.market.ends_at - at
        if self.signals.settings.min_time_left_seconds <= left <= self.signals.settings.max_time_left_seconds:
            row["last_in_window_reason"] = reason
        if first_strong or new_round or at - self.last_note_write >= 10:
            self.flush_round()

    def flush_round(self):
        row = self.round_note
        if row:
            with self.store._lock, self.store.conn:
                self.store.conn.execute("""INSERT OR REPLACE INTO first_strong_rounds VALUES(
                    :round_id,:first_seen,:last_seen,:samples,:fresh_samples,:peak_entry_score,
                    :first_strong_at,:last_code,:last_reason,:last_in_window_reason)""", row)
            self.last_note_write = row["last_seen"]

    def _entry_block(self, market, side, rule):
        with self.store._lock:
            db = self.store.conn
            if db.execute("SELECT 1 FROM trades WHERE round_id=?", (market.round_id,)).fetchone():
                return "本局已成交，持有至官方结算，不再开仓"
            if db.execute("SELECT 1 FROM paper_orders WHERE status='PENDING' AND round_id=?",
                          (market.round_id,)).fetchone():
                return "本局已有委托，等待模拟成交确认"
            used = db.execute("""SELECT (SELECT count(*) FROM trades WHERE status='OPEN')+
                                        (SELECT count(*) FROM paper_orders WHERE status='PENDING')""").fetchone()[0]
            if used >= SLOTS:
                return "持仓和待确认委托已占满两笔名额"
            if self.store.account()["cash_balance"] + 1e-6 < BUDGET:
                return "可用模拟资金不足本笔 $5 预算"
        return None

    def enqueue_buy(self, market, signal, ceiling, rule, revision, info, *, evidence=None):
        if not qualified(signal):
            self.event("SCORE_WAIT", f"主账户方向评分 {signal.confidence:.4f}，等待评分达到 0.65")
            return None
        with self.lock:
            # Bind the submission to the second, post-book-read evaluation.
            self._checked = None
            try:
                return super().enqueue_buy(market, signal, ceiling, rule, revision, info, evidence=evidence)
            finally:
                self._checked = None

    def _entry_recheck(self, rule, fresh, info):
        again, limit, left = super()._entry_recheck(rule, fresh, info)
        if again.side in ("Up", "Down") and not qualified(again):
            raise ValueError(f"提交前方向评分 {again.confidence:.4f}，低于 0.65，等待新信号")
        self._checked = (fresh, again)
        return again, limit, left

    def _queue(self, market, signal, action, terms, revision, delay, trade_id=None, exit_reason=None):
        # Original PaperExecution serializes all pending orders across rounds.
        # Here two separate rounds may occupy slots; each pending buy locks its
        # own market and cash atomically, exactly like a filled position.
        if action != "BUY" or not self._checked or terms["amount"] != BUDGET:
            raise ValueError("本策略仅接受通过复查的 $5 模拟买单")
        fresh, checked = self._checked
        if fresh.market != market or checked.side != signal.side or not qualified(checked):
            raise ValueError("提交前主账户方向信号已变化")
        now = self.clock()
        if now >= market.ends_at:
            raise ValueError("本局已结束")
        payload = {**terms, "action": "BUY", "market": asdict(market), "round_id": market.round_id,
                   "token": market.up_token if checked.side == "Up" else market.down_token,
                   "side": checked.side, "signal": asdict(checked), "submitted_at": now,
                   "arrival_delay": delay, "revision": revision, "position_id": None,
                   "exit_reason": None, "version": VERSION, "strategy_key": KEY,
                   "signal_snapshot_id": fresh.snapshot_id, "signal_observed_at": fresh.observed_at,
                   "decision_quotes": {s: {k: q.get(k) for k in ("token_id", "best_bid", "best_ask",
                                             "source_ms", "received_ms")} for s, q in fresh.quotes.items()}}
        self.store.upsert_round(market)
        with self.store._lock, self.store.conn:
            block = self._entry_block(market, checked.side, self.rules)
            if block:
                raise ValueError(block)
            ident = self.store._insert_paper_order(market=market, side=checked.side, order_type="FAK",
                status="PENDING", limit_price=terms["limit"], post_only=False, expires_at=None,
                requested_cash=BUDGET, reserved_cash=BUDGET, remaining_cash=BUDGET, filled_shares=0,
                avg_fill_price=None, notional=0, fee=0, cash_spent=0, trade_id=None,
                confidence=checked.confidence, move_bps=checked.move_bps, reason=checked.reason, now=now)
            self.store.conn.execute("UPDATE account SET cash_balance=cash_balance-?,updated_at=? WHERE id=1",
                                    (BUDGET, now))
            self.store.conn.execute("INSERT INTO paper_sim_orders VALUES(?,'IN_FLIGHT',?,?)",
                                    (ident, now + delay, json.dumps(payload, allow_nan=False)))
        self._record_signal(checked)
        self.store.record_equity()
        self.event("IN_FLIGHT", f"已预留 $5 模拟资金，等待 {checked.side} 委托到达盘口")
        return ident

    def enqueue_sell(self, *args, **kwargs):
        raise ValueError("本策略只按官方结果结算")

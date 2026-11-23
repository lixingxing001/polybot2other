"""Isolated standard-forecast Paper arm requiring a second book update for a premium exit."""

from dataclasses import dataclass, replace

from .forecast_execution import ForecastPaperExecution
from .shared_market_inputs import quote_error


@dataclass
class PremiumCheck:
    row_id: int | None
    position_key: tuple
    first_book: dict
    confirmed_book: dict | None = None


class ForecastConfirmTrialExecution(ForecastPaperExecution):
    def _init_signals(self, base_settings, feed):
        super()._init_signals(base_settings, feed)
        self.premium_check = None
        with self.store._lock, self.store.conn:
            self.store.conn.execute("""CREATE TABLE IF NOT EXISTS forecast_premium_checks(
                id INTEGER PRIMARY KEY, round_id TEXT NOT NULL, trade_id INTEGER NOT NULL, side TEXT NOT NULL,
                first_at REAL NOT NULL, first_source_ms REAL NOT NULL, first_received_ms REAL NOT NULL,
                first_net_bid REAL NOT NULL, first_floor REAL NOT NULL, first_snapshot_id TEXT,
                confirmed_at REAL, confirmed_source_ms REAL, confirmed_received_ms REAL,
                confirmed_net_bid REAL, confirmed_floor REAL, confirmed_snapshot_id TEXT,
                finished_at REAL, outcome TEXT NOT NULL, reason TEXT, order_id INTEGER)""")
            self.store.conn.execute("""UPDATE forecast_premium_checks
                SET finished_at=?, outcome='interrupted', reason='模拟服务重启，重新等待两次报价'
                WHERE finished_at IS NULL""", (self.clock(),))

    def _trace(self, sql, parameters):
        # Instrumentation must not change whether an executable Paper order is sent.
        with self._trace_guard(), self.store._lock, self.store.conn:
            return self.store.conn.execute(sql, parameters).lastrowid

    def _finish_check(self, outcome, reason, order_id=None):
        check = self.premium_check
        if check is not None:
            self.premium_check = None
            self._trace("""UPDATE forecast_premium_checks SET finished_at=?,outcome=?,reason=?,order_id=?
                WHERE id=?""", (self.clock(), outcome, reason, order_id, check.row_id))

    def event(self, code, message):
        if code in {"DATA_WAIT", "EXECUTION_WAIT", "PAUSED"}:
            self._finish_check("invalidated", message)
        super().event(code, message)

    def _premium_decision(self, frame, decision, position):
        if decision.action != "SELL" or decision.min_net_bid is None:
            # A direction reversal is already an unconditional exit in the standard policy.
            self._finish_check("withdrawn", decision.reason)
            return decision
        side = position["side"]
        book = frame.quotes[side]
        now = self.clock()
        key = (position["round_id"], position["id"], side, position["shares"])
        invalid = frame.quote_error(side, now)
        if invalid or self.bot.paper_trading_paused or self.pending():
            reason = invalid or "已有模拟委托或已暂停，重新等待溢价信号"
            self._finish_check("invalidated", reason)
            return replace(decision, action="HOLD", reason=reason)

        check = self.premium_check
        if check:
            previous = check.confirmed_book or check.first_book
            if key != check.position_key or quote_error(frame.market, {side: previous}, side, now):
                self._finish_check("expired", "持仓已变化或上次盘口已过期")
                check = None
            elif book["source_ms"] < previous["source_ms"] or book["received_ms"] < previous["received_ms"]:
                self._finish_check("invalidated", "盘口时间倒退，重新等待两次报价")
                return replace(decision, action="HOLD", reason="盘口时间倒退，等待新盘口")
        values = (frame.observed_at, book["source_ms"], book["received_ms"],
                  decision.net_bid, decision.min_net_bid, frame.snapshot_id)
        if check is None:
            row_id = self._trace("""INSERT INTO forecast_premium_checks
                (round_id,trade_id,side,first_at,first_source_ms,first_received_ms,first_net_bid,first_floor,
                 first_snapshot_id,outcome) VALUES(?,?,?,?,?,?,?,?,?,'waiting')""", (*key[:3], *values))
            self.premium_check = PremiumCheck(row_id, key, book.copy())
            return replace(decision, action="HOLD", reason="首次达到溢价卖出条件，等待下一次买盘更新")

        if check.confirmed_book is None:
            if book["source_ms"] == check.first_book["source_ms"]:
                # Re-reading the REST/WS book can change received_ms or the frame's snapshot id.
                return replace(decision, action="HOLD", reason="买盘来源时间尚未更新，继续等待溢价确认")
            check.confirmed_book = book.copy()
            self._trace("""UPDATE forecast_premium_checks SET confirmed_at=?,confirmed_source_ms=?,
                confirmed_received_ms=?,confirmed_net_bid=?,confirmed_floor=?,confirmed_snapshot_id=?,
                outcome='confirmed' WHERE id=?""", (*values, check.row_id))
        return replace(decision, reason="新买盘确认净卖价仍高于模型估值，尝试卖出")

    def _decision(self, frame, info, position=None, net_bid=None):
        decision = self.forecast.evaluate(frame, info, position=position, net_bid=net_bid)
        decision = self._premium_decision(frame, decision, position)
        check = self.premium_check
        self.forecast_features = {
            **decision.as_features(), "max_entry_seconds": self.forecast.max_entry_seconds,
            "premium_check_id": check.row_id if check else None,
            "premium_first_source_ms": check.first_book["source_ms"] if check else None,
            "premium_confirmed_source_ms": check.confirmed_book["source_ms"] if check and check.confirmed_book else None,
        }
        self._record_signal(decision.as_signal())
        return decision

    def _queue(self, market, signal, action, terms, revision, delay, trade_id=None, exit_reason=None):
        order_id = super()._queue(market, signal, action, terms, revision, delay, trade_id, exit_reason)
        check = self.premium_check
        if check and check.confirmed_book and action == "SELL" and exit_reason == self.forecast_features.get("reason"):
            self._finish_check("submitted", exit_reason, order_id)
        else:
            self._finish_check("superseded", "已提交其他模拟委托")
        return order_id

"""Paper-only strategy orchestration. Matches, fees and payouts use the common ledger."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import logging

from .experiments import FORECAST_EARLY_PAPER_ID, FORECAST_LIMIT_060_PAPER_ID, FORECAST_LIMIT_CONTROL_PAPER_ID
from .forecast_forward import ForecastForwardAudit
from .forecast_observations import CHECKPOINTS, ForecastObservations
from .forecast_entry_trace import ForecastEntryTrace, ask_depth
from .forecast_paper import EARLY_MAX_ENTRY_SECONDS, LIMIT_060, MAX_ENTRY_SECONDS, MIN_ENTRY_SECONDS, ForecastPaperPolicy
from .paper_execution import PaperExecution, floor_to, minimum_sell_price, sweep_book


REALIZED_DRAWDOWN_BUDGET = 10.


class ForecastPaperExecution(PaperExecution):
    def _init_signals(self, base_settings, feed):
        max_entry_seconds = EARLY_MAX_ENTRY_SECONDS if self.strategy_id == FORECAST_EARLY_PAPER_ID else MAX_ENTRY_SECONDS
        trial = self.strategy_id in (FORECAST_LIMIT_060_PAPER_ID, FORECAST_LIMIT_CONTROL_PAPER_ID)
        self.forecast = ForecastPaperPolicy(feed.references, max_entry_seconds=max_entry_seconds,
            max_order_limit=LIMIT_060 if self.strategy_id == FORECAST_LIMIT_060_PAPER_ID else None)
        checkpoints = (EARLY_MAX_ENTRY_SECONDS, *CHECKPOINTS) if self.strategy_id == FORECAST_EARLY_PAPER_ID else CHECKPOINTS
        self.forecast_observations = ForecastObservations(self.store, checkpoints=checkpoints)
        self.entry_trace = None
        self._trace_error_logged = False
        self.forward_audit = ForecastForwardAudit(self) if trial else None
        if trial:
            self._init_realized_peak()
        if trial or self.strategy_id == FORECAST_EARLY_PAPER_ID:
            with self._trace_guard():
                self.entry_trace = ForecastEntryTrace(self.store, max_entry_seconds=max_entry_seconds)
        self.forecast_features = {}
        self.last_recorded_event_at = 0.

    def _init_realized_peak(self):
        # The account update and its new high must survive a crash together, including partial exits.
        with self.store._lock, self.store.conn:
            self.store.conn.execute("""CREATE TABLE IF NOT EXISTS forecast_realized_peak (
                id INTEGER PRIMARY KEY CHECK(id=1), pnl REAL NOT NULL, stopped_at REAL)""")
            self.store.conn.execute("""INSERT OR IGNORE INTO forecast_realized_peak(id,pnl)
                SELECT 1, max(0, realized_pnl, coalesce((SELECT max(realized_pnl) FROM equity_curve),0))
                FROM account WHERE id=1""")
            self.store.conn.execute("""CREATE TRIGGER IF NOT EXISTS forecast_record_realized_peak
                AFTER UPDATE OF realized_pnl ON account WHEN NEW.id=1
                BEGIN UPDATE forecast_realized_peak SET pnl=max(pnl,NEW.realized_pnl) WHERE id=1; END""")

    def _trial_entry_block(self, market, amount):
        if market.started_at < self.forward_audit.start_at:
            return "等待试验起算后的第一个完整市场"
        with self.store._lock, self.store.conn:
            # open_trades() intentionally omits sub-cent residuals; they still count here.
            row = self.store.conn.execute("""SELECT peak.stopped_at, peak.pnl - account.realized_pnl AS drawdown,
                    (SELECT coalesce(sum(stake),0) FROM trades WHERE status='OPEN') AS principal
                FROM account JOIN forecast_realized_peak AS peak ON peak.id=account.id
                WHERE account.id=1""").fetchone()
            ended = "空仓后剩余回撤额度不足 5 美元，本期模拟买入已结束，继续记录行情"
            if row["stopped_at"] is not None:
                return ended
            outstanding = row["principal"] + self.store.reserved_cash()
            if round(row["drawdown"] + outstanding + amount, 6) <= REALIZED_DRAWDOWN_BUDGET:
                return None
            if outstanding > 0:
                return "从历史盈利高点算起，剩余的 10 美元回撤额度不足以承担本单，暂停新买单"
            self.store.conn.execute("UPDATE forecast_realized_peak SET stopped_at=? WHERE id=1", (self.clock(),))
            return ended

    @contextmanager
    def _trace_guard(self):
        # Diagnostic failures must never prevent or repeat an execution.
        try:
            yield
        except Exception as exc:
            if not self._trace_error_logged:
                logging.getLogger(__name__).warning("Paper entry trace unavailable (%s)", type(exc).__name__)
                self._trace_error_logged = True

    def _observe_entry(self, frame, features, info):
        if self.entry_trace is not None:
            with self._trace_guard():
                self.entry_trace.observe(frame, features, info)

    @staticmethod
    def _quote_details(book, limit):
        book = book or {}
        at_best, in_limit = ask_depth(book, limit)
        return {
            "book_source_ms": book.get("source_ms"), "book_received_ms": book.get("received_ms"),
            "bid": book.get("best_bid"), "ask": book.get("best_ask"),
            "ask_size_at_best": at_best, "ask_size_in_limit": in_limit,
        }

    def _record_rejection(self, decision, evidence, exc):
        if self.entry_trace is None:
            return
        with self._trace_guard():
            book = evidence.get("submission_book")
            frame = evidence.get("recheck_frame")
            final = self.forecast_features if evidence.get("recheck_complete") else {}
            details = {
                "action": "BUY", "side": decision.side,
                "reason": str(exc)[:180] if isinstance(exc, ValueError) else type(exc).__name__,
                "candidate_decision_at": decision.decision_at, "candidate_snapshot_id": decision.snapshot_id,
                "candidate_score": decision.score, "candidate_ask": decision.entry_price,
                "candidate_all_in_cost": decision.all_in_cost, "limit_price": decision.limit,
                "book_checked_at": evidence.get("book_checked_at"),
                "book_token_matches": evidence.get("book_token_matches"),
                **self._quote_details(book, decision.limit),
                "recheck_decision_at": final.get("decision_at"), "recheck_score": final.get("score"),
                "recheck_side": final.get("side"), "recheck_action": final.get("action"),
                "recheck_reason": final.get("reason"), "recheck_limit": final.get("limit"),
                "recheck_all_in_cost": final.get("all_in_cost"),
            }
            # The synchronous order book and the strategy's recheck frame are different reads.
            if frame is not None:
                details["recheck_snapshot_id"] = frame.snapshot_id
                recheck_quote = self._quote_details(frame.quotes.get(decision.side), decision.limit)
                details.update({"recheck_" + key: value for key, value in recheck_quote.items()})
            self.entry_trace.record("pre_submit_reject", decision.round_id, self.clock(), details)

    def _observe_buy_arrival(self, order, now, book, consumed, fill):
        if self.entry_trace is None:
            return
        with self._trace_guard():
            limit = order["limit"]
            shares = fill["shares"]
            available = sum(max(0., float(level["size"])*self.policy.depth_fraction
                                - consumed.get(str(float(level["price"])), 0.))
                            for level in book.get("asks", []) if float(level["price"]) <= limit+1e-8)
            self.entry_trace.record("arrival_fill" if shares > 0 else "arrival_empty", order["round_id"], now, {
                "order_id": order["order_id"], "action": "BUY", "side": order["side"],
                "submitted_at": order["submitted_at"], "limit_price": limit,
                **self._quote_details(book, limit), "available_shares_in_limit": available,
                "matched_shares": shares, "matched_price": fill["price"] if shares > 0 else None,
                "fee": fill["fee"], "cash_spent": fill["cash"],
                "reason": None if shares > 0 else "FAK 到达时限价内没有可用深度",
            })

    def event(self, code, message):
        # Price-dependent explanations update every tick. Keep useful changes in the
        # ledger without writing the same type of holding/wait event every second.
        now = self.clock()
        if (code in {"FORECAST_HOLD", "SIGNAL_WAIT"} and code == self.last_event["code"]
                and now-self.last_recorded_event_at < 60):
            self.last_event = {"code": code, "message": message, "at": now}
            return
        super().event(code, message)
        self.last_recorded_event_at = now

    def _record_signal(self, signal):
        with self.bot._lock:
            self.bot.last_signal = asdict(signal)
            self.bot.last_decision = dict(self.forecast_features)

    def _decision(self, frame, info, position=None, net_bid=None):
        decision = self.forecast.evaluate(frame, info, position=position, net_bid=net_bid)
        self.forecast_features = {**decision.as_features(), "max_entry_seconds": self.forecast.max_entry_seconds}
        if self.forward_audit is not None:
            self.forecast_features.update(trial_strategy=self.strategy_id, trial_start_at=self.forward_audit.start_at,
                max_order_limit=self.forecast.max_order_limit, realized_drawdown_budget=REALIZED_DRAWDOWN_BUDGET)
        self._record_signal(decision.as_signal())
        return decision

    def _entry_recheck(self, rule, fresh, info):
        decision = self._decision(fresh, info)
        return decision.as_signal(), decision.limit if decision.action == "BUY" else None, MIN_ENTRY_SECONDS

    def _entry_block(self, market, side, rule):
        if self.forward_audit is not None:
            reason = self._trial_entry_block(market, rule["stake_dollars"])
            if reason:
                return reason
        with self.store._lock:
            prior = self.store.conn.execute(
                "SELECT 1 FROM trades WHERE round_id=? LIMIT 1", (market.round_id,)).fetchone()
        if prior:
            return "本局已经买过一次，等待下一局"
        return super()._entry_block(market, side, rule)

    def _queue(self, market, signal, action, terms, revision, delay, trade_id=None, exit_reason=None):
        terms = {**terms, "forecast_decision": self.forecast_features}
        if action == "BUY" and self.forward_audit is not None:
            with self.store._lock:
                if self.forecast.max_order_limit is not None and terms["limit"] > self.forecast.max_order_limit:
                    raise ValueError("原买入限价超过 60 美分，取消提交")
                block = self._trial_entry_block(market, terms["amount"])
                if block:
                    raise ValueError(block)
                return super()._queue(market, signal, action, terms, revision, delay, trade_id, exit_reason)
        return super()._queue(market, signal, action, terms, revision, delay, trade_id, exit_reason)

    def _net_bid(self, book, info, shares, limit):
        estimate = sweep_book(book, "SELL", limit, info, shares=shares, fraction=self.policy.depth_fraction)
        return estimate["cash"]/estimate["shares"] if estimate["shares"] > 0 else None

    def _sell_recheck(self, position, book, info, quantity, limit):
        fresh = self.frame_reader()
        now = self.clock()
        if (fresh.error or not fresh.market or fresh.market.round_id != position["round_id"]
                or not 0 <= now-fresh.observed_at <= 3 or now >= fresh.market.ends_at
                or self.bot.paper_trading_paused or (getattr(self.bot, "_stop", None) and self.bot._stop.is_set())):
            raise ValueError("卖出复查时行情或运行状态已变化，等待下一次判断")
        fresh = replace(fresh, quotes={**fresh.quotes, position["side"]: book})
        if fresh.quote_error(position["side"], now):
            raise ValueError("卖出复查时盘口已过期，等待新买盘")
        net_bid = self._net_bid(book, info, quantity, limit)
        decision = self._decision(fresh, info, position, net_bid)
        if decision.action != "SELL":
            raise ValueError(decision.reason)
        if decision.min_net_bid is not None:
            limit = max(limit, minimum_sell_price(decision.min_net_bid, info))
        if limit > book["best_bid"]+.000001:
            raise ValueError("当前买盘达不到策略要求的扣费净回款")
        return limit, decision.reason

    def decide(self):
        with self.lock:
            frame = self.bot._sync_shared_market_inputs()
            observed = False
            submitting_entry = False
            evidence = {} if self.entry_trace is not None else None
            try:
                if self.forward_audit is not None and frame.market and frame.market.started_at < self.forward_audit.start_at:
                    self.event("TRIAL_WAIT", "等待试验起算后的第一个完整市场")
                    return frame
                if not frame.market or frame.error:
                    self.event("DATA_WAIT", frame.error or "等待本局官方行情")
                    return frame
                info = self._info(frame.market)
                current = self.bot._sync_shared_market_inputs()
                if current.error or not current.market or current.market.round_id != frame.market.round_id:
                    self.event("DATA_WAIT", current.error or "市场已切换，等待新一局行情")
                    frame = current
                    return frame
                frame = current
                now = self.clock()
                if not 0 <= now-frame.observed_at <= 3 or now >= frame.market.ends_at:
                    self.event("DATA_WAIT", "本次行情已过期，等待新一局或新行情")
                    return frame
                position = next((row for row in self.store.open_trades() if row["round_id"] == frame.market.round_id), None)
                net_bid = None
                if position and not frame.quote_error(position["side"], now):
                    book = frame.quotes[position["side"]]
                    # Bound the estimated depth to the same price range as a sell order.
                    limit = max(float(info["tick_size"]), book["best_bid"]-.02)
                    net_bid = self._net_bid(book, info, floor_to(position["shares"], 2), limit)
                decision = self._decision(frame, info, position, net_bid)
                observed = True
                self._observe_entry(frame, self.forecast_features, info)
                self.forecast_observations.observe(frame, self.forecast_features)
                if self.bot.paper_trading_paused or (getattr(self.bot, "_stop", None) and self.bot._stop.is_set()):
                    self.event("PAUSED", "本策略模拟交易已暂停")
                    return frame
                if self.pending():
                    return frame
                if decision.action not in ("BUY", "SELL"):
                    self.event("FORECAST_HOLD" if position else "SIGNAL_WAIT", decision.reason)
                    return frame
                if now-self.last_attempt < 3:
                    return frame
                if position and decision.action == "SELL":
                    self.last_attempt = now
                    self.enqueue_sell(position, frame.market, decision.reason, recheck=lambda *args: self._sell_recheck(position, *args))
                elif not position and decision.action == "BUY":
                    config = self.config_reader()
                    rule = config["strategies"][self.strategy_id]
                    block = self._entry_block(frame.market, decision.side, rule)
                    if block:
                        self.forecast_observations.observe_entry_result(frame, block)
                        self.event("ENTRY_LIMIT", block)
                        return frame
                    self.last_attempt = now
                    submitting_entry = True
                    self.enqueue_buy(frame.market, decision.as_signal(), decision.limit, rule, config["revision"], info,
                                     evidence=evidence)
            except Exception as exc:
                if submitting_entry and evidence is not None:
                    self._record_rejection(decision, evidence, exc)
                if submitting_entry and isinstance(exc, ValueError):
                    entry_reason = ("提交前盘口已超过策略限价" if str(exc) == "提交前盘口已超过策略限价"
                                    else "提交前复查未通过")
                    self.forecast_observations.observe_entry_result(frame, entry_reason)
                reason = str(exc)[:180] if isinstance(exc, ValueError) else f"模拟预测暂时不可用（{type(exc).__name__}）"
                self.event("EXECUTION_WAIT", reason)
            finally:
                if (not observed and frame.market and
                        (self.forward_audit is None or frame.market.started_at >= self.forward_audit.start_at)):
                    self._observe_entry(frame, {}, None)
                    self.forecast_observations.observe(frame, {})
                if self.forward_audit is not None:
                    with self._trace_guard():
                        self.forward_audit.observe(frame, self.clock())
            return frame

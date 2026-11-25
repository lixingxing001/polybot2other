"""Isolated, paper-only execution shared by MAIN-R1 and its shadow candidates.

Original signal implementations are reused explicitly. No existing strategy
registration, live account, or default runner imports this module.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from dataclasses import asdict, replace
import hashlib
import json
import math
import threading
import time

from .main_r1_risk import MainR1Risk
from .managed_live_client import fee_amount
from .models import Signal
from .paper_execution import PaperExecution, VERSION, buy_terms, checked_info, sweep_book
from .shared_market_inputs import quote_error


KEY = "MAIN_R1_ADAPTIVE_PAPER_V1"
LABEL = "MAIN-R1"
ARRIVAL_MODEL = "r1-local-observation-v2"
EXECUTION_BASIS = "local_observation"
LEGACY_ARRIVAL_MODEL = "legacy-dual-clock-v1"
FLOOR = .60
CANDIDATES = {
    "MAIN": {"signal_family": "MAIN", "price_floor": None},
    "MAIN_FLOOR_060": {"signal_family": "MAIN", "price_floor": FLOOR},
    "FAST": {"signal_family": "MAIN_OPTIMIZED_FAST", "price_floor": None},
    "PLUS": {"signal_family": "MAIN_OPTIMIZED_PLUS", "price_floor": None},
}
SHADOW_IDS = {f"MAIN_R1_SHADOW_{key}_PAPER_V1" for key in CANDIDATES}
RULE = {"enabled": True, "stake_dollars": 5., "max_open_trades": 2, "max_daily_loss": 0.}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _arrival_contract(order):
    # Untagged persisted orders retain the policy under which they were submitted.
    if "arrival_model" not in order:
        return LEGACY_ARRIVAL_MODEL, "dual_clock"
    return order.get("arrival_model"), order.get("execution_basis")


def _finite_time(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def fixed_plan(candidate, frame):
    """An explicit shadow/control plan; the public runner supplies a real selector."""
    return {"candidate_key": candidate, **CANDIDATES[candidate],
            "selection_id": f"shadow:{candidate}:{getattr(frame.market, 'round_id', 'none')}",
            "regime": "control", "reason": "固定候选独立模拟对照"}


def _fee_safe_ceiling(confidence, min_edge, ceiling, info):
    """Highest legal MAIN price tick whose fee-inclusive cost preserves edge.

    The supported fee curves (rate <= 1, exponent >= 1) have a monotone
    price-plus-fee cost. Search integer ticks so rounding can never raise the
    submitted price above either the original ceiling or the net-edge cap.
    Actual budget-sized, per-level fee rounding is checked again after quoting.
    """
    if (not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                and math.isfinite(x) for x in (confidence, min_edge, ceiling))
            or not 0 <= confidence <= 1 or not 0 <= min_edge <= 1):
        raise ValueError("MAIN 净优势参数无效")
    info = checked_info(info)
    tick = float(info["tick_size"])
    rate, exponent = float(info["fee_rate"]), float(info["fee_exponent"])
    target = confidence - min_edge
    low, high = 1, math.floor(min(ceiling, 1-tick)/tick+1e-9)
    best = None
    while low <= high:
        middle = (low+high)//2
        price = round(middle*tick, 6)
        if price + fee_amount(1., price, rate, exponent) <= target+1e-12:
            best, low = price, middle+1
        else:
            high = middle-1
    return best


class MainR1Execution(PaperExecution):
    def __init__(self, bot, strategy_id, base_settings, *, frame_reader, info_reader,
                 book_reader, config_reader, feed, policy=None, clock=time.time,
                 entry_plan_reader=None):
        if strategy_id not in {KEY, *SHADOW_IDS}:
            raise ValueError("MAIN-R1 仅接受独立研究账本 ID")
        if base_settings.live_trading_runtime_enabled or base_settings.managed_live_enabled:
            raise ValueError("MAIN-R1 is PAPER_ONLY; live execution must be disabled")
        self.base_settings = base_settings
        self._decision_lock = threading.RLock()
        self.entry_plan_reader = entry_plan_reader or (lambda frame: fixed_plan("MAIN_FLOOR_060", frame))
        self.last_risk = {"allowed": False, "budget": 0., "nav": None, "reason": "等待账户估值"}
        self.last_observation_valid = False
        self._last_poll_risk_at = None
        self._last_poll_account = None
        self.frozen_config = deepcopy(config_reader())
        self.config_hash = _digest(self.frozen_config)
        self._assert_private_store(bot.store, strategy_id)
        super().__init__(bot, strategy_id, base_settings, frame_reader=frame_reader,
                         info_reader=info_reader, book_reader=book_reader,
                         config_reader=config_reader, feed=feed, policy=policy, clock=clock)

    @staticmethod
    def _assert_private_store(store, strategy_id):
        with store._lock:
            db = store.conn
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='main_r1_identity'").fetchone()
            if exists:
                row = db.execute("SELECT strategy_id FROM main_r1_identity WHERE id=1").fetchone()
                if not row or row[0] != strategy_id:
                    raise ValueError("研究账本所有者不匹配")
            else:
                used = db.execute("SELECT (SELECT count(*) FROM trades)+(SELECT count(*) FROM paper_orders)").fetchone()[0]
                simulated = db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_sim_run'").fetchone()
                if used or simulated:
                    raise ValueError("拒绝复用其他策略的模拟账本")
            account = store.account()
            if account["initial_balance"] != 1000:
                raise ValueError("MAIN-R1 研究账本必须使用 1000 虚拟初始资金")

    def _create_schema(self):
        super()._create_schema()
        with self.store._lock, self.store.conn:
            self.store.conn.executescript("""
                CREATE TABLE IF NOT EXISTS main_r1_identity(
                    id INTEGER PRIMARY KEY CHECK(id=1), strategy_id TEXT NOT NULL,
                    config_hash TEXT NOT NULL, config_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS main_r1_round_locks(
                    round_id TEXT PRIMARY KEY, state TEXT NOT NULL, order_id INTEGER,
                    updated_at REAL NOT NULL, reason TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS main_r1_one_fill ON trades(round_id);
                CREATE UNIQUE INDEX IF NOT EXISTS main_r1_one_pending ON paper_orders(round_id)
                    WHERE status='PENDING';
                CREATE TABLE IF NOT EXISTS main_r1_decisions(
                    id INTEGER PRIMARY KEY, observed_at REAL NOT NULL, round_id TEXT,
                    snapshot_id TEXT, code TEXT NOT NULL, order_id INTEGER,
                    detail_json TEXT NOT NULL);
            """)
            self.store.conn.execute("INSERT OR IGNORE INTO main_r1_identity VALUES(1,?,?,?)",
                                    (self.strategy_id, self.config_hash, _json(self.frozen_config)))
            saved = self.store.conn.execute("SELECT config_hash FROM main_r1_identity WHERE id=1").fetchone()[0]
            if saved != self.config_hash:
                raise ValueError("研究配置已冻结；不同配置必须使用新的独立实验目录")
        self.guard = MainR1Risk(self.store, clock=self.clock)

    def _config(self):
        value = deepcopy(self.config_reader())
        if _digest(value) != self.config_hash:
            raise ValueError("配置已变化，冻结的研究不接受运行中改参数")
        if self.strategy_id not in value.get("strategies", {}):
            raise ValueError("缺少独立策略规则")
        return value

    def _frame(self, raw, info=None):
        """Seal nested dictionaries and bind public market rules to this book frame."""
        frame = deepcopy(raw)
        if not frame.market or info is None:
            return frame
        with getattr(self.feed, "lock", nullcontext()):
            rules = deepcopy(getattr(self.feed, "rules", {}))
            checked = getattr(self.feed, "rules_checked_at", 0.)
        for side, book in frame.quotes.items():
            if not book.get("tick_size") or not book.get("min_order_size"):
                rule = rules.get(side, {})
                if (0 <= self.clock() - checked <= 60
                        and str(rule.get("token_id")) == str(book.get("token_id"))
                        and str(rule.get("tick_size")) == str(info["tick_size"])):
                    book.setdefault("tick_size", rule["tick_size"])
                    book.setdefault("min_order_size", rule["min_order_size"])
        identity = {"source_snapshot_id": frame.snapshot_id, "market": asdict(frame.market),
                    "observed_at": frame.observed_at, "reference": frame.reference,
                    "price": frame.price, "quotes": frame.quotes, "info": info}
        return replace(frame, snapshot_id=_digest(identity))

    def _audit(self, frame, code, *, order_id=None, **details):
        summary = None
        if frame is not None:
            summary = {"observed_at": frame.observed_at, "price": frame.price,
                       "reference": frame.reference, "error": frame.error,
                       "quotes": {s: {k: q.get(k) for k in ("token_id", "best_bid", "best_ask",
                           "source_ms", "received_ms", "ask_size", "bid_size", "tick_size", "min_order_size")}
                                  for s, q in frame.quotes.items()}}
        with self.store._lock, self.store.conn:
            model, basis = ARRIVAL_MODEL, EXECUTION_BASIS
            if order_id is not None:
                saved = self.store.conn.execute("SELECT payload FROM paper_sim_orders WHERE order_id=?", (order_id,)).fetchone()
                if saved:
                    model, basis = _arrival_contract(json.loads(saved[0]))
            payload = {"config_hash": self.config_hash, "strategy_id": self.strategy_id,
                       "arrival_model": model, "execution_basis": basis,
                       "frame": summary, **details}
            self.store.conn.execute("INSERT INTO main_r1_decisions(observed_at,round_id,snapshot_id,code,order_id,detail_json) VALUES(?,?,?,?,?,?)",
                (self.clock(), getattr(getattr(frame, "market", None), "round_id", None) or details.get("round_id"),
                 getattr(frame, "snapshot_id", None), code, order_id, _json(payload)))

    def _plan(self, frame):
        plan = deepcopy(self.entry_plan_reader(frame))
        if plan is None:
            return None
        if not isinstance(plan, dict) or plan.get("candidate_key") not in CANDIDATES:
            raise ValueError("未知的候选策略")
        expected = CANDIDATES[plan["candidate_key"]]
        if any(plan.get(key) != value for key, value in expected.items()):
            raise ValueError("候选的信号家族或价格下限与冻结定义不匹配")
        if not isinstance(plan.get("selection_id"), str) or not plan["selection_id"]:
            raise ValueError("缺少可追溯的策略选择 ID")
        return plan

    def _entry_block(self, market, side=None, rule=None):
        with self.store._lock:
            row = self.store.conn.execute("SELECT state FROM main_r1_round_locks WHERE round_id=?", (market.round_id,)).fetchone()
            if row and row[0] != "RELEASED":
                return "本轮已成交或存在在途/未知订单，不再开仓"
            if self.store.conn.execute("SELECT 1 FROM trades WHERE round_id=?", (market.round_id,)).fetchone():
                return "本轮已有成交，整轮锁定"
            if self.store.conn.execute("SELECT 1 FROM paper_orders WHERE round_id=? AND status='PENDING'", (market.round_id,)).fetchone():
                return "本轮订单尚未确定"
        return None

    def _account_state(self, frame, info):
        """One reconciled checkpoint; missing marks are an error, never zero/cost."""
        now = self.clock()
        with self.store._lock:
            db = self.store.conn
            account = dict(db.execute("SELECT * FROM account WHERE id=1").fetchone())
            # Keep historical validation in aggregate SQL. Do not materialize
            # and JSON-hash the entire growing ledger on each risk heartbeat.
            pending = [dict(r) for r in db.execute("SELECT * FROM paper_orders WHERE status='PENDING'")]
            phases = {r["order_id"]: dict(r) for r in db.execute("SELECT * FROM paper_sim_orders WHERE phase IN ('IN_FLIGHT','CONFIRMING','UNKNOWN')")}
            opened = [dict(r) for r in db.execute("SELECT * FROM trades WHERE status='OPEN'")]
            confirmed_cost = db.execute("SELECT coalesce(sum(cash_spent),0) FROM paper_orders WHERE status IN ('FILLED','PARTIAL')").fetchone()[0]
            total_stake, closed_pnl = db.execute("SELECT coalesce(sum(stake),0),coalesce(sum(CASE WHEN status='SETTLED' THEN pnl ELSE 0 END),0) FROM trades").fetchone()
            bad_order = db.execute("SELECT 1 FROM paper_orders WHERE order_type!='FAK' LIMIT 1").fetchone()
            bad_trade = db.execute("SELECT 1 FROM trades WHERE status NOT IN ('OPEN','SETTLED') OR (status='SETTLED' AND (settlement_source!='polymarket_official' OR settlement_source IS NULL)) LIMIT 1").fetchone()
            bad_claim = db.execute("""SELECT 1 FROM paper_sim_claims c LEFT JOIN trades t ON t.id=c.trade_id
                WHERE t.id IS NULL OR t.status!='SETTLED' OR t.settlement_source IS NULL
                    OR t.settlement_source!='polymarket_official' OR c.amount<=0
                    OR t.payout IS NULL OR abs(c.amount-t.payout)>.000001 LIMIT 1""").fetchone()
            missing_claim = db.execute("""SELECT 1 FROM trades t LEFT JOIN paper_sim_claims c ON c.trade_id=t.id
                WHERE t.status='SETTLED' AND t.payout>0 AND c.trade_id IS NULL LIMIT 1""").fetchone()
            unrealized_claims, released = db.execute("""SELECT
                coalesce(sum(CASE WHEN released_at IS NULL THEN amount ELSE 0 END),0),
                coalesce(sum(CASE WHEN released_at IS NOT NULL THEN amount ELSE 0 END),0)
                FROM paper_sim_claims""").fetchone()
        for order in pending:
            row = phases.get(order["id"])
            if not row or row["phase"] not in ("IN_FLIGHT", "CONFIRMING"):
                raise ValueError("订单状态未知，账户尚未对账")
            if row["phase"] == "CONFIRMING" or now >= row["due_at"]:
                raise ValueError("成交尚未确认，暂停新风险直到对账")
        if any(row["phase"] == "UNKNOWN" for row in phases.values()):
            raise ValueError("未知订单保留预算，等待人工复核")
        reserved = sum(float(o["remaining_cash"]) for o in pending)
        if bad_order:
            raise ValueError("研究账本出现不支持的订单类型")
        if bad_trade:
            raise ValueError("持仓状态或结算来源无法核实")
        if abs(confirmed_cost - total_stake) > .00002:
            raise ValueError("订单成本与持仓账本不一致")
        if bad_claim:
            raise ValueError("结算领取凭据无法核实")
        if missing_claim:
            raise ValueError("官方兑付与领取凭据不完整，暂停新增风险")
        expected_cash = account["initial_balance"] - confirmed_cost - reserved + released
        cash = float(account["cash_balance"])
        if not math.isfinite(cash) or cash < -.000001 or abs(cash - expected_cash) > .00002:
            raise ValueError("现金与订单/领取流水未闭环")
        if abs(closed_pnl - account["realized_pnl"]) > .00002:
            raise ValueError("已实现损益账本未闭环")
        marks = []
        for trade in opened:
            if (frame is None or frame.error or frame.market is None
                    or frame.market.round_id != trade["round_id"] or now >= frame.market.ends_at or not info):
                raise ValueError("未决仓位缺少当前可执行估值，等待盘口或官方结算")
            book = frame.quotes.get(trade["side"], {})
            error = quote_error(frame.market, frame.quotes, trade["side"], now)
            if error:
                raise ValueError(error)
            minimum = book.get("min_order_size")
            if (not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum <= 0
                    or float(trade["shares"])+.000001 < minimum):
                raise ValueError("持仓低于可执行最小份数或缺少市场规则，等待官方结算")
            value = sweep_book(book, "SELL", 0, info, shares=float(trade["shares"]), fraction=1.)
            if not value["full"] or value["shares"] + .000001 < trade["shares"]:
                raise ValueError("可执行买盘深度不足全仓估值")
            marks.append({"trade_id": trade["id"], "round_id": trade["round_id"],
                          "shares": trade["shares"], "net_value": value["cash"], "fee": value["fee"],
                          "levels": value["levels"], "snapshot_id": frame.snapshot_id})
        rounds = {r["round_id"] for r in opened} | {r["round_id"] for r in pending}
        exposure = round(sum(float(r["stake"]) for r in opened) + reserved, 6)
        nav = round(cash + reserved + sum(x["net_value"] for x in marks) + unrealized_claims, 6)
        ledger_version = _digest({"account": account, "pending": pending, "opened": opened,
                                  "confirmed_cost": confirmed_cost, "reserved": reserved,
                                  "claims": unrealized_claims, "released": released})
        return {"nav": nav, "available_cash": cash, "reserved_cash": reserved,
                "pending_claims": unrealized_claims, "exposure": exposure,
                "unresolved_rounds": len(rounds), "marks": marks,
                "as_of": now, "ledger_version": ledger_version}

    def risk_checkpoint(self, frame=None):
        with self.lock:
            try:
                frame = self._frame(frame if frame is not None else self.frame_reader())
                info = self.info_cache.get(frame.market.round_id) if frame and frame.market else None
                if info and any(not q.get("min_order_size") or not q.get("tick_size") for q in frame.quotes.values()):
                    frame = self._frame(frame, info)
                state = self._account_state(frame, info)
                decision = self.guard.evaluate(nav=state["nav"], available_cash=state["available_cash"],
                    exposure=state["exposure"], unresolved_rounds=state["unresolved_rounds"], now=self.clock())
                self.last_risk = {**state, **decision, "nav": state["nav"], "valuation_status": "verified"}
            except (ValueError, TypeError, KeyError) as exc:
                self.last_risk = {"allowed": False, "budget": 0., "nav": None,
                                  "valuation_status": "unavailable", "reason": str(exc), "as_of": self.clock()}
            return deepcopy(self.last_risk)

    def decide(self):
        # Public metadata can block on the network. Keep it outside the matching
        # lock so a refresh cannot make an already submitted order miss arrival.
        with self._decision_lock:
            # A technical failure is not a legitimate no-trade training sample.
            self.last_observation_valid = False
            frame = self._frame(self.bot._sync_shared_market_inputs())
            try:
                config = self._config()
                if not frame.market or frame.error:
                    with self.lock:
                        risk = self.risk_checkpoint(frame)
                        self._audit(frame, "DATA_WAIT", reason=frame.error or "等待市场", risk=risk)
                        self.event("DATA_WAIT", frame.error or "等待市场")
                    return frame
                # Metadata is read BEFORE the final input capture. Price/size and
                # floor never come from a separate earlier REST book.
                info = checked_info(self.info_reader(frame.market))
                expected_round = frame.market.round_id
                with self.lock:
                    frame = self._frame(self.frame_reader(), info)
                    if not frame.market or frame.market.round_id != expected_round or frame.error:
                        raise ValueError(frame.error or "市场已切换，重新读取参数")
                    self.info_cache[expected_round] = info
                    self._decide_frame(frame, info, config)
                    self.last_observation_valid = self.last_risk.get("valuation_status") == "verified"
                    if not self.last_observation_valid:
                        # A known positive fill or in-flight intent already blocks
                        # every further entry in this round; awaiting confirmation
                        # does not create an unobserved new entry opportunity.
                        with self.store._lock:
                            row = self.store.conn.execute("""SELECT l.state,s.phase FROM main_r1_round_locks l
                                LEFT JOIN paper_sim_orders s ON s.order_id=l.order_id WHERE l.round_id=?""",
                                (frame.market.round_id,)).fetchone()
                        self.last_observation_valid = bool(row and row["state"] in ("PENDING", "CONSUMED")
                            and row["phase"] in ("IN_FLIGHT", "CONFIRMING", "FILLED", "PARTIAL"))
            except (ValueError, TypeError, KeyError) as exc:
                with self.lock:
                    self._audit(frame, "REJECTED", reason=str(exc))
                    self.event("EXECUTION_WAIT", str(exc)[:240])
            return frame

    def enqueue_buy(self, market, signal, ceiling, rule, revision, info, *, evidence=None):
        # Compatibility entrypoint; external candidates are always re-evaluated.
        with self._decision_lock:
            config = self._config()
            if config.get("revision") != revision:
                raise ValueError("配置版本已变化")
            info = checked_info(self.info_reader(market))
            with self.lock:
                frame = self._frame(self.frame_reader(), info)
                if frame.error or not frame.market or frame.market.round_id != market.round_id:
                    raise ValueError(frame.error or "市场已切换")
                self.info_cache[market.round_id] = info
                return self._decide_frame(frame, info, config, expected_side=signal.side)

    def _decide_frame(self, frame, info, config, expected_side=None):
        now = self.clock()
        if not 0 <= now-frame.observed_at <= min(3., self.base_settings.max_quote_age_ms/1000):
            raise ValueError("最终行情帧过期或来自未来")
        risk = self.risk_checkpoint(frame)
        if self.bot.paper_trading_paused or (getattr(self.bot, "_stop", None) and self.bot._stop.is_set()):
            self._audit(frame, "PAUSED", risk=risk)
            self.event("PAUSED", "已暂停新增风险，原订单继续确认结算")
            return None
        if not config["strategies"][self.strategy_id].get("enabled", True):
            self._audit(frame, "DISABLED", risk=risk)
            return None
        plan = self._plan(frame)
        if plan is None:
            self._audit(frame, "ABSTAIN", risk=risk, reason="本轮没有通过证据门槛的候选")
            self.event("ABSTAIN", "证据不足或行情不适合，MAIN-R1 本轮空仓")
            return None
        if not risk["allowed"]:
            self._audit(frame, "RISK_WAIT", risk=risk, plan=plan)
            self.event("RISK_WAIT", risk["reason"])
            return None
        rule = {**config["strategies"][self.strategy_id], **RULE, "stake_dollars": risk["budget"]}
        self.rules = rule
        signal, ceiling = self.signals.evaluate(plan["signal_family"], rule, frame.market,
            frame.reference, frame.quotes, frame.observed_at, info, market_input=frame)
        self._record_signal(signal)
        if expected_side and signal.side != expected_side:
            raise ValueError("提交前复查方向信号已变化")
        if signal.side not in ("Up", "Down") or ceiling is None:
            self._audit(frame, "SIGNAL_WAIT", signal=asdict(signal), plan=plan, risk=risk,
                        features=deepcopy(getattr(self.signals, "last_features", {})))
            self.event("SIGNAL_WAIT", signal.reason)
            return None
        block = self._entry_block(frame.market, signal.side, rule)
        if block:
            self._audit(frame, "ROUND_LOCKED", signal=asdict(signal), plan=plan, reason=block)
            self.event("ROUND_LOCKED", block)
            return None
        error = quote_error(frame.market, frame.quotes, signal.side, now)
        if error:
            raise ValueError(error)
        book = frame.quotes[signal.side]
        net_edge = None
        if plan["signal_family"] == "MAIN":
            safe_ceiling = _fee_safe_ceiling(signal.confidence, self.base_settings.min_edge, ceiling, info)
            net_edge = {"score": signal.confidence, "min_edge": self.base_settings.min_edge,
                        "original_ceiling": ceiling, "fee_safe_ceiling": safe_ceiling}
            if safe_ceiling is None or book["best_ask"] > safe_ceiling+1e-8:
                self._audit(frame, "EDGE_WAIT", signal=asdict(signal), plan=plan, risk=risk,
                            book=book, info=info, net_edge=net_edge)
                self.event("EDGE_WAIT", "手续费后优势不足，跳过本帧并继续等待")
                return None
            ceiling = safe_ceiling
        terms = buy_terms(risk["budget"], ceiling, info, book)
        quote = sweep_book(book, "BUY", terms["limit"], info, notional_budget=terms["notional_budget"])
        if quote["shares"] <= 0 or not quote["full"]:
            raise ValueError("最终盘口不足以报价本笔预算")
        if now >= frame.market.ends_at-self.base_settings.min_time_left_seconds:
            raise ValueError("提交前复查已离开 MAIN 入场时窗")
        details = {"signal": asdict(signal), "plan": plan, "risk": risk, "quote": quote,
                   "book": book, "info": info, "limit": terms["limit"],
                   "net_edge": net_edge,
                   "features": deepcopy(getattr(self.signals, "last_features", {}))}
        if net_edge is not None:
            all_in_price = quote["cash"]/quote["shares"]
            net_edge.update(quote_all_in_price=all_in_price,
                            quote_net_edge=signal.confidence-all_in_price)
            # quote.cash already includes rounded per-level fees. Do not round
            # away a real shortfall in the budget-sized per-share net edge.
            if net_edge["quote_net_edge"]+1e-12 < net_edge["min_edge"]:
                self._audit(frame, "EDGE_WAIT", **details)
                self.event("EDGE_WAIT", "逐档报价后的净优势不足，跳过本帧并继续等待")
                return None
        if plan["price_floor"] is not None and quote["cash"] < round(plan["price_floor"]*quote["shares"], 6):
            self._audit(frame, "FLOOR_WAIT", **details)
            self.event("FLOOR_WAIT", "预计含费均价低于 0.60，跳过本帧并继续等待")
            return None
        self._config()  # Freeze the exact same configuration through reservation.
        terms.update(r1_plan=plan, r1_quote=quote, r1_frame=frame.snapshot_id,
                     r1_observed_at=frame.observed_at, r1_config_hash=self.config_hash,
                     r1_decision=details)
        ident = self._queue(frame.market, signal, "BUY", terms, config["revision"],
                            max(self.policy.arrival_floor_seconds, self.clock()-frame.observed_at))
        self._audit(frame, "SUBMITTED", order_id=ident, **details)
        return ident

    def _queue(self, market, signal, action, terms, revision, delay, trade_id=None, exit_reason=None):
        if action != "BUY" or not terms.get("r1_plan") or not terms.get("r1_frame"):
            raise ValueError("MAIN-R1 只接受完整同帧检查后的模拟买单")
        now = self.clock()
        if now >= market.ends_at-self.base_settings.min_time_left_seconds or now-terms["r1_observed_at"] > 3:
            raise ValueError("提交前行情或时窗已过期")
        amount = terms["amount"]
        if not 0 < amount <= 5:
            raise ValueError("单笔预算超出研究上限")
        payload = {**terms, "action": "BUY", "market": asdict(market), "round_id": market.round_id,
                   "token": market.up_token if signal.side == "Up" else market.down_token,
                   "side": signal.side, "signal": asdict(signal), "submitted_at": now,
                   "arrival_delay": delay, "revision": revision, "position_id": None,
                   "arrival_model": ARRIVAL_MODEL, "execution_basis": EXECUTION_BASIS,
                   "arrival_at": now+delay,
                   "arrival_deadline": now+delay+self.policy.max_poll_lateness_seconds,
                   "exit_reason": None, "version": VERSION, "strategy_key": self.strategy_id,
                   "signal_snapshot_id": terms["r1_frame"]}
        self.store.upsert_round(market)
        with self.store._lock, self.store.conn:
            self.store.conn.execute("BEGIN IMMEDIATE")
            block = self._entry_block(market, signal.side, self.rules)
            if block:
                raise ValueError(block)
            # The executor lock serializes poll, settlement and decisions. The
            # database transaction also protects the reserve from another writer.
            cash = self.store.conn.execute("SELECT cash_balance FROM account WHERE id=1").fetchone()[0]
            reserved = self.store.conn.execute("SELECT coalesce(sum(remaining_cash),0) FROM paper_orders WHERE status='PENDING'").fetchone()[0]
            exposure = reserved+self.store.conn.execute("SELECT coalesce(sum(stake),0) FROM trades WHERE status='OPEN'").fetchone()[0]
            count = self.store.conn.execute("SELECT count(*) FROM (SELECT round_id FROM trades WHERE status='OPEN' UNION SELECT round_id FROM paper_orders WHERE status='PENDING')").fetchone()[0]
            checkpoint = terms["r1_decision"]["risk"]
            if (abs(cash-checkpoint["available_cash"]) > .000001 or abs(exposure-checkpoint["exposure"]) > .000001
                    or cash+.000001 < amount or exposure+amount > .01*checkpoint["nav"]+.000001 or count >= 2):
                raise ValueError("预算或账户状态已变化，必须重新完整计算")
            ident = self.store._insert_paper_order(market=market, side=signal.side, order_type="FAK",
                status="PENDING", limit_price=terms["limit"], post_only=False, expires_at=None,
                requested_cash=amount, reserved_cash=amount, remaining_cash=amount, filled_shares=0,
                avg_fill_price=None, notional=0, fee=0, cash_spent=0, trade_id=None,
                confidence=signal.confidence, move_bps=signal.move_bps, reason=signal.reason, now=now)
            self.store.conn.execute("UPDATE account SET cash_balance=cash_balance-?,updated_at=? WHERE id=1", (amount, now))
            self.store.conn.execute("INSERT INTO paper_sim_orders VALUES(?,'IN_FLIGHT',?,?)", (ident, now+delay, _json(payload)))
            self.store.conn.execute("INSERT INTO main_r1_round_locks VALUES(?,'PENDING',?,?,?) ON CONFLICT(round_id) DO UPDATE SET state='PENDING',order_id=excluded.order_id,updated_at=excluded.updated_at,reason=excluded.reason",
                (market.round_id, ident, now, "模拟订单在途，最大预算已预留"))
        self.store.record_equity()
        self.event("IN_FLIGHT", "MAIN-R1 已原子占位并预留模拟资金，等待到达盘口")
        return ident

    def _consume(self, order, fill):
        if fill.get("shares", 0) <= 0:
            return
        with self.store._lock, self.store.conn:
            self.store.conn.execute("INSERT INTO main_r1_round_locks VALUES(?,'CONSUMED',?,?,?) ON CONFLICT(round_id) DO UPDATE SET state='CONSUMED',order_id=excluded.order_id,updated_at=excluded.updated_at,reason=excluded.reason",
                (order["round_id"], order["order_id"], self.clock(), "任意正成交永久消耗整轮"))
        plan = order.get("r1_plan", {})
        floor = plan.get("price_floor")
        if floor is not None and fill["cash"] < round(floor*fill["shares"], 6):
            self.guard.halt("floor_drift", now=self.clock())
        net_edge = order.get("r1_decision", {}).get("net_edge")
        if net_edge is not None:
            actual_edge = net_edge["score"]-fill["cash"]/fill["shares"]
            if actual_edge+1e-12 < net_edge["min_edge"]:
                # Keep a partial fill and its real rounded costs in the ledger.
                # A decision threshold must never delete a losing observation.
                self.guard.halt("net_edge_drift", now=self.clock())

    def _arrive(self, order):
        """Match the first eligible observation processed in the fixed window.

        Exchange source time remains freshness evidence, not a required update
        after the hypothetical arrival. Untagged legacy orders keep both clock
        lower bounds. Missing evidence never proves a terminal zero fill.
        """
        now = self.clock()
        arrival_at = order["submitted_at"]+order["arrival_delay"]
        deadline_at = arrival_at+self.policy.max_poll_lateness_seconds
        if now < arrival_at:
            return
        if now > deadline_at:
            self._finish_empty(order, "处理已超过固定到达窗口，保持未知订单占位")
            return
        model, basis = _arrival_contract(order)
        if (model, basis) not in ((ARRIVAL_MODEL, EXECUTION_BASIS), (LEGACY_ARRIVAL_MODEL, "dual_clock")):
            self._finish_empty(order, "到达观察模型无法核实，保持未知订单占位",
                               failure_code="ARRIVAL_MODEL_UNSUPPORTED")
            return
        # WS depth has no tick/minimum metadata. Reuse only fresh, token-bound
        # public rules compatible with the metadata frozen into this order.
        raw_frame = None
        try:
            raw_frame = self.frame_reader()
            frame = self._frame(raw_frame, order["info"])
        except (ValueError, TypeError, KeyError, OverflowError):
            self._finish_empty(order, "到达盘口或市场规则无法核实，保持未知订单占位",
                               failure_code="ARRIVAL_BOOK_INVALID",
                               arrival_evidence=self._arrival_evidence(order, raw_frame))
            return
        now = self.clock()
        evidence = self._arrival_evidence(order, frame, now)
        if now > deadline_at:
            self._finish_empty(order, "读取盘口后已超过固定到达窗口，保持未知订单占位",
                               failure_code="ARRIVAL_CAPTURE_LATE", arrival_evidence=evidence)
            return
        market = frame.market
        if frame.error or not market or market.round_id != order["round_id"] or now >= market.ends_at:
            self._finish_empty(order, "缺少本轮到达时盘口，保持未知订单占位",
                               failure_code="ARRIVAL_MARKET_UNAVAILABLE", arrival_evidence=evidence)
            return
        book = frame.quotes.get(order["side"], {})
        if any(_finite_time(book.get(key)) is None for key in ("source_ms", "received_ms")):
            self._finish_empty(order, "到达盘口时间无法核实，保持未知订单占位",
                               failure_code="ARRIVAL_BOOK_INVALID", arrival_evidence=evidence)
            return
        error = frame.quote_error(order["side"], now)
        if error or str(book.get("token_id")) != str(order["token"]):
            self._finish_empty(order, "到达盘口过期或代币不匹配，保持未知订单占位",
                               failure_code="ARRIVAL_BOOK_STALE_OR_MISMATCH", arrival_evidence=evidence)
            return
        before_arrival = book["received_ms"] < arrival_at*1000
        if model == LEGACY_ARRIVAL_MODEL:
            before_arrival = before_arrival or book["source_ms"] < arrival_at*1000
        if before_arrival:
            if now >= deadline_at:
                self._finish_empty(order, "固定到达窗口结束仍无合格到达收据，保持未知订单占位",
                                   failure_code="ARRIVAL_WINDOW_EXHAUSTED", arrival_evidence=evidence)
                return
            # Leave due_at fixed. Waiting may not extend the opportunity window,
            # release the reservation, or turn missing evidence into zero fills.
            self._audit(frame, "ARRIVAL_WAIT", order_id=order["order_id"],
                        arrival=evidence,
                        required_clock="source_and_received" if model == LEGACY_ARRIVAL_MODEL else "received")
            self.event("ARRIVAL_WAIT", "缺少符合本单观察模型的到达收据，固定窗口内继续等待；预算保持占用")
            return
        if str(book.get("tick_size")) != str(order["info"]["tick_size"]):
            self._finish_empty(order, "到达时价格精度无法核实，保持未知订单占位", arrival_evidence=evidence)
            return
        if order["action"] != "BUY":
            self._finish_empty(order, "研究订单类型无法核实，保持未知订单占位", arrival_evidence=evidence)
            return
        raw = book.get("asks")
        if not isinstance(raw, (list, tuple)) or not raw:
            self._finish_empty(order, "到达盘口缺少逐档深度，保持未知订单占位", arrival_evidence=evidence)
            return
        try:
            fingerprint = hashlib.sha256(_json(raw).encode()).hexdigest()
            with self.store._lock:
                previous = self.store.conn.execute(
                    "SELECT * FROM paper_sim_consumed WHERE token=? AND action='BUY'",
                    (order["token"],)).fetchone()
            consumed = json.loads(previous["levels"]) if previous and previous["fingerprint"] == fingerprint else {}
            if not isinstance(consumed, dict):
                raise ValueError("深度消费凭据格式无效")
            fill = sweep_book(book, "BUY", order["limit"], order["info"],
                              notional_budget=order["notional_budget"],
                              fraction=self.policy.depth_fraction, consumed=consumed)
        except (ValueError, TypeError, KeyError, OverflowError):
            self._finish_empty(order, "到达盘口档位无法核实，保持未知订单占位", arrival_evidence=evidence)
            return
        if fill["shares"] <= 0:
            self._finish_empty(order, "已核实 FAK 到达时限价内零成交", verified_zero_fill=True)
            self._observe_buy_arrival(order, now, book, consumed, fill)
            return
        if fill["cash"] > order["amount"]+.000001:
            self._finish_empty(order, "撮合费用超出预留预算，保持未知订单占位", arrival_evidence=evidence)
            return
        consumed_after = dict(consumed)
        for level in fill["levels"]:
            key = str(level["price"])
            consumed_after[key] = consumed_after.get(key, 0)+level["shares"]
        payload = {**order, "fill": fill, "matched_at": now, "arrival_evidence": evidence,
                   "arrival_book": {k: book.get(k) for k in
                                    ("source_ms", "received_ms", "best_bid", "best_ask", "bids", "asks")}}
        due = max(now+.1, order["submitted_at"]+self.policy.confirmation_seconds)
        with self.store._lock, self.store.conn:
            cursor = self.store.conn.execute(
                "UPDATE paper_sim_orders SET phase='CONFIRMING',due_at=?,payload=? WHERE order_id=? AND phase='IN_FLIGHT'",
                (due, _json(payload), order["order_id"]))
            if cursor.rowcount != 1:
                return
            self.store.conn.execute("INSERT OR REPLACE INTO paper_sim_consumed VALUES(?,'BUY',?,?)",
                                    (order["token"], fingerprint, _json(consumed_after)))
            self.store.conn.execute(
                "UPDATE paper_orders SET external_status='SIM_MATCHED_AWAITING_CONFIRMATION',reason=reason||?,updated_at=? WHERE id=?",
                (f" | 已模拟撮合 {fill['shares']:.6f} 份，等待确认；余额尚未释放", now, order["order_id"]))
        self.event("CONFIRMING", f"已模拟撮合，约 {max(0,due-now):.1f} 秒后确认；资金保持占用")
        self._observe_buy_arrival(order, now, book, consumed, fill)

    def _observe_buy_arrival(self, order, now, book, consumed, fill):
        self._consume(order, fill)
        self._audit(None, "MATCHED" if fill["shares"] > 0 else "ZERO_FILL", order_id=order["order_id"],
                    round_id=order["round_id"], fill=fill, book=book, consumed_before=consumed,
                    arrival=self._arrival_evidence(order, now=now, book=book),
                    plan=order.get("r1_plan"), signal_snapshot_id=order.get("r1_frame"))

    def _arrival_evidence(self, order, frame=None, now=None, *, book=None):
        at = self.clock() if now is None else now
        if book is None:
            quotes = getattr(frame, "quotes", {})
            book = quotes.get(order["side"], {}) if isinstance(quotes, dict) else {}
        book = book if isinstance(book, dict) else {}
        source, received = (_finite_time(book.get(key)) for key in ("source_ms", "received_ms"))
        arrival = order["submitted_at"]+order["arrival_delay"]
        return {"arrival_at": arrival, "deadline_at": arrival+self.policy.max_poll_lateness_seconds,
                "source_ms": source, "received_ms": received, "observed_at": at,
                "source_age_ms": _finite_time(at*1000-source) if source is not None else None,
                "receipt_age_ms": _finite_time(at*1000-received) if received is not None else None,
                "transport_lag_ms": _finite_time(received-source) if source is not None and received is not None else None}

    def _last_arrival_wait(self, order_id):
        with self.store._lock:
            row = self.store.conn.execute("""SELECT detail_json FROM main_r1_decisions
                WHERE order_id=? AND code='ARRIVAL_WAIT' ORDER BY id DESC LIMIT 1""", (order_id,)).fetchone()
        return json.loads(row[0]).get("arrival") if row else None

    def _finish_empty(self, order, reason, *, verified_zero_fill=False,
                      failure_code=None, arrival_evidence=None):
        with self.store._lock:
            row = self.store.conn.execute("SELECT * FROM paper_sim_orders WHERE order_id=?", (order["order_id"],)).fetchone()
            if not row or row["phase"] != "IN_FLIGHT" or json.loads(row["payload"]).get("fill", {}).get("shares", 0) > 0:
                return
        if not verified_zero_fill:
            now = self.clock()
            arrival = order["submitted_at"]+order["arrival_delay"]
            deadline = arrival+self.policy.max_poll_lateness_seconds
            last_wait = self._last_arrival_wait(order["order_id"])
            if failure_code is None and now > deadline:
                failure_code = "ARRIVAL_WINDOW_EXHAUSTED" if last_wait else "ARRIVAL_POLL_LATE"
                reason = ("固定到达窗口内持续等待但未取得合格收据，保持未知订单占位" if last_wait else
                          "首次到达处理已超过固定观察窗口，保持未知订单占位")
            failure_code = failure_code or "ARRIVAL_UNVERIFIED"
            arrival_evidence = arrival_evidence or last_wait
            with self.store._lock, self.store.conn:
                cursor = self.store.conn.execute(
                    "UPDATE paper_sim_orders SET phase='UNKNOWN' WHERE order_id=? AND phase='IN_FLIGHT'",
                    (order["order_id"],))
                if cursor.rowcount != 1:
                    return
                self.store.conn.execute(
                    "UPDATE paper_orders SET external_status='SIM_ARRIVAL_UNOBSERVED',reason=reason||?,updated_at=? WHERE id=? AND status='PENDING'",
                    (" | "+reason, self.clock(), order["order_id"]))
                self.store.conn.execute("""
                    INSERT INTO main_r1_round_locks VALUES(?,'UNKNOWN',?,?,?)
                    ON CONFLICT(round_id) DO UPDATE SET
                      state=CASE WHEN state='CONSUMED' THEN state ELSE 'UNKNOWN' END,
                      updated_at=excluded.updated_at,reason=excluded.reason
                """, (order["round_id"], order["order_id"], self.clock(), reason))
            self.guard.halt("unknown_order", now=self.clock())
            self._audit(None, "UNKNOWN", order_id=order["order_id"], round_id=order["round_id"], reason=reason,
                        failure_code=failure_code, arrival=arrival_evidence,
                        arrival_at=arrival, arrival_deadline=deadline)
            self.event("UNKNOWN", reason)
            return
        super()._finish_empty(order, reason)
        with self.store._lock, self.store.conn:
            row = self.store.conn.execute("SELECT status,filled_shares,cash_spent FROM paper_orders WHERE id=?", (order["order_id"],)).fetchone()
            if row and row["status"] == "CANCELED" and row["filled_shares"] == 0 and row["cash_spent"] == 0:
                self.store.conn.execute("UPDATE main_r1_round_locks SET state='RELEASED',updated_at=?,reason=? WHERE round_id=? AND state='PENDING' AND order_id=?",
                    (self.clock(), "确定终态零成交，允许新帧重试", order["round_id"], order["order_id"]))
        self._audit(None, "ZERO_FILL", order_id=order["order_id"], round_id=order["round_id"], reason=reason)

    def recover(self):
        with self.store._lock:
            rows = [dict(r) for r in self.store.conn.execute("SELECT * FROM paper_sim_orders")]
        for row in rows:
            order = {**json.loads(row["payload"]), "order_id": row["order_id"]}
            fill = order.get("fill", {})
            if fill.get("shares", 0) > 0:
                self._consume(order, fill)
                if row["phase"] in ("CONFIRMING", "FILLED", "PARTIAL"):
                    continue
            if row["phase"] in ("IN_FLIGHT", "UNKNOWN", "CONFIRMING"):
                with self.store._lock, self.store.conn:
                    self.store.conn.execute("UPDATE paper_sim_orders SET phase='UNKNOWN' WHERE order_id=?", (row["order_id"],))
                    self.store.conn.execute("INSERT INTO main_r1_round_locks VALUES(?,'UNKNOWN',?,?,?) ON CONFLICT(round_id) DO UPDATE SET state=CASE WHEN state='CONSUMED' THEN state ELSE 'UNKNOWN' END,updated_at=excluded.updated_at,reason=excluded.reason",
                        (order["round_id"], row["order_id"], self.clock(), "重启缺少到达证据，保持占位与预留"))
                self.guard.halt("unknown_order", now=self.clock())
            elif row["phase"] == "CANCELED" and not fill.get("shares", 0):
                with self.store._lock, self.store.conn:
                    verified = self.store.conn.execute("SELECT 1 FROM paper_orders WHERE id=? AND status='CANCELED' AND filled_shares=0 AND cash_spent=0 AND remaining_cash=0", (row["order_id"],)).fetchone()
                    if verified:
                        self.store.conn.execute("UPDATE main_r1_round_locks SET state='RELEASED',updated_at=? WHERE round_id=? AND state='PENDING' AND order_id=?", (self.clock(), order["round_id"], row["order_id"]))
        with self.store._lock:
            orphan = self.store.conn.execute("SELECT 1 FROM paper_orders o LEFT JOIN paper_sim_orders s ON s.order_id=o.id WHERE o.status='PENDING' AND s.order_id IS NULL LIMIT 1").fetchone()
        if orphan:
            self.guard.halt("orphan_pending_order", now=self.clock())

    def protects_order(self, ident):
        with self.store._lock:
            return self.store.conn.execute("SELECT 1 FROM paper_sim_orders WHERE order_id=? AND phase IN ('IN_FLIGHT','CONFIRMING','UNKNOWN')", (ident,)).fetchone() is not None

    def settlement_blocked(self, round_id):
        with self.store._lock:
            return self.store.conn.execute("SELECT 1 FROM paper_orders WHERE round_id=? AND status='PENDING'", (round_id,)).fetchone() is not None

    def poll(self):
        with self.lock:
            super().poll()
            now = self.clock()
            with self.store._lock:
                account = tuple(self.store.conn.execute(
                    "SELECT cash_balance,realized_pnl FROM account WHERE id=1").fetchone())
            # Confirmation/claim processing stays at the caller's 100ms cadence.
            # Idle NAV sampling is limited to 1Hz; cash/PnL transitions are sampled
            # immediately. Every entry decision still takes its own fresh check.
            if (self._last_poll_risk_at is None or now < self._last_poll_risk_at
                    or now-self._last_poll_risk_at >= 1.0 or account != self._last_poll_account):
                self.risk_checkpoint()
                self._last_poll_risk_at = now
                self._last_poll_account = account

    def manage_exits(self):
        return None

    def enqueue_sell(self, *args, **kwargs):
        raise ValueError("MAIN-R1 研究只按官方结果结算，不提交卖单")

    def snapshot(self):
        with self.lock, self.store._lock:
            db = self.store.conn
            counts = dict(db.execute("SELECT phase,count(*) FROM paper_sim_orders GROUP BY phase").fetchall())
            decisions = [dict(r) for r in db.execute("SELECT id,observed_at,round_id,snapshot_id,code,order_id FROM main_r1_decisions ORDER BY id DESC LIMIT 20")]
            unknown = []
            for row in db.execute("""SELECT s.order_id,s.phase,s.payload,o.round_id,o.reason,
                    l.reason AS lock_reason FROM paper_sim_orders s JOIN paper_orders o ON o.id=s.order_id
                    LEFT JOIN main_r1_round_locks l ON l.order_id=s.order_id
                    WHERE s.phase='UNKNOWN' ORDER BY s.order_id DESC LIMIT 10""").fetchall():
                order = json.loads(row["payload"])
                model, basis = _arrival_contract(order)
                audit = db.execute("""SELECT detail_json FROM main_r1_decisions
                    WHERE order_id=? AND code='UNKNOWN' ORDER BY id ASC LIMIT 1""", (row["order_id"],)).fetchone()
                detail = json.loads(audit[0]) if audit else {}
                evidence = detail.get("arrival") or self._last_arrival_wait(row["order_id"]) or {}
                observed = _finite_time(evidence.get("observed_at"))
                source, received = (_finite_time(evidence.get(key)) for key in ("source_ms", "received_ms"))
                arrival = order["submitted_at"]+order["arrival_delay"]
                unknown.append({"order_id": row["order_id"], "round_id": row["round_id"], "phase": row["phase"],
                    "model": model, "execution_basis": basis, "submitted_at": order["submitted_at"],
                    "arrival_at": arrival, "deadline_at": arrival+self.policy.max_poll_lateness_seconds,
                    "reason": detail.get("reason") or row["lock_reason"] or row["reason"],
                    "failure_code": detail.get("failure_code"), "evidence_at": observed,
                    "source_ms": source, "received_ms": received,
                    "source_age_ms": _finite_time(observed*1000-source) if observed is not None and source is not None else None,
                    "receipt_age_ms": _finite_time(observed*1000-received) if observed is not None and received is not None else None,
                    "transport_lag_ms": _finite_time(received-source) if source is not None and received is not None else None})
            return {"strategy_id": self.strategy_id, "name": LABEL, "mode": "PAPER_ONLY", "live_enabled": False,
                    "arrival_model": ARRIVAL_MODEL, "execution_basis": EXECUTION_BASIS,
                    "unknown_order_count": counts.get("UNKNOWN", 0), "unknown_orders": unknown,
                    "config_hash": self.config_hash, "policy": asdict(self.policy), "account": self.store.account(),
                    "risk": deepcopy(self.last_risk), "risk_state": self.guard.snapshot(),
                    "counts": counts, "last_event": dict(self.last_event), "decisions": decisions,
                    "pending_settlement_cash": self.pending_cash(),
                    "capital_scope": "独立 1000 虚拟资金；不同研究账本不合并收益"}

"""Owned real orders and fills; never uses Paper cash or Paper entry prices."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from . import first_strong_rules as first_strong
from .managed_live_recovery import (FINALIZE_MIN_CHECKS, FINALIZE_MIN_SPAN_SECONDS, FINAL_AUDIT_INTERVAL_SECONDS,
                                   LATE_AUDIT_FAULT, wallet_only_audit_fault, fresh_absent_order, settled_activity_only,
                                   finalization_evidence, direct_order_evidence)

EXIT_SETTLE_LIVE = "FORECAST_EXIT_SETTLE"
STRATEGIES = {"MAIN": "主账户", "MAIN_OPTIMIZED": "主账户（优化版）",
              "MAIN_ADAPTIVE": "主账户自适应成长版", "MAIN_OPTIMIZED_FAST": "主账户（优化极速赚钱版）",
              "MAIN_OPTIMIZED_PLUS": "主账户（优化进阶版）", EXIT_SETTLE_LIVE: "拿到官方结算",
              first_strong.KEY: first_strong.LABEL}
RETIRED_STRATEGIES = {"VALUE_CONFIRM_V1": "价值确认 V1", "MAIN_TP_SL": "主账户止盈止损"}
STRATEGY_LABELS = {**STRATEGIES, **RETIRED_STRATEGIES}
ACTIVE_ORDER_STATES = ("PREPARED", "SUBMITTING", "PENDING", "UNKNOWN", "QUARANTINED")
BUDGET_ERROR = "官方成交费用超出预算，需核对"
BUDGET_TOLERANCE_MICROS = 10


class IncompleteFillReceipt(RuntimeError):
    """Keep the order reserved until the amounts and receipt identity can be verified."""


def defaults():
    config = {"enabled": False, "manage_existing_when_off": True, "revision": 0,
            "strategies": {key: {"enabled": False, "stake_dollars": 2.0 if key == "VALUE_CONFIRM_V1" else 5.0,
                                "max_open_trades": 1 if key == "VALUE_CONFIRM_V1" else 2,
                                "max_daily_loss": 6.0 if key == "VALUE_CONFIRM_V1" else 30.0 if key == "MAIN_OPTIMIZED_PLUS" else 21.0,
                                "take_profit_pct": 20.0, "stop_loss_pct": 40.0}
                           for key in STRATEGIES}}
    config["strategies"][EXIT_SETTLE_LIVE].update(max_open_trades=1, max_daily_loss=15.0)
    config["strategies"][first_strong.KEY].update(stake_dollars=first_strong.BUDGET,
        max_open_trades=first_strong.SLOTS, max_daily_loss=15.0)
    return config


def finite(value, low=0, high=1_000_000):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"金额/参数须为 {low} 至 {high} 之间的有限数字")
    return float(value)


def _settlement_curve_points(points, max_points=1200):
    """Bound display size over the full account history, retaining endpoints and local extrema."""
    if len(points) <= max_points:
        return points
    bucket_size = math.ceil((len(points)-2) / ((max_points-2)//2))
    selected = [points[0]]
    for start in range(1,len(points)-1,bucket_size):
        indices = range(start,min(start+bucket_size,len(points)-1))
        low = min(indices,key=lambda i:points[i]["realized_pnl"])
        high = max(indices,key=lambda i:points[i]["realized_pnl"])
        selected.extend(points[i] for i in sorted({low,high}))
    selected.append(points[-1])
    return selected


class ManagedLiveStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self._curve_lock=threading.Lock();self._curve_cache=None
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS live_config(id INTEGER PRIMARY KEY CHECK(id=1),json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS loss_limit_resets(
            strategy_id TEXT PRIMARY KEY,reset_at REAL NOT NULL,position_pnl_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS wallet_identity(id INTEGER PRIMARY KEY CHECK(id=1),identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS rounds(round_id TEXT PRIMARY KEY,json TEXT NOT NULL,ends_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS positions(
            id INTEGER PRIMARY KEY,strategy_id TEXT NOT NULL,round_id TEXT NOT NULL,token_id TEXT NOT NULL,
            outcome TEXT NOT NULL,entry_order TEXT UNIQUE NOT NULL,opened_at REAL NOT NULL,
            initial_shares REAL NOT NULL DEFAULT 0,remaining_shares REAL NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,remaining_cost REAL NOT NULL DEFAULT 0,
            buy_fee REAL NOT NULL DEFAULT 0,sell_fee REAL NOT NULL DEFAULT 0,realized_pnl REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'OPEN',closed_at REAL,claim_due REAL NOT NULL DEFAULT 0,
            resolved_outcome TEXT,exit_reason TEXT);
          CREATE TABLE IF NOT EXISTS orders(
            id TEXT PRIMARY KEY,external_id TEXT UNIQUE NOT NULL,strategy_id TEXT NOT NULL,
            round_id TEXT NOT NULL,token_id TEXT NOT NULL,outcome TEXT NOT NULL,action TEXT NOT NULL,
            position_id INTEGER REFERENCES positions(id),requested_cash REAL NOT NULL DEFAULT 0,
            requested_shares REAL NOT NULL DEFAULT 0,limit_price REAL NOT NULL,state TEXT NOT NULL,
            created_at REAL NOT NULL,submitted_at REAL,updated_at REAL NOT NULL,reason TEXT,error TEXT,
            confirmed_shares REAL NOT NULL DEFAULT 0,confirmed_notional REAL NOT NULL DEFAULT 0,
            confirmed_fee REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS fills(
            fill_key TEXT PRIMARY KEY,order_id TEXT NOT NULL REFERENCES orders(id),trade_id TEXT NOT NULL,
            shares REAL NOT NULL,price REAL NOT NULL,notional REAL NOT NULL,fee REAL NOT NULL,
            fee_source TEXT NOT NULL,confirmed_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS fill_receipt_trades(
            order_id TEXT NOT NULL REFERENCES orders(id),trade_id TEXT NOT NULL,receipt_id TEXT NOT NULL,
            PRIMARY KEY(order_id,trade_id));
          CREATE TABLE IF NOT EXISTS wallet_curve(id INTEGER PRIMARY KEY,at REAL NOT NULL,balance REAL,total_equity REAL);
          CREATE TABLE IF NOT EXISTS strategy_curve(id INTEGER PRIMARY KEY,at REAL NOT NULL,strategy_id TEXT NOT NULL,pnl REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS valuation_books(token_id TEXT PRIMARY KEY,round_id TEXT NOT NULL,json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at REAL NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS reconciliation_faults(
            order_id TEXT PRIMARY KEY REFERENCES orders(id),message TEXT NOT NULL,created_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS live_positions_strategy ON positions(strategy_id,status);
          CREATE INDEX IF NOT EXISTS live_orders_state ON orders(state);
          CREATE INDEX IF NOT EXISTS live_orders_strategy ON orders(strategy_id,created_at);
          CREATE INDEX IF NOT EXISTS live_fills_order ON fills(order_id);
          CREATE INDEX IF NOT EXISTS live_wallet_curve_at ON wallet_curve(at,id);
        """)
        columns={r[1] for r in self.conn.execute("PRAGMA table_info(orders)")}
        for name,kind in {"ack_id":"TEXT","quarantined_at":"REAL","verification_at":"REAL",
                          "verification_json":"TEXT","submission_json":"TEXT","trade_observations_json":"TEXT",
                          "closure_checks":"INTEGER NOT NULL DEFAULT 0",
                          "closure_since":"REAL","closure_checked_at":"REAL","finalized_at":"REAL","finalization_json":"TEXT",
                          "audit_recovery_json":"TEXT","adaptive_decision_json":"TEXT","fast_decision_json":"TEXT","plus_decision_json":"TEXT"}.items():
            if name not in columns:self.conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {kind}")
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO live_config VALUES(1,?)", (json.dumps(defaults()),))
        self._migrate_config()

    def _migrate_config(self):
        """Add newly supported strategies without resetting live choices.

        The live ledger predates MAIN_ADAPTIVE, so an existing database may
        still contain only the original four strategy rows.  Merge each known
        row with the current defaults and keep all saved values.  Unknown
        retired rows are omitted from the active config so the execution loop
        cannot try to evaluate a strategy that no longer exists.
        """
        with self.lock:
            row = self.conn.execute("SELECT json FROM live_config WHERE id=1").fetchone()
            if not row:
                return
            try:
                stored = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                stored = {}
            base = defaults()
            if isinstance(stored, dict):
                retired = stored.get("retired_strategies", {})
                base["retired_strategies"] = dict(retired) if isinstance(retired, dict) else {}
                for field in ("enabled", "manage_existing_when_off", "revision"):
                    if field in stored:
                        base[field] = stored[field]
                old_strategies = stored.get("strategies")
                if isinstance(old_strategies, dict):
                    for key in RETIRED_STRATEGIES:
                        values = old_strategies.get(key)
                        if isinstance(values, dict):
                            base["retired_strategies"][key] = {**values, "enabled": False}
                    for key in STRATEGIES:
                        values = old_strategies.get(key)
                        if isinstance(values, dict):
                            base["strategies"][key].update(values)
            # A malformed legacy value must never turn the startup migration
            # into an enabled live switch or an invalid risk configuration.
            base["enabled"] = bool(base.get("enabled", False))
            base["manage_existing_when_off"] = bool(base.get("manage_existing_when_off", True))
            try:
                base["revision"] = int(base.get("revision", 0))
            except (TypeError, ValueError):
                base["revision"] = 0
            payload = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
            if payload != row[0]:
                with self.conn:
                    self.conn.execute("UPDATE live_config SET json=? WHERE id=1", (payload,))

    def config(self):
        with self.lock:
            return json.loads(self.conn.execute("SELECT json FROM live_config WHERE id=1").fetchone()[0])

    def exit_rules(self, strategy_id):
        """Keep managing historical positions without allowing retired entries."""
        config = self.config()
        return (config["strategies"].get(strategy_id)
                or config.get("retired_strategies", {}).get(strategy_id)
                or {"enabled": False, "take_profit_pct": 20.0, "stop_loss_pct": 40.0})

    def update_config(self, payload):
        with self.lock:
            config = self.config()
            if not isinstance(payload, dict) or set(payload)-{"enabled", "strategies"}:
                raise ValueError("实盘设置字段不支持")
            if "enabled" in payload:
                if not isinstance(payload["enabled"], bool):
                    raise ValueError("开关须为 true/false")
                config["enabled"] = payload["enabled"]
            strategies = payload.get("strategies", {})
            if not isinstance(strategies, dict) or set(strategies)-set(STRATEGIES):
                raise ValueError("未知实盘策略")
            for key, values in strategies.items():
                if not isinstance(values, dict) or set(values)-set(config["strategies"][key]):
                    raise ValueError("策略参数字段不支持")
                for field, value in values.items():
                    if field == "enabled":
                        if not isinstance(value, bool):
                            raise ValueError("策略开关须为 true/false")
                        config["strategies"][key][field] = value
                    elif field == "max_open_trades":
                        number = finite(value, 1, first_strong.SLOTS if key == first_strong.KEY else 10)
                        if int(number) != number:
                            raise ValueError("最大持仓须为整数")
                        config["strategies"][key][field] = int(number)
                    else:
                        low, high = (.1, 10000) if field == "stake_dollars" else (1, 99) if field == "stop_loss_pct" else (1, 1000) if field == "take_profit_pct" else (0, 100000)
                        config["strategies"][key][field] = round(finite(value, low, high), 2)
                settings = config["strategies"][key]
                if settings["max_daily_loss"] and settings["stake_dollars"] > settings["max_daily_loss"]:
                    raise ValueError("单笔金额不能超过本策略的每日亏损限额")
            config["revision"] += 1
            with self.conn:
                self.conn.execute("UPDATE live_config SET json=? WHERE id=1", (json.dumps(config),))
            return config

    def bind_wallet(self, identity):
        with self.lock, self.conn:
            row = self.conn.execute("SELECT identity FROM wallet_identity WHERE id=1").fetchone()
            if row and row[0] != identity:
                raise ValueError("该实盘账本属于另一钱包，禁止混用持仓")
            self.conn.execute("INSERT OR IGNORE INTO wallet_identity VALUES(1,?)", (identity,))

    def event(self, kind, message):
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO events(at,kind,message) VALUES(?,?,?)", (time.time(), kind, message[:500]))

    def save_market(self, market):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO rounds VALUES(?,?,?)", (market.round_id, json.dumps(asdict(market)), market.ends_at))

    def market(self, round_id):
        with self.lock:
            row = self.conn.execute("SELECT json FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def prepare_order(self, external_id, strategy_id, market, token_id, outcome, action,
                      limit_price, cash=0, shares=0, position_id=None, reason="", now=None, adaptive_decision=None,
                      fast_decision=None, plus_decision=None):
        now = time.time() if now is None else now
        with self.lock, self.conn:
            if strategy_id not in STRATEGY_LABELS or action not in ("BUY", "SELL"):
                raise ValueError("未知策略或订单方向")
            if action == "BUY" and strategy_id not in STRATEGIES:
                raise ValueError("该策略已移除，禁止新开仓")
            if not external_id or now >= market.ends_at:
                raise ValueError("缺少订单哈希或市场已结束")
            if self.active_orders(strategy_id, market.round_id):
                raise ValueError("本策略本期已有订单待确认")
            if action=="BUY" and any(o["state"]=="QUARANTINED" for o in self.active_orders(round_id=market.round_id)):
                raise ValueError("本市场存在隔离订单，禁止重复开仓")
            if action == "SELL":
                p = self.position(position_id)
                if not p or p["strategy_id"] != strategy_id or p["token_id"] != token_id or not 0 < shares <= p["remaining_shares"]+.000001:
                    raise ValueError("卖出份额不属于当前策略")
            if action == "BUY" and strategy_id == "MAIN_ADAPTIVE" and self.has_entry(strategy_id, market.round_id):
                raise ValueError("成长版本期已有成交，禁止同一期反手或重复开仓")
            if action == "BUY" and strategy_id == "MAIN_OPTIMIZED_FAST" and self.has_entry(strategy_id, market.round_id):
                raise ValueError("极速版本期已有成交，禁止同一期反手或重复开仓")
            if action == "BUY" and strategy_id == "MAIN_OPTIMIZED_PLUS" and self.has_entry(strategy_id, market.round_id):
                raise ValueError("进阶版本期已有成交，禁止同一期反手或重复开仓")
            if action == "BUY" and strategy_id == EXIT_SETTLE_LIVE and self.has_entry(strategy_id, market.round_id):
                raise ValueError("拿到官方结算本局已经买过一次")
            if action == "BUY" and strategy_id == first_strong.KEY and self.has_entry(strategy_id, market.round_id):
                raise ValueError("首个强信号本局已有成交，等待官方结算")
            adaptive_json = None
            if adaptive_decision is not None:
                if strategy_id != "MAIN_ADAPTIVE" or action != "BUY":
                    raise ValueError("自适应决策只能绑定成长版买入订单")
                from .adaptive_main import compact_adaptive_decision
                evidence = compact_adaptive_decision(adaptive_decision, mode="live")
                if evidence.get("round_id") != market.round_id or evidence.get("candidate_side") != outcome:
                    raise ValueError("自适应决策与订单市场或方向不一致")
                adaptive_json = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
            fast_json = None
            if strategy_id == "MAIN_OPTIMIZED_FAST" and action == "BUY" and fast_decision is None:
                raise ValueError("极速版买入缺少最终入场决策快照")
            if fast_decision is not None:
                if strategy_id != "MAIN_OPTIMIZED_FAST" or action != "BUY":
                    raise ValueError("极速版决策只能绑定极速版买入订单")
                from .optimized_fast import compact_fast_decision
                evidence = compact_fast_decision(fast_decision, mode="live")
                if evidence.get("round_id") != market.round_id or evidence.get("candidate_side") != outcome:
                    raise ValueError("极速版决策与订单市场或方向不一致")
                if not evidence.get("fast_allowed"):
                    raise ValueError("极速版决策未通过最终入场检查")
                fast_json = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
            plus_json = None
            if strategy_id == "MAIN_OPTIMIZED_PLUS" and action == "BUY" and plus_decision is None:
                raise ValueError("进阶版买入缺少最终入场决策快照")
            if plus_decision is not None:
                if strategy_id != "MAIN_OPTIMIZED_PLUS" or action != "BUY":
                    raise ValueError("进阶版决策只能绑定进阶版买入订单")
                from .optimized_plus import compact_plus_decision
                evidence = compact_plus_decision(plus_decision, mode="live")
                if evidence.get("round_id") != market.round_id or evidence.get("candidate_side") != outcome:
                    raise ValueError("进阶版决策与订单市场或方向不一致")
                if not evidence.get("plus_allowed"):
                    raise ValueError("进阶版决策未通过最终入场检查")
                plus_json = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
            ident = uuid.uuid4().hex
            self.conn.execute("""INSERT INTO orders(id,external_id,strategy_id,round_id,token_id,outcome,action,
                position_id,requested_cash,requested_shares,limit_price,state,created_at,updated_at,reason,adaptive_decision_json,fast_decision_json,plus_decision_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'PREPARED',?,?,?,?,?,?)""",
                (ident, external_id, strategy_id, market.round_id, token_id, outcome, action,
                 position_id, cash, shares, limit_price, now, now, reason, adaptive_json,fast_json,plus_json))
            return self.order(ident)

    def order(self, ident):
        with self.lock:
            row = self.conn.execute("SELECT * FROM orders WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def position(self, ident):
        with self.lock:
            row = self.conn.execute("SELECT * FROM positions WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def active_orders(self, strategy_id=None, round_id=None):
        with self.lock:
            sql = "SELECT * FROM orders WHERE state IN ('PREPARED','SUBMITTING','PENDING','UNKNOWN','QUARANTINED')"
            args = []
            if strategy_id:
                sql += " AND strategy_id=?"; args.append(strategy_id)
            if round_id:
                sql += " AND round_id=?"; args.append(round_id)
            return [dict(r) for r in self.conn.execute(sql+" ORDER BY created_at", args)]

    def set_order_state(self, ident, state, error=None, now=None):
        with self.lock, self.conn:
            now = time.time() if now is None else now
            self.conn.execute("""UPDATE orders SET state=?,error=CASE WHEN error=? THEN error ELSE ? END,
                updated_at=?,submitted_at=CASE WHEN ?='SUBMITTING' THEN ? ELSE submitted_at END WHERE id=?""",
                (state, BUDGET_ERROR, error, now, state, now, ident))

    def record_ack(self, ident, ack_id):
        with self.lock,self.conn:
            self.conn.execute("UPDATE orders SET ack_id=? WHERE id=?",(str(ack_id),ident))

    def record_submission(self,ident,result):
        # Only the adapter's sanitized result, never signatures, credentials or raw HTTP bodies.
        fields=("accepted","rejected","status","http_status","error_code","error_message","response_type","rejected_reason","error_type","trade_ids","response_has_order_id","response_acceptance_evidence")
        safe={k:result[k] for k in fields if k in result}
        with self.lock,self.conn:
            self.conn.execute("UPDATE orders SET submission_json=? WHERE id=?",(json.dumps(safe),ident))

    def record_verification(self,ident,evidence):
        with self.lock,self.conn:
            if evidence.get("matched_trade_ids") or evidence.get("confirmed_trade_ids"):
                order = self.order(ident)
                observations = json.loads(order.get("trade_observations_json") or "{}")
                for key in ("matched_trade_ids", "confirmed_trade_ids"):
                    observations[key] = sorted(set(observations.get(key, ())) | set(evidence.get(key, ())))
                self.conn.execute("UPDATE orders SET trade_observations_json=? WHERE id=?",
                                  (json.dumps(observations), ident))
            self.conn.execute("UPDATE orders SET verification_json=?,verification_at=? WHERE id=?",
                              (json.dumps(evidence),time.time(),ident))

    def recover_fak_no_match(self,ident,evidence,now=None):
        """Repair an old misclassified BUY using its refusal and fresh CLOB proof.

        No financial rows are deleted or credited. Only the unused reservation
        is released; real fills, acks, faults or incomplete reads forbid recovery.
        """
        from .managed_live_recovery import saved_fak_no_match,fresh_absent_order
        now=time.time() if now is None else now
        with self.lock,self.conn:
            order=self.order(ident)
            if (not order or order["state"] not in ("UNKNOWN","PENDING","QUARANTINED")
                    or order["action"]!="BUY" or order.get("ack_id") or order.get("position_id")
                    or any(order.get(k,0) for k in ("confirmed_shares","confirmed_notional","confirmed_fee"))
                    or not saved_fak_no_match(order) or not fresh_absent_order(evidence,now)):
                return False
            balance=evidence.get("token_balance")
            if not isinstance(balance,(int,float)) or isinstance(balance,bool) or not math.isfinite(balance) or not 0<=balance<=.000001:return False
            if (self.conn.execute("SELECT 1 FROM fills WHERE order_id=?",(ident,)).fetchone()
                    or self.conn.execute("SELECT 1 FROM positions WHERE entry_order=?",(ident,)).fetchone()
                    or self.conn.execute("SELECT 1 FROM reconciliation_faults WHERE order_id=?",(ident,)).fetchone()):return False
            proof={"policy":"official-fak-no-match-v1","recovered_at":now,
                   "submission":json.loads(order["submission_json"]),"evidence":evidence}
            return self.conn.execute("""UPDATE orders SET state='REJECTED',updated_at=?,finalized_at=?,finalization_json=?,
                error='官方明确返回 FAK 无匹配成交；重新核对无成交、无挂单，已释放预留'
                WHERE id=? AND state IN ('UNKNOWN','PENDING','QUARANTINED') AND ack_id IS NULL
                AND confirmed_shares=0 AND confirmed_notional=0 AND confirmed_fee=0""",
                (now,now,json.dumps(proof),ident)).rowcount>0

    def record_closure_probe(self,ident,eligible,evidence,now=None):
        now=time.time() if now is None else now
        with self.lock,self.conn:
            order=self.order(ident)
            if not order or order["state"]!="QUARANTINED":return False
            if not eligible:
                self.conn.execute("UPDATE orders SET closure_checks=0,closure_since=NULL,closure_checked_at=NULL WHERE id=?",(ident,))
                return False
            count=order["closure_checks"] or 0;since=order["closure_since"];last=order["closure_checked_at"]
            if last is None or now-last>180:count=0;since=now
            elif now-last<10:return False
            count+=1
            self.conn.execute("UPDATE orders SET closure_checks=?,closure_since=?,closure_checked_at=? WHERE id=?",(count,since,now,ident))
            if count<FINALIZE_MIN_CHECKS or now-since<FINALIZE_MIN_SPAN_SECONDS:return False
            proof={"policy":"resolved-market-no-activity-v1","first_check_at":since,"last_check_at":now,
                   "consecutive_checks":count,"evidence":evidence}
            return self.conn.execute("""UPDATE orders SET state='EXPIRED_UNFILLED',finalized_at=?,finalization_json=?,updated_at=?,
                error='市场已结算，多源连续核对未发现成交，已释放占用；仍保留后续复查'
                WHERE id=? AND state='QUARANTINED' AND ack_id IS NULL AND confirmed_shares=0
                AND confirmed_notional=0 AND confirmed_fee=0 AND id NOT IN (SELECT order_id FROM reconciliation_faults)""",
                (now,json.dumps(proof),now,ident)).rowcount>0

    def finalization_audit_candidate(self,now=None):
        now=time.time() if now is None else now
        with self.lock:
            row=self.conn.execute("""SELECT * FROM orders WHERE state='EXPIRED_UNFILLED'
                AND coalesce(verification_at,0)<=? ORDER BY coalesce(verification_at,0) LIMIT 1""",
                (now-FINAL_AUDIT_INTERVAL_SECONDS,)).fetchone()
            return dict(row) if row else None

    def quarantine_order(self,ident,now=None):
        now=time.time() if now is None else now
        with self.lock,self.conn:
            return self.conn.execute("""UPDATE orders SET state='QUARANTINED',quarantined_at=coalesce(quarantined_at,?),updated_at=?
                WHERE id=? AND state IN ('PENDING','UNKNOWN','SUBMITTING')
                AND id NOT IN (SELECT order_id FROM reconciliation_faults)""",(now,now,ident)).rowcount>0

    def risk_usage(self,strategy_id):
        with self.lock:
            positions=self.positions(strategy_id,open_only=True);orders=self.active_orders(strategy_id)
            slots={f"position:{p['id']}" for p in positions};unconfirmed=0.
            for order in orders:
                if order["action"]!="BUY":continue
                slots.add(f"position:{order['position_id']}" if order["position_id"] else f"order:{order['id']}")
                unconfirmed+=max(0,order["requested_cash"]-order["confirmed_notional"]-order["confirmed_fee"])
            isolated=[o for o in orders if o["state"]=="QUARANTINED"]
            return {"slots":len(slots),"confirmed_slots":len(positions),"pending_slots":len(slots)-len(positions),
                    "unconfirmed_budget":round(unconfirmed,6),
                    "loss_exposure":round(sum(p["remaining_cost"] for p in positions)+unconfirmed,6),
                    "quarantined_count":len(isolated),
                    "quarantined_cash":round(sum(o["requested_cash"] for o in isolated if o["action"]=="BUY"),6)}

    def reconciliation_faults(self):
        with self.lock:
            return {r["order_id"]:r["message"] for r in self.conn.execute("SELECT * FROM reconciliation_faults ORDER BY created_at")}

    def record_reconciliation_fault(self, ident, message):
        # Identity/accounting failures require review; a later successful read or a
        # process restart must not silently clear them along with temporary delays.
        with self.lock,self.conn:
            return self.conn.execute("INSERT OR IGNORE INTO reconciliation_faults VALUES(?,?,?)",
                                     (ident,message,time.time())).rowcount>0

    def recover_wallet_activity(self,ident,evidence):
        """Narrow automatic recovery; retain the original fault and all evidence.

        Unattributed wallet activity only reserves this order's bounded exposure.
        Authenticated fills, identity failures and budget faults cannot use this path.
        """
        now=time.time()
        with self.lock,self.conn:
            order=self.order(ident)
            fault=self.conn.execute('SELECT * FROM reconciliation_faults WHERE order_id=?',(ident,)).fetchone()
            if not order or order['state'] not in ('UNKNOWN','QUARANTINED') or not finalization_evidence(order):return None
            if order.get('ack_id') or any(order.get(k,0) for k in ('confirmed_shares','confirmed_notional','confirmed_fee')):return None
            if fault:
                if fault['message']!=LATE_AUDIT_FAULT or not wallet_only_audit_fault(order):return None
            elif direct_order_evidence(json.loads(order.get('verification_json') or '{}')):return None
            if not fresh_absent_order(evidence,now) or evidence.get('wallet_activity')!='checked':return None
            settled=settled_activity_only(order,evidence,now)
            if not settled and (not fault or not (evidence.get('wallet_activity_count',0)>0 or evidence.get('wallet_activity_complete') is True)):return None
            target='EXPIRED_UNFILLED' if settled else 'QUARANTINED'
            message=('后续记录为正常结算赎回，旧卖单无新成交，已恢复正常检查' if settled else
                     '钱包活动尚未关联到该订单，仅隔离本订单核对；其他市场按剩余额度运行')
            history=json.loads(order.get('audit_recovery_json') or '[]')
            history.append({'at':now,'state':target,'previous_fault':dict(fault) if fault else None,
                            'previous_verification':json.loads(order.get('verification_json') or '{}'),'evidence':evidence})
            self.conn.execute("""UPDATE orders SET state=?,error=?,updated_at=?,verification_at=?,verification_json=?,
                audit_recovery_json=?,quarantined_at=CASE WHEN ?='QUARANTINED' THEN coalesce(quarantined_at,?) ELSE quarantined_at END
                WHERE id=?""",(target,message,now,now,json.dumps(evidence),json.dumps(history),target,now,ident))
            if fault:self.conn.execute('DELETE FROM reconciliation_faults WHERE order_id=? AND message=?',(ident,LATE_AUDIT_FAULT))
            return target

    def apply_confirmed_fill(self, ident, trade_id, shares, price, fee, fee_source, now=None, *, notional):
        fill = dict(trade_id=trade_id, shares=shares, price=price, fee=fee,
                    fee_source=fee_source, notional=notional)
        return bool(self.apply_reconciled_fills(ident, [fill], {}, now=now))

    def apply_reconciled_fills(self, ident, fills, receipt_groups, now=None):
        """Book the authenticated amounts together, including across the CLOB ID migration."""
        if not fills:
            return 0
        now = time.time() if now is None else now
        with self.lock, self.conn:
            order = self.order(ident)
            if order is None:
                raise ValueError("未知本地订单，拒绝归属外部成交")
            recorded = {row["trade_id"]: row for row in self.conn.execute(
                "SELECT trade_id,shares,notional,fee FROM fills WHERE order_id=?", (ident,))}
            aliases = {row["trade_id"]: row["receipt_id"] for row in self.conn.execute(
                "SELECT trade_id,receipt_id FROM fill_receipt_trades WHERE order_id=?", (ident,))}
            new_count = 0
            polygon_batch = any(str(fill.get("trade_id", "")).startswith("polygon:") for fill in fills)
            if polygon_batch:
                current_ids = {fill.get("trade_id") for fill in fills}
                if (not isinstance(receipt_groups, dict) or current_ids != set(receipt_groups)
                        or any(not isinstance(key, str) or not key.startswith("polygon:") for key in current_ids)):
                    raise IncompleteFillReceipt("部分链上成交缺少对应的官方交易编号，继续核对")
                legacy_ids = set(recorded) - {key for key in recorded if key.startswith("polygon:")}
                covered = set(aliases)
                for ids in receipt_groups.values():
                    if not isinstance(ids, list) or not ids or not all(
                        isinstance(k, str) and k and not k.startswith("polygon:") for k in ids
                    ):
                        raise IncompleteFillReceipt("链上成交缺少对应的官方交易编号，继续核对")
                    covered.update(ids)
                if legacy_ids - covered:
                    raise IncompleteFillReceipt("旧版已入账成交尚未与链上回执全部对应，继续核对")
            for fill in fills:
                trade_id = fill.get("trade_id")
                if not isinstance(trade_id, str) or not trade_id:
                    raise IncompleteFillReceipt("成交缺少官方编号，继续核对")
                if trade_id in aliases:
                    raise IncompleteFillReceipt("此成交已与链上回执关联，等待完整回执核对")
                if any(fill.get(key) is None for key in ("shares", "price", "notional", "fee", "fee_source")):
                    raise IncompleteFillReceipt("成交本金、份额或手续费的官方凭证尚未齐全，继续核对")
                shares = finite(fill["shares"], .000001, 1e12)
                price = finite(fill["price"], .000001, 1)
                notional = finite(fill["notional"], .000001, shares)
                fee = finite(fill["fee"], 0, 1e9)
                amounts = (shares, notional, fee)
                if any(round(amount, 6) != amount for amount in amounts):
                    raise ValueError("官方成交金额或份额精度超出六位")
                members = receipt_groups.get(trade_id, [])
                if members:
                    if len(set(members)) != len(members) or any(
                        key in aliases and aliases[key] != trade_id for key in members
                    ):
                        raise ValueError("同一笔官方成交与多张链上回执关联，需核对")
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO fill_receipt_trades VALUES(?,?,?)",
                        [(ident, key, trade_id) for key in members])
                    aliases.update(dict.fromkeys(members, trade_id))
                existing_ids = {trade_id}
                if members:
                    existing_ids.update(key for key, receipt in aliases.items() if receipt == trade_id)
                booked = [recorded[key] for key in existing_ids if key in recorded]
                if booked:
                    totals = tuple(round(sum(row[key] for row in booked), 6)
                                   for key in ("shares", "notional", "fee"))
                    if totals != amounts:
                        raise ValueError("已入账的成交与官方回执份额、本金或手续费不一致，需核对")
                    continue
                self._write_confirmed_fill(self.order(ident), trade_id, shares, price, notional,
                                           fee, fill["fee_source"], now)
                recorded[trade_id] = dict(shares=shares, notional=notional, fee=fee)
                new_count += 1
            return new_count

    def _write_confirmed_fill(self, order, trade_id, shares, price, notional, fee, fee_source, now):
        ident = order["id"]
        fill_key = order["external_id"] + ":" + trade_id
        if order["action"] == "BUY":
            spent_micros = sum(round(amount * 1_000_000) for amount in
                               (order["confirmed_notional"], order["confirmed_fee"], notional, fee))
            if spent_micros > round(order["requested_cash"] * 1_000_000) + BUDGET_TOLERANCE_MICROS:
                # Preserve the real holding even when a broker report violates
                # the signed budget assumption; block further entries separately.
                self.conn.execute("UPDATE orders SET error=? WHERE id=?", (BUDGET_ERROR,ident))
            if order["position_id"] is None:
                cur = self.conn.execute("""INSERT INTO positions(strategy_id,round_id,token_id,outcome,
                    entry_order,opened_at) VALUES(?,?,?,?,?,?)""", (order["strategy_id"], order["round_id"],
                    order["token_id"], order["outcome"], ident, order["created_at"]))
                order["position_id"] = cur.lastrowid
                self.conn.execute("UPDATE orders SET position_id=? WHERE id=?", (order["position_id"], ident))
            self.conn.execute("""UPDATE positions SET initial_shares=round(initial_shares+?,6),remaining_shares=round(remaining_shares+?,6),
                total_cost=round(total_cost+?,6),remaining_cost=round(remaining_cost+?,6),buy_fee=round(buy_fee+?,6)
                WHERE id=?""", (shares, shares, notional+fee, notional+fee, fee, order["position_id"]))
        else:
            position = self.position(order["position_id"])
            available = round(position["remaining_shares"], 6) if position else 0
            if shares > available:
                raise ValueError("官方卖出份额超出本策略持仓，需要核对")
            close_all = shares == available
            basis = position["remaining_cost"] if close_all else round(position["remaining_cost"]*shares/available,6)
            remain = round(available-shares,6)
            self.conn.execute("""UPDATE positions SET remaining_shares=?,remaining_cost=round(remaining_cost-?,6),
                sell_fee=round(sell_fee+?,6),realized_pnl=round(realized_pnl+?,6),status=?,closed_at=?,exit_reason=? WHERE id=?""",
                (remain, basis, fee, notional-fee-basis, "CLOSED" if close_all else "OPEN",
                 now if close_all else None, order["reason"], position["id"]))
        self.conn.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)", (fill_key,ident,str(trade_id),shares,price,notional,fee,fee_source,now))
        self.conn.execute("""UPDATE orders SET confirmed_shares=round(confirmed_shares+?,6),confirmed_notional=round(confirmed_notional+?,6),
            confirmed_fee=round(confirmed_fee+?,6),updated_at=? WHERE id=?""", (shares,notional,fee,now,ident))

    def positions(self, strategy_id=None, open_only=False):
        with self.lock:
            sql = "SELECT * FROM positions WHERE 1=1"; args=[]
            if strategy_id:sql += " AND strategy_id=?";args.append(strategy_id)
            if open_only:sql += " AND remaining_shares>0 AND resolved_outcome IS NULL"
            return [dict(r) for r in self.conn.execute(sql+" ORDER BY opened_at,id",args)]

    def resolve(self, round_id, outcome, now=None):
        if outcome not in ("Up","Down"):
            raise ValueError("缺少明确的官方结果")
        now = time.time() if now is None else now
        with self.lock, self.conn:
            if any(o["round_id"] == round_id for o in self.active_orders()):
                return False
            for p in self.positions(open_only=True):
                if p["round_id"] != round_id:continue
                payout = p["remaining_shares"] if p["outcome"] == outcome else 0
                self.conn.execute("""UPDATE positions SET realized_pnl=round(realized_pnl+?,6),claim_due=?,
                    remaining_shares=0,remaining_cost=0,status=?,closed_at=?,resolved_outcome=? WHERE id=?""",
                    (payout-p["remaining_cost"],payout,"REDEEMABLE" if payout else "SETTLED",now,outcome,p["id"]))
            return True

    def reserved_cash(self, wallet_read_started_at=0):
        with self.lock:
            row = self.conn.execute("""SELECT coalesce(sum(CASE WHEN state IN ('PREPARED','SUBMITTING','PENDING','UNKNOWN','QUARANTINED')
                THEN max(requested_cash,round(confirmed_notional+confirmed_fee,6))
                WHEN updated_at>=? THEN confirmed_notional+confirmed_fee ELSE 0 END),0)
                FROM orders WHERE action='BUY'""", (wallet_read_started_at,)).fetchone()
            return round(row[0],6)

    def has_entry(self, strategy_id, round_id, side=None):
        with self.lock:
            sql="SELECT 1 FROM positions WHERE strategy_id=? AND round_id=?";args=[strategy_id,round_id]
            if side:sql+=" AND outcome=?";args.append(side)
            return self.conn.execute(sql,args).fetchone() is not None

    def recent_buy_attempt(self, strategy_id, *, since):
        with self.lock:
            return self.conn.execute("""SELECT 1 FROM orders WHERE strategy_id=? AND action='BUY'
                AND created_at>? LIMIT 1""", (strategy_id,since)).fetchone() is not None

    def reset_loss_limit(self, strategy_id, now=None):
        if not isinstance(strategy_id,str) or strategy_id not in STRATEGIES:
            raise ValueError("未知实盘策略")
        with self.lock,self.conn:
            now=time.time() if now is None else now
            # Keep each realized baseline, including partial exits. A later close
            # must not bring pre-reset profit/loss back into the risk window.
            baseline={str(p["id"]):p["realized_pnl"] for p in self.positions(strategy_id) if p["realized_pnl"]}
            self.conn.execute("INSERT OR REPLACE INTO loss_limit_resets VALUES(?,?,?)",
                              (strategy_id,now,json.dumps(baseline)))
            config=self.config();config["revision"]+=1
            self.conn.execute("UPDATE live_config SET json=? WHERE id=1",(json.dumps(config),))
            self.conn.execute("INSERT INTO events(at,kind,message) VALUES(?,?,?)",
                              (now,"loss_limit_reset",f"{STRATEGIES[strategy_id]} 已重置实盘亏损限额，从此刻重新统计；历史盈亏和持仓保留"))
        return self.loss_limit_status(strategy_id,now)

    def loss_limit_status(self, strategy_id, now=None):
        now=time.time() if now is None else now
        with self.lock:
            reset=self.conn.execute("SELECT * FROM loss_limit_resets WHERE strategy_id=?",(strategy_id,)).fetchone()
            reset_at=reset["reset_at"] if reset else None
            baseline=json.loads(reset["position_pnl_json"]) if reset else {}
            start=max(now-86400,reset_at) if reset_at is not None else now-86400
            # Preserve the existing conservative treatment of partial exits,
            # counting only changes after the manual reset when one exists.
            realized=sum(p["realized_pnl"]-baseline.get(str(p["id"]),0) for p in self.positions(strategy_id)
                         if (p["closed_at"] if p["closed_at"] is not None else now)>=start)
            return {"reset_at":reset_at,"window_start":start,"realized_pnl":round(realized,6)}

    def daily_pnl(self, strategy_id, now=None):
        return self.loss_limit_status(strategy_id,now)["realized_pnl"]

    def record_wallet(self, balance, total_equity, now=None):
        with self.lock,self.conn:
            self.conn.execute("INSERT INTO wallet_curve(at,balance,total_equity) VALUES(?,?,?)", (time.time() if now is None else now,balance,total_equity))

    def wallet_curve_window(self):
        """Seven-day display history, independently read and cached for 30 seconds.

        Preserve actual extrema and endpoints instead of truncating the oldest
        records or averaging away drawdowns. Never hold the order ledger lock.
        """
        with self._curve_lock:
            if self._curve_cache and time.monotonic()-self._curve_cache[0]<30:return self._curve_cache[1]
            now=time.time();cutoff=now-7*86400;max_points=1200
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as reader:
                reader.row_factory=sqlite3.Row
                reader.execute('BEGIN')
                first=reader.execute('SELECT min(at) FROM wallet_curve WHERE total_equity IS NOT NULL').fetchone()[0]
                raw=reader.execute('SELECT id,at,balance,total_equity FROM wallet_curve '
                                   'WHERE at>=? AND at<=? AND total_equity IS NOT NULL ORDER BY at,id',(cutoff,now)).fetchall()
            rows=[r for r in raw if isinstance(r['total_equity'],(int,float)) and math.isfinite(r['total_equity'])]
            selected=rows
            if len(rows)>max_points:
                buckets={};bucket_count=(max_points-2)//2
                step=max(.000001,(rows[-1]['at']-rows[0]['at'])/bucket_count)
                for row in rows[1:-1]:
                    key=min(bucket_count-1,int((row['at']-rows[0]['at'])/step))
                    low,high=buckets.get(key,(row,row))
                    if row['total_equity']<low['total_equity']:low=row
                    if row['total_equity']>=high['total_equity']:high=row
                    buckets[key]=(low,high)
                chosen={rows[0]['id']:rows[0],rows[-1]['id']:rows[-1]}
                for pair in buckets.values():
                    for row in pair:chosen[row['id']]=row
                selected=sorted(chosen.values(),key=lambda r:(r['at'],r['id']))
            result={'rows':[{'created_at':r['at'],'cash_balance':r['balance'],'total_equity':r['total_equity']} for r in selected],
                    'meta':{'days':7,'window_start':cutoff,'window_end':now,'first_recorded_at':first,
                            'record_count':len(rows),'sample_count':len(selected),
                            'actual_start':rows[0]['at'] if rows else None,'actual_end':rows[-1]['at'] if rows else None}}
            self._curve_cache=(time.monotonic(),result)
            return result

    def valuation_books(self):
        with self.lock:
            return {r["token_id"]:json.loads(r["json"]) for r in self.conn.execute("SELECT * FROM valuation_books")}

    def save_valuation_book(self, observed):
        # Display-only cache: never used as evidence of a fill or for order execution.
        with self.lock,self.conn:
            self.conn.execute("INSERT OR REPLACE INTO valuation_books VALUES(?,?,?)",
                              (observed["token_id"],observed["round_id"],json.dumps(observed)))

    def prune_valuation_books(self):
        with self.lock,self.conn:
            self.conn.execute("DELETE FROM valuation_books WHERE token_id NOT IN "
                              "(SELECT token_id FROM positions WHERE remaining_shares>0 AND resolved_outcome IS NULL)")

    def snapshot(self, marks=None):
        marks=marks or {}
        now=time.time()
        history=self.wallet_curve_window()
        with self.lock:
            config=self.config();variants=[]
            sales={r["position_id"]:dict(r) for r in self.conn.execute("""SELECT o.position_id,
                sum(f.shares) AS shares,sum(f.notional) AS notional
                FROM fills f JOIN orders o ON o.id=f.order_id
                WHERE o.action='SELL' AND o.position_id IS NOT NULL GROUP BY o.position_id""")}
            for key,label in STRATEGIES.items():
                positions=self.positions(key);open_rows=[p for p in positions if p["remaining_shares"]>0]
                for p in positions:
                    sale=sales.get(p["id"],{})
                    p["entry_price"]=(p["total_cost"]-p["buy_fee"])/p["initial_shares"] if p["initial_shares"]>0 else None
                    p["sell_price"]=sale["notional"]/sale["shares"] if sale.get("shares",0)>0 else None
                    p["sold_shares"]=sale.get("shares",0)
                    p["settlement_price"]=(1. if p["outcome"]==p["resolved_outcome"] else 0.) if p["resolved_outcome"] in ("Up","Down") else None
                closed=[p for p in positions if p["closed_at"] is not None]
                realized=round(sum(p["realized_pnl"] for p in positions),6)
                unrealized=0.;value=0.;complete=True;observed_times=[];pending_count=0
                for p in open_rows:
                    observed=marks.get(p["id"])
                    pending=self.market(p["round_id"])["ends_at"]<=now
                    pending_count+=int(pending)
                    mark=(observed["value"] if isinstance(observed,dict)
                          and abs(observed["shares"]-p["remaining_shares"])<.000001
                          and observed.get("token_id")==p["token_id"] and observed.get("round_id")==p["round_id"]
                          and isinstance(observed.get("as_of"),(int,float)) and 0<observed["as_of"]<=now
                          and math.isfinite(observed["value"]) and observed["value"]>=0 else None)
                    if mark is None:complete=False
                    else:
                        value+=mark;unrealized+=mark-p["remaining_cost"];observed_times.append(observed["as_of"])
                    p["mark_value"]=mark;p["unrealized_pnl"]=round(mark-p["remaining_cost"],6) if mark is not None else None
                    age=round(now-observed["as_of"],1) if mark is not None else None
                    p["valuation"]={"status":"missing" if mark is None else "pending" if pending else
                                    "stale" if age>30 else "cached" if now>observed["valid_until"] else "fresh",
                                    "as_of":observed["as_of"] if mark is not None else None,"age_seconds":age,
                                    "pending_settlement":pending}
                oldest=min(observed_times) if observed_times else None
                statuses={p["valuation"]["status"] for p in open_rows}
                valuation={"status":next((s for s in ("missing","pending","stale","cached","fresh") if s in statuses),"empty"),
                           "as_of":oldest,"age_seconds":round(now-oldest,1) if oldest else None,
                           "missing_positions":len(open_rows)-len(observed_times),"pending_positions":pending_count}
                peak=0.;drawdown=0.;cumulative=0.
                settled_pnl_curve = []
                if key in (EXIT_SETTLE_LIVE, first_strong.KEY) and closed:
                    settled_pnl_curve.append({"created_at":positions[0]["opened_at"], "realized_pnl":0.0})
                for p in sorted(closed,key=lambda x:(x["closed_at"],x["id"])):
                    cumulative+=p["realized_pnl"];peak=max(peak,cumulative);drawdown=max(drawdown,peak-cumulative)
                    if settled_pnl_curve:
                        settled_pnl_curve.append({"created_at":p["closed_at"], "realized_pnl":round(cumulative,6)})
                orders=[dict(r) for r in self.conn.execute("SELECT * FROM orders WHERE strategy_id=? ORDER BY created_at DESC LIMIT 50",(key,))]
                known={o["id"] for o in orders}
                orders.extend(o for o in self.active_orders(key) if o["id"] not in known)
                variants.append({"variant_id":key,"combo":label,"settings":config["strategies"][key],
                    "risk":self.risk_usage(key),
                    "loss_limit":self.loss_limit_status(key,now),
                    "metrics":{"realized_pnl":realized,"unrealized_pnl":round(unrealized,6) if complete else None,
                    "total_pnl":round(realized+unrealized,6) if complete else None,"position_value":round(value,6) if complete else None,
                    "valuation":valuation,
                    "open_risk":round(sum(p["remaining_cost"] for p in open_rows),6),"open_trades":len(open_rows),
                    "settled_trades":len(closed),"win_rate":sum(p["realized_pnl"]>0 for p in closed)/len(closed)*100 if closed else None,
                    "max_drawdown":round(drawdown,6),"claim_due":round(sum(p["claim_due"] for p in positions),6),
                    "reserved_cash":round(sum(o["requested_cash"] for o in self.active_orders(key) if o["action"]=="BUY"),6)},
                    "open_trades":open_rows,"recent_trades":list(reversed(positions[-50:])),"orders":orders})
                if key in (EXIT_SETTLE_LIVE, first_strong.KEY):
                    variants[-1]["settled_pnl_curve"] = _settlement_curve_points(settled_pnl_curve)
            return {"enabled":config["enabled"],"settings":{k:v for k,v in config.items() if k!="retired_strategies"},"variants":variants,"wallet_curve":history['rows'],
                    "wallet_curve_meta":history['meta'],
                    "quarantined_orders":[o for o in self.active_orders() if o["state"]=="QUARANTINED"],
                    "events":[dict(r) for r in self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 10")]}

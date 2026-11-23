"""Prospective Paper trials with independent wallets and one public market feed."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .paper_execution import buy_terms, checked_info, floor_to, minimum_sell_price, sweep_book
from .shared_market_inputs import quote_error
from .strategy_league_policies import CATALOG_VERSION, POLICIES, entry_decision, exit_decision, sample_frame


INITIAL = 100.
BUDGET = 5.
DEPTH_FRACTION = .5
ARRIVAL_DELAY = 1.
MAX_ARRIVAL_AGE = 4.
MAX_FEED_GAP = 15.
RETRY_SECONDS = 5.
HISTORY_LOOKBACK = 34.
FINAL_SETTLEMENT_SECONDS = 2.
VERSION = "league-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS league_run (
 id INTEGER PRIMARY KEY CHECK (id=1), starts_at INTEGER NOT NULL, catalog_hash TEXT NOT NULL,
 created_at REAL NOT NULL, last_seen REAL, last_valid REAL);
CREATE TABLE IF NOT EXISTS league_accounts (
 strategy_id TEXT PRIMARY KEY, cash REAL NOT NULL DEFAULT 100, realized REAL NOT NULL DEFAULT 0,
 realized_peak REAL NOT NULL DEFAULT 0, equity REAL NOT NULL DEFAULT 100, equity_peak REAL NOT NULL DEFAULT 100,
 max_drawdown REAL NOT NULL DEFAULT 0, unknown_shares REAL NOT NULL DEFAULT 0,
 stopped_at REAL, last_reason TEXT NOT NULL DEFAULT '等待首个完整市场', last_order_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS league_admission_policy (
 id INTEGER PRIMARY KEY CHECK (id=1), unrestricted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS league_risk_releases (
 strategy_id TEXT PRIMARY KEY, stopped_at REAL NOT NULL, released_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS league_rounds (
 round_id TEXT PRIMARY KEY, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL, first_seen REAL NOT NULL,
 last_seen REAL NOT NULL, last_valid REAL, outcome TEXT, resolved_at REAL, gap INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS league_orders (
 id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, round_id TEXT NOT NULL, side TEXT NOT NULL,
 action TEXT NOT NULL, limit_price REAL NOT NULL, submitted_at REAL NOT NULL, matched_at REAL, finished_at REAL,
 status TEXT NOT NULL, filled_shares REAL NOT NULL DEFAULT 0, fee REAL NOT NULL DEFAULT 0,
 cash REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL, result_reason TEXT, token TEXT NOT NULL,
 source_ms REAL NOT NULL, received_ms REAL NOT NULL, notional_budget REAL NOT NULL, requested_shares REAL NOT NULL,
 tick_size TEXT NOT NULL, min_size REAL NOT NULL, fee_rate REAL NOT NULL, fee_exponent REAL NOT NULL,
 decision_snapshot TEXT, arrival_snapshot TEXT, arrival_source_ms REAL, arrival_received_ms REAL, fills TEXT);
CREATE INDEX IF NOT EXISTS league_order_round ON league_orders(round_id,strategy_id);
CREATE INDEX IF NOT EXISTS league_market_end ON league_rounds(ends_at);
CREATE TABLE IF NOT EXISTS league_positions (
 strategy_id TEXT NOT NULL, round_id TEXT NOT NULL, side TEXT NOT NULL, opened_at REAL NOT NULL,
 closed_at REAL, shares REAL NOT NULL, stake REAL NOT NULL, bought_shares REAL NOT NULL, cost REAL NOT NULL,
 proceeds REAL NOT NULL DEFAULT 0, pnl REAL NOT NULL DEFAULT 0, fees REAL NOT NULL,
 consumed_key TEXT, consumed TEXT, PRIMARY KEY(strategy_id,round_id));
CREATE INDEX IF NOT EXISTS league_positions_by_round ON league_positions(round_id,strategy_id);
CREATE INDEX IF NOT EXISTS league_open_positions ON league_positions(strategy_id,round_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS league_pnl_events (
 strategy_id TEXT NOT NULL, round_id TEXT NOT NULL, at REAL NOT NULL, pnl REAL NOT NULL);
CREATE INDEX IF NOT EXISTS league_recent_pnl ON league_pnl_events(at);
CREATE TABLE IF NOT EXISTS league_round_decisions (
 strategy_id TEXT NOT NULL, round_id TEXT NOT NULL, first_seen REAL NOT NULL, first_reason TEXT NOT NULL,
 first_signal_at REAL, side TEXT, limit_price REAL, PRIMARY KEY(strategy_id,round_id));
"""


def catalog_hash(policies=POLICIES):
    parameters = [VERSION, CATALOG_VERSION, [asdict(p) for p in policies]]
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _valid_book(frame, side, now, info, rules):
    market = frame.market
    if not info or not market or quote_error(market, frame.quotes, side, now):
        return None
    raw = frame.quotes[side]
    token = market.up_token if side == "Up" else market.down_token
    rule = rules.get(side) or rules.get(token) or {}
    if rule.get("token_id") != token or str(rule.get("tick_size")) != str(info["tick_size"]):
        return None
    if raw.get("tick_size") is not None and str(raw["tick_size"]) != str(info["tick_size"]):
        return None
    try:
        minimum = float(rule.get("min_order_size") or 0)
        if not math.isfinite(minimum) or minimum <= 0:
            return None
        for levels, top, choose in (("asks","best_ask", min), ("bids","best_bid", max)):
            orders = raw.get(levels) or []
            if not orders or any(not (0 < float(p["price"]) < 1 and math.isfinite(float(p["size"]))
                                      and float(p["size"]) > 0) for p in orders):
                return None
            if abs(choose(float(p["price"]) for p in orders) - raw[top]) > 1e-7:
                return None
    except (KeyError, ValueError, TypeError):
        return None
    return {**raw, "tick_size": info["tick_size"], "min_order_size": minimum}


def _sell_limit(book, info):
    tick = float(info["tick_size"])
    return round(max(tick, math.floor((book["best_bid"]-.02)/tick+1e-9)*tick), 6)


def _available_bids(position, book):
    identity = hashlib.sha256(json.dumps(sorted((float(l["price"]), float(l["size"])) for l in book["bids"]),
                                         separators=(",", ":")).encode()).hexdigest()
    return identity, json.loads(position["consumed"]) if position["consumed_key"] == identity else {}


def _sale_estimate(position, book, info):
    if not book or not info:
        return None
    qty = floor_to(position["shares"], 2)
    if qty < book["min_order_size"]:
        return None
    _, consumed = _available_bids(position, book)
    return sweep_book(book, "SELL", _sell_limit(book,info), info, shares=qty,
                      fraction=DEPTH_FRACTION, consumed=consumed)


class LeagueEngine:
    """The caller owns the shared data sources. All order and settlement writes are atomic."""
    def __init__(self, path, *, start_at, clock=time.time, policies=POLICIES):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policies = tuple(policies)
        self.clock = clock
        self.lock = threading.RLock()
        self.history = deque(maxlen=90)
        self.history_round = None
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(_SCHEMA)
            digest = catalog_hash(self.policies)
            with self.db:
                if int(start_at) != start_at or start_at <= 0 or int(start_at)%300:
                    raise ValueError("试验时间必须在五分钟市场开始时")
                self.db.execute("INSERT OR IGNORE INTO league_run(id,starts_at,catalog_hash,created_at) VALUES(1,?,?,?)",
                                (int(start_at),digest,clock()))
                run = self.db.execute("SELECT * FROM league_run WHERE id=1").fetchone()
                if run["catalog_hash"] != digest:
                    raise ValueError("策略版本与已存在的试验账本不一致")
                self.starts_at = run["starts_at"]
                self.coverage_lookback = max(p.window[1] for p in self.policies) + HISTORY_LOOKBACK
                ids = [p.key for p in self.policies]
                if not ids or len(ids) != len(set(ids)):
                    raise ValueError("策略编号必须唯一")
                self.db.executemany("INSERT OR IGNORE INTO league_accounts(strategy_id) VALUES(?)",[(k,) for k in ids])
                # Older markets may still await official settlement when the next market opens.
                self.db.execute("DROP INDEX IF EXISTS league_one_position")
                self.db.execute("DROP INDEX IF EXISTS league_one_order")
                self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS league_one_round_order
                    ON league_orders(strategy_id,round_id) WHERE status='PENDING'""")
                changed_at = self.starts_at if run["last_seen"] is None else max(self.starts_at, clock())
                self.db.execute("INSERT OR IGNORE INTO league_admission_policy VALUES(1,?)", (changed_at,))
                self.db.execute("""INSERT OR IGNORE INTO league_risk_releases(strategy_id,stopped_at,released_at)
                    SELECT strategy_id,stopped_at,? FROM league_accounts WHERE stopped_at IS NOT NULL""", (clock(),))
                self.db.execute("""UPDATE league_accounts SET stopped_at=NULL,last_reason='已恢复候选采样，等待新信号'
                    WHERE stopped_at IS NOT NULL""")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def cancel_pending(self, now):
        with self.lock, self.db:
            self.history.clear()
            self.db.execute("""UPDATE league_orders SET status='CANCELLED',finished_at=?,
                result_reason='模拟策略暂停或服务停止' WHERE status='PENDING'""", (now,))

    def pending_rounds(self, now):
        with self.lock:
            return [r[0] for r in self.db.execute("""SELECT DISTINCT r.round_id FROM league_rounds r
                JOIN league_positions p ON p.round_id=r.round_id WHERE r.outcome IS NULL AND r.ends_at<=?
                AND p.closed_at IS NULL ORDER BY r.ends_at""", (now,))]

    def settle(self, round_id, outcome, now, *, source):
        if source != "polymarket_official" or outcome not in ("Up","Down"):
            return False
        with self.lock, self.db:
            round_row = self.db.execute("SELECT ends_at,outcome FROM league_rounds WHERE round_id=?", (round_id,)).fetchone()
            if not round_row or round_row["ends_at"] > now or round_row["outcome"] is not None:
                return False
            self.db.execute("UPDATE league_rounds SET outcome=?,resolved_at=? WHERE round_id=?", (outcome,now,round_id))
            self.db.execute("""UPDATE league_orders SET status='CANCELLED',finished_at=?,result_reason='已官方结算'
                WHERE round_id=? AND status='PENDING'""", (now,round_id))
            # Only the market in the last observed frame can have a nonzero book valuation.
            last = self.db.execute("""SELECT run.last_seen,r.round_id,r.ends_at FROM league_run run
                LEFT JOIN league_rounds r ON r.last_seen=run.last_seen AND r.starts_at<=run.last_seen
                AND run.last_seen<r.ends_at WHERE run.id=1 ORDER BY r.starts_at DESC LIMIT 1""").fetchone()
            gap = last["last_seen"] is None or now-last["last_seen"] > MAX_FEED_GAP
            keep_mark = not gap and last["round_id"] not in (None, round_id) and last["ends_at"] > now
            for p in self.db.execute("SELECT * FROM league_positions WHERE round_id=? AND closed_at IS NULL",
                                     (round_id,)).fetchall():
                proceeds = p["shares"] if p["side"] == outcome else 0.
                key = p["strategy_id"]
                other_shares = self.db.execute("""SELECT coalesce(sum(shares),0) FROM league_positions
                    WHERE strategy_id=? AND round_id<>? AND closed_at IS NULL""", (key,round_id)).fetchone()[0]
                value, unknown = 0., other_shares
                if other_shares and keep_mark:
                    account = self.db.execute("SELECT equity-cash,unknown_shares FROM league_accounts WHERE strategy_id=?",
                                              (key,)).fetchone()
                    value = max(0., account[0])
                    unknown = min(other_shares, max(0., account[1]-p["shares"]))
                if gap:
                    self._mark(key, 0., other_shares + p["shares"], True)
                self._credit(p, proceeds, p["shares"], 0., now)
                self._mark(key, value, unknown, False)
            return True

    def _credit(self, position, proceeds, shares, fees, now):
        shares = min(shares,position["shares"])
        remaining = round(position["shares"]-shares,6)
        basis = position["stake"] if remaining == 0 else round(position["stake"]*shares/position["shares"],6)
        gain = round(proceeds-basis,6)
        key = position["strategy_id"]
        self.db.execute("""UPDATE league_positions SET proceeds=round(proceeds+?,6), fees=round(fees+?,6),
            pnl=round(pnl+?,6), shares=?, stake=round(stake-?,6), closed_at=?
            WHERE strategy_id=? AND round_id=?""",
            (proceeds,fees,gain,remaining,basis,now if remaining == 0 else None,key,position["round_id"]))
        self.db.execute("""UPDATE league_accounts SET cash=round(cash+?,6), realized=round(realized+?,6),
            realized_peak=max(realized_peak,round(realized+?,6)) WHERE strategy_id=?""", (proceeds,gain,gain,key))
        self.db.execute("INSERT INTO league_pnl_events VALUES(?,?,?,?)", (key,position["round_id"],now,gain))

    def _mark(self, key, proceeds, unknown, gap):
        unknown = max(0., round(unknown,6))
        self.db.execute("""UPDATE league_accounts SET equity=round(cash+?,6),
            max_drawdown=max(max_drawdown,round(equity_peak-cash-?,6)),
            equity_peak=max(equity_peak,round(cash+?,6)), unknown_shares=? WHERE strategy_id=?
            AND (equity!=round(cash+?,6) OR unknown_shares!=? OR ?)""",
            (proceeds,0. if gap else proceeds,proceeds,unknown,key,proceeds,unknown,int(gap)))

    def _arrive(self, order, frame, now, books, info):
        if now < order["submitted_at"]+ARRIVAL_DELAY:
            return
        book = books.get(order["side"])
        if (not book or not frame.market or frame.market.round_id != order["round_id"]
                or now >= frame.market.ends_at or now > order["submitted_at"]+MAX_ARRIVAL_AGE or not info):
            return self._cancel(order,now,"到达时缺少同局新鲜盘口或已超时")
        if (min(book["source_ms"],book["received_ms"]) < order["submitted_at"]*1000
                or book["source_ms"] <= order["source_ms"] or book["received_ms"] <= order["received_ms"]
                or frame.snapshot_id == order["decision_snapshot"]):
            # Wait only inside the bounded arrival interval; never replay the pre-decision book.
            return
        if (str(info["tick_size"]) != order["tick_size"] or book["min_order_size"] != order["min_size"]
                or float(info["fee_rate"]) != order["fee_rate"] or float(info["fee_exponent"]) != order["fee_exponent"]):
            return self._cancel(order,now,"市场费用或最小委托规则已变化")
        key = order["strategy_id"]
        consumed = {}
        position = None
        if order["action"] == "SELL":
            position = self.db.execute("SELECT * FROM league_positions WHERE strategy_id=? AND round_id=? AND closed_at IS NULL",
                                       (key,order["round_id"])).fetchone()
            if not position:
                return self._cancel(order,now,"持仓已结清")
            identity, consumed = _available_bids(position,book)
        fill = sweep_book(book,order["action"],order["limit_price"],info,notional_budget=order["notional_budget"],
                          shares=order["requested_shares"],fraction=DEPTH_FRACTION,consumed=consumed)
        if fill["shares"] <= 0 or fill["cash"] <= 0:
            return self._cancel(order,now,"到达时限价内无可用深度")
        if order["action"] == "BUY":
            if fill["cash"] > BUDGET+.000001:
                return self._cancel(order,now,"费用超过买入预算")
            self.db.execute("""INSERT INTO league_positions(strategy_id,round_id,side,opened_at,shares,stake,
                bought_shares,cost,fees) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (key,order["round_id"],order["side"],now,fill["shares"],fill["cash"],fill["shares"],
                             fill["cash"],fill["fee"]))
            self.db.execute("UPDATE league_accounts SET cash=round(cash-?,6) WHERE strategy_id=?", (fill["cash"],key))
        else:
            self._credit(position,fill["cash"],fill["shares"],fill["fee"],now)
            for level in fill["levels"]:
                price = str(level["price"])
                consumed[price] = consumed.get(price,0.)+level["shares"]
            self.db.execute("UPDATE league_positions SET consumed_key=?,consumed=? WHERE strategy_id=? AND round_id=?",
                            (identity,json.dumps(consumed),key,order["round_id"]))
        self.db.execute("""UPDATE league_orders SET status=?,matched_at=?,finished_at=?,filled_shares=?,
            fee=?,cash=?,arrival_snapshot=?,arrival_source_ms=?,arrival_received_ms=?,fills=? WHERE id=?""",
            ("FILLED" if fill["full"] else "PARTIAL",now,now,fill["shares"],fill["fee"],fill["cash"],frame.snapshot_id,
             book["source_ms"],book["received_ms"],json.dumps(fill["levels"]),order["id"]))

    def _cancel(self, order, now, reason):
        self.db.execute("UPDATE league_orders SET status='CANCELLED',finished_at=?,result_reason=? WHERE id=?",
                        (now,reason,order["id"]))

    def _submit(self, key, signal, frame, now, book, info, position):
        try:
            action = signal.action
            if action == "BUY":
                terms = buy_terms(BUDGET,signal.limit,info,book)
                limit, budget, qty = terms["limit"], terms["notional_budget"], 0.
            else:
                qty = floor_to(position["shares"],2)
                if qty < book["min_order_size"]:
                    return "剩余份额低于最小卖单，等官方结算"
                limit, budget = _sell_limit(book,info), 0.
                if signal.min_net_bid is not None:
                    limit = max(limit,minimum_sell_price(signal.min_net_bid,info))
                if limit > book["best_bid"]:
                    return "当前买盘低于策略退出净价"
        except ValueError as exc:
            return str(exc)
        self.db.execute("""INSERT INTO league_orders(strategy_id,round_id,side,action,limit_price,submitted_at,
            status,reason,token,source_ms,received_ms,notional_budget,requested_shares,tick_size,min_size,fee_rate,
            fee_exponent,decision_snapshot) VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?,?,?,?,?,?,?,?)""",
            (key,frame.market.round_id,signal.side,action,limit,now,signal.reason,book["token_id"],book["source_ms"],
             book["received_ms"],budget,qty,str(info["tick_size"]),book["min_order_size"],info["fee_rate"],
             info["fee_exponent"],frame.snapshot_id))
        self.db.execute("UPDATE league_accounts SET last_order_at=? WHERE strategy_id=?", (now,key))
        return signal.reason

    def step(self, frame, decision, info, book_rules):
        now = self.clock()
        at = frame.observed_at
        market = frame.market
        if now < self.starts_at:
            return
        with self.lock, self.db:
            run = self.db.execute("SELECT last_seen FROM league_run WHERE id=1").fetchone()
            if run[0] is not None and now <= run[0]:
                return
            gap = run[0] is not None and now-run[0] > MAX_FEED_GAP
            try:
                info = checked_info(info or {})
            except (ValueError, TypeError, KeyError):
                info = None
            fresh = (market is not None and self.starts_at <= market.started_at <= at <= now < market.ends_at
                     and now-at <= 2.5 and not frame.error)
            books = {side:_valid_book(frame,side,now,info,book_rules or {}) for side in ("Up","Down")} if fresh else {}
            valid = fresh and all(books.values())
            sample = sample_frame(frame,decision,info) if valid and decision is not None else None
            usable = sample is not None
            rid = market.round_id if market and self.starts_at <= market.started_at <= now < market.ends_at else None
            if self.history_round != rid or gap:
                # On restart or rollover a previous market may have ended without a final live tick.
                self.db.execute("""UPDATE league_rounds SET gap=1 WHERE ends_at<=? AND gap=0
                  AND (last_valid IS NULL OR last_valid<ends_at-?)""", (now, MAX_FEED_GAP+FINAL_SETTLEMENT_SECONDS))
            if rid:
                # Require continuous coverage from the first possible strategy lookback, through the last exit.
                # Feeds often warm up while every strategy is still outside its entry window.
                start_needed = max(market.started_at, market.ends_at-self.coverage_lookback)
                end_needed = market.ends_at-FINAL_SETTLEMENT_SECONDS
                check_until = min(now,end_needed)
                self.db.execute("""INSERT INTO league_rounds(round_id,starts_at,ends_at,first_seen,last_seen,last_valid,gap)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET
                    gap=max(gap, ?-max(?,coalesce(league_rounds.last_valid,?))>?),
                    last_seen=excluded.last_seen,last_valid=coalesce(excluded.last_valid,league_rounds.last_valid)""",
                    (rid,market.started_at,market.ends_at,now,now,now if usable else None,
                     int(check_until-start_needed>MAX_FEED_GAP),check_until,start_needed,start_needed,MAX_FEED_GAP))
            if self.history_round != rid:
                self.history.clear()
                self.history_round = rid
            while self.history and self.history[0].at < at-65:
                self.history.popleft()
            orders = self.db.execute("SELECT * FROM league_orders WHERE status='PENDING'").fetchall()
            pending = {r["strategy_id"] for r in orders if r["round_id"] == rid}
            for order in orders:
                self._arrive(order,frame,now,books,info)
            accounts = self.db.execute("SELECT * FROM league_accounts").fetchall()
            wallets = {r["strategy_id"]:r for r in accounts}
            positions = {}
            open_shares = defaultdict(float)
            for position in self.db.execute("SELECT * FROM league_positions WHERE closed_at IS NULL"):
                key = position["strategy_id"]
                open_shares[key] += position["shares"]
                if position["round_id"] == rid:
                    positions[key] = position
            used = {r[0] for r in self.db.execute("SELECT strategy_id FROM league_positions WHERE round_id=?", (rid,))}
            # A single SQLite transaction captures one causal frame for all participants.
            for policy in self.policies:
                key = policy.key
                wallet, position = wallets[key], positions.get(key)
                book = books.get(position["side"]) if position else None
                mark = _sale_estimate(position,book,info) if position else None
                if open_shares[key] or wallet["equity"] != wallet["cash"] or wallet["unknown_shares"]:
                    self._mark(key, mark["cash"] if mark else 0., open_shares[key]-(mark["shares"] if mark else 0.), gap)
                ready = usable and key not in pending and now-wallet["last_order_at"] >= RETRY_SECONDS
                if not ready:
                    reason = "等待独立新盘口模拟成交" if key in pending else "等待本局新鲜行情或完整参数"
                elif position:
                    px = mark["cash"]/mark["shares"] if mark and mark["shares"] else None
                    signal = exit_decision(policy,sample,self.history,position["side"],px,
                                           entry_cost=position["cost"]/position["bought_shares"])
                    reason = signal.reason
                    if signal.action == "SELL" and px is not None:
                        reason = self._submit(key,signal,frame,now,book,info,position)
                elif key in used:
                    reason = "本局已经买过一次"
                else:
                    signal = entry_decision(policy,sample,self.history)
                    reason = signal.reason
                    if signal.action == "BUY":
                        self.db.execute("""INSERT INTO league_round_decisions VALUES(?,?,?,?,?,?,?)
                          ON CONFLICT(strategy_id,round_id) DO UPDATE SET first_signal_at=excluded.first_signal_at,
                          side=excluded.side,limit_price=excluded.limit_price WHERE first_signal_at IS NULL""",
                          (key,rid,now,reason,now,signal.side,signal.limit))
                        # Fixed-notional Paper trades continue through negative cash without resetting PnL.
                        reason = self._submit(key,signal,frame,now,books[signal.side],info,None)
                if reason != wallet["last_reason"]:
                    self.db.execute("UPDATE league_accounts SET last_reason=? WHERE strategy_id=?", (reason,key))
                if rid and self.history_round == rid and not self.history:
                    self.db.execute("INSERT OR IGNORE INTO league_round_decisions VALUES(?,?,?,?,NULL,NULL,NULL)",
                                    (key,rid,now,reason))
            self.db.execute("UPDATE league_run SET last_seen=?, last_valid=coalesce(?,last_valid) WHERE id=1",
                            (now,now if usable else None))
            if sample and (not self.history or at >= self.history[-1].at+1):
                self.history.append(sample)

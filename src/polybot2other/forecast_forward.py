"""Prospective, fee-inclusive measurements for the two isolated limit experiment ledgers."""
from bisect import bisect_right
from contextlib import closing
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .paper_execution import floor_to, sweep_book

CANDIDATE = "FORECAST_LIMIT_060_PAPER"
CONTROL = "FORECAST_LIMIT_CONTROL_PAPER"
DAY = 86400
ROUND = 300
DAYS = 7
ROUNDS = 100


class ForecastForwardAudit:
    def __init__(self, engine):
        self.engine = engine
        store = engine.store
        with store._lock, store.conn as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS forecast_limit_run (
                id INTEGER PRIMARY KEY CHECK(id=1), starts_at INTEGER NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS forecast_limit_marks (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, equity REAL NOT NULL,
                peak REAL NOT NULL, drawdown REAL NOT NULL, liquidatable REAL NOT NULL,
                has_exposure INTEGER NOT NULL, unknown_shares REAL NOT NULL, gap INTEGER NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_limit_marks_at ON forecast_limit_marks(at)")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_forecast_limit_confirming ON paper_sim_orders(phase)
                            WHERE phase='CONFIRMING'""")
            run = conn.execute("SELECT starts_at FROM forecast_limit_run WHERE id=1").fetchone()
            if run is None:
                created = conn.execute("SELECT started_at FROM paper_sim_run WHERE id=1").fetchone()[0]
                default = getattr(engine.bot, "forecast_trial_boundary", math.ceil(created / ROUND) * ROUND)
                start = int(os.environ.get("POLYBOT2OTHER_FORECAST_LIMIT_START_AT", "").strip() or default)
                if start <= 0 or start % ROUND:
                    raise ValueError("前瞻试验起点必须是 BTC 五分钟市场的 Unix 整秒边界")
                conn.execute("INSERT INTO forecast_limit_run VALUES(1,?)", (start,))
            self.start_at = conn.execute("SELECT starts_at FROM forecast_limit_run WHERE id=1").fetchone()[0]
            last = conn.execute("SELECT * FROM forecast_limit_marks ORDER BY id DESC LIMIT 1").fetchone()
            self.initial = conn.execute("SELECT initial_balance FROM account WHERE id=1").fetchone()[0]
        self.peak = last["peak"] if last else self.initial
        self.drawdown = last["drawdown"] if last else 0.
        self.previous = last
        self.last_write = last["at"] if last else 0.
        self.preflight = {}

    def _liquidate(self, frame, now, rid, side, shares, consumed):
        market = frame.market
        info = self.engine.info_cache.get(rid)
        if not (market and market.round_id == rid and not frame.error and info and now < market.ends_at
                and not frame.quote_error(side, now)):
            return 0., shares
        book = frame.quotes[side]
        # WebSocket publishes timely depth but omits REST order minimum and tick size.
        # Reuse the matching token's persisted, validated buy preflight from this very round.
        token = book.get("token_id")
        key = (rid, token)
        if not book.get("min_order_size") and key not in self.preflight:
            c = self.engine.store.conn
            terms = c.execute("""SELECT json_extract(s.payload,'$.minimum_shares'),
                        json_extract(s.payload,'$.info.tick_size') FROM paper_orders o
                        JOIN paper_sim_orders s ON s.order_id=o.id WHERE o.round_id=? AND o.order_type='FAK'
                        AND s.phase IN ('CONFIRMING','FILLED','PARTIAL') AND json_extract(s.payload,'$.token')=?
                        ORDER BY o.created_at DESC LIMIT 1""", (rid, token)).fetchone()
            if terms:
                self.preflight = {key: tuple(terms)}
        minimum_fallback, tick_fallback = self.preflight.get(key, (None, None))
        minimum = float(book.get("min_order_size", minimum_fallback) or 0.)
        tick_size = book.get("tick_size", tick_fallback)

        qty = floor_to(shares, 2)
        if minimum <= 0 or qty < minimum or str(tick_size) != str(info["tick_size"]):
            return 0., shares
        tick = float(info["tick_size"])
        limit = max(tick, math.floor((book["best_bid"] - .02) / tick + 1e-9) * tick)
        fill = sweep_book(book, "SELL", limit, info, shares=qty, fraction=self.engine.policy.depth_fraction,
                          consumed=consumed)
        return fill["cash"], max(0., shares - fill["shares"])

    def observe(self, frame, now):
        if now < self.start_at:
            return
        store = self.engine.store
        with store._lock:
            conn = store.conn
            realized = conn.execute("SELECT realized_pnl FROM account WHERE id=1").fetchone()[0]
            open_rows = conn.execute("SELECT id,round_id,side,stake,shares FROM trades WHERE status='OPEN'").fetchall()
            pending = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM paper_sim_orders WHERE phase='CONFIRMING'")]
            # Match economic exposure as of the known fill while the normal ledger awaits confirmation.
            exposure = {r["id"]: dict(r) for r in open_rows}
            locked_cash = 0.
            for order in pending:
                fill = order["fill"]
                if order["action"] == "BUY":
                    exposure[-order["order_id"]] = dict(round_id=order["round_id"], side=order["side"],
                                                        stake=fill["cash"], shares=fill["shares"])
                else:
                    pos = exposure[order["position_id"]]
                    pos["shares"] = max(0., pos["shares"] - fill["shares"])
                    locked_cash += fill["cash"]
            cash = self.initial + realized + locked_cash - sum(p["stake"] for p in exposure.values())
            liquidatable = missing = 0.
            for p in exposure.values():
                if p["shares"] <= 0:
                    continue
                consumed = {}
                if frame.market and frame.market.round_id == p["round_id"]:
                    book = frame.quotes.get(p["side"]) or {}
                    levels = book.get("bids") or []
                    prior = conn.execute("SELECT fingerprint,levels FROM paper_sim_consumed WHERE token=? AND action='SELL'",
                                         (book.get("token_id"),)).fetchone()
                    if prior and hashlib.sha256(json.dumps(levels, sort_keys=True).encode()).hexdigest() == prior[0]:
                        consumed = json.loads(prior[1])
                try:
                    value, unknown = self._liquidate(frame, now, p["round_id"], p["side"], p["shares"], consumed)
                except (TypeError, ValueError, KeyError):
                    value, unknown = 0., p["shares"]
                liquidatable += value
                missing += unknown
            equity = round(cash + liquidatable, 6)
            previous = self.previous
            gap = bool(previous and now - previous["at"] > 15 and previous["has_exposure"])
            low = min(equity, previous["equity"] - previous["liquidatable"]) if gap else equity
            peak = max(self.peak, equity)
            dd = max(self.drawdown, self.peak - low)
            had_risk = bool(exposure)
            row = dict(at=now, equity=equity, peak=peak, drawdown=dd, liquidatable=liquidatable,
                       has_exposure=int(had_risk), unknown_shares=round(missing, 6), gap=int(gap))
            # Flat periods cost one heartbeat per minute. Record extrema and material changes immediately.
            changed = not previous or any(row[k] != previous[k] for k in
                                          ("peak", "drawdown", "has_exposure", "unknown_shares", "liquidatable", "equity"))
            if changed or gap or now-self.last_write >= (6 if had_risk else 60):
                with conn:
                    conn.execute("""INSERT INTO forecast_limit_marks(at,equity,peak,drawdown,liquidatable,
                        has_exposure,unknown_shares,gap) VALUES(:at,:equity,:peak,:drawdown,:liquidatable,
                        :has_exposure,:unknown_shares,:gap)""", row)
                self.last_write = now
            self.peak, self.drawdown, self.previous = peak, dd, row


def _trades(conn, schema, start, now):
    # A partial exit creates multiple trade rows. The original round is the statistical unit.
    return conn.execute(f"""SELECT t.round_id,m.ends_at,min(t.opened_at) AS opened,
        max(CASE WHEN t.settled_at<=? THEN t.settled_at END) AS last_settlement,
        max(CASE WHEN t.settled_at IS NULL OR t.settled_at>? THEN 1 ELSE 0 END) AS outstanding,
        sum(CASE WHEN t.settled_at<=? THEN t.pnl ELSE 0 END) AS pnl,
        sum(t.shares) AS shares
        FROM {schema}.trades t JOIN {schema}.market_rounds m ON m.round_id=t.round_id
        WHERE m.started_at>=? AND t.opened_at<=? GROUP BY t.round_id ORDER BY opened,t.round_id""",
        (now, now, now, start, now)).fetchall()


def _order_intervals(conn, schema, trades, start, now):
    by_round = {t["round_id"]: t for t in trades}
    result = []
    for o in conn.execute(f"""SELECT o.round_id,o.created_at,o.updated_at,s.phase
            FROM {schema}.paper_orders o JOIN {schema}.paper_sim_orders s ON s.order_id=o.id
            WHERE o.order_type='FAK' AND o.created_at>=? AND o.created_at<=?""", (start, now)):
        if o["phase"] in ("IN_FLIGHT", "CONFIRMING") or o["updated_at"] > now:
            released = math.inf
        elif o["phase"] in ("FILLED", "PARTIAL"):
            t = by_round.get(o["round_id"])
            released = max(t["last_settlement"],o["updated_at"]) if t and not t["outstanding"] else math.inf
        else:
            released = o["updated_at"]
        result.append((o["created_at"], released))
    return result


def _first_flat_boundary(candidate_trades, intervals, start, now):
    first = candidate_trades[:ROUNDS]
    if len(first) < ROUNDS or any(t["outstanding"] for t in first):
        return None
    target = max(start + DAYS*DAY, max(max(t["last_settlement"], t["ends_at"]) for t in first))
    target = math.ceil(target/ROUND)*ROUND
    for submitted, released in sorted(intervals):
        if submitted >= target:
            break
        if released > target:
            if released > now:
                return None
            target = math.ceil(released/ROUND)*ROUND
    return target if target <= now else None


def _unobserved_exposures(conn, schema, eligible, start, end):
    # Flat intervals can have sparse heartbeats. Count only unobserved time actually spent in a position.
    stamps = [r[0] for r in conn.execute(f"SELECT at FROM {schema}.forecast_limit_marks WHERE at>=? AND at<=? ORDER BY at",
                                          (start,end))]
    missing = 0
    for t in eligible:
        left = t["opened"]
        right = min(end, t["last_settlement"]) if not t["outstanding"] else end
        if right-left <= 15:
            continue
        ix = bisect_right(stamps,left)
        while ix < len(stamps) and stamps[ix] <= right and stamps[ix]-left <= 15:
            left = stamps[ix]
            ix += 1
        if right-left > 15:
            missing += 1
    return missing


def _arm(conn, schema, trades, start, end, now):
    eligible = [t for t in trades if t["opened"] < end]
    closed = [t for t in eligible if not t["outstanding"]]
    pnl = sum(t["pnl"] for t in closed)
    def cohort_pnl(cohort):
        if len(cohort) == 50 and all(not t["outstanding"] for t in cohort):
            return round(sum(t["pnl"] for t in cohort), 6)
    observed = conn.execute(f"""SELECT count(*),min(round_id),max(round_id) FROM
        (SELECT DISTINCT c.round_id FROM {schema}.forecast_checkpoints c
         JOIN {schema}.market_rounds m ON m.round_id=c.round_id
         WHERE m.started_at>=? AND m.started_at<?)""", (start,end)).fetchone()
    expected = max(0, int((min(end, now)-start)//ROUND))
    tables = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}
    # Missing entire markets, late initial observation, or a hole inside the entry window.
    clean = 0
    if "forecast_entry_trace" in tables:
        clean = conn.execute(f"""SELECT count(*) FROM {schema}.forecast_entry_trace w
            JOIN {schema}.market_rounds m ON m.round_id=w.round_id
            WHERE m.started_at>=? AND m.ends_at<=? AND w.stage='window_seen'
              AND json_extract(w.details,'$.late_start')=0 AND NOT EXISTS (
                  SELECT 1 FROM {schema}.forecast_entry_trace g
                  WHERE g.round_id=w.round_id AND g.stage='observation_gap')""", (start,end)).fetchone()[0]
    order_info = conn.execute(f"""SELECT count(*),coalesce(sum(s.phase IN ('IN_FLIGHT','CONFIRMING')),0)
        FROM {schema}.paper_orders o JOIN {schema}.paper_sim_orders s ON o.id=s.order_id
        WHERE o.order_type='FAK' AND o.created_at>=? AND o.created_at<?""", (start,end)).fetchone()
    mark = conn.execute(f"SELECT * FROM {schema}.forecast_limit_marks WHERE at<=? ORDER BY at DESC,id DESC LIMIT 1", (end,)).fetchone()
    marked_drawdown = mark["drawdown"] if mark else None
    if mark and len(closed) == len(eligible) and not order_info[1]:
        # A final settlement may arrive after the last quote sample at the flat boundary.
        initial = conn.execute(f"SELECT initial_balance FROM {schema}.account WHERE id=1").fetchone()[0]
        marked_drawdown = max(marked_drawdown, mark["peak"] - initial - pnl)
    stopped = None
    if "forecast_realized_peak" in tables:
        record = conn.execute(f"SELECT stopped_at FROM {schema}.forecast_realized_peak WHERE id=1").fetchone()
        stopped = record[0] if record else None
    return {
        "closed_rounds": len(closed), "filled_rounds": len(eligible), "submitted_orders": order_info[0],
        "pending_buys": order_info[1], "unresolved_rounds": len(eligible)-len(closed),
        "closed_pnl": round(pnl, 6), "stressed_pnl": round(pnl - .02*sum(t["shares"] for t in closed), 6),
        "first_50_pnl": cohort_pnl(eligible[:50]), "next_50_pnl": cohort_pnl(eligible[50:100]),
        "marked_drawdown": round(marked_drawdown, 6) if marked_drawdown is not None else None,
        "last_mark_at": mark["at"] if mark else None,
        "unpriced_shares": mark["unknown_shares"] if mark else None,
        "observed_rounds": observed[0], "first_observed_round": observed[1], "last_observed_round": observed[2],
        "data_gap_rounds": max(0, expected - clean), "expected_complete_rounds": expected,
        "valuation_gap_rounds": _unobserved_exposures(conn, schema, eligible, start, end),
        "flat_budget_exhausted": stopped is not None and stopped <= end, "stopped_at": stopped,
    }


def compare_forward(candidate_path, control_path, now):
    uris = [Path(p).resolve().as_uri()+"?mode=ro" for p in (candidate_path,control_path)]
    if uris[0] == uris[1]:
        return {"available": False, "reason": "等待两个独立 Paper 账本"}
    with closing(sqlite3.connect(uris[0], uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ? AS control", (uris[1],))
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        runs = conn.execute("""SELECT a.starts_at,b.starts_at FROM main.forecast_limit_run a
                   JOIN control.forecast_limit_run b ON a.id=b.id WHERE a.id=1""").fetchone()
        if runs is None or runs[0] != runs[1]:
            return {"available": False, "reason":"试验账户起点缺失或不同，无法同期比较"}
        start = runs[0]
        candidate_trades = _trades(conn,"main",start,now)
        control_trades = _trades(conn,"control",start,now)
        # Once sample size and duration are met, keep the same full-market cutoff on future reviews.
        intervals = _order_intervals(conn, "main", candidate_trades, start, now)
        intervals.extend(_order_intervals(conn, "control", control_trades, start, now))
        cutoff = _first_flat_boundary(candidate_trades, intervals, start, now)
        mature = cutoff is not None
        end = cutoff if mature else now
        candidate = _arm(conn,"main",candidate_trades,start,end,now)
        control = _arm(conn,"control",control_trades,start,end,now)
    status = "waiting" if now < start else "collecting"
    if candidate["flat_budget_exhausted"] and not mature:
        status = "failed"
    if mature and not any(a["pending_buys"] or a["unresolved_rounds"] for a in (candidate, control)):
        all_marked = all(a["last_mark_at"] is not None and end-a["last_mark_at"] <= 75
                         and a["valuation_gap_rounds"] == 0 for a in (candidate, control))
        criteria = (candidate["first_50_pnl"] > 0 and candidate["next_50_pnl"] > 0
                    and candidate["closed_pnl"] > control["closed_pnl"] and candidate["stressed_pnl"] > 0
                    and candidate["marked_drawdown"] is not None and candidate["marked_drawdown"] <= 10
                    and not candidate["flat_budget_exhausted"])
        status = ("passed" if criteria else "failed") if all_marked else "incomplete"
    return {"available": True, "status": status, "as_of": now, "start_at": start,
            "cohort_cutoff": end if mature else None, "elapsed_days": round(max(0.,end-start)/DAY,4),
            "requirements": {"days":DAYS,"closed_rounds":ROUNDS}, "candidate":candidate,"control":control}


@lru_cache(maxsize=4)
def _cached(candidate, control, time_bucket):
    return compare_forward(candidate, control, time.time())


def forecast_forward_status(bot):
    runner = getattr(bot, "strategy_experiments", None)
    if runner:
        with runner._lock:
            candidate, control = (runner._bots.get(key) for key in (CANDIDATE, CONTROL))
        if candidate and control:
            try:
                return _cached(str(candidate.store.db_path), str(control.store.db_path), int(time.time()//10))
            except sqlite3.Error:
                return {"available":False, "reason":"等待两个前瞻 Paper 账本就绪"}
    return {"available":False, "reason":"等待 60 美分策略和同期对照账户启动"}

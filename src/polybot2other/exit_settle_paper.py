"""Read the promoted Paper account directly from its original candidate wallet."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time

from .strategy_league import BUDGET
from .strategy_league_policies import CONTROL_KEY

VARIANT_ID = "FORECAST_EXIT_SETTLE"
KEY = "exit_settle"
NAME = "拿到官方结算"
TAGS = {"variant_id": VARIANT_ID, "combo": NAME, "account_scope": "strategy_experiment"}
POSITION_ROWS = """
    SELECT p.*, r.ends_at, r.outcome, o.reason,
           CASE WHEN o.filled_shares>0 THEN (o.cash-o.fee)/o.filled_shares END AS entry_price
    FROM league_positions p LEFT JOIN league_rounds r ON r.round_id=p.round_id
    LEFT JOIN league_orders o ON o.strategy_id=p.strategy_id AND o.round_id=p.round_id
        AND o.action='BUY' AND o.filled_shares>0
    WHERE p.strategy_id=?
"""


def is_exit_settle_scope(scope, variant_id):
    return scope in {"experiment", "strategy_experiment", "strategy_experiments"} and variant_id == VARIANT_ID


def _page_meta(total, size, limit, offset, **filters):
    loaded = min(total, offset + size)
    return dict(limit=limit, offset=offset, total=total, loaded=loaded, has_more=loaded < total, **filters)


def _summary(conn, after="", params=()):
    row = conn.execute("""
        SELECT count(*) AS total_count, count(closed_at) AS settled_count,
               count(*)-count(closed_at) AS open_count,
               coalesce(sum(closed_at IS NOT NULL AND pnl>0),0) AS win_count,
               coalesce(sum(closed_at IS NOT NULL AND pnl<0),0) AS loss_count,
               coalesce(round(sum(cost),6),0) AS total_stake,
               coalesce(round(sum(CASE WHEN closed_at IS NOT NULL THEN cost END),6),0) AS settled_stake,
               coalesce(round(sum(CASE WHEN closed_at IS NOT NULL THEN proceeds END),6),0) AS total_payout,
               coalesce(round(sum(CASE WHEN closed_at IS NOT NULL THEN pnl END),6),0) AS total_pnl,
               coalesce(round(sum(stake),6),0) AS open_risk,
               coalesce(round(sum(fees),6),0) AS fees
        FROM league_positions WHERE strategy_id=?""" + after, (KEY, *params)).fetchone()
    s = dict(row)
    s.update(win_rate=round(100*s["win_count"]/s["settled_count"], 4) if s["settled_count"] else None,
             roi_pct=round(100*s["total_pnl"]/s["settled_stake"], 4) if s["settled_stake"] else None,
             official_count=s["settled_count"], chainlink_count=0, early_exit_count=0)
    return s


def _trade(row):
    closed = row["closed_at"] is not None
    return {**TAGS, "id": row["round_id"], "round_id": row["round_id"], "side": row["side"],
            "opened_at": row["opened_at"], "settled_at": row["closed_at"], "ends_at": row["ends_at"],
            "status": "SETTLED" if closed else "OPEN", "strategy_type": NAME,
            "reason": row["reason"], "stake": row["cost"], "entry_price": row["entry_price"],
            "shares": row["bought_shares"] if closed else row["shares"], "remaining_shares": row["shares"],
            "payout": row["proceeds"] if closed else None, "pnl": row["pnl"] if closed else None,
            "roi_pct": round(100*row["pnl"]/row["cost"], 4) if closed and row["cost"] else None,
            "fee": row["fees"], "fees": row["fees"], "outcome": row["outcome"],
            "settlement_source": "polymarket_official" if closed else None,
            "settlement_source_label": "官方结算" if closed else None,
            "max_payout": row["bought_shares"], "max_profit": round(row["bought_shares"]-row["cost"], 6),
            "max_loss": row["cost"]}


class ExitSettlePaper:
    def __init__(self, bot):
        self.bot = bot

    @contextmanager
    def _read(self):
        path = Path(self.bot.store.db_path).resolve().parent / "strategy-league.sqlite3"
        try:
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                yield conn
        except sqlite3.Error as exc:
            raise ValueError("暂时读不到「拿到官方结算」的候选模拟账本") from exc

    @staticmethod
    def _account(conn):
        row = conn.execute("""SELECT a.*, r.starts_at, r.last_seen FROM league_run r
                            JOIN league_accounts a ON a.strategy_id=? WHERE r.id=1""", (KEY,)).fetchone()
        if row is None:
            raise ValueError("候选模拟钱包尚未初始化")
        return row

    @staticmethod
    def _principal(conn, account):
        # The original schema stores no separate deposit column. For this cash-only
        # wallet deposits = unspent cash + remaining cost basis - realized gains.
        open_basis = conn.execute("SELECT coalesce(sum(stake),0) FROM league_positions WHERE strategy_id=? AND closed_at IS NULL",
                                  (KEY,)).fetchone()[0]
        return round(account["cash"] + open_basis - account["realized"], 6)

    def _snapshot(self, conn):
        a = self._account(conn)
        initial = self._principal(conn, a)
        s = _summary(conn)
        enabled = bool(getattr(self.bot.settings, "strategy_league_enabled", False))
        missing = a["unknown_shares"] > 0
        total_pnl = None if missing else round(a["equity"] - initial, 6)
        metrics = dict(initial_balance=initial, total_equity=None if missing else a["equity"],
                       total_pnl=total_pnl, realized_pnl=a["realized"],
                       unrealized_pnl=None if missing else round(total_pnl-a["realized"], 6),
                       valuation_missing=missing, valuation_as_of=a["last_seen"], unpriced_shares=a["unknown_shares"],
                       cash_balance=a["cash"], reserved_cash=0, open_risk=s["open_risk"],
                       open_trades=s["open_count"], settled_trades=s["settled_count"], win_rate=s["win_rate"],
                       max_drawdown=a["max_drawdown"], fees=s["fees"])
        opened = [_trade(row) for row in conn.execute(POSITION_ROWS+" AND p.closed_at IS NULL ORDER BY p.opened_at DESC", (KEY,))]
        counts = dict(conn.execute("SELECT status,count(*) FROM league_orders WHERE strategy_id=? GROUP BY status", (KEY,)))
        control = conn.execute("SELECT realized FROM league_accounts WHERE strategy_id=?", (CONTROL_KEY,)).fetchone()
        variant = {**TAGS, "role": "与候选钱包共用同一份 Paper 记录，买入后持有至官方结算",
                   "stake_dollars": BUDGET, "start_at": a["starts_at"], "metrics": metrics, "open_trades": opened,
                   "last_signal": {"reason": a["last_reason"]}, "recent_trades_summary": s,
                   "order_counts": counts, "control_realized_pnl": control[0] if control else None,
                   "excess_vs_control": round(a["realized"] - control[0], 6) if control else None}
        return dict(enabled=enabled, available=True, as_of=time.time(), last_seen=a["last_seen"], start_at=a["starts_at"],
                    curve_basis="realized", candidate_key=KEY, last_reason=a["last_reason"], variant=variant)

    def snapshot(self, trade_limit=None, order_limit=None):
        try:
            with self._read() as conn:
                result = self._snapshot(conn)
                if trade_limit is not None and order_limit is not None:
                    result["recent_trades_page"] = self._trades(conn, trade_limit, 0, None, None)
                    result["recent_orders_page"] = self._orders(conn, order_limit, 0, "all")
                return result
        except (sqlite3.Error, ValueError):
            return {"enabled": bool(getattr(self.bot.settings, "strategy_league_enabled", False)), "available": False,
                    "candidate_key": KEY, "reason": "暂时读不到「拿到官方结算」的候选模拟账本"}

    def growth_inputs(self, start, end):
        with self._read() as conn:
            a = self._account(conn)
            rows = conn.execute("""SELECT round_id, cost AS stake, pnl, closed_at AS settled_at
                FROM league_positions WHERE strategy_id=? AND closed_at>=? AND closed_at<?
                ORDER BY closed_at, round_id""", (KEY,start,end))
            trades = [{**dict(r),"strategy_id":VARIANT_ID} for r in rows]
            opened = conn.execute("""SELECT round_id,stake FROM league_positions WHERE strategy_id=?
                AND closed_at IS NULL AND opened_at<?""", (KEY,end))
            open_positions = [{**dict(r),"strategy_id":VARIANT_ID} for r in opened]
            latest = conn.execute("SELECT max(closed_at) FROM league_positions WHERE strategy_id=? AND closed_at<?",
                                  (KEY,end)).fetchone()[0]
            return {"account":{"strategy_id":VARIANT_ID,"name":NAME,"created_at":a["starts_at"]},
                    "rows":trades,"latest_settlement":latest,"open_positions":open_positions}

    def _trades(self, conn, limit, offset, start_at, end_at):
        if start_at is not None and end_at is not None and start_at > end_at:
            raise ValueError("结束时间不能早于开始时间")
        after, params = "", []
        for op, value in ((">=", start_at), ("<=", end_at)):
            if value is not None:
                after += " AND coalesce(closed_at,opened_at) " + op + " ?"
                params.append(value)
        s = _summary(conn, after, params)
        rows = [_trade(r) for r in conn.execute(POSITION_ROWS + after + """
            ORDER BY coalesce(p.closed_at,p.opened_at) DESC, p.round_id DESC LIMIT ? OFFSET ?""",
                (KEY, *params, limit, offset))]
        return {"recent_trades": rows, "recent_trades_summary": s,
                "recent_trades_meta": _page_meta(s["total_count"], len(rows), limit, offset, start_at=start_at, end_at=end_at)}

    def recent_trades_page(self, limit, offset, start_at, end_at, **scope):
        with self._read() as conn:
            return self._trades(conn, limit, offset, start_at, end_at)

    def _orders(self, conn, limit, offset, status_filter):
        statuses={"all":(), "active":("PENDING",), "filled":("FILLED","PARTIAL"), "canceled":("CANCELLED",),
                  "expired":("EXPIRED",), "rejected":("REJECTED",)}[status_filter]
        where = "strategy_id=?"
        if statuses:
            where += " AND status IN ("+",".join("?" for _ in statuses)+")"
        total = conn.execute("SELECT count(*) FROM league_orders WHERE "+where, (KEY,*statuses)).fetchone()[0]
        rows = []
        for o in conn.execute("""SELECT id,round_id,side,action,limit_price,submitted_at,matched_at,finished_at,status,
              fee,cash,filled_shares,notional_budget,reason,result_reason,json_array_length(fills) AS fill_count
              FROM league_orders WHERE """+where+" ORDER BY submitted_at DESC,id DESC LIMIT ? OFFSET ?", (KEY,*statuses,limit,offset)):
            buy = o["action"] == "BUY"
            notional = o["cash"] - o["fee"] if buy else o["cash"] + o["fee"]
            rows.append({**TAGS, "id":o["id"], "round_id":o["round_id"], "side":o["side"], "action":o["action"],
                         "order_type":"FAK", "status":"CANCELED" if o["status"]=="CANCELLED" else o["status"],
                         "created_at":o["submitted_at"], "matched_at":o["matched_at"], "finished_at":o["finished_at"],
                         "limit_price":o["limit_price"], "requested_cash":BUDGET if buy else None,
                         "notional_budget":o["notional_budget"], "filled_shares":o["filled_shares"], "fee":o["fee"],
                         "avg_fill_price":round(notional/o["filled_shares"], 8) if o["filled_shares"] else None,
                         "cash_spent":o["cash"], "fill_count":o["fill_count"] or 0,
                         "trade_id":o["round_id"] if o["filled_shares"] else None,
                         "reason":o["result_reason"] or o["reason"], "decision_reason":o["reason"]})
        return {"recent_orders":rows, "recent_orders_meta":_page_meta(total,len(rows),limit,offset,status_filter=status_filter)}

    def orders_page(self, limit, offset, status_filter, **scope):
        with self._read() as conn:
            return self._orders(conn, limit, offset, status_filter)

    def order_fills(self, order_id, **scope):
        with self._read() as conn:
            row = conn.execute("SELECT matched_at,fills FROM league_orders WHERE strategy_id=? AND id=?", (KEY,order_id)).fetchone()
            levels = json.loads(row["fills"]) if row and row["fills"] else []
            return {"order_id":order_id, "fills":[{**level,"level_index":i,"order_id":order_id,"created_at":row["matched_at"]}
                                                 for i,level in enumerate(levels,1)]}

    def equity_curve_window(self, days, max_points, *, full_history=False, **scope):
        with self._read() as conn:
            a = self._account(conn)
            initial = self._principal(conn, a)
            now = time.time()
            start = a["starts_at"] if full_history else max(a["starts_at"], now - days*86400)
            prior = conn.execute("""SELECT coalesce(sum(pnl),0) FROM league_pnl_events WHERE strategy_id=? AND at<?""", (KEY,start)).fetchone()[0]
            changes = conn.execute("""SELECT at,sum(pnl) FROM league_pnl_events WHERE strategy_id=? AND at>=? AND at<=?
                                      GROUP BY at ORDER BY at""", (KEY,start,now))
            pnl = round(prior, 6)
            points = [(start,pnl)]
            for at,change in changes:
                pnl = round(pnl+change, 6)
                if at==start:
                    points[0] = (at,pnl)
                else:
                    points.append((at,pnl))
            count = len(points)
            if count > max_points:
                points = [points[round(i*(count-1)/(max_points-1))] for i in range(max_points)]
            rows = [{"created_at":at,"realized_pnl":gain,"total_pnl":gain,"total_equity":round(initial+gain,6)} for at,gain in points]
            return {"equity_curve":rows, "equity_curve_meta":{**TAGS, "label":NAME+" · 已结算净值", "curve_basis":"realized",
                    "initial_balance":initial, "points":len(rows), "source_points":count, "max_points":max_points,
                    "start_at":start,"as_of":now,"days":None if full_history else days,"full_history":full_history}}

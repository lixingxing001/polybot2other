"""Independent ledger for matched entries and auditable partial early exits."""
import json
from pathlib import Path
import time

from .storage import TradeStore, _locked


class MainExitStore(TradeStore):
    def __init__(self, path, initial_balance):
        super().__init__(path, initial_balance)
        with self._lock, self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS exit_run(
                    id INTEGER PRIMARY KEY CHECK(id=1), baseline_db TEXT NOT NULL,
                    started_at REAL NOT NULL, cursor INTEGER NOT NULL, policy_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exit_links(
                    parent_id INTEGER PRIMARY KEY, trade_id INTEGER, round_id TEXT NOT NULL,
                    side TEXT NOT NULL, original_stake REAL NOT NULL, original_shares REAL NOT NULL,
                    parent_opened_at REAL NOT NULL, mirrored_at REAL NOT NULL, skip_reason TEXT,
                    hold_pnl REAL, official_outcome TEXT, resolved_at REAL,
                    consumed_book TEXT, exit_latch TEXT, max_roi REAL, min_roi REAL, observations INTEGER NOT NULL DEFAULT 0,
                    first_observed_at REAL, last_observed_at REAL, max_gap_seconds REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS exit_parts(
                    trade_id INTEGER PRIMARY KEY REFERENCES trades(id),
                    parent_id INTEGER NOT NULL REFERENCES exit_links(parent_id));
                CREATE TABLE IF NOT EXISTS exit_sales(
                    id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES exit_links(parent_id),
                    trade_id INTEGER NOT NULL REFERENCES trades(id), reason TEXT NOT NULL,
                    trigger_at REAL NOT NULL, filled_at REAL NOT NULL, shares REAL NOT NULL,
                    gross REAL NOT NULL, fee REAL NOT NULL, net REAL NOT NULL, basis REAL NOT NULL,
                    pnl REAL NOT NULL, book_source_ms REAL NOT NULL, book_received_ms REAL NOT NULL,
                    limit_price REAL NOT NULL, levels_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS exit_links_round ON exit_links(round_id);
                CREATE INDEX IF NOT EXISTS exit_parts_parent ON exit_parts(parent_id);
                CREATE INDEX IF NOT EXISTS exit_sales_parent ON exit_sales(parent_id);
            """)
        self._curve_cursor = 0
        self._peak = float(self.account()["initial_balance"])
        self._drawdown = 0.0

    @_locked
    def initialize_run(self, baseline_path, cursor, policy, now=None):
        path = str(Path(baseline_path).resolve())
        row = self.conn.execute("SELECT * FROM exit_run WHERE id=1").fetchone()
        if row:
            if row["baseline_db"] != path:
                raise ValueError("Exit account belongs to another main ledger")
            return dict(row)
        with self.conn:
            self.conn.execute("INSERT INTO exit_run VALUES(1,?,?,?,?)",
                              (path, time.time() if now is None else now, cursor, json.dumps(policy.payload())))
        return dict(self.conn.execute("SELECT * FROM exit_run WHERE id=1").fetchone())

    @_locked
    def run(self):
        row = self.conn.execute("SELECT * FROM exit_run WHERE id=1").fetchone()
        return dict(row) if row else None

    @_locked
    def mirror(self, parent, fill, now, skip_reason=None):
        """The copied entry and idempotency marker commit in one transaction."""
        parent_id = int(parent["id"])
        old = self.conn.execute("SELECT * FROM exit_links WHERE parent_id=?", (parent_id,)).fetchone()
        if old:
            return dict(old)
        if fill and self.account()["cash_balance"] + 1e-8 < fill.cash_spent:
            skip_reason = "新账户可用资金不足"
        trade_id = None
        with self.conn:
            if fill is not None and not skip_reason:
                reason = f"主账户止盈止损 · 对应主账户 #{parent_id} · 沿用原入场成本和份额 | {fill.signal.reason}"
                self.conn.execute("UPDATE account SET cash_balance=cash_balance-?,updated_at=? WHERE id=1", (fill.cash_spent, now))
                cur = self.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,
                    confidence,move_bps,status,opened_at,reason) VALUES(?,?,?,?,?,?,?,?,'OPEN',?,?)""",
                    (fill.market.round_id, fill.market.symbol, fill.side, fill.cash_spent, fill.fill_price,
                     fill.shares, fill.signal.confidence, fill.signal.move_bps, parent["opened_at"], reason))
                trade_id = cur.lastrowid
                self._insert_paper_order_for_fill(fill, trade_id, reason, parent["opened_at"])
            self.conn.execute("""INSERT INTO exit_links(parent_id,trade_id,round_id,side,original_stake,
                original_shares,parent_opened_at,mirrored_at,skip_reason) VALUES(?,?,?,?,?,?,?,?,?)""",
                (parent_id, trade_id, parent["round_id"], parent["side"],
                 fill.cash_spent if fill else parent["stake"], fill.shares if fill else parent["shares"],
                 parent["opened_at"], now, skip_reason))
            if trade_id is not None:
                self.conn.execute("INSERT INTO exit_parts VALUES(?,?)", (trade_id, parent_id))
            self.conn.execute("UPDATE exit_run SET cursor=max(cursor,?) WHERE id=1", (parent_id,))
        if trade_id is not None:
            self.record_equity()
        return dict(self.conn.execute("SELECT * FROM exit_links WHERE parent_id=?", (parent_id,)).fetchone())

    @_locked
    def advance_cursor(self, cursor):
        with self.conn:
            self.conn.execute("UPDATE exit_run SET cursor=max(cursor,?) WHERE id=1", (cursor,))

    @_locked
    def watchers(self, round_id=None):
        sql = "SELECT * FROM exit_links WHERE trade_id IS NOT NULL AND resolved_at IS NULL"
        args = ()
        if round_id:
            sql += " AND round_id=?"
            args = (round_id,)
        return [dict(x) for x in self.conn.execute(sql, args)]

    @_locked
    def position(self, parent_id):
        row = self.conn.execute("""SELECT t.*,l.consumed_book,l.exit_latch,m.ends_at,m.up_token,m.down_token
            FROM exit_links l JOIN trades t ON t.id=l.trade_id JOIN market_rounds m USING(round_id)
            WHERE l.parent_id=? AND t.status='OPEN'""", (parent_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def latch_stop(self, parent_id):
        with self.conn:
            self.conn.execute("UPDATE exit_links SET exit_latch='SL' WHERE parent_id=?", (parent_id,))

    @_locked
    def note_observation(self, parent_id, roi, now):
        with self.conn:
            self.conn.execute("""UPDATE exit_links SET max_roi=CASE WHEN max_roi IS NULL THEN ? ELSE max(max_roi,?) END,
                min_roi=CASE WHEN min_roi IS NULL THEN ? ELSE min(min_roi,?) END,
                max_gap_seconds=max(max_gap_seconds,?-coalesce(last_observed_at,parent_opened_at)),
                first_observed_at=coalesce(first_observed_at,?),last_observed_at=?,observations=observations+1
                WHERE parent_id=?""", (roi, roi, roi, roi, now, now, now, parent_id))

    @_locked
    def sell(self, parent_id, sweep, book, intent, now):
        position = self.position(parent_id)
        if not position or now >= position["ends_at"] or position["consumed_book"] == book["fingerprint"]:
            return None
        quantity = sweep["shares"]
        if quantity <= 0 or quantity > position["shares"] + 1e-7:
            return None
        full = quantity >= position["shares"] - 1e-7
        basis = position["stake"] if full else round(position["stake"] * quantity / position["shares"], 6)
        pnl = round(sweep["net"] - basis, 6)
        label = "止盈" if intent["reason"] == "TP" else "止损"
        reason = position["reason"] + f" | {label}提前卖出，卖出费 {sweep['fee']:.6f}，主账户 #{parent_id}"
        avg = round(sweep["avg_price"], 4)
        with self.conn:
            if full:
                closed_id = position["id"]
                self.conn.execute("""UPDATE trades SET status='SETTLED',settled_at=?,exit_price=?,payout=?,pnl=?,
                    reason=?,settlement_source='early_exit' WHERE id=?""", (now, avg, sweep["net"], pnl, reason, closed_id))
            else:
                self.conn.execute("UPDATE trades SET stake=?,shares=? WHERE id=?",
                    (round(position["stake"]-basis, 6), round(position["shares"]-quantity, 6), position["id"]))
                cur = self.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,
                    confidence,move_bps,status,opened_at,settled_at,exit_price,payout,pnl,reason,settlement_source)
                    VALUES(?,?,?,?,?,?,?,?,'SETTLED',?,?,?,?,?,?,'early_exit')""",
                    (position["round_id"], position["symbol"], position["side"], basis, position["entry_price"], quantity,
                     position["confidence"], position["move_bps"], position["opened_at"], now, avg, sweep["net"], pnl, reason))
                closed_id = cur.lastrowid
                self.conn.execute("INSERT INTO exit_parts VALUES(?,?)", (closed_id, parent_id))
            self.conn.execute("UPDATE account SET cash_balance=cash_balance+?,realized_pnl=realized_pnl+?,updated_at=? WHERE id=1",
                              (sweep["net"], pnl, now))
            self.conn.execute("UPDATE exit_links SET consumed_book=? WHERE parent_id=?", (book["fingerprint"], parent_id))
            self.conn.execute("""INSERT INTO exit_sales(parent_id,trade_id,reason,trigger_at,filled_at,shares,gross,fee,
                net,basis,pnl,book_source_ms,book_received_ms,limit_price,levels_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (parent_id, closed_id, intent["reason"], intent["at"], now, quantity, sweep["gross"], sweep["fee"],
                 sweep["net"], basis, pnl, book["source_ms"], book["received_ms"], intent["limit"], json.dumps(sweep["levels"])))
        self.record_equity()
        return {"parent_id": parent_id, "trade_id": closed_id, "reason": intent["reason"], "shares": quantity,
                "net": sweep["net"], "fee": sweep["fee"], "pnl": pnl, "full": full, "filled_at": now}

    @_locked
    def resolve(self, round_id, resolution, now):
        if resolution.get("outcome") not in ("Up", "Down"):
            return
        # Already sold portions remain early_exit; only residual OPEN shares settle.
        self.settle_round_outcome(round_id, resolution["outcome"], now,
            final_price=resolution.get("final_price"), target_price=resolution.get("target_price"),
            settlement_source="polymarket_official")
        with self.conn:
            self.conn.execute("""UPDATE exit_links SET official_outcome=?,resolved_at=?,
                hold_pnl=CASE WHEN side=? THEN original_shares ELSE 0 END-original_stake,
                max_gap_seconds=max(max_gap_seconds,
                    (SELECT ends_at FROM market_rounds WHERE round_id=exit_links.round_id)-coalesce(last_observed_at,parent_opened_at))
                WHERE round_id=? AND trade_id IS NOT NULL AND resolved_at IS NULL""",
                (resolution["outcome"], now, resolution["outcome"], round_id))

    @_locked
    def grouped(self):
        return [dict(x) for x in self.conn.execute("""SELECT l.*,coalesce(sum(t.pnl),0) exit_pnl,
            sum(t.status='OPEN') remaining_parts, max(t.settled_at) closed_at
            FROM exit_links l JOIN exit_parts p ON p.parent_id=l.parent_id JOIN trades t ON t.id=p.trade_id
            GROUP BY l.parent_id ORDER BY l.parent_id""")]

    @_locked
    def comparison(self):
        groups = self.grouped()
        pairs = [x for x in groups if x["resolved_at"] is not None and not x["remaining_parts"]]
        hold = sum(x["hold_pnl"] for x in pairs)
        exited = sum(x["exit_pnl"] for x in pairs)
        fees = self.conn.execute("SELECT coalesce(sum(fee),0) FROM exit_sales").fetchone()[0]
        counts = dict(self.conn.execute("SELECT reason,count(DISTINCT parent_id) FROM exit_sales GROUP BY reason").fetchall())
        recent = []
        for row in reversed(groups[-20:]):
            recent.append({**row, "difference": round(row["exit_pnl"]-row["hold_pnl"],6) if row["hold_pnl"] is not None and not row["remaining_parts"] else None})
        return {"matched_entries": len(groups), "paired_settled": len(pairs),
                "pending_pairs": len(groups)-len(pairs), "hold_pnl": round(hold,6), "exit_pnl": round(exited,6),
                "difference": round(exited-hold,6), "exit_fees": round(fees,6),
                "entry_fees": self.paper_order_summary()["fee"], "take_profit_trades": counts.get("TP",0),
                "stop_loss_trades": counts.get("SL",0),
                "saved_losses": sum(x["hold_pnl"]<0 and x["exit_pnl"]>0 for x in pairs),
                "winners_stopped_out": sum(x["hold_pnl"]>0 and x["exit_pnl"]<0 for x in pairs),
                "benefit_on_losers": round(sum(x["exit_pnl"]-x["hold_pnl"] for x in pairs if x["hold_pnl"]<0),6),
                "profit_given_up_on_winners": round(sum(x["hold_pnl"]-x["exit_pnl"] for x in pairs if x["hold_pnl"]>0),6),
                "skipped_entries": self.conn.execute("SELECT count(*) FROM exit_links WHERE trade_id IS NULL").fetchone()[0],
                "recent_pairs": recent,
                "recent_sales": [dict(x) for x in self.conn.execute("SELECT id,parent_id,reason,filled_at,shares,net,fee,pnl FROM exit_sales ORDER BY id DESC LIMIT 10")]}

    @_locked
    def metrics(self):
        result = super().metrics()
        # Trade counts use original entries, so partial exits do not inflate win rate.
        groups = self.grouped()
        closed = [g for g in groups if not g["remaining_parts"]]
        wins = sum(g["exit_pnl"] > 0 for g in closed)
        for row in self.conn.execute("SELECT id,total_equity FROM equity_curve WHERE id>? ORDER BY id", (self._curve_cursor,)):
            self._curve_cursor = row["id"]
            self._peak = max(self._peak, row["total_equity"])
            self._drawdown = max(self._drawdown, self._peak-row["total_equity"])
        result.update(settled_trades=len(closed), wins=wins, losses=sum(g["exit_pnl"]<0 for g in closed),
                      win_rate=round(wins/len(closed)*100,4) if closed else 0, max_drawdown=round(self._drawdown,6))
        return result

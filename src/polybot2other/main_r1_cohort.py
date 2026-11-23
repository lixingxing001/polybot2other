"""Prospective common-opportunity accounting for the MAIN-R1 selector.

All four isolated shadow wallets must have observed the same complete market
window. Zero-entry rounds are observations too. Missing coverage and unresolved
orders remain visible, and never turn into invented zero-profit training rows.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import math
import time

from .main_r1_execution import CANDIDATES
from .optimized_reference import SPOT


def make_history_reader(references):
    """Only ticks actually received by the decision cutoff; no backfill snapshots."""
    def read(start_ms, end_ms):
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start_ms, end_ms)) or start_ms > end_ms:
            raise ValueError("Invalid reference history interval")
        with references.lock:
            rows = references.conn.execute("""
                SELECT source_ms,received_ms,price FROM reference_ticks
                WHERE channel=? AND snapshot=0 AND source_ms BETWEEN ? AND ?
                    AND received_ms<=? ORDER BY source_ms,received_ms
            """, (SPOT, start_ms, end_ms, end_ms)).fetchall()
        return [{"source_ms": row[0], "received_ms": row[1], "price": row[2]} for row in rows]
    return read


class CohortCollector:
    def __init__(self, store, shadows, selector, *, clock=time.time, start_at,
                 min_time_left_seconds=20, max_time_left_seconds=285, tick_seconds=2):
        if set(shadows) != set(CANDIDATES):
            raise ValueError("A cohort needs all four declared shadow candidates")
        if not 0 <= min_time_left_seconds < max_time_left_seconds <= 300 or tick_seconds <= 0:
            raise ValueError("Invalid common observation window")
        self.store, self.shadows, self.selector = store, shadows, selector
        self.clock, self.start_at = clock, float(start_at)
        self.min_left, self.max_left = min_time_left_seconds, max_time_left_seconds
        self.grace = max(2., 2*float(tick_seconds))
        self.max_gap = max(10., 3*float(tick_seconds))
        with store._lock, store.conn:
            store.conn.execute("""
                CREATE TABLE IF NOT EXISTS main_r1_cohort_coverage(
                    round_id TEXT PRIMARY KEY, started_at REAL NOT NULL, ends_at REAL NOT NULL,
                    market_json TEXT NOT NULL, regime TEXT, feature_time REAL NOT NULL,
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL, max_gap REAL NOT NULL,
                    samples INTEGER NOT NULL, failed_cycles INTEGER NOT NULL,
                    outcome TEXT, resolution_seen_at REAL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, cohort_json TEXT)
            """)

    def observe_cycle(self, frame, per_arm_success):
        market = frame.market
        now = self.clock()
        if market is None or market.started_at < self.start_at or not market.started_at <= now < market.ends_at:
            return
        choice = self.selector.choose(frame)
        if choice.get("round_id") != market.round_id:
            raise ValueError("Selector decision does not belong to the observed round")
        complete = set(per_arm_success) == set(CANDIDATES) and all(per_arm_success.values())
        complete = complete and 0 <= now-frame.observed_at <= self.max_gap
        open_at, close_at = market.ends_at-self.max_left, market.ends_at-self.min_left
        in_window = open_at <= now <= close_at
        with self.store._lock, self.store.conn:
            db = self.store.conn
            saved = db.execute("SELECT * FROM main_r1_cohort_coverage WHERE round_id=?", (market.round_id,)).fetchone()
            if saved is None:
                db.execute("INSERT INTO main_r1_cohort_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (market.round_id, market.started_at, market.ends_at,
                     json.dumps(asdict(market), sort_keys=True, allow_nan=False), choice["regime"],
                     (choice.get("features") or {}).get("feature_time", frame.observed_at), now, now, 0., 1,
                     int(in_window and not complete), None, None, "OBSERVING", "记录共同机会", None))
                return
            if saved["status"] in ("LEARNED", "EXCLUDED"):
                return
            if now < saved["last_seen"]:
                raise ValueError("Cohort observation clock moved backwards")
            gap = max(0., min(now, close_at)-max(saved["last_seen"], open_at))
            db.execute("""UPDATE main_r1_cohort_coverage SET last_seen=?,max_gap=?,samples=samples+1,
                failed_cycles=failed_cycles+? WHERE round_id=?""",
                (now, max(saved["max_gap"], gap), int(in_window and not complete), market.round_id))

    def due_rounds(self, now=None):
        at = self.clock() if now is None else now
        with self.store._lock:
            return [r[0] for r in self.store.conn.execute("SELECT round_id FROM main_r1_cohort_coverage WHERE ends_at<=? AND outcome IS NULL ORDER BY ends_at", (at,))]

    def record_resolution(self, round_id, result):
        now = self.clock()
        if (not isinstance(result, dict) or result.get("market_slug") != round_id
                or result.get("price_source") != "Gamma:outcomePrices" or result.get("outcome") not in ("Up", "Down")):
            return False
        with self.store._lock, self.store.conn:
            saved = self.store.conn.execute("SELECT * FROM main_r1_cohort_coverage WHERE round_id=?", (round_id,)).fetchone()
            if not saved or saved["ends_at"] > now:
                return False
            if saved["outcome"] is not None:
                if saved["outcome"] != result["outcome"]:
                    raise ValueError("Conflicting official resolution; refuse to relabel history")
                return True
            self.store.conn.execute("UPDATE main_r1_cohort_coverage SET outcome=?,resolution_seen_at=? WHERE round_id=?",
                                    (result["outcome"], now, round_id))
        return True

    def _coverage_error(self, row):
        if row["regime"] is None:
            return "开仓前缺少有效行情分类，保留记录但不作为训练样本"
        if row["failed_cycles"]:
            return "候选观测存在失败，不能当作完整共同机会"
        if row["first_seen"] > row["ends_at"]-self.max_left+self.grace:
            return "未覆盖完整入场窗口起点"
        if row["last_seen"] < row["ends_at"]-self.min_left-self.grace:
            return "未覆盖完整入场窗口终点"
        if row["max_gap"] > self.max_gap:
            return "共同机会观测存在过长中断"
        return None

    def _arm(self, engine, row):
        with engine.lock, engine.store._lock:
            db = engine.store.conn
            orders = [dict(r) for r in db.execute("SELECT * FROM paper_orders WHERE round_id=?", (row["round_id"],))]
            trades = [dict(r) for r in db.execute("SELECT * FROM trades WHERE round_id=?", (row["round_id"],))]
            phases = [r[0] for r in db.execute("SELECT s.phase FROM paper_sim_orders s JOIN paper_orders o ON o.id=s.order_id WHERE o.round_id=?", (row["round_id"],))]
        if any(o["status"] == "PENDING" for o in orders) or any(p in ("UNKNOWN", "IN_FLIGHT", "CONFIRMING") for p in phases):
            return None
        if any(o["status"] not in ("FILLED", "PARTIAL", "CANCELED") for o in orders):
            return None
        if not trades:
            if any(o["filled_shares"] or o["cash_spent"] for o in orders):
                return None
            return {"status": "NO_TRADE", "requested_budget": 0., "cost": 0., "shares": 0., "pnl": 0.}
        if len(trades) != 1 or any(t["status"] != "SETTLED" or t["settlement_source"] != "polymarket_official" for t in trades):
            return None
        trade = trades[0]
        filled = [o for o in orders if o["filled_shares"] > 0]
        if len(filled) != 1 or filled[0]["trade_id"] != trade["id"]:
            return None
        order = filled[0]
        expected_payout = trade["shares"] if trade["side"] == row["outcome"] else 0.
        if (abs(float(trade["payout"])-expected_payout) > .00001
                or abs(float(trade["pnl"])-(expected_payout-float(trade["stake"]))) > .00001
                or abs(order["cash_spent"]-trade["stake"]) > .00001
                or abs(order["filled_shares"]-trade["shares"]) > .00001):
            return None
        return {"status": "SETTLED", "requested_budget": float(order["requested_cash"]),
                "cost": float(trade["stake"]), "shares": float(trade["shares"]), "pnl": float(trade["pnl"])}

    def flush_completed(self):
        with self.store._lock:
            rows = [dict(r) for r in self.store.conn.execute("SELECT * FROM main_r1_cohort_coverage WHERE ends_at<? AND outcome IS NOT NULL AND status NOT IN ('LEARNED','EXCLUDED') ORDER BY ends_at", (self.clock(),))]
        learned = 0
        for row in rows:
            error = self._coverage_error(row)
            if error:
                self._status(row["round_id"], "EXCLUDED", error)
                continue
            arms = {key: self._arm(engine, row) for key, engine in self.shadows.items()}
            if any(value is None for value in arms.values()):
                self._status(row["round_id"], "WAITING_ORDERS", "等待四个候选全部确认并完成官方结算")
                continue
            cohort = {"round_id": row["round_id"], "started_at": row["started_at"], "ends_at": row["ends_at"],
                      "regime": row["regime"], "feature_time": row["feature_time"],
                      "coverage_complete": True, "arms": arms}
            # No input may supply an earlier available_at. The selector records
            # the actual time this complete outcome first became knowable.
            self.selector.observe(cohort)
            with self.store._lock, self.store.conn:
                self.store.conn.execute("UPDATE main_r1_cohort_coverage SET status='LEARNED',reason=?,cohort_json=? WHERE round_id=?",
                    ("完整共同机会已写入选择器", json.dumps(cohort, sort_keys=True, allow_nan=False), row["round_id"]))
            learned += 1
        return learned

    def _status(self, round_id, status, reason):
        with self.store._lock, self.store.conn:
            self.store.conn.execute("UPDATE main_r1_cohort_coverage SET status=?,reason=? WHERE round_id=?", (status, reason, round_id))

    def snapshot(self):
        with self.store._lock:
            counts = dict(self.store.conn.execute("SELECT status,count(*) FROM main_r1_cohort_coverage GROUP BY status").fetchall())
            recent = [dict(r) for r in self.store.conn.execute("SELECT round_id,regime,first_seen,last_seen,max_gap,samples,failed_cycles,status,reason FROM main_r1_cohort_coverage ORDER BY started_at DESC LIMIT 20")]
        return {"counts": counts, "recent": recent, "common_window": {"min_time_left_seconds": self.min_left,
                "max_time_left_seconds": self.max_left, "grace_seconds": self.grace, "max_gap_seconds": self.max_gap}}

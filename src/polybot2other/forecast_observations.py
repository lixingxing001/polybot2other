"""Forward-only forecasts in the independent Paper ledger. Never submits orders."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import time

CHECKPOINTS = (180, 60, 20)
TOLERANCE_SECONDS = 5


def book_direction(frame):
    if any(frame.quote_error(side) for side in ("Up", "Down")):
        return None
    up = frame.quotes["Up"]
    down = frame.quotes["Down"]
    difference = up["best_bid"] + up["best_ask"] - down["best_bid"] - down["best_ask"]
    if abs(difference) < 1e-8:
        return None
    return "Up" if difference > 0 else "Down"


class ForecastObservations:
    def __init__(self, store, *, checkpoints=CHECKPOINTS):
        self.store = store
        self.checkpoints = tuple(checkpoints)
        self.current = None
        self.last_round = None
        self.notes_round = None
        self.notes_seen = set()
        self.summary = None
        self.exits = None
        self.entry_reasons = None
        self.summary_at = 0.
        self.summary_lock = threading.Lock()
        with store._lock, store.conn:
            store.conn.execute("""CREATE TABLE IF NOT EXISTS forecast_checkpoints (
                round_id TEXT NOT NULL, seconds_left INTEGER NOT NULL,
                due_at REAL NOT NULL, observed_at REAL, direction TEXT,
                up_score REAL, book_direction TEXT, PRIMARY KEY (round_id,seconds_left))""")
            store.conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_checkpoints_due ON forecast_checkpoints(due_at)")
            store.conn.execute("""CREATE TABLE IF NOT EXISTS forecast_entry_notes (
                round_id TEXT NOT NULL, ends_at REAL NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY (round_id,reason))""")
            store.conn.execute("CREATE INDEX IF NOT EXISTS forecast_notes_end ON forecast_entry_notes(ends_at)")

    def observe_entry_result(self, frame, reason):
        market = frame.market
        if not market or not min(self.checkpoints) <= market.ends_at - frame.observed_at <= max(self.checkpoints):
            return
        if market.round_id != self.notes_round:
            self.notes_round = market.round_id
            self.notes_seen.clear()
        if reason and reason not in self.notes_seen:
            with self.store._lock, self.store.conn:
                self.store.conn.execute("INSERT OR IGNORE INTO forecast_entry_notes VALUES(?,?,?)",
                                        (market.round_id, market.ends_at, reason))
            self.notes_seen.add(reason)

    def observe(self, frame, features):
        market = frame.market
        if market is None:
            self.current = None
            return
        now = frame.observed_at
        left = market.ends_at - now
        if not market.started_at <= now < market.ends_at:
            self.current = None
            return
        direction = features.get("forecast_direction")
        score = features.get("up_score")
        benchmark = book_direction(frame)
        self.current = {
            "round_id": market.round_id, "as_of": now, "ends_at": market.ends_at,
            "direction": direction, "up_score": score, "book_direction": benchmark,
            "action": features.get("action"), "reason": features.get("reason") or frame.error,
            "remaining_average_to_tie": features.get("remaining_average_to_tie"),
        }
        action = features.get("action")
        if action == "BUY":
            self.observe_entry_result(frame, "出现过买入信号")
        elif action == "WAIT" or not features:
            self.observe_entry_result(frame, features.get("reason") or "判断所需行情未就绪")
        checkpoint = next((t for t in self.checkpoints if t <= left <= t + TOLERANCE_SECONDS), None)
        if self.last_round == market.round_id and checkpoint is None:
            return
        with self.store._lock, self.store.conn:
            if self.last_round != market.round_id:
                self.store.conn.executemany("""INSERT OR IGNORE INTO forecast_checkpoints
                    (round_id,seconds_left,due_at) VALUES(?,?,?)""", [
                        (market.round_id, t, market.ends_at-t) for t in self.checkpoints])
            if checkpoint is not None:
                # Last actual observation at or before the target. Delayed frames never fill a past slot.
                self.store.conn.execute("""UPDATE forecast_checkpoints
                    SET observed_at=?, direction=?, up_score=?, book_direction=?
                    WHERE round_id=? AND seconds_left=? AND (observed_at IS NULL OR observed_at<?)""",
                    (now, direction, score, benchmark, market.round_id, checkpoint, now))
        self.last_round = market.round_id

    def snapshot(self):
        now = time.time()
        with self.summary_lock:
            if self.summary is None or now-self.summary_at >= 10:
                path = Path(self.store.db_path).resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(path, uri=True, timeout=1)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute("BEGIN")
                    rows = conn.execute("""SELECT c.seconds_left, count(*) AS tracked,
                        count(c.direction) AS calls, count(r.outcome) AS resolved,
                        count(CASE WHEN r.outcome IS NOT NULL THEN c.direction END) AS graded,
                        sum(CASE WHEN c.direction=r.outcome THEN 1 ELSE 0 END) AS correct,
                        count(CASE WHEN c.direction IS NOT NULL AND c.book_direction IS NOT NULL
                               THEN r.outcome END) AS comparable,
                        sum(CASE WHEN c.book_direction IS NOT NULL AND c.direction=r.outcome THEN 1 ELSE 0 END) AS paired_correct,
                        sum(CASE WHEN c.direction IS NOT NULL AND c.book_direction=r.outcome THEN 1 ELSE 0 END) AS book_correct
                        FROM forecast_checkpoints c LEFT JOIN market_rounds r ON r.round_id=c.round_id
                        AND r.settlement_source='polymarket_official' AND r.outcome IN ('Up','Down')
                        WHERE c.due_at<=? GROUP BY c.seconds_left ORDER BY c.seconds_left DESC""", (now,))
                    self.summary = [dict(row) for row in rows]
                    self.exits = dict(conn.execute("""SELECT count(*) AS exits,
                        count(r.outcome) AS graded, round(coalesce(sum(CASE WHEN r.outcome IS NOT NULL
                        THEN t.payout - CASE WHEN t.side=r.outcome THEN t.shares ELSE 0 END END),0),6) AS relative_pnl
                        FROM trades t LEFT JOIN market_rounds r ON r.round_id=t.round_id
                            AND r.settlement_source='polymarket_official' AND r.outcome IN ('Up','Down')
                        WHERE t.status='SETTLED' AND t.settlement_source='early_exit'""").fetchone())
                    self.entry_reasons = recent_entry_reasons(conn, now)
                    self.summary_at = now
                finally:
                    conn.close()
            return {"available": True, "as_of": now, "current": self.current, "checkpoints": self.summary,
                    "early_exits": self.exits, "entry_reasons": self.entry_reasons}


def recent_entry_reasons(conn, now):
    bounds = (now - 86400, now)
    totals = conn.execute("""SELECT count(*) AS rounds, count(*) FILTER (WHERE NOT opened) AS no_entry
        FROM (SELECT DISTINCT n.round_id, EXISTS(SELECT 1 FROM trades t WHERE t.round_id=n.round_id) AS opened
              FROM forecast_entry_notes n WHERE n.ends_at>? AND n.ends_at<=?)""", bounds).fetchone()
    reasons = conn.execute("""SELECT n.reason, count(*) AS rounds FROM forecast_entry_notes n
        WHERE n.ends_at>? AND n.ends_at<=? AND n.reason!='出现过买入信号'
          AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.round_id=n.round_id)
        GROUP BY n.reason ORDER BY rounds DESC,n.reason LIMIT 5""", bounds)
    return {**dict(totals), "period_hours": 24, "reasons": [dict(row) for row in reasons]}


def forecast_snapshot(bot, strategy_id="FORECAST_PAPER"):
    from .experiments import FORECAST_PAPER_NAMES
    if strategy_id not in FORECAST_PAPER_NAMES:
        return {"available": False}
    runner = getattr(bot, "strategy_experiments", None)
    if runner is not None:
        with runner._lock:
            strategy = runner._bots.get(strategy_id)
        execution = getattr(strategy, "paper_execution", None)
        tracker = getattr(execution, "forecast_observations", None)
        if tracker is not None:
            result = tracker.snapshot()
            if strategy_id in {"FORECAST_EARLY_PAPER", "FORECAST_LIMIT_060_PAPER", "FORECAST_LIMIT_CONTROL_PAPER"}:
                trace = getattr(execution, "entry_trace", None)
                result = {**result, "entry_trace": trace.summary() if trace is not None else {"available": False}}
            return result
    return {"available": False}

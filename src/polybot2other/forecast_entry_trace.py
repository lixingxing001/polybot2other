"""Prospective, read-only observations of the early Paper strategy's entry decisions."""
from collections import OrderedDict
from contextlib import closing
from itertools import islice
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .forecast_observations import TOLERANCE_SECONDS
from .forecast_paper import EARLY_MAX_ENTRY_SECONDS, MIN_ENTRY_SECONDS
from .managed_live_client import fee_amount

STAGES = ("window_seen", "first_direction", "first_priced_direction", "first_buy", "pre_submit_reject",
          "arrival_fill", "arrival_empty")
COVERAGE_GAP = "observation_gap"


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def ask_depth(book, limit=None):
    """Visible shares at the best ask and within a buy limit, before any simulation haircut."""
    best = _number(book.get("best_ask"))
    levels = [(row.get("price"), row.get("size")) for row in book.get("asks") or ()
              if isinstance(row, dict) and _number(row.get("price")) is not None
              and 0 < row["price"] < 1 and _number(row.get("size")) is not None and row["size"] > 0]
    if best is None or not levels or abs(min(price for price, _ in levels) - best) > 1e-6:
        return None, None
    at_best = sum(size for price, size in levels if abs(price - best) <= 1e-6)
    in_limit = sum(size for price, size in levels if price <= limit + 1e-9) if _number(limit) is not None else None
    return at_best, in_limit


def _decision_details(frame, features, info):
    side = features["forecast_direction"]
    book = frame.quotes.get(side) or {}
    score = _number(features.get("score"))
    result = {
        "snapshot_id": frame.snapshot_id, "side": side, "score": score, "reason": features.get("reason"),
        "seconds_left": frame.market.ends_at - frame.observed_at, "action": features.get("action"),
        "book_source_ms": _number(book.get("source_ms")), "book_received_ms": _number(book.get("received_ms")),
        "quote_error": frame.quote_error(side), "bid": None, "ask": None,
        "ask_size_at_best": None, "ask_size_in_limit": None, "ask_all_in_cost": None, "ask_edge": None,
        "limit_price": None, "all_in_cost": None, "edge": None,
    }
    if result["quote_error"]:
        return result
    ask = book["best_ask"]
    limit = _number(features.get("limit"))
    at_best, in_limit = ask_depth(book, limit)
    result.update(bid=book["best_bid"], ask=ask, ask_size_at_best=at_best, ask_size_in_limit=in_limit)
    if at_best is None:
        result["quote_error"] = "卖一价与卖盘深度不一致"
        return result
    rate, exponent = _number(info.get("fee_rate")), _number(info.get("fee_exponent"))
    if rate is None or exponent is None or not 0 <= rate <= 1 or not 1 <= exponent <= 4:
        return result
    ask_cost = ask + fee_amount(1, ask, rate, exponent)
    result.update(ask_all_in_cost=ask_cost, ask_edge=None if score is None else score - ask_cost)
    # Only the decision knows whether its ceiling, spread and tick checks passed.
    # A high-priced ask may have an observable cost but no eligible strategy limit.
    cost = _number(features.get("all_in_cost"))
    if limit is not None and cost is not None:
        result.update(limit_price=limit, all_in_cost=cost, edge=None if score is None else score - cost)
    return result


class ForecastEntryTrace:
    def __init__(self, store, *, max_entry_seconds=EARLY_MAX_ENTRY_SECONDS):
        self.max_entry_seconds = max_entry_seconds
        self.path = Path(store.db_path).resolve()
        self._lock = threading.RLock()
        self._pending = OrderedDict()
        self._saved = OrderedDict()
        self._schema_ready = False
        self._retry_at = 0.
        self._last_window = None
        self._initial_window = None
        self._cache = None
        self._cache_at = 0.
        self._dropped = 0

    def _queue(self, stage, round_id, at, details):
        key = (round_id, stage)
        if key in self._pending or key in self._saved:
            return
        try:
            payload = json.dumps(details, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            self._dropped += 1
            return
        if len(self._pending) >= 2048:
            self._pending.popitem(last=False)
            self._dropped += 1
        self._pending[key] = (round_id, stage, at, payload)

    def _flush(self):
        if not self._pending or time.monotonic() < self._retry_at:
            return
        batch = list(islice(self._pending.values(), 32))
        try:
            # Use our own zero-wait connection; a busy ledger can retry after trading proceeds.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0)) as conn, conn:
                if not self._schema_ready:
                    conn.execute("""CREATE TABLE IF NOT EXISTS forecast_entry_trace (
                        round_id TEXT NOT NULL, stage TEXT NOT NULL, observed_at REAL NOT NULL, details TEXT NOT NULL,
                        PRIMARY KEY (round_id,stage))""")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_trace_stage_at ON forecast_entry_trace(stage,observed_at)")
                for round_id, stage, at, details in batch:
                    row = conn.execute("INSERT OR IGNORE INTO forecast_entry_trace VALUES(?,?,?,?)",
                                       (round_id, stage, at, details))
                    if not row.rowcount and stage == "window_seen" and round_id == self._initial_window:
                        resume = json.dumps({"reason": "服务重启后续接，期间采样覆盖未知"}, ensure_ascii=False)
                        conn.execute("INSERT OR IGNORE INTO forecast_entry_trace VALUES(?,?,?,?)",
                                     (round_id, COVERAGE_GAP, at, resume))
        except sqlite3.Error:
            self._schema_ready = False
            self._retry_at = time.monotonic() + 1
            return
        self._schema_ready = True
        self._cache = None
        for round_id, stage, _, _ in batch:
            key = round_id, stage
            del self._pending[key]
            self._saved[key] = True
        while len(self._saved) > 512:
            self._saved.popitem(last=False)

    def observe(self, frame, features, info):
        market, at = frame.market, frame.observed_at
        if not market or _number(at) is None:
            return
        left = market.ends_at - at
        with self._lock:
            last = self._last_window
            if last:
                previous_id, previous_at, window_end = last
                until = min(window_end, at)
                if until - previous_at > TOLERANCE_SECONDS:
                    self._queue(COVERAGE_GAP, previous_id, until, {
                        "from_at": previous_at, "to_at": until, "seconds": until - previous_at,
                        "reason": "观察间隔超过 5 秒",
                    })
                if at >= window_end:
                    self._last_window = None
            if MIN_ENTRY_SECONDS <= left <= self.max_entry_seconds:
                key = market.round_id
                self._initial_window = self._initial_window or key
                self._last_window = (key, at, market.ends_at - MIN_ENTRY_SECONDS)
                self._queue("window_seen", key, at, {"ends_at": market.ends_at, "seconds_left": left,
                            "late_start": self.max_entry_seconds - left > TOLERANCE_SECONDS})
                action = features.get("action")
                if not frame.error and action in ("WAIT", "BUY") and features.get("forecast_direction") in ("Up", "Down"):
                    stages = ["first_direction", "first_priced_direction"]
                    if action == "BUY":
                        stages.append("first_buy")
                    needed = [s for s in stages if (key, s) not in self._pending and (key, s) not in self._saved]
                    if needed:
                        details = _decision_details(frame, features, info or {})
                        for stage in needed:
                            if stage != "first_priced_direction" or details["ask_all_in_cost"] is not None:
                                self._queue(stage, key, at, details)
            self._flush()

    def record(self, stage, round_id, observed_at, details):
        if stage not in ("pre_submit_reject", "arrival_fill", "arrival_empty") or not round_id or _number(observed_at) is None:
            return
        with self._lock:
            self._queue(stage, round_id, observed_at, details)
            self._flush()

    def summary(self):
        with self._lock:
            if self._cache is not None and time.monotonic() - self._cache_at < 5:
                return self._cache
        result = {"available": True, "started_at": None, "counts": dict.fromkeys(STAGES, 0), "rows": []}
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "forecast_entry_trace" in tables:
                    self._read_summary(conn, tables, result)
        except (sqlite3.Error, ValueError):
            result = {**result, "available": False, "reason": "诊断记录暂时无法读取"}
        with self._lock:
            result["dropped_events"] = self._dropped
            self._cache, self._cache_at = result, time.monotonic()
        return result

    def _read_summary(self, conn, tables, result):
        for stage, count, first in conn.execute("""SELECT t.stage,count(*),min(t.observed_at)
            FROM forecast_entry_trace t JOIN forecast_entry_trace w ON t.round_id=w.round_id AND w.stage='window_seen'
            GROUP BY t.stage"""):
            if stage in STAGES:
                result["counts"][stage] = count
            if stage == "window_seen":
                result["started_at"] = first
        newest = conn.execute("""SELECT round_id FROM forecast_entry_trace WHERE stage='window_seen'
            ORDER BY observed_at DESC LIMIT 8""").fetchall()
        for (key,) in newest:
            item = dict.fromkeys((*STAGES, COVERAGE_GAP, "first_order"))
            item["round_id"] = key
            for stage, at, raw in conn.execute("SELECT stage,observed_at,details FROM forecast_entry_trace WHERE round_id=?", (key,)):
                item[stage] = {**json.loads(raw), "at": at}
            item["ends_at"] = item["window_seen"]["ends_at"]
            if {"paper_orders", "paper_sim_orders"} <= tables:
                order = conn.execute("""SELECT o.id AS order_id, o.created_at AS at, o.side, o.limit_price, s.phase
                    FROM paper_orders o JOIN paper_sim_orders s ON s.order_id=o.id
                    WHERE o.round_id=? AND o.order_type='FAK' AND o.created_at>=?
                    ORDER BY o.created_at,o.id LIMIT 1""", (key, item["window_seen"]["at"])).fetchone()
                if order:
                    item["first_order"] = dict(order)
            result["rows"].append(item)

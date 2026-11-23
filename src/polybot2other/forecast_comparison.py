"""Compare confirmed Paper fills in completed BTC five-minute rounds observed by both accounts."""
from __future__ import annotations

from contextlib import closing
from functools import lru_cache
import math
from pathlib import Path
import sqlite3
import time

from .forecast_observations import TOLERANCE_SECONDS

BASE = "FORECAST_PAPER"
EARLY = "FORECAST_EARLY_PAPER"
ROUND_SECONDS = 300


def _ledger(conn, schema, start, end, required):
    # The checkpoint, order and trade time indexes restrict each query to the
    # comparison period. Neither raw market ticks nor order payloads are needed.
    ids = ",".join("?" for _ in required)
    covered = {
        row[0] for row in conn.execute(f"""
            SELECT m.round_id FROM {schema}.forecast_checkpoints c
            JOIN {schema}.market_rounds m ON m.round_id=c.round_id
            WHERE c.due_at>=? AND c.due_at<? AND c.seconds_left IN ({ids})
              AND m.started_at>=? AND m.ends_at<=? AND m.ends_at-m.started_at=?
              AND c.due_at=m.ends_at-c.seconds_left
              AND c.observed_at BETWEEN c.due_at-? AND c.due_at
            GROUP BY m.round_id HAVING count(*)=?
        """, (start, end, *required, start, end, ROUND_SECONDS,
              TOLERANCE_SECONDS, len(required)))
    }
    trades = {row["round_id"]: row for row in conn.execute(f"""
        SELECT t.round_id, m.ends_at-min(t.opened_at) AS remaining_at_fill,
               sum(CASE WHEN t.status='SETTLED' THEN t.pnl ELSE 0 END) AS pnl,
               max(t.status='OPEN') AS open_position
        FROM {schema}.trades t JOIN {schema}.market_rounds m ON m.round_id=t.round_id
        WHERE t.status IN ('OPEN','SETTLED') AND t.opened_at>=? AND t.opened_at<?
          AND m.started_at>=? AND m.ends_at<=?
        GROUP BY t.round_id
    """, (start, end, start, end))}
    orders = {row["round_id"]: row for row in conn.execute(f"""
        SELECT o.round_id, max(o.order_type='FAK') AS buy_submitted,
               max(s.phase IN ('IN_FLIGHT','CONFIRMING')) AS pending,
               max(o.order_type='FAK' AND s.phase IN ('IN_FLIGHT','CONFIRMING')) AS buy_pending,
               max(o.order_type='FAK' AND s.phase IN ('PARTIAL','FILLED')
                   AND m.ends_at-o.created_at>180 AND m.ends_at-o.created_at<=240) AS early_submitted_fill
        FROM {schema}.paper_orders o JOIN {schema}.paper_sim_orders s ON s.order_id=o.id
        JOIN {schema}.market_rounds m ON m.round_id=o.round_id
        WHERE o.symbol='BTC' AND o.created_at>=? AND o.created_at<?
        GROUP BY o.round_id
    """, (start, end))}
    return covered, trades, orders


def _stats(trades, orders, rounds):
    opened = rounds.intersection(trades)
    submitted = {key for key in rounds.intersection(orders) if orders[key]["buy_submitted"]}
    pending = {key for key in rounds.intersection(orders) if orders[key]["pending"]}
    pending_buys = {key for key in submitted if orders[key]["buy_pending"]}
    unsettled = {key for key in opened if trades[key]["open_position"]}
    excluded = trades.keys() - rounds
    return {
        "opened": len(opened),
        "open_rate": round(100 * len(opened) / len(rounds), 2) if rounds else None,
        "submitted": len(submitted), "unfilled": len(submitted - opened - pending_buys),
        "skipped": len(rounds - submitted - opened), "unresolved": len(unsettled | pending),
        "realized_pnl": round(sum(trades[key]["pnl"] for key in opened), 6),
        "excluded_opened": len(excluded),
        "excluded_realized_pnl": round(sum(trades[key]["pnl"] for key in excluded), 6),
    }


def _summarize(conn, now):
    # Pin both read snapshots before querying orders and trades.
    runs = conn.execute("""SELECT b.started_at, e.started_at FROM main.paper_sim_run b
                           JOIN early.paper_sim_run e ON e.id=1 WHERE b.id=1""").fetchone()
    if not runs or any(value is None for value in runs):
        return {"available": False, "reason": "等待两个 Paper 账户启动"}
    start = math.ceil(max(runs) / ROUND_SECONDS) * ROUND_SECONDS
    end = math.floor(now / ROUND_SECONDS) * ROUND_SECONDS
    base_coverage, base_trades, base_orders = _ledger(conn, "main", start, end, (180, 60, 20))
    early_coverage, early_trades, early_orders = _ledger(conn, "early", start, end, (240, 180, 60, 20))
    rounds = base_coverage & early_coverage
    expected = max(0, (end-start) // ROUND_SECONDS)
    current_slug = f"btc-updown-5m-{end}"
    active = end >= start and conn.execute("""
        SELECT EXISTS(SELECT 1 FROM main.forecast_checkpoints WHERE round_id=?)
            OR EXISTS(SELECT 1 FROM early.forecast_checkpoints WHERE round_id=?)
    """, (current_slug, current_slug)).fetchone()[0]

    base = _stats(base_trades, base_orders, rounds)
    early = _stats(early_trades, early_orders, rounds)
    base_bought = rounds.intersection(base_trades)
    early_bought = rounds.intersection(early_trades)
    early_only = early_bought - base_bought
    base_only = base_bought - early_bought
    early["early_entries"] = sum(
        180 < early_trades[key]["remaining_at_fill"] <= 240
        for key in early_bought
    )
    early["early_submitted_and_filled"] = sum(
        early_orders[key]["early_submitted_fill"] for key in early_bought.intersection(early_orders)
    )
    return {
        "available": True, "as_of": now, "start_at": start, "rounds": len(rounds),
        "incomplete_rounds": expected-len(rounds), "active_rounds": int(active),
        "base": base, "early": early,
        "overlap": {
            "both": len(base_bought & early_bought), "base_only": len(base_only),
            "early_only": len(early_only), "neither": len(rounds - (base_bought | early_bought)),
            "base_only_realized_pnl": round(sum(base_trades[key]["pnl"] for key in base_only), 6),
            "early_only_realized_pnl": round(sum(early_trades[key]["pnl"] for key in early_only), 6),
        },
        "pnl_delta": round(early["realized_pnl"] - base["realized_pnl"], 6),
    }


def compare_ledgers(base_path, early_path, now):
    base_uri = Path(base_path).resolve().as_uri() + "?mode=ro"
    early_uri = Path(early_path).resolve().as_uri() + "?mode=ro"
    if base_uri == early_uri:
        return {"available": False, "reason": "需要两个独立的 Paper 账本"}
    with closing(sqlite3.connect(base_uri, uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ? AS early", (early_uri,))
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        return _summarize(conn, now)


@lru_cache(maxsize=4)
def _cached_comparison(base_path, early_path, time_bucket):
    return compare_ledgers(base_path, early_path, time.time())


def forecast_comparison(bot):
    runner = getattr(bot, "strategy_experiments", None)
    if runner is not None:
        with runner._lock:
            baseline = runner._bots.get(BASE)
            early = runner._bots.get(EARLY)
        if baseline is not None and early is not None:
            try:
                return _cached_comparison(baseline.store.db_path, early.store.db_path, int(time.time() // 15))
            except sqlite3.Error:
                return {"available": False, "reason": "暂时读不到同期 Paper 记录"}
    return {"available": False, "reason": "等待基线和提前版 Paper 账户"}

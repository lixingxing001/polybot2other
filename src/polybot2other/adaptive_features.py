"""Causal V3 features shared by live observations and historical diagnostics.

Both exchange source time and local receipt time must precede the decision.
Missing history stays missing; it never becomes a synthetic flat market.
"""
from __future__ import annotations

from contextlib import closing
import math
from pathlib import Path
import sqlite3

from .adaptive_calibration import finite

FEATURE_VERSION = "adaptive-context-features-v1"
MARKET_FIELDS = ("v3_momentum_10_bps", "v3_momentum_30_bps", "v3_momentum_60_bps",
                 "v3_volatility_bps", "v3_trend_gap_bps", "v3_spread")
FEATURE_FIELDS = (*MARKET_FIELDS, "v3_feature_source_ms", "v3_feature_received_ms",
                  "v3_history_seconds", "v3_feature_status", "v3_feature_version")


def committed_features(decision,decision_at):
    """Recheck immutable imported metadata without inventing missing observations."""
    result={k:decision.get(k) for k in FEATURE_FIELDS}
    when=finite(decision_at)
    source=finite(result.get("v3_feature_source_ms"))
    received=finite(result.get("v3_feature_received_ms"))
    has_history=any(finite(result.get(k)) is not None for k in MARKET_FIELDS[:-1])
    training=finite(decision.get("adaptive_training_cutoff"))
    invalid=(has_history and (when is None or source is None or received is None
                              or source>when*1000 or received>when*1000))
    invalid=invalid or (training is not None and (when is None or training>=when))
    if invalid:
        for key in MARKET_FIELDS[:-1]:result[key]=None
        result["v3_feature_status"]="INVALID_CAUSAL_PROVENANCE"
    return result


def extract_market_features(points, *, decision_at, ask=None, bid=None, target_price=None):
    result = {name: None for name in MARKET_FIELDS}
    result.update(v3_feature_version=FEATURE_VERSION, v3_feature_status="MISSING_HISTORY",
                  v3_feature_source_ms=None, v3_feature_received_ms=None, v3_history_seconds=0.)
    ask, bid, decision_at = finite(ask), finite(bid), finite(decision_at)
    if ask is not None and bid is not None and 0 < bid <= ask < 1:
        result["v3_spread"] = ask-bid
    if decision_at is None:
        return result
    cutoff = decision_at*1000
    unique = {}
    for point in points:
        if len(point) < 3:
            continue
        source, price, receipt = (finite(x) for x in point[:3])
        if (source is None or price is None or receipt is None or price <= 0
                or source > cutoff or receipt > cutoff or source < cutoff-125000):
            continue
        previous = unique.get(source)
        if previous is None or receipt < previous[2]:
            unique[source] = (source, price, receipt)
    rows = sorted(unique.values())
    if not rows:
        return result
    stamp, price, received = rows[-1]
    result.update(v3_feature_source_ms=stamp, v3_feature_received_ms=received,
                  v3_history_seconds=(stamp-rows[0][0])/1000)
    if cutoff-stamp > 3000:
        result["v3_feature_status"] = "STALE_HISTORY"
        return result
    if len(rows) < 40 or stamp-rows[0][0] < 60000:
        return result
    if any(b[0]-a[0] > 4000 for a,b in zip(rows,rows[1:])):
        result["v3_feature_status"] = "GAPPED_HISTORY"
        return result
    for seconds in (10,30,60):
        before = next((r for r in reversed(rows) if r[0] <= stamp-seconds*1000),None)
        if before is not None and stamp-seconds*1000-before[0] <= 3000:
            result[f"v3_momentum_{seconds}_bps"] = math.log(price/before[1])*10000
    variance = elapsed = 0.
    for a,b in zip(rows,rows[1:]):
        dt = (b[0]-a[0])/1000
        if 0 < dt <= 4:
            variance += (math.log(b[1]/a[1])*10000)**2
            elapsed += dt
    if elapsed >= 60:
        result["v3_volatility_bps"] = math.sqrt(variance/elapsed)
    start = stamp-60000
    previous = next((r for r in reversed(rows) if r[0] <= start),None)
    if previous is not None and start-previous[0] <= 3000:
        last_t, last_price, integral = start, previous[1], 0.
        for row in rows:
            if row[0] <= start:
                continue
            integral += last_price*(row[0]-last_t)
            last_t,last_price = row[0],row[1]
        average = integral/60000
        if average > 0:
            result["v3_trend_gap_bps"] = math.log(price/average)*10000
    result["v3_feature_status"] = "READY" if all(result[k] is not None for k in MARKET_FIELDS[:-1]) else "PARTIAL_HISTORY"
    return result


def read_reference_features(path, decision_at, *, ask=None, bid=None, target_price=None):
    """Read a small indexed historical window without opening a writable source."""
    decision_at = finite(decision_at)
    rows = []
    path = Path(path) if path else None
    error = None
    if path is not None and path.exists() and decision_at is not None:
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=2)) as conn:
                conn.execute("PRAGMA query_only=ON")
                rows = conn.execute("""SELECT source_ms,price,received_ms FROM reference_ticks
                    WHERE channel='price.crypto' AND source_ms BETWEEN ? AND ? AND received_ms<=?
                    ORDER BY source_ms""",(decision_at*1000-125000,decision_at*1000,decision_at*1000)).fetchall()
        except (OSError,sqlite3.Error) as exc:
            error = type(exc).__name__
    result = extract_market_features(rows,decision_at=decision_at,ask=ask,bid=bid,target_price=target_price)
    if error:
        result["v3_feature_status"] = "SOURCE_ERROR:"+error
    return result


def current_reference_features(references, *, decision_at, ask=None, bid=None, target_price=None):
    """Use the collector's own first-receipt records, never a future latest tick."""
    rows = []
    decision_at = finite(decision_at)
    if decision_at is None:
        return extract_market_features([],decision_at=None,ask=ask,bid=bid,target_price=target_price)
    try:
        with references.lock:
            rows = references.conn.execute("""SELECT source_ms,price,received_ms FROM reference_ticks
                WHERE channel='price.crypto' AND source_ms BETWEEN ? AND ? AND received_ms<=?
                ORDER BY source_ms""",(decision_at*1000-125000,decision_at*1000,decision_at*1000)).fetchall()
    except (AttributeError,OSError,sqlite3.Error):
        # Missing optional learner features do not suspend the baseline strategy.
        pass
    return extract_market_features(rows,decision_at=decision_at,ask=ask,bid=bid,target_price=target_price)

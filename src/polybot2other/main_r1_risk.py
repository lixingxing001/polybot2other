"""Persistent entry risk limits for the isolated MAIN-R1 Paper account.

The caller supplies one reconciled NAV/cash/exposure checkpoint. This module
does not value assets, reserve cash, submit orders, or close existing positions.
Call ``evaluate`` continuously on valid checkpoints, including while entry is
paused. Each call is logged: unobserved intraday peaks cannot be reconstructed.
Without an exact midnight checkpoint a new day's conservative loss anchor is
max(previous valid adjusted NAV, current adjusted NAV), so a gap across midnight
does not silently erase the loss during that gap.

Every mutation owns a separate SQLite transaction. Never call it from inside a
reservation transaction. ``snapshot`` is informational and read-only; it is not
a fresh permission to enter. There is deliberately no automatic/manual reset
API: persistent halts require a separately reviewed, auditable recovery design
that must retain the historical high watermark.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import math
from numbers import Integral, Real
import time
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
INITIAL_BALANCE = Decimal("1000")
MAX_ENTRY_BUDGET = Decimal("5")
ENTRY_RISK_FRACTION = Decimal("0.005")
EXPOSURE_FRACTION = Decimal("0.01")
DAILY_LOSS_FRACTION = Decimal("0.02")
PEAK_DRAWDOWN_FRACTION = Decimal("0.08")
MAX_UNRESOLVED_ROUNDS = 2
MONEY_QUANTUM = Decimal("0.000001")
PEAK_HALT_REASON = "peak_drawdown_limit"


def _number(value, name, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = Decimal(str(value))
        finite = result.is_finite() and math.isfinite(float(result))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        finite = False
    if not finite or (nonnegative and result < 0):
        suffix = " nonnegative" if nonnegative else ""
        raise ValueError(f"{name} must be a finite{suffix} number")
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class MainR1Risk:
    """One persistent risk state per independent MAIN-R1 database.

    ``store`` must expose ``conn`` (SQLite) and ``_lock`` (context manager).
    ``net_external_flow`` is cumulative *verified* funding since experiment
    start, not the flow since the previous sample. Budget uses verified NAV;
    daily/peak loss limits use adjusted NAV = NAV - cumulative funding.
    """

    def __init__(self, store, clock=time.time):
        self.store = store
        self.conn = store.conn
        self._lock = store._lock
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.clock = clock
        with self._transaction():
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS main_r1_risk_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    day TEXT,
                    day_start_nav REAL,
                    high_watermark REAL NOT NULL,
                    last_adjusted_nav REAL,
                    last_sample_at REAL,
                    last_observed_at REAL,
                    day_halted INTEGER NOT NULL DEFAULT 0,
                    day_anchor_json TEXT,
                    latest_result_json TEXT
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS main_r1_risk_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL, day TEXT NOT NULL,
                    nav REAL NOT NULL, available_cash REAL NOT NULL,
                    exposure REAL NOT NULL, unresolved_rounds INTEGER NOT NULL,
                    net_external_flow REAL NOT NULL, adjusted_nav REAL NOT NULL,
                    day_start_nav REAL NOT NULL, high_watermark REAL NOT NULL,
                    day_halted INTEGER NOT NULL, allowed INTEGER NOT NULL,
                    budget REAL NOT NULL, reason TEXT NOT NULL,
                    day_anchor_json TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS main_r1_risk_halts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
                    adjusted_nav REAL, high_watermark REAL NOT NULL
                )
            """)
            self.conn.execute("""
                INSERT OR IGNORE INTO main_r1_risk_state (id, high_watermark)
                VALUES (1, ?)
            """, (float(INITIAL_BALANCE),))

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self.conn.in_transaction:
                raise RuntimeError("MAIN-R1 risk mutation requires its own transaction")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _one(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        row = cursor.fetchone()
        return None if row is None else dict(zip((x[0] for x in cursor.description), row))

    def _state(self):
        return self._one("SELECT * FROM main_r1_risk_state WHERE id=1")

    def _halts(self):
        cursor = self.conn.execute("SELECT * FROM main_r1_risk_halts ORDER BY id")
        columns = [x[0] for x in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _time(self, now):
        at = float(_number(self.clock() if now is None else now, "now", nonnegative=True))
        try:
            local = datetime.fromtimestamp(at, TIMEZONE)
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("now is outside the supported calendar") from exc
        return at, local.date().isoformat(), midnight

    @staticmethod
    def _check_time(state, at):
        previous = state["last_observed_at"]
        if previous is not None and at < previous:
            raise ValueError("MAIN-R1 risk clock moved backwards")

    def evaluate(self, *, nav, available_cash, exposure, unresolved_rounds,
                 now=None, net_external_flow=0.0):
        """Record a valid checkpoint and return a new entry decision.

        Invalid/missing valuations or a reversed clock raise ``ValueError`` and
        leave risk state untouched. The caller must pause entry on these errors;
        it must never substitute zero or reuse an earlier allowed decision.
        Zero NAV is a valid insolvency checkpoint and triggers the loss limits.
        ``budget`` is zero whenever blocked; ``candidate_budget`` shows the
        unblocked cap for diagnostics. It never overrides exchange minimum size.
        """
        nav_d = _number(nav, "nav", nonnegative=True)
        cash_d = _number(available_cash, "available_cash", nonnegative=True)
        exposure_d = _number(exposure, "exposure", nonnegative=True)
        flow_d = _number(net_external_flow, "net_external_flow")
        if (isinstance(unresolved_rounds, bool)
                or not isinstance(unresolved_rounds, Integral) or unresolved_rounds < 0):
            raise ValueError("unresolved_rounds must be a nonnegative integer")
        unresolved_rounds = int(unresolved_rounds)
        adjusted = _number(nav_d - flow_d, "adjusted_nav")
        at, day, midnight = self._time(now)

        with self._transaction():
            state = self._state()
            self._check_time(state, at)
            previous_a = state["last_adjusted_nav"]
            previous_at = state["last_sample_at"]
            if state["day"] is None:
                baseline = max(INITIAL_BALANCE, adjusted)
                anchor = {"kind": "initial_virtual_balance_conservative",
                          "baseline": float(baseline), "at": at,
                          "initial_virtual_balance": float(INITIAL_BALANCE),
                          "midnight_observed": at == midnight,
                          "local_midnight_at": midnight,
                          "current_adjusted_nav": float(adjusted)}
                day_halted = False
            elif state["day"] != day:
                exact_midnight = at == midnight
                baseline = adjusted if exact_midnight else max(Decimal(str(previous_a)), adjusted)
                anchor = {"kind": "exact_midnight" if exact_midnight else "conservative_sampling_gap",
                          "baseline": float(baseline), "at": at,
                          "midnight_observed": exact_midnight,
                          "local_midnight_at": midnight,
                          "previous_sample_at": previous_at,
                          "previous_adjusted_nav": previous_a,
                          "current_adjusted_nav": float(adjusted),
                          "gap_seconds": at - previous_at}
                day_halted = False
            else:
                baseline = Decimal(str(state["day_start_nav"]))
                anchor = json.loads(state["day_anchor_json"])
                day_halted = bool(state["day_halted"])

            high = max(Decimal(str(state["high_watermark"])), adjusted)
            if baseline <= 0 or adjusted <= baseline * (1 - DAILY_LOSS_FRACTION):
                day_halted = True
            halt_events = self._halts()
            reasons = list(dict.fromkeys(x["reason"] for x in halt_events))
            if adjusted <= high * (1 - PEAK_DRAWDOWN_FRACTION) and PEAK_HALT_REASON not in reasons:
                self.conn.execute("""
                    INSERT INTO main_r1_risk_halts
                    (at,reason,source,adjusted_nav,high_watermark) VALUES (?,?,?,?,?)
                """, (at, PEAK_HALT_REASON, "risk_threshold", float(adjusted), float(high)))
                halt_events = self._halts()
                reasons = list(dict.fromkeys(x["reason"] for x in halt_events))

            remaining = max(Decimal(0), EXPOSURE_FRACTION * nav_d - exposure_d)
            cap = min(MAX_ENTRY_BUDGET, ENTRY_RISK_FRACTION * nav_d, cash_d, remaining)
            candidate = cap.quantize(MONEY_QUANTUM, rounding=ROUND_FLOOR)
            if reasons:
                reason = "persistent_halt"
            elif day_halted:
                reason = "daily_loss_limit"
            elif unresolved_rounds >= MAX_UNRESOLVED_ROUNDS:
                reason = "unresolved_round_limit"
            elif cash_d <= 0:
                reason = "no_available_cash"
            elif remaining <= 0:
                reason = "exposure_limit"
            elif candidate <= 0:
                reason = "budget_below_precision"
            else:
                reason = "allowed"
            allowed = reason == "allowed"
            result = {
                "initialized": True, "allowed": allowed,
                "budget": float(candidate) if allowed else 0.0,
                "candidate_budget": float(candidate), "reason": reason,
                "nav": float(nav_d), "available_cash": float(cash_d),
                "exposure": float(exposure_d), "remaining_risk_budget": float(remaining),
                "unresolved_rounds": unresolved_rounds,
                "net_external_flow": float(flow_d), "adjusted_nav": float(adjusted),
                "day_start_nav": float(baseline), "high_watermark": float(high),
                "day": day, "day_halted": day_halted, "day_anchor": anchor,
                "day_loss_fraction": float(max(Decimal(0), (baseline-adjusted)/baseline)) if baseline > 0 else None,
                "peak_drawdown_fraction": float(max(Decimal(0), (high-adjusted)/high)),
                "persistent_halts": reasons, "halt_events": halt_events,
                "latest_sample_at": at, "last_observed_at": at,
            }
            cursor = self.conn.execute("""
                INSERT INTO main_r1_risk_samples
                (at,day,nav,available_cash,exposure,unresolved_rounds,net_external_flow,
                 adjusted_nav,day_start_nav,high_watermark,day_halted,allowed,budget,
                 reason,day_anchor_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (at, day, float(nav_d), float(cash_d), float(exposure_d), unresolved_rounds,
                  float(flow_d), float(adjusted), float(baseline), float(high), int(day_halted),
                  int(allowed), result["budget"], reason, _json(anchor)))
            result["latest_sample_id"] = cursor.lastrowid
            self.conn.execute("""
                UPDATE main_r1_risk_state SET day=?,day_start_nav=?,high_watermark=?,
                last_adjusted_nav=?,last_sample_at=?,last_observed_at=?,day_halted=?,
                day_anchor_json=?,latest_result_json=? WHERE id=1
            """, (day, float(baseline), float(high), float(adjusted), at, at, int(day_halted),
                  _json(anchor), _json(result)))
            return result

    def halt(self, reason, now=None):
        """Append an externally requested persistent halt; never clear old halts."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("halt reason must be a nonempty string")
        reason = reason.strip()
        at, _, _ = self._time(now)
        with self._transaction():
            state = self._state()
            self._check_time(state, at)
            self.conn.execute("""
                INSERT INTO main_r1_risk_halts
                (at,reason,source,adjusted_nav,high_watermark) VALUES (?,?,?,?,?)
            """, (at, reason, "external", state["last_adjusted_nav"], state["high_watermark"]))
            self.conn.execute("UPDATE main_r1_risk_state SET last_observed_at=? WHERE id=1", (at,))
            return self._snapshot_locked()

    def _snapshot_locked(self):
        state = self._state()
        result = json.loads(state["latest_result_json"]) if state["latest_result_json"] else {
            "initialized": False, "allowed": False, "budget": 0.0,
            "candidate_budget": 0.0, "reason": "not_evaluated", "nav": None,
            "adjusted_nav": None, "day_start_nav": None,
            "high_watermark": state["high_watermark"], "day": None,
            "day_halted": False, "day_anchor": None, "latest_sample_at": None,
        }
        events = self._halts()
        result["halt_events"] = events
        result["persistent_halts"] = list(dict.fromkeys(x["reason"] for x in events))
        result["last_observed_at"] = state["last_observed_at"]
        if events:
            result.update(allowed=False, budget=0.0, reason="persistent_halt")
        return result

    def snapshot(self):
        """Return persisted observations without taking a sample or resetting a day."""
        with self._lock:
            return self._snapshot_locked()

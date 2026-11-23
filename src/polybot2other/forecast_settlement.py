"""Recover official outcomes for rounds observed by the independent forecast account."""
from __future__ import annotations

import threading
import time


class ForecastSettlements:
    def __init__(self, store, shared_round=None):
        self.store = store
        self.shared_round = shared_round
        self._worker = None
        self._next_check = 0.
        self.last_error = None
        with store._lock, store.conn:
            store.conn.execute("""CREATE TABLE IF NOT EXISTS forecast_resolution_retries (
                round_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, next_at REAL NOT NULL)""")
            store.conn.execute("""CREATE INDEX IF NOT EXISTS forecast_unresolved_rounds
                ON market_rounds(ends_at) WHERE outcome IS NULL""")

    def check(self, now, get_resolution):
        if now < self._next_check or (self._worker and self._worker.is_alive()):
            return
        self._next_check = now + 15
        self._worker = threading.Thread(
            target=self._poll, args=(now, get_resolution), name="forecast-paper-settlements", daemon=True,
        )
        self._worker.start()

    def join(self):
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=3)

    def _poll(self, now, get_resolution):
        try:
            with self.store._lock:
                due = self.store.conn.execute("""SELECT r.round_id, COALESCE(a.attempts, 0)
                    FROM market_rounds r LEFT JOIN forecast_resolution_retries a USING(round_id)
                    WHERE r.outcome IS NULL AND r.ends_at<=? AND COALESCE(a.next_at,0)<=?
                    AND (EXISTS (SELECT 1 FROM forecast_checkpoints c WHERE c.round_id=r.round_id)
                      OR EXISTS (SELECT 1 FROM trades t WHERE t.round_id=r.round_id))
                    ORDER BY r.ends_at LIMIT 8""", (now, now)).fetchall()
            requested = False
            for round_id, attempts in due:
                resolved = self.shared_round(round_id) if self.shared_round else None
                if not resolved or resolved.get("settlement_source") != "polymarket_official" or resolved.get("outcome") not in ("Up", "Down"):
                    if requested:
                        continue
                    requested = True
                    self._defer(round_id, attempts, now)
                    resolved = get_resolution(round_id)
                if not resolved or resolved.get("outcome") not in ("Up", "Down"):
                    continue
                self.store.settle_round_outcome(
                    round_id, resolved["outcome"], time.time(), final_price=resolved.get("final_price"),
                    target_price=resolved.get("target_price"), settlement_source="polymarket_official",
                )
                # A match still awaiting confirmation blocks settlement in the common ledger.
                if self.store.get_round(round_id)["outcome"] is not None:
                    with self.store._lock, self.store.conn:
                        self.store.conn.execute("DELETE FROM forecast_resolution_retries WHERE round_id=?", (round_id,))
            self.last_error = None
        except Exception as exc:
            # A slow or unavailable official source must not interrupt forecasting or Paper orders.
            self.last_error = type(exc).__name__

    def _defer(self, round_id, attempts, now):
        delay = min(900, 15 * 2 ** min(attempts, 6))
        with self.store._lock, self.store.conn:
            self.store.conn.execute("""INSERT INTO forecast_resolution_retries VALUES(?,?,?)
                ON CONFLICT(round_id) DO UPDATE SET attempts=excluded.attempts,next_at=excluded.next_at""",
                (round_id, attempts + 1, now + delay))

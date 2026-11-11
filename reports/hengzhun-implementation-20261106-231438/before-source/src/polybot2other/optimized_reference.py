"""Authenticated, read-only Chainlink spot/TWAP references for the isolated account."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import random
import sqlite3
import threading
import time

from .clob_ws import _StdlibWebSocket

SPOT = "price.crypto"
TWAP = "price.crypto.twap"
CHANNELS = (SPOT, TWAP)


class ReferenceCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS reference_ticks (
                channel TEXT NOT NULL, source_ms INTEGER NOT NULL,
                price REAL NOT NULL, exact_price TEXT NOT NULL,
                received_ms REAL NOT NULL, snapshot INTEGER NOT NULL,
                PRIMARY KEY(channel, source_ms)
            );
            CREATE TABLE IF NOT EXISTS reference_validation (
                round_id TEXT PRIMARY KEY, observed_open REAL NOT NULL,
                official_open REAL NOT NULL, difference REAL NOT NULL,
                verified INTEGER NOT NULL, checked_at REAL NOT NULL
            );
        """)
        self.latest = {}
        self.sequences = {}
        self.last_sources = {}
        self.state = "waiting"
        self.last_error = None
        self.frames = 0

    def reset_connection(self, state="connecting"):
        with self.lock:
            self.latest.clear()
            self.sequences.clear()
            self.last_sources.clear()
            self.state = state

    def ingest(self, frame, received_ms=None):
        received_ms = time.time()*1000 if received_ms is None else received_ms
        channel = frame.get("channel")
        if channel not in CHANNELS:
            return False
        payload = frame.get("payload") or {}
        if payload.get("symbol") != "btcusd" or payload.get("source") != "chainlink":
            return False
        if channel == TWAP and payload.get("window_seconds") != 60:
            return False
        if payload.get("is_carried_forward"):
            return False
        with self.lock:
            seq = frame.get("seq")
            previous_seq = self.sequences.get(channel)
            if frame.get("dropped"):
                self.latest.clear()
                raise ValueError("REFERENCE_DROPPED")
            if isinstance(seq, int):
                if previous_seq is not None and seq <= previous_seq:
                    return False
                if previous_seq is not None and seq != previous_seq + 1:
                    self.latest.clear()
                    raise ValueError("REFERENCE_SEQUENCE_GAP")
                self.sequences[channel] = seq
            snapshot = bool(frame.get("snapshot"))
            rows = payload.get("data") if snapshot else [payload]
            if not isinstance(rows, list):
                return False
            advanced = False
            with self.conn:
                for row in rows:
                    try:
                        stamp = int(row["timestamp"])
                        exact = str(row.get("full_accuracy_value", row.get("value")))
                        value = float(exact)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        continue
                    if not math.isfinite(value) or value <= 0 or stamp > received_ms+1000:
                        continue
                    self.conn.execute(
                        "INSERT OR IGNORE INTO reference_ticks VALUES(?,?,?,?,?,?)",
                        (channel, stamp, value, exact, received_ms, int(snapshot)),
                    )
                    if stamp <= self.last_sources.get(channel, 0):
                        continue
                    self.last_sources[channel] = stamp
                    if not snapshot:
                        self.latest[channel] = {
                            "price": value, "exact_price": exact, "source_ms": stamp,
                            "received_ms": received_ms, "source": "chainlink",
                        }
                        advanced = True
            self.frames += 1
            self.state = "streaming" if all(c in self.latest for c in CHANNELS) else "warming"
            self.last_error = None
            return advanced

    def import_history(self, path: Path):
        """Bootstrap fixed references/history only; never establish live freshness."""
        if not path.exists():
            return
        with self.lock, self.conn:
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get("source") != "chainlink" or row.get("channel") not in CHANNELS:
                    continue
                if row["channel"] == TWAP and row.get("window_seconds") != 60:
                    continue
                value = float(row["value"])
                if not math.isfinite(value) or value <= 0:
                    continue
                self.conn.execute("INSERT OR IGNORE INTO reference_ticks VALUES(?,?,?,?,?,1)",
                                  (row["channel"], int(row["source_ms"]), value, str(row["value"]), row["received_ms"]))

    def points(self, channel, start_ms, end_ms):
        with self.lock:
            return self.conn.execute(
                "SELECT source_ms,price FROM reference_ticks WHERE channel=? AND source_ms BETWEEN ? AND ? ORDER BY source_ms",
                (channel, int(start_ms), int(end_ms)),
            ).fetchall()

    def opening(self, start_s):
        rows = self.points(TWAP, int(start_s*1000), int(start_s*1000))
        return rows[0][1] if rows else None

    def completed_openings(self, now_s):
        with self.lock:
            return [row[0]/1000 for row in self.conn.execute(
                "SELECT source_ms FROM reference_ticks WHERE channel=? AND source_ms%300000=0 AND source_ms<? ORDER BY source_ms DESC LIMIT 3",
                (TWAP, int((now_s-330)*1000)),
            )]

    def verify(self, round_id, start_s, official_open):
        observed = self.opening(start_s)
        if observed is None or official_open is None or float(official_open) <= 0:
            return None
        difference = observed-float(official_open)
        verified = abs(difference) <= .01
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO reference_validation VALUES(?,?,?,?,?,?)",
                              (round_id, observed, float(official_open), difference, int(verified), time.time()))
        return verified

    def protocol_verified(self):
        with self.lock:
            row = self.conn.execute("SELECT verified FROM reference_validation ORDER BY checked_at DESC LIMIT 1").fetchone()
        return bool(row and row[0])

    def snapshot(self, now_ms=None):
        now_ms = time.time()*1000 if now_ms is None else now_ms
        with self.lock:
            latest = {k: dict(v) for k,v in self.latest.items()}
            state, error, frames = self.state, self.last_error, self.frames
        for row in latest.values():
            row["source_age_ms"] = now_ms-row["source_ms"]
            row["receive_age_ms"] = now_ms-row["received_ms"]
            row["fresh"] = -1000 <= row["source_age_ms"] <= 4000 and 0 <= row["receive_age_ms"] <= 2500
        return {"state": state, "error": error, "frames": frames, "latest": latest,
                "ready": all(latest.get(c,{}).get("fresh") for c in CHANNELS),
                "protocol_verified": self.protocol_verified()}


class PolyBoltReferenceFeed:
    def __init__(self, cache: ReferenceCache, socket_factory=None):
        self.cache = cache
        self.ws = None
        self.socket_factory=socket_factory

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except OSError:
                pass

    def run(self, stop):
        # Only existing API credentials are used. No private key, order client or derivation.
        auth = {k: os.environ.get(v,"") for k,v in (
            ("apiKey","POLYBOT2OTHER_LIVE_API_KEY"),
            ("secret","POLYBOT2OTHER_LIVE_API_SECRET"),
            ("passphrase","POLYBOT2OTHER_LIVE_API_PASSPHRASE"),
        )}
        if not all(auth.values()):
            self.cache.state = "READ_CREDENTIALS_NOT_CONFIGURED"
            return
        backoff = .5
        while not stop.is_set():
            self.cache.reset_connection()
            try:
                self.ws = (self.socket_factory or _StdlibWebSocket.connect)("wss://ws-live-v2.polymarket.com/ws", 8)
                self.ws.send_text(json.dumps({"op":"auth","auth":auth}))
                authenticated = False
                deadline = time.monotonic()+10
                while not stop.is_set() and time.monotonic()<deadline:
                    text = self.ws.read_text(1)
                    if text and json.loads(text).get("op") == "authed":
                        authenticated = True
                        break
                if not authenticated:
                    raise ValueError("REFERENCE_AUTH_NOT_CONFIRMED")
                self.ws.send_text(json.dumps({"op":"subscribe","subscriptions":[
                    {"channel":SPOT,"filter":{"symbol":"btcusd","provider":"chainlink"}},
                    {"channel":TWAP,"filter":{"symbol":"btcusd","window_seconds":60}},
                ]}))
                last_advance = {channel: time.monotonic() for channel in CHANNELS}
                while not stop.is_set():
                    if time.monotonic()-min(last_advance.values()) > 12:
                        raise ValueError("REFERENCE_STREAM_STALLED")
                    text = self.ws.read_text(1)
                    if not text:
                        continue
                    frame = json.loads(text)
                    if frame.get("op") == "error":
                        raise ValueError("REFERENCE_PROTOCOL_REJECTED")
                    if self.cache.ingest(frame):
                        last_advance[frame["channel"]] = time.monotonic()
                        backoff = .5
            except Exception as exc:
                self.cache.reset_connection("reconnecting")
                # Whitelist local error labels; never echo server frames or credentials.
                message = str(exc)
                self.cache.last_error = message if message.startswith("REFERENCE_") else type(exc).__name__
            finally:
                self.close()
                self.ws = None
            if not stop.is_set():
                stop.wait(backoff+random.uniform(0,.25))
                backoff = min(30,backoff*2)

"""Copy public market fields into the isolated Paper service's SQLite feed."""
import argparse
from contextlib import closing
import logging
from pathlib import Path
import signal
import sqlite3
import threading
import time

MAIN = Path("polybot2other-real-btc.sqlite3")
REF = Path("live-accounts/market.references.sqlite3")
CHANNELS = ("price.crypto", "price.crypto.twap")
MARKET_FIELDS = ("round_id", "symbol", "started_at", "ends_at", "target_price", "question",
                 "condition_id", "up_token", "down_token", "event_slug", "url")
TICK_FIELDS = ("channel", "source_ms", "price", "received_ms", "snapshot")
VALIDATION_FIELDS = ("round_id", "checked_at", "verified")
MARKET_TABLE = """CREATE TABLE IF NOT EXISTS market_rounds(
    round_id TEXT PRIMARY KEY, symbol TEXT, started_at REAL, ends_at REAL, target_price REAL,
    question TEXT, condition_id TEXT, up_token TEXT, down_token TEXT, event_slug TEXT, url TEXT)"""
TICK_TABLE = """CREATE TABLE IF NOT EXISTS reference_ticks(
    channel TEXT, source_ms INTEGER, price REAL, received_ms REAL, snapshot INTEGER,
    PRIMARY KEY(channel, source_ms))"""
VALIDATION_TABLE = """CREATE TABLE IF NOT EXISTS reference_validation(
    round_id TEXT PRIMARY KEY, checked_at REAL, verified INTEGER)"""


def reader(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)
    db.execute("PRAGMA query_only=ON")
    return db


def writer(path, tables):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    with db:
        for statement in tables:
            db.execute(statement)
    return db


def upsert(db, table, fields, keys, rows):
    values = [k for k in fields if k not in keys]
    changes = ",".join(name + "=excluded." + name for name in values)
    changed = " OR ".join(name + " IS NOT excluded." + name for name in values)
    placeholders = ",".join("?" for _ in fields)
    db.executemany(f"INSERT INTO {table}({','.join(fields)}) VALUES ({placeholders}) "
                   f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {changes} WHERE {changed}", rows)


class Projection:
    def __init__(self, source, output):
        self.source = source
        if source.resolve() == output.resolve():
            raise ValueError("source and projected market directories must be distinct")
        self.market = writer(output / MAIN, (MARKET_TABLE,))
        self.ref = writer(output / REF, (TICK_TABLE, VALIDATION_TABLE))
        self.previous = None
        self.next_cleanup = 0

    def tick(self, now):
        millis = int(now * 1000)
        # A long source interruption needs a fresh history window. During normal
        # streaming reread an overlap to collect delayed arrivals and corrections.
        start = millis - 15 * 60_000 if self.previous is None else max(millis - 15 * 60_000,
                                                                       self.previous - 10_000)
        round_id = f"btc-updown-5m-{int(now) // 300 * 300}"
        with closing(reader(self.source / MAIN)) as db:
            market = db.execute(f"SELECT {','.join(MARKET_FIELDS)} FROM market_rounds WHERE round_id=?",
                                (round_id,)).fetchall()
        with closing(reader(self.source / REF)) as db:
            db.execute("BEGIN")
            ticks = db.execute(f"SELECT {','.join(TICK_FIELDS)} FROM reference_ticks WHERE channel IN (?,?) "
                               "AND source_ms BETWEEN ? AND ?", (*CHANNELS, start, millis)).fetchall()
            verified = db.execute(f"SELECT {','.join(VALIDATION_FIELDS)} FROM reference_validation "
                                  "ORDER BY checked_at DESC LIMIT 1").fetchall()
        with self.market:
            upsert(self.market, "market_rounds", MARKET_FIELDS, ("round_id",), market)
        with self.ref:
            upsert(self.ref, "reference_ticks", TICK_FIELDS, ("channel", "source_ms"), ticks)
            if verified:
                upsert(self.ref, "reference_validation", VALIDATION_FIELDS, ("round_id",), verified)
            else:
                self.ref.execute("DELETE FROM reference_validation")
        if now >= self.next_cleanup:
            with self.market:
                self.market.execute("DELETE FROM market_rounds WHERE ends_at<?", (now - 7 * 86400,))
            with self.ref:
                for channel in CHANNELS:
                    self.ref.execute("DELETE FROM reference_ticks WHERE channel=? AND source_ms<?",
                                     (channel, millis - 2 * 86_400_000))
                self.ref.execute("DELETE FROM reference_validation WHERE checked_at<? AND round_id IS NOT "
                                 "(SELECT round_id FROM reference_validation ORDER BY checked_at DESC LIMIT 1)",
                                 (now - 2 * 86400,))
            self.next_cleanup = now + 300
        self.previous = millis

    def close(self):
        self.market.close()
        self.ref.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    project = Projection(args.source, args.output)
    report_after = 0
    try:
        while not stop.is_set():
            try:
                project.tick(time.time())
            except sqlite3.Error as exc:
                if time.monotonic() >= report_after:
                    logging.warning("upstream market rows unavailable (%s)", type(exc).__name__)
                    report_after = time.monotonic() + 30
            stop.wait(.45)
    finally:
        project.close()


if __name__ == "__main__":
    main()

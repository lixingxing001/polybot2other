"""Read the existing verified reference ticks without opening a second authenticated stream."""
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time

from .managed_live_book import ReliableLiveBooks
from .models import MarketRound
from .optimized_reference import CHANNELS
from .shared_market_inputs import SharedMarketInputs


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                           timeout=1, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    return conn


class TrialReferences:
    def __init__(self, path):
        self.conn = readonly(path)
        self.lock = threading.RLock()

    def points(self, channel, start_ms, end_ms):
        with self.lock:
            return self.conn.execute("SELECT source_ms,price FROM reference_ticks WHERE channel=? "
                                     "AND source_ms BETWEEN ? AND ? ORDER BY source_ms",
                                     (channel, int(start_ms), int(end_ms))).fetchall()

    def snapshot(self, now):
        millis = now * 1000
        latest = {}
        with self.lock:
            # Bound both clocks. Snapshot/backfill rows cannot establish live readiness.
            for channel in CHANNELS:
                row = self.conn.execute("SELECT source_ms,received_ms,price FROM reference_ticks "
                                        "WHERE channel=? AND source_ms BETWEEN ? AND ? AND received_ms<=? "
                                        "AND snapshot=0 ORDER BY source_ms DESC LIMIT 1",
                                        (channel, int(millis - 6000), int(millis), millis)).fetchone()
                if row:
                    stamp, received, price = row
                    latest[channel] = dict(source_ms=stamp, received_ms=received, price=price,
                                           fresh=millis-stamp <= 4000 and millis-received <= 2500)
            verified = self.conn.execute("SELECT verified FROM reference_validation "
                                         "ORDER BY checked_at DESC LIMIT 1").fetchone()
        return dict(latest=latest, ready=all(latest.get(k, {}).get("fresh") for k in CHANNELS),
                    protocol_verified=bool(verified and verified[0]))


class TrialFeed:
    def __init__(self, source, exchange, *, strict_received_time=False):
        self.source = Path(source)
        self.references = TrialReferences(self.source / "live-accounts/market.references.sqlite3")
        self.exchange = exchange
        self.lock = threading.RLock()
        self.market, self.quotes = None, {}
        self.rules, self.info = {}, {}
        self.rules_checked_at = 0.
        self.stop = threading.Event()
        self.books = ReliableLiveBooks(self.current, self.publish, self.get_quotes, exchange.rest_book,
                                       strict_received_time=strict_received_time)
        self.inputs = SharedMarketInputs(self, lambda: {})
        self.threads = []
        self.last_error = None

    def current(self):
        with self.lock:
            return self.market

    def get_quotes(self):
        with self.lock:
            return dict(self.quotes)

    def publish(self, market, quotes):
        with self.lock:
            if self.market != market or market.ends_at <= time.time():
                return False
            for side, quote in quotes.items():
                if (side in ("Up", "Down") and
                        quote.get("token_id") == (market.up_token if side == "Up" else market.down_token)
                        and quote.get("source_ms", 0) >= self.quotes.get(side, {}).get("source_ms", 0)):
                    self.quotes[side] = quote
            return True

    def observation(self):
        with self.lock:
            now = time.time()
            market = self.market if self.market and self.market.started_at <= now < self.market.ends_at else None
            quotes = dict(self.quotes) if market else {}
        return market, self.references.snapshot(now), quotes, now

    def capture(self):
        return self.inputs.capture()

    def _primary_market(self, start):
        with closing(readonly(self.source / "polybot2other-real-btc.sqlite3")) as conn:
            row = conn.execute("SELECT round_id,symbol,started_at,ends_at,target_price,question,condition_id,"
                               "up_token,down_token,event_slug,url FROM market_rounds WHERE round_id=?",
                               (f"btc-updown-5m-{start}",)).fetchone()
        if row and row[4] and row[4] > 0 and all(row[6:9]):
            return MarketRound(*row[:9], slug=row[0], event_slug=row[9] or row[0], url=row[10] or "")

    def _refresh(self):
        next_rules = 0
        while not self.stop.is_set():
            try:
                now = time.time()
                market = self.current()
                if not market or not market.started_at <= now < market.ends_at:
                    market = self._primary_market(int(now)//300*300)
                    with self.lock:
                        self.market, self.quotes, self.info, self.rules = market, {}, {}, {}
                        self.rules_checked_at = 0.
                    next_rules = 0
                if market and now >= next_rules:
                    info = self.exchange.market_info(market)
                    rules = {}
                    for side, token in (("Up", market.up_token), ("Down", market.down_token)):
                        try:
                            book = self.exchange.rest_book(token)
                            if book["token_id"] == token and book["tick_size"] == info["tick_size"]:
                                rules[side] = {k: book[k] for k in ("token_id", "tick_size", "min_order_size")}
                        except Exception:
                            continue
                    with self.lock:
                        self.info, self.rules = info, rules
                        self.rules_checked_at = now
                    next_rules = now + (40 if len(rules) == 2 else 5)
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.stop.wait(2)
            self.stop.wait(.5)

    def start(self):
        for task, args in ((self._refresh, ()), (self.books.run, (self.stop,))):
            thread = threading.Thread(target=task, args=args, daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stop.set()
        self.books.request_resync("试验退出")
        for thread in self.threads:
            thread.join(timeout=8)
        with self.references.lock:
            self.references.conn.close()

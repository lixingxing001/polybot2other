"""Arrival evidence and R1-only wire receipt times, with no network or live ledger."""
from copy import deepcopy
import inspect
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_main_r1_execution as execution_helpers
from polybot2other import main_r1_paper, managed_live_book, trial_market_feed


@pytest.fixture
def r1():
    # Reuse the disposable ledger and fake clock without collecting inherited tests.
    case = execution_helpers.MainR1ExecutionTests(methodName="runTest")
    try:
        case.setUp()
        yield case
    finally:
        case.doCleanups()


def phase(case, ident):
    return case.store.conn.execute(
        "SELECT phase FROM paper_sim_orders WHERE order_id=?", (ident,)).fetchone()[0]


def arrival_book(case, stamps):
    def capture():
        frame = case.frame()
        frame.quotes["Up"].update(stamps)
        return frame
    case.engine.frame_reader = capture


@pytest.mark.parametrize("early", ["source_ms", "received_ms", "both"])
def test_pre_arrival_quote_waits_then_matches_only_later_book(r1, early):
    ident = r1.queue()
    original, = r1.engine.pending()
    arrival = original["submitted_at"] + original["arrival_delay"]
    r1.now = arrival + .1
    stamps = {"source_ms": r1.now*1000, "received_ms": r1.now*1000}
    for key in ("source_ms", "received_ms"):
        if early in (key, "both"):
            stamps[key] = arrival*1000-1
    arrival_book(r1, stamps)
    r1.engine.poll()
    assert phase(r1, ident) == "IN_FLIGHT"
    assert r1.store.account()["cash_balance"] == 995
    assert r1.store.reserved_cash() == 5
    assert r1.store.open_trade_count() == 0
    pending, = r1.engine.pending()
    assert pending["due_at"] == original["due_at"]
    audit = r1.store.conn.execute(
        "SELECT detail_json FROM main_r1_decisions WHERE order_id=? AND code='ARRIVAL_WAIT' ORDER BY id DESC LIMIT 1",
        (ident,)).fetchone()
    assert audit is not None
    evidence = json.loads(audit[0])["arrival"]
    assert evidence["arrival_at"] == arrival
    assert evidence["deadline_at"] == arrival+r1.policy.max_poll_lateness_seconds
    assert evidence["source_ms"] == stamps["source_ms"]
    assert evidence["received_ms"] == stamps["received_ms"]
    # A different, later price must determine the fill, not the earlier .61 quote.
    r1.now += .1
    r1.asks = [{"price": .65, "size": 1000.}]
    stamps.update(source_ms=r1.now*1000, received_ms=r1.now*1000)
    r1.engine.poll()
    matched, = r1.engine.pending()
    assert matched["phase"] == "CONFIRMING"
    assert matched["fill"]["price"] == pytest.approx(.65)
    assert matched["arrival_book"]["source_ms"] >= arrival*1000
    assert matched["arrival_book"]["received_ms"] >= arrival*1000


def test_both_timestamp_bounds_include_exact_arrival(r1):
    ident = r1.queue()
    order, = r1.engine.pending()
    r1.now = order["submitted_at"] + order["arrival_delay"]
    r1.engine.poll()
    assert phase(r1, ident) == "CONFIRMING"


def test_wait_deadline_is_fixed_and_expiry_never_refunds_or_replays(r1):
    ident = r1.queue()
    order, = r1.engine.pending()
    arrival = order["submitted_at"] + order["arrival_delay"]
    stamps = {"source_ms": arrival*1000-1, "received_ms": arrival*1000-1}
    arrival_book(r1, stamps)
    r1.now = arrival+.1
    r1.engine.poll()
    assert phase(r1, ident) == "IN_FLIGHT"
    r1.now = arrival+r1.policy.max_poll_lateness_seconds+.001
    r1.engine.poll()
    assert phase(r1, ident) == "UNKNOWN"
    assert r1.store.account()["cash_balance"] == 995
    assert r1.store.reserved_cash() == 5
    assert r1.store.recent_paper_orders(1)[0]["status"] == "PENDING"
    assert "unknown_order" in r1.engine.snapshot()["risk_state"]["persistent_halts"]
    stamps.update(source_ms=r1.now*1000, received_ms=r1.now*1000)
    r1.advance(15)
    assert phase(r1, ident) == "UNKNOWN"
    assert r1.store.open_trade_count() == 0
    assert r1.store.account()["cash_balance"] == 995


def test_slow_frame_capture_cannot_match_after_fixed_deadline(r1):
    ident = r1.queue()
    order, = r1.engine.pending()
    arrival = order["submitted_at"]+order["arrival_delay"]
    r1.now = arrival+.1
    def delayed_capture():
        r1.now = arrival+r1.policy.max_poll_lateness_seconds+.001
        return r1.frame()
    r1.engine.frame_reader = delayed_capture
    r1.engine.poll()
    assert phase(r1, ident) == "UNKNOWN"
    assert r1.store.reserved_cash() == 5
    assert r1.store.account()["cash_balance"] == 995
    assert r1.store.open_trade_count() == 0
    assert r1.store.conn.execute("SELECT count(*) FROM paper_sim_consumed").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["source_ms", "received_ms"])
@pytest.mark.parametrize("invalid", ["missing", "future", "nan"])
def test_invalid_arrival_timestamp_is_unknown_not_zero_fill(r1, field, invalid):
    ident = r1.queue()
    r1.now += 1.1
    stamps = {"source_ms": r1.now*1000, "received_ms": r1.now*1000}
    stamps[field] = {"missing": None, "future": r1.now*1000+1, "nan": float("nan")}[invalid]
    arrival_book(r1, stamps)
    r1.engine.poll()
    assert phase(r1, ident) == "UNKNOWN"
    assert r1.store.reserved_cash() == 5
    assert r1.store.open_trade_count() == 0
    assert r1.store.recent_paper_orders(1)[0]["status"] == "PENDING"


def test_pre_arrival_limit_miss_cannot_be_labeled_zero_fill(r1):
    ident = r1.queue()
    order, = r1.engine.pending()
    arrival = order["submitted_at"]+order["arrival_delay"]
    r1.asks = [{"price": .90, "size": 1000.}]
    stamps = {"source_ms": arrival*1000-1, "received_ms": arrival*1000-1}
    arrival_book(r1, stamps)
    r1.now = arrival+.1
    r1.engine.poll()
    assert phase(r1, ident) == "IN_FLIGHT"
    assert r1.store.reserved_cash() == 5
    r1.now += .1
    stamps.update(source_ms=r1.now*1000, received_ms=r1.now*1000)
    r1.engine.poll()
    assert phase(r1, ident) == "CANCELED"
    assert r1.store.account()["cash_balance"] == 1000
    assert r1.store.reserved_cash() == 0


def test_arrival_consumption_and_confirmation_remain_idempotent(r1):
    ident = r1.queue()
    r1.asks = [{"price": .61, "size": 4.}]
    r1.advance(1.1)
    consumption = tuple(r1.store.conn.execute("SELECT * FROM paper_sim_consumed").fetchone())
    assert phase(r1, ident) == "CONFIRMING"
    # New receipt/source timestamps alone must not create another match.
    r1.advance(.1)
    assert tuple(r1.store.conn.execute("SELECT * FROM paper_sim_consumed").fetchone()) == consumption
    r1.reopen()
    r1.advance(11)
    r1.engine.poll()
    assert phase(r1, ident) == "PARTIAL"
    assert r1.store.open_trade_count() == 1
    assert r1.store.open_trades()[0]["shares"] == 2
    assert r1.store.conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 1
    assert tuple(r1.store.conn.execute("SELECT * FROM paper_sim_consumed").fetchone()) == consumption


@pytest.mark.parametrize("rule_state", ["fresh", "expired", "wrong_token", "wrong_tick"])
def test_ws_arrival_uses_only_fresh_token_bound_rules_without_mutating_frame(r1, rule_state):
    ident = r1.queue()
    r1.now += 1.1
    raw_frame = r1.frame()
    # LiveDepthBook.quote publishes depth and timestamps, not market rule fields.
    for quote in raw_frame.quotes.values():
        quote.pop("tick_size")
        quote.pop("min_order_size")
        quote["source"] = "clob-ws-live"
    original_quotes = deepcopy(raw_frame.quotes)
    rule = {"token_id": "up", "tick_size": "0.01", "min_order_size": 5.}
    if rule_state == "wrong_token":
        rule["token_id"] = "previous-round-token"
    if rule_state == "wrong_tick":
        rule["tick_size"] = "0.001"
    r1.engine.feed.rules = {"Up": rule}
    r1.engine.feed.rules_checked_at = r1.now-(60.001 if rule_state == "expired" else .1)
    r1.engine.frame_reader = lambda: raw_frame
    r1.engine.poll()
    assert raw_frame.quotes == original_quotes
    assert "tick_size" not in raw_frame.quotes["Up"]
    assert "min_order_size" not in raw_frame.quotes["Up"]
    assert r1.store.account()["cash_balance"] == 995
    assert r1.store.reserved_cash() == 5
    if rule_state == "fresh":
        assert phase(r1, ident) == "CONFIRMING"
        order, = r1.engine.pending()
        assert order["fill"]["shares"] > 0
        assert order["fill"]["price"] == pytest.approx(.61)
        assert r1.store.conn.execute(
            "SELECT 1 FROM main_r1_decisions WHERE order_id=? AND code='MATCHED'", (ident,)).fetchone()
    else:
        assert phase(r1, ident) == "UNKNOWN"
        assert r1.store.open_trade_count() == 0
        assert r1.store.conn.execute("SELECT count(*) FROM paper_sim_consumed").fetchone()[0] == 0
        assert r1.store.recent_paper_orders(1)[0]["status"] == "PENDING"


def run_socket_book(monkeypatch, received, *, strict=None, source=99_400.):
    """Run the actual collector for one buffered frame, with no sockets/repair IO."""
    monkeypatch.setattr(managed_live_book.time, "time", lambda: 100.)
    monkeypatch.setattr(managed_live_book.time, "monotonic", lambda: 1000.)
    stop, published, observed = Event(), [], []
    market = SimpleNamespace(round_id="r", ends_at=200., up_token="up", down_token="down")
    payload = {"event_type": "book", "asset_id": "up", "timestamp": source,
               "bids": [{"price": ".59", "size": "10"}],
               "asks": [{"price": ".61", "size": "10"}]}
    class Socket:
        last_received_ms = received
        reads = 0
        def send_text(self, text):
            pass
        def read_text(self, timeout):
            self.reads += 1
            if self.reads == 1:
                return json.dumps(payload)
            stop.set()
            return None
        def close(self):
            stop.set()
    def publish(current, quotes):
        published.append(quotes["Up"])
        stop.set()
        return True
    kwargs = {} if strict is None else {"strict_received_time": strict}
    books = managed_live_book.ReliableLiveBooks(lambda: market, publish, lambda: {}, None,
                                               connector=lambda *a: Socket(), **kwargs)
    books._repair_loop = lambda *a: None
    books.observer = lambda kind, data, stamp, **kw: observed.append((kind, stamp))
    books.run(stop)
    return books, published, observed


def test_strict_wire_receipt_is_preserved_in_published_book_and_audit(monkeypatch):
    books, published, observed = run_socket_book(monkeypatch, 99_500., strict=True)
    assert len(published) == 1
    assert published[0]["received_ms"] == 99_500.
    assert published[0]["source_ms"] == 99_400.
    assert ("BOOK", 99_500) in observed
    assert books.stats["reconnections"] == 0


@pytest.mark.parametrize("received", [None, True, "99500", float("nan"), float("inf"), 0., -1., 100_001.])
def test_strict_bad_receipt_never_falls_back_to_processing_time(monkeypatch, received):
    books, published, _ = run_socket_book(monkeypatch, received, strict=True)
    assert published == []
    assert books.stats["last_reason"] == "BOOK_RECEIVED_TIMESTAMP_INVALID"
    assert books.stats["reconnections"] == 1


def test_strict_buffered_old_receipt_is_not_refreshed_on_processing(monkeypatch):
    _, published, _ = run_socket_book(monkeypatch, 97_100., strict=True, source=97_010.)
    assert published == []


@pytest.mark.parametrize("received", [None, 99_500.])
def test_default_collector_keeps_legacy_processing_timestamp(monkeypatch, received):
    books, published, _ = run_socket_book(monkeypatch, received)
    assert books.strict_received_time is False
    assert len(published) == 1
    assert published[0]["received_ms"] == 100_000.


def test_only_r1_default_feed_opts_in_and_factory_keeps_two_arguments(monkeypatch, tmp_path):
    monkeypatch.setattr(trial_market_feed, "TrialReferences", Mock())
    exchange = SimpleNamespace(rest_book=Mock())
    legacy = trial_market_feed.TrialFeed(tmp_path, exchange)
    assert legacy.books.strict_received_time is False
    default_factory = inspect.signature(main_r1_paper.MainR1Runner).parameters["feed_factory"].default
    feed = default_factory(tmp_path, exchange)
    assert feed.books.strict_received_time is True
    assert len(inspect.signature(default_factory).parameters) == 2

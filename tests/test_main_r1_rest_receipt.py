"""R1 REST receipt provenance, using buffered fake responses and private ledgers."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_main_r1_execution as execution_helpers
from polybot2other import managed_live_client
from polybot2other.main_r1_paper import PublicExchange
from polybot2other.managed_live_client import ManagedExchange


def payload(source_ms):
    return {"asset_id": "up", "timestamp": source_ms, "tick_size": "0.01",
            "min_order_size": "5", "bids": [{"price": ".59", "size": "1000"}],
            "asks": [{"price": ".61", "size": "1000"}]}


def reader(raw, parse):
    exchange = object.__new__(ManagedExchange)
    exchange.settings = SimpleNamespace(clob_url="https://example.invalid")
    response = Mock()
    def decode():
        parse()
        return deepcopy(raw)
    response.json.side_effect = decode
    exchange.http = Mock()
    exchange.http.get.return_value = response
    return exchange


@pytest.mark.parametrize("strict", [False, True])
def test_receipt_opt_in_keeps_response_time_while_legacy_keeps_existing_policy(monkeypatch, strict):
    clock = [100.]
    monkeypatch.setattr(managed_live_client.time, "time", lambda: clock[0])
    exchange = reader(payload(99_900.), lambda: clock.__setitem__(0, 101.2))
    book = exchange.rest_book("up", strict_received_time=strict)
    assert book["received_ms"] == (100_000. if strict else 101_200.)
    assert book["source_ms"] == 99_900.
    assert book["asks"] == [{"price": .61, "size": 1000.}]
    exchange.http.get.assert_called_once_with("https://example.invalid/book", params={"token_id": "up"})


def test_slow_parsing_does_not_relax_source_freshness(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(managed_live_client.time, "time", lambda: clock[0])
    exchange = reader(payload(99_900.), lambda: clock.__setitem__(0, 103.))
    with pytest.raises(ValueError, match="盘口过期"):
        exchange.rest_book("up", strict_received_time=True)


def test_pre_arrival_rest_receipt_cannot_be_relabelled_by_slow_parsing():
    case = execution_helpers.MainR1ExecutionTests(methodName="runTest")
    try:
        case.setUp()
        ident = case.queue()
        order, = case.engine.pending()
        arrival = order["submitted_at"] + order["arrival_delay"]
        case.now = arrival - .05
        exchange = reader(payload((arrival-.1)*1000), lambda: setattr(case, "now", arrival+.1))
        public = object.__new__(PublicExchange)
        public._reader = exchange
        book = public.rest_book("up")
        assert book["received_ms"] < arrival*1000 < case.now*1000

        def capture():
            frame = case.frame()
            frame.quotes["Up"].update(book)
            return frame
        case.engine.frame_reader = capture
        case.engine.poll()
        waiting, = case.engine.pending()
        assert waiting["phase"] == "IN_FLIGHT"
        assert waiting["due_at"] == order["due_at"]
        assert case.store.reserved_cash() == 5
        assert case.store.open_trade_count() == 0

        # Only another real response, received after arrival, can unblock it.
        case.now = arrival+.2
        exchange.http.get.return_value.json.side_effect = lambda: payload((arrival-.1)*1000)
        book = public.rest_book("up")
        case.engine.poll()
        matched, = case.engine.pending()
        assert matched["phase"] == "CONFIRMING"
        assert matched["order_id"] == ident
        assert matched["arrival_book"]["source_ms"] < arrival*1000
        assert matched["arrival_book"]["received_ms"] == case.now*1000
        assert matched["matched_at"] == case.now
        assert exchange.http.get.call_count == 2
    finally:
        case.doCleanups()

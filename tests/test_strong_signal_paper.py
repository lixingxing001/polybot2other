from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_paper_execution as original_paper_tests
from polybot2other.first_strong_execution import FirstStrongExecution, SCORE_FLOOR
from polybot2other.strong_signal_paper import RULE, SETTINGS_FIELDS, StrongSignalPaper, campaign


def make_engine(case):
    return FirstStrongExecution(case.bot, "MAIN", case.settings, frame_reader=case.frame,
        info_reader=Mock(return_value=case.info), book_reader=case.reader, config_reader=lambda: case.config,
        feed=case.feed, policy=case.policy, clock=lambda: case.now)


@pytest.fixture
def case():
    fixture = original_paper_tests.PaperExecutionTests()
    fixture.setUp()
    try:
        fixture.config["strategies"]["MAIN"] = dict(RULE)
        fixture.engine = make_engine(fixture)
        yield fixture
    finally:
        fixture.doCleanups()


def test_uses_original_main_score_and_locks_first_fill(case):
    # Both scoring passes call the unmodified real MAIN evaluator.
    case.quote_changes["updated_at_ms"] = case.now * 1000
    case.engine.decide()
    order, = case.engine.pending()
    assert order["side"] == "Up" and order["signal"]["confidence"] >= SCORE_FLOOR
    assert order["amount"] == 5 and case.store.account()["cash_balance"] == 95
    assert order["decision_quotes"]["Up"]["token_id"] == "up"
    assert case.engine._entry_block(case.market, "Down", RULE)
    case.confirm()
    trade, = case.store.open_trades()
    assert trade["stake"] <= 5 and trade["shares"] > 0
    assert case.engine._entry_block(case.market, "Down", RULE)
    assert not case.engine.pending()


def test_both_confidence_checks_and_held_amount(case):
    high, low = case.signal, replace(case.signal, confidence=.6499)
    case.engine.signals.evaluate = Mock(return_value=(low, .5))
    case.engine.decide()
    assert not case.engine.pending()
    case.reader.assert_not_called()
    case.engine.signals.evaluate.side_effect = [(high, .5), (low, .5)]
    case.now += 4
    case.engine.decide()
    assert not case.engine.pending() and case.store.account()["cash_balance"] == 100
    case.now += 4
    case.engine.signals.evaluate.side_effect = [(high, .5), (replace(high, confidence=.65), .5)]
    case.engine.decide()
    order, = case.engine.pending()
    assert order["signal"]["confidence"] == .65
    assert case.engine._entry_block(case.market, "Down", RULE)


def test_pending_is_per_market_but_both_slots_count(case):
    initial = case.market
    case.queue(amount=5)
    case.market = replace(initial, round_id=initial.round_id+"b")
    case.queue(amount=5)
    assert case.store.account()["cash_balance"] == 90
    case.market = replace(initial, round_id=initial.round_id+"c")
    with pytest.raises(ValueError, match="两笔名额"):
        case.queue(amount=5)
    assert len(case.engine.pending()) == 2
    assert case.store.account()["cash_balance"] == 90


def test_partial_fill_and_confirming_restart_lock_both_sides(case):
    case.queue(amount=5)
    case.quote_changes["asks"] = [{"price":.4, "size":8}]
    case.advance(1.1)
    assert case.engine.pending()[0]["phase"] == "CONFIRMING"
    case.engine = make_engine(case)
    assert case.engine._entry_block(case.market, "Down", RULE)
    case.advance(10)
    order = case.store.conn.execute("SELECT status,fee,cash_spent FROM paper_orders").fetchone()
    assert order["status"] == "PARTIAL" and 0 < order["fee"] < order["cash_spent"] < 5
    assert case.engine._entry_block(case.market, "Down", RULE)
    case.store.settle_round_outcome(case.market.round_id, "Up", case.now + 350, settlement_source="polymarket_official")
    assert case.engine._entry_block(case.market, "Down", RULE)
    with pytest.raises(ValueError, match="只按官方结果结算"):
        case.engine.enqueue_sell()


def test_empty_order_releases_room_and_allows_retry(case):
    case.queue(amount=5)
    case.quote_changes["asks"] = [{"price":.8, "size":100}]
    case.advance(1.1)
    assert not case.engine.pending()
    assert case.store.account()["cash_balance"] == 100
    del case.quote_changes["asks"]
    assert case.queue(amount=5)
    assert case.store.account()["cash_balance"] == 95


def test_round_diagnostics_include_zero_order_market_and_restart(case):
    case.frame_error = "等待参考行情"
    case.engine.decide()
    db = case.store.conn
    row = db.execute("SELECT * FROM first_strong_rounds").fetchone()
    assert row["samples"] == 1 and row["fresh_samples"] == 0 and row["first_strong_at"] is None
    assert row["last_reason"] == "等待参考行情"
    case.engine = make_engine(case)
    case.frame_error = None
    low = replace(case.signal, confidence=.63)
    case.engine.signals.evaluate = Mock(return_value=(low, .5))
    case.now += 11
    case.engine.decide()
    case.engine.flush_round()
    row = db.execute("SELECT * FROM first_strong_rounds").fetchone()
    assert row["samples"] == 2 and row["fresh_samples"] == 1
    assert row["peak_entry_score"] == .63 and row["first_strong_at"] is None
    assert "0.65" in row["last_in_window_reason"]
    assert not case.engine.pending()
    case.engine.signals.evaluate = Mock(side_effect=[(case.signal, .5), (low, .5)])
    case.now += 4
    case.engine.decide()
    row = db.execute("SELECT * FROM first_strong_rounds").fetchone()
    assert row["first_strong_at"] == case.now and row["peak_entry_score"] == .8
    assert "低于 0.65" in row["last_reason"] and not case.engine.pending()


def test_official_only_and_snapshot_after_restart(case):
    case.queue(amount=5)
    case.confirm()
    case.now = case.market.ends_at + 1
    trial = object.__new__(StrongSignalPaper)
    trial.store, trial.engine = case.store, case.engine
    trial.resolver = Mock()
    trial.run = {"start_at": case.market.started_at}
    trial.feed = SimpleNamespace(capture=case.frame, last_error=None)
    trial.last_step, trial.interval, trial.errors = case.now, 2, {}
    trial.resolver.get_resolution.side_effect = [
        {"market_slug":"wrong", "price_source":"Gamma:outcomePrices", "outcome":"Up"},
        {"market_slug":case.market.round_id, "price_source":"other", "outcome":"Up"},
        {"market_slug":case.market.round_id, "price_source":"Gamma:outcomePrices", "outcome":"Up"},
    ]
    trial._settle()
    trial._settle()
    assert trial.snapshot()["account"]["open_positions"] == 1
    trial._settle()
    snapshot = trial.snapshot()
    assert snapshot["account"]["wins"] == snapshot["account"]["settled"] == 1
    assert snapshot["account"]["open_positions"] == 0
    assert snapshot["account"]["fees"] > 0
    assert snapshot["equity_curve"][-1]["equity"] == snapshot["account"]["equity_at_cost"]
    assert snapshot["account"]["cash"] < snapshot["account"]["equity_at_cost"]


def test_campaign_freezes_source_values_on_restart(case, tmp_path):
    cp = tmp_path / "source.json"
    s = {k: getattr(case.settings, k) for k in SETTINGS_FIELDS}
    cp.write_text(json.dumps({"settings":s, "policy":asdict(case.policy)}))
    frozen = campaign(tmp_path, cp)
    assert frozen["start_at"] % 300 == 0 and frozen["start_at"] >= case.now
    cp.unlink()
    assert campaign(tmp_path, cp) == frozen

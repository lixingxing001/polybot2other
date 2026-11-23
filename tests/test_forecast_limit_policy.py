"""The paired Paper accounts use the original limit, actual ledger risk and a common start."""
from dataclasses import replace

import pytest
import test_forecast_execution as fixtures

from polybot2other.experiments import FORECAST_LIMIT_060_PAPER_ID, FORECAST_LIMIT_CONTROL_PAPER_ID


@pytest.fixture
def trial(monkeypatch):
    harness = fixtures.ForecastExecutionTests()
    harness.setUp()
    monkeypatch.setenv("POLYBOT2OTHER_FORECAST_LIMIT_START_AT", str(int(harness.market.started_at)))
    try:
        yield harness
    finally:
        harness.doCleanups()


@pytest.mark.parametrize("strategy,expected", [
    (FORECAST_LIMIT_060_PAPER_ID, "WAIT"), (FORECAST_LIMIT_CONTROL_PAPER_ID, "BUY")])
def test_original_limit_excludes_ask_at_sixty_only_in_candidate(trial, strategy, expected):
    engine = trial.make_engine(trial.store, strategy)
    trial.bid, trial.ask = .59, .60
    engine.decide()
    assert engine.bot.last_decision["action"] == expected
    assert engine.bot.last_decision["limit"] == .61
    assert bool(engine.pending()) == (expected == "BUY")
    if expected == "WAIT":
        trial.bid, trial.ask = .58, .59
        engine.decide()
        order, = engine.pending()
        assert order["limit"] == .60
        assert order["forecast_decision"]["realized_drawdown_budget"] == 10


def test_candidate_rechecks_original_limit_before_submission(trial):
    engine = trial.make_engine(trial.store, FORECAST_LIMIT_060_PAPER_ID)
    trial.bid, trial.ask = .58, .59
    def later_frame():
        trial.bid, trial.ask = .59, .60
        return trial.frame()
    engine.frame_reader = later_frame
    engine.decide()
    assert not engine.pending()
    assert trial.store.account()["cash_balance"] == 100
    assert engine.bot.last_decision["limit"] == .61
    assert engine.bot.last_decision["action"] == "WAIT"


@pytest.mark.parametrize("strategy", [FORECAST_LIMIT_060_PAPER_ID, FORECAST_LIMIT_CONTROL_PAPER_ID])
def test_first_partial_market_is_excluded(trial, monkeypatch, strategy):
    start = int(trial.market.ends_at)
    monkeypatch.setenv("POLYBOT2OTHER_FORECAST_LIMIT_START_AT", str(start))
    engine = trial.make_engine(trial.store, strategy)
    engine.decide()
    assert not engine.pending()
    assert "完整市场" in engine.last_event["message"]
    assert trial.store.conn.execute("SELECT count(*) FROM forecast_checkpoints").fetchone()[0] == 0
    trial.market = replace(trial.market, round_id="next", started_at=start, ends_at=start + 300)
    assert engine._entry_block(trial.market, "Up", trial.rule) is None


@pytest.mark.parametrize("strategy", [FORECAST_LIMIT_060_PAPER_ID, FORECAST_LIMIT_CONTROL_PAPER_ID])
def test_cross_day_peak_survives_restart_and_includes_open_dust(trial, strategy):
    store = trial.store
    engine = trial.make_engine(store, strategy)
    with store.conn:
        store.conn.execute("UPDATE account SET realized_pnl=12")
        store.conn.execute("UPDATE account SET realized_pnl=7")
    assert engine._trial_entry_block(trial.market, 5) is None
    with store.conn:
        store.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,confidence,
            move_bps,status,opened_at,reason) VALUES(?,'BTC','Up',0.002,0.50,0.004,0.8,1,'OPEN',?,'dust')""",
            (trial.market.round_id, trial.now))
    assert not store.open_trades()
    assert "回撤额度" in engine._trial_entry_block(trial.market, 5)
    with store.conn:
        store.conn.execute("UPDATE account SET realized_pnl=6")
    trial.now += 86401
    restarted = trial.make_engine(store, strategy)
    assert "回撤额度" in restarted._trial_entry_block(trial.market, 5)
    assert store.conn.execute("SELECT pnl FROM forecast_realized_peak").fetchone()[0] == 12


def test_buy_budget_remains_reserved_until_fill_confirmation(trial):
    engine = trial.make_engine(trial.store, FORECAST_LIMIT_060_PAPER_ID)
    engine.decide()
    order, = engine.pending()
    assert order["amount"] == 5
    with trial.store.conn:
        trial.store.conn.execute("UPDATE account SET realized_pnl=-0.1")
    assert "回撤额度" in engine._trial_entry_block(trial.market, 5)
    trial.now += 1.1
    engine.poll()
    assert engine.pending()[0]["phase"] == "CONFIRMING"
    assert "回撤额度" in engine._trial_entry_block(trial.market, 5)
    trial.now += 3
    engine.poll()
    assert not engine.pending()
    stake = trial.store.open_trades()[0]["stake"]
    assert engine._trial_entry_block(trial.market, 9.9-stake) is None
    assert "回撤额度" in engine._trial_entry_block(trial.market, 9.91-stake)


def test_drawdown_rechecked_at_queue_and_flat_stop_persists(trial, monkeypatch):
    engine = trial.make_engine(trial.store, FORECAST_LIMIT_060_PAPER_ID)
    original_queue = engine._queue
    def queue_after_loss(*args, **kwargs):
        with trial.store.conn:
            trial.store.conn.execute("UPDATE account SET realized_pnl=-6")
        return original_queue(*args, **kwargs)
    monkeypatch.setattr(engine, "_queue", queue_after_loss)
    engine.decide()
    assert not engine.pending()
    assert "回撤额度" in engine.last_event["message"]
    assert trial.store.account()["cash_balance"] == 100
    with trial.store.conn:
        trial.store.conn.execute("UPDATE account SET realized_pnl=0")
    restarted = trial.make_engine(trial.store, FORECAST_LIMIT_060_PAPER_ID)
    assert "已结束" in restarted._entry_block(trial.market, "Up", trial.rule)


def test_drawdown_does_not_stop_exits(trial):
    engine = trial.make_engine(trial.store, FORECAST_LIMIT_060_PAPER_ID)
    trial.engine = engine
    trial.buy()
    with trial.store.conn:
        trial.store.conn.execute("UPDATE account SET realized_pnl=-6")
    trial.spot, trial.bid, trial.ask = 98, .32, .34
    trial.now += 1
    engine.decide()
    assert engine.pending()[0]["action"] == "SELL"

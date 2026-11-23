from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from polybot2other.main_r1_cohort import CohortCollector, make_history_reader
from polybot2other.main_r1_execution import CANDIDATES
from polybot2other.models import MarketRound
from polybot2other.optimized_reference import SPOT
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.storage import TradeStore


@pytest.fixture
def case(tmp_path):
    now = [1800000000.]
    market = MarketRound("btc-updown-5m-1800000000", "BTC", now[0], now[0]+300, 100., up_token="u", down_token="d")
    stores = []
    def store(name):
        result = TradeStore(tmp_path/(name+".sqlite3"), 1000.)
        result.conn.execute("CREATE TABLE paper_sim_orders(order_id INTEGER PRIMARY KEY, phase TEXT, due_at REAL, payload TEXT)")
        result.conn.commit()
        result.upsert_round(market)
        stores.append(result)
        return result
    observations = []
    selector = SimpleNamespace(
        choose=lambda frame: {"round_id": frame.market.round_id, "regime": "trend", "decision_at": market.started_at},
        observe=lambda cohort: observations.append((now[0], cohort)))
    shadows = {key: SimpleNamespace(store=store(key), lock=RLock()) for key in CANDIDATES}
    collector = CohortCollector(store("portfolio"), shadows, selector, clock=lambda: now[0], start_at=now[0])
    frame = lambda: MarketInputs(market, {}, {}, {}, now[0], str(now[0]), None)
    def cycle(at, success=True):
        now[0] = market.started_at+at
        collector.observe_cycle(frame(), {key: success for key in CANDIDATES})
    def cover():
        for at in range(10, 286, 5):
            cycle(at)
    def resolve():
        now[0] = market.ends_at+1
        return collector.record_resolution(market.round_id,
            {"market_slug": market.round_id, "price_source": "Gamma:outcomePrices", "outcome": "Up"})
    result = SimpleNamespace(now=now, market=market, stores=stores, shadows=shadows,
        collector=collector, observations=observations, frame=frame, cycle=cycle, cover=cover, resolve=resolve)
    yield result
    for result in stores:
        result.conn.close()


def add_order(case, key, *, status, shares=0., cost=0., trade_id=None, phase=None):
    store = case.shadows[key].store
    with store.conn:
        ident = store._insert_paper_order(market=case.market, side="Up", order_type="FAK", status=status,
            limit_price=.65, post_only=False, expires_at=None, requested_cash=5.,
            reserved_cash=5. if status == "PENDING" else 0., remaining_cash=5. if status == "PENDING" else 0.,
            filled_shares=shares, avg_fill_price=.6 if shares else None, notional=cost,
            fee=0., cash_spent=cost, trade_id=trade_id, confidence=.8, move_bps=10.,
            reason="fixture", now=case.market.started_at+50)
        store.conn.execute("INSERT INTO paper_sim_orders VALUES(?,?,?,?)", (ident, phase or status, case.market.started_at+51, "{}"))
    return ident


def test_complete_zero_trade_round_is_learned_and_not_repeated(case):
    case.cover()
    assert case.resolve()
    assert case.collector.flush_completed() == 1
    at, cohort = case.observations[0]
    assert at > case.market.ends_at
    assert "available_at" not in cohort
    assert set(cohort["arms"]) == set(CANDIDATES)
    assert all(a["pnl"] == 0 and a["status"] == "NO_TRADE" for a in cohort["arms"].values())
    assert case.collector.flush_completed() == 0
    assert len(case.observations) == 1


def test_missing_any_candidate_cycle_excludes_whole_cohort(case):
    case.cover()
    with case.collector.store.conn:
        case.collector.store.conn.execute("UPDATE main_r1_cohort_coverage SET failed_cycles=1")
    case.resolve()
    assert case.collector.flush_completed() == 0
    assert case.collector.snapshot()["counts"]["EXCLUDED"] == 1
    assert not case.observations


@pytest.mark.parametrize("times", [(40, 285), (10, 200), (10, 285)])
def test_late_start_early_finish_or_large_gap_cannot_become_zero_reward(case, times):
    for at in times:
        case.cycle(at)
    case.resolve()
    assert case.collector.flush_completed() == 0
    assert case.collector.snapshot()["counts"]["EXCLUDED"] == 1


def test_no_official_resolution_no_training(case):
    case.cover()
    case.now[0] = case.market.ends_at+10
    assert not case.collector.record_resolution(case.market.round_id,
        {"market_slug": case.market.round_id, "price_source": "estimate", "outcome": "Up"})
    assert case.collector.due_rounds() == [case.market.round_id]
    assert case.collector.flush_completed() == 0


def test_unknown_pending_arm_is_never_treated_as_no_trade(case):
    case.cover()
    add_order(case, "FAST", status="PENDING", phase="UNKNOWN")
    case.resolve()
    assert case.collector.flush_completed() == 0
    assert case.collector.snapshot()["counts"]["WAITING_ORDERS"] == 1
    assert not case.observations


def test_positive_fill_uses_actual_requested_budget_not_zero_fill_retry_sum(case):
    case.cover()
    store = case.shadows["MAIN"].store
    add_order(case, "MAIN", status="CANCELED")
    with store.conn:
        trade_id = store.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,
            confidence,move_bps,status,opened_at,settled_at,payout,pnl,settlement_source,reason)
            VALUES(?,'BTC','Up',4.9,.6125,8,.8,10,'SETTLED',?,?,8,3.1,'polymarket_official','fixture')""",
            (case.market.round_id, case.market.started_at+50, case.market.ends_at+1)).lastrowid
    add_order(case, "MAIN", status="FILLED", shares=8, cost=4.9, trade_id=trade_id)
    case.resolve()
    assert case.collector.flush_completed() == 1
    arm = case.observations[0][1]["arms"]["MAIN"]
    assert arm == {"status": "SETTLED", "requested_budget": 5., "cost": 4.9, "shares": 8., "pnl": 3.1}


def test_conflicting_official_resolution_cannot_relabel_history(case):
    case.cover()
    case.resolve()
    with pytest.raises(ValueError, match="Conflicting"):
        case.collector.record_resolution(case.market.round_id,
            {"market_slug": case.market.round_id, "price_source": "Gamma:outcomePrices", "outcome": "Down"})


def test_history_reader_filters_future_receipt_source_and_backfill():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reference_ticks(channel TEXT, source_ms REAL, received_ms REAL, price REAL, snapshot INTEGER)")
    conn.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,?)", [
        (SPOT, 1000, 1001, 100., 0), (SPOT, 1002, 2000, 101., 0),
        (SPOT, 2000, 1000, 102., 0), (SPOT, 1003, 1004, 103., 1),
        ("other", 1000, 1001, 100., 0)])
    reader = make_history_reader(SimpleNamespace(conn=conn, lock=RLock()))
    assert reader(900, 1500) == [{"source_ms": 1000., "received_ms": 1001., "price": 100.}]
    conn.close()


def test_unknown_regime_is_preserved_and_excluded_without_fake_training(case):
    case.collector.selector.choose = lambda frame: {"round_id": frame.market.round_id,
        "regime": None, "decision_at": frame.observed_at+.01,
        "features": {"feature_time": frame.observed_at}}
    case.cover()
    case.resolve()
    assert case.collector.flush_completed() == 0
    assert case.collector.snapshot()["counts"]["EXCLUDED"] == 1
    assert not case.observations


def test_feature_time_is_input_cutoff_not_later_selector_clock(case):
    case.collector.selector.choose = lambda frame: {"round_id": frame.market.round_id,
        "regime": "trend", "decision_at": frame.observed_at+.01,
        "features": {"feature_time": frame.observed_at}}
    case.cycle(10)
    row = case.collector.store.conn.execute("SELECT feature_time FROM main_r1_cohort_coverage").fetchone()
    assert row[0] == case.market.started_at+10

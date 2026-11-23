"""Offline forward smoke for the complete MAIN-R1 Paper system.

Use real runner, execution, risk, selector and cohort code with an explicit fake
public feed/exchange/resolver. No workers or funded/production APIs are started.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace
import time
from unittest.mock import Mock

import pytest

from polybot2other import main_r1_paper as runner_module
from polybot2other.main_r1_execution import CANDIDATES
from polybot2other.models import MarketRound, Signal
from polybot2other.optimized_reference import SPOT
from polybot2other.shared_market_inputs import MarketInputs


@pytest.fixture
def forward_case(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1800000000.)
    monkeypatch.setattr(time, "time", lambda: clock.now)
    base = clock.now
    source = tmp_path / "input"
    (source / "live-accounts").mkdir(parents=True)
    with sqlite3.connect(source / runner_module.SOURCE_FILES[0]) as conn:
        conn.execute("CREATE TABLE market_rounds(round_id,condition_id,up_token,down_token,target_price)")
    with sqlite3.connect(source / runner_module.SOURCE_FILES[1]) as conn:
        conn.execute("CREATE TABLE reference_ticks(channel,source_ms,received_ms,price,snapshot)")
        conn.execute("CREATE TABLE reference_validation(verified)")
    source_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*.sqlite3")}
    config_path = Path(__file__).resolve().parents[1] / "deploy/main-r1-paper/main-r1.example.json"
    config = json.loads(config_path.read_text())

    class FakePublicExchange:
        def __init__(self, settings):
            assert not settings.live_trading_runtime_enabled
            assert not settings.managed_live_enabled
            self.closed = False

        def market_info(self, market, force=False):
            return {"fee_rate": .05, "fee_exponent": 1, "tick_size": "0.01"}

        def rest_book(self, token):
            raise AssertionError("Final-frame Paper path must not need a trading or REST book client here")

        def close(self):
            self.closed = True

    class FakeFeed:
        def __init__(self, source, exchange):
            self.ready = True
            self.frame_lag = .025  # Distinguish captured feature time from decision clock.
            self.depth = 1000.
            self.last_error = None
            self.rules, self.rules_checked_at = {}, 0.
            self.books = SimpleNamespace(health=lambda: {"ws_state": "fake-open"})
            refs = sqlite3.connect(":memory:")
            refs.execute("CREATE TABLE reference_ticks(channel,source_ms,received_ms,price,snapshot)")
            refs.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,?)", [
                (SPOT, (base+i)*1000, (base+i)*1000, 100+i*.00001, 0)
                for i in range(-700, 7201)
            ])
            refs.commit()
            self.references = SimpleNamespace(conn=refs, lock=RLock(), points=self.points)
            self.closed = False

        def points(self, channel, start, end):
            return self.references.conn.execute(
                "SELECT source_ms,price FROM reference_ticks WHERE channel=? AND source_ms BETWEEN ? AND ?",
                (channel, start, end)).fetchall()

        def capture(self):
            start = int(clock.now)//300*300
            market = MarketRound(f"btc-updown-5m-{start}", "BTC", float(start), float(start+300), 100.,
                                 condition_id=f"condition-{start}", up_token=f"up-{start}", down_token=f"down-{start}")
            observed = clock.now-self.frame_lag
            quotes = {}
            for side, token in (("Up", market.up_token), ("Down", market.down_token)):
                quotes[side] = {"token_id": token, "best_ask": .61, "best_bid": .59,
                                "asks": [{"price": .61, "size": self.depth}],
                                "bids": [{"price": .59, "size": 1000.}],
                                "source_ms": observed*1000, "received_ms": observed*1000,
                                "tick_size": "0.01", "min_order_size": 5.}
            reference = {"ready": self.ready, "protocol_verified": self.ready,
                         "latest": {SPOT: {"price": 100.15, "source_ms": observed*1000}}}
            return MarketInputs(market, reference,
                                {"chainlink": 100.15, "chainlink_updated_ms": observed*1000},
                                quotes, observed, f"fake-{observed}", None if self.ready else "reference unavailable")

        def start(self):
            raise AssertionError("The smoke test never starts network/worker threads")

        def close(self):
            if not self.closed:
                self.references.conn.close()
                self.closed = True

    class FakeResolver:
        def __init__(self):
            self.available = True
            self.calls = []

        def get_resolution(self, round_id):
            self.calls.append(round_id)
            if not self.available:
                return None
            return {"market_slug": round_id, "price_source": "Gamma:outcomePrices",
                    "outcome": "Up", "final_price": 100.15, "target_price": 100.}

    monkeypatch.setattr(runner_module, "StrictTwapMarketClient", lambda *args: FakeResolver())
    instances = []

    def build(*, traded=False):
        instance = runner_module.MainR1Runner(
            source, tmp_path / "experiment", deepcopy(config), clock=lambda: clock.now,
            feed_factory=FakeFeed, exchange_factory=FakePublicExchange,
        )
        instances.append(instance)
        for engine in instance.engines.values():
            def evaluate(family, rule, market, reference, quotes, now, info, *, market_input=None):
                if traded:
                    return Signal("BTC", "Up", .9, .61, 15., "offline fixture"), .72
                return Signal("BTC", "NO_TRADE", 0., 0., 0., "fixture abstention"), None
            engine.signals.evaluate = Mock(side_effect=evaluate)
        return instance

    def cover(instance, *, partial=False, fail_once=None, start_offset=17):
        for offset in range(start_offset, 282, 2):
            clock.now = base+offset
            instance.feed.depth = 1000.
            if fail_once is not None and offset == start_offset:
                engine = instance.shadows[fail_once]
                original = engine.info_reader
                engine.info_reader = Mock(side_effect=ValueError("metadata temporarily unavailable"))
                try:
                    instance._drive_once()
                    assert engine.last_observation_valid is False
                finally:
                    engine.info_reader = original
            else:
                instance._drive_once()
            clock.now = base+offset+1.1
            instance.feed.depth = 4. if partial else 1000.
            for engine in instance.engines.values():
                engine.poll()
        instance.feed.depth = 1000.

    def resolve(instance, *, at=None):
        clock.now = base+301. if at is None else at
        return instance._settle_once()

    case = SimpleNamespace(clock=clock, base=base, build=build, cover=cover, resolve=resolve,
                           round_id=f"btc-updown-5m-{int(base)}", source_hashes=source_hashes)
    yield case
    for instance in reversed(instances):
        instance.close()
    assert source_hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_hashes}


def test_cold_portfolio_remains_flat_while_complete_zero_trade_shadows_keep_learning(forward_case):
    case = forward_case
    instance = case.build()
    case.cover(instance)
    assert instance.portfolio.store.paper_order_count() == 0
    assert all(engine.store.paper_order_count() == 0 for engine in instance.shadows.values())
    assert instance.selector.status()["cohorts"] == 0
    case.resolve(instance)
    assert case.round_id in instance.resolver.calls  # Resolution is requested even without a trade.
    assert instance.selector.status()["cohorts"] == 1
    assert instance.cohorts.snapshot()["counts"].get("LEARNED") == 1
    row = instance.portfolio.store.conn.execute("SELECT * FROM main_r1_selector_cohorts").fetchone()
    cohort = json.loads(row["payload_json"])
    assert set(cohort["arms"]) == set(CANDIDATES)
    assert all(arm["status"] == "NO_TRADE" and arm["pnl"] == 0 for arm in cohort["arms"].values())
    assert row["available_at"] == case.clock.now
    decision = instance.selector.status()["last_decision"]
    assert row["feature_time"] == decision["features"]["feature_time"]
    assert row["feature_time"] < decision["decision_at"]  # Captured frame time is not decision time.
    assert decision["candidate_key"] is None


def test_forward_partial_fills_use_actual_cost_but_requested_budget_denominator(forward_case):
    case = forward_case
    instance = case.build(traded=True)
    case.cover(instance, partial=True)
    assert instance.portfolio.store.paper_order_count() == 0  # Cold start is not a fallback to MAIN.
    for engine in instance.shadows.values():
        trade, = engine.store.open_trades()
        assert trade["shares"] == 2.
        assert engine.store.recent_paper_orders(1)[0]["status"] == "PARTIAL"
    case.resolve(instance)
    assert instance.selector.status()["cohorts"] == 1
    row = instance.portfolio.store.conn.execute("SELECT payload_json FROM main_r1_selector_cohorts").fetchone()
    cohort = json.loads(row[0])
    for name, arm in cohort["arms"].items():
        assert arm["requested_budget"] == 5.
        assert arm["shares"] == 2.
        assert arm["cost"] == pytest.approx(2*.61 + round(2*.05*.61*.39, 6))
        assert arm["pnl"] == pytest.approx(2.-arm["cost"])
        assert instance.shadows[name].pending_cash() == 2.
    case.clock.now += 61.
    for engine in instance.engines.values():
        engine.poll()
    status = instance.snapshot()
    assert status["portfolio"]["cash_balance"] == 1000.
    assert len(status["shadows"]) == 4
    assert all(row["cash_balance"] == pytest.approx(1000.+cohort["arms"][row["key"]]["pnl"])
               for row in status["shadows"])
    assert all(engine.pending_cash() == 0 for engine in instance.shadows.values())


def test_engine_caught_metadata_failure_excludes_common_cohort_instead_of_fake_zero_reward(forward_case):
    case = forward_case
    instance = case.build()
    case.cover(instance, fail_once="FAST")
    case.resolve(instance)
    assert instance.cohorts.snapshot()["counts"].get("EXCLUDED") == 1
    assert instance.selector.status()["cohorts"] == 0
    assert all(engine.store.paper_order_count() == 0 for engine in instance.shadows.values())


def test_initial_unavailable_regime_is_visible_exclusion_and_does_not_crash_later_cycles(forward_case):
    case = forward_case
    instance = case.build()
    references = instance.feed.references.conn
    ticks = references.execute("SELECT * FROM reference_ticks").fetchall()
    references.execute("DELETE FROM reference_ticks")
    references.commit()
    case.clock.now = case.base+17.
    instance._drive_once()
    assert instance.selector.status()["last_decision"]["reason"] == "features_unavailable"
    references.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,?)", ticks)
    references.commit()
    # Restored history cannot retroactively rewrite the observed unavailable regime.
    case.cover(instance)
    case.resolve(instance)
    assert instance.selector.status()["cohorts"] == 0
    assert instance.cohorts.snapshot()["counts"].get("EXCLUDED") == 1


def test_entry_window_and_unready_feed_wait_without_freezing_the_round_choice(forward_case):
    case = forward_case
    instance = case.build()
    case.clock.now = case.base+5.
    instance._drive_once()
    assert instance.selector.status()["decisions"] == 0
    instance.feed.ready = False
    case.clock.now = case.base+17.
    instance._drive_once()
    assert instance.selector.status()["decisions"] == 0
    assert all(engine.store.paper_order_count() == 0 for engine in instance.engines.values())
    instance.feed.ready = True
    case.cover(instance, start_offset=19)
    case.resolve(instance)
    assert instance.selector.status()["decisions"] == 1
    assert instance.selector.status()["cohorts"] == 1
    assert instance.selector.status()["last_decision"]["regime"] is not None


def test_long_feed_readiness_gap_excludes_the_missed_common_window(forward_case):
    case = forward_case
    instance = case.build()
    instance.feed.ready = False
    case.clock.now = case.base+17.
    instance._drive_once()
    case.clock.now = case.base+35.
    instance._drive_once()
    assert instance.selector.status()["decisions"] == 0
    instance.feed.ready = True
    case.cover(instance, start_offset=41)
    case.resolve(instance)
    assert instance.selector.status()["decisions"] == 1
    assert instance.selector.status()["cohorts"] == 0
    assert instance.cohorts.snapshot()["counts"].get("EXCLUDED") == 1


def test_result_first_available_after_restart_is_never_backdated_to_market_end(forward_case):
    case = forward_case
    instance = case.build()
    case.cover(instance)
    instance.resolver.available = False
    case.resolve(instance)
    assert instance.selector.status()["cohorts"] == 0
    decision_before = instance.selector.status()["last_decision"]
    instance.close()
    case.clock.now = case.base+420.
    resumed = case.build()
    assert resumed.selector.status()["cohorts"] == 0
    assert resumed.selector.status()["last_decision"] == decision_before
    case.resolve(resumed, at=case.clock.now)
    assert resumed.selector.status()["cohorts"] == 1
    row = resumed.portfolio.store.conn.execute("SELECT * FROM main_r1_selector_cohorts").fetchone()
    assert row["ends_at"] == case.base+300.
    assert row["available_at"] == case.base+420.
    assert row["available_at"] > row["ends_at"]
    assert resumed.selector.status()["last_decision"] == decision_before

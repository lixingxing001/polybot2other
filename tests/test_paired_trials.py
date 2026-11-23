import io
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from polybot2other.config import Settings
from polybot2other.models import MarketRound
from polybot2other.optimized_reference import ReferenceCache, SPOT, TWAP
from polybot2other.paired_trials import ARMS, FAST, FORECAST, Arm, PairedTrials, campaign, compare
from polybot2other.shared_market_inputs import reference_error
from polybot2other.storage import TradeStore
from polybot2other.trial_market_feed import TrialFeed, TrialReferences, readonly


def market_at(start):
    return MarketRound(f"btc-updown-5m-{start}", "BTC", start, start + 300, 100.,
                       condition_id="condition", up_token="up", down_token="down")


def test_matched_net_requires_same_side_quantity_cost_cycle_and_both_closed():
    entry = ("Up", 5., 8.1, 71)
    baseline = {k: [entry] for k in ("same", "direction", "size", "cost", "cycle", "missing",
                                     "unsettled", "extra_fill", "control_only")}
    trial = {**baseline, "direction": [("Down", *entry[1:])], "size": [("Up", 5., 8., 71)],
             "cost": [("Up", 4.99, 8.1, 71)], "cycle": [(*entry[:3], 72)],
             "missing": [(*entry[:3], None)], "extra_fill": [entry, entry], "variant_only": [entry]}
    baseline["missing"] = trial["missing"]
    del trial["control_only"]
    control_closed = dict.fromkeys(baseline, -2.)
    trial_closed = dict.fromkeys(trial, 4.)
    del trial_closed["unsettled"]
    control_closed["same"], trial_closed["same"] = -.07, .36

    result = compare("forecast", ({}, baseline, control_closed), ({}, trial, trial_closed))
    assert result == dict(pair="forecast", matched=1, control_pnl=-.07, variant_pnl=.36,
                          control_only=1, variant_only=1, different_entries=6)


def add_order(arm, market, amount, filled, cost):
    with arm.store.conn as conn:
        conn.execute("""INSERT INTO paper_orders(round_id,symbol,side,order_type,status,requested_cash,
                        filled_shares,cash_spent,reason,created_at,updated_at)
                        VALUES(?,'BTC','Up','FAK','FILLED',?,?,?,'test',1,1)""",
                     (market.round_id, amount, filled, cost))


def add_trade(arm, market, status, gain):
    with arm.store.conn as conn:
        conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,confidence,
                        move_bps,status,opened_at,pnl,reason)
                        VALUES(?,'BTC','Up',5,.5,10,.8,3,?,1,?,'test')""",
                     (market.round_id, status, gain))


@pytest.fixture
def arms(tmp_path):
    feed = Mock()
    run = {"accounts": {key: {"rule": {"stake_dollars": stake}, "policy": {}}
                        for key, stake in ((FAST, 10), (FORECAST, 5))}}
    arms = [Arm(tmp_path, spec, Settings(), run, feed, threading.Event()) for spec in ARMS]
    try:
        yield arms
    finally:
        for arm in arms:
            arm.store.conn.close()


def test_four_isolated_wallets_and_paired_entry_report(arms):
    a, b, c, d = arms
    assert len({arm.store.conn.execute('PRAGMA database_list').fetchone()[2] for arm in arms}) == 4
    assert {arm.engine.strategy_id for arm in arms} == {FAST, FORECAST}
    assert [arm.rule["stake_dollars"] for arm in arms] == [10, 10, 5, 5]
    assert [arm.report()[0]["cash"] for arm in arms] == [100.] * 4
    a.store.rebase_initial_balance(82)
    assert [arm.report()[0]["cash"] for arm in arms] == [82., 100., 100., 100.]

    market = market_at(300)
    frame = SimpleNamespace(market=market)

    def buy_and_exit(arm):
        def decide():
            assert arm.bot._sync_shared_market_inputs() is frame
            add_order(arm, market, 5., 10., 5.)
            add_order(arm, market, 0., 2., 0.)  # Sell shares do not represent another entry.
        return decide

    for arm in (c, d):
        arm.engine.decide = buy_and_exit(arm)
        arm.decide(frame, 123)
        assert arm.bot.first_frame is None
        add_trade(arm, market, "SETTLED", .2)
    add_trade(d, market, "OPEN", None)
    summary, entry, _ = d.report()
    assert entry == {market.round_id: [("Up", 5., 10., 123)]}
    assert (summary["submitted"], summary["buy_fills"], summary["closed_markets"]) == (1, 1, 0)
    assert compare("forecast", c.report(), d.report())["matched"] == 0
    with d.store.conn as db:
        db.execute("UPDATE trades SET status='SETTLED',pnl=-.3 WHERE status='OPEN'")
    result = compare("forecast", c.report(), d.report())
    assert (result["matched"], result["control_pnl"], result["variant_pnl"]) == (1, .2, -.1)
    assert all(arm.report()[0]["buy_fills"] == 0 for arm in (a, b))


def test_reference_freshness_from_both_live_channels_and_verified_source(tmp_path):
    path = tmp_path / "with space?#.sqlite3"
    cache = ReferenceCache(path)
    reader = TrialReferences(path)
    try:
        now = 900.
        ms = int(now * 1000)

        def tick(channel, source_age, receive_age, snapshot):
            with cache.conn as db:
                db.execute("INSERT INTO reference_ticks VALUES(?,?,?,?,?,?)",
                           (channel, ms - source_age, 100., "100", ms - receive_age, snapshot))

        tick(SPOT, 4000, 2500, 0)
        tick(TWAP, 200, 200, 1)  # Snapshot alone cannot establish live readiness.
        tick(TWAP, -10, 0, 0)    # Future source.
        tick(TWAP, 10, -1, 0)    # Unreceived at decision time.
        assert not reader.snapshot(now)["ready"]
        tick(TWAP, 400, 100, 0)
        state = reader.snapshot(now)
        assert state["ready"] and not state["protocol_verified"]
        assert state["latest"][TWAP]["source_ms"] == ms - 400
        assert reference_error(market_at(900), state, now)
        with cache.conn as db:
            db.execute("INSERT INTO reference_validation VALUES('r',100,100,0,1,?)", (now,))
        assert reference_error(market_at(900), reader.snapshot(now), now) is None
        assert not reader.snapshot(now + .002)["ready"]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.conn.execute("DELETE FROM reference_ticks")
    finally:
        reader.conn.close()
        cache.conn.close()
    missing = tmp_path / "not_created.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        readonly(missing)
    assert not missing.exists()


def test_quotes_bound_to_market_side_and_source_time(tmp_path, monkeypatch):
    cache = ReferenceCache(tmp_path / "live-accounts/market.references.sqlite3")
    cache.conn.close()
    feed = TrialFeed(tmp_path, Mock())
    try:
        monkeypatch.setattr("polybot2other.trial_market_feed.time.time", lambda: 450.)
        m = market_at(300)
        feed.market = m
        up = dict(token_id="up", source_ms=450000, received_ms=450000, best_bid=.52, best_ask=.53)
        assert not feed.publish(replace(m, round_id="other"), {"Up": up})
        feed.publish(m, {"Down": up})
        assert feed.get_quotes() == {}
        feed.publish(m, {"Up": up})
        feed.publish(m, {"Up": {**up, "source_ms": 449999, "best_bid": .51}})
        assert feed.get_quotes()["Up"] == up
        frame = feed.capture()
        assert set(frame.valid_quotes(450.)) == {"Up"}
        assert not frame.valid_quotes(453.)
        monkeypatch.setattr("polybot2other.trial_market_feed.time.time", lambda: 600.)
        assert not feed.publish(m, {"Up": up})
        assert feed.observation()[0] is None
    finally:
        feed.close()


@pytest.mark.parametrize("overrides,settles", [
    ({"market_slug": "another-market"}, False),
    ({"price_source": "local-spot"}, False),
    ({"outcome": "unknown"}, False),
    ({}, True),
])
def test_settles_only_with_same_market_official_outcome(arms, overrides, settles):
    m = market_at(300)
    a = arms[0]
    a.store.upsert_round(m)
    add_trade(a, m, "OPEN", None)
    resolver = Mock()
    resolver.get_resolution.return_value = {"market_slug": m.round_id, "outcome": "Up",
                                            "price_source": "Gamma:outcomePrices", "target_price": 100.,
                                            **overrides}
    service = PairedTrials.__new__(PairedTrials)
    service.resolver, service.arms = resolver, arms
    service.stop = Mock()
    service.stop.is_set.side_effect = (False, True)
    service._settle()

    resolver.get_resolution.assert_called_once_with(m.round_id)
    assert service.settlement_error is None
    summary, _, closed = a.report()
    assert closed == ({m.round_id: 5.} if settles else {})
    assert summary["realized"] == (5. if settles else 0.)
    assert all(arm.report()[0]["realized"] == 0. for arm in arms[1:])


def test_campaign_persists_initial_rules_ignoring_other_wallets_and_live_settings(tmp_path, monkeypatch):
    accounts = [{"strategy_id": key, "rules": {"stake_dollars": stake}, "policy": {"depth_fraction": .5}}
                for key, stake in (("MAIN", 17), (FAST, 10), ("MAIN_OPTIMIZED", 17), (FORECAST, 5))]
    payload = {"runtime": {"paper_execution": {"accounts": accounts}},
               "settings": {"tick_seconds": 2., "managed_live_enabled": True, "db_path": "original.sqlite3"}}
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr("polybot2other.paired_trials.urllib.request.build_opener", lambda *_: opener)
    monkeypatch.setattr("polybot2other.paired_trials.time.time", lambda: 601.)
    monkeypatch.setenv("POLYBOT2OTHER_MIN_PRICE_DISTANCE_BPS", "3.5")
    frozen = campaign(tmp_path)
    assert frozen["start_at"] == 900
    assert frozen["settings"] == {"tick_seconds": 2., "min_price_distance_bps": 3.5}
    assert {key: cfg["rule"]["stake_dollars"] for key, cfg in frozen["accounts"].items()} == {FAST: 10, FORECAST: 5}
    assert campaign(tmp_path) == frozen
    opener.open.assert_called_once_with("http://127.0.0.1:8791/api/status", timeout=12)


def test_official_result_survives_early_exit_and_observation_without_a_fill(arms):
    market = market_at(300)
    traded, observed = arms[0], arms[2]
    for arm in (traded, observed):
        arm.store.upsert_round(market)
    add_trade(traded, market, "SETTLED", .7)
    with observed.store.conn as conn:
        conn.execute("INSERT INTO forecast_checkpoints(round_id,seconds_left,due_at) VALUES(?,?,?)",
                     (market.round_id, 60, market.ends_at - 60))
    resolver = Mock()
    resolver.get_resolution.return_value = dict(market_slug=market.round_id, outcome="Down",
                                                price_source="Gamma:outcomePrices", target_price=100.)
    service = PairedTrials.__new__(PairedTrials)
    service.resolver, service.arms = resolver, arms
    service.stop = Mock()
    service.stop.is_set.side_effect = (False, True)
    service._settle()
    assert service.settlement_error is None
    resolver.get_resolution.assert_called_once_with(market.round_id)
    for arm in (traded, observed):
        row = arm.store.conn.execute("SELECT outcome,settlement_source FROM market_rounds").fetchone()
        assert tuple(row) == ("Down", "polymarket_official")
    assert traded.report()[2] == {market.round_id: .7}
    assert observed.report()[2] == {}


def test_primary_market_requires_current_round_target_and_both_outcome_tokens(tmp_path):
    database = TradeStore(tmp_path / "polybot2other-real-btc.sqlite3", 100.)
    cache = ReferenceCache(tmp_path / "live-accounts/market.references.sqlite3")
    cache.conn.close()
    feed = TrialFeed(tmp_path, Mock())
    try:
        m = market_at(300)
        database.upsert_round(replace(m, target_price=0))
        assert feed._primary_market(300) is None
        database.upsert_round(market_at(600))
        assert feed._primary_market(300) is None
        database.upsert_round(replace(m, target_price=101.))
        row = feed._primary_market(300)
        assert (row.round_id, row.target_price, row.up_token, row.down_token) == (m.round_id, 101., "up", "down")
        with database.conn as db:
            db.execute("UPDATE market_rounds SET down_token='' WHERE round_id=?", (m.round_id,))
        assert feed._primary_market(300) is None
    finally:
        feed.close()
        database.conn.close()

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from polybot2other import main_r1_paper as runner
from polybot2other.main_r1_web import page, serve
from polybot2other.models import MarketRound, Signal
from polybot2other.optimized_reference import SPOT, TWAP
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.trial_market_feed import TrialReferences


@pytest.fixture
def config():
    path = Path(__file__).resolve().parents[1] / "deploy/main-r1-paper/main-r1.example.json"
    return json.loads(path.read_text())


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    (source / "live-accounts").mkdir(parents=True)
    with sqlite3.connect(source / runner.SOURCE_FILES[0]) as conn:
        conn.execute("CREATE TABLE market_rounds(round_id,condition_id,up_token,down_token,target_price)")
        conn.execute("INSERT INTO market_rounds VALUES('test-round','condition','up','down',100)")
    with sqlite3.connect(source / runner.SOURCE_FILES[1]) as conn:
        conn.execute("CREATE TABLE reference_ticks(channel,source_ms,received_ms,price,snapshot)")
        conn.execute("CREATE TABLE reference_validation(verified)")
        conn.execute("INSERT INTO reference_validation VALUES(1)")
    return source


def test_complete_config_disables_all_live_and_default_extra_features(config, tmp_path, monkeypatch):
    monkeypatch.setenv("POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("POLYBOT2OTHER_MANAGED_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYBOT2OTHER_MIN_CONFIDENCE", "0.01")
    settings = runner.build_settings(config, tmp_path)
    assert settings.initial_balance == 1000
    assert settings.min_confidence == config["base_main"]["min_confidence"]
    assert settings.min_price_distance_bps == 3.5  # Do not inherit the dataclass's 1.5.
    assert settings.max_daily_loss == 0  # New NAV guard replaces rolling-24h legacy risk.
    assert not any((settings.live_trading_runtime_enabled, settings.managed_live_enabled,
                    settings.strategy_experiments_enabled, settings.strategy_league_enabled,
                    settings.llm_super_agent_enabled, settings.market_scout_enabled))
    assert settings.paper_realistic_execution


@pytest.mark.parametrize("change", [
    lambda d: d.update(live_enabled=True),
    lambda d: d.update(mode="LIVE"),
    lambda d: d.update(strategy_id="MAIN"),
    lambda d: d.update(version=True),
    lambda d: d["base_main"].update(private_key="sentinel"),
    lambda d: d["base_main"].pop("min_ask_size"),
    lambda d: d["base_main"].update(min_confidence=True),
    lambda d: d["base_main"].update(min_confidence=float("nan")),
    lambda d: d["base_main"].update(max_entry_price=1),
    lambda d: d["base_main"].update(min_time_left_seconds=290, max_time_left_seconds=280),
    lambda d: d["base_main"].update(market_data_mode="MULTI_LEAD"),
    lambda d: d["provenance"].update(captured_at="2026-11-23"),
])
def test_unknown_live_incomplete_and_unsupported_config_rejected(config, change):
    change(config)
    with pytest.raises(ValueError):
        runner.validate_config(config)


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"mode":"PAPER_ONLY","mode":"LIVE"}')
    with pytest.raises(ValueError, match="Duplicate"):
        runner.read_json(path)


def test_campaign_is_frozen_private_and_restart_does_not_reset_start(config, tmp_path):
    root = tmp_path / "r1"
    lock = runner.WriterLock(root)
    try:
        first = runner.campaign(root, config, now=301)
        assert first["start_at"] == 600
        assert first["risk_template"]["initial_balance"] == 1000
        assert first["policy"]["depth_fraction"] == .5
        assert first["execution_model"] == {"arrival_model": "r1-local-observation-v2", "execution_basis": "local_observation"}
        assert set(first["candidates"]) == {"MAIN", "MAIN_FLOOR_060", "FAST", "PLUS"}
        assert (root / "campaign.json").stat().st_mode & 0o777 == 0o600
        assert runner.campaign(root, copy.deepcopy(config), now=99999) == first
        changed = copy.deepcopy(config)
        changed["base_main"]["min_edge"] += .001
        with pytest.raises(ValueError, match="Frozen"):
            runner.campaign(root, changed)
        assert runner.read_json(root / "campaign.json") == first
    finally:
        lock.close()


def test_writer_lock_and_unknown_existing_directory_are_not_reused(config, tmp_path):
    root = tmp_path / "r1"
    lock = runner.WriterLock(root)
    try:
        with pytest.raises(RuntimeError, match="Another"):
            runner.WriterLock(root)
        (root / "old-ledger.sqlite3").write_bytes(b"old account")
        with pytest.raises(ValueError, match="empty"):
            runner.campaign(root, config)
        assert (root / "old-ledger.sqlite3").read_bytes() == b"old account"
    finally:
        lock.close()
    reopened = runner.WriterLock(root)
    reopened.close()


def test_source_open_is_readonly_and_paths_cannot_overlap(source, tmp_path):
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*.sqlite3")}
    assert runner.validate_paths(source, tmp_path / "r1") == (source, tmp_path / "r1")
    with runner.readonly(source / runner.SOURCE_FILES[0]) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM market_rounds")
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    for path in (source, source / "r1", source.parent):
        with pytest.raises(ValueError, match="separate"):
            runner.validate_paths(source, path)
    (source / runner.SOURCE_FILES[1]).unlink()
    with pytest.raises(ValueError, match="missing"):
        runner.validate_paths(source, tmp_path / "r1")
    assert not (source / runner.SOURCE_FILES[1]).exists()


def test_public_exchange_exposes_no_wallet_signing_or_order_api(config, tmp_path, monkeypatch):
    settings = runner.build_settings(config, tmp_path)
    transport = Mock()
    factory = Mock(return_value=transport)
    monkeypatch.setattr(runner, "ManagedExchange", factory)
    public = runner.PublicExchange(settings)
    market = SimpleNamespace(round_id="market")
    public.market_info(market, force=True)
    public.rest_book("token")
    public.close()
    transport.market_info.assert_called_once_with(market, force=True)
    transport.rest_book.assert_called_once_with("token", strict_received_time=True)
    transport.http.close.assert_called_once()
    for name in ("client", "identity", "read_wallet", "prepare", "submit", "reconcile", "token_balance"):
        assert not hasattr(public, name)
        getattr(transport, name).assert_not_called()
    with pytest.raises(ValueError, match="disabled"):
        runner.PublicExchange(replace(settings, live_trading_runtime_enabled=True))
    assert factory.call_count == 1


def test_wallets_have_independent_1000_accounts_and_preserve_restart(tmp_path):
    feed = SimpleNamespace(capture=lambda: SimpleNamespace(market=None))
    stop = threading.Event()
    wallets = [runner.Wallet(tmp_path / key / "paper.sqlite3", feed, stop, expected_id=runner.KEY)
               for key in ("portfolio", *runner.CANDIDATES)]
    try:
        assert [w.store.account()["cash_balance"] for w in wallets] == [1000] * 5
        with wallets[0].store.conn as conn:
            conn.execute("UPDATE account SET cash_balance=997 WHERE id=1")
            conn.execute("CREATE TABLE main_r1_identity(id INTEGER PRIMARY KEY,strategy_id TEXT)")
            conn.execute("INSERT INTO main_r1_identity VALUES(1,?)", (runner.KEY,))
        assert [w.store.account()["cash_balance"] for w in wallets] == [997, 1000, 1000, 1000, 1000]
    finally:
        for wallet in wallets:
            wallet.store.conn.close()
    resumed = runner.Wallet(tmp_path / "portfolio/paper.sqlite3", feed, stop, expected_id=runner.KEY)
    try:
        assert resumed.store.account()["cash_balance"] == 997
    finally:
        resumed.store.conn.close()


def test_foreign_ledger_and_hardlink_rejected_before_any_tradestore_migration(tmp_path, monkeypatch):
    source = tmp_path / "old-production.sqlite3"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE legacy_marker(value TEXT)")
        conn.execute("INSERT INTO legacy_marker VALUES('unchanged sentinel')")
    before = source.read_bytes()
    unopened = Mock(side_effect=AssertionError("TradeStore must not open this ledger"))
    monkeypatch.setattr(runner, "TradeStore", unopened)
    with pytest.raises(ValueError, match="not an initialized"):
        runner.Wallet(source, None, threading.Event(), expected_id=runner.KEY)
    assert source.read_bytes() == before
    alias = tmp_path / "paper.sqlite3"
    os.link(source, alias)
    with pytest.raises(ValueError, match="hard links"):
        runner.Wallet(alias, None, threading.Event(), expected_id=runner.KEY)
    unopened.assert_not_called()
    assert source.read_bytes() == before


def test_wrong_wallet_owner_rejected_before_migration(tmp_path, monkeypatch):
    path = tmp_path / "paper.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE main_r1_identity(id INTEGER PRIMARY KEY,strategy_id TEXT)")
        conn.execute("INSERT INTO main_r1_identity VALUES(1,'MAIN_R1_SHADOW_FAST_PAPER_V1')")
        conn.execute("CREATE TABLE account(id INTEGER PRIMARY KEY,initial_balance REAL)")
        conn.execute("INSERT INTO account VALUES(1,1000)")
    before = path.read_bytes()
    unopened = Mock(side_effect=AssertionError("Foreign owner must not migrate"))
    monkeypatch.setattr(runner, "TradeStore", unopened)
    with pytest.raises(ValueError, match="does not belong"):
        runner.Wallet(path, None, threading.Event(), expected_id=runner.KEY)
    unopened.assert_not_called()
    assert path.read_bytes() == before


def test_public_metadata_ttl_is_shared_and_never_falls_back_to_stale_terms(config, tmp_path, monkeypatch):
    now = [0.]
    transport = Mock()
    transport.market_info.return_value = {"fee_rate": .05, "fee_exponent": 1, "tick_size": "0.01"}
    monkeypatch.setattr(runner, "ManagedExchange", lambda _: transport)
    public = runner.PublicExchange(runner.build_settings(config, tmp_path), clock=lambda: now[0])
    market = SimpleNamespace(round_id="market")
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            replies = list(pool.map(lambda _: public.market_info(market), range(5)))
        assert transport.market_info.call_count == 1
        assert all(reply["fee_rate"] == .05 for reply in replies)
        replies[0]["fee_rate"] = .99
        assert public.market_info(market)["fee_rate"] == .05
        now[0] = 5.1
        transport.market_info.return_value = {"fee_rate": .06, "fee_exponent": 1, "tick_size": "0.001"}
        assert public.market_info(market)["tick_size"] == "0.001"
        assert transport.market_info.call_count == 2
        assert all(call.kwargs == {"force": True} for call in transport.market_info.call_args_list)
        now[0] = 10.2
        transport.market_info.side_effect = RuntimeError("unavailable")
        for _ in range(5):
            with pytest.raises(ValueError, match="unavailable"):
                public.market_info(market)
        assert transport.market_info.call_count == 3  # Shared failure cooldown, never old fee/tick.
    finally:
        public.close()


def test_campaign_binds_runtime_bytes_and_installed_dependency_versions(config, tmp_path, monkeypatch):
    fingerprint = runner.runtime_fingerprint()
    for name in ("main_r1_paper.py", "main_r1_web.py", "main_r1_execution.py", "main_r1_selector.py",
                 "strategy.py", "optimized_fast.py", "optimized_plus.py", "paper_execution.py", "storage.py",
                 "trial_market_feed.py", "managed_live_client.py"):
        digest = fingerprint["source_sha256"]["src/polybot2other/" + name]
        assert len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
    assert fingerprint["dependency_versions"]["py_clob_client_v2"] != "NOT_INSTALLED"
    assert fingerprint["dependency_versions"]["httpx"] != "NOT_INSTALLED"
    root = tmp_path / "r1"
    lock = runner.WriterLock(root)
    try:
        first = runner.campaign(root, config, now=301)
        changed = copy.deepcopy(fingerprint)
        changed["source_sha256"]["src/polybot2other/main_r1_web.py"] = "0" * 64
        monkeypatch.setattr(runner, "runtime_fingerprint", lambda: changed)
        with pytest.raises(ValueError, match="Frozen"):
            runner.campaign(root, config, now=999)
        changed = copy.deepcopy(fingerprint)
        changed["dependency_versions"]["py_clob_client_v2"] = "different-version"
        with pytest.raises(ValueError, match="Frozen"):
            runner.campaign(root, config, now=999)
        assert runner.read_json(root / "campaign.json") == first
    finally:
        lock.close()


def test_status_http_is_readonly_loopback_and_distinguishes_feed_pause():
    snapshot = Mock(return_value={"strategy_id": runner.KEY, "mode": "PAPER_ONLY", "state": "waiting_data",
                                 "feed": {"ready": False}, "portfolio": {}, "shadows": []})
    with pytest.raises(ValueError, match="loopback"):
        serve(("0.0.0.0", 0), snapshot)
    server = serve(("127.0.0.1", 0), snapshot)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        client.request("GET", "/health")
        response = client.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"ok": True, "mode": "PAPER_ONLY", "strategy_id": runner.KEY,
                                               "state": "waiting_data", "feed_ready": False}
        snapshot.return_value["state"] = "learning_blocked"
        client.request("GET", "/health")
        response = client.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["ok"] is True  # A persistent halt must not trigger restart loops.
        called = snapshot.call_count
        client.request("POST", "/api/status", body='{"resume":true}')
        response = client.getresponse()
        assert response.status == 405
        response.read()
        assert snapshot.call_count == called
        client.request("GET", "/resume")
        response = client.getresponse()
        assert response.status == 404
        response.read()
        assert snapshot.call_count == called
        client.request("GET", "/api/status", headers={"Host": "attacker.invalid"})
        response = client.getresponse()
        assert response.status == 403
        response.read()
        assert snapshot.call_count == called
        client.request("GET", "/")
        response = client.getresponse()
        assert response.status == 200
        assert "MAIN-R1" in response.read().decode()
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FakeSelector:
    def __init__(self, store, *, history_reader, clock, config):
        self.store, self.clock = store, clock
        self.candidate = None
        self.calls = []

    def choose(self, frame):
        self.calls.append(frame.snapshot_id)
        return {"candidate_key": self.candidate, "selection_id": "fixed-selection",
                "regime": "trend", "reason": "test prospective choice"}

    def status(self):
        return {"candidate_key": self.candidate}


class FakeCohorts:
    def __init__(self, store, *, shadows, selector, **kwargs):
        self.cycles, self.resolutions, self.due = [], [], []

    def observe_cycle(self, frame, successes):
        self.cycles.append((frame.snapshot_id, dict(successes)))

    def due_rounds(self, now=None):
        return list(self.due)

    def record_resolution(self, round_id, result):
        self.resolutions.append((round_id, result))
        return True

    def flush_completed(self):
        return 0

    def snapshot(self):
        return {"observed_cycles": len(self.cycles)}


class FakeEngine:
    def __init__(self, wallet, strategy_id, settings, **kwargs):
        assert not settings.live_trading_runtime_enabled and not settings.managed_live_enabled
        self.bot, self.store, self.strategy_id = wallet, wallet.store, strategy_id
        self.lock = threading.RLock()
        self.guard = SimpleNamespace(snapshot=lambda: {"halted": False})
        self.entry_plan_reader = kwargs["entry_plan_reader"]
        self.config_reader = kwargs["config_reader"]
        self.plans = []
        self.decisions = self.polls = 0
        self.last_observation_valid = True

    def decide(self):
        self.decisions += 1
        frame = self.bot._sync_shared_market_inputs()
        self.plans.append(self.entry_plan_reader(frame))
        return frame

    def poll(self):
        self.polls += 1

    def snapshot(self):
        return {"risk": {"nav": 1000, "allowed": True}}


@pytest.fixture
def isolated_runner(config, source, tmp_path):
    clock = [301.]
    market = MarketRound("btc-updown-5m-600", "BTC", 600., 900., 100.,
                         condition_id="condition", up_token="up", down_token="down")

    class Feed:
        def __init__(self, path, exchange):
            self.references = TrialReferences(path / runner.SOURCE_FILES[1])
            self.last_error = None
            self.books = SimpleNamespace(health=lambda: {"ws_state": "test"})
            self.closed = False

        def capture(self):
            now = clock[0]
            quotes = {side: {"token_id": token, "best_bid": .60, "best_ask": .61,
                            "source_ms": now*1000, "received_ms": now*1000,
                            "bids": [{"price": .60, "size": 1000}],
                            "asks": [{"price": .61, "size": 1000}]}
                      for side, token in (("Up", "up"), ("Down", "down"))}
            return MarketInputs(market, {"ready": True, "protocol_verified": True}, {},
                                quotes, now, "test-frame", None)

        def start(self):
            pass

        def close(self):
            self.references.conn.close()
            self.closed = True

    exchange = Mock()
    obj = runner.MainR1Runner(source, tmp_path / "r1", config, clock=lambda: clock[0],
        feed_factory=Feed, exchange_factory=lambda settings: exchange, engine_factory=FakeEngine,
        selector_factory=FakeSelector, cohort_factory=FakeCohorts)
    try:
        yield obj, clock, exchange
    finally:
        obj.close()


def test_snapshot_uses_clock_after_capture_without_clamping_genuinely_future_frames(isolated_runner):
    obj, clock, _ = isolated_runner
    clock[0] = 615.
    capture = obj.feed.capture

    def elapsed_capture():
        clock[0] += .025  # The production capture observes time after snapshot() begins.
        return capture()

    obj.feed.capture = elapsed_capture
    status = obj.snapshot()
    assert status["as_of"] == 615.025
    assert status["feed"]["ready"] is True
    assert status["state"] == "running"
    assert status["learning"]["state"] == "collecting"

    def future_capture():
        return replace(capture(), observed_at=clock[0]+.25)

    obj.feed.capture = future_capture
    status = obj.snapshot()
    assert status["as_of"] == 615.025  # Do not hide clock errors by moving now to the frame time.
    assert status["feed"]["ready"] is False
    assert status["state"] == "waiting_data"


def test_unknown_shadow_blocks_learning_and_is_visible_without_reading_risk_twice(isolated_runner):
    obj, clock, _ = isolated_runner
    clock[0] = 615.
    engine = obj.shadows["FAST"]
    engine.snapshot = Mock(return_value={
        "counts": {"FILLED": 3, "UNKNOWN": 1}, "unknown_order_count": 1,
        "arrival_model": "r1-local-observation-v2", "execution_basis": "local_observation",
        "risk": {"allowed": False, "valuation_status": "unavailable"},
        "risk_state": {"persistent_halts": ["unknown_order"]},
        "last_event": {"code": "RISK_WAIT", "message": "订单状态未知", "at": 615.},
        "unknown_orders": [{"order_id": 5, "round_id": "btc-updown-5m-600", "phase": "UNKNOWN",
                            "model": "legacy-dual-clock-v1", "execution_basis": "dual_clock",
                            "submitted_at": 610., "arrival_at": 611., "deadline_at": 613.,
                            "reason": "固定窗口无合格证据 <unsafe>", "evidence_at": 612.,
                            "source_ms": 610900., "received_ms": 611020.,
                            "source_age_ms": 1100., "receipt_age_ms": 980., "transport_lag_ms": 120.}]
    })
    engine.guard.snapshot = Mock(side_effect=AssertionError("Use the already captured risk state"))
    obj._account_snapshot = Mock(wraps=obj._account_snapshot)
    status = obj.snapshot()
    assert obj._account_snapshot.call_count == 5
    assert [call.args[0] for call in obj._account_snapshot.call_args_list] == ["portfolio", *runner.CANDIDATES]
    engine.snapshot.assert_called_once_with()
    engine.guard.snapshot.assert_not_called()
    assert status["state"] == "learning_blocked"
    assert status["learning_blocked"] is True and status["execution_halted"] is False
    assert status["learning"]["state"] == "blocked"
    assert status["blocking"]["blocked_shadow_count"] == 1
    assert status["blocking"]["unknown_order_count"] == 1
    assert status["blocking"]["shadows"][0]["key"] == "FAST"
    assert status["blocking"]["shadows"][0]["reasons"]
    html = page(status)
    for evidence in ("learning_blocked", "UNKNOWN 1", "RISK_WAIT", "订单 #5", "legacy-dual-clock-v1",
                     "r1-local-observation-v2", "预计到达", "截止", "120.0 ms", "cohorts", "errors"):
        assert evidence in html
    assert "&lt;unsafe&gt;" in html and "<unsafe>" not in html
    assert "影子账户继续采样。" not in html


@pytest.mark.parametrize("account_name,unknown,halts,expected", [
    ("portfolio", 1, [], "execution_halted"),
    ("portfolio", 0, ["peak_drawdown"], "execution_halted"),
    ("MAIN", 1, [], "learning_blocked"),
    ("MAIN", 0, ["net_edge_drift"], "learning_blocked"),
])
def test_persistent_or_unknown_blockers_do_not_require_each_other(isolated_runner, account_name, unknown, halts, expected):
    obj, clock, _ = isolated_runner
    clock[0] = 615.
    obj.engines[account_name].snapshot = Mock(return_value={
        "counts": {"UNKNOWN": unknown}, "risk_state": {"persistent_halts": halts}})
    status = obj.snapshot()
    assert status["state"] == expected
    assert status["blocking"]["unknown_order_count"] == unknown
    assert status["execution_halted"] is (account_name == "portfolio")
    assert status["learning_blocked"] is (account_name != "portfolio")


def test_process_priority_preserves_blocker_diagnostics(isolated_runner):
    obj, clock, _ = isolated_runner
    obj.shadows["MAIN"].snapshot = Mock(return_value={"counts": {"UNKNOWN": 1}})
    assert obj.snapshot()["state"] == "waiting_start"
    clock[0] = 615.
    obj.errors["decision:MAIN"] = "ValueError"
    status = obj.snapshot()
    assert status["state"] == "degraded" and status["learning_blocked"] is True
    obj._closed = True
    try:
        status = obj.snapshot()
        assert status["state"] == "stopped" and status["learning_blocked"] is True
    finally:
        obj._closed = False


def test_runner_has_one_portfolio_four_shadow_ledgers_and_no_implicit_choice(isolated_runner):
    obj, clock, exchange = isolated_runner
    obj._drive_once()
    assert not any(engine.decisions for engine in obj.engines.values())  # Next full round only.
    clock[0] = 615.
    obj._drive_once()
    assert obj.portfolio.plans == [None]
    assert list(obj.engines) == [*runner.CANDIDATES, "portfolio"]
    assert {name: engine.plans[0]["candidate_key"] for name, engine in obj.shadows.items()} == {
        name: name for name in runner.CANDIDATES}
    assert obj.shadows["MAIN_FLOOR_060"].plans[0]["price_floor"] == .60
    assert obj.shadows["FAST"].plans[0]["signal_family"] == "MAIN_OPTIMIZED_FAST"
    assert obj.cohorts.cycles == [("test-frame", dict.fromkeys(runner.CANDIDATES, True))]
    assert {engine.store.account()["initial_balance"] for engine in obj.engines.values()} == {1000}
    assert len({engine.store.db_path for engine in obj.engines.values()}) == 5
    obj.selector.candidate = "PLUS"
    obj._drive_once()
    assert obj.portfolio.plans[-1] == {"candidate_key": "PLUS", "signal_family": "MAIN_OPTIMIZED_PLUS",
        "price_floor": None, "selection_id": "fixed-selection", "regime": "trend", "reason": "test prospective choice"}
    before = [(engine.decisions, engine.polls) for engine in obj.engines.values()]
    snapshot = obj.snapshot()
    assert snapshot["portfolio"]["key"] == "portfolio"
    assert len(snapshot["shadows"]) == 4
    assert [(engine.decisions, engine.polls) for engine in obj.engines.values()] == before
    assert obj.run["selector_config"]  # Runtime freezes the selector constants, not an empty placeholder.
    exchange.prepare.assert_not_called()
    exchange.submit.assert_not_called()


def test_shadow_exception_records_incomplete_coverage_and_other_arms_continue(isolated_runner):
    obj, clock, _ = isolated_runner
    clock[0] = 615.
    obj.shadows["FAST"].decide = Mock(side_effect=RuntimeError("test"))
    obj._drive_once()
    assert obj.cohorts.cycles[-1][1] == {"MAIN": True, "MAIN_FLOOR_060": True, "FAST": False, "PLUS": True}
    assert obj.portfolio.decisions == 1
    assert obj.errors["decision:FAST"] == "RuntimeError"
    assert all(wallet.first_frame is None for wallet in obj.wallets.values())


@pytest.mark.parametrize("present", [True, False])
def test_valid_looking_frame_without_verified_opportunity_is_not_zero_pnl_training(isolated_runner, present):
    obj, clock, _ = isolated_runner
    clock[0] = 615.
    if present:
        obj.shadows["MAIN"].last_observation_valid = False
    else:
        del obj.shadows["MAIN"].last_observation_valid
    obj._drive_once()
    assert obj.cohorts.cycles[-1][1]["MAIN"] is False
    assert all(obj.cohorts.cycles[-1][1][key] for key in ("MAIN_FLOOR_060", "FAST", "PLUS"))


def test_settlement_requires_official_result_and_holds_each_engine_lock(isolated_runner):
    obj, clock, _ = isolated_runner
    clock[0] = 615.
    obj._drive_once()
    clock[0] = 901.
    round_id = "btc-updown-5m-600"
    obj.cohorts.due = [round_id]
    calls = []
    for name, engine in obj.engines.items():
        def settle(*args, _name=name, _engine=engine):
            assert _engine.lock._is_owned()
            calls.append((_name, args))
        engine.store.settle_round_outcome = settle
    result = {"market_slug": round_id, "price_source": "local-model", "outcome": "Up"}
    obj.resolver = SimpleNamespace(get_resolution=lambda _: result)
    obj._settle_once()
    assert not calls and not obj.cohorts.resolutions
    result["price_source"] = "Gamma:outcomePrices"
    obj.retry.clear()
    # Entry pause must not stop official settlement.
    for wallet in obj.wallets.values():
        wallet.paper_trading_paused = True
    obj._settle_once()
    assert len(calls) == 5
    assert len(obj.cohorts.resolutions) == 1
    assert all(args[-1] == "polymarket_official" for _, args in calls)


def test_unknown_router_candidate_is_rejected_and_none_stays_none():
    assert runner.selection_plan({"candidate_key": None}) is None
    with pytest.raises(ValueError, match="unknown"):
        runner.selection_plan({"candidate_key": "LIVE"})


def test_real_components_learn_only_completed_common_zero_trade_cohort(config, source, tmp_path):
    """Exercise the real runner/selector/collector/engines without any network."""
    clock = [301.]
    with sqlite3.connect(source / runner.SOURCE_FILES[1]) as conn:
        conn.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,0)",
            [(channel, stamp*1000, stamp*1000, 100+stamp*.00001)
             for channel in (SPOT, TWAP) for stamp in range(300, 902)])
    source_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*.sqlite3")}
    market = MarketRound("btc-updown-5m-600", "BTC", 600., 900., 100.,
                         condition_id="condition", up_token="up", down_token="down")

    class PublicFeed:
        def __init__(self, path, exchange):
            self.references = TrialReferences(path / runner.SOURCE_FILES[1])
            self.books = SimpleNamespace(health=lambda: {"ws_state": "fixture"})
            self.last_error = None
            self.rules = {}
            self.rules_checked_at = 0
            self.empty_quotes = False

        def capture(self):
            observed = clock[0] - .001  # A real capture precedes selector's wall-clock call.
            quotes = {side: {"token_id": token, "best_bid": .60, "best_ask": .61,
                            "source_ms": observed*1000, "received_ms": observed*1000,
                            "bids": [{"price": .60, "size": 1000}],
                            "asks": [{"price": .61, "size": 1000}],
                            "tick_size": "0.01", "min_order_size": 5}
                      for side, token in (("Up", "up"), ("Down", "down"))}
            reference = {"ready": True, "protocol_verified": True,
                         "latest": {SPOT: {"price": 100.01, "source_ms": observed*1000}}}
            return MarketInputs(market, reference,
                {"chainlink": 100.01, "chainlink_updated_ms": observed*1000}, {} if self.empty_quotes else quotes,
                observed, "fixture-"+str(observed), None)

        def close(self):
            self.references.conn.close()

    exchange = Mock()
    exchange.market_info.return_value = {"fee_rate": .05, "fee_exponent": 1, "tick_size": "0.01"}
    obj = runner.MainR1Runner(source, tmp_path / "real-components", config, clock=lambda: clock[0],
                             feed_factory=PublicFeed, exchange_factory=lambda _: exchange)
    try:
        for engine in obj.shadows.values():
            engine.signals.evaluate = Mock(return_value=(Signal("BTC", "NO_TRADE", 0, 0, 0, "fixture rejection"), None))
        clock[0] = 601.
        obj.feed.empty_quotes = True
        obj._drive_once()
        assert obj.selector.status()["last_decision"] is None
        assert not obj.snapshot()["feed"]["ready"]
        clock[0] = 614.
        obj.feed.empty_quotes = False
        obj._drive_once()
        assert obj.selector.status()["last_decision"] is None  # Full books, but too early.
        assert obj.snapshot()["state"] == "waiting_entry_window"
        clock[0] = 615.
        obj.feed.empty_quotes = True
        obj._drive_once()
        assert obj.selector.status()["last_decision"] is None  # Entry window alone is insufficient.
        clock[0] = 616.
        obj.feed.empty_quotes = False
        obj._drive_once()
        assert obj.selector.status()["last_decision"]["candidate_key"] is None
        assert obj.selector.status()["last_decision"]["regime"] == "trend"
        for stamp in range(618, 884, 2):
            clock[0] = float(stamp)
            obj._drive_once()
        assert obj.selector.status()["cohorts"] == 0
        clock[0] = 901.
        obj.resolver = SimpleNamespace(get_resolution=lambda rid: {
            "market_slug": rid, "price_source": "Gamma:outcomePrices", "outcome": "Up",
            "target_price": 100., "final_price": 101.})
        obj._settle_once()
        assert obj.selector.status()["cohorts"] == 1
        assert all(engine.store.account()["cash_balance"] == 1000 for engine in obj.engines.values())
        with obj.portfolio.store._lock:
            row = obj.portfolio.store.conn.execute("SELECT * FROM main_r1_cohort_coverage").fetchone()
        assert row["status"] == "LEARNED"
        assert row["feature_time"] == pytest.approx(615.999)
        assert all(v["status"] == "NO_TRADE" and v["pnl"] == 0
                   for v in json.loads(row["cohort_json"])["arms"].values())
        assert all(p.read_bytes() and hashlib.sha256(p.read_bytes()).hexdigest() == digest
                   for p, digest in source_hashes.items())
        exchange.rest_book.assert_not_called()
        exchange.prepare.assert_not_called()
        exchange.submit.assert_not_called()
    finally:
        obj.close()

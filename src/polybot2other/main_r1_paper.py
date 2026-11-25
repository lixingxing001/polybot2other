"""Opt-in MAIN-R1 Paper runner; independent wallets and public/read-only inputs.

This module deliberately does not load .env files, instantiate the live manager,
register a default strategy, or accept a signing/submission callback.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import fcntl
import hashlib
from importlib import metadata
import json
import logging
import math
import os
from pathlib import Path
import platform
import signal
import sqlite3
import stat
import tempfile
import threading
import time

from .config import Settings
from .managed_live_client import ManagedExchange
from .optimized_main import StrictTwapMarketClient
from .paper_execution import ExecutionPolicy
from .storage import TradeStore
from .trial_market_feed import TrialFeed, readonly

LOG = logging.getLogger(__name__)
KEY = "MAIN_R1_ADAPTIVE_PAPER_V1"
LABEL = "MAIN-R1"
CANDIDATES = {
    "MAIN": ("MAIN", None),
    "MAIN_FLOOR_060": ("MAIN", .60),
    "FAST": ("MAIN_OPTIMIZED_FAST", None),
    "PLUS": ("MAIN_OPTIMIZED_PLUS", None),
}
MODES = {"price_source_mode": "MIXED", "market_data_mode": "BASE", "anti_bot_guard_mode": "NONE"}
NUMERIC_MAIN = {
    "min_confidence": (0, 1), "min_edge": (0, 1), "min_price_distance_bps": (0, 10000),
    "max_entry_price": (0, 1), "max_spread": (0, 1), "min_ask_size": (0, 1e9),
    "min_time_left_seconds": (0, 299), "max_time_left_seconds": (1, 300),
    "max_quote_age_ms": (1, 60000),
}
POLICY = asdict(ExecutionPolicy(arrival_floor_seconds=1, confirmation_seconds=12,
    confirmation_samples=0, depth_fraction=.5, redemption_seconds=60, max_poll_lateness_seconds=2))
RISK_TEMPLATE = {
    "initial_balance": 1000, "max_entry_budget": 5, "entry_nav_fraction": .005,
    "unresolved_nav_fraction": .01, "max_unresolved_rounds": 2,
    "daily_loss_fraction": .02, "peak_drawdown_fraction": .08,
    "day_timezone": "Asia/Shanghai", "external_funding": 0,
    "status": "UNREPLAYED_PAPER_RESEARCH_PARAMETERS",
}
SOURCE_FILES = ("polybot2other-real-btc.sqlite3", "live-accounts/market.references.sqlite3")
RUNTIME_FILES = (
    "config.py", "models.py", "experiments.py", "strategy.py", "optimized_main.py",
    "optimized_fast.py", "optimized_plus.py", "optimized_reference.py", "forecast_paper.py",
    "paper_execution.py", "storage.py", "execution.py", "shared_market_inputs.py",
    "trial_market_feed.py", "managed_live_client.py", "managed_live_signals.py",
    "managed_live_book.py", "managed_live_receipts.py", "managed_live_recovery.py",
    "clob_ws.py", "buffered_ws.py", "polymarket.py", "live.py", "bot.py",
    "adaptive_main.py", "adaptive_features.py", "first_strong_rules.py", "value_confirm.py",
    "strategy_league_policies.py", "managed_live_store.py", "signal_filters.py",
)
RUNTIME_DEPENDENCIES = (
    "polybot2other", "py_clob_client_v2", "httpx", "httpcore", "eth-account", "eth-utils",
    "eth-abi", "eth-keys", "eth-typing", "hexbytes", "pydantic", "jsonschema", "PyYAML",
)


def runtime_fingerprint():
    """Hash shipped code only; declared Git provenance is not execution evidence."""
    package = Path(__file__).resolve().parent
    names = sorted(set(RUNTIME_FILES) | {p.name for p in package.glob("main_r1_*.py")})
    files = {"src/polybot2other/" + name: hashlib.sha256((package / name).read_bytes()).hexdigest()
             for name in names}
    versions = {}
    for name in RUNTIME_DEPENDENCIES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "NOT_INSTALLED"
    return {"source_sha256": files, "dependency_versions": versions,
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(), "sqlite_version": sqlite3.sqlite_version}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 1_000_000:
        raise ValueError("Configuration is too large")
    return json.loads(raw, object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def validate_config(value):
    """Accept a complete signal snapshot, never general Settings or live flags."""
    keys = {"version", "mode", "strategy_id", "base_main", "provenance"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Config requires exactly version, mode, strategy_id, base_main, provenance")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported MAIN-R1 config version")
    if value["mode"] != "PAPER_ONLY" or value["strategy_id"] != KEY:
        raise ValueError("MAIN-R1 accepts PAPER_ONLY and its isolated strategy ID only")
    main = value["base_main"]
    if not isinstance(main, dict) or set(main) != set(NUMERIC_MAIN) | set(MODES):
        raise ValueError("base_main must contain the complete allowlisted MAIN signal snapshot")
    for name, expected in MODES.items():
        if main[name] != expected:
            raise ValueError(f"This feed supports the reviewed MAIN {name}={expected} only")
    for name, (low, high) in NUMERIC_MAIN.items():
        number = main[name]
        if type(number) not in (int, float) or not low <= number <= high or not math.isfinite(number):
            raise ValueError(f"Invalid numeric MAIN parameter: {name}")
        if name.endswith("_seconds") or name == "max_quote_age_ms":
            if type(number) is not int:
                raise ValueError(f"MAIN parameter must be an integer: {name}")
    if not 0 < main["max_entry_price"] < 1:
        raise ValueError("max_entry_price must be strictly between zero and one")
    if main["min_time_left_seconds"] >= main["max_time_left_seconds"]:
        raise ValueError("MAIN entry time window is inverted")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"source_commit", "captured_at", "source_note"}:
        raise ValueError("provenance requires source_commit, captured_at and source_note")
    commit = provenance["source_commit"]
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("source_commit must be a full lowercase Git commit hash")
    for name in ("captured_at", "source_note"):
        if not isinstance(provenance[name], str) or not 1 <= len(provenance[name]) <= 500:
            raise ValueError(f"Invalid provenance {name}")
    try:
        captured = datetime.fromisoformat(provenance["captured_at"].replace("Z", "+00:00"))
        if captured.tzinfo is None:
            raise ValueError("timezone missing")
    except (TypeError, ValueError) as exc:
        raise ValueError("captured_at must be an ISO timestamp with a timezone") from exc
    return json.loads(json.dumps(value, allow_nan=False))


def build_settings(config, data_dir):
    config = validate_config(config)
    return Settings(**{k: config["base_main"][k] for k in NUMERIC_MAIN},
        initial_balance=1000, db_path=Path(data_dir) / "portfolio/paper.sqlite3",
        stake_dollars=5, max_open_trades=2, max_daily_loss=0, tick_seconds=2,
        round_seconds=300, paper_entry_order_type="FAK", paper_realistic_execution=True,
        live_trading_runtime_enabled=False, managed_live_enabled=False,
        strategy_experiments_enabled=False, strategy_league_enabled=False,
        llm_super_agent_enabled=False, market_scout_enabled=False,
        market_scout_evidence_enabled=False)


def campaign(data_dir, config, *, now=None, selector_config=None):
    """Create once under the caller's writer lock; restarts cannot change the run."""
    from .main_r1_execution import ARRIVAL_MODEL, EXECUTION_BASIS

    config = validate_config(config)
    root = Path(data_dir)
    path = root / "campaign.json"
    frozen = {"config": config, "policy": POLICY, "risk_template": RISK_TEMPLATE,
              "execution_model": {"arrival_model": ARRIVAL_MODEL, "execution_basis": EXECUTION_BASIS},
              "runtime": runtime_fingerprint(),
              "selector_config": json.loads(json.dumps(selector_config or {}, allow_nan=False)),
              "candidates": {k: {"signal_family": v[0], "price_floor": v[1]} for k, v in CANDIDATES.items()}}
    digest = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if path.exists():
        if path.is_symlink():
            raise ValueError("Campaign file must not be a symlink")
        run = read_json(path)
        if any(run.get(k) != v for k, v in frozen.items()) or run.get("config_id") != digest:
            raise ValueError("Frozen MAIN-R1 configuration or runtime differs; do not reuse this run directory")
        if any(type(run.get(k)) not in (int, float) or not math.isfinite(run[k]) for k in ("created_at", "start_at")):
            raise ValueError("Campaign timestamps are invalid")
        return run
    if any(p.name != "writer.lock" for p in root.iterdir()):
        raise ValueError("New MAIN-R1 run requires an empty independent data directory")
    now = time.time() if now is None else now
    run = {**frozen, "config_id": digest, "created_at": now, "start_at": math.ceil(now / 300) * 300}
    fd, temporary = tempfile.mkstemp(prefix="campaign-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(run, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Exclusive publication; never overwrite an existing campaign.
    finally:
        os.unlink(temporary)
    return run


def validate_paths(source, data_dir):
    source, root = Path(source).resolve(strict=True), Path(data_dir).resolve()
    if not source.is_dir() or root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("MAIN-R1 output and production input directories must be separate")
    for relative in SOURCE_FILES:
        path = source / relative
        if not path.is_file():
            raise ValueError(f"Required read-only market input is missing: {relative}")
    # Opening mode=ro also prevents accidentally creating a missing source database.
    with closing(readonly(source / SOURCE_FILES[0])) as conn:
        conn.execute("SELECT round_id,condition_id,up_token,down_token,target_price FROM market_rounds LIMIT 0")
    with closing(readonly(source / SOURCE_FILES[1])) as conn:
        conn.execute("SELECT channel,source_ms,received_ms,price,snapshot FROM reference_ticks LIMIT 0")
        conn.execute("SELECT verified FROM reference_validation LIMIT 0")
    return source, root


class WriterLock:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC
        self.fd = os.open(root / "writer.lock", flags, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError("Another MAIN-R1 writer is using this data directory") from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class PublicExchange:
    """A narrow facade; callers receive no wallet, signer or order API."""
    def __init__(self, settings, *, clock=time.monotonic, metadata_ttl_seconds=5.):
        if settings.live_trading_runtime_enabled or settings.managed_live_enabled:
            raise ValueError("MAIN-R1 exchange requires live paths to be disabled")
        if not 0 < metadata_ttl_seconds <= 5:
            raise ValueError("Public metadata TTL must be positive and at most five seconds")
        self._reader = ManagedExchange(settings)
        self._clock, self._ttl = clock, metadata_ttl_seconds
        self._info_lock = threading.RLock()
        self._info_cache = {}

    def market_info(self, market, force=False):
        with self._info_lock:
            saved = self._info_cache.get(market.round_id)
            if not force and saved and self._clock() < saved[0]:
                if saved[1] is None:
                    raise ValueError("Public market metadata refresh is unavailable")
                return deepcopy(saved[1])
            try:
                # The underlying reader otherwise caches for the entire market round.
                info = self._reader.market_info(market, force=True)
            except Exception as exc:
                # Fail closed and share a brief failure cooldown, never stale terms.
                self._info_cache[market.round_id] = (self._clock()+self._ttl, None)
                raise ValueError("Public market metadata refresh is unavailable") from exc
            self._info_cache[market.round_id] = (self._clock()+self._ttl, deepcopy(info))
            if len(self._info_cache) > 16:
                oldest = min(self._info_cache, key=lambda key: self._info_cache[key][0])
                del self._info_cache[oldest]
            return deepcopy(info)

    def rest_book(self, token_id):
        return self._reader.rest_book(token_id, strict_received_time=True)

    def close(self):
        self._reader.http.close()


class Wallet:
    def __init__(self, path, feed, stop, *, expected_id):
        path = Path(path)
        if path.is_symlink():
            raise ValueError("Paper ledger must not be a symlink")
        if path.exists():
            details = path.stat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise ValueError("Existing Paper ledger must be a regular file without hard links")
            try:
                with closing(readonly(path)) as conn:
                    owner = conn.execute("SELECT strategy_id FROM main_r1_identity WHERE id=1").fetchone()
                    account = conn.execute("SELECT initial_balance FROM account WHERE id=1").fetchone()
                    if not owner or owner[0] != expected_id or not account or account[0] != 1000:
                        raise ValueError("Existing ledger does not belong to this MAIN-R1 wallet")
            except sqlite3.Error as exc:
                raise ValueError("Existing ledger is not an initialized MAIN-R1 wallet") from exc
        self.store = TradeStore(path, 1000)
        self.feed, self._stop = feed, stop
        self._lock = threading.RLock()
        self.paper_trading_paused = False
        self.first_frame = None
        self.saved_market = None

    def _sync_shared_market_inputs(self):
        frame = self.first_frame if self.first_frame is not None else self.feed.capture()
        self.first_frame = None
        if frame.market and frame.market != self.saved_market:
            self.store.upsert_round(frame.market)
            self.saved_market = frame.market
        return frame


def fixed_plan(candidate_key, frame):
    family, floor = CANDIDATES[candidate_key]
    return {"candidate_key": candidate_key, "signal_family": family, "price_floor": floor,
            "selection_id": f"shadow:{candidate_key}:{getattr(frame.market, 'round_id', 'none')}", "regime": "shadow",
            "reason": "Independent prospective shadow; same research risk and hold-to-settlement policy"}


def selection_plan(selection):
    """Translate only an explicit frozen choice; abstention never falls back."""
    candidate = selection.get("candidate_key")
    if candidate is None:
        return None
    if candidate not in CANDIDATES:
        raise ValueError("Selector returned an unknown candidate")
    family, floor = CANDIDATES[candidate]
    return {"candidate_key": candidate, "signal_family": family, "price_floor": floor,
            "selection_id": selection["selection_id"], "regime": selection["regime"],
            "reason": selection["reason"]}


def official_resolution(round_id, result):
    return (isinstance(result, dict) and result.get("market_slug") == round_id
            and result.get("price_source") == "Gamma:outcomePrices"
            and result.get("outcome") in ("Up", "Down"))


def common_inputs_ready(frame, now):
    """Do not freeze a round on the collector's transient empty opening books."""
    market = frame.market
    if (market is None or frame.error or not market.started_at <= now < market.ends_at
            or not 0 <= now-frame.observed_at <= 3
            or not frame.reference.get("ready") or not frame.reference.get("protocol_verified")):
        return False
    for side in ("Up", "Down"):
        if frame.quote_error(side, now) is not None:
            return False
        book = frame.quotes.get(side, {})
        for name in ("bids", "asks"):
            levels = book.get(name)
            if not isinstance(levels, (list, tuple)) or not levels:
                return False
            for level in levels:
                if not isinstance(level, dict):
                    return False
                price, size = level.get("price"), level.get("size")
                if (type(price) not in (int, float) or not 0 < price < 1
                        or type(size) not in (int, float) or size <= 0):
                    return False
                try:
                    if not math.isfinite(size):
                        return False
                except OverflowError:
                    return False
    return True


def r1_trial_feed(source, exchange):
    """Opt in to socket receipt timestamps without changing other trial feeds."""
    return TrialFeed(source, exchange, strict_received_time=True)


class MainR1Runner:
    """One prospective portfolio and four independent, comparable shadow wallets."""
    def __init__(self, source, data_dir, config, *, clock=time.time,
                 feed_factory=r1_trial_feed, exchange_factory=PublicExchange,
                 engine_factory=None, selector_factory=None, cohort_factory=None):
        from .main_r1_execution import MainR1Execution
        from .main_r1_selector import MainR1Selector, SELECTOR_CONFIG
        from .main_r1_cohort import CohortCollector, make_history_reader

        self.clock = clock
        self.stop = threading.Event()
        self.threads = []
        self.wallets, self.engines = {}, {}
        self.feed = self.exchange = self.writer_lock = None
        self._started = self._closed = False
        self.last_step = None
        self.errors = {}
        self.retry = {}
        self.source, self.root = validate_paths(source, data_dir)
        self.settings = build_settings(config, self.root)
        try:
            self.writer_lock = WriterLock(self.root)
            self.run = campaign(self.root, config, now=clock(), selector_config=SELECTOR_CONFIG)
            self.exchange = exchange_factory(self.settings)
            self.feed = feed_factory(self.source, self.exchange)
            self.resolver = StrictTwapMarketClient(self.settings, self.feed.references)
            for name in ("portfolio", *CANDIDATES):
                relative = "portfolio/paper.sqlite3" if name == "portfolio" else f"shadows/{name}/paper.sqlite3"
                path = self.root / relative
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("Paper ledger escaped the independent data directory")
                expected_id = KEY if name == "portfolio" else f"MAIN_R1_SHADOW_{name}_PAPER_V1"
                self.wallets[name] = Wallet(path, self.feed, self.stop, expected_id=expected_id)
            self.selector = (selector_factory or MainR1Selector)(self.wallets["portfolio"].store,
                history_reader=make_history_reader(self.feed.references), clock=clock,
                config=self.run["selector_config"])
            engine_factory = engine_factory or MainR1Execution
            # Construct all shadows before the portfolio; never use a default plan for it.
            for name in (*CANDIDATES, "portfolio"):
                key = KEY if name == "portfolio" else f"MAIN_R1_SHADOW_{name}_PAPER_V1"
                wallet = self.wallets[name]
                cfg = {"revision": 1, "mode": "PAPER_ONLY", "live_enabled": False,
                       "config_id": self.run["config_id"], "base_main": self.run["config"]["base_main"],
                       "execution_model": deepcopy(self.run["execution_model"]),
                       "strategies": {key: {"enabled": True, "stake_dollars": 5,
                            "max_open_trades": 2, "max_daily_loss": 0, **MODES}}}
                plan_reader = self._portfolio_plan if name == "portfolio" else (
                    lambda frame, candidate=name: fixed_plan(candidate, frame))
                self.engines[name] = engine_factory(wallet, key,
                    replace(self.settings, db_path=wallet.store.db_path),
                    frame_reader=self.feed.capture, info_reader=self.exchange.market_info,
                    book_reader=self.exchange.rest_book, config_reader=lambda value=cfg: deepcopy(value),
                    feed=self.feed, policy=ExecutionPolicy(**self.run["policy"]), clock=clock,
                    entry_plan_reader=plan_reader)
            self.portfolio = self.engines["portfolio"]
            self.shadows = {name: self.engines[name] for name in CANDIDATES}
            self.cohorts = (cohort_factory or CohortCollector)(self.portfolio.store,
                shadows=self.shadows, selector=self.selector, clock=clock, start_at=self.run["start_at"],
                min_time_left_seconds=self.settings.min_time_left_seconds,
                max_time_left_seconds=self.settings.max_time_left_seconds,
                tick_seconds=self.settings.tick_seconds)
        except Exception:
            self.close()
            raise

    def _portfolio_plan(self, frame):
        return selection_plan(self.selector.choose(frame))

    def _drive_once(self):
        frame = self.feed.capture()
        market = frame.market
        now = self.clock()
        if (now < self.run["start_at"] or (market and market.started_at < self.run["start_at"])
                or not common_inputs_ready(frame, now)
                or now < market.ends_at-self.settings.max_time_left_seconds):
            # Poll and settlement run independently. No temporary opening-data gap
            # is converted into a permanent selector abstention or a zero-PnL row.
            self.last_step = now
            return
        # Freeze one regime/choice before observing this cycle's shadow executions.
        # The selector itself handles invalid or insufficient input without hindsight.
        if market is not None:
            self.selector.choose(frame)
        success = {}
        for name in (*CANDIDATES, "portfolio"):
            if self.stop.is_set():
                break
            wallet, engine = self.wallets[name], self.engines[name]
            wallet.first_frame = frame
            try:
                returned = engine.decide()
                success[name] = bool(getattr(engine, "last_observation_valid", False) and market and returned and returned.market
                    and returned.market.round_id == market.round_id and not returned.error)
                self.errors.pop("decision:" + name, None)
            except Exception as exc:
                success[name] = False
                self.errors["decision:" + name] = type(exc).__name__
                LOG.error("MAIN-R1 %s decision unavailable (%s)", name, type(exc).__name__)
            finally:
                wallet.first_frame = None
        if market is not None:
            self.cohorts.observe_cycle(frame, {name: success.get(name, False) for name in CANDIDATES})
        self.cohorts.flush_completed()
        self.last_step = self.clock()

    def _drive(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self._drive_once()
                self.errors.pop("drive", None)
            except Exception as exc:
                self.errors["drive"] = type(exc).__name__
                LOG.error("MAIN-R1 cycle unavailable (%s)", type(exc).__name__)
            self.stop.wait(max(.05, self.settings.tick_seconds - (time.monotonic() - started)))

    def _poll(self):
        while not self.stop.is_set():
            for name, engine in self.engines.items():
                if not engine.lock.acquire(blocking=False):
                    continue
                try:
                    engine.poll()  # Confirmation, claims and marks continue during entry pauses.
                    self.errors.pop("poll:" + name, None)
                except Exception as exc:
                    self.errors["poll:" + name] = type(exc).__name__
                    LOG.error("MAIN-R1 %s confirmation unavailable (%s)", name, type(exc).__name__)
                finally:
                    engine.lock.release()
            self.stop.wait(.1)

    def _settle_once(self):
        now = self.clock()
        due = set(self.cohorts.due_rounds(now))
        for engine in self.engines.values():
            with engine.store._lock:
                due.update(row[0] for row in engine.store.conn.execute("""SELECT DISTINCT m.round_id
                    FROM market_rounds m JOIN trades t USING(round_id)
                    WHERE m.ends_at<=? AND (t.status='OPEN' OR m.outcome IS NULL)""", (now,)))
        for round_id in sorted(due):
            if self.stop.is_set():
                break
            if self.retry.get(round_id, 0) > self.clock():
                continue
            self.retry[round_id] = self.clock() + 15
            result = self.resolver.get_resolution(round_id)
            if not official_resolution(round_id, result):
                continue
            self.cohorts.record_resolution(round_id, result)
            for engine in self.engines.values():
                with engine.lock:
                    engine.store.settle_round_outcome(round_id, result["outcome"], self.clock(),
                        result.get("final_price"), result.get("target_price"), "polymarket_official")
        self.retry = {key: value for key, value in self.retry.items() if key in due}
        return self.cohorts.flush_completed()

    def _settle(self):
        while not self.stop.is_set():
            try:
                self._settle_once()
                self.errors.pop("settlement", None)
            except Exception as exc:
                self.errors["settlement"] = type(exc).__name__
                LOG.error("MAIN-R1 official settlement unavailable (%s)", type(exc).__name__)
            self.stop.wait(4)

    def start(self):
        if self._started or self._closed:
            raise RuntimeError("MAIN-R1 runner cannot be started twice or after closing")
        self.feed.start()
        self._started = True
        for task in (self._drive, self._poll, self._settle):
            thread = threading.Thread(target=task, name="main-r1" + task.__name__, daemon=True)
            thread.start()
            self.threads.append(thread)

    def _account_snapshot(self, name):
        engine = self.engines[name]
        execution = engine.snapshot()  # Cached risk state; never evaluates or submits on GET.
        risk_state = execution.get("risk_state")
        if risk_state is None:
            risk_state = engine.guard.snapshot()
        with engine.store._lock:
            account = engine.store.account()
            counts = engine.store.conn.execute("""SELECT count(*) trade_count,
                coalesce(sum(CASE WHEN status='OPEN' THEN stake ELSE 0 END),0) open_cost
                FROM trades""").fetchone()
            settled = engine.store.conn.execute("""SELECT count(*) FROM
                (SELECT round_id FROM trades GROUP BY round_id HAVING count(*)=sum(status='SETTLED'))""").fetchone()[0]
        return {"key": name, "label": LABEL if name == "portfolio" else name,
                "role": "组合" if name == "portfolio" else "独立影子",
                "strategy_id": engine.strategy_id, "initial_balance": account["initial_balance"],
                "cash_balance": account["cash_balance"], "realized_pnl": account["realized_pnl"],
                "open_cost": counts["open_cost"], "trade_count": counts["trade_count"],
                "settled_rounds": settled, "execution": execution,
                "risk": {"checkpoint": execution.get("risk"), "state": risk_state}}

    def snapshot(self):
        frame = self.feed.capture()
        # Capture itself takes time. Comparing its timestamp with a clock read
        # taken before capture would falsely classify a fresh frame as future data.
        now = self.clock()
        ready = common_inputs_ready(frame, now)
        feed = {"ready": ready, "round_id": frame.market.round_id if frame.market else None,
                "reference_ready": bool(frame.reference.get("ready") and frame.reference.get("protocol_verified")),
                "quote_sides": list(frame.valid_quotes(now)), "error": frame.error,
                "last_market_error": self.feed.last_error,
                "ws_state": self.feed.books.health().get("ws_state")}
        accounts = [self._account_snapshot(name) for name in ("portfolio", *CANDIDATES)]
        blockers = []
        for account in accounts:
            execution = account["execution"]
            unknown = max(execution.get("unknown_order_count", 0), execution.get("counts", {}).get("UNKNOWN", 0))
            halts = list(account["risk"]["state"].get("persistent_halts", []))
            if unknown or halts:
                reasons = ([f"{unknown} 笔 UNKNOWN 订单保留预算，等待证据核对"] if unknown else [])
                reasons.extend(f"持久停止：{reason}" for reason in halts)
                blockers.append({"key": account["key"], "unknown_order_count": unknown,
                                 "persistent_halts": halts, "reasons": reasons})
        portfolio_blocker = next((item for item in blockers if item["key"] == "portfolio"), None)
        shadow_blockers = [item for item in blockers if item["key"] != "portfolio"]
        execution_halted, learning_blocked = bool(portfolio_blocker), bool(shadow_blockers)
        router = self.selector.status()
        cohorts = self.cohorts.snapshot()
        state = "stopped" if self._closed else "waiting_start" if now < self.run["start_at"] else (
            "degraded" if self.errors or (self._started and (self.last_step is None or now-self.last_step > 20))
            else "execution_halted" if execution_halted else "learning_blocked" if learning_blocked
            else "waiting_entry_window" if ready and now < frame.market.ends_at-self.settings.max_time_left_seconds
            else "running" if ready else "waiting_data")
        if learning_blocked:
            learning_state, learning_reason = "blocked", "影子账户存在未知订单或持久停止，无法继续形成完整共同学习样本；等待审计处理。"
        elif state in ("stopped", "waiting_start", "degraded", "waiting_entry_window", "waiting_data"):
            learning_state = state
            learning_reason = {"stopped": "实验已停止。", "waiting_start": "等待实验开始。",
                               "degraded": "运行异常，先检查错误与循环状态。", "waiting_entry_window": "等待本轮共同入场窗口。",
                               "waiting_data": "等待合格行情；缺失数据不能计为正常空仓样本。"}[state]
        elif (router.get("last_decision") or {}).get("candidate_key"):
            learning_state, learning_reason = "candidate_selected", "本轮已选出候选；完整共同机会仍需确认与官方结算。"
        else:
            learning_state, learning_reason = "collecting", "冷启动或选择条件尚未满足；影子账户继续积累并检验合格共同样本。"
        return {"strategy_id": KEY, "name": LABEL, "mode": "PAPER_ONLY", "live_enabled": False,
                "state": state, "as_of": now, "start_at": self.run["start_at"], "last_step": self.last_step,
                "config_id": self.run["config_id"], "provenance": self.run["config"]["provenance"],
                "runtime": self.run["runtime"], "execution_model": self.run["execution_model"],
                "execution_halted": execution_halted, "learning_blocked": learning_blocked,
                "blocking": {"portfolio": portfolio_blocker, "shadows": shadow_blockers,
                             "blocked_shadow_count": len(shadow_blockers),
                             "unknown_order_count": sum(item["unknown_order_count"] for item in blockers)},
                "learning": {"state": learning_state, "reason": learning_reason,
                             "learned_cohorts": router.get("cohorts", 0),
                             "selector_reason": (router.get("last_decision") or {}).get("reason")},
                "feed": feed, "errors": dict(self.errors), "portfolio": accounts[0], "shadows": accounts[1:],
                "router": router, "cohorts": cohorts}

    def close(self):
        if self._closed:
            return
        self.stop.set()
        deadline = time.monotonic() + 35
        for thread in self.threads:
            thread.join(timeout=max(0, deadline-time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            # Keep connection/lock ownership until process exit, not beneath a live writer.
            raise RuntimeError("MAIN-R1 worker did not stop; ledger resources remain protected")
        if self.feed is not None:
            self.feed.close()
        for wallet in self.wallets.values():
            with wallet.store._lock:
                wallet.store.conn.close()
        if self.exchange is not None:
            self.exchange.close()
        if self.writer_lock is not None:
            self.writer_lock.close()
        self._closed = True


def main(argv=None):
    from .main_r1_web import serve
    parser = argparse.ArgumentParser(description="MAIN-R1 independent adaptive Paper experiment; no live execution")
    parser.add_argument("--config", type=Path, required=True, help="Complete allowlisted MAIN baseline JSON")
    parser.add_argument("--source", type=Path, required=True, help="Existing read-only market-data directory")
    parser.add_argument("--data-dir", type=Path, required=True, help="Independent MAIN-R1 experiment directory")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "::1", "localhost"))
    parser.add_argument("--port", type=int, default=8794)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    instance = server = None
    try:
        instance = MainR1Runner(args.source, args.data_dir, read_json(args.config))
        server = serve((args.host, args.port), instance.snapshot)

        def stop(*_):
            instance.stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, stop)
        instance.start()
        LOG.info("MAIN-R1 PAPER_ONLY ready at http://%s:%s; independent data directory %s", args.host, args.port, args.data_dir)
        server.serve_forever()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        LOG.error("MAIN-R1 stopped (%s): %s", type(exc).__name__, exc)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if instance is not None:
            instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prospective paired Paper experiments. All writable state belongs to this service."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time
import urllib.request

from .config import Settings
from .fast_floor_trial import FastFloorPaperExecution
from .forecast_confirm_trial import ForecastConfirmTrialExecution
from .forecast_execution import ForecastPaperExecution
from .forecast_paper import ForecastPaperPolicy
from .managed_live_client import ManagedExchange
from .optimized_main import StrictTwapMarketClient
from .paper_execution import ExecutionPolicy, PaperExecution
from .storage import TradeStore
from .trial_diagnostics import CandidateDiagnostics
from .trial_market_feed import TrialFeed

LOG = logging.getLogger(__name__)
FAST = "MAIN_OPTIMIZED_FAST"
FORECAST = "FORECAST_PAPER"
ARMS = (
    ("fast_control", "极速版 · 原规则", FAST, PaperExecution),
    ("fast_floor", "极速版 · 含费买价至少60美分", FAST, FastFloorPaperExecution),
    ("forecast_control", "方向标准版 · 原规则", FORECAST, ForecastPaperExecution),
    ("forecast_confirm", "方向标准版 · 溢价卖二次确认", FORECAST, ForecastConfirmTrialExecution),
)


def campaign(root):
    """Freeze both controls' actual Paper rules and latency settings before the first round."""
    path = root / "campaign.json"
    if path.exists():
        return json.loads(path.read_text())
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open("http://127.0.0.1:8791/api/status", timeout=12) as response:
        source = json.load(response)
    accounts = {a["strategy_id"]: a for a in source["runtime"]["paper_execution"]["accounts"]}
    setup = {k: {"rule": accounts[k]["rules"], "policy": accounts[k]["policy"]} for k in (FAST, FORECAST)}
    # Use only decision parameters exposed by the source instance. No paths or credentials.
    params = {k: source["settings"][k] for k in ("tick_seconds", "min_edge", "min_confidence",
              "max_entry_price", "paper_taker_fee_rate", "round_seconds", "paper_entry_order_type")
              if k in source["settings"]}
    # The original status API omits this fast-strategy gate. Its effective value was
    # verified in the running source container and is pinned in the trial manifest.
    distance = float(os.environ["POLYBOT2OTHER_MIN_PRICE_DISTANCE_BPS"])
    if not math.isfinite(distance) or distance < 0:
        raise ValueError("Invalid source strategy minimum price distance")
    params["min_price_distance_bps"] = distance
    run = {"version": 2, "start_at": math.ceil(time.time()/300)*300, "accounts": setup, "settings": params}
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps(run, ensure_ascii=False))
    os.replace(tmp, path)
    return run


class TrialWallet:
    """The small subset of the Paper bot API consumed by the original execution engines."""
    def __init__(self, path, feed, stop):
        path.parent.mkdir(exist_ok=True, parents=True)
        self.store = TradeStore(path, 100)
        self.feed, self._stop = feed, stop
        self._lock = threading.RLock()
        self.paper_trading_paused = False
        self.first_frame = None
        self.saved_market = None
        with self.store._lock, self.store.conn:
            self.store.conn.execute("CREATE TABLE IF NOT EXISTS paired_entry_cycles "
                                    "(order_id INTEGER PRIMARY KEY, cycle INTEGER NOT NULL)")

    def _sync_shared_market_inputs(self):
        frame = self.first_frame or self.feed.capture()
        self.first_frame = None
        if frame.market and frame.market != self.saved_market:
            self.store.upsert_round(frame.market)
            self.saved_market = frame.market
        return frame


class Arm:
    def __init__(self, root, spec, settings, run, feed, stop):
        self.key, self.label, strategy, engine_type = spec
        self.bot = TrialWallet(root / self.key / "paper.sqlite3", feed, stop)
        self.store = self.bot.store
        definition = run["accounts"][strategy]
        self.rule = definition["rule"]
        self.engine = engine_type(self.bot, strategy, settings,
            frame_reader=feed.capture, info_reader=feed.exchange.market_info, book_reader=feed.exchange.rest_book,
            config_reader=lambda: {"revision": 1, "strategies": {strategy: self.rule}}, feed=feed,
            policy=ExecutionPolicy(**definition["policy"]))

    def decide(self, frame, cycle):
        with self.store._lock:
            last = self.store.conn.execute("SELECT coalesce(max(id),0) FROM paper_orders").fetchone()[0]
        self.bot.first_frame = frame
        try:
            self.engine.decide()
        finally:
            self.bot.first_frame = None
        with self.store._lock, self.store.conn:
            self.store.conn.execute("INSERT OR IGNORE INTO paired_entry_cycles "
                "SELECT id,? FROM paper_orders WHERE id>? AND requested_cash>0", (cycle, last))

    def report(self):
        with self.store._lock:
            db = self.store.conn
            account = self.store.account()
            groups = db.execute("""SELECT round_id, sum(pnl) AS gain FROM trades GROUP BY round_id
                                HAVING count(*)=sum(status='SETTLED')""").fetchall()
            closed = {r["round_id"]: r["gain"] for r in groups}
            buys = db.execute("""SELECT o.round_id,o.side,o.cash_spent,o.filled_shares,x.cycle FROM paper_orders o
                                  LEFT JOIN paired_entry_cycles x ON x.order_id=o.id
                                  WHERE o.cash_spent>0 AND o.filled_shares>0 ORDER BY o.id""").fetchall()
            entries = {}
            for r in buys:
                entries.setdefault(r["round_id"], []).append(tuple(r)[1:])
            submitted = db.execute("SELECT count(*) FROM paper_orders WHERE requested_cash>0").fetchone()[0]
            open_cost = db.execute("SELECT coalesce(sum(stake),0) FROM trades WHERE status='OPEN'").fetchone()[0]
            details = {}
            if isinstance(self.engine, FastFloorPaperExecution):
                details = self.engine.fast_floor_summary()
            if isinstance(self.engine, ForecastConfirmTrialExecution):
                totals = db.execute("""SELECT count(*) first_seen,count(confirmed_at) confirmed,
                    count(CASE WHEN outcome='submitted' THEN 1 END) submitted FROM forecast_premium_checks""").fetchone()
                details = {**dict(totals), "latest": [dict(r) for r in db.execute("""
                    SELECT round_id,outcome,first_at,confirmed_at FROM forecast_premium_checks ORDER BY id DESC LIMIT 3""")]}
            summary = dict(key=self.key, label=self.label, pair=self.key.split("_")[0],
                role="control" if self.key.endswith("control") else "variant", initial=account["initial_balance"],
                cash=account["cash_balance"], realized=account["realized_pnl"], closed_markets=len(closed),
                buy_fills=len(buys), submitted=submitted, open_cost=open_cost, pending=len(self.engine.pending()),
                last_event=dict(self.engine.last_event), details=details, rule=self.rule)
        return summary, entries, closed


def compare(pair, control, variant):
    _, a, a_closed = control
    _, b, b_closed = variant
    both_closed = a_closed.keys() & b_closed.keys()
    same = [rid for rid in both_closed if rid in a and rid in b and a[rid] == b[rid]
            and all(entry[-1] is not None for entry in a[rid])]
    return dict(pair=pair, matched=len(same), control_pnl=round(sum(a_closed[k] for k in same), 6),
                variant_pnl=round(sum(b_closed[k] for k in same), 6), control_only=len(a.keys()-b.keys()),
                variant_only=len(b.keys()-a.keys()), different_entries=len(both_closed)-len(same))


class PairedTrials:
    def __init__(self, source, root):
        self.stop = threading.Event()
        self.run = campaign(root)
        self.start_at = self.run["start_at"]
        self.source = source
        settings = Settings(**self.run["settings"], live_trading_runtime_enabled=False,
                            managed_live_enabled=False, strategy_experiments_enabled=False,
                            llm_super_agent_enabled=False, market_scout_enabled=False, paper_realistic_execution=True)
        self.feed = TrialFeed(source, ManagedExchange(settings))
        self.resolver = StrictTwapMarketClient(settings, self.feed.references)
        self.forecast = ForecastPaperPolicy(self.feed.references)
        self.arms = [Arm(root, spec, settings, self.run, self.feed, self.stop) for spec in ARMS]
        self.diagnostics = CandidateDiagnostics(root/"diagnostics.sqlite3",
                         league_path=source/"strategy-league.sqlite3", start_at=self.start_at)
        self.interval = max(.5, settings.tick_seconds)
        self.diagnostics_error = self.settlement_error = None
        self.last_step = None
        self.threads = []

    def _drive(self):
        futures = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="trial-decision") as pool:
            while not self.stop.is_set():
                tick_at = time.monotonic()
                try:
                    frame = self.feed.capture()
                    if frame.market and frame.market.started_at >= self.start_at:
                        cycle = time.time_ns()
                        for index in (0, 2):
                            pair = self.arms[index:index+2]
                            if any(a in futures and not futures[a].done() for a in pair):
                                continue
                            for a in pair:
                                old = futures.pop(a, None)
                                if old and old.exception():
                                    LOG.error("Trial decision failed for %s: %s", a.key, type(old.exception()).__name__)
                                futures[a] = pool.submit(a.decide, frame, cycle)
                        self.last_step = time.time()
                except Exception:
                    LOG.exception("Trial decision input unavailable")
                self.stop.wait(max(.05, self.interval-(time.monotonic()-tick_at)))

    def _poll(self):
        while not self.stop.is_set():
            for a in self.arms:
                if not a.engine.lock.acquire(blocking=False):
                    continue
                try:
                    a.engine.poll()
                except Exception:
                    LOG.exception("Trial matching failed for %s", a.key)
                finally:
                    a.engine.lock.release()
            self.stop.wait(.1)

    def _diagnose(self):
        while not self.stop.is_set():
            try:
                frame = self.feed.capture()
                with self.feed.lock:
                    same_market = frame.market is not None and frame.market == self.feed.market
                    recent = 0 <= frame.observed_at - self.feed.rules_checked_at <= 65
                    info, rules = (self.feed.info, self.feed.rules) if same_market and recent else ({}, {})
                decision = self.forecast.evaluate(frame, info)
                self.diagnostics.observe(frame, decision, info, rules, now=time.time())
                self.diagnostics_error = None
            except Exception as exc:
                self.diagnostics_error = type(exc).__name__
                LOG.exception("Trial candidate diagnostics failed")
            self.stop.wait(.8)

    def _settle(self):
        retry = {}
        while not self.stop.is_set():
            try:
                due = set()
                for a in self.arms:
                    with a.store._lock:
                        due.update(r[0] for r in a.store.conn.execute("""SELECT DISTINCT t.round_id FROM trades t
                            JOIN market_rounds m USING(round_id) WHERE (t.status='OPEN' OR m.outcome IS NULL)
                            AND m.ends_at<=?""", (time.time(),)))
                        if a.engine.strategy_id == FORECAST:
                            due.update(r[0] for r in a.store.conn.execute("""SELECT DISTINCT m.round_id
                                FROM market_rounds m JOIN forecast_checkpoints c USING(round_id)
                                WHERE m.outcome IS NULL AND m.ends_at<=?""", (time.time(),)))
                for rid in sorted(due):
                    if retry.get(rid, 0) > time.time():
                        continue
                    retry[rid] = time.time()+15
                    result = self.resolver.get_resolution(rid) or {}
                    if (result.get("market_slug") != rid or result.get("price_source") != "Gamma:outcomePrices"
                            or result.get("outcome") not in ("Up", "Down")):
                        continue
                    for a in self.arms:
                        with a.engine.lock:
                            a.store.settle_round_outcome(rid, result["outcome"], time.time(), result.get("final_price"),
                                                         result.get("target_price"), "polymarket_official")
                retry = {k:v for k,v in retry.items() if k in due}
                self.settlement_error = None
            except Exception as exc:
                self.settlement_error = type(exc).__name__
                LOG.exception("Trial official settlement failed")
            self.stop.wait(4)

    def start(self):
        self.feed.start()
        for target in (self._drive, self._poll, self._settle, self._diagnose):
            t = threading.Thread(target=target, name=target.__name__, daemon=True)
            t.start()
            self.threads.append(t)

    def snapshot(self):
        now = time.time()
        frame = self.feed.capture()
        quotes = list(frame.valid_quotes(now))
        data = [a.report() for a in self.arms]
        feed = dict(round_id=frame.market.round_id if frame.market else None,
                    reference_ready=bool(frame.reference.get("ready") and frame.reference.get("protocol_verified")),
                    quote_sides=quotes, ws_state=self.feed.books.health().get("ws_state"),
                    diagnostics_error=self.diagnostics_error, settlement_error=self.settlement_error,
                    last_market_error=self.feed.last_error, last_step=self.last_step)
        state = "waiting"
        if now >= self.start_at:
            state = "collecting" if self.last_step and now-self.last_step < 10 else "degraded"
            if state == "collecting" and (not feed["reference_ready"] or not quotes):
                state = "waiting_data"
        return dict(start_at=self.start_at, as_of=now, state=state, feed=feed, accounts=[d[0] for d in data],
                    comparisons=[compare("fast", *data[:2]), compare("forecast", *data[2:])],
                    diagnostics=self.diagnostics.report())

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=30)
        self.feed.close()
        self.diagnostics.close()
        for a in self.arms:
            a.store.conn.close()


def main():
    from .paired_trial_web import serve
    logging.basicConfig(level=logging.INFO)
    root, source = Path("/trial-data/campaign-v2"), Path("/source")
    root.mkdir(exist_ok=True)
    with (root/"writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        trials = PairedTrials(source, root)
        server = serve(("127.0.0.1", 8792), trials.snapshot)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        try:
            trials.start()
            server.serve_forever()
        finally:
            server.server_close()
            trials.close()


if __name__ == "__main__":
    main()

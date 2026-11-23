"""Prospective first-strong MAIN Paper account, run as its own process."""
import argparse
from dataclasses import asdict
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time

from .config import Settings
from .first_strong_execution import BUDGET, FirstStrongExecution, KEY, LABEL, SCORE_FLOOR, SLOTS
from .managed_live_client import ManagedExchange
from .optimized_main import StrictTwapMarketClient
from .paired_trials import TrialWallet
from .paper_execution import ExecutionPolicy
from .trial_market_feed import TrialFeed

LOG = logging.getLogger(__name__)
SETTINGS_FIELDS = {
    "tick_seconds", "round_seconds", "min_confidence", "min_edge", "max_entry_price",
    "min_price_distance_bps", "max_spread", "min_time_left_seconds", "max_time_left_seconds",
    "min_ask_size", "max_quote_age_ms", "paper_taker_fee_rate", "request_timeout_seconds",
}
RULE = {"enabled": True, "stake_dollars": BUDGET, "max_open_trades": SLOTS, "max_daily_loss": 0}
VERSION = 1


def campaign(root, config_path):
    path = root / "campaign.json"
    if path.exists():
        data = json.loads(path.read_text())
        if data["version"] != VERSION or data["strategy"] != KEY or data["score_floor"] != SCORE_FLOOR or data["rule"] != RULE:
            raise ValueError("模拟研究版本与账本不匹配")
        return data
    if (root / "paper.sqlite3").exists():
        raise ValueError("已有模拟账本缺少原始研究配置")
    config = json.loads(config_path.read_text())
    s = config["settings"]
    if set(s) != SETTINGS_FIELDS or any(type(v) not in (int, float) or not math.isfinite(v) for v in s.values()):
        raise ValueError("缺少有效的主账户方向与执行参数")
    policy = ExecutionPolicy(**config["policy"])
    if (set(config["policy"]) != set(asdict(policy)) or s["round_seconds"] != 300 or s["tick_seconds"] <= 0
            or not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in asdict(policy).values())
            or policy.arrival_floor_seconds <= 0 or not 0 < policy.depth_fraction <= 1):
        raise ValueError("主账户的 Paper 执行假设不完整")
    now = time.time()
    data = {"version": VERSION, "strategy": KEY, "created_at": now, "start_at": math.ceil(now/300)*300,
            "score_floor": SCORE_FLOOR, "rule": RULE, "settings": s, "policy": asdict(policy)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
    return data


class StrongSignalPaper:
    def __init__(self, source, root, config):
        self.run = campaign(root, config)
        settings = Settings(**self.run["settings"], db_path=root / "paper.sqlite3",
                            live_trading_runtime_enabled=False, managed_live_enabled=False,
                            strategy_experiments_enabled=False, llm_super_agent_enabled=False,
                            market_scout_enabled=False, strategy_league_enabled=False, paper_realistic_execution=True)
        self.stop = threading.Event()
        self.exchange = ManagedExchange(settings)
        self.feed = TrialFeed(source, self.exchange)
        self.resolver = StrictTwapMarketClient(settings, self.feed.references)
        self.wallet = TrialWallet(settings.db_path, self.feed, self.stop)
        self.store = self.wallet.store
        self.engine = FirstStrongExecution(self.wallet, "MAIN", settings, frame_reader=self.feed.capture,
            info_reader=self.exchange.market_info, book_reader=self.exchange.rest_book,
            config_reader=lambda: {"revision": VERSION, "strategies": {"MAIN": RULE}},
            feed=self.feed, policy=ExecutionPolicy(**self.run["policy"]))
        self.interval = max(.25, settings.tick_seconds)
        self.last_step = None
        self.errors = {}
        self.threads = []

    def _decide(self):
        frame = self.feed.capture()
        if frame.market and frame.market.started_at >= self.run["start_at"]:
            self.wallet.first_frame = frame
            try:
                self.engine.decide()
            finally:
                self.wallet.first_frame = None
        self.last_step = time.time()

    def _settle(self):
        with self.store._lock:
            rounds = self.store.conn.execute("""SELECT DISTINCT t.round_id FROM trades t JOIN market_rounds m
                USING(round_id) WHERE t.status='OPEN' AND m.ends_at<=?""", (time.time(),)).fetchall()
        for row in rounds:
            rid = row["round_id"]
            data = self.resolver.get_resolution(rid) or {}
            if (data.get("market_slug") == rid and data.get("price_source") == "Gamma:outcomePrices"
                    and data.get("outcome") in ("Up", "Down")):
                with self.engine.lock:
                    self.store.settle_round_outcome(rid, data["outcome"], time.time(), data.get("final_price"),
                                                    data.get("target_price"), "polymarket_official")

    def _loop(self, name, work, period):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                work()
                self.errors.pop(name, None)
            except Exception as exc:
                if self.errors.get(name) != type(exc).__name__:
                    LOG.exception("Paper %s failed", name)
                self.errors[name] = type(exc).__name__
            self.stop.wait(max(.05, period - (time.monotonic() - started)))

    def start(self):
        self.feed.start()
        for name, work, period in (("decision", self._decide, self.interval),
                                   ("matching", self.engine.poll, .1), ("settlement", self._settle, 10)):
            t = threading.Thread(target=self._loop, args=(name, work, period), daemon=True, name="strong-"+name)
            self.threads.append(t)
            t.start()

    def snapshot(self):
        now = time.time()
        try:
            frame = self.feed.capture()
            feed = {"round_id": frame.market.round_id if frame.market else None,
                    "reference_ready": bool(frame.reference.get("ready") and frame.reference.get("protocol_verified")),
                    "quote_sides": list(frame.valid_quotes(now)), "last_step": self.last_step,
                    "last_error": frame.error or self.feed.last_error}
        except Exception as exc:
            feed = {"round_id": None, "reference_ready": False, "quote_sides": [],
                    "last_step": self.last_step, "last_error": type(exc).__name__}
        state = "collecting"
        if now < self.run["start_at"]:
            state = "waiting"
        elif self.errors or self.last_step is None or now-self.last_step > max(20, self.interval*4):
            state = "degraded"
        elif feed["last_error"] or not feed["reference_ready"] or not feed["quote_sides"]:
            state = "waiting_data"
        feed["last_error"] = ", ".join(self.errors.copy().values()) or feed["last_error"]
        with self.store._lock:
            db = self.store.conn
            wallet = self.store.account()
            trades = [dict(t) for t in db.execute("""SELECT round_id,side,opened_at,settled_at,status,
                stake,entry_price,pnl,confidence FROM trades ORDER BY (status='OPEN') DESC,opened_at DESC LIMIT 80""")]
            orders = [dict(o) for o in db.execute("""SELECT id,round_id,side,created_at,status,limit_price,
                cash_spent,fee,confidence FROM paper_orders ORDER BY id DESC LIMIT 80""")]
            count, pending, reserved, fees = db.execute("""SELECT count(*),count(CASE WHEN status='PENDING' THEN 1 END),
                coalesce(sum(CASE WHEN status='PENDING' THEN remaining_cash END),0),coalesce(sum(fee),0) FROM paper_orders""").fetchone()
            positions, cost = db.execute("SELECT count(*),coalesce(sum(stake),0) FROM trades WHERE status='OPEN'").fetchone()
            closed = db.execute("SELECT settled_at,pnl FROM trades WHERE status='SETTLED' ORDER BY settled_at,id").fetchall()
            rounds = [dict(r) for r in db.execute("SELECT * FROM first_strong_rounds ORDER BY last_seen DESC LIMIT 30")]
        equity = wallet["initial_balance"]
        curve = [{"at": self.run["start_at"], "equity": equity}]
        wins = 0
        for at, pnl in closed:
            equity = round(equity + pnl, 6)
            curve.append({"at": at, "equity": equity})
            wins += int(pnl > 0)
        account = {"initial": wallet["initial_balance"], "cash": wallet["cash_balance"],
                   "reserved": reserved, "open_cost": cost, "realized": wallet["realized_pnl"],
                   "equity_at_cost": round(wallet["initial_balance"] + wallet["realized_pnl"], 6),
                   "fees": fees, "settled": len(closed), "wins": wins,
                   "win_rate": round(100*wins/len(closed), 2) if closed else None, "open_positions": positions,
                   "pending_orders": pending, "submitted": count}
        return {"key": KEY, "label": LABEL, "mode": "paper", "start_at": self.run["start_at"], "as_of": now,
                "state": state, "feed": feed, "rule": {**RULE, "min_confidence": SCORE_FLOOR},
                "account": account, "last_event": dict(self.engine.last_event), "equity_curve": curve,
                "recent_trades": trades, "recent_orders": orders, "recent_rounds": rounds}

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join()
        self.engine.flush_round()
        self.feed.close()
        self.exchange.http.close()
        self.store.conn.close()


def main():
    from .strong_signal_web import serve
    parser = argparse.ArgumentParser(description=LABEL+" · 独立 Paper 策略")
    parser.add_argument("--source-dir", type=Path, default=Path("/source"))
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    parser.add_argument("--config", type=Path, default=Path("/config/main.json"))
    parser.add_argument("--port", type=int, default=8793)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        trial = StrongSignalPaper(args.source_dir, args.state_dir, args.config)
        server = serve(("127.0.0.1", args.port), trial.snapshot)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        try:
            trial.start()
            server.serve_forever()
        finally:
            server.server_close()
            trial.close()


if __name__ == "__main__":
    main()

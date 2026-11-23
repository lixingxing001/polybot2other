from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from polybot2other.bot import PaperTradingBot
from polybot2other.config import Settings
from polybot2other.experiments import (
    FORECAST_EARLY_PAPER_ID, FORECAST_LIMIT_060_PAPER_ID, FORECAST_LIMIT_CONTROL_PAPER_ID,
    FORECAST_PAPER_ID, FORECAST_PAPER_NAME, FORECAST_PAPER_NAMES, selected_strategy_variants,
)
from polybot2other.forecast_observations import ForecastObservations
from polybot2other.forecast_settlement import ForecastSettlements
from polybot2other.managed_live_store import ManagedLiveStore
from polybot2other.models import MarketRound, Signal, TradeIntent
from polybot2other.optimized_main import ForecastPaperBot, OptimizedTradeStore
from polybot2other.paper_growth import PaperGrowthMonitor
from polybot2other.paper_execution import PaperExecutionService
from polybot2other.storage import TradeStore


class ForecastRegistrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(
            db_path=self.root / "parent.sqlite3", strategy_experiments_db_dir=self.root / "strategies",
            managed_live_dir=self.root / "live", btc_runtime_settings_path=self.root / "btc.json",
            market_scout_enabled=False, llm_super_agent_enabled=False, live_trading_runtime_enabled=False,
        )

    def test_realistic_paper_registration_and_net_early_sale_are_isolated(self):
        existing = [variant.variant_id for variant in selected_strategy_variants("")]
        self.assertTrue(FORECAST_PAPER_NAMES.keys().isdisjoint(existing))
        settings = replace(
            self.settings, managed_live_enabled=True, paper_realistic_execution=True,
            strategy_experiments_enabled=True, strategy_experiments_variants=",".join([*existing, *FORECAST_PAPER_NAMES]),
            initial_balance=145, stake_dollars=19, max_open_trades=3, max_daily_loss=53,
        )
        main = TradeStore(settings.db_path, settings.initial_balance)
        live = ManagedLiveStore(self.root / "live.sqlite3")
        self.addCleanup(main.conn.close)
        self.addCleanup(live.conn.close)
        live_before = live.config()
        self.assertTrue(FORECAST_PAPER_NAMES.keys().isdisjoint(live_before["strategies"]))
        manager = SimpleNamespace(
            store=live, market_input_snapshot=Mock(), exchange=Mock(),
            feed=SimpleNamespace(references=SimpleNamespace(snapshot=lambda: {}), polymarket=Mock()),
        )
        with patch("polybot2other.managed_live.ManagedLiveAccounts", return_value=manager):
            parent = PaperTradingBot(settings, main)
        for child in parent.strategy_experiments._bots.values():
            self.addCleanup(child.store.conn.close)
            self.addCleanup(child.reference_feed.cache.conn.close)

        new = parent.strategy_experiments._bots[FORECAST_PAPER_ID]
        self.assertEqual(new.store.db_path, settings.strategy_experiments_db_dir / "forecast_paper.sqlite3")
        self.assertIs(new.references, manager.feed.references)
        self.assertEqual(new.store.account()["initial_balance"], 100)
        self.assertEqual(main.account()["initial_balance"], 145)
        accounts = parent.paper_execution_service.snapshot()["accounts"]
        self.assertEqual(len(accounts), 9)
        early = parent.strategy_experiments._bots[FORECAST_EARLY_PAPER_ID]
        self.assertEqual(early.store.db_path, settings.strategy_experiments_db_dir / "forecast_early_paper.sqlite3")
        self.assertEqual(early.store.account()["initial_balance"], 100)
        self.assertEqual(early.paper_execution.forecast.max_entry_seconds, 240)
        self.assertEqual(new.paper_execution.forecast.max_entry_seconds, 180)
        self.assertIs(early.references, new.references)
        self.assertFalse(early.settings.live_trading_runtime_enabled)
        self.assertIsNone(early.managed_live)
        early_account = next(a for a in accounts if a["strategy_id"] == FORECAST_EARLY_PAPER_ID)
        self.assertEqual(early_account["name"], early.strategy_label)
        self.assertIn("提前1分钟", early.strategy_label)
        forecast = next(a for a in accounts if a["strategy_id"] == FORECAST_PAPER_ID)
        self.assertEqual(forecast["name"], FORECAST_PAPER_NAME)
        rules = forecast["rules"]
        self.assertEqual(rules, early_account["rules"])
        self.assertEqual((rules["stake_dollars"], rules["max_open_trades"], rules["max_daily_loss"]), (5, 1, 15))
        self.assertFalse(new.settings.live_trading_runtime_enabled)
        self.assertFalse(new.settings.managed_live_enabled)
        self.assertIsNone(new.live_trading)
        self.assertIsNone(new.managed_live)

        for variant_id in (FORECAST_LIMIT_060_PAPER_ID, FORECAST_LIMIT_CONTROL_PAPER_ID):
            trial = parent.strategy_experiments._bots[variant_id]
            self.assertIsInstance(trial, ForecastPaperBot)
            self.assertEqual(trial.paper_execution.forecast.max_entry_seconds, 180)
            self.assertEqual(trial.store.db_path, settings.strategy_experiments_db_dir / f"{variant_id.lower()}.sqlite3")
            self.assertEqual(trial.store.account()["initial_balance"], 100)
            self.assertEqual(trial.settings.stake_dollars, 5)
            self.assertEqual(trial.settings.max_open_trades, 1)
            self.assertEqual(trial.settings.max_daily_loss, 15)
            self.assertIs(trial.references, manager.feed.references)
            self.assertFalse(trial.settings.live_trading_runtime_enabled)
            self.assertIsNone(trial.managed_live)
            self.assertIsNone(trial.live_trading)
            trial_row = next(a for a in accounts if a["strategy_id"] == variant_id)
            self.assertEqual(trial_row["name"], FORECAST_PAPER_NAMES[variant_id])
            self.assertEqual(parent.strategy_experiment_detail(variant_id)["variant"]["combo"], trial_row["name"])

        now = time.time()
        market = MarketRound("test-forecast-round", "BTC", now - 120, now + 180, 100)
        signal = Signal("BTC", "Up", 0.7, 0.5, 5, "Paper test")
        new.store.upsert_round(market)
        trade_id = new.store.place_trade(TradeIntent(market, signal, 5))
        new.store.close_trade_shares(trade_id, 10, 0.6, now=now, fee=0.12)
        growth = PaperGrowthMonitor(parent, clock=lambda: now + 1).snapshot()
        by_id = {row["strategy_id"]: row for row in growth["strategies"]}
        self.assertEqual(growth["strategy_count"], 9)
        self.assertAlmostEqual(by_id[FORECAST_PAPER_ID]["windows"]["today"]["pnl"], 0.88)
        self.assertTrue(all(row["windows"]["today"]["pnl"] == 0 for key, row in by_id.items() if key != FORECAST_PAPER_ID))
        detail = parent.strategy_experiment_detail(FORECAST_PAPER_ID)["variant"]
        self.assertEqual(detail["combo"], FORECAST_PAPER_NAME)
        self.assertEqual(detail["recent_trades_summary"]["early_exit_count"], 1)
        self.assertEqual(live.config(), live_before)
        manager.exchange.submit.assert_not_called()

    def test_new_or_resumed_pair_uses_same_first_complete_market(self):
        fresh = SimpleNamespace(store=TradeStore(self.root / "a.sqlite3", 100))
        other = SimpleNamespace(store=TradeStore(self.root / "b.sqlite3", 100))
        for account in (fresh, other):
            self.addCleanup(account.store.conn.close)
        with patch("polybot2other.paper_execution.time.time", return_value=601):
            PaperExecutionService._share_trial_start([fresh, other])
        self.assertEqual(fresh.forecast_trial_boundary, 900)
        self.assertEqual(other.forecast_trial_boundary, 900)
        with other.store.conn:
            other.store.conn.execute("CREATE TABLE forecast_limit_run(id INTEGER PRIMARY KEY, starts_at INTEGER)")
            other.store.conn.execute("INSERT INTO forecast_limit_run VALUES (1,900)")
        with patch("polybot2other.paper_execution.time.time", return_value=1234):
            PaperExecutionService._share_trial_start([fresh, other])
        self.assertEqual(fresh.forecast_trial_boundary, 900)
        self.assertEqual(other.forecast_trial_boundary, 900)

    def test_no_legacy_execution_when_realistic_engine_missing(self):
        store = OptimizedTradeStore(self.settings.db_path, 100)
        self.addCleanup(store.conn.close)
        bot = ForecastPaperBot(self.settings, store)
        self.addCleanup(bot.references.conn.close)
        with patch.object(bot, "_start_workers") as start_workers, patch.object(bot, "evaluate") as evaluate:
            bot.start()
            bot.optimized_tick()
        start_workers.assert_not_called()
        evaluate.assert_not_called()
        self.assertEqual(store.open_trades(), [])

    def test_backlog_grades_rounds_without_positions_and_limits_remote_requests(self):
        store = TradeStore(self.settings.db_path, 100)
        self.addCleanup(store.conn.close)
        ForecastObservations(store)
        now = time.time()
        for i in range(5):
            market = MarketRound(f"old-{i}", "BTC", now - 7200 + i * 300, now - 6900 + i * 300, 100)
            store.upsert_round(market)
            if i < 4:
                with store.conn:
                    store.conn.execute("INSERT INTO forecast_checkpoints(round_id,seconds_left,due_at) VALUES (?,60,?)",
                                       (market.round_id, market.ends_at - 60))
        shared = {"old-0": {"outcome":"Down", "settlement_source":"polymarket_official"},
                  "old-1": {"outcome":"Down", "settlement_source":"chainlink_fallback"}}
        pending = ForecastSettlements(store, shared.get)
        remote = Mock(side_effect=[None, {"outcome":"Down"}, {"outcome":"Up"}, {"outcome":"Up"}])
        pending._poll(now, remote)
        self.assertEqual(remote.call_args.args, ("old-1",))
        self.assertEqual(store.get_round("old-0")["outcome"], "Down")
        self.assertIsNone(store.get_round("old-1")["outcome"])
        # Retain rate limiting and pending checkpoints across process restarts.
        pending = ForecastSettlements(store, shared.get)
        pending._poll(now + 1, remote)
        self.assertEqual(remote.call_args.args, ("old-2",))
        pending._poll(now + 16, remote)
        self.assertEqual(remote.call_args.args, ("old-1",))
        self.assertEqual(store.get_round("old-1")["outcome"], "Up")
        pending._poll(now + 32, remote)
        pending._poll(now + 48, remote)
        self.assertEqual(remote.call_count, 4)
        self.assertIsNone(store.get_round("old-4")["outcome"])
        self.assertEqual(store.account()["cash_balance"], 100)
        self.assertIsNone(pending.last_error)

    def test_slow_official_lookup_runs_outside_the_trading_thread(self):
        store = TradeStore(self.settings.db_path, 100)
        self.addCleanup(store.conn.close)
        ForecastObservations(store)
        now = time.time()
        market = MarketRound("owed", "BTC", now - 500, now - 200, 100)
        store.upsert_round(market)
        with store.conn:
            store.conn.execute("INSERT INTO forecast_checkpoints(round_id,seconds_left,due_at) VALUES (?,60,?)",
                               (market.round_id, market.ends_at - 60))
        entered = threading.Event()
        release = threading.Event()
        def remote(_round_id):
            entered.set()
            release.wait(timeout=3)
            return {"outcome":"Up"}
        pending = ForecastSettlements(store)
        try:
            pending.check(now, remote)
            self.assertTrue(entered.wait(timeout=1))
            self.assertIsNone(store.get_round("owed")["outcome"])
            duplicate = Mock()
            pending.check(now + 20, duplicate)
            duplicate.assert_not_called()
        finally:
            release.set()
            pending.join()
        self.assertEqual(store.get_round("owed")["outcome"], "Up")

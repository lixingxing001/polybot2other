from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from polybot2other.config import Settings
from polybot2other.bot import (
    PaperTradingBot,
    _experiment_decision_summary,
    _experiment_profit_summary,
    _experiment_review_score,
)
from polybot2other.execution import STATUS_FILLED, STATUS_PARTIAL, simulate_fak_buy, simulate_post_only_buy, taker_fee
from polybot2other.experiments import STRATEGY_VARIANTS
from polybot2other.models import MarketRound, Signal
from polybot2other.polymarket import PolymarketClient
from polybot2other.report_snapshot import generate_strategy_experiment_report_snapshot
from polybot2other.storage import (
    SETTLEMENT_SOURCE_CHAINLINK,
    SETTLEMENT_SOURCE_EARLY_EXIT,
    SETTLEMENT_SOURCE_POLYMARKET,
    TradeStore,
)
from polybot2other.strategy import RealBtcFiveMinuteStrategy, input_from_snapshot
from polybot2other.web import _strategy_experiments_retrospective_report_html


class TradingCoreTest(unittest.TestCase):
    def test_initial_balance_defaults_to_100(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            metrics = store.metrics()
            self.assertEqual(metrics["initial_balance"], 100.0)
            self.assertEqual(metrics["cash_balance"], 100.0)
            self.assertEqual(metrics["total_pnl"], 0.0)

    def test_trade_settlement_updates_pnl_with_real_market_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-1000",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test")
            trade_id = store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            self.assertGreater(trade_id, 0)
            settled = store.settle_round_outcome(market.round_id, "Up", now)
            self.assertEqual(len(settled), 1)
            metrics = store.metrics()
            self.assertEqual(metrics["settled_trades"], 1)
            self.assertGreater(metrics["realized_pnl"], 0)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Up")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertIsNone(recent[0]["final_price"])

    def test_chainlink_fallback_settlement_records_source_and_final_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-chainlink-source",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "chainlink fallback")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())

            settled = store.settle_due_rounds({"BTC": 99.5}, now)

            self.assertEqual(len(settled), 1)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Down")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_CHAINLINK)
            self.assertAlmostEqual(recent[0]["final_price"], 99.5, places=6)

    def test_official_recheck_upgrades_matching_chainlink_fallback_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-official-match",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official match")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_due_rounds({"BTC": 99.5}, now)
            before = store.account()

            result = store.reconcile_round_official_outcome(market.round_id, "Down", now + 10)

            self.assertFalse(result["corrected"])
            self.assertEqual(result["updated_trades"], 1)
            self.assertAlmostEqual(result["cash_delta"], 0.0, places=6)
            self.assertAlmostEqual(result["pnl_delta"], 0.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], before["cash_balance"], places=6)
            self.assertAlmostEqual(store.account()["realized_pnl"], before["realized_pnl"], places=6)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Down")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertEqual(recent[0]["trade_settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertIsNone(recent[0]["final_price"])

    def test_official_recheck_corrects_mismatched_chainlink_fallback_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-official-mismatch",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official mismatch")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_due_rounds({"BTC": 99.5}, now)
            self.assertAlmostEqual(store.account()["cash_balance"], 105.0, places=6)
            self.assertAlmostEqual(store.account()["realized_pnl"], 5.0, places=6)

            result = store.reconcile_round_official_outcome(market.round_id, "Up", now + 10)

            self.assertTrue(result["corrected"])
            self.assertEqual(result["previous_outcome"], "Down")
            self.assertEqual(result["official_outcome"], "Up")
            self.assertEqual(result["updated_trades"], 1)
            self.assertAlmostEqual(result["cash_delta"], -10.0, places=6)
            self.assertAlmostEqual(result["pnl_delta"], -10.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            self.assertAlmostEqual(store.account()["realized_pnl"], -5.0, places=6)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Up")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertEqual(recent[0]["payout"], 0.0)
            self.assertEqual(recent[0]["pnl"], -5.0)
            self.assertIsNone(recent[0]["final_price"])
            self.assertIn("OFFICIAL_RECONCILE Down->Up", recent[0]["reason"])

    def test_bot_rechecks_fallback_settlement_until_official_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-bot-official-recheck",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "bot official recheck")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_due_rounds({"BTC": 99.5}, now)
            bot.polymarket.get_resolution = lambda slug: {"outcome": "Up"} if slug == market.round_id else None

            bot._reconcile_official_settlements(now + 10)

            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Up")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)

    def test_bot_broadcasts_official_resolution_to_strategy_experiments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-official-broadcast",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official broadcast")
            store.upsert_round(market)
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_due_rounds({"BTC": 99.5}, now)

            variant_bot = bot.strategy_experiments._bots["SINGLE_FAK"]
            variant_bot.store.upsert_round(market)
            variant_bot.store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            variant_bot.store.settle_due_rounds({"BTC": 99.5}, now)
            bot.polymarket.get_resolution = (
                lambda slug: {"outcome": "Up", "final_price": 101.0, "target_price": 100.0}
                if slug == market.round_id
                else None
            )

            bot._reconcile_official_settlements(now + 10)

            main_recent = store.recent_trades(1)
            self.assertEqual(main_recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            variant_detail = bot.strategy_experiment_detail("SINGLE_FAK", trade_limit=5, order_limit=5)
            variant_recent = variant_detail["recent_trades_page"]["recent_trades"]
            self.assertEqual(variant_recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(variant_bot.store.account()["cash_balance"], 95.0, places=6)
            self.assertEqual(variant_detail["variant"]["recent_trades_summary"]["official_count"], 1)
            self.assertEqual(variant_detail["variant"]["recent_trades_summary"]["chainlink_count"], 0)
            self.assertEqual(bot.strategy_experiments.snapshot()["official_broadcast_count"], 1)

    def test_bot_records_official_resolution_final_and_target_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-bot-official-price",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=99.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "bot official price")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            bot.polymarket.get_resolution = (
                lambda slug: {"outcome": "Up", "final_price": 101.25, "target_price": 100.5}
                if slug == market.round_id
                else None
            )

            bot._settle_due(now)

            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(recent[0]["final_price"], 101.25, places=6)
            self.assertAlmostEqual(recent[0]["target_price"], 100.5, places=6)

    def test_bot_backfills_missing_official_final_price_once_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-official-backfill-price",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=99.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official backfill")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_round_outcome(
                market.round_id,
                "Down",
                now,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            bot.polymarket.get_resolution = (
                lambda slug: {"outcome": "Down", "final_price": 98.75, "target_price": 100.5}
                if slug == market.round_id
                else None
            )

            bot._backfill_official_final_prices(now + 60)

            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(recent[0]["final_price"], 98.75, places=6)
            self.assertAlmostEqual(recent[0]["target_price"], 100.5, places=6)

    def test_partial_close_keeps_account_and_open_position_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-2000",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test partial close")
            trade_id = store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})())
            closed = store.close_trade_shares(trade_id, 10.0, 0.25, now + 1, "test stop")
            self.assertIsNotNone(closed)
            self.assertEqual(closed["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            open_rows = store.open_trades()
            self.assertEqual(len(open_rows), 1)
            self.assertAlmostEqual(open_rows[0]["shares"], 10.0, places=5)
            self.assertAlmostEqual(open_rows[0]["stake"], 5.0, places=5)
            metrics = store.metrics()
            self.assertAlmostEqual(metrics["cash_balance"], 92.5, places=5)
            self.assertAlmostEqual(metrics["realized_pnl"], -2.5, places=5)
            self.assertAlmostEqual(metrics["open_risk"], 5.0, places=5)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            store.settle_round_outcome(market.round_id, "Up", now + 2)
            recent = store.recent_trades(2)
            early_exit = next(row for row in recent if row["id"] == closed["id"])
            self.assertEqual(early_exit["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            metrics = store.metrics()
            self.assertAlmostEqual(metrics["cash_balance"], 102.5, places=5)
            self.assertAlmostEqual(metrics["realized_pnl"], 2.5, places=5)
            self.assertAlmostEqual(metrics["open_risk"], 0.0, places=5)

    def test_recent_trades_supports_count_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            for index in range(3):
                market = MarketRound(
                    round_id=f"btc-updown-5m-page-{index}",
                    symbol="BTC",
                    started_at=now - 60 + index,
                    ends_at=now + 120 + index,
                    target_price=100.0,
                )
                store.upsert_round(market)
                signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, f"test page {index}")
                store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 1.0})())
            self.assertEqual(store.recent_trade_count("BTC"), 3)
            first_page = store.recent_trades(limit=2, offset=0, symbol="BTC")
            second_page = store.recent_trades(limit=2, offset=2, symbol="BTC")
            self.assertEqual(len(first_page), 2)
            self.assertEqual(len(second_page), 1)
            first_ids = {row["id"] for row in first_page}
            second_ids = {row["id"] for row in second_page}
            self.assertFalse(first_ids & second_ids)

    def test_recent_trades_time_range_summary_uses_full_range_not_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            base = 1_800_000_000.0
            for index, side in enumerate(("Up", "Down", "Up")):
                market = MarketRound(
                    round_id=f"btc-updown-5m-range-{index}",
                    symbol="BTC",
                    started_at=base - 60 + index,
                    ends_at=base + index,
                    target_price=100.0,
                )
                store.upsert_round(market)
                signal = Signal("BTC", side, 0.7, 0.5, 10.0, f"test range {index}")
                store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
                outcome = "Up" if index != 1 else "Up"
                store.settle_round_outcome(market.round_id, outcome, base + index * 60)

            start_at = base - 1
            end_at = base + 61
            page = store.recent_trades(limit=1, offset=0, symbol="BTC", start_at=start_at, end_at=end_at)
            summary = store.recent_trade_summary("BTC", start_at, end_at)

            self.assertEqual(len(page), 1)
            self.assertEqual(store.recent_trade_count("BTC", start_at, end_at), 2)
            self.assertEqual(summary["total_count"], 2)
            self.assertEqual(summary["settled_count"], 2)
            self.assertEqual(summary["win_count"], 1)
            self.assertEqual(summary["loss_count"], 1)
            self.assertAlmostEqual(summary["settled_stake"], 10.0, places=6)
            self.assertAlmostEqual(summary["total_payout"], 10.0, places=6)
            self.assertAlmostEqual(summary["total_pnl"], 0.0, places=6)
            self.assertAlmostEqual(summary["roi_pct"], 0.0, places=6)

    def test_equity_curve_window_filters_and_downsamples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = 2_000_000.0
            start = now - 90 * 24 * 60 * 60
            rows = [
                (100.0, 0.0, 0.0, 100.0, start - 1),
                (101.0, 0.0, 1.0, 101.0, start + 1),
                (102.0, 0.0, 2.0, 102.0, start + 2),
                (103.0, 0.0, 3.0, 103.0, start + 3),
                (104.0, 0.0, 4.0, 104.0, start + 4),
                (105.0, 0.0, 5.0, 105.0, start + 5),
                (106.0, 0.0, 6.0, 106.0, now),
                (107.0, 0.0, 7.0, 107.0, now + 1),
            ]
            with store.conn:
                store.conn.executemany(
                    """
                    INSERT INTO equity_curve(cash_balance, open_risk, realized_pnl, total_equity, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            curve = store.equity_curve_window(days=90, max_points=4, now=now)
            self.assertLessEqual(len(curve), 4)
            self.assertEqual(curve[0]["created_at"], start + 1)
            self.assertEqual(curve[-1]["created_at"], now)
            self.assertTrue(all(start <= row["created_at"] <= now for row in curve))
            self.assertEqual(curve[-1]["total_pnl"], 6.0)

    def test_bot_equity_curve_window_supports_strategy_experiment_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            variant_bot = bot.strategy_experiments._bots["SINGLE_FAK"]
            now = time.time()
            with variant_bot.store.conn:
                variant_bot.store.conn.execute(
                    """
                    INSERT INTO equity_curve(cash_balance, open_risk, realized_pnl, total_equity, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (105.0, 0.0, 5.0, 105.0, now),
                )

            main_curve = bot.equity_curve_window(account_scope="main", days=90, max_points=10)
            variant_curve = bot.equity_curve_window(
                account_scope="strategy_experiment",
                variant_id="SINGLE_FAK",
                days=90,
                max_points=10,
            )

            self.assertEqual(main_curve["equity_curve_meta"]["account_scope"], "main")
            self.assertEqual(main_curve["equity_curve_meta"]["label"], "主账户")
            self.assertEqual(variant_curve["equity_curve_meta"]["account_scope"], "strategy_experiment")
            self.assertEqual(variant_curve["equity_curve_meta"]["variant_id"], "SINGLE_FAK")
            self.assertEqual(variant_curve["equity_curve_meta"]["combo"], "SINGLE + FAK")
            self.assertTrue(any(row["total_equity"] == 105.0 for row in variant_curve["equity_curve"]))
            with self.assertRaises(ValueError):
                bot.equity_curve_window(account_scope="strategy_experiment", variant_id="bad-variant")
            with self.assertRaises(ValueError):
                bot.equity_curve_window(account_scope="bad-scope")

    def test_snapshot_keeps_unrealized_pnl_out_of_total_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-unrealized",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test unrealized")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})())
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.2, "best_ask": 0.21, "updated_at_ms": int(now * 1000)},
                }
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}

            metrics = bot.snapshot()["metrics"]
            self.assertAlmostEqual(metrics["cash_balance"], 90.0, places=5)
            self.assertAlmostEqual(metrics["open_risk"], 10.0, places=5)
            self.assertAlmostEqual(metrics["total_equity"], 100.0, places=5)
            self.assertAlmostEqual(metrics["total_pnl"], 0.0, places=5)
            self.assertAlmostEqual(metrics["unrealized_pnl"], -6.0, places=5)
            self.assertAlmostEqual(metrics["open_mark_value"], 4.0, places=5)
            self.assertAlmostEqual(metrics["estimated_total_equity"], 94.0, places=5)
            self.assertAlmostEqual(metrics["estimated_total_pnl"], -6.0, places=5)

    def test_fak_execution_charges_fee_and_caps_by_top_ask_size(self) -> None:
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-fak",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 120,
            target_price=100.0,
        )
        signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test fak")
        intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})()

        result = simulate_fak_buy(intent, {"best_ask": 0.5, "ask_size": 5.0}, taker_fee_rate=0.07)

        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        self.assertAlmostEqual(fill.shares, 5.0, places=6)
        self.assertAlmostEqual(fill.notional, 2.5, places=6)
        self.assertAlmostEqual(fill.fee, 0.0875, places=6)
        self.assertAlmostEqual(fill.cash_spent, 2.5875, places=6)

    def test_fak_execution_walks_multiple_ask_levels_when_limit_allows(self) -> None:
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-fak-depth",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 120,
            target_price=100.0,
        )
        signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "test fak depth")
        intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()

        result = simulate_fak_buy(
            intent,
            {
                "best_ask": 0.34,
                "ask_size": 5.0,
                "asks": [
                    {"price": 0.34, "size": 5.0},
                    {"price": 0.45, "size": 100.0},
                ],
            },
            taker_fee_rate=0.07,
            limit_price=0.45,
        )

        self.assertEqual(result.status, STATUS_FILLED)
        fill = result.fills[0]
        self.assertGreater(fill.shares, 5.0)
        self.assertGreater(fill.fill_price, 0.34)
        self.assertLessEqual(fill.cash_spent, 5.000001)
        self.assertIn("levels 2", fill.reason)
        self.assertIn("avg", fill.reason)

    def test_post_only_marketable_order_rejects_instead_of_taking_liquidity(self) -> None:
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-post-only",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 120,
            target_price=100.0,
        )
        signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "test post only")
        intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})()

        result = simulate_post_only_buy(intent, {"best_ask": 0.34, "ask_size": 100})

        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.fills, [])
        self.assertIn("会立即吃到卖一", result.reason)

    def test_bot_fak_entry_records_fee_in_open_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_confidence=0.55, min_edge=0.0, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-fak-bot",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                }

            bot._run_strategy_from_state()

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["side"], "Up")
            self.assertAlmostEqual(row["stake"], 5.0, places=5)
            self.assertLess(row["shares"], 5.0 / 0.54)
            self.assertIn("FAK FILLED", row["reason"])
            self.assertIn("fee", row["reason"])

    def test_bot_fak_entry_uses_multi_level_average_with_edge_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_edge=0.0, max_entry_price=0.8, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-depth-bot",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "manual depth signal")
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.34,
                        "ask_size": 1.0,
                        "asks": [
                            {"price": 0.34, "size": 1.0},
                            {"price": 0.45, "size": 100.0},
                        ],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertGreater(rows[0]["entry_price"], 0.34)
            self.assertIn("levels 2", rows[0]["reason"])
            self.assertIn("limit 0.7000", rows[0]["reason"])

    def test_bot_fetches_rest_depth_when_snapshot_has_only_best_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_edge=0.0, max_entry_price=0.8, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-depth-rest",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "manual best-only signal")
            bot.polymarket.get_quote = lambda _token, _side: type(
                "Quote",
                (),
                {
                    "to_dict": lambda _self: {
                        "token_id": "up-token",
                        "outcome": "Up",
                        "best_bid": 0.33,
                        "best_ask": 0.34,
                        "bid_size": 10,
                        "ask_size": 1,
                        "asks": [{"price": 0.34, "size": 1}, {"price": 0.45, "size": 100}],
                        "bids": [{"price": 0.33, "size": 10}],
                        "updated_at_ms": int(now * 1000),
                        "source": "rest",
                    }
                },
            )()
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.33, "best_ask": 0.34, "ask_size": 1, "updated_at_ms": int(now * 1000)}
                }

            bot._maybe_place_trade(market, signal)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertIn("levels 2", rows[0]["reason"])
            self.assertGreater(rows[0]["entry_price"], 0.34)

    def test_storage_records_paper_order_and_each_fill_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-audit",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "audit signal")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            result = simulate_fak_buy(
                intent,
                {
                    "best_ask": 0.34,
                    "ask_size": 1.0,
                    "asks": [{"price": 0.34, "size": 1.0}, {"price": 0.45, "size": 100.0}],
                },
                taker_fee_rate=0.07,
                limit_price=0.45,
            )

            trade_ids = store.place_execution_result(intent, result)

            self.assertEqual(len(trade_ids), 1)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertEqual(orders[0]["order_type"], "FAK")
            self.assertEqual(orders[0]["fill_count"], 2)
            self.assertEqual(orders[0]["trade_id"], trade_ids[0])
            fills = store.paper_order_fills(orders[0]["id"])
            self.assertEqual([row["price"] for row in fills], [0.34, 0.45])
            self.assertAlmostEqual(sum(row["cash_spent"] for row in fills), orders[0]["cash_spent"], places=5)

    def test_storage_records_rejected_order_without_open_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-reject",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "audit reject")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            result = simulate_post_only_buy(intent, {"best_ask": 0.34})

            trade_ids = store.place_execution_result(intent, result)

            self.assertEqual(trade_ids, [])
            self.assertEqual(store.open_trades(), [])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["status"], "REJECTED")
            self.assertEqual(orders[0]["fill_count"], 0)
            self.assertAlmostEqual(orders[0]["cash_spent"], 0.0, places=6)

    def test_bot_orders_page_and_order_fills_are_paginated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-page",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            for index in range(3):
                signal = Signal("BTC", "Up", 0.7, 0.34 + index * 0.01, 10.0, f"audit page {index}")
                intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 2.0})()
                result = simulate_fak_buy(
                    intent,
                    {
                        "best_ask": 0.34 + index * 0.01,
                        "ask_size": 100.0,
                        "asks": [{"price": 0.34 + index * 0.01, "size": 100.0}],
                    },
                    taker_fee_rate=0.07,
                    limit_price=0.5,
                )
                store.place_execution_result(intent, result)

            page = bot.orders_page(limit=2, offset=1)

            self.assertEqual(page["recent_orders_meta"]["total"], 3)
            self.assertEqual(page["recent_orders_meta"]["loaded"], 3)
            self.assertTrue(page["recent_orders_meta"]["has_more"] is False)
            self.assertEqual(len(page["recent_orders"]), 2)
            first_order_id = page["recent_orders"][0]["id"]
            fills = bot.order_fills(first_order_id)
            self.assertEqual(fills["order_id"], first_order_id)
            self.assertEqual(len(fills["fills"]), 1)
            self.assertGreater(fills["fills"][0]["cash_spent"], 0)

    def test_orders_page_filters_by_paper_order_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-filter",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.34,
                        "best_ask": 0.36,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.36, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot._maybe_place_trade(market, Signal("BTC", "Up", 0.7, 0.35, 10.0, "active filter"))
            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.36, -10.0, "cancel filter"))

            down_order_id = next(order["id"] for order in store.active_paper_orders("BTC") if order["side"] == "Down")
            store.cancel_paper_order(down_order_id, "filter setup", now=now)
            fill_intent = type("Intent", (), {"market": market, "signal": Signal("BTC", "Up", 0.8, 0.34, 12.0, "filled filter"), "stake_dollars": 2.0})()
            fill_result = simulate_fak_buy(
                fill_intent,
                {"best_ask": 0.34, "ask_size": 100.0, "asks": [{"price": 0.34, "size": 100.0}]},
                taker_fee_rate=0.0,
                limit_price=0.5,
            )
            store.place_execution_result(fill_intent, fill_result)

            active_page = bot.orders_page(status_filter="active")
            canceled_page = bot.orders_page(status_filter="canceled")
            filled_page = bot.orders_page(status_filter="filled")
            all_page = bot.orders_page(status_filter="all")

            self.assertEqual(all_page["recent_orders_meta"]["total"], 3)
            self.assertEqual(active_page["recent_orders_meta"]["status_filter"], "active")
            self.assertEqual([row["status"] for row in active_page["recent_orders"]], ["RESTING"])
            self.assertEqual([row["status"] for row in canceled_page["recent_orders"]], ["CANCELED"])
            self.assertEqual([row["status"] for row in filled_page["recent_orders"]], ["FILLED"])
            with self.assertRaises(ValueError):
                bot.orders_page(status_filter="bad-status")

    def test_post_only_rests_reserves_cash_and_later_fills_as_maker_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-resting-fill",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "maker signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            self.assertEqual(store.open_trades(), [])
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 5.0, places=6)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertEqual(orders[0]["post_only"], 1)
            self.assertAlmostEqual(orders[0]["limit_price"], 0.33, places=6)
            order_id = int(orders[0]["id"])
            with store.conn:
                store.conn.execute(
                    "UPDATE paper_orders SET created_at = ?, updated_at = ? WHERE id = ?",
                    (now - 30.0, now - 30.0, order_id),
                )

            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.32,
                        "best_ask": 0.33,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.33, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }
            bot._manage_resting_orders(market, bot.latest_quotes)

            self.assertEqual(store.open_trades(), [])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")

            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.31,
                        "best_ask": 0.32,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.32, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }
            bot._manage_resting_orders(market, bot.latest_quotes)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["entry_price"], 0.33, places=6)
            self.assertIn("POST_ONLY_QUEUE_FILL", rows[0]["reason"])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "PARTIAL_RESTING")
            self.assertGreater(orders[0]["remaining_cash"], 0.0)
            self.assertLess(orders[0]["remaining_cash"], 5.0)
            self.assertGreater(store.metrics()["reserved_cash"], 0.0)
            self.assertLess(store.metrics()["reserved_cash"], 5.0)
            fills = store.paper_order_fills(orders[0]["id"])
            self.assertEqual(len(fills), 1)
            self.assertAlmostEqual(fills[0]["fee"], 0.0, places=6)

    def test_gtc_resting_order_can_fill_without_post_only_queue_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="GTC", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-gtc-resting-fill",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "gtc maker signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertEqual(orders[0]["order_type"], "GTC")
            self.assertEqual(orders[0]["post_only"], 0)
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.32,
                        "best_ask": 0.33,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.33, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }
            bot._manage_resting_orders(market, bot.latest_quotes)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["entry_price"], 0.33, places=6)
            self.assertIn("RESTING_FILL", rows[0]["reason"])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertAlmostEqual(orders[0]["remaining_cash"], 0.0, places=6)

    def test_resting_order_partial_fills_update_existing_trade_and_release_dust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="GTC", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-resting-dust-release",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "dust resting")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            store.record_paper_order(
                intent,
                order_type="GTC",
                status="RESTING",
                side="Up",
                limit_price=0.5,
                requested_cash=5.0,
                reason="resting dust",
            )

            first_order = store.active_paper_orders("BTC")[0]
            first_fill = store.fill_resting_order(
                first_order,
                fill_price=0.5,
                shares=5.0,
                notional=2.5,
                fee=0.0,
                cash_spent=2.5,
                level_price=0.49,
                reason="RESTING_FILL first",
                now=now + 1,
            )
            self.assertIsNotNone(first_fill)
            second_order = store.active_paper_orders("BTC")[0]
            second_fill = store.fill_resting_order(
                second_order,
                fill_price=0.5,
                shares=4.92,
                notional=2.46,
                fee=0.0,
                cash_spent=2.46,
                level_price=0.49,
                reason="RESTING_FILL second",
                now=now + 2,
            )

            self.assertIsNotNone(second_fill)
            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["stake"], 4.96, places=6)
            self.assertAlmostEqual(rows[0]["shares"], 9.92, places=6)
            self.assertAlmostEqual(rows[0]["entry_price"], 0.5, places=6)
            self.assertIn("RESTING_FILL first", rows[0]["reason"])
            self.assertIn("RESTING_FILL second", rows[0]["reason"])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertAlmostEqual(orders[0]["remaining_cash"], 0.0, places=6)
            self.assertAlmostEqual(orders[0]["cash_spent"], 4.96, places=6)
            self.assertIn("DUST_RELEASE", orders[0]["reason"])
            self.assertAlmostEqual(store.account()["cash_balance"], 95.04, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)
            fills = store.paper_order_fills(orders[0]["id"])
            self.assertEqual(len(fills), 2)
            self.assertEqual({fill["trade_id"] for fill in fills}, {rows[0]["id"]})

    def test_resting_order_tiny_initial_fill_does_not_create_dust_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-resting-tiny-initial",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "tiny initial")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            store.record_paper_order(
                intent,
                order_type="POST_ONLY",
                status="RESTING",
                side="Up",
                limit_price=0.5,
                requested_cash=5.0,
                post_only=True,
                reason="tiny initial",
            )

            order = store.active_paper_orders("BTC")[0]
            result = store.fill_resting_order(
                order,
                fill_price=0.5,
                shares=0.01,
                notional=0.005,
                fee=0.0,
                cash_spent=0.005,
                level_price=0.49,
                reason="RESTING_FILL tiny",
                now=now + 1,
            )

            self.assertIsNone(result)
            self.assertEqual(store.open_trades(), [])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertAlmostEqual(orders[0]["remaining_cash"], 5.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)

    def test_gtd_resting_order_expires_and_releases_reserved_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="GTD", paper_gtd_seconds=1.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-gtd-expire",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "gtd signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertLessEqual(orders[0]["expires_at"], now + 2.0)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)

            expired = store.expire_resting_orders(float(orders[0]["expires_at"]) + 0.1)

            self.assertEqual(len(expired), 1)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "EXPIRED")
            self.assertAlmostEqual(store.account()["cash_balance"], 100.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)

    def test_cancel_resting_order_releases_reserved_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-cancel-resting",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "cancel signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)
            order_id = store.recent_paper_orders(10, 0, "BTC")[0]["id"]

            result = bot.cancel_order(order_id)

            self.assertEqual(result["canceled"], [order_id])
            self.assertEqual(result["not_canceled"], {})
            self.assertAlmostEqual(result["released_cash"], 5.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 100.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "CANCELED")
            self.assertIn("CANCELED", orders[0]["reason"])

            second = bot.cancel_order(order_id)
            self.assertEqual(second["canceled"], [])
            self.assertIn(str(order_id), second["not_canceled"])

    def test_cancel_orders_scopes_current_market_and_all_active_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            current_market = MarketRound(
                round_id="btc-updown-5m-batch-current",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            other_market = MarketRound(
                round_id="btc-updown-5m-batch-other",
                symbol="BTC",
                started_at=now - 30,
                ends_at=now + 150,
                target_price=101.0,
            )

            def place_resting_order(market: MarketRound, side: str) -> None:
                store.upsert_round(market)
                with bot._lock:
                    bot.current_market = market
                    bot.latest_quotes = {
                        side: {
                            "best_bid": 0.33,
                            "best_ask": 0.35,
                            "bid_size": 100,
                            "ask_size": 100,
                            "asks": [{"price": 0.35, "size": 100}],
                            "updated_at_ms": int(now * 1000),
                        }
                    }
                bot._maybe_place_trade(market, Signal("BTC", side, 0.7, 0.35, 10.0, f"{side} batch cancel"))

            place_resting_order(current_market, "Up")
            place_resting_order(current_market, "Down")
            place_resting_order(other_market, "Up")
            with bot._lock:
                bot.current_market = current_market

            self.assertEqual(len(store.active_paper_orders("BTC")), 3)
            self.assertEqual(len(store.active_paper_orders("BTC", current_market.round_id)), 2)
            self.assertAlmostEqual(store.account()["cash_balance"], 85.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 15.0, places=6)

            current_result = bot.cancel_orders("current_market")

            self.assertEqual(len(current_result["canceled"]), 2)
            self.assertEqual(current_result["not_canceled"], {})
            self.assertAlmostEqual(current_result["released_cash"], 10.0, places=6)
            remaining = store.active_paper_orders("BTC")
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["round_id"], other_market.round_id)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 5.0, places=6)

            all_result = bot.cancel_orders("all")

            self.assertEqual(len(all_result["canceled"]), 1)
            self.assertEqual(store.active_paper_orders("BTC"), [])
            self.assertAlmostEqual(all_result["released_cash"], 5.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 100.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)

    def test_live_snapshot_does_not_promote_client_fallback_target_to_market_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_confidence=0.55, min_edge=0.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-4000",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=0.0,
                slug="btc-updown-5m-4000",
            )
            bot.polymarket.find_current_btc_5m_market = lambda: market
            payload = {
                "market": {"slug": market.round_id},
                "target_price": 100.0,
                "price": {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int(now * 1000),
                    "target_price": 100.0,
                    "target_price_source": "rtds-chainlink-fallback",
                    "target_price_fallback": True,
                },
                "quotes": {
                    "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                },
            }

            snapshot = bot.ingest_live_snapshot(payload)

            self.assertEqual(snapshot["runtime"]["current_market"]["target_price"], 0.0)
            self.assertNotIn("target_price", snapshot["runtime"]["latest_price"])
            self.assertEqual(snapshot["runtime"]["last_signal"]["side"], "NO_TRADE")
            self.assertIn("缺少官方目标价", snapshot["runtime"]["last_signal"]["reason"])
            self.assertEqual(store.open_trades(), [])

    def test_live_snapshot_uses_official_market_target_over_client_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_confidence=0.55, min_edge=0.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-4100",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                slug="btc-updown-5m-4100",
            )
            bot.polymarket.find_current_btc_5m_market = lambda: market
            payload = {
                "market": {"slug": market.round_id},
                "target_price": 99.0,
                "price": {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int(now * 1000),
                    "target_price": 99.0,
                    "target_price_source": "rtds-chainlink-fallback",
                    "target_price_fallback": True,
                },
                "quotes": {
                    "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                },
            }

            snapshot = bot.ingest_live_snapshot(payload)

            self.assertEqual(snapshot["runtime"]["current_market"]["target_price"], 100.0)
            self.assertEqual(snapshot["runtime"]["latest_price"]["target_price"], 100.0)
            self.assertEqual(snapshot["runtime"]["latest_price"]["target_price_source"], "market.target_price")
            self.assertFalse(snapshot["runtime"]["latest_price"]["target_price_fallback"])

    def test_pair_strategy_does_not_open_without_official_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", max_open_trades=2, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-no-target-pair",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=0.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.39, "best_ask": 0.4, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                }
            bot.set_pair_strategy_enabled(True)

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            self.assertEqual(bot.last_signal["side"], "PAIR_WAIT")
            self.assertIn("缺少官方目标价", bot.last_signal["reason"])

    def test_pair_strategy_opens_two_sides_and_exits_on_bid_sum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", max_open_trades=2, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-3000",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.39, "best_ask": 0.4, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                }
            bot.set_pair_strategy_enabled(True)
            bot._run_strategy_from_state()
            open_rows = sorted(store.open_trades(), key=lambda row: row["side"])
            self.assertEqual([row["side"] for row in open_rows], ["Down", "Up"])
            self.assertAlmostEqual(open_rows[0]["shares"], open_rows[1]["shares"], places=4)
            self.assertTrue(all("PAIR_OPEN" in row["reason"] for row in open_rows))
            self.assertTrue(all("fee" in row["reason"] for row in open_rows))

            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.5, "best_ask": 0.51, "ask_size": 100, "updated_at_ms": int(time.time() * 1000)},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "ask_size": 100, "updated_at_ms": int(time.time() * 1000)},
                }
            bot._run_strategy_from_state()
            self.assertEqual(store.open_trades(), [])
            recent = store.recent_trades(2)
            self.assertEqual(len(recent), 2)
            self.assertTrue(all("PAIR_EXIT" in row["reason"] for row in recent))
            self.assertTrue(all("fee" in row["reason"] for row in recent))
            self.assertGreater(store.metrics()["realized_pnl"], 0)

    def test_strategy_variants_cover_all_eight_target_combinations(self) -> None:
        combos = [variant.combo for variant in STRATEGY_VARIANTS]

        self.assertEqual(
            combos,
            [
                "SINGLE + FAK",
                "SINGLE + GTC",
                "SINGLE + GTD",
                "SINGLE + POST_ONLY",
                "PAIR + FAK",
                "PAIR + GTC",
                "PAIR + GTD",
                "PAIR + POST_ONLY",
            ],
        )

    def test_pair_strategy_gtd_places_two_resting_pair_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=2,
                stake_dollars=5.0,
                paper_entry_order_type="GTD",
                paper_gtd_seconds=60.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-pair-gtd",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot.set_pair_strategy_enabled(True)

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            active_orders = sorted(store.active_paper_orders("BTC"), key=lambda row: row["side"])
            self.assertEqual([row["side"] for row in active_orders], ["Down", "Up"])
            self.assertTrue(all(row["order_type"] == "GTD" for row in active_orders))
            self.assertTrue(all(row["status"] == "RESTING" for row in active_orders))
            self.assertTrue(all("PAIR_OPEN_RESTING GTD" in row["reason"] for row in active_orders))
            self.assertAlmostEqual(sum(row["requested_cash"] for row in active_orders), 5.0, places=5)
            self.assertLessEqual(active_orders[0]["expires_at"], now + 61.0)
            self.assertEqual(bot.last_signal["side"], "PAIR_RESTING")

            bot._run_strategy_from_state()

            self.assertEqual(len(store.active_paper_orders("BTC")), 2)

    def test_pair_strategy_post_only_places_two_maker_pair_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=2,
                stake_dollars=5.0,
                paper_entry_order_type="POST_ONLY",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-pair-post-only",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot.set_pair_strategy_enabled(True)

            bot._run_strategy_from_state()

            active_orders = sorted(store.active_paper_orders("BTC"), key=lambda row: row["side"])
            self.assertEqual(len(active_orders), 2)
            self.assertTrue(all(row["order_type"] == "POST_ONLY" for row in active_orders))
            self.assertTrue(all(row["post_only"] == 1 for row in active_orders))
            self.assertLess(active_orders[0]["limit_price"], 0.5)
            self.assertLess(active_orders[1]["limit_price"], 0.4)
            self.assertEqual(store.open_trades(), [])

    def test_strategy_experiments_run_all_variants_in_isolated_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                max_open_trades=2,
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-experiments",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }

            bot._run_strategy_from_state()

            snapshot = bot.strategy_experiments_snapshot()
            variants = {row["variant_id"]: row for row in snapshot["variants"]}
            self.assertEqual(len(variants), 8)
            self.assertEqual(snapshot["run_count"], 1)
            self.assertIn("profit_summary", snapshot)
            self.assertEqual(snapshot["profit_summary"]["status"], "WAITING_FOR_SAMPLE")
            self.assertIsNone(snapshot["profit_summary"]["winner_variant_id"])
            self.assertFalse(snapshot["decision_summary"]["comparison_ready"])
            self.assertEqual(snapshot["decision_summary"]["status"], "WAITING_FOR_SAMPLE")
            self.assertEqual(snapshot["decision_summary"]["ready_count"], 0)
            self.assertEqual(snapshot["decision_summary"]["total_count"], 8)
            self.assertIsNone(snapshot["decision_summary"]["recommended_variant_id"])
            self.assertIsNotNone(snapshot["decision_summary"]["current_leader_variant_id"])
            self.assertTrue(all(row["last_error"] is None for row in variants.values()))
            self.assertIn("review_score", variants["SINGLE_FAK"])
            self.assertIn("score", variants["SINGLE_FAK"]["review_score"])
            self.assertEqual(variants["SINGLE_FAK"]["review_score"]["sample_status"], "INSUFFICIENT")
            self.assertIn("结算样本不足", variants["SINGLE_FAK"]["review_score"]["reasons"][0])
            self.assertEqual(variants["SINGLE_FAK"]["metrics"]["open_trades"], 1)
            self.assertEqual(variants["PAIR_FAK"]["metrics"]["open_trades"], 2)
            self.assertEqual(variants["PAIR_GTD"]["active_orders"], 2)
            self.assertEqual(variants["PAIR_POST_ONLY"]["active_orders"], 2)
            self.assertEqual(variants["PAIR_FAK"]["order_summary"]["filled_count"], 2)
            self.assertEqual(variants["PAIR_FAK"]["order_summary"]["fill_rate"], 100.0)
            self.assertEqual(variants["PAIR_GTD"]["order_summary"]["active_count"], 2)
            self.assertEqual(variants["PAIR_GTD"]["order_summary"]["fill_rate"], 0.0)
            self.assertNotEqual(variants["SINGLE_FAK"]["db_path"], variants["PAIR_FAK"]["db_path"])

            detail = bot.strategy_experiment_detail("PAIR_GTD", trade_limit=5, order_limit=5)
            self.assertEqual(detail["variant"]["variant_id"], "PAIR_GTD")
            self.assertEqual(detail["variant"]["order_summary"]["total_count"], 2)
            self.assertEqual(detail["recent_orders_page"]["recent_orders_meta"]["total"], 2)
            self.assertEqual(detail["recent_trades_page"]["recent_trades_meta"]["total"], 0)

            retrospective = bot.strategy_experiments_retrospective()
            self.assertTrue(retrospective["enabled"])
            self.assertEqual(len(retrospective["variants"]), 8)
            self.assertEqual(len(retrospective["profit_summary"]["rankings"]), 8)
            self.assertEqual(retrospective["window"], {"start_at": None, "end_at": None})

            tables = bot.strategy_experiments_tables(trade_limit=20, order_limit=20)
            self.assertTrue(tables["enabled"])
            self.assertGreaterEqual(len(tables["open_trades"]), 3)
            self.assertTrue(all("combo" in row for row in tables["open_trades"]))
            self.assertTrue(any(row["variant_id"] == "SINGLE_FAK" for row in tables["open_trades"]))
            self.assertTrue(any(row["variant_id"] == "PAIR_FAK" for row in tables["open_trades"]))
            self.assertGreaterEqual(tables["recent_orders_meta"]["total"], 8)
            self.assertTrue(all("combo" in row for row in tables["recent_orders"]))
            self.assertGreaterEqual(tables["recent_trades_meta"]["total"], 3)
            self.assertEqual(tables["recent_trades_summary"]["total_count"], tables["recent_trades_meta"]["total"])

            with self.assertRaises(ValueError):
                bot.strategy_experiment_detail("bad-variant")

    def test_strategy_experiment_low_fill_variant_is_disqualified(self) -> None:
        review = _experiment_review_score(
            {
                "settled_count": 0,
                "official_count": 0,
                "chainlink_count": 0,
                "roi_pct": None,
                "win_rate": None,
                "total_pnl": 0.0,
            },
            {
                "total_count": 60,
                "rejected_count": 0,
                "expired_count": 0,
                "canceled_count": 0,
                "fill_attempt_count": 0,
                "fill_rate": 0.0,
            },
            None,
            None,
        )

        self.assertEqual(review["sample_status"], "DISQUALIFIED")
        self.assertEqual(review["sample_label"], "执行淘汰")
        self.assertEqual(review["decision"], "执行不可用")
        self.assertTrue(review["disqualified"])
        self.assertFalse(review["eligible_for_decision"])
        self.assertLessEqual(review["score"], 25.0)
        self.assertIn("长期低成交，暂不纳入决胜", review["reasons"])

    def test_strategy_experiment_decision_can_finish_with_disqualified_variants(self) -> None:
        variants = [
            {
                "variant_id": "PAIR_POST_ONLY",
                "combo": "PAIR + POST_ONLY",
                "review_score": {"score": 82.0, "eligible_for_decision": True, "disqualified": False},
                "recent_trades_summary": {"settled_count": 40, "total_pnl": 12.0},
                "order_summary": {"total_count": 90, "fill_rate": 58.0},
            },
            {
                "variant_id": "PAIR_GTC",
                "combo": "PAIR + GTC",
                "review_score": {
                    "score": 18.0,
                    "eligible_for_decision": False,
                    "disqualified": True,
                    "disqualification_reason": "长期低成交",
                },
                "recent_trades_summary": {"settled_count": 0, "total_pnl": 0.0},
                "order_summary": {"total_count": 80, "fill_rate": 0.0},
            },
        ]

        summary = _experiment_decision_summary(variants)

        self.assertTrue(summary["comparison_ready"])
        self.assertEqual(summary["status"], "READY")
        self.assertEqual(summary["recommended_variant_id"], "PAIR_POST_ONLY")
        self.assertEqual(summary["ready_count"], 1)
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(summary["disqualified_count"], 1)
        self.assertEqual(summary["disqualified_variants"][0]["variant_id"], "PAIR_GTC")

    def test_strategy_experiment_profit_summary_separates_leader_from_final_winner(self) -> None:
        variants = [
            {
                "variant_id": "SINGLE_FAK",
                "combo": "SINGLE + FAK",
                "review_score": {"score": 55.0, "eligible_for_decision": False, "disqualified": False},
                "recent_trades_summary": {"settled_count": 4, "total_pnl": 15.0, "roi_pct": 75.0, "win_rate": 100.0},
                "order_summary": {"fill_rate": 100.0},
            },
            {
                "variant_id": "PAIR_POST_ONLY",
                "combo": "PAIR + POST_ONLY",
                "review_score": {"score": 82.0, "eligible_for_decision": True, "disqualified": False},
                "recent_trades_summary": {"settled_count": 40, "total_pnl": 12.0, "roi_pct": 24.0, "win_rate": 62.5},
                "order_summary": {"fill_rate": 58.0},
            },
        ]

        waiting = _experiment_profit_summary(variants)

        self.assertEqual(waiting["status"], "WAITING_FOR_SAMPLE")
        self.assertEqual(waiting["current_profit_leader_variant_id"], "SINGLE_FAK")
        self.assertIsNone(waiting["winner_variant_id"])
        self.assertFalse(waiting["comparison_ready"])

        variants[0]["review_score"] = {"score": 20.0, "eligible_for_decision": False, "disqualified": True}
        ready = _experiment_profit_summary(variants)

        self.assertEqual(ready["status"], "READY")
        self.assertEqual(ready["current_profit_leader_variant_id"], "SINGLE_FAK")
        self.assertEqual(ready["winner_variant_id"], "PAIR_POST_ONLY")
        self.assertTrue(ready["comparison_ready"])
        self.assertTrue(ready["profitable_winner_ready"])

        variants[1]["recent_trades_summary"]["total_pnl"] = -1.0
        no_profit = _experiment_profit_summary(variants)

        self.assertEqual(no_profit["status"], "NO_PROFIT")
        self.assertTrue(no_profit["comparison_ready"])
        self.assertFalse(no_profit["profitable_winner_ready"])
        self.assertEqual(no_profit["best_eligible_variant_id"], "PAIR_POST_ONLY")
        self.assertIsNone(no_profit["winner_variant_id"])

    def test_strategy_experiment_html_report_escapes_and_summarizes_variants(self) -> None:
        report = {
            "enabled": True,
            "db_dir": "data/strategy-experiments",
            "window": {"start_at": None, "end_at": None},
            "profit_summary": {
                "status_label": "等待盈利样本",
                "winner_combo": None,
                "current_profit_leader_combo": "PAIR + POST_ONLY",
                "ready_count": 1,
                "total_count": 2,
                "disqualified_count": 0,
                "profitable_winner_ready": False,
                "reason": "还有 1 个组合未达到样本阈值",
            },
            "decision_summary": {
                "reason": "继续观察",
                "missing_sample_variants": [
                    {
                        "combo": "SINGLE + FAK",
                        "sample_label": "样本不足",
                        "settled_count": 2,
                        "order_count": 4,
                    }
                ],
                "disqualified_variants": [],
            },
            "variants": [
                {
                    "variant_id": "PAIR_POST_ONLY",
                    "combo": "PAIR + POST_ONLY",
                    "role": "最核心目标",
                    "target_code_completion": "90%+",
                    "target_report_alignment": "90%+",
                    "recent_trades_summary": {
                        "total_pnl": 3.25,
                        "roi_pct": 12.5,
                        "win_rate": 66.7,
                        "settled_count": 3,
                        "total_count": 4,
                        "official_count": 3,
                    },
                    "order_summary": {"total_count": 8, "fill_rate": 62.5},
                    "review_score": {
                        "score": 76.5,
                        "decision": "优先候选",
                        "sample_label": "观察中",
                        "eligible_for_decision": False,
                        "disqualified": False,
                        "reasons": ["reason <script>alert(1)</script>"],
                    },
                }
            ],
        }

        html = _strategy_experiments_retrospective_report_html(report, generated_at=1_779_871_200)

        self.assertIn("策略实验复盘报告", html)
        self.assertIn("PAIR + POST_ONLY", html)
        self.assertIn("+$3.25", html)
        self.assertIn("reason &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("reason <script>alert(1)</script>", html)

    def test_strategy_experiment_report_snapshot_writes_docs_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
            )
            output = Path(tmp) / "docs" / "snapshot.html"

            result = generate_strategy_experiment_report_snapshot(
                settings,
                output,
                generated_at=1_779_871_200,
            )

            self.assertEqual(result["output_path"], str(output))
            self.assertEqual(result["variant_count"], 1)
            self.assertTrue(output.exists())
            html = output.read_text(encoding="utf-8")
            self.assertIn("策略实验复盘报告", html)
            self.assertIn("SINGLE + FAK", html)
            self.assertIn("数据源", html)

    def test_paper_order_summary_supports_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            old_market = MarketRound(
                round_id="btc-updown-5m-order-window-old",
                symbol="BTC",
                started_at=now - 600,
                ends_at=now - 300,
                target_price=100.0,
            )
            new_market = MarketRound(
                round_id="btc-updown-5m-order-window-new",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 240,
                target_price=100.0,
            )
            store.upsert_round(old_market)
            store.upsert_round(new_market)
            store._insert_paper_order(
                market=old_market,
                side="Up",
                order_type="GTC",
                status="RESTING",
                limit_price=0.5,
                post_only=False,
                expires_at=None,
                requested_cash=1.0,
                reserved_cash=1.0,
                remaining_cash=1.0,
                filled_shares=0.0,
                avg_fill_price=None,
                notional=0.0,
                fee=0.0,
                cash_spent=0.0,
                trade_id=None,
                confidence=0.6,
                move_bps=10.0,
                reason="old",
                now=now - 500,
            )
            store._insert_paper_order(
                market=new_market,
                side="Down",
                order_type="GTC",
                status="RESTING",
                limit_price=0.5,
                post_only=False,
                expires_at=None,
                requested_cash=1.0,
                reserved_cash=1.0,
                remaining_cash=1.0,
                filled_shares=0.0,
                avg_fill_price=None,
                notional=0.0,
                fee=0.0,
                cash_spent=0.0,
                trade_id=None,
                confidence=0.6,
                move_bps=-10.0,
                reason="new",
                now=now - 10,
            )

            summary = store.paper_order_summary("BTC", start_at=now - 60, end_at=now)

            self.assertEqual(summary["total_count"], 1)
            self.assertEqual(summary["active_count"], 1)
            self.assertEqual(summary["requested_cash"], 1.0)

    def test_real_strategy_uses_orderbook_ask_price(self) -> None:
        settings = Settings(min_confidence=0.55, min_edge=0.0, max_entry_price=0.8)
        strategy = RealBtcFiveMinuteStrategy(settings)
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-1000",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 180,
            target_price=100.0,
        )
        payload = {
            "price": {
                "chainlink": 101.0,
                "chainlink_updated_ms": int(now * 1000),
                "binance": 101.1,
                "binance_updated_ms": int(now * 1000),
            },
            "quotes": {
                "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": int(now * 1000)},
            },
        }
        signal = strategy.signal(input_from_snapshot(market, payload))
        self.assertEqual(signal.side, "Up")
        self.assertEqual(signal.entry_price, 0.54)

    def test_parse_real_btc_5m_market(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        raw = {
            "question": "Bitcoin Up or Down - test",
            "slug": "btc-updown-5m-1779871200",
            "conditionId": "0xabc",
            "endDate": "2026-05-27T08:45:00Z",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["1", "2"]',
        }
        market = client._parse_market(raw)
        self.assertIsNotNone(market)
        self.assertEqual(market.up_token, "1")
        self.assertEqual(market.down_token, "2")
        self.assertEqual(market.symbol, "BTC")

    def test_polymarket_resolution_reads_event_metadata_prices(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        slug = "btc-updown-5m-1779871200"
        client._get_event_by_slug = lambda _slug: {
            "eventMetadata": {"finalPrice": 101.25, "priceToBeat": 100.5},
            "markets": [
                {
                    "slug": slug,
                    "closed": True,
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["1", "0"]',
                }
            ],
        }

        def fail_page_fetch(_url: str) -> str:
            raise AssertionError("page fallback should not be used when Gamma eventMetadata has prices")

        client._get_text = fail_page_fetch

        resolution = client.get_resolution(slug)

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution["outcome"], "Up")
        self.assertAlmostEqual(resolution["final_price"], 101.25, places=6)
        self.assertAlmostEqual(resolution["target_price"], 100.5, places=6)
        self.assertEqual(resolution["settlement_price_source"], "Gamma:eventMetadata")

    def test_polymarket_resolution_falls_back_to_page_prices(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        slug = "btc-updown-5m-1779871200"
        client._get_event_by_slug = lambda _slug: {
            "markets": [
                {
                    "slug": slug,
                    "closed": True,
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["0", "1"]',
                }
            ],
        }
        client._get_text = lambda _url: (
            '<html><script>{"eventMetadata":{"finalPrice":98.75,"priceToBeat":100.5}}</script></html>'
        )

        resolution = client.get_resolution(slug)

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution["outcome"], "Down")
        self.assertAlmostEqual(resolution["final_price"], 98.75, places=6)
        self.assertAlmostEqual(resolution["target_price"], 100.5, places=6)
        self.assertEqual(resolution["settlement_price_source"], "PolymarketPage:eventMetadata")

    def test_polymarket_quote_keeps_sorted_orderbook_levels(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        client._get_json = lambda _url, _params: {
            "bids": [{"price": "0.31", "size": "9"}, {"price": "0.33", "size": "4"}],
            "asks": [{"price": "0.45", "size": "8"}, {"price": "0.34", "size": "2"}],
        }

        quote = client.get_quote("token-1", "Up").to_dict()

        self.assertEqual(quote["best_bid"], 0.33)
        self.assertEqual(quote["best_ask"], 0.34)
        self.assertEqual(quote["bids"][0], {"price": 0.33, "size": 4.0})
        self.assertEqual(quote["asks"][0], {"price": 0.34, "size": 2.0})


if __name__ == "__main__":
    unittest.main()

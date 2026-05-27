from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from polybot2other.config import Settings
from polybot2other.bot import PaperTradingBot
from polybot2other.models import MarketRound, Signal
from polybot2other.polymarket import PolymarketClient
from polybot2other.storage import TradeStore
from polybot2other.strategy import RealBtcFiveMinuteStrategy, input_from_snapshot


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
            open_rows = store.open_trades()
            self.assertEqual(len(open_rows), 1)
            self.assertAlmostEqual(open_rows[0]["shares"], 10.0, places=5)
            self.assertAlmostEqual(open_rows[0]["stake"], 5.0, places=5)
            metrics = store.metrics()
            self.assertAlmostEqual(metrics["cash_balance"], 92.5, places=5)
            self.assertAlmostEqual(metrics["realized_pnl"], -2.5, places=5)
            self.assertAlmostEqual(metrics["open_risk"], 5.0, places=5)

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
            self.assertGreater(store.metrics()["realized_pnl"], 0)

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


if __name__ == "__main__":
    unittest.main()

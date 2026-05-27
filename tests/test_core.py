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

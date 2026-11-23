"""Exercise the forecast account through the Paper decision, arrival and confirmation loop."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock

from polybot2other.config import Settings
from polybot2other.experiments import FORECAST_EARLY_PAPER_ID, FORECAST_PAPER_ID
from polybot2other.forecast_execution import ForecastPaperExecution
from polybot2other.models import MarketRound
from polybot2other.optimized_reference import ReferenceCache, SPOT, TWAP
from polybot2other.paper_execution import ExecutionPolicy
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.storage import TradeStore


class ForecastExecutionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = TradeStore(root/"paper.sqlite3", 100)
        self.refs = ReferenceCache(root/"references.sqlite3")
        self.addCleanup(self.store.conn.close)
        self.addCleanup(self.refs.conn.close)
        self.market = MarketRound("btc-updown-5m-1800000000", "BTC", 1800000000., 1800000300., 100.,
                                  condition_id="condition", up_token="up", down_token="down")
        self.store.upsert_round(self.market)
        self.now = self.market.started_at+151
        self.spot = 100.5
        self.bid, self.ask = .55, .56
        self.sell_depth = 500.
        self.frame_error = None
        self.info = {"fee_rate": .07, "fee_exponent": 1, "tick_size": "0.01"}
        self.rule = {"enabled": True, "stake_dollars": 5., "max_open_trades": 1, "max_daily_loss": 15.}
        self.engine = self.make_engine(self.store, FORECAST_PAPER_ID)
        with self.refs.conn:
            self.refs.conn.executemany("INSERT INTO reference_ticks VALUES(?,?,?,?,?,0)", [
                (SPOT, sec*1000, self.spot, str(self.spot), sec*1000)
                for sec in range(int(self.now)-120, int(self.now)+1)])

    def make_engine(self, store, strategy_id):
        settings = Settings(db_path=store.db_path, live_trading_runtime_enabled=False)
        bot = SimpleNamespace(store=store, settings=settings, _lock=threading.RLock(),
                              _sync_shared_market_inputs=self.frame, paper_trading_paused=False)
        return ForecastPaperExecution(bot, strategy_id, settings, frame_reader=self.frame,
            info_reader=lambda market: self.info, book_reader=self.book,
            config_reader=lambda: {"revision": 1, "strategies": {strategy_id: self.rule}},
            feed=SimpleNamespace(references=self.refs), clock=lambda: self.now,
            policy=ExecutionPolicy(confirmation_seconds=4))

    def book(self, token):
        return {"token_id": token, "source_ms": self.now*1000, "received_ms": self.now*1000,
                "best_bid": self.bid, "best_ask": self.ask,
                "asks": [{"price": self.ask, "size": 500}], "bids": [{"price": self.bid, "size": self.sell_depth}],
                "tick_size": "0.01", "min_order_size": 5}

    def frame(self):
        tick = {"price": self.spot, "source_ms": self.now*1000, "received_ms": self.now*1000}
        with self.refs.conn:
            self.refs.conn.execute("INSERT OR IGNORE INTO reference_ticks VALUES(?,?,?,?,?,0)",
                (SPOT, int(self.now*1000), self.spot, str(self.spot), self.now*1000))
        return MarketInputs(self.market, {"ready": True, "protocol_verified": True, "latest": {SPOT: tick, TWAP: tick}},
                            {}, {"Up": self.book("up"), "Down": self.book("down")}, self.now, "frame", self.frame_error)

    def confirm(self):
        self.now += 1.1
        self.engine.poll()
        self.now += 3.
        self.engine.poll()

    def buy(self):
        self.engine.decide()
        order, = self.engine.pending()
        self.assertEqual((order["action"], order["forecast_decision"]["action"]), ("BUY", "BUY"))
        self.confirm()
        return self.store.open_trades()[0]

    def test_early_account_can_open_in_extra_minute_alongside_reference_account(self):
        early_store = TradeStore(self.store.db_path.with_name("early.sqlite3"), 100)
        self.addCleanup(early_store.conn.close)
        early = self.make_engine(early_store, FORECAST_EARLY_PAPER_ID)
        self.market = replace(self.market, started_at=self.now-60, ends_at=self.now+240)
        self.store.upsert_round(self.market)
        early_store.upsert_round(self.market)
        self.now -= 1
        early.decide()
        self.assertFalse(early.pending())
        self.now += 1
        self.engine.decide()
        self.assertFalse(self.engine.pending())
        early.decide()
        early_checkpoint = early_store.conn.execute("SELECT direction,observed_at FROM forecast_checkpoints WHERE seconds_left=240").fetchone()
        self.assertEqual(tuple(early_checkpoint), ("Up", self.now))
        self.assertIsNone(self.store.conn.execute("SELECT direction FROM forecast_checkpoints WHERE seconds_left=240").fetchone())
        order, = early.pending()
        self.assertEqual(order["forecast_decision"]["time_left_seconds"], 240)
        self.assertEqual(order["forecast_decision"]["max_entry_seconds"], 240)
        self.now += 1.1
        early.poll()
        self.now += 3.
        early.poll()
        self.assertEqual(len(early_store.open_trades()), 1)
        self.assertEqual(self.store.open_trades(), [])
        self.assertEqual(self.store.account()["cash_balance"], 100)
        # At 180s the same fresh frame is in both policies' entry windows.
        self.now = self.market.ends_at - 180
        self.engine.decide()
        self.assertEqual(self.engine.pending()[0]["forecast_decision"]["max_entry_seconds"], 180)
        early.decide()
        self.assertFalse(early.pending())
        self.assertEqual(len(early_store.open_trades()), 1)

    def test_direction_reversal_sells_even_at_loss_and_residual_settles_officially(self):
        position = self.buy()
        self.engine.decide()
        self.assertEqual(self.engine.bot.last_decision["action"], "HOLD")
        self.spot = 98.
        self.bid, self.ask = .32, .34
        self.now += 1
        self.engine.decide()
        sell, = self.engine.pending()
        self.assertEqual((sell["action"], sell["forecast_decision"]["action"]), ("SELL", "SELL"))
        self.engine.decide()
        self.assertEqual(len(self.engine.pending()), 1)
        self.sell_depth = 6.
        self.confirm()
        closed = self.store.conn.execute("SELECT * FROM trades WHERE settlement_source='early_exit'").fetchone()
        self.assertGreater(closed["payout"], 0)
        self.assertLess(closed["pnl"], 0)
        self.assertAlmostEqual(closed["payout"], 3*(.32-.07*.32*.68), places=6)
        remainder, = self.store.open_trades()
        self.assertAlmostEqual(position["stake"], remainder["stake"]+closed["stake"], places=6)
        self.now = self.market.ends_at+30
        self.store.settle_round_outcome(self.market.round_id, "Down", self.now,
            final_price=98, target_price=100, settlement_source="polymarket_official")
        account = self.store.account()
        self.assertAlmostEqual(account["cash_balance"], 100 + account["realized_pnl"], places=6)
        self.assertEqual(self.store.open_trade_count(), 0)
        self.assertIn("已经买过", self.engine._entry_block(self.market, "Down", self.rule))
        self.assertIsNotNone(self.store.conn.execute(
            "SELECT id FROM trades WHERE settlement_source='polymarket_official'").fetchone())

    def test_stale_data_or_missing_liquidity_preserves_the_position(self):
        starting = self.buy()
        self.now += 1
        self.spot = 98
        self.frame_error = "参考价断开"
        self.engine.decide()
        self.assertEqual(self.engine.pending(), [])
        self.frame_error = None
        self.sell_depth = 0
        self.engine.decide()
        self.assertEqual(self.engine.pending(), [])
        self.assertEqual(self.store.open_trades()[0]["shares"], starting["shares"])

    def test_net_value_exit_floor_survives_arrival_price_change(self):
        self.buy()
        self.now += 1
        self.bid, self.ask = .96, .97
        self.engine.decide()
        sell, = self.engine.pending()
        required = sell["forecast_decision"]["min_net_bid"]
        self.assertGreater(required, .9)
        self.assertGreaterEqual(sell["limit"]-.07*sell["limit"]*(1-sell["limit"]), required)
        self.bid, self.ask = .90, .91
        self.confirm()
        self.assertEqual(self.engine.pending(), [])
        self.assertEqual(self.store.open_trade_count(), 1)
        self.assertEqual(self.store.account()["realized_pnl"], 0)

    def early_engine_with_trace(self):
        early = self.make_engine(self.store, FORECAST_EARLY_PAPER_ID)
        early.entry_trace = Mock()
        return early

    def test_rejection_captures_exact_submit_book_without_inventing_a_recheck(self):
        early = self.early_engine_with_trace()
        early.book_reader = Mock(return_value={**self.book("up"), "best_ask": .76,
                                                "asks": [{"price": .76, "size": 7}]})
        early.frame_reader = Mock(wraps=early.frame_reader)
        early.decide()
        self.assertIsNone(self.engine.entry_trace)
        early.frame_reader.assert_not_called()
        self.assertEqual(early.pending(), [])
        stage, round_id, at, data = early.entry_trace.record.call_args.args
        self.assertEqual((stage, round_id, at), ("pre_submit_reject", self.market.round_id, self.now))
        self.assertEqual(data["candidate_ask"], self.ask)
        self.assertEqual((data["ask"], data["ask_size_at_best"], data["ask_size_in_limit"]), (.76, 7, 0))
        self.assertIsNone(data["recheck_score"])
        self.assertEqual(data["reason"], "提交前盘口已超过策略限价")

    def test_failed_model_recheck_links_original_decision_and_the_second_frame(self):
        early = self.early_engine_with_trace()
        def changed_frame():
            frame = self.frame()
            quote = dict(frame.quotes["Up"], best_ask=.92, asks=[{"price": .92, "size": 19}])
            return replace(frame, snapshot_id="later", quotes={**frame.quotes, "Up": quote})
        early.frame_reader = Mock(side_effect=changed_frame)
        early.decide()
        early.frame_reader.assert_called_once()
        data = early.entry_trace.record.call_args.args[-1]
        self.assertEqual((data["candidate_snapshot_id"], data["recheck_snapshot_id"]), ("frame", "later"))
        self.assertEqual((data["ask"], data["recheck_ask"], data["recheck_ask_size_at_best"]), (.56, .92, 19))
        self.assertEqual(data["recheck_action"], "WAIT")
        self.assertIn("差价", data["recheck_reason"])

    def test_arrival_uses_actual_book_and_trace_failure_cannot_change_matching(self):
        for filled in (False, True):
            with self.subTest(filled=filled):
                early = self.early_engine_with_trace()
                early.entry_trace.observe.side_effect = RuntimeError("diagnostic failed")
                early.entry_trace.record.side_effect = RuntimeError("diagnostic failed")
                self.bid, self.ask = .55, .56
                early.decide()
                order, = early.pending()
                self.now += 1.1
                if not filled:
                    self.ask = .62
                early.poll()
                stage, round_id, at, details = early.entry_trace.record.call_args.args
                self.assertEqual((round_id, at), (self.market.round_id, self.now))
                self.assertEqual(details["order_id"], order["order_id"])
                self.assertEqual(details["ask"], self.ask)
                self.assertEqual(details["book_received_ms"], self.now*1000)
                self.assertEqual(details["matched_shares"] > 0, filled)
                if filled:
                    self.assertEqual(stage, "arrival_fill")
                    self.assertEqual(early.pending()[0]["phase"], "CONFIRMING")
                    self.assertEqual(details["available_shares_in_limit"], 250)
                else:
                    self.assertEqual(stage, "arrival_empty")
                    self.assertEqual(early.pending(), [])
                    self.assertEqual(details["available_shares_in_limit"], 0)

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from polybot2other.config import Settings
from polybot2other.fast_floor_trial import FastFloorPaperExecution
from polybot2other.models import MarketRound, Signal
from polybot2other.optimized_fast import VARIANT_ID
from polybot2other.paper_execution import PaperExecution
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.storage import TradeStore


class FastFloorTrialTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.now = 1800000050.
        self.market = MarketRound("btc-updown-5m-1800000000", "BTC", 1800000000., 1800000300.,
                                  100., up_token="up", down_token="down")
        self.asks = [{"price": .57, "size": 100}]
        self.rate = .07
        self.book_reader = Mock(side_effect=self.book)
        self.rule = {"stake_dollars": 5., "max_open_trades": 2, "max_daily_loss": 20.}
        self.signal = Signal("BTC", "Up", .9, .57, 12., "FAST")
        self.engine = self.build_engine(FastFloorPaperExecution, "floor")

    def book(self, token):
        return {"token_id": token, "source_ms": self.now*1000, "received_ms": self.now*1000,
                "tick_size": "0.01", "min_order_size": 5., "best_ask": self.asks[0]["price"],
                "best_bid": .50, "asks": self.asks, "bids": [{"price": .50, "size": 100}]}

    def frame(self):
        return MarketInputs(self.market, {}, {}, {"Up": self.book("up")}, self.now, "snapshot", None)

    def build_engine(self, cls, folder):
        path = self.folder/folder/"ledger.sqlite3"
        path.parent.mkdir(exist_ok=True)
        store = TradeStore(path, 100.)
        self.addCleanup(store.conn.close)
        settings = Settings(db_path=path, live_trading_runtime_enabled=False)
        bot = SimpleNamespace(store=store, _lock=RLock(), paper_trading_paused=False,
                              _sync_shared_market_inputs=self.frame)
        engine = cls(bot, VARIANT_ID, settings, frame_reader=self.frame, book_reader=self.book_reader,
                     info_reader=lambda *args: self.info(), clock=lambda: self.now,
                     feed=SimpleNamespace(references=None),
                     config_reader=lambda: {"revision": 1, "strategies": {VARIANT_ID: self.rule}})
        def evaluate(*args, **kwargs):
            engine.signals.last_features = {"round_id": self.market.round_id, "score": .9,
                                            "fast_allowed": True, "candidate_side": "Up", "min_edge": .02}
            engine.signals.last_strategy_id = VARIANT_ID
            return self.signal, .65
        engine.signals.evaluate = evaluate
        return engine

    def info(self):
        return {"fee_rate": self.rate, "fee_exponent": 1, "tick_size": "0.01"}

    def buy(self, engine):
        return engine.enqueue_buy(self.market, self.signal, .65, self.rule, 1, self.info())

    def test_cheap_executable_quote_skipped_before_reserving_despite_high_ceiling(self):
        control = self.build_engine(PaperExecution, "control")
        self.buy(control)
        self.assertEqual(len(control.pending()), 1)
        self.assertIsNone(self.buy(self.engine))
        self.assertEqual(self.engine.pending(), [])
        self.assertEqual(self.engine.store.account()["cash_balance"], 100)
        self.assertEqual(self.engine.fast_floor_summary(),
                         {"checked": 1, "markets": 1, "skipped": 1, "skipped_markets": 1, "floor": .6})
        row = self.engine.store.conn.execute("SELECT * FROM fast_floor_checks").fetchone()
        self.assertAlmostEqual(row["cost_per_share"], .57 + .07*.57*.43, places=6)
        self.assertAlmostEqual(row["quote_notional"]+row["quote_fee"], row["quote_shares"]*row["cost_per_share"])
        self.buy(self.engine)
        self.assertEqual(self.engine.fast_floor_summary()["checked"], 1)
        self.now += 1
        self.asks = [{"price": .59, "size": 100}]
        self.assertIsNotNone(self.buy(self.engine))
        self.assertEqual(self.engine.fast_floor_summary()["checked"], 2)
        self.assertEqual(self.engine.fast_floor_summary()["markets"], 1)

    def test_budget_weighted_executable_depth_with_fees_passes_despite_cheaper_top_ask(self):
        self.asks = [{"price": .55, "size": 1.}, {"price": .61, "size": 100.}]
        self.buy(self.engine)
        order, = self.engine.pending()
        self.assertGreater(order["fast_floor_quote"]["cost_per_share"], .6)
        self.assertEqual(order["phase"], "IN_FLIGHT")
        self.book_reader.assert_called_once_with("up")

    def test_exact_floor_is_inclusive_and_survives_reopen(self):
        self.asks = [{"price": .60, "size": 100.}]
        self.rate = 0.
        self.assertIsNotNone(self.buy(self.engine))
        reopened = self.build_engine(FastFloorPaperExecution, "floor")
        self.assertEqual(reopened.fast_floor_summary()["checked"], 1)
        self.assertEqual(reopened.fast_floor_summary()["skipped"], 0)

    def test_empty_book_or_wrong_direction_is_not_counted_as_a_floor_opportunity(self):
        self.asks = [{"price": .57, "size": .1}]
        with self.assertRaisesRegex(ValueError, "深度不足"):
            self.buy(self.engine)
        self.assertEqual(self.engine.fast_floor_summary()["checked"], 0)
        self.asks = [{"price": .57, "size": 100.}]
        self.engine.signals.evaluate = lambda *a, **kw: (Signal("BTC", "Down", .9, .58, 2., "changed"), .65)
        with self.assertRaisesRegex(ValueError, "复查"):
            self.buy(self.engine)
        self.assertEqual(self.engine.fast_floor_summary()["checked"], 0)

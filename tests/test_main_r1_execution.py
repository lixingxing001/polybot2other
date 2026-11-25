"""MAIN-R1 execution invariants on disposable Paper ledgers and fake public data.

No funded client, credentials, network feed or production ledger is used here.
The default plan exercises MAIN_FLOOR_060; explicit plans cover router boundaries.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock, Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from polybot2other.config import Settings
from polybot2other.main_r1_execution import ARRIVAL_MODEL, EXECUTION_BASIS, KEY, MainR1Execution
from polybot2other.models import MarketRound, Signal
from polybot2other.paper_execution import ExecutionPolicy
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.storage import TradeStore


class MainR1ExecutionTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "paper.sqlite3"
        self.now = 1800000100.
        timer = patch("time.time", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.market = MarketRound(
            "btc-updown-5m-1800000000", "BTC", 1800000000., 1800000300., 100.,
            condition_id="condition-a", up_token="up", down_token="down",
        )
        self.asks = [{"price": .61, "size": 1000.}]
        self.bids = [{"price": .59, "size": 1000.}]
        self.book_age_ms = 0.
        self.minimum = 5.
        self.info = {"fee_rate": .05, "fee_exponent": 1, "tick_size": "0.01"}
        self.rule = {"enabled": True, "stake_dollars": 5., "max_open_trades": 2,
                     "max_daily_loss": 0., "take_profit_pct": 20., "stop_loss_pct": 40.}
        self.config = {"enabled": False, "revision": 1, "strategies": {KEY: self.rule}}
        self.signal = Signal("BTC", "Up", .9, .61, 15., "early candidate")
        self.final_side = "Up"
        self.final_score = .9
        self.final_limit = .72
        self.frames_evaluated = []
        self.book_reader = Mock(side_effect=self.book)
        self.info_reader = Mock(side_effect=lambda *a, **kw: dict(self.info))
        self.policy = ExecutionPolicy(confirmation_seconds=12., depth_fraction=.5,
                                      redemption_seconds=60.)
        self.settings = Settings(db_path=self.path, live_trading_runtime_enabled=False,
                                 managed_live_enabled=False, market_scout_enabled=False,
                                 llm_super_agent_enabled=False)
        self.plan_reader = None
        self.make_engine()

    def make_engine(self, *, plan_reader=None, settings=None, strategy_id=KEY):
        self.store = TradeStore(self.path, 1000.)
        self.addCleanup(self.store.conn.close)
        self.store.upsert_round(self.market)
        self.bot = SimpleNamespace(store=self.store, settings=settings or self.settings,
                                   _lock=RLock(), paper_trading_paused=False,
                                   last_signal=None, _sync_shared_market_inputs=self.frame)
        kwargs = {}
        if plan_reader is not None:
            kwargs["entry_plan_reader"] = plan_reader
        self.plan_reader = plan_reader
        self.engine = MainR1Execution(
            self.bot, strategy_id, settings or self.settings, frame_reader=self.frame,
            info_reader=self.info_reader, book_reader=self.book_reader,
            config_reader=lambda: self.config, feed=SimpleNamespace(references=None),
            policy=self.policy, clock=lambda: self.now, **kwargs,
        )
        self.engine.signals.evaluate = Mock(side_effect=self.evaluate)
        return self.engine

    def reopen(self):
        self.store.conn.close()
        return self.make_engine(plan_reader=self.plan_reader)

    def book(self, token="up"):
        return {"token_id": token, "source_ms": self.now * 1000 - self.book_age_ms,
                "received_ms": self.now * 1000 - self.book_age_ms,
                "tick_size": self.info["tick_size"], "min_order_size": self.minimum,
                "best_ask": self.asks[0]["price"], "best_bid": self.bids[0]["price"],
                "asks": deepcopy(self.asks), "bids": deepcopy(self.bids)}

    def frame(self):
        reference = {"ready": True, "protocol_verified": True,
                     "latest": {"price.crypto": {"price": 100.15, "source_ms": self.now * 1000},
                                "price.crypto.twap": {"price": 100.1, "source_ms": self.now * 1000}}}
        return MarketInputs(self.market, reference,
                            {"chainlink": 100.15, "chainlink_updated_ms": self.now * 1000},
                            {"Up": self.book("up"), "Down": self.book("down")},
                            self.now, "frame-" + str(self.now), None)

    def evaluate(self, strategy_id, rule, market, reference, quotes, now, info, *, market_input=None):
        self.frames_evaluated.append((strategy_id, market_input))
        self.engine.signals.last_features = {"round_id": market.round_id,
                                             "candidate_side": self.final_side}
        self.engine.signals.last_strategy_id = strategy_id
        signal = Signal("BTC", self.final_side, self.final_score,
                        quotes[self.final_side]["best_ask"], 15., "final same-frame signal")
        return signal, self.final_limit

    def queue(self, *, side="Up"):
        return self.engine.enqueue_buy(self.market, replace(self.signal, side=side), .72,
                                       dict(self.rule), self.config["revision"], dict(self.info))

    def blocked(self, *, side="Up"):
        count = self.store.paper_order_count()
        cash = self.store.account()["cash_balance"]
        try:
            result = self.queue(side=side)
        except ValueError:
            result = None
        self.assertIsNone(result)
        self.assertEqual(self.store.paper_order_count(), count)
        self.assertEqual(self.store.account()["cash_balance"], cash)

    def advance(self, seconds):
        self.now += seconds
        self.engine.poll()

    def confirm(self):
        self.advance(1.1)
        self.advance(11.)

    @staticmethod
    def plan(candidate="MAIN_FLOOR_060", family="MAIN", floor=.6):
        return {"candidate_key": candidate, "signal_family": family, "price_floor": floor,
                "selection_id": "selection-before-submit", "regime": "test-regime",
                "reason": "deterministic test selection"}

    def fee_case(self, candidate="MAIN", *, strategy_id=KEY, tick="0.01", rate=.05,
                 exponent=1, min_edge=.02):
        """A fresh isolated wallet for each fee/tick/account subcase."""
        self.store.conn.close()
        self.fee_case_count = getattr(self, "fee_case_count", 0) + 1
        self.path = self.path.parent / f"fee-case-{self.fee_case_count}.sqlite3"
        self.settings = replace(self.settings, db_path=self.path, min_edge=min_edge)
        self.config = {"enabled": False, "revision": 1,
                       "strategies": {strategy_id: dict(self.rule)}}
        self.info = {"fee_rate": rate, "fee_exponent": exponent, "tick_size": tick}
        self.asks = [{"price": .60, "size": 1000.}]
        self.bids = [{"price": .58, "size": 1000.}]
        self.final_score, self.final_limit = .64, .62
        family = {"FAST": "MAIN_OPTIMIZED_FAST", "PLUS": "MAIN_OPTIMIZED_PLUS"}.get(candidate, "MAIN")
        floor = .60 if candidate == "MAIN_FLOOR_060" else None
        return self.make_engine(strategy_id=strategy_id,
                                plan_reader=lambda frame: self.plan(candidate, family, floor))

    def test_main_fee_rejection_is_valid_no_trade_for_portfolio_and_both_shadows(self):
        for candidate in ("MAIN", "MAIN_FLOOR_060"):
            for account in ("portfolio", "shadow"):
                with self.subTest(candidate=candidate, account=account):
                    strategy_id = KEY if account == "portfolio" else f"MAIN_R1_SHADOW_{candidate}_PAPER_V1"
                    self.fee_case(candidate, strategy_id=strategy_id)
                    self.asks = [{"price": .61, "size": 1000.}]
                    self.assertGreater(self.final_score - .61, self.settings.min_edge)
                    self.engine.decide()
                    self.assertEqual(self.store.paper_order_count(), 0)
                    self.assertEqual(self.store.account()["cash_balance"], 1000.)
                    self.assertEqual(self.store.reserved_cash(), 0.)
                    # The cohort collector must see an observed no-trade, not a
                    # technical failure that silently excludes this market.
                    self.assertTrue(self.engine.last_observation_valid)
                    codes = [row[0] for row in self.store.conn.execute(
                        "SELECT code FROM main_r1_decisions ORDER BY id")]
                    self.assertIn("EDGE_WAIT", codes)
                    self.assertNotIn("REJECTED", codes)
                    self.now += .1
                    self.asks = [{"price": .60, "size": 1000.}]
                    self.engine.decide()
                    self.assertEqual(self.store.paper_order_count(), 1)
                    self.assertTrue(self.engine.last_observation_valid)
        self.fee_case()
        self.final_score = self.settings.min_edge  # No positive price tick can fit.
        self.engine.decide()
        self.assertEqual(self.store.paper_order_count(), 0)
        self.assertEqual(self.store.reserved_cash(), 0.)
        self.assertTrue(self.engine.last_observation_valid)
        code, payload = self.store.conn.execute(
            "SELECT code,detail_json FROM main_r1_decisions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(code, "EDGE_WAIT")
        self.assertIsNone(json.loads(payload)["net_edge"]["fee_safe_ceiling"])

    def test_main_fee_safe_limit_and_same_frame_evidence_cover_portfolio_and_shadows(self):
        for candidate in ("MAIN", "MAIN_FLOOR_060"):
            for account in ("portfolio", "shadow"):
                with self.subTest(candidate=candidate, account=account):
                    strategy_id = KEY if account == "portfolio" else f"MAIN_R1_SHADOW_{candidate}_PAPER_V1"
                    self.fee_case(candidate, strategy_id=strategy_id)
                    self.assertIsNotNone(self.queue())
                    order, = self.engine.pending()
                    self.assertEqual(order["limit"], .60)
                    self.assertEqual(order["amount"], 5.)
                    self.assertEqual(self.store.reserved_cash(), 5.)
                    details = order["r1_decision"]
                    edge = details["net_edge"]
                    quote = details["quote"]
                    self.assertEqual(edge["score"], .64)
                    self.assertEqual(edge["min_edge"], .02)
                    self.assertEqual(edge["original_ceiling"], .62)
                    self.assertEqual(edge["fee_safe_ceiling"], .60)
                    self.assertEqual(details["book"]["asks"], self.asks)
                    self.assertEqual(details["info"], order["info"])
                    self.assertEqual(order["info"], self.info)
                    self.assertAlmostEqual(edge["quote_all_in_price"], quote["cash"] / quote["shares"])
                    self.assertAlmostEqual(edge["quote_net_edge"], .64 - quote["cash"] / quote["shares"])
                    self.assertGreaterEqual(edge["quote_net_edge"] + 1e-9, .02)

    def test_main_fee_limit_handles_zero_fees_ticks_and_fee_exponents(self):
        cases = (
            (0., "0.01", 1, .62),
            (.05, "0.1", 1, .60),
            (.05, "0.001", 1, .608),
            (.05, "0.0001", 1, .608),
            (.05, "0.01", 2, .61),
            (.05, "0.01", 4, .61),
        )
        for rate, tick, exponent, expected in cases:
            with self.subTest(rate=rate, tick=tick, exponent=exponent):
                self.fee_case(rate=rate, tick=tick, exponent=exponent)
                self.assertIsNotNone(self.queue())
                order, = self.engine.pending()
                self.assertEqual(order["limit"], expected)
                self.assertLessEqual(order["limit"], self.final_limit)
                self.assertAlmostEqual(order["limit"] / float(tick), round(order["limit"] / float(tick)))
                cost = expected + round(rate * (expected * (1 - expected)) ** exponent, 6)
                self.assertLessEqual(cost, self.final_score - self.settings.min_edge + 1e-9)
                if expected + float(tick) <= self.final_limit + 1e-9:
                    next_tick = round(expected + float(tick), 6)
                    next_cost = next_tick + round(rate * (next_tick * (1 - next_tick)) ** exponent, 6)
                    self.assertGreater(next_cost, self.final_score - self.settings.min_edge)

    def test_main_fee_limit_uses_frozen_minimum_edge_not_a_hardcoded_two_cents(self):
        self.fee_case(min_edge=.03)
        self.asks = [{"price": .59, "size": 1000.}]
        self.bids = [{"price": .57, "size": 1000.}]
        self.assertIsNotNone(self.queue())
        order, = self.engine.pending()
        self.assertEqual(order["limit"], .59)
        self.assertEqual(order["r1_decision"]["net_edge"]["min_edge"], .03)

    def test_main_fee_gate_rechecks_the_final_frame_after_metadata_refresh(self):
        self.fee_case(rate=0.)
        def refreshed(*args, **kwargs):
            self.info["fee_rate"] = .05
            self.asks = [{"price": .61, "size": 1000.}]
            return dict(self.info)
        self.info_reader.side_effect = refreshed
        self.engine.decide()
        self.assertEqual(self.frames_evaluated[-1][1].quotes["Up"]["best_ask"], .61)
        self.assertEqual(self.store.paper_order_count(), 0)
        self.assertEqual(self.store.reserved_cash(), 0.)
        self.assertTrue(self.engine.last_observation_valid)
        code, = self.store.conn.execute("SELECT code FROM main_r1_decisions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(code, "EDGE_WAIT")

    def test_main_fee_gate_checks_actual_sweep_cost_at_rounding_boundary(self):
        self.fee_case()
        self.asks = [{"price": .61, "size": 1000.}]
        # Unit-price fee arithmetic gives exactly two cents. Six-decimal
        # quantities/cash make this particular full-budget quote slightly dearer.
        self.final_score, self.final_limit = .641895, .61
        self.engine.decide()
        self.assertEqual(self.store.paper_order_count(), 0)
        self.assertEqual(self.store.reserved_cash(), 0.)
        self.assertTrue(self.engine.last_observation_valid)
        code, payload = self.store.conn.execute(
            "SELECT code,detail_json FROM main_r1_decisions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(code, "EDGE_WAIT")
        edge = json.loads(payload)["net_edge"]
        self.assertEqual(edge["fee_safe_ceiling"], .61)
        self.assertLess(edge["quote_net_edge"], .02 - 1e-9)

    def test_main_fee_gate_does_not_charge_swept_fees_twice(self):
        self.fee_case()
        self.final_score, self.final_limit = .66, .64
        self.asks = [{"price": .60, "size": 4.}, {"price": .62, "size": 1000.}]
        self.assertIsNotNone(self.queue())
        self.confirm()
        trade, = self.store.open_trades()
        order, = self.store.recent_paper_orders(1)
        fills = list(self.store.conn.execute("SELECT price,shares,notional,fee FROM paper_fills ORDER BY id"))
        self.assertEqual(len(fills), 2)
        expected_fee = round(sum(round(shares * .05 * price * (1 - price), 6)
                                 for price, shares, _, _ in fills), 6)
        self.assertGreater(expected_fee, 0.)
        self.assertAlmostEqual(order["fee"], expected_fee, places=6)
        self.assertAlmostEqual(trade["stake"], order["notional"] + expected_fee, places=6)
        self.assertAlmostEqual(order["cash_spent"], trade["stake"], places=6)
        self.assertAlmostEqual(self.store.account()["cash_balance"], 1000. - trade["stake"], places=6)
        self.store.settle_round_outcome(self.market.round_id, "Up", self.now)
        self.assertAlmostEqual(self.store.account()["realized_pnl"], trade["shares"] - trade["stake"], places=6)

    def test_fast_and_plus_keep_their_own_accepted_fee_limits(self):
        for candidate in ("FAST", "PLUS"):
            with self.subTest(candidate=candidate):
                self.fee_case(candidate)
                self.asks = [{"price": .61, "size": 1000.}]
                # This fixture represents an already-approved family evaluator.
                # R1 must not impose the new MAIN-only gate on its returned limit.
                self.assertIsNotNone(self.queue())
                order, = self.engine.pending()
                self.assertEqual(order["limit"], .62)
                self.assertEqual(order["r1_plan"]["candidate_key"], candidate)
                self.assertEqual(self.frames_evaluated[-1][0], f"MAIN_OPTIMIZED_{candidate}")

    def test_partial_fee_rounding_drift_keeps_the_fill_and_confirms_while_paused(self):
        self.fee_case()
        self.assertIsNotNone(self.queue())
        # Half of this real-shaped arrival level fills 0.000048 shares. Its
        # rounded notional is $0.000029 and fee is $0.000001: $0.625/share,
        # exceeding the $0.62 all-in bound despite the legal $0.60 limit.
        self.asks = [{"price": .60, "size": .000096}]
        self.advance(1.1)
        order, = self.engine.pending()
        self.assertEqual(order["phase"], "CONFIRMING")
        self.assertAlmostEqual(order["fill"]["shares"], .000048, places=9)
        self.assertAlmostEqual(order["fill"]["cash"], .000030, places=9)
        self.assertGreater(order["fill"]["cash"] / order["fill"]["shares"], .62)
        self.assertIn("net_edge_drift", self.engine.snapshot()["risk_state"]["persistent_halts"])
        self.assertEqual(self.store.reserved_cash(), 5.)
        self.advance(11.)
        # The shared display helper hides sub-cent stakes; inspect the ledger
        # itself, as the R1 risk/settlement paths also do for every positive fill.
        trade, = self.store.conn.execute("SELECT * FROM trades")
        self.assertEqual(trade["status"], "OPEN")
        self.assertAlmostEqual(trade["shares"], .000048, places=9)
        self.assertAlmostEqual(trade["stake"], .000030, places=9)
        self.assertEqual(self.store.recent_paper_orders(1)[0]["status"], "PARTIAL")
        self.assertEqual(self.store.paper_order_count(), 1)
        self.assertEqual(self.store.reserved_cash(), 0.)
        self.assertAlmostEqual(self.store.account()["cash_balance"], 1000. - trade["stake"], places=6)
        self.assertIn("net_edge_drift", self.engine.snapshot()["risk_state"]["persistent_halts"])
        state, = self.store.conn.execute("SELECT state FROM main_r1_round_locks").fetchone()
        self.assertEqual(state, "CONSUMED")
        self.engine.manage_exits()
        self.blocked()
        self.assertEqual(self.store.paper_order_count(), 1)

    def test_constructor_rejects_live_capability_flags(self):
        for field in ("live_trading_runtime_enabled", "managed_live_enabled"):
            with self.subTest(field=field):
                settings = replace(self.settings, **{field: True})
                with self.assertRaises(ValueError):
                    MainR1Execution(
                        self.bot, KEY, settings, frame_reader=self.frame,
                        info_reader=self.info_reader, book_reader=self.book_reader,
                        config_reader=lambda: self.config, feed=SimpleNamespace(references=None),
                        policy=self.policy, clock=lambda: self.now,
                    )

    def test_floor_rejection_only_skips_current_frame_then_waits_for_later_entry(self):
        self.asks = [{"price": .57, "size": 1000.}]
        self.bids = [{"price": .55, "size": 1000.}]
        self.assertIsNone(self.queue())
        self.assertEqual(self.engine.pending(), [])
        self.assertEqual(self.store.account()["cash_balance"], 1000.)
        self.now += 1
        self.asks = [{"price": .61, "size": 1000.}]
        self.assertIsNotNone(self.queue())
        self.assertEqual(len(self.engine.pending()), 1)

    def test_exact_fee_inclusive_floor_is_inclusive(self):
        self.info["fee_rate"] = 0.
        self.asks = [{"price": .60, "size": 1000.}]
        self.assertIsNotNone(self.queue())
        self.assertEqual(len(self.engine.pending()), 1)

    def test_floor_uses_budget_weighted_depth_and_official_fees(self):
        self.asks = [{"price": .55, "size": 1.}, {"price": .61, "size": 1000.}]
        self.bids = [{"price": .54, "size": 1000.}]
        self.assertIsNotNone(self.queue())
        self.assertEqual(self.store.reserved_cash(), 5.)

    def test_signal_and_quote_use_final_frame_instead_of_separate_rest_price(self):
        old_book = self.book("up")
        old_book.update(best_ask=.55, best_bid=.54,
                        asks=[{"price": .55, "size": 1000.}], bids=[{"price": .54, "size": 1000.}])
        self.book_reader.side_effect = lambda token: {**deepcopy(old_book), "token_id": token}
        self.final_score = .91
        self.assertIsNotNone(self.queue())
        order, = self.engine.pending()
        self.assertEqual(order["signal"]["confidence"], .91)
        self.assertEqual(order["signal"]["entry_price"], .61)
        self.assertTrue(self.frames_evaluated)
        self.assertTrue(all(family == "MAIN" for family, _ in self.frames_evaluated))
        self.assertEqual(self.frames_evaluated[-1][1].quotes["Up"]["best_ask"], .61)

    def test_final_limit_and_stale_frame_are_checked_before_reserving(self):
        self.final_limit = .60
        self.blocked()
        self.final_limit = .72
        self.book_age_ms = 10_000
        self.blocked()
        self.assertEqual(self.store.paper_order_count(), 0)

    def test_final_settings_revision_change_cannot_reserve_an_old_decision(self):
        original = self.evaluate
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.config["revision"] += 1
            return result
        self.engine.signals.evaluate.side_effect = changed
        self.blocked()

    def test_minimum_order_is_skipped_without_increasing_budget(self):
        self.minimum = 100.
        self.blocked()
        self.assertEqual(self.store.account()["cash_balance"], 1000.)
        self.assertEqual(self.store.reserved_cash(), 0.)

    def test_partial_match_locks_whole_round_through_confirmation_settlement_and_restart(self):
        self.assertIsNotNone(self.queue())
        self.asks = [{"price": .61, "size": 4.}]
        self.advance(1.1)
        self.assertEqual(self.engine.pending()[0]["phase"], "CONFIRMING")
        self.asks = [{"price": .61, "size": 1000.}]
        self.final_side = "Down"
        self.blocked(side="Down")
        self.advance(11.)
        trade, = self.store.open_trades()
        self.assertEqual(trade["shares"], 2.)
        self.assertEqual(self.store.recent_paper_orders(1)[0]["status"], "PARTIAL")
        self.store.settle_round_outcome(self.market.round_id, "Up", self.now)
        self.assertEqual(self.store.open_trade_count(), 0)
        self.asks = [{"price": .61, "size": 1000.}]
        self.blocked(side="Down")
        self.reopen()
        self.blocked(side="Down")
        self.assertEqual(self.store.paper_order_count(), 1)

    def test_restart_unknown_in_flight_does_not_refund_or_invent_zero_fill(self):
        self.assertIsNotNone(self.queue())
        before = self.store.account()["cash_balance"]
        self.reopen()
        self.assertEqual(self.store.account()["cash_balance"], before)
        self.assertEqual(self.store.reserved_cash(), 5.)
        self.advance(30.)
        self.assertEqual(self.store.account()["cash_balance"], before)
        self.assertEqual(self.store.open_trade_count(), 0)
        self.final_side = "Down"
        self.blocked(side="Down")

    def test_verified_zero_fill_releases_cash_and_allows_fresh_attempt(self):
        self.assertIsNotNone(self.queue())
        self.asks = [{"price": .90, "size": 1000.}]
        self.advance(1.1)
        self.assertEqual(self.store.recent_paper_orders(1)[0]["status"], "CANCELED")
        self.assertEqual(self.store.account()["cash_balance"], 1000.)
        self.assertEqual(self.store.reserved_cash(), 0.)
        self.now += 1.
        self.asks = [{"price": .62, "size": 1000.}]
        self.assertIsNotNone(self.queue())

    def test_late_worker_keeps_unknown_reservation_instead_of_inventing_zero_fill(self):
        self.assertIsNotNone(self.queue())
        self.advance(4.1)
        self.assertEqual(self.engine.snapshot()["counts"].get("UNKNOWN"), 1)
        unknown, = self.engine.snapshot()["unknown_orders"]
        self.assertEqual(unknown["failure_code"], "ARRIVAL_POLL_LATE")
        self.assertEqual(unknown["model"], ARRIVAL_MODEL)
        self.assertIsNone(unknown["evidence_at"])
        self.assertEqual(self.store.account()["cash_balance"], 995.)
        self.assertEqual(self.store.reserved_cash(), 5.)
        self.assertEqual(self.store.recent_paper_orders(1)[0]["status"], "PENDING")
        self.advance(30.)
        self.assertEqual(self.store.account()["cash_balance"], 995.)
        self.assertEqual(self.store.open_trade_count(), 0)

    def test_stale_arrival_cannot_prove_zero_fill_and_does_not_retry_on_recovery(self):
        self.assertIsNotNone(self.queue())
        self.book_age_ms = 10_000.
        self.advance(1.1)
        self.assertEqual(self.engine.snapshot()["counts"].get("UNKNOWN"), 1)
        self.assertIn("unknown_order", self.engine.snapshot()["risk_state"]["persistent_halts"])
        self.book_age_ms = 0.
        self.advance(11.)
        self.assertEqual(self.store.account()["cash_balance"], 995.)
        self.assertEqual(self.store.open_trade_count(), 0)
        self.blocked()

    def test_missing_arrival_depth_is_unknown_even_if_top_ask_still_exists(self):
        self.assertIsNotNone(self.queue())
        def missing_depth():
            frame = self.frame()
            frame.quotes["Up"]["asks"] = []
            return frame
        self.engine.frame_reader = missing_depth
        self.advance(1.1)
        self.assertEqual(self.engine.snapshot()["counts"].get("UNKNOWN"), 1)
        self.assertEqual(self.store.reserved_cash(), 5.)
        self.assertEqual(self.store.account()["cash_balance"], 995.)

    def test_saved_positive_match_cannot_be_canceled_as_zero_fill(self):
        ident = self.queue()
        self.asks = [{"price": .61, "size": 4.}]
        self.advance(1.1)
        order, = self.engine.pending()
        self.assertEqual(order["fill"]["shares"], 2.)
        self.engine._finish_empty(order, "deliberately invalid zero-fill request", verified_zero_fill=True)
        self.assertEqual(self.store.cancel_paper_order(ident)["canceled"], [])
        self.assertEqual(self.store.reserved_cash(), 5.)
        self.assertEqual(self.engine.pending()[0]["phase"], "CONFIRMING")
        self.advance(11.)
        self.assertEqual(self.store.open_trades()[0]["shares"], 2.)

    def test_actual_fill_below_floor_is_recorded_and_never_immediately_sold(self):
        self.assertIsNotNone(self.queue())
        self.asks = [{"price": .55, "size": 1000.}]
        self.bids = [{"price": .54, "size": 1000.}]
        self.confirm()
        trade, = self.store.open_trades()
        self.assertAlmostEqual(trade["entry_price"], .55, places=6)
        self.assertGreater(trade["shares"], 0)
        self.assertEqual(self.store.paper_order_count(), 1)
        self.engine.manage_exits()
        self.assertEqual(self.store.open_trade_count(), 1)
        with self.assertRaises(ValueError):
            self.engine.enqueue_sell(trade, self.market)
        self.assertEqual(self.store.paper_order_count(), 1)

    def test_saved_partial_match_recovers_without_duplicate_fill(self):
        self.assertIsNotNone(self.queue())
        self.asks = [{"price": .61, "size": 4.}]
        self.advance(1.1)
        self.reopen()
        self.assertEqual(self.store.reserved_cash(), 5.)
        self.advance(11.)
        self.engine.poll()
        trade, = self.store.open_trades()
        self.assertEqual(trade["shares"], 2.)
        self.assertEqual(self.store.paper_order_count(), 1)
        self.asks = [{"price": .61, "size": 1000.}]
        self.final_side = "Down"
        self.blocked(side="Down")

    def test_partial_below_market_minimum_has_no_executable_nav_but_still_settles(self):
        self.assertIsNotNone(self.queue())
        self.asks = [{"price": .61, "size": 4.}]
        self.confirm()
        trade, = self.store.open_trades()
        self.assertEqual(trade["shares"], 2.)
        self.assertLess(trade["shares"], self.minimum)
        risk = self.engine.risk_checkpoint()
        self.assertFalse(risk["allowed"])
        self.assertEqual(risk["budget"], 0.)
        self.assertIsNone(risk["nav"])
        self.assertEqual(risk["valuation_status"], "unavailable")
        self.assertEqual(self.store.reserved_cash(), 0.)
        self.store.settle_round_outcome(self.market.round_id, "Up", self.now)
        claim = self.engine.risk_checkpoint()
        self.assertEqual(claim["valuation_status"], "verified")
        self.assertEqual(claim["pending_claims"], 2.)
        self.assertAlmostEqual(claim["nav"], 1000. + self.store.account()["realized_pnl"], places=6)
        self.advance(61.)
        self.assertEqual(self.engine.pending_cash(), 0.)
        self.assertAlmostEqual(self.store.account()["cash_balance"], claim["nav"], places=6)

    def test_claim_release_is_once_only_and_does_not_unlock_the_round(self):
        self.assertIsNotNone(self.queue())
        self.confirm()
        trade, = self.store.open_trades()
        self.store.settle_round_outcome(self.market.round_id, "Up", self.now)
        before = self.store.account()["cash_balance"]
        self.assertAlmostEqual(self.engine.pending_cash(), trade["shares"], places=6)
        self.advance(59.)
        self.assertEqual(self.store.account()["cash_balance"], before)
        self.advance(2.)
        self.assertAlmostEqual(self.store.account()["cash_balance"], before + trade["shares"], places=6)
        self.assertEqual(self.engine.pending_cash(), 0.)
        after = dict(self.store.account())
        self.engine.poll()
        self.assertEqual(self.store.account(), after)
        self.final_side = "Down"
        self.blocked(side="Down")

    def test_nav_conserves_cash_reservations_marked_cost_and_claims_without_double_counting(self):
        self.assertEqual(self.engine.risk_checkpoint()["nav"], 1000.)
        self.assertIsNotNone(self.queue())
        pending = self.engine.risk_checkpoint()
        self.assertEqual(pending["nav"], 1000.)
        self.assertEqual(pending["available_cash"], 995.)
        self.assertEqual(pending["reserved_cash"], 5.)
        self.assertEqual(pending["exposure"], 5.)
        self.assertEqual(pending["unresolved_rounds"], 1)
        self.advance(1.1)
        unknown_value = self.engine.risk_checkpoint()
        self.assertFalse(unknown_value["allowed"])
        self.assertIsNone(unknown_value["nav"])
        self.advance(11.)
        trade, = self.store.open_trades()
        cash = self.store.account()["cash_balance"]
        self.assertAlmostEqual(cash + trade["stake"], 1000., places=6)
        sell_net = round(round(trade["shares"]*.59, 6) -
                         round(trade["shares"]*.05*.59*.41, 6), 6)
        confirmed = self.engine.risk_checkpoint()
        self.assertAlmostEqual(confirmed["nav"], cash + sell_net, places=6)
        self.assertEqual(confirmed["reserved_cash"], 0.)
        self.assertAlmostEqual(confirmed["exposure"], trade["stake"], places=6)
        self.store.settle_round_outcome(self.market.round_id, "Up", self.now)
        claim = self.engine.risk_checkpoint()
        self.assertAlmostEqual(claim["nav"], 1000. + self.store.account()["realized_pnl"], places=6)
        self.assertEqual(claim["available_cash"], cash)
        self.assertAlmostEqual(claim["pending_claims"], trade["shares"], places=6)
        self.assertEqual(claim["exposure"], 0.)
        self.advance(61.)
        released = self.engine.risk_checkpoint()
        self.assertAlmostEqual(released["nav"], claim["nav"], places=6)
        self.assertAlmostEqual(released["available_cash"], cash + trade["shares"], places=6)
        self.assertEqual(released["pending_claims"], 0.)

    def test_missing_valuation_depth_blocks_new_risk_without_cost_or_zero_substitution(self):
        self.assertIsNotNone(self.queue())
        self.confirm()
        self.bids = [{"price": .59, "size": .1}]
        risk = self.engine.risk_checkpoint()
        self.assertFalse(risk["allowed"])
        self.assertEqual(risk["budget"], 0.)
        self.assertIsNone(risk["nav"])
        self.assertEqual(risk["valuation_status"], "unavailable")
        self.assertEqual(self.store.open_trade_count(), 1)
        self.bids = [{"price": .59, "size": 1000.}]
        self.assertEqual(self.engine.risk_checkpoint()["valuation_status"], "verified")

    def test_missing_official_claim_is_unreconciled_instead_of_a_verified_lower_nav(self):
        self.assertIsNotNone(self.queue())
        self.confirm()
        self.store.settle_round_outcome(self.market.round_id, "Up", self.now)
        with self.store.conn:
            self.store.conn.execute("DELETE FROM paper_sim_claims")
        risk = self.engine.risk_checkpoint()
        self.assertFalse(risk["allowed"])
        self.assertIsNone(risk["nav"])
        self.assertEqual(risk["valuation_status"], "unavailable")

    def test_snapshot_is_read_only_and_does_not_reset_risk_on_day_change(self):
        self.engine.risk_checkpoint()
        before = self.engine.snapshot()
        self.now += 86400.
        changes = self.store.conn.total_changes
        after = self.engine.snapshot()
        self.assertEqual(self.store.conn.total_changes, changes)
        self.assertEqual(after["risk_state"], before["risk_state"])
        self.assertEqual(after["mode"], "PAPER_ONLY")
        self.assertFalse(after["live_enabled"])
        self.assertEqual(after["arrival_model"], ARRIVAL_MODEL)
        self.assertEqual(after["execution_basis"], EXECUTION_BASIS)
        self.assertEqual(after["unknown_order_count"], 0)
        self.assertEqual(after["unknown_orders"], [])

    def test_idle_poll_risk_sampling_is_one_hertz_but_entry_still_rechecks(self):
        self.engine.risk_checkpoint = Mock(wraps=self.engine.risk_checkpoint)
        start = self.now
        for i in range(10):
            self.now = start + i*.1
            self.engine.poll()
        self.assertEqual(self.engine.risk_checkpoint.call_count, 1)
        self.now = start + 1.
        self.engine.poll()
        self.assertEqual(self.engine.risk_checkpoint.call_count, 2)
        self.assertIsNotNone(self.queue())
        self.assertEqual(self.engine.risk_checkpoint.call_count, 3)

    def test_blocked_public_metadata_read_does_not_block_pending_order_arrival(self):
        self.assertIsNotNone(self.queue())
        reading, release = Event(), Event()
        errors = []
        def blocked_info(*args, **kwargs):
            reading.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test metadata reader was not released")
            return dict(self.info)
        self.engine.info_reader = blocked_info
        def decide():
            try:
                self.engine.decide()
            except Exception as exc:
                errors.append(exc)
        worker = Thread(target=decide, name="blocked-public-metadata-test", daemon=True)
        worker.start()
        try:
            self.assertTrue(reading.wait(timeout=1))
            acquired = self.engine.lock.acquire(timeout=.2)
            self.assertTrue(acquired, "Public metadata I/O must not own the confirmation lock")
            if acquired:
                self.engine.lock.release()
            self.advance(1.1)
            order, = self.engine.pending()
            self.assertEqual(order["phase"], "CONFIRMING")
            self.assertGreater(order["fill"]["shares"], 0)
            self.assertNotIn("UNKNOWN", self.engine.snapshot()["counts"])
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.paper_order_count(), 1)

    def test_no_router_evidence_means_no_entry_and_no_default_fallback(self):
        self.store.conn.close()
        self.make_engine(plan_reader=lambda frame: None)
        self.blocked()
        self.assertEqual(self.store.paper_order_count(), 0)

    def test_router_candidate_outside_allowlist_is_rejected(self):
        self.store.conn.close()
        self.make_engine(plan_reader=lambda frame: self.plan("VALUE_CONFIRM_V1"))
        self.blocked()

    def test_router_main_arm_can_skip_floor_but_not_execution_safety(self):
        self.store.conn.close()
        self.make_engine(plan_reader=lambda frame: self.plan("MAIN", "MAIN", None))
        self.asks = [{"price": .57, "size": 1000.}]
        self.bids = [{"price": .55, "size": 1000.}]
        self.assertIsNotNone(self.queue())
        self.assertEqual(self.store.reserved_cash(), 5.)

    def test_submitted_selection_is_frozen_despite_later_router_change(self):
        plan = self.plan()
        self.store.conn.close()
        self.make_engine(plan_reader=lambda frame: plan)
        self.assertIsNotNone(self.queue())
        before = json.dumps(self.engine.pending()[0], sort_keys=True)
        self.assertIn("selection-before-submit", before)
        plan.update(candidate_key="PLUS", signal_family="MAIN_OPTIMIZED_PLUS",
                    price_floor=None, selection_id="later-selection")
        after = json.dumps(self.engine.pending()[0], sort_keys=True)
        self.assertEqual(after, before)
        self.assertNotIn("later-selection", after)

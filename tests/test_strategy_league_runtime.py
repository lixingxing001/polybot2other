from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest
from pathlib import Path

from polybot2other.config import Settings
from polybot2other.optimized_main import StrictTwapMarketClient
from polybot2other.strategy_league_runtime import StrategyLeagueRunner


class LeagueRunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.owner = Mock()
        self.owner.store.db_path = Path(tmp.name) / "main.sqlite3"
        self.manager = Mock()
        self.runner = StrategyLeagueRunner(self.owner, self.manager)

    def test_rules_bound_to_same_market_and_token(self):
        exchange = self.manager.exchange
        exchange.market_info.return_value = {"tick_size": "0.01", "fee_rate": .07, "fee_exponent": 1}
        exchange.rest_book.side_effect = [
            {"token_id": "up", "tick_size": "0.01", "min_order_size": 5},
            {"token_id": "old-down", "tick_size": "0.01", "min_order_size": 1},
        ]
        market = SimpleNamespace(round_id="one", up_token="up", down_token="down")
        rid, info, rules = self.runner._public_rules(market)
        self.assertEqual(rid, "one")
        self.assertEqual(info["fee_rate"], .07)
        self.assertEqual(rules, {"Up": {"token_id": "up", "tick_size": "0.01", "min_order_size": 5.0}})
        self.assertFalse(self.runner.path.exists())

    def test_official_outcome_and_round_are_required(self):
        self.owner.store.get_round.return_value = {"outcome": "Down", "settlement_source": "estimate"}
        self.owner.polymarket.get_resolution.return_value = {"outcome":"Down","market_slug":"old","price_source":"Gamma:outcomePrices"}
        self.assertIsNone(self.runner._resolution("new"))
        self.owner.polymarket.get_resolution.return_value = {"outcome":"Up","market_slug":"new","price_source":"Gamma:outcomePrices"}
        self.assertEqual(self.runner._resolution("new"), "Up")
        self.owner.store.get_round.return_value = {"outcome":"Down","settlement_source":"polymarket_official"}
        self.assertEqual(self.runner._resolution("new"), "Down")
        self.assertEqual(self.owner.polymarket.get_resolution.call_count, 2)

    def test_candidate_only_market_resolves_through_the_shared_twap_client(self):
        rid = "btc-updown-5m-1795059300"
        self.owner.store.get_round.return_value = None
        self.owner.polymarket = StrictTwapMarketClient(Settings(), None)
        event = {
            "slug": rid,
            "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "eventMetadata": {"priceToBeat": 65000},
            "markets": [{"slug": rid, "closed": True, "outcomes": '["Up","Down"]',
                         "outcomePrices": '["1","0"]'}],
        }
        with patch.object(self.owner.polymarket, "_get_event_by_slug", return_value=event):
            self.assertEqual(self.runner._resolution(rid), "Up")

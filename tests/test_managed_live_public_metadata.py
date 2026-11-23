"""Paper metadata reads need no wallet; live routing remains authenticated."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from polybot2other.config import Settings
from polybot2other.managed_live_client import ManagedExchange


class PublicMarketMetadataTests(unittest.TestCase):
    def setUp(self):
        self.market = SimpleNamespace(
            round_id="test-round", condition_id="condition", up_token="up", down_token="down"
        )
        self.raw = {
            "t": [{"t": "up"}, {"t": "down"}],
            "fd": {"r": 0.07, "e": 1}, "mts": "0.01", "nr": False,
        }
        self.expected = {
            "fee_rate": 0.07, "fee_exponent": 1,
            "tick_size": "0.01", "neg_risk": False,
        }

    def exchange(self, *, live=False):
        exchange = ManagedExchange(Settings(live_trading_runtime_enabled=live))
        exchange.http.close()
        self.requests = []

        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.raw)

        exchange.http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(exchange.http.close)
        return exchange

    def test_paper_reads_official_metadata_without_credentials_or_signer(self):
        with patch.dict(os.environ, {}, clear=True):
            exchange = self.exchange()
            exchange.client = Mock(side_effect=AssertionError("Paper must not request a signer"))
            self.assertEqual(exchange.market_info(self.market), self.expected)
        exchange.client.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/clob-markets/condition")
        self.assertFalse(any(k.lower().startswith("poly_") or k.lower() == "authorization"
                             for k in request.headers))

    def test_paper_still_rejects_wrong_market_tokens(self):
        exchange = self.exchange()
        self.raw["t"] = [{"t": "another-token"}]
        with self.assertRaisesRegex(ValueError, "官方市场标识不匹配"):
            exchange.market_info(self.market)

    def test_live_metadata_keeps_existing_authenticated_client(self):
        exchange = self.exchange(live=True)
        client = SimpleNamespace(get_clob_market_info=Mock(return_value=self.raw))
        exchange.client = Mock(return_value=({}, client))
        self.assertEqual(exchange.market_info(self.market), self.expected)
        exchange.client.assert_called_once_with()
        client.get_clob_market_info.assert_called_once_with("condition")
        self.assertEqual(self.requests, [])

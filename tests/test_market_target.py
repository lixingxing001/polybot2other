import unittest
import urllib.error
from unittest.mock import patch

from polybot2other.polymarket import PolymarketClient


class MarketTargetTests(unittest.TestCase):
    def setUp(self):
        self.client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        self.raw = {
            "slug": "btc-updown-5m-1793024700",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["1", "2"],
        }
        self.page = patch.object(self.client, "_get_text", return_value="").start()
        self.addCleanup(patch.stopall)

    def test_official_opening_price_uses_exact_window_and_is_cached(self):
        with patch.object(self.client, "_get_json", return_value={
            "openPrice": 79842.42249623848, "closePrice": None, "completed": False,
        }) as get_json:
            first = self.client._parse_market(self.raw)
            second = self.client._parse_market(self.raw)
        self.assertEqual(first.target_price, 79842.42249623848)
        self.assertEqual(second.target_price, first.target_price)
        get_json.assert_called_once_with("https://polymarket.com/api/crypto/crypto-price", {
            "symbol": "BTC", "eventStartTime": "2026-10-26T14:25:00Z",
            "variant": "fiveminute", "endDate": "2026-10-26T14:30:00Z",
        })
        self.page.assert_not_called()

    def test_gamma_metadata_keeps_priority(self):
        with patch.object(self.client, "_get_json") as get_json:
            market = self.client._parse_market({**self.raw, "eventMetadata": {"priceToBeat": 80001.5}})
        self.assertEqual(market.target_price, 80001.5)
        get_json.assert_not_called()
        self.page.assert_not_called()

    def test_missing_api_price_keeps_legacy_page_fallback(self):
        self.page.return_value = (
            '{"state":{"data":{"openPrice":79842.5}},'
            '"queryKey":["crypto-prices","price","BTC","2026-10-26T14:25:00Z",'
            '"fiveminute","2026-10-26T14:30:00Z"]}'
        )
        with patch.object(self.client, "_get_json", return_value={"openPrice": None}):
            market = self.client._parse_market(self.raw)
        self.assertEqual(market.target_price, 79842.5)

    def test_invalid_opening_prices_never_use_closing_price(self):
        for value in (None, 0, -1, "nan", "inf", True, {}, "not-a-price"):
            with self.subTest(value=value), patch.object(self.client, "_get_json", return_value={
                "openPrice": value, "closePrice": 99999,
            }):
                self.assertEqual(self.client._parse_market(self.raw).target_price, 0)
        self.assertEqual(self.client._target_cache, {})

    def test_unavailable_api_keeps_fail_closed_behavior(self):
        for error in (urllib.error.URLError("unavailable"), TimeoutError(), ValueError("invalid JSON")):
            with self.subTest(error=type(error).__name__), patch.object(self.client, "_get_json", side_effect=error):
                self.assertEqual(self.client._parse_market(self.raw).target_price, 0)

    def test_missing_price_is_retried_and_windows_do_not_share_cache(self):
        with patch.object(self.client, "_get_json", side_effect=[
            {"openPrice": None}, {"openPrice": 80000}, {"openPrice": 80100},
        ]) as get_json:
            self.assertEqual(self.client._parse_market(self.raw).target_price, 0)
            self.assertEqual(self.client._parse_market(self.raw).target_price, 80000)
            later = {**self.raw, "slug": "btc-updown-5m-1793025000"}
            self.assertEqual(self.client._parse_market(later).target_price, 80100)
        self.assertEqual(get_json.call_count, 3)


if __name__ == "__main__":
    unittest.main()

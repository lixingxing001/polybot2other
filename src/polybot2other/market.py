from __future__ import annotations

import json
import math
import time
from concurrent import futures
from urllib import error as urlerror
from urllib import request as urlrequest

from .models import PriceTick


SYMBOLS = ("BTC",)
BINANCE_SYMBOLS = {"BTC": "BTCUSDT"}
COINBASE_SYMBOLS = {"BTC": "BTC-USD"}
OKX_SYMBOLS = {"BTC": "BTC-USDT"}


class PublicPriceClient:
    def __init__(self, timeout_seconds: float = 4.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_all(self) -> dict[str, PriceTick]:
        now = time.time()
        ticks: dict[str, PriceTick] = {}
        for symbol in SYMBOLS:
            ticks[symbol] = self.fetch_symbol(symbol, now)
        return ticks

    def fetch_symbol(self, symbol: str, now: float | None = None) -> PriceTick:
        if symbol not in SYMBOLS:
            raise ValueError(f"unsupported symbol for real BTC bot: {symbol}")
        now = now or time.time()
        failures: list[str] = []
        for fetcher in (self._fetch_coinbase, self._fetch_binance, self._fetch_okx):
            try:
                price, source = fetcher(symbol)
                if price > 0 and math.isfinite(price):
                    return PriceTick(symbol=symbol, price=price, source=source, timestamp=now)
            except (OSError, ValueError, KeyError, TimeoutError, urlerror.URLError, json.JSONDecodeError) as exc:
                failures.append(f"{fetcher.__name__}: {type(exc).__name__}")
                continue
        detail = "; ".join(failures) or "no public BTC price source attempted"
        raise RuntimeError(f"real BTC price fallback unavailable: {detail}")

    def fetch_sources(self, symbol: str, now: float | None = None) -> dict[str, PriceTick]:
        if symbol not in SYMBOLS:
            raise ValueError(f"unsupported symbol for real BTC bot: {symbol}")
        now = now or time.time()
        ticks: dict[str, PriceTick] = {}
        fetchers = (self._fetch_coinbase, self._fetch_binance, self._fetch_okx)
        with futures.ThreadPoolExecutor(max_workers=len(fetchers), thread_name_prefix="polybot2other-price") as executor:
            submitted = {executor.submit(fetcher, symbol): fetcher for fetcher in fetchers}
            for future in futures.as_completed(submitted):
                fetcher = submitted[future]
                try:
                    price, source = future.result()
                    if price > 0 and math.isfinite(price):
                        ticks[source] = PriceTick(symbol=symbol, price=price, source=source, timestamp=now)
                except (OSError, ValueError, KeyError, TimeoutError, urlerror.URLError, json.JSONDecodeError):
                    continue
        if not ticks:
            raise RuntimeError("real BTC price fallback unavailable: no public BTC price source succeeded")
        return ticks

    def _fetch_binance(self, symbol: str) -> tuple[float, str]:
        api_symbol = BINANCE_SYMBOLS[symbol]
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={api_symbol}"
        data = self._json_get(url)
        return float(data["price"]), "binance"

    def _fetch_coinbase(self, symbol: str) -> tuple[float, str]:
        api_symbol = COINBASE_SYMBOLS[symbol]
        url = f"https://api.coinbase.com/v2/prices/{api_symbol}/spot"
        data = self._json_get(url)
        return float(data["data"]["amount"]), "coinbase"

    def _fetch_okx(self, symbol: str) -> tuple[float, str]:
        api_symbol = OKX_SYMBOLS[symbol]
        url = f"https://www.okx.com/api/v5/market/ticker?instId={api_symbol}"
        data = self._json_get(url)
        rows = data.get("data") or []
        if not rows:
            raise ValueError("OKX ticker response missing data")
        return float(rows[0]["last"]), "okx"

    def _json_get(self, url: str) -> dict:
        req = urlrequest.Request(url, headers={"User-Agent": "polybot2other/0.1"})
        with urlrequest.urlopen(req, timeout=self.timeout_seconds) as response:
            payload = response.read(64 * 1024)
        return json.loads(payload.decode("utf-8"))

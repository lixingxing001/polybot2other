from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import MarketRound


BTC_5M_WINDOW_SECONDS = 300
MAX_BOOK_LEVELS = 50


@dataclass(frozen=True)
class PolymarketQuote:
    token_id: str
    outcome: str
    best_bid: float | None
    best_ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    bids: list[dict[str, float]] = field(default_factory=list)
    asks: list[dict[str, float]] = field(default_factory=list)
    updated_at_ms: int | None = None
    source: str = "rest"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PolymarketClient:
    def __init__(self, gamma_url: str, clob_url: str, timeout_seconds: float = 4.0) -> None:
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context()
        self._market_cache: MarketRound | None = None
        self._market_cache_until = 0.0
        self._target_cache: dict[str, float] = {}
        self._settlement_price_cache: dict[str, dict[str, Any]] = {}

    def find_current_btc_5m_market(self) -> MarketRound | None:
        now = time.time()
        if (
            self._market_cache
            and now < self._market_cache_until
            and self._market_cache.ends_at > now
            and self._market_cache.target_price > 0
        ):
            return self._market_cache

        current_window = int(now) // BTC_5M_WINDOW_SECONDS * BTC_5M_WINDOW_SECONDS
        for offset in (0, -1, 1):
            slug = f"btc-updown-5m-{current_window + offset * BTC_5M_WINDOW_SECONDS}"
            raw = self._get_event_by_slug(slug)
            market_raw = _first_event_market(raw) if raw else None
            market = self._parse_market(market_raw or raw)
            if market and market.started_at <= now < market.ends_at:
                self._market_cache = market
                self._market_cache_until = min(now + 2.0, market.ends_at - 0.5)
                return market
        return None

    def get_market_by_slug(self, slug: str) -> MarketRound | None:
        raw = self._get_event_by_slug(slug)
        market_raw = _first_event_market(raw) if raw else None
        return self._parse_market(market_raw or raw)

    def get_quotes(self, market: MarketRound) -> dict[str, PolymarketQuote]:
        return {
            "Up": self.get_quote(market.up_token, "Up"),
            "Down": self.get_quote(market.down_token, "Down"),
        }

    def get_quote(self, token_id: str, outcome: str) -> PolymarketQuote:
        book = self._get_json(f"{self.clob_url}/book", {"token_id": token_id})
        bids = _book_levels(book.get("bids"), reverse=True)
        asks = _book_levels(book.get("asks"), reverse=False)
        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        return PolymarketQuote(
            token_id=token_id,
            outcome=outcome,
            best_bid=_level_float(best_bid, "price"),
            best_ask=_level_float(best_ask, "price"),
            bid_size=_level_float(best_bid, "size"),
            ask_size=_level_float(best_ask, "size"),
            bids=bids,
            asks=asks,
            updated_at_ms=int(time.time() * 1000),
            source="rest",
        )

    def get_resolution(self, slug: str) -> dict[str, Any] | None:
        raw = self._get_event_by_slug(slug)
        market = _first_event_market(raw) if raw else None
        if not market:
            market = self._get_market_by_slug(slug)
        if not market or not _truthy(market.get("closed")):
            return None
        outcomes = _jsonish_list(market.get("outcomes"))
        prices = [_maybe_float(price) for price in _jsonish_list(market.get("outcomePrices"))]
        if len(outcomes) != len(prices) or not outcomes:
            return None
        winners = [str(outcome) for outcome, price in zip(outcomes, prices) if price is not None and price >= 0.999]
        if len(winners) != 1 or winners[0] not in {"Up", "Down"}:
            return None
        settlement_prices = _settlement_prices_from_market(market)
        if settlement_prices.get("final_price") is None or settlement_prices.get("target_price") is None:
            settlement_prices = _merge_settlement_prices(
                settlement_prices,
                self._settlement_prices_from_polymarket_page(slug),
            )
        return {
            "market_slug": slug,
            "outcome": winners[0],
            "price_source": "Gamma:outcomePrices",
            "final_price": settlement_prices.get("final_price"),
            "target_price": settlement_prices.get("target_price"),
            "settlement_price_source": settlement_prices.get("source"),
            "resolved_at": time.time(),
        }

    def _get_event_by_slug(self, slug: str) -> dict[str, Any] | None:
        try:
            data = self._get_json(f"{self.gamma_url}/events/slug/{slug}", {})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        except urllib.error.URLError:
            return None
        return data if isinstance(data, dict) else None

    def _get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        try:
            data = self._get_json(f"{self.gamma_url}/markets", {"slug": slug})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        except urllib.error.URLError:
            return None
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("slug") == slug:
                    return row
        if isinstance(data, dict) and data.get("slug") == slug:
            return data
        return None

    def _parse_market(self, raw: dict[str, Any] | None) -> MarketRound | None:
        if not raw:
            return None
        slug = str(raw.get("slug") or "")
        if not re.fullmatch(r"btc-updown-5m-\d+", slug):
            return None
        outcomes = _jsonish_list(raw.get("outcomes"))
        token_ids = _jsonish_list(raw.get("clobTokenIds"))
        if len(outcomes) != len(token_ids) or len(token_ids) < 2:
            return None
        token_by_outcome = {str(outcome).lower(): str(token) for outcome, token in zip(outcomes, token_ids)}
        up_token = token_by_outcome.get("up")
        down_token = token_by_outcome.get("down")
        if not up_token or not down_token:
            return None
        start_ts = _slug_start_ts(slug)
        end_ts = _parse_ts(raw.get("endDate")) or (start_ts + BTC_5M_WINDOW_SECONDS)
        target_price = _target_from_market(raw)
        if target_price is None:
            target_price = self._target_from_polymarket_page(slug)
        return MarketRound(
            round_id=slug,
            symbol="BTC",
            started_at=float(start_ts),
            ends_at=float(end_ts),
            target_price=float(target_price or 0.0),
            question=str(raw.get("question") or slug),
            condition_id=str(raw.get("conditionId") or ""),
            up_token=up_token,
            down_token=down_token,
            slug=slug,
            url=f"https://polymarket.com/event/{slug}",
        )

    def _target_from_polymarket_page(self, slug: str) -> float | None:
        cached = self._target_cache.get(slug)
        if cached is not None:
            return cached
        start_ts = _slug_start_ts(slug)
        end_ts = start_ts + BTC_5M_WINDOW_SECONDS
        try:
            html = self._get_text(f"https://polymarket.com/event/{urllib.parse.quote(slug)}")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError):
            return None
        target = _target_from_page(html, start_ts, end_ts)
        if target is not None:
            self._target_cache[slug] = target
        return target

    def _settlement_prices_from_polymarket_page(self, slug: str) -> dict[str, Any]:
        cached = self._settlement_price_cache.get(slug)
        if cached is not None:
            return dict(cached)
        start_ts = _slug_start_ts(slug)
        end_ts = start_ts + BTC_5M_WINDOW_SECONDS
        try:
            html = self._get_text(f"https://polymarket.com/event/{urllib.parse.quote(slug)}")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError):
            return {}
        settlement_prices = _settlement_prices_from_page(html, start_ts, end_ts)
        if settlement_prices.get("final_price") is not None or settlement_prices.get("target_price") is not None:
            self._settlement_price_cache[slug] = dict(settlement_prices)
        return settlement_prices

    def _get_json(self, url: str, params: dict[str, str]) -> Any:
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        req = urllib.request.Request(full_url, headers={"User-Agent": "polybot2other/0.2"})
        with urllib.request.urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "polybot2other/0.2"})
        with urllib.request.urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as response:
            return response.read().decode("utf-8", errors="replace")


def market_to_payload(market: MarketRound | None) -> dict[str, Any] | None:
    if market is None:
        return None
    return {
        "slug": market.slug or market.round_id,
        "round_id": market.round_id,
        "symbol": market.symbol,
        "question": market.question,
        "condition_id": market.condition_id,
        "up_token": market.up_token,
        "down_token": market.down_token,
        "start_ts": market.started_at,
        "end_ts": market.ends_at,
        "target_price": market.target_price,
        "url": market.url,
    }


def _first_event_market(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not event:
        return None
    markets = event.get("markets")
    if isinstance(markets, list) and markets and isinstance(markets[0], dict):
        market = dict(markets[0])
        metadata = event.get("eventMetadata")
        if isinstance(metadata, dict) and "eventMetadata" not in market:
            market["eventMetadata"] = metadata
        return market
    return None


def _jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parse_ts(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _slug_start_ts(slug: str) -> int:
    match = re.fullmatch(r"btc-updown-5m-(\d+)", slug)
    if not match:
        raise ValueError(f"invalid BTC 5m slug: {slug}")
    return int(match.group(1))


def _target_from_market(raw: dict[str, Any]) -> float | None:
    metadata = raw.get("eventMetadata")
    if isinstance(metadata, dict):
        target = _maybe_float(metadata.get("priceToBeat"))
        if target is not None:
            return target
    events = raw.get("events")
    event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else None
    metadata = event.get("eventMetadata") if event else None
    if isinstance(metadata, dict):
        target = _maybe_float(metadata.get("priceToBeat"))
        if target is not None:
            return target
    return None


def _settlement_prices_from_market(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = raw.get("eventMetadata")
    if not isinstance(metadata, dict):
        events = raw.get("events")
        event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else None
        metadata = event.get("eventMetadata") if event else None
    if not isinstance(metadata, dict):
        return {}
    final_price = _maybe_float(metadata.get("finalPrice"))
    target_price = _maybe_float(metadata.get("priceToBeat"))
    result: dict[str, Any] = {
        "final_price": final_price,
        "target_price": target_price,
    }
    if final_price is not None or target_price is not None:
        result["source"] = "Gamma:eventMetadata"
    return result


def _merge_settlement_prices(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if not fallback:
        return dict(primary)
    merged = dict(primary)
    filled_from_fallback = False
    for key in ("final_price", "target_price"):
        if merged.get(key) is None and fallback.get(key) is not None:
            merged[key] = fallback[key]
            filled_from_fallback = True
    if filled_from_fallback and merged.get("source") and fallback.get("source"):
        merged["source"] = f"{merged['source']}+{fallback['source']}"
    elif not merged.get("source") and (merged.get("final_price") is not None or merged.get("target_price") is not None):
        merged["source"] = fallback.get("source")
    return merged


def _settlement_prices_from_page(page: str, start_ts: int, end_ts: int) -> dict[str, Any]:
    metadata = _event_metadata_from_page(page)
    final_price = _maybe_float(metadata.get("finalPrice")) if metadata else None
    target_price = _maybe_float(metadata.get("priceToBeat")) if metadata else None
    if target_price is None:
        target_price = _target_from_page(page, start_ts, end_ts)
    if final_price is None:
        final_price = _final_price_from_page_window(page, start_ts, end_ts)
    result: dict[str, Any] = {
        "final_price": final_price,
        "target_price": target_price,
    }
    if final_price is not None or target_price is not None:
        result["source"] = "PolymarketPage:eventMetadata"
    return result


def _event_metadata_from_page(page: str) -> dict[str, Any] | None:
    match = re.search(r'"eventMetadata"\s*:\s*\{([^{}]{0,400})\}', page)
    if not match:
        return None
    metadata: dict[str, Any] = {}
    for key in ("finalPrice", "priceToBeat"):
        value = _regex_float(match.group(1), rf'"{key}"\s*:\s*(-?\d+(?:\.\d+)?)')
        if value is not None:
            metadata[key] = value
    return metadata or None


def _final_price_from_page_window(page: str, start_ts: int, end_ts: int) -> float | None:
    start_iso = _iso_ms_z(start_ts)
    end_iso = _iso_ms_z(end_ts)
    return _regex_float(
        page,
        rf'"startTime"\s*:\s*"{re.escape(start_iso)}".{{0,360}}?'
        rf'"endTime"\s*:\s*"{re.escape(end_iso)}".{{0,360}}?'
        r'"closePrice"\s*:\s*(-?\d+(?:\.\d+)?)',
    )


def _target_from_page(page: str, start_ts: int, end_ts: int) -> float | None:
    start_iso = _iso_z(start_ts)
    end_iso = _iso_z(end_ts)
    query_key = (
        f'"queryKey":["crypto-prices","price","BTC","{start_iso}",'
        f'"fiveminute","{end_iso}"]'
    )
    query_index = page.find(query_key)
    if query_index >= 0:
        segment = page[max(0, query_index - 1400) : query_index + len(query_key)]
        target = _regex_last_float(segment, r'"openPrice"\s*:\s*(-?\d+(?:\.\d+)?)')
        if target is not None:
            return target
    return _regex_float(
        page,
        rf'"endTime"\s*:\s*"{re.escape(start_iso)}".{{0,260}}?"closePrice"\s*:\s*(-?\d+(?:\.\d+)?)',
    )


def _iso_z(timestamp_s: int) -> str:
    return datetime.fromtimestamp(timestamp_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_ms_z(timestamp_s: int) -> str:
    return datetime.fromtimestamp(timestamp_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _regex_float(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text)
    if not match:
        return None
    return _maybe_float(match.group(1))


def _regex_last_float(text: str, pattern: str) -> float | None:
    matches = re.findall(pattern, text)
    if not matches:
        return None
    return _maybe_float(matches[-1])


def _level_float(level: Any, key: str) -> float | None:
    if not isinstance(level, dict):
        return None
    return _maybe_float(level.get(key))


def _book_levels(levels: Any, *, reverse: bool) -> list[dict[str, float]]:
    if not isinstance(levels, list):
        return []
    parsed: list[dict[str, float]] = []
    for row in levels:
        if not isinstance(row, dict):
            continue
        price = _maybe_float(row.get("price"))
        size = _maybe_float(row.get("size"))
        if price is None or size is None or price <= 0 or price >= 1 or size <= 0:
            continue
        parsed.append({"price": round(price, 4), "size": round(size, 6)})
    return sorted(parsed, key=lambda row: row["price"], reverse=reverse)[:MAX_BOOK_LEVELS]


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}

from __future__ import annotations

import json
import math
import re
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .models import MarketRound
from .polymarket import market_to_payload


DATA_API_DEFAULT_URL = "https://data-api.polymarket.com"
ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")


class ActorDataClient(Protocol):
    def get_market_holders(self, condition_id: str, limit: int = 20) -> list[dict[str, Any]]:
        ...

    def get_market_positions(self, condition_id: str, limit: int = 80) -> list[dict[str, Any]]:
        ...

    def get_market_trades(self, condition_id: str, limit: int = 100) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class SourceResult:
    ok: bool
    rows: list[dict[str, Any]]
    error: str | None = None


class PolymarketDataClient:
    """Small read-only client for Polymarket Data API."""

    def __init__(self, base_url: str = DATA_API_DEFAULT_URL, timeout_seconds: float = 4.0) -> None:
        self.base_url = (base_url or DATA_API_DEFAULT_URL).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.ssl_context = ssl.create_default_context()

    def get_market_holders(self, condition_id: str, limit: int = 20) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/holders",
            {
                "market": condition_id,
                "limit": str(max(1, min(20, int(limit)))),
                "minBalance": "1",
            },
        )
        return _flatten_token_children(payload, "holders")

    def get_market_positions(self, condition_id: str, limit: int = 80) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/v1/market-positions",
            {
                "market": condition_id,
                "status": "OPEN",
                "sortBy": "TOTAL_PNL",
                "sortDirection": "DESC",
                "limit": str(max(1, min(500, int(limit)))),
                "offset": "0",
            },
        )
        return _flatten_token_children(payload, "positions")

    def get_market_trades(self, condition_id: str, limit: int = 100) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/trades",
            {
                "market": condition_id,
                "limit": str(max(1, min(10_000, int(limit)))),
                "offset": "0",
                "takerOnly": "true",
            },
        )
        return _as_dict_list(payload)

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "polybot2other/0.2"})
        with urllib.request.urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as response:
            return json.loads(response.read().decode("utf-8"))


def build_actor_analysis(
    market: MarketRound | None,
    latest_price: dict[str, Any] | None,
    latest_quotes: dict[str, dict[str, Any]] | None,
    data_client: ActorDataClient,
    now: float | None = None,
) -> dict[str, Any]:
    checked_at = float(time.time() if now is None else now)
    if market is None:
        return _empty_analysis("NO_MARKET", checked_at, "当前没有可分析的 BTC 5m 市场。")
    if not market.condition_id:
        return _empty_analysis("NO_CONDITION_ID", checked_at, "当前市场缺少 condition_id，无法查询 Data API。", market)

    price = dict(latest_price or {})
    quotes = _copy_quotes(latest_quotes or {})
    holders = _safe_source(lambda: data_client.get_market_holders(market.condition_id, 20))
    positions = _safe_source(lambda: data_client.get_market_positions(market.condition_id, 80))
    trades = _safe_source(lambda: data_client.get_market_trades(market.condition_id, 100))
    sources = {
        "holders": _source_payload(holders),
        "positions": _source_payload(positions),
        "trades": _source_payload(trades),
    }
    wallet_rows = _aggregate_wallets(market, quotes, holders.rows, positions.rows, trades.rows)
    holder_rows = [_project_holder(row, market, quotes) for row in holders.rows]
    position_rows = [_project_position(row, market) for row in positions.rows]
    trade_rows = sorted(
        (_project_trade(row, market) for row in trades.rows),
        key=lambda row: row.get("timestamp") or 0,
        reverse=True,
    )
    probability = _probability_payload(market, price, quotes, wallet_rows, checked_at)
    summary = _summary_payload(holder_rows, position_rows, trade_rows, wallet_rows, probability)
    risk_tags = _risk_tags(summary, probability, wallet_rows, sources)
    failed_sources = [name for name, source in sources.items() if not source["ok"]]
    has_rows = bool(holder_rows or position_rows or trade_rows)
    status = "PARTIAL" if failed_sources else ("READY" if has_rows else "EMPTY")
    return {
        "analysis_only": True,
        "affects_trading": False,
        "can_identify_orderbook_addresses": False,
        "status": status,
        "checked_at": checked_at,
        "cached": False,
        "market": market_to_payload(market),
        "sources": sources,
        "summary": summary,
        "probability": probability,
        "wallets": wallet_rows[:12],
        "holders": holder_rows[:40],
        "positions": position_rows[:40],
        "trades": trade_rows[:60],
        "risk_tags": risk_tags,
        "notes": [
            "当前分析只读，不参与 signal、Paper 下单、实盘下单、卖出或撤单。",
            "公开订单簿快照不提供 maker 地址，因此只能从持仓、持有人和成交记录反推参与者画像。",
        ],
    }


def _empty_analysis(
    status: str,
    checked_at: float,
    note: str,
    market: MarketRound | None = None,
) -> dict[str, Any]:
    return {
        "analysis_only": True,
        "affects_trading": False,
        "can_identify_orderbook_addresses": False,
        "status": status,
        "checked_at": checked_at,
        "cached": False,
        "market": market_to_payload(market),
        "sources": {},
        "summary": {
            "wallet_count": 0,
            "holder_count": 0,
            "position_count": 0,
            "trade_count": 0,
            "up_exposure": 0.0,
            "down_exposure": 0.0,
            "active_wallet_count": 0,
        },
        "probability": {},
        "wallets": [],
        "holders": [],
        "positions": [],
        "trades": [],
        "risk_tags": [
            {
                "code": "PUBLIC_ORDERBOOK_ADDRESS_UNAVAILABLE",
                "label": "订单簿地址不可见",
                "severity": "info",
                "message": "公开订单簿不返回 maker 地址，不能把当前挂单直接归因到具体钱包。",
            }
        ],
        "notes": [note],
    }


def _safe_source(fetcher) -> SourceResult:
    try:
        rows = fetcher()
        return SourceResult(True, _as_dict_list(rows))
    except Exception as exc:  # noqa: BLE001 - analysis must degrade independently from trading.
        return SourceResult(False, [], _safe_error(exc))


def _source_payload(result: SourceResult) -> dict[str, Any]:
    return {"ok": result.ok, "count": len(result.rows), "error": result.error}


def _aggregate_wallets(
    market: MarketRound,
    quotes: dict[str, dict[str, Any]],
    holders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wallets: dict[str, dict[str, Any]] = {}
    for row in holders:
        address = _address_from_row(row)
        if not address:
            continue
        wallet = _wallet(wallets, address, row)
        side = _outcome_side(row, market)
        amount = _maybe_float(row.get("amount")) or _maybe_float(row.get("size")) or 0.0
        value = _value_from_amount(side, amount, quotes)
        _add_exposure(wallet, side, amount, value)
        wallet["holder_count"] += 1
        wallet["sources"].add("holders")

    for row in positions:
        address = _address_from_row(row)
        if not address:
            continue
        wallet = _wallet(wallets, address, row)
        side = _outcome_side(row, market)
        size = _maybe_float(row.get("size")) or 0.0
        value = _maybe_float(row.get("currentValue"))
        if value is None:
            value = size * (_maybe_float(row.get("currPrice")) or _quote_mid(side, quotes) or 0.0)
        pnl = _first_float(row, ("totalPnl", "cashPnl", "realizedPnl")) or 0.0
        _add_exposure(wallet, side, size, value)
        wallet["pnl"] += pnl
        wallet["position_count"] += 1
        wallet["sources"].add("positions")

    for row in trades:
        address = _address_from_row(row)
        if not address:
            continue
        wallet = _wallet(wallets, address, row)
        side = _outcome_side(row, market)
        action = str(row.get("side") or "").upper()
        size = _maybe_float(row.get("size")) or 0.0
        price = _maybe_float(row.get("price")) or 0.0
        notional = size * price
        wallet["trade_count"] += 1
        wallet["trade_notional"] += notional
        wallet["buy_count"] += 1 if action == "BUY" else 0
        wallet["sell_count"] += 1 if action == "SELL" else 0
        if side == "Up":
            wallet["up_trade_notional"] += notional if action != "SELL" else -notional
        elif side == "Down":
            wallet["down_trade_notional"] += notional if action != "SELL" else -notional
        ts = _timestamp_seconds(row.get("timestamp"))
        if ts is not None:
            wallet["last_trade_ts"] = max(wallet["last_trade_ts"] or 0.0, ts)
        wallet["sources"].add("trades")

    rows = [_finalize_wallet(row) for row in wallets.values()]
    return sorted(rows, key=lambda row: (row["exposure_value"], row["trade_notional"]), reverse=True)


def _wallet(wallets: dict[str, dict[str, Any]], address: str, row: dict[str, Any]) -> dict[str, Any]:
    if address not in wallets:
        wallets[address] = {
            "address": address,
            "name": _display_name(row),
            "up_shares": 0.0,
            "down_shares": 0.0,
            "up_value": 0.0,
            "down_value": 0.0,
            "up_trade_notional": 0.0,
            "down_trade_notional": 0.0,
            "pnl": 0.0,
            "holder_count": 0,
            "position_count": 0,
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "trade_notional": 0.0,
            "last_trade_ts": None,
            "sources": set(),
        }
    else:
        existing = wallets[address]
        if not existing.get("name"):
            existing["name"] = _display_name(row)
    return wallets[address]


def _finalize_wallet(wallet: dict[str, Any]) -> dict[str, Any]:
    up_value = float(wallet["up_value"])
    down_value = float(wallet["down_value"])
    exposure = up_value + down_value
    up_score = up_value + max(0.0, float(wallet["up_trade_notional"]))
    down_score = down_value + max(0.0, float(wallet["down_trade_notional"]))
    bias = "Balanced"
    if up_score > down_score * 1.2 and up_score > 0:
        bias = "Up"
    elif down_score > up_score * 1.2 and down_score > 0:
        bias = "Down"
    tags = _wallet_tags(wallet, exposure, bias)
    return {
        "address": wallet["address"],
        "short_address": _short_address(wallet["address"]),
        "name": wallet.get("name") or "",
        "bias": bias,
        "up_shares": _round_float(wallet["up_shares"], 4),
        "down_shares": _round_float(wallet["down_shares"], 4),
        "up_value": _round_float(up_value, 4),
        "down_value": _round_float(down_value, 4),
        "exposure_value": _round_float(exposure, 4),
        "trade_notional": _round_float(wallet["trade_notional"], 4),
        "pnl": _round_float(wallet["pnl"], 4),
        "holder_count": wallet["holder_count"],
        "position_count": wallet["position_count"],
        "trade_count": wallet["trade_count"],
        "buy_count": wallet["buy_count"],
        "sell_count": wallet["sell_count"],
        "last_trade_ts": wallet["last_trade_ts"],
        "sources": sorted(wallet["sources"]),
        "tags": tags,
    }


def _wallet_tags(wallet: dict[str, Any], exposure: float, bias: str) -> list[str]:
    tags: list[str] = []
    if exposure >= 100.0 or wallet["trade_notional"] >= 100.0:
        tags.append("WHALE")
    if wallet["trade_count"] >= 3:
        tags.append("ACTIVE_CURRENT_MARKET")
    if bias in {"Up", "Down"}:
        tags.append(f"{bias.upper()}_BIASED")
    if wallet["pnl"] > 0:
        tags.append("CURRENT_PNL_POSITIVE")
    elif wallet["pnl"] < 0:
        tags.append("CURRENT_PNL_NEGATIVE")
    return tags or ["OBSERVED"]


def _project_holder(row: dict[str, Any], market: MarketRound, quotes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    side = _outcome_side(row, market)
    amount = _maybe_float(row.get("amount")) or 0.0
    return {
        "address": _address_from_row(row),
        "short_address": _short_address(_address_from_row(row) or ""),
        "name": _display_name(row),
        "side": side,
        "amount": _round_float(amount, 4),
        "estimated_value": _round_float(_value_from_amount(side, amount, quotes), 4),
        "asset": str(row.get("asset") or row.get("token") or row.get("_source_token") or ""),
        "outcome_index": row.get("outcomeIndex"),
    }


def _project_position(row: dict[str, Any], market: MarketRound) -> dict[str, Any]:
    return {
        "address": _address_from_row(row),
        "short_address": _short_address(_address_from_row(row) or ""),
        "name": _display_name(row),
        "side": _outcome_side(row, market),
        "size": _round_float(_maybe_float(row.get("size")), 4),
        "avg_price": _round_float(_maybe_float(row.get("avgPrice")), 4),
        "current_price": _round_float(_maybe_float(row.get("currPrice")), 4),
        "current_value": _round_float(_maybe_float(row.get("currentValue")), 4),
        "cash_pnl": _round_float(_maybe_float(row.get("cashPnl")), 4),
        "realized_pnl": _round_float(_maybe_float(row.get("realizedPnl")), 4),
        "total_pnl": _round_float(_maybe_float(row.get("totalPnl")), 4),
        "asset": str(row.get("asset") or row.get("token") or row.get("_source_token") or ""),
    }


def _project_trade(row: dict[str, Any], market: MarketRound) -> dict[str, Any]:
    size = _maybe_float(row.get("size")) or 0.0
    price = _maybe_float(row.get("price")) or 0.0
    return {
        "address": _address_from_row(row),
        "short_address": _short_address(_address_from_row(row) or ""),
        "name": _display_name(row),
        "side": _outcome_side(row, market),
        "action": str(row.get("side") or "").upper(),
        "size": _round_float(size, 4),
        "price": _round_float(price, 4),
        "notional": _round_float(size * price, 4),
        "timestamp": _timestamp_seconds(row.get("timestamp")),
        "transaction_hash": str(row.get("transactionHash") or ""),
    }


def _summary_payload(
    holders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    wallets: list[dict[str, Any]],
    probability: dict[str, Any],
) -> dict[str, Any]:
    up_exposure = sum(_maybe_float(row.get("up_value")) or 0.0 for row in wallets)
    down_exposure = sum(_maybe_float(row.get("down_value")) or 0.0 for row in wallets)
    active_wallets = [row for row in wallets if int(row.get("trade_count") or 0) >= 3]
    return {
        "wallet_count": len(wallets),
        "holder_count": len(holders),
        "position_count": len(positions),
        "trade_count": len(trades),
        "active_wallet_count": len(active_wallets),
        "up_exposure": _round_float(up_exposure, 4),
        "down_exposure": _round_float(down_exposure, 4),
        "top_wallet_exposure": _round_float(wallets[0]["exposure_value"], 4) if wallets else 0.0,
        "top_wallet_share_pct": _round_float(
            (wallets[0]["exposure_value"] / (up_exposure + down_exposure) * 100.0)
            if wallets and (up_exposure + down_exposure) > 0
            else 0.0,
            2,
        ),
        "direction": probability.get("direction"),
        "combined_up": probability.get("combined_up"),
        "combined_down": probability.get("combined_down"),
    }


def _probability_payload(
    market: MarketRound,
    price: dict[str, Any],
    quotes: dict[str, dict[str, Any]],
    wallets: list[dict[str, Any]],
    now: float,
) -> dict[str, Any]:
    up_mid = _quote_mid("Up", quotes)
    down_mid = _quote_mid("Down", quotes)
    if up_mid is None and down_mid is not None:
        up_mid = 1.0 - down_mid
    market_implied_up = _clamp01(up_mid) if up_mid is not None else None
    current_price = _first_float(
        price,
        ("chainlink", "binance_market", "binance", "okx"),
    )
    price_model_up = _price_model_up(market, current_price, now)
    up_exposure = sum(_maybe_float(row.get("up_value")) or 0.0 for row in wallets)
    down_exposure = sum(_maybe_float(row.get("down_value")) or 0.0 for row in wallets)
    actor_up = up_exposure / (up_exposure + down_exposure) if up_exposure + down_exposure > 0 else None
    combined = _weighted_probability(
        [
            (market_implied_up, 0.50),
            (price_model_up, 0.35),
            (actor_up, 0.15),
        ]
    )
    direction = "Balanced"
    if combined is not None and combined >= 0.55:
        direction = "Up"
    elif combined is not None and combined <= 0.45:
        direction = "Down"
    return {
        "direction": direction,
        "combined_up": _round_float(combined, 4),
        "combined_down": _round_float(1.0 - combined, 4) if combined is not None else None,
        "confidence_pct": _round_float(abs((combined or 0.5) - 0.5) * 200.0, 2) if combined is not None else None,
        "market_implied_up": _round_float(market_implied_up, 4),
        "price_model_up": _round_float(price_model_up, 4),
        "actor_up_ratio": _round_float(actor_up, 4),
        "current_price": _round_float(current_price, 2),
        "target_price": _round_float(market.target_price, 2),
        "distance_bps": _round_float(
            ((current_price - market.target_price) / market.target_price * 10_000.0)
            if current_price is not None and market.target_price > 0
            else None,
            2,
        ),
        "seconds_left": _round_float(max(0.0, market.ends_at - now), 1),
    }


def _risk_tags(
    summary: dict[str, Any],
    probability: dict[str, Any],
    wallets: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    tags = [
        {
            "code": "PUBLIC_ORDERBOOK_ADDRESS_UNAVAILABLE",
            "label": "订单簿地址不可见",
            "severity": "info",
            "message": "公开订单簿不返回 maker 地址，当前不能识别挂单背后的具体钱包。",
        }
    ]
    if any(not source.get("ok") for source in sources.values()):
        tags.append(
            {
                "code": "DATA_PARTIAL",
                "label": "数据不完整",
                "severity": "warning",
                "message": "至少一个 Data API 来源失败，当前画像只能作为部分样本观察。",
            }
        )
    if (_maybe_float(summary.get("top_wallet_share_pct")) or 0.0) >= 45.0:
        tags.append(
            {
                "code": "HOLDER_CONCENTRATION",
                "label": "头部地址集中",
                "severity": "warning",
                "message": f"最大地址约占已识别敞口 {summary.get('top_wallet_share_pct')}%。",
            }
        )
    actor_up = _maybe_float(probability.get("actor_up_ratio"))
    if actor_up is not None and actor_up >= 0.65:
        tags.append(
            {
                "code": "ACTOR_UP_BIASED",
                "label": "地址偏 Up",
                "severity": "info",
                "message": "已识别地址敞口明显偏向 Up。",
            }
        )
    elif actor_up is not None and actor_up <= 0.35:
        tags.append(
            {
                "code": "ACTOR_DOWN_BIASED",
                "label": "地址偏 Down",
                "severity": "info",
                "message": "已识别地址敞口明显偏向 Down。",
            }
        )
    active_count = int(summary.get("active_wallet_count") or 0)
    if active_count > 0:
        tags.append(
            {
                "code": "ACTIVE_WALLETS",
                "label": "活跃地址",
                "severity": "info",
                "message": f"{active_count} 个地址在当前市场有多笔成交记录。",
            }
        )
    if wallets and any("WHALE" in row.get("tags", []) for row in wallets[:3]):
        tags.append(
            {
                "code": "LARGE_ACTOR_OBSERVED",
                "label": "大额地址出现",
                "severity": "warning",
                "message": "Top 地址中存在大额持仓或大额成交地址。",
            }
        )
    return tags


def _price_model_up(market: MarketRound, current_price: float | None, now: float) -> float | None:
    if current_price is None or current_price <= 0 or market.target_price <= 0:
        return None
    seconds_left = max(1.0, market.ends_at - now)
    distance_bps = (current_price - market.target_price) / market.target_price * 10_000.0
    remaining_vol_bps = max(0.5, 12.0 * math.sqrt(seconds_left / 300.0))
    return _clamp01(0.5 * (1.0 + math.erf(distance_bps / remaining_vol_bps / math.sqrt(2.0))))


def _weighted_probability(values: list[tuple[float | None, float]]) -> float | None:
    available = [(value, weight) for value, weight in values if value is not None]
    if not available:
        return None
    total_weight = sum(weight for _, weight in available)
    if total_weight <= 0:
        return None
    return _clamp01(sum(float(value) * weight for value, weight in available) / total_weight)


def _flatten_token_children(payload: Any, child_key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _as_dict_list(payload):
        token = str(item.get("token") or "")
        children = item.get(child_key)
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                row = dict(child)
                row.setdefault("asset", token or row.get("asset"))
                if token:
                    row["_source_token"] = token
                rows.append(row)
            continue
        rows.append(dict(item))
    return rows


def _as_dict_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "rows", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(row) for row in value if isinstance(row, dict)]
        return [dict(payload)]
    return []


def _copy_quotes(quotes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    for side, row in quotes.items():
        if isinstance(row, dict):
            copied[str(side)] = dict(row)
    return copied


def _add_exposure(wallet: dict[str, Any], side: str | None, shares: float, value: float) -> None:
    if side == "Up":
        wallet["up_shares"] += shares
        wallet["up_value"] += value
    elif side == "Down":
        wallet["down_shares"] += shares
        wallet["down_value"] += value


def _value_from_amount(side: str | None, amount: float, quotes: dict[str, dict[str, Any]]) -> float:
    mid = _quote_mid(side, quotes) if side else None
    return amount * mid if mid is not None else 0.0


def _outcome_side(row: dict[str, Any], market: MarketRound) -> str | None:
    asset = str(row.get("asset") or row.get("token") or row.get("_source_token") or "")
    if asset and asset == market.up_token:
        return "Up"
    if asset and asset == market.down_token:
        return "Down"
    outcome = str(row.get("outcome") or "").strip().lower()
    if outcome == "up":
        return "Up"
    if outcome == "down":
        return "Down"
    outcome_index = row.get("outcomeIndex")
    if str(outcome_index) == "0":
        return "Up"
    if str(outcome_index) == "1":
        return "Down"
    return None


def _address_from_row(row: dict[str, Any]) -> str | None:
    for key in ("proxyWallet", "proxy_wallet", "wallet", "address", "user", "maker", "taker"):
        value = str(row.get(key) or "").strip()
        if ADDRESS_PATTERN.fullmatch(value):
            return value.lower()
    return None


def _display_name(row: dict[str, Any]) -> str:
    for key in ("name", "pseudonym", "username"):
        value = str(row.get(key) or "").strip()
        if value:
            return value[:80]
    return ""


def _quote_mid(side: str | None, quotes: dict[str, dict[str, Any]]) -> float | None:
    if not side:
        return None
    quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
    bid = _maybe_float(quote.get("best_bid"))
    ask = _maybe_float(quote.get("best_ask"))
    if bid is not None and ask is not None:
        return _clamp01((bid + ask) / 2.0)
    if bid is not None:
        return _clamp01(bid)
    if ask is not None:
        return _clamp01(ask)
    return None


def _first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _maybe_float(row.get(key))
        if value is not None:
            return value
    return None


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _timestamp_seconds(value: Any) -> float | None:
    parsed = _maybe_float(value)
    if parsed is None:
        return None
    return parsed / 1000.0 if parsed > 10_000_000_000 else parsed


def _round_float(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _short_address(address: str) -> str:
    if not address:
        return ""
    return f"{address[:6]}...{address[-4:]}" if len(address) > 12 else address


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip()
    if len(message) > 180:
        message = f"{message[:177]}..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__

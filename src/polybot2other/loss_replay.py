from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import MarketRound, Signal


LOSS_REPLAY_SCHEMA_VERSION = 1


class AggressiveEdgeLossReplayRecorder:
    """Aggressive Edge 输局复盘证据包；运行中只放内存，确认输局后才落盘。"""

    def __init__(
        self,
        path: Path,
        *,
        variant_id: str,
        combo: str,
        sample_interval_seconds: float = 1.0,
        max_samples_per_round: int = 360,
        max_active_rounds: int = 8,
        quote_depth: int = 10,
    ) -> None:
        self.path = path
        self.variant_id = variant_id
        self.combo = combo
        self.sample_interval_seconds = max(0.2, float(sample_interval_seconds))
        self.max_samples_per_round = max(20, int(max_samples_per_round))
        self.max_active_rounds = max(2, int(max_active_rounds))
        self.quote_depth = max(1, min(20, int(quote_depth)))
        self._rounds: dict[str, dict[str, Any]] = {}

    def record_sample(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        signal: Signal | None,
        *,
        event: str,
        now: float | None = None,
        force: bool = False,
        trade_ids: list[int] | None = None,
    ) -> None:
        now = time.time() if now is None else now
        round_id = str(market.round_id or "").strip()
        if not round_id:
            return
        replay = self._rounds.get(round_id)
        if replay is None:
            replay = self._new_replay(market, now)
            self._rounds[round_id] = replay
        if trade_ids:
            known_ids = replay.setdefault("trade_ids", [])
            for trade_id in trade_ids:
                if trade_id not in known_ids:
                    known_ids.append(int(trade_id))
        last_sample_at = _float_or_none(replay.get("last_sample_at"))
        if not force and last_sample_at is not None and now - last_sample_at < self.sample_interval_seconds:
            self._prune_rounds(now)
            return

        sample = {
            "at": round(now, 6),
            "event": str(event or "tick"),
            "force": bool(force),
            "seconds_from_start": _round_float(now - market.started_at, 3),
            "seconds_to_end": _round_float(market.ends_at - now, 3),
            "target_price": _round_float(market.target_price, 8),
            "price": _price_snapshot(price, market.target_price, now),
            "quotes": _quote_snapshot(quotes, self.quote_depth, now),
            "signal": _signal_snapshot(signal),
            "trade_ids": [int(trade_id) for trade_id in trade_ids or []],
        }
        samples = replay.setdefault("samples", [])
        samples.append(sample)
        replay["last_sample_at"] = now
        replay["last_event"] = sample["event"]
        self._trim_samples(samples)
        self._prune_rounds(now)

    def finalize_official_round(
        self,
        round_id: str,
        outcome: str,
        *,
        now: float,
        final_price: float | None,
        target_price: float | None,
        trades: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        replay = self._rounds.pop(str(round_id or "").strip(), None)
        if replay is None:
            return None
        replay_trade_ids = {int(item) for item in replay.get("trade_ids") or []}
        settled_losses: list[dict[str, Any]] = []
        for trade in trades:
            pnl = _float_or_none(trade.get("pnl"))
            trade_id = _int_or_none(trade.get("id"))
            if pnl is None or pnl >= 0:
                continue
            if replay_trade_ids and trade_id not in replay_trade_ids:
                continue
            settled_losses.append(_trade_snapshot(trade))
        if not settled_losses:
            return None

        packet = {
            "schema_version": LOSS_REPLAY_SCHEMA_VERSION,
            "generated_at": round(now, 6),
            "variant_id": self.variant_id,
            "combo": self.combo,
            "round": replay.get("round"),
            "settlement": {
                "outcome": str(outcome or ""),
                "final_price": _round_float(final_price, 8),
                "target_price": _round_float(target_price, 8),
                "final_distance_bps": _distance_bps(final_price, target_price),
            },
            "loss_trades": settled_losses,
            "sample_count": len(replay.get("samples") or []),
            "summary": _replay_summary(replay.get("samples") or []),
            "samples": replay.get("samples") or [],
        }
        self._append_packet(packet)
        return packet

    def discard_round(self, round_id: str) -> None:
        self._rounds.pop(str(round_id or "").strip(), None)

    def _new_replay(self, market: MarketRound, now: float) -> dict[str, Any]:
        return {
            "created_at": round(now, 6),
            "last_sample_at": None,
            "last_event": None,
            "trade_ids": [],
            "round": {
                "round_id": market.round_id,
                "symbol": market.symbol,
                "started_at": round(market.started_at, 6),
                "ends_at": round(market.ends_at, 6),
                "target_price": _round_float(market.target_price, 8),
                "question": market.question,
                "condition_id": market.condition_id,
                "up_token": market.up_token,
                "down_token": market.down_token,
                "url": market.url,
            },
            "samples": [],
        }

    def _trim_samples(self, samples: list[dict[str, Any]]) -> None:
        while len(samples) > self.max_samples_per_round:
            remove_index = 1 if len(samples) > 1 else 0
            for index, sample in enumerate(samples[1:], start=1):
                if not sample.get("force"):
                    remove_index = index
                    break
            del samples[remove_index]

    def _prune_rounds(self, now: float) -> None:
        stale_without_trade = [
            round_id
            for round_id, replay in self._rounds.items()
            if not replay.get("trade_ids")
            and _float_or_none((replay.get("round") or {}).get("ends_at")) is not None
            and now - float((replay.get("round") or {}).get("ends_at")) > 600.0
        ]
        for round_id in stale_without_trade:
            self._rounds.pop(round_id, None)
        if len(self._rounds) <= self.max_active_rounds:
            return
        candidates = sorted(
            self._rounds.items(),
            key=lambda item: (bool(item[1].get("trade_ids")), _float_or_none(item[1].get("last_sample_at")) or 0.0),
        )
        for round_id, _replay in candidates:
            if len(self._rounds) <= self.max_active_rounds:
                break
            self._rounds.pop(round_id, None)

    def _append_packet(self, packet: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")


def _price_snapshot(price: dict[str, Any], target_price: float, now: float) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "target_price": _round_float(target_price, 8),
        "source": str(price.get("source") or ""),
    }
    for source in ("chainlink", "okx", "binance", "binance_market"):
        value = _float_or_none(price.get(source))
        updated_ms = _int_or_none(price.get(f"{source}_updated_ms"))
        if source == "binance":
            updated_ms = updated_ms or _int_or_none(price.get("binance_market_updated_ms"))
        if value is None:
            continue
        snapshot[source] = {
            "price": _round_float(value, 8),
            "updated_ms": updated_ms,
            "age_ms": _age_ms(updated_ms, now),
            "distance_bps": _distance_bps(value, target_price),
        }
        for suffix in ("basis_latest_bps", "basis_median_bps", "basis_samples"):
            key = f"{source}_{suffix}"
            if key in price:
                snapshot[source][suffix] = _round_float(_float_or_none(price.get(key)), 6)
    return snapshot


def _quote_snapshot(quotes: dict[str, dict[str, Any]], quote_depth: int, now: float) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for side in ("Up", "Down"):
        quote = quotes.get(side) if isinstance(quotes.get(side), dict) else {}
        best_bid = _float_or_none(quote.get("best_bid"))
        best_ask = _float_or_none(quote.get("best_ask"))
        bid_size = _float_or_none(quote.get("bid_size"))
        ask_size = _float_or_none(quote.get("ask_size"))
        bids = _levels_snapshot(quote.get("bids"), quote_depth)
        asks = _levels_snapshot(quote.get("asks"), quote_depth)
        updated_ms = _int_or_none(quote.get("updated_at_ms"))
        snapshot[side] = {
            "best_bid": _round_float(best_bid, 4),
            "best_ask": _round_float(best_ask, 4),
            "bid_size": _round_float(bid_size, 6),
            "ask_size": _round_float(ask_size, 6),
            "spread": _round_float(best_ask - best_bid, 4) if best_bid is not None and best_ask is not None else None,
            "mid": _round_float((best_bid + best_ask) / 2.0, 4)
            if best_bid is not None and best_ask is not None
            else None,
            "updated_ms": updated_ms,
            "age_ms": _age_ms(updated_ms, now),
            "bids": bids,
            "asks": asks,
            "bid_depth_shares": _round_float(sum(level["size"] for level in bids), 6),
            "ask_depth_shares": _round_float(sum(level["size"] for level in asks), 6),
            "bid_depth_notional": _round_float(sum(level["price"] * level["size"] for level in bids), 6),
            "ask_depth_notional": _round_float(sum(level["price"] * level["size"] for level in asks), 6),
        }
    return snapshot


def _levels_snapshot(value: Any, quote_depth: int) -> list[dict[str, float]]:
    if not isinstance(value, list):
        return []
    levels: list[dict[str, float]] = []
    for item in value[:quote_depth]:
        if not isinstance(item, dict):
            continue
        price = _float_or_none(item.get("price"))
        size = _float_or_none(item.get("size"))
        if price is None or size is None:
            continue
        levels.append({"price": round(price, 4), "size": round(size, 6)})
    return levels


def _signal_snapshot(signal: Signal | None) -> dict[str, Any] | None:
    if signal is None:
        return None
    confidence = _float_or_none(signal.confidence)
    entry_price = _float_or_none(signal.entry_price)
    move_bps = _float_or_none(signal.move_bps)
    return {
        "symbol": signal.symbol,
        "side": signal.side,
        "confidence": _round_float(confidence, 6),
        "entry_price": _round_float(entry_price, 6),
        "move_bps": _round_float(move_bps, 6),
        "edge": _round_float(confidence - entry_price, 6)
        if confidence is not None and entry_price is not None
        else None,
        "reason": str(signal.reason or "")[:1200],
    }


def _trade_snapshot(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _int_or_none(trade.get("id")),
        "round_id": str(trade.get("round_id") or ""),
        "side": str(trade.get("side") or ""),
        "stake": _round_float(_float_or_none(trade.get("stake")), 6),
        "entry_price": _round_float(_float_or_none(trade.get("entry_price")), 6),
        "shares": _round_float(_float_or_none(trade.get("shares")), 6),
        "confidence": _round_float(_float_or_none(trade.get("confidence")), 6),
        "move_bps": _round_float(_float_or_none(trade.get("move_bps")), 6),
        "opened_at": _round_float(_float_or_none(trade.get("opened_at")), 6),
        "settled_at": _round_float(_float_or_none(trade.get("settled_at")), 6),
        "pnl": _round_float(_float_or_none(trade.get("pnl")), 6),
        "payout": _round_float(_float_or_none(trade.get("payout")), 6),
        "settlement_source": str(trade.get("settlement_source") or ""),
        "reason": str(trade.get("reason") or "")[:1200],
    }


def _replay_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    chainlink_bps = [
        value
        for value in (
            _float_or_none(((sample.get("price") or {}).get("chainlink") or {}).get("distance_bps"))
            for sample in samples
        )
        if value is not None
    ]
    up_spreads = [
        value
        for value in (
            _float_or_none(((sample.get("quotes") or {}).get("Up") or {}).get("spread")) for sample in samples
        )
        if value is not None
    ]
    down_spreads = [
        value
        for value in (
            _float_or_none(((sample.get("quotes") or {}).get("Down") or {}).get("spread")) for sample in samples
        )
        if value is not None
    ]
    return {
        "first_sample_at": samples[0].get("at") if samples else None,
        "last_sample_at": samples[-1].get("at") if samples else None,
        "forced_samples": sum(1 for sample in samples if sample.get("force")),
        "events": sorted({str(sample.get("event") or "") for sample in samples if sample.get("event")}),
        "chainlink_distance_bps_min": _round_float(min(chainlink_bps), 6) if chainlink_bps else None,
        "chainlink_distance_bps_max": _round_float(max(chainlink_bps), 6) if chainlink_bps else None,
        "up_spread_min": _round_float(min(up_spreads), 6) if up_spreads else None,
        "up_spread_max": _round_float(max(up_spreads), 6) if up_spreads else None,
        "down_spread_min": _round_float(min(down_spreads), 6) if down_spreads else None,
        "down_spread_max": _round_float(max(down_spreads), 6) if down_spreads else None,
    }


def _distance_bps(price: float | None, target: float | None) -> float | None:
    if price is None or target is None or target <= 0:
        return None
    return round((price - target) / target * 10_000.0, 6)


def _age_ms(updated_ms: int | None, now: float) -> int | None:
    if updated_ms is None or updated_ms <= 0:
        return None
    return max(0, int(now * 1000) - updated_ms)


def _round_float(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

"""BTC five-minute direction decisions for the isolated Paper account.

The model score is uncalibrated. Entry/exit margins are fixed experimental
choices, measured against prospective decisions and actual simulated fills.
"""

from dataclasses import asdict, dataclass, replace
import math

from .managed_live_client import fee_amount
from .models import Signal
from .optimized_main import forecast_twap
from .optimized_reference import SPOT, TWAP
from .shared_market_inputs import quote_error

MIN_ENTRY_SECONDS = 20
MAX_ENTRY_SECONDS = 180
EARLY_MAX_ENTRY_SECONDS = 240
MIN_ENTRY_SCORE = .62
ENTRY_EDGE = .04
MAX_ENTRY_PRICE = .80
LIMIT_060 = .60
MAX_SPREAD = .03
REVERSAL_SCORE = .42
EXIT_PREMIUM = .02
VERSION = "forecast-paper-v1"


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _ObservedReferences:
    """Spots must already have reached the server when the frame was captured."""

    def __init__(self, cache, at):
        self.cache = cache
        self.cutoff_ms = at * 1000

    def points(self, channel, start_ms, end_ms):
        with self.cache.lock:
            return self.cache.conn.execute(
                """SELECT source_ms,price FROM reference_ticks
                   WHERE channel=? AND source_ms BETWEEN ? AND ? AND received_ms<=?
                   ORDER BY source_ms""",
                (channel, int(start_ms), int(end_ms), self.cutoff_ms),
            ).fetchall()


@dataclass(frozen=True)
class ForecastDecision:
    action: str
    reason: str
    round_id: str | None
    decision_at: float
    snapshot_id: str | None
    time_left_seconds: float | None = None
    target_price: float | None = None
    up_score: float | None = None
    forecast_direction: str | None = None
    projected_final_twap: float | None = None
    remaining_average_to_tie: float | None = None
    forecast_source_ms: float | None = None
    side: str | None = None
    score: float | None = None
    entry_price: float | None = None
    limit: float | None = None
    all_in_cost: float | None = None
    net_bid: float | None = None
    min_net_bid: float | None = None
    move_bps: float = 0.0

    def as_signal(self):
        # Only a BUY may pass the execution service's entry signal gate.
        side = self.side if self.action == "BUY" else "NO_TRADE"
        return Signal("BTC", side, self.score or 0., self.entry_price or 0., self.move_bps, self.reason)

    def as_features(self):
        return {"version": VERSION, **asdict(self)}


def _fresh_reference(latest, channel, at):
    tick = latest.get(channel, {})
    price, source, received = (tick.get(k) for k in ("price", "source_ms", "received_ms"))
    return (all(_finite(x) for x in (price, source, received)) and price > 0
            and 0 <= at * 1000 - source <= 4000 and 0 <= at * 1000 - received <= 2500)


class ForecastPaperPolicy:
    def __init__(self, references, *, max_entry_seconds=MAX_ENTRY_SECONDS, max_order_limit=None):
        self.references = references
        self.max_entry_seconds = max_entry_seconds
        self.max_order_limit = max_order_limit

    def evaluate(self, frame, info, *, position=None, net_bid=None):
        at, market = frame.observed_at, frame.market
        held = position is not None
        decision = ForecastDecision(
            action="HOLD" if held else "WAIT", reason="等待本局结算基准和新鲜行情",
            round_id=market.round_id if market else None, decision_at=at,
            snapshot_id=frame.snapshot_id, side=position["side"] if held else None,
        )
        if market is None:
            return decision
        left = market.ends_at - at
        decision = replace(decision, target_price=market.target_price, time_left_seconds=left)
        if held and (position["round_id"] != market.round_id or position["side"] not in ("Up", "Down")):
            return replace(decision, reason="持仓属于另一期或方向无效，等待核对")
        if at < market.started_at or left <= 0:
            return replace(decision, reason="本局尚未开始或已经结束，等待官方结果")
        latest = frame.reference.get("latest", {})
        if (frame.error or not frame.reference.get("ready") or not frame.reference.get("protocol_verified")
                or not _finite(market.target_price) or market.target_price <= 0
                or not all(_fresh_reference(latest, channel, at) for channel in (SPOT, TWAP))):
            return decision
        prediction = forecast_twap(_ObservedReferences(self.references, at), market, latest)
        if prediction is None:
            return replace(decision, reason="历史行情不足，暂时看不清")
        up = prediction["up_score"]
        projected = prediction["projected_final_twap"]
        if not _finite(up) or not 0 <= up <= 1 or not _finite(projected) or projected <= 0:
            return replace(decision, reason="本次预测数据无效，等待下一次更新")
        forecast_side = "Up" if up >= .5 else "Down"
        side = position["side"] if held else forecast_side
        source = latest[SPOT]["source_ms"]
        horizon = market.ends_at - source / 1000
        # Before the last minute there is no fixed part of the closing average.
        to_tie = None
        if 0 < horizon < 60:
            to_tie = latest[SPOT]["price"] + (market.target_price - projected) * 60 / horizon
        decision = replace(
            decision, side=side, up_score=up, score=up if side == "Up" else 1 - up,
            forecast_direction=forecast_side if max(up, 1 - up) >= MIN_ENTRY_SCORE else None,
            projected_final_twap=projected, remaining_average_to_tie=to_tie, forecast_source_ms=source,
            move_bps=(projected - market.target_price) / market.target_price * 10000,
        )
        if held and left <= 2:
            return replace(decision, reason="已临近收盘，等待官方结算")
        if not held and not MIN_ENTRY_SECONDS <= left <= self.max_entry_seconds:
            return replace(decision, reason=f"等待距收盘 {MIN_ENTRY_SECONDS} 到 {self.max_entry_seconds} 秒的入场时段")
        if not held and decision.forecast_direction is None:
            return replace(decision, reason="涨跌方向还不够明确，先观察")
        error = quote_error(market, frame.quotes, side, at)
        if error:
            return replace(decision, reason=error)
        if held:
            return self._position_decision(decision, net_bid, frame.quotes[side]["best_bid"])
        return self._entry_decision(decision, frame.quotes[side], info)

    def _entry_decision(self, decision, quote, info):
        ask, bid = quote["best_ask"], quote["best_bid"]
        decision = replace(decision, entry_price=ask)
        if ask > MAX_ENTRY_PRICE or ask - bid > MAX_SPREAD + 1e-9:
            return replace(decision, reason="买价或买卖差价偏高，等待更好的盘口")
        asks = [level["price"] for level in quote.get("asks", ()) if level.get("size", 0) > 0]
        if not asks or abs(min(asks) - ask) > 1e-6:
            return replace(decision, reason="卖一价与卖盘深度不一致，等待同步")
        if not info or str(info.get("tick_size")) not in {"0.1", "0.01", "0.001", "0.0001"}:
            return replace(decision, reason="等待本局官方价格精度")
        rate, exponent = info.get("fee_rate"), info.get("fee_exponent")
        if not (_finite(rate) and 0 <= rate <= 1 and _finite(exponent) and 1 <= exponent <= 4):
            return replace(decision, reason="等待本局官方费用")
        tick = float(info["tick_size"])
        ceiling = min(MAX_ENTRY_PRICE, ask + tick)
        limit = round(math.floor(ceiling / tick + 1e-9) * tick, 6)
        cost = limit + fee_amount(1, limit, rate, exponent)
        decision = replace(decision, limit=limit, all_in_cost=cost)
        if self.max_order_limit is not None and limit > self.max_order_limit:
            return replace(decision, reason="原买入限价超过 60 美分，跳过本次机会")
        if decision.score - cost < ENTRY_EDGE - 1e-9:
            return replace(decision, reason="方向有倾向，但买入限价加手续费后优势不足")
        direction = "涨" if decision.side == "Up" else "跌"
        return replace(decision, action="BUY", reason=f"预测偏{direction}，限价扣费后仍有买入空间")

    @staticmethod
    def _position_decision(decision, net_bid, best_bid):
        if not _finite(net_bid) or not 0 < net_bid <= best_bid + 1e-6:
            return replace(decision, reason="缺少可成交的净卖价，继续观察持仓")
        decision = replace(decision, net_bid=net_bid)
        if decision.score <= REVERSAL_SCORE + 1e-9:
            return replace(decision, action="SELL", reason="预测已明显转向另一边，尝试卖出")
        floor = decision.score + EXIT_PREMIUM
        if net_bid >= floor - 1e-9:
            return replace(decision, action="SELL", min_net_bid=floor,
                           reason="现在卖出的净价高于预计结算价值，尝试卖出")
        return replace(decision, reason="继续持有，当前净卖价尚未优于预计结算价值")

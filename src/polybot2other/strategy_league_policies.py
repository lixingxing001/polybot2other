"""Fixed, prospective BTC five-minute Paper hypotheses for a shared-input experiment.

Depth hypotheses are inspired by Gould & Bonart (arXiv:1512.03492) and Cont et al.
(arXiv:1011.6402). Those papers study equities. Their results do not calibrate a
Polymarket terminal score. The league measures its own executable outcomes.
"""
from dataclasses import dataclass
import math

from .forecast_paper import _fresh_reference
from .managed_live_client import fee_amount
from .optimized_reference import SPOT, TWAP
from .shared_market_inputs import quote_error


CATALOG_VERSION = "btc5m-league-v1"
CONTROL_KEY = "control_060"
SIDES = ("Up", "Down")
EPS = 1e-9
BASE_EXIT = "评分降至 0.42 尝试退出，或扣费净卖价高出持仓评分 2 美分时卖出。"


@dataclass(frozen=True)
class SideQuote:
    bid: float
    ask: float
    limit: float
    cost: float
    bid_depth: float
    ask_depth: float

    @property
    def mid(self):
        return (self.ask + self.bid) / 2

    @property
    def spread(self):
        return self.ask - self.bid

    @property
    def pressure(self):
        return self.bid_depth / (self.bid_depth + self.ask_depth)


@dataclass(frozen=True)
class LeagueSample:
    at: float
    round_id: str
    left: float
    up_score: float
    spot: float
    target: float
    projected: float
    remaining_to_tie: float | None
    up: SideQuote
    down: SideQuote

    def book(self, side):
        return self.up if side == "Up" else self.down

    def score(self, side):
        return self.up_score if side == "Up" else 1 - self.up_score


@dataclass(frozen=True)
class LeagueDecision:
    action: str
    reason: str
    side: str | None = None
    limit: float | None = None
    min_net_bid: float | None = None


def _finite(v):
    return isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)


def _side_quote(q, tick, rate, exponent):
    bid, ask = q["best_bid"], q["best_ask"]
    # Restrict imbalance to posted shares within two cents of the touch.
    depth = []
    for key, top in (("bids", bid), ("asks", ask)):
        levels = q.get(key) or ()
        if not levels:
            return None
        valid = [(x["price"], x["size"]) for x in levels if isinstance(x,dict)
                 and _finite(x.get("price")) and 0 < x["price"] < 1
                 and _finite(x.get("size")) and x["size"] > 0]
        if not valid:
            return None
        best = (max if key == "bids" else min)(p for p, _ in valid)
        if abs(best - top) > EPS:
            return None
        depth.append(sum(size for price, size in valid if abs(price - top) <= .02 + EPS))
    limit = round(math.floor(min(.80, ask + tick) / tick + EPS) * tick, 6)
    return SideQuote(bid, ask, limit, limit + fee_amount(1, limit, rate, exponent), *depth)


def sample_frame(frame, forecast_decision, info):
    """Normalize one causal shared forecast and both matching token books once per tick."""
    market, at = frame.market, frame.observed_at
    if (frame.error or not market or not market.started_at <= at < market.ends_at
            or not frame.reference.get("ready") or not frame.reference.get("protocol_verified")
            or not _finite(market.target_price) or market.target_price <= 0
            or not forecast_decision or forecast_decision.round_id != market.round_id
            or forecast_decision.snapshot_id != frame.snapshot_id or forecast_decision.decision_at != at):
        return None
    latest = frame.reference.get("latest", {})
    up_score, projected = forecast_decision.up_score, forecast_decision.projected_final_twap
    if (not all(_fresh_reference(latest, channel, at) for channel in (SPOT, TWAP))
            or not _finite(up_score) or not 0 <= up_score <= 1 or not _finite(projected) or projected <= 0
            or any(quote_error(market, frame.quotes, side, at) for side in SIDES)
            or not info or str(info.get("tick_size")) not in {"0.1", "0.01", "0.001", "0.0001"}):
        return None
    tick = float(info["tick_size"])
    rate, exponent = info.get("fee_rate"), info.get("fee_exponent")
    if not (_finite(rate) and 0 <= rate <= 1 and _finite(exponent) and 1 <= exponent <= 4):
        return None
    books = tuple(_side_quote(frame.quotes[side], tick, rate, exponent) for side in SIDES)
    if None in books:
        return None
    tie = forecast_decision.remaining_average_to_tie
    return LeagueSample(at, market.round_id, market.ends_at - at, up_score, latest[SPOT]["price"],
                        market.target_price, projected, tie if _finite(tie) and tie > 0 else None, *books)


@dataclass(frozen=True)
class LeaguePolicy:
    key: str
    name: str
    family: str
    trigger: str
    weakness: str
    cap: float = .60
    min_score: float = .62
    min_edge: float = .04
    window: tuple[int, int] = (20, 180)
    exit_rule: str = BASE_EXIT

    @property
    def is_control(self):
        return self.key == CONTROL_KEY

    @property
    def description(self):
        return (f"{self.trigger} 距收盘 {self.window[0]}–{self.window[1]} 秒，原买入限价最多 {self.cap:.2f}，"
                f"方向评分至少 {self.min_score:.2f}，扣除买入费用后保留 {self.min_edge:.2f} 分值余量，价差最多 3 美分。")


VALUE, TREND, REVERSE, DEPTH, CLOSE, EXIT = ("价格折让", "现货趋势", "反转与回归", "盘口供需", "临近结算", "卖出时机")
P = LeaguePolicy
POLICIES = (
    P(CONTROL_KEY, "60 美分基准", "共同对照", "跟随标准方向预测。", "方向评分尚未校准，低价仍可跌到零。"),
    P("value_half", "半价以内买方向", VALUE, "只接模型偏好且最高愿付不超过半价的一边。", "便宜合约可能反映真实的低胜率。", cap=.50),
    P("value_dispute", "市场不看好时买入", VALUE, "模型偏好一侧的盘口中间价须在 0.45 以下。", "盘口可能比结算模型更快反映反转。", cap=.52, min_score=.65),
    P("value_persist", "折价持续再买", VALUE, "现在与约 12 秒前同一侧的评分都至少 0.62，且都比含费成本高 0.06。", "等待确认可能错过短暂报价。", min_edge=.06),
    P("value_catchup", "模型先动、报价未跟", VALUE, "约 12 秒内同侧评分上升至少 0.06，盘口中间价最多上涨 1 美分。", "模型短时变动可能只是估计噪声。"),
    P("trend_twoleg", "现货两段同向", TREND, "近 10 秒、此前约 20 秒的现货各沿同一方向移动至少 1 个基点，跟随现货。", "短趋势可能在进入收盘均价前反转。", cap=.66, min_score=.54),
    P("trend_accel", "现货短线加速", TREND, "近 10 秒现货变动至少 2 个基点，且同向速度至少是前约 20 秒的两倍。", "急动后盘口可能很快补价。", cap=.65, min_score=.52),
    P("trend_break", "突破半分钟区间", TREND, "现货超出过去 30 秒采样高低区间至少 0.5 个基点。", "窄区间的假突破容易反手。", cap=.64, min_score=.53),
    P("trend_cross", "穿过结算目标再跟", TREND, "约 10 秒前现货在目标另一侧，当前在突破方向一侧至少 1 个基点。", "价格可能在目标两边来回穿越。", cap=.63, min_score=.52),
    P("rev_book", "盘口急跌、模型未跌", REVERSE, "同一侧 12 秒内中间价下跌至少 4 美分，模型同侧评分回落不超过 0.02。", "交易者可能提前获得了模型未捕捉的信息。", cap=.56, min_score=.53),
    P("rev_spot", "急动后回头", REVERSE, "前约 20 秒现货移动至少 2 个基点，最近 10 秒反向收回先前幅度的一半。", "暂时反弹可能很快结束。", cap=.58, min_score=.52),
    P("rev_return", "从目标外向回走", REVERSE, "现货仍偏离目标至少 1 个基点，最近 12 秒已向目标收回至少 1 个基点，买回归方向。", "回到目标不代表最后会穿过目标。", cap=.52, min_score=.48),
    P("rev_model", "模型确认翻向", REVERSE, "约 12 秒前持另一侧判断（本侧评分至多 0.45），当前本侧评分已升至 0.62。", "翻向模型在震荡段可能反复出错。"),
    P("book_backing", "双边挂量确认", DEPTH, "选择买方挂量占比更高的一侧，它至少占 65%，另一侧至多占 45%。", "显示出来的挂量可以随时撤走。", cap=.63, min_score=.52),
    P("book_refill", "同价买墙补单", DEPTH, "同侧买一价保持 10 秒前水平，近档买量增加至少一半，买量占比达到 60%。", "补量可能未必会转化为主动买入。", cap=.62, min_score=.54),
    P("book_thin", "卖盘变薄且买价抬高", DEPTH, "同侧较 10 秒前近档卖量至少减半，买一价抬高至少 1 美分。", "新卖单可能在入场前补回。", cap=.63, min_score=.52),
    P("book_tight", "宽价差收窄后进", DEPTH, "同侧 12 秒前价差至少 4 美分，目前至多 2 美分，卖一至少便宜 1 美分。", "成交机会可能只存在于一次盘口更新。", cap=.60, min_score=.58),
    P("close_cushion", "尾盘现货留出距离", CLOSE, "现货与预测收盘均价同在目标一侧，两者分别离目标至少 2 和 1 个基点。", "最后数秒的大波动可能吃掉缓冲。", cap=.72, min_score=.62, window=(20,60)),
    P("close_hurdle", "翻盘所需均价已远", CLOSE, "按已积累的末分钟均价估算，要让模型偏好的方向打平，余时均价须逆向偏离现货至少 4 个基点。", "行情跳变仍可能跨过这个距离。", cap=.75, min_score=.62, window=(15,55)),
    P("close_diverge", "现货逆向、均价仍领先", CLOSE, "现货已在目标另一侧，但预测收盘均价在所买一侧仍领先至少 1 个基点。", "逆向现货可能维持到足以改变结算。", cap=.66, min_score=.58, window=(15,55)),
    P("close_quiet", "平静尾盘跟均价", CLOSE, "近 30 秒现货最高最低相差至多 3 个基点，预测收盘均价在所买一侧领先至少 1 个基点。", "安静时段的末秒跳价仍会造成亏损。", cap=.69, min_score=.60, window=(18,65)),
    P("exit_scratch", "跌破五五开就走", EXIT, "入场与共同对照相同。", "中性附近的噪声可能过早洗出持仓。",
      exit_rule="持仓评分降至 0.50 就卖，保留基准的溢价退出。"),
    P("exit_cash", "净赚八美分先落袋", EXIT, "入场与共同对照相同。", "截断大赢单会压低平均盈利。",
      exit_rule="保留基准退出；扣去卖费的净价高于每份含费入场成本 8 美分也卖。"),
    P("exit_confirm", "反转两次才走", EXIT, "入场与共同对照相同。", "延后撤退会放大急跌损失。",
      exit_rule="当前和约 10 秒前的持仓评分都低至 0.42 才按反转卖出；保留基准溢价退出。"),
    P("exit_settle", "拿到官方结算", EXIT, "入场与共同对照相同。", "持有错误方向可能损失全部投入。",
      exit_rule="整笔持有到官方结果。"),
)


def _bps(new, old):
    return (new - old) / old * 10_000


def _window(s, history, seconds):
    """Require continuous, distinct, same-round observations with a recent left endpoint."""
    result = []
    for h in reversed(history):
        if h.round_id != s.round_id or h.at >= s.at:
            continue
        next_at = result[-1].at if result else s.at
        if h.at >= next_at:
            continue
        if next_at - h.at > 5.0 or s.at - h.at > seconds + 4:
            break
        result.append(h)
        if s.at - h.at >= seconds:
            return result[::-1]
    return []


def _direction(v):
    return "Up" if v > 0 else "Down" if v < 0 else None


def _signal(key, s, history):
    model = _direction(s.up_score - .5)
    if key.startswith("exit_") or key in (CONTROL_KEY, "value_half"):
        return model
    if key == "value_dispute":
        return model if model and s.book(model).mid <= .45 + EPS else None
    side = _direction(s.spot - s.target)
    projected = _direction(s.projected - s.target)
    if key == "close_cushion":
        if projected == side and abs(_bps(s.spot,s.target)) >= 2 and abs(_bps(s.projected,s.target)) >= 1:
            return side
        return None
    if key == "close_hurdle":
        if s.remaining_to_tie is None:
            return None
        hurdle = _bps(s.spot, s.remaining_to_tie)
        return model if model == _direction(hurdle) and abs(hurdle) >= 4 else None
    if key == "close_diverge":
        return projected if side and projected != side and abs(_bps(s.projected,s.target)) >= 1 else None
    if key == "book_backing":
        pick = _direction(s.up.pressure - s.down.pressure)
        other = s.down if pick == "Up" else s.up
        return pick if pick and s.book(pick).pressure >= .65 and other.pressure <= .45 else None

    if key in {"value_persist","value_catchup","rev_book","rev_model","rev_return","book_tight"}:
        window = _window(s,history,12)
        if not window:
            return None
        prior = window[0]
        if key == "rev_return":
            change = _bps(s.spot,prior.spot)
            return _direction(change) if abs(change) >= 1 and abs(_bps(s.spot,s.target)) >= 1 and _direction(change) != side else None
        choices = []
        for d in SIDES:
            old, new = prior.book(d), s.book(d)
            score, old_score = s.score(d), prior.score(d)
            condition = {
                "value_persist": old_score >= .62 and old_score-old.cost >= .06-EPS and old.limit <= .60+EPS and old.spread <= .03+EPS,
                "value_catchup": score-old_score >= .06-EPS and new.mid-old.mid <= .01+EPS,
                "rev_book": old.mid-new.mid >= .04-EPS and score-old_score >= -.02-EPS,
                "rev_model": old_score <= .45+EPS and score >= .62-EPS,
                "book_tight": old.spread >= .04-EPS and new.spread <= .02+EPS and old.ask-new.ask >= .01-EPS,
            }[key]
            if condition:
                choices.append(d)
        return max(choices,key=s.score,default=None)

    short = _window(s, history, 10)
    if not short:
        return None
    old = short[0]
    fast = _bps(s.spot, old.spot)
    if key == "trend_cross":
        return side if side and side != _direction(old.spot-s.target) and abs(_bps(s.spot,s.target)) >= 1 else None
    if key in ("book_refill", "book_thin"):
        candidates = []
        for d in SIDES:
            a,b = old.book(d),s.book(d)
            if key == "book_refill":
                ok = abs(a.bid-b.bid) <= EPS and b.bid_depth >= 1.5*a.bid_depth and b.pressure >= .60
            else:
                ok = b.ask_depth <= .5*a.ask_depth and b.bid >= a.bid+.01-EPS
            if ok:
                candidates.append(d)
        return max(candidates,key=s.score,default=None)
    long = _window(s, history, 30)
    if not long:
        return None
    slow = _bps(old.spot, long[0].spot)
    if key == "trend_twoleg":
        return _direction(fast) if fast*slow>0 and min(abs(fast), abs(slow)) >= 1 else None
    if key == "trend_accel":
        v1,v2 = fast/(s.at-old.at), slow/(old.at-long[0].at)
        return _direction(fast) if fast*slow>0 and abs(fast)>=2 and abs(v1)>=2*abs(v2) else None
    if key == "rev_spot":
        return _direction(fast) if fast*slow<0 and abs(slow)>=2 and abs(fast)>=.5*abs(slow) else None
    prices = [h.spot for h in long]
    if key == "trend_break":
        if _bps(s.spot,max(prices)) >= .5:
            return "Up"
        if _bps(s.spot,min(prices)) <= -.5:
            return "Down"
    if key == "close_quiet" and _bps(max(*prices,s.spot),min(*prices,s.spot)) <= 3 and abs(_bps(s.projected,s.target)) >= 1:
        return projected
    return None


def entry_decision(policy, sample, history):
    if sample is None:
        return LeagueDecision("WAIT", "等同局参考价和双边有效深度")
    lo, hi = policy.window
    if not lo <= sample.left <= hi:
        return LeagueDecision("WAIT", f"等待距收盘 {lo}–{hi} 秒")
    side = _signal(policy.key, sample, history)
    if side is None:
        return LeagueDecision("WAIT", policy.trigger)
    q = sample.book(side)
    if q.ask > q.limit + EPS or q.limit > policy.cap + EPS:
        return LeagueDecision("WAIT", f"原限价超过 {policy.cap:.2f}", side)
    if q.spread > .03 + EPS:
        return LeagueDecision("WAIT", "买卖价差大于 3 美分", side)
    if sample.score(side) < policy.min_score - EPS:
        return LeagueDecision("WAIT", "方向评分不足", side)
    if sample.score(side) - q.cost < policy.min_edge - EPS:
        return LeagueDecision("WAIT", "计入买入手续费后余量不足", side)
    return LeagueDecision("BUY", policy.name, side, q.limit)


def exit_decision(policy, sample, history, side, net_bid, entry_cost=None):
    if (sample is None or side not in SIDES or sample.left <= 2 or policy.key == "exit_settle"):
        return LeagueDecision("HOLD", "继续等待有效行情或官方结算", side)
    if not _finite(net_bid) or not 0 < net_bid <= sample.book(side).bid + EPS:
        return LeagueDecision("HOLD", "等可成交的净卖价", side)
    score = sample.score(side)
    threshold = .50 if policy.key == "exit_scratch" else .42
    reversal = score <= threshold + EPS
    if reversal and policy.key == "exit_confirm":
        window = _window(sample,history,10)
        reversal = bool(window and window[0].score(side) <= threshold + EPS)
    if reversal:
        return LeagueDecision("SELL", "持仓方向评分转弱", side)
    targets = [(score + .02, "扣费卖价已高于模型估值")]
    if policy.key == "exit_cash" and _finite(entry_cost) and entry_cost > 0:
        targets.append((entry_cost + .08, "已达到每份 8 美分净收益目标"))
    floor, reason = min(targets)
    if net_bid + EPS >= floor:
        return LeagueDecision("SELL", reason, side, min_net_bid=floor)
    return LeagueDecision("HOLD", "继续持有", side)

"""Paper-only exit policy. Thresholds are experimental, not fitted performance claims."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math


@dataclass(frozen=True)
class ExitPolicy:
    version: str = "main-exit-20261030-v1"
    take_profit: float = .20
    stop_loss: float = .40
    fee_rate: float = .07
    depth_fraction: float = .50
    price_haircut: float = .01
    limit_slippage: float = .02
    latency_seconds: float = .50
    quote_source_max_ms: float = 3000
    quote_receive_max_ms: float = 2500
    mirror_max_delay_seconds: float = 10
    book_depth: int = 20

    def payload(self):
        return asdict(self)


def checked_book(quote, token, now, policy):
    """Reject another token, stale/future clocks, crossed or inconsistent depth."""
    if str(quote.get("token_id", "")) != str(token):
        return None, "等待本期本方向盘口"
    source, received = quote.get("source_ms"), quote.get("received_ms")
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (source, received)):
        return None, "盘口时间缺失"
    if not 0 <= now * 1000 - source <= policy.quote_source_max_ms or not 0 <= now * 1000 - received <= policy.quote_receive_max_ms:
        return None, "盘口过期或时间异常"
    bid, ask = quote.get("best_bid"), quote.get("best_ask")
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (bid, ask)) or not 0 < bid <= ask <= 1:
        return None, "买卖盘口缺失或交叉"
    levels = quote.get("bids") or []
    if not levels:
        return None, "没有可卖出的买盘"
    clean = []
    for level in levels:
        price, size = level.get("price"), level.get("size")
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (price, size)) or not 0 < price < 1 or size <= 0:
            return None, "买盘深度无效"
        clean.append({"price": price, "size": size})
    clean.sort(key=lambda x: -x["price"])
    if abs(clean[0]["price"] - bid) > 1e-6 or len({x["price"] for x in clean}) != len(clean):
        return None, "买一与完整深度不一致"
    clean = clean[:policy.book_depth]
    fingerprint = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
    return {"levels": clean, "best_bid": bid, "source_ms": source, "received_ms": received,
            "fingerprint": fingerprint, "token_id": str(token)}, None


def sweep_sell(book, shares, policy, limit=0.0):
    """Walk half of visible depth, with an explicit one-cent adverse price haircut."""
    remaining = shares
    fills = []
    for level in book["levels"]:
        price = round(level["price"] - policy.price_haircut, 4)
        if price <= 0 or price + 1e-9 < limit:
            continue
        quantity = min(remaining, level["size"] * policy.depth_fraction)
        quantity = math.floor((quantity + 1e-9) * 1_000_000) / 1_000_000
        if quantity <= 0:
            continue
        gross = quantity * price
        fee = quantity * policy.fee_rate * price * (1 - price)
        fills.append({"price": price, "shares": quantity, "gross": gross, "fee": fee})
        remaining -= quantity
        if remaining < .000001:
            break
    quantity = round(sum(x["shares"] for x in fills), 6)
    gross = round(sum(x["gross"] for x in fills), 6)
    fee = round(sum(x["fee"] for x in fills), 6)
    return {"shares": quantity, "gross": gross, "fee": fee, "net": round(gross - fee, 6),
            "avg_price": gross / quantity if quantity else None, "levels": fills,
            "full": quantity >= shares - .000001}


def exit_signal(sweep, stake, shares, policy):
    if sweep["shares"] <= 0 or shares <= 0 or stake <= 0:
        return None, None
    basis = stake * sweep["shares"] / shares
    roi = sweep["net"] / basis - 1
    reason = "TP" if roi >= policy.take_profit else "SL" if roi <= -policy.stop_loss else None
    return reason, roi

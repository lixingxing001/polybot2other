"""Replay archived order attempts at their original submission times. Python 3.9+."""
import gzip
import json
from collections import Counter
from pathlib import Path
from random import Random
from statistics import mean

ROOT = Path(__file__).resolve().parent
CUTOFF = 1795358400
COMMON = 1795059000
SPLIT = 1795190400
BUDGET = 5
WINDOWS = {"full": (0, CUTOFF), "common": (COMMON, CUTOFF),
           "early": (0, SPLIT), "late": (SPLIT, CUTOFF),
           "last36h": (CUTOFF - 36 * 3600, CUTOFF), "last24h": (CUTOFF - 86400, CUTOFF)}


def outcome(event, extra_cost):
    # Stress the effective acquisition cost per share. Never assume extra depth.
    price = event["cost"] / event["shares"] + extra_cost
    shares = min(event["shares"], BUDGET / price)
    return shares * (event["payout"] / event["shares"] - price)


def earnings_router(events):
    # Freeze the route before the market opens. Only earlier completed markets
    # are evidence. Repeated entries from the same source/market are counted once.
    first_fills = {}
    for event in events:
        if "close" in event:
            first_fills.setdefault((event["leg"], event["round"]), event)
    samples = list(first_fills.values())
    routes = {}
    for market in sorted({event["round"] for event in events}):
        strengths = {}
        for source in ("main", "settle"):
            recent = [row for row in samples if row["leg"] == source and
                      market - 72 * 3600 <= row["close"] < market]
            day = [row for row in recent if row["close"] >= market - 24 * 3600]
            if len(recent) >= 24 and len(day) >= 12:
                strengths[source] = min(mean(outcome(row, 0) for row in recent),
                                        mean(outcome(row, 0) for row in day))
        allowed = [key for key in strengths if strengths[key] > 0]
        routes[market] = max(allowed, key=strengths.get) if allowed else None
    return routes


def eligible(event, model, routes, margin):
    source, market = event["leg"], event["round"]
    if model == "past_winners":
        return source == routes.get(market)
    if source == "settle":
        return model == "settle" or (model in ("quiet_settle", "combined") and event["quiet"])
    if model in ("raw_main", "first_main"):
        return True
    if model == "lower_only":
        return event["score"] >= 0.65 + margin
    if model in ("middle", "combined"):
        return 0.65 + margin <= event["score"] < 0.75 - margin
    return False


def evaluate(events, model, routes, extra_cost=0, margin=0):
    # Pending attempts reserve a slot and budget. A failed buy releases both.
    # In first-fill models an accepted fill permanently locks this market.
    active, deals, locked = [], [], set()
    cash, cash_skips = 100., 0
    for event in events:
        if not eligible(event, model, routes, margin):
            continue
        now = event["submit"]
        cash += sum(slot[2] for slot in active if slot[0] < now)
        active = [slot for slot in active if slot[0] >= now]
        if model != "raw_main" and (event["round"] in locked or
                                    any(slot[1] == event["round"] for slot in active)):
            continue
        if len(active) >= 2:
            continue
        if cash < BUDGET - 1e-8:
            cash_skips += 1
            continue
        cash -= BUDGET
        if "cost" not in event:
            active.append((event["release"], event["round"], BUDGET))
            continue
        pnl = outcome(event, extra_cost)
        # Keeping unused partial budget reserved through close is conservative.
        active.append((event["close"], event["round"], BUDGET + pnl))
        locked.add(event["round"])
        deals.append((event["submit"], event["close"], pnl, event["leg"]))
    return deals, cash_skips


def summarize(deals, start, end):
    # Only entries opened AND closed within the reporting interval.
    closed = sorted((row for row in deals if start <= row[0] and row[1] < end),
                    key=lambda row: row[1])
    value = peak = drawdown = 0.
    for _, _, pnl, _ in closed:
        value += pnl
        peak = max(peak, value)
        drawdown = max(drawdown, peak - value)
    return {"trades": len(closed), "wins": sum(row[2] > 0 for row in closed),
            "pnl": round(value, 2), "drawdown": round(drawdown, 2),
            "legs": dict(Counter(row[3] for row in closed))}


def paired_blocks(reference, chosen, start, end):
    """Resample whole 12h clock blocks together, including inactive blocks."""
    block = 12 * 3600
    lo, hi = int(start // block), int(end // block)
    diffs = [0.] * (hi - lo + 1)
    for trades, sign in ((reference, -1), (chosen, 1)):
        for _, close, amount, _ in trades:
            diffs[int(close // block) - lo] += sign * amount
    rng = Random(1729)
    sums = sorted(sum(rng.choices(diffs, k=len(diffs))) for _ in range(5000))
    return {"n": len(diffs), "positive": sum(v > 1e-6 for v in diffs),
            "total_diff": round(sum(diffs), 2),
            "descriptive_95": [round(sums[124], 2), round(sums[4874], 2)]}


def main():
    with gzip.open(ROOT / "回放输入.json.gz", "rt", encoding="utf8") as f:
        data = json.load(f)
    report = {}
    for ledger, events in data.items():
        events.sort(key=lambda event: (event["submit"], event["leg"]))
        routes = earnings_router(events) if ledger == "paper" else {}
        models = ["raw_main", "first_main", "lower_only", "middle"]
        if ledger == "paper":
            models += ["settle", "quiet_settle", "combined", "past_winners"]
        report[ledger] = {}
        reference, _ = evaluate(events, "raw_main", routes)
        start = events[0]["submit"]
        end = max(row["close"] for row in events if "close" in row)
        for model in models:
            cases = {}
            for cost, margin, name in [(0, 0, "base"), (.01, 0, "plus1c"), (.02, 0, "plus2c"),
                                       (0, .00555, "definite"), (0, -.00555, "possible")]:
                if margin and (model not in ("middle", "lower_only") or "live" not in ledger):
                    continue
                deals, cash_skips = evaluate(events, model, routes, cost, margin)
                cases[name] = {key: summarize(deals, *limits) for key, limits in WINDOWS.items()}
                cases[name]["cash_skips"] = cash_skips
                if name == "base" and model != "raw_main":
                    cases[name]["paired_vs_raw"] = paired_blocks(reference, deals, start, end)
            report[ledger][model] = cases
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

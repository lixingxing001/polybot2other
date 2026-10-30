"""Read-only, exploratory exit study. Run against the frozen ledger beside this file.

This is an exit overlay on recorded main-account entries, not an account backtest.
Missing quotes are not reconstructed. Legacy replay files were saved for losing
legacy strategies; their market selection is biased. No strategy settings change.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import hashlib
import json
import math
import sqlite3
import statistics
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent.parent
REPLAYS = ROOT / "data/backups/strategy-retirement-20261026-225605/retired-runtime/strategy-experiments/loss-replays"
TZ = ZoneInfo("Asia/Shanghai")
FEE_RATE = 0.07  # Same paper taker model as execution.py; not an historical fee audit.


def local_time(value):
    return dt.datetime.fromtimestamp(value, TZ).isoformat()


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sell(shares, levels, fraction=1.0, slip=0.0):
    """Full-position bid-depth sweep; reject insufficient depth, never invent a fill."""
    remaining = shares
    gross = fees = 0.0
    used = []
    for level in levels:
        quantity = min(remaining, level["size"] * fraction)
        price = max(0.0, level["price"] - slip)
        if price <= 0 or quantity <= 0:
            continue
        fee = quantity * FEE_RATE * price * (1 - price)
        gross += quantity * price
        fees += fee
        remaining -= quantity
        used.append({"price": price, "shares": quantity, "fee": fee})
        if remaining <= 1e-7:
            break
    if remaining > 1e-7:
        return None
    assert abs(sum(x["shares"] for x in used) - shares) < 1e-6
    return {"gross": gross, "fee": fees, "net": gross - fees, "levels": used}


def valid_quote(quote, at):
    bid, ask, updated = (quote.get(k) for k in ("best_bid", "best_ask", "updated_ms"))
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (bid, ask, updated)):
        return None, "missing_top_or_time"
    if not 0 < bid <= ask < 1:
        return None, "invalid_top"
    age = at * 1000 - updated
    if not 0 <= age <= 3000:
        return None, "recorded_quote_age"
    levels = quote.get("bids") or []
    if not levels or any(not (0 < v.get("price", 0) < 1 and v.get("size", 0) > 0) for v in levels):
        return None, "invalid_depth"
    levels = sorted(levels, key=lambda x: -x["price"])
    if abs(levels[0]["price"] - bid) > 1e-6:
        return None, "top_depth_mismatch"
    if len({x["price"] for x in levels}) != len(levels):
        return None, "duplicate_level"
    return {"bid": bid, "ask": ask, "age_ms": age, "levels": levels, "updated_ms": updated}, None


def replay_exit(row, take_profit, stop_loss, stress):
    """Chronological first trigger. Never use a later peak to choose an exit."""
    marks = row["marks"]
    rejected = 0
    i = 0
    while i < len(marks):
        mark = marks[i]
        i += 1
        quote = sell(row["shares"], mark["levels"], .5 if stress else 1.0, .01 if stress else 0.0)
        if quote is None:
            continue
        roi = (quote["net"] / row["stake"] - 1) * 100
        reason = "TP" if take_profit is not None and roi >= take_profit else "SL" if stop_loss is not None and roi <= -stop_loss else None
        if reason is None:
            continue
        fill_mark, fill = mark, quote
        if stress:
            # Sparse historical samples cannot reconstruct the book exactly at +500ms.
            # Use the next valid observation >=500ms later, within 10s; report its delay.
            candidate_index = next((j for j in range(i, len(marks)) if marks[j]["at"] >= mark["at"] + .5), None)
            if candidate_index is None:
                rejected += 1
                break
            candidate = marks[candidate_index]
            if candidate["at"] - mark["at"] > 10:
                rejected += 1
                i = candidate_index
                continue
            fill_mark = candidate
            fill = sell(row["shares"], candidate["levels"], .5, .01)
            if fill is None:
                rejected += 1
                i = candidate_index + 1
                continue
        assert row["opened_at"] < fill_mark["at"] < row["ends_at"]
        assert not stress or fill_mark["at"] - mark["at"] >= .5
        return {"trade_id": row["id"], "pnl": fill["net"] - row["stake"], "reason": reason,
                "trigger_at": mark["at"], "exit_at": fill_mark["at"],
                "delay_seconds": fill_mark["at"] - mark["at"], "fee": fill["fee"],
                "baseline_pnl": row["pnl"], "unfilled_attempts": rejected}
    return {"trade_id": row["id"], "pnl": row["pnl"], "reason": "SETTLEMENT",
            "baseline_pnl": row["pnl"], "unfilled_attempts": rejected}


def metrics(rows, exits):
    baseline = sum(r["pnl"] for r in rows)
    result = sum(e["pnl"] for e in exits)
    gains = sum(e["pnl"] - e["baseline_pnl"] for e in exits if e["baseline_pnl"] < 0)
    winner_cost = sum(e["baseline_pnl"] - e["pnl"] for e in exits if e["baseline_pnl"] > 0)
    assert abs((result - baseline) - (gains - winner_cost)) < .00001
    delays = [e["delay_seconds"] for e in exits if "delay_seconds" in e]
    return {"trades": len(rows), "baseline_pnl": baseline, "scenario_pnl": result,
            "difference": result - baseline, "benefit_on_original_losers": gains,
            "profit_given_up_on_original_winners": winner_cost,
            "tp_exits": sum(e["reason"] == "TP" for e in exits),
            "sl_exits": sum(e["reason"] == "SL" for e in exits),
            "hold_to_settlement": sum(e["reason"] == "SETTLEMENT" for e in exits),
            "losers_to_profit": sum(e["baseline_pnl"] < 0 < e["pnl"] for e in exits),
            "winners_to_loss": sum(e["baseline_pnl"] > 0 > e["pnl"] for e in exits),
            "exit_fees": sum(e.get("fee", 0) for e in exits),
            "median_delay_seconds": statistics.median(delays) if delays else None,
            "max_delay_seconds": max(delays) if delays else None,
            "unfilled_attempts": sum(e["unfilled_attempts"] for e in exits)}


def main():
    db = sqlite3.connect(f"file:{OUT / 'main-account-snapshot.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    all_trades = [dict(x) for x in db.execute("SELECT t.*,m.started_at,m.ends_at FROM trades t LEFT JOIN market_rounds m USING(round_id) ORDER BY t.opened_at,t.id")]
    settled = [x for x in all_trades if x["status"] == "SETTLED"]
    orders = {x["trade_id"]: dict(x) for x in db.execute("SELECT trade_id, SUM(cash_spent) cost, SUM(fee) fee, SUM(filled_shares) shares FROM paper_orders WHERE trade_id IS NOT NULL GROUP BY trade_id")}
    samples = collections.defaultdict(list)
    source_files = []
    packet_count = 0
    for path in sorted(REPLAYS.glob("*.jsonl")):
        data = path.read_bytes()
        source_files.append({"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        for line_no, line in enumerate(data.splitlines(), 1):
            packet = json.loads(line)
            packet_count += 1
            for item in packet["samples"]:
                samples[packet["round"]["round_id"]].append({**item, "source_file": path.name, "source_line": line_no})
    paths, excluded = [], []
    invalid = collections.Counter()
    for trade in settled:
        if trade["round_id"] not in samples:
            continue
        row = dict(trade)
        marks = []
        seen = set()
        raw = 0
        for sample in sorted(samples[trade["round_id"]], key=lambda p: (p["at"], p["source_file"], p["source_line"])):
            at = sample["at"]
            if not trade["opened_at"] + .25 <= at < trade["ends_at"]:
                continue
            quote = sample["quotes"].get(trade["side"], {})
            identity = (at, json.dumps(quote, sort_keys=True))
            if identity in seen:
                continue
            seen.add(identity)
            raw += 1
            q, error = valid_quote(quote, at)
            if error:
                invalid[error] += 1
                continue
            sweep = sell(trade["shares"], q["levels"])
            if sweep is None:
                invalid["insufficient_depth"] += 1
                continue
            marks.append({"at": at, **q, "pnl": sweep["net"] - trade["stake"],
                          "roi_pct": (sweep["net"] / trade["stake"] - 1) * 100,
                          "exit_fee": sweep["fee"], "source_file": sample["source_file"], "source_line": sample["source_line"]})
        if not marks:
            excluded.append({"trade_id": trade["id"], "reason": "no_valid_post_entry_before_expiry_depth", "raw_samples": raw})
            continue
        order = orders.get(trade["id"])
        assert order is not None and abs(order["cost"] - trade["stake"]) < .00001
        assert abs(order["shares"] - trade["shares"]) < .00001
        row["entry_fee"] = order["fee"]
        row["raw_post_entry_samples"] = raw
        row["marks"] = marks
        row["mfe"] = max(marks, key=lambda p: p["roi_pct"])
        row["mae"] = min(marks, key=lambda p: p["roi_pct"])
        times = [row["opened_at"], *[m["at"] for m in marks], row["ends_at"]]
        row["max_gap_seconds"] = max(b - a for a, b in zip(times, times[1:]))
        row["first_gap_seconds"] = marks[0]["at"] - row["opened_at"]
        row["last_gap_seconds"] = row["ends_at"] - marks[-1]["at"]
        row["denser_path"] = len(marks) >= 10 and row["first_gap_seconds"] <= 15 and row["last_gap_seconds"] <= 15 and row["max_gap_seconds"] <= 20
        paths.append(row)
    losses = [x for x in paths if x["pnl"] < 0]
    winners = [x for x in paths if x["pnl"] > 0]
    cohorts = {"all_observed": paths, "denser_paths": [x for x in paths if x["denser_path"]]}
    rounds = sorted({x["round_id"] for x in paths})
    early_rounds = set(rounds[:len(rounds) // 2])
    cohorts["earlier_observed_rounds"] = [x for x in paths if x["round_id"] in early_rounds]
    cohorts["later_observed_rounds"] = [x for x in paths if x["round_id"] not in early_rounds]
    scenarios = []
    candidates = [(tp, None) for tp in (10, 20, 30, 40, 50)] + [(None, sl) for sl in (20, 40, 60)] + [(tp, sl) for tp in (20, 30) for sl in (20, 40, 60)]
    for tp, sl in candidates:
        for stress in (False, True):
            exits = [replay_exit(row, tp, sl, stress) for row in paths]
            by_id = {x["trade_id"]: x for x in exits}
            scenarios.append({"take_profit_pct": tp, "stop_loss_pct": sl,
                              "execution_assumption": "next_observation_stress" if stress else "same_snapshot",
                              "cohorts": {name: metrics(rs, [by_id[r["id"]] for r in rs]) for name, rs in cohorts.items()},
                              "exits": exits})
    summary = {
        "captured_at": json.loads((OUT / "scope-before.json").read_text())["captured_at"],
        "ledger": {"all_trades": len(all_trades), "settled": len(settled), "open": len(all_trades) - len(settled),
                   "wins": sum(x["pnl"] > 0 for x in settled), "losses": sum(x["pnl"] < 0 for x in settled),
                   "full_stake_losses": sum(x["pnl"] < 0 and abs(x["pnl"] + x["stake"]) < .00001 for x in settled),
                   "net_pnl": sum(x["pnl"] for x in settled),
                   "settlement_sources": dict(collections.Counter(x["settlement_source"] or "unknown" for x in settled))},
        "coverage": {"replay_packets": packet_count, "replay_markets": len(samples), "matched_trades": len(paths) + len(excluded),
                     "usable_trades": len(paths), "usable_markets": len(rounds), "usable_losses": len(losses), "usable_wins": len(winners),
                     "denser_trades": len(cohorts["denser_paths"]), "first_entry": local_time(paths[0]["opened_at"]),
                     "last_entry": local_time(paths[-1]["opened_at"]), "invalid_samples": dict(invalid), "excluded": excluded,
                     "baseline_pnl": sum(x["pnl"] for x in paths),
                     "positive_observed_mfe_losers": sum(x["mfe"]["roi_pct"] > 0 for x in losses),
                     "loss_mfe_threshold_counts": {str(t): sum(x["mfe"]["roi_pct"] >= t for x in losses) for t in (0, 10, 20, 30, 40, 50)},
                     "win_mae_threshold_counts": {str(t): sum(x["mae"]["roi_pct"] <= -t for x in winners) for t in (20, 40, 60)}},
        "assumptions": {"fee_rate": FEE_RATE, "quote_max_age_ms": 3000, "after_entry_delay_seconds": .25,
                        "stress": "Trigger net return includes 1-cent price haircut and 50% displayed bid depth. Execute at next valid recorded observation >=500ms later and <=10s later, with same haircut/depth. Not an exact 500ms latency reconstruction.",
                        "denser_path": "At least 10 usable samples; first/last gaps <=15s; every valid-observation gap including boundaries <=20s. Still not continuous data.",
                        "limitations": ["Loss-selected legacy strategy markets; not representative of the main account. No population extrapolation.",
                                        "Recorded quote updated_ms is the legacy system timestamp; independent exchange sequence/receipt provenance was not retained.",
                                        "Observed extrema only; missing intervals may contain other triggers. Terminal returns are not intraperiod marks.",
                                        "Full-position hypothetical exits only. No actual orders, queue priority or fills established; thin-book partial exits are not modeled.",
                                        "Keep actual entries and entry costs fixed; no cash recycling, reentries, changed daily stops or portfolio effects. No whole-account return or drawdown claim.",
                                        "Candidate thresholds and chronological splits are exploratory on the same biased data, not out-of-sample validation.",
                                        "Current optimized decision logs contain selected-side top prices but no recorded bid sizes/depth; value-confirm logs contain asks. Neither supports full-position executable exits for recent main trades."]},
        "source_files": source_files,
        "scenarios": scenarios,
    }
    write_json("analysis.json", summary)
    write_json("trade-paths.json", paths)
    with (OUT / "trade-summary.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["trade_id", "round", "side", "opened_at_Asia_Shanghai", "stake_including_buy_fee", "entry_price", "shares", "terminal_pnl", "observed_mfe_pct", "observed_mfe_pnl", "mfe_time", "observed_mae_pct", "samples", "first_gap_s", "last_gap_s", "max_gap_s", "denser_path"])
        for r in paths:
            writer.writerow([r["id"], r["round_id"], r["side"], local_time(r["opened_at"]), r["stake"], r["entry_price"], r["shares"], r["pnl"], r["mfe"]["roi_pct"], r["mfe"]["pnl"], local_time(r["mfe"]["at"]), r["mae"]["roi_pct"], len(r["marks"]), r["first_gap_seconds"], r["last_gap_seconds"], r["max_gap_seconds"], r["denser_path"]])
    print(json.dumps({"ledger": summary["ledger"], "coverage": summary["coverage"]}, ensure_ascii=False, indent=2))
    for scenario in scenarios:
        m = scenario["cohorts"]["all_observed"]
        print("TP", scenario["take_profit_pct"], "SL", scenario["stop_loss_pct"], scenario["execution_assumption"],
              {k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()})


if __name__ == "__main__":
    main()

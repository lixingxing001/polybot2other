"""Read-only, chronological diagnostics of adaptive V2 on recorded fills.

This is an exploratory selection replay, not an executable market backtest.
Historical fill prices/costs are hindsight execution proxies. Rejected signals,
order-book depth, latency, capacity and counterfactual execution are unavailable.
No model or trading database is created or updated by this script.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

REPORT_VERSION = "adaptive-v2-recorded-fill-diagnostic-v1"
LIMITATIONS = [
    "Candidate calibration only: the runtime champion/challenger activation gate is evaluated separately and is not simulated here.",
    "Exploratory retrospective: all existing outcomes may already be known; no untouched holdout is claimed.",
    "Candidates are optimized-ledger fills, not all signals or rejected opportunities; selection bias remains.",
    "Historical fill price and stake/shares are hindsight execution-cost proxies, not the original decision-time book.",
    "Selected outcomes reuse historical fills exactly; depth, slippage changes, latency, balance, position limits and retries are not replayed.",
    "Training uses earlier observed optimized and provenance-verified adaptive V2 outcomes, including trades this replay would reject; this is shadow learning, not a self-contained counterfactual strategy.",
    "Old adaptive confidence values are not raw base scores and cannot be training labels for V2 without decision provenance.",
    "Only outcomes settled strictly before the earliest observed entry in the candidate market are available; the entire current market is excluded.",
    "Normalized sizing is an arithmetic comparison, not evidence that those sizes could have filled.",
    "Pending and post-cutoff settlements contribute no realized PnL, win rate or Brier score; their committed cost is shown separately.",
    "No parameter optimization, future profit guarantee, production change or live order is performed.",
]


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def iso(timestamp: float | None) -> str | None:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


def timestamp(value: str) -> float:
    numeric = finite(value)
    if numeric is not None:
        return numeric
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use epoch seconds or an ISO timestamp with timezone") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("ISO timestamps must include a timezone, e.g. +08:00")
    return parsed.timestamp()


def read_ledger(path: Path, source: str) -> dict[str, Any]:
    """Copy a consistent transaction's needed rows into memory via mode=ro.

    The content hash covers extracted rows, including active WAL data visible in
    this transaction. A hash of the SQLite main file alone would omit that data.
    """
    path = path.resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "trades" not in tables:
            raise ValueError(f"{source}: missing trades table in {path}")
        rows = [dict(row) for row in conn.execute("SELECT * FROM trades ORDER BY opened_at,id")]
        rounds = []
        if "market_rounds" in tables:
            round_columns = {row[1] for row in conn.execute("PRAGMA table_info(market_rounds)")}
            wanted = "round_id,ends_at" + (",outcome" if "outcome" in round_columns else "")
            rounds = [dict(row) for row in conn.execute(f"SELECT {wanted} FROM market_rounds ORDER BY round_id")]
        run = [dict(row) for row in conn.execute("SELECT * FROM paper_sim_run ORDER BY id")] if "paper_sim_run" in tables else []
        provenance = {}
        if source == "MAIN_ADAPTIVE" and {"paper_orders", "paper_sim_orders"} <= tables:
            for row in conn.execute("""
                SELECT o.trade_id,s.payload FROM paper_orders o
                JOIN paper_sim_orders s ON s.order_id=o.id
                WHERE o.trade_id IS NOT NULL ORDER BY o.id
            """):
                try:
                    payload = json.loads(row["payload"])
                    decision = payload.get("adaptive_decision")
                except (TypeError, ValueError, AttributeError):
                    continue
                if isinstance(decision, dict) and payload.get("action", "BUY") == "BUY":
                    provenance.setdefault(str(row["trade_id"]), decision)
        conn.rollback()
    copied = {"trades": rows, "rounds": rounds, "run": run, "adaptive_decisions": provenance}
    return {
        "source": source, "path": str(path), "read_mode": "sqlite mode=ro; query_only; transaction snapshot",
        "captured_at_utc": iso(time.time()), "content_sha256": digest(copied),
        "hash_scope": "Canonical JSON of extracted trades, round end times, run and adaptive decisions; includes visible WAL rows",
        **copied,
    }


def normalized_rows(ledger: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    from polybot2other.adaptive_calibration import MODEL_VERSION
    ends = {row["round_id"]: finite(row.get("ends_at")) for row in ledger.get("rounds", [])}
    outcomes = {row["round_id"]: row.get("outcome") for row in ledger.get("rounds", [])}
    rejected: Counter[str] = Counter()
    records = []
    source = ledger["source"]
    for trade in ledger["trades"]:
        opened = finite(trade.get("opened_at"))
        cost, shares, entry = (finite(trade.get(key)) for key in ("stake", "shares", "entry_price"))
        side, round_id = trade.get("side"), trade.get("round_id")
        if opened is None or not round_id or side not in {"Up", "Down"} or any(
            value is None or value <= 0 for value in (cost, shares, entry)
        ) or entry >= 1:
            rejected["invalid_trade_fields"] += 1
            continue
        decision = ledger.get("adaptive_decisions", {}).get(str(trade["id"]), {})
        if source == "MAIN_OPTIMIZED":
            base_score = finite(trade.get("confidence"))
            provenance = "optimized_raw"
        else:
            base_score = finite(decision.get("base_selected_score"))
            decision_at = finite(decision.get("decision_at"))
            version = str(decision.get("adaptive_model_version", decision.get("model_version", "")))
            if (base_score is None or decision_at is None or decision_at > opened
                    or decision.get("candidate_side") != side or version != MODEL_VERSION
                    or decision.get("round_id") != round_id):
                base_score = None
                rejected["legacy_or_unverified_adaptive_base_score"] += 1
            provenance = "adaptive_v2_raw" if base_score is not None else "unverified_adaptive"
        if base_score is not None and not .5 <= base_score <= 1:
            base_score = None
            rejected["invalid_base_score"] += 1
        settled = finite(trade.get("settled_at"))
        pnl, payout = finite(trade.get("pnl")), finite(trade.get("payout"))
        is_settled = trade.get("status") == "SETTLED" and settled is not None and pnl is not None
        if is_settled and settled < opened:
            rejected["settlement_precedes_open"] += 1
            continue
        end = ends.get(round_id)
        remaining = finite(decision.get("time_left_seconds")) if provenance == "adaptive_v2_raw" else None
        if remaining is None and end is not None:
            remaining = end - opened
        label = int(outcomes[round_id] == side) if outcomes.get(round_id) in {"Up", "Down"} else int(pnl > 0) if is_settled else None
        records.append({
            "source_key": f"{source}:{trade['id']}", "source_name": source, "trade_id": trade["id"],
            "round_id": str(round_id), "side": side, "base_score": base_score,
            "provenance": provenance, "opened_at": opened, "settled_at": settled,
            "is_settled": is_settled, "pnl": pnl, "payout": payout,
            "won": label if is_settled else None,
            "stake": cost, "shares": shares, "entry_price": entry,
            "all_in_cost": cost / shares,
            "time_left_seconds": remaining,
            "settlement_source": trade.get("settlement_source"),
        })
    return sorted(records, key=lambda row: (row["opened_at"], row["source_key"])), dict(rejected)


def outcome_as_of(row: dict[str, Any], cutoff: float) -> bool:
    return bool(row["is_settled"] and row["settled_at"] <= cutoff)


def summarize(decisions: list[dict[str, Any]], *, selected_only: bool,
              unit_stake: float, cutoff: float) -> dict[str, Any]:
    entries = [row for row in decisions if not selected_only or row["selected"]]
    settled = [row for row in entries if outcome_as_of(row, cutoff)]
    pending = [row for row in entries if not outcome_as_of(row, cutoff)]
    cost = sum(row["stake"] for row in settled)
    pnl = sum(row["pnl"] for row in settled)
    normalized_pnl = sum(row["pnl"] / row["stake"] * unit_stake for row in settled)
    # Aggregate simultaneous settlements: SQLite row order is not a risk path.
    cashflow: defaultdict[float, list[float]] = defaultdict(lambda: [0., 0.])
    for row in settled:
        cashflow[row["settled_at"]][0] += row["pnl"]
        cashflow[row["settled_at"]][1] += row["pnl"] / row["stake"] * unit_stake
    equity = [0., 0.]
    peak = [0., 0.]
    drawdown = [0., 0.]
    for event in sorted(cashflow):
        for index in (0, 1):
            equity[index] += cashflow[event][index]
            peak[index] = max(peak[index], equity[index])
            drawdown[index] = max(drawdown[index], peak[index] - equity[index])
    return {
        "recorded_candidates": len(entries), "settled_count": len(settled), "pending_count": len(pending),
        "wins": sum(row["won"] for row in settled),
        "win_rate": sum(row["won"] for row in settled) / len(settled) if settled else None,
        "historical_stake_including_costs": cost, "historical_net_pnl": pnl,
        "return_on_historical_stake": pnl / cost if cost else None,
        "settled_payout": sum(row["payout"] for row in settled if row["payout"] is not None),
        "payout_available_count": sum(row["payout"] is not None for row in settled),
        "inferred_entry_fees": sum(row["stake"] - row["shares"] * row["entry_price"] for row in settled),
        "max_realized_drawdown": drawdown[0],
        "normalized_unit_stake": unit_stake, "normalized_net_pnl": normalized_pnl,
        "normalized_max_realized_drawdown": drawdown[1],
        "pending_committed_cost": sum(row["stake"] for row in pending),
        "normalized_pending_committed_cost": len(pending) * unit_stake,
        "brier_raw": sum((row["base_score"] - row["won"]) ** 2 for row in settled) / len(settled) if settled else None,
        "brier_v2": sum((row["predicted_probability"] - row["won"]) ** 2 for row in settled) / len(settled) if settled else None,
        "mean_raw_probability": sum(row["base_score"] for row in settled) / len(settled) if settled else None,
        "mean_v2_probability": sum(row["predicted_probability"] for row in settled) / len(settled) if settled else None,
        "drawdown_scope": "Realized net PnL, grouped by settlement timestamp; excludes unknown mark-to-market pending values",
    }


def evaluate(records: list[dict[str, Any]], *, cutoff: float, evaluation_start: float | None = None,
             split_fraction: float = .7, min_edge: float = .02, unit_stake: float = 5.,
             assessor: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    if not 0 <= split_fraction < 1:
        raise ValueError("split_fraction must be in [0,1)")
    if not math.isfinite(cutoff) or not math.isfinite(unit_stake) or unit_stake <= 0:
        raise ValueError("cutoff must be finite and unit_stake must be positive")
    if not math.isfinite(min_edge) or min_edge < 0:
        raise ValueError("min_edge must be finite and nonnegative")
    if assessor is None:
        from polybot2other.adaptive_calibration import calibrate_entry
        assessor = calibrate_entry
    observed = [row for row in records if row["opened_at"] <= cutoff]
    candidates = [row for row in observed if row["source_name"] == "MAIN_OPTIMIZED" and row["base_score"] is not None]
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        groups[row["round_id"]].append(row)
    ordered = sorted(groups, key=lambda key: (min(row["opened_at"] for row in groups[key]), key))
    if evaluation_start is None:
        split_index = min(int(len(ordered) * split_fraction), max(0, len(ordered) - 1))
        test_rounds = ordered[split_index:]
        split_at = min(row["opened_at"] for row in groups[test_rounds[0]]) if test_rounds else cutoff
        split_mode = "chronological fraction of distinct recorded markets"
    else:
        if not math.isfinite(evaluation_start) or evaluation_start > cutoff:
            raise ValueError("evaluation_start must be finite and no later than cutoff")
        split_at = evaluation_start
        test_rounds = [key for key in ordered if min(row["opened_at"] for row in groups[key]) >= split_at]
        split_mode = "explicit start; markets spanning the start are excluded wholesale"
    training_pool = [row for row in observed if row["base_score"] is not None and row["provenance"] in
                     {"optimized_raw", "adaptive_v2_raw"} and row["is_settled"]
                     and row.get("settlement_source") != "early_exit"]
    decisions = []
    violations = []
    for round_id in test_rounds:
        now = min(row["opened_at"] for row in groups[round_id])
        earlier = [row for row in training_pool if row["settled_at"] < now and row["round_id"] != round_id]
        # Preserve only permissible observation fields; current/future outcomes
        # and historical trade PnL are never arguments to the scoring function.
        samples = [{key: row[key] for key in (
            "round_id", "side", "base_score", "opened_at", "settled_at", "won",
            "entry_price", "time_left_seconds", "provenance", "source_key",
        )} for row in earlier]
        invalid = [row["source_key"] for row in samples if row["settled_at"] >= now or row["round_id"] == round_id]
        violations.extend(invalid)
        train_hash = digest(samples)
        for candidate in sorted(groups[round_id], key=lambda row: (row["opened_at"], row["source_key"])):
            result = assessor(samples, base_score=candidate["base_score"], side=candidate["side"],
                              now=now, round_id=round_id, entry_price=candidate["entry_price"],
                              all_in_cost=candidate["all_in_cost"], min_edge=min_edge,
                              time_left_seconds=candidate["time_left_seconds"])
            prediction = finite(result.get("adaptive_score"))
            allowed = result.get("adaptive_allowed", result.get("allowed"))
            if prediction is None or not 0 <= prediction <= 1 or not isinstance(allowed, bool):
                raise ValueError("assessor returned invalid probability or allowed flag")
            training_cutoff = finite(result.get("adaptive_training_cutoff", result.get("training_cutoff")))
            if training_cutoff is not None and training_cutoff >= now:
                violations.append(candidate["source_key"] + ":model_training_cutoff")
            record = dict(candidate)
            # Even output diagnostics must not show unknown post-cutoff labels.
            if not outcome_as_of(record, cutoff):
                record.update(pnl=None, payout=None, won=None, settled_at=None, is_settled=False)
            record.update({
                "decision_at": now, "decision_at_utc": iso(now), "selected": allowed,
                "predicted_probability": prediction, "model": result,
                "available_training_rows": len(samples), "available_training_markets": len({row["round_id"] for row in samples}),
                "available_training_sha256": train_hash,
                "latest_available_settlement": max((row["settled_at"] for row in samples), default=None),
            })
            decisions.append(record)
    settled = [row for row in decisions if outcome_as_of(row, cutoff)]
    mismatches = [row["source_key"] for row in settled if row["payout"] is not None and
                  abs(row["payout"] - row["stake"] - row["pnl"]) > 1e-5]
    return {
        "report_version": REPORT_VERSION, "kind": "exploratory_recorded_fill_selection_replay",
        "model_scope": "candidate_calibration_only; runtime_activation_gate_not_evaluated",
        "untouched_holdout": False, "parameter_search_performed": False,
        "cutoff": cutoff, "cutoff_utc": iso(cutoff), "evaluation_start": split_at,
        "evaluation_start_utc": iso(split_at), "split_mode": split_mode, "split_fraction": split_fraction if evaluation_start is None else None,
        "candidate_source": "MAIN_OPTIMIZED raw confidence only", "min_edge": min_edge,
        "observed_optimized_candidates": len(candidates), "observed_markets": len(ordered),
        "evaluated_markets": len(test_rounds),
        "training_provenance_counts": dict(Counter(row["provenance"] for row in training_pool if row["settled_at"] <= cutoff)),
        "excluded_adaptive_unverified": sum(row["source_name"] == "MAIN_ADAPTIVE" and row["base_score"] is None for row in observed),
        "baseline_all_recorded": summarize(decisions, selected_only=False, unit_stake=unit_stake, cutoff=cutoff),
        "v2_selected": summarize(decisions, selected_only=True, unit_stake=unit_stake, cutoff=cutoff),
        "decision_states": dict(Counter(str(row["model"].get("adaptive_state", row["model"].get("state"))) for row in decisions)),
        "chronology_audit": {"passed": not violations, "violations": violations,
                             "strictly_earlier_settlements": True, "current_market_excluded": True,
                             "market_group_clock": "earliest observed optimized entry in the entire market"},
        "accounting_audit": {"passed": not mismatches, "payout_minus_cost_mismatches": mismatches},
        "limitations": LIMITATIONS, "decisions": decisions,
    }


def text_report(report: dict[str, Any]) -> str:
    def amount(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.6f}"
    lines = [
        "MAIN_ADAPTIVE V2 — recorded-fill chronological diagnostic",
        "EXPLORATORY RETROSPECTIVE; NOT AN UNTOUCHED HOLDOUT OR AN EXECUTABLE BACKTEST.",
        "Candidate calibration only; runtime activation gate evaluated separately.",
        f"Evaluation: {report['evaluation_start_utc']} through {report['cutoff_utc']}",
        f"Model: {report.get('model_version', 'see decision metadata')}; no parameter search",
        "group | recorded | settled | pending | win rate | net PnL | normalized PnL | realized drawdown | Brier raw/V2",
    ]
    for name in ("baseline_all_recorded", "v2_selected"):
        row = report[name]
        win = "n/a" if row["win_rate"] is None else f"{row['win_rate']:.2%}"
        lines.append(f"{name} | {row['recorded_candidates']} | {row['settled_count']} | {row['pending_count']} | {win} | "
                     f"{amount(row['historical_net_pnl'])} | {amount(row['normalized_net_pnl'])} | {amount(row['max_realized_drawdown'])} | "
                     f"{amount(row['brier_raw'])}/{amount(row['brier_v2'])}")
        lines.append(f"  Pending committed cost: {amount(row['pending_committed_cost'])}; normalized unit: {row['normalized_unit_stake']}")
    lines.extend([f"Chronology audit: {report['chronology_audit']}", f"Accounting audit: {report['accounting_audit']}", "Limitations:"])
    lines.extend("- " + line for line in report["limitations"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimized-db", type=Path, required=True)
    parser.add_argument("--adaptive-db", type=Path)
    parser.add_argument("--cutoff", type=timestamp, default=None)
    parser.add_argument("--evaluation-start", type=timestamp)
    parser.add_argument("--split-fraction", type=float, default=.7)
    parser.add_argument("--min-edge", type=float, default=.02)
    parser.add_argument("--unit-stake", type=float, default=5.)
    parser.add_argument("--output", type=Path, required=True, help="JSON report; sibling .txt contains the readable summary")
    args = parser.parse_args(argv)
    cutoff = time.time() if args.cutoff is None else args.cutoff
    output, text_output = args.output.resolve(), args.output.resolve().with_suffix(".txt")
    if output.suffix.lower() != ".json":
        parser.error("--output must end in .json")
    source_paths = [p.resolve() for p in (args.optimized_db, args.adaptive_db) if p]
    protected = {p for source in source_paths for p in (source, Path(str(source) + "-wal"), Path(str(source) + "-shm"))}
    if output in protected or text_output in protected:
        parser.error("Reports cannot overwrite a source database or its WAL/SHM files")
    try:
        ledgers = [read_ledger(args.optimized_db, "MAIN_OPTIMIZED")]
        if args.adaptive_db:
            if args.adaptive_db.resolve() == args.optimized_db.resolve():
                parser.error("Optimized and adaptive sources must be different ledgers")
            ledgers.append(read_ledger(args.adaptive_db, "MAIN_ADAPTIVE"))
        records, excluded = [], {}
        for ledger in ledgers:
            normalized, rejected = normalized_rows(ledger)
            records.extend(normalized)
            excluded[ledger["source"]] = rejected
        report = evaluate(records, cutoff=cutoff, evaluation_start=args.evaluation_start,
                          split_fraction=args.split_fraction, min_edge=args.min_edge, unit_stake=args.unit_stake)
        from polybot2other import adaptive_calibration
        report["model_version"] = adaptive_calibration.MODEL_VERSION
        report["model_policy"] = adaptive_calibration.POLICY
        report["model_source_sha256"] = hashlib.sha256(Path(adaptive_calibration.__file__).read_bytes()).hexdigest()
        report["evaluator_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report["datasets"] = [{key: value for key, value in ledger.items() if key not in
                               {"trades", "rounds", "adaptive_decisions"}} for ledger in ledgers]
        report["excluded_or_unverified_rows"] = excluded
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        summary = text_report(report)
        text_output.write_text(summary)
        print(summary, end="")
        print(f"JSON: {output}\nText: {text_output}")
        return 0 if report["chronology_audit"]["passed"] and report["accounting_audit"]["passed"] else 2
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only causal feature audit and chronological V3 diagnostics.

Reads a fixed ledger/model backup and the append-only shared reference cache.
Historical outcomes are already observed: this is exploratory, not an untouched
holdout and not an execution simulation. No production database is updated.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

VERSION = "adaptive-v3-causal-history-audit-v1"
LIMITATIONS = [
    "Exploratory historical analysis: existing outcomes were previously inspected; no untouched holdout is claimed.",
    "Quote-cost utility is theoretical per unit budget, not realized trading PnL or an executable backtest.",
    "Historical depth, complete rejected-opportunity coverage, latency, retries, capital and fill changes are not replayed.",
    "Optimized order scores were known by submission; exact earlier signal-generation time was not retained.",
    "Only source and receive timestamps at or before the observation are eligible for reconstructed features.",
    "Arrival books and fill prices are never used to reconstruct decision-time spread or cost.",
    "Missing or stale market features stay missing; a missing feature does not mean zero momentum or spread.",
    "Each training forecast uses only labels settled strictly before that decision, excluding the entire current market.",
]


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def read_json(value):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def load_payloads(path):
    """Only already-committed order metadata; no writes or secret-bearing fields."""
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        result = {}
        for row in conn.execute("""SELECT o.trade_id,s.payload FROM paper_orders o
                JOIN paper_sim_orders s ON s.order_id=o.id
                WHERE o.trade_id IS NOT NULL ORDER BY o.id"""):
            payload = read_json(row["payload"])
            if payload.get("action", "BUY") != "BUY":
                continue
            result.setdefault(row["trade_id"], payload)
        return result


def read_history(backup):
    """Return verified core rows with decision-time provenance, before enrichment."""
    payloads = {"MAIN_OPTIMIZED": load_payloads(backup/"optimized.sqlite3"),
                "MAIN_ADAPTIVE": load_payloads(backup/"adaptive.sqlite3")}
    with closing(sqlite3.connect((backup/"model.sqlite3").resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        training = [dict(row) for row in conn.execute("SELECT * FROM training_samples ORDER BY settled_at,source_key")]
        observed = [dict(row) for row in conn.execute("SELECT * FROM calibration_observations ORDER BY decision_at")]
    excluded = Counter()
    rows = []
    for original in training:
        if original.get("provenance") not in ("optimized_raw", "adaptive_v2_raw") or original.get("exclusion_reason"):
            excluded[original.get("exclusion_reason") or "unverified_provenance"] += 1
            continue
        row = dict(original)
        payload = payloads.get(row["source_name"], {}).get(row["source_trade_id"], {})
        decision = payload.get("adaptive_decision") or {}
        market, signal = payload.get("market") or {}, payload.get("signal") or {}
        if row["source_name"] == "MAIN_ADAPTIVE":
            decision_at = finite(decision.get("decision_at"))
            basis = "adaptive-decision"
            ask, bid = finite(decision.get("ask")), finite(decision.get("bid"))
            target = finite(decision.get("target_price"))
            entry = finite(decision.get("entry_price"))
            cost = finite(decision.get("all_in_cost"))
            min_edge = finite(decision.get("min_edge"))
        else:
            decision_at = finite(payload.get("submitted_at"))
            basis = "order-submission"
            # No bid was retained before arrival for original optimized orders.
            ask, bid = finite(signal.get("entry_price")), None
            target = finite(market.get("target_price"))
            entry = ask
            limit = finite(payload.get("limit"))
            info = payload.get("info") or {}
            rate, exponent = finite(info.get("fee_rate")), finite(info.get("fee_exponent"))
            cost = limit+round(rate*(limit*(1-limit))**exponent, 6) if (
                limit is not None and 0 < limit < 1 and rate is not None and rate >= 0
                and exponent is not None and exponent >= 0) else None
            # A diagnostic baseline; this is not a reconstruction of old runtime settings.
            min_edge = .02
        if decision_at is None or not decision_at <= row["opened_at"] < row["settled_at"]:
            excluded["missing_valid_pre_fill_decision_time"] += 1
            row.update(v3_known_at=None, decision_time_basis="unavailable",
                       v3_feature_status="MISSING_DECISION_TIME", v3_core_only=True)
            rows.append(row)
            continue
        ends = finite(market.get("ends_at"))
        if ends is None:
            ends = finite(decision.get("market_ends_at"))
        row.update(decision_at=decision_at, v3_known_at=decision_at,
                   decision_time_basis=basis, decision_entry_price=entry,
                   entry_price=entry if entry is not None else row["entry_price"],
                   all_in_cost=cost, min_edge=min_edge, fee_source="official-market" if cost is not None else None,
                   time_left_seconds=ends-decision_at if ends is not None else row.get("time_left_seconds"),
                   _ask=ask, _bid=bid, _target_price=target)
        rows.append(row)
    shadows = []
    for original in observed:
        payload = read_json(original["payload"])
        decision_at = finite(original["decision_at"])
        row = {"source_key":"SHADOW:"+original["round_id"], "source_name":"SHADOW",
               "round_id":original["round_id"], "side":original["side"],
               "base_score":payload.get("base_selected_score"), "entry_price":payload.get("entry_price"),
               "decision_entry_price":payload.get("entry_price"), "opened_at":decision_at,
               "decision_at":decision_at, "v3_known_at":decision_at,
               "decision_time_basis":"shadow-decision", "settled_at":original["settled_at"],
               "won":int(original["side"]==original["outcome"]) if original["outcome"] in ("Up","Down") else None,
               "provenance":"shadow_v2_raw", "time_left_seconds":payload.get("time_left_seconds"),
               "all_in_cost":payload.get("all_in_cost"), "min_edge":payload.get("min_edge"),
               "fee_source":payload.get("fee_source"), "label_source":original["label_source"],
               "recorded_at":original["recorded_at"], "ends_at":original["ends_at"],
               "model_version":payload.get("adaptive_model_version"),
               "v2_candidate_score":payload.get("adaptive_candidate_score"),
               "v2_candidate_delta":payload.get("adaptive_candidate_delta"),
               "v2_validation_state":payload.get("adaptive_validation_state"),
               "_ask":payload.get("entry_price"), "_bid":None, "_target_price":None}
        shadows.append(row)
    return rows, shadows, dict(excluded)


def enrich_rows(rows, reference_path):
    """Shared production feature calculation; fixed observation time and source."""
    from polybot2other.adaptive_features import extract_market_features
    features = []
    with closing(sqlite3.connect(reference_path.resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for row in rows:
            known_at = finite(row.get("v3_known_at"))
            if known_at is None:
                features.append(dict(row))
                continue
            stamp = known_at*1000
            points = conn.execute("""SELECT source_ms,price,received_ms FROM reference_ticks
                WHERE channel='price.crypto' AND source_ms>=? AND source_ms<=? AND received_ms<=?
                ORDER BY source_ms""", (stamp-180000, stamp, stamp)).fetchall()
            feature = extract_market_features(points, decision_at=known_at,
                ask=row.get("_ask"), bid=row.get("_bid"), target_price=row.get("_target_price"))
            clean = {k:v for k,v in row.items() if not k.startswith("_")}
            clean.update(feature, v3_reference_source=str(reference_path.resolve()))
            features.append(clean)
    return features


def summary(rows):
    return {"rows":len(rows), "by_source":dict(Counter(r["source_name"] for r in rows)),
            "feature_status":dict(Counter(r.get("v3_feature_status","unknown") for r in rows)),
            "coverage":{k:sum(finite(r.get(k)) is not None for r in rows)
                for k in ("base_score","entry_price","time_left_seconds","all_in_cost",
                    "v3_momentum_10_bps","v3_momentum_30_bps","v3_momentum_60_bps",
                    "v3_volatility_bps","v3_trend_gap_bps","v3_spread")}}


def diagnose_v2(rows):
    groups = defaultdict(list)
    for row in rows:
        if row.get("won") is None or row.get("settled_at") is None:
            continue
        raw, candidate, cost, edge = (finite(row.get(k)) for k in
                                     ("base_score","v2_candidate_score","all_in_cost","min_edge"))
        if None in (raw,candidate,cost,edge) or cost <= 0:
            continue
        baseline, challenger = raw-cost+1e-12 >= edge, candidate-cost+1e-12 >= edge
        key = "both" if baseline and challenger else "removed" if baseline else "added" if challenger else "neither"
        groups[key].append({**row,"quote_return":row["won"]/cost-1})
    def group(items):
        return {"count":len(items),"wins":sum(r["won"] for r in items),
                "unit_budget_quote_return_sum":sum(r["quote_return"] for r in items),
                "mean_candidate_delta":sum(r["v2_candidate_delta"] for r in items)/len(items) if items else None}
    return {key:{**group(items),"by_side":{side:group([r for r in items if r["side"]==side])
                    for side in ("Up","Down")}} for key,items in groups.items()}


def feature_patches(rows):
    """Portable migration evidence: no outcomes, credentials, raw payloads or PnL."""
    keys = {"source_key","round_id","source_name","decision_at","v3_known_at",
            "decision_time_basis","entry_price","decision_entry_price","time_left_seconds",
            "all_in_cost","min_edge","fee_source"}
    return [{k:v for k,v in row.items() if k in keys or k.startswith("v3_") and not k.startswith("v3_candidate")}
            for row in rows]


def chronological_diagnostic(training, shadows):
    """Frozen-policy walk-forward on already-seen observations, never promotion."""
    from polybot2other.adaptive_v3 import MODEL_VERSION, POLICY, fit_model, predict
    history = training+shadows
    forecasts = []
    for row in sorted(shadows, key=lambda r:(r["decision_at"],r["round_id"])):
        if row.get("settled_at") is None or row.get("won") is None:
            continue
        model = fit_model(history, now=row["decision_at"], round_id=row["round_id"])
        assert model["trained_before"] is None or model["trained_before"] < row["decision_at"]
        prediction = predict(model,row)
        forecasts.append({**row,"v3_candidate_score":prediction["score"],
            "v3_candidate_delta":prediction["delta"],"v3_training_cutoff":model["trained_before"],
            "v3_training_fingerprint":model["training_fingerprint"],
            "v3_training_markets":model["counts"]["unique_markets"],
            "v3_training_effective_samples":model["counts"]["effective_samples"],
            "v3_used_features":prediction["feature_coverage"]["used"]})

    def describe(items,score_key):
        done=[r for r in items if r.get("won") is not None]
        selected=[r for r in done if r[score_key]-r["all_in_cost"]+1e-12>=r["min_edge"]]
        utility=sum(r["won"]/r["all_in_cost"]-1 for r in selected)
        by_settlement=defaultdict(float)
        for r in selected:by_settlement[r["settled_at"]]+=r["won"]/r["all_in_cost"]-1
        total=peak=drawdown=0.
        for timestamp in sorted(by_settlement):
            total+=by_settlement[timestamp];peak=max(peak,total);drawdown=max(drawdown,peak-total)
        return {"observations":len(done),"selected":len(selected),
            "selected_wins":sum(r["won"] for r in selected),
            "brier":sum((r[score_key]-r["won"])**2 for r in done)/len(done) if done else None,
            "unit_budget_quote_return_sum":utility,
            "quote_return_per_opportunity":utility/len(done) if done else None,
            "unit_budget_quote_max_drawdown":drawdown}

    split=int(len(forecasts)*.7)
    windows={"all_recorded_shadow":forecasts,"first_70_percent":forecasts[:split],
             "last_30_percent_retrospective":forecasts[split:]}
    metrics={name:{label:describe(rows,key) for label,key in
             (("baseline","base_score"),("recorded_v2","v2_candidate_score"),("replayed_v3","v3_candidate_score"))}
             for name,rows in windows.items()}
    groups=defaultdict(list)
    for row in forecasts:
        baseline=row["base_score"]-row["all_in_cost"]+1e-12>=row["min_edge"]
        candidate=row["v3_candidate_score"]-row["all_in_cost"]+1e-12>=row["min_edge"]
        groups["both" if baseline and candidate else "removed" if baseline else "added" if candidate else "neither"].append(row)
    changes={key:{"count":len(rows),"wins":sum(r["won"] for r in rows),
                  "unit_budget_quote_return_sum":sum(r["won"]/r["all_in_cost"]-1 for r in rows)}
             for key,rows in groups.items()}
    path=ROOT/"src/polybot2other/adaptive_v3.py"
    return {"model_version":MODEL_VERSION,"policy":POLICY,
        "source_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"windows":metrics,
        "selection_changes":changes,"validation_eligible":False,
        "validation_exclusion":"Historical V3 predictions were reconstructed after outcomes; only prospective V3 records may promote.",
        "parameter_search_performed":False,"forecasts":forecasts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup",type=Path,required=True)
    parser.add_argument("--reference",type=Path,default=ROOT/"data/live-accounts/market.references.sqlite3")
    parser.add_argument("--output-dir",type=Path,default=ROOT/"reports/adaptive-v3-20261116")
    parser.add_argument("--evaluate",action="store_true",help="Run fixed-policy chronological diagnostics; does not train a production model")
    args=parser.parse_args()
    training,shadows,excluded=read_history(args.backup)
    training=enrich_rows(training,args.reference);shadows=enrich_rows(shadows,args.reference)
    report={"version":VERSION,"created_at":datetime.now(timezone.utc).isoformat(),
            "backup":str(args.backup.resolve()),"reference":str(args.reference.resolve()),
            "training":summary(training),"shadow":summary(shadows),"exclusions":excluded,
            "v2_diagnostic":diagnose_v2(shadows),"limitations":LIMITATIONS,
            "data_digest":digest({"training":training,"shadow":shadows})}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/"feature-audit.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    patches={"version":VERSION,"source_digest":report["data_digest"],
             "reference_source":str(args.reference.resolve()),
             "patches":feature_patches(training+shadows)}
    (args.output_dir/"historical-feature-patches.json").write_text(json.dumps(patches,ensure_ascii=False,indent=2)+"\n")
    (args.output_dir/"historical-evaluation-rows.json").write_text(json.dumps(
        {"version":VERSION,"training":training,"shadow":shadows,"limitations":LIMITATIONS},
        ensure_ascii=False,indent=2)+"\n")
    if args.evaluate:
        diagnostic=chronological_diagnostic(training,shadows)
        (args.output_dir/"chronological-diagnostic.json").write_text(json.dumps(
            {"version":VERSION,"data_digest":report["data_digest"],"limitations":LIMITATIONS,**diagnostic},
            ensure_ascii=False,indent=2)+"\n")
        report["v3_diagnostic"]={k:v for k,v in diagnostic.items() if k!="forecasts"}
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()

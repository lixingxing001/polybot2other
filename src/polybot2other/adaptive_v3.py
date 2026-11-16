"""Small causal context learner and forward-only promotion checks for adaptive V3.

The base log odds are an offset, never a learned replacement direction model.
The fixed ridge penalty, scales and bounds are declared policy, not a search over
historical profit. Missing features make no contribution and are reported.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math

from .adaptive_calibration import finite

MODEL_VERSION = "main-adaptive-context-v3"
HALF_LIFE_SECONDS = 72 * 3600
MAX_SAMPLE_AGE_SECONDS = 30 * 86400
MIN_EFFECTIVE_SAMPLES = 40
MIN_FEATURE_EFFECTIVE_SAMPLES = 40
MAX_ADJUSTMENT = .06
RIDGE_PENALTY = 8.0
FEATURES = ("intercept", "base_score", "entry_price", "side", "time_left",
            "momentum_10", "momentum_30", "momentum_60", "volatility", "trend_gap", "spread")
MARKET_FIELDS = ("v3_momentum_10_bps", "v3_momentum_30_bps", "v3_momentum_60_bps",
                 "v3_volatility_bps", "v3_trend_gap_bps", "v3_spread")
FIELD_FOR_FEATURE = dict(zip(FEATURES[5:], MARKET_FIELDS))
PROVENANCE_PRIORITY = {"shadow_v3_raw": 0, "shadow_v2_raw": 1, "adaptive_v3_raw": 2,
                       "adaptive_v2_raw": 3, "optimized_raw": 4}
POLICY = {"model": "ridge_logistic_residual", "base_logit_offset": True,
          "half_life_hours": 72, "max_age_days": 30, "ridge_penalty": RIDGE_PENALTY,
          "minimum_effective_samples": MIN_EFFECTIVE_SAMPLES,
          "minimum_feature_effective_samples": MIN_FEATURE_EFFECTIVE_SAMPLES,
          "max_upward_adjustment": MAX_ADJUSTMENT, "max_downward_adjustment": MAX_ADJUSTMENT,
          "deduplication": "one_market_one_observation", "same_round_training": False,
          "features": list(FEATURES), "missing_features": "no_contribution_with_explicit_coverage"}
VALIDATION_POLICY = {"window_days": 7, "minimum_markets": 40, "minimum_observation_hours": 24,
    "minimum_selected_each": 10, "minimum_brier_improvement": .005,
    "paired_brier_lower90_must_be_positive": True, "minimum_candidate_quote_utility": 0.,
    "candidate_quote_utility_must_improve": True,
    "candidate_drawdown_must_not_exceed_baseline": True,
    "maximum_recording_delay_seconds": 30, "required_fee_source": "official-market",
    "utility_basis": "quote_cost_per_budget_per_opportunity_not_realized_pnl"}


def _clip(value, low, high):
    return min(high, max(low, value))


def _logit(p):
    p = _clip(p, 1e-6, 1-1e-6)
    return math.log(p/(1-p))


def _sigmoid(value):
    if value >= 0:
        return 1/(1+math.exp(-min(value, 700)))
    e = math.exp(max(value, -700))
    return e/(1+e)


def feature_values(row):
    """Fixed-scale features; None means unavailable, never a synthesized tick.

    Momentum and spot-vs-prior-60-second-average gap are signed and aligned to the
    proposed side here. Volatility is bps per sqrt(second), not a raw price.
    """
    base = finite(row.get("base_score", row.get("base_selected_score")))
    entry = finite(row.get("entry_price"))
    side = row.get("side", row.get("candidate_side"))
    seconds = finite(row.get("time_left_seconds"))
    sign = 1. if side == "Up" else -1. if side == "Down" else None
    result = {name: None for name in FEATURES}
    if base is not None and .5 <= base <= 1:
        result["intercept"] = 1.
        result["base_score"] = _clip((base-.7)/.2, -3, 3)
    if entry is not None and 0 < entry < 1:
        result["entry_price"] = _clip((entry-.6)/.2, -3, 3)
    result["side"] = sign
    if seconds is not None and 0 < seconds <= 300:
        result["time_left"] = (seconds-150)/150
    for name, scale in (("momentum_10", 5.), ("momentum_30", 10.), ("momentum_60", 15.), ("trend_gap", 15.)):
        value = finite(row.get(FIELD_FOR_FEATURE[name]))
        if value is not None and sign is not None:
            result[name] = _clip(sign*value/scale, -3, 3)
    volatility = finite(row.get("v3_volatility_bps"))
    if volatility is not None and volatility >= 0:
        result["volatility"] = _clip(volatility/2., 0, 3)
    spread = finite(row.get("v3_spread"))
    if spread is not None and 0 <= spread < 1:
        result["spread"] = _clip(spread/.03, 0, 3)
    return result


def canonical_samples(samples, *, now, round_id=None):
    """Availability filtering precedes market de-duplication and fitting."""
    now = finite(now)
    if now is None:
        raise ValueError("V3 fitting timestamp must be finite")
    counts, selected = Counter(), {}
    total = 0
    for source in samples:
        total += 1
        row = dict(source)
        provenance = row.get("provenance")
        if provenance not in PROVENANCE_PRIORITY:
            counts["legacy_or_unknown_provenance"] += 1
            continue
        base, opened, settled, decision, won = (finite(row.get(k)) for k in
            ("base_score", "opened_at", "settled_at", "decision_at", "won"))
        entry = finite(row.get("decision_entry_price"))
        if entry is None:
            entry = finite(row.get("entry_price"))
        if decision is None:
            decision = opened
        ident = row.get("round_id")
        if (not isinstance(ident, str) or not ident or base is None or not .5 <= base <= 1
                or entry is None or not 0 < entry < 1 or row.get("side") not in ("Up", "Down")
                or opened is None or settled is None or decision is None or not decision <= opened < settled
                or won not in (0., 1.) or row.get("exclusion_reason")):
            counts["invalid_or_nonterminal_label"] += 1
            continue
        if settled >= now:
            counts["not_yet_settled"] += 1
            continue
        if ident == round_id:
            counts["current_market"] += 1
            continue
        if now-settled > MAX_SAMPLE_AGE_SECONDS:
            counts["expired"] += 1
            continue
        row.update(base_score=base, entry_price=entry, opened_at=opened,
                   decision_at=decision, settled_at=settled, won=int(won),
                   weight=2.**(-(now-settled)/HALF_LIFE_SECONDS))
        old = selected.get(ident)
        priority = lambda r: (PROVENANCE_PRIORITY[r["provenance"]], r["decision_at"], str(r.get("source_key", "")))
        if old is not None:
            counts["duplicate_market"] += 1
        if old is None or priority(row) < priority(old):
            selected[ident] = row
    rows = sorted(selected.values(), key=lambda r: (r["settled_at"], r["round_id"]))
    return rows, {"raw_samples": total, "canonical_samples": len(rows), "unique_market_samples": len(rows),
                  "recent_effective_samples": sum(r["weight"] for r in rows),
                  "unique_markets": len(rows), "effective_samples": sum(r["weight"] for r in rows),
                  "excluded_legacy_samples": counts["legacy_or_unknown_provenance"], "exclusions": dict(counts)}


def _solve(matrix, vector):
    """Cholesky solve of the strictly positive-definite ridge Hessian."""
    size = len(vector)
    lower = [[0.] * size for _ in range(size)]
    for i in range(size):
        for j in range(i+1):
            value = matrix[i][j]-sum(lower[i][k]*lower[j][k] for k in range(j))
            lower[i][j] = math.sqrt(max(value, 1e-12)) if i == j else value/lower[j][j]
    forward = [0.] * size
    for i in range(size):
        forward[i] = (vector[i]-sum(lower[i][j]*forward[j] for j in range(i)))/lower[i][i]
    result = [0.] * size
    for i in reversed(range(size)):
        result[i] = (forward[i]-sum(lower[j][i]*result[j] for j in range(i+1, size)))/lower[i][i]
    return result


def fit_model(rows, *, now, round_id=None):
    """Fit fixed-penalty residual log odds on prior terminal market outcomes."""
    selected, counts = canonical_samples(rows, now=now, round_id=round_id)
    values = [feature_values(r) for r in selected]
    feature_counts = {key: {"samples": sum(v[key] is not None for v in values),
        "effective_samples": sum(r["weight"] for r, v in zip(selected, values) if v[key] is not None)} for key in FEATURES}
    active = [key for key in FEATURES if feature_counts[key]["effective_samples"] >= MIN_FEATURE_EFFECTIVE_SAMPLES]
    ready = counts["recent_effective_samples"] >= MIN_EFFECTIVE_SAMPLES
    fingerprint_rows = [{"round_id": r["round_id"], "provenance":r["provenance"], "decision_at":r["decision_at"],
        "settled_at":r["settled_at"], "base_score":r["base_score"], "won":r["won"], "features":v}
        for r, v in zip(selected, values)]
    fingerprint = hashlib.sha256(json.dumps(fingerprint_rows, sort_keys=True, allow_nan=False,
                                           separators=(",", ":")).encode()).hexdigest()
    weights = [0.] * len(active)
    iterations = 0
    if ready and active:
        # Missing components contribute zero algebraically, with explicit masks;
        # no missing raw value is imputed or exposed as an observed zero.
        matrix = [[v[name] if v[name] is not None else 0. for name in active] for v in values]
        for iteration in range(20):
            gradient = [RIDGE_PENALTY*b for b in weights]
            hessian = [[RIDGE_PENALTY if i == j else 0. for j in range(len(active))] for i in range(len(active))]
            for row, vector in zip(selected, matrix):
                p = _sigmoid(_logit(row["base_score"])+sum(b*x for b,x in zip(weights,vector)))
                residual, curvature = row["weight"]*(p-row["won"]), row["weight"]*p*(1-p)
                for i, x in enumerate(vector):
                    gradient[i] += residual*x
                    for j in range(i+1):
                        hessian[i][j] += curvature*x*vector[j]
            for i in range(len(active)):
                for j in range(i):
                    hessian[j][i] = hessian[i][j]
            step = _solve(hessian, gradient)
            # A deterministic trust radius prevents extreme Newton jumps on a
            # nearly separated small sample; it is not tuned on PnL.
            length = max((abs(x) for x in step), default=0.)
            scale = min(1., 1./length) if length else 1.
            weights = [b-scale*d for b,d in zip(weights,step)]
            iterations = iteration+1
            if length < 1e-7:
                break
    learned = {key: 0. for key in FEATURES}
    learned.update(zip(active, weights))
    return {"model_version": MODEL_VERSION, "state": "READY" if ready else "BASELINE_FALLBACK",
            "weights": learned, "features": list(FEATURES), "active_features": active if ready else [],
            "feature_counts": feature_counts, "counts": counts, "fitted_at": float(now),
            "trained_before": max((r["settled_at"] for r in selected), default=None),
            "training_fingerprint": fingerprint, "iterations": iterations, "policy": dict(POLICY)}


def predict(model, features):
    """Return a bounded candidate; this does not activate it for trading."""
    values = feature_values(features)
    base = finite(features.get("base_score", features.get("base_selected_score")))
    if base is None or not .5 <= base <= 1 or values["side"] is None:
        raise ValueError("V3 prediction requires an unadjusted base score and direction")
    active = set(model.get("active_features", []))
    contributions = {key: model.get("weights", {}).get(key, 0.)*value
                     for key, value in values.items() if value is not None and key in active}
    usable = model.get("state") == "READY" and model.get("model_version") == MODEL_VERSION
    score = _sigmoid(_logit(base)+sum(contributions.values())) if usable else base
    score = _clip(score, max(.01, base-MAX_ADJUSTMENT), min(.99, base+MAX_ADJUSTMENT)) if usable else base
    available = [key for key, value in values.items() if value is not None]
    missing = [key for key, value in values.items() if value is None]
    used = [key for key in available if key in active]
    return {"score":score, "delta":score-base, "base_score":base,
            "feature_coverage":{"available":available, "missing":missing, "used":used,
                "market_available":sum(name in available for name in FEATURES[5:]),
                "market_total":len(MARKET_FIELDS)},
            "contributions":contributions if usable else {},
            "state":"CONTEXT_CANDIDATE" if usable else "BASELINE_FALLBACK",
            "reason":"近期行情条件残差学习候选，等待独立效果验证" if usable else "有效独立样本不足，原始基线继续运行"}


def _drawdown(rows, selected):
    by_time = defaultdict(float)
    for r in rows:
        if selected(r):
            by_time[r["settled_at"]] += r["won"]/r["all_in_cost"]-1
    level = peak = maximum = 0.
    for timestamp in sorted(by_time):
        level += by_time[timestamp]
        peak = max(peak, level)
        maximum = max(maximum, peak-level)
    return maximum


def validate_candidate(observations, *, now, round_id=None):
    """Evaluate immutable V3 forecasts on the same forward opportunity set.

    Quotes are theoretical budget-normalized utilities, never simulated fills
    or a statement of realized PnL. Old versions and historical backfills cannot
    promote a new candidate. Simultaneous settlements are aggregated for drawdown.
    """
    now = finite(now)
    if now is None:
        raise ValueError("V3 validation timestamp must be finite")
    unique, excluded = {}, Counter()
    for source in observations:
        row = dict(source)
        values = {key: finite(row.get(key)) for key in
                  ("decision_at", "settled_at", "recorded_at", "ends_at", "base_score", "candidate_score", "all_in_cost", "min_edge", "won")}
        if any(v is None for v in values.values()):
            excluded["invalid_fields"] += 1
            continue
        if row.get("model_version") != MODEL_VERSION:
            excluded["different_version"] += 1
            continue
        d, settled, captured = (values[k] for k in ("decision_at", "settled_at", "recorded_at"))
        ident = row.get("round_id")
        if (not ident or ident == round_id or row.get("label_source") != "polymarket_official"
                or row.get("fee_source") != VALIDATION_POLICY["required_fee_source"]
                or not now-VALIDATION_POLICY["window_days"]*86400 <= d < settled < now
                or not d <= captured <= d+VALIDATION_POLICY["maximum_recording_delay_seconds"]
                or not captured < values["ends_at"] <= settled or captured >= now
                or not .5 <= values["base_score"] <= 1 or not 0 <= values["candidate_score"] <= 1
                or not 0 < values["all_in_cost"] < 1 or values["min_edge"] < 0 or values["won"] not in (0., 1.)):
            excluded["noncausal_or_ineligible"] += 1
            continue
        trained_before = finite(row.get("trained_before", row.get("training_cutoff")))
        fingerprint = row.get("training_fingerprint")
        if (trained_before is None or trained_before >= d
                or not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)):
            excluded["invalid_training_provenance"] += 1
            continue
        # Explicit replays can be useful diagnostics, but never gate evidence.
        if row.get("historical_replay") or row.get("backfilled"):
            excluded["historical_replay"] += 1
            continue
        candidate = {**values, "round_id":ident}
        if ident in unique:
            excluded["duplicate_market"] += 1
        if ident not in unique or d < unique[ident]["decision_at"]:
            unique[ident] = candidate
    rows = sorted(unique.values(), key=lambda r:(r["decision_at"], str(r["round_id"])))
    count = len(rows)
    span = rows[-1]["decision_at"]-rows[0]["decision_at"] if rows else 0.
    stats = {"model_version":MODEL_VERSION, "samples":count, "observation_hours":span/3600,
             "raw_brier":None, "candidate_brier":None, "brier_improvement":None,
             "paired_brier_standard_error":None, "paired_brier_lower90":None,
             "raw_selected":0, "candidate_selected":0, "raw_quote_utility":None,
             "candidate_quote_utility":None, "quote_utility_improvement":None,
             "raw_quote_drawdown":None, "candidate_quote_drawdown":None,
             "missed_winners":0, "avoided_losers":0, "prevented_losers":0, "added_winners":0, "added_losers":0,
             "removed_quote_sum":0., "added_quote_sum":0.,
             "training_cutoff":max((r["settled_at"] for r in rows),default=None),
             "excluded":dict(excluded), "policy":dict(VALIDATION_POLICY), "passed":False}
    base_select = lambda r:r["base_score"]-r["all_in_cost"]+1e-12 >= r["min_edge"]
    candidate_select = lambda r:r["candidate_score"]-r["all_in_cost"]+1e-12 >= r["min_edge"]
    if rows:
        raw_errors = [(r["base_score"]-r["won"])**2 for r in rows]
        candidate_errors = [(r["candidate_score"]-r["won"])**2 for r in rows]
        stats["raw_brier"], stats["candidate_brier"] = sum(raw_errors)/count, sum(candidate_errors)/count
        improvements = [raw-candidate for raw,candidate in zip(raw_errors,candidate_errors)]
        improvement = sum(improvements)/count
        stats["brier_improvement"] = improvement
        variance = sum((value-improvement)**2 for value in improvements)/(count-1) if count>1 else 0.
        error = math.sqrt(variance/count)
        stats["paired_brier_standard_error"] = error
        stats["paired_brier_lower90"] = improvement-1.645*error
        for prefix, choose in (("raw",base_select),("candidate",candidate_select)):
            selected = [r for r in rows if choose(r)]
            stats[prefix+"_selected"] = len(selected)
            stats[prefix+"_quote_utility"] = sum(r["won"]/r["all_in_cost"]-1 for r in selected)/count
            stats[prefix+"_quote_drawdown"] = _drawdown(rows,choose)
        stats["quote_utility_improvement"] = stats["candidate_quote_utility"]-stats["raw_quote_utility"]
        for r in rows:
            b, c = base_select(r), candidate_select(r)
            utility = r["won"]/r["all_in_cost"]-1
            if b and not c:
                stats["missed_winners" if r["won"] else "avoided_losers"] += 1
                stats["removed_quote_sum"] += utility
            elif c and not b:
                stats["added_winners" if r["won"] else "added_losers"] += 1
                stats["added_quote_sum"] += utility
    stats["prevented_losers"] = stats["avoided_losers"]
    if count < VALIDATION_POLICY["minimum_markets"] or span < 3600*VALIDATION_POLICY["minimum_observation_hours"]:
        state, reason = "BASELINE_SHADOW", "V3 前向对照尚未达到独立市场数或观察时长；原始基线继续开仓"
    elif min(stats["raw_selected"],stats["candidate_selected"]) < VALIDATION_POLICY["minimum_selected_each"]:
        state, reason = "BASELINE_SHADOW", "V3 可比较选单不足；原始基线继续开仓"
    elif (stats["brier_improvement"] < VALIDATION_POLICY["minimum_brier_improvement"]
            or stats["paired_brier_lower90"] <= 0 or stats["candidate_quote_utility"] <= 0
            or stats["quote_utility_improvement"] <= 1e-12
            or stats["candidate_quote_drawdown"] > stats["raw_quote_drawdown"]+1e-12):
        state, reason = "FALLBACK", "V3 预测、扣费报价收益或回撤未共同改善；原始基线继续开仓"
    else:
        state, reason = "VALIDATED", "V3 前向预测、扣费报价收益和回撤对照通过；启用有界修正并持续复核"
        stats["passed"] = True
    return {**stats,"state":state,"reason":reason}

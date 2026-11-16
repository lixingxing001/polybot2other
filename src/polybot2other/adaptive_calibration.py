"""Pure, time-causal calibration policy. Scores are estimates, not guarantees.

The constants are a versioned prior policy, not a search over historical profit.
One market contributes at most one observation, even across strategy ledgers.
"""
from __future__ import annotations

from collections import Counter
import math

MODEL_VERSION = "main-adaptive-calibration-v2"
HALF_LIFE_SECONDS = 72 * 3600
MAX_SAMPLE_AGE_SECONDS = 30 * 86400
MIN_ADJUSTMENT_SAMPLES = 8
MIN_EXACT_BUCKET_SAMPLES = 20
PRIOR_WEIGHT = 20.0
MAX_UPWARD_ADJUSTMENT = 0.04
MAX_DOWNWARD_ADJUSTMENT = 0.08
MAX_ADJUSTMENT = MAX_DOWNWARD_ADJUSTMENT
UNCERTAINTY_MULTIPLIER = 0.5
PROVENANCE_PRIORITY = {"optimized_raw": 0, "adaptive_v2_raw": 1, "shadow_v2_raw": 2}
POLICY = {"half_life_hours": 72, "max_age_days": 30,
          "minimum_effective_samples": MIN_ADJUSTMENT_SAMPLES, "prior_weight": PRIOR_WEIGHT,
          "max_upward_adjustment": MAX_UPWARD_ADJUSTMENT,
          "max_downward_adjustment": MAX_DOWNWARD_ADJUSTMENT,
          "uncertainty_multiplier": UNCERTAINTY_MULTIPLIER,
          "deduplication": "one_market_one_observation", "same_round_training": False}
VALIDATION_POLICY = {"window_days": 7, "minimum_markets": 40, "minimum_observation_hours": 24,
    "minimum_selected_each": 10, "minimum_brier_improvement": .005,
    "paired_brier_lower90_must_be_positive": True, "minimum_candidate_quote_utility": 0.0,
    "maximum_recording_delay_seconds": 30,
    "required_fee_source": "official-market",
    "utility_basis": "quote_cost_per_budget_per_opportunity_not_realized_pnl"}


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def score_bucket(score):
    return "<.66" if score < .66 else ".66-.72" if score < .72 else ".72-.78" if score < .78 else ".78+"


def price_bucket(price):
    price = round(price, 6)  # Fill-share rounding must not flip a decimal boundary.
    return "<.50" if price < .50 else ".50-.60" if price < .60 else ".60-.70" if price < .70 else ".70+"


def time_bucket(seconds):
    return "last60" if seconds <= 60 else "60-180" if seconds <= 180 else "180+"


def canonical_samples(samples, *, now, round_id=None):
    """Filter availability before deduplication; no source or outcome lookahead."""
    exclusions = Counter()
    eligible = []
    rows = list(samples)
    for raw in rows:
        row = dict(raw)
        provenance = row.get("provenance")
        if provenance not in PROVENANCE_PRIORITY:
            exclusions["legacy_or_unknown_provenance"] += 1
            continue
        score, settled, opened = (finite(row.get(k)) for k in ("base_score", "settled_at", "opened_at"))
        decision = finite(row.get("decision_at"))
        won, price = finite(row.get("won")), finite(row.get("decision_entry_price"))
        if price is None:
            price = finite(row.get("entry_price"))
        if (score is None or not .5 <= score <= 1 or settled is None or opened is None
                or won not in (0., 1.) or price is None or not 0 < price < 1
                or not row.get("round_id") or row.get("side") not in ("Up", "Down")
                or opened >= settled or (decision is not None and not decision <= opened < settled)
                or row.get("exclusion_reason")):
            exclusions["invalid_or_nonterminal_label"] += 1
            continue
        if settled >= now:
            exclusions["not_yet_settled"] += 1
            continue
        if row["round_id"] == round_id:
            exclusions["current_market"] += 1
            continue
        age = now - settled
        if age > MAX_SAMPLE_AGE_SECONDS:
            exclusions["expired"] += 1
            continue
        row.update(base_score=score, settled_at=settled, opened_at=opened,
                   won=int(won), entry_price=price, weight=2. ** (-age / HALF_LIFE_SECONDS))
        eligible.append(row)
    canonical = {}
    def priority(row):
        return (PROVENANCE_PRIORITY[row["provenance"]], row["opened_at"], str(row.get("source_key", "")))
    for row in eligible:
        old = canonical.get(row["round_id"])
        if old is None or priority(row) < priority(old):
            canonical[row["round_id"]] = row
    selected = sorted(canonical.values(), key=lambda r: (r["settled_at"], str(r["round_id"])))
    exclusions["duplicate_market"] = len(eligible) - len(selected)
    return selected, {"raw_samples": len(rows), "usable_samples": len(eligible),
                      "canonical_samples": len(selected), "unique_market_samples": len(selected),
                      "recent_effective_samples": sum(r["weight"] for r in selected),
                      "excluded_legacy_samples": exclusions["legacy_or_unknown_provenance"],
                      "exclusions": dict(exclusions)}


def _moments(rows):
    mass = sum(r["weight"] for r in rows)
    if mass <= 0:
        return 0., 0., 0., 0.
    residual = sum(r["weight"] * (r["won"] - r["base_score"]) for r in rows) / mass
    variance = sum(r["weight"] * ((r["won"] - r["base_score"]) - residual) ** 2 for r in rows) / mass
    kish = mass ** 2 / sum(r["weight"] ** 2 for r in rows)
    # Prior variance prevents identical wins/losses from claiming zero uncertainty.
    pooled_variance = (variance * mass + .25 * PRIOR_WEIGHT) / (mass + PRIOR_WEIGHT)
    standard_error = math.sqrt(pooled_variance / max(1., min(kish, mass)))
    return mass, residual, standard_error, kish


def calibrate_entry(samples, *, base_score, side, now, round_id, entry_price,
                    all_in_cost, min_edge, time_left_seconds=None):
    """Return a bounded score and cost decision using only prior settled markets.

    `base_score` is the unadjusted model's probability-like score for `side`.
    Its original side never changes; adverse calibration can take it below .5.
    Sparse/aged evidence explicitly falls back to the original score, without
    suspending the strategy or manufacturing a model warmup requirement.
    """
    base_score, entry_price, all_in_cost, min_edge, now = (
        finite(v) for v in (base_score, entry_price, all_in_cost, min_edge, now))
    if (base_score is None or not .5 <= base_score <= 1 or side not in ("Up", "Down")
            or entry_price is None or not 0 < entry_price < 1 or all_in_cost is None
            or all_in_cost < entry_price or min_edge is None or min_edge < 0 or now is None):
        raise ValueError("自适应校准输入或含费成本无效")
    available, counts = canonical_samples(samples, now=now, round_id=round_id)
    score_rows = [r for r in available if score_bucket(r["base_score"]) == score_bucket(base_score)]
    side_rows = [r for r in score_rows if r["side"] == side]
    price_rows = [r for r in side_rows if price_bucket(r["entry_price"]) == price_bucket(entry_price)]
    seconds = finite(time_left_seconds)
    exact_rows = [r for r in price_rows if seconds is not None
                  and finite(r.get("time_left_seconds")) is not None
                  and time_bucket(float(r["time_left_seconds"])) == time_bucket(seconds)]
    hierarchy = [(exact_rows, "side+score+price+time", 20),
                 (price_rows, "side+score+price", 16),
                 (side_rows, "side+score", 8), (score_rows, "score", 12)]
    chosen, bucket = [], "BASELINE"
    mass = residual = error = kish = 0.
    for rows, name, minimum in hierarchy:
        values = _moments(rows)
        if values[0] >= minimum:
            chosen, bucket = rows, name
            mass, residual, error, kish = values
            break
    posterior = base_score
    delta = 0.
    if chosen:
        reliability = mass / (mass + PRIOR_WEIGHT)
        posterior = base_score + reliability * residual
        delta = reliability * (residual - UNCERTAINTY_MULTIPLIER * error)
        delta = max(-MAX_DOWNWARD_ADJUSTMENT, min(MAX_UPWARD_ADJUSTMENT, delta))
    score = min(.99, max(.01, base_score + delta)) if chosen else base_score
    edge = score - all_in_cost
    allowed = edge + 1e-12 >= min_edge
    state = "BASELINE_FALLBACK" if not chosen else "ADAPTING" if mass >= 30 else "CALIBRATING"
    prefix = "有效同类样本不足，沿用原始分数" if not chosen else "近期独立市场样本校准"
    reason = f"{prefix}；含费优势 {edge:.4f} {'≥' if allowed else '<'} {min_edge:.4f}"
    return {"base_selected_score": base_score, "candidate_side": side,
            "adaptive_score": score, "adaptive_delta": score - base_score,
            "adaptive_posterior": min(.99, max(.01, posterior)),
            "adaptive_expected_edge": edge, "adaptive_effective_samples": mass,
            "adaptive_samples": len(chosen), "adaptive_wins": sum(r["won"] for r in chosen),
            "adaptive_standard_error": error, "adaptive_kish_samples": kish,
            "adaptive_training_cutoff": max((r["settled_at"] for r in chosen), default=None),
            "adaptive_state": state, "adaptive_bucket": bucket,
            "adaptive_allowed": allowed, "adaptive_reason": reason,
            "adaptive_model_version": MODEL_VERSION, "adaptive_observed_at": now,
            "decision_at": now, "round_id": round_id, "entry_price": entry_price,
            "all_in_cost": all_in_cost, "min_edge": min_edge, "time_left_seconds": seconds,
            **{"adaptive_" + key: value for key, value in counts.items()}}


def validate_candidate(observations, *, now, round_id=None):
    """Champion/challenger gate based only on genuinely forward shadow forecasts.

    Utility includes abstentions as zero on the SAME opportunity set. It uses
    quoted all-in cost and known terminal outcome, not fictional fills or PnL.
    Backfilled historical observations cannot promote the challenger.
    """
    unique = {}
    for row in observations:
        values = {k: finite(row.get(k)) for k in
                  ("decision_at", "settled_at", "recorded_at", "ends_at", "base_score", "candidate_score", "all_in_cost", "min_edge", "won")}
        if any(v is None for v in values.values()):
            continue
        d, s, captured = (values[k] for k in ("decision_at", "settled_at", "recorded_at"))
        ident = row.get("round_id")
        if (not ident or ident == round_id or row.get("label_source") != "polymarket_official"
                or row.get("model_version") != MODEL_VERSION
                or row.get("fee_source") != VALIDATION_POLICY["required_fee_source"]
                or not now - VALIDATION_POLICY["window_days"]*86400 <= d < s < now
                or not d <= captured <= d + VALIDATION_POLICY["maximum_recording_delay_seconds"]
                or not captured < values["ends_at"] <= s
                or captured >= now or not .5 <= values["base_score"] <= 1
                or not 0 <= values["candidate_score"] <= 1 or not 0 < values["all_in_cost"] < 1
                or values["min_edge"] < 0 or values["won"] not in (0., 1.)):
            continue
        candidate = {**values, "round_id": ident}
        if ident not in unique or d < unique[ident]["decision_at"]:
            unique[ident] = candidate
    rows = list(unique.values())
    count = len(rows)
    span = max((r["decision_at"] for r in rows),default=0) - min((r["decision_at"] for r in rows),default=0)
    stats = {"samples": count, "observation_hours": span/3600, "raw_brier": None,
             "candidate_brier": None, "brier_improvement": None, "raw_selected": 0,
             "paired_brier_standard_error":None,"paired_brier_lower90":None,
             "candidate_selected": 0, "raw_quote_utility": None, "candidate_quote_utility": None,
             "training_cutoff":max((r["settled_at"] for r in rows),default=None),
             "policy":dict(VALIDATION_POLICY), "passed":False}
    if rows:
        stats["raw_brier"] = sum((r["base_score"]-r["won"])**2 for r in rows)/count
        stats["candidate_brier"] = sum((r["candidate_score"]-r["won"])**2 for r in rows)/count
        stats["brier_improvement"] = stats["raw_brier"]-stats["candidate_brier"]
        differences = [(r["base_score"]-r["won"])**2-(r["candidate_score"]-r["won"])**2 for r in rows]
        mean = stats["brier_improvement"]
        variance = sum((d-mean)**2 for d in differences)/(count-1) if count>1 else 0.
        stats["paired_brier_standard_error"] = math.sqrt(variance/count)
        # A fixed approximate normal 90% bound; market dedup is not proof of IID.
        stats["paired_brier_lower90"] = mean-1.645*stats["paired_brier_standard_error"]
        for name, score_key in (("raw", "base_score"), ("candidate", "candidate_score")):
            selected = [r for r in rows if r[score_key]-r["all_in_cost"]+1e-12 >= r["min_edge"]]
            stats[name+"_selected"] = len(selected)
            stats[name+"_quote_utility"] = sum(r["won"]/r["all_in_cost"]-1 for r in selected)/count
    if count < VALIDATION_POLICY["minimum_markets"] or span < VALIDATION_POLICY["minimum_observation_hours"]*3600:
        state, reason = "BASELINE_SHADOW", "前向独立市场或观察时长不足，候选仅观察；继续原始基线开仓"
    elif min(stats["raw_selected"],stats["candidate_selected"]) < VALIDATION_POLICY["minimum_selected_each"]:
        state, reason = "BASELINE_SHADOW", "前向可比较候选不足，继续原始基线开仓"
    elif (stats["brier_improvement"] < VALIDATION_POLICY["minimum_brier_improvement"] or stats["paired_brier_lower90"] <= 0
            or stats["candidate_quote_utility"] <= VALIDATION_POLICY["minimum_candidate_quote_utility"]
            or stats["candidate_quote_utility"]+1e-12 < stats["raw_quote_utility"]):
        state, reason = "FALLBACK", "前向预测误差或报价成本收益未优于基线，继续原始基线开仓"
    else:
        state, reason = "VALIDATED", "前向预测与报价成本对照通过，启用有界校准并持续复核"
        stats["passed"] = True
    return {**stats,"state":state,"reason":reason}

"""衡准 V1 离线研究工具。只处理明确提供的数据，不生成行情、标签或收益。"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3

from .config import load_config, config_hash

DAY_MS = 86_400_000


def canonical_hash(value):
    """摘要不包含自身；调用者只传待摘要内容，拒绝 NaN/Infinity。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _epoch(value):
    if type(value) is not int or not 0 <= value <= 9_007_199_254_740_991:
        raise ValueError("UTC_EPOCH_MILLISECONDS_REQUIRED")
    return value


def market_key(row):
    key = (row.get("condition_id"), row.get("rule_version"))
    if not all(isinstance(x, str) and x for x in key):
        raise ValueError("MARKET_GROUP_ID_REQUIRED")
    return key


@dataclass(frozen=True)
class TimeSplit:
    training_start_at: int
    training_end_at: int
    validation_start_at: int
    validation_end_at: int
    test_start_at: int
    test_end_at: int

    def validate(self):
        stamps = tuple(asdict(self).values())
        if any(_epoch(t) % DAY_MS for t in stamps):
            raise ValueError("FULL_UTC_WINDOWS_REQUIRED")
        if (self.training_end_at-self.training_start_at != 28*DAY_MS
                or self.validation_end_at-self.validation_start_at != 7*DAY_MS
                or self.test_end_at-self.test_start_at != 7*DAY_MS
                or self.training_end_at != self.validation_start_at
                or self.validation_end_at != self.test_start_at):
            raise ValueError("SPLIT_28_7_7_REQUIRED")
        return self


def walk_forward_windows(first_day_at, end_at):
    """完整的 28/7/7 窗口，每次前移七天；不使用不足跨度的尾窗。"""
    if _epoch(first_day_at) % DAY_MS or _epoch(end_at) % DAY_MS:
        raise ValueError("FULL_UTC_WINDOWS_REQUIRED")
    windows = []
    start = first_day_at
    while start+42*DAY_MS <= end_at:
        windows.append(TimeSplit(start, start+28*DAY_MS, start+28*DAY_MS,
                                 start+35*DAY_MS, start+35*DAY_MS, start+42*DAY_MS))
        start += 7*DAY_MS
    return windows


def partition_rows(rows, split, *, publication_at=None, evaluation_at=None):
    """按真实开盘分组，而不是按方向、landmark 或结果随机分割。

    输入字段与 calibration.py 相同。晚到标签整组排除，并列出原因；尚未
    发布模型的测试时段保留缺席证据，不伪造当时可用的预测。
    """
    split.validate()
    publication_at = split.test_start_at if publication_at is None else _epoch(publication_at)
    evaluation_at = split.test_end_at if evaluation_at is None else _epoch(evaluation_at)
    if publication_at < split.validation_end_at:
        raise ValueError("MODEL_PUBLICATION_BEFORE_VALIDATION_END")
    grouped = defaultdict(list)
    for row in rows:
        grouped[market_key(row)].append(dict(row))
    result = {"train": [], "validation": [], "test": [], "excluded": []}
    for key, group in sorted(grouped.items()):
        starts = {_epoch(row["window_open_at"]) for row in group}
        if len(starts) != 1:
            raise ValueError("MARKET_OPEN_TIME_CONFLICT")
        opened = starts.pop()
        stage = ("train" if split.training_start_at <= opened < split.training_end_at else
                 "validation" if split.validation_start_at <= opened < split.validation_end_at else
                 "test" if split.test_start_at <= opened < split.test_end_at else None)
        reason = None
        if stage is None:
            reason = "OUTSIDE_WINDOW"
        else:
            end = {"train": split.training_end_at, "validation": split.validation_end_at,
                   "test": split.test_end_at}[stage]
            cutoff = {"train": split.training_end_at, "validation": publication_at,
                      "test": evaluation_at}[stage]
            labels = {row.get("y") for row in group}
            if any(type(row.get("y")) is not int or row["y"] not in (0, 1) for row in group):
                reason = "NON_BINARY_OR_MISSING_LABEL"
            elif len(labels) != 1:
                raise ValueError("MARKET_LABEL_CONFLICT")
            elif any(_epoch(row["decision_at"]) < opened or row["decision_at"] >= end for row in group):
                reason = "FEATURE_OUTSIDE_PARTITION"
            elif any(_epoch(row["label_available_at"]) > cutoff or
                     _epoch(row.get("label_verified_at", row["label_available_at"])) > cutoff for row in group):
                reason = "LABEL_NOT_YET_AVAILABLE"
            elif stage == "test" and any(row["decision_at"] < publication_at for row in group):
                reason = "MODEL_NOT_YET_PUBLISHED"
        if reason:
            result["excluded"].append({"condition_id": key[0], "rule_version": key[1],
                                       "partition": stage, "row_count": len(group), "reason": reason})
        else:
            weight = 1./len(group)
            result[stage].extend({**row, "market_weight": weight} for row in sorted(group, key=lambda x: x["decision_at"]))
    result["split"] = asdict(split)
    result["publication_at"] = publication_at
    result["evaluation_at"] = evaluation_at
    result["manifest_hash"] = canonical_hash(result)
    return result


def probability_metrics(rows, *, probability_key="p_fit_up", baseline_key="p_market_up", clip=1e-12):
    """同一有效 landmark 集合，每市场总权重一；无效预测不能静默删除。"""
    rows = list(rows)
    if not rows:
        return {"status": "INSUFFICIENT", "independent_markets": 0, "landmark_rows": 0}
    counts = Counter(market_key(row) for row in rows)
    labels = {}
    totals = {"model": {"brier": 0., "log_loss": 0.},
              "market": {"brier": 0., "log_loss": 0.}}
    if not 0 < clip < .5:
        raise ValueError("INVALID_NUMERIC_CLIP")
    for row in rows:
        key, y = market_key(row), row.get("y")
        if type(y) is not int or y not in (0, 1):
            raise ValueError("BINARY_LABEL_REQUIRED")
        if key in labels and labels[key] != y:
            raise ValueError("MARKET_LABEL_CONFLICT")
        labels[key] = y
        for target, field in (("model", probability_key), ("market", baseline_key)):
            p = row.get(field)
            if isinstance(p, bool) or not isinstance(p, (float, int)) or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError("INVALID_PREDICTION: "+field)
            weight = 1./counts[key]
            safe = max(clip, min(1-clip, p))
            totals[target]["brier"] += weight*(p-y)**2
            totals[target]["log_loss"] += -weight*(y*math.log(safe)+(1-y)*math.log1p(-safe))
    for metrics in totals.values():
        for field in metrics:
            metrics[field] /= len(counts)
    return {"status": "COMPUTED", "independent_markets": len(counts), "landmark_rows": len(rows),
            "up_markets": sum(labels.values()), "down_markets": len(labels)-sum(labels.values()),
            "total_market_weight": float(len(counts)), "evaluation_scope": "COMMON_VALID_LANDMARKS_MARKET_WEIGHT_ONE",
            **totals}


def quantile_linear(values, q):
    if not values or not 0 <= q <= 1:
        raise ValueError("QUANTILE_INPUT_INVALID")
    values = sorted(values)
    index = (len(values)-1)*q
    lower = int(math.floor(index)); upper = int(math.ceil(index))
    return values[lower]+(values[upper]-values[lower])*(index-lower)


def probability_metric_intervals(rows, *, repetitions=20000, block_days=3, seed=20261106):
    """概率诊断区间：按已登记的 UTC 日成对循环块方法重采样。

    §19 要求概率指标置信范围但未唯一指定算法；这里显式声明沿用研究的
    20k/3天/固定种子方法。每次重采样仍按市场权重，不把日权重当市场权重。
    区间只作诊断，不改变 G2 的两项点估计 0.01 容差判定。
    """
    if repetitions < 20000 or block_days not in (1,3,7) or seed != 20261106:
        raise ValueError("UNREGISTERED_PROBABILITY_INTERVAL_RECIPE")
    rows=list(rows); point=probability_metrics(rows)
    if not rows:return {"status":"INSUFFICIENT","reason":"NO_PROBABILITY_ROWS"}
    by_day=defaultdict(list)
    for row in rows:by_day[_epoch(row["window_open_at"])//DAY_MS].append(row)
    day_keys=sorted(by_day)
    if day_keys != list(range(day_keys[0],day_keys[-1]+1)):
        return {"status":"INSUFFICIENT","reason":"INCOMPLETE_UTC_DAY_SEQUENCE","point_metrics":point}
    daily=[probability_metrics(by_day[key]) for key in day_keys]
    rng=random.Random(seed); distributions={key:[] for key in ("model_brier","model_log_loss","market_brier","market_log_loss","delta_brier","delta_log_loss")}
    for _ in range(repetitions):
        indices=circular_block_indices(len(daily),block_days,rng)
        count=sum(daily[j]["independent_markets"] for j in indices)
        result={}
        for target in ("model","market"):
            for metric in ("brier","log_loss"):
                key=target+"_"+metric
                result[key]=math.fsum(daily[j][target][metric]*daily[j]["independent_markets"] for j in indices)/count
                distributions[key].append(result[key])
        for metric in ("brier","log_loss"):
            distributions["delta_"+metric].append(result["model_"+metric]-result["market_"+metric])
    return {"status":"COMPUTED","method":"PAIRED_COMPLETE_UTC_DAY_CIRCULAR_BLOCKS_MARKET_WEIGHT_ONE",
            "method_scope":"DECLARED_DIAGNOSTIC_NOT_G2_THRESHOLD","block_days":block_days,
            "repetitions":repetitions,"seed":seed,"complete_observed_days":len(daily),
            "point_metrics":point,"interval_95":{key:[quantile_linear(values,.025),quantile_linear(values,.975)] for key,values in distributions.items()}}


def circular_block_indices(days, block_days, rng):
    if type(days) is not int or type(block_days) is not int or days <= 0 or block_days <= 0:
        raise ValueError("BOOTSTRAP_DIMENSIONS_INVALID")
    indices = []
    while len(indices) < days:
        start = rng.randrange(days)
        indices.extend((start+j) % days for j in range(block_days))
    return indices[:days]


def paired_daily_bootstrap(strategy_days, baseline_days=None, *, candidate_count=1,
                           registered=True, candidate_history_known=True,
                           block_days=3, repetitions=20_000, seed=20261106,
                           conservative_bounds=False):
    """UTC 完整日循环移动块；策略与基准使用完全相同的抽样索引。

    每行 day 为 ISO 日期，net_return_usd 为已扣外部资金流的十进制字符串。
    无精确日末估值时保留 lower_return_usd/upper_return_usd，必须显式请求
    保守边界检验，绝不删除缺估值日期。重排仅估计均值，不重建账户或回撤。
    """
    if type(candidate_count) is not int or not 1 <= candidate_count <= 12:
        raise ValueError("CANDIDATE_COUNT_INVALID")
    if type(repetitions) is not int or repetitions < 20_000:
        raise ValueError("BOOTSTRAP_REPETITIONS_BELOW_SPEC")
    if block_days not in (1, 3, 7) or seed != 20261106:
        raise ValueError("UNREGISTERED_BOOTSTRAP_RECIPE")
    def index_rows(rows):
        indexed = {}
        for row in rows:
            day = date.fromisoformat(row["day"])
            if day.isoformat() != row["day"] or day in indexed:
                raise ValueError("DUPLICATE_OR_INVALID_UTC_DAY")
            indexed[day] = dict(row)
        return indexed
    strategy = index_rows(strategy_days)
    baseline = index_rows(baseline_days) if baseline_days is not None else None
    if not strategy:
        return {"status": "INSUFFICIENT", "reasons": ["NO_DAILY_RETURNS"]}
    days = sorted(set(strategy) | (set(baseline) if baseline is not None else set()))
    expected = [days[0]+timedelta(days=i) for i in range((days[-1]-days[0]).days+1)]
    missing = [d.isoformat() for d in expected if d not in strategy or (baseline is not None and d not in baseline)]
    if missing:
        return {"status": "INSUFFICIENT", "reasons": ["INCOMPLETE_DAILY_SEQUENCE"], "missing_days": missing}
    a, b, uncertain = [], [], []
    def value(row, field):
        raw = row.get(field)
        if raw is None: return None
        if not isinstance(raw, str): raise ValueError("RETURN_DECIMAL_STRING_REQUIRED")
        number = Decimal(raw)
        if not number.is_finite(): raise ValueError("RETURN_NOT_FINITE")
        converted = float(number)
        if not math.isfinite(converted): raise ValueError("STATISTICAL_RETURN_OVERFLOW")
        return converted
    for day in expected:
        sa = value(strategy[day], "net_return_usd")
        sb = value(baseline[day], "net_return_usd") if baseline is not None else 0.
        if sa is None or sb is None:
            uncertain.append(day.isoformat())
            if conservative_bounds:
                for candidate in (strategy[day], baseline[day] if baseline is not None else {}):
                    lo = value(candidate, "lower_return_usd"); hi = value(candidate, "upper_return_usd")
                    if lo is not None and hi is not None and lo > hi:
                        raise ValueError("VALUATION_INTERVAL_REVERSED")
                sa = value(strategy[day], "lower_return_usd") if sa is None else sa
                sb = value(baseline[day], "upper_return_usd") if baseline is not None and sb is None else sb
        if sa is None or sb is None:
            return {"status": "INSUFFICIENT", "reasons": ["DAILY_VALUATION_MISSING"],
                    "valuation_gap_days": uncertain, "input_days": [d.isoformat() for d in expected]}
        a.append(sa); b.append(sb)
    rng = random.Random(seed); net_means = []; paired_means = []; digest = hashlib.sha256()
    count = len(a)
    for _ in range(repetitions):
        indices = circular_block_indices(count, block_days, rng)
        digest.update(json.dumps(indices, separators=(",", ":")).encode()+b"\n")
        net_means.append(math.fsum(a[j] for j in indices)/count)
        if baseline is not None:
            paired_means.append(math.fsum(a[j]-b[j] for j in indices)/count)
    alpha = .05/(2*candidate_count)
    exploratory = not registered or not candidate_history_known
    def summary(distribution, point):
        lower = quantile_linear(distribution, alpha)
        return {"point_estimate": point, "one_sided_lower": lower,
                "interval_95": [quantile_linear(distribution, .025), quantile_linear(distribution, .975)],
                "test_status": "EXPLORATORY" if exploratory else "PASS" if lower > 0 else "FAIL"}
    return {"status": "EXPLORATORY" if exploratory else "COMPUTED", "days": count,
            "day_start": expected[0].isoformat(), "day_end": expected[-1].isoformat(),
            "block_days": block_days, "repetitions": repetitions, "seed": seed,
            "registered_candidate_count": candidate_count, "one_sided_alpha": alpha,
            "sampling": "CIRCULAR_MOVING_BLOCK_PAIRED_INDICES", "index_sha256": digest.hexdigest(),
            "valuation_gap_days": uncertain, "conservative_bounds_used": bool(uncertain),
            "net": summary(net_means, math.fsum(a)/count),
            "improvement": summary(paired_means, math.fsum(x-y for x,y in zip(a,b))/count) if baseline is not None else None,
            "baseline_status": "AVAILABLE" if baseline is not None else "BASELINE_UNAVAILABLE",
            "not_an_account_replay": True, "drawdown_from_resampling": None}


def bootstrap_sensitivity(strategy_days, baseline_days=None, **kwargs):
    return {str(block): paired_daily_bootstrap(strategy_days, baseline_days, block_days=block, **kwargs)
            for block in (1, 3, 7)}


def summarize_channels(outcomes):
    """经济空间分别汇总，影子收益不加进 $100 资金账户。金额全程 Decimal。"""
    groups = defaultdict(list)
    for row in outcomes:
        scope = row.get("ledger_scope")
        if scope not in ("SIGNAL_SHADOW", "PAPER_PORTFOLIO"):
            raise ValueError("LEDGER_SCOPE_INVALID")
        space = tuple(row.get(k) for k in ("mode", "portfolio_run_id", "scenario_id", "ledger_scope"))
        if space[0] not in ("REPLAY", "PAPER") or not all(isinstance(x,str) and x for x in space):
            raise ValueError("ECONOMIC_SPACE_INVALID")
        groups[space].append(dict(row))
    result = []
    for space, rows in sorted(groups.items()):
        seen = set(); total = Decimal(0); unresolved = 0; uncertain = 0
        for row in rows:
            key = market_key(row)
            if key in seen: raise ValueError("DUPLICATE_MARKET_OUTCOME")
            seen.add(key)
            if row.get("economic_status") not in ("RESOLVED_LOSS", "CLAIM_PENDING", "CLOSED_CLAIMED"):
                unresolved += 1
                if row.get("economic_status") in ("UNKNOWN", "SUBMITTING", "MATCHED", "RETRYING"): uncertain += 1
                continue
            raw = row.get("realized_pnl_usd")
            if not isinstance(raw,str): raise ValueError("PNL_DECIMAL_STRING_REQUIRED")
            amount = Decimal(raw)
            if not amount.is_finite(): raise ValueError("PNL_NOT_FINITE")
            total += amount
        result.append({**dict(zip(("mode","portfolio_run_id","scenario_id","ledger_scope"),space)),
                       "independent_markets": len(seen), "unresolved_markets": unresolved,
                       "uncertain_markets": uncertain, "result_status": "PENDING_TAIL" if unresolved else "ACCOUNTED",
                       "realized_pnl_usd" if space[3] == "PAPER_PORTFOLIO" else "hypothetical_pnl_usd": str(total),
                       "is_funded_account_return": space[3] == "PAPER_PORTFOLIO"})
    return {"economic_spaces": result, "combined_pnl_usd": None,
            "note": "独立账户、影子通道及不同情景不得相加为共享钱包收益。"}


def concentration_report(daily_returns, market_returns):
    """保留全部日期/市场，集中度只描述收益来源，不据此筛样本。"""
    def numbers(rows):
        values = [Decimal(x) for x in rows]
        if any(not x.is_finite() for x in values): raise ValueError("RETURN_NOT_FINITE")
        return values
    days = numbers(daily_returns); markets = numbers(market_returns)
    positive_days = sorted((x for x in days if x > 0),reverse=True)
    positive_markets = sorted((x for x in markets if x > 0),reverse=True)
    return {"largest_day_share_of_positive_returns": float(positive_days[0]/sum(positive_days)) if positive_days else None,
            "top_five_markets_share_of_positive_returns": float(sum(positive_markets[:5])/sum(positive_markets)) if positive_markets else None,
            "day_count": len(days), "market_count": len(markets),
            "nontrading_or_zero_days": sum(x == 0 for x in days)}


def assess_research_gates(evidence):
    """分层判定已提供且已验证的证据；没有数据时明确 INSUFFICIENT。

    此函数不能替调用者核实真实来源。counts/manifests 必须由相应数据和模型
    校验器生成，任何 absent/unknown 证据都会阻断通过。
    """
    c = load_config(); r = c["research"]; cal = c["calibration"]
    def gate(missing, failures=(), special=None):
        return {"status": "INSUFFICIENT" if missing else special or ("FAIL" if failures else "PASS"),
                "missing": list(missing), "failures": list(failures)}
    result = {}
    for name in ("G0", "G1"):
        test = evidence.get(name, {})
        result[name] = gate([] if test.get("executed") and test.get("evidence_verified") else ["ACTUAL_TEST_EVIDENCE_REQUIRED"],
                            ["TEST_FAILURES"] if test.get("failed", 0) else [])
    model = evidence.get("model", {}); missing = []; failures = []
    for field in ("artifact_verified", "manifests_verified", "coverage_reported", "probability_intervals_reported"):
        if model.get(field) is not True: missing.append(field.upper())
    minimums = {"train_markets": cal["min_train_markets"], "complete_train_days": cal["min_train_days"],
                "up_train_markets": cal["min_markets_each_outcome"], "down_train_markets": cal["min_markets_each_outcome"],
                "validation_markets": cal["min_validation_markets"], "bootstrap_success": cal["bootstrap_min_success"]}
    for key, minimum in minimums.items():
        if type(model.get(key)) is not int or model[key] < minimum: missing.append(key.upper())
    if model.get("train_window_days") != 28 or model.get("validation_window_days") != 7:
        missing.append("COMPLETE_28_7_WINDOWS")
    metrics = model.get("validation_metrics", {})
    for key in ("brier", "log_loss"):
        a = metrics.get("model", {}).get(key); b = metrics.get("market", {}).get(key)
        if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (a,b)):
            missing.append("VALIDATION_"+key.upper())
        elif a > b+float(cal["validation_score_tolerance"]): failures.append("VALIDATION_"+key.upper()+"_DEGRADED")
    result["G2"] = gate(missing, failures)
    study = evidence.get("study", {}); missing = []; failures = []
    for name in ("G0", "G1", "G2"):
        if result[name]["status"] != "PASS": missing.append(name+"_NOT_PASSED")
    for field in ("manifests_verified", "continuous_accounts_verified", "complete_daily_sequence", "tails_accounted", "concentration_reported"):
        if study.get(field) is not True: missing.append(field.upper())
    for key, minimum in (("nonoverlapping_test_weeks",r["required_test_weeks"]),
                         ("shadow_filled_markets",r["minimum_shadow_filled_markets"]),
                         ("portfolio_filled_markets",r["minimum_portfolio_filled_markets"])):
        if type(study.get(key)) is not int or study[key] < minimum: missing.append(key.upper())
    if study.get("unexplained_reconciliation_differences") is None: missing.append("RECONCILIATION_REPORT")
    elif study["unexplained_reconciliation_differences"] != 0: failures.append("RECONCILIATION_DIFFERENCE")
    drawdown = study.get("maximum_drawdown")
    if not isinstance(drawdown,(int,float)) or not math.isfinite(drawdown): missing.append("DRAWDOWN_REPORT")
    elif drawdown > float(c["risk"]["maximum_high_water_drawdown"]): failures.append("DRAWDOWN_BOUNDARY_EXCEEDED")
    statistics = study.get("bootstrap", {})
    if set(statistics) != {"1","3","7"}: missing.append("BOOTSTRAP_SENSITIVITY_1_3_7")
    main = statistics.get("3", {})
    for block in ("1","3","7"):
        sample = statistics.get(block,{})
        candidates = sample.get("registered_candidate_count")
        alpha = sample.get("one_sided_alpha")
        valid = (sample.get("status") == "COMPUTED" and sample.get("block_days") == int(block)
                 and type(sample.get("days")) is int and sample["days"] >= r["required_test_weeks"]*7
                 and type(sample.get("repetitions")) is int and sample["repetitions"] >= 20000
                 and sample.get("seed") == 20261106 and type(candidates) is int and 1 <= candidates <= 12
                 and isinstance(alpha,(int,float)) and math.isfinite(alpha)
                 and abs(alpha-.05/(2*candidates)) < 1e-14)
        if not valid: missing.append("REGISTERED_VALID_BOOTSTRAP_L"+block)
    lower = main.get("net", {}).get("one_sided_lower")
    if not isinstance(lower,(int,float)) or not math.isfinite(lower): missing.append("NET_RETURN_TEST")
    elif lower <= 0: failures.append("NET_RETURN_LOWER_BOUND_NONPOSITIVE")
    scenarios = study.get("scenarios", {}); stress_negative = False
    if set(scenarios) != {"S1","S2","S3"}: missing.append("ALL_THREE_SCENARIOS_REQUIRED")
    for name in ("S1","S2","S3"):
        scenario = scenarios.get(name,{})
        if scenario.get("full_replay") is not True or scenario.get("manifest_verified") is not True:
            missing.append(name+"_COMPLETE_REPLAY")
        pnl = scenario.get("net_pnl_usd")
        if pnl is None: missing.append(name+"_NET_PNL")
        elif not Decimal(str(pnl)).is_finite(): missing.append(name+"_FINITE_PNL")
        elif name != "S1" and Decimal(str(pnl)) < 0: stress_negative = True
    tick = study.get("one_extra_tick_cost_pnl_usd")
    if tick is None: missing.append("ONE_TICK_COST_SENSITIVITY")
    elif not Decimal(str(tick)).is_finite(): missing.append("FINITE_ONE_TICK_COST")
    elif Decimal(str(tick)) < 0: failures.append("ONE_TICK_COST_NEGATIVE")
    if stress_negative: failures.append("STRESS_EXECUTION_NEGATIVE")
    base_pnl=scenarios.get("S1",{}).get("net_pnl_usd")
    fragile=stress_negative and base_pnl is not None and Decimal(str(base_pnl)).is_finite() and Decimal(str(base_pnl))>0
    result["G3"] = gate(missing, failures, "EXECUTION_FRAGILE" if fragile and not missing else None)
    missing = []; failures = []
    if result["G3"]["status"] != "PASS": missing.append("G3_NOT_PASSED")
    if evidence.get("baseline",{}).get("frozen_source_verified") is not True: missing.append("BASELINE_UNAVAILABLE")
    if evidence.get("baseline",{}).get("unified_risk_supports_improvement") is not True: missing.append("UNIFIED_RISK_COMPARISON")
    improved = main.get("improvement") or {}
    lower = improved.get("one_sided_lower")
    if not isinstance(lower,(int,float)) or not math.isfinite(lower): missing.append("PAIRED_BASELINE_TEST")
    elif lower <= 0: failures.append("IMPROVEMENT_LOWER_BOUND_NONPOSITIVE")
    result["G4"] = gate(missing, failures)
    result["execution_realism"] = {"status": "VERIFIED" if evidence.get("execution",{}).get("applicable_empirical_evidence_verified") is True else "ASSUMED",
                                   "live_execution_enabled": False}
    return result


class ExperimentRegistry:
    """不可覆盖的预登记与报告；H2 必须使用新 run_id，不能改写 H1 成绩。"""
    def __init__(self, path):
        self.conn = sqlite3.connect(Path(path))
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""CREATE TABLE IF NOT EXISTS research_runs(run_id TEXT PRIMARY KEY,manifest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_reports(report_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(run_id),report TEXT NOT NULL);""")

    def close(self): self.conn.close()

    def register(self, run_id, config, data_manifest, *, strategy_hash, parent_run_id=None):
        if (not isinstance(run_id,str) or not run_id or not isinstance(strategy_hash,str) or len(strategy_hash) != 64
                or any(ch not in "0123456789abcdef" for ch in strategy_hash)):
            raise ValueError("REGISTRATION_IDENTITY_REQUIRED")
        manifest = {"run_id":run_id,"parent_run_id":parent_run_id,"config_hash":config_hash(config),
                    "data_manifest_hash":canonical_hash(data_manifest),"strategy_hash":strategy_hash,
                    "scenario_ids":["S1","S2","S3"],"seed":config["runtime"]["seed"]}
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            old = self.conn.execute("SELECT manifest FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
            if old and old[0] != encoded: raise ValueError("EXPERIMENT_IMMUTABLE_NEW_RUN_REQUIRED")
            self.conn.execute("INSERT OR IGNORE INTO research_runs VALUES(?,?)", (run_id,encoded))
        return manifest

    def save_report(self, report_id, run_id, report):
        if not isinstance(report_id,str) or not report_id: raise ValueError("REPORT_ID_REQUIRED")
        row = self.conn.execute("SELECT manifest FROM research_runs WHERE run_id=?",(run_id,)).fetchone()
        if not row: raise ValueError("EXPERIMENT_NOT_REGISTERED")
        manifest = json.loads(row[0])
        if report.get("config_hash") != manifest["config_hash"] or report.get("data_manifest_hash") != manifest["data_manifest_hash"]:
            raise ValueError("REPORT_FROZEN_INPUT_MISMATCH")
        encoded = json.dumps(report,sort_keys=True,separators=(",", ":"),allow_nan=False)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            old = self.conn.execute("SELECT run_id,report FROM research_reports WHERE report_id=?",(report_id,)).fetchone()
            if old and tuple(old) != (run_id,encoded): raise ValueError("RESEARCH_REPORT_IMMUTABLE")
            self.conn.execute("INSERT OR IGNORE INTO research_reports VALUES(?,?,?)",(report_id,run_id,encoded))
        return canonical_hash(report)

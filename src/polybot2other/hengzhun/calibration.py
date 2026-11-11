"""四参数逻辑校准、按日 bootstrap 与严格发布门槛；只做离线数学。

训练函数接受已记录的行与覆盖证据。没有真实训练集时返回 INSUFFICIENT，
不生成零系数模型，不把人工测试样例视为策略研究结果。
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import time

from .config import load_config, config_hash, STRATEGY_ID, SPEC_VERSION
from .model import ModelUnavailable, linear_quantile, conservative_guards
from jsonschema.exceptions import ValidationError

DAY_MS = 86_400_000
FEATURE_ORDER = ["u", "v", "u_times_h"]


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value):
        raise ModelUnavailable("NONFINITE_PARAMETER")
    return float(value)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _sigmoid(value):
    if value >= 0:
        return 1/(1+math.exp(-value))
    e = math.exp(value)
    return e/(1+e)


def raw_features(p_raw_up, p_market_up, tau_wall_seconds, clip=.0001):
    raw, market, tau = map(_finite, (p_raw_up,p_market_up,tau_wall_seconds))
    if not 0 <= raw <= 1 or not 0 <= market <= 1 or not 0 < clip < .5:
        raise ModelUnavailable("MODEL_FEATURE_INVALID")
    def logit(p):
        p = min(1-clip,max(clip,p))
        return math.log(p/(1-p))
    u,v = logit(raw),logit(market)
    return (u,v,u*((tau-60)/60))


def market_probability(bid_up,ask_up,bid_down,ask_down):
    bu,au,bd,ad = map(_finite,(bid_up,ask_up,bid_down,ask_down))
    if not 0 < bu <= au < 1 or not 0 < bd <= ad < 1:
        raise ModelUnavailable("BOOK_INVALID")
    up,down = (bu+au)/2,(bd+ad)/2
    return up/(up+down)


def weighted_transform(features,weights,scale_floor=1e-12):
    if not features or len(features) != len(weights) or any(len(row) != 3 for row in features):
        raise ModelUnavailable("INVALID_TRANSFORM")
    features = [tuple(map(_finite,row)) for row in features]
    weights = [_finite(w) for w in weights]
    if any(w <= 0 for w in weights): raise ModelUnavailable("INVALID_WEIGHTS")
    total = math.fsum(weights)
    means = [math.fsum(w*x[j] for x,w in zip(features,weights))/total for j in range(3)]
    scales = [math.sqrt(math.fsum(w*(x[j]-means[j])**2 for x,w in zip(features,weights))/total) for j in range(3)]
    degenerate = [s < scale_floor for s in scales]
    scales = [1. if s < scale_floor else s for s in scales]
    return {"method":"MARKET_WEIGHTED_TRAIN_STANDARDIZATION","mean":means,"scale":scales,
            "degenerate_features":degenerate,"scale_floor":scale_floor,"degenerate_scale_replacement":1.}


def _standardize(features,transform):
    if len(transform.get("mean",[])) != 3 or len(transform.get("scale",[])) != 3:
        raise ModelUnavailable("INVALID_TRANSFORM")
    mean = list(map(_finite,transform["mean"]));scale = list(map(_finite,transform["scale"]))
    if any(s <= 0 for s in scale): raise ModelUnavailable("INVALID_TRANSFORM")
    return tuple((_finite(x)-m)/s for x,m,s in zip(features,mean,scale))


def _solve(matrix, vector):
    n = len(vector);a = [list(row)+[v] for row,v in zip(matrix,vector)]
    for col in range(n):
        pivot = max(range(col,n),key=lambda row:abs(a[row][col]))
        a[col],a[pivot] = a[pivot],a[col]
        if abs(a[col][col]) < 1e-15: raise ModelUnavailable("OPTIMIZER_NONCONVERGENCE","singular Hessian")
        divisor = a[col][col];a[col] = [x/divisor for x in a[col]]
        for row in range(n):
            if row != col:
                multiplier = a[row][col];a[row] = [x-multiplier*y for x,y in zip(a[row],a[col])]
    return [row[-1] for row in a]


def fit_logistic(features,labels,weights=None,*,ridge_lambda=.01,max_iterations=1000,tolerance=1e-8):
    """确定性 Newton + 回溯；平均 log loss，截距不惩罚，系数无符号限制。"""
    if len(features) != len(labels) or not features:
        raise ModelUnavailable("DATA_INTEGRITY_ERROR")
    if any(type(y) is not int or y not in (0,1) for y in labels) or len(set(labels)) != 2:
        raise ModelUnavailable("SINGLE_CLASS")
    if not 0 < ridge_lambda or not 0 < tolerance or not 1 <= max_iterations <= 1000:
        raise ModelUnavailable("OPTIMIZER_CONFIGURATION")
    weights = list(weights) if weights is not None else [1.]*len(features)
    transform = weighted_transform(features,weights)
    x = [(1.,)+_standardize(row,transform) for row in features]
    weight_sum = math.fsum(weights)
    normalized = [w/weight_sum for w in weights]
    label_mean = math.fsum(w*y for w,y in zip(normalized,labels))
    beta = [math.log(label_mean/(1-label_mean)),0.,0.,0.]
    def evaluate(coef,derivatives=False):
        objective = 0.;gradient = [0.]*4;hessian = [[0.]*4 for _ in range(4)]
        for row,y,w in zip(x,labels,normalized):
            score = math.fsum(a*b for a,b in zip(row,coef));p = _sigmoid(score)
            objective += w*(max(score,0)+math.log1p(math.exp(-abs(score)))-y*score)
            if derivatives:
                for j in range(4):
                    gradient[j] += w*(p-y)*row[j]
                    for k in range(4):hessian[j][k] += w*p*(1-p)*row[j]*row[k]
        loss = objective
        for j in range(1,4):
            objective += ridge_lambda*coef[j]**2/2
            gradient[j] += ridge_lambda*coef[j];hessian[j][j] += ridge_lambda
        if not math.isfinite(objective):raise ModelUnavailable("NONFINITE_PARAMETER")
        return objective,loss,gradient,hessian
    converged = False
    for iteration in range(max_iterations+1):
        objective,loss,gradient,hessian = evaluate(beta,True)
        if max(abs(g) for g in gradient) <= tolerance:
            converged = True;break
        if iteration == max_iterations:break
        delta = _solve(hessian,gradient)
        descent = math.fsum(g*d for g,d in zip(gradient,delta));step = 1.
        for _ in range(60):
            candidate = [b-step*d for b,d in zip(beta,delta)]
            if evaluate(candidate)[0] <= objective-1e-4*step*descent:
                beta = candidate;break
            step /= 2
        else:raise ModelUnavailable("OPTIMIZER_NONCONVERGENCE","line search")
    if not converged:raise ModelUnavailable("OPTIMIZER_NONCONVERGENCE")
    return {"transform":transform,"parameters":{"intercept":beta[0],"coefficients":beta[1:]},
            "optimizer_result":{"status":"CONVERGED","iterations":iteration,
                "weighted_training_log_loss":loss,"penalized_objective":objective}}


def predict_fit(fit, features, *, max_abs_z=5.):
    if fit.get("optimizer_result",{}).get("status") != "CONVERGED":
        raise ModelUnavailable("MODEL_UNFITTED")
    z = _standardize(features,fit["transform"])
    if any(not math.isfinite(v) or abs(v) > max_abs_z for v in z):
        raise ModelUnavailable("MODEL_OUT_OF_DOMAIN")
    p = fit.get("parameters",{});coef = p.get("coefficients",[])
    if len(coef) != 3: raise ModelUnavailable("NONFINITE_PARAMETER")
    score = _finite(p.get("intercept"))+math.fsum(_finite(b)*v for b,v in zip(coef,z))
    if not math.isfinite(score):raise ModelUnavailable("NONFINITE_PARAMETER")
    return _sigmoid(score)


def dataset_arrays(rows):
    """每一期独立市场权重为一；不同规则是不同分组。"""
    keys = [(r["condition_id"],r["rule_version"]) for r in rows]
    count = Counter(keys);outcomes = {}
    for key,row in zip(keys,rows):
        if type(row["y"]) is not int or row["y"] not in (0,1):raise ModelUnavailable("DATA_INTEGRITY_ERROR")
        if key in outcomes and outcomes[key] != row["y"]:raise ModelUnavailable("DATA_INTEGRITY_ERROR","conflicting labels")
        outcomes[key] = row["y"]
    features = [raw_features(r["p_raw_up"],r["p_market_up"],r["tau_wall_seconds"]) for r in rows]
    weights = [1/count[key] for key in keys]
    return features,[r["y"] for r in rows],weights,outcomes


def _day(row):
    return datetime.fromtimestamp(row["window_open_at"]/1000,timezone.utc).date().isoformat()


def _counts(rows,*,complete_days=None):
    _,_,weights,outcomes = dataset_arrays(rows)
    counts = {"independent_markets":len(outcomes),"up_markets":sum(outcomes.values()),
              "down_markets":len(outcomes)-sum(outcomes.values()),"landmark_rows":len(rows),
              "total_market_weight":math.fsum(weights)}
    if complete_days is not None:counts["complete_utc_days"] = len(set(complete_days))
    return counts


def _write_manifest(directory,name,value):
    data = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    path = Path(directory)/name;path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.read_bytes() != data:raise ModelUnavailable("ARTIFACT_EXISTS",str(path))
    path.write_bytes(data)
    return {"uri":str(path.resolve()),"sha256":hashlib.sha256(data).hexdigest()}


def fit_day_bootstrap(rows,complete_utc_days,output_dir,*,config=None):
    cfg = config or load_config();c = cfg["calibration"];days = sorted(set(complete_utc_days))
    if not days or any(_day(row) not in days for row in rows):raise ModelUnavailable("DATA_INTEGRITY_ERROR","day coverage mismatch")
    x,y,w,_ = dataset_arrays(rows);indices = {day:[i for i,row in enumerate(rows) if _day(row)==day] for day in days}
    rng = random.Random(cfg["runtime"]["seed"]);success=[];failures=[]
    for attempt in range(c["bootstrap_models"]):
        draws = [days[rng.randrange(len(days))] for _ in days]
        draw_manifest = _write_manifest(output_dir,f"bootstrap-draw-{attempt:03}.json",{"days":draws,"seed":cfg["runtime"]["seed"],"attempt_index":attempt})
        selected = [i for day in draws for i in indices[day]]
        try:
            fitted = fit_logistic([x[i] for i in selected],[y[i] for i in selected],[w[i] for i in selected],
                ridge_lambda=float(c["ridge_lambda"]),max_iterations=c["optimizer_max_iterations"],tolerance=float(c["optimizer_tolerance"]))
            outcomes = {(rows[i]["condition_id"],rows[i]["rule_version"]):y[i] for i in selected}
            counts = {"day_draws":len(draws),"unique_utc_days":len(set(draws)),"unique_markets":len(outcomes),
                "unique_up_markets":sum(outcomes.values()),"unique_down_markets":len(outcomes)-sum(outcomes.values()),
                "landmark_rows_with_multiplicity":len(selected),"total_market_weight_with_multiplicity":math.fsum(w[i] for i in selected)}
            success.append({"attempt_index":attempt,"draw_manifest":draw_manifest,"sample_counts":counts,
                            "fit":fitted,"fitted_parameters_hash":_hash(fitted)})
        except ModelUnavailable as exc:
            reason = exc.reason_code if exc.reason_code in ("SINGLE_CLASS","OPTIMIZER_NONCONVERGENCE","NONFINITE_PARAMETER","INVALID_TRANSFORM","DATA_INTEGRITY_ERROR") else "OTHER"
            failures.append({"attempt_index":attempt,"draw_manifest":draw_manifest,"reason_code":reason,"detail":str(exc)})
    manifest = _write_manifest(output_dir,"bootstrap-manifest.json",{"successful":success,"failures":failures})
    return {"resampling_unit":"COMPLETE_UTC_DAY","sampling":"WITH_REPLACEMENT_D_FROM_D", "seed":cfg["runtime"]["seed"],
            "total_attempts":c["bootstrap_models"],"successful_count":len(success),"failed_count":len(failures),
            "successful_models":success,"failures":failures,"quantiles":[.1,.9],
            "quantile_method":"numpy.quantile:method=linear","manifest":manifest}


def probability_metrics(probabilities,labels,weights):
    total = math.fsum(weights)
    if not total or len(probabilities) != len(labels) or len(labels) != len(weights):raise ModelUnavailable("DATA_INTEGRITY_ERROR")
    probabilities = [_finite(p) for p in probabilities]
    if any(not 0 <= p <= 1 for p in probabilities):raise ModelUnavailable("INVALID_PROBABILITY")
    brier = math.fsum(w*(p-y)**2 for p,y,w in zip(probabilities,labels,weights))/total
    loss = -math.fsum(w*(y*math.log(max(p,1e-12))+(1-y)*math.log(max(1-p,1e-12))) for p,y,w in zip(probabilities,labels,weights))/total
    return {"brier":brier,"log_loss":loss}


def unfitted_artifact(created_at):
    """使用供应方 schema 的明确 UNFITTED 形式，不放置假参数。"""
    source = Path(__file__).with_name("spec")/"model_unfitted.json"
    if source.exists():
        result = json.loads(source.read_text());result["created_at"] = int(created_at)
        return result
    raise ModelUnavailable("MODEL_CONTRACT_MISSING")


def train_candidate(training_rows,validation_rows,*,complete_utc_days,training_window,
                    validation_window,evaluated_at,rule_version,rule_contract_hash,output_dir,
                    code_revision,coverage_evidence,config=None):
    """构建真实 FITTED/CANDIDATE，不自动发布；不足数据时明确报告而不训练。"""
    cfg = config or load_config();c = cfg["calibration"];output_dir = Path(output_dir)
    train = list(training_rows);validation = list(validation_rows)
    train_start,train_end = training_window["start_at"],training_window["end_at"]
    val_start,val_end = validation_window["start_at"],validation_window["end_at"]
    if train_end-train_start != c["train_window_days"]*DAY_MS or val_end-val_start != c["validation_window_days"]*DAY_MS or train_end != val_start or evaluated_at < val_end:
        raise ModelUnavailable("TIME_SPLIT_INVALID")
    for rows,start,end,cutoff in ((train,train_start,train_end,train_end),(validation,val_start,val_end,evaluated_at)):
        seen = set()
        for row in rows:
            if (row["rule_version"] != rule_version or not start <= row["window_open_at"] < end
                    or row["decision_at"] > row["label_available_at"] or row["label_available_at"] > cutoff
                    or row.get("label_verified_at",row["label_available_at"]) > cutoff):
                raise ModelUnavailable("LABEL_LEAKAGE_OR_SPLIT")
            key = (row["condition_id"],row["rule_version"],row["decision_at"])
            if key in seen:raise ModelUnavailable("DUPLICATE_LANDMARK")
            seen.add(key)
    train_keys = {(r["condition_id"],r["rule_version"]) for r in train}
    if train_keys & {(r["condition_id"],r["rule_version"]) for r in validation}:
        raise ModelUnavailable("MARKET_SPLIT_LEAKAGE")
    complete_days = sorted(set(complete_utc_days))
    for day in complete_days:
        try:day_ms=int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()*1000)
        except (ValueError,TypeError):raise ModelUnavailable("COVERAGE_INCOMPLETE") from None
        if not train_start <= day_ms or day_ms+DAY_MS > train_end:
            raise ModelUnavailable("COVERAGE_INCOMPLETE", "day outside training window")
    available_days = {_day(row) for row in train}
    if not available_days <= set(complete_days):raise ModelUnavailable("COVERAGE_INCOMPLETE")
    count = _counts(train,complete_days=complete_days) if train else {"independent_markets":0,"complete_utc_days":len(complete_days),"up_markets":0,"down_markets":0,"landmark_rows":0,"total_market_weight":0.}
    val_count = _counts(validation) if validation else {"independent_markets":0,"up_markets":0,"down_markets":0,"landmark_rows":0,"total_market_weight":0.}
    missing=[]
    for field,limit in (("independent_markets",c["min_train_markets"]),("complete_utc_days",c["min_train_days"]),
                        ("up_markets",c["min_markets_each_outcome"]),("down_markets",c["min_markets_each_outcome"])):
        if count[field] < limit:missing.append(f"{field}: {count[field]} < {limit}")
    if val_count["independent_markets"] < c["min_validation_markets"]:missing.append("validation_markets_insufficient")
    if missing:
        return {"status":"INSUFFICIENT","training_counts":count,"validation_counts":val_count,"reason_codes":missing,
                "artifact":unfitted_artifact(evaluated_at)}
    if not coverage_evidence or not code_revision or len(rule_contract_hash) != 64:
        raise ModelUnavailable("PROVENANCE_MISSING")
    training_manifest = _write_manifest(output_dir,"training-rows.json",train)
    validation_input_manifest = _write_manifest(output_dir,"validation-all-rows.json",validation)
    quality = _write_manifest(output_dir,"coverage.json",{"complete_utc_days":complete_days,"evidence":coverage_evidence})
    partition = _write_manifest(output_dir,"partition.json",{"training":training_window,"validation":validation_window,
        "condition_rule_train":sorted(train_keys),"condition_rule_validation":sorted({(r["condition_id"],r["rule_version"]) for r in validation})})
    x,y,w,_ = dataset_arrays(train)
    fit = fit_logistic(x,y,w,ridge_lambda=float(c["ridge_lambda"]),max_iterations=c["optimizer_max_iterations"],tolerance=float(c["optimizer_tolerance"]))
    bootstrap = fit_day_bootstrap(train,complete_days,output_dir,config=cfg)
    if bootstrap["successful_count"] < c["bootstrap_min_success"]:
        return {"status":"FAILED","reason_codes":["BOOTSTRAP_INSUFFICIENT"],"bootstrap":bootstrap}
    vx,vy,vw,_ = dataset_arrays(validation)
    accepted=[];predictions=[];excluded=[]
    for original,features in zip(validation,vx):
        try:
            prediction=predict_fit(fit,features)
            for child in bootstrap["successful_models"]:predict_fit(child["fit"],features)
            accepted.append(original);predictions.append(prediction)
        except ModelUnavailable as exc:
            excluded.append({"condition_id":original["condition_id"],"rule_version":original["rule_version"],
                             "decision_at":original["decision_at"],"reason_code":exc.reason_code})
    raw_validation_count=val_count
    validation=accepted
    val_count=_counts(validation) if validation else {"independent_markets":0,"up_markets":0,"down_markets":0,"landmark_rows":0,"total_market_weight":0.}
    coverage_report={"all_input":validation_input_manifest,"all_counts":raw_validation_count,
                     "common_valid_counts":val_count,"excluded_rows":excluded}
    coverage_manifest=_write_manifest(output_dir,"validation-coverage.json",coverage_report)
    if val_count["independent_markets"]<c["min_validation_markets"]:
        return {"status":"INSUFFICIENT","reason_codes":["VALIDATION_COMMON_DOMAIN_INSUFFICIENT"],
                "training_counts":count,"validation_counts":val_count,"validation_coverage":coverage_manifest,
                "training_executed":True}
    validation_manifest = _write_manifest(output_dir,"validation-rows.json",validation)
    _,vy,vw,_=dataset_arrays(validation)
    model_metrics = probability_metrics(predictions,vy,vw)
    market_metrics = probability_metrics([r["p_market_up"] for r in validation],vy,vw)
    tolerance = float(c["validation_score_tolerance"])
    brier_pass = model_metrics["brier"] <= market_metrics["brier"]+tolerance
    loss_pass = model_metrics["log_loss"] <= market_metrics["log_loss"]+tolerance
    from .research import probability_metric_intervals
    intervals=probability_metric_intervals([{**row,"p_fit_up":p} for row,p in zip(validation,predictions)])
    validation_report = _write_manifest(output_dir,"validation-report.json",{"model":model_metrics,"market":market_metrics,
        "counts":val_count,"tolerance":tolerance,"coverage":coverage_manifest,
        "interval_diagnostics":intervals,"computation_wall_at":int(time.time()*1000)})
    schema = json.loads((Path(__file__).with_name("spec")/"model_artifact.schema.json").read_text())
    recipe = {key:rule["const"] for key,rule in schema["$defs"]["FitRecipe"]["properties"].items()}
    inference = {key:rule["const"] for key,rule in schema["$defs"]["FittedArtifact"]["properties"]["inference_contract"]["properties"].items()}
    artifact = {"schema_version":SPEC_VERSION,"contract_kind":"HENGZHUN_INTERNAL_MODEL_ARTIFACT", "strategy_id":STRATEGY_ID,
        "spec_version":SPEC_VERSION,"created_at":int(evaluated_at),"feature_order":FEATURE_ORDER,
        "inference_enabled":False,"artifact_status":"FITTED","training_executed":True,
        "provenance":{"training_run_id":output_dir.resolve().name,"model_version":_hash(fit)[:20],"rule_version":rule_version,
            "feature_builder_version":"hengzhun-features-1.0.0","prior_version":"hengzhun-prior-1.0.0","training_code_revision":code_revision,
            "configuration_hash":config_hash(cfg),"rule_contract_hash":rule_contract_hash,
            "feature_definition_hash":_hash({"order":FEATURE_ORDER,"recipe":recipe}),"prior_configuration_hash":_hash(cfg["prior"]),
            "training_code_hash":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"training_manifest":training_manifest,
            "validation_manifest":validation_manifest,"fitted_parameters_hash":_hash({"fit":fit,"bootstrap":[m["fit"] for m in bootstrap["successful_models"]]})},
        "split_plan":{"train_window_days":28,"validation_window_days":7,"test_window_days":7,"roll_step_days":7,
            "training_window":training_window,"validation_window":validation_window,
            "test_window":{"start_at":val_end,"end_at":val_end+7*DAY_MS},"partition_manifest":partition},
        "fit_recipe":recipe,"training":{"cutoff_at":train_end,"label_available_cutoff_at":train_end,
            "max_label_available_at":max(r["label_available_at"] for r in train),"fit_completed_at":int(evaluated_at),
            "sample_counts":count,"data_quality_report":quality},"fit":fit,"bootstrap":bootstrap,
        "validation":{"status":"PASS" if brier_pass and loss_pass else "FAIL","evaluated_at":int(evaluated_at),
            "label_available_cutoff_at":int(evaluated_at),"max_label_available_at":max(r["label_available_at"] for r in validation),
            "sample_counts":val_count,"evaluation_scope":"COMMON_VALID_LANDMARKS_MARKET_WEIGHT_ONE","model_metrics":model_metrics,
            "market_probability_metrics":market_metrics,"absolute_tolerance":tolerance,"brier_passed":brier_pass,"log_loss_passed":loss_pass,
            "report":validation_report},
        "release":{"status":"CANDIDATE" if brier_pass and loss_pass else "REJECTED","decision_at":int(evaluated_at),
            "published_at":None,"available_at":None,"expires_at":None,"revoked_at":None,"publication_boundary_market_uid":None,
            "reason_codes":[] if brier_pass and loss_pass else ["VALIDATION_FAILED"]},"inference_contract":inference}
    validate_artifact(artifact)
    _write_manifest(output_dir,"model-candidate.json",artifact)
    return {"status":"FITTED_CANDIDATE" if brier_pass and loss_pass else "VALIDATION_FAILED","artifact":artifact}


def _verify_manifest(reference):
    path = Path(reference["uri"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ModelUnavailable("MODEL_PROVENANCE_INVALID",str(path))
    try:return json.loads(path.read_text())
    except (ValueError,UnicodeError):raise ModelUnavailable("MODEL_PROVENANCE_INVALID", "manifest is not JSON") from None


def validate_artifact(artifact, *, verify_manifests=True):
    if artifact.get("artifact_status") != "FITTED" or artifact.get("training_executed") is not True:
        raise ModelUnavailable("MODEL_UNFITTED")
    from .contracts import validate_record
    try:validate_record(artifact,"model_artifact.schema.json")
    except ValidationError as exc:
        raise ModelUnavailable("MODEL_ARTIFACT_INVALID", "/".join(map(str,exc.absolute_path))+":"+str(exc.validator)) from None
    except (ValueError,TypeError,KeyError) as exc:raise ModelUnavailable("MODEL_ARTIFACT_INVALID",str(exc)) from None
    b=artifact["bootstrap"];models=b["successful_models"];failures=b["failures"]
    indices=[x["attempt_index"] for x in models+failures]
    if b["successful_count"] != len(models) or b["failed_count"] != len(failures) or sorted(indices) != list(range(100)):
        raise ModelUnavailable("MODEL_BOOTSTRAP_INVALID")
    for model in models:
        if _hash(model["fit"]) != model["fitted_parameters_hash"]:raise ModelUnavailable("MODEL_HASH_MISMATCH")
    if _hash({"fit":artifact["fit"],"bootstrap":[m["fit"] for m in models]}) != artifact["provenance"]["fitted_parameters_hash"]:
        raise ModelUnavailable("MODEL_HASH_MISMATCH")
    t=artifact["training"];v=artifact["validation"];s=artifact["split_plan"]
    if (s["training_window"]["end_at"]-s["training_window"]["start_at"] != 28*DAY_MS
            or s["validation_window"]["end_at"]-s["validation_window"]["start_at"] != 7*DAY_MS
            or s["validation_window"]["start_at"] != s["training_window"]["end_at"]
            or t["max_label_available_at"] > t["label_available_cutoff_at"] or t["label_available_cutoff_at"] > t["cutoff_at"]
            or t["cutoff_at"] != s["training_window"]["end_at"]
            or v["max_label_available_at"] > v["label_available_cutoff_at"] or v["label_available_cutoff_at"] > v["evaluated_at"]
            or v["evaluated_at"] < s["validation_window"]["end_at"]):
        raise ModelUnavailable("MODEL_TIME_LEAKAGE")
    for counts in (t["sample_counts"],v["sample_counts"]):
        if counts["up_markets"]+counts["down_markets"] != counts["independent_markets"] or abs(counts["total_market_weight"]-counts["independent_markets"]) > 1e-8:
            raise ModelUnavailable("MODEL_COUNTS_INVALID")
    if verify_manifests:
        refs=[artifact["provenance"]["training_manifest"],artifact["provenance"]["validation_manifest"],
              t["data_quality_report"],s["partition_manifest"],v["report"],b["manifest"]]
        refs += [m["draw_manifest"] for m in models+failures]
        for reference in refs:_verify_manifest(reference)
        train_rows=_verify_manifest(artifact["provenance"]["training_manifest"])
        val_rows=_verify_manifest(artifact["provenance"]["validation_manifest"])
        coverage=_verify_manifest(t["data_quality_report"])
        if (not isinstance(train_rows,list) or not isinstance(val_rows,list)
                or _counts(train_rows,complete_days=coverage.get("complete_utc_days",[])) != t["sample_counts"]
                or _counts(val_rows) != v["sample_counts"]):
            raise ModelUnavailable("MODEL_COUNTS_INVALID", "manifest content differs from declared counts")
        training_keys={(r["condition_id"],r["rule_version"]) for r in train_rows}
        validation_keys={(r["condition_id"],r["rule_version"]) for r in val_rows}
        if training_keys & validation_keys:raise ModelUnavailable("MARKET_SPLIT_LEAKAGE")
        for rows,window,cutoff in ((train_rows,s["training_window"],t["label_available_cutoff_at"]),
                                    (val_rows,s["validation_window"],v["label_available_cutoff_at"])):
            if any(not window["start_at"]<=row["window_open_at"]<window["end_at"]
                   or row["label_available_at"]>cutoff or row["decision_at"]>row["label_available_at"]
                   or row["rule_version"]!=artifact["provenance"]["rule_version"] for row in rows):
                raise ModelUnavailable("MODEL_TIME_LEAKAGE")
    return artifact


def publish_artifact(artifact,published_at_ms,market_uid,*,available_at_ms=None):
    validate_artifact(artifact)
    if artifact["validation"]["status"] != "PASS" or artifact["release"]["status"] != "CANDIDATE":
        raise ModelUnavailable("MODEL_NOT_VALIDATED")
    if published_at_ms % 300000 or published_at_ms < max(artifact["validation"]["evaluated_at"],artifact["training"]["fit_completed_at"]):
        raise ModelUnavailable("MODEL_PUBLICATION_BOUNDARY")
    if not market_uid:raise ModelUnavailable("MODEL_PUBLICATION_BOUNDARY")
    available = published_at_ms if available_at_ms is None else available_at_ms
    if available < published_at_ms:raise ModelUnavailable("MODEL_TIME_LEAKAGE")
    result=deepcopy(artifact);result["inference_enabled"]=True
    result["release"].update(status="RELEASED",decision_at=published_at_ms,published_at=published_at_ms,
        available_at=available,expires_at=published_at_ms+7*DAY_MS,publication_boundary_market_uid=market_uid,reason_codes=[])
    validate_artifact(result)
    return result


def predict_calibrated(artifact,p_raw_up,p_market_up,tau_wall_seconds,decision_at_ms,
                       rule_version,rule_contract_hash,*,verify_manifests=True):
    validate_artifact(artifact,verify_manifests=verify_manifests)
    release=artifact["release"];validation=artifact["validation"]
    if not artifact["inference_enabled"] or release["status"] != "RELEASED" or release.get("revoked_at") is not None:
        raise ModelUnavailable("MODEL_NOT_RELEASED")
    if validation["status"] != "PASS" or not validation["brier_passed"] or not validation["log_loss_passed"]:
        raise ModelUnavailable("MODEL_NOT_VALIDATED")
    if not release["available_at"] <= decision_at_ms < release["expires_at"] or release["expires_at"] > release["published_at"]+7*DAY_MS:
        raise ModelUnavailable("MODEL_EXPIRED_OR_NOT_AVAILABLE")
    if release["published_at"] % 300000 or release["available_at"] < max(release["published_at"],validation["evaluated_at"]):
        raise ModelUnavailable("MODEL_TIME_LEAKAGE")
    if artifact["provenance"]["rule_version"] != rule_version or artifact["provenance"]["rule_contract_hash"] != rule_contract_hash:
        raise ModelUnavailable("MODEL_RULE_MISMATCH")
    features=raw_features(p_raw_up,p_market_up,tau_wall_seconds)
    fitted=predict_fit(artifact["fit"],features)
    # Every successful submodel participates; invalid inference rejects the decision.
    probabilities=[predict_fit(m["fit"],features) for m in artifact["bootstrap"]["successful_models"]]
    p10,p90=linear_quantile(probabilities,.1),linear_quantile(probabilities,.9)
    guards=conservative_guards(str(fitted),str(p10),str(p90))
    return {"p_fit_up":fitted,"bootstrap_p10_up":p10,"bootstrap_p90_up":p90,
            "guard_up":float(guards["guard_up"]),"guard_down":float(guards["guard_down"]),
            "bootstrap_successful_count":len(probabilities),"model_version":artifact["provenance"]["model_version"]}

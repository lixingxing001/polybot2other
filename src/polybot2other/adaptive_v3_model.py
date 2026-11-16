"""Isolated V3 storage, causal feature recovery, and forward candidate evidence.

V2 tables are preserved. Historical recovery supplies training only; only new
predictions committed before market end can activate the V3 score correction.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time

from .adaptive_calibration import finite
from .adaptive_features import (FEATURE_FIELDS, FEATURE_VERSION, MARKET_FIELDS,
                                committed_features, read_reference_features)
from .adaptive_model import _read_settled_rows
from .adaptive_v3 import (MODEL_VERSION, POLICY, MAX_ADJUSTMENT, MIN_EFFECTIVE_SAMPLES,
                         canonical_samples, fit_model, predict, validate_candidate)

OBSERVATION_LIMIT = 12000
V2_VERSION = "main-adaptive-calibration-v2"


def _json(value):
    try:
        result = json.loads(value)
    except (ValueError,TypeError):
        return {}
    return result if isinstance(result,dict) else {}


def normalize_trade(row, source_name):
    """Anchor recovery to a known pre-fill decision, never to the later fill."""
    payload = row.get("order_payload") or {}
    decision = payload.get("adaptive_decision") or {}
    signal, market = payload.get("signal") or {}, payload.get("market") or {}
    opened, settled = finite(row.get("opened_at")), finite(row.get("settled_at"))
    side = row.get("side")
    result = {"source_key":f"{source_name}:{row['id']}", "source_name":source_name,
        "round_id":row.get("round_id"), "side":side, "opened_at":opened,
        "settled_at":settled, "pnl":finite(row.get("pnl")),
        "provenance":"legacy_adaptive", "exclusion_reason":None}
    if source_name == "MAIN_OPTIMIZED":
        base = finite(row.get("confidence"))
        when = finite(payload.get("submitted_at"))
        entry = finite(signal.get("entry_price"))
        ask, bid = entry, None
        provenance, basis = "optimized_raw", "order-submission"
        result["min_edge"] = None  # Historical global threshold was not persisted.
        if payload and (signal.get("side")!=side or market.get("round_id")!=row.get("round_id")):
            result["exclusion_reason"]="mismatched_order_provenance"
    else:
        base = finite(decision.get("base_selected_score"))
        when = finite(decision.get("decision_at"))
        entry = finite(decision.get("entry_price"))
        ask,bid = finite(decision.get("ask")),finite(decision.get("bid"))
        version = decision.get("adaptive_model_version")
        provenance = "adaptive_v3_raw" if version == MODEL_VERSION else "adaptive_v2_raw"
        basis = "adaptive-decision"
        if (version not in (MODEL_VERSION,V2_VERSION) or decision.get("round_id") != row.get("round_id")
                or decision.get("candidate_side") != side):
            result["exclusion_reason"] = "missing_raw_base_score"
            provenance = "legacy_adaptive"
        result["min_edge"] = finite(decision.get("min_edge"))
    if (when is None or opened is None or settled is None or not when <= opened < settled
            or base is None or not .5 <= base <= 1 or entry is None or not 0 < entry < 1):
        result["exclusion_reason"] = result["exclusion_reason"] or "missing_valid_pre_fill_decision"
    outcome = row.get("outcome") if row.get("outcome_source") == "polymarket_official" else None
    if outcome not in ("Up","Down") or row.get("settlement_source") != "polymarket_official":
        result["exclusion_reason"] = result["exclusion_reason"] or "nonofficial_or_nonterminal_label"
    ends = finite(market.get("ends_at", decision.get("market_ends_at",row.get("ends_at"))))
    result.update(base_score=base,decision_at=when,decision_entry_price=entry,entry_price=entry,
        provenance=provenance,decision_time_basis=basis,v3_known_at=when,
        time_left_seconds=ends-when if ends is not None and when is not None else None,
        ends_at=ends,won=int(outcome == side) if outcome is not None else None,
        label_source="polymarket_official" if outcome is not None else None,
        _ask=ask,_bid=bid,_target_price=finite(decision.get("target_price",market.get("target_price"))),
        applied_score=finite(decision.get("adaptive_score")),
        applied_delta=finite(decision.get("adaptive_delta")),
        decision_model_version=decision.get("adaptive_model_version"))
    # Preserve actual V3 decision-time features, never recompute them with later data.
    if decision.get("v3_feature_version") == FEATURE_VERSION:
        result.update(committed_features(decision,when))
        result["v3_feature_origin"]="decision-snapshot"
    return result


class AdaptiveV3Model:
    def __init__(self, model_path, seed_path, adaptive_path=None, *, reference_path=None):
        self.path,self.seed_path = Path(model_path),Path(seed_path)
        self.adaptive_path = Path(adaptive_path) if adaptive_path else None
        self.reference_path = Path(reference_path) if reference_path else self.path.parent.parent/"live-accounts/market.references.sqlite3"
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.conn=sqlite3.connect(self.path,check_same_thread=False)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS context_training_samples_v3(
                source_key TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS context_observations_v3(
                round_id TEXT PRIMARY KEY,side TEXT NOT NULL,decision_at REAL NOT NULL,
                ends_at REAL NOT NULL,payload TEXT NOT NULL,recorded_at REAL NOT NULL,
                outcome TEXT,settled_at REAL,label_source TEXT);
        """)
        self._rows=[];self._shadow_rows=[];self._source_errors={};self._last_sync=0.
        self._last_assessment={};self._last_prediction={};self._latest_features={}
        self._fit=None;self._fit_key=None;self._last_fit_at=0.;self._learner_error=None
        self._next_feature_recovery=0.
        self._cache={r["source_key"]:_json(r["payload"]) for r in self.conn.execute("SELECT * FROM context_training_samples_v3")}
        self.sync(force=True)

    def close(self):
        with self.lock:self.conn.close()

    def _cache_row(self,row):
        key=row["source_key"]
        old=self._cache.get(key)
        if old is not None:
            return old
        if not row.get("exclusion_reason") and row.get("v3_feature_version") != FEATURE_VERSION:
            row.update(read_reference_features(self.reference_path,row.get("v3_known_at"),
                ask=row.get("_ask"),bid=row.get("_bid"),target_price=row.get("_target_price")))
            row["v3_feature_origin"]="historical-recovery"
        row={k:v for k,v in row.items() if not k.startswith("_")}
        encoded=json.dumps(row,ensure_ascii=False,allow_nan=False,sort_keys=True)
        self.conn.execute("INSERT OR IGNORE INTO context_training_samples_v3 VALUES(?,?,?)",(key,encoded,time.time()))
        self._cache[key]=row
        return row

    def _import(self,path,name):
        try:
            if path is None or not path.exists():
                self._source_errors[name]="SOURCE_MISSING";return
            rows=_read_settled_rows(path)
            with self.conn:
                for row in rows:
                    key=f"{name}:{row['id']}"
                    if key not in self._cache:self._cache_row(normalize_trade(row,name))
            self._source_errors.pop(name,None)
        except (OSError,sqlite3.Error,TypeError,ValueError) as exc:
            self._source_errors[name]=type(exc).__name__

    def _official_labels(self):
        labels={}
        for path in (self.seed_path,self.adaptive_path):
            if path is None or not path.exists():continue
            try:
                with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=2)) as source:
                    source.row_factory=sqlite3.Row
                    for row in source.execute("""SELECT round_id,outcome,settled_at FROM market_rounds
                        WHERE settlement_source='polymarket_official' AND outcome IN ('Up','Down')
                        AND settled_at IS NOT NULL"""):
                        labels[row["round_id"]]=dict(row)
            except (OSError,sqlite3.Error):continue
        return labels

    @staticmethod
    def _observation_row(row, *, legacy=False):
        d=_json(row["payload"]);when=row["decision_at"]
        result={"source_key":("V2_SHADOW:" if legacy else "V3_SHADOW:")+row["round_id"],
            "source_name":"V2_SHADOW" if legacy else "V3_SHADOW","round_id":row["round_id"],
            "side":row["side"],"base_score":d.get("base_selected_score"),
            "entry_price":d.get("entry_price"),"decision_entry_price":d.get("entry_price"),
            "opened_at":when,"decision_at":when,"v3_known_at":when,"decision_time_basis":"shadow-decision",
            "settled_at":row["settled_at"],"ends_at":row["ends_at"],"recorded_at":row["recorded_at"],
            "won":int(row["outcome"]==row["side"]) if row["outcome"] in ("Up","Down") else None,
            "label_source":row["label_source"],"provenance":"shadow_v2_raw" if legacy else "shadow_v3_raw",
            "time_left_seconds":d.get("time_left_seconds"),"candidate_score":d.get("adaptive_candidate_score"),
            "all_in_cost":d.get("all_in_cost"),"min_edge":d.get("min_edge"),"fee_source":d.get("fee_source"),
            "model_version":d.get("adaptive_model_version"),"trained_before":d.get("adaptive_training_cutoff"),
            "training_fingerprint":d.get("v3_training_fingerprint"),
            "_ask":d.get("ask",d.get("entry_price")),"_bid":d.get("bid")}
        if d.get("v3_feature_version")==FEATURE_VERSION:
            result.update(committed_features(d,when))
            result["v3_feature_origin"]="decision-snapshot"
        if legacy and d.get("adaptive_model_version")!=V2_VERSION:
            result["exclusion_reason"]="unknown_observation_version"
        if row["label_source"]!="polymarket_official":result["exclusion_reason"]="nonofficial_label"
        return result

    def _recover_missing_features(self,now,force):
        if not self.reference_path.exists():
            self._source_errors["REFERENCE_HISTORY"]="SOURCE_MISSING"
            return
        if not force and now<self._next_feature_recovery:return
        self._next_feature_recovery=now+300
        failure=None
        with self.conn:
            for key,row in list(self._cache.items()):
                status=row.get("v3_feature_status","")
                if (row.get("exclusion_reason") or row.get("v3_feature_origin")=="decision-snapshot"
                        or not (status=="MISSING_HISTORY" or status.startswith("SOURCE_ERROR:"))):continue
                recovered=read_reference_features(self.reference_path,row.get("v3_known_at"))
                if recovered["v3_feature_status"].startswith("SOURCE_ERROR:"):
                    failure=recovered["v3_feature_status"]
                    break
                # A pre-existing decision quote is independent of reference recovery.
                recovered["v3_spread"]=row.get("v3_spread")
                changed={**row,**recovered}
                self._cache[key]=changed
                self.conn.execute("UPDATE context_training_samples_v3 SET payload=?,updated_at=? WHERE source_key=?",
                    (json.dumps(changed,ensure_ascii=False,allow_nan=False,sort_keys=True),now,key))
                self._fit_key=None
        if failure:self._source_errors["REFERENCE_HISTORY"]=failure
        else:self._source_errors.pop("REFERENCE_HISTORY",None)

    def _sync_observations(self):
        labels=self._official_labels()
        tables={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with self.conn:
            if "calibration_observations" in tables:
                for row in self.conn.execute("SELECT * FROM calibration_observations").fetchall():
                    row=dict(row)
                    label=labels.get(row["round_id"])
                    if row.get("settled_at") is None and label and row["ends_at"]<=label["settled_at"]:
                        row.update(outcome=label["outcome"],settled_at=label["settled_at"],label_source="polymarket_official")
                    if row.get("settled_at") is not None:self._cache_row(self._observation_row(row,legacy=True))
            for row in self.conn.execute("SELECT * FROM context_observations_v3 WHERE settled_at IS NULL").fetchall():
                label=labels.get(row["round_id"])
                if label and row["decision_at"]<row["ends_at"]<=label["settled_at"]:
                    self.conn.execute("""UPDATE context_observations_v3 SET outcome=?,settled_at=?,label_source=?
                        WHERE round_id=? AND settled_at IS NULL""",
                        (label["outcome"],label["settled_at"],"polymarket_official",row["round_id"]))
        self._shadow_rows=[self._observation_row(dict(r)) for r in self.conn.execute(
            "SELECT * FROM context_observations_v3 WHERE settled_at IS NOT NULL")]

    def sync(self, *, force=False):
        with self.lock:
            now=time.time()
            if not force and now-self._last_sync<5:return
            self._import(self.seed_path,"MAIN_OPTIMIZED")
            if self.adaptive_path and self.adaptive_path.resolve()!=self.seed_path.resolve():
                self._import(self.adaptive_path,"MAIN_ADAPTIVE")
            self._sync_observations()
            self._recover_missing_features(now,force)
            self._rows=list(self._cache.values())
            self._last_sync=now
            with self.conn:
                for k,v in (("active_model_version",MODEL_VERSION),("v3_last_sync_at",str(now))):
                    self.conn.execute("INSERT OR REPLACE INTO model_meta VALUES(?,?)",(k,v))

    def _model(self,now,round_id=None):
        rows=self._rows+self._shadow_rows
        key=(len(rows),max((r.get("settled_at") or 0 for r in rows),default=0),round_id)
        if self._fit is None or key!=self._fit_key or now-self._last_fit_at>=60 or now<self._last_fit_at:
            self._fit=fit_model(rows,now=now,round_id=round_id)
            self._fit_key,self._last_fit_at=key,now
        return self._fit

    def adjust_prediction(self,prediction,market,now):
        base_up=finite(prediction.get("base_up_score",prediction.get("up_score")))
        if base_up is None or not 0<=base_up<=1:return prediction
        side="Up" if base_up>=.5 else "Down";base=base_up if side=="Up" else 1-base_up
        return {**prediction,"base_up_score":base_up,"base_selected_score":base,"up_score":base_up,
            "candidate_side":side,"adaptive_score":base,"adaptive_delta":0.,"adaptive_samples":0,
            "adaptive_state":"AWAITING_COST","adaptive_model_version":MODEL_VERSION,"adaptive_observed_at":now}

    def assess_entry(self,prediction,market,now,*,entry_price,all_in_cost,min_edge):
        self.sync()
        base=self.adjust_prediction(prediction,market,now)
        score=base["base_selected_score"];side=base["candidate_side"]
        inputs={**base,"base_score":score,"side":side,"entry_price":entry_price,
                "time_left_seconds":market.ends_at-now}
        with self.lock:
            validation=validate_candidate(self._shadow_rows,now=now,round_id=market.round_id)
            try:
                model=self._model(now,market.round_id)
                proposed=predict(model,inputs)
                self._learner_error=None
            except (ValueError,ArithmeticError,KeyError,TypeError) as exc:
                self._learner_error=type(exc).__name__
                model={"counts":{},"trained_before":None,"training_fingerprint":None,"state":"ERROR"}
                proposed={"score":score,"delta":0.,"base_score":score,"reason":"学习计算暂不可用，原始基线继续开仓",
                          "feature_coverage":{},"contributions":{}}
            applied=proposed["score"] if validation["passed"] and not self._learner_error else score
            counts=model["counts"]
            result={"base_selected_score":score,"candidate_side":side,"base_up_score":base["base_up_score"],
                "up_score":applied if side=="Up" else 1-applied,"adaptive_score":applied,
                "adaptive_delta":applied-score,"adaptive_candidate_score":proposed["score"],
                "adaptive_candidate_delta":proposed["score"]-score,
                "adaptive_expected_edge":applied-all_in_cost,"adaptive_candidate_expected_edge":proposed["score"]-all_in_cost,
                "adaptive_allowed":applied-all_in_cost+1e-12>=min_edge,
                "adaptive_candidate_allowed":proposed["score"]-all_in_cost+1e-12>=min_edge,
                "adaptive_validation_state":validation["state"],"adaptive_validation_reason":validation["reason"],
                "adaptive_validation_samples":validation["samples"],"adaptive_state":"CONTEXT_LEARNING",
                "adaptive_bucket":"continuous-context","adaptive_samples":counts.get("unique_markets",0),
                "adaptive_effective_samples":counts.get("effective_samples",0),
                "adaptive_training_cutoff":model.get("trained_before"),"adaptive_model_version":MODEL_VERSION,
                "adaptive_model_health":"SOURCE_WARNING" if self._source_errors else "OK",
                "adaptive_reason":proposed["reason"]+"；"+validation["reason"],
                "adaptive_observed_at":now,"decision_at":now,"round_id":market.round_id,
                "entry_price":entry_price,"all_in_cost":all_in_cost,"min_edge":min_edge,
                "market_ends_at":market.ends_at,"time_left_seconds":market.ends_at-now,
                "v3_training_fingerprint":model.get("training_fingerprint"),
                "v3_model_state":model.get("state"),"v3_actual_correction":abs(applied-score)>1e-12,
                **{k:inputs.get(k) for k in FEATURE_FIELDS if k in inputs}}
            self._last_assessment=result;self._last_prediction=proposed
            self._latest_features={k:inputs.get(k) for k in FEATURE_FIELDS}
            return result

    def record_observation(self,metadata):
        # The caller gives a compact, scalar-only final fee-aware decision.
        if not isinstance(metadata,dict) or metadata.get("adaptive_model_version")!=MODEL_VERSION:return False
        now=finite(metadata.get("decision_at"));ends=finite(metadata.get("market_ends_at"));captured=time.time()
        base,candidate,entry,cost,edge=(finite(metadata.get(k)) for k in
            ("base_selected_score","adaptive_candidate_score","entry_price","all_in_cost","min_edge"))
        training=finite(metadata.get("adaptive_training_cutoff"))
        if (now is None or ends is None or not now<=captured<ends or captured-now>30
                or not metadata.get("round_id") or metadata.get("candidate_side") not in ("Up","Down")
                or base is None or not .5<=base<=1 or candidate is None or not 0<candidate<1
                or entry is None or cost is None or not 0<entry<=cost<1 or edge is None or edge<0
                or (training is not None and training>=now)):
            return False
        if (metadata.get("v3_feature_version")==FEATURE_VERSION
                and committed_features(metadata,now)["v3_feature_status"]=="INVALID_CAUSAL_PROVENANCE"):
            return False
        fields=("round_id","candidate_side","decision_at","market_ends_at","base_selected_score",
            "adaptive_score","adaptive_delta","adaptive_candidate_score","adaptive_candidate_delta",
            "adaptive_allowed","adaptive_candidate_allowed","adaptive_model_version","adaptive_training_cutoff",
            "adaptive_validation_state","entry_price","all_in_cost","min_edge","time_left_seconds",
            "fee_source","ask","bid","v3_training_fingerprint","v3_model_state",*FEATURE_FIELDS)
        payload={k:metadata[k] for k in fields if k in metadata and
                 (metadata[k] is None or isinstance(metadata[k],(str,int,float,bool)))}
        try:encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False,sort_keys=True)
        except (ValueError,TypeError):return False
        with self.lock,self.conn:
            changed=self.conn.execute("""INSERT OR IGNORE INTO context_observations_v3
                (round_id,side,decision_at,ends_at,payload,recorded_at) VALUES(?,?,?,?,?,?)""",
                (metadata["round_id"],metadata["candidate_side"],now,ends,encoded,captured)).rowcount
            self.conn.execute("""DELETE FROM context_observations_v3 WHERE round_id IN
                (SELECT round_id FROM context_observations_v3 ORDER BY decision_at DESC LIMIT -1 OFFSET ?)""",
                (OBSERVATION_LIMIT,))
            return bool(changed)

    def snapshot(self):
        self.sync();now=time.time()
        with self.lock:
            canonical,counts=canonical_samples(self._rows+self._shadow_rows,now=now)
            # Snapshot never replaces the model used for the last actual decision.
            model=self._fit
            if model is None:
                try:model=self._model(now)
                except (ValueError,ArithmeticError,TypeError):model={"counts":counts,"state":"ERROR"}
            validation=validate_candidate(self._shadow_rows,now=now)
            validation.update(raw_drawdown=validation.get("raw_quote_drawdown"),
                              candidate_drawdown=validation.get("candidate_quote_drawdown"),
                              selection={k:validation.get(k,0) for k in
                                  ("missed_winners","prevented_losers","added_winners","added_losers")})
            trades=[r for r in self._rows if r["source_name"]=="MAIN_ADAPTIVE"]
            v3_trades=[r for r in trades if r.get("decision_model_version")==MODEL_VERSION]
            execution={"v3_trades":len(v3_trades),"actual_adjusted_trades":sum(
                abs(r.get("applied_delta") or 0)>1e-12 for r in v3_trades),"scope":"settled_trades"}
            total=self.conn.execute("SELECT count(*) FROM context_observations_v3").fetchone()[0]
            coverage={k:sum(finite(r.get(k)) is not None for r in canonical) for k in MARKET_FIELDS}
            sources=Counter(r["source_name"] for r in self._rows)
            v3={"model_version":MODEL_VERSION,"state":validation["state"],"policy":dict(POLICY),
                "model":model,"validation":validation,"shadow_only":not validation["passed"],
                "feature_coverage":coverage,"last_prediction":dict(self._last_prediction),
                "latest_features":dict(self._latest_features),"last_assessment_at":self._last_assessment.get("decision_at"),
                "execution":execution,"learner_error":self._learner_error,
                "historical_seed":{"loaded":bool(self._rows),"rows":len(self._rows),
                    "usable_markets":counts["unique_markets"],"source":"optimized+verified_adaptive+v2_shadow",
                    "cutoff":max((r.get("settled_at") or 0 for r in self._rows),default=None)}}
            return {"model_version":MODEL_VERSION,"model_path":str(self.path),"seed_path":str(self.seed_path),
                "adaptive_path":str(self.adaptive_path) if self.adaptive_path else None,
                "total_samples":sources["MAIN_OPTIMIZED"]+sources["MAIN_ADAPTIVE"],
                "seed_samples":sources["MAIN_OPTIMIZED"],"adaptive_samples":sources["MAIN_ADAPTIVE"],
                "usable_samples":counts["unique_markets"],"unique_markets":counts["unique_markets"],
                "canonical_samples":counts["unique_markets"],"excluded_legacy_samples":counts["excluded_legacy_samples"],
                "exclusions":counts["exclusions"],"effective_samples":counts["effective_samples"],
                "half_life_hours":72,"max_adjustment":MAX_ADJUSTMENT,"max_upward_adjustment":MAX_ADJUSTMENT,
                "max_downward_adjustment":MAX_ADJUSTMENT,"min_adjustment_samples":MIN_EFFECTIVE_SAMPLES,
                "last_sync_at":self._last_sync,"latest_settled_at":max((r["settled_at"] for r in canonical),default=None),
                "shadow_observations":total,"shadow_resolved":len(self._shadow_rows),
                "state":"READY" if counts["effective_samples"]>=MIN_EFFECTIVE_SAMPLES else "WARMING",
                "health":"SOURCE_WARNING" if self._source_errors else "OK","source_errors":dict(self._source_errors),
                "policy":dict(POLICY),"recent_quality":{},"validation":validation,
                "validation_state":validation["state"],"last_assessment":dict(self._last_assessment),"v3":v3}

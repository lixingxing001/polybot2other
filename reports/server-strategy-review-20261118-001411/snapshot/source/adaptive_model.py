"""Isolated V2 calibration storage. Source ledgers are always opened read-only."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time

from .adaptive_calibration import (MODEL_VERSION, POLICY, HALF_LIFE_SECONDS,
    MAX_SAMPLE_AGE_SECONDS, MIN_EXACT_BUCKET_SAMPLES, MIN_ADJUSTMENT_SAMPLES,
    MAX_ADJUSTMENT, MAX_DOWNWARD_ADJUSTMENT, MAX_UPWARD_ADJUSTMENT,
    calibrate_entry, canonical_samples, finite, validate_candidate)

EXTRA_COLUMNS = {"base_score": "REAL", "provenance": "TEXT", "decision_at": "REAL",
    "time_left_seconds": "REAL", "decision_json": "TEXT", "predicted_score": "REAL",
    "exclusion_reason": "TEXT", "label_source": "TEXT", "decision_entry_price": "REAL"}
OBSERVATION_LIMIT = 12000


def _read_settled_rows(path):
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")  # Trades and immutable order metadata share one read snapshot.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        source = "t.settlement_source" if "settlement_source" in cols else "NULL"
        joined = "market_rounds" in tables
        market_cols = {r[1] for r in conn.execute("PRAGMA table_info(market_rounds)")} if joined else set()
        outcome = "m.outcome" if "outcome" in market_cols else "NULL"
        ends = "m.ends_at" if "ends_at" in market_cols else "NULL"
        outcome_source = "m.settlement_source" if "settlement_source" in market_cols else "NULL"
        query = f"""SELECT t.id,t.round_id,t.side,t.confidence,t.move_bps,t.entry_price,
            t.opened_at,t.settled_at,t.pnl,t.status,{source} AS settlement_source,
            {outcome} AS outcome,{ends} AS ends_at,{outcome_source} AS outcome_source FROM trades t
            {'LEFT JOIN market_rounds m ON m.round_id=t.round_id' if joined else ''}
            WHERE t.status='SETTLED' AND t.settled_at IS NOT NULL ORDER BY t.settled_at,t.id"""
        rows = [dict(r) for r in conn.execute(query)]
        payloads = {}
        if {"paper_orders", "paper_sim_orders"} <= tables:
            for row in conn.execute("""SELECT o.trade_id,s.payload FROM paper_orders o
                    JOIN paper_sim_orders s ON s.order_id=o.id
                    WHERE o.trade_id IS NOT NULL ORDER BY o.id"""):
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("action", "BUY") == "BUY":
                    payloads.setdefault(row["trade_id"], payload)
        for row in rows:
            row["order_payload"] = payloads.get(row["id"], {})
        return rows


class AdaptiveModel:
    def __init__(self, model_path, seed_path, adaptive_path=None):
        self.path = Path(model_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seed_path = Path(seed_path)
        self.adaptive_path = Path(adaptive_path) if adaptive_path else None
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS training_samples (
                source_key TEXT PRIMARY KEY, source_name TEXT NOT NULL,
                source_trade_id INTEGER NOT NULL, round_id TEXT NOT NULL, side TEXT NOT NULL,
                confidence REAL NOT NULL, move_bps REAL NOT NULL, entry_price REAL NOT NULL,
                opened_at REAL NOT NULL, settled_at REAL NOT NULL, pnl REAL NOT NULL,
                won INTEGER NOT NULL, imported_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS model_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calibration_observations (
                round_id TEXT PRIMARY KEY, side TEXT NOT NULL, decision_at REAL NOT NULL,
                ends_at REAL NOT NULL, payload TEXT NOT NULL, outcome TEXT,
                settled_at REAL, label_source TEXT, recorded_at REAL);
        """)
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(training_samples)")}
        with self.conn:
            shadow_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(calibration_observations)")}
            if "recorded_at" not in shadow_columns:
                self.conn.execute("ALTER TABLE calibration_observations ADD COLUMN recorded_at REAL")
            for name, kind in EXTRA_COLUMNS.items():
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE training_samples ADD COLUMN {name} {kind}")
            # Legacy adjusted confidence cannot be relabeled as raw confidence.
            self.conn.execute("""UPDATE training_samples SET base_score=confidence,
                provenance='optimized_raw',decision_at=opened_at
                WHERE source_name='MAIN_OPTIMIZED' AND provenance IS NULL""")
            self.conn.execute("""UPDATE training_samples SET provenance='legacy_adaptive',
                exclusion_reason='missing_raw_base_score' WHERE provenance IS NULL""")
        self._last_sync = 0.
        self._rows = []
        self._source_errors = {}
        self._last_assessment = {}
        self.sync(force=True)

    def close(self):
        with self.lock:
            self.conn.close()

    def _import_rows(self, path, source_name):
        try:
            rows = _read_settled_rows(path)
            if not path.exists():
                self._source_errors[source_name] = "SOURCE_MISSING"
            else:
                self._source_errors.pop(source_name, None)
        except (OSError, sqlite3.Error) as exc:
            self._source_errors[source_name] = type(exc).__name__
            return
        now = time.time()
        with self.conn:
            for row in rows:
                numeric = {k: finite(row.get(k)) for k in
                           ("confidence", "move_bps", "entry_price", "opened_at", "settled_at", "pnl")}
                if (any(v is None for v in numeric.values()) or row.get("side") not in ("Up", "Down")
                        or not row.get("round_id") or not .01 <= numeric["confidence"] <= 1
                        or numeric["entry_price"] <= 0):
                    continue
                payload = row["order_payload"]
                decision = payload.get("adaptive_decision")
                decision = decision if isinstance(decision, dict) else {}
                decision_at = finite(decision.get("decision_at"))
                base = finite(decision.get("base_selected_score"))
                version = decision.get("adaptive_model_version", decision.get("model_version", ""))
                valid_v2 = (version == MODEL_VERSION and base is not None and .5 <= base <= 1
                    and decision.get("candidate_side") == row["side"]
                    and decision.get("round_id") == row["round_id"]
                    and decision_at is not None and decision_at <= numeric["opened_at"] < numeric["settled_at"])
                if source_name == "MAIN_OPTIMIZED":
                    base, provenance, decision_at = numeric["confidence"], "optimized_raw", numeric["opened_at"]
                    exclusion = None
                elif valid_v2:
                    provenance, exclusion = "adaptive_v2_raw", None
                else:
                    base, provenance, exclusion = None, "legacy_adaptive", "missing_raw_base_score"
                    decision_at = numeric["opened_at"]
                if row.get("settlement_source") == "early_exit":
                    exclusion = "nonterminal_exit"
                outcome = row.get("outcome") if row.get("outcome_source") == "polymarket_official" else None
                if row.get("settlement_source") not in (None, "", "polymarket_official"):
                    exclusion = "nonofficial_settlement"
                if valid_v2 and outcome not in ("Up", "Down"):
                    exclusion = "official_outcome_not_available"
                won = int(outcome == row["side"]) if outcome in ("Up", "Down") else int(numeric["pnl"] > 0)
                label_source = "official_outcome" if outcome in ("Up", "Down") else "settled_pnl"
                seconds = finite(decision.get("time_left_seconds", decision.get("remaining_seconds"))) if valid_v2 else None
                ends = finite(row.get("ends_at"))
                if seconds is None and ends is not None:
                    seconds = ends - decision_at
                predicted = finite(decision.get("adaptive_score")) if valid_v2 else None
                quoted = finite(decision.get("entry_price")) if valid_v2 else None
                if source_name == "MAIN_OPTIMIZED" and isinstance(payload.get("signal"), dict):
                    quoted = finite(payload["signal"].get("entry_price"))
                if quoted is not None and not 0 < quoted < 1:
                    quoted = None
                key = f"{source_name}:{row['id']}"
                self.conn.execute("""INSERT OR IGNORE INTO training_samples
                    (source_key,source_name,source_trade_id,round_id,side,confidence,move_bps,
                     entry_price,opened_at,settled_at,pnl,won,imported_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key,source_name,row["id"],row["round_id"],row["side"],numeric["confidence"],
                     numeric["move_bps"],numeric["entry_price"],numeric["opened_at"],numeric["settled_at"],
                     numeric["pnl"],won,now))
                self.conn.execute("""UPDATE training_samples SET base_score=?,provenance=?,
                    decision_at=?,time_left_seconds=?,decision_json=?,predicted_score=?,
                    exclusion_reason=?,label_source=?,won=?,decision_entry_price=? WHERE source_key=?""",
                    (base,provenance,decision_at,seconds,json.dumps(decision,sort_keys=True) if valid_v2 else None,
                     predicted,exclusion,label_source,won,quoted,key))

    def record_observation(self, metadata):
        """One first candidate per market; never a trade or fabricated PnL."""
        if not isinstance(metadata, dict):
            return False
        now = finite(metadata.get("decision_at"))
        ends = finite(metadata.get("market_ends_at"))
        score = finite(metadata.get("base_selected_score"))
        captured_at = time.time()
        side = metadata.get("candidate_side")
        round_id = metadata.get("round_id")
        if (now is None or ends is None or not now <= captured_at < ends or captured_at-now > 30 or not round_id
                or side not in ("Up", "Down") or score is None or not .5 <= score <= 1
                or metadata.get("adaptive_model_version") != MODEL_VERSION):
            return False
        # Only scalar fields, no quote dumps, credentials, or opaque objects.
        names = ("round_id", "candidate_side", "decision_at", "market_ends_at", "base_selected_score",
            "adaptive_score", "adaptive_model_version", "entry_price", "all_in_cost", "min_edge",
            "time_left_seconds", "adaptive_allowed", "adaptive_bucket", "adaptive_candidate_score",
            "adaptive_candidate_delta", "adaptive_candidate_allowed", "adaptive_candidate_expected_edge",
            "adaptive_validation_state", "fee_source")
        payload = {k: metadata[k] for k in names if k in metadata and
                   (metadata[k] is None or isinstance(metadata[k], (str, int, float, bool)))}
        try:
            encoded = json.dumps(payload, allow_nan=False, sort_keys=True)
        except (ValueError, TypeError):
            return False
        with self.lock, self.conn:
            inserted = self.conn.execute("""INSERT OR IGNORE INTO calibration_observations
                (round_id,side,decision_at,ends_at,payload,recorded_at) VALUES(?,?,?,?,?,?)""",
                (round_id, side, now, ends, encoded, captured_at)).rowcount
            self.conn.execute("""DELETE FROM calibration_observations WHERE round_id IN
                (SELECT round_id FROM calibration_observations ORDER BY decision_at DESC LIMIT -1 OFFSET ?)""",
                (OBSERVATION_LIMIT,))
            return bool(inserted)

    def _sync_observations(self):
        unresolved = {r[0] for r in self.conn.execute(
            "SELECT round_id FROM calibration_observations WHERE settled_at IS NULL")}
        if not unresolved:
            return
        for path in (self.seed_path, self.adaptive_path):
            if path is None or not path.exists():
                continue
            try:
                with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as source:
                    source.row_factory = sqlite3.Row
                    # No network or settlement mutations: only already-known official labels.
                    rows = source.execute("""SELECT round_id,outcome,settled_at,settlement_source
                        FROM market_rounds WHERE outcome IN ('Up','Down') AND settled_at IS NOT NULL
                        AND settlement_source='polymarket_official'""").fetchall()
                with self.conn:
                    for row in rows:
                        if row["round_id"] in unresolved:
                            self.conn.execute("""UPDATE calibration_observations SET outcome=?,settled_at=?,label_source=?
                                WHERE round_id=? AND settled_at IS NULL AND decision_at<? AND ends_at<=?""",
                                (row["outcome"],row["settled_at"],row["settlement_source"],row["round_id"],
                                 row["settled_at"],row["settled_at"]))
            except (OSError, sqlite3.Error):
                continue

    def _observation_rows(self):
        rows = []
        for row in self.conn.execute("SELECT * FROM calibration_observations WHERE settled_at IS NOT NULL"):
            payload = json.loads(row["payload"])
            rows.append({"source_key": "SHADOW:"+row["round_id"], "source_name": "SHADOW",
                "round_id": row["round_id"], "side": row["side"], "base_score": payload.get("base_selected_score"),
                "entry_price": payload.get("entry_price"), "opened_at": row["decision_at"],
                "decision_at": row["decision_at"], "settled_at": row["settled_at"],
                "won": int(row["outcome"] == row["side"]), "provenance": "shadow_v2_raw",
                "time_left_seconds": payload.get("time_left_seconds"), "predicted_score": payload.get("adaptive_score"),
                "candidate_score":payload.get("adaptive_candidate_score"),"all_in_cost":payload.get("all_in_cost"),
                "min_edge":payload.get("min_edge"),"label_source":row["label_source"],"recorded_at":row["recorded_at"],
                "model_version":payload.get("adaptive_model_version"),"ends_at":row["ends_at"],
                "fee_source":payload.get("fee_source")})
        return rows

    def sync(self, *, force=False):
        with self.lock:
            now = time.time()
            if not force and now - self._last_sync < 5.:
                return
            self._import_rows(self.seed_path, "MAIN_OPTIMIZED")
            if self.adaptive_path and self.adaptive_path.resolve() != self.seed_path.resolve():
                self._import_rows(self.adaptive_path, "MAIN_ADAPTIVE")
            self._sync_observations()
            self._rows = [dict(r) for r in self.conn.execute("SELECT * FROM training_samples ORDER BY settled_at,source_key")]
            self._shadow_rows = self._observation_rows()
            self._last_sync = now
            with self.conn:
                for key, value in (("model_version", MODEL_VERSION), ("last_sync_at", str(now))):
                    self.conn.execute("INSERT OR REPLACE INTO model_meta(key,value) VALUES(?,?)", (key,value))

    def adjust_prediction(self, prediction, market, now):
        """Preserve raw provenance; cost-aware calibration occurs exactly once later."""
        base_up = finite(prediction.get("base_up_score", prediction.get("up_score")))
        if base_up is None or not 0 <= base_up <= 1:
            return prediction
        side = "Up" if base_up >= .5 else "Down"
        base = base_up if side == "Up" else 1-base_up
        return {**prediction, "base_up_score":base_up, "base_selected_score":base,
                "up_score":base_up, "candidate_side":side, "adaptive_score":base,
                "adaptive_delta":0., "adaptive_samples":0, "adaptive_state":"AWAITING_COST",
                "adaptive_model_version":MODEL_VERSION, "adaptive_observed_at":now}

    def assess_entry(self, prediction, market, now, *, entry_price, all_in_cost, min_edge):
        self.sync()
        base = self.adjust_prediction(prediction, market, now)
        ends = finite(getattr(market, "ends_at", None))
        with self.lock:
            result = calibrate_entry(self._rows+self._shadow_rows,
                base_score=base.get("base_selected_score"),side=base.get("candidate_side"),now=now,
                round_id=getattr(market,"round_id",None),entry_price=entry_price,all_in_cost=all_in_cost,
                min_edge=min_edge,time_left_seconds=ends-now if ends is not None else None)
            validation = validate_candidate(self._shadow_rows, now=now, round_id=getattr(market,"round_id",None))
            for name in ("score", "delta", "allowed", "expected_edge"):
                result["adaptive_candidate_"+name] = result["adaptive_"+name]
            result.update(adaptive_validation_state=validation["state"],
                          adaptive_validation_reason=validation["reason"],
                          adaptive_validation_samples=validation["samples"])
            if not validation["passed"]:
                result.update(adaptive_score=base["base_selected_score"],adaptive_delta=0.,
                    adaptive_expected_edge=base["base_selected_score"]-all_in_cost,
                    adaptive_allowed=base["base_selected_score"]-all_in_cost+1e-12 >= min_edge)
            result["adaptive_reason"] = validation["reason"] + "；含费优势 " + format(result["adaptive_expected_edge"],".4f")
            result["base_up_score"] = base.get("base_up_score")
            result["up_score"] = result["adaptive_score"] if result["candidate_side"] == "Up" else 1-result["adaptive_score"]
            result["market_ends_at"] = ends
            result["adaptive_model_health"] = "SOURCE_WARNING" if self._source_errors else "OK"
            self._last_assessment = dict(result)
            return result

    def snapshot(self):
        self.sync()
        now = time.time()
        with self.lock:
            sources = Counter(r["source_name"] for r in self._rows)
            canonical, counts = canonical_samples(self._rows+self._shadow_rows, now=now)
            raw = [(r["base_score"]-r["won"])**2 for r in canonical]
            predictions = [r for r in canonical if finite(r.get("predicted_score")) is not None]
            # Descriptive as-of Brier scores, never in-sample fitted-profit claims.
            quality = {"sample_count": len(canonical), "paired_prediction_samples":len(predictions),
                "raw_brier": sum(raw)/len(raw) if raw else None,
                "paired_raw_brier": sum((r["base_score"]-r["won"])**2 for r in predictions)/len(predictions) if predictions else None,
                "adaptive_brier": sum((r["predicted_score"]-r["won"])**2 for r in predictions)/len(predictions) if predictions else None,
                "calibration_gap":sum(r["base_score"]-r["won"] for r in canonical)/len(canonical) if canonical else None}
            shadow_total = self.conn.execute("SELECT count(*) FROM calibration_observations").fetchone()[0]
            validation = validate_candidate(self._shadow_rows,now=now)
            return {"model_version":MODEL_VERSION,"model_path":str(self.path),"seed_path":str(self.seed_path),
                "adaptive_path":str(self.adaptive_path) if self.adaptive_path else None,
                "total_samples":len(self._rows),"seed_samples":sources["MAIN_OPTIMIZED"],
                "adaptive_samples":sources["MAIN_ADAPTIVE"],"usable_samples":counts["usable_samples"],
                "unique_markets":counts["canonical_samples"],"canonical_samples":counts["canonical_samples"],
                "excluded_legacy_samples":counts["excluded_legacy_samples"],"exclusions":counts["exclusions"],
                "effective_samples":counts["recent_effective_samples"],"half_life_hours":72,
                "max_downward_adjustment":MAX_DOWNWARD_ADJUSTMENT,"max_upward_adjustment":MAX_UPWARD_ADJUSTMENT,
                "max_adjustment":MAX_ADJUSTMENT,"min_exact_bucket_samples":MIN_EXACT_BUCKET_SAMPLES,
                "min_adjustment_samples":MIN_ADJUSTMENT_SAMPLES,"last_sync_at":self._last_sync,
                "latest_settled_at":max((r["settled_at"] for r in canonical),default=None),
                "shadow_observations":shadow_total,"shadow_resolved":len(self._shadow_rows),
                "state":"READY" if counts["recent_effective_samples"]>=MIN_ADJUSTMENT_SAMPLES else "WARMING",
                "health":"SOURCE_WARNING" if self._source_errors else "OK","source_errors":dict(self._source_errors),
                "policy":dict(POLICY),"recent_quality":quality,"validation":validation,
                "validation_state":validation["state"],"last_assessment":dict(self._last_assessment)}

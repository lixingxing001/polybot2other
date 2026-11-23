"""Causal, abstaining Paper selector for MAIN-R1's four independent shadows.

All numbers below are frozen research assumptions, not fitted optima or a
statistical guarantee. Features use already received reference ticks, not dates.
Learning uses complete four-arm opportunity cohorts, including rejected/no-trade
opportunities. A cohort becomes available only when ``observe`` actually runs.
Scores use a zero-shrunk, cost-stressed mean minus a time-block standard-error
penalty. Blocks can remain correlated; this is not an out-of-sample proof.

The +0.01 stress formula is a historical fixed-budget approximation. It does not
replay order-book levels, changed quantities, market minimums or later decisions.
Every round, including an abstention, is immutable after its first choice. All
four shadows must keep running while the portfolio abstains.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from numbers import Real
import time
from types import MappingProxyType


CANDIDATES = ("MAIN", "MAIN_FLOOR_060", "FAST", "PLUS")
REGIMES = ("trend", "range", "high_vol", "neutral")
SELECTOR_CONFIG = {
    "version": "main-r1-selector-v1",
    "history_window_ms": 300_000,
    "history_min_points": 30,
    "history_min_coverage_ms": 270_000,
    "history_max_gap_ms": 30_000,
    "reference_max_age_ms": 3_000,
    "frame_max_age_ms": 3_000,
    "book_source_max_age_ms": 3_000,
    "book_received_max_age_ms": 2_500,
    "liquidity_max_spread": .03,
    "liquidity_price_band": .01,
    "liquidity_min_depth_cash": 10.0,
    "trend_efficiency_min": .55,
    "range_efficiency_max": .25,
    "high_volatility_bps": 20.0,
    "lookback_seconds": 7 * 86_400,
    "max_training_cohorts": 1_440,
    "min_regime_cohorts": 60,
    "min_time_blocks": 12,
    "min_active_entries": 20,
    "block_seconds": 1_800,
    "round_seconds": 300,
    "shrinkage_cohorts": 60.0,
    "standard_error_multiple": 2.58,
    "stress_per_share": .01,
    "switch_margin": .005,
    "switch_confirmations": 3,
    "min_hold_rounds": 6,
}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _finite(value, name, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} is below its minimum")
    return value


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _configuration(config):
    supplied = {} if config is None else dict(config)
    if set(supplied) - set(SELECTOR_CONFIG):
        raise ValueError("Unknown MAIN-R1 selector configuration keys")
    result = {**SELECTOR_CONFIG, **supplied}
    if result["version"] != SELECTOR_CONFIG["version"]:
        raise ValueError("Unsupported MAIN-R1 selector version")
    integers = {k for k, v in SELECTOR_CONFIG.items() if type(v) is int}
    for key in integers:
        if type(result[key]) is not int or result[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in set(result) - integers - {"version"}:
        result[key] = _finite(result[key], key, minimum=0)
    if (result["round_seconds"] != 300 or result["block_seconds"] % 300
            or result["min_time_blocks"] < 2 or result["history_min_points"] < 2
            or result["history_min_coverage_ms"] > result["history_window_ms"]
            or result["min_regime_cohorts"] > result["max_training_cohorts"]
            or not 0 <= result["range_efficiency_max"] < result["trend_efficiency_min"] <= 1
            or not 0 < result["liquidity_max_spread"] < 1
            or not 0 < result["liquidity_price_band"] < 1
            or result["liquidity_min_depth_cash"] <= 0
            or result["high_volatility_bps"] <= 0
            or result["standard_error_multiple"] <= 0
            or result["stress_per_share"] != .01):
        raise ValueError("Invalid frozen MAIN-R1 selector configuration")
    return result


class MainR1Selector:
    def __init__(self, store, *, history_reader, clock=time.time, config=None):
        self.store, self.conn, self._lock = store, store.conn, store._lock
        if not callable(history_reader) or not callable(clock):
            raise TypeError("history_reader and clock must be callable")
        self.history_reader, self.clock = history_reader, clock
        frozen = _configuration(config)
        self.config = MappingProxyType(frozen)
        config_json = _json(frozen)
        self.config_hash = hashlib.sha256(config_json.encode()).hexdigest()
        with self._transaction():
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS main_r1_selector_meta (
                    id INTEGER PRIMARY KEY CHECK(id=1), config_hash TEXT NOT NULL,
                    config_json TEXT NOT NULL, state_json TEXT NOT NULL,
                    last_event_at REAL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS main_r1_selector_cohorts (
                    round_id TEXT PRIMARY KEY, started_at REAL NOT NULL,
                    ends_at REAL NOT NULL, regime TEXT NOT NULL,
                    feature_time REAL NOT NULL, available_at REAL NOT NULL,
                    config_hash TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS main_r1_selector_decisions (
                    round_id TEXT PRIMARY KEY, selection_id TEXT NOT NULL UNIQUE,
                    decision_at REAL NOT NULL, started_at REAL NOT NULL,
                    ends_at REAL NOT NULL, regime TEXT, candidate_key TEXT,
                    config_hash TEXT NOT NULL, payload_json TEXT NOT NULL
                )
            """)
            state = {"incumbent": None, "incumbent_since": None,
                     "pending_candidate": None, "pending_regime": None,
                     "pending_confirmations": 0, "last_confirmation_block": None}
            self.conn.execute("""
                INSERT OR IGNORE INTO main_r1_selector_meta
                (id,config_hash,config_json,state_json) VALUES (1,?,?,?)
            """, (self.config_hash, config_json, _json(state)))
            if self._meta()["config_hash"] != self.config_hash:
                raise ValueError("Frozen selector config differs; use a new independent campaign")

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self.conn.in_transaction:
                raise RuntimeError("MAIN-R1 selector mutation requires its own transaction")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _one(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        row = cursor.fetchone()
        return None if row is None else dict(zip((d[0] for d in cursor.description), row))

    def _meta(self):
        return self._one("SELECT * FROM main_r1_selector_meta WHERE id=1")

    @staticmethod
    def _check_clock(meta, now):
        if meta["last_event_at"] is not None and now < meta["last_event_at"]:
            raise ValueError("MAIN-R1 selector clock moved backwards")

    def _market(self, frame):
        market = _field(frame, "market")
        round_id = _field(market, "round_id")
        if not isinstance(round_id, str) or not round_id:
            raise ValueError("A market round_id is required")
        start = _finite(_field(market, "started_at"), "started_at", minimum=0)
        end = _finite(_field(market, "ends_at"), "ends_at", minimum=0)
        if abs(end - start - self.config["round_seconds"]) > 1e-6:
            raise ValueError("Selector supports five-minute market rounds only")
        return market, round_id, start, end

    def _features(self, frame, market, start, end, now):
        config = self.config
        observed = _finite(_field(frame, "observed_at"), "frame.observed_at", minimum=0)
        if not start <= observed <= now < end:
            raise ValueError("frame_outside_current_market")
        if 1000 * (now-observed) > config["frame_max_age_ms"] or _field(frame, "error"):
            raise ValueError("frame_unready_or_stale")
        reference = _field(frame, "reference", {})
        if not reference.get("ready") or not reference.get("protocol_verified"):
            raise ValueError("reference_unverified")
        end_ms = observed * 1000
        start_ms = end_ms - config["history_window_ms"]
        try:
            ticks = list(self.history_reader(start_ms, end_ms))
        except Exception as exc:
            raise ValueError(f"reference_history_unavailable:{type(exc).__name__}") from exc
        if len(ticks) > 20_000:
            raise ValueError("reference_history_exceeds_limit")
        points = {}
        for tick in ticks:
            source = _finite(_field(tick, "source_ms"), "source_ms", minimum=0)
            received = _finite(_field(tick, "received_ms"), "received_ms", minimum=0)
            price = _finite(_field(tick, "price"), "reference price", minimum=0)
            if price <= 0:
                raise ValueError("invalid_reference_price")
            if source > end_ms or received > end_ms:
                raise ValueError("reference_history_contains_future_ticks")
            if source < start_ms:
                continue
            previous = points.get(source)
            if previous is None or received > previous[0]:
                points[source] = (received, price)
            elif received == previous[0] and price != previous[1]:
                raise ValueError("conflicting_reference_tick")
        ordered = sorted(points.items())
        if len(ordered) < config["history_min_points"]:
            raise ValueError("insufficient_reference_history")
        stamps = [x[0] for x in ordered]
        coverage = stamps[-1] - stamps[0]
        gap = max(b-a for a, b in zip(stamps, stamps[1:]))
        if coverage < config["history_min_coverage_ms"] or gap > config["history_max_gap_ms"]:
            raise ValueError("incomplete_reference_history")
        if (end_ms-stamps[-1] > config["reference_max_age_ms"]
                or end_ms-ordered[-1][1][0] > config["reference_max_age_ms"]):
            raise ValueError("stale_reference_history")
        prices = [x[1][1] for x in ordered]
        changes = [math.log(b/a) for a, b in zip(prices, prices[1:])]
        path = math.fsum(abs(v) for v in changes)
        efficiency = abs(math.log(prices[-1]/prices[0])) / path if path else 0.0
        volatility = math.sqrt(math.fsum(v*v for v in changes)) * 10_000
        liquidity = {}
        quotes = _field(frame, "quotes", {})
        for side, token_field in (("Up", "up_token"), ("Down", "down_token")):
            quote = quotes.get(side)
            if not isinstance(quote, dict):
                raise ValueError("missing_two_sided_book")
            if str(quote.get("token_id")) != str(_field(market, token_field)):
                raise ValueError("book_token_mismatch")
            source = _finite(quote.get("source_ms"), "book source_ms", minimum=0)
            received = _finite(quote.get("received_ms"), "book received_ms", minimum=0)
            if (not 0 <= end_ms-source <= config["book_source_max_age_ms"]
                    or not 0 <= end_ms-received <= config["book_received_max_age_ms"]):
                raise ValueError("book_stale_or_future")
            ask = _finite(quote.get("best_ask"), "best_ask")
            bid = _finite(quote.get("best_bid"), "best_bid")
            if not 0 < bid <= ask < 1 or ask-bid > config["liquidity_max_spread"]+1e-12:
                raise ValueError("book_crossed_or_wide")
            depth = {}
            for name in ("asks", "bids"):
                levels = quote.get(name)
                if not isinstance(levels, (list, tuple)) or not levels:
                    raise ValueError("missing_executable_depth")
                amounts = []
                for level in levels:
                    price = _finite(level.get("price") if isinstance(level, dict) else level[0], "level price")
                    size = _finite(level.get("size") if isinstance(level, dict) else level[1], "level size", minimum=0)
                    if not 0 < price < 1:
                        raise ValueError("invalid_depth_price")
                    if name == "asks":
                        if price < ask-1e-12:
                            raise ValueError("inconsistent_best_ask")
                        keep = price <= ask+config["liquidity_price_band"]+1e-12
                    else:
                        if price > bid+1e-12:
                            raise ValueError("inconsistent_best_bid")
                        keep = price >= bid-config["liquidity_price_band"]-1e-12
                    if keep:
                        amounts.append(price*size)
                depth[name] = math.fsum(amounts)
                if depth[name] < config["liquidity_min_depth_cash"]:
                    raise ValueError("thin_executable_depth")
            liquidity[side] = {"spread": ask-bid, "ask_depth_cash": depth["asks"],
                               "bid_depth_cash": depth["bids"]}
        regime = ("high_vol" if volatility >= config["high_volatility_bps"] else
                  "trend" if efficiency >= config["trend_efficiency_min"] else
                  "range" if efficiency <= config["range_efficiency_max"] else "neutral")
        return {"regime": regime, "feature_time": observed,
                "history_points": len(ordered), "history_coverage_ms": coverage,
                "history_max_gap_ms": gap, "reference_last_source_ms": stamps[-1],
                "trend_efficiency": efficiency, "realized_volatility_bps": volatility,
                "liquidity": liquidity}

    def _normalize_cohort(self, cohort):
        expected = {"round_id", "started_at", "ends_at", "regime", "feature_time", "coverage_complete", "arms"}
        if not isinstance(cohort, dict) or set(cohort) != expected:
            raise ValueError("Cohort fields must match the public schema; available_at is internal")
        if (cohort["coverage_complete"] is not True or not isinstance(cohort["arms"], dict)
                or set(cohort["arms"]) != set(CANDIDATES)):
            raise ValueError("Only complete four-arm common cohorts can be observed")
        if not isinstance(cohort["round_id"], str) or not cohort["round_id"]:
            raise ValueError("Cohort round_id is required")
        if cohort["regime"] not in REGIMES:
            raise ValueError("Cohort must have a known, previously observed regime")
        start = _finite(cohort["started_at"], "started_at", minimum=0)
        end = _finite(cohort["ends_at"], "ends_at", minimum=0)
        feature = _finite(cohort["feature_time"], "feature_time", minimum=0)
        if abs(end-start-self.config["round_seconds"]) > 1e-6 or not start <= feature < end:
            raise ValueError("Cohort times are inconsistent")
        arms = {}
        for key in CANDIDATES:
            arm = cohort["arms"][key]
            if not isinstance(arm, dict) or set(arm) != {"status", "requested_budget", "cost", "shares", "pnl"}:
                raise ValueError("Invalid arm result fields")
            status = arm["status"]
            if status not in ("SETTLED", "NO_TRADE"):
                raise ValueError("Unsettled/unknown arms cannot enter a common cohort")
            amounts = {k: _finite(arm[k], k, minimum=None if k == "pnl" else 0)
                       for k in ("requested_budget", "cost", "shares", "pnl")}
            if status == "NO_TRADE":
                if any(amounts.values()):
                    raise ValueError("NO_TRADE arm amounts must all be zero")
            elif (min(amounts[k] for k in ("requested_budget", "cost", "shares")) <= 0
                  or amounts["pnl"] < -amounts["cost"]-1e-6
                  or amounts["pnl"]+amounts["cost"] > amounts["shares"]+1e-6):
                raise ValueError("Invalid settled arm cash/share result")
            arms[key] = {"status": status, **amounts}
        return {"round_id": cohort["round_id"], "started_at": start, "ends_at": end,
                "regime": cohort["regime"], "feature_time": feature,
                "coverage_complete": True, "arms": arms}

    def observe(self, cohort):
        """Insert once at the actual observation clock; contradictory repeats fail."""
        value = self._normalize_cohort(cohort)
        now = _finite(self.clock(), "clock", minimum=0)
        if value["ends_at"] > now:
            raise ValueError("A future/unended market cannot be a settled cohort")
        payload = _json(value)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._transaction():
            meta = self._meta()
            self._check_clock(meta, now)
            previous = self._one("SELECT * FROM main_r1_selector_cohorts WHERE round_id=?", (value["round_id"],))
            if previous:
                if previous["payload_hash"] != digest:
                    raise ValueError("Contradictory repeated cohort; historical learning is immutable")
                return {"inserted": False, "round_id": value["round_id"], "available_at": previous["available_at"]}
            decision = self._one("SELECT * FROM main_r1_selector_decisions WHERE round_id=?", (value["round_id"],))
            if not decision or decision["decision_at"] >= value["ends_at"]:
                raise ValueError("Cohort requires a pre-recorded decision before market end")
            chosen = json.loads(decision["payload_json"])
            if (decision["regime"] not in REGIMES or decision["regime"] != value["regime"]
                    or decision["started_at"] != value["started_at"]
                    or decision["ends_at"] != value["ends_at"]
                    or chosen["features"].get("feature_time") != value["feature_time"]):
                raise ValueError("Cohort disagrees with its recorded decision-time regime/frame")
            self.conn.execute("""
                INSERT INTO main_r1_selector_cohorts
                (round_id,started_at,ends_at,regime,feature_time,available_at,config_hash,payload_hash,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (value["round_id"], value["started_at"], value["ends_at"], value["regime"],
                  value["feature_time"], now, self.config_hash, digest, payload))
            self.conn.execute("UPDATE main_r1_selector_meta SET last_event_at=? WHERE id=1", (now,))
            return {"inserted": True, "round_id": value["round_id"], "available_at": now}

    def _training(self, regime, now, round_id):
        cursor = self.conn.execute("""
            SELECT payload_json,available_at FROM main_r1_selector_cohorts
            WHERE regime=? AND ends_at<? AND available_at<? AND ends_at>=? AND round_id<>?
            ORDER BY ends_at DESC,round_id DESC LIMIT ?
        """, (regime, now, now, now-self.config["lookback_seconds"], round_id,
              self.config["max_training_cohorts"]))
        rows = []
        for payload, available in cursor.fetchall():
            row = json.loads(payload)
            block = int(row["started_at"] // self.config["block_seconds"])
            if (block+1)*self.config["block_seconds"] > now:
                continue  # A partially elapsed block cannot confirm a regime switch.
            row["block"] = block
            row["available_at"] = available
            rows.append(row)
        return list(reversed(rows))

    def _reward(self, arm, stressed=True):
        if arm["status"] == "NO_TRADE":
            return 0.0
        extra = self.config["stress_per_share"] * arm["shares"] if stressed else 0.0
        value = (arm["pnl"]-extra) / max(arm["requested_budget"], arm["cost"]+extra)
        if not math.isfinite(value):
            raise ValueError("Nonfinite normalized cohort return")
        return value

    def _estimate(self, values, blocks):
        n = len(values)
        if not n:
            return {"mean": None, "shrunk_mean": None, "block_standard_error": None, "lower_score": None}
        mean = math.fsum(values)/n
        residuals = {}
        for value, block in zip(values, blocks):
            residuals.setdefault(block, []).append(value-mean)
        b = len(residuals)
        se = (math.sqrt((b/(b-1))*math.fsum(math.fsum(v)**2 for v in residuals.values()))/n) if b > 1 else None
        shrunk = mean*n/(n+self.config["shrinkage_cohorts"])
        return {"mean": mean, "shrunk_mean": shrunk, "block_standard_error": se,
                "lower_score": shrunk-self.config["standard_error_multiple"]*se if se is not None else None}

    def _scores(self, rows):
        blocks = [r["block"] for r in rows]
        result, rewards = {}, {}
        for key in CANDIDATES:
            values = [self._reward(r["arms"][key]) for r in rows]
            rewards[key] = values
            estimate = self._estimate(values, blocks)
            active = sum(r["arms"][key]["status"] == "SETTLED" for r in rows)
            sufficient = (len(rows) >= self.config["min_regime_cohorts"]
                          and len(set(blocks)) >= self.config["min_time_blocks"]
                          and active >= self.config["min_active_entries"])
            result[key] = {**estimate, "cohorts": len(rows), "time_blocks": len(set(blocks)),
                           "active_entries": active, "sufficient_history": sufficient,
                           "eligible": sufficient and estimate["lower_score"] is not None and estimate["lower_score"] > 0,
                           "mean_net_return": math.fsum(self._reward(r["arms"][key], False) for r in rows)/len(rows) if rows else None}
        return result, rewards, blocks

    @staticmethod
    def _clear_pending(state):
        state.update(pending_candidate=None, pending_regime=None, pending_confirmations=0,
                     last_confirmation_block=None)

    def _select(self, state, scores, rewards, blocks, regime, start):
        qualified = [k for k in CANDIDATES if scores[k]["eligible"]]
        incumbent = state["incumbent"]
        current_ok = incumbent in qualified
        if not qualified:
            self._clear_pending(state)
            sufficient = any(v["sufficient_history"] for v in scores.values())
            return None, "no_positive_cost_stressed_edge" if sufficient else "cold_start_or_insufficient_regime_history"
        best = max(qualified, key=lambda k: scores[k]["lower_score"])
        if best == incumbent:
            self._clear_pending(state)
            return incumbent, "retained_incumbent"
        if incumbent is not None:
            paired = self._estimate([a-b for a, b in zip(rewards[best], rewards[incumbent])], blocks)
            scores[best]["paired_vs_incumbent"] = paired
            if paired["lower_score"] is None or paired["lower_score"] <= self.config["switch_margin"]:
                self._clear_pending(state)
                return (incumbent if current_ok else None), "insufficient_paired_switch_advantage"
        latest_block = max(blocks)
        if state["pending_candidate"] != best or state["pending_regime"] != regime:
            state.update(pending_candidate=best, pending_regime=regime,
                         pending_confirmations=1, last_confirmation_block=latest_block)
        elif latest_block > state["last_confirmation_block"]:
            state["pending_confirmations"] += 1
            state["last_confirmation_block"] = latest_block
        held_long_enough = (incumbent is None or
                            start-state["incumbent_since"] >= self.config["min_hold_rounds"]*self.config["round_seconds"])
        confirmed = state["pending_confirmations"] >= self.config["switch_confirmations"]
        if confirmed and held_long_enough:
            state.update(incumbent=best, incumbent_since=start)
            self._clear_pending(state)
            return best, "selected_after_block_confirmation"
        return (incumbent if current_ok else None), "waiting_switch_confirmation" if not confirmed else "minimum_hold_hysteresis"

    def choose(self, frame):
        """Choose once per round; even an unavailable/cold/negative-edge round stays flat."""
        market, round_id, start, end = self._market(frame)
        with self._transaction():
            now = _finite(self.clock(), "clock", minimum=0)
            meta = self._meta()
            self._check_clock(meta, now)
            existing = self._one("SELECT * FROM main_r1_selector_decisions WHERE round_id=?", (round_id,))
            if existing:
                if existing["started_at"] != start or existing["ends_at"] != end:
                    raise ValueError("Round identity conflicts with its frozen decision")
                if now < existing["decision_at"]:
                    raise ValueError("MAIN-R1 selector clock moved backwards")
                return json.loads(existing["payload_json"])
            state = json.loads(meta["state_json"])
            candidate, scores, regime = None, {}, None
            try:
                features = self._features(frame, market, start, end, now)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
                observed = _field(frame, "observed_at")
                observed = float(observed) if isinstance(observed, Real) and not isinstance(observed, bool) and math.isfinite(observed) else None
                features = {"feature_time": observed, "unavailable_reason": str(exc)}
                reason = "features_unavailable"
                self._clear_pending(state)
            else:
                regime = features["regime"]
                rows = self._training(regime, now, round_id)
                scores, rewards, blocks = self._scores(rows)
                candidate, reason = self._select(state, scores, rewards, blocks, regime, start)
                features["training_latest_available_at"] = max((r["available_at"] for r in rows), default=None)
                features["training_latest_round_end"] = max((r["ends_at"] for r in rows), default=None)
            decision = {"candidate_key": candidate, "selection_id": "r1sel_"+hashlib.sha256(
                            _json([self.config_hash, round_id, now]).encode()).hexdigest()[:24],
                        "regime": regime, "reason": reason, "scores": scores, "features": features,
                        "round_id": round_id, "decision_at": now, "config_hash": self.config_hash,
                        "locked": True, "selection_state": dict(state)}
            payload = _json(decision)
            self.conn.execute("""
                INSERT INTO main_r1_selector_decisions
                (round_id,selection_id,decision_at,started_at,ends_at,regime,candidate_key,config_hash,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (round_id, decision["selection_id"], now, start, end, regime, candidate,
                  self.config_hash, payload))
            self.conn.execute("UPDATE main_r1_selector_meta SET state_json=?,last_event_at=? WHERE id=1",
                              (_json(state), now))
            return decision

    def status(self):
        """Read-only state; it neither retrains nor advances hysteresis/round locks."""
        with self._lock:
            meta = self._meta()
            last = self._one("SELECT payload_json FROM main_r1_selector_decisions ORDER BY decision_at DESC,round_id DESC LIMIT 1")
            return {"mode": "PAPER_RESEARCH", "config_hash": self.config_hash,
                    "config": dict(self.config), "state": json.loads(meta["state_json"]),
                    "cohorts": self._one("SELECT COUNT(*) AS n FROM main_r1_selector_cohorts")["n"],
                    "decisions": self._one("SELECT COUNT(*) AS n FROM main_r1_selector_decisions")["n"],
                    "last_decision": json.loads(last["payload_json"]) if last else None,
                    "cold_start_policy": "ABSTAIN; all four shadows continue",
                    "method_limit": "Frozen Paper ranking assumptions; block dependence, selection uncertainty and execution error remain."}

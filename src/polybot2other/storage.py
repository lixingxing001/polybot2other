from __future__ import annotations

import json
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from .models import MarketRound, PaperFill, PaperFillLevel, TradeIntent


SCHEMA_VERSION = 9
ACTIVE_ORDER_STATUSES = ("RESTING", "PARTIAL_RESTING", "PENDING")
PAPER_MIN_RESTING_FILL_CASH = 0.01
PAPER_DUST_RELEASE_CASH = 0.05
PAPER_MIN_OPEN_TRADE_STAKE = 0.01
CHAINLINK_FALLBACK_SETTLEMENT_MAX_AGE_SECONDS = 5.0
SETTLEMENT_SOURCE_POLYMARKET = "polymarket_official"
SETTLEMENT_SOURCE_CHAINLINK = "chainlink_fallback"
SETTLEMENT_SOURCE_EARLY_EXIT = "early_exit"
PAPER_ORDER_STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "all": (),
    "active": ACTIVE_ORDER_STATUSES,
    "filled": ("FILLED", "PARTIAL"),
    "canceled": ("CANCELED",),
    "expired": ("EXPIRED",),
    "rejected": ("REJECTED",),
}


def normalize_paper_order_status_filter(value: str | None) -> str:
    normalized = str(value or "all").strip().lower().replace("-", "_")
    if normalized not in PAPER_ORDER_STATUS_FILTERS:
        allowed = ", ".join(sorted(PAPER_ORDER_STATUS_FILTERS))
        raise ValueError(f"status must be one of {allowed}")
    return normalized


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapper(self: "TradeStore", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class TradeStore:
    def __init__(self, db_path: Path, initial_balance: float) -> None:
        self.db_path = db_path
        self.initial_balance = round(float(initial_balance), 2)
        self._lock = threading.RLock()
        self.conn = connect(db_path)
        self._init_schema()
        self._init_account()

    @_locked
    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_balance REAL NOT NULL,
                cash_balance REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS market_rounds (
                round_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                started_at REAL NOT NULL,
                ends_at REAL NOT NULL,
                target_price REAL NOT NULL,
                question TEXT,
                condition_id TEXT,
                up_token TEXT,
                down_token TEXT,
                url TEXT,
                final_price REAL,
                outcome TEXT,
                settled_at REAL,
                settlement_source TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                stake REAL NOT NULL,
                entry_price REAL NOT NULL,
                shares REAL NOT NULL,
                confidence REAL NOT NULL,
                move_bps REAL NOT NULL,
                status TEXT NOT NULL,
                opened_at REAL NOT NULL,
                settled_at REAL,
                exit_price REAL,
                payout REAL,
                pnl REAL,
                settlement_source TEXT,
                reason TEXT NOT NULL,
                FOREIGN KEY(round_id) REFERENCES market_rounds(round_id)
            );

            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                status TEXT NOT NULL,
                limit_price REAL,
                post_only INTEGER NOT NULL DEFAULT 0,
                expires_at REAL,
                requested_cash REAL NOT NULL,
                reserved_cash REAL NOT NULL DEFAULT 0,
                remaining_cash REAL NOT NULL DEFAULT 0,
                filled_shares REAL NOT NULL DEFAULT 0,
                avg_fill_price REAL,
                notional REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                cash_spent REAL NOT NULL DEFAULT 0,
                trade_id INTEGER,
                execution_mode TEXT NOT NULL DEFAULT 'PAPER',
                external_order_id TEXT,
                client_order_id TEXT,
                external_status TEXT,
                raw_response TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                move_bps REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(round_id) REFERENCES market_rounds(round_id),
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            );

            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                trade_id INTEGER,
                level_index INTEGER NOT NULL,
                price REAL NOT NULL,
                shares REAL NOT NULL,
                notional REAL NOT NULL,
                fee REAL NOT NULL,
                cash_spent REAL NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(order_id) REFERENCES paper_orders(id),
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            );

            CREATE INDEX IF NOT EXISTS idx_paper_orders_round_created
                ON paper_orders(round_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_paper_orders_symbol_created_at
                ON paper_orders(symbol, created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_paper_orders_trade_id
                ON paper_orders(trade_id);

            CREATE INDEX IF NOT EXISTS idx_paper_fills_order_id
                ON paper_fills(order_id, level_index);

            CREATE INDEX IF NOT EXISTS idx_trades_status_opened_at
                ON trades(status, opened_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_trades_symbol_activity_at
                ON trades(symbol, COALESCE(settled_at, opened_at) DESC, id DESC);

            CREATE TABLE IF NOT EXISTS price_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_price_ticks_symbol_created_at
                ON price_ticks(symbol, created_at DESC);

            CREATE TABLE IF NOT EXISTS equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cash_balance REAL NOT NULL,
                open_risk REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                total_equity REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_equity_curve_created_at
                ON equity_curve(created_at DESC);

            CREATE TABLE IF NOT EXISTS llm_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                route TEXT NOT NULL,
                allow_trade INTEGER NOT NULL,
                confidence REAL NOT NULL,
                market_regime TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL,
                features_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                error TEXT,
                valid_until REAL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_llm_decisions_round_created
                ON llm_decisions(round_id, created_at DESC);
            """
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._ensure_column("market_rounds", "question", "TEXT")
        self._ensure_column("market_rounds", "condition_id", "TEXT")
        self._ensure_column("market_rounds", "up_token", "TEXT")
        self._ensure_column("market_rounds", "down_token", "TEXT")
        self._ensure_column("market_rounds", "url", "TEXT")
        self._ensure_column("market_rounds", "settlement_source", "TEXT")
        self._ensure_column("trades", "settlement_source", "TEXT")
        self._ensure_column("paper_orders", "post_only", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("paper_orders", "expires_at", "REAL")
        self._ensure_column("paper_orders", "reserved_cash", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("paper_orders", "remaining_cash", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("paper_orders", "execution_mode", "TEXT NOT NULL DEFAULT 'PAPER'")
        self._ensure_column("paper_orders", "external_order_id", "TEXT")
        self._ensure_column("paper_orders", "client_order_id", "TEXT")
        self._ensure_column("paper_orders", "external_status", "TEXT")
        self._ensure_column("paper_orders", "raw_response", "TEXT")
        self._ensure_column("paper_orders", "confidence", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("paper_orders", "move_bps", "REAL NOT NULL DEFAULT 0")
        self.conn.commit()

    @_locked
    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in rows):
            return
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @_locked
    def _init_account(self) -> None:
        now = time.time()
        row = self.conn.execute("SELECT id FROM account WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO account(id, initial_balance, cash_balance, realized_pnl, created_at, updated_at)
                VALUES(1, ?, ?, 0, ?, ?)
                """,
                (self.initial_balance, self.initial_balance, now, now),
            )
            self.conn.commit()
            self.record_equity()

    @_locked
    def save_price_tick(self, symbol: str, price: float, source: str, created_at: float) -> None:
        self.conn.execute(
            "INSERT INTO price_ticks(symbol, price, source, created_at) VALUES(?, ?, ?, ?)",
            (symbol, price, source, created_at),
        )
        self.conn.commit()

    @_locked
    def record_llm_decision(
        self,
        *,
        round_id: str,
        variant_id: str,
        decision: dict[str, Any],
        features: dict[str, Any],
        created_at: float | None = None,
    ) -> int:
        now = time.time() if created_at is None else float(created_at)
        response = decision.get("raw_response") if isinstance(decision.get("raw_response"), dict) else {}
        cur = self.conn.execute(
            """
            INSERT INTO llm_decisions(
                round_id, variant_id, route, allow_trade, confidence, market_regime,
                source, reason, reason_codes_json, features_json, response_json,
                error, valid_until, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(round_id),
                str(variant_id),
                str(decision.get("route") or ""),
                1 if decision.get("allow_trade") else 0,
                float(decision.get("confidence") or 0.0),
                str(decision.get("market_regime") or ""),
                str(decision.get("source") or ""),
                str(decision.get("reason") or ""),
                json.dumps(decision.get("reason_codes") or [], ensure_ascii=False, sort_keys=True),
                json.dumps(features, ensure_ascii=False, sort_keys=True),
                json.dumps(response, ensure_ascii=False, sort_keys=True),
                decision.get("error"),
                decision.get("valid_until"),
                now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @_locked
    def recent_llm_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        rows = self.conn.execute(
            """
            SELECT *
            FROM llm_decisions
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def llm_decision_review(
        self,
        *,
        limit: int = 80,
        opportunity_stake: float = 5.0,
        variant_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(300, int(limit)))
        sample_limit = max(limit, 500)
        sample_limit = min(sample_limit, 5_000)
        variant_filter = str(variant_id or "").strip()
        where = "WHERE d.variant_id = ?" if variant_filter else ""
        params: tuple[Any, ...] = (variant_filter,) if variant_filter else ()
        total_row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM llm_decisions d {where}",
            params,
        ).fetchone()
        rows = self.conn.execute(
            f"""
            SELECT
                d.id,
                d.round_id,
                d.variant_id,
                d.route,
                d.allow_trade,
                d.confidence,
                d.market_regime,
                d.source,
                d.reason,
                d.reason_codes_json,
                d.features_json,
                d.response_json,
                d.error,
                d.valid_until,
                d.created_at,
                r.outcome,
                r.final_price,
                r.target_price,
                r.settled_at AS round_settled_at,
                r.settlement_source AS market_settlement_source
            FROM llm_decisions d
            LEFT JOIN market_rounds r ON r.round_id = d.round_id
            {where}
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT ?
            """,
            (*params, sample_limit),
        ).fetchall()
        total_decision_count = int(total_row["count"] or 0) if total_row else 0
        if not rows:
            return {
                "status": "EMPTY",
                "generated_at": time.time(),
                "variant_id": variant_filter or None,
                "total_decision_count": total_decision_count,
                "sample_limit": sample_limit,
                "summary": _empty_llm_review_summary(total_decision_count, sample_limit),
                "route_stats": [],
                "reason_stats": [],
                "recent_decisions": [],
                "attribution_note": _llm_review_attribution_note(),
            }

        stake = max(0.0, float(opportunity_stake or 0.0))
        decisions: list[dict[str, Any]] = []
        decisions_by_round: dict[str, list[dict[str, Any]]] = {}
        summary = _empty_llm_review_summary(total_decision_count, sample_limit)
        route_stats: dict[str, dict[str, Any]] = {}
        reason_stats: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            decision_id = int(item["id"])
            route = str(item.get("route") or "UNKNOWN")
            source = str(item.get("source") or "")
            reason_codes = _json_list(item.get("reason_codes_json"))
            if not reason_codes:
                reason_codes = ["NO_CODE"]
            features = _json_dict(item.get("features_json"))
            allow_trade = bool(item.get("allow_trade"))
            error_text = str(item.get("error") or "")
            direction_side = _normalize_side(str(features.get("direction_side") or ""))
            outcome = _normalize_side(str(item.get("outcome") or ""))
            side_ask = _llm_feature_side_ask(features, direction_side)
            opposite_ask = _llm_feature_side_ask(features, "Down" if direction_side == "Up" else "Up")
            no_trade_estimate = (
                _llm_no_trade_estimate(features, outcome, stake)
                if route == "NO_TRADE"
                else None
            )
            enriched = {
                "id": decision_id,
                "round_id": item.get("round_id"),
                "variant_id": item.get("variant_id"),
                "route": route,
                "allow_trade": allow_trade,
                "confidence": round(float(item.get("confidence") or 0.0), 6),
                "market_regime": item.get("market_regime"),
                "source": source,
                "reason": item.get("reason"),
                "reason_codes": reason_codes,
                "error": item.get("error"),
                "valid_until": item.get("valid_until"),
                "created_at": item.get("created_at"),
                "outcome": outcome or None,
                "final_price": item.get("final_price"),
                "target_price": item.get("target_price"),
                "round_settled_at": item.get("round_settled_at"),
                "settlement_source": item.get("market_settlement_source"),
                "feature_direction_side": direction_side or None,
                "feature_time_left_seconds": _maybe_float(features.get("time_left_seconds")),
                "feature_distance_bps": _maybe_float(features.get("signed_distance_bps")),
                "feature_side_ask": side_ask,
                "feature_opposite_ask": opposite_ask,
                "matched_trade_count": 0,
                "matched_settled_trade_count": 0,
                "matched_trade_pnl": 0.0,
                "no_trade_estimate": no_trade_estimate,
            }
            decisions.append(enriched)
            decisions_by_round.setdefault(str(enriched["round_id"] or ""), []).append(enriched)

            _llm_review_add_decision(summary, allow_trade, source, error_text, route)
            route_bucket = route_stats.setdefault(route, _llm_review_bucket(route))
            _llm_review_add_decision(route_bucket, allow_trade, source, error_text, route)
            for code in reason_codes:
                reason_bucket = reason_stats.setdefault(code, _llm_review_bucket(code))
                _llm_review_add_decision(reason_bucket, allow_trade, source, error_text, route)
            if no_trade_estimate:
                _llm_review_add_no_trade_estimate(summary, no_trade_estimate)
                _llm_review_add_no_trade_estimate(route_bucket, no_trade_estimate)
                for code in reason_codes:
                    _llm_review_add_no_trade_estimate(reason_stats[code], no_trade_estimate)

        for round_decisions in decisions_by_round.values():
            round_decisions.sort(key=lambda item: (float(item.get("created_at") or 0.0), int(item.get("id") or 0)))

        round_ids = [round_id for round_id in decisions_by_round if round_id]
        if round_ids:
            placeholders = ",".join("?" for _ in round_ids)
            trade_rows = self.conn.execute(
                f"""
                SELECT id, round_id, side, stake, entry_price, shares, status, opened_at,
                       settled_at, pnl, reason
                FROM trades
                WHERE round_id IN ({placeholders})
                ORDER BY opened_at ASC, id ASC
                """,
                tuple(round_ids),
            ).fetchall()
            for trade_row in trade_rows:
                trade = dict(trade_row)
                matched = _match_llm_decision_for_trade(trade, decisions_by_round.get(str(trade.get("round_id") or ""), []))
                if matched is None:
                    continue
                _llm_review_add_trade(summary, trade)
                _llm_review_add_trade(route_stats.setdefault(str(matched["route"]), _llm_review_bucket(str(matched["route"]))), trade)
                for code in matched.get("reason_codes") or ["NO_CODE"]:
                    _llm_review_add_trade(reason_stats.setdefault(str(code), _llm_review_bucket(str(code))), trade)
                matched["matched_trade_count"] = int(matched.get("matched_trade_count") or 0) + 1
                if str(trade.get("status") or "").upper() == "SETTLED":
                    matched["matched_settled_trade_count"] = int(matched.get("matched_settled_trade_count") or 0) + 1
                    matched["matched_trade_pnl"] = round(
                        float(matched.get("matched_trade_pnl") or 0.0) + float(trade.get("pnl") or 0.0),
                        6,
                    )

        finalized_routes = [_finalize_llm_review_bucket(row) for row in route_stats.values()]
        finalized_reasons = [_finalize_llm_review_bucket(row) for row in reason_stats.values()]
        recent_decisions = []
        for item in decisions[:limit]:
            row = dict(item)
            row["matched_trade_pnl"] = round(float(row.get("matched_trade_pnl") or 0.0), 6)
            recent_decisions.append(row)
        return {
            "status": "READY",
            "generated_at": time.time(),
            "variant_id": variant_filter or (str(decisions[0].get("variant_id") or "") or None),
            "total_decision_count": total_decision_count,
            "sample_limit": sample_limit,
            "summary": _finalize_llm_review_bucket(summary),
            "route_stats": sorted(
                finalized_routes,
                key=lambda item: (int(item.get("decision_count") or 0), float(item.get("total_pnl") or 0.0)),
                reverse=True,
            ),
            "reason_stats": sorted(
                finalized_reasons,
                key=lambda item: (int(item.get("decision_count") or 0), float(item.get("total_pnl") or 0.0)),
                reverse=True,
            )[:80],
            "recent_decisions": recent_decisions,
            "attribution_note": _llm_review_attribution_note(),
        }

    @_locked
    def recent_prices(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT symbol, price, source, created_at
            FROM price_ticks
            WHERE symbol = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @_locked
    def closest_price_tick(
        self,
        symbol: str,
        target_at: float,
        *,
        max_distance_seconds: float = 20.0,
        source_contains: str = "",
    ) -> dict[str, Any] | None:
        """读取目标时间附近的价格 tick，供策略复盘和学习过滤使用。"""

        distance = max(0.0, float(max_distance_seconds))
        source_like = f"%{source_contains.lower()}%" if source_contains else ""
        row = self.conn.execute(
            """
            SELECT
                symbol,
                price,
                source,
                created_at,
                ABS(created_at - ?) AS distance_seconds
            FROM price_ticks
            WHERE symbol = ?
              AND created_at BETWEEN ? AND ?
              AND (? = '' OR LOWER(source) LIKE ?)
            ORDER BY distance_seconds ASC, created_at DESC, id DESC
            LIMIT 1
            """,
            (target_at, symbol, target_at - distance, target_at + distance, source_like, source_like),
        ).fetchone()
        return dict(row) if row is not None else None

    @_locked
    def upsert_round(self, market: MarketRound) -> None:
        self.conn.execute(
            """
            INSERT INTO market_rounds(
                round_id, symbol, started_at, ends_at, target_price,
                question, condition_id, up_token, down_token, url
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(round_id) DO UPDATE SET
                target_price = CASE
                    WHEN excluded.target_price > 0 THEN excluded.target_price
                    ELSE market_rounds.target_price
                END,
                question = COALESCE(NULLIF(excluded.question, ''), market_rounds.question),
                condition_id = COALESCE(NULLIF(excluded.condition_id, ''), market_rounds.condition_id),
                up_token = COALESCE(NULLIF(excluded.up_token, ''), market_rounds.up_token),
                down_token = COALESCE(NULLIF(excluded.down_token, ''), market_rounds.down_token),
                url = COALESCE(NULLIF(excluded.url, ''), market_rounds.url)
            """,
            (
                market.round_id,
                market.symbol,
                market.started_at,
                market.ends_at,
                market.target_price,
                market.question,
                market.condition_id,
                market.up_token,
                market.down_token,
                market.url,
            ),
        )
        self.conn.commit()

    @_locked
    def get_round(self, round_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM market_rounds WHERE round_id = ?", (round_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def latest_active_round(self, now: float) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM market_rounds
            WHERE symbol = 'BTC' AND ends_at > ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        return dict(row) if row else None

    @_locked
    def open_trade_exists(self, round_id: str, side: str) -> bool:
        normalized = _normalize_side(side)
        row = self.conn.execute(
            """
            SELECT id
            FROM trades
            WHERE round_id = ?
              AND side = ?
              AND status = 'OPEN'
              AND stake >= ?
            LIMIT 1
            """,
            (round_id, normalized, PAPER_MIN_OPEN_TRADE_STAKE),
        ).fetchone()
        return row is not None

    @_locked
    def open_trade_exists_for_round(self, round_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT id
            FROM trades
            WHERE round_id = ?
              AND status = 'OPEN'
              AND stake >= ?
            LIMIT 1
            """,
            (round_id, PAPER_MIN_OPEN_TRADE_STAKE),
        ).fetchone()
        return row is not None

    @_locked
    def active_paper_order_exists(self, round_id: str, side: str) -> bool:
        normalized = _normalize_side(side)
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        row = self.conn.execute(
            f"""
            SELECT id
            FROM paper_orders
            WHERE round_id = ? AND side = ? AND status IN ({placeholders})
            LIMIT 1
            """,
            (round_id, normalized, *ACTIVE_ORDER_STATUSES),
        ).fetchone()
        return row is not None

    @_locked
    def active_paper_order_exists_for_round(self, round_id: str) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        row = self.conn.execute(
            f"""
            SELECT id
            FROM paper_orders
            WHERE round_id = ? AND status IN ({placeholders})
            LIMIT 1
            """,
            (round_id, *ACTIVE_ORDER_STATUSES),
        ).fetchone()
        return row is not None

    @_locked
    def active_live_entry_order_exists_for_round(self, round_id: str) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        row = self.conn.execute(
            f"""
            SELECT id
            FROM paper_orders
            WHERE execution_mode = 'LIVE'
              AND round_id = ?
              AND order_type != 'FAK_SELL'
              AND status IN ({placeholders})
            LIMIT 1
            """,
            (round_id, *ACTIVE_ORDER_STATUSES),
        ).fetchone()
        return row is not None

    @_locked
    def open_trade_count(self, symbol: str | None = None) -> int:
        if symbol:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM trades
                WHERE status = 'OPEN'
                  AND symbol = ?
                  AND stake >= ?
                """,
                (symbol, PAPER_MIN_OPEN_TRADE_STAKE),
            ).fetchone()
            return int(row["count"])
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM trades
            WHERE status = 'OPEN'
              AND stake >= ?
            """,
            (PAPER_MIN_OPEN_TRADE_STAKE,),
        ).fetchone()
        return int(row["count"])

    @_locked
    def account(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("account was not initialized")
        return dict(row)

    @_locked
    def rebase_initial_balance(self, initial_balance: float) -> dict[str, Any]:
        """调整隔离账户本金口径；保留已有盈亏并按差额调整可用资金。"""

        next_initial = round(float(initial_balance), 2)
        if next_initial <= 0:
            raise ValueError("initial balance must be positive")
        account = self.account()
        previous_initial = round(float(account["initial_balance"]), 2)
        if abs(previous_initial - next_initial) < 0.000001:
            return account
        delta = round(next_initial - previous_initial, 6)
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                UPDATE account
                SET initial_balance = ?,
                    cash_balance = MAX(0, cash_balance + ?),
                    updated_at = ?
                WHERE id = 1
                """,
                (next_initial, delta, now),
            )
        self.record_equity()
        return self.account()

    @_locked
    def reserved_cash(self) -> float:
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(remaining_cash), 0) AS reserved FROM paper_orders WHERE status IN ({placeholders})",
            ACTIVE_ORDER_STATUSES,
        ).fetchone()
        return round(float(row["reserved"] or 0.0), 6)

    @_locked
    def daily_realized_pnl(self) -> float:
        start = time.time() - 24 * 60 * 60
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades WHERE status = 'SETTLED' AND settled_at >= ?",
            (start,),
        ).fetchone()
        return float(row["pnl"])

    @_locked
    def place_trade(self, intent: TradeIntent) -> int:
        return self.place_trades([intent])[0]

    @_locked
    def place_trades(self, intents: list[TradeIntent]) -> list[int]:
        if not intents:
            return []
        account = self.account()
        rows: list[tuple[TradeIntent, float, float, str, float]] = []
        total_stake = 0.0
        for intent in intents:
            stake = round(float(intent.stake_dollars), 6)
            if stake <= 0:
                raise ValueError("stake must be positive")
            entry_price = max(0.01, min(0.99, round(intent.signal.entry_price, 4)))
            side = _normalize_side(intent.signal.side)
            shares = round(stake / entry_price, 6)
            rows.append((intent, stake, entry_price, side, shares))
            total_stake = round(total_stake + stake, 6)
        if account["cash_balance"] + 1e-9 < total_stake:
            raise ValueError("insufficient paper cash")
        now = time.time()
        trade_ids: list[int] = []
        with self.conn:
            self.conn.execute(
                "UPDATE account SET cash_balance = cash_balance - ?, updated_at = ? WHERE id = 1",
                (total_stake, now),
            )
            for intent, stake, entry_price, side, shares in rows:
                cur = self.conn.execute(
                    """
                    INSERT INTO trades(
                        round_id, symbol, side, stake, entry_price, shares, confidence,
                        move_bps, status, opened_at, reason
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (
                        intent.market.round_id,
                        intent.market.symbol,
                        side,
                        stake,
                        entry_price,
                        shares,
                        intent.signal.confidence,
                        intent.signal.move_bps,
                        now,
                        intent.signal.reason,
                    ),
                )
                trade_ids.append(int(cur.lastrowid))
        self.record_equity()
        return trade_ids

    @_locked
    def place_execution_result(self, intent: TradeIntent, result: Any) -> list[int]:
        if getattr(result, "fills", None):
            return self.place_fills(list(result.fills))
        self.record_paper_order(
            intent,
            order_type=str(getattr(result, "order_type", "")),
            status=str(getattr(result, "status", "")),
            side=intent.signal.side,
            limit_price=getattr(result, "limit_price", None),
            requested_cash=getattr(result, "requested_cash", None) or intent.stake_dollars,
            expires_at=getattr(result, "expires_at", None),
            post_only=bool(getattr(result, "post_only", False)),
            reason=_append_reason(str(intent.signal.reason or ""), str(getattr(result, "reason", ""))),
        )
        return []

    @_locked
    def record_paper_order(
        self,
        intent: TradeIntent,
        *,
        order_type: str,
        status: str,
        side: str,
        limit_price: float | None,
        requested_cash: float,
        reason: str,
        expires_at: float | None = None,
        post_only: bool = False,
        execution_mode: str = "PAPER",
        external_order_id: str | None = None,
        client_order_id: str | None = None,
        external_status: str | None = None,
        raw_response: str | None = None,
    ) -> int:
        now = time.time()
        should_reserve = status in ACTIVE_ORDER_STATUSES
        reserve_cash = round(float(requested_cash or 0.0), 6) if should_reserve else 0.0
        if should_reserve:
            account = self.account()
            if account["cash_balance"] + 1e-9 < reserve_cash:
                status = "REJECTED"
                reason = _append_reason(reason, "纸交易可用资金不足，挂单拒绝")
                reserve_cash = 0.0
        with self.conn:
            if reserve_cash > 0:
                self.conn.execute(
                    "UPDATE account SET cash_balance = cash_balance - ?, updated_at = ? WHERE id = 1",
                    (reserve_cash, now),
                )
            order_id = self._insert_paper_order(
                market=intent.market,
                side=side,
                order_type=order_type,
                status=status,
                limit_price=limit_price,
                post_only=post_only,
                expires_at=expires_at,
                requested_cash=requested_cash,
                reserved_cash=reserve_cash,
                remaining_cash=reserve_cash,
                filled_shares=0.0,
                avg_fill_price=None,
                notional=0.0,
                fee=0.0,
                cash_spent=0.0,
                trade_id=None,
                execution_mode=execution_mode,
                external_order_id=external_order_id,
                client_order_id=client_order_id,
                external_status=external_status,
                raw_response=raw_response,
                confidence=intent.signal.confidence,
                move_bps=intent.signal.move_bps,
                reason=reason,
                now=now,
            )
        self.record_equity()
        return order_id

    @_locked
    def record_external_order_rejection(
        self,
        intent: TradeIntent,
        *,
        order_type: str,
        status: str,
        side: str,
        limit_price: float | None,
        requested_cash: float,
        reason: str,
        external_order_id: str | None,
        client_order_id: str | None,
        external_status: str | None,
        raw_response: str | None,
    ) -> int:
        return self.record_paper_order(
            intent,
            order_type=order_type,
            status=status,
            side=side,
            limit_price=limit_price,
            requested_cash=requested_cash,
            reason=reason,
            execution_mode="LIVE",
            external_order_id=external_order_id,
            client_order_id=client_order_id,
            external_status=external_status,
            raw_response=raw_response,
        )

    @_locked
    def place_external_fill(
        self,
        fill: PaperFill,
        *,
        external_order_id: str | None,
        client_order_id: str | None,
        external_status: str | None,
        raw_response: str | None,
        reason_suffix: str = "",
    ) -> int:
        account = self.account()
        total_cash_spent = round(float(fill.cash_spent), 6)
        if total_cash_spent <= 0 or fill.shares <= 0:
            raise ValueError("external fill must have positive shares and cash spent")
        if account["cash_balance"] + 1e-9 < total_cash_spent:
            raise ValueError("insufficient live account cash")
        now = time.time()
        reason = _append_reason(_append_reason(fill.signal.reason, fill.reason), reason_suffix)
        with self.conn:
            self.conn.execute(
                "UPDATE account SET cash_balance = cash_balance - ?, updated_at = ? WHERE id = 1",
                (total_cash_spent, now),
            )
            cur = self.conn.execute(
                """
                INSERT INTO trades(
                    round_id, symbol, side, stake, entry_price, shares, confidence,
                    move_bps, status, opened_at, reason
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    fill.market.round_id,
                    fill.market.symbol,
                    _normalize_side(fill.side),
                    total_cash_spent,
                    max(0.01, min(0.99, round(float(fill.fill_price), 4))),
                    round(float(fill.shares), 6),
                    fill.signal.confidence,
                    fill.signal.move_bps,
                    now,
                    reason,
                ),
            )
            trade_id = int(cur.lastrowid)
            self._insert_paper_order_for_fill(
                fill,
                trade_id,
                reason,
                now,
                execution_mode="LIVE",
                external_order_id=external_order_id,
                client_order_id=client_order_id,
                external_status=external_status,
                raw_response=raw_response,
            )
        self.record_equity()
        return trade_id

    @_locked
    def fill_external_pending_order(
        self,
        order_id: int,
        fill: PaperFill,
        *,
        external_status: str | None,
        raw_response: str | None,
        reason_suffix: str = "",
    ) -> dict[str, Any] | None:
        current = self.conn.execute(
            "SELECT * FROM paper_orders WHERE id = ? AND execution_mode = 'LIVE' AND status = 'PENDING'",
            (int(order_id),),
        ).fetchone()
        if current is None:
            return None
        if fill.cash_spent <= 0 or fill.shares <= 0:
            raise ValueError("external pending fill must have positive shares and cash spent")
        now = time.time()
        reserved_cash = round(float(current["remaining_cash"] or current["reserved_cash"] or 0.0), 6)
        total_cash_spent = round(float(fill.cash_spent), 6)
        cash_delta = round(reserved_cash - total_cash_spent, 6)
        reason = _append_reason(_append_reason(str(current["reason"] or ""), fill.reason), reason_suffix)
        with self.conn:
            if abs(cash_delta) > 0.000001:
                self.conn.execute(
                    "UPDATE account SET cash_balance = cash_balance + ?, updated_at = ? WHERE id = 1",
                    (cash_delta, now),
                )
            cur = self.conn.execute(
                """
                INSERT INTO trades(
                    round_id, symbol, side, stake, entry_price, shares, confidence,
                    move_bps, status, opened_at, reason
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    current["round_id"],
                    current["symbol"],
                    _normalize_side(fill.side),
                    total_cash_spent,
                    max(0.01, min(0.99, round(float(fill.fill_price), 4))),
                    round(float(fill.shares), 6),
                    float(current["confidence"] or fill.signal.confidence or 0.0),
                    float(current["move_bps"] or fill.signal.move_bps or 0.0),
                    now,
                    reason,
                ),
            )
            trade_id = int(cur.lastrowid)
            self.conn.execute(
                """
                UPDATE paper_orders
                SET status = 'FILLED',
                    remaining_cash = 0,
                    filled_shares = ?,
                    avg_fill_price = ?,
                    notional = ?,
                    fee = ?,
                    cash_spent = ?,
                    trade_id = ?,
                    external_status = ?,
                    raw_response = ?,
                    reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    round(float(fill.shares), 6),
                    max(0.01, min(0.99, round(float(fill.fill_price), 4))),
                    round(float(fill.notional), 6),
                    round(float(fill.fee), 6),
                    total_cash_spent,
                    trade_id,
                    external_status,
                    raw_response,
                    reason,
                    now,
                    int(order_id),
                ),
            )
            self._insert_paper_fill_levels(int(order_id), trade_id, fill, now)
        self.record_equity()
        return {
            **dict(current),
            "status": "FILLED",
            "remaining_cash": 0.0,
            "filled_shares": round(float(fill.shares), 6),
            "avg_fill_price": max(0.01, min(0.99, round(float(fill.fill_price), 4))),
            "notional": round(float(fill.notional), 6),
            "fee": round(float(fill.fee), 6),
            "cash_spent": total_cash_spent,
            "trade_id": trade_id,
            "external_status": external_status,
            "raw_response": raw_response,
            "reason": reason,
            "updated_at": now,
        }

    @_locked
    def update_external_pending_order(
        self,
        order_id: int,
        *,
        status: str,
        external_status: str | None,
        raw_response: str | None,
        reason: str,
    ) -> dict[str, Any] | None:
        current = self.conn.execute(
            "SELECT * FROM paper_orders WHERE id = ? AND execution_mode = 'LIVE' AND status = 'PENDING'",
            (int(order_id),),
        ).fetchone()
        if current is None:
            return None
        now = time.time()
        next_status = str(status or "PENDING")
        next_reason = _append_reason(str(current["reason"] or ""), reason)
        release_cash = 0.0
        if next_status != "PENDING":
            release_cash = round(float(current["remaining_cash"] or 0.0), 6)
        with self.conn:
            self.conn.execute(
                """
                UPDATE paper_orders
                SET status = ?,
                    remaining_cash = ?,
                    external_status = ?,
                    raw_response = ?,
                    reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    0.0 if next_status != "PENDING" else float(current["remaining_cash"] or 0.0),
                    external_status,
                    raw_response,
                    next_reason,
                    now,
                    int(order_id),
                ),
            )
            if release_cash > 0:
                self.conn.execute(
                    "UPDATE account SET cash_balance = cash_balance + ?, updated_at = ? WHERE id = 1",
                    (release_cash, now),
                )
        self.record_equity()
        return {
            **dict(current),
            "status": next_status,
            "remaining_cash": 0.0 if next_status != "PENDING" else float(current["remaining_cash"] or 0.0),
            "released_cash": release_cash,
            "external_status": external_status,
            "raw_response": raw_response,
            "reason": next_reason,
            "updated_at": now,
        }

    @_locked
    def fill_external_pending_exit_order(
        self,
        order_id: int,
        *,
        shares: float,
        exit_price: float,
        notional: float,
        fee: float,
        external_status: str | None,
        raw_response: str | None,
        reason: str,
    ) -> dict[str, Any] | None:
        current = self.conn.execute(
            "SELECT * FROM paper_orders WHERE id = ? AND execution_mode = 'LIVE' AND status = 'PENDING'",
            (int(order_id),),
        ).fetchone()
        if current is None:
            return None
        if str(current["order_type"] or "") != "FAK_SELL":
            raise ValueError("pending order is not a live exit order")
        trade_id = int(current["trade_id"] or 0)
        if trade_id <= 0:
            raise ValueError("pending exit order has no trade id")
        close_shares = round(max(0.0, float(shares or 0.0)), 6)
        close_price = max(0.01, min(0.99, round(float(exit_price or 0.0), 6)))
        close_notional = round(max(0.0, float(notional or 0.0)), 6)
        close_fee = round(max(0.0, float(fee or 0.0)), 6)
        if close_shares <= 0 or close_notional <= 0:
            raise ValueError("external pending exit fill must have positive shares and notional")
        next_reason = _append_reason(str(current["reason"] or ""), reason)
        closed = self.close_trade_shares(
            trade_id,
            close_shares,
            close_price,
            time.time(),
            next_reason,
            fee=close_fee,
        )
        if closed is None:
            return None
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                UPDATE paper_orders
                SET status = 'FILLED',
                    filled_shares = ?,
                    avg_fill_price = ?,
                    notional = ?,
                    fee = ?,
                    trade_id = ?,
                    external_status = ?,
                    raw_response = ?,
                    reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    close_shares,
                    close_price,
                    close_notional,
                    close_fee,
                    int(closed["id"]),
                    external_status,
                    raw_response,
                    next_reason,
                    now,
                    int(order_id),
                ),
            )
        self.record_equity()
        return {
            **dict(current),
            "status": "FILLED",
            "filled_shares": close_shares,
            "avg_fill_price": close_price,
            "notional": close_notional,
            "fee": close_fee,
            "trade_id": int(closed["id"]),
            "external_status": external_status,
            "raw_response": raw_response,
            "reason": next_reason,
            "updated_at": now,
            "closed_trade": closed,
        }

    @_locked
    def pending_external_orders(self, limit: int = 20, symbol: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        conditions = ["o.execution_mode = 'LIVE'", "o.status = 'PENDING'"]
        params: list[Any] = []
        if symbol:
            conditions.append("o.symbol = ?")
            params.append(symbol)
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT
                o.*,
                r.started_at,
                r.ends_at,
                r.target_price,
                r.question,
                r.condition_id,
                r.up_token,
                r.down_token,
                r.url
            FROM paper_orders o
            JOIN market_rounds r ON r.round_id = o.round_id
            WHERE {' AND '.join(conditions)}
            ORDER BY o.created_at ASC, o.id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def live_order_by_external_id(self, external_order_id: str) -> dict[str, Any] | None:
        order_id = str(external_order_id or "").strip()
        if not order_id:
            return None
        row = self.conn.execute(
            """
            SELECT
                o.*,
                r.started_at,
                r.ends_at,
                r.target_price,
                r.question,
                r.condition_id,
                r.up_token,
                r.down_token,
                r.url,
                COALESCE(f.fill_count, 0) AS fill_count
            FROM paper_orders o
            JOIN market_rounds r ON r.round_id = o.round_id
            LEFT JOIN (
                SELECT order_id, COUNT(*) AS fill_count
                FROM paper_fills
                GROUP BY order_id
            ) f ON f.order_id = o.id
            WHERE o.execution_mode = 'LIVE' AND o.external_order_id = ?
            ORDER BY o.id DESC
            LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @_locked
    def record_external_exit_order(
        self,
        market: MarketRound,
        *,
        trade_id: int,
        side: str,
        status: str,
        limit_price: float | None,
        shares: float,
        notional: float,
        fee: float,
        reason: str,
        external_order_id: str | None,
        client_order_id: str | None,
        external_status: str | None,
        raw_response: str | None,
    ) -> int:
        now = time.time()
        order_id = self._insert_paper_order(
            market=market,
            side=side,
            order_type="FAK_SELL",
            status=status,
            limit_price=limit_price,
            post_only=False,
            expires_at=None,
            requested_cash=0.0,
            reserved_cash=0.0,
            remaining_cash=0.0,
            filled_shares=shares,
            avg_fill_price=limit_price,
            notional=notional,
            fee=fee,
            cash_spent=0.0,
            trade_id=trade_id,
            execution_mode="LIVE",
            external_order_id=external_order_id,
            client_order_id=client_order_id,
            external_status=external_status,
            raw_response=raw_response,
            confidence=0.0,
            move_bps=0.0,
            reason=reason,
            now=now,
        )
        self.conn.commit()
        return order_id

    @_locked
    def place_fills(self, fills: list[PaperFill]) -> list[int]:
        valid_fills = [fill for fill in fills if fill.shares > 0 and fill.cash_spent > 0]
        if not valid_fills:
            return []
        account = self.account()
        total_cash_spent = round(sum(float(fill.cash_spent) for fill in valid_fills), 6)
        if account["cash_balance"] + 1e-9 < total_cash_spent:
            raise ValueError("insufficient paper cash")
        now = time.time()
        trade_ids: list[int] = []
        with self.conn:
            self.conn.execute(
                "UPDATE account SET cash_balance = cash_balance - ?, updated_at = ? WHERE id = 1",
                (total_cash_spent, now),
            )
            for fill in valid_fills:
                reason = _append_reason(fill.signal.reason, fill.reason)
                cur = self.conn.execute(
                    """
                    INSERT INTO trades(
                        round_id, symbol, side, stake, entry_price, shares, confidence,
                        move_bps, status, opened_at, reason
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (
                        fill.market.round_id,
                        fill.market.symbol,
                        _normalize_side(fill.side),
                        round(float(fill.cash_spent), 6),
                        max(0.01, min(0.99, round(float(fill.fill_price), 4))),
                        round(float(fill.shares), 6),
                        fill.signal.confidence,
                        fill.signal.move_bps,
                        now,
                        reason,
                    ),
                )
                trade_ids.append(int(cur.lastrowid))
                self._insert_paper_order_for_fill(fill, int(cur.lastrowid), reason, now)
        self.record_equity()
        return trade_ids

    def _insert_paper_order_for_fill(
        self,
        fill: PaperFill,
        trade_id: int,
        reason: str,
        now: float,
        *,
        execution_mode: str = "PAPER",
        external_order_id: str | None = None,
        client_order_id: str | None = None,
        external_status: str | None = None,
        raw_response: str | None = None,
    ) -> int:
        order_id = self._insert_paper_order(
            market=fill.market,
            side=fill.side,
            order_type=fill.order_type,
            status=fill.status,
            limit_price=fill.limit_price,
            post_only=False,
            expires_at=None,
            requested_cash=fill.requested_cash if fill.requested_cash is not None else fill.cash_spent,
            reserved_cash=0.0,
            remaining_cash=0.0,
            filled_shares=fill.shares,
            avg_fill_price=fill.fill_price,
            notional=fill.notional,
            fee=fill.fee,
            cash_spent=fill.cash_spent,
            trade_id=trade_id,
            execution_mode=execution_mode,
            external_order_id=external_order_id,
            client_order_id=client_order_id,
            external_status=external_status,
            raw_response=raw_response,
            confidence=fill.signal.confidence,
            move_bps=fill.signal.move_bps,
            reason=reason,
            now=now,
        )
        self._insert_paper_fill_levels(order_id, trade_id, fill, now)
        return order_id

    def _insert_paper_order(
        self,
        *,
        market: MarketRound,
        side: str,
        order_type: str,
        status: str,
        limit_price: float | None,
        post_only: bool,
        expires_at: float | None,
        requested_cash: float,
        reserved_cash: float,
        remaining_cash: float,
        filled_shares: float,
        avg_fill_price: float | None,
        notional: float,
        fee: float,
        cash_spent: float,
        trade_id: int | None,
        confidence: float,
        move_bps: float,
        reason: str,
        now: float,
        execution_mode: str = "PAPER",
        external_order_id: str | None = None,
        client_order_id: str | None = None,
        external_status: str | None = None,
        raw_response: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO paper_orders(
                round_id, symbol, side, order_type, status, limit_price, post_only,
                expires_at, requested_cash, reserved_cash, remaining_cash, filled_shares,
                avg_fill_price, notional, fee, cash_spent, trade_id, execution_mode,
                external_order_id, client_order_id, external_status, raw_response,
                confidence, move_bps, reason, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.round_id,
                market.symbol,
                _normalize_side(side),
                str(order_type or ""),
                str(status or ""),
                _nullable_price(limit_price),
                1 if post_only else 0,
                expires_at,
                round(float(requested_cash or 0.0), 6),
                round(float(reserved_cash or 0.0), 6),
                round(float(remaining_cash or 0.0), 6),
                round(float(filled_shares or 0.0), 6),
                _nullable_price(avg_fill_price),
                round(float(notional or 0.0), 6),
                round(float(fee or 0.0), 6),
                round(float(cash_spent or 0.0), 6),
                trade_id,
                str(execution_mode or "PAPER"),
                external_order_id,
                client_order_id,
                external_status,
                raw_response,
                round(float(confidence or 0.0), 6),
                round(float(move_bps or 0.0), 6),
                str(reason or ""),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def _insert_paper_fill_levels(self, order_id: int, trade_id: int, fill: PaperFill, now: float) -> None:
        levels = fill.levels or (
            PaperFillLevel(
                price=fill.fill_price,
                shares=fill.shares,
                notional=fill.notional,
                fee=fill.fee,
                cash_spent=fill.cash_spent,
            ),
        )
        for index, level in enumerate(levels, start=1):
            self.conn.execute(
                """
                INSERT INTO paper_fills(
                    order_id, trade_id, level_index, price, shares,
                    notional, fee, cash_spent, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    trade_id,
                    index,
                    round(float(level.price), 4),
                    round(float(level.shares), 6),
                    round(float(level.notional), 6),
                    round(float(level.fee), 6),
                    round(float(level.cash_spent), 6),
                    now,
                ),
            )

    @_locked
    def close_trade_shares(
        self,
        trade_id: int,
        shares_to_close: float,
        exit_price: float,
        now: float | None = None,
        reason: str = "manual close",
        fee: float = 0.0,
    ) -> dict[str, Any] | None:
        now = time.time() if now is None else now
        exit_price = max(0.0, min(1.0, round(float(exit_price), 4)))
        if shares_to_close <= 0:
            return None
        row = self.conn.execute(
            "SELECT * FROM trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,),
        ).fetchone()
        if row is None:
            return None
        trade = dict(row)
        open_shares = float(trade["shares"])
        if open_shares <= 0:
            return None
        close_shares = min(open_shares, round(float(shares_to_close), 6))
        if close_shares <= 0:
            return None
        close_all = close_shares >= open_shares - 0.000001
        dust_reason = ""
        if not close_all:
            remaining_shares_candidate = round(open_shares - close_shares, 6)
            remaining_stake_candidate = round(float(trade["stake"]) * (remaining_shares_candidate / open_shares), 6)
            if remaining_stake_candidate < PAPER_MIN_OPEN_TRADE_STAKE:
                close_all = True
                dust_reason = (
                    f"DUST_CLOSE remaining stake {remaining_stake_candidate:.6f}, "
                    f"shares {remaining_shares_candidate:.6f} below min {PAPER_MIN_OPEN_TRADE_STAKE:.2f}"
                )
        close_ratio = 1.0 if close_all else close_shares / open_shares
        close_stake = round(float(trade["stake"]) * close_ratio, 6)
        close_fee = max(0.0, round(float(fee), 6))
        payout = round(max(0.0, close_shares * exit_price - close_fee), 6)
        pnl = round(payout - close_stake, 6)
        fee_reason = f"{reason} fee {close_fee:.6f}" if close_fee > 0 else reason
        close_reason = _append_reason(str(trade["reason"] or ""), fee_reason)
        if dust_reason:
            close_reason = _append_reason(close_reason, dust_reason)
        with self.conn:
            if close_all:
                self.conn.execute(
                    """
                    UPDATE trades
                    SET stake = ?,
                        shares = ?,
                        status = 'SETTLED',
                        settled_at = ?,
                        exit_price = ?,
                        payout = ?,
                        pnl = ?,
                        settlement_source = ?,
                        reason = ?
                    WHERE id = ?
                    """,
                    (
                        close_stake,
                        close_shares,
                        now,
                        exit_price,
                        payout,
                        pnl,
                        SETTLEMENT_SOURCE_EARLY_EXIT,
                        close_reason,
                        trade_id,
                    ),
                )
                closed_id = trade_id
                remaining_stake = 0.0
                remaining_shares = 0.0
            else:
                remaining_stake = round(float(trade["stake"]) - close_stake, 6)
                remaining_shares = round(open_shares - close_shares, 6)
                self.conn.execute(
                    """
                    UPDATE trades
                    SET stake = ?, shares = ?
                    WHERE id = ?
                    """,
                    (remaining_stake, remaining_shares, trade_id),
                )
                cur = self.conn.execute(
                    """
                    INSERT INTO trades(
                        round_id, symbol, side, stake, entry_price, shares, confidence,
                        move_bps, status, opened_at, settled_at, exit_price, payout, pnl,
                        settlement_source, reason
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'SETTLED', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade["round_id"],
                        trade["symbol"],
                        trade["side"],
                        close_stake,
                        trade["entry_price"],
                        close_shares,
                        trade["confidence"],
                        trade["move_bps"],
                        trade["opened_at"],
                        now,
                        exit_price,
                        payout,
                        pnl,
                        SETTLEMENT_SOURCE_EARLY_EXIT,
                        close_reason,
                    ),
                )
                closed_id = int(cur.lastrowid)
            self.conn.execute(
                """
                UPDATE account
                SET cash_balance = cash_balance + ?,
                    realized_pnl = realized_pnl + ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (payout, pnl, now),
            )
        self.record_equity()
        closed = dict(trade)
        closed.update(
            {
                "id": closed_id,
                "stake": close_stake,
                "shares": close_shares,
                "status": "SETTLED",
                "settled_at": now,
                "exit_price": exit_price,
                "payout": payout,
                "pnl": pnl,
                "settlement_source": SETTLEMENT_SOURCE_EARLY_EXIT,
                "reason": close_reason,
                "remaining_stake": remaining_stake,
                "remaining_shares": remaining_shares,
            }
        )
        return closed

    @_locked
    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        now: float | None = None,
        reason: str = "manual close",
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT shares FROM trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,),
        ).fetchone()
        if row is None:
            return None
        return self.close_trade_shares(trade_id, float(row["shares"]), exit_price, now, reason)

    @_locked
    def close_all_open_trades_for_round(
        self,
        round_id: str,
        exit_prices: dict[str, float],
        now: float | None = None,
        reason: str = "round close",
    ) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        rows = self.conn.execute(
            "SELECT id, side FROM trades WHERE round_id = ? AND status = 'OPEN' ORDER BY opened_at ASC, id ASC",
            (round_id,),
        ).fetchall()
        closed: list[dict[str, Any]] = []
        for row in rows:
            side = _normalize_side(row["side"])
            exit_price = exit_prices.get(side)
            if exit_price is None:
                continue
            item = self.close_trade(int(row["id"]), exit_price, now, reason)
            if item:
                closed.append(item)
        return closed

    @_locked
    def settle_due_rounds(self, prices: dict[str, float], now: float) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM market_rounds WHERE settled_at IS NULL AND ends_at <= ?",
            (now,),
        ).fetchall()
        settled: list[dict[str, Any]] = []
        for row in rows:
            symbol = row["symbol"]
            if prices.get(symbol) is None:
                continue
            settlement_tick = self._chainlink_settlement_tick(symbol, float(row["ends_at"]))
            if settlement_tick is None:
                continue
            final_price = float(settlement_tick["price"])
            outcome = "Up" if final_price >= row["target_price"] else "Down"
            settled.extend(
                self._settle_round(
                    row["round_id"],
                    final_price,
                    None,
                    outcome,
                    now,
                    SETTLEMENT_SOURCE_CHAINLINK,
                )
            )
        if settled:
            self.record_equity()
        return settled

    def _chainlink_settlement_tick(self, symbol: str, ends_at: float) -> dict[str, Any] | None:
        max_age = CHAINLINK_FALLBACK_SETTLEMENT_MAX_AGE_SECONDS
        row = self.conn.execute(
            """
            SELECT
                price,
                source,
                created_at,
                ABS(created_at - ?) AS distance_seconds
            FROM price_ticks
            WHERE symbol = ?
              AND LOWER(source) LIKE '%chainlink%'
              AND created_at BETWEEN ? AND ?
            ORDER BY distance_seconds ASC, created_at DESC, id DESC
            LIMIT 1
            """,
            (ends_at, symbol, ends_at - max_age, ends_at + max_age),
        ).fetchone()
        if row is None:
            return None
        price = _maybe_float(row["price"])
        if price is None:
            return None
        return {
            "price": price,
            "source": row["source"],
            "created_at": float(row["created_at"]),
            "distance_seconds": round(float(row["distance_seconds"] or 0.0), 6),
        }

    @_locked
    def settle_round_outcome(
        self,
        round_id: str,
        outcome: str,
        now: float | None = None,
        final_price: float | None = None,
        target_price: float | None = None,
        settlement_source: str = SETTLEMENT_SOURCE_POLYMARKET,
    ) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        settled = self._settle_round(
            round_id,
            final_price,
            target_price,
            _normalize_side(outcome),
            now,
            settlement_source,
        )
        if settled:
            self.record_equity()
        return settled

    @_locked
    def _settle_round(
        self,
        round_id: str,
        final_price: float | None,
        target_price: float | None,
        outcome: str,
        now: float,
        settlement_source: str,
    ) -> list[dict[str, Any]]:
        normalized_outcome = _normalize_side(outcome)
        normalized_source = str(settlement_source or "").strip() or SETTLEMENT_SOURCE_CHAINLINK
        normalized_target_price = _positive_price_or_none(target_price)
        trades = self.conn.execute(
            "SELECT * FROM trades WHERE round_id = ? AND status = 'OPEN'",
            (round_id,),
        ).fetchall()
        settled: list[dict[str, Any]] = []
        with self.conn:
            self.conn.execute(
                """
                UPDATE market_rounds
                SET final_price = ?,
                    target_price = COALESCE(?, target_price),
                    outcome = ?,
                    settled_at = ?,
                    settlement_source = ?
                WHERE round_id = ?
                """,
                (final_price, normalized_target_price, normalized_outcome, now, normalized_source, round_id),
            )
            for trade in trades:
                win = _normalize_side(trade["side"]) == normalized_outcome
                payout = round(float(trade["shares"]) if win else 0.0, 6)
                pnl = round(payout - float(trade["stake"]), 6)
                self.conn.execute(
                    """
                    UPDATE trades
                    SET status = 'SETTLED',
                        settled_at = ?,
                        exit_price = ?,
                        payout = ?,
                        pnl = ?,
                        settlement_source = ?
                    WHERE id = ?
                    """,
                    (now, 1.0 if win else 0.0, payout, pnl, normalized_source, trade["id"]),
                )
                self.conn.execute(
                    """
                    UPDATE account
                    SET cash_balance = cash_balance + ?,
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (payout, pnl, now),
                )
                item = dict(trade)
                item.update(
                    {
                        "outcome": normalized_outcome,
                        "status": "SETTLED",
                        "settled_at": now,
                        "exit_price": 1.0 if win else 0.0,
                        "payout": payout,
                        "pnl": pnl,
                        "final_price": final_price,
                        "settlement_source": normalized_source,
                    }
                )
                settled.append(item)
        return settled

    @_locked
    def official_recheck_candidates(
        self,
        now: float,
        lookback_seconds: float,
        limit: int = 5,
        symbol: str = "BTC",
    ) -> list[dict[str, Any]]:
        cutoff = now - max(0.0, float(lookback_seconds))
        safe_limit = max(1, min(50, int(limit)))
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_rounds
            WHERE symbol = ?
              AND settled_at IS NOT NULL
              AND ends_at >= ?
              AND (settlement_source IS NULL OR settlement_source = ?)
            ORDER BY ends_at DESC, round_id DESC
            LIMIT ?
            """,
            (symbol, cutoff, SETTLEMENT_SOURCE_CHAINLINK, safe_limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def official_final_price_candidates(
        self,
        now: float,
        lookback_seconds: float,
        limit: int = 5,
        symbol: str = "BTC",
    ) -> list[dict[str, Any]]:
        cutoff = now - max(0.0, float(lookback_seconds))
        safe_limit = max(1, min(50, int(limit)))
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_rounds
            WHERE symbol = ?
              AND settled_at IS NOT NULL
              AND ends_at >= ?
              AND settlement_source = ?
              AND (
                    final_price IS NULL
                    OR target_price <= 0
                    OR (
                        final_price IS NOT NULL
                        AND target_price > 0
                        AND ABS(final_price - target_price) <= 0.000001
                    )
                  )
            ORDER BY ends_at DESC, round_id DESC
            LIMIT ?
            """,
            (symbol, cutoff, SETTLEMENT_SOURCE_POLYMARKET, safe_limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def reconcile_round_official_outcome(
        self,
        round_id: str,
        official_outcome: str,
        now: float | None = None,
        final_price: float | None = None,
        target_price: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        normalized_outcome = _normalize_side(official_outcome)
        if normalized_outcome not in {"Up", "Down"}:
            raise ValueError("official_outcome must be Up or Down")
        normalized_target_price = _positive_price_or_none(target_price)

        round_row = self.get_round(round_id)
        if round_row is None:
            return {
                "round_id": round_id,
                "previous_outcome": None,
                "official_outcome": normalized_outcome,
                "corrected": False,
                "updated_trades": 0,
                "cash_delta": 0.0,
                "pnl_delta": 0.0,
            }

        previous_outcome = _normalize_side(round_row.get("outcome")) if round_row.get("outcome") else None
        market_settled_at = _maybe_float(round_row.get("settled_at"))
        trades = self.conn.execute(
            """
            SELECT *
            FROM trades
            WHERE round_id = ?
              AND status = 'SETTLED'
              AND (
                    settlement_source = ?
                    OR (
                        settlement_source IS NULL
                        AND (? IS NULL OR settled_at IS NULL OR ABS(settled_at - ?) <= 0.001)
                    )
              )
            ORDER BY id ASC
            """,
            (round_id, SETTLEMENT_SOURCE_CHAINLINK, market_settled_at, market_settled_at),
        ).fetchall()

        cash_delta = 0.0
        pnl_delta = 0.0
        updated_trades = 0
        corrected = previous_outcome in {"Up", "Down"} and previous_outcome != normalized_outcome
        reconcile_note = ""
        if corrected:
            reconcile_note = f"OFFICIAL_RECONCILE {previous_outcome}->{normalized_outcome}"

        with self.conn:
            self.conn.execute(
                """
                UPDATE market_rounds
                SET final_price = ?,
                    target_price = COALESCE(?, target_price),
                    outcome = ?,
                    settled_at = COALESCE(settled_at, ?),
                    settlement_source = ?
                WHERE round_id = ?
                """,
                (final_price, normalized_target_price, normalized_outcome, now, SETTLEMENT_SOURCE_POLYMARKET, round_id),
            )
            for trade in trades:
                win = _normalize_side(trade["side"]) == normalized_outcome
                payout = round(float(trade["shares"]) if win else 0.0, 6)
                pnl = round(payout - float(trade["stake"]), 6)
                old_payout = round(float(trade["payout"] or 0.0), 6)
                old_pnl = round(float(trade["pnl"] or 0.0), 6)
                cash_delta = round(cash_delta + payout - old_payout, 6)
                pnl_delta = round(pnl_delta + pnl - old_pnl, 6)
                reason = _append_reason(str(trade["reason"] or ""), reconcile_note)
                self.conn.execute(
                    """
                    UPDATE trades
                    SET exit_price = ?,
                        payout = ?,
                        pnl = ?,
                        settlement_source = ?,
                        reason = ?,
                        settled_at = COALESCE(settled_at, ?)
                    WHERE id = ?
                    """,
                    (
                        1.0 if win else 0.0,
                        payout,
                        pnl,
                        SETTLEMENT_SOURCE_POLYMARKET,
                        reason,
                        now,
                        trade["id"],
                    ),
                )
                updated_trades += 1
            if abs(cash_delta) > 0.000001 or abs(pnl_delta) > 0.000001:
                self.conn.execute(
                    """
                    UPDATE account
                    SET cash_balance = cash_balance + ?,
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (cash_delta, pnl_delta, now),
                )

        if abs(cash_delta) > 0.000001 or abs(pnl_delta) > 0.000001:
            self.record_equity()
        return {
            "round_id": round_id,
            "previous_outcome": previous_outcome,
            "official_outcome": normalized_outcome,
            "corrected": corrected,
            "updated_trades": updated_trades,
            "cash_delta": round(cash_delta, 6),
            "pnl_delta": round(pnl_delta, 6),
        }

    @_locked
    def open_trades(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                t.*,
                r.target_price,
                r.started_at,
                r.ends_at,
                r.question,
                r.condition_id,
                r.up_token,
                r.down_token,
                r.url,
                r.final_price,
                r.outcome,
                r.settlement_source AS market_settlement_source
            FROM trades t
            JOIN market_rounds r ON r.round_id = t.round_id
            WHERE t.status = 'OPEN' AND t.stake >= ?
            ORDER BY t.opened_at DESC
            """,
            (PAPER_MIN_OPEN_TRADE_STAKE,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def recent_trades(
        self,
        limit: int = 30,
        offset: int = 0,
        symbol: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._recent_trade_where(symbol, start_at, end_at)
        query_params: tuple[Any, ...] = (*params, limit, offset)
        rows = self.conn.execute(
            f"""
            SELECT
                t.id,
                t.round_id,
                t.symbol,
                t.side,
                t.stake,
                t.entry_price,
                t.shares,
                t.confidence,
                t.move_bps,
                t.status,
                t.opened_at,
                t.settled_at,
                t.exit_price,
                t.payout,
                t.pnl,
                t.reason,
                COALESCE(t.settlement_source, r.settlement_source) AS settlement_source,
                t.settlement_source AS trade_settlement_source,
                r.target_price,
                r.started_at,
                r.final_price,
                r.outcome,
                r.settlement_source AS market_settlement_source,
                r.ends_at,
                r.question,
                r.condition_id,
                r.up_token,
                r.down_token,
                r.url
            FROM trades t
            JOIN market_rounds r ON r.round_id = t.round_id
            {where}
            ORDER BY COALESCE(t.settled_at, t.opened_at) DESC, t.id DESC
            LIMIT ?
            OFFSET ?
            """,
            query_params,
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def trades_for_round(self, round_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                t.id,
                t.round_id,
                t.symbol,
                t.side,
                t.stake,
                t.entry_price,
                t.shares,
                t.confidence,
                t.move_bps,
                t.status,
                t.opened_at,
                t.settled_at,
                t.exit_price,
                t.payout,
                t.pnl,
                t.reason,
                COALESCE(t.settlement_source, r.settlement_source) AS settlement_source,
                t.settlement_source AS trade_settlement_source,
                r.target_price,
                r.started_at,
                r.final_price,
                r.outcome,
                r.settlement_source AS market_settlement_source,
                r.ends_at,
                r.question,
                r.condition_id,
                r.up_token,
                r.down_token,
                r.url
            FROM trades t
            JOIN market_rounds r ON r.round_id = t.round_id
            WHERE t.round_id = ?
            ORDER BY t.id ASC
            """,
            (round_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def recent_trade_count(
        self,
        symbol: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> int:
        where, params = self._recent_trade_where(symbol, start_at, end_at)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM trades t
            {where}
            """,
            params,
        ).fetchone()
        return int(row["count"])

    @_locked
    def recent_trade_summary(
        self,
        symbol: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        where, params = self._recent_trade_where(symbol, start_at, end_at)
        row = self.conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' THEN 1 ELSE 0 END), 0) AS settled_count,
                COALESCE(SUM(CASE WHEN t.status = 'OPEN' THEN 1 ELSE 0 END), 0) AS open_count,
                COALESCE(SUM(t.stake), 0) AS total_stake,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' THEN t.stake ELSE 0 END), 0) AS settled_stake,
                COALESCE(SUM(CASE WHEN t.status = 'OPEN' THEN t.stake ELSE 0 END), 0) AS open_risk,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' THEN COALESCE(t.payout, 0) ELSE 0 END), 0) AS total_payout,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' THEN COALESCE(t.pnl, 0) ELSE 0 END), 0) AS total_pnl,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' AND t.pnl > 0 THEN 1 ELSE 0 END), 0) AS win_count,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' AND t.pnl < 0 THEN 1 ELSE 0 END), 0) AS loss_count,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' AND t.pnl = 0 THEN 1 ELSE 0 END), 0) AS breakeven_count,
                MAX(CASE WHEN t.status = 'SETTLED' THEN t.pnl ELSE NULL END) AS max_win,
                MIN(CASE WHEN t.status = 'SETTLED' THEN t.pnl ELSE NULL END) AS max_loss,
                AVG(CASE WHEN t.status = 'SETTLED' THEN t.pnl ELSE NULL END) AS avg_pnl,
                COALESCE(SUM(CASE
                    WHEN t.status = 'SETTLED'
                     AND COALESCE(t.settlement_source, r.settlement_source) = ?
                    THEN 1 ELSE 0 END), 0) AS official_count,
                COALESCE(SUM(CASE
                    WHEN t.status = 'SETTLED'
                     AND COALESCE(t.settlement_source, r.settlement_source) = ?
                    THEN 1 ELSE 0 END), 0) AS chainlink_count,
                COALESCE(SUM(CASE
                    WHEN t.status = 'SETTLED'
                     AND COALESCE(t.settlement_source, r.settlement_source) = ?
                    THEN 1 ELSE 0 END), 0) AS early_exit_count,
                COALESCE(SUM(CASE
                    WHEN t.status = 'SETTLED'
                     AND COALESCE(t.settlement_source, r.settlement_source) IS NULL
                    THEN 1 ELSE 0 END), 0) AS unknown_source_count
            FROM trades t
            JOIN market_rounds r ON r.round_id = t.round_id
            {where}
            """,
            (
                SETTLEMENT_SOURCE_POLYMARKET,
                SETTLEMENT_SOURCE_CHAINLINK,
                SETTLEMENT_SOURCE_EARLY_EXIT,
                *params,
            ),
        ).fetchone()
        summary = dict(row)
        settled_count = int(summary["settled_count"] or 0)
        settled_stake = float(summary["settled_stake"] or 0.0)
        win_count = int(summary["win_count"] or 0)
        total_pnl = float(summary["total_pnl"] or 0.0)
        summary.update(
            {
                "start_at": start_at,
                "end_at": end_at,
                "total_count": int(summary["total_count"] or 0),
                "settled_count": settled_count,
                "open_count": int(summary["open_count"] or 0),
                "win_count": win_count,
                "loss_count": int(summary["loss_count"] or 0),
                "breakeven_count": int(summary["breakeven_count"] or 0),
                "official_count": int(summary["official_count"] or 0),
                "chainlink_count": int(summary["chainlink_count"] or 0),
                "early_exit_count": int(summary["early_exit_count"] or 0),
                "unknown_source_count": int(summary["unknown_source_count"] or 0),
                "total_stake": round(float(summary["total_stake"] or 0.0), 6),
                "settled_stake": round(settled_stake, 6),
                "open_risk": round(float(summary["open_risk"] or 0.0), 6),
                "total_payout": round(float(summary["total_payout"] or 0.0), 6),
                "total_pnl": round(total_pnl, 6),
                "avg_pnl": round(float(summary["avg_pnl"]), 6) if summary["avg_pnl"] is not None else None,
                "max_win": round(float(summary["max_win"]), 6) if summary["max_win"] is not None else None,
                "max_loss": round(float(summary["max_loss"]), 6) if summary["max_loss"] is not None else None,
                "roi_pct": round(total_pnl / settled_stake * 100.0, 4) if settled_stake else None,
                "win_rate": round(win_count / settled_count * 100.0, 4) if settled_count else None,
            }
        )
        return summary

    @_locked
    def trade_reason_summary(
        self,
        reason_marker: str,
        symbol: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        marker = str(reason_marker or "").strip()
        if not marker:
            return _empty_trade_reason_summary(marker, start_at, end_at)
        where, params = self._recent_trade_where(symbol, start_at, end_at)
        marker_where = f"{where} AND t.reason LIKE ?" if where else "WHERE t.reason LIKE ?"
        marker_params = (*params, f"%{marker}%")
        row = self.conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' THEN 1 ELSE 0 END), 0) AS settled_count,
                COALESCE(SUM(CASE WHEN t.status = 'OPEN' THEN 1 ELSE 0 END), 0) AS open_count,
                COALESCE(SUM(t.stake), 0) AS total_stake,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' THEN t.stake ELSE 0 END), 0) AS settled_stake,
                COALESCE(SUM(CASE WHEN t.status = 'OPEN' THEN t.stake ELSE 0 END), 0) AS open_risk,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' THEN COALESCE(t.pnl, 0) ELSE 0 END), 0) AS total_pnl,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' AND t.pnl > 0 THEN 1 ELSE 0 END), 0) AS win_count,
                COALESCE(SUM(CASE WHEN t.status = 'SETTLED' AND t.pnl < 0 THEN 1 ELSE 0 END), 0) AS loss_count
            FROM trades t
            {marker_where}
            """,
            marker_params,
        ).fetchone()
        summary = dict(row)
        settled_count = int(summary["settled_count"] or 0)
        settled_stake = float(summary["settled_stake"] or 0.0)
        win_count = int(summary["win_count"] or 0)
        total_pnl = float(summary["total_pnl"] or 0.0)
        return {
            "reason_marker": marker,
            "start_at": start_at,
            "end_at": end_at,
            "total_count": int(summary["total_count"] or 0),
            "settled_count": settled_count,
            "open_count": int(summary["open_count"] or 0),
            "win_count": win_count,
            "loss_count": int(summary["loss_count"] or 0),
            "total_stake": round(float(summary["total_stake"] or 0.0), 6),
            "settled_stake": round(settled_stake, 6),
            "open_risk": round(float(summary["open_risk"] or 0.0), 6),
            "total_pnl": round(total_pnl, 6),
            "roi_pct": round(total_pnl / settled_stake * 100.0, 4) if settled_stake else None,
            "win_rate": round(win_count / settled_count * 100.0, 4) if settled_count else None,
        }

    def _recent_trade_where(
        self,
        symbol: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("t.symbol = ?")
            params.append(symbol)
        if start_at is not None:
            clauses.append("COALESCE(t.settled_at, t.opened_at) >= ?")
            params.append(float(start_at))
        if end_at is not None:
            clauses.append("COALESCE(t.settled_at, t.opened_at) <= ?")
            params.append(float(end_at))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, tuple(params)

    @_locked
    def recent_paper_orders(
        self,
        limit: int = 50,
        offset: int = 0,
        symbol: str | None = None,
        status_filter: str = "all",
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        status_key = normalize_paper_order_status_filter(status_filter)
        conditions: list[str] = []
        params: list[Any] = []
        if symbol:
            conditions.append("o.symbol = ?")
            params.append(symbol)
        status_values = PAPER_ORDER_STATUS_FILTERS[status_key]
        if status_values:
            placeholders = ",".join("?" for _ in status_values)
            conditions.append(f"o.status IN ({placeholders})")
            params.extend(status_values)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        rows = self.conn.execute(
            f"""
            SELECT
                o.*,
                r.question,
                r.condition_id,
                r.url,
                COALESCE(f.fill_count, 0) AS fill_count
            FROM paper_orders o
            JOIN market_rounds r ON r.round_id = o.round_id
            LEFT JOIN (
                SELECT order_id, COUNT(*) AS fill_count
                FROM paper_fills
                GROUP BY order_id
            ) f ON f.order_id = o.id
            {where}
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ?
            OFFSET ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def paper_order_count(self, symbol: str | None = None, status_filter: str = "all") -> int:
        status_key = normalize_paper_order_status_filter(status_filter)
        conditions: list[str] = []
        params: list[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        status_values = PAPER_ORDER_STATUS_FILTERS[status_key]
        if status_values:
            placeholders = ",".join("?" for _ in status_values)
            conditions.append(f"status IN ({placeholders})")
            params.extend(status_values)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM paper_orders {where}",
            tuple(params),
        ).fetchone()
        return int(row["count"])

    @_locked
    def paper_order_summary(
        self,
        symbol: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if start_at is not None:
            conditions.append("created_at >= ?")
            params.append(float(start_at))
        if end_at is not None:
            conditions.append("created_at <= ?")
            params.append(float(end_at))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self.conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(CASE WHEN status IN ('RESTING', 'PARTIAL_RESTING') THEN 1 ELSE 0 END), 0) AS active_count,
                COALESCE(SUM(CASE WHEN status = 'FILLED' THEN 1 ELSE 0 END), 0) AS filled_count,
                COALESCE(SUM(CASE WHEN status = 'PARTIAL' THEN 1 ELSE 0 END), 0) AS partial_count,
                COALESCE(SUM(CASE WHEN status = 'PARTIAL_RESTING' THEN 1 ELSE 0 END), 0) AS partial_resting_count,
                COALESCE(SUM(CASE WHEN status = 'CANCELED' THEN 1 ELSE 0 END), 0) AS canceled_count,
                COALESCE(SUM(CASE WHEN status = 'EXPIRED' THEN 1 ELSE 0 END), 0) AS expired_count,
                COALESCE(SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END), 0) AS rejected_count,
                COALESCE(SUM(CASE WHEN post_only = 1 THEN 1 ELSE 0 END), 0) AS post_only_count,
                COALESCE(SUM(requested_cash), 0) AS requested_cash,
                COALESCE(SUM(remaining_cash), 0) AS remaining_cash,
                COALESCE(SUM(filled_shares), 0) AS filled_shares,
                COALESCE(SUM(cash_spent), 0) AS cash_spent,
                COALESCE(SUM(fee), 0) AS fee
            FROM paper_orders
            {where}
            """,
            tuple(params),
        ).fetchone()
        summary = dict(row)
        total_count = int(summary["total_count"] or 0)
        fill_attempt_count = int(summary["filled_count"] or 0) + int(summary["partial_count"] or 0) + int(summary["partial_resting_count"] or 0)
        summary.update(
            {
                "total_count": total_count,
                "active_count": int(summary["active_count"] or 0),
                "filled_count": int(summary["filled_count"] or 0),
                "partial_count": int(summary["partial_count"] or 0),
                "partial_resting_count": int(summary["partial_resting_count"] or 0),
                "canceled_count": int(summary["canceled_count"] or 0),
                "expired_count": int(summary["expired_count"] or 0),
                "rejected_count": int(summary["rejected_count"] or 0),
                "post_only_count": int(summary["post_only_count"] or 0),
                "fill_attempt_count": fill_attempt_count,
                "fill_rate": round(fill_attempt_count / total_count * 100.0, 4) if total_count else None,
                "requested_cash": round(float(summary["requested_cash"] or 0.0), 6),
                "remaining_cash": round(float(summary["remaining_cash"] or 0.0), 6),
                "filled_shares": round(float(summary["filled_shares"] or 0.0), 6),
                "cash_spent": round(float(summary["cash_spent"] or 0.0), 6),
                "fee": round(float(summary["fee"] or 0.0), 6),
            }
        )
        return summary

    @_locked
    def paper_order_fills(self, order_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM paper_fills
            WHERE order_id = ?
            ORDER BY level_index ASC, id ASC
            """,
            (order_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def active_paper_orders(self, symbol: str = "BTC", round_id: str | None = None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        params: list[Any] = [symbol, *ACTIVE_ORDER_STATUSES]
        round_filter = ""
        if round_id:
            round_filter = " AND o.round_id = ?"
            params.append(round_id)
        rows = self.conn.execute(
            f"""
            SELECT
                o.*,
                r.target_price,
                r.ends_at,
                r.question,
                r.condition_id,
                r.up_token,
                r.down_token,
                r.url
            FROM paper_orders o
            JOIN market_rounds r ON r.round_id = o.round_id
            WHERE o.symbol = ? AND o.status IN ({placeholders}){round_filter}
            ORDER BY o.created_at ASC, o.id ASC
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def active_live_exit_orders(self, symbol: str = "BTC") -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM paper_orders
            WHERE execution_mode = 'LIVE'
              AND order_type = 'FAK_SELL'
              AND symbol = ?
              AND status IN ({placeholders})
            ORDER BY created_at ASC, id ASC
            """,
            (symbol, *ACTIVE_ORDER_STATUSES),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def active_live_exit_order_for_trade(self, trade_id: int) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        row = self.conn.execute(
            f"""
            SELECT *
            FROM paper_orders
            WHERE execution_mode = 'LIVE'
              AND order_type = 'FAK_SELL'
              AND trade_id = ?
              AND status IN ({placeholders})
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (int(trade_id), *ACTIVE_ORDER_STATUSES),
        ).fetchone()
        return dict(row) if row else None

    @_locked
    def expire_resting_orders(self, now: float) -> list[dict[str, Any]]:
        active_orders = self.active_paper_orders()
        expired = [
            order
            for order in active_orders
            if (_maybe_float(order.get("expires_at")) is not None and float(order["expires_at"]) <= now)
            or (_maybe_float(order.get("ends_at")) is not None and float(order["ends_at"]) <= now)
        ]
        if not expired:
            return []
        results: list[dict[str, Any]] = []
        with self.conn:
            for order in expired:
                remaining_cash = max(0.0, round(float(order.get("remaining_cash") or 0.0), 6))
                reason = _append_reason(str(order.get("reason") or ""), f"EXPIRED release {remaining_cash:.6f}")
                self.conn.execute(
                    """
                    UPDATE paper_orders
                    SET status = 'EXPIRED',
                        remaining_cash = 0,
                        reason = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (reason, now, order["id"]),
                )
                if remaining_cash > 0:
                    self.conn.execute(
                        "UPDATE account SET cash_balance = cash_balance + ?, updated_at = ? WHERE id = 1",
                        (remaining_cash, now),
                    )
                item = dict(order)
                item.update({"status": "EXPIRED", "remaining_cash": 0.0, "released_cash": remaining_cash, "reason": reason})
                results.append(item)
        self.record_equity()
        return results

    @_locked
    def cancel_paper_order(self, order_id: int, reason: str = "manual cancel", now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        row = self.conn.execute(
            "SELECT * FROM paper_orders WHERE id = ?",
            (int(order_id),),
        ).fetchone()
        if row is None:
            return {"canceled": [], "not_canceled": {str(order_id): "order not found"}}
        order = dict(row)
        if order["status"] not in ACTIVE_ORDER_STATUSES:
            return {"canceled": [], "not_canceled": {str(order_id): f"order status is {order['status']}"}}
        remaining_cash = max(0.0, round(float(order.get("remaining_cash") or 0.0), 6))
        cancel_reason = _append_reason(str(order.get("reason") or ""), f"CANCELED {reason} release {remaining_cash:.6f}")
        with self.conn:
            self.conn.execute(
                """
                UPDATE paper_orders
                SET status = 'CANCELED',
                    remaining_cash = 0,
                    reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (cancel_reason, now, order["id"]),
            )
            if remaining_cash > 0:
                self.conn.execute(
                    "UPDATE account SET cash_balance = cash_balance + ?, updated_at = ? WHERE id = 1",
                    (remaining_cash, now),
                )
        self.record_equity()
        return {
            "canceled": [int(order_id)],
            "not_canceled": {},
            "released_cash": remaining_cash,
            "order": {
                **order,
                "status": "CANCELED",
                "remaining_cash": 0.0,
                "reason": cancel_reason,
                "updated_at": now,
            },
        }

    @_locked
    def cancel_active_paper_orders(
        self,
        *,
        symbol: str = "BTC",
        round_id: str | None = None,
        reason: str = "manual batch cancel",
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        orders = self.active_paper_orders(symbol, round_id)
        if not orders:
            return {"canceled": [], "not_canceled": {}, "released_cash": 0.0, "orders": []}

        canceled: list[int] = []
        updated_orders: list[dict[str, Any]] = []
        released_cash = 0.0
        with self.conn:
            for order in orders:
                order_id = int(order["id"])
                remaining_cash = max(0.0, round(float(order.get("remaining_cash") or 0.0), 6))
                cancel_reason = _append_reason(str(order.get("reason") or ""), f"CANCELED {reason} release {remaining_cash:.6f}")
                self.conn.execute(
                    """
                    UPDATE paper_orders
                    SET status = 'CANCELED',
                        remaining_cash = 0,
                        reason = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (cancel_reason, now, order_id),
                )
                canceled.append(order_id)
                released_cash = round(released_cash + remaining_cash, 6)
                updated = dict(order)
                updated.update(
                    {
                        "status": "CANCELED",
                        "remaining_cash": 0.0,
                        "released_cash": remaining_cash,
                        "reason": cancel_reason,
                        "updated_at": now,
                    }
                )
                updated_orders.append(updated)
            if released_cash > 0:
                self.conn.execute(
                    "UPDATE account SET cash_balance = cash_balance + ?, updated_at = ? WHERE id = 1",
                    (released_cash, now),
                )
        self.record_equity()
        return {
            "canceled": canceled,
            "not_canceled": {},
            "released_cash": released_cash,
            "orders": updated_orders,
        }

    @_locked
    def fill_resting_order(
        self,
        order: dict[str, Any],
        *,
        fill_price: float,
        shares: float,
        notional: float,
        fee: float,
        cash_spent: float,
        level_price: float,
        reason: str,
        now: float,
    ) -> dict[str, Any] | None:
        if shares <= 0 or cash_spent <= 0:
            return None
        order_id = int(order["id"])
        current = self.conn.execute(
            "SELECT * FROM paper_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if current is None or current["status"] not in ACTIVE_ORDER_STATUSES:
            return None
        remaining_cash = max(0.0, round(float(current["remaining_cash"] or 0.0), 6))
        previous_shares = round(float(current["filled_shares"] or 0.0), 6)
        previous_notional = round(float(current["notional"] or 0.0), 6)
        previous_fee = round(float(current["fee"] or 0.0), 6)
        previous_cash_spent = round(float(current["cash_spent"] or 0.0), 6)
        cash_spent = min(round(float(cash_spent), 6), remaining_cash)
        if cash_spent < PAPER_MIN_RESTING_FILL_CASH:
            if previous_shares > 0 and remaining_cash <= PAPER_DUST_RELEASE_CASH:
                return self._release_resting_order_dust(dict(current), remaining_cash, now)
            return None
        if cash_spent <= 0:
            return None
        fill_price = max(0.01, min(0.99, round(float(fill_price), 4)))
        shares = round(min(float(shares), cash_spent / fill_price), 6)
        if shares <= 0:
            return None
        notional = round(shares * fill_price, 6)
        fee = max(0.0, round(float(fee), 6))
        cash_spent = round(notional + fee, 6)
        next_remaining = max(0.0, round(remaining_cash - cash_spent, 6))
        total_shares = round(previous_shares + shares, 6)
        total_notional = round(previous_notional + notional, 6)
        total_fee = round(previous_fee + fee, 6)
        total_cash_spent = round(previous_cash_spent + cash_spent, 6)
        avg_fill_price = round(total_notional / total_shares, 4) if total_shares > 0 else fill_price
        order_reason = _append_reason(str(current["reason"] or ""), reason)
        dust_release = 0.0
        if next_remaining <= PAPER_DUST_RELEASE_CASH:
            dust_release = next_remaining
            next_remaining = 0.0
            if dust_release > 0:
                order_reason = _append_reason(order_reason, f"DUST_RELEASE release {dust_release:.6f}")
        status = "FILLED" if next_remaining <= 0.000001 else "PARTIAL_RESTING"
        with self.conn:
            trade_id = self._upsert_resting_order_trade(
                current,
                fill_price=fill_price,
                shares=shares,
                cash_spent=cash_spent,
                fill_reason=reason,
                order_reason=order_reason,
                now=now,
            )
            level_row = self.conn.execute(
                "SELECT COALESCE(MAX(level_index), 0) + 1 AS next_index FROM paper_fills WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            level_index = int(level_row["next_index"] or 1)
            self.conn.execute(
                """
                INSERT INTO paper_fills(
                    order_id, trade_id, level_index, price, shares,
                    notional, fee, cash_spent, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    trade_id,
                    level_index,
                    round(float(level_price), 4),
                    shares,
                    notional,
                    fee,
                    cash_spent,
                    now,
                ),
            )
            if dust_release > 0:
                self.conn.execute(
                    "UPDATE account SET cash_balance = cash_balance + ?, updated_at = ? WHERE id = 1",
                    (dust_release, now),
                )
            self.conn.execute(
                """
                UPDATE paper_orders
                SET status = ?,
                    filled_shares = ?,
                    avg_fill_price = ?,
                    notional = ?,
                    fee = ?,
                    cash_spent = ?,
                    remaining_cash = ?,
                    trade_id = COALESCE(trade_id, ?),
                    reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    total_shares,
                    avg_fill_price,
                    total_notional,
                    total_fee,
                    total_cash_spent,
                    next_remaining,
                    trade_id,
                    order_reason,
                    now,
                    order_id,
                ),
            )
        self.record_equity()
        result = dict(current)
        result.update(
            {
                "status": status,
                "trade_id": trade_id,
                "filled_shares": total_shares,
                "avg_fill_price": avg_fill_price,
                "notional": total_notional,
                "fee": total_fee,
                "cash_spent": total_cash_spent,
                "remaining_cash": next_remaining,
                "reason": order_reason,
            }
        )
        return result

    def _upsert_resting_order_trade(
        self,
        current: sqlite3.Row,
        *,
        fill_price: float,
        shares: float,
        cash_spent: float,
        fill_reason: str,
        order_reason: str,
        now: float,
    ) -> int:
        existing_trade_id = int(current["trade_id"] or 0)
        existing_trade = None
        if existing_trade_id > 0:
            existing_trade = self.conn.execute(
                "SELECT * FROM trades WHERE id = ? AND status = 'OPEN'",
                (existing_trade_id,),
            ).fetchone()
        if existing_trade is None:
            cur = self.conn.execute(
                """
                INSERT INTO trades(
                    round_id, symbol, side, stake, entry_price, shares, confidence,
                    move_bps, status, opened_at, reason
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    current["round_id"],
                    current["symbol"],
                    current["side"],
                    cash_spent,
                    fill_price,
                    shares,
                    float(current["confidence"] or 0.0),
                    float(current["move_bps"] or 0.0),
                    now,
                    order_reason,
                ),
            )
            return int(cur.lastrowid)

        previous_stake = round(float(existing_trade["stake"] or 0.0), 6)
        previous_shares = round(float(existing_trade["shares"] or 0.0), 6)
        total_stake = round(previous_stake + cash_spent, 6)
        total_shares = round(previous_shares + shares, 6)
        previous_notional = round(previous_shares * float(existing_trade["entry_price"] or 0.0), 6)
        next_notional = round(previous_notional + shares * fill_price, 6)
        avg_entry_price = round(next_notional / total_shares, 4) if total_shares > 0 else fill_price
        trade_reason = _append_reason(str(existing_trade["reason"] or ""), fill_reason)
        self.conn.execute(
            """
            UPDATE trades
            SET stake = ?,
                entry_price = ?,
                shares = ?,
                reason = ?
            WHERE id = ?
            """,
            (total_stake, avg_entry_price, total_shares, trade_reason, existing_trade_id),
        )
        return existing_trade_id

    def _release_resting_order_dust(self, current: dict[str, Any], remaining_cash: float, now: float) -> dict[str, Any] | None:
        order_id = int(current["id"])
        release_cash = max(0.0, round(float(remaining_cash or 0.0), 6))
        if release_cash <= 0:
            return None
        reason = _append_reason(
            str(current.get("reason") or ""),
            f"DUST_RELEASE skipped fill below {PAPER_MIN_RESTING_FILL_CASH:.2f}, release {release_cash:.6f}",
        )
        with self.conn:
            self.conn.execute(
                """
                UPDATE paper_orders
                SET status = 'FILLED',
                    remaining_cash = 0,
                    reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (reason, now, order_id),
            )
            self.conn.execute(
                "UPDATE account SET cash_balance = cash_balance + ?, updated_at = ? WHERE id = 1",
                (release_cash, now),
            )
        self.record_equity()
        result = dict(current)
        result.update({"status": "FILLED", "remaining_cash": 0.0, "reason": reason})
        return result

    @_locked
    def metrics(self) -> dict[str, Any]:
        account = self.account()
        open_rows = self.open_trades()
        open_risk = round(sum(float(row["stake"]) for row in open_rows), 6)
        reserved_cash = self.reserved_cash()
        settled = self.conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status = 'SETTLED'").fetchone()["c"]
        wins = self.conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status = 'SETTLED' AND pnl > 0").fetchone()["c"]
        losses = self.conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status = 'SETTLED' AND pnl < 0").fetchone()["c"]
        row = self.conn.execute("SELECT COALESCE(MIN(total_equity), ?) AS min_equity FROM equity_curve", (account["initial_balance"],)).fetchone()
        total_equity = round(float(account["cash_balance"]) + open_risk + reserved_cash, 6)
        total_pnl = round(total_equity - float(account["initial_balance"]), 6)
        max_drawdown = round(float(account["initial_balance"]) - float(row["min_equity"]), 6)
        return {
            "initial_balance": float(account["initial_balance"]),
            "cash_balance": round(float(account["cash_balance"]), 6),
            "reserved_cash": reserved_cash,
            "open_risk": open_risk,
            "realized_pnl": round(float(account["realized_pnl"]), 6),
            "unrealized_pnl": 0.0,
            "total_equity": total_equity,
            "total_pnl": total_pnl,
            "total_pnl_pct": round(total_pnl / float(account["initial_balance"]) * 100, 4),
            "settled_trades": int(settled),
            "open_trades": len(open_rows),
            "wins": int(wins),
            "losses": int(losses),
            "win_rate": round((int(wins) / int(settled) * 100) if settled else 0.0, 4),
            "max_drawdown": max(0.0, max_drawdown),
            "daily_realized_pnl": round(self.daily_realized_pnl(), 6),
        }

    @_locked
    def record_equity(self) -> None:
        account = self.account()
        open_risk = round(sum(float(row["stake"]) for row in self.open_trades()), 6)
        reserved_cash = self.reserved_cash()
        total_equity = round(float(account["cash_balance"]) + open_risk + reserved_cash, 6)
        self.conn.execute(
            """
            INSERT INTO equity_curve(cash_balance, open_risk, realized_pnl, total_equity, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (account["cash_balance"], open_risk, account["realized_pnl"], total_equity, time.time()),
        )
        self.conn.commit()

    @_locked
    def equity_curve(self, limit: int = 120) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT cash_balance, open_risk, realized_pnl, total_equity, created_at
            FROM equity_curve
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return self._equity_rows([dict(row) for row in reversed(rows)])

    @_locked
    def equity_curve_window(
        self,
        days: int = 90,
        max_points: int = 1200,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        days = max(1, min(365, int(days)))
        max_points = max(2, min(5000, int(max_points)))
        end_at = time.time() if now is None else float(now)
        start_at = end_at - days * 24 * 60 * 60
        count_row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM equity_curve WHERE created_at >= ? AND created_at <= ?",
            (start_at, end_at),
        ).fetchone()
        count = int(count_row["count"] or 0)
        if count <= max_points:
            rows = self.conn.execute(
                """
                SELECT cash_balance, open_risk, realized_pnl, total_equity, created_at
                FROM equity_curve
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at ASC
                """,
                (start_at, end_at),
            ).fetchall()
            return self._equity_rows([dict(row) for row in rows])

        stride = max(1, -(-count // max(1, max_points - 1)))
        rows = self.conn.execute(
            """
            SELECT cash_balance, open_risk, realized_pnl, total_equity, created_at
            FROM (
                SELECT
                    cash_balance,
                    open_risk,
                    realized_pnl,
                    total_equity,
                    created_at,
                    ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC) AS row_num,
                    COUNT(*) OVER () AS total_count
                FROM equity_curve
                WHERE created_at >= ? AND created_at <= ?
            )
            WHERE row_num = 1
               OR row_num = total_count
               OR ((row_num - 1) % ?) = 0
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (start_at, end_at, stride, max_points),
        ).fetchall()
        return self._equity_rows([dict(row) for row in rows])

    def _equity_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        initial_balance = float(self.account()["initial_balance"])
        result: list[dict[str, Any]] = []
        for row in rows:
            total_equity = round(float(row["total_equity"]), 6)
            total_pnl = round(total_equity - initial_balance, 6)
            item = dict(row)
            item["cash_balance"] = round(float(item["cash_balance"]), 6)
            item["open_risk"] = round(float(item["open_risk"]), 6)
            item["realized_pnl"] = round(float(item["realized_pnl"]), 6)
            item["total_equity"] = total_equity
            item["total_pnl"] = total_pnl
            item["total_pnl_pct"] = round(total_pnl / initial_balance * 100, 4) if initial_balance else 0.0
            result.append(item)
        return result


def _normalize_side(side: str) -> str:
    text = str(side or "").strip().lower()
    if text == "up":
        return "Up"
    if text == "down":
        return "Down"
    return str(side or "")


def _append_reason(existing: str, reason: str) -> str:
    existing = existing.strip()
    reason = reason.strip()
    if not existing:
        return reason
    if not reason:
        return existing
    return f"{existing} | {reason}"


def _empty_trade_reason_summary(marker: str, start_at: float | None, end_at: float | None) -> dict[str, Any]:
    return {
        "reason_marker": marker,
        "start_at": start_at,
        "end_at": end_at,
        "total_count": 0,
        "settled_count": 0,
        "open_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "total_stake": 0.0,
        "settled_stake": 0.0,
        "open_risk": 0.0,
        "total_pnl": 0.0,
        "roi_pct": None,
        "win_rate": None,
    }


def _nullable_price(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, round(float(value), 4)))


def _positive_price_or_none(value: float | None) -> float | None:
    parsed = _maybe_float(value)
    if parsed is None or parsed <= 0:
        return None
    return float(parsed)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item or "").strip()]


def _llm_review_attribution_note() -> str:
    return (
        "trades are attributed to the latest same-round LLM trade route near trade open time; "
        "NO_TRADE opportunity is an estimate from recorded decision-time quotes, not a real fill."
    )


def _empty_llm_review_summary(total_decision_count: int, sample_limit: int) -> dict[str, Any]:
    summary = _llm_review_bucket("summary")
    summary.update(
        {
            "total_decision_count": int(total_decision_count or 0),
            "sample_limit": int(sample_limit or 0),
            "decision_count": 0,
        }
    )
    return summary


def _llm_review_bucket(key: str) -> dict[str, Any]:
    return {
        "key": str(key or "UNKNOWN"),
        "decision_count": 0,
        "allow_count": 0,
        "block_count": 0,
        "no_trade_count": 0,
        "llm_source_count": 0,
        "local_source_count": 0,
        "error_count": 0,
        "trade_count": 0,
        "settled_trade_count": 0,
        "open_trade_count": 0,
        "total_stake": 0.0,
        "total_pnl": 0.0,
        "avg_pnl": None,
        "win_count": 0,
        "loss_count": 0,
        "no_trade_evaluated_count": 0,
        "no_trade_direction_win_count": 0,
        "no_trade_direction_estimated_pnl": 0.0,
        "no_trade_best_estimated_pnl": 0.0,
    }


def _llm_review_add_decision(
    bucket: dict[str, Any],
    allow_trade: bool,
    source: str,
    error_text: str,
    route: str,
) -> None:
    bucket["decision_count"] = int(bucket.get("decision_count") or 0) + 1
    if allow_trade:
        bucket["allow_count"] = int(bucket.get("allow_count") or 0) + 1
    else:
        bucket["block_count"] = int(bucket.get("block_count") or 0) + 1
    if str(route or "").upper() == "NO_TRADE":
        bucket["no_trade_count"] = int(bucket.get("no_trade_count") or 0) + 1
    normalized_source = str(source or "").lower()
    if normalized_source.startswith("llm"):
        bucket["llm_source_count"] = int(bucket.get("llm_source_count") or 0) + 1
    if normalized_source.startswith("local"):
        bucket["local_source_count"] = int(bucket.get("local_source_count") or 0) + 1
    if error_text:
        bucket["error_count"] = int(bucket.get("error_count") or 0) + 1


def _llm_review_add_trade(bucket: dict[str, Any], trade: dict[str, Any]) -> None:
    bucket["trade_count"] = int(bucket.get("trade_count") or 0) + 1
    bucket["total_stake"] = round(float(bucket.get("total_stake") or 0.0) + float(trade.get("stake") or 0.0), 6)
    if str(trade.get("status") or "").upper() == "SETTLED":
        pnl = float(trade.get("pnl") or 0.0)
        bucket["settled_trade_count"] = int(bucket.get("settled_trade_count") or 0) + 1
        bucket["total_pnl"] = round(float(bucket.get("total_pnl") or 0.0) + pnl, 6)
        if pnl > 0:
            bucket["win_count"] = int(bucket.get("win_count") or 0) + 1
        elif pnl < 0:
            bucket["loss_count"] = int(bucket.get("loss_count") or 0) + 1
    else:
        bucket["open_trade_count"] = int(bucket.get("open_trade_count") or 0) + 1


def _llm_review_add_no_trade_estimate(bucket: dict[str, Any], estimate: dict[str, Any]) -> None:
    if not estimate.get("evaluated"):
        return
    bucket["no_trade_evaluated_count"] = int(bucket.get("no_trade_evaluated_count") or 0) + 1
    if estimate.get("direction_would_win"):
        bucket["no_trade_direction_win_count"] = int(bucket.get("no_trade_direction_win_count") or 0) + 1
    bucket["no_trade_direction_estimated_pnl"] = round(
        float(bucket.get("no_trade_direction_estimated_pnl") or 0.0)
        + float(estimate.get("direction_estimated_pnl") or 0.0),
        6,
    )
    bucket["no_trade_best_estimated_pnl"] = round(
        float(bucket.get("no_trade_best_estimated_pnl") or 0.0)
        + float(estimate.get("winner_estimated_pnl") or 0.0),
        6,
    )


def _finalize_llm_review_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    item = dict(bucket)
    settled = int(item.get("settled_trade_count") or 0)
    evaluated = int(item.get("no_trade_evaluated_count") or 0)
    decision_count = int(item.get("decision_count") or 0)
    item["total_stake"] = round(float(item.get("total_stake") or 0.0), 6)
    item["total_pnl"] = round(float(item.get("total_pnl") or 0.0), 6)
    item["avg_pnl"] = round(item["total_pnl"] / settled, 6) if settled else None
    item["win_rate"] = round(int(item.get("win_count") or 0) / settled * 100.0, 4) if settled else None
    item["allow_rate"] = round(int(item.get("allow_count") or 0) / decision_count * 100.0, 4) if decision_count else None
    item["no_trade_direction_win_rate"] = (
        round(int(item.get("no_trade_direction_win_count") or 0) / evaluated * 100.0, 4)
        if evaluated
        else None
    )
    item["no_trade_direction_estimated_pnl"] = round(float(item.get("no_trade_direction_estimated_pnl") or 0.0), 6)
    item["no_trade_best_estimated_pnl"] = round(float(item.get("no_trade_best_estimated_pnl") or 0.0), 6)
    return item


def _llm_feature_side_ask(features: dict[str, Any], side: str) -> float | None:
    normalized = _normalize_side(side)
    key = normalized.lower() if normalized in {"Up", "Down"} else ""
    quote = features.get(key) if key and isinstance(features.get(key), dict) else {}
    return _maybe_float(quote.get("ask"))


def _llm_no_trade_estimate(features: dict[str, Any], outcome: str, stake: float) -> dict[str, Any]:
    direction_side = _normalize_side(str(features.get("direction_side") or ""))
    normalized_outcome = _normalize_side(outcome)
    direction_ask = _llm_feature_side_ask(features, direction_side)
    winner_ask = _llm_feature_side_ask(features, normalized_outcome)
    direction_pnl = _llm_estimated_binary_pnl(stake, direction_ask, direction_side, normalized_outcome)
    winner_pnl = _llm_estimated_binary_pnl(stake, winner_ask, normalized_outcome, normalized_outcome)
    evaluated = normalized_outcome in {"Up", "Down"} and direction_side in {"Up", "Down"} and direction_ask is not None
    return {
        "evaluated": bool(evaluated),
        "stake": round(float(stake or 0.0), 6),
        "direction_side": direction_side or None,
        "outcome": normalized_outcome or None,
        "direction_ask": direction_ask,
        "winner_ask": winner_ask,
        "direction_would_win": bool(evaluated and direction_side == normalized_outcome),
        "direction_estimated_pnl": direction_pnl,
        "winner_estimated_pnl": winner_pnl,
    }


def _llm_estimated_binary_pnl(stake: float, ask: float | None, side: str, outcome: str) -> float | None:
    if ask is None or stake <= 0:
        return None
    price = max(0.01, min(0.99, float(ask)))
    win = _normalize_side(side) == _normalize_side(outcome)
    pnl = stake / price - stake if win else -stake
    return round(pnl, 6)


def _match_llm_decision_for_trade(
    trade: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not decisions:
        return None
    route = _llm_route_from_trade_reason(str(trade.get("reason") or ""))
    opened_at = _maybe_float(trade.get("opened_at")) or 0.0
    candidates: list[dict[str, Any]] = []
    for decision in decisions:
        if not decision.get("allow_trade"):
            continue
        if str(decision.get("route") or "").upper() == "NO_TRADE":
            continue
        if route and str(decision.get("route") or "") != route:
            continue
        created_at = _maybe_float(decision.get("created_at")) or 0.0
        if opened_at - 30.0 <= created_at <= opened_at + 2.0:
            candidates.append(decision)
    if not candidates and route:
        candidates = [
            decision
            for decision in decisions
            if decision.get("allow_trade") and str(decision.get("route") or "") == route
        ]
    if not candidates:
        candidates = [
            decision
            for decision in decisions
            if decision.get("allow_trade") and str(decision.get("route") or "").upper() != "NO_TRADE"
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (float(item.get("created_at") or 0.0), int(item.get("id") or 0)))
    return candidates[-1]


def _llm_route_from_trade_reason(reason: str) -> str | None:
    marker = "LLM_SUPER_AGENT route "
    index = reason.find(marker)
    if index < 0:
        return None
    start = index + len(marker)
    end = reason.find(",", start)
    if end < 0:
        end = reason.find("|", start)
    if end < 0:
        end = len(reason)
    route = reason[start:end].strip()
    return route or None


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

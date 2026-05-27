from __future__ import annotations

import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from .models import MarketRound, Signal, TradeIntent


SCHEMA_VERSION = 2


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
                settled_at REAL
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
                reason TEXT NOT NULL,
                FOREIGN KEY(round_id) REFERENCES market_rounds(round_id)
            );

            CREATE TABLE IF NOT EXISTS price_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cash_balance REAL NOT NULL,
                open_risk REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                total_equity REAL NOT NULL,
                created_at REAL NOT NULL
            );
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
            "SELECT id FROM trades WHERE round_id = ? AND side = ? AND status = 'OPEN' LIMIT 1",
            (round_id, normalized),
        ).fetchone()
        return row is not None

    @_locked
    def open_trade_exists_for_round(self, round_id: str) -> bool:
        row = self.conn.execute(
            "SELECT id FROM trades WHERE round_id = ? AND status = 'OPEN' LIMIT 1",
            (round_id,),
        ).fetchone()
        return row is not None

    @_locked
    def open_trade_count(self, symbol: str | None = None) -> int:
        if symbol:
            row = self.conn.execute(
                "SELECT COUNT(*) AS count FROM trades WHERE status = 'OPEN' AND symbol = ?",
                (symbol,),
            ).fetchone()
            return int(row["count"])
        row = self.conn.execute("SELECT COUNT(*) AS count FROM trades WHERE status = 'OPEN'").fetchone()
        return int(row["count"])

    @_locked
    def account(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("account was not initialized")
        return dict(row)

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
    def close_trade_shares(
        self,
        trade_id: int,
        shares_to_close: float,
        exit_price: float,
        now: float | None = None,
        reason: str = "manual close",
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
        close_all = close_shares >= open_shares - 0.000001
        close_ratio = 1.0 if close_all else close_shares / open_shares
        close_stake = round(float(trade["stake"]) * close_ratio, 6)
        payout = round(close_shares * exit_price, 6)
        pnl = round(payout - close_stake, 6)
        close_reason = _append_reason(str(trade["reason"] or ""), reason)
        with self.conn:
            if close_all:
                self.conn.execute(
                    """
                    UPDATE trades
                    SET status = 'SETTLED', settled_at = ?, exit_price = ?, payout = ?, pnl = ?, reason = ?
                    WHERE id = ?
                    """,
                    (now, exit_price, payout, pnl, close_reason, trade_id),
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
                        move_bps, status, opened_at, settled_at, exit_price, payout, pnl, reason
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'SETTLED', ?, ?, ?, ?, ?, ?)
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
            final_price = prices.get(symbol)
            if final_price is None:
                continue
            outcome = "Up" if final_price >= row["target_price"] else "Down"
            settled.extend(self._settle_round(row["round_id"], final_price, outcome, now))
        if settled:
            self.record_equity()
        return settled

    @_locked
    def settle_round_outcome(
        self,
        round_id: str,
        outcome: str,
        now: float | None = None,
        final_price: float | None = None,
    ) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        row = self.get_round(round_id)
        fallback_price = float(row["target_price"]) if row and row.get("target_price") is not None else 0.0
        settled = self._settle_round(round_id, final_price if final_price is not None else fallback_price, _normalize_side(outcome), now)
        if settled:
            self.record_equity()
        return settled

    @_locked
    def _settle_round(self, round_id: str, final_price: float, outcome: str, now: float) -> list[dict[str, Any]]:
        normalized_outcome = _normalize_side(outcome)
        trades = self.conn.execute(
            "SELECT * FROM trades WHERE round_id = ? AND status = 'OPEN'",
            (round_id,),
        ).fetchall()
        settled: list[dict[str, Any]] = []
        with self.conn:
            self.conn.execute(
                """
                UPDATE market_rounds
                SET final_price = ?, outcome = ?, settled_at = ?
                WHERE round_id = ?
                """,
                (final_price, normalized_outcome, now, round_id),
            )
            for trade in trades:
                win = _normalize_side(trade["side"]) == normalized_outcome
                payout = round(float(trade["shares"]) if win else 0.0, 6)
                pnl = round(payout - float(trade["stake"]), 6)
                self.conn.execute(
                    """
                    UPDATE trades
                    SET status = 'SETTLED', settled_at = ?, exit_price = ?, payout = ?, pnl = ?
                    WHERE id = ?
                    """,
                    (now, 1.0 if win else 0.0, payout, pnl, trade["id"]),
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
                item.update({"outcome": normalized_outcome, "payout": payout, "pnl": pnl, "final_price": final_price})
                settled.append(item)
        return settled

    @_locked
    def open_trades(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                t.*,
                r.target_price,
                r.ends_at,
                r.question,
                r.condition_id,
                r.up_token,
                r.down_token,
                r.url,
                r.final_price,
                r.outcome
            FROM trades t
            JOIN market_rounds r ON r.round_id = t.round_id
            WHERE t.status = 'OPEN'
            ORDER BY t.opened_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def recent_trades(self, limit: int = 30, offset: int = 0, symbol: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE t.symbol = ?" if symbol else ""
        params: tuple[Any, ...] = (symbol, limit, offset) if symbol else (limit, offset)
        rows = self.conn.execute(
            f"""
            SELECT
                t.*,
                r.target_price,
                r.final_price,
                r.outcome,
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
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def recent_trade_count(self, symbol: str | None = None) -> int:
        if symbol:
            row = self.conn.execute(
                "SELECT COUNT(*) AS count FROM trades WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM trades").fetchone()
        return int(row["count"])

    @_locked
    def metrics(self) -> dict[str, Any]:
        account = self.account()
        open_rows = self.open_trades()
        open_risk = round(sum(float(row["stake"]) for row in open_rows), 6)
        settled = self.conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status = 'SETTLED'").fetchone()["c"]
        wins = self.conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status = 'SETTLED' AND pnl > 0").fetchone()["c"]
        losses = self.conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status = 'SETTLED' AND pnl < 0").fetchone()["c"]
        row = self.conn.execute("SELECT COALESCE(MIN(total_equity), ?) AS min_equity FROM equity_curve", (account["initial_balance"],)).fetchone()
        total_equity = round(float(account["cash_balance"]) + open_risk, 6)
        total_pnl = round(total_equity - float(account["initial_balance"]), 6)
        max_drawdown = round(float(account["initial_balance"]) - float(row["min_equity"]), 6)
        return {
            "initial_balance": float(account["initial_balance"]),
            "cash_balance": round(float(account["cash_balance"]), 6),
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
        total_equity = round(float(account["cash_balance"]) + open_risk, 6)
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

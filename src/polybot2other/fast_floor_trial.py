"""Isolated FAST Paper arm with a pre-submission fee-inclusive price floor."""
import json

from .optimized_fast import VARIANT_ID
from .paper_execution import PaperExecution, sweep_book


FLOOR = 0.60


class FastFloorPaperExecution(PaperExecution):
    """Use the ordinary FAST signal, risk checks and delayed matching on a separate ledger."""

    def __init__(self, bot, strategy_id, base_settings, **kwargs):
        if strategy_id != VARIANT_ID:
            raise ValueError("极速买价过滤实验只能绑定极速 Paper 策略")
        self._buy_evidence = None
        super().__init__(bot, strategy_id, base_settings, **kwargs)

    def _create_schema(self):
        super()._create_schema()
        with self.store._lock, self.store.conn:
            self.store.conn.execute("""
                CREATE TABLE IF NOT EXISTS fast_floor_checks (
                    id INTEGER PRIMARY KEY, round_id TEXT NOT NULL, side TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, observed_at REAL NOT NULL,
                    book_source_ms REAL NOT NULL, book_received_ms REAL NOT NULL,
                    best_ask REAL NOT NULL, limit_price REAL NOT NULL, budget REAL NOT NULL,
                    quote_shares REAL NOT NULL, quote_notional REAL NOT NULL, quote_fee REAL NOT NULL,
                    cost_per_share REAL NOT NULL, floor REAL NOT NULL, allowed INTEGER NOT NULL,
                    quote_json TEXT NOT NULL,
                    UNIQUE(round_id,side,book_source_ms,book_received_ms,snapshot_id)
                )
            """)

    def enqueue_buy(self, market, signal, ceiling, rule, revision, info, *, evidence=None):
        # Base preflight places the exact REST book and final market frame here.
        # _queue is called synchronously only after its normal recheck and risk gates.
        with self.lock:
            self._buy_evidence = {} if evidence is None else evidence
            try:
                return super().enqueue_buy(market, signal, ceiling, rule, revision, info,
                                           evidence=self._buy_evidence)
            finally:
                self._buy_evidence = None

    def _queue(self, market, signal, action, terms, revision, delay, trade_id=None, exit_reason=None):
        if action == "BUY":
            preflight = self._buy_evidence
            if not preflight or not preflight.get("recheck_complete"):
                raise ValueError("缺少极速实验提交前盘口")
            book = preflight["submission_book"]
            # Price the intended notional through the displayed executable asks.
            # The existing simulator still applies its depth haircut and latency at arrival.
            quote = sweep_book(book, "BUY", terms["limit"], terms["info"],
                               notional_budget=terms["notional_budget"])
            shares = quote["shares"]
            if shares <= 0:
                raise ValueError("极速实验限价内没有可报价的份额")
            cost = quote["cash"] / shares
            # Ledger cash is rounded to six decimals. Compare at that same precision
            # so an exact 60-cent quote passes despite share/notional rounding.
            allowed = quote["cash"] >= round(FLOOR * shares, 6)
            review = {"token": book["token_id"], "fee_rate": terms["info"]["fee_rate"],
                      "fee_exponent": terms["info"]["fee_exponent"], "tick_size": terms["info"]["tick_size"],
                      "min_order_size": book["min_order_size"], "levels": quote["levels"]}
            with self.store._lock, self.store.conn:
                self.store.conn.execute("""
                    INSERT OR IGNORE INTO fast_floor_checks
                        (round_id,side,snapshot_id,observed_at,book_source_ms,book_received_ms,
                         best_ask,limit_price,budget,quote_shares,quote_notional,quote_fee,
                         cost_per_share,floor,allowed,quote_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (market.round_id, signal.side, preflight["recheck_frame"].snapshot_id,
                      preflight["book_checked_at"], book["source_ms"], book["received_ms"],
                      book["best_ask"], terms["limit"], terms["notional_budget"], shares,
                      quote["notional"], quote["fee"], cost, FLOOR, int(allowed),
                      json.dumps(review, separators=(",", ":"), allow_nan=False)))
            if not allowed:
                self.event("ENTRY_LIMIT", f"极速实验提交前每份含费报价 ${cost:.4f}，低于 ${FLOOR:.2f}，跳过买入")
                return None
            terms = {**terms, "fast_floor_quote": {"cost_per_share": cost, "floor": FLOOR,
                     "book_source_ms": book["source_ms"], "snapshot_id": preflight["recheck_frame"].snapshot_id}}
        return super()._queue(market, signal, action, terms, revision, delay, trade_id, exit_reason)

    def fast_floor_summary(self):
        with self.store._lock:
            row = self.store.conn.execute("""
                SELECT count(*) AS checked, count(DISTINCT round_id) AS markets,
                       count(CASE WHEN allowed=0 THEN 1 END) AS skipped,
                       count(DISTINCT CASE WHEN allowed=0 THEN round_id END) AS skipped_markets
                FROM fast_floor_checks
            """).fetchone()
        return {**dict(row), "floor": FLOOR}

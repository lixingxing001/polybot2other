"""Read-only candidate observations from the trial feed and the original Paper order ledger."""
from collections import deque
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .forecast_paper import _fresh_reference
from .optimized_reference import SPOT, TWAP
from .paper_execution import checked_info
from .shared_market_inputs import quote_error
from .strategy_league import _valid_book
from .strategy_league_policies import POLICIES, SIDES, _window, entry_decision, sample_frame


WATCH = tuple(p for p in POLICIES if p.key in ("rev_return", "close_diverge"))
TAIL_SECONDS = 17
_SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnostic_run (
 id INTEGER PRIMARY KEY CHECK(id=1), start_at REAL NOT NULL, last_at REAL);
CREATE TABLE IF NOT EXISTS diagnostic_counts (
 round_id TEXT NOT NULL, band TEXT NOT NULL, subject TEXT NOT NULL, reason TEXT NOT NULL,
 samples INTEGER NOT NULL, PRIMARY KEY(round_id,band,subject,reason));
CREATE TABLE IF NOT EXISTS diagnostic_sells (
 id INTEGER PRIMARY KEY, status TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS diagnostic_gaps (
 observed_at REAL PRIMARY KEY, previous_at REAL NOT NULL);
"""


def _book_reasons(frame, side, info, rules, now):
    market = frame.market
    token = market.up_token if side == "Up" else market.down_token
    label = "涨方" if side == "Up" else "跌方"
    raw = frame.quotes.get(side) or {}
    if str(raw.get("token_id")) != token:
        return [label + "合约标识缺失或错误"]
    issues = []
    for field, maximum, name in (("source_ms", 3000, "来源"), ("received_ms", 2500, "接收")):
        stamp = raw.get(field)
        if not isinstance(stamp, (int, float)) or not 0 <= now * 1000 - stamp <= maximum:
            issues.append(label + "盘口" + name + "时间缺失或不新鲜")
    if quote_error(market, frame.quotes, side, now) and not issues:
        issues.append(label + "买卖一价缺失或交叉")
    rule = rules.get(side) or rules.get(token) or {}
    if info and (rule.get("token_id") != token or str(rule.get("tick_size")) != str(info["tick_size"])
                 or not isinstance(rule.get("min_order_size"), (int, float))
                 or not math.isfinite(rule["min_order_size"]) or rule["min_order_size"] <= 0):
        issues.append(label + "最小下单量或价格精度未核实")
    if info and raw.get("tick_size") is not None and str(raw["tick_size"]) != str(info["tick_size"]):
        issues.append(label + "盘口与市场价格精度不一致")
    for levels, top, choose, name in (("bids", "best_bid", max, "买盘"), ("asks", "best_ask", min, "卖盘")):
        try:
            pairs = [(float(level["price"]), float(level["size"])) for level in raw.get(levels, [])]
            good = all(0 < price < 1 and math.isfinite(size) and size > 0 for price, size in pairs)
            if not pairs or not good:
                issues.append(label + name + "缺少有效深度")
            elif abs(choose(p for p, _ in pairs) - raw[top]) > 1e-7:
                issues.append(label + name + "深度与一档价格不一致")
        except (TypeError, ValueError, KeyError):
            issues.append(label + name + "深度字段无效")
    return issues


def _sample_and_issues(frame, decision, info, rules, now, expected):
    market = frame.market
    if market is None or market.round_id != expected:
        return None, ["当前市场缺失或轮次不匹配"]
    issues = []
    if not market.started_at <= frame.observed_at <= now < market.ends_at or now - frame.observed_at > 2.5:
        issues.append("行情快照超时或时间不匹配")
    if frame.error:
        issues.append("共享输入：" + str(frame.error)[:80])
    if not market.target_price or market.target_price <= 0:
        issues.append("本局结算基准缺失")
    if not frame.reference.get("ready") or not frame.reference.get("protocol_verified"):
        issues.append("参考行情未就绪或结算口径未校验")
    for channel, label in ((SPOT, "现货"), (TWAP, "结算均价")):
        if not _fresh_reference(frame.reference.get("latest", {}), channel, frame.observed_at):
            issues.append(label + "参考价或收发时间不合格")
    try:
        info = checked_info(info or {})
    except (KeyError, ValueError, TypeError):
        info = None
        issues.append("官方费率或价格精度不可用")
    for side in SIDES:
        if not _valid_book(frame, side, now, info, rules):
            detail = _book_reasons(frame, side, info, rules, now)
            issues.extend(detail or ([side + "盘口校验失败"] if info else []))
    matched = (decision is not None and decision.round_id == expected and decision.snapshot_id == frame.snapshot_id
               and decision.decision_at == frame.observed_at)
    if not matched:
        issues.append("方向预测与快照不匹配")
    elif decision.up_score is None or decision.projected_final_twap is None:
        issues.append("方向分数或预计结算价缺失：" + str(decision.reason)[:80])
    if issues:
        return None, issues
    sample = sample_frame(frame, decision, info)
    return (sample, []) if sample is not None else (None, ["候选输入字段未通过归一化校验"])


def _entry_gate(policy, sample, history, left):
    if not policy.window[0] <= left <= policy.window[1]:
        return "入场时窗外"
    if sample is None:
        return "时窗内但行情不合格"
    if policy.key == "rev_return" and not _window(sample, history, 12):
        return "形态缺少连续12秒历史"
    decision = entry_decision(policy, sample, history)
    if decision.action == "BUY":
        return "形态及成本条件均满足"
    return "形态未出现" if decision.reason == policy.trigger else decision.reason


class CandidateDiagnostics:
    """Each eligible second counts once. Original candidate orders are never edited."""

    def __init__(self, path, league_path=None, *, start_at=None):
        self.path = Path(path).resolve()
        self.league_path = Path(league_path).resolve() if league_path is not None else None
        if self.path == self.league_path:
            raise ValueError("诊断账本必须与候选交易账本分开")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO diagnostic_run VALUES(1,?,NULL)",
                            (time.time() if start_at is None else start_at,))
        self.start_at, self.last_at = self.db.execute("SELECT start_at,last_at FROM diagnostic_run").fetchone()
        self.history = deque(maxlen=90)
        self.history_id = None
        self.books = deque(maxlen=30)
        self.next_sync = 0
        self.ledger_status = "等待读取候选卖单"
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            self.db.close()

    def observe(self, frame, decision, info=None, rules=None, *, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if now < self.start_at or (self.last_at is not None and int(now) <= int(self.last_at)):
                return
            anchor = int(now // 300) * 300
            rid = f"btc-updown-5m-{anchor}"
            left = anchor + 300 - now
            gap = self.last_at is not None and now - self.last_at > 5
            if rid != self.history_id or gap:
                self.history.clear()
                self.history_id = rid
            sample, issues = _sample_and_issues(frame, decision, info, rules or {}, now, rid)
            counters = [("quality", "采样总数")]
            counters.extend(("quality", reason) for reason in issues or ["行情合格"])
            counters.extend((policy.key, _entry_gate(policy, sample, self.history, left)) for policy in WATCH)
            band = "tail" if left <= TAIL_SECONDS else "body"
            with self.db:
                if gap:
                    self.db.execute("INSERT INTO diagnostic_gaps VALUES(?,?)", (now, self.last_at))
                self.db.executemany("""INSERT INTO diagnostic_counts VALUES(?,?,?,?,1)
                    ON CONFLICT(round_id,band,subject,reason) DO UPDATE SET samples=samples+1""",
                    [(rid, band, subject, reason) for subject, reason in counters])
                self.db.execute("UPDATE diagnostic_run SET last_at=? WHERE id=1", (now,))
            self.last_at = now
            if sample and (not self.history or sample.at >= self.history[-1].at + 1):
                self.history.append(sample)
            if frame.market is not None and frame.market.round_id == rid and 0 <= now - frame.observed_at <= 2.5:
                self.books.append((now, frame))
            if self.league_path is not None and now >= self.next_sync:
                try:
                    self._orders()
                    self.ledger_status = "候选卖单已核对"
                    self.next_sync = now + 3
                except (OSError, sqlite3.Error) as exc:
                    self.ledger_status = f"候选卖单账本读取失败（{type(exc).__name__}）"
                    self.next_sync = now + 8

    def _nearby_book(self, order, at, arrival):
        if at is None:
            return None
        side = order["side"]
        for at_seen, frame in reversed(self.books):
            if at_seen > at:
                continue
            if at - at_seen > 2.5:
                break
            if frame.market.round_id != order["round_id"] or quote_error(frame.market, frame.quotes, side, at):
                continue
            q = frame.quotes[side]
            if q.get("token_id") != order["token"]:
                continue
            if arrival and (min(q["source_ms"], q["received_ms"]) < order["submitted_at"] * 1000
                            or q["source_ms"] <= order["source_ms"] or q["received_ms"] <= order["received_ms"]):
                continue
            try:
                levels = [(float(v["price"]), float(v["size"])) for v in q.get("bids", ())]
                if not levels or any(not (0 < p < 1 and math.isfinite(s) and s > 0) for p, s in levels):
                    continue
                if abs(max(p for p, _ in levels) - q["best_bid"]) > 1e-7:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            return {"snapshot": frame.snapshot_id, "sample_at": at_seen, "offset_ms": round((at - at_seen) * 1000),
                    "source_ms": q["source_ms"], "received_ms": q["received_ms"], "best_bid": q["best_bid"],
                    "displayed_shares_at_limit": round(sum(s for p, s in levels if p >= order["limit_price"] - 1e-9), 6)}
        return None

    def _orders(self):
        cursor = self.db.execute("SELECT coalesce(max(id),0) FROM diagnostic_sells").fetchone()[0]
        unresolved = [r[0] for r in self.db.execute("SELECT id FROM diagnostic_sells WHERE status='PENDING'")]
        query = "SELECT * FROM league_orders WHERE action='SELL' AND submitted_at>=? AND (id>?"
        params = [self.start_at, cursor]
        if unresolved:
            query += " OR id IN (" + ",".join("?" for _ in unresolved) + ")"
            params.extend(unresolved)
        query += ") ORDER BY id LIMIT 256"
        with closing(sqlite3.connect(self.league_path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as source:
            source.row_factory = sqlite3.Row
            orders = source.execute(query, params).fetchall()
        with self.db:
            for order in orders:
                evidence = {k: order[k] for k in (
                    "id", "strategy_id", "round_id", "side", "token", "reason", "status", "result_reason",
                    "submitted_at", "matched_at", "finished_at", "limit_price", "requested_shares", "filled_shares",
                    "source_ms", "received_ms", "arrival_source_ms", "arrival_received_ms", "decision_snapshot",
                    "arrival_snapshot", "cash", "fee")}
                evidence["fills"] = json.loads(order["fills"] or "[]")
                previous = self.db.execute("SELECT evidence FROM diagnostic_sells WHERE id=?", (order["id"],)).fetchone()
                old = json.loads(previous[0]) if previous else {}
                evidence["observer_at_submit"] = (old.get("observer_at_submit") or
                                                  self._nearby_book(order, order["submitted_at"], False))
                evidence["observer_at_finish"] = self._nearby_book(order, order["finished_at"], True)
                self.db.execute("""INSERT INTO diagnostic_sells VALUES(?,?,?)
                    ON CONFLICT(id) DO UPDATE SET status=excluded.status,evidence=excluded.evidence""",
                    (order["id"], order["status"], json.dumps(evidence, ensure_ascii=False)))

    def report(self):
        with self.lock:
            # The full per-round history stays on disk; dashboard reads cover at most 288 markets.
            since = max(self.start_at, ((self.last_at or self.start_at) // 300 - 287) * 300)
            first_round = f"btc-updown-5m-{int(since // 300) * 300}"
            counts = self.db.execute("""SELECT band,subject,reason,sum(samples) samples,count(*) rounds
                FROM diagnostic_counts WHERE round_id>=? GROUP BY band,subject,reason
                ORDER BY band,subject,samples DESC""", (first_round,)).fetchall()
            sales = self.db.execute("SELECT evidence FROM diagnostic_sells ORDER BY id DESC LIMIT 30").fetchall()
            gap = self.db.execute("SELECT count(*),max(observed_at-previous_at) FROM diagnostic_gaps").fetchone()
            return {"start_at": self.start_at, "last_at": self.last_at, "source_status": self.ledger_status,
                    "source": "行情为试验旁路独立采样，委托和成交来自原候选账本；旁路深度仅供对照。",
                    "tail_seconds": TAIL_SECONDS, "counts_since": since, "counts": [dict(r) for r in counts],
                    "observer_gaps": gap[0], "largest_observer_gap_seconds": gap[1],
                    "sell_count": self.db.execute("SELECT count(*) FROM diagnostic_sells").fetchone()[0],
                    "recent_sells": [json.loads(r["evidence"]) for r in sales]}

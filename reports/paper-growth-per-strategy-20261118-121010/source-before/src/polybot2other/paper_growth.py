"""Read-only, clock-bounded descriptions of Paper performance, never order gates."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import math
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
VERSION = "paper-growth-v1"
CACHE_SECONDS = 10
POLICY = {
    "minimum_3h_markets": 5,
    "minimum_1h_markets": 3,
    "minimum_declining_markets": 3,
    "acceleration_ratio": 1.25,
    "inactive_after_seconds": 7200,
    "basis": "realized_after_fee",
    "market_deduplication": "round_id_across_accounts",
    "controls_orders": False,
}
LABELS = {
    "WARMING": ("升温", "blue"),
    "STRONG": ("强势延续", "green"),
    "PULLBACK": ("回撤待确认", "amber"),
    "WEAKENING": ("持续转弱", "red"),
    "OBSERVING": ("观察中", "neutral"),
    "INSUFFICIENT": ("样本积累中", "neutral"),
    "INACTIVE": ("近期暂无结算", "neutral"),
    "UNAVAILABLE": ("数据暂不可用", "neutral"),
}


def _finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def windows_at(now):
    day = datetime.fromtimestamp(now, TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    reference = day if now >= day + 21600 else day - 86400
    return {
        "today": (day, now),
        "last1h": (now - 3600, now),
        "last3h": (now - 10800, now),
        "last6h": (now - 21600, now),
        "previous1h": (now - 7200, now - 3600),
        "night": (reference, reference + 21600),
    }


def _window(rows, start, end, created_at):
    selected = [r for r in rows if start <= r["settled_at"] < end]
    by_market = defaultdict(float)
    for r in selected:
        by_market[r["round_id"]] += r["pnl"]
    pnl = sum(r["pnl"] for r in selected)
    cost = sum(r["stake"] for r in selected)
    n = len(selected)
    hours = (end - start) / 3600
    coverage = max(0., end - max(start, created_at))
    return {
        "start_at": start, "end_at": end,
        "pnl": round(pnl, 6), "cost": round(cost, 6),
        "pnl_per_hour": round(pnl / hours, 6) if hours > 0 else None,
        "cost_return_pct": round(pnl / cost * 100, 4) if cost > 0 else None,
        "trades": n, "wins": sum(r["pnl"] > 0 for r in selected),
        "win_rate": round(sum(r["pnl"] > 0 for r in selected) / n * 100, 2) if n else None,
        "markets": len(by_market),
        "winning_markets": sum(v > 0 for v in by_market.values()),
        "losing_markets": sum(v < 0 for v in by_market.values()),
        "coverage_hours": round(coverage / 3600, 3),
        "complete_history": created_at <= start,
    }


def _day_curve(rows, start, now):
    # Batch the same market so asynchronous account settlements do not invent a peak.
    groups = {}
    for r in rows:
        if not start <= r["settled_at"] < now:
            continue
        g = groups.setdefault(r["round_id"], {"pnl": 0., "at": r["settled_at"]})
        g["pnl"] += r["pnl"]
        g["at"] = max(g["at"], r["settled_at"])
    cumulative = peak = drawdown = 0.
    peak_at = start
    for g in sorted(groups.values(), key=lambda g: g["at"]):
        cumulative += g["pnl"]
        if cumulative > peak:
            peak, peak_at = cumulative, g["at"]
        drawdown = max(drawdown, peak - cumulative)
    return {"peak_pnl": round(peak, 6), "peak_at": peak_at,
            "giveback": round(max(0., peak - cumulative), 6),
            "giveback_pct_of_peak_profit": round((peak - cumulative) / peak * 100, 2) if peak > 0 else None,
            "max_drawdown": round(drawdown, 6)}


def classify(windows, *, latest_settlement, now):
    one, three, six, previous = (windows[k] for k in ("last1h", "last3h", "last6h", "previous1h"))
    if latest_settlement is None or now - latest_settlement >= POLICY["inactive_after_seconds"]:
        code = "INACTIVE"
        reason = "近两小时没有新增结算，等待新的交易结果，不把曲线走平视为转弱。"
    elif three["markets"] < POLICY["minimum_3h_markets"] or not three["complete_history"]:
        code = "INSUFFICIENT"
        reason = f"近三小时覆盖 {three['markets']} 个不同市场，先积累完整时段和更多独立结果。"
    elif (one["pnl"] < 0 and three["pnl"] < 0 and previous["pnl"] < 0
          and one["markets"] >= POLICY["minimum_1h_markets"]
          and three["losing_markets"] >= POLICY["minimum_declining_markets"]):
        code = "WEAKENING"
        reason = "相邻两小时均亏损，近三小时也转亏，且亏损分布在多个不同市场。"
    elif one["pnl"] < 0:
        code = "PULLBACK"
        reason = ("近一小时回撤，较长窗口仍在盈利，等待更多市场确认。"
                  if three["pnl"] > 0 or six["pnl"] > 0
                  else "近期出现回撤，持续性证据仍不足，暂不确认持续转弱。")
    elif one["pnl"] > 0 and one["markets"] >= POLICY["minimum_1h_markets"]:
        acceleration = (
            previous["markets"] >= POLICY["minimum_1h_markets"]
            and (previous["pnl"] <= 0 or (
                one["pnl_per_hour"] > previous["pnl_per_hour"] * POLICY["acceleration_ratio"]
                and one["cost_return_pct"] is not None
                and previous["cost_return_pct"] is not None
                and one["cost_return_pct"] > previous["cost_return_pct"] * POLICY["acceleration_ratio"]
            ))
        )
        if three["pnl"] <= 0 or acceleration:
            code = "WARMING"
            reason = "近一小时转好，或收益速度与单位成本收益同步提高，继续观察延续性。"
        elif three["pnl"] > 0 and six["pnl"] > 0 and six["complete_history"]:
            code = "STRONG"
            reason = "近一、三、六小时均盈利，当前仍延续正增长。"
        else:
            code = "OBSERVING"
            reason = "近期有盈利，较长窗口尚未形成一致表现。"
    else:
        code = "OBSERVING"
        reason = "最近一小时结算较少或方向不一致，等待更多市场结果。"
    label, tone = LABELS[code]
    return {"code": code, "label": label, "tone": tone, "reason": reason}


def build_growth_snapshot(accounts, rows, *, now, latest_settlement=None, open_positions=None):
    """Pure calculations; input rows contain only confirmed Paper settlements."""
    bounds = windows_at(now)
    rows = [r for r in rows if r["settled_at"] < now]
    created_at = max(a["created_at"] for a in accounts)
    windows = {key: _window(rows, start, end, created_at) for key, (start, end) in bounds.items()}
    latest = latest_settlement if latest_settlement is not None else max((r["settled_at"] for r in rows), default=None)
    state = classify(windows, latest_settlement=latest, now=now)
    one_rows = [r for r in rows if bounds["last1h"][0] <= r["settled_at"] < now]
    losing = defaultdict(list)
    for r in one_rows:
        losing[r["round_id"]].append(r)
    losses = []
    for round_id, group in losing.items():
        pnl = sum(r["pnl"] for r in group)
        if pnl < 0:
            losses.append({"round_id": round_id, "pnl": round(pnl, 6),
                           "accounts": len({r["strategy_id"] for r in group})})
    losses.sort(key=lambda r: r["pnl"])
    reasons = [state["reason"],
               f"近一小时 {windows['last1h']['trades']} 笔结算来自 {windows['last1h']['markets']} 个不同市场；同一市场跨账户只计一次。"]
    if losses:
        largest = losses[0]
        reasons.append(f"近一小时最大单市场净亏 ${abs(largest['pnl']):.2f}，涉及 {largest['accounts']} 个账户。")
    curve = _day_curve(rows, bounds["today"][0], now)
    per_account = []
    for account in accounts:
        group = [r for r in rows if r["strategy_id"] == account["strategy_id"]]
        parts = {key: _window(group, start, end, account["created_at"]) for key, (start, end) in bounds.items()}
        per_account.append({"strategy_id": account["strategy_id"], "name": account["name"], "windows": parts})
    positions = open_positions or []
    return {
        "available": True, "version": VERSION, "scope": "paper_all", "timezone": "Asia/Shanghai",
        "as_of": now, "refresh_seconds": 15, "account_count": len(accounts),
        "state": state, "reasons": reasons, "windows": windows, "today_curve": curve,
        "day_label": "今日盈利" if windows["today"]["pnl"] > 0 else "今日亏损" if windows["today"]["pnl"] < 0 else "今日暂无净收益",
        "night_label": datetime.fromtimestamp(bounds["night"][0], TIMEZONE).strftime("%m-%d") + " 凌晨 00–06 点",
        "latest_settlement_at": latest, "largest_recent_market_loss": losses[0] if losses else None,
        "open_positions": len(positions), "open_cost": round(sum(r["stake"] for r in positions), 6),
        "unresolved_markets": len({r["round_id"] for r in positions}),
        "accounts": per_account, "policy": dict(POLICY),
    }


class PaperGrowthMonitor:
    def __init__(self, bot, *, clock=None):
        self.bot = bot
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._cache = None
        self._cache_at = 0.

    def _sources(self):
        sources = []
        store = getattr(self.bot, "store", None)
        if store is not None:
            sources.append(("MAIN", "主账户", Path(store.db_path)))
        runner = getattr(self.bot, "strategy_experiments", None)
        if runner is not None:
            with runner._lock:
                variants = tuple(runner.variants)
                bots = dict(runner._bots)
            for variant in variants:
                bot = bots.get(variant.variant_id)
                if bot is not None:
                    sources.append((variant.variant_id, variant.combo, Path(bot.store.db_path)))
        seen, unique = set(), []
        for key, name, path in sources:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append((key, name, resolved))
        return unique

    def snapshot(self):
        with self._lock:
            now = self.clock()
            if self._cache is not None and 0 <= now - self._cache_at < CACHE_SECONDS:
                return self._cache
            accounts, rows, positions, errors, latest = [], [], [], [], None
            start = min(a for a, _ in windows_at(now).values())
            sources = self._sources()
            for key, name, path in sources:
                try:
                    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)
                    conn.row_factory = sqlite3.Row
                    try:
                        conn.execute("PRAGMA query_only=ON")
                        conn.execute("BEGIN")
                        account = conn.execute("SELECT created_at FROM account WHERE id=1").fetchone()
                        if account is None or not _finite(account["created_at"]):
                            raise ValueError("invalid account")
                        trades = conn.execute("""SELECT round_id,stake,pnl,settled_at FROM trades
                            WHERE status='SETTLED' AND settled_at>=? AND settled_at<?
                            ORDER BY settled_at,id""", (start, now)).fetchall()
                        opened = conn.execute("SELECT round_id,stake FROM trades WHERE status='OPEN' AND opened_at<?", (now,)).fetchall()
                        last = conn.execute("SELECT max(settled_at) FROM trades WHERE status='SETTLED' AND settled_at<?", (now,)).fetchone()[0]
                        if any(not r["round_id"] or any(not _finite(r[k]) for k in ("stake", "pnl", "settled_at")) or r["stake"] <= 0 for r in trades):
                            raise ValueError("invalid settlement")
                        if any(not r["round_id"] or not _finite(r["stake"]) or r["stake"] < 0 for r in opened):
                            raise ValueError("invalid open position")
                        accounts.append({"strategy_id": key, "name": name, "created_at": account["created_at"]})
                        rows.extend({**dict(r), "strategy_id": key} for r in trades)
                        positions.extend({**dict(r), "strategy_id": key} for r in opened)
                        if last is not None:
                            latest = max(latest or last, last)
                    finally:
                        conn.close()
                except (sqlite3.Error, OSError, ValueError):
                    errors.append(key)
            if errors or not accounts:
                label, tone = LABELS["UNAVAILABLE"]
                payload = {"available": False, "version": VERSION, "as_of": now, "scope": "paper_all",
                           "state": {"code": "UNAVAILABLE", "label": label, "tone": tone},
                           "error": "Paper 账本读取不完整，等待重新读取。", "unavailable_accounts": errors,
                           "refresh_seconds": 15}
            else:
                payload = build_growth_snapshot(accounts, rows, now=now, latest_settlement=latest, open_positions=positions)
            self._cache, self._cache_at = payload, now
            return payload

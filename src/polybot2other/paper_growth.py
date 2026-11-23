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

from .exit_settle_paper import ExitSettlePaper, NAME as EXIT_SETTLE_NAME, VARIANT_ID as EXIT_SETTLE_ID


TIMEZONE = ZoneInfo("Asia/Shanghai")
VERSION = "paper-growth-v2"
CACHE_SECONDS = 10
POLICY = {
    "minimum_3h_markets": 5,
    "minimum_1h_markets": 3,
    "minimum_declining_markets": 3,
    "acceleration_ratio": 1.25,
    "inactive_after_seconds": 7200,
    "basis": "realized_after_fee",
    "market_deduplication": "round_id_within_strategy",
    "controls_orders": False,
}
LABELS = {
    "RECOVERING": ("最近开始盈利", "blue"),
    "WARMING": ("最近赚得更快了", "blue"),
    "STRONG": ("最近持续盈利", "green"),
    "PULLBACK": ("最近一小时亏损", "amber"),
    "WEAKENING": ("最近持续亏损", "red"),
    "OBSERVING": ("还看不出明显趋势", "neutral"),
    "INSUFFICIENT": ("交易太少，暂不好判断", "neutral"),
    "INACTIVE": ("近两小时没有新结算", "neutral"),
    "UNAVAILABLE": ("暂时读不到数据", "neutral"),
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
    # Batch multiple orders in the same market before measuring this strategy's peak.
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
        reason = "这个策略近两小时没有新的结算结果，等下一笔交易结束后再看。"
    elif three["markets"] < POLICY["minimum_3h_markets"] or not three["complete_history"]:
        code = "INSUFFICIENT"
        reason = (f"这个策略近三小时只有 {three['markets']} 个不同市场的结果，至少需要 5 个才判断。"
                  if three["complete_history"] else "这个策略的数据还不足三小时，先等更多交易结果。")
    elif (one["pnl"] < 0 and three["pnl"] < 0 and previous["pnl"] < 0
          and one["markets"] >= POLICY["minimum_1h_markets"]
          and three["losing_markets"] >= POLICY["minimum_declining_markets"]):
        code = "WEAKENING"
        reason = "最近一小时和再前一小时都亏钱，近三小时合计也亏钱，而且已在至少三个不同市场亏损。"
    elif one["pnl"] < 0:
        code = "PULLBACK"
        reason = ("最近一小时亏钱，但近三小时合计仍赚钱，再看后面几笔是否继续亏损。"
                  if three["pnl"] > 0 else "最近一小时亏钱，但近六小时合计仍赚钱，再看后面几笔是否继续亏损。"
                  if six["pnl"] > 0 else "最近一小时亏钱，但交易结果还不够多，暂时不能判断亏损是否持续。")
    elif one["pnl"] > 0 and one["markets"] >= POLICY["minimum_1h_markets"]:
        acceleration = (
            previous["markets"] >= POLICY["minimum_1h_markets"]
            and previous["pnl"] > 0
            and (
                one["pnl_per_hour"] > previous["pnl_per_hour"] * POLICY["acceleration_ratio"]
                and one["cost_return_pct"] is not None
                and previous["cost_return_pct"] is not None
                and one["cost_return_pct"] > previous["cost_return_pct"] * POLICY["acceleration_ratio"]
            )
        )
        if three["pnl"] <= 0 or (previous["pnl"] <= 0 and previous["markets"] >= POLICY["minimum_1h_markets"]):
            code = "RECOVERING"
            reason = "最近一小时开始赚钱了，不过前面的表现还没完全好转，继续看后面几笔。"
        elif acceleration:
            code = "WARMING"
            reason = "最近一小时比再前一小时赚得更多，每投入一美元赚到的比例也更高。"
        elif three["pnl"] > 0 and six["pnl"] > 0 and six["complete_history"]:
            code = "STRONG"
            reason = "最近一小时、三小时和六小时合计都在赚钱。"
        else:
            code = "OBSERVING"
            reason = "最近一小时赚钱，但更长时间里的表现还不一致，继续观察。"
    else:
        code = "OBSERVING"
        reason = "最近一小时结束的交易较少，或赚亏接近，暂时看不出明显趋势。"
    label, tone = LABELS[code]
    return {"code": code, "label": label, "tone": tone, "reason": reason}


def build_strategy_snapshot(account, rows, *, now, latest_settlement=None, open_positions=None):
    """Describe exactly one strategy; other strategies cannot affect its result."""
    strategy_id = account["strategy_id"]
    bounds = windows_at(now)
    rows = [r for r in rows if r["strategy_id"] == strategy_id and r["settled_at"] < now]
    created_at = account["created_at"]
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
            losses.append({"round_id": round_id, "pnl": round(pnl, 6), "trades": len(group)})
    losses.sort(key=lambda r: r["pnl"])
    reasons = [state["reason"],
               f"这个策略近一小时结束了 {windows['last1h']['trades']} 笔交易，来自 {windows['last1h']['markets']} 个不同市场；同一个市场重复开仓只算一个结果。"]
    if losses:
        largest = losses[0]
        reasons.append(f"这个策略近一小时亏得最多的一个市场，合计亏了 ${abs(largest['pnl']):.2f}（{largest['trades']} 笔交易）。")
    curve = _day_curve(rows, bounds["today"][0], now)
    positions = [r for r in open_positions or [] if r["strategy_id"] == strategy_id]
    return {
        "available": True, "strategy_id": strategy_id, "name": account["name"], "as_of": now,
        "state": state, "reasons": reasons, "windows": windows, "today_curve": curve,
        "day_label": "今日盈利" if windows["today"]["pnl"] > 0 else "今日亏损" if windows["today"]["pnl"] < 0 else "今日暂无净收益",
        "night_label": datetime.fromtimestamp(bounds["night"][0], TIMEZONE).strftime("%m-%d") + " 凌晨 00–06 点",
        "latest_settlement_at": latest, "largest_recent_market_loss": losses[0] if losses else None,
        "open_positions": len(positions), "open_cost": round(sum(r["stake"] for r in positions), 6),
        "unresolved_markets": len({r["round_id"] for r in positions}),
    }


def _envelope(strategies, now):
    return {
        "available": any(s["available"] for s in strategies),
        "complete": bool(strategies) and all(s["available"] for s in strategies),
        "version": VERSION, "scope": "paper_by_strategy", "timezone": "Asia/Shanghai",
        "as_of": now, "refresh_seconds": 15, "strategy_count": len(strategies),
        "strategies": strategies, "policy": dict(POLICY),
    }


def build_growth_snapshot(accounts, rows, *, now, latest_settlements=None, open_positions=None):
    """Return independent results, with no combined profit or combined state."""
    latest_settlements = latest_settlements or {}
    return _envelope([
        build_strategy_snapshot(account, rows, now=now,
                                latest_settlement=latest_settlements.get(account["strategy_id"]),
                                open_positions=open_positions)
        for account in accounts
    ], now)


def _unavailable_strategy(key, name, now):
    label, tone = LABELS["UNAVAILABLE"]
    return {"strategy_id": key, "name": name, "available": False, "as_of": now,
            "state": {"code": "UNAVAILABLE", "label": label, "tone": tone,
                      "reason": "这个策略的账本暂时读不到，稍后会自动重试。"}}


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
                path = Path(bot.store.db_path) if bot is not None else None
                sources.append((variant.variant_id, variant.combo, path))
        seen, unique = set(), []
        for key, name, path in sources:
            resolved = path.resolve() if path is not None else None
            if resolved is None or resolved not in seen:
                seen.add(resolved)
                unique.append((key, name, resolved))
        return unique

    def snapshot(self):
        with self._lock:
            now = self.clock()
            if self._cache is not None and 0 <= now - self._cache_at < CACHE_SECONDS:
                return self._cache
            strategies = []
            start = min(a for a, _ in windows_at(now).values())
            sources = self._sources()
            for key, name, path in sources:
                try:
                    if path is None:
                        raise ValueError("strategy not ready")
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
                        if last is not None and not _finite(last):
                            raise ValueError("invalid latest settlement")
                        if any(not r["round_id"] or any(not _finite(r[k]) for k in ("stake", "pnl", "settled_at")) or r["stake"] <= 0 for r in trades):
                            raise ValueError("invalid settlement")
                        if any(not r["round_id"] or not _finite(r["stake"]) or r["stake"] < 0 for r in opened):
                            raise ValueError("invalid open position")
                        strategies.append(build_strategy_snapshot(
                            {"strategy_id": key, "name": name, "created_at": account["created_at"]},
                            [{**dict(r), "strategy_id": key} for r in trades],
                            now=now, latest_settlement=last,
                            open_positions=[{**dict(r), "strategy_id": key} for r in opened],
                        ))
                    finally:
                        conn.close()
                except (sqlite3.Error, OSError, ValueError):
                    strategies.append(_unavailable_strategy(key, name, now))
            if getattr(getattr(self.bot, "settings", None), "strategy_league_enabled", False):
                try:
                    inputs = ExitSettlePaper(self.bot).growth_inputs(start, now)
                    strategies.append(build_strategy_snapshot(now=now, **inputs))
                except (sqlite3.Error, OSError, ValueError):
                    strategies.append(_unavailable_strategy(EXIT_SETTLE_ID, EXIT_SETTLE_NAME, now))
            payload = _envelope(strategies, now)
            self._cache, self._cache_at = payload, now
            return payload

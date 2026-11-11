from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any


STRATEGY_MEMORY_SCHEMA_VERSION = 1
AGGRESSIVE_EDGE_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE"
AGGRESSIVE_EDGE_V1_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_V1"
AGGRESSIVE_EDGE_REAL_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_REAL"
AGGRESSIVE_EDGE_COMBO = "SINGLE + FAK Aggressive Edge"
AGGRESSIVE_EDGE_V1_COMBO = "SINGLE + FAK Aggressive Edge V1"
DEFAULT_AGGRESSIVE_EDGE_DB_PATH = Path("data/strategy-experiments/single_fak_aggressive_edge.sqlite3")
DEFAULT_AGGRESSIVE_EDGE_MEMORY_PATH = Path("data/strategy-memory/single_fak_aggressive_edge.memory.jsonl")

BRANCH_PATTERN = re.compile(r"PASS ([a-zA-Z0-9_]+):")
SWEET_BRANCH = "sweet_move_6_8bps"


def build_aggressive_edge_memory_entry(
    db_path: Path = DEFAULT_AGGRESSIVE_EDGE_DB_PATH,
    *,
    memory_path: Path | None = DEFAULT_AGGRESSIVE_EDGE_MEMORY_PATH,
    created_at: float | None = None,
) -> dict[str, Any]:
    """从当前实验库生成一条 Aggressive Edge 策略经验记忆。"""

    created_at = time.time() if created_at is None else float(created_at)
    trades = _load_aggressive_edge_trades(db_path)
    settled = [trade for trade in trades if trade["status"] == "SETTLED"]
    losses = [trade for trade in settled if (_float_or_none(trade.get("pnl")) or 0.0) < 0.0]
    wins = [trade for trade in settled if (_float_or_none(trade.get("pnl")) or 0.0) > 0.0]
    branch_summary = _branch_summary(settled)
    false_breakout_rule = _false_breakout_rule_analysis(settled)
    hard_gate_rejection = _hard_gate_rejection_analysis(settled)
    paper_v2_rule = _paper_v2_rule_analysis(settled)
    unresolved_losses = [
        _compact_loss_trade(trade)
        for trade in losses
        if trade.get("branch") == SWEET_BRANCH and not _is_false_breakout_risk(trade)
    ]
    memory_count = len(load_strategy_memory(memory_path)) if memory_path else 0

    entry: dict[str, Any] = {
        "schema_version": STRATEGY_MEMORY_SCHEMA_VERSION,
        "strategy_id": AGGRESSIVE_EDGE_VARIANT_ID,
        "learning_target_strategy_id": AGGRESSIVE_EDGE_V1_VARIANT_ID,
        "real_strategy_id": AGGRESSIVE_EDGE_REAL_VARIANT_ID,
        "combo": AGGRESSIVE_EDGE_COMBO,
        "learning_target_combo": AGGRESSIVE_EDGE_V1_COMBO,
        "created_at": round(created_at, 6),
        "memory_sequence": memory_count + 1,
        "sample_window": _sample_window(trades, settled, wins, losses),
        "branch_summary": branch_summary,
        "loss_trades": [_compact_loss_trade(trade) for trade in losses],
        "confirmed_patterns": _confirmed_patterns(branch_summary, false_breakout_rule, paper_v2_rule),
        "suspected_patterns": _suspected_patterns(unresolved_losses),
        "rejected_hypotheses": _rejected_hypotheses(hard_gate_rejection),
        "parameter_recommendations": _parameter_recommendations(
            false_breakout_rule,
            hard_gate_rejection,
            paper_v2_rule,
        ),
        "risk_tags": _risk_tags(false_breakout_rule, unresolved_losses, paper_v2_rule),
        "evidence": {
            "false_breakout_rule": false_breakout_rule,
            "hard_gate_rejection": hard_gate_rejection,
            "paper_v2_guard": paper_v2_rule,
            "unresolved_losses": unresolved_losses,
        },
        "confidence_level": _confidence_level(len(settled), len(losses), false_breakout_rule),
        "notes": [
            "该记忆记录策略经验和被证伪假设，不直接修改下注逻辑。",
            "后续复盘应先读取本文件，再用最新 SQLite 样本验证规律是否继续成立。",
        ],
    }
    entry["entry_id"] = _entry_id(entry)
    return entry


def append_strategy_memory_entry(path: Path, entry: dict[str, Any], *, dedupe: bool = True) -> dict[str, Any]:
    """追加策略记忆；默认用 entry_id 防止同一份样本重复写入。"""

    existing = load_strategy_memory(path)
    if dedupe:
        entry_id = str(entry.get("entry_id") or "")
        for item in existing:
            if entry_id and str(item.get("entry_id") or "") == entry_id:
                return {"appended": False, "reason": "duplicate_entry_id", "path": str(path), "entry": item}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        handle.write("\n")
    return {"appended": True, "reason": "created", "path": str(path), "entry": entry}


def load_strategy_memory(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries


def _load_aggressive_edge_trades(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        raise FileNotFoundError(f"strategy db not found: {db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
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
                t.payout,
                t.pnl,
                t.settlement_source,
                t.reason,
                r.started_at,
                r.ends_at,
                r.target_price,
                r.final_price,
                r.outcome,
                r.settlement_source AS market_settlement_source
            FROM trades t
            JOIN market_rounds r ON r.round_id = t.round_id
            ORDER BY t.id ASC
            """
        ).fetchall()
        trades: list[dict[str, Any]] = []
        for row in rows:
            trade = dict(row)
            trade["branch"] = _branch_from_reason(str(trade.get("reason") or ""))
            trade["edge"] = _round_float((_float_or_none(trade.get("confidence")) or 0.0) - (_float_or_none(trade.get("entry_price")) or 0.0), 6)
            trade["abs_bps"] = _round_float(abs(_float_or_none(trade.get("move_bps")) or 0.0), 6)
            trade["time_left_at_open"] = _round_float(
                (_float_or_none(trade.get("ends_at")) or 0.0) - (_float_or_none(trade.get("opened_at")) or 0.0),
                6,
            )
            target_price = _float_or_none(trade.get("target_price"))
            final_price = _float_or_none(trade.get("final_price"))
            move_bps = _float_or_none(trade.get("move_bps"))
            final_bps = _distance_bps(final_price, target_price)
            trade["final_bps"] = final_bps
            trade["drift_bps"] = _round_float(final_bps - move_bps, 6) if final_bps is not None and move_bps is not None else None
            trade["trajectory"] = _trade_trajectory(con, trade)
            trades.append(trade)
        return trades
    finally:
        con.close()


def _trade_trajectory(con: sqlite3.Connection, trade: dict[str, Any]) -> dict[str, Any]:
    # 只从已落盘的 Chainlink tick 还原轨迹；缺 tick 时保留 None，避免伪造信号。
    started_at = _float_or_none(trade.get("started_at"))
    ends_at = _float_or_none(trade.get("ends_at"))
    opened_at = _float_or_none(trade.get("opened_at"))
    target_price = _float_or_none(trade.get("target_price"))
    if started_at is None or ends_at is None or opened_at is None or target_price is None or target_price <= 0:
        return {}
    ticks = con.execute(
        """
        SELECT price, source, created_at
        FROM price_ticks
        WHERE symbol = ?
          AND created_at >= ?
          AND created_at <= ?
        ORDER BY created_at ASC
        """,
        (str(trade.get("symbol") or "BTC"), started_at, ends_at + 10.0),
    ).fetchall()
    if not ticks:
        return {}

    def closest(ts: float) -> dict[str, Any] | None:
        row = min(ticks, key=lambda item: abs(float(item["created_at"]) - ts))
        price = _float_or_none(row["price"])
        if price is None:
            return None
        return {
            "price": _round_float(price, 8),
            "source": str(row["source"] or ""),
            "at": _round_float(_float_or_none(row["created_at"]), 6),
            "dt": _round_float((_float_or_none(row["created_at"]) or 0.0) - ts, 3),
            "bps": _distance_bps(price, target_price),
        }

    samples = {
        "start": closest(started_at),
        "before60": closest(opened_at - 60.0),
        "before30": closest(opened_at - 30.0),
        "open": closest(opened_at),
        "plus30": closest(opened_at + 30.0),
        "plus60": closest(opened_at + 60.0),
        "plus90": closest(opened_at + 90.0),
        "plus120": closest(opened_at + 120.0),
        "end": closest(ends_at),
    }
    bps_values = [
        value
        for value in (_distance_bps(_float_or_none(row["price"]), target_price) for row in ticks)
        if value is not None
    ]
    open_bps = _nested_float(samples.get("open"), "bps")
    before60_bps = _nested_float(samples.get("before60"), "bps")
    before30_bps = _nested_float(samples.get("before30"), "bps")
    start_bps = _nested_float(samples.get("start"), "bps")
    samples["tick_count"] = len(ticks)
    samples["min_bps"] = _round_float(min(bps_values), 6) if bps_values else None
    samples["max_bps"] = _round_float(max(bps_values), 6) if bps_values else None
    samples["pre_open_jump_from_start"] = _round_float(open_bps - start_bps, 6) if open_bps is not None and start_bps is not None else None
    samples["pre_open_jump_60s"] = _round_float(open_bps - before60_bps, 6) if open_bps is not None and before60_bps is not None else None
    samples["pre_open_jump_30s"] = _round_float(open_bps - before30_bps, 6) if open_bps is not None and before30_bps is not None else None
    return samples


def _sample_window(trades: list[dict[str, Any]], settled: list[dict[str, Any]], wins: list[dict[str, Any]], losses: list[dict[str, Any]]) -> dict[str, Any]:
    total_stake = sum(_float_or_none(trade.get("stake")) or 0.0 for trade in settled)
    total_pnl = sum(_float_or_none(trade.get("pnl")) or 0.0 for trade in settled)
    return {
        "total_trades": len(trades),
        "settled_trades": len(settled),
        "open_trades": len([trade for trade in trades if trade.get("status") == "OPEN"]),
        "win_count": len(wins),
        "loss_count": len(losses),
        "total_stake": _round_float(total_stake, 6),
        "total_pnl": _round_float(total_pnl, 6),
        "roi_pct": _round_float(total_pnl / total_stake * 100.0, 4) if total_stake else None,
        "win_rate": _round_float(len(wins) / len(settled) * 100.0, 4) if settled else None,
        "first_trade_id": int(trades[0]["id"]) if trades else None,
        "last_trade_id": int(trades[-1]["id"]) if trades else None,
        "first_opened_at": _round_float(min((_float_or_none(t.get("opened_at")) or 0.0 for t in trades), default=0.0), 6)
        if trades
        else None,
        "last_settled_at": _round_float(max((_float_or_none(t.get("settled_at")) or 0.0 for t in settled), default=0.0), 6)
        if settled
        else None,
    }


def _branch_summary(settled: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for branch in sorted({str(trade.get("branch") or "UNKNOWN") for trade in settled}):
        rows = [trade for trade in settled if str(trade.get("branch") or "UNKNOWN") == branch]
        wins = [trade for trade in rows if (_float_or_none(trade.get("pnl")) or 0.0) > 0.0]
        losses = [trade for trade in rows if (_float_or_none(trade.get("pnl")) or 0.0) < 0.0]
        stake = sum(_float_or_none(trade.get("stake")) or 0.0 for trade in rows)
        pnl = sum(_float_or_none(trade.get("pnl")) or 0.0 for trade in rows)
        summary[branch] = {
            "settled": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "pnl": _round_float(pnl, 6),
            "roi_pct": _round_float(pnl / stake * 100.0, 4) if stake else None,
            "win_rate": _round_float(len(wins) / len(rows) * 100.0, 4) if rows else None,
            "loss_trade_ids": [int(trade["id"]) for trade in losses],
        }
    return summary


def _false_breakout_rule_analysis(settled: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [trade for trade in settled if _is_false_breakout_risk(trade)]
    wins = [trade for trade in candidates if (_float_or_none(trade.get("pnl")) or 0.0) > 0.0]
    losses = [trade for trade in candidates if (_float_or_none(trade.get("pnl")) or 0.0) < 0.0]
    avoided_loss = -sum(min(0.0, _float_or_none(trade.get("pnl")) or 0.0) for trade in losses)
    missed_win = sum(max(0.0, _float_or_none(trade.get("pnl")) or 0.0) for trade in wins)
    return {
        "name": "sweet_up_false_breakout_risk",
        "description": (
            "Up + sweet_move_6_8bps + entry>=0.60 + 剩余时间>=210s + "
            "开仓前60秒bps<=1 + 60秒跳升>=6bps"
        ),
        "candidate_trade_ids": [int(trade["id"]) for trade in candidates],
        "matched_wins": len(wins),
        "matched_losses": len(losses),
        "matched_win_ids": [int(trade["id"]) for trade in wins],
        "matched_loss_ids": [int(trade["id"]) for trade in losses],
        "estimated_avoided_loss": _round_float(avoided_loss, 6),
        "estimated_missed_win": _round_float(missed_win, 6),
        "net_effect_if_blocked": _round_float(avoided_loss - missed_win, 6),
        "status": "candidate_watch_rule",
    }


def _hard_gate_rejection_analysis(settled: list[dict[str, Any]]) -> dict[str, Any]:
    # 上一轮人工反思提出过 confidence>=0.82 / edge>=0.20 硬门槛，这里专门做反证检查。
    rows = [
        trade
        for trade in settled
        if trade.get("branch") == SWEET_BRANCH
        and str(trade.get("side") or "") == "Up"
        and (_float_or_none(trade.get("entry_price")) or 0.0) >= 0.60
        and (
            (_float_or_none(trade.get("confidence")) or 0.0) < 0.82
            or (_float_or_none(trade.get("edge")) or 0.0) < 0.20
        )
    ]
    wins = [trade for trade in rows if (_float_or_none(trade.get("pnl")) or 0.0) > 0.0]
    losses = [trade for trade in rows if (_float_or_none(trade.get("pnl")) or 0.0) < 0.0]
    missed_win = sum(_float_or_none(trade.get("pnl")) or 0.0 for trade in wins)
    avoided_loss = -sum(min(0.0, _float_or_none(trade.get("pnl")) or 0.0) for trade in losses)
    return {
        "name": "hard_confidence_0_82_edge_0_20_gate",
        "blocked_trade_ids": [int(trade["id"]) for trade in rows],
        "blocked_win_ids": [int(trade["id"]) for trade in wins],
        "blocked_loss_ids": [int(trade["id"]) for trade in losses],
        "estimated_missed_win": _round_float(missed_win, 6),
        "estimated_avoided_loss": _round_float(avoided_loss, 6),
        "net_effect_if_blocked": _round_float(avoided_loss - missed_win, 6),
        "status": "rejected_overfit_gate" if wins else "needs_more_data",
    }


def _paper_v2_rule_analysis(settled: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = [trade for trade in settled if _is_paper_v2_block_candidate(trade)]
    kept = [trade for trade in settled if not _is_paper_v2_block_candidate(trade)]
    blocked_stats = _trade_stats(blocked)
    kept_stats = _trade_stats(kept)
    status = (
        "recommend_apply_paper_guard"
        if blocked_stats["pnl"] < 0.0 and (kept_stats["roi_pct"] or 0.0) > 0.0
        else "watch_only"
    )
    return {
        "name": "paper_v2_negative_expectancy_guard",
        "description": (
            "Paper 只保留 low_entry_high_edge；high_confidence_high_entry 需要 edge>=0.10 且 abs_bps<12；"
            "过滤 sweet_move_6_8bps + Up"
        ),
        "status": status,
        "blocked_trade_ids": [int(trade["id"]) for trade in blocked],
        "kept_trade_ids": [int(trade["id"]) for trade in kept],
        "blocked": blocked_stats,
        "kept": kept_stats,
    }


def _is_paper_v2_block_candidate(trade: dict[str, Any]) -> bool:
    branch = str(trade.get("branch") or "")
    side = str(trade.get("side") or "")
    entry_price = _float_or_none(trade.get("entry_price")) or 0.0
    edge = _float_or_none(trade.get("edge")) or 0.0
    abs_bps = _float_or_none(trade.get("abs_bps")) or abs(_float_or_none(trade.get("move_bps")) or 0.0)
    if branch == SWEET_BRANCH and side == "Up":
        return True
    return branch == "high_confidence_high_entry" and entry_price >= 0.70 and (edge < 0.10 or abs_bps >= 12.0)


def _is_false_breakout_risk(trade: dict[str, Any]) -> bool:
    trajectory = trade.get("trajectory") if isinstance(trade.get("trajectory"), dict) else {}
    before60_bps = _nested_float(trajectory.get("before60"), "bps")
    jump_60s = _float_or_none(trajectory.get("pre_open_jump_60s"))
    abs_bps = _float_or_none(trade.get("abs_bps"))
    return (
        str(trade.get("side") or "") == "Up"
        and trade.get("branch") == SWEET_BRANCH
        and (_float_or_none(trade.get("entry_price")) or 0.0) >= 0.60
        and (_float_or_none(trade.get("time_left_at_open")) or 0.0) >= 210.0
        and before60_bps is not None
        and before60_bps <= 1.0
        and jump_60s is not None
        and jump_60s >= 6.0
        and abs_bps is not None
        and 6.0 <= abs_bps <= 8.2
    )


def _confirmed_patterns(
    branch_summary: dict[str, Any],
    false_breakout_rule: dict[str, Any],
    paper_v2_rule: dict[str, Any],
) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    sweet = branch_summary.get(SWEET_BRANCH) or {}
    if sweet.get("losses"):
        patterns.append(
            {
                "pattern": "sweet_move_loss_cluster",
                "note": "当前输单集中在 sweet_move_6_8bps 分支，优先复查该分支。",
                "loss_count": sweet.get("losses"),
                "loss_trade_ids": sweet.get("loss_trade_ids"),
            }
        )
    if int(false_breakout_rule.get("matched_losses") or 0) > 0:
        patterns.append(
            {
                "pattern": "early_up_false_breakout",
                "note": "部分 sweet Up 输单符合早段从目标价附近快速冲高后反转的形态。",
                "matched_loss_ids": false_breakout_rule.get("matched_loss_ids"),
                "matched_win_ids": false_breakout_rule.get("matched_win_ids"),
            }
        )
    blocked = paper_v2_rule.get("blocked") if isinstance(paper_v2_rule.get("blocked"), dict) else {}
    kept = paper_v2_rule.get("kept") if isinstance(paper_v2_rule.get("kept"), dict) else {}
    if paper_v2_rule.get("status") == "recommend_apply_paper_guard":
        patterns.append(
            {
                "pattern": "paper_v2_negative_expectancy_split",
                "note": "二代过滤候选显示被过滤样本为负收益，保留样本为正收益。",
                "blocked_trade_ids": paper_v2_rule.get("blocked_trade_ids"),
                "blocked_pnl": blocked.get("pnl"),
                "kept_pnl": kept.get("pnl"),
                "kept_roi_pct": kept.get("roi_pct"),
            }
        )
    return patterns


def _suspected_patterns(unresolved_losses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not unresolved_losses:
        return []
    return [
        {
            "pattern": "unresolved_sweet_reversal",
            "note": "仍有 sweet 输单未被假突破规则解释，后续需要更多盘口和外部源样本。",
            "loss_trade_ids": [item.get("id") for item in unresolved_losses],
        }
    ]


def _rejected_hypotheses(hard_gate_rejection: dict[str, Any]) -> list[dict[str, Any]]:
    if hard_gate_rejection.get("status") != "rejected_overfit_gate":
        return []
    return [
        {
            "hypothesis": "entry>=0.60 时直接要求 confidence>=0.82 且 edge>=0.20",
            "reason": "该硬门槛会误杀已盈利样本，属于过拟合风险。",
            "blocked_win_ids": hard_gate_rejection.get("blocked_win_ids"),
            "estimated_missed_win": hard_gate_rejection.get("estimated_missed_win"),
        }
    ]


def _parameter_recommendations(
    false_breakout_rule: dict[str, Any],
    hard_gate_rejection: dict[str, Any],
    paper_v2_rule: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "action": "apply_paper_guard",
            "target": AGGRESSIVE_EDGE_V1_VARIANT_ID,
            "rule": paper_v2_rule.get("description"),
            "reason": "隔夜样本推翻 sweet Up 分支，且高价单需要更厚 edge 才覆盖固定亏损。",
            "status": paper_v2_rule.get("status"),
        },
        {
            "action": "add_watch_rule",
            "target": "sweet_move_6_8bps",
            "rule": false_breakout_rule.get("description"),
            "reason": "该规则当前命中输单多于赢家，适合作为下一阶段 Paper/REAL 风险标签。",
            "status": "recommend_record_or_paper_guard_first",
        },
        {
            "action": "do_not_apply",
            "target": "confidence_edge_hard_gate",
            "rule": "entry>=0.60 时 confidence>=0.82 且 edge>=0.20",
            "reason": "当前样本显示该规则会误杀盈利单。",
            "status": hard_gate_rejection.get("status"),
        },
        {
            "action": "keep_and_tighten",
            "target": "low_entry_high_edge/high_confidence_high_entry",
            "rule": "保留 low_entry_high_edge；high_confidence_high_entry 按 V2 要求 edge>=0.10 且 abs_bps<12。",
            "status": "tightened_by_paper_v2",
        },
    ]


def _risk_tags(
    false_breakout_rule: dict[str, Any],
    unresolved_losses: list[dict[str, Any]],
    paper_v2_rule: dict[str, Any],
) -> list[str]:
    tags = ["aggressive_edge", "loss_reflection"]
    if int(false_breakout_rule.get("matched_losses") or 0) > 0:
        tags.append("sweet_up_false_breakout")
    if paper_v2_rule.get("status") == "recommend_apply_paper_guard":
        tags.append("paper_v2_guard")
    if unresolved_losses:
        tags.append("unresolved_reversal")
    return tags


def _confidence_level(settled_count: int, loss_count: int, false_breakout_rule: dict[str, Any]) -> str:
    if settled_count >= 30 and loss_count >= 5 and int(false_breakout_rule.get("matched_losses") or 0) >= 3:
        return "HIGH"
    if settled_count >= 10 and loss_count >= 2:
        return "MEDIUM"
    return "LOW"


def _compact_loss_trade(trade: dict[str, Any]) -> dict[str, Any]:
    trajectory = trade.get("trajectory") if isinstance(trade.get("trajectory"), dict) else {}
    return {
        "id": int(trade["id"]),
        "round_id": str(trade.get("round_id") or ""),
        "side": str(trade.get("side") or ""),
        "branch": str(trade.get("branch") or ""),
        "entry_price": _round_float(_float_or_none(trade.get("entry_price")), 4),
        "confidence": _round_float(_float_or_none(trade.get("confidence")), 4),
        "edge": _round_float(_float_or_none(trade.get("edge")), 4),
        "move_bps": _round_float(_float_or_none(trade.get("move_bps")), 4),
        "final_bps": _round_float(_float_or_none(trade.get("final_bps")), 4),
        "drift_bps": _round_float(_float_or_none(trade.get("drift_bps")), 4),
        "time_left_at_open": _round_float(_float_or_none(trade.get("time_left_at_open")), 3),
        "pnl": _round_float(_float_or_none(trade.get("pnl")), 6),
        "trajectory": {
            "before60_bps": _round_float(_nested_float(trajectory.get("before60"), "bps"), 4),
            "before30_bps": _round_float(_nested_float(trajectory.get("before30"), "bps"), 4),
            "open_bps": _round_float(_nested_float(trajectory.get("open"), "bps"), 4),
            "plus30_bps": _round_float(_nested_float(trajectory.get("plus30"), "bps"), 4),
            "plus60_bps": _round_float(_nested_float(trajectory.get("plus60"), "bps"), 4),
            "plus90_bps": _round_float(_nested_float(trajectory.get("plus90"), "bps"), 4),
            "plus120_bps": _round_float(_nested_float(trajectory.get("plus120"), "bps"), 4),
            "pre_open_jump_60s": _round_float(_float_or_none(trajectory.get("pre_open_jump_60s")), 4),
        },
        "risk_tags": ["false_breakout_risk"] if _is_false_breakout_risk(trade) else ["unresolved_reversal"],
    }


def _trade_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [trade for trade in rows if (_float_or_none(trade.get("pnl")) or 0.0) > 0.0]
    losses = [trade for trade in rows if (_float_or_none(trade.get("pnl")) or 0.0) < 0.0]
    stake = sum(_float_or_none(trade.get("stake")) or 0.0 for trade in rows)
    pnl = sum(_float_or_none(trade.get("pnl")) or 0.0 for trade in rows)
    return {
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "pnl": _round_float(pnl, 6),
        "roi_pct": _round_float(pnl / stake * 100.0, 4) if stake else None,
        "win_rate": _round_float(len(wins) / len(rows) * 100.0, 4) if rows else None,
    }


def _entry_id(entry: dict[str, Any]) -> str:
    payload = {
        "strategy_id": entry.get("strategy_id"),
        "sample_window": entry.get("sample_window"),
        "loss_trade_ids": [item.get("id") for item in entry.get("loss_trades") or []],
        "risk_tags": entry.get("risk_tags"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _branch_from_reason(reason: str) -> str:
    match = BRANCH_PATTERN.search(reason or "")
    return match.group(1) if match else "UNKNOWN"


def _nested_float(value: Any, key: str) -> float | None:
    if not isinstance(value, dict):
        return None
    return _float_or_none(value.get(key))


def _distance_bps(price: float | None, target: float | None) -> float | None:
    if price is None or target is None or target <= 0:
        return None
    return _round_float((price - target) / target * 10_000.0, 6)


def _round_float(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成并追加 SINGLE_FAK_AGGRESSIVE_EDGE 策略经验记忆")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_AGGRESSIVE_EDGE_DB_PATH,
        help="策略实验 SQLite 路径，默认读取 data/strategy-experiments/single_fak_aggressive_edge.sqlite3",
    )
    parser.add_argument(
        "--memory-path",
        type=Path,
        default=DEFAULT_AGGRESSIVE_EDGE_MEMORY_PATH,
        help="策略记忆 JSONL 输出路径，默认写入 data/strategy-memory/single_fak_aggressive_edge.memory.jsonl",
    )
    parser.add_argument("--no-append", action="store_true", help="只打印本次生成的记忆内容，不追加到文件")
    parser.add_argument("--allow-duplicate", action="store_true", help="允许相同 entry_id 的样本重复追加")
    args = parser.parse_args(argv)

    entry = build_aggressive_edge_memory_entry(args.db_path, memory_path=args.memory_path)
    if args.no_append:
        print(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    result = append_strategy_memory_entry(args.memory_path, entry, dedupe=not args.allow_duplicate)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

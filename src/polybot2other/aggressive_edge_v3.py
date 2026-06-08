from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Signal
from .signal_filters import SINGLE_AGGRESSIVE_EDGE_MARKER


V3_MIN_SIMILAR_SAMPLES = 6
V3_MIN_SIMILAR_LOSSES = 2
V3_WIN_RATE_MARGIN = 0.02
V3_MAX_HISTORY_ROWS = 1200

_HISTORY_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def aggressive_edge_v3_guard_report(
    signal: Signal,
    risk_report: dict[str, Any] | None,
    *,
    source_db_paths: list[Path],
    loss_replay_paths: list[Path],
    symbol: str = "BTC",
) -> dict[str, Any] | None:
    """生成 V3 下注前直觉报告。

    V3 只在历史相似样本已经证明当前价格带负期望时拦截，避免把少量输局写成固定规则。
    """

    if signal.side not in {"Up", "Down"}:
        return None
    current = _current_fingerprint(signal, risk_report)
    if current is None:
        return None
    history = _load_history(source_db_paths, loss_replay_paths, symbol)
    samples = history.get("samples") if isinstance(history.get("samples"), list) else []
    episodes = history.get("loss_episodes") if isinstance(history.get("loss_episodes"), list) else []
    similar = [sample for sample in samples if _similar_sample(current, sample)]
    wins = sum(1 for sample in similar if sample.get("would_win") is True)
    losses = sum(1 for sample in similar if sample.get("would_win") is False)
    sample_count = len(similar)
    win_rate = wins / sample_count if sample_count else None
    required_win_rate = min(0.95, current["entry_price"] + V3_WIN_RATE_MARGIN)
    expectancy_edge = win_rate - current["entry_price"] if win_rate is not None else None
    episode_matches = [episode for episode in episodes if _similar_loss_episode(current, episode)]
    block = (
        sample_count >= V3_MIN_SIMILAR_SAMPLES
        and losses >= V3_MIN_SIMILAR_LOSSES
        and win_rate is not None
        and win_rate < required_win_rate
        and bool(current["signatures"] or episode_matches)
    )
    top_sources = _source_counts(similar)
    return {
        "block": block,
        "current": current,
        "history": {
            "sample_count": sample_count,
            "win_count": wins,
            "loss_count": losses,
            "win_rate": round(win_rate, 6) if win_rate is not None else None,
            "required_win_rate": round(required_win_rate, 6),
            "expectancy_edge": round(expectancy_edge, 6) if expectancy_edge is not None else None,
            "source_counts": top_sources,
        },
        "loss_episode_matches": [
            {
                "round_id": episode.get("round_id"),
                "side": episode.get("side"),
                "entry_price": episode.get("entry_price"),
                "move_bps": episode.get("move_bps"),
                "fingerprints": episode.get("fingerprints") or [],
            }
            for episode in episode_matches[:5]
        ],
        "history_sources": history.get("sources") or {},
    }


def aggressive_edge_v3_guard_note(report: dict[str, Any] | None) -> str | None:
    """把 V3 直觉报告压缩成交易 reason，方便前端和复盘直接看。"""

    if not report:
        return None
    current = report.get("current") if isinstance(report.get("current"), dict) else {}
    history = report.get("history") if isinstance(report.get("history"), dict) else {}
    signatures = ",".join(current.get("signatures") or []) or "-"
    sample_count = int(history.get("sample_count") or 0)
    loss_count = int(history.get("loss_count") or 0)
    win_rate = _format_pct(history.get("win_rate"))
    required = _format_pct(history.get("required_win_rate"))
    expectancy = _format_float(history.get("expectancy_edge"), 4)
    episode_count = len(report.get("loss_episode_matches") or [])
    action = "BLOCK" if report.get("block") else "OBSERVE"
    return (
        f"{SINGLE_AGGRESSIVE_EDGE_MARKER} V3_INTUITION {action}: "
        f"similar {sample_count}, losses {loss_count}, win_rate {win_rate}, "
        f"required {required}, ev_edge {expectancy}, signatures {signatures}, "
        f"loss_episodes {episode_count}"
    )


def aggressive_edge_v3_memory_summary(
    *,
    source_db_paths: list[Path],
    loss_replay_paths: list[Path],
    symbol: str = "BTC",
) -> dict[str, Any]:
    """返回 V3 可用历史记忆规模，给策略实验面板展示。"""

    history = _load_history(source_db_paths, loss_replay_paths, symbol)
    samples = history.get("samples") if isinstance(history.get("samples"), list) else []
    episodes = history.get("loss_episodes") if isinstance(history.get("loss_episodes"), list) else []
    settled = len(samples)
    losses = sum(1 for sample in samples if sample.get("would_win") is False)
    high_entry = [sample for sample in samples if _maybe_float(sample.get("entry_price")) is not None and float(sample["entry_price"]) >= 0.70]
    high_entry_wins = sum(1 for sample in high_entry if sample.get("would_win") is True)
    low_up_sprint = [
        sample
        for sample in samples
        if sample.get("side") == "Up"
        and _maybe_float(sample.get("entry_price")) is not None
        and float(sample["entry_price"]) < 0.50
        and abs(_maybe_float(sample.get("move_bps")) or 0.0) >= 6.0
    ]
    low_up_sprint_wins = sum(1 for sample in low_up_sprint if sample.get("would_win") is True)
    return {
        "settled_memory_samples": settled,
        "loss_memory_samples": losses,
        "loss_episode_count": len(episodes),
        "high_entry_sample_count": len(high_entry),
        "high_entry_win_rate_pct": round(high_entry_wins / len(high_entry) * 100.0, 4) if high_entry else None,
        "low_up_sprint_sample_count": len(low_up_sprint),
        "low_up_sprint_win_rate_pct": round(low_up_sprint_wins / len(low_up_sprint) * 100.0, 4)
        if low_up_sprint
        else None,
        "ready": settled >= V3_MIN_SIMILAR_SAMPLES,
        "sources": history.get("sources") or {},
    }


def _load_history(source_db_paths: list[Path], loss_replay_paths: list[Path], symbol: str) -> dict[str, Any]:
    db_signature = tuple(_path_signature(path) for path in source_db_paths)
    replay_signature = tuple(_path_signature(path) for path in loss_replay_paths)
    cache_key = (str(symbol or "BTC"), db_signature, replay_signature)
    cached = _HISTORY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    samples: list[dict[str, Any]] = []
    sources: dict[str, Any] = {"db_paths": [], "loss_replay_paths": []}
    seen: set[tuple[Any, ...]] = set()
    for path in source_db_paths:
        if not path.exists():
            continue
        sources["db_paths"].append(str(path))
        for sample in _read_trade_samples(path, symbol):
            key = _sample_identity(sample)
            if key in seen:
                continue
            seen.add(key)
            samples.append(sample)
        for sample in _read_shadow_samples(path, symbol):
            key = _sample_identity(sample)
            if key in seen:
                continue
            seen.add(key)
            samples.append(sample)
    episodes: list[dict[str, Any]] = []
    for path in loss_replay_paths:
        if not path.exists():
            continue
        sources["loss_replay_paths"].append(str(path))
        episodes.extend(_read_loss_episodes(path))
    result = {
        "samples": samples[-V3_MAX_HISTORY_ROWS:],
        "loss_episodes": episodes[-V3_MAX_HISTORY_ROWS:],
        "sources": sources,
    }
    _HISTORY_CACHE.clear()
    _HISTORY_CACHE[cache_key] = result
    return result


def _read_trade_samples(path: Path, symbol: str) -> list[dict[str, Any]]:
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        if not _table_exists(con, "trades"):
            return []
        rows = con.execute(
            """
            SELECT round_id, symbol, side, entry_price, confidence, move_bps, pnl, reason
            FROM trades
            WHERE symbol = ?
              AND status = 'SETTLED'
              AND pnl IS NOT NULL
            ORDER BY id ASC
            """,
            (str(symbol or "BTC"),),
        ).fetchall()
        samples: list[dict[str, Any]] = []
        for row in rows:
            entry_price = _maybe_float(row["entry_price"])
            move_bps = _maybe_float(row["move_bps"])
            if entry_price is None or move_bps is None:
                continue
            sample = _sample_from_values(
                round_id=str(row["round_id"] or ""),
                side=str(row["side"] or ""),
                entry_price=entry_price,
                confidence=_maybe_float(row["confidence"]),
                move_bps=move_bps,
                would_win=(_maybe_float(row["pnl"]) or 0.0) > 0.0,
                source=f"trade:{path.name}",
                reason=str(row["reason"] or ""),
            )
            if sample:
                samples.append(sample)
        return samples
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _read_shadow_samples(path: Path, symbol: str) -> list[dict[str, Any]]:
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        if not _table_exists(con, "aggressive_edge_v2_shadow_samples"):
            return []
        rows = con.execute(
            """
            SELECT round_id, side, entry_price, confidence, move_bps,
                   risk_score, risk_level, features_json, components_json,
                   would_win, signal_reason
            FROM aggressive_edge_v2_shadow_samples
            WHERE symbol = ?
              AND settled_at IS NOT NULL
              AND base_would_trade = 1
              AND would_win IS NOT NULL
            ORDER BY id ASC
            """,
            (str(symbol or "BTC"),),
        ).fetchall()
        samples: list[dict[str, Any]] = []
        for row in rows:
            features = _json_dict(row["features_json"])
            sample = _sample_from_values(
                round_id=str(row["round_id"] or ""),
                side=str(row["side"] or ""),
                entry_price=_maybe_float(row["entry_price"]) or _maybe_float(features.get("entry_price")),
                confidence=_maybe_float(row["confidence"]) or _maybe_float(features.get("confidence")),
                move_bps=_maybe_float(row["move_bps"]) or _maybe_float(features.get("move_bps")),
                would_win=bool(int(row["would_win"])),
                source=f"shadow:{path.name}",
                reason=str(row["signal_reason"] or ""),
                risk_score=_maybe_float(row["risk_score"]),
                risk_level=str(row["risk_level"] or ""),
                features=features,
            )
            if sample:
                samples.append(sample)
        return samples
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _read_loss_episodes(path: Path) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return episodes
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            packet = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        summary = packet.get("summary") if isinstance(packet.get("summary"), dict) else {}
        for trade in packet.get("loss_trades") or []:
            if not isinstance(trade, dict):
                continue
            entry_price = _maybe_float(trade.get("entry_price"))
            move_bps = _maybe_float(trade.get("move_bps"))
            side = str(trade.get("side") or "")
            if entry_price is None or move_bps is None or side not in {"Up", "Down"}:
                continue
            fingerprints = _loss_episode_fingerprints(side, entry_price, move_bps, summary)
            episodes.append(
                {
                    "round_id": str(trade.get("round_id") or ""),
                    "side": side,
                    "entry_price": round(entry_price, 4),
                    "move_bps": round(move_bps, 4),
                    "entry_band": _entry_band(entry_price),
                    "move_band": _move_band(move_bps),
                    "fingerprints": fingerprints,
                    "source": f"episode:{path.name}",
                }
            )
    return episodes


def _current_fingerprint(signal: Signal, risk_report: dict[str, Any] | None) -> dict[str, Any] | None:
    entry_price = _maybe_float(signal.entry_price)
    confidence = _maybe_float(signal.confidence)
    move_bps = _maybe_float(signal.move_bps)
    if entry_price is None or confidence is None or move_bps is None:
        return None
    features = risk_report.get("features") if isinstance(risk_report, dict) and isinstance(risk_report.get("features"), dict) else {}
    risk_score = _maybe_float(risk_report.get("risk_score")) if isinstance(risk_report, dict) else None
    signatures = _candidate_signatures(
        signal.side,
        entry_price,
        confidence,
        move_bps,
        risk_score,
        features,
    )
    return {
        "side": signal.side,
        "entry_price": round(entry_price, 4),
        "confidence": round(confidence, 4),
        "edge": round(confidence - entry_price, 4),
        "move_bps": round(move_bps, 4),
        "entry_band": _entry_band(entry_price),
        "move_band": _move_band(move_bps),
        "risk_bucket": _risk_bucket(risk_score),
        "signatures": signatures,
    }


def _sample_from_values(
    *,
    round_id: str,
    side: str,
    entry_price: float | None,
    confidence: float | None,
    move_bps: float | None,
    would_win: bool,
    source: str,
    reason: str,
    risk_score: float | None = None,
    risk_level: str = "",
    features: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if side not in {"Up", "Down"} or entry_price is None or move_bps is None:
        return None
    features = features or {}
    signatures = _candidate_signatures(side, entry_price, confidence, move_bps, risk_score, features)
    return {
        "round_id": round_id,
        "side": side,
        "entry_price": round(entry_price, 4),
        "confidence": round(confidence, 4) if confidence is not None else None,
        "move_bps": round(move_bps, 4),
        "entry_band": _entry_band(entry_price),
        "move_band": _move_band(move_bps),
        "risk_bucket": _risk_bucket(risk_score),
        "risk_score": round(risk_score, 4) if risk_score is not None else None,
        "risk_level": risk_level,
        "signatures": signatures,
        "would_win": bool(would_win),
        "source": source,
        "reason": reason[:500],
    }


def _candidate_signatures(
    side: str,
    entry_price: float,
    confidence: float | None,
    move_bps: float,
    risk_score: float | None,
    features: dict[str, Any],
) -> list[str]:
    signatures: list[str] = []
    abs_bps = abs(move_bps)
    edge = (confidence - entry_price) if confidence is not None else None
    if entry_price >= 0.70:
        signatures.append("high_entry_fragility")
    if edge is not None and entry_price >= 0.70 and edge < 0.08:
        signatures.append("critical_high_entry_thin_edge")
    if side == "Up" and entry_price >= 0.60 and 6.0 <= abs_bps <= 8.2:
        signatures.append("sweet_up_reversal_zone")
    if side == "Up" and entry_price < 0.50 and abs_bps >= 6.0:
        signatures.append("low_entry_up_sprint_reversal")
    if side == "Up" and edge is not None and entry_price < 0.50 and edge >= 0.25 and abs_bps >= 6.0:
        signatures.append("low_entry_false_safety")
    if side == "Down" and entry_price >= 0.64 and abs_bps >= 7.0:
        signatures.append("down_flush_rebound_zone")
    if edge is not None and entry_price >= 0.68 and edge < 0.16:
        signatures.append("thin_edge_high_price")
    momentum_decay = _maybe_float(features.get("momentum_decay_bps"))
    momentum_30_to_now = _maybe_float(features.get("momentum_30_to_now_bps"))
    if momentum_decay is not None and momentum_decay >= 3.0:
        signatures.append("momentum_decay")
    if momentum_30_to_now is not None and momentum_30_to_now < -2.0:
        signatures.append("post_jump_stall")
    if (_maybe_float(features.get("external_divergence_count")) or 0.0) >= 1.0:
        signatures.append("external_divergence")
    if risk_score is not None and 0.10 <= risk_score < 0.20:
        signatures.append("v2_low_mid_risk_trap")
    return signatures


def _loss_episode_fingerprints(side: str, entry_price: float, move_bps: float, summary: dict[str, Any]) -> list[str]:
    fingerprints = _candidate_signatures(side, entry_price, None, move_bps, None, {})
    min_bps = _maybe_float(summary.get("chainlink_distance_bps_min"))
    max_bps = _maybe_float(summary.get("chainlink_distance_bps_max"))
    if min_bps is not None and max_bps is not None:
        amplitude = max_bps - min_bps
        if amplitude >= 12.0:
            fingerprints.append("full_round_large_reversal")
        if side == "Up" and min_bps < -2.0 and max_bps > abs(move_bps) * 0.8:
            fingerprints.append("up_failed_to_hold_advantage")
        if side == "Down" and max_bps > 2.0 and abs(min_bps) > abs(move_bps) * 0.8:
            fingerprints.append("down_failed_to_hold_advantage")
    return sorted(set(fingerprints))


def _similar_sample(current: dict[str, Any], sample: dict[str, Any]) -> bool:
    if current.get("side") != sample.get("side"):
        return False
    score = 0.0
    if current.get("entry_band") == sample.get("entry_band"):
        score += 2.0
    if current.get("move_band") == sample.get("move_band"):
        score += 1.25
    if current.get("risk_bucket") == sample.get("risk_bucket"):
        score += 0.5
    overlap = set(current.get("signatures") or []) & set(sample.get("signatures") or [])
    if overlap:
        score += 1.5
    if current.get("entry_band") == "0.70+" and sample.get("entry_band") == "0.70+":
        score += 0.75
    return score >= 3.25


def _similar_loss_episode(current: dict[str, Any], episode: dict[str, Any]) -> bool:
    if current.get("side") != episode.get("side"):
        return False
    if current.get("entry_band") != episode.get("entry_band"):
        return False
    current_signatures = set(current.get("signatures") or [])
    episode_signatures = set(episode.get("fingerprints") or [])
    return bool(current_signatures & episode_signatures) or current.get("move_band") == episode.get("move_band")


def _entry_band(entry_price: float) -> str:
    if entry_price < 0.60:
        return "<0.60"
    if entry_price < 0.70:
        return "0.60-0.70"
    return "0.70+"


def _move_band(move_bps: float) -> str:
    abs_bps = abs(move_bps)
    if abs_bps < 6.0:
        return "<6bps"
    if abs_bps <= 8.2:
        return "6-8bps"
    if abs_bps < 12.0:
        return "8-12bps"
    return "12bps+"


def _risk_bucket(risk_score: float | None) -> str:
    if risk_score is None:
        return "unknown"
    if risk_score < 0.10:
        return "0-0.10"
    if risk_score < 0.20:
        return "0.10-0.20"
    if risk_score < 0.35:
        return "0.20-0.35"
    return "0.35+"


def _source_counts(samples: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sample in samples:
        source = str(sample.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[:6])


def _sample_identity(sample: dict[str, Any]) -> tuple[Any, ...]:
    return (
        sample.get("round_id"),
        sample.get("side"),
        sample.get("entry_band"),
        sample.get("move_band"),
        round(_maybe_float(sample.get("entry_price")) or 0.0, 2),
    )


def _path_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), 0, 0)
    return (str(path), int(stat.st_mtime_ns), int(stat.st_size))


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_pct(value: Any) -> str:
    number = _maybe_float(value)
    if number is None:
        return "-"
    return f"{number * 100.0:.2f}%"


def _format_float(value: Any, digits: int) -> str:
    number = _maybe_float(value)
    if number is None:
        return "-"
    return f"{number:.{digits}f}"

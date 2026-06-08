from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/strategy-experiments/single_fak_aggressive_edge_v5_diagnostic.sqlite3")
DEFAULT_OUTPUT_DIR = Path("data/strategy-experiments/monitor")
DEFAULT_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC"
DEFAULT_INTERVAL_SECONDS = 300.0
PLAN_JSON_NAME = "single_fak_aggressive_edge_v5_plan.json"
PLAN_MARKDOWN_NAME = "single_fak_aggressive_edge_v5_plan.md"

STATUS_WAITING_FOR_SAMPLE = "CONTINUE_SAMPLING"
STATUS_EARLY_FAIL_REVIEW_REQUIRED = "V5_EARLY_FAIL_REVIEW_REQUIRED"
STATUS_PROMISING_BUT_INSUFFICIENT = "PROMISING_BUT_INSUFFICIENT"
STATUS_FAILED_PREPARE_V6 = "V5_FAILED_PREPARE_V6"
STATUS_FULL_REVIEW_REQUIRED = "FULL_REVIEW_REQUIRED"
STATUS_DB_UNAVAILABLE = "DB_UNAVAILABLE"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorThresholds:
    """V5 监控阈值；只生成计划，不直接触发策略改动。"""

    early_review_samples: int = 20
    mid_review_samples: int = 40
    full_review_samples: int = 80
    stable_samples: int = 100
    min_acceptable_win_rate_pct: float = 75.0
    promising_win_rate_pct: float = 80.0
    min_acceptable_roi_pct: float = 10.0
    promising_roi_pct: float = 15.0


def collect_v5_monitor_snapshot(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    variant_id: str = DEFAULT_VARIANT_ID,
    now: float | None = None,
    thresholds: MonitorThresholds | None = None,
) -> dict[str, Any]:
    """读取 V5 样本库并生成下一步计划。

    这个函数只读数据库，方便测试和后台任务复用，任何自动交易动作都不在这里发生。
    """

    checked_at = time.time() if now is None else float(now)
    threshold = thresholds or MonitorThresholds()
    path = Path(db_path)
    if not path.exists():
        plan = _decision_for_unavailable_db(path, threshold)
        return _snapshot_payload(
            checked_at=checked_at,
            db_path=path,
            variant_id=variant_id,
            metrics=_empty_metrics(),
            by_side=[],
            by_bucket=[],
            plan=plan,
            thresholds=threshold,
        )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "aggressive_edge_v2_shadow_samples"):
            plan = _decision_for_unavailable_db(path, threshold, reason="shadow table missing")
            return _snapshot_payload(
                checked_at=checked_at,
                db_path=path,
                variant_id=variant_id,
                metrics=_empty_metrics(),
                by_side=[],
                by_bucket=[],
                plan=plan,
                thresholds=threshold,
            )
        metrics = _collect_metrics(conn)
        by_side = _collect_breakdown(conn, "side")
        by_bucket = _collect_breakdown(conn, "substr(sample_key,1,2)")

    plan = decide_v5_next_plan(metrics, threshold)
    return _snapshot_payload(
        checked_at=checked_at,
        db_path=path,
        variant_id=variant_id,
        metrics=metrics,
        by_side=by_side,
        by_bucket=by_bucket,
        plan=plan,
        thresholds=threshold,
    )


def decide_v5_next_plan(metrics: dict[str, Any], thresholds: MonitorThresholds | None = None) -> dict[str, Any]:
    """根据 V5 指标生成下一步计划，禁止自动改策略或触发交易。"""

    threshold = thresholds or MonitorThresholds()
    settled = int(metrics.get("v5_would_trade_settled") or 0)
    win_rate = _float_or_none(metrics.get("v5_win_rate_pct"))
    roi = _float_or_none(metrics.get("v5_roi_pct"))

    if settled >= threshold.full_review_samples:
        return {
            "status": STATUS_FULL_REVIEW_REQUIRED,
            "severity": "HIGH",
            "decision": "触发完整复盘",
            "next_step": "暂停新增策略改动，复盘 V5 全部输单、误杀样本、方向和时间桶结构",
            "reason": f"V5 已结算 {settled} 单，达到完整复盘线 {threshold.full_review_samples}",
            "blocked_actions": _blocked_actions(),
        }

    if settled >= threshold.mid_review_samples and _fails_quality_gate(metrics, threshold):
        return {
            "status": STATUS_FAILED_PREPARE_V6,
            "severity": "HIGH",
            "decision": "判定 V5 阶段性失败，准备 V6 设计",
            "next_step": "先做失败复盘，再提出 V6 规则，不允许直接扩大下注",
            "reason": _quality_gate_reason(metrics, threshold),
            "blocked_actions": _blocked_actions(),
        }

    if settled >= threshold.early_review_samples and _fails_quality_gate(metrics, threshold):
        return {
            "status": STATUS_EARLY_FAIL_REVIEW_REQUIRED,
            "severity": "MEDIUM",
            "decision": "触发小复盘",
            "next_step": "复盘 V5 输单和 V5 拦截但原始会赢的样本，暂不改代码",
            "reason": _quality_gate_reason(metrics, threshold),
            "blocked_actions": _blocked_actions(),
        }

    if settled >= threshold.early_review_samples and _is_promising(metrics, threshold):
        return {
            "status": STATUS_PROMISING_BUT_INSUFFICIENT,
            "severity": "LOW",
            "decision": "继续采样到 40 单",
            "next_step": "保持 V5 Diagnostic 运行，不切 Paper，不碰 REAL",
            "reason": (
                f"V5 已结算 {settled} 单，胜率和 ROI 达到积极阈值，"
                f"仍低于 {threshold.mid_review_samples} 单中期复盘口径"
            ),
            "blocked_actions": _blocked_actions(),
        }

    return {
        "status": STATUS_WAITING_FOR_SAMPLE,
        "severity": "LOW",
        "decision": "继续采样",
        "next_step": "等待 V5 已结算放行样本达到 20 单后做小复盘",
        "reason": f"V5 已结算 {settled} 单，低于小复盘线 {threshold.early_review_samples}",
        "blocked_actions": _blocked_actions(),
    }


def write_monitor_plan(snapshot: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, str]:
    """把监控结果写成 JSON 和 Markdown，写入过程使用临时文件替换。"""

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / PLAN_JSON_NAME
    markdown_path = target_dir / PLAN_MARKDOWN_NAME
    _atomic_write_text(json_path, json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _atomic_write_text(markdown_path, render_markdown_plan(snapshot))
    return {"json": str(json_path), "markdown": str(markdown_path)}


def render_markdown_plan(snapshot: dict[str, Any]) -> str:
    """生成给人工复盘看的 Markdown 计划。"""

    metrics = snapshot.get("metrics") or {}
    plan = snapshot.get("plan") or {}
    by_side = snapshot.get("by_side") or []
    by_bucket = snapshot.get("by_bucket") or []
    checked_at = _format_time(snapshot.get("checked_at"))
    lines = [
        "# SINGLE + FAK Aggressive Edge V5 Diagnostic Monitor",
        "",
        f"- 检查时间: {checked_at}",
        f"- 策略: {snapshot.get('variant_id')}",
        f"- 数据库: {snapshot.get('db_path')}",
        f"- 状态: {plan.get('status')}",
        f"- 决策: {plan.get('decision')}",
        f"- 下一步: {plan.get('next_step')}",
        f"- 原因: {plan.get('reason')}",
        "",
        "## 样本概览",
        "",
        "| 项目 | 当前值 |",
        "| --- | ---: |",
        f"| 全部影子样本 | {_int(metrics.get('shadow_total'))} |",
        f"| 已结算影子样本 | {_int(metrics.get('shadow_settled'))} |",
        f"| 原始 Aggressive Edge 会下注样本 | {_int(metrics.get('base_would_trade_settled'))} 已结算 |",
        f"| 原始会下注胜率 | {_pct_or_dash(metrics.get('base_win_rate_pct'))} |",
        f"| V4 会放行样本 | {_int(metrics.get('v4_would_trade_total'))} |",
        f"| V5 会放行样本 | {_int(metrics.get('v5_would_trade_total'))} |",
        f"| V5 已结算样本 | {_int(metrics.get('v5_would_trade_settled'))} |",
        f"| V5 未结算样本 | {_int(metrics.get('v5_unsettled'))} |",
        f"| V5 胜 | {_int(metrics.get('v5_would_win'))} |",
        f"| V5 负 | {_int(metrics.get('v5_would_loss'))} |",
        f"| V5 胜率 | {_pct_or_dash(metrics.get('v5_win_rate_pct'))} |",
        f"| V5 模拟 ROI | {_pct_or_dash(metrics.get('v5_roi_pct'))} |",
        f"| 距离 80 单复盘线 | {_remaining_label(metrics.get('v5_would_trade_settled'), 80)} |",
        f"| 距离 100 单稳定性线 | {_remaining_label(metrics.get('v5_would_trade_settled'), 100)} |",
        "",
        "## 分方向",
        "",
        "| 方向 | 已结算 | 胜 | 负 | 胜率 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    if by_side:
        lines.extend(
            f"| {row.get('key')} | {_int(row.get('n'))} | {_int(row.get('wins'))} | "
            f"{_int(row.get('losses'))} | {_pct_or_dash(row.get('win_rate_pct'))} |"
            for row in by_side
        )
    else:
        lines.append("| 暂无 | 0 | 0 | 0 | - |")

    lines.extend(
        [
            "",
            "## 分时间桶",
            "",
            "| 桶 | 已结算 | 胜 | 负 | 胜率 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    if by_bucket:
        lines.extend(
            f"| {row.get('key')} | {_int(row.get('n'))} | {_int(row.get('wins'))} | "
            f"{_int(row.get('losses'))} | {_pct_or_dash(row.get('win_rate_pct'))} |"
            for row in by_bucket
        )
    else:
        lines.append("| 暂无 | 0 | 0 | 0 | - |")

    lines.extend(
        [
            "",
            "## 禁止自动执行",
            "",
            *[f"- {item}" for item in plan.get("blocked_actions") or _blocked_actions()],
            "",
        ]
    )
    return "\n".join(lines)


def run_monitor_loop(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    once: bool = False,
    variant_id: str = DEFAULT_VARIANT_ID,
) -> None:
    """执行监控循环；每次检查都会覆盖计划文件并输出日志。"""

    interval = max(30.0, float(interval_seconds))
    while True:
        snapshot = collect_v5_monitor_snapshot(db_path, variant_id=variant_id)
        paths = write_monitor_plan(snapshot, output_dir)
        plan = snapshot.get("plan") or {}
        metrics = snapshot.get("metrics") or {}
        logger.info(
            "V5样本监控 status=%s settled=%s win_rate=%s roi=%s json=%s",
            plan.get("status"),
            metrics.get("v5_would_trade_settled"),
            metrics.get("v5_win_rate_pct"),
            metrics.get("v5_roi_pct"),
            paths.get("json"),
        )
        if once:
            return
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="监控 SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC 样本并生成下一步计划")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="V5 Diagnostic SQLite 路径")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="计划文件输出目录")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS, help="循环检查间隔，默认 300 秒")
    parser.add_argument("--once", action="store_true", help="只执行一次检查并退出")
    parser.add_argument("--variant-id", default=DEFAULT_VARIANT_ID, help="写入计划文件的策略 ID")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="日志级别")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    run_monitor_loop(
        db_path=args.db_path,
        output_dir=args.output_dir,
        interval_seconds=args.interval_seconds,
        once=args.once,
        variant_id=args.variant_id,
    )
    return 0


def _collect_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS shadow_total,
            SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS shadow_settled,
            SUM(CASE WHEN base_would_trade = 1 THEN 1 ELSE 0 END) AS base_would_trade_total,
            SUM(CASE WHEN base_would_trade = 1 AND outcome IS NOT NULL THEN 1 ELSE 0 END) AS base_would_trade_settled,
            SUM(CASE WHEN base_would_trade = 1 AND outcome IS NOT NULL AND would_win = 1 THEN 1 ELSE 0 END) AS base_would_win,
            SUM(CASE WHEN base_would_trade = 1 AND outcome IS NOT NULL AND would_win = 0 THEN 1 ELSE 0 END) AS base_would_loss,
            SUM(CASE WHEN v4_would_trade = 1 THEN 1 ELSE 0 END) AS v4_would_trade_total,
            SUM(CASE WHEN v4_would_trade = 1 AND outcome IS NOT NULL THEN 1 ELSE 0 END) AS v4_would_trade_settled,
            SUM(CASE WHEN v5_would_trade = 1 THEN 1 ELSE 0 END) AS v5_would_trade_total,
            SUM(CASE WHEN v5_would_trade = 1 AND outcome IS NOT NULL THEN 1 ELSE 0 END) AS v5_would_trade_settled,
            SUM(CASE WHEN v5_would_trade = 1 AND outcome IS NOT NULL AND would_win = 1 THEN 1 ELSE 0 END) AS v5_would_win,
            SUM(CASE WHEN v5_would_trade = 1 AND outcome IS NOT NULL AND would_win = 0 THEN 1 ELSE 0 END) AS v5_would_loss,
            SUM(CASE WHEN v5_would_trade = 1 AND outcome IS NULL THEN 1 ELSE 0 END) AS v5_unsettled,
            AVG(CASE
                WHEN v5_would_trade = 1 AND outcome IS NOT NULL AND entry_price IS NOT NULL AND entry_price > 0
                THEN CASE WHEN would_win = 1 THEN (1.0 - entry_price) / entry_price ELSE -1.0 END
                ELSE NULL
            END) * 100.0 AS v5_roi_pct
        FROM aggressive_edge_v2_shadow_samples
        """
    ).fetchone()
    metrics = dict(row) if row is not None else _empty_metrics()
    base_settled = _int(metrics.get("base_would_trade_settled"))
    base_wins = _int(metrics.get("base_would_win"))
    v5_settled = _int(metrics.get("v5_would_trade_settled"))
    v5_wins = _int(metrics.get("v5_would_win"))
    metrics["base_win_rate_pct"] = round(base_wins / base_settled * 100.0, 4) if base_settled else None
    metrics["v5_win_rate_pct"] = round(v5_wins / v5_settled * 100.0, 4) if v5_settled else None
    return {key: _normalize_metric_value(value) for key, value in metrics.items()}


def _collect_breakdown(conn: sqlite3.Connection, expression: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            {expression} AS key,
            COUNT(*) AS n,
            SUM(CASE WHEN would_win = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN would_win = 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(SUM(CASE WHEN would_win = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS win_rate_pct
        FROM aggressive_edge_v2_shadow_samples
        WHERE v5_would_trade = 1 AND outcome IS NOT NULL
        GROUP BY key
        ORDER BY key
        """
    ).fetchall()
    return [{key: _normalize_metric_value(value) for key, value in dict(row).items()} for row in rows]


def _snapshot_payload(
    *,
    checked_at: float,
    db_path: Path,
    variant_id: str,
    metrics: dict[str, Any],
    by_side: list[dict[str, Any]],
    by_bucket: list[dict[str, Any]],
    plan: dict[str, Any],
    thresholds: MonitorThresholds,
) -> dict[str, Any]:
    return {
        "checked_at": round(float(checked_at), 3),
        "checked_at_label": _format_time(checked_at),
        "variant_id": variant_id,
        "db_path": str(db_path),
        "metrics": metrics,
        "by_side": by_side,
        "by_bucket": by_bucket,
        "plan": plan,
        "thresholds": {
            "early_review_samples": thresholds.early_review_samples,
            "mid_review_samples": thresholds.mid_review_samples,
            "full_review_samples": thresholds.full_review_samples,
            "stable_samples": thresholds.stable_samples,
            "min_acceptable_win_rate_pct": thresholds.min_acceptable_win_rate_pct,
            "promising_win_rate_pct": thresholds.promising_win_rate_pct,
            "min_acceptable_roi_pct": thresholds.min_acceptable_roi_pct,
            "promising_roi_pct": thresholds.promising_roi_pct,
        },
    }


def _decision_for_unavailable_db(
    db_path: Path,
    thresholds: MonitorThresholds,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    detail = reason or f"数据库不存在: {db_path}"
    return {
        "status": STATUS_DB_UNAVAILABLE,
        "severity": "HIGH",
        "decision": "等待数据库可用",
        "next_step": "检查 V5 Diagnostic 服务是否已经启动并写入样本库",
        "reason": detail,
        "blocked_actions": _blocked_actions(),
        "full_review_samples": thresholds.full_review_samples,
    }


def _fails_quality_gate(metrics: dict[str, Any], thresholds: MonitorThresholds) -> bool:
    win_rate = _float_or_none(metrics.get("v5_win_rate_pct"))
    roi = _float_or_none(metrics.get("v5_roi_pct"))
    if win_rate is None or roi is None:
        return True
    return win_rate < thresholds.min_acceptable_win_rate_pct or roi < thresholds.min_acceptable_roi_pct


def _is_promising(metrics: dict[str, Any], thresholds: MonitorThresholds) -> bool:
    win_rate = _float_or_none(metrics.get("v5_win_rate_pct"))
    roi = _float_or_none(metrics.get("v5_roi_pct"))
    return (
        win_rate is not None
        and roi is not None
        and win_rate >= thresholds.promising_win_rate_pct
        and roi >= thresholds.promising_roi_pct
    )


def _quality_gate_reason(metrics: dict[str, Any], thresholds: MonitorThresholds) -> str:
    settled = _int(metrics.get("v5_would_trade_settled"))
    win_rate = _float_or_none(metrics.get("v5_win_rate_pct"))
    roi = _float_or_none(metrics.get("v5_roi_pct"))
    return (
        f"V5 已结算 {settled} 单，胜率 {_pct_or_dash(win_rate)}，ROI {_pct_or_dash(roi)}，"
        f"低于最低要求 {thresholds.min_acceptable_win_rate_pct:.2f}% / {thresholds.min_acceptable_roi_pct:.2f}%"
    )


def _blocked_actions() -> list[str]:
    return [
        "禁止自动改策略代码",
        "禁止自动重启交易服务",
        "禁止自动切换 Paper",
        "禁止自动启用 REAL",
        "禁止自动下单",
    ]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (str(table_name),),
    ).fetchone()
    return row is not None


def _empty_metrics() -> dict[str, Any]:
    return {
        "shadow_total": 0,
        "shadow_settled": 0,
        "base_would_trade_total": 0,
        "base_would_trade_settled": 0,
        "base_would_win": 0,
        "base_would_loss": 0,
        "base_win_rate_pct": None,
        "v4_would_trade_total": 0,
        "v4_would_trade_settled": 0,
        "v5_would_trade_total": 0,
        "v5_would_trade_settled": 0,
        "v5_would_win": 0,
        "v5_would_loss": 0,
        "v5_unsettled": 0,
        "v5_win_rate_pct": None,
        "v5_roi_pct": None,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _normalize_metric_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if value is None:
        return None
    if isinstance(value, int):
        return int(value)
    return value


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _pct_or_dash(value: Any) -> str:
    parsed = _float_or_none(value)
    if parsed is None:
        return "-"
    return f"{parsed:.2f}%"


def _remaining_label(value: Any, target: int) -> str:
    settled = _int(value)
    remaining = int(target) - settled
    if remaining > 0:
        return f"还差 {remaining} 单"
    return f"已超过 {abs(remaining)} 单"


def _format_time(value: Any) -> str:
    parsed = _float_or_none(value)
    if parsed is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(parsed))


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .bot import PaperTradingBot
from .config import Settings, load_settings
from .storage import TradeStore
from .web import _strategy_experiments_retrospective_report_html


def generate_strategy_experiment_report_snapshot(
    settings: Settings | None = None,
    output_path: Path | None = None,
    *,
    start_at: float | None = None,
    end_at: float | None = None,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """生成 8 组合复盘 HTML 快照；只读现有 Paper 数据，不启动交易循环。"""

    settings = settings or load_settings()
    generated_at = time.time() if generated_at is None else float(generated_at)
    output_path = output_path or _default_output_path(generated_at)
    store = TradeStore(settings.db_path, settings.initial_balance)
    bot = PaperTradingBot(settings, store)
    report = bot.strategy_experiments_retrospective(start_at, end_at)
    html = _strategy_experiments_retrospective_report_html(report, generated_at=generated_at)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    profit = report.get("profit_summary") or {}
    return {
        "output_path": str(output_path),
        "generated_at": generated_at,
        "enabled": report.get("enabled"),
        "variant_count": len(report.get("variants") or []),
        "profit_status": profit.get("status"),
        "profitable_winner_ready": profit.get("profitable_winner_ready"),
        "winner_variant_id": profit.get("winner_variant_id"),
        "current_profit_leader_variant_id": profit.get("current_profit_leader_variant_id"),
        "start_at": start_at,
        "end_at": end_at,
    }


def _default_output_path(generated_at: float) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(generated_at))
    return Path("docs") / f"strategy-experiments-retrospective-{stamp}.html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export strategy experiment retrospective HTML snapshot")
    parser.add_argument("--output", type=Path, default=None, help="Output HTML path; defaults to docs/strategy-experiments-retrospective-<timestamp>.html")
    parser.add_argument("--start-at", type=float, default=None, help="Optional unix timestamp window start")
    parser.add_argument("--end-at", type=float, default=None, help="Optional unix timestamp window end")
    args = parser.parse_args(argv)
    result = generate_strategy_experiment_report_snapshot(
        output_path=args.output,
        start_at=args.start_at,
        end_at=args.end_at,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

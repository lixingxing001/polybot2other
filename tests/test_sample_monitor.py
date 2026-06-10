from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from polybot2other.sample_monitor import (
    STATUS_EARLY_FAIL_REVIEW_REQUIRED,
    STATUS_FAILED_PREPARE_V6,
    STATUS_FULL_REVIEW_REQUIRED,
    STATUS_PROMISING_BUT_INSUFFICIENT,
    STATUS_WAITING_FOR_SAMPLE,
    collect_v5_monitor_snapshot,
    decide_v5_next_plan,
    write_monitor_plan,
)
from polybot2other.storage import SETTLEMENT_SOURCE_POLYMARKET, TradeStore


class SampleMonitorTestCase(unittest.TestCase):
    def test_decide_v5_next_plan_waits_before_twenty_samples(self) -> None:
        plan = decide_v5_next_plan(
            {
                "v5_would_trade_settled": 6,
                "v5_win_rate_pct": 66.67,
                "v5_roi_pct": -5.77,
            }
        )

        self.assertEqual(plan["status"], STATUS_WAITING_FOR_SAMPLE)
        self.assertIn("禁止自动下单", plan["blocked_actions"])

    def test_decide_v5_next_plan_triggers_early_fail_review(self) -> None:
        plan = decide_v5_next_plan(
            {
                "v5_would_trade_settled": 20,
                "v5_win_rate_pct": 70.0,
                "v5_roi_pct": 8.0,
            }
        )

        self.assertEqual(plan["status"], STATUS_EARLY_FAIL_REVIEW_REQUIRED)
        self.assertEqual(plan["decision"], "触发小复盘")

    def test_decide_v5_next_plan_marks_promising_before_mid_review(self) -> None:
        plan = decide_v5_next_plan(
            {
                "v5_would_trade_settled": 20,
                "v5_win_rate_pct": 80.0,
                "v5_roi_pct": 18.0,
            }
        )

        self.assertEqual(plan["status"], STATUS_PROMISING_BUT_INSUFFICIENT)
        self.assertIn("继续采样", plan["decision"])

    def test_decide_v5_next_plan_marks_failed_after_forty_samples(self) -> None:
        plan = decide_v5_next_plan(
            {
                "v5_would_trade_settled": 40,
                "v5_win_rate_pct": 74.0,
                "v5_roi_pct": 9.0,
            }
        )

        self.assertEqual(plan["status"], STATUS_FAILED_PREPARE_V6)

    def test_decide_v5_next_plan_requests_full_review_at_eighty_samples(self) -> None:
        plan = decide_v5_next_plan(
            {
                "v5_would_trade_settled": 80,
                "v5_win_rate_pct": 90.0,
                "v5_roi_pct": 25.0,
            }
        )

        self.assertEqual(plan["status"], STATUS_FULL_REVIEW_REQUIRED)

    def test_collect_v5_monitor_snapshot_and_write_plan_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v5.sqlite3"
            output_dir = Path(tmp) / "monitor"
            store = TradeStore(db_path, 100.0)
            now = time.time()
            for index in range(4):
                round_id = f"btc-updown-5m-monitor-{index}"
                store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key=f"m{2 + index % 2}:pass",
                    side="Up",
                    source_signal_side="Up",
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    entry_price=0.65,
                    confidence=0.78,
                    move_bps=6.5,
                    report={"risk_score": 0.1, "risk_level": "LOW", "risk_reasons": [], "features": {}, "components": {}},
                    base_block_reason=None,
                    v1_block_reason=None,
                    v4_block_reason=None,
                    v5_block_reason=None,
                    signal_reason="监控测试样本",
                    created_at=now + index,
                )
                outcome = "Up" if index < 3 else "Down"
                store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    outcome,
                    now + index + 1,
                    final_price=101.0 if outcome == "Up" else 99.0,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )

            snapshot = collect_v5_monitor_snapshot(db_path, now=now + 10)
            self.assertEqual(snapshot["metrics"]["v5_would_trade_settled"], 4)
            self.assertEqual(snapshot["metrics"]["v5_would_win"], 3)
            self.assertEqual(snapshot["metrics"]["v5_would_loss"], 1)
            self.assertEqual(snapshot["plan"]["status"], STATUS_WAITING_FOR_SAMPLE)
            self.assertEqual(snapshot["by_side"][0]["key"], "Up")
            paths = write_monitor_plan(snapshot, output_dir)
            json_payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
            self.assertEqual(json_payload["metrics"]["v5_would_trade_settled"], 4)
            self.assertIn("SINGLE + FAK Aggressive Edge V5 Diagnostic Monitor", markdown)
            self.assertIn("禁止自动下单", markdown)


if __name__ == "__main__":
    unittest.main()

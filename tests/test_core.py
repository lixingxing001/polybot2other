from __future__ import annotations

import io
import json
import os
import threading
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from polybot2other.actor_analysis import build_actor_analysis
from polybot2other.clob_ws import (
    ClobMarketOrderBook,
    binance_spot_price_from_payload,
    okx_spot_price_from_payload,
    rtds_chainlink_price_from_payload,
)
from polybot2other.config import Settings, env_file_status, load_settings, reload_live_credential_env
from polybot2other.bot import (
    LiveOnceBlockedError,
    PaperTradingBot,
    PriceBasisTracker,
    StrategyExperimentRunner,
    _experiment_decision_summary,
    _experiment_profit_summary,
    _experiment_review_score,
)
from polybot2other.execution import (
    ORDER_TYPE_FAK,
    STATUS_CANCELED,
    STATUS_FILLED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_REJECTED,
    simulate_fak_buy,
    simulate_post_only_buy,
    taker_fee,
)
from polybot2other.experiments import (
    ANTI_BOT_GUARD_MODE_ENABLED,
    MARKET_DATA_MODE_MULTI_CONFIRM,
    MARKET_DATA_MODE_MULTI_LEAD,
    PRICE_SOURCE_MODE_CHAINLINK_ONLY,
    PRICE_SOURCE_MODE_FALLBACK_ONLY,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC,
    SIGNAL_SIDE_MODE_REVERSE,
    SINGLE_ENTRY_MODE_LEGACY,
    SINGLE_ENTRY_MODE_REVERSAL,
    SINGLE_ENTRY_MODE_STOP_AND_FLIP,
    SINGLE_ENTRY_MODE_STRICT,
    STRATEGY_VARIANTS,
    active_strategy_variants,
    selected_strategy_variants,
)
from polybot2other.live import (
    LIVE_ACTIVE_LOCK_PRESERVE_MESSAGE,
    LIVE_AGGRESSIVE_EDGE_COMBO,
    LIVE_AGGRESSIVE_EDGE_V10_COMBO,
    LIVE_AGGRESSIVE_EDGE_V11_COMBO,
    LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID,
    LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID,
    LIVE_AGGRESSIVE_EDGE_VARIANT_ID,
    LIVE_COMBO,
    LIVE_PAPER_STOP_WIN_VARIANT_ID,
    LIVE_PAPER_VARIANT_ID,
    LIVE_STARTUP_REARM_MESSAGE,
    LIVE_STOP_WIN_COMBO,
    LIVE_STOP_WIN_MARKER,
    LIVE_STOP_WIN_VARIANT_ID,
    LIVE_VARIANT_ID,
    LiveOrderResponse,
    LiveProcessLock,
    PolymarketLiveClient,
    _live_basis_rows,
    _response_terminal_no_fill_local_status,
)
from polybot2other.live_doctor import build_live_doctor_from_bot, main as live_doctor_main
from polybot2other.live_env_setup import LiveEnvSetupValues, validate_live_env_values, write_live_env_file
from polybot2other.live_evidence import build_live_evidence_payload, main as live_evidence_main
from polybot2other.live_once import main as live_once_main
from polybot2other.live_preflight import build_live_preflight_payload, main as live_preflight_main
from polybot2other.models import MarketRound, PaperFill, PaperFillLevel, PriceTick, Signal, TradeIntent
from polybot2other.polymarket import PolymarketClient
from polybot2other.report_snapshot import generate_strategy_experiment_report_snapshot
from polybot2other.storage import (
    SCHEMA_VERSION,
    SETTLEMENT_SOURCE_CHAINLINK,
    SETTLEMENT_SOURCE_EARLY_EXIT,
    SETTLEMENT_SOURCE_POLYMARKET,
    TradeStore,
)
from polybot2other.strategy_memory import (
    append_strategy_memory_entry,
    build_aggressive_edge_memory_entry,
    load_strategy_memory,
)
from polybot2other.strategy import RealBtcFiveMinuteStrategy, input_from_snapshot
from polybot2other.web import DashboardServer, Handler, _strategy_experiments_retrospective_report_html


class FakeLiveClient:
    def __init__(self) -> None:
        self.buy_calls = []
        self.sell_calls = []
        self.buy_response = LiveOrderResponse(
            True,
            "matched",
            "live-buy-1",
            None,
            {"status": "matched", "makingAmount": "4837461", "takingAmount": "9302810"},
            filled_shares=9.30281,
            cash_spent=4.837461,
            avg_fill_price=0.52,
        )
        self.sell_response = LiveOrderResponse(
            True,
            "matched",
            "live-sell-1",
            None,
            {"status": "matched", "makingAmount": "9302810", "takingAmount": "3721124"},
            filled_shares=9.30281,
            cash_spent=3.721124,
            avg_fill_price=0.4,
        )
        self.fetch_order_states = []
        self.fetch_order_calls = []
        self.wallet_calls = []
        self.wallet_payload = {"ready": True, "errors": [], "balance": 100.0, "allowance": 100.0}
        self.readiness_error_list = []
        self.token_calls = []
        self.token_payload = {"ready": True, "errors": [], "balance": 100.0, "allowance": 100.0}
        self.sign_calls = []
        self.sign_payload = {
            "ready": True,
            "status": "SIGNED",
            "errors": [],
            "submitted_to_clob": False,
            "signed_order_hash": "0xfakesigned",
        }
        self.cancel_all_calls = []
        self.cancel_all_payload = {
            "ready": True,
            "errors": [],
            "open_orders_before": [{"id": "open-live-order"}],
            "open_orders_after": [],
            "cancel_response": {"canceled": ["open-live-order"]},
        }
        self.open_orders_calls = []
        self.open_orders_payload = {
            "ready": True,
            "skipped": False,
            "errors": [],
            "orders": [],
            "count": 0,
            "checked_at": time.time(),
        }
        self.geoblock_calls = []
        self.geoblock_payload = {
            "ready": True,
            "blocked": False,
            "country": "KR",
            "region": "11",
            "checked_at": time.time(),
            "errors": [],
            "source": "fake",
        }
        self.credential_presence = {
            "private_key": True,
            "signature_type": True,
            "funder_address": True,
            "api_creds_complete": False,
            "api_creds_partial": False,
        }
        self.env_files = []
        self.fetch_order_hook = None
        self.clear_cached_credentials_calls = 0

    def readiness(self, **kwargs) -> dict:
        wallet = self.wallet_state(required_cash=kwargs.get("required_cash"))
        errors = list(self.readiness_error_list or []) + list(wallet.get("errors", []))
        return {
            "ready": not errors,
            "errors": errors,
            "sdk": "fake",
            "sdk_version": "fake-1.0",
            "sdk_status": {"package": "fake", "version": "fake-1.0", "compatible": True, "errors": []},
            "chain_id": 137,
            "host": "fake",
            "wallet": wallet,
            "credential_presence": dict(self.credential_presence),
            "env_files": list(self.env_files),
        }

    def cached_readiness(self, **kwargs) -> dict:
        wallet = dict(self.wallet_payload)
        wallet["required_cash"] = kwargs.get("required_cash")
        errors = list(self.readiness_error_list or []) + list(wallet.get("errors", []))
        return {
            "ready": not errors,
            "errors": errors,
            "sdk": "fake",
            "sdk_version": "fake-1.0",
            "sdk_status": {"package": "fake", "version": "fake-1.0", "compatible": True, "errors": []},
            "chain_id": 137,
            "host": "fake",
            "wallet": wallet,
            "credential_presence": dict(self.credential_presence),
            "env_files": list(self.env_files),
            "cached": True,
        }

    def readiness_errors(self) -> list[str]:
        return list(self.readiness_error_list)

    def place_market_buy(self, **kwargs) -> LiveOrderResponse:
        self.buy_calls.append(kwargs)
        return self.buy_response

    def place_market_sell(self, **kwargs) -> LiveOrderResponse:
        self.sell_calls.append(kwargs)
        return self.sell_response

    def sign_market_order_preview(self, **kwargs) -> dict:
        self.sign_calls.append(kwargs)
        payload = dict(self.sign_payload)
        payload.update(
            {
                "side": kwargs.get("side"),
                "token_id": kwargs.get("token_id"),
                "amount": kwargs.get("amount"),
                "price": kwargs.get("price"),
                "tick_size": kwargs.get("tick_size"),
                "neg_risk": kwargs.get("neg_risk"),
                "user_usdc_balance": kwargs.get("amount") if kwargs.get("side") == "BUY" else None,
            }
        )
        return payload

    def cancel_all_orders(self, **kwargs) -> dict:
        self.cancel_all_calls.append(kwargs)
        return dict(self.cancel_all_payload)

    def open_orders_state(self, **kwargs) -> dict:
        self.open_orders_calls.append(kwargs)
        payload = dict(self.open_orders_payload)
        payload["orders"] = [dict(row) for row in self.open_orders_payload.get("orders", [])]
        return payload

    def cached_open_orders_state(self) -> dict:
        payload = dict(self.open_orders_payload)
        payload["orders"] = [dict(row) for row in self.open_orders_payload.get("orders", [])]
        payload["cached"] = True
        return payload

    def geoblock_state(self, **kwargs) -> dict:
        self.geoblock_calls.append(kwargs)
        return dict(self.geoblock_payload)

    def fetch_order_state(self, **kwargs) -> LiveOrderResponse | None:
        self.fetch_order_calls.append(kwargs)
        if self.fetch_order_hook is not None:
            self.fetch_order_hook(kwargs)
        if self.fetch_order_states:
            return self.fetch_order_states.pop(0)
        return None

    def wallet_state(self, **kwargs) -> dict:
        self.wallet_calls.append(kwargs)
        payload = dict(self.wallet_payload)
        payload["required_cash"] = kwargs.get("required_cash")
        return payload

    def token_state(self, **kwargs) -> dict:
        self.token_calls.append(kwargs)
        payload = dict(self.token_payload)
        payload["token_id"] = kwargs.get("token_id")
        payload["required_shares"] = kwargs.get("required_shares")
        return payload

    def clear_cached_credentials(self) -> None:
        self.clear_cached_credentials_calls += 1


class FakeActorDataClient:
    def __init__(self, fail_positions: bool = False) -> None:
        self.fail_positions = fail_positions
        self.wallet_a = "0x" + "a" * 40
        self.wallet_b = "0x" + "b" * 40

    def get_market_holders(self, condition_id: str, limit: int = 20) -> list[dict]:
        return [
            {"proxyWallet": self.wallet_a, "asset": "up-token", "amount": 40, "outcome": "Up", "name": "Alpha"},
            {"proxyWallet": self.wallet_b, "asset": "down-token", "amount": 20, "outcome": "Down", "name": "Beta"},
        ]

    def get_market_positions(self, condition_id: str, limit: int = 80) -> list[dict]:
        if self.fail_positions:
            raise RuntimeError("positions unavailable")
        return [
            {
                "proxyWallet": self.wallet_a,
                "asset": "up-token",
                "outcome": "Up",
                "size": 160,
                "currPrice": 0.62,
                "currentValue": 99.2,
                "totalPnl": 8.5,
            },
            {
                "proxyWallet": self.wallet_b,
                "asset": "down-token",
                "outcome": "Down",
                "size": 30,
                "currPrice": 0.38,
                "currentValue": 11.4,
                "totalPnl": -1.2,
            },
        ]

    def get_market_trades(self, condition_id: str, limit: int = 100) -> list[dict]:
        return [
            {
                "proxyWallet": self.wallet_a,
                "asset": "up-token",
                "outcome": "Up",
                "side": "BUY",
                "size": 10,
                "price": 0.58,
                "timestamp": 1_780_000_001,
            },
            {
                "proxyWallet": self.wallet_a,
                "asset": "up-token",
                "outcome": "Up",
                "side": "BUY",
                "size": 11,
                "price": 0.6,
                "timestamp": 1_780_000_002,
            },
            {
                "proxyWallet": self.wallet_a,
                "asset": "up-token",
                "outcome": "Up",
                "side": "BUY",
                "size": 12,
                "price": 0.61,
                "timestamp": 1_780_000_003,
            },
        ]


class TradingCoreTest(unittest.TestCase):
    def _seed_live_aggressive_edge_readiness(self, settings: Settings, *, version: str = "V11", count: int = 80) -> None:
        """为实盘准入测试准备已结算诊断样本，避免测试依赖真实采样库。"""

        ready_store = TradeStore(
            settings.strategy_experiments_db_dir / "single_fak_aggressive_edge_diagnostic.sqlite3",
            100.0,
        )
        seed_now = time.time()
        try:
            for index in range(count):
                side = "Up" if index % 2 == 0 else "Down"
                bucket = 2 if index % 3 else 3
                round_id = f"btc-updown-5m-live-{version.lower()}-ready-{index}"
                ready_store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key=f"m{bucket}:pass",
                    side=side,
                    source_signal_side=side,
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    v6_would_trade=True,
                    v7_would_trade=True,
                    v8_would_trade=True,
                    v9_would_trade=True,
                    v10_would_trade=True,
                    v11_would_trade=True,
                    v12_would_trade=True,
                    entry_price=0.65,
                    confidence=0.82,
                    move_bps=6.5 if side == "Up" else -6.5,
                    report={
                        "risk_score": 0.1,
                        "risk_level": "LOW",
                        "risk_reasons": [],
                        "features": {
                            "entry_price": 0.65,
                            "move_bps": 6.5 if side == "Up" else -6.5,
                            "depth_skew": 0.55,
                            "top_level_skew": 0.60,
                        },
                        "components": {},
                    },
                    base_block_reason=None,
                    v1_block_reason=None,
                    v4_block_reason=None,
                    v5_block_reason=None,
                    v6_block_reason=None,
                    v7_block_reason=None,
                    v8_block_reason=None,
                    v9_block_reason=None,
                    v10_block_reason=None,
                    v11_block_reason=None,
                    v12_block_reason=None,
                    signal_reason=f"{version} REAL 准入测试样本",
                    created_at=seed_now + index,
                )
                ready_store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    side,
                    seed_now + index + 1,
                    final_price=101.0 if side == "Up" else 99.0,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )
        finally:
            ready_store.conn.close()

    def test_trade_store_schema_creates_hot_path_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            index_rows = store.conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'
                """
            ).fetchall()
            indexes = {str(row["name"]) for row in index_rows}

            self.assertTrue(
                {
                    "idx_equity_curve_created_at",
                    "idx_price_ticks_symbol_created_at",
                    "idx_trades_status_opened_at",
                    "idx_trades_symbol_activity_at",
                    "idx_paper_orders_symbol_created_at",
                }.issubset(indexes)
            )

            schema_version = store.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
            self.assertEqual(str(SCHEMA_VERSION), schema_version)

    def test_actor_analysis_is_read_only_and_scores_wallets(self) -> None:
        market = MarketRound(
            "btc-updown-5m-1780000000",
            "BTC",
            1_780_000_000,
            1_780_000_300,
            100_000.0,
            condition_id="0x" + "1" * 64,
            up_token="up-token",
            down_token="down-token",
        )
        analysis = build_actor_analysis(
            market,
            {"chainlink": 100_060.0},
            {
                "Up": {"best_bid": 0.61, "best_ask": 0.63},
                "Down": {"best_bid": 0.37, "best_ask": 0.39},
            },
            FakeActorDataClient(),
            now=1_780_000_120,
        )

        self.assertTrue(analysis["analysis_only"])
        self.assertFalse(analysis["affects_trading"])
        self.assertFalse(analysis["can_identify_orderbook_addresses"])
        self.assertEqual(analysis["status"], "READY")
        self.assertGreaterEqual(analysis["summary"]["wallet_count"], 2)
        self.assertGreater(analysis["probability"]["combined_up"], 0.5)
        top_wallet = analysis["wallets"][0]
        self.assertEqual(top_wallet["bias"], "Up")
        self.assertIn("ACTIVE_CURRENT_MARKET", top_wallet["tags"])
        self.assertTrue(
            any(tag["code"] == "PUBLIC_ORDERBOOK_ADDRESS_UNAVAILABLE" for tag in analysis["risk_tags"])
        )

    def test_actor_analysis_degrades_when_data_api_source_fails(self) -> None:
        market = MarketRound(
            "btc-updown-5m-1780000000",
            "BTC",
            1_780_000_000,
            1_780_000_300,
            100_000.0,
            condition_id="0x" + "1" * 64,
            up_token="up-token",
            down_token="down-token",
        )
        analysis = build_actor_analysis(
            market,
            {"chainlink": 99_980.0},
            {
                "Up": {"best_bid": 0.48, "best_ask": 0.5},
                "Down": {"best_bid": 0.5, "best_ask": 0.52},
            },
            FakeActorDataClient(fail_positions=True),
            now=1_780_000_120,
        )

        self.assertEqual(analysis["status"], "PARTIAL")
        self.assertFalse(analysis["sources"]["positions"]["ok"])
        self.assertIn("positions unavailable", analysis["sources"]["positions"]["error"])
        self.assertTrue(any(tag["code"] == "DATA_PARTIAL" for tag in analysis["risk_tags"]))

    def test_load_settings_reads_local_env_live_without_overriding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.live"
            env_path.write_text(
                "\n".join(
                    [
                        "POLYBOT2OTHER_INITIAL_BALANCE=77",
                        "POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=3.5",
                        "POLYBOT2OTHER_LIVE_PRIVATE_KEY=0x" + "1" * 64,
                        "IGNORED_KEY=should_not_load",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {"POLYBOT2OTHER_INITIAL_BALANCE": "88"}, clear=True):
                    settings = load_settings()

                    self.assertEqual(settings.initial_balance, 88.0)
                    self.assertEqual(settings.live_trading_default_stake_dollars, 3.5)
                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"], "0x" + "1" * 64)
                    self.assertNotIn("IGNORED_KEY", os.environ)
                    status = env_file_status()
                    self.assertEqual(status[0]["path"], ".env.live")
                    self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", status[0]["loaded_keys"])
                    self.assertIn("POLYBOT2OTHER_INITIAL_BALANCE", status[0]["skipped_existing"])
                    self.assertEqual(status[0]["ignored_keys"], 1)
                    self.assertEqual(status[0]["mode"], "0o600")
                    self.assertTrue(status[0]["secure_permissions"])
                    self.assertEqual(status[0]["empty_keys"], [])
                    self.assertEqual(status[0]["sensitive_keys_present"], ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"])
            finally:
                os.chdir(previous_cwd)

    def test_load_settings_blank_env_values_do_not_shadow_later_env_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_path = Path(tmp) / ".env.live"
            local_path = Path(tmp) / ".env.local"
            private_key = "0x" + "2" * 64
            live_path.write_text(
                "POLYBOT2OTHER_LIVE_PRIVATE_KEY=\n"
                "POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=\n",
                encoding="utf-8",
            )
            local_path.write_text(
                f"POLYBOT2OTHER_LIVE_PRIVATE_KEY={private_key}\n"
                "POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=4.25\n",
                encoding="utf-8",
            )
            live_path.chmod(0o600)
            local_path.chmod(0o600)
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {}, clear=True):
                    settings = load_settings()
                    status = env_file_status()

                    self.assertEqual(settings.live_trading_default_stake_dollars, 4.25)
                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"], private_key)
                    self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", status[0]["empty_keys"])
                    self.assertIn("POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS", status[0]["empty_keys"])
                    self.assertEqual(status[0]["sensitive_keys_present"], [])
                    self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", status[1]["loaded_keys"])
                    self.assertEqual(status[1]["sensitive_keys_present"], ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"])
            finally:
                os.chdir(previous_cwd)

    def test_load_settings_reads_explicit_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "live.env"
            env_path.write_text(
                "export POLYBOT2OTHER_LIVE_DEFAULT_RETRY_COUNT=4\n"
                "POLYBOT2OTHER_LIVE_DEFAULT_RETRY_DELAY_MS='750'\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"POLYBOT2OTHER_ENV_FILE": str(env_path)}, clear=True):
                settings = load_settings()

                self.assertEqual(settings.live_trading_default_retry_count, 4)
                self.assertEqual(settings.live_trading_default_retry_delay_ms, 750)

    def test_reload_live_credential_env_refreshes_env_file_credentials_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.live"
            first_private_key = "0x" + "1" * 64
            second_private_key = "0x" + "2" * 64
            env_path.write_text(
                f"POLYBOT2OTHER_LIVE_PRIVATE_KEY={first_private_key}\n"
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=3\n"
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=0x" + "a" * 40 + "\n"
                "POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=3.25\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {}, clear=True):
                    settings = load_settings()
                    self.assertEqual(settings.live_trading_default_stake_dollars, 3.25)
                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"], first_private_key)

                    env_path.write_text(
                        f"POLYBOT2OTHER_LIVE_PRIVATE_KEY={second_private_key}\n"
                        "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=3\n"
                        "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=0x" + "b" * 40 + "\n"
                        "POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=9.99\n",
                        encoding="utf-8",
                    )
                    status = reload_live_credential_env()

                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"], second_private_key)
                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_SIGNATURE_TYPE"], "3")
                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_FUNDER_ADDRESS"], "0x" + "b" * 40)
                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS"], "3.25")
                    self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", status[0]["loaded_keys"])
                    self.assertIn("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS", status[0]["loaded_keys"])
                    self.assertIn("POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS", status[0]["skipped_existing"])
            finally:
                os.chdir(previous_cwd)

    def test_live_env_setup_writes_secure_env_file_without_changing_non_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.live"
            env_path.write_text(
                "POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=2\n"
                "POLYBOT2OTHER_LIVE_PRIVATE_KEY=\n"
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=\n"
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=\n",
                encoding="utf-8",
            )
            values = LiveEnvSetupValues(
                private_key="0x" + "1" * 64,
                signature_type="3",
                funder_address="0x" + "2" * 40,
            )

            result = write_live_env_file(env_path, values)
            text = env_path.read_text(encoding="utf-8")
            mode = env_path.stat().st_mode & 0o777

            self.assertEqual(mode, 0o600)
            self.assertEqual(result["api_credentials_mode"], "derive_api_creds_with_private_key")
            self.assertIn("POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=2", text)
            self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY=0x" + "1" * 64, text)
            self.assertIn("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=3", text)
            self.assertIn("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=0x" + "2" * 40, text)

    def test_live_env_setup_rejects_partial_api_credentials(self) -> None:
        values = LiveEnvSetupValues(
            private_key="0x" + "1" * 64,
            signature_type="3",
            funder_address="0x" + "2" * 40,
            api_key="key",
            api_secret="",
            api_passphrase="passphrase",
        )

        errors = validate_live_env_values(values)

        self.assertTrue(any("all filled" in item for item in errors))

    def test_load_settings_can_disable_live_runtime_from_environment(self) -> None:
        with patch.dict("os.environ", {"POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED": "false"}, clear=True):
            settings = load_settings()

            self.assertFalse(settings.live_trading_runtime_enabled)

    def test_disabled_live_runtime_does_not_create_live_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "paper.sqlite3",
                live_trading_runtime_enabled=False,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))

            self.assertIsNone(bot.live_trading)
            payload = bot.live_settings()
            self.assertFalse(payload["enabled"])
            self.assertFalse(payload["readiness"]["ready"])
            self.assertIn("live trading disabled", payload["readiness"]["errors"][0])
            with self.assertRaisesRegex(ValueError, "live trading is disabled"):
                bot.set_live_enabled(True)

    def test_initial_balance_defaults_to_100(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            metrics = store.metrics()
            self.assertEqual(metrics["initial_balance"], 100.0)
            self.assertEqual(metrics["cash_balance"], 100.0)
            self.assertEqual(metrics["total_pnl"], 0.0)

    def test_trade_settlement_updates_pnl_with_real_market_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-1000",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test")
            trade_id = store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            self.assertGreater(trade_id, 0)
            settled = store.settle_round_outcome(market.round_id, "Up", now)
            self.assertEqual(len(settled), 1)
            metrics = store.metrics()
            self.assertEqual(metrics["settled_trades"], 1)
            self.assertGreater(metrics["realized_pnl"], 0)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Up")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertIsNone(recent[0]["final_price"])

    def test_llm_decision_review_estimates_no_trade_and_attributes_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-llm-review",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 240,
                target_price=100.0,
            )
            features = {
                "round_id": market.round_id,
                "direction_side": "Up",
                "time_left_seconds": 80,
                "signed_distance_bps": 12.5,
                "up": {"ask": 0.4},
                "down": {"ask": 0.62},
            }
            store.upsert_round(market)
            store.record_llm_decision(
                round_id=market.round_id,
                variant_id="MAIN",
                decision={
                    "route": "NO_TRADE",
                    "allow_trade": False,
                    "confidence": 0.9,
                    "market_regime": "NEAR_TARGET_NOISY",
                    "source": "llm",
                    "reason": "wait",
                    "reason_codes": ["NEAR_TARGET"],
                    "valid_until": now + 20,
                },
                features=features,
                created_at=now,
            )
            store.record_llm_decision(
                round_id=market.round_id,
                variant_id="MAIN",
                decision={
                    "route": "SINGLE_FAK",
                    "allow_trade": True,
                    "confidence": 0.8,
                    "market_regime": "TREND",
                    "source": "llm",
                    "reason": "enter",
                    "reason_codes": ["EDGE_OK"],
                    "valid_until": now + 20,
                },
                features=features,
                created_at=now + 0.1,
            )
            signal = Signal(
                "BTC",
                "Up",
                0.8,
                0.5,
                12.5,
                "LLM_SUPER_AGENT route SINGLE_FAK, source llm, regime TREND, conf 0.8, allow True, codes EDGE_OK",
            )
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_round_outcome(market.round_id, "Up", now + 20)

            review = store.llm_decision_review(
                limit=10,
                opportunity_stake=5.0,
                variant_id="MAIN",
            )

            self.assertEqual(review["status"], "READY")
            self.assertEqual(review["summary"]["decision_count"], 2)
            self.assertEqual(review["summary"]["settled_trade_count"], 1)
            self.assertAlmostEqual(review["summary"]["total_pnl"], 5.0, places=6)
            self.assertAlmostEqual(review["summary"]["no_trade_direction_estimated_pnl"], 7.5, places=6)
            routes = {row["key"]: row for row in review["route_stats"]}
            self.assertAlmostEqual(routes["SINGLE_FAK"]["total_pnl"], 5.0, places=6)
            self.assertAlmostEqual(routes["NO_TRADE"]["no_trade_direction_estimated_pnl"], 7.5, places=6)
            reasons = {row["key"]: row for row in review["reason_stats"]}
            self.assertAlmostEqual(reasons["NEAR_TARGET"]["no_trade_direction_estimated_pnl"], 7.5, places=6)
            self.assertEqual(review["recent_decisions"][0]["route"], "SINGLE_FAK")
            self.assertEqual(review["recent_decisions"][0]["matched_settled_trade_count"], 1)

    def test_chainlink_fallback_settlement_records_source_and_final_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-chainlink-source",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "chainlink fallback")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.save_price_tick("BTC", 99.5, "test-chainlink", market.ends_at)

            settled = store.settle_due_rounds({"BTC": 99.5}, now)

            self.assertEqual(len(settled), 1)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Down")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_CHAINLINK)
            self.assertAlmostEqual(recent[0]["final_price"], 99.5, places=6)

    def test_chainlink_fallback_settlement_uses_end_time_tick_not_late_latest_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-chainlink-stale",
                symbol="BTC",
                started_at=now - 360,
                ends_at=now - 70,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "stale chainlink fallback")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.save_price_tick("BTC", 101.0, "test-chainlink", now)

            self.assertEqual(store.settle_due_rounds({"BTC": 101.0}, now), [])
            self.assertEqual(len(store.open_trades()), 1)
            row = store.conn.execute("SELECT settled_at, outcome, final_price FROM market_rounds WHERE round_id = ?", (market.round_id,)).fetchone()
            self.assertIsNone(row["settled_at"])
            self.assertIsNone(row["outcome"])
            self.assertIsNone(row["final_price"])

            store.save_price_tick("BTC", 99.5, "test-chainlink", market.ends_at + 2.0)
            settled = store.settle_due_rounds({"BTC": 101.0}, now + 1)

            self.assertEqual(len(settled), 1)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Down")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_CHAINLINK)
            self.assertAlmostEqual(recent[0]["final_price"], 99.5, places=6)

    def test_official_recheck_upgrades_matching_chainlink_fallback_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-official-match",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official match")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.save_price_tick("BTC", 99.5, "test-chainlink", market.ends_at)
            store.settle_due_rounds({"BTC": 99.5}, now)
            before = store.account()

            result = store.reconcile_round_official_outcome(market.round_id, "Down", now + 10)

            self.assertFalse(result["corrected"])
            self.assertEqual(result["updated_trades"], 1)
            self.assertAlmostEqual(result["cash_delta"], 0.0, places=6)
            self.assertAlmostEqual(result["pnl_delta"], 0.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], before["cash_balance"], places=6)
            self.assertAlmostEqual(store.account()["realized_pnl"], before["realized_pnl"], places=6)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Down")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertEqual(recent[0]["trade_settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertIsNone(recent[0]["final_price"])

    def test_official_recheck_corrects_mismatched_chainlink_fallback_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-official-mismatch",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official mismatch")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.save_price_tick("BTC", 99.5, "test-chainlink", market.ends_at)
            store.settle_due_rounds({"BTC": 99.5}, now)
            self.assertAlmostEqual(store.account()["cash_balance"], 105.0, places=6)
            self.assertAlmostEqual(store.account()["realized_pnl"], 5.0, places=6)

            result = store.reconcile_round_official_outcome(market.round_id, "Up", now + 10)

            self.assertTrue(result["corrected"])
            self.assertEqual(result["previous_outcome"], "Down")
            self.assertEqual(result["official_outcome"], "Up")
            self.assertEqual(result["updated_trades"], 1)
            self.assertAlmostEqual(result["cash_delta"], -10.0, places=6)
            self.assertAlmostEqual(result["pnl_delta"], -10.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            self.assertAlmostEqual(store.account()["realized_pnl"], -5.0, places=6)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Up")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertEqual(recent[0]["payout"], 0.0)
            self.assertEqual(recent[0]["pnl"], -5.0)
            self.assertIsNone(recent[0]["final_price"])
            self.assertIn("OFFICIAL_RECONCILE Down->Up", recent[0]["reason"])

    def test_official_candidates_ignore_synthetic_round_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            real_round_id = f"btc-updown-5m-{int(now) - 300:010d}"
            synthetic_round_id = "btc-updown-5m-single-aggressive-pass"
            real_market = MarketRound(real_round_id, "BTC", now - 360, now - 60, 100.0)
            synthetic_market = MarketRound(synthetic_round_id, "BTC", now - 360, now - 60, 100.0)
            store.upsert_round(real_market)
            store.upsert_round(synthetic_market)
            store.conn.executemany(
                """
                UPDATE market_rounds
                SET settled_at = ?, outcome = 'Up', final_price = ?, settlement_source = ?
                WHERE round_id = ?
                """,
                [
                    (now, 101.0, SETTLEMENT_SOURCE_CHAINLINK, real_round_id),
                    (now, 101.0, SETTLEMENT_SOURCE_CHAINLINK, synthetic_round_id),
                ],
            )

            recheck = store.official_recheck_candidates(now, 24 * 60 * 60, limit=10)

            self.assertEqual([row["round_id"] for row in recheck], [real_round_id])

            store.conn.executemany(
                """
                UPDATE market_rounds
                SET final_price = target_price, settlement_source = ?
                WHERE round_id = ?
                """,
                [
                    (SETTLEMENT_SOURCE_POLYMARKET, real_round_id),
                    (SETTLEMENT_SOURCE_POLYMARKET, synthetic_round_id),
                ],
            )

            backfill = store.official_final_price_candidates(now, 24 * 60 * 60, limit=10)

            self.assertEqual([row["round_id"] for row in backfill], [real_round_id])

    def test_bot_rechecks_fallback_settlement_until_official_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id=f"btc-updown-5m-{int(now) - 300:010d}",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "bot official recheck")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.save_price_tick("BTC", 99.5, "test-chainlink", market.ends_at)
            store.settle_due_rounds({"BTC": 99.5}, now)
            bot.polymarket.get_resolution = lambda slug: {"outcome": "Up"} if slug == market.round_id else None

            bot._reconcile_official_settlements(now + 10)

            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["outcome"], "Up")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)

    def test_bot_broadcasts_official_resolution_to_strategy_experiments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id=f"btc-updown-5m-{int(now) - 600:010d}",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official broadcast")
            store.upsert_round(market)
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.save_price_tick("BTC", 99.5, "test-chainlink", market.ends_at)
            store.settle_due_rounds({"BTC": 99.5}, now)

            variant_bot = bot.strategy_experiments._bots["SINGLE_FAK"]
            variant_bot.store.upsert_round(market)
            variant_bot.store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            variant_bot.store.save_price_tick("BTC", 99.5, "test-chainlink", market.ends_at)
            variant_bot.store.settle_due_rounds({"BTC": 99.5}, now)
            bot.polymarket.get_resolution = (
                lambda slug: {"outcome": "Up", "final_price": 101.0, "target_price": 100.0}
                if slug == market.round_id
                else None
            )

            bot._reconcile_official_settlements(now + 10)

            main_recent = store.recent_trades(1)
            self.assertEqual(main_recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            variant_detail = bot.strategy_experiment_detail("SINGLE_FAK", trade_limit=5, order_limit=5)
            variant_recent = variant_detail["recent_trades_page"]["recent_trades"]
            self.assertEqual(variant_recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(variant_bot.store.account()["cash_balance"], 95.0, places=6)
            self.assertEqual(variant_detail["variant"]["recent_trades_summary"]["official_count"], 1)
            self.assertEqual(variant_detail["variant"]["recent_trades_summary"]["chainlink_count"], 0)
            self.assertEqual(bot.strategy_experiments.snapshot()["official_broadcast_count"], 1)

    def test_strategy_experiments_settle_pending_open_trades_from_official_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            ended_market = MarketRound(
                round_id="btc-updown-5m-experiment-official-pending",
                symbol="BTC",
                started_at=now - 601,
                ends_at=now - 301,
                target_price=100.0,
            )
            current_market = MarketRound(
                round_id="btc-updown-5m-experiment-current",
                symbol="BTC",
                started_at=now,
                ends_at=now + 300,
                target_price=101.0,
            )
            variant_bot = bot.strategy_experiments._bots["SINGLE_FAK"]
            variant_bot.store.upsert_round(ended_market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "experiment official pending")
            variant_bot.store.place_trade(
                type("Intent", (), {"market": ended_market, "signal": signal, "stake_dollars": 5.0})()
            )
            self.assertEqual(len(variant_bot.store.open_trades()), 1)
            calls: list[str] = []

            def fake_resolution(slug: str) -> dict[str, Any] | None:
                calls.append(slug)
                if slug == ended_market.round_id:
                    return {"outcome": "Up", "final_price": 101.0, "target_price": 100.0}
                return None

            bot.polymarket.get_resolution = fake_resolution

            bot.strategy_experiments.run_from_state(current_market, {"chainlink": 101.0}, {})

            self.assertEqual(calls, [ended_market.round_id])
            self.assertEqual(variant_bot.store.open_trades(), [])
            recent = variant_bot.store.recent_trades(1)
            self.assertEqual(recent[0]["round_id"], ended_market.round_id)
            self.assertEqual(recent[0]["status"], "SETTLED")
            self.assertEqual(recent[0]["outcome"], "Up")
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(recent[0]["payout"], 0.0, places=6)
            self.assertAlmostEqual(recent[0]["pnl"], -5.0, places=6)
            self.assertEqual(bot.strategy_experiments.snapshot()["official_broadcast_count"], 1)

    def test_strategy_experiments_settle_shadow_only_round_from_official_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC",
            )
            calls: list[str] = []

            class FakePolymarket:
                def get_resolution(self, slug: str) -> dict[str, Any] | None:
                    calls.append(slug)
                    if slug == ended_market.round_id:
                        return {"outcome": "Up", "final_price": 101.0, "target_price": 100.0}
                    return None

            now = time.time()
            ended_market = MarketRound(
                round_id="btc-updown-5m-shadow-official-pending",
                symbol="BTC",
                started_at=now - 601,
                ends_at=now - 301,
                target_price=100.0,
            )
            runner = StrategyExperimentRunner(settings, FakePolymarket(), object())
            variant_bot = runner._bots["SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC"]
            variant_bot.store.upsert_round(ended_market)
            variant_bot.store.record_aggressive_edge_v2_shadow_sample(
                round_id=ended_market.round_id,
                symbol="BTC",
                sample_key="m2:pass",
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
                report={
                    "risk_score": 0.1,
                    "risk_level": "LOW",
                    "risk_reasons": [],
                    "features": {},
                    "components": {},
                },
                base_block_reason=None,
                v1_block_reason=None,
                v4_block_reason=None,
                v5_block_reason=None,
                signal_reason="shadow only official pending",
                created_at=now - 200,
            )

            runner._settle_pending_official_rounds(tuple(runner.variants), now)

            self.assertEqual(calls, [ended_market.round_id])
            round_row = variant_bot.store.get_round(ended_market.round_id)
            self.assertEqual(round_row["outcome"], "Up")
            self.assertEqual(round_row["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            summary = variant_bot.store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["v5_would_trade_settled_count"], 1)
            self.assertEqual(summary["v5_would_win_count"], 1)
            self.assertEqual(summary["v5_would_loss_count"], 0)
            self.assertEqual(runner.snapshot()["official_broadcast_count"], 1)

    def test_bot_records_official_resolution_final_and_target_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-bot-official-price",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=99.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "bot official price")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            bot.polymarket.get_resolution = (
                lambda slug: {"outcome": "Up", "final_price": 101.25, "target_price": 100.5}
                if slug == market.round_id
                else None
            )

            bot._settle_due(now)

            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(recent[0]["final_price"], 101.25, places=6)
            self.assertAlmostEqual(recent[0]["target_price"], 100.5, places=6)

    def test_bot_backfills_missing_official_final_price_once_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id=f"btc-updown-5m-{int(now) - 900:010d}",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=99.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official backfill")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_round_outcome(
                market.round_id,
                "Down",
                now,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            bot.polymarket.get_resolution = (
                lambda slug: {"outcome": "Down", "final_price": 98.75, "target_price": 100.5}
                if slug == market.round_id
                else None
            )

            bot._backfill_official_final_prices(now + 60)

            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(recent[0]["final_price"], 98.75, places=6)
            self.assertAlmostEqual(recent[0]["target_price"], 100.5, places=6)

    def test_bot_backfills_suspicious_official_final_price_equal_to_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id=f"btc-updown-5m-{int(now) - 1200:010d}",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "official equal target backfill")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_round_outcome(
                market.round_id,
                "Up",
                now,
                final_price=100.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            bot.polymarket.get_resolution = (
                lambda slug: {"outcome": "Up", "final_price": 101.25, "target_price": 100.0}
                if slug == market.round_id
                else None
            )

            bot._backfill_official_final_prices(now + 60)

            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_POLYMARKET)
            self.assertAlmostEqual(recent[0]["final_price"], 101.25, places=6)
            self.assertAlmostEqual(recent[0]["target_price"], 100.0, places=6)

    def test_partial_close_keeps_account_and_open_position_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-2000",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test partial close")
            trade_id = store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})())
            closed = store.close_trade_shares(trade_id, 10.0, 0.25, now + 1, "test stop")
            self.assertIsNotNone(closed)
            self.assertEqual(closed["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            open_rows = store.open_trades()
            self.assertEqual(len(open_rows), 1)
            self.assertAlmostEqual(open_rows[0]["shares"], 10.0, places=5)
            self.assertAlmostEqual(open_rows[0]["stake"], 5.0, places=5)
            metrics = store.metrics()
            self.assertAlmostEqual(metrics["cash_balance"], 92.5, places=5)
            self.assertAlmostEqual(metrics["realized_pnl"], -2.5, places=5)
            self.assertAlmostEqual(metrics["open_risk"], 5.0, places=5)
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            store.settle_round_outcome(market.round_id, "Up", now + 2)
            recent = store.recent_trades(2)
            early_exit = next(row for row in recent if row["id"] == closed["id"])
            self.assertEqual(early_exit["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            metrics = store.metrics()
            self.assertAlmostEqual(metrics["cash_balance"], 102.5, places=5)
            self.assertAlmostEqual(metrics["realized_pnl"], 2.5, places=5)
            self.assertAlmostEqual(metrics["open_risk"], 0.0, places=5)

    def test_partial_close_dust_remainder_settles_without_open_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-dust-close",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test dust close")
            trade_id = store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})())

            closed = store.close_trade_shares(trade_id, 19.99, 0.25, now + 1, "test dust sell")

            self.assertIsNotNone(closed)
            self.assertEqual(store.open_trades(), [])
            self.assertAlmostEqual(closed["stake"], 10.0, places=6)
            self.assertAlmostEqual(closed["shares"], 19.99, places=6)
            self.assertAlmostEqual(closed["payout"], 4.9975, places=6)
            self.assertAlmostEqual(closed["pnl"], -5.0025, places=6)
            self.assertEqual(closed["remaining_stake"], 0.0)
            self.assertEqual(closed["remaining_shares"], 0.0)
            self.assertIn("DUST_CLOSE", closed["reason"])
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["id"], trade_id)
            self.assertEqual(recent[0]["status"], "SETTLED")
            self.assertIn("DUST_CLOSE", recent[0]["reason"])
            metrics = store.metrics()
            self.assertAlmostEqual(metrics["cash_balance"], 94.9975, places=6)
            self.assertAlmostEqual(metrics["realized_pnl"], -5.0025, places=6)
            self.assertAlmostEqual(metrics["open_risk"], 0.0, places=6)

    def test_recent_trades_supports_count_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            for index in range(3):
                market = MarketRound(
                    round_id=f"btc-updown-5m-page-{index}",
                    symbol="BTC",
                    started_at=now - 60 + index,
                    ends_at=now + 120 + index,
                    target_price=100.0,
                )
                store.upsert_round(market)
                signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, f"test page {index}")
                store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 1.0})())
            self.assertEqual(store.recent_trade_count("BTC"), 3)
            first_page = store.recent_trades(limit=2, offset=0, symbol="BTC")
            second_page = store.recent_trades(limit=2, offset=2, symbol="BTC")
            self.assertEqual(len(first_page), 2)
            self.assertEqual(len(second_page), 1)
            first_ids = {row["id"] for row in first_page}
            second_ids = {row["id"] for row in second_page}
            self.assertFalse(first_ids & second_ids)

    def test_recent_trades_time_range_summary_uses_full_range_not_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            base = 1_800_000_000.0
            for index, side in enumerate(("Up", "Down", "Up")):
                market = MarketRound(
                    round_id=f"btc-updown-5m-range-{index}",
                    symbol="BTC",
                    started_at=base - 60 + index,
                    ends_at=base + index,
                    target_price=100.0,
                )
                store.upsert_round(market)
                signal = Signal("BTC", side, 0.7, 0.5, 10.0, f"test range {index}")
                store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
                outcome = "Up" if index != 1 else "Up"
                store.settle_round_outcome(market.round_id, outcome, base + index * 60)

            start_at = base - 1
            end_at = base + 61
            page = store.recent_trades(limit=1, offset=0, symbol="BTC", start_at=start_at, end_at=end_at)
            summary = store.recent_trade_summary("BTC", start_at, end_at)

            self.assertEqual(len(page), 1)
            self.assertEqual(store.recent_trade_count("BTC", start_at, end_at), 2)
            self.assertEqual(summary["total_count"], 2)
            self.assertEqual(summary["settled_count"], 2)
            self.assertEqual(summary["win_count"], 1)
            self.assertEqual(summary["loss_count"], 1)
            self.assertAlmostEqual(summary["settled_stake"], 10.0, places=6)
            self.assertAlmostEqual(summary["total_payout"], 10.0, places=6)
            self.assertAlmostEqual(summary["total_pnl"], 0.0, places=6)
            self.assertAlmostEqual(summary["roi_pct"], 0.0, places=6)

    def test_equity_curve_window_filters_and_downsamples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = 2_000_000.0
            start = now - 90 * 24 * 60 * 60
            rows = [
                (100.0, 0.0, 0.0, 100.0, start - 1),
                (101.0, 0.0, 1.0, 101.0, start + 1),
                (102.0, 0.0, 2.0, 102.0, start + 2),
                (103.0, 0.0, 3.0, 103.0, start + 3),
                (104.0, 0.0, 4.0, 104.0, start + 4),
                (105.0, 0.0, 5.0, 105.0, start + 5),
                (106.0, 0.0, 6.0, 106.0, now),
                (107.0, 0.0, 7.0, 107.0, now + 1),
            ]
            with store.conn:
                store.conn.executemany(
                    """
                    INSERT INTO equity_curve(cash_balance, open_risk, realized_pnl, total_equity, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            curve = store.equity_curve_window(days=90, max_points=4, now=now)
            self.assertLessEqual(len(curve), 4)
            self.assertEqual(curve[0]["created_at"], start + 1)
            self.assertEqual(curve[-1]["created_at"], now)
            self.assertTrue(all(start <= row["created_at"] <= now for row in curve))
            self.assertEqual(curve[-1]["total_pnl"], 6.0)

    def test_bot_equity_curve_window_supports_strategy_experiment_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            variant_bot = bot.strategy_experiments._bots["SINGLE_FAK"]
            now = time.time()
            with variant_bot.store.conn:
                variant_bot.store.conn.execute(
                    """
                    INSERT INTO equity_curve(cash_balance, open_risk, realized_pnl, total_equity, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (105.0, 0.0, 5.0, 105.0, now),
                )

            main_curve = bot.equity_curve_window(account_scope="main", days=90, max_points=10)
            variant_curve = bot.equity_curve_window(
                account_scope="strategy_experiment",
                variant_id="SINGLE_FAK",
                days=90,
                max_points=10,
            )

            self.assertEqual(main_curve["equity_curve_meta"]["account_scope"], "main")
            self.assertEqual(main_curve["equity_curve_meta"]["label"], "主账户")
            self.assertEqual(variant_curve["equity_curve_meta"]["account_scope"], "strategy_experiment")
            self.assertEqual(variant_curve["equity_curve_meta"]["variant_id"], "SINGLE_FAK")
            self.assertEqual(variant_curve["equity_curve_meta"]["combo"], "SINGLE + FAK")
            self.assertTrue(any(row["total_equity"] == 105.0 for row in variant_curve["equity_curve"]))
            with self.assertRaises(ValueError):
                bot.equity_curve_window(account_scope="strategy_experiment", variant_id="bad-variant")
            with self.assertRaises(ValueError):
                bot.equity_curve_window(account_scope="bad-scope")

    def test_snapshot_keeps_unrealized_pnl_out_of_total_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-unrealized",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test unrealized")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})())
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.2, "best_ask": 0.21, "updated_at_ms": int(now * 1000)},
                }
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}

            metrics = bot.snapshot()["metrics"]
            self.assertAlmostEqual(metrics["cash_balance"], 90.0, places=5)
            self.assertAlmostEqual(metrics["open_risk"], 10.0, places=5)
            self.assertAlmostEqual(metrics["total_equity"], 100.0, places=5)
            self.assertAlmostEqual(metrics["total_pnl"], 0.0, places=5)
            self.assertAlmostEqual(metrics["unrealized_pnl"], -6.0, places=5)
            self.assertAlmostEqual(metrics["open_mark_value"], 4.0, places=5)
            self.assertAlmostEqual(metrics["estimated_total_equity"], 94.0, places=5)
            self.assertAlmostEqual(metrics["estimated_total_pnl"], -6.0, places=5)

    def test_expired_open_trade_is_marked_pending_settlement_without_current_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            expired_market = MarketRound(
                round_id="btc-updown-5m-pending-settlement",
                symbol="BTC",
                started_at=now - 360,
                ends_at=now - 60,
                target_price=100.0,
            )
            current_market = MarketRound(
                round_id="btc-updown-5m-current-after-pending",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 240,
                target_price=101.0,
            )
            store.upsert_round(expired_market)
            store.upsert_round(current_market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test pending settlement")
            store.place_trade(type("Intent", (), {"market": expired_market, "signal": signal, "stake_dollars": 10.0})())
            with bot._lock:
                bot.current_market = current_market
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.2, "best_ask": 0.21, "updated_at_ms": int(now * 1000)},
                }
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}

            snapshot = bot.snapshot()
            open_trade = snapshot["open_trades"][0]
            self.assertTrue(open_trade["settlement_pending"])
            self.assertEqual(open_trade["position_state"], "PENDING_SETTLEMENT")
            self.assertEqual(open_trade["position_state_label"], "等待官方结算")
            self.assertIsNone(open_trade.get("current_price"))
            self.assertIsNone(open_trade.get("current_bid"))
            self.assertIsNone(open_trade.get("current_distance_bps"))

            recent = bot.recent_trades_page(5, 0)["recent_trades"][0]
            self.assertTrue(recent["settlement_pending"])
            self.assertEqual(recent["status_display"], "PENDING_SETTLEMENT")
            self.assertEqual(recent["settlement_source_label"], "等待官方结算")

    def test_fak_execution_charges_fee_and_caps_by_top_ask_size(self) -> None:
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-fak",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 120,
            target_price=100.0,
        )
        signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "test fak")
        intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})()

        result = simulate_fak_buy(intent, {"best_ask": 0.5, "ask_size": 5.0}, taker_fee_rate=0.07)

        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        self.assertAlmostEqual(fill.shares, 5.0, places=6)
        self.assertAlmostEqual(fill.notional, 2.5, places=6)
        self.assertAlmostEqual(fill.fee, 0.0875, places=6)
        self.assertAlmostEqual(fill.cash_spent, 2.5875, places=6)

    def test_fak_execution_walks_multiple_ask_levels_when_limit_allows(self) -> None:
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-fak-depth",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 120,
            target_price=100.0,
        )
        signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "test fak depth")
        intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()

        result = simulate_fak_buy(
            intent,
            {
                "best_ask": 0.34,
                "ask_size": 5.0,
                "asks": [
                    {"price": 0.34, "size": 5.0},
                    {"price": 0.45, "size": 100.0},
                ],
            },
            taker_fee_rate=0.07,
            limit_price=0.45,
        )

        self.assertEqual(result.status, STATUS_FILLED)
        fill = result.fills[0]
        self.assertGreater(fill.shares, 5.0)
        self.assertGreater(fill.fill_price, 0.34)
        self.assertLessEqual(fill.cash_spent, 5.000001)
        self.assertIn("levels 2", fill.reason)
        self.assertIn("avg", fill.reason)

    def test_post_only_marketable_order_rejects_instead_of_taking_liquidity(self) -> None:
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-post-only",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 120,
            target_price=100.0,
        )
        signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "test post only")
        intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 10.0})()

        result = simulate_post_only_buy(intent, {"best_ask": 0.34, "ask_size": 100})

        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.fills, [])
        self.assertIn("会立即吃到卖一", result.reason)

    def test_bot_fak_entry_records_fee_in_open_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
                live_trading_runtime_enabled=False,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-fak-bot",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                }

            bot._run_strategy_from_state()

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["side"], "Up")
            self.assertAlmostEqual(row["stake"], 5.0, places=5)
            self.assertLess(row["shares"], 5.0 / 0.54)
            self.assertIn("FAK FILLED", row["reason"])
            self.assertIn("fee", row["reason"])

    def test_bot_fak_entry_uses_multi_level_average_with_edge_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_edge=0.0, max_entry_price=0.8, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-depth-bot",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "manual depth signal")
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.34,
                        "ask_size": 1.0,
                        "asks": [
                            {"price": 0.34, "size": 1.0},
                            {"price": 0.45, "size": 100.0},
                        ],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertGreater(rows[0]["entry_price"], 0.34)
            self.assertIn("levels 2", rows[0]["reason"])
            self.assertIn("limit 0.7000", rows[0]["reason"])

    def test_single_fak_legacy_allows_implicit_opposite_side_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_edge=0.0,
                max_entry_price=0.8,
                max_open_trades=2,
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.single_entry_mode = SINGLE_ENTRY_MODE_LEGACY
            now = time.time()
            market = MarketRound("btc-updown-5m-single-legacy", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.39, "best_ask": 0.4, "asks": [{"price": 0.4, "size": 100}]},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "asks": [{"price": 0.5, "size": 100}]},
                }

            bot._maybe_place_trade(market, Signal("BTC", "Up", 0.7, 0.4, 10.0, "legacy first"))
            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.5, -10.0, "legacy flip"))

            rows = sorted(store.open_trades(), key=lambda row: row["side"])
            self.assertEqual([row["side"] for row in rows], ["Down", "Up"])
            self.assertFalse(any("SINGLE_REVERSAL" in row["reason"] for row in rows))

    def test_single_fak_strict_blocks_opposite_side_entry_for_same_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_edge=0.0,
                max_entry_price=0.8,
                max_open_trades=2,
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.single_entry_mode = SINGLE_ENTRY_MODE_STRICT
            now = time.time()
            market = MarketRound("btc-updown-5m-single-strict", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.39, "best_ask": 0.4, "asks": [{"price": 0.4, "size": 100}]},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "asks": [{"price": 0.5, "size": 100}]},
                }

            bot._maybe_place_trade(market, Signal("BTC", "Up", 0.7, 0.4, 10.0, "strict first"))
            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.5, -10.0, "strict blocked"))

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "Up")
            self.assertIn("SINGLE_STRICT", bot.last_signal["reason"])

    def test_single_fak_reversal_marks_opposite_side_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_edge=0.0,
                max_entry_price=0.8,
                max_open_trades=2,
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.single_entry_mode = SINGLE_ENTRY_MODE_REVERSAL
            now = time.time()
            market = MarketRound("btc-updown-5m-single-reversal", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.39, "best_ask": 0.4, "asks": [{"price": 0.4, "size": 100}]},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "asks": [{"price": 0.5, "size": 100}]},
                }

            bot._maybe_place_trade(market, Signal("BTC", "Up", 0.7, 0.4, 10.0, "reversal first"))
            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.5, -10.0, "reversal flip"))

            rows = sorted(store.open_trades(), key=lambda row: row["side"])
            self.assertEqual([row["side"] for row in rows], ["Down", "Up"])
            down = next(row for row in rows if row["side"] == "Down")
            self.assertIn("SINGLE_REVERSAL", down["reason"])
            summary = store.trade_reason_summary("SINGLE_REVERSAL", "BTC")
            self.assertEqual(summary["total_count"], 1)
            self.assertEqual(summary["open_count"], 1)

    def test_single_fak_reverse_flips_up_signal_to_down_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_side_mode = SIGNAL_SIDE_MODE_REVERSE
            now = time.time()
            market = MarketRound("btc-updown-5m-single-reverse-up", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }

            bot._run_strategy_from_state()

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "Down")
            self.assertAlmostEqual(rows[0]["entry_price"], 0.5)
            self.assertLess(rows[0]["confidence"], 0.5)
            self.assertIn("SINGLE_REVERSE", rows[0]["reason"])
            self.assertIn("原始信号 Up->反向下单 Down", rows[0]["reason"])
            self.assertEqual(bot.last_signal["side"], "Down")
            self.assertIn("反向入场价 0.5000", bot.last_signal["reason"])

    def test_single_fak_reverse_flips_down_signal_to_up_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_side_mode = SIGNAL_SIDE_MODE_REVERSE
            now = time.time()
            market = MarketRound("btc-updown-5m-single-reverse-down", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 99.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }

            bot._run_strategy_from_state()

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "Up")
            self.assertAlmostEqual(rows[0]["entry_price"], 0.5)
            self.assertLess(rows[0]["confidence"], 0.5)
            self.assertIn("SINGLE_REVERSE", rows[0]["reason"])
            self.assertIn("原始信号 Down->反向下单 Up", rows[0]["reason"])
            self.assertEqual(bot.last_signal["side"], "Up")
            self.assertIn("反向入场价 0.5000", bot.last_signal["reason"])

    def test_single_fak_aggressive_edge_allows_low_entry_high_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-pass", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 101.02,
                    "binance_market_updated_ms": now_ms,
                    "okx": 101.01,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.58,
                        "best_ask": 0.6,
                        "ask_size": 100,
                        "asks": [{"price": 0.6, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "Up")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE PASS low_entry_high_edge", rows[0]["reason"])
            self.assertEqual(bot.last_signal["side"], "Up")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE PASS", bot.last_signal["reason"])

    def test_single_fak_aggressive_edge_blocks_mid_entry_band(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=-0.5,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-block", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.03,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.03,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.03,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.6,
                        "best_ask": 0.62,
                        "ask_size": 100,
                        "asks": [{"price": 0.62, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.36,
                        "best_ask": 0.38,
                        "ask_size": 100,
                        "asks": [{"price": 0.38, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            self.assertEqual(bot.last_signal["side"], "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE 过滤历史亏损价格带", bot.last_signal["reason"])

    def test_single_fak_aggressive_edge_v2_scores_blocked_candidate_without_trading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=-0.5,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V2
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-v2-block-score", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            store.save_price_tick("BTC", 100.0, "strategy-experiment-chainlink", now - 60.0)
            store.save_price_tick("BTC", 100.04, "strategy-experiment-chainlink", now - 30.0)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.03,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.03,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.03,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.6,
                        "best_ask": 0.62,
                        "bid_size": 20,
                        "ask_size": 100,
                        "bids": [{"price": 0.6, "size": 20}],
                        "asks": [{"price": 0.62, "size": 70}, {"price": 0.63, "size": 30}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.36,
                        "best_ask": 0.38,
                        "bid_size": 100,
                        "ask_size": 20,
                        "bids": [{"price": 0.36, "size": 100}],
                        "asks": [{"price": 0.38, "size": 20}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            self.assertEqual(bot.last_signal["side"], "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V2_SHADOW", bot.last_signal["reason"])
            self.assertIn("risk=", bot.last_signal["reason"])
            self.assertIn("SINGLE_AGGRESSIVE_EDGE 过滤历史亏损价格带", bot.last_signal["reason"])
            summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["total_count"], 1)
            self.assertEqual(summary["settled_count"], 0)
            self.assertEqual(summary["base_would_trade_count"], 0)
            self.assertEqual(summary["recent_samples"][0]["side"], "Up")

            store.settle_round_outcome(
                market.round_id,
                "Down",
                now + 180,
                final_price=99.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            bot._finalize_aggressive_edge_loss_replay(market.round_id, "Down", now + 180, 99.0, 100.0)
            settled_summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(settled_summary["settled_count"], 1)
            self.assertEqual(settled_summary["recent_samples"][0]["outcome"], "Down")
            self.assertEqual(settled_summary["recent_samples"][0]["would_win"], 0)

    def test_single_fak_aggressive_edge_v4_diagnostic_records_candidate_without_trading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V4_DIAGNOSTIC
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-v4-diagnostic", "BTC", now - 30, now + 240, 100.0)
            store.upsert_round(market)
            store.save_price_tick("BTC", 100.0, "strategy-experiment-chainlink", now - 60.0)
            store.save_price_tick("BTC", 100.08, "strategy-experiment-chainlink", now - 30.0)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.16,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.16,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.16,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.71,
                        "best_ask": 0.72,
                        "bid_size": 40,
                        "ask_size": 100,
                        "bids": [{"price": 0.71, "size": 40}],
                        "asks": [{"price": 0.72, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.27,
                        "best_ask": 0.28,
                        "bid_size": 100,
                        "ask_size": 40,
                        "bids": [{"price": 0.27, "size": 100}],
                        "asks": [{"price": 0.28, "size": 40}],
                        "updated_at_ms": now_ms,
                    },
                }

            signal = Signal(
                symbol="BTC",
                side="Up",
                confidence=0.9,
                entry_price=0.72,
                move_bps=16.0,
                reason="V4 诊断测试基础候选",
            )
            filtered = bot._apply_signal_filter_mode(market, signal, bot.latest_price, bot.latest_quotes)

            self.assertEqual(filtered.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V4_GUARD BLOCK", filtered.reason)
            self.assertIn("V4_FIRST_MINUTE_HIGH_ENTRY", filtered.reason)
            self.assertIn("V4_OVEREXTENDED_MOVE", filtered.reason)
            self.assertEqual(store.open_trades(), [])
            summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["total_count"], 1)
            self.assertEqual(summary["base_would_trade_count"], 1)
            self.assertEqual(summary["v4_would_trade_count"], 0)
            self.assertIn("V4_FIRST_MINUTE_HIGH_ENTRY", summary["recent_samples"][0]["v4_block_reason"])

    def test_single_fak_aggressive_edge_v5_diagnostic_records_stricter_down_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V5_DIAGNOSTIC
            now = time.time()
            now_ms = int(now * 1000)

            def apply_down_candidate(round_id: str, entry_price: float, confidence: float, move_bps: float) -> Signal:
                market = MarketRound(round_id, "BTC", now - 150, now + 150, 100.0)
                store.upsert_round(market)
                store.save_price_tick("BTC", 99.94, "strategy-experiment-chainlink", now - 60.0)
                store.save_price_tick("BTC", 99.93, "strategy-experiment-chainlink", now - 30.0)
                with bot._lock:
                    bot.current_market = market
                    bot.latest_price = {
                        "chainlink": 99.93,
                        "chainlink_updated_ms": now_ms,
                        "binance_market": 99.93,
                        "binance_market_updated_ms": now_ms,
                        "okx": 99.93,
                        "okx_updated_ms": now_ms,
                    }
                    bot.latest_quotes = {
                        "Down": {
                            "best_bid": 0.67,
                            "best_ask": entry_price,
                            "bid_size": 120,
                            "ask_size": 40,
                            "bids": [{"price": 0.67, "size": 120}],
                            "asks": [{"price": entry_price, "size": 40}],
                            "updated_at_ms": now_ms,
                        },
                        "Up": {
                            "best_bid": 0.31,
                            "best_ask": 0.32,
                            "bid_size": 40,
                            "ask_size": 120,
                            "bids": [{"price": 0.31, "size": 40}],
                            "asks": [{"price": 0.32, "size": 120}],
                            "updated_at_ms": now_ms,
                        },
                    }
                signal = Signal(
                    symbol="BTC",
                    side="Down",
                    confidence=confidence,
                    entry_price=entry_price,
                    move_bps=move_bps,
                    reason="V5 诊断 Down 候选",
                )
                return bot._apply_signal_filter_mode(market, signal, bot.latest_price, bot.latest_quotes)

            blocked = apply_down_candidate("btc-updown-5m-single-aggressive-v5-block", 0.72, 0.9, -7.2)
            self.assertEqual(blocked.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V5_GUARD BLOCK", blocked.reason)
            self.assertIn("V5_DOWN_HIGH_ENTRY", blocked.reason)

            passed = apply_down_candidate("btc-updown-5m-single-aggressive-v5-pass", 0.68, 0.76, -7.0)
            self.assertEqual(passed.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V5_DIAGNOSTIC_NO_TRADE", passed.reason)
            self.assertIn("V5 候选通过，只记录不下注", passed.reason)
            self.assertEqual(store.open_trades(), [])

            summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["total_count"], 2)
            self.assertEqual(summary["base_would_trade_count"], 2)
            self.assertEqual(summary["v4_would_trade_count"], 2)
            self.assertEqual(summary["v5_would_trade_count"], 1)
            self.assertIn("v5_block_reason", summary["recent_samples"][0])

    def test_single_fak_aggressive_edge_v6_diagnostic_records_low_risk_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V6_DIAGNOSTIC
            now = time.time()
            now_ms = int(now * 1000)

            def apply_up_candidate(
                round_id: str,
                *,
                move_bps: float,
                before60_price: float,
                before30_price: float,
                bid_size: float,
                ask_size: float,
            ) -> Signal:
                market = MarketRound(round_id, "BTC", now - 150, now + 150, 100.0)
                store.upsert_round(market)
                store.save_price_tick("BTC", before60_price, "strategy-experiment-chainlink", now - 60.0)
                store.save_price_tick("BTC", before30_price, "strategy-experiment-chainlink", now - 30.0)
                chainlink = 100.0 * (1.0 + move_bps / 10_000.0)
                with bot._lock:
                    bot.current_market = market
                    bot.latest_price = {
                        "chainlink": chainlink,
                        "chainlink_updated_ms": now_ms,
                        "binance_market": chainlink,
                        "binance_market_updated_ms": now_ms,
                        "okx": chainlink,
                        "okx_updated_ms": now_ms,
                    }
                    bot.latest_quotes = {
                        "Up": {
                            "best_bid": 0.69,
                            "best_ask": 0.70,
                            "bid_size": bid_size,
                            "ask_size": ask_size,
                            "bids": [{"price": 0.69, "size": bid_size}],
                            "asks": [{"price": 0.70, "size": ask_size}],
                            "updated_at_ms": now_ms,
                        },
                        "Down": {
                            "best_bid": 0.29,
                            "best_ask": 0.30,
                            "bid_size": ask_size,
                            "ask_size": bid_size,
                            "bids": [{"price": 0.29, "size": ask_size}],
                            "asks": [{"price": 0.30, "size": bid_size}],
                            "updated_at_ms": now_ms,
                        },
                    }
                signal = Signal(
                    symbol="BTC",
                    side="Up",
                    confidence=0.82,
                    entry_price=0.70,
                    move_bps=move_bps,
                    reason="V6 诊断 Up 候选",
                )
                return bot._apply_signal_filter_mode(market, signal, bot.latest_price, bot.latest_quotes)

            risk_blocked = apply_up_candidate(
                "btc-updown-5m-single-aggressive-v6-risk-block",
                move_bps=6.2,
                before60_price=100.0,
                before30_price=100.14,
                bid_size=10,
                ask_size=120,
            )
            self.assertEqual(risk_blocked.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V6_GUARD BLOCK", risk_blocked.reason)
            self.assertIn("V6_RISK_SCORE_HIGH", risk_blocked.reason)

            move_blocked = apply_up_candidate(
                "btc-updown-5m-single-aggressive-v6-move-block",
                move_bps=8.5,
                before60_price=100.04,
                before30_price=100.07,
                bid_size=120,
                ask_size=10,
            )
            self.assertEqual(move_blocked.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V6_GUARD BLOCK", move_blocked.reason)
            self.assertIn("V6_EXTREME_MOVE", move_blocked.reason)

            passed = apply_up_candidate(
                "btc-updown-5m-single-aggressive-v6-pass",
                move_bps=6.5,
                before60_price=100.04,
                before30_price=100.07,
                bid_size=120,
                ask_size=10,
            )
            self.assertEqual(passed.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V6_DIAGNOSTIC_NO_TRADE", passed.reason)
            self.assertIn("V6 候选通过，只记录不下注", passed.reason)
            self.assertEqual(store.open_trades(), [])

            summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["total_count"], 3)
            self.assertEqual(summary["base_would_trade_count"], 3)
            self.assertEqual(summary["v5_would_trade_count"], 3)
            self.assertEqual(summary["v6_would_trade_count"], 1)
            self.assertIn("v6_block_reason", summary["recent_samples"][0])

            store.settle_aggressive_edge_v2_shadow_samples(
                "btc-updown-5m-single-aggressive-v6-pass",
                "Up",
                now + 180,
                final_price=101.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            settled_summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(settled_summary["base_would_trade_settled_count"], 1)
            self.assertEqual(settled_summary["base_would_win_count"], 1)
            self.assertEqual(settled_summary["base_would_win_rate_pct"], 100.0)
            self.assertEqual(settled_summary["v6_would_trade_settled_count"], 1)
            self.assertEqual(settled_summary["v6_would_win_count"], 1)
            self.assertAlmostEqual(settled_summary["v6_simulated_roi_pct"], 42.8571, places=4)
            self.assertEqual(settled_summary["v6_direction_stats"][0]["side"], "Up")
            self.assertEqual(settled_summary["v6_direction_stats"][0]["win_rate_pct"], 100.0)
            self.assertEqual(settled_summary["v6_bucket_stats"][0]["settled_count"], 1)
            self.assertEqual(settled_summary["recent_v6_samples"][0]["round_id"], "btc-updown-5m-single-aggressive-v6-pass")

    def test_single_fak_aggressive_edge_v7_diagnostic_requires_up_depth_and_down_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V7_DIAGNOSTIC
            now = time.time()
            now_ms = int(now * 1000)

            def apply_candidate(
                round_id: str,
                *,
                side: str,
                elapsed_seconds: float,
                entry_price: float,
                move_bps: float,
                bid_size: float,
                ask_size: float,
            ) -> Signal:
                market = MarketRound(round_id, "BTC", now - elapsed_seconds, now + 300 - elapsed_seconds, 100.0)
                store.upsert_round(market)
                before60 = 100.02 if side == "Up" else 99.98
                before30 = 100.04 if side == "Up" else 99.96
                store.save_price_tick("BTC", before60, "strategy-experiment-chainlink", now - 60.0)
                store.save_price_tick("BTC", before30, "strategy-experiment-chainlink", now - 30.0)
                chainlink = 100.0 * (1.0 + move_bps / 10_000.0)
                opposite = "Down" if side == "Up" else "Up"
                opposite_ask = max(0.01, min(0.99, round(1.0 - entry_price, 2)))
                with bot._lock:
                    bot.current_market = market
                    bot.latest_price = {
                        "chainlink": chainlink,
                        "chainlink_updated_ms": now_ms,
                        "binance_market": chainlink,
                        "binance_market_updated_ms": now_ms,
                        "okx": chainlink,
                        "okx_updated_ms": now_ms,
                    }
                    bot.latest_quotes = {
                        side: {
                            "best_bid": max(0.01, round(entry_price - 0.01, 2)),
                            "best_ask": entry_price,
                            "bid_size": bid_size,
                            "ask_size": ask_size,
                            "bids": [{"price": max(0.01, round(entry_price - 0.01, 2)), "size": bid_size}],
                            "asks": [{"price": entry_price, "size": ask_size}],
                            "updated_at_ms": now_ms,
                        },
                        opposite: {
                            "best_bid": max(0.01, round(opposite_ask - 0.01, 2)),
                            "best_ask": opposite_ask,
                            "bid_size": ask_size,
                            "ask_size": bid_size,
                            "bids": [{"price": max(0.01, round(opposite_ask - 0.01, 2)), "size": ask_size}],
                            "asks": [{"price": opposite_ask, "size": bid_size}],
                            "updated_at_ms": now_ms,
                        },
                    }
                signal = Signal(
                    symbol="BTC",
                    side=side,
                    confidence=0.84,
                    entry_price=entry_price,
                    move_bps=move_bps,
                    reason="V7 诊断候选",
                )
                return bot._apply_signal_filter_mode(market, signal, bot.latest_price, bot.latest_quotes)

            m2_weak_depth = apply_candidate(
                "btc-updown-5m-single-aggressive-v7-m2-weak-depth",
                side="Up",
                elapsed_seconds=150,
                entry_price=0.70,
                move_bps=6.4,
                bid_size=54,
                ask_size=46,
            )
            self.assertEqual(m2_weak_depth.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V7_GUARD BLOCK", m2_weak_depth.reason)
            self.assertIn("V7_UP_M2_WEAK_DEPTH", m2_weak_depth.reason)

            m3_weak_depth = apply_candidate(
                "btc-updown-5m-single-aggressive-v7-m3-weak-depth",
                side="Up",
                elapsed_seconds=210,
                entry_price=0.70,
                move_bps=6.2,
                bid_size=65,
                ask_size=35,
            )
            self.assertEqual(m3_weak_depth.side, "NO_TRADE")
            self.assertIn("V7_UP_M3_WEAK_DEPTH", m3_weak_depth.reason)

            down_high_entry = apply_candidate(
                "btc-updown-5m-single-aggressive-v7-down-high-entry",
                side="Down",
                elapsed_seconds=150,
                entry_price=0.69,
                move_bps=-6.2,
                bid_size=120,
                ask_size=20,
            )
            self.assertEqual(down_high_entry.side, "NO_TRADE")
            self.assertIn("V7_DOWN_HIGH_ENTRY", down_high_entry.reason)

            passed = apply_candidate(
                "btc-updown-5m-single-aggressive-v7-pass",
                side="Up",
                elapsed_seconds=210,
                entry_price=0.70,
                move_bps=6.1,
                bid_size=180,
                ask_size=20,
            )
            self.assertEqual(passed.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V7_DIAGNOSTIC_NO_TRADE", passed.reason)
            self.assertIn("V7 候选通过，只记录不下注", passed.reason)
            self.assertEqual(store.open_trades(), [])

            summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["total_count"], 4)
            self.assertEqual(summary["v6_would_trade_count"], 4)
            self.assertEqual(summary["v7_would_trade_count"], 1)
            self.assertIn("v7_block_reason", summary["recent_samples"][0])

            store.settle_aggressive_edge_v2_shadow_samples(
                "btc-updown-5m-single-aggressive-v7-pass",
                "Up",
                now + 180,
                final_price=101.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            settled_summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(settled_summary["v7_would_trade_settled_count"], 1)
            self.assertEqual(settled_summary["v7_would_win_count"], 1)
            self.assertAlmostEqual(settled_summary["v7_simulated_roi_pct"], 42.8571, places=4)
            self.assertEqual(settled_summary["v7_direction_stats"][0]["side"], "Up")
            self.assertEqual(settled_summary["v7_bucket_stats"][0]["bucket"], "m3")
            self.assertEqual(settled_summary["recent_v7_samples"][0]["round_id"], "btc-updown-5m-single-aggressive-v7-pass")
            versions = {row["version"]: row for row in settled_summary["diagnostic_version_summaries"]}
            self.assertEqual(set(versions), {"V4", "V5", "V6", "V7", "V8", "V9", "V10", "V11", "V12"})
            self.assertEqual(versions["V7"]["settled_count"], 1)
            self.assertEqual(versions["V7"]["win_count"], 1)
            self.assertAlmostEqual(versions["V7"]["simulated_roi_pct"], 42.8571, places=4)
            self.assertEqual(versions["V7"]["direction_stats"][0]["side"], "Up")
            self.assertEqual(versions["V7"]["bucket_stats"][0]["bucket"], "m3")

    def test_single_fak_aggressive_edge_v8_learning_diagnostic_collects_broad_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V8_DIAGNOSTIC
            now = time.time()
            now_ms = int(now * 1000)

            def apply_candidate(round_id: str, *, move_bps: float, bid_size: float, ask_size: float) -> Signal:
                market = MarketRound(round_id, "BTC", now - 30, now + 270, 100.0)
                store.upsert_round(market)
                store.save_price_tick("BTC", 100.01, "strategy-experiment-chainlink", now - 60.0)
                store.save_price_tick("BTC", 100.03, "strategy-experiment-chainlink", now - 30.0)
                chainlink = 100.0 * (1.0 + move_bps / 10_000.0)
                with bot._lock:
                    bot.current_market = market
                    bot.latest_price = {
                        "chainlink": chainlink,
                        "chainlink_updated_ms": now_ms,
                        "binance_market": chainlink,
                        "binance_market_updated_ms": now_ms,
                        "okx": chainlink,
                        "okx_updated_ms": now_ms,
                    }
                    bot.latest_quotes = {
                        "Up": {
                            "best_bid": 0.69,
                            "best_ask": 0.70,
                            "bid_size": bid_size,
                            "ask_size": ask_size,
                            "bids": [{"price": 0.69, "size": bid_size}],
                            "asks": [{"price": 0.70, "size": ask_size}],
                            "updated_at_ms": now_ms,
                        },
                        "Down": {
                            "best_bid": 0.29,
                            "best_ask": 0.30,
                            "bid_size": ask_size,
                            "ask_size": bid_size,
                            "bids": [{"price": 0.29, "size": ask_size}],
                            "asks": [{"price": 0.30, "size": bid_size}],
                            "updated_at_ms": now_ms,
                        },
                    }
                signal = Signal(
                    symbol="BTC",
                    side="Up",
                    confidence=0.84,
                    entry_price=0.70,
                    move_bps=move_bps,
                    reason="V8 学习采样候选",
                )
                return bot._apply_signal_filter_mode(market, signal, bot.latest_price, bot.latest_quotes)

            passed = apply_candidate(
                "btc-updown-5m-single-aggressive-v8-pass",
                move_bps=6.5,
                bid_size=10,
                ask_size=120,
            )
            self.assertEqual(passed.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V8_LEARNING_DIAGNOSTIC_NO_TRADE", passed.reason)
            self.assertIn("V8 学习样本通过，只记录不下注", passed.reason)
            self.assertIn("weak_depth", passed.reason)
            self.assertEqual(store.open_trades(), [])

            blocked = apply_candidate(
                "btc-updown-5m-single-aggressive-v8-extreme-move",
                move_bps=21.0,
                bid_size=120,
                ask_size=10,
            )
            self.assertEqual(blocked.side, "NO_TRADE")
            self.assertIn("V8_EXTREME_MOVE", blocked.reason)

            summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["total_count"], 2)
            self.assertEqual(summary["base_would_trade_count"], 2)
            self.assertEqual(summary["v8_would_trade_count"], 1)
            self.assertIn("v8_block_reason", summary["recent_samples"][0])
            self.assertEqual(summary["recent_v8_samples"][0]["round_id"], "btc-updown-5m-single-aggressive-v8-pass")

            store.settle_aggressive_edge_v2_shadow_samples(
                "btc-updown-5m-single-aggressive-v8-pass",
                "Up",
                now + 180,
                final_price=101.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            settled_summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(settled_summary["v8_would_trade_settled_count"], 1)
            self.assertEqual(settled_summary["v8_would_win_count"], 1)
            self.assertAlmostEqual(settled_summary["v8_simulated_roi_pct"], 42.8571, places=4)
            versions = {row["version"]: row for row in settled_summary["diagnostic_version_summaries"]}
            self.assertEqual(versions["V8"]["settled_count"], 1)
            self.assertEqual(versions["V8"]["direction_stats"][0]["side"], "Up")
            self.assertEqual(versions["V8"]["bucket_stats"][0]["bucket"], "m0")

    def test_single_fak_aggressive_edge_v9_diagnostic_blocks_m1_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V9_DIAGNOSTIC
            now = time.time()
            now_ms = int(now * 1000)

            def apply_candidate(round_id: str, *, elapsed_seconds: float) -> Signal:
                market = MarketRound(round_id, "BTC", now - elapsed_seconds, now + 300 - elapsed_seconds, 100.0)
                store.upsert_round(market)
                store.save_price_tick("BTC", 100.01, "strategy-experiment-chainlink", now - 60.0)
                store.save_price_tick("BTC", 100.03, "strategy-experiment-chainlink", now - 30.0)
                chainlink = 100.065
                with bot._lock:
                    bot.current_market = market
                    bot.latest_price = {
                        "chainlink": chainlink,
                        "chainlink_updated_ms": now_ms,
                        "binance_market": chainlink,
                        "binance_market_updated_ms": now_ms,
                        "okx": chainlink,
                        "okx_updated_ms": now_ms,
                    }
                    bot.latest_quotes = {
                        "Up": {
                            "best_bid": 0.69,
                            "best_ask": 0.70,
                            "bid_size": 120,
                            "ask_size": 20,
                            "bids": [{"price": 0.69, "size": 120}],
                            "asks": [{"price": 0.70, "size": 20}],
                            "updated_at_ms": now_ms,
                        },
                        "Down": {
                            "best_bid": 0.29,
                            "best_ask": 0.30,
                            "bid_size": 20,
                            "ask_size": 120,
                            "bids": [{"price": 0.29, "size": 20}],
                            "asks": [{"price": 0.30, "size": 120}],
                            "updated_at_ms": now_ms,
                        },
                    }
                signal = Signal(
                    symbol="BTC",
                    side="Up",
                    confidence=0.84,
                    entry_price=0.70,
                    move_bps=6.5,
                    reason="V9 诊断候选",
                )
                return bot._apply_signal_filter_mode(market, signal, bot.latest_price, bot.latest_quotes)

            m1_blocked = apply_candidate("btc-updown-5m-single-aggressive-v9-m1-block", elapsed_seconds=90)
            self.assertEqual(m1_blocked.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V9_M1_GUARD_DIAGNOSTIC_NO_TRADE", m1_blocked.reason)
            self.assertIn("V9_M1_BUCKET_BLOCK", m1_blocked.reason)

            m2_passed = apply_candidate("btc-updown-5m-single-aggressive-v9-m2-pass", elapsed_seconds=150)
            self.assertEqual(m2_passed.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V9_M1_GUARD_DIAGNOSTIC_NO_TRADE", m2_passed.reason)
            self.assertIn("V9 候选通过，只记录不下注", m2_passed.reason)
            self.assertEqual(store.open_trades(), [])

            summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(summary["total_count"], 2)
            self.assertEqual(summary["v8_would_trade_count"], 2)
            self.assertEqual(summary["v9_would_trade_count"], 1)
            self.assertIn("v9_block_reason", summary["recent_samples"][0])
            self.assertEqual(summary["recent_v9_samples"][0]["round_id"], "btc-updown-5m-single-aggressive-v9-m2-pass")

            store.settle_aggressive_edge_v2_shadow_samples(
                "btc-updown-5m-single-aggressive-v9-m2-pass",
                "Up",
                now + 180,
                final_price=101.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            settled_summary = store.aggressive_edge_v2_shadow_summary("BTC")
            self.assertEqual(settled_summary["v9_would_trade_settled_count"], 1)
            self.assertEqual(settled_summary["v9_would_win_count"], 1)
            versions = {row["version"]: row for row in settled_summary["diagnostic_version_summaries"]}
            self.assertEqual(versions["V9"]["settled_count"], 1)
            self.assertEqual(versions["V9"]["bucket_stats"][0]["bucket"], "m2")
            self.assertEqual(versions["V9"]["live_readiness"]["status"], "WAITING_FOR_SAMPLE")
            self.assertFalse(versions["V9"]["live_readiness"]["eligible_for_live_review"])

    def test_aggressive_edge_v9_backfills_from_v8_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            store = TradeStore(db_path, 100.0)
            now = time.time()
            report = {
                "risk_score": 0.1,
                "risk_level": "LOW",
                "risk_reasons": [],
                "features": {},
                "components": {},
            }
            for sample_key, outcome in [("m0:pass", "Up"), ("m1:pass", "Up"), ("m2:pass", "Down")]:
                side = "Down" if outcome == "Down" else "Up"
                round_id = f"btc-updown-5m-v9-backfill-{sample_key.split(':')[0]}"
                store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key=sample_key,
                    side=side,
                    source_signal_side=side,
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    v6_would_trade=True,
                    v7_would_trade=True,
                    v8_would_trade=True,
                    v9_would_trade=False,
                    entry_price=0.65,
                    confidence=0.78,
                    move_bps=6.5,
                    report=report,
                    base_block_reason=None,
                    v1_block_reason=None,
                    v4_block_reason=None,
                    v5_block_reason=None,
                    v6_block_reason=None,
                    v7_block_reason=None,
                    v8_block_reason=None,
                    v9_block_reason=None,
                    signal_reason="V9 历史回填测试样本",
                    created_at=now,
                )
                store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    outcome,
                    now + 10,
                    final_price=101.0 if outcome == "Up" else 99.0,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )
            store.conn.close()

            reopened = TradeStore(db_path, 100.0)
            summary = reopened.aggressive_edge_v2_shadow_summary("BTC")
            versions = {row["version"]: row for row in summary["diagnostic_version_summaries"]}
            self.assertEqual(summary["v8_would_trade_settled_count"], 3)
            self.assertEqual(summary["v9_would_trade_settled_count"], 2)
            self.assertEqual(versions["V9"]["settled_count"], 2)
            self.assertEqual({row["bucket"] for row in versions["V9"]["bucket_stats"]}, {"m0", "m2"})
            blocked_row = reopened.conn.execute(
                """
                SELECT v9_would_trade, v9_block_reason
                FROM aggressive_edge_v2_shadow_samples
                WHERE sample_key = 'm1:pass'
                """
            ).fetchone()
            self.assertEqual(blocked_row["v9_would_trade"], 0)
            self.assertIn("V9_M1_BUCKET_BLOCK", blocked_row["v9_block_reason"])
            reopened.conn.close()

    def test_aggressive_edge_v10_blocks_up_weak_move_and_top_skew(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", max_quote_age_ms=60_000)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound("btc-updown-5m-v10-guard", "BTC", now - 150, now + 150, 100.0)
            report = {
                "risk_score": 0.1,
                "risk_level": "LOW",
                "risk_reasons": [],
                "features": {
                    "entry_price": 0.70,
                    "move_bps": 6.5,
                    "top_level_skew": 0.50,
                },
                "components": {},
            }

            weak_move = bot._aggressive_edge_v10_up_reversal_guard_block_reason(
                market,
                Signal("BTC", "Up", 0.80, 0.70, 5.0, "V10 弱动能测试"),
                {**report, "features": {**report["features"], "move_bps": 5.0, "top_level_skew": 0.50}},
                now=now,
            )
            self.assertIn("V10_UP_WEAK_MOVE", weak_move)

            weak_top = bot._aggressive_edge_v10_up_reversal_guard_block_reason(
                market,
                Signal("BTC", "Up", 0.80, 0.70, 6.5, "V10 弱顶层盘口测试"),
                {**report, "features": {**report["features"], "move_bps": 6.5, "top_level_skew": 0.10}},
                now=now,
            )
            self.assertIn("V10_UP_WEAK_TOP_SKEW", weak_top)

            strong_up = bot._aggressive_edge_v10_up_reversal_guard_block_reason(
                market,
                Signal("BTC", "Up", 0.80, 0.70, 6.5, "V10 强 Up 测试"),
                report,
                now=now,
            )
            self.assertIsNone(strong_up)

            down_candidate = bot._aggressive_edge_v10_up_reversal_guard_block_reason(
                market,
                Signal("BTC", "Down", 0.80, 0.68, -4.5, "V10 Down 旁路测试"),
                {**report, "features": {**report["features"], "entry_price": 0.68, "move_bps": -4.5}},
                now=now,
            )
            self.assertIsNone(down_candidate)

    def test_aggressive_edge_v10_backfills_from_v9_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            store = TradeStore(db_path, 100.0)
            now = time.time()
            fixtures = [
                ("strong-up", "m2:pass", "Up", 0.70, 6.5, 0.50, "Up"),
                ("weak-move", "m2:pass", "Up", 0.70, 5.0, 0.50, "Down"),
                ("weak-top", "m3:pass", "Up", 0.70, 6.5, 0.10, "Down"),
                ("down-pass", "m2:pass", "Down", 0.68, -4.5, 0.10, "Down"),
            ]
            for label, sample_key, side, entry_price, move_bps, top_level_skew, outcome in fixtures:
                report = {
                    "risk_score": 0.1,
                    "risk_level": "LOW",
                    "risk_reasons": [],
                    "features": {
                        "entry_price": entry_price,
                        "move_bps": move_bps,
                        "top_level_skew": top_level_skew,
                    },
                    "components": {},
                }
                round_id = f"btc-updown-5m-v10-backfill-{label}"
                store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key=sample_key,
                    side=side,
                    source_signal_side=side,
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    v6_would_trade=True,
                    v7_would_trade=True,
                    v8_would_trade=True,
                    v9_would_trade=True,
                    v10_would_trade=False,
                    entry_price=entry_price,
                    confidence=0.80,
                    move_bps=move_bps,
                    report=report,
                    base_block_reason=None,
                    v1_block_reason=None,
                    v4_block_reason=None,
                    v5_block_reason=None,
                    v6_block_reason=None,
                    v7_block_reason=None,
                    v8_block_reason=None,
                    v9_block_reason=None,
                    v10_block_reason=None,
                    signal_reason="V10 历史回填测试样本",
                    created_at=now,
                )
                store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    outcome,
                    now + 10,
                    final_price=101.0 if outcome == "Up" else 99.0,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )
            store.conn.close()

            reopened = TradeStore(db_path, 100.0)
            summary = reopened.aggressive_edge_v2_shadow_summary("BTC")
            versions = {row["version"]: row for row in summary["diagnostic_version_summaries"]}
            self.assertEqual(summary["v9_would_trade_settled_count"], 4)
            self.assertEqual(summary["v10_would_trade_settled_count"], 2)
            self.assertEqual(versions["V10"]["win_count"], 2)
            self.assertEqual(versions["V10"]["loss_count"], 0)
            blocked_rows = reopened.conn.execute(
                """
                SELECT v10_block_reason
                FROM aggressive_edge_v2_shadow_samples
                WHERE v10_would_trade = 0
                ORDER BY round_id
                """
            ).fetchall()
            self.assertTrue(any("V10_UP_WEAK_MOVE" in row["v10_block_reason"] for row in blocked_rows))
            self.assertTrue(any("V10_UP_WEAK_TOP_SKEW" in row["v10_block_reason"] for row in blocked_rows))
            reopened.conn.close()

    def test_aggressive_edge_v12_reversal_guard_blocks_overextended_and_weak_down_top(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", max_quote_age_ms=60_000)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound("btc-updown-5m-v12-guard", "BTC", now - 150, now + 150, 100.0)
            base_report = {
                "risk_score": 0.1,
                "risk_level": "LOW",
                "risk_reasons": [],
                "features": {
                    "entry_price": 0.65,
                    "move_bps": 6.5,
                    "depth_skew": 0.55,
                    "top_level_skew": 0.60,
                },
                "components": {},
            }

            overextended = bot._aggressive_edge_v12_reversal_guard_block_reason(
                market,
                Signal("BTC", "Up", 0.82, 0.65, 8.5, "V12 过度位移测试"),
                {**base_report, "features": {**base_report["features"], "move_bps": 8.5}},
                now=now,
            )
            self.assertIn("V12_OVEREXTENDED_MOVE", overextended)

            weak_down_top = bot._aggressive_edge_v12_reversal_guard_block_reason(
                market,
                Signal("BTC", "Down", 0.82, 0.65, -6.5, "V12 Down 弱顶层盘口测试"),
                {**base_report, "features": {**base_report["features"], "move_bps": -6.5, "top_level_skew": 0.20}},
                now=now,
            )
            self.assertIn("V12_DOWN_WEAK_TOP_SKEW", weak_down_top)

            strong_down = bot._aggressive_edge_v12_reversal_guard_block_reason(
                market,
                Signal("BTC", "Down", 0.82, 0.65, -6.5, "V12 Down 强候选测试"),
                {**base_report, "features": {**base_report["features"], "move_bps": -6.5, "top_level_skew": 0.35}},
                now=now,
            )
            self.assertIsNone(strong_down)

            weak_v11 = bot._aggressive_edge_v12_reversal_guard_block_reason(
                market,
                Signal("BTC", "Up", 0.82, 0.65, 6.5, "V12 继承 V11 测试"),
                {**base_report, "features": {**base_report["features"], "depth_skew": 0.20}},
                now=now,
            )
            self.assertIn("V11 守卫未通过", weak_v11)
            self.assertIn("V11_WEAK_DEPTH", weak_v11)

    def test_aggressive_edge_v12_backfills_from_v11_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            store = TradeStore(db_path, 100.0)
            now = time.time()
            fixtures = [
                ("pass", "Down", -6.5, 0.35, "Down"),
                ("overextended", "Up", 8.5, 0.60, "Down"),
            ]
            for label, side, move_bps, top_level_skew, outcome in fixtures:
                report = {
                    "risk_score": 0.1,
                    "risk_level": "LOW",
                    "risk_reasons": [],
                    "features": {
                        "entry_price": 0.65,
                        "move_bps": move_bps,
                        "depth_skew": 0.55,
                        "top_level_skew": top_level_skew,
                    },
                    "components": {},
                }
                round_id = f"btc-updown-5m-v12-backfill-{label}"
                store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key="m2:pass",
                    side=side,
                    source_signal_side=side,
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    v6_would_trade=True,
                    v7_would_trade=True,
                    v8_would_trade=True,
                    v9_would_trade=True,
                    v10_would_trade=True,
                    v11_would_trade=True,
                    entry_price=0.65,
                    confidence=0.82,
                    move_bps=move_bps,
                    report=report,
                    base_block_reason=None,
                    v1_block_reason=None,
                    v4_block_reason=None,
                    v5_block_reason=None,
                    v6_block_reason=None,
                    v7_block_reason=None,
                    v8_block_reason=None,
                    v9_block_reason=None,
                    v10_block_reason=None,
                    v11_block_reason=None,
                    signal_reason="V12 历史回填测试样本",
                    created_at=now,
                )
                store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    outcome,
                    now + 10,
                    final_price=101.0 if outcome == "Up" else 99.0,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )
            store.conn.close()

            reopened = TradeStore(db_path, 100.0)
            summary = reopened.aggressive_edge_v2_shadow_summary("BTC")
            versions = {row["version"]: row for row in summary["diagnostic_version_summaries"]}
            self.assertEqual(summary["v11_would_trade_settled_count"], 2)
            self.assertEqual(summary["v12_would_trade_settled_count"], 1)
            self.assertEqual(versions["V12"]["settled_count"], 1)
            self.assertEqual(versions["V12"]["win_count"], 1)
            blocked_row = reopened.conn.execute(
                """
                SELECT v12_would_trade, v12_block_reason
                FROM aggressive_edge_v2_shadow_samples
                WHERE round_id = 'btc-updown-5m-v12-backfill-overextended'
                """
            ).fetchone()
            self.assertEqual(blocked_row["v12_would_trade"], 0)
            self.assertIn("V12_OVEREXTENDED_MOVE", blocked_row["v12_block_reason"])
            reopened.conn.close()

    def test_aggressive_edge_shadow_candidates_are_paginated_by_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            report = {
                "risk_score": 0.1,
                "risk_level": "LOW",
                "risk_reasons": [],
                "features": {
                    "entry_price": 0.65,
                    "move_bps": 6.5,
                    "depth_skew": 0.55,
                    "top_level_skew": 0.60,
                },
                "components": {},
            }
            for index in range(10):
                side = "Up" if index % 2 == 0 else "Down"
                store.record_aggressive_edge_v2_shadow_sample(
                    round_id=f"btc-updown-5m-v12-page-{index}",
                    symbol="BTC",
                    sample_key="m2:pass",
                    side=side,
                    source_signal_side=side,
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    v6_would_trade=True,
                    v7_would_trade=True,
                    v8_would_trade=True,
                    v9_would_trade=True,
                    v10_would_trade=True,
                    v11_would_trade=True,
                    v12_would_trade=True,
                    entry_price=0.65,
                    confidence=0.82,
                    move_bps=6.5,
                    report=report,
                    base_block_reason=None,
                    v1_block_reason=None,
                    signal_reason="V12 分页测试样本",
                    created_at=now + index,
                )

            first = store.aggressive_edge_v2_shadow_candidates("BTC", "V12", limit=8, offset=0)
            second = store.aggressive_edge_v2_shadow_candidates("BTC", "V12", limit=8, offset=8)

            self.assertEqual(first["meta"]["total"], 10)
            self.assertEqual(first["meta"]["loaded"], 8)
            self.assertTrue(first["meta"]["has_more"])
            self.assertEqual(first["meta"]["total_pages"], 2)
            self.assertEqual(len(second["candidates"]), 2)
            self.assertFalse(second["meta"]["has_more"])
            self.assertEqual(first["candidates"][0]["round_id"], "btc-updown-5m-v12-page-9")
            self.assertEqual(second["candidates"][0]["round_id"], "btc-updown-5m-v12-page-1")
            with self.assertRaises(ValueError):
                store.aggressive_edge_v2_shadow_candidates("BTC", "BAD", limit=8, offset=0)
            store.conn.close()

    def test_aggressive_edge_live_readiness_marks_clean_v9_sample_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            report = {
                "risk_score": 0.12,
                "risk_level": "LOW",
                "risk_reasons": [],
                "features": {
                    "entry_price": 0.70,
                    "confidence": 0.84,
                    "edge": 0.14,
                    "move_bps": 6.5,
                    "depth_skew": 0.55,
                    "top_level_skew": 0.55,
                    "momentum_decay_bps": -2.0,
                },
                "components": {},
            }
            # 构造 80 单高质量 V9 样本，覆盖方向和时间桶，验证实盘准入门槛能给出 READY。
            for index in range(80):
                bucket = (0, 2, 3)[index % 3]
                side = "Up" if index % 2 == 0 else "Down"
                round_id = f"btc-updown-5m-v9-ready-{index}"
                store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key=f"m{bucket}:pass",
                    side=side,
                    source_signal_side=side,
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    v6_would_trade=True,
                    v7_would_trade=True,
                    v8_would_trade=True,
                    v9_would_trade=True,
                    entry_price=0.70,
                    confidence=0.84,
                    move_bps=6.5 if side == "Up" else -6.5,
                    report=report,
                    base_block_reason=None,
                    v1_block_reason=None,
                    signal_reason="V9 ready gate fixture",
                    created_at=now + index,
                )
                outcome = side if index < 70 else ("Down" if side == "Up" else "Up")
                store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    outcome,
                    now + index + 60,
                    final_price=101.0 if outcome == "Up" else 99.0,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )

            versions = {
                row["version"]: row
                for row in store.aggressive_edge_v2_shadow_summary("BTC")["diagnostic_version_summaries"]
            }
            readiness = versions["V9"]["live_readiness"]
            self.assertEqual(versions["V9"]["settled_count"], 80)
            self.assertGreaterEqual(versions["V9"]["win_rate_pct"], 70.0)
            self.assertGreaterEqual(versions["V9"]["simulated_roi_pct"], 5.0)
            self.assertEqual(readiness["status"], "READY_FOR_REAL_REVIEW")
            self.assertTrue(readiness["eligible_for_live_review"])
            self.assertEqual(readiness["reasons"], [])

    def test_single_fak_aggressive_edge_v3_blocks_historical_loss_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history_store = TradeStore(Path(tmp) / "single_fak_aggressive_edge_v2.sqlite3", 100.0)
            now = time.time()
            # V3 的历史直觉来自已结算相似样本：高价入场、弱延续，历史胜率低于买入价要求。
            for index in range(6):
                round_id = f"btc-updown-5m-v3-history-{index}"
                report = {
                    "risk_score": 0.15,
                    "risk_level": "LOW",
                    "risk_reasons": [],
                    "features": {
                        "entry_price": 0.71,
                        "confidence": 0.89,
                        "edge": 0.18,
                        "move_bps": 10.1,
                        "momentum_decay_bps": 0.0,
                        "momentum_30_to_now_bps": 1.0,
                        "external_divergence_count": 0,
                    },
                    "components": {},
                }
                history_store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key="m2:pass",
                    side="Up",
                    source_signal_side="Up",
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    entry_price=0.71,
                    confidence=0.89,
                    move_bps=10.1,
                    report=report,
                    base_block_reason=None,
                    v1_block_reason=None,
                    signal_reason="历史高价弱延续样本",
                    created_at=now + index,
                )
                outcome = "Up" if index < 2 else "Down"
                final_price = 101.0 if outcome == "Up" else 99.0
                history_store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    outcome,
                    now + index + 1,
                    final_price=final_price,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )

            settings = Settings(
                db_path=Path(tmp) / "single_fak_aggressive_edge_v3.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V3
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-v3-block", "BTC", now - 90, now + 180, 100.0)
            store.upsert_round(market)
            store.save_price_tick("BTC", 100.0, "strategy-experiment-chainlink", now - 60.0)
            store.save_price_tick("BTC", 100.06, "strategy-experiment-chainlink", now - 30.0)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.11,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.11,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.11,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.7,
                        "best_ask": 0.71,
                        "bid_size": 100,
                        "ask_size": 100,
                        "bids": [{"price": 0.7, "size": 100}],
                        "asks": [{"price": 0.71, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.28,
                        "best_ask": 0.29,
                        "bid_size": 100,
                        "ask_size": 100,
                        "bids": [{"price": 0.28, "size": 100}],
                        "asks": [{"price": 0.29, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            signal = Signal(
                symbol="BTC",
                side="Up",
                confidence=0.89,
                entry_price=0.71,
                move_bps=10.1,
                reason="V3 测试基础候选",
            )
            filtered = bot._apply_signal_filter_mode(market, signal, bot.latest_price, bot.latest_quotes)

            self.assertEqual(filtered.side, "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V3_INTUITION BLOCK", filtered.reason)
            self.assertIn("similar 6", filtered.reason)
            self.assertIn("required 73.00%", filtered.reason)

    def test_single_fak_aggressive_edge_v1_blocks_learned_false_breakout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-false-breakout", "BTC", now - 45, now + 240, 100.0)
            store.upsert_round(market)
            # 复盘记忆命中的形态：60 秒前贴近目标价，当前突然冲到 sweet_move 区间。
            store.save_price_tick("BTC", 100.0, "strategy-experiment-chainlink", now - 60.0)
            store.save_price_tick("BTC", 100.07, "strategy-experiment-chainlink", now)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.07,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.07,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.07,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.64,
                        "best_ask": 0.66,
                        "ask_size": 100,
                        "asks": [{"price": 0.66, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.32,
                        "best_ask": 0.34,
                        "ask_size": 100,
                        "asks": [{"price": 0.34, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            self.assertEqual(bot.last_signal["side"], "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE 学习过滤V2 sweet Up负期望分支", bot.last_signal["reason"])

    def test_single_fak_aggressive_edge_base_keeps_sweet_up_as_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-baseline-sweet", "BTC", now - 45, now + 240, 100.0)
            store.upsert_round(market)
            store.save_price_tick("BTC", 100.0, "strategy-experiment-chainlink", now - 60.0)
            store.save_price_tick("BTC", 100.07, "strategy-experiment-chainlink", now)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.07,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.07,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.07,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.64,
                        "best_ask": 0.66,
                        "ask_size": 100,
                        "asks": [{"price": 0.66, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.32,
                        "best_ask": 0.34,
                        "ask_size": 100,
                        "asks": [{"price": 0.34, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "Up")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE PASS sweet_move_6_8bps", rows[0]["reason"])

    def test_single_fak_aggressive_edge_v1_blocks_sweet_up_without_fast_jump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-sweet-keep", "BTC", now - 45, now + 240, 100.0)
            store.upsert_round(market)
            store.save_price_tick("BTC", 100.05, "strategy-experiment-chainlink", now - 60.0)
            store.save_price_tick("BTC", 100.07, "strategy-experiment-chainlink", now)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.07,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.07,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.07,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.64,
                        "best_ask": 0.66,
                        "ask_size": 100,
                        "asks": [{"price": 0.66, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.32,
                        "best_ask": 0.34,
                        "ask_size": 100,
                        "asks": [{"price": 0.34, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            self.assertEqual(bot.last_signal["side"], "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE 学习过滤V2 sweet Up负期望分支", bot.last_signal["reason"])

    def test_single_fak_aggressive_edge_v1_blocks_high_entry_with_thin_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-high-thin-edge", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.058,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.058,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.058,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.69,
                        "best_ask": 0.71,
                        "ask_size": 100,
                        "asks": [{"price": 0.71, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.27,
                        "best_ask": 0.29,
                        "ask_size": 100,
                        "asks": [{"price": 0.29, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            self.assertEqual(bot.last_signal["side"], "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE 学习过滤V2 高价安全边际不足", bot.last_signal["reason"])

    def test_single_fak_aggressive_edge_v1_allows_high_entry_with_v2_margin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V1
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-high-v2-pass", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 100.09,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 100.09,
                    "binance_market_updated_ms": now_ms,
                    "okx": 100.09,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.69,
                        "best_ask": 0.71,
                        "ask_size": 100,
                        "asks": [{"price": 0.71, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.27,
                        "best_ask": 0.29,
                        "ask_size": 100,
                        "asks": [{"price": 0.29, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "Up")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE PASS high_confidence_high_entry", rows[0]["reason"])

    def test_single_fak_aggressive_edge_writes_loss_replay_for_official_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-loss-replay", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 101.02,
                    "binance_market_updated_ms": now_ms,
                    "okx": 101.01,
                    "okx_updated_ms": now_ms,
                    "source": "test",
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "bid_size": 90,
                        "ask_size": 100,
                        "bids": [{"price": 0.39, "size": 90}, {"price": 0.38, "size": 80}],
                        "asks": [{"price": 0.4, "size": 70}, {"price": 0.41, "size": 30}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.58,
                        "best_ask": 0.6,
                        "bid_size": 85,
                        "ask_size": 100,
                        "bids": [{"price": 0.58, "size": 85}],
                        "asks": [{"price": 0.6, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()
            open_rows = store.open_trades()
            self.assertEqual(len(open_rows), 1)
            trade_id = int(open_rows[0]["id"])

            store.settle_round_outcome(
                market.round_id,
                "Down",
                now + 180,
                final_price=99.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            bot._finalize_aggressive_edge_loss_replay(market.round_id, "Down", now + 180, 99.0, 100.0)

            replay_path = Path(tmp) / "loss-replays" / "single_fak_aggressive_edge.jsonl"
            packet = json.loads(replay_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(packet["variant_id"], "SINGLE_FAK_AGGRESSIVE_EDGE")
            self.assertEqual(packet["round"]["round_id"], market.round_id)
            self.assertEqual(packet["settlement"]["outcome"], "Down")
            self.assertAlmostEqual(packet["settlement"]["final_distance_bps"], -100.0, places=6)
            self.assertEqual(packet["loss_trades"][0]["id"], trade_id)
            self.assertLess(packet["loss_trades"][0]["pnl"], 0)
            self.assertGreaterEqual(packet["sample_count"], 1)
            self.assertIn("entry_fill", packet["summary"]["events"])
            self.assertEqual(packet["samples"][0]["quotes"]["Up"]["asks"][0]["price"], 0.4)
            self.assertAlmostEqual(packet["samples"][0]["price"]["chainlink"]["distance_bps"], 100.0, places=6)

    def test_single_fak_aggressive_edge_discards_win_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.signal_filter_mode = SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound("btc-updown-5m-single-aggressive-win-replay", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": now_ms,
                    "binance_market": 101.02,
                    "binance_market_updated_ms": now_ms,
                    "okx": 101.01,
                    "okx_updated_ms": now_ms,
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "bid_size": 100,
                        "ask_size": 100,
                        "bids": [{"price": 0.39, "size": 100}],
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                    "Down": {
                        "best_bid": 0.58,
                        "best_ask": 0.6,
                        "bid_size": 100,
                        "ask_size": 100,
                        "bids": [{"price": 0.58, "size": 100}],
                        "asks": [{"price": 0.6, "size": 100}],
                        "updated_at_ms": now_ms,
                    },
                }

            bot._run_strategy_from_state()
            self.assertEqual(len(store.open_trades()), 1)

            store.settle_round_outcome(
                market.round_id,
                "Up",
                now + 180,
                final_price=101.0,
                target_price=100.0,
                settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
            )
            bot._finalize_aggressive_edge_loss_replay(market.round_id, "Up", now + 180, 101.0, 100.0)

            replay_path = Path(tmp) / "loss-replays" / "single_fak_aggressive_edge.jsonl"
            self.assertFalse(replay_path.exists())

    def test_aggressive_edge_strategy_memory_records_loss_lessons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "single_fak_aggressive_edge.sqlite3"
            memory_path = Path(tmp) / "single_fak_aggressive_edge.memory.jsonl"
            store = TradeStore(db_path, 100.0)
            con = store.conn
            loss_round = MarketRound("btc-updown-5m-memory-loss", "BTC", 1_000.0, 1_300.0, 100.0)
            win_round = MarketRound("btc-updown-5m-memory-win", "BTC", 2_000.0, 2_300.0, 100.0)
            store.upsert_round(loss_round)
            store.upsert_round(win_round)
            with con:
                con.execute(
                    """
                    UPDATE market_rounds
                    SET final_price = ?, outcome = ?, settled_at = ?, settlement_source = ?
                    WHERE round_id = ?
                    """,
                    (99.0, "Down", 1_305.0, SETTLEMENT_SOURCE_POLYMARKET, loss_round.round_id),
                )
                con.execute(
                    """
                    UPDATE market_rounds
                    SET final_price = ?, outcome = ?, settled_at = ?, settlement_source = ?
                    WHERE round_id = ?
                    """,
                    (101.0, "Up", 2_305.0, SETTLEMENT_SOURCE_POLYMARKET, win_round.round_id),
                )
                con.execute(
                    """
                    INSERT INTO trades(
                        id, round_id, symbol, side, stake, entry_price, shares, confidence, move_bps,
                        status, opened_at, settled_at, exit_price, payout, pnl, settlement_source, reason
                    )
                    VALUES (?, ?, 'BTC', 'Up', 5, 0.66, 7.4, 0.79, 7.0, 'SETTLED', 1045, 1305, 0, 0, -5, ?, ?)
                    """,
                    (
                        1,
                        loss_round.round_id,
                        SETTLEMENT_SOURCE_POLYMARKET,
                        "真实 BTC 5m Up: Chainlink 100.07 vs target 100.00, 距离 7.00bps, ask 0.66, edge 0.130 | "
                        "SINGLE_AGGRESSIVE_EDGE PASS sweet_move_6_8bps: entry 0.6600, confidence 0.7900, edge 0.1300, abs_bps 7.00",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO trades(
                        id, round_id, symbol, side, stake, entry_price, shares, confidence, move_bps,
                        status, opened_at, settled_at, exit_price, payout, pnl, settlement_source, reason
                    )
                    VALUES (?, ?, 'BTC', 'Up', 5, 0.66, 7.4, 0.7978, 6.1, 'SETTLED', 2110, 2305, 1, 7.399646, 2.399646, ?, ?)
                    """,
                    (
                        2,
                        win_round.round_id,
                        SETTLEMENT_SOURCE_CHAINLINK,
                        "真实 BTC 5m Up: Chainlink 100.06 vs target 100.00, 距离 6.10bps, ask 0.66, edge 0.138 | "
                        "SINGLE_AGGRESSIVE_EDGE PASS sweet_move_6_8bps: entry 0.6600, confidence 0.7978, edge 0.1378, abs_bps 6.10",
                    ),
                )
                for created_at, price in [(1001.0, 100.0), (1045.0, 100.07), (1075.0, 100.08), (1105.0, 99.99), (1300.0, 99.0)]:
                    con.execute(
                        "INSERT INTO price_ticks(symbol, price, source, created_at) VALUES ('BTC', ?, 'test-chainlink', ?)",
                        (price, created_at),
                    )
                for created_at, price in [(2001.0, 100.0), (2050.0, 99.95), (2110.0, 100.061), (2170.0, 100.12), (2300.0, 101.0)]:
                    con.execute(
                        "INSERT INTO price_ticks(symbol, price, source, created_at) VALUES ('BTC', ?, 'test-chainlink', ?)",
                        (price, created_at),
                    )

            entry = build_aggressive_edge_memory_entry(db_path, memory_path=memory_path, created_at=1_234.0)

            self.assertEqual(entry["strategy_id"], "SINGLE_FAK_AGGRESSIVE_EDGE")
            self.assertEqual(entry["learning_target_strategy_id"], "SINGLE_FAK_AGGRESSIVE_EDGE_V1")
            self.assertEqual(entry["real_strategy_id"], "SINGLE_FAK_AGGRESSIVE_EDGE_REAL")
            self.assertEqual(entry["sample_window"]["settled_trades"], 2)
            self.assertEqual(entry["sample_window"]["loss_count"], 1)
            false_rule = entry["evidence"]["false_breakout_rule"]
            self.assertEqual(false_rule["matched_loss_ids"], [1])
            self.assertEqual(false_rule["matched_win_ids"], [])
            hard_gate = entry["evidence"]["hard_gate_rejection"]
            self.assertEqual(hard_gate["status"], "rejected_overfit_gate")
            self.assertIn(2, hard_gate["blocked_win_ids"])
            self.assertIn("sweet_up_false_breakout", entry["risk_tags"])
            self.assertTrue(
                any(
                    item["action"] == "apply_paper_guard" and item["target"] == "SINGLE_FAK_AGGRESSIVE_EDGE_V1"
                    for item in entry["parameter_recommendations"]
                )
            )
            self.assertTrue(any(item["action"] == "do_not_apply" for item in entry["parameter_recommendations"]))

            first = append_strategy_memory_entry(memory_path, entry)
            second = append_strategy_memory_entry(memory_path, entry)
            self.assertTrue(first["appended"])
            self.assertFalse(second["appended"])
            loaded = load_strategy_memory(memory_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["entry_id"], entry["entry_id"])

    def test_single_fak_stop_and_flip_closes_old_side_before_new_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_edge=0.0,
                max_entry_price=0.8,
                max_open_trades=1,
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.single_entry_mode = SINGLE_ENTRY_MODE_STOP_AND_FLIP
            now = time.time()
            market = MarketRound("btc-updown-5m-single-stop-flip", "BTC", now - 60, now + 120, 100.0)
            store.upsert_round(market)
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.36, "best_ask": 0.4, "asks": [{"price": 0.4, "size": 100}]},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "asks": [{"price": 0.5, "size": 100}]},
                }

            bot._maybe_place_trade(market, Signal("BTC", "Up", 0.7, 0.4, 10.0, "stop flip first"))
            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.5, -10.0, "stop flip new side"))

            open_rows = store.open_trades()
            self.assertEqual(len(open_rows), 1)
            self.assertEqual(open_rows[0]["side"], "Down")
            recent = store.recent_trades(5)
            closed_up = next(row for row in recent if row["side"] == "Up")
            self.assertEqual(closed_up["status"], "SETTLED")
            self.assertEqual(closed_up["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            self.assertIn("SINGLE_STOP_AND_FLIP", closed_up["reason"])
            self.assertIn("SINGLE_STOP_AND_FLIP", open_rows[0]["reason"])
            summary = store.trade_reason_summary("SINGLE_STOP_AND_FLIP", "BTC")
            self.assertEqual(summary["total_count"], 2)
            self.assertEqual(summary["settled_count"], 1)
            self.assertEqual(summary["open_count"], 1)

    def test_bot_fetches_rest_depth_when_snapshot_has_only_best_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_edge=0.0, max_entry_price=0.8, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-depth-rest",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "manual best-only signal")
            bot.polymarket.get_quote = lambda _token, _side: type(
                "Quote",
                (),
                {
                    "to_dict": lambda _self: {
                        "token_id": "up-token",
                        "outcome": "Up",
                        "best_bid": 0.33,
                        "best_ask": 0.34,
                        "bid_size": 10,
                        "ask_size": 1,
                        "asks": [{"price": 0.34, "size": 1}, {"price": 0.45, "size": 100}],
                        "bids": [{"price": 0.33, "size": 10}],
                        "updated_at_ms": int(now * 1000),
                        "source": "rest",
                    }
                },
            )()
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.33, "best_ask": 0.34, "ask_size": 1, "updated_at_ms": int(now * 1000)}
                }

            bot._maybe_place_trade(market, signal)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertIn("levels 2", rows[0]["reason"])
            self.assertGreater(rows[0]["entry_price"], 0.34)

    def test_storage_records_paper_order_and_each_fill_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-audit",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "audit signal")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            result = simulate_fak_buy(
                intent,
                {
                    "best_ask": 0.34,
                    "ask_size": 1.0,
                    "asks": [{"price": 0.34, "size": 1.0}, {"price": 0.45, "size": 100.0}],
                },
                taker_fee_rate=0.07,
                limit_price=0.45,
            )

            trade_ids = store.place_execution_result(intent, result)

            self.assertEqual(len(trade_ids), 1)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertEqual(orders[0]["order_type"], "FAK")
            self.assertEqual(orders[0]["fill_count"], 2)
            self.assertEqual(orders[0]["trade_id"], trade_ids[0])
            fills = store.paper_order_fills(orders[0]["id"])
            self.assertEqual([row["price"] for row in fills], [0.34, 0.45])
            self.assertAlmostEqual(sum(row["cash_spent"] for row in fills), orders[0]["cash_spent"], places=5)

    def test_storage_records_rejected_order_without_open_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-reject",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.34, 10.0, "audit reject")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            result = simulate_post_only_buy(intent, {"best_ask": 0.34})

            trade_ids = store.place_execution_result(intent, result)

            self.assertEqual(trade_ids, [])
            self.assertEqual(store.open_trades(), [])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["status"], "REJECTED")
            self.assertEqual(orders[0]["fill_count"], 0)
            self.assertAlmostEqual(orders[0]["cash_spent"], 0.0, places=6)

    def test_bot_orders_page_and_order_fills_are_paginated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-page",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            for index in range(3):
                signal = Signal("BTC", "Up", 0.7, 0.34 + index * 0.01, 10.0, f"audit page {index}")
                intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 2.0})()
                result = simulate_fak_buy(
                    intent,
                    {
                        "best_ask": 0.34 + index * 0.01,
                        "ask_size": 100.0,
                        "asks": [{"price": 0.34 + index * 0.01, "size": 100.0}],
                    },
                    taker_fee_rate=0.07,
                    limit_price=0.5,
                )
                store.place_execution_result(intent, result)

            page = bot.orders_page(limit=2, offset=1)

            self.assertEqual(page["recent_orders_meta"]["total"], 3)
            self.assertEqual(page["recent_orders_meta"]["loaded"], 3)
            self.assertTrue(page["recent_orders_meta"]["has_more"] is False)
            self.assertEqual(len(page["recent_orders"]), 2)
            first_order_id = page["recent_orders"][0]["id"]
            fills = bot.order_fills(first_order_id)
            self.assertEqual(fills["order_id"], first_order_id)
            self.assertEqual(len(fills["fills"]), 1)
            self.assertGreater(fills["fills"][0]["cash_spent"], 0)

    def test_orders_page_filters_by_paper_order_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-order-filter",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.34,
                        "best_ask": 0.36,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.36, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot._maybe_place_trade(market, Signal("BTC", "Up", 0.7, 0.35, 10.0, "active filter"))
            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.36, -10.0, "cancel filter"))

            down_order_id = next(order["id"] for order in store.active_paper_orders("BTC") if order["side"] == "Down")
            store.cancel_paper_order(down_order_id, "filter setup", now=now)
            fill_intent = type("Intent", (), {"market": market, "signal": Signal("BTC", "Up", 0.8, 0.34, 12.0, "filled filter"), "stake_dollars": 2.0})()
            fill_result = simulate_fak_buy(
                fill_intent,
                {"best_ask": 0.34, "ask_size": 100.0, "asks": [{"price": 0.34, "size": 100.0}]},
                taker_fee_rate=0.0,
                limit_price=0.5,
            )
            store.place_execution_result(fill_intent, fill_result)

            active_page = bot.orders_page(status_filter="active")
            canceled_page = bot.orders_page(status_filter="canceled")
            filled_page = bot.orders_page(status_filter="filled")
            all_page = bot.orders_page(status_filter="all")

            self.assertEqual(all_page["recent_orders_meta"]["total"], 3)
            self.assertEqual(active_page["recent_orders_meta"]["status_filter"], "active")
            self.assertEqual([row["status"] for row in active_page["recent_orders"]], ["RESTING"])
            self.assertEqual([row["status"] for row in canceled_page["recent_orders"]], ["CANCELED"])
            self.assertEqual([row["status"] for row in filled_page["recent_orders"]], ["FILLED"])
            with self.assertRaises(ValueError):
                bot.orders_page(status_filter="bad-status")

    def test_paper_pause_cancels_active_orders_and_blocks_new_paper_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-paper-pause-main",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.34,
                        "best_ask": 0.36,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.36, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot._maybe_place_trade(market, Signal("BTC", "Up", 0.7, 0.35, 10.0, "paper pause setup"))
            self.assertEqual(len(store.active_paper_orders("BTC")), 1)

            payload = bot.set_paper_trading_paused(True)

            self.assertTrue(payload["paper_trading"]["paused"])
            self.assertEqual(payload["paper_trading"]["main_cancel"]["canceled_count"], 1)
            self.assertEqual(store.active_paper_orders("BTC"), [])
            canceled_page = bot.orders_page(status_filter="canceled")
            self.assertEqual(canceled_page["recent_orders_meta"]["total"], 1)
            self.assertIn("PAPER_PAUSED", canceled_page["recent_orders"][0]["reason"])

            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.36, -10.0, "blocked while paused"))

            self.assertEqual(store.active_paper_orders("BTC"), [])
            self.assertEqual(bot.orders_page(status_filter="all")["recent_orders_meta"]["total"], 1)
            self.assertIn("PAPER_PAUSED", bot.snapshot()["runtime"]["last_signal"]["reason"])

            bot.set_paper_trading_paused(False)
            bot._maybe_place_trade(market, Signal("BTC", "Down", 0.7, 0.36, -10.0, "after resume"))

            self.assertEqual(len(store.active_paper_orders("BTC")), 1)
            self.assertEqual(bot.orders_page(status_filter="all")["recent_orders_meta"]["total"], 2)

    def test_paper_pause_propagates_to_strategy_experiment_bots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-paper-pause-experiment",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.33,
                    "best_ask": 0.35,
                    "bid_size": 100,
                    "ask_size": 100,
                    "asks": [{"price": 0.35, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.34,
                    "best_ask": 0.36,
                    "bid_size": 100,
                    "ask_size": 100,
                    "asks": [{"price": 0.36, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }
            bot.strategy_experiments.run_from_state(market, price, quotes)
            variant_bot = bot.strategy_experiments._bots["SINGLE_FAK"]
            self.assertEqual(variant_bot.store.open_trade_count("BTC"), 1)

            payload = bot.set_paper_trading_paused(True)

            self.assertTrue(payload["paper_trading"]["paused"])
            self.assertEqual(
                payload["paper_trading"]["strategy_experiments_cancel"]["variants"]["SINGLE_FAK"]["canceled_count"],
                0,
            )
            self.assertTrue(variant_bot.paper_trading_runtime()["paused"])
            self.assertEqual(variant_bot.store.active_paper_orders("BTC"), [])

            bot.strategy_experiments.run_from_state(market, price, quotes)

            self.assertEqual(variant_bot.store.active_paper_orders("BTC"), [])
            self.assertEqual(variant_bot.orders_page(status_filter="all")["recent_orders_meta"]["total"], 1)
            self.assertIn("PAPER_PAUSED", variant_bot.snapshot()["runtime"]["last_signal"]["reason"])

    def test_post_only_rests_reserves_cash_and_later_fills_as_maker_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-resting-fill",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "maker signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            self.assertEqual(store.open_trades(), [])
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 5.0, places=6)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertEqual(orders[0]["post_only"], 1)
            self.assertAlmostEqual(orders[0]["limit_price"], 0.33, places=6)
            order_id = int(orders[0]["id"])
            with store.conn:
                store.conn.execute(
                    "UPDATE paper_orders SET created_at = ?, updated_at = ? WHERE id = ?",
                    (now - 30.0, now - 30.0, order_id),
                )

            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.32,
                        "best_ask": 0.33,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.33, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }
            bot._manage_resting_orders(market, bot.latest_quotes)

            self.assertEqual(store.open_trades(), [])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")

            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.31,
                        "best_ask": 0.32,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.32, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }
            bot._manage_resting_orders(market, bot.latest_quotes)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["entry_price"], 0.33, places=6)
            self.assertIn("POST_ONLY_QUEUE_FILL", rows[0]["reason"])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "PARTIAL_RESTING")
            self.assertGreater(orders[0]["remaining_cash"], 0.0)
            self.assertLess(orders[0]["remaining_cash"], 5.0)
            self.assertGreater(store.metrics()["reserved_cash"], 0.0)
            self.assertLess(store.metrics()["reserved_cash"], 5.0)
            fills = store.paper_order_fills(orders[0]["id"])
            self.assertEqual(len(fills), 1)
            self.assertAlmostEqual(fills[0]["fee"], 0.0, places=6)

    def test_gtc_resting_order_can_fill_without_post_only_queue_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="GTC", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-gtc-resting-fill",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "gtc maker signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertEqual(orders[0]["order_type"], "GTC")
            self.assertEqual(orders[0]["post_only"], 0)
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.32,
                        "best_ask": 0.33,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.33, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }
            bot._manage_resting_orders(market, bot.latest_quotes)

            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["entry_price"], 0.33, places=6)
            self.assertIn("RESTING_FILL", rows[0]["reason"])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertAlmostEqual(orders[0]["remaining_cash"], 0.0, places=6)

    def test_resting_order_partial_fills_update_existing_trade_and_release_dust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="GTC", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-resting-dust-release",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "dust resting")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            store.record_paper_order(
                intent,
                order_type="GTC",
                status="RESTING",
                side="Up",
                limit_price=0.5,
                requested_cash=5.0,
                reason="resting dust",
            )

            first_order = store.active_paper_orders("BTC")[0]
            first_fill = store.fill_resting_order(
                first_order,
                fill_price=0.5,
                shares=5.0,
                notional=2.5,
                fee=0.0,
                cash_spent=2.5,
                level_price=0.49,
                reason="RESTING_FILL first",
                now=now + 1,
            )
            self.assertIsNotNone(first_fill)
            second_order = store.active_paper_orders("BTC")[0]
            second_fill = store.fill_resting_order(
                second_order,
                fill_price=0.5,
                shares=4.92,
                notional=2.46,
                fee=0.0,
                cash_spent=2.46,
                level_price=0.49,
                reason="RESTING_FILL second",
                now=now + 2,
            )

            self.assertIsNotNone(second_fill)
            rows = store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["stake"], 4.96, places=6)
            self.assertAlmostEqual(rows[0]["shares"], 9.92, places=6)
            self.assertAlmostEqual(rows[0]["entry_price"], 0.5, places=6)
            self.assertIn("RESTING_FILL first", rows[0]["reason"])
            self.assertIn("RESTING_FILL second", rows[0]["reason"])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertAlmostEqual(orders[0]["remaining_cash"], 0.0, places=6)
            self.assertAlmostEqual(orders[0]["cash_spent"], 4.96, places=6)
            self.assertIn("DUST_RELEASE", orders[0]["reason"])
            self.assertAlmostEqual(store.account()["cash_balance"], 95.04, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)
            fills = store.paper_order_fills(orders[0]["id"])
            self.assertEqual(len(fills), 2)
            self.assertEqual({fill["trade_id"] for fill in fills}, {rows[0]["id"]})

    def test_resting_order_tiny_initial_fill_does_not_create_dust_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-resting-tiny-initial",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.5, 10.0, "tiny initial")
            intent = type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})()
            store.record_paper_order(
                intent,
                order_type="POST_ONLY",
                status="RESTING",
                side="Up",
                limit_price=0.5,
                requested_cash=5.0,
                post_only=True,
                reason="tiny initial",
            )

            order = store.active_paper_orders("BTC")[0]
            result = store.fill_resting_order(
                order,
                fill_price=0.5,
                shares=0.01,
                notional=0.005,
                fee=0.0,
                cash_spent=0.005,
                level_price=0.49,
                reason="RESTING_FILL tiny",
                now=now + 1,
            )

            self.assertIsNone(result)
            self.assertEqual(store.open_trades(), [])
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertAlmostEqual(orders[0]["remaining_cash"], 5.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)

    def test_gtd_resting_order_expires_and_releases_reserved_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="GTD", paper_gtd_seconds=1.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-gtd-expire",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "gtd signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)

            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "RESTING")
            self.assertLessEqual(orders[0]["expires_at"], now + 2.0)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)

            expired = store.expire_resting_orders(float(orders[0]["expires_at"]) + 0.1)

            self.assertEqual(len(expired), 1)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "EXPIRED")
            self.assertAlmostEqual(store.account()["cash_balance"], 100.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)

    def test_cancel_resting_order_releases_reserved_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-cancel-resting",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Up", 0.7, 0.35, 10.0, "cancel signal")
            with bot._lock:
                bot.current_market = market
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.33,
                        "best_ask": 0.35,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.35, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._maybe_place_trade(market, signal)
            order_id = store.recent_paper_orders(10, 0, "BTC")[0]["id"]

            result = bot.cancel_order(order_id)

            self.assertEqual(result["canceled"], [order_id])
            self.assertEqual(result["not_canceled"], {})
            self.assertAlmostEqual(result["released_cash"], 5.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 100.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(orders[0]["status"], "CANCELED")
            self.assertIn("CANCELED", orders[0]["reason"])

            second = bot.cancel_order(order_id)
            self.assertEqual(second["canceled"], [])
            self.assertIn(str(order_id), second["not_canceled"])

    def test_cancel_orders_scopes_current_market_and_all_active_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", paper_entry_order_type="POST_ONLY", stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            current_market = MarketRound(
                round_id="btc-updown-5m-batch-current",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            other_market = MarketRound(
                round_id="btc-updown-5m-batch-other",
                symbol="BTC",
                started_at=now - 30,
                ends_at=now + 150,
                target_price=101.0,
            )

            def place_resting_order(market: MarketRound, side: str) -> None:
                store.upsert_round(market)
                with bot._lock:
                    bot.current_market = market
                    bot.latest_quotes = {
                        side: {
                            "best_bid": 0.33,
                            "best_ask": 0.35,
                            "bid_size": 100,
                            "ask_size": 100,
                            "asks": [{"price": 0.35, "size": 100}],
                            "updated_at_ms": int(now * 1000),
                        }
                    }
                bot._maybe_place_trade(market, Signal("BTC", side, 0.7, 0.35, 10.0, f"{side} batch cancel"))

            place_resting_order(current_market, "Up")
            place_resting_order(current_market, "Down")
            place_resting_order(other_market, "Up")
            with bot._lock:
                bot.current_market = current_market

            self.assertEqual(len(store.active_paper_orders("BTC")), 3)
            self.assertEqual(len(store.active_paper_orders("BTC", current_market.round_id)), 2)
            self.assertAlmostEqual(store.account()["cash_balance"], 85.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 15.0, places=6)

            current_result = bot.cancel_orders("current_market")

            self.assertEqual(len(current_result["canceled"]), 2)
            self.assertEqual(current_result["not_canceled"], {})
            self.assertAlmostEqual(current_result["released_cash"], 10.0, places=6)
            remaining = store.active_paper_orders("BTC")
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["round_id"], other_market.round_id)
            self.assertAlmostEqual(store.account()["cash_balance"], 95.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 5.0, places=6)

            all_result = bot.cancel_orders("all")

            self.assertEqual(len(all_result["canceled"]), 1)
            self.assertEqual(store.active_paper_orders("BTC"), [])
            self.assertAlmostEqual(all_result["released_cash"], 5.0, places=6)
            self.assertAlmostEqual(store.account()["cash_balance"], 100.0, places=6)
            self.assertAlmostEqual(store.metrics()["reserved_cash"], 0.0, places=6)

    def test_live_snapshot_does_not_promote_client_fallback_target_to_market_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_confidence=0.55, min_edge=0.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-4000",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=0.0,
                slug="btc-updown-5m-4000",
            )
            bot.polymarket.find_current_btc_5m_market = lambda: market
            payload = {
                "market": {"slug": market.round_id},
                "target_price": 100.0,
                "price": {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int(now * 1000),
                    "target_price": 100.0,
                    "target_price_source": "rtds-chainlink-fallback",
                    "target_price_fallback": True,
                },
                "quotes": {
                    "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                },
            }

            result = bot.ingest_live_snapshot(payload)
            snapshot = bot.snapshot()

            self.assertTrue(result["ok"])
            self.assertLess(len(json.dumps(result)), 1000)
            self.assertEqual(snapshot["runtime"]["current_market"]["target_price"], 0.0)
            self.assertNotIn("target_price", snapshot["runtime"]["latest_price"])
            self.assertEqual(snapshot["runtime"]["last_signal"], {})
            self.assertEqual(store.open_trades(), [])

    def test_live_snapshot_uses_official_market_target_over_client_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", min_confidence=0.55, min_edge=0.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-4100",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                slug="btc-updown-5m-4100",
            )
            bot.polymarket.find_current_btc_5m_market = lambda: market
            payload = {
                "market": {"slug": market.round_id},
                "target_price": 99.0,
                "price": {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int(now * 1000),
                    "target_price": 99.0,
                    "target_price_source": "rtds-chainlink-fallback",
                    "target_price_fallback": True,
                },
                "quotes": {
                    "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                },
            }

            result = bot.ingest_live_snapshot(payload)
            snapshot = bot.snapshot()

            self.assertTrue(result["ok"])
            self.assertLess(len(json.dumps(result)), 1000)
            self.assertEqual(snapshot["runtime"]["current_market"]["target_price"], 100.0)
            self.assertEqual(snapshot["runtime"]["latest_price"]["target_price"], 100.0)
            self.assertEqual(snapshot["runtime"]["latest_price"]["target_price_source"], "market.target_price")
            self.assertFalse(snapshot["runtime"]["latest_price"]["target_price_fallback"])
            self.assertEqual(snapshot["runtime"]["paper_price"], {})
            self.assertEqual(snapshot["runtime"]["paper_quotes"], {})
            self.assertEqual(snapshot["runtime"]["execution_price"], {})
            self.assertEqual(snapshot["runtime"]["execution_quotes"], {})

            paper_price, paper_quotes, paper_source = bot._paper_market_data()
            self.assertEqual(paper_price, {})
            self.assertEqual(paper_quotes, {})
            self.assertEqual(paper_source, "backend")
            execution_price, execution_quotes, execution_source = bot._execution_market_data()
            self.assertEqual(execution_price, {})
            self.assertEqual(execution_quotes, {})
            self.assertEqual(execution_source, "backend")

    def test_live_snapshot_does_not_drive_paper_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                min_confidence=0.55,
                min_edge=0.0,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
                live_trading_runtime_enabled=False,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-paper-isolated",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                slug="btc-updown-5m-paper-isolated",
            )
            bot.polymarket.find_current_btc_5m_market = lambda: market
            snapshot_payload = {
                "market": {"slug": market.round_id},
                "price": {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)},
                "quotes": {
                    "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                },
            }

            calls = []
            bot._run_strategy_from_state = lambda: calls.append("strategy")

            result = bot.ingest_live_snapshot(snapshot_payload)

            self.assertTrue(result["ok"])
            self.assertEqual(calls, [])
            self.assertEqual(store.open_trades(), [])
            snapshot = bot.snapshot()
            self.assertEqual(snapshot["runtime"]["paper_price"], {})
            self.assertEqual(snapshot["runtime"]["paper_quotes"], {})
            self.assertEqual(snapshot["runtime"]["market_data_scope"]["paper"], "backend_only")

    def test_backend_market_data_refreshes_when_quotes_exceed_strategy_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_runtime_enabled=False,
                max_quote_age_ms=3_000,
                live_snapshot_max_age_seconds=8.0,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-backend-feed",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 180,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            stale_quote_ms = now_ms - settings.max_quote_age_ms - 500
            with bot._lock:
                bot.current_market = market
                bot.ws_status["browser_feed_at"] = now
                bot.latest_price = {"binance": 101.0, "binance_updated_ms": now_ms}
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.50, "best_ask": 0.52, "updated_at_ms": stale_quote_ms},
                    "Down": {"best_bid": 0.48, "best_ask": 0.50, "updated_at_ms": stale_quote_ms},
                }
                bot.paper_price = {"binance": 101.0, "binance_updated_ms": now_ms}
                bot.paper_quotes = {
                    "Up": {"best_bid": 0.50, "best_ask": 0.52, "updated_at_ms": stale_quote_ms},
                    "Down": {"best_bid": 0.48, "best_ask": 0.50, "updated_at_ms": stale_quote_ms},
                }
                bot.execution_price = {"binance": 101.0, "binance_updated_ms": now_ms}
                bot.execution_quotes = {
                    "Up": {"best_bid": 0.50, "best_ask": 0.52, "updated_at_ms": stale_quote_ms},
                    "Down": {"best_bid": 0.48, "best_ask": 0.50, "updated_at_ms": stale_quote_ms},
                }

            refresh_calls = []

            def fake_rest_fallback_snapshot(snapshot_market):
                refresh_calls.append(snapshot_market.round_id)
                fresh_ms = int(time.time() * 1000)
                with bot._lock:
                    bot.latest_price = {"binance": 101.0, "binance_updated_ms": fresh_ms}
                    bot.latest_quotes = {
                        "Up": {"best_bid": 0.50, "best_ask": 0.52, "updated_at_ms": fresh_ms},
                        "Down": {"best_bid": 0.48, "best_ask": 0.50, "updated_at_ms": fresh_ms},
                    }
                    bot.paper_price = dict(bot.latest_price)
                    bot.paper_quotes = dict(bot.latest_quotes)
                    bot.execution_price = dict(bot.latest_price)
                    bot.execution_quotes = dict(bot.latest_quotes)

            bot._refresh_market = lambda: market
            bot._rest_fallback_snapshot = fake_rest_fallback_snapshot
            bot._settle_due = lambda _now: None
            bot._reconcile_official_settlements = lambda _now: None
            bot._backfill_official_final_prices = lambda _now: None
            bot._run_strategy_from_state = lambda: None

            self.assertFalse(bot._live_feed_stale(now))
            self.assertFalse(bot._price_feed_stale(now))
            self.assertTrue(bot._backend_market_data_refresh_needed(now))

            bot.tick()

            self.assertEqual(refresh_calls, [market.round_id])
            paper_price, paper_quotes, paper_source = bot._paper_market_data()
            self.assertEqual(paper_source, "backend")
            self.assertIn("binance_updated_ms", paper_price)
            self.assertGreater(int(paper_quotes["Up"].get("updated_at_ms") or 0), stale_quote_ms)

    def test_clob_ws_orderbook_applies_book_and_price_changes(self) -> None:
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-clob-ws-book",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 180,
            target_price=100.0,
            up_token="up-token",
            down_token="down-token",
        )
        book = ClobMarketOrderBook.for_market(market)

        changed = book.apply_payload(
            {
                "event_type": "book",
                "asset_id": "up-token",
                "bids": [{"price": "0.49", "size": "20"}, {"price": "0.50", "size": "10"}],
                "asks": [{"price": "0.52", "size": "8"}, {"price": "0.51", "size": "12"}],
                "timestamp": "1780120000000",
            },
            now,
        )

        self.assertEqual(changed["Up"]["best_bid"], 0.5)
        self.assertEqual(changed["Up"]["bid_size"], 10.0)
        self.assertEqual(changed["Up"]["best_ask"], 0.51)
        self.assertEqual(changed["Up"]["ask_size"], 12.0)
        self.assertEqual(changed["Up"]["source"], "clob-ws-book")
        self.assertEqual(changed["Up"]["updated_at_ms"], int(now * 1000))
        self.assertEqual(changed["Up"]["clob_received_ms"], int(now * 1000))
        self.assertEqual(changed["Up"]["clob_event_updated_ms"], 1780120000000)

        changed = book.apply_payload(
            {
                "event_type": "price_change",
                "timestamp": "1780120000500",
                "price_changes": [
                    {
                        "asset_id": "up-token",
                        "side": "BUY",
                        "price": "0.50",
                        "size": "0",
                        "best_bid": "0.49",
                        "best_ask": "0.51",
                    },
                    {
                        "asset_id": "up-token",
                        "side": "SELL",
                        "price": "0.51",
                        "size": "5",
                        "best_bid": "0.49",
                        "best_ask": "0.51",
                    },
                ],
            },
            now,
        )

        self.assertEqual(changed["Up"]["best_bid"], 0.49)
        self.assertEqual(changed["Up"]["best_ask"], 0.51)
        self.assertEqual(changed["Up"]["ask_size"], 5.0)
        self.assertEqual(changed["Up"]["source"], "clob-ws-price-change")
        self.assertEqual(changed["Up"]["updated_at_ms"], int(now * 1000))
        self.assertEqual(changed["Up"]["clob_received_ms"], int(now * 1000))
        self.assertEqual(changed["Up"]["clob_event_updated_ms"], 1780120000500)

    def test_clob_ws_orderbook_uses_receive_time_for_strategy_freshness(self) -> None:
        now = 1780120010.25
        market = MarketRound(
            round_id="btc-updown-5m-clob-ws-receive-time",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 180,
            target_price=100.0,
            up_token="up-token",
            down_token="down-token",
        )
        book = ClobMarketOrderBook.for_market(market)

        changed = book.apply_payload(
            {
                "event_type": "price_change",
                "timestamp": "1780120000000",
                "price_changes": [
                    {
                        "asset_id": "up-token",
                        "side": "BUY",
                        "price": "0.50",
                        "size": "10",
                        "best_bid": "0.50",
                        "best_ask": "0.51",
                    }
                ],
            },
            now,
        )

        self.assertEqual(changed["Up"]["updated_at_ms"], 1780120010250)
        self.assertEqual(changed["Up"]["clob_received_ms"], 1780120010250)
        self.assertEqual(changed["Up"]["clob_event_updated_ms"], 1780120000000)

    def test_rtds_chainlink_parser_reads_crypto_price_payload(self) -> None:
        tick = rtds_chainlink_price_from_payload(
            {
                "topic": "crypto_prices_chainlink",
                "payload": {
                    "data": [
                        {
                            "symbol": "btc/usd",
                            "value": "101.25",
                            "timestamp": 1780120000,
                        }
                    ]
                },
            },
            now=1780120001.0,
        )

        self.assertIsNotNone(tick)
        assert tick is not None
        self.assertEqual(tick["chainlink"], 101.25)
        self.assertEqual(tick["chainlink_updated_ms"], 1780120000000)
        self.assertEqual(tick["source"], "polymarket-rtds-chainlink")

    def test_spot_ws_parsers_use_receive_time_for_strategy_freshness(self) -> None:
        okx = okx_spot_price_from_payload(
            {
                "arg": {"channel": "tickers", "instId": "BTC-USDT"},
                "data": [{"instId": "BTC-USDT", "last": "101.25", "ts": "1780120000000"}],
            },
            now=1780120010.5,
        )
        binance = binance_spot_price_from_payload(
            {"s": "BTCUSDT", "c": "101.30", "E": 1780120000000},
            now=1780120010.5,
        )

        self.assertIsNotNone(okx)
        self.assertIsNotNone(binance)
        assert okx is not None
        assert binance is not None
        self.assertEqual(okx["okx"], 101.25)
        self.assertEqual(okx["okx_updated_ms"], 1780120010500)
        self.assertEqual(okx["okx_received_ms"], 1780120010500)
        self.assertEqual(okx["okx_exchange_updated_ms"], 1780120000000)
        self.assertEqual(binance["binance_market"], 101.30)
        self.assertEqual(binance["binance_market_updated_ms"], 1780120010500)
        self.assertEqual(binance["binance_market_received_ms"], 1780120010500)
        self.assertEqual(binance["binance_market_exchange_updated_ms"], 1780120000000)

    def test_backend_clob_ws_quotes_feed_paper_and_live_without_rest_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_runtime_enabled=False,
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            fresh_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-clob-ws-ingest",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 180,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.ws_status["browser_feed_at"] = now - 120

            quotes = {
                "Up": {
                    "token_id": "up-token",
                    "best_bid": 0.53,
                    "best_ask": 0.54,
                    "bid_size": 10,
                    "ask_size": 20,
                    "bids": [{"price": 0.53, "size": 10}],
                    "asks": [{"price": 0.54, "size": 20}],
                    "updated_at_ms": fresh_ms,
                    "source": "clob-ws-book",
                },
                "Down": {
                    "token_id": "down-token",
                    "best_bid": 0.45,
                    "best_ask": 0.46,
                    "bid_size": 30,
                    "ask_size": 40,
                    "bids": [{"price": 0.45, "size": 30}],
                    "asks": [{"price": 0.46, "size": 40}],
                    "updated_at_ms": fresh_ms,
                    "source": "clob-ws-book",
                },
            }

            bot._ingest_backend_clob_ws_quotes(market, quotes, {"state": "message", "event_type": "book", "at": now})

            self.assertFalse(bot._backend_quote_refresh_needed(now))
            paper_price, paper_quotes, paper_source = bot._paper_market_data()
            execution_price, execution_quotes, execution_source = bot._execution_market_data()
            self.assertEqual(paper_source, "backend")
            self.assertEqual(execution_source, "backend")
            self.assertEqual(paper_price, {})
            self.assertEqual(execution_price, {})
            self.assertEqual(paper_quotes["Up"]["source"], "clob-ws-book")
            self.assertEqual(execution_quotes["Down"]["best_ask"], 0.46)
            snapshot = bot.snapshot()
            self.assertEqual(snapshot["runtime"]["ws_status"]["market"], "clob-ws")
            self.assertEqual(snapshot["runtime"]["ws_status"]["backend_clob_ws"], "message")

    def test_backend_quote_refresh_needed_when_one_side_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_runtime_enabled=False,
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            fresh_ms = int(now * 1000)
            stale_ms = int((now - 10) * 1000)
            with bot._lock:
                bot.execution_quotes = {
                    "Up": {
                        "best_bid": 0.53,
                        "best_ask": 0.54,
                        "updated_at_ms": fresh_ms,
                    },
                    "Down": {
                        "best_bid": 0.45,
                        "best_ask": 0.46,
                        "updated_at_ms": stale_ms,
                    },
                }
                bot._last_backend_quote_refresh_at = now - 2

            self.assertTrue(bot._backend_quote_refresh_needed(now))

    def test_backend_clob_ws_stale_quote_does_not_suppress_rest_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_runtime_enabled=False,
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            stale_ms = int((now - 10) * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-clob-ws-stale-ingest",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 180,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot._last_backend_quote_refresh_at = now - 2

            quotes = {
                "Up": {
                    "token_id": "up-token",
                    "best_bid": 0.53,
                    "best_ask": 0.54,
                    "updated_at_ms": stale_ms,
                    "source": "clob-ws-book",
                },
                "Down": {
                    "token_id": "down-token",
                    "best_bid": 0.45,
                    "best_ask": 0.46,
                    "updated_at_ms": stale_ms,
                    "source": "clob-ws-book",
                },
            }

            bot._ingest_backend_clob_ws_quotes(market, quotes, {"state": "message", "event_type": "book", "at": now})

            self.assertTrue(bot._backend_quote_refresh_needed(now))

    def test_backend_chainlink_and_rest_prices_feed_basis_to_execution_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_runtime_enabled=False,
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-rtds-basis",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 180,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market

            class FakePriceClient:
                def fetch_sources(self, symbol, now_arg):
                    return {
                        "binance": PriceTick(symbol, 101.0, "binance", now_arg),
                        "okx": PriceTick(symbol, 102.0, "okx", now_arg),
                    }

                def fetch_symbol(self, symbol, now_arg):
                    return PriceTick(symbol, 101.0, "binance", now_arg)

            bot.market_data_price_fallback = FakePriceClient()
            bot._ingest_backend_chainlink_price(
                {
                    "chainlink": 100.0,
                    "chainlink_updated_ms": int(now * 1000),
                    "source": "polymarket-rtds-chainlink",
                },
                {"state": "message", "topic": "crypto_prices_chainlink", "at": now},
            )
            bot._refresh_backend_prices(market)

            paper_price, _paper_quotes, paper_source = bot._paper_market_data()
            execution_price, _execution_quotes, execution_source = bot._execution_market_data()

            self.assertEqual(paper_source, "compat_latest_without_browser")
            self.assertEqual(execution_source, "backend")
            self.assertAlmostEqual(execution_price["chainlink"], 100.0)
            self.assertEqual(execution_price["okx_basis_samples"], 1)
            self.assertEqual(execution_price["binance_basis_samples"], 1)
            self.assertAlmostEqual(execution_price["okx_basis_median_bps"], 200.0, places=6)
            self.assertAlmostEqual(execution_price["binance_basis_median_bps"], 100.0, places=6)
            self.assertEqual(paper_price["okx_basis_samples"], 1)
            self.assertEqual(bot.snapshot()["runtime"]["ws_status"]["backend_rtds_ws"], "message")

    def test_backend_spot_ws_prices_keep_execution_fresh_with_old_exchange_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_runtime_enabled=False,
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-spot-ws-ingest",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 180,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.execution_price = {
                    "chainlink": 100.0,
                    "chainlink_updated_ms": now_ms,
                    "target_price": 100.0,
                }

            bot._ingest_backend_spot_price(
                {
                    "okx": 101.0,
                    "okx_updated_ms": now_ms,
                    "okx_received_ms": now_ms,
                    "okx_exchange_updated_ms": now_ms - 10_000,
                    "okx_source": "okx-spot-ws",
                },
                {"source": "okx", "state": "message", "at": now},
            )

            execution_price, _execution_quotes, execution_source = bot._execution_market_data()
            self.assertEqual(execution_source, "backend")
            self.assertEqual(execution_price["okx_updated_ms"], now_ms)
            self.assertEqual(execution_price["okx_exchange_updated_ms"], now_ms - 10_000)
            self.assertEqual(execution_price["okx_basis_samples"], 1)
            self.assertTrue(bot._backend_price_refresh_needed(now + 0.1))

            rows = _live_basis_rows({**execution_price, "okx_basis_samples": 5}, now_ms, ("okx",))
            okx = next(row for row in rows if row["source"] == "okx")
            self.assertTrue(okx["ready"])
            self.assertEqual(okx["age_ms"], 0)
            snapshot = bot.snapshot()
            self.assertEqual(snapshot["runtime"]["ws_status"]["backend_okx_ws"], "message")
            self.assertEqual(snapshot["runtime"]["ws_status"]["backend_okx_ws_exchange_age_ms"], 10_000)

    def test_backend_price_refresh_runs_when_chainlink_is_fresh_but_fallback_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_runtime_enabled=False,
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            with bot._lock:
                bot.execution_price = {"chainlink": 100.0, "chainlink_updated_ms": int(now * 1000)}
                bot._last_backend_price_refresh_at = now - 2.0

            self.assertTrue(bot._backend_price_refresh_needed(now))

    def test_backend_price_refresh_checks_each_selected_live_fallback_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            assert bot.live_trading is not None
            bot.live_trading.update_settings({"fallback_sources": ["okx", "binance"]})
            now = time.time()
            with bot._lock:
                bot.execution_price = {
                    "binance_market": 101.0,
                    "binance_market_updated_ms": int(now * 1000),
                    "binance_updated_ms": int(now * 1000),
                    "okx": 102.0,
                    "okx_updated_ms": int((now - 10.0) * 1000),
                }
                bot._last_backend_price_refresh_at = now - 2.0

            self.assertTrue(bot._backend_price_refresh_needed(now))

            bot.live_trading.update_settings({"fallback_sources": ["binance"]})
            self.assertFalse(bot._backend_price_refresh_needed(now))

    def test_status_snapshot_does_not_hold_bot_lock_during_live_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                max_quote_age_ms=3_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            assert bot.live_trading is not None
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-status-lock",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot._last_live_snapshot_ingest_at = 0.0

            entered = threading.Event()
            release = threading.Event()
            snapshot_errors: list[BaseException] = []

            def slow_live_snapshot(*, refresh_external: bool = True) -> dict[str, Any]:
                entered.set()
                release.wait(2.0)
                return {
                    "enabled": False,
                    "variant_id": LIVE_VARIANT_ID,
                    "combo": "SINGLE + FAK REAL",
                    "execution_mode": "LIVE",
                    "db_path": str(settings.live_trading_db_path),
                    "settings_path": str(settings.live_trading_settings_path),
                    "process_lock_path": str(bot.live_trading.process_lock.path),
                    "process_lock_acquired": False,
                    "process_lock": {},
                    "run_count": 0,
                    "last_run_at": None,
                    "overlap_skip_count": 0,
                    "last_signal": {},
                    "last_error": None,
                    "startup_rearmed": False,
                    "last_order_at": None,
                    "last_order": {},
                    "readiness": fake_client.readiness(required_cash=5.0),
                    "open_orders": fake_client.open_orders_state(),
                    "settings": {"enabled": False, "fallback_sources": []},
                    "variant": {"variant_id": LIVE_VARIANT_ID, "metrics": {}},
                    "variants": [{"variant_id": LIVE_VARIANT_ID, "metrics": {}}],
                }

            bot.live_trading.snapshot = slow_live_snapshot  # type: ignore[method-assign]

            def run_snapshot() -> None:
                try:
                    bot.snapshot()
                except BaseException as exc:  # noqa: BLE001 - surface thread failures to the assertion below.
                    snapshot_errors.append(exc)

            snapshot_thread = threading.Thread(target=run_snapshot, daemon=True)
            snapshot_thread.start()
            self.assertTrue(entered.wait(1.0))

            ingest_errors: list[BaseException] = []

            def run_ingest() -> None:
                try:
                    bot.ingest_live_snapshot(
                        {
                            "market": {"slug": market.round_id},
                            "price": {"chainlink": 101.0, "chainlink_updated_ms": int(time.time() * 1000)},
                            "quotes": {},
                            "market_ws_status": "test",
                            "price_ws_status": "test",
                        }
                    )
                except BaseException as exc:  # noqa: BLE001 - surface thread failures to the assertion below.
                    ingest_errors.append(exc)

            ingest_thread = threading.Thread(target=run_ingest, daemon=True)
            ingest_thread.start()
            ingest_thread.join(0.5)
            self.assertFalse(ingest_thread.is_alive(), "live snapshot ingest was blocked by status snapshot")
            release.set()
            snapshot_thread.join(2.0)
            self.assertFalse(snapshot_thread.is_alive())
            self.assertEqual([], ingest_errors)
            self.assertEqual([], snapshot_errors)

    def test_dashboard_live_snapshot_refreshes_in_background(self) -> None:
        class SlowSnapshotBot:
            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()
                self.calls = 0

            def ingest_live_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
                self.calls += 1
                self.entered.set()
                self.release.wait(2.0)
                return {"ok": True, "seq": self.calls, "market": payload.get("market", {})}

        bot = SlowSnapshotBot()
        server = DashboardServer(("127.0.0.1", 0), Handler, bot)  # type: ignore[arg-type]
        try:
            payload = {"market": {"slug": "btc-updown-5m-web-live-snapshot"}, "price": {}, "quotes": {}}
            started = time.perf_counter()
            result = server.live_snapshot(payload)
            first_elapsed = time.perf_counter() - started

            self.assertTrue(result["accepted_snapshot"])
            self.assertLess(first_elapsed, 0.5)
            self.assertTrue(bot.entered.wait(1.0))

            started = time.perf_counter()
            second_result = server.live_snapshot(payload)
            second_elapsed = time.perf_counter() - started

            self.assertTrue(second_result["accepted_snapshot"])
            self.assertLess(second_elapsed, 0.5)
            self.assertEqual(bot.calls, 1)

            bot.release.set()
            deadline = time.time() + 2.0
            cached: dict[str, Any] | None = None
            while time.time() < deadline:
                with server._live_snapshot_lock:
                    cached = dict(server._live_snapshot_cache or {})
                if cached:
                    break
                time.sleep(0.01)
            self.assertEqual(cached.get("seq"), 1)

            bot.entered.clear()
            bot.release.clear()
            with server._live_snapshot_lock:
                server._live_snapshot_cache_at = 0.0

            started = time.perf_counter()
            stale_result = server.live_snapshot(payload)
            stale_elapsed = time.perf_counter() - started

            self.assertEqual(stale_result["seq"], 1)
            self.assertTrue(stale_result["server_refreshing_snapshot"])
            self.assertLess(stale_elapsed, 0.5)
            self.assertTrue(bot.entered.wait(1.0))
        finally:
            bot.release.set()
            server.server_close()

    def test_live_snapshot_ignores_expired_market_without_sync_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-expired-live-snapshot",
                symbol="BTC",
                started_at=now - 360,
                ends_at=now - 60,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market

            refresh_calls: list[bool] = []

            def fail_refresh() -> None:
                refresh_calls.append(True)
                raise AssertionError("live snapshot must not refresh market synchronously")

            bot._refresh_market = fail_refresh  # type: ignore[method-assign]
            result = bot.ingest_live_snapshot(
                {
                    "market": {"slug": market.round_id},
                    "price": {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)},
                    "quotes": {},
                }
            )

            self.assertEqual(result["ignored_snapshot"], "expired_market")
            self.assertEqual(refresh_calls, [])

    def test_pair_strategy_does_not_open_without_official_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=2,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-no-target-pair",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=0.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot.set_pair_strategy_enabled(True)

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            self.assertEqual(bot.last_signal["side"], "PAIR_WAIT")
            self.assertIn("缺少官方目标价", bot.last_signal["reason"])

    def test_pair_strategy_opens_two_sides_and_exits_on_bid_sum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", max_open_trades=2, stake_dollars=5.0)
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-3000",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot.set_pair_strategy_enabled(True)
            bot._run_strategy_from_state()
            open_rows = sorted(store.open_trades(), key=lambda row: row["side"])
            self.assertEqual([row["side"] for row in open_rows], ["Down", "Up"])
            self.assertAlmostEqual(open_rows[0]["shares"], open_rows[1]["shares"], places=4)
            self.assertTrue(all("PAIR_OPEN" in row["reason"] for row in open_rows))
            self.assertTrue(all("fee" in row["reason"] for row in open_rows))

            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.5, "best_ask": 0.51, "ask_size": 100, "updated_at_ms": int(time.time() * 1000)},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "ask_size": 100, "updated_at_ms": int(time.time() * 1000)},
                }
            bot._run_strategy_from_state()
            self.assertEqual(store.open_trades(), [])
            recent = store.recent_trades(2)
            self.assertEqual(len(recent), 2)
            self.assertTrue(all("PAIR_EXIT" in row["reason"] for row in recent))
            self.assertTrue(all("fee" in row["reason"] for row in recent))
            self.assertGreater(store.metrics()["realized_pnl"], 0)

    def test_strategy_variants_cover_target_combinations_and_single_fak_modes(self) -> None:
        combos = [variant.combo for variant in STRATEGY_VARIANTS]

        self.assertEqual(
            combos,
            [
                "SINGLE + FAK",
                "SINGLE + FAK Reverse",
                "SINGLE + FAK Aggressive Edge",
                "SINGLE + FAK Aggressive Edge V1",
                "SINGLE + FAK Aggressive Edge V2",
                "SINGLE + FAK Aggressive Edge V3",
                "SINGLE + FAK Aggressive Edge Diagnostic",
                "SINGLE + FAK Aggressive Edge V4 Diagnostic",
                "SINGLE + FAK Aggressive Edge V5 Diagnostic",
                "SINGLE + FAK Aggressive Edge V6 Diagnostic",
                "SINGLE + FAK Aggressive Edge V7 Diagnostic",
                "SINGLE + FAK Aggressive Edge V8 Learning Diagnostic",
                "SINGLE + FAK Aggressive Edge V9 M1 Guard Diagnostic",
                "SINGLE + FAK Aggressive Edge V10 Up Reversal Guard Diagnostic",
                "SINGLE + FAK Aggressive Edge V11 Depth Momentum Diagnostic",
                "SINGLE + FAK Aggressive Edge V12 Reversal Guard Diagnostic",
                "SINGLE + FAK CHAINLINK_ONLY",
                "SINGLE + FAK CHAINLINK_ONLY ANTI_BOT_GUARD",
                "SINGLE + FAK FALLBACK_ONLY",
                "SINGLE + FAK REVERSAL",
                "SINGLE + FAK STOP_AND_FLIP",
            ],
        )

    def test_selected_strategy_variants_ignore_deprecated_variants(self) -> None:
        selected = selected_strategy_variants("PAIR_GTD,SINGLE_FAK,SINGLE_GTD,SINGLE_FAK_MULTI_LEAD")
        self.assertEqual([variant.variant_id for variant in selected], ["SINGLE_FAK"])

        selected_only_deprecated = selected_strategy_variants(
            "PAIR_GTD,PAIR_POST_ONLY,SINGLE_GTC,SINGLE_FAK_STRICT,SINGLE_FAK_AGGRESSIVE_EDGE_V3"
        )
        self.assertEqual(selected_only_deprecated, tuple())

        active_ids = {variant.variant_id for variant in active_strategy_variants()}
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V4_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V6_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V7_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V8_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V9_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V10_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V11_DIAGNOSTIC", active_ids)
        self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC", active_ids)
        self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE", active_ids)
        self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE_V1", active_ids)
        self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE_V2", active_ids)
        self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE_V3", active_ids)

    def test_pair_strategy_gtd_places_two_resting_pair_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=2,
                stake_dollars=5.0,
                max_quote_age_ms=60_000,
                paper_entry_order_type="GTD",
                paper_gtd_seconds=60.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-pair-gtd",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot.set_pair_strategy_enabled(True)

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            active_orders = sorted(store.active_paper_orders("BTC"), key=lambda row: row["side"])
            self.assertEqual([row["side"] for row in active_orders], ["Down", "Up"])
            self.assertTrue(all(row["order_type"] == "GTD" for row in active_orders))
            self.assertTrue(all(row["status"] == "RESTING" for row in active_orders))
            self.assertTrue(all("PAIR_OPEN_RESTING GTD" in row["reason"] for row in active_orders))
            self.assertAlmostEqual(sum(row["requested_cash"] for row in active_orders), 5.0, places=5)
            self.assertLessEqual(active_orders[0]["expires_at"], float(active_orders[0]["created_at"]) + 61.0)
            self.assertEqual(bot.last_signal["side"], "PAIR_RESTING")

            bot._run_strategy_from_state()

            self.assertEqual(len(store.active_paper_orders("BTC")), 2)

    def test_pair_strategy_post_only_places_two_maker_pair_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=2,
                stake_dollars=5.0,
                paper_entry_order_type="POST_ONLY",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-pair-post-only",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }
            bot.set_pair_strategy_enabled(True)

            bot._run_strategy_from_state()

            active_orders = sorted(store.active_paper_orders("BTC"), key=lambda row: row["side"])
            self.assertEqual(len(active_orders), 2)
            self.assertTrue(all(row["order_type"] == "POST_ONLY" for row in active_orders))
            self.assertTrue(all(row["post_only"] == 1 for row in active_orders))
            self.assertLess(active_orders[0]["limit_price"], 0.5)
            self.assertLess(active_orders[1]["limit_price"], 0.4)
            self.assertEqual(store.open_trades(), [])

    def test_realtime_maker_places_post_only_and_exits_on_profit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=1,
                stake_dollars=5.0,
                max_quote_age_ms=3000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.realtime_maker_enabled = True
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-realtime-maker",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 180,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int(now * 1000),
                    "realtime_probability": {"combined_up": 0.65, "updated_at_ms": int(now * 1000)},
                    "actor_probability": {"combined_up": 0.62, "checked_at": now, "status": "READY"},
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.53,
                        "best_ask": 0.55,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.55, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.44,
                        "best_ask": 0.46,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.46, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }

            bot._run_strategy_from_state()

            active_orders = store.active_paper_orders("BTC", market.round_id)
            self.assertEqual(len(active_orders), 1)
            order = active_orders[0]
            self.assertEqual(order["side"], "Up")
            self.assertEqual(order["order_type"], "POST_ONLY")
            self.assertEqual(order["post_only"], 1)
            self.assertIn("REALTIME_MAKER_PAPER", order["reason"])

            limit_price = float(order["limit_price"])
            cash_spent = float(order["remaining_cash"])
            shares = round(cash_spent / limit_price, 6)
            filled = store.fill_resting_order(
                order,
                fill_price=limit_price,
                shares=shares,
                notional=round(shares * limit_price, 6),
                fee=0.0,
                cash_spent=round(shares * limit_price, 6),
                level_price=limit_price,
                reason="test maker fill",
                now=now + 10,
            )
            self.assertIsNotNone(filled)
            self.assertEqual(len(store.open_trades()), 1)

            with bot._lock:
                bot.latest_price = {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int((now + 12) * 1000),
                    "realtime_probability": {"combined_up": 0.60, "updated_at_ms": int((now + 12) * 1000)},
                    "actor_probability": {"combined_up": 0.60, "checked_at": now + 12, "status": "READY"},
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.595,
                        "best_ask": 0.61,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.61, "size": 100}],
                        "updated_at_ms": int((now + 12) * 1000),
                    },
                    "Down": {
                        "best_bid": 0.39,
                        "best_ask": 0.41,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.41, "size": 100}],
                        "updated_at_ms": int((now + 12) * 1000),
                    },
                }

            bot._run_strategy_from_state()

            self.assertEqual(store.open_trades(), [])
            recent = store.recent_trades(1)
            self.assertEqual(recent[0]["status"], "SETTLED")
            self.assertGreater(recent[0]["pnl"], 0)
            self.assertIn("REALTIME_MAKER_PAPER_EXIT Up TAKE_PROFIT", recent[0]["reason"])

    def test_realtime_maker_keeps_young_order_when_edge_temporarily_decays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=1,
                stake_dollars=5.0,
                max_quote_age_ms=3000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.realtime_maker_enabled = True
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-realtime-maker-grace",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 180,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int(now * 1000),
                    "realtime_probability": {"combined_up": 0.65, "updated_at_ms": int(now * 1000)},
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.53,
                        "best_ask": 0.70,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.70, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.29,
                        "best_ask": 0.31,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.31, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }

            bot._run_strategy_from_state()
            order = store.active_paper_orders("BTC", market.round_id)[0]
            order_id = int(order["id"])
            limit_price = float(order["limit_price"])

            mild_edge_decay_fair = round(limit_price + 0.005, 4)
            with bot._lock:
                bot.latest_price = {
                    "chainlink": 101.0,
                    "chainlink_updated_ms": int(time.time() * 1000),
                    "realtime_probability": {
                        "combined_up": mild_edge_decay_fair,
                        "updated_at_ms": int(time.time() * 1000),
                    },
                }
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.52,
                        "best_ask": 0.70,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.70, "size": 100}],
                        "updated_at_ms": int(time.time() * 1000),
                    },
                    "Down": {
                        "best_bid": 0.29,
                        "best_ask": 0.31,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.31, "size": 100}],
                        "updated_at_ms": int(time.time() * 1000),
                    },
                }

            bot._run_strategy_from_state()
            active_orders = store.active_paper_orders("BTC", market.round_id)
            self.assertEqual([int(row["id"]) for row in active_orders], [order_id])

            old_created_at = time.time() - 20.0
            with store.conn:
                store.conn.execute(
                    "UPDATE paper_orders SET created_at = ?, updated_at = ?, expires_at = ? WHERE id = ?",
                    (old_created_at, old_created_at, time.time() + 60.0, order_id),
                )

            bot._run_strategy_from_state()
            row = store.conn.execute("SELECT status, reason FROM paper_orders WHERE id = ?", (order_id,)).fetchone()
            self.assertEqual(row["status"], STATUS_CANCELED)
            self.assertIn("edge decayed", row["reason"])

    def test_llm_super_agent_paper_uses_local_router_and_records_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=2,
                stake_dollars=5.0,
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                llm_super_agent_api_key="",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.llm_super_agent_enabled = True
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-llm-super-agent",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }

            bot._run_strategy_from_state()

            self.assertEqual(bot.last_signal["side"], "Up")
            self.assertIn("LLM_SUPER_AGENT route SINGLE_FAK_STOP_AND_FLIP", bot.last_signal["reason"])
            self.assertEqual(len(store.open_trades()), 1)
            orders = store.recent_paper_orders(10, 0, "BTC")
            self.assertEqual(len([row for row in orders if row["status"] == STATUS_FILLED]), 1)
            decisions = store.recent_llm_decisions(5)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["variant_id"], "MAIN")
            self.assertEqual(decisions[0]["route"], "SINGLE_FAK_STOP_AND_FLIP")
            self.assertEqual(decisions[0]["allow_trade"], 1)
            self.assertIn("pair_cost", decisions[0]["features_json"])

    def test_snapshot_exposes_llm_status_without_secret_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret = "haoai-secret-for-test"
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                llm_super_agent_api_key=secret,
                llm_super_agent_base_url="https://api.hao.ai/v1",
                llm_super_agent_model="openai/gpt-5.4-mini",
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)

            snapshot = bot.snapshot()
            llm_status = snapshot["settings"]["llm_super_agent"]

            self.assertTrue(llm_status["enabled"])
            self.assertTrue(llm_status["api_key_present"])
            self.assertEqual(llm_status["base_url"], "https://api.hao.ai/v1")
            self.assertEqual(llm_status["model"], "openai/gpt-5.4-mini")
            self.assertNotIn(secret, str(snapshot))

    def test_strategy_experiments_run_all_variants_in_isolated_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                min_confidence=0.55,
                min_edge=0.0,
                max_entry_price=0.8,
                max_open_trades=2,
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-experiments",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 101.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.39,
                        "best_ask": 0.4,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.4, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {
                        "best_bid": 0.49,
                        "best_ask": 0.5,
                        "bid_size": 100,
                        "ask_size": 100,
                        "asks": [{"price": 0.5, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                }

            bot._run_strategy_from_state()

            snapshot = bot.strategy_experiments_snapshot()
            variants = {row["variant_id"]: row for row in snapshot["variants"]}
            active_variants = active_strategy_variants()
            self.assertEqual(len(variants), len(active_variants))
            self.assertTrue(all(not key.startswith("PAIR_") for key in variants))
            self.assertEqual(snapshot["run_count"], 1)
            self.assertIn("profit_summary", snapshot)
            self.assertEqual(snapshot["profit_summary"]["status"], "WAITING_FOR_SAMPLE")
            self.assertIsNone(snapshot["profit_summary"]["winner_variant_id"])
            self.assertFalse(snapshot["decision_summary"]["comparison_ready"])
            self.assertEqual(snapshot["decision_summary"]["status"], "WAITING_FOR_SAMPLE")
            self.assertEqual(snapshot["decision_summary"]["ready_count"], 0)
            self.assertEqual(snapshot["decision_summary"]["total_count"], len(active_variants))
            self.assertIsNone(snapshot["decision_summary"]["recommended_variant_id"])
            self.assertIsNotNone(snapshot["decision_summary"]["current_leader_variant_id"])
            self.assertTrue(all(row["last_error"] is None for row in variants.values()))
            self.assertIn("review_score", variants["SINGLE_FAK"])
            self.assertIn("score", variants["SINGLE_FAK"]["review_score"])
            self.assertEqual(variants["SINGLE_FAK"]["review_score"]["sample_status"], "INSUFFICIENT")
            self.assertIn("结算样本不足", variants["SINGLE_FAK"]["review_score"]["reasons"][0])
            self.assertEqual(variants["SINGLE_FAK"]["metrics"]["open_trades"], 1)
            self.assertEqual(variants["SINGLE_FAK_REVERSE"]["signal_side_mode"], "REVERSE")
            self.assertEqual(variants["SINGLE_FAK_REVERSE"]["metrics"]["open_trades"], 1)
            self.assertEqual(variants["SINGLE_FAK_REVERSE"]["last_signal"]["side"], "Down")
            self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE", variants)
            self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE_V1", variants)
            self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE_V2", variants)
            self.assertNotIn("SINGLE_FAK_AGGRESSIVE_EDGE_V3", variants)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V3_INTUITION",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"]["loss_replay_path"],
            )
            diagnostic_memory = variants["SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"]["aggressive_edge_v3_memory_summary"]
            self.assertIsNotNone(diagnostic_memory)
            self.assertFalse(diagnostic_memory["ready"])
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V4_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V4_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V4_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V4_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V4_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v4_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V4_DIAGNOSTIC"]["loss_replay_path"],
            )
            v4_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V4_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v4_shadow_summary)
            self.assertIn("v4_would_trade_count", v4_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V5_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V5_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v5_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC"]["loss_replay_path"],
            )
            v5_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V5_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v5_shadow_summary)
            self.assertIn("v5_would_trade_count", v5_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V6_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V6_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V6_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V6_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V6_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v6_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V6_DIAGNOSTIC"]["loss_replay_path"],
            )
            v6_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V6_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v6_shadow_summary)
            self.assertIn("v6_would_trade_count", v6_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V7_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V7_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V7_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V7_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V7_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v7_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V7_DIAGNOSTIC"]["loss_replay_path"],
            )
            v7_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V7_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v7_shadow_summary)
            self.assertIn("v7_would_trade_count", v7_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V8_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V8_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V8_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V8_LEARNING_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V8_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v8_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V8_DIAGNOSTIC"]["loss_replay_path"],
            )
            v8_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V8_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v8_shadow_summary)
            self.assertIn("v8_would_trade_count", v8_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V9_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V9_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V9_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V9_M1_GUARD_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V9_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v9_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V9_DIAGNOSTIC"]["loss_replay_path"],
            )
            v9_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V9_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v9_shadow_summary)
            self.assertIn("v9_would_trade_count", v9_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V10_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V10_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V10_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V10_UP_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V10_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v10_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V10_DIAGNOSTIC"]["loss_replay_path"],
            )
            v10_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V10_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v10_shadow_summary)
            self.assertIn("v10_would_trade_count", v10_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V11_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V11_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V11_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V11_DEPTH_MOMENTUM_GUARD_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V11_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v11_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V11_DIAGNOSTIC"]["loss_replay_path"],
            )
            v11_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V11_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v11_shadow_summary)
            self.assertIn("v11_would_trade_count", v11_shadow_summary)
            self.assertEqual(
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC"]["signal_filter_mode"],
                "AGGRESSIVE_EDGE_V12_DIAGNOSTIC",
            )
            self.assertEqual(variants["SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC"]["metrics"]["open_trades"], 0)
            self.assertIn(
                "SINGLE_AGGRESSIVE_EDGE V12_REVERSAL_GUARD_DIAGNOSTIC_NO_TRADE",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC"]["last_signal"]["reason"],
            )
            self.assertIn(
                "single_fak_aggressive_edge_v12_diagnostic.jsonl",
                variants["SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC"]["loss_replay_path"],
            )
            v12_shadow_summary = variants["SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC"]["aggressive_edge_v2_shadow_summary"]
            self.assertIsNotNone(v12_shadow_summary)
            self.assertIn("v12_would_trade_count", v12_shadow_summary)
            self.assertEqual(variants["SINGLE_FAK_REVERSAL"]["single_entry_mode"], "REVERSAL")
            self.assertEqual(variants["SINGLE_FAK_STOP_AND_FLIP"]["single_entry_mode"], "STOP_AND_FLIP")
            self.assertEqual(variants["SINGLE_FAK_CHAINLINK_ONLY"]["price_source_mode"], "CHAINLINK_ONLY")
            self.assertEqual(variants["SINGLE_FAK_CHAINLINK_ONLY"]["metrics"]["open_trades"], 1)
            self.assertEqual(variants["SINGLE_FAK_ANTI_BOT_GUARD"]["price_source_mode"], "CHAINLINK_ONLY")
            self.assertEqual(variants["SINGLE_FAK_ANTI_BOT_GUARD"]["anti_bot_guard_mode"], "ANTI_BOT_GUARD")
            self.assertEqual(variants["SINGLE_FAK_ANTI_BOT_GUARD"]["metrics"]["open_trades"], 1)
            self.assertEqual(variants["SINGLE_FAK_FALLBACK_ONLY"]["price_source_mode"], "FALLBACK_ONLY")
            self.assertEqual(variants["SINGLE_FAK_FALLBACK_ONLY"]["last_signal"]["side"], "NO_TRADE")
            self.assertIn("当前有新鲜 Chainlink", variants["SINGLE_FAK_FALLBACK_ONLY"]["last_signal"]["reason"])
            self.assertNotIn("REALTIME_MAKER_POST_ONLY", variants)
            self.assertNotIn("LLM_SUPER_AGENT_PAPER", variants)
            self.assertNotIn("SINGLE_GTC", variants)
            self.assertNotIn("SINGLE_GTD", variants)
            self.assertNotIn("SINGLE_POST_ONLY", variants)
            self.assertNotIn("SINGLE_FAK_STRICT", variants)
            self.assertNotIn("SINGLE_FAK_MULTI_CONFIRM", variants)
            self.assertNotIn("SINGLE_FAK_MULTI_LEAD", variants)

            detail = bot.strategy_experiment_detail("SINGLE_FAK", trade_limit=5, order_limit=5)
            self.assertEqual(detail["variant"]["variant_id"], "SINGLE_FAK")
            self.assertEqual(detail["variant"]["order_summary"]["total_count"], 1)
            self.assertEqual(detail["recent_orders_page"]["recent_orders_meta"]["total"], 1)
            self.assertEqual(detail["recent_trades_page"]["recent_trades_meta"]["total"], 1)

            retrospective = bot.strategy_experiments_retrospective()
            self.assertTrue(retrospective["enabled"])
            self.assertEqual(len(retrospective["variants"]), len(active_variants))
            self.assertEqual(len(retrospective["profit_summary"]["rankings"]), len(active_variants))
            self.assertEqual(retrospective["window"], {"start_at": None, "end_at": None})

            tables = bot.strategy_experiments_tables(trade_limit=20, order_limit=20)
            self.assertTrue(tables["enabled"])
            self.assertGreaterEqual(len(tables["open_trades"]), 3)
            self.assertTrue(all("combo" in row for row in tables["open_trades"]))
            self.assertTrue(any(row["variant_id"] == "SINGLE_FAK" for row in tables["open_trades"]))
            self.assertTrue(all(not str(row["variant_id"]).startswith("PAIR_") for row in tables["open_trades"]))
            self.assertGreaterEqual(tables["recent_orders_meta"]["total"], 5)
            self.assertTrue(all("combo" in row for row in tables["recent_orders"]))
            self.assertGreaterEqual(tables["recent_trades_meta"]["total"], 1)
            self.assertEqual(tables["recent_trades_summary"]["total_count"], tables["recent_trades_meta"]["total"])

            with self.assertRaises(ValueError):
                bot.strategy_experiment_detail("bad-variant")

    def test_strategy_experiment_low_fill_variant_is_disqualified(self) -> None:
        review = _experiment_review_score(
            {
                "settled_count": 0,
                "official_count": 0,
                "chainlink_count": 0,
                "roi_pct": None,
                "win_rate": None,
                "total_pnl": 0.0,
            },
            {
                "total_count": 60,
                "rejected_count": 0,
                "expired_count": 0,
                "canceled_count": 0,
                "fill_attempt_count": 0,
                "fill_rate": 0.0,
            },
            None,
            None,
        )

        self.assertEqual(review["sample_status"], "DISQUALIFIED")
        self.assertEqual(review["sample_label"], "执行淘汰")
        self.assertEqual(review["decision"], "执行不可用")
        self.assertTrue(review["disqualified"])
        self.assertFalse(review["eligible_for_decision"])
        self.assertLessEqual(review["score"], 25.0)
        self.assertIn("长期低成交，暂不纳入决胜", review["reasons"])

    def test_strategy_experiment_decision_can_finish_with_disqualified_variants(self) -> None:
        variants = [
            {
                "variant_id": "SINGLE_FAK_STOP_AND_FLIP",
                "combo": "SINGLE + FAK STOP_AND_FLIP",
                "review_score": {"score": 82.0, "eligible_for_decision": True, "disqualified": False},
                "recent_trades_summary": {"settled_count": 40, "total_pnl": 12.0},
                "order_summary": {"total_count": 90, "fill_rate": 58.0},
            },
            {
                "variant_id": "SINGLE_FAK_FALLBACK_ONLY",
                "combo": "SINGLE + FAK FALLBACK_ONLY",
                "review_score": {
                    "score": 18.0,
                    "eligible_for_decision": False,
                    "disqualified": True,
                    "disqualification_reason": "长期低成交",
                },
                "recent_trades_summary": {"settled_count": 0, "total_pnl": 0.0},
                "order_summary": {"total_count": 80, "fill_rate": 0.0},
            },
        ]

        summary = _experiment_decision_summary(variants)

        self.assertTrue(summary["comparison_ready"])
        self.assertEqual(summary["status"], "READY")
        self.assertEqual(summary["recommended_variant_id"], "SINGLE_FAK_STOP_AND_FLIP")
        self.assertEqual(summary["ready_count"], 1)
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(summary["disqualified_count"], 1)
        self.assertEqual(summary["disqualified_variants"][0]["variant_id"], "SINGLE_FAK_FALLBACK_ONLY")

    def test_strategy_experiment_profit_summary_separates_leader_from_final_winner(self) -> None:
        variants = [
            {
                "variant_id": "SINGLE_FAK",
                "combo": "SINGLE + FAK",
                "review_score": {"score": 55.0, "eligible_for_decision": False, "disqualified": False},
                "recent_trades_summary": {"settled_count": 4, "total_pnl": 15.0, "roi_pct": 75.0, "win_rate": 100.0},
                "order_summary": {"fill_rate": 100.0},
            },
            {
                "variant_id": "SINGLE_FAK_STOP_AND_FLIP",
                "combo": "SINGLE + FAK STOP_AND_FLIP",
                "review_score": {"score": 82.0, "eligible_for_decision": True, "disqualified": False},
                "recent_trades_summary": {"settled_count": 40, "total_pnl": 12.0, "roi_pct": 24.0, "win_rate": 62.5},
                "order_summary": {"fill_rate": 58.0},
            },
        ]

        waiting = _experiment_profit_summary(variants)

        self.assertEqual(waiting["status"], "WAITING_FOR_SAMPLE")
        self.assertEqual(waiting["current_profit_leader_variant_id"], "SINGLE_FAK")
        self.assertIsNone(waiting["winner_variant_id"])
        self.assertFalse(waiting["comparison_ready"])

        variants[0]["review_score"] = {"score": 20.0, "eligible_for_decision": False, "disqualified": True}
        ready = _experiment_profit_summary(variants)

        self.assertEqual(ready["status"], "READY")
        self.assertEqual(ready["current_profit_leader_variant_id"], "SINGLE_FAK")
        self.assertEqual(ready["winner_variant_id"], "SINGLE_FAK_STOP_AND_FLIP")
        self.assertTrue(ready["comparison_ready"])
        self.assertTrue(ready["profitable_winner_ready"])

        variants[1]["recent_trades_summary"]["total_pnl"] = -1.0
        no_profit = _experiment_profit_summary(variants)

        self.assertEqual(no_profit["status"], "NO_PROFIT")
        self.assertTrue(no_profit["comparison_ready"])
        self.assertFalse(no_profit["profitable_winner_ready"])
        self.assertEqual(no_profit["best_eligible_variant_id"], "SINGLE_FAK_STOP_AND_FLIP")
        self.assertIsNone(no_profit["winner_variant_id"])

    def test_strategy_experiment_html_report_escapes_and_summarizes_variants(self) -> None:
        report = {
            "enabled": True,
            "db_dir": "data/strategy-experiments",
            "window": {"start_at": None, "end_at": None},
            "profit_summary": {
                "status_label": "等待盈利样本",
                "winner_combo": None,
                "current_profit_leader_combo": "SINGLE + FAK STOP_AND_FLIP",
                "ready_count": 1,
                "total_count": 2,
                "disqualified_count": 0,
                "profitable_winner_ready": False,
                "reason": "还有 1 个组合未达到样本阈值",
            },
            "decision_summary": {
                "reason": "继续观察",
                "missing_sample_variants": [
                    {
                        "combo": "SINGLE + FAK",
                        "sample_label": "样本不足",
                        "settled_count": 2,
                        "order_count": 4,
                    }
                ],
                "disqualified_variants": [],
            },
            "variants": [
                {
                    "variant_id": "SINGLE_FAK_STOP_AND_FLIP",
                    "combo": "SINGLE + FAK STOP_AND_FLIP",
                    "role": "最核心目标",
                    "target_code_completion": "90%+",
                    "target_report_alignment": "90%+",
                    "recent_trades_summary": {
                        "total_pnl": 3.25,
                        "roi_pct": 12.5,
                        "win_rate": 66.7,
                        "settled_count": 3,
                        "total_count": 4,
                        "official_count": 3,
                    },
                    "order_summary": {"total_count": 8, "fill_rate": 62.5},
                    "review_score": {
                        "score": 76.5,
                        "decision": "优先候选",
                        "sample_label": "观察中",
                        "eligible_for_decision": False,
                        "disqualified": False,
                        "reasons": ["reason <script>alert(1)</script>"],
                    },
                }
            ],
        }

        html = _strategy_experiments_retrospective_report_html(report, generated_at=1_779_871_200)

        self.assertIn("策略实验复盘报告", html)
        self.assertIn("SINGLE + FAK STOP_AND_FLIP", html)
        self.assertIn("+$3.25", html)
        self.assertIn("reason &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("reason <script>alert(1)</script>", html)

    def test_strategy_experiment_report_snapshot_writes_docs_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                strategy_experiments_enabled=True,
                strategy_experiments_db_dir=Path(tmp) / "experiments",
                strategy_experiments_variants="SINGLE_FAK",
            )
            output = Path(tmp) / "docs" / "snapshot.html"

            result = generate_strategy_experiment_report_snapshot(
                settings,
                output,
                generated_at=1_779_871_200,
            )

            self.assertEqual(result["output_path"], str(output))
            self.assertEqual(result["variant_count"], 1)
            self.assertTrue(output.exists())
            html = output.read_text(encoding="utf-8")
            self.assertIn("策略实验复盘报告", html)
            self.assertIn("SINGLE + FAK", html)
            self.assertIn("数据源", html)

    def test_paper_order_summary_supports_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeStore(Path(tmp) / "test.sqlite3", 100.0)
            now = time.time()
            old_market = MarketRound(
                round_id="btc-updown-5m-order-window-old",
                symbol="BTC",
                started_at=now - 600,
                ends_at=now - 300,
                target_price=100.0,
            )
            new_market = MarketRound(
                round_id="btc-updown-5m-order-window-new",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 240,
                target_price=100.0,
            )
            store.upsert_round(old_market)
            store.upsert_round(new_market)
            store._insert_paper_order(
                market=old_market,
                side="Up",
                order_type="GTC",
                status="RESTING",
                limit_price=0.5,
                post_only=False,
                expires_at=None,
                requested_cash=1.0,
                reserved_cash=1.0,
                remaining_cash=1.0,
                filled_shares=0.0,
                avg_fill_price=None,
                notional=0.0,
                fee=0.0,
                cash_spent=0.0,
                trade_id=None,
                confidence=0.6,
                move_bps=10.0,
                reason="old",
                now=now - 500,
            )
            store._insert_paper_order(
                market=new_market,
                side="Down",
                order_type="GTC",
                status="RESTING",
                limit_price=0.5,
                post_only=False,
                expires_at=None,
                requested_cash=1.0,
                reserved_cash=1.0,
                remaining_cash=1.0,
                filled_shares=0.0,
                avg_fill_price=None,
                notional=0.0,
                fee=0.0,
                cash_spent=0.0,
                trade_id=None,
                confidence=0.6,
                move_bps=-10.0,
                reason="new",
                now=now - 10,
            )

            summary = store.paper_order_summary("BTC", start_at=now - 60, end_at=now)

            self.assertEqual(summary["total_count"], 1)
            self.assertEqual(summary["active_count"], 1)
            self.assertEqual(summary["requested_cash"], 1.0)

    def test_real_strategy_uses_orderbook_ask_price(self) -> None:
        settings = Settings(min_confidence=0.55, min_edge=0.0, max_entry_price=0.8)
        strategy = RealBtcFiveMinuteStrategy(settings)
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-1000",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 180,
            target_price=100.0,
        )
        payload = {
            "price": {
                "chainlink": 101.0,
                "chainlink_updated_ms": int(now * 1000),
                "binance": 101.1,
                "binance_updated_ms": int(now * 1000),
            },
            "quotes": {
                "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": int(now * 1000)},
                "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": int(now * 1000)},
            },
        }
        signal = strategy.signal(input_from_snapshot(market, payload))
        self.assertEqual(signal.side, "Up")
        self.assertEqual(signal.entry_price, 0.54)

    def test_real_strategy_price_source_modes_gate_chainlink_and_fallback(self) -> None:
        settings = Settings(min_confidence=0.55, min_edge=0.0, max_entry_price=0.8)
        strategy = RealBtcFiveMinuteStrategy(settings)
        now = time.time()
        now_ms = int(now * 1000)
        market = MarketRound(
            round_id="btc-updown-5m-price-source",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 180,
            target_price=100.0,
        )
        quotes = {
            "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": now_ms},
            "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": now_ms},
        }
        chainlink_payload = {
            "price": {
                "chainlink": 101.0,
                "chainlink_updated_ms": now_ms,
                "binance": 102.0,
                "binance_updated_ms": now_ms,
            },
            "quotes": quotes,
        }

        chainlink_signal = strategy.signal(
            input_from_snapshot(market, chainlink_payload),
            price_source_mode=PRICE_SOURCE_MODE_CHAINLINK_ONLY,
        )
        fallback_blocked = strategy.signal(
            input_from_snapshot(market, chainlink_payload),
            price_source_mode=PRICE_SOURCE_MODE_FALLBACK_ONLY,
        )

        self.assertEqual(chainlink_signal.side, "Up")
        self.assertIn("Chainlink", chainlink_signal.reason)
        self.assertEqual(fallback_blocked.side, "NO_TRADE")
        self.assertIn("当前有新鲜 Chainlink", fallback_blocked.reason)

        fallback_payload = {
            "price": {
                "binance": 101.0,
                "binance_updated_ms": now_ms,
            },
            "quotes": quotes,
        }
        chainlink_missing = strategy.signal(
            input_from_snapshot(market, fallback_payload),
            price_source_mode=PRICE_SOURCE_MODE_CHAINLINK_ONLY,
        )
        fallback_signal = strategy.signal(
            input_from_snapshot(market, fallback_payload),
            price_source_mode=PRICE_SOURCE_MODE_FALLBACK_ONLY,
        )

        self.assertEqual(chainlink_missing.side, "NO_TRADE")
        self.assertIn("缺少 Chainlink", chainlink_missing.reason)
        self.assertEqual(fallback_signal.side, "Up")
        self.assertIn("fallback", fallback_signal.reason)
        self.assertIn("price_source_mode FALLBACK_ONLY", fallback_signal.reason)

        stale_payload = {
            "price": {
                "binance": 101.0,
                "binance_updated_ms": now_ms - settings.max_quote_age_ms - 1_000,
            },
            "quotes": quotes,
        }
        stale_signal = strategy.signal(
            input_from_snapshot(market, stale_payload),
            price_source_mode=PRICE_SOURCE_MODE_FALLBACK_ONLY,
        )

        self.assertEqual(stale_signal.side, "NO_TRADE")
        self.assertIn("fallback 价格过期", stale_signal.reason)

    def test_real_strategy_anti_bot_guard_filters_external_and_rich_contract_anomalies(self) -> None:
        settings = Settings(min_confidence=0.55, min_edge=0.0, max_entry_price=0.8)
        strategy = RealBtcFiveMinuteStrategy(settings)
        now = time.time()
        now_ms = int(now * 1000)
        market = MarketRound(
            round_id="btc-updown-5m-anti-bot",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 180,
            target_price=100.0,
        )
        quotes = {
            "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": now_ms},
            "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": now_ms},
        }
        aligned_payload = {
            "price": {
                "chainlink": 101.0,
                "chainlink_updated_ms": now_ms,
                "binance_market": 101.03,
                "binance_market_updated_ms": now_ms,
                "okx": 101.02,
                "okx_updated_ms": now_ms,
            },
            "quotes": quotes,
        }
        aligned_signal = strategy.signal(
            input_from_snapshot(market, aligned_payload),
            price_source_mode=PRICE_SOURCE_MODE_CHAINLINK_ONLY,
            anti_bot_guard_mode=ANTI_BOT_GUARD_MODE_ENABLED,
        )

        self.assertEqual(aligned_signal.side, "Up")
        self.assertIn("anti_bot_guard ANTI_BOT_GUARD:PASS", aligned_signal.reason)

        opposing_payload = {
            "price": {
                "chainlink": 101.0,
                "chainlink_updated_ms": now_ms,
                "binance_market": 99.95,
                "binance_market_updated_ms": now_ms,
                "okx": 99.96,
                "okx_updated_ms": now_ms,
            },
            "quotes": quotes,
        }
        external_blocked = strategy.signal(
            input_from_snapshot(market, opposing_payload),
            price_source_mode=PRICE_SOURCE_MODE_CHAINLINK_ONLY,
            anti_bot_guard_mode=ANTI_BOT_GUARD_MODE_ENABLED,
        )

        self.assertEqual(external_blocked.side, "NO_TRADE")
        self.assertIn("ANTI_BOT_GUARD external_price_disagree", external_blocked.reason)

        rich_quotes = {
            "Up": {"best_bid": 0.65, "best_ask": 0.68, "ask_size": 20, "updated_at_ms": now_ms},
            "Down": {"best_bid": 0.29, "best_ask": 0.34, "ask_size": 20, "updated_at_ms": now_ms},
        }
        rich_payload = {
            "price": {
                "chainlink": 100.05,
                "chainlink_updated_ms": now_ms,
                "binance_market": 100.06,
                "binance_market_updated_ms": now_ms,
                "okx": 100.06,
                "okx_updated_ms": now_ms,
            },
            "quotes": rich_quotes,
        }
        rich_blocked = strategy.signal(
            input_from_snapshot(market, rich_payload),
            price_source_mode=PRICE_SOURCE_MODE_CHAINLINK_ONLY,
            anti_bot_guard_mode=ANTI_BOT_GUARD_MODE_ENABLED,
        )

        self.assertEqual(rich_blocked.side, "NO_TRADE")
        self.assertIn("rich_contract_weak_anchor", rich_blocked.reason)

    def test_real_strategy_multi_modes_use_basis_residuals(self) -> None:
        settings = Settings(min_confidence=0.55, min_edge=0.0, max_entry_price=0.8)
        strategy = RealBtcFiveMinuteStrategy(settings)
        now = time.time()
        market = MarketRound(
            round_id="btc-updown-5m-multi",
            symbol="BTC",
            started_at=now - 60,
            ends_at=now + 180,
            target_price=100.0,
        )
        quotes = {
            "Up": {"best_bid": 0.52, "best_ask": 0.54, "ask_size": 20, "updated_at_ms": int(now * 1000)},
            "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 20, "updated_at_ms": int(now * 1000)},
        }
        aligned_payload = {
            "price": {
                "chainlink": 101.0,
                "chainlink_updated_ms": int(now * 1000),
                "binance_market": 101.04,
                "binance_market_updated_ms": int(now * 1000),
                "binance_basis_median_bps": 0.0,
                "binance_basis_samples": 5,
                "okx": 101.03,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 0.0,
                "okx_basis_samples": 5,
            },
            "quotes": quotes,
        }
        aligned_input = input_from_snapshot(market, aligned_payload)

        confirm_signal = strategy.signal(aligned_input, MARKET_DATA_MODE_MULTI_CONFIRM)
        lead_signal = strategy.signal(aligned_input, MARKET_DATA_MODE_MULTI_LEAD)

        self.assertEqual(confirm_signal.side, "Up")
        self.assertIn("multi_confirm", confirm_signal.reason)
        self.assertEqual(lead_signal.side, "Up")
        self.assertIn("multi_lead", lead_signal.reason)

        opposing_payload = {
            "price": {
                "chainlink": 101.0,
                "chainlink_updated_ms": int(now * 1000),
                "binance_market": 100.96,
                "binance_market_updated_ms": int(now * 1000),
                "binance_basis_median_bps": 0.0,
                "binance_basis_samples": 5,
                "okx": 100.95,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 0.0,
                "okx_basis_samples": 5,
            },
            "quotes": quotes,
        }

        blocked = strategy.signal(input_from_snapshot(market, opposing_payload), MARKET_DATA_MODE_MULTI_CONFIRM)

        self.assertEqual(blocked.side, "NO_TRADE")
        self.assertIn("MULTI_CONFIRM", blocked.reason)

    def test_price_basis_tracker_keeps_median_when_chainlink_is_temporarily_missing(self) -> None:
        tracker = PriceBasisTracker(max_samples=10)
        now_ms = int(time.time() * 1000)
        seeded = tracker.enrich(
            {
                "chainlink": 100.0,
                "chainlink_updated_ms": now_ms,
                "okx": 101.0,
                "okx_updated_ms": now_ms,
                "binance": 100.5,
                "binance_updated_ms": now_ms,
            },
            now_ms=now_ms,
        )
        without_chainlink = tracker.enrich(
            {
                "chainlink": None,
                "okx": 102.0,
                "okx_updated_ms": now_ms + 100,
                "binance": 101.0,
                "binance_updated_ms": now_ms + 100,
            },
            now_ms=now_ms + 100,
        )

        self.assertAlmostEqual(seeded["okx_basis_median_bps"], 100.0, places=6)
        self.assertEqual(without_chainlink["okx_basis_samples"], 1)
        self.assertAlmostEqual(without_chainlink["okx_basis_median_bps"], 100.0, places=6)
        self.assertEqual(without_chainlink["binance_basis_samples"], 1)

    def test_live_basis_rows_allow_four_and_half_second_source_age(self) -> None:
        now_ms = int(time.time() * 1000)
        price = {
            "chainlink": 100.0,
            "chainlink_updated_ms": now_ms,
            "okx": 101.0,
            "okx_updated_ms": now_ms - 4_200,
            "okx_basis_median_bps": 100.0,
            "okx_basis_samples": 5,
        }

        rows = _live_basis_rows(price, now_ms, ("okx",))

        okx = next(row for row in rows if row["source"] == "okx")
        self.assertTrue(okx["ready"])
        self.assertEqual(okx["age_ms"], 4_200)

        stale_rows = _live_basis_rows({**price, "okx_updated_ms": now_ms - 4_600}, now_ms, ("okx",))
        stale_okx = next(row for row in stale_rows if row["source"] == "okx")
        self.assertFalse(stale_okx["ready"])
        self.assertIn("age=4600ms", stale_okx["reason"])

    def test_parse_real_btc_5m_market(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        raw = {
            "question": "Bitcoin Up or Down - test",
            "slug": "btc-updown-5m-1779871200",
            "conditionId": "0xabc",
            "endDate": "2026-05-27T08:45:00Z",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["1", "2"]',
        }
        market = client._parse_market(raw)
        self.assertIsNotNone(market)
        self.assertEqual(market.up_token, "1")
        self.assertEqual(market.down_token, "2")
        self.assertEqual(market.symbol, "BTC")

    def test_polymarket_resolution_reads_event_metadata_prices(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        slug = "btc-updown-5m-1779871200"
        client._get_event_by_slug = lambda _slug: {
            "eventMetadata": {"finalPrice": 101.25, "priceToBeat": 100.5},
            "markets": [
                {
                    "slug": slug,
                    "closed": True,
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["1", "0"]',
                }
            ],
        }

        def fail_page_fetch(_url: str) -> str:
            raise AssertionError("page fallback should not be used when Gamma eventMetadata has prices")

        client._get_text = fail_page_fetch

        resolution = client.get_resolution(slug)

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution["outcome"], "Up")
        self.assertAlmostEqual(resolution["final_price"], 101.25, places=6)
        self.assertAlmostEqual(resolution["target_price"], 100.5, places=6)
        self.assertEqual(resolution["settlement_price_source"], "Gamma:eventMetadata")

    def test_polymarket_resolution_falls_back_to_page_target_only(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        slug = "btc-updown-5m-1779871200"
        client._get_event_by_slug = lambda _slug: {
            "markets": [
                {
                    "slug": slug,
                    "closed": True,
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["0", "1"]',
                }
            ],
        }
        client._get_text = lambda _url: (
            '<html><script>{"eventMetadata":{"finalPrice":98.75,"priceToBeat":100.5}}</script></html>'
        )

        resolution = client.get_resolution(slug)

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution["outcome"], "Down")
        self.assertIsNone(resolution["final_price"])
        self.assertAlmostEqual(resolution["target_price"], 100.5, places=6)
        self.assertEqual(resolution["settlement_price_source"], "PolymarketPage:eventMetadata")

    def test_polymarket_resolution_ignores_page_final_when_gamma_final_missing(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        slug = "btc-updown-5m-1779871200"
        client._get_event_by_slug = lambda _slug: {
            "eventMetadata": {"priceToBeat": 100.5},
            "markets": [
                {
                    "slug": slug,
                    "closed": True,
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["1", "0"]',
                }
            ],
        }
        client._get_text = lambda _url: (
            '<html><script>{"eventMetadata":{"finalPrice":999.0,"priceToBeat":100.5}}</script></html>'
        )

        resolution = client.get_resolution(slug)

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution["outcome"], "Up")
        self.assertIsNone(resolution["final_price"])
        self.assertAlmostEqual(resolution["target_price"], 100.5, places=6)
        self.assertEqual(resolution["settlement_price_source"], "Gamma:eventMetadata")

    def test_polymarket_quote_keeps_sorted_orderbook_levels(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        client._get_json = lambda _url, _params: {
            "bids": [{"price": "0.31", "size": "9"}, {"price": "0.33", "size": "4"}],
            "asks": [{"price": "0.45", "size": "8"}, {"price": "0.34", "size": "2"}],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
        }

        quote = client.get_quote("token-1", "Up").to_dict()

        self.assertEqual(quote["best_bid"], 0.33)
        self.assertEqual(quote["best_ask"], 0.34)
        self.assertEqual(quote["bids"][0], {"price": 0.33, "size": 4.0})
        self.assertEqual(quote["asks"][0], {"price": 0.34, "size": 2.0})
        self.assertEqual(quote["min_order_size"], 5.0)
        self.assertEqual(quote["tick_size"], "0.01")

    def test_polymarket_quotes_use_batch_books_endpoint(self) -> None:
        client = PolymarketClient("https://gamma-api.polymarket.com", "https://clob.polymarket.com")
        post_calls = []

        def fake_post_json(url, payload):
            post_calls.append((url, payload))
            return [
                {
                    "asset_id": "up-token",
                    "bids": [{"price": "0.41", "size": "3"}],
                    "asks": [{"price": "0.42", "size": "4"}],
                    "min_order_size": "1",
                    "tick_size": "0.01",
                },
                {
                    "asset_id": "down-token",
                    "bids": [{"price": "0.57", "size": "5"}],
                    "asks": [{"price": "0.58", "size": "6"}],
                    "min_order_size": "1",
                    "tick_size": "0.01",
                },
            ]

        client._post_json = fake_post_json
        client._get_json = lambda _url, _params: self.fail("individual /book fallback should not be used")
        market = MarketRound(
            "round-1",
            "BTC",
            time.time() - 1,
            time.time() + 100,
            73000.0,
            "BTC test",
            "condition",
            "up-token",
            "down-token",
        )

        quotes = client.get_quotes(market)

        self.assertEqual(len(post_calls), 1)
        self.assertTrue(post_calls[0][0].endswith("/books"))
        self.assertEqual(post_calls[0][1], [{"token_id": "up-token"}, {"token_id": "down-token"}])
        self.assertEqual(quotes["Up"].best_ask, 0.42)
        self.assertEqual(quotes["Down"].best_bid, 0.57)

    def test_live_trading_stays_disabled_until_explicit_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.live_trading.client = FakeLiveClient()
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-disabled",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.50, "best_ask": 0.52, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                }

            bot._run_strategy_from_state()

            self.assertEqual(bot.live_trading.store.open_trades(), [])
            self.assertEqual(bot.live_trading.store.paper_order_count("BTC"), 0)
            self.assertEqual(bot.live_trading.client.buy_calls, [])

    def test_live_startup_rearms_saved_enabled_setting_to_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_settings_path = Path(tmp) / "live-settings.json"
            live_settings_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "initial_balance": 20.0,
                        "stake_dollars": 2.0,
                        "max_open_trades": 2,
                        "max_daily_loss": 6.0,
                        "max_total_drawdown": 12.0,
                        "max_entry_price": 0.72,
                        "retry_count": 2,
                        "retry_delay_ms": 250,
                        "compliance_acknowledged": True,
                        "updated_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=live_settings_path,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)

            bot = PaperTradingBot(settings, store)

            self.assertFalse(bot.live_trading.config.enabled)
            self.assertTrue(bot.live_trading.startup_rearmed)
            self.assertEqual(bot.live_trading.last_error, LIVE_STARTUP_REARM_MESSAGE)
            persisted = json.loads(live_settings_path.read_text(encoding="utf-8"))
            self.assertFalse(persisted["enabled"])
            self.assertTrue(persisted["compliance_acknowledged"])
            self.assertTrue(bot.live_settings()["startup_rearmed"])
            self.assertFalse(bot.snapshot()["runtime"]["live_trading"]["enabled"])

    def test_live_startup_does_not_disable_settings_file_when_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_settings_path = Path(tmp) / "live-settings.json"
            live_settings_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "initial_balance": 20.0,
                        "stake_dollars": 2.0,
                        "max_open_trades": 2,
                        "max_daily_loss": 6.0,
                        "max_total_drawdown": 12.0,
                        "max_entry_price": 0.72,
                        "retry_count": 2,
                        "retry_delay_ms": 250,
                        "compliance_acknowledged": True,
                        "updated_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            lock = LiveProcessLock(live_settings_path.with_name(f"{live_settings_path.name}.lock"))
            self.assertIsNone(lock.acquire())
            try:
                settings = Settings(
                    db_path=Path(tmp) / "main.sqlite3",
                    live_trading_db_path=Path(tmp) / "live.sqlite3",
                    live_trading_settings_path=live_settings_path,
                )
                store = TradeStore(settings.db_path, settings.initial_balance)

                bot = PaperTradingBot(settings, store)

                self.assertFalse(bot.live_trading.config.enabled)
                self.assertFalse(bot.live_trading.startup_rearmed)
                self.assertTrue(bot.live_trading.startup_rearm_skipped_active_lock)
                self.assertEqual(bot.live_trading.last_error, LIVE_ACTIVE_LOCK_PRESERVE_MESSAGE)
                persisted = json.loads(live_settings_path.read_text(encoding="utf-8"))
                self.assertTrue(persisted["enabled"])
                settings_file = bot.live_trading.settings_store.file_status(bot.live_trading.config)
                self.assertTrue(settings_file["enabled"])
                self.assertFalse(settings_file["runtime_enabled"])
                self.assertFalse(settings_file["enabled_matches_runtime"])
            finally:
                lock.release()

    def test_bot_reload_live_credentials_reloads_env_and_clears_client_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.live"
            first_private_key = "0x" + "3" * 64
            second_private_key = "0x" + "4" * 64
            env_path.write_text(
                f"POLYBOT2OTHER_LIVE_PRIVATE_KEY={first_private_key}\n"
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=3\n"
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=0x" + "c" * 40 + "\n"
                "POLYBOT2OTHER_DB_PATH=main.sqlite3\n"
                "POLYBOT2OTHER_LIVE_TRADING_DB_PATH=live.sqlite3\n"
                "POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH=live-settings.json\n"
                "POLYBOT2OTHER_STRATEGY_EXPERIMENTS_ENABLED=false\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {}, clear=True):
                    settings = load_settings()
                    store = TradeStore(settings.db_path, settings.initial_balance)
                    bot = PaperTradingBot(settings, store)
                    fake_client = FakeLiveClient()
                    bot.live_trading.client = fake_client

                    env_path.write_text(
                        f"POLYBOT2OTHER_LIVE_PRIVATE_KEY={second_private_key}\n"
                        "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=3\n"
                        "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=0x" + "d" * 40 + "\n"
                        "POLYBOT2OTHER_DB_PATH=main.sqlite3\n"
                        "POLYBOT2OTHER_LIVE_TRADING_DB_PATH=live.sqlite3\n"
                        "POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH=live-settings.json\n"
                        "POLYBOT2OTHER_STRATEGY_EXPERIMENTS_ENABLED=false\n",
                        encoding="utf-8",
                    )

                    payload = bot.reload_live_credentials()

                    self.assertEqual(os.environ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"], second_private_key)
                    self.assertEqual(fake_client.clear_cached_credentials_calls, 1)
                    self.assertIn("credential_reload", payload["live_trading"])
                    self.assertEqual(payload["live_trading"]["credential_reload"]["env_files"][0]["path"], ".env.live")
                    self.assertIn("snapshot", payload)
            finally:
                os.chdir(previous_cwd)

    def test_live_preflight_blocks_missing_credentials_without_wallet_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.readiness_error_list = ["POLYBOT2OTHER_LIVE_PRIVATE_KEY is required"]
            fake_client.credential_presence["private_key"] = False
            fake_client.env_files = [
                {
                    "path": ".env.live",
                    "loaded_keys": [],
                    "empty_keys": ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"],
                    "sensitive_keys_present": [],
                    "mode": "0o600",
                    "secure_permissions": True,
                }
            ]
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                }
            )
            fake_client.wallet_calls.clear()
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-preflight-missing-key",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "updated_at_ms": int(now * 1000)},
                }

            preflight = bot.live_trading.preflight(market, dict(bot.latest_price), dict(bot.latest_quotes))
            checks = {row["key"]: row for row in preflight["checks"]}

            self.assertFalse(preflight["can_place_next_order"])
            self.assertEqual(checks["credentials"]["status"], "BLOCK")
            self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", checks["credentials"]["errors"][0])
            self.assertEqual(fake_client.wallet_calls, [])

    def test_live_preflight_passes_with_fake_wallet_and_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            fake_client.wallet_calls.clear()
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-preflight-ok",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "updated_at_ms": int(now * 1000)},
                }

            preflight = bot.live_trading.preflight(market, dict(bot.latest_price), dict(bot.latest_quotes))
            checks = {row["key"]: row for row in preflight["checks"]}

            self.assertTrue(preflight["can_place_next_order"])
            self.assertTrue(preflight["ready"])
            self.assertEqual(checks["collateral_wallet"]["status"], "PASS")
            self.assertEqual(checks["sign_market_order"]["status"], "PASS")
            self.assertEqual(preflight["entry"]["token_id"], "up-token")
            self.assertFalse(preflight["signing"]["submitted_to_clob"])
            self.assertEqual(preflight["signing"]["signed_order_hash"], "0xfakesigned")
            self.assertTrue(fake_client.wallet_calls[-1]["force"])
            self.assertEqual(fake_client.sign_calls[-1]["token_id"], "up-token")
            self.assertEqual(fake_client.buy_calls, [])

    def test_live_gate_status_exposes_daily_loss_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                live_trading_default_max_daily_loss=1.0,
                live_trading_default_max_total_drawdown=12.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_daily_loss": 1.0,
                    "max_total_drawdown": 12.0,
                    "max_open_trades": 2,
                    "max_entry_price": 0.72,
                }
            )
            self.assertTrue(bot.live_trading.config.enabled)
            now = time.time()
            loss_market = MarketRound(
                round_id="btc-updown-5m-live-gate-loss",
                symbol="BTC",
                started_at=now - 900,
                ends_at=now - 600,
                target_price=100.0,
                up_token="loss-up-token",
                down_token="loss-down-token",
            )
            bot.live_trading.store.upsert_round(loss_market)
            bot.live_trading.store.place_trade(
                TradeIntent(
                    loss_market,
                    Signal("BTC", "Up", 0.9, 0.5, 100.0, "seed loss for live gate"),
                    5.0,
                )
            )
            bot.live_trading.store.settle_round_outcome(
                loss_market.round_id,
                "Down",
                now=now - 300,
                final_price=99.0,
                target_price=100.0,
            )
            market = MarketRound(
                round_id="btc-updown-5m-live-gate-current",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.45,
                    "best_ask": 0.47,
                    "asks": [{"price": 0.47, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            gate = bot.live_trading.gate_status(
                market,
                price,
                quotes,
                readiness=fake_client.readiness(required_cash=5.0),
                official_open_orders=fake_client.open_orders_state(),
            )
            checks = {row["key"]: row for row in gate["checks"]}

            self.assertFalse(gate["can_place_order"])
            self.assertEqual(gate["overall_status"], "BLOCKED")
            self.assertEqual(gate["primary_blocker"], "daily_loss")
            self.assertEqual(checks["daily_loss"]["status"], "BLOCK")
            self.assertLessEqual(gate["metrics"]["daily_realized_pnl"], -5.0)
            self.assertEqual(checks["signal"]["status"], "PASS")
            self.assertEqual(checks["collateral_wallet"]["status"], "PASS")

    def test_live_gate_uses_selected_okx_basis_adjusted_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "fallback_sources": ["okx"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_entry_price": 0.8,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-basis-okx",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": None,
                "okx": 101.0,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 300.0,
                "okx_basis_samples": 8,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.30,
                    "best_ask": 0.32,
                    "asks": [{"price": 0.32, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.40,
                    "best_ask": 0.42,
                    "asks": [{"price": 0.42, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            gate = bot.live_trading.gate_status(
                market,
                price,
                quotes,
                readiness=fake_client.readiness(required_cash=5.0),
                official_open_orders=fake_client.open_orders_state(),
            )

            self.assertEqual(gate["signal"]["side"], "Down")
            self.assertLess(gate["price_selection"]["selected_price"], 100.0)
            self.assertEqual(gate["price_selection"]["selected_source"], "basis_adjusted")
            self.assertEqual(gate["price_selection"]["selected_sources"], ["okx"])
            self.assertIn("basis_adjusted okx", gate["signal"]["reason"])

    def test_live_gate_blocks_chainlink_single_source_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "fallback_sources": ["chainlink"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-chainlink-single-source",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {"best_bid": 0.30, "best_ask": 0.32, "updated_at_ms": int(now * 1000)},
                "Down": {"best_bid": 0.40, "best_ask": 0.42, "updated_at_ms": int(now * 1000)},
            }

            gate = bot.live_trading.gate_status(
                market,
                price,
                quotes,
                readiness=fake_client.readiness(required_cash=5.0),
                official_open_orders=fake_client.open_orders_state(),
            )
            checks = {row["key"]: row for row in gate["checks"]}

            self.assertEqual(gate["signal"]["side"], "NO_TRADE")
            self.assertTrue(gate["price_selection"]["blocked"])
            self.assertIn("Chainlink 单源不允许实盘入场", gate["price_selection"]["message"])
            self.assertEqual(checks["price_source"]["status"], "BLOCK")

    def test_live_gate_allows_chainlink_when_basis_confirms_direction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "fallback_sources": ["chainlink", "okx", "binance"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_entry_price": 0.8,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-chainlink-basis-confirmed",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 102.0,
                "chainlink_updated_ms": int(now * 1000),
                "okx": 103.0,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 100.0,
                "okx_basis_samples": 8,
                "binance": 102.5,
                "binance_updated_ms": int(now * 1000),
                "binance_basis_median_bps": 50.0,
                "binance_basis_samples": 8,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.30,
                    "best_ask": 0.32,
                    "asks": [{"price": 0.32, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.40,
                    "best_ask": 0.42,
                    "asks": [{"price": 0.42, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            gate = bot.live_trading.gate_status(
                market,
                price,
                quotes,
                readiness=fake_client.readiness(required_cash=5.0),
                official_open_orders=fake_client.open_orders_state(),
            )

            self.assertEqual(gate["signal"]["side"], "Up")
            self.assertFalse(gate["price_selection"]["blocked"])
            self.assertEqual(gate["price_selection"]["selected_source"], "chainlink")
            self.assertEqual(gate["price_selection"]["selected_sources"], ["chainlink", "okx", "binance"])
            self.assertIn("basis_confirmed okx,binance", gate["signal"]["reason"])

    def test_live_gate_blocks_chainlink_when_basis_direction_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "fallback_sources": ["chainlink", "okx", "binance"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-chainlink-basis-conflict",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 102.0,
                "chainlink_updated_ms": int(now * 1000),
                "okx": 99.0,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 0.0,
                "okx_basis_samples": 8,
                "binance": 99.2,
                "binance_updated_ms": int(now * 1000),
                "binance_basis_median_bps": 0.0,
                "binance_basis_samples": 8,
            }
            quotes = {
                "Up": {"best_bid": 0.30, "best_ask": 0.32, "updated_at_ms": int(now * 1000)},
                "Down": {"best_bid": 0.40, "best_ask": 0.42, "updated_at_ms": int(now * 1000)},
            }

            gate = bot.live_trading.gate_status(
                market,
                price,
                quotes,
                readiness=fake_client.readiness(required_cash=5.0),
                official_open_orders=fake_client.open_orders_state(),
            )
            checks = {row["key"]: row for row in gate["checks"]}

            self.assertEqual(gate["signal"]["side"], "NO_TRADE")
            self.assertTrue(gate["price_selection"]["blocked"])
            self.assertIn("Chainlink 与基差校正方向不一致", gate["price_selection"]["message"])
            self.assertEqual(checks["price_source"]["status"], "BLOCK")

    def test_live_gate_blocks_selected_basis_fallback_when_samples_are_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "fallback_sources": ["okx"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-basis-insufficient",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": None,
                "okx": 101.0,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 300.0,
                "okx_basis_samples": 2,
            }
            quotes = {
                "Up": {"best_bid": 0.30, "best_ask": 0.32, "updated_at_ms": int(now * 1000)},
                "Down": {"best_bid": 0.40, "best_ask": 0.42, "updated_at_ms": int(now * 1000)},
            }

            gate = bot.live_trading.gate_status(
                market,
                price,
                quotes,
                readiness=fake_client.readiness(required_cash=5.0),
                official_open_orders=fake_client.open_orders_state(),
            )
            checks = {row["key"]: row for row in gate["checks"]}

            self.assertEqual(gate["signal"]["side"], "NO_TRADE")
            self.assertTrue(gate["price_selection"]["blocked"])
            self.assertIn("基差样本不足", gate["price_selection"]["message"])
            self.assertEqual(checks["price_source"]["status"], "BLOCK")

    def test_live_preflight_blocks_when_official_open_orders_exist_and_does_not_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.open_orders_payload = {
                "ready": True,
                "skipped": False,
                "errors": [],
                "orders": [{"id": "external-open-order"}],
                "count": 1,
                "checked_at": time.time(),
            }
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            fake_client.wallet_calls.clear()
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-preflight-open-orders",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            preflight = bot.live_trading.preflight(market, price, quotes)
            checks = {row["key"]: row for row in preflight["checks"]}

            self.assertFalse(preflight["ready"])
            self.assertFalse(preflight["arming_ready"])
            self.assertFalse(preflight["can_enable_live"])
            self.assertEqual(checks["official_open_orders_clear"]["status"], "BLOCK")
            self.assertEqual(preflight["official_open_orders"]["count"], 1)
            self.assertTrue(fake_client.open_orders_calls[-1]["force"])
            self.assertEqual(fake_client.sign_calls, [])
            blocked_keys = {row["key"] for row in preflight["blocked_checks"]}
            self.assertIn("official_open_orders_clear", blocked_keys)

    def test_live_preflight_blocks_when_geoblock_reports_restricted_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.geoblock_payload = {
                "ready": True,
                "blocked": True,
                "country": "US",
                "region": "CA",
                "checked_at": time.time(),
                "errors": [],
                "source": "fake",
            }
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            fake_client.wallet_calls.clear()
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-preflight-geoblocked",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            preflight = bot.live_trading.preflight(market, price, quotes)
            checks = {row["key"]: row for row in preflight["checks"]}

            self.assertFalse(preflight["arming_ready"])
            self.assertEqual(checks["geo_access"]["status"], "BLOCK")
            self.assertEqual(checks["geo_access"]["country"], "US")
            self.assertEqual(fake_client.wallet_calls, [])
            self.assertEqual(fake_client.sign_calls, [])

    def test_live_preflight_arming_ready_ignores_only_disabled_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-preflight-arming",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            preflight = bot.live_trading.preflight(market, price, quotes)

            self.assertFalse(preflight["ready"])
            self.assertTrue(preflight["arming_ready"])
            self.assertTrue(preflight["can_enable_live"])
            self.assertFalse(preflight["can_place_next_order"])
            self.assertEqual([row["key"] for row in preflight["blocked_checks"]], ["enabled"])
            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(fake_client.sign_calls[-1]["token_id"], "up-token")

    def test_live_preflight_cli_no_refresh_outputs_machine_readable_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )

            payload = build_live_preflight_payload(settings, refresh=False)

            self.assertIn("live_preflight", payload)
            self.assertFalse(payload["live_preflight"]["ready"])
            self.assertFalse(payload["live_preflight"]["arming_ready"])
            blocked_keys = {row["key"] for row in payload["live_preflight"]["blocked_checks"]}
            self.assertIn("market", blocked_keys)

            env = {
                "POLYBOT2OTHER_ENV_FILE": str(Path(tmp) / "missing.env"),
                "POLYBOT2OTHER_DB_PATH": str(Path(tmp) / "cli-main.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_DB_PATH": str(Path(tmp) / "cli-live.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH": str(Path(tmp) / "cli-live-settings.json"),
            }
            stdout = io.StringIO()
            with patch.dict("os.environ", env, clear=True), patch("sys.stdout", stdout):
                exit_code = live_preflight_main(["--no-refresh", "--require-arming-ready"])

            output = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertIn("live_preflight", output)
            self.assertNotIn("snapshot", output)
            self.assertFalse(output["live_preflight"]["arming_ready"])

    def test_live_preflight_cli_can_read_running_service(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "live_preflight": {
                            "ready": False,
                            "arming_ready": True,
                            "can_enable_live": True,
                            "can_place_next_order": False,
                            "blocked_checks": [{"key": "enabled"}],
                        },
                        "snapshot": {"current_market": {"round_id": "btc-updown-5m-preflight-service"}},
                    }
                ).encode("utf-8")

        stdout = io.StringIO()
        with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen, patch("sys.stdout", stdout):
            exit_code = live_preflight_main(
                [
                    "--service-url",
                    "http://127.0.0.1:8791",
                    "--pretty",
                    "--require-arming-ready",
                ]
            )

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(output["live_preflight"]["arming_ready"])
        self.assertNotIn("snapshot", output)
        requested_url = urlopen.call_args.args[0]
        self.assertIn("/api/live-preflight", requested_url)
        self.assertIn("include_snapshot=false", requested_url)

    def test_live_doctor_reports_missing_credentials_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.readiness_error_list = ["POLYBOT2OTHER_LIVE_PRIVATE_KEY is required"]
            fake_client.credential_presence["private_key"] = False
            fake_client.env_files = [
                {
                    "path": ".env.live",
                    "loaded_keys": [],
                    "empty_keys": ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"],
                    "sensitive_keys_present": [],
                    "mode": "0o600",
                    "secure_permissions": True,
                }
            ]
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-doctor-missing-key",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            payload = build_live_doctor_from_bot(bot, refresh=False)
            doctor = payload["live_doctor"]

            self.assertEqual(doctor["status"], "BLOCKED")
            self.assertIn("credentials", doctor["fatal_one_shot_blockers"])
            self.assertFalse(doctor["can_wait_for_one_shot"])
            self.assertEqual(doctor["sdk_version"], "fake-1.0")
            self.assertTrue(doctor["sdk_status"]["compatible"])
            actions = {row["key"]: row["action"] for row in doctor["next_actions"]}
            self.assertIn("credentials", actions)
            self.assertIn(".env.live", actions["credentials"])
            self.assertEqual(
                doctor["credential_setup"]["missing_required_keys"],
                ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"],
            )
            self.assertEqual(
                doctor["credential_setup"]["empty_keys"],
                ["POLYBOT2OTHER_LIVE_PRIVATE_KEY"],
            )
            self.assertTrue(doctor["credential_setup"]["env_file_security_ready"])
            self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", doctor["credential_setup"]["next_step"])
            self.assertEqual(fake_client.buy_calls, [])

    def test_live_doctor_marks_enabled_only_blocker_as_one_shot_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-doctor-ready",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            payload = build_live_doctor_from_bot(bot, refresh=False)
            doctor = payload["live_doctor"]

            self.assertEqual(doctor["status"], "READY_FOR_ONE_SHOT_NOW")
            self.assertTrue(doctor["ready_for_one_shot_now"])
            self.assertTrue(doctor["can_wait_for_one_shot"])
            self.assertEqual(doctor["one_shot_blockers"], [])
            self.assertIn("polybot2other.live_once", doctor["first_order"]["recommended_cli"])
            self.assertIn("--max-stake 5", doctor["first_order"]["recommended_cli"])
            self.assertEqual(doctor["first_order"]["max_stake_dollars"], 5.0)
            self.assertEqual(doctor["first_order"]["recommended_api"]["body"]["max_stake_dollars"], 5.0)
            self.assertEqual(doctor["next_actions"][0]["key"], "first_order")
            self.assertEqual(fake_client.buy_calls, [])

    def test_live_doctor_reports_min_order_size_stake_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=2.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 2.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-doctor-min-order",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "asks": [{"price": 0.52, "size": 100}],
                        "min_order_size": 5.0,
                        "updated_at_ms": int(now * 1000),
                    }
                }

            payload = build_live_doctor_from_bot(bot, refresh=False)
            doctor = payload["live_doctor"]
            stake_requirement = doctor["first_order"]["stake_requirement"]
            min_order_block = next(row for row in doctor["blocked_checks"] if row["key"] == "min_order_size")
            actions = {row["key"]: row["action"] for row in doctor["next_actions"]}

            self.assertEqual(doctor["status"], "BLOCKED")
            self.assertIn("min_order_size", doctor["fatal_one_shot_blockers"])
            self.assertEqual(min_order_block["stake"], 2.0)
            self.assertEqual(min_order_block["min_order_size"], 5.0)
            self.assertEqual(min_order_block["shortfall"], 3.0)
            self.assertEqual(stake_requirement["stake_dollars"], 2.0)
            self.assertEqual(stake_requirement["min_order_size"], 5.0)
            self.assertFalse(stake_requirement["meets_min_order_size"])
            self.assertEqual(stake_requirement["shortfall"], 3.0)
            self.assertEqual(stake_requirement["suggested_stake_dollars"], 5.0)
            self.assertTrue(stake_requirement["can_fix_by_settings_update"])
            self.assertEqual(stake_requirement["recommended_settings_patch"], {"stake_dollars": 5.0})
            self.assertIn("至少 5.00", actions["min_order_size"])
            self.assertIn("缺口 3.00", actions["min_order_size"])

    def test_live_doctor_recommends_current_market_locked_stake_for_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-doctor-locked-stake",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            bot.live_trading.store.upsert_round(market)
            bot.live_trading.store.place_external_fill(
                PaperFill(
                    market=market,
                    signal=Signal("BTC", "Up", 0.8, 0.52, 200.0, "existing live leg"),
                    side="Up",
                    order_type=ORDER_TYPE_FAK,
                    status=STATUS_FILLED,
                    limit_price=0.52,
                    fill_price=0.52,
                    shares=5.769231,
                    notional=3.0,
                    fee=0.0,
                    cash_spent=3.0,
                    quote_size=100.0,
                    reason="seed live locked stake",
                    levels=(PaperFillLevel(0.52, 5.769231, 3.0, 0.0, 3.0),),
                    requested_cash=3.0,
                ),
                external_order_id="seed-live-order",
                client_order_id="seed-client-order",
                external_status="FILLED",
                raw_response="{}",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 98.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Down": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            payload = build_live_doctor_from_bot(bot, refresh=False)
            doctor = payload["live_doctor"]

            self.assertEqual(doctor["status"], "READY_FOR_ONE_SHOT_NOW")
            self.assertEqual(doctor["summary"]["software_account"]["stake_source"], "current_market_open_trade")
            self.assertEqual(doctor["first_order"]["max_stake_dollars"], 3.0)
            self.assertIn("--max-stake 3", doctor["first_order"]["recommended_cli"])
            self.assertEqual(doctor["first_order"]["recommended_api"]["body"]["max_stake_dollars"], 3.0)
            self.assertEqual(fake_client.buy_calls, [])

    def test_live_doctor_cli_requires_one_shot_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "POLYBOT2OTHER_ENV_FILE": str(Path(tmp) / "missing.env"),
                "POLYBOT2OTHER_DB_PATH": str(Path(tmp) / "cli-main.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_DB_PATH": str(Path(tmp) / "cli-live.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH": str(Path(tmp) / "cli-live-settings.json"),
            }
            stdout = io.StringIO()
            with patch.dict("os.environ", env, clear=True), patch("sys.stdout", stdout):
                exit_code = live_doctor_main(["--no-refresh", "--require-one-shot-ready"])

            output = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertIn("live_doctor", output)
            self.assertNotIn("snapshot", output)
            self.assertEqual(output["live_doctor"]["status"], "BLOCKED")

    def test_live_doctor_cli_can_read_running_service(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "live_doctor": {
                            "status": "READY_FOR_ONE_SHOT_NOW",
                            "ready_for_one_shot_now": True,
                            "can_wait_for_one_shot": True,
                            "first_order": {"max_stake_dollars": 2.0},
                        },
                        "snapshot": {"current_market": {"round_id": "btc-updown-5m-service"}},
                    }
                ).encode("utf-8")

        stdout = io.StringIO()
        with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen, patch("sys.stdout", stdout):
            exit_code = live_doctor_main(
                [
                    "--service-url",
                    "http://127.0.0.1:8791",
                    "--no-refresh",
                    "--require-one-shot-ready",
                ]
            )

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["live_doctor"]["status"], "READY_FOR_ONE_SHOT_NOW")
        self.assertNotIn("snapshot", output)
        requested_url = urlopen.call_args.args[0]
        self.assertIn("/api/live-doctor", requested_url)
        self.assertIn("refresh=false", requested_url)
        self.assertIn("include_snapshot=false", requested_url)
        self.assertIn("--service-url http://127.0.0.1:8791", output["live_doctor"]["first_order"]["recommended_service_cli"])
        self.assertIn(
            "--service-url http://127.0.0.1:8791",
            output["live_doctor"]["post_order_evidence"]["standalone_service_cli"],
        )

    def test_live_once_cli_requires_explicit_real_order_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "POLYBOT2OTHER_ENV_FILE": str(Path(tmp) / "missing.env"),
                "POLYBOT2OTHER_DB_PATH": str(Path(tmp) / "cli-main.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_DB_PATH": str(Path(tmp) / "cli-live.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH": str(Path(tmp) / "cli-live-settings.json"),
            }
            stdout = io.StringIO()
            with patch.dict("os.environ", env, clear=True), patch("sys.stdout", stdout):
                exit_code = live_once_main(["--no-refresh", "--max-stake", "2"])

            output = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertIn("confirm must be PLACE_REAL_ORDER", output["error"])

    def test_live_once_cli_outputs_structured_blocked_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "POLYBOT2OTHER_ENV_FILE": str(Path(tmp) / "missing.env"),
                "POLYBOT2OTHER_DB_PATH": str(Path(tmp) / "cli-main.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_DB_PATH": str(Path(tmp) / "cli-live.sqlite3"),
                "POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH": str(Path(tmp) / "cli-live-settings.json"),
            }
            stdout = io.StringIO()
            with patch.dict("os.environ", env, clear=True), patch("sys.stdout", stdout):
                exit_code = live_once_main(["--no-refresh", "--confirm-real-order", "--max-stake", "2"])

            output = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertIn("current market unavailable", output["error"])
            self.assertTrue(output["live_once"]["blocked"])
            self.assertFalse(output["live_once"]["submitted"])
            self.assertEqual(output["live_once"]["blocked_keys"], ["market"])
            self.assertEqual(output["live_once"]["waitable_blocked_keys"], ["market"])
            self.assertIsNone(output["live_once"]["preflight"])

    def test_live_once_cli_can_post_to_running_service(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "live_once": {
                            "submitted": True,
                            "last_order": {"order_id": "official-order-1"},
                        }
                    }
                ).encode("utf-8")

        stdout = io.StringIO()
        with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen, patch("sys.stdout", stdout):
            exit_code = live_once_main(
                [
                    "--service-url",
                    "http://127.0.0.1:8791",
                    "--confirm-real-order",
                    "--acknowledge-compliance",
                    "--max-stake",
                    "2",
                    "--wait-ready-seconds",
                    "180",
                    "--wait-reconcile-seconds",
                    "20",
                    "--require-submitted",
                ]
            )

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(output["live_once"]["submitted"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8791/api/live-once")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["confirm"], "PLACE_REAL_ORDER")
        self.assertTrue(body["acknowledge_compliance"])
        self.assertEqual(body["max_stake_dollars"], 2.0)
        self.assertEqual(body["wait_ready_seconds"], 180.0)
        self.assertEqual(body["reconcile_wait_seconds"], 20.0)
        self.assertTrue(body["include_evidence"])

    def test_live_evidence_cli_can_read_running_service(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "live_evidence": {
                            "requested_external_order_id": "official-order-1",
                            "order": {"external_order_id": "official-order-1", "status": STATUS_FILLED},
                        },
                        "snapshot": {"current_market": {"round_id": "btc-updown-5m-evidence-service"}},
                    }
                ).encode("utf-8")

        stdout = io.StringIO()
        with patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen, patch("sys.stdout", stdout):
            exit_code = live_evidence_main(
                [
                    "--service-url",
                    "http://127.0.0.1:8791",
                    "--external-order-id",
                    "official-order-1",
                    "--cached-open-orders",
                ]
            )

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["live_evidence"]["requested_external_order_id"], "official-order-1")
        self.assertNotIn("snapshot", output)
        requested_url = urlopen.call_args.args[0]
        self.assertIn("/api/live-evidence", requested_url)
        self.assertIn("external_order_id=official-order-1", requested_url)
        self.assertIn("force=false", requested_url)
        self.assertIn("include_snapshot=false", requested_url)

    def test_live_once_runs_one_order_and_disables_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=2.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": False,
                    "initial_balance": 20.0,
                    "stake_dollars": 2.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-once",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.45,
                    "best_ask": 0.47,
                    "ask_size": 100,
                    "asks": [{"price": 0.47, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)

            payload = bot.run_live_once(
                confirm="PLACE_REAL_ORDER",
                max_stake_dollars=2.0,
                acknowledge_compliance=True,
                disable_after=True,
                refresh=False,
            )

            live_once = payload["live_once"]
            self.assertTrue(live_once["submitted"])
            self.assertTrue(live_once["disabled_after"])
            self.assertEqual(live_once["evidence"]["requested_external_order_id"], "live-buy-1")
            self.assertEqual(live_once["evidence"]["order"]["external_order_id"], "live-buy-1")
            self.assertFalse(any("raw_response" in row for row in live_once["evidence"]["recent_orders"]))
            self.assertTrue(live_once["audit"]["saved"])
            audit_path = Path(live_once["audit"]["path"])
            self.assertTrue(audit_path.is_file())
            self.assertEqual(audit_path.parent, Path(tmp) / "audit")
            audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit_payload["purpose"], "SINGLE_FAK_REAL live one-shot audit")
            self.assertEqual(audit_payload["live_once"]["last_order"]["order_id"], "live-buy-1")
            self.assertNotIn("snapshot", audit_payload["live_once"])
            audit_text = json.dumps(audit_payload, ensure_ascii=False)
            self.assertNotIn("raw_response", audit_text)
            self.assertNotIn('"raw"', audit_text)
            self.assertFalse(bot.live_trading.config.enabled)
            self.assertFalse(bot.live_trading.process_lock.locked)
            self.assertEqual(fake_client.buy_calls[0]["amount"], 2.0)
            self.assertEqual(fake_client.buy_calls[0]["token_id"], "up-token")
            self.assertEqual(bot.live_trading.store.open_trades()[0]["side"], "Up")

    def test_live_evidence_summarizes_first_order_without_raw_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=2.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": False,
                    "initial_balance": 20.0,
                    "stake_dollars": 2.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-evidence",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot.run_live_once(
                confirm="PLACE_REAL_ORDER",
                max_stake_dollars=2.0,
                acknowledge_compliance=True,
                disable_after=True,
                refresh=False,
            )
            payload = bot.live_evidence("live-buy-1", force=True)

            evidence = payload["live_evidence"]
            self.assertEqual(evidence["requested_external_order_id"], "live-buy-1")
            self.assertEqual(evidence["order"]["external_order_id"], "live-buy-1")
            self.assertEqual(evidence["order"]["status"], STATUS_FILLED)
            self.assertEqual(evidence["official_open_orders"]["count"], 0)
            self.assertTrue(evidence["readiness"]["ready"])
            self.assertEqual(evidence["software_account"]["account"]["initial_balance"], 20.0)
            self.assertEqual(evidence["recent_orders"][0]["external_order_id"], "live-buy-1")
            self.assertNotIn("raw_response", evidence["order"])
            self.assertFalse(any("raw_response" in row for row in evidence["recent_orders"]))
            self.assertFalse(any("raw_response" in row for row in evidence["pending_orders"]))
            self.assertTrue(any(row.get("force") is True for row in fake_client.open_orders_calls))

    def test_live_evidence_build_payload_returns_disabled_shape_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_runtime_enabled=False,
            )

            payload = build_live_evidence_payload(settings, external_order_id="official-order-1")

            evidence = payload["live_evidence"]
            self.assertFalse(evidence["enabled"])
            self.assertEqual(evidence["requested_external_order_id"], "official-order-1")
            self.assertFalse(evidence["readiness"]["ready"])
            self.assertEqual(evidence["official_open_orders"]["count"], 0)

    def test_live_once_waits_for_pending_order_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=2.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(True, "live", "live-once-pending", None, {"status": "live"})
            fake_client.fetch_order_states = [
                LiveOrderResponse(True, "ORDER_STATUS_LIVE", "live-once-pending", None, {"order": {"status": "live"}}),
                LiveOrderResponse(
                    True,
                    "TRADES_MATCHED",
                    "live-once-pending",
                    None,
                    {"trades": [{"price": "0.5", "size": "4"}]},
                    filled_shares=4.0,
                    cash_spent=2.0,
                    avg_fill_price=0.5,
                ),
            ]
            lock_states = []
            enabled_states = []
            bot.live_trading.client = fake_client
            fake_client.fetch_order_hook = lambda _kwargs: (
                lock_states.append(bot.live_trading.process_lock.locked),
                enabled_states.append(bot.live_trading.config.enabled),
            )
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": False,
                    "initial_balance": 20.0,
                    "stake_dollars": 2.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-once-reconcile",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            payload = bot.run_live_once(
                confirm="PLACE_REAL_ORDER",
                max_stake_dollars=2.0,
                acknowledge_compliance=True,
                disable_after=True,
                refresh=False,
                reconcile_wait_seconds=1.0,
                reconcile_poll_seconds=0.1,
            )

            reconcile = payload["live_once"]["reconcile"]
            self.assertTrue(reconcile["settled"])
            self.assertEqual(reconcile["order"]["status"], "FILLED")
            self.assertEqual(reconcile["order"]["external_order_id"], "live-once-pending")
            self.assertEqual(bot.live_trading.store.open_trades()[0]["side"], "Up")
            self.assertEqual(len(fake_client.fetch_order_calls), 2)
            self.assertEqual(lock_states, [True, True])
            self.assertEqual(enabled_states, [True, True])
            self.assertFalse(bot.live_trading.config.enabled)
            self.assertFalse(bot.live_trading.process_lock.locked)

    def test_live_once_waits_for_transient_preflight_blockers_before_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=2.0,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": False,
                    "initial_balance": 20.0,
                    "stake_dollars": 2.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            markets = [
                MarketRound(
                    round_id="btc-updown-5m-live-once-wait",
                    symbol="BTC",
                    started_at=now - 5,
                    ends_at=now + 120,
                    target_price=0.0,
                    up_token="up-token",
                    down_token="down-token",
                ),
                MarketRound(
                    round_id="btc-updown-5m-live-once-wait",
                    symbol="BTC",
                    started_at=now - 5,
                    ends_at=now + 120,
                    target_price=100.0,
                    up_token="up-token",
                    down_token="down-token",
                ),
            ]

            def fake_refresh():
                market = markets.pop(0) if markets else markets_ready
                with bot._lock:
                    bot.current_market = market
                return market

            markets_ready = markets[-1]

            def fake_rest_snapshot(market):
                snapshot_now = time.time()
                with bot._lock:
                    bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(snapshot_now * 1000)}
                    bot.latest_quotes = {
                        "Up": {
                            "best_bid": 0.50,
                            "best_ask": 0.52,
                            "ask_size": 100,
                            "asks": [{"price": 0.52, "size": 100}],
                            "updated_at_ms": int(snapshot_now * 1000),
                        }
                    }

            bot._refresh_market = fake_refresh
            bot._rest_fallback_snapshot = fake_rest_snapshot

            payload = bot.run_live_once(
                confirm="PLACE_REAL_ORDER",
                max_stake_dollars=2.0,
                acknowledge_compliance=True,
                disable_after=True,
                refresh=True,
                wait_ready_seconds=3.0,
                ready_poll_seconds=0.25,
            )

            live_once = payload["live_once"]
            self.assertTrue(live_once["submitted"])
            self.assertEqual(live_once["preflight_attempts"], 2)
            self.assertIn("evidence", live_once)
            self.assertEqual(fake_client.buy_calls[0]["token_id"], "up-token")

    def test_live_once_wait_ready_fails_fast_on_fatal_preflight_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=2.0,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            fake_client.readiness_error_list = ["POLYBOT2OTHER_LIVE_PRIVATE_KEY is required"]
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": False,
                    "initial_balance": 20.0,
                    "stake_dollars": 2.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            with bot._lock:
                bot.current_market = MarketRound(
                    round_id="btc-updown-5m-live-once-fatal",
                    symbol="BTC",
                    started_at=now - 5,
                    ends_at=now + 120,
                    target_price=100.0,
                    up_token="up-token",
                    down_token="down-token",
                )
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
            }

            started = time.time()
            with self.assertRaisesRegex(LiveOnceBlockedError, "credentials") as caught:
                bot.run_live_once(
                    confirm="PLACE_REAL_ORDER",
                    max_stake_dollars=2.0,
                    acknowledge_compliance=True,
                    disable_after=True,
                    refresh=False,
                    wait_ready_seconds=30.0,
                    ready_poll_seconds=1.0,
                )

            self.assertLess(time.time() - started, 1.0)
            self.assertNotIn("enabled", str(caught.exception))
            self.assertTrue(caught.exception.payload["live_once"]["blocked"])
            self.assertIn("credentials", caught.exception.payload["live_once"]["blocked_keys"])
            self.assertIn("credentials", caught.exception.payload["live_once"]["fatal_blocked_keys"])
            self.assertIn("preflight", caught.exception.payload["live_once"])
            self.assertEqual(fake_client.buy_calls, [])

    def test_live_once_aborts_when_actual_stake_exceeds_confirmed_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=3.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 3.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-once-cap",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            with self.assertRaisesRegex(RuntimeError, "exceeds max_stake_dollars"):
                bot.run_live_once(
                    confirm="PLACE_REAL_ORDER",
                    max_stake_dollars=2.0,
                    acknowledge_compliance=False,
                    disable_after=True,
                    refresh=False,
                )

            self.assertFalse(bot.live_trading.config.enabled)
            self.assertEqual(fake_client.buy_calls, [])

    def test_live_once_requires_live_switch_off_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            bot.live_trading.client = FakeLiveClient()
            bot.live_trading.update_settings({"enabled": True, "compliance_acknowledged": True})

            with self.assertRaisesRegex(RuntimeError, "live switch to be off"):
                bot.run_live_once(
                    confirm="PLACE_REAL_ORDER",
                    max_stake_dollars=2.0,
                    acknowledge_compliance=False,
                    disable_after=True,
                    refresh=False,
                )

            bot.live_trading.update_settings({"enabled": False})

    def test_live_enable_stays_off_when_readiness_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.readiness_error_list = ["POLYBOT2OTHER_LIVE_PRIVATE_KEY is required"]
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings({"enabled": False, "compliance_acknowledged": True})

            payload = bot.set_live_enabled(True)

            self.assertFalse(payload["live_trading"]["enabled"])
            self.assertFalse(bot.live_trading.config.enabled)
            self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", bot.live_trading.last_error)

    def test_live_enable_stays_off_when_official_open_orders_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.open_orders_payload = {
                "ready": True,
                "skipped": False,
                "errors": [],
                "orders": [{"id": "external-open-order"}],
                "count": 1,
                "checked_at": time.time(),
            }
            bot.live_trading.client = fake_client

            payload = bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )

            self.assertFalse(payload["enabled"])
            self.assertFalse(bot.live_trading.config.enabled)
            self.assertFalse(payload["process_lock_acquired"])
            self.assertIn("open orders", bot.live_trading.last_error)
            self.assertTrue(any(row.get("force") for row in fake_client.open_orders_calls))

    def test_live_enable_stays_off_when_geoblock_reports_restricted_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.geoblock_payload = {
                "ready": True,
                "blocked": True,
                "country": "US",
                "region": "NY",
                "checked_at": time.time(),
                "errors": [],
                "source": "fake",
            }
            bot.live_trading.client = fake_client

            payload = bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )

            self.assertFalse(payload["enabled"])
            self.assertFalse(bot.live_trading.config.enabled)
            self.assertFalse(payload["process_lock_acquired"])
            self.assertIn("country=US", bot.live_trading.last_error)
            self.assertTrue(any(row.get("force") for row in fake_client.geoblock_calls))

    def test_live_enable_requires_single_process_lock_for_shared_settings_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_db_path = Path(tmp) / "live.sqlite3"
            live_settings_path = Path(tmp) / "live-settings.json"
            settings_one = Settings(
                db_path=Path(tmp) / "main-one.sqlite3",
                live_trading_db_path=live_db_path,
                live_trading_settings_path=live_settings_path,
                live_trading_default_stake_dollars=5.0,
            )
            settings_two = Settings(
                db_path=Path(tmp) / "main-two.sqlite3",
                live_trading_db_path=live_db_path,
                live_trading_settings_path=live_settings_path,
                live_trading_default_stake_dollars=5.0,
            )
            bot_one = PaperTradingBot(settings_one, TradeStore(settings_one.db_path, settings_one.initial_balance))
            bot_two = PaperTradingBot(settings_two, TradeStore(settings_two.db_path, settings_two.initial_balance))
            bot_one.live_trading.client = FakeLiveClient()
            bot_two.live_trading.client = FakeLiveClient()
            enable_payload = {
                "enabled": True,
                "compliance_acknowledged": True,
                "initial_balance": 20.0,
                "stake_dollars": 5.0,
            }

            first = bot_one.live_trading.update_settings(enable_payload)
            second = bot_two.live_trading.update_settings(enable_payload)

            self.assertTrue(first["enabled"])
            self.assertTrue(first["process_lock_acquired"])
            self.assertFalse(second["enabled"])
            self.assertFalse(second["process_lock_acquired"])
            self.assertIn("实盘进程锁", bot_two.live_trading.last_error)

            disabled = bot_one.live_trading.update_settings({"enabled": False})
            third = bot_two.live_trading.update_settings(enable_payload)

            self.assertFalse(disabled["process_lock_acquired"])
            self.assertTrue(third["enabled"])
            self.assertTrue(third["process_lock_acquired"])
            self.assertTrue(Path(third["process_lock_path"]).name.endswith(".lock"))

    def test_live_emergency_stop_disables_live_and_cancels_official_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "retry_count": 2,
                    "retry_delay_ms": 250,
                }
            )

            payload = bot.live_emergency_stop()

            self.assertFalse(bot.live_trading.config.enabled)
            self.assertFalse(payload["enabled"])
            self.assertTrue(payload["cancel_all"]["ready"])
            self.assertEqual(fake_client.cancel_all_calls, [{"retry_count": 2, "retry_delay_ms": 250}])
            self.assertEqual(payload["cancel_all"]["open_orders_before"][0]["id"], "open-live-order")

    def test_live_open_orders_skips_official_call_when_credentials_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            with patch.dict("os.environ", {}, clear=True):
                client = PolymarketLiveClient(settings)
                client._sdk = lambda: {}  # type: ignore[method-assign]

                payload = client.open_orders_state(retry_count=0, retry_delay_ms=0)

            self.assertFalse(payload["ready"])
            self.assertTrue(payload["skipped"])
            self.assertEqual(payload["orders"], [])
            self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", payload["errors"][0])

    def test_live_open_orders_state_uses_short_cache_unless_forced(self) -> None:
        class FakeSdkClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_open_orders(self, *, only_first_page: bool = False) -> list[dict]:
                self.calls += 1
                self.assert_first_page = only_first_page
                return [{"id": f"open-{self.calls}", "asset_id": "token-1", "side": "BUY"}]

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_default_retry_count=0,
                live_trading_default_retry_delay_ms=0,
            )
            sdk_client = FakeSdkClient()
            client = PolymarketLiveClient(settings)
            client.readiness_errors = lambda: []  # type: ignore[method-assign]
            client._sdk = lambda: {}  # type: ignore[method-assign]
            client._authenticated_client = lambda _sdk: sdk_client  # type: ignore[method-assign]

            first = client.open_orders_state(retry_count=0, retry_delay_ms=0)
            second = client.open_orders_state(retry_count=0, retry_delay_ms=0)
            forced = client.open_orders_state(force=True, retry_count=0, retry_delay_ms=0)

            self.assertTrue(first["ready"])
            self.assertEqual(first["orders"][0]["id"], "open-1")
            self.assertEqual(second["orders"][0]["id"], "open-1")
            self.assertEqual(forced["orders"][0]["id"], "open-2")
            self.assertEqual(sdk_client.calls, 2)

    def test_live_open_orders_endpoint_payload_includes_snapshot_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            fake_client.open_orders_payload = {
                "ready": True,
                "skipped": False,
                "errors": [],
                "orders": [{"id": "open-live-order"}],
                "count": 1,
                "checked_at": time.time(),
            }

            payload = bot.live_open_orders(force=True)

            self.assertEqual(payload["live_open_orders"]["open_orders"]["count"], 1)
            self.assertTrue(fake_client.open_orders_calls[0]["force"])
            self.assertEqual(payload["snapshot"]["runtime"]["live_trading"]["open_orders"]["count"], 1)
            self.assertEqual(payload["snapshot"]["settings"]["live_trading"]["open_orders"]["count"], 1)

    def test_single_fak_real_skips_buy_when_official_open_orders_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            fake_client.wallet_calls.clear()
            fake_client.open_orders_calls.clear()
            fake_client.open_orders_payload = {
                "ready": True,
                "skipped": False,
                "errors": [],
                "orders": [{"id": "external-open-order"}],
                "count": 1,
                "checked_at": time.time(),
            }
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-buy-open-orders",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_client.buy_calls, [])
            self.assertTrue(fake_client.open_orders_calls[-1]["force"])
            self.assertIn("open orders", bot.live_trading.last_signal["reason"])
            self.assertEqual(bot.live_trading.store.open_trades(), [])

    def test_single_fak_real_skips_buy_when_geoblock_reports_restricted_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            fake_client.geoblock_calls.clear()
            fake_client.geoblock_payload = {
                "ready": True,
                "blocked": True,
                "country": "US",
                "region": "TX",
                "checked_at": time.time(),
                "errors": [],
                "source": "fake",
            }
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-buy-geoblocked",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_client.buy_calls, [])
            self.assertTrue(fake_client.geoblock_calls[-1]["force"])
            self.assertIn("country=US", bot.live_trading.last_signal["reason"])
            self.assertEqual(bot.live_trading.store.open_trades(), [])

    def test_single_fak_real_places_live_order_and_live_scope_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.live_trading.client = FakeLiveClient()
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-buy",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)

            bot.live_trading.run_from_state(market, price, quotes)

            live_rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(live_rows), 1)
            self.assertEqual(live_rows[0]["side"], "Up")
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["account_scope"], "live")
            self.assertEqual(orders[0]["variant_id"], LIVE_VARIANT_ID)
            self.assertEqual(orders[0]["external_order_id"], "live-buy-1")
            self.assertEqual(orders[0]["execution_mode"], "LIVE")
            live_trades = bot.recent_trades_page(account_scope="live")["recent_trades"]
            self.assertEqual(live_trades[0]["account_scope"], "live")
            self.assertEqual(live_trades[0]["variant_id"], LIVE_VARIANT_ID)
            self.assertEqual(bot.live_trading.client.buy_calls[0]["token_id"], "up-token")
            self.assertEqual(bot.live_trading.client.buy_calls[0]["tick_size"], "0.01")
            self.assertIsNone(bot.live_trading.client.buy_calls[0]["neg_risk"])
            snapshot = bot.snapshot()
            live_snapshot = snapshot["runtime"]["live_trading"]
            live_open_trade = live_snapshot["open_trades"][0]
            live_metrics = live_snapshot["variant"]["metrics"]
            self.assertAlmostEqual(live_metrics["unrealized_pnl"], live_open_trade["unrealized_pnl"], places=6)
            self.assertAlmostEqual(live_metrics["open_mark_value"], live_open_trade["exit_value"], places=6)
            self.assertAlmostEqual(
                live_metrics["estimated_total_equity"],
                live_metrics["cash_balance"] + live_metrics["open_mark_value"],
                places=6,
            )
            self.assertEqual(live_snapshot["variants"][0]["metrics"]["unrealized_pnl"], live_metrics["unrealized_pnl"])

    def test_single_fak_real_can_select_stop_win_live_strategy_when_flat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-switch-history",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "bid_size": 100,
                    "ask_size": 100,
                    "bids": [{"price": 0.50, "size": 100}],
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            bot.live_trading.run_from_state(
                market,
                {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )
            self.assertEqual(bot.live_trading.store.db_path, settings.live_trading_db_path)
            self.assertEqual(len(bot.live_trading.store.open_trades()), 1)
            bot.live_trading.apply_official_resolution(
                market.round_id,
                "Up",
                time.time(),
                final_price=102.0,
                target_price=100.0,
            )
            self.assertEqual(bot.live_trading.store.open_trades(), [])
            self.assertGreater(bot.live_trading.store.recent_trade_count("BTC", None, None), 0)
            bot.live_trading.update_settings({"enabled": False})

            payload = bot.update_live_settings(
                {
                    "live_strategy_id": LIVE_STOP_WIN_VARIANT_ID,
                    "paper_stop_win_take_profit_pct": 70.0,
                }
            )

            self.assertEqual(bot.live_trading.config.live_strategy_id, LIVE_STOP_WIN_VARIANT_ID)
            self.assertEqual(bot.live_trading.variant_id, LIVE_STOP_WIN_VARIANT_ID)
            self.assertEqual(bot.live_trading.combo, LIVE_STOP_WIN_COMBO)
            self.assertTrue(bot.live_trading.stop_win_enabled)
            stop_win_db_path = Path(tmp) / "single_fak_real_stop_win.sqlite3"
            self.assertEqual(bot.live_trading.store.db_path, stop_win_db_path)
            self.assertTrue(stop_win_db_path.exists())
            self.assertEqual(bot.live_trading.store.recent_trade_count("BTC", None, None), 0)
            self.assertEqual(payload["live_trading"]["live_strategy_id"], LIVE_STOP_WIN_VARIANT_ID)
            self.assertEqual(payload["live_trading"]["db_path"], str(stop_win_db_path))
            self.assertEqual(payload["snapshot"]["runtime"]["live_trading"]["variant"]["variant_id"], LIVE_STOP_WIN_VARIANT_ID)
            self.assertEqual(payload["snapshot"]["settings"]["live_trading"]["combo"], LIVE_STOP_WIN_COMBO)
            self.assertEqual(
                payload["snapshot"]["runtime"]["live_trading"]["variant"]["metrics"]["settled_trades"],
                0,
            )
            options = payload["snapshot"]["settings"]["live_trading"]["live_strategy_options"]
            self.assertIn(LIVE_STOP_WIN_VARIANT_ID, {row["variant_id"] for row in options})
            option_by_id = {row["variant_id"]: row for row in options}
            self.assertEqual(option_by_id[LIVE_VARIANT_ID]["db_path"], str(settings.live_trading_db_path))
            self.assertEqual(option_by_id[LIVE_STOP_WIN_VARIANT_ID]["db_path"], str(stop_win_db_path))
            default_stop_page = bot.recent_trades_page(account_scope="live")
            self.assertEqual(default_stop_page["recent_trades_meta"]["total"], 0)
            legacy_live_page = bot.recent_trades_page(account_scope="live", variant_id=LIVE_VARIANT_ID)
            self.assertGreater(legacy_live_page["recent_trades_meta"]["total"], 0)
            self.assertEqual(legacy_live_page["recent_trades"][0]["variant_id"], LIVE_VARIANT_ID)
            self.assertEqual(legacy_live_page["recent_trades"][0]["combo"], LIVE_COMBO)
            stop_win_page = bot.recent_trades_page(account_scope="live", variant_id=LIVE_STOP_WIN_VARIANT_ID)
            self.assertEqual(stop_win_page["recent_trades_meta"]["total"], 0)
            legacy_orders = bot.orders_page(account_scope="live", variant_id=LIVE_VARIANT_ID)
            self.assertGreater(legacy_orders["recent_orders_meta"]["total"], 0)
            self.assertEqual(legacy_orders["recent_orders"][0]["variant_id"], LIVE_VARIANT_ID)
            legacy_equity = bot.equity_curve_window(account_scope="live", variant_id=LIVE_VARIANT_ID)
            self.assertEqual(legacy_equity["equity_curve_meta"]["variant_id"], LIVE_VARIANT_ID)
            self.assertEqual(legacy_equity["equity_curve_meta"]["combo"], LIVE_COMBO)
            persisted = json.loads(settings.live_trading_settings_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["live_strategy_id"], LIVE_STOP_WIN_VARIANT_ID)
            self.assertEqual(bot.live_paper_trading.config.live_strategy_id, LIVE_STOP_WIN_VARIANT_ID)
            bot.live_trading.update_settings({"live_strategy_id": LIVE_VARIANT_ID})
            self.assertEqual(bot.live_trading.store.db_path, settings.live_trading_db_path)
            self.assertGreater(bot.live_trading.store.recent_trade_count("BTC", None, None), 0)

    def test_single_fak_aggressive_edge_real_selects_independent_live_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            bot.live_trading.client = FakeLiveClient()

            payload = bot.update_live_settings({"live_strategy_id": LIVE_AGGRESSIVE_EDGE_VARIANT_ID})

            aggressive_db_path = Path(tmp) / "single_fak_aggressive_edge_real.sqlite3"
            self.assertEqual(bot.live_trading.config.live_strategy_id, LIVE_AGGRESSIVE_EDGE_VARIANT_ID)
            self.assertEqual(bot.live_trading.variant_id, LIVE_AGGRESSIVE_EDGE_VARIANT_ID)
            self.assertEqual(bot.live_trading.combo, LIVE_AGGRESSIVE_EDGE_COMBO)
            self.assertEqual(bot.live_trading.variant.signal_filter_mode, SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE)
            self.assertEqual(bot.live_trading.store.db_path, aggressive_db_path)
            self.assertTrue(aggressive_db_path.exists())
            self.assertEqual(payload["live_trading"]["db_path"], str(aggressive_db_path))
            self.assertEqual(payload["live_trading"]["live_strategy"]["signal_filter_mode"], SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE)
            options = payload["snapshot"]["settings"]["live_trading"]["live_strategy_options"]
            option_by_id = {row["variant_id"]: row for row in options}
            self.assertEqual(
                option_by_id[LIVE_AGGRESSIVE_EDGE_VARIANT_ID]["signal_filter_mode"],
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
            )
            self.assertEqual(option_by_id[LIVE_AGGRESSIVE_EDGE_VARIANT_ID]["db_path"], str(aggressive_db_path))

    def test_single_fak_aggressive_edge_v10_real_selects_independent_live_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                strategy_experiments_db_dir=Path(tmp) / "strategy-experiments",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            bot.live_trading.client = FakeLiveClient()

            payload = bot.update_live_settings({"live_strategy_id": LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID})

            v10_db_path = Path(tmp) / "single_fak_aggressive_edge_v10_real.sqlite3"
            self.assertEqual(bot.live_trading.config.live_strategy_id, LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID)
            self.assertEqual(bot.live_trading.variant_id, LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID)
            self.assertEqual(bot.live_trading.combo, LIVE_AGGRESSIVE_EDGE_V10_COMBO)
            self.assertEqual(bot.live_trading.variant.signal_filter_mode, SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC)
            self.assertEqual(bot.live_trading.store.db_path, v10_db_path)
            self.assertTrue(v10_db_path.exists())
            self.assertEqual(payload["live_trading"]["db_path"], str(v10_db_path))
            self.assertEqual(
                payload["live_trading"]["live_strategy"]["signal_filter_mode"],
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            )
            options = payload["snapshot"]["settings"]["live_trading"]["live_strategy_options"]
            option_by_id = {row["variant_id"]: row for row in options}
            self.assertEqual(
                option_by_id[LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID]["signal_filter_mode"],
                SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            )
            self.assertEqual(option_by_id[LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID]["db_path"], str(v10_db_path))
            self.assertIn("sample_readiness", option_by_id[LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID])
            self.assertFalse(option_by_id[LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID]["sample_readiness"]["eligible_for_live_review"])

    def test_single_fak_aggressive_edge_real_blocks_same_mid_entry_band_as_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_confidence=0.55,
                min_edge=-0.5,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_AGGRESSIVE_EDGE_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-live-aggressive-block",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 100.03,
                "chainlink_updated_ms": now_ms,
                "binance_market": 100.03,
                "binance_market_updated_ms": now_ms,
                "okx": 100.03,
                "okx_updated_ms": now_ms,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.60,
                    "best_ask": 0.62,
                    "ask_size": 100,
                    "asks": [{"price": 0.62, "size": 100}],
                    "updated_at_ms": now_ms,
                },
                "Down": {
                    "best_bid": 0.36,
                    "best_ask": 0.38,
                    "ask_size": 100,
                    "asks": [{"price": 0.38, "size": 100}],
                    "updated_at_ms": now_ms,
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(bot.live_trading.last_signal["side"], "NO_TRADE")
            self.assertIn("SINGLE_AGGRESSIVE_EDGE 过滤历史亏损价格带", bot.live_trading.last_signal["reason"])
            self.assertEqual(bot.live_trading.store.open_trades(), [])

    def test_single_fak_aggressive_edge_real_places_live_order_when_filter_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_confidence=0.55,
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_AGGRESSIVE_EDGE_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-live-aggressive-pass",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 101.0,
                "chainlink_updated_ms": now_ms,
                "binance_market": 101.02,
                "binance_market_updated_ms": now_ms,
                "okx": 101.01,
                "okx_updated_ms": now_ms,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.39,
                    "best_ask": 0.40,
                    "ask_size": 100,
                    "asks": [{"price": 0.40, "size": 100}],
                    "updated_at_ms": now_ms,
                },
                "Down": {
                    "best_bid": 0.58,
                    "best_ask": 0.60,
                    "ask_size": 100,
                    "asks": [{"price": 0.60, "size": 100}],
                    "updated_at_ms": now_ms,
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertEqual(fake_client.buy_calls[0]["token_id"], "up-token")
            self.assertEqual(bot.live_trading.variant_id, LIVE_AGGRESSIVE_EDGE_VARIANT_ID)
            self.assertIn("SINGLE_AGGRESSIVE_EDGE PASS low_entry_high_edge", bot.live_trading.last_signal["reason"])
            rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_REAL live FAK", rows[0]["reason"])
            orders = bot.orders_page(account_scope="live", variant_id=LIVE_AGGRESSIVE_EDGE_VARIANT_ID)["recent_orders"]
            self.assertEqual(orders[0]["variant_id"], LIVE_AGGRESSIVE_EDGE_VARIANT_ID)
            self.assertEqual(orders[0]["signal_filter_mode"], SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE)

    def test_single_fak_aggressive_edge_v10_real_blocks_weak_up_reversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_confidence=0.55,
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-live-v10-weak-up",
                symbol="BTC",
                started_at=now - 150,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 100.065,
                "chainlink_updated_ms": now_ms,
                "binance_market": 100.07,
                "binance_market_updated_ms": now_ms,
                "okx": 100.07,
                "okx_updated_ms": now_ms,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.69,
                    "best_ask": 0.70,
                    "bid_size": 10,
                    "ask_size": 120,
                    "bids": [{"price": 0.69, "size": 10}],
                    "asks": [{"price": 0.70, "size": 120}],
                    "updated_at_ms": now_ms,
                },
                "Down": {
                    "best_bid": 0.29,
                    "best_ask": 0.30,
                    "bid_size": 120,
                    "ask_size": 10,
                    "bids": [{"price": 0.29, "size": 120}],
                    "asks": [{"price": 0.30, "size": 10}],
                    "updated_at_ms": now_ms,
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(bot.live_trading.last_signal["side"], "NO_TRADE")
            self.assertIn("V10_UP_WEAK_TOP_SKEW", bot.live_trading.last_signal["reason"])
            self.assertEqual(bot.live_trading.store.open_trades(), [])

    def test_single_fak_aggressive_edge_v10_real_places_live_order_when_guard_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                strategy_experiments_db_dir=Path(tmp) / "strategy-experiments",
                min_confidence=0.55,
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            ready_store = TradeStore(
                settings.strategy_experiments_db_dir / "single_fak_aggressive_edge_diagnostic.sqlite3",
                100.0,
            )
            seed_now = time.time()
            for index in range(80):
                side = "Up" if index % 2 == 0 else "Down"
                bucket = [0, 2, 3][index % 3]
                round_id = f"btc-updown-5m-live-v10-ready-{index}"
                ready_store.record_aggressive_edge_v2_shadow_sample(
                    round_id=round_id,
                    symbol="BTC",
                    sample_key=f"m{bucket}:pass",
                    side=side,
                    source_signal_side=side,
                    base_would_trade=True,
                    v1_would_trade=True,
                    v2_would_trade=True,
                    v4_would_trade=True,
                    v5_would_trade=True,
                    v6_would_trade=True,
                    v7_would_trade=True,
                    v8_would_trade=True,
                    v9_would_trade=True,
                    v10_would_trade=True,
                    entry_price=0.65,
                    confidence=0.82,
                    move_bps=6.5 if side == "Up" else -6.5,
                    report={
                        "risk_score": 0.1,
                        "risk_level": "LOW",
                        "risk_reasons": [],
                        "features": {"entry_price": 0.65, "move_bps": 6.5, "top_level_skew": 0.6},
                        "components": {},
                    },
                    base_block_reason=None,
                    v1_block_reason=None,
                    v4_block_reason=None,
                    v5_block_reason=None,
                    v6_block_reason=None,
                    v7_block_reason=None,
                    v8_block_reason=None,
                    v9_block_reason=None,
                    v10_block_reason=None,
                    signal_reason="V10 REAL 准入测试样本",
                    created_at=seed_now + index,
                )
                ready_store.settle_aggressive_edge_v2_shadow_samples(
                    round_id,
                    side,
                    seed_now + index + 1,
                    final_price=101.0 if side == "Up" else 99.0,
                    target_price=100.0,
                    settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
                )
            ready_store.conn.close()
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-live-v10-pass",
                symbol="BTC",
                started_at=now - 150,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 100.065,
                "chainlink_updated_ms": now_ms,
                "binance_market": 100.07,
                "binance_market_updated_ms": now_ms,
                "okx": 100.07,
                "okx_updated_ms": now_ms,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.69,
                    "best_ask": 0.70,
                    "bid_size": 120,
                    "ask_size": 10,
                    "bids": [{"price": 0.69, "size": 120}],
                    "asks": [{"price": 0.70, "size": 10}],
                    "updated_at_ms": now_ms,
                },
                "Down": {
                    "best_bid": 0.29,
                    "best_ask": 0.30,
                    "bid_size": 10,
                    "ask_size": 120,
                    "bids": [{"price": 0.29, "size": 10}],
                    "asks": [{"price": 0.30, "size": 120}],
                    "updated_at_ms": now_ms,
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertEqual(fake_client.buy_calls[0]["token_id"], "up-token")
            self.assertEqual(bot.live_trading.variant_id, LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID)
            self.assertIn("SINGLE_AGGRESSIVE_EDGE PASS sweet_move_6_8bps", bot.live_trading.last_signal["reason"])
            rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V10_REAL live FAK", rows[0]["reason"])
            orders = bot.orders_page(account_scope="live", variant_id=LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID)["recent_orders"]
            self.assertEqual(orders[0]["variant_id"], LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID)
            self.assertEqual(orders[0]["signal_filter_mode"], SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC)

    def test_single_fak_aggressive_edge_v10_real_blocks_when_sample_gate_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                strategy_experiments_db_dir=Path(tmp) / "strategy-experiments",
                min_confidence=0.55,
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-live-v10-sample-gate",
                symbol="BTC",
                started_at=now - 150,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 100.065,
                "chainlink_updated_ms": now_ms,
                "binance_market": 100.07,
                "binance_market_updated_ms": now_ms,
                "okx": 100.07,
                "okx_updated_ms": now_ms,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.69,
                    "best_ask": 0.70,
                    "bid_size": 120,
                    "ask_size": 10,
                    "bids": [{"price": 0.69, "size": 120}],
                    "asks": [{"price": 0.70, "size": 10}],
                    "updated_at_ms": now_ms,
                },
                "Down": {
                    "best_bid": 0.29,
                    "best_ask": 0.30,
                    "bid_size": 10,
                    "ask_size": 120,
                    "bids": [{"price": 0.29, "size": 10}],
                    "asks": [{"price": 0.30, "size": 120}],
                    "updated_at_ms": now_ms,
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(bot.live_trading.last_signal["side"], "Up")
            self.assertIn("V10 样本准入未通过", bot.live_trading.last_signal["reason"])
            self.assertEqual(bot.live_trading.store.open_trades(), [])

            preflight = bot.live_trading.preflight(market, price, quotes)
            sample_check = [row for row in preflight["checks"] if row["key"] == "sample_readiness"][0]
            self.assertEqual(sample_check["status"], "BLOCK")
            self.assertIn("缺少 V10 诊断样本库", sample_check["message"])
            self.assertIn("sample_readiness", {row["key"] for row in preflight["blocked_checks"]})

    def test_single_fak_aggressive_edge_v11_real_blocks_weak_up_top_skew(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                strategy_experiments_db_dir=Path(tmp) / "strategy-experiments",
                min_confidence=0.55,
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            self._seed_live_aggressive_edge_readiness(settings, version="V11")
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-live-v11-weak-top",
                symbol="BTC",
                started_at=now - 150,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 100.075,
                "chainlink_updated_ms": now_ms,
                "binance_market": 100.08,
                "binance_market_updated_ms": now_ms,
                "okx": 100.08,
                "okx_updated_ms": now_ms,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.67,
                    "best_ask": 0.68,
                    "bid_size": 45,
                    "ask_size": 1400,
                    "bids": [{"price": 0.67, "size": 45}],
                    "asks": [{"price": 0.68, "size": 1400}],
                    "updated_at_ms": now_ms,
                },
                "Down": {
                    "best_bid": 0.31,
                    "best_ask": 0.32,
                    "bid_size": 1400,
                    "ask_size": 45,
                    "bids": [{"price": 0.31, "size": 1400}],
                    "asks": [{"price": 0.32, "size": 45}],
                    "updated_at_ms": now_ms,
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(bot.live_trading.last_signal["side"], "NO_TRADE")
            self.assertIn("V11_UP_WEAK_TOP_SKEW", bot.live_trading.last_signal["reason"])
            self.assertEqual(bot.live_trading.store.open_trades(), [])

    def test_single_fak_aggressive_edge_v11_real_places_live_order_when_guard_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                strategy_experiments_db_dir=Path(tmp) / "strategy-experiments",
                min_confidence=0.55,
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            self._seed_live_aggressive_edge_readiness(settings, version="V11")
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            now_ms = int(now * 1000)
            market = MarketRound(
                round_id="btc-updown-5m-live-v11-pass",
                symbol="BTC",
                started_at=now - 150,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 100.075,
                "chainlink_updated_ms": now_ms,
                "binance_market": 100.08,
                "binance_market_updated_ms": now_ms,
                "okx": 100.08,
                "okx_updated_ms": now_ms,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.67,
                    "best_ask": 0.68,
                    "bid_size": 1200,
                    "ask_size": 200,
                    "bids": [{"price": 0.67, "size": 1200}],
                    "asks": [{"price": 0.68, "size": 200}],
                    "updated_at_ms": now_ms,
                },
                "Down": {
                    "best_bid": 0.31,
                    "best_ask": 0.32,
                    "bid_size": 200,
                    "ask_size": 1200,
                    "bids": [{"price": 0.31, "size": 200}],
                    "asks": [{"price": 0.32, "size": 1200}],
                    "updated_at_ms": now_ms,
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertEqual(fake_client.buy_calls[0]["token_id"], "up-token")
            self.assertEqual(bot.live_trading.variant_id, LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID)
            self.assertIn("SINGLE_AGGRESSIVE_EDGE V11_REAL_GUARD PASS", bot.live_trading.last_signal["reason"])
            rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertIn("SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL live FAK", rows[0]["reason"])
            self.assertIn("V11_REAL_GUARD PASS", rows[0]["reason"])
            orders = bot.orders_page(account_scope="live", variant_id=LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID)["recent_orders"]
            self.assertEqual(orders[0]["variant_id"], LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID)
            self.assertEqual(orders[0]["signal_filter_mode"], SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC)

    def test_single_fak_real_blocks_strategy_switch_with_open_live_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-switch-open",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            bot.live_trading.run_from_state(
                market,
                {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )
            self.assertEqual(len(bot.live_trading.store.open_trades()), 1)
            bot.live_trading.update_settings({"enabled": False})

            with self.assertRaisesRegex(ValueError, "持仓"):
                bot.live_trading.update_settings({"live_strategy_id": LIVE_STOP_WIN_VARIANT_ID})
            self.assertEqual(bot.live_trading.config.live_strategy_id, LIVE_VARIANT_ID)

    def test_single_fak_real_stop_win_strategy_places_live_sell_and_blocks_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.sell_response = LiveOrderResponse(
                True,
                "matched",
                "live-sell-1",
                None,
                {"status": "matched", "makingAmount": "9302810", "takingAmount": "5767742"},
                filled_shares=9.30281,
                cash_spent=5.767742,
                avg_fill_price=0.62,
            )
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "live_strategy_id": LIVE_STOP_WIN_VARIANT_ID,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                    "paper_stop_win_take_profit_pct": 8.0,
                }
            )
            self.assertEqual(bot.live_trading.store.db_path, Path(tmp) / "single_fak_real_stop_win.sqlite3")
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-stop-win",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "bid_size": 100,
                    "ask_size": 100,
                    "bids": [{"price": 0.50, "size": 100}],
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.45,
                    "best_ask": 0.47,
                    "bid_size": 100,
                    "ask_size": 100,
                    "bids": [{"price": 0.45, "size": 100}],
                    "asks": [{"price": 0.47, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            bot.live_trading.run_from_state(market, price, quotes)
            open_rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(open_rows), 1)
            trade_id = int(open_rows[0]["id"])
            bot.live_trading.store.conn.execute(
                "UPDATE trades SET opened_at = ? WHERE id = ?",
                (time.time() - 20.0, trade_id),
            )
            bot.live_trading.store.conn.commit()
            stop_quotes = dict(quotes)
            stop_quotes["Up"] = {
                **quotes["Up"],
                "best_bid": 0.62,
                "bid_size": 100,
                "bids": [{"price": 0.62, "size": 100}],
                "updated_at_ms": int(time.time() * 1000),
            }

            bot.live_trading.run_from_state(market, price, stop_quotes)

            self.assertEqual(bot.live_trading.store.open_trades(), [])
            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertEqual(len(fake_client.sell_calls), 1)
            self.assertEqual(fake_client.sell_calls[0]["token_id"], "up-token")
            self.assertGreaterEqual(fake_client.sell_calls[0]["min_price"], 0.55)
            recent = bot.recent_trades_page(account_scope="live")["recent_trades"]
            self.assertEqual(recent[0]["variant_id"], LIVE_STOP_WIN_VARIANT_ID)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            self.assertGreater(recent[0]["pnl"], 0)
            self.assertIn(LIVE_STOP_WIN_MARKER, recent[0]["reason"])
            self.assertEqual(bot.live_trading.last_stop_win["trade_id"], trade_id)
            self.assertEqual(bot.live_trading.last_order["marker"], LIVE_STOP_WIN_MARKER)

            bot.live_trading.run_from_state(market, price, stop_quotes)

            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertIn("已触发止盈退出", bot.live_trading.last_signal["reason"])

    def test_single_fak_real_stake_change_applies_next_market_when_current_market_has_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 30.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 3,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-stake-lock",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            bot.live_trading.run_from_state(
                market,
                {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )
            bot.live_trading.update_settings({"stake_dollars": 9.0})
            preflight = bot.live_trading.preflight(
                market,
                {"chainlink": 98.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )
            bot.live_trading.run_from_state(
                market,
                {"chainlink": 98.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )

            self.assertEqual(len(fake_client.buy_calls), 2)
            self.assertAlmostEqual(fake_client.buy_calls[0]["amount"], 5.0)
            self.assertAlmostEqual(fake_client.buy_calls[1]["amount"], 5.0)
            self.assertEqual(preflight["software_account"]["stake_source"], "current_market_open_trade")
            self.assertAlmostEqual(preflight["software_account"]["configured_stake"], 9.0)
            self.assertAlmostEqual(preflight["entry"]["stake"], 5.0)
            live_rows = bot.live_trading.store.open_trades()
            self.assertEqual({row["side"] for row in live_rows}, {"Up", "Down"})
            self.assertTrue(all(abs(float(row["stake"]) - 5.0) < 0.000001 for row in live_rows))

            next_market = MarketRound(
                round_id="btc-updown-5m-live-stake-next",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token-next",
                down_token="down-token-next",
            )
            bot.live_trading.run_from_state(
                next_market,
                {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )

            self.assertEqual(len(fake_client.buy_calls), 3)
            self.assertAlmostEqual(fake_client.buy_calls[2]["amount"], 9.0)

    def test_single_fak_real_skips_overlapping_live_run(self) -> None:
        class BlockingBuyClient(FakeLiveClient):
            def __init__(self) -> None:
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def place_market_buy(self, **kwargs) -> LiveOrderResponse:
                self.buy_calls.append(kwargs)
                self.entered.set()
                self.release.wait(2.0)
                return self.buy_response

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = BlockingBuyClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-overlap",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
            }
            errors: list[BaseException] = []

            def first_run() -> None:
                try:
                    bot.live_trading.run_from_state(market, price, quotes)
                except BaseException as exc:  # noqa: BLE001 - 测试线程需要把异常带回主线程断言。
                    errors.append(exc)

            thread = threading.Thread(target=first_run)
            thread.start()
            self.assertTrue(fake_client.entered.wait(2.0))

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertEqual(bot.live_trading.last_error, "live runner busy; skipped overlapping tick")
            self.assertEqual(bot.live_trading.overlap_skip_count, 1)
            fake_client.release.set()
            thread.join(2.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_single_fak_real_rechecks_enabled_before_submit_buy(self) -> None:
        class BlockingWalletClient(FakeLiveClient):
            def __init__(self) -> None:
                super().__init__()
                self.block_next_wallet = False
                self.entered = threading.Event()
                self.release = threading.Event()

            def wallet_state(self, **kwargs) -> dict:
                self.wallet_calls.append(kwargs)
                if self.block_next_wallet:
                    self.block_next_wallet = False
                    self.entered.set()
                    self.release.wait(2.0)
                payload = dict(self.wallet_payload)
                payload["required_cash"] = kwargs.get("required_cash")
                return payload

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = BlockingWalletClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            fake_client.block_next_wallet = True
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-disable-before-submit",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            errors: list[BaseException] = []

            def live_run() -> None:
                try:
                    bot.live_trading.run_from_state(market, price, quotes)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            thread = threading.Thread(target=live_run)
            thread.start()
            self.assertTrue(fake_client.entered.wait(2.0))

            bot.live_trading.update_settings({"enabled": False})
            fake_client.release.set()
            thread.join(2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(bot.live_trading.store.paper_order_count("BTC"), 0)
            self.assertIn("实盘开关已关闭", bot.live_trading.last_signal["reason"])

    def test_single_fak_real_rest_book_blocks_stale_ws_depth_before_submit_buy(self) -> None:
        class FakeRestBookClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_quotes(self, market: MarketRound) -> dict[str, dict[str, Any]]:
                self.calls += 1
                return {
                    "Up": {
                        "token_id": market.up_token,
                        "outcome": "Up",
                        "best_bid": 0.50,
                        "best_ask": None,
                        "ask_size": None,
                        "asks": [],
                        "updated_at_ms": int(time.time() * 1000),
                        "source": "rest",
                    },
                    "Down": {
                        "token_id": market.down_token,
                        "outcome": "Down",
                        "best_bid": 0.45,
                        "best_ask": 0.47,
                        "ask_size": 100,
                        "asks": [{"price": 0.47, "size": 100}],
                        "updated_at_ms": int(time.time() * 1000),
                        "source": "rest",
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_rest = FakeRestBookClient()
            bot.live_trading.client = fake_client
            bot.live_trading.polymarket = fake_rest
            bot.live_trading._force_rest_fak_confirmation = True
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-rest-book-block",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                    "source": "clob-ws-book",
                }
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_rest.calls, 1)
            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(bot.live_trading.store.paper_order_count("BTC"), 0)
            self.assertIn("实盘 REST FAK 可成交份额不足", bot.live_trading.last_signal["reason"])
            quote_check = bot.live_trading.last_signal["fak_quote_check"]
            self.assertEqual(quote_check["status"], "REST_DEPTH_BLOCK")
            self.assertGreater(quote_check["local"]["sweep_shares"], 0)
            self.assertEqual(quote_check["rest"]["sweep_shares"], 0.0)

    def test_single_fak_real_rest_book_pass_uses_rest_quote_before_submit_buy(self) -> None:
        class FakeRestBookClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_quotes(self, market: MarketRound) -> dict[str, dict[str, Any]]:
                self.calls += 1
                return {
                    "Up": {
                        "token_id": market.up_token,
                        "outcome": "Up",
                        "best_bid": 0.51,
                        "best_ask": 0.53,
                        "ask_size": 100,
                        "asks": [{"price": 0.53, "size": 100}],
                        "min_order_size": 1.0,
                        "tick_size": "0.001",
                        "neg_risk": True,
                        "updated_at_ms": int(time.time() * 1000),
                        "source": "rest",
                    },
                    "Down": {
                        "token_id": market.down_token,
                        "outcome": "Down",
                        "best_bid": 0.45,
                        "best_ask": 0.47,
                        "ask_size": 100,
                        "asks": [{"price": 0.47, "size": 100}],
                        "updated_at_ms": int(time.time() * 1000),
                        "source": "rest",
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_rest = FakeRestBookClient()
            bot.live_trading.client = fake_client
            bot.live_trading.polymarket = fake_rest
            bot.live_trading._force_rest_fak_confirmation = True
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-rest-book-pass",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                    "source": "clob-ws-book",
                }
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(fake_rest.calls, 1)
            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertEqual(fake_client.buy_calls[0]["tick_size"], "0.001")
            self.assertIs(fake_client.buy_calls[0]["neg_risk"], True)
            quote_check = bot.live_trading.last_signal["fak_quote_check"]
            self.assertEqual(quote_check["status"], "PASS")
            self.assertEqual(quote_check["local"]["best_ask"], 0.52)
            self.assertEqual(quote_check["rest"]["best_ask"], 0.53)

    def test_single_fak_real_does_not_open_trade_on_success_without_fill_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(
                True,
                "OK",
                "live-buy-unconfirmed",
                None,
                {"success": True, "orderID": "live-buy-unconfirmed"},
            )
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": True,
                    "compliance_acknowledged": True,
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_open_trades": 2,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-buy-unconfirmed",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(bot.live_trading.store.open_trades(), [])
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], STATUS_PENDING)
            self.assertEqual(orders[0]["external_order_id"], "live-buy-unconfirmed")

    def test_single_fak_real_paper_mirrors_live_signal_without_real_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "compliance_acknowledged": False,
                    "fallback_sources": ["chainlink", "okx", "binance"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_entry_price": 0.8,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-paper-shadow",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 102.0,
                "chainlink_updated_ms": int(now * 1000),
                "okx": 103.0,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 100.0,
                "okx_basis_samples": 8,
                "binance": 102.5,
                "binance_updated_ms": int(now * 1000),
                "binance_basis_median_bps": 50.0,
                "binance_basis_samples": 8,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.45,
                    "best_ask": 0.47,
                    "ask_size": 100,
                    "asks": [{"price": 0.47, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            bot.live_paper_trading.run_from_state(
                market,
                price,
                quotes,
                live_config=bot.live_trading.config,
            )

            self.assertEqual(fake_client.buy_calls, [])
            rows = bot.live_paper_trading.store.open_trades()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "Up")
            self.assertIn("SINGLE_FAK_REAL_PAPER", rows[0]["reason"])
            self.assertIn("basis_confirmed", rows[0]["reason"])
            orders = bot.orders_page(account_scope="live_paper")["recent_orders"]
            self.assertEqual(orders[0]["variant_id"], LIVE_PAPER_VARIANT_ID)
            self.assertEqual(orders[0]["execution_mode"], "PAPER")
            self.assertEqual(orders[0]["account_scope"], "live_paper")
            trades_page = bot.recent_trades_page(account_scope="live_paper")
            self.assertEqual(trades_page["recent_trades"][0]["variant_id"], LIVE_PAPER_VARIANT_ID)
            equity = bot.equity_curve_window(account_scope="live_paper")
            self.assertEqual(equity["equity_curve_meta"]["account_scope"], "live_paper")
            snapshot = bot.snapshot()
            self.assertEqual(snapshot["runtime"]["live_paper_trading"]["variant"]["variant_id"], LIVE_PAPER_VARIANT_ID)

    def test_single_fak_real_paper_blocks_chainlink_single_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            bot.live_trading.client = FakeLiveClient()
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "fallback_sources": ["chainlink"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-paper-chainlink-single",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            bot.live_paper_trading.run_from_state(
                market,
                price,
                quotes,
                live_config=bot.live_trading.config,
            )

            self.assertEqual(bot.live_paper_trading.store.open_trades(), [])
            self.assertEqual(bot.live_paper_trading.last_signal["side"], "NO_TRADE")
            self.assertIn("Chainlink 单源不允许实盘入场", bot.live_paper_trading.last_signal["reason"])

    def test_single_fak_real_paper_stop_win_closes_on_configured_bid_profit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            bot.live_trading.client = FakeLiveClient()
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "fallback_sources": ["chainlink", "okx", "binance"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_entry_price": 0.8,
                    "paper_stop_win_take_profit_pct": 8.0,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-paper-stop-win",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 102.0,
                "chainlink_updated_ms": int(now * 1000),
                "okx": 103.0,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 100.0,
                "okx_basis_samples": 8,
                "binance": 102.5,
                "binance_updated_ms": int(now * 1000),
                "binance_basis_median_bps": 50.0,
                "binance_basis_samples": 8,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "bid_size": 100,
                    "ask_size": 100,
                    "bids": [{"price": 0.50, "size": 100}],
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.45,
                    "best_ask": 0.47,
                    "bid_size": 100,
                    "ask_size": 100,
                    "bids": [{"price": 0.45, "size": 100}],
                    "asks": [{"price": 0.47, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            bot.live_paper_stop_win_trading.run_from_state(
                market,
                price,
                quotes,
                live_config=bot.live_trading.config,
            )
            open_rows = bot.live_paper_stop_win_trading.store.open_trades()
            self.assertEqual(len(open_rows), 1)
            trade_id = int(open_rows[0]["id"])
            bot.live_paper_stop_win_trading.store.conn.execute(
                "UPDATE trades SET opened_at = ? WHERE id = ?",
                (time.time() - 20.0, trade_id),
            )
            bot.live_paper_stop_win_trading.store.conn.commit()
            below_target_quotes = dict(quotes)
            below_target_quotes["Up"] = {
                **quotes["Up"],
                "best_bid": 0.56,
                "bid_size": 100,
                "bids": [{"price": 0.56, "size": 100}],
                "updated_at_ms": int(time.time() * 1000),
            }

            bot.live_paper_stop_win_trading.run_from_state(
                market,
                price,
                below_target_quotes,
                live_config=bot.live_trading.config,
            )

            open_rows = bot.live_paper_stop_win_trading.store.open_trades()
            self.assertEqual(len(open_rows), 1)
            self.assertEqual(open_rows[0]["id"], trade_id)
            bot.live_paper_stop_win_trading._stop_win_next_check_at.clear()
            stop_quotes = dict(quotes)
            stop_quotes["Up"] = {
                **quotes["Up"],
                "best_bid": 0.62,
                "bid_size": 100,
                "bids": [{"price": 0.62, "size": 100}],
                "updated_at_ms": int(time.time() * 1000),
            }

            bot.live_paper_stop_win_trading.run_from_state(
                market,
                price,
                stop_quotes,
                live_config=bot.live_trading.config,
            )

            self.assertEqual(bot.live_paper_stop_win_trading.store.open_trades(), [])
            recent = bot.recent_trades_page(
                account_scope="live_paper",
                variant_id=LIVE_PAPER_STOP_WIN_VARIANT_ID,
            )["recent_trades"]
            self.assertEqual(recent[0]["variant_id"], LIVE_PAPER_STOP_WIN_VARIANT_ID)
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            self.assertGreater(recent[0]["pnl"], 0)
            self.assertIn("PAPER_STOP_WIN", recent[0]["reason"])
            self.assertIn("max_profit_pct 8.00%", recent[0]["reason"])
            self.assertEqual(
                bot.recent_trades_page(account_scope="live_paper")["recent_trades"],
                [],
            )
            snapshot = bot.snapshot()
            self.assertEqual(
                snapshot["runtime"]["live_paper_stop_win_trading"]["variant"]["variant_id"],
                LIVE_PAPER_STOP_WIN_VARIANT_ID,
            )

    def test_single_fak_real_paper_stop_win_pct_100_disables_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            bot = PaperTradingBot(settings, TradeStore(settings.db_path, settings.initial_balance))
            bot.live_trading.client = FakeLiveClient()
            bot.live_trading.update_settings(
                {
                    "enabled": False,
                    "fallback_sources": ["chainlink", "okx", "binance"],
                    "initial_balance": 20.0,
                    "stake_dollars": 5.0,
                    "max_entry_price": 0.8,
                    "paper_stop_win_take_profit_pct": 100.0,
                }
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-paper-stop-win-disabled",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {
                "chainlink": 102.0,
                "chainlink_updated_ms": int(now * 1000),
                "okx": 103.0,
                "okx_updated_ms": int(now * 1000),
                "okx_basis_median_bps": 100.0,
                "okx_basis_samples": 8,
                "binance": 102.5,
                "binance_updated_ms": int(now * 1000),
                "binance_basis_median_bps": 50.0,
                "binance_basis_samples": 8,
            }
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "bid_size": 100,
                    "ask_size": 100,
                    "bids": [{"price": 0.50, "size": 100}],
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.45,
                    "best_ask": 0.47,
                    "bid_size": 100,
                    "ask_size": 100,
                    "bids": [{"price": 0.45, "size": 100}],
                    "asks": [{"price": 0.47, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            bot.live_paper_stop_win_trading.run_from_state(
                market,
                price,
                quotes,
                live_config=bot.live_trading.config,
            )
            trade_id = int(bot.live_paper_stop_win_trading.store.open_trades()[0]["id"])
            bot.live_paper_stop_win_trading.store.conn.execute(
                "UPDATE trades SET opened_at = ? WHERE id = ?",
                (time.time() - 20.0, trade_id),
            )
            bot.live_paper_stop_win_trading.store.conn.commit()
            high_bid_quotes = dict(quotes)
            high_bid_quotes["Up"] = {
                **quotes["Up"],
                "best_bid": 0.99,
                "bid_size": 100,
                "bids": [{"price": 0.99, "size": 100}],
                "updated_at_ms": int(time.time() * 1000),
            }

            bot.live_paper_stop_win_trading.run_from_state(
                market,
                price,
                high_bid_quotes,
                live_config=bot.live_trading.config,
            )

            open_rows = bot.live_paper_stop_win_trading.store.open_trades()
            self.assertEqual(len(open_rows), 1)
            self.assertEqual(open_rows[0]["id"], trade_id)

    def test_single_fak_real_records_official_matched_amounts_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(
                True,
                "matched",
                "live-buy-1",
                None,
                {"status": "matched", "makingAmount": "2000000", "takingAmount": "4000000"},
                filled_shares=4.0,
                cash_spent=2.0,
                avg_fill_price=0.5,
            )
            bot.live_trading.client = fake_client
            payload = bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-official-amounts",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.49,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "tick_size": "0.001",
                        "neg_risk": True,
                        "updated_at_ms": int(now * 1000),
                    },
                    "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                }

            bot._run_strategy_from_state()

            live_rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(live_rows), 1)
            self.assertAlmostEqual(live_rows[0]["shares"], 4.0)
            self.assertAlmostEqual(live_rows[0]["entry_price"], 0.5)
            self.assertEqual(fake_client.buy_calls[0]["tick_size"], "0.001")
            self.assertIs(fake_client.buy_calls[0]["neg_risk"], True)

    def test_single_fak_real_rechecks_official_amounts_when_matched_response_has_no_amounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(
                True,
                "matched",
                "live-buy-no-amounts",
                None,
                {"success": True, "status": "matched", "orderID": "live-buy-no-amounts"},
            )
            fake_client.fetch_order_states = [
                LiveOrderResponse(
                    True,
                    "TRADES_MATCHED",
                    "live-buy-no-amounts",
                    None,
                    {"trades": [{"taker_order_id": "live-buy-no-amounts", "size": "3000000", "price": "0.5"}]},
                    filled_shares=3.0,
                    cash_spent=1.5,
                    avg_fill_price=0.5,
                )
            ]
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-recheck-amounts",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                condition_id="0x" + "a" * 64,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.49,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            bot.live_trading.run_from_state(market, price, quotes)

            live_rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(live_rows), 1)
            self.assertAlmostEqual(live_rows[0]["shares"], 3.0)
            self.assertAlmostEqual(live_rows[0]["stake"], 1.5525)
            self.assertEqual(fake_client.fetch_order_calls[0]["order_id"], "live-buy-no-amounts")

    def test_single_fak_real_keeps_pending_when_matched_response_amount_recheck_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(
                True,
                "matched",
                "live-buy-no-amounts-fetch-error",
                None,
                {"success": True, "status": "matched", "orderID": "live-buy-no-amounts-fetch-error"},
            )

            def fetch_error(**_kwargs):
                raise TimeoutError("official order read timeout")

            fake_client.fetch_order_state = fetch_error
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-recheck-amounts-error",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                condition_id="0x" + "a" * 64,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.49,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            bot.live_trading.run_from_state(market, price, quotes)

            self.assertEqual(bot.live_trading.store.open_trades(), [])
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], STATUS_PENDING)
            self.assertEqual(orders[0]["external_order_id"], "live-buy-no-amounts-fetch-error")
            self.assertIn("等待官方确认", bot.live_trading.last_signal["reason"])

    def test_single_fak_real_disables_live_when_local_accounting_fails_after_official_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            original_fill_pending = bot.live_trading.store.fill_external_pending_order

            def fail_after_pending(*_args, **_kwargs):
                raise RuntimeError("sqlite write failed after official fill")

            bot.live_trading.store.fill_external_pending_order = fail_after_pending
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-accounting-fail",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.49,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            with self.assertRaisesRegex(RuntimeError, "sqlite write failed"):
                bot.live_trading.run_from_state(market, price, quotes)
            bot.live_trading.store.fill_external_pending_order = original_fill_pending
            bot.live_trading.run_from_state(market, price, quotes)

            self.assertFalse(bot.live_trading.config.enabled)
            self.assertFalse(bot.live_trading.process_lock.locked)
            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertIn("local accounting failed", bot.live_trading.last_error)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], STATUS_PENDING)
            self.assertEqual(orders[0]["external_order_id"], "live-buy-1")

    def test_single_fak_real_pending_order_reconciles_to_official_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(True, "live", "live-pending-1", None, {"status": "live"})
            fake_client.fetch_order_states = [
                LiveOrderResponse(True, "ORDER_STATUS_LIVE", "live-pending-1", None, {"order": {"status": "live"}}),
                LiveOrderResponse(
                    True,
                    "TRADES_MATCHED",
                    "live-pending-1",
                    None,
                    {"trades": [{"taker_order_id": "live-pending-1", "size": "4000000", "price": "0.5"}]},
                    filled_shares=4.0,
                    cash_spent=2.0,
                    avg_fill_price=0.5,
                ),
            ]
            bot.live_trading.client = fake_client
            payload = bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-pending-fill",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                condition_id="0x" + "a" * 64,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.49,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._run_strategy_from_state()

            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], "PENDING")
            self.assertEqual(bot.live_trading.store.open_trades(), [])
            self.assertAlmostEqual(bot.live_trading.store.account()["cash_balance"], 15.0)

            bot.live_trading._live_order_reconcile_next_at.clear()
            bot._run_strategy_from_state()

            live_rows = bot.live_trading.store.open_trades()
            self.assertEqual(len(live_rows), 1)
            self.assertAlmostEqual(live_rows[0]["shares"], 4.0)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertEqual(orders[0]["external_status"], "TRADES_MATCHED")

    def test_single_fak_real_blocks_opposite_entry_while_buy_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(True, "live", "live-pending-entry", None, {"status": "live"})
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-pending-entry-block",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            quotes = {
                "Up": {
                    "best_bid": 0.49,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {
                    "best_bid": 0.49,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
            }

            bot.live_trading.run_from_state(
                market,
                {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )
            self.assertEqual(len(fake_client.buy_calls), 1)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], "PENDING")

            bot.live_trading.run_from_state(
                market,
                {"chainlink": 98.0, "chainlink_updated_ms": int(now * 1000)},
                quotes,
            )

            self.assertEqual(len(fake_client.buy_calls), 1)
            self.assertIn("待确认实盘买入订单", bot.live_trading.last_signal["reason"])
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["side"], "Up")

    def test_single_fak_real_pending_order_reconciles_to_no_fill_and_releases_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(True, "live", "live-pending-cancel", None, {"status": "live"})
            fake_client.fetch_order_states = [
                LiveOrderResponse(True, "ORDER_STATUS_LIVE", "live-pending-cancel", None, {"order": {"status": "live"}}),
                LiveOrderResponse(
                    True,
                    "ORDER_STATUS_UNMATCHED",
                    "live-pending-cancel",
                    None,
                    {"order": {"status": "unmatched", "size_matched": "0", "price": "0.5"}},
                ),
            ]
            bot.live_trading.client = fake_client
            payload = bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-pending-cancel",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.49,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._run_strategy_from_state()
            self.assertAlmostEqual(bot.live_trading.store.account()["cash_balance"], 15.0)
            bot.live_trading.update_settings({"enabled": False})
            bot.live_trading._live_order_reconcile_next_at.clear()
            bot._run_strategy_from_state()

            self.assertEqual(bot.live_trading.store.open_trades(), [])
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], "CANCELED")
            self.assertAlmostEqual(bot.live_trading.store.account()["cash_balance"], 20.0)

    def test_single_fak_real_pending_order_reconciles_invalid_to_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(True, "live", "live-pending-invalid", None, {"status": "live"})
            fake_client.fetch_order_states = [
                LiveOrderResponse(
                    True,
                    "ORDER_STATUS_LIVE",
                    "live-pending-invalid",
                    None,
                    {"order": {"status": "ORDER_STATUS_LIVE", "size_matched": "0", "price": "0.5"}},
                ),
                LiveOrderResponse(
                    False,
                    "ORDER_STATUS_INVALID",
                    "live-pending-invalid",
                    "ORDER_STATUS_INVALID",
                    {"order": {"status": "ORDER_STATUS_INVALID", "size_matched": "0", "price": "0.5"}},
                ),
            ]
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-pending-invalid",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.49,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._run_strategy_from_state()
            self.assertAlmostEqual(bot.live_trading.store.account()["cash_balance"], 15.0)
            bot.live_trading.update_settings({"enabled": False})
            bot.live_trading._live_order_reconcile_next_at.clear()
            bot._run_strategy_from_state()

            self.assertEqual(bot.live_trading.store.open_trades(), [])
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], STATUS_REJECTED)
            self.assertEqual(orders[0]["external_status"], "ORDER_STATUS_INVALID")
            self.assertAlmostEqual(bot.live_trading.store.account()["cash_balance"], 20.0)

    def test_single_fak_real_pending_order_times_out_and_releases_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(
                True,
                "POST_STATUS_UNKNOWN",
                "0xsignedorderhash",
                "TimeoutError: timeout before response",
                {"submitted_to_clob_unknown": True, "signed_order_hash": "0xsignedorderhash"},
            )
            fake_client.fetch_order_states = [
                LiveOrderResponse(False, "RECONCILE_ERROR", "0xsignedorderhash", "not found", {"order_error": "not found"})
            ]
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-pending-timeout",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.49,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }

            bot.live_trading.run_from_state(market, price, quotes)
            self.assertAlmostEqual(bot.live_trading.store.account()["cash_balance"], 15.0)
            order = bot.orders_page(account_scope="live")["recent_orders"][0]
            bot.live_trading.store.conn.execute(
                "UPDATE paper_orders SET created_at = ? WHERE id = ?",
                (time.time() - 180.0, int(order["id"])),
            )
            bot.live_trading.store.conn.commit()
            bot.live_trading.update_settings({"enabled": False})
            bot.live_trading._live_order_reconcile_next_at.clear()
            bot.live_trading.run_from_state(market, price, quotes)

            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertEqual(orders[0]["status"], "CANCELED")
            self.assertEqual(orders[0]["external_status"], "LOCAL_PENDING_TIMEOUT")
            self.assertAlmostEqual(bot.live_trading.store.account()["cash_balance"], 20.0)

    def test_live_manual_sell_closes_live_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            bot.live_trading.client = FakeLiveClient()
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)
            bot.live_trading.run_from_state(market, price, quotes)
            before = bot.live_trading.store.open_trades()[0]
            trade_id = int(before["id"])
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.40, "best_ask": 0.41, "bid_size": 100, "updated_at_ms": int(now * 1000)},
                }

            result = bot.sell_live_trade(trade_id)

            self.assertIn("closed_trade", result)
            self.assertEqual(bot.live_trading.store.open_trades(), [])
            recent = bot.recent_trades_page(account_scope="live")["recent_trades"]
            self.assertEqual(recent[0]["settlement_source"], SETTLEMENT_SOURCE_EARLY_EXIT)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertTrue(any(row["external_order_id"] == "live-sell-1" for row in orders))
            self.assertEqual(bot.live_trading.client.sell_calls[0]["token_id"], "up-token")
            self.assertEqual(bot.live_trading.client.token_calls[0]["token_id"], "up-token")

    def test_live_manual_sell_blocks_after_market_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-ended",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                },
                "Down": {"best_bid": 0.45, "best_ask": 0.47, "ask_size": 100, "updated_at_ms": int(now * 1000)},
            }
            bot.live_trading.run_from_state(market, price, quotes)
            trade_id = int(bot.live_trading.store.open_trades()[0]["id"])
            with bot.live_trading.store.conn:
                bot.live_trading.store.conn.execute(
                    "UPDATE market_rounds SET ends_at = ? WHERE round_id = ?",
                    (now - 1, market.round_id),
                )
            with bot._lock:
                bot.latest_quotes = {
                    "Up": {"best_bid": 0.40, "best_ask": 0.41, "bid_size": 100, "updated_at_ms": int(now * 1000)},
                }

            with self.assertRaisesRegex(RuntimeError, "市场已结束"):
                bot.sell_live_trade(trade_id)

            self.assertEqual(fake_client.sell_calls, [])

    def test_live_manual_sell_blocks_when_token_allowance_is_below_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.token_payload = {
                "ready": False,
                "errors": ["Polymarket conditional token allowance 0.500000 低于本次卖出份额 9.000000"],
                "balance": 100.0,
                "allowance": 0.5,
            }
            bot.live_trading.client = fake_client
            payload = bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-token-block",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)
            bot.live_trading.run_from_state(market, price, quotes)
            before = bot.live_trading.store.open_trades()[0]
            trade_id = int(before["id"])
            with bot._lock:
                bot.latest_quotes = {"Up": {"best_bid": 0.40, "bid_size": 100, "updated_at_ms": int(now * 1000)}}

            with self.assertRaises(RuntimeError):
                bot.sell_live_trade(trade_id)

            self.assertEqual(fake_client.sell_calls, [])
            self.assertEqual(len(bot.live_trading.store.open_trades()), 1)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertTrue(
                any(row["external_status"] == "TOKEN_PRECHECK_FAILED" and row["status"] == "REJECTED" for row in orders)
            )

    def test_live_manual_sell_does_not_close_without_confirmed_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.sell_response = LiveOrderResponse(True, "live", "live-sell-open", None, {"status": "live"})
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-unfilled",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)
            bot.live_trading.run_from_state(market, price, quotes)
            trade_id = int(bot.live_trading.store.open_trades()[0]["id"])

            with self.assertRaises(RuntimeError):
                bot.sell_live_trade(trade_id)

            self.assertEqual(len(bot.live_trading.store.open_trades()), 1)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertTrue(
                any(row["external_order_id"] == "live-sell-open" and row["status"] == "PENDING" for row in orders)
            )

    def test_live_manual_sell_pending_order_reconciles_to_official_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.sell_response = LiveOrderResponse(True, "live", "live-sell-pending", None, {"status": "live"})
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-pending-fill",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                condition_id="0x" + "a" * 64,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)
            bot.live_trading.run_from_state(market, price, quotes)
            before = bot.live_trading.store.open_trades()[0]
            trade_id = int(before["id"])
            with bot._lock:
                bot.latest_quotes = {"Up": {"best_bid": 0.40, "bid_size": 100, "updated_at_ms": int(now * 1000)}}

            with self.assertRaises(RuntimeError):
                bot.sell_live_trade(trade_id)

            orders = bot.orders_page(account_scope="live")["recent_orders"]
            pending_sell = next(row for row in orders if row["external_order_id"] == "live-sell-pending")
            self.assertEqual(pending_sell["status"], "PENDING")
            self.assertEqual(len(bot.live_trading.store.open_trades()), 1)
            open_row = bot.live_trading.open_trades()[0]
            self.assertEqual(open_row["pending_live_sell_external_order_id"], "live-sell-pending")
            sell_call_count = len(fake_client.sell_calls)
            with self.assertRaisesRegex(RuntimeError, "live sell already pending"):
                bot.sell_live_trade(trade_id)
            self.assertEqual(len(fake_client.sell_calls), sell_call_count)
            fake_client.fetch_order_states = [
                LiveOrderResponse(
                    True,
                    "TRADES_MATCHED",
                    "live-sell-pending",
                    None,
                    {"trades": [{"taker_order_id": "live-sell-pending", "size": "1000000", "price": "0.4"}]},
                    filled_shares=1.0,
                    cash_spent=0.4,
                    avg_fill_price=0.4,
                )
            ]
            bot.live_trading.update_settings({"enabled": False})
            bot.live_trading._live_order_reconcile_next_at.clear()

            bot.live_trading.run_from_state(market, price, quotes)

            remaining = bot.live_trading.store.open_trades()[0]
            self.assertAlmostEqual(remaining["shares"], before["shares"] - 1.0, places=6)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            reconciled = next(row for row in orders if row["external_order_id"] == "live-sell-pending")
            self.assertEqual(reconciled["status"], "FILLED")
            self.assertEqual(reconciled["external_status"], "TRADES_MATCHED")
            self.assertEqual(fake_client.fetch_order_calls[-1]["side"], "SELL")

    def test_live_manual_sell_closes_only_official_partial_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
                max_quote_age_ms=60_000,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.sell_response = LiveOrderResponse(
                True,
                "matched",
                "live-sell-partial",
                None,
                {"status": "matched", "makingAmount": "1000000", "takingAmount": "400000"},
                filled_shares=1.0,
                cash_spent=0.4,
                avg_fill_price=0.4,
            )
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-partial",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.50,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }
            bot._run_strategy_from_state()
            before = bot.live_trading.store.open_trades()[0]
            trade_id = int(before["id"])
            with bot._lock:
                bot.latest_quotes = {"Up": {"best_bid": 0.40, "bid_size": 100, "updated_at_ms": int(now * 1000)}}

            result = bot.sell_live_trade(trade_id)

            self.assertAlmostEqual(result["closed_trade"]["shares"], 1.0)
            remaining = bot.live_trading.store.open_trades()[0]
            self.assertAlmostEqual(remaining["shares"], before["shares"] - 1.0)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            self.assertTrue(any(row["external_order_id"] == "live-sell-partial" for row in orders))

    def test_live_manual_sell_rechecks_official_amounts_when_matched_response_has_no_amounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.buy_response = LiveOrderResponse(
                True,
                "matched",
                "live-buy-for-sell-recheck",
                None,
                {"status": "matched", "makingAmount": "2000000", "takingAmount": "4000000"},
                filled_shares=4.0,
                cash_spent=2.0,
                avg_fill_price=0.5,
            )
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-recheck",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                condition_id="0x" + "a" * 64,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)
            bot.live_trading.run_from_state(market, price, quotes)
            before = bot.live_trading.store.open_trades()[0]
            trade_id = int(before["id"])
            fake_client.fetch_order_calls.clear()
            fake_client.sell_response = LiveOrderResponse(
                True,
                "matched",
                "live-sell-no-amounts",
                None,
                {"success": True, "status": "matched", "orderID": "live-sell-no-amounts"},
            )
            fake_client.fetch_order_states = [
                LiveOrderResponse(
                    True,
                    "TRADES_MATCHED",
                    "live-sell-no-amounts",
                    None,
                    {"trades": [{"taker_order_id": "live-sell-no-amounts", "size": "1000000", "price": "0.4"}]},
                    filled_shares=1.0,
                    cash_spent=0.4,
                    avg_fill_price=0.4,
                )
            ]
            with bot._lock:
                bot.latest_quotes = {"Up": {"best_bid": 0.40, "bid_size": 100, "updated_at_ms": int(now * 1000)}}

            result = bot.sell_live_trade(trade_id)

            self.assertAlmostEqual(result["closed_trade"]["shares"], 1.0)
            remaining = bot.live_trading.store.open_trades()[0]
            self.assertAlmostEqual(remaining["shares"], before["shares"] - 1.0)
            self.assertEqual(fake_client.fetch_order_calls[-1]["side"], "SELL")

    def test_live_manual_sell_keeps_pending_when_matched_response_amount_recheck_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-recheck-error",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                condition_id="0x" + "a" * 64,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)
            bot.live_trading.run_from_state(market, price, quotes)
            trade_id = int(bot.live_trading.store.open_trades()[0]["id"])
            fake_client.sell_response = LiveOrderResponse(
                True,
                "matched",
                "live-sell-no-amounts-fetch-error",
                None,
                {"success": True, "status": "matched", "orderID": "live-sell-no-amounts-fetch-error"},
            )

            def fetch_error(**_kwargs):
                raise TimeoutError("official sell read timeout")

            fake_client.fetch_order_state = fetch_error
            with bot._lock:
                bot.latest_quotes = {"Up": {"best_bid": 0.40, "bid_size": 100, "updated_at_ms": int(now * 1000)}}

            with self.assertRaisesRegex(RuntimeError, "等待官方确认"):
                bot.sell_live_trade(trade_id)

            self.assertEqual(len(bot.live_trading.store.open_trades()), 1)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            pending_sell = next(row for row in orders if row["external_order_id"] == "live-sell-no-amounts-fetch-error")
            self.assertEqual(pending_sell["status"], STATUS_PENDING)
            sell_call_count = len(fake_client.sell_calls)
            with self.assertRaisesRegex(RuntimeError, "live sell already pending"):
                bot.sell_live_trade(trade_id)
            self.assertEqual(len(fake_client.sell_calls), sell_call_count)

    def test_live_manual_sell_disables_live_when_local_accounting_fails_after_official_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            bot.live_trading.client = fake_client
            bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-sell-accounting-fail",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
            quotes = {
                "Up": {
                    "best_bid": 0.50,
                    "best_ask": 0.52,
                    "ask_size": 100,
                    "asks": [{"price": 0.52, "size": 100}],
                    "updated_at_ms": int(now * 1000),
                }
            }
            with bot._lock:
                bot.current_market = market
                bot.latest_price = dict(price)
                bot.latest_quotes = dict(quotes)
            bot.live_trading.run_from_state(market, price, quotes)
            trade_id = int(bot.live_trading.store.open_trades()[0]["id"])
            original_fill_pending_exit = bot.live_trading.store.fill_external_pending_exit_order

            def fail_after_pending_exit(*_args, **_kwargs):
                raise RuntimeError("sqlite sell write failed after official fill")

            bot.live_trading.store.fill_external_pending_exit_order = fail_after_pending_exit
            with bot._lock:
                bot.latest_quotes = {"Up": {"best_bid": 0.40, "bid_size": 100, "updated_at_ms": int(now * 1000)}}

            with self.assertRaisesRegex(RuntimeError, "sqlite sell write failed"):
                bot.sell_live_trade(trade_id)
            bot.live_trading.store.fill_external_pending_exit_order = original_fill_pending_exit
            with self.assertRaisesRegex(RuntimeError, "live sell already pending"):
                bot.sell_live_trade(trade_id)

            self.assertFalse(bot.live_trading.config.enabled)
            self.assertFalse(bot.live_trading.process_lock.locked)
            self.assertEqual(len(fake_client.sell_calls), 1)
            self.assertIn("local accounting failed", bot.live_trading.last_error)
            orders = bot.orders_page(account_scope="live")["recent_orders"]
            pending_sell = next(row for row in orders if row["external_order_id"] == "live-sell-1")
            self.assertEqual(pending_sell["status"], STATUS_PENDING)

    def test_live_order_retry_reuses_same_signed_order(self) -> None:
        class FakePostingClient:
            def __init__(self) -> None:
                self.calls = []

            def post_order(self, order, order_type):
                self.calls.append((order, order_type))
                if len(self.calls) == 1:
                    raise TimeoutError("timeout before response")
                return {"success": True, "status": "matched", "orderID": "same-order"}

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = PolymarketLiveClient(settings)
            posting_client = FakePostingClient()
            sdk = {"OrderType": type("OrderType", (), {"FAK": "FAK"})}
            signed_order = object()

            response = live_client._post_signed_order_with_retry(
                posting_client,
                sdk,
                signed_order,
                "BUY",
                retry_count=1,
                retry_delay_ms=0,
            )

            self.assertTrue(response.success)
            self.assertEqual(response.order_id, "same-order")
            self.assertEqual(len(posting_client.calls), 2)
            self.assertIs(posting_client.calls[0][0], signed_order)
            self.assertIs(posting_client.calls[1][0], signed_order)
            self.assertEqual(response.raw["attempts"], 2)

    def test_live_retryable_post_exhaustion_keeps_signed_hash_pending(self) -> None:
        class FakeBuilder:
            def build_order_typed_data(self, order):
                return {"order": order}

            def build_order_hash(self, typed_data):
                return "0xsignedorderhash"

        class FakePostingClient:
            def __init__(self) -> None:
                self.calls = []
                self.builder = FakeBuilder()

            def post_order(self, order, order_type):
                self.calls.append((order, order_type))
                raise TimeoutError("timeout before response")

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = PolymarketLiveClient(settings)
            posting_client = FakePostingClient()
            sdk = {"OrderType": type("OrderType", (), {"FAK": "FAK"})}
            signed_order = object()

            response = live_client._post_signed_order_with_retry(
                posting_client,
                sdk,
                signed_order,
                "BUY",
                retry_count=1,
                retry_delay_ms=0,
            )

            self.assertTrue(response.success)
            self.assertEqual(response.status, "POST_STATUS_UNKNOWN")
            self.assertEqual(response.order_id, "0xsignedorderhash")
            self.assertTrue(response.raw["submitted_to_clob_unknown"])
            self.assertEqual(len(posting_client.calls), 2)

    def test_live_create_market_order_retry_happens_before_submit(self) -> None:
        class RetryableError(Exception):
            status_code = 503

        class FakeCreateClient:
            def __init__(self) -> None:
                self.calls = 0

            def create_market_order(self, order_args, options):
                self.calls += 1
                if self.calls == 1:
                    raise RetryableError("market metadata unavailable")
                return {"signed": True}

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = PolymarketLiveClient(settings)
            fake_client = FakeCreateClient()

            signed_order = live_client._create_signed_market_order_with_retry(
                fake_client,
                object(),
                object(),
                retry_count=1,
                retry_delay_ms=0,
            )

            self.assertEqual(signed_order, {"signed": True})
            self.assertEqual(fake_client.calls, 2)

    def test_live_sign_market_order_preview_does_not_submit(self) -> None:
        class FakeMarketOrderArgs:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        class FakePartialCreateOrderOptions:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        class FakeSide:
            BUY = "BUY"
            SELL = "SELL"

        class FakeOrderType:
            FAK = "FAK"

        class FakeBuilder:
            def build_order_typed_data(self, order):
                return {"order": order}

            def build_order_hash(self, typed_data):
                return "0xpreviewhash"

        class FakeCreateClient:
            def __init__(self) -> None:
                self.builder = FakeBuilder()
                self.create_calls = []
                self.post_calls = []

            def create_market_order(self, order_args, options):
                self.create_calls.append((order_args, options))
                return {"signed": "order"}

            def post_order(self, order, order_type):
                self.post_calls.append((order, order_type))
                raise AssertionError("sign preview must not submit")

        class PreviewClient(PolymarketLiveClient):
            def __init__(self, settings) -> None:
                super().__init__(settings)
                self.fake_client = FakeCreateClient()

            def _sdk(self):
                return {
                    "MarketOrderArgs": FakeMarketOrderArgs,
                    "OrderType": FakeOrderType,
                    "PartialCreateOrderOptions": FakePartialCreateOrderOptions,
                    "Side": FakeSide,
                }

            def _authenticated_client(self, sdk):
                return self.fake_client

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = PreviewClient(settings)

            preview = live_client.sign_market_order_preview(
                token_id="token-1",
                amount=5.1234567,
                side="BUY",
                price=0.52123,
                tick_size="0.001",
                neg_risk=True,
                retry_count=0,
                retry_delay_ms=0,
            )

            self.assertTrue(preview["ready"])
            self.assertEqual(preview["status"], "SIGNED")
            self.assertFalse(preview["submitted_to_clob"])
            self.assertEqual(preview["signed_order_hash"], "0xpreviewhash")
            self.assertNotIn("raw", preview)
            self.assertEqual(live_client.fake_client.post_calls, [])
            order_args, options = live_client.fake_client.create_calls[0]
            self.assertEqual(order_args.kwargs["token_id"], "token-1")
            self.assertEqual(order_args.kwargs["side"], "BUY")
            self.assertEqual(order_args.kwargs["order_type"], "FAK")
            self.assertAlmostEqual(order_args.kwargs["user_usdc_balance"], 5.123457)
            self.assertEqual(options.kwargs["tick_size"], "0.001")
            self.assertTrue(options.kwargs["neg_risk"])

    def test_live_order_response_parses_fixed_math_matched_amounts(self) -> None:
        class FakePostingClient:
            def post_order(self, order, order_type):
                return {
                    "success": True,
                    "status": "matched",
                    "orderID": "official-amounts",
                    "makingAmount": "2000000",
                    "takingAmount": "4000000",
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = PolymarketLiveClient(settings)
            sdk = {"OrderType": type("OrderType", (), {"FAK": "FAK"})}

            response = live_client._post_signed_order_with_retry(
                FakePostingClient(),
                sdk,
                object(),
                "BUY",
                retry_count=0,
                retry_delay_ms=0,
            )

            self.assertTrue(response.success)
            self.assertEqual(response.order_id, "official-amounts")
            self.assertAlmostEqual(response.cash_spent, 2.0)
            self.assertAlmostEqual(response.filled_shares, 4.0)
            self.assertAlmostEqual(response.avg_fill_price, 0.5)

    def test_live_order_response_parses_decimal_cash_and_integer_shares(self) -> None:
        class FakePostingClient:
            def post_order(self, order, order_type):
                return {
                    "success": True,
                    "status": "matched",
                    "orderID": "official-decimal-amounts",
                    "makingAmount": "1.95",
                    "takingAmount": "3",
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = PolymarketLiveClient(settings)
            sdk = {"OrderType": type("OrderType", (), {"FAK": "FAK"})}

            response = live_client._post_signed_order_with_retry(
                FakePostingClient(),
                sdk,
                object(),
                "BUY",
                retry_count=0,
                retry_delay_ms=0,
            )

            self.assertTrue(response.success)
            self.assertEqual(response.order_id, "official-decimal-amounts")
            self.assertAlmostEqual(response.cash_spent, 1.95)
            self.assertAlmostEqual(response.filled_shares, 3.0)
            self.assertAlmostEqual(response.avg_fill_price, 0.65)

    def test_live_order_response_parses_sell_decimal_cash_and_integer_shares(self) -> None:
        class FakePostingClient:
            def post_order(self, order, order_type):
                return {
                    "success": True,
                    "status": "matched",
                    "orderID": "official-sell-decimal-amounts",
                    "makingAmount": "3",
                    "takingAmount": "1.2",
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = PolymarketLiveClient(settings)
            sdk = {"OrderType": type("OrderType", (), {"FAK": "FAK"})}

            response = live_client._post_signed_order_with_retry(
                FakePostingClient(),
                sdk,
                object(),
                "SELL",
                retry_count=0,
                retry_delay_ms=0,
            )

            self.assertTrue(response.success)
            self.assertEqual(response.order_id, "official-sell-decimal-amounts")
            self.assertAlmostEqual(response.cash_spent, 1.2)
            self.assertAlmostEqual(response.filled_shares, 3.0)
            self.assertAlmostEqual(response.avg_fill_price, 0.4)

    def test_live_terminal_no_fill_classifies_invalid_as_rejected(self) -> None:
        self.assertEqual(
            _response_terminal_no_fill_local_status(
                LiveOrderResponse(
                    False,
                    "ORDER_STATUS_INVALID",
                    "invalid-order",
                    "ORDER_STATUS_INVALID",
                    {"order": {"status": "ORDER_STATUS_INVALID", "size_matched": "0", "price": "0.5"}},
                )
            ),
            STATUS_REJECTED,
        )
        self.assertEqual(
            _response_terminal_no_fill_local_status(
                LiveOrderResponse(
                    True,
                    "ORDER_STATUS_UNMATCHED",
                    "unmatched-order",
                    None,
                    {"order": {"status": "ORDER_STATUS_UNMATCHED", "size_matched": "0", "price": "0.5"}},
                )
            ),
            STATUS_CANCELED,
        )
        self.assertIsNone(
            _response_terminal_no_fill_local_status(
                LiveOrderResponse(True, "ORDER_STATUS_LIVE", "live-order", None, {"order": {"status": "live"}})
            )
        )

    def test_live_wallet_state_parses_collateral_balance_and_allowance(self) -> None:
        class FakeBalanceParams:
            def __init__(self, asset_type=None, signature_type=-1, **_kwargs) -> None:
                self.asset_type = asset_type
                self.token_id = None
                self.signature_type = signature_type

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"

        class FakeSdkClient:
            def update_balance_allowance(self, params):
                self.update_params = params
                return {"synced": True}

            def get_balance_allowance(self, params):
                self.params = params
                return {"balance": "20000000", "allowance": "15000000"}

        class WalletClient(PolymarketLiveClient):
            def __init__(self, settings) -> None:
                super().__init__(settings)
                self.fake_client = FakeSdkClient()

            def _sdk(self):
                return {"BalanceAllowanceParams": FakeBalanceParams, "AssetType": FakeAssetType}

            def _authenticated_client(self, sdk):
                return self.fake_client

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = WalletClient(settings)

            with patch.dict("os.environ", {"POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": "3"}, clear=False):
                state = live_client.wallet_state(required_cash=10.0, force=True)

            self.assertTrue(state["ready"])
            self.assertAlmostEqual(state["balance"], 20.0)
            self.assertAlmostEqual(state["allowance"], 15.0)
            self.assertEqual(live_client.fake_client.params.asset_type, "COLLATERAL")
            self.assertEqual(live_client.fake_client.params.signature_type, 3)
            self.assertEqual(live_client.fake_client.update_params.asset_type, "COLLATERAL")

    def test_live_wallet_state_accepts_allowances_map_without_top_level_allowance(self) -> None:
        class FakeBalanceParams:
            def __init__(self, asset_type=None, signature_type=-1, **_kwargs) -> None:
                self.asset_type = asset_type
                self.signature_type = signature_type

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"

        class FakeSdkClient:
            def update_balance_allowance(self, params):
                return {"synced": True}

            def get_balance_allowance(self, params):
                return {
                    "balance": "2379349",
                    "allowances": {
                        "0xE111180000d2663C0091e4f400237545B87B996B": (
                            "115792089237316195423570985008687907853269984665640564039457584007913129639935"
                        ),
                        "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": "0",
                    },
                }

        class WalletClient(PolymarketLiveClient):
            def __init__(self, settings) -> None:
                super().__init__(settings)
                self.fake_client = FakeSdkClient()

            def _sdk(self):
                return {"BalanceAllowanceParams": FakeBalanceParams, "AssetType": FakeAssetType}

            def _authenticated_client(self, sdk):
                return self.fake_client

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = WalletClient(settings)

            with patch.dict("os.environ", {"POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": "1"}, clear=False):
                state = live_client.wallet_state(required_cash=1.0, force=True)

            self.assertTrue(state["ready"])
            self.assertAlmostEqual(state["balance"], 2.379349)
            self.assertGreater(state["allowance"], 1.0)
            self.assertEqual(state["errors"], [])

    def test_live_wallet_state_retries_balance_allowance_read(self) -> None:
        class FakeBalanceParams:
            def __init__(self, asset_type=None, signature_type=-1, **_kwargs) -> None:
                self.asset_type = asset_type
                self.signature_type = signature_type

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"

        class FakeSdkClient:
            def __init__(self) -> None:
                self.read_calls = 0

            def update_balance_allowance(self, params):
                return {"synced": True}

            def get_balance_allowance(self, params):
                self.read_calls += 1
                if self.read_calls == 1:
                    raise TimeoutError("timeout reading balance")
                return {"balance": "20000000", "allowance": "15000000"}

        class WalletClient(PolymarketLiveClient):
            def __init__(self, settings) -> None:
                super().__init__(settings)
                self.fake_client = FakeSdkClient()

            def _sdk(self):
                return {"BalanceAllowanceParams": FakeBalanceParams, "AssetType": FakeAssetType}

            def _authenticated_client(self, sdk):
                return self.fake_client

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = WalletClient(settings)

            state = live_client.wallet_state(required_cash=10.0, force=True, retry_count=1, retry_delay_ms=0)

            self.assertTrue(state["ready"])
            self.assertEqual(live_client.fake_client.read_calls, 2)
            self.assertEqual(state["raw"]["read_retry_reasons"], ["TimeoutError: timeout reading balance"])

    def test_live_client_rebuilds_cached_sdk_client_when_credentials_change(self) -> None:
        class FakeBalanceParams:
            def __init__(self, asset_type=None, signature_type=-1, **_kwargs) -> None:
                self.asset_type = asset_type
                self.signature_type = signature_type

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"

        class FakeApiCreds:
            def __init__(self, api_key=None, api_secret=None, api_passphrase=None, **_kwargs) -> None:
                self.api_key = api_key
                self.api_secret = api_secret
                self.api_passphrase = api_passphrase

        class FakeSdkClient:
            instances = []

            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.read_calls = 0
                FakeSdkClient.instances.append(self)

            def update_balance_allowance(self, params):
                self.update_params = params
                return {"synced": True}

            def get_balance_allowance(self, params):
                self.params = params
                self.read_calls += 1
                return {"balance": "11000000", "allowance": "11000000"}

        class RebuildClient(PolymarketLiveClient):
            def _sdk(self):
                return {
                    "ClobClient": FakeSdkClient,
                    "ApiCreds": FakeApiCreds,
                    "BalanceAllowanceParams": FakeBalanceParams,
                    "AssetType": FakeAssetType,
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = RebuildClient(settings)
            first_env = {
                "POLYBOT2OTHER_LIVE_PRIVATE_KEY": "0x" + "1" * 64,
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": "3",
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS": "0x" + "2" * 40,
                "POLYBOT2OTHER_LIVE_API_KEY": "key-a",
                "POLYBOT2OTHER_LIVE_API_SECRET": "secret-a",
                "POLYBOT2OTHER_LIVE_API_PASSPHRASE": "pass-a",
            }
            second_env = {
                **first_env,
                "POLYBOT2OTHER_LIVE_PRIVATE_KEY": "0x" + "3" * 64,
                "POLYBOT2OTHER_LIVE_API_KEY": "key-b",
                "POLYBOT2OTHER_LIVE_API_SECRET": "secret-b",
                "POLYBOT2OTHER_LIVE_API_PASSPHRASE": "pass-b",
            }

            with patch.dict("os.environ", first_env, clear=False):
                first = live_client.wallet_state(required_cash=1.0)
            with patch.dict("os.environ", second_env, clear=False):
                second = live_client.wallet_state(required_cash=1.0)

            self.assertTrue(first["ready"])
            self.assertTrue(second["ready"])
            self.assertEqual(len(FakeSdkClient.instances), 2)
            self.assertEqual(FakeSdkClient.instances[0].kwargs["key"], "0x" + "1" * 64)
            self.assertEqual(FakeSdkClient.instances[1].kwargs["key"], "0x" + "3" * 64)
            self.assertEqual(FakeSdkClient.instances[1].kwargs["creds"].api_key, "key-b")

    def test_live_wallet_state_blocks_when_allowance_is_below_stake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
                min_edge=0.0,
                stake_dollars=5.0,
                live_trading_default_stake_dollars=5.0,
            )
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            fake_client = FakeLiveClient()
            fake_client.wallet_payload = {
                "ready": False,
                "errors": ["Polymarket collateral allowance 1.000000 低于本次实盘预算 5.000000"],
                "balance": 20.0,
                "allowance": 1.0,
            }
            bot.live_trading.client = fake_client
            payload = bot.live_trading.update_settings(
                {"enabled": True, "compliance_acknowledged": True, "initial_balance": 20.0, "stake_dollars": 5.0}
            )
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-live-wallet-block",
                symbol="BTC",
                started_at=now - 60,
                ends_at=now + 120,
                target_price=100.0,
                up_token="up-token",
                down_token="down-token",
            )
            store.upsert_round(market)
            with bot._lock:
                bot.current_market = market
                bot.latest_price = {"chainlink": 102.0, "chainlink_updated_ms": int(now * 1000)}
                bot.latest_quotes = {
                    "Up": {
                        "best_bid": 0.49,
                        "best_ask": 0.52,
                        "ask_size": 100,
                        "asks": [{"price": 0.52, "size": 100}],
                        "updated_at_ms": int(now * 1000),
                    }
                }

            bot._run_strategy_from_state()

            self.assertFalse(payload["enabled"])
            self.assertIn("allowance", bot.live_trading.last_error)
            self.assertEqual(fake_client.buy_calls, [])
            self.assertEqual(bot.live_trading.store.open_trades(), [])

    def test_live_token_state_parses_conditional_balance_and_allowance(self) -> None:
        class FakeBalanceParams:
            def __init__(self, asset_type=None, token_id=None, signature_type=-1, **_kwargs) -> None:
                self.asset_type = asset_type
                self.token_id = token_id
                self.signature_type = signature_type

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"
            CONDITIONAL = "CONDITIONAL"

        class FakeSdkClient:
            def update_balance_allowance(self, params):
                self.update_params = params
                return {"synced": True}

            def get_balance_allowance(self, params):
                self.params = params
                return {"balance": "12000000", "allowance": "11000000"}

        class TokenClient(PolymarketLiveClient):
            def __init__(self, settings) -> None:
                super().__init__(settings)
                self.fake_client = FakeSdkClient()

            def _sdk(self):
                return {"BalanceAllowanceParams": FakeBalanceParams, "AssetType": FakeAssetType}

            def _authenticated_client(self, sdk):
                return self.fake_client

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = TokenClient(settings)

            with patch.dict("os.environ", {"POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": "3"}, clear=False):
                state = live_client.token_state(token_id="token-1", required_shares=10.0, force=True)

            self.assertTrue(state["ready"])
            self.assertAlmostEqual(state["balance"], 12.0)
            self.assertAlmostEqual(state["allowance"], 11.0)
            self.assertEqual(live_client.fake_client.params.asset_type, "CONDITIONAL")
            self.assertEqual(live_client.fake_client.params.token_id, "token-1")
            self.assertEqual(live_client.fake_client.params.signature_type, 3)
            self.assertEqual(live_client.fake_client.update_params.token_id, "token-1")

    def test_live_fetch_order_state_uses_matching_trade_amounts(self) -> None:
        class FakeTradeParams:
            def __init__(self, market=None, asset_id=None, **_kwargs) -> None:
                self.market = market
                self.asset_id = asset_id
                self.after = None
                self.before = None
                self.maker_address = None
                self.id = None

        class FakeSdkClient:
            def get_order(self, order_id):
                return {"id": order_id, "status": "ORDER_STATUS_LIVE", "size_matched": "0", "price": "0.5"}

            def get_trades(self, params, only_first_page=False):
                self.params = params
                self.only_first_page = only_first_page
                return [
                    {
                        "id": "trade-1",
                        "taker_order_id": "order-1",
                        "size": "4000000",
                        "price": "0.5",
                        "status": "TRADE_STATUS_CONFIRMED",
                    },
                    {
                        "id": "trade-2",
                        "taker_order_id": "other-order",
                        "size": "1000000",
                        "price": "0.9",
                        "status": "TRADE_STATUS_CONFIRMED",
                    },
                ]

        class FetchClient(PolymarketLiveClient):
            def __init__(self, settings) -> None:
                super().__init__(settings)
                self.fake_client = FakeSdkClient()

            def _sdk(self):
                return {"TradeParams": FakeTradeParams}

            def _authenticated_client(self, sdk):
                return self.fake_client

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = FetchClient(settings)

            response = live_client.fetch_order_state(
                order_id="order-1",
                side="BUY",
                token_id="token-1",
                condition_id="0x" + "a" * 64,
            )

            self.assertIsNotNone(response)
            self.assertEqual(response.status, "TRADES_MATCHED")
            self.assertAlmostEqual(response.filled_shares, 4.0)
            self.assertAlmostEqual(response.cash_spent, 2.0)
            self.assertEqual(live_client.fake_client.params.asset_id, "token-1")
            self.assertTrue(live_client.fake_client.only_first_page)

    def test_live_fetch_order_state_retries_official_order_read(self) -> None:
        class FakeTradeParams:
            def __init__(self, market=None, asset_id=None, **_kwargs) -> None:
                self.market = market
                self.asset_id = asset_id

        class FakeSdkClient:
            def __init__(self) -> None:
                self.order_calls = 0

            def get_order(self, order_id):
                self.order_calls += 1
                if self.order_calls == 1:
                    raise TimeoutError("timeout reading order")
                return {"id": order_id, "status": "ORDER_STATUS_LIVE", "size_matched": "0", "price": "0.5"}

            def get_trades(self, params, only_first_page=False):
                return [
                    {
                        "id": "trade-1",
                        "taker_order_id": "order-1",
                        "size": "4000000",
                        "price": "0.5",
                        "status": "TRADE_STATUS_CONFIRMED",
                    }
                ]

        class FetchClient(PolymarketLiveClient):
            def __init__(self, settings) -> None:
                super().__init__(settings)
                self.fake_client = FakeSdkClient()

            def _sdk(self):
                return {"TradeParams": FakeTradeParams}

            def _authenticated_client(self, sdk):
                return self.fake_client

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            live_client = FetchClient(settings)

            response = live_client.fetch_order_state(
                order_id="order-1",
                side="BUY",
                token_id="token-1",
                condition_id="0x" + "a" * 64,
                retry_count=1,
                retry_delay_ms=0,
            )

            self.assertIsNotNone(response)
            self.assertEqual(response.status, "TRADES_MATCHED")
            self.assertEqual(live_client.fake_client.order_calls, 2)
            self.assertEqual(response.raw["order_retry_reasons"], ["TimeoutError: timeout reading order"])

    def test_live_readiness_requires_signature_type_and_funder(self) -> None:
        class ReadyClient(PolymarketLiveClient):
            def _sdk(self):
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            client = ReadyClient(settings)
            with patch.dict(
                "os.environ",
                {
                    "POLYBOT2OTHER_LIVE_PRIVATE_KEY": "0x" + "1" * 64,
                    "POLYBOT2OTHER_LIVE_API_KEY": "",
                    "POLYBOT2OTHER_LIVE_API_SECRET": "",
                    "POLYBOT2OTHER_LIVE_API_PASSPHRASE": "",
                    "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": "",
                    "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS": "",
                },
                clear=False,
            ):
                errors = client.readiness_errors()

            self.assertTrue(any("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE" in item for item in errors))
            self.assertTrue(any("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS" in item for item in errors))

    def test_live_readiness_exposes_credential_presence_without_secret_values(self) -> None:
        class ReadyClient(PolymarketLiveClient):
            def _sdk(self):
                return {}

            def _sdk_compatibility_errors(self, sdk):
                return []

            def wallet_state(self, **kwargs):
                return {"ready": True, "errors": [], "balance": 10.0, "allowance": 10.0}

            def geoblock_state(self, **kwargs):
                return {"ready": True, "blocked": False, "country": "KR", "region": "11", "errors": []}

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.live"
            private_key = "0x" + "1" * 64
            env_path.write_text(
                f"POLYBOT2OTHER_LIVE_PRIVATE_KEY={private_key}\n"
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=3\n"
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=0x" + "2" * 40 + "\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {}, clear=True):
                    settings = load_settings()
                    readiness = ReadyClient(settings).readiness(required_cash=2.0)

                    self.assertTrue(readiness["ready"])
                    self.assertTrue(readiness["credential_presence"]["private_key"])
                    self.assertTrue(readiness["credential_presence"]["signature_type"])
                    self.assertTrue(readiness["credential_presence"]["funder_address"])
                    self.assertEqual(readiness["sdk_status"]["package"], "py_clob_client_v2")
                    self.assertTrue(readiness["sdk_status"]["compatible"])
                    self.assertEqual(readiness["env_files"][0]["path"], ".env.live")
                    self.assertNotIn(private_key, str(readiness))
            finally:
                os.chdir(previous_cwd)

    def test_live_readiness_does_not_treat_blank_secret_template_as_secret_file(self) -> None:
        class ReadyClient(PolymarketLiveClient):
            def _sdk(self):
                return {}

            def _sdk_compatibility_errors(self, sdk):
                return []

            def geoblock_state(self, **kwargs):
                return {"ready": True, "blocked": False, "country": "KR", "region": "11", "errors": []}

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.live"
            env_path.write_text(
                "POLYBOT2OTHER_LIVE_PRIVATE_KEY=\n"
                "POLYBOT2OTHER_LIVE_API_SECRET=\n",
                encoding="utf-8",
            )
            env_path.chmod(0o644)
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {}, clear=True):
                    settings = load_settings()
                    readiness = ReadyClient(settings).readiness(required_cash=2.0)

                    self.assertFalse(readiness["ready"])
                    self.assertEqual(readiness["env_files"][0]["mode"], "0o644")
                    self.assertFalse(readiness["env_files"][0]["secure_permissions"])
                    self.assertEqual(readiness["env_files"][0]["sensitive_keys_present"], [])
                    self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", readiness["env_files"][0]["empty_keys"])
                    self.assertFalse(any("chmod 600 .env.live" in item for item in readiness["errors"]))
                    self.assertTrue(any("POLYBOT2OTHER_LIVE_PRIVATE_KEY" in item for item in readiness["errors"]))
            finally:
                os.chdir(previous_cwd)

    def test_live_readiness_blocks_secret_env_file_with_loose_permissions(self) -> None:
        class ReadyClient(PolymarketLiveClient):
            def _sdk(self):
                return {}

            def _sdk_compatibility_errors(self, sdk):
                return []

            def wallet_state(self, **kwargs):
                return {"ready": True, "errors": [], "balance": 10.0, "allowance": 10.0}

            def geoblock_state(self, **kwargs):
                return {"ready": True, "blocked": False, "country": "KR", "region": "11", "errors": []}

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.live"
            private_key = "0x" + "1" * 64
            env_path.write_text(
                f"POLYBOT2OTHER_LIVE_PRIVATE_KEY={private_key}\n"
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=3\n"
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=0x" + "2" * 40 + "\n",
                encoding="utf-8",
            )
            env_path.chmod(0o644)
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {}, clear=True):
                    settings = load_settings()
                    readiness = ReadyClient(settings).readiness(required_cash=2.0)

                    self.assertFalse(readiness["ready"])
                    self.assertEqual(readiness["env_files"][0]["mode"], "0o644")
                    self.assertFalse(readiness["env_files"][0]["secure_permissions"])
                    self.assertIn("POLYBOT2OTHER_LIVE_PRIVATE_KEY", readiness["env_files"][0]["sensitive_keys_present"])
                    self.assertTrue(any("chmod 600 .env.live" in item for item in readiness["errors"]))
                    self.assertNotIn(private_key, str(readiness))
            finally:
                os.chdir(previous_cwd)

    def test_live_readiness_exposes_signer_and_funder_address_summary(self) -> None:
        class ReadyClient(PolymarketLiveClient):
            def wallet_state(self, **kwargs):
                return {"ready": True, "errors": [], "balance": 10.0, "allowance": 10.0}

            def geoblock_state(self, **kwargs):
                return {"ready": True, "blocked": False, "country": "KR", "region": "11", "errors": []}

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            private_key = "0x" + "1" * 64
            signer_address = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
            with patch.dict(
                "os.environ",
                {
                    "POLYBOT2OTHER_LIVE_PRIVATE_KEY": private_key,
                    "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": "0",
                    "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS": signer_address,
                    "POLYBOT2OTHER_LIVE_API_KEY": "",
                    "POLYBOT2OTHER_LIVE_API_SECRET": "",
                    "POLYBOT2OTHER_LIVE_API_PASSPHRASE": "",
                },
                clear=False,
            ):
                readiness = ReadyClient(settings).readiness(required_cash=2.0)

            addresses = readiness["credential_addresses"]
            self.assertTrue(readiness["ready"])
            self.assertEqual(addresses["signature_type"], 0)
            self.assertEqual(addresses["signer_address"].lower(), signer_address.lower())
            self.assertEqual(addresses["funder_address"].lower(), signer_address.lower())
            self.assertTrue(addresses["signer_matches_funder"])
            self.assertEqual(addresses["signer_address_masked"], "0x19E7...ff2A")
            self.assertNotIn(private_key, str(readiness))

    def test_live_readiness_blocks_eoa_funder_that_does_not_match_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            with patch.dict(
                "os.environ",
                {
                    "POLYBOT2OTHER_LIVE_PRIVATE_KEY": "0x" + "1" * 64,
                    "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": "0",
                    "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS": "0x" + "2" * 40,
                    "POLYBOT2OTHER_LIVE_API_KEY": "",
                    "POLYBOT2OTHER_LIVE_API_SECRET": "",
                    "POLYBOT2OTHER_LIVE_API_PASSPHRASE": "",
                },
                clear=False,
            ):
                client = PolymarketLiveClient(settings)
                errors = client.readiness_errors()
                summary = client._credential_address_summary()

            self.assertTrue(any("SIGNATURE_TYPE=0" in item for item in errors))
            self.assertFalse(summary["signer_matches_funder"])
            self.assertTrue(summary["warnings"])

    def test_live_sdk_compatibility_matches_installed_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            client = PolymarketLiveClient(settings)

            errors = client._sdk_compatibility_errors(client._sdk())
            status = client.sdk_status()

            self.assertEqual(errors, [])
            self.assertEqual(status["package"], "py_clob_client_v2")
            self.assertTrue(status["version"])
            self.assertTrue(status["compatible"])
            self.assertEqual(status["errors"], [])

    def test_live_sdk_compatibility_reports_missing_method(self) -> None:
        class FakeClobClient:
            def create_market_order(self, order_args, options=None):
                return {}

        class FakeArgs:
            def __init__(self, token_id=None, amount=None, side=None, **_kwargs) -> None:
                pass

        class FakeOptions:
            def __init__(self, tick_size=None, neg_risk=None) -> None:
                pass

        class FakeTradeParams:
            def __init__(self, market=None, asset_id=None) -> None:
                pass

        class FakeBalanceParams:
            def __init__(self, asset_type=None, token_id=None) -> None:
                pass

        class FakeOpenOrderParams:
            def __init__(self, id=None, market=None, asset_id=None) -> None:
                pass

        class FakeApiCreds:
            def __init__(self, api_key=None, api_secret=None, api_passphrase=None) -> None:
                pass

        class FakeOrderType:
            FAK = "FAK"

        class FakeSide:
            BUY = "BUY"
            SELL = "SELL"

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"
            CONDITIONAL = "CONDITIONAL"

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            client = PolymarketLiveClient(settings)
            sdk = {
                "ApiCreds": FakeApiCreds,
                "ClobClient": FakeClobClient,
                "MarketOrderArgs": FakeArgs,
                "OrderType": FakeOrderType,
                "PartialCreateOrderOptions": FakeOptions,
                "Side": FakeSide,
                "TradeParams": FakeTradeParams,
                "BalanceAllowanceParams": FakeBalanceParams,
                "AssetType": FakeAssetType,
                "OpenOrderParams": FakeOpenOrderParams,
            }

            errors = client._sdk_compatibility_errors(sdk)

            self.assertTrue(any("post_order" in item for item in errors))

    def test_live_sdk_compatibility_requires_market_order_budget_parameters(self) -> None:
        class FakeClobClient:
            def __init__(
                self,
                host=None,
                chain_id=None,
                key=None,
                creds=None,
                signature_type=None,
                funder=None,
                retry_on_error=False,
            ) -> None:
                pass

            def create_market_order(self, order_args, options=None):
                return {}

            def post_order(self, order, order_type=None):
                return {}

            def update_balance_allowance(self, params=None):
                return {}

            def get_balance_allowance(self, params=None):
                return {}

            def get_order(self, order_id):
                return {}

            def get_trades(self, params=None, only_first_page=False):
                return []

            def get_open_orders(self, params=None, only_first_page=False):
                return []

            def cancel_all(self):
                return {}

            def create_or_derive_api_key(self):
                return {}

        class FakeArgs:
            def __init__(self, token_id=None, amount=None, side=None) -> None:
                pass

        class FakeOptions:
            def __init__(self, tick_size=None, neg_risk=None) -> None:
                pass

        class FakeTradeParams:
            def __init__(self, market=None, asset_id=None) -> None:
                pass

        class FakeBalanceParams:
            def __init__(self, asset_type=None, token_id=None, signature_type=-1) -> None:
                pass

        class FakeOpenOrderParams:
            def __init__(self, id=None, market=None, asset_id=None) -> None:
                pass

        class FakeApiCreds:
            def __init__(self, api_key=None, api_secret=None, api_passphrase=None) -> None:
                pass

        class FakeOrderType:
            FAK = "FAK"

        class FakeSide:
            BUY = "BUY"
            SELL = "SELL"

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"
            CONDITIONAL = "CONDITIONAL"

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                live_trading_db_path=Path(tmp) / "live.sqlite3",
                live_trading_settings_path=Path(tmp) / "live-settings.json",
            )
            client = PolymarketLiveClient(settings)
            sdk = {
                "ApiCreds": FakeApiCreds,
                "ClobClient": FakeClobClient,
                "MarketOrderArgs": FakeArgs,
                "OrderType": FakeOrderType,
                "PartialCreateOrderOptions": FakeOptions,
                "Side": FakeSide,
                "TradeParams": FakeTradeParams,
                "BalanceAllowanceParams": FakeBalanceParams,
                "AssetType": FakeAssetType,
                "OpenOrderParams": FakeOpenOrderParams,
            }

            errors = client._sdk_compatibility_errors(sdk)

            self.assertTrue(any("MarketOrderArgs" in item and "price" in item for item in errors))
            self.assertTrue(any("MarketOrderArgs" in item and "user_usdc_balance" in item for item in errors))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import os
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from polybot2other.actor_analysis import build_actor_analysis
from polybot2other.config import Settings, env_file_status, load_settings, reload_live_credential_env
from polybot2other.bot import (
    LiveOnceBlockedError,
    PaperTradingBot,
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
    SINGLE_ENTRY_MODE_LEGACY,
    SINGLE_ENTRY_MODE_REVERSAL,
    SINGLE_ENTRY_MODE_STOP_AND_FLIP,
    SINGLE_ENTRY_MODE_STRICT,
    STRATEGY_VARIANTS,
)
from polybot2other.live import (
    LIVE_STARTUP_REARM_MESSAGE,
    LIVE_VARIANT_ID,
    LiveOrderResponse,
    PolymarketLiveClient,
    _response_terminal_no_fill_local_status,
)
from polybot2other.live_doctor import build_live_doctor_from_bot, main as live_doctor_main
from polybot2other.live_env_setup import LiveEnvSetupValues, validate_live_env_values, write_live_env_file
from polybot2other.live_evidence import build_live_evidence_payload, main as live_evidence_main
from polybot2other.live_once import main as live_once_main
from polybot2other.live_preflight import build_live_preflight_payload, main as live_preflight_main
from polybot2other.models import MarketRound, PaperFill, PaperFillLevel, Signal
from polybot2other.polymarket import PolymarketClient
from polybot2other.report_snapshot import generate_strategy_experiment_report_snapshot
from polybot2other.storage import (
    SETTLEMENT_SOURCE_CHAINLINK,
    SETTLEMENT_SOURCE_EARLY_EXIT,
    SETTLEMENT_SOURCE_POLYMARKET,
    TradeStore,
)
from polybot2other.strategy import RealBtcFiveMinuteStrategy, input_from_snapshot
from polybot2other.web import _strategy_experiments_retrospective_report_html


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

            settled = store.settle_due_rounds({"BTC": 99.5}, now)

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

    def test_bot_rechecks_fallback_settlement_until_official_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3")
            store = TradeStore(settings.db_path, settings.initial_balance)
            bot = PaperTradingBot(settings, store)
            now = time.time()
            market = MarketRound(
                round_id="btc-updown-5m-bot-official-recheck",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            store.upsert_round(market)
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "bot official recheck")
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
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
                round_id="btc-updown-5m-official-broadcast",
                symbol="BTC",
                started_at=now - 301,
                ends_at=now - 1,
                target_price=100.0,
            )
            signal = Signal("BTC", "Down", 0.7, 0.5, -10.0, "official broadcast")
            store.upsert_round(market)
            store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
            store.settle_due_rounds({"BTC": 99.5}, now)

            variant_bot = bot.strategy_experiments._bots["SINGLE_FAK"]
            variant_bot.store.upsert_round(market)
            variant_bot.store.place_trade(type("Intent", (), {"market": market, "signal": signal, "stake_dollars": 5.0})())
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
                round_id="btc-updown-5m-official-backfill-price",
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

            snapshot = bot.ingest_live_snapshot(payload)

            self.assertEqual(snapshot["runtime"]["current_market"]["target_price"], 0.0)
            self.assertNotIn("target_price", snapshot["runtime"]["latest_price"])
            self.assertEqual(snapshot["runtime"]["last_signal"]["side"], "NO_TRADE")
            self.assertIn("缺少官方目标价", snapshot["runtime"]["last_signal"]["reason"])
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

            snapshot = bot.ingest_live_snapshot(payload)

            self.assertEqual(snapshot["runtime"]["current_market"]["target_price"], 100.0)
            self.assertEqual(snapshot["runtime"]["latest_price"]["target_price"], 100.0)
            self.assertEqual(snapshot["runtime"]["latest_price"]["target_price_source"], "market.target_price")
            self.assertFalse(snapshot["runtime"]["latest_price"]["target_price_fallback"])

    def test_pair_strategy_does_not_open_without_official_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp) / "test.sqlite3", max_open_trades=2, stake_dollars=5.0)
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
                    "Up": {"best_bid": 0.39, "best_ask": 0.4, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "ask_size": 100, "updated_at_ms": int(now * 1000)},
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
                    "Up": {"best_bid": 0.39, "best_ask": 0.4, "ask_size": 100, "updated_at_ms": int(now * 1000)},
                    "Down": {"best_bid": 0.49, "best_ask": 0.5, "ask_size": 100, "updated_at_ms": int(now * 1000)},
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
                "SINGLE + FAK CHAINLINK_ONLY",
                "SINGLE + FAK CHAINLINK_ONLY ANTI_BOT_GUARD",
                "SINGLE + FAK FALLBACK_ONLY",
                "SINGLE + FAK MULTI_CONFIRM",
                "SINGLE + FAK MULTI_LEAD",
                "SINGLE + FAK STRICT",
                "SINGLE + FAK REVERSAL",
                "SINGLE + FAK STOP_AND_FLIP",
                "SINGLE + GTC",
                "SINGLE + GTD",
                "SINGLE + POST_ONLY",
                "PAIR + FAK",
                "PAIR + FAK MULTI_CONFIRM",
                "PAIR + FAK MULTI_LEAD",
                "PAIR + GTC",
                "PAIR + GTD",
                "PAIR + POST_ONLY",
            ],
        )

    def test_pair_strategy_gtd_places_two_resting_pair_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "test.sqlite3",
                max_open_trades=2,
                stake_dollars=5.0,
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
            self.assertLessEqual(active_orders[0]["expires_at"], now + 61.0)
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
            self.assertEqual(len(variants), 18)
            self.assertEqual(snapshot["run_count"], 1)
            self.assertIn("profit_summary", snapshot)
            self.assertEqual(snapshot["profit_summary"]["status"], "WAITING_FOR_SAMPLE")
            self.assertIsNone(snapshot["profit_summary"]["winner_variant_id"])
            self.assertFalse(snapshot["decision_summary"]["comparison_ready"])
            self.assertEqual(snapshot["decision_summary"]["status"], "WAITING_FOR_SAMPLE")
            self.assertEqual(snapshot["decision_summary"]["ready_count"], 0)
            self.assertEqual(snapshot["decision_summary"]["total_count"], 18)
            self.assertIsNone(snapshot["decision_summary"]["recommended_variant_id"])
            self.assertIsNotNone(snapshot["decision_summary"]["current_leader_variant_id"])
            self.assertTrue(all(row["last_error"] is None for row in variants.values()))
            self.assertIn("review_score", variants["SINGLE_FAK"])
            self.assertIn("score", variants["SINGLE_FAK"]["review_score"])
            self.assertEqual(variants["SINGLE_FAK"]["review_score"]["sample_status"], "INSUFFICIENT")
            self.assertIn("结算样本不足", variants["SINGLE_FAK"]["review_score"]["reasons"][0])
            self.assertEqual(variants["SINGLE_FAK"]["metrics"]["open_trades"], 1)
            self.assertEqual(variants["SINGLE_FAK_STRICT"]["single_entry_mode"], "STRICT")
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
            self.assertEqual(variants["SINGLE_FAK_MULTI_CONFIRM"]["market_data_mode"], "MULTI_CONFIRM")
            self.assertEqual(variants["SINGLE_FAK_MULTI_LEAD"]["market_data_mode"], "MULTI_LEAD")
            self.assertEqual(variants["PAIR_FAK"]["metrics"]["open_trades"], 2)
            self.assertEqual(variants["PAIR_GTD"]["active_orders"], 2)
            self.assertEqual(variants["PAIR_POST_ONLY"]["active_orders"], 2)
            self.assertEqual(variants["PAIR_FAK"]["order_summary"]["filled_count"], 2)
            self.assertEqual(variants["PAIR_FAK"]["order_summary"]["fill_rate"], 100.0)
            self.assertEqual(variants["PAIR_GTD"]["order_summary"]["active_count"], 2)
            self.assertEqual(variants["PAIR_GTD"]["order_summary"]["fill_rate"], 0.0)
            self.assertNotEqual(variants["SINGLE_FAK"]["db_path"], variants["PAIR_FAK"]["db_path"])

            detail = bot.strategy_experiment_detail("PAIR_GTD", trade_limit=5, order_limit=5)
            self.assertEqual(detail["variant"]["variant_id"], "PAIR_GTD")
            self.assertEqual(detail["variant"]["order_summary"]["total_count"], 2)
            self.assertEqual(detail["recent_orders_page"]["recent_orders_meta"]["total"], 2)
            self.assertEqual(detail["recent_trades_page"]["recent_trades_meta"]["total"], 0)

            retrospective = bot.strategy_experiments_retrospective()
            self.assertTrue(retrospective["enabled"])
            self.assertEqual(len(retrospective["variants"]), 18)
            self.assertEqual(len(retrospective["profit_summary"]["rankings"]), 18)
            self.assertEqual(retrospective["window"], {"start_at": None, "end_at": None})

            tables = bot.strategy_experiments_tables(trade_limit=20, order_limit=20)
            self.assertTrue(tables["enabled"])
            self.assertGreaterEqual(len(tables["open_trades"]), 3)
            self.assertTrue(all("combo" in row for row in tables["open_trades"]))
            self.assertTrue(any(row["variant_id"] == "SINGLE_FAK" for row in tables["open_trades"]))
            self.assertTrue(any(row["variant_id"] == "PAIR_FAK" for row in tables["open_trades"]))
            self.assertGreaterEqual(tables["recent_orders_meta"]["total"], 8)
            self.assertTrue(all("combo" in row for row in tables["recent_orders"]))
            self.assertGreaterEqual(tables["recent_trades_meta"]["total"], 3)
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
                "variant_id": "PAIR_POST_ONLY",
                "combo": "PAIR + POST_ONLY",
                "review_score": {"score": 82.0, "eligible_for_decision": True, "disqualified": False},
                "recent_trades_summary": {"settled_count": 40, "total_pnl": 12.0},
                "order_summary": {"total_count": 90, "fill_rate": 58.0},
            },
            {
                "variant_id": "PAIR_GTC",
                "combo": "PAIR + GTC",
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
        self.assertEqual(summary["recommended_variant_id"], "PAIR_POST_ONLY")
        self.assertEqual(summary["ready_count"], 1)
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(summary["disqualified_count"], 1)
        self.assertEqual(summary["disqualified_variants"][0]["variant_id"], "PAIR_GTC")

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
                "variant_id": "PAIR_POST_ONLY",
                "combo": "PAIR + POST_ONLY",
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
        self.assertEqual(ready["winner_variant_id"], "PAIR_POST_ONLY")
        self.assertTrue(ready["comparison_ready"])
        self.assertTrue(ready["profitable_winner_ready"])

        variants[1]["recent_trades_summary"]["total_pnl"] = -1.0
        no_profit = _experiment_profit_summary(variants)

        self.assertEqual(no_profit["status"], "NO_PROFIT")
        self.assertTrue(no_profit["comparison_ready"])
        self.assertFalse(no_profit["profitable_winner_ready"])
        self.assertEqual(no_profit["best_eligible_variant_id"], "PAIR_POST_ONLY")
        self.assertIsNone(no_profit["winner_variant_id"])

    def test_strategy_experiment_html_report_escapes_and_summarizes_variants(self) -> None:
        report = {
            "enabled": True,
            "db_dir": "data/strategy-experiments",
            "window": {"start_at": None, "end_at": None},
            "profit_summary": {
                "status_label": "等待盈利样本",
                "winner_combo": None,
                "current_profit_leader_combo": "PAIR + POST_ONLY",
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
                    "variant_id": "PAIR_POST_ONLY",
                    "combo": "PAIR + POST_ONLY",
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
        self.assertIn("PAIR + POST_ONLY", html)
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

    def test_polymarket_resolution_falls_back_to_page_prices(self) -> None:
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
        self.assertAlmostEqual(resolution["final_price"], 98.75, places=6)
        self.assertAlmostEqual(resolution["target_price"], 100.5, places=6)
        self.assertEqual(resolution["settlement_price_source"], "PolymarketPage:eventMetadata")

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

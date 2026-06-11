from __future__ import annotations

import inspect
import json
import hashlib
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, replace
from importlib import metadata
from pathlib import Path
from typing import Any

from .config import LIVE_CREDENTIAL_ENV_KEYS, Settings, env_file_status
from .execution import (
    ORDER_TYPE_FAK,
    STATUS_CANCELED,
    STATUS_FILLED,
    STATUS_PENDING,
    STATUS_REJECTED,
    build_taker_buy_fill_from_sweep,
    sweep_taker_buy_by_budget,
    taker_fee,
)
from .experiments import (
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
    SIGNAL_FILTER_MODE_NONE,
    SINGLE_ENTRY_MODE_LEGACY,
    STRATEGY_FAMILY_SINGLE,
    StrategyVariant,
)
from .models import MarketRound, PaperFill, PaperFillLevel, Signal, TradeIntent
from .polymarket import PolymarketClient
from .signal_filters import aggressive_edge_block_reason, aggressive_edge_pass_note, aggressive_edge_v2_risk_report
from .storage import (
    SETTLEMENT_SOURCE_POLYMARKET,
    TradeStore,
    normalize_paper_order_status_filter,
)
from .strategy import (
    MULTI_SOURCE_MAX_AGE_MS,
    MULTI_SOURCE_MIN_SAMPLES,
    RealBtcFiveMinuteStrategy,
    input_from_snapshot,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - production target is Linux, this keeps imports explicit.
    fcntl = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)
LIVE_VARIANT_ID = "SINGLE_FAK_REAL"
LIVE_COMBO = "SINGLE + FAK REAL"
LIVE_ENTRY_MARKER = "SINGLE_FAK_REAL"
LIVE_STOP_WIN_VARIANT_ID = "SINGLE_FAK_REAL_STOP_WIN"
LIVE_STOP_WIN_COMBO = "SINGLE + FAK REAL STOP WIN"
LIVE_STOP_WIN_ENTRY_MARKER = "SINGLE_FAK_REAL_STOP_WIN"
LIVE_STOP_WIN_MARKER = "LIVE_STOP_WIN"
LIVE_AGGRESSIVE_EDGE_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_REAL"
LIVE_AGGRESSIVE_EDGE_COMBO = "SINGLE + FAK Aggressive Edge REAL"
LIVE_AGGRESSIVE_EDGE_ENTRY_MARKER = "SINGLE_FAK_AGGRESSIVE_EDGE_REAL"
LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_V10_REAL"
LIVE_AGGRESSIVE_EDGE_V10_COMBO = "SINGLE + FAK Aggressive Edge V10 REAL"
LIVE_AGGRESSIVE_EDGE_V10_ENTRY_MARKER = "SINGLE_FAK_AGGRESSIVE_EDGE_V10_REAL"
LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL"
LIVE_AGGRESSIVE_EDGE_V11_COMBO = "SINGLE + FAK Aggressive Edge V11 REAL"
LIVE_AGGRESSIVE_EDGE_V11_ENTRY_MARKER = "SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL"
LIVE_AGGRESSIVE_EDGE_V11_1_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_V11_1_REAL"
LIVE_AGGRESSIVE_EDGE_V11_1_COMBO = "SINGLE + FAK Aggressive Edge V11.1 REAL"
LIVE_AGGRESSIVE_EDGE_V11_1_ENTRY_MARKER = "SINGLE_FAK_AGGRESSIVE_EDGE_V11_1_REAL"
LIVE_PAPER_VARIANT_ID = "SINGLE_FAK_REAL_PAPER"
LIVE_PAPER_COMBO = "SINGLE + FAK REAL PAPER"
LIVE_PAPER_ENTRY_MARKER = "SINGLE_FAK_REAL_PAPER"
LIVE_PAPER_STOP_WIN_VARIANT_ID = "SINGLE_FAK_REAL_PAPER_STOP_WIN"
LIVE_PAPER_STOP_WIN_COMBO = "SINGLE + FAK REAL PAPER STOP WIN"
LIVE_PAPER_STOP_WIN_ENTRY_MARKER = "SINGLE_FAK_REAL_PAPER_STOP_WIN"
LIVE_PAPER_STOP_WIN_MARKER = "PAPER_STOP_WIN"
LIVE_PAPER_STOP_WIN_DEFAULT_TAKE_PROFIT_PCT = 8.0
LIVE_PAPER_STOP_WIN_DISABLE_PCT = 100.0
LIVE_PAPER_STOP_WIN_MIN_HOLD_SECONDS = 10.0
LIVE_PAPER_STOP_WIN_CHECK_SECONDS = 10.0
LIVE_PAPER_STOP_WIN_FINAL_WINDOW_SECONDS = 60.0
LIVE_PAPER_STOP_WIN_FINAL_CHECK_SECONDS = 3.0
LIVE_MANUAL_SELL_MARKER = "LIVE_MANUAL_SELL"
LIVE_OFFICIAL_RECHECK_INTERVAL_SECONDS = 10.0
LIVE_OFFICIAL_RECHECK_WINDOW_SECONDS = 24 * 60 * 60
LIVE_OFFICIAL_RECHECK_LIMIT = 5
LIVE_PRICE_BACKFILL_INTERVAL_SECONDS = 60.0
LIVE_PRICE_BACKFILL_WINDOW_SECONDS = 24 * 60 * 60
LIVE_PRICE_BACKFILL_LIMIT = 3
LIVE_ORDER_RECONCILE_INTERVAL_SECONDS = 5.0
LIVE_ORDER_RECONCILE_LIMIT = 10
LIVE_PENDING_ORDER_MAX_AGE_SECONDS = 120.0
LIVE_MIN_USDC = 0.1
LIVE_EPSILON = 0.000001
LIVE_STARTUP_REARM_MESSAGE = "服务启动后实盘开关已自动关闭，需要人工重新预检并开启"
LIVE_ACTIVE_LOCK_PRESERVE_MESSAGE = "检测到已有实盘进程持锁，未改写磁盘实盘开关"
LIVE_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
LIVE_FALLBACK_SOURCE_CHAINLINK = "chainlink"
LIVE_FALLBACK_SOURCE_OKX = "okx"
LIVE_FALLBACK_SOURCE_BINANCE = "binance"
LIVE_FALLBACK_SOURCE_ORDER = (
    LIVE_FALLBACK_SOURCE_CHAINLINK,
    LIVE_FALLBACK_SOURCE_OKX,
    LIVE_FALLBACK_SOURCE_BINANCE,
)
LIVE_BASIS_FALLBACK_SOURCES = (LIVE_FALLBACK_SOURCE_OKX, LIVE_FALLBACK_SOURCE_BINANCE)
LIVE_AGGRESSIVE_EDGE_V8_ALLOWED_BUCKETS = frozenset({0, 1, 2, 3, 4})
LIVE_AGGRESSIVE_EDGE_V8_MAX_RISK_SCORE = 0.90
LIVE_AGGRESSIVE_EDGE_V8_MAX_ABS_MOVE_BPS = 20.0
LIVE_AGGRESSIVE_EDGE_V9_BLOCKED_BUCKETS = frozenset({1})
LIVE_AGGRESSIVE_EDGE_V10_UP_MIN_ABS_MOVE_BPS = 5.7
LIVE_AGGRESSIVE_EDGE_V10_UP_MIN_TOP_LEVEL_SKEW = 0.20
LIVE_AGGRESSIVE_EDGE_V11_ALLOWED_BUCKETS = frozenset({2, 3})
LIVE_AGGRESSIVE_EDGE_V11_MIN_ABS_MOVE_BPS = 5.5
LIVE_AGGRESSIVE_EDGE_V11_MIN_DEPTH_SKEW = 0.35
LIVE_AGGRESSIVE_EDGE_V11_MAX_RISK_SCORE = 0.25
LIVE_AGGRESSIVE_EDGE_V11_UP_MIN_TOP_LEVEL_SKEW = 0.20
LIVE_AGGRESSIVE_EDGE_V11_1_UP_MIN_TOP_LEVEL_SKEW = 0.35
LIVE_STRATEGY_OPTIONS = (
    {
        "variant_id": LIVE_VARIANT_ID,
        "combo": LIVE_COMBO,
        "entry_marker": LIVE_ENTRY_MARKER,
        "role": "实盘隔离账户，沿用 SINGLE_FAK LEGACY 反转双边逻辑",
        "stop_win_enabled": False,
        "signal_filter_mode": SIGNAL_FILTER_MODE_NONE,
    },
    {
        "variant_id": LIVE_STOP_WIN_VARIANT_ID,
        "combo": LIVE_STOP_WIN_COMBO,
        "entry_marker": LIVE_STOP_WIN_ENTRY_MARKER,
        "role": "实盘隔离账户，沿用 SINGLE_FAK 并在真实仓位上执行止盈卖出",
        "stop_win_enabled": True,
        "signal_filter_mode": SIGNAL_FILTER_MODE_NONE,
    },
    {
        "variant_id": LIVE_AGGRESSIVE_EDGE_VARIANT_ID,
        "combo": LIVE_AGGRESSIVE_EDGE_COMBO,
        "entry_marker": LIVE_AGGRESSIVE_EDGE_ENTRY_MARKER,
        "role": "实盘隔离账户，沿用 SINGLE_FAK Aggressive Edge 过滤逻辑",
        "stop_win_enabled": False,
        "signal_filter_mode": SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
    },
    {
        "variant_id": LIVE_AGGRESSIVE_EDGE_V10_VARIANT_ID,
        "combo": LIVE_AGGRESSIVE_EDGE_V10_COMBO,
        "entry_marker": LIVE_AGGRESSIVE_EDGE_V10_ENTRY_MARKER,
        "role": "实盘隔离账户，沿用 Aggressive Edge V10 诊断过滤；未过样本准入前只做预检准备",
        "stop_win_enabled": False,
        "signal_filter_mode": SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
    },
    {
        "variant_id": LIVE_AGGRESSIVE_EDGE_V11_VARIANT_ID,
        "combo": LIVE_AGGRESSIVE_EDGE_V11_COMBO,
        "entry_marker": LIVE_AGGRESSIVE_EDGE_V11_ENTRY_MARKER,
        "role": "实盘隔离账户，沿用 Aggressive Edge V11 深盘口强动量过滤；过样本准入后进入实盘预检",
        "stop_win_enabled": False,
        "signal_filter_mode": SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
    },
    {
        "variant_id": LIVE_AGGRESSIVE_EDGE_V11_1_VARIANT_ID,
        "combo": LIVE_AGGRESSIVE_EDGE_V11_1_COMBO,
        "entry_marker": LIVE_AGGRESSIVE_EDGE_V11_1_ENTRY_MARKER,
        "role": "实盘隔离账户，基于 V11 小幅收紧 Up 顶层盘口偏斜；用于和 V11 做独立实盘对照",
        "stop_win_enabled": False,
        "signal_filter_mode": SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
    },
)


@dataclass(frozen=True)
class LiveRuntimeConfig:
    """实盘运行配置；enabled 为 true 时才允许真实买入和手动卖出。"""

    enabled: bool
    live_strategy_id: str
    initial_balance: float
    stake_dollars: float
    max_open_trades: int
    max_daily_loss: float
    max_total_drawdown: float
    max_entry_price: float
    fallback_sources: tuple[str, ...]
    retry_count: int
    retry_delay_ms: int
    paper_stop_win_take_profit_pct: float
    compliance_acknowledged: bool
    updated_at: float

    def normalized(self) -> "LiveRuntimeConfig":
        return LiveRuntimeConfig(
            enabled=bool(self.enabled),
            live_strategy_id=_normalize_live_strategy_id(self.live_strategy_id),
            initial_balance=max(1.0, round(float(self.initial_balance), 2)),
            stake_dollars=max(LIVE_MIN_USDC, round(float(self.stake_dollars), 2)),
            max_open_trades=max(1, int(self.max_open_trades)),
            max_daily_loss=max(0.0, round(float(self.max_daily_loss), 2)),
            max_total_drawdown=max(0.0, round(float(self.max_total_drawdown), 2)),
            max_entry_price=max(0.01, min(0.99, round(float(self.max_entry_price), 4))),
            fallback_sources=_normalize_live_fallback_sources(self.fallback_sources),
            retry_count=max(0, min(10, int(self.retry_count))),
            retry_delay_ms=max(0, min(10_000, int(self.retry_delay_ms))),
            paper_stop_win_take_profit_pct=max(
                0.0,
                min(
                    LIVE_PAPER_STOP_WIN_DISABLE_PCT,
                    round(float(self.paper_stop_win_take_profit_pct), 2),
                ),
            ),
            compliance_acknowledged=bool(self.compliance_acknowledged),
            updated_at=float(self.updated_at or time.time()),
        )


class LiveSettingsStore:
    def __init__(self, path: Path, defaults: LiveRuntimeConfig) -> None:
        self.path = path
        self.defaults = defaults.normalized()

    def load(self) -> LiveRuntimeConfig:
        if not self.path.exists():
            return self.defaults
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.defaults
        if not isinstance(payload, dict):
            return self.defaults
        return replace(
            self.defaults,
            enabled=bool(payload.get("enabled", self.defaults.enabled)),
            live_strategy_id=_normalize_live_strategy_id(
                payload.get("live_strategy_id", self.defaults.live_strategy_id)
            ),
            initial_balance=_float(payload.get("initial_balance"), self.defaults.initial_balance),
            stake_dollars=_float(payload.get("stake_dollars"), self.defaults.stake_dollars),
            max_open_trades=_int(payload.get("max_open_trades"), self.defaults.max_open_trades),
            max_daily_loss=_float(payload.get("max_daily_loss"), self.defaults.max_daily_loss),
            max_total_drawdown=_float(payload.get("max_total_drawdown"), self.defaults.max_total_drawdown),
            max_entry_price=_float(payload.get("max_entry_price"), self.defaults.max_entry_price),
            fallback_sources=_normalize_live_fallback_sources(
                payload.get("fallback_sources", self.defaults.fallback_sources)
            ),
            retry_count=_int(payload.get("retry_count"), self.defaults.retry_count),
            retry_delay_ms=_int(payload.get("retry_delay_ms"), self.defaults.retry_delay_ms),
            paper_stop_win_take_profit_pct=_float(
                payload.get("paper_stop_win_take_profit_pct"),
                self.defaults.paper_stop_win_take_profit_pct,
            ),
            compliance_acknowledged=bool(
                payload.get("compliance_acknowledged", self.defaults.compliance_acknowledged)
            ),
            updated_at=_float(payload.get("updated_at"), self.defaults.updated_at),
        ).normalized()

    def save(self, config: LiveRuntimeConfig) -> LiveRuntimeConfig:
        next_config = replace(config, updated_at=time.time()).normalized()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(asdict(next_config), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)
        return next_config

    def file_status(self, runtime_config: LiveRuntimeConfig) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(self.path),
            "exists": self.path.exists(),
            "runtime_enabled": bool(runtime_config.enabled),
            "runtime_updated_at": runtime_config.updated_at,
            "enabled": None,
            "updated_at": None,
            "enabled_matches_runtime": None,
            "error": None,
        }
        if not payload["exists"]:
            return payload
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload["error"] = f"{type(exc).__name__}: {exc}"
            return payload
        if not isinstance(raw, dict):
            payload["error"] = "settings file payload is not an object"
            return payload
        enabled = bool(raw.get("enabled", self.defaults.enabled))
        payload["enabled"] = enabled
        payload["updated_at"] = _float(raw.get("updated_at"), 0.0)
        payload["enabled_matches_runtime"] = enabled == bool(runtime_config.enabled)
        return payload


class LiveProcessLock:
    """进程级实盘锁；同一 live settings path 同时只允许一个 runner 持有。"""

    _registry_lock = threading.Lock()
    _held_paths: set[str] = set()

    def __init__(self, path: Path) -> None:
        self.path = path
        self._path_key = str(path.expanduser().resolve(strict=False))
        self._handle: Any | None = None

    @property
    def locked(self) -> bool:
        return self._handle is not None

    def acquire(self) -> str | None:
        if self.locked:
            return None
        if fcntl is None:
            return "当前运行环境不支持实盘进程锁，停止开启实盘"
        with self._registry_lock:
            if self._path_key in self._held_paths:
                return f"同一进程内已有 runner 持有实盘进程锁 {self.path}，实盘开关保持关闭"
            self._held_paths.add(self._path_key)
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "locked_at": time.time()}, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
            self._handle = handle
            return None
        except BlockingIOError:
            if handle is not None:
                handle.close()
            self._forget_path()
            return f"另一个 polybot2other 进程已持有实盘进程锁 {self.path}，实盘开关保持关闭"
        except OSError as exc:
            if handle is not None:
                handle.close()
            self._forget_path()
            return f"无法获取实盘进程锁 {self.path}: {exc}，实盘开关保持关闭"

    def active_holder_exists(self) -> bool:
        if self.locked:
            return True
        with self._registry_lock:
            if self._path_key in self._held_paths:
                return True
        if fcntl is None or not self.path.exists():
            return False
        handle = None
        try:
            handle = self.path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            if handle is not None:
                handle.close()

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self._forget_path()

    def payload(self) -> dict[str, Any]:
        return {"path": str(self.path), "acquired": self.locked}

    def _forget_path(self) -> None:
        with self._registry_lock:
            self._held_paths.discard(self._path_key)

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


@dataclass(frozen=True)
class LiveOrderResponse:
    success: bool
    status: str
    order_id: str | None
    error: str | None
    raw: dict[str, Any]
    filled_shares: float | None = None
    cash_spent: float | None = None
    avg_fill_price: float | None = None


class PolymarketLiveClient:
    """官方 CLOB Python SDK 适配层；没有密钥或 SDK 时只暴露 readiness 错误。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.host = settings.clob_url.rstrip("/")
        self.chain_id = int(settings.live_trading_chain_id)
        self._client: Any | None = None
        self._client_credential_fingerprint: str | None = None
        self._sdk_error: str | None = None
        self._wallet_cache: tuple[float, dict[str, Any]] | None = None
        self._token_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._open_orders_cache: tuple[float, dict[str, Any]] | None = None
        self._geoblock_cache: tuple[float, dict[str, Any]] | None = None

    def readiness(
        self,
        required_cash: float | None = None,
        retry_count: int | None = None,
        retry_delay_ms: int | None = None,
    ) -> dict[str, Any]:
        errors = self.readiness_errors()
        geo_check = self.geoblock_state()
        geo_error = _geoblock_block_reason(geo_check)
        if geo_error:
            errors.append(geo_error)
        wallet = None
        if not errors:
            wallet = self.wallet_state(
                required_cash=required_cash or LIVE_MIN_USDC,
                retry_count=retry_count,
                retry_delay_ms=retry_delay_ms,
            )
            errors.extend(wallet.get("errors") or [])
        return {
            "ready": not errors,
            "errors": errors,
            "host": self.host,
            "chain_id": self.chain_id,
            "sdk": "py_clob_client_v2",
            "sdk_version": _package_version("py_clob_client_v2"),
            "sdk_status": self.sdk_status(),
            "credential_env": [
                "POLYBOT2OTHER_LIVE_PRIVATE_KEY",
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE",
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS",
                "POLYBOT2OTHER_LIVE_API_KEY",
                "POLYBOT2OTHER_LIVE_API_SECRET",
                "POLYBOT2OTHER_LIVE_API_PASSPHRASE",
            ],
            "credential_presence": self._credential_presence(),
            "credential_mode": self._credential_mode(),
            "credential_addresses": self._credential_address_summary(),
            "env_files": env_file_status(),
            "geo_check": geo_check,
            "wallet": wallet,
            "install_hint": "rtk proxy python3 -m pip install -e .",
        }

    def cached_readiness(self, required_cash: float | None = None) -> dict[str, Any]:
        errors = self.readiness_errors()
        geo_check = self._cached_geoblock_state()
        geo_error = _geoblock_block_reason(geo_check)
        if geo_error:
            errors.append(geo_error)
        wallet = None
        if not errors:
            wallet = self._cached_wallet_state(required_cash=required_cash or LIVE_MIN_USDC)
            errors.extend(wallet.get("errors") or [])
        return {
            "ready": not errors,
            "errors": errors,
            "host": self.host,
            "chain_id": self.chain_id,
            "sdk": "py_clob_client_v2",
            "sdk_version": _package_version("py_clob_client_v2"),
            "sdk_status": self.sdk_status(),
            "credential_env": [
                "POLYBOT2OTHER_LIVE_PRIVATE_KEY",
                "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE",
                "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS",
                "POLYBOT2OTHER_LIVE_API_KEY",
                "POLYBOT2OTHER_LIVE_API_SECRET",
                "POLYBOT2OTHER_LIVE_API_PASSPHRASE",
            ],
            "credential_presence": self._credential_presence(),
            "credential_mode": self._credential_mode(),
            "credential_addresses": self._credential_address_summary(),
            "env_files": env_file_status(),
            "geo_check": geo_check,
            "wallet": wallet,
            "cached": True,
            "install_hint": "rtk proxy python3 -m pip install -e .",
        }

    def _cached_wallet_state(self, *, required_cash: float | None = None) -> dict[str, Any]:
        if self._wallet_cache:
            checked_at, payload = self._wallet_cache
            cached = _wallet_state_with_requirement(dict(payload), required_cash)
            cached["cached"] = True
            cached["stale"] = time.time() - checked_at > 30.0
            return cached
        return {
            "checked_at": None,
            "asset_type": "COLLATERAL",
            "balance": 0.0,
            "allowance": 0.0,
            "raw": {},
            "errors": ["wallet cache empty; async refresh pending"],
            "ready": False,
            "required_cash": required_cash,
            "cached": True,
            "cache_missing": True,
        }

    def _cached_geoblock_state(self) -> dict[str, Any]:
        if self._geoblock_cache:
            checked_at, payload = self._geoblock_cache
            cached = dict(payload)
            cached["cached"] = True
            cached["stale"] = time.time() - checked_at > 300.0
            return cached
        return {
            "ready": False,
            "blocked": None,
            "country": None,
            "region": None,
            "checked_at": None,
            "errors": ["geoblock cache empty; async refresh pending"],
            "source": LIVE_GEOBLOCK_URL,
            "cached": True,
            "cache_missing": True,
        }

    def sdk_status(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "package": "py_clob_client_v2",
            "version": _package_version("py_clob_client_v2"),
            "compatible": False,
            "errors": [],
        }
        try:
            sdk = self._sdk()
            errors = self._sdk_compatibility_errors(sdk)
        except Exception as exc:  # noqa: BLE001 - readiness 要把 SDK 导入错误返回给前端。
            errors = [f"py_clob_client_v2 不可用: {type(exc).__name__}: {exc}"]
        payload["errors"] = errors
        payload["compatible"] = not errors
        return payload

    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        errors.extend(_env_file_permission_errors())
        private_key = _env("POLYBOT2OTHER_LIVE_PRIVATE_KEY")
        funder = _env("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS")
        if not private_key:
            errors.append("缺少 POLYBOT2OTHER_LIVE_PRIVATE_KEY，实盘下单未就绪")
        elif not _is_private_key_like(private_key):
            errors.append("POLYBOT2OTHER_LIVE_PRIVATE_KEY 格式非法，必须是 32 字节 hex 私钥")
        signature_type = _int_or_none(_env("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE"))
        if signature_type is None or signature_type not in {0, 1, 2, 3}:
            errors.append("缺少或非法 POLYBOT2OTHER_LIVE_SIGNATURE_TYPE，必须是 0/1/2/3")
        if not funder:
            errors.append("缺少 POLYBOT2OTHER_LIVE_FUNDER_ADDRESS，无法确认真实资金钱包")
        elif not _is_address_like(funder):
            errors.append("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS 格式非法，必须是 20 字节 0x 地址")
        api_parts = [
            bool(_env("POLYBOT2OTHER_LIVE_API_KEY")),
            bool(_env("POLYBOT2OTHER_LIVE_API_SECRET")),
            bool(_env("POLYBOT2OTHER_LIVE_API_PASSPHRASE")),
        ]
        if any(api_parts) and not all(api_parts):
            errors.append("POLYBOT2OTHER_LIVE_API_KEY/SECRET/PASSPHRASE 必须同时配置，或全部留空由 SDK 派生")
        if self._sdk_error:
            errors.append(self._sdk_error)
        else:
            sdk: dict[str, Any] | None = None
            sdk_compatibility_errors: list[str] = []
            try:
                sdk = self._sdk()
                sdk_compatibility_errors = self._sdk_compatibility_errors(sdk)
                errors.extend(sdk_compatibility_errors)
            except Exception as exc:  # noqa: BLE001 - readiness 要把 SDK 缺失暴露给前端。
                errors.append(f"py_clob_client_v2 不可用: {exc}；安装依赖后再开启实盘")
            if (
                sdk is not None
                and not sdk_compatibility_errors
                and private_key
                and _is_private_key_like(private_key)
                and signature_type == 0
                and funder
                and _is_address_like(funder)
            ):
                try:
                    signer_address = self._signer_address_from_private_key(sdk, private_key)
                except Exception as exc:  # noqa: BLE001 - EOA 模式必须确认 signer 和 funder 一致。
                    errors.append(f"无法从实盘私钥推导 signer address: {type(exc).__name__}: {exc}")
                else:
                    if signer_address.lower() != funder.lower():
                        errors.append(
                            "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=0 时 "
                            "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS 必须等于私钥 signer address"
                        )
        return errors

    def place_market_buy(
        self,
        *,
        token_id: str,
        amount: float,
        max_price: float,
        tick_size: str | None = None,
        neg_risk: bool | None = None,
        retry_count: int,
        retry_delay_ms: int,
    ) -> LiveOrderResponse:
        return self._post_market_order(
            token_id,
            amount,
            "BUY",
            max_price,
            tick_size,
            neg_risk,
            retry_count,
            retry_delay_ms,
        )

    def sign_market_order_preview(
        self,
        *,
        token_id: str,
        amount: float,
        side: str,
        price: float,
        tick_size: str | None = None,
        neg_risk: bool | None = None,
        retry_count: int,
        retry_delay_ms: int,
    ) -> dict[str, Any]:
        normalized_side = "BUY" if str(side or "").upper() == "BUY" else "SELL"
        try:
            sdk = self._sdk()
            client = self._authenticated_client(sdk)
            order_args, options = self._market_order_args(
                sdk,
                token_id=token_id,
                amount=amount,
                side=normalized_side,
                price=price,
                tick_size=tick_size,
                neg_risk=neg_risk,
                user_usdc_balance=amount if normalized_side == "BUY" else None,
            )
            signed_order_response = self._create_signed_market_order_with_retry(
                client,
                order_args,
                options,
                retry_count,
                retry_delay_ms,
            )
            if isinstance(signed_order_response, LiveOrderResponse):
                return {
                    "ready": False,
                    "status": signed_order_response.status,
                    "errors": [signed_order_response.error or signed_order_response.status],
                    "submitted_to_clob": False,
                    "raw": signed_order_response.raw,
                }
            return {
                "ready": True,
                "status": "SIGNED",
                "errors": [],
                "submitted_to_clob": False,
                "signed_order_hash": _signed_order_hash(client, signed_order_response),
                "side": normalized_side,
                "token_id": str(token_id),
                "amount": round(float(amount), 6),
                "price": max(0.01, min(0.99, round(float(price), 4))),
                "tick_size": _normalize_tick_size(tick_size),
                "neg_risk": neg_risk if isinstance(neg_risk, bool) else None,
                "user_usdc_balance": round(float(amount), 6) if normalized_side == "BUY" else None,
            }
        except Exception as exc:  # noqa: BLE001 - 预检需要把 SDK 签名失败原因返回给前端。
            return {
                "ready": False,
                "status": "SIGN_ERROR",
                "errors": [f"签名 FAK 订单失败: {type(exc).__name__}: {exc}"],
                "submitted_to_clob": False,
            }

    def wallet_state(
        self,
        *,
        required_cash: float | None = None,
        max_age_seconds: float = 30.0,
        force: bool = False,
        retry_count: int | None = None,
        retry_delay_ms: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_credential_cache_current()
        now = time.time()
        if not force and self._wallet_cache and now - self._wallet_cache[0] <= max_age_seconds:
            cached = dict(self._wallet_cache[1])
            return _wallet_state_with_requirement(cached, required_cash)
        raw: dict[str, Any] | None = None
        errors: list[str] = []
        balance = 0.0
        allowance = 0.0
        try:
            sdk = self._sdk()
            client = self._authenticated_client(sdk)
            params = self._balance_allowance_params(sdk, sdk["AssetType"].COLLATERAL)
            sync_raw, sync_retry_reasons = self._call_sdk_with_retry(
                lambda: client.update_balance_allowance(params),
                retry_count,
                retry_delay_ms,
            )
            raw_value, read_retry_reasons = self._call_sdk_with_retry(
                lambda: client.get_balance_allowance(params),
                retry_count,
                retry_delay_ms,
            )
            raw = raw_value if isinstance(raw_value, dict) else {"raw": raw_value}
            if sync_raw is not None:
                raw["sync_response"] = sync_raw
            if sync_retry_reasons:
                raw["sync_retry_reasons"] = sync_retry_reasons
            if read_retry_reasons:
                raw["read_retry_reasons"] = read_retry_reasons
            balance = _fixed_math_amount(raw.get("balance")) or 0.0
            allowance = _allowance_amount(raw) or 0.0
        except Exception as exc:  # noqa: BLE001 - readiness 要保留第三方错误上下文。
            errors.append(f"同步/读取 Polymarket collateral balance/allowance 失败: {type(exc).__name__}: {exc}")
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                raw = {"retry_reasons": retry_reasons}
        payload = {
            "checked_at": now,
            "asset_type": "COLLATERAL",
            "balance": round(balance, 6),
            "allowance": round(allowance, 6),
            "raw": raw or {},
            "errors": errors,
        }
        self._wallet_cache = (now, payload)
        return _wallet_state_with_requirement(payload, required_cash)

    def token_state(
        self,
        *,
        token_id: str,
        required_shares: float | None = None,
        max_age_seconds: float = 10.0,
        force: bool = False,
        retry_count: int | None = None,
        retry_delay_ms: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_credential_cache_current()
        token = str(token_id or "").strip()
        if not token:
            return _token_state_with_requirement(
                {
                    "checked_at": time.time(),
                    "asset_type": "CONDITIONAL",
                    "token_id": "",
                    "balance": 0.0,
                    "allowance": 0.0,
                    "raw": {},
                    "errors": ["缺少 Polymarket 条件代币 token_id，无法预检卖出授权"],
                },
                required_shares,
            )
        now = time.time()
        cached_item = self._token_cache.get(token)
        if not force and cached_item and now - cached_item[0] <= max_age_seconds:
            return _token_state_with_requirement(dict(cached_item[1]), required_shares)
        raw: dict[str, Any] | None = None
        errors: list[str] = []
        balance = 0.0
        allowance = 0.0
        try:
            sdk = self._sdk()
            client = self._authenticated_client(sdk)
            params = self._balance_allowance_params(sdk, sdk["AssetType"].CONDITIONAL, token_id=token)
            sync_raw, sync_retry_reasons = self._call_sdk_with_retry(
                lambda: client.update_balance_allowance(params),
                retry_count,
                retry_delay_ms,
            )
            raw_value, read_retry_reasons = self._call_sdk_with_retry(
                lambda: client.get_balance_allowance(params),
                retry_count,
                retry_delay_ms,
            )
            raw = raw_value if isinstance(raw_value, dict) else {"raw": raw_value}
            if sync_raw is not None:
                raw["sync_response"] = sync_raw
            if sync_retry_reasons:
                raw["sync_retry_reasons"] = sync_retry_reasons
            if read_retry_reasons:
                raw["read_retry_reasons"] = read_retry_reasons
            balance = _fixed_math_amount(raw.get("balance")) or 0.0
            allowance = _allowance_amount(raw) or 0.0
        except Exception as exc:  # noqa: BLE001
            errors.append(f"同步/读取 Polymarket conditional token balance/allowance 失败: {type(exc).__name__}: {exc}")
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                raw = {"retry_reasons": retry_reasons}
        payload = {
            "checked_at": now,
            "asset_type": "CONDITIONAL",
            "token_id": token,
            "balance": round(balance, 6),
            "allowance": round(allowance, 6),
            "raw": raw or {},
            "errors": errors,
        }
        self._token_cache[token] = (now, payload)
        return _token_state_with_requirement(payload, required_shares)

    def place_market_sell(
        self,
        *,
        token_id: str,
        shares: float,
        min_price: float,
        tick_size: str | None = None,
        neg_risk: bool | None = None,
        retry_count: int,
        retry_delay_ms: int,
    ) -> LiveOrderResponse:
        return self._post_market_order(
            token_id,
            shares,
            "SELL",
            min_price,
            tick_size,
            neg_risk,
            retry_count,
            retry_delay_ms,
        )

    def fetch_order_state(
        self,
        *,
        order_id: str,
        side: str,
        token_id: str | None = None,
        condition_id: str | None = None,
        retry_count: int | None = None,
        retry_delay_ms: int | None = None,
    ) -> LiveOrderResponse | None:
        sdk = self._sdk()
        client = self._authenticated_client(sdk)
        payload: dict[str, Any] = {"order_id": order_id}
        order_payload: dict[str, Any] | None = None
        try:
            raw_order, order_retry_reasons = self._call_sdk_with_retry(
                lambda: client.get_order(order_id),
                retry_count,
                retry_delay_ms,
            )
            if isinstance(raw_order, dict):
                order_payload = raw_order
            payload["order"] = raw_order
            if order_retry_reasons:
                payload["order_retry_reasons"] = order_retry_reasons
        except Exception as exc:  # noqa: BLE001 - 回查失败要进入 raw_response，供后续排查。
            payload["order_error"] = f"{type(exc).__name__}: {exc}"
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                payload["order_retry_reasons"] = retry_reasons
        try:
            params = sdk["TradeParams"](
                market=condition_id or None,
                asset_id=token_id or None,
            )
            raw_trades, trades_retry_reasons = self._call_sdk_with_retry(
                lambda: client.get_trades(params, only_first_page=True),
                retry_count,
                retry_delay_ms,
            )
            trades = [row for row in raw_trades if isinstance(row, dict)]
            matched_trades = [row for row in trades if _trade_matches_order(row, order_id)]
            payload["trades"] = matched_trades
            if trades_retry_reasons:
                payload["trades_retry_reasons"] = trades_retry_reasons
            trade_fill = _matched_amounts_from_trades(matched_trades)
            if trade_fill:
                return LiveOrderResponse(
                    success=True,
                    status="TRADES_MATCHED",
                    order_id=order_id,
                    error=None,
                    raw=payload,
                    filled_shares=trade_fill["shares"],
                    cash_spent=trade_fill["cash"],
                    avg_fill_price=trade_fill["price"],
                )
        except Exception as exc:  # noqa: BLE001
            payload["trades_error"] = f"{type(exc).__name__}: {exc}"
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                payload["trades_retry_reasons"] = retry_reasons
        if order_payload is not None:
            order_response = _order_state_response(order_payload, side, payload)
            if order_response.order_id is None:
                return LiveOrderResponse(
                    order_response.success,
                    order_response.status,
                    order_id,
                    order_response.error,
                    order_response.raw,
                    order_response.filled_shares,
                    order_response.cash_spent,
                    order_response.avg_fill_price,
                )
            return order_response
        if payload.get("order_error") or payload.get("trades_error"):
            return LiveOrderResponse(
                success=False,
                status="RECONCILE_ERROR",
                order_id=order_id,
                error=str(payload.get("order_error") or payload.get("trades_error")),
                raw=payload,
            )
        return None

    def cancel_all_orders(self, *, retry_count: int, retry_delay_ms: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ready": False,
            "errors": [],
            "open_orders_before": None,
            "open_orders_after": None,
            "cancel_response": None,
        }
        try:
            sdk = self._sdk()
            client = self._authenticated_client(sdk)
            open_orders_before, before_retry_reasons = self._call_sdk_with_retry(
                lambda: client.get_open_orders(only_first_page=True),
                retry_count,
                retry_delay_ms,
            )
            payload["open_orders_before"] = open_orders_before if isinstance(open_orders_before, list) else []
            if before_retry_reasons:
                payload["open_orders_before_retry_reasons"] = before_retry_reasons
        except Exception as exc:  # noqa: BLE001 - 即使读取 open orders 失败，也继续尝试 cancel_all。
            payload["open_orders_before_error"] = f"{type(exc).__name__}: {exc}"
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                payload["open_orders_before_retry_reasons"] = retry_reasons
            try:
                sdk = self._sdk()
                client = self._authenticated_client(sdk)
            except Exception as auth_exc:  # noqa: BLE001
                payload["errors"].append(f"实盘急停认证失败: {type(auth_exc).__name__}: {auth_exc}")
                return payload
        try:
            cancel_response, cancel_retry_reasons = self._call_sdk_with_retry(
                client.cancel_all,
                retry_count,
                retry_delay_ms,
            )
            payload["cancel_response"] = cancel_response if isinstance(cancel_response, dict) else {"raw": cancel_response}
            if cancel_retry_reasons:
                payload["cancel_retry_reasons"] = cancel_retry_reasons
            payload["ready"] = True
        except Exception as exc:  # noqa: BLE001
            payload["errors"].append(f"官方 cancel_all 失败: {type(exc).__name__}: {exc}")
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                payload["cancel_retry_reasons"] = retry_reasons
            return payload
        try:
            open_orders_after, after_retry_reasons = self._call_sdk_with_retry(
                lambda: client.get_open_orders(only_first_page=True),
                retry_count,
                retry_delay_ms,
            )
            payload["open_orders_after"] = open_orders_after if isinstance(open_orders_after, list) else []
            if after_retry_reasons:
                payload["open_orders_after_retry_reasons"] = after_retry_reasons
        except Exception as exc:  # noqa: BLE001 - cancel_all 已发出，后验失败不覆盖急停提交结果。
            payload["open_orders_after_error"] = f"{type(exc).__name__}: {exc}"
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                payload["open_orders_after_retry_reasons"] = retry_reasons
        return payload

    def open_orders_state(
        self,
        *,
        max_age_seconds: float = 10.0,
        force: bool = False,
        retry_count: int | None = None,
        retry_delay_ms: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_credential_cache_current()
        now = time.time()
        if not force and self._open_orders_cache and now - self._open_orders_cache[0] <= max_age_seconds:
            return dict(self._open_orders_cache[1])
        payload: dict[str, Any] = {
            "ready": False,
            "skipped": False,
            "errors": [],
            "orders": [],
            "count": 0,
            "checked_at": now,
        }
        credential_errors = self.readiness_errors()
        if credential_errors:
            payload["skipped"] = True
            payload["errors"] = credential_errors
            self._open_orders_cache = (now, payload)
            return dict(payload)
        try:
            sdk = self._sdk()
            client = self._authenticated_client(sdk)
            raw_orders, retry_reasons = self._call_sdk_with_retry(
                lambda: client.get_open_orders(only_first_page=True),
                retry_count,
                retry_delay_ms,
            )
            orders = raw_orders if isinstance(raw_orders, list) else []
            payload["orders"] = [_public_open_order(row) for row in orders if isinstance(row, dict)]
            payload["count"] = len(payload["orders"])
            payload["ready"] = True
            if retry_reasons:
                payload["retry_reasons"] = retry_reasons
        except Exception as exc:  # noqa: BLE001 - 官方 open orders 状态用于监控，不能影响主快照。
            payload["errors"].append(f"读取官方 open orders 失败: {type(exc).__name__}: {exc}")
            retry_reasons = _exception_retry_reasons(exc)
            if retry_reasons:
                payload["retry_reasons"] = retry_reasons
        self._open_orders_cache = (now, payload)
        return payload

    def cached_open_orders_state(self) -> dict[str, Any]:
        if self._open_orders_cache:
            checked_at, payload = self._open_orders_cache
            cached = dict(payload)
            cached["cached"] = True
            cached["stale"] = time.time() - checked_at > 10.0
            return cached
        return {
            "ready": False,
            "skipped": True,
            "errors": ["open orders cache empty; async refresh pending"],
            "orders": [],
            "count": 0,
            "checked_at": None,
            "cached": True,
            "cache_missing": True,
        }

    def geoblock_state(
        self,
        *,
        max_age_seconds: float = 300.0,
        force: bool = False,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        now = time.time()
        if not force and self._geoblock_cache and now - self._geoblock_cache[0] <= max_age_seconds:
            return dict(self._geoblock_cache[1])
        payload: dict[str, Any] = {
            "ready": False,
            "blocked": None,
            "country": None,
            "region": None,
            "checked_at": now,
            "errors": [],
            "source": LIVE_GEOBLOCK_URL,
        }
        try:
            request = urllib.request.Request(
                LIVE_GEOBLOCK_URL,
                headers={"User-Agent": "polybot2other-live-preflight/1.0"},
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(2048)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("unexpected geoblock response")
            payload.update(
                {
                    "ready": True,
                    "blocked": bool(data.get("blocked")),
                    "country": str(data.get("country") or "") or None,
                    "region": str(data.get("region") or "") or None,
                }
            )
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            payload["errors"].append(f"无法确认 Polymarket 地区访问状态: {type(exc).__name__}: {exc}")
        self._geoblock_cache = (now, payload)
        return dict(payload)

    def _post_market_order(
        self,
        token_id: str,
        amount: float,
        side: str,
        price: float,
        tick_size: str | None,
        neg_risk: bool | None,
        retry_count: int,
        retry_delay_ms: int,
    ) -> LiveOrderResponse:
        sdk = self._sdk()
        client = self._authenticated_client(sdk)
        order_args, options = self._market_order_args(
            sdk,
            token_id=token_id,
            amount=amount,
            side=side,
            price=price,
            tick_size=tick_size,
            neg_risk=neg_risk,
            user_usdc_balance=amount if str(side or "").upper() == "BUY" else None,
        )
        signed_order_response = self._create_signed_market_order_with_retry(
            client,
            order_args,
            options,
            retry_count,
            retry_delay_ms,
        )
        if isinstance(signed_order_response, LiveOrderResponse):
            return signed_order_response
        signed_order = signed_order_response
        return self._post_signed_order_with_retry(client, sdk, signed_order, side, retry_count, retry_delay_ms)

    def _market_order_args(
        self,
        sdk: dict[str, Any],
        *,
        token_id: str,
        amount: float,
        side: str,
        price: float,
        tick_size: str | None,
        neg_risk: bool | None,
        user_usdc_balance: float | None = None,
    ) -> tuple[Any, Any]:
        normalized_side = "BUY" if str(side or "").upper() == "BUY" else "SELL"
        order_kwargs: dict[str, Any] = {
            "token_id": str(token_id),
            "amount": round(float(amount), 6),
            "side": sdk["Side"].BUY if normalized_side == "BUY" else sdk["Side"].SELL,
            "price": max(0.01, min(0.99, round(float(price), 4))),
            "order_type": sdk["OrderType"].FAK,
        }
        if normalized_side == "BUY" and user_usdc_balance is not None:
            order_kwargs["user_usdc_balance"] = round(float(user_usdc_balance), 6)
        order_args = sdk["MarketOrderArgs"](**order_kwargs)
        options = sdk["PartialCreateOrderOptions"](
            tick_size=_normalize_tick_size(tick_size),
            neg_risk=neg_risk if isinstance(neg_risk, bool) else None,
        )
        return order_args, options

    def _create_signed_market_order_with_retry(
        self,
        client: Any,
        order_args: Any,
        options: Any,
        retry_count: int,
        retry_delay_ms: int,
    ) -> Any | LiveOrderResponse:
        attempts = max(1, int(retry_count) + 1)
        last_error: Exception | None = None
        retry_reasons: list[str] = []
        for attempt in range(attempts):
            try:
                return client.create_market_order(order_args, options)
            except Exception as exc:  # noqa: BLE001 - 还没提交订单，重试不会产生重复真实订单。
                last_error = exc
                if attempt >= attempts - 1 or not _retryable_order_exception(exc):
                    break
                retry_reasons.append(f"{type(exc).__name__}: {exc}")
                delay = max(0, int(retry_delay_ms)) / 1000.0
                if delay:
                    time.sleep(delay)
        return LiveOrderResponse(
            success=False,
            status="CREATE_ERROR",
            order_id=None,
            error=f"{type(last_error).__name__}: {last_error}",
            raw={
                "stage": "create_market_order",
                "exception": f"{type(last_error).__name__}: {last_error}",
                "retry_reasons": retry_reasons,
                "submitted_to_clob": False,
            },
        )

    def _call_sdk_with_retry(
        self,
        operation: Any,
        retry_count: int | None,
        retry_delay_ms: int | None,
    ) -> tuple[Any, list[str]]:
        configured_count = self.settings.live_trading_default_retry_count if retry_count is None else retry_count
        configured_delay = self.settings.live_trading_default_retry_delay_ms if retry_delay_ms is None else retry_delay_ms
        attempts = max(1, int(configured_count) + 1)
        last_error: Exception | None = None
        retry_reasons: list[str] = []
        for attempt in range(attempts):
            try:
                return operation(), retry_reasons
            except Exception as exc:  # noqa: BLE001 - 官方读/同步接口也按配置快速重试。
                last_error = exc
                if attempt >= attempts - 1 or not _retryable_order_exception(exc):
                    break
                retry_reasons.append(f"{type(exc).__name__}: {exc}")
                delay = max(0, int(configured_delay)) / 1000.0
                if delay:
                    time.sleep(delay)
        if last_error is None:
            raise RuntimeError("Polymarket SDK call failed without exception")
        if retry_reasons:
            setattr(last_error, "_polybot_retry_reasons", retry_reasons)
        raise last_error

    def _post_signed_order_with_retry(
        self,
        client: Any,
        sdk: dict[str, Any],
        signed_order: Any,
        side: str,
        retry_count: int,
        retry_delay_ms: int,
    ) -> LiveOrderResponse:
        attempts = max(1, int(retry_count) + 1)
        last_error: Exception | None = None
        retry_reasons: list[str] = []
        signed_order_hash = _signed_order_hash(client, signed_order)
        for attempt in range(attempts):
            try:
                raw = client.post_order(signed_order, sdk["OrderType"].FAK)
                response = _order_response(raw, side)
                if signed_order_hash:
                    response.raw["signed_order_hash"] = signed_order_hash
                if retry_reasons:
                    response.raw["retry_reasons"] = retry_reasons
                    response.raw["attempts"] = attempt + 1
                return response
            except Exception as exc:  # noqa: BLE001 - 同一份签名订单可按配置快速重发。
                last_error = exc
                if attempt >= attempts - 1 or not _retryable_order_exception(exc):
                    break
                retry_reasons.append(f"{type(exc).__name__}: {exc}")
                delay = max(0, int(retry_delay_ms)) / 1000.0
                if delay:
                    time.sleep(delay)
        if signed_order_hash and last_error is not None and _retryable_order_exception(last_error):
            return LiveOrderResponse(
                success=True,
                status="POST_STATUS_UNKNOWN",
                order_id=signed_order_hash,
                error=f"{type(last_error).__name__}: {last_error}",
                raw={
                    "stage": "post_order",
                    "exception": f"{type(last_error).__name__}: {last_error}",
                    "retry_reasons": retry_reasons,
                    "same_signed_order_retry": True,
                    "submitted_to_clob_unknown": True,
                    "signed_order_hash": signed_order_hash,
                },
            )
        return LiveOrderResponse(
            success=False,
            status="ERROR",
            order_id=None,
            error=f"{type(last_error).__name__}: {last_error}",
            raw={
                "exception": f"{type(last_error).__name__}: {last_error}",
                "retry_reasons": retry_reasons,
                "same_signed_order_retry": True,
            },
        )

    def _authenticated_client(self, sdk: dict[str, Any]) -> Any:
        fingerprint = self._credential_fingerprint()
        if self._client is not None and self._client_credential_fingerprint == fingerprint:
            return self._client
        if self._client is not None:
            self._clear_authenticated_state()
        private_key = _env("POLYBOT2OTHER_LIVE_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("POLYBOT2OTHER_LIVE_PRIVATE_KEY is required")
        signature_type = _int_or_none(_env("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE"))
        funder = _env("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS")
        creds = self._api_creds(sdk)
        kwargs = {
            "host": self.host,
            "chain_id": self.chain_id,
            "key": private_key,
            "signature_type": signature_type,
            "funder": funder or None,
            "creds": creds,
            "retry_on_error": False,
        }
        client = sdk["ClobClient"](**kwargs)
        if creds is None:
            derived = client.create_or_derive_api_key()
            kwargs["creds"] = derived
            client = sdk["ClobClient"](**kwargs)
        self._client = client
        self._client_credential_fingerprint = fingerprint
        return client

    def _api_creds(self, sdk: dict[str, Any]) -> Any | None:
        api_key = _env("POLYBOT2OTHER_LIVE_API_KEY")
        api_secret = _env("POLYBOT2OTHER_LIVE_API_SECRET")
        api_passphrase = _env("POLYBOT2OTHER_LIVE_API_PASSPHRASE")
        if not any((api_key, api_secret, api_passphrase)):
            return None
        if not (api_key and api_secret and api_passphrase):
            raise RuntimeError("POLYBOT2OTHER_LIVE_API_KEY/SECRET/PASSPHRASE must be provided together")
        return sdk["ApiCreds"](api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)

    def _credential_mode(self) -> str:
        if all(
            _env(name)
            for name in (
                "POLYBOT2OTHER_LIVE_API_KEY",
                "POLYBOT2OTHER_LIVE_API_SECRET",
                "POLYBOT2OTHER_LIVE_API_PASSPHRASE",
            )
        ):
            return "env_api_creds"
        return "derive_api_creds_with_private_key"

    def _credential_presence(self) -> dict[str, Any]:
        api_parts = [
            bool(_env("POLYBOT2OTHER_LIVE_API_KEY")),
            bool(_env("POLYBOT2OTHER_LIVE_API_SECRET")),
            bool(_env("POLYBOT2OTHER_LIVE_API_PASSPHRASE")),
        ]
        return {
            "private_key": bool(_env("POLYBOT2OTHER_LIVE_PRIVATE_KEY")),
            "signature_type": bool(_env("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE")),
            "funder_address": bool(_env("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS")),
            "api_creds_complete": all(api_parts),
            "api_creds_partial": any(api_parts) and not all(api_parts),
        }

    def _credential_address_summary(self) -> dict[str, Any]:
        private_key = _env("POLYBOT2OTHER_LIVE_PRIVATE_KEY")
        funder = _env("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS")
        signature_type = _int_or_none(_env("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE"))
        summary: dict[str, Any] = {
            "signature_type": signature_type,
            "funder_address": funder if _is_address_like(funder) else None,
            "funder_address_masked": _mask_address(funder) if _is_address_like(funder) else None,
            "signer_address": None,
            "signer_address_masked": None,
            "signer_matches_funder": None,
            "warnings": [],
            "errors": [],
        }
        if not private_key or not _is_private_key_like(private_key):
            return summary
        try:
            signer_address = self._signer_address_from_private_key(self._sdk(), private_key)
        except Exception as exc:  # noqa: BLE001 - readiness_errors 负责决定是否阻断，这里只暴露安全摘要。
            summary["errors"].append(f"无法推导 signer address: {type(exc).__name__}: {exc}")
            return summary
        summary["signer_address"] = signer_address
        summary["signer_address_masked"] = _mask_address(signer_address)
        if _is_address_like(funder):
            matches = signer_address.lower() == funder.lower()
            summary["signer_matches_funder"] = matches
            if signature_type == 0 and not matches:
                summary["warnings"].append("EOA 模式下 funder address 应等于私钥 signer address")
        return summary

    def _signer_address_from_private_key(self, sdk: dict[str, Any], private_key: str) -> str:
        client = sdk["ClobClient"](
            self.host,
            chain_id=self.chain_id,
            key=private_key,
            retry_on_error=False,
        )
        address = client.get_address()
        if not address:
            raise RuntimeError("ClobClient.get_address returned empty address")
        return str(address)

    def _credential_fingerprint(self) -> str:
        parts = [
            self.host,
            str(self.chain_id),
            _env("POLYBOT2OTHER_LIVE_PRIVATE_KEY"),
            _env("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE"),
            _env("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS"),
            _env("POLYBOT2OTHER_LIVE_API_KEY"),
            _env("POLYBOT2OTHER_LIVE_API_SECRET"),
            _env("POLYBOT2OTHER_LIVE_API_PASSPHRASE"),
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _ensure_credential_cache_current(self) -> None:
        if self._client is None:
            return
        if self._client_credential_fingerprint != self._credential_fingerprint():
            self._clear_authenticated_state()

    def _clear_authenticated_state(self) -> None:
        self._client = None
        self._client_credential_fingerprint = None
        self._wallet_cache = None
        self._token_cache.clear()
        self._open_orders_cache = None

    def clear_cached_credentials(self) -> None:
        self._clear_authenticated_state()

    def _balance_allowance_params(self, sdk: dict[str, Any], asset_type: Any, token_id: str | None = None) -> Any:
        signature_type = _int_or_none(_env("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE"))
        kwargs: dict[str, Any] = {"asset_type": asset_type}
        if token_id:
            kwargs["token_id"] = token_id
        if signature_type is not None:
            kwargs["signature_type"] = signature_type
        return sdk["BalanceAllowanceParams"](**kwargs)

    def _sdk(self) -> dict[str, Any]:
        try:
            from py_clob_client_v2 import (  # type: ignore[import-not-found]
                ApiCreds,
                ClobClient,
                MarketOrderArgs,
                OrderType,
                PartialCreateOrderOptions,
                Side,
                TradeParams,
                BalanceAllowanceParams,
                AssetType,
                OpenOrderParams,
            )
        except Exception as exc:  # noqa: BLE001
            self._sdk_error = str(exc)
            raise
        self._sdk_error = None
        return {
            "ApiCreds": ApiCreds,
            "ClobClient": ClobClient,
            "MarketOrderArgs": MarketOrderArgs,
            "OrderType": OrderType,
            "PartialCreateOrderOptions": PartialCreateOrderOptions,
            "Side": Side,
            "TradeParams": TradeParams,
            "BalanceAllowanceParams": BalanceAllowanceParams,
            "AssetType": AssetType,
            "OpenOrderParams": OpenOrderParams,
        }

    def _sdk_compatibility_errors(self, sdk: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        required_exports = (
            "ApiCreds",
            "ClobClient",
            "MarketOrderArgs",
            "OrderType",
            "PartialCreateOrderOptions",
            "Side",
            "TradeParams",
            "BalanceAllowanceParams",
            "AssetType",
            "OpenOrderParams",
        )
        for name in required_exports:
            if sdk.get(name) is None:
                errors.append(f"py_clob_client_v2 缺少导出 {name}，当前 SDK 版本不兼容")
        if errors:
            return errors

        clob_client = sdk["ClobClient"]
        required_methods = (
            "create_market_order",
            "post_order",
            "update_balance_allowance",
            "get_balance_allowance",
            "get_order",
            "get_trades",
            "get_open_orders",
            "cancel_all",
            "create_or_derive_api_key",
        )
        for name in required_methods:
            if not callable(getattr(clob_client, name, None)):
                errors.append(f"py_clob_client_v2.ClobClient 缺少方法 {name}，当前 SDK 版本不兼容")

        constructor_params = {
            "ClobClient": ("host", "chain_id", "key", "creds", "signature_type", "funder", "retry_on_error"),
            "ApiCreds": ("api_key", "api_secret", "api_passphrase"),
            "MarketOrderArgs": ("token_id", "amount", "side", "price", "order_type", "user_usdc_balance"),
            "PartialCreateOrderOptions": ("tick_size", "neg_risk"),
            "TradeParams": ("market", "asset_id"),
            "BalanceAllowanceParams": ("asset_type", "token_id", "signature_type"),
            "OpenOrderParams": ("id", "market", "asset_id"),
        }
        for export_name, required_params in constructor_params.items():
            obj = sdk[export_name]
            try:
                params = inspect.signature(obj).parameters
            except (TypeError, ValueError) as exc:
                errors.append(f"py_clob_client_v2.{export_name} 无法读取签名: {exc}")
                continue
            missing = [param for param in required_params if param not in params]
            if missing:
                errors.append(
                    f"py_clob_client_v2.{export_name} 缺少参数 {', '.join(missing)}，当前 SDK 版本不兼容"
                )

        enum_attrs = {
            "OrderType": ("FAK",),
            "Side": ("BUY", "SELL"),
            "AssetType": ("COLLATERAL", "CONDITIONAL"),
        }
        for export_name, attrs in enum_attrs.items():
            obj = sdk[export_name]
            for attr in attrs:
                if not hasattr(obj, attr):
                    errors.append(f"py_clob_client_v2.{export_name} 缺少 {attr}，当前 SDK 版本不兼容")
        return errors


class LiveStrategyRunner:
    def __init__(self, settings: Settings, polymarket: PolymarketClient) -> None:
        self.settings = settings
        self.polymarket = polymarket
        self.variant = _live_strategy_variant(LIVE_VARIANT_ID)
        self.settings_store = LiveSettingsStore(settings.live_trading_settings_path, self._default_config(settings))
        self.process_lock = LiveProcessLock(
            settings.live_trading_settings_path.with_name(f"{settings.live_trading_settings_path.name}.lock")
        )
        loaded_config = self.settings_store.load()
        self.startup_rearm_skipped_active_lock = False
        if loaded_config.enabled and self.process_lock.active_holder_exists():
            self.startup_rearmed = False
            self.startup_rearm_skipped_active_lock = True
            self.config = replace(loaded_config, enabled=False).normalized()
        else:
            self.startup_rearmed = bool(loaded_config.enabled)
            self.config = (
                self.settings_store.save(replace(loaded_config, enabled=False).normalized())
                if self.startup_rearmed
                else loaded_config
            )
        self.variant = _live_strategy_variant(self.config.live_strategy_id)
        self.store = TradeStore(
            _live_strategy_db_path(settings, self.config.live_strategy_id),
            self.config.initial_balance,
        )
        self.store.rebase_initial_balance(self.config.initial_balance)
        self.client = PolymarketLiveClient(settings)
        self.strategy = RealBtcFiveMinuteStrategy(settings)
        self.last_signal: dict[str, Any] | None = None
        self.last_error: str | None = (
            LIVE_ACTIVE_LOCK_PRESERVE_MESSAGE
            if self.startup_rearm_skipped_active_lock
            else LIVE_STARTUP_REARM_MESSAGE
            if self.startup_rearmed
            else None
        )
        self.last_order_at: float | None = None
        self.last_order: dict[str, Any] | None = None
        self.run_count = 0
        self.last_run_at: float | None = None
        self.overlap_skip_count = 0
        self._official_recheck_next_at: dict[str, float] = {}
        self._official_price_backfill_next_at: dict[str, float] = {}
        self._live_order_reconcile_next_at: dict[str, float] = {}
        self._stop_win_next_check_at: dict[int, float] = {}
        self._stop_win_closed_rounds: dict[str, float] = {}
        self.last_stop_win: dict[str, Any] | None = None
        self._run_lock = threading.Lock()
        self._status_refresh_lock = threading.Lock()
        self._status_refresh_thread: threading.Thread | None = None

    @property
    def variant_id(self) -> str:
        override = getattr(self, "_variant_id_override", None)
        return str(override if override is not None else self.variant.variant_id)

    @variant_id.setter
    def variant_id(self, value: str) -> None:
        self._variant_id_override = str(value)

    @property
    def combo(self) -> str:
        override = getattr(self, "_combo_override", None)
        if override is not None:
            return str(override)
        return str(_live_strategy_meta(self.variant_id).get("combo") or LIVE_COMBO)

    @combo.setter
    def combo(self, value: str) -> None:
        self._combo_override = str(value)

    @property
    def entry_marker(self) -> str:
        override = getattr(self, "_entry_marker_override", None)
        if override is not None:
            return str(override)
        return str(_live_strategy_meta(self.variant_id).get("entry_marker") or LIVE_ENTRY_MARKER)

    @entry_marker.setter
    def entry_marker(self, value: str) -> None:
        self._entry_marker_override = str(value)

    @property
    def stop_win_enabled(self) -> bool:
        override = getattr(self, "_stop_win_enabled_override", None)
        if override is not None:
            return bool(override)
        return bool(_live_strategy_meta(self.variant_id).get("stop_win_enabled"))

    @stop_win_enabled.setter
    def stop_win_enabled(self, value: bool) -> None:
        self._stop_win_enabled_override = bool(value)

    @staticmethod
    def _default_config(settings: Settings) -> LiveRuntimeConfig:
        return LiveRuntimeConfig(
            enabled=False,
            live_strategy_id=LIVE_VARIANT_ID,
            initial_balance=settings.live_trading_default_initial_balance,
            stake_dollars=settings.live_trading_default_stake_dollars,
            max_open_trades=settings.max_open_trades,
            max_daily_loss=settings.live_trading_default_max_daily_loss,
            max_total_drawdown=settings.live_trading_default_max_total_drawdown,
            max_entry_price=settings.max_entry_price,
            fallback_sources=(),
            retry_count=settings.live_trading_default_retry_count,
            retry_delay_ms=settings.live_trading_default_retry_delay_ms,
            paper_stop_win_take_profit_pct=LIVE_PAPER_STOP_WIN_DEFAULT_TAKE_PROFIT_PCT,
            compliance_acknowledged=False,
            updated_at=time.time(),
        )

    def settings_payload(self) -> dict[str, Any]:
        payload = asdict(self.config)
        payload["variant_id"] = self.variant_id
        payload["combo"] = self.combo
        payload["live_strategy_id"] = self.variant_id
        payload["live_strategy"] = {
            **_live_strategy_meta(self.variant_id),
            "paper_stop_win_take_profit_pct": self.config.paper_stop_win_take_profit_pct,
        }
        payload["live_strategy_options"] = _live_strategy_options_payload(self.settings)
        payload["db_path"] = str(self.store.db_path)
        payload["process_lock_path"] = str(self.process_lock.path)
        payload["process_lock_acquired"] = self.process_lock.locked
        payload["process_lock"] = self.process_lock.payload()
        payload["startup_rearmed"] = self.startup_rearmed
        payload["startup_rearm_skipped_active_lock"] = self.startup_rearm_skipped_active_lock
        payload["settings_file"] = self.settings_store.file_status(self.config)
        payload["readiness"] = self.client.readiness(
            required_cash=self.config.stake_dollars,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        payload["open_orders"] = self.client.open_orders_state(
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        return payload

    def open_orders_payload(self, *, force: bool = False) -> dict[str, Any]:
        return {
            "execution_mode": "LIVE",
            "variant_id": self.variant_id,
            "combo": self.combo,
            "open_orders": self.client.open_orders_state(
                force=force,
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            ),
        }

    def evidence_payload(self, external_order_id: str | None = None, *, force: bool = True) -> dict[str, Any]:
        order_summary = self.store.paper_order_summary("BTC")
        trade_summary = self.store.recent_trade_summary("BTC")
        recent_orders = self.store.recent_paper_orders(20, 0, "BTC", "all")
        recent_order_total = self.store.paper_order_count("BTC", "all")
        recent_trades_page = self.recent_trades_page(20, 0, None, None)
        pending_orders = self.store.pending_external_orders(20, "BTC")
        readiness = self.client.readiness(
            required_cash=self.config.stake_dollars,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        official_open_orders = self.client.open_orders_state(
            force=force,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        order_id = str(external_order_id or "").strip()
        order = self.store.live_order_by_external_id(order_id) if order_id else None
        return {
            "checked_at": time.time(),
            "execution_mode": "LIVE",
            "variant_id": self.variant_id,
            "combo": self.combo,
            "db_path": str(self.store.db_path),
            "settings_path": str(self.settings.live_trading_settings_path),
            "process_lock": self.process_lock.payload(),
            "enabled": self.config.enabled,
            "startup_rearmed": self.startup_rearmed,
            "run_count": self.run_count,
            "last_run_at": self.last_run_at,
            "last_signal": dict(self.last_signal or {}),
            "last_error": self.last_error,
            "last_order_at": self.last_order_at,
            "last_order": dict(self.last_order or {}),
            "last_stop_win": dict(self.last_stop_win or {}),
            "settings": asdict(self.config),
            "software_account": {
                "account": self.store.account(),
                "metrics": self.store.metrics(),
                "order_summary": order_summary,
                "recent_trades_summary": trade_summary,
            },
            "readiness": readiness,
            "wallet": readiness.get("wallet") if isinstance(readiness.get("wallet"), dict) else None,
            "official_open_orders": official_open_orders,
            "open_trades": self.open_trades(),
            "pending_orders": _public_evidence_orders(pending_orders, variant_id=self.variant_id, combo=self.combo),
            "recent_orders": _public_evidence_orders(recent_orders, variant_id=self.variant_id, combo=self.combo),
            "recent_orders_meta": {
                "limit": 20,
                "offset": 0,
                "loaded": len(recent_orders),
                "total": recent_order_total,
                "has_more": len(recent_orders) < recent_order_total,
                "status_filter": "all",
            },
            "recent_trades": recent_trades_page["recent_trades"],
            "recent_trades_summary": recent_trades_page["recent_trades_summary"],
            "recent_trades_meta": recent_trades_page["recent_trades_meta"],
            "order": _public_evidence_order(order, variant_id=self.variant_id, combo=self.combo),
            "requested_external_order_id": order_id or None,
        }

    def _signal_from_state(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> tuple[Signal, dict[str, Any], dict[str, Any]]:
        signal_price, price_selection = _live_signal_price_payload(
            price,
            self.config.fallback_sources,
            self.settings.max_quote_age_ms,
            market.target_price,
        )
        if price_selection.get("blocked"):
            reason = str(price_selection.get("message") or "LIVE fallback price source blocked")
            return Signal("BTC", "NO_TRADE", 0.0, 0.0, 0.0, reason), signal_price, price_selection
        signal = self.strategy.signal(input_from_snapshot(market, {"price": signal_price, "quotes": quotes}))
        signal = self._apply_signal_filter_mode(market, signal, signal_price, quotes)
        note = str(price_selection.get("note") or "").strip()
        if note:
            signal = replace(signal, reason=_append_reason(signal.reason, note))
        return signal, signal_price, price_selection

    def _apply_signal_filter_mode(
        self,
        market: MarketRound,
        signal: Signal,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> Signal:
        """按实盘组合过滤信号；Aggressive Edge REAL、V10 REAL、V11 REAL 各自复用对应 Paper 口径。"""

        if self.variant.signal_filter_mode not in {
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
        } or signal.side not in {"Up", "Down"}:
            return signal
        block_reason = aggressive_edge_block_reason(market, signal, price, self.settings.max_quote_age_ms)
        if block_reason:
            return replace(signal, side="NO_TRADE", reason=_append_reason(signal.reason, block_reason))
        if self.variant.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC:
            v10_block_reason = self._aggressive_edge_v10_live_block_reason(market, signal, price, quotes)
            if v10_block_reason:
                logger.debug("Aggressive Edge V10 REAL 过滤拦截 round_id=%s reason=%s", market.round_id, v10_block_reason)
                return replace(signal, side="NO_TRADE", reason=_append_reason(signal.reason, v10_block_reason))
        if self.variant.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC:
            v11_block_reason, v11_pass_note = self._aggressive_edge_v11_live_guard_result(market, signal, price, quotes)
            if v11_block_reason:
                logger.debug("Aggressive Edge V11 REAL 过滤拦截 round_id=%s reason=%s", market.round_id, v11_block_reason)
                return replace(signal, side="NO_TRADE", reason=_append_reason(signal.reason, v11_block_reason))
            pass_note = aggressive_edge_pass_note(signal)
            if v11_pass_note:
                pass_note = _append_reason(pass_note, v11_pass_note)
            return replace(signal, reason=_append_reason(signal.reason, pass_note))
        return replace(signal, reason=_append_reason(signal.reason, aggressive_edge_pass_note(signal)))

    def _aggressive_edge_v10_live_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> str | None:
        """V10 REAL 入场守卫；和 V10 Diagnostic 使用同一批可解释特征。"""

        now = time.time()
        minute_bucket = _aggressive_edge_minute_bucket(market, now)
        quote = quotes.get(signal.side) if isinstance(quotes.get(signal.side), dict) else {}
        quote = self._quote_with_depth(market, signal.side, quote)
        signal_at = _updated_at_seconds(price.get("chainlink_updated_ms"), now)
        before60_tick = self.store.closest_price_tick(
            market.symbol or "BTC",
            signal_at - 60.0,
            max_distance_seconds=20.0,
            source_contains="chainlink",
        )
        before30_tick = self.store.closest_price_tick(
            market.symbol or "BTC",
            signal_at - 30.0,
            max_distance_seconds=15.0,
            source_contains="chainlink",
        )
        report = aggressive_edge_v2_risk_report(
            market,
            signal,
            price=price,
            quote=quote,
            signal_at=signal_at,
            before60_tick=before60_tick,
            before30_tick=before30_tick,
            max_age_ms=self.settings.max_quote_age_ms,
        )
        if report is None:
            return "SINGLE_AGGRESSIVE_EDGE V10_REAL_GUARD BLOCK: 缺少盘口风险报告，禁止实盘入场"
        features = report.get("features") if isinstance(report.get("features"), dict) else {}
        move_bps = _float_or_none(signal.move_bps)
        if move_bps is None:
            move_bps = _float_or_none(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        risk_score = _float_or_none(report.get("risk_score"))
        top_level_skew = _float_or_none(features.get("top_level_skew"))
        reasons: list[str] = []
        if risk_score is None:
            reasons.append("V10_RISK_SCORE_MISSING")
        elif risk_score >= LIVE_AGGRESSIVE_EDGE_V8_MAX_RISK_SCORE:
            reasons.append(f"V10_RISK_SCORE_EXTREME risk={risk_score:.4f}")
        if abs_move_bps >= LIVE_AGGRESSIVE_EDGE_V8_MAX_ABS_MOVE_BPS:
            reasons.append(f"V10_EXTREME_MOVE abs_move={abs_move_bps:.2f}bps")
        if minute_bucket in LIVE_AGGRESSIVE_EDGE_V9_BLOCKED_BUCKETS:
            reasons.append(f"V10_M1_BUCKET_BLOCK m{minute_bucket}")
        if signal.side == "Up":
            if abs_move_bps < LIVE_AGGRESSIVE_EDGE_V10_UP_MIN_ABS_MOVE_BPS:
                reasons.append(f"V10_UP_WEAK_MOVE abs_move={abs_move_bps:.2f}bps")
            if top_level_skew is None:
                reasons.append("V10_UP_TOP_SKEW_MISSING")
            elif top_level_skew < LIVE_AGGRESSIVE_EDGE_V10_UP_MIN_TOP_LEVEL_SKEW:
                reasons.append(f"V10_UP_WEAK_TOP_SKEW top={top_level_skew:.4f}")
        if not reasons:
            return None
        return (
            "SINGLE_AGGRESSIVE_EDGE V10_REAL_GUARD BLOCK: "
            + "; ".join(reasons)
            + f" | m{minute_bucket} side={signal.side} entry={_format_optional_float(signal.entry_price, 4)} "
            + f"abs_move={abs_move_bps:.2f} top={_format_optional_float(top_level_skew, 4)} risk={_format_optional_float(risk_score, 4)}"
        )

    def _aggressive_edge_v8_live_block_reason(
        self,
        signal: Signal,
        report: dict[str, Any],
        *,
        guard_label: str = "V11",
        minute_bucket: int,
        abs_move_bps: float,
        risk_score: float | None,
        entry_price: float | None,
    ) -> str | None:
        """V8 前置守卫；V11 系列 REAL 必须先通过样本准入使用的基础安全线。"""

        reasons: list[str] = []
        # 这里对齐 diagnostic V11 的 V8 前置逻辑，防止实盘比样本准入更宽松。
        if signal.side not in {"Up", "Down"}:
            reasons.append("V8_NO_SIDE")
        if entry_price is None:
            reasons.append("V8_ENTRY_MISSING")
        if risk_score is None:
            reasons.append("V8_RISK_SCORE_MISSING")
        elif risk_score >= LIVE_AGGRESSIVE_EDGE_V8_MAX_RISK_SCORE:
            reasons.append(f"V8_RISK_SCORE_EXTREME risk={risk_score:.4f}")
        if abs_move_bps >= LIVE_AGGRESSIVE_EDGE_V8_MAX_ABS_MOVE_BPS:
            reasons.append(f"V8_EXTREME_MOVE abs_move={abs_move_bps:.2f}bps")
        if minute_bucket not in LIVE_AGGRESSIVE_EDGE_V8_ALLOWED_BUCKETS:
            reasons.append(f"V8_BUCKET_INVALID m{minute_bucket}")
        if not reasons:
            return None
        features = report.get("features") if isinstance(report.get("features"), dict) else {}
        return (
            f"SINGLE_AGGRESSIVE_EDGE {guard_label}_REAL_GUARD BLOCK: V8 前置守卫未通过；"
            + "; ".join(reasons)
            + f" | m{minute_bucket} side={signal.side} entry={_format_optional_float(entry_price, 4)} "
            + f"abs_move={abs_move_bps:.2f} depth={_format_optional_float(_float_or_none(features.get('depth_skew')), 4)} "
            + f"top={_format_optional_float(_float_or_none(features.get('top_level_skew')), 4)} "
            + f"risk={_format_optional_float(risk_score, 4)}"
        )

    def _aggressive_edge_v11_live_guard_profile(self) -> tuple[str, float]:
        """返回 V11 系列实盘守卫标签和 Up 顶层盘口阈值，避免 V11 历史口径被覆盖。"""

        if self.variant_id == LIVE_AGGRESSIVE_EDGE_V11_1_VARIANT_ID:
            return "V11.1", LIVE_AGGRESSIVE_EDGE_V11_1_UP_MIN_TOP_LEVEL_SKEW
        return "V11", LIVE_AGGRESSIVE_EDGE_V11_UP_MIN_TOP_LEVEL_SKEW

    def _aggressive_edge_v11_live_guard_result(
        self,
        market: MarketRound,
        signal: Signal,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> tuple[str | None, str | None]:
        """V11 系列 REAL 入场守卫；和 V11 Diagnostic 使用同一批盘口深度、动量和风险特征。"""

        now = time.time()
        guard_label, up_min_top_level_skew = self._aggressive_edge_v11_live_guard_profile()
        minute_bucket = _aggressive_edge_minute_bucket(market, now)
        quote = quotes.get(signal.side) if isinstance(quotes.get(signal.side), dict) else {}
        quote = self._quote_with_depth(market, signal.side, quote)
        signal_at = _updated_at_seconds(price.get("chainlink_updated_ms"), now)
        before60_tick = self.store.closest_price_tick(
            market.symbol or "BTC",
            signal_at - 60.0,
            max_distance_seconds=20.0,
            source_contains="chainlink",
        )
        before30_tick = self.store.closest_price_tick(
            market.symbol or "BTC",
            signal_at - 30.0,
            max_distance_seconds=15.0,
            source_contains="chainlink",
        )
        report = aggressive_edge_v2_risk_report(
            market,
            signal,
            price=price,
            quote=quote,
            signal_at=signal_at,
            before60_tick=before60_tick,
            before30_tick=before30_tick,
            max_age_ms=self.settings.max_quote_age_ms,
        )
        if report is None:
            return f"SINGLE_AGGRESSIVE_EDGE {guard_label}_REAL_GUARD BLOCK: 缺少盘口风险报告，禁止实盘入场", None
        features = report.get("features") if isinstance(report.get("features"), dict) else {}
        entry_price = _float_or_none(signal.entry_price)
        if entry_price is None:
            entry_price = _float_or_none(features.get("entry_price"))
        move_bps = _float_or_none(signal.move_bps)
        if move_bps is None:
            move_bps = _float_or_none(features.get("move_bps"))
        abs_move_bps = abs(move_bps or 0.0)
        depth_skew = _float_or_none(features.get("depth_skew"))
        top_level_skew = _float_or_none(features.get("top_level_skew"))
        risk_score = _float_or_none(report.get("risk_score"))
        v8_block_reason = self._aggressive_edge_v8_live_block_reason(
            signal,
            report,
            guard_label=guard_label,
            minute_bucket=minute_bucket,
            abs_move_bps=abs_move_bps,
            risk_score=risk_score,
            entry_price=entry_price,
        )
        if v8_block_reason:
            return v8_block_reason, None
        reasons: list[str] = []
        if minute_bucket not in LIVE_AGGRESSIVE_EDGE_V11_ALLOWED_BUCKETS:
            reasons.append(f"V11_BUCKET_BLOCK m{minute_bucket}")
        if abs_move_bps < LIVE_AGGRESSIVE_EDGE_V11_MIN_ABS_MOVE_BPS:
            reasons.append(f"V11_WEAK_MOVE abs_move={abs_move_bps:.2f}bps")
        if depth_skew is None:
            reasons.append("V11_DEPTH_SKEW_MISSING")
        elif depth_skew < LIVE_AGGRESSIVE_EDGE_V11_MIN_DEPTH_SKEW:
            reasons.append(f"V11_WEAK_DEPTH depth={depth_skew:.4f}")
        if risk_score is None:
            reasons.append("V11_RISK_SCORE_MISSING")
        elif risk_score > LIVE_AGGRESSIVE_EDGE_V11_MAX_RISK_SCORE:
            reasons.append(f"V11_RISK_TOO_HIGH risk={risk_score:.4f}")
        if signal.side == "Up":
            # 实盘首批输单暴露出 Up 深度达标但顶层买盘偏薄的问题；V11.1 只在这里加严，方便和 V11 对照复盘。
            if top_level_skew is None:
                reasons.append("V11_UP_TOP_SKEW_MISSING")
            elif top_level_skew < up_min_top_level_skew:
                reasons.append(f"V11_UP_WEAK_TOP_SKEW top={top_level_skew:.4f} min={up_min_top_level_skew:.2f}")
        if not reasons:
            pass_note = (
                f"SINGLE_AGGRESSIVE_EDGE {guard_label}_REAL_GUARD PASS: "
                f"m{minute_bucket} side={signal.side} entry={_format_optional_float(entry_price, 4)} "
                f"abs_move={abs_move_bps:.2f} depth={_format_optional_float(depth_skew, 4)} "
                f"top={_format_optional_float(top_level_skew, 4)} "
                f"up_top_min={up_min_top_level_skew:.2f} risk={_format_optional_float(risk_score, 4)}"
            )
            logger.debug("Aggressive Edge %s REAL 放行 round_id=%s %s", guard_label, market.round_id, pass_note)
            return None, pass_note
        return (
            f"SINGLE_AGGRESSIVE_EDGE {guard_label}_REAL_GUARD BLOCK: "
            + "; ".join(reasons)
            + f" | m{minute_bucket} side={signal.side} entry={_format_optional_float(entry_price, 4)} "
            + f"abs_move={abs_move_bps:.2f} depth={_format_optional_float(depth_skew, 4)} "
            + f"top={_format_optional_float(top_level_skew, 4)} "
            + f"up_top_min={up_min_top_level_skew:.2f} risk={_format_optional_float(risk_score, 4)}"
        ), None

    def _aggressive_edge_v11_live_block_reason(
        self,
        market: MarketRound,
        signal: Signal,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> str | None:
        """兼容旧测试入口，返回 V11 REAL 拦截原因。"""

        block_reason, _pass_note = self._aggressive_edge_v11_live_guard_result(market, signal, price, quotes)
        return block_reason

    def preflight(
        self,
        market: MarketRound | None,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        now = time.time()
        payload: dict[str, Any] = {
            "checked_at": now,
            "variant_id": self.variant_id,
            "combo": self.combo,
            "live_strategy_id": self.variant_id,
            "live_strategy": _live_strategy_meta(self.variant_id),
            "execution_mode": "LIVE",
            "enabled": self.config.enabled,
            "checks": checks,
            "ready": False,
            "arming_ready": False,
            "can_enable_live": False,
            "can_place_next_order": False,
            "blocked_checks": [],
        }
        checks.append(_preflight_check("runtime", True, "live runner 已加载"))
        checks.append(_preflight_check("enabled", self.config.enabled, "实盘开关已开启", "实盘开关当前关闭"))
        if self.variant.signal_filter_mode in {
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
        }:
            sample_version = (
                "V11"
                if self.variant.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC
                else "V10"
            )
            sample_readiness = _live_aggressive_edge_readiness(self.settings, sample_version)
            payload["sample_readiness"] = sample_readiness
            sample_ready = bool(sample_readiness.get("eligible_for_live_review"))
            sample_reasons = sample_readiness.get("reasons") if isinstance(sample_readiness.get("reasons"), list) else []
            sample_message = (
                f"{sample_version} 样本准入通过: {sample_readiness.get('settled_count', 0)}/80"
                if sample_ready
                else (
                    "；".join(str(item) for item in sample_reasons[:3])
                    or str(sample_readiness.get("label") or f"{sample_version} 样本准入未通过")
                )
            )
            checks.append(
                _preflight_check(
                    "sample_readiness",
                    sample_ready,
                    sample_message,
                    sample_message,
                    version=sample_version,
                    settled_count=int(sample_readiness.get("settled_count") or 0),
                    win_rate_pct=sample_readiness.get("win_rate_pct"),
                    simulated_roi_pct=sample_readiness.get("simulated_roi_pct"),
                )
            )
        checks.append(
            _preflight_check(
                "process_lock",
                (not self.config.enabled) or self.process_lock.locked,
                "实盘进程锁已持有" if self.process_lock.locked else "实盘未开启，无需持有进程锁",
                "实盘进程锁未持有",
                path=str(self.process_lock.path),
            )
        )
        checks.append(
            _preflight_check(
                "compliance_acknowledged",
                self.config.compliance_acknowledged,
                "风险确认已勾选",
                "风险确认未勾选",
            )
        )
        geo_check = self.client.geoblock_state(force=True)
        payload["geo_check"] = geo_check
        geo_block_reason = _geoblock_block_reason(geo_check)
        geo_ok = geo_block_reason is None
        checks.append(
            _preflight_check(
                "geo_access",
                geo_ok,
                (
                    "Polymarket 地区访问检查通过"
                    if geo_check.get("country")
                    else "Polymarket 地区访问检查通过"
                ),
                geo_block_reason or "",
                errors=list(geo_check.get("errors") or []),
                blocked=geo_check.get("blocked"),
                country=geo_check.get("country"),
                region=geo_check.get("region"),
            )
        )
        readiness_errors = self.client.readiness_errors()
        checks.append(
            _preflight_check(
                "credentials",
                not readiness_errors,
                "SDK 和凭证格式检查通过",
                readiness_errors[0] if readiness_errors else "",
                errors=readiness_errors,
            )
        )
        if market is None:
            checks.append(_preflight_check("market", False, "", "当前市场不可用"))
            return _finalize_preflight_payload(payload, signal_ok=False)

        self.store.upsert_round(market)
        payload["market"] = {
            "round_id": market.round_id,
            "target_price": market.target_price,
            "ends_at": market.ends_at,
            "up_token": market.up_token,
            "down_token": market.down_token,
        }
        checks.append(_preflight_check("market", True, f"当前市场 {market.round_id}"))
        checks.append(
            _preflight_check(
                "target_price",
                market.target_price > 0,
                f"官方目标价 {market.target_price:.2f}",
                "缺少官方 market.target_price",
            )
        )

        signal, _signal_price, price_selection = self._signal_from_state(market, price, quotes)
        payload["price_selection"] = price_selection
        payload["price_basis"] = price_selection.get("basis") or []
        payload["signal"] = {
            "symbol": signal.symbol,
            "side": signal.side,
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "move_bps": signal.move_bps,
            "reason": signal.reason,
        }
        signal_ok = signal.side in {"Up", "Down"}
        checks.append(
            _preflight_check(
                "signal",
                signal_ok,
                f"当前信号 {signal.side}",
                f"当前策略不下单: {signal.side}",
            )
        )

        account = self.store.account()
        stake, stake_source = self._entry_stake_for_market(market, account)
        payload["software_account"] = {
            "initial_balance": account["initial_balance"],
            "cash_balance": account["cash_balance"],
            "configured_stake": self.config.stake_dollars,
            "stake": round(stake, 6),
            "stake_source": stake_source,
            "stake_locked_to_current_market": stake_source == "current_market_open_trade",
        }
        checks.append(
            _preflight_check(
                "software_cash",
                stake >= LIVE_MIN_USDC,
                f"软件隔离账户可用 {float(account['cash_balance']):.4f}，本次预算 {stake:.4f}",
                "软件隔离账户可用资金不足",
            )
        )

        risk_ok = False
        min_order_ok = False
        depth_ok = False
        wallet_ok = False
        open_orders_ok = False
        entry_token_id: str | None = None
        quote: dict[str, Any] | None = None
        limit_price = 0.0
        if signal_ok:
            risk_reason = self._entry_block_reason(market, signal, skip_readiness=True)
            risk_ok = risk_reason is None
            checks.append(
                _preflight_check(
                    "strategy_risk",
                    risk_ok,
                    "策略风控检查通过",
                    risk_reason or "",
                )
            )
            quote = self._quote_with_depth(market, signal.side, quotes.get(signal.side) or {})
            limit_price = self._entry_limit_price(signal)
            min_order_size = _float(quote.get("min_order_size"), 0.0)
            entry_token_id = market.up_token if signal.side == "Up" else market.down_token
            sweep = sweep_taker_buy_by_budget(
                quote,
                limit_price=limit_price,
                budget=stake,
                taker_fee_rate=self.settings.paper_taker_fee_rate,
            )
            min_order_ok = not min_order_size or stake + LIVE_EPSILON >= min_order_size
            depth_ok = sweep.shares >= self.settings.min_ask_size
            payload["entry"] = {
                "side": signal.side,
                "token_id": entry_token_id,
                "limit_price": limit_price,
                "stake": round(stake, 6),
                "best_ask": quote.get("best_ask"),
                "min_order_size": min_order_size,
                "tick_size": quote.get("tick_size"),
                "neg_risk": quote.get("neg_risk"),
                "sweep_shares": sweep.shares,
                "sweep_avg_price": sweep.avg_price,
                "sweep_cash_spent": sweep.cash_spent,
            }
            checks.append(
                _preflight_check(
                    "min_order_size",
                    min_order_ok,
                    "市场最小订单检查通过",
                    f"实盘预算 {stake:.2f} 低于市场最小订单 {min_order_size:.2f}",
                    stake=round(stake, 6),
                    min_order_size=round(min_order_size, 6),
                    shortfall=round(max(0.0, min_order_size - stake), 6),
                )
            )
            checks.append(
                _preflight_check(
                    "orderbook_depth",
                    depth_ok,
                    f"可成交份额 {sweep.shares:.6f}",
                    "FAK 可成交份额不足",
                )
            )

        if not readiness_errors and geo_ok:
            official_open_orders = self.client.open_orders_state(
                force=True,
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
            payload["official_open_orders"] = official_open_orders
            open_orders_count = int(official_open_orders.get("count") or 0)
            open_order_errors = list(official_open_orders.get("errors") or [])
            open_orders_ok = bool(official_open_orders.get("ready")) and open_orders_count == 0 and not open_order_errors
            checks.append(
                _preflight_check(
                    "official_open_orders_clear",
                    open_orders_ok,
                    "官方 CLOB open orders 为 0",
                    (
                        f"官方 CLOB 仍有 {open_orders_count} 笔 open orders，先刷新挂单或执行实盘急停"
                        if open_orders_count
                        else (open_order_errors[0] if open_order_errors else "官方 CLOB open orders 未确认")
                    ),
                    errors=open_order_errors,
                    count=open_orders_count,
                )
            )
            wallet = self.client.wallet_state(
                required_cash=stake,
                force=True,
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
            wallet_ok = not wallet.get("errors")
            payload["wallet"] = wallet
            checks.append(
                _preflight_check(
                    "collateral_wallet",
                    wallet_ok,
                    "collateral balance/allowance 检查通过",
                    wallet.get("errors", ["collateral wallet 未就绪"])[0] if wallet.get("errors") else "",
                    errors=wallet.get("errors") or [],
                )
            )

        if (
            signal_ok
            and not readiness_errors
            and geo_ok
            and risk_ok
            and min_order_ok
            and depth_ok
            and wallet_ok
            and open_orders_ok
            and entry_token_id
            and quote is not None
        ):
            signing = self.client.sign_market_order_preview(
                token_id=entry_token_id,
                amount=stake,
                side="BUY",
                price=limit_price,
                tick_size=str(quote.get("tick_size") or "0.01"),
                neg_risk=quote.get("neg_risk") if isinstance(quote.get("neg_risk"), bool) else None,
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
            signing_errors = list(signing.get("errors") or [])
            payload["signing"] = signing
            checks.append(
                _preflight_check(
                    "sign_market_order",
                    bool(signing.get("ready")) and not signing_errors and signing.get("submitted_to_clob") is False,
                    "SDK 可构造并签名 FAK 订单，未提交到 CLOB",
                    signing_errors[0] if signing_errors else "SDK 签名预检失败",
                    errors=signing_errors,
                )
            )

        return _finalize_preflight_payload(payload, signal_ok=signal_ok)

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.config
        requested_strategy_id = _normalize_live_strategy_id(
            payload.get("live_strategy_id", current.live_strategy_id)
        )
        switch_block_reason = self._live_strategy_switch_block_reason(current.live_strategy_id, requested_strategy_id)
        if switch_block_reason:
            self.last_error = switch_block_reason
            raise ValueError(switch_block_reason)
        next_config = replace(
            current,
            enabled=_bool(payload.get("enabled"), current.enabled),
            live_strategy_id=requested_strategy_id,
            initial_balance=_float(payload.get("initial_balance"), current.initial_balance),
            stake_dollars=_float(payload.get("stake_dollars"), current.stake_dollars),
            max_open_trades=_int(payload.get("max_open_trades"), current.max_open_trades),
            max_daily_loss=_float(payload.get("max_daily_loss"), current.max_daily_loss),
            max_total_drawdown=_float(payload.get("max_total_drawdown"), current.max_total_drawdown),
            max_entry_price=_float(payload.get("max_entry_price"), current.max_entry_price),
            fallback_sources=_normalize_live_fallback_sources(
                payload.get("fallback_sources", current.fallback_sources)
            ),
            retry_count=_int(payload.get("retry_count"), current.retry_count),
            retry_delay_ms=_int(payload.get("retry_delay_ms"), current.retry_delay_ms),
            paper_stop_win_take_profit_pct=_float(
                payload.get("paper_stop_win_take_profit_pct"),
                current.paper_stop_win_take_profit_pct,
            ),
            compliance_acknowledged=_bool(
                payload.get("compliance_acknowledged"),
                current.compliance_acknowledged,
            ),
        ).normalized()
        self.config = self.settings_store.save(next_config)
        self.variant = _live_strategy_variant(self.config.live_strategy_id)
        self._sync_strategy_store()
        if self.config.enabled:
            block_reason = self._enable_block_reason()
            if block_reason:
                self.config = self.settings_store.save(replace(self.config, enabled=False).normalized())
                self._release_process_lock()
                self.last_error = block_reason
            else:
                self.last_error = None
        else:
            self._release_process_lock()
            if payload.get("enabled") is not None:
                self.last_error = None
        return self.settings_payload()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        return self.update_settings({"enabled": bool(enabled)})

    def _sync_strategy_store(self) -> None:
        db_path = _live_strategy_db_path(self.settings, self.config.live_strategy_id)
        if getattr(self.store, "db_path", None) == db_path:
            self.store.rebase_initial_balance(self.config.initial_balance)
            return
        previous_store = self.store
        self.store = TradeStore(db_path, self.config.initial_balance)
        self.store.rebase_initial_balance(self.config.initial_balance)
        try:
            previous_store.conn.close()
        except Exception:
            pass

    def emergency_stop(self) -> dict[str, Any]:
        was_enabled = self.config.enabled
        self.config = self.settings_store.save(replace(self.config, enabled=False).normalized())
        try:
            cancel_result = self.client.cancel_all_orders(
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
        finally:
            self._release_process_lock()
        if cancel_result.get("errors"):
            self.last_error = str(cancel_result["errors"][0])
        else:
            self.last_error = None
        return {
            "execution_mode": "LIVE",
            "variant_id": self.variant_id,
            "combo": self.combo,
            "was_enabled": was_enabled,
            "enabled": self.config.enabled,
            "cancel_all": cancel_result,
            "settings": self.settings_payload(),
        }

    def run_from_state(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        if not self._run_lock.acquire(blocking=False):
            self.overlap_skip_count += 1
            self.last_error = "live runner busy; skipped overlapping tick"
            return
        try:
            self._run_from_state_unlocked(market, price, quotes)
        finally:
            self._run_lock.release()

    def run_once_from_state(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        *,
        max_stake_dollars: float,
        disable_after: bool = True,
        reconcile_wait_seconds: float = 0.0,
        reconcile_poll_seconds: float = 1.0,
    ) -> dict[str, Any]:
        if max_stake_dollars < LIVE_MIN_USDC:
            raise ValueError(f"max_stake_dollars must be >= {LIVE_MIN_USDC}")
        if not self._run_lock.acquire(blocking=False):
            self.overlap_skip_count += 1
            self.last_error = "live runner busy; skipped one-shot live run"
            raise RuntimeError(self.last_error)
        enabled_before = bool(self.config.enabled)
        last_order_at_before = self.last_order_at or 0.0
        try:
            account = self.store.account()
            stake, stake_source = self._entry_stake_for_market(market, account)
            if stake > max_stake_dollars + LIVE_EPSILON:
                raise RuntimeError(
                    f"one-shot live stake {stake:.6f} exceeds max_stake_dollars {max_stake_dollars:.6f}"
                )
            if not self.config.enabled:
                self.update_settings({"enabled": True})
            if not self.config.enabled:
                raise RuntimeError(self.last_error or "live trading did not enable for one-shot run")
            self._run_from_state_unlocked(market, price, quotes)
            submitted = bool(self.last_order_at and self.last_order_at > last_order_at_before)
            order_id = self.last_order.get("order_id") if isinstance(self.last_order, dict) else None
            payload = {
                "execution_mode": "LIVE",
                "variant_id": self.variant_id,
                "combo": self.combo,
                "submitted": submitted,
                "enabled_before": enabled_before,
                "disabled_after": bool(disable_after),
                "max_stake_dollars": round(float(max_stake_dollars), 6),
                "stake": round(float(stake), 6),
                "stake_source": stake_source,
                "last_order_at": self.last_order_at,
                "last_order": dict(self.last_order or {}),
                "last_signal": dict(self.last_signal or {}),
                "last_error": self.last_error,
            }
            if reconcile_wait_seconds > 0 or order_id:
                payload["reconcile"] = self.wait_for_order_reconciliation(
                    str(order_id or ""),
                    timeout_seconds=reconcile_wait_seconds,
                    poll_seconds=reconcile_poll_seconds,
                )
            return payload
        finally:
            if disable_after and self.config.enabled:
                self.config = self.settings_store.save(replace(self.config, enabled=False).normalized())
                self._release_process_lock()
            self._run_lock.release()

    def wait_for_order_reconciliation(
        self,
        external_order_id: str | None,
        *,
        timeout_seconds: float,
        poll_seconds: float,
    ) -> dict[str, Any]:
        order_id = str(external_order_id or "").strip()
        timeout = max(0.0, min(120.0, float(timeout_seconds or 0.0)))
        poll = max(0.1, min(10.0, float(poll_seconds or 0.5)))
        payload: dict[str, Any] = {
            "external_order_id": order_id or None,
            "waited_seconds": 0.0,
            "timeout_seconds": timeout,
            "poll_seconds": poll,
            "attempts": 0,
            "settled": False,
            "order": None,
            "open_trades": self.open_trades(),
        }
        if not order_id:
            payload["status"] = "NO_ORDER_ID"
            return payload
        deadline = time.time() + timeout
        while True:
            payload["attempts"] += 1
            self._live_order_reconcile_next_at[order_id] = 0.0
            self._reconcile_live_orders(time.time())
            order = self.store.live_order_by_external_id(order_id)
            payload["order"] = _public_reconciled_order(order)
            payload["open_trades"] = self.open_trades()
            status = str((order or {}).get("status") or "")
            payload["status"] = status or "UNKNOWN"
            if status and status != STATUS_PENDING:
                payload["settled"] = True
                return payload
            now = time.time()
            payload["waited_seconds"] = round(max(0.0, timeout - max(0.0, deadline - now)), 3)
            if timeout <= 0 or now >= deadline:
                return payload
            time.sleep(min(poll, max(0.0, deadline - now)))

    def _run_from_state_unlocked(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        now = time.time()
        self.run_count += 1
        self.last_run_at = now
        self.store.upsert_round(market)
        self._save_price_tick(price, now)
        self._settle_due(price, now)
        self._reconcile_official_settlements(now)
        self._backfill_official_final_prices(now)
        self._reconcile_live_orders(now)
        if self.config.enabled and self.stop_win_enabled:
            stop_win_actions = self._run_live_stop_win(market, quotes, now)
            if stop_win_actions:
                return
        signal, _signal_price, price_selection = self._signal_from_state(market, price, quotes)
        self.last_signal = {
            "symbol": signal.symbol,
            "side": signal.side,
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "move_bps": signal.move_bps,
            "reason": signal.reason,
            "price_selection": price_selection,
        }
        if not self.config.enabled:
            return
        block_reason = self._entry_block_reason(market, signal)
        if block_reason:
            self._append_last_signal_reason(block_reason)
            return
        if signal.side not in {"Up", "Down"}:
            return
        quote = self._quote_with_depth(market, signal.side, quotes.get(signal.side) or {})
        limit_price = self._entry_limit_price(signal)
        account = self.store.account()
        stake, stake_source = self._entry_stake_for_market(market, account)
        wallet = self.client.wallet_state(
            required_cash=stake,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        if wallet.get("errors"):
            self._append_last_signal_reason(str(wallet["errors"][0]))
            return
        min_order_size = _float(quote.get("min_order_size"), 0.0)
        if min_order_size and stake + LIVE_EPSILON < min_order_size:
            self._append_last_signal_reason(f"实盘预算 {stake:.2f} 低于市场最小订单 {min_order_size:.2f}，跳过")
            return
        intent = TradeIntent(
            market=market,
            signal=replace(
                signal,
                reason=_append_reason(
                    signal.reason,
                    (
                        f"{self.entry_marker} live FAK stake locked to current market"
                        if stake_source == "current_market_open_trade"
                        else f"{self.entry_marker} live FAK"
                    ),
                ),
            ),
            stake_dollars=stake,
        )
        sweep = sweep_taker_buy_by_budget(
            quote,
            limit_price=limit_price,
            budget=stake,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
        )
        if sweep.shares < self.settings.min_ask_size:
            self._append_last_signal_reason("实盘 FAK 可成交份额不足，跳过真实下单")
            return
        disarm_reason = self._entry_disarmed_reason()
        if disarm_reason:
            self._append_last_signal_reason(disarm_reason)
            return
        geo_reason = self._geo_access_block_reason(force=True)
        if geo_reason:
            self._append_last_signal_reason(geo_reason)
            return
        open_orders_reason = self._official_open_orders_block_reason(force=True)
        if open_orders_reason:
            self._append_last_signal_reason(open_orders_reason)
            return
        quote, sweep, rest_book_reason = self._confirm_live_fak_quote(
            market=market,
            side=signal.side,
            local_quote=quote,
            local_sweep=sweep,
            limit_price=limit_price,
            stake=stake,
        )
        if rest_book_reason:
            self._append_last_signal_reason(rest_book_reason)
            return
        response = self.client.place_market_buy(
            token_id=market.up_token if signal.side == "Up" else market.down_token,
            amount=stake,
            max_price=limit_price,
            tick_size=str(quote.get("tick_size") or "0.01"),
            neg_risk=quote.get("neg_risk") if isinstance(quote.get("neg_risk"), bool) else None,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        self.last_order_at = time.time()
        self.last_order = _response_public_payload(response)
        self.last_order.update({"variant_id": self.variant_id, "combo": self.combo})
        client_order_id = _client_order_id()
        token_id = market.up_token if signal.side == "Up" else market.down_token
        pending_order_id: int | None = None
        if response.order_id:
            try:
                pending_order_id = self.store.record_external_order_rejection(
                    intent,
                    order_type=ORDER_TYPE_FAK,
                    status=STATUS_PENDING,
                    side=signal.side,
                    limit_price=limit_price,
                    requested_cash=stake,
                    reason=_append_reason(
                        intent.signal.reason,
                        f"LIVE FAK 已提交官方订单，等待本地成交确认: {response.status}",
                    ),
                    external_order_id=response.order_id,
                    client_order_id=client_order_id,
                    external_status=response.status,
                    raw_response=_json_dumps(response.raw),
                )
                self._live_order_reconcile_next_at[response.order_id] = (
                    time.time() + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - 官方已返回 order id，必须先停止新实盘单。
                self._disable_after_live_accounting_failure(response, exc)
                raise
        resolved_response = response
        if response.order_id and (
            not _response_indicates_fill(response) or not _response_has_fill_amounts(response)
        ):
            fetched = None
            try:
                fetched = self.client.fetch_order_state(
                    order_id=response.order_id,
                    side="BUY",
                    token_id=token_id,
                    condition_id=market.condition_id,
                    retry_count=self.config.retry_count,
                    retry_delay_ms=self.config.retry_delay_ms,
                )
            except Exception as exc:  # noqa: BLE001 - 本地已记录 PENDING，后续轮询继续收口。
                self.last_error = f"live order immediate reconcile failed {response.order_id}: {type(exc).__name__}: {exc}"
            if fetched is not None and fetched.status != "RECONCILE_ERROR" and (
                not _response_indicates_fill(response)
                or _response_has_fill_amounts(fetched)
                or _response_terminal_no_fill(fetched)
            ):
                resolved_response = fetched
                self.last_order = _response_public_payload(resolved_response)
                self.last_order.update({"variant_id": self.variant_id, "combo": self.combo})
        if (
            pending_order_id is not None
            and _response_indicates_fill(resolved_response)
            and not _response_has_fill_amounts(resolved_response)
        ):
            try:
                self.store.update_external_pending_order(
                    pending_order_id,
                    status=STATUS_PENDING,
                    external_status=resolved_response.status,
                    raw_response=_json_dumps(resolved_response.raw),
                    reason=f"LIVE FAK 成交状态缺少官方金额，等待 order/trade 回查: {resolved_response.status}",
                )
            except Exception as exc:  # noqa: BLE001
                self._disable_after_live_accounting_failure(resolved_response, exc)
                raise
            self._append_last_signal_reason(f"实盘成交状态缺少官方金额，等待官方确认: {resolved_response.status}")
            return
        if not resolved_response.success and not _response_indicates_fill(resolved_response):
            try:
                if pending_order_id is not None:
                    self.store.update_external_pending_order(
                        pending_order_id,
                        status=STATUS_REJECTED,
                        external_status=resolved_response.status,
                        raw_response=_json_dumps(resolved_response.raw),
                        reason=f"LIVE FAK rejected {resolved_response.error or resolved_response.status}",
                    )
                else:
                    self.store.record_external_order_rejection(
                        intent,
                        order_type=ORDER_TYPE_FAK,
                        status=STATUS_REJECTED,
                        side=signal.side,
                        limit_price=limit_price,
                        requested_cash=stake,
                        reason=_append_reason(intent.signal.reason, resolved_response.error or resolved_response.status),
                        external_order_id=resolved_response.order_id,
                        client_order_id=client_order_id,
                        external_status=resolved_response.status,
                        raw_response=_json_dumps(resolved_response.raw),
                    )
            except Exception as exc:  # noqa: BLE001
                self._disable_after_live_accounting_failure(resolved_response, exc)
                raise
            self.last_error = resolved_response.error or resolved_response.status
            return
        if not _response_indicates_fill(resolved_response):
            pending = bool(resolved_response.order_id) and not _response_terminal_no_fill(resolved_response)
            status = STATUS_PENDING if pending else STATUS_CANCELED
            try:
                if pending_order_id is not None:
                    self.store.update_external_pending_order(
                        pending_order_id,
                        status=status,
                        external_status=resolved_response.status,
                        raw_response=_json_dumps(resolved_response.raw),
                        reason=f"LIVE FAK 未确认成交: {resolved_response.status}",
                    )
                else:
                    self.store.record_external_order_rejection(
                        intent,
                        order_type=ORDER_TYPE_FAK,
                        status=status,
                        side=signal.side,
                        limit_price=limit_price,
                        requested_cash=stake,
                        reason=_append_reason(intent.signal.reason, f"LIVE FAK 未确认成交: {resolved_response.status}"),
                        external_order_id=resolved_response.order_id,
                        client_order_id=client_order_id,
                        external_status=resolved_response.status,
                        raw_response=_json_dumps(resolved_response.raw),
                    )
            except Exception as exc:  # noqa: BLE001
                self._disable_after_live_accounting_failure(resolved_response, exc)
                raise
            if pending and resolved_response.order_id:
                self._live_order_reconcile_next_at[resolved_response.order_id] = (
                    time.time() + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                )
            self._append_last_signal_reason(f"实盘下单成功但未确认成交: {resolved_response.status}")
            return
        sweep_fill = build_taker_buy_fill_from_sweep(
            intent,
            side=signal.side,
            order_type=ORDER_TYPE_FAK,
            status=STATUS_FILLED,
            limit_price=limit_price,
            sweep=sweep,
        )
        fill = _fill_from_response_or_sweep(
            sweep_fill,
            resolved_response,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
        )
        try:
            if pending_order_id is not None:
                self.store.fill_external_pending_order(
                    pending_order_id,
                    fill,
                    external_status=resolved_response.status,
                    raw_response=_json_dumps(resolved_response.raw),
                    reason_suffix=f"LIVE_ORDER {resolved_response.order_id or '-'}",
                )
                if resolved_response.order_id:
                    self._live_order_reconcile_next_at.pop(resolved_response.order_id, None)
            else:
                self.store.place_external_fill(
                    fill,
                    external_order_id=resolved_response.order_id,
                    client_order_id=client_order_id,
                    external_status=resolved_response.status,
                    raw_response=_json_dumps(resolved_response.raw),
                    reason_suffix=f"LIVE_ORDER {resolved_response.order_id or '-'}",
                )
        except Exception as exc:  # noqa: BLE001 - 官方已成交，本地失败时必须停机防重复下单。
            self._disable_after_live_accounting_failure(resolved_response, exc)
            raise
        self.last_error = None

    def _run_live_stop_win(
        self,
        market: MarketRound,
        quotes: dict[str, dict[str, Any]],
        now: float,
    ) -> list[dict[str, Any]]:
        take_profit_pct = _float(self.config.paper_stop_win_take_profit_pct, LIVE_PAPER_STOP_WIN_DISABLE_PCT)
        if take_profit_pct >= LIVE_PAPER_STOP_WIN_DISABLE_PCT - LIVE_EPSILON:
            return []
        if market.ends_at <= now:
            return []
        open_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        actions: list[dict[str, Any]] = []
        for row in open_rows:
            trade_id = int(row.get("id") or 0)
            if trade_id <= 0:
                continue
            if self.store.active_live_exit_order_for_trade(trade_id) is not None:
                continue
            opened_at = _float(row.get("opened_at"), 0.0)
            if now - opened_at < LIVE_PAPER_STOP_WIN_MIN_HOLD_SECONDS:
                continue
            next_check_at = self._stop_win_next_check_at.get(trade_id, 0.0)
            if now < next_check_at:
                continue
            seconds_left = max(0.0, market.ends_at - now)
            interval = (
                LIVE_PAPER_STOP_WIN_FINAL_CHECK_SECONDS
                if seconds_left <= LIVE_PAPER_STOP_WIN_FINAL_WINDOW_SECONDS
                else LIVE_PAPER_STOP_WIN_CHECK_SECONDS
            )
            self._stop_win_next_check_at[trade_id] = now + interval
            side = str(row.get("side") or "")
            if side not in {"Up", "Down"}:
                continue
            quote = self._quote_with_bid(market, side, quotes.get(side) or {})
            shares = _float(row.get("shares"), 0.0)
            stake = _float(row.get("stake"), 0.0)
            entry_price = _float(row.get("entry_price"), 0.0)
            if shares <= 0 or stake <= 0 or entry_price <= 0:
                continue
            max_profit = _paper_stop_win_max_profit(stake, shares)
            if max_profit <= LIVE_EPSILON:
                continue
            target_pnl = _paper_stop_win_target_pnl(stake, shares, take_profit_pct)
            trigger_bid = _paper_stop_win_trigger_bid(
                stake,
                shares,
                target_pnl,
                self.settings.paper_taker_fee_rate,
            )
            if trigger_bid is None:
                continue
            exit_fill = _paper_sell_sweep_from_bid_quote(quote, shares, trigger_bid)
            if exit_fill is None:
                continue
            expected_exit_price = _float(exit_fill.get("exit_price"), 0.0)
            expected_notional = _float(exit_fill.get("notional"), 0.0)
            expected_fee = taker_fee(shares, expected_exit_price, self.settings.paper_taker_fee_rate)
            expected_pnl = round(expected_notional - expected_fee - stake, 6)
            if expected_pnl + LIVE_EPSILON < target_pnl or expected_pnl <= LIVE_EPSILON:
                continue
            reason = (
                f"max_profit_pct {take_profit_pct:.2f}% "
                f"target_pnl {target_pnl:.6f}/{max_profit:.6f}, "
                f"trigger {trigger_bid:.4f}, expected sell {shares:.6f} @ {expected_exit_price:.4f}, "
                f"expected gross {expected_notional:.6f}, fee {expected_fee:.6f}, pnl {expected_pnl:.6f}"
            )
            try:
                result = self.sell_trade(
                    trade_id,
                    quotes,
                    marker=LIVE_STOP_WIN_MARKER,
                    min_price=trigger_bid,
                    reason_detail=reason,
                )
                self._stop_win_closed_rounds[market.round_id] = now
                self._stop_win_next_check_at.pop(trade_id, None)
                self.last_stop_win = {
                    "variant_id": self.variant_id,
                    "combo": self.combo,
                    "trade_id": trade_id,
                    "round_id": market.round_id,
                    "side": side,
                    "trigger_bid": trigger_bid,
                    "expected_pnl": expected_pnl,
                    "reason": reason,
                    "result": result.get("closed_trade"),
                    "order": result.get("order"),
                    "at": time.time(),
                }
                actions.append(self.last_stop_win)
            except Exception as exc:  # noqa: BLE001 - 自动止盈失败必须留仓并暴露原因。
                active_exit = self.store.active_live_exit_order_for_trade(trade_id)
                if active_exit is not None:
                    self._stop_win_closed_rounds[market.round_id] = now
                    self.last_stop_win = {
                        "variant_id": self.variant_id,
                        "combo": self.combo,
                        "trade_id": trade_id,
                        "round_id": market.round_id,
                        "side": side,
                        "trigger_bid": trigger_bid,
                        "expected_pnl": expected_pnl,
                        "reason": reason,
                        "pending_exit_order": dict(active_exit),
                        "error": str(exc),
                        "at": time.time(),
                    }
                    actions.append(self.last_stop_win)
                self.last_error = f"{LIVE_STOP_WIN_MARKER} 自动止盈失败 trade={trade_id}: {type(exc).__name__}: {exc}"
        return actions

    def sell_trade(
        self,
        trade_id: int,
        quotes: dict[str, dict[str, Any]],
        *,
        marker: str = LIVE_MANUAL_SELL_MARKER,
        min_price: float | None = None,
        reason_detail: str | None = None,
    ) -> dict[str, Any]:
        reason_marker = str(marker or LIVE_MANUAL_SELL_MARKER)
        reason_prefix = (
            f"{reason_marker} {str(reason_detail).strip()}"
            if reason_detail
            else reason_marker
        )
        temporary_process_lock = False
        if not self.process_lock.locked:
            lock_error = self._ensure_process_lock()
            if lock_error:
                raise RuntimeError(f"{lock_error}，停止实盘卖出")
            temporary_process_lock = not self.config.enabled
        try:
            row = next((item for item in self.store.open_trades() if int(item["id"]) == int(trade_id)), None)
            if row is None:
                raise ValueError("live trade not found or already closed")
            ends_at = _float_or_none(row.get("ends_at"))
            if ends_at is not None and ends_at <= time.time():
                raise RuntimeError("市场已结束，等待官方结算，禁止继续手动卖出")
            active_exit = self.store.active_live_exit_order_for_trade(int(trade_id))
            if active_exit is not None:
                raise RuntimeError(
                    "live sell already pending for trade "
                    f"{trade_id}: order {active_exit.get('id')} {active_exit.get('external_order_id') or ''}".strip()
                )
            market = _market_from_row(row)
            side = str(row.get("side") or "")
            quote = self._quote_with_bid(market, side, quotes.get(side) or {})
            bid = _float_or_none(quote.get("best_bid"))
            if bid is None or bid <= 0:
                raise ValueError(f"missing {side} bid for live sell")
            limit_price = round(max(0.01, min(0.99, _float(min_price, bid))), 4)
            if bid + LIVE_EPSILON < limit_price:
                raise RuntimeError(f"{side} bid {bid:.4f} 低于卖出保护价 {limit_price:.4f}，停止实盘卖出")
            shares = _float(row.get("shares"), 0.0)
            if shares <= 0:
                raise ValueError("live trade has no shares")
            token_id = market.up_token if side == "Up" else market.down_token
            client_order_id = _client_order_id()
            token_state = self.client.token_state(
                token_id=token_id,
                required_shares=shares,
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
            if token_state.get("errors"):
                reason = str(token_state["errors"][0])
                self.store.record_external_exit_order(
                    market,
                    trade_id=int(trade_id),
                    side=side,
                    status=STATUS_REJECTED,
                    limit_price=limit_price,
                    shares=0.0,
                    notional=0.0,
                    fee=0.0,
                    reason=f"{reason_prefix} token precheck rejected {reason}",
                    external_order_id=None,
                    client_order_id=client_order_id,
                    external_status="TOKEN_PRECHECK_FAILED",
                    raw_response=_json_dumps(token_state),
                )
                raise RuntimeError(reason)
            response = self.client.place_market_sell(
                token_id=token_id,
                shares=shares,
                min_price=limit_price,
                tick_size=str(quote.get("tick_size") or "0.01"),
                neg_risk=quote.get("neg_risk") if isinstance(quote.get("neg_risk"), bool) else None,
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
            self.last_order_at = time.time()
            self.last_order = _response_public_payload(response)
            self.last_order.update({"variant_id": self.variant_id, "combo": self.combo, "marker": reason_marker})
            pending_exit_order_id: int | None = None
            if response.order_id:
                try:
                    pending_exit_order_id = self.store.record_external_exit_order(
                        market,
                        trade_id=int(trade_id),
                        side=side,
                        status=STATUS_PENDING,
                        limit_price=limit_price,
                        shares=0.0,
                        notional=0.0,
                        fee=0.0,
                        reason=f"{reason_prefix} 已提交官方卖出订单，等待本地成交确认: {response.status}",
                        external_order_id=response.order_id,
                        client_order_id=client_order_id,
                        external_status=response.status,
                        raw_response=_json_dumps(response.raw),
                    )
                    self._live_order_reconcile_next_at[response.order_id] = (
                        time.time() + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                    )
                except Exception as exc:  # noqa: BLE001 - 官方已返回 order id，必须先停止新实盘动作。
                    self._disable_after_live_accounting_failure(response, exc)
                    raise
            resolved_response = response
            if response.order_id and _response_indicates_fill(response) and not _response_has_fill_amounts(response):
                fetched = None
                try:
                    fetched = self.client.fetch_order_state(
                        order_id=response.order_id,
                        side="SELL",
                        token_id=token_id,
                        condition_id=market.condition_id,
                        retry_count=self.config.retry_count,
                        retry_delay_ms=self.config.retry_delay_ms,
                    )
                except Exception as exc:  # noqa: BLE001 - 本地已记录 PENDING，后续轮询继续收口。
                    self.last_error = f"live sell immediate reconcile failed {response.order_id}: {type(exc).__name__}: {exc}"
                if fetched is not None and (_response_has_fill_amounts(fetched) or _response_terminal_no_fill(fetched)):
                    resolved_response = fetched
                    self.last_order = _response_public_payload(resolved_response)
                    self.last_order.update({"variant_id": self.variant_id, "combo": self.combo, "marker": reason_marker})
            if not resolved_response.success:
                try:
                    if pending_exit_order_id is not None:
                        self.store.update_external_pending_order(
                            pending_exit_order_id,
                            status=STATUS_REJECTED,
                            external_status=resolved_response.status,
                            raw_response=_json_dumps(resolved_response.raw),
                            reason=f"{reason_prefix} rejected {resolved_response.error or resolved_response.status}",
                        )
                    else:
                        self.store.record_external_exit_order(
                            market,
                            trade_id=int(trade_id),
                            side=side,
                            status=STATUS_REJECTED,
                            limit_price=limit_price,
                            shares=0.0,
                            notional=0.0,
                            fee=0.0,
                            reason=f"{reason_prefix} rejected {resolved_response.error or resolved_response.status}",
                            external_order_id=resolved_response.order_id,
                            client_order_id=client_order_id,
                            external_status=resolved_response.status,
                            raw_response=_json_dumps(resolved_response.raw),
                        )
                except Exception as exc:  # noqa: BLE001
                    self._disable_after_live_accounting_failure(resolved_response, exc)
                    raise
                raise RuntimeError(resolved_response.error or resolved_response.status)
            if not _response_indicates_fill(resolved_response):
                pending = bool(resolved_response.order_id) and not _response_terminal_no_fill(resolved_response)
                status = STATUS_PENDING if pending else STATUS_CANCELED
                try:
                    if pending_exit_order_id is not None:
                        self.store.update_external_pending_order(
                            pending_exit_order_id,
                            status=status,
                            external_status=resolved_response.status,
                            raw_response=_json_dumps(resolved_response.raw),
                            reason=f"{reason_prefix} not confirmed filled {resolved_response.status}",
                        )
                    else:
                        self.store.record_external_exit_order(
                            market,
                            trade_id=int(trade_id),
                            side=side,
                            status=status,
                            limit_price=limit_price,
                            shares=0.0,
                            notional=0.0,
                            fee=0.0,
                            reason=f"{reason_prefix} not confirmed filled {resolved_response.status}",
                            external_order_id=resolved_response.order_id,
                            client_order_id=client_order_id,
                            external_status=resolved_response.status,
                            raw_response=_json_dumps(resolved_response.raw),
                        )
                except Exception as exc:  # noqa: BLE001
                    self._disable_after_live_accounting_failure(resolved_response, exc)
                    raise
                if pending and resolved_response.order_id:
                    self._live_order_reconcile_next_at[resolved_response.order_id] = (
                        time.time() + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                    )
                raise RuntimeError(f"LIVE FAK sell 未确认成交: {resolved_response.status}")
            if not _response_has_fill_amounts(resolved_response):
                try:
                    if pending_exit_order_id is not None:
                        self.store.update_external_pending_order(
                            pending_exit_order_id,
                            status=STATUS_PENDING,
                            external_status=resolved_response.status,
                            raw_response=_json_dumps(resolved_response.raw),
                            reason=f"{reason_prefix} 成交状态缺少官方金额，等待 order/trade 回查: {resolved_response.status}",
                        )
                except Exception as exc:  # noqa: BLE001
                    self._disable_after_live_accounting_failure(resolved_response, exc)
                    raise
                raise RuntimeError(f"LIVE FAK sell 成交状态缺少官方金额，等待官方确认: {resolved_response.status}")
            sell_shares = (
                min(shares, _float(resolved_response.filled_shares, shares))
                if resolved_response.filled_shares
                else shares
            )
            exit_price = _float(resolved_response.avg_fill_price, bid) if resolved_response.avg_fill_price else bid
            notional = (
                _float(resolved_response.cash_spent, sell_shares * exit_price)
                if resolved_response.cash_spent
                else sell_shares * exit_price
            )
            sell_shares = round(max(0.0, sell_shares), 6)
            exit_price = round(max(0.01, min(0.99, exit_price)), 6)
            notional = round(max(0.0, notional), 6)
            if sell_shares <= 0 or notional <= 0:
                raise RuntimeError("LIVE FAK sell 成交金额为空，停止本地平仓")
            fee = taker_fee(sell_shares, exit_price, self.settings.paper_taker_fee_rate)
            try:
                if pending_exit_order_id is not None:
                    filled_order = self.store.fill_external_pending_exit_order(
                        pending_exit_order_id,
                        shares=sell_shares,
                        exit_price=exit_price,
                        notional=notional,
                        fee=fee,
                        external_status=resolved_response.status,
                        raw_response=_json_dumps(resolved_response.raw),
                        reason=f"{reason_prefix} filled {sell_shares:.6f} @ {exit_price:.4f}",
                    )
                    closed = (filled_order or {}).get("closed_trade")
                    if resolved_response.order_id:
                        self._live_order_reconcile_next_at.pop(resolved_response.order_id, None)
                else:
                    closed = self.store.close_trade_shares(
                        int(trade_id),
                        sell_shares,
                        exit_price,
                        time.time(),
                        f"{reason_prefix} FAK sell @ {exit_price:.4f}",
                        fee=fee,
                    )
                    self.store.record_external_exit_order(
                        market,
                        trade_id=int(closed["id"] if closed else trade_id),
                        side=side,
                        status=STATUS_FILLED,
                        limit_price=exit_price,
                        shares=sell_shares,
                        notional=notional,
                        fee=fee,
                        reason=f"{reason_prefix} filled {sell_shares:.6f} @ {exit_price:.4f}",
                        external_order_id=resolved_response.order_id,
                        client_order_id=client_order_id,
                        external_status=resolved_response.status,
                        raw_response=_json_dumps(resolved_response.raw),
                    )
            except Exception as exc:  # noqa: BLE001 - 官方卖单已提交，本地失败时必须停机防重复卖出。
                self._disable_after_live_accounting_failure(resolved_response, exc)
                raise
            return {"closed_trade": closed, "order": self.last_order, "settings": self.settings_payload()}
        finally:
            if temporary_process_lock:
                self._release_process_lock()

    def snapshot(self, *, refresh_external: bool = True) -> dict[str, Any]:
        order_summary = self.store.paper_order_summary("BTC")
        trade_summary = self.store.recent_trade_summary("BTC")
        metrics = self.store.metrics()
        readiness, open_orders = self._external_status_payload(refresh_external=refresh_external)
        if not refresh_external:
            self._start_status_refresh_if_needed()
        return {
            "enabled": self.config.enabled,
            "variant_id": self.variant_id,
            "combo": self.combo,
            "live_strategy_id": self.variant_id,
            "live_strategy": _live_strategy_meta(self.variant_id),
            "execution_mode": "LIVE",
            "db_path": str(self.store.db_path),
            "settings_path": str(self.settings.live_trading_settings_path),
            "process_lock_path": str(self.process_lock.path),
            "process_lock_acquired": self.process_lock.locked,
            "process_lock": self.process_lock.payload(),
            "run_count": self.run_count,
            "last_run_at": self.last_run_at,
            "overlap_skip_count": self.overlap_skip_count,
            "last_signal": dict(self.last_signal or {}),
            "last_error": self.last_error,
            "startup_rearmed": self.startup_rearmed,
            "startup_rearm_skipped_active_lock": self.startup_rearm_skipped_active_lock,
            "last_order_at": self.last_order_at,
            "last_order": dict(self.last_order or {}),
            "last_stop_win": dict(self.last_stop_win or {}),
            "readiness": readiness,
            "open_orders": open_orders,
            "settings": asdict(self.config),
            "live_strategy_options": _live_strategy_options_payload(self.settings),
            "settings_file": self.settings_store.file_status(self.config),
            "variant": self.variant_payload(metrics, trade_summary, order_summary),
            "variants": [self.variant_payload(metrics, trade_summary, order_summary)],
        }

    def _external_status_payload(self, *, refresh_external: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        if refresh_external:
            readiness = self.client.readiness(
                required_cash=self.config.stake_dollars,
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
            open_orders = self.client.open_orders_state(
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
            return readiness, open_orders
        return (
            self.client.cached_readiness(required_cash=self.config.stake_dollars),
            self.client.cached_open_orders_state(),
        )

    def _start_status_refresh_if_needed(self) -> bool:
        if not self._status_refresh_lock.acquire(blocking=False):
            return False

        def _worker() -> None:
            try:
                self._external_status_payload(refresh_external=True)
            finally:
                self._status_refresh_lock.release()

        self._status_refresh_thread = threading.Thread(
            target=_worker,
            name="polybot2other-live-status-refresh",
            daemon=True,
        )
        self._status_refresh_thread.start()
        return True

    def gate_status(
        self,
        market: MarketRound | None,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        *,
        readiness: dict[str, Any] | None = None,
        official_open_orders: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        checks: list[dict[str, Any]] = []
        readiness_payload = readiness if isinstance(readiness, dict) else self.client.readiness(
            required_cash=self.config.stake_dollars,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        open_orders_payload = (
            official_open_orders
            if isinstance(official_open_orders, dict)
            else self.client.open_orders_state(
                retry_count=self.config.retry_count,
                retry_delay_ms=self.config.retry_delay_ms,
            )
        )
        account = self.store.account()
        metrics = self.store.metrics()
        daily_pnl = self.store.daily_realized_pnl()
        open_trade_count = self.store.open_trade_count("BTC")
        payload: dict[str, Any] = {
            "checked_at": now,
            "variant_id": self.variant_id,
            "combo": self.combo,
            "live_strategy_id": self.variant_id,
            "live_strategy": _live_strategy_meta(self.variant_id),
            "execution_mode": "LIVE",
            "checks": checks,
            "enabled": self.config.enabled,
            "can_place_order": False,
            "overall_status": "BLOCKED",
            "overall_label": "已阻断",
            "primary_blocker": None,
            "primary_message": "",
            "next_action": "",
            "metrics": {
                "daily_realized_pnl": round(float(daily_pnl), 6),
                "daily_loss_limit": round(float(self.config.max_daily_loss), 6),
                "total_pnl": round(_float(metrics.get("total_pnl"), 0.0), 6),
                "total_drawdown_limit": round(float(self.config.max_total_drawdown), 6),
                "open_trades": open_trade_count,
                "max_open_trades": self.config.max_open_trades,
                "cash_balance": round(float(account["cash_balance"]), 6),
                "configured_stake": round(float(self.config.stake_dollars), 6),
            },
        }

        checks.append(_gate_check("runtime", "PASS", "live runner 已加载"))
        checks.append(
            _gate_check(
                "enabled",
                "PASS" if self.config.enabled else "BLOCK",
                "实盘开关已开启" if self.config.enabled else "实盘开关当前关闭",
                current="ON" if self.config.enabled else "OFF",
                required="ON",
            )
        )
        process_lock_ok = (not self.config.enabled) or self.process_lock.locked
        checks.append(
            _gate_check(
                "process_lock",
                "PASS" if process_lock_ok else "BLOCK",
                "实盘进程锁已持有" if self.process_lock.locked else (
                    "实盘关闭，无需持有进程锁" if not self.config.enabled else "实盘进程锁未持有"
                ),
                current="locked" if self.process_lock.locked else "unlocked",
                required="locked when enabled",
                path=str(self.process_lock.path),
            )
        )
        checks.append(
            _gate_check(
                "compliance_acknowledged",
                "PASS" if self.config.compliance_acknowledged else "BLOCK",
                "风险确认已勾选" if self.config.compliance_acknowledged else "风险确认未勾选",
                current=bool(self.config.compliance_acknowledged),
                required=True,
            )
        )

        geo_check = readiness_payload.get("geo_check") if isinstance(readiness_payload.get("geo_check"), dict) else {}
        geo_reason = _geoblock_block_reason(geo_check) if geo_check else None
        checks.append(
            _gate_check(
                "geo_access",
                "PASS" if geo_reason is None else "BLOCK",
                "Polymarket 地区访问检查通过" if geo_reason is None else geo_reason,
                current=geo_check.get("country") or "-",
                required="not restricted",
                errors=list(geo_check.get("errors") or []) if geo_check else [],
            )
        )

        readiness_errors = list(readiness_payload.get("errors") or [])
        wallet = readiness_payload.get("wallet") if isinstance(readiness_payload.get("wallet"), dict) else {}
        wallet_errors = list(wallet.get("errors") or []) if wallet else []
        credential_errors = [item for item in readiness_errors if item not in wallet_errors and item != geo_reason]
        checks.append(
            _gate_check(
                "credentials",
                "PASS" if not credential_errors else "BLOCK",
                "SDK 和凭证格式检查通过" if not credential_errors else str(credential_errors[0]),
                errors=credential_errors,
            )
        )

        if market is None:
            checks.append(_gate_check("market", "BLOCK", "当前市场不可用"))
            return _finalize_gate_status(payload)

        payload["market"] = {
            "round_id": market.round_id,
            "target_price": market.target_price,
            "ends_at": market.ends_at,
            "seconds_left": round(max(0.0, market.ends_at - now), 3),
        }
        checks.append(_gate_check("market", "PASS", f"当前市场 {market.round_id}"))
        checks.append(
            _gate_check(
                "target_price",
                "PASS" if market.target_price > 0 else "BLOCK",
                f"官方目标价 {market.target_price:.2f}" if market.target_price > 0 else "缺少官方 market.target_price",
                current=round(float(market.target_price), 6),
                required="> 0",
            )
        )

        signal, _signal_price, price_selection = self._signal_from_state(market, price, quotes)
        payload["price_selection"] = price_selection
        payload["price_basis"] = price_selection.get("basis") or []
        signal_ok = signal.side in {"Up", "Down"}
        payload["signal"] = {
            "symbol": signal.symbol,
            "side": signal.side,
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "move_bps": signal.move_bps,
            "reason": signal.reason,
        }
        checks.append(
            _gate_check(
                "signal",
                "PASS" if signal_ok else "BLOCK",
                f"当前信号 {signal.side}" if signal_ok else f"当前策略不下单: {signal.side}",
                current=signal.side,
                required="Up/Down",
            )
        )

        source_name, source_age_ms = _gate_price_source(price, now)
        price_source_status = "BLOCK" if source_name is None else "WARN" if source_name == "fallback" else "PASS"
        price_source_message = (
            "当前价格源不可用"
            if source_name is None
            else (
                "当前价格源为 fallback，实盘风险较高"
                if source_name == "fallback"
                else f"当前价格源 {source_name or '-'}"
            )
        )
        if price_selection.get("mode") == "SELECTED":
            selected_source = str(price_selection.get("selected_source") or "")
            source_name = selected_source or source_name
            source_age_ms = _float_or_none(price_selection.get("selected_age_ms"))
            price_source_message = str(price_selection.get("message") or price_source_message)
            if price_selection.get("blocked"):
                price_source_status = "BLOCK"
            elif selected_source == LIVE_FALLBACK_SOURCE_CHAINLINK:
                price_source_status = "PASS"
            else:
                price_source_status = "WARN"
        checks.append(
            _gate_check(
                "price_source",
                price_source_status,
                price_source_message,
                current=source_name or "-",
                threshold="prefer Chainlink",
                age_ms=source_age_ms,
                fallback_sources=list(self.config.fallback_sources),
            )
        )

        daily_ok = daily_pnl > -abs(self.config.max_daily_loss)
        checks.append(
            _gate_check(
                "daily_loss",
                "PASS" if daily_ok else "BLOCK",
                (
                    f"单日已实现 PnL {daily_pnl:.4f}，未触发 {self.config.max_daily_loss:.2f} USDC 停止"
                    if daily_ok
                    else f"单日亏损 {daily_pnl:.4f} 已达到停止阈值 -{abs(self.config.max_daily_loss):.2f} USDC"
                ),
                current=round(float(daily_pnl), 6),
                threshold=-abs(self.config.max_daily_loss),
            )
        )
        total_pnl = _float(metrics.get("total_pnl"), 0.0)
        drawdown_ok = total_pnl > -abs(self.config.max_total_drawdown)
        checks.append(
            _gate_check(
                "total_drawdown",
                "PASS" if drawdown_ok else "BLOCK",
                (
                    f"总 PnL {total_pnl:.4f}，未触发 {self.config.max_total_drawdown:.2f} USDC 总回撤停止"
                    if drawdown_ok
                    else f"总回撤 {total_pnl:.4f} 已达到停止阈值 -{abs(self.config.max_total_drawdown):.2f} USDC"
                ),
                current=round(float(total_pnl), 6),
                threshold=-abs(self.config.max_total_drawdown),
            )
        )
        max_open_ok = open_trade_count < self.config.max_open_trades
        checks.append(
            _gate_check(
                "max_open_trades",
                "PASS" if max_open_ok else "BLOCK",
                (
                    f"当前持仓 {open_trade_count}/{self.config.max_open_trades}"
                    if max_open_ok
                    else f"实盘最大同时持仓 {self.config.max_open_trades} 笔已满"
                ),
                current=open_trade_count,
                threshold=self.config.max_open_trades,
            )
        )

        current_market_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        same_direction_open = bool(signal_ok and any(row.get("side") == signal.side for row in current_market_rows))
        checks.append(
            _gate_check(
                "duplicate_direction",
                "BLOCK" if same_direction_open else "PASS",
                (
                    f"当前市场已有 {signal.side} 持仓，跳过重复开仓"
                    if same_direction_open
                    else "当前市场无同方向持仓"
                ),
                current="yes" if same_direction_open else "no",
                required="no",
            )
        )
        active_same_side_order = bool(signal_ok and self.store.active_paper_order_exists(market.round_id, signal.side))
        active_entry_order = self.store.active_live_entry_order_exists_for_round(market.round_id)
        checks.append(
            _gate_check(
                "pending_entry_order",
                "BLOCK" if (active_same_side_order or active_entry_order) else "PASS",
                (
                    "当前市场已有待确认实盘买入订单"
                    if active_entry_order
                    else "已有同方向实盘订单，等待状态确认"
                    if active_same_side_order
                    else "当前市场无待确认买入订单"
                ),
                current="yes" if (active_same_side_order or active_entry_order) else "no",
                required="no",
            )
        )

        stake, stake_source = self._entry_stake_for_market(market, account)
        software_cash_ok = stake >= LIVE_MIN_USDC and float(account["cash_balance"]) >= LIVE_MIN_USDC
        checks.append(
            _gate_check(
                "software_cash",
                "PASS" if software_cash_ok else "BLOCK",
                (
                    f"软件隔离账户可用 {float(account['cash_balance']):.4f}，本次预算 {stake:.4f}"
                    if software_cash_ok
                    else "实盘隔离预算可用资金不足"
                ),
                current=round(float(account["cash_balance"]), 6),
                threshold=round(float(stake), 6),
                stake_source=stake_source,
            )
        )

        if signal_ok:
            quote = quotes.get(signal.side) if isinstance(quotes.get(signal.side), dict) else {}
            quote_age_ms = _gate_quote_age_ms(quote, now)
            max_age_ms = int(self.settings.max_quote_age_ms)
            if quote_age_ms is None:
                quote_status = "BLOCK"
                quote_message = f"缺少 {signal.side} 盘口时间戳"
            elif quote_age_ms > max_age_ms:
                quote_status = "BLOCK"
                quote_message = f"{signal.side} 盘口报价过期 {quote_age_ms:.0f}ms > {max_age_ms}ms"
            elif quote_age_ms > max_age_ms * 0.8:
                quote_status = "WARN"
                quote_message = f"{signal.side} 盘口接近过期 {quote_age_ms:.0f}ms / {max_age_ms}ms"
            else:
                quote_status = "PASS"
                quote_message = f"{signal.side} 盘口新鲜 {quote_age_ms:.0f}ms / {max_age_ms}ms"
            checks.append(
                _gate_check(
                    "quote_freshness",
                    quote_status,
                    quote_message,
                    current=round(quote_age_ms, 3) if quote_age_ms is not None else None,
                    threshold=max_age_ms,
                    side=signal.side,
                )
            )
            limit_price = self._entry_limit_price(signal)
            entry_price_ok = signal.entry_price <= self.config.max_entry_price + LIVE_EPSILON
            checks.append(
                _gate_check(
                    "max_entry_price",
                    "PASS" if entry_price_ok else "BLOCK",
                    (
                        f"买入价 {signal.entry_price:.4f} <= 最高买价 {self.config.max_entry_price:.4f}"
                        if entry_price_ok
                        else f"买入价 {signal.entry_price:.4f} 高于最高买价 {self.config.max_entry_price:.4f}"
                    ),
                    current=round(float(signal.entry_price), 6),
                    threshold=round(float(self.config.max_entry_price), 6),
                )
            )
            min_order_size = _float(quote.get("min_order_size"), 0.0)
            min_order_ok = not min_order_size or stake + LIVE_EPSILON >= min_order_size
            checks.append(
                _gate_check(
                    "min_order_size",
                    "PASS" if min_order_ok else "BLOCK",
                    (
                        "市场最小订单检查通过"
                        if min_order_ok
                        else f"实盘预算 {stake:.2f} 低于市场最小订单 {min_order_size:.2f}"
                    ),
                    current=round(float(stake), 6),
                    threshold=round(float(min_order_size), 6),
                )
            )
            sweep = sweep_taker_buy_by_budget(
                quote,
                limit_price=limit_price,
                budget=stake,
                taker_fee_rate=self.settings.paper_taker_fee_rate,
            )
            depth_ok = sweep.shares >= self.settings.min_ask_size
            checks.append(
                _gate_check(
                    "orderbook_depth",
                    "PASS" if depth_ok else "BLOCK",
                    (
                        f"FAK 可成交份额 {sweep.shares:.6f}"
                        if depth_ok
                        else "FAK 可成交份额不足"
                    ),
                    current=round(float(sweep.shares), 6),
                    threshold=round(float(self.settings.min_ask_size), 6),
                    limit_price=limit_price,
                    best_ask=quote.get("best_ask"),
                )
            )

        open_order_errors = list(open_orders_payload.get("errors") or [])
        open_orders_count = int(open_orders_payload.get("count") or 0)
        official_orders_ok = bool(open_orders_payload.get("ready")) and open_orders_count == 0 and not open_order_errors
        checks.append(
            _gate_check(
                "official_open_orders_clear",
                "PASS" if official_orders_ok else "BLOCK",
                (
                    "官方 CLOB open orders 为 0"
                    if official_orders_ok
                    else (
                        f"官方 CLOB 仍有 {open_orders_count} 笔 open orders，先刷新挂单或执行实盘急停"
                        if open_orders_count
                        else (open_order_errors[0] if open_order_errors else "官方 CLOB open orders 未确认")
                    )
                ),
                current=open_orders_count,
                required=0,
                errors=open_order_errors,
            )
        )
        checks.append(
            _gate_check(
                "collateral_wallet",
                "PASS" if wallet and not wallet_errors else "BLOCK",
                (
                    f"collateral balance {float(wallet.get('balance') or 0.0):.6f}, allowance {float(wallet.get('allowance') or 0.0):.6f}"
                    if wallet and not wallet_errors
                    else (wallet_errors[0] if wallet_errors else "collateral wallet 未检查")
                ),
                current=round(float(wallet.get("balance") or 0.0), 6) if wallet else None,
                threshold=round(float(stake), 6),
                errors=wallet_errors,
            )
        )
        return _finalize_gate_status(payload)

    def variant_payload(
        self,
        metrics: dict[str, Any] | None = None,
        trade_summary: dict[str, Any] | None = None,
        order_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "combo": self.combo,
            "strategy_family": self.variant.strategy_family,
            "order_type": self.variant.order_type,
            "single_entry_mode": self.variant.single_entry_mode,
            "signal_filter_mode": self.variant.signal_filter_mode,
            "target_code_completion": self.variant.target_code_completion,
            "target_report_alignment": self.variant.target_report_alignment,
            "role": self.variant.role,
            "account_scope": "live",
            "execution_mode": "LIVE",
            "db_path": str(self.store.db_path),
            "last_signal": dict(self.last_signal or {}),
            "last_error": self.last_error,
            "last_stop_win": dict(self.last_stop_win or {}),
            "active_orders": len(self.store.active_paper_orders("BTC")),
            "order_summary": order_summary or self.store.paper_order_summary("BTC"),
            "metrics": metrics or self.store.metrics(),
            "recent_trades_summary": trade_summary or self.store.recent_trade_summary("BTC"),
        }

    def orders_page(self, limit: int, offset: int, status_filter: str = "all") -> dict[str, Any]:
        status_key = normalize_paper_order_status_filter(status_filter)
        total = self.store.paper_order_count("BTC", status_key)
        rows = _tag_live_rows(
            self.store.recent_paper_orders(limit, offset, "BTC", status_key),
            variant_id=self.variant_id,
            combo=self.combo,
        )
        loaded = min(total, offset + len(rows))
        return {
            "recent_orders": rows,
            "recent_orders_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "status_filter": status_key,
            },
        }

    def open_trades(self) -> list[dict[str, Any]]:
        rows = _tag_live_rows(
            [row for row in self.store.open_trades() if row["symbol"] == "BTC"],
            variant_id=self.variant_id,
            combo=self.combo,
        )
        active_exit_by_trade_id = {
            int(row["trade_id"]): row
            for row in self.store.active_live_exit_orders("BTC")
            if row.get("trade_id") is not None
        }
        for row in rows:
            active_exit = active_exit_by_trade_id.get(int(row["id"]))
            if active_exit:
                row["pending_live_sell_order_id"] = active_exit.get("id")
                row["pending_live_sell_external_order_id"] = active_exit.get("external_order_id")
                row["pending_live_sell_status"] = active_exit.get("status")
        return rows

    def recent_trades_page(
        self,
        limit: int,
        offset: int,
        start_at: float | None,
        end_at: float | None,
    ) -> dict[str, Any]:
        total = self.store.recent_trade_count("BTC", start_at, end_at)
        rows = _tag_live_rows(
            self.store.recent_trades(limit, offset, "BTC", start_at, end_at),
            variant_id=self.variant_id,
            combo=self.combo,
        )
        loaded = min(total, offset + len(rows))
        return {
            "recent_trades": rows,
            "recent_trades_summary": self.store.recent_trade_summary("BTC", start_at, end_at),
            "recent_trades_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "start_at": start_at,
                "end_at": end_at,
            },
        }

    def equity_curve_window(self, days: int, max_points: int) -> dict[str, Any]:
        rows = self.store.equity_curve_window(days, max_points)
        return {
            "equity_curve": rows,
            "equity_curve_meta": {
                "account_scope": "live",
                "variant_id": self.variant_id,
                "combo": self.combo,
                "label": self.combo,
                "days": days,
                "max_points": max_points,
                "points": len(rows),
                "initial_balance": self.config.initial_balance,
            },
        }

    def apply_official_resolution(
        self,
        round_id: str,
        outcome: str,
        now: float,
        *,
        final_price: float | None = None,
        target_price: float | None = None,
    ) -> None:
        if outcome not in {"Up", "Down"}:
            return
        self.store.reconcile_round_official_outcome(
            round_id,
            outcome,
            now,
            final_price=final_price,
            target_price=target_price,
        )
        self.store.settle_round_outcome(
            round_id,
            outcome,
            now,
            final_price=final_price,
            target_price=target_price,
            settlement_source=SETTLEMENT_SOURCE_POLYMARKET,
        )

    def _entry_block_reason(self, market: MarketRound, signal: Signal, *, skip_readiness: bool = False) -> str | None:
        if signal.side not in {"Up", "Down"}:
            return None
        if self.config.enabled and not self.process_lock.locked:
            return "实盘进程锁未持有，停止真实下单"
        if not self.config.compliance_acknowledged:
            return "实盘风险确认未完成，停止真实下单"
        if not skip_readiness:
            readiness_errors = self.client.readiness_errors()
            if readiness_errors:
                return readiness_errors[0]
        if market.target_price <= 0:
            return "实盘缺少官方 market.target_price，停止真实下单"
        if self.store.daily_realized_pnl() <= -abs(self.config.max_daily_loss):
            return f"实盘单日亏损达到 {self.config.max_daily_loss:.2f} USDC，停止开新仓"
        metrics = self.store.metrics()
        total_pnl = _float(metrics.get("total_pnl"), 0.0)
        if total_pnl <= -abs(self.config.max_total_drawdown):
            return f"实盘总回撤达到 {self.config.max_total_drawdown:.2f} USDC，停止开新仓"
        round_open_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        if self.stop_win_enabled and self._stop_win_closed_rounds.get(market.round_id):
            return f"{self.variant_id} 当前市场已触发止盈退出，跳过重新开仓"
        if any(row.get("side") == signal.side for row in round_open_rows):
            return f"{self.variant_id} 当前市场已有同方向持仓，跳过重复开仓"
        if self.store.active_paper_order_exists(market.round_id, signal.side):
            return f"{self.variant_id} 已有同方向实盘订单，等待状态确认"
        if self.store.active_live_entry_order_exists_for_round(market.round_id):
            return f"{self.variant_id} 当前市场已有待确认实盘买入订单，等待官方确认后再开新仓"
        if self.variant.signal_filter_mode in {
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC,
            SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC,
        }:
            sample_version = (
                "V11"
                if self.variant.signal_filter_mode == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC
                else "V10"
            )
            readiness = _live_aggressive_edge_readiness(self.settings, sample_version)
            if not readiness.get("eligible_for_live_review"):
                reasons = readiness.get("reasons") if isinstance(readiness.get("reasons"), list) else []
                detail = "；".join(str(item) for item in reasons[:3]) if reasons else str(readiness.get("label") or "样本准入未通过")
                return f"{self.variant_id} {sample_version} 样本准入未通过，停止真实下单: {detail}"
        if self.store.open_trade_count("BTC") >= self.config.max_open_trades:
            return f"实盘最大同时持仓 {self.config.max_open_trades} 笔已满"
        if float(self.store.account()["cash_balance"]) < LIVE_MIN_USDC:
            return "实盘隔离预算可用资金不足"
        return None

    def _entry_stake_for_market(self, market: MarketRound, account: dict[str, Any] | None = None) -> tuple[float, str]:
        cash_balance = float((account or self.store.account())["cash_balance"])
        configured_stake = float(self.config.stake_dollars)
        current_market_rows = sorted(
            [
                row
                for row in self.store.open_trades()
                if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
            ],
            key=lambda row: int(row.get("id") or 0),
        )
        current_market_stakes = [
            _float(row.get("stake"), 0.0)
            for row in current_market_rows
        ]
        locked_stakes = [stake for stake in current_market_stakes if stake > 0]
        if locked_stakes:
            return min(round(locked_stakes[0], 6), cash_balance), "current_market_open_trade"
        return min(configured_stake, cash_balance), "config"

    def _entry_disarmed_reason(self) -> str | None:
        if not self.config.enabled:
            return "实盘开关已关闭，停止真实下单"
        if not self.process_lock.locked:
            return "实盘进程锁未持有，停止真实下单"
        if not self.config.compliance_acknowledged:
            return "实盘风险确认已撤销，停止真实下单"
        return None

    def _live_strategy_switch_block_reason(self, current_strategy_id: str, next_strategy_id: str) -> str | None:
        current = _normalize_live_strategy_id(current_strategy_id)
        next_id = _normalize_live_strategy_id(next_strategy_id)
        if current == next_id:
            return None
        if self.config.enabled:
            return "实盘开启中禁止切换实盘策略，请先关闭实盘"
        if self.process_lock.locked:
            return "实盘进程锁仍被当前 runner 持有，禁止切换实盘策略"
        current_block = self._strategy_store_block_reason(self.store, current, "当前策略")
        if current_block:
            return current_block
        target_path = _live_strategy_db_path(self.settings, next_id)
        if target_path != self.store.db_path and target_path.exists():
            target_store = TradeStore(target_path, self.config.initial_balance)
            try:
                target_block = self._strategy_store_block_reason(target_store, next_id, "目标策略")
                if target_block:
                    return target_block
            finally:
                try:
                    target_store.conn.close()
                except Exception:
                    pass
        return None

    def _strategy_store_block_reason(self, store: TradeStore, strategy_id: str, label: str) -> str | None:
        open_count = store.open_trade_count("BTC")
        if open_count > 0:
            return f"{label} {strategy_id} 仍有 {open_count} 笔实盘持仓未结束，禁止切换实盘策略"
        pending_orders = store.pending_external_orders(1, "BTC")
        if pending_orders:
            return f"{label} {strategy_id} 仍有实盘订单等待官方确认，禁止切换实盘策略"
        active_orders = [
            row
            for row in store.active_paper_orders("BTC")
            if row.get("execution_mode") == "LIVE"
        ]
        if active_orders:
            return f"{label} {strategy_id} 仍有活跃实盘订单，禁止切换实盘策略"
        return None

    def _enable_block_reason(self) -> str | None:
        if not self.config.compliance_acknowledged:
            return "未确认地区、密钥和真实资金风险，实盘开关保持关闭"
        geo_reason = self._geo_access_block_reason(force=True)
        if geo_reason:
            return f"{geo_reason}，实盘开关保持关闭"
        account = self.store.account()
        software_cash = float(account["cash_balance"])
        if software_cash < LIVE_MIN_USDC:
            return "实盘隔离预算可用资金不足，实盘开关保持关闭"
        lock_error = self._ensure_process_lock()
        if lock_error:
            return lock_error
        required_cash = min(self.config.stake_dollars, software_cash)
        readiness = self.client.readiness(
            required_cash=required_cash,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        if not readiness.get("ready"):
            errors = readiness.get("errors") if isinstance(readiness.get("errors"), list) else []
            first_error = str(errors[0]) if errors else "实盘 readiness 未通过"
            return f"{first_error}，实盘开关保持关闭"
        open_orders_reason = self._official_open_orders_block_reason(force=True)
        if open_orders_reason:
            return f"{open_orders_reason}，实盘开关保持关闭"
        return None

    def _ensure_process_lock(self) -> str | None:
        return self.process_lock.acquire()

    def _release_process_lock(self) -> None:
        self.process_lock.release()

    def _disable_after_live_accounting_failure(self, response: LiveOrderResponse, exc: Exception) -> None:
        order_ref = response.order_id or response.raw.get("signed_order_hash") or "-"
        self.config = self.settings_store.save(replace(self.config, enabled=False).normalized())
        self._release_process_lock()
        self.last_error = (
            "CRITICAL live order may have reached Polymarket but local accounting failed; "
            f"disabled live trading. order={order_ref} status={response.status} "
            f"error={type(exc).__name__}: {exc}"
        )

    def _geo_access_block_reason(self, *, force: bool = False) -> str | None:
        return _geoblock_block_reason(self.client.geoblock_state(force=force))

    def _official_open_orders_block_reason(self, *, force: bool = False) -> str | None:
        state = self.client.open_orders_state(
            force=force,
            retry_count=self.config.retry_count,
            retry_delay_ms=self.config.retry_delay_ms,
        )
        errors = list(state.get("errors") or [])
        if errors:
            return str(errors[0])
        if not state.get("ready"):
            return "官方 CLOB open orders 状态未确认"
        count = int(state.get("count") or 0)
        if count > 0:
            return f"官方 CLOB 仍有 {count} 笔 open orders，先刷新挂单或执行实盘急停"
        return None

    def _entry_limit_price(self, signal: Signal) -> float:
        edge_preserving_limit = signal.confidence - self.settings.min_edge
        limit_price = max(signal.entry_price, edge_preserving_limit)
        return round(min(self.config.max_entry_price, limit_price), 4)

    def _quote_with_depth(self, market: MarketRound, side: str, quote: dict[str, Any]) -> dict[str, Any]:
        if isinstance(quote.get("asks"), list) and quote.get("asks"):
            return quote
        token_id = market.up_token if side == "Up" else market.down_token if side == "Down" else ""
        if not token_id:
            return quote
        try:
            return self.polymarket.get_quote(token_id, side).to_dict()
        except Exception:  # noqa: BLE001 - 缺盘口时由下单前置检查阻断。
            return quote

    def _confirm_live_fak_quote(
        self,
        *,
        market: MarketRound,
        side: str,
        local_quote: dict[str, Any],
        local_sweep: Any,
        limit_price: float,
        stake: float,
    ) -> tuple[dict[str, Any], Any, str | None]:
        """真实 FAK 下单前用官方 REST book 复核深度，降低 WS 盘口瞬时失配导致的拒单。"""

        if not self._should_confirm_live_fak_quote(local_quote):
            return local_quote, local_sweep, None
        started = time.perf_counter()
        try:
            rest_quotes = self.polymarket.get_quotes(market)
            rest_quote = _quote_payload(rest_quotes.get(side))
        except Exception as exc:  # noqa: BLE001 - 实盘下单前复核失败时要保守跳过。
            latency_ms = _elapsed_ms(started)
            self._record_fak_quote_check(
                {
                    "status": "REST_ERROR",
                    "side": side,
                    "latency_ms": latency_ms,
                    "error": f"{type(exc).__name__}: {exc}",
                    "local": _quote_depth_payload(local_quote, local_sweep),
                }
            )
            logger.warning(
                "实盘 FAK 官方盘口二次确认失败 market=%s side=%s latency_ms=%.3f error=%s",
                market.round_id,
                side,
                latency_ms,
                exc,
            )
            return local_quote, local_sweep, "实盘官方盘口二次确认失败，跳过真实下单"

        latency_ms = _elapsed_ms(started)
        if not rest_quote:
            self._record_fak_quote_check(
                {
                    "status": "REST_EMPTY",
                    "side": side,
                    "latency_ms": latency_ms,
                    "local": _quote_depth_payload(local_quote, local_sweep),
                }
            )
            logger.warning(
                "实盘 FAK 官方盘口二次确认缺少目标侧盘口 market=%s side=%s latency_ms=%.3f",
                market.round_id,
                side,
                latency_ms,
            )
            return local_quote, local_sweep, "实盘官方盘口二次确认缺少目标侧盘口，跳过真实下单"

        rest_sweep = sweep_taker_buy_by_budget(
            rest_quote,
            limit_price=limit_price,
            budget=stake,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
        )
        depth_ok = rest_sweep.shares >= self.settings.min_ask_size
        self._record_fak_quote_check(
            {
                "status": "PASS" if depth_ok else "REST_DEPTH_BLOCK",
                "side": side,
                "latency_ms": latency_ms,
                "limit_price": limit_price,
                "stake": stake,
                "min_ask_size": self.settings.min_ask_size,
                "local": _quote_depth_payload(local_quote, local_sweep),
                "rest": _quote_depth_payload(rest_quote, rest_sweep),
            }
        )
        if not depth_ok:
            logger.info(
                "实盘 FAK 官方盘口深度不足 market=%s side=%s local_shares=%.6f rest_shares=%.6f "
                "limit_price=%.4f latency_ms=%.3f",
                market.round_id,
                side,
                _float(getattr(local_sweep, "shares", 0.0), 0.0),
                rest_sweep.shares,
                limit_price,
                latency_ms,
            )
            return rest_quote, rest_sweep, "实盘 REST FAK 可成交份额不足，跳过真实下单"
        return rest_quote, rest_sweep, None

    def _should_confirm_live_fak_quote(self, quote: dict[str, Any]) -> bool:
        if bool(getattr(self, "_force_rest_fak_confirmation", False)):
            return True
        if not isinstance(self.client, PolymarketLiveClient):
            return False
        source = str(quote.get("source") or "")
        if source.startswith("clob-ws") or source in {"browser-ws", "rest"}:
            return True
        return bool(quote.get("clob_received_ms") or quote.get("clob_event_updated_ms"))

    def _record_fak_quote_check(self, payload: dict[str, Any]) -> None:
        signal = dict(self.last_signal or {})
        signal["fak_quote_check"] = payload
        self.last_signal = signal

    def _quote_with_bid(self, market: MarketRound, side: str, quote: dict[str, Any]) -> dict[str, Any]:
        if _float_or_none(quote.get("best_bid")) is not None:
            return quote
        token_id = market.up_token if side == "Up" else market.down_token if side == "Down" else ""
        if not token_id:
            return quote
        try:
            return self.polymarket.get_quote(token_id, side).to_dict()
        except Exception:  # noqa: BLE001
            return quote

    def _save_price_tick(self, price: dict[str, Any], now: float) -> None:
        chainlink = _float_or_none(price.get("chainlink"))
        binance = _float_or_none(price.get("binance"))
        if chainlink:
            self.store.save_price_tick(
                "BTC",
                chainlink,
                "live-chainlink",
                _updated_at_seconds(price.get("chainlink_updated_ms"), now),
            )
        elif binance:
            self.store.save_price_tick("BTC", binance, "live-binance", now)

    def _settle_due(self, price: dict[str, Any], now: float) -> None:
        due_slugs = sorted(
            {
                row["round_id"]
                for row in self.store.open_trades()
                if row["symbol"] == "BTC" and row["ends_at"] <= now
            }
        )
        for slug in due_slugs:
            try:
                resolution = self.polymarket.get_resolution(slug)
            except Exception:  # noqa: BLE001
                resolution = None
            if isinstance(resolution, dict) and resolution.get("outcome") in {"Up", "Down"}:
                final_price = _float_or_none(resolution.get("final_price"))
                target_price = _float_or_none(resolution.get("target_price"))
                self.apply_official_resolution(
                    slug,
                    str(resolution["outcome"]),
                    now,
                    final_price=final_price,
                    target_price=target_price,
                )
                if final_price is None:
                    self._official_price_backfill_next_at[slug] = now + LIVE_PRICE_BACKFILL_INTERVAL_SECONDS
        chainlink_price = _float_or_none(price.get("chainlink"))
        if chainlink_price:
            settled = self.store.settle_due_rounds({"BTC": chainlink_price}, now)
            for row in settled:
                round_id = str(row.get("round_id") or "")
                if round_id:
                    self._official_recheck_next_at.setdefault(
                        round_id,
                        now + LIVE_OFFICIAL_RECHECK_INTERVAL_SECONDS,
                    )

    def _reconcile_official_settlements(self, now: float) -> None:
        try:
            candidates = self.store.official_recheck_candidates(
                now,
                LIVE_OFFICIAL_RECHECK_WINDOW_SECONDS,
                LIVE_OFFICIAL_RECHECK_LIMIT,
                "BTC",
            )
        except Exception:  # noqa: BLE001
            return
        for row in candidates:
            round_id = str(row.get("round_id") or "")
            if not round_id or self._official_recheck_next_at.get(round_id, 0.0) > now:
                continue
            try:
                resolution = self.polymarket.get_resolution(round_id)
                outcome = resolution.get("outcome") if isinstance(resolution, dict) else None
                if outcome in {"Up", "Down"}:
                    final_price = _float_or_none(resolution.get("final_price"))
                    target_price = _float_or_none(resolution.get("target_price"))
                    self.store.reconcile_round_official_outcome(
                        round_id,
                        str(outcome),
                        now,
                        final_price=final_price,
                        target_price=target_price,
                    )
                    self._official_recheck_next_at.pop(round_id, None)
                else:
                    self._official_recheck_next_at[round_id] = now + LIVE_OFFICIAL_RECHECK_INTERVAL_SECONDS
            except Exception:  # noqa: BLE001
                self._official_recheck_next_at[round_id] = now + LIVE_OFFICIAL_RECHECK_INTERVAL_SECONDS

    def _backfill_official_final_prices(self, now: float) -> None:
        try:
            candidates = self.store.official_final_price_candidates(
                now,
                LIVE_PRICE_BACKFILL_WINDOW_SECONDS,
                LIVE_PRICE_BACKFILL_LIMIT,
                "BTC",
            )
        except Exception:  # noqa: BLE001
            return
        for row in candidates:
            round_id = str(row.get("round_id") or "")
            if not round_id or self._official_price_backfill_next_at.get(round_id, 0.0) > now:
                continue
            try:
                resolution = self.polymarket.get_resolution(round_id)
                if not isinstance(resolution, dict):
                    self._official_price_backfill_next_at[round_id] = now + LIVE_PRICE_BACKFILL_INTERVAL_SECONDS
                    continue
                outcome = resolution.get("outcome") or row.get("outcome")
                final_price = _float_or_none(resolution.get("final_price"))
                target_price = _float_or_none(resolution.get("target_price"))
                if outcome in {"Up", "Down"} and (final_price is not None or target_price is not None):
                    local_final_price = _float_or_none(row.get("final_price"))
                    if (
                        final_price is not None
                        and local_final_price is not None
                        and abs(final_price - local_final_price) > 0.000001
                    ):
                        logger.debug(
                            "实盘官方最终价回填更新 round_id=%s local_final=%s official_final=%s local_target=%s official_target=%s",
                            round_id,
                            local_final_price,
                            final_price,
                            row.get("target_price"),
                            target_price,
                        )
                    self.store.reconcile_round_official_outcome(
                        round_id,
                        str(outcome),
                        now,
                        final_price=final_price,
                        target_price=target_price,
                    )
                if final_price is not None:
                    self._official_price_backfill_next_at.pop(round_id, None)
                else:
                    self._official_price_backfill_next_at[round_id] = now + LIVE_PRICE_BACKFILL_INTERVAL_SECONDS
            except Exception:  # noqa: BLE001
                self._official_price_backfill_next_at[round_id] = now + LIVE_PRICE_BACKFILL_INTERVAL_SECONDS

    def _reconcile_live_orders(self, now: float) -> None:
        try:
            rows = self.store.pending_external_orders(LIVE_ORDER_RECONCILE_LIMIT, "BTC")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"live order reconcile load failed: {type(exc).__name__}: {exc}"
            return
        for row in rows:
            external_order_id = str(row.get("external_order_id") or "")
            if not external_order_id:
                continue
            if self._live_order_reconcile_next_at.get(external_order_id, 0.0) > now:
                continue
            created_at = _float(row.get("created_at"), 0.0)
            if created_at and now - created_at > LIVE_PENDING_ORDER_MAX_AGE_SECONDS:
                self.store.update_external_pending_order(
                    int(row["id"]),
                    status=STATUS_CANCELED,
                    external_status="LOCAL_PENDING_TIMEOUT",
                    raw_response=_json_dumps({"timeout_seconds": LIVE_PENDING_ORDER_MAX_AGE_SECONDS, "row": row}),
                    reason=f"LIVE_RECONCILED local pending timeout {LIVE_PENDING_ORDER_MAX_AGE_SECONDS:.0f}s",
                )
                self._live_order_reconcile_next_at.pop(external_order_id, None)
                continue
            side = str(row.get("side") or "")
            token_id = str(row.get("up_token") if side == "Up" else row.get("down_token") or "")
            order_side = "SELL" if str(row.get("order_type") or "") == "FAK_SELL" else "BUY"
            try:
                response = self.client.fetch_order_state(
                    order_id=external_order_id,
                    side=order_side,
                    token_id=token_id,
                    condition_id=str(row.get("condition_id") or "") or None,
                    retry_count=self.config.retry_count,
                    retry_delay_ms=self.config.retry_delay_ms,
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"live order reconcile failed {external_order_id}: {type(exc).__name__}: {exc}"
                self._live_order_reconcile_next_at[external_order_id] = now + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                continue
            if response is None:
                self._live_order_reconcile_next_at[external_order_id] = now + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                continue
            if str(row.get("order_type") or "") == "FAK_SELL":
                if _response_indicates_fill(response):
                    exit_fill = _exit_fill_from_response(
                        response,
                        taker_fee_rate=self.settings.paper_taker_fee_rate,
                    )
                    if exit_fill is None:
                        self._live_order_reconcile_next_at[external_order_id] = now + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                        self.last_error = f"live exit order reconcile missing fill amounts {external_order_id}"
                        continue
                    self.store.fill_external_pending_exit_order(
                        int(row["id"]),
                        shares=exit_fill["shares"],
                        exit_price=exit_fill["exit_price"],
                        notional=exit_fill["notional"],
                        fee=exit_fill["fee"],
                        external_status=response.status,
                        raw_response=_json_dumps(response.raw),
                        reason=f"LIVE_RECONCILED exit fill {external_order_id}",
                    )
                    self._live_order_reconcile_next_at.pop(external_order_id, None)
                    self.last_error = None
                    continue
                terminal_status = _response_terminal_no_fill_local_status(response)
                if terminal_status is not None:
                    self.store.update_external_pending_order(
                        int(row["id"]),
                        status=terminal_status,
                        external_status=response.status,
                        raw_response=_json_dumps(response.raw),
                        reason=f"LIVE_RECONCILED exit no fill {response.status}",
                    )
                    self._live_order_reconcile_next_at.pop(external_order_id, None)
                    continue
                self.store.update_external_pending_order(
                    int(row["id"]),
                    status=STATUS_PENDING,
                    external_status=response.status,
                    raw_response=_json_dumps(response.raw),
                    reason=f"LIVE_RECONCILE exit pending {response.status}",
                )
                self._live_order_reconcile_next_at[external_order_id] = now + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                continue
            if _response_indicates_fill(response):
                fill = _fill_from_response_row(
                    row,
                    response,
                    taker_fee_rate=self.settings.paper_taker_fee_rate,
                )
                if fill is None:
                    self._live_order_reconcile_next_at[external_order_id] = now + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS
                    self.last_error = f"live order reconcile missing fill amounts {external_order_id}"
                    continue
                self.store.fill_external_pending_order(
                    int(row["id"]),
                    fill,
                    external_status=response.status,
                    raw_response=_json_dumps(response.raw),
                    reason_suffix=f"LIVE_RECONCILED {external_order_id}",
                )
                self._live_order_reconcile_next_at.pop(external_order_id, None)
                self.last_error = None
                continue
            terminal_status = _response_terminal_no_fill_local_status(response)
            if terminal_status is not None:
                self.store.update_external_pending_order(
                    int(row["id"]),
                    status=terminal_status,
                    external_status=response.status,
                    raw_response=_json_dumps(response.raw),
                    reason=f"LIVE_RECONCILED no fill {response.status}",
                )
                self._live_order_reconcile_next_at.pop(external_order_id, None)
                continue
            self.store.update_external_pending_order(
                int(row["id"]),
                status=STATUS_PENDING,
                external_status=response.status,
                raw_response=_json_dumps(response.raw),
                reason=f"LIVE_RECONCILE pending {response.status}",
            )
            self._live_order_reconcile_next_at[external_order_id] = now + LIVE_ORDER_RECONCILE_INTERVAL_SECONDS

    def _append_last_signal_reason(self, reason: str) -> None:
        signal = dict(self.last_signal or {})
        existing = str(signal.get("reason") or "")
        signal["reason"] = _append_reason(existing, reason)
        self.last_signal = signal


class LivePaperStrategyRunner(LiveStrategyRunner):
    """实盘策略影子账户：复用实盘信号和风控，但只写本地 Paper 账本。"""

    def __init__(
        self,
        settings: Settings,
        polymarket: PolymarketClient,
        *,
        variant_id: str = LIVE_PAPER_VARIANT_ID,
        combo: str = LIVE_PAPER_COMBO,
        entry_marker: str = LIVE_PAPER_ENTRY_MARKER,
        role: str = "SINGLE_FAK_REAL 的 Paper 影子账户，不触发真实下单",
        db_path: Path | None = None,
        stop_win_enabled: bool = False,
    ) -> None:
        self.settings = settings
        self.polymarket = polymarket
        self.variant_id = variant_id
        self.combo = combo
        self.entry_marker = entry_marker
        self.stop_win_enabled = bool(stop_win_enabled)
        self.variant = StrategyVariant(
            self.variant_id,
            STRATEGY_FAMILY_SINGLE,
            ORDER_TYPE_FAK,
            "85%",
            "25%-35%",
            role,
            SINGLE_ENTRY_MODE_LEGACY,
        )
        self.settings_store = LiveSettingsStore(settings.live_trading_settings_path, self._default_config(settings))
        self.config = self.settings_store.load().normalized()
        self.store = TradeStore(db_path or _live_paper_db_path(settings), self.config.initial_balance)
        self.store.rebase_initial_balance(self.config.initial_balance)
        self.strategy = RealBtcFiveMinuteStrategy(settings)
        self.last_signal: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_order_at: float | None = None
        self.last_order: dict[str, Any] | None = None
        self.run_count = 0
        self.last_run_at: float | None = None
        self.overlap_skip_count = 0
        self.official_broadcast_count = 0
        self.last_official_broadcast_at: float | None = None
        self._official_recheck_next_at: dict[str, float] = {}
        self._official_price_backfill_next_at: dict[str, float] = {}
        self._stop_win_next_check_at: dict[int, float] = {}
        self._stop_win_closed_rounds: dict[str, float] = {}
        self.last_stop_win: dict[str, Any] | None = None
        self._run_lock = threading.Lock()

    def sync_config(self, config: LiveRuntimeConfig | None = None) -> LiveRuntimeConfig:
        next_config = (config or self.settings_store.load()).normalized()
        if next_config != self.config:
            self.config = next_config
            self.store.rebase_initial_balance(self.config.initial_balance)
        return self.config

    def run_from_state(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        *,
        live_config: LiveRuntimeConfig | None = None,
    ) -> None:
        if not self._run_lock.acquire(blocking=False):
            self.overlap_skip_count += 1
            self.last_error = "live paper runner busy; skipped overlapping tick"
            return
        try:
            self.sync_config(live_config)
            self._run_from_state_unlocked(market, price, quotes)
        finally:
            self._run_lock.release()

    def _run_from_state_unlocked(
        self,
        market: MarketRound,
        price: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
    ) -> None:
        now = time.time()
        self.run_count += 1
        self.last_run_at = now
        self.store.upsert_round(market)
        self._save_price_tick(price, now)
        self._settle_due(price, now)
        self._reconcile_official_settlements(now)
        self._backfill_official_final_prices(now)
        self._run_stop_win(market, quotes, now)
        signal, _signal_price, price_selection = self._signal_from_state(market, price, quotes)
        self.last_signal = {
            "symbol": signal.symbol,
            "side": signal.side,
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "move_bps": signal.move_bps,
            "reason": signal.reason,
            "price_selection": price_selection,
        }
        block_reason = self._paper_entry_block_reason(market, signal)
        if block_reason:
            self._append_last_signal_reason(block_reason)
            return
        if signal.side not in {"Up", "Down"}:
            return
        quote = self._quote_with_depth(market, signal.side, quotes.get(signal.side) or {})
        limit_price = self._entry_limit_price(signal)
        account = self.store.account()
        stake, stake_source = self._entry_stake_for_market(market, account)
        min_order_size = _float(quote.get("min_order_size"), 0.0)
        if min_order_size and stake + LIVE_EPSILON < min_order_size:
            self._append_last_signal_reason(f"实盘复刻 Paper 预算 {stake:.2f} 低于市场最小订单 {min_order_size:.2f}，跳过")
            return
        intent = TradeIntent(
            market=market,
            signal=replace(
                signal,
                reason=_append_reason(
                    signal.reason,
                    (
                        f"{self.entry_marker} paper FAK stake locked to current market"
                        if stake_source == "current_market_open_trade"
                        else f"{self.entry_marker} paper FAK"
                    ),
                ),
            ),
            stake_dollars=stake,
        )
        sweep = sweep_taker_buy_by_budget(
            quote,
            limit_price=limit_price,
            budget=stake,
            taker_fee_rate=self.settings.paper_taker_fee_rate,
        )
        if sweep.shares < self.settings.min_ask_size:
            self._append_last_signal_reason("实盘复刻 Paper FAK 可成交份额不足，跳过模拟下单")
            return
        fill = build_taker_buy_fill_from_sweep(
            intent,
            side=signal.side,
            order_type=ORDER_TYPE_FAK,
            status=STATUS_FILLED,
            limit_price=limit_price,
            sweep=sweep,
        )
        trade_ids = self.store.place_fills([fill])
        self.last_order_at = time.time()
        self.last_order = {
            "execution_mode": "PAPER",
            "variant_id": self.variant_id,
            "combo": self.combo,
            "status": fill.status,
            "side": fill.side,
            "limit_price": fill.limit_price,
            "filled_shares": fill.shares,
            "avg_fill_price": fill.fill_price,
            "cash_spent": fill.cash_spent,
            "trade_ids": trade_ids,
            "simulated": True,
        }
        self.last_error = None

    def _paper_entry_block_reason(self, market: MarketRound, signal: Signal) -> str | None:
        if signal.side not in {"Up", "Down"}:
            return None
        if market.target_price <= 0:
            return f"{self.variant_id} 缺少官方 market.target_price，停止模拟开仓"
        if self.stop_win_enabled and self._stop_win_closed_rounds.get(market.round_id):
            return f"{self.variant_id} 当前市场已触发止盈退出，跳过重新开仓"
        if self.store.daily_realized_pnl() <= -abs(self.config.max_daily_loss):
            return f"{self.variant_id} 单日亏损达到 {self.config.max_daily_loss:.2f} USDC，停止开新仓"
        metrics = self.store.metrics()
        total_pnl = _float(metrics.get("total_pnl"), 0.0)
        if total_pnl <= -abs(self.config.max_total_drawdown):
            return f"{self.variant_id} 总回撤达到 {self.config.max_total_drawdown:.2f} USDC，停止开新仓"
        round_open_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        if any(row.get("side") == signal.side for row in round_open_rows):
            return f"{self.variant_id} 当前市场已有同方向持仓，跳过重复开仓"
        if self.store.active_paper_order_exists(market.round_id, signal.side):
            return f"{self.variant_id} 已有同方向 Paper 订单，等待状态确认"
        if self.store.open_trade_count("BTC") >= self.config.max_open_trades:
            return f"{self.variant_id} 最大同时持仓 {self.config.max_open_trades} 笔已满"
        if float(self.store.account()["cash_balance"]) < LIVE_MIN_USDC:
            return f"{self.variant_id} 隔离预算可用资金不足"
        return None

    def _run_stop_win(self, market: MarketRound, quotes: dict[str, dict[str, Any]], now: float) -> list[dict[str, Any]]:
        if not self.stop_win_enabled:
            return []
        take_profit_pct = _float(self.config.paper_stop_win_take_profit_pct, LIVE_PAPER_STOP_WIN_DISABLE_PCT)
        if take_profit_pct >= LIVE_PAPER_STOP_WIN_DISABLE_PCT - LIVE_EPSILON:
            return []
        if market.ends_at <= now:
            return []
        open_rows = [
            row
            for row in self.store.open_trades()
            if row.get("symbol") == "BTC" and row.get("round_id") == market.round_id
        ]
        closed: list[dict[str, Any]] = []
        for row in open_rows:
            trade_id = int(row.get("id") or 0)
            if trade_id <= 0:
                continue
            opened_at = _float(row.get("opened_at"), 0.0)
            if now - opened_at < LIVE_PAPER_STOP_WIN_MIN_HOLD_SECONDS:
                continue
            next_check_at = self._stop_win_next_check_at.get(trade_id, 0.0)
            if now < next_check_at:
                continue
            seconds_left = max(0.0, market.ends_at - now)
            interval = (
                LIVE_PAPER_STOP_WIN_FINAL_CHECK_SECONDS
                if seconds_left <= LIVE_PAPER_STOP_WIN_FINAL_WINDOW_SECONDS
                else LIVE_PAPER_STOP_WIN_CHECK_SECONDS
            )
            self._stop_win_next_check_at[trade_id] = now + interval
            side = str(row.get("side") or "")
            if side not in {"Up", "Down"}:
                continue
            quote = self._quote_with_bid(market, side, quotes.get(side) or {})
            entry_price = _float(row.get("entry_price"), 0.0)
            shares = _float(row.get("shares"), 0.0)
            stake = _float(row.get("stake"), 0.0)
            if entry_price <= 0 or shares <= 0 or stake <= 0:
                continue
            max_profit = _paper_stop_win_max_profit(stake, shares)
            if max_profit <= LIVE_EPSILON:
                continue
            target_pnl = _paper_stop_win_target_pnl(stake, shares, take_profit_pct)
            trigger_bid = _paper_stop_win_trigger_bid(
                stake,
                shares,
                target_pnl,
                self.settings.paper_taker_fee_rate,
            )
            if trigger_bid is None:
                continue
            exit_fill = _paper_sell_sweep_from_bid_quote(quote, shares, trigger_bid)
            if exit_fill is None:
                continue
            exit_price = exit_fill["exit_price"]
            notional = exit_fill["notional"]
            fee = taker_fee(shares, exit_price, self.settings.paper_taker_fee_rate)
            expected_pnl = round(notional - fee - stake, 6)
            if expected_pnl + LIVE_EPSILON < target_pnl or expected_pnl <= LIVE_EPSILON:
                continue
            reason = (
                f"{LIVE_PAPER_STOP_WIN_MARKER} max_profit_pct {take_profit_pct:.2f}% "
                f"target_pnl {target_pnl:.6f}/{max_profit:.6f}, "
                f"trigger {trigger_bid:.4f}, sell {shares:.6f} @ {exit_price:.4f}, "
                f"gross {notional:.6f}, fee {fee:.6f}, pnl {expected_pnl:.6f}"
            )
            item = self.store.close_trade_shares(
                trade_id,
                shares,
                exit_price,
                now,
                reason,
                fee=fee,
            )
            if not item:
                continue
            self._stop_win_closed_rounds[market.round_id] = now
            self._stop_win_next_check_at.pop(trade_id, None)
            self.last_order_at = time.time()
            self.last_order = {
                "execution_mode": "PAPER",
                "variant_id": self.variant_id,
                "combo": self.combo,
                "status": STATUS_FILLED,
                "side": side,
                "order_type": "FAK_SELL",
                "limit_price": trigger_bid,
                "avg_fill_price": exit_price,
                "filled_shares": shares,
                "notional": notional,
                "fee": fee,
                "pnl": expected_pnl,
                "trade_ids": [int(item["id"])],
                "simulated": True,
                "reason": reason,
            }
            self.last_stop_win = dict(self.last_order)
            closed.append(item)
        return closed

    def snapshot(self) -> dict[str, Any]:
        order_summary = self.store.paper_order_summary("BTC")
        trade_summary = self.store.recent_trade_summary("BTC")
        metrics = self.store.metrics()
        settings_payload = asdict(self.config)
        settings_payload["paper_shadow_enabled"] = True
        settings_payload["mirrors_live_enabled"] = self.config.enabled
        settings_payload["paper_stop_win_enabled"] = self.stop_win_enabled
        settings_payload["paper_stop_win_disabled"] = not self.stop_win_enabled or (
            self.config.paper_stop_win_take_profit_pct >= LIVE_PAPER_STOP_WIN_DISABLE_PCT - LIVE_EPSILON
        )
        return {
            "enabled": True,
            "variant_id": self.variant_id,
            "combo": self.combo,
            "execution_mode": "PAPER",
            "account_scope": "live_paper",
            "mirrors_variant_id": LIVE_VARIANT_ID,
            "db_path": str(self.store.db_path),
            "settings_path": str(self.settings.live_trading_settings_path),
            "run_count": self.run_count,
            "last_run_at": self.last_run_at,
            "overlap_skip_count": self.overlap_skip_count,
            "official_broadcast_count": self.official_broadcast_count,
            "last_official_broadcast_at": self.last_official_broadcast_at,
            "last_signal": dict(self.last_signal or {}),
            "last_error": self.last_error,
            "last_order_at": self.last_order_at,
            "last_order": dict(self.last_order or {}),
            "last_stop_win": dict(self.last_stop_win or {}),
            "settings": settings_payload,
            "variant": self.variant_payload(metrics, trade_summary, order_summary),
            "variants": [self.variant_payload(metrics, trade_summary, order_summary)],
        }

    def variant_payload(
        self,
        metrics: dict[str, Any] | None = None,
        trade_summary: dict[str, Any] | None = None,
        order_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "combo": self.combo,
            "strategy_family": self.variant.strategy_family,
            "order_type": self.variant.order_type,
            "single_entry_mode": self.variant.single_entry_mode,
            "target_code_completion": self.variant.target_code_completion,
            "target_report_alignment": self.variant.target_report_alignment,
            "role": self.variant.role,
            "account_scope": "live_paper",
            "execution_mode": "PAPER",
            "mirrors_variant_id": LIVE_VARIANT_ID,
            "db_path": str(self.store.db_path),
            "last_signal": dict(self.last_signal or {}),
            "last_error": self.last_error,
            "last_stop_win": dict(self.last_stop_win or {}),
            "active_orders": len(self.store.active_paper_orders("BTC")),
            "order_summary": order_summary or self.store.paper_order_summary("BTC"),
            "metrics": metrics or self.store.metrics(),
            "recent_trades_summary": trade_summary or self.store.recent_trade_summary("BTC"),
        }

    def orders_page(self, limit: int, offset: int, status_filter: str = "all") -> dict[str, Any]:
        status_key = normalize_paper_order_status_filter(status_filter)
        total = self.store.paper_order_count("BTC", status_key)
        rows = self._tag_rows(self.store.recent_paper_orders(limit, offset, "BTC", status_key))
        loaded = min(total, offset + len(rows))
        return {
            "recent_orders": rows,
            "recent_orders_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "status_filter": status_key,
            },
        }

    def open_trades(self) -> list[dict[str, Any]]:
        return self._tag_rows([row for row in self.store.open_trades() if row["symbol"] == "BTC"])

    def recent_trades_page(
        self,
        limit: int,
        offset: int,
        start_at: float | None,
        end_at: float | None,
    ) -> dict[str, Any]:
        total = self.store.recent_trade_count("BTC", start_at, end_at)
        rows = self._tag_rows(self.store.recent_trades(limit, offset, "BTC", start_at, end_at))
        loaded = min(total, offset + len(rows))
        return {
            "recent_trades": rows,
            "recent_trades_summary": self.store.recent_trade_summary("BTC", start_at, end_at),
            "recent_trades_meta": {
                "limit": limit,
                "offset": offset,
                "loaded": loaded,
                "total": total,
                "has_more": loaded < total,
                "start_at": start_at,
                "end_at": end_at,
            },
        }

    def equity_curve_window(self, days: int, max_points: int) -> dict[str, Any]:
        rows = self.store.equity_curve_window(days, max_points)
        return {
            "equity_curve": rows,
            "equity_curve_meta": {
                "account_scope": "live_paper",
                "variant_id": self.variant_id,
                "combo": self.combo,
                "label": self.combo,
                "days": days,
                "max_points": max_points,
                "points": len(rows),
                "initial_balance": self.config.initial_balance,
            },
        }

    def apply_official_resolution(
        self,
        round_id: str,
        outcome: str,
        now: float,
        *,
        final_price: float | None = None,
        target_price: float | None = None,
    ) -> None:
        super().apply_official_resolution(
            round_id,
            outcome,
            now,
            final_price=final_price,
            target_price=target_price,
        )
        self.official_broadcast_count += 1
        self.last_official_broadcast_at = now

    def _tag_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _tag_live_paper_rows(rows, variant_id=self.variant_id, combo=self.combo)


class LivePaperStopWinStrategyRunner(LivePaperStrategyRunner):
    """实盘策略止盈影子账户：只在 Paper 里模拟按 bid 百分比止盈。"""

    def __init__(self, settings: Settings, polymarket: PolymarketClient) -> None:
        super().__init__(
            settings,
            polymarket,
            variant_id=LIVE_PAPER_STOP_WIN_VARIANT_ID,
            combo=LIVE_PAPER_STOP_WIN_COMBO,
            entry_marker=LIVE_PAPER_STOP_WIN_ENTRY_MARKER,
            role="SINGLE_FAK_REAL 的 Paper 止盈对照账户，不触发真实下单",
            db_path=_live_paper_stop_win_db_path(settings),
            stop_win_enabled=True,
        )


def _order_response(raw: Any, side: str = "BUY") -> LiveOrderResponse:
    data = raw if isinstance(raw, dict) else {"raw": raw}
    success = bool(data.get("success") if "success" in data else data.get("orderID") or data.get("orderId"))
    status = str(data.get("status") or data.get("orderStatus") or ("OK" if success else "ERROR"))
    order_id = data.get("orderID") or data.get("orderId") or data.get("id")
    error = data.get("errorMsg") or data.get("error") or data.get("message")
    fill = _matched_amounts_from_response(data, side)
    return LiveOrderResponse(
        success=success and not error,
        status=status,
        order_id=str(order_id) if order_id else None,
        error=str(error) if error else None,
        raw=data,
        filled_shares=fill["shares"] if fill else None,
        cash_spent=fill["cash"] if fill else None,
        avg_fill_price=fill["price"] if fill else None,
    )


def _order_state_response(order: dict[str, Any], side: str, raw: dict[str, Any]) -> LiveOrderResponse:
    status = str(order.get("status") or order.get("orderStatus") or "UNKNOWN")
    order_id = order.get("id") or order.get("orderID") or order.get("orderId") or raw.get("order_id")
    fill = _matched_amounts_from_order(order, side)
    success = not _status_is_rejected(status)
    return LiveOrderResponse(
        success=success,
        status=status,
        order_id=str(order_id) if order_id else None,
        error=None if success else status,
        raw=raw,
        filled_shares=fill["shares"] if fill else None,
        cash_spent=fill["cash"] if fill else None,
        avg_fill_price=fill["price"] if fill else None,
    )


def _response_indicates_fill(response: LiveOrderResponse) -> bool:
    status = response.status.lower()
    raw = response.raw
    if status in {"matched", "filled", "trades_matched"}:
        return True
    if "unmatched" not in status and ("matched" in status or "filled" in status):
        return True
    trade_ids = raw.get("tradeIDs") or raw.get("tradeIds") or raw.get("trades")
    if isinstance(trade_ids, list) and trade_ids:
        return True
    if _response_has_fill_amounts(response):
        return True
    return False


def _response_has_fill_amounts(response: LiveOrderResponse) -> bool:
    return bool(
        response.filled_shares
        and response.filled_shares > 0
        and response.cash_spent
        and response.cash_spent > 0
        and response.avg_fill_price
        and response.avg_fill_price > 0
    )


def _response_terminal_no_fill(response: LiveOrderResponse) -> bool:
    return _response_terminal_no_fill_local_status(response) is not None


def _response_terminal_no_fill_local_status(response: LiveOrderResponse) -> str | None:
    if _response_indicates_fill(response):
        return None
    status = str(response.status or "").lower()
    if any(marker in status for marker in ("invalid", "rejected", "failed", "error")):
        return STATUS_REJECTED
    if any(marker in status for marker in ("canceled", "cancelled", "expired", "unmatched")):
        return STATUS_CANCELED
    raw = response.raw or {}
    order = raw.get("order") if isinstance(raw.get("order"), dict) else {}
    size_matched = _fixed_math_amount(order.get("size_matched") or order.get("sizeMatched")) if order else None
    if size_matched is not None and size_matched <= 0 and any(marker in status for marker in ("done", "terminal")):
        return STATUS_CANCELED
    return None


def _response_public_payload(response: LiveOrderResponse) -> dict[str, Any]:
    return {
        "success": response.success,
        "status": response.status,
        "order_id": response.order_id,
        "error": response.error,
    }


def _public_open_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id") or row.get("orderID") or row.get("orderId"),
        "market": row.get("market") or row.get("condition_id") or row.get("conditionId"),
        "asset_id": row.get("asset_id") or row.get("assetId") or row.get("token_id") or row.get("tokenId"),
        "side": row.get("side"),
        "price": row.get("price"),
        "original_size": row.get("original_size") or row.get("originalSize") or row.get("size"),
        "remaining_size": row.get("remaining_size") or row.get("remainingSize") or row.get("size_matched"),
        "status": row.get("status") or row.get("orderStatus"),
        "created_at": row.get("created_at") or row.get("createdAt"),
    }


def _public_reconciled_order(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row.get("id"),
        "symbol": row.get("symbol"),
        "round_id": row.get("round_id"),
        "side": row.get("side"),
        "order_type": row.get("order_type"),
        "status": row.get("status"),
        "limit_price": row.get("limit_price"),
        "external_order_id": row.get("external_order_id"),
        "external_status": row.get("external_status"),
        "requested_cash": row.get("requested_cash"),
        "reserved_cash": row.get("reserved_cash"),
        "remaining_cash": row.get("remaining_cash"),
        "filled_shares": row.get("filled_shares"),
        "avg_fill_price": row.get("avg_fill_price"),
        "notional": row.get("notional"),
        "fee": row.get("fee"),
        "cash_spent": row.get("cash_spent"),
        "trade_id": row.get("trade_id"),
        "fill_count": row.get("fill_count"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "reason": row.get("reason"),
    }


def _public_evidence_order(
    row: dict[str, Any] | None,
    *,
    variant_id: str = LIVE_VARIANT_ID,
    combo: str = LIVE_COMBO,
) -> dict[str, Any] | None:
    payload = _public_reconciled_order(row)
    if payload is None:
        return None
    meta = _live_strategy_meta(variant_id)
    payload.update(
        {
            "variant_id": variant_id,
            "combo": combo,
            "strategy_family": STRATEGY_FAMILY_SINGLE,
            "experiment_order_type": ORDER_TYPE_FAK,
            "single_entry_mode": SINGLE_ENTRY_MODE_LEGACY,
            "signal_filter_mode": str(meta.get("signal_filter_mode") or SIGNAL_FILTER_MODE_NONE),
            "account_scope": "live",
            "execution_mode": "LIVE",
            "condition_id": row.get("condition_id"),
            "url": row.get("url"),
        }
    )
    return payload


def _public_evidence_orders(
    rows: list[dict[str, Any]],
    *,
    variant_id: str = LIVE_VARIANT_ID,
    combo: str = LIVE_COMBO,
) -> list[dict[str, Any]]:
    return [
        payload
        for payload in (_public_evidence_order(row, variant_id=variant_id, combo=combo) for row in rows)
        if payload is not None
    ]


def _signed_order_hash(client: Any, signed_order: Any) -> str | None:
    builder = getattr(client, "builder", None)
    if builder is not None and hasattr(builder, "build_order_typed_data") and hasattr(builder, "build_order_hash"):
        try:
            typed_data = builder.build_order_typed_data(signed_order)
            value = builder.build_order_hash(typed_data)
            return str(value) if value else None
        except Exception:  # noqa: BLE001 - hash 只用于审计/对账增强，不影响主下单。
            pass
    for attr in ("order_hash", "orderHash", "hash", "id"):
        value = getattr(signed_order, attr, None)
        if value:
            return str(value)
    return None


def _preflight_check(
    key: str,
    passed: bool,
    ok_message: str,
    error_message: str = "",
    *,
    errors: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    error_list = list(errors or [])
    if not passed and error_message and not error_list:
        error_list.append(error_message)
    payload = {
        "key": key,
        "status": "PASS" if passed else "BLOCK",
        "message": ok_message if passed else error_message,
        "errors": [] if passed else error_list,
    }
    payload.update(extra)
    return payload


def _finalize_preflight_payload(payload: dict[str, Any], *, signal_ok: bool) -> dict[str, Any]:
    checks = [row for row in payload.get("checks", []) if isinstance(row, dict)]
    blocked = [row for row in checks if row.get("status") != "PASS"]
    payload["blocked_checks"] = [
        _blocked_preflight_check(row)
        for row in blocked
    ]
    payload["ready"] = not blocked
    arming_blocked = [row for row in blocked if row.get("key") != "enabled"]
    payload["arming_ready"] = not arming_blocked
    payload["can_enable_live"] = bool(payload["arming_ready"])
    payload["can_place_next_order"] = bool(payload["ready"] and signal_ok and payload.get("enabled"))
    return payload


def _normalize_live_fallback_sources(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values = [item.strip().lower() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_values = [str(item or "").strip().lower() for item in value]
    else:
        raw_values = []
    selected = {item for item in raw_values if item in LIVE_FALLBACK_SOURCE_ORDER}
    return tuple(source for source in LIVE_FALLBACK_SOURCE_ORDER if source in selected)


def _normalize_live_strategy_id(value: Any) -> str:
    text = str(value or "").strip().upper()
    allowed = {str(item["variant_id"]).upper() for item in LIVE_STRATEGY_OPTIONS}
    return text if text in allowed else LIVE_VARIANT_ID


def _live_strategy_meta(variant_id: Any) -> dict[str, Any]:
    normalized = _normalize_live_strategy_id(variant_id)
    for item in LIVE_STRATEGY_OPTIONS:
        if str(item["variant_id"]).upper() == normalized:
            return dict(item)
    return dict(LIVE_STRATEGY_OPTIONS[0])


def _live_strategy_options_payload(settings: Settings | None = None) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in LIVE_STRATEGY_OPTIONS:
        row = {
            "variant_id": str(item["variant_id"]),
            "combo": str(item["combo"]),
            "stop_win_enabled": bool(item.get("stop_win_enabled")),
            "signal_filter_mode": str(item.get("signal_filter_mode") or SIGNAL_FILTER_MODE_NONE),
            "role": str(item.get("role") or ""),
        }
        if settings is not None:
            row["db_path"] = str(_live_strategy_db_path(settings, item["variant_id"]))
            if row["signal_filter_mode"] == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V10_DIAGNOSTIC:
                row["sample_readiness"] = _live_aggressive_edge_v10_readiness(settings)
            if row["signal_filter_mode"] == SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V11_DIAGNOSTIC:
                row["sample_readiness"] = _live_aggressive_edge_v11_readiness(settings)
        payload.append(row)
    return payload


def _live_aggressive_edge_readiness(settings: Settings, version: str) -> dict[str, Any]:
    """读取指定诊断版本样本准入结果；REAL 只能消费统一诊断池的已结算证据。"""

    normalized_version = str(version or "V10").upper()
    db_path = settings.strategy_experiments_db_dir / "single_fak_aggressive_edge_diagnostic.sqlite3"
    if not db_path.exists():
        return {
            "status": "WAITING_FOR_SAMPLE",
            "label": "继续采样",
            "eligible_for_live_review": False,
            "reasons": [f"缺少 {normalized_version} 诊断样本库 {db_path}"],
        }
    store = TradeStore(db_path, 100.0)
    try:
        summary = store.aggressive_edge_v2_shadow_summary("BTC")
    finally:
        store.conn.close()
    versions = {
        str(row.get("version") or "").upper(): row
        for row in summary.get("diagnostic_version_summaries") or []
        if isinstance(row, dict)
    }
    version_row = versions.get(normalized_version) or {}
    readiness = version_row.get("live_readiness") if isinstance(version_row.get("live_readiness"), dict) else {}
    result = dict(readiness)
    result.setdefault("status", "WAITING_FOR_SAMPLE")
    result.setdefault("label", "继续采样")
    result.setdefault("eligible_for_live_review", False)
    result.setdefault("reasons", [f"{normalized_version} 样本准入数据缺失"])
    result["version"] = normalized_version
    result["settled_count"] = int(version_row.get("settled_count") or 0)
    result["win_rate_pct"] = version_row.get("win_rate_pct")
    result["simulated_roi_pct"] = version_row.get("simulated_roi_pct")
    return result


def _live_aggressive_edge_v10_readiness(settings: Settings) -> dict[str, Any]:
    """读取 V10 诊断样本准入结果。"""

    return _live_aggressive_edge_readiness(settings, "V10")


def _live_aggressive_edge_v11_readiness(settings: Settings) -> dict[str, Any]:
    """读取 V11 诊断样本准入结果。"""

    return _live_aggressive_edge_readiness(settings, "V11")


def _live_strategy_variant(variant_id: Any) -> StrategyVariant:
    meta = _live_strategy_meta(variant_id)
    return StrategyVariant(
        str(meta["variant_id"]),
        STRATEGY_FAMILY_SINGLE,
        ORDER_TYPE_FAK,
        "85%",
        "25%-35%",
        str(meta.get("role") or ""),
        single_entry_mode=SINGLE_ENTRY_MODE_LEGACY,
        signal_filter_mode=str(meta.get("signal_filter_mode") or SIGNAL_FILTER_MODE_NONE),
    )


def _live_signal_price_payload(
    price: dict[str, Any],
    fallback_sources: tuple[str, ...],
    max_age_ms: int,
    target_price: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    selected = _normalize_live_fallback_sources(fallback_sources)
    basis_rows = _live_basis_rows(price, now_ms, selected)
    selection: dict[str, Any] = {
        "mode": "DEFAULT" if not selected else "SELECTED",
        "fallback_sources": list(selected),
        "basis": basis_rows,
        "blocked": False,
        "message": "",
        "note": "",
        "selected_source": None,
        "selected_sources": [],
        "selected_price": None,
        "selected_age_ms": None,
    }
    if not selected:
        payload = dict(price)
        source, age_ms = _gate_price_source(payload, time.time())
        selection.update(
            {
                "selected_source": source or "default",
                "selected_sources": [source] if source else [],
                "selected_price": (
                    _float_or_none(payload.get("chainlink"))
                    or _live_source_price(payload, LIVE_FALLBACK_SOURCE_BINANCE)
                    or _live_source_price(payload, LIVE_FALLBACK_SOURCE_OKX)
                ),
                "selected_age_ms": age_ms,
                "message": "未选择实盘 fallback 来源，使用 SINGLE_FAK_REAL 默认价格源",
            }
        )
        return payload, selection

    chainlink = _float_or_none(price.get("chainlink"))
    chainlink_updated_ms = _int_or_none(price.get("chainlink_updated_ms"))
    chainlink_age_ms = max(0, now_ms - chainlink_updated_ms) if chainlink_updated_ms else None
    chainlink_fresh = (
        chainlink is not None
        and chainlink > 0
        and chainlink_age_ms is not None
        and chainlink_age_ms <= max_age_ms
    )
    selected_basis_sources = [source for source in selected if source in LIVE_BASIS_FALLBACK_SOURCES]
    ready_rows = [
        row
        for row in basis_rows
        if row.get("source") in selected_basis_sources
        and row.get("ready")
        and _float_or_none(row.get("adjusted_price")) is not None
    ]
    target = _float_or_none(target_price)
    if LIVE_FALLBACK_SOURCE_CHAINLINK in selected and chainlink_fresh and target and target > 0:
        if not selected_basis_sources:
            selection.update(
                {
                    "blocked": True,
                    "message": "Chainlink 单源不允许实盘入场；请选择 OKX 或 Binance 基差校正确认",
                }
            )
            return dict(price), selection
        if not ready_rows:
            reasons = [
                str(row.get("reason") or "")
                for row in basis_rows
                if row.get("source") in selected_basis_sources
            ]
            message = "Chainlink 已选择但缺少可用基差确认，NO_TRADE"
            if reasons:
                message = f"{message}: {'; '.join(item for item in reasons if item)}"
            selection.update({"blocked": True, "message": message})
            return dict(price), selection
        chainlink_direction = _price_target_direction(chainlink, target)
        confirming_rows = [
            row
            for row in ready_rows
            if _price_target_direction(_float_or_none(row.get("adjusted_price")), target) == chainlink_direction
        ]
        opposing_rows = [
            row
            for row in ready_rows
            if _price_target_direction(_float_or_none(row.get("adjusted_price")), target) not in {None, chainlink_direction}
        ]
        if chainlink_direction is None or not confirming_rows or opposing_rows:
            basis_text = ", ".join(
                f"{row.get('source')} {_float_or_none(row.get('adjusted_price')) or 0.0:.2f}"
                for row in ready_rows
            )
            selection.update(
                {
                    "blocked": True,
                    "message": f"Chainlink 与基差校正方向不一致，NO_TRADE: Chainlink {chainlink:.2f}, target {target:.2f}, basis {basis_text}",
                }
            )
            return dict(price), selection
        selected_names = [str(row.get("source")) for row in confirming_rows]
        median_text = ", ".join(
            f"{row.get('source')} {float(row.get('median_bps') or 0.0):+.2f}bps" for row in confirming_rows
        )
        payload = dict(price)
        selection.update(
            {
                "selected_source": LIVE_FALLBACK_SOURCE_CHAINLINK,
                "selected_sources": [LIVE_FALLBACK_SOURCE_CHAINLINK, *selected_names],
                "selected_price": chainlink,
                "selected_age_ms": chainlink_age_ms,
                "message": f"已选择 Chainlink，且基差校正价同向确认: {','.join(selected_names)}",
                "note": f"LIVE_FALLBACK selected Chainlink basis_confirmed {','.join(selected_names)} median {median_text}",
            }
        )
        return payload, selection

    if ready_rows:
        adjusted_price = sum(float(row["adjusted_price"]) for row in ready_rows) / len(ready_rows)
        updated_ms = max(int(row.get("updated_ms") or 0) for row in ready_rows)
        selected_names = [str(row.get("source")) for row in ready_rows]
        median_text = ", ".join(
            f"{row.get('source')} {float(row.get('median_bps') or 0.0):+.2f}bps" for row in ready_rows
        )
        payload = dict(price)
        payload["chainlink"] = None
        payload["chainlink_updated_ms"] = None
        payload["binance"] = round(adjusted_price, 8)
        payload["binance_updated_ms"] = updated_ms
        payload["source"] = f"basis-adjusted:{','.join(selected_names)}"
        selection.update(
            {
                "selected_source": "basis_adjusted",
                "selected_sources": selected_names,
                "selected_price": round(adjusted_price, 8),
                "selected_age_ms": max(0, now_ms - updated_ms) if updated_ms else None,
                "message": f"使用基差校正价 {adjusted_price:.2f}，来源 {','.join(selected_names)}",
                "note": f"LIVE_FALLBACK basis_adjusted {','.join(selected_names)} median {median_text}",
            }
        )
        return payload, selection

    reasons = [str(row.get("reason") or "") for row in basis_rows if row.get("source") in selected_basis_sources]
    if LIVE_FALLBACK_SOURCE_CHAINLINK in selected:
        reasons.append("Chainlink 不可用或过期")
    message = "实盘 fallback 选择无可用价格源，样本不足则 NO_TRADE"
    if reasons:
        message = f"{message}: {'; '.join(item for item in reasons if item)}"
    selection.update({"blocked": True, "message": message})
    return dict(price), selection


def _price_target_direction(price: float | None, target_price: float | None) -> str | None:
    if price is None or target_price is None or target_price <= 0:
        return None
    if price > target_price:
        return "Up"
    if price < target_price:
        return "Down"
    return None


def _live_basis_rows(price: dict[str, Any], now_ms: int, selected: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    chainlink = _float_or_none(price.get("chainlink"))
    for source in LIVE_BASIS_FALLBACK_SOURCES:
        source_price = _live_source_price(price, source)
        updated_ms = _live_source_updated_ms(price, source)
        age_ms = max(0, now_ms - updated_ms) if updated_ms else None
        median_bps = _float_or_none(price.get(f"{source}_basis_median_bps"))
        latest_bps = _float_or_none(price.get(f"{source}_basis_latest_bps"))
        if latest_bps is None and chainlink and source_price:
            latest_bps = (source_price - chainlink) / chainlink * 10_000.0
        samples = _int_or_none(price.get(f"{source}_basis_samples")) or 0
        adjusted_price = None
        if source_price is not None and median_bps is not None:
            adjusted_price = source_price / (1.0 + median_bps / 10_000.0)
        ready = (
            source in selected
            and source_price is not None
            and source_price > 0
            and updated_ms is not None
            and age_ms is not None
            and age_ms <= MULTI_SOURCE_MAX_AGE_MS
            and median_bps is not None
            and samples >= MULTI_SOURCE_MIN_SAMPLES
            and adjusted_price is not None
        )
        reason = "ready"
        if source_price is None or source_price <= 0:
            reason = "缺少实时价"
        elif updated_ms is None or age_ms is None or age_ms > MULTI_SOURCE_MAX_AGE_MS:
            reason = f"价格过期或缺时间戳 age={age_ms if age_ms is not None else '-'}ms"
        elif median_bps is None or samples < MULTI_SOURCE_MIN_SAMPLES:
            reason = f"基差样本不足 {samples}/{MULTI_SOURCE_MIN_SAMPLES}"
        rows.append(
            {
                "source": source,
                "selected": source in selected,
                "ready": ready,
                "reason": reason,
                "price": round(float(source_price), 8) if source_price is not None else None,
                "updated_ms": updated_ms,
                "age_ms": age_ms,
                "latest_bps": round(float(latest_bps), 6) if latest_bps is not None else None,
                "median_bps": round(float(median_bps), 6) if median_bps is not None else None,
                "samples": samples,
                "adjusted_price": round(float(adjusted_price), 8) if adjusted_price is not None else None,
                "basis_usd": round(float(source_price - adjusted_price), 6)
                if source_price is not None and adjusted_price is not None
                else None,
            }
        )
    return rows


def _live_source_price(price: dict[str, Any], source: str) -> float | None:
    if source == LIVE_FALLBACK_SOURCE_BINANCE:
        return _float_or_none(price.get("binance_market")) or _float_or_none(price.get("binance"))
    if source == LIVE_FALLBACK_SOURCE_OKX:
        return _float_or_none(price.get("okx"))
    return _float_or_none(price.get(source))


def _live_source_updated_ms(price: dict[str, Any], source: str) -> int | None:
    if source == LIVE_FALLBACK_SOURCE_BINANCE:
        return _int_or_none(price.get("binance_market_updated_ms")) or _int_or_none(price.get("binance_updated_ms"))
    if source == LIVE_FALLBACK_SOURCE_OKX:
        return _int_or_none(price.get("okx_updated_ms"))
    return _int_or_none(price.get(f"{source}_updated_ms"))


def _gate_check(
    key: str,
    status: str,
    message: str,
    *,
    current: Any = None,
    threshold: Any = None,
    required: Any = None,
    errors: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    normalized = str(status or "PASS").strip().upper()
    if normalized not in {"PASS", "WARN", "BLOCK"}:
        normalized = "BLOCK"
    payload = {
        "key": key,
        "status": normalized,
        "message": message,
        "current": current,
        "threshold": threshold,
        "required": required,
        "errors": list(errors or []),
    }
    payload.update(extra)
    return payload


def _finalize_gate_status(payload: dict[str, Any]) -> dict[str, Any]:
    checks = [row for row in payload.get("checks", []) if isinstance(row, dict)]
    blocked = [row for row in checks if row.get("status") == "BLOCK"]
    warnings = [row for row in checks if row.get("status") == "WARN"]
    payload["blocked_checks"] = blocked
    payload["warning_checks"] = warnings
    signal = payload.get("signal") if isinstance(payload.get("signal"), dict) else {}
    signal_block_only = bool(blocked) and all(row.get("key") == "signal" for row in blocked)
    can_place = not blocked and bool(payload.get("enabled")) and signal.get("side") in {"Up", "Down"}
    payload["can_place_order"] = can_place
    if can_place:
        payload["overall_status"] = "READY_WITH_WARN" if warnings else "READY"
        payload["overall_label"] = "可下单但有警告" if warnings else "可下单"
        payload["primary_blocker"] = None
        payload["primary_message"] = warnings[0]["message"] if warnings else "全部条件通过"
        payload["next_action"] = "实盘开关开启时，满足当前信号会提交订单"
        return payload
    if not payload.get("enabled"):
        primary = _first_check(checks, "enabled") or (blocked[0] if blocked else None)
        payload["overall_status"] = "DISABLED"
        payload["overall_label"] = "实盘关闭"
    elif signal_block_only:
        primary = blocked[0]
        payload["overall_status"] = "WAIT_SIGNAL"
        payload["overall_label"] = "等待信号"
    else:
        primary = blocked[0] if blocked else (warnings[0] if warnings else None)
        payload["overall_status"] = "BLOCKED" if blocked else "WARN"
        payload["overall_label"] = "已阻断" if blocked else "有警告"
    payload["primary_blocker"] = primary.get("key") if primary else None
    payload["primary_message"] = primary.get("message") if primary else "暂无状态"
    payload["next_action"] = _gate_next_action(str(payload.get("primary_blocker") or ""))
    return payload


def _first_check(checks: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    return next((row for row in checks if row.get("key") == key), None)


def _gate_next_action(key: str) -> str:
    return {
        "enabled": "打开顶部实盘开关；重启后实盘默认关闭，需要重新预检并手动开启",
        "process_lock": "确认只运行一个 dashboard 进程，必要时用 restart-dashboard.sh 重启",
        "compliance_acknowledged": "在实盘配置中勾选风险确认后保存",
        "geo_access": "地区检查未通过时不要实盘下单",
        "credentials": "检查 .env.live 的私钥、签名类型、funder 和 API 凭证",
        "market": "等待后端发现当前 BTC 5m 市场",
        "target_price": "等待官方 market.target_price 可用；缺目标价禁止实盘",
        "signal": "等待下一次市场波动产生 Up/Down 信号",
        "price_source": "检查实盘 fallback 复选框；基差样本不足时先采样，不要实盘",
        "daily_loss": "日亏损已触发；等待窗口恢复或手动调整阈值",
        "total_drawdown": "总回撤已触发；停止实盘或手动调整总回撤阈值",
        "max_open_trades": "等待持仓结算/卖出，或调整最大持仓",
        "duplicate_direction": "等待当前市场同方向持仓结算，不重复开同方向",
        "pending_entry_order": "等待待确认买入订单变为成交、取消或超时",
        "software_cash": "提高隔离初始金额或降低单笔金额",
        "quote_freshness": "等待后台 CLOB WebSocket/REST 刷新盘口",
        "max_entry_price": "等待更低买价，或手动提高最高买价",
        "min_order_size": "提高单笔金额到市场最小订单以上",
        "orderbook_depth": "等待盘口深度恢复",
        "official_open_orders_clear": "点击刷新挂单；若仍有官方挂单，执行实盘急停或手动处理",
        "collateral_wallet": "检查 Polymarket USDC 余额和 CLOB collateral allowance",
    }.get(key, "查看阻断条件明细")


def _gate_price_source(price: dict[str, Any], now: float) -> tuple[str | None, float | None]:
    chainlink = _float_or_none(price.get("chainlink"))
    chainlink_at = _int_or_none(price.get("chainlink_updated_ms"))
    if chainlink is not None and chainlink > 0 and chainlink_at:
        age_ms = max(0.0, now * 1000.0 - float(chainlink_at))
        return "Chainlink", round(age_ms, 3)
    for key in ("binance", "okx", "binance_market"):
        fallback = _float_or_none(price.get(key))
        updated_key = f"{key}_updated_ms"
        updated = _int_or_none(price.get(updated_key))
        if fallback is not None and fallback > 0:
            age_ms = max(0.0, now * 1000.0 - float(updated)) if updated else None
            return "fallback", round(age_ms, 3) if age_ms is not None else None
    return None, None


def _gate_quote_age_ms(quote: dict[str, Any], now: float) -> float | None:
    updated_ms = _int_or_none(quote.get("updated_at_ms"))
    if updated_ms is None or updated_ms <= 0:
        return None
    return max(0.0, now * 1000.0 - float(updated_ms))


def _blocked_preflight_check(row: dict[str, Any]) -> dict[str, Any]:
    blocked = {
        "key": str(row.get("key") or ""),
        "message": str(row.get("message") or ""),
        "errors": list(row.get("errors") or []),
    }
    for key, value in row.items():
        if key in {"key", "status", "message", "errors"}:
            continue
        blocked[key] = value
    return blocked


def _geoblock_block_reason(payload: dict[str, Any]) -> str | None:
    errors = list(payload.get("errors") or [])
    if errors:
        return str(errors[0])
    if not payload.get("ready"):
        return "Polymarket 地区访问状态未确认，停止真实下单"
    if payload.get("blocked") is True:
        country = str(payload.get("country") or "-")
        region = str(payload.get("region") or "-")
        return f"Polymarket 当前运行地区被限制 country={country} region={region}，停止真实下单"
    return None


def _env_file_permission_errors() -> list[str]:
    errors: list[str] = []
    for item in env_file_status():
        live_sensitive_keys = [
            str(key)
            for key in (item.get("sensitive_keys_present") or [])
            if str(key) in LIVE_CREDENTIAL_ENV_KEYS
        ]
        if not live_sensitive_keys:
            continue
        if item.get("secure_permissions") is not False:
            continue
        path = str(item.get("path") or "env file")
        mode = str(item.get("mode") or "unknown")
        errors.append(
            f"{path} 包含实盘密钥字段但权限为 {mode}，请执行 chmod 600 {path} 后再开启实盘"
        )
    return errors


def _retryable_order_exception(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 425, 429} or 500 <= status_code < 600
    text = str(exc).lower()
    return any(marker in text for marker in ("timeout", "connect", "network", "request exception"))


def _exception_retry_reasons(exc: Exception) -> list[str]:
    reasons = getattr(exc, "_polybot_retry_reasons", None)
    return list(reasons) if isinstance(reasons, list) else []


def _normalize_tick_size(value: str | None) -> str:
    tick = str(value or "0.01").strip()
    if tick in {"0.1", "0.01", "0.001", "0.0001"}:
        return tick
    return "0.01"


def _matched_amounts_from_response(data: dict[str, Any], side: str) -> dict[str, float] | None:
    making_raw = data.get("makingAmount") or data.get("making_amount")
    taking_raw = data.get("takingAmount") or data.get("taking_amount")
    if making_raw in (None, "") or taking_raw in (None, ""):
        return None
    normalized_side = str(side or "").upper()
    if normalized_side == "SELL":
        shares_raw = making_raw
        cash_raw = taking_raw
    else:
        shares_raw = taking_raw
        cash_raw = making_raw
    parsed = _matched_cash_and_shares(cash_raw, shares_raw)
    if parsed is None:
        return None
    cash, shares = parsed
    if shares <= 0 or cash <= 0:
        return None
    return {"shares": round(shares, 6), "cash": round(cash, 6), "price": round(cash / shares, 6)}


def _matched_amounts_from_order(data: dict[str, Any], side: str) -> dict[str, float] | None:
    price = _float_or_none(data.get("price") or data.get("avg_price") or data.get("average_price"))
    shares = _clob_quantity_amount(
        data.get("size_matched") or data.get("sizeMatched") or data.get("matched_size"),
        price=price,
    )
    if shares is None or shares <= 0 or price is None or price <= 0:
        return _matched_amounts_from_response(data, side)
    cash = round(shares * price, 6)
    return {"shares": round(shares, 6), "cash": cash, "price": round(price, 6)}


def _matched_amounts_from_trades(trades: list[dict[str, Any]]) -> dict[str, float] | None:
    total_shares = 0.0
    total_cash = 0.0
    for row in trades:
        status = str(row.get("status") or "").lower()
        if "failed" in status or "cancel" in status:
            continue
        price = _float_or_none(row.get("price"))
        shares = _clob_quantity_amount(
            row.get("size") or row.get("matched_size") or row.get("size_matched"),
            price=price,
        )
        if shares is None or shares <= 0 or price is None or price <= 0:
            continue
        total_shares += shares
        total_cash += shares * price
    if total_shares <= 0 or total_cash <= 0:
        return None
    return {
        "shares": round(total_shares, 6),
        "cash": round(total_cash, 6),
        "price": round(total_cash / total_shares, 6),
    }


def _matched_cash_and_shares(cash_raw: Any, shares_raw: Any) -> tuple[float, float] | None:
    candidates: list[tuple[float, float, float]] = []
    for cash, cash_score in _clob_amount_candidates(cash_raw):
        for shares, shares_score in _clob_amount_candidates(shares_raw):
            if cash <= 0 or shares <= 0:
                continue
            price = cash / shares
            price_score = 100.0 if 0.001 <= price <= 1.0 else -100.0
            size_score = 0.0
            if cash > 100_000:
                size_score -= 20.0
            if shares > 100_000_000:
                size_score -= 20.0
            candidates.append((price_score + cash_score + shares_score + size_score, cash, shares))
    if not candidates:
        return None
    score, cash, shares = max(candidates, key=lambda item: item[0])
    if score < 50.0:
        return None
    return cash, shares


def _clob_quantity_amount(value: Any, *, price: float | None = None) -> float | None:
    candidates = _clob_amount_candidates(value)
    if not candidates:
        return None
    if price and price > 0:
        plausible = [
            (amount, score)
            for amount, score in candidates
            if 0 < amount * price <= 100_000
        ]
        if plausible:
            return max(plausible, key=lambda item: item[1])[0]
    return max(candidates, key=lambda item: item[1])[0]


def _clob_amount_candidates(value: Any) -> list[tuple[float, float]]:
    if value in (None, ""):
        return []
    try:
        text = str(value).strip()
        amount = float(text)
    except (TypeError, ValueError):
        return []
    if amount <= 0:
        return []
    normalized = text.lower()
    if "." in normalized or "e" in normalized:
        return [(amount, 2.0)]
    decimal_score = 2.0 if amount < 1_000_000 else -4.0
    fixed_score = 3.0 if amount >= 1_000_000 else -4.0
    return [(amount, decimal_score), (amount / 1_000_000.0, fixed_score)]


def _trade_matches_order(trade: dict[str, Any], order_id: str) -> bool:
    expected = str(order_id or "").lower()
    if not expected:
        return False
    for key in ("taker_order_id", "takerOrderId", "order_id", "orderID", "orderId", "maker_order_id"):
        if str(trade.get(key) or "").lower() == expected:
            return True
    maker_orders = trade.get("maker_orders") or trade.get("makerOrders")
    if isinstance(maker_orders, list):
        for row in maker_orders:
            if isinstance(row, str) and row.lower() == expected:
                return True
            if isinstance(row, dict):
                for key in ("order_id", "orderID", "orderId", "id"):
                    if str(row.get(key) or "").lower() == expected:
                        return True
    return False


def _status_is_rejected(status: str) -> bool:
    normalized = str(status or "").lower()
    return any(marker in normalized for marker in ("invalid", "rejected", "failed", "error"))


def _fixed_math_amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        amount = float(text)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    if "." in str(value):
        return amount
    return amount / 1_000_000.0


def _allowance_amount(raw: dict[str, Any]) -> float | None:
    top_level = _fixed_math_amount(raw.get("allowance"))
    if top_level is not None:
        return top_level
    allowances = raw.get("allowances")
    if not isinstance(allowances, dict):
        return None
    parsed_values = [
        amount
        for amount in (_fixed_math_amount(value) for value in allowances.values())
        if amount is not None
    ]
    if not parsed_values:
        return None
    return max(parsed_values)


def _wallet_state_with_requirement(payload: dict[str, Any], required_cash: float | None) -> dict[str, Any]:
    item = dict(payload)
    errors = list(item.get("errors") or [])
    required = max(0.0, round(float(required_cash or 0.0), 6))
    item["required_cash"] = required
    balance = _float(item.get("balance"), 0.0)
    allowance = _float(item.get("allowance"), 0.0)
    if required > 0:
        if balance + LIVE_EPSILON < required:
            errors.append(f"Polymarket collateral balance {balance:.6f} 低于本次实盘预算 {required:.6f}")
        if allowance + LIVE_EPSILON < required:
            errors.append(f"Polymarket collateral allowance {allowance:.6f} 低于本次实盘预算 {required:.6f}")
    item["ready"] = not errors
    item["errors"] = errors
    return item


def _token_state_with_requirement(payload: dict[str, Any], required_shares: float | None) -> dict[str, Any]:
    item = dict(payload)
    errors = list(item.get("errors") or [])
    required = max(0.0, round(float(required_shares or 0.0), 6))
    item["required_shares"] = required
    balance = _float(item.get("balance"), 0.0)
    allowance = _float(item.get("allowance"), 0.0)
    if required > 0:
        if balance + LIVE_EPSILON < required:
            errors.append(f"Polymarket conditional token balance {balance:.6f} 低于本次卖出份额 {required:.6f}")
        if allowance + LIVE_EPSILON < required:
            errors.append(f"Polymarket conditional token allowance {allowance:.6f} 低于本次卖出份额 {required:.6f}")
    item["ready"] = not errors
    item["errors"] = errors
    return item


def _fill_from_response_or_sweep(
    sweep_fill: PaperFill,
    response: LiveOrderResponse,
    *,
    taker_fee_rate: float,
) -> PaperFill:
    if not (response.filled_shares and response.cash_spent and response.avg_fill_price):
        return sweep_fill
    shares = round(float(response.filled_shares), 6)
    notional = round(float(response.cash_spent), 6)
    fill_price = round(float(response.avg_fill_price), 6)
    fee = taker_fee(shares, fill_price, taker_fee_rate)
    cash_spent = round(notional + fee, 6)
    level = PaperFillLevel(
        price=round(fill_price, 4),
        shares=shares,
        notional=notional,
        fee=fee,
        cash_spent=cash_spent,
    )
    return PaperFill(
        market=sweep_fill.market,
        signal=sweep_fill.signal,
        side=sweep_fill.side,
        order_type=sweep_fill.order_type,
        status=sweep_fill.status,
        limit_price=sweep_fill.limit_price,
        fill_price=fill_price,
        shares=shares,
        notional=notional,
        fee=fee,
        cash_spent=cash_spent,
        quote_size=sweep_fill.quote_size,
        reason=_append_reason(
            sweep_fill.reason,
            (
                "official response matched amounts: "
                f"fill {shares:.6f} @ avg {fill_price:.4f}, "
                f"notional {notional:.6f}, fee {fee:.6f}"
            ),
        ),
        levels=(level,),
        requested_cash=sweep_fill.requested_cash,
    )


def _fill_from_response_row(
    row: dict[str, Any],
    response: LiveOrderResponse,
    *,
    taker_fee_rate: float,
) -> PaperFill | None:
    if not (response.filled_shares and response.cash_spent and response.avg_fill_price):
        return None
    market = _market_from_row(row)
    side = str(row.get("side") or "")
    signal = Signal(
        symbol=str(row.get("symbol") or "BTC"),
        side=side,
        confidence=_float(row.get("confidence"), 0.0),
        entry_price=_float(response.avg_fill_price, _float(row.get("limit_price"), 0.0)),
        move_bps=_float(row.get("move_bps"), 0.0),
        reason=str(row.get("reason") or ""),
    )
    intent = TradeIntent(market=market, signal=signal, stake_dollars=_float(row.get("requested_cash"), 0.0))
    shares = round(float(response.filled_shares), 6)
    notional = round(float(response.cash_spent), 6)
    fill_price = round(float(response.avg_fill_price), 6)
    fee = taker_fee(shares, fill_price, taker_fee_rate)
    cash_spent = round(notional + fee, 6)
    level = PaperFillLevel(
        price=round(fill_price, 4),
        shares=shares,
        notional=notional,
        fee=fee,
        cash_spent=cash_spent,
    )
    return PaperFill(
        market=intent.market,
        signal=intent.signal,
        side=side,
        order_type=ORDER_TYPE_FAK,
        status=STATUS_FILLED,
        limit_price=_float(row.get("limit_price"), fill_price),
        fill_price=fill_price,
        shares=shares,
        notional=notional,
        fee=fee,
        cash_spent=cash_spent,
        quote_size=None,
        reason="LIVE_RECONCILED official order/trade fill",
        levels=(level,),
        requested_cash=intent.stake_dollars,
    )


def _exit_fill_from_response(
    response: LiveOrderResponse,
    *,
    taker_fee_rate: float,
) -> dict[str, float] | None:
    if not (response.filled_shares and response.cash_spent and response.avg_fill_price):
        return None
    shares = round(max(0.0, float(response.filled_shares)), 6)
    notional = round(max(0.0, float(response.cash_spent)), 6)
    exit_price = round(max(0.01, min(0.99, float(response.avg_fill_price))), 6)
    if shares <= 0 or notional <= 0:
        return None
    return {
        "shares": shares,
        "notional": notional,
        "exit_price": exit_price,
        "fee": taker_fee(shares, exit_price, taker_fee_rate),
    }


def _live_paper_db_path(settings: Settings) -> Path:
    return settings.live_trading_db_path.parent / f"{LIVE_PAPER_VARIANT_ID.lower()}.sqlite3"


def _live_paper_stop_win_db_path(settings: Settings) -> Path:
    return settings.live_trading_db_path.parent / f"{LIVE_PAPER_STOP_WIN_VARIANT_ID.lower()}.sqlite3"


def _live_strategy_db_path(settings: Settings, variant_id: Any) -> Path:
    normalized = _normalize_live_strategy_id(variant_id)
    if normalized == LIVE_VARIANT_ID:
        return settings.live_trading_db_path
    return settings.live_trading_db_path.parent / f"{normalized.lower()}.sqlite3"


def _paper_stop_win_max_profit(stake: float, shares: float) -> float:
    return round(max(0.0, float(shares or 0.0) - float(stake or 0.0)), 6)


def _paper_stop_win_target_pnl(stake: float, shares: float, take_profit_pct: float) -> float:
    max_profit = _paper_stop_win_max_profit(stake, shares)
    return round(max_profit * max(0.0, min(LIVE_PAPER_STOP_WIN_DISABLE_PCT, float(take_profit_pct))) / 100.0, 6)


def _paper_stop_win_trigger_bid(
    stake: float,
    shares: float,
    target_pnl: float,
    taker_fee_rate: float,
) -> float | None:
    normalized_stake = max(0.0, float(stake or 0.0))
    normalized_shares = max(0.0, float(shares or 0.0))
    required_cash = normalized_stake + max(0.0, float(target_pnl or 0.0))
    if normalized_shares <= 0 or required_cash <= 0:
        return None

    def net_cash(price: float) -> float:
        return normalized_shares * price - taker_fee(normalized_shares, price, taker_fee_rate)

    if net_cash(0.99) + LIVE_EPSILON < required_cash:
        return None

    low = 0.01
    high = 0.99
    for _ in range(40):
        mid = (low + high) / 2.0
        if net_cash(mid) + LIVE_EPSILON >= required_cash:
            high = mid
        else:
            low = mid
    return round(max(0.01, min(0.99, math.ceil(high * 10000.0 - LIVE_EPSILON) / 10000.0)), 4)


def _paper_sell_sweep_from_bid_quote(
    quote: dict[str, Any],
    shares: float,
    min_price: float,
) -> dict[str, float | str] | None:
    target_shares = round(max(0.0, float(shares or 0.0)), 6)
    if target_shares <= 0:
        return None
    threshold = max(0.01, min(0.99, round(float(min_price or 0.0), 4)))
    raw_bids = quote.get("bids")
    levels: list[tuple[float, float]] = []
    if isinstance(raw_bids, list):
        for row in raw_bids:
            if not isinstance(row, dict):
                continue
            price = _float_or_none(row.get("price"))
            size = _float_or_none(row.get("size"))
            if price is None or size is None or price <= 0 or price >= 1 or size <= 0:
                continue
            levels.append((round(float(price), 4), round(float(size), 6)))
    if levels:
        by_price: dict[float, float] = {}
        for price, size in levels:
            by_price[price] = round(by_price.get(price, 0.0) + size, 6)
        remaining = target_shares
        notional = 0.0
        for price, size in sorted(by_price.items(), reverse=True):
            if price + LIVE_EPSILON < threshold:
                break
            take = min(remaining, size)
            notional += take * price
            remaining = round(remaining - take, 6)
            if remaining <= LIVE_EPSILON:
                exit_price = round(notional / target_shares, 6)
                return {
                    "shares": target_shares,
                    "notional": round(notional, 6),
                    "exit_price": exit_price,
                    "depth_source": "bids",
                }
        return None
    best_bid = _float_or_none(quote.get("best_bid"))
    if best_bid is None or best_bid + LIVE_EPSILON < threshold:
        return None
    bid_size = _float_or_none(quote.get("bid_size"))
    if bid_size is not None and bid_size + LIVE_EPSILON < target_shares:
        return None
    exit_price = round(max(0.01, min(0.99, float(best_bid))), 6)
    return {
        "shares": target_shares,
        "notional": round(target_shares * exit_price, 6),
        "exit_price": exit_price,
        "depth_source": "best_bid",
    }


def _tag_live_rows(
    rows: list[dict[str, Any]],
    *,
    variant_id: str = LIVE_VARIANT_ID,
    combo: str = LIVE_COMBO,
) -> list[dict[str, Any]]:
    meta = _live_strategy_meta(variant_id)
    signal_filter_mode = str(meta.get("signal_filter_mode") or SIGNAL_FILTER_MODE_NONE)
    tagged: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "variant_id": variant_id,
                "combo": combo,
                "strategy_family": STRATEGY_FAMILY_SINGLE,
                "experiment_order_type": ORDER_TYPE_FAK,
                "single_entry_mode": SINGLE_ENTRY_MODE_LEGACY,
                "signal_filter_mode": signal_filter_mode,
                "account_scope": "live",
                "execution_mode": "LIVE",
            }
        )
        tagged.append(item)
    return tagged


def _tag_live_paper_rows(
    rows: list[dict[str, Any]],
    *,
    variant_id: str = LIVE_PAPER_VARIANT_ID,
    combo: str = LIVE_PAPER_COMBO,
) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "variant_id": variant_id,
                "combo": combo,
                "strategy_family": STRATEGY_FAMILY_SINGLE,
                "experiment_order_type": ORDER_TYPE_FAK,
                "single_entry_mode": SINGLE_ENTRY_MODE_LEGACY,
                "account_scope": "live_paper",
                "execution_mode": "PAPER",
                "mirrors_variant_id": LIVE_VARIANT_ID,
            }
        )
        tagged.append(item)
    return tagged


def _market_from_row(row: dict[str, Any]) -> MarketRound:
    return MarketRound(
        round_id=str(row.get("round_id") or ""),
        symbol=str(row.get("symbol") or "BTC"),
        started_at=_float(row.get("started_at"), 0.0),
        ends_at=_float(row.get("ends_at"), 0.0),
        target_price=_float(row.get("target_price"), 0.0),
        question=str(row.get("question") or ""),
        condition_id=str(row.get("condition_id") or ""),
        up_token=str(row.get("up_token") or ""),
        down_token=str(row.get("down_token") or ""),
        slug=str(row.get("round_id") or ""),
        url=str(row.get("url") or ""),
    )


def _client_order_id() -> str:
    return f"polybot2other-{uuid.uuid4().hex[:20]}"


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:8000]


def _append_reason(existing: str, addition: str) -> str:
    existing_text = str(existing or "").strip()
    addition_text = str(addition or "").strip()
    if existing_text and addition_text:
        return f"{existing_text} | {addition_text}"
    return existing_text or addition_text


def _quote_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        payload = value.to_dict()
        return dict(payload) if isinstance(payload, dict) else {}
    if isinstance(value, dict):
        return dict(value)
    return {}


def _quote_depth_payload(quote: dict[str, Any], sweep: Any) -> dict[str, Any]:
    return {
        "source": str(quote.get("source") or ""),
        "updated_at_ms": _int_or_none(quote.get("updated_at_ms")),
        "clob_received_ms": _int_or_none(quote.get("clob_received_ms")),
        "clob_event_updated_ms": _int_or_none(quote.get("clob_event_updated_ms")),
        "best_ask": _float_or_none(quote.get("best_ask")),
        "ask_size": _float_or_none(quote.get("ask_size")),
        "sweep_shares": _float(getattr(sweep, "shares", 0.0), 0.0),
        "sweep_avg_price": _float_or_none(getattr(sweep, "avg_price", None)),
    }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _is_private_key_like(value: str) -> bool:
    text = value[2:] if value.lower().startswith("0x") else value
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", text))


def _is_address_like(value: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", value.strip()))


def _mask_address(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 12:
        return text
    return f"{text[:6]}...{text[-4:]}"


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _updated_at_seconds(value: Any, fallback: float) -> float:
    parsed = _int_or_none(value)
    if parsed is None or parsed <= 0:
        return fallback
    return parsed / 1000.0 if parsed > 10_000_000_000 else float(parsed)


def _aggressive_edge_minute_bucket(market: MarketRound, now: float) -> int:
    seconds_from_start = max(0.0, float(now) - float(market.started_at or now))
    return max(0, min(4, int(seconds_from_start // 60.0)))


def _format_optional_float(value: Any, digits: int) -> str:
    parsed = _float_or_none(value)
    return "-" if parsed is None else f"{parsed:.{digits}f}"


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ENV_FILE_NAME = "POLYBOT2OTHER_ENV_FILE"
ENV_KEY_PREFIX = "POLYBOT2OTHER_"
EXTERNAL_ALLOWED_ENV_KEYS = {"HAOAI_API_KEY"}
DEFAULT_ENV_FILES = (".env.live", ".env.local", ".env")
SENSITIVE_ENV_KEYS = {
    "HAOAI_API_KEY",
    "POLYBOT2OTHER_LIVE_PRIVATE_KEY",
    "POLYBOT2OTHER_LIVE_API_KEY",
    "POLYBOT2OTHER_LIVE_API_SECRET",
    "POLYBOT2OTHER_LIVE_API_PASSPHRASE",
    "POLYBOT2OTHER_LLM_API_KEY",
}
LIVE_CREDENTIAL_ENV_KEYS = {
    "POLYBOT2OTHER_LIVE_PRIVATE_KEY",
    "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE",
    "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS",
    "POLYBOT2OTHER_LIVE_API_KEY",
    "POLYBOT2OTHER_LIVE_API_SECRET",
    "POLYBOT2OTHER_LIVE_API_PASSPHRASE",
}
_LOADED_ENV_FILES: list[dict[str, Any]] = []


@dataclass(frozen=True)
class Settings:
    """运行配置；实盘密钥只允许从环境变量读取，禁止写入代码仓库。"""

    initial_balance: float = 100.0
    db_path: Path = Path("data/polybot2other-real-btc.sqlite3")
    tick_seconds: float = 2.0
    round_seconds: int = 300
    stake_dollars: float = 5.0
    max_open_trades: int = 2
    max_daily_loss: float = 100.0
    min_confidence: float = 0.62
    min_edge: float = 0.02
    max_entry_price: float = 0.72
    min_price_distance_bps: float = 1.5
    max_spread: float = 0.08
    min_time_left_seconds: int = 20
    max_time_left_seconds: int = 285
    min_ask_size: float = 1.0
    max_quote_age_ms: int = 3000
    live_snapshot_max_age_seconds: float = 8.0
    market_refresh_seconds: float = 2.0
    price_history_limit: int = 180
    request_timeout_seconds: float = 4.0
    paper_entry_order_type: str = "FAK"
    paper_taker_fee_rate: float = 0.07
    paper_gtd_seconds: float = 90.0
    strategy_experiments_enabled: bool = False
    strategy_experiments_db_dir: Path = Path("data/strategy-experiments")
    strategy_experiments_variants: str = ""
    live_trading_db_path: Path = Path("data/live/single_fak_real.sqlite3")
    live_trading_settings_path: Path = Path("data/live/live-settings.json")
    live_trading_chain_id: int = 137
    live_trading_default_initial_balance: float = 20.0
    live_trading_default_stake_dollars: float = 2.0
    live_trading_default_max_daily_loss: float = 6.0
    live_trading_default_max_total_drawdown: float = 12.0
    live_trading_default_retry_count: int = 2
    live_trading_default_retry_delay_ms: int = 250
    live_trading_runtime_enabled: bool = True
    llm_super_agent_enabled: bool = True
    llm_super_agent_api_key: str = ""
    llm_super_agent_base_url: str = "https://api.hao.ai/v1"
    llm_super_agent_model: str = "openai/gpt-5.4-mini"
    llm_super_agent_timeout_seconds: float = 1.2
    llm_super_agent_min_interval_seconds: float = 12.0
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"


def _float_env(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _load_env_files() -> None:
    global _LOADED_ENV_FILES
    _LOADED_ENV_FILES = []
    explicit = os.environ.get(ENV_FILE_NAME, "").strip()
    paths = [Path(explicit)] if explicit else [Path(name) for name in DEFAULT_ENV_FILES]
    for path in paths:
        if path.exists() and path.is_file():
            _LOADED_ENV_FILES.append(_load_env_file(path))


def _load_env_file(path: Path) -> dict[str, Any]:
    loaded_keys: list[str] = []
    skipped_existing: list[str] = []
    empty_keys: list[str] = []
    sensitive_keys_present: list[str] = []
    ignored_keys = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {
            "path": str(path),
            "loaded_keys": [],
            "skipped_existing": [],
            "empty_keys": [],
            "ignored_keys": 0,
            "sensitive_keys_present": [],
            "mode": None,
            "secure_permissions": None,
            "error": "read_failed",
        }
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key.startswith(ENV_KEY_PREFIX) and key not in EXTERNAL_ALLOWED_ENV_KEYS:
            ignored_keys += 1
            continue
        value = _env_file_value(raw_value)
        if key in SENSITIVE_ENV_KEYS and value and key not in sensitive_keys_present:
            sensitive_keys_present.append(key)
        if key in os.environ:
            skipped_existing.append(key)
            continue
        if value == "":
            empty_keys.append(key)
            continue
        os.environ[key] = value
        loaded_keys.append(key)
    security = _env_file_security(path)
    return {
        "path": str(path),
        "loaded_keys": loaded_keys,
        "skipped_existing": skipped_existing,
        "empty_keys": empty_keys,
        "ignored_keys": ignored_keys,
        "sensitive_keys_present": sensitive_keys_present,
        **security,
    }


def _env_file_security(path: Path) -> dict[str, Any]:
    try:
        mode_int = path.stat().st_mode & 0o777
    except OSError:
        return {"mode": None, "secure_permissions": None}
    return {
        "mode": oct(mode_int),
        "secure_permissions": (mode_int & 0o077) == 0,
    }


def _env_file_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def env_file_status() -> list[dict[str, Any]]:
    return [dict(item) for item in _LOADED_ENV_FILES]


def reload_live_credential_env() -> list[dict[str, Any]]:
    loaded_keys = {
        str(key)
        for item in _LOADED_ENV_FILES
        for key in (item.get("loaded_keys") or [])
        if isinstance(key, str)
    }
    for key in sorted(LIVE_CREDENTIAL_ENV_KEYS & loaded_keys):
        os.environ.pop(key, None)
    _load_env_files()
    return env_file_status()


def load_settings() -> Settings:
    _load_env_files()
    return Settings(
        initial_balance=_float_env("POLYBOT2OTHER_INITIAL_BALANCE", 100.0, 1.0),
        db_path=Path(os.environ.get("POLYBOT2OTHER_DB_PATH", "data/polybot2other-real-btc.sqlite3")),
        tick_seconds=_float_env("POLYBOT2OTHER_TICK_SECONDS", 2.0, 0.5),
        round_seconds=_int_env("POLYBOT2OTHER_ROUND_SECONDS", 300, 60),
        stake_dollars=_float_env("POLYBOT2OTHER_STAKE_DOLLARS", 5.0, 0.1),
        max_open_trades=_int_env("POLYBOT2OTHER_MAX_OPEN_TRADES", 2, 1),
        max_daily_loss=_float_env("POLYBOT2OTHER_MAX_DAILY_LOSS", 21.0, 0.0),
        min_confidence=_float_env("POLYBOT2OTHER_MIN_CONFIDENCE", 0.62, 0.5),
        min_edge=_float_env("POLYBOT2OTHER_MIN_EDGE", 0.02, -0.5),
        max_entry_price=_float_env("POLYBOT2OTHER_MAX_ENTRY_PRICE", 0.72, 0.01),
        min_price_distance_bps=_float_env("POLYBOT2OTHER_MIN_PRICE_DISTANCE_BPS", 3.5, 0.0),
        max_spread=_float_env("POLYBOT2OTHER_MAX_SPREAD", 0.08, 0.0),
        min_time_left_seconds=_int_env("POLYBOT2OTHER_MIN_TIME_LEFT_SECONDS", 20, 0),
        max_time_left_seconds=_int_env("POLYBOT2OTHER_MAX_TIME_LEFT_SECONDS", 285, 1),
        min_ask_size=_float_env("POLYBOT2OTHER_MIN_ASK_SIZE", 1.0, 0.0),
        max_quote_age_ms=_int_env("POLYBOT2OTHER_MAX_QUOTE_AGE_MS", 3000, 100),
        live_snapshot_max_age_seconds=_float_env("POLYBOT2OTHER_LIVE_SNAPSHOT_MAX_AGE_SECONDS", 8.0, 1.0),
        market_refresh_seconds=_float_env("POLYBOT2OTHER_MARKET_REFRESH_SECONDS", 2.0, 0.5),
        paper_entry_order_type=os.environ.get("POLYBOT2OTHER_PAPER_ENTRY_ORDER_TYPE", "FAK"),
        paper_taker_fee_rate=_float_env("POLYBOT2OTHER_PAPER_TAKER_FEE_RATE", 0.07, 0.0),
        paper_gtd_seconds=_float_env("POLYBOT2OTHER_PAPER_GTD_SECONDS", 90.0, 1.0),
        strategy_experiments_enabled=_bool_env("POLYBOT2OTHER_STRATEGY_EXPERIMENTS_ENABLED", True),
        strategy_experiments_db_dir=Path(
            os.environ.get("POLYBOT2OTHER_STRATEGY_EXPERIMENTS_DB_DIR", "data/strategy-experiments")
        ),
        strategy_experiments_variants=os.environ.get("POLYBOT2OTHER_STRATEGY_EXPERIMENTS_VARIANTS", ""),
        live_trading_db_path=Path(
            os.environ.get("POLYBOT2OTHER_LIVE_TRADING_DB_PATH", "data/live/single_fak_real.sqlite3")
        ),
        live_trading_settings_path=Path(
            os.environ.get("POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH", "data/live/live-settings.json")
        ),
        live_trading_chain_id=_int_env("POLYBOT2OTHER_LIVE_CHAIN_ID", 137, 1),
        live_trading_default_initial_balance=_float_env("POLYBOT2OTHER_LIVE_DEFAULT_INITIAL_BALANCE", 20.0, 1.0),
        live_trading_default_stake_dollars=_float_env("POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS", 2.0, 0.1),
        live_trading_default_max_daily_loss=_float_env("POLYBOT2OTHER_LIVE_DEFAULT_MAX_DAILY_LOSS", 6.0, 0.0),
        live_trading_default_max_total_drawdown=_float_env("POLYBOT2OTHER_LIVE_DEFAULT_MAX_TOTAL_DRAWDOWN", 12.0, 0.0),
        live_trading_default_retry_count=_int_env("POLYBOT2OTHER_LIVE_DEFAULT_RETRY_COUNT", 2, 0),
        live_trading_default_retry_delay_ms=_int_env("POLYBOT2OTHER_LIVE_DEFAULT_RETRY_DELAY_MS", 250, 0),
        live_trading_runtime_enabled=_bool_env("POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED", True),
        llm_super_agent_enabled=_bool_env("POLYBOT2OTHER_LLM_SUPER_AGENT_ENABLED", True),
        llm_super_agent_api_key=os.environ.get("POLYBOT2OTHER_LLM_API_KEY", os.environ.get("HAOAI_API_KEY", "")),
        llm_super_agent_base_url=os.environ.get("POLYBOT2OTHER_LLM_BASE_URL", "https://api.hao.ai/v1"),
        llm_super_agent_model=os.environ.get("POLYBOT2OTHER_LLM_MODEL", "openai/gpt-5.4-mini"),
        llm_super_agent_timeout_seconds=_float_env("POLYBOT2OTHER_LLM_TIMEOUT_SECONDS", 1.2, 0.2),
        llm_super_agent_min_interval_seconds=_float_env("POLYBOT2OTHER_LLM_MIN_INTERVAL_SECONDS", 12.0, 1.0),
        gamma_url=os.environ.get("POLYBOT2OTHER_GAMMA_URL", "https://gamma-api.polymarket.com"),
        clob_url=os.environ.get("POLYBOT2OTHER_CLOB_URL", "https://clob.polymarket.com"),
        data_api_url=os.environ.get("POLYBOT2OTHER_DATA_API_URL", "https://data-api.polymarket.com"),
    )

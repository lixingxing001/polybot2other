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
DEFAULT_DB_PATH = Path("data/polybot2other-real-btc.sqlite3")
DEFAULT_BTC_RUNTIME_SETTINGS_PATH = Path("data/btc-runtime.json")
DEFAULT_MARKET_SCOUT_SETTINGS_PATH = Path("data/market-scout/settings.json")
DEFAULT_MARKET_SCOUT_PAPER_DB_PATH = Path("data/market-scout/paper.sqlite3")


@dataclass(frozen=True)
class Settings:
    """运行配置；实盘密钥只允许从环境变量读取，禁止写入代码仓库。"""

    initial_balance: float = 100.0
    db_path: Path = DEFAULT_DB_PATH
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
    # BTC 运行态配置文件；用于持久化 BTC 数据采集总开关，避免页面刷新后自动恢复采集。
    btc_runtime_settings_path: Path = DEFAULT_BTC_RUNTIME_SETTINGS_PATH
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
    # 非 BTC 市场扫描器总开关；只读扫描和 LLM 分析，不会触发真实下单。
    market_scout_enabled: bool = True
    # 非 BTC 市场扫描间隔，单位秒；调低会增加 Gamma API 和 LLM 请求频率。
    market_scout_interval_seconds: float = 30.0
    # 每轮从 Gamma API 拉取的活跃市场数量上限；数量越大覆盖越广，网络耗时越高。
    market_scout_scan_limit: int = 120
    # 每轮送入 LLM 的候选市场数量上限；用于控制 token 成本。
    market_scout_analyze_top_n: int = 10
    # 候选市场最低流动性，单位 USD；低于该值只写过滤日志。
    market_scout_min_liquidity: float = 5_000.0
    # 候选市场最低 24h 成交量，单位 USD；低于该值只写过滤日志。
    market_scout_min_volume_24h: float = 1_000.0
    # 同一轮 LLM 分析结果的有效时间，单位秒；有效期内不重复请求同一组候选。
    market_scout_llm_ttl_seconds: float = 120.0
    # 非 BTC 市场 LLM 分析超时，单位秒；只影响只读分析，不影响 BTC 策略 tick。
    market_scout_llm_timeout_seconds: float = 8.0
    # 非 BTC 市场 Evidence Scout 默认开关；开启后会在 LLM 前检索英文新闻证据。
    market_scout_evidence_enabled: bool = True
    # 每轮最多做 Web 证据搜索的候选数量；控制外部搜索请求量。
    market_scout_evidence_max_markets: int = 6
    # 每个市场最多保留的英文新闻证据条数。
    market_scout_evidence_results_per_market: int = 4
    # Evidence Scout 单个搜索请求超时，单位秒。
    market_scout_evidence_timeout_seconds: float = 6.0
    # Evidence Scout 缓存有效期，单位秒；避免候选不变时反复请求搜索入口。
    market_scout_evidence_ttl_seconds: float = 900.0
    # 市场页运行配置文件；保存扫描、LLM、Paper 自动下注和风控参数。
    market_scout_settings_path: Path = DEFAULT_MARKET_SCOUT_SETTINGS_PATH
    # 市场页独立 Paper 账本；和 BTC Paper 账户隔离，避免订单和资金互相污染。
    market_scout_paper_db_path: Path = DEFAULT_MARKET_SCOUT_PAPER_DB_PATH
    # 市场页 Paper 初始资金，单位 USD；页面可调整，调整后按差额修正可用资金。
    market_scout_default_paper_initial_balance: float = 100.0
    # 市场页单笔 Paper 预算，单位 USD；自动下注只使用这个隔离预算。
    market_scout_default_paper_stake_dollars: float = 2.0
    # 市场页最多同时持有的 Paper 市场数量。
    market_scout_default_paper_max_open_positions: int = 3
    # LLM 没有给 RECOMMEND 时是否允许小额 Paper 探针；仅用于样本采集，不连接实盘。
    market_scout_default_paper_probe_enabled: bool = True
    # Paper 探针最多同时持有的市场数量；默认 3，用小额仓位覆盖不同事件族，避免单个长周期市场锁死采样。
    market_scout_default_paper_probe_max_open_positions: int = 3
    # Paper 探针最低本地候选置信度；该置信度由候选分、价差和盘口可执行性折算。
    market_scout_default_paper_probe_min_confidence: float = 0.55
    # Paper 探针最低本地候选分；低分候选即使 LLM 反复 NO_TRADE 也不会采样。
    market_scout_default_paper_probe_min_selection_score: float = 14.0
    # 市场页 24 小时已实现亏损上限，单位 USD；触发后 Paper 自动下注暂停。
    market_scout_default_paper_max_daily_loss: float = 10.0
    # LLM 推荐进入 Paper 自动下注前的最低置信度。
    market_scout_default_paper_min_confidence: float = 0.72
    # LLM 推荐进入 Paper 自动下注前允许的最高买入价。
    market_scout_default_paper_max_entry_price: float = 0.65
    # LLM 推荐进入 Paper 自动下注前允许的最大盘口价差。
    market_scout_default_paper_max_spread: float = 0.04
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
        db_path=Path(os.environ.get("POLYBOT2OTHER_DB_PATH", str(DEFAULT_DB_PATH))),
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
        btc_runtime_settings_path=Path(
            os.environ.get("POLYBOT2OTHER_BTC_RUNTIME_SETTINGS_PATH", str(DEFAULT_BTC_RUNTIME_SETTINGS_PATH))
        ),
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
        market_scout_enabled=_bool_env("POLYBOT2OTHER_MARKET_SCOUT_ENABLED", True),
        market_scout_interval_seconds=_float_env("POLYBOT2OTHER_MARKET_SCOUT_INTERVAL_SECONDS", 30.0, 5.0),
        market_scout_scan_limit=_int_env("POLYBOT2OTHER_MARKET_SCOUT_SCAN_LIMIT", 120, 20),
        market_scout_analyze_top_n=_int_env("POLYBOT2OTHER_MARKET_SCOUT_ANALYZE_TOP_N", 10, 1),
        market_scout_min_liquidity=_float_env("POLYBOT2OTHER_MARKET_SCOUT_MIN_LIQUIDITY", 5_000.0, 0.0),
        market_scout_min_volume_24h=_float_env("POLYBOT2OTHER_MARKET_SCOUT_MIN_VOLUME_24H", 1_000.0, 0.0),
        market_scout_llm_ttl_seconds=_float_env("POLYBOT2OTHER_MARKET_SCOUT_LLM_TTL_SECONDS", 120.0, 10.0),
        market_scout_llm_timeout_seconds=_float_env("POLYBOT2OTHER_MARKET_SCOUT_LLM_TIMEOUT_SECONDS", 8.0, 1.0),
        market_scout_evidence_enabled=_bool_env("POLYBOT2OTHER_MARKET_SCOUT_EVIDENCE_ENABLED", True),
        market_scout_evidence_max_markets=_int_env("POLYBOT2OTHER_MARKET_SCOUT_EVIDENCE_MAX_MARKETS", 6, 0),
        market_scout_evidence_results_per_market=_int_env(
            "POLYBOT2OTHER_MARKET_SCOUT_EVIDENCE_RESULTS_PER_MARKET",
            4,
            1,
        ),
        market_scout_evidence_timeout_seconds=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_EVIDENCE_TIMEOUT_SECONDS",
            6.0,
            1.0,
        ),
        market_scout_evidence_ttl_seconds=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_EVIDENCE_TTL_SECONDS",
            900.0,
            30.0,
        ),
        market_scout_settings_path=Path(
            os.environ.get("POLYBOT2OTHER_MARKET_SCOUT_SETTINGS_PATH", str(DEFAULT_MARKET_SCOUT_SETTINGS_PATH))
        ),
        market_scout_paper_db_path=Path(
            os.environ.get("POLYBOT2OTHER_MARKET_SCOUT_PAPER_DB_PATH", str(DEFAULT_MARKET_SCOUT_PAPER_DB_PATH))
        ),
        market_scout_default_paper_initial_balance=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_INITIAL_BALANCE",
            100.0,
            1.0,
        ),
        market_scout_default_paper_stake_dollars=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_STAKE_DOLLARS",
            2.0,
            0.1,
        ),
        market_scout_default_paper_max_open_positions=_int_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_MAX_OPEN_POSITIONS",
            3,
            1,
        ),
        market_scout_default_paper_probe_enabled=_bool_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_PROBE_ENABLED",
            True,
        ),
        market_scout_default_paper_probe_max_open_positions=_int_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_PROBE_MAX_OPEN_POSITIONS",
            3,
            1,
        ),
        market_scout_default_paper_probe_min_confidence=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_PROBE_MIN_CONFIDENCE",
            0.55,
            0.0,
        ),
        market_scout_default_paper_probe_min_selection_score=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_PROBE_MIN_SELECTION_SCORE",
            14.0,
            0.0,
        ),
        market_scout_default_paper_max_daily_loss=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_MAX_DAILY_LOSS",
            10.0,
            0.0,
        ),
        market_scout_default_paper_min_confidence=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_MIN_CONFIDENCE",
            0.72,
            0.0,
        ),
        market_scout_default_paper_max_entry_price=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_MAX_ENTRY_PRICE",
            0.65,
            0.01,
        ),
        market_scout_default_paper_max_spread=_float_env(
            "POLYBOT2OTHER_MARKET_SCOUT_PAPER_MAX_SPREAD",
            0.04,
            0.0,
        ),
        gamma_url=os.environ.get("POLYBOT2OTHER_GAMMA_URL", "https://gamma-api.polymarket.com"),
        clob_url=os.environ.get("POLYBOT2OTHER_CLOB_URL", "https://clob.polymarket.com"),
        data_api_url=os.environ.get("POLYBOT2OTHER_DATA_API_URL", "https://data-api.polymarket.com"),
    )

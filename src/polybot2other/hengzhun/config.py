"""衡准 V1 设计书第 23 章的 94 项版本化参数；不读取实盘设置。"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json

STRATEGY_ID = "HENGZHUN_TWAP_VALUE_V1"
SPEC_VERSION = "1.0.0"
DEFAULT_CONFIG = {'runtime': {'mode': 'RECORD',
             'live_execution_enabled': False,
             'strategy_id': 'HENGZHUN_TWAP_VALUE_V1',
             'spec_version': '1.0.0',
             'display_timezone': 'Asia/Shanghai',
             'decision_interval_ms': 250,
             'seed': 20261106},
 'market': {'asset': 'BTC',
            'duration_seconds': 300,
            'twap_window_seconds': 60,
            'require_verified_contract': True,
            'external_direction_filter': 'DIAGNOSTIC_ONLY'},
 'data': {'price_source_max_age_ms': 1500,
          'price_receive_max_age_ms': 1000,
          'book_source_max_age_ms': 1000,
          'book_receive_max_age_ms': 1000,
          'cross_book_max_skew_ms': 500,
          'clock_offset_max_ms': 250,
          'integral_max_gap_ms': 2000,
          'recovery_healthy_seconds': 10},
 'prior': {'vol_grid_seconds': 5,
           'vol_fast_window_seconds': 120,
           'vol_slow_window_seconds': 600,
           'vol_fast_min_returns': 20,
           'vol_slow_min_returns': 96,
           'sigma_floor_bps_sqrt_second': '0.25',
           'proxy_history_seconds': 86400,
           'proxy_sample_spacing_seconds': 60,
           'proxy_min_samples': 120,
           'proxy_sd_floor_bps': '1.0',
           'proxy_abs_error_p95_max_bps': '3.0',
           'shock_min_abs_return_bps': '10.0',
           'shock_z_threshold': '6.0',
           'shock_cooldown_seconds': 60,
           'numeric_probability_clip': '0.000000000001'},
 'calibration': {'landmarks_seconds': [90, 75, 60, 45, 30],
                 'landmark_max_lateness_ms': 1000,
                 'train_window_days': 28,
                 'validation_window_days': 7,
                 'test_window_days': 7,
                 'min_train_markets': 3000,
                 'min_train_days': 20,
                 'min_markets_each_outcome': 300,
                 'min_validation_markets': 1000,
                 'logit_clip': '0.0001',
                 'standardization_scale_floor': '0.000000000001',
                 'ridge_lambda': '0.01',
                 'optimizer_max_iterations': 1000,
                 'optimizer_tolerance': '0.00000001',
                 'bootstrap_models': 100,
                 'bootstrap_min_success': 90,
                 'guard_quantiles': ['0.10', '0.90'],
                 'model_error_buffer': '0.02',
                 'max_abs_feature_z': '5.0',
                 'max_age_days': 7,
                 'validation_score_tolerance': '0.01'},
 'entry': {'submit_remaining_min_seconds': 30,
           'submit_remaining_max_seconds': 90,
           'expected_arrival_remaining_min_seconds': 25,
           'minimum_best_ask': '0.20',
           'hard_limit_price_cap': '0.70',
           'max_spread': '0.02',
           'max_slippage_ticks': 1,
           'minimum_guard_edge': '0.03',
           'minimum_guard_roi': '0.05',
           'budget_usd': '5.00',
           'max_intents_per_market': 1,
           'value_tie_tolerance_usd': '0.000000001'},
 'risk': {'initial_paper_cash_usd': '100.00',
          'max_open_risk_positions': 1,
          'max_committed_cost_usd': '10.00',
          'max_claim_pending_markets': 2,
          'gross_loss_24h_plus_risk_cap_usd': '10.00',
          'maximum_high_water_drawdown': '0.15',
          'wallet_single_flight': True,
          'wallet_market_single_owner': True},
 'paper': {'execution_evidence': 'ASSUMED',
           'default_scenario': 'S1',
           'total_delay_ms': 1000,
           'depth_haircut': '0.50',
           'confirm_after_fill_ms': 12000,
           'claim_after_final_ms': 60000,
           'book_model': 'LOCAL_OBSERVED_ASOF',
           'require_post_submit_book': True},
 'research': {'screening_test_weeks': 4,
              'required_test_weeks': 6,
              'minimum_shadow_filled_markets': 1000,
              'minimum_portfolio_filled_markets': 100,
              'bootstrap_repetitions': 20000,
              'bootstrap_block_days': 3,
              'bootstrap_sensitivity_blocks_days': [1, 7],
              'registered_candidate_count': 1,
              'maximum_candidate_count': 12,
              'family_alpha': '0.05'}}
SCENARIOS = {"S1": {"total_delay_ms": 1000, "depth_haircut": "0.50"},
             "S2": {"total_delay_ms": 2000, "depth_haircut": "0.25"},
             "S3": {"total_delay_ms": 5000, "depth_haircut": "0.25"}}
DEFAULT_CONFIG["paper"]["scenarios"] = [{"id": key, **values} for key, values in SCENARIOS.items()]


def validate_config(config):
    if set(config) != set(DEFAULT_CONFIG):
        raise ValueError("CONFIG_SCHEMA: unknown or missing group")
    for group, defaults in DEFAULT_CONFIG.items():
        values = config[group]
        if not isinstance(values, dict) or set(values) != set(defaults):
            raise ValueError("CONFIG_SCHEMA: " + group)
        for key, default in defaults.items():
            value = values[key]
            if type(value) is not type(default):
                raise ValueError("CONFIG_TYPE: " + group + "." + key)
            if isinstance(default, int) and not isinstance(default, bool) and value <= 0:
                raise ValueError("CONFIG_RANGE: " + group + "." + key)
            if isinstance(default, str):
                try: Decimal(default)
                except InvalidOperation: continue
                try: parsed = Decimal(value)
                except InvalidOperation: raise ValueError("CONFIG_DECIMAL: " + key) from None
                if not parsed.is_finite() or parsed <= 0:
                    raise ValueError("CONFIG_RANGE: " + key)
    runtime = config["runtime"]
    if runtime["live_execution_enabled"] is not False or runtime["mode"] not in ("RECORD", "REPLAY", "PAPER"):
        raise ValueError("LIVE_EXECUTION_DISABLED")
    if runtime["strategy_id"] != STRATEGY_ID or runtime["spec_version"] != SPEC_VERSION:
        raise ValueError("STRATEGY_IDENTITY_MISMATCH")
    market = config["market"]
    if (market["asset"] != "BTC" or market["duration_seconds"] != 300 or market["twap_window_seconds"] != 60
            or market["require_verified_contract"] is not True or market["external_direction_filter"] != "DIAGNOSTIC_ONLY"):
        raise ValueError("CONTRACT_UNSUPPORTED")
    if config["entry"]["max_intents_per_market"] != 1:
        raise ValueError("ONE_INTENT_PER_MARKET_REQUIRED")
    if not config["risk"]["wallet_single_flight"] or not config["risk"]["wallet_market_single_owner"]:
        raise ValueError("WALLET_INVARIANT_REQUIRED")
    if config["paper"]["execution_evidence"] != "ASSUMED" or config["paper"]["book_model"] != "LOCAL_OBSERVED_ASOF" or not config["paper"]["require_post_submit_book"]:
        raise ValueError("UNSUPPORTED_EXECUTION_CONTRACT")
    if config["paper"]["default_scenario"] not in SCENARIOS:
        raise ValueError("SCENARIO_UNKNOWN")
    if config["paper"]["scenarios"] != DEFAULT_CONFIG["paper"]["scenarios"]:
        raise ValueError("SCENARIO_REGISTRATION_MISMATCH")
    c = config["calibration"]
    if c["bootstrap_min_success"] > c["bootstrap_models"]:
        raise ValueError("BOOTSTRAP_CONFIGURATION")
    if not isinstance(c["landmarks_seconds"], list) or any(type(x) is not int or not 0 < x < 300 for x in c["landmarks_seconds"]) or len(set(c["landmarks_seconds"])) != len(c["landmarks_seconds"]):
        raise ValueError("LANDMARK_CONFIGURATION")
    if c["guard_quantiles"] != ["0.10", "0.90"]:
        raise ValueError("UNSUPPORTED_GUARD_QUANTILES")
    if config["research"]["registered_candidate_count"] > config["research"]["maximum_candidate_count"] or config["research"]["maximum_candidate_count"] > 12:
        raise ValueError("CANDIDATE_REGISTRATION_LIMIT")
    for group, key in (("prior", "numeric_probability_clip"), ("calibration", "logit_clip"),
                       ("calibration", "model_error_buffer"), ("entry", "hard_limit_price_cap"),
                       ("risk", "maximum_high_water_drawdown"), ("paper", "depth_haircut"),
                       ("research", "family_alpha")):
        if not Decimal(0) < Decimal(config[group][key]) < Decimal(1):
            raise ValueError("CONFIG_PROBABILITY_RANGE: " + key)
    if config["entry"]["submit_remaining_min_seconds"] > config["entry"]["submit_remaining_max_seconds"]:
        raise ValueError("ENTRY_TIME_RANGE")
    from .contracts import validate_record
    validate_record(config, "config.schema.json")
    return config


def load_config(overrides=None, *, scenario=None):
    config = deepcopy(DEFAULT_CONFIG)
    for group, values in (overrides or {}).items():
        if group not in config or not isinstance(values, dict):
            raise ValueError("CONFIG_SCHEMA: " + str(group))
        for key, value in values.items():
            if key not in config[group]: raise ValueError("CONFIG_SCHEMA: " + group + "." + key)
            config[group][key] = deepcopy(value)
    if scenario is not None:
        if scenario not in SCENARIOS: raise ValueError("SCENARIO_UNKNOWN")
        config["paper"].update(SCENARIOS[scenario]); config["paper"]["default_scenario"] = scenario
    return validate_config(config)


def config_hash(config):
    validate_config(config)
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

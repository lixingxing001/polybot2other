from __future__ import annotations

import argparse
import json
import shlex
import time
import urllib.parse
import urllib.request
from typing import Any

from .bot import LIVE_ONCE_CONFIRM_PHRASE, LIVE_ONCE_WAITABLE_BLOCKERS, PaperTradingBot
from .config import Settings, load_settings
from .storage import TradeStore


EVIDENCE_COMMAND = (
    "rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_evidence "
    "--external-order-id OFFICIAL_ORDER_ID --pretty"
)

BLOCKER_ACTIONS = {
    "runtime": "确认 POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=true，并重启当前服务进程。",
    "enabled": "one-shot 首单要求实盘开关保持关闭；若做常驻实盘，才需要在预检通过后打开顶部实盘开关。",
    "process_lock": "停止指向同一个 live-settings.json 的重复服务进程，确保只有一个进程能持有实盘锁。",
    "compliance_acknowledged": "在页面勾选实盘风险确认并保存，或 one-shot 命令带 --acknowledge-compliance。",
    "geo_access": "当前运行地区未通过 Polymarket 访问检查；不要绕过访问限制，换到合规运行环境后重试。",
    "credentials": "填写 .env.live 中的 PRIVATE_KEY、SIGNATURE_TYPE、FUNDER_ADDRESS；API_KEY/SECRET/PASSPHRASE 要么三项都填，要么三项都留空让 SDK 派生。",
    "market": "等待当前 BTC 5m 市场刷新，或重新执行带 refresh 的预检/doctor。",
    "target_price": "等待 Polymarket 官方 market.target_price 出现；缺目标价时禁止实盘下注。",
    "signal": "当前 SINGLE_FAK_REAL 策略没有 Up/Down 信号；one-shot 可用 wait-ready 等待短暂信号窗口。",
    "software_cash": "降低 stake_dollars，或提高 live 初始软件预算；软件隔离账户可用资金必须覆盖本次下注。",
    "strategy_risk": "检查最大持仓、单日亏损、总回撤、同市场待确认订单和重复方向限制。",
    "min_order_size": "提高单笔 stake，或等待市场最小订单要求可被当前预算覆盖。",
    "orderbook_depth": "等待盘口深度恢复；FAK 可成交份额不足时不提交真实订单。",
    "official_open_orders_clear": "先刷新官方挂单；如确认要清空，执行实盘急停 cancel_all，直到官方 open orders 为 0。",
    "collateral_wallet": "给 funder 钱包补足 Polymarket collateral/pUSD，并完成对应 CLOB collateral allowance 授权。",
    "sign_market_order": "检查 SDK 版本、signature_type、funder、token_id、tick_size 和 neg_risk；签名预检失败时禁止下单。",
    "live_switch_must_be_off": "先关闭顶部实盘开关；one-shot 首单要求从关闭状态开始。",
    "preflight": "读取 live_preflight 失败；先修复返回的异常，再考虑真实下单。",
}


def build_live_doctor_payload(
    settings: Settings | None = None,
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    resolved_settings = settings or load_settings()
    store = TradeStore(resolved_settings.db_path, resolved_settings.initial_balance)
    bot = PaperTradingBot(resolved_settings, store)
    return build_live_doctor_from_bot(bot, refresh=refresh)


def build_live_doctor_from_bot(bot: PaperTradingBot, *, refresh: bool = True) -> dict[str, Any]:
    settings_payload = bot.live_settings()
    preflight_payload: dict[str, Any] = {}
    preflight: dict[str, Any] | None = None
    preflight_error: str | None = None
    try:
        preflight_payload = bot.refresh_live_preflight() if refresh else bot.live_preflight()
        maybe_preflight = preflight_payload.get("live_preflight")
        if isinstance(maybe_preflight, dict):
            preflight = maybe_preflight
        else:
            preflight_error = "live_preflight payload missing"
    except Exception as exc:  # noqa: BLE001 - doctor 必须把只读检查失败原因机器可读返回。
        preflight_error = f"{type(exc).__name__}: {exc}"
    snapshot = preflight_payload.get("snapshot") if isinstance(preflight_payload.get("snapshot"), dict) else bot.snapshot()
    doctor = _build_doctor(settings_payload, preflight, preflight_error)
    return {"live_doctor": doctor, "snapshot": snapshot}


def fetch_live_doctor_from_service(
    service_url: str,
    *,
    refresh: bool = True,
    include_snapshot: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = _service_live_doctor_url(service_url, refresh=refresh, include_snapshot=include_snapshot)
    with urllib.request.urlopen(url, timeout=max(0.5, float(timeout))) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("live_doctor"), dict):
        raise RuntimeError("service /api/live-doctor response missing live_doctor")
    _attach_service_first_order_command(payload, service_url)
    return payload


def _service_live_doctor_url(service_url: str, *, refresh: bool, include_snapshot: bool) -> str:
    base = str(service_url or "").strip()
    if not base:
        raise ValueError("service_url is required")
    parts = urllib.parse.urlsplit(base)
    if not parts.scheme or not parts.netloc:
        raise ValueError("service_url must include scheme and host, for example http://127.0.0.1:8791")
    path = parts.path.rstrip("/")
    if not path or path == "/":
        path = "/api/live-doctor"
    elif not path.endswith("/api/live-doctor"):
        path = f"{path}/api/live-doctor"
    query_items = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query_items["refresh"] = "true" if refresh else "false"
    query_items["include_snapshot"] = "true" if include_snapshot else "false"
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            path,
            urllib.parse.urlencode(query_items),
            "",
        )
    )


def _attach_service_first_order_command(payload: dict[str, Any], service_url: str) -> None:
    doctor = payload.get("live_doctor") if isinstance(payload.get("live_doctor"), dict) else {}
    first_order = doctor.get("first_order") if isinstance(doctor.get("first_order"), dict) else {}
    stake = _positive_float(first_order.get("max_stake_dollars"))
    if stake is not None:
        first_order["recommended_service_cli"] = _first_order_service_command(stake, service_url)
    evidence = doctor.get("post_order_evidence") if isinstance(doctor.get("post_order_evidence"), dict) else None
    if evidence is None:
        evidence = {}
        doctor["post_order_evidence"] = evidence
    evidence["standalone_service_cli"] = _evidence_service_command(service_url)


def _build_doctor(
    settings_payload: dict[str, Any],
    preflight: dict[str, Any] | None,
    preflight_error: str | None,
) -> dict[str, Any]:
    blocked_checks = _blocked_checks(preflight, preflight_error)
    blocked_keys = _unique([str(row.get("key") or "") for row in blocked_checks if row.get("key")])
    enabled = bool(settings_payload.get("enabled"))
    one_shot_blockers = [key for key in blocked_keys if key != "enabled"]
    if enabled:
        one_shot_blockers.append("live_switch_must_be_off")
    one_shot_blockers = _unique(one_shot_blockers)
    fatal_one_shot_blockers = [key for key in one_shot_blockers if key not in LIVE_ONCE_WAITABLE_BLOCKERS]
    waitable_one_shot_blockers = [key for key in one_shot_blockers if key in LIVE_ONCE_WAITABLE_BLOCKERS]
    ready_for_one_shot_now = bool(preflight is not None and not one_shot_blockers)
    can_wait_for_one_shot = bool(preflight is not None and not enabled and not fatal_one_shot_blockers)
    ready_for_live_loop = bool(preflight and preflight.get("can_place_next_order"))
    status = "BLOCKED"
    if ready_for_live_loop:
        status = "READY_FOR_LIVE_LOOP"
    elif ready_for_one_shot_now:
        status = "READY_FOR_ONE_SHOT_NOW"
    elif can_wait_for_one_shot:
        status = "READY_FOR_ONE_SHOT_WAIT"

    readiness = settings_payload.get("readiness") if isinstance(settings_payload.get("readiness"), dict) else {}
    open_orders = settings_payload.get("open_orders") if isinstance(settings_payload.get("open_orders"), dict) else {}
    first_order_stake = _first_order_max_stake(settings_payload, preflight)
    stake_requirement = _stake_requirement(first_order_stake, preflight)
    credential_setup = _credential_setup(readiness)
    sdk_status = readiness.get("sdk_status") if isinstance(readiness.get("sdk_status"), dict) else {}
    return {
        "checked_at": time.time(),
        "status": status,
        "execution_mode": "LIVE",
        "variant_id": settings_payload.get("variant_id"),
        "combo": settings_payload.get("combo"),
        "sdk": readiness.get("sdk"),
        "sdk_version": readiness.get("sdk_version"),
        "sdk_status": sdk_status,
        "enabled": enabled,
        "ready_for_one_shot_now": ready_for_one_shot_now,
        "can_wait_for_one_shot": can_wait_for_one_shot,
        "ready_for_live_loop": ready_for_live_loop,
        "one_shot_blockers": one_shot_blockers,
        "fatal_one_shot_blockers": fatal_one_shot_blockers,
        "waitable_one_shot_blockers": waitable_one_shot_blockers,
        "blocked_checks": blocked_checks,
        "next_actions": _next_actions(blocked_checks, status),
        "first_order": {
            "confirm_phrase": LIVE_ONCE_CONFIRM_PHRASE,
            "max_stake_dollars": first_order_stake,
            "stake_requirement": stake_requirement,
            "recommended_cli": _first_order_command(first_order_stake),
            "recommended_api": {
                "method": "POST",
                "path": "/api/live-once",
                "body": {
                    "confirm": LIVE_ONCE_CONFIRM_PHRASE,
                    "acknowledge_compliance": True,
                    "max_stake_dollars": first_order_stake,
                    "disable_after": True,
                    "wait_ready_seconds": 180,
                    "ready_poll_seconds": 2,
                    "reconcile_wait_seconds": 20,
                    "include_evidence": True,
                },
            },
        },
        "post_order_evidence": {
            "expected_response_field": "live_once.evidence",
            "standalone_cli": EVIDENCE_COMMAND,
            "checklist": [
                "live_once.submitted=true",
                "last_order.order_id 或 reconcile.external_order_id 有官方订单 id",
                "live_once.evidence.order.external_order_id 与官方订单 id 一致",
                "订单状态不是无解释的 PENDING；如仍 PENDING，继续用 live_evidence 重查",
                "official_open_orders.count 与预期一致，FAK 首单正常应为 0 或很快归零",
                "software_account.cash_balance / open_trades 与官方成交金额匹配",
            ],
        },
        "credential_setup": credential_setup,
        "summary": {
            "readiness_ready": bool(readiness.get("ready")),
            "readiness_errors": list(readiness.get("errors") or []),
            "sdk": readiness.get("sdk"),
            "sdk_version": readiness.get("sdk_version"),
            "sdk_status": sdk_status,
            "credential_presence": readiness.get("credential_presence") or {},
            "credential_addresses": readiness.get("credential_addresses") or {},
            "env_files": readiness.get("env_files") or [],
            "wallet": readiness.get("wallet") if isinstance(readiness.get("wallet"), dict) else None,
            "geo_check": readiness.get("geo_check") if isinstance(readiness.get("geo_check"), dict) else None,
            "official_open_orders": {
                "ready": open_orders.get("ready"),
                "skipped": open_orders.get("skipped"),
                "count": open_orders.get("count"),
                "errors": list(open_orders.get("errors") or []),
            },
            "market": preflight.get("market") if isinstance(preflight, dict) else None,
            "signal": preflight.get("signal") if isinstance(preflight, dict) else None,
            "entry": preflight.get("entry") if isinstance(preflight, dict) else None,
            "software_account": preflight.get("software_account") if isinstance(preflight, dict) else None,
        },
    }


def _blocked_checks(preflight: dict[str, Any] | None, preflight_error: str | None) -> list[dict[str, Any]]:
    if preflight_error:
        return [{"key": "preflight", "message": preflight_error, "errors": [preflight_error]}]
    if not isinstance(preflight, dict):
        return [{"key": "preflight", "message": "live_preflight unavailable", "errors": ["live_preflight unavailable"]}]
    rows = preflight.get("blocked_checks")
    if not isinstance(rows, list):
        return []
    blocked = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        if not key:
            continue
        blocked_row = {
            "key": key,
            "message": str(row.get("message") or ""),
            "errors": list(row.get("errors") or []),
        }
        for extra_key, value in row.items():
            if extra_key in {"key", "status", "message", "errors"}:
                continue
            blocked_row[str(extra_key)] = value
        blocked.append(blocked_row)
    return blocked


def _next_actions(blocked_checks: list[dict[str, Any]], status: str) -> list[dict[str, str]]:
    if status in {"READY_FOR_ONE_SHOT_NOW", "READY_FOR_ONE_SHOT_WAIT"}:
        return [
            {
                "key": "first_order",
                "action": "执行推荐的 one-shot 首单命令；保持 disable_after=true，首单后先核对 live_once.evidence。",
            }
        ]
    actions = []
    for key in _unique([str(row.get("key") or "") for row in blocked_checks]):
        row = next((item for item in blocked_checks if item.get("key") == key), {})
        actions.append({"key": key, "action": _blocker_action(key, row)})
    return actions


def _blocker_action(key: str, row: dict[str, Any]) -> str:
    if key == "min_order_size":
        stake = _positive_float(row.get("stake"))
        min_order_size = _positive_float(row.get("min_order_size"))
        shortfall = _positive_float(row.get("shortfall"))
        if stake is not None and min_order_size is not None:
            suffix = f"当前 stake {stake:.2f}，官方最小订单 {min_order_size:.2f}"
            if shortfall is not None and shortfall > 0:
                suffix += f"，缺口 {shortfall:.2f}"
            return f"将单笔 stake_dollars 提高到至少 {min_order_size:.2f}，保存设置后重新执行首单检查；{suffix}。"
    return BLOCKER_ACTIONS.get(key, "查看 blocked_checks 里的 message/errors 后修复该阻断项。")


REQUIRED_CREDENTIAL_KEYS = {
    "private_key": "POLYBOT2OTHER_LIVE_PRIVATE_KEY",
    "signature_type": "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE",
    "funder_address": "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS",
}

API_CREDENTIAL_KEYS = [
    "POLYBOT2OTHER_LIVE_API_KEY",
    "POLYBOT2OTHER_LIVE_API_SECRET",
    "POLYBOT2OTHER_LIVE_API_PASSPHRASE",
]


def _credential_setup(readiness: dict[str, Any]) -> dict[str, Any]:
    presence = readiness.get("credential_presence") if isinstance(readiness.get("credential_presence"), dict) else {}
    env_files = readiness.get("env_files") if isinstance(readiness.get("env_files"), list) else []
    missing_required = [
        env_name for presence_key, env_name in REQUIRED_CREDENTIAL_KEYS.items() if not bool(presence.get(presence_key))
    ]
    readiness_errors = [str(item) for item in readiness.get("errors") or []]
    for env_name in REQUIRED_CREDENTIAL_KEYS.values():
        if env_name not in missing_required and any(env_name in error for error in readiness_errors):
            missing_required.append(env_name)
    empty_keys = _unique(
        [
            str(key)
            for item in env_files
            if isinstance(item, dict)
            for key in item.get("empty_keys") or []
            if key
        ]
    )
    loaded_keys = _unique(
        [
            str(key)
            for item in env_files
            if isinstance(item, dict)
            for key in item.get("loaded_keys") or []
            if key
        ]
    )
    insecure_secret_files = [
        {"path": str(item.get("path") or ""), "mode": item.get("mode")}
        for item in env_files
        if isinstance(item, dict)
        and item.get("secure_permissions") is False
        and list(item.get("sensitive_keys_present") or [])
    ]
    api_mode = "derive_api_creds_with_private_key"
    if presence.get("api_creds_partial"):
        api_mode = "partial_api_creds_blocking"
    elif presence.get("api_creds_complete"):
        api_mode = "env_api_creds"
    next_step = "credentials_ready"
    if insecure_secret_files:
        first = insecure_secret_files[0]
        next_step = f"先修复密钥文件权限: chmod 600 {first.get('path') or '.env.live'}"
    elif missing_required:
        next_step = "填写缺失字段: " + ", ".join(missing_required)
    elif presence.get("api_creds_partial"):
        next_step = "API credentials 必须三项都填，或三项都留空让 SDK 派生"
    return {
        "required_keys": list(REQUIRED_CREDENTIAL_KEYS.values()),
        "optional_api_keys": list(API_CREDENTIAL_KEYS),
        "missing_required_keys": missing_required,
        "empty_keys": empty_keys,
        "loaded_keys": loaded_keys,
        "api_credentials_mode": api_mode,
        "env_file_security_ready": not insecure_secret_files,
        "insecure_secret_files": insecure_secret_files,
        "next_step": next_step,
    }


def _first_order_max_stake(settings_payload: dict[str, Any], preflight: dict[str, Any] | None) -> float:
    if isinstance(preflight, dict):
        for container_key in ("entry", "software_account"):
            container = preflight.get(container_key)
            if isinstance(container, dict):
                value = _positive_float(container.get("stake"))
                if value is not None:
                    return _round_stake(value)
    value = _positive_float(settings_payload.get("stake_dollars"))
    if value is not None:
        return _round_stake(value)
    return 2.0


def _stake_requirement(first_order_stake: float, preflight: dict[str, Any] | None) -> dict[str, Any]:
    entry = preflight.get("entry") if isinstance(preflight, dict) else None
    software_account = preflight.get("software_account") if isinstance(preflight, dict) else None
    min_order_size = _positive_float(entry.get("min_order_size")) if isinstance(entry, dict) else None
    shortfall = max(0.0, min_order_size - first_order_stake) if min_order_size is not None else 0.0
    locked_to_current_market = (
        bool(software_account.get("stake_locked_to_current_market")) if isinstance(software_account, dict) else False
    )
    requirement = {
        "stake_dollars": _round_stake(first_order_stake),
        "min_order_size": _round_amount(min_order_size) if min_order_size is not None else None,
        "meets_min_order_size": bool(min_order_size is None or first_order_stake + 1e-9 >= min_order_size),
        "shortfall": _round_amount(shortfall),
        "suggested_stake_dollars": _round_stake(max(first_order_stake, min_order_size or first_order_stake)),
        "stake_locked_to_current_market": locked_to_current_market,
        "can_fix_by_settings_update": bool(shortfall > 0 and not locked_to_current_market),
    }
    if requirement["can_fix_by_settings_update"]:
        requirement["recommended_settings_patch"] = {
            "stake_dollars": requirement["suggested_stake_dollars"],
        }
    return requirement


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _round_stake(value: float) -> float:
    return round(max(0.1, float(value)), 6)


def _round_amount(value: float) -> float:
    return round(max(0.0, float(value)), 6)


def _cli_number(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _first_order_command(max_stake_dollars: float) -> str:
    return (
        "rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once "
        "--confirm-real-order --acknowledge-compliance "
        f"--max-stake {_cli_number(max_stake_dollars)} "
        "--wait-ready-seconds 180 --wait-reconcile-seconds 20 --require-submitted --pretty"
    )


def _first_order_service_command(max_stake_dollars: float, service_url: str) -> str:
    return (
        "rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once "
        f"--service-url {shlex.quote(str(service_url).strip())} "
        "--confirm-real-order --acknowledge-compliance "
        f"--max-stake {_cli_number(max_stake_dollars)} "
        "--wait-ready-seconds 180 --wait-reconcile-seconds 20 --require-submitted --pretty"
    )


def _evidence_service_command(service_url: str) -> str:
    return (
        "rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_evidence "
        f"--service-url {shlex.quote(str(service_url).strip())} "
        "--external-order-id OFFICIAL_ORDER_ID --pretty"
    )


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize whether SINGLE_FAK_REAL is ready for the first live order")
    parser.add_argument("--no-refresh", action="store_true", help="Use the current local snapshot without REST refresh")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--include-snapshot", action="store_true", help="Include the full dashboard snapshot in output")
    parser.add_argument(
        "--service-url",
        help="Read /api/live-doctor from a running dashboard service instead of creating a fresh local bot",
    )
    parser.add_argument("--service-timeout", type=float, default=10.0, help="HTTP timeout for --service-url")
    parser.add_argument(
        "--require-one-shot-ready",
        action="store_true",
        help="Exit with code 2 unless one-shot can submit now or wait only on transient blockers",
    )
    args = parser.parse_args(argv)

    try:
        if args.service_url:
            payload = fetch_live_doctor_from_service(
                args.service_url,
                refresh=not args.no_refresh,
                include_snapshot=args.include_snapshot,
                timeout=args.service_timeout,
            )
        else:
            payload = build_live_doctor_payload(refresh=not args.no_refresh)
        if not args.include_snapshot and isinstance(payload, dict):
            payload = {"live_doctor": payload.get("live_doctor")}
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - command output must stay machine-readable.
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        exit_code = 1

    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=bool(args.pretty)))
    if exit_code:
        return exit_code
    doctor = payload.get("live_doctor") if isinstance(payload, dict) else {}
    if args.require_one_shot_ready and not (
        isinstance(doctor, dict) and (doctor.get("ready_for_one_shot_now") or doctor.get("can_wait_for_one_shot"))
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .bot import LIVE_ONCE_CONFIRM_PHRASE, LiveOnceBlockedError, PaperTradingBot
from .config import Settings, load_settings
from .storage import TradeStore


def build_live_once_payload(
    settings: Settings | None = None,
    *,
    confirm: str,
    max_stake_dollars: float,
    acknowledge_compliance: bool = False,
    disable_after: bool = True,
    refresh: bool = True,
    reconcile_wait_seconds: float = 0.0,
    reconcile_poll_seconds: float = 1.0,
    wait_ready_seconds: float = 0.0,
    ready_poll_seconds: float = 2.0,
    include_evidence: bool = True,
) -> dict[str, Any]:
    resolved_settings = settings or load_settings()
    store = TradeStore(resolved_settings.db_path, resolved_settings.initial_balance)
    bot = PaperTradingBot(resolved_settings, store)
    return bot.run_live_once(
        confirm=confirm,
        max_stake_dollars=max_stake_dollars,
        acknowledge_compliance=acknowledge_compliance,
        disable_after=disable_after,
        refresh=refresh,
        reconcile_wait_seconds=reconcile_wait_seconds,
        reconcile_poll_seconds=reconcile_poll_seconds,
        wait_ready_seconds=wait_ready_seconds,
        ready_poll_seconds=ready_poll_seconds,
        include_evidence=include_evidence,
    )


class LiveOnceServiceError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("error") or f"service returned HTTP {status}"))
        self.status = status
        self.payload = payload


def post_live_once_to_service(
    service_url: str,
    *,
    confirm: str,
    max_stake_dollars: float,
    acknowledge_compliance: bool = False,
    disable_after: bool = True,
    refresh: bool = True,
    reconcile_wait_seconds: float = 0.0,
    reconcile_poll_seconds: float = 1.0,
    wait_ready_seconds: float = 0.0,
    ready_poll_seconds: float = 2.0,
    include_evidence: bool = True,
    timeout: float = 1800.0,
) -> dict[str, Any]:
    url = _service_live_once_url(service_url)
    body = {
        "confirm": confirm,
        "max_stake_dollars": max_stake_dollars,
        "acknowledge_compliance": acknowledge_compliance,
        "disable_after": disable_after,
        "refresh": refresh,
        "reconcile_wait_seconds": reconcile_wait_seconds,
        "reconcile_poll_seconds": reconcile_poll_seconds,
        "wait_ready_seconds": wait_ready_seconds,
        "ready_poll_seconds": ready_poll_seconds,
        "include_evidence": include_evidence,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = _decode_service_payload(raw)
        payload.setdefault("http_status", exc.code)
        raise LiveOnceServiceError(exc.code, payload) from exc
    payload = _decode_service_payload(raw)
    if not isinstance(payload.get("live_once"), dict):
        raise RuntimeError("service /api/live-once response missing live_once")
    return payload


def _service_live_once_url(service_url: str) -> str:
    base = str(service_url or "").strip()
    if not base:
        raise ValueError("service_url is required")
    parts = urllib.parse.urlsplit(base)
    if not parts.scheme or not parts.netloc:
        raise ValueError("service_url must include scheme and host, for example http://127.0.0.1:8791")
    path = parts.path.rstrip("/")
    if not path or path == "/":
        path = "/api/live-once"
    elif not path.endswith("/api/live-once"):
        path = f"{path}/api/live-once"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _decode_service_payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - CLI 输出必须保持 JSON。
        return {"ok": False, "error": f"invalid service JSON response: {type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "service response is not a JSON object"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one controlled SINGLE_FAK_REAL live execution attempt")
    parser.add_argument("--no-refresh", action="store_true", help="Use the current local snapshot without REST refresh")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument(
        "--service-url",
        help="POST /api/live-once to a running dashboard service instead of creating a fresh local bot",
    )
    parser.add_argument("--service-timeout", type=float, default=1800.0, help="HTTP timeout for --service-url")
    parser.add_argument(
        "--confirm-real-order",
        action="store_true",
        help=f"Required. Confirms this command may submit one real Polymarket order ({LIVE_ONCE_CONFIRM_PHRASE})",
    )
    parser.add_argument(
        "--acknowledge-compliance",
        action="store_true",
        help="Set the same compliance/risk acknowledgement used by the dashboard before running",
    )
    parser.add_argument(
        "--max-stake",
        type=float,
        required=True,
        help="Abort unless the one-shot live stake is at or below this collateral/pUSD dollar amount",
    )
    parser.add_argument(
        "--leave-enabled",
        action="store_true",
        help="Leave live trading enabled after the one-shot run. Default disables it again.",
    )
    parser.add_argument(
        "--require-submitted",
        action="store_true",
        help="Exit with code 2 unless the one-shot run submitted an order attempt",
    )
    parser.add_argument(
        "--wait-reconcile-seconds",
        type=float,
        default=0.0,
        help="After submission, poll official order/trade reconciliation for up to this many seconds",
    )
    parser.add_argument(
        "--reconcile-poll-seconds",
        type=float,
        default=1.0,
        help="Polling interval for --wait-reconcile-seconds",
    )
    parser.add_argument(
        "--wait-ready-seconds",
        type=float,
        default=0.0,
        help="Before submitting, poll preflight for transient market/target/signal/depth blockers for up to this many seconds",
    )
    parser.add_argument(
        "--ready-poll-seconds",
        type=float,
        default=2.0,
        help="Polling interval for --wait-ready-seconds",
    )
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="Do not include the read-only post-order evidence package in successful output",
    )
    args = parser.parse_args(argv)

    confirm = LIVE_ONCE_CONFIRM_PHRASE if args.confirm_real_order else ""
    try:
        kwargs = {
            "confirm": confirm,
            "max_stake_dollars": args.max_stake,
            "acknowledge_compliance": args.acknowledge_compliance,
            "disable_after": not args.leave_enabled,
            "refresh": not args.no_refresh,
            "reconcile_wait_seconds": args.wait_reconcile_seconds,
            "reconcile_poll_seconds": args.reconcile_poll_seconds,
            "wait_ready_seconds": args.wait_ready_seconds,
            "ready_poll_seconds": args.ready_poll_seconds,
            "include_evidence": not args.no_evidence,
        }
        if args.service_url:
            payload = post_live_once_to_service(args.service_url, timeout=args.service_timeout, **kwargs)
        else:
            payload = build_live_once_payload(**kwargs)
        exit_code = 0
    except LiveOnceServiceError as exc:
        payload = exc.payload
        exit_code = 2
    except LiveOnceBlockedError as exc:
        payload = exc.payload
        exit_code = 2
    except ValueError as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        exit_code = 1
    except Exception as exc:  # noqa: BLE001 - command output must remain machine-readable.
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        exit_code = 2

    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=bool(args.pretty)))
    if exit_code:
        return exit_code
    live_once = payload.get("live_once") if isinstance(payload, dict) else {}
    if args.require_submitted and not (isinstance(live_once, dict) and live_once.get("submitted")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

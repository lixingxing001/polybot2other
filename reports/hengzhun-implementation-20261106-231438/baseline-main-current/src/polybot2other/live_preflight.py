from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any

from .bot import PaperTradingBot
from .config import Settings, load_settings
from .storage import TradeStore


def build_live_preflight_payload(settings: Settings | None = None, *, refresh: bool = True) -> dict[str, Any]:
    resolved_settings = settings or load_settings()
    store = TradeStore(resolved_settings.db_path, resolved_settings.initial_balance)
    bot = PaperTradingBot(resolved_settings, store)
    if refresh:
        return bot.refresh_live_preflight()
    return bot.live_preflight()


def fetch_live_preflight_from_service(
    service_url: str,
    *,
    include_snapshot: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = _service_live_preflight_url(service_url, include_snapshot=include_snapshot)
    with urllib.request.urlopen(url, timeout=max(0.5, float(timeout))) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("live_preflight"), dict):
        raise RuntimeError("service /api/live-preflight response missing live_preflight")
    return payload


def _service_live_preflight_url(service_url: str, *, include_snapshot: bool) -> str:
    base = str(service_url or "").strip()
    if not base:
        raise ValueError("service_url is required")
    parts = urllib.parse.urlsplit(base)
    if not parts.scheme or not parts.netloc:
        raise ValueError("service_url must include scheme and host, for example http://127.0.0.1:8791")
    path = parts.path.rstrip("/")
    if not path or path == "/":
        path = "/api/live-preflight"
    elif not path.endswith("/api/live-preflight"):
        path = f"{path}/api/live-preflight"
    query_items = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only SINGLE_FAK_REAL live preflight check")
    parser.add_argument("--no-refresh", action="store_true", help="Use the current local snapshot without REST refresh")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--include-snapshot", action="store_true", help="Include the full dashboard snapshot in output")
    parser.add_argument(
        "--service-url",
        help="Read /api/live-preflight from a running dashboard service instead of creating a fresh local bot",
    )
    parser.add_argument("--service-timeout", type=float, default=10.0, help="HTTP timeout for --service-url")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Exit with code 2 unless the live path is ready to place the next order",
    )
    parser.add_argument(
        "--require-arming-ready",
        action="store_true",
        help="Exit with code 2 unless all checks except the live enabled switch pass",
    )
    args = parser.parse_args(argv)

    try:
        if args.service_url:
            payload = fetch_live_preflight_from_service(
                args.service_url,
                include_snapshot=args.include_snapshot,
                timeout=args.service_timeout,
            )
        else:
            payload = build_live_preflight_payload(refresh=not args.no_refresh)
        if not args.include_snapshot and isinstance(payload, dict):
            payload = {"live_preflight": payload.get("live_preflight")}
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - command output must be machine-readable for runbooks.
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        exit_code = 1

    text = json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=bool(args.pretty))
    print(text)
    if exit_code:
        return exit_code

    live_preflight = payload.get("live_preflight") if isinstance(payload, dict) else {}
    if not isinstance(live_preflight, dict):
        return 1
    if args.require_ready and not live_preflight.get("ready"):
        return 2
    if args.require_arming_ready and not live_preflight.get("arming_ready"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

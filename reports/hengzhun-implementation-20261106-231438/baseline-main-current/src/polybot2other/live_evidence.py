from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from typing import Any

from .bot import PaperTradingBot
from .config import Settings, load_settings
from .storage import TradeStore


def build_live_evidence_payload(
    settings: Settings | None = None,
    *,
    external_order_id: str | None = None,
    force: bool = True,
) -> dict[str, Any]:
    resolved_settings = settings or load_settings()
    store = TradeStore(resolved_settings.db_path, resolved_settings.initial_balance)
    bot = PaperTradingBot(resolved_settings, store)
    return bot.live_evidence(external_order_id, force=force)


def fetch_live_evidence_from_service(
    service_url: str,
    *,
    external_order_id: str | None = None,
    force: bool = True,
    include_snapshot: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = _service_live_evidence_url(
        service_url,
        external_order_id=external_order_id,
        force=force,
        include_snapshot=include_snapshot,
    )
    with urllib.request.urlopen(url, timeout=max(0.5, float(timeout))) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("live_evidence"), dict):
        raise RuntimeError("service /api/live-evidence response missing live_evidence")
    return payload


def _service_live_evidence_url(
    service_url: str,
    *,
    external_order_id: str | None,
    force: bool,
    include_snapshot: bool,
) -> str:
    base = str(service_url or "").strip()
    if not base:
        raise ValueError("service_url is required")
    parts = urllib.parse.urlsplit(base)
    if not parts.scheme or not parts.netloc:
        raise ValueError("service_url must include scheme and host, for example http://127.0.0.1:8791")
    path = parts.path.rstrip("/")
    if not path or path == "/":
        path = "/api/live-evidence"
    elif not path.endswith("/api/live-evidence"):
        path = f"{path}/api/live-evidence"
    query_items = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    order_id = str(external_order_id or "").strip()
    if order_id:
        query_items["external_order_id"] = order_id
    query_items["force"] = "true" if force else "false"
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
    parser = argparse.ArgumentParser(description="Collect a read-only SINGLE_FAK_REAL live evidence package")
    parser.add_argument(
        "--external-order-id",
        default=None,
        help="Official Polymarket order id to map back to the local live ledger",
    )
    parser.add_argument("--cached-open-orders", action="store_true", help="Use cached official open orders when present")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--include-snapshot", action="store_true", help="Include the full dashboard snapshot in output")
    parser.add_argument(
        "--service-url",
        help="Read /api/live-evidence from a running dashboard service instead of creating a fresh local bot",
    )
    parser.add_argument("--service-timeout", type=float, default=10.0, help="HTTP timeout for --service-url")
    args = parser.parse_args(argv)

    try:
        if args.service_url:
            payload = fetch_live_evidence_from_service(
                args.service_url,
                external_order_id=args.external_order_id,
                force=not args.cached_open_orders,
                include_snapshot=args.include_snapshot,
                timeout=args.service_timeout,
            )
        else:
            payload = build_live_evidence_payload(
                external_order_id=args.external_order_id,
                force=not args.cached_open_orders,
            )
        if not args.include_snapshot and isinstance(payload, dict):
            payload = {"live_evidence": payload.get("live_evidence")}
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - command output must stay machine-readable for first-order checks.
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        exit_code = 1

    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=bool(args.pretty)))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import mimetypes
import signal
import sys
import threading
import urllib.error
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .bot import PaperTradingBot
from .config import Settings, load_settings
from .storage import TradeStore


STATIC_DIR = Path(__file__).resolve().parent / "static"


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, bot: PaperTradingBot) -> None:
        super().__init__(server_address, handler_class)
        self.bot = bot


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send_static("index.html", include_body=False)
            return
        if path == "/api/status":
            self._send_json(self.server.bot.snapshot(), include_body=False)
            return
        if path == "/api/equity-curve":
            self._send_json(self.server.bot.equity_curve_window(), include_body=False)
            return
        if path.startswith("/static/"):
            self._send_static(path.removeprefix("/static/"), include_body=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/":
            self._send_static("index.html")
            return
        if path == "/api/status":
            self._send_json(self.server.bot.snapshot())
            return
        if path == "/api/recent-trades":
            limit = _query_int(query, "limit", 100, 1, 500)
            offset = _query_int(query, "offset", 0, 0, 100_000)
            self._send_json(self.server.bot.recent_trades_page(limit, offset))
            return
        if path == "/api/equity-curve":
            days = _query_int(query, "days", 90, 1, 365)
            max_points = _query_int(query, "max_points", 1200, 2, 5000)
            self._send_json(self.server.bot.equity_curve_window(days, max_points))
            return
        if path == "/api/current-market":
            self.server.bot.tick()
            self._send_json(self.server.bot.snapshot())
            return
        if path.startswith("/static/"):
            self._send_static(path.removeprefix("/static/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/api/tick":
            self.server.bot.tick()
            self._send_json(self.server.bot.snapshot())
            return
        if self.path == "/api/live-snapshot":
            try:
                payload = self._read_json_body()
                self._send_json(self.server.bot.ingest_live_snapshot(payload))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
                self._send_error_json(HTTPStatus.CONFLICT, str(exc))
            return
        if self.path == "/api/strategy-settings":
            try:
                payload = self._read_json_body()
                enabled = _read_bool(payload, "pair_strategy_enabled")
                self._send_json(self.server.bot.set_pair_strategy_enabled(enabled))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > 128 * 1024:
            raise ValueError("request body too large")
        data = self.rfile.read(length)
        payload = json.loads(data.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, payload: dict[str, Any], include_body: bool = True) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                return

    def _send_error_json(self, status: HTTPStatus, message: str, include_body: bool = True) -> None:
        payload = {"error": message}
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                return

    def _send_static(self, relative: str, include_body: bool = True) -> None:
        path = (STATIC_DIR / relative).resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif path.suffix in {".html", ".css"}:
            content_type = f"{content_type}; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                return


def _read_bool(payload: dict[str, Any], key: str) -> bool:
    if key not in payload:
        raise ValueError(f"{key} is required")
    value = payload[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean")


def _query_int(query: dict[str, list[str]], key: str, default: int, minimum: int, maximum: int) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def build_app(settings: Settings | None = None) -> tuple[DashboardServer, PaperTradingBot]:
    settings = settings or load_settings()
    store = TradeStore(settings.db_path, settings.initial_balance)
    bot = PaperTradingBot(settings, store)
    server = DashboardServer(("127.0.0.1", 8787), Handler, bot)
    return server, bot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the polybot2other paper trading dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    settings = load_settings()
    store = TradeStore(settings.db_path, settings.initial_balance)
    bot = PaperTradingBot(settings, store)
    server = DashboardServer((args.host, args.port), Handler, bot)
    stop_event = threading.Event()

    def _stop(_signum, _frame) -> None:
        stop_event.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    bot.start()
    print(f"polybot2other dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        bot.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

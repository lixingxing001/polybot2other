from __future__ import annotations

import argparse
import html
import json
import mimetypes
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .bot import LiveOnceBlockedError, PaperTradingBot
from .config import Settings, load_settings
from .live_doctor import build_live_doctor_from_bot
from .storage import PAPER_ORDER_STATUS_FILTERS, TradeStore


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
        if path == "/api/actor-analysis":
            self._send_json(self.server.bot.actor_analysis(force=False), include_body=False)
            return
        if path == "/api/equity-curve":
            self._send_json(self.server.bot.equity_curve_window(), include_body=False)
            return
        if path == "/api/orders":
            self._send_json(self.server.bot.orders_page(), include_body=False)
            return
        if path == "/api/live-settings":
            self._send_json(self.server.bot.live_settings(), include_body=False)
            return
        if path == "/api/live-preflight":
            self._send_json(self.server.bot.live_preflight(), include_body=False)
            return
        if path == "/api/live-open-orders":
            self._send_json(self.server.bot.live_open_orders(force=False), include_body=False)
            return
        if path == "/api/live-evidence":
            self._send_json(self.server.bot.live_evidence(force=False), include_body=False)
            return
        if path == "/api/live-doctor":
            payload = build_live_doctor_from_bot(self.server.bot, refresh=False)
            self._send_json({"live_doctor": payload.get("live_doctor")}, include_body=False)
            return
        if path == "/api/strategy-experiments":
            self._send_json(self.server.bot.strategy_experiments_snapshot(), include_body=False)
            return
        if path == "/api/strategy-experiments-retrospective":
            self._send_json(self.server.bot.strategy_experiments_retrospective(), include_body=False)
            return
        if path == "/api/strategy-experiments-tables":
            self._send_json(self.server.bot.strategy_experiments_tables(), include_body=False)
            return
        if path == "/strategy-experiments-retrospective.html":
            self._send_html(
                _strategy_experiments_retrospective_report_html(
                    self.server.bot.strategy_experiments_retrospective(),
                    generated_at=time.time(),
                ),
                include_body=False,
            )
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
        if path == "/api/actor-analysis":
            try:
                refresh = _query_bool_optional(query, "refresh", False)
                self._send_json(self.server.bot.actor_analysis(force=refresh))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/recent-trades":
            try:
                limit = _query_int(query, "limit", 100, 1, 500)
                offset = _query_int(query, "offset", 0, 0, 100_000)
                start_at = _query_float_optional(query, "start_at", 0, 4_102_444_800)
                end_at = _query_float_optional(query, "end_at", 0, 4_102_444_800)
                account_scope = _query_str_optional(query, "account_scope") or "main"
                variant_id = _query_str_optional(query, "variant_id") or _query_str_optional(query, "variant")
                self._send_json(
                    self.server.bot.recent_trades_page(
                        limit,
                        offset,
                        start_at,
                        end_at,
                        account_scope=account_scope,
                        variant_id=variant_id,
                    )
                )
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/orders":
            try:
                limit = _query_int(query, "limit", 20, 1, 200)
                offset = _query_int(query, "offset", 0, 0, 100_000)
                status_filter = _query_choice(query, "status", set(PAPER_ORDER_STATUS_FILTERS), "all")
                account_scope = _query_str_optional(query, "account_scope") or "main"
                variant_id = _query_str_optional(query, "variant_id") or _query_str_optional(query, "variant")
                self._send_json(
                    self.server.bot.orders_page(
                        limit,
                        offset,
                        status_filter,
                        account_scope=account_scope,
                        variant_id=variant_id,
                    )
                )
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/order-fills":
            try:
                order_id = _query_int(query, "order_id", 0, 1, 1_000_000_000)
                account_scope = _query_str_optional(query, "account_scope") or "main"
                variant_id = _query_str_optional(query, "variant_id") or _query_str_optional(query, "variant")
                self._send_json(self.server.bot.order_fills(order_id, account_scope, variant_id))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/equity-curve":
            try:
                days = _query_int(query, "days", 90, 1, 365)
                max_points = _query_int(query, "max_points", 1200, 2, 5000)
                account_scope = _query_str_optional(query, "account_scope") or "main"
                variant_id = _query_str_optional(query, "variant_id") or _query_str_optional(query, "variant")
                self._send_json(
                    self.server.bot.equity_curve_window(
                        days,
                        max_points,
                        account_scope=account_scope,
                        variant_id=variant_id,
                    )
                )
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/strategy-experiments":
            try:
                variant_id = _query_str_optional(query, "variant_id") or _query_str_optional(query, "variant")
                if variant_id:
                    trade_limit = _query_int(query, "trade_limit", 50, 1, 200)
                    order_limit = _query_int(query, "order_limit", 50, 1, 200)
                    self._send_json(self.server.bot.strategy_experiment_detail(variant_id, trade_limit, order_limit))
                else:
                    self._send_json(self.server.bot.strategy_experiments_snapshot())
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/strategy-experiments-retrospective":
            try:
                start_at = _query_float_optional(query, "start_at", 0, 4_102_444_800)
                end_at = _query_float_optional(query, "end_at", 0, 4_102_444_800)
                self._send_json(self.server.bot.strategy_experiments_retrospective(start_at, end_at))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/strategy-experiments-tables":
            try:
                trade_limit = _query_int(query, "trade_limit", 100, 1, 500)
                order_limit = _query_int(query, "order_limit", 20, 1, 200)
                status_filter = _query_choice(query, "status", set(PAPER_ORDER_STATUS_FILTERS), "all")
                start_at = _query_float_optional(query, "start_at", 0, 4_102_444_800)
                end_at = _query_float_optional(query, "end_at", 0, 4_102_444_800)
                self._send_json(
                    self.server.bot.strategy_experiments_tables(
                        trade_limit=trade_limit,
                        order_limit=order_limit,
                        order_status_filter=status_filter,
                        start_at=start_at,
                        end_at=end_at,
                    )
                )
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/strategy-experiments-retrospective.html":
            try:
                start_at = _query_float_optional(query, "start_at", 0, 4_102_444_800)
                end_at = _query_float_optional(query, "end_at", 0, 4_102_444_800)
                report = self.server.bot.strategy_experiments_retrospective(start_at, end_at)
                self._send_html(_strategy_experiments_retrospective_report_html(report, generated_at=time.time()))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/live-settings":
            self._send_json(self.server.bot.live_settings())
            return
        if path == "/api/live-preflight":
            try:
                include_snapshot = _query_bool_optional(query, "include_snapshot", True)
                payload = self.server.bot.live_preflight()
                if not include_snapshot:
                    payload = {"live_preflight": payload.get("live_preflight")}
                self._send_json(payload)
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/live-open-orders":
            self._send_json(self.server.bot.live_open_orders(force=True))
            return
        if path == "/api/live-evidence":
            try:
                external_order_id = _query_str_optional(query, "external_order_id")
                force = _query_bool_optional(query, "force", True)
                include_snapshot = _query_bool_optional(query, "include_snapshot", False)
                payload = self.server.bot.live_evidence(external_order_id, force=force)
                if not include_snapshot:
                    payload = {"live_evidence": payload.get("live_evidence")}
                self._send_json(payload)
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if path == "/api/live-doctor":
            try:
                refresh = _query_bool_optional(query, "refresh", True)
                include_snapshot = _query_bool_optional(query, "include_snapshot", False)
                payload = build_live_doctor_from_bot(self.server.bot, refresh=refresh)
                if not include_snapshot:
                    payload = {"live_doctor": payload.get("live_doctor")}
                self._send_json(payload)
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
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
        if self.path == "/api/live-settings":
            try:
                payload = self._read_json_body()
                self._send_json(self.server.bot.update_live_settings(payload))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if self.path == "/api/live-reload-credentials":
            try:
                self._send_json(self.server.bot.reload_live_credentials())
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if self.path == "/api/live-toggle":
            try:
                payload = self._read_json_body()
                enabled = _read_bool(payload, "enabled")
                self._send_json(self.server.bot.set_live_enabled(enabled))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if self.path == "/api/live-preflight":
            try:
                self._send_json(self.server.bot.live_preflight())
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if self.path == "/api/live-once":
            try:
                payload = self._read_json_body()
                self._send_json(
                    self.server.bot.run_live_once(
                        confirm=str(payload.get("confirm") or ""),
                        max_stake_dollars=_body_float(payload, "max_stake_dollars", 0.01, 1_000_000.0),
                        acknowledge_compliance=_body_bool_optional(payload, "acknowledge_compliance", False),
                        disable_after=_body_bool_optional(payload, "disable_after", True),
                        refresh=_body_bool_optional(payload, "refresh", True),
                        reconcile_wait_seconds=_body_float_optional(
                            payload,
                            "reconcile_wait_seconds",
                            0.0,
                            120.0,
                            0.0,
                        ),
                        reconcile_poll_seconds=_body_float_optional(
                            payload,
                            "reconcile_poll_seconds",
                            0.1,
                            10.0,
                            1.0,
                        ),
                        wait_ready_seconds=_body_float_optional(
                            payload,
                            "wait_ready_seconds",
                            0.0,
                            1800.0,
                            0.0,
                        ),
                        ready_poll_seconds=_body_float_optional(
                            payload,
                            "ready_poll_seconds",
                            0.25,
                            30.0,
                            2.0,
                        ),
                        include_evidence=_body_bool_optional(payload, "include_evidence", True),
                    )
                )
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except LiveOnceBlockedError as exc:
                self._send_json_status(HTTPStatus.CONFLICT, exc.payload)
            except RuntimeError as exc:
                self._send_error_json(HTTPStatus.CONFLICT, str(exc))
            return
        if self.path == "/api/live-emergency-stop":
            try:
                self._send_json(self.server.bot.live_emergency_stop())
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if self.path == "/api/live-sell":
            try:
                payload = self._read_json_body()
                trade_id = _body_int(payload, "trade_id", 1, 1_000_000_000)
                self._send_json(self.server.bot.sell_live_trade(trade_id))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except RuntimeError as exc:
                self._send_error_json(HTTPStatus.CONFLICT, str(exc))
            return
        if self.path == "/api/cancel-order":
            try:
                payload = self._read_json_body()
                order_id = _body_int(payload, "order_id", 1, 1_000_000_000)
                self._send_json(self.server.bot.cancel_order(order_id))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if self.path == "/api/cancel-orders":
            try:
                payload = self._read_json_body()
                scope = _body_choice(payload, "scope", {"current_market", "all"}, "current_market")
                self._send_json(self.server.bot.cancel_orders(scope))
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

    def _send_json_status(self, status: HTTPStatus, payload: dict[str, Any], include_body: bool = True) -> None:
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

    def _send_html(self, payload: str, include_body: bool = True) -> None:
        data = payload.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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


def _strategy_experiments_retrospective_report_html(report: dict[str, Any], generated_at: float | None = None) -> str:
    generated_at = time.time() if generated_at is None else float(generated_at)
    profit = report.get("profit_summary") or {}
    decision = report.get("decision_summary") or {}
    window = report.get("window") or {}
    variants = report.get("variants") if isinstance(report.get("variants"), list) else []
    title = "策略实验复盘报告"
    rows = "\n".join(_strategy_report_row(row, index + 1) for index, row in enumerate(variants))
    if not rows:
        rows = '<tr><td colspan="17" class="empty">暂无策略实验数据</td></tr>'
    missing_rows = "\n".join(_strategy_report_pending_row(row) for row in decision.get("missing_sample_variants") or [])
    if not missing_rows:
        missing_rows = '<tr><td colspan="5" class="empty">暂无待补样本组合</td></tr>'
    disqualified_rows = "\n".join(_strategy_report_disqualified_row(row) for row in decision.get("disqualified_variants") or [])
    if not disqualified_rows:
        disqualified_rows = '<tr><td colspan="5" class="empty">暂无执行淘汰组合</td></tr>'
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{_html(title)}</title>
    <style>
      :root {{
        color-scheme: light;
        --text: #17202a;
        --muted: #657080;
        --line: #d9e0e8;
        --bg: #f5f7fa;
        --panel: #ffffff;
        --good: #0f7a4f;
        --bad: #b42318;
        --warn: #9a5b00;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Arial, "Microsoft YaHei", sans-serif;
        color: var(--text);
        background: var(--bg);
      }}
      main {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
      header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 18px; }}
      h1 {{ margin: 0 0 8px; font-size: 28px; }}
      h2 {{ margin: 0 0 12px; font-size: 18px; }}
      p {{ margin: 4px 0; color: var(--muted); }}
      .stamp {{ text-align: right; font-size: 13px; color: var(--muted); }}
      .cards {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
      .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; min-height: 78px; }}
      .card span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
      .card strong {{ display: block; font-size: 17px; line-height: 1.25; word-break: break-word; }}
      .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin-top: 14px; }}
      table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
      th, td {{ border-bottom: 1px solid var(--line); padding: 8px 7px; text-align: left; vertical-align: top; }}
      th {{ color: var(--muted); font-weight: 600; background: #f8fafc; position: sticky; top: 0; }}
      .table-wrap {{ overflow-x: auto; }}
      .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
      .muted {{ color: var(--muted); }}
      .good {{ color: var(--good); font-weight: 700; }}
      .bad {{ color: var(--bad); font-weight: 700; }}
      .warn {{ color: var(--warn); font-weight: 700; }}
      .empty {{ color: var(--muted); text-align: center; padding: 20px; }}
      .reason {{ max-width: 280px; line-height: 1.45; }}
      @media (max-width: 900px) {{
        main {{ padding: 14px; }}
        header {{ display: block; }}
        .stamp {{ text-align: left; margin-top: 8px; }}
        .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      }}
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <h1>{_html(title)}</h1>
          <p>{len(variants)} 组 SINGLE/PAIR 策略实验隔离 Paper 复盘。</p>
          <p>窗口：{_html(_format_report_window(window))}</p>
        </div>
        <div class="stamp">
          <div>生成时间：{_html(_format_report_time(generated_at))}</div>
          <div>数据源：{_html(str(report.get("db_dir") or "-"))}</div>
        </div>
      </header>
      <section class="cards">
        {_strategy_report_card("盈利状态", profit.get("status_label") or "-")}
        {_strategy_report_card("正式盈利胜出", profit.get("winner_combo") or "暂无")}
        {_strategy_report_card("当前盈利领先", profit.get("current_profit_leader_combo") or "暂无")}
        {_strategy_report_card("样本", f"{profit.get('ready_count') or 0} / {profit.get('total_count') or len(variants)}")}
        {_strategy_report_card("执行淘汰", str(profit.get("disqualified_count") or 0))}
      </section>
      <section class="panel">
        <h2>复盘结论</h2>
        <p>盈利口径：{_html(str(profit.get("reason") or "-"))}</p>
        <p>评分口径：{_html(str(decision.get("reason") or "-"))}</p>
        <p>正式决胜条件：未淘汰组合达到样本阈值，且最高净盈亏大于 0。当前 profitable_winner_ready={_html(str(profit.get("profitable_winner_ready")))}。</p>
      </section>
      <section class="panel">
        <h2>{len(variants)} 组合盈利排名</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>排名</th><th>组合</th><th>定位</th><th>样本</th><th>净盈亏</th><th>ROI</th><th>胜率</th>
                <th>结算</th><th>官方</th><th>订单</th><th>成交率</th><th>评分</th><th>决策</th><th>目标代码</th>
                <th>报告契合</th><th>状态</th><th>原因</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>待补样本</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>组合</th><th>样本状态</th><th>结算数</th><th>订单数</th><th>说明</th></tr></thead>
            <tbody>{missing_rows}</tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>执行淘汰</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>组合</th><th>原因</th><th>评分</th><th>结算数</th><th>订单/成交率</th></tr></thead>
            <tbody>{disqualified_rows}</tbody>
          </table>
        </div>
      </section>
    </main>
  </body>
</html>
"""


def _strategy_report_card(label: str, value: Any) -> str:
    return f'<div class="card"><span>{_html(label)}</span><strong>{_html(str(value))}</strong></div>'


def _strategy_report_row(row: dict[str, Any], fallback_rank: int) -> str:
    summary = row.get("recent_trades_summary") or {}
    orders = row.get("order_summary") or {}
    review = row.get("review_score") or {}
    rank = _rank_for_variant(row, fallback_rank)
    pnl = _number(summary.get("total_pnl"))
    pnl_class = "good" if pnl is not None and pnl > 0 else "bad" if pnl is not None and pnl < 0 else ""
    status_bits = []
    if review.get("eligible_for_decision"):
        status_bits.append("可决胜")
    if review.get("disqualified"):
        status_bits.append("执行淘汰")
    if row.get("last_error"):
        status_bits.append("运行异常")
    if row.get("official_broadcast_error"):
        status_bits.append("官方广播异常")
    return f"""
              <tr>
                <td class="mono">{_html(str(rank))}</td>
                <td><strong>{_html(str(row.get("combo") or row.get("variant_id") or "-"))}</strong><br><span class="muted mono">{_html(str(row.get("variant_id") or "-"))}</span></td>
                <td>{_html(str(row.get("role") or "-"))}</td>
                <td>{_html(str(review.get("sample_label") or "-"))}</td>
                <td class="{pnl_class}">{_html(_format_money(summary.get("total_pnl")))}</td>
                <td>{_html(_format_pct(summary.get("roi_pct")))}</td>
                <td>{_html(_format_pct(summary.get("win_rate")))}</td>
                <td>{_html(str(summary.get("settled_count") or 0))} / {_html(str(summary.get("total_count") or 0))}</td>
                <td>{_html(str(summary.get("official_count") or 0))}</td>
                <td>{_html(str(orders.get("total_count") or 0))}</td>
                <td>{_html(_format_pct(orders.get("fill_rate")))}</td>
                <td>{_html(_format_number(review.get("score"), 2))}</td>
                <td>{_html(str(review.get("decision") or "-"))}</td>
                <td>{_html(str(row.get("target_code_completion") or "-"))}</td>
                <td>{_html(str(row.get("target_report_alignment") or "-"))}</td>
                <td>{_html(" / ".join(status_bits) if status_bits else "-")}</td>
                <td class="reason">{_html("; ".join(str(item) for item in review.get("reasons") or []))}</td>
              </tr>"""


def _strategy_report_pending_row(row: dict[str, Any]) -> str:
    return f"""
              <tr>
                <td>{_html(str(row.get("combo") or row.get("variant_id") or "-"))}</td>
                <td>{_html(str(row.get("sample_label") or row.get("sample_status") or "-"))}</td>
                <td>{_html(str(row.get("settled_count") or 0))}</td>
                <td>{_html(str(row.get("order_count") or 0))}</td>
                <td>继续积累样本后再纳入正式决胜。</td>
              </tr>"""


def _strategy_report_disqualified_row(row: dict[str, Any]) -> str:
    order_text = f"{row.get('order_count') or 0} / {_format_pct(row.get('fill_rate'))}"
    return f"""
              <tr>
                <td>{_html(str(row.get("combo") or row.get("variant_id") or "-"))}</td>
                <td>{_html(str(row.get("reason") or "-"))}</td>
                <td>{_html(_format_number(row.get("score"), 2))}</td>
                <td>{_html(str(row.get("settled_count") or 0))}</td>
                <td>{_html(order_text)}</td>
              </tr>"""


def _rank_for_variant(row: dict[str, Any], fallback_rank: int) -> int:
    profit = row.get("profit_rank")
    try:
        return int(profit)
    except (TypeError, ValueError):
        return fallback_rank


def _format_report_window(window: dict[str, Any]) -> str:
    start_at = window.get("start_at")
    end_at = window.get("end_at")
    if start_at is None and end_at is None:
        return "全部历史"
    return f"{_format_report_time(start_at)} - {_format_report_time(end_at)}"


def _format_report_time(value: Any) -> str:
    parsed = _number(value)
    if parsed is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(parsed))


def _format_money(value: Any) -> str:
    parsed = _number(value)
    if parsed is None:
        return "-"
    prefix = "+" if parsed > 0 else ""
    return f"{prefix}${parsed:.2f}"


def _format_pct(value: Any) -> str:
    parsed = _number(value)
    if parsed is None:
        return "-"
    return f"{parsed:.2f}%"


def _format_number(value: Any, digits: int = 2) -> str:
    parsed = _number(value)
    if parsed is None:
        return "-"
    return f"{parsed:.{digits}f}"


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _html(value: str) -> str:
    return html.escape(str(value), quote=True)


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


def _query_float_optional(query: dict[str, list[str]], key: str, minimum: float, maximum: float) -> float | None:
    raw = query.get(key, [""])[0]
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _query_choice(query: dict[str, list[str]], key: str, choices: set[str], default: str) -> str:
    raw = query.get(key, [default])[0]
    value = str(raw or default).strip().lower().replace("-", "_")
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{key} must be one of {allowed}")
    return value


def _query_str_optional(query: dict[str, list[str]], key: str) -> str | None:
    raw = query.get(key, [""])[0]
    value = str(raw or "").strip()
    return value or None


def _query_bool_optional(query: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = query.get(key, [None])[0]
    if raw in (None, ""):
        return default
    value = str(raw).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def _body_int(payload: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    if key not in payload:
        raise ValueError(f"{key} is required")
    try:
        value = int(payload[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _body_float(payload: dict[str, Any], key: str, minimum: float, maximum: float) -> float:
    if key not in payload:
        raise ValueError(f"{key} is required")
    try:
        value = float(payload[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _body_float_optional(
    payload: dict[str, Any],
    key: str,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    if key not in payload:
        return default
    return _body_float(payload, key, minimum, maximum)


def _body_bool_optional(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
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


def _body_choice(payload: dict[str, Any], key: str, choices: set[str], default: str) -> str:
    raw = payload.get(key, default)
    value = str(raw or default).strip().lower().replace("-", "_")
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{key} must be one of {allowed}")
    return value


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

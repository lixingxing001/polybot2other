"""Loopback-only, read-only status surface for the opt-in MAIN-R1 experiment."""
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import math
import socket
from urllib.parse import urlsplit


def _loopback(value):
    if value == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _money(value):
    return f"${value:,.4f}" if type(value) in (int, float) and math.isfinite(value) else "—"


def _text(value):
    return escape(str(value), quote=True)


def _time(value, *, milliseconds=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        return "—"
    try:
        moment = datetime.fromtimestamp(value / 1000 if milliseconds else value,
                                        timezone(timedelta(hours=8)))
        return moment.isoformat(sep=" ", timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return "—"


def _milliseconds(value):
    return f"{value:.1f} ms" if type(value) in (int, float) and math.isfinite(value) else "—"


def _execution_card(account):
    execution = account.get("execution") or {}
    phases = " · ".join(f"{key} {count}" for key, count in sorted((execution.get("counts") or {}).items())) or "无模拟订单"
    event = execution.get("last_event") or {}
    halts = ((account.get("risk") or {}).get("state") or {}).get("persistent_halts") or []
    model = execution.get("arrival_model", "未标记")
    basis = execution.get("execution_basis", "未标记")
    unknown = execution.get("unknown_orders") or []
    count = execution.get("unknown_order_count", (execution.get("counts") or {}).get("UNKNOWN", 0))
    fragments = ["<article><h3>" + _text(account.get("label", account.get("key", "—"))) + "</h3>",
                 "<p>订单阶段：" + _text(phases) + "<br>最近事件：" + _text(event.get("code", "—")) + " · "
                 + _text(event.get("message", "等待执行事件")) + "（" + _text(_time(event.get("at"))) + "）<br>模型："
                 + _text(model) + " / " + _text(basis) + "<br>持久停止：" + _text("、".join(halts) or "无") + "</p>"]
    if count:
        fragments.append("<p class=blocked>UNKNOWN 共 " + _text(count) + " 笔；下列为可用的最近 "
                         + _text(min(len(unknown), 10)) + " 笔审计摘要。预算仍保留，页面不提供恢复或清账操作。</p>")
    for order in unknown[:10]:
        fragments.append("<div class=order><p>订单 #" + _text(order.get("order_id", "—")) + " · "
                         + _text(order.get("round_id", "—")) + " · " + _text(order.get("phase", "UNKNOWN"))
                         + "<br>原因：" + _text(order.get("reason") or "缺少历史原因，请核对原始订单审计")
                         + "<br>模型：" + _text(order.get("model", "未标记")) + " / "
                         + _text(order.get("execution_basis", "未标记")) + "</p><p>提交 "
                         + _text(_time(order.get("submitted_at"))) + "；预计到达 " + _text(_time(order.get("arrival_at")))
                         + "；截止 " + _text(_time(order.get("deadline_at"))) + "<br>证据记录 "
                         + _text(_time(order.get("evidence_at"))) + "；源时间 " + _text(_time(order.get("source_ms"), milliseconds=True))
                         + "；实际接收 " + _text(_time(order.get("received_ms"), milliseconds=True))
                         + "<br>源年龄 " + _text(_milliseconds(order.get("source_age_ms")))
                         + "；接收年龄 " + _text(_milliseconds(order.get("receipt_age_ms")))
                         + "；传输时间差 " + _text(_milliseconds(order.get("transport_lag_ms"))) + "</p></div>")
    fragments.append("</article>")
    return "".join(fragments)


def page(data):
    rows = []
    accounts = [data.get("portfolio", {})] + list(data.get("shadows", []))
    for account in accounts:
        rows.append("<tr>" + "".join("<td>" + _text(value) + "</td>" for value in (
            account.get("label", "—"), account.get("role", "—"),
            _money(account.get("initial_balance")), _money(account.get("cash_balance")),
            _money(account.get("realized_pnl")), _money(account.get("open_cost")),
            account.get("trade_count", "—"), account.get("settled_rounds", "—"))) + "</tr>")
    details = {key: data.get(key) for key in ("feed", "router", "cohorts", "errors", "blocking", "learning",
                                             "execution_model", "config_id", "provenance")}
    details["risk"] = {a.get("key", "portfolio"): a.get("risk")
                       for a in accounts}
    blocking = data.get("blocking") or {}
    blocker_rows = ([blocking["portfolio"]] if blocking.get("portfolio") else []) + list(blocking.get("shadows") or [])
    blocked = "".join("<li>" + _text(item.get("key", "—")) + "：" + _text("；".join(item.get("reasons") or [])) + "</li>"
                      for item in blocker_rows)
    learning = data.get("learning") or {}
    learning_message = learning.get("reason") or "选择器需要合格共同样本；请结合各账户执行状态判断是否仍能学习。"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="15">
<title>MAIN-R1 · 自适应 Paper</title><style>
body{font:16px/1.65 system-ui,sans-serif;margin:0;color:#243244;background:#f2f5f9}
main{max-width:1200px;margin:auto;padding:24px}section{background:white;padding:20px;margin:16px 0;border-radius:10px}
h1{margin:0}h2{font-size:20px}p{max-width:1000px}table{border-collapse:collapse;width:100%;white-space:nowrap}
th,td{text-align:left;border-bottom:1px solid #dde4eb;padding:10px;font-variant-numeric:tabular-nums}
.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#185ca7}
article{border-top:1px solid #dde4eb;padding:8px 0}article p{margin:8px 0;font-size:14px;overflow-wrap:anywhere}
h3{margin:10px 0}.order{background:#f5f7fa;border-radius:6px;padding:8px 12px;margin:8px 0}.blocked{color:#9b3122}
</style></head><body><main><h1>MAIN-R1 · 自适应 Paper</h1>
<p>独立模拟实验；未启用真实资金、签名或下单。页面每 15 秒更新。</p>
<section><h2>当前状态：""" + _text(data.get("state", "unknown")) + """</h2>
<p>快照时间：""" + _text(_time(data.get("as_of"))) + """；实验开始：""" + _text(_time(data.get("start_at"))) + """（北京时间）</p>
<p>学习状态：""" + _text(learning.get("state", "unknown")) + """ · """ + _text(learning_message) + """</p>
<p>合格共同样本：""" + _text(learning.get("learned_cohorts", (data.get("router") or {}).get("cohorts", 0))) + """；受阻影子账户：""" + _text(blocking.get("blocked_shadow_count", 0)) + """ / 4；UNKNOWN 总数：""" + _text(blocking.get("unknown_order_count", 0)) + """。</p>
""" + ("<ul class=blocked>" + blocked + "</ul>" if blocked else "") + """
<p>组合账本是 MAIN-R1 的唯一组合结果。四个影子账户分别使用独立的虚拟 $1,000，
用于积累未来选择所需的共同市场样本；这些账户的资金和收益不能相加。</p>
<p>候选继承 MAIN、FAST、PLUS 的入场信号，并统一为每轮一次正成交、同一研究风险模板、持有至官方结算。
因此影子结果不代表原策略完整执行规则的重现。正常冷启动时组合空仓；影子出现 UNKNOWN 或持久停止时，完整共同样本的积累会受阻。</p>
<p>本地观测 Paper 模型使用实际收到的合格盘口模拟成交；该结果不证明交易所实际到达时能够成交。历史订单保留其原有模型和证据。</p></section>
<section><h2>独立账本</h2><div class="scroll"><table><thead><tr>
<th>账户</th><th>用途</th><th>初始虚拟资金</th><th>可用现金</th><th>已实现净收益</th>
<th>未结算成本</th><th>成交笔数</th><th>已结算市场</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table></div>
<p>可用现金不含待确认预留和未释放兑付；已实现收益不代表完整 NAV。估值不足时新风险暂停，已有仓位继续确认和结算。</p>
</section><section><h2>各账户执行与未知订单</h2>""" + "".join(_execution_card(account) for account in accounts) + """</section>
<section><h2>行情、选择与风险诊断</h2><pre>""" + _text(json.dumps(details, ensure_ascii=False, indent=2, allow_nan=False)) + """</pre>
<p><a href="/api/status">完整只读 JSON 状态</a> · <a href="/health">进程健康状态</a></p></section></main></body></html>"""


def serve(server_address, snapshot):
    """Construct the server without starting it. No mutation routes exist."""
    bind, port = server_address
    if not _loopback(bind):
        raise ValueError("MAIN-R1 status must bind to a loopback address")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._read(False)

        def do_HEAD(self):
            self._read(True)

        def _denied(self):
            self._reply(405, "text/plain", b"Read-only endpoint", False)

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _denied

        def _reply(self, code, content_type, body, head):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _read(self, head):
            try:
                host = urlsplit("//" + self.headers.get("Host", ""))
                _ = host.port
                valid = (host.username is None and host.password is None and not host.path
                         and not host.query and not host.fragment and _loopback(host.hostname or ""))
            except ValueError:
                valid = False
            if not valid or not _loopback(self.client_address[0]):
                self._reply(403, "text/plain", b"Loopback only", head)
                return
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/api/status", "/health"):
                self._reply(404, "text/plain", b"Not found", head)
                return
            try:
                data = snapshot()
                if path == "/health":
                    # A data/risk pause is not a reason to erase state by restarting.
                    data = {"ok": data.get("state") != "stopped", "mode": "PAPER_ONLY",
                            "strategy_id": data.get("strategy_id"), "state": data.get("state"),
                            "feed_ready": data.get("feed", {}).get("ready", False)}
                body = page(data).encode() if path == "/" else json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            except Exception:
                self._reply(503, "text/plain; charset=utf-8", "MAIN-R1 状态暂不可读".encode(), head)
                return
            self._reply(200, "text/html; charset=utf-8" if path == "/" else "application/json; charset=utf-8", body, head)

        def log_message(self, *_):
            return

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        daemon_threads = True

    return Server((bind, port), Handler)

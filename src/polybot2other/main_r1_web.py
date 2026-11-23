"""Loopback-only, read-only status surface for the opt-in MAIN-R1 experiment."""
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


def page(data):
    rows = []
    for account in [data.get("portfolio", {})] + list(data.get("shadows", [])):
        rows.append("<tr>" + "".join("<td>" + _text(value) + "</td>" for value in (
            account.get("label", "—"), account.get("role", "—"),
            _money(account.get("initial_balance")), _money(account.get("cash_balance")),
            _money(account.get("realized_pnl")), _money(account.get("open_cost")),
            account.get("trade_count", "—"), account.get("settled_rounds", "—"))) + "</tr>")
    details = {key: data.get(key) for key in ("feed", "router", "config_id", "provenance")}
    details["risk"] = {a.get("key", "portfolio"): a.get("risk")
                       for a in [data.get("portfolio", {})] + list(data.get("shadows", []))}
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="15">
<title>MAIN-R1 · 自适应 Paper</title><style>
body{font:16px/1.65 system-ui,sans-serif;margin:0;color:#243244;background:#f2f5f9}
main{max-width:1200px;margin:auto;padding:24px}section{background:white;padding:20px;margin:16px 0;border-radius:10px}
h1{margin:0}h2{font-size:20px}p{max-width:1000px}table{border-collapse:collapse;width:100%;white-space:nowrap}
th,td{text-align:left;border-bottom:1px solid #dde4eb;padding:10px;font-variant-numeric:tabular-nums}
.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#185ca7}
</style></head><body><main><h1>MAIN-R1 · 自适应 Paper</h1>
<p>独立模拟实验；未启用真实资金、签名或下单。页面每 15 秒更新。</p>
<section><h2>当前状态：""" + _text(data.get("state", "unknown")) + """</h2>
<p>组合账本是 MAIN-R1 的唯一组合结果。四个影子账户分别使用独立的虚拟 $1,000，
用于积累未来选择所需的共同市场样本；这些账户的资金和收益不能相加。</p>
<p>候选继承 MAIN、FAST、PLUS 的入场信号，并统一为每轮一次正成交、同一研究风险模板、持有至官方结算。
因此影子结果不代表原策略完整执行规则的重现。选择器冷启动时组合空仓，影子账户继续采样。</p></section>
<section><h2>独立账本</h2><div class="scroll"><table><thead><tr>
<th>账户</th><th>用途</th><th>初始虚拟资金</th><th>可用现金</th><th>已实现净收益</th>
<th>未结算成本</th><th>成交笔数</th><th>已结算市场</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table></div>
<p>可用现金不含待确认预留和未释放兑付；已实现收益不代表完整 NAV。估值不足时新风险暂停，已有仓位继续确认和结算。</p>
</section><section><h2>行情、选择与风险状态</h2><pre>""" + _text(json.dumps(details, ensure_ascii=False, indent=2, allow_nan=False)) + """</pre>
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

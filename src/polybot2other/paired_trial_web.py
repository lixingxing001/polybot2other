"""Local, read-only Paper trial dashboard."""
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import math
import socket
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


_CSS = """
:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif;color:#edf3f7;background:#101416}
*{box-sizing:border-box}body{margin:0}main{max-width:1370px;margin:auto;padding:1.4rem}
header,section{background:#191f23;border:1px solid #334049;border-radius:.7rem;padding:1.15rem;margin-bottom:1rem}
h1{font-size:1.55rem;margin:0}h2{font-size:1.16rem;margin:0 0 .55rem}h3{font-size:1rem;margin:.9rem 0 .3rem}
p{line-height:1.6;margin:.3rem 0;color:#b8c6cf}small{color:#aab9c3}a{color:#85c0ff}
table{border-collapse:collapse;width:100%;margin:.6rem 0;font-size:.91rem}
th,td{padding:.55rem;text-align:left;border-bottom:1px solid #344048;vertical-align:top;font-variant-numeric:tabular-nums}
thead{color:#b3c4cf}tbody th{font-weight:600}td{line-height:1.38} .wide{overflow-x:auto}.wide th{white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,22rem),1fr));gap:0 1.15rem}
.pos{color:#7ce0a6}.neg{color:#ff9a9a}.quiet{color:#b1c1ca}.num{white-space:nowrap}
@media(max-width:640px){main{padding:.6rem}section,header{padding:.8rem}th,td{padding:.45rem}}
"""


def _text(value):
    return escape(str(value), quote=True) if value is not None and value != "" else "—"


def _time(stamp):
    try:
        return datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError, OverflowError):
        return "—"


def _money(value, *, signed=False):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    css, prefix = ("neg", "−") if value < 0 else ("pos", "+") if signed and value > 0 else ("", "")
    return f'<span class="num {css}">{prefix}${abs(value):,.2f}</span>'


def _table(headers, rows):
    header_html = "".join(f'<th scope="col">{_text(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="wide"><table><thead><tr>{header_html}</tr></thead><tbody>{body}</tbody></table></div>'


def _event(value):
    if isinstance(value, dict):
        return _text(value.get("message") or value.get("reason") or value.get("outcome") or value.get("code"))
    return _text(value)


def _trial_page(snapshot):
    state = snapshot.get("state")
    states = {"collecting":"采集中", "running":"运行中", "waiting":"等待试验开始", "waiting_data":"等待新行情", "starting":"启动中",
              "paused":"暂停", "degraded":"等待试验循环恢复", "error":"异常", "retrying":"正在重试", "stopped":"已停止"}
    feed = snapshot.get("feed") or {}
    sides = feed.get("quote_sides") or []
    if isinstance(sides, (list, tuple)):
        sides = "、".join({"Up": "涨", "Down":"跌"}.get(k,k) for k in sides) or "等待盘口"
    parts = [f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15">
<title>Paper 策略对照</title><style>{_CSS}</style></head><body><main><header><h1>Paper 策略对照</h1>
<p>{_text(states.get(state,state))} · 北京时间 {_time(snapshot.get('start_at'))} 开始 · 最近更新 {_time(snapshot.get('as_of'))}</p>
<p>市场 {_text(feed.get('round_id'))} · 参考行情{'就绪' if feed.get('reference_ready') else '等待校验'} ·
盘口 {_text(sides)} · 行情连接 {_text(feed.get('ws_state'))}</p></header>"""]
    errors = [(name, feed[key]) for key,name in (("diagnostics_error", "诊断"), ("settlement_error","结算"),
                                                 ("last_market_error","公开行情")) if feed.get(key)]
    if errors:
        parts.append("<p>" + " · ".join(f"{_text(name)}异常 {_text(err)}" for name,err in errors) + "</p>")
    accounts = snapshot.get("accounts") or []
    parts.append("<section><h2>模拟钱包</h2>")
    parts.append(_table(["策略", "初始资金", "现金", "已实现净收益", "持仓成本", "结清局", "买入成交", "已提交", "待成交", "最近记录"], [
        (_text(a.get("label")), _money(a.get("initial")), _money(a.get("cash")), _money(a.get("realized"),signed=True),
         _money(a.get("open_cost")), _text(a.get("closed_markets")), _text(a.get("buy_fills")),
         _text(a.get("submitted")), _text(a.get("pending")), _event(a.get("last_event"))) for a in accounts]))
    for a in accounts:
        details = a.get("details") or {}
        if "skipped" in details:
            parts.append(f'<p>{_text(a.get("label"))} 已检查 {_text(details.get("checked"))} 次，'
                         f'低于每份含费买价 {_money(details.get("floor"))} 而跳过 {_text(details.get("skipped"))} 次，'
                         f'覆盖 {_text(details.get("skipped_markets"))} 个跳过的市场。</p>')
        if "first_seen" in details:
            parts.append(f'<p>{_text(a.get("label"))} 首次达到溢价卖出条件 {_text(details.get("first_seen"))} 次，'
                         f'新报价确认 {_text(details.get("confirmed"))} 次，已提交卖单 {_text(details.get("submitted"))} 笔。</p>')
            latest = details.get("latest") or []
            if latest:
                outcomes = {"waiting":"等待新报价", "confirmed":"已确认", "submitted":"已提交", "withdrawn":"条件撤销",
                            "interrupted":"重启后重新确认", "invalidated":"行情已失效", "superseded":"已有其他委托"}
                parts.append(_table(["近期溢价信号市场", "首次触发", "二次确认", "结果"], [
                    (_text(r.get("round_id")), _time(r.get("first_at")), _time(r.get("confirmed_at")),
                     _text(outcomes.get(r.get("outcome"),r.get("outcome")))) for r in latest]))
    pairs = {a.get("pair"): a.get("label","") for a in accounts if a.get("role")=="variant"}
    pairs.update({"fast":"极速版", "forecast":"方向标准版"})
    parts.append("</section><section><h2>同笔入场对照</h2><p>收益只比较同一批决策、相同方向、买入金额和份数且均已结清的市场。</p>")
    parts.append(_table(["实验", "双方可比市场", "对照扣费收益", "实验扣费收益", "仅对照买入", "仅实验买入", "双方结清但入场不同"], [
        (_text(pairs.get(c.get("pair"),c.get("pair"))), _text(c.get("matched")), _money(c.get("control_pnl"),signed=True),
         _money(c.get("variant_pnl"),signed=True), _text(c.get("control_only")), _text(c.get("variant_only")), _text(c.get("different_entries")))
        for c in snapshot.get("comparisons") or []]))
    diag = snapshot.get("diagnostics") or {}
    counts = diag.get("counts") or []
    parts.append('</section><section><h2>候选策略的行情与入场条件</h2>')
    parts.append(f'<p>{_text(diag.get("source"))} 最近采样 {_time(diag.get("last_at"))}，'
                 f'以下统计从 {_time(diag.get("counts_since",diag.get("start_at")))} 起。</p>')
    parts.append('<div class="grid">')
    for subject, title in (("quality", "收盘前行情"), ("rev_return", "从目标外向回走"),
                           ("close_diverge", "现货逆向、均价仍领先")):
        parts.append(f"<div><h3>{title}</h3>")
        group = [r for r in counts if r.get("subject")==subject]
        if subject == "quality":
            rows = [(_text(r.get("reason")), _text(r.get("samples")), _text(r.get("rounds")))
                    for r in group if r.get("band")=="tail"]
            parts.append(f'<small>距收盘 {_text(diag.get("tail_seconds",17))} 秒内，每次采样可能有多项问题。</small>')
            parts.append(_table(["行情字段或状态","采样秒数","涉及市场"], rows) if rows else "<p>等待首次尾盘采样。</p>")
        else:
            totals = {}
            for r in group:
                reason = r.get("reason")
                totals[reason] = totals.get(reason,0) + r.get("samples",0)
            rows = [(_text(reason), _text(n)) for reason,n in sorted(totals.items(),key=lambda v:-v[1])]
            parts.append(_table(["入场条件","采样秒数"],rows) if rows else "<p>等待首次条件检查。</p>")
        parts.append("</div>")
    parts.append("</div></section><section><h2>原候选账户卖单记录</h2>")
    parts.append(f'<p>{_text(diag.get("source_status"))} · 收录 {_text(diag.get("sell_count",0))} 笔，'
                 '深度取自同一合约在事件前的邻近旁路盘口，空值代表未采到合格报价。</p>')
    sales = diag.get("recent_sells") or []
    if sales:
        def book(q):
            if not q:
                return "—"
            return (f'买一 {_text(q.get("best_bid"))} · 限价内 {_text(q.get("displayed_shares_at_limit"))} 份'
                    f' · 提前 {_text(q.get("offset_ms"))} 毫秒')
        rows = []
        for o in sales:
            status = {"FILLED":"成交", "PARTIAL":"部分成交", "CANCELLED":"撤单", "PENDING":"待成交"}.get(o.get("status"),o.get("status"))
            levels = "、".join(f'{_text(v.get("price"))}×{_text(v.get("shares"))}' for v in o.get("fills",[]) if isinstance(v,dict))
            rows.append((_time(o.get("submitted_at")), _text(o.get("strategy_id")), _text(o.get("round_id")), _text(o.get("reason")),
                         _text(o.get("limit_price")), book(o.get("observer_at_submit")), book(o.get("observer_at_finish")),
                         f'{_text(status)} · {_text(o.get("filled_shares"))}/{_text(o.get("requested_shares"))} 份',
                         levels or _text(o.get("result_reason"))))
        parts.append(_table(["提交时间","策略","市场","卖出原因","限价","提交前旁路盘口","处理前旁路盘口","实际结果","成交档位或撤单原因"],rows))
    else:
        parts.append("<p>正在等待试验开始后原候选账户的新卖单。</p>")
    parts.append('</section><p><a href="/api/trials">查看 JSON 数据</a></p></main></body></html>')
    return "".join(parts)


def _loopback(name):
    if name == "localhost":
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False


def serve(server_address, snapshot):
    """Construct a loopback-only server; the caller owns serve_forever()/shutdown()."""
    bind, port = server_address
    if not _loopback(bind):
        raise ValueError("试验面板只允许监听本机回环地址")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._read(False)

        def do_HEAD(self):
            self._read(True)

        def _reply(self, status, content_type, body, head):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _read(self, head):
            plain = "text/plain; charset=utf-8"
            try:
                host = urlsplit("//" + self.headers.get("Host", ""))
                _ = host.port  # Validate a supplied port inside the forwarded Host header.
                valid = (host.username is None and host.password is None and not host.path and
                         not host.query and not host.fragment and _loopback(host.hostname or ""))
            except ValueError:
                valid = False
            if not valid or not _loopback(self.client_address[0]):
                self._reply(403, plain, b"Loopback only", head)
                return
            path = self.path.split("?",1)[0]
            if path == "/health":
                self._reply(200, "application/json; charset=utf-8", b'{"ok":true}', head)
                return
            if path not in ("/", "/api/trials"):
                self._reply(404, plain, b"Not found", head)
                return
            try:
                data = snapshot()
                if path == "/api/trials":
                    content = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
                    mime = "application/json; charset=utf-8"
                else:
                    content = _trial_page(data).encode()
                    mime = "text/html; charset=utf-8"
            except Exception:
                self._reply(503, plain, "暂时无法读取试验状态".encode(), head)
                return
            self._reply(200, mime, content, head)

        def log_message(self, format, *args):
            return

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET

    return Server((bind,port), Handler)

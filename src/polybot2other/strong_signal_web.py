"""Loopback, read-only HTTP dashboard for the independent first strong signal wallet."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from urllib.parse import urlsplit

from .paired_trial_web import _loopback


def serve(server_address, snapshot):
    """Return the HTTP server. The caller owns its lifetime and the Paper engine."""
    host, port = server_address
    if not _loopback(host):
        raise ValueError("策略监控只允许监听本机回环地址")
    static = Path(__file__).with_name("static")
    assets = {
        "/": ("text/html; charset=utf-8", (static / "strong-signal.html").read_bytes()),
        "/strong-signal.js": ("text/javascript; charset=utf-8", (static / "strong-signal.js").read_bytes()),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.read_resource(False)

        def do_HEAD(self):
            self.read_resource(True)

        def reply(self, status, mime, body, head):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                             "frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def local_request(self):
            try:
                authority = urlsplit("//" + self.headers.get("Host", ""))
                _ = authority.port
            except ValueError:
                return False
            return (_loopback(self.client_address[0]) and _loopback(authority.hostname or "") and
                    authority.username is None and authority.password is None and not authority.path and
                    not authority.query and not authority.fragment)

        def read_resource(self, head):
            if not self.local_request():
                return self.reply(403, "text/plain", b"Loopback only", head)
            path = self.path.partition("?")[0]
            if path in assets:
                mime, body = assets[path]
                return self.reply(200, mime, body, head)
            if path not in ("/api/status", "/healthz"):
                return self.reply(404, "text/plain", b"Not found", head)
            try:
                data = snapshot()
                if path == "/healthz":
                    data = {"ok": True, "state": data["state"], "as_of": data["as_of"]}
                body = json.dumps(data, allow_nan=False, ensure_ascii=False).encode()
            except Exception:
                return self.reply(503, "text/plain; charset=utf-8", "暂时无法读取模拟账本".encode(), head)
            self.reply(200, "application/json; charset=utf-8", body, head)

        def log_message(self, format, *args):
            pass

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    return Server((host, port), Handler)

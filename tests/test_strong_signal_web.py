import http.client
import json
import threading
import unittest

from polybot2other.strong_signal_web import serve


class StrongSignalDashboardTests(unittest.TestCase):
    def setUp(self):
        self.reads = []
        self.state = {"key": "MAIN_FIRST_STRONG", "state": "collecting", "as_of": 99,
                      "account": {"settled": 0}, "last_event": {"message": "<demo>"}}
        def snapshot():
            self.reads.append(True)
            return self.state
        self.server = serve(("127.0.0.1", 0), snapshot)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.worker.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address[:2], timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    def test_assets_and_fresh_status(self):
        code, headers, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("主账户 · 每局首个强信号", body.decode())
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"<demo>", body)
        self.assertEqual(self.request("/strong-signal.js")[0], 200)
        self.assertEqual(self.reads, [])
        self.assertEqual(json.loads(self.request("/api/status")[2]), self.state)
        self.state["as_of"] += 1
        self.assertEqual(json.loads(self.request("/healthz")[2]), {"ok": True, "as_of": 100, "state": "collecting"})
        self.assertEqual(self.request("/api/status", "HEAD")[2], b"")
        self.state["account"]["cash"] = float("nan")
        self.assertEqual(self.request("/api/status")[0], 503)

    def test_external_host_and_changes_have_no_access(self):
        self.assertEqual(self.request("/api/status", headers={"Host": "example.org"})[0], 403)
        self.assertEqual(self.request("/api/status", headers={"Host": "127.0.0.1@foo.example"})[0], 403)
        self.assertGreaterEqual(self.request("/api/status", "POST")[0], 400)
        self.assertEqual(self.request("/api/managed-live")[0], 404)
        self.assertEqual(self.reads, [])
        with self.assertRaises(ValueError):
            serve(("0.0.0.0", 0), lambda:self.state)

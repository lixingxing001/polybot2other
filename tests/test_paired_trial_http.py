"""The isolated trial service exposes only loopback GETs and displays untrusted feed text safely."""
import http.client
import json
import threading
import unittest

from polybot2other.paired_trial_web import serve


class TrialHttpTests(unittest.TestCase):
    def setUp(self):
        self.tip = '<img src=x onerror=window.INJECTED=true>'
        self.sample = {
            "start_at": 1795059300, "as_of": 1795059315, "state": "collecting",
            "feed": {"round_id": "btc-updown-5m-1795059300", "reference_ready": True,
                     "quote_sides": ["Up", "Down"], "ws_state": "connected"},
            "accounts": [{"key": "fast_control", "label": self.tip, "pair": "fast", "role": "control",
                          "initial": 100, "cash": 97.8, "realized": -.2, "closed_markets": 1, "buy_fills": 2,
                          "submitted": 3, "open_cost": 2, "pending": 0, "last_event": None, "details": {}}],
            "comparisons": [{"pair":"fast", "matched": 0, "control_pnl": 0, "variant_pnl": 0,
                             "control_only": 1, "variant_only": 0}],
            "diagnostics": {"start_at":1795059300, "last_at":1795059315, "source_status":"候选卖单已核对",
                            "source": "旁路观测", "tail_seconds":17, "counts":[], "recent_sells": [],
                            "sell_count":0, "observer_gaps":0, "largest_observer_gap_seconds":None},
        }
        self.httpd = serve(("127.0.0.1", 0), lambda:self.sample)
        worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address[:2], timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            res = conn.getresponse()
            return res.status, res.getheader("Content-Type", ""), res.read()
        finally:
            conn.close()

    def test_trial_data_and_home_are_readable(self):
        status, content, body = self.get("/api/trials")
        self.assertEqual(status,200)
        self.assertIn("application/json",content)
        self.assertEqual(json.loads(body)["accounts"][0]["label"],self.tip)
        status, content, body = self.get("/")
        self.assertEqual(status,200)
        self.assertIn("text/html",content)
        self.assertNotIn(self.tip,body.decode())
        self.assertEqual(self.get("/health")[0],200)
        self.assertEqual(self.get("/missing")[0],404)

    def test_external_host_and_mutating_methods_are_rejected(self):
        self.assertGreaterEqual(self.get("/api/trials",headers={"Host":"unrelated.invalid"})[0],400)
        self.assertGreaterEqual(self.get("/api/trials",method="POST")[0],400)

    def test_wildcard_listening_is_rejected(self):
        with self.assertRaises((ValueError,OSError)):
            server=serve(("0.0.0.0",0), lambda:self.sample)
            server.server_close()

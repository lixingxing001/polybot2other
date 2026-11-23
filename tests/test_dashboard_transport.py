import copy
import gzip
import http.client
import json
import threading
import unittest
import zlib
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from polybot2other.dashboard_transport import StatusStream, accepts_gzip, json_bytes
from polybot2other.web import Handler


@contextmanager
def dashboard(snapshots):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    snapshots = iter(snapshots)
    latest = None

    def status_snapshot():
        nonlocal latest
        latest = next(snapshots, latest)
        return latest

    server.status_snapshot = status_snapshot
    server.bot = SimpleNamespace(managed_live=SimpleNamespace(
        control_token="test-token", snapshot=lambda: {"description": "x" * 1000}
    ))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(address, path, method="GET", **headers):
    conn = http.client.HTTPConnection(*address, timeout=2)
    try:
        conn.request(method, path, headers=headers)
        with conn.getresponse() as response:
            return response.status, response.headers, response.read()
    finally:
        conn.close()


def stream_frames(address, path, count, **headers):
    conn = http.client.HTTPConnection(*address, timeout=2)
    try:
        conn.request("GET", path, headers=headers)
        with conn.getresponse() as response:
            compressed = response.headers.get("Content-Encoding") == "gzip"
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if compressed else None
            data = b""
            while data.count(b"\n\n") < count:
                chunk = response.read1(65536)
                if not chunk:
                    raise AssertionError("unexpected end of event stream")
                data += decoder.decompress(chunk) if decoder else chunk
            frames = [
                json.loads(line.removeprefix(b"data: "))
                for line in data.splitlines() if line.startswith(b"data: ")
            ]
            return response.headers, frames[:count]
    finally:
        conn.close()


def apply_delta(data, delta):
    for path, value in delta["set"]:
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    for path in delta["remove"]:
        node = data
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]


class DashboardTransportTests(unittest.TestCase):
    def test_delta_reconstructs_nested_objects_arrays_and_deletions(self):
        stream = StatusStream()
        old = {"history": "old result " * 200, "object": {"a": None, "b": True},
               "array": [{"a": 0, "b": 2}], "shrinks": [3, 5]}
        first = json.loads(stream.frame(json_bytes(old))[6:])
        self.assertEqual(first["data"], old)
        new = copy.deepcopy(old)
        new["object"] = {"b": 1, "c": None}
        new["array"][0]["a"] = 99
        new["shrinks"].pop()
        second = json.loads(stream.frame(json_bytes(new))[6:])
        self.assertEqual((second["type"], second["base"], second["seq"]), ("delta", 1, 2))
        self.assertIn([["object", "b"], 1], second["set"])
        self.assertIn([["array", 0, "a"], 99], second["set"])
        apply_delta(first["data"], second)
        self.assertEqual(first["data"], new)
        ping = json.loads(stream.frame(json_bytes(new))[6:])
        self.assertEqual((ping["type"], ping["seq"]), ("ping", 2))
        self.assertIsInstance(ping["sent_at_ms"], int)

    def test_full_fallback_and_reconnect_start_from_current_state(self):
        stream = StatusStream()
        stream.frame(b'{"a":1}')
        second = json.loads(stream.frame(b'{"b":2}')[6:])
        self.assertEqual((second["type"], second["seq"], second["data"]), ("full", 2, {"b": 2}))
        fresh = json.loads(StatusStream().frame(b'{"c":3}')[6:])
        self.assertEqual((fresh["type"], fresh["seq"], fresh["data"]), ("full", 1, {"c": 3}))

    def test_encoding_negotiation(self):
        for header in ["gzip", "gzip, deflate, br", "*;q=.5", "GZip; Q=0.1"]:
            self.assertTrue(accepts_gzip(header))
        for header in ["", "br", "gzip;q=0, *;q=1", "gzip;q=garbage", "gzip;q=2"]:
            self.assertFalse(accepts_gzip(header))

    def test_html_static_and_conditional_head(self):
        with dashboard([{}]) as address:
            status, head, data = request(address, "/", **{"Accept-Encoding": "gzip"})
            self.assertEqual((status, head["Cache-Control"], head["Content-Encoding"]), (200, "no-store", "gzip"))
            self.assertIn(b"<html", gzip.decompress(data))
            path = "/static/app.js?v=unit-test"
            _, plain, content = request(address, path)
            status, zipped, data = request(address, path, **{"Accept-Encoding": "gzip"})
            self.assertEqual((status, zipped["Vary"], zipped["ETag"]), (200, "Accept-Encoding", plain["ETag"]))
            self.assertEqual(gzip.decompress(data), content)
            self.assertEqual(request(address, path, "HEAD", **{"Accept-Encoding": "gzip"})[1]["Content-Length"], str(len(data)))
            status, cached, body = request(address, path, **{"If-None-Match": zipped["ETag"]})
            self.assertEqual((status, cached["ETag"], body), (304, zipped["ETag"], b""))

    def test_only_read_only_non_secret_payloads_compressed(self):
        public = {"description": "x" * 1200}
        sensitive = {"inner": {"control_token": "test-token"}, **public}
        with dashboard([public, sensitive]) as address:
            status, head, data = request(address, "/api/status", **{"Accept-Encoding": "gzip"})
            self.assertEqual((status, head["Content-Encoding"], json.loads(gzip.decompress(data))), (200, "gzip", public))
            for path in ["/api/status", "/api/managed-live"]:
                status, head, data = request(address, path, **{"Accept-Encoding": "gzip"})
                self.assertEqual(status, 200)
                self.assertIsNone(head["Content-Encoding"])
                self.assertIn(b"control_token", data)

    def test_incremental_gzip_stream_flushes_first_update_and_heartbeat(self):
        old = {"history": "x" * 2000, "v": 1}
        new = {**old, "v": 2}
        with patch("polybot2other.web.STATUS_STREAM_INTERVAL_SECONDS", .03), dashboard([old, new]) as address:
            head, frames = stream_frames(address, "/api/status-stream?format=delta", 3, **{"Accept-Encoding": "gzip"})
            self.assertEqual(head["Content-Encoding"], "gzip")
            self.assertEqual([f["type"] for f in frames], ["full", "delta", "ping"])
            self.assertEqual([f["seq"] for f in frames], [1, 2, 2])
            self.assertEqual(frames[1]["set"], [[["v"], 2]])
            head, frames = stream_frames(address, "/api/status-stream?format=delta", 1)
            self.assertIsNone(head["Content-Encoding"])
            self.assertEqual((frames[0]["seq"], frames[0]["data"]), (1, new))
        with dashboard([new]) as address:
            head, frames = stream_frames(address, "/api/status-stream", 1)
            self.assertIsNone(head["Content-Encoding"])
            self.assertEqual(frames, [new])


if __name__ == "__main__":
    unittest.main()

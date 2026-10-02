"""score_bench.py never hangs: every HTTP call and every wait is bounded.

2026-10-02: one bench sat >2 h on a single request — urllib's `timeout`
bounds each socket operation, not the call. Loopback only; no Tools call.
"""
import os
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", ".claude", "skills", "tools-health", "scripts"))
import score_bench  # noqa: E402


class SilentServer:
    """Accepts connections and never answers (or trickles one byte a while)."""

    def __init__(self, trickle=False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.trickle = trickle
        self.stop = threading.Event()
        self.conns = []
        threading.Thread(target=self.serve, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.sock.getsockname()[1]

    def serve(self):
        self.sock.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.conns.append(conn)
            if self.trickle:
                threading.Thread(target=self.drip, args=(conn,), daemon=True).start()

    def drip(self, conn):
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            while not self.stop.is_set():
                conn.sendall(b"x")  # every read succeeds: a per-op timeout never fires
                time.sleep(0.05)
        except OSError:
            pass

    def close(self):
        self.stop.set()
        for conn in self.conns:
            conn.close()
        self.sock.close()


class BenchTimeoutTests(unittest.TestCase):
    def run_against(self, server, bound):
        self.addCleanup(server.close)
        with patch.object(score_bench, "TOOLS_URL", server.url):
            started = time.monotonic()
            result, _secs = score_bench.call("web_form_score_probe", {}, "k", timeout=bound)
            return result, time.monotonic() - started

    def test_a_silent_peer_is_abandoned_at_the_bound(self):
        result, took = self.run_against(SilentServer(), 0.5)
        self.assertEqual(result["error"], "TimeoutError")
        self.assertLess(took, 3.0)

    def test_a_trickling_peer_is_abandoned_at_the_bound(self):
        # Each recv returns a byte, so urllib's per-op timeout never fires.
        result, took = self.run_against(SilentServer(trickle=True), 0.5)
        self.assertEqual(result["error"], "TimeoutError")
        self.assertTrue(result.get("hard_timeout_s"))
        self.assertLess(took, 3.0)

    def test_budget_is_the_probe_timeout_plus_margin(self):
        self.assertEqual(score_bench.budget_s("web_form_score_probe", {}),
                         120 + score_bench.MARGIN_S)
        self.assertEqual(score_bench.budget_s("web_form_score_probe", {"timeout_ms": 180000}),
                         180 + score_bench.MARGIN_S)
        self.assertEqual(score_bench.budget_s("web_form_exit_select", {}),
                         270 + score_bench.MARGIN_S)

    def test_waiting_out_restarts_is_bounded_in_total(self):
        clock = [0.0]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            clock[0] += s

        def call(tool, body, key):
            clock[0] += 200.0  # each refused call itself takes a while
            return {"error": "HTTP 503"}, 200.0

        with patch.object(score_bench, "call", call):
            result, _secs, attempts = score_bench.call_when_up(
                "web_form_score_probe", {}, "k", away_budget_s=600, pause_s=75,
                _sleep=sleep, _clock=lambda: clock[0])
        self.assertEqual(result["error"], "HTTP 503")
        self.assertLessEqual(clock[0], 600 + 200)  # at most one call past the budget
        self.assertTrue(all(s <= 75 for s in sleeps))
        self.assertLess(attempts, 8)

    def test_a_real_answer_returns_at_once(self):
        with patch.object(score_bench, "call", lambda *a, **k: ({"score": 0.9}, 1.0)):
            result, _secs, attempts = score_bench.call_when_up(
                "web_form_score_probe", {}, "k", _sleep=self.fail)
        self.assertEqual(result["score"], 0.9)
        self.assertEqual(attempts, 0)


if __name__ == "__main__":
    unittest.main()

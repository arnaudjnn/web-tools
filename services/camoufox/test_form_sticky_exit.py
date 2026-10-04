"""Exit coherence between the score gate and the form it gates.

Atoka, 2026-10-03 00:48 UTC: the gate scored an exit on ASN 16232 (0.9) and
the form, relaunched seconds later on the SAME proxy session token, egressed
from ASN 3269 — so the score described an exit the form never used. Two
fixes, both tested here without a browser or network:

1. the Evomi session options (proxy_session.py): a valid 6-10 char id and an
   explicit lifetime, never a raw caller string spliced into the password;
2. the form re-verifies its egress IP against the gate's BEFORE it contacts
   the target (zero input, zero target), and a mismatch re-gates once, then
   answers a zero-POST 503 `exit_mismatch`.

app.py is imported with its dependencies stubbed (shared with the other
endpoint tests).
"""
import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import form_flow
import proxy_session
import test_form_flow as _flow
import test_form_retry as _retry
import test_form_score_gate as _gate
from form_flow import run_form

app = _retry.app
score_probe = _gate.score_probe

PROXY = "http://user:secretpass_country-IT@proxy.test:1234"


# ── 1. the session token ───────────────────────────────────────────

class TokenTests(unittest.TestCase):
    def test_new_tokens_are_valid_evomi_ids(self):
        tokens = {proxy_session.new_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for token in tokens:
            self.assertRegex(token, r"^[A-Za-z0-9]{6,10}$")
            self.assertEqual(proxy_session.session_id(token), token)

    def test_out_of_spec_tokens_map_onto_a_stable_valid_id(self):
        # Legacy 12-hex pins (profiles, echoed exit_session) keep "same
        # token, same session" — just on a valid id.
        legacy = "a1b2c3d4e5f6"
        sid = proxy_session.session_id(legacy)
        self.assertRegex(sid, r"^[A-Za-z0-9]{10}$")
        self.assertEqual(sid, proxy_session.session_id(legacy))
        self.assertNotEqual(sid, proxy_session.session_id("a1b2c3d4e5f7"))
        self.assertEqual(proxy_session.session_id("abc"), proxy_session.session_id("abc"))
        self.assertRegex(proxy_session.session_id("abc"), r"^[A-Za-z0-9]{10}$")

    def test_a_caller_token_cannot_inject_proxy_options(self):
        # exit_session is caller-controlled: "_lifetime-1" or "_country-US"
        # must never reach the provider as options.
        for token in ("abcdef12_lifetime-1", "abcdef12_country-US", "x-y-z", "a b"):
            options = proxy_session.options(token)
            self.assertEqual(options.count("_"), 2, options)
            self.assertNotIn("country", options)
            self.assertNotIn("_lifetime-1_", options + "_")

    def test_default_options_are_a_session_with_an_explicit_lifetime(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROXY_SESSION_KIND", None)
            os.environ.pop("PROXY_SESSION_LIFETIME_MIN", None)
            self.assertEqual(proxy_session.options("abcdef1234"), "_session-abcdef1234_lifetime-60")

    def test_lifetime_is_clamped_to_ten_minutes_and_the_provider_max(self):
        for raw, want in (("5", 10), ("10", 10), ("90", 90), ("5000", 1440), ("junk", 60)):
            with patch.dict(os.environ, {"PROXY_SESSION_LIFETIME_MIN": raw}):
                self.assertEqual(proxy_session.lifetime_min(), want, raw)

    def test_hardsession_takes_no_lifetime(self):
        with patch.dict(os.environ, {"PROXY_SESSION_KIND": "hardsession"}):
            self.assertEqual(proxy_session.options("abcdef1234"), "_hardsession-abcdef1234")
        with patch.dict(os.environ, {"PROXY_SESSION_KIND": "bogus"}):
            self.assertTrue(proxy_session.options("abcdef1234").startswith("_session-"))

    def test_parse_proxy_appends_the_options_to_the_password_only(self):
        with patch.dict(os.environ, {"PROXY_SESSION_LIFETIME_MIN": "30"}):
            proxy = app.parse_proxy(PROXY, "abcdef1234")
        self.assertEqual(proxy, {"server": "http://proxy.test:1234", "username": "user",
                                 "password": "secretpass_country-IT_session-abcdef1234_lifetime-30"})
        self.assertEqual(app.parse_proxy(PROXY, None)["password"], "secretpass_country-IT")

    def test_a_provider_pinned_session_is_left_alone(self):
        for pinned in ("p_session-abcdef12", "p_hardsession-abcdef12", "p_lockedsession-abcdef12"):
            url = "http://user:%s@proxy.test:1" % pinned
            self.assertEqual(app.parse_proxy(url, "zzzzzz9999")["password"], pinned)

    def test_parse_proxy_never_logs_the_credential(self):
        with self.assertNoLogs(level="DEBUG"):
            app.parse_proxy(PROXY, "abcdef1234")


# ── 2. the form verifies its exit before touching the target ───────

def _echo(ip):
    return Mock(json=Mock(return_value={"success": True, "ip": ip, "country": "Italy",
                                        "connection": {"asn": 3269, "isp": "Telecom Italia"}}))


class ExitCheckTests(unittest.TestCase):
    setUp = _flow.FormTests.setUp
    submit = _flow.FormTests.submit

    def drive(self, echo_ip, **kw):
        visited = []

        def goto(url, **_):
            visited.append(url)
            if url == form_flow.EGRESS_URL:
                if isinstance(echo_ip, Exception):
                    raise echo_ip
                return _echo(echo_ip)
            return Mock(status=200)

        self.page.goto.side_effect = goto
        return run_form(self.context, **self.params, **kw), visited

    def test_a_matching_exit_runs_the_form(self):
        result, visited = self.drive("151.0.0.7", expect_ip="151.0.0.7")
        self.assertTrue(result["ok"])
        self.assertEqual(visited[:2], [form_flow.EGRESS_URL, self.params["url"]])
        d = result["diagnostics"]
        self.assertEqual((d["gate_ip"], d["form_ip"], d["exit_verified"]), ("151.0.0.7", "151.0.0.7", True))

    def test_a_moved_exit_stops_before_the_target_and_types_nothing(self):
        result, visited = self.drive("2.36.97.102", expect_ip="151.0.0.7")
        self.assertEqual(result["error"], "exit_mismatch")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(visited, [form_flow.EGRESS_URL])  # the target never saw a request
        self.context.route.assert_not_called()
        self.page.keyboard.type.assert_not_called()
        self.page.locator.return_value.click.assert_not_called()
        d = result["diagnostics"]
        self.assertEqual((d["gate_ip"], d["form_ip"], d["exit_verified"], d["phase"]),
                         ("151.0.0.7", "2.36.97.102", False, "exit_check"))

    def test_an_unreadable_echo_is_not_a_verified_exit(self):
        result, visited = self.drive(RuntimeError("echo down"), expect_ip="151.0.0.7")
        self.assertEqual(result["error"], "exit_mismatch")
        self.assertEqual(visited, [form_flow.EGRESS_URL])
        self.assertIsNone(result["diagnostics"]["form_ip"])
        self.assertNotIn("echo down", str(result))

    def test_without_a_gate_ip_nothing_extra_is_fetched(self):
        result, visited = self.drive("151.0.0.7")
        self.assertTrue(result["ok"])
        self.assertNotIn(form_flow.EGRESS_URL, visited)
        self.assertIsNone(result["diagnostics"]["exit_verified"])

    def test_the_form_run_line_carries_gate_and_form_ip(self):
        live = form_flow.FormLive()
        result, _ = self.drive("2.36.97.102", expect_ip="151.0.0.7", live=live)
        summary = live.summary(result)
        self.assertEqual((summary["gate_ip"], summary["form_ip"], summary["exit_verified"]),
                         ("151.0.0.7", "2.36.97.102", False))


# ── 3. the gate reports the IP its score is about ──────────────────

class GateIpTests(_gate._Root):
    run_gate = _gate.ProbeCandidatesTests.run_gate

    def test_the_chosen_ip_is_the_one_the_oracle_saw(self):
        outcome, _ = self.run_gate([{"ip": "1.1.1.1", "asn": 16232}], [(0.9, "1.1.1.9", 16232)])
        record = score_probe.gate_record(outcome)
        # The pre-check and the probe disagreed: the score is about the probe's.
        self.assertEqual((record["gate_ip"], record["precheck_ip"]), ("1.1.1.9", "1.1.1.1"))
        self.assertEqual(app._gate_ip({"record": record}), "1.1.1.9")

    def test_a_skipped_or_failed_gate_verifies_nothing(self):
        self.assertIsNone(app._gate_ip(None))
        self.assertIsNone(app._gate_ip({"record": {"skipped": "no_oracle"}}))
        self.assertIsNone(app._gate_ip({"record": {"passed": False, "gate_ip": None}}))


# ── 4. a mismatch re-gates once, then is a zero-POST 503 ───────────

def _mismatch(gate_ip, form_ip):
    return {"ok": False, "error": "exit_mismatch", "form_submissions": 0, "status": 0,
            "url": "https://form.test/f", "html": "",
            "diagnostics": {"gate_ip": gate_ip, "form_ip": form_ip, "exit_verified": False,
                            "phase": "exit_check"}}


def done():
    return {"ok": True, "error": None, "form_submissions": 1, "status": 302,
            "url": "https://form.test/complete", "html": "",
            "diagnostics": {"submit_click_attempted": True, "exit_verified": True}}


class MismatchTests(unittest.TestCase):
    # Borrowed, not inherited, so EndpointTests is not collected twice.
    setUp = _retry.EndpointTests.setUp
    tearDown = _retry.EndpointTests.tearDown

    def req(self, **kw):
        # These tests exercise the gate; it is opt-in (score_gate=True).
        kw.setdefault("score_gate", True)
        return _retry.EndpointTests.req(self, **kw)

    def attempt(self, req, runs, gates):
        runs, gates = list(runs), list(gates)
        calls = {"gate": [], "run": []}

        async def score_gate(req_, session, deadline, live, recheck=False):
            calls["gate"].append((session, recheck))
            session_, ip = gates.pop(0)
            if session_ is None:
                raise _retry._HTTPError(503, {"retryable": True, "error": "no_scoring_exit",
                                              "form_submissions": 0})
            return {"session": session_, "record": {"passed": True, "gate_ip": ip, "chosen_score": 0.9}}

        async def form_run(req_, deadline, session, live, rotate, expect_ip):
            calls["run"].append((session, expect_ip, rotate))
            return runs.pop(0)

        with patch.object(app, "_score_gate", score_gate), patch.object(app, "_form_run", form_run), \
                patch.object(app, "HTTPException", _retry._HTTPError):
            live = form_flow.FormLive()
            result = asyncio.run(app._form_attempt_run(req, time.monotonic() + 300, "tok0000001", live))
        return result, calls

    def test_the_form_is_told_the_gates_ip(self):
        (data, gate, session), calls = self.attempt(self.req(), [done()], [("tokA000001", "1.1.1.1")])
        self.assertEqual(calls["run"], [("tokA000001", "1.1.1.1", None)])
        self.assertEqual(data["diagnostics"]["score_gate"]["gate_ip"], "1.1.1.1")
        self.assertNotIn("exit_mismatches", data["diagnostics"])

    def test_a_mismatch_rejudges_the_exit_it_moved_to_and_runs_on_the_new_ip(self):
        (data, gate, session), calls = self.attempt(
            self.req(), [_mismatch("1.1.1.1", "2.2.2.2"), done()],
            [("tokA000001", "1.1.1.1"), ("tokA000001", "2.2.2.2")])
        self.assertTrue(data["ok"])
        self.assertEqual(calls["gate"], [("tok0000001", False), ("tokA000001", True)])
        self.assertEqual([r[:2] for r in calls["run"]], [("tokA000001", "1.1.1.1"), ("tokA000001", "2.2.2.2")])
        self.assertEqual(data["diagnostics"]["exit_mismatches"], [{"gate_ip": "1.1.1.1", "form_ip": "2.2.2.2"}])
        self.assertEqual(data["diagnostics"]["score_gate"]["gate_ip"], "2.2.2.2")

    def test_a_second_mismatch_is_a_zero_post_503(self):
        with self.assertRaises(_retry._HTTPError) as caught:
            self.attempt(self.req(), [_mismatch("1.1.1.1", "2.2.2.2"), _mismatch("2.2.2.2", "3.3.3.3")],
                         [("tokA000001", "1.1.1.1"), ("tokA000001", "2.2.2.2")])
        error = caught.exception
        self.assertEqual(error.status_code, 503)
        self.assertEqual((error.detail["retryable"], error.detail["error"], error.detail["form_submissions"]),
                         (True, "exit_mismatch", 0))
        self.assertEqual(len(error.detail["exit_mismatches"]), 2)

    def test_a_failed_regate_keeps_the_mismatch_evidence(self):
        with self.assertRaises(_retry._HTTPError) as caught:
            self.attempt(self.req(), [_mismatch("1.1.1.1", "2.2.2.2")],
                         [("tokA000001", "1.1.1.1"), (None, None)])
        self.assertEqual(caught.exception.detail["error"], "no_scoring_exit")
        self.assertEqual(caught.exception.detail["exit_mismatches"], [{"gate_ip": "1.1.1.1", "form_ip": "2.2.2.2"}])

    def test_a_mismatch_retries_through_the_endpoint_loop_as_zero_post(self):
        # The 503 is zero-POST retryable: on a retry attempt it is an
        # AttemptRefused like no_scoring_exit (form_retry's contract).
        async def run(req_, deadline, session, live):
            raise app._exit_mismatch_503([{"gate_ip": "a", "form_ip": "b"}], None)

        with patch.object(app, "_form_attempt_run", run), patch.object(app, "HTTPException", _retry._HTTPError):
            with self.assertRaises(app.form_retry.AttemptRefused) as caught:
                asyncio.run(app._form_attempt(self.req(), time.monotonic() + 300, "tok0000001", retry=True))
        self.assertEqual(caught.exception.cause.detail["error"], "exit_mismatch")


class RecheckSessionsTests(_gate._Root):
    def gate(self, **kw):
        req = _retry.EndpointTests.req(None, **kw)
        seen = {}

        async def run_gate(**k):
            seen.update(k)
            return {"passed": True, "session": "tokB000001", "score": 0.9, "egress": {"ip": "5.5.5.5"},
                    "tries": [{"score": 0.9}], "ip": "5.5.5.5", "precheck_ip": "5.5.5.5"}

        with patch.object(score_probe, "run_gate", run_gate):
            asyncio.run(app._score_gate(req, "tokA000001", time.monotonic() + 300, form_flow.FormLive(),
                                        recheck=True))
        return seen

    def test_an_unpinned_recheck_judges_the_moved_exit_first_then_fresh_ones(self):
        seen = self.gate()
        self.assertEqual((seen["sessions"], seen["tries"]), (["tokA000001"], 3))

    def test_a_caller_pinned_exit_is_rejudged_once_never_replaced(self):
        seen = self.gate(exit_session="mine000001", score_gate=True)
        self.assertEqual((seen["sessions"], seen["tries"]), (["mine000001"], 1))


# ── 5. opt-in real browser: the check runs before the target sees a byte ──

@unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST"), "opt-in browser fixture")
class BrowserExitCheckTests(unittest.TestCase):
    """Loopback only: the echo is our own fixture standing in for ipwho.is."""

    def exercise(self, echo_ip, expect_ip):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from test_form_browser import browser_engine

        hits = {"echo": 0, "form_get": 0, "post": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path.startswith("/echo"):
                    hits["echo"] += 1
                    body = json.dumps({"success": True, "ip": echo_ip, "country": "Italy",
                                       "connection": {"asn": 3269, "isp": "fixture"}}).encode()
                    ctype = "application/json"
                else:
                    hits["form_get"] += 1
                    body = (b'<form method="post" action="/form"><input id="name" name="name">'
                            b'<button id="submit">Submit</button></form>')
                    ctype = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                hits["post"] += 1
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(303)
                self.send_header("Location", "/done")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(form_flow, "EGRESS_URL", base + "/echo"), browser_engine() as browser:
                context = browser.new_context(service_workers="block")
                result = run_form(context, url=base + "/form", fields=[{"selector": "#name", "value": "Test"}],
                                  submit="#submit", success_url=r"/done$", settle_ms=1000,
                                  timeout_ms=15000, expect_ip=expect_ip)
                context.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        return result, hits

    def test_a_moved_exit_never_reaches_the_target(self):
        result, hits = self.exercise("10.0.0.2", "10.0.0.1")
        self.assertEqual(result["error"], "exit_mismatch")
        self.assertEqual((hits["echo"], hits["form_get"], hits["post"]), (1, 0, 0))

    def test_a_verified_exit_submits_once(self):
        result, hits = self.exercise("10.0.0.1", "10.0.0.1")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["diagnostics"]["exit_verified"])
        # echo: the pre-navigation check + the in-page egress check;
        # form_get: the form and the /done redirect target.
        self.assertEqual((hits["echo"], hits["form_get"], hits["post"]), (2, 2, 1))
        self.assertEqual(result["diagnostics"]["form_ip"], "10.0.0.1")


if __name__ == "__main__":
    unittest.main()

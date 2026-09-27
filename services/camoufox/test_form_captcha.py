"""CAPTCHA solving: the solver's provider contract (loopback) and form integration.

The provider is a loopback HTTP server — same JSON shapes as CapSolver,
no external calls. Form-level tests reuse the Mock harness style of
test_form_flow: the rule under test is that a solver failure returns
structured with ZERO submissions BEFORE the click, and a solved token
reaches only the CAPTCHA response field.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import captcha_solver
from captcha_solver import SolverError, solve
from form_flow import run_form


def _start_provider(handler):
    """Serve `handler(path, payload) -> dict` on loopback; returns (server, url)."""

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = handler(self.path, json.loads(self.rfile.read(length)))
            raw = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", server.server_close


class SolverProviderTests(unittest.TestCase):
    def setUp(self):
        self._saved = (captcha_solver.API_KEY, captcha_solver.API_URL, captcha_solver.POLL_MS)
        captcha_solver.POLL_MS = 50  # keep poll sleeps out of the test budget
        self.addCleanup(self._restore)

    def _restore(self):
        captcha_solver.API_KEY, captcha_solver.API_URL, captcha_solver.POLL_MS = self._saved

    def _serve(self, handler):
        server, url, close = _start_provider(handler)
        self.addCleanup(server.shutdown)
        self.addCleanup(close)
        captcha_solver.API_KEY = "test-key"
        captcha_solver.API_URL = url

    def test_solves_v3_through_the_provider_contract(self):
        created = []

        def provider(path, payload):
            if path == "/createTask":
                created.append(payload)
                return {"errorId": 0, "taskId": "t-1"}
            return {"errorId": 0, "status": "ready",
                    "solution": {"gRecaptchaResponse": "solved-token"}}

        self._serve(provider)
        token = solve(sitekey="site-1", page_url="https://x.test/form",
                      action="signup", remaining=lambda: 60_000)
        self.assertEqual(token, "solved-token")
        self.assertEqual(created[0]["clientKey"], "test-key")
        self.assertEqual(created[0]["task"], {
            "type": "ReCaptchaV3TaskProxyLess",
            "websiteURL": "https://x.test/form",
            "websiteKey": "site-1",
            "pageAction": "signup",
        })

    def test_polls_until_ready(self):
        polls = []

        def provider(path, payload):
            if path == "/createTask":
                return {"errorId": 0, "taskId": "t-1"}
            polls.append(payload["taskId"])
            if len(polls) < 3:
                return {"errorId": 0, "status": "processing"}
            return {"errorId": 0, "status": "ready",
                    "solution": {"gRecaptchaResponse": "late-token"}}

        self._serve(provider)
        self.assertEqual(solve(sitekey="s", page_url="https://x.test",
                               remaining=lambda: 60_000), "late-token")
        self.assertEqual(len(polls), 3)

    def test_v2_maps_to_the_v2_task_type(self):
        created = []

        def provider(path, payload):
            if path == "/createTask":
                created.append(payload)
                return {"errorId": 0, "taskId": "t-1"}
            return {"errorId": 0, "status": "ready",
                    "solution": {"gRecaptchaResponse": "tok"}}

        self._serve(provider)
        solve(sitekey="s", page_url="https://x.test", version="v2",
              remaining=lambda: 60_000)
        self.assertEqual(created[0]["task"]["type"], "ReCaptchaV2TaskProxyLess")

    def test_an_exit_switches_the_task_to_that_proxy(self):
        created = []

        def provider(path, payload):
            if path == "/createTask":
                created.append(payload)
                return {"errorId": 0, "taskId": "t-1"}
            return {"errorId": 0, "status": "ready",
                    "solution": {"gRecaptchaResponse": "solved-token"}}

        self._serve(provider)
        solve(sitekey="site-1", page_url="https://x.test/form", action="signup",
              remaining=lambda: 60_000,
              proxy={"server": "http://1.2.3.4:8080", "username": "u",
                     "password": "p_session-abc"})
        # Mint IP must equal the form's submit IP, so the PROXY task family —
        # never the provider's own IPs — carries the very proxy the browser
        # navigates with (session-bearing password included, unchanged).
        self.assertEqual(created[0]["task"]["type"], "ReCaptchaV3Task")
        self.assertEqual(created[0]["task"]["proxy"],
                         "http://u:p_session-abc@1.2.3.4:8080")

    def test_v2_with_an_exit_uses_the_proxy_v2_task(self):
        created = []

        def provider(path, payload):
            if path == "/createTask":
                created.append(payload)
                return {"errorId": 0, "taskId": "t-1"}
            return {"errorId": 0, "status": "ready",
                    "solution": {"gRecaptchaResponse": "tok"}}

        self._serve(provider)
        solve(sitekey="s", page_url="https://x.test", version="v2",
              remaining=lambda: 60_000,
              proxy={"server": "http://1.2.3.4:8080"})
        self.assertEqual(created[0]["task"]["type"], "ReCaptchaV2Task")

    def test_a_malformed_proxy_fails_closed_without_any_http(self):
        hits = []

        def provider(path, payload):
            hits.append(path)
            return {"errorId": 0, "taskId": "t-1"}

        self._serve(provider)
        for unusable in ({"server": "not-a-proxy"}, {"server": "http://no-port"},
                         {}):
            with self.assertRaises(SolverError) as caught:
                solve(sitekey="s", page_url="https://x.test",
                      remaining=lambda: 60_000, proxy=unusable)
            self.assertEqual(caught.exception.kind, "failed")
        self.assertEqual(hits, [])  # never fell back to a proxyless solve

    def test_missing_key_is_unavailable_without_any_http(self):
        captcha_solver.API_KEY = ""
        with self.assertRaises(SolverError) as caught:
            solve(sitekey="s", page_url="https://x.test", remaining=lambda: 60_000)
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_provider_error_fails_without_leaking_the_description(self):
        def provider(path, payload):
            if path == "/createTask":
                return {"errorId": 7, "errorDescription": "private-provider-detail"}
            return {"errorId": 7}

        self._serve(provider)
        with self.assertRaises(SolverError) as caught:
            solve(sitekey="s", page_url="https://x.test", remaining=lambda: 60_000)
        self.assertEqual(caught.exception.kind, "failed")
        self.assertNotIn("private-provider-detail", str(caught.exception))

    def test_deadline_before_the_next_poll_fails_rather_than_stalls(self):
        def provider(path, payload):
            if path == "/createTask":
                return {"errorId": 0, "taskId": "t-1"}
            return {"errorId": 0, "status": "processing"}

        self._serve(provider)
        with self.assertRaises(SolverError) as caught:
            solve(sitekey="s", page_url="https://x.test", remaining=lambda: 400)
        self.assertEqual(caught.exception.kind, "failed")

    def test_ready_without_a_token_is_a_failure(self):
        def provider(path, payload):
            if path == "/createTask":
                return {"errorId": 0, "taskId": "t-1"}
            return {"errorId": 0, "status": "ready", "solution": {}}

        self._serve(provider)
        with self.assertRaises(SolverError) as caught:
            solve(sitekey="s", page_url="https://x.test", remaining=lambda: 60_000)
        self.assertEqual(caught.exception.kind, "failed")


class FormCaptchaTests(unittest.TestCase):
    def setUp(self):
        self._solve = captcha_solver.solve
        self.addCleanup(self._restore)
        self.page = Mock()
        self.page.url = "https://example.test/done"
        self.page.content.return_value = "done"
        self.context = Mock()
        self.context.new_page.return_value = self.page
        self.request = SimpleNamespace(method="POST", url="https://example.test/form",
                                       post_data="email=private-email")
        self.route = Mock(request=self.request)
        self.params = dict(url=self.request.url,
                           fields=[{"selector": "#name", "value": "private-value"}],
                           submit="#submit",
                           success_url=r"^https://example\.test/done$")
        self.page.locator.return_value.click.side_effect = self.submit
        self.page.locator.return_value.bounding_box.return_value = {
            "x": 10, "y": 10, "width": 100, "height": 20}
        self.page.locator.return_value.input_value.return_value = "private-value"

    def _restore(self):
        captcha_solver.solve = self._solve

    def submit(self, **kwargs):
        guard = self.context.route.call_args.args[1]
        guard(self.route)
        self.page.on.call_args.args[1](SimpleNamespace(request=self.request, status=303))

    def test_without_captcha_the_solver_is_never_consulted(self):
        captcha_solver.solve = Mock(side_effect=AssertionError("must not be called"))
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertEqual(result["diagnostics"]["solver_attempts"], 0)
        self.assertIsNone(result["diagnostics"]["solver_status"])
        captcha_solver.solve.assert_not_called()

    def test_unavailable_solver_fails_closed_before_the_click(self):
        captcha_solver.solve = Mock(side_effect=SolverError("unavailable"))
        result = run_form(self.context, **self.params, captcha={"sitekey": "k"})
        self.assertEqual(result["error"], "captcha_solver_unavailable")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["diagnostics"]["solver_status"], "unavailable")
        self.assertEqual(result["diagnostics"]["solver_attempts"], 1)
        self.assertFalse(result["ok"])
        self.page.locator.return_value.click.assert_not_called()

    def test_failed_solver_fails_closed_before_the_click(self):
        captcha_solver.solve = Mock(side_effect=SolverError("failed"))
        result = run_form(self.context, **self.params, captcha={"sitekey": "k"})
        self.assertEqual(result["error"], "captcha_solver_failed")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["diagnostics"]["solver_status"], "failed")
        self.page.locator.return_value.click.assert_not_called()

    def test_captcha_proxy_reaches_the_solver(self):
        seen = []

        def fake_solve(**kwargs):
            seen.append(kwargs)
            return "solved-token"

        captcha_solver.solve = fake_solve
        result = run_form(self.context, **self.params, captcha={"sitekey": "k"},
                          captcha_proxy={"server": "http://1.2.3.4:8080"})
        self.assertTrue(result["ok"])
        self.assertEqual(seen[0]["proxy"], {"server": "http://1.2.3.4:8080"})

    def test_solved_token_reaches_only_the_captcha_response_field(self):
        seen = []

        def fake_solve(**kwargs):
            seen.append(kwargs)
            return "solved-token"

        captcha_solver.solve = fake_solve
        result = run_form(self.context, **self.params,
                          captcha={"sitekey": "k", "action": "signup"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["diagnostics"]["solver_status"], "solved")
        self.assertNotIn("solved-token", str(result))  # never in the result
        self.assertEqual(seen[0]["sitekey"], "k")
        self.assertEqual(seen[0]["action"], "signup")
        self.assertEqual(seen[0]["page_url"], self.page.url)
        self.assertIsNone(seen[0]["proxy"])  # no exit given → proxyless, as before
        selectors = [call.args[0] for call in self.page.locator.call_args_list]
        self.assertIn('[name="g-recaptcha-response"]', selectors)
        injected = [call for call in self.page.locator.return_value.first.evaluate.call_args_list
                    if len(call.args) == 2 and call.args[1] == "solved-token"]
        self.assertEqual(len(injected), 1)
        # A custom captcha_field names both the response field and the POST
        # field — which may be an input, not recaptcha's textarea.
        result = run_form(self.context, **self.params,
                          captcha={"sitekey": "k"}, captcha_field="0-captcha")
        self.assertTrue(result["ok"])
        selectors = [call.args[0] for call in self.page.locator.call_args_list]
        self.assertIn('[name="0-captcha"]', selectors)

    def test_missing_token_field_fails_closed_without_the_click(self):
        captcha_solver.solve = Mock(return_value="solved-token")
        shared = self.page.locator.return_value

        def locator(selector):
            if selector.startswith("[name="):
                missing = Mock()
                missing.first.evaluate.side_effect = RuntimeError("no node")
                return missing
            return shared

        self.page.locator.side_effect = locator
        result = run_form(self.context, **self.params, captcha={"sitekey": "k"})
        self.assertEqual(result["error"], "captcha_field_missing")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["diagnostics"]["solver_status"], "field_missing")
        self.page.locator.return_value.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()

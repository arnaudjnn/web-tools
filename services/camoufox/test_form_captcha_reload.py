"""The reCAPTCHA readiness gate: one pre-input reload, then a zero-POST 503.

Measured on our oracle (cf156, 2026-10-02): 5/33 POSTs carried NO token,
every one with captcha_scripts [2,2,1] and an oracle verdict without
t_submit — the page's submit listener (attached in grecaptcha.ready) never
ran, so the click fell through to a native submit with an empty field. The
counts cannot see it ([2,2,1] has as many responses as requests: a body cut
after its headers is a response AND a failure), so the gate asks the page
whether its client can mint. Reuses test_form_flow.FormTests' fake page
(borrowed methods, so its own tests are not collected twice).
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import form_flow
import test_form_flow as _flow
from form_flow import captcha_path_class, run_form

API_JS = "https://www.google.com/recaptcha/api.js?render=private-sitekey"
LIB_JS = "https://www.gstatic.com/recaptcha/releases/private-release/recaptcha__it.js"
ANCHOR = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=private-sitekey&size=invisible"


class CaptchaGateTests(unittest.TestCase):
    setUp_flow = _flow.FormTests.setUp
    submit = _flow.FormTests.submit

    def setUp(self):
        self.setUp_flow()
        patcher = patch.object(form_flow, "CAPTCHA_READY_WAIT_S", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.page.frames = []
        # No main world in the fake page: evaluate answers only the egress
        # probe; the gate's mw: probe reads False unless a test says so.
        self.main_world = False
        self.page.evaluate.side_effect = self.evaluate

    def evaluate(self, expression, *args, **kwargs):
        if expression.startswith("mw:") and "grecaptcha" in expression:
            return self.main_world
        return None

    def handler(self, event):
        return {c.args[0]: c.args[1] for c in self.page.on.call_args_list}[event]

    def load(self, url, outcome):
        """One captcha script: 'ok', 'cut' (headers then a failed body), 'refused'."""
        request = SimpleNamespace(method="GET", url=url, resource_type="script",
                                  failure="NS_ERROR_NET_INTERRUPT" if outcome == "cut"
                                  else "NS_ERROR_CONNECTION_REFUSED")
        self.handler("request")(request)
        if outcome in ("ok", "cut"):
            self.handler("response")(SimpleNamespace(request=request, url=url, status=200))
        if outcome == "ok":
            self.handler("requestfinished")(request)
        else:
            self.handler("requestfailed")(request)

    def run_with(self, loads, **overrides):
        """`loads`: one entry per goto — a (api, lib) outcome pair, or None."""
        sequence = iter(loads)

        def goto(*args, **kwargs):
            step = next(sequence)
            if step is not None:
                api, lib = step
                self.load(API_JS, api)
                if lib is not None:
                    self.load(LIB_JS, lib)
            return SimpleNamespace(status=200)

        self.page.goto.side_effect = goto
        return run_form(self.context, **{**self.params, **overrides})

    # -- the observed failure ------------------------------------------

    def test_library_cut_after_headers_reloads_once_then_submits(self):
        # [2,2,1]: api.js fine, recaptcha__*.js headers then a cut body.
        result = self.run_with([("ok", "cut"), ("ok", "ok")])
        d = result["diagnostics"]
        self.assertEqual([d["captcha_script_requests"], d["captcha_script_responses"],
                          d["captcha_network_failures"]], [4, 4, 1])
        self.assertEqual(d["captcha_failed"], [{"path": "recaptcha__*.js", "type": "script",
                                                "code": "NS_ERROR_NET_INTERRUPT",
                                                "after_response": True}])
        self.assertTrue(d["captcha_script_reload"])
        self.assertTrue(d["captcha_ready"])
        self.assertEqual(d["captcha_signal"], "lib")
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(result["form_submissions"], 1)  # the one POST, after the reload
        self.assertNotIn("private", json.dumps(d))

    def test_still_unusable_after_reload_is_a_zero_post_captcha_unavailable(self):
        result = self.run_with([("ok", "cut"), ("ok", "cut")])
        self.assertEqual(result["error"], "captcha_unavailable")
        self.assertEqual(result["form_submissions"], 0)
        self.assertFalse(result["diagnostics"]["submit_click_attempted"])
        self.assertTrue(result["diagnostics"]["captcha_script_reload_failed"])
        self.assertEqual(self.page.goto.call_count, 2)  # never a third
        self.page.keyboard.type.assert_not_called()     # no field was touched
        self.page.mouse.wheel.assert_not_called()       # not even the arrival
        self.route.continue_.assert_not_called()

    def test_api_js_refused_is_unusable_too(self):
        # [1,0,1] (cf156 10:30/10:33): no grecaptcha at all.
        result = self.run_with([("refused", None), ("ok", "ok")])
        self.assertEqual(result["diagnostics"]["captcha_failed"][0]["path"], "api.js")
        self.assertFalse(result["diagnostics"]["captcha_failed"][0]["after_response"])
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(result["form_submissions"], 1)

    # -- usable pages are never reloaded ---------------------------------

    def test_loaded_library_never_reloads(self):
        result = self.run_with([("ok", "ok")])
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertIsNone(result["diagnostics"].get("captcha_script_reload"))
        self.assertTrue(result["diagnostics"]["captcha_ready"])

    def test_main_world_execute_is_a_usable_client(self):
        # The library may arrive from cache with no network event; the main
        # world still sees grecaptcha.execute.
        self.main_world = True
        result = self.run_with([("ok", None)])
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertEqual(result["diagnostics"]["captcha_signal"], "execute")
        probes = [c.args[0] for c in self.page.evaluate.call_args_list if "grecaptcha" in c.args[0]]
        self.assertTrue(probes and all(p.startswith("mw:(") for p in probes))

    def test_a_loaded_anchor_frame_is_a_usable_client(self):
        anchor = Mock(url=ANCHOR)
        anchor.evaluate.return_value = True
        self.page.frames = [Mock(url="https://example.test/form"), anchor]
        result = self.run_with([("ok", None)])
        self.assertEqual(result["diagnostics"]["captcha_signal"], "anchor")
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertIn("recaptcha-token", anchor.evaluate.call_args.args[0])

    def test_an_empty_anchor_frame_beats_a_loaded_library(self):
        # The anchor is what execute() talks to: a frame that never loaded
        # cannot mint, whatever the library says.
        anchor = Mock(url=ANCHOR)
        anchor.evaluate.return_value = False
        self.page.frames = [anchor]
        result = self.run_with([("ok", "ok"), ("ok", "ok")])
        self.assertEqual(result["error"], "captcha_unavailable")
        self.assertEqual(result["diagnostics"]["captcha_signal"], "anchor_empty")
        self.assertEqual(result["form_submissions"], 0)

    def test_pages_without_recaptcha_are_never_gated(self):
        result = self.run_with([None])
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertIsNone(result["diagnostics"]["captcha_ready"])
        self.assertFalse(any("grecaptcha" in c.args[0] for c in self.page.evaluate.call_args_list))
        self.assertEqual(result["form_submissions"], 1)

    def test_inspect_only_never_gates_or_reloads(self):
        result = self.run_with([("ok", "cut")], inspect_only=True)
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertIsNone(result["diagnostics"]["captcha_ready"])

    def test_gate_calls_are_marked_pre_post(self):
        live = form_flow.FormLive()
        seen = []
        original = live.mark
        live.mark = lambda name, stuck_s=form_flow.PRE_SUBMIT_STUCK_S: (
            seen.append(name), original(name, stuck_s))
        self.params["live"] = live
        self.run_with([("ok", "cut"), ("ok", "ok")])
        for name in ("captcha check", "captcha reload", "captcha reload wait"):
            self.assertIn(name, seen)

    def test_summary_names_the_failing_piece_without_queries(self):
        live = form_flow.FormLive()
        self.params["live"] = live
        result = self.run_with([("ok", "cut"), ("ok", "ok")])
        summary = live.summary(result)
        self.assertEqual(summary["captcha_failed"][0]["path"], "recaptcha__*.js")
        self.assertTrue(summary["captcha_reload"])
        self.assertTrue(summary["captcha_ready"])
        self.assertNotIn("private", json.dumps(summary))


class CaptchaPathClassTests(unittest.TestCase):
    def test_classes_are_path_only(self):
        cases = {
            API_JS: "api.js",
            "https://www.google.com/recaptcha/enterprise.js?render=k": "enterprise.js",
            LIB_JS: "recaptcha__*.js",
            "https://www.gstatic.com/recaptcha/releases/v/styles__ltr.css": "styles__*.css",
            ANCHOR: "anchor",
            "https://www.google.com/recaptcha/enterprise/anchor?k=x": "anchor",
            "https://www.google.com/recaptcha/api2/reload?k=private": "reload",
            "https://www.google.com/recaptcha/api2/bframe?k=x": "bframe",
            "https://www.google.com/recaptcha/api2/clr?k=x": "clr",
            "https://www.google.com/recaptcha/api2/webworker.js?hl=it": "webworker.js",
            "https://www.google.com/recaptcha/api2/logo_48.png": "other",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(captcha_path_class(url), expected)

    def test_failure_codes_are_tokens_never_messages(self):
        self.assertEqual(form_flow._failure_code("NS_ERROR_NET_INTERRUPT"), "NS_ERROR_NET_INTERRUPT")
        self.assertEqual(form_flow._failure_code("net::ERR_ABORTED at https://x/?k=private"),
                         "net::ERR_ABORTED")
        self.assertEqual(form_flow._failure_code("something with https://private.example/"), "other")
        self.assertEqual(form_flow._failure_code(None), "unknown")


if __name__ == "__main__":
    unittest.main()

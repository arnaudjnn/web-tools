"""A CAPTCHA script that failed to load gets ONE pre-input reload.

Measured on our oracle 2026-10-02: every no-token run had a failed api.js
request (captcha_scripts [1,0,1]) — no grecaptcha, no submit listener, a
native submit with an empty token. Reuses test_form_flow.FormTests' fake
page (borrowed methods, so its own tests are not collected twice).
"""
import unittest
from types import SimpleNamespace

import test_form_flow as _flow
from form_flow import run_form

API_JS = "https://www.google.com/recaptcha/api.js?render=k"


class CaptchaScriptReloadTests(unittest.TestCase):
    setUp = _flow.FormTests.setUp
    submit = _flow.FormTests.submit

    def handler(self, event):
        return {c.args[0]: c.args[1] for c in self.page.on.call_args_list}[event]

    def script_load(self, fail):
        request = SimpleNamespace(method="GET", url=API_JS, resource_type="script")
        self.handler("request")(request)
        if fail:
            self.handler("requestfailed")(request)
        else:
            self.handler("response")(SimpleNamespace(request=request, url=API_JS, status=200))

    def run_with(self, outcomes):
        loads = iter(outcomes)

        def goto(*args, **kwargs):
            self.script_load(next(loads))
            return SimpleNamespace(status=200)

        self.page.goto.side_effect = goto
        return run_form(self.context, **self.params)

    def test_failed_script_reloads_once_then_submits(self):
        result = self.run_with([True, False])
        self.assertEqual(self.page.goto.call_count, 2)
        d = result["diagnostics"]
        self.assertTrue(d["captcha_script_reload"])
        self.assertFalse(d["captcha_script_reload_failed"])
        self.assertEqual(result["form_submissions"], 1)  # the one POST, after the reload

    def test_loaded_script_never_reloads(self):
        result = self.run_with([False])
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertIsNone(result["diagnostics"].get("captcha_script_reload"))

    def test_second_failure_proceeds_and_says_so(self):
        result = self.run_with([True, True])
        self.assertEqual(self.page.goto.call_count, 2)  # never a third
        self.assertTrue(result["diagnostics"]["captcha_script_reload_failed"])

    def test_inspect_only_never_reloads(self):
        self.params["inspect_only"] = True
        self.run_with([True])
        self.assertEqual(self.page.goto.call_count, 1)


if __name__ == "__main__":
    unittest.main()

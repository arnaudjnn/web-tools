"""Gate re-verify forensics: did the gate POST carry a NEW token or step0's?

Reuses test_form_flow.WizardTests' fake page/clock (borrowed methods, not
inheritance, so its tests do not run twice). Only booleans and counts come
out — never a token or its digest.
"""
import unittest
from types import SimpleNamespace

import test_form_flow as _flow  # module import: its classes must not be re-collected here

RELOAD = "https://www.google.com/recaptcha/api2/reload?k=site"


class GateReverifyForensicsTests(unittest.TestCase):
    setUp = _flow.WizardTests.setUp
    guard = _flow.WizardTests.guard
    respond = _flow.WizardTests.respond
    post_step0 = _flow.WizardTests.post_step0
    fire_post = _flow.WizardTests.fire_post
    run_wizard = _flow.WizardTests.run_wizard
    gate_warning = _flow.WizardTests.gate_warning

    def reload(self):
        request = SimpleNamespace(method="POST", url=RELOAD, resource_type="xhr")
        self.page.on.call_args.args[1](SimpleNamespace(request=request, url=RELOAD, status=200))

    def post(self, body, mint_first):
        if mint_first:
            self.reload()
        self.clock[0] += 5.0
        self.request.post_data = body
        self.guard()(self.route)
        self.respond()

    def wire(self, gate_body, gate_mints):
        self.params["captcha_field"] = "0-captcha"  # Atoka's formtools-prefixed field
        self.locator("#submit").click.side_effect = lambda **kw: self.post(
            "0-captcha=step0-token-AAAA&0-email=x", True)

        def mouse_click(*args, **kwargs):
            self.clicks += 1
            if self.clicks == 2:  # the gate's human click
                self.post(gate_body, gate_mints)

        self.page.mouse.click.side_effect = mouse_click
        self.locator("button, a").is_visible.return_value = True
        self.locator("#id_1-company_name").is_visible.return_value = False

    def gate_then_reject(self):
        return self.run_wizard(
            [self.gate_warning(), self.gate_warning(),
             {"url": "https://example.test/try", "step0": True, "step2": False,
              "errs": ["Error verifying reCAPTCHA, please try again."], "text": "x"}])

    def test_fresh_mint_on_the_gate_click(self):
        self.wire("0-captcha=gate-token-BBBBBB&0-confirm=1", gate_mints=True)
        result = self.gate_then_reject()
        first, gate = result["diagnostics"]["submission_tokens"]
        self.assertIsNone(first["same_as_first"])
        self.assertIs(gate["same_as_first"], False)
        self.assertEqual(gate["reloads"] - first["reloads"], 1)
        self.assertEqual(result["diagnostics"]["rejected_after_posts"], 2)
        self.assertEqual(result["error"], "wizard_rejected")

    def test_stale_resend_of_the_step0_token(self):
        self.wire("0-captcha=step0-token-AAAA&0-confirm=1", gate_mints=False)
        result = self.gate_then_reject()
        first, gate = result["diagnostics"]["submission_tokens"]
        self.assertIs(gate["same_as_first"], True)
        self.assertEqual(gate["reloads"], first["reloads"])

    def test_tokenless_post_is_unknown_not_stale(self):
        self.wire("0-confirm=1", gate_mints=False)
        result = self.gate_then_reject()
        gate = result["diagnostics"]["submission_tokens"][1]
        self.assertIs(gate["token"], False)
        self.assertIsNone(gate["same_as_first"])

    def test_the_form_run_summary_carries_the_answer(self):
        live = _flow.form_flow.FormLive()
        self.params["live"] = live
        self.wire("0-captcha=step0-token-AAAA&0-confirm=1", gate_mints=False)
        summary = live.summary(self.gate_then_reject())
        self.assertEqual([p["same_as_first"] for p in summary["posts"]], [None, True])
        self.assertEqual(summary["rejected_after_posts"], 2)
        self.assertNotIn("step0-token-AAAA", repr(summary))

    def test_no_token_material_in_the_result(self):
        self.wire("0-captcha=gate-token-BBBBBB&0-confirm=1", gate_mints=True)
        text = repr(self.gate_then_reject())
        for secret in ("step0-token-AAAA", "gate-token-BBBBBB"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()

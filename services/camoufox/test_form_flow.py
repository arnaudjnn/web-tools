import ast
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from form_flow import run_form, validate_form


class FormTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.page.url = "https://example.test/done"
        self.page.content.return_value = "done"
        self.context = Mock()
        self.context.new_page.return_value = self.page
        self.request = SimpleNamespace(method="POST", url="https://example.test/form")
        self.route = Mock(request=self.request)
        self.params = dict(url=self.request.url, fields=[{"selector": "#name", "value": "private-value"}],
                           submit="#submit", success_url=r"^https://example\.test/done$")
        self.page.locator.return_value.click.side_effect = self.submit
        # Human input reads geometry before moving the pointer, and reads back
        # what it typed (a miss fails loud instead of submitting empty).
        self.page.locator.return_value.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 20}
        self.page.locator.return_value.input_value.return_value = "private-value"

    def submit(self, **kwargs):
        guard = self.context.route.call_args.args[1]
        guard(self.route)
        guard(self.route)  # A second page handler tries the same POST.
        self.page.on.call_args.args[1](SimpleNamespace(request=self.request, status=303))

    def test_one_post_and_actual_response_status(self):
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 303)
        self.assertEqual(result["form_submissions"], 1)
        self.route.continue_.assert_called_once()
        self.route.abort.assert_called_once_with("blockedbyclient")

    def test_required_field_failure_never_clicks_submit(self):
        self.page.keyboard.type.side_effect = RuntimeError("private-value")
        result = run_form(self.context, **self.params)
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "fields_failed")
        # Class name yes, message no — the kind of failure is safe to log,
        # whatever the exception text carried.
        self.assertEqual(result["diagnostics"]["failure_class"], "RuntimeError")
        self.assertNotIn("private-value", str(result))
        self.page.locator.return_value.click.assert_not_called()

    def test_lost_response_does_not_erase_attempt_or_retry(self):
        def crash(**kwargs):
            self.context.route.call_args.args[1](self.route)
            raise RuntimeError("Target closed")
        self.page.locator.return_value.click.side_effect = crash
        result = run_form(self.context, **self.params)
        self.assertEqual(result["form_submissions"], 1)
        self.assertEqual(result["error"], "outcome_unknown")
        self.assertFalse(result["ok"])
        self.page.locator.return_value.click.assert_called_once()

    def test_success_url_without_post_is_not_acceptance(self):
        self.page.locator.return_value.click.side_effect = None
        result = run_form(self.context, **self.params)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_submission")

    def test_rejection_retains_html_and_is_not_success(self):
        self.page.url = self.request.url
        self.page.content.return_value = "field error"
        result = run_form(self.context, **self.params)
        self.assertFalse(result["ok"])
        self.assertEqual(result["html"], "field error")

    def test_validation_precedes_browser_work(self):
        for target in ["https://other.test/form", "http://example.test/form"]:
            with self.assertRaises(ValueError):
                validate_form(self.request.url, [target], None)

    def test_token_presence_without_disclosing_token(self):
        self.request.post_data = "0-captcha=private-token&email=private-email"
        result = run_form(self.context, **self.params, captcha_field="0-captcha")
        self.assertTrue(result["diagnostics"]["token_present"])
        self.assertNotIn("private-token", str(result))
        self.assertNotIn("private-email", str(result))

    def test_missing_token_is_distinct_from_unknown(self):
        self.request.post_data = "email=private-email"
        result = run_form(self.context, **self.params, captcha_field="0-captcha")
        self.assertIs(result["diagnostics"]["token_present"], False)

    def test_required_missing_token_blocks_every_attempt_without_sending(self):
        self.request.post_data = "email=private-email"
        result = run_form(self.context, **self.params, captcha_field="0-captcha", require_captcha_token=True)
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "captcha_token_missing")
        self.assertTrue(result["diagnostics"]["captcha_guard_blocked"])
        self.route.continue_.assert_not_called()
        self.assertEqual(self.route.abort.call_count, 2)

    def test_present_required_token_can_be_submitted_once(self):
        self.request.post_data = "0-captcha=fixture-only-token"
        result = run_form(self.context, **self.params, captcha_field="0-captcha", require_captcha_token=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 1)
        self.route.continue_.assert_called_once()

    def test_placeholder_or_ambiguous_tokens_fail_closed(self):
        for body in ["0-captcha=undefined", "0-captcha=null", "0-captcha=false",
                     "0-captcha=one&0-captcha=two"]:
            with self.subTest(body=body):
                self.request.post_data = body
                result = run_form(self.context, **self.params, captcha_field="0-captcha", require_captcha_token=True)
                self.assertEqual(result["form_submissions"], 0)
                self.assertEqual(result["error"], "captcha_token_missing")
        self.route.continue_.assert_not_called()

    def test_waits_for_main_world_readiness_before_click(self):
        self.page.evaluate.side_effect = [False, False, True]
        result = run_form(self.context, **self.params, ready_expression="window.formReady === true")
        self.assertTrue(result["diagnostics"]["ready_condition_met"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.page.evaluate.call_count, 3)
        self.page.evaluate.assert_called_with("mw:(window.formReady === true)")

    def test_readiness_failure_never_clicks(self):
        self.page.evaluate.side_effect = RuntimeError("page is gone")
        result = run_form(self.context, **self.params, ready_expression="window.formReady === true")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "readiness_failed")
        self.page.locator.return_value.click.assert_not_called()

    def test_inspection_never_fills_clicks_or_allows_same_origin_mutation(self):
        self.page.goto.side_effect = lambda *args, **kwargs: self.context.route.call_args.args[1](self.route)
        result = run_form(self.context, **self.params, inspect_only=True)
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["diagnostics"]["blocked_mutations"], 1)
        self.assertIsNone(result["diagnostics"]["token_present"])
        self.assertFalse(result["diagnostics"]["submit_click_attempted"])
        self.assertEqual(result["html"], "")
        self.page.locator.assert_not_called()
        self.route.continue_.assert_not_called()

    def test_captcha_network_and_script_errors_are_counts_not_payloads(self):
        def navigate(*args, **kwargs):
            events = {call.args[0]: call.args[1] for call in self.page.on.call_args_list}
            req = SimpleNamespace(url="https://www.google.com/recaptcha/api.js?secret=private", resource_type="script", method="GET")
            events["request"](req)
            events["response"](SimpleNamespace(request=req, status=403))
            events["requestfailed"](req)
            events["pageerror"](RuntimeError("private exception"))
        self.page.goto.side_effect = navigate
        result = run_form(self.context, **self.params, inspect_only=True)
        d = result["diagnostics"]
        self.assertEqual(d["captcha_script_requests"], 1)
        self.assertEqual(d["captcha_script_responses"], 1)
        self.assertEqual(d["captcha_script_http_errors"], [403])
        self.assertEqual(d["captcha_network_failures"], 1)
        self.assertEqual(d["page_script_errors"], 1)
        self.assertNotIn("private", str(result))

    def test_forms_do_not_use_read_retry_wrapper(self):
        tree = ast.parse(pathlib.Path(__file__).with_name("app.py").read_text())
        handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "form_submit")
        self.assertNotIn("_run_render", ast.unparse(handler))

    def test_forms_are_independent_of_shared_browser_recycling(self):
        tree = ast.parse(pathlib.Path(__file__).with_name("app.py").read_text())
        handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "form_submit")
        recycle = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "recycle")
        self.assertIn("_form_worker.run", ast.unparse(handler))
        self.assertNotIn("_render_executor", ast.unparse(handler))
        self.assertNotIn("_ensure_render_browser", ast.unparse(handler))
        self.assertNotIn("_form_worker", ast.unparse(recycle))

    def test_text_fields_are_typed_not_filled(self):
        # fill() sets the value in one assignment — no pointer, no keystrokes,
        # no dwell — which reCAPTCHA v3 scores as automation (measured 0 passes
        # 2026-09-26 across headless and headed fleets). Text entry must stay
        # keystroke-by-keystroke with trusted events.
        src = pathlib.Path(__file__).with_name("form_flow.py").read_text()
        self.assertNotIn(".fill(", src)
        self.assertIn("keyboard.type", src)

    def test_typing_into_the_void_fails_loud(self):
        # Empty fields trip HTML5 validation, which blocks the submit with no
        # POST and no error — so a miss must surface as fields_failed here,
        # not as a silent no_submission downstream.
        self.page.locator.return_value.input_value.return_value = ""
        result = run_form(self.context, **self.params)
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "fields_failed")
        # ValueError is the typed-text check; a click that never got to type
        # reports TimeoutError instead — the two fields_failed causes.
        self.assertEqual(result["diagnostics"]["failure_class"], "ValueError")

    def test_dismiss_clicks_record_and_banner_state(self):
        params = dict(self.params, dismiss=["button.accept-cookies"])
        self.page.locator.return_value.first.is_visible.return_value = False
        result = run_form(self.context, **params)
        self.assertTrue(result["ok"])
        d = result["diagnostics"]
        self.assertEqual(d["dismiss_clicked"], ["button.accept-cookies"])
        self.assertFalse(d["banner_visible"])

    def test_failed_dismiss_leaves_banner_visible_flagged(self):
        params = dict(self.params, dismiss=["button.accept-cookies"])
        # The dismiss click times out (banner not up yet); the banner itself
        # still reports visible right before the fields phase.
        self.page.locator.return_value.first.click.side_effect = TimeoutError("banner")
        self.page.locator.return_value.first.is_visible.return_value = True
        result = run_form(self.context, **params)
        self.assertTrue(result["ok"])
        d = result["diagnostics"]
        self.assertEqual(d["dismiss_clicked"], [])
        self.assertTrue(d["banner_visible"])


if __name__ == "__main__":
    unittest.main()

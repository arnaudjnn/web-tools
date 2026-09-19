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
        self.page.locator.return_value.fill.side_effect = RuntimeError("private-value")
        result = run_form(self.context, **self.params)
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "fields_failed")
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

    def test_expired_queued_form_never_launches_browser(self):
        tree = ast.parse(pathlib.Path(__file__).with_name("app.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_do_form_submit")
        launch = Mock()
        namespace = {"validate_form": validate_form, "time": SimpleNamespace(monotonic=lambda: 10),
                     "_ensure_render_browser": launch}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), namespace)
        with self.assertRaises(TimeoutError):
            namespace["_do_form_submit"](self.request.url, [], "button", [], None,
                                         "load", 0, 1000, 1000, expires_at=9)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

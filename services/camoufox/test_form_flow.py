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

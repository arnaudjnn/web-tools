import ast
import json
import pathlib
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import form_flow
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

    def test_captcha_field_lengths_expose_shape_not_values(self):
        # Which captcha field the server could be validating: the custom
        # input (minted) vs the standard response textarea (possibly carried
        # empty). Lengths only — the token never leaves the wire.
        self.request.post_data = "0-captcha=private-token&g-recaptcha-response="
        result = run_form(self.context, **self.params, captcha_field="0-captcha")
        self.assertEqual(result["diagnostics"]["captcha_field_lengths"],
                         {"0-captcha": [13], "g-recaptcha-response": [0]})
        self.assertNotIn("private-token", str(result))

    def test_egress_records_country_and_isp_of_the_real_exit(self):
        # The proxy contract is only checkable from inside the page: which
        # IP actually minted the token. Shape only, never a body.
        self.page.evaluate.side_effect = lambda expression, *args, **kwargs: (
            {"success": True, "country": "Italy",
             "connection": {"isp": "Vodafone Italia", "asn": 30722}}
            if "ipwho.is" in expression else True)
        result = run_form(self.context, **self.params)
        self.assertEqual(result["diagnostics"]["egress"],
                         {"country": "Italy", "isp": "Vodafone Italia", "asn": 30722})

    def test_egress_failure_stays_absent_and_does_not_fail_the_run(self):
        self.page.evaluate.side_effect = RuntimeError("network down")
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["diagnostics"]["egress"])

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
        self.assertEqual(set(result["diagnostics"]["field_state"]), {"visible", "enabled"})

    def test_type_retries_the_click_when_focus_never_landed(self):
        # Focus missing after the geometric click = the keystrokes would land
        # elsewhere; one actionability-aware click before typing.
        self.page.locator.return_value.is_focused.return_value = False
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        # the fallback click (focus miss) + the single submit click
        self.assertEqual(self.page.locator.return_value.click.call_count, 2)

    def test_type_does_not_double_click_when_focus_landed(self):
        self.page.locator.return_value.is_focused.return_value = True
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertEqual(self.page.locator.return_value.click.call_count, 1)

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

    def test_dismiss_retries_when_banner_survives_the_first_click(self):
        # A click that does not clear the overlay (render race) must be
        # retried — check-once sent the fields into a covered form (the
        # 2026-09-29 failure: banner_visible=True, first field timed out).
        params = dict(self.params, dismiss=["button.accept-cookies"])
        self.page.locator.return_value.first.is_visible.side_effect = [True, False]
        result = run_form(self.context, **params)
        self.assertTrue(result["ok"])
        d = result["diagnostics"]
        self.assertEqual(d["dismiss_clicked"],
                         ["button.accept-cookies", "button.accept-cookies"])
        self.assertFalse(d["banner_visible"])

    def test_dismiss_catches_a_banner_that_arrives_after_the_click_window(self):
        # Slow third-party load: nothing to click in round one, the banner
        # is visible at the check — round two must find and click it (the
        # 2026-09-30 failure: dismiss_clicked=[] yet banner_visible=True).
        params = dict(self.params, dismiss=["button.accept-cookies"])
        first = self.page.locator.return_value.first
        first.wait_for.side_effect = [TimeoutError("not yet"), None]
        first.is_visible.side_effect = [True, False]
        result = run_form(self.context, **params)
        self.assertTrue(result["ok"])
        d = result["diagnostics"]
        self.assertEqual(d["dismiss_clicked"], ["button.accept-cookies"])
        self.assertFalse(d["banner_visible"])

    # -- one humanized move per click ------------------------------------

    def test_each_click_is_one_pointer_move_not_a_stepped_approach(self):
        # humanize=True already draws the trajectory; the manual 6-18 step
        # approach was double humanization and 6-18x the wedge surface.
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertEqual(self.page.mouse.move.call_count, 1)  # one field, one move
        self.assertEqual(self.page.mouse.click.call_count, 1)
        x, y = self.page.mouse.move.call_args.args
        self.assertEqual((x, y), self.page.mouse.click.call_args.args)
        # inside the control's box (10..110 x 10..30), off-centre allowed
        self.assertTrue(10 <= x <= 110 and 10 <= y <= 30)

    def test_click_target_never_lands_on_a_viewport_axis(self):
        # daijro/camoufox#751: a trajectory point on x==0/y==0 deadlocks
        # the input chain. A control flush with the corner must still be
        # aimed strictly inside the viewport.
        for _ in range(200):
            x, y = form_flow._click_target({"x": 0, "y": 0, "width": 4, "height": 2})
            self.assertGreaterEqual(x, 2.0)
            self.assertGreaterEqual(y, 2.0)

    def test_pointer_marks_are_split_so_a_park_names_its_call(self):
        live = form_flow.FormLive()
        seen = []
        original = live.mark

        def recording(name, stuck_s=form_flow.PRE_SUBMIT_STUCK_S):
            seen.append((name, stuck_s))
            original(name, stuck_s)

        live.mark = recording
        run_form(self.context, **self.params, live=live)
        names = [name for name, _ in seen]
        self.assertIn("pointer move", names)
        self.assertIn("pointer click", names)
        self.assertEqual(dict(seen)["pointer move"], form_flow.POINTER_MOVE_STUCK_S)

    # -- navigation retry --------------------------------------------------

    @patch("form_flow.time.sleep")
    def test_transient_navigation_refusal_is_retried_in_run(self, sleep):
        # 2026-10-01: 18 navigation_failed (class Error) failed ~70ms after
        # the page opened and the same identity navigated fine seconds
        # later. Navigation is a GET before any input: retry it here.
        self.page.goto.side_effect = [RuntimeError("Page.goto: NS_ERROR_CONNECTION_REFUSED"), Mock()]
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(result["diagnostics"]["nav_attempts"], 2)
        self.assertEqual(result["diagnostics"]["nav_error"], "NS_ERROR_CONNECTION_REFUSED")
        sleep.assert_called_once()

    @patch("form_flow.time.sleep")
    def test_a_page_closed_on_arrival_is_replaced(self, _sleep):
        class TargetClosedError(Exception):
            pass
        self.page.goto.side_effect = [
            TargetClosedError("Target page, context or browser has been closed"), Mock()]
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertEqual(self.context.new_page.call_count, 2)
        self.assertEqual(result["diagnostics"]["nav_error"], "target_closed")

    @patch("form_flow.time.sleep")
    def test_navigation_retries_are_bounded_and_stay_zero_post(self, _sleep):
        self.page.goto.side_effect = RuntimeError(
            "Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED at https://example.test/private")
        result = run_form(self.context, **self.params)
        self.assertEqual(result["error"], "navigation_failed")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(self.page.goto.call_count, form_flow.NAV_ATTEMPTS)
        self.assertFalse(result["diagnostics"]["submit_click_attempted"])
        self.assertNotIn("private", str(result))

    @patch("form_flow.time.sleep")
    def test_unknown_navigation_errors_are_not_retried(self, sleep):
        self.page.goto.side_effect = RuntimeError("Page.goto: Invalid url")
        result = run_form(self.context, **self.params)
        self.assertEqual(result["error"], "navigation_failed")
        self.assertEqual(self.page.goto.call_count, 1)
        sleep.assert_not_called()

    # -- readiness bound ---------------------------------------------------

    def test_readiness_is_bounded_and_never_clicks(self):
        # 2026-10-01: every readiness_failed polled until the whole deadline
        # (~2 min). A condition false for READY_WAIT_S fails fast instead.
        self.page.evaluate.return_value = False
        with patch.object(form_flow, "READY_WAIT_S", 0.05):
            result = run_form(self.context, **self.params,
                              ready_expression="window.formReady === true", timeout_ms=60000)
        self.assertEqual(result["error"], "readiness_failed")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["diagnostics"]["failure_class"], "TimeoutError")
        self.page.locator.return_value.click.assert_not_called()

    # -- the done line and the summary on every path ---------------------

    def run_logged(self, **overrides):
        with self.assertLogs("camoufox.forms", level="INFO") as logs:
            result = run_form(self.context, **{**self.params, **overrides})
        return result, [r.getMessage() for r in logs.records]

    def test_inspect_only_logs_the_done_line_and_one_summary(self):
        _result, messages = self.run_logged(inspect_only=True)
        self.assertEqual(sum(m.startswith("form flow: done") for m in messages), 1)
        self.assertEqual(sum(m.startswith("form-run ") for m in messages), 1)

    def test_summary_never_carries_values_tokens_or_bodies(self):
        self.request.post_data = "0-captcha=private-token&email=private-email"
        result, messages = self.run_logged(captcha_field="0-captcha")
        self.assertTrue(result["ok"])
        line = next(m for m in messages if m.startswith("form-run "))
        summary = json.loads(line[len("form-run "):])
        self.assertEqual(summary["posts"], [{"n": 0, "token": True, "mint_age_s": None}])
        self.assertTrue(summary["token_present"])
        joined = "\n".join(messages)
        for secret in ("private-value", "private-token", "private-email"):
            self.assertNotIn(secret, joined)

    def test_first_post_forensics_agree_with_the_per_post_record(self):
        # Parsed once: token_present / captcha_field_lengths (first POST)
        # and submission_tokens[0] come from the same shape.
        self.request.post_data = "0-captcha=private-token&g-recaptcha-response="
        result = run_form(self.context, **self.params, captcha_field="0-captcha")
        d = result["diagnostics"]
        self.assertEqual(d["captcha_field_lengths"], d["submission_tokens"][0]["lengths"])
        self.assertTrue(d["token_present"])
        self.assertTrue(d["submission_tokens"][0]["token"])


class WizardTests(unittest.TestCase):
    """atoka's wizard: step0 -> business-email gate -> step2 -> completion.

    step0's answer is one of four shapes and only the URL sometimes
    distinguishes them: the next step, a gate needing one click, a
    rejection rendered as errors ON the form, or completion whose body
    copy is the only signal (manual review stays on the same URL). The
    guard allows exactly the wizard's own three POSTs, seconds apart —
    a same-click double-fire or a fourth aborts. The clock is fake so
    those gaps are deterministic.
    """

    def setUp(self):
        self.clock = [1_000.0]
        patcher = patch.object(form_flow.time, "monotonic", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.locs = {}
        self.clicks = 0

        class Loc(Mock):
            @property
            def first(self):
                return self

        def locator(selector, **kwargs):
            loc = self.locs.get(selector)
            if loc is None:
                loc = self.locs[selector] = Loc()
                loc.bounding_box.return_value = {"x": 10, "y": 10,
                                                 "width": 100, "height": 20}
            return loc

        self.locator = locator

        def mouse_click(*args, **kwargs):
            # human_click ends in a pointer click; the wizard's own POSTs
            # hang off clicks 2 (gate) and 5 (step2 submit).
            self.clicks += 1
            if self.clicks == 2:
                self.fire_post()
            elif self.clicks == 5:
                self.fire_post(double=True)

        self.page = Mock()
        self.page.url = "https://example.test/try"
        self.page.content.return_value = "<html>wizard</html>"
        self.page.locator.side_effect = locator
        self.page.mouse.click.side_effect = mouse_click
        self.context = Mock()
        self.context.new_page.return_value = self.page
        self.request = SimpleNamespace(method="POST", url="https://example.test/try")
        self.route = Mock(request=self.request)
        self.locator("#name").input_value.return_value = "private-value"
        self.locator("#id_1-company_name").input_value.return_value = "Verdi Consulenza"
        self.locator("#id_1-tos").is_checked.return_value = True
        self.locator("#submit").click.side_effect = self.post_step0
        self.params = dict(
            url="https://example.test/try",
            fields=[{"selector": "#name", "value": "private-value"}],
            submit="#submit",
            success_url=r"^https://example\.test/complete$",
            gate_text="To complete the registration click here",
            step2=[{"selector": "#id_1-company_name", "value": "Verdi Consulenza"},
                   {"selector": "#id_1-tos", "action": "check"}],
            completion_markers=[r"And now, what happens\?"],
            timeout_ms=120_000,
        )

    def guard(self):
        return self.context.route.call_args.args[1]

    def respond(self, status=200):
        self.page.on.call_args.args[1](
            SimpleNamespace(request=self.request, status=status))

    def post_step0(self, **kwargs):
        self.clock[0] += 5.0
        guard = self.guard()
        guard(self.route)
        guard(self.route)  # A second page handler tries the same POST.
        self.respond()

    def fire_post(self, double=False):
        self.clock[0] += 5.0
        guard = self.guard()
        guard(self.route)
        if double:
            guard(self.route)
        self.respond()

    def run_wizard(self, states, final_text="", race_once=False):
        ticks = list(states)
        raced = {"done": False}

        def evaluate(expression, *args, **kwargs):
            if expression.startswith("fetch("):
                return None  # egress probe: absent, as when it fails
            if expression == "document.body.innerText":
                return final_text
            if race_once and not raced["done"]:
                raced["done"] = True
                raise RuntimeError("Execution context was destroyed")
            self.clock[0] += 2.0  # every poll tick costs two fake seconds
            if ticks:
                return ticks.pop(0)
            return {"url": "https://example.test/try", "step0": True,
                    "step2": False, "errs": [], "text": "wizard form"}

        self.page.evaluate.side_effect = evaluate
        return run_form(self.context, **self.params)

    def gate_warning(self, **over):
        state = {"url": "https://example.test/try", "step0": True, "step2": False,
                 "errs": ["Please use a business email address"],
                 "text": "To complete the registration click here"}
        state.update(over)
        return state

    def test_wizard_completes_on_body_copy_after_three_posts(self):
        # Manual review: same URL, no form, the body copy is the ONLY signal.
        self.locator("button, a").is_visible.return_value = True
        self.locator("#id_1-company_name").is_visible.return_value = True
        result = self.run_wizard(
            [self.gate_warning(),
             {"url": "https://example.test/try", "step0": True, "step2": True,
              "errs": [], "text": "company step"},
             {"url": "https://example.test/try", "step0": False, "step2": False,
              "errs": [],
              "text": "And now, what happens? you will receive a notification"}],
            final_text="And now, what happens? you will receive a notification at"
                       " the email address you provided")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["form_submissions"], 3)  # step0, gate, step2
        # The same-click double-fires aborted (step0 and step2) while the
        # wizard's own three POSTs passed the guard.
        self.assertEqual(self.route.continue_.call_count, 3)
        self.assertEqual(self.route.abort.call_count, 2)
        d = result["diagnostics"]
        self.assertTrue(d["wizard_gate_clicked"])
        self.assertTrue(d["wizard_step2"])
        self.assertEqual(self.clicks, 5)
        self.assertEqual(self.locs["button, a"].bounding_box.call_count, 1)

    def test_gate_clicks_once_even_when_the_warning_persists(self):
        # The business-email error stays rendered across ticks; the gate is
        # clicked exactly once — a second click would be a duplicate POST.
        self.locator("button, a").is_visible.return_value = True
        self.locator("#id_1-company_name").is_visible.return_value = False
        result = self.run_wizard(
            [self.gate_warning(), self.gate_warning(),
             {"url": "https://example.test/try", "step0": True, "step2": False,
              "errs": [], "text": "And now, what happens?"}],
            final_text="And now, what happens?")
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 2)  # step0 + gate only
        self.assertEqual(self.locs["button, a"].bounding_box.call_count, 1)
        self.assertTrue(result["diagnostics"]["wizard_gate_clicked"])
        self.assertFalse(result["diagnostics"]["wizard_step2"])

    def test_rejection_after_step0_is_captured_and_never_reposted(self):
        # A captcha/server rejection renders as errors on the form: capture
        # them, never re-POST the identity.
        self.locator("button, a").is_visible.return_value = False
        self.locator("#id_1-company_name").is_visible.return_value = False
        result = self.run_wizard(
            [{"url": "https://example.test/try", "step0": True, "step2": False,
              "errs": ["Invalid verification"], "text": "form"}],
            final_text="Invalid verification")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertEqual(result["form_submissions"], 1)
        self.assertEqual(self.route.continue_.call_count, 1)  # step0 only

    def test_step0_reset_after_step2_is_a_rejection(self):
        # step2 POST answered by the form rendered from scratch: the wizard
        # reset — never a completion, never another POST.
        self.locator("button, a").is_visible.return_value = True
        self.locator("#id_1-company_name").is_visible.return_value = True
        result = self.run_wizard(
            [self.gate_warning(),
             {"url": "https://example.test/try", "step0": True, "step2": True,
              "errs": [], "text": "company step"},
             {"url": "https://example.test/try", "step0": True, "step2": False,
              "errs": [], "text": "wizard form"}],
            final_text="wizard form")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "wizard_reset")
        self.assertEqual(result["form_submissions"], 3)

    def test_deadline_without_any_completion_is_wizard_incomplete(self):
        # Idle ticks run the fake clock to the deadline; nothing completed.
        self.locator("button, a").is_visible.return_value = False
        self.locator("#id_1-company_name").is_visible.return_value = False
        result = self.run_wizard([], final_text="")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "wizard_incomplete")
        self.assertEqual(result["form_submissions"], 1)

    def test_navigation_race_on_the_first_tick_tolerates_and_recovers(self):
        # Measured 2026-10-01: the walk's first evaluate raced the step0
        # POST's own navigation (destroyed execution context), the exception
        # escaped as phase=outcome/class=Error, and a perfectly good
        # rejection page was reported as outcome_unknown with no html.
        self.locator("button, a").is_visible.return_value = False
        self.locator("#id_1-company_name").is_visible.return_value = False
        result = self.run_wizard(
            [{"url": "https://example.test/try", "step0": True, "step2": False,
              "errs": ["Invalid verification"], "text": "form"}],
            final_text="Invalid verification", race_once=True)
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertFalse(result["ok"])
        self.assertEqual(result["form_submissions"], 1)
        self.assertTrue(result["html"])

    def test_stop_after_posts_halts_after_the_verifying_step0_post(self):
        # Warm-up mode: a real step0 POST (that is where the score the
        # rejects cite gets verified) and then stop — the gate and step2 are
        # where identities complete, and a warm run must not reach them.
        # An inspect-only warm never POSTs and so never verifies.
        self.locator("button, a").is_visible.return_value = False
        self.locator("#id_1-company_name").is_visible.return_value = False
        result = run_form(self.context, **{**self.params, "stop_after_posts": 1})
        self.assertEqual(result["error"], "stopped_after_posts")
        self.assertFalse(result["ok"])
        self.assertEqual(result["form_submissions"], 1)
        self.assertFalse(result["diagnostics"]["wizard_gate_clicked"])
        self.assertFalse(result["diagnostics"]["wizard_step2"])
        self.assertEqual(self.locs["button, a"].click.call_count, 0)
        self.assertEqual(self.route.continue_.call_count, 1)  # step0 only
        self.assertEqual(self.route.abort.call_count, 1)      # its double-fire

    def test_stopped_after_posts_logs_the_done_line(self):
        self.locator("button, a").is_visible.return_value = False
        self.locator("#id_1-company_name").is_visible.return_value = False
        with self.assertLogs("camoufox.forms", level="INFO") as logs:
            result = run_form(self.context, **{**self.params, "stop_after_posts": 1})
        self.assertEqual(result["error"], "stopped_after_posts")
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(sum(m.startswith("form flow: done") for m in messages), 1)
        self.assertEqual(sum(m.startswith("form-run ") and "stopped_after_posts" in m
                             for m in messages), 1)

    def test_gate_and_marker_patterns_must_compile(self):
        with self.assertRaises(re.error):
            validate_form(self.request.url, None, None, gate_text="[", completion_markers=None)
        with self.assertRaises(re.error):
            validate_form(self.request.url, None, None, completion_markers=["("])


if __name__ == "__main__":
    unittest.main()

"""Pre-input recovery: the launch tab is reused, and a stalled/failed page
open or a goto timeout is retried in-run (zero POSTs either way).

cf156 evidence (2026-10-02, bench-cf156a-warm, a persistent profile):
- 15:36:33 and 15:43:49 parked at 'new page' — context.new_page() on a
  persistent context that already owns its launch tab never returned;
  isolated contexts (no launch tab) never parked there (0/41).
- 15:43:41 new_page RAISED (class Error, 0.17s, no "page ready").
- 15:38:47 goto sat its full 60s (playwright TimeoutError) and the run
  failed navigation_failed with ~58s of budget left.
Reuses test_form_flow.FormTests' fake page.
"""
import builtins
import unittest
from unittest.mock import Mock, patch

import form_flow
import test_form_flow as _flow
from form_flow import first_page, run_form


class TimeoutError(Exception):  # noqa: A001 — playwright's, NOT the builtin
    pass


class LaunchTabTests(unittest.TestCase):
    setUp = _flow.FormTests.setUp
    submit = _flow.FormTests.submit

    def test_persistent_context_reuses_its_launch_tab(self):
        self.page.is_closed.return_value = False
        self.context.pages = [self.page]
        result = run_form(self.context, **self.params)
        self.context.new_page.assert_not_called()
        self.assertTrue(result["diagnostics"]["page_reused"])
        self.assertTrue(result["ok"])

    def test_isolated_context_opens_a_fresh_page(self):
        self.context.pages = []
        result = run_form(self.context, **self.params)
        self.context.new_page.assert_called_once()
        self.assertFalse(result["diagnostics"]["page_reused"])

    def test_a_closed_launch_tab_is_not_reused(self):
        closed = Mock()
        closed.is_closed.return_value = True
        self.context.pages = [closed]
        page, reused = first_page(self.context)
        self.assertIs(page, self.page)
        self.assertFalse(reused)

    def test_new_page_is_marked_with_its_own_threshold(self):
        live = form_flow.FormLive()
        seen = []
        original = live.mark
        live.mark = lambda name, stuck_s=form_flow.PRE_SUBMIT_STUCK_S: (
            seen.append((name, stuck_s)), original(name, stuck_s))
        self.context.pages = []
        first_page(self.context, live)
        self.assertIn(("new page", form_flow.NEW_PAGE_STUCK_S), seen)

    @patch("form_flow.time.sleep")
    def test_new_page_raising_is_retried_before_any_input(self, sleep):
        self.context.pages = []
        self.context.new_page.side_effect = [RuntimeError("Browser.new_page: <unknown error>"), self.page]
        result = run_form(self.context, **self.params)
        self.assertTrue(result["ok"])
        self.assertEqual(self.context.new_page.call_count, 2)
        self.assertEqual(result["diagnostics"]["nav_error"], "new_page_failed")
        self.assertEqual(result["form_submissions"], 1)
        sleep.assert_called_once()

    @patch("form_flow.time.sleep")
    def test_new_page_failing_every_time_stays_a_zero_post_navigation_failure(self, _sleep):
        self.context.pages = []
        self.context.new_page.side_effect = RuntimeError("closed")
        result = run_form(self.context, **self.params)
        self.assertEqual(result["error"], "navigation_failed")
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(self.context.new_page.call_count, form_flow.NAV_ATTEMPTS)


class GotoTimeoutTests(unittest.TestCase):
    setUp = _flow.FormTests.setUp
    submit = _flow.FormTests.submit

    @patch("form_flow.time.sleep")
    def test_goto_timeout_is_retried_while_the_budget_holds(self, sleep):
        self.page.goto.side_effect = [TimeoutError("Page.goto: Timeout 30000ms exceeded."), Mock(status=200)]
        result = run_form(self.context, **self.params, timeout_ms=120000)
        self.assertTrue(result["ok"])
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(result["diagnostics"]["nav_error"], "timeout")
        self.assertEqual(result["form_submissions"], 1)

    @patch("form_flow.time.sleep")
    def test_goto_timeout_without_budget_is_not_retried(self, sleep):
        self.page.goto.side_effect = TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        result = run_form(self.context, **self.params, timeout_ms=30000)
        self.assertEqual(result["error"], "navigation_failed")
        self.assertEqual(self.page.goto.call_count, 1)
        sleep.assert_not_called()

    def test_each_goto_is_capped_below_the_old_60s(self):
        run_form(self.context, **self.params, timeout_ms=120000)
        self.assertLessEqual(self.page.goto.call_args.kwargs["timeout"], form_flow.NAV_TIMEOUT_MS)
        self.assertEqual(form_flow.NAV_TIMEOUT_MS, 30000)

    def test_the_form_deadline_itself_is_never_retried(self):
        # remaining() raises the BUILTIN TimeoutError: no budget, no retry.
        self.page.goto.side_effect = builtins.TimeoutError("Form deadline exceeded")
        result = run_form(self.context, **self.params, timeout_ms=120000)
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertEqual(result["form_submissions"], 0)


if __name__ == "__main__":
    unittest.main()

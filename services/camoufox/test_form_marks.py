"""Live-step marks: any pre-POST driver call may park, and each says when.

A mark's threshold sits above that call's own legitimate worst case, so
`pre_submit_hang_step` fires only for a call that never returned. Marks are
strictly pre-POST — the worker turns a stale mark into 503 retryable — so a
leaked mark would be a false retry, and `_at` must always clear it.
"""
import time
import unittest

import form_flow


class MarkTests(unittest.TestCase):
    def tearDown(self):
        form_flow.reset_live_step()

    def test_a_fresh_mark_is_not_a_hang(self):
        form_flow._mark("field select")
        self.assertIsNone(form_flow.pre_submit_hang_step())

    def test_any_step_name_goes_stale_not_only_the_arrival(self):
        for name in ("field select", "pointer move", "ready check", form_flow.PRE_SUBMIT_STEP):
            form_flow._mark(name)
            form_flow._LIVE["at"] = time.monotonic() - 99
            self.assertEqual(form_flow.pre_submit_hang_step(), name)
            form_flow.reset_live_step()

    def test_threshold_is_per_mark(self):
        form_flow._mark("field type", 18.0)
        form_flow._LIVE["at"] = time.monotonic() - 9
        self.assertIsNone(form_flow.pre_submit_hang_step())  # legit keystrokes can take this long
        form_flow._LIVE["at"] = time.monotonic() - 19
        self.assertEqual(form_flow.pre_submit_hang_step(), "field type")

    def test_at_clears_the_mark_on_success_and_on_raise(self):
        with form_flow._at("dismiss click"):
            pass
        self.assertIsNone(form_flow.pre_submit_hang_step())
        self.assertEqual(form_flow._LIVE["name"], "")
        with self.assertRaises(ValueError):
            with form_flow._at("field select"):
                raise ValueError("bounded call failed")
        self.assertEqual(form_flow._LIVE["name"], "")

    def test_a_mark_over_its_threshold_reports_during_the_block(self):
        with form_flow._at("ready check", 0.001):
            time.sleep(0.01)
            self.assertEqual(form_flow.pre_submit_hang_step(), "ready check")
        self.assertIsNone(form_flow.pre_submit_hang_step())

    def test_reset_restores_the_default_threshold(self):
        form_flow._mark("field type", 18.0)
        form_flow.reset_live_step()
        self.assertEqual(form_flow._LIVE["stuck"], form_flow.PRE_SUBMIT_STUCK_S)


if __name__ == "__main__":
    unittest.main()

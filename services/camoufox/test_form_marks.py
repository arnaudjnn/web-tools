"""Live-step marks and the run summary: the per-job FormLive object.

A mark's threshold sits above that call's own legitimate worst case, so
`hang_step` fires only for a call that never returned. Marks are strictly
pre-POST — the worker turns a stale mark into 503 retryable — so a leaked
mark would be a false retry, and `at` must always clear it. Each job owns
its own object: one job's mark can never age into another's poll.

The summary is one `form-run {json}` line per job and the measurement
source: it must carry phases, durations and per-POST shapes, and NEVER a
value, a token, a body or an exception message.
"""
import json
import time
import unittest
from unittest.mock import patch

import form_flow
from form_flow import FormLive


def _age(live, seconds):
    name, _at, stuck = live._step
    live._step = (name, form_flow._clock() - seconds, stuck)


class MarkTests(unittest.TestCase):
    def test_no_module_global_marker_remains(self):
        for name in ("_LIVE", "_mark", "reset_live_step", "pre_submit_hang_step", "_at"):
            self.assertFalse(hasattr(form_flow, name), name)

    def test_a_fresh_mark_is_not_a_hang(self):
        live = FormLive()
        live.mark("field select")
        self.assertIsNone(live.hang_step())

    def test_any_step_name_goes_stale_not_only_the_arrival(self):
        for name in ("field select", "pointer move", "ready check", form_flow.PRE_SUBMIT_STEP):
            live = FormLive()
            live.mark(name)
            _age(live, 99)
            self.assertEqual(live.hang_step(), name)

    def test_threshold_is_per_mark(self):
        live = FormLive()
        live.mark("field type", 18.0)
        _age(live, 9)
        self.assertIsNone(live.hang_step())  # legit keystrokes can take this long
        _age(live, 19)
        self.assertEqual(live.hang_step(), "field type")

    def test_at_clears_the_mark_on_success_and_on_raise(self):
        live = FormLive()
        with live.at("dismiss click"):
            pass
        self.assertIsNone(live.hang_step())
        self.assertEqual(live.step, "")
        with self.assertRaises(ValueError):
            with live.at("field select"):
                raise ValueError("bounded call failed")
        self.assertEqual(live.step, "")

    def test_a_mark_over_its_threshold_reports_during_the_block(self):
        live = FormLive()
        with live.at("ready check", 0.001):
            time.sleep(0.01)
            self.assertEqual(live.hang_step(), "ready check")
        self.assertIsNone(live.hang_step())

    def test_clear_restores_the_default_threshold(self):
        live = FormLive()
        live.mark("field type", 18.0)
        live.clear()
        self.assertEqual(live._step[2], form_flow.PRE_SUBMIT_STUCK_S)

    def test_jobs_do_not_share_marks(self):
        first, second = FormLive(), FormLive()
        first.mark("pointer move")
        _age(first, 99)
        self.assertEqual(first.hang_step(), "pointer move")
        self.assertIsNone(second.hang_step())

    def test_pointer_move_threshold_fits_one_trajectory(self):
        # One humanized move is capped by humanize's maxTime (1.5s): the
        # 25s threshold of the 6-18 step approach is gone with the steps.
        self.assertLessEqual(form_flow.POINTER_MOVE_STUCK_S, 10.0)


class SummaryTests(unittest.TestCase):
    def capture(self, live, result=None, **override):
        with self.assertLogs("camoufox.forms", level="INFO") as logs:
            self.assertTrue(live.emit(result, **override))
        lines = [r.getMessage() for r in logs.records if r.getMessage().startswith("form-run ")]
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0][len("form-run "):]), lines[0]

    def test_summary_carries_the_measurement_fields(self):
        live = FormLive(profile="warm-1", headed=True, camoufox="0.5.4/official/stable/152.0.4-beta.30")
        live.enter("navigation")
        live.enter("fields")
        result = {"error": "wizard_rejected", "ok": False, "form_submissions": 1, "status": 200,
                  "diagnostics": {
                      "submission_tokens": [{"n": 0, "path": "/try", "token": True,
                                             "mint_age_s": 71.3, "lengths": {"g": [2048]}}],
                      "token_present": True,
                      "egress": {"country": "Italy", "isp": "Vodafone", "asn": 30722},
                      "nav_attempts": 2, "nav_error": "NS_ERROR_CONNECTION_REFUSED"}}
        summary, _line = self.capture(live, result)
        self.assertEqual(summary["error"], "wizard_rejected")
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["form_submissions"], 1)
        self.assertEqual(summary["phase"], "fields")
        self.assertEqual(set(summary["durations_s"]), {"navigation", "fields"})
        self.assertEqual(summary["posts"], [{"n": 0, "token": True, "mint_age_s": 71.3}])
        self.assertEqual(summary["egress"], {"country": "Italy", "asn": 30722})
        self.assertEqual(summary["profile"], "warm-1")
        self.assertTrue(summary["headed"])
        self.assertIn("152.0.4-beta.30", summary["camoufox"])
        self.assertEqual(summary["nav_attempts"], 2)
        self.assertEqual(summary["nav_error"], "NS_ERROR_CONNECTION_REFUSED")

    def test_summary_is_emitted_once_per_job(self):
        live = FormLive()
        self.capture(live, {"error": "parked"})
        with patch.object(form_flow.log, "info") as info:
            self.assertFalse(live.emit({"error": "late orphan"}))
        info.assert_not_called()

    def test_park_summary_reads_the_live_result(self):
        # The worker emits for a parked job from the event loop: it has no
        # result of its own, so the summary reads what the thread recorded.
        live = FormLive()
        live.result = {"error": None, "form_submissions": 0, "status": 0,
                       "diagnostics": {"field_attempt": "#email"}}
        live.enter("fields")
        summary, _ = self.capture(live, None, error="parked", parked_step="pointer move")
        self.assertEqual(summary["error"], "parked")
        self.assertEqual(summary["parked_step"], "pointer move")
        self.assertEqual(summary["field"], "#email")
        self.assertEqual(summary["phase"], "fields")


if __name__ == "__main__":
    unittest.main()

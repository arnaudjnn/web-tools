"""FORM_PACING=lab: the lab runner's typing cadence and pauses on Camoufox.

The pointer path stays humanize's (one move per click), and the submit
click is never marked pre-POST.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_form_headed import RECORDED, app  # noqa: F401  (installs the stubs, imports app)

import form_flow


class LabPacingTests(unittest.TestCase):
    def test_type_text_lab_is_one_key_per_call_with_gaps(self):
        page = Mock()
        form_flow.type_text(page, "abc", lambda *a: 10_000, "lab")
        self.assertEqual([c.args[0] for c in page.keyboard.type.call_args_list], ["a", "b", "c"])
        self.assertEqual(page.wait_for_timeout.call_count, 3)
        for c in page.wait_for_timeout.call_args_list:
            self.assertGreaterEqual(c.args[0], 60)

    def test_type_text_default_keeps_the_default_cadence(self):
        page = Mock()
        form_flow.type_text(page, "abc", lambda *a: 10_000, None)
        page.keyboard.type.assert_called_once()
        self.assertEqual(page.keyboard.type.call_args.args[0], "abc")

    def test_lab_click_is_still_one_humanized_move(self):
        # humanize=True draws the trajectory; stepping it too re-opens #751.
        page, control = Mock(), Mock()
        control.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 20}
        form_flow.human_click(page, control, lambda *a: 10_000, pacing="lab")
        page.mouse.move.assert_called_once()
        self.assertNotIn("steps", page.mouse.move.call_args.kwargs)

    def test_unmarked_submit_click_never_carries_a_pre_submit_mark(self):
        page, control = Mock(), Mock()
        control.bounding_box.return_value = None
        live = form_flow.FormLive()
        seen = []
        original = live.mark
        live.mark = lambda name, *a: (seen.append(name), original(name, *a))
        form_flow.human_click(page, control, lambda *a: 10_000, live=live, pacing="lab",
                              mark_click=False)
        self.assertNotIn("field click", seen)
        control.click.assert_called_once()

    def test_pacing_comes_from_the_environment(self):
        req = SimpleNamespace()
        with patch.dict(os.environ, {"FORM_PACING": "lab"}):
            self.assertEqual(app._pacing(req), "lab")
        with patch.dict(os.environ, {"FORM_PACING": "bogus"}):
            self.assertIsNone(app._pacing(req))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORM_PACING", None)
            self.assertEqual(app._pacing(req), "auto")  # the default


class ProfileTests(unittest.TestCase):
    def test_lab_fast_is_about_half_the_lab_cadence(self):
        lab = [form_flow._key_gap_ms("lab") for _ in range(400)]
        fast = [form_flow._key_gap_ms("lab_fast") for _ in range(400)]
        self.assertLess(sum(fast) / len(fast), 0.65 * sum(lab) / len(lab))

    def test_request_pacing_overrides_the_environment(self):
        with patch.dict(os.environ, {"FORM_PACING": "lab"}):
            self.assertEqual(app._pacing(SimpleNamespace(pacing="lab_fast")), "lab_fast")
            self.assertIsNone(app._pacing(SimpleNamespace(pacing="default")))
            self.assertEqual(app._pacing(SimpleNamespace(pacing=None)), "lab")

    def test_pauses_follow_the_profile(self):
        for _ in range(50):
            self.assertLessEqual(form_flow._pause("lab_fast", "field", (1, 2)), 600)
            self.assertGreaterEqual(form_flow._pause("lab", "submit", (1, 2)), 1000)
            self.assertLessEqual(form_flow._pause(None, "field", (120, 420)), 420)


class AutoPacingTests(unittest.TestCase):
    def test_auto_is_lab_with_a_captcha_and_fast_without(self):
        self.assertEqual(form_flow.resolve_auto("auto", True), "lab")
        self.assertEqual(form_flow.resolve_auto("auto", False), "fast")
        self.assertEqual(form_flow.resolve_auto("lab", False), "lab")
        self.assertIsNone(form_flow.resolve_auto(None, True))

    def test_fast_typing_is_one_quick_type_call(self):
        page = Mock()
        form_flow.type_text(page, "hello", lambda *a: 10_000, "fast")
        page.keyboard.type.assert_called_once()
        self.assertLessEqual(page.keyboard.type.call_args.kwargs["delay"], 30)

    def test_auto_is_the_service_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORM_PACING", None)
            self.assertEqual(app._pacing(SimpleNamespace(pacing=None)), "auto")
        self.assertIsNone(app._pacing(SimpleNamespace(pacing="default")))


if __name__ == "__main__":
    unittest.main()

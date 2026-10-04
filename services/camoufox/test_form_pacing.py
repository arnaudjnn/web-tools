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
            self.assertIsNone(app._pacing(req))


if __name__ == "__main__":
    unittest.main()

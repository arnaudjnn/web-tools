"""engine: "chromium" (chromium_engine.py) — selection, defaults and wiring.

No browser is needed. app.py is imported the way test_form_headed does it,
with its third-party dependencies stubbed. The real-browser half (reload
after dismiss, the whole form suite on Patchright) lives in
test_form_browser.py under FORM_BROWSER_TEST=chromium.
"""
import os
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from test_form_headed import RECORDED, app  # noqa: F401  (installs the stubs, imports app)

import chromium_engine
import form_flow
import form_worker
import score_probe


def req(**overrides):
    base = dict(score_gate=None, engine=None, headed=True, exit_session=None, profile=None,
                sticky_exit=None, ready_expression=None)
    base.update(overrides)
    return types.SimpleNamespace(**base)


class EngineSelectionTests(unittest.TestCase):
    def setUp(self):
        RECORDED.clear()

    def test_default_engine_is_camoufox(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORM_ENGINE", None)
            self.assertEqual(chromium_engine.resolve_engine(None), "camoufox")
            app._form_browser("session-a", False, True)
        self.assertEqual(len(RECORDED), 1)  # the Camoufox stub was constructed

    def test_form_engine_env_sets_the_default(self):
        with patch.dict(os.environ, {"FORM_ENGINE": "chromium"}):
            self.assertEqual(chromium_engine.resolve_engine(None), "chromium")
            self.assertIsInstance(app._form_browser("session-b", False, True), chromium_engine.ChromiumForm)
        with patch.dict(os.environ, {"FORM_ENGINE": "netscape"}):
            self.assertEqual(chromium_engine.resolve_engine(None), "camoufox")

    def test_unknown_engine_is_refused(self):
        with self.assertRaises(ValueError):
            chromium_engine.resolve_engine("netscape")

    def test_chromium_factory_rides_the_same_sticky_exit(self):
        browser = app._form_browser("abcdef1234", False, True, None, engine="chromium")
        self.assertIsInstance(browser, chromium_engine.ChromiumForm)
        self.assertEqual(RECORDED, [])  # Camoufox never constructed
        self.assertFalse(browser.headless)
        self.assertIn(app.proxy_session.options("abcdef1234"), browser.proxy["password"])
        self.assertEqual(browser.proxy["server"], "http://proxy.test:1234")

    def test_chromium_takes_no_profile(self):
        with self.assertRaises(ValueError):
            app._form_browser("s", False, True, "bat-1", engine="chromium")

    def test_rotation_keeps_the_engine(self):
        rotate = app._form_rotator(None, None, None, False, True, engine="chromium")
        self.assertIsInstance(rotate()(), chromium_engine.ChromiumForm)
        self.assertIsNone(app._form_rotator("pinned", None, None, False, True, engine="chromium"))


class LaunchShapeTests(unittest.TestCase):
    def test_headed_launch_is_the_bundled_chromium_on_the_display(self):
        kwargs = chromium_engine.ChromiumForm(headless=False, proxy={"server": "http://p:1"}).launch_kwargs()
        self.assertFalse(kwargs["headless"])
        self.assertNotIn("channel", kwargs)  # headed: Patchright's bundled Chromium
        self.assertIn("--window-size=1440,900", kwargs["args"])
        self.assertIn("--disable-backgrounding-occluded-windows", kwargs["args"])
        self.assertEqual(kwargs["proxy"], {"server": "http://p:1"})

    def test_headless_is_new_headless_not_the_shell(self):
        kwargs = chromium_engine.ChromiumForm(headless=True).launch_kwargs()
        self.assertEqual(kwargs["channel"], "chromium")
        self.assertNotIn("proxy", kwargs)

    def test_context_has_no_viewport_override(self):
        # page_fp coherence: the page IS the window, never a viewport as large
        # as the whole Xvfb screen; locale/timezone follow the Italian pool.
        options = chromium_engine.ChromiumForm().context_options
        self.assertTrue(options["no_viewport"])
        self.assertNotIn("viewport", options)
        self.assertEqual(options["locale"], "it-IT")
        self.assertEqual(options["timezone_id"], "Europe/Rome")

    def test_exit_never_raises_and_stops_everything(self):
        engine = chromium_engine.ChromiumForm()
        browser, playwright = MagicMock(), MagicMock()
        browser.close.side_effect = RuntimeError("already gone")
        engine._browser, engine._playwright = browser, playwright
        self.assertFalse(engine.__exit__(None, None, None))
        playwright.stop.assert_called_once()


class WorkerContextTests(unittest.TestCase):
    def run_isolated(self, manager):
        def runner(context, timeout_ms, **params):
            return {"ok": True, "form_submissions": 0, "error": None, "diagnostics": {}}
        return form_worker._run_isolated(lambda: manager, time.monotonic() + 30,
                                         form_flow.FormLive(), runner, {"url": "https://example.test/f"})

    def test_engine_context_options_are_used_and_service_workers_stay_blocked(self):
        browser = MagicMock()
        manager = MagicMock()
        manager.__enter__.return_value = browser
        manager.context_options = {"no_viewport": True, "locale": "it-IT", "service_workers": "allow"}
        self.run_isolated(manager)
        kwargs = browser.new_context.call_args.kwargs
        self.assertTrue(kwargs["no_viewport"])
        self.assertNotIn("viewport", kwargs)
        self.assertEqual(kwargs["service_workers"], "block")

    def test_camoufox_keeps_its_viewport(self):
        browser = MagicMock()
        manager = MagicMock(spec=["__enter__", "__exit__"])
        manager.__enter__.return_value = browser
        self.run_isolated(manager)
        self.assertEqual(browser.new_context.call_args.kwargs["viewport"], {"width": 1440, "height": 900})


class GateDefaultTests(unittest.TestCase):
    def test_chromium_defaults_the_gate_off(self):
        self.assertIs(app._gate_default(req(engine="chromium")), False)
        self.assertFalse(score_probe.gate_wanted(app._gate_default(req(engine="chromium")), True, None, False))

    def test_explicit_gate_still_runs_for_chromium(self):
        self.assertIs(app._gate_default(req(engine="chromium", score_gate=True)), True)

    def test_camoufox_keeps_the_implicit_gate(self):
        self.assertIsNone(app._gate_default(req(engine="camoufox")))
        self.assertTrue(score_probe.gate_wanted(app._gate_default(req(engine="camoufox")), True, None, False))

    def test_form_run_line_names_the_engine(self):
        self.assertEqual(app._form_live(req(engine="chromium")).summary()["engine"], "chromium")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORM_ENGINE", None)
            self.assertEqual(app._form_live(req()).summary()["engine"], "camoufox")

    def test_gate_probes_the_forms_engine(self):
        calls = []
        with patch.dict(score_probe._deps, {"browser_factory": lambda *a, **k: calls.append((a, k))}):
            score_probe._factory("tok", True, None, "chromium")()
            score_probe._factory("tok", True, None)()
        self.assertEqual(calls[0], (("tok", False, True, None), {"engine": "chromium"}))
        self.assertEqual(calls[1], (("tok", False, True, None), {}))


class MainWorldTests(unittest.TestCase):
    def test_patchright_page_asks_for_the_main_world(self):
        seen = {}

        class PatchrightPage:
            def evaluate(self, expression, arg=None, isolated_context=True):
                seen.update(expression=expression, isolated_context=isolated_context)
                return True

        self.assertTrue(form_flow.main_world_eval(PatchrightPage(), "window.formReady === true"))
        self.assertEqual(seen, {"expression": "window.formReady === true", "isolated_context": False})

    def test_other_engines_use_the_mw_prefix(self):
        page = MagicMock()
        form_flow.main_world_eval(page, "1 + 1")
        page.evaluate.assert_called_once_with("mw:(1 + 1)")


if __name__ == "__main__":
    unittest.main()


class LabPacingTests(unittest.TestCase):
    """engine=chromium types and moves at the lab runner's cadence (18/20 on
    Atoka); the submit click is never marked pre-POST."""

    def test_type_text_lab_is_one_key_per_call_with_gaps(self):
        page = Mock()
        form_flow.type_text(page, "abc", lambda *a: 10_000, "lab")
        self.assertEqual([c.args[0] for c in page.keyboard.type.call_args_list], ["a", "b", "c"])
        self.assertEqual(page.wait_for_timeout.call_count, 3)
        for c in page.wait_for_timeout.call_args_list:
            self.assertGreaterEqual(c.args[0], 60)

    def test_type_text_default_keeps_camoufox_cadence(self):
        page = Mock()
        form_flow.type_text(page, "abc", lambda *a: 10_000, None)
        page.keyboard.type.assert_called_once()
        self.assertEqual(page.keyboard.type.call_args.args[0], "abc")

    def test_lab_click_steps_the_pointer_path(self):
        page, control = Mock(), Mock()
        control.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 20}
        form_flow.human_click(page, control, lambda *a: 10_000, pacing="lab")
        self.assertGreaterEqual(page.mouse.move.call_args.kwargs.get("steps", 1), 12)

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

    def test_pacing_follows_the_engine(self):
        req = SimpleNamespace(engine="chromium")
        with patch.dict(os.environ, {"FORM_CHROMIUM_PACING": "lab"}):
            self.assertEqual(app._pacing(req), "lab")
        self.assertIsNone(app._pacing(SimpleNamespace(engine="camoufox")))

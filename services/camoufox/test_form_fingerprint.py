"""Fingerprint knobs (`fingerprint` request field) — no browser needed.

Pins: no spec = the default launch, unchanged; a spec maps onto Camoufox
launch kwargs at the single place it crosses into Camoufox (_form_browser),
for both the isolated and the persistent-profile path; bad specs are
ValueErrors (the endpoint's 400); the form-run line names knobs, never
values; the rotator and the score gate's probes carry the same spec.
"""
import asyncio
import os
import tempfile
import unittest
from functools import partial
from unittest.mock import patch

import test_form_headed  # noqa: F401  (installs the third-party stubs, imports app)
from test_form_headed import RECORDED

import app
import fingerprint
import score_probe


def _screen(**kw):
    return ("Screen", kw)


class LaunchKwargsTests(unittest.TestCase):
    def test_none_and_empty_add_nothing(self):
        self.assertEqual(fingerprint.launch_kwargs(None), {})
        self.assertEqual(fingerprint.launch_kwargs({}), {})

    def test_maps_every_knob(self):
        spec = {"os": "windows", "locale": ["it-IT", "en-US"], "screen": [1920, 1080],
                "window": [1920, 1040], "fonts": ["Arial"], "custom_fonts_only": False,
                "block_webgl": False, "webgl_config": ["Google Inc. (Intel)", "ANGLE (Intel)"],
                "humanize": 1.5, "fingerprint_preset": True,
                "config": {"window.devicePixelRatio": 1.25},
                "firefox_user_prefs": {"intl.locale.requested": "it-IT"}}
        out = fingerprint.launch_kwargs(spec, screen_factory=_screen)
        self.assertEqual(out["os"], "windows")
        self.assertEqual(out["locale"], ["it-IT", "en-US"])
        self.assertEqual(out["screen"], ("Screen", {"min_width": 1920, "max_width": 1920,
                                                    "min_height": 1080, "max_height": 1080}))
        self.assertEqual(out["window"], (1920, 1040))
        self.assertEqual(out["webgl_config"], ("Google Inc. (Intel)", "ANGLE (Intel)"))
        self.assertEqual(out["humanize"], 1.5)
        self.assertTrue(out["fingerprint_preset"])
        self.assertEqual(out["config"], {"window.devicePixelRatio": 1.25})
        self.assertEqual(out["firefox_user_prefs"], {"intl.locale.requested": "it-IT"})

    def test_rejects_bad_specs(self):
        bad = [
            {"nope": 1}, {"os": "android"}, {"screen": [10, 10]}, {"screen": [1920]},
            {"window": ["a", 900]}, {"webgl_config": ["v", "r"]},  # needs os
            {"humanize": 99}, {"block_webgl": "yes"}, {"config": {"k": {"nested": 1}}},
            {"custom_fonts_only": True}, {"locale": ""}, "windows",
        ]
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                fingerprint.validate(spec)

    def test_names_never_values(self):
        names = fingerprint.names({"os": "macos", "config": {"window.devicePixelRatio": 2}})
        self.assertEqual(names, ["config", "os", "config.window.devicePixelRatio"])
        self.assertNotIn("macos", names)
        self.assertIsNone(fingerprint.names(None))


class FormBrowserTests(unittest.TestCase):
    def setUp(self):
        RECORDED.clear()

    def test_default_launch_unchanged(self):
        app._form_browser("s-fp0", False, True)
        kwargs = RECORDED[0]
        self.assertTrue(kwargs["geoip"])
        self.assertTrue(kwargs["humanize"])
        for key in ("os", "locale", "screen", "window", "config"):
            self.assertNotIn(key, kwargs)

    def test_spec_layers_over_defaults(self):
        app._form_browser("s-fp1", False, True, None,
                          fingerprint={"os": "windows", "locale": "it-IT", "humanize": False})
        kwargs = RECORDED[0]
        self.assertEqual(kwargs["os"], "windows")
        self.assertEqual(kwargs["locale"], "it-IT")
        self.assertFalse(kwargs["humanize"])
        self.assertTrue(kwargs["geoip"])
        self.assertFalse(kwargs["headless"])

    def test_profile_first_draw_takes_spec_later_launches_keep_it(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"FORM_PROFILE_DIR": root}):
            app._form_browser("s-fp2", False, True, "fp-prof", fingerprint={"os": "macos"})
            self.assertEqual(RECORDED[0]["from_options"]["os"], "macos")
            # A later launch with a different spec reuses the saved draw.
            app._form_browser("s-fp3", False, True, "fp-prof", fingerprint={"os": "linux"})
            self.assertEqual(RECORDED[1]["from_options"]["os"], "macos")

    def test_rotator_carries_spec(self):
        rotate = app._form_rotator(None, None, None, False, True, fingerprint={"os": "linux"})
        factory = rotate()
        self.assertEqual(factory.keywords, {"fingerprint": {"os": "linux"}})
        plain = app._form_rotator(None, None, None, False, True)()
        self.assertEqual(plain.keywords, {})


class GateProbeTests(unittest.TestCase):
    def test_probe_factory_carries_spec_only_when_set(self):
        seen = []

        async def run(job, **kw):
            seen.append(job.args[0])
            return {"diagnostics": {}}

        class _Worker:
            pass

        worker = _Worker()
        worker.run = run
        deps = dict(score_probe._deps)
        try:
            score_probe._deps.update(worker=worker, browser_factory=lambda *a, **k: None,
                                     run_isolated=lambda *a, **k: None, camoufox="t")
            for fp in ({"os": "windows"}, None):
                asyncio.run(score_probe.run_probe(
                    oracle_url="https://oracle.test/oracle/recaptcha", session="tok", profile=None,
                    headed=True, wait_ms=0, field_count=1, action=None, timeout_ms=10_000,
                    fingerprint=fp))
        finally:
            score_probe._deps.clear()
            score_probe._deps.update(deps)
        self.assertIsInstance(seen[0], partial)
        self.assertEqual(seen[0].keywords, {"fingerprint": {"os": "windows"}})
        self.assertEqual(seen[1].keywords, {})


if __name__ == "__main__":
    unittest.main()

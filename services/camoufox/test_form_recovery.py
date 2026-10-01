"""Recovery classification for the shared render browser (no browser needed).

`_run_render` retries once on a fresh thread when the worker thread is dead.
This pins which error strings qualify: a poisoned Playwright thread must heal
itself, while a launch-environment failure (sandbox, proxy) must surface at
once instead of burning a retry that cannot help.

app.py is imported with its third-party dependencies stubbed — the pattern is
a module-level constant with no runtime dependency on them.
"""
import re
import sys
import types
import unittest


def _stub_module(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_stubs():
    fastapi = _stub_module("fastapi")

    class _App:
        def __init__(self, *args, **kwargs):
            pass

        def _decorator(self, *args, **kwargs):
            def wrap(fn):
                return fn
            return wrap

        on_event = _decorator
        get = _decorator
        post = _decorator

    fastapi.FastAPI = _App
    fastapi.HTTPException = type("HTTPException", (Exception,), {})

    responses = _stub_module("fastapi.responses")
    responses.JSONResponse = object

    pydantic = _stub_module("pydantic")

    class _BaseModel:
        def __init_subclass__(cls, **kwargs):
            pass

    pydantic.BaseModel = _BaseModel
    pydantic.Field = lambda *args, **kwargs: None

    camoufox = _stub_module("camoufox")
    sync_api = _stub_module("camoufox.sync_api")
    sync_api.Camoufox = object
    utils = _stub_module("camoufox.utils")
    utils.launch_options = lambda **kwargs: {**kwargs}
    # form_worker is NOT stubbed: it imports nothing beyond stdlib + form_flow,
    # so the real module loads and stays importable for the other test files
    # sharing this unittest process.


_install_stubs()

import app  # noqa: E402  (stubs above stand in for its third-party imports)


class DeadBrowserPatternTests(unittest.TestCase):
    def test_poisoned_thread_heals(self):
        # Observed live 2026-09-26: a launch on a thread that had already
        # hosted a Playwright instance raises this instead of a browser error.
        self.assertTrue(app._DEAD_BROWSER.search(
            "It looks like you are using Playwright Sync API inside the asyncio loop.\n"
            "Please use the Async API instead."))

    def test_dead_browser_still_heals(self):
        for message in (
            "Browser.new_page: Target page, context or browser has been closed",
            "browser has been closed",
            "Connection closed while reading from the driver",
        ):
            with self.subTest(message=message[:40]):
                self.assertTrue(app._DEAD_BROWSER.search(message))

    def test_launch_environment_failures_surface_at_once(self):
        # A retry on a fresh thread cannot fix these — same host, same proxy —
        # so they must propagate without the recovery detour.
        for message in (
            "Sandbox: CanCreateUserNamespace() clone() failure: EACCES",
            "BrowserType.launch: Failed to launch the browser process",
            "net::ERR_PROXY_CONNECTION_FAILED",
            "Timeout 60000ms exceeded",
        ):
            with self.subTest(message=message[:40]):
                self.assertFalse(app._DEAD_BROWSER.search(message))

    def test_pattern_is_case_insensitive(self):
        self.assertTrue(app._DEAD_BROWSER.search("BROWSER HAS BEEN CLOSED"))
        self.assertTrue(app._DEAD_BROWSER.search("sync api inside the asyncio loop"))


class RetryableZeroPostTests(unittest.TestCase):
    """Which failures the caller may replay (HTTP 503, retryable: true)."""

    def test_pre_submit_failures_with_zero_posts_qualify(self):
        for error in ("fields_failed", "browser_launch_failed", "navigation_failed"):
            with self.subTest(error=error):
                self.assertTrue(app._retryable_zero_post({
                    "error": error, "form_submissions": 0,
                    "diagnostics": {"submit_click_attempted": False},
                }))

    def test_any_post_or_any_click_disqualifies(self):
        for data in (
            {"error": "fields_failed", "form_submissions": 1,
             "diagnostics": {"submit_click_attempted": False}},
            {"error": "fields_failed", "form_submissions": 0,
             "diagnostics": {"submit_click_attempted": True}},
            {"error": "no_submission", "form_submissions": 0,
             "diagnostics": {"submit_click_attempted": True}},
            {"error": "outcome_unknown", "form_submissions": 0,
             "diagnostics": {"submit_click_attempted": True}},
            {"error": "captcha_token_missing", "form_submissions": 0,
             "diagnostics": {"submit_click_attempted": True}},
        ):
            with self.subTest(error=data["error"], subs=data["form_submissions"]):
                self.assertFalse(app._retryable_zero_post(data))

    def test_missing_diagnostics_never_replays_a_posted_outcome(self):
        # launch/parse paths can omit diagnostics; the error allowlist plus
        # a zero submission count is still the proof.
        self.assertTrue(app._retryable_zero_post(
            {"error": "browser_launch_failed", "form_submissions": 0}))
        self.assertFalse(app._retryable_zero_post(
            {"error": "outcome_unknown", "form_submissions": 0}))


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

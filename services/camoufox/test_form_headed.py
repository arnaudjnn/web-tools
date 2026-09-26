"""Headed flag plumbing for isolated form browsers (no browser needed).

Score-gated forms (reCAPTCHA v3) refuse the headless fingerprint, so
/form-submit takes an opt-in `headed` browser that runs under the image's
xvfb. This pins the mapping headed -> headless=False at the single place the
flag crosses into Camoufox, with the shared readers untouched (they never go
through _form_browser).

app.py is imported with its third-party dependencies stubbed — the mapping is
construction-time kwargs with no runtime dependency on them.
"""
import os
import sys
import types
import unittest

RECORDED = []


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

    class _Camoufox:
        def __init__(self, *args, **kwargs):
            RECORDED.append(kwargs)

        def __enter__(self):
            raise AssertionError("must not launch in this test")

    sync_api.Camoufox = _Camoufox


_install_stubs()
os.environ.setdefault("PROXY_URL", "http://user:pass_country-IT@proxy.test:1234")

import app  # noqa: E402  (stubs above stand in for its third-party imports)


class HeadedFormBrowserTests(unittest.TestCase):
    def setUp(self):
        RECORDED.clear()

    def test_default_is_headless(self):
        app._form_browser("session-1")
        self.assertTrue(RECORDED[0]["headless"])

    def test_headed_opt_in(self):
        app._form_browser("session-2", False, True)
        self.assertFalse(RECORDED[0]["headless"])

    def test_headed_keeps_stealth_and_proxy(self):
        browser = app._form_browser("session-3", True, True)
        kwargs = RECORDED[0]
        self.assertTrue(kwargs["geoip"])
        self.assertTrue(kwargs["humanize"])
        self.assertTrue(browser is not None)
        self.assertIn("_session-session-3", kwargs["proxy"]["password"])

    def test_request_model_accepts_headed(self):
        # pydantic is stubbed out, so assert the field declaration survives on
        # the model rather than validating (annotations are strings here).
        self.assertIn("headed", app.FormSubmitRequest.__annotations__)


if __name__ == "__main__":
    unittest.main()

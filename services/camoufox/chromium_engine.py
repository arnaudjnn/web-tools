"""The forms' second browser engine: Patchright Chromium (`engine: "chromium"`).

Why it exists (lane-chromium, Atoka, 2026-10-03, one guarded POST per
attempt, random identities, Evomi IT residential exits, no score gate):
headed Patchright Chromium got 18/20 step-0 POSTs accepted (9/10 on a macOS
display, 9/10 in the Scrapling image under Xvfb, linux/amd64). Production
Camoufox got 11/30 that day. With our oracle's pre-form score at <=0.7,
Chromium was accepted 6/7 times (four at 0.1-0.3). Camoufox was accepted
2/14 at <=0.7. The target trusts this engine where our oracle does not, so
`engine: "chromium"` leaves the score gate OFF by default (app.py).
Headless Chromium lost on the same target (0/4 POSTed: oracle 0.0-0.5, and
the post-consent reload landed mid-fill).

ONE form path, two engines. This module only LAUNCHES. `ChromiumForm(...)`
has the shape the form worker expects from `Camoufox(...)`: a context
manager whose `__enter__` returns a Browser with `new_context()`. Everything
after the launch is the same code for both engines: run_form, the POST guard,
the token check, the wizard, retries, verdicts and exit coherence.

The engine differs from Camoufox in only these ways:
- Its own context options (`context_options`, read by form_worker). There is
  no viewport override: the page is the real window (`no_viewport`), sized
  to the Xvfb screen. A fixed 1440x900 viewport inside a 1440x900 screen
  would make the inner window as big as the screen, which no real browser
  window is. Locale and timezone follow the Italian exit pool (Camoufox
  derives them from the exit IP with geoip).
- Patchright evaluates in an ISOLATED world by default. That is where its
  stealth comes from: page scripts cannot see the driver's own calls. Reads
  of page globals therefore go through `form_flow.main_world_eval`, which
  asks for the main world explicitly.
- Headless uses Chromium's new headless mode (`channel="chromium"`).
  Patchright's plain `headless=True` would launch chrome-headless-shell,
  which is a different binary.
"""
from __future__ import annotations

import os
import shutil
import tempfile

# Matches the Xvfb screen in entrypoint.sh: the headed window fills it.
WINDOW_SIZE = os.environ.get("FORM_CHROMIUM_WINDOW", "1440,900")
LOCALE = os.environ.get("FORM_CHROMIUM_LOCALE", "it-IT")
TIMEZONE = os.environ.get("FORM_CHROMIUM_TIMEZONE", "Europe/Rome")
ENGINES = ("camoufox", "chromium")


def default_engine() -> str:
    """FORM_ENGINE (default camoufox) — what a request without `engine` uses."""
    value = os.environ.get("FORM_ENGINE", "camoufox").strip().lower()
    return value if value in ENGINES else "camoufox"


def resolve_engine(engine: str | None) -> str:
    if engine is None:
        return default_engine()
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}")
    return engine


def persistent_default() -> bool:
    """FORM_CHROMIUM_PERSISTENT (default on): launch a persistent context on
    a throwaway profile instead of browser.launch() + new_context()."""
    return os.environ.get("FORM_CHROMIUM_PERSISTENT", "1").strip().lower() not in ("0", "false", "no", "off")


def context_options() -> dict:
    """What form_worker passes to new_context() for this engine."""
    return {"no_viewport": True, "service_workers": "block",
            "locale": LOCALE, "timezone_id": TIMEZONE}


def launch_args(headless: bool) -> list[str]:
    args = [f"--window-size={WINDOW_SIZE}", "--window-position=0,0"]
    if not headless:
        # Xvfb has no compositor that reports occlusion. Never let Chromium
        # decide the form's window is hidden and park its timers: a parked
        # page would hold reCAPTCHA's execute() and the submit handler.
        args += ["--disable-backgrounding-occluded-windows",
                 "--disable-renderer-backgrounding"]
    return args


class ChromiumForm:
    """Patchright Chromium for ONE form job (isolated per attempt).

    `__enter__` starts Patchright on the calling thread (the form worker's
    fresh job thread; its sync API is thread-bound) and returns the Browser.
    `__exit__` closes the browser and stops Patchright, and never raises.
    """

    def __init__(self, *, headless: bool = False, proxy: dict | None = None,
                 timeout: int = 30000, extra_args: list[str] | None = None,
                 persistent: bool | None = None):
        self.headless = bool(headless)
        self.proxy = proxy
        self.timeout = timeout
        self.extra_args = list(extra_args or [])
        self.persistent = persistent_default() if persistent is None else bool(persistent)
        self.context_options = context_options()
        self._playwright = None
        self._browser = None
        self._user_dir = None

    def launch_kwargs(self) -> dict:
        kwargs = {"headless": self.headless, "timeout": self.timeout,
                  "args": launch_args(self.headless) + self.extra_args}
        if self.headless:
            kwargs["channel"] = "chromium"
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return kwargs

    def __enter__(self):
        from patchright.sync_api import sync_playwright  # imported per job: only this engine needs it
        self._playwright = sync_playwright().start()
        try:
            if self.persistent:
                # A persistent context on a FRESH temp profile, deleted on exit:
                # still one isolated identity per attempt, but a regular
                # profile rather than an incognito-style context. Patchright
                # recommends this, and the lab measured this shape.
                self._user_dir = tempfile.mkdtemp(prefix="form-chromium-")
                self._browser = self._playwright.chromium.launch_persistent_context(
                    self._user_dir, **self.launch_kwargs(), **self.context_options)
            else:
                self._browser = self._playwright.chromium.launch(**self.launch_kwargs())
        except BaseException:
            self._stop()
            raise
        return self._browser

    def __exit__(self, *_exc):
        self._stop()
        return False

    def _stop(self):
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = None
        for close in ((browser.close if browser is not None else None),
                      (playwright.stop if playwright is not None else None)):
            if close is None:
                continue
            try:
                close()
            except Exception:
                pass
        if self._user_dir:
            shutil.rmtree(self._user_dir, ignore_errors=True)
            self._user_dir = None

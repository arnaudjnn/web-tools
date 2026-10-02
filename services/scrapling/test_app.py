"""Unit tests for the Scrapling sidecar's pure functions.

No browser and no Scrapling install: the two Scrapling imports app.py needs at
module load are stubbed, so this runs with only fastapi + httpx installed:

    python -m unittest discover -s services/scrapling -p 'test_*.py' -v
"""

import sys
import time
import types
import unittest

for name in (
    "scrapling",
    "scrapling.engines",
    "scrapling.engines.toolbelt",
    "scrapling.engines.toolbelt.proxy_rotation",
    "scrapling.fetchers",
):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["scrapling.engines.toolbelt.proxy_rotation"].ProxyRotator = object
sys.modules["scrapling.fetchers"].StealthySession = object

import app  # noqa: E402


class PickMode(unittest.TestCase):
    def test_stealth_hosts(self):
        self.assertIs(app.pick_mode("https://www.linkedin.com/in/x"), app.Mode.STEALTH)
        self.assertIs(app.pick_mode("https://linkedin.com/"), app.Mode.STEALTH)
        self.assertIs(app.pick_mode("https://web.archive.org/cdx/search/cdx?url=x"), app.Mode.STEALTH)

    def test_suffix_match_is_on_a_label_boundary(self):
        self.assertIs(app.pick_mode("https://notlinkedin.com/"), app.Mode.FAST)

    def test_default_is_fast(self):
        self.assertIs(app.pick_mode("https://example.com/"), app.Mode.FAST)
        self.assertIs(app.pick_mode("not a url"), app.Mode.FAST)

    def test_trustpilot_is_never_solve(self):
        self.assertIs(app.pick_mode("https://www.trustpilot.com/review/x"), app.Mode.FAST)
        self.assertTrue(app._host_matches("www.trustpilot.com", app.NEVER_ESCALATE_HOSTS))


class LooksLikeChallenge(unittest.TestCase):
    def test_interstitial(self):
        self.assertTrue(app.looks_like_challenge(403, "<title>Just a moment...</title>"))
        self.assertTrue(app.looks_like_challenge(503, "window._cf_chl_opt={}"))

    def test_status_must_be_a_challenge_status(self):
        self.assertFalse(app.looks_like_challenge(200, "<title>Just a moment...</title>"))
        self.assertFalse(app.looks_like_challenge(404, "<title>Just a moment...</title>"))

    def test_plain_forbidden_is_not_a_challenge(self):
        self.assertFalse(app.looks_like_challenge(403, "<h1>Forbidden</h1>"))

    def test_large_pages_are_real_pages(self):
        self.assertFalse(app.looks_like_challenge(403, "just a moment" + "x" * 200_001))


class AbsolutizeLinks(unittest.TestCase):
    def test_relative_links_and_images(self):
        md = "[a](/users/1) ![i](img.png) [b](../up)"
        self.assertEqual(
            app._absolutize_links(md, "https://ex.com/dir/page"),
            "[a](https://ex.com/users/1) ![i](https://ex.com/dir/img.png) [b](https://ex.com/up)",
        )

    def test_schemed_targets_untouched(self):
        md = "[m](mailto:a@b.c) [h](https://x.org/p) [d](data:image/png;base64,AA)"
        self.assertEqual(app._absolutize_links(md, "https://ex.com/"), md)

    def test_title_is_kept(self):
        self.assertEqual(
            app._absolutize_links('[a](/p "Title")', "https://ex.com/"),
            '[a](https://ex.com/p "Title")',
        )


class Deadlines(unittest.TestCase):
    def test_caps_are_validated_not_clamped(self):
        with self.assertRaises(Exception):
            app.FetchRequest(url="https://ex.com", timeout_ms=app.MAX_FETCH_MS + 1)
        for model in (app.RawRequest, app.ScreenshotRequest, app.PdfRequest):
            with self.assertRaises(Exception):
                model(url="https://ex.com", timeout_ms=180_000)
        self.assertEqual(app.FetchRequest(url="https://ex.com", timeout_ms=app.MAX_FETCH_MS).timeout_ms, 90_000)

    def test_slack_stays_under_the_client_abort(self):
        # The toolkit aborts at timeout + 25s (scrapling.ts CLIENT_SLACK_MS).
        self.assertLess(app.HARD_DEADLINE_SLACK_S, 25)

    def test_escalation_fits_inside_the_first_deadline(self):
        now = time.monotonic()
        deadline = now + 60 + app.HARD_DEADLINE_SLACK_S
        budget = app.escalation_budget_ms(deadline, now + 30)  # first run took 30s
        self.assertGreater(budget, 0)
        # Second run's timeout plus half the slack still lands on the same deadline.
        self.assertLessEqual(now + 30 + budget / 1000 + app.HARD_DEADLINE_SLACK_S / 2, deadline + 1e-6)

    def test_no_escalation_when_the_budget_is_spent(self):
        now = time.monotonic()
        deadline = now + 60 + app.HARD_DEADLINE_SLACK_S
        self.assertEqual(app.escalation_budget_ms(deadline, deadline - 5), 0)


class ServeDocument(unittest.TestCase):
    def test_strip_scripts(self):
        html = '<p>a</p><script src="x.js"></script><SCRIPT>evil()</SCRIPT ><p>b</p>'
        self.assertEqual(app.strip_scripts(html), "<p>a</p><p>b</p>")

    def test_main_frame_navigation_is_answered_once(self):
        main = object()

        class Req:
            def __init__(self, nav, frame):
                self._nav, self.frame = nav, frame

            def is_navigation_request(self):
                return self._nav

        class Route:
            def __init__(self, req):
                self.request, self.fulfilled, self.fell_back = req, None, False

            def fulfill(self, **kw):
                self.fulfilled = kw

            def fallback(self):
                self.fell_back = True

        class Page:
            main_frame = main

            def route(self, pattern, handler):
                self.pattern, self.handler = pattern, handler

        page = Page()
        app.serve_document("<p>ok</p><script>x()</script>")(page)
        first, again, sub = Route(Req(True, main)), Route(Req(True, main)), Route(Req(False, main))
        for r in (first, again, sub):
            page.handler(r)
        self.assertEqual(first.fulfilled["body"], "<p>ok</p>")
        self.assertTrue(again.fell_back)
        self.assertTrue(sub.fell_back)


if __name__ == "__main__":
    unittest.main()

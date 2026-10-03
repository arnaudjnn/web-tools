"""Opt-in real-browser test; submits only to a loopback fixture we own."""
import os
import signal
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import form_flow
from form_flow import run_form
from form_worker import run_isolated_form


@contextmanager
def browser_engine():
    if os.environ.get("FORM_BROWSER_TEST") == "camoufox":
        from camoufox.sync_api import Camoufox
        with Camoufox(headless=True, main_world_eval=True) as browser:
            yield browser
    elif os.environ.get("FORM_BROWSER_TEST") == "chromium":
        # The production engine=chromium launch (Patchright, isolated-world
        # evaluation); FORM_BROWSER_HEADED=1 runs it headed, as forms do.
        from chromium_engine import ChromiumForm
        with ChromiumForm(headless=os.environ.get("FORM_BROWSER_HEADED") != "1", persistent=False) as browser:
            yield browser
    else:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                yield browser
            finally:
                browser.close()


@unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST"), "opt-in browser fixture")
class BrowserTests(unittest.TestCase):
    def test_native_form_success(self):
        self.exercise(False)

    def test_native_form_and_duplicate_post_guard(self):
        self.exercise(True)

    def test_inspection_performs_no_form_post(self):
        self.exercise(False, inspect_only=True)

    def test_missing_required_token_never_reaches_server(self):
        self.exercise(False, missing_token=True)

    def test_repeated_isolated_browser_lifetimes(self):
        # Each attempt is bounded by SIGALRM (POSIX, main thread): camoufox's
        # launch or __exit__ has been observed to block forever — one stuck
        # close hung this whole job in CI (2026-10-01) with no output after
        # the fourth test. The alarm raises inside the blocking call; a rare
        # uninterruptible state still hangs, and the workflow's job timeout
        # is the backstop for that.
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                self.exercise(False, isolated=True)

    def _run_isolated_bounded(self, **params):
        """run_isolated_form under a SIGALRM bound; raises inside a stuck launch/close."""
        deadline = time.monotonic() + 30
        if not hasattr(signal, "setitimer"):
            return run_isolated_form(browser_engine, deadline=deadline, **params)

        def _alarm(_signum, _frame):
            raise TimeoutError("isolated attempt exceeded the SIGALRM bound")

        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, 90)
        try:
            return run_isolated_form(browser_engine, deadline=deadline, **params)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def exercise(self, duplicate, inspect_only=False, isolated=False, missing_token=False):
        posts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                html = b'''<script>setTimeout(()=>{window.formReady=true},250)</script><form method="post" action="/form">
                  <input id="name" name="name" required><input id="agree" name="agree" type="checkbox">
                  <input type="hidden" name="g-recaptcha-response" value="fixture-token-not-a-real-captcha">
                  <select id="country" name="country"><option value="IT">Italy</option></select>
                  <button id="submit">Submit</button></form>'''
                if missing_token:
                    html = html.replace(b"fixture-token-not-a-real-captcha", b"")
                self.wfile.write(html)
                if duplicate:
                    self.wfile.write(b'''<script>document.querySelector('form').addEventListener('submit',async event=>{
                    event.preventDefault();
                    const accepted=await fetch('/form',{method:'POST',body:'first=1'});
                    await fetch('/form',{method:'POST',body:'duplicate=1'}).catch(()=>{});
                    location.href=accepted.url;
                  });</script>''')

            def do_POST(self):
                posts.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(303)
                self.send_header("Location", "/done")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/form"
            params = dict(url=url, fields=[
                    {"selector": "#name", "value": "Test"},
                    {"selector": "#agree", "action": "check"},
                    {"selector": "#country", "action": "select", "value": "IT"},
                ], submit="#submit", settle_ms=1000, timeout_ms=10000,
                    success_url=r"/done$", inspect_only=inspect_only,
                    ready_expression="window.formReady === true", require_captcha_token=not duplicate)
            if isolated:
                params.pop("timeout_ms")
                result = self._run_isolated_bounded(**params)
            else:
                with browser_engine() as browser:
                    context = browser.new_context(service_workers="block")
                    result = run_form(context, **params)
                    context.close()
            if inspect_only:
                self.assertEqual(len(posts), 0)
                self.assertEqual(result["form_submissions"], 0)
                self.assertFalse(result["diagnostics"]["submit_click_attempted"])
                return
            if missing_token:
                self.assertEqual(len(posts), 0)
                self.assertEqual(result["form_submissions"], 0)
                self.assertEqual(result["error"], "captcha_token_missing")
                self.assertTrue(result["diagnostics"]["captcha_guard_blocked"])
                return
            self.assertTrue(result["diagnostics"]["ready_condition_met"])
            self.assertEqual(result["form_submissions"], 1)
            self.assertEqual(len(posts), 1)
            self.assertEqual(result["status"], 303)
            if not duplicate:
                self.assertTrue(result["ok"])
                self.assertTrue(result["diagnostics"]["token_present"])
                self.assertIn(b"name=Test", posts[0])
                self.assertIn(b"agree=on", posts[0])
                self.assertIn(b"country=IT", posts[0])
            # The site's handler tries a second POST before navigating. It must
            # be blocked, with only the accepted first response counted.
            self.assertEqual(result["ok"], result["url"].endswith("/done"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST"), "opt-in browser fixture")
class ReloadTests(unittest.TestCase):
    """A consent click (or anything else) that RELOADS the page must never
    leave the submit clicking an emptied form. Lane-chromium, Atoka
    2026-10-03: iubenda's accept reloads the page. Headed, the reload came
    2-6 s after the click and before typing. Headless, it came ~25 s after
    the click, mid-fill, and the click then hit HTML5 validation with no
    POST."""

    def serve(self, page):
        posts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                consented = "consent=1" in (self.headers.get("Cookie") or "")
                self.wfile.write(page(consented).encode())

            def do_POST(self):
                posts.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(303)
                self.send_header("Location", "/done")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/form", posts

    FORM = '''<form method="post" action="/form"><input id="name" name="name" required>
      <input id="agree" name="agree" type="checkbox" required><button id="submit">Submit</button></form>'''

    def run_form(self, url, **extra):
        params = dict(url=url, fields=[{"selector": "#name", "value": "Test"},
                                       {"selector": "#agree", "action": "check"}],
                      submit="#submit", settle_ms=3000, timeout_ms=60000, success_url=r"/done$")
        params.update(extra)
        with browser_engine() as browser:
            context = browser.new_context(service_workers="block")
            try:
                return run_form(context, **params)
            finally:
                context.close()

    def test_dismiss_that_reloads_the_page_types_after_the_reload(self):
        def page(consented):
            banner = "" if consented else '''<div id="banner"><button class="accept" onclick="
                document.cookie='consent=1; path=/'; this.parentNode.remove();
                setTimeout(() => location.reload(), 1500)">Accept</button></div>'''
            return banner + self.FORM

        url, posts = self.serve(page)
        result = self.run_form(url, dismiss=[".accept"])
        self.assertEqual(result["diagnostics"]["dismiss_clicked"], [".accept"])
        self.assertTrue(result["diagnostics"]["dismiss_reload"])
        self.assertIsNone(result["diagnostics"]["refilled_after_reload"])
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(len(posts), 1)
        self.assertIn(b"name=Test", posts[0])
        self.assertIn(b"agree=on", posts[0])

    @unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST") == "chromium", "the engine=chromium launch")
    def test_production_chromium_launch_through_the_worker(self):
        # What /form-submit runs for engine=chromium: the worker launches
        # ChromiumForm (a persistent context on a throwaway profile, the
        # engine's own context options) and tears it all down.
        from chromium_engine import ChromiumForm

        def page(consented):
            banner = "" if consented else '''<div><button class="accept" onclick="
                document.cookie='consent=1; path=/'; this.parentNode.remove();
                setTimeout(() => location.reload(), 1000)">Accept</button></div>'''
            return banner + self.FORM + '''<script>window.formReady = true;</script>'''

        url, posts = self.serve(page)
        made = []

        def factory():
            made.append(ChromiumForm(headless=os.environ.get("FORM_BROWSER_HEADED") != "1"))
            return made[-1]

        result = run_isolated_form(
            factory, deadline=time.monotonic() + 60, url=url,
            fields=[{"selector": "#name", "value": "Test"}, {"selector": "#agree", "action": "check"}],
            submit="#submit", settle_ms=3000, success_url=r"/done$", dismiss=[".accept"],
            ready_expression="window.formReady === true")
        self.assertTrue(made[0].persistent)
        self.assertTrue(result["ok"], result["error"])
        self.assertTrue(result["diagnostics"]["ready_condition_met"])  # main world, not Patchright's isolated one
        self.assertTrue(result["diagnostics"]["dismiss_reload"])
        self.assertTrue(result["diagnostics"]["page_reused"])  # the persistent launch's own tab
        self.assertEqual(len(posts), 1)
        self.assertIsNone(made[0]._user_dir)  # the throwaway profile is gone

    def test_a_reload_after_typing_refills_once_before_the_click(self):
        # The page reloads itself (once) shortly after the last keystroke:
        # past the dismiss window, after the fields were typed.
        def page(_consented):
            return self.FORM + '''<script>
              let timer = null;
              document.querySelector('#name').addEventListener('input', () => {
                if (sessionStorage.getItem('reloaded')) return;
                clearTimeout(timer);
                timer = setTimeout(() => { sessionStorage.setItem('reloaded', '1'); location.reload(); }, 700);
              });</script>'''

        url, posts = self.serve(page)
        with patch.object(form_flow, "DISMISS_RELOAD_WAIT_S", 0.5):
            result = self.run_form(url)
        self.assertIn(result["diagnostics"]["refilled_after_reload"], ("fields", "preclick"))
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(len(posts), 1)
        self.assertIn(b"name=Test", posts[0])
        self.assertIn(b"agree=on", posts[0])


if __name__ == "__main__":
    unittest.main()

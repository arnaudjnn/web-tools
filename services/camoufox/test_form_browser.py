"""Opt-in real-browser test; submits only to a loopback fixture we own."""
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from form_flow import run_form


@contextmanager
def browser_engine():
    if os.environ.get("FORM_BROWSER_TEST") == "camoufox":
        from camoufox.sync_api import Camoufox
        with Camoufox(headless=True) as browser:
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

    def exercise(self, duplicate):
        posts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b'''<form method="post" action="/form">
                  <input id="name" name="name" required><input id="agree" name="agree" type="checkbox">
                  <select id="country" name="country"><option value="IT">Italy</option></select>
                  <button id="submit">Submit</button></form>''')
                if duplicate:
                    self.wfile.write(b'''<script>document.querySelector('form').addEventListener('submit',()=>{
                    fetch('/form',{method:'POST',body:'duplicate=1'}).catch(()=>{});
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
            with browser_engine() as browser:
                context = browser.new_context(service_workers="block")
                url = f"http://127.0.0.1:{server.server_port}/form"
                result = run_form(context, url=url, fields=[
                    {"selector": "#name", "value": "Test"},
                    {"selector": "#agree", "action": "check"},
                    {"selector": "#country", "action": "select", "value": "IT"},
                ], submit="#submit", settle_ms=1000, timeout_ms=10000,
                    success_url=r"/done$")
                context.close()
                self.assertEqual(result["form_submissions"], 1)
                self.assertEqual(len(posts), 1)
                self.assertEqual(result["status"], 303)
                if not duplicate:
                    self.assertTrue(result["ok"])
                    self.assertIn(b"name=Test", posts[0])
                    self.assertIn(b"agree=on", posts[0])
                    self.assertIn(b"country=IT", posts[0])
                # Either competing POST may win. A blocked native navigation
                # must not be reported as success just because fetch redirected.
                self.assertEqual(result["ok"], result["url"].endswith("/done"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

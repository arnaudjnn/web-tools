"""/form-inspect: unit checks plus an opt-in browser run against a loopback fixture we own."""
import json
import os
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import form_inspect
from form_flow import run_form
from form_worker import run_isolated_form
from form_inspect import inspect_form

SECRETS = ("csrf-secret-value", "textarea-default-secret", "prefilled-secret")

PAGE = b'''<!doctype html><html><body>
<div id="cookie-banner" class="cookie-consent"><p>We use cookies</p><button id="cookie-ok">Accept all</button></div>
<p>Step 1 of 2</p>
<script src="/recaptcha/api.js?render=explicit"></script>
<form id="lead" method="post" action="/form">
  <label for="email">Work email *</label><input id="email" name="email" type="email" required autocomplete="email">
  <label>Company <input name="company" placeholder="ACME" value="prefilled-secret"></label>
  <label for="country">Country</label>
  <select id="country" name="country" required><option value="">Choose</option><option value="IT" selected>Italy</option><option value="FR">France</option></select>
  <fieldset><legend>Size</legend>
    <label><input type="radio" name="size" value="s" required> Small</label>
    <label><input type="radio" name="size" value="l"> Large</label>
  </fieldset>
  <label for="notes">Notes</label><textarea id="notes" name="notes">textarea-default-secret</textarea>
  <input type="checkbox" id="agree" name="agree" required style="opacity:0;position:absolute"><label for="agree">I agree</label>
  <input type="hidden" name="csrf" value="csrf-secret-value">
  <div style="position:absolute;left:-9999px"><input name="website" tabindex="-1" autocomplete="off"></div>
  <fieldset style="display:none"><input name="step2_phone"><input name="step2_role"></fieldset>
  <div class="g-recaptcha" data-sitekey="fixture-sitekey-not-real"></div>
  <button id="send" type="submit">Send request</button>
</form>
<script>fetch('/beacon', {method: 'POST', body: 'beacon=1'}).catch(() => {});</script>
</body></html>'''


# Atoka's shape (measured live 2026-10-02): a django-formtools wizard
# (`<prefix>-current_step`, step-prefixed `0-` names), django-recaptcha V3
# writing its token into the hidden `0-captcha` (the widget's own
# g-recaptcha-response textarea stays empty), a hidden `0-email_last`
# shadowing the visible `0-email` (the honeypot), and an iubenda consent form
# of purpose toggles. No ids on the form; the submit is a plain button.
ATOKA_PAGE = b'''<!doctype html><html><body class="has-cookie-banner">
<div id="iubenda-cs-banner" class="iubenda-cs-container"><p>Cookie</p>
  <form class="iub-prefs">
    <div class="iub-toggle"><input type="checkbox" id="iub-toggle-id-1" style="position:absolute;opacity:0"><label for="iub-toggle-id-1">Functionality</label></div>
    <div class="iub-toggle"><input type="checkbox" id="iub-toggle-id-2" style="position:absolute;opacity:0"><label for="iub-toggle-id-2">Experience</label></div>
    <div class="iub-toggle"><input type="checkbox" id="iub-toggle-id-3" style="position:absolute;opacity:0"><label for="iub-toggle-id-3">Measurement</label></div>
    <div class="iub-toggle"><input type="checkbox" id="iub-toggle-id-4" style="position:absolute;opacity:0"><label for="iub-toggle-id-4">Marketing</label></div>
  </form>
  <button class="iubenda-cs-accept-btn" onclick="document.getElementById('iubenda-cs-banner').style.display='none'">Accetta</button>
</div>
<div class="container"><div class="row"><div class="col">
<form method="post" action="/register/" class="registration">
  <input type="hidden" name="csrfmiddlewaretoken" value="csrf-secret-value">
  <input type="hidden" name="atoka_registration_wizard_l_p1_v3-current_step" value="0">
  <label for="id_0-email">Email aziendale *</label><input type="email" name="0-email" id="id_0-email" required>
  <input type="hidden" name="0-email_last" id="id_0-email_last">
  <label for="id_0-first_name">Nome</label><input type="text" name="0-first_name" id="id_0-first_name" required>
  <input type="hidden" name="0-captcha" class="g-recaptcha" data-sitekey="fixture-sitekey-not-real" data-widget-uuid="fixture" required id="id_0-captcha">
  <textarea id="g-recaptcha-response" name="g-recaptcha-response" class="g-recaptcha-response" style="display:none"></textarea>
  <button type="submit" class="btn btn-primary">Continua</button>
</form></div></div></div>
<script src="/recaptcha/api.js?render=fixture-sitekey-not-real"></script>
<script>// django-recaptcha V3 stand-in: the integration fills its own hidden field.
document.querySelector('[data-widget-uuid="fixture"]').value = 'fixture-token-not-a-real-captcha';</script>
</body></html>'''


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


class SuggestTests(unittest.TestCase):
    def test_suggests_required_fields_submit_and_same_origin_post_target(self):
        form = {"action": "https://example.test/api/lead", "method": "post",
                "fields": [{"selector": "#email", "type": "email", "action": "type", "required": True},
                           {"selector": "#notes", "type": "textarea", "action": "type", "required": False},
                           {"selector": None, "type": "radio", "action": "check", "required": True,
                            "options": [{"selector": 'input[name="size"][value="s"]'}]}],
                "submit_candidates": [{"selector": "#send"}]}
        captcha = [{"provider": "recaptcha", "token_field": "g-recaptcha-response"}]
        got = form_inspect._suggest("https://example.test/contact?x=1", form, captcha, [{"selector": "#ok"}])
        self.assertEqual(got["fields"], [{"selector": "#email", "action": "type"},
                                         {"selector": 'input[name="size"][value="s"]', "action": "check"}])
        self.assertEqual(got["submit"], "#send")
        self.assertEqual(got["dismiss"], ["#ok"])
        self.assertEqual(got["captcha_field"], "g-recaptcha-response")
        self.assertEqual(got["submission_urls"], ["https://example.test/api/lead"])

    def test_cross_origin_action_is_never_a_submission_url(self):
        form = {"action": "https://forms.other.test/x", "method": "post", "fields": [],
                "submit_candidates": []}
        self.assertNotIn("submission_urls", form_inspect._suggest("https://example.test/", form, [], []))

    def test_guard_aborts_every_mutating_request(self):
        page = Mock()
        page.goto.return_value = SimpleNamespace(status=200)
        page.url = "https://example.test/"
        page.frames = []
        page.evaluate.return_value = {"forms": [], "captcha": [], "cookie_banners": [],
                                      "wizard": {"likely": False, "signals": [], "next_buttons": []}}
        context = Mock()
        context.new_page.return_value = page
        result = inspect_form(context, url="https://example.test/", wait_ms=0)
        guard = context.route.call_args.args[1]
        for method, origin in (("POST", "https://example.test/f"), ("PUT", "https://beacon.other.test/")):
            route = Mock(request=SimpleNamespace(method=method, url=origin))
            guard(route)
            route.abort.assert_called_once_with("blockedbyclient")
            route.continue_.assert_not_called()
        route = Mock(request=SimpleNamespace(method="GET", url="https://example.test/app.js"))
        guard(route)
        route.continue_.assert_called_once()
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["diagnostics"]["blocked_mutations"], 2)
        page.locator.assert_not_called()
        page.keyboard.type.assert_not_called()


@unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST"), "opt-in browser fixture")
class InspectBrowserTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        posts = self.posts

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                if self.path.startswith("/recaptcha/"):
                    self.send_header("Content-Type", "application/javascript")
                    self.end_headers()
                    self.wfile.write(b"/* fixture, not a real captcha */")
                    return
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                page = ATOKA_PAGE if self.path.startswith("/register") else PAGE
                self.wfile.write(b"<p>done</p>" if self.path == "/done" else page)

            def do_POST(self):
                posts.append((self.path, self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                self.send_response(303)
                self.send_header("Location", "/done")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/form"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def inspect(self, url=None):
        with browser_engine() as browser:
            context = browser.new_context(service_workers="block")
            try:
                return inspect_form(context, url=url or self.url, wait_ms=500, timeout_ms=15000)
            finally:
                context.close()

    def test_inspection_describes_the_form_and_never_posts(self):
        result = self.inspect()
        self.assertEqual(self.posts, [])  # the page's own load-time beacon POST was aborted
        self.assertGreaterEqual(result["diagnostics"]["blocked_mutations"], 1)
        self.assertEqual(result["form_submissions"], 0)
        dump = json.dumps(result)
        for secret in SECRETS + ("fixture-sitekey-not-real",):
            self.assertNotIn(secret, dump)

        self.assertEqual(len(result["forms"]), 1)
        form = result["forms"][0]
        self.assertEqual(form["selector"], "form#lead")
        self.assertEqual(form["kind"], "form")
        self.assertEqual(form["method"], "post")
        by_name = {f["name"]: f for f in form["fields"]}
        self.assertEqual(set(by_name), {"email", "company", "country", "size", "notes", "agree"})
        email = by_name["email"]
        self.assertEqual((email["selector"], email["type"], email["action"], email["required"]),
                         ("#email", "email", "type", True))
        self.assertEqual(email["label"], "Work email *")
        self.assertEqual(email["autocomplete"], "email")
        company = by_name["company"]
        self.assertEqual(company["selector"], 'input[name="company"]')
        self.assertEqual(company["label"], "Company")
        self.assertEqual(company["placeholder"], "ACME")
        self.assertEqual(by_name["country"]["action"], "select")
        self.assertEqual([o["value"] for o in by_name["country"]["options"]], ["", "IT", "FR"])
        size = by_name["size"]
        self.assertEqual((size["type"], size["label"], size["required"]), ("radio", "Size", True))
        self.assertEqual([(o["value"], o["label"]) for o in size["options"]], [("s", "Small"), ("l", "Large")])
        self.assertEqual(size["options"][0]["selector"], 'input[name="size"][value="s"]')
        self.assertEqual(by_name["agree"]["action"], "check")
        self.assertEqual(by_name["agree"]["label_selector"], 'label[for="agree"]')
        self.assertEqual(form["hidden_inputs"], ["csrf"])
        self.assertEqual([h["name"] for h in form["honeypot_candidates"]], ["website"])
        self.assertIn("tabindex_-1", form["honeypot_candidates"][0]["reasons"])
        self.assertEqual(form["submit_candidates"][0], {"selector": "#send", "text": "Send request", "type": "submit"})

        self.assertEqual([c["provider"] for c in result["captcha"]], ["recaptcha"])
        self.assertIn("v2", result["captcha"][0]["variants"])
        self.assertTrue(result["captcha"][0]["sitekey_present"])
        self.assertEqual(result["cookie_banners"][0]["selector"], "#cookie-ok")
        self.assertTrue(result["wizard"]["likely"])
        self.assertTrue(any(s.startswith("step_text:") for s in result["wizard"]["signals"]))
        self.assertTrue(any(s.startswith("hidden_steps:") for s in result["wizard"]["signals"]))

        suggested = form["suggested"]
        self.assertEqual(suggested["submit"], "#send")
        self.assertEqual(suggested["captcha_field"], "g-recaptcha-response")
        self.assertEqual(suggested["dismiss"], ["#cookie-ok"])
        self.assertEqual([f["selector"] for f in suggested["fields"]],
                         ["#email", "#country", 'input[name="size"][value="s"]', "#agree"])

    def test_suggested_request_submits_through_run_form(self):
        # The point of the tool: its output IS the /form-submit request.
        suggested = self.inspect()["forms"][0]["suggested"]
        values = {"#email": "test@example.test", "#country": "FR"}
        fields = [dict(f, **({"value": values[f["selector"]]} if f["selector"] in values else {}))
                  for f in suggested["fields"]]
        with browser_engine() as browser:
            context = browser.new_context(service_workers="block")
            try:
                result = run_form(context, url=suggested["url"], fields=fields, submit=suggested["submit"],
                                  dismiss=suggested["dismiss"], success_url=r"/done$",
                                  settle_ms=1000, timeout_ms=60000)
            finally:
                context.close()
        self.assertEqual(result["form_submissions"], 1)
        self.assertTrue(result["ok"], result["error"])
        submitted = [body for path, body in self.posts if path == "/form"]
        self.assertEqual(len(submitted), 1)
        body = submitted[0]
        for expected in (b"email=test%40example.test", b"country=FR", b"size=s", b"agree=on"):
            self.assertIn(expected, body)

    def atoka(self):
        return self.inspect(self.url.replace("/form", "/register/"))

    def test_atoka_shape_captcha_honeypot_wizard_selectors(self):
        result = self.atoka()
        self.assertEqual(self.posts, [])
        dump = json.dumps(result)
        for secret in ("csrf-secret-value", "fixture-token-not-a-real-captcha", "fixture-sitekey-not-real"):
            self.assertNotIn(secret, dump)
        # The registration form first; the iubenda toggles are a consent
        # widget ranked last, with no honeypots and no submit skeleton.
        self.assertEqual([f["kind"] for f in result["forms"]], ["form", "consent"])
        form, consent = result["forms"]
        self.assertEqual(consent["honeypot_candidates"], [])
        self.assertNotIn("suggested", consent)
        self.assertNotIn("iub-toggle", json.dumps([f["honeypot_candidates"] for f in result["forms"]]))

        # 4. short, unique selectors
        self.assertEqual(form["selector"], 'form[action="/register/"]')
        self.assertEqual(form["submit_candidates"][0]["selector"], 'form[action="/register/"] button[type="submit"]')
        self.assertEqual([f["selector"] for f in form["fields"]], ["#id_0-email", "#id_0-first_name"])

        # 1. the integration's field, not the widget textarea
        self.assertEqual(form["captcha_fields"], ["0-captcha", "g-recaptcha-response"])
        recaptcha = result["captcha"][0]
        self.assertEqual(recaptcha["token_field"], "0-captcha")
        self.assertEqual(recaptcha["widget_field"], "g-recaptcha-response")
        self.assertIn("v3", recaptcha["variants"])
        self.assertNotIn("v2", recaptcha["variants"])
        self.assertTrue(recaptcha["sitekey_present"])

        # 2. the shadow is the honeypot; the response textarea and state fields are not
        self.assertEqual([(h["name"], h["reasons"]) for h in form["honeypot_candidates"]],
                         [("0-email_last", ["type_hidden", "shadows:0-email"])])
        self.assertEqual(form["hidden_inputs"], ["csrfmiddlewaretoken", "atoka_registration_wizard_l_p1_v3-current_step"])

        # 3. formtools wizard
        wizard = result["wizard"]
        self.assertTrue(wizard["likely"])
        self.assertIn("formtools_current_step:atoka_registration_wizard_l_p1_v3-current_step", wizard["signals"])
        self.assertIn("step_prefix:0-", wizard["signals"])
        self.assertEqual(wizard["fields"]["current_step_field"], "atoka_registration_wizard_l_p1_v3-current_step")
        self.assertEqual(wizard["fields"]["step_prefixes"], ["0-"])
        self.assertIn("0-email_last", wizard["fields"]["step_fields"])
        self.assertIn("'1-'", wizard["hint"])

        suggested = form["suggested"]
        self.assertEqual(suggested["captcha_field"], "0-captcha")
        self.assertEqual(suggested["dismiss"], [".iubenda-cs-accept-btn"])
        self.assertEqual(suggested["fields"], [{"selector": "#id_0-email", "action": "type"},
                                               {"selector": "#id_0-first_name", "action": "type"}])

    def test_atoka_suggested_submits_with_token_guard(self):
        suggested = self.atoka()["forms"][0]["suggested"]
        values = {"#id_0-email": "test@example.test", "#id_0-first_name": "Test"}
        fields = [dict(f, value=values[f["selector"]]) for f in suggested["fields"]]
        with browser_engine() as browser:
            context = browser.new_context(service_workers="block")
            try:
                result = run_form(context, url=suggested["url"], fields=fields, submit=suggested["submit"],
                                  dismiss=suggested["dismiss"], success_url=r"/done$",
                                  captcha_field=suggested["captcha_field"], require_captcha_token=True,
                                  settle_ms=1000, timeout_ms=60000)
            finally:
                context.close()
        self.assertTrue(result["ok"], result["error"])
        self.assertTrue(result["diagnostics"]["token_present"])
        submitted = [body for path, body in self.posts if path == "/register/"]
        self.assertEqual(len(submitted), 1)
        self.assertIn(b"0-email=test%40example.test", submitted[0])
        self.assertIn(b"0-email_last=&", submitted[0] + b"&")

    def test_isolated_worker_path(self):
        result = run_isolated_form(browser_engine, deadline=time.monotonic() + 30, runner=inspect_form,
                                   url=self.url, wait_ms=200)
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["forms"]), 1)
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()

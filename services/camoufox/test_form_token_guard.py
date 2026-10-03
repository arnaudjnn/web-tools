"""A tokenless POST the guard blocked ends the run, is a 503 retryable,
and triggers retry_on_captcha_rejection; the reCAPTCHA client is re-checked
right before the click.

Atoka batch 2, run 6 (2026-10-03 00:49 UTC): the submit click went out, the
page's own handler POSTed step 0 WITHOUT a token (posts[0].token=false,
reloads=0), require_captcha_token aborted it — and the wizard walk then sat
448 s to the 540 s deadline before answering captcha_token_missing with zero
submissions. Its form-run line: captcha_signal "lib" (not "anchor"),
captcha_scripts [7,7,2], two recaptcha__*.js bodies cut after their headers
(NS_ERROR_NET_PARTIAL_TRANSFER). The arrival gate trusted a finished library
copy while a cut one existed; the page's listener (attached in
grecaptcha.ready) never ran, so the click fell through to a native submit.

Fake pages only (borrowed from test_form_flow / test_form_captcha_reload,
so their tests are not collected twice); the opt-in browser test submits
only to a loopback fixture.
"""
import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import form_flow
import form_retry
import test_form_captcha_reload as _reload
import test_form_flow as _flow
import test_form_retry as _retry
from form_flow import run_form

app = _retry.app


# ── 1. a guard-blocked POST ends the run at once ────────────────────

class WizardGuardBlockTests(unittest.TestCase):
    setUp = _flow.WizardTests.setUp
    guard = _flow.WizardTests.guard
    respond = _flow.WizardTests.respond
    post_step0 = _flow.WizardTests.post_step0
    fire_post = _flow.WizardTests.fire_post
    run_wizard = _flow.WizardTests.run_wizard

    def test_a_blocked_step0_post_ends_the_walk_immediately(self):
        self.request.post_data = "0-email=private-email&0-captcha="
        self.params.update(captcha_field="0-captcha", require_captcha_token=True, timeout_ms=540_000)
        started = self.clock[0]
        result = self.run_wizard([])  # every walk tick would cost 2 fake seconds
        self.assertEqual(result["error"], "captcha_token_missing")
        self.assertEqual(result["form_submissions"], 0)
        self.assertTrue(result["diagnostics"]["captcha_guard_blocked"])
        self.route.continue_.assert_not_called()
        # No walk at all: before, this polled to the 540 s deadline.
        self.assertLess(self.clock[0] - started, 30.0)
        self.assertFalse(result["diagnostics"]["wizard_gate_clicked"])
        self.assertTrue(form_retry.guard_blocked_zero_post(result))

    def test_a_token_carrying_post_still_walks(self):
        self.request.post_data = "0-email=private-email&0-captcha=fixture-token"
        self.params.update(captcha_field="0-captcha", require_captcha_token=True)
        self.locator("button, a").is_visible.return_value = False
        self.locator("#id_1-company_name").is_visible.return_value = False
        result = self.run_wizard([{"url": "https://example.test/try", "step0": False, "step2": False,
                                   "errs": [], "text": "And now, what happens? ok"}],
                                 final_text="And now, what happens? ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 1)


class PlainGuardBlockTests(unittest.TestCase):
    setUp = _flow.FormTests.setUp
    submit = _flow.FormTests.submit

    def test_a_blocked_post_does_not_wait_out_settle_ms(self):
        self.request.post_data = "email=private-email"
        waits = []

        def wait_for_url(*args, **kwargs):
            waits.append(kwargs.get("timeout"))
            raise RuntimeError("Timeout 500ms exceeded")  # the slice: still on the form

        self.page.wait_for_url.side_effect = wait_for_url
        self.page.url = self.request.url
        result = run_form(self.context, **self.params, captcha_field="0-captcha",
                          require_captcha_token=True, settle_ms=60_000)
        self.assertEqual(result["error"], "captcha_token_missing")
        self.assertEqual(waits, [])  # the guard had already blocked: no wait at all

    def test_an_unanswered_form_waits_in_slices_up_to_settle_ms(self):
        waits = []

        def wait_for_url(*args, **kwargs):
            waits.append(kwargs.get("timeout"))
            time.sleep(0.01)
            raise RuntimeError("Timeout exceeded")

        self.page.wait_for_url.side_effect = wait_for_url
        self.page.url = self.request.url
        with patch.object(form_flow, "OUTCOME_SLICE_MS", 20):
            result = run_form(self.context, **self.params, settle_ms=1000)
        self.assertEqual(result["form_submissions"], 1)
        self.assertGreater(len(waits), 3)
        self.assertTrue(all(w <= 20 for w in waits))


# ── 2. classification: 503 retryable, and a retry trigger ───────────

def blocked(**over):
    data = {"contract_version": 2, "ok": False, "error": "captcha_token_missing", "form_submissions": 0,
            "status": 0, "url": "https://form.test/f", "html": "",
            "diagnostics": {"captcha_guard_blocked": True, "submit_click_attempted": True,
                            "token_present": False}}
    data.update(over)
    return data


class ClassificationTests(unittest.TestCase):
    def test_a_guard_blocked_post_is_provably_zero_post(self):
        self.assertTrue(app._retryable_zero_post(blocked()))
        self.assertTrue(form_retry.guard_blocked_zero_post(blocked()))
        # Not the guard's own block, or a POST that did leave: never.
        self.assertFalse(app._retryable_zero_post(blocked(diagnostics={"submit_click_attempted": True})))
        self.assertFalse(app._retryable_zero_post(blocked(form_submissions=1)))
        self.assertFalse(app._retryable_zero_post(blocked(error="no_submission")))
        # The other post-click answers stay unknown.
        self.assertFalse(app._retryable_zero_post({"error": "no_submission", "form_submissions": 0,
                                                   "diagnostics": {"submit_click_attempted": True}}))


class GuardRetryTests(unittest.TestCase):
    """The endpoint wiring with the REAL one-attempt classification: the fake
    run raises the 503 _form_attempt_run raises for run_form-shaped data."""
    setUp = _retry.EndpointTests.setUp
    tearDown = _retry.EndpointTests.tearDown
    req = _retry.EndpointTests.req

    def submit(self, req, answers, floor=1.0):
        sessions = []
        answers = list(answers)

        async def run(req_, deadline, session, live):
            sessions.append(session)
            data = answers.pop(0)
            gate = {"session": session, "record": {"chosen_score": 0.9, "asn": 16232}}
            if app._retryable_zero_post(data):
                detail = {"message": "Form never submitted", "retryable": True, "reason": data["error"]}
                if data["error"] == "captcha_token_missing":
                    detail.update(error="captcha_token_missing", form_submissions=0, score_gate=gate["record"])
                raise app.HTTPException(status_code=503, detail=detail)
            data = dict(data, diagnostics=dict(data["diagnostics"], score_gate=gate["record"]))
            return data, gate, session

        with patch.object(app, "_form_attempt_run", run), patch.object(app, "HTTPException", _retry._HTTPError), \
                patch.object(app, "FormSubmitResponse", lambda **kw: kw), \
                patch.object(app, "_attempt_floor_s", lambda req_, gate: floor):
            return asyncio.run(app.form_submit(req)), sessions

    def test_a_guard_block_is_retried_on_a_fresh_exit(self):
        result, sessions = self.submit(self.req(), [blocked(), _retry.answered(**_retry.DONE)])
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(sessions[0], sessions[1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 1)  # the blocked POST never left
        self.assertEqual([(a["error"], a["status"], a["form_submissions"]) for a in result["attempts"]],
                         [("captcha_token_missing", 503, 0), (None, 302, 1)])
        self.assertEqual(result["attempts"][0]["score_gate_score"], 0.9)

    def test_only_guard_blocks_is_the_503_with_its_attempts(self):
        with self.assertRaises(_retry._HTTPError) as caught:
            self.submit(self.req(retry_on_captcha_rejection=1), [blocked(), blocked()])
        detail = caught.exception.detail
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual((detail["retryable"], detail["error"], detail["form_submissions"]),
                         (True, "captcha_token_missing", 0))
        self.assertEqual([a["n"] for a in detail["attempts"]], [1, 2])

    def test_a_guard_block_after_a_rejection_keeps_the_real_answer(self):
        result, _ = self.submit(self.req(retry_on_captcha_rejection=1),
                                [_retry.answered(**_retry.REJECTED), blocked()])
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertEqual(result["form_submissions"], 1)
        self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], "retries_exhausted")

    def test_a_guard_block_then_a_rejection_then_acceptance(self):
        result, sessions = self.submit(self.req(retry_on_captcha_rejection=3),
                                       [blocked(), _retry.answered(**_retry.REJECTED),
                                        _retry.answered(**_retry.DONE)])
        self.assertEqual(len(sessions), 3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 2)

    def test_without_retries_it_is_a_plain_503(self):
        with self.assertRaises(_retry._HTTPError) as caught:
            self.submit(self.req(retry_on_captcha_rejection=0), [blocked()])
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("attempts", caught.exception.detail)

    def test_a_pinned_exit_is_not_retried_and_answers_503(self):
        with self.assertRaises(_retry._HTTPError) as caught:
            self.submit(self.req(exit_session="mine"), [blocked(), _retry.answered(**_retry.DONE)])
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(len(caught.exception.detail["attempts"]), 1)

    def test_the_deadline_stops_a_guard_retry(self):
        with self.assertRaises(_retry._HTTPError) as caught:
            self.submit(self.req(timeout_ms=60_000), [blocked(), _retry.answered(**_retry.DONE)], floor=305.0)
        self.assertEqual(len(caught.exception.detail["attempts"]), 1)


# ── 3. the reCAPTCHA client, re-checked before the click ────────────

LIB_2 = _reload.LIB_JS.replace("recaptcha__it", "recaptcha__en")


class PreClickTests(unittest.TestCase):
    setUp = _reload.CaptchaGateTests.setUp
    setUp_flow = _flow.FormTests.setUp
    submit = _flow.FormTests.submit
    evaluate = _reload.CaptchaGateTests.evaluate
    handler = _reload.CaptchaGateTests.handler
    load = _reload.CaptchaGateTests.load

    def run_loads(self, loads, during_fields=(), **overrides):
        """`loads`: per goto, a list of (url, outcome); `during_fields`: loads
        that land while the fields are typed (a consent click re-loading)."""
        sequence = iter(loads)
        pending = list(during_fields)

        def goto(*args, **kwargs):
            for url, outcome in next(sequence):
                self.load(url, outcome)
            return SimpleNamespace(status=200)

        def typed(*args, **kwargs):
            while pending:
                self.load(*pending.pop(0))

        self.page.goto.side_effect = goto
        self.page.keyboard.type.side_effect = typed
        return run_form(self.context, **{**self.params, **overrides})

    def test_a_finished_copy_does_not_vouch_for_a_cut_one(self):
        # Run 6's shape at arrival: one copy finished, another cut → not
        # usable (lib_cut) → the one pre-input reload → usable.
        result = self.run_loads([[(_reload.API_JS, "ok"), (_reload.LIB_JS, "ok"), (LIB_2, "cut")],
                                 [(_reload.API_JS, "ok"), (_reload.LIB_JS, "ok")]])
        d = result["diagnostics"]
        self.assertTrue(d["captcha_script_reload"])
        self.assertEqual(d["captcha_signal"], "lib")
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(result["form_submissions"], 1)

    def test_a_library_cut_after_arrival_never_reaches_the_click(self):
        # Ready at arrival, cut while typing (consent re-loads scripts): the
        # pre-click check refuses — zero POSTs, nothing clicked, retryable.
        result = self.run_loads([[(_reload.API_JS, "ok"), (_reload.LIB_JS, "ok")]],
                                during_fields=[(LIB_2, "cut")])
        d = result["diagnostics"]
        self.assertEqual(d["captcha_signal"], "lib")
        self.assertEqual(d["captcha_preclick_signal"], "lib_cut")
        self.assertEqual(result["error"], "captcha_unavailable")
        self.assertEqual(result["form_submissions"], 0)
        self.assertFalse(d["submit_click_attempted"])
        self.route.continue_.assert_not_called()
        self.assertFalse(d["captcha_rotate"])  # a pinned (non-rotatable) exit
        self.assertTrue(app._retryable_zero_post(result))

    def test_an_unpinned_exit_asks_to_rotate_from_the_pre_click_check(self):
        result = self.run_loads([[(_reload.API_JS, "ok"), (_reload.LIB_JS, "ok")]],
                                during_fields=[(LIB_2, "cut")], exit_rotatable=True)
        self.assertTrue(result["diagnostics"]["captcha_rotate"])

    def test_a_usable_client_is_clicked_and_the_signal_recorded(self):
        result = self.run_loads([[(_reload.API_JS, "ok"), (_reload.LIB_JS, "ok")]])
        self.assertEqual(result["diagnostics"]["captcha_preclick_signal"], "lib")
        self.assertEqual(result["form_submissions"], 1)

    def test_pages_without_recaptcha_skip_the_pre_click_check(self):
        result = self.run_loads([[]])
        self.assertNotIn("captcha_preclick_signal", result["diagnostics"])
        self.assertEqual(result["form_submissions"], 1)


# ── 4. real browser, loopback fixture ───────────────────────────────

NATIVE = '''<!doctype html><title>f</title>
<form method="post" action="/form">
  <input id="email" name="0-email" required>
  <input type="hidden" name="0-captcha" value="{token}">
  <button id="submit" type="submit">Invia</button>
</form>'''


@unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST"), "opt-in browser fixture")
class BrowserGuardTests(unittest.TestCase):
    def test_a_tokenless_native_submit_ends_fast_and_retries_on_a_fresh_context(self):
        from http.server import BaseHTTPRequestHandler
        from form_worker import run_isolated_form
        from test_form_browser import browser_engine

        gets = []
        posts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status, html, headers=()):
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())

            def do_GET(self):
                gets.append(self.path)
                if self.path.startswith("/done"):
                    return self._send(200, "<p>in revisione</p>")
                # The first page "could not mint": its field stays empty.
                self._send(200, NATIVE.format(token="" if len(gets) == 1 else "fixture-token-not-real"))

            def do_POST(self):
                posts.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self._send(303, "", headers=[("Location", "/done")])

        from http.server import ThreadingHTTPServer
        import threading
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/form"
            deadline = time.monotonic() + 150
            durations = []

            async def attempt(n):
                started = time.monotonic()
                data = await asyncio.to_thread(
                    run_isolated_form, browser_engine, deadline=deadline, url=url,
                    fields=[{"selector": "#email", "value": "a@b.it"}], submit="#submit",
                    captcha_field="0-captcha", require_captcha_token=True,
                    completion_markers=[r"in revisione"], settle_ms=3000, wait_ms=0)
                durations.append(time.monotonic() - started)
                if form_retry.guard_blocked_zero_post(data):
                    raise form_retry.AttemptRefused("captcha_token_missing", trigger=True)
                return data, None

            result = asyncio.run(form_retry.run_attempts(attempt, retries=1, blocked=None,
                                                         deadline=deadline, floor_s=1.0))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(len(posts), 1)          # the tokenless POST never reached the server
        self.assertIn(b"0-captcha=fixture-token-not-real", posts[0])
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 1)
        self.assertEqual([a["error"] for a in result["attempts"]], ["captcha_token_missing", None])
        # The blocked attempt ended right after its click, not at the deadline.
        self.assertLess(durations[0], 60.0)


if __name__ == "__main__":
    unittest.main()


class ZeroPostThenNonTriggerTests(unittest.TestCase):
    """2026-10-03 batch 4: attempt 1 a guard block (trigger, zero POST),
    attempt 2 no_scoring_exit (non-trigger, zero POST) -> `final` was None
    and run_attempts crashed into a 500. No attempt answered, so the last
    refusal is the answer: its own 503 with every attempt listed."""

    def test_every_attempt_zero_post_raises_the_last_refusal(self):
        refusals = [form_retry.AttemptRefused("captcha_token_missing", trigger=True),
                    form_retry.AttemptRefused("no_scoring_exit", trigger=False)]

        async def attempt(n):
            raise refusals[n - 1]

        with self.assertRaises(form_retry.AttemptRefused) as caught:
            asyncio.run(form_retry.run_attempts(attempt, retries=3, blocked=None,
                                                deadline=time.monotonic() + 600, floor_s=0))
        self.assertEqual(caught.exception.error, "no_scoring_exit")
        self.assertEqual([(a["n"], a["error"], a["form_submissions"]) for a in caught.exception.attempts],
                         [(1, "captcha_token_missing", 0), (2, "no_scoring_exit", 0)])

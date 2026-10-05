"""retry_on_captcha_rejection: a fresh attempt after an explicit step-0
CAPTCHA refusal, and after nothing else.

Unit tests (no browser): the predicate, the attempt loop, and the endpoint
wiring (app.py imported with its dependencies stubbed). The opt-in browser
tests (FORM_BROWSER_TEST=chromium) drive the REAL flow against loopback
fixtures we own — no third-party form ever receives a submission.
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import form_retry
import test_form_stealth as _stealth  # installs the stubs (or reuses them) and imports app

app = _stealth.app

PATTERN = form_retry.compile_pattern(None)


def answered(*, error=None, ok=False, posts=1, status=200, captcha=True, same_url=True,
             at_post=1, wizard=True, rejected_after=1, errors=1, asn=3269):
    """One run's result as run_form shapes it."""
    diagnostics = {"egress": {"country": "Italy", "asn": asn}}
    if errors:
        diagnostics["rejection"] = {"at_post": at_post, "errors": errors, "captcha": captcha,
                                    "same_url": same_url, "wizard": wizard}
    if rejected_after is not None and error == "wizard_rejected":
        diagnostics["rejected_after_posts"] = rejected_after
    return {"contract_version": 2, "ok": ok, "error": error, "form_submissions": posts,
            "status": status, "url": "https://form.test/f", "html": "", "diagnostics": diagnostics}


REJECTED = dict(error="wizard_rejected")
DONE = dict(ok=True, status=302, errors=0, error=None)


# ── the predicate ───────────────────────────────────────────────────

class PredicateTests(unittest.TestCase):
    def test_the_step0_captcha_refusal_is_retryable(self):
        self.assertTrue(form_retry.is_captcha_rejection(answered(**REJECTED)))
        # The non-wizard equivalent: one 2xx POST, no error, captcha errors on the form.
        self.assertTrue(form_retry.is_captcha_rejection(answered(wizard=False)))

    def test_everything_else_is_never_retried(self):
        cases = {
            "non-captcha validation error": answered(**REJECTED, captcha=False),
            "a 302": answered(**REJECTED, status=302),
            "a 303 plain form": answered(wizard=False, status=303),
            "completion": answered(ok=True, error=None, errors=0),
            "later-step rejection": answered(error="wizard_rejected", rejected_after=2, at_post=2, posts=2),
            "two POSTs": answered(**REJECTED, posts=2),
            "zero POSTs": answered(**REJECTED, posts=0),
            "navigated away": answered(**REJECTED, same_url=False),
            "business-email gate left incomplete": answered(error="wizard_incomplete", errors=0),
            "unknown": answered(error="outcome_unknown", status=0),
            "wizard reset": answered(error="wizard_reset"),
            "no error nodes": answered(wizard=False, errors=0),
            "plain form with an error code": answered(wizard=False, error="no_submission"),
            "a 5xx": answered(**REJECTED, status=500),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertFalse(form_retry.is_captcha_rejection(data))
        guarded = answered(**REJECTED)
        guarded["diagnostics"]["captcha_guard_blocked"] = True
        self.assertFalse(form_retry.is_captcha_rejection(guarded))

    def test_every_error_node_must_be_the_captcha_error(self):
        record = form_retry.rejection_record
        for text in ("Error verifying reCAPTCHA, please try again.", "Captcha non valido",
                     "captcha invalid", "reCAPTCHA validation failed"):
            self.assertTrue(record([text], PATTERN, at_post=1, same_url=True, wizard=True)["captcha"], text)
        mixed = record(["Error verifying reCAPTCHA", "Enter a valid email address."], PATTERN,
                       at_post=1, same_url=True, wizard=True)
        self.assertEqual((mixed["errors"], mixed["captcha"]), (2, False))
        self.assertFalse(record([], PATTERN, at_post=1, same_url=True, wizard=True)["captcha"])
        self.assertFalse(record(["Use a business email"], PATTERN, at_post=1, same_url=True,
                                wizard=True)["captcha"])
        # The caller's own copy replaces the default.
        own = form_retry.compile_pattern(r"verifica non riuscita")
        self.assertTrue(record(["Verifica non riuscita"], own, at_post=1, same_url=True, wizard=False)["captcha"])
        self.assertFalse(record(["Error verifying reCAPTCHA"], own, at_post=1, same_url=True, wizard=False)["captcha"])
        # Counts and booleans only: never the text.
        self.assertNotIn("reCAPTCHA", repr(mixed))

    def test_same_page_ignores_only_the_fragment(self):
        self.assertTrue(form_retry.same_page("https://a/f?x=1#top", "https://a/f?x=1"))
        self.assertFalse(form_retry.same_page("https://a/f/complete/", "https://a/f"))
        self.assertFalse(form_retry.same_page("", ""))

    def test_pinned_identities_are_never_retried(self):
        reason = lambda **kw: form_retry.blocked_reason(**{"exit_session": None, "profile": None,
                                                          "sticky": False, "fresh_ip": True, **kw})
        self.assertIsNone(reason())
        self.assertEqual(reason(exit_session="tok"), "exit_pinned")
        self.assertEqual(reason(profile="p", sticky=True), "exit_pinned")
        self.assertEqual(reason(profile="p"), "profile")
        self.assertEqual(reason(fresh_ip=False), "exit_shared")


# ── the attempt loop ────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def loop(answers, *, retries=2, blocked=None, left_s=600.0, floor_s=100.0, cost_s=50.0):
    clock = _Clock()
    seen = []
    answers = list(answers)

    async def attempt(n):
        seen.append(n)
        clock.now += cost_s
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, {"chosen_score": 0.7 + n / 100, "asn": 1000 + n}

    result = asyncio.run(form_retry.run_attempts(
        attempt, retries=retries, blocked=blocked, deadline=clock.now + left_s,
        floor_s=floor_s, clock=clock))
    return result, seen


class LoopTests(unittest.TestCase):
    def test_a_captcha_refusal_then_acceptance(self):
        result, seen = loop([answered(**REJECTED), answered(**DONE)])
        self.assertEqual(seen, [1, 2])
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 2)  # each refused attempt WAS a POST
        self.assertEqual([a["n"] for a in result["attempts"]], [1, 2])
        self.assertEqual(result["attempts"][0], {
            "n": 1, "error": "wizard_rejected", "ok": False, "status": 200,
            "form_submissions": 1, "score_gate_score": 0.71, "asn": 1001})
        self.assertEqual(result["diagnostics"]["captcha_retry"],
                         {"retries": 2, "attempted": 2, "stopped": None})

    def test_retries_are_bounded(self):
        result, seen = loop([answered(**REJECTED)] * 3, retries=2)
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(result["form_submissions"], 3)
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], "retries_exhausted")

    def test_default_zero_keeps_the_single_attempt_contract(self):
        first = answered(**REJECTED)
        result, seen = loop([first], retries=0)
        self.assertEqual(seen, [1])
        self.assertIs(result, first)
        self.assertNotIn("attempts", result)

    def test_never_retried_answers_stop_after_one(self):
        for name, data in {"validation error": answered(**REJECTED, captcha=False),
                           "302": answered(**REJECTED, status=302),
                           "later step": answered(error="wizard_rejected", rejected_after=2, posts=2, at_post=2),
                           "completed": answered(**DONE)}.items():
            with self.subTest(name):
                result, seen = loop([data, answered(**DONE)])
                self.assertEqual(seen, [1])
                self.assertEqual(len(result["attempts"]), 1)
                self.assertIsNone(result["diagnostics"]["captcha_retry"]["stopped"])

    def test_the_deadline_must_hold_a_full_attempt(self):
        # 600 s budget, 50 s spent: a 600 s floor cannot fit.
        result, seen = loop([answered(**REJECTED), answered(**DONE)], floor_s=600.0)
        self.assertEqual(seen, [1])
        self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], "deadline")
        # The previous attempt's own duration counts too: 400 s spent, 200 s left.
        result, seen = loop([answered(**REJECTED), answered(**DONE)], floor_s=10.0, cost_s=400.0)
        self.assertEqual(seen, [1])
        self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], "deadline")
        # A callable floor is read after the attempt ran.
        result, seen = loop([answered(**REJECTED), answered(**DONE)], floor_s=lambda: 10.0)
        self.assertEqual(seen, [1, 2])

    def test_a_pinned_exit_is_reported_and_not_retried(self):
        result, seen = loop([answered(**REJECTED), answered(**DONE)], blocked="exit_pinned")
        self.assertEqual(seen, [1])
        self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], "exit_pinned")
        self.assertEqual(result["form_submissions"], 1)

    def test_a_zero_post_refusal_keeps_the_previous_answer(self):
        refused = form_retry.AttemptRefused("no_scoring_exit", gate_record={"chosen_score": None, "asn": None})
        result, seen = loop([answered(**REJECTED), refused])
        self.assertEqual(seen, [1, 2])
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertEqual(result["form_submissions"], 1)
        self.assertEqual(result["attempts"][1], {
            "n": 2, "error": "no_scoring_exit", "ok": False, "status": 503,
            "form_submissions": 0, "score_gate_score": None, "asn": None})
        self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], "refused")

    def test_an_unknown_retry_is_never_hidden(self):
        with self.assertRaises(form_retry.RetryUnknown) as caught:
            loop([answered(**REJECTED), RuntimeError("502")])
        self.assertEqual(caught.exception.form_submissions_before, 1)
        self.assertEqual([a["error"] for a in caught.exception.attempts],
                         ["wizard_rejected", "outcome_unknown"])
        self.assertIsNone(caught.exception.attempts[1]["form_submissions"])

    def test_attempt_one_failures_propagate_unchanged(self):
        with self.assertRaises(KeyError):
            loop([KeyError("first")])


# ── the endpoint wiring ─────────────────────────────────────────────

class _HTTPError(Exception):
    def __init__(self, status_code=None, detail=None):
        super().__init__(status_code)
        self.status_code, self.detail = status_code, detail


class _Field:
    def __init__(self, **kw):
        self.kw = kw

    def model_dump(self):
        return dict(self.kw)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"FORM_PROFILE_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def req(self, **kw):
        base = dict(url="https://form.test/f", fields=[_Field(selector="#e", value="x")], submit="#s",
                    dismiss=[], success_url=None, submission_urls=None, wait_until="domcontentloaded",
                    wait_ms=0, settle_ms=1000, timeout_ms=360_000, fresh_ip=True, exit_session=None,
                    gate_text="continua", step2=[], step2_submit=None, completion_markers=[],
                    profile=None, sticky_exit=None, score_gate=None, score_threshold=0.8,
                    score_gate_tries=3, oracle_url="https://tools.test/oracle/recaptcha",
                    headed=True, inspect_only=False, captcha_field=None, require_captcha_token=False,
                    ready_expression=None, stop_after_posts=None,
                    retry_on_captcha_rejection=2, captcha_rejection_text=None)
        base.update(kw)
        from types import SimpleNamespace
        return SimpleNamespace(**base)

    def submit(self, req, answers, floor=1.0):
        sessions = []
        answers = list(answers)

        async def run(req_, deadline, session, live):
            sessions.append(session)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            data = dict(answer, diagnostics=dict(answer["diagnostics"], score_gate={"chosen_score": 0.8, "asn": 1}))
            return data, {"session": "gated-" + session, "record": {"chosen_score": 0.8, "asn": 1}}, "gated-" + session

        with patch.object(app, "_form_attempt_run", run), patch.object(app, "HTTPException", _HTTPError), \
                patch.object(app, "FormSubmitResponse", lambda **kw: kw), \
                patch.object(app, "_attempt_floor_s", lambda req_, gate: floor):
            return asyncio.run(app.form_submit(req)), sessions

    def test_each_retry_is_a_fresh_exit_and_the_total_is_counted(self):
        result, sessions = self.submit(self.req(), [answered(**REJECTED), answered(**DONE)])
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(sessions[0], sessions[1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 2)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["exit_session"], "gated-" + sessions[1])  # the final attempt's exit

    def test_a_pinned_exit_or_profile_is_never_retried(self):
        for kw, reason in (({"exit_session": "mine"}, "exit_pinned"),
                           ({"profile": "p", "sticky_exit": True}, "exit_pinned"),
                           ({"profile": "p", "sticky_exit": False}, "profile"),
                           ({"fresh_ip": False}, "exit_shared")):
            with self.subTest(kw):
                result, sessions = self.submit(self.req(**kw), [answered(**REJECTED), answered(**DONE)])
                self.assertEqual(len(sessions), 1)
                self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], reason)
                self.assertEqual(result["error"], "wizard_rejected")

    def test_a_retry_that_cannot_fit_is_not_started(self):
        result, sessions = self.submit(self.req(timeout_ms=60_000), [answered(**REJECTED), answered(**DONE)],
                                       floor=305.0)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], "deadline")

    def test_a_zero_post_retry_failure_returns_the_real_answer(self):
        refusal = _HTTPError(503, {"retryable": True, "error": "no_scoring_exit", "form_submissions": 0,
                                   "score_gate": {"chosen_score": None, "asn": None}})
        # A zero-POST refusal is itself retried on a fresh exit; when every
        # retry after the real answer is zero-POST, that answer stands.
        result, _ = self.submit(self.req(retry_on_captcha_rejection=1), [answered(**REJECTED), refusal])
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertEqual(result["form_submissions"], 1)
        self.assertEqual(result["attempts"][1]["error"], "no_scoring_exit")

    def test_an_unknown_retry_is_a_502_naming_the_known_posts(self):
        with self.assertRaises(_HTTPError) as caught:
            self.submit(self.req(), [answered(**REJECTED), _HTTPError(502, "Form outcome unavailable")])
        self.assertEqual(caught.exception.status_code, 502)
        detail = caught.exception.detail
        self.assertEqual((detail["retryable"], detail["form_submissions_before"]), (False, 1))
        self.assertEqual(len(detail["attempts"]), 2)

    def test_attempt_one_errors_keep_their_shape(self):
        refusal = _HTTPError(503, {"retryable": True, "error": "no_scoring_exit"})
        with self.assertRaises(_HTTPError) as caught:
            self.submit(self.req(retry_on_captcha_rejection=0), [refusal])
        self.assertIs(caught.exception, refusal)

    def test_a_zero_post_first_attempt_is_retried_on_a_fresh_exit(self):
        # 2026-10-04: navigation_failed (0 POSTs) ended a call that a fresh
        # exit would have completed. Any provably zero-POST failure retries.
        refusal = _HTTPError(503, {"retryable": True, "error": "navigation_failed", "form_submissions": 0})
        result, sessions = self.submit(self.req(retry_on_captcha_rejection=1), [refusal, answered(**DONE)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 1)
        self.assertEqual([a["error"] for a in result["attempts"]], ["navigation_failed", None])

    def test_a_long_deadline_needs_the_retry_option_and_a_bad_pattern_is_a_400(self):
        with self.assertRaises(_HTTPError) as caught:
            self.submit(self.req(timeout_ms=500_000, retry_on_captcha_rejection=0), [])
        self.assertEqual(caught.exception.status_code, 400)
        result, _ = self.submit(self.req(timeout_ms=500_000), [answered(**DONE)])
        self.assertTrue(result["ok"])
        with self.assertRaises(_HTTPError) as caught:
            self.submit(self.req(captcha_rejection_text="(unclosed"), [])
        self.assertEqual(caught.exception.status_code, 400)

    def test_the_floor_is_a_full_attempt(self):
        gated = {"record": {"chosen_score": 0.8}}
        self.assertEqual(app._attempt_floor_s(self.req(), gated), 305.0)       # wizard + one candidate
        self.assertEqual(app._attempt_floor_s(self.req(), {"record": {"skipped": "no_oracle"}}), 240.0)
        self.assertEqual(app._attempt_floor_s(self.req(gate_text=None), None), 100.0)


# ── real browser, loopback fixtures ─────────────────────────────────

FORM = '''<!doctype html><title>f</title>
<form method="post" action="{action}">{errors}
  <input id="email" name="0-email" required>
  <button id="submit" type="submit">Invia</button>
</form>'''
STEP2 = '''<!doctype html><title>f</title>
<form method="post" action="{action}">{errors}
  <input id="phone" name="1-phone" required>
  <button id="next" type="submit">Avanti</button>
</form>'''
CAPTCHA_ERROR = '<ul class="errorlist"><li>Error verifying reCAPTCHA, please try again.</li></ul>'
EMAIL_ERROR = '<ul class="errorlist"><li>Enter a valid email address.</li></ul>'


@contextmanager
def fixture(answer):
    """A loopback form; `answer(post_n, body)` returns (status, headers, html)."""
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, status, headers, html):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def do_GET(self):
            if self.path.startswith("/done"):
                self._send(200, {}, "<p>Grazie</p>")
            else:
                self._send(200, {}, FORM.format(action="/form", errors=""))

        def do_POST(self):
            posts.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._send(*answer(len(posts), posts[-1]))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/form", posts
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST"), "opt-in browser fixture")
class BrowserRetryTests(unittest.TestCase):
    def run_flow(self, url, *, retries=2, blocked=None, floor_s=1.0, budget_s=150.0, **params):
        from form_worker import run_isolated_form
        from test_form_browser import browser_engine
        deadline = time.monotonic() + budget_s
        contexts = []

        def factory():
            contexts.append(1)  # one launch = one new isolated browser context
            return browser_engine()

        async def attempt(n):
            data = await asyncio.to_thread(
                run_isolated_form, factory, deadline=deadline, url=url,
                fields=[{"selector": "#email", "value": "a@b.it"}], submit="#submit",
                settle_ms=3000, wait_ms=0, **params)
            return data, None

        result = asyncio.run(form_retry.run_attempts(attempt, retries=retries, blocked=blocked,
                                                     deadline=deadline, floor_s=floor_s))
        return result, len(contexts)

    def test_plain_form_refused_with_a_captcha_error_then_accepted(self):
        def answer(n, _body):
            if n == 1:
                return 200, {}, FORM.format(action="/form", errors=CAPTCHA_ERROR)
            return 303, {"Location": "/done"}, ""

        with fixture(answer) as (url, posts):
            result, launches = self.run_flow(url, success_url=r"/done$")
        self.assertEqual(len(posts), 2)
        self.assertEqual(launches, 2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["form_submissions"], 2)
        self.assertEqual([a["error"] for a in result["attempts"]], [None, None])
        self.assertEqual([a["status"] for a in result["attempts"]], [200, 303])

    def test_wizard_step0_captcha_refusal_then_completion(self):
        def answer(n, _body):
            if n == 1:
                return 200, {}, FORM.format(action="/form", errors=CAPTCHA_ERROR)
            return 200, {}, "<p>La tua richiesta e' in revisione</p>"

        with fixture(answer) as (url, posts):
            result, _ = self.run_flow(url, completion_markers=[r"in revisione"])
        self.assertEqual(len(posts), 2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"][0]["error"], "wizard_rejected")
        self.assertEqual(result["form_submissions"], 2)

    def test_a_non_captcha_validation_error_is_not_retried(self):
        def answer(n, _body):
            return 200, {}, FORM.format(action="/form", errors=EMAIL_ERROR)

        with fixture(answer) as (url, posts):
            result, _ = self.run_flow(url, completion_markers=[r"in revisione"])
        self.assertEqual(len(posts), 1)
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertFalse(result["diagnostics"]["rejection"]["captcha"])
        self.assertEqual(len(result["attempts"]), 1)

    def test_a_302_is_not_retried(self):
        def answer(n, _body):
            return 302, {"Location": "/done"}, ""

        with fixture(answer) as (url, posts):
            result, _ = self.run_flow(url, success_url=r"/never$")
        self.assertEqual(len(posts), 1)
        self.assertEqual(result["status"], 302)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["attempts"]), 1)

    def test_a_later_step_captcha_rejection_is_not_retried(self):
        def answer(n, _body):
            if n == 1:
                return 200, {}, STEP2.format(action="/form", errors="")
            return 200, {}, STEP2.format(action="/form", errors=CAPTCHA_ERROR)

        with fixture(answer) as (url, posts):
            result, _ = self.run_flow(url, step2=[{"selector": "#phone", "value": "3331234567"}],
                                      step2_submit="#next", completion_markers=[r"in revisione"])
        self.assertEqual(len(posts), 2)
        self.assertEqual(result["error"], "wizard_rejected")
        self.assertEqual(result["diagnostics"]["rejected_after_posts"], 2)
        self.assertEqual(len(result["attempts"]), 1)

    def test_the_deadline_and_a_pinned_exit_stop_the_retry(self):
        def answer(n, _body):
            return 200, {}, FORM.format(action="/form", errors=CAPTCHA_ERROR)

        for kw, stopped in (({"floor_s": 10_000.0}, "deadline"), ({"blocked": "exit_pinned"}, "exit_pinned")):
            with self.subTest(stopped), fixture(answer) as (url, posts):
                result, launches = self.run_flow(url, completion_markers=[r"in revisione"], **kw)
                self.assertEqual((len(posts), launches), (1, 1))
                self.assertEqual(result["diagnostics"]["captcha_retry"]["stopped"], stopped)
                self.assertEqual(result["form_submissions"], 1)


if __name__ == "__main__":
    unittest.main()


class RetryGateTests(unittest.TestCase):
    """Attempt 1 ungated; a retry is gated at FORM_RETRY_GATE unless the caller chose."""

    def req(self, **kw):
        from types import SimpleNamespace
        base = dict(url="https://form.test/f", score_gate=None, score_threshold=0.7)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_only_retries_are_gated(self):
        req = self.req()
        self.assertIs(app._retry_request(req, 1), req)
        retry = app._retry_request(req, 2)
        self.assertTrue(retry.score_gate)
        self.assertEqual(retry.score_threshold, 0.9)

    def test_an_explicit_caller_choice_wins(self):
        for choice in (True, False):
            req = self.req(score_gate=choice)
            self.assertIs(app._retry_request(req, 3), req)

    def test_the_env_can_disable_or_raise_it(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"FORM_RETRY_GATE": "0"}):
            self.assertIsNone(app._retry_request(self.req(), 2).score_gate)
        with patch.dict(os.environ, {"FORM_RETRY_GATE": "0.8"}):
            self.assertEqual(app._retry_request(self.req(score_threshold=0.95), 2).score_threshold, 0.95)

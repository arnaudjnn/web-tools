"""`retry_on_captcha_rejection`: fresh attempts after an explicit step-0
CAPTCHA refusal, and nothing else.

Measured 2026-10-03 (Atoka batch 1, 10 runs, headed, score-gated at 0.7):
5 completed, 4 were refused AT STEP 0 with the form re-rendered and
"Error verifying reCAPTCHA" on it, 1 found no scoring exit. Passing and
refused exits scored the same on our oracle (0.7-0.9 vs 0.7-0.8): the
target scores per site, so the gate cannot pick the winner in advance.

A step-0 CAPTCHA refusal is the server saying no to the FIRST POST and
showing the same form again: nothing was created, so a fresh attempt is
safe. Every condition below must hold — anything else (unknown, a 3xx,
completion, a non-CAPTCHA validation error, the business-email gate, a
later-step rejection) is never retried:

- the run's error is the explicit first-POST rejection shape —
  `wizard_rejected` with `rejected_after_posts == 1`, or on a plain form
  `ok:false` with no error after one answered POST;
- EVERY error node on the re-rendered form matches the CAPTCHA pattern
  (`captcha_rejection_text`, default `DEFAULT_CAPTCHA_REJECTION`), so a
  CAPTCHA error next to a field error is not a CAPTCHA-only refusal;
- exactly one POST left the browser, it was answered 2xx, and the page is
  still on the URL it was submitted from (the form re-rendered).

The one other trigger is a token-guard block (`guard_blocked_zero_post`):
the page POSTed without a token and require_captcha_token aborted it —
nothing was sent, and the page could not mint (the same family).

Each retry is a NEW attempt: a new isolated browser context on a new exit
(fresh proxy session token), score-gated again when the first one was. A
pinned exit (`exit_session`, a sticky profile, `fresh_ip:false`) or a named
profile is never retried — the same identity gets the same verdict. A retry
starts only when the deadline still holds a full attempt.

The flow (form_flow.run_form) records the evidence in
`diagnostics.rejection = {at_post, errors, captcha, same_url, wizard}` —
counts and booleans, never the error text itself.
"""
import re
import time

MAX_RETRIES = 4
DEFAULT_CAPTCHA_REJECTION = r"error verifying recaptcha|captcha (?:non |in)?valid|recaptcha"
# The nodes a rejection renders into (django `.errorlist`, bootstrap
# `.invalid-feedback`, the site's own `.error-msg`). Only these are read.
ERROR_NODES = ".error-msg,.invalid-feedback,.errorlist"


def compile_pattern(text):
    """The caller's regex (case-insensitive), or the default. Raises re.error."""
    return re.compile(text or DEFAULT_CAPTCHA_REJECTION, re.I)


def rejection_record(errors, pattern, *, at_post, same_url, wizard):
    """What the re-rendered form said, as counts and booleans (no text)."""
    errors = [e for e in (errors or []) if e]
    return {"at_post": at_post, "errors": len(errors),
            "captcha": bool(errors) and all(pattern.search(e) for e in errors),
            "same_url": bool(same_url), "wizard": bool(wizard)}


def same_page(a, b):
    """Same URL, ignoring only the fragment."""
    strip = lambda u: (u or "").split("#", 1)[0]
    return bool(a) and strip(a) == strip(b)


def is_captcha_rejection(data):
    """True only for the explicit step-0 CAPTCHA refusal (module docstring)."""
    diagnostics = data.get("diagnostics") or {}
    rejection = diagnostics.get("rejection") or {}
    status = data.get("status") or 0
    if data.get("ok") or data.get("form_submissions") != 1 or not 200 <= status < 300:
        return False
    if not (rejection.get("captcha") and rejection.get("same_url")) or rejection.get("at_post") != 1:
        return False
    if diagnostics.get("captcha_guard_blocked"):
        return False
    if rejection.get("wizard"):
        return (data.get("error") == "wizard_rejected"
                and diagnostics.get("rejected_after_posts") == 1)
    return data.get("error") is None


def guard_blocked_zero_post(data):
    """The token guard aborted the page's tokenless POST: after the click,
    yet provably zero-POST (the guard aborts the only matching request and
    every later one). The page could not mint — the same family as a
    CAPTCHA refusal, so a retry trigger, and a 503 retryable on its own."""
    diagnostics = data.get("diagnostics") or {}
    return bool(data.get("error") == "captcha_token_missing"
                and data.get("form_submissions") == 0
                and diagnostics.get("captcha_guard_blocked"))


def blocked_reason(*, exit_session, profile, sticky, fresh_ip):
    """Why this request's identity cannot be retried on a fresh one, or None."""
    if exit_session:
        return "exit_pinned"
    if profile and sticky:
        return "exit_pinned"
    if profile:
        return "profile"
    if fresh_ip is False:
        return "exit_shared"
    return None


def attempt_entry(n, data, gate_record=None):
    gate_record = gate_record or {}
    egress = (data.get("diagnostics") or {}).get("egress") or {}
    return {"n": n, "error": data.get("error"), "ok": bool(data.get("ok")),
            "status": data.get("status", 0), "form_submissions": data.get("form_submissions", 0),
            "score_gate_score": gate_record.get("chosen_score"),
            "asn": gate_record.get("asn") or (egress.get("asn") if isinstance(egress, dict) else None)}


class AttemptRefused(Exception):
    """An attempt that provably never reached the target (zero POSTs).

    `trigger` False (on a retry: the gate found no scoring exit, a pre-click
    failure) ends the loop with the previous answer. `trigger` True is the
    token guard's block (`captcha_token_missing`: the page's handler POSTed
    without a token, the guard aborted it) — the same family as a CAPTCHA
    refusal (the page could not mint), so it is itself a retry trigger, on
    any attempt including the first. `cause` is the endpoint's own 503,
    re-raised (with `attempts`) when no attempt ever produced an answer.
    """

    def __init__(self, error, status=503, gate_record=None, trigger=False, cause=None):
        super().__init__(error)
        self.error, self.status, self.gate_record = error, status, gate_record or {}
        self.trigger, self.cause = trigger, cause
        self.attempts = []


class RetryUnknown(Exception):
    """A retry whose outcome is unknown (a 502, a lost run): the earlier
    attempts' POSTs are known, this one's are not. Never replayed."""

    def __init__(self, attempts, form_submissions_before, cause):
        super().__init__(type(cause).__name__)
        self.attempts, self.form_submissions_before, self.cause = (
            attempts, form_submissions_before, cause)


async def run_attempts(attempt, *, retries, blocked, deadline, floor_s, clock=time.monotonic):
    """Run `attempt(n) -> (data, gate_record)` once, then again while the
    answer is a step-0 CAPTCHA refusal (or a guard-blocked, tokenless
    POST) and a retry is allowed.

    Attempt 1's exceptions propagate unchanged (the single-attempt contract),
    except a trigger `AttemptRefused`. On a retry, a non-trigger
    `AttemptRefused` ends the loop with the previous answer; anything else
    becomes `RetryUnknown`. A retry needs `max(floor_s, the previous
    attempt's duration)` of deadline left. When every attempt was a
    zero-POST trigger, the last `AttemptRefused` is raised (with
    `.attempts`) so the endpoint answers its own 503.
    """
    attempts = []
    total = 0
    final = None
    stopped = None
    n = 1

    def why_stop(duration):
        if n > retries:
            return "retries_exhausted"
        if blocked:
            return blocked
        floor = floor_s() if callable(floor_s) else floor_s
        if deadline - clock() < max(floor, duration):
            return "deadline"
        return None

    while True:
        started = clock()
        try:
            data, gate_record = await attempt(n)
        except AttemptRefused as refused:
            attempts.append({"n": n, "error": refused.error, "ok": False,
                             "status": refused.status, "form_submissions": 0,
                             "score_gate_score": refused.gate_record.get("chosen_score"),
                             "asn": refused.gate_record.get("asn")})
            if not refused.trigger:
                if n == 1 or final is None:
                    # No attempt produced a real answer (every one was zero
                    # POST): this refusal IS the answer — its own 503.
                    refused.attempts = attempts
                    raise
                stopped = "refused"
                break
            stopped = why_stop(clock() - started)
            if stopped is None:
                n += 1
                continue
            if final is None:
                refused.attempts = attempts
                raise
            break
        except Exception as error:
            if n == 1:
                raise
            attempts.append({"n": n, "error": "outcome_unknown", "ok": False, "status": 0,
                             "form_submissions": None, "score_gate_score": None, "asn": None})
            raise RetryUnknown(attempts, total, error) from error
        duration = clock() - started
        attempts.append(attempt_entry(n, data, gate_record))
        total += data.get("form_submissions") or 0
        final = data
        if not is_captcha_rejection(data):
            stopped = None
            break
        stopped = why_stop(duration)
        if stopped is not None:
            break
        n += 1
    if retries <= 0:
        return final
    final = dict(final)
    final["form_submissions"] = total
    final["attempts"] = attempts
    diagnostics = final.setdefault("diagnostics", {})
    diagnostics["captcha_retry"] = {"retries": retries, "attempted": len(attempts),
                                    "stopped": stopped}
    return final

"""Generic single-attempt form execution. No target-specific logic.

No CAPTCHA is ever minted here. The page's own submit handler supplies any
token (the site's recaptcha integration runs on interaction); the caller may
only OBSERVE — `require_captcha_token` aborts the POST before it leaves when
the response field has no readable token, and `captcha_field` names the field
to inspect (presence only, never the value). Provider-side solving (CapSolver)
was removed 2026-09-29: measured on the form's own egress it added nothing —
page-minted tokens passed whenever any token passed — and it was the last
third-party credential in this service.

The caller owns durable reservations. A lost HTTP response is UNKNOWN and must
not be retried automatically. The context guard only prevents duplicate POSTs
within this operation; it is not cross-request idempotency.

HUMAN INPUT. Text fields are filled the way a person does — move the pointer
to the field, click it, type it one keystroke at a time — never with a single
fill() assignment. reCAPTCHA v3 scores BEHAVIOUR alongside IP reputation, and
a form completed in a few hundred milliseconds with no pointer movement, no
keystrokes and no dwell is a textbook automation signature: measured
2026-09-26, instant fill scored 0 passes across headless AND headed browsers
on fresh residential exits, while keystroke-by-keystroke input passed on the
same class of exit. Playwright's mouse/keyboard produce TRUSTED events
(isTrusted: true), which synthetic JS events cannot — so this is real
interaction, not a spoof. The ~one minute this costs per form is the price of
the score, not waste.
"""
import contextlib
import hashlib
import json
import logging
import random
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs

# Phase logs are the ONLY visibility into a flow that never returns: the
# wedge handler sees a stackless greenlet and can only say "somewhere".
log = logging.getLogger("camoufox.forms")

PRE_SUBMIT_STUCK_S = 8.0
PRE_SUBMIT_STEP = "arrival scroll"
# One humanized pointer trajectory: camoufox animates it browser-side and
# caps it at humanize's maxTime (1.5s default). 10s is the legitimate worst
# case with a wide margin; a call older than that never returned.
POINTER_MOVE_STUCK_S = 10.0
# Navigation: a transient refusal is retried in-run (pre-POST, a GET only).
NAV_ATTEMPTS = 3
NAV_BACKOFF_S = (2.0, 4.0)
# Per goto. Every successful oracle navigation on cf156 reached
# domcontentloaded within ~10s; the one TimeoutError (2026-10-02 15:38:47)
# sat the full 60s on a stalled exit and left no budget to retry. 30s is 3x
# the worst success, and a timed-out GET is retried in-run (pre-input) when
# at least NAV_RETRY_MIN_LEFT_S of the deadline remains for the form itself.
NAV_TIMEOUT_MS = 30000
NAV_RETRY_MIN_LEFT_S = 60.0
# 'new page' parks (2026-10-02 15:36:33, 15:43:49): both on PERSISTENT
# profiles, whose launch already owns a blank tab; context.new_page() opening
# a second one never returned. Isolated contexts start with no page and never
# parked there (0/41). Reuse the launch's own tab when there is one.
NEW_PAGE_STUCK_S = 8.0
# reCAPTCHA readiness, pre-input. A page that requested a reCAPTCHA script
# must have a usable client before anything is typed: on cf156, 5/33 POSTs
# carried NO token, every one with captcha_scripts [2,2,1] and an oracle
# verdict with no t_submit (page_dwell_s null, mint_error null) — the
# page's submit listener, attached inside grecaptcha.ready(), never ran, so
# the click fell through to a NATIVE submit with an empty field. Polled for
# CAPTCHA_READY_WAIT_S; unusable then → ONE reload (no field touched, no
# POST possible); still unusable → captcha_unavailable, zero POSTs, 503.
CAPTCHA_READY_WAIT_S = 8.0
CAPTCHA_READY_POLL_MS = 250
# Main-world probe (Camoufox `mw:`; a JS label elsewhere). The api.js stub
# defines only grecaptcha.ready — execute() arrives with the recaptcha__*.js
# library, so its presence is the "library initialised" signal. Without a
# main world, `wrappedJSObject` (Firefox xray waiver) is tried; anything
# else reads false and the frame/network signals below decide.
GRECAPTCHA_USABLE_JS = (
    "(() => { const w = window.wrappedJSObject || window; const g = w.grecaptcha;"
    " return !!g && (typeof g.execute === 'function' ||"
    " !!(g.enterprise && typeof g.enterprise.execute === 'function')); })()")
_ANCHOR_PATH = re.compile(r"/recaptcha/(?:api2|enterprise)/anchor\b")
# The readiness gate's own bound. Every passing run met it within
# milliseconds of the fields finishing; every failing one (7 on 2026-10-01)
# polled until the whole deadline expired (~2 min) — a condition still
# false after this long will not turn true, and the run is retryable anyway.
READY_WAIT_S = 30.0


# The live object's own clock: tests drive the form DEADLINE through a fake
# `time.monotonic`, and a summary/mark must never consume or follow it.
_clock = time.monotonic


class FormLive:
    """Per-job live state: the pre-POST step mark and the run summary.

    The job thread writes it; the admission worker polls it from the event
    loop. One object per job — a previous job's mark can never age into
    the next one's poll, and nothing is module-global.

    The MARK. A pre-POST driver roundtrip has no timeout we can trust:
    playwright's own `timeout=` is enforced by the driver's loop, and a
    wedged transport (the greenlet parked on a socket read) never reaches it
    — measured 2026-09-29: the fields phase sat in the driver's `select` for
    minutes past every timeout. So EVERY driver call before the submit click
    is marked around with a threshold ABOVE its own legitimate worst case
    (its timeout, or the keystroke duration for `type`): a mark older than
    its threshold means the call never returned, so no field was touched
    and no POST left the machine — retryable, unlike every other stall.
    Marks are strictly pre-POST; the submit click onwards is never marked
    (a click may already have POSTed).

    The SUMMARY. One `form-run {json}` line per job, whatever happened —
    returned, parked, wedged, never admitted. Counts, booleans, class
    names, phase names, durations, selectors: NEVER a field value, a token,
    a body or an exception message. It is the measurement source.
    """

    def __init__(self, profile=None, headed=None, camoufox=None):
        # (name, at, stuck) swapped as one tuple: the poller never reads a
        # half-written mark.
        self._step = ("", 0.0, PRE_SUBMIT_STUCK_S)
        self._lock = threading.Lock()
        self._emitted = False
        self.started = _clock()
        self.meta = {"profile": profile, "headed": headed, "camoufox": camoufox}
        self.phase = None
        self.reached = None  # furthest flow phase (teardown is not one)
        self._phase_at = None
        self.durations = {}
        self.extra = {}
        self.result = None

    # -- the mark --------------------------------------------------------
    def mark(self, name, stuck_s=PRE_SUBMIT_STUCK_S):
        self._step = (name, _clock(), float(stuck_s))

    def clear(self):
        self._step = ("", 0.0, PRE_SUBMIT_STUCK_S)

    @property
    def step(self):
        return self._step[0]

    def hang_step(self):
        """The mark name if the flow parked on a pre-POST driver call."""
        name, at, stuck = self._step
        if name and _clock() - at > stuck:
            return name
        return None

    @contextlib.contextmanager
    def at(self, name, stuck_s=PRE_SUBMIT_STUCK_S):
        """Mark one driver call; always clear it, even when the call raises."""
        self.mark(name, stuck_s)
        try:
            yield
        finally:
            self.clear()

    # -- the summary -----------------------------------------------------
    def enter(self, phase):
        """Close the running phase's duration and open `phase`."""
        now = _clock()
        if self.phase is not None and self._phase_at is not None:
            self.durations[self.phase] = round(
                self.durations.get(self.phase, 0.0) + now - self._phase_at, 1)
        self.phase, self._phase_at = phase, now
        if phase != "teardown":
            self.reached = phase

    def note(self, **values):
        self.extra.update(values)

    def summary(self, result=None, **override):
        result = result if result is not None else (self.result or {})
        diagnostics = result.get("diagnostics") or {}
        durations = dict(self.durations)
        if self.phase is not None and self._phase_at is not None:
            durations[self.phase] = round(
                durations.get(self.phase, 0.0) + _clock() - self._phase_at, 1)
        egress = diagnostics.get("egress") or None
        posts = [{"n": p.get("n"), "token": p.get("token"), "mint_age_s": p.get("mint_age_s"),
                  "same_as_first": p.get("same_as_first"), "reloads": p.get("reloads")}
                 for p in list(diagnostics.get("submission_tokens") or [])]
        out = {
            "error": result.get("error"),
            "ok": bool(result.get("ok")),
            "form_submissions": result.get("form_submissions", 0),
            "status": result.get("status", 0),
            "phase": self.reached,
            "failed_phase": diagnostics.get("phase"),
            "failure_class": diagnostics.get("failure_class"),
            "parked_step": None,
            "field": diagnostics.get("field_attempt"),
            "durations_s": durations,
            "total_s": round(_clock() - self.started, 1),
            "posts": posts,
            "rejected_after_posts": diagnostics.get("rejected_after_posts"),
            "token_present": diagnostics.get("token_present"),
            "egress": ({"country": egress.get("country"), "asn": egress.get("asn")}
                       if isinstance(egress, dict) else None),
            "nav_attempts": diagnostics.get("nav_attempts"),
            "nav_error": diagnostics.get("nav_error"),
            "ready_met": diagnostics.get("ready_condition_met"),
            "captcha_reload": diagnostics.get("captcha_script_reload"),
            "captcha_ready": diagnostics.get("captcha_ready"),
            "captcha_signal": diagnostics.get("captcha_signal"),
            "captcha_failed": diagnostics.get("captcha_failed") or None,
            "page_reused": diagnostics.get("page_reused"),
            "captcha_scripts": [diagnostics.get("captcha_script_requests"),
                                diagnostics.get("captcha_script_responses"),
                                diagnostics.get("captcha_network_failures")],
            "submit_clicked": diagnostics.get("submit_click_attempted"),
            "inspect_only": diagnostics.get("inspection_only"),
            **self.meta,
            **self.extra,
        }
        out.update(override)
        return out

    def emit(self, result=None, **override):
        """Log the summary ONCE per job; later calls are no-ops."""
        with self._lock:
            if self._emitted:
                return False
            self._emitted = True
        try:
            line = json.dumps(self.summary(result, **override), separators=(",", ":"),
                              default=str)
        except Exception as error:  # a dict mutated mid-dump by a live thread
            line = json.dumps({"error": override.get("error"), "summary_failed": type(error).__name__})
        log.info("form-run %s", line)
        return True


def _unmarked(name, stuck_s=PRE_SUBMIT_STUCK_S):
    """A post-submission driver call: NO retryable mark.

    The mark's whole promise is "no POST left this machine". The wizard's
    gate and step2 clicks happen AFTER the step0 POST, so a park on one of
    them must surface as an unknown outcome (never replayed), not as the
    503-safe-to-retry contract.
    """
    return contextlib.nullcontext()


def validate_form(url, submission_urls, success_url, gate_text=None, completion_markers=None):
    origin = urlsplit(url)
    if origin.scheme not in ("http", "https") or not origin.hostname or origin.username or origin.password:
        raise ValueError("Invalid form URL")
    urls = submission_urls or [url]
    for target in urls:
        parsed = urlsplit(target)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("Submission URLs must share the form origin")
    if success_url:
        re.compile(success_url)
    if gate_text:
        re.compile(gate_text, re.I)
    for marker in completion_markers or []:
        re.compile(marker, re.I)
    return {urlsplit(value)._replace(query="", fragment="").geturl() for value in urls}


_NO_TOKEN = ("", "null", "undefined", "false")


def _token_shape(request, captcha_field, salt=None):
    """A POST body's captcha SHAPE — presence and lengths, never a value.

    keep_blank_values: an absent key means "not in the form", [''] means
    "carried but empty" — the distinction between a server reading the
    custom field (minted) and the standard one (which the page may leave
    blank). `present` is the configured field alone (the guard's input);
    `any` is either watched field (the per-POST record). None when the body
    cannot be read at all.

    With `salt`, `digest` identifies the carried token(s) so a later POST can
    say whether it re-sent the FIRST POST's token (a stale re-send) or a new
    one (the page minted on that click) — gate re-verify forensics. The
    digest is per-run salted and stays in the caller's memory; only the
    resulting boolean is ever recorded.
    """
    try:
        values = parse_qs(request.post_data or "", keep_blank_values=True)
    except Exception:  # an unreadable body (or none to read): unknown, not absent
        return None
    watch = sorted({n for n in (captcha_field, "g-recaptcha-response") if n})

    def carried(name):
        found = values.get(name, [])
        return len(found) == 1 and found[0].strip().lower() not in _NO_TOKEN

    tokens = [values[name][0] for name in watch if carried(name)]
    digest = (hashlib.sha256((salt + "\x00".join(tokens)).encode()).hexdigest()
              if salt and tokens else None)
    return {"present": carried(captcha_field or "g-recaptcha-response"),
            "any": any(carried(name) for name in watch),
            "lengths": {name: [len(v) for v in values.get(name, [])] for name in watch},
            "digest": digest}


# Navigation failures by CODE — the engine's error token, never its message
# (which carries the URL). Measured 2026-10-01: 18 navigation_failed with
# class Error, each ~60-120ms after the page opened (the refusal is
# immediate) and the next attempt of the same identity seconds later
# navigated fine; 7 with TargetClosedError the same way. Both are retried
# in-run: navigation is a GET, nothing has been touched, no POST can leave.
_NAV_CODE = re.compile(r"\b(NS_ERROR_[A-Z_]+|NS_BINDING_[A-Z_]+|net::ERR_[A-Z_]+)")
_NAV_RETRY = ("NS_ERROR_CONNECTION_REFUSED", "NS_ERROR_PROXY_CONNECTION_REFUSED",
              "NS_ERROR_NET_RESET", "NS_ERROR_NET_INTERRUPT", "NS_ERROR_NET_TIMEOUT",
              "NS_ERROR_PROXY_BAD_GATEWAY", "NS_ERROR_PROXY_GATEWAY_TIMEOUT",
              "NS_ERROR_UNKNOWN_HOST", "NS_ERROR_UNKNOWN_PROXY_HOST", "NS_ERROR_ABORT",
              "NS_BINDING_ABORTED", "net::ERR_CONNECTION_REFUSED",
              "net::ERR_PROXY_CONNECTION_FAILED", "net::ERR_CONNECTION_RESET",
              "net::ERR_TUNNEL_CONNECTION_FAILED", "net::ERR_ABORTED",
              "interrupted", "target_closed", "new_page_failed")


class CaptchaUnavailable(Exception):
    """reCAPTCHA never became usable, even after one pre-input reload."""


# reCAPTCHA request PATH CLASSES — which piece failed, never a query string
# (sitekeys, tokens and versions live there and in the release path).
_CAPTCHA_CLASSES = (
    (re.compile(r"/recaptcha/api\.js$"), "api.js"),
    (re.compile(r"/recaptcha/enterprise\.js$"), "enterprise.js"),
    (re.compile(r"/recaptcha/releases/[^/]+/recaptcha__[^/]*\.js$"), "recaptcha__*.js"),
    (re.compile(r"/recaptcha/releases/[^/]+/styles__[^/]*\.css$"), "styles__*.css"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/anchor$"), "anchor"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/bframe$"), "bframe"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/reload$"), "reload"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/clr$"), "clr"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/userverify$"), "userverify"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/webworker\.js$"), "webworker.js"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/payload$"), "payload"),
)
_FAILURE_TOKEN = re.compile(r"^[A-Za-z_:]{1,48}$")


def captcha_path_class(url):
    """The loggable class of a reCAPTCHA URL (path only, normalised)."""
    path = urlsplit(url or "").path
    for pattern, name in _CAPTCHA_CLASSES:
        if pattern.search(path):
            return name
    return "other"


def _failure_code(text):
    """An engine error token from request.failure (never a message/URL)."""
    text = str(text or "")
    found = _NAV_CODE.search(text)
    if found:
        return found.group(1)
    text = text.strip()
    return text if _FAILURE_TOKEN.match(text) else ("unknown" if not text else "other")


def first_page(context, live=None):
    """The page a form job drives: the launch's own tab when it has one.

    A persistent context (`profile`) launches INTO a blank tab; asking it
    for a second one is what parked twice on cf156 (see NEW_PAGE_STUCK_S).
    An isolated context has none, and gets a fresh page as before. Returns
    (page, reused).
    """
    try:
        pages = list(context.pages) if isinstance(context.pages, (list, tuple)) else []
    except Exception:
        pages = []
    for candidate in pages:
        try:
            if not candidate.is_closed():
                return candidate, True
        except Exception:
            continue
    at = live.at if live is not None else _unmarked
    with at("new page", NEW_PAGE_STUCK_S):
        return context.new_page(), False


def _nav_code(error):
    """A loggable token for a navigation failure (no URL, no message)."""
    text = str(error)
    found = _NAV_CODE.search(text)
    if found:
        return found.group(1)
    if "interrupted by another navigation" in text:
        return "interrupted"
    if type(error).__name__ == "TargetClosedError" or "has been closed" in text:
        return "target_closed"
    if "Timeout" in type(error).__name__ or "Timeout" in text:
        return "timeout"
    return type(error).__name__


def _click_target(box):
    """An off-centre point inside `box`, never on a viewport axis.

    Off-centre: an identical dead-centre click on every control is its own
    pattern. Never x<=1 or y<=1: a trajectory point on x==0/y==0 never gets
    a renderer ack in camoufox and deadlocks the whole input chain
    (daijro/camoufox#751, fixed only from 156.0.1-beta.32; kept as defence) — the old
    approach START (target minus 100-400px / 60-200px) went off-screen for
    any field near the left or top edge and was clamped onto exactly that
    axis.
    """
    w, h = box["width"], box["height"]
    tx = box["x"] + w / 2 + random.uniform(-w / 4, w / 4)
    ty = box["y"] + h / 2 + random.uniform(-min(4.0, h / 4), min(4.0, h / 4))
    return max(2.0, tx), max(2.0, ty)


def human_click(page, control, remaining, pre_submit=True, live=None) -> None:
    """Click a control the way a pointer does: bring it into view instantly,
    move to it, land off-centre, press and release. A synthetic .click()
    with no pointer ever moving is an automation signature.

    ONE mouse.move, not a hand-stepped approach. The form browser launches
    with humanize=True (app.py `_form_browser`), so camoufox already draws
    a human trajectory for every dispatched move — browser-side, each step
    re-animated (~0.75s each, measured 2026-09-29: steps=11 took 8.7s). The
    6-18 manual steps were double humanization: a jagged path of
    trajectories rather than one, 6-18x the dispatches, and every dispatch
    a chance at the input-chain deadlock (2026-10-01: 32 parks at 'pointer
    move', plus 12 at 'field scroll' — plausibly the next driver call
    queued behind a chain the previous click left stuck). The arrival teleport (e0b814a) and the arrival move's steps
    (10d7a1d) were retired for the same reason; nothing in the history
    shows humanize's own single trajectory being scored as automation.

    The scroll is instant and settled before geometry is read: with smooth
    scrolling a rect read mid-animation points where the element was, the
    click lands on whatever is there instead, and — because mouse.click never
    fails on an overlay the way locator.click does — the miss is silent
    (measured 2026-09-26: every field typed into the void, HTML5 validation
    then blocked the submit with no POST and no error).

    `pre_submit=False` (wizard steps after the first POST) drops the marks:
    see `_unmarked`.
    """
    at = live.at if (pre_submit and live is not None) else _unmarked
    with at("field scroll"):
        control.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    # Instant scrolls do not animate, but layout may need a beat before the
    # rect is readable. Fixed margin, not a scrollY poll: polling would spend
    # page.evaluate calls that belong to the readiness gate.
    with at("field settle"):
        page.wait_for_timeout(300)
    # Bounded, then marked above that bound: a form field that takes >5s to
    # appear is a broken page (fail fast as fields_failed), and a driver
    # roundtrip that never returns must surface as a hang, not deadline+grace.
    with at("field geometry", 13.0):
        box = control.bounding_box(timeout=min(remaining(), 5000))
    if not box:
        with at("field click", 13.0):
            control.click(timeout=min(remaining(), 5000))
        return
    tx, ty = _click_target(box)
    # Input dispatch is the class that wedges (playwright gives it no
    # timeout of its own); one animated trajectory is bounded by humanize's
    # maxTime, so its mark sits at POINTER_MOVE_STUCK_S. Move, a short
    # human beat, then press — each its own mark, so a park names which.
    with at("pointer move", POINTER_MOVE_STUCK_S):
        page.mouse.move(tx, ty)
    with at("pointer dwell"):
        page.wait_for_timeout(min(random.randint(60, 180), remaining()))
    with at("pointer click"):
        page.mouse.click(tx, ty)


def _skim(page, remaining, min_steps=6, max_steps=22) -> None:
    """Best-effort reading pause: a short cursor wander, no click.

    The wizard's gate page gets NO fill engagement — measured 2026-10-01,
    the click landed ~4s after the page rendered and the gate's verify was
    the most-failed of the wizard's three POSTs, while step0's verify (after
    a ~60-140s fill) passed often. A human reads the warning first. Steps,
    not wall-clock, so a frozen test clock cannot spin; ~250-750ms each ⇒
    roughly 4-12s. Best effort only: the click after this must still happen.
    """
    try:
        x = random.randint(250, 800)
        y = random.randint(180, 500)
        page.mouse.move(x, y)
        for _ in range(random.randint(min_steps, max_steps)):
            remaining()  # a deadline mid-skim ends it; the click still follows
            page.mouse.move(max(40, min(1360, x + random.randint(-180, 180))),
                            max(40, min(860, y + random.randint(-120, 120))))
            page.wait_for_timeout(random.randint(250, 750))
    except Exception:
        pass  # deadline or a closed page — proceed to the click itself


def run_form(context, *, url, fields, submit, dismiss=None, success_url=None,
             submission_urls=None, wait_until="domcontentloaded", wait_ms=0,
             settle_ms=20000, timeout_ms=120000, captcha_field=None, inspect_only=False,
             require_captcha_token=False, ready_expression=None,
             gate_text=None, step2=None, step2_submit=None, completion_markers=None,
             stop_after_posts=None, live=None):
    targets = validate_form(url, submission_urls, success_url, gate_text, completion_markers)
    # The caller (run_isolated_form, via the worker) owns the live object and
    # its summary line; a direct call (tests, fixtures) owns its own.
    owns_live = live is None
    live = live if live is not None else FormLive()
    deadline = time.monotonic() + timeout_ms / 1000
    # Gate re-verify forensics: the first POST's token digest (salted per run,
    # memory only) — see _token_shape.
    token_memory = {"salt": secrets.token_hex(16), "first": None}
    result = {"contract_version": 2, "status": 0, "url": url, "html": "",
              "ok": False, "form_submissions": 0, "error": None}
    diagnostics = {"inspection_only": inspect_only, "captcha_script_requests": 0,
                   "captcha_script_responses": 0, "captcha_script_http_errors": [], "last_captcha_mint": None,
                   "captcha_network_failures": 0, "submit_click_attempted": False,
                   "token_present": None, "captcha_field_lengths": None,
                   "blocked_mutations": 0, "page_script_errors": 0,
                   "navigation_status": None, "captcha_guard_blocked": False,
                   "ready_condition_met": None, "field_attempt": None,
                    "phase": None, "failure_class": None, "dismiss_clicked": [],
                    "banner_visible": None, "field_state": None, "egress": None,
                    "wizard_gate_clicked": False, "wizard_step2": False,
                    "nav_attempts": 0, "nav_error": None,
                    "captcha_failed": [], "captcha_lib_loaded": False,
                    "captcha_ready": None, "captcha_signal": None,
                    "page_reused": None}
    result["diagnostics"] = diagnostics
    live.result = result
    live.note(wizard=bool(gate_text or step2 or completion_markers),
              stop_after_posts=stop_after_posts)
    page = None
    control = None
    phase = "navigation"
    wizard = bool(gate_text or step2 or completion_markers)
    last_submission_at = 0.0

    def remaining(cap=None):
        value = int((deadline - time.monotonic()) * 1000)
        if value <= 0:
            raise TimeoutError("Form deadline exceeded")
        return min(value, cap) if cap else value

    def is_submission(request):
        target = urlsplit(request.url)._replace(query="", fragment="").geturl()
        return request.method == "POST" and target in targets

    def guard(route):
        nonlocal last_submission_at
        target = urlsplit(route.request.url)
        origin = urlsplit(url)
        if inspect_only and route.request.method not in ("GET", "HEAD", "OPTIONS") and (
                target.scheme, target.netloc) == (origin.scheme, origin.netloc):
            diagnostics["blocked_mutations"] += 1
            route.abort("blockedbyclient")
            return
        if is_submission(route.request):
            # A wizard legitimately POSTs up to three times (step0, the
            # business-email gate, step2) — seconds apart. What is blocked is
            # a double-fire of ONE click and a runaway loop past the wizard.
            now = time.monotonic()
            if (result["form_submissions"] >= 3 or
                    (result["form_submissions"] and now - last_submission_at < 1.2) or
                    diagnostics["captcha_guard_blocked"]):
                route.abort("blockedbyclient")
                return
            # Per-POST token forensics, parsed ONCE: what did THIS post carry,
            # and from which URL? The gate rejections read "Error verifying
            # reCAPTCHA" while gate/step2 were assumed to carry no field — an
            # assumption no measurement had tested. Record every POST; the
            # FIRST one's shape is also the run's token_present (the guard's
            # input) and must survive a later POST that carries nothing.
            shape = _token_shape(route.request, captcha_field, token_memory["salt"])
            if shape is not None:
                minted = diagnostics.get("last_captcha_mint")
                digest = shape.pop("digest", None)
                if result["form_submissions"] == 0:
                    token_memory["first"] = digest
                diagnostics.setdefault("submission_tokens", []).append({
                    "n": result["form_submissions"],
                    "path": urlsplit(route.request.url).path,
                    "token": shape["any"],
                    "mint_age_s": (round(now - minted, 1)
                                   if minted is not None else None),
                    "lengths": shape["lengths"],
                    # reloads: api2/reload responses so far (no change since
                    # the previous POST = nothing minted in between);
                    # same_as_first: this POST re-sent the first POST's token.
                    "reloads": diagnostics.get("captcha_reloads", 0),
                    "same_as_first": (digest == token_memory["first"]
                                      if result["form_submissions"] and digest and token_memory["first"]
                                      else None),
                })
            if result["form_submissions"] == 0:
                if shape is None:
                    diagnostics["token_present"] = None
                else:
                    diagnostics["token_present"] = shape["present"]
                    diagnostics["captcha_field_lengths"] = shape["lengths"]
                if require_captcha_token and diagnostics["token_present"] is not True:
                    diagnostics["captcha_guard_blocked"] = True
                    result["error"] = "captcha_token_missing"
                    route.abort("blockedbyclient")
                    return
            result["form_submissions"] += 1
            last_submission_at = now
        route.continue_()

    def captcha_request(request):
        parsed = urlsplit(request.url)
        return parsed.hostname in ("www.google.com", "www.recaptcha.net", "www.gstatic.com", "recaptcha.google.com") and "/recaptcha/" in parsed.path

    def request_started(request):
        if captcha_request(request) and request.resource_type == "script":
            diagnostics["captcha_script_requests"] += 1

    captcha_answered = set()  # id() of captcha requests whose headers arrived

    def request_failed(request):
        if captcha_request(request):
            diagnostics["captcha_network_failures"] += 1
            # WHICH piece failed, and how: the path class (never the query —
            # sitekeys and tokens live there), the resource type, the
            # engine's error token, and whether headers had already arrived
            # (a body cut mid-transfer counts as a response AND a failure:
            # that is how [2,2,1] can have as many responses as requests).
            try:
                failure = request.failure
            except Exception:
                failure = None
            entry = {"path": captcha_path_class(request.url),
                     "type": getattr(request, "resource_type", None),
                     "code": _failure_code(failure),
                     "after_response": id(request) in captcha_answered}
            diagnostics["captcha_failed"] = (diagnostics["captcha_failed"] + [entry])[-10:]

    def request_finished(request):
        # The library's body fully arrived: the network half of "usable".
        try:
            if (captcha_request(request)
                    and captcha_path_class(request.url) == "recaptcha__*.js"):
                diagnostics["captcha_lib_loaded"] = True
        except Exception:
            pass

    def page_error(_error):
        diagnostics["page_script_errors"] += 1

    def response_received(response):
        # A v3 token is minted by the api2/api3 reload POST — record when the
        # LAST one landed, so each wizard POST can report the token's age.
        # The step0 submit follows the fill (60-140s); v3 tokens expire at
        # ~2 min, and an expired/aging token reads as a low score to the
        # verifier. gate POSTs re-mint at gate-page load (measured: ~2.5k
        # chars fresh), so their age should be seconds.
        try:
            if captcha_request(response.request) and "/reload" in urlsplit(response.url).path:
                diagnostics["last_captcha_mint"] = time.monotonic()
                diagnostics["captcha_reloads"] = diagnostics.get("captcha_reloads", 0) + 1
        except Exception:
            pass
        if captcha_request(response.request):
            captcha_answered.add(id(response.request))
        if captcha_request(response.request) and response.request.resource_type == "script":
            diagnostics["captcha_script_responses"] += 1
            if response.status >= 400:
                diagnostics["captcha_script_http_errors"] = (diagnostics["captcha_script_http_errors"] + [response.status])[-10:]
        if is_submission(response.request):
            result["status"] = response.status

    def open_page():
        log.info("form flow: new_page")
        opened, reused = first_page(context, live)
        diagnostics["page_reused"] = reused
        log.info("form flow: page ready (reused=%s)", reused)
        opened.on("request", request_started)
        opened.on("requestfailed", request_failed)
        opened.on("requestfinished", request_finished)
        opened.on("pageerror", page_error)
        opened.on("response", response_received)
        return opened

    def captcha_usable():
        """(usable, signal) — can the page's reCAPTCHA client mint?

        Decided by the strongest signal present, all read-only:
        - an anchor frame (v3 badge / v2 widget) is attached: usable iff
          its document carries #recaptcha-token (the anchor really loaded);
        - else the page's main world exposes grecaptcha.execute (the
          library initialised — the api.js stub has only ready());
        - else the recaptcha__*.js body arrived (requestfinished).
        """
        try:
            frames = list(page.frames) if isinstance(page.frames, (list, tuple)) else []
        except Exception:
            frames = []
        anchors = []
        for frame in frames:
            try:
                if _ANCHOR_PATH.search(urlsplit(frame.url or "").path):
                    anchors.append(frame)
            except Exception:
                continue
        for frame in anchors:
            try:
                with live.at("captcha frame check"):
                    if frame.evaluate("() => !!document.getElementById('recaptcha-token')") is True:
                        return True, "anchor"
            except Exception:
                continue
        if anchors:
            return False, "anchor_empty"
        try:
            with live.at("captcha check"):
                if page.evaluate("mw:(" + GRECAPTCHA_USABLE_JS + ")") is True:
                    return True, "execute"
        except Exception:
            pass
        if diagnostics.get("captcha_lib_loaded"):
            return True, "lib"
        return False, None

    def wait_captcha_usable():
        until = time.monotonic() + CAPTCHA_READY_WAIT_S
        while True:
            usable, signal = captcha_usable()
            if usable or time.monotonic() >= until:
                return usable, signal
            with live.at("captcha pause"):
                page.wait_for_timeout(remaining(CAPTCHA_READY_POLL_MS))

    try:
        live.enter("navigation")
        context.route("**/*", guard)
        # Bounded retry, strictly pre-input: a transient refusal or a page
        # that died on arrival (see _NAV_RETRY) costs seconds here instead
        # of a whole caller attempt. A closed page is replaced by a fresh
        # one in the same context; anything else re-raises as before.
        for attempt in range(1, NAV_ATTEMPTS + 1):
            diagnostics["nav_attempts"] = attempt
            opening = page is None
            try:
                if page is None:
                    page = open_page()
                navigation = page.goto(url, wait_until=wait_until,
                                       timeout=remaining(NAV_TIMEOUT_MS))
                break
            except TimeoutError:
                raise  # remaining(): the form deadline itself, nothing to retry with
            except Exception as error:
                # new_page raising (2026-10-02 15:43:41: class Error 0.17s
                # after new_page, no "page ready") is a page that never
                # existed — the same zero-input state as a refused GET.
                code = "new_page_failed" if opening and page is None else _nav_code(error)
                diagnostics["nav_error"] = code
                # A goto timeout (playwright's own TimeoutError, NOT the
                # builtin one remaining() raises) is a stalled exit: retried
                # only while the form itself still has its budget.
                retry = code in _NAV_RETRY or (
                    code == "timeout"
                    and deadline - time.monotonic() >= NAV_RETRY_MIN_LEFT_S)
                if attempt >= NAV_ATTEMPTS or not retry:
                    raise
                log.info("form flow: navigation attempt %d failed (%s); retrying",
                         attempt, code)
                if code == "target_closed":
                    with contextlib.suppress(Exception):
                        page.close()
                    page = None
                pause = NAV_BACKOFF_S[min(attempt - 1, len(NAV_BACKOFF_S) - 1)]
                time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
        log.info("form flow: navigated (status=%s)",
                 navigation.status if navigation is not None else None)
        if navigation is not None and isinstance(navigation.status, int):
            diagnostics["navigation_status"] = navigation.status
        if wait_ms:
            log.info("form flow: wait_ms=%s", wait_ms)
            page.wait_for_timeout(min(wait_ms, remaining()))
        # Deliberately noisy: every phase logs, because a flow that never
        # returns leaves nothing else — the wedge handler sees a stackless
        # thread and can only read the last marker (the earlier wedges died
        # between this line and a later marker and could not say which call
        # never returned).
        log.info("form flow: wait done")
        # Which IP does this browser ACTUALLY egress from? The contract says
        # every run is pinned to the residential pool by PROXY_URL — but a
        # datacenter or direct egress mints reCAPTCHA tokens from the worst
        # possible IP and reads exactly like the 2026-09-29 collapse. Fetched
        # from inside the page, so it traverses the same proxy the form will.
        try:
            with live.at("egress check", 12.0):
                egress = page.evaluate(
                    "fetch('https://ipwho.is/?fields=success,country,connection',"
                    "{signal:AbortSignal.timeout(8000)}).then(r=>r.json()).catch(()=>null)")
            if isinstance(egress, dict) and egress.get("success"):
                connection = egress.get("connection") or {}
                diagnostics["egress"] = {"country": egress.get("country"),
                                         "isp": connection.get("isp"),
                                         "asn": connection.get("asn")}
        except Exception:
            diagnostics["egress"] = None
        if inspect_only:
            result["url"] = page.url
            # No page contents/hidden tokens in an inspection response.
            return result
        # reCAPTCHA readiness gate, BEFORE any input. On cf156 every no-token
        # POST (5/33) came from a page whose reCAPTCHA client never
        # initialised: the oracle saw no t_submit at all, i.e. the submit
        # listener attached in grecaptcha.ready() did not exist and the
        # click fell through to a NATIVE submit with an empty field
        # (missing-input-response; "Error verifying reCAPTCHA" on a real
        # django-recaptcha form). Counting scripts cannot see it — [2,2,1]
        # has as many responses as requests — so ask the page instead.
        # Only pages that requested a reCAPTCHA script are gated. Nothing has
        # been touched yet, so ONE reload is as safe as the navigation retry;
        # still unusable after it is a zero-POST captcha_unavailable (503
        # retryable), never a submit with an empty token.
        if diagnostics["captcha_script_requests"]:
            live.enter("captcha")
            usable, signal = wait_captcha_usable()
            diagnostics["captcha_ready"] = usable
            diagnostics["captcha_signal"] = signal
            if not usable:
                diagnostics["captcha_script_reload"] = True
                log.info("form flow: reCAPTCHA not usable (signal=%s failed=%s); reloading once",
                         signal, [f.get("path") for f in diagnostics["captcha_failed"]])
                diagnostics["captcha_lib_loaded"] = False
                failures_before = diagnostics["captcha_network_failures"]
                with live.at("captcha reload", NAV_TIMEOUT_MS / 1000 + 10.0):
                    page.goto(url, wait_until=wait_until, timeout=remaining(NAV_TIMEOUT_MS))
                with live.at("captcha reload wait", max(wait_ms, 2000) / 1000 + 8.0):
                    page.wait_for_timeout(min(max(wait_ms, 2000), remaining()))
                usable, signal = wait_captcha_usable()
                diagnostics["captcha_ready"] = usable
                diagnostics["captcha_signal"] = signal
                diagnostics["captcha_script_reload_failed"] = (
                    diagnostics["captcha_network_failures"] > failures_before)
                if not usable:
                    result["error"] = "captcha_unavailable"
                    raise CaptchaUnavailable("reCAPTCHA client unusable after one reload")
                log.info("form flow: reCAPTCHA usable after reload (signal=%s)", signal)
        # Arrive like a person before touching anything: settle, scroll,
        # dwell. A submit seconds after navigation with no prior input reads
        # as automation no matter how human the typing itself is.
        #
        # The arrival used to open with a full-viewport mouse.move — and
        # input dispatch is the one call playwright gives no timeout, so
        # when camoufox's browser-side trajectory animation never returned
        # the flow parked there (every logged wedge died between "moving
        # pointer" and "pointer moved"; 2026-09-29: 2 parks in 6 attempts).
        # The teleport is retired: it was the least human gesture anyway
        # (a jump from offscreen over the whole viewport). The arrival is
        # now the wheel — an input dispatch that has never wedged across
        # every logged arrival — still marked, so a park on it keeps the
        # same 503-retryable contract: nothing touched, nothing POSTed.
        live.enter("arrival")
        page.wait_for_timeout(min(random.randint(700, 2200), remaining()))
        with live.at(PRE_SUBMIT_STEP):
            page.mouse.wheel(0, random.randint(200, 600))
        log.info("form flow: scrolled")
        page.wait_for_timeout(min(random.randint(400, 1200), remaining()))
        log.info("form flow: dwell done; dismiss=%s", dismiss)
        # The banner can arrive AFTER this moment (slow third-party load) or
        # survive the first click (render race) — a banner left covering the
        # form burns the first human_click's whole bounded timeout at the
        # field. So dismiss is a bounded LOOP: wait for the target, click,
        # settle, re-check, retry. Check-once let both observed failures
        # through: a click that did not clear the overlay (2026-09-29), and
        # a banner that had not loaded at click time yet was visible right
        # before fields (2026-09-30) — diagnostics recorded the truth and
        # the fields failed anyway.
        for _ in range(3 if dismiss else 0):
            clicked = False
            for selector in dismiss or []:
                target = page.locator(selector).first
                try:
                    target.wait_for(state="visible", timeout=remaining(3000))
                except Exception:
                    continue  # not here (yet) — a late arrival is next round
                try:
                    with live.at("dismiss click"):
                        target.click(timeout=remaining(2000))
                    diagnostics["dismiss_clicked"].append(selector)
                    clicked = True
                except Exception:
                    pass  # Optional cookie banners; required fields below fail closed.
            if clicked:
                page.wait_for_timeout(min(random.randint(400, 900), remaining()))
            try:
                with live.at("banner check"):
                    diagnostics["banner_visible"] = bool(page.locator(dismiss[0]).first.is_visible())
            except Exception:
                diagnostics["banner_visible"] = None
            if not diagnostics["banner_visible"]:
                break
        log.info("form flow: fields (%d)", len(fields))

        def fill_fields(field_list, pre_submit=True):
            nonlocal control, phase
            at = live.at if pre_submit else _unmarked
            for field in field_list:
                # Selector only — never the value. On an exception this is the
                # field that was in flight, which is otherwise invisible (the
                # outer handler deliberately swallows the message).
                diagnostics["field_attempt"] = field["selector"]
                control = page.locator(field["selector"])
                action = field.get("action", "type")
                if action == "check":
                    # A human click toggles: ensure the checked state rather than
                    # assuming it, but keep the pointer real throughout.
                    for _ in range(3):
                        human_click(page, control, remaining, pre_submit=pre_submit, live=live)
                        try:
                            with at("field check"):
                                if control.is_checked(timeout=remaining(1000)):
                                    break
                        except Exception:
                            break
                elif action == "select":
                    # Bounded at 10s (a control still unselectable then is a
                    # broken form), marked above the bound so a wedged driver
                    # surfaces as a hang instead of riding to deadline+grace.
                    with at("field select", 18.0):
                        control.select_option(field.get("value"), timeout=min(remaining(), 10000))
                elif action == "type":
                    human_click(page, control, remaining, pre_submit=pre_submit, live=live)
                    value = field.get("value") or ""
                    # The human click aims by geometry. When focus never landed
                    # (an overlay swallowed the click, the rect was stale), the
                    # keystrokes go to whatever already had focus — measured
                    # 2026-09-29 as a recurring first-field fields_failed. One
                    # actionability-aware retry: it waits out an overlay instead
                    # of clicking through it. is_focused is advisory (a raise
                    # reads as not focused); the read-back below still fails loud
                    # if the text did not land either way.
                    try:
                        with at("field focus read"):
                            focused = bool(control.is_focused())
                    except Exception:
                        focused = False
                    if not focused:
                        with at("field focus click", 18.0):
                            control.click(timeout=min(remaining(), 10000))
                    # Keystroke delay is client-side and scales with the value;
                    # dispatch itself is animated in camoufox. The mark's
                    # threshold is the worst case + margin, so a slow type is
                    # never mistaken for a park.
                    with at("field type", len(value) * 0.25 + 10.0):
                        page.keyboard.type(value, delay=random.randint(45, 120))
                    with at("field pause"):
                        page.wait_for_timeout(min(random.randint(120, 420), remaining()))
                    # Typed into the void is the silent killer (empty fields trip
                    # HTML5 validation, which blocks the submit with no POST and
                    # no error). Read back what landed and fail loud on a miss.
                    if value:
                        with at("field read-back", 16.0):
                            landed = control.input_value(timeout=min(remaining(), 8000))
                        if landed != value:
                            raise ValueError("typed text did not land in the field")
                else:
                    raise ValueError("Unknown field action")
            diagnostics["field_attempt"] = None

        phase = "fields"
        live.enter("fields")
        fill_fields(fields)
        if ready_expression:
            phase = "readiness"
            live.enter("readiness")
            log.info("form flow: readiness")
            diagnostics["ready_condition_met"] = False
            # Camoufox isolates ordinary evaluation from the page's globals.
            # The prefix opts into its main world (a JS label in other engines).
            # Each iteration re-marks, so the loop never looks parked — only
            # one stuck call does. The loop itself is bounded by READY_WAIT_S:
            # a condition still false then is a page whose integration never
            # loaded, and the run fails fast as readiness_failed (zero POSTs,
            # retryable) instead of polling the whole remaining deadline away.
            ready_until = time.monotonic() + READY_WAIT_S
            while True:
                with live.at("ready check"):
                    met = page.evaluate("mw:(" + ready_expression + ")") is True
                if met:
                    break
                if time.monotonic() >= ready_until:
                    raise TimeoutError("ready condition not met")
                with live.at("ready pause"):
                    page.wait_for_timeout(remaining(100))
            diagnostics["ready_condition_met"] = True
        phase = "submit"
        live.enter("submit")
        log.info("form flow: submit click")
        # Exactly one click — the page's own handler mints any CAPTCHA token
        # it needs (observation only above; this service never mints). Dwell
        # first: a submit the instant the last field fills in is machine
        # timing, and the token must be minted after the interaction anyway.
        page.wait_for_timeout(min(random.randint(800, 2400), remaining()))
        diagnostics["submit_click_attempted"] = True
        page.locator(submit).click(timeout=remaining())
        phase = "outcome"
        live.enter("outcome")
        if not wizard:
            log.info("form flow: waiting for outcome (success_url=%s)", bool(success_url))
            try:
                if success_url:
                    page.wait_for_url(re.compile(success_url), timeout=remaining(settle_ms))
                else:
                    page.wait_for_load_state("networkidle", timeout=remaining(settle_ms))
            except Exception:
                pass  # Rejections stay on the form. Capture them, don't resubmit.
        else:
            # Wizard outcome walk. step0's answer is one of four shapes and
            # only the URL sometimes distinguishes them: the next step, a
            # business-email gate needing a click, a rejection rendered as
            # errors ON the form, or completion whose body copy (manual
            # review, same URL) is the ONLY signal. So: poll, act once per
            # wizard step, never re-POST (the guard allows exactly the
            # wizard's own three: step0, gate, step2).
            log.info("form flow: wizard outcome walk (gate=%s step2=%s success_url=%s)",
                     bool(gate_text), bool(step2), bool(success_url))
            gate_re = re.compile(gate_text, re.I) if gate_text else None
            marker_res = [re.compile(m, re.I) for m in (completion_markers or [])]
            # "still on step0" detector = the first field of the real form.
            step0_sel = fields[0]["selector"] if fields else None
            step2_sel = step2[0]["selector"] if step2 else None
            step2_submit_sel = step2_submit or "form button"
            gate_clicked = False
            step2_done = False
            wizard_error = None

            def normalize(text):
                return re.sub(r"\s+", " ", text or "")

            # The POST's own response navigates the page; polling before it
            # lands races the navigation and dies with a destroyed execution
            # context (measured 2026-10-01: first tick, phase=outcome,
            # class=Error). One settle, then every tick tolerates the race.
            page.wait_for_timeout(min(random.randint(700, 1500), remaining()))
            # The step0 POST fires from the PAGE's own submit handler after
            # our click — it may not be on the wire when we get here (the
            # first warm runs raced: the check saw 0, the walk then clicked
            # the gate and completed the whole wizard). Poll for it before
            # deciding; a POST that never arrives falls through to the walk.
            stop_waited = 0.0
            while (stop_after_posts is not None
                   and result["form_submissions"] < stop_after_posts
                   and stop_waited < 20.0):
                page.wait_for_timeout(min(500, remaining()))
                stop_waited += 0.5
            if stop_after_posts is not None and result["form_submissions"] >= stop_after_posts:
                # Warm-up mode: the step0 POST is what VERIFIES (the score the
                # reject cites), so a warm run does a real POST and stops here
                # — the gate/step2 are where identities complete, and they are
                # deliberately not reached. An inspect-only warm never POSTs
                # and so never verifies; measured weaker (a1 stays cold).
                log.info("form flow: stop_after_posts=%s reached (subs=%s)",
                         stop_after_posts, result["form_submissions"])
                try:
                    result["html"] = page.content()
                except Exception:
                    pass
                result["error"] = "stopped_after_posts"
                return result
            while True:
                if time.monotonic() >= deadline:
                    break
                if stop_after_posts is not None and result["form_submissions"] >= stop_after_posts:
                    # The POST landed mid-walk (the pre-walk wait timed out
                    # with it still in flight): stop before any gate action.
                    log.info("form flow: stop_after_posts=%s reached mid-walk (subs=%s)",
                             stop_after_posts, result["form_submissions"])
                    try:
                        result["html"] = page.content()
                    except Exception:
                        pass
                    result["error"] = "stopped_after_posts"
                    return result
                # One evaluate per tick: url, both step detectors, the error
                # nodes, the gate button, and the body text (completion
                # markers live in the text, not the url).
                try:
                    state = page.evaluate("""(sel) => {
                        const q = (s) => s ? document.querySelector(s) : null;
                        const visible = (el) => !!el && el.getBoundingClientRect().width > 0;
                        const errs = [...document.querySelectorAll(
                            '.error-msg,.invalid-feedback,.errorlist')]
                            .map((e) => (e.textContent || '').replace(/\\s+/g, ' ').trim())
                            .filter(Boolean);
                        return {
                            url: location.href,
                            step0: visible(q(sel.step0)),
                            step2: visible(q(sel.step2)),
                            errs,
                            text: (document.body.innerText || '').replace(/\\s+/g, ' ')
                        };
                    }""", {"step0": step0_sel, "step2": step2_sel})
                except Exception:
                    # Mid-navigation: the context will be back next tick.
                    try:
                        page.wait_for_timeout(min(random.randint(600, 1200), remaining()))
                    except TimeoutError:
                        break
                    continue
                state["text"] = normalize(state["text"])
                if success_url and re.search(success_url, state["url"]):
                    break
                if any(m.search(state["text"]) for m in marker_res):
                    break
                if gate_re and not gate_clicked:
                    # The gate is a BUTTON (not a link) inside the warning.
                    # Once only: a second click would be a duplicate POST.
                    gate_loc = page.locator(
                        "button, a", has_text=gate_re).first
                    try:
                        if gate_loc.is_visible():
                            log.info("form flow: clicking business-email gate")
                            _skim(page, remaining)
                            human_click(page, gate_loc, remaining, pre_submit=False)
                            gate_clicked = True
                            diagnostics["wizard_gate_clicked"] = True
                            page.wait_for_timeout(min(random.randint(700, 1500), remaining()))
                            continue
                    except Exception:
                        pass  # not rendered (yet) — next tick
                if step2_sel and not step2_done:
                    probe = page.locator(step2_sel).first
                    try:
                        step2_visible = probe.is_visible()
                    except Exception:
                        step2_visible = False
                    if step2_visible:
                        log.info("form flow: step2 — filling %d field(s)", len(step2 or []))
                        # phase marks WHERE a raise happened, not retryability:
                        # step0's POST is already out, so any later failure
                        # stays outcome_unknown (never replayed) while the
                        # diagnostics still name the field.
                        phase = "fields"
                        fill_fields(step2 or [], pre_submit=False)
                        page.wait_for_timeout(min(random.randint(500, 1100), remaining()))
                        phase = "outcome"
                        _skim(page, remaining, 3, 8)
                        human_click(page, page.locator(step2_submit_sel).first,
                                    remaining, pre_submit=False)
                        step2_done = True
                        diagnostics["wizard_step2"] = True
                        page.wait_for_timeout(min(random.randint(700, 1500), remaining()))
                        continue
                # Terminal rejections: the form reset to step0 after step2, or
                # an error that is NOT the gate's own message (a captcha or
                # server rejection stays on the form — capture, never re-POST).
                business_only = bool(state["errs"]) and all(
                    re.search(r"business email", e, re.I) for e in state["errs"])
                if step2_done and state["step0"] and not state["step2"]:
                    wizard_error = "wizard_reset"
                    break
                if state["errs"] and not business_only:
                    wizard_error = "wizard_rejected"
                    # WHICH POST's answer carried the error (1 = step0,
                    # 2 = gate, 3 = step2) — the re-verify question's key.
                    diagnostics["rejected_after_posts"] = result["form_submissions"]
                    break
                try:
                    page.wait_for_timeout(min(random.randint(700, 1300), remaining()))
                except TimeoutError:
                    break  # polled to the deadline: incomplete, not unknown
        result["url"] = page.url
        log.info("form flow: capturing content")
        try:
            result["html"] = page.content()
        except Exception:
            # A page still navigating at the deadline yields no DOM; the
            # url/status/error already captured stay valid evidence.
            result["html"] = ""
        if wizard:
            try:
                final_text = re.sub(r"\s+", " ",
                                    page.evaluate("document.body.innerText") or "")
            except Exception:
                final_text = ""
            marker_hit = any(m.search(final_text) for m in marker_res)
            url_hit = bool(success_url and re.search(success_url, result["url"]))
            status_ok = bool(result["status"]) and 200 <= result["status"] < 400
            result["ok"] = bool(result["form_submissions"] >= 1 and status_ok and
                                (url_hit or marker_hit))
            if wizard_error and not result["error"]:
                result["error"] = wizard_error
        else:
            result["ok"] = bool(result["form_submissions"] == 1 and
                                200 <= result["status"] < 400 and success_url and
                                re.search(success_url, result["url"]))
        if not result["form_submissions"] and not result["error"]:
            result["error"] = "no_submission"
        elif result["form_submissions"] and not result["status"]:
            result["error"] = "outcome_unknown"
        elif wizard and not result["ok"] and not result["error"]:
            result["error"] = "wizard_incomplete"
    except Exception as error:
        # Never expose field values, page exception text or proxy credentials.
        # The CLASS name is not text — it separates a click that timed out from
        # the typed-text check failing (both surface as fields_failed) without
        # carrying anything the exception's message might. The field's own
        # state at failure is booleans only.
        diagnostics["failure_class"] = type(error).__name__
        diagnostics["phase"] = phase
        if phase == "fields" and control is not None:
            try:
                diagnostics["field_state"] = {"visible": bool(control.is_visible()),
                                              "enabled": bool(control.is_enabled())}
            except Exception:
                diagnostics["field_state"] = None
        result["error"] = result["error"] or ("outcome_unknown" if result["form_submissions"] else f"{phase}_failed")
    finally:
        # One line per form, on EVERY return path — inspect_only and
        # stopped_after_posts return from inside the try and used to skip
        # it (2026-10-01: inspect runs ended at "wait done" with no verdict
        # in the log). field_attempt names the SELECTOR in flight when a
        # phase raised, failure_class the kind of exception behind it,
        # banner the cookie-banner state before the fields phase, and the
        # path shows what the submit actually landed on. Selector + class +
        # booleans + path only — never a value or an exception message.
        log.info("form flow: done ok=%s error=%s field=%s cls=%s subs=%s status=%s path=%s banner=%s fstate=%s",
                 result.get("ok"), result.get("error"), diagnostics.get("field_attempt"),
                 diagnostics.get("failure_class"),
                 result.get("form_submissions"), result.get("status"),
                 urlsplit(result.get("url") or "").path,
                 diagnostics.get("banner_visible"), diagnostics.get("field_state"))
        if owns_live:
            live.emit(result)
    return result

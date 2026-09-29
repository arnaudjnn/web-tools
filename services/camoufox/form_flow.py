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
import logging
import random
import re
import time
from urllib.parse import urlsplit, parse_qs

# Phase logs are the ONLY visibility into a flow that never returns: the
# wedge handler sees a stackless greenlet and can only say "somewhere".
log = logging.getLogger("camoufox.forms")

# Live-step marker, polled by the admission worker from the event loop. Exactly
# ONE pre-POST call is unbounded: the arrival wheel below — playwright gives
# input dispatch no timeout and camoufox's browser-side animation has been
# observed to never return (every logged wedge died on an input dispatch:
# first the full-viewport mouse.move, retired 2026-09-29 after 2 parks in 6
# attempts, now the wheel, which has never wedged). Everything else is
# bounded: locator/fill/click by playwright's default timeout, goto by its
# own, the outcome wait by remaining(). A park here means no field was
# touched and no POST left the machine — retryable, unlike every other stall.
_LIVE = {"name": "", "at": 0.0}
PRE_SUBMIT_STUCK_S = 8.0
PRE_SUBMIT_STEP = "arrival scroll"


def _mark(name):
    _LIVE["name"] = name
    _LIVE["at"] = time.monotonic()


def reset_live_step():
    _LIVE["name"] = ""
    _LIVE["at"] = 0.0


def pre_submit_hang_step():
    """The step name if the flow parked on the unbounded pre-POST call."""
    if _LIVE["name"] == PRE_SUBMIT_STEP and time.monotonic() - _LIVE["at"] > PRE_SUBMIT_STUCK_S:
        return _LIVE["name"]
    return None


def validate_form(url, submission_urls, success_url):
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
    return {urlsplit(value)._replace(query="", fragment="").geturl() for value in urls}


def human_click(page, control, remaining) -> None:
    """Click a control the way a pointer does: bring it into view instantly,
    approach in steps, land off-centre, press and release. An identical
    dead-centre click on every control is its own pattern; a synthetic .click()
    with no pointer ever moving is a bigger one.

    The scroll is instant and settled before geometry is read: with smooth
    scrolling a rect read mid-animation points where the element was, the
    click lands on whatever is there instead, and — because mouse.click never
    fails on an overlay the way locator.click does — the miss is silent
    (measured 2026-09-26: every field typed into the void, HTML5 validation
    then blocked the submit with no POST and no error).
    """
    control.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    # Instant scrolls do not animate, but layout may need a beat before the
    # rect is readable. Fixed margin, not a scrollY poll: polling would spend
    # page.evaluate calls that belong to the readiness gate.
    page.wait_for_timeout(300)
    box = control.bounding_box(timeout=remaining())
    if not box:
        control.click(timeout=remaining())
        return
    tx = box["x"] + box["width"] / 2 + random.uniform(-box["width"] / 4, box["width"] / 4)
    ty = box["y"] + box["height"] / 2 + random.uniform(-4, 4)
    steps = random.randint(6, 18)
    sx, sy = tx - random.randint(100, 400), ty - random.randint(60, 200)
    for i in range(1, steps + 1):
        page.mouse.move(sx + (tx - sx) * i / steps, sy + (ty - sy) * i / steps)
        page.wait_for_timeout(random.randint(8, 30))
    page.mouse.click(tx, ty)


def run_form(context, *, url, fields, submit, dismiss=None, success_url=None,
             submission_urls=None, wait_until="domcontentloaded", wait_ms=0,
             settle_ms=20000, timeout_ms=120000, captcha_field=None, inspect_only=False,
             require_captcha_token=False, ready_expression=None):
    targets = validate_form(url, submission_urls, success_url)
    deadline = time.monotonic() + timeout_ms / 1000
    result = {"contract_version": 2, "status": 0, "url": url, "html": "",
              "ok": False, "form_submissions": 0, "error": None}
    diagnostics = {"inspection_only": inspect_only, "captcha_script_requests": 0,
                   "captcha_script_responses": 0, "captcha_script_http_errors": [],
                   "captcha_network_failures": 0, "submit_click_attempted": False,
                   "token_present": None, "blocked_mutations": 0, "page_script_errors": 0,
                   "navigation_status": None, "captcha_guard_blocked": False,
                   "ready_condition_met": None, "field_attempt": None,
                   "phase": None, "failure_class": None, "dismiss_clicked": [],
                   "banner_visible": None, "field_state": None}
    result["diagnostics"] = diagnostics
    page = None
    control = None
    phase = "navigation"

    def remaining(cap=None):
        value = int((deadline - time.monotonic()) * 1000)
        if value <= 0:
            raise TimeoutError("Form deadline exceeded")
        return min(value, cap) if cap else value

    def is_submission(request):
        target = urlsplit(request.url)._replace(query="", fragment="").geturl()
        return request.method == "POST" and target in targets

    def guard(route):
        target = urlsplit(route.request.url)
        origin = urlsplit(url)
        if inspect_only and route.request.method not in ("GET", "HEAD", "OPTIONS") and (
                target.scheme, target.netloc) == (origin.scheme, origin.netloc):
            diagnostics["blocked_mutations"] += 1
            route.abort("blockedbyclient")
            return
        if is_submission(route.request):
            if result["form_submissions"] or diagnostics["captcha_guard_blocked"]:
                route.abort("blockedbyclient")
                return
            # Observe presence only. Never retain, log or return the token/body.
            try:
                values = parse_qs(route.request.post_data or "")
                names = [captcha_field] if captcha_field else ["g-recaptcha-response"]
                diagnostics["token_present"] = any(
                    len(values.get(name, [])) == 1 and
                    values[name][0].strip().lower() not in ("", "null", "undefined", "false")
                    for name in names)
            except Exception:
                diagnostics["token_present"] = None
            if require_captcha_token and diagnostics["token_present"] is not True:
                diagnostics["captcha_guard_blocked"] = True
                result["error"] = "captcha_token_missing"
                route.abort("blockedbyclient")
                return
            result["form_submissions"] = 1
        route.continue_()

    def captcha_request(request):
        parsed = urlsplit(request.url)
        return parsed.hostname in ("www.google.com", "www.recaptcha.net", "www.gstatic.com", "recaptcha.google.com") and "/recaptcha/" in parsed.path

    def request_started(request):
        if captcha_request(request) and request.resource_type == "script":
            diagnostics["captcha_script_requests"] += 1

    def request_failed(request):
        if captcha_request(request):
            diagnostics["captcha_network_failures"] += 1

    def page_error(_error):
        diagnostics["page_script_errors"] += 1

    def response_received(response):
        if captcha_request(response.request) and response.request.resource_type == "script":
            diagnostics["captcha_script_responses"] += 1
            if response.status >= 400:
                diagnostics["captcha_script_http_errors"] = (diagnostics["captcha_script_http_errors"] + [response.status])[-10:]
        if is_submission(response.request):
            result["status"] = response.status

    try:
        context.route("**/*", guard)
        _mark("new page")
        log.info("form flow: new_page")
        page = context.new_page()
        log.info("form flow: page ready")
        page.on("request", request_started)
        page.on("requestfailed", request_failed)
        page.on("pageerror", page_error)
        page.on("response", response_received)
        navigation = page.goto(url, wait_until=wait_until, timeout=remaining())
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
        if inspect_only:
            result["url"] = page.url
            # No page contents/hidden tokens in an inspection response.
            return result
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
        page.wait_for_timeout(min(random.randint(700, 2200), remaining()))
        _mark(PRE_SUBMIT_STEP)
        page.mouse.wheel(0, random.randint(200, 600))
        reset_live_step()
        log.info("form flow: scrolled")
        page.wait_for_timeout(min(random.randint(400, 1200), remaining()))
        log.info("form flow: dwell done; dismiss=%s", dismiss)
        for selector in dismiss or []:
            try:
                page.locator(selector).first.click(timeout=remaining(2000))
                diagnostics["dismiss_clicked"].append(selector)
            except Exception:
                pass  # Optional cookie banners; required fields below fail closed.
        # A banner left covering the form makes the first human_click wait out
        # its actionability timeout — recorded so a fields_failed can say
        # whether the overlay was still up, without logging any page text.
        if dismiss:
            try:
                diagnostics["banner_visible"] = bool(page.locator(dismiss[0]).first.is_visible())
            except Exception:
                diagnostics["banner_visible"] = None
        log.info("form flow: fields (%d)", len(fields))
        phase = "fields"
        for field in fields:
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
                    human_click(page, control, remaining)
                    try:
                        if control.is_checked(timeout=remaining(1000)):
                            break
                    except Exception:
                        break
            elif action == "select":
                control.select_option(field.get("value"), timeout=remaining())
            elif action == "type":
                human_click(page, control, remaining)
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
                    focused = bool(control.is_focused())
                except Exception:
                    focused = False
                if not focused:
                    control.click(timeout=remaining())
                page.keyboard.type(value, delay=random.randint(45, 120))
                page.wait_for_timeout(min(random.randint(120, 420), remaining()))
                # Typed into the void is the silent killer (empty fields trip
                # HTML5 validation, which blocks the submit with no POST and
                # no error). Read back what landed and fail loud on a miss.
                if value and control.input_value(timeout=remaining()) != value:
                    raise ValueError("typed text did not land in the field")
            else:
                raise ValueError("Unknown field action")
        diagnostics["field_attempt"] = None
        if ready_expression:
            phase = "readiness"
            log.info("form flow: readiness")
            diagnostics["ready_condition_met"] = False
            # Camoufox isolates ordinary evaluation from the page's globals.
            # The prefix opts into its main world (a JS label in other engines).
            while page.evaluate("mw:(" + ready_expression + ")") is not True:
                page.wait_for_timeout(remaining(100))
            diagnostics["ready_condition_met"] = True
        phase = "submit"
        log.info("form flow: submit click")
        # Exactly one click — the page's own handler mints any CAPTCHA token
        # it needs (observation only above; this service never mints). Dwell
        # first: a submit the instant the last field fills in is machine
        # timing, and the token must be minted after the interaction anyway.
        page.wait_for_timeout(min(random.randint(800, 2400), remaining()))
        diagnostics["submit_click_attempted"] = True
        page.locator(submit).click(timeout=remaining())
        phase = "outcome"
        log.info("form flow: waiting for outcome (success_url=%s)", bool(success_url))
        try:
            if success_url:
                page.wait_for_url(re.compile(success_url), timeout=remaining(settle_ms))
            else:
                page.wait_for_load_state("networkidle", timeout=remaining(settle_ms))
        except Exception:
            pass  # Rejections stay on the form. Capture them, don't resubmit.
        result["url"] = page.url
        log.info("form flow: capturing content")
        result["html"] = page.content()
        result["ok"] = bool(result["form_submissions"] == 1 and
                            200 <= result["status"] < 400 and success_url and
                            re.search(success_url, result["url"]))
        if not result["form_submissions"] and not result["error"]:
            result["error"] = "no_submission"
        elif result["form_submissions"] and not result["status"]:
            result["error"] = "outcome_unknown"
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
    # One line per form, whatever happened: field_attempt names the SELECTOR
    # in flight when a phase raised (fields_failed alone says where nothing),
    # failure_class the kind of exception behind it, banner the cookie-banner
    # state before the fields phase, and the path shows what the submit
    # actually landed on. Selector + class + booleans + path only — never a
    # value or an exception message.
    log.info("form flow: done ok=%s error=%s field=%s cls=%s subs=%s status=%s path=%s banner=%s fstate=%s",
             result.get("ok"), result.get("error"), diagnostics.get("field_attempt"),
             diagnostics.get("failure_class"),
             result.get("form_submissions"), result.get("status"),
             urlsplit(result.get("url") or "").path,
             diagnostics.get("banner_visible"), diagnostics.get("field_state"))
    return result

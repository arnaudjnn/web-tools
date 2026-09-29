"""Generic single-attempt form execution. No target-specific logic.

CAPTCHA solving happens only when the caller passes `captcha` AND the
Camoufox service holds CAPSOLVER_API_KEY (see captcha_solver); a solver
failure returns structured with zero submissions BEFORE the submit click.
`captcha_proxy` hands the solver the form's own exit (the same proxy dict
the browser POSTs from) so the token's mint IP equals the submit IP — a
gate comparing them rejects a provider-side mint. Without an explicit
`captcha` the site's own handler supplies any token, exactly as before.

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

import captcha_solver

# Phase logs are the ONLY visibility into a flow that never returns: the
# wedge handler sees a stackless greenlet and can only say "somewhere".
log = logging.getLogger("camoufox.forms")


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
             require_captcha_token=False, ready_expression=None, captcha=None,
             captcha_proxy=None):
    targets = validate_form(url, submission_urls, success_url)
    deadline = time.monotonic() + timeout_ms / 1000
    result = {"contract_version": 2, "status": 0, "url": url, "html": "",
              "ok": False, "form_submissions": 0, "error": None}
    diagnostics = {"inspection_only": inspect_only, "captcha_script_requests": 0,
                   "captcha_script_responses": 0, "captcha_script_http_errors": [],
                   "captcha_network_failures": 0, "submit_click_attempted": False,
                   "token_present": None, "blocked_mutations": 0, "page_script_errors": 0,
                   "navigation_status": None, "captcha_guard_blocked": False,
                   "ready_condition_met": None, "solver_attempts": 0,
                   "solver_status": None, "field_attempt": None, "phase": None}
    result["diagnostics"] = diagnostics
    page = None
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
        # Deliberately noisy: wedged runs previously died between this marker
        # and "pointer moved", and nothing could say which of the two calls
        # (client timer vs browser-side move ack) never returned.
        log.info("form flow: wait done")
        if inspect_only:
            result["url"] = page.url
            # No page contents/hidden tokens in an inspection response.
            return result
        # Arrive like a person before touching anything: look around, scroll,
        # dwell. A submit seconds after navigation with no prior input reads
        # as automation no matter how human the typing itself is.
        #
        # Exactly ONE dispatched event: the form browser launches with
        # humanize=True (app.py), so camoufox re-animates EVERY mouse event
        # browser-side (~0.75s each — measured: steps=11 took 8.7s) and each
        # animation is a chance to never return (the wedge: wait done logged,
        # pointer moved never). Playwright's multi-step chain is redundant
        # against camoufox's own full-path trajectory generator — single-event
        # ops (clicks) have never wedged in our logs.
        move_started = time.monotonic()
        move_steps = 1
        page.mouse.move(random.randint(200, 1200), random.randint(150, 700),
                        steps=move_steps)
        log.info("form flow: pointer moved (steps=%s in %.1fs)",
                 move_steps, time.monotonic() - move_started)
        page.wait_for_timeout(min(random.randint(700, 2200), remaining()))
        page.mouse.wheel(0, random.randint(200, 600))
        log.info("form flow: scrolled")
        page.wait_for_timeout(min(random.randint(400, 1200), remaining()))
        log.info("form flow: dwell done; dismiss=%s", dismiss)
        for selector in dismiss or []:
            try:
                page.locator(selector).first.click(timeout=remaining(2000))
            except Exception:
                pass  # Optional cookie banners; required fields below fail closed.
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
        if captcha:
            # Mint the token AFTER the human interaction and BEFORE the submit
            # dwell: the site reads the field on click, and a solver failure
            # must return structured and zero — the click never happens, so a
            # solver outage cannot become a half-submitted form.
            phase = "captcha"
            log.info("form flow: captcha solve")
            diagnostics["solver_attempts"] += 1
            try:
                token = captcha_solver.solve(
                    sitekey=captcha["sitekey"],
                    page_url=page.url,
                    action=captcha.get("action"),
                    version=captcha.get("version") or "v3",
                    remaining=remaining,
                    proxy=captcha_proxy,
                )
            except captcha_solver.SolverError as error:
                diagnostics["solver_status"] = error.kind
                result["error"] = f"captcha_solver_{error.kind}"
                return result
            diagnostics["solver_status"] = "solved"
            try:
                # The response field — recaptcha's hidden textarea by default,
                # but a site binding its own element exposes an INPUT under the
                # custom name (Atoka's 0-captcha is an input: a textarea-only
                # selector silently found nothing and failed closed forever).
                # Set its value directly (it is not a field a person types
                # into — the keystroke rule governs TEXT fields). Events let
                # page listeners see the new value; the DOM is shared with the
                # page even where Camoufox isolates JS globals.
                page.locator(f'[name="{captcha_field or "g-recaptcha-response"}"]').first.evaluate(
                    "(el, token) => { el.value = token;"
                    " el.dispatchEvent(new Event('input', {bubbles: true}));"
                    " el.dispatchEvent(new Event('change', {bubbles: true})); }",
                    token)
            except Exception:
                diagnostics["solver_status"] = "field_missing"
                result["error"] = "captcha_field_missing"
                return result
            token = None  # never hold it longer than the injection
        phase = "submit"
        log.info("form flow: submit click")
        # Exactly one click. Any CAPTCHA token was minted and injected above
        # when the caller asked for it; otherwise the site's own handler
        # supplies it. Dwell first: a submit the instant the last field fills
        # in is machine timing, and the token must be minted after the
        # interaction anyway.
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
    except Exception:
        # Never expose field values, page exception text or proxy credentials.
        diagnostics["phase"] = phase
        result["error"] = result["error"] or ("outcome_unknown" if result["form_submissions"] else f"{phase}_failed")
    # One line per form, whatever happened: field_attempt names the SELECTOR
    # in flight when a phase raised (fields_failed alone says where nothing),
    # and the path shows what the submit actually landed on. Selector + path
    # only — never a value.
    log.info("form flow: done ok=%s error=%s field=%s subs=%s status=%s path=%s",
             result.get("ok"), result.get("error"), diagnostics.get("field_attempt"),
             result.get("form_submissions"), result.get("status"),
             urlsplit(result.get("url") or "").path)
    return result

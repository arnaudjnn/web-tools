"""Generic single-attempt form execution. No target-specific or CAPTCHA logic.

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
import random
import re
import time
from urllib.parse import urlsplit, parse_qs


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
                   "ready_condition_met": None}
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
        page = context.new_page()
        page.on("request", request_started)
        page.on("requestfailed", request_failed)
        page.on("pageerror", page_error)
        page.on("response", response_received)
        navigation = page.goto(url, wait_until=wait_until, timeout=remaining())
        if navigation is not None and isinstance(navigation.status, int):
            diagnostics["navigation_status"] = navigation.status
        if wait_ms:
            page.wait_for_timeout(min(wait_ms, remaining()))
        if inspect_only:
            result["url"] = page.url
            # No page contents/hidden tokens in an inspection response.
            return result
        # Arrive like a person before touching anything: look around, scroll,
        # dwell. A submit seconds after navigation with no prior input reads
        # as automation no matter how human the typing itself is.
        page.mouse.move(random.randint(200, 1200), random.randint(150, 700),
                        steps=random.randint(8, 20))
        page.wait_for_timeout(min(random.randint(700, 2200), remaining()))
        page.mouse.wheel(0, random.randint(200, 600))
        page.wait_for_timeout(min(random.randint(400, 1200), remaining()))
        for selector in dismiss or []:
            try:
                page.locator(selector).first.click(timeout=remaining(2000))
            except Exception:
                pass  # Optional cookie banners; required fields below fail closed.
        phase = "fields"
        for field in fields:
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
        if ready_expression:
            phase = "readiness"
            diagnostics["ready_condition_met"] = False
            # Camoufox isolates ordinary evaluation from the page's globals.
            # The prefix opts into its main world (a JS label in other engines).
            while page.evaluate("mw:(" + ready_expression + ")") is not True:
                page.wait_for_timeout(remaining(100))
            diagnostics["ready_condition_met"] = True
        phase = "submit"
        # Exactly one click. The site's own handler supplies any CAPTCHA token.
        # Dwell first: a submit the instant the last field fills in is machine
        # timing, and the token must be minted after the interaction anyway.
        page.wait_for_timeout(min(random.randint(800, 2400), remaining()))
        diagnostics["submit_click_attempted"] = True
        page.locator(submit).click(timeout=remaining())
        phase = "outcome"
        try:
            if success_url:
                page.wait_for_url(re.compile(success_url), timeout=remaining(settle_ms))
            else:
                page.wait_for_load_state("networkidle", timeout=remaining(settle_ms))
        except Exception:
            pass  # Rejections stay on the form. Capture them, don't resubmit.
        result["url"] = page.url
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
        result["error"] = result["error"] or ("outcome_unknown" if result["form_submissions"] else f"{phase}_failed")
    return result

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
import logging
import random
import re
import time
from urllib.parse import urlsplit, parse_qs

# Phase logs are the ONLY visibility into a flow that never returns: the
# wedge handler sees a stackless greenlet and can only say "somewhere".
log = logging.getLogger("camoufox.forms")

# Live-step marker, polled by the admission worker from the event loop. A
# pre-POST driver roundtrip has no timeout we can trust: playwright's own
# `timeout=` is enforced by the driver's loop, and a wedged transport (the
# greenlet parked on a socket read) never reaches it — measured 2026-09-29:
# the fields phase sat in the driver's `select` for minutes past every
# timeout. So EVERY driver call before the submit click is marked around
# with a threshold ABOVE its own legitimate worst case (its timeout, or the
# keystroke duration for `type`): a mark older than its threshold means the
# call never returned, so no field was touched and no POST left the machine
# — retryable, unlike every other stall. Marks are strictly pre-POST; the
# submit click onwards is never marked (a click may already have POSTed).
_LIVE = {"name": "", "at": 0.0, "stuck": 8.0}
PRE_SUBMIT_STUCK_S = 8.0
PRE_SUBMIT_STEP = "arrival scroll"


def _mark(name, stuck_s=PRE_SUBMIT_STUCK_S):
    _LIVE["name"] = name
    _LIVE["at"] = time.monotonic()
    _LIVE["stuck"] = float(stuck_s)


def reset_live_step():
    _LIVE["name"] = ""
    _LIVE["at"] = 0.0
    _LIVE["stuck"] = PRE_SUBMIT_STUCK_S


def pre_submit_hang_step():
    """The mark name if the flow parked on a pre-POST driver call."""
    if _LIVE["name"] and time.monotonic() - _LIVE["at"] > _LIVE["stuck"]:
        return _LIVE["name"]
    return None


@contextlib.contextmanager
def _at(name, stuck_s=PRE_SUBMIT_STUCK_S):
    """Mark one driver call; always clear it, even when the call raises."""
    _mark(name, stuck_s)
    try:
        yield
    finally:
        reset_live_step()


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


def human_click(page, control, remaining, pre_submit=True) -> None:
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

    `pre_submit=False` (wizard steps after the first POST) drops the marks:
    see `_unmarked`.
    """
    at = _at if pre_submit else _unmarked
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
    tx = box["x"] + box["width"] / 2 + random.uniform(-box["width"] / 4, box["width"] / 4)
    ty = box["y"] + box["height"] / 2 + random.uniform(-4, 4)
    steps = random.randint(6, 18)
    sx, sy = tx - random.randint(100, 400), ty - random.randint(60, 200)
    # Input dispatch is the class that wedges (playwright gives it no
    # timeout of its own) — but camoufox ANIMATES trajectories: measured
    # 2026-09-29, every run parked here at the old 8s threshold with the
    # job alive, so the approach legitimately takes up to ~15s. 25s covers
    # 18 animated steps with margin; a true park still surfaces far before
    # deadline + grace.
    with at("pointer move", 25.0):
        for i in range(1, steps + 1):
            page.mouse.move(sx + (tx - sx) * i / steps, sy + (ty - sy) * i / steps)
            page.wait_for_timeout(random.randint(8, 30))
        page.mouse.click(tx, ty)


def run_form(context, *, url, fields, submit, dismiss=None, success_url=None,
             submission_urls=None, wait_until="domcontentloaded", wait_ms=0,
             settle_ms=20000, timeout_ms=120000, captcha_field=None, inspect_only=False,
             require_captcha_token=False, ready_expression=None,
             gate_text=None, step2=None, step2_submit=None, completion_markers=None):
    targets = validate_form(url, submission_urls, success_url, gate_text, completion_markers)
    deadline = time.monotonic() + timeout_ms / 1000
    result = {"contract_version": 2, "status": 0, "url": url, "html": "",
              "ok": False, "form_submissions": 0, "error": None}
    diagnostics = {"inspection_only": inspect_only, "captcha_script_requests": 0,
                   "captcha_script_responses": 0, "captcha_script_http_errors": [],
                   "captcha_network_failures": 0, "submit_click_attempted": False,
                   "token_present": None, "captcha_field_lengths": None,
                   "blocked_mutations": 0, "page_script_errors": 0,
                   "navigation_status": None, "captcha_guard_blocked": False,
                   "ready_condition_met": None, "field_attempt": None,
                    "phase": None, "failure_class": None, "dismiss_clicked": [],
                    "banner_visible": None, "field_state": None, "egress": None,
                    "wizard_gate_clicked": False, "wizard_step2": False}
    result["diagnostics"] = diagnostics
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
            if result["form_submissions"] == 0:
                # Token forensics belong to the FIRST (only tokened) POST;
                # the gate and step2 POSTs carry no captcha field and must
                # not overwrite or fail the record.
                try:
                    # keep_blank_values: an absent key then means "not in the
                    # form", [''] means "carried but empty" — the distinction
                    # between a server reading the custom field (minted) and
                    # the standard one (which the page may leave blank).
                    values = parse_qs(route.request.post_data or "", keep_blank_values=True)
                    names = [captcha_field] if captcha_field else ["g-recaptcha-response"]
                    diagnostics["token_present"] = any(
                        len(values.get(name, [])) == 1 and
                        values[name][0].strip().lower() not in ("", "null", "undefined", "false")
                        for name in names)
                    watch = sorted({n for n in (captcha_field, "g-recaptcha-response") if n})
                    diagnostics["captcha_field_lengths"] = {
                        name: [len(v) for v in values.get(name, [])] for name in watch}
                except Exception:
                    diagnostics["token_present"] = None
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
        with _at("new page"):
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
        # Which IP does this browser ACTUALLY egress from? The contract says
        # every run is pinned to the residential pool by PROXY_URL — but a
        # datacenter or direct egress mints reCAPTCHA tokens from the worst
        # possible IP and reads exactly like the 2026-09-29 collapse. Fetched
        # from inside the page, so it traverses the same proxy the form will.
        try:
            with _at("egress check", 12.0):
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
        with _at(PRE_SUBMIT_STEP):
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
                    with _at("dismiss click"):
                        target.click(timeout=remaining(2000))
                    diagnostics["dismiss_clicked"].append(selector)
                    clicked = True
                except Exception:
                    pass  # Optional cookie banners; required fields below fail closed.
            if clicked:
                page.wait_for_timeout(min(random.randint(400, 900), remaining()))
            try:
                with _at("banner check"):
                    diagnostics["banner_visible"] = bool(page.locator(dismiss[0]).first.is_visible())
            except Exception:
                diagnostics["banner_visible"] = None
            if not diagnostics["banner_visible"]:
                break
        log.info("form flow: fields (%d)", len(fields))

        def fill_fields(field_list, pre_submit=True):
            nonlocal control, phase
            at = _at if pre_submit else _unmarked
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
                        human_click(page, control, remaining, pre_submit=pre_submit)
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
                    human_click(page, control, remaining, pre_submit=pre_submit)
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
        fill_fields(fields)
        if ready_expression:
            phase = "readiness"
            log.info("form flow: readiness")
            diagnostics["ready_condition_met"] = False
            # Camoufox isolates ordinary evaluation from the page's globals.
            # The prefix opts into its main world (a JS label in other engines).
            # Each iteration re-marks, so the loop may legitimately run for
            # minutes without ever looking parked — only one stuck call does.
            while True:
                with _at("ready check"):
                    met = page.evaluate("mw:(" + ready_expression + ")") is True
                if met:
                    break
                with _at("ready pause"):
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

            while True:
                if time.monotonic() >= deadline:
                    break
                # One evaluate per tick: url, both step detectors, the error
                # nodes, the gate button, and the body text (completion
                # markers live in the text, not the url).
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
                            page.wait_for_timeout(min(random.randint(400, 900), remaining()))
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
                    break
                try:
                    page.wait_for_timeout(min(random.randint(700, 1300), remaining()))
                except TimeoutError:
                    break  # polled to the deadline: incomplete, not unknown
        result["url"] = page.url
        log.info("form flow: capturing content")
        result["html"] = page.content()
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

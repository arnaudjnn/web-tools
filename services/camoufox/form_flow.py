"""Generic single-attempt form execution. No target-specific or CAPTCHA logic.

The caller owns durable reservations. A lost HTTP response is UNKNOWN and must
not be retried automatically. The context guard only prevents duplicate POSTs
within this operation; it is not cross-request idempotency.
"""
import re
import time
from urllib.parse import urlsplit


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


def run_form(context, *, url, fields, submit, dismiss=None, success_url=None,
             submission_urls=None, wait_until="domcontentloaded", wait_ms=0,
             settle_ms=20000, timeout_ms=120000):
    targets = validate_form(url, submission_urls, success_url)
    deadline = time.monotonic() + timeout_ms / 1000
    result = {"contract_version": 2, "status": 0, "url": url, "html": "",
              "ok": False, "form_submissions": 0, "error": None}
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
        if is_submission(route.request):
            if result["form_submissions"]:
                route.abort("blockedbyclient")
                return
            result["form_submissions"] = 1
        route.continue_()

    def response_received(response):
        if is_submission(response.request):
            result["status"] = response.status

    try:
        context.route("**/*", guard)
        page = context.new_page()
        page.on("response", response_received)
        page.goto(url, wait_until=wait_until, timeout=remaining())
        if wait_ms:
            page.wait_for_timeout(min(wait_ms, remaining()))
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
                control.check(timeout=remaining())
            elif action == "select":
                control.select_option(field.get("value"), timeout=remaining())
            elif action == "type":
                control.fill(field.get("value") or "", timeout=remaining())
            else:
                raise ValueError("Unknown field action")
        phase = "submit"
        # Exactly one click. The site's own handler supplies any CAPTCHA token.
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
        if not result["form_submissions"]:
            result["error"] = "no_submission"
        elif not result["status"]:
            result["error"] = "outcome_unknown"
    except Exception:
        # Never expose field values, page exception text or proxy credentials.
        result["error"] = "outcome_unknown" if result["form_submissions"] else f"{phase}_failed"
    return result

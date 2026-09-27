"""CapSolver solving for forms whose caller explicitly asks for it.

Invoked ONLY when the caller passes `captcha` to /form-submit. Without
CAPSOLVER_API_KEY on the Camoufox service the solver reports `unavailable`
and the form fails closed BEFORE the submit click — an absent key can never
become a half-submitted form. The solution travels straight from the
provider response into the page's token field: it is never logged, stored
or returned to the caller.

Failure surfaces as SolverError(kind) where kind is `unavailable` (no key)
or `failed` (network, provider error, empty token or deadline); the caller
maps it to a structured zero-submission result. The form's own deadline
(`remaining`, milliseconds, may raise TimeoutError) bounds every wait —
there is no retry loop here, and a form operation stays single-attempt.

When the caller passes `proxy` — the very Playwright proxy dict the form
browser runs on — the mint leaves through that exit and the proxy task
family is used. This is not cosmetic: a gate that compares the token's
mint IP against the submitting IP rejects a provider-side (proxyless) mint
("Error verifying reCAPTCHA" on a completed POST), so a form that posts
from an exit must also mint from it. A malformed proxy fails closed before
any HTTP rather than silently dropping back to proxyless.

HTTP is stdlib urllib: the form worker is a plain thread, and the solver
must add no dependency to the image.
"""
import json
import os
import time
import urllib.request
from urllib.parse import urlsplit

# Module attrs so tests can point at a loopback provider; the key is read
# from the environment at import (Railway env is fixed for the process life).
API_URL = os.environ.get("CAPSOLVER_API_URL", "https://api.capsolver.com")
API_KEY = os.environ.get("CAPSOLVER_API_KEY", "")

POLL_MS = 1500
_HTTP_TIMEOUT_S = 10
# The loop stops when the remaining budget could not survive another wait +
# request round-trip, rather than raising mid-sleep.
_POLL_MARGIN_MS = 500

_TASK_TYPES = {"v3": "ReCaptchaV3TaskProxyLess", "v2": "ReCaptchaV2TaskProxyLess"}
_PROXY_TASK_TYPES = {"v3": "ReCaptchaV3Task", "v2": "ReCaptchaV2Task"}


class SolverError(Exception):
    """kind: `unavailable` (no key) or `failed` (everything else).

    The message is deliberately generic: provider error descriptions must
    not travel into results or logs (see FORMS.md diagnostics rules).
    """

    def __init__(self, kind):
        super().__init__(f"captcha solver {kind}")
        self.kind = kind


def _post(path, payload, budget_ms):
    timeout = max(1, min(_HTTP_TIMEOUT_S, int(budget_ms) // 1000))
    request = urllib.request.Request(
        API_URL.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _proxy_payload(proxy):
    """Playwright proxy dict → CapSolver's `proxy` string.

    The dict is exactly what the form browser navigates with, so the mint and
    the submit leave from the same exit IP. Anything unusable here (no server,
    no port, odd scheme) means that guarantee cannot be held — fail closed
    before any HTTP instead of quietly solving proxyless from other IPs.

    CapSolver wants ONE string ("http://user:pwd@host:port" per their proxy
    guide) — the documented object form (`proxyType`/`proxyAddress`/…) is not
    what the `proxy` field of ReCaptcha*Task takes, and a JSON object there is
    rejected with a parse error.
    """
    try:
        parsed = urlsplit(proxy["server"])
        if parsed.scheme not in ("http", "https", "socks4", "socks5") \
                or not parsed.hostname or not parsed.port:
            raise ValueError("unusable proxy")
        user = proxy.get("username") or ""
        password = proxy.get("password") or ""
        auth = f"{user}:{password}@" if user else ""
        return f"{parsed.scheme}://{auth}{parsed.hostname}:{parsed.port}"
    except Exception:
        raise SolverError("failed") from None


def solve(*, sitekey, page_url, action=None, version="v3", remaining, proxy=None):
    """Mint a reCAPTCHA token. `remaining` is form_flow's remaining(): milliseconds,
    raises TimeoutError when the form deadline passes. `proxy` is the form
    browser's own Playwright proxy dict (see module docstring): when given,
    the mint leaves from the form's exit and the proxy task family is used.

    Polling is part of ONE attempt — a provider stall is a failed form, never
    a second submission opportunity.
    """
    if not API_KEY:
        raise SolverError("unavailable")
    task_type = (_PROXY_TASK_TYPES if proxy is not None else _TASK_TYPES).get(version)
    if task_type is None:
        raise SolverError("failed")
    task = {"type": task_type, "websiteURL": page_url, "websiteKey": sitekey}
    if action:
        task["pageAction"] = action
    if proxy is not None:
        task["proxy"] = _proxy_payload(proxy)
    budget = remaining()  # outside try: the form deadline is not a solver failure
    try:
        created = _post("/createTask", {"clientKey": API_KEY, "task": task}, budget)
    except Exception:
        raise SolverError("failed") from None
    task_id = created.get("taskId")
    if created.get("errorId") or not task_id:
        raise SolverError("failed")
    while True:
        budget = remaining()
        if budget < POLL_MS + _POLL_MARGIN_MS:
            raise SolverError("failed")
        time.sleep(POLL_MS / 1000)
        try:
            polled = _post("/getTaskResult", {"clientKey": API_KEY, "taskId": task_id},
                           remaining())
        except Exception:
            raise SolverError("failed") from None
        if polled.get("errorId") or polled.get("status") == "failed":
            raise SolverError("failed")
        if polled.get("status") == "ready":
            token = (polled.get("solution") or {}).get("gRecaptchaResponse")
            if not token:
                raise SolverError("failed")
            return token

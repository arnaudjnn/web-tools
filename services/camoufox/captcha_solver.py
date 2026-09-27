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

HTTP is stdlib urllib: the form worker is a plain thread, and the solver
must add no dependency to the image.
"""
import json
import os
import time
import urllib.request

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


def solve(*, sitekey, page_url, action=None, version="v3", remaining):
    """Mint a reCAPTCHA token. `remaining` is form_flow's remaining(): milliseconds,
    raises TimeoutError when the form deadline passes.

    Polling is part of ONE attempt — a provider stall is a failed form, never
    a second submission opportunity.
    """
    if not API_KEY:
        raise SolverError("unavailable")
    task_type = _TASK_TYPES.get(version)
    if task_type is None:
        raise SolverError("failed")
    task = {"type": task_type, "websiteURL": page_url, "websiteKey": sitekey}
    if action:
        task["pageAction"] = action
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

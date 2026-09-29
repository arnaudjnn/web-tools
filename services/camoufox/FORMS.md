# Single-attempt forms

`web_form_submit` (Tools API) delegates to Camoufox `/form-submit`. Domain
selectors and business validation stay with the consumer. The browser service
owns navigation, required-field filling, isolated cookies, cleanup and a
context-wide one-POST guard. It lets the page's own submit handler run; unless
the caller passes `captcha` (below) it supplies no CAPTCHA answer, and it never
promises provider acceptance.

Version 2 adds `contract_version: 2`, `form_submissions: 0 | 1`, and nullable
`error`. `status` is the actual matching POST response, not the initial GET.
`submission_urls` optionally names same-origin POST endpoints sharing the same
budget (defaults to `url`; query strings are ignored). `ok` requires one POST,
a 2xx/3xx response and a matching `success_url`.

Required field errors prevent the click. Form jobs never use the read-only
browser recovery/retry wrapper. Their deadline starts before queueing; expired
jobs cannot later start a form submission. Browser contexts block service
workers so form requests remain visible to interception.

Forms own a short-lived browser and fresh worker thread, separate from the
shared render/Akamai browsers. `/recycle` cannot close a form's browser. Admission
is serial per replica, including while a disconnected caller's operation finishes.
Launch and queue time consume the request deadline. A launch/context failure or
pre-navigation deadline returns a structured result with **zero submissions**;
unexpected failures after execution begins remain unknown and are never retried.
One structured failure is explicit about being pre-POST: if the flow parks on
the unbounded arrival `mouse.wheel` (playwright gives input dispatch no
timeout — the full-viewport `mouse.move` that used to open the arrival is
retired: it was the input call that kept wedging), the worker answers **503**
with `detail = {message, retryable: true}`
within seconds — no field was touched, no POST left the machine, so that
attempt's identity may be replayed. The leaked browser is shed with the
process (`FORM_WEDGE_EXIT_S`, 3 in the image), so the retry lands on a
restarted container. Cleanup failure cannot overwrite an already captured form
result. A host that cannot launch at all (user namespaces revoked — every
launch fails in every mode until the container is moved) is shed the same way:
`LAUNCH_FAIL_STREAK` consecutive launch failures across any path exit the
process after `LAUNCH_FAIL_SHED_S` seconds of grace (`3`/`10` in the image;
0 disables), and a success in between retracts the pending exit.

Headed forms additionally need `DISPLAY :99`, and a Railway restart reuses the
container's writable layer: the dead X server's `/tmp/.X99-lock` and
`/tmp/.X11-unix/X99` made the next Xvfb refuse to start ("Server is already
active for display 99") and every headed launch fail from then on — while
headless renders kept answering and hid it (measured 2026-09-29: 6/6 form
launches failed across several restarts; only a redeploy, which recreates the
filesystem, cured it). `entrypoint.sh` probes the display every few seconds,
clears the stale files when nothing answers and starts Xvfb again — covering
both the restart and an Xvfb killed mid-life (OOM).

The Docker image pins the browser build as well as the Python wrapper. CI runs
the real Linux image against loopback form fixtures, including five consecutive
isolated browser lifetimes. These are runtime tests, not proof of CAPTCHA acceptance.

This is not cross-request idempotency: callers must persist their operation or
identity reservation BEFORE sending the HTTP request. A lost response or 502
means unknown, never zero submissions or permission to replay. Do not add a
generic HTTP retry policy to this endpoint — the ONLY sanctioned replay is the
explicit 503 `retryable: true` above (bounded by the caller, e.g. two attempts
ten seconds apart). Site rejections are returned as
page content for the caller to interpret. Neither fields nor exception payloads
are logged by the form runner.

`diagnostics` contains passive CAPTCHA script request/response counts, failing
HTTP statuses, network/page-script error counts, whether the submit click was
attempted, nullable `token_present`, and the solver's `solver_attempts` /
`solver_status` enum (`solved | unavailable | failed | field_missing`, null when
`captcha` was not requested). Token presence inspects only the first
outgoing form POST (`captcha_field`, default `g-recaptcha-response`); diagnostics
never include tokens, request bodies, query strings or exception messages.
For a failed run it also carries `failure_class` (the exception's class NAME —
which failure kind, never its message), `field_attempt` (the selector in
flight), `phase`, `dismiss_clicked` (cookie-banner selectors actually clicked),
`banner_visible` (whether the first dismiss target was still showing right
before the fields phase — an overlay that never went away blocks the first
click and looks like a slow field) and `field_state` (the failing field's
visible/enabled booleans at failure time).
A token's presence does NOT prove validity, action, score or server acceptance.
Likewise, a loaded script does not prove its handler ran. The solver below is
the only way this service mints a token.

`captcha: { sitekey, action?, version? }` mints the token via CapSolver AFTER
the human interaction and BEFORE the single click, and only when the Camoufox
service holds `CAPSOLVER_API_KEY`. By default the mint leaves through the form's
OWN exit — the same proxy and `exit_session` the browser navigates and POSTs
with — because a gate that compares the token's mint IP against the submit IP
rejects a provider-side (proxyless) mint; an exit that cannot be parsed fails
closed (`captcha_solver_failed`) instead of silently solving from other IPs.
`captcha_proxyless: true` opts OUT of that: the solver gets no proxy and mints
from its own infrastructure. Use it when the form's exit scores 0 anyway —
reCAPTCHA v3 scores IP REPUTATION, and measured 2026-09-26..29 the default
exit accepted 1 of 18 posted submissions whatever minted the token, while the
same pages from a clean IP pass 85%. The form POST itself always leaves
through the form's exit either way. A form with no exit at all still solves
proxyless, as before. Without the key the
result is `captcha_solver_unavailable`; a provider error, stall, empty token or
deadline is `captcha_solver_failed`; a page with no matching token textarea is
`captcha_field_missing`. All three are zero submissions and no click — a solver
outage cannot become a half-submitted form. The token is written only into the
CAPTCHA field (`captcha_field`, default `g-recaptcha-response`) and is never
logged, stored or returned. The solve consumes the operation's own deadline and
is part of the one attempt; there is no solver retry. `version` picks the task
family (default `v3`); `v2` does not tick a visible checkbox — a site whose
gate is the widget still needs its own handler.
Pair with `require_captcha_token` so the outgoing POST is checked for presence.

`ready_expression` optionally waits for a caller-supplied boolean expression in
the page's main world before the single submit click. This is important with
Camoufox: ordinary evaluation cannot see the page's globals. The condition uses
the operation's existing deadline; a failure never clicks submit.

`require_captcha_token: true` prevents a matching POST from leaving the browser
when its configured CAPTCHA field is missing, empty, ambiguous, or unreadable.
The result is `captcha_token_missing`, `form_submissions: 0`, and
`diagnostics.captcha_guard_blocked: true`. Further matching POSTs are blocked too;
there is no delayed retry after an empty token. This guard prevents a known-bad
submission, but cannot determine whether a populated token will be accepted.

`inspect_only: true` navigates without filling or clicking and blocks same-origin
mutating requests. It returns no HTML and never reports form success. Use this
to check script delivery without consuming a mailbox or creating an account.
It cannot measure a token generated only on submit: `token_present` stays null.

Tests (only our loopback fixture receives submissions):

```
python -m unittest discover -s services/camoufox -p 'test_form*.py' -v
FORM_BROWSER_TEST=chromium python -m unittest discover -s services/camoufox -p 'test_form*.py' -v
```

The browser test also supports `FORM_BROWSER_TEST=camoufox` in the deployed
image. Deploy Camoufox and Tools before a consumer requiring contract version 2.

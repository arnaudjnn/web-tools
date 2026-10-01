# Single-attempt forms

`web_form_submit` (Tools API) delegates to Camoufox `/form-submit`. Domain
selectors and business validation stay with the consumer. The browser service
owns navigation, required-field filling, isolated cookies, cleanup and a
context-wide one-POST guard. It lets the page's own submit handler run and
supplies no CAPTCHA answer itself; it never promises provider acceptance.

Version 2 adds `contract_version: 2`, `form_submissions`, and nullable
`error`. `status` is the actual matching POST response, not the initial GET.
`submission_urls` optionally names same-origin POST endpoints sharing the same
budget (defaults to `url`; query strings are ignored). `ok` requires at least
one POST, a 2xx/3xx response and a matching `success_url` — or, on a wizard
(below), a completion marker in the body text.

A wizard (`gate_text`, `step2`, `step2_submit`, `completion_markers`) is a
multi-step form: after the single fill+submit, the answer may be a gate
button (`gate_text`, a regex on its text — clicked ONCE, never twice), a
second step whose own fields+submit (`step2`, default selector `form
button`) are filled only when that step renders, or a completion that never
changes the URL (`completion_markers`: regexes on body text — the site's
manual-review end state). The POST guard therefore allows up to three
POSTs, seconds apart; a same-click double-fire or a fourth aborts. Step0
rejections render as errors ON the form and return `wizard_rejected` (a
real answer: replayable under the caller's own rejection rules); a reset
after step2 is `wizard_reset` and a deadline mid-wizard is
`wizard_incomplete` — both may conceal a performed POST and are never
replayed. All driver calls AFTER step0's POST run without retryable marks:
a park there is an unknown outcome, never a 503.

Required field errors prevent the click. Form jobs never use the read-only
browser recovery/retry wrapper. Their deadline starts before queueing; expired
jobs cannot later start a form submission. Browser contexts block service
workers so form requests remain visible to interception.

Forms own a short-lived browser and fresh worker thread, separate from the
shared render/Akamai browsers. `/recycle` cannot close a form's browser. Admission
is serial per replica, including while a disconnected caller's operation finishes.
Launch and queue time consume the request deadline. A launch failure gets ONE
retry inside the worker (the first launch of a fresh process fails transiently
— tunnel coming up, exit cycling — and it is pre-navigation, so zero
submissions either way); a launch/context failure after that, or a
pre-navigation deadline, returns a structured result with **zero submissions**;
unexpected failures after execution begins remain unknown and are never retried.
One structured failure is explicit about being pre-POST: every driver call
before the submit click runs inside a live-step mark whose threshold sits
above that call's own legitimate worst case (its timeout, the keystroke
duration for `type`), because playwright's timeouts are enforced by the
driver's loop and never fire once its transport is stuck — measured
2026-09-29: the fields phase sat in the driver's `select` for minutes past
every timeout. A mark older than its threshold means the call never
returned, and the worker answers **503** with
`detail = {message, retryable: true}`
within seconds — no field was touched, no POST left the machine, so that
attempt's identity may be replayed. The same 503 answers the structured
failures that prove the same thing (`_retryable_zero_post`): `fields_failed`
(died before the submit click), `navigation_failed`, or
`browser_launch_failed`, each with the guard's submission count still zero.
After the click nothing is provable — `no_submission` in particular can still
have the page's native submit in flight when the wait expires — so those stay
unknown and are never replayed. The leaked browser is shed with the
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
attempted, and nullable `token_present`. Token presence inspects only the first
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
Likewise, a loaded script does not prove its handler ran.

This service NEVER mints a CAPTCHA token. The page's own recaptcha
integration mints on interaction (the human typing and clicking above is the
interaction), and the observation fields above are the only CAPTCHA surface
this endpoint accepts. Provider-side solving (CapSolver, and its
`captcha_proxyless` variant) was removed 2026-09-29 with its
`CAPSOLVER_API_KEY`: measured on the form's own egress, page-minted tokens
passed whenever any token passed, so the third-party credential bought
nothing. A caller that needs a rejection retried rotates its exit and
re-attempts — never a solver.

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

`profile: "<name>"` launches the form in a NAMED persistent context
(`FORM_PROFILE_DIR/<name>/`, default under the system temp dir): cookies and
a fingerprint the target has already seen are reused across submissions,
which is what a per-session score (reCAPTCHA v3) responds to. The first
launch's options are persisted to `fingerprint.json`; later launches reload
them and override only the request-scoped fields (exit/proxy, headless).
Omit `profile` for the default isolated browser per submit. Profiles are
per-replica (ephemeral filesystem) and never opened concurrently — the form
worker's admission is serial.

Tests (only our loopback fixture receives submissions):

```
python -m unittest discover -s services/camoufox -p 'test_form*.py' -v
FORM_BROWSER_TEST=chromium python -m unittest discover -s services/camoufox -p 'test_form*.py' -v
```

The browser test also supports `FORM_BROWSER_TEST=camoufox` in the deployed
image. Deploy Camoufox and Tools before a consumer requiring contract version 2.

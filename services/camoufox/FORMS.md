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
Launch and queue time consume the request deadline. A launch gets up to
three attempts inside the worker, 2 s then 5 s apart (the first launch of a
fresh process fails transiently — tunnel coming up, exit cycling — and so
does a persistent profile whose previous browser is still letting go of it:
2026-10-02 15:43, two `TargetClosedError` refusals 2 s apart, the next launch
6 s later fine; it is pre-navigation, so zero submissions either way;
`form-run.launch_errors` lists the class of each failed attempt); a
launch/context failure after that, or a
pre-navigation deadline, returns a structured result with **zero submissions**;
unexpected failures after execution begins remain unknown and are never retried.
One structured failure is explicit about being pre-POST: every driver call
before the submit click runs inside a live-step mark whose threshold sits
above that call's own legitimate worst case (its timeout, the keystroke
duration for `type`), because playwright's timeouts are enforced by the
driver's loop and never fire once its transport is stuck — measured
2026-09-29: the fields phase sat in the driver's `select` for minutes past
every timeout. Marks live on a per-job `FormLive` object (`form_flow.py`),
created by the endpoint and passed to both the worker (which polls it) and
the flow (which writes it) — never a module global, so one job's mark cannot
age into the next one's poll. A mark older than its threshold means the call never
returned, and the worker answers **503** with
`detail = {message, retryable: true}`
within seconds — no field was touched, no POST left the machine, so that
attempt's identity may be replayed. The same 503 answers the structured
failures that prove the same thing (`_retryable_zero_post`): `fields_failed`
(died before the submit click), `navigation_failed`, `captcha_unavailable`
(below), or `browser_launch_failed`, each with the guard's submission count
still zero.
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

Pointer input is ONE `mouse.move` per click (to an off-centre point of the
control, then a short beat, then the press). The form browser launches with
`humanize=True`, so Camoufox already draws a human trajectory for every
dispatched move; the former 6–18 manual steps were double humanization —
6–18× the dispatches, each a chance at the input-chain deadlock that parked
32 attempts at `pointer move` (and plausibly 12 more at `field scroll`, the
next call behind it) on 2026-10-01. Click targets never sit on `x<=1`/`y<=1`:
a trajectory point on a viewport axis deadlocks Camoufox's input chain
(daijro/camoufox#751, unfixed in the pinned 152.0.4-beta.30; fixed upstream in
the 156.0.1-beta.32/.33 prereleases), and the old stepped approach started
100–400 px left / 60–200 px above the target, i.e. off-screen and clamped onto
exactly that axis for any field near the left or top edge.

Navigation retries in-run, strictly before any input: up to 3 attempts with
2 s / 4 s backoff for a transient refusal (`NS_ERROR_*CONNECTION_REFUSED`,
proxy/net errors, an interrupted navigation) or a page closed on arrival
(`TargetClosedError` — replaced by a fresh page in the same context), a page
that failed to open at all (`new_page_failed`), or a `goto` timeout while at
least `NAV_RETRY_MIN_LEFT_S` (60 s) of the deadline remains. It is a GET; the
guard has seen no POST. `goto` itself is capped at 30 s (`NAV_TIMEOUT_MS`;
every successful cf156 navigation took ≤10 s, and the one timeout sat the old
60 s cap and left no budget to retry). The readiness gate (`ready_expression`)
is bounded at `READY_WAIT_S` (30 s): passing runs meet it within
milliseconds, and every 2026-10-01 `readiness_failed` instead polled away the
whole remaining deadline (~2 min).

The page a job drives is the launch's own tab when the context has one
(`first_page`): a persistent `profile` context launches INTO a blank tab, and
asking it for a second one is what parked at `'new page'` twice on cf156
(2026-10-02 15:36 and 15:43, both `bench-cf156a-warm`; isolated contexts,
which start with no page, never parked there in 41 runs). Isolated contexts
still get `new_page()`, marked at `NEW_PAGE_STUCK_S`. Inspect, warm and the
exit pre-check use the same helper. `form-run.page_reused` says which.

**reCAPTCHA readiness gate.** When the page requested a reCAPTCHA script, the
flow checks — after arrival's wait and before ANY input — that the page's
client can mint, polling up to `CAPTCHA_READY_WAIT_S` (8 s). Signals, strongest
first: an attached anchor frame (`/recaptcha/{api2,enterprise}/anchor`) must
carry `#recaptcha-token`; else the main world must expose
`grecaptcha.execute` (`mw:`; the api.js stub defines only `ready`); else the
`recaptcha__*.js` body must have arrived (`requestfinished`). Unusable → ONE
reload (no field touched, no POST possible); still unusable →
`captcha_unavailable`, zero POSTs, 503 retryable — never a submit with an
empty token. Why: on cf156 5/33 oracle POSTs carried no token, all with
`captcha_scripts` `[2,2,1]` and a verdict with no `t_submit`
(`page_dwell_s`/`mint_error` null) — the submit listener the page attaches
inside `grecaptcha.ready()` never existed, so the click fell through to a
native submit (missing-input-response). The counts cannot see it: a body cut
after its headers is a response AND a failure. `inspect_only` is never gated.
Each failed reCAPTCHA request is recorded in `diagnostics.captcha_failed` /
`form-run.captcha_failed` as `{path, type, code, after_response}` — `path` a
CLASS (`api.js`, `recaptcha__*.js`, `anchor`, `bframe`, `reload`, `clr`,
`webworker.js`, `styles__*.css`, `other`), never a query string.

Every job logs exactly one `form-run {json}` line — on return, on a
structured failure, on an escaping exception, and (from the worker) on a
queue timeout, a park or a wedge. It is the measurement source and carries
NO field values, tokens, bodies or exception messages: `error`, `ok`,
`form_submissions`, `status`, `phase` (furthest reached), `failed_phase`,
`failure_class`, `parked_step`, `field` (selector), `durations_s` per phase
(queue, launch, navigation, arrival, fields, readiness, submit, outcome,
teardown), `total_s`, `posts` (`[{n, token, mint_age_s}]`), `token_present`,
`egress` (`{country, asn}`), `nav_attempts`, `nav_error` (an engine code such
as `NS_ERROR_CONNECTION_REFUSED`, never the message), `ready_met`,
`captcha_scripts` (`[requests, responses, network_failures]`),
`captcha_ready` / `captcha_signal` / `captcha_reload` / `captcha_failed`
(the gate above), `page_reused`,
`submit_clicked`, `inspect_only`, `wizard`, `stop_after_posts`,
`launch_attempts`, `launch_errors`, `profile`, `headed`, and `camoufox` (wrapper version /
`CAMOUFOX_BUILD` browser pin). Count G1 straight from it:
`railway logs --service Camoufox --filter '"form-run"'`. The human-readable
`form flow: done …` line also fires on every return path now (inspect_only
and `stopped_after_posts` used to skip it).

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
Camoufox: ordinary evaluation cannot see the page's globals. The condition is
polled for at most `READY_WAIT_S` (30 s) inside the operation's deadline; a
failure (`readiness_failed`, zero POSTs, 503 retryable) never clicks submit.

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
per-replica and never opened concurrently — the form worker's admission is
serial. Without `FORM_PROFILE_DIR` they live in the image's temp dir and are
wiped by every redeploy; production mounts a Railway volume there.

`sticky_exit: true` (or `FORM_PROFILE_STICKY_EXIT=1` as the default) pins a
profile to ONE exit: the Evomi session token is stored under the reserved
`_web_tools` key of `fingerprint.json` (stripped before Camoufox sees the
options) and reused on every later run; `fresh_ip` no longer rotates it, an
explicit `exit_session` overrides it (and becomes the new pin). The pin is
written before launch, so an unknown outcome still keeps the identity on the
exit it was seen from. A profile whose saved `executable_path` no longer
exists (a browser upgrade on the volume) redraws its fingerprint and keeps
its cookies. The token selects an exit; the provider may still recycle the
IP behind it, which `exit_ip`/`exit_ip_changed` make visible.

## Score oracle and probe (`score_probe.py`)

Our own reCAPTCHA v3 key scores the form browser: Tools serves the page and
does siteverify (`/oracle/recaptcha`, the only public hostname registered on
the key); `/form-score-probe` runs `run_isolated_form` + `_form_browser` +
`run_form` against it — the forms' own path, not a copy — and returns the
verified score and egress. `/form-warm` (google, youtube, target origin;
dwell, scroll, consent) and `/form-exit-select` (pre-check an exit's IP/ASN
against `exit-blocklist.json` next to the profiles, probe it isolated, pin
the first passing one) reuse the same launch path with their own runner.
Tune against this oracle only, never a third-party form; the bench is
`.claude/skills/tools-health/scripts/score_bench.py`.

`/form-inspect` (Tools: `web_form_inspect`, module `form_inspect.py`) is
the read-only step an agent takes BEFORE submitting. It runs under the same form
worker admission, the same `_form_browser` launch path (exit, `headed`,
`profile`) and the same `run_isolated_form` launch retry and teardown. It never
fills, clicks or dismisses anything, and its route guard aborts every
non-GET/HEAD/OPTIONS request to ANY origin. That is stricter than
`inspect_only`, which blocks same-origin only: an inspection has no reason to
send even a beacon. For each visible form it returns stable selectors (`#id`,
else `tag[name="..."]`, scoped by the form when that name is ambiguous), the
`/form-submit` field `action`, labels, `required` (HTML, aria, or a trailing
`*`), select/radio options, submit candidates and honeypot candidates. A
hidden, offscreen, aria-hidden or `tabindex=-1` text input counts as a
honeypot. So does a trap-like name. A hidden container holding several
controls does not: that is a later wizard step. Each form also gets a
`suggested` `/form-submit` skeleton. Page-wide, it reports CAPTCHA providers
(reCAPTCHA v2/invisible/v3/enterprise, hCaptcha, Turnstile; presence of a
sitekey, never the key), cookie-banner dismiss candidates and wizard hints.
It never returns VALUES: not typed, default, selected or hidden ones (only the
names of hidden inputs).

Four rules come from Atoka's live output (2026-10-02):

- The CAPTCHA `token_field` is the field the page's own integration writes
  into, preferred over the widget's response textarea. A hidden input carrying
  `.g-recaptcha` or `data-sitekey`, or one whose name says captcha, wins:
  django-recaptcha V3 writes into `0-captcha`, not `g-recaptcha-response`.
  Each form lists its `captcha_fields` in that order, and
  `suggested.captcha_field` takes the first.
- A honeypot is never a CAPTCHA response field, a CSRF/state field or a
  consent toggle. A hidden input (even `type=hidden`) whose name is a visible
  field's name plus `_last`, `_confirm`, `_2`, `-hp` and the like is a
  honeypot: `0-email_last` shadows `0-email`. So is a visible input with
  `tabindex=-1` plus `autocomplete=off` or a trap-like name.
- Server-side wizards (django-formtools) are recognised by a
  `*-current_step` field, `wizard_goto_step` buttons and step-prefixed names
  (`0-email`). `wizard.fields` names them, and the hint gives the next step's
  prefix for `step2`.
- Form selectors are tried in this order: `form#id`, `form[action]`,
  `form:has([name=first field])`, `form[name]`. Submit selectors are `#id`,
  then `<form> button[type="submit"]`-style. Every selector is checked for
  uniqueness in the page. A form of cookie-purpose toggles, or one inside a CMP
  container, is `kind: "consent"`: it is ranked last and gets no `suggested`. Every failure is a 503 `retryable: true`, since
nothing can have been submitted. A form inside a child frame carries
`frame_url`; `/form-submit` drives only the main frame.

The toolkit turns a failed `/form-submit` into structured JSON
(`isError: true`), so agents never parse an error string:

- a 503 with `retryable: true` becomes `{ok:false, retryable:true, outcome:"not_submitted", form_submissions:0}`;
- a 400 or 422 becomes `{retryable:false, outcome:"invalid_request"}`;
- anything else (a 502, a lost response, a client timeout) becomes `{retryable:false, outcome:"unknown", form_submissions:null}`.

An answered run always carries `retryable: false`.

Tests (only our loopback fixture receives submissions):

```
python -m unittest discover -s services/camoufox -p 'test_form*.py' -v
FORM_BROWSER_TEST=chromium python -m unittest discover -s services/camoufox -p 'test_form*.py' -v
```

The browser test also supports `FORM_BROWSER_TEST=camoufox` in the deployed
image. Deploy Camoufox and Tools before a consumer requiring contract version 2.

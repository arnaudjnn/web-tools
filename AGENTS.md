# web-tools — the map

Five Railway services: **Tools** (Next-less Node server: MCP + REST + CLI host),
**Scrapling** sidecar, **Camoufox** sidecar, **SearXNG**, **Redis**. Forms run
on the one Camoufox (a forms-only role exists but measured no gain, below). Public endpoint
is Tools only; the sidecars are internal. Auth is `Authorization: Bearer <API_KEY>`
(the REST routes read only that header or `?api_key=` — `X-API-Key` does not work).

README is the user-facing story; RAILWAY.md is the template/hosting story. What
lives here is the map, the benchmark that shaped the architecture, and the traps.

## Two fetchers, one router

- `packages/toolkit/src/routing.ts` `pickBackend` decides the backend **by host**;
  callers never choose. Italian sources and `CAMOUFOX_HOSTS` (trustpilot, with
  `forcedWaitMs: 20000`) → Camoufox; everything else → Scrapling. Camoufox also
  owns `web_bytes`, `web_eval`, `web_spa_fetch`, forms.
- `functions.ts` `routed()` is the one fallback: preferred sidecar first, the other
  second, the error names both causes. **Every URL tool goes through it** —
  fetch/html/crawl, screenshot, pdf, execute_js. It does **not** fall back on a
  sidecar 4xx (the request itself was refused — replaying a script elsewhere would
  run its side effects twice). PDF is Chromium-only: for a Camoufox host,
  Camoufox renders and Scrapling's `/pdf` prints that DOM (`html` field, the
  navigation is route-fulfilled, scripts stripped). `web_execute_js` on Camoufox
  sequences the scripts into one `/eval` expression.
- **Markdown survives a Scrapling outage**: `markdown.ts` tries `/markdown`, then
  renders locally (turndown + domino, same noise/hidden strip, `<base href>`,
  links absolutised). `web_crawl` results carry `renderer: scrapling|local` —
  assert on it. Wayback GETs fall back from Scrapling `/raw` to Camoufox `/bytes`
  (residential either way; never from the Tools process).
- `sidecar.ts` is the one HTTP client for both sidecars, with a circuit breaker
  that trips **only** on unreachable (ENOTFOUND/ECONNREFUSED/…), 60 s, never on a
  timeout or HTTP error. No retries anywhere (forms: a lost response may hide a
  submission). Client aborts: Scrapling timeout+25 s, Camoufox +30 s, forms +60 s.
- One result shape: every tool returns an MCP `ToolResult`; `functionMap` wraps
  each in `instrument()` (counts calls/bytes/errors for **all 17** tools (incl. `web_form_inspect`, `web_agent`), turns a
  throw into `isError`). JSON-native tools (`output:'data'`: search, snapshots,
  archive, usage_stats) also set `data`, which REST returns bare (500 `{error}` on
  failure) — the v0 contract `health.py` parses. Screenshots are MCP `image`,
  PDFs an embedded `resource`; REST downgrades both to the old base64 text.
- REST validates with the same zod schemas as MCP (`validateParams`): 400
  `{error:'invalid_params', issues}`. SearXNG failures throw (no silent `[]`);
  zero results with `unresponsive_engines` is an error naming them.
- `web_crawl` is sequential, max 20 URLs, 300 s overall deadline (each fetch's
  timeout shrinks to fit), shaped `{results:[{url,status_code,success,mode,
  renderer,markdown}]}`; a failed or unreached URL is
  `{url,status_code:0,success:false,error}` inside the same array.
- `delay` is explicit-only (no hidden 2 s default), 0–60 s. `WebFetchInput.f` is
  `raw|fit` only; `css_selector` implies `fit`. Fetch-shaped `timeout_ms` max is
  90000 = Scrapling `MAX_FETCH_MS` (a vitest asserts they match); above it is a
  400, not a silent cap.

## History: Crawl4AI (removed 2026-09-27)

Removed after a side-by-side benchmark (Scrapling 0.25–1.06 s vs Crawl4AI
2.4–3.8 s on the same URLs, equal markdown once links were absolutised); any
mention of it in code is history. **Trustpilot decided the routing**: Scrapling
fast answers 403/970 B, escalating hit the managed Turnstile and **wedged the
whole worker for minutes**; Camoufox with `wait_ms:20000` renders the full page.

## Scrapling sidecar (`services/scrapling/app.py`)

Endpoints: `/fetch` (with `wait_ms`), `/markdown` (`raw|fit`), `/raw`,
`/screenshot`, `/pdf`, `/eval`, `/healthz`. Traps encoded there:

- **`web.archive.org` drops this project's datacenter egress** (verified
  2026-09-27: wget from the Tools container and `/fetch` fast from the sidecar
  both hang; the same URL answers from a laptop and through the residential
  exit). It sits in `STEALTH_HOSTS`, and `web_archive`/`web_snapshots` call the
  sidecar's `/raw` (plain httpx GET on the mode's egress, no browser) — never
  `fetch` from the server process. Both tools moved into `PROXY_BACKED` in
  `stats.ts` when that landed.
- **Pinned version `scrapling[fetchers]==0.4.14`** — `Response.markdown()` does
  not exist yet; the render path uses the Convertor internals. `markdownify==1.2.3`
  is a hard dep of `/markdown`; `httpx==0.28.1` of `/raw`.
- **One deadline per request** (timeout + 20 s slack; clients abort at +25 s),
  escalation included: `/fetch` gives the solve retry only what is left
  (`escalation_budget_ms`, none under 10 s). Two runs of timeout+20 s each used
  to outlast the client. `_execute` tracks `_inflight`; `/healthz` reports
  `busy_age_s` — a mode busy >150 s is a wedge.
- `timeout_ms` above `MAX_FETCH_MS` (90000) is a 422 on every endpoint, not a
  silent `min()`.
- Action errors return **400 without discarding the session** (the session is
  fine; the page lied).
- `NEVER_ESCALATE_HOSTS=("trustpilot.com",)` — escalation there cannot win and
  costs the worker. `SOLVE_HOSTS` is empty on purpose; a host only enters it when
  SOLVE is proven to *clear* the challenge, not merely when FAST is refused.

## web_agent (`services/scrapling/agent.py`, `agent_runner.py`)

browser-use (MIT, pinned `0.13.10`) on the Scrapling sidecar, with no new service.
`POST /agent` with `{task, start_url?, max_steps?, allowed_domains?, output_schema?,
timeout_ms?, stealth?, allow_mutations?, allow_form_submit?}` returns
`{final_result, success, steps:[{n, action, url}], urls, duration_s, blocked_requests,
form_submissions, error}`. Traps:

- **It cannot share the sidecar's venv.** browser-use pins `anyio==4.12.1` and
  `markdownify==1.2.2`, while Scrapling 0.4.14 needs `anyio>=4.14` and `/markdown`
  pins 1.2.3. Every release from 0.12.6 to 0.13.10 has the same pins. So it lives
  in `/opt/agent-venv` and runs as a **subprocess**: `agent.py` never imports it,
  and the deadline is a `killpg`, not an unkillable thread. The Dockerfile installs
  the main venv's exact patchright version there, so one Chromium serves both.
- **Disabled without a key**: `503 {disabled:true, reason:"set AGENT_LLM_API_KEY"}`.
  One run per container (flock): a busy replica answers `429 {busy, retryable}`.
  A run needs `start_url` or `allowed_domains`; a run with no domain fence is refused.
- **The agent's browser cannot submit forms.** Patchright launches Chromium and keeps
  a `route("**/*")` guard while browser-use drives it over `cdp_url`. A non-GET
  `document` request (to any host) is aborted, and so is a non-GET xhr/fetch to the
  task's own domains. A site that reads through same-origin POST (GraphQL) needs
  `allow_mutations`. Forms go through `submit_form_via_web_tools`, which is offered
  only with `allow_form_submit` and calls Camoufox `/form-submit` once per run. The
  budget is spent before the call, and only a 503 `retryable` (zero POSTs) refunds it.
  The sidecar needs `CAMOUFOX_FORMS_URL` (else `CAMOUFOX_URL`) for this.
- **The LLM step contract is structured outputs.** Four traps, all measured on
  `claude-sonnet-5-5` on 2026-10-02:
  - Forced `tool_choice` returns a 400 on Sonnet/Opus 5.5.
  - `auto` lets the model flatten the action list or answer in prose. That broke prod.
  - The full AgentOutput schema in `output_config.format` is refused with "compiled
    grammar is too large". It compiles at 15 actions and fails at 16.
  - A `thinking` output field trips the `reasoning_extraction` classifier
    (`stop_reason: refusal`), which looked like "Unterminated string" parse errors.

  The runner sends a compact envelope: `{name: enum, params: [{key, value}]}` plus the
  fully typed `done` branch, with per-action parameters in the system prompt. It also
  sets `use_thinking=False` and checks refusals before parsing, and server-side
  fallbacks default to on (`AGENT_LLM_FALLBACKS=off`). Result: 10/10 real runs, no
  step errors. After you change actions or the bridge, run `LiveGrammar` in
  `test_agent.py`.
- **example.com has no `<h1>` any more** (title, one `<p>`, one link). An empty `h1`
  is the correct answer, not a failure.
- Not stealth-equivalent to Scrapling: Patchright's driver patches do not cover
  browser-use's own CDP session. Only the launch flags, the profile and the egress
  carry over. There is no CAPTCHA solving.

## Camoufox sidecar (`services/camoufox/app.py`)

The Italian-residential browser: `/render`, `/eval`, `/screenshot`, `/spa-fetch`
(Akamai), forms (`FORMS.md`). Traps:

- **Playwright handles are thread-bound.** `/recycle` and dead-browser recovery
  swap the executor thread; `_ensure_page` / `_ensure_render_browser` record the
  owning thread and rebuild a foreign handle locally, and `_DEAD_BROWSER` also
  matches `cannot switch to a different thread` (fresh thread + one retry).
  Without that, every later render 502s and the toolkit silently falls back to
  the wrong-country exit — observed live 2026-09-27.
- **`web_form_inspect` is the agent's first step and is strictly read-only**
  (`form_inspect.py`, mounted with one `register()` call in `app.py`; its
  file is in the Dockerfile `COPY` list). Its guard aborts EVERY mutating
  request on any origin, and it never returns a value. Its `suggested` output
  is the `/form-submit` request, which `test_form_inspect.py` proves by
  submitting it through `run_form` to the loopback fixture.
  `web_form_submit` failures are structured: replay only on
  `retryable:true`, never on `outcome:"unknown"`.
- **Forms are single-attempt, never solver-gated**: CapSolver was removed
  2026-09-29 (last third-party credential; page-minted tokens passed whenever
  any token passed, so it bought nothing) — the endpoint accepts only the
  observation fields `captcha_field` / `require_captcha_token`. Text is typed
  keystroke-by-keystroke — reCAPTCHA v3 scores behaviour — so never
  reintroduce `.fill(`: `test_form_flow.py` greps for it.
- **One `mouse.move` per click; never a hand-stepped approach.** Forms launch
  with `humanize=True`, which already animates every move; stepping it was
  double humanization and 6–18× the dispatches into Camoufox's input-chain
  deadlock (daijro/camoufox#751: any trajectory point on x==0/y==0) — 32
  `pointer move` parks on 2026-10-01. Click targets are clamped to ≥2 px.
- **Forms measure themselves**: every job logs one `form-run {json}` line (no
  values/tokens/bodies) — phases with durations, per-POST `{n, token,
  mint_age_s}`, egress country/asn, profile, headed, Camoufox version,
  `parked_step`, `nav_error`. Count G1 (`posts` non-empty / all runs) from it
  rather than from caller-side bookkeeping. The live mark is per job
  (`FormLive`), not a module global.

## Forms on the shared Camoufox (`CAMOUFOX_ROLE`, measured)

- **One Camoufox, on purpose.** A dedicated forms service was tried
  2026-10-02/03 and removed: once the pre-POST fixes landed (exit rotation,
  reCAPTCHA readiness gate, nav/launch retries), the SHARED replica reached a
  POST on 30/30 oracle runs while serving 368 renders, 234 spa-fetches and 42
  recycles in the same window — the dedicated one did 31/31. The earlier
  "host degradation" (12/18 failures on cf156g) is gone with those fixes.
- `CAMOUFOX_ROLE=forms` and `CAMOUFOX_FORMS_URL` (fallback `CAMOUFOX_URL`)
  remain as an escape hatch: set both to split forms out again in minutes,
  with the forms' env (`PROXY_URL`, `FORM_PROFILE_DIR` on the volume). Only
  do it with an A/B like the one above.
- Restart policy is `ALWAYS`: a parked form sheds the process with exit(1),
  and `ON_FAILURE` (max 10) once left Camoufox permanently Crashed.
- **Score-gated exits**: a headed `web_form_submit` without a pinned
  `exit_session` probes up to `score_gate_tries` (3) exits on our oracle with
  its own launch config and runs on the first scoring ≥ `score_threshold`
  (0.7); none → `503 retryable {error:"no_scoring_exit", form_submissions:0}`.
  Nothing reaches the target before the gate passes. Probes spend the
  caller's `timeout_ms` (max 360000; a gated wizard ≈ 240 s + 90 s). Every
  verdict feeds the exit blocklist, which skips an IP or ASN whose MEAN is
  under the threshold. `diagnostics.score_gate` records what was tried. The
  implicit gate is skipped (`timeout_too_short`) below 165 s (305 s for a
  wizard), i.e. at the 120 s default.
- **The gate cannot pick the winner** (Atoka 2026-10-03: passing exits
  scored 0.7–0.9, step-0 refusals 0.7–0.8 — the target scores per site).
  `retry_on_captcha_rejection` (0–4, `form_retry.py`) re-attempts ONLY an
  explicit step-0 CAPTCHA refusal (one 2xx POST, same URL, every error node
  matching `captcha_rejection_text`) on a new context + new exit, re-gated;
  never with a pinned exit/profile, never past the deadline. `timeout_ms` may
  then go to 540000 (undici's 600 s header timeout minus the client slack).
  `form_submissions` is the total; `attempts[]` lists each.

## reCAPTCHA v3 score oracle (G2)

- **Tune against our key, never a third party.** Tools serves
  `/oracle/recaptcha` (Atoka-shaped: mint inside the submit listener) and does
  siteverify; it is outside the Bearer auth on purpose (the browser carries no
  key) and 404s unless `RECAPTCHA_ORACLE_SITEKEY`/`SECRET` are set on Tools.
  The key is registered for `tools-production-d199.up.railway.app` only, so a
  local page cannot mint. The Camoufox copies of those vars are unused.
- `web_form_score_probe` / `web_form_warm` / `web_form_exit_select` /
  `web_form_exits` are REST-only (`/api/v0/...`, not MCP) and run the forms'
  own launch path (`score_probe.py`). Bench:
  `.claude/skills/tools-health/scripts/score_bench.py --n 20`.
- **Profiles need a volume**: `FORM_PROFILE_DIR` unset = temp dir = wiped per
  deploy. Sticky exits (`FORM_PROFILE_STICKY_EXIT`, `sticky_exit`) and the
  exit blocklist live there too.
- **Camoufox version is a deploy-level A/B**: one pinned browser per image;
  bench with `--label`, deploy, bench again, `--report` both.
- **`mint_age_s` is not a token age**: it counts from the last
  `api2/reload` response, and api.js reloads on load too. Whether a wizard
  POST re-sent step0's token is `submission_tokens[].same_as_first`.

## Deploy lore (Railway, project `3375ebc9…`)

- **Tools does not auto-deploy on push.** After a commit, run
  `railway redeploy --service Tools --from-source`; a bare `redeploy` re-runs the
  same build and picks up nothing. `--from-source` builds the latest **commit** —
  uncommitted changes are not included. (Why Tools is exempt from auto-deploy is
  unconfirmed; observed on the 2026-09-27 removal push.)
- **A push rebuilds a sidecar iff it touches that sidecar's directory**
  (observed both ways: `services/scrapling/**` changes rebuilt Scrapling,
  `services/camoufox/**` rebuilt Camoufox, pushes touching neither left both
  alone; Camoufox also shows SKIPPED records on non-matching pushes). A
  docs/packages-only push should build nothing — verify with
  `railway service list` rather than assuming.
- `railway down -s <svc> --yes` is the off switch (status may read `Failed` after
  a graceful stop; the logs show `Stopping Container`). Never `scale=0`.
- **Railway's edge drops a request after 5 min with no bytes** (15 min while
  bytes flow; measured 2026-10-03). Forms with `score_gate` +
  `retry_on_captcha_rejection` run 300–540 s, `web_agent` up to 300 s; twice the
  client got an empty-body error at ~300 s while the sidecar was still working,
  turning real form outcomes into unknowns. `packages/api/src/heartbeat.ts` is
  the fix: REST calls still running after `HEARTBEAT_MS` (20 s) commit
  `200 application/json` and write a space every 20 s before the JSON (so past
  20 s the status is always 200 and the body's `error`/`isError` is the truth);
  `/mcp` writes `: ping` SSE comments on the tools/call stream (SDK 1.26 answers
  POSTs as SSE and has no ping hook, so they go on the Node response between
  its events). Do not add compression or any buffering proxy in front of these
  routes; never make a long call silent again.
- Public TCP proxies (`railway tcp-proxy`) are debug-only and must be deleted
  when done; they are public hosts. Scrapling's `/healthz` is otherwise reachable
  only on the private network (set `SCRAPLING_HEALTHZ` for the health skill).

## QA

- `pnpm typecheck && pnpm build && pnpm test` runs vitest in toolkit, api and
  cli. Every test fakes the sidecars at global `fetch` (toolkit `test/sidecars.ts`),
  so nothing touches the network. The suites cover routing, the symmetric
  fallback, the local markdown renderer, the sidecar client's breaker and
  deadlines, form outcome mapping, wayback/searxng, the crawl deadline, REST
  validation, auth (Bearer or `?api_key=`, never `X-API-Key`), MCP over HTTP and
  the SIGTERM drain (`api/test/server.test.ts` boots the real server on loopback).
- `pnpm test:coverage` is the same run with v8 coverage (text + `coverage/lcov.info`
  per package). It **fails below the thresholds** in each `vitest.config.ts`:
  toolkit 80% lines / 70% branches, api 80% lines, cli 60% lines. CI runs it and
  uploads the lcov files as the `lcov` artifact. New code without tests can
  push a package under its bar, so add tests in the same change.
- `pnpm test:py` runs the Scrapling pure functions (Scrapling stubbed).
  `pnpm test:py:coverage` reports Python coverage for Scrapling and for the
  browser-free Camoufox form tests (needs `coverage`, plus `fastapi`/`httpx`, or
  `fastapi`/`pydantic`/`playwright` for Camoufox). It reports only and is not gated.
  `ci.yml` runs all of this on every push/PR; `forms.yml` stays separate.
  The live API is still the oracle: POST `/api/v0/{tool}` with the Bearer key and
  assert `mode`/`status`/`renderer`, never liveness alone.
- SIGTERM drains: `/health` turns 503, in-flight calls finish, exit after at most
  `DRAIN_TIMEOUT_MS` (60 s). Railway only waits `RAILWAY_DEPLOYMENT_DRAINING_SECONDS`.
- SearXNG is pinned (`2026.9.30-a9d990033`); `google_sorry_fix.py` exits 1 when a
  patch neither applies nor is upstream, failing the build. Patch 1 (302/sorry)
  is upstream as of that tag.
- `.claude/skills/tools-health` diagnoses/heals the stack (`health.py`,
  `heal.py`); `references/signatures.md` maps every observed failure signature to
  its confirmed cause and fix. Read it before interpreting a probe.

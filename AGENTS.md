# web-tools — the map

Five Railway services: **Tools** (Next-less Node server: MCP + REST + CLI host),
**Scrapling** sidecar, **Camoufox** sidecar, **SearXNG**, **Redis**. Public endpoint
is Tools only; the sidecars are internal. Auth is `Authorization: Bearer <API_KEY>`
(the REST routes read only that header or `?api_key=` — `X-API-Key` does not work).

README is the user-facing story; RAILWAY.md is the template/hosting story. What
lives here is the map, the benchmark that shaped the architecture, and the traps.

## Two fetchers, one router

- `packages/toolkit/src/routing.ts` decides the backend **by host**; callers never
  choose. `isItalianSource` → Camoufox, `prefersCamoufox` (trustpilot) → Camoufox
  with `forcedWaitMs: 20000`; everything else → Scrapling. Camoufox also owns
  `web_bytes`, `web_spa_fetch`, forms.
- `functions.ts` `fetchPage` falls back **symmetrically**: preferred sidecar first,
  the other second; the thrown error names both causes. A transient Camoufox render
  failure (`NS_ERROR_CONNECTION_REFUSED`/`ABORT` happens) must not kill a tool
  Scrapling could serve, and vice versa.
- `web_crawl` is sequential — one `/markdown` post per URL — shaped
  `{results:[{url,status_code,success,mode,markdown}]}`; a failed URL is
  `{url,status_code:0,success:false,error}` inside the same array, not an error.
- `delay` is explicit-only (no hidden 2 s default). `WebFetchInput.f` is
  `raw|fit` only; `css_selector` implies `fit`.

## The benchmark that removed Crawl4AI (2026-09-27)

Side-by-side, same URLs, raw results in the session bench dir (`sl_results.json`,
`c4a_results.json`, `md_results.json` — kept out of git on purpose):

- **Speed**: Scrapling fast 0.25–1.06 s for full pages (wikipedia 0.62 s / 235 KB,
  github 1.06 s / 575 KB, bbc 0.50 s) vs Crawl4AI 2.4–3.8 s on the same URLs.
- **Markdown parity**: Crawl4AI's edge was link absolutisation; once the sidecar
  urljoins links and honours `<base href>`, quality matched — and Scrapling's
  markdown conversion is local (`scrapling/core/shell.py` Convertor), no second
  HTTP hop (~0.8 s/call) and no second service that can wedge.
- **Capability**: everything Crawl4AI did is `sessions` + Chromium now —
  `/markdown`, `/screenshot`, `/pdf`, `/eval` on the sidecar, `web_crawl`
  composed from it.
- **Trustpilot decided the routing, not the benchmark**: Scrapling `fast` answers
  403/970 B in ~0.5 s, clean. Escalating it hit the *managed* Turnstile and
  **wedged the whole worker for minutes** (no log line; the solve holds the driver
  while the queue starves). Camoufox with `wait_ms:20000` renders the full page
  (691 KB with live reviews, verified through the public Tools API).
- Crawl4AI itself was decommissioned the same day (`railway down -s Crawl4AI`,
  its `CRAWL4AI_*` vars removed from Tools). Historical references to it in code
  comments are history, not live paths.

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
- `_execute` enforces a hard deadline (timeout + 20 s slack; clients abort at
  +25 s), tracks `_inflight`, and `/healthz` reports `busy_age_s` — a mode busy
  >150 s is a wedge.
- Action errors return **400 without discarding the session** (the session is
  fine; the page lied).
- `NEVER_ESCALATE_HOSTS=("trustpilot.com",)` — escalation there cannot win and
  costs the worker. `SOLVE_HOSTS` is empty on purpose; a host only enters it when
  SOLVE is proven to *clear* the challenge, not merely when FAST is refused.

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
- Public TCP proxies (`railway tcp-proxy`) are debug-only and must be deleted
  when done; they are public hosts. Scrapling's `/healthz` is otherwise reachable
  only on the private network (set `SCRAPLING_HEALTHZ` for the health skill).

## QA

- `pnpm typecheck && pnpm build` — there are **no toolkit tests** (only
  `forms.yml` CI and Python form tests). The live API is the oracle: POST
  `/api/v0/{tool}` with the Bearer key and assert `mode`/`status`, never
  liveness alone.
- `.claude/skills/tools-health` diagnoses/heals the stack (`health.py`,
  `heal.py`); `references/signatures.md` maps every observed failure signature to
  its confirmed cause and fix. Read it before interpreting a probe.

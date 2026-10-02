# Failure signatures

Every entry here was observed in production on this stack, with the remediation
that actually fixed it. If you hit something not on this list, add it once you
have confirmed the fix, not when you have a theory.

## The thing that makes this stack hard to monitor

**Almost every degradation returns a plausible HTTP 200.** There are two
fetchers (Scrapling, Camoufox) behind a symmetric fallback, and when the good one
is unavailable the request silently falls back to the other. So a caller gets a
real page, a 200, and quietly wrong provenance: LinkedIn fetched from the Italian
exit, or an Italian source fetched from a US one.

That is why `health.py` asserts on the `mode` field and on result counts, not on
liveness. A liveness check passes through all of it.

| what you see | what it actually is |
| --- | --- |
| `web_search` returns `[]`, HTTP 200, ~15.2s | every SearXNG engine timing out (before 2026-10-02; now an HTTP 500 naming the `unresponsive_engines`) |
| `web_crawl` succeeds with `renderer=local` | Scrapling `/markdown` is unreachable or erroring; the Tools process rendered the markdown itself |
| `web_html` returns 200 with `mode=camoufox` on a host that should be stealth | the Scrapling sidecar is unreachable; the fallback served it |
| `web_html` on a `.it` host with `mode=fast` | Camoufox is unreachable, wrong country |
| a fetch takes much longer than its timeout then succeeds | the preferred sidecar timed out, the fallback served it |

## Signatures

| signature | cause | remediation | evidence |
| --- | --- | --- | --- |
| `web_search` returns 0 results in ~15.2s; SearXNG logs show brave/bing/mojeek/wikipedia all `httpx.TimeoutException` | SearXNG's outgoing state degrades after long uptime. Mechanism unconfirmed: `keepalive_expiry` defaults to 5s, which argues against a simply-stale pool | `railway redeploy -s SearXNG` | 15.2s/0 results before, 0.8-2.1s/3 results immediately after |
| `pthread_create: Resource temporarily unavailable` (Scrapling) | too many browsers in one container. `WORKERS × 3 modes` Chromium instances exhausts threads before memory | redeploy; keep `WORKERS=1`, scale with replicas | 2 workers × 3 modes = 6 browsers killed it |
| `BrowserType.launch: Failed to launch the browser process` with `signal=SIGSEGV`, `Sandbox: CanCreateUserNamespace() clone() failure: EACCES`, and `glxtest` “No such file or directory” (Camoufox) | **`/dev/shm` is 62 MB**, the Docker default, while Firefox wants far more. Under sustained load (several contexts, a heavy JS page) it exhausts shm and segfaults AT LAUNCH, and every later request 502s. Every usual culprit reads clean — 35% disk, no profile-dir build-up, 3.9 GB peak against a 32 GB limit — which is what makes this one hard to see | a redeploy clears it only until load returns, and an in-process relaunch hits the same wall. Real fix: give the container a bigger `/dev/shm`, or cut what the browser needs there. `df -h /dev/shm` inside the container is the whole diagnosis | 62M shm; recovery relaunches failed identically |
| `cannot switch to a different thread (which happens to have exited)` on `/render` (Camoufox), repeating until every render 502s and the toolkit falls back to the wrong-country exit | a job queued on a retired executor thread rebuilt the browser handle THERE after a `/recycle` or dead-browser recovery swapped the executor; Playwright's sync API is thread-bound, and this error was missing from `_DEAD_BROWSER` so nothing recovered | fixed 2026-09-27: thread-ownership guards in `_ensure_render_browser`/`_ensure_page` rebuild a foreign handle locally, and `_DEAD_BROWSER` now matches the message (fresh thread + one retry). `POST /recycle` clears the wedge in the meantime | observed live: renders 502 for minutes while `/recycle` returned `akamai_closed: false` |
| `ModuleNotFoundError: No module named '…'` at uvicorn boot (Camoufox), service `Crashed`, Tools answers `camoufox /form-submit unreachable: fetch failed` | the Dockerfile's `COPY` lists each file explicitly — a new `services/camoufox/*.py` module not added there never reaches `/app`, and the import chain (`app.py` → `form_worker` → `form_flow` → …) dies at startup | add the file to the `COPY` line and push; every camoufox consumer is down until it lands, so treat it as urgent. `python3 -m py_compile` locally does NOT catch it — only the image does | observed 2026-09-27: `captcha_solver.py` omitted, full sidecar outage for one build cycle |
| `BrowserType.launch: Target page … closed` (Scrapling) | a session wedged after a raised fetch. The sidecar now discards sessions on error, so a persistent one means something worse | redeploy | Patchright leaves the driver unusable after a mid-flight failure |
| `akamai _abck state … UNVALIDATED(~-1~)` (Camoufox) | the sensor has not cleared; every gated POST will 403 | `web_recycle` for a fresh exit, then re-warm | validated state logs `VALIDATED(~0~)` |
| `Cloudflare page didn't disappear … solving again` looping (Scrapling) | a **managed** Turnstile the solver cannot clear. It will loop to the fetch cap every time | keep the host out of `SOLVE_HOSTS` (and in `NEVER_ESCALATE_HOSTS` where it is); the fast fetch returning 403 is then the correct, cheap answer, and Camoufox + a forced wait is the measured survivor for a full render | Trustpilot: solvable-unmanaged fast fetch 403s in 0.7s (no wedge); escalated it wedged the worker for minutes; Camoufox `wait_ms:20000` renders the full 691KB page |
| `ERR_PNPM_OUTDATED_LOCKFILE` in a build | a manifest was bumped without the lockfile. CI installs `--frozen-lockfile`, so it is fatal | commit manifests and lock together; **manual** | broke all three gtm-tools services while the last good container kept serving |
| container crashes on `ZodError: API_KEY Required` | the service built the repo-root Dockerfile, i.e. the wrong program. Its Root Directory is unset | set Root Directory, then redeploy; **manual** | recurs on every push until fixed |
| a URL resolves to `http://host:` or `http://:8000` | a `${{Service.PORT}}` or `${{service.…}}` reference resolved to empty. References fail OPEN | hardcode the port, or check the service-name casing; **manual** | `Crawl4AI.PORT` read 8000 while the app listened on 11235 (the service has since been decommissioned) |
| an unrelated fetch waits the full client budget under load | queueing. One request per mode per container, so concurrent callers serialise | add a replica | 90.5s on one replica during a signal sweep, 0.7s with two |
| `web_archive` / `web_snapshots` fail with `{"error": "fetch failed"}` HTTP 500 in ~1s | wayback called with plain `fetch` from the server process — web.archive.org silently DROPS this project's datacenter egress (TCP hang, no RST) | fixed 2026-09-27: both go through the sidecar's `/raw` on the residential exit (host-routed via `STEALTH_HOSTS`). If it recurs, the stealth path to archive.org is down | Tools `wget` and sidecar fast both hang; laptop and sidecar stealth both answer |

## Form signatures (Camoufox `/form-submit`)

Read these off the `form-run {json}` line first (`railway logs --service
Camoufox --filter '"form-run"'`): one per job, no values. Counts below are
2026-10-01, all deployments, from the `form flow: done` / park lines.

| signature | cause | remediation | evidence |
| --- | --- | --- | --- |
| `form job parked pre-submit at 'pointer move'` (503 retryable, process shed 3s later) | a humanized `mouse.move` that never returns: Camoufox's input chain deadlocks on a trajectory point at x==0/y==0 (daijro/camoufox#751, unfixed in the pinned 152.0.4-beta.30). `human_click` made it 6-18x likelier by hand-stepping an approach that Camoufox's `humanize=True` already animates, and by starting it 100-400px left / 60-200px above the target (off-screen, clamped onto the axis, for edge fields) | landed 2026-10-02 (unverified in prod): ONE move per click to an off-centre target clamped to `>=2px`, threshold 10s, separate `pointer dwell`/`pointer click` marks. If parks persist, A/B the browser bump to 156.0.1-beta.33 (has the #751 fix) — a fingerprint change, re-verify the Akamai path | 32 parks; the fix's effect is measurable as `parked_step` counts in `form-run` |
| `form job parked pre-submit at 'field scroll'` | the `scrollIntoView` evaluate parked; most plausibly the next driver call queued behind an input chain the previous click left stuck (same mechanism as above). Unconfirmed | same fix as above; if `field scroll` parks persist after it while `pointer move` parks stop, the chain theory is wrong — capture `field` from `form-run` (which field) and re-open | 12 parks |
| `form flow: done … error=navigation_failed … cls=Error` ~60-120ms after `form flow: page ready`, with render-path `Page.goto: NS_ERROR_CONNECTION_REFUSED` tracebacks around it, often the first runs after a restart | the residential proxy refusing the connection, transiently (the same identity navigated fine 3-10s later, in every cluster) | landed (unverified in prod): `goto` retries in-run, 3 attempts, 2s/4s backoff, for transient engine codes only — a GET before any input, zero POSTs. `form-run.nav_error` names the code, `nav_attempts` the attempts | 18 runs; a cluster of 2 at 12:41:45 right after a shed, next attempt fine |
| `navigation_failed … cls=TargetClosedError` <1s after `form flow: new_page` (sometimes before `page ready`), in clusters of 3-4 | the browser closed the page (or itself) on arrival. Cause unconfirmed: clusters followed an `akamai session did not close in 20s — abandoning that worker thread` (17:20) and an `outcome_unknown cls=TargetClosedError` (10:14) — a degraded browser host, likely the same family as the `/dev/shm` launch crash above. Cured each time by the process shed after a `'new page'` park | landed (unverified): a closed page is replaced by a fresh page in the same context within the navigation retry. If the context itself is dead the retries fail fast and the 503 stands; the `'new page'` park + shed remains the cure for a dead host | 7 runs (10:15 x4, 17:25 x3) |
| `navigation_failed` after `NAV_ATTEMPTS` with `nav_error` a transient code | the refusal outlived ~6s of retries — the exit itself is down | the 503 is retryable: the caller re-attempts (rotating `exit_session` if it repeats) | new signature; not yet observed |
| `form flow: done … error=readiness_failed … cls=TimeoutError` ~2 min after `form flow: readiness` | `ready_expression` never turned true — the page's CAPTCHA integration never became ready (co-occurs with proxy refusals; most likely its script never loaded). The old loop polled until the whole deadline | landed (unverified): readiness bounded at `READY_WAIT_S`=30s, so the 503 comes ~100s sooner. Confirm the cause with `form-run.captcha_scripts` (`[requests, responses, network_failures]`) on the next occurrence | 7 runs, every one burned the deadline |
| oracle POST with `token:false`, `form-run.captcha_scripts` `[2,2,1]` (or `[1,0,1]`), Tools `oracle verdict` `missing-input-response` with `dwell:null` and `mint_error:null`; the run's `navigation` duration ~13-15 s instead of ~6.5 s | the page's reCAPTCHA library never initialised (a library body cut after its headers — counted as a response AND a failure — or api.js refused): no `t_submit` means the submit listener attached in `grecaptcha.ready()` never existed, so the click fell through to a NATIVE submit with an empty field. Co-occurs with render-path `NS_ERROR_NET_TIMEOUT`/goto timeouts through the same pool | landed (unverified in prod): pre-input readiness gate (anchor `#recaptcha-token` / main-world `grecaptcha.execute` / library `requestfinished`), ONE reload, else `captcha_unavailable` (503 retryable, zero POSTs). `form-run.captcha_failed` now names the failing piece (`path` class, `code`, `after_response`) — CONFIRMED on cf156g (18:40-19:57, 100 runs): the piece is `recaptcha__*.js` (~850 KB from www.gstatic.com), `NS_ERROR_NET_PARTIAL_TRANSFER` after headers (7 events), and a same-exit reload repeats the cut; a slow library was also ABORTED by the reload (`NS_BINDING_ABORTED`). Follow-up (unverified): wait out an in-flight library (≤15 s more), and on a truncation rotate to a fresh exit once when it is not pinned. 0/82 tokenless POSTs with the gate; 6 runs reloaded: 1 saved, 5 ended `captcha_unavailable`. Not ASN-specific (1267: 2/22, 3269: 1/19, 202870: 1/4, 202613: 1/2) — not a blocklist input | cf156 2026-10-02: 5/33 POSTs tokenless, all `[2,2,1]` (15:29:41, 15:30:38, 15:32:28, 15:43:12, 15:43:39); `[1,0,1]` twice at 10:30/10:33 |
| `form job parked pre-submit at 'new page'` on a `profile` run | `context.new_page()` on a PERSISTENT context (which already owns its launch tab) never returned; both observed parks were on `bench-cf156a-warm`, each in a degraded window (a `/recycle` abandoning the render thread at 15:36:00; at 15:43 a `new_page` that raised, then two refused launches on the same profile) | landed (unverified): `first_page` reuses the launch tab, so a profile run never calls `new_page`; `form-run.page_reused` says so. Isolated contexts (no launch tab) never parked there | 2/2 parks profile runs, 0/41 isolated; 15:36:33, 15:43:49 |
| `navigation_failed cls=TimeoutError` with `navigation` ≈ 60 s | a `goto` that never reached domcontentloaded on a stalled exit; playwright's TimeoutError is not the builtin one and was never retried, and the 60 s cap left no budget anyway | landed (unverified): `goto` capped at 30 s and a timeout retried in-run while ≥60 s of deadline remains (`nav_error: timeout`) | 2026-10-02 15:38:47 (60.2 s) |
| `navigation_failed cls=Error` ~0.2 s after `form flow: new_page` with no `page ready` | `new_page()` itself raised on a degraded browser | landed (unverified): retried as `nav_error: new_page_failed`, 3 attempts, pre-input | 2026-10-02 15:43:41 |
| `form browser launch failed attempt 1/2 (TargetClosedError: BrowserType.launch_persistent_context …)` on a profile right after a failed run on it | the profile's previous browser still letting go of the profile (cause unconfirmed; the next launch 6 s later worked) | landed (unverified): 3 launch attempts, 2 s / 5 s backoff; `form-run.launch_errors` lists each class | 2026-10-02 15:43:43/45 |
| an `inspect_only` (or `stopped_after_posts`) run's logs end at `form flow: wait done` with no `done` line | not a failure: those paths returned from inside the flow and skipped the done line | fixed: the done line and `form-run` fire on every return path | 10:14:59 inspect run |

## Things that look like problems and are not

- **Trustpilot (and hosts in `NEVER_ESCALATE_HOSTS`) returning `status: 403`,
  `mode: fast`, `escalated: false`, sub-second.** The guard working as designed:
  the sidecar refuses to burn an escalation it cannot win. A full render comes
  from Camoufox, which the toolkit routes there with a forced 20s wait.
- **`sensor matured after N probe(s) (status=500)`** in Camoufox. An app-level
  500 still means Akamai let the request through, which is what maturation tests.
- **A single engine returning 0 results.** Engine availability swings daily:
  google answered 5/5 one day and was CAPTCHA'd on every attempt the day before,
  with brave doing the reverse. The fan-out exists for this.

## Remediations that are NOT allowed here

`heal.py` deliberately cannot delete a service, change a domain, edit a variable,
or scale anything. Those either cannot be undone by trying again, or change the
security posture. In particular: removing a public domain is what makes a service
private, and only `Tools` should have one.

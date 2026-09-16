# Failure signatures

Every entry here was observed in production on this stack, with the remediation
that actually fixed it. If you hit something not on this list, add it once you
have confirmed the fix, not when you have a theory.

## The thing that makes this stack hard to monitor

**Almost every degradation returns a plausible HTTP 200.** There are three
fetchers, and when the good one is unavailable the request silently falls back to
a worse one. So a caller gets a real page, a 200, and quietly wrong provenance:
LinkedIn fetched from a datacenter IP that LinkedIn blocks, or an Italian source
fetched from a US exit.

That is why `health.py` asserts on the `mode` field and on result counts, not on
liveness. A liveness check passes through all of it.

| what you see | what it actually is |
| --- | --- |
| `web_search` returns `[]`, HTTP 200, ~15.2s | every SearXNG engine timing out |
| `web_html` returns 200 with `mode=crawl4ai` | the stealth sidecar is unreachable |
| `web_html` on a `.it` host with `mode=fast` | Camoufox is unreachable, wrong country |
| a fetch takes ~85s then succeeds | sidecar timed out, Crawl4AI served it instead |
| `{"error": 500, "detail": "…correlation_id…"}` | Crawl4AI withholding the cause; see below |

## Signatures

| signature | cause | remediation | evidence |
| --- | --- | --- | --- |
| `web_search` returns 0 results in ~15.2s; SearXNG logs show brave/bing/mojeek/wikipedia all `httpx.TimeoutException` | SearXNG's outgoing state degrades after long uptime. Mechanism unconfirmed: `keepalive_expiry` defaults to 5s, which argues against a simply-stale pool | `railway redeploy -s SearXNG` | 15.2s/0 results before, 0.8-2.1s/3 results immediately after |
| `can't start new thread` (Crawl4AI) | browser pool leak. `crawler_pool._sig()` hashes the WHOLE BrowserConfig, so any per-request-varying field mints a new ~180MB Chromium. `user_agent_mode: "random"` does exactly that | redeploy; never send `user_agent_mode: random` | measured 10 browsers / 1890MB / `reuse_rate_percent: 0` |
| `pthread_create: Resource temporarily unavailable` (Scrapling) | too many browsers in one container. `WORKERS × 3 modes` Chromium instances exhausts threads before memory | redeploy; keep `WORKERS=1`, scale with replicas | 2 workers × 3 modes = 6 browsers killed it |
|  + , ,  (Camoufox) | ** is 62 MB**, the Docker default. Firefox needs far more shared memory, so under sustained load (several contexts, a heavy JS page) it exhausts shm and segfaults AT LAUNCH — and then every later request 502s. Disk, inodes and container memory are all fine, which is what makes this one hard to see | a redeploy clears it only until load returns. Real fix: give the container a bigger , or cut what the browser needs there (fewer content processes).  inside the container is the whole diagnosis | 62M shm against a 32 GB memory limit at 3.9 GB peak; recovery relaunches also failed, because the relaunch hits the same wall |
| `Target page, context or browser has been closed` (Crawl4AI) | a dead browser still cached in the pool, which never checks liveness before reuse | evict via `/monitor/actions/kill_browser`, then retry | pool reported 2 browsers at 100% reuse while 500ing every request |
| `BrowserType.launch: Target page … closed` (Scrapling) | a session wedged after a raised fetch. The sidecar now discards sessions on error, so a persistent one means something worse | redeploy | Patchright leaves the driver unusable after a mid-flight failure |
| `akamai _abck state … UNVALIDATED(~-1~)` (Camoufox) | the sensor has not cleared; every gated POST will 403 | `web_recycle` for a fresh exit, then re-warm | validated state logs `VALIDATED(~0~)` |
| `Cloudflare page didn't disappear … solving again` looping (Scrapling) | a **managed** Turnstile the solver cannot clear. It will loop to the fetch cap every time | route that host to Crawl4AI instead; do not leave it in `SOLVE_HOSTS` | Trustpilot: 90s timeout per call, vs ~4s via Crawl4AI |
| `ERR_PNPM_OUTDATED_LOCKFILE` in a build | a manifest was bumped without the lockfile. CI installs `--frozen-lockfile`, so it is fatal | commit manifests and lock together; **manual** | broke all three gtm-tools services while the last good container kept serving |
| container crashes on `ZodError: API_KEY Required` | the service built the repo-root Dockerfile, i.e. the wrong program. Its Root Directory is unset | set Root Directory, then redeploy; **manual** | recurs on every push until fixed |
| a URL resolves to `http://host:` or `http://:8000` | a `${{Service.PORT}}` or `${{service.…}}` reference resolved to empty. References fail OPEN | hardcode the port, or check the service-name casing; **manual** | `Crawl4AI.PORT` read 8000 while the app listened on 11235 |
| an unrelated fetch waits the full client budget under load | queueing. One request per mode per container, so concurrent callers serialise | add a replica | 90.5s on one replica during a signal sweep, 0.7s with two |

## Things that look like problems and are not

- **`Blocked by anti-bot protection: HTTP 403` from Crawl4AI, reported as a 500.**
  Intermittent on Trustpilot from the datacenter IP. The Tools service already
  evicts and retries once, and it usually succeeds. Do not redeploy for this.
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

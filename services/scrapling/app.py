"""
Scrapling sidecar — fetch, render and capture for the pages a plain browser cannot reach.

POST /fetch      { url, mode?, network_idle?, timeout_ms?, disable_resources?, wait_ms? }
             → { status, url, html, size, mode, escalated }
POST /markdown   { html, url, filter?, css_selector? }   → { markdown }   (no browser)
POST /raw        { url, mode?, timeout_ms? } → { status, url, body, size, mode } (no browser)
POST /screenshot { url, mode?, full_page?, wait_ms?, timeout_ms? }        → { status, url, b64 }
POST /pdf        { url, wait_ms?, timeout_ms?, format?, landscape? }      → { status, url, b64 }
POST /eval       { url, scripts, mode?, wait_ms?, timeout_ms? }           → { status, url, results }
GET  /healthz    → also reports busy_age_s: submission age of in-flight runs per mode.
                   A slot whose age keeps growing is a wedged driver — see _execute.

Why markdown lives here too
---------------------------
Rendering used to be Crawl4AI's REST /md with a raw:// body, which died with the
Crawl4AI service (opaque 500s, ~0.8s per call, a second dependency to wedge).
The conversion itself is pure CPU, so it is one small endpoint on this sidecar:
Scrapling's own Convertor (noise-tag strip + prompt-injection sanitize + markdownify),
then a urljoin pass so relative links resolve against the final URL — markdownify
emits them verbatim and a page with relative /users/ links reads as a page with
no links at all. The production pin is 0.4.14, whose Response has no `.markdown()`
yet (added in 0.4.15); the Convertor methods it wraps exist unchanged in 0.4.14
and are called directly here so the browser pin does not have to move. When the
pin reaches 0.4.15+, _render_markdown can collapse to `Response.markdown(...)`.

Why this service exists at all
-----------------------------
Crawl4AI >= 0.9 refuses `proxy_config` on a request body (every HTTP body is
Provenance.UNTRUSTED and proxy_config is a forbidden power-field), and pins
Chromium to its own localhost egress proxy. So Crawl4AI can only ever egress
from the platform's own datacenter IP. Measured against LinkedIn profiles, that
IP burns out: 3/6 → 1/6 → 0/6 over 18 sequential fetches, all HTTP 999, and no
amount of retrying helps because the IP itself is what is blocked. This service
owns the residential egress and the challenge solving Crawl4AI cannot have.

Three modes, because the failure modes are different and so are their costs
-------------------------------------------------------------------------
Measured 2026-08-12, same browser engine throughout:

  FAST     direct, no solve, no subresources
           ordinary pages 0.7-1.9s · socialblade 200 · linkedin 999 · trustpilot 403
  STEALTH  residential proxy, no solve
           linkedin 34/36 (94%) @ ~2.9s  (direct decays to 0/6 HTTP 999)
  SOLVE    direct, solve_cloudflare, subresources loaded
           clears an unattended JS challenge; CANNOT clear a managed Turnstile,
           where it loops until the fetch cap (see SOLVE_HOSTS)

FAST is the default rather than SOLVE, even though SOLVE is a superset
functionally, because solve_cloudflare is not free and not always bounded:
  - ~2x latency on ordinary pages (gorgias.com 1.9s → 4.0s, HN 0.7s → 1.4s).
  - On a challenge it CANNOT solve it hangs for the entire timeout rather than
    failing fast — measured `Locator.bounding_box: Timeout 120000ms exceeded`
    on a site FAST rejects in 0.3s. Each mode owns a single-slot executor, so
    one such page would block every other request for that mode in this worker.
So we pay for solving only when we see a challenge: an auto-routed request that
comes back looking challenged is retried once in SOLVE (see `escalated`).

Note the Trustpilot 403 is NOT an IP problem — an unproxied fetch from a
residential home IP returned the identical 970-byte "Verifying Connection"
body, so it is the challenge, which is what SOLVE handles. And SOLVE
deliberately does not use the proxy: solving through a rotating residential
exit measured 1/2 with a 32.9s outlier, vs 2/2 at 1.9-4.1s direct.

Scrapling refuses per-fetch `proxy=` overrides and solve_cloudflare is fixed at
session construction, so modes cannot be per-request arguments on one session —
each is its own session, built lazily.

Threading model
---------------
- uvicorn --workers N forks N independent Python processes.
- Each worker lazily builds at most one session PER MODE it actually sees, so a
  worker that only serves FAST pays for one browser, not three.
- Each mode gets its own single-slot ThreadPoolExecutor: Patchright's greenlet
  plumbing requires every call for a session to stay on one OS thread, and a
  shared executor would serialise unrelated modes behind each other.
- Worst case per worker is 3 browsers, so WORKERS is deliberately lower than it
  would be for a single-session service. Budget ~200-300MB per live browser.
"""

from __future__ import annotations

import asyncio
import base64
import httpx
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from functools import partial
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator
from scrapling.fetchers import StealthySession


PROXY_URL = os.environ.get("PROXY_URL", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scrapling-svc")


class Mode(str, Enum):
    # Direct, no challenge solving, subresources blocked. Fastest; the default.
    FAST = "fast"
    # Residential proxy. For IP-reputation walls (LinkedIn HTTP 999) where the
    # block is *who you are* rather than a challenge to solve.
    STEALTH = "stealth"
    # Direct + solve the JS challenge + load subresources (a challenge needs its
    # own CSS/JS to run). For Cloudflare-style "Verifying Connection" walls.
    SOLVE = "solve"


# Hosts whose correct mode we have measured and which FAST cannot serve at all.
# Everything else starts at FAST and escalates to SOLVE only if it looks
# challenged, so an unlisted host never pays the solve tax up front.
#
# web.archive.org joins LinkedIn here for a measured network reason, not a bot
# one: this project's datacenter egress is silently DROPPED there (verified
# 2026-09-27 — wget from the Tools container and /fetch from this sidecar both
# hang; the same URL answers in ~1s from a laptop and in ~11s through the
# residential exit). Only the residential path reaches it at all.
STEALTH_HOSTS = ("linkedin.com", "web.archive.org")

# Hosts that must START at SOLVE, because escalation can never rescue them: they
# answer FAST with HTTP 200, so nothing looks challenged, yet the body is wrong.
#
# Empty, and that is a finding rather than an oversight. trustpilot.com lived here
# — FAST returns a ~965KB page whose review data sits only in embedded JSON with
# no rendered /users/ anchors, so the markdown parsed as zero reviews on a page
# that had 24. SOLVE fixed that until Trustpilot moved to a *managed* Cloudflare
# Turnstile, which this solver cannot clear: it loops "captcha is still present,
# solving again" until the fetch cap, every time. web-tools now routes that host to
# Camoufox, which renders the full page behind a forced 20s wait.
#
# Before adding a host, confirm SOLVE actually CLEARS it — not merely that FAST is
# refused. An unsolvable challenge here costs the full MAX_FETCH_MS per request and
# blocks the solve executor behind it.
SOLVE_HOSTS: tuple[str, ...] = ()

# Hosts that must NEVER be escalated to SOLVE, even when the response looks like
# a challenge. trustpilot.com lived here as a measurement, not a guess: on
# 2026-09-27 one auto-routed fetch escalated into the managed-Turnstile solve
# loop and wedged the WHOLE worker — for minutes afterwards no request on any
# mode completed, including a plain fast fetch of example.com that normally
# answers in 0.5s, and no log line appeared (the solve attempt holds the driver
# while the queue behind it starves). fast alone answers trustpilot in 0.7s with
# the 970-byte interstitial; the page that renders is Camoufox's job now, which
# is where web-tools routes the host. An unsolvable challenge here costs the
# full fetch cap AND the worker; a skipped escalation costs one useless 403.
NEVER_ESCALATE_HOSTS: tuple[str, ...] = ("trustpilot.com",)


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def pick_mode(url: str) -> Mode:
    host = (urlsplit(url).hostname or "").lower()
    if _host_matches(host, STEALTH_HOSTS):
        return Mode.STEALTH
    if _host_matches(host, SOLVE_HOSTS):
        return Mode.SOLVE
    return Mode.FAST


# Interstitials that mean "solve the challenge", not "this page is forbidden".
# Matched against the returned body, which is short for a challenge page.
_CHALLENGE_MARKERS = (
    "just a moment",
    "verifying connection",
    "verifying you are human",
    "attention required! | cloudflare",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_opt",
)


def looks_like_challenge(status: int, html: str) -> bool:
    """A challenge wall we could plausibly solve, as opposed to a hard block.

    Challenge pages are small and carry a known interstitial title; a genuine
    403/404 from the origin does not. Requiring both keeps us from burning a
    120s solve attempt on a page that is simply forbidden.
    """
    if status not in (403, 429, 503):
        return False
    if len(html) > 200_000:  # a real page, not an interstitial
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


_executors: dict[Mode, ThreadPoolExecutor] = {
    m: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"scrapling-{m.value}")
    for m in Mode
}
_sessions: dict[Mode, StealthySession] = {}

# Submission timestamps of runs that have been handed to an executor but have not
# completed — INCLUDING time spent queued behind a single-slot slot's current job.
# This is the observability for the one failure mode no timeout reaches: a driver
# that never returns. asyncio.wait gives the CALLER its deadline, but the thread
# keeps running, so healthz reports the oldest age per mode and a slot that keeps
# growing is a wedged driver rather than a busy one. See _execute.
_inflight: dict[Mode, list[float]] = {}


def _ensure_session_in_worker(mode: Mode) -> StealthySession:
    """Build this mode's session lazily, inside its own worker thread."""
    session = _sessions.get(mode)
    if session is not None:
        return session

    if mode is Mode.STEALTH:
        proxy = parse_proxy(PROXY_URL)
        if proxy is None:
            raise RuntimeError(
                "PROXY_URL must be set as http://user:pass@host:port for mode=stealth"
            )
        s = StealthySession(
            headless=True,
            solve_cloudflare=False,
            proxy_rotator=ProxyRotator(proxies=[proxy]),
        )
    elif mode is Mode.SOLVE:
        s = StealthySession(headless=True, solve_cloudflare=True)
    else:
        s = StealthySession(headless=True, solve_cloudflare=False)

    s.__enter__()
    _sessions[mode] = s
    log.info("session initialised pid=%d mode=%s", os.getpid(), mode.value)
    return s


def parse_proxy(url: str):
    """Convert "http://user:pass@host:port" into Scrapling's dict form."""
    if not url:
        return None
    m = re.match(r"^(https?)://([^:]+):([^@]+)@(.+)$", url)
    if not m:
        return None
    return {
        "server": f"{m.group(1)}://{m.group(4)}",
        "username": m.group(2),
        "password": m.group(3),
    }


def _discard_session(mode: Mode) -> None:
    """Tear down a mode's session so the next request builds a fresh one.

    A raised fetch does not leave a usable session behind: Patchright's sync API
    is driven from this one pinned thread, and once a call blows up mid-flight the
    driver can be left in a state where every subsequent fetch on that session
    hangs instead of erroring. Observed exactly that after Scrapling raised "No
    Cloudflare challenge found" — the service kept accepting connections and
    answering nothing, so callers saw an 85s timeout rather than a failure, and
    the whole sidecar looked dead while the process was fine.

    So an error always costs us the session, never the worker.
    """
    session = _sessions.pop(mode, None)
    if session is None:
        return
    try:
        session.__exit__(None, None, None)
    except Exception as e:  # noqa: BLE001 - teardown must not mask the real error
        log.warning("discarding %s session raised on close: %s", mode.value, e)
    log.info("discarded %s session; next request rebuilds it", mode.value)


def _do_fetch(mode: Mode, req_url: str, network_idle: bool, timeout_ms: int,
              disable_resources: bool, wait_ms: int = 0) -> dict:
    """Runs inside this mode's single-thread executor."""
    session = _ensure_session_in_worker(mode)
    try:
        page = session.fetch(
            req_url,
            network_idle=network_idle,
            timeout=timeout_ms,
            disable_resources=disable_resources,
            wait=wait_ms,
        )
    except Exception:
        _discard_session(mode)
        raise
    return {
        "status": page.status,
        "url": page.url,
        "html": page.html_content,
        "size": len(page.html_content),
        "mode": mode.value,
    }


app = FastAPI(title="scrapling-svc", version="1.0.0")


class FetchRequest(BaseModel):
    url: str = Field(..., description="Absolute URL to fetch")
    mode: Mode | None = Field(
        None,
        description="fast = direct (default). stealth = residential proxy, for IP walls "
                    "like LinkedIn. solve = direct + solve the JS challenge, for "
                    "Cloudflare-style walls. Omit to pick by host and auto-escalate to "
                    "solve if the response looks challenged.",
    )
    network_idle: bool = Field(False, description="Wait for network idle before returning")
    disable_resources: bool | None = Field(
        None,
        description="Block images/CSS/fonts. Defaults to False for solve (a challenge needs "
                    "its subresources) and True otherwise.",
    )
    timeout_ms: int = Field(60_000, ge=1_000, le=180_000)
    wait_ms: int = Field(
        0,
        ge=0,
        le=60_000,
        description="Extra settle time after the page is stable, before capture/return",
    )


class FetchResponse(BaseModel):
    status: int
    url: str
    html: str
    size: int
    mode: str
    escalated: bool = False


# Modes worth paying for before the first request arrives. A browser launch costs
# tens of seconds on a cold container, and the caller's budget is finite: web-tools
# aborts a fetch at timeout+25s and falls back to the other sidecar. So a cold
# start does not merely feel slow, it silently downgrades the result — the wrong
# exit and fingerprint for the host.
#
# SOLVE is deliberately not pre-warmed: nothing routes to it by host any more, it is
# only reached by escalation, and it is the most expensive session to build.
PREWARM = (Mode.FAST, Mode.STEALTH)


@app.on_event("startup")
async def _prewarm() -> None:
    """Build the hot sessions in the background, without blocking startup.

    Fire-and-forget on purpose: the container must report ready immediately, and a
    warm that fails (no PROXY_URL, for instance) has to degrade to lazy building
    rather than stop the service from serving the modes that do work.
    """
    loop = asyncio.get_running_loop()

    async def warm_all() -> None:
        # SEQUENTIALLY. Launching two browsers at once from one process fails with
        # "Racing with another loop to spawn a process" — Patchright cannot spawn
        # concurrently even from separate executor threads. Warming them in
        # parallel cost the stealth session on the first attempt at this, which is
        # the LinkedIn path and the one most worth having warm.
        for mode in PREWARM:
            try:
                await loop.run_in_executor(_executors[mode], _ensure_session_in_worker, mode)
                log.info("prewarmed %s", mode.value)
            except Exception as e:  # noqa: BLE001 - never fatal
                log.warning("prewarm %s failed (will build lazily): %s", mode.value, e)

    asyncio.create_task(warm_all())


@app.get("/healthz")
def healthz():
    now = time.monotonic()
    return {
        "ok": True,
        "pid": os.getpid(),
        "sessions_ready": sorted(m.value for m in _sessions),
        "proxy_configured": bool(PROXY_URL),
        # Seconds since submission of the OLDEST unfinished run per mode. A mode
        # absent here is idle; a mode sitting at hundreds of seconds is wedged
        # (its driver never returned) and the container needs a restart.
        "busy_age_s": {
            m.value: [round(now - ts) for ts in sorted(starts)]
            for m, starts in _inflight.items()
            if starts
        },
    }


# Hard ceiling on any single fetch, independent of the caller's timeout_ms. The
# challenge solver can block well past its own deadline (measured
# `Locator.bounding_box: Timeout 120000ms exceeded`), and each mode has a
# single-slot executor — so one unbounded fetch stalls every later request for
# that mode. Cap it here so the slot always comes back.
MAX_FETCH_MS = 90_000


# Slack on top of a run's own timeout_ms before the hard deadline fires: queue
# wait (a single-slot executor can sit behind one slow job) plus transport. The
# toolkit client aborts at timeout+25s, so this MUST stay below 25s — otherwise
# the caller sees an aborted socket instead of the honest 504 raised here.
HARD_DEADLINE_SLACK_S = 20


async def _execute(mode: Mode, timeout_ms: int, fn: Callable[[], dict]) -> dict:
    """Run fn on mode's single-slot executor with a caller-facing hard deadline.

    asyncio.wait bounds the WAIT, not the thread: on timeout the caller gets a
    504 while the executor keeps running (Python cannot kill a thread). That is
    deliberate — the alternative is what happened before this existed, when the
    caller waited forever. The thread's refusal to finish stays observable
    through healthz `busy_age_s`, which is the signal for a restart.
    """
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_executors[mode], fn)
    started = time.monotonic()
    _inflight.setdefault(mode, []).append(started)

    def _done(_f: asyncio.Future) -> None:
        starts = _inflight.get(mode)
        if starts and started in starts:
            starts.remove(started)
        if starts == []:
            _inflight.pop(mode, None)

    fut.add_done_callback(_done)
    done, _pending = await asyncio.wait(
        {fut}, timeout=(timeout_ms + HARD_DEADLINE_SLACK_S) / 1000
    )
    if not done:
        log.error(
            "hard deadline exceeded mode=%s timeout_ms=%d (driver may be wedged)",
            mode.value,
            timeout_ms,
        )
        raise HTTPException(
            status_code=504,
            detail=(
                f"[{mode.value}] run exceeded {timeout_ms + HARD_DEADLINE_SLACK_S * 1000}ms; "
                "the browser driver may be wedged (healthz busy_age_s)"
            ),
        )
    return fut.result()


async def _run(mode: Mode, req: FetchRequest) -> dict:
    disable_resources = (
        req.disable_resources
        if req.disable_resources is not None
        else (mode is not Mode.SOLVE)
    )
    timeout_ms = min(req.timeout_ms, MAX_FETCH_MS)
    return await _execute(
        mode,
        timeout_ms,
        partial(
            _do_fetch, mode, req.url, req.network_idle, timeout_ms,
            disable_resources, req.wait_ms,
        ),
    )


@app.post("/fetch", response_model=FetchResponse)
async def fetch(req: FetchRequest):
    explicit = req.mode is not None
    mode = req.mode or pick_mode(req.url)

    try:
        data = await _run(mode, req)
    except Exception as e:
        log.exception("fetch failed url=%s mode=%s", req.url, mode.value)
        raise HTTPException(status_code=502, detail=f"[{mode.value}] {e}")

    # Only escalate when we chose the mode ourselves — an explicit mode is the
    # caller's decision and we should not silently spend a second fetch on it —
    # and never for a host whose challenge this solver cannot clear (see
    # NEVER_ESCALATE_HOSTS: solving it wedges the worker, not just the request).
    host = (urlsplit(req.url).hostname or "").lower()
    if (
        not explicit
        and mode is not Mode.SOLVE
        and not _host_matches(host, NEVER_ESCALATE_HOSTS)
        and looks_like_challenge(data["status"], data["html"])
    ):
        log.info("escalating to solve url=%s (status=%s)", req.url, data["status"])
        try:
            solved = await _run(Mode.SOLVE, req)
        except Exception as e:
            # Keep the original response rather than turning a usable 403 body
            # into a 502 — the caller can still inspect it. Scrapling raises "No
            # Cloudflare challenge found" here whenever the wall is some other
            # vendor's, which is common and not fatal; _do_fetch has already
            # discarded the solve session, so the next attempt starts clean.
            log.warning("escalation to solve failed url=%s: %s", req.url, e)
            return FetchResponse(**data, escalated=False)
        return FetchResponse(**solved, escalated=True)

    return FetchResponse(**data, escalated=False)


# ── Markdown: pure CPU, no browser ──────────────────────────────────

class MarkdownRequest(BaseModel):
    html: str = Field(..., description="HTML to convert")
    url: str = Field(
        ...,
        description="URL the HTML was fetched from; relative links resolve against it "
                    "(or against the document's own <base href> if it declares one)",
    )
    filter: str = Field(
        "fit",
        description="fit = <body> content only (default); raw = whole document. "
                    "Both strip scripts/styles/hidden (prompt-injection) content.",
    )
    css_selector: str | None = Field(
        None, description="Convert only elements matching this selector (overrides filter scope)"
    )


class MarkdownResponse(BaseModel):
    markdown: str


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)([^)]*)\)")


def _absolutize_links(md: str, base: str) -> str:
    """Resolve relative markdown link/image targets against `base`.

    markdownify emits hrefs verbatim, so a page whose links are `/users/abc`
    comes out with links no consumer can follow — it reads as a page with no
    links at all. Scheme'd targets (http:, mailto:, data:, …) are left alone.
    """

    def repl(mo: re.Match) -> str:
        target, rest = mo.group(1), mo.group(2)
        if _SCHEME_RE.match(target):
            return mo.group(0)
        try:
            return "](" + urljoin(base, target) + rest + ")"
        except ValueError:
            return mo.group(0)

    return _MD_LINK_RE.sub(repl, md)


def _render_markdown(html: str, url: str, filt: str, css_selector: str | None) -> str:
    from scrapling.core.shell import Convertor
    from scrapling.engines.toolbelt.custom import Response as SResponse

    # A document that declares its own <base href> has already chosen the origin
    # its relative links belong to; overriding it would break rebased links.
    base = url
    m = re.search(r"<base\s[^>]*href=[\"']([^\"']+)", html, re.I)
    if m:
        base = urljoin(url, m.group(1))

    # method="MD" only decorates the synthetic response's log line, so renders
    # are distinguishable from real fetches in the log.
    sr = SResponse(
        url=url, content=html, status=200, reason="OK",
        cookies={}, headers={}, request_headers={}, method="MD",
    )
    page = (sr.css("body").first or sr) if filt == "fit" else sr
    page = Convertor._sanitize_for_ai(Convertor._strip_noise_tags(page))
    pages = [page] if not css_selector else list(page.css(css_selector))
    md = "".join(Convertor._convert_to_markdown(p.html_content) for p in pages)
    return _absolutize_links(md, base)


@app.post("/markdown", response_model=MarkdownResponse)
def markdown_endpoint(req: MarkdownRequest):
    if req.filter not in ("raw", "fit"):
        raise HTTPException(status_code=422, detail="filter must be 'raw' or 'fit'")
    try:
        return MarkdownResponse(
            markdown=_render_markdown(req.html, req.url, req.filter, req.css_selector)
        )
    except ModuleNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"markdown dependency missing: {e}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - hostile/broken HTML must not kill the worker
        log.exception("markdown render failed url=%s", req.url)
        raise HTTPException(status_code=422, detail=f"markdown render failed: {e}")


# ── Plain HTTP: /raw ─────────────────────────────────────────────────
#
# A GET with no browser: the CDX API answers JSON, archived pages are static
# HTML, and neither can clear a challenge — so unlike /fetch there is no
# escalation, and a 403 comes back as a status for the caller to judge. It
# still runs on the mode's slot (one in-flight egress per exit, and the same
# deadline/busy_age_s bookkeeping as everything else).


class RawRequest(BaseModel):
    url: str = Field(..., description="Absolute URL to GET")
    mode: Mode | None = Field(
        None,
        description="fast = direct (default). stealth = residential proxy. "
                    "Omit to pick by host.",
    )
    timeout_ms: int = Field(60_000, ge=1_000, le=180_000)


class RawResponse(BaseModel):
    status: int
    url: str = Field(..., description="Final URL after redirects")
    body: str
    size: int
    mode: str


_RAW_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _do_raw(url: str, timeout_ms: int, mode: Mode) -> dict:
    """Text bodies only — HTML/JSON is what this endpoint exists for; a binary
    would be mangled by `.text`. Redirects are followed because wayback 302s to
    the canonical timestamp, and relative links resolve against the FINAL url.
    """
    if mode is Mode.STEALTH and not PROXY_URL:
        raise RuntimeError(
            "PROXY_URL must be set as http://user:pass@host:port for mode=stealth"
        )
    r = httpx.get(
        url,
        proxy=PROXY_URL if mode is Mode.STEALTH else None,
        follow_redirects=True,
        timeout=timeout_ms / 1000,
        headers={"User-Agent": _RAW_UA},
    )
    body = r.text
    return {
        "status": r.status_code,
        "url": str(r.url),
        "body": body,
        "size": len(body.encode("utf-8", "replace")),
    }


@app.post("/raw", response_model=RawResponse)
async def raw(req: RawRequest):
    mode = req.mode or pick_mode(req.url)
    timeout_ms = min(req.timeout_ms, MAX_FETCH_MS)
    try:
        data = await _execute(
            mode, timeout_ms, partial(_do_raw, req.url, timeout_ms, mode)
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("raw failed url=%s mode=%s", req.url, mode.value)
        raise HTTPException(status_code=502, detail=f"[{mode.value}] {e}")
    return RawResponse(**data, mode=mode.value)


# ── Capture through page_action: screenshot / pdf / eval ────────────
#
# No auto-escalation here, unlike /fetch: a challenged page should be ROUTED
# (Italian and managed-challenge hosts go to Camoufox) rather than solved, and
# a screenshot of a challenge page is a truthful answer to the wrong question.


class ActionFailed(Exception):
    """The page_action failed while the driver itself is fine.

    Raised by _do_action after the fetch returns, so the session is kept: a
    bad caller script (or a page that cannot evaluate it) is not evidence that
    the browser needs rebuilding.
    """


def _do_action(
    mode: Mode,
    req_url: str,
    timeout_ms: int,
    network_idle: bool,
    wait_ms: int,
    action: Callable[[Any, dict], None],
) -> dict:
    """Runs inside this mode's single-thread executor: fetch, settle, act."""
    session = _ensure_session_in_worker(mode)
    box: dict[str, Any] = {}

    def wrapper(page: Any) -> None:
        # page_action runs BEFORE Scrapling's own post-action wait, so any
        # settle time must be spent here, before the capture.
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        try:
            action(page, box)
        except Exception as e:  # noqa: BLE001 - reported through the box, not raised
            box["error"] = f"{type(e).__name__}: {e}"

    try:
        page = session.fetch(
            req_url,
            network_idle=network_idle,
            timeout=timeout_ms,
            disable_resources=False,  # a screenshot without CSS is not a screenshot
            page_action=wrapper,
        )
    except Exception:
        _discard_session(mode)
        raise
    if "error" in box:
        raise ActionFailed(box["error"])
    return {"status": page.status, "url": page.url, "mode": mode.value, **box}


async def _run_action(req: Any, action: Callable[[Any, dict], None]) -> dict:
    mode = req.mode or pick_mode(req.url)
    timeout_ms = min(req.timeout_ms, MAX_FETCH_MS)
    network_idle = getattr(req, "network_idle", False)
    try:
        return await _execute(
            mode,
            timeout_ms,
            partial(
                _do_action, mode, req.url, timeout_ms, network_idle,
                req.wait_ms, action,
            ),
        )
    except HTTPException:
        raise
    except ActionFailed as e:
        raise HTTPException(status_code=400, detail=f"[{mode.value}] capture failed: {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("capture failed url=%s mode=%s", req.url, mode.value)
        raise HTTPException(status_code=502, detail=f"[{mode.value}] {e}")


class ScreenshotRequest(BaseModel):
    url: str = Field(..., description="Absolute URL to capture")
    mode: Mode | None = None
    full_page: bool = Field(True, description="Capture the whole scrollable page")
    wait_ms: int = Field(0, ge=0, le=60_000, description="Settle time before capture")
    network_idle: bool = Field(False, description="Wait for network idle before capture")
    timeout_ms: int = Field(60_000, ge=1_000, le=180_000)


class PdfRequest(BaseModel):
    url: str = Field(..., description="Absolute URL to print")
    mode: Mode | None = None
    wait_ms: int = Field(0, ge=0, le=60_000, description="Settle time before print")
    timeout_ms: int = Field(60_000, ge=1_000, le=180_000)
    format: str = Field("A4", description="Paper format accepted by Chromium print-to-PDF")
    landscape: bool = False


class EvalRequest(BaseModel):
    url: str = Field(..., description="Absolute URL to open")
    scripts: list[str] = Field(..., min_length=1, description="JS expressions/IIFEs, evaluated in order")
    mode: Mode | None = None
    wait_ms: int = Field(0, ge=0, le=60_000, description="Settle time before the first script")
    timeout_ms: int = Field(60_000, ge=1_000, le=180_000)


class CaptureResponse(BaseModel):
    status: int
    url: str
    mode: str
    b64: str


class EvalResponse(BaseModel):
    status: int
    url: str
    mode: str
    results: list[Any]


@app.post("/screenshot", response_model=CaptureResponse)
async def screenshot_endpoint(req: ScreenshotRequest):
    def act(page: Any, box: dict) -> None:
        box["b64"] = base64.b64encode(
            page.screenshot(full_page=req.full_page, type="png")
        ).decode()

    return CaptureResponse(**await _run_action(req, act))


@app.post("/pdf", response_model=CaptureResponse)
async def pdf_endpoint(req: PdfRequest):
    def act(page: Any, box: dict) -> None:
        box["b64"] = base64.b64encode(
            page.pdf(format=req.format, landscape=req.landscape, print_background=True)
        ).decode()

    return CaptureResponse(**await _run_action(req, act))


@app.post("/eval", response_model=EvalResponse)
async def eval_endpoint(req: EvalRequest):
    def act(page: Any, box: dict) -> None:
        results: list[Any] = []
        for script in req.scripts:
            value = page.evaluate(script)
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                # page.evaluate already serialises JSON-able results, but a
                # proxy/handle can slip through — degrade to repr, don't fail
                # the scripts that DID succeed.
                value = repr(value)
            results.append(value)
        box["results"] = results

    return EvalResponse(**await _run_action(req, act))

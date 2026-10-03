"""
Camoufox sidecar — THE project browser service: a JS-enabled stealth
Firefox (Camoufox) that always egresses through the Evomi Italian
residential PROXY_URL. Serves every browser-rendered fetch: plain JS
listings (IVASS), bot-gated portals (Radware/hCaptcha — Consob), and the
Akamai warmed-session flow (tributario CGT, fiscooggi).

The only browser service
------------------------
Every browser-backed source goes through here, including the Cloudflare /
SSO-gated ones (the `altalex` commentary source). Cloudflare-fronted
SERVER-rendered targets that don't need a browser at all use plain undici
with Chrome's TLS cipher order instead (scripts/lib/residential-http.ts) —
that's ufficiocamerale, and it is the cheaper path: try it first.

(This service replaced browserless — the datacenter-Chrome CDP service whose
only production consumer was the IVASS listings render; /render covers
that. Page-DRIVING flows — corteconti, sister, spid — run locally via
scripts/lib/browser.ts, not through this service. Camoufox's fingerprint
is internally coherent, unlike the stealth-patched headless Chrome that
Akamai flagged even through an Italian residential IP.)

API
---
POST /spa-fetch { base_url, warm_path, method, path, body?, accept?, sensor_wait_ms? }
    → { status, text }     # Akamai in-page fetch; status = the fetch's HTTP status
POST /render { url, wait_until?, wait_ms?, timeout_ms? }
    → { status, url, html } # generic RESIDENTIAL render (the "residential" via in
                            # scripts/lib/fetcher.ts) — full-JS DOM via Evomi for
                            # JS listings (IVASS) + bot-gated sites (e.g. Consob)
POST /screenshot { url, wait_until?, wait_ms?, full_page?, width?, height?, click_all?, fresh_ip? }
    → { status, url, b64 } # residential full-page PNG (base64) via the render
                            # browser — same egress/stealth as /render
POST /eval { url, js, wait_ms?, fresh_ip? }
    → { status, url, result } # run arbitrary JS in the residential page and
                            # return its JSON result (drive/inspect JS SPAs)
POST /form-submit { url, fields[], submit, dismiss?, success_url?, fresh_ip? }
    → { contract_version, form_submissions, status, url, html, ok, error }
                            # isolated, single attempt; never replayed on failure
                            # CAPTCHA: the page's own handler mints any token
                            # (observation only: captcha_field/require_captcha_token)
POST /form-inspect { url, wait_ms?, exit_session?, headed?, profile? }
    → { forms[{fields, submit_candidates, honeypot_candidates, suggested}],
        captcha, cookie_banners, wizard }  # read-only; never fills or clicks
POST /bytes { url, timeout_ms? }
    → { status, b64 }       # residential binary fetch (PDFs) through the same exit
POST /recycle {}           # drop both the Akamai warmed session and the render browser
                           # (heavy: full relaunch. For a fresh EXIT IP on one
                           #  request, pass fresh_ip=true instead — same clean
                           #  slate via a new context, ~1s not ~30-60s.)
GET  /healthz

Threading
---------
Camoufox's sync API can't share a thread with asyncio, so the persistent
browser + page live on a single-slot ThreadPoolExecutor; the async
handlers dispatch onto it. One warmed page per process (WORKERS=1).

Env
---
- PROXY_URL        http://user:pass@host:port  (Evomi; _country-IT in the
                   password for the Italian exit Akamai expects).
- NAV_TIMEOUT_MS   navigation timeout (default 60000).
- CAMOUFOX_ROLE    `all` (default: every endpoint) or `forms` — the dedicated
                   forms service: no render/Akamai prewarm, no keepalive, and
                   every non-form endpoint answers 503 {role: "forms"}. Only
                   /form-* and /healthz do work there (same image).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from camoufox.sync_api import Camoufox
from camoufox.utils import launch_options
from form_flow import FormLive, validate_form
from form_worker import FormRetryable, FormWorker, not_started, run_isolated_form
import form_inspect
import form_retry
import launch_health
import profile_store
import proxy_session
import score_probe
import target_verdicts


PROXY_URL = os.environ.get("PROXY_URL", "")
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "60000"))
# Hard deadline for a single in-page fetch (see _build_fetch_expr).
AK_INPAGE_TIMEOUT_MS = int(os.environ.get("AK_INPAGE_TIMEOUT_MS", "45000"))
# Akamai's _abck cookie goes stale within a couple of POSTs unless the
# sensor keeps seeing human activity. A background task interacts with the
# warmed page every KEEPALIVE_SEC so the cookie stays mature between
# requests (set 0 to disable).
KEEPALIVE_SEC = float(os.environ.get("KEEPALIVE_SEC", "4"))


def _role() -> str:
    """`forms` = the dedicated forms service; anything else = `all`."""
    return "forms" if os.environ.get("CAMOUFOX_ROLE", "all").strip().lower() == "forms" else "all"


ROLE = _role()
# The forms role serves ONLY these; everything else (render, eval, screenshot,
# bytes, spa-fetch, recycle) is refused so nobody uses it by mistake — its
# browsers were never started there.
_FORMS_ROLE_PATHS = ("/healthz", "/docs", "/openapi.json")


def role_allows(path: str, role: str | None = None) -> bool:
    role = role or ROLE
    return role != "forms" or path.startswith("/form-") or path in _FORMS_ROLE_PATHS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("camoufox")


# Evomi encodes options in the PASSWORD, and `_session-<token>` pins a STICKY
# exit IP for that token (measured: same token -> 151.63.104.95 on three calls;
# new token -> 151.27.184.32). Without a token the exit rotates PER REQUEST
# (measured: 79.56.153.156 then 2.36.97.102 back-to-back).
#
# Rotating per request is wrong for the Akamai flow. `_abck` is scored on
# fingerprint + interaction + IP, and maturation spans many requests on one
# warmed page — so a rotating exit means the sensor is validated from one IP and
# then used from another, which is exactly the shape of "intermittent POST" this
# service has always shown. So: pin the warmed session to ONE sticky IP, and mint
# a NEW token on /recycle, which is also how we escape an IP that Akamai has
# rate-hardened (it hardens per IP, and a hardened IP does not recover quickly).
# Token format, lifetime and the 2026-10-03 gate -> form drift: proxy_session.py.
_proxy_session = None


def new_proxy_session() -> str:
    """Rotate to a fresh sticky exit. Called on (re)warm and by /recycle."""
    global _proxy_session
    _proxy_session = proxy_session.new_token()
    log.info("proxy session token rotated -> %s", _proxy_session)
    return _proxy_session


def parse_proxy(url: str, session: str | None = None):
    """Convert "http://user:pass@host:port" into the Playwright proxy dict
    Camoufox accepts. When `session` is given (and the provider isn't already
    carrying a session token) the Evomi sticky options are appended to the
    password — a valid 6-10 char id and an explicit lifetime (proxy_session.py)
    — so the exit IP is sticky for the life of that browser and across the
    gate -> form relaunch. Never log the result: it carries the credential."""
    if not url:
        return None
    m = re.match(r"^(https?)://([^:]+):([^@]+)@(.+)$", url)
    if not m:
        return None
    password = m.group(3)
    if session and not proxy_session.has_session(password):
        password = password + proxy_session.options(session)
    return {
        "server": f"{m.group(1)}://{m.group(4)}",
        "username": m.group(2),
        "password": password,
    }


# Single-slot executor: the Camoufox sync API + its greenlets must all live
# on one OS thread, away from FastAPI's asyncio loop.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camoufox")


# Playwright's sync API permits ONE instance per thread, and this service keeps
# TWO persistent browsers: the Akamai warmed page and the render browser. Sharing
# one worker thread meant whichever browser started first owned it, and the other
# could never be created — "/eval → _ensure_render_browser → Camoufox.__enter__ →
# It looks like you are using Playwright Sync API inside the asyncio loop", with a
# /spa-fetch happily served on the same thread moments earlier. That is why the
# doctrine extraction could never run while the Akamai flow was warm.
#
# One thread per browser fixes it, and as a bonus /render and /spa-fetch stop
# serialising against each other — the contention that had the tributario loop
# starving local runs all day.
_render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camoufox-render")


def _reset_render_executor() -> None:
    global _render_executor
    old = _render_executor
    _render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camoufox-render")
    old.shutdown(wait=False)
    log.info("render executor thread recycled")


def _reset_executor() -> None:
    """Swap in a FRESH worker thread after tearing a browser down.

    Playwright's sync API refuses to start on a thread it considers tainted, and
    a thread that has already run Camoufox.__exit__() is exactly that: the next
    Camoufox(...).__enter__() on it raises "It looks like you are using Playwright
    Sync API inside the asyncio loop." The executor was created once at import and
    never replaced, so EVERY /recycle poisoned the single worker and the next
    /eval or /render failed — which is what kept wedging the doctrine extraction
    and forced full service restarts.

    Teardown itself must still run on the OLD thread (it owns the browser), so
    this is called after the closes. shutdown(wait=False) lets the retired thread
    finish and exit on its own.
    """
    global _executor
    old = _executor
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camoufox")
    old.shutdown(wait=False)
    log.info("executor thread recycled (fresh thread for the next browser)")
_cm = None        # the Camoufox context manager
_browser = None   # the Playwright Browser it yields
_page = None      # the persistent warmed page
_warmed = None    # (base_url, warm_path) the current page is warmed for
# The thread that owns _page/_browser. Playwright's sync API is thread-bound:
# after an executor swap, a job queued on the OLD thread can rebuild the handle
# there, and every later job on the new thread would then use a foreign handle
# ("cannot switch to a different thread"). _ensure_page checks this and rebuilds.
_page_thread: int | None = None


def _close_cm(cm, label: str) -> None:
    """Close a SNAPSHOT of a context manager, never the live global.

    THE HANDLES ARE CLEARED BY THE CALLER, SYNCHRONOUSLY, BEFORE THIS RUNS.
    Reading the globals here is a race with a rebuild: /recycle hands the close
    to a worker thread and returns, the next /spa-fetch builds a fresh browser on
    the new thread, and then this — still running on the old one — reaches for
    `_cm` and tears down the browser that just replaced it. What the caller sees
    is `Browser.new_page: Target page, context or browser has been closed`,
    answered as 502, on a service whose own logs say it recycled cleanly.
    Observed 2026-09-14 against the tributario sweep, which recycles before every
    single POST and so hits this window on every target.
    """
    try:
        if cm is not None:
            cm.__exit__(None, None, None)
    except Exception:
        log.exception("error closing %s", label)


def _close_in_worker() -> None:
    """Kept for the keepalive/error paths that still close in place."""
    global _cm, _browser, _page, _warmed, _page_thread
    cm = _cm
    _cm = _browser = _page = _warmed = None
    _page_thread = None
    _close_cm(cm, "camoufox session")


def _abck_validated(page) -> bool:
    """True once Akamai upgraded _abck from ~-1~ (bot) to ~0~ (cleared).

    This is the REAL maturation signal. The probe-based test this replaced was a
    weak proxy: it accepted any non-403 — including an app 500 — as "matured", so
    we never actually knew whether a session had cleared Akamai. Measured with a
    local Camoufox on a residential line, the POST succeeds exactly when this
    flips to ~0~.
    """
    try:
        ck = page.evaluate("() => document.cookie") or ""
    except Exception:
        return False
    for part in ck.split(";"):
        part = part.strip()
        if part.startswith("_abck="):
            return "~0~" in part
    return False


def _interact(page) -> None:
    """Drive genuine human-like events (mouse motion, scroll, input focus)
    so Akamai's sensor matures the _abck cookie — required before it lets
    state-changing POSTs through. humanize=True makes the moves human-like."""
    try:
        vw = page.viewport_size or {"width": 1366, "height": 900}
        w, h = vw["width"], vw["height"]
        for (x, y) in [(0.2, 0.3), (0.6, 0.45), (0.4, 0.7), (0.75, 0.6), (0.5, 0.4), (0.3, 0.55)]:
            page.mouse.move(int(w * x), int(h * y))
            page.wait_for_timeout(180)
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(350)
        page.mouse.wheel(0, -300)
        page.evaluate(
            "() => { const el = document.querySelector('input,textarea'); if (el) el.focus(); }"
        )
    except Exception:
        log.exception("warmup interaction failed (continuing)")


def _interact_light(page) -> None:
    """Cheap (<1s) interaction to keep the sensor fed between requests."""
    try:
        vw = page.viewport_size or {"width": 1366, "height": 900}
        w, h = vw["width"], vw["height"]
        page.mouse.move(int(w * 0.4), int(h * 0.5))
        page.mouse.move(int(w * 0.55), int(h * 0.42))
        page.mouse.wheel(0, 140)
        page.mouse.wheel(0, -120)
    except Exception:
        pass


def _keepalive_in_worker() -> None:
    if _page is not None:
        _interact_light(_page)


def _build_fetch_expr(method, path, body, accept) -> str:
    method_j = json.dumps(method)
    accept_j = json.dumps(accept)
    path_j = json.dumps(path)
    if body is not None:
        # body is sent as a JSON string, matching the SPA's own XHR.
        body_j = json.dumps(json.dumps(body))
        body_line = f"opts.headers['Content-Type']='application/json'; opts.body={body_j};"
    else:
        body_line = ""
    # Aborted in-page after AK_INPAGE_TIMEOUT_MS. page.evaluate() takes NO
    # timeout, so an in-page fetch that never settles (Akamai stall, proxy
    # black-hole, dead page) would park this service's single worker thread
    # forever — and /recycle is served by that same slot, so recovery would be
    # impossible too. A synthetic 599 lets the caller retry/recycle instead.
    return (
        "(async () => {"
        f"  const opts = {{ method: {method_j}, headers: {{ Accept: {accept_j} }} }};"
        f"  {body_line}"
        f"  const ctl = new AbortController(); opts.signal = ctl.signal;"
        f"  const timer = setTimeout(() => ctl.abort(), {AK_INPAGE_TIMEOUT_MS});"
        "  try {"
        f"    const r = await fetch({path_j}, opts);"
        "    const text = await r.text();"
        "    return { status: r.status, text: text };"
        "  } catch (e) {"
        "    return { status: 599, text: 'in-page fetch aborted: ' + (e && e.message || e) };"
        "  } finally { clearTimeout(timer); }"
        "})()"
    )


def _inpage_fetch(page, method, path, body, accept) -> dict:
    try:
        page.set_default_timeout(AK_INPAGE_TIMEOUT_MS + 10_000)
    except Exception:
        pass
    result = page.evaluate(_build_fetch_expr(method, path, body, accept))
    return {"status": int(result["status"]), "text": result["text"]}


def _ensure_page(base_url, warm_path, sensor_wait_ms, mature_probe, mature_max_tries):
    """Return a page warmed for (base_url, warm_path), creating/re-warming
    it if needed. On (re)warm we interact to seed the sensor, then — if the
    caller gave a maturation probe — keep interacting until that probe stops
    returning 403 (200 or an app-level non-403 both mean it cleared Akamai)."""
    global _cm, _browser, _page, _warmed, _page_thread
    key = (base_url, warm_path)
    if _page is not None:
        if _warmed == key and _page_thread == threading.get_ident():
            return _page
        if _page_thread == threading.get_ident():
            _close_in_worker()
        else:
            # This handle belongs to a retired executor thread (a job queued on
            # the old thread rebuilt it after a swap). It cannot be closed from
            # here — Playwright's sync API is thread-bound — so leak it; its
            # thread owns the close and is already gone or will exit.
            log.warning("warmed page owned by retired thread %s (now %s) — re-warming",
                        _page_thread, threading.get_ident())
            _cm = _browser = _page = _warmed = None
            _page_thread = None
    proxy = parse_proxy(PROXY_URL, new_proxy_session())
    if proxy is None:
        raise RuntimeError("PROXY_URL must be set as http://user:pass@host:port")
    # geoip=True matches locale/timezone to the proxy's exit IP (an Italian
    # user signal Akamai expects); humanize adds human-like cursor motion.
    cm = Camoufox(headless=True, geoip=True, humanize=True, proxy=proxy)
    try:
        browser = cm.__enter__()
    except Exception:
        launch_health.note(False)
        raise
    launch_health.note(True)
    page = browser.new_page()
    page.goto(f"{base_url}{warm_path}", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    half = max(0, sensor_wait_ms) / 2000.0
    time.sleep(half)
    _interact(page)
    time.sleep(half)
    if mature_probe is not None:
        for attempt in range(1, max(1, mature_max_tries) + 1):
            try:
                r = _inpage_fetch(
                    page,
                    mature_probe.get("method", "POST"),
                    mature_probe["path"],
                    mature_probe.get("body"),
                    mature_probe.get("accept", "application/json"),
                )
            except Exception:
                log.exception("maturation probe failed")
                break
            if r["status"] != 403:
                log.info("sensor matured after %d probe(s) (status=%s)", attempt, r["status"])
                log.info("akamai _abck state after maturation: %s",
                         "VALIDATED(~0~)" if _abck_validated(page) else "UNVALIDATED(~-1~) — POSTs will 403")
                break
            log.info("maturation probe %d still 403 — interacting more", attempt)
            _interact(page)
            page.wait_for_timeout(1500)
    _cm, _browser, _page, _warmed = cm, browser, page, key
    _page_thread = threading.get_ident()
    log.info("warmed page base=%s path=%s", base_url, warm_path)
    return page


def _do_spa_fetch(base_url, warm_path, method, path, body, accept, sensor_wait_ms,
                  mature_probe, mature_max_tries) -> dict:
    """Runs inside the single-thread executor. Performs an in-page fetch on
    the warmed origin so the cleared _abck cookie + same-origin context
    apply, exactly like chromium.ts:apiGet/apiPost.

    Akamai re-challenges after a burst of POSTs (the _abck cookie degrades
    without fresh sensor data), so on a 403 we re-interact to re-mature the
    cookie and retry the SAME request, up to mature_max_tries."""
    page = _ensure_page(base_url, warm_path, sensor_wait_ms, mature_probe, mature_max_tries)
    result = _inpage_fetch(page, method, path, body, accept)
    if result["status"] == 403 and mature_probe is not None:
        for attempt in range(1, max(1, mature_max_tries) + 1):
            log.info("request 403 — re-maturing (attempt %d) path=%s", attempt, path)
            _interact(page)
            page.wait_for_timeout(1500)
            result = _inpage_fetch(page, method, path, body, accept)
            if result["status"] != 403:
                break
    return result


# --- Generic residential render -------------------------------------------
# Besides the Akamai in-page-fetch flow above, this sidecar doubles as the
# project's RESIDENTIAL render backend (the "residential" via in
# scripts/lib/fetcher.ts): a real Camoufox + Evomi Italian residential exit
# that fetches a fully-rendered DOM for JS sites that bot-gate datacenter IPs
# (Radware/hCaptcha — e.g. Consob). It uses its OWN persistent browser, kept
# separate from the Akamai warm page so neither disturbs the other; both run
# on the single-slot executor (serialised), which is fine for low-frequency
# authority ingest.
import base64

_render_cm = None
_render_browser = None
# Thread that owns _render_browser (see _page_thread for why this matters).
_render_thread: int | None = None
# Set when a render-browser LAUNCH fails, cleared when one succeeds. This is
# what /healthz reports: a replica whose browser cannot start is not healthy,
# however cheerfully the rest of the process answers.
_render_broken = False


def _ensure_render_browser():
    global _render_cm, _render_browser, _render_thread
    if _render_browser is not None:
        if _render_thread == threading.get_ident():
            return _render_browser
        # Foreign thread owns this handle: an executor swap let a job queued on
        # the old thread build it. Playwright's sync API is thread-bound, so
        # new_page() from here raises greenlet "Cannot switch to a different
        # thread" — an error _DEAD_BROWSER cannot recover from, because the
        # handle LOOKS alive. Leak it (its retired thread owns the close) and
        # rebuild on THIS thread.
        log.warning("render browser owned by retired thread %s (now %s) — rebuilding",
                    _render_thread, threading.get_ident())
        _render_cm = _render_browser = None
        _render_thread = None
    # Sticky for this browser's lifetime as well. A page load is many requests,
    # and with geoip=True the fingerprint (locale/timezone) is derived from the
    # exit IP — so letting the exit rotate MID-LOAD advertises one identity while
    # the packets come from several countries' worth of IPs. Pinning costs nothing
    # here (each /render is still a fresh page) and keeps the story coherent.
    proxy = parse_proxy(PROXY_URL, new_proxy_session())
    if proxy is None:
        raise RuntimeError("PROXY_URL must be set as http://user:pass@host:port")
    try:
        cm = Camoufox(headless=True, geoip=True, humanize=True, proxy=proxy)
        browser = cm.__enter__()
    except Exception:
        # Record it for /healthz before re-raising: the caller gets its 502 either
        # way, but a replica that cannot launch must stop being told it is fine.
        launch_health.note(False)
        globals()["_render_broken"] = True
        raise
    launch_health.note(True)
    globals()["_render_broken"] = False
    _render_cm, _render_browser = cm, browser
    _render_thread = threading.get_ident()
    log.info("render browser ready (residential)")
    return browser


def _close_render_in_worker() -> None:
    global _render_cm, _render_browser, _render_thread
    cm = _render_cm
    _render_cm = _render_browser = None
    _render_thread = None
    _close_cm(cm, "render browser")


def _do_render(url, wait_until, wait_ms, timeout_ms, click_all, settle_ms) -> dict:
    browser = _ensure_render_browser()
    page = browser.new_page()
    try:
        resp = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        # Some portals lazy-load list content into collapsed accordions/tabs;
        # click the given selectors (all matches) to trigger the load, then
        # let the AJAX settle before capturing.
        if click_all:
            for sel in click_all:
                try:
                    for el in page.query_selector_all(sel):
                        try:
                            el.click(timeout=1500)
                        except Exception:
                            pass
                except Exception:
                    pass
            page.wait_for_timeout(settle_ms if settle_ms else 3000)
        return {"status": resp.status if resp else 200, "url": page.url, "html": page.content()}
    finally:
        try:
            page.close()
        except Exception:
            pass


def _fresh_page(browser, viewport, exit_session: str | None = None):
    """A page on a NEW browser context bound to a NEW exit IP.

    The render browser pins one exit for its lifetime, so the only way to get a
    fresh IP used to be /recycle — a full Camoufox teardown + relaunch (~30-60s).
    That is ruinous for any source metered PER IP (doctrine.it gates anonymous
    views that way: once an exit is spent every later load is a full-page
    restriction). A context carries its own proxy AND its own cookie jar, so this
    gives the same clean slate for the price of a context (~1s).

     PINS the exit: Evomi derives the IP from the session token, so
    passing the same token again lands on the SAME exit. That matters because a
    scoring anti-bot's verdict on an exit is binary and stable — once one passes,
    reusing it turns a ~12-attempt search into one attempt per later request,
    which is the whole difference between viable and not. Omit it for a random
    exit (the right default for per-IP-metered reads).

    Returns (page, context) — the caller must close the context.
    Falls back to (page, None) on the shared context if per-context proxying is
    unavailable, so existing consumers can never be broken by this path.
    geoip stays coherent because the pool is single-country (_country-IT).
    """
    try:
        ctx = browser.new_context(
            proxy=parse_proxy(PROXY_URL, exit_session or new_proxy_session()),
            viewport=viewport)
        return ctx.new_page(), ctx
    except Exception as e:
        log.warning("fresh_ip context failed (%s) — falling back to the shared exit", e)
        return browser.new_page(viewport=viewport), None


def _do_screenshot(url, wait_until, wait_ms, timeout_ms, full_page, width, height,
                   click_all, settle_ms, fresh_ip=False) -> dict:
    """Navigate through the residential render browser and return a PNG
    (base64). Same egress/stealth as /render — for capturing what a real
    Italian residential visitor sees (e.g. doctrine.it filtered lists)."""
    browser = _ensure_render_browser()
    viewport = {"width": width, "height": height}
    page, ctx = _fresh_page(browser, viewport) if fresh_ip else (browser.new_page(viewport=viewport), None)
    try:
        resp = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        if click_all:
            for sel in click_all:
                try:
                    for el in page.query_selector_all(sel):
                        try:
                            el.click(timeout=1500)
                        except Exception:
                            pass
                except Exception:
                    pass
            page.wait_for_timeout(settle_ms if settle_ms else 3000)
        png = page.screenshot(full_page=full_page)
        return {
            "status": resp.status if resp else 200,
            "url": page.url,
            "b64": base64.b64encode(png).decode("ascii"),
        }
    finally:
        try:
            page.close()
        except Exception:
            pass
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _do_eval(url, wait_until, wait_ms, timeout_ms, js, fresh_ip=False) -> dict:
    """Navigate through the residential render browser and return the result
    of page.evaluate(js) (must be JSON-serialisable). For driving/inspecting
    JS SPAs (open a filter dropdown by text, scrape facet codes, etc.)."""
    browser = _ensure_render_browser()
    viewport = {"width": 1440, "height": 1200}
    page, ctx = _fresh_page(browser, viewport) if fresh_ip else (browser.new_page(viewport=viewport), None)
    try:
        resp = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        result = page.evaluate(js)
        return {"status": resp.status if resp else 200, "url": page.url, "result": result}
    finally:
        try:
            page.close()
        except Exception:
            pass
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


_form_worker = FormWorker()
_form_proxy_session = proxy_session.new_token()


def _camoufox_version() -> str:
    """Wrapper package + pinned browser build, for the form-run summary.

    CAMOUFOX_BUILD is the Dockerfile's CAMOUFOX_BROWSER pin re-exported as
    an ENV (a build ARG is gone at runtime). Never raises: a summary field.
    """
    try:
        from importlib.metadata import version
        package = version("camoufox")
    except Exception:
        package = "?"
    return "%s/%s" % (package, os.environ.get("CAMOUFOX_BUILD") or "?")


_CAMOUFOX_VERSION = _camoufox_version()


def _profile_dir(profile: str) -> str:
    # FORM_PROFILE_DIR must point at a Railway VOLUME in production: the
    # default under the temp dir is wiped by every redeploy. See profile_store.
    return profile_store.profile_dir(profile)


def _form_browser(session, main_world_eval=False, headed=False, profile=None):
    proxy = parse_proxy(PROXY_URL, session)
    if proxy is None:
        raise RuntimeError("PROXY_URL is required")
    # Headed is opt-in per request: score-gated forms (reCAPTCHA v3) refuse the
    # headless fingerprint while the isolated per-submit browser keeps it from
    # disturbing the shared headless readers. Needs a display — the image runs
    # under xvfb-run (see Dockerfile), so :99 is always there.
    headless = not headed
    if not profile:
        return Camoufox(headless=headless, geoip=True, humanize=True, proxy=proxy, timeout=30000,
                        main_world_eval=main_world_eval)
    # Named persistent profile: cookies + fingerprint the target has already
    # seen (the reCAPTCHA verdict is per-session, not per-IP alone). The FIRST
    # launch's options are saved verbatim (fingerprint drawn once); later
    # launches reload them and override only what belongs to the request —
    # the exit (rotating it must not carry the old identity's geo) and the
    # headless flag. Sequential runs share one process under the form
    # worker's single admission, so the profile dir is never opened twice.
    directory = _profile_dir(profile)
    os.makedirs(directory, exist_ok=True)
    # profile_store strips its own meta (the pinned exit) and returns None
    # for options drawn for a browser build this image no longer ships (an
    # upgrade on a persistent volume): redraw then, keep the cookies.
    opts = profile_store.load_launch_opts(directory)
    if opts is None:
        opts = launch_options(headless=headless, geoip=True, humanize=True, proxy=proxy,
                              timeout=30000, main_world_eval=main_world_eval,
                              user_data_dir=directory)
        profile_store.save_launch_opts(directory, opts)
    opts["proxy"] = proxy
    opts["headless"] = headless
    opts["user_data_dir"] = directory
    opts["service_workers"] = "block"
    return Camoufox(from_options=opts, persistent_context=True)


def _do_bytes(url, timeout_ms) -> dict:
    browser = _ensure_render_browser()
    page = browser.new_page()
    try:
        resp = page.request.get(url, timeout=timeout_ms)
        return {"status": resp.status, "b64": base64.b64encode(resp.body()).decode("ascii")}
    finally:
        try:
            page.close()
        except Exception:
            pass


app = FastAPI(title="camoufox", version="1.0.0")


@app.middleware("http")
async def _role_gate(request, call_next):
    if not role_allows(request.url.path):
        return JSONResponse(status_code=503, content={"detail": {
            "role": ROLE, "retryable": False,
            "message": "this Camoufox runs CAMOUFOX_ROLE=forms: only /form-* and /healthz"}})
    return await call_next(request)


@app.on_event("startup")
async def _prewarm_render() -> None:
    """Launch the render browser before the first request needs it.

    /render, /screenshot, /eval and /bytes all share this browser, and building it
    costs tens of seconds. Callers upstream abort and fall back, so a cold start
    changes the ANSWER rather than just the latency. Fire-and-forget: startup must
    not block, and a failure has to leave lazy building intact.

    The Akamai warmed page stays lazy — it is keyed by (base_url, warm_path), so
    there is nothing to warm until a caller says which origin it wants.

    The forms role has no render browser at all: the form browsers are
    per-job, and a prewarmed reader would only compete with them.
    """
    if ROLE == "forms":
        log.info("CAMOUFOX_ROLE=forms: no render prewarm, no keepalive, form endpoints only")
        return
    loop = asyncio.get_running_loop()

    async def warm() -> None:
        try:
            await loop.run_in_executor(_render_executor, _ensure_render_browser)
            log.info("prewarmed render browser")
        except Exception as e:  # noqa: BLE001 - never fatal
            log.warning("render prewarm failed (will build lazily): %s", e)

    asyncio.create_task(warm())


@app.on_event("startup")
async def _start_keepalive():
    if KEEPALIVE_SEC <= 0 or ROLE == "forms":
        return

    async def loop_ka():
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(KEEPALIVE_SEC)
            if _page is None:
                continue
            try:
                # await serializes behind any in-flight request on the
                # single-slot executor, so no page-thread contention or pileup.
                await loop.run_in_executor(_executor, _keepalive_in_worker)
            except Exception:
                log.exception("keepalive tick failed")

    asyncio.create_task(loop_ka())
    log.info("keepalive started (every %.1fs)", KEEPALIVE_SEC)


# A dead render browser, and the ONE safe way to come back from it.
#
# The Camoufox process can die mid-life — sustained /form-submit load did it
# repeatedly — and nothing noticed: the cached handle then failed every later
# request with "Target … closed" until a human redeployed. The obvious fix,
# relaunching from _ensure_render_browser, is the WRONG one and was tried: the
# worker thread has already hosted a Playwright instance, so building a second
# one on it raises "Sync API inside the asyncio loop" (the same constraint that
# _reset_render_executor exists for). Recovery therefore belongs on the event
# loop, which can drop the handles, swap in a FRESH thread, and retry — exactly
# what /recycle does, minus the teardown a dead process does not need.
_DEAD_BROWSER = re.compile(
    r"Target (?:page, context or browser|closed)|browser has been closed|Browser\.new_page|Connection closed"
    # A worker thread that already hosted a Playwright instance is poisoned for
    # every later launch on it. The launch itself then raises this instead of a
    # browser error (observed 2026-09-26: a post-restart /eval failed this way on
    # the prewarmed thread, and the next launch on a fresh thread succeeded) —
    # so it is the same dead-thread recovery, not a caller bug, and retrying it
    # on a fresh thread is correct. If the fresh thread raises it too, the retry
    # propagates, so a genuinely broken Playwright still surfaces.
    r"|Sync API inside the asyncio loop"
    # A handle owned by a thread that has since been swapped out or exited
    # (observed 2026-09-27: "cannot switch to a different thread (which happens
    # to have exited)" after an executor recycle, failing every /render until a
    # redeploy). Same remedy — drop the handles, fresh thread, retry once. The
    # _ensure_render_browser/_ensure_page thread guards prevent the common way
    # to get here; this is the backstop for the ones they cannot see.
    r"|cannot switch to a different thread",
    re.I,
)


async def _run_render(fn, *args):
    """Run a render-browser job, recovering ONCE from a browser that has died."""
    global _render_cm, _render_browser, _render_thread
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_render_executor, fn, *args)
    except Exception as e:
        if not _DEAD_BROWSER.search(str(e)):
            raise
        log.warning("render browser is dead (%s) — fresh thread, retrying once", str(e)[:120])
        _render_cm = _render_browser = None
        _render_thread = None
        _reset_render_executor()
        return await loop.run_in_executor(_render_executor, fn, *args)


class SpaFetchRequest(BaseModel):
    base_url: str = Field(..., description="Origin to warm + fetch against")
    warm_path: str = Field("/", description="Path to navigate for the sensor warmup")
    method: str = Field("GET")
    path: str = Field(..., description="Same-origin path for the in-page fetch")
    body: dict | None = Field(None, description="JSON body for POST (sent as a JSON string)")
    accept: str = Field("application/json")
    sensor_wait_ms: int = Field(20_000, ge=0, le=120_000)
    mature_probe: dict | None = Field(
        None,
        description="Optional {method,path,body,accept} probe used during (re)warm to "
        "warm-until-mature the _abck cookie (loop interaction until it stops 403ing).",
    )
    mature_max_tries: int = Field(6, ge=1, le=20)


class SpaFetchResponse(BaseModel):
    status: int
    text: str


@app.get("/healthz")
async def healthz():
    # Whether the AKAMAI worker can still take work, which `session_ready` cannot
    # tell you: a wedged thread keeps `_page` set while every /spa-fetch times
    # out. One trivial task with a short deadline separates "busy" from "stuck",
    # and it queues behind real work rather than pre-empting it.
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(_executor, lambda: None), 5)
        akamai_responsive = True
    except asyncio.TimeoutError:
        akamai_responsive = False
    # A replica whose RENDER browser cannot launch served 502s for every /render,
    # /eval and /form-submit while reporting ok:true, so the load balancer kept
    # sending it work — measured as a ~50-70% error rate across two replicas,
    # cured only by a redeploy. Report it, and fail the check so an orchestrator
    # configured to watch this path can replace the replica instead of a human.
    # The forms role starts no render browser, so its health is the form
    # launches' (launch_health sheds a replica that cannot launch at all).
    healthy = akamai_responsive and (ROLE == "forms" or not _render_broken)
    body = {
        "ok": healthy,
        "role": ROLE,
        "pid": os.getpid(),
        "session_ready": _page is not None,
        "akamai_worker_responsive": akamai_responsive,
        "render_browser_ok": not _render_broken,
        "proxy_configured": bool(PROXY_URL),
        "warmed_for": list(_warmed) if _warmed else None,
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)


@app.post("/spa-fetch", response_model=SpaFetchResponse)
async def spa_fetch(req: SpaFetchRequest):
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            _executor, _do_spa_fetch,
            req.base_url, req.warm_path, req.method, req.path, req.body, req.accept,
            req.sensor_wait_ms, req.mature_probe, req.mature_max_tries,
        )
    except Exception as e:
        log.exception("spa-fetch failed path=%s", req.path)
        raise HTTPException(status_code=502, detail=str(e))
    return SpaFetchResponse(**data)


class RenderRequest(BaseModel):
    url: str
    wait_until: str = Field("load", description="load | domcontentloaded | networkidle | commit")
    wait_ms: int = Field(4000, ge=0, le=60_000)
    timeout_ms: int = Field(60_000, ge=1000, le=180_000)
    click_all: list[str] = Field(default_factory=list, description="CSS selectors to click (all matches) before capture — for accordion/tab lazy-load")
    settle_ms: int = Field(3000, ge=0, le=30_000, description="wait after click_all for AJAX to settle")
    fresh_ip: bool = Field(False, description="serve this request from a NEW browser context on a NEW exit IP (clean cookies too) — for per-IP-metered targets; costs ~1s, unlike /recycle")


class RenderResponse(BaseModel):
    status: int
    url: str
    html: str


@app.post("/render", response_model=RenderResponse)
async def render(req: RenderRequest):
    loop = asyncio.get_running_loop()
    try:
        data = await _run_render(
            _do_render, req.url, req.wait_until, req.wait_ms, req.timeout_ms,
            req.click_all, req.settle_ms,
        )
    except Exception as e:
        log.exception("render failed url=%s", req.url)
        raise HTTPException(status_code=502, detail=str(e))
    return RenderResponse(**data)


class ScreenshotRequest(BaseModel):
    url: str
    wait_until: str = Field("networkidle", description="load | domcontentloaded | networkidle | commit")
    wait_ms: int = Field(6000, ge=0, le=60_000)
    timeout_ms: int = Field(90_000, ge=1000, le=180_000)
    full_page: bool = Field(True, description="capture the full scroll height, not just the viewport")
    width: int = Field(1440, ge=320, le=3840)
    height: int = Field(900, ge=320, le=3840)
    click_all: list[str] = Field(default_factory=list, description="CSS selectors to click before capture — for accordion/tab lazy-load")
    settle_ms: int = Field(3000, ge=0, le=30_000, description="wait after click_all for AJAX to settle")
    fresh_ip: bool = Field(False, description="serve this request from a NEW browser context on a NEW exit IP (clean cookies too) — for per-IP-metered targets; costs ~1s, unlike /recycle")


class ScreenshotResponse(BaseModel):
    status: int
    url: str
    b64: str


@app.post("/screenshot", response_model=ScreenshotResponse)
async def screenshot(req: ScreenshotRequest):
    loop = asyncio.get_running_loop()
    try:
        data = await _run_render(
            _do_screenshot, req.url, req.wait_until, req.wait_ms,
            req.timeout_ms, req.full_page, req.width, req.height, req.click_all, req.settle_ms,
            req.fresh_ip,
        )
    except Exception as e:
        log.exception("screenshot failed url=%s", req.url)
        raise HTTPException(status_code=502, detail=str(e))
    return ScreenshotResponse(**data)


class EvalRequest(BaseModel):
    url: str
    js: str = Field(..., description="JS expression/IIFE evaluated in the page; must return JSON-serialisable data")
    wait_until: str = Field("networkidle")
    wait_ms: int = Field(6000, ge=0, le=60_000)
    timeout_ms: int = Field(90_000, ge=1000, le=180_000)
    fresh_ip: bool = Field(False, description="serve this request from a NEW browser context on a NEW exit IP (clean cookies too) — for per-IP-metered targets; costs ~1s, unlike /recycle")


class EvalResponse(BaseModel):
    status: int
    url: str
    result: object | None = None


@app.post("/eval", response_model=EvalResponse)
async def eval_(req: EvalRequest):
    loop = asyncio.get_running_loop()
    try:
        data = await _run_render(
            _do_eval, req.url, req.wait_until, req.wait_ms, req.timeout_ms, req.js,
            req.fresh_ip,
        )
    except Exception as e:
        log.exception("eval failed url=%s", req.url)
        raise HTTPException(status_code=502, detail=str(e))
    return EvalResponse(**data)


# A single attempt keeps its 360 s cap; a retrying call may ask for more so a
# gated wizard can hold a second attempt. 540 s + the client's 60 s slack
# stays under the toolkit's 600 s undici header timeout (packages/toolkit/src/http.ts).
FORM_TIMEOUT_MAX_MS = 360_000
FORM_TIMEOUT_RETRY_MAX_MS = 540_000


class FormField(BaseModel):
    selector: str
    value: str | None = None
    action: str = Field("type", description="type | check | select")


class FormSubmitRequest(BaseModel):
    ready_expression: str | None = Field(None, max_length=2000, description="Main-world boolean expression required before clicking submit")
    require_captcha_token: bool = Field(False, description="Abort the form POST if its CAPTCHA field is empty/unreadable; never retry")
    captcha_field: str | None = Field(None, max_length=100, description="POST field checked for token presence only; value is never returned")
    inspect_only: bool = Field(False, description="Navigate without filling/clicking; block same-origin mutating requests")
    stop_after_posts: int | None = Field(None, ge=1, le=3, description="return right after this many wizard POSTs are sent — warm-up stops after the verifying step0 POST")
    headed: bool = Field(False, description="headed browser (under xvfb) for score-gated forms; headless fleets score 0 on reCAPTCHA v3")
    url: str
    fields: list[FormField] = Field(default_factory=list)
    submit: str = Field(..., description="CSS selector of the submit control")
    dismiss: list[str] = Field(default_factory=list, description="selectors clicked first (cookie walls)")
    success_url: str | None = Field(None, description="regex; matching final URL means success")
    submission_urls: list[str] | None = Field(None, description="Same-origin POST URLs sharing one submission budget; defaults to url")
    wait_until: str = Field("domcontentloaded")
    wait_ms: int = Field(4000, ge=0, le=60_000)
    settle_ms: int = Field(20_000, ge=1000, le=120_000)
    timeout_ms: int = Field(120_000, ge=1000, le=FORM_TIMEOUT_RETRY_MAX_MS, description="whole run INCLUDING the score gate's probes (a gated wizard: ~240 s + ~90 s); above 360 s only with retry_on_captcha_rejection")
    fresh_ip: bool = Field(True, description="new context + new exit IP (scoring anti-bot is per-IP)")
    exit_session: str | None = Field(None, description="pin the exit: same token = same IP, so a passing exit can be REUSED instead of re-searched")
    gate_text: str | None = Field(None, max_length=300, description="regex on button/link text; after step0, a matching gate is clicked ONCE (atoka business-email gate)")
    step2: list[FormField] = Field(default_factory=list, description="fields of the wizard's second step, filled+submitted only when that step renders")
    step2_submit: str | None = Field(None, max_length=300, description="CSS selector of step2's submit control; defaults to 'form button'")
    completion_markers: list[str] = Field(default_factory=list, max_length=10, description="regexes on body text; a match counts as completion even when the URL does not change")
    profile: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", description="named persistent profile: warm cookies/fingerprint reused across submissions (empty = isolated, default)")
    sticky_exit: bool | None = Field(None, description="with a profile: reuse the exit pinned in its fingerprint.json (pin a new one on first use); None = FORM_PROFILE_STICKY_EXIT")
    score_gate: bool | None = Field(None, description="probe exits on the oracle first and run on the first scoring >= score_threshold; None = on when headed and no exit_session")
    score_threshold: float = Field(0.7, ge=0, le=1)
    score_gate_tries: int = Field(3, ge=1, le=6)
    oracle_url: str | None = Field(None, max_length=500, description="the score oracle the gate probes (Tools passes its own)")
    retry_on_captcha_rejection: int = Field(0, ge=0, le=form_retry.MAX_RETRIES, description="fresh attempts (new context, new exit, re-gated) after an explicit step-0 CAPTCHA refusal only (form_retry.py)")
    captcha_rejection_text: str | None = Field(None, max_length=300, description="regex every error node of the re-rendered form must match to count as a CAPTCHA refusal; default form_retry.DEFAULT_CAPTCHA_REJECTION")


class FormSubmitResponse(BaseModel):
    diagnostics: dict = Field(default_factory=dict)
    contract_version: int = 2
    form_submissions: int
    error: str | None = None
    status: int
    url: str
    html: str
    ok: bool
    exit_session: str = ""
    # Only with retry_on_captcha_rejection > 0: one entry per attempt, and
    # form_submissions above is then the TOTAL across attempts.
    attempts: list[dict] | None = None


@app.post("/form-submit", response_model=FormSubmitResponse)
async def form_submit(req: FormSubmitRequest):
    deadline = time.monotonic() + req.timeout_ms / 1000
    try:
        # Before admission: a bad regex is the caller's error (400), never a
        # launched browser that dies as an unknown 502.
        validate_form(req.url, req.submission_urls, req.success_url, req.gate_text, req.completion_markers)
        form_retry.compile_pattern(req.captcha_rejection_text)
    except (ValueError, re.error) as e:
        raise HTTPException(status_code=400, detail=str(e))
    retries = req.retry_on_captcha_rejection or 0
    if req.timeout_ms > FORM_TIMEOUT_MAX_MS and not retries:
        raise HTTPException(status_code=400, detail="timeout_ms above %d needs retry_on_captcha_rejection"
                                                    % FORM_TIMEOUT_MAX_MS)
    session, pin = _resolve_form_session(req.profile, req.exit_session, req.fresh_ip, req.sticky_exit)
    blocked = form_retry.blocked_reason(exit_session=req.exit_session, profile=req.profile,
                                        sticky=_sticky(req.sticky_exit), fresh_ip=req.fresh_ip)
    floor = {"s": 0.0}  # known only after an attempt ran (was it gated?)

    async def attempt(n):
        # Attempt 1 runs on the resolved session; a retry on a NEW exit token
        # (fresh proxy session, new isolated context), gated again.
        try:
            data, gate, used = await _form_attempt(req, deadline, session if n == 1 else proxy_session.new_token(),
                                                   retry=n > 1, retries=retries,
                                                   attempt_n=n if retries else None)
        except form_retry.AttemptRefused as refused:
            # A guard block may still be retried: its floor counts too.
            floor["s"] = _attempt_floor_s(req, {"record": refused.gate_record} if refused.gate_record else None)
            raise
        floor["s"] = _attempt_floor_s(req, gate)
        attempt.used = used
        return data, (gate or {}).get("record")

    attempt.used = session
    try:
        data = await form_retry.run_attempts(attempt, retries=retries, blocked=blocked,
                                             deadline=deadline, floor_s=lambda: floor["s"])
    except form_retry.AttemptRefused as refused:
        # Every attempt was a zero-POST token-guard block: the endpoint's own
        # 503 (retryable, nothing sent), naming the attempts.
        error = refused.cause or HTTPException(status_code=503, detail={
            "message": "Form never submitted (%s); safe to retry" % refused.error,
            "retryable": True, "error": refused.error, "form_submissions": 0})
        if isinstance(getattr(error, "detail", None), dict):
            error.detail["attempts"] = refused.attempts
        raise error
    except form_retry.RetryUnknown as unknown:
        log.warning("form-submit retry %d outcome unknown (%s); not retried",
                    len(unknown.attempts), unknown)
        raise HTTPException(status_code=502, detail={
            "message": "Form retry outcome unavailable; do not automatically retry",
            "retryable": False, "attempts": unknown.attempts,
            "form_submissions_before": unknown.form_submissions_before,
        })
    # A profile-pinned or gate-chosen exit (the final attempt's) is reported
    # back — the caller did not choose it; otherwise echo what was passed.
    session = attempt.used
    pinned =(req.profile and _sticky(req.sticky_exit)) or (data.get("diagnostics") or {}).get("score_gate") is not None
    return FormSubmitResponse(**data, exit_session=req.exit_session or (session if pinned else ""))


def _attempt_floor_s(req, gate):
    """The least budget a full retry needs: the form's own reserve, plus one
    oracle candidate when the attempt was gated."""
    wizard = bool(req.gate_text or req.step2 or req.completion_markers)
    reserve = score_probe.GATE_RESERVE_WIZARD_S if wizard else score_probe.GATE_RESERVE_PLAIN_S
    record = (gate or {}).get("record") or {}
    gated = gate is not None and "skipped" not in record
    return reserve + (score_probe.CANDIDATE_MIN_MS / 1000 + 5.0 if gated else 0.0)


async def _form_attempt(req, deadline, session, *, retry=False, retries=0, attempt_n=None):
    """ONE form attempt on `session`: (data, gate, session used).

    Raises the endpoint's HTTPExceptions on attempt 1 (503 retryable,
    502 unknown). On a retry, a provably zero-POST refusal is
    form_retry.AttemptRefused instead, so the loop can answer with the
    previous attempt's (real) result. With retries enabled, a token-guard
    block (`captcha_token_missing`, zero POSTs) is a trigger AttemptRefused
    on ANY attempt: a fresh exit may mint where this one could not.
    """
    # One per job: the pre-POST mark the worker polls and the job's single
    # `form-run {json}` summary line (no values, tokens or bodies).
    live = FormLive(profile=req.profile, headed=req.headed, camoufox=_CAMOUFOX_VERSION)
    if attempt_n is not None:
        live.note(attempt=attempt_n)
    try:
        return await _form_attempt_run(req, deadline, session, live)
    except HTTPException as error:
        detail = getattr(error, "detail", None)
        zero_post = (getattr(error, "status_code", None) == 503 and isinstance(detail, dict)
                     and detail.get("retryable") is True)
        trigger = zero_post and retries > 0 and detail.get("error") == "captcha_token_missing"
        if zero_post and (retry or trigger):
            raise form_retry.AttemptRefused(detail.get("error") or detail.get("reason") or "not_submitted",
                                            gate_record=detail.get("score_gate"), trigger=trigger,
                                            cause=error) from error
        raise


async def _form_attempt_run(req, deadline, session, live):
    # Exit rotation (pre-input, once) only for an exit the caller did not
    # pin — neither an explicit exit_session nor a sticky profile's own.
    rotate = _form_rotator(req.exit_session, req.profile, req.sticky_exit,
                           bool(req.ready_expression), req.headed)
    gate = None
    if score_probe.gate_wanted(req.score_gate, req.headed, req.exit_session, req.inspect_only):
        gate = await _score_gate(req, session, deadline, live)
        session = gate["session"]
        # The gate chose this exit on its score: the form runs ON it, so no
        # rotation away from it.
        rotate = None
    data = await _form_run(req, deadline, session, live, rotate, _gate_ip(gate))
    mismatches = []
    while data.get("error") == "exit_mismatch" and data.get("form_submissions", 0) == 0:
        # The token moved off the IP the gate scored, and the form stopped
        # before contacting the target. The score no longer describes this
        # exit: re-judge the exit the token is ON now (then fresh ones,
        # unless the caller pinned it), within the deadline, and run on what
        # passes. Bounded: EXIT_MISMATCH_REGATES, then a zero-POST 503.
        diagnostics = data.get("diagnostics") or {}
        mismatches.append({"gate_ip": diagnostics.get("gate_ip"), "form_ip": diagnostics.get("form_ip")})
        if len(mismatches) > EXIT_MISMATCH_REGATES:
            raise _exit_mismatch_503(mismatches, gate)
        live = FormLive(profile=req.profile, headed=req.headed, camoufox=_CAMOUFOX_VERSION)
        live.note(exit_mismatches=list(mismatches))
        try:
            gate = await _score_gate(req, session, deadline, live, recheck=True)
        except HTTPException as error:
            detail = getattr(error, "detail", None)
            if isinstance(detail, dict):
                detail["exit_mismatches"] = mismatches
            raise
        session = gate["session"]
        data = await _form_run(req, deadline, session, live, None, _gate_ip(gate))
    if mismatches:
        data.setdefault("diagnostics", {})["exit_mismatches"] = mismatches
    # The target's own verdict on this exit (only when it POSTed), which
    # the next gate for this host ranks on (target_verdicts.py).
    target_verdicts.record_attempt(req.url, data, (gate or {}).get("record"))
    if _retryable_zero_post(data):
        # Provably nothing left this machine: the failure predates the submit
        # click (or the browser) and the guard saw no submission. Same
        # contract as the park above — replay the identity.
        log.warning("form-submit retryable: %s with zero POSTs", data.get("error"))
        detail = {
            "message": "Form never submitted (%s); safe to retry" % data.get("error"),
            "retryable": True, "reason": data.get("error"),
        }
        if data.get("error") == "captcha_token_missing":
            # Named, like no_scoring_exit: the toolkit reports the code, and
            # the gate's record says which exit could not mint.
            detail.update(error="captcha_token_missing", form_submissions=0)
            if gate is not None:
                detail["score_gate"] = gate["record"]
        raise HTTPException(status_code=503, detail=detail)
    if gate is not None:
        data.setdefault("diagnostics", {})["score_gate"] = gate["record"]
    return data, gate, session


# One re-gate after a gate -> form exit mismatch; a second mismatch is a
# 503 (the provider is moving this token under us — retrying blind would
# only spend more of the deadline on exits that do not hold).
EXIT_MISMATCH_REGATES = 1


def _gate_ip(gate):
    """The IP the gate's chosen score is about, or None (no gate, skipped)."""
    record = (gate or {}).get("record") or {}
    return record.get("gate_ip") if record.get("passed") else None


def _exit_mismatch_503(mismatches, gate):
    log.warning("form-submit retryable: exit_mismatch x%d with zero POSTs (target never contacted)",
                len(mismatches))
    detail = {
        "message": "The exit moved off the IP the score gate chose; form never started, safe to retry",
        "retryable": True, "error": "exit_mismatch", "reason": "exit_mismatch",
        "form_submissions": 0, "exit_mismatches": mismatches,
    }
    if gate is not None:
        detail["score_gate"] = gate["record"]
    return HTTPException(status_code=503, detail=detail)


async def _form_run(req, deadline, session, live, rotate, expect_ip):
    """One form browser on `session` (the worker job); HTTPExceptions for a
    park (503) and an unknown outcome (502)."""
    try:
        # Neither /recycle nor read-job recovery owns this browser. Never retry.
        return await _form_worker.run(partial(run_isolated_form,
            partial(_form_browser, session, bool(req.ready_expression), req.headed, req.profile),
            deadline=deadline, url=req.url, rotate_factory=rotate, expect_ip=expect_ip,
            fields=[f.model_dump() for f in req.fields], submit=req.submit,
            dismiss=req.dismiss, success_url=req.success_url,
            wait_until=req.wait_until, wait_ms=req.wait_ms, settle_ms=req.settle_ms,
            submission_urls=req.submission_urls, captcha_field=req.captcha_field,
            inspect_only=req.inspect_only, require_captcha_token=req.require_captcha_token,
            ready_expression=req.ready_expression, gate_text=req.gate_text,
            step2=[f.model_dump() for f in req.step2], step2_submit=req.step2_submit,
            completion_markers=req.completion_markers, stop_after_posts=req.stop_after_posts,
            captcha_rejection_text=req.captcha_rejection_text,
            live=live), url=req.url, deadline=deadline, live=live)
    except FormRetryable as parked:
        # Parked on the one unbounded pre-POST call (the marker says where):
        # no field touched, no POST left this machine — the identity is
        # untouched and the client may replay after the shed recycles the
        # leaked browser. Never merge this into the 502 below: a 502 forbids
        # resubmission, this one demands it.
        log.warning("form-submit retryable: parked pre-submit at '%s'", parked)
        raise HTTPException(status_code=503, detail={
            "message": "Form never submitted (stuck pre-POST); safe to retry",
            "retryable": True,
        })
    except Exception as e:
        log.warning("form-submit unavailable (%s); not retried", type(e).__name__)
        raise HTTPException(status_code=502, detail="Form outcome unavailable; do not automatically retry")


async def _score_gate(req, session, deadline, live, recheck=False):
    """Probe exits on OUR oracle before the form; returns {session, record}.

    `recheck`: the form found `session` off the IP the gate had scored
    (exit_mismatch) — judge the exit it is on NOW first, then fresh ones
    unless the caller pinned it.

    Raises 400 when gating was asked for explicitly without an oracle, and
    503 retryable `no_scoring_exit` when no candidate scores >= threshold —
    provably zero POSTs: only the oracle and the egress echo were contacted,
    never the target.
    """
    if not req.oracle_url:
        if req.score_gate:
            raise HTTPException(status_code=400, detail="score_gate needs oracle_url")
        log.info("score gate skipped: no oracle_url")
        return {"session": session, "record": {"skipped": "no_oracle"}}
    try:
        score_probe.oracle_urls(req.oracle_url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    wizard = bool(req.gate_text or req.step2 or req.completion_markers)
    if req.score_gate is None and not score_probe.gate_fits(req.timeout_ms, wizard):
        # Default-on gate, deadline too short to probe AND keep the form's
        # budget: run ungated (as before) and say why.
        log.info("score gate skipped: timeout_ms=%s too short", req.timeout_ms)
        return {"session": session, "record": {"skipped": "timeout_too_short"}}
    sticky = bool(req.profile and _sticky(req.sticky_exit))
    # A caller-pinned exit is only ever judged, never replaced; a sticky
    # profile's own exit is tried first, then fresh ones.
    sessions = [req.exit_session] if req.exit_session else ([session] if sticky or recheck else [])
    tries = 1 if req.exit_session else req.score_gate_tries
    outcome = await score_probe.run_gate(
        oracle_url=req.oracle_url, profile=req.profile, headed=req.headed,
        threshold=req.score_threshold, tries=tries, deadline=deadline,
        reserve_s=score_probe.form_reserve_s(req.timeout_ms, wizard), sessions=sessions,
        target_url=req.url)
    record = score_probe.gate_record(outcome)
    live.note(score_gate=record)
    if not outcome["passed"]:
        live.emit(not_started(req.url, "no_scoring_exit"))
        raise HTTPException(status_code=503, detail={
            "message": "No exit scored >= %.2f on the oracle; form never started, safe to retry"
                       % req.score_threshold,
            "retryable": True, "error": "no_scoring_exit", "form_submissions": 0,
            "score_gate": record,
        })
    if sticky and outcome["session"] != session:
        profile_store.remember_exit(req.profile, outcome["session"],
                                    ip=(outcome["egress"] or {}).get("ip"), score=outcome["score"])
    return {"session": outcome["session"], "record": record}


def _form_rotator(exit_session, profile, sticky_exit, main_world_eval, headed):
    """A fresh-exit browser factory for run_isolated_form, or None when the
    exit is pinned (explicit exit_session, or a sticky profile's own exit:
    rotating it would move the identity off the exit it is known on)."""
    if exit_session or (profile and _sticky(sticky_exit)):
        return None
    return lambda: partial(_form_browser, proxy_session.new_token(), main_world_eval, headed, profile)


def _sticky(flag) -> bool:
    return profile_store.sticky_default() if flag is None else bool(flag)


def _resolve_form_session(profile, exit_session, fresh_ip, sticky_exit):
    """Exit token for one form browser; pins it to a sticky profile up front
    (before launch) so the identity stays on the exit it was first seen from
    even when the outcome is unknown."""
    session, pin = profile_store.resolve_exit_session(
        profile=profile, exit_session=exit_session, fresh_ip=fresh_ip,
        sticky=_sticky(sticky_exit), shared=_form_proxy_session,
        new_token=proxy_session.new_token)
    if pin:
        profile_store.remember_exit(profile, session)
    return session, pin


def _retryable_zero_post(data: dict) -> bool:
    """A failed run the caller may safely replay.

    Only failures that PROVE no submission happened qualify: an error raised
    before the submit click (or before a browser existed), with the guard's
    submission count still zero. Anything after the click — no_submission,
    outcome_unknown, a captcha verdict — carries the identity with it and
    must never be replayed automatically.
    """
    diagnostics = data.get("diagnostics") or {}
    if form_retry.guard_blocked_zero_post(data):
        # After the click, yet provably zero-POST: the token guard ABORTED
        # the only matching request (and blocks every later one), so the
        # server never saw a submission. The page could not mint — replay.
        return True
    return (
        data.get("error") in (
            # Pre-click failures: nothing left this machine (fields, context,
            # navigation, the readiness gate, an unusable reCAPTCHA client
            # after its one reload) — the caller may replay.
            "fields_failed", "browser_launch_failed", "navigation_failed",
            "browser_context_failed", "readiness_failed", "captcha_unavailable",
            "deadline_before_browser", "deadline_before_navigation",
        )
        and data.get("form_submissions") == 0
        and not diagnostics.get("submit_click_attempted")
    )


# Read-only sibling of /form-submit: same browser path and admission.
form_inspect.register(app, worker=_form_worker, form_browser=_form_browser,
                      shared_session=_form_proxy_session, camoufox=_CAMOUFOX_VERSION)

# Stealth-score diagnostics: oracle probe, warm routine, exit selection —
# the SAME factory, worker and launch path as /form-submit (score_probe.py).
score_probe.register(app, worker=_form_worker, browser_factory=_form_browser,
                     run_isolated=run_isolated_form, resolve_session=_resolve_form_session,
                     camoufox=_CAMOUFOX_VERSION)


class BytesRequest(BaseModel):
    url: str
    timeout_ms: int = Field(60_000, ge=1000, le=180_000)


class BytesResponse(BaseModel):
    status: int
    b64: str


@app.post("/bytes", response_model=BytesResponse)
async def bytes_(req: BytesRequest):
    loop = asyncio.get_running_loop()
    try:
        data = await _run_render(
            _do_bytes, req.url, req.timeout_ms)
    except Exception as e:
        log.exception("bytes failed url=%s", req.url)
        raise HTTPException(status_code=502, detail=str(e))
    return BytesResponse(**data)


# How long /recycle waits for a browser to close before abandoning its thread.
RECYCLE_CLOSE_TIMEOUT_S = float(os.getenv("RECYCLE_CLOSE_TIMEOUT_S", "20"))


async def _close_or_abandon(executor, fn, label: str) -> bool:
    """Close on the worker thread, or give up on that thread entirely.

    RECYCLE IS THE ESCAPE HATCH AND IT WAS BEHIND THE LOCK. Both executors are
    single-slot, and this endpoint used to `await run_in_executor(...)` on them —
    so a worker parked inside Playwright (a navigation that never settles, a
    proxy black-hole during warm) made /recycle queue behind the very task it
    exists to clear. Observed 2026-09-14: /spa-fetch and /recycle both timing out
    for hours while /render, which has its own executor, answered 200 throughout.
    The in-page fetch already guards itself with an AbortController; warming does
    not, and that is the gap.

    So: wait a bounded time for a clean close, and if it does not come, abandon
    the thread and swap in a fresh executor. The old thread and its browser leak
    until the process restarts, which is the right trade — a leaked browser costs
    memory, a wedged service costs every caller.
    """
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(executor, fn), RECYCLE_CLOSE_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        log.warning("%s did not close in %.0fs — abandoning that worker thread", label, RECYCLE_CLOSE_TIMEOUT_S)
        return False


@app.post("/recycle")
async def recycle():
    global _cm, _browser, _page, _warmed, _page_thread
    global _render_cm, _render_browser, _render_thread
    # SNAPSHOT AND CLEAR FIRST, on the event loop, before any thread work. From
    # this line on the service has no session, so a /spa-fetch arriving while the
    # old browser is still closing builds a fresh one instead of racing the
    # teardown for the same globals.
    cm, render_cm = _cm, _render_cm
    _cm = _browser = _page = _warmed = None
    _page_thread = None
    _render_cm = _render_browser = None
    _render_thread = None
    akamai = await _close_or_abandon(_executor, lambda: _close_cm(cm, "akamai session"), "akamai session")
    render = await _close_or_abandon(_render_executor, lambda: _close_cm(render_cm, "render browser"), "render browser")
    _reset_executor()
    _reset_render_executor()
    return {"ok": True, "akamai_closed": akamai, "render_closed": render}

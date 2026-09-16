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
    → { status, url, html, ok }  # fill + submit a form with HUMAN interaction
                            # (pointer, per-char typing, real click) so scoring
                            # anti-bot sees behaviour, not just a good exit IP
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
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from camoufox.sync_api import Camoufox


PROXY_URL = os.environ.get("PROXY_URL", "")
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "60000"))
# Hard deadline for a single in-page fetch (see _build_fetch_expr).
AK_INPAGE_TIMEOUT_MS = int(os.environ.get("AK_INPAGE_TIMEOUT_MS", "45000"))
# Akamai's _abck cookie goes stale within a couple of POSTs unless the
# sensor keeps seeing human activity. A background task interacts with the
# warmed page every KEEPALIVE_SEC so the cookie stays mature between
# requests (set 0 to disable).
KEEPALIVE_SEC = float(os.environ.get("KEEPALIVE_SEC", "4"))

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
_proxy_session = None


def new_proxy_session() -> str:
    """Rotate to a fresh sticky exit. Called on (re)warm and by /recycle."""
    global _proxy_session
    _proxy_session = secrets.token_hex(6)
    log.info("proxy session token rotated -> %s", _proxy_session)
    return _proxy_session


def parse_proxy(url: str, session: str | None = None):
    """Convert "http://user:pass@host:port" into the Playwright proxy dict
    Camoufox accepts. When `session` is given (and the provider isn't already
    carrying a session token) it is appended to the password so the exit IP is
    sticky for the life of that browser."""
    if not url:
        return None
    m = re.match(r"^(https?)://([^:]+):([^@]+)@(.+)$", url)
    if not m:
        return None
    password = m.group(3)
    if session and "_session-" not in password:
        password = f"{password}_session-{session}"
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
    global _cm, _browser, _page, _warmed
    cm = _cm
    _cm = _browser = _page = _warmed = None
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
    global _cm, _browser, _page, _warmed
    key = (base_url, warm_path)
    if _page is not None and _warmed == key:
        return _page
    if _page is not None:
        _close_in_worker()
    proxy = parse_proxy(PROXY_URL, new_proxy_session())
    if proxy is None:
        raise RuntimeError("PROXY_URL must be set as http://user:pass@host:port")
    # geoip=True matches locale/timezone to the proxy's exit IP (an Italian
    # user signal Akamai expects); humanize adds human-like cursor motion.
    cm = Camoufox(headless=True, geoip=True, humanize=True, proxy=proxy)
    browser = cm.__enter__()
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


def _ensure_render_browser():
    """The shared render browser, relaunched if it died.

    LIVENESS IS CHECKED, and it must be: a Camoufox process can crash (OOM, a
    segfault in the bundled Firefox) while this module still holds the handle,
    and every later call then raises "Target page, context or browser has been
    closed" — a wedge that only a recycle or a redeploy cleared, because nothing
    ever asked whether the cached browser was still alive. Same failure the
    health skill records for the Crawl4AI pool. Checking is one cheap call.
    """
    global _render_cm, _render_browser
    if _render_browser is not None:
        try:
            if _render_browser.is_connected():
                return _render_browser
        except Exception:
            pass
        # Drop the handles and fall through to a fresh launch. Deliberately NOT
        # a teardown: the process is already gone, so cm.__exit__ has nothing to
        # close, and calling it here raised "Sync API inside the asyncio loop"
        # whenever this ran off the executor thread — turning a self-heal into a
        # second failure mode.
        log.warning("render browser is dead — relaunching")
        _render_cm = _render_browser = None
    # Sticky for this browser's lifetime as well. A page load is many requests,
    # and with geoip=True the fingerprint (locale/timezone) is derived from the
    # exit IP — so letting the exit rotate MID-LOAD advertises one identity while
    # the packets come from several countries' worth of IPs. Pinning costs nothing
    # here (each /render is still a fresh page) and keeps the story coherent.
    proxy = parse_proxy(PROXY_URL, new_proxy_session())
    if proxy is None:
        raise RuntimeError("PROXY_URL must be set as http://user:pass@host:port")
    cm = Camoufox(headless=True, geoip=True, humanize=True, proxy=proxy)
    browser = cm.__enter__()
    _render_cm, _render_browser = cm, browser
    log.info("render browser ready (residential)")
    return browser


def _close_render_in_worker() -> None:
    global _render_cm, _render_browser
    cm = _render_cm
    _render_cm = _render_browser = None
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


def _fresh_page(browser, viewport):
    """A page on a NEW browser context bound to a NEW exit IP.

    The render browser pins one exit for its lifetime, so the only way to get a
    fresh IP used to be /recycle — a full Camoufox teardown + relaunch (~30-60s).
    That is ruinous for any source metered PER IP (doctrine.it gates anonymous
    views that way: once an exit is spent every later load is a full-page
    restriction). A context carries its own proxy AND its own cookie jar, so this
    gives the same clean slate for the price of a context (~1s).

    Returns (page, context) — the caller must close the context.
    Falls back to (page, None) on the shared context if per-context proxying is
    unavailable, so existing consumers can never be broken by this path.
    geoip stays coherent because the pool is single-country (_country-IT).
    """
    try:
        ctx = browser.new_context(proxy=parse_proxy(PROXY_URL, new_proxy_session()),
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


def _do_form_submit(url, fields, submit, dismiss, success_url, wait_until, wait_ms,
                    settle_ms, timeout_ms, fresh_ip=True) -> dict:
    """Fill and submit a form the way a person does, then return where we landed.

    WHY A DEDICATED ENDPOINT rather than /eval. Scoring anti-bot (reCAPTCHA v3
    and friends) grades BEHAVIOUR as well as IP: pointer movement, per-character
    typing with human cadence, dwell between fields, and a real click on the
    site's own submit control — which lets the PAGE's handler mint its token in
    the main world, with the interaction history behind it. Driving the same form
    from /eval scores near zero however good the exit is, because the token is
    minted on a page nobody touched, and an in-page fetch() never clicks
    anything. (Measured on atoka's trial form: /eval + injected grecaptcha =
    "Error verifying reCAPTCHA" every time on a clean Italian residential exit.)

    Generic on purpose — `fields` is a list of {selector, value, action} — so any
    gated form is a config, not a new endpoint. `action`: "type" (default),
    "check", or "select".
    """
    browser = _ensure_render_browser()
    viewport = {"width": 1440, "height": 900}
    page, ctx = _fresh_page(browser, viewport) if fresh_ip else (browser.new_page(viewport=viewport), None)
    try:
        resp = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        if wait_ms:
            page.wait_for_timeout(wait_ms)

        # Cookie walls overlay the submit control; a click that lands on the
        # overlay silently does nothing.
        for sel in dismiss or []:
            try:
                page.click(sel, timeout=4000)
                page.wait_for_timeout(random.randint(300, 900))
            except Exception:
                pass

        # Arrive like a reader before touching anything.
        page.mouse.move(random.randint(200, 1200), random.randint(150, 700))
        page.wait_for_timeout(random.randint(600, 1600))
        page.mouse.wheel(0, random.randint(150, 500))
        page.wait_for_timeout(random.randint(400, 1000))

        for f in fields or []:
            sel = f.get("selector")
            action = (f.get("action") or "type").lower()
            val = f.get("value")
            try:
                if action == "check":
                    page.check(sel, timeout=8000)
                elif action == "select":
                    page.select_option(sel, val, timeout=8000)
                else:
                    page.click(sel, timeout=8000)
                    page.keyboard.type(val or "", delay=random.randint(45, 120))
                page.wait_for_timeout(random.randint(150, 500))
            except Exception as e:
                log.warning("form field %s (%s) failed: %s", sel, action, e)

        page.wait_for_timeout(random.randint(500, 1500))
        page.click(submit, timeout=15_000)

        # Race the two real outcomes: the success navigation, or the form coming
        # back with errors. Never just sleep — the old document carries no errors
        # and scores as a false success.
        try:
            if success_url:
                page.wait_for_url(re.compile(success_url), timeout=settle_ms)
            else:
                page.wait_for_load_state("networkidle", timeout=settle_ms)
        except Exception:
            pass
        page.wait_for_timeout(1200)

        final = page.url
        return {
            "status": resp.status if resp else 200,
            "url": final,
            "html": page.content(),
            "ok": bool(success_url and re.search(success_url, final)),
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


@app.on_event("startup")
async def _prewarm_render() -> None:
    """Launch the render browser before the first request needs it.

    /render, /screenshot, /eval and /bytes all share this browser, and building it
    costs tens of seconds. Callers upstream abort and fall back, so a cold start
    changes the ANSWER rather than just the latency. Fire-and-forget: startup must
    not block, and a failure has to leave lazy building intact.

    The Akamai warmed page stays lazy — it is keyed by (base_url, warm_path), so
    there is nothing to warm until a caller says which origin it wants.
    """
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
    if KEEPALIVE_SEC <= 0:
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
    return {
        "ok": True,
        "pid": os.getpid(),
        "session_ready": _page is not None,
        "akamai_worker_responsive": akamai_responsive,
        "proxy_configured": bool(PROXY_URL),
        "warmed_for": list(_warmed) if _warmed else None,
    }


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
        data = await loop.run_in_executor(
            _render_executor, _do_render, req.url, req.wait_until, req.wait_ms, req.timeout_ms,
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
        data = await loop.run_in_executor(
            _render_executor, _do_screenshot, req.url, req.wait_until, req.wait_ms,
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
        data = await loop.run_in_executor(
            _render_executor, _do_eval, req.url, req.wait_until, req.wait_ms, req.timeout_ms, req.js,
            req.fresh_ip,
        )
    except Exception as e:
        log.exception("eval failed url=%s", req.url)
        raise HTTPException(status_code=502, detail=str(e))
    return EvalResponse(**data)


class FormField(BaseModel):
    selector: str
    value: str | None = None
    action: str = Field("type", description="type | check | select")


class FormSubmitRequest(BaseModel):
    url: str
    fields: list[FormField] = Field(default_factory=list)
    submit: str = Field(..., description="CSS selector of the submit control")
    dismiss: list[str] = Field(default_factory=list, description="selectors clicked first (cookie walls)")
    success_url: str | None = Field(None, description="regex; matching final URL means success")
    wait_until: str = Field("domcontentloaded")
    wait_ms: int = Field(4000, ge=0, le=60_000)
    settle_ms: int = Field(20_000, ge=1000, le=120_000)
    timeout_ms: int = Field(120_000, ge=1000, le=180_000)
    fresh_ip: bool = Field(True, description="new context + new exit IP (scoring anti-bot is per-IP)")


class FormSubmitResponse(BaseModel):
    status: int
    url: str
    html: str
    ok: bool


@app.post("/form-submit", response_model=FormSubmitResponse)
async def form_submit(req: FormSubmitRequest):
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            _render_executor, _do_form_submit, req.url,
            [f.model_dump() for f in req.fields], req.submit, req.dismiss, req.success_url,
            req.wait_until, req.wait_ms, req.settle_ms, req.timeout_ms, req.fresh_ip,
        )
    except Exception as e:
        log.exception("form-submit failed url=%s", req.url)
        raise HTTPException(status_code=502, detail=str(e))
    return FormSubmitResponse(**data)


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
        data = await loop.run_in_executor(
            _render_executor, _do_bytes, req.url, req.timeout_ms)
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
    global _cm, _browser, _page, _warmed, _render_cm, _render_browser
    # SNAPSHOT AND CLEAR FIRST, on the event loop, before any thread work. From
    # this line on the service has no session, so a /spa-fetch arriving while the
    # old browser is still closing builds a fresh one instead of racing the
    # teardown for the same globals.
    cm, render_cm = _cm, _render_cm
    _cm = _browser = _page = _warmed = None
    _render_cm = _render_browser = None
    akamai = await _close_or_abandon(_executor, lambda: _close_cm(cm, "akamai session"), "akamai session")
    render = await _close_or_abandon(_render_executor, lambda: _close_cm(render_cm, "render browser"), "render browser")
    _reset_executor()
    _reset_render_executor()
    return {"ok": True, "akamai_closed": akamai, "render_closed": render}

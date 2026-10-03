"""Lane-Chromium lab runner: does reCAPTCHA v3 on atoka.io score a stealth
Chromium (Patchright) better than Camoufox/Firefox?

Local only, never production. Reproduces web_form_submit's Atoka step 0:
navigate, accept iubenda, type every field keystroke by keystroke, tick both
ToS boxes, click submit ONCE (a second POST is aborted by a route guard), and
classify the answer. Egress: the Camoufox service's Evomi Italian residential
PROXY_URL (read from `railway variables`, never printed), with a fresh sticky
session per attempt (same syntax as services/camoufox/proxy_session.py).

Appends one ledger line per attempt (lab README format). Never writes identity
values: evidence screenshots are taken after every input is blanked.

usage: python atoka_chromium.py VARIANT N
  VARIANT: bundled | chrome | warm   (warm = persistent profile warmed on
           google.com / youtube.com before the form; reused across attempts)
  env: LAB (ledger dir), HEADLESS=1 for headless=new, RAILWAY_CWD (a
       railway-linked checkout; the proxy URL is read from there).
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from patchright.sync_api import sync_playwright

URL = "https://atoka.io/en/try-atoka/"
SUBMIT = 'form:has([name="0-first_name"]) button[type="submit"]'
FIELDS = ["#id_0-first_name", "#id_0-last_name", "#id_0-email",
          "#id_0-password1", "#id_0-phone", "#id_0-company_vat"]
LAB = os.environ.get("LAB", ".")
LEDGER = os.path.join(LAB, "ledger.jsonl")
DRY = os.environ.get("DRY") == "1"  # abort the form POST, print instead of ledgering
EVIDENCE = os.path.join(LAB, "evidence", "lane-chromium")

FIRST = ["Marco", "Giulia", "Luca", "Francesca", "Andrea", "Chiara", "Matteo", "Sara", "Davide", "Elena"]
LAST = ["Bianchi", "Romano", "Colombo", "Ricci", "Marino", "Greco", "Bruno", "Gallo", "Conti", "Costa"]
WORDS = ["studio", "consulenze", "servizi", "tecnica", "digitale", "progetti", "soluzioni"]


def vat() -> str:
    d = [random.randint(0, 9) for _ in range(10)]
    s = 0
    for i, x in enumerate(d):
        if i % 2 == 0:
            s += x
        else:
            y = x * 2
            s += y - 9 if y > 9 else y
    return "".join(map(str, d)) + str((10 - s % 10) % 10)


def identity() -> list[str]:
    fn, ln = random.choice(FIRST), random.choice(LAST)
    tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    domain = f"{random.choice(WORDS)}-{ln.lower()}-{tag}.it"  # unregistered: no inbox
    pw = "".join(random.choices(string.ascii_letters, k=8)) + str(random.randint(10, 99)) + "!Aa"
    phone = "3" + "".join(random.choices(string.digits, k=9))
    return [fn, ln, f"{fn.lower()}.{ln.lower()}@{domain}", pw, phone, vat()]


def proxy_for_attempt() -> dict:
    url = subprocess.run("railway variables --service Camoufox --kv | sed -n 's/^PROXY_URL=//p'",
                         shell=True, capture_output=True, text=True,
                         cwd=os.environ.get("RAILWAY_CWD") or None).stdout.strip()
    m = re.match(r"^(https?)://([^:]+):([^@]+)@(.+)$", url)
    if not m:
        raise SystemExit("PROXY_URL missing or unparsable")
    password = m.group(3)
    if not re.search(r"_(?:hard|locked)?session-", password):
        password += f"_session-{secrets.token_hex(5)}_lifetime-60"
    return {"server": f"{m.group(1)}://{m.group(4)}", "username": m.group(2), "password": password}


def pause(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


def human_click(page, selector: str) -> None:
    loc = page.locator(selector).first
    try:
        loc.scroll_into_view_if_needed(timeout=5000)
        pause(0.2, 0.5)
    except Exception:
        pass
    box = loc.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        page.mouse.move(x, y, steps=random.randint(12, 30))
        pause(0.08, 0.25)
        page.mouse.click(x, y)
    else:
        page.locator(selector).first.click()


def type_human(page, text: str) -> None:
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.06, 0.19) + (random.random() < 0.08) * random.uniform(0.2, 0.5))


def accept_consent(page) -> None:
    for sel in ['button:has-text("Accetta tutto")', 'button:has-text("Accept all")',
                'button[aria-label*="Accetta"]', 'button[aria-label*="Accept"]']:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                loc.click()
                pause(1, 2)
                return
        except Exception:
            pass


def warm(page) -> None:
    page.goto("https://www.google.com/?hl=it", wait_until="domcontentloaded", timeout=45000)
    pause(1.5, 3)
    accept_consent(page)
    try:
        box = page.locator('textarea[name="q"], input[name="q"]').first
        box.click()
        type_human(page, random.choice(["atoka", "atoka cerved", "banca dati aziende italiane"]))
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        pause(2, 4)
        page.mouse.wheel(0, random.randint(300, 900))
        pause(1, 2)
    except Exception:
        pass
    page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=45000)
    pause(2, 4)
    accept_consent(page)
    page.mouse.wheel(0, random.randint(400, 1200))
    pause(2, 4)


def egress(page) -> dict:
    try:
        page.goto("https://ipwho.is/", wait_until="domcontentloaded", timeout=30000)
        d = json.loads(page.locator("body").inner_text(timeout=10000))
        c = d.get("connection") or {}
        return {"ip": d.get("ip"), "asn": c.get("asn"), "isp": c.get("isp"), "country": d.get("country_code")}
    except Exception as e:
        return {"error": type(e).__name__}


ORACLE = os.environ.get("ORACLE_URL", "https://tools-production-d199.up.railway.app/oracle/recaptcha")
VERDICT_RE = re.compile(r'<script type="application/json" id="oracle-verdict">(.*?)</script>', re.S)


def oracle_score(page) -> tuple[float | None, str]:
    """This browser's reCAPTCHA v3 score on our own oracle key, same exit,
    same humanised typing + one submit click (the production gate's shape)."""
    try:
        page.goto(ORACLE, wait_until="domcontentloaded", timeout=45000)
        pause(3, 5)
        name = random.choice(LAST)
        for sel, val in (("#company", f"{name} Servizi srl"),
                         ("#email", f"info.{name.lower()}{random.randint(10, 99)}@example.it")):
            human_click(page, sel)
            type_human(page, val)
            pause(0.3, 0.8)
        human_click(page, "#submit")
        page.wait_for_url(re.compile(r"/oracle/recaptcha/verify"), timeout=45000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        m = VERDICT_RE.search(page.content())
        if not m:
            return None, "no verdict node"
        d = json.loads(m.group(1))
        return (float(d["score"]) if isinstance(d.get("score"), (int, float)) else None,
                "ok" if d.get("success") else f"fail {d.get('error-codes')}")
    except Exception as e:
        return None, f"{type(e).__name__}"


def ledger(row: dict) -> None:
    if DRY:
        print("DRY", json.dumps(row)[:3000], flush=True)
        return
    fd = os.open(LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(row) + "\n").encode())
    finally:
        os.close(fd)


def attempt(pw, variant: str, n: int, warm_dir: str | None) -> dict:
    headless = os.environ.get("HEADLESS") == "1"
    cfg = {"engine": "patchright-chromium", "variant": variant, "headless": headless,
           "channel": "chrome" if variant == "chrome" else None,
           "profile": "warm-persistent" if variant == "warm" else "fresh", "typing": "keystroke",
           "proxy": "evomi-it-residential sticky fresh session"}
    t0 = time.time()
    durations: dict = {}
    posts: list = []
    row = {"ts": int(t0), "lane": "lane-chromium", "config": cfg, "egress": None, "oracle_score": None,
           "result": "unknown", "detail": "", "durations_s": durations, "evidence": None}
    user_dir = warm_dir or tempfile.mkdtemp(prefix="lc-prof-")
    ctx = None
    try:
        kw = dict(user_data_dir=user_dir, headless=headless, no_viewport=True, proxy=proxy_for_attempt(),
                  locale="it-IT", timezone_id="Europe/Rome",
                  # a headed window behind others on macOS is "occluded": Chromium then
                  # reports the page hidden and parks grecaptcha.execute until a
                  # repaint (observed: POST 100-150 s after the click, right when a
                  # screenshot forced a frame). Production Camoufox runs on Xvfb.
                  args=["--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling"])
        if variant == "chrome":
            kw["channel"] = "chrome"
        ctx = pw.chromium.launch_persistent_context(**kw)
        if warm_dir:  # reused profile: never carry the previous attempt's Atoka session
            ctx.clear_cookies(domain=re.compile(r"atoka\.io$"))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def is_post(req):
            # host-anchored: analytics beacons carry the page URL in their query
            u = urlsplit(req.url)
            return (req.method == "POST" and (u.hostname or "").endswith("atoka.io")
                    and "try-atoka" in u.path)

        rc_log: list = []
        row["recaptcha_reqs"] = rc_log

        def on_request(req):
            if is_post(req):
                posts.append(time.time())
            u = urlsplit(req.url)
            if "/recaptcha/" in u.path and "atoka" in (page.url or "") and len(rc_log) < 80:
                rc_log.append([round(time.time() - t0, 1), u.path.rsplit("/", 1)[-1][:24]])

        post_log: list = []
        closed = [False]  # set once the run has its verdict: nothing may POST after it
        row["posts"] = post_log

        def token_len(req) -> int | None:
            try:
                m = re.search(r"(?:^|&)0-captcha=([^&]*)", req.post_data or "")
                return len(m.group(1)) if m else None
            except Exception:
                return None

        def guard(route):
            # one POST only: anything after the first is aborted before it leaves
            req = route.request
            if is_post(req) and (closed[0] or DRY):
                if DRY:
                    post_log.append({"t": round(time.time() - t0, 2), "dry_aborted": True,
                                     "captcha_len": token_len(req)})
                    closed[0] = True
                    return route.abort()
                post_log.append({"t": round(time.time() - t0, 2), "aborted": True, "after_verdict": True})
                return route.abort()
            if is_post(req):
                entry = {"t": round(time.time() - t0, 2), "nav": req.is_navigation_request(),
                         "type": req.resource_type, "captcha_len": token_len(req)}
                post_log.append(entry)
                if len(post_log) > 1:
                    entry["aborted"] = True
                    return route.abort()
            return route.continue_()

        def on_done(req):
            if is_post(req) and post_log:
                try:
                    resp = req.response()
                    post_log[0].setdefault("status", resp.status if resp else None)
                except Exception:
                    pass

        def on_failed(req):
            if is_post(req) and post_log:
                post_log[-1].setdefault("failure", (req.failure or "")[:60])

        page.on("requestfinished", on_done)
        page.on("requestfailed", on_failed)
        page.on("request", on_request)
        page.route(re.compile(r"atoka\.io/.*try-atoka"), guard)

        row["egress"] = egress(page)
        durations["egress"] = round(time.time() - t0, 1)
        if variant == "warm":
            t = time.time()
            warm(page)
            durations["warm"] = round(time.time() - t, 1)

        t = time.time()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        pause(2, 4)
        try:
            btn = page.locator(".iubenda-cs-accept-btn").first
            btn.wait_for(state="visible", timeout=10000)
            pause(0.6, 1.5)
            human_click(page, ".iubenda-cs-accept-btn")
        except Exception:
            cfg["iubenda"] = "absent"
        page.locator(FIELDS[0]).wait_for(state="visible", timeout=30000)
        page.mouse.wheel(0, random.randint(150, 400))
        pause(1, 2.5)
        for sel, val in zip(FIELDS, identity()):
            human_click(page, sel)
            pause(0.2, 0.6)
            type_human(page, val)
            pause(0.4, 1.2)
        for sel in ("#id_0-tos", "#id_0-tos1"):
            loc = page.locator(sel).first
            try:
                human_click(page, sel)
            except Exception:
                pass
            if not loc.is_checked():
                loc.check(force=True)
            pause(0.4, 1.0)
        assert page.locator('[name="0-email_last"]').input_value() == "", "honeypot not empty"
        durations["fill"] = round(time.time() - t, 1)
        pause(1, 2.5)
        t = time.time()
        page.evaluate("""() => { window.__lc = []; const f = document.querySelector('[name="0-first_name"]').form;
            const now = () => Math.round(performance.now());
            document.addEventListener('click', e => window.__lc.push(['click', e.target.tagName + '.' + String(e.target.className).slice(0, 30), now()]), true);
            f.addEventListener('submit', e => window.__lc.push(['submit', now()]), true);
            f.addEventListener('invalid', e => window.__lc.push(['invalid', e.target.id, now()]), true);
            const c = document.getElementById('id_0-captcha');
            const iv = setInterval(() => { if (c && c.value) { window.__lc.push(['token', c.value.length, now()]); clearInterval(iv); } }, 100);
            return 1; }""")
        cfg["form_valid_preclick"] = page.evaluate("() => document.querySelector('[name=\"0-first_name\"]').form.checkValidity()")
        page.bring_to_front()
        cfg["visibility_preclick"] = page.evaluate("document.visibilityState")
        human_click(page, SUBMIT)
        cfg["t_click"] = round(time.time() - t0, 2)
        deadline = time.time() + 150  # Chromium's first POST came ~100 s after the click once
        while time.time() < deadline:
            if post_log and deadline - time.time() > 60:
                deadline = time.time() + 60  # a POST left: 60 s more for its verdict
            pause(0.5, 0.5)
            url = page.url
            if re.search(r"/try-atoka/complete/?(\?.*)?$", url):
                row["result"], row["detail"] = "accepted", "302 -> /try-atoka/complete/"
                break
            try:
                cfg["page_events"] = page.evaluate("() => window.__lc")
            except Exception:
                pass
            if not post_log:
                continue
            try:
                body = page.locator("body").inner_text(timeout=2000)
            except Exception:
                continue
            if re.search(r"Error verifying reCAPTCHA", body, re.I):
                row["result"], row["detail"] = "captcha_rejected", "re-render: Error verifying reCAPTCHA"
                break
            if re.search(r"And now, what happens\?|E ora,? cosa succede", body):
                row["result"], row["detail"] = "accepted", "manual-review completion marker"
                break
            if re.search(r"To complete the registration click here", body):
                row["result"], row["detail"] = "other", "wizard gate after step0 (reCAPTCHA passed; not followed)"
                break
        else:
            if not post_log:
                row["result"], row["detail"] = "zero_post", "submit click produced no POST within 150s"
            else:
                row["detail"] = f"no verdict within 150s; url path {re.sub(r'^https?://[^/]+', '', page.url)}"
                if re.search(r"error", page.locator("body").inner_text(timeout=2000), re.I):
                    row["result"], row["detail"] = "other", row["detail"] + "; page shows an error (non-captcha?)"
        closed[0] = True
        durations["submit_to_verdict"] = round(time.time() - t, 1)
        mints = [r[0] for r in rc_log if r[1] == "reload" and r[0] >= cfg["t_click"]]
        if mints:
            durations["click_to_mint"] = round(mints[0] - cfg["t_click"], 2)
        if post_log and not post_log[0].get("aborted"):
            durations["click_to_post"] = round(post_log[0]["t"] - cfg["t_click"], 2)
        if os.environ.get("ORACLE", "1") == "1":
            # AFTER the verdict, in its own tab: an oracle mint earlier in the same
            # tab parked Atoka's grecaptcha.execute for 100-150 s (3 runs; without
            # it click->POST is ~0.5 s). Same browser, same exit, minutes apart.
            t = time.time()
            op = ctx.new_page()
            row["oracle_score"], cfg["oracle"] = oracle_score(op)
            cfg["oracle_when"] = "after_verdict_same_exit"
            op.close()
            durations["oracle"] = round(time.time() - t, 1)
        row["detail"] += f"; posts_sent={sum(1 for p in post_log if not p.get('aborted'))}"
        try:
            os.makedirs(EVIDENCE, exist_ok=True)
            page.evaluate("document.querySelectorAll('input,textarea').forEach(e=>{if(e.type!=='hidden'&&e.type!=='checkbox')e.value=''})")
            path = os.path.join(EVIDENCE, f"{int(t0)}-{variant}-{n}.png")
            page.screenshot(path=path)
            row["evidence"] = path
        except Exception:
            pass
    except Exception as e:
        row["result"] = "other" if not row.get("posts") else "unknown"
        row["detail"] = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}; posts_sent={len(row.get('posts') or [])}"
    finally:
        durations["total"] = round(time.time() - t0, 1)
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass
        if not warm_dir:
            shutil.rmtree(user_dir, ignore_errors=True)
    ledger(row)
    return row


def main() -> None:
    variant = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    assert variant in ("bundled", "chrome", "warm")
    warm_dir = os.path.join(LAB, "lane-chromium-warm-profile") if variant == "warm" else None
    with sync_playwright() as pw:
        for i in range(n):
            row = attempt(pw, variant, i, warm_dir)
            print(json.dumps({k: row[k] for k in ("lane", "result", "detail", "durations_s")}
                             | {"asn": (row["egress"] or {}).get("asn"), "isp": (row["egress"] or {}).get("isp")}),
                  flush=True)
            time.sleep(random.uniform(8, 15))


if __name__ == "__main__":
    main()

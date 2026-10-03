"""Stealth-score diagnostics: measure the FORM browser's reCAPTCHA v3 score.

The oracle (our own key; page + siteverify) is served by the public Tools
service at /oracle/recaptcha — a key only mints on hostnames registered on it,
and this sidecar is private. This module drives the form browser to it.

SAME PATH AS FORMS, by construction rather than by imitation:
- `/form-score-probe` calls `run_isolated_form` with the `_form_browser`
  factory (session, headed, profile) through the shared FormWorker admission,
  and the runner is `run_form` itself: the same wait, arrival scroll, dwell,
  humanized keystroke typing into the oracle's dummy fields and ONE submit
  click whose handler mints the token (grecaptcha.execute inside the submit
  listener — Atoka's django-recaptcha V3 shape). The verify POST navigates to
  a page carrying the verdict as an inert JSON node, parsed here.
- `/form-warm` and the exit pre-check reuse `run_isolated_form` with their own
  `runner=` (forms-infra's hook; the launch, context, retry and teardown are
  the forms' own), each job with its own FormLive, so every probe, warm and
  pre-check emits the standard `form-run` summary line (`probe` names it).

The oracle is the ONLY score this project tunes against: never point a probe
at a third-party form. Never logs tokens or field values (the dummy values
are random and go only to our own oracle).

`/form-exit-select` probes candidate exits and pins the first passing one to
a profile; `/form-exits` shows the blocklist (profile_store) and pins.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from functools import partial
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field

import profile_store
import proxy_session
from form_flow import EGRESS_URL, FormLive, first_page, human_click, normalize_egress

log = logging.getLogger("camoufox.score")

DEFAULT_THRESHOLD = 0.7
DEFAULT_VISITS = ("https://www.google.com/", "https://www.youtube.com/")
ACTION_RE = re.compile(r"^[A-Za-z0-9_/]{1,64}$")
VERDICT_RE = re.compile(
    r'<script type="application/json" id="oracle-verdict">(.*?)</script>', re.S)
# Consent walls on the warm-up pages (IT exits get the Italian copy). Accepting
# is what most visitors do, and the consent cookie is part of a lived-in jar.
CONSENT_RE = re.compile(r"^\s*(Accetta tutto|Accept all|Accetta)\s*$", re.I)

_deps: dict = {}


# ── pure helpers (unit-tested) ──────────────────────────────────────

def oracle_urls(oracle_url: str, action: str | None = None):
    """(page_url, verify_url) for an oracle base URL. Raises ValueError."""
    parts = urlsplit(oracle_url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("oracle_url must be an http(s) URL")
    if action is not None and not ACTION_RE.match(action):
        raise ValueError("invalid action")
    base = parts._replace(query="", fragment="").geturl().rstrip("/")
    page = base + ("?" + urlencode({"action": action}) if action else "")
    return page, base + "/verify"


def parse_verdict(html: str | None):
    """The oracle's verdict out of the verify page's DOM, or None."""
    match = VERDICT_RE.search(html or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    def number(key):
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def text(key):
        value = data.get(key)
        return value if isinstance(value, str) else None

    codes = data.get("error-codes")
    return {
        "success": data.get("success") is True,
        "score": number("score"),
        "action": text("action"),
        "hostname": text("hostname"),
        "challenge_ts": text("challenge_ts"),
        "error-codes": [c for c in codes if isinstance(c, str)] if isinstance(codes, list) else [],
        "action_ok": data.get("action_ok") if isinstance(data.get("action_ok"), bool) else None,
        "token_age_s": number("token_age_s"),
        "client_ip": text("client_ip"),
        "token_length": number("token_length"),
        "page_dwell_s": number("page_dwell_s"),
        "mint_s": number("mint_s"),
        "mint_error": text("mint_error"),
    }


_COMPANIES = ("Rossi", "Bianchi", "Ferrari", "Esposito", "Romano", "Colombo", "Ricci",
              "Marino", "Greco", "Bruno", "Gallo", "Conti", "De Luca", "Costa")
_KINDS = ("Consulting", "Servizi", "Impianti", "Logistica", "Studio", "Tecnologie", "Group")


def probe_fields(count: int, rng=random):
    """Dummy oracle fields, typed like a form's: 1-3 of company/email/phone."""
    name = rng.choice(_COMPANIES)
    values = [
        {"selector": "#company", "value": f"{name} {rng.choice(_KINDS)} {rng.choice(('srl', 'snc', 'spa'))}"},
        {"selector": "#email", "value": f"info.{name.lower().replace(' ', '')}{rng.randint(10, 99)}@example.it"},
        {"selector": "#phone", "value": f"3{rng.randint(20, 49)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}"},
    ]
    return values[:max(1, min(3, int(count)))]


def probe_params(page_url, verify_url, field_count, wait_ms, rng=random):
    """run_form kwargs: the oracle is a plain one-POST form."""
    return {
        "url": page_url,
        "fields": probe_fields(field_count, rng),
        "submit": "#submit",
        "success_url": re.escape(urlsplit(verify_url).path),
        "submission_urls": [verify_url],
        "captcha_field": "g-recaptcha-response",
        "wait_ms": wait_ms,
        # Up to 30 s for the verify page; and the verdict is read from the
        # POST's own response body when the page has not rendered by then.
        "settle_ms": 30000,
        "capture_submission_body": True,
    }


def summarize_probe(data, *, session, profile, headed, threshold, started):
    """Probe answer: verdict + exit, never the page's HTML or a token."""
    data = data or {}
    diagnostics = data.get("diagnostics") or {}
    # The page first; the submission's own response body when the page had
    # not navigated in time (a "capture miss": Google scored it, we lost it).
    verdict = parse_verdict(data.get("html")) or parse_verdict(data.get("submission_body"))
    egress = diagnostics.get("egress") or {}
    score = verdict["score"] if verdict else None
    return {
        "score": score,
        "success": verdict["success"] if verdict else False,
        "action": verdict["action"] if verdict else None,
        "hostname": verdict["hostname"] if verdict else None,
        "error-codes": verdict["error-codes"] if verdict else [],
        "passed": score is not None and score >= threshold,
        "threshold": threshold,
        "verdict": verdict,
        "egress": {"country": egress.get("country"), "asn": egress.get("asn"),
                   "isp": egress.get("isp"), "ip": verdict["client_ip"] if verdict else None},
        "exit_session": session,
        "profile": profile,
        "headed": headed,
        "form": {
            "error": data.get("error"),
            "ok": data.get("ok"),
            "status": data.get("status"),
            "form_submissions": data.get("form_submissions"),
            "token_present": diagnostics.get("token_present"),
            "submission_tokens": diagnostics.get("submission_tokens"),
            "phase": diagnostics.get("phase"),
            "failure_class": diagnostics.get("failure_class"),
            # The pre-input reCAPTCHA gate: did the client become usable,
            # by which signal, was the page reloaded once, and which pieces
            # failed (path classes only — form_flow.captcha_path_class).
            "captcha_ready": diagnostics.get("captcha_ready"),
            "captcha_signal": diagnostics.get("captcha_signal"),
            "captcha_reload": diagnostics.get("captcha_script_reload"),
            "captcha_failed": diagnostics.get("captcha_failed"),
            "nav_error": diagnostics.get("nav_error"),
            "exit_rotated": diagnostics.get("exit_rotated"),
            "captcha_waited_inflight": diagnostics.get("captcha_waited_inflight"),
        },
        "duration_s": round(time.monotonic() - started, 1),
    }


# ── runners (inside the form worker thread, on the forms' own context) ──

def _browse(page, dwell_s, deadline):
    """Read a page: wheel scrolls both ways and pointer drift, never at the
    viewport edge (camoufox #751: humanized moves through x==0/y==0 deadlock
    the input chain on Linux + Xvfb before 156.0.1-beta.32)."""
    end = min(time.monotonic() + dwell_s, deadline - 2)
    x, y = random.randint(300, 900), random.randint(200, 600)
    page.mouse.move(x, y)
    while time.monotonic() < end:
        roll = random.random()
        if roll < 0.45:
            page.mouse.wheel(0, random.randint(150, 600))
        elif roll < 0.6:
            page.mouse.wheel(0, -random.randint(100, 300))
        else:
            x = max(60, min(1380, x + random.randint(-220, 220)))
            y = max(60, min(840, y + random.randint(-160, 160)))
            page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(400, 1400))


def _accept_consent(page, deadline):
    try:
        button = page.get_by_role("button", name=CONSENT_RE).first
        button.wait_for(state="visible", timeout=int(max(0, min(4.0, deadline - time.monotonic() - 2)) * 1000))
    except Exception:
        return False
    try:
        human_click(page, button, lambda: int(max(1, deadline - time.monotonic()) * 1000), pre_submit=False)
        page.wait_for_timeout(random.randint(800, 1800))
        return True
    except Exception:
        return False


def _egress_from_page(page):
    try:
        return normalize_egress(page.evaluate(
            "fetch('" + EGRESS_URL + "',{signal:AbortSignal.timeout(8000)})"
            ".then(r=>r.json()).catch(()=>null)"))
    except Exception:
        return None


def warm_flow(context, *, timeout_ms, url, visits=DEFAULT_VISITS, dwell_ms=None):
    """google.com, youtube.com, then the target origin — dwell and scroll on
    each — so the profile's jar carries a lived-in google cookie set before a
    form ever mints a token on it."""
    deadline = time.monotonic() + timeout_ms / 1000
    out = {"visits": [], "egress": None, "error": None}
    page, _reused = first_page(context)  # a profile's launch tab, never a 2nd
    try:
        for target in [*visits, url]:
            left = deadline - time.monotonic()
            if left < 8:
                out["error"] = "deadline"
                break
            entry = {"host": urlsplit(target).hostname, "ok": False, "status": None, "consent": False}
            started = time.monotonic()
            try:
                response = page.goto(target, wait_until="domcontentloaded", timeout=int(min(30.0, left - 4) * 1000))
                entry["status"] = response.status if response is not None else None
                page.wait_for_timeout(random.randint(900, 2200))
                entry["consent"] = _accept_consent(page, deadline)
                dwell = (dwell_ms / 1000) if dwell_ms else random.uniform(6, 14)
                _browse(page, dwell, deadline)
                entry["ok"] = True
            except Exception as error:
                entry["error"] = type(error).__name__  # class only, never text
            entry["dwell_s"] = round(time.monotonic() - started, 1)
            out["visits"].append(entry)
        out["egress"] = _egress_from_page(page)
    finally:
        try:
            page.close()
        except Exception:
            pass
    return out


def egress_flow(context, *, timeout_ms, url):
    """Which IP/ASN does this exit token land on? One navigation, no page JS."""
    page, _reused = first_page(context)  # a profile's launch tab, never a 2nd
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=int(min(20000, timeout_ms)))
        return {"egress": normalize_egress(response.json() if response is not None else None)}
    except Exception as error:
        return {"egress": None, "error": type(error).__name__}
    finally:
        try:
            page.close()
        except Exception:
            pass


# ── async orchestration ─────────────────────────────────────────────

def _live(profile, headed, kind):
    """One FormLive per job (forms-infra's per-job mark + form-run summary);
    `probe` names which stealth routine the summary line belongs to."""
    live = FormLive(profile=profile, headed=headed, camoufox=_deps.get("camoufox"))
    live.note(probe=kind)
    return live


async def _run(job, url, deadline, live):
    try:
        return await _deps["worker"].run(job, url=url, deadline=deadline, live=live)
    except Exception as error:
        # FormRetryable (a pre-POST park) or the wedge timeout: no verdict.
        log.warning("score probe job failed (%s)", type(error).__name__)
        return {"error": "probe_unavailable", "failure_class": type(error).__name__, "diagnostics": {}}


async def run_probe(*, oracle_url, session, profile, headed, wait_ms, field_count, action, timeout_ms,
                    rotatable=False, rotated=None):
    page_url, verify_url = oracle_urls(oracle_url, action)
    deadline = time.monotonic() + timeout_ms / 1000
    params = probe_params(page_url, verify_url, field_count, wait_ms)
    factory = partial(_deps["browser_factory"], session, False, headed, profile)
    live = _live(profile, headed, "score")

    def rotate():
        # The forms' own exit rotation; `rotated` tells the summary which
        # exit token the verdict actually came from.
        token = proxy_session.new_token()
        if rotated is not None:
            rotated["session"] = token
        return partial(_deps["browser_factory"], token, False, headed, profile)

    job = partial(_deps["run_isolated"], factory, deadline=deadline, live=live,
                  rotate_factory=rotate if rotatable else None, **params)
    return await _run(job, page_url, deadline, live)


async def run_egress(session, timeout_ms=30000):
    deadline = time.monotonic() + timeout_ms / 1000
    factory = partial(_deps["browser_factory"], session, False, False, None)
    live = _live(None, False, "egress")
    job = partial(_deps["run_isolated"], factory, deadline=deadline, runner=egress_flow,
                  live=live, url=EGRESS_URL)
    return await _run(job, EGRESS_URL, deadline, live)


def _record(summary, threshold, record_exit):
    verdict = summary.get("verdict") or {}
    egress = summary.get("egress") or {}
    if record_exit and verdict.get("success") and summary.get("score") is not None:
        profile_store.record_exit_score(egress.get("ip"), egress.get("asn"), summary["score"], threshold)
    summary["blocked"] = profile_store.blocked_reason(egress.get("ip"), egress.get("asn"), threshold)


# ── HTTP ────────────────────────────────────────────────────────────

class ScoreProbeRequest(BaseModel):
    oracle_url: str = Field(..., max_length=500, description="the Tools oracle page, e.g. https://<tools>/oracle/recaptcha")
    profile: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    headed: bool = Field(True, description="headed under xvfb: the score-gated form path")
    fresh_ip: bool = Field(True)
    exit_session: str | None = Field(None, max_length=64)
    sticky_exit: bool | None = Field(None, description="with a profile: reuse/pin its stored exit; None = FORM_PROFILE_STICKY_EXIT")
    wait_ms: int = Field(4000, ge=0, le=60_000, description="dwell after load (forms default 4000)")
    field_count: int = Field(2, ge=1, le=3)
    action: str | None = Field(None, max_length=64)
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0, le=1)
    record_exit: bool = Field(True)
    timeout_ms: int = Field(120_000, ge=10_000, le=240_000)


class WarmRequest(BaseModel):
    profile: str = Field(..., max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    target_url: str = Field(..., max_length=500, description="origin visited last (the form site)")
    visits: list[str] | None = Field(None, max_length=5)
    headed: bool = Field(True)
    fresh_ip: bool = Field(True)
    exit_session: str | None = Field(None, max_length=64)
    sticky_exit: bool | None = Field(True, description="warm the profile ON its pinned exit (default true)")
    dwell_ms: int | None = Field(None, ge=1000, le=60_000)
    timeout_ms: int = Field(150_000, ge=10_000, le=240_000)


class ExitSelectRequest(BaseModel):
    oracle_url: str = Field(..., max_length=500)
    profile: str = Field(..., max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    headed: bool = Field(True)
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0, le=1)
    max_tries: int = Field(4, ge=1, le=6)
    timeout_ms: int = Field(270_000, ge=30_000, le=290_000)


def _check_url(url):
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=400, detail="URLs must be http(s)")


async def form_score_probe(req: ScoreProbeRequest):
    try:
        oracle_urls(req.oracle_url, req.action)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    started = time.monotonic()
    session, _pin = _deps["resolve_session"](req.profile, req.exit_session, req.fresh_ip, req.sticky_exit)
    sticky = profile_store.sticky_default() if req.sticky_exit is None else req.sticky_exit
    rotated = {}
    data = await run_probe(oracle_url=req.oracle_url, session=session, profile=req.profile,
                           headed=req.headed, wait_ms=req.wait_ms, field_count=req.field_count,
                           action=req.action, timeout_ms=req.timeout_ms,
                           rotatable=not req.exit_session and not (req.profile and sticky),
                           rotated=rotated)
    session = rotated.get("session", session)
    summary = summarize_probe(data, session=session, profile=req.profile, headed=req.headed,
                              threshold=req.threshold, started=started)
    _record(summary, req.threshold, req.record_exit)
    sticky = profile_store.sticky_default() if req.sticky_exit is None else req.sticky_exit
    if req.profile and sticky and summary["egress"]["ip"]:
        summary["exit_ip_changed"] = profile_store.note_exit_seen(
            req.profile, ip=summary["egress"]["ip"], score=summary["score"])
    summary["profile_persistent"] = profile_store.is_persistent_root()
    log.info("score probe: score=%s passed=%s ip=%s asn=%s profile=%s headed=%s error=%s",
             summary["score"], summary["passed"], summary["egress"]["ip"], summary["egress"]["asn"],
             req.profile, req.headed, summary["form"]["error"])
    return summary


async def form_warm(req: WarmRequest):
    _check_url(req.target_url)
    for url in req.visits or []:
        _check_url(url)
    started = time.monotonic()
    session, _pin = _deps["resolve_session"](req.profile, req.exit_session, req.fresh_ip, req.sticky_exit)
    deadline = time.monotonic() + req.timeout_ms / 1000
    visits = tuple(req.visits) if req.visits is not None else DEFAULT_VISITS
    factory = partial(_deps["browser_factory"], session, False, req.headed, req.profile)
    live = _live(req.profile, req.headed, "warm")
    job = partial(_deps["run_isolated"], factory, deadline=deadline, runner=warm_flow, live=live,
                  url=req.target_url, visits=visits, dwell_ms=req.dwell_ms)
    data = await _run(job, req.target_url, deadline, live)
    egress = data.get("egress") or None
    if egress and egress.get("ip") and req.sticky_exit is not False:
        data["exit_ip_changed"] = profile_store.note_exit_seen(req.profile, ip=egress["ip"])
    data.pop("html", None)
    data.update(profile=req.profile, exit_session=session, headed=req.headed,
                duration_s=round(time.monotonic() - started, 1),
                profile_persistent=profile_store.is_persistent_root())
    log.info("form warm: profile=%s visits=%s error=%s", req.profile,
             [(v.get("host"), v.get("ok")) for v in data.get("visits") or []], data.get("error"))
    return data


# A candidate needs a pre-check (~5-30 s) and a full humanized probe (~25-40 s).
CANDIDATE_MIN_MS = 60_000


async def probe_candidates(*, oracle_url, profile, headed, threshold, max_tries, end,
                           reserve_s=0.0, sessions=()):
    """Probe candidate exits on OUR oracle; stop at the first scoring >= threshold.

    Shared by /form-exit-select and the form score gate. Each candidate gets
    a cheap headless egress pre-check first: an IP or ASN the blocklist
    already knows as low-scoring is skipped for the price of one launch, not
    a whole humanized form. Every verdict is recorded in the blocklist.
    `sessions` are tried first (a sticky profile's own exit), then fresh
    tokens. `reserve_s` is budget kept back for the caller (the form itself):
    no candidate starts unless it fits before `end - reserve_s`. Only our
    oracle and the egress echo are ever contacted here.

    Returns {passed, session, score, egress, tries}.
    """
    tries = []
    queue = list(sessions)
    for _ in range(max_tries):
        left_ms = int((end - reserve_s - time.monotonic()) * 1000)
        if left_ms < CANDIDATE_MIN_MS:
            break
        session = queue.pop(0) if queue else proxy_session.new_token()
        pre = await run_egress(session, min(30_000, left_ms - 45_000))
        egress = pre.get("egress") or {}
        reason = profile_store.blocked_reason(egress.get("ip"), egress.get("asn"), threshold)
        if reason:
            tries.append({"skipped": reason, "egress": egress})
            continue
        started = time.monotonic()
        left_ms = int((end - reserve_s - time.monotonic()) * 1000)
        data = await run_probe(oracle_url=oracle_url, session=session, profile=profile,
                               headed=headed, wait_ms=4000, field_count=2, action=None,
                               timeout_ms=max(10_000, min(120_000, left_ms - 5_000)))
        summary = summarize_probe(data, session=session, profile=profile, headed=headed,
                                  threshold=threshold, started=started)
        _record(summary, threshold, True)
        tries.append({k: summary[k] for k in ("score", "passed", "egress", "error-codes", "blocked")}
                     | {"error": summary["form"]["error"]})
        if summary["passed"]:
            # The IP the score is ABOUT (the oracle saw it); the form is
            # verified against it before it contacts the target.
            return {"passed": True, "session": session, "score": summary["score"],
                    "egress": summary["egress"], "tries": tries,
                    "ip": summary["egress"].get("ip") or egress.get("ip"),
                    "precheck_ip": egress.get("ip")}
    return {"passed": False, "session": None, "score": None, "egress": None, "tries": tries,
            "ip": None, "precheck_ip": None}


# ── the score gate for real submits (/form-submit score_gate) ────────
#
# Before a real form, candidate exits are probed on OUR oracle with the
# form's own launch config (headed, profile or isolated); the form then runs
# pinned to the first exit that scores >= threshold. Nothing touches the
# target before that, so a gate that finds no exit is provably zero-POST.
GATE_RESERVE_PLAIN_S = 100.0   # a one-POST form: launch + nav + fill + outcome
GATE_RESERVE_WIZARD_S = 240.0  # a wizard needs ~240 s on its own


def gate_wanted(score_gate, headed, exit_session, inspect_only):
    """Explicit score_gate wins; default on when headed and no exit is pinned."""
    if inspect_only:
        return False
    if score_gate is not None:
        return bool(score_gate)
    return bool(headed and not exit_session)


def form_reserve_s(timeout_ms, wizard):
    """Budget kept back for the form itself out of the caller's timeout_ms
    (squeezed, never below 30 s, so an explicit gate on a short deadline
    still fits one candidate)."""
    want = GATE_RESERVE_WIZARD_S if wizard else GATE_RESERVE_PLAIN_S
    return min(want, max(30.0, timeout_ms / 1000 - CANDIDATE_MIN_MS / 1000 - 5.0))


def gate_fits(timeout_ms, wizard):
    """Does the deadline hold the full form reserve AND one candidate? An
    IMPLICIT gate is skipped when not (default 120 s plain form: no), so the
    default-on gate never turns a call that used to run into a 503."""
    want = GATE_RESERVE_WIZARD_S if wizard else GATE_RESERVE_PLAIN_S
    return timeout_ms / 1000 >= want + CANDIDATE_MIN_MS / 1000 + 5.0


async def run_gate(*, oracle_url, profile, headed, threshold, tries, deadline, reserve_s,
                   sessions=()):
    outcome = await probe_candidates(oracle_url=oracle_url, profile=profile, headed=headed,
                                     threshold=threshold, max_tries=tries, end=deadline,
                                     reserve_s=reserve_s, sessions=sessions)
    log.info("score gate: passed=%s score=%s asn=%s after %d tries (profile=%s headed=%s)",
             outcome["passed"], outcome["score"], (outcome["egress"] or {}).get("asn"),
             len(outcome["tries"]), profile, headed)
    return outcome


def gate_record(outcome):
    """diagnostics.score_gate: what the gate tried and what it chose (no tokens)."""
    tries = outcome.get("tries") or []
    egress = outcome.get("egress") or {}
    return {"passed": bool(outcome.get("passed")), "tries": len(tries),
            "probed": sum(1 for t in tries if "skipped" not in t),
            "skipped": [t["skipped"] for t in tries if "skipped" in t],
            "scores": [t.get("score") for t in tries if "skipped" not in t],
            "chosen_score": outcome.get("score"), "asn": egress.get("asn"),
            # gate_ip: what the chosen score is about; precheck_ip: the same
            # token's IP on the headless pre-check moments earlier.
            "gate_ip": outcome.get("ip"), "precheck_ip": outcome.get("precheck_ip")}


async def form_exit_select(req: ExitSelectRequest):
    try:
        oracle_urls(req.oracle_url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    end = time.monotonic() + req.timeout_ms / 1000
    # Candidates are probed ISOLATED (no profile): the profile must not be
    # seen from every exit that gets rejected on the way.
    outcome = await probe_candidates(oracle_url=req.oracle_url, profile=None, headed=req.headed,
                                     threshold=req.threshold, max_tries=req.max_tries, end=end)
    tries = outcome["tries"]
    if outcome["passed"]:
        session = outcome["session"]
        profile_store.remember_exit(req.profile, session, ip=outcome["egress"]["ip"], score=outcome["score"])
        log.info("exit select: pinned profile=%s ip=%s score=%s after %d tries",
                 req.profile, outcome["egress"]["ip"], outcome["score"], len(tries))
        return {"pinned": True, "profile": req.profile, "exit_session": session,
                "score": outcome["score"], "tries": tries,
                "profile_persistent": profile_store.is_persistent_root()}
    log.info("exit select: nothing pinned for profile=%s after %d tries", req.profile, len(tries))
    return {"pinned": False, "profile": req.profile, "tries": tries,
            "profile_persistent": profile_store.is_persistent_root()}


async def form_exits():
    data = profile_store.load_blocklist()
    blocked = {ip: entry for ip, entry in data["ips"].items()
               if profile_store.blocked_reason(ip, None, data=data)}
    return {"root": profile_store.root(), "persistent": profile_store.is_persistent_root(),
            "ips_seen": len(data["ips"]), "blocked_ips": blocked, "asns": data["asns"],
            "pinned": profile_store.pinned_exits()}


def register(app, *, worker, browser_factory, run_isolated, resolve_session, camoufox=None):
    """Mount on the app with the forms' own factory/worker (no import cycle:
    app.py owns them and hands them over)."""
    _deps.update(worker=worker, browser_factory=browser_factory,
                 run_isolated=run_isolated, resolve_session=resolve_session, camoufox=camoufox)
    app.post("/form-score-probe")(form_score_probe)
    app.post("/form-warm")(form_warm)
    app.post("/form-exit-select")(form_exit_select)
    app.post("/form-exits")(form_exits)

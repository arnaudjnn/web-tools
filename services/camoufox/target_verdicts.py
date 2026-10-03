"""Target verdicts: what each form host actually said to each exit ASN.

Why (measured 2026-10-03, Atoka): our oracle's reCAPTCHA v3 score predicts a
target's verdict poorly — reCAPTCHA scores PER SITE. Exits scoring 0.7-0.9
on the oracle both completed and got "Error verifying reCAPTCHA"; 14 POSTed
attempts, 8 completed, 6 rejected. So the oracle stays a MINIMUM BAR (the
score gate still needs >= threshold), and the target's own record RANKS the
candidates that clear it.

Nothing here is target-specific: everything is keyed by the form URL's
registrable host (`registrable_host`).

RECORD. After every /form-submit attempt that POSTed (form_submissions >= 1),
app.py records {host, asn, ip, verdict, ts}:

- `accepted`         — the run's `ok` (a 2xx/3xx landing on success_url, or
                       a wizard's completion: URL or completion marker);
- `captcha_rejected` — form_retry.is_captcha_rejection (the explicit step-0
                       CAPTCHA refusal, the same detection the retry uses);
- `other`            — anything else that POSTed (a field error, the
                       business-email gate, a later-step rejection, an
                       unknown outcome). Kept, but never counted for or
                       against an ASN: it is not the exit's verdict.

A zero-POST run is no verdict and is never recorded.

RULE (per host, per ASN; `decisive = accepted + captcha_rejected`,
`rate = accepted / decisive`):

- SKIP  when decisive >= MIN_VERDICTS (3) and rate <= BAD_RATE (0.20) — a
        sustained refusal record; one bad night (1-2 refusals) bans nothing.
- RANK  the rest by the smoothed rate (accepted + 1) / (decisive + 2): an
        unknown ASN sits at 0.5, a good record above it, a poor one below.
- PREFERRED when decisive >= MIN_VERDICTS and rate >= GOOD_RATE (0.6): the
        gate stops looking for a better candidate and probes it.
- EXPLORE with probability EXPLORE_RATE (0.1): a skipped ASN is let through
        anyway (ranked last), and a pick takes the least-tried candidate
        instead of the best — so a record can recover and new ASNs still get
        tried.

STORE: <FORM_PROFILE_DIR>/target-verdicts.json next to exit-blocklist.json
(same volume, same atomic tmp+rename write). Bounded: the last
MAX_EVENTS_PER_HOST verdicts per host (stats are computed from them, so the
record also follows a host that changes its mind), and MAX_HOSTS hosts (the
least recently updated is dropped). Never holds field values, tokens or page
text — host, ASN, exit IP, a verdict word and a timestamp.

Stdlib only (the tests import it without camoufox/fastapi).
"""
from __future__ import annotations

import ipaddress
import os
import random
import threading
import time
from urllib.parse import urlsplit

import form_retry
import profile_store

VERDICTS_FILE = "target-verdicts.json"
MAX_HOSTS = 50
MAX_EVENTS_PER_HOST = 100
MIN_VERDICTS = 3
BAD_RATE = 0.20
GOOD_RATE = 0.60
EXPLORE_RATE = 0.10
VERDICTS = ("accepted", "captcha_rejected", "other")

_lock = threading.Lock()
_rng = random.Random()

# Second-level labels under a 2-letter ccTLD that are themselves public
# suffixes (co.uk, com.au, gov.it...). Not the full Public Suffix List — a
# stdlib approximation that is right for the common shapes; at worst two
# subdomains of an exotic suffix share one record.
_SECOND_LEVEL = frozenset(("ac", "co", "com", "edu", "gov", "net", "org", "ne", "or", "go", "gob", "mil"))


def registrable_host(url_or_host) -> str | None:
    """The registrable domain of a URL or hostname ('www.atoka.io' ->
    'atoka.io', 'a.b.co.uk' -> 'b.co.uk'); an IP literal stays itself."""
    text = (url_or_host or "").strip()
    if not text:
        return None
    host = urlsplit(text).hostname if "//" in text else text.split("/", 1)[0].split(":", 1)[0]
    host = (host or "").strip(".").lower()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def classify(data) -> str | None:
    """The target's verdict on one attempt, or None when nothing was POSTed."""
    data = data or {}
    if not data.get("form_submissions"):
        return None
    if data.get("ok"):
        return "accepted"
    if form_retry.is_captcha_rejection(data):
        return "captcha_rejected"
    return "other"


def _exit_of(data, gate_record=None):
    """(asn, ip) the attempt POSTed from: the form's own egress check first,
    then what the gate chose."""
    diagnostics = (data or {}).get("diagnostics") or {}
    egress = diagnostics.get("egress") if isinstance(diagnostics.get("egress"), dict) else {}
    gate_record = gate_record or {}
    asn = egress.get("asn") if egress.get("asn") is not None else gate_record.get("asn")
    ip = egress.get("ip") or diagnostics.get("form_ip") or gate_record.get("gate_ip")
    return asn, ip


# ── store ───────────────────────────────────────────────────────────

def _path() -> str:
    return os.path.join(profile_store.root(), VERDICTS_FILE)


def load() -> dict:
    data = profile_store._read_json(_path()) or {}
    hosts = data.get("hosts") if isinstance(data.get("hosts"), dict) else {}
    return {"hosts": {h: v for h, v in hosts.items() if isinstance(v, dict)}}


def record(host, asn, ip, verdict, now=None) -> dict | None:
    """Append one verdict for `host` (bounded, atomic). Returns the event."""
    host = registrable_host(host)
    if not host or verdict not in VERDICTS:
        return None
    event = {"asn": str(asn) if asn is not None else None, "ip": ip or None,
             "verdict": verdict, "ts": int(now if now is not None else time.time())}
    with _lock:
        data = load()
        entry = data["hosts"].get(host) or {}
        events = [e for e in entry.get("events") or [] if isinstance(e, dict)]
        events.append(event)
        data["hosts"][host] = {"events": events[-MAX_EVENTS_PER_HOST:], "at": event["ts"]}
        if len(data["hosts"]) > MAX_HOSTS:
            by_age = sorted(data["hosts"], key=lambda h: data["hosts"][h].get("at", 0))
            for stale in by_age[:len(data["hosts"]) - MAX_HOSTS]:
                data["hosts"].pop(stale, None)
        profile_store._write_json(_path(), data)
    return event


def record_attempt(url, data, gate_record=None, now=None):
    """The hook app.py calls after every attempt: records it when it POSTed.
    Never raises (a full disk must not turn an answered form into a 502)."""
    try:
        verdict = classify(data)
        if verdict is None:
            return None
        asn, ip = _exit_of(data, gate_record)
        return record(url, asn, ip, verdict, now=now)
    except Exception:
        return None


# ── stats and the rule ──────────────────────────────────────────────

def host_stats(host, data=None) -> dict:
    """{asn: {accepted, rejected, other, rate}} for one host (rate = accepted
    / (accepted + rejected), None with no decisive verdict)."""
    host = registrable_host(host)
    data = data if data is not None else load()
    out: dict = {}
    for event in (data["hosts"].get(host) or {}).get("events") or []:
        key = event.get("asn")
        if key is None:
            key = "unknown"
        stats = out.setdefault(key, {"accepted": 0, "rejected": 0, "other": 0, "rate": None})
        verdict = event.get("verdict")
        if verdict == "accepted":
            stats["accepted"] += 1
        elif verdict == "captcha_rejected":
            stats["rejected"] += 1
        else:
            stats["other"] += 1
    for stats in out.values():
        decisive = stats["accepted"] + stats["rejected"]
        stats["rate"] = round(stats["accepted"] / decisive, 3) if decisive else None
    return out


def all_stats(data=None) -> dict:
    """{host: {asn: {accepted, rejected, other, rate}}} — the /form-exits view."""
    data = data if data is not None else load()
    return {host: host_stats(host, data) for host in sorted(data["hosts"])}


def assess(host, asn, *, stats=None, rng=None) -> dict:
    """One candidate exit judged by the target's record for its ASN.

    {skip: reason|None, rank: float, preferred: bool, explored: bool,
     decisive: int, rate: float|None}
    """
    rng = rng or _rng
    stats = stats if stats is not None else host_stats(host)
    own = stats.get(str(asn)) if asn is not None else None
    accepted = (own or {}).get("accepted", 0)
    decisive = accepted + (own or {}).get("rejected", 0)
    rate = accepted / decisive if decisive else None
    out = {"skip": None, "rank": (accepted + 1) / (decisive + 2), "preferred": False,
           "explored": False, "decisive": decisive, "rate": rate}
    if decisive >= MIN_VERDICTS and rate <= BAD_RATE:
        if rng.random() < EXPLORE_RATE:
            out.update(explored=True, rank=-1.0)  # let through, ranked last
        else:
            out["skip"] = "target_asn_rejected"
    elif decisive >= MIN_VERDICTS and rate >= GOOD_RATE:
        out["preferred"] = True
    return out


def pick(candidates, *, rng=None) -> int:
    """Index of the candidate to probe next among assessed ones (each has an
    `assessment`): the best rank, or — with probability EXPLORE_RATE — the
    least-tried one (ties keep arrival order)."""
    rng = rng or _rng
    if not candidates:
        raise ValueError("no candidates")
    order = range(len(candidates))
    if len(candidates) > 1 and rng.random() < EXPLORE_RATE:
        return min(order, key=lambda i: candidates[i]["assessment"]["decisive"])
    return max(order, key=lambda i: (candidates[i]["assessment"]["rank"], -i))

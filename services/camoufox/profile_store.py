"""Form profiles on disk: launch options, the pinned exit, and exit quality.

Stdlib only (the tests import it without camoufox/fastapi).

LAYOUT under FORM_PROFILE_DIR (default <tmp>/form-profiles — WIPED ON EVERY
REDEPLOY; production must mount a Railway volume and point this at it):

    <root>/<profile>/fingerprint.json   camoufox launch options (the fingerprint,
                                        drawn once) + a reserved "_web_tools"
                                        key: the profile's pinned exit
    <root>/<profile>/...                the Firefox profile (cookies, storage)
    <root>/exit-blocklist.json          oracle scores per exit IP / ASN

The "_web_tools" key is stripped before the options reach Camoufox: an
unknown launch kwarg would break the launch, and the meta must survive the
first launch writing the options (the exit is pinned BEFORE that launch).

STICKY EXIT. reCAPTCHA v3 scores (fingerprint, cookies, IP) together; a warm
profile that shows up from a new IP every run throws half its warmth away.
With sticky exits on, a profile keeps the Evomi session token it was first
used with (`_session-<token>` = same exit IP while the provider holds it).
Caveat measured nowhere yet: a provider may recycle a token's IP after its
sticky lifetime — `exit_ip` records the IP last SEEN on the token so a change
is visible (the probe reports `exit_ip_changed`).
"""
from __future__ import annotations

import json
import os
import tempfile
import time

META_KEY = "_web_tools"
BLOCKLIST_FILE = "exit-blocklist.json"
# A residential IP's verdict is not forever: the household behind it changes,
# and so does Google's view of it. IP entries expire; ASN stats are a slow
# aggregate and only block on a sustained record.
IP_TTL_S = 24 * 3600
ASN_MIN_SAMPLES = 5
ASN_BAD_RATIO = 0.8
MAX_IPS = 500


def root() -> str:
    return os.environ.get("FORM_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), "form-profiles")


def profile_dir(profile: str) -> str:
    return os.path.join(root(), profile)


def is_persistent_root() -> bool:
    """False when profiles live in the image's temp dir (lost on redeploy)."""
    return bool(os.environ.get("FORM_PROFILE_DIR"))


def sticky_default() -> bool:
    """FORM_PROFILE_STICKY_EXIT=1 makes profiles keep their exit by default."""
    return os.environ.get("FORM_PROFILE_STICKY_EXIT", "").strip().lower() in ("1", "true", "yes", "on")


def _fingerprint_path(directory: str) -> str:
    return os.path.join(directory, "fingerprint.json")


def _read_json(path: str):
    try:
        with open(path) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> None:
    """Atomic: a crash mid-write must not leave a half file the next launch
    would read as 'no fingerprint' and silently redraw."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


# ── launch options ──────────────────────────────────────────────────

def load_launch_opts(directory: str):
    """The persisted camoufox options without our meta, or None when the
    profile has none yet — or when they were drawn for a browser build this
    image no longer has (a Camoufox upgrade: the saved executable_path points
    into the old install, and a fingerprint drawn for the old engine version
    would be incoherent on the new one). The caller then redraws; cookies
    (the profile directory itself) are kept."""
    data = _read_json(_fingerprint_path(directory))
    if not data:
        return None
    opts = {k: v for k, v in data.items() if k != META_KEY}
    if not opts:
        return None
    executable = opts.get("executable_path")
    if executable and not os.path.exists(executable):
        return None
    return opts


def save_launch_opts(directory: str, opts: dict) -> None:
    """Persist options, preserving any meta already on disk."""
    current = _read_json(_fingerprint_path(directory)) or {}
    data = dict(opts)
    data.pop(META_KEY, None)
    if META_KEY in current:
        data[META_KEY] = current[META_KEY]
    _write_json(_fingerprint_path(directory), data)


# ── pinned exit ─────────────────────────────────────────────────────

def meta(profile: str) -> dict:
    data = _read_json(_fingerprint_path(profile_dir(profile))) or {}
    value = data.get(META_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _update_meta(profile: str, **fields) -> dict:
    path = _fingerprint_path(profile_dir(profile))
    data = _read_json(path) or {}
    current = data.get(META_KEY) if isinstance(data.get(META_KEY), dict) else {}
    current = {**current, **{k: v for k, v in fields.items() if v is not None}}
    data[META_KEY] = current
    _write_json(path, data)
    return current


def stored_exit(profile: str):
    value = meta(profile).get("exit_session")
    return value if isinstance(value, str) and value else None


def remember_exit(profile: str, session: str, *, ip=None, score=None) -> dict:
    return _update_meta(profile, exit_session=session, exit_pinned_at=int(time.time()),
                        exit_ip=ip, exit_score=score)


def note_exit_seen(profile: str, *, ip=None, score=None) -> bool:
    """Record what the pinned exit looked like on this run. True when the
    token now lands on a different IP than last seen."""
    previous = meta(profile).get("exit_ip")
    _update_meta(profile, exit_ip=ip, exit_score=score, exit_seen_at=int(time.time()))
    return bool(previous and ip and previous != ip)


def forget_exit(profile: str) -> None:
    path = _fingerprint_path(profile_dir(profile))
    data = _read_json(path)
    if not data or not isinstance(data.get(META_KEY), dict):
        return
    for key in ("exit_session", "exit_pinned_at", "exit_ip", "exit_score", "exit_seen_at"):
        data[META_KEY].pop(key, None)
    _write_json(path, data)


def resolve_exit_session(*, profile, exit_session, fresh_ip, sticky, shared, new_token):
    """Which exit token a form/probe runs on, and whether to pin it.

    Returns (session, pin): pin=True means "remember this token on the
    profile". Precedence: an explicit exit_session (caller override; pinned
    when the profile is sticky), then a sticky profile's stored exit, then a
    new token pinned to a sticky profile, then the old behaviour (fresh token
    per run when fresh_ip, else the replica's shared one).
    """
    if exit_session:
        return exit_session, bool(profile and sticky)
    if profile and sticky:
        stored = stored_exit(profile)
        if stored:
            return stored, False
        return new_token(), True
    return (new_token() if fresh_ip else shared), False


def pinned_exits() -> dict:
    """{profile: meta} for every profile under the root that has a pinned exit."""
    out = {}
    try:
        names = sorted(os.listdir(root()))
    except OSError:
        return out
    for name in names:
        if os.path.isdir(os.path.join(root(), name)):
            value = meta(name)
            if value.get("exit_session"):
                # The token is an exit selector, not a credential; still, the
                # listing only says THAT a profile is pinned and where it landed.
                out[name] = {k: value.get(k) for k in ("exit_ip", "exit_score", "exit_pinned_at", "exit_seen_at")}
    return out


# ── exit quality / blocklist ────────────────────────────────────────

def _blocklist_path() -> str:
    return os.path.join(root(), BLOCKLIST_FILE)


def load_blocklist() -> dict:
    data = _read_json(_blocklist_path()) or {}
    return {"ips": dict(data.get("ips") or {}), "asns": dict(data.get("asns") or {})}


def record_exit_score(ip, asn, score, threshold, now=None) -> dict:
    """Fold one oracle verdict into the per-IP and per-ASN record."""
    now = int(now if now is not None else time.time())
    data = load_blocklist()
    low = score is None or score < threshold
    if ip:
        entry = data["ips"].get(ip) or {"n": 0, "low": 0}
        entry.update(n=entry["n"] + 1, low=entry["low"] + int(low), last=score, at=now, asn=asn)
        data["ips"][ip] = entry
        if len(data["ips"]) > MAX_IPS:
            for stale in sorted(data["ips"], key=lambda k: data["ips"][k].get("at", 0))[:len(data["ips"]) - MAX_IPS]:
                data["ips"].pop(stale, None)
    if asn is not None:
        key = str(asn)
        entry = data["asns"].get(key) or {"n": 0, "low": 0, "sum": 0.0}
        entry.update(n=entry["n"] + 1, low=entry["low"] + int(low),
                     sum=round(entry["sum"] + (score or 0.0), 3), at=now)
        data["asns"][key] = entry
    _write_json(_blocklist_path(), data)
    return data


def blocked_reason(ip, asn, threshold=0.7, now=None, data=None):
    """Why this exit should not be used, or None.

    An IP is blocked while its LAST verdict is under the threshold and fresh
    (IP_TTL_S). An ASN is blocked only on a sustained record: at least
    ASN_MIN_SAMPLES verdicts with ASN_BAD_RATIO of them low — one ASN carries
    a whole carrier's households, and one bad night must not ban it.
    """
    now = now if now is not None else time.time()
    data = data if data is not None else load_blocklist()
    entry = data["ips"].get(ip) if ip else None
    if entry and now - entry.get("at", 0) < IP_TTL_S:
        last = entry.get("last")
        if last is None or last < threshold:
            return "ip_low_score"
    if asn is not None:
        stats = data["asns"].get(str(asn))
        if stats and stats.get("n", 0) >= ASN_MIN_SAMPLES and stats["low"] / stats["n"] >= ASN_BAD_RATIO:
            return "asn_low_scores"
    return None

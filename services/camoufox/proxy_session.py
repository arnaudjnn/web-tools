"""Evomi sticky-session tokens: one place that mints them and one that writes
them into the proxy password.

Evomi reads its options from the PASSWORD (docs.evomi.com, residential
"Proxy Sessions", read 2026-10-03):

- `_session-<id>`: the id is "a unique alphanumeric string, 6 to 10
  characters long". Default lifetime 30 min; it "may change IP address in
  favor of stability".
- `_lifetime-<minutes>`: session only; max 1440 (higher = HTTP 412).
- `_hardsession-<id>`: "keeps the same IP for as long as possible"; takes no
  lifetime.

Until 2026-10-03 every token here was `secrets.token_hex(6)` — TWELVE
characters, outside that spec — with no explicit lifetime, and the Atoka run
of 2026-10-03 00:48 had its gate choose an exit on ASN 16232 while the form,
launched seconds later on the same token, egressed from ASN 3269. New tokens
are 10 alphanumerics; any other caller-supplied token (legacy 12-hex pins in
profiles, an exit_session echoed back from an older response, or anything
carrying `_`/`-` that would otherwise be spliced into the password as extra
proxy options) maps deterministically onto a valid id, so "same token, same
session" still holds for it.

The form does not trust the token alone either: a gate-chosen exit is
re-verified by IP before the form touches the target (form_flow, `expect_ip`).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets

_VALID_ID = re.compile(r"^[A-Za-z0-9]{6,10}$")
KINDS = ("session", "hardsession")
DEFAULT_LIFETIME_MIN = 60  # gate + a 540 s wizard + retries fit with room
MIN_LIFETIME_MIN = 10
MAX_LIFETIME_MIN = 1440    # Evomi: above it the proxy answers 412


def new_token() -> str:
    """A fresh exit token, already a valid Evomi session id (10 chars)."""
    return secrets.token_hex(5)


def session_id(token: str) -> str:
    """The Evomi session id for a token: itself when valid, else a stable
    10-char digest of it (never the raw token: it may carry option syntax)."""
    token = str(token)
    if _VALID_ID.match(token):
        return token
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


def kind() -> str:
    value = os.environ.get("PROXY_SESSION_KIND", "session").strip().lower()
    return value if value in KINDS else "session"


def lifetime_min() -> int:
    try:
        value = int(os.environ.get("PROXY_SESSION_LIFETIME_MIN", DEFAULT_LIFETIME_MIN))
    except ValueError:
        value = DEFAULT_LIFETIME_MIN
    return max(MIN_LIFETIME_MIN, min(MAX_LIFETIME_MIN, value))


def options(token: str) -> str:
    """The password suffix pinning `token`'s exit, e.g.
    `_session-1a2b3c4d5e_lifetime-60` (or `_hardsession-1a2b3c4d5e`)."""
    sid = session_id(token)
    if kind() == "hardsession":
        return f"_hardsession-{sid}"
    return f"_session-{sid}_lifetime-{lifetime_min()}"


def has_session(password: str) -> bool:
    """The provider URL already pins a session itself (left alone)."""
    return bool(re.search(r"_(?:hard|locked)?session-", password))

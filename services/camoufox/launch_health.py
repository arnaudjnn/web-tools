"""Shed a replica whose browsers can no longer launch.

A launch failure is not always about the request. Measured 2026-09-26 and
again 2026-09-29: when a host takes user namespaces away (or otherwise
degrades), EVERY launch fails in EVERY mode — forms, render and spa-fetch
together — with CanCreateUserNamespace() EACCES then SIGSEGV or
TargetClosed, and nothing inside the container can repair it; only a
restart moves the process to a host that still works (the Dockerfile's
MOZ_DISABLE_CONTENT_SANDBOX note carries the first measurement, the
b5a2bcc /healthz note the second: ~50-70% errors across replicas, cured
by a redeploy).

So every browser launch anywhere in this service reports its outcome
here. LAUNCH_FAIL_STREAK consecutive failures schedule ONE exit after
LAUNCH_FAIL_SHED_S seconds (the grace lets the response that proved the
streak flush first); a launch that succeeds in between retracts the
pending exit, and the timer re-checks the streak when it fires. Railway
restarts the exited container — the same shed contract as
FORM_WEDGE_EXIT_S in form_worker. LAUNCH_FAIL_STREAK defaults to 0 =
never, so a test suite or a local run can never be killed by this; the
Dockerfile sets the production values.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("camoufox.launch")

_lock = threading.Lock()
_streak = 0
_timer: threading.Timer | None = None


def _threshold() -> int:
    """Consecutive failures that trigger a shed; 0 (or garbage) = never."""
    try:
        return int(os.getenv("LAUNCH_FAIL_STREAK", "0"))
    except ValueError:
        return 0


def _shed_delay_s() -> float:
    try:
        return float(os.getenv("LAUNCH_FAIL_SHED_S", "10"))
    except ValueError:
        return 10.0


def note(ok: bool) -> None:
    """Record the outcome of ONE browser launch attempt, from any path."""
    global _streak, _timer
    with _lock:
        if ok:
            _streak = 0
            if _timer is not None:
                _timer.cancel()
                _timer = None
            return
        _streak += 1
        threshold = _threshold()
        if threshold <= 0 or _streak < threshold or _timer is not None:
            return
        delay = _shed_delay_s()
        if delay <= 0:
            return
        log.warning("browser launch failed %d time(s) in a row — shedding in %.1fs "
                    "(LAUNCH_FAIL_STREAK/LAUNCH_FAIL_SHED_S)", _streak, delay)
        timer = threading.Timer(delay, _shed_if_still_broken)
        timer.daemon = True
        timer.start()
        _timer = timer


def _shed_if_still_broken() -> None:
    """Fire only if nothing launched in the grace window."""
    global _streak, _timer
    with _lock:
        timer, _timer = _timer, None
        if timer is not None:
            timer.cancel()
        threshold = _threshold()
        if threshold <= 0 or _streak < threshold:
            log.info("browser launch recovered — shed retracted")
            return
        log.warning("browser launches still failing (%d) — exiting for a restart", _streak)
        os._exit(1)

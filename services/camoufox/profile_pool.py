"""A per-replica pool of warmed browser profiles for score-gated forms.

reCAPTCHA v3 leans on the browser's Google cookies and history; every form
browser used to be a blank isolated profile — the riskiest kind — and four
of them at once looked like one device four times (2026-10-05: acceptance
fell to 50% at 4 in flight). FORM_PROFILE_POOL profiles per replica are
warmed in the background (google.com, youtube.com — score_probe.warm_flow)
and handed out least-recently-used, one attempt at a time; a retry takes a
different one. Each profile draws its own fingerprint once (fingerprint.py
diversify), so parallel sessions differ in OS and screen as real visitors do.

Profiles live under FORM_PROFILE_DIR (ephemeral per replica): a redeploy
starts a fresh pool, re-warmed on startup.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("camoufox.pool")

WARMED_MARK = ".warmed"


def size() -> int:
    try:
        return max(0, int(os.environ.get("FORM_PROFILE_POOL", "0")))
    except ValueError:
        return 0


def enabled() -> bool:
    return size() > 0


def names() -> list[str]:
    return ["pool%d" % i for i in range(size())]


_in_use: set[str] = set()
_last_used: dict[str, float] = {}


def _warmed(name: str, profile_dir) -> bool:
    return os.path.exists(os.path.join(profile_dir(name), WARMED_MARK))


def mark_warmed(name: str, profile_dir) -> None:
    path = profile_dir(name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, WARMED_MARK), "w") as handle:
        handle.write(str(int(time.time())))


def acquire(profile_dir) -> str | None:
    """The least-recently-used free profile, warmed ones first; None when all
    are busy (the caller then runs an isolated browser, as before)."""
    free = [n for n in names() if n not in _in_use]
    if not free:
        return None
    free.sort(key=lambda n: (not _warmed(n, profile_dir), _last_used.get(n, 0.0)))
    name = free[0]
    _in_use.add(name)
    return name


def release(name: str | None) -> None:
    if name:
        _in_use.discard(name)
        _last_used[name] = time.monotonic()


def pending(profile_dir) -> list[str]:
    return [n for n in names() if not _warmed(n, profile_dir)]


async def warm_all(warm_one, profile_dir) -> None:
    """Warm every unwarmed pool profile, one at a time (never fatal)."""
    for name in pending(profile_dir):
        if name in _in_use:
            continue
        _in_use.add(name)
        try:
            ok = await warm_one(name)
            if ok:
                mark_warmed(name, profile_dir)
                log.info("pool profile %s warmed", name)
            else:
                log.warning("pool profile %s warm-up incomplete; will be used cold", name)
        except Exception as error:  # noqa: BLE001 - a warm-up must never take the service down
            log.warning("pool profile %s warm-up failed (%s)", name, type(error).__name__)
        finally:
            _in_use.discard(name)


def reset() -> None:
    """Tests only."""
    _in_use.clear()
    _last_used.clear()

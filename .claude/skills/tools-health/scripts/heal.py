#!/usr/bin/env python3
"""Apply the bounded remediations that health.py asks for.

Every action here is one that has actually fixed this stack in production, and
nothing else is allowed. It will not delete a service, touch a domain, change a
variable or scale anything, because those are the operations that cannot be undone
by trying again.

Requires --apply to act; without it, prints the plan. Redeploys are rate-limited
per service so a flapping container cannot be restart-looped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

STATE = os.path.expanduser("~/.tools-health-state.json")
REDEPLOY_COOLDOWN_S = 900  # 15 min: long enough that a crashloop is not amplified
TOOLS_URL = "https://tools-production-d199.up.railway.app"


def sh(cmd: list[str], timeout: int = 300) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"__ERROR__ {e}"


def load_state() -> dict:
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s: dict) -> None:
    try:
        json.dump(s, open(STATE, "w"))
    except Exception:
        pass


def api_key() -> str | None:
    if os.environ.get("WEB_TOOLS_API_KEY"):
        return os.environ["WEB_TOOLS_API_KEY"]
    m = re.search(r"^API_KEY=(\S+)", sh(["railway", "variables", "-s", "Tools", "--kv"]), re.M)
    return m.group(1) if m else None


def redeploy(service: str, apply: bool) -> str:
    state = load_state()
    last = state.get(f"redeploy:{service}", 0)
    waited = time.time() - last
    if waited < REDEPLOY_COOLDOWN_S:
        return f"SKIP redeploy {service}: last one {int(waited)}s ago (cooldown {REDEPLOY_COOLDOWN_S}s)"
    if not apply:
        return f"WOULD redeploy {service}"
    out = sh(["railway", "redeploy", "-s", service, "-y"], timeout=600)
    state[f"redeploy:{service}"] = time.time()
    save_state(state)
    return f"redeployed {service}" + (" (cli reported an error)" if "__ERROR__" in out else "")


def recycle(apply: bool) -> str:
    """Drop Camoufox's warmed Akamai session and take a fresh exit IP.

    Costs whoever is mid-crawl their sensor maturation, so it is only worth doing
    when the sensor is stuck UNVALIDATED and every gated POST is 403ing anyway.
    """
    if not apply:
        return "WOULD call web_recycle (drops the warmed Akamai session)"
    key = api_key()
    if not key:
        return "SKIP recycle: no API key"
    try:
        req = urllib.request.Request(
            f"{TOOLS_URL}/api/v0/web_recycle", data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=180) as r:
            r.read()
        return "recycled camoufox sessions"
    except Exception as e:
        return f"recycle failed: {type(e).__name__}"


ACTIONS = {"redeploy", "recycle"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually act (default: print the plan)")
    ap.add_argument("--findings", help="path to health.py --json output; omit to read stdin")
    args = ap.parse_args()

    raw = open(args.findings).read() if args.findings else sys.stdin.read()
    try:
        findings = json.loads(raw).get("findings", [])
    except Exception:
        print("heal: could not parse findings json")
        return 2

    actionable = [f for f in findings if f.get("action") in ACTIONS]
    manual = [f for f in findings if f.get("action") == "manual"]

    # Report the manual ones ALWAYS, not only when there is nothing to automate.
    # They are the findings where retrying makes things worse, so burying them
    # behind a successful redeploy is the wrong way round.
    if manual:
        print("heal: these need a human and will not be attempted:")
        for f in manual:
            print(f"  - {f.get('service','-')}: {f['detail']}")
        if actionable:
            print()

    if not actionable:
        if not manual:
            print("heal: nothing to do")
        return 0

    # One redeploy per service per run, even if several findings ask for it.
    done: set[str] = set()
    for f in actionable:
        svc, action = f.get("service", ""), f["action"]
        if action == "redeploy":
            if svc in done:
                continue
            done.add(svc)
            print(" ", redeploy(svc, args.apply))
        elif action == "recycle":
            if "recycle" in done:
                continue
            done.add("recycle")
            print(" ", recycle(args.apply))

    if not args.apply:
        print("\n  (plan only; re-run with --apply to act)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

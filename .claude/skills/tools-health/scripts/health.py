#!/usr/bin/env python3
"""Diagnose the web-tools Railway stack: service state, log signatures, live probes.

Read-only. Prints findings and exits non-zero when something is wrong, so it can
be used as a gate. `heal.py` consumes the same findings to act.

The probes matter more than the logs here. Every degradation this stack has shown
in practice is invisible in the HTTP status: a fetch returns a plausible 200 from
the WRONG backend, or a search returns `[]` with no error at all. So the checks
assert on the `mode` field and the result count, not on liveness.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT = "3375ebc9-cb5f-42ac-999d-b1d3b8feb5ef"  # web-tools
TOOLS_URL = "https://tools-production-d199.up.railway.app"
SERVICES = ["Tools", "SearXNG", "Scrapling", "Camoufox"]

# signature -> what it means. Every one was observed in production; see
# references/signatures.md.
#
# These are reported as CONTEXT, never as actions, for two reasons. `railway logs`
# returns a window of history with no way to ask "only the last N minutes", so a
# signature from hours ago, already self-healed, still matches now. And the Tools
# service already evicts and retries on its own, so a dead-browser line is usually
# the record of a fix rather than a live fault.
#
# What triggers a remediation is a PROBE, which is live by construction. The log
# scan is here to explain a probe failure, not to cause an action.
LOG_SIGNATURES = [
    (r"can't start new thread", "thread budget exhausted (browser pool leak)"),
    (r"pthread_create: Resource temporarily unavailable", "thread budget exhausted"),
    (r"Target page, context or browser has been closed", "dead pooled browser seen"),
    (r"BrowserType\.launch: Target page", "wedged browser session seen"),
    (r"UNVALIDATED\(~-1~\)", "akamai sensor was unvalidated at some point"),
    (r"ERR_PNPM_OUTDATED_LOCKFILE", "manifest and lockfile disagree"),
    (r"ZodError.*API_KEY", "wrong program deployed (root Dockerfile)"),
    (r"Cloudflare page didn't disappear", "unsolvable Turnstile; host should stay out of SOLVE_HOSTS"),
]

# Signatures that are structural rather than transient: they do not self-heal and
# a human has to change something.
NEEDS_HUMAN = ("ERR_PNPM_OUTDATED_LOCKFILE", "ZodError.*API_KEY")


def sh(cmd: list[str], timeout: int = 120) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"__ERROR__ {e}"


def api_key() -> str | None:
    import os

    if os.environ.get("WEB_TOOLS_API_KEY"):
        return os.environ["WEB_TOOLS_API_KEY"]
    out = sh(["railway", "variables", "-s", "Tools", "--kv"])
    m = re.search(r"^API_KEY=(\S+)", out, re.M)
    return m.group(1) if m else None


def call_tool(tool: str, body: dict, key: str, timeout: int = 120):
    req = urllib.request.Request(
        f"{TOOLS_URL}/api/v0/{tool}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), round(time.time() - t0, 1)


def check_services(findings: list[dict]) -> None:
    out = sh(["railway", "status"])
    if "__ERROR__" in out:
        findings.append({"check": "status", "severity": "error", "detail": out.strip()[:200],
                         "action": "manual"})
        return
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        name = line[2:].split(":")[0].strip()
        if name not in SERVICES and "Redis" not in name:
            continue
        if "Deploy failed" in line:
            findings.append({"check": "status", "service": name, "severity": "error",
                             "detail": "latest deploy failed; a previous container is still serving",
                             "action": "manual"})
        elif "Online" not in line and "Completed" not in line:
            findings.append({"check": "status", "service": name, "severity": "error",
                             "detail": line[:120], "action": "redeploy"})


def check_logs(findings: list[dict]) -> None:
    for svc in SERVICES:
        out = sh(["railway", "logs", "-s", svc], timeout=180)
        if "__ERROR__" in out:
            continue
        for pattern, meaning in LOG_SIGNATURES:
            if not re.search(pattern, out):
                continue
            human = pattern in NEEDS_HUMAN
            findings.append({
                "check": "log", "service": svc,
                "severity": "error" if human else "info",
                "detail": meaning + ("" if human else " (historical; may already be resolved)"),
                "action": "manual" if human else "context",
            })


def check_probes(findings: list[dict], key: str) -> None:
    # web_search: the failure mode is [] with a 200, at ~15s (every engine timing out).
    try:
        res, secs = call_tool("web_search", {"query": "gorgias ecommerce", "limit": 3}, key, 90)
        n = len(res) if isinstance(res, list) else 0
        if n == 0:
            findings.append({"check": "probe", "service": "SearXNG", "severity": "error",
                             "detail": f"web_search returned 0 results in {secs}s",
                             "action": "redeploy"})
        elif secs > 8:
            findings.append({"check": "probe", "service": "SearXNG", "severity": "warn",
                             "detail": f"web_search slow ({secs}s, expect <2s)", "action": "watch"})
    except Exception as e:
        findings.append({"check": "probe", "service": "SearXNG", "severity": "error",
                         "detail": f"web_search failed: {type(e).__name__}", "action": "redeploy"})

    # web_html on LinkedIn: asserts the ENGINE, not the status. A non-stealth
    # mode here (camoufox, fast) means the stealth path is down and we are
    # silently fetching from the wrong exit.
    try:
        res, secs = call_tool("web_html", {"url": "https://www.linkedin.com/in/williamhgates"}, key, 150)
        env = json.loads(res["content"][0]["text"])
        mode, status = env.get("mode"), env.get("status")
        if mode != "stealth":
            findings.append({"check": "probe", "service": "Scrapling", "severity": "error",
                             "detail": f"linkedin served by mode={mode} (expected stealth) "
                                       f"status={status}: silent downgrade",
                             "action": "redeploy"})
        elif status != 200:
            findings.append({"check": "probe", "service": "Scrapling", "severity": "warn",
                             "detail": f"linkedin status={status} via stealth", "action": "watch"})
    except Exception as e:
        findings.append({"check": "probe", "service": "Scrapling", "severity": "error",
                         "detail": f"web_html failed: {type(e).__name__}", "action": "redeploy"})


def check_probes_deep(findings: list[dict], key: str) -> None:
    # An Italian host must be served by camoufox; anything else means that
    # sidecar is unreachable and we are fetching from the wrong country.
    try:
        res, secs = call_tool("web_html", {"url": "https://www.ivass.it/", "timeout_ms": 90000}, key, 150)
        env = json.loads(res["content"][0]["text"])
        if env.get("mode") != "camoufox":
            findings.append({"check": "probe", "service": "Camoufox", "severity": "error",
                             "detail": f"italian host served by mode={env.get('mode')} "
                                       f"(expected camoufox)", "action": "redeploy"})
    except Exception as e:
        findings.append({"check": "probe", "service": "Camoufox", "severity": "error",
                         "detail": f"camoufox probe failed: {type(e).__name__}", "action": "redeploy"})

    # Scrapling serves web_crawl (sequential, one post to /markdown per URL).
    try:
        res, secs = call_tool("web_crawl", {"urls": ["https://example.com"]}, key, 120)
        if res.get("isError"):
            findings.append({"check": "probe", "service": "Scrapling", "severity": "error",
                             "detail": "web_crawl returned an error", "action": "redeploy"})
        else:
            # The toolkit renders markdown locally when Scrapling's /markdown
            # cannot answer, so success alone no longer proves the sidecar is
            # rendering. `renderer` says who did.
            row = (json.loads(res["content"][0]["text"]).get("results") or [{}])[0]
            if row.get("renderer") != "scrapling":
                findings.append({"check": "probe", "service": "Scrapling", "severity": "error",
                                 "detail": f"web_crawl rendered by renderer={row.get('renderer')} "
                                           f"mode={row.get('mode')} (expected scrapling): "
                                           "/markdown is down, the local fallback served it",
                                 "action": "redeploy"})
    except Exception as e:
        findings.append({"check": "probe", "service": "Scrapling", "severity": "error",
                         "detail": f"web_crawl failed: {type(e).__name__}", "action": "redeploy"})


def check_sidecar_wedge(findings: list[dict]) -> None:
    """busy_age_s on the Scrapling /healthz: a request older than 150s is a wedge.

    The sidecar's hard deadline is timeout+20s slack and clients abort at
    +25s, so nothing legitimate is still busy at 150s — a leftover busy_age_s
    means a stuck executor. healthz is internal-only, so this needs a
    reachable URL: set SCRAPLING_HEALTHZ (e.g. a tcp-proxy URL while one is
    open). Skipped silently when unset.
    """
    import os

    url = os.environ.get("SCRAPLING_HEALTHZ")
    if not url:
        return
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            health = json.loads(r.read())
    except Exception as e:
        findings.append({"check": "healthz", "service": "Scrapling", "severity": "warn",
                         "detail": f"healthz unreachable ({type(e).__name__})", "action": "watch"})
        return
    for mode, age in (health.get("busy_age_s") or {}).items():
        if age > 150:
            findings.append({"check": "healthz", "service": "Scrapling", "severity": "error",
                             "detail": f"sidecar wedged: {mode} busy for {int(age)}s (deadline "
                                       f"should have fired)", "action": "redeploy"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=PROJECT, help="Railway project id (default: web-tools)")
    ap.add_argument("--fast", action="store_true",
                    help="status + two probes only; no log scan, no deep probes (for a SessionStart hook)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("tools-health: cannot read API_KEY (set WEB_TOOLS_API_KEY or `railway link`)")
        return 2

    findings: list[dict] = []
    check_services(findings)
    check_probes(findings, key)
    if not args.fast:
        check_logs(findings)
        check_probes_deep(findings, key)
        check_sidecar_wedge(findings)

    if args.json:
        print(json.dumps({"project": args.project, "findings": findings}, indent=1))
    else:
        errors = [f for f in findings if f["severity"] == "error"]
        warns = [f for f in findings if f["severity"] == "warn"]
        infos = [f for f in findings if f["severity"] == "info"]
        if not errors and not warns:
            print("tools-health: web-tools OK (services online, search and stealth path verified)"
                  + (f"; {len(infos)} historical log note(s) below" if infos else ""))
        else:
            print(f"tools-health: {len(errors)} error(s), {len(warns)} warning(s), "
                  f"{len(infos)} note(s)")
            for f in findings:
                svc = f.get("service", "-")
                print(f"  [{f['severity']}] {svc:10} {f['detail']}  -> {f['action']}")
    return 1 if any(f["severity"] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

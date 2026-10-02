#!/usr/bin/env python3
"""reCAPTCHA v3 score bench for the FORM browser, against OUR oracle only.

Runs N probes per configuration through the public Tools API
(web_form_score_probe / web_form_warm / web_form_exit_select — REST-only
diagnostics) and prints a score distribution table. Never targets a
third-party form: every token is minted on our own key's page
(/oracle/recaptcha on Tools) and verified by Tools.

    # G2 matrix, 20 runs each (~30-60 s per probe; the sidecar is serial):
    python3 score_bench.py --n 20
    python3 score_bench.py --n 20 --configs baseline,headless,warm-sticky
    # Camoufox version A/B is DEPLOY-level (one pinned browser per image):
    python3 score_bench.py --n 20 --label cf152 --out cf152.jsonl
    #   ...deploy the upgrade commit, then:
    python3 score_bench.py --n 20 --label cf156 --out cf156.jsonl
    python3 score_bench.py --report cf152.jsonl cf156.jsonl

Key: WEB_TOOLS_API_KEY, else `railway variables -s Tools` (same as health.py).
G2 target: the shipped configuration scores >= 0.7 on >= 95% of runs.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from health import TOOLS_URL, api_key  # noqa: E402

# name -> (description, setup, probe body factory(run_tag, i)).
# setup: None | ("warm", {...}) | ("select", {...}) — run once per bench run.
CONFIGS: dict[str, tuple] = {
    "baseline": ("isolated, fresh_ip, headed, wait 4s, 2 fields (forms default)", None,
                 lambda tag, i: {}),
    "headless": ("as baseline, headless", None,
                 lambda tag, i: {"headed": False}),
    "fresh-profile": ("new named profile per run, fresh_ip", None,
                      lambda tag, i: {"profile": f"bench-{tag}-fresh-{i}", "sticky_exit": False}),
    "warm-sticky": ("one profile warmed once (google, youtube, origin), its pinned exit every run",
                    ("warm", {"profile": "bench-{tag}-warm"}),
                    lambda tag, i: {"profile": f"bench-{tag}-warm", "sticky_exit": True}),
    "warm-fresh-ip": ("same warm profile cookies, new exit every run",
                      ("warm", {"profile": "bench-{tag}-warmf", "sticky_exit": False}),
                      lambda tag, i: {"profile": f"bench-{tag}-warmf", "sticky_exit": False}),
    "sticky-isolated": ("isolated browser, ONE exit token for all runs", None,
                        lambda tag, i: {"exit_session": f"b{tag}"[:24]}),
    "selected-sticky": ("profile pinned by web_form_exit_select, its exit every run",
                        ("select", {"profile": "bench-{tag}-sel"}),
                        lambda tag, i: {"profile": f"bench-{tag}-sel", "sticky_exit": True}),
    "dwell-0": ("as baseline, no dwell after load", None, lambda tag, i: {"wait_ms": 0}),
    "dwell-15s": ("as baseline, 15 s dwell after load", None, lambda tag, i: {"wait_ms": 15000}),
    "dwell-40s": ("as baseline, 40 s dwell after load", None, lambda tag, i: {"wait_ms": 40000}),
    # Google's challenge_ts measured ~= page load, not execute(): does a long
    # dwell (Atoka's step0 fill is 60-140 s) age the token toward expiry?
    "dwell-60s": ("as baseline, 60 s dwell after load (token-age test)", None,
                  lambda tag, i: {"wait_ms": 60000, "timeout_ms": 180000}),
    "typing-1": ("as baseline, 1 field typed", None, lambda tag, i: {"field_count": 1}),
    "typing-3": ("as baseline, 3 fields typed", None, lambda tag, i: {"field_count": 3}),
}
DEFAULT_CONFIGS = ["baseline", "headless", "fresh-profile", "warm-sticky", "warm-fresh-ip",
                   "sticky-isolated", "dwell-0", "dwell-40s"]


# Server-side budget per tool (score_probe.py request models' timeout_ms
# defaults), used when the body does not set its own.
TOOL_BUDGET_S = {"web_form_score_probe": 120, "web_form_warm": 150, "web_form_exit_select": 270}
# On top of that budget: Tools -> Camoufox transit, the form worker's
# TEARDOWN_GRACE_S (45 s: a wedged job answers only after deadline + grace)
# and slack. Past budget + margin the answer is not coming.
MARGIN_S = 75
# One call may wait out sidecar restarts for at most this long in total.
AWAY_BUDGET_S = 900
AWAY_PAUSE_S = 75


def budget_s(tool: str, body: dict) -> float:
    """The hard wall-clock bound for one HTTP call of `tool` with `body`."""
    timeout_ms = body.get("timeout_ms")
    base = timeout_ms / 1000 if isinstance(timeout_ms, (int, float)) and timeout_ms > 0 \
        else TOOL_BUDGET_S.get(tool, 120)
    return base + MARGIN_S


def call(tool: str, body: dict, key: str, timeout: float | None = None):
    """POST one tool call with a HARD wall-clock bound.

    urllib's `timeout` bounds each socket operation, not the request: DNS
    resolution is not covered at all, and a peer that keeps the connection
    open (or trickles bytes) can hold one call indefinitely — a 2026-10-02
    bench sat >2 h on a single request. So the request runs on a daemon
    thread joined with the bound; past it the call is abandoned and reported
    as a TimeoutError (the bench moves on; the thread dies with the process).
    """
    total = float(timeout if timeout is not None else budget_s(tool, body))
    req = urllib.request.Request(
        f"{TOOLS_URL}/api/v0/{tool}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    started = time.time()
    box: dict = {}

    def work():
        try:
            with urllib.request.urlopen(req, timeout=total) as r:
                box["raw"] = r.read()
        except urllib.error.HTTPError as e:
            box["error"] = f"HTTP {e.code}"
        except Exception as e:  # network, socket timeout
            box["error"] = type(e).__name__

    worker = threading.Thread(target=work, name=f"bench-{tool}", daemon=True)
    worker.start()
    worker.join(total)
    secs = round(time.time() - started, 1)
    if worker.is_alive():
        return {"error": "TimeoutError", "hard_timeout_s": total}, secs
    if "error" in box:
        return {"error": box["error"]}, secs
    try:
        outer = json.loads(box.get("raw") or b"{}")
    except ValueError:
        return {"error": "unparseable"}, secs
    text = ((outer.get("content") or [{}])[0] or {}).get("text", "")
    try:
        return json.loads(text), secs
    except ValueError:
        return {"error": text[:200] or "unparseable"}, secs


# The sidecar being away (a park or launch-shed restarts it for ~60 s, and
# Tools' breaker then refuses for 60 s) is not a sample: wait it out — but
# only for AWAY_BUDGET_S in total per call, never open-ended.
AWAY = ("unreachable", "breaker open", "HTTP 502", "HTTP 503", "HTTP 504",
        "URLError", "TimeoutError", "ConnectionResetError", "RemoteDisconnected")


def call_when_up(tool: str, body: dict, key: str, waits: int = 8,
                 away_budget_s: float | None = None, pause_s: float | None = None,
                 _sleep=time.sleep, _clock=time.monotonic):
    budget = AWAY_BUDGET_S if away_budget_s is None else away_budget_s
    pause = AWAY_PAUSE_S if pause_s is None else pause_s
    give_up = _clock() + budget
    result, secs, attempt = {"error": "not_attempted"}, 0.0, 0
    for attempt in range(waits + 1):
        result, secs = call(tool, body, key)
        err = str(result.get("error") or "")
        left = give_up - _clock()
        if not any(a in err for a in AWAY) or attempt == waits or left <= 0:
            return result, secs, attempt
        _sleep(min(pause, left))
    return result, secs, attempt


def setup(kind: str, body: dict, tag: str, key: str) -> dict:
    body = {k: (v.format(tag=tag) if isinstance(v, str) else v) for k, v in body.items()}
    if kind == "warm":
        out, secs, _ = call_when_up("web_form_warm", body, key)
        print(f"  setup warm {body['profile']}: {secs}s visits="
              f"{[(v.get('host'), v.get('ok')) for v in out.get('visits') or []]} error={out.get('error')}")
    else:
        out, secs, _ = call_when_up("web_form_exit_select", body, key)
        print(f"  setup exit-select {body['profile']}: {secs}s pinned={out.get('pinned')} "
              f"tries={[t.get('score', t.get('skipped')) for t in out.get('tries') or []]}")
    return out


def run(configs, n, tag, label, out_path, key, threshold):
    sink = open(out_path, "a") if out_path else None
    rows = []
    for name in configs:
        desc, prep, factory = CONFIGS[name]
        print(f"[{name}] {desc} — {n} runs")
        if prep:
            setup(prep[0], dict(prep[1]), tag, key)
        for i in range(n):
            body = {"threshold": threshold, **factory(tag, i)}
            result, secs, waited = call_when_up("web_form_score_probe", body, key)
            row = {"label": label, "config": name, "i": i, "secs": secs, "at": int(time.time()),
                   "score": result.get("score"), "passed": result.get("passed"),
                   "error": result.get("error") or (result.get("form") or {}).get("error"),
                   "codes": result.get("error-codes"), "egress": result.get("egress"),
                   "blocked": result.get("blocked"), "exit_ip_changed": result.get("exit_ip_changed"),
                   "waited_restarts": waited,
                   "dwell_s": (result.get("verdict") or {}).get("page_dwell_s"),
                   "mint_s": (result.get("verdict") or {}).get("mint_s"),
                   "mint_error": (result.get("verdict") or {}).get("mint_error"),
                   "token_age_s": (result.get("verdict") or {}).get("token_age_s"),
                   "subs": (result.get("form") or {}).get("form_submissions"),
                   # The pre-input reCAPTCHA gate (form_flow): usable?, by
                   # which signal, reloaded once?, which pieces failed.
                   "captcha_ready": (result.get("form") or {}).get("captcha_ready"),
                   "captcha_signal": (result.get("form") or {}).get("captcha_signal"),
                   "captcha_reload": (result.get("form") or {}).get("captcha_reload"),
                   "captcha_failed": (result.get("form") or {}).get("captcha_failed"),
                   "token": (result.get("form") or {}).get("token_present"),
                   # POSTed but no verdict read back: Google may still have
                   # scored it (Tools logs `oracle verdict`) — a probe miss,
                   # not a score.
                   "capture_miss": bool((result.get("form") or {}).get("form_submissions")
                                        and result.get("verdict") is None)}
            rows.append(row)
            if sink:
                sink.write(json.dumps(row) + "\n")
                sink.flush()
            if row["error"] == "probe_unavailable":
                time.sleep(AWAY_PAUSE_S)  # the park shed the replica; let it come back
            eg = row["egress"] or {}
            print(f"  {i + 1:>3}/{n} score={row['score']} {secs:>5}s ip={eg.get('ip')} "
                  f"asn={eg.get('asn')} err={row['error']}")
    if sink:
        sink.close()
    report(rows, threshold)


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]


def report(rows, threshold=0.7):
    groups = defaultdict(list)
    for row in rows:
        groups[(row.get("label") or "", row["config"])].append(row)
    head = (f"{'label':<8} {'config':<16} {'n':>3} {'scored':>6} {'mean':>5} {'p10':>4} {'med':>4} "
            f"{'min':>4} {'>=' + str(threshold):>6} {'>=0.5':>6} {'err':>4} {'IPs':>4} {'ASNs':>4}  histogram")
    print("\n" + head + "\n" + "-" * len(head))
    for (label, config), items in sorted(groups.items()):
        scores = [r["score"] for r in items if isinstance(r.get("score"), (int, float))]
        n = len(items)
        ips = {(r.get("egress") or {}).get("ip") for r in items} - {None}
        asns = {(r.get("egress") or {}).get("asn") for r in items} - {None}
        hist = defaultdict(int)
        for s in scores:
            hist[round(s, 1)] += 1
        # The rate is over ALL runs: a run with no verdict is a failed run.
        at_t = sum(1 for s in scores if s >= threshold) / n if n else 0
        at_5 = sum(1 for s in scores if s >= 0.5) / n if n else 0
        fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
        print(f"{label:<8} {config:<16} {n:>3} {len(scores):>6} "
              f"{fmt(statistics.mean(scores) if scores else None):>5} {fmt(pct(scores, 0.1)):>4} "
              f"{fmt(statistics.median(scores) if scores else None):>4} {fmt(min(scores) if scores else None):>4} "
              f"{at_t:>6.0%} {at_5:>6.0%} {n - len(scores):>4} {len(ips):>4} {len(asns):>4}  "
              + " ".join(f"{k:.1f}:{v}" for k, v in sorted(hist.items())))
    print(f"\nG2: ship a configuration with >={threshold} on >=95% of runs (column '>={threshold}').")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20, help="probes per configuration (default 20)")
    ap.add_argument("--configs", default=",".join(DEFAULT_CONFIGS),
                    help=f"comma list from: {', '.join(CONFIGS)}")
    ap.add_argument("--tag", default=time.strftime("%m%d%H%M"), help="profile-name suffix (fresh profiles per bench)")
    ap.add_argument("--label", default="", help="free label, e.g. the deployed camoufox version")
    ap.add_argument("--out", help="append raw rows as JSONL")
    ap.add_argument("--threshold", type=float, default=0.7)
    ap.add_argument("--away-budget", type=float, default=AWAY_BUDGET_S,
                    help=f"max seconds one call waits out sidecar restarts (default {AWAY_BUDGET_S})")
    ap.add_argument("--report", nargs="+", metavar="JSONL", help="only re-print the table from saved rows")
    ap.add_argument("--list", action="store_true", help="list configurations")
    args = ap.parse_args()
    if args.list:
        for name, (desc, prep, _) in CONFIGS.items():
            print(f"{name:<16} {desc}" + (f"  [setup: {prep[0]}]" if prep else ""))
        return
    if args.report:
        rows = []
        for path in args.report:
            with open(path) as handle:
                rows += [json.loads(line) for line in handle if line.strip()]
        report(rows, args.threshold)
        return
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown:
        sys.exit(f"unknown configs: {unknown} (see --list)")
    key = api_key()
    if not key:
        sys.exit("score_bench: cannot read API_KEY (set WEB_TOOLS_API_KEY or `railway link`)")
    globals()["AWAY_BUDGET_S"] = args.away_budget  # read by call_when_up at call time
    run(configs, args.n, args.tag, args.label, args.out, key, args.threshold)


if __name__ == "__main__":
    main()

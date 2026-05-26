#!/usr/bin/env python3
"""
SearXNG entrypoint — generate settings.yml with the full Webshare
residential pool, then exec granian.

If WEBSHARE_API_KEY is set we call /api/v2/proxy/list/?mode=direct and
fold every valid proxy into `outgoing.proxies.all://`. SearXNG then
natively round-robins requests across the pool, and uses a different
proxy on each retry — which is exactly what we want when Google /
Brave CAPTCHA an individual IP.

If WEBSHARE_API_KEY is not set, fall back to:
  - the static PROXY_URL env var (single proxy, legacy behaviour)
  - or no proxy at all (delete the proxies: block)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TEMPLATE = "/etc/searxng/settings.yml.tpl"
OUTPUT = "/etc/searxng/settings.yml"

API_KEY = os.environ.get("WEBSHARE_API_KEY", "").strip()
SINGLE_PROXY = os.environ.get("PROXY_URL", "").strip()
WEBSHARE_BASE = "https://proxy.webshare.io/api/v2"


def fetch_webshare_pool() -> list[str]:
    """Return [http://user:pass@host:port, ...] for every valid proxy."""
    proxies: list[str] = []
    page = 1
    while True:
        req = urllib.request.Request(
            f"{WEBSHARE_BASE}/proxy/list/?mode=direct&page_size=100&page={page}",
            headers={"Authorization": f"Token {API_KEY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"webshare API page {page}: HTTP {e.code}", file=sys.stderr)
            break
        for p in data.get("results", []):
            if not p.get("valid"):
                continue
            proxies.append(
                f"http://{p['username']}:{p['password']}"
                f"@{p['proxy_address']}:{p['port']}"
            )
        if not data.get("next"):
            break
        page += 1
    return proxies


def render(template: str, pool: list[str]) -> str:
    """Substitute the proxies block in the template. The template uses
    `${PROXY_URL}` as a single-line placeholder under `proxies.all://`."""
    if not pool:
        # No pool → strip the entire proxies: block so SearXNG goes direct.
        lines = template.splitlines()
        out = []
        skipping = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("proxies:"):
                skipping = True
                continue
            if skipping:
                # Stop skipping when indentation returns to ≤ 2 spaces
                # (next top-level key inside outgoing:) or empty line.
                if line and not line.startswith(" ") and not line.startswith("\t"):
                    skipping = False
                elif line.startswith("  ") and not line.startswith("    "):
                    skipping = False
                else:
                    continue
            out.append(line)
        return "\n".join(out)

    # Build the YAML fragment: one `- http://...` per pool entry, indented
    # to match the existing `      - ${PROXY_URL}` line in settings.yml.
    fragment = "\n".join(f"      - {p}" for p in pool)
    rendered = []
    for line in template.splitlines():
        if "${PROXY_URL}" in line:
            rendered.append(fragment)
        else:
            rendered.append(line)
    return "\n".join(rendered)


def log(msg: str) -> None:
    """Print and flush so Railway's log aggregator captures it before exec."""
    sys.stdout.write(f"[searxng-entrypoint] {msg}\n")
    sys.stdout.flush()


def main() -> None:
    log(f"booting; WEBSHARE_API_KEY={'set' if API_KEY else 'unset'} PROXY_URL={'set' if SINGLE_PROXY else 'unset'}")
    with open(TEMPLATE) as f:
        template = f.read()

    pool: list[str] = []
    if API_KEY:
        try:
            pool = fetch_webshare_pool()
            log(f"webshare: loaded {len(pool)} proxies into outgoing.proxies.all://")
        except Exception as e:  # noqa: BLE001
            log(f"webshare fetch failed: {e}")
    elif SINGLE_PROXY:
        pool = [SINGLE_PROXY]
        log(f"using single PROXY_URL ({SINGLE_PROXY.split('@')[-1]})")
    else:
        log("no proxies configured; SearXNG will go direct")

    rendered = render(template, pool)
    with open(OUTPUT, "w") as f:
        f.write(rendered)
    log(f"wrote {OUTPUT} ({len(rendered)} bytes); exec'ing granian")

    # Exec granian — same args as the old entrypoint.
    os.execvp(
        "/usr/local/searxng/.venv/bin/granian",
        [
            "granian",
            "--interface", "wsgi",
            "--host", "0.0.0.0",
            "--port", "8080",
            "--workers", "4",
            "--blocking-threads", "8",
            "searx.webapp:app",
        ],
    )


if __name__ == "__main__":
    main()

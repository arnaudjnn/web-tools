"""
Scrapling sidecar — single-endpoint HTTP wrapper around StealthySession.

POST /fetch { url, network_idle?, timeout_ms? }  →  { status, url, html, size }

Auth: Bearer ${API_KEY} (matches the web-tools server convention).
Egress: routed through ${PROXY_URL} (e.g. thordata residential).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator
from scrapling.fetchers import StealthySession


API_KEY = os.environ.get("API_KEY", "")
PROXY_URL = os.environ.get("PROXY_URL", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scrapling-svc")


def parse_proxy(url: str):
    """Convert "http://user:pass@host:port" into Scrapling's dict form."""
    if not url:
        return None
    m = re.match(r"^(https?)://([^:]+):([^@]+)@(.+)$", url)
    if not m:
        return url
    return {
        "server": f"{m.group(1)}://{m.group(4)}",
        "username": m.group(2),
        "password": m.group(3),
    }


# Lazily-initialised, singleton StealthySession reused across requests so we
# don't pay the ~3-5 s browser-launch cost per fetch. Guarded by a lock —
# Camoufox/Patchright contexts are not thread-safe.
_session: Optional[StealthySession] = None
_lock = threading.Lock()


def _get_session() -> StealthySession:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                proxy = parse_proxy(PROXY_URL)
                rotator = ProxyRotator(proxies=[proxy]) if proxy else None
                s = StealthySession(headless=True, solve_cloudflare=False, proxy_rotator=rotator)
                s.__enter__()
                _session = s
                log.info("StealthySession initialised (proxy=%s)", proxy["server"] if isinstance(proxy, dict) else proxy)
    return _session


app = FastAPI(title="scrapling-svc", version="0.1.0")


class FetchRequest(BaseModel):
    url: str = Field(..., description="Absolute URL to fetch")
    network_idle: bool = Field(True, description="Wait for network idle before returning")
    timeout_ms: int = Field(60_000, ge=1_000, le=180_000)


class FetchResponse(BaseModel):
    status: int
    url: str
    html: str
    size: int


def _auth(authorization: Optional[str]) -> None:
    if not API_KEY:
        return
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/fetch", response_model=FetchResponse)
def fetch(req: FetchRequest, authorization: Optional[str] = Header(default=None)):
    _auth(authorization)

    session = _get_session()
    # Single global lock — only one fetch in flight at a time. Camoufox
    # contexts are not safe to share concurrently across threads.
    with _lock:
        try:
            page = session.fetch(req.url, network_idle=req.network_idle, timeout=req.timeout_ms)
        except Exception as e:
            log.exception("fetch failed url=%s", req.url)
            raise HTTPException(status_code=502, detail=str(e))

    return FetchResponse(
        status=page.status,
        url=page.url,
        html=page.html_content,
        size=len(page.html_content),
    )

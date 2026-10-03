"""reCAPTCHA's STATIC library, fetched direct (`FORM_CAPTCHA_LIB_DIRECT`).

Through the Evomi Italian residential exits the ~850 KB
`www.gstatic.com/recaptcha/releases/<ver>/recaptcha__<lang>.js` is often cut
mid-body (NS_ERROR_NET_PARTIAL_TRANSFER): grecaptcha never initialises and
the run ends `captcha_unavailable` / `captcha_token_missing` with zero POSTs —
the biggest source of attempts that never reach the target.

Behind a flag (default OFF; a request's `captcha_lib_direct` overrides it),
the form context routes ONLY versioned static release files on
www.gstatic.com through a direct server-side GET, with a small in-memory LRU
keyed by the full URL, and fulfils the browser's request with the same
bytes. The bytes are byte-identical (api.js loads the library with an SRI
`integrity` hash — a changed body would not run at all), and the request
carries no cookie, no Referer, no Origin and no query string.

Everything identity-bearing stays on the residential exit, untouched:
www.google.com / www.recaptcha.net (api.js, anchor, reload, bframe, clr,
userverify, webworker.js, payload) and every other gstatic path. A direct
fetch that fails for any reason (timeout, non-200, size mismatch, too big)
falls back: the request continues through the proxy exactly as before.

The trade-off FORMS.md names: Google's CDN sees the library fetched from the
service's datacenter IP while the anchor/reload traffic comes from the
residential exit. The library is a public, versioned, cookie-less asset;
whether that split moves the score is what the A/B (score_bench
`lib-direct` vs `lib-proxy`) measures before the default changes.

hCaptcha's assets do not live on www.gstatic.com and are out of scope.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

log = logging.getLogger("camoufox.forms")

ENV_FLAG = "FORM_CAPTCHA_LIB_DIRECT"
# Whole direct fetch, wall clock. It runs inside the route handler (the
# driver's dispatcher waits on it), so it sits below the 8 s default
# pre-POST mark: a slow CDN costs at most this before the proxy fallback.
TIMEOUT_S = float(os.environ.get("FORM_CAPTCHA_LIB_TIMEOUT_S", "5"))
MAX_BODY_BYTES = 4 * 1024 * 1024
CACHE_MAX_BYTES = int(os.environ.get("FORM_CAPTCHA_LIB_CACHE_BYTES", str(8 * 1024 * 1024)))
CACHE_MAX_ENTRIES = 16

STATIC_HOSTS = ("www.gstatic.com",)
# Versioned release files only: /recaptcha/releases/<ver>/<file>.(js|css).
# recaptcha__<lang>.js (the library), styles__ltr.css (the anchor's CSS)
# and their siblings. No other gstatic path, never a query string.
_STATIC_PATH = re.compile(
    r"^/recaptcha/releases/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\.(?:js|css)$")
_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
# Upstream headers worth handing back to the page; never Set-Cookie,
# Content-Encoding (the body is decoded) or Content-Length (fulfil sets it).
_KEEP_HEADERS = ("content-type", "cache-control", "expires", "last-modified", "etag",
                 "access-control-allow-origin", "cross-origin-resource-policy",
                 "timing-allow-origin", "x-content-type-options")


def env_default() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def enabled(override=None) -> bool:
    """The request's `captcha_lib_direct` when given, else the env flag."""
    return env_default() if override is None else bool(override)


def is_static_url(url: str) -> bool:
    """A versioned reCAPTCHA release file on www.gstatic.com — nothing else."""
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname in STATIC_HOSTS
            and port in (None, 443) and not parts.username and not parts.password
            and not parts.query and bool(_STATIC_PATH.match(parts.path)))


def is_static_request(url: str, method: str) -> bool:
    return (method or "").upper() == "GET" and is_static_url(url)


def new_stats() -> dict:
    return {"hits": 0, "cache_hits": 0, "bytes": 0, "failures": 0}


class FetchFailed(Exception):
    """The direct fetch did not produce a complete 200 body."""


class Fetcher:
    """Direct GET with an LRU by full URL (bounded in entries and bytes).

    `rewrite` (tests only) maps the URL actually dialled — the loopback
    fixture stands in for www.gstatic.com; the cache key stays the original.
    """

    def __init__(self, *, timeout_s=TIMEOUT_S, max_body=MAX_BODY_BYTES,
                 cache_bytes=CACHE_MAX_BYTES, cache_entries=CACHE_MAX_ENTRIES, rewrite=None):
        self.timeout_s = float(timeout_s)
        self.max_body = int(max_body)
        self.cache_bytes = int(cache_bytes)
        self.cache_entries = int(cache_entries)
        self.rewrite = rewrite
        self._cache = collections.OrderedDict()
        self._size = 0
        self._lock = threading.Lock()
        # Direct means direct: no HTTP(S)_PROXY from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, url, user_agent=None):
        """(status, headers, body, cached). Raises FetchFailed."""
        with self._lock:
            entry = self._cache.get(url)
            if entry is not None:
                self._cache.move_to_end(url)
                return entry + (True,)
        entry = self._fetch(url, user_agent)
        self._store(url, entry)
        return entry + (False,)

    def _fetch(self, url, user_agent):
        target = self.rewrite(url) if self.rewrite else url
        headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
        if user_agent:
            headers["User-Agent"] = user_agent
        deadline = time.monotonic() + self.timeout_s
        try:
            with self._opener.open(urllib.request.Request(target, headers=headers),
                                   timeout=self.timeout_s) as response:
                status = response.status
                if status != 200:
                    raise FetchFailed("status %s" % status)
                upstream = {k.lower(): v for k, v in response.headers.items()}
                expected = upstream.get("content-length")
                chunks, size = [], 0
                while True:
                    if time.monotonic() > deadline:
                        raise FetchFailed("deadline")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_body:
                        raise FetchFailed("too large")
                    chunks.append(chunk)
        except FetchFailed:
            raise
        except urllib.error.HTTPError as error:
            raise FetchFailed("status %s" % error.code) from None
        except Exception as error:
            raise FetchFailed(type(error).__name__) from None
        body = b"".join(chunks)
        if expected is not None and expected.isdigit() and int(expected) != len(body):
            raise FetchFailed("truncated")  # the very failure this exists to avoid
        if upstream.get("content-encoding", "identity").lower() not in ("identity", ""):
            raise FetchFailed("encoded")  # asked for identity; never pass on a coded body
        ext = urlsplit(url).path.rsplit(".", 1)[-1]
        out = {k: upstream[k] for k in _KEEP_HEADERS if k in upstream}
        out.setdefault("content-type", _TYPES.get(ext, "application/octet-stream"))
        # api.js loads the library crossorigin=anonymous with an SRI hash;
        # gstatic answers with ACAO * and so must the fulfilled copy.
        out.setdefault("access-control-allow-origin", "*")
        return 200, out, body

    def _store(self, url, entry):
        body = entry[2]
        if len(body) > self.cache_bytes:
            return
        with self._lock:
            old = self._cache.pop(url, None)
            if old is not None:
                self._size -= len(old[2])
            self._cache[url] = entry
            self._size += len(body)
            while self._cache and (self._size > self.cache_bytes
                                   or len(self._cache) > self.cache_entries):
                _, evicted = self._cache.popitem(last=False)
                self._size -= len(evicted[2])

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._size = 0


# One per process: the cache outlives a form job (the library's release path
# changes when Google ships a new version, which changes the key).
FETCHER = Fetcher()


def handler(stats, fetcher=None):
    """The route handler. Non-static requests fall back to the next handler
    (the POST guard), which continues them through the residential exit;
    a failed direct fetch falls back the same way."""

    def handle(route):
        request = route.request
        if not is_static_request(request.url, request.method):
            route.fallback()
            return
        source = fetcher or FETCHER
        try:
            try:
                user_agent = (request.headers or {}).get("user-agent")
            except Exception:
                user_agent = None
            status, headers, body, cached = source.get(request.url, user_agent)
        except Exception as error:
            stats["failures"] += 1
            log.info("form flow: captcha lib direct failed (%s); via the exit",
                     type(error).__name__ if not isinstance(error, FetchFailed) else error)
            route.fallback()
            return
        route.fulfill(status=status, headers=headers, body=body)
        stats["hits"] += 1
        stats["cache_hits"] += 1 if cached else 0
        stats["bytes"] += len(body)

    return handle


def install(context, stats, fetcher=None):
    """Route static release files through `handler`. Call AFTER the POST
    guard's `context.route("**/*", ...)`: the last registered route runs
    first, and anything this one does not fulfil falls back to the guard."""
    context.route(is_static_url, handler(stats, fetcher))

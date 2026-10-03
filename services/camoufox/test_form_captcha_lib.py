"""captcha_lib.py: reCAPTCHA's static library direct, everything else via the exit.

Unit tests need no browser. The Chromium test (FORM_BROWSER_TEST=chromium)
maps www.gstatic.com and www.google.com to an unreachable loopback port, so
nothing can leave the machine: the "direct" fetch is rewritten to a loopback
fixture, and any request that falls back to the network fails locally.
"""
import asyncio
import base64
import hashlib
import os
import re
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import captcha_lib

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = "https://www.gstatic.com/recaptcha/releases/TeSt-ver_42/recaptcha__it.js"
CSS = "https://www.gstatic.com/recaptcha/releases/TeSt-ver_42/styles__ltr.css"


class ClassificationTests(unittest.TestCase):
    STATIC = (
        LIB,
        CSS,
        "https://www.gstatic.com/recaptcha/releases/0aeEuuJmrVqDrE_39NbVIaW6/recaptcha__en.js",
        "https://www.gstatic.com:443/recaptcha/releases/v1/recaptcha__en.js",
    )
    IDENTITY = (
        # api.js and every google.com / recaptcha.net request: the exit's.
        "https://www.google.com/recaptcha/api.js?render=SITEKEY",
        "https://www.google.com/recaptcha/api.js",
        "https://www.google.com/recaptcha/enterprise.js",
        "https://www.recaptcha.net/recaptcha/api.js",
        "https://www.google.com/recaptcha/api2/anchor?ar=1&k=SITEKEY&co=x&hl=it&v=v1&size=invisible",
        "https://www.google.com/recaptcha/api2/reload?k=SITEKEY",
        "https://www.google.com/recaptcha/api2/bframe?hl=it&v=v1&k=SITEKEY",
        "https://www.google.com/recaptcha/api2/clr?k=SITEKEY",
        "https://www.google.com/recaptcha/api2/userverify?k=SITEKEY",
        "https://www.google.com/recaptcha/api2/webworker.js?hl=it&v=v1",
        "https://www.google.com/recaptcha/api2/payload?p=x&k=SITEKEY",
        "https://www.google.com/recaptcha/releases/v1/recaptcha__en.js",  # right path, wrong host
        "https://recaptcha.google.com/recaptcha/releases/v1/recaptcha__en.js",
    )
    NOT_STATIC = (
        LIB + "?k=SITEKEY",                      # a query string can carry identity
        "http://www.gstatic.com/recaptcha/releases/v1/recaptcha__en.js",
        "https://www.gstatic.com:8443/recaptcha/releases/v1/recaptcha__en.js",
        "https://user:pw@www.gstatic.com/recaptcha/releases/v1/recaptcha__en.js",
        "https://www.gstatic.com.example.com/recaptcha/releases/v1/recaptcha__en.js",
        "https://gstatic.com/recaptcha/releases/v1/recaptcha__en.js",
        "https://www.gstatic.com/recaptcha/api2/logo_48.png",
        "https://www.gstatic.com/recaptcha/releases/v1/recaptcha__en.json",
        "https://www.gstatic.com/recaptcha/releases/../releases/v1/recaptcha__en.js",
        "https://www.gstatic.com/recaptcha/releases/v1/sub/recaptcha__en.js",
        "https://www.gstatic.com/recaptcha/releases/v1/.hidden.js",
        "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js",
        "https://www.gstatic.com/recaptcha/releases/v1/",
        "",
        "not a url",
        "https://[::1/recaptcha/releases/v1/recaptcha__en.js",
    )

    def test_versioned_release_files_on_gstatic_are_static(self):
        for url in self.STATIC:
            self.assertTrue(captcha_lib.is_static_url(url), url)
            self.assertTrue(captcha_lib.is_static_request(url, "GET"), url)

    def test_identity_bearing_requests_are_never_static(self):
        for url in self.IDENTITY:
            self.assertFalse(captcha_lib.is_static_url(url), url)

    def test_anything_else_is_not_static(self):
        for url in self.NOT_STATIC:
            self.assertFalse(captcha_lib.is_static_url(url), url)

    def test_only_get(self):
        for method in ("POST", "HEAD", "OPTIONS", "PUT", "", None):
            self.assertFalse(captcha_lib.is_static_request(LIB, method), method)
        self.assertTrue(captcha_lib.is_static_request(LIB, "get"))

    def test_the_path_classes_agree_with_the_readiness_gate(self):
        import form_flow
        self.assertEqual(form_flow.captcha_path_class(LIB), "recaptcha__*.js")
        self.assertEqual(form_flow.captcha_path_class(CSS), "styles__*.css")


class FlagTests(unittest.TestCase):
    def test_default_off_and_env_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(captcha_lib.ENV_FLAG, None)
            self.assertFalse(captcha_lib.enabled())
        for value in ("1", "true", "YES", " on "):
            with patch.dict(os.environ, {captcha_lib.ENV_FLAG: value}):
                self.assertTrue(captcha_lib.enabled(), value)
        for value in ("0", "false", "", "off"):
            with patch.dict(os.environ, {captcha_lib.ENV_FLAG: value}):
                self.assertFalse(captcha_lib.enabled(), value)

    def test_the_request_overrides_the_env(self):
        with patch.dict(os.environ, {captcha_lib.ENV_FLAG: "1"}):
            self.assertFalse(captcha_lib.enabled(False))
            self.assertTrue(captcha_lib.enabled(None))
        with patch.dict(os.environ, {captcha_lib.ENV_FLAG: "0"}):
            self.assertTrue(captcha_lib.enabled(True))
            self.assertFalse(captcha_lib.enabled(None))


# ── loopback CDN stand-in ───────────────────────────────────────────

LIB_JS = (b"/* fixture library: not reCAPTCHA */\n"
          b"window.__fixtureLib = 'ran';\n"
          b"window.grecaptcha = {ready: function (f) { f(); }, execute: function () {"
          b" return Promise.resolve('fixture-token-not-a-real-captcha'); }};\n"
          b"document.addEventListener('submit', function (e) {"
          b" var t = e.target.querySelector('[name=g-recaptcha-response]');"
          b" if (t) t.value = 'fixture-token-not-a-real-captcha';"
          b" var m = e.target.querySelector('[name=lib]'); if (m) m.value = window.__fixtureLib; }, true);\n")


@contextmanager
def cdn(behaviour=None):
    """A loopback server answering /recaptcha/releases/... like gstatic."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_GET(self):
            seen.append({"path": self.path, "headers": dict(self.headers)})
            mode = (behaviour or {}).get(self.path, "ok")
            if mode == "404":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "slow":
                time.sleep(1.5)
            body = LIB_JS if self.path.endswith(".js") else b".rc-anchor{}"
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript" if self.path.endswith(".js") else "text/css")
            self.send_header("Cache-Control", "public, max-age=31536000")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Set-Cookie", "never=forwarded")
            if mode == "cut":
                # Headers promise more than the body: the very cut the exit does.
                self.send_header("Content-Length", str(len(body) + 1000))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def to(base):
    """Dial the loopback CDN instead of www.gstatic.com (tests only)."""
    return lambda url: re.sub(r"^https://www\.gstatic\.com(?::443)?", base, url)


class FetcherTests(unittest.TestCase):
    def test_a_fetch_is_cached_by_full_url(self):
        with cdn() as (base, seen):
            fetcher = captcha_lib.Fetcher(rewrite=to(base))
            status, headers, body, cached = fetcher.get(LIB, "UA/1")
            self.assertEqual((status, body, cached), (200, LIB_JS, False))
            self.assertEqual(fetcher.get(LIB)[3], True)
            self.assertEqual(len(seen), 1)
            # Only what the page needs; never a cookie or a coding header.
            self.assertEqual(headers["content-type"], "text/javascript")
            self.assertEqual(headers["access-control-allow-origin"], "*")
            self.assertIn("cache-control", headers)
            self.assertNotIn("set-cookie", headers)
            self.assertNotIn("content-length", headers)
            # The request carries no cookie, referer or origin.
            sent = {k.lower() for k in seen[0]["headers"]}
            self.assertFalse(sent & {"cookie", "referer", "origin", "authorization"})
            self.assertEqual(seen[0]["headers"].get("User-Agent"), "UA/1")

    def test_a_cut_body_is_a_failure_not_a_cache_entry(self):
        path = "/recaptcha/releases/TeSt-ver_42/recaptcha__it.js"
        with cdn({path: "cut"}) as (base, _):
            fetcher = captcha_lib.Fetcher(rewrite=to(base))
            with self.assertRaises(captcha_lib.FetchFailed):
                fetcher.get(LIB)
            self.assertEqual(len(fetcher._cache), 0)

    def test_non_200_and_timeouts_fail(self):
        path = "/recaptcha/releases/TeSt-ver_42/recaptcha__it.js"
        with cdn({path: "404"}) as (base, _):
            with self.assertRaises(captcha_lib.FetchFailed):
                captcha_lib.Fetcher(rewrite=to(base)).get(LIB)
        with cdn({path: "slow"}) as (base, _):
            started = time.monotonic()
            with self.assertRaises(captcha_lib.FetchFailed):
                captcha_lib.Fetcher(rewrite=to(base), timeout_s=0.3).get(LIB)
            self.assertLess(time.monotonic() - started, 1.4)

    def test_a_body_over_the_cap_fails(self):
        with cdn() as (base, _):
            with self.assertRaises(captcha_lib.FetchFailed):
                captcha_lib.Fetcher(rewrite=to(base), max_body=10).get(LIB)

    def test_direct_ignores_proxy_environment(self):
        with cdn() as (base, seen), patch.dict(os.environ, {
                "http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                "https_proxy": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                "no_proxy": "", "NO_PROXY": ""}):
            fetcher = captcha_lib.Fetcher(rewrite=to(base))
            self.assertEqual(fetcher.get(LIB)[2], LIB_JS)
            self.assertEqual(len(seen), 1)

    def test_the_cache_is_bounded_in_entries_and_bytes(self):
        fetcher = captcha_lib.Fetcher(cache_bytes=100, cache_entries=2)
        for i in range(3):
            fetcher._store(f"u{i}", (200, {}, b"x" * 10))
        self.assertEqual(list(fetcher._cache), ["u1", "u2"])
        fetcher._store("big", (200, {}, b"x" * 95))
        self.assertEqual(list(fetcher._cache), ["big"])
        self.assertEqual(fetcher._size, 95)
        fetcher._store("huge", (200, {}, b"x" * 101))  # never cached at all
        self.assertNotIn("huge", fetcher._cache)


class FakeRoute:
    def __init__(self, url, method="GET", headers=None):
        self.request = SimpleNamespace(url=url, method=method, headers=headers or {})
        self.calls = []

    def fallback(self):
        self.calls.append(("fallback",))

    def fulfill(self, **kw):
        self.calls.append(("fulfill", kw))


class FakeFetcher:
    def __init__(self, fail=False):
        self.fail, self.urls = fail, []

    def get(self, url, user_agent=None):
        self.urls.append(url)
        if self.fail:
            raise captcha_lib.FetchFailed("deadline")
        return 200, {"content-type": "text/javascript"}, LIB_JS, len(self.urls) > 1


class HandlerTests(unittest.TestCase):
    def test_static_get_is_fulfilled_and_counted(self):
        stats, fetcher = captcha_lib.new_stats(), FakeFetcher()
        handle = captcha_lib.handler(stats, fetcher)
        for _ in range(2):
            route = FakeRoute(LIB, headers={"user-agent": "UA"})
            handle(route)
            self.assertEqual(route.calls[0][0], "fulfill")
            self.assertEqual(route.calls[0][1]["body"], LIB_JS)
            self.assertEqual(route.calls[0][1]["status"], 200)
        self.assertEqual(stats, {"hits": 2, "cache_hits": 1, "bytes": 2 * len(LIB_JS), "failures": 0})

    def test_everything_else_falls_back_untouched(self):
        stats, fetcher = captcha_lib.new_stats(), FakeFetcher()
        handle = captcha_lib.handler(stats, fetcher)
        for url, method in [(u, "GET") for u in ClassificationTests.IDENTITY] + [(LIB, "POST")]:
            route = FakeRoute(url, method)
            handle(route)
            self.assertEqual(route.calls, [("fallback",)], url)
        self.assertEqual(fetcher.urls, [])
        self.assertEqual(stats, captcha_lib.new_stats())

    def test_a_failed_direct_fetch_falls_back_to_the_exit(self):
        stats = captcha_lib.new_stats()
        route = FakeRoute(LIB)
        captcha_lib.handler(stats, FakeFetcher(fail=True))(route)
        self.assertEqual(route.calls, [("fallback",)])
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["hits"], 0)

    def test_install_registers_one_predicate_route(self):
        registered = []
        context = SimpleNamespace(route=lambda match, handle: registered.append((match, handle)))
        captcha_lib.install(context, captcha_lib.new_stats(), FakeFetcher())
        self.assertEqual(len(registered), 1)
        match = registered[0][0]
        self.assertTrue(match(LIB))
        self.assertFalse(match("https://www.google.com/recaptcha/api2/anchor?k=x"))


class WiringTests(unittest.TestCase):
    """The flag reaches run_form from /form-submit, the score probe and the gate."""

    def test_service_workers_stay_blocked_on_every_form_context(self):
        # Route interception cannot see a request a service worker serves.
        with open(os.path.join(HERE, "form_worker.py")) as handle:
            self.assertIn('service_workers="block"', handle.read())
        with open(os.path.join(HERE, "app.py")) as handle:
            self.assertIn('opts["service_workers"] = "block"', handle.read())

    def test_the_lib_route_is_registered_after_the_guard(self):
        # Playwright runs routes last-registered-first: the lib route must
        # come after the guard so it sees static files first and falls back
        # to the guard for everything else.
        with open(os.path.join(HERE, "form_flow.py")) as handle:
            source = handle.read()
        guard_at = source.index('context.route("**/*", guard)')
        lib_at = source.index("captcha_lib.install(context")
        self.assertLess(guard_at, lib_at)

    def test_form_submit_passes_the_override_to_run_form(self):
        import test_form_stealth as stealth
        app = stealth.app
        self.assertIn("captcha_lib_direct", app.FormSubmitRequest.__annotations__)
        req = SimpleNamespace(
            ready_expression=None, headed=False, profile=None, url="https://form.test/",
            fields=[], submit="#s", dismiss=[], success_url=None, wait_until="domcontentloaded",
            wait_ms=0, settle_ms=1000, submission_urls=None, captcha_field=None,
            inspect_only=False, require_captcha_token=False, gate_text=None, step2=[],
            step2_submit=None, completion_markers=[], stop_after_posts=None,
            captcha_rejection_text=None, captcha_lib_direct=True)
        seen = {}

        async def run(job, **kw):
            seen.update(job.keywords)
            return {"ok": True}

        with patch.object(app._form_worker, "run", run):
            asyncio.run(app._form_run(req, time.monotonic() + 60, "tok", None, None, None))
        self.assertIs(seen["captcha_lib_direct"], True)

    def test_the_score_probe_and_gate_pass_it_through(self):
        import test_form_stealth as stealth
        score_probe = stealth.score_probe
        self.assertIn("captcha_lib_direct", score_probe.ScoreProbeRequest.__annotations__)
        jobs = []

        async def run(job, url, deadline, live):
            jobs.append(job.keywords)
            return {"diagnostics": {"captcha_lib_direct": {"hits": 1}}}

        with patch.object(score_probe, "_run", run), \
                patch.dict(score_probe._deps, {"browser_factory": lambda *a: None,
                                               "run_isolated": lambda *a, **k: None}):
            for value in (True, False, None):
                asyncio.run(score_probe.run_probe(
                    oracle_url="https://tools.test/oracle/recaptcha", session="s", profile=None,
                    headed=True, wait_ms=0, field_count=1, action=None, timeout_ms=30000,
                    captcha_lib_direct=value))
        self.assertIs(jobs[0]["captcha_lib_direct"], True)
        self.assertIs(jobs[1]["captcha_lib_direct"], False)
        self.assertNotIn("captcha_lib_direct", jobs[2])  # None = the env default
        summary = score_probe.summarize_probe({"diagnostics": {"captcha_lib_direct": {"hits": 1}}},
                                              session="s", profile=None, headed=True,
                                              threshold=0.7, started=time.monotonic())
        self.assertEqual(summary["form"]["captcha_lib_direct"], {"hits": 1})

        probed = []

        async def run_egress(session, timeout_ms):
            return {"egress": {"ip": "1.1.1.1", "asn": 1}}

        async def run_probe(**kw):
            probed.append(kw)
            return {}

        with patch.object(score_probe, "run_egress", run_egress), \
                patch.object(score_probe, "run_probe", run_probe), \
                patch.object(score_probe.profile_store, "blocked_reason", lambda *a: None), \
                patch.object(score_probe.profile_store, "record_exit_score", lambda *a: None):
            asyncio.run(score_probe.run_gate(
                oracle_url="https://tools.test/oracle/recaptcha", profile=None, headed=True,
                threshold=0.7, tries=1, deadline=time.monotonic() + 400, reserve_s=100,
                captcha_lib_direct=True))
        self.assertIs(probed[0]["captcha_lib_direct"], True)

    def test_the_bench_has_the_ab_pair_and_interleaves(self):
        import test_form_score_bench  # noqa: F401  (puts score_bench on sys.path)
        import score_bench
        self.assertEqual(score_bench.CONFIGS["lib-direct"][2]("t", 0), {"captcha_lib_direct": True})
        self.assertEqual(score_bench.CONFIGS["lib-proxy"][2]("t", 0), {"captcha_lib_direct": False})
        self.assertEqual(score_bench.schedule(["a", "b"], 2, interleave=True),
                         [("a", 0), ("b", 0), ("a", 1), ("b", 1)])
        self.assertEqual(score_bench.schedule(["a", "b"], 2),
                         [("a", 0), ("a", 1), ("b", 0), ("b", 1)])


# ── Chromium: the fulfilled library really runs ─────────────────────

def _sri(body):
    return "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode()


@unittest.skipUnless(os.environ.get("FORM_BROWSER_TEST") == "chromium", "opt-in Chromium fixture")
class ChromiumTests(unittest.TestCase):
    """Nothing leaves the machine: both Google hosts resolve to a closed
    loopback port, the direct fetch is rewritten to the loopback CDN."""

    def run_page(self, fetcher, direct=True):
        from playwright.sync_api import sync_playwright
        from form_flow import run_form
        posts = []
        page = ('''<form method="post" action="/form">
          <input id="name" name="name" required>
          <input type="hidden" name="g-recaptcha-response" value="">
          <input type="hidden" name="lib" value="">
          <button id="submit">Submit</button></form>
          <link rel="stylesheet" href="%s">
          <script>
            // What api.js does: insert the release library with SRI, crossorigin.
            var s = document.createElement('script');
            s.src = '%s'; s.crossOrigin = 'anonymous'; s.integrity = '%s';
            document.head.appendChild(s);
            // An identity-bearing request: must never be fulfilled direct.
            fetch('https://www.google.com/recaptcha/api2/clr?k=fixture', {method: 'POST', body: 'x'})
              .catch(function () {});
          </script>''' % (CSS, LIB, _sri(LIB_JS))).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"done" if self.path == "/done" else page)

            def do_POST(self):
                posts.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(303)
                self.send_header("Location", "/done")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p, patch.object(captcha_lib, "FETCHER", fetcher):
                browser = p.chromium.launch(args=[
                    "--host-resolver-rules=MAP www.gstatic.com 127.0.0.1:1, "
                    "MAP www.google.com 127.0.0.1:1"])
                try:
                    context = browser.new_context(service_workers="block")
                    result = run_form(
                        context, url=f"http://127.0.0.1:{server.server_port}/form",
                        fields=[{"selector": "#name", "value": "Test"}], submit="#submit",
                        settle_ms=1000, timeout_ms=30000, success_url=r"/done$",
                        require_captcha_token=True, captcha_lib_direct=direct)
                    context.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        return result, posts

    def test_the_fulfilled_library_runs_and_the_guard_still_counts_the_post(self):
        with cdn() as (base, seen):
            fetcher = captcha_lib.Fetcher(rewrite=to(base))
            result, posts = self.run_page(fetcher)
        d = result["diagnostics"]
        # The library executed (SRI + CORS accepted the fulfilled copy) and
        # its submit listener wrote the token the guard requires.
        self.assertEqual(len(posts), 1)
        self.assertIn(b"lib=ran", posts[0])
        self.assertIn(b"g-recaptcha-response=fixture-token-not-a-real-captcha", posts[0])
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["form_submissions"], 1)
        self.assertTrue(d["token_present"])
        # The page's request listeners saw the library like any other.
        self.assertTrue(d["captcha_lib_loaded"])
        self.assertGreaterEqual(d["captcha_script_requests"], 1)
        self.assertTrue(d["captcha_ready"])
        # Library + stylesheet direct; the google.com request was not.
        self.assertEqual(d["captcha_lib_direct"]["hits"], 2)
        self.assertEqual(d["captcha_lib_direct"]["failures"], 0)
        self.assertEqual(d["captcha_lib_direct"]["cache_hits"], 0)
        self.assertEqual(d["captcha_lib_direct"]["bytes"], len(LIB_JS) + len(b".rc-anchor{}"))
        self.assertEqual(sorted(s["path"] for s in seen),
                         ["/recaptcha/releases/TeSt-ver_42/recaptcha__it.js",
                          "/recaptcha/releases/TeSt-ver_42/styles__ltr.css"])

    def test_sri_is_enforced_on_the_fulfilled_copy(self):
        # Why the body must be byte-identical: api.js pins the library with
        # an integrity hash. A changed copy never runs — no listener, no
        # token — and the token guard blocks the POST.
        class Altered(FakeFetcher):
            def get(self, url, user_agent=None):
                status, headers, body, cached = super().get(url, user_agent)
                return status, headers, body + b"\n", cached

        result, posts = self.run_page(Altered())
        self.assertEqual(posts, [])
        self.assertEqual(result["error"], "captcha_token_missing")
        self.assertEqual(result["form_submissions"], 0)

    def test_a_failed_direct_fetch_falls_back_to_the_network(self):
        # Direct fails -> the request continues through the browser's own
        # network (here: a closed loopback port) -> the library never runs,
        # and the readiness gate answers captcha_unavailable with zero POSTs.
        fetcher = FakeFetcher(fail=True)
        result, posts = self.run_page(fetcher)
        d = result["diagnostics"]
        self.assertEqual(posts, [])
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "captcha_unavailable")
        self.assertGreaterEqual(d["captcha_lib_direct"]["failures"], 2)
        self.assertEqual(d["captcha_lib_direct"]["hits"], 0)
        self.assertIn(LIB, fetcher.urls)
        self.assertFalse(any("www.google.com" in u for u in fetcher.urls))
        self.assertGreaterEqual(d["captcha_network_failures"], 1)

    def test_flag_off_installs_nothing(self):
        fetcher = FakeFetcher()
        result, posts = self.run_page(fetcher, direct=False)
        self.assertIsNone(result["diagnostics"]["captcha_lib_direct"])
        self.assertEqual(fetcher.urls, [])
        self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()

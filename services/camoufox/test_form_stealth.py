"""Stealth-score plumbing (no browser, no network, no third-party form).

- The oracle verdict is parsed out of the verify page exactly as Tools
  renders it (packages/api/src/oracle.ts renderVerdict), and a probe answer
  never carries the page HTML.
- Profiles remember their exit in fingerprint.json under a reserved key that
  never reaches Camoufox, reuse it unless the caller overrides, and survive a
  browser upgrade by redrawing the fingerprint while keeping the cookies.
- The exit-quality blocklist blocks a fresh low IP, never an ASN on one bad
  sample.

app.py is imported with its third-party dependencies stubbed when no other
test file did so first; launches are recorded by patching app.Camoufox.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def _stub_module(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_stubs():
    fastapi = _stub_module("fastapi")

    class _App:
        def __init__(self, *args, **kwargs):
            pass

        def _decorator(self, *args, **kwargs):
            def wrap(fn):
                return fn
            return wrap

        on_event = _decorator
        get = _decorator
        post = _decorator

    fastapi.FastAPI = _App
    fastapi.HTTPException = type("HTTPException", (Exception,), {})
    responses = _stub_module("fastapi.responses")
    responses.JSONResponse = object
    pydantic = _stub_module("pydantic")

    class _BaseModel:
        def __init_subclass__(cls, **kwargs):
            pass

    pydantic.BaseModel = _BaseModel
    pydantic.Field = lambda *args, **kwargs: None
    _stub_module("camoufox")
    sync_api = _stub_module("camoufox.sync_api")
    sync_api.Camoufox = object
    utils = _stub_module("camoufox.utils")
    utils.launch_options = lambda **kwargs: {**kwargs}


if "app" not in sys.modules:
    _install_stubs()
    os.environ.setdefault("PROXY_URL", "http://user:pass_country-IT@proxy.test:1234")

import app  # noqa: E402
import profile_store  # noqa: E402
import score_probe  # noqa: E402


def verdict_page(payload):
    # Byte-for-byte the shape of oracle.ts renderVerdict.
    body = json.dumps(payload).replace("<", "\\u003c")
    return ('<!doctype html><html><head><meta charset="utf-8"><title>Esito</title></head><body>\n'
            '<h1>Esito verifica</h1>\n'
            f'<script type="application/json" id="oracle-verdict">{body}</script>\n'
            f'<pre>{json.dumps(payload)}</pre></body></html>')


class _Root(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._env = patch.dict(os.environ, {"FORM_PROFILE_DIR": self.root})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class VerdictParsingTests(unittest.TestCase):
    def test_passing_verdict(self):
        v = score_probe.parse_verdict(verdict_page({
            "success": True, "score": 0.9, "action": "user_registration_production",
            "hostname": "tools.example", "error-codes": [], "action_ok": True,
            "client_ip": "151.0.0.1", "token_length": 2400, "page_dwell_s": 41.2, "mint_s": 0.4}))
        self.assertTrue(v["success"])
        self.assertEqual(v["score"], 0.9)
        self.assertTrue(v["action_ok"])
        self.assertEqual(v["client_ip"], "151.0.0.1")
        self.assertEqual(v["page_dwell_s"], 41.2)

    def test_failure_verdict_keeps_codes(self):
        v = score_probe.parse_verdict(verdict_page({"success": False, "score": None,
                                                    "error-codes": ["timeout-or-duplicate", 3]}))
        self.assertFalse(v["success"])
        self.assertIsNone(v["score"])
        self.assertEqual(v["error-codes"], ["timeout-or-duplicate"])

    def test_escaped_payload_round_trips(self):
        v = score_probe.parse_verdict(verdict_page({"success": True, "score": 0.3,
                                                    "hostname": "</script><b>"}))
        self.assertEqual(v["hostname"], "</script><b>")

    def test_no_or_broken_verdict(self):
        self.assertIsNone(score_probe.parse_verdict(""))
        self.assertIsNone(score_probe.parse_verdict(None))
        self.assertIsNone(score_probe.parse_verdict("<html>Registrazione</html>"))
        self.assertIsNone(score_probe.parse_verdict(
            '<script type="application/json" id="oracle-verdict">{nope</script>'))
        self.assertIsNone(score_probe.parse_verdict(
            '<script type="application/json" id="oracle-verdict">[1]</script>'))

    def test_bool_is_not_a_score(self):
        v = score_probe.parse_verdict(verdict_page({"success": True, "score": True}))
        self.assertIsNone(v["score"])


class ProbeShapeTests(unittest.TestCase):
    def test_oracle_urls(self):
        page, verify = score_probe.oracle_urls("https://t.example/oracle/recaptcha/")
        self.assertEqual(page, "https://t.example/oracle/recaptcha")
        self.assertEqual(verify, "https://t.example/oracle/recaptcha/verify")
        page, _ = score_probe.oracle_urls("https://t.example/oracle/recaptcha", "login_v2")
        self.assertEqual(page, "https://t.example/oracle/recaptcha?action=login_v2")
        for bad in ("ftp://x/y", "https://u:p@t.example/o", "", "/oracle/recaptcha"):
            with self.assertRaises(ValueError):
                score_probe.oracle_urls(bad)
        with self.assertRaises(ValueError):
            score_probe.oracle_urls("https://t.example/o", "bad action")

    def test_probe_is_a_plain_one_post_form_on_the_oracle(self):
        page, verify = score_probe.oracle_urls("https://t.example/oracle/recaptcha")
        params = score_probe.probe_params(page, verify, 3, 4000)
        self.assertEqual([f["selector"] for f in params["fields"]], ["#company", "#email", "#phone"])
        self.assertTrue(all(f["value"] for f in params["fields"]))
        self.assertEqual(params["submission_urls"], [verify])
        self.assertEqual(params["captcha_field"], "g-recaptcha-response")
        # run_form's own validation accepts it (same origin, valid regex).
        app.validate_form(params["url"], params["submission_urls"], params["success_url"])
        self.assertEqual(len(score_probe.probe_params(page, verify, 1, 0)["fields"]), 1)

    def test_summary_never_carries_html_and_applies_threshold(self):
        data = {"html": verdict_page({"success": True, "score": 0.7, "client_ip": "1.2.3.4"}),
                "error": None, "ok": True, "status": 200, "form_submissions": 1,
                "diagnostics": {"egress": {"country": "Italy", "asn": 3269, "isp": "TIM"},
                                "token_present": True}}
        s = score_probe.summarize_probe(data, session="abc", profile=None, headed=True,
                                        threshold=0.7, started=0.0)
        self.assertTrue(s["passed"])
        self.assertEqual(s["egress"], {"country": "Italy", "asn": 3269, "isp": "TIM", "ip": "1.2.3.4"})
        self.assertNotIn("html", json.dumps(s))
        low = score_probe.summarize_probe({**data, "html": verdict_page({"success": True, "score": 0.3})},
                                          session="abc", profile=None, headed=True,
                                          threshold=0.7, started=0.0)
        self.assertFalse(low["passed"])
        none = score_probe.summarize_probe({"error": "browser_launch_failed"}, session="abc",
                                           profile=None, headed=True, threshold=0.7, started=0.0)
        self.assertIsNone(none["score"])
        self.assertFalse(none["passed"])

    def test_egress_normalization(self):
        self.assertEqual(score_probe.normalize_egress(
            {"success": True, "ip": "1.2.3.4", "country": "Italy",
             "connection": {"asn": 1267, "isp": "WIND"}}),
            {"ip": "1.2.3.4", "country": "Italy", "asn": 1267, "isp": "WIND"})
        self.assertIsNone(score_probe.normalize_egress({"success": False}))
        self.assertIsNone(score_probe.normalize_egress(None))


class ExitPersistenceTests(_Root):
    def new_tokens(self):
        counter = iter(range(1000))
        return lambda: f"tok{next(counter)}"

    def resolve(self, **kw):
        base = dict(profile=None, exit_session=None, fresh_ip=True, sticky=False,
                    shared="shared", new_token=self.new_tokens())
        base.update(kw)
        return profile_store.resolve_exit_session(**base)

    def test_old_behaviour_without_sticky(self):
        self.assertEqual(self.resolve(), ("tok0", False))
        self.assertEqual(self.resolve(fresh_ip=False), ("shared", False))
        self.assertEqual(self.resolve(profile="p", sticky=False), ("tok0", False))

    def test_sticky_profile_pins_then_reuses(self):
        session, pin = self.resolve(profile="p", sticky=True)
        self.assertEqual((session, pin), ("tok0", True))
        profile_store.remember_exit("p", session)
        self.assertEqual(self.resolve(profile="p", sticky=True), ("tok0", False))
        # fresh_ip does not rotate a sticky profile; only an override does.
        self.assertEqual(self.resolve(profile="p", sticky=True, fresh_ip=True)[0], "tok0")

    def test_caller_override_wins_and_is_pinned_on_sticky(self):
        profile_store.remember_exit("p", "old")
        self.assertEqual(self.resolve(profile="p", sticky=True, exit_session="mine"), ("mine", True))
        self.assertEqual(self.resolve(profile="p", sticky=False, exit_session="mine"), ("mine", False))

    def test_meta_survives_the_first_launch_writing_options(self):
        profile_store.remember_exit("p", "tokA", ip="1.1.1.1", score=0.9)
        directory = profile_store.profile_dir("p")
        self.assertIsNone(profile_store.load_launch_opts(directory))  # meta only = no fingerprint yet
        profile_store.save_launch_opts(directory, {"headless": False, "args": []})
        self.assertEqual(profile_store.stored_exit("p"), "tokA")
        opts = profile_store.load_launch_opts(directory)
        self.assertEqual(opts, {"headless": False, "args": []})
        self.assertNotIn(profile_store.META_KEY, opts)
        # A later save (e.g. a redraw) keeps the pin too.
        profile_store.save_launch_opts(directory, {"headless": True, profile_store.META_KEY: {"x": 1}})
        self.assertEqual(profile_store.stored_exit("p"), "tokA")

    def test_upgrade_redraws_options_but_keeps_pin(self):
        directory = profile_store.profile_dir("p")
        profile_store.save_launch_opts(directory, {"executable_path": "/gone/camoufox-152/camoufox-bin"})
        profile_store.remember_exit("p", "tokA")
        self.assertIsNone(profile_store.load_launch_opts(directory))
        real = os.path.join(self.root, "bin")
        open(real, "w").close()
        profile_store.save_launch_opts(directory, {"executable_path": real})
        self.assertEqual(profile_store.load_launch_opts(directory), {"executable_path": real})
        self.assertEqual(profile_store.stored_exit("p"), "tokA")

    def test_ip_change_is_visible_and_forget_clears(self):
        profile_store.remember_exit("p", "tokA", ip="1.1.1.1")
        self.assertFalse(profile_store.note_exit_seen("p", ip="1.1.1.1"))
        self.assertTrue(profile_store.note_exit_seen("p", ip="2.2.2.2"))
        self.assertEqual(profile_store.pinned_exits()["p"]["exit_ip"], "2.2.2.2")
        profile_store.forget_exit("p")
        self.assertIsNone(profile_store.stored_exit("p"))
        self.assertEqual(profile_store.pinned_exits(), {})

    def test_corrupt_file_reads_as_empty(self):
        directory = profile_store.profile_dir("p")
        os.makedirs(directory)
        with open(os.path.join(directory, "fingerprint.json"), "w") as handle:
            handle.write("{half")
        self.assertIsNone(profile_store.load_launch_opts(directory))
        self.assertIsNone(profile_store.stored_exit("p"))

    def test_sticky_default_flag(self):
        with patch.dict(os.environ, {"FORM_PROFILE_STICKY_EXIT": "1"}):
            self.assertTrue(profile_store.sticky_default())
        with patch.dict(os.environ, {"FORM_PROFILE_STICKY_EXIT": ""}):
            self.assertFalse(profile_store.sticky_default())


class AppWiringTests(_Root):
    def setUp(self):
        super().setUp()
        self.recorded = []
        recorded = self.recorded

        class _Recorder:
            def __init__(self, *args, **kwargs):
                recorded.append(kwargs)

        self._patches = [patch.object(app, "Camoufox", _Recorder),
                         patch.object(app, "launch_options", lambda **kw: {**kw})]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def test_meta_never_reaches_camoufox(self):
        session, pin = app._resolve_form_session("bat", None, True, True)
        self.assertTrue(pin)
        self.assertEqual(profile_store.stored_exit("bat"), session)
        app._form_browser(session, False, True, "bat")
        app._form_browser(session, False, True, "bat")
        for call in self.recorded:
            self.assertNotIn(profile_store.META_KEY, call["from_options"])
            self.assertIn(f"_session-{session}", call["from_options"]["proxy"]["password"])
        # Second resolve reuses the pinned exit instead of a fresh token.
        self.assertEqual(app._resolve_form_session("bat", None, True, True), (session, False))

    def test_non_sticky_profile_keeps_fresh_ip_per_run(self):
        a, _ = app._resolve_form_session("bat", None, True, False)
        b, _ = app._resolve_form_session("bat", None, True, False)
        self.assertNotEqual(a, b)
        self.assertIsNone(profile_store.stored_exit("bat"))

    def test_probe_endpoints_registered(self):
        for name in ("form_score_probe", "form_warm", "form_exit_select", "form_exits"):
            self.assertTrue(callable(getattr(score_probe, name)))
        self.assertIs(score_probe._deps["browser_factory"], app._form_browser)
        self.assertIs(score_probe._deps["worker"], app._form_worker)


class BlocklistTests(_Root):
    def test_fresh_low_ip_is_blocked_and_expires(self):
        profile_store.record_exit_score("1.2.3.4", 3269, 0.3, 0.7, now=1000)
        self.assertEqual(profile_store.blocked_reason("1.2.3.4", 3269, 0.7, now=1001), "ip_low_score")
        self.assertIsNone(profile_store.blocked_reason("1.2.3.4", 3269, 0.7,
                                                       now=1000 + profile_store.IP_TTL_S + 1))
        self.assertIsNone(profile_store.blocked_reason("9.9.9.9", 3269, 0.7, now=1001))

    def test_a_good_last_score_unblocks_the_ip(self):
        profile_store.record_exit_score("1.2.3.4", 3269, 0.1, 0.7, now=1000)
        profile_store.record_exit_score("1.2.3.4", 3269, 0.9, 0.7, now=1100)
        self.assertIsNone(profile_store.blocked_reason("1.2.3.4", 3269, 0.7, now=1200))
        entry = profile_store.load_blocklist()["ips"]["1.2.3.4"]
        self.assertEqual((entry["n"], entry["low"], entry["last"]), (2, 1, 0.9))

    def test_asn_needs_a_sustained_record(self):
        for i in range(profile_store.ASN_MIN_SAMPLES - 1):
            profile_store.record_exit_score(f"10.0.0.{i}", 1267, 0.1, 0.7, now=1000)
        self.assertIsNone(profile_store.blocked_reason("10.9.9.9", 1267, 0.7, now=1001))
        profile_store.record_exit_score("10.0.0.99", 1267, 0.1, 0.7, now=1000)
        self.assertEqual(profile_store.blocked_reason("10.9.9.9", 1267, 0.7, now=1001), "asn_low_scores")
        self.assertIsNone(profile_store.blocked_reason("10.9.9.9", 3269, 0.7, now=1001))

    def test_ip_table_is_bounded(self):
        with patch.object(profile_store, "MAX_IPS", 3):
            for i in range(5):
                profile_store.record_exit_score(f"10.0.0.{i}", None, 0.9, 0.7, now=1000 + i)
        self.assertEqual(sorted(profile_store.load_blocklist()["ips"]), ["10.0.0.2", "10.0.0.3", "10.0.0.4"])

    def test_file_lives_next_to_profiles(self):
        profile_store.record_exit_score("1.2.3.4", 1, 0.9, 0.7)
        self.assertTrue(os.path.exists(os.path.join(self.root, profile_store.BLOCKLIST_FILE)))


if __name__ == "__main__":
    unittest.main()

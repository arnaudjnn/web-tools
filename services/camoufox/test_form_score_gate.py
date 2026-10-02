"""Forms-only role, the score gate for real submits, and the probe's capture.

No browser, no network, no third-party form: the oracle probes and egress
checks are faked; app.py is imported with its dependencies stubbed (shared
with test_form_stealth / test_form_recovery).
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_form_stealth as _stealth  # installs the stubs (or reuses them) and imports app
from test_form_stealth import verdict_page

app = _stealth.app
profile_store = _stealth.profile_store
score_probe = _stealth.score_probe


class _HTTPError(Exception):
    def __init__(self, status_code=None, detail=None):
        super().__init__(status_code)
        self.status_code, self.detail = status_code, detail


class _Root(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"FORM_PROFILE_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


# ── A. the forms-only role ──────────────────────────────────────────

class FormsRoleTests(unittest.TestCase):
    def test_role_is_forms_only_when_asked(self):
        with patch.dict(os.environ, {"CAMOUFOX_ROLE": "forms"}):
            self.assertEqual(app._role(), "forms")
        with patch.dict(os.environ, {"CAMOUFOX_ROLE": " Forms "}):
            self.assertEqual(app._role(), "forms")
        for value in ("all", "", "render", "anything"):
            with patch.dict(os.environ, {"CAMOUFOX_ROLE": value}):
                self.assertEqual(app._role(), "all")

    def test_forms_role_serves_forms_and_health_only(self):
        for path in ("/form-submit", "/form-inspect", "/form-score-probe", "/form-warm",
                     "/form-exit-select", "/form-exits", "/healthz"):
            self.assertTrue(app.role_allows(path, "forms"), path)
        for path in ("/render", "/screenshot", "/eval", "/bytes", "/spa-fetch", "/recycle"):
            self.assertFalse(app.role_allows(path, "forms"), path)
            self.assertTrue(app.role_allows(path, "all"), path)

    def test_refused_paths_answer_503_with_the_role(self):
        recorded = []
        request = SimpleNamespace(url=SimpleNamespace(path="/render"))
        call_next = Mock()
        with patch.object(app, "ROLE", "forms"), \
                patch.object(app, "JSONResponse", lambda **kw: recorded.append(kw) or "refused"):
            self.assertEqual(asyncio.run(app._role_gate(request, call_next)), "refused")
        call_next.assert_not_called()
        self.assertEqual(recorded[0]["status_code"], 503)
        self.assertEqual(recorded[0]["content"]["detail"]["role"], "forms")

    def test_allowed_paths_pass_through(self):
        async def call_next(_request):
            return "served"
        request = SimpleNamespace(url=SimpleNamespace(path="/form-submit"))
        with patch.object(app, "ROLE", "forms"):
            self.assertEqual(asyncio.run(app._role_gate(request, call_next)), "served")

    def test_forms_role_starts_no_render_browser_and_no_keepalive(self):
        for role, started in (("forms", 0), ("all", 2)):
            with self.subTest(role=role):
                tasks = []

                def create_task(coro):
                    tasks.append(coro)
                    coro.close()

                async def boot():
                    await app._prewarm_render()
                    await app._start_keepalive()

                with patch.object(app, "ROLE", role), patch.object(app.asyncio, "create_task", create_task), \
                        patch.object(app, "KEEPALIVE_SEC", 4.0):
                    asyncio.run(boot())
                self.assertEqual(len(tasks), started)


# ── B. the score gate ───────────────────────────────────────────────

def _summary_data(score, ip, asn):
    payload = {"success": score is not None, "score": score, "client_ip": ip}
    return {"error": None, "form_submissions": 1, "html": verdict_page(payload),
            "diagnostics": {"egress": {"country": "Italy", "asn": asn}}}


class GateDecisionTests(unittest.TestCase):
    def test_default_on_when_headed_and_unpinned(self):
        self.assertTrue(score_probe.gate_wanted(None, True, None, False))
        self.assertFalse(score_probe.gate_wanted(None, False, None, False))
        self.assertFalse(score_probe.gate_wanted(None, True, "pinned", False))
        self.assertFalse(score_probe.gate_wanted(False, True, None, False))
        self.assertTrue(score_probe.gate_wanted(True, False, "pinned", False))
        self.assertFalse(score_probe.gate_wanted(True, True, None, True))  # inspect never gates

    def test_the_form_keeps_its_budget(self):
        self.assertEqual(score_probe.form_reserve_s(360_000, True), 240.0)
        self.assertEqual(score_probe.form_reserve_s(360_000, False), 100.0)
        # Short deadlines still leave an explicit gate one candidate's worth.
        self.assertEqual(score_probe.form_reserve_s(120_000, False), 55.0)

    def test_a_default_gate_needs_room_for_the_form_and_one_candidate(self):
        self.assertFalse(score_probe.gate_fits(120_000, False))  # today's default deadline
        self.assertTrue(score_probe.gate_fits(165_000, False))
        self.assertFalse(score_probe.gate_fits(300_000, True))
        self.assertTrue(score_probe.gate_fits(360_000, True))


class ProbeCandidatesTests(_Root):
    def run_gate(self, egress, verdicts, *, tries=3, left_s=330.0, reserve_s=100.0, sessions=()):
        egress_it, verdict_it = iter(egress), iter(verdicts)
        probed = []

        async def run_egress(session, timeout_ms):
            return {"egress": next(egress_it)}

        async def run_probe(**kw):
            probed.append(kw)
            return _summary_data(*next(verdict_it))

        with patch.object(score_probe, "run_egress", run_egress), \
                patch.object(score_probe, "run_probe", run_probe):
            outcome = asyncio.run(score_probe.probe_candidates(
                oracle_url="https://tools.test/oracle/recaptcha", profile="p1", headed=True,
                threshold=0.7, max_tries=tries, end=time.monotonic() + left_s,
                reserve_s=reserve_s, sessions=sessions))
        return outcome, probed

    def test_first_exit_scoring_at_least_the_threshold_wins(self):
        outcome, probed = self.run_gate(
            [{"ip": "1.1.1.1", "asn": 1267}, {"ip": "2.2.2.2", "asn": 3269}],
            [(0.3, "1.1.1.1", 1267), (0.9, "2.2.2.2", 3269)])
        self.assertTrue(outcome["passed"])
        self.assertEqual(outcome["score"], 0.9)
        self.assertEqual(len(probed), 2)
        # Same launch config as the form: headed, the caller's profile.
        self.assertTrue(all(p["headed"] and p["profile"] == "p1" for p in probed))
        record = score_probe.gate_record(outcome)
        self.assertEqual((record["tries"], record["chosen_score"], record["asn"]), (2, 0.9, 3269))
        # Every verdict lands in the blocklist; the low one now blocks its IP.
        self.assertEqual(profile_store.blocked_reason("1.1.1.1", None, 0.7), "ip_low_score")
        self.assertIsNone(profile_store.blocked_reason("2.2.2.2", None, 0.7))

    def test_known_low_exits_are_skipped_without_a_probe(self):
        profile_store.record_exit_score("1.1.1.1", 1267, 0.2, 0.7)
        outcome, probed = self.run_gate(
            [{"ip": "1.1.1.1", "asn": 1267}, {"ip": "3.3.3.3", "asn": 3269}],
            [(0.8, "3.3.3.3", 3269)])
        self.assertTrue(outcome["passed"])
        self.assertEqual(len(probed), 1)
        self.assertEqual(outcome["tries"][0]["skipped"], "ip_low_score")

    def test_no_passing_exit_after_the_tries(self):
        outcome, probed = self.run_gate(
            [{"ip": f"9.9.9.{i}", "asn": 1} for i in range(3)],
            [(0.3, "9.9.9.0", 1), (0.5, "9.9.9.1", 1), (None, "9.9.9.2", 1)])
        self.assertFalse(outcome["passed"])
        self.assertEqual(len(probed), 3)
        self.assertEqual(score_probe.gate_record(outcome)["scores"], [0.3, 0.5, None])

    def test_no_candidate_starts_inside_the_forms_reserve(self):
        outcome, probed = self.run_gate([], [], left_s=150.0, reserve_s=100.0)
        self.assertFalse(outcome["passed"])
        self.assertEqual(probed, [])

    def test_given_sessions_are_tried_first(self):
        sessions_seen = []

        async def run_egress(session, timeout_ms):
            sessions_seen.append(session)
            return {"egress": {"ip": "4.4.4.4", "asn": 1}}

        async def run_probe(**kw):
            return _summary_data(0.9, "4.4.4.4", 1)

        with patch.object(score_probe, "run_egress", run_egress), \
                patch.object(score_probe, "run_probe", run_probe):
            outcome = asyncio.run(score_probe.probe_candidates(
                oracle_url="https://tools.test/oracle/recaptcha", profile="p", headed=True,
                threshold=0.7, max_tries=3, end=time.monotonic() + 300, sessions=["mine"]))
        self.assertEqual(sessions_seen, ["mine"])
        self.assertEqual(outcome["session"], "mine")


class FormSubmitGateTests(_Root):
    def req(self, **kw):
        base = dict(url="https://form.test/f", oracle_url="https://tools.test/oracle/recaptcha",
                    score_gate=None, score_threshold=0.7, score_gate_tries=3, exit_session=None,
                    profile=None, sticky_exit=None, headed=True, timeout_ms=360_000,
                    gate_text=None, step2=[], completion_markers=[])
        base.update(kw)
        return SimpleNamespace(**base)

    def gate(self, req, outcome, session="tok0"):
        live = Mock()
        seen = {}

        async def run_gate(**kw):
            seen.update(kw)
            return outcome

        with patch.object(score_probe, "run_gate", run_gate), patch.object(app, "HTTPException", _HTTPError):
            result = asyncio.run(app._score_gate(req, session, time.monotonic() + 360, live))
        return result, seen, live

    def test_a_passing_exit_is_returned_with_its_record(self):
        outcome = {"passed": True, "session": "good", "score": 0.9,
                   "egress": {"ip": "1.1.1.1", "asn": 3269}, "tries": [{"score": 0.9}]}
        result, seen, live = self.gate(self.req(), outcome)
        self.assertEqual(result["session"], "good")
        self.assertEqual(result["record"]["chosen_score"], 0.9)
        self.assertEqual(result["record"]["asn"], 3269)
        self.assertEqual(seen["reserve_s"], 100.0)
        self.assertEqual(seen["sessions"], [])
        live.emit.assert_not_called()

    def test_no_scoring_exit_is_a_503_retryable_zero_post(self):
        outcome = {"passed": False, "session": None, "score": None, "egress": None,
                   "tries": [{"score": 0.3}, {"skipped": "asn_low_scores"}]}
        with self.assertRaises(_HTTPError) as caught:
            self.gate(self.req(), outcome)
        detail = caught.exception.detail
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual((detail["retryable"], detail["error"], detail["form_submissions"]),
                         (True, "no_scoring_exit", 0))
        self.assertEqual(detail["score_gate"]["skipped"], ["asn_low_scores"])

    def test_a_pinned_exit_is_judged_once_never_replaced(self):
        outcome = {"passed": True, "session": "mine", "score": 0.8, "egress": {}, "tries": [{}]}
        _result, seen, _live = self.gate(self.req(exit_session="mine", score_gate=True), outcome)
        self.assertEqual((seen["sessions"], seen["tries"]), (["mine"], 1))

    def test_a_wizard_keeps_240s_for_itself(self):
        outcome = {"passed": True, "session": "x", "score": 0.9, "egress": {}, "tries": [{}]}
        _result, seen, _live = self.gate(self.req(gate_text="continua"), outcome)
        self.assertEqual(seen["reserve_s"], 240.0)

    def test_explicit_gate_without_oracle_is_a_400_implicit_is_skipped(self):
        with self.assertRaises(_HTTPError) as caught:
            self.gate(self.req(oracle_url=None, score_gate=True), {})
        self.assertEqual(caught.exception.status_code, 400)
        result, _seen, _live = self.gate(self.req(oracle_url=None), {})
        self.assertEqual(result, {"session": "tok0", "record": {"skipped": "no_oracle"}})

    def test_an_implicit_gate_on_a_short_deadline_runs_ungated(self):
        result, seen, _live = self.gate(self.req(timeout_ms=120_000), {})
        self.assertEqual(result, {"session": "tok0", "record": {"skipped": "timeout_too_short"}})
        self.assertEqual(seen, {})
        # An explicit gate still probes, with a squeezed reserve.
        outcome = {"passed": True, "session": "x", "score": 0.9, "egress": {}, "tries": [{}]}
        _result, seen, _live = self.gate(self.req(timeout_ms=120_000, score_gate=True), outcome)
        self.assertEqual(seen["reserve_s"], 55.0)

    def test_a_sticky_profile_tries_its_own_exit_first_and_repins(self):
        with patch.dict(os.environ, {"FORM_PROFILE_STICKY_EXIT": "1"}):
            outcome = {"passed": True, "session": "new", "score": 0.9,
                       "egress": {"ip": "5.5.5.5"}, "tries": [{}, {}]}
            _result, seen, _live = self.gate(self.req(profile="bat"), outcome, session="old")
        self.assertEqual(seen["sessions"], ["old"])
        self.assertEqual(profile_store.stored_exit("bat"), "new")

    def test_zero_post_retry_list_and_timeout_cap(self):
        self.assertTrue(app._retryable_zero_post({"error": "captcha_unavailable", "form_submissions": 0}))
        # The gate's own failure never reaches the target: it is raised as a
        # 503 before any form job exists (see test above).


# ── C. the probe reads its verdict from the wire ────────────────────

class CaptureTests(unittest.TestCase):
    def test_summary_falls_back_to_the_submission_body(self):
        data = {"error": None, "form_submissions": 1, "html": "<html>still the form</html>",
                "submission_body": verdict_page({"success": True, "score": 0.8, "client_ip": "1.2.3.4"}),
                "diagnostics": {}}
        summary = score_probe.summarize_probe(data, session="s", profile=None, headed=True,
                                              threshold=0.7, started=0.0)
        self.assertEqual(summary["score"], 0.8)
        self.assertTrue(summary["passed"])
        self.assertNotIn("submission_body", json.dumps(summary))

    def flow(self, finish, **params):
        import test_form_flow as _flow
        from form_flow import run_form
        case = _flow.FormTests("test_one_post_and_actual_response_status")
        case.setUp()
        body = verdict_page({"success": True, "score": 0.9})
        case.request.response = lambda: SimpleNamespace(text=lambda: body)
        original = case.submit

        def submit(**kwargs):
            original(**kwargs)
            if finish:
                handlers = {c.args[0]: c.args[1] for c in case.page.on.call_args_list}
                handlers["requestfinished"](case.request)

        case.page.locator.return_value.click.side_effect = submit
        with patch("form_flow.SUBMISSION_BODY_WAIT_S", 0.5):
            return run_form(case.context, **case.params, **params), body

    def test_run_form_reads_the_finished_submission_body(self):
        result, body = self.flow(True, capture_submission_body=True)
        self.assertEqual(result["submission_body"], body)

    def test_unfinished_body_is_never_read_and_real_forms_never_capture(self):
        result, _ = self.flow(False, capture_submission_body=True)
        self.assertNotIn("submission_body", result)  # waited (bounded), never blocked on a read
        result, _ = self.flow(True)
        self.assertNotIn("submission_body", result)

    def test_probe_waits_30s_and_asks_for_the_body(self):
        params = score_probe.probe_params("https://t/oracle/recaptcha", "https://t/oracle/recaptcha/verify", 2, 4000)
        self.assertEqual(params["settle_ms"], 30000)
        self.assertTrue(params["capture_submission_body"])


if __name__ == "__main__":
    unittest.main()

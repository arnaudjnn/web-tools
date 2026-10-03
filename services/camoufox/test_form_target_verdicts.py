"""Target verdicts: recording what a form host said to each exit ASN, and
the score gate ranking/skipping on it (target_verdicts.py).

No browser, no network, no third-party form: the oracle probes and egress
checks are faked, app.py is imported with its dependencies stubbed (shared
with test_form_stealth), and every host below is a reserved .test name.
"""
import asyncio
import importlib
import json
import os
import random
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_form_stealth as _stealth  # installs the stubs (or reuses them) and imports app
from test_form_stealth import verdict_page

import target_verdicts

app = _stealth.app
profile_store = _stealth.profile_store
score_probe = _stealth.score_probe

FORM = "https://www.form.test/register/"


def answered(*, ok=False, posts=1, status=200, captcha=True, error=None, asn=3269, ip="5.5.5.5"):
    """One run's result as run_form shapes it (a plain one-POST form)."""
    diagnostics = {"egress": {"country": "Italy", "asn": asn, "ip": ip}}
    if not ok:
        diagnostics["rejection"] = {"at_post": 1, "errors": 1, "captcha": captcha,
                                    "same_url": True, "wizard": False}
    return {"ok": ok, "error": error, "form_submissions": posts, "status": status,
            "url": FORM, "html": "", "diagnostics": diagnostics}


class _Rng:
    """random() answers from a script (then 0.99: never explore)."""

    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0) if self.values else 0.99


NEVER = _Rng()


class _Root(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"FORM_PROFILE_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def seed(self, asn, accepted=0, rejected=0, other=0, host=FORM):
        for verdict, n in (("accepted", accepted), ("captcha_rejected", rejected), ("other", other)):
            for _ in range(n):
                target_verdicts.record(host, asn, "9.9.9.9", verdict)


# ── keys and verdicts ───────────────────────────────────────────────

class HostAndVerdictTests(unittest.TestCase):
    def test_registrable_host(self):
        cases = {
            "https://www.form.test/register/?x=1": "form.test",
            "https://a.b.form.test:8443/f": "form.test",
            "FORM.TEST.": "form.test",
            "https://shop.example.co.uk/f": "example.co.uk",
            "https://x.comune.gov.it/f": "comune.gov.it",
            "http://127.0.0.1:8080/f": "127.0.0.1",
            "": None, None: None,
        }
        for url, host in cases.items():
            with self.subTest(url=url):
                self.assertEqual(target_verdicts.registrable_host(url), host)

    def test_classify(self):
        self.assertEqual(target_verdicts.classify(answered(ok=True, status=302)), "accepted")
        self.assertEqual(target_verdicts.classify(answered()), "captcha_rejected")
        # A field error next to the form, not the CAPTCHA: not the exit's verdict.
        self.assertEqual(target_verdicts.classify(answered(captcha=False)), "other")
        self.assertEqual(target_verdicts.classify(answered(error="outcome_unknown", status=0)), "other")
        # Zero POSTs: the target said nothing.
        self.assertIsNone(target_verdicts.classify(answered(posts=0)))
        self.assertIsNone(target_verdicts.classify({}))


# ── recording ───────────────────────────────────────────────────────

class RecordingTests(_Root):
    def test_attempt_is_recorded_with_its_exit(self):
        event = target_verdicts.record_attempt(FORM, answered(ok=True), now=1000)
        self.assertEqual(event, {"asn": "3269", "ip": "5.5.5.5", "verdict": "accepted", "ts": 1000})
        target_verdicts.record_attempt(FORM, answered())
        target_verdicts.record_attempt(FORM, answered(captcha=False))
        target_verdicts.record_attempt(FORM, answered(posts=0))  # not a verdict
        self.assertEqual(target_verdicts.host_stats("form.test"),
                         {"3269": {"accepted": 1, "rejected": 1, "other": 1, "rate": 0.5}})

    def test_exit_falls_back_to_the_gate_record(self):
        data = answered(ok=True)
        data["diagnostics"]["egress"] = None
        event = target_verdicts.record_attempt(FORM, data, {"asn": 1267, "gate_ip": "7.7.7.7"})
        self.assertEqual((event["asn"], event["ip"]), ("1267", "7.7.7.7"))

    def test_store_lives_next_to_the_blocklist_and_never_raises(self):
        target_verdicts.record_attempt(FORM, answered(ok=True))
        path = os.path.join(self._tmp.name, target_verdicts.VERDICTS_FILE)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.listdir(self._tmp.name), [target_verdicts.VERDICTS_FILE])  # no tmp left
        with patch.object(profile_store, "_write_json", side_effect=OSError("disk full")):
            self.assertIsNone(target_verdicts.record_attempt(FORM, answered(ok=True)))

    def test_persists_across_a_restart(self):
        self.seed(3269, accepted=2, rejected=1)
        importlib.reload(target_verdicts)  # a fresh process: no in-memory state
        self.assertEqual(target_verdicts.host_stats(FORM)["3269"]["accepted"], 2)
        self.assertEqual(target_verdicts.host_stats(FORM)["3269"]["rejected"], 1)

    def test_corrupt_file_reads_as_empty(self):
        with open(os.path.join(self._tmp.name, target_verdicts.VERDICTS_FILE), "w") as handle:
            handle.write("{nope")
        self.assertEqual(target_verdicts.all_stats(), {})
        target_verdicts.record(FORM, 1, None, "accepted")
        self.assertEqual(target_verdicts.all_stats()["form.test"]["1"]["accepted"], 1)

    def test_size_is_bounded(self):
        with patch.object(target_verdicts, "MAX_EVENTS_PER_HOST", 5), \
                patch.object(target_verdicts, "MAX_HOSTS", 3):
            for i in range(8):
                target_verdicts.record(FORM, 1, None, "captcha_rejected" if i < 4 else "accepted", now=i)
            # The last 5 only: the record follows a host that changed its mind.
            self.assertEqual(target_verdicts.host_stats(FORM)["1"],
                             {"accepted": 4, "rejected": 1, "other": 0, "rate": 0.8})
            for i, host in enumerate(("a.test", "b.test", "c.test")):
                target_verdicts.record(host, 1, None, "accepted", now=100 + i)
            # form.test was the least recently updated: dropped.
            self.assertEqual(sorted(target_verdicts.load()["hosts"]), ["a.test", "b.test", "c.test"])

    def test_unknown_asn_and_bad_input(self):
        self.assertIsNone(target_verdicts.record(None, 1, None, "accepted"))
        self.assertIsNone(target_verdicts.record(FORM, 1, None, "maybe"))
        target_verdicts.record(FORM, None, None, "accepted")
        self.assertIn("unknown", target_verdicts.host_stats(FORM))


# ── the rule ────────────────────────────────────────────────────────

class RuleTests(_Root):
    def test_skip_rule_needs_six_decisive_verdicts_at_most_10_percent(self):
        self.seed(1, rejected=5)  # five refusals ban nothing
        self.assertIsNone(target_verdicts.assess(FORM, 1, rng=NEVER)["skip"])
        self.seed(1, rejected=1)
        self.assertEqual(target_verdicts.assess(FORM, 1, rng=NEVER)["skip"], "target_asn_rejected")
        self.seed(2, accepted=1, rejected=9)  # 10%: still skipped
        self.assertEqual(target_verdicts.assess(FORM, 2, rng=NEVER)["skip"], "target_asn_rejected")
        self.seed(3, accepted=1, rejected=4)  # 20%: ranked low, never skipped
        self.assertIsNone(target_verdicts.assess(FORM, 3, rng=NEVER)["skip"])
        # "other" verdicts (field errors...) never count against an ASN.
        self.seed(4, other=10)
        self.assertIsNone(target_verdicts.assess(FORM, 4, rng=NEVER)["skip"])
        # Per host: the same ASN is untouched elsewhere.
        self.assertIsNone(target_verdicts.assess("https://other.test/", 1, rng=NEVER)["skip"])

    def test_ranking_and_preferred(self):
        self.seed(10, accepted=4)
        self.seed(20, accepted=1, rejected=1)
        self.seed(30, accepted=1, rejected=2)
        good, mixed, poor, new = (target_verdicts.assess(FORM, a, rng=NEVER) for a in (10, 20, 30, 40))
        self.assertGreater(good["rank"], new["rank"])
        self.assertEqual(new["rank"], mixed["rank"])  # 0.5 both: no evidence either way
        self.assertGreater(new["rank"], poor["rank"])
        self.assertTrue(good["preferred"])
        self.assertFalse(new["preferred"] or mixed["preferred"] or poor["preferred"])
        cands = [{"assessment": a} for a in (poor, new, good)]
        self.assertEqual(target_verdicts.pick(cands, rng=NEVER), 2)

    def test_exploration(self):
        self.seed(1, rejected=6)
        explored = target_verdicts.assess(FORM, 1, rng=_Rng(0.05))
        self.assertIsNone(explored["skip"])
        self.assertTrue(explored["explored"])
        self.assertLess(explored["rank"], 0)  # let through, ranked last
        self.seed(10, accepted=5)
        cands = [{"assessment": target_verdicts.assess(FORM, 10, rng=NEVER)},
                 {"assessment": target_verdicts.assess(FORM, 99, rng=NEVER)}]
        # An exploring pick takes the least-tried candidate over the best.
        self.assertEqual(target_verdicts.pick(cands, rng=_Rng(0.05)), 1)
        self.assertEqual(target_verdicts.pick(cands, rng=NEVER), 0)
        # The rate is small: over many draws, about EXPLORE_RATE.
        rng = random.Random(7)
        lets = sum(target_verdicts.assess(FORM, 1, rng=rng)["skip"] is None for _ in range(2000))
        self.assertTrue(0.06 < lets / 2000 < 0.14, lets)


# ── the gate ────────────────────────────────────────────────────────

def _summary_data(score, ip, asn):
    payload = {"success": score is not None, "score": score, "client_ip": ip}
    return {"error": None, "form_submissions": 1, "html": verdict_page(payload),
            "diagnostics": {"egress": {"country": "Italy", "asn": asn}}}


class GateTests(_Root):
    def run_gate(self, egress, scores, *, tries=3, left_s=400.0, sessions=(), target_url=FORM, rng=NEVER):
        egress_it = iter(egress)
        by_session, prechecked, probed = {}, [], []

        async def run_egress(session, timeout_ms):
            e = next(egress_it)
            by_session[session] = e
            prechecked.append(e["asn"])
            return {"egress": e}

        async def run_probe(**kw):
            e = by_session[kw["session"]]
            probed.append(e["asn"])
            return _summary_data(scores.get(e["asn"], 0.9), e["ip"], e["asn"])

        with patch.object(score_probe, "run_egress", run_egress), \
                patch.object(score_probe, "run_probe", run_probe):
            outcome = asyncio.run(score_probe.probe_candidates(
                oracle_url="https://tools.test/oracle/recaptcha", profile=None, headed=True,
                threshold=0.7, max_tries=tries, end=time.monotonic() + left_s, reserve_s=100.0,
                sessions=sessions, target_url=target_url, rng=rng))
        return outcome, prechecked, probed

    def test_a_clearly_rejected_asn_is_skipped(self):
        self.seed(1267, rejected=6)
        outcome, prechecked, probed = self.run_gate(
            [{"ip": "1.1.1.1", "asn": 1267}, {"ip": "2.2.2.2", "asn": 3269},
             {"ip": "3.3.3.3", "asn": 6762}], {})
        self.assertTrue(outcome["passed"])
        self.assertEqual(prechecked, [1267, 3269, 6762])
        self.assertNotIn(1267, probed)
        self.assertEqual(outcome["tries"][0]["skipped"], "target_asn_rejected")
        self.assertIn("target_asn_rejected", score_probe.gate_record(outcome)["skipped"])

    def test_the_better_target_record_is_probed_first(self):
        self.seed(1267, accepted=1, rejected=2)  # poor (33%), not skipped
        self.seed(3269, accepted=1)               # one acceptance, not yet preferred
        outcome, prechecked, probed = self.run_gate(
            [{"ip": "1.1.1.1", "asn": 1267}, {"ip": "2.2.2.2", "asn": 3269}], {})
        self.assertEqual(prechecked, [1267, 3269])  # looked ahead once
        self.assertEqual(probed, [3269])
        self.assertEqual(outcome["egress"]["asn"], 3269)
        self.assertEqual(outcome["tries"][0]["target"], {"rate": 1.0, "decisive": 1, "explored": False})

    def test_a_preferred_asn_is_probed_at_once(self):
        self.seed(3269, accepted=3)
        _outcome, prechecked, probed = self.run_gate(
            [{"ip": "2.2.2.2", "asn": 3269}, {"ip": "1.1.1.1", "asn": 1267}], {})
        self.assertEqual((prechecked, probed), ([3269], [3269]))

    def test_the_oracle_stays_the_minimum_bar(self):
        self.seed(3269, accepted=5)
        outcome, _prechecked, probed = self.run_gate(
            [{"ip": "2.2.2.2", "asn": 3269}, {"ip": "1.1.1.1", "asn": 1267},
             {"ip": "3.3.3.3", "asn": 6762}], {3269: 0.3})
        self.assertEqual(probed, [3269, 1267])  # best record first, but it scored 0.3
        self.assertEqual(outcome["egress"]["asn"], 1267)

    def test_no_record_keeps_the_old_one_at_a_time_gate(self):
        _outcome, prechecked, probed = self.run_gate(
            [{"ip": "1.1.1.1", "asn": 1}, {"ip": "2.2.2.2", "asn": 2}], {1: 0.9})
        self.assertEqual((prechecked, probed), ([1], [1]))
        _outcome, prechecked, probed = self.run_gate(
            [{"ip": "1.1.1.1", "asn": 1}], {}, target_url=None)
        self.assertEqual((prechecked, probed), ([1], [1]))

    def test_a_given_session_is_never_skipped_on_the_target_record(self):
        self.seed(1267, rejected=5)
        outcome, prechecked, probed = self.run_gate([{"ip": "1.1.1.1", "asn": 1267}], {},
                                                    sessions=["mine"])
        self.assertEqual((prechecked, probed), ([1267], [1267]))
        self.assertEqual(outcome["session"], "mine")

    def test_exploration_lets_a_rejected_asn_through(self):
        self.seed(1267, rejected=6)
        outcome, _prechecked, probed = self.run_gate([{"ip": "1.1.1.1", "asn": 1267}], {},
                                                     tries=1, rng=_Rng(0.05))
        self.assertEqual(probed, [1267])
        self.assertTrue(outcome["tries"][0]["target"]["explored"])

    def test_lookahead_stays_inside_the_budget_and_the_tries(self):
        self.seed(1, accepted=1, rejected=1)
        # Only one candidate fits after the form's reserve: no look-ahead.
        _outcome, prechecked, _probed = self.run_gate(
            [{"ip": "1.1.1.1", "asn": 1}, {"ip": "2.2.2.2", "asn": 2}], {}, left_s=170.0)
        self.assertEqual(prechecked, [1])
        # max_tries bounds the pre-checks, look-ahead included.
        _outcome, prechecked, probed = self.run_gate(
            [{"ip": f"3.3.3.{i}", "asn": 1} for i in range(5)], {1: 0.2}, tries=3)
        self.assertEqual(len(prechecked), 3)
        self.assertEqual(len(probed), 3)


# ── the hooks ───────────────────────────────────────────────────────

class HookTests(_Root):
    def req(self, **kw):
        base = dict(url=FORM, exit_session=None, profile=None, sticky_exit=None,
                    ready_expression=None, headed=True, score_gate=None, inspect_only=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def attempt(self, data, gate_record=None):
        async def score_gate(req, session, deadline, live, recheck=False):
            return {"session": "gated", "record": gate_record or {"passed": True, "asn": 1}}

        async def form_run(*args):
            return data

        with patch.object(app, "_score_gate", score_gate), patch.object(app, "_form_run", form_run):
            return asyncio.run(app._form_attempt_run(self.req(), time.monotonic() + 300, "tok", Mock()))

    def test_every_posted_attempt_is_recorded(self):
        self.attempt(answered(ok=True, asn=3269))
        self.attempt(answered(asn=1267))
        self.assertEqual(target_verdicts.all_stats(), {"form.test": {
            "3269": {"accepted": 1, "rejected": 0, "other": 0, "rate": 1.0},
            "1267": {"accepted": 0, "rejected": 1, "other": 0, "rate": 0.0}}})

    def test_the_gate_is_handed_the_form_url(self):
        seen = {}

        async def run_gate(**kw):
            seen.update(kw)
            return {"passed": True, "session": "x", "score": 0.9, "egress": {}, "tries": [{}]}

        req = SimpleNamespace(url=FORM, oracle_url="https://tools.test/oracle/recaptcha", score_gate=True,
                              score_threshold=0.7, score_gate_tries=3, exit_session=None, profile=None,
                              sticky_exit=None, headed=True, timeout_ms=360_000, gate_text=None,
                              step2=[], completion_markers=[])
        with patch.object(score_probe, "run_gate", run_gate):
            asyncio.run(app._score_gate(req, "tok", time.monotonic() + 360, Mock()))
        self.assertEqual(seen["target_url"], FORM)

    def test_form_exits_exposes_per_host_stats(self):
        self.seed(3269, accepted=2, rejected=1)
        out = asyncio.run(score_probe.form_exits())
        self.assertEqual(out["targets"], {"form.test": {
            "3269": {"accepted": 2, "rejected": 1, "other": 0, "rate": 0.667}}})
        json.dumps(out)  # serialisable as-is

    def test_the_module_ships_in_the_image(self):
        with open(os.path.join(os.path.dirname(__file__), "Dockerfile")) as handle:
            self.assertIn("target_verdicts.py", handle.read())


if __name__ == "__main__":
    unittest.main()

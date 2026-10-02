import asyncio
import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

import form_worker
from form_worker import FormRetryable, FormWorker, run_isolated_form


class IsolatedFormTests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock()
        self.manager.__enter__ = Mock(return_value=Mock())
        self.manager.__exit__ = Mock()
        self.factory = Mock(return_value=self.manager)
        self.params = dict(url="https://example.test/form", fields=[], submit="button")

    def run_form(self, **overrides):
        return run_isolated_form(self.factory, deadline=time.monotonic() + 5,
                                 **(self.params | overrides))

    @patch("form_worker.time.sleep")
    @patch("form_worker.run_form")
    def test_launch_failure_has_zero_submissions_and_no_replay(self, execute, _sleep):
        # Every attempt fails: structured, zero submissions, no execute — a
        # retry cannot turn a launch failure into a submission.
        self.manager.__enter__.side_effect = RuntimeError("private proxy credential")
        result = self.run_form()
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "browser_launch_failed")
        self.assertNotIn("credential", str(result))
        self.assertEqual(self.factory.call_count, form_worker.LAUNCH_ATTEMPTS)
        self.assertEqual(self.manager.__exit__.call_count, form_worker.LAUNCH_ATTEMPTS)
        execute.assert_not_called()

    @patch("form_worker.time.sleep")
    @patch("form_worker.run_form")
    def test_profile_refusing_two_launches_recovers_on_the_third(self, execute, sleep):
        # 2026-10-02 15:43:43/45: a persistent profile refused
        # launch_persistent_context twice (TargetClosedError, 2s apart); the
        # next job's launch 6s later worked. The third attempt waits longer.
        class TargetClosedError(Exception):
            pass
        execute.return_value = {"ok": True, "form_submissions": 1}
        self.manager.__enter__.side_effect = [TargetClosedError("closed"), TargetClosedError("closed"),
                                              self.manager.__enter__.return_value]
        live = __import__("form_flow").FormLive()
        result = run_isolated_form(self.factory, deadline=time.monotonic() + 30, live=live, **self.params)
        self.assertEqual(result, execute.return_value)
        self.assertEqual(self.factory.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(form_worker.LAUNCH_BACKOFF_S))
        self.assertEqual(live.extra["launch_errors"], ["TargetClosedError", "TargetClosedError"])
        execute.assert_called_once()

    @patch("form_worker.time.sleep")
    @patch("form_worker.run_form")
    def test_transient_launch_failure_recovers_on_the_single_retry(self, execute, _sleep):
        # The first launch of a fresh process fails (tunnel coming up); the
        # retry lands and the form runs — no caller attempt burned.
        execute.return_value = {"ok": True, "form_submissions": 1}
        self.manager.__enter__.side_effect = [RuntimeError("tunnel up yet"), self.manager.__enter__.return_value]
        result = self.run_form()
        self.assertEqual(result, execute.return_value)
        self.assertEqual(self.factory.call_count, 2)
        execute.assert_called_once()

    @patch("form_worker.run_form")
    def test_context_failure_has_zero_submissions(self, execute):
        self.manager.__enter__.return_value.new_context.side_effect = RuntimeError()
        self.assertEqual(self.run_form()["error"], "browser_context_failed")
        self.manager.__exit__.assert_called_once()
        execute.assert_not_called()

    @patch("form_worker.run_form")
    def test_persistent_profile_context_is_managed_by_the_manager(self, execute):
        # A persistent profile's __enter__ IS the context (no new_context):
        # run_form receives it, and only the manager closes it — closing the
        # same object as an "owned" context would burn the warm profile (and
        # log a spurious cleanup failure on the double close).
        execute.return_value = {"ok": True, "form_submissions": 1}
        persistent = Mock(spec=["close", "on", "route"])
        persistent.close = Mock()
        self.manager.__enter__.return_value = persistent
        self.assertEqual(self.run_form(), execute.return_value)
        self.assertIs(execute.call_args.args[0], persistent)
        persistent.close.assert_not_called()
        self.manager.__exit__.assert_called_once()

    @patch("form_worker.run_form")
    def test_cleanup_failure_preserves_submission_evidence(self, execute):
        execute.return_value = {"ok": True, "form_submissions": 1}
        self.manager.__exit__.side_effect = RuntimeError()
        context = self.manager.__enter__.return_value.new_context.return_value
        context.close.side_effect = RuntimeError()
        self.assertEqual(self.run_form(), execute.return_value)
        execute.assert_called_once()
        self.manager.__exit__.assert_called_once()

    @patch("form_worker.run_form", side_effect=RuntimeError())
    def test_unexpected_execution_error_stays_unknown(self, execute):
        with self.assertRaises(RuntimeError):
            self.run_form()
        execute.assert_called_once()
        self.manager.__exit__.assert_called_once()

    @patch("form_worker.run_form")
    def test_launch_consumes_navigation_budget(self, execute):
        with patch("form_worker.time.monotonic", side_effect=[0, 0, 6]):
            result = self.run_form()
        self.assertEqual(result["error"], "deadline_before_navigation")
        self.manager.__exit__.assert_called_once()
        execute.assert_not_called()

    @patch("form_worker.time.sleep")
    def test_launch_failure_emits_one_summary_without_the_message(self, _sleep):
        import form_flow
        self.manager.__enter__.side_effect = RuntimeError("private proxy credential")
        live = form_flow.FormLive(profile="p1", headed=True, camoufox="x/y")
        with self.assertLogs("camoufox.forms", level="INFO") as logs:
            run_isolated_form(self.factory, deadline=time.monotonic() + 5, live=live, **self.params)
        runs = [r.getMessage() for r in logs.records if r.getMessage().startswith("form-run ")]
        self.assertEqual(len(runs), 1)
        summary = json.loads(runs[0][len("form-run "):])
        self.assertEqual(summary["error"], "browser_launch_failed")
        self.assertEqual(summary["launch_attempts"], form_worker.LAUNCH_ATTEMPTS)
        self.assertEqual(summary["launch_errors"], ["RuntimeError"] * form_worker.LAUNCH_ATTEMPTS)
        self.assertEqual(summary["profile"], "p1")
        self.assertIn("launch", summary["durations_s"])
        self.assertIn("teardown", summary["durations_s"])
        self.assertNotIn("credential", runs[0])

    @patch("form_worker.run_form", side_effect=RuntimeError("private"))
    def test_escaping_exception_still_emits_a_summary(self, _execute):
        with self.assertLogs("camoufox.forms", level="INFO") as logs:
            with self.assertRaises(RuntimeError):
                self.run_form()
        runs = [r.getMessage() for r in logs.records if r.getMessage().startswith("form-run ")]
        self.assertEqual(len(runs), 1)
        self.assertIn('"failure_class":"RuntimeError"', runs[0])
        self.assertNotIn("private", runs[0])

    def test_expired_operation_does_not_launch(self):
        result = run_isolated_form(self.factory, deadline=time.monotonic() - 1, **self.params)
        self.assertEqual(result["form_submissions"], 0)
        self.factory.assert_not_called()


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_use_fresh_threads_even_after_failure(self):
        worker = FormWorker()
        threads = []
        def failed():
            threads.append(threading.current_thread())
            raise RuntimeError()
        def succeeded():
            threads.append(threading.current_thread())
            return "ok"
        with self.assertRaises(RuntimeError):
            await worker.run(failed, url="https://example.test", deadline=time.monotonic() + 5)
        self.assertEqual(await worker.run(succeeded, url="https://example.test",
                                          deadline=time.monotonic() + 5), "ok")
        self.assertIsNot(threads[0], threads[1])

    async def test_cancelled_caller_does_not_release_running_worker(self):
        worker = FormWorker()
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            release.wait(5)
        first = asyncio.create_task(worker.run(blocked, url="https://example.test",
                                                deadline=time.monotonic() + 5))
        try:
            await asyncio.to_thread(started.wait, 2)
            self.assertTrue(started.is_set())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = Mock()
            result = await worker.run(second, url="https://example.test", deadline=time.monotonic() + .02)
            self.assertEqual(result["error"], "queue_deadline_exceeded")
            self.assertEqual(result["form_submissions"], 0)
            second.assert_not_called()
        finally:
            release.set()
        self.assertEqual(await worker.run(lambda: "recovered", url="https://example.test",
                                          deadline=time.monotonic() + 5), "recovered")

    async def test_wedged_job_releases_admission_and_fails_unknown(self):
        worker = FormWorker()
        stuck = threading.Event()
        def wedged():
            stuck.wait(5)
            return "late"
        try:
            with patch("form_worker.TEARDOWN_GRACE_S", 0.05):
                with self.assertRaises(TimeoutError):
                    await worker.run(wedged, url="https://example.test",
                                     deadline=time.monotonic() + 0.01)
            # The orphan must not hold the gate: the next form runs at once.
            self.assertEqual(await worker.run(lambda: "next", url="https://example.test",
                                              deadline=time.monotonic() + 5), "next")
        finally:
            stuck.set()
        # The orphan finishing later must not release a second time.
        await asyncio.sleep(0.05)
        self.assertEqual(await worker.run(lambda: "after", url="https://example.test",
                                          deadline=time.monotonic() + 5), "after")

    async def test_wedge_schedules_shed_when_configured(self):
        # FORM_WEDGE_EXIT_S > 0 must schedule os._exit(1) shortly after the
        # wedge is reported — but only then: the suite leaves it unset so a
        # wedge can never take the test process down.
        worker = FormWorker()
        stuck = threading.Event()
        def wedged():
            stuck.wait(5)
            return "late"
        loop = asyncio.get_running_loop()
        fired = []
        with patch.dict(os.environ, {"FORM_WEDGE_EXIT_S": "0.01"}):
            with patch("form_worker.os._exit", lambda code: fired.append(code)):
                try:
                    with patch("form_worker.TEARDOWN_GRACE_S", 0.05):
                        with self.assertRaises(TimeoutError):
                            await worker.run(wedged, url="https://example.test",
                                             deadline=time.monotonic() + 0.01)
                    await asyncio.sleep(0.05)  # let call_later fire
                finally:
                    stuck.set()
        self.assertEqual(fired, [1])

    async def test_pre_submit_park_is_retryable_and_sheds(self):
        # A park on ANY marked pre-POST driver call (form_flow's marker —
        # fields, dismiss, arrival — the observed class) must surface as
        # FormRetryable within one poll — long before deadline + grace could
        # fold it into the unknown-outcome 502 — and still shed, because the
        # orphan's browser can never be closed either.
        import form_flow
        worker = FormWorker()
        stuck = threading.Event()
        live = form_flow.FormLive()

        def parked():
            live.mark(form_flow.PRE_SUBMIT_STEP)
            name, _, threshold = live._step
            live._step = (name, form_flow._clock() - 99, threshold)  # parked long ago
            stuck.wait(5)
            return "late"

        fired = []
        try:
            with patch.dict(os.environ, {"FORM_WEDGE_EXIT_S": "0.01"}), \
                 patch("form_worker.os._exit", lambda code: fired.append(code)), \
                 patch("form_worker.TEARDOWN_GRACE_S", 30):
                with self.assertLogs("camoufox.forms", level="INFO") as logs:
                    with self.assertRaises(FormRetryable):
                        await worker.run(parked, url="https://example.test",
                                         deadline=time.monotonic() + 30, live=live)
            await asyncio.sleep(0.05)  # let the shed's call_later fire
        finally:
            stuck.set()
        self.assertEqual(fired, [1])
        # The park is the job's verdict: one summary line, from the worker.
        runs = [r.getMessage() for r in logs.records if r.getMessage().startswith("form-run ")]
        self.assertEqual(len(runs), 1)
        summary = json.loads(runs[0][len("form-run "):])
        self.assertEqual(summary["error"], "parked")
        self.assertEqual(summary["parked_step"], form_flow.PRE_SUBMIT_STEP)

    async def test_a_previous_jobs_mark_never_parks_the_next_job(self):
        # The old module-global marker had to be reset before every job; a
        # per-job object cannot leak at all.
        import form_flow
        stale = form_flow.FormLive()
        stale.mark("pointer move")
        name, _, threshold = stale._step
        stale._step = (name, form_flow._clock() - 99, threshold)
        worker = FormWorker()
        self.assertEqual(await worker.run(lambda: "fine", url="https://example.test",
                                          deadline=time.monotonic() + 5,
                                          live=form_flow.FormLive()), "fine")

    async def test_queue_timeout_emits_the_summary(self):
        import form_flow
        worker = FormWorker()
        with self.assertLogs("camoufox.forms", level="INFO") as logs:
            result = await worker.run(Mock(), url="https://example.test",
                                      deadline=time.monotonic() - 1, live=form_flow.FormLive())
        self.assertEqual(result["error"], "queue_deadline_exceeded")
        self.assertTrue(any(r.getMessage().startswith("form-run ") and
                            "queue_deadline_exceeded" in r.getMessage() for r in logs.records))


if __name__ == "__main__":
    unittest.main()

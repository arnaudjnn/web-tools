import asyncio
import threading
import time
import unittest
from unittest.mock import Mock, patch

from form_worker import FormWorker, run_isolated_form


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

    @patch("form_worker.run_form")
    def test_launch_failure_has_zero_submissions_and_no_replay(self, execute):
        self.manager.__enter__.side_effect = RuntimeError("private proxy credential")
        result = self.run_form()
        self.assertEqual(result["form_submissions"], 0)
        self.assertEqual(result["error"], "browser_launch_failed")
        self.assertNotIn("credential", str(result))
        self.factory.assert_called_once()
        self.manager.__exit__.assert_called_once()
        execute.assert_not_called()

    @patch("form_worker.run_form")
    def test_context_failure_has_zero_submissions(self, execute):
        self.manager.__enter__.return_value.new_context.side_effect = RuntimeError()
        self.assertEqual(self.run_form()["error"], "browser_context_failed")
        self.manager.__exit__.assert_called_once()
        execute.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()

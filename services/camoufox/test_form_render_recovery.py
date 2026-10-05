"""Render-browser recovery: the dead browser is closed on its own thread
(never leaked), and repeated recoveries shed the process for a clean restart."""
import asyncio
import unittest
from unittest.mock import Mock, patch

from test_form_headed import RECORDED, app  # noqa: F401  (installs the stubs, imports app)


class RenderRecoveryTests(unittest.TestCase):
    def setUp(self):
        app._render_recoveries.clear()
        self.addCleanup(app._render_recoveries.clear)

    def run_render(self, fn):
        async def go():
            return await app._run_render(fn)
        return asyncio.run(go())

    def test_a_dead_browser_is_closed_before_the_thread_swap(self):
        dead = Mock(name="dead_cm")
        app._render_cm, app._render_browser = dead, Mock()
        calls = {"n": 0}

        def job():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Target page, context or browser has been closed")
            return "ok"

        closed = []
        with patch.object(app, "_close_cm", lambda cm, label: closed.append(cm)):
            self.assertEqual(self.run_render(job), "ok")
        self.assertEqual(closed, [dead])          # closed, not leaked
        self.assertIsNone(app._render_cm)

    def test_other_errors_are_not_recovered(self):
        def job():
            raise ValueError("bad selector")
        with self.assertRaises(ValueError):
            self.run_render(job)
        self.assertEqual(app._render_recoveries, [])

    def test_repeated_recoveries_shed_the_process(self):
        with patch.object(app, "RENDER_RECOVERY_LIMIT", 3):
            self.assertFalse(app._note_render_recovery())
            self.assertFalse(app._note_render_recovery())
            self.assertTrue(app._note_render_recovery())

    def test_recoveries_outside_the_window_do_not_count(self):
        with patch.object(app, "RENDER_RECOVERY_LIMIT", 2), \
                patch.object(app, "RENDER_RECOVERY_WINDOW_S", 600), \
                patch.object(app.time, "monotonic", side_effect=[0.0, 1000.0]):
            self.assertFalse(app._note_render_recovery())
            self.assertFalse(app._note_render_recovery())   # the first one aged out

    def test_the_shed_is_scheduled_past_the_limit(self):
        app._render_cm = None
        calls = {"n": 0}

        def job():
            calls["n"] += 1
            if calls["n"] % 2 == 1:
                raise RuntimeError("Browser.new_page: Target closed")
            return "ok"

        scheduled = []

        async def go():
            loop = asyncio.get_running_loop()
            with patch.object(app, "RENDER_RECOVERY_LIMIT", 1), \
                    patch.object(loop, "call_later", lambda delay, fn, *a: scheduled.append((delay, a))):
                return await app._run_render(job)

        self.assertEqual(asyncio.run(go()), "ok")
        self.assertEqual(scheduled, [(3.0, (1,))])


if __name__ == "__main__":
    unittest.main()

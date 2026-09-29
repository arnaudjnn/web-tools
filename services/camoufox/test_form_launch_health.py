"""The launch-failure streak shed (no browser needed).

A degraded host fails every launch until a restart — measured 2026-09-26
and 2026-09-29 — so a streak of failures exits and Railway restarts. The
defaults (both envs absent) must never schedule anything: a test suite or
a local run cannot be killed by a service it is only importing.
"""
import os
import unittest
from unittest.mock import patch

import launch_health


class LaunchStreakTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("LAUNCH_FAIL_STREAK", "LAUNCH_FAIL_SHED_S")}
        for k in self._saved:
            os.environ.pop(k, None)
        self._reset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._reset()

    @staticmethod
    def _reset():
        with launch_health._lock:
            if launch_health._timer is not None:
                launch_health._timer.cancel()
            launch_health._timer = None
            launch_health._streak = 0

    def test_disabled_by_default(self):
        for _ in range(10):
            launch_health.note(False)
        self.assertIsNone(launch_health._timer)
        self.assertEqual(launch_health._streak, 10)

    def test_garbage_env_disables(self):
        os.environ["LAUNCH_FAIL_STREAK"] = "not-a-number"
        launch_health.note(False)
        self.assertIsNone(launch_health._timer)

    def test_streak_schedules_exactly_one_shed(self):
        os.environ["LAUNCH_FAIL_STREAK"] = "3"
        os.environ["LAUNCH_FAIL_SHED_S"] = "60"
        launch_health.note(False)
        launch_health.note(False)
        self.assertIsNone(launch_health._timer)
        launch_health.note(False)
        first = launch_health._timer
        self.assertIsNotNone(first)
        launch_health.note(False)  # beyond the threshold: no second timer
        self.assertIs(launch_health._timer, first)

    def test_a_success_resets_the_streak(self):
        os.environ["LAUNCH_FAIL_STREAK"] = "3"
        launch_health.note(False)
        launch_health.note(False)
        launch_health.note(True)
        launch_health.note(False)
        launch_health.note(False)
        self.assertIsNone(launch_health._timer)

    def test_a_success_retracts_a_pending_shed(self):
        os.environ["LAUNCH_FAIL_STREAK"] = "3"
        os.environ["LAUNCH_FAIL_SHED_S"] = "60"
        for _ in range(3):
            launch_health.note(False)
        timer = launch_health._timer
        self.assertIsNotNone(timer)
        launch_health.note(True)
        self.assertIsNone(launch_health._timer)
        self.assertTrue(timer.finished.is_set() or not timer.is_alive())

    def test_the_timer_retracts_when_something_launched_in_between(self):
        os.environ["LAUNCH_FAIL_STREAK"] = "3"
        launch_health.note(False)
        launch_health.note(False)
        launch_health.note(False)
        launch_health._streak = 0  # a success reset it after the schedule
        with patch.object(launch_health.os, "_exit") as exit_:
            launch_health._shed_if_still_broken()
        exit_.assert_not_called()

    def test_the_timer_exits_when_still_broken(self):
        os.environ["LAUNCH_FAIL_STREAK"] = "3"
        launch_health.note(False)
        launch_health.note(False)
        launch_health.note(False)
        with patch.object(launch_health.os, "_exit") as exit_:
            launch_health._shed_if_still_broken()
        exit_.assert_called_once_with(1)

    def test_disabled_at_fire_time_never_exits(self):
        os.environ["LAUNCH_FAIL_STREAK"] = "3"
        for _ in range(3):
            launch_health.note(False)
        os.environ["LAUNCH_FAIL_STREAK"] = "0"
        with patch.object(launch_health.os, "_exit") as exit_:
            launch_health._shed_if_still_broken()
        exit_.assert_not_called()


if __name__ == "__main__":
    unittest.main()

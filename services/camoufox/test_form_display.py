import pathlib
import unittest


class DisplayLifecycleTests(unittest.TestCase):
    """The display rule a redeploy silently used to be the only cure for.

    A Railway restart reuses the container's writable layer: the dead X
    server's lock/socket survive it, the next Xvfb exits with "Server is
    already active for display 99", and every HEADED form launch dies while
    headless renders keep answering (measured 2026-09-29: 6/6 form launches
    failed across several restarts; a redeploy, which recreates the
    filesystem, was the only cure).
    """

    def setUp(self):
        self.entry = pathlib.Path(__file__).with_name("entrypoint.sh").read_text()

    def test_stale_display_files_are_cleared_when_nothing_answers(self):
        self.assertIn("rm -f /tmp/.X99-lock /tmp/.X11-unix/X99", self.entry)
        self.assertIn("s.connect('/tmp/.X11-unix/X99')", self.entry)

    def test_display_is_watchdogged_not_started_once(self):
        # A one-shot Xvfb at boot cannot survive its own death (OOM) or a
        # restart that keeps the files but not the process.
        self.assertIn("while :", self.entry)
        self.assertIn("Xvfb :99", self.entry)

    def test_the_app_is_still_the_execd_process(self):
        self.assertIn('exec uvicorn app:app', self.entry)


if __name__ == "__main__":
    unittest.main()

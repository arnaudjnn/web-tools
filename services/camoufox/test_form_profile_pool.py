"""The warmed profile pool and per-session fingerprint diversity."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from test_form_headed import RECORDED, app  # noqa: F401  (installs the stubs, imports app)

import fingerprint
import profile_pool


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dir = lambda name: os.path.join(self.root, name)
        profile_pool.reset()
        self.env = patch.dict(os.environ, {"FORM_PROFILE_POOL": "3"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(profile_pool.reset)

    def test_off_by_default(self):
        with patch.dict(os.environ, {"FORM_PROFILE_POOL": "0"}):
            self.assertFalse(profile_pool.enabled())

    def test_warmed_and_least_recently_used_first_and_never_shared(self):
        profile_pool.mark_warmed("pool2", self.dir)
        first = profile_pool.acquire(self.dir)
        self.assertEqual(first, "pool2")                 # warmed beats cold
        second = profile_pool.acquire(self.dir)
        third = profile_pool.acquire(self.dir)
        self.assertEqual(len({first, second, third}), 3)  # never the same twice at once
        self.assertIsNone(profile_pool.acquire(self.dir))  # all busy -> isolated browser
        profile_pool.release(second)
        self.assertEqual(profile_pool.acquire(self.dir), second)

    def test_warm_all_marks_successes_and_survives_failures(self):
        async def warm_one(name):
            if name == "pool1":
                raise RuntimeError("boom")
            return name != "pool2"
        asyncio.run(profile_pool.warm_all(warm_one, self.dir))
        self.assertEqual(profile_pool.pending(self.dir), ["pool1", "pool2"])


class DiversifyTests(unittest.TestCase):
    def test_off_unless_enabled(self):
        with patch.dict(os.environ, {"FORM_FINGERPRINT_DIVERSIFY": "0"}):
            self.assertIsNone(fingerprint.diversify(None))

    def test_draws_os_and_screen_within_the_display_and_keeps_explicit_values(self):
        with patch.dict(os.environ, {"FORM_FINGERPRINT_DIVERSIFY": "1"}):
            seen_os = set()
            for _ in range(200):
                spec = fingerprint.diversify({"locale": "it-IT"})
                seen_os.add(spec["os"])
                w, h = spec["screen"]
                self.assertLessEqual(w, 1440)
                self.assertLessEqual(h, 900)
                self.assertEqual(spec["locale"], "it-IT")
                fingerprint.launch_kwargs(dict(spec), screen_factory=lambda **k: None)  # valid
            self.assertEqual(seen_os, {"windows", "macos"})
            self.assertEqual(fingerprint.diversify({"os": "linux", "screen": [800, 600]}),
                             {"os": "linux", "screen": [800, 600]})


if __name__ == "__main__":
    unittest.main()

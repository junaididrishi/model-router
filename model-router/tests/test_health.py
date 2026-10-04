from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_router.router import Health


class HealthTests(unittest.TestCase):
    def test_opens_after_three_failures_and_blocks(self):
        h = Health()
        now = 1_000
        for i in range(3):
            h.observe("borealis-pro", "503", 0, now + i)
        self.assertFalse(h.allow("borealis-pro", now + 2))
        self.assertFalse(h.allow("borealis-pro", now + 2 + Health.COOLDOWN_MS - 1))
        self.assertTrue(h.allow("borealis-pro", now + 2 + Health.COOLDOWN_MS))

    def test_failed_probe_reopens(self):
        h = Health()
        now = 0
        for i in range(3):
            h.observe("borealis-pro", "503", 0, now + i)
        probe_at = now + 2 + Health.COOLDOWN_MS
        self.assertTrue(h.allow("borealis-pro", probe_at))
        h.observe("borealis-pro", "503", 0, probe_at)
        self.assertFalse(h.allow("borealis-pro", probe_at + 1))

    def test_success_closes(self):
        h = Health()
        for i in range(3):
            h.observe("borealis-pro", "503", 0, i)
        h.observe("borealis-pro", "ok", 100, 50_000)
        self.assertTrue(h.allow("borealis-pro", 50_000))

    def test_ordinary_5xx_does_not_open(self):
        h = Health()
        for i in range(10):
            h.observe("local-7b", "5xx", 10, i)
        self.assertTrue(h.allow("local-7b", 10))

    def test_allow_does_not_consume_the_probe(self):
        h = Health()
        for i in range(3):
            h.observe("borealis-pro", "503", 0, i)
        t = 2 + Health.COOLDOWN_MS
        self.assertTrue(h.allow("borealis-pro", t))
        self.assertTrue(h.allow("borealis-pro", t))


if __name__ == "__main__":
    unittest.main()

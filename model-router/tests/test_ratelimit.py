from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_router.router import LimitTracker
from model_router.simulator import Simulator, WINDOW_MS
from model_router.spec import PROVIDERS


class RateLimitTests(unittest.TestCase):
    def test_rpm_sliding_window_returns_429(self):
        sim = Simulator(seed=1)
        # local-7b is 30 RPM. 30 admits at t=0, 31st is 429.
        results = []
        for _ in range(31):
            results.append(
                sim.call("local-7b", 10, 10, (), timeout_ms=30_000, now_ms=0).result
            )
        admitted = [r for r in results if r != "429"]
        rejected = [r for r in results if r == "429"]
        self.assertEqual(len(admitted), 30)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(results[-1], "429")

    def test_rpm_frees_after_sixty_seconds(self):
        sim = Simulator(seed=1)
        for _ in range(30):
            sim.call("local-7b", 10, 10, (), timeout_ms=30_000, now_ms=0)
        blocked = sim.call("local-7b", 10, 10, (), timeout_ms=30_000, now_ms=0)
        self.assertEqual(blocked.result, "429")
        self.assertGreaterEqual(blocked.retry_after_ms, 1)
        # At exactly 60_000 the start at 0 has aged out of (now-60s, now].
        freed = sim.call("local-7b", 10, 10, (), timeout_ms=30_000, now_ms=WINDOW_MS)
        self.assertNotEqual(freed.result, "429")

    def test_tpm_counts_input_at_start(self):
        sim = Simulator(seed=1)
        # local-7b TPM is 60k, context 8k. Fifteen 4k calls fill TPM; a 16th 4k is 429.
        for _ in range(15):
            resp = sim.call("local-7b", 4_000, 10, (), timeout_ms=30_000, now_ms=0)
            self.assertNotEqual(resp.result, "429")
            self.assertNotEqual(resp.result, "context_exceeded")
        blocked = sim.call("local-7b", 4_000, 10, (), timeout_ms=30_000, now_ms=0)
        self.assertEqual(blocked.result, "429")
        self.assertEqual(blocked.cost_usd, 0.0)

    def test_429_does_not_consume_rpm(self):
        sim = Simulator(seed=1)
        for _ in range(30):
            sim.call("local-7b", 10, 10, (), timeout_ms=30_000, now_ms=0)
        sim.call("local-7b", 10, 10, (), timeout_ms=30_000, now_ms=0)  # 429
        # Still 30 in the window; another 429, not an admit after a no-op 429.
        again = sim.call("local-7b", 10, 10, (), timeout_ms=30_000, now_ms=0)
        self.assertEqual(again.result, "429")

    def test_atlas_large_incident_drops_rpm_to_20(self):
        sim = Simulator(seed=1)
        now = 1_850_000  # inside [1800, 2100)
        results = [
            sim.call("atlas-large", 10, 10, (), timeout_ms=30_000, now_ms=now).result
            for _ in range(21)
        ]
        self.assertEqual(sum(1 for r in results if r != "429"), 20)
        self.assertEqual(results[-1], "429")

    def test_preview_keeps_output_debit_until_it_completes(self):
        tracker = LimitTracker()
        spec = PROVIDERS["local-7b"]
        tracker.note_start("local-7b", 4_000, 0)
        tracker.note_output("local-7b", 1_000, 5_000)
        # At 1s the completion is still in the future, so only the 4k input counts.
        self.assertTrue(tracker.can_admit(spec, 55_000, 1_000))
        # Asking again at 5s must still see that output. Dropping the future
        # event on the earlier query was the bug.
        self.assertFalse(tracker.can_admit(spec, 56_000, 5_000))


if __name__ == "__main__":
    unittest.main()

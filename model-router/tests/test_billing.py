from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_router.simulator import Simulator
from model_router.spec import PROVIDERS


class BillingTests(unittest.TestCase):
    def test_timeout_bills_input_tokens_only(self):
        sim = Simulator(seed=1)
        inp = 10_000
        resp = sim.call("atlas-large", inp, 100, (), timeout_ms=1, now_ms=0)
        self.assertEqual(resp.result, "timeout")
        self.assertEqual(resp.latency_ms, 1)
        self.assertEqual(resp.output_tokens, 0)
        self.assertAlmostEqual(resp.cost_usd, inp * 3.0 / 1_000_000, places=12)

    def test_success_bills_input_and_sampled_output(self):
        sim = Simulator(seed=1)
        # Generous timeout so we are not racing p95.
        resp = sim.call("borealis-flash", 1_000, 200, (), timeout_ms=30_000, now_ms=0)
        self.assertEqual(resp.result, "ok")
        spec = PROVIDERS["borealis-flash"]
        expected = (
            1_000 * spec.in_per_mtok + resp.output_tokens * spec.out_per_mtok
        ) / 1_000_000
        self.assertAlmostEqual(resp.cost_usd, expected, places=12)
        self.assertGreaterEqual(resp.output_tokens, int(round(200 * 0.7)))
        self.assertLessEqual(resp.output_tokens, int(round(200 * 1.3)))

    def test_5xx_is_free(self):
        sim = Simulator(seed=0)
        found = None
        for i in range(400):
            # Space calls 2 minutes apart so RPM/TPM never bind.
            resp = sim.call("local-7b", 80, 10, (), timeout_ms=30_000, now_ms=i * 120_000)
            if resp.result == "5xx":
                found = resp
                break
        self.assertIsNotNone(found)
        self.assertEqual(found.cost_usd, 0.0)
        self.assertEqual(found.output_tokens, 0)

    def test_context_exceeded_is_free_and_counted(self):
        sim = Simulator(seed=1)
        resp = sim.call("local-7b", 20_000, 1_000, (), timeout_ms=30_000, now_ms=0)
        self.assertEqual(resp.result, "context_exceeded")
        self.assertEqual(resp.cost_usd, 0.0)
        self.assertEqual(sim.context_exceeded_calls, 1)

    def test_unsupported_capability_is_free(self):
        sim = Simulator(seed=1)
        resp = sim.call("local-7b", 100, 10, ("tools",), timeout_ms=30_000, now_ms=0)
        self.assertEqual(resp.result, "unsupported")
        self.assertEqual(resp.cost_usd, 0.0)

    def test_timeout_then_success_bills_input_twice(self):
        sim = Simulator(seed=2)
        inp = 5_000
        first = sim.call("nimbus-code", inp, 50, (), timeout_ms=1, now_ms=0)
        self.assertEqual(first.result, "timeout")
        second = sim.call("nimbus-code", inp, 50, (), timeout_ms=30_000, now_ms=2_000)
        # Second may 5xx (~4%); if it succeeds, input is billed again.
        if second.result == "ok":
            spec = PROVIDERS["nimbus-code"]
            expected_second = (
                inp * spec.in_per_mtok + second.output_tokens * spec.out_per_mtok
            ) / 1_000_000
            self.assertAlmostEqual(second.cost_usd, expected_second, places=12)
            self.assertGreater(first.cost_usd + second.cost_usd, first.cost_usd)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_router.router import Router
from model_router.simulator import CallResponse, Simulator


class ScriptedSim:
    """Stand-in simulator that returns a scripted sequence of results."""

    def __init__(self, script: list[CallResponse]):
        self.script = list(script)
        self.calls: list[tuple] = []
        self.context_exceeded_calls = 0
        self.calls_by_provider: dict[str, int] = {}
        self.result_counts: dict[str, int] = {}

    def call(self, provider, input_tokens, expected_output_tokens, required, timeout_ms, now_ms):
        self.calls.append((provider, timeout_ms, now_ms))
        resp = self.script.pop(0)
        return resp


def _req(region="us") -> dict:
    return {
        "id": "r-1",
        "t_ms": 0,
        "tenant": "acme",
        "category": "chat",
        "input_tokens": 100,
        "expected_output_tokens": 50,
        "priority": "normal",
        "constraints": {"required": [], "region": region},
    }


def _resp(provider, result, latency=10, cost=0.0, retry_after=0) -> CallResponse:
    return CallResponse(provider, result, latency, cost, 0, retry_after_ms=retry_after)


class RetryAccountingTests(unittest.TestCase):
    def test_b_large_three_retries_on_5xx(self):
        # 1 initial + 3 retries = 4 calls, then failed.
        script = [_resp("atlas-large", "5xx") for _ in range(4)]
        sim = ScriptedSim(script)
        router = Router(sim, policy="b-large")  # type: ignore[arg-type]
        outcome = router.route(_req(), 0)
        self.assertEqual(outcome.final, "failed")
        self.assertEqual(len(outcome.attempts), 4)
        self.assertEqual([a.result for a in outcome.attempts], ["5xx"] * 4)

    def test_b_large_stops_on_success(self):
        script = [_resp("atlas-large", "5xx"), _resp("atlas-large", "ok", cost=0.001)]
        sim = ScriptedSim(script)
        router = Router(sim, policy="b-large")  # type: ignore[arg-type]
        outcome = router.route(_req(), 0)
        self.assertEqual(outcome.final, "ok")
        self.assertEqual(len(outcome.attempts), 2)
        self.assertEqual(outcome.provider, "atlas-large")

    def test_b_large_429_wait_counts_as_a_retry(self):
        script = [
            _resp("atlas-large", "429", latency=0, retry_after=500),
            _resp("atlas-large", "ok", cost=0.001),
        ]
        sim = ScriptedSim(script)
        router = Router(sim, policy="b-large")  # type: ignore[arg-type]
        outcome = router.route(_req(), 1_000)
        self.assertEqual(outcome.final, "ok")
        self.assertEqual(len(outcome.attempts), 2)
        # Second call starts after retry_after.
        self.assertEqual(sim.calls[1][2], 1_000 + 500)
        self.assertEqual(outcome.total_latency_ms, 500 + 10)

    def test_b_large_picks_atlas_large_in_for_india(self):
        script = [_resp("atlas-large-in", "ok", cost=0.001)]
        sim = ScriptedSim(script)
        router = Router(sim, policy="b-large")  # type: ignore[arg-type]
        outcome = router.route(_req(region="in"), 0)
        self.assertEqual(sim.calls[0][0], "atlas-large-in")
        self.assertEqual(outcome.provider, "atlas-large-in")

    def test_b_cheap_one_retry(self):
        # Cheapest eligible for a tiny us/json-free request is local-7b.
        script = [_resp("local-7b", "5xx"), _resp("local-7b", "5xx")]
        sim = ScriptedSim(script)
        router = Router(sim, policy="b-cheap")  # type: ignore[arg-type]
        outcome = router.route(_req(), 0)
        self.assertEqual(outcome.final, "failed")
        self.assertEqual(len(outcome.attempts), 2)

    def test_b_large_timeout_30s(self):
        script = [_resp("atlas-large", "ok")]
        sim = ScriptedSim(script)
        router = Router(sim, policy="b-large")  # type: ignore[arg-type]
        router.route(_req(), 0)
        self.assertEqual(sim.calls[0][1], 30_000)

    def test_real_sim_b_large_never_raises(self):
        sim = Simulator(seed=3)
        router = Router(sim, policy="b-large")
        req = _req()
        req["input_tokens"] = 50
        outcome = router.route(req, 0)
        self.assertIn(outcome.final, ("ok", "failed", "no_route"))


if __name__ == "__main__":
    unittest.main()

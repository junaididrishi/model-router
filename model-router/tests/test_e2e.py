from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_router.replay import metrics_public, replay
from model_router.trace import generate_trace, load_trace


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reqs = generate_trace(seed=7, duration_s=3600.0, limit=200)
        assert len(cls.reqs) == 200

    def test_two_hundred_request_composite_pinned(self):
        _, mine = replay(self.reqs, policy="mine", seed=7)
        # Pinned to two decimal places as required. Regenerating the 200-request
        # prefix from seed 7 must keep this value; if you change the policy,
        # update the pin and say why in DECISIONS.md.
        self.assertEqual(round(mine["composite"], 2), 82.24)
        self.assertEqual(mine["violations"]["region"], 0)
        self.assertEqual(mine["context_exceeded_calls"], 0)

    def test_mine_beats_baselines_on_short_trace(self):
        _, mine = replay(self.reqs, policy="mine", seed=7)
        _, large = replay(self.reqs, policy="b-large", seed=7)
        _, cheap = replay(self.reqs, policy="b-cheap", seed=7)
        self.assertGreater(mine["composite"], large["composite"])
        self.assertGreater(mine["composite"], cheap["composite"])

    def test_two_runs_byte_identical_metrics(self):
        _, a = replay(self.reqs, policy="mine", seed=7)
        _, b = replay(self.reqs, policy="mine", seed=7)
        left = json.dumps(metrics_public(a), indent=2, sort_keys=False) + "\n"
        right = json.dumps(metrics_public(b), indent=2, sort_keys=False) + "\n"
        self.assertEqual(left, right)

    def test_seed_42_trace_beats_baselines(self):
        reqs = load_trace(Path(__file__).resolve().parents[1] / "data" / "requests.jsonl")
        _, mine = replay(reqs, policy="mine", seed=42)
        _, large = replay(reqs, policy="b-large", seed=42)
        _, cheap = replay(reqs, policy="b-cheap", seed=42)
        self.assertGreater(mine["composite"], large["composite"])
        self.assertGreater(mine["composite"], cheap["composite"])
        self.assertEqual(mine["violations"]["region"], 0)
        self.assertEqual(mine["context_exceeded_calls"], 0)
        self.assertLess(mine["borealis_outage_calls"], 20)
        self.assertLessEqual(mine["cost_usd"]["acme"], 40)
        self.assertLessEqual(mine["cost_usd"]["globex"], 30)
        self.assertLessEqual(mine["cost_usd"]["initech"], 5)


if __name__ == "__main__":
    unittest.main()

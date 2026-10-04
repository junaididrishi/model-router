from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_router.trace import generate_trace, load_trace

FROZEN = Path(__file__).resolve().parents[1] / "data" / "requests.jsonl"


class TraceTests(unittest.TestCase):
    def test_seed_42_count_is_frozen(self):
        reqs = generate_trace(seed=42)
        self.assertEqual(len(reqs), 5093)
        self.assertEqual(reqs[0].id, "r-000001")
        self.assertEqual(reqs[-1].id, "r-005093")

    def test_frozen_file_matches_generator(self):
        generated = generate_trace(seed=42)
        loaded = load_trace(FROZEN)
        self.assertEqual(len(generated), len(loaded))
        for a, b in zip(generated, loaded):
            self.assertEqual(a.id, b.id)
            self.assertEqual(a.t_ms, b.t_ms)
            self.assertEqual(a.true_category, b.true_category)
            self.assertEqual(a.input_tokens, b.input_tokens)
            self.assertEqual(a.category, b.category)


if __name__ == "__main__":
    unittest.main()

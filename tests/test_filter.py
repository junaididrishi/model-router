from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_router.router import hard_filter
from model_router.spec import expected_cost


def _req(**kwargs) -> dict:
    base = {
        "id": "r-1",
        "t_ms": 0,
        "tenant": "acme",
        "category": "chat",
        "input_tokens": 100,
        "expected_output_tokens": 50,
        "priority": "normal",
        "constraints": {
            "required": [],
            "region": "us",
        },
    }
    constraints = dict(base["constraints"])
    constraints.update(kwargs.pop("constraints", {}))
    base.update(kwargs)
    base["constraints"] = constraints
    return base


class HardFilterTests(unittest.TestCase):
    def test_region_in_drops_us_only_providers(self):
        eligible, _ = hard_filter(_req(constraints={"region": "in"}))
        names = {s.name for s in eligible}
        self.assertNotIn("atlas-large", names)
        self.assertNotIn("borealis-pro", names)
        self.assertNotIn("nimbus-code", names)
        self.assertIn("atlas-large-in", names)
        self.assertIn("atlas-small", names)
        self.assertIn("borealis-flash", names)
        self.assertIn("local-7b", names)

    def test_context_window(self):
        eligible, reasons = hard_filter(
            _req(input_tokens=500_000, expected_output_tokens=1_000)
        )
        names = {s.name for s in eligible}
        self.assertNotIn("atlas-small", names)
        self.assertNotIn("local-7b", names)
        self.assertIn("borealis-pro", names)
        self.assertIn("borealis-flash", names)

    def test_capability_tools(self):
        eligible, _ = hard_filter(_req(constraints={"required": ["tools"]}))
        names = {s.name for s in eligible}
        self.assertNotIn("local-7b", names)
        self.assertNotIn("borealis-flash", names)
        self.assertIn("atlas-small", names)

    def test_max_cost_uses_expected_price(self):
        req = _req(
            input_tokens=10_000,
            expected_output_tokens=1_000,
            constraints={"max_cost_usd": 0.002},
        )
        eligible, reasons = hard_filter(req)
        for spec in eligible:
            self.assertLessEqual(
                expected_cost(spec, 10_000, 1_000), 0.002 + 1e-12
            )
        if not eligible:
            self.assertIn("cost", reasons)

    def test_p50_vs_max_latency(self):
        eligible, _ = hard_filter(_req(constraints={"max_latency_ms": 1000}))
        names = {s.name for s in eligible}
        self.assertNotIn("atlas-large", names)  # p50 1800
        self.assertNotIn("atlas-large-in", names)  # p50 2100
        self.assertIn("atlas-small", names)  # p50 500
        self.assertIn("borealis-flash", names)  # p50 300

    def test_min_quality_shown_category(self):
        eligible, _ = hard_filter(
            _req(category="code", constraints={"min_quality": 9})
        )
        names = {s.name for s in eligible}
        self.assertEqual(names, {"atlas-large", "nimbus-code"})

    def test_unknown_worst_case_quality(self):
        eligible, _ = hard_filter(
            _req(category="unknown", constraints={"min_quality": 8}),
            unknown="worst",
        )
        names = {s.name for s in eligible}
        # Only atlas-large family has min(quality) >= 8 (actually 9).
        self.assertEqual(names, {"atlas-large"})

    def test_unknown_as_chat_is_more_permissive(self):
        worst, _ = hard_filter(
            _req(category="unknown", constraints={"min_quality": 8}),
            unknown="worst",
        )
        chat, _ = hard_filter(
            _req(category="unknown", constraints={"min_quality": 8}),
            unknown="chat",
        )
        self.assertGreater(len(chat), len(worst))

    def test_no_route_reasons_when_unsatisfiable(self):
        eligible, reasons = hard_filter(
            _req(
                input_tokens=300_000,
                expected_output_tokens=1_000,
                constraints={"max_cost_usd": 0.002, "region": "in", "required": ["tools"]},
            )
        )
        self.assertEqual(eligible, [])
        self.assertTrue(reasons)


if __name__ == "__main__":
    unittest.main()

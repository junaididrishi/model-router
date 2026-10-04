"""Provider catalogue and scoring constants. Prices, limits, incidents stay as specified."""

from __future__ import annotations

from dataclasses import dataclass


CATEGORIES = ("chat", "code", "summarize", "extract", "reasoning", "creative")

# Trace mix, used by the unknown-category expected-quality strategy.
CATEGORY_MIX = {
    "chat": 0.35,
    "code": 0.20,
    "summarize": 0.15,
    "extract": 0.10,
    "reasoning": 0.10,
    "creative": 0.10,
}

TENANT_BUDGETS = {"acme": 40.0, "globex": 30.0, "initech": 5.0}

PRIORITY_WEIGHT = {"high": 2.0, "normal": 1.0, "low": 0.5}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    in_per_mtok: float
    out_per_mtok: float
    p50_ms: float
    p95_ms: float
    context: int
    rpm: int
    tpm: int
    error_rate: float
    capabilities: frozenset[str]
    regions: frozenset[str]
    quality: dict[str, int]

    @property
    def mu(self) -> float:
        import math

        return math.log(self.p50_ms)

    @property
    def sigma(self) -> float:
        import math

        return (math.log(self.p95_ms) - math.log(self.p50_ms)) / 1.645


def _q(chat, code, summarize, extract, reasoning, creative) -> dict[str, int]:
    return {
        "chat": chat,
        "code": code,
        "summarize": summarize,
        "extract": extract,
        "reasoning": reasoning,
        "creative": creative,
    }


ATLAS_LARGE_Q = _q(9, 9, 9, 9, 10, 9)

PROVIDERS: dict[str, ProviderSpec] = {
    p.name: p
    for p in (
        ProviderSpec(
            "atlas-large", 3.00, 15.00, 1800, 4500, 200_000, 60, 300_000, 0.02,
            frozenset({"tools", "vision", "json"}), frozenset({"us"}), ATLAS_LARGE_Q,
        ),
        ProviderSpec(
            "atlas-large-in", 3.00, 15.00, 2100, 5000, 200_000, 30, 150_000, 0.02,
            frozenset({"tools", "vision", "json"}), frozenset({"in"}), ATLAS_LARGE_Q,
        ),
        ProviderSpec(
            "atlas-small", 0.30, 1.50, 500, 1200, 200_000, 600, 2_000_000, 0.02,
            frozenset({"tools", "json"}), frozenset({"us", "in"}), _q(7, 6, 8, 8, 6, 7),
        ),
        ProviderSpec(
            "borealis-pro", 1.50, 6.00, 1200, 6000, 1_000_000, 120, 1_000_000, 0.05,
            frozenset({"tools", "vision", "json"}), frozenset({"us"}), _q(8, 7, 9, 8, 8, 8),
        ),
        ProviderSpec(
            "borealis-flash", 0.10, 0.40, 300, 800, 1_000_000, 1000, 4_000_000, 0.03,
            frozenset({"vision", "json"}), frozenset({"us", "in"}), _q(6, 4, 7, 7, 4, 6),
        ),
        ProviderSpec(
            "nimbus-code", 0.60, 2.40, 900, 2500, 128_000, 200, 500_000, 0.04,
            frozenset({"tools", "json"}), frozenset({"us"}), _q(5, 9, 5, 7, 6, 3),
        ),
        ProviderSpec(
            "local-7b", 0.02, 0.02, 900, 3000, 8_000, 30, 60_000, 0.15,
            frozenset(), frozenset({"us", "in"}), _q(4, 3, 5, 4, 2, 4),
        ),
    )
}

PROVIDER_ORDER = tuple(PROVIDERS)


def expected_cost(spec: ProviderSpec, input_tokens: int, expected_output_tokens: int) -> float:
    return (input_tokens * spec.in_per_mtok + expected_output_tokens * spec.out_per_mtok) / 1_000_000


def quality_for(spec: ProviderSpec, category: str, unknown: str = "worst") -> float:
    """Quality the router is allowed to see. `unknown` is not a real category."""
    if category in spec.quality:
        return float(spec.quality[category])
    if unknown == "chat":
        return float(spec.quality["chat"])
    if unknown == "expected":
        return sum(spec.quality[c] * CATEGORY_MIX[c] for c in CATEGORIES)
    return float(min(spec.quality.values()))

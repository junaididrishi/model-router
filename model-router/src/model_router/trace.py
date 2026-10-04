"""Trace generator. Frozen distribution, seed 42 by default.

The generator RNG is separate from the simulator RNG, so replaying a trace
does not depend on how many calls the router makes.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .rng import stream


@dataclass
class Constraints:
    max_latency_ms: int | None = None
    max_cost_usd: float | None = None
    required: list[str] = field(default_factory=list)
    region: str = "us"
    min_quality: int | None = None

    def to_json(self) -> dict:
        # Always emit every key so the frozen file has one schema.
        # Absent constraints are null.
        return {
            "max_latency_ms": self.max_latency_ms,
            "max_cost_usd": self.max_cost_usd,
            "required": list(self.required),
            "region": self.region,
            "min_quality": self.min_quality,
        }


@dataclass
class Request:
    id: str
    t_ms: int
    tenant: str
    category: str
    input_tokens: int
    expected_output_tokens: int
    priority: str
    constraints: Constraints
    true_category: str

    def router_view(self) -> dict:
        """What route() is allowed to see. true_category is omitted."""
        return {
            "id": self.id,
            "t_ms": self.t_ms,
            "tenant": self.tenant,
            "category": self.category,
            "input_tokens": self.input_tokens,
            "expected_output_tokens": self.expected_output_tokens,
            "priority": self.priority,
            "constraints": self.constraints.to_json(),
        }

    def to_json(self) -> dict:
        d = self.router_view()
        d["true_category"] = self.true_category
        return d


def _lognormal_int(rng, median: float, sigma: float, lo: int, hi: int | None = None) -> int:
    # median = exp(mu) => mu = ln(median)
    mu = math.log(median)
    val = math.exp(mu + sigma * _standard_normal(rng))
    n = int(round(val))
    if n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _standard_normal(rng) -> float:
    # Box-Muller on this stream. Two uniforms, one normal; the spare is discarded
    # so each call consumes a fixed number of draws.
    u1 = max(rng.random(), 1e-12)
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _choice(rng, items, weights):
    x = rng.random() * sum(weights)
    acc = 0.0
    for item, w in zip(items, weights):
        acc += w
        if x <= acc:
            return item
    return items[-1]


def generate_trace(
    seed: int = 42, duration_s: float = 3600.0, limit: int | None = None
) -> list[Request]:
    """Poisson arrivals. Rate is 1/s, and 6/s when the draw starts inside [1200, 1500).

    A gap that crosses a rate boundary is kept, not redrawn. That is a
    simplification of a piecewise Poisson process; the frozen trace uses it.
    """
    rng = stream(seed, "trace")
    requests: list[Request] = []
    t = 0.0
    n = 0
    while True:
        rate = 6.0 if 1200.0 <= t < 1500.0 else 1.0
        gap = -math.log(max(rng.random(), 1e-12)) / rate
        t += gap
        if t >= duration_s:
            break

        true_cat = _choice(
            rng,
            ["chat", "code", "summarize", "extract", "reasoning", "creative"],
            [0.35, 0.20, 0.15, 0.10, 0.10, 0.10],
        )
        shown = "unknown" if rng.random() < 0.20 else true_cat
        tenant = _choice(rng, ["acme", "globex", "initech"], [0.50, 0.30, 0.20])
        priority = _choice(rng, ["high", "normal", "low"], [0.10, 0.70, 0.20])

        roll = rng.random()
        if roll < 0.92:
            input_tokens = _lognormal_int(rng, 1200, 0.8, 50, None)
        elif roll < 0.98:
            input_tokens = rng.randint(20_000, 150_000)
        else:
            input_tokens = rng.randint(150_000, 400_000)

        output_tokens = _lognormal_int(rng, 300, 0.7, 10, 4000)

        max_latency = None
        if rng.random() < 0.30:
            max_latency = _choice(rng, [1000, 2000, 4000], [1, 1, 1])
        max_cost = None
        if rng.random() < 0.25:
            max_cost = _choice(rng, [0.002, 0.01, 0.05], [1, 1, 1])
        required = []
        if rng.random() < 0.20:
            required.append("tools")
        if rng.random() < 0.10:
            required.append("vision")
        if rng.random() < 0.40:
            required.append("json")
        region = "us" if rng.random() < 0.85 else "in"
        min_quality = None
        if rng.random() < 0.30:
            min_quality = _choice(rng, [6, 8, 9], [1, 1, 1])

        n += 1
        requests.append(
            Request(
                id=f"r-{n:06d}",
                t_ms=int(round(t * 1000)),
                tenant=tenant,
                category=shown,
                input_tokens=input_tokens,
                expected_output_tokens=output_tokens,
                priority=priority,
                constraints=Constraints(
                    max_latency_ms=max_latency,
                    max_cost_usd=max_cost,
                    required=required,
                    region=region,
                    min_quality=min_quality,
                ),
                true_category=true_cat,
            )
        )
        if limit is not None and n >= limit:
            break
    return requests


def write_trace(path: Path, seed: int = 42) -> list[Request]:
    reqs = generate_trace(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in reqs:
            f.write(json.dumps(r.to_json(), separators=(",", ":")) + "\n")
    return reqs


def load_trace(path: Path) -> list[Request]:
    reqs = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            c = d["constraints"]
            reqs.append(
                Request(
                    id=d["id"],
                    t_ms=d["t_ms"],
                    tenant=d["tenant"],
                    category=d["category"],
                    input_tokens=d["input_tokens"],
                    expected_output_tokens=d["expected_output_tokens"],
                    priority=d["priority"],
                    constraints=Constraints(
                        max_latency_ms=c.get("max_latency_ms"),
                        max_cost_usd=c.get("max_cost_usd"),
                        required=list(c.get("required") or []),
                        region=c["region"],
                        min_quality=c.get("min_quality"),
                    ),
                    true_category=d["true_category"],
                )
            )
    return reqs

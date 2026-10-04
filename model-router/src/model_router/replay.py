"""Replay a trace on a simulated clock and score it.

The scorer is the only place `true_category` is read. The router receives
`router_view()`, which omits it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .router import Outcome, Router
from .simulator import Simulator
from .spec import PRIORITY_WEIGHT, PROVIDERS, TENANT_BUDGETS
from .trace import Request


def violations_for(request: Request, outcome: Outcome) -> list[str]:
    if outcome.final != "ok" or outcome.provider is None:
        return []
    c = request.constraints
    found: list[str] = []
    spec = PROVIDERS[outcome.provider]
    if c.max_latency_ms is not None and outcome.total_latency_ms > c.max_latency_ms:
        found.append("latency")
    if c.max_cost_usd is not None and outcome.total_cost_usd > c.max_cost_usd + 1e-12:
        found.append("cost")
    if c.region not in spec.regions:
        found.append("region")
    if c.min_quality is not None and spec.quality[request.true_category] < c.min_quality:
        found.append("min_quality")
    return found


def score_request(
    request: Request,
    outcome: Outcome,
    *,
    spend_before: float,
    budget: float,
) -> float:
    w = PRIORITY_WEIGHT[request.priority]
    c = outcome.total_cost_usd
    # Penalty only when the tenant was already at/over budget before this
    # request spent anything. The request that crosses the line is not
    # penalised; every later one that still spends is.
    extra = -2 * w if (spend_before >= budget - 1e-12 and c > 0) else 0.0

    if outcome.final == "no_route":
        return -0.5 * w
    if outcome.final == "failed":
        return -1 * w - 20 * c + extra
    q = PROVIDERS[outcome.provider].quality[request.true_category] if outcome.provider else 0
    viol = violations_for(request, outcome)
    if viol:
        return -0.5 * w - 20 * c + extra
    return (q / 10.0) * w - 20 * c + extra


@dataclass
class Scored:
    outcome: Outcome
    score: float
    violations: list[str]


def replay(
    requests: list[Request],
    policy: str,
    seed: int = 42,
    unknown: str = "worst",
    use_health: bool = True,
    use_budget: bool = True,
    use_rate_preview: bool = True,
    timeout_mode: str = "30s",
) -> tuple[list[Scored], dict]:
    sim = Simulator(seed=seed)
    router = Router(
        sim,
        policy=policy,
        unknown=unknown,
        use_health=use_health,
        use_budget=use_budget,
        use_rate_preview=use_rate_preview,
        timeout_mode=timeout_mode,
    )
    spent = {t: 0.0 for t in TENANT_BUDGETS}
    scored: list[Scored] = []
    for req in requests:
        outcome = router.route(req.router_view(), req.t_ms)
        viol = violations_for(req, outcome)
        outcome.violations = viol
        s = score_request(
            req,
            outcome,
            spend_before=spent[req.tenant],
            budget=TENANT_BUDGETS[req.tenant],
        )
        spent[req.tenant] += outcome.total_cost_usd
        scored.append(Scored(outcome, s, viol))
    metrics = summarise(policy, seed, requests, scored, sim)
    return scored, metrics


def summarise(
    policy: str, seed: int, requests: list[Request], scored: list[Scored], sim: Simulator
) -> dict:
    ok = failed = no_route = 0
    viol = {"latency": 0, "cost": 0, "region": 0, "min_quality": 0}
    cost = {"total": 0.0, "acme": 0.0, "globex": 0.0, "initech": 0.0}
    latencies: list[int] = []
    quality_sum = 0.0
    quality_n = 0
    composite = 0.0
    borealis_outage_calls = 0

    for req, sc in zip(requests, scored):
        o = sc.outcome
        composite += sc.score
        if o.final == "ok":
            ok += 1
            latencies.append(o.total_latency_ms)
            if o.provider:
                quality_sum += PROVIDERS[o.provider].quality[req.true_category]
                quality_n += 1
        elif o.final == "failed":
            failed += 1
        else:
            no_route += 1
        for v in sc.violations:
            if v in viol:
                viol[v] += 1
        cost["total"] += o.total_cost_usd
        cost[req.tenant] += o.total_cost_usd
        for attempt in o.attempts:
            if attempt.provider == "borealis-pro" and 900_000 <= attempt.started_ms < 1_080_000:
                borealis_outage_calls += 1

    latencies.sort()

    def pct(p: float) -> int:
        if not latencies:
            return 0
        idx = min(len(latencies) - 1, max(0, int(round(p * (len(latencies) - 1)))))
        return int(latencies[idx])

    def money(x: float) -> float:
        return round(x, 6)

    return {
        "policy": policy,
        "seed": seed,
        "requests": len(requests),
        "ok": ok,
        "failed": failed,
        "no_route": no_route,
        "violations": viol,
        "cost_usd": {k: money(v) for k, v in cost.items()},
        "latency_ms": {"p50": pct(0.50), "p95": pct(0.95)},
        "avg_quality": round(quality_sum / quality_n, 4) if quality_n else 0.0,
        "composite": round(composite, 4),
        "context_exceeded_calls": sim.context_exceeded_calls,
        "calls_by_provider": dict(sim.calls_by_provider),
        "result_counts": dict(sim.result_counts),
        "borealis_outage_calls": borealis_outage_calls,
        "count_429": int(sim.result_counts.get("429", 0)),
    }


def metrics_public(metrics: dict) -> dict:
    """The file the brief asks for. Extra diagnostic keys stay out."""
    keys = (
        "policy",
        "seed",
        "requests",
        "ok",
        "failed",
        "no_route",
        "violations",
        "cost_usd",
        "latency_ms",
        "avg_quality",
        "composite",
    )
    return {k: metrics[k] for k in keys}


def write_metrics(path, metrics: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(metrics_public(metrics), indent=2, sort_keys=False) + "\n"
    path.write_text(payload)


def write_metrics_full(path, metrics: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(metrics, indent=2, sort_keys=False) + "\n"
    path.write_text(payload)


def bucket_spend(requests: list[Request], scored: list[Scored], bucket_s: int = 300) -> dict:
    n_buckets = 3600 // bucket_s
    buckets: dict[str, list[float]] = {t: [0.0] * n_buckets for t in TENANT_BUDGETS}
    for req, sc in zip(requests, scored):
        b = min(n_buckets - 1, req.t_ms // (bucket_s * 1000))
        buckets[req.tenant][b] += sc.outcome.total_cost_usd
    return {t: [round(x, 6) for x in xs] for t, xs in buckets.items()}

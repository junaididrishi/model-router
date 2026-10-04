"""CLI: generate the frozen trace and replay policies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .replay import bucket_spend, replay, write_metrics, write_metrics_full
from .trace import load_trace, write_trace


ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay the model router against the seeded simulator."
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--trace", type=Path, default=ROOT / "data" / "requests.jsonl")
    parser.add_argument("--metrics-dir", type=Path, default=ROOT / "metrics")
    parser.add_argument(
        "--regen-trace",
        action="store_true",
        help="Regenerate the trace. Do this once, then freeze.",
    )
    parser.add_argument(
        "--policies",
        nargs="+",
        default=["b-large", "b-cheap", "mine"],
    )
    parser.add_argument(
        "--ablate",
        action="store_true",
        help="Write before/after metrics for Tier B knobs (mine only).",
    )
    args = parser.parse_args(argv)

    if args.regen_trace or not args.trace.exists():
        reqs = write_trace(args.trace, seed=args.seed)
        print(f"wrote {len(reqs)} requests to {args.trace}")
    else:
        reqs = load_trace(args.trace)

    rows = []
    for policy in args.policies:
        scored, metrics = replay(reqs, policy=policy, seed=args.seed)
        out = args.metrics_dir / f"metrics-{policy}.json"
        write_metrics(out, metrics)
        rows.append(_row(policy, metrics))
        print(
            f"{policy}: composite={metrics['composite']} ok={metrics['ok']} "
            f"failed={metrics['failed']} no_route={metrics['no_route']} "
            f"cost={metrics['cost_usd']['total']} region={metrics['violations']['region']} "
            f"context_exceeded={metrics['context_exceeded_calls']} "
            f"429s={metrics['count_429']} "
            f"borealis_outage={metrics['borealis_outage_calls']}"
        )
        if policy == "mine":
            buckets = bucket_spend(reqs, scored)
            (args.metrics_dir / "spend-buckets-mine.json").write_text(
                json.dumps(buckets, indent=2) + "\n"
            )
            write_metrics_full(args.metrics_dir / "diagnostics-mine.json", metrics)

    if args.ablate:
        rows.extend(_run_ablations(reqs, args.seed, args.metrics_dir))
        (args.metrics_dir / "tier-b.json").write_text(json.dumps(rows, indent=2) + "\n")


def _row(name: str, metrics: dict) -> dict:
    return {
        "policy": name,
        "composite": metrics["composite"],
        "ok": metrics["ok"],
        "failed": metrics["failed"],
        "no_route": metrics["no_route"],
        "violations": metrics["violations"],
        "cost_usd": metrics["cost_usd"],
        "count_429": metrics["count_429"],
        "borealis_outage_calls": metrics["borealis_outage_calls"],
        "context_exceeded_calls": metrics["context_exceeded_calls"],
    }


def _run_ablations(reqs, seed: int, metrics_dir: Path) -> list[dict]:
    variants = [
        ("mine-no-health", dict(use_health=False)),
        ("mine-no-budget", dict(use_budget=False)),
        ("mine-no-rate-preview", dict(use_rate_preview=False)),
        ("mine-unknown-chat", dict(unknown="chat")),
        ("mine-unknown-expected", dict(unknown="expected")),
        ("mine-timeout-p50", dict(timeout_mode="p50")),
        ("mine-timeout-p95", dict(timeout_mode="p95")),
        ("mine-timeout-p99", dict(timeout_mode="p99")),
        ("mine-timeout-p95x1.5", dict(timeout_mode="p95x1.5")),
    ]
    rows = []
    for name, kwargs in variants:
        _, metrics = replay(reqs, policy="mine", seed=seed, **kwargs)
        metrics["policy"] = name
        write_metrics(metrics_dir / f"metrics-{name}.json", metrics)
        rows.append(_row(name, metrics))
        print(
            f"{name}: composite={metrics['composite']} ok={metrics['ok']} "
            f"no_route={metrics['no_route']} 429s={metrics['count_429']} "
            f"borealis_outage={metrics['borealis_outage_calls']} "
            f"initech={metrics['cost_usd']['initech']}"
        )
    return rows


if __name__ == "__main__":
    main()

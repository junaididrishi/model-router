# Model router

Offline router in front of seven simulated LLM providers. No network, no API keys, no real tokens.

Language: **Python 3.11+** (developed on 3.14). Standard library only.

## Commands

```bash
python3 run.py test
python3 run.py replay
```

`replay` writes `metrics/metrics-mine.json`, `metrics/metrics-b-large.json`, and `metrics/metrics-b-cheap.json` from the frozen seed-42 trace. Optional: `python3 run.py replay --ablate` writes the Tier B before/after files cited in `DECISIONS.md`.

Regenerate the trace only if you mean to un-freeze it:

```bash
python3 run.py replay --regen-trace
```

## Layout

```
run.py                      entry point (adds src/ to sys.path)
src/model_router/
  spec.py                   provider catalogue, prices, quality, budgets
  rng.py                    sha256-keyed RNG streams
  simulator.py              call(): latency, errors, RPM/TPM, billing, incidents
  trace.py                  Poisson trace, seed 42
  router.py                 hard_filter, B-large, B-cheap, mine
  replay.py                 clock, scorer, metrics
data/requests.jsonl         frozen trace (true_category is for the scorer only)
metrics/                    graded files plus cited ablations
tests/                      unittest
```

The router receives `Request.router_view()`, which omits `true_category`. The scorer is the only reader of that field. It is stored on the jsonl so a replay does not have to regenerate the trace.

## Policy (mine)

1. Hard-filter: context, capabilities, region, expected cost × 1.3 vs `max_cost_usd`, p50 vs `max_latency_ms`, quality vs `min_quality`. Unknown category uses the minimum quality across the six categories.
2. Rank survivors by expected score `(q/10)×w − 20×expected_cost`, minus an EWMA error penalty. Skip if even a perfect serve scores below `no_route`.
3. Timeout 30 s, or remaining `max_latency_ms` when that constraint is present. Floor 50 ms; skip the call if the remaining budget is below that.
4. On 5xx/timeout, fail over to the next candidate (3 attempts). On 429, skip.
5. Circuit breaker opens after three `503`s in 15 s, half-open after 30 s. Ordinary 5xx does not open it.
6. Tenant spend is paced over 3600 s and hard-capped at the budget.
7. Rate preview: skip a provider we believe is at RPM/TPM, using actual output tokens from our own calls.

B-large and B-cheap match the brief and are not changed after the first passing run.

## Seed-42 result (5093 requests)

| policy  | composite | ok   | cost USD | region | context_exceeded |
|---------|-----------|------|----------|--------|------------------|
| mine    | 2836.99   | 4496 | 23.32    | 0      | 0                |
| b-cheap | 677.81    | 4816 | 15.28    | 0      | 0                |
| b-large | −4678.56  | 4975 | 135.42   | 0      | 88               |

Two replays on seed 42 produce byte-identical metrics files. See `DECISIONS.md` for plan, cuts, and Tier B before/after numbers.

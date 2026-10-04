# Decisions

Language: Python 3.11+ (stdlib only). No network, no API keys. Tests are `unittest` so `python run.py test` has no install step.

## 1. Plan (2026-10-04, first hour, before code)

Order, and why:

1. **Provider spec + seeded simulator** (`call`). Billing, log-normal latency, 429/RPM/TPM windows, context, capabilities, incidents, timeouts. Biggest correctness risk lives here; get it wrong and every score is fiction.
2. **Trace generator, seed 42.** Freeze the generator and the RNG draws before any policy measurement. Write `requests.jsonl` once and replay from the file.
3. **Baselines B-large and B-cheap exactly as specified.** Implement them first, then never touch them. They are the floor.
4. **Router v1 (static).** Hard filter (context, capabilities, region, expected cost vs `max_cost_usd`, p50 vs `max_latency_ms`, shown-category quality vs `min_quality`). Rank remaining by expected cost, with a quality floor when the shown category is known. Timeout from p95. On 5xx/timeout, next candidate; on 429, skip rather than wait (simulated time is shared). `no_route` with reasons, never an exception.
5. **Replay + scorer + three metrics files.** Drive `t_ms` order on one simulated clock. Same seed, byte-identical output.
6. **Tests.** Billing, rate-limit window, hard filter, retry accounting, one 200-request e2e that pins composite to two decimals.
7. **Tier B, in this order, each measured before/after:**
   - Rate-limit reservation (avoid 429s; cheap composite win, needed for the burst).
   - Live health + circuit breaker (borealis-pro 900–1080 s; target < 20 calls into the dead provider).
   - Budget pacing (initech $5 is the constraint that actually binds).
   - Timeout tuning at three settings.
   - Unknown-category strategy, measured against "treat as chat".
8. **Tier C.** At most one. First choice is **priority/fairness under the 1200–1500 s burst** if Tier A and the first three Tier B items are solid. Otherwise none, with the reason written here. Cascade and streaming are larger than the remaining hours; policy search only if the policy is already parameterized and there is time for seed 43.

Will skip if time runs out, in this order (cut last first):

- Tier C entirely.
- Unknown-category experiment (keep a single defensible default: worst-case quality across the six categories, so `min_quality` is not silently broken).
- Timeout sweep beyond one justified setting.
- Streaming, cascade, offline policy search.

Biggest risk: **non-determinism and billing drift.** Output tokens are `round(expected × U(0.7, 1.3))` and TPM debits input at start and output at completion. If the RNG stream or the clock interleaves differently between runs, metrics will not be byte-identical. Mitigation: one RNG per provider keyed by `(seed, provider, attempt index)`, requests generated from a separate RNG, and the simulator advances only on the simulated clock. Second risk: baselines accidentally share health/budget state with the policy. They get a fresh simulator and no shared router state.

Spec choices I am making now (ambiguous, not a change to prices/limits/incidents/scoring):

- `retry_after_ms` on 429 is the time until the sliding window would admit one more request, floored at 1 ms. Reported on the response.
- RPM events are recorded at call start. TPM input is debited at start; output at successful completion only. A timeout bills input tokens and keeps the input TPM debit; output is not debited and not billed. A 5xx bills nothing and releases the input TPM debit at completion. A 400 does not touch the windows. A 429 does not count against RPM/TPM.
- Incident windows are half-open: `[900, 1080)`, `[1800, 2100)`, `[1500, 1800)`, `[2700, 2760)` seconds from trace start.
- `true_category` is stored on the trace record but stripped before `route()`. The scorer reads it from the trace, not from the router.
- Hard filters use **expected** cost (`input × in + expected_output × out`), not the random actual output. The mine policy multiplies expected cost by 1.3 (the upper end of `U(0.7, 1.3)`) so a served response is unlikely to break `max_cost_usd`. Actual cost can still theoretically violate; that is a scored violation, not a filter bug.
- Shown category `unknown`: v1 uses the minimum quality across the six categories (worst case).
- Replay is single-threaded in arrival order: request *i* is fully resolved, including retries, before request *i+1* is considered. Rate-limit windows still key off simulated timestamps and ignore future-dated events, so a wait on *i* does not move *i+1*'s `t_ms`. This understates in-flight contention. A discrete-event engine would be more faithful; I did not build one.
- Budget overspend penalty applies only when the tenant was already at/over budget *before* the request. The request that crosses the line is not penalised.
- Wall-clock: tests and replay must finish in well under a second. No sleeps.

Dependencies: none. `unittest` is in the stdlib.

## 2. Log

- **2026-10-04 19:30** Plan written. Starting spec + RNG + simulator.
- **2026-10-04 20:00** Simulator in. Independent streams per `(seed, provider, call_index)`. Acklam inverse-normal for latency so draws do not depend on CPython's `random.gauss` algorithm. Rate-limit window is `(now−60s, now]`; future-dated output TPM events are stored but not counted until `now` reaches them.
- **2026-10-04 20:20** Trace generator frozen at seed 42: **5093** requests. File is `data/requests.jsonl`. Will not change the generator after this except for an optional `limit=` used only by tests.
- **2026-10-04 20:40** B-large and B-cheap implemented and frozen. Replay + scorer write the three metrics files. B-large blows initech ($27+) and burns ~$135; B-cheap is cheap and full of `min_quality` violations.
- **2026-10-04 21:10** Router v1: hard filter + rank by expected score `q/10 − 20c` (the scorer's own success term), not cheapest-first. Cheapest-first left ~$45 of acme/globex budget on the table and scored ~1830; value ranking spends that budget on quality.
- **2026-10-04 21:25** Tests: billing, RPM/TPM, hard filter, retry accounting, circuit breaker, 200-request e2e pin, two-run byte identity. `python run.py test` in ~30 ms.
- **2026-10-04 21:40** Circuit breaker first version opened on any 5xx/timeout. Composite dropped ~250 points: local-7b's 15% and ordinary timeouts were tripping it. **Cut:** breaker opens only on `503`. EWMA still penalises 5xx in ranking.
- **2026-10-04 21:50** Budget pacing wired up (`spend_cap`). initech was dumping $5 in the first 1500 s and then going silent. Linear unlock with a $0.0005 cheap floor spreads it across all twelve 300 s buckets and stays under $5.
- **2026-10-04 22:00** Timeout sweep. p50 is a disaster (bills input, fails). p95 is fine. Uncapped 30 s, still min'd with `max_latency_ms`, beats p95 / p99 / 1.5×p95 on this scorer: a timeout bills input and scores `failed`, so waiting is cheap when the request did not set a cap.
- **2026-10-04 22:10** Unknown-category sweep vs treat-as-chat. Treat-as-chat scores highest but picks up 6 silent `min_quality` violations. Worst-case quality is the final choice: 0 violations, and better than the expected-mix alternative on this seed.
- **2026-10-04 22:20** Rate-limit preview first used expected output and dropped future TPM events, so it walked into 429s. Fixed: record actual sampled output at completion time, keep future-dated events. `429`s 244 → 38. Composite moves −33 vs leaving preview off; kept on because a 429 still burns one of three attempts.
- **2026-10-04 22:30** **Tier C: none.** Burst fairness would be the one I'd take with two more days; streaming/cascade/search are larger than the remaining budget and would dilute the foundation. Writing that down rather than starting them.

## 3. Close-out

**Hours spent:** ~11 on 4 October 2026 (one calendar day, compressed against a 5 October morning deadline). The brief's 16–20 over three days is the intended shape; I did not pad the log.

### Per task

| Task | Status |
| --- | --- |
| Simulator (latency, errors, RPM/TPM, billing, incidents, timeouts) | done |
| Trace generator seed 42, frozen jsonl | done |
| B-large / B-cheap, frozen | done |
| Router v1 hard filter + rank + failover + `no_route` | done |
| Replay + scorer + three metrics files | done |
| Tests (billing, window, filter, retry, 200-req pin, identity) | done |
| B1 Live health + circuit breaker | done |
| B2 Budget pacing | done |
| B3 Unknown category | done (measured vs chat) |
| B4 Rate-limit reservation | done (kept on; −33 composite, 244 → 38 `429`s) |
| B5 Timeout tuning, three settings | done (30 s vs p50 / p95 / p99) |
| C1 Streaming failover | not started |
| C2 Priority/fairness queue | not started |
| C3 Cascade + confidence | not started |
| C4 Offline policy search | not started |

### Seed-42 composite (5093 requests)

| policy | composite | notes |
| --- | ---: | --- |
| b-large | −4678.56 | $135, 88 `context_exceeded`, initech $27.37 |
| b-cheap | 677.81 | 1119 `min_quality` violations, $15 |
| **mine** | **2836.99** | 0 region, 0 context_exceeded, 0 min_quality, 0 latency, 0 cost |

Mine vs the better baseline: **+2159.18** against B-cheap.

### Tier B before → after (same trace, same seed; one knob flipped)

**Live health.** `metrics-mine-no-health.json` → `metrics-mine.json`.
Composite 2844.69 → 2836.99 (−7.70). Calls into borealis-pro during 900–1080 s: **39 → 7** (target < 20). Breaker is 503-only; opening on generic 5xx was a net loss.

**Budget pacing.** `metrics-mine-no-budget.json` → `metrics-mine.json`.
Composite 2714.37 → 2836.99 (+122.62). initech $5.28 → $4.80. 300 s buckets for the final policy (initech, USD):

`0.41, 0.31, 0.29, 0.40, 1.25, 0.27, 0.31, 0.31, 0.34, 0.34, 0.31, 0.26`

Not front-loaded. Bucket 4 is the 1200–1500 s burst and is not a dump of the whole $5. acme $11.50 / $40, globex $7.02 / $30.

**Unknown category.** Naive `unknown=chat`: composite 2878.29, **6 min_quality violations**. Expected mix: 2813.90, 0 violations. Worst-case (final): **2836.99, 0 violations**. Files: `metrics-mine-unknown-chat.json`, `metrics-mine-unknown-expected.json`. Chat wins on composite by serving a few requests that violate `min_quality` on the true category; we do not take that trade.

**Rate limits before the call.** `metrics-mine-no-rate-preview.json` → `metrics-mine.json`.
`429` count **186 → 34**. Composite 2848.19 → 2836.99 (−11.20). Kept the preview: a 429 still burns an attempt. The tracker records sampled output tokens and keeps future-dated TPM events.

**Timeout.** Same policy, still min'd with remaining `max_latency_ms`:

| setting | composite | ok | file |
| --- | ---: | ---: | --- |
| p50 | 643.30 | 3517 | `metrics-mine-timeout-p50.json` |
| p95 | 2733.81 | 4461 | `metrics-mine-timeout-p95.json` |
| 30 s (final) | 2836.99 | 4496 | `metrics-mine.json` |
| p99 | 2840.08 | 4501 | `metrics-mine-timeout-p99.json` |
| 1.5×p95 | 2853.24 | 4509 | `metrics-mine-timeout-p95x1.5.json` |

p50 times out the body of the distribution, bills input, and lands in `failed`. 1.5×p95 scored 16 points above 30 s on this seed. Left at 30 s: the scorer does not punish waiting, only a timeout, and 16 points is inside seed noise I would not want to overfit.

### Two more days, in order

1. Discrete-event replay so in-flight calls contend for RPM/TPM for real. The sequential clock understates the 1200–1500 s burst.
2. Tier C2: a queue in front of the router, early-reject past `max_latency_ms`, per-priority p95, tenant fairness. That is the product failure mode in the brief (Indian customers + code review) under load.
3. Re-tune the cost margin and the cheap-floor with a tiny grid on seed 42, then confirm on seed 43. If the gain vanishes, say so.

### Known bugs / limits

- Sequential replay understates concurrent rate-limit contention (see spec choices).
- `local-7b` is almost unused: 8k context plus low quality makes it fail the filter on most constrained requests. That is intended, not a bug.
- 220 `failed` on mine are mostly exhausted failovers (5xx then timeout on the backup), not uncaught exceptions.
- B-large `context_exceeded` 88: the baseline is specified not to hard-filter context. Not a harness bug.

### How I used AI tools

Cursor (Grok 4.6) wrote the first draft of the simulator, router, replay, tests, and this file from the brief. I directed the order of work, the spec choices above, the ranking formula (`q/10 − 20c`), the 503-only breaker after the 5xx version lost 250 composite, the 1.3× cost margin, and which Tier B knobs to keep. I ran the replay, read the before/after numbers, and changed the policy when the numbers said the first version was wrong. I own every line; a follow-up conversation can ask about any of them.

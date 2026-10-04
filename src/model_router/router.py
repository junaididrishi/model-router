"""Routing policies.

B-large and B-cheap are frozen: they do not read health, budgets, or rate-limit
state. The `mine` policy may.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .simulator import CallResponse, Simulator
from .spec import PRIORITY_WEIGHT, PROVIDERS, ProviderSpec, expected_cost, quality_for


@dataclass
class Attempt:
    provider: str
    started_ms: int
    latency_ms: int
    cost_usd: float
    result: str

    def to_json(self) -> dict:
        return {
            "provider": self.provider,
            "started_ms": self.started_ms,
            "latency_ms": self.latency_ms,
            "cost_usd": round(self.cost_usd, 10),
            "result": self.result,
        }


@dataclass
class Outcome:
    request_id: str
    attempts: list[Attempt]
    final: str  # ok | failed | no_route
    provider: str | None
    total_latency_ms: int
    total_cost_usd: float
    violations: list[str]
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "request_id": self.request_id,
            "attempts": [a.to_json() for a in self.attempts],
            "final": self.final,
            "provider": self.provider,
            "total_latency_ms": self.total_latency_ms,
            "total_cost_usd": self.total_cost_usd,
            "violations": list(self.violations),
            "reasons": list(self.reasons),
        }


def _constraints(request: dict) -> dict:
    return request.get("constraints") or {}


def hard_filter(
    request: dict,
    *,
    use_cost: bool = True,
    use_latency: bool = True,
    use_quality: bool = True,
    unknown: str = "worst",
    enforce_region: bool = True,
    cost_margin: float = 1.0,
) -> tuple[list[ProviderSpec], list[str]]:
    """Eligible providers and, if none, the rejection reasons that fired.

    Reasons are de-duplicated in first-seen order. A request is `no_route`
    only when the filter leaves nobody, never via an exception.
    """
    c = _constraints(request)
    region = c.get("region")
    required = set(c.get("required") or [])
    max_cost = c.get("max_cost_usd")
    max_latency = c.get("max_latency_ms")
    min_q = c.get("min_quality")
    shown = request["category"]
    inp = request["input_tokens"]
    out = request["expected_output_tokens"]

    eligible: list[ProviderSpec] = []
    rejected: list[str] = []

    for spec in PROVIDERS.values():
        reason = None
        if inp + out > spec.context:
            reason = "context"
        elif not required.issubset(spec.capabilities):
            reason = "capability"
        elif enforce_region and region and region not in spec.regions:
            reason = "region"
        elif use_cost and max_cost is not None:
            if expected_cost(spec, inp, out) * cost_margin > max_cost + 1e-12:
                reason = "cost"
        if reason is None and use_latency and max_latency is not None:
            if spec.p50_ms > max_latency:
                reason = "latency"
        if reason is None and use_quality and min_q is not None:
            if quality_for(spec, shown, unknown) + 1e-9 < min_q:
                reason = "min_quality"
        if reason is None:
            eligible.append(spec)
        elif reason not in rejected:
            rejected.append(reason)

    reasons = [] if eligible else rejected
    return eligible, reasons


class Router:
    def __init__(
        self,
        simulator: Simulator,
        policy: str = "mine",
        unknown: str = "worst",
        use_health: bool = True,
        use_budget: bool = True,
        use_rate_preview: bool = True,
        timeout_mode: str = "30s",
    ):
        self.sim = simulator
        self.policy = policy
        self.unknown = unknown
        self.use_health = use_health
        self.use_budget = use_budget
        self.use_rate_preview = use_rate_preview
        self.timeout_mode = timeout_mode
        self._health = Health()
        self._budgets = TenantBudgets()
        self._limits = LimitTracker()

    def route(self, request: dict, now_ms: int) -> Outcome:
        if self.policy == "b-large":
            return self._baseline_fixed(request, now_ms, retries=3)
        if self.policy == "b-cheap":
            return self._baseline_cheap(request, now_ms)
        return self._mine(request, now_ms)

    def _baseline_fixed(self, request: dict, now_ms: int, retries: int) -> Outcome:
        region = _constraints(request).get("region")
        name = "atlas-large-in" if region == "in" else "atlas-large"
        return self._run_candidates(
            request,
            now_ms,
            candidates=[PROVIDERS[name]],
            per_provider_retries=retries,
            timeout_ms=30_000,
            on_429="wait",
            reasons_if_empty=[],
        )

    def _baseline_cheap(self, request: dict, now_ms: int) -> Outcome:
        eligible, reasons = hard_filter(
            request,
            use_cost=False,
            use_latency=False,
            use_quality=False,
            enforce_region=True,
        )
        eligible.sort(
            key=lambda s: (
                expected_cost(s, request["input_tokens"], request["expected_output_tokens"]),
                s.name,
            )
        )
        if not eligible:
            return _no_route(request, reasons)
        return self._run_candidates(
            request,
            now_ms,
            candidates=eligible[:1],
            per_provider_retries=1,
            timeout_ms=30_000,
            on_429="wait",
            reasons_if_empty=reasons,
        )

    def _mine(self, request: dict, now_ms: int) -> Outcome:
        eligible, reasons = hard_filter(
            request,
            use_cost=True,
            use_latency=True,
            use_quality=True,
            unknown=self.unknown,
            enforce_region=True,
            cost_margin=1.3,
        )

        cap = float("inf")
        if self.use_budget:
            cap = self._budgets.spend_cap(request["tenant"], now_ms)
            max_cost = _constraints(request).get("max_cost_usd")
            if max_cost is not None:
                cap = min(cap, max_cost)

        inp = request["input_tokens"]
        out = request["expected_output_tokens"]
        ranked: list[ProviderSpec] = []
        skipped_health = False
        for spec in eligible:
            if self.use_health and not self._health.allow(spec.name, now_ms):
                skipped_health = True
                continue
            if self.use_rate_preview and not self._limits.can_admit(spec, inp, now_ms):
                continue
            if expected_cost(spec, inp, out) > cap + 1e-12:
                continue
            ranked.append(spec)

        if not ranked:
            # Nothing healthy and under the rate preview. Fall back to anyone
            # who still fits the budget, including an open breaker: better to
            # probe the only eligible provider than to no_route while it is
            # the unique match (1M-context + tools).
            for spec in eligible:
                if expected_cost(spec, inp, out) > cap + 1e-12:
                    continue
                ranked.append(spec)
            if not ranked:
                why = list(reasons) if not eligible else []
                if skipped_health and "health" not in why:
                    why.append("health")
                if self.use_budget and cap <= 1e-12 and "budget" not in why:
                    why.append("budget")
                if not why:
                    why = ["rate_limit"]
                return _no_route(request, why)

        w = PRIORITY_WEIGHT[request["priority"]]

        def rank_key(spec: ProviderSpec):
            cost = expected_cost(spec, inp, out)
            q = quality_for(spec, request["category"], self.unknown)
            health_pen = self._health.penalty(spec.name) if self.use_health else 0.0
            # Scorer success term is (q/10)*w - 20*c. Priority scales quality, not cost.
            value = (q / 10.0) * w - 20.0 * cost - health_pen
            return (-value, spec.name)

        ranked.sort(key=rank_key)
        ranked = ranked[:4]

        # A sure success that scores below no_route is not worth the spend.
        # Failure is worse still, so this check is the optimistic case.
        best = ranked[0]
        best_value = (quality_for(best, request["category"], self.unknown) / 10.0) * w - 20.0 * expected_cost(
            best, inp, out
        )
        if best_value < -0.5 * w:
            return _no_route(request, ["cost"])

        outcome = self._run_candidates(
            request,
            now_ms,
            candidates=ranked,
            per_provider_retries=0,
            timeout_ms=None,
            on_429="skip",
            reasons_if_empty=["rate_limit"],
            failover=True,
            total_attempts=3,
        )
        if self.use_budget:
            self._budgets.note(request["tenant"], outcome.total_cost_usd)
        return outcome

    def _run_candidates(
        self,
        request: dict,
        now_ms: int,
        candidates: list[ProviderSpec],
        per_provider_retries: int,
        timeout_ms: int | None,
        on_429: str,
        reasons_if_empty: list[str],
        failover: bool = False,
        total_attempts: int | None = None,
    ) -> Outcome:
        attempts: list[Attempt] = []
        clock = now_ms
        required = list(_constraints(request).get("required") or [])
        inp = request["input_tokens"]
        out = request["expected_output_tokens"]

        if not candidates:
            return _no_route(request, reasons_if_empty)

        made = 0
        cap = total_attempts if total_attempts is not None else 10**9

        for spec in candidates:
            budget = 1 if failover else per_provider_retries + 1
            used_here = 0
            while used_here < budget and made < cap:
                to = timeout_ms if timeout_ms is not None else _timeout_for(
                    request, spec, self.timeout_mode, elapsed_ms=clock - now_ms
                )
                if to is None:
                    # Latency budget already spent. Another call would time out
                    # almost immediately and still bill input tokens.
                    return _finish(request, attempts, "failed", None, clock - now_ms) if attempts else _no_route(
                        request, reasons_if_empty or ["latency"]
                    )
                started = clock
                resp: CallResponse = self.sim.call(spec.name, inp, out, required, to, started)
                attempts.append(
                    Attempt(spec.name, started, resp.latency_ms, resp.cost_usd, resp.result)
                )
                if self.policy == "mine":
                    self._record(resp, inp, started)
                made += 1
                used_here += 1
                clock += resp.latency_ms
                if resp.result == "ok":
                    return _finish(request, attempts, "ok", spec.name, clock - now_ms)
                if resp.result == "429" and on_429 == "wait":
                    if used_here < budget and made < cap:
                        clock += resp.retry_after_ms
                    continue
                if resp.result in ("context_exceeded", "unsupported"):
                    break
                if resp.result == "429" and on_429 == "skip":
                    break

        if not attempts:
            return _no_route(request, reasons_if_empty or ["no_candidate"])
        return _finish(request, attempts, "failed", None, clock - now_ms)

    def _record(self, resp: CallResponse, input_tokens: int, started_ms: int) -> None:
        """Update health and the rate preview from a call the simulator actually made.

        503s are not recorded: the simulator does not put an outage response
        in the RPM/TPM window. Output tokens are the actual sample, not the
        request's expected count, so the preview stays aligned with billing.
        """
        if self.use_health:
            self._health.observe(resp.provider, resp.result, resp.latency_ms, started_ms)
        if not self.use_rate_preview:
            return
        if resp.result not in ("ok", "timeout", "5xx"):
            return
        self._limits.note_start(resp.provider, input_tokens, started_ms)
        if resp.result == "ok":
            self._limits.note_output(resp.provider, resp.output_tokens, started_ms + resp.latency_ms)
        elif resp.result == "5xx":
            # Input was held for the attempt and released when it failed.
            self._limits.note_output(resp.provider, -input_tokens, started_ms + resp.latency_ms)


def _timeout_for(
    request: dict, spec: ProviderSpec, mode: str = "p95", elapsed_ms: int = 0
) -> int | None:
    """Per-call timeout, never past the request's remaining latency budget.

    A timeout bills input tokens, so an unconstrained request waits 30s
    (same as the baselines) rather than abandoning the tail of the latency
    distribution. A request with max_latency_ms is capped at the time left,
    which keeps a successful attempt inside the cap.
    """
    cap = _constraints(request).get("max_latency_ms")
    # Quantiles of the fitted log-normal. z=0 is p50 by construction.
    quantile_z = {
        "p50": 0.0,
        "p95": 1.6448536269514722,
        "p99": 2.3263478740408408,
    }
    if mode == "30s":
        # The score does not punish waiting, only a timeout (input tokens
        # billed, request failed). With no latency cap, wait the baseline's 30s.
        target = 30_000.0
    elif mode == "p95x1.5":
        target = spec.p95_ms * 1.5
    elif mode in quantile_z:
        target = math.exp(spec.mu + spec.sigma * quantile_z[mode])
    else:
        target = spec.p95_ms
    if cap is not None:
        remaining = cap - elapsed_ms
        if remaining < 50:
            return None
        target = min(target, remaining)
    return max(50, int(target))


def _finish(
    request: dict,
    attempts: list[Attempt],
    final: str,
    provider: str | None,
    elapsed_ms: int,
) -> Outcome:
    return Outcome(
        request_id=request["id"],
        attempts=attempts,
        final=final,
        provider=provider,
        total_latency_ms=elapsed_ms,
        total_cost_usd=sum(a.cost_usd for a in attempts),
        violations=[],
    )


def _no_route(request: dict, reasons: list[str]) -> Outcome:
    return Outcome(
        request_id=request["id"],
        attempts=[],
        final="no_route",
        provider=None,
        total_latency_ms=0,
        total_cost_usd=0.0,
        violations=[],
        reasons=reasons,
    )


class Health:
    """EWMA error rate and a circuit breaker.

    Opens after 3 `503`s inside 15 s (the borealis-pro outage). Half-open
    after 30 s. `allow` is a pure query: ranking a provider without calling
    it does not consume the probe. Ordinary 5xx updates the error EWMA
    only. Timeouts do not. Neither opens the breaker.
    """

    OPEN_AFTER = 3
    FAIL_WINDOW_MS = 15_000
    COOLDOWN_MS = 30_000

    def __init__(self) -> None:
        self.ewma_error: dict[str, float] = {}
        self.ewma_latency: dict[str, float] = {}
        self._failures: dict[str, list[int]] = {n: [] for n in PROVIDERS}
        self._open_until: dict[str, int] = {}

    def allow(self, name: str, now_ms: int) -> bool:
        until = self._open_until.get(name)
        if until is None:
            return True
        return now_ms >= until

    def observe(self, name: str, result: str, latency_ms: int, now_ms: int) -> None:
        # Timeouts are the router's budget, not evidence the provider is sick,
        # so they do not move the error EWMA in either direction.
        if result in ("ok", "5xx", "503"):
            failed = result != "ok"
            prev_e = self.ewma_error.get(name, 0.0)
            self.ewma_error[name] = 0.2 * (1.0 if failed else 0.0) + 0.8 * prev_e
        if result == "ok":
            prev_l = self.ewma_latency.get(name, float(latency_ms))
            self.ewma_latency[name] = 0.2 * latency_ms + 0.8 * prev_l
            self._open_until.pop(name, None)
            self._failures[name] = []
            return

        # Only 503 opens the breaker. A 5% base error rate would otherwise
        # hold local-7b and nimbus-code open for the whole trace.
        if result != "503":
            return

        if name in self._open_until:
            self._open_until[name] = now_ms + self.COOLDOWN_MS
            return

        window = [t for t in self._failures[name] if now_ms - t <= self.FAIL_WINDOW_MS]
        window.append(now_ms)
        self._failures[name] = window
        if len(window) >= self.OPEN_AFTER:
            self._open_until[name] = now_ms + self.COOLDOWN_MS
            self._failures[name] = []

    def penalty(self, name: str) -> float:
        # Subtracted from expected value. Kept small: a 0.4 weight made the
        # router avoid borealis-pro all hour because of its 5% base error,
        # which cost more composite than the outage. The breaker, not this
        # penalty, is what stops the 503 storm.
        return 0.05 * self.ewma_error.get(name, 0.0)


class TenantBudgets:
    """Hard stop at the tenant budget. Pacing keeps a reserve for later time.

    The trace is 3600 s. Allowed spend is budget * (0.20 + 0.80 * elapsed/3600),
    never past the budget itself. When we are ahead of pace, only cheap calls
    still go through so initech's $5 is not dumped in the first 20 minutes.
    """

    CHEAP_FLOOR = 0.0005

    def __init__(self, budgets: dict[str, float] | None = None, horizon_ms: int = 3_600_000) -> None:
        from .spec import TENANT_BUDGETS

        self.limit = dict(budgets or TENANT_BUDGETS)
        self.spent = {k: 0.0 for k in self.limit}
        self.horizon_ms = horizon_ms

    def residual(self, tenant: str) -> float:
        return max(0.0, self.limit.get(tenant, 0.0) - self.spent.get(tenant, 0.0))

    def paced_residual(self, tenant: str, now_ms: int) -> float:
        limit = self.limit.get(tenant, 0.0)
        spent = self.spent.get(tenant, 0.0)
        progress = min(1.0, max(0.0, now_ms / self.horizon_ms))
        allowed = limit * (0.20 + 0.80 * progress)
        return max(0.0, min(limit - spent, allowed - spent))

    def spend_cap(self, tenant: str, now_ms: int) -> float:
        hard = self.residual(tenant)
        if hard <= 1e-12:
            return 0.0
        paced = self.paced_residual(tenant, now_ms)
        return max(paced, min(hard, self.CHEAP_FLOOR))

    def blocked(self, tenant: str) -> bool:
        return self.spent.get(tenant, 0.0) >= self.limit.get(tenant, 0.0) - 1e-9

    def note(self, tenant: str, cost: float) -> None:
        self.spent[tenant] = self.spent.get(tenant, 0.0) + cost


class LimitTracker:
    """Router-side view of RPM and TPM. Mirrors the simulator window rules."""

    def __init__(self) -> None:
        self._rpm: dict[str, list[int]] = {n: [] for n in PROVIDERS}
        self._tpm: dict[str, list[tuple[int, int]]] = {n: [] for n in PROVIDERS}

    def _expire(self, name: str, now_ms: int) -> None:
        # Keep future-dated events. A completion scheduled after this instant
        # still has to count once simulated time reaches it. Dropping it here
        # was how the preview walked into TPM 429s during the burst.
        cutoff = now_ms - 60_000
        self._rpm[name] = [t for t in self._rpm[name] if t > cutoff]
        self._tpm[name] = [(t, tok) for t, tok in self._tpm[name] if t > cutoff]

    def _rpm_cap(self, spec: ProviderSpec, now_ms: int) -> int:
        if spec.name == "atlas-large" and 1_800_000 <= now_ms < 2_100_000:
            return 20
        return spec.rpm

    def _used(self, name: str, now_ms: int) -> tuple[int, int]:
        self._expire(name, now_ms)
        rpm = sum(1 for t in self._rpm[name] if t <= now_ms)
        tpm = sum(tok for t, tok in self._tpm[name] if t <= now_ms)
        return rpm, tpm

    def can_admit(self, spec: ProviderSpec, input_tokens: int, now_ms: int) -> bool:
        rpm, tpm = self._used(spec.name, now_ms)
        if rpm >= self._rpm_cap(spec, now_ms):
            return False
        return tpm + input_tokens <= spec.tpm

    def headroom(self, spec: ProviderSpec, now_ms: int) -> float:
        rpm, _ = self._used(spec.name, now_ms)
        cap = self._rpm_cap(spec, now_ms)
        return max(0, cap - rpm) / cap

    def note_start(self, name: str, input_tokens: int, now_ms: int) -> None:
        self._rpm[name].append(now_ms)
        self._tpm[name].append((now_ms, input_tokens))

    def note_output(self, name: str, tokens: int, at_ms: int) -> None:
        self._tpm[name].append((at_ms, tokens))

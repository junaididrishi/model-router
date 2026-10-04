"""Seeded provider simulator. Simulated time only; no network, no sleeps."""

from __future__ import annotations

import math
import random
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .rng import stream
from .spec import PROVIDERS, ProviderSpec

WINDOW_MS = 60_000


@dataclass
class CallResponse:
    provider: str
    result: str  # ok | 5xx | 429 | timeout | context_exceeded | unsupported | 503
    latency_ms: int
    cost_usd: float
    output_tokens: int
    retry_after_ms: int = 0
    sampled_latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.result == "ok"


@dataclass
class _Window:
    rpm_times: deque = field(default_factory=deque)
    tpm_events: deque = field(default_factory=deque)  # (time_ms, tokens); negative = release


def _in_window(ts: int, now_ms: int) -> bool:
    """Sliding 60 s window: (now - 60_000, now]. Future timestamps do not count."""
    return now_ms - WINDOW_MS < ts <= now_ms


@dataclass
class Simulator:
    """One simulator instance owns the rate-limit windows and the incident clock.

    Each call draws from a stream keyed by (seed, provider, call_index), so the
    outcome of call N to a provider does not depend on other providers.
    """

    seed: int = 42

    def __post_init__(self) -> None:
        self._windows: dict[str, _Window] = {name: _Window() for name in PROVIDERS}
        self._call_index: dict[str, int] = {name: 0 for name in PROVIDERS}
        self.context_exceeded_calls = 0
        self.calls_by_provider: dict[str, int] = {name: 0 for name in PROVIDERS}
        self.result_counts: dict[str, int] = defaultdict(int)

    def call(
        self,
        provider: str,
        input_tokens: int,
        expected_output_tokens: int,
        required: list[str] | tuple[str, ...],
        timeout_ms: int,
        now_ms: int,
    ) -> CallResponse:
        spec = PROVIDERS[provider]
        idx = self._call_index[provider]
        self._call_index[provider] = idx + 1
        self.calls_by_provider[provider] += 1
        rng = stream(self.seed, "call", provider, idx)

        required_set = set(required)
        if not required_set.issubset(spec.capabilities):
            return self._finish(CallResponse(provider, "unsupported", 0, 0.0, 0))

        if input_tokens + expected_output_tokens > spec.context:
            self.context_exceeded_calls += 1
            return self._finish(CallResponse(provider, "context_exceeded", 0, 0.0, 0))

        t_s = now_ms / 1000.0
        if spec.name == "borealis-pro" and 900.0 <= t_s < 1080.0:
            # Incident: every call is 503. Does not enter the rate window: the
            # provider is down, not busy. Counting it as RPM would hide the
            # outage behind 429s, which is not what the brief describes.
            return self._finish(CallResponse(spec.name, "503", 0, 0.0, 0))

        rpm_cap = spec.rpm
        if spec.name == "atlas-large" and 1800.0 <= t_s < 2100.0:
            rpm_cap = 20

        window = self._windows[provider]
        self._gc(window, now_ms)

        rpm_used = sum(1 for t in window.rpm_times if _in_window(t, now_ms))
        tpm_used = sum(tok for t, tok in window.tpm_events if _in_window(t, now_ms))

        if rpm_used >= rpm_cap or tpm_used + input_tokens > spec.tpm:
            retry_after = self._retry_after(
                window, now_ms, rpm_used >= rpm_cap, tpm_used, input_tokens, spec.tpm
            )
            return self._finish(
                CallResponse(provider, "429", 0, 0.0, 0, retry_after_ms=retry_after)
            )

        window.rpm_times.append(now_ms)
        window.tpm_events.append((now_ms, input_tokens))

        latency = self._sample_latency(spec, rng, t_s)
        output_tokens = int(round(expected_output_tokens * rng.uniform(0.7, 1.3)))
        output_tokens = max(0, output_tokens)

        error_rate = spec.error_rate
        if spec.name == "borealis-flash" and 2700.0 <= t_s < 2760.0:
            error_rate = 0.30

        if rng.random() < error_rate:
            # 5xx costs nothing. Input TPM is released at completion.
            window.tpm_events.append((now_ms + latency, -input_tokens))
            return self._finish(
                CallResponse(provider, "5xx", latency, 0.0, 0, sampled_latency_ms=latency)
            )

        if latency > timeout_ms:
            cost = input_tokens * spec.in_per_mtok / 1_000_000
            return self._finish(
                CallResponse(
                    provider, "timeout", timeout_ms, cost, 0, sampled_latency_ms=latency
                )
            )

        window.tpm_events.append((now_ms + latency, output_tokens))
        cost = (
            input_tokens * spec.in_per_mtok + output_tokens * spec.out_per_mtok
        ) / 1_000_000
        return self._finish(
            CallResponse(
                provider, "ok", latency, cost, output_tokens, sampled_latency_ms=latency
            )
        )

    def _finish(self, resp: CallResponse) -> CallResponse:
        self.result_counts[resp.result] += 1
        return resp

    def _sample_latency(self, spec: ProviderSpec, rng: random.Random, t_s: float) -> int:
        z = inv_norm(rng.random())
        ms = math.exp(spec.mu + spec.sigma * z)
        if spec.name == "local-7b" and 1500.0 <= t_s < 1800.0:
            ms *= 2.5
        return max(1, int(round(ms)))

    def _gc(self, window: _Window, now_ms: int) -> None:
        """Drop events older than any time we will query. Keep future-dated ones."""
        cutoff = now_ms - WINDOW_MS
        window.rpm_times = deque(t for t in window.rpm_times if t > cutoff)
        window.tpm_events = deque((t, tok) for t, tok in window.tpm_events if t > cutoff)

    def _retry_after(
        self,
        window: _Window,
        now_ms: int,
        rpm_blocked: bool,
        tpm_used: int,
        input_tokens: int,
        tpm_cap: int,
    ) -> int:
        """Milliseconds until the sliding window would admit this call. At least 1."""
        waits = []
        if rpm_blocked:
            in_win = [t for t in window.rpm_times if _in_window(t, now_ms)]
            if in_win:
                waits.append(in_win[0] + WINDOW_MS - now_ms + 1)
        if tpm_used + input_tokens > tpm_cap:
            need = tpm_used + input_tokens - tpm_cap
            freed = 0
            events = [(t, tok) for t, tok in window.tpm_events if _in_window(t, now_ms)]
            for ts, tok in events:
                if tok > 0:
                    freed += tok
                if freed >= need:
                    waits.append(ts + WINDOW_MS - now_ms + 1)
                    break
        if not waits:
            return 1
        return max(1, int(max(waits)))


def inv_norm(u: float) -> float:
    """Acklam's inverse normal. u in (0, 1) -> standard normal quantile.

    Using a closed form keeps latency draws identical across Python versions,
    unlike random.gauss, whose algorithm is not stable across releases.
    """
    if u <= 0.0:
        u = 1e-12
    elif u >= 1.0:
        u = 1.0 - 1e-12

    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    plow = 0.02425
    phigh = 1 - plow
    if u < plow:
        q = math.sqrt(-2 * math.log(u))
        return (
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    if u > phigh:
        q = math.sqrt(-2 * math.log(1 - u))
        return -(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    q = u - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )

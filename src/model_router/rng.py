"""Deterministic RNG streams. Same seed and same call sequence => same draws."""

from __future__ import annotations

import hashlib
import random


def stream(seed: int, *parts: object) -> random.Random:
    """Independent stream so provider draws do not depend on other providers' traffic."""
    key = "|".join(str(p) for p in (seed, *parts))
    digest = hashlib.sha256(key.encode()).digest()
    n = int.from_bytes(digest[:8], "big")
    return random.Random(n)

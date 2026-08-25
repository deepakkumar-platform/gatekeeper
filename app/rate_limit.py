"""
Token-bucket rate limiter, in-memory, per app.

Each app gets a bucket sized to its `rate_limit_per_min`. Tokens regenerate
linearly at rate_limit_per_min / 60 per second. A request costs 1 token.

This is deliberately in-process: one gateway instance, one bucket per app.
If the gateway scales to N replicas later we move this into Redis (the API
stays the same so callers don't change).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class Bucket:
    capacity: float
    tokens: float
    refill_per_sec: float
    last_refill: float


_lock = threading.Lock()
_buckets: dict[str, Bucket] = {}


def _get_or_create(app_name: str, rate_per_min: int) -> Bucket:
    now = time.monotonic()
    bucket = _buckets.get(app_name)
    capacity = float(rate_per_min)
    refill_per_sec = capacity / 60.0
    if bucket is None:
        bucket = Bucket(
            capacity=capacity,
            tokens=capacity,
            refill_per_sec=refill_per_sec,
            last_refill=now,
        )
        _buckets[app_name] = bucket
        return bucket

    if bucket.capacity != capacity:
        bucket.capacity = capacity
        bucket.refill_per_sec = refill_per_sec
        bucket.tokens = min(bucket.tokens, capacity)
    return bucket


def try_consume(app_name: str, rate_per_min: int, cost: float = 1.0) -> tuple[bool, float]:
    """
    Try to consume `cost` tokens from this app's bucket.

    Returns (allowed, retry_after_seconds). If not allowed, retry_after is
    the number of seconds the caller should wait before retrying.
    """
    with _lock:
        bucket = _get_or_create(app_name, rate_per_min)
        now = time.monotonic()
        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_sec)
        bucket.last_refill = now
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True, 0.0
        needed = cost - bucket.tokens
        retry_after = needed / bucket.refill_per_sec if bucket.refill_per_sec > 0 else 60.0
        return False, retry_after

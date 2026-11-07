"""Bounded token buckets with reservations for concurrent authentication checks."""

from collections import OrderedDict
from dataclasses import dataclass
import math
import threading
import time
from typing import Callable

from junkyard.config import Settings


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)


@dataclass
class Bucket:
    tokens: float
    updated: float
    touched: float
    pending: int = 0


class TokenBuckets:
    def __init__(self, rate: float, burst: int, max_keys: int, *, clock: Callable[[], float] = time.monotonic):
        self.rate = rate
        self.burst = burst
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, Bucket] = OrderedDict()
        self._lock = threading.Lock()
        self._idle_seconds = max(60.0, burst / rate)

    def _refill(self, bucket: Bucket, now: float) -> None:
        bucket.tokens = min(self.burst, bucket.tokens + max(0.0, now - bucket.updated) * self.rate)
        bucket.updated = now

    def take(self, key: str, *, reserve: bool = False) -> Bucket:
        with self._lock:
            now = self._clock()
            if key not in self._buckets:
                # Evict only idle, fully replenished entries. Churning IP addresses
                # must not evict a blocked client and reset its limit.
                while self._buckets:
                    oldest = next(iter(self._buckets.values()))
                    if oldest.pending or now - oldest.touched < self._idle_seconds:
                        break
                    self._buckets.popitem(last=False)
                if len(self._buckets) >= self.max_keys:
                    oldest = next(iter(self._buckets.values()))
                    raise RateLimited(math.ceil(max(1.0, self._idle_seconds - (now - oldest.touched))))
                self._buckets[key] = Bucket(float(self.burst), now, now)
            bucket = self._buckets[key]
            self._buckets.move_to_end(key)
            self._refill(bucket, now)
            bucket.touched = now
            if bucket.tokens < 1:
                raise RateLimited(math.ceil((1 - bucket.tokens) / self.rate))
            bucket.tokens -= 1
            if reserve:
                bucket.pending += 1
            return bucket

    def settle(self, bucket: Bucket, *, refund: bool) -> None:
        with self._lock:
            self._refill(bucket, self._clock())
            if refund:
                bucket.tokens = min(float(self.burst), bucket.tokens + 1)
            bucket.pending -= 1


class RequestLimits:
    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.monotonic):
        self.anonymous = TokenBuckets(settings.anonymous_rate, settings.anonymous_burst,
                                      settings.rate_limit_max_ips, clock=clock)
        self.authentication = TokenBuckets(settings.auth_failures_per_minute / 60,
                                           settings.auth_failures_per_minute,
                                           settings.rate_limit_max_ips, clock=clock)

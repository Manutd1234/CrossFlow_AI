"""Bounded, single-flight cache for expensive read-only observed evaluation."""

from copy import deepcopy
import threading
import time
from typing import Callable, Any


class EvaluationBusy(RuntimeError):
    pass


class ObservedEvaluationRuntime:
    """Keep one report briefly; never cache errors or queue concurrent fits."""

    def __init__(self, *, ttl_seconds: float = 60.0, monotonic=time.monotonic):
        self._ttl = ttl_seconds
        self._clock = monotonic
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cached = None

    def evaluate(self, key: str, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._cache_lock:
            cached = self._cached
            if cached is not None and cached[0] == key and self._clock() - cached[1] < self._ttl:
                return deepcopy(cached[2])
        if not self._lock.acquire(blocking=False):
            raise EvaluationBusy("An observed evaluation is already running.")
        try:
            # Another request may have completed between the cache check and lock.
            with self._cache_lock:
                cached = self._cached
                if cached is not None and cached[0] == key and self._clock() - cached[1] < self._ttl:
                    return deepcopy(cached[2])
            report = build()
            with self._cache_lock:
                self._cached = (key, self._clock(), deepcopy(report))
            return report
        finally:
            self._lock.release()

    def invalidate(self) -> None:
        with self._cache_lock:
            self._cached = None


RUNTIME = ObservedEvaluationRuntime()

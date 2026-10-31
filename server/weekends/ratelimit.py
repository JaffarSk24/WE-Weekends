"""In-process sliding-window rate limits. nginx limits per IP first; these
limits sit behind it and also cover per-account and per-chat keys."""
from __future__ import annotations

import threading
import time
from collections import deque

# name: list of (max hits, window seconds)
RULES: dict[str, list[tuple[int, int]]] = {
    "ip": [(120, 60), (1500, 3600)],
    "auth_start": [(5, 60), (20, 3600), (60, 86400)],
    "auth_verify": [(10, 60), (40, 3600)],
    "account": [(60, 60), (600, 3600)],
    "save": [(10, 60), (120, 3600)],
    "chat": [(20, 60), (200, 3600)],
    "login_chat": [(5, 600), (20, 86400)],
}

_MAX_KEYS = 100_000


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def allow(self, rule: str, key: str) -> bool:
        limits = RULES[rule]
        longest = max(window for _, window in limits)
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            hits = self._hits.setdefault((rule, key), deque())
            while hits and now - hits[0] > longest:
                hits.popleft()
            for limit, window in limits:
                recent = sum(1 for t in hits if now - t <= window)
                if recent >= limit:
                    return False
            hits.append(now)
            return True

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < 300 and len(self._hits) < _MAX_KEYS:
            return
        self._last_sweep = now
        for key in list(self._hits):
            rule = key[0]
            longest = max(window for _, window in RULES[rule])
            hits = self._hits[key]
            if not hits or now - hits[-1] > longest:
                del self._hits[key]
        if len(self._hits) >= _MAX_KEYS:
            # Under a flood of distinct keys drop everything rather than grow
            # without bound; nginx and fail2ban still hold the line.
            self._hits.clear()

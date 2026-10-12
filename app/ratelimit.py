"""Minimal in-process rate limiter for the ingest endpoint.

Single host, single bearer token, so a global gate is both simpler and stricter
than a per-IP bucket: one shared budget that no client can widen by rotating
source addresses.

In-memory on purpose — the only party who could clear it by restarting the process
is the operator, and persistence would add a dependency for no real gain.
"""

from __future__ import annotations

import threading
import time


class MinIntervalLimiter:
    """Allows at most one request per ``min_interval_s`` seconds."""

    def __init__(self, min_interval_s: int) -> None:
        self.min_interval_s = max(0, int(min_interval_s))
        self._last: float | None = None
        self._lock = threading.Lock()
        self.rejected = 0

    @property
    def enabled(self) -> bool:
        return self.min_interval_s > 0

    def check(self) -> float:
        """Return ``0.0`` when the request may proceed, else seconds to wait.

        Every authenticated attempt consumes the slot, valid or not: otherwise a
        flood of malformed payloads would never trip the limiter.
        """
        if not self.enabled:
            return 0.0

        now = time.monotonic()
        with self._lock:
            if self._last is None:
                self._last = now
                return 0.0
            elapsed = now - self._last
            if elapsed < self.min_interval_s:
                self.rejected += 1
                return self.min_interval_s - elapsed
            self._last = now
            return 0.0

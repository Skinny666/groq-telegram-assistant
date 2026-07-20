from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import time


@dataclass(slots=True)
class SlidingWindowRateLimiter:
    max_events: int = 20
    window_seconds: float = 60.0
    _events: deque[float] = field(default_factory=deque, init=False, repr=False)

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds

        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

        if len(self._events) >= self.max_events:
            return False

        self._events.append(current)
        return True

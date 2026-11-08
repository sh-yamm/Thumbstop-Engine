"""Requests-per-minute limiter shared by every thread (NVIDIA's free tier allows about 40 a minute)."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Sliding one-minute window: wait() returns once another request fits under the limit."""

    def __init__(self, rpm: int):
        self.rpm, self.calls, self.lock = rpm, [], threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                self.calls = [t for t in self.calls if now - t < 60]
                if len(self.calls) < self.rpm:
                    self.calls.append(now)
                    return
                sleep_for = 60 - (now - self.calls[0]) + 0.05
            time.sleep(sleep_for)

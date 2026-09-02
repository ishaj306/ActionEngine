"""Per-user rate limits.

In-process, and therefore per-replica: two instances behind a load balancer
each allow the full quota. That is a real limitation and the honest place for
it to live is here rather than in a footnote -- the fix is a shared counter in
Redis or Postgres, and it belongs with the persistence work rather than ahead
of it.

What this does buy today is a bound on what one signed-in account can cost.
Analysis is CPU-bound and synchronous, so a loop uploading documents is enough
to make the service unusable for everyone else, and that needs no malice at all
-- a retry loop in somebody's script does it.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException

__all__ = ["Limiter", "requests", "uploads"]


class Limiter:
    """A fixed number of events per user per window."""

    def __init__(self, *, allowance: int, window_seconds: int, what: str) -> None:
        self.allowance = allowance
        self.window = window_seconds
        self.what = what
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, user_id: str) -> None:
        """Record one event, or raise 429 with a retry hint."""
        now = time.monotonic()
        with self._lock:
            seen = self._events[user_id]
            while seen and now - seen[0] > self.window:
                seen.popleft()

            if len(seen) >= self.allowance:
                wait = int(self.window - (now - seen[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Too many {self.what}. The limit is {self.allowance} "
                        f"per {self.window // 60} minutes."
                    ),
                    headers={"Retry-After": str(wait)},
                )
            seen.append(now)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


#: Analysis is CPU-bound and runs synchronously, so uploads are the expensive
#: verb and get the tighter limit.
uploads = Limiter(allowance=30, window_seconds=3600, what="uploads")

#: Everything else is a dictionary lookup and a re-analysis of text already in
#: memory; the ceiling is here to stop a runaway client, not to ration use.
requests = Limiter(allowance=300, window_seconds=3600, what="requests")

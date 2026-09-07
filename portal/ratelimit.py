"""Sliding-window rate limits, in memory, keyed by client address or team.

Limits are strings like ``"20/600"`` (20 events per 600 seconds); ``"0"``, ``""``
or ``"off"`` disables a limit. State lives in the process and resets on restart,
which is enough to stop a single client from monopolizing the Space or burning
a team's upload cap in one burst.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Limit:
    count: int
    seconds: int

    @property
    def enabled(self) -> bool:
        return self.count > 0 and self.seconds > 0

    def describe(self) -> str:
        if self.seconds % 3600 == 0:
            span = f"{self.seconds // 3600} hour{'s' if self.seconds != 3600 else ''}"
        elif self.seconds % 60 == 0:
            span = f"{self.seconds // 60} minute{'s' if self.seconds != 60 else ''}"
        else:
            span = f"{self.seconds} seconds"
        return f"{self.count} per {span}"


def parse_limit(value: Optional[str], default: str) -> Limit:
    raw = (value if value not in (None, "") else default).strip().lower()
    if raw in ("0", "off", "none", "disabled"):
        return Limit(0, 0)
    try:
        count, seconds = raw.split("/")
        return Limit(int(count), int(seconds))
    except ValueError as exc:
        raise ValueError(f"bad rate limit {raw!r}; expected 'COUNT/SECONDS' or 'off'") from exc


class Limiter:
    """One limit, many keys. ``hit(key)`` records an event and says whether it was allowed."""

    def __init__(self, limit: Limit, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.clock = clock
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._last_prune = clock()

    def hit(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds). A denied hit is not counted."""
        if not self.limit.enabled or not key:
            return True, 0
        now = self.clock()
        with self._lock:
            if now - self._last_prune > self.limit.seconds:
                self._prune(now)
            q = self._events.setdefault(key, deque())
            while q and now - q[0] >= self.limit.seconds:
                q.popleft()
            if len(q) >= self.limit.count:
                return False, max(1, int(self.limit.seconds - (now - q[0])) + 1)
            q.append(now)
            return True, 0

    def _prune(self, now: float) -> None:
        for key in [k for k, q in self._events.items() if not q or now - q[-1] >= self.limit.seconds]:
            del self._events[key]
        self._last_prune = now

    def keys(self) -> int:
        with self._lock:
            return len(self._events)


def client_address(request) -> str:
    """Best-effort client address behind the Hugging Face proxy; empty when unknown."""
    if request is None:
        return ""
    try:
        headers = getattr(request, "headers", None) or {}
        fwd = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        client = getattr(request, "client", None)
        host = getattr(client, "host", None) if client is not None else None
        return str(host or "")
    except Exception:
        return ""

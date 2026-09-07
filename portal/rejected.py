"""Rejected team names, buffered in memory and flushed to storage as one file per window.

One Hub commit per rejected name would let anyone generate unbounded commits
from the public form, and every commit forces concurrent submissions to retry.
So names are counted in memory (distinct names capped) and written at most
once per ``flush_seconds`` or once ``flush_at`` distinct names accumulate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
from typing import Any, Optional

from .roster import normalize_name
from .storage import Add

log = logging.getLogger("portal.rejected")


class RejectedLog:
    def __init__(self, storage, *, flush_seconds: int = 600, flush_at: int = 50, max_distinct: int = 200,
                 background: bool = True) -> None:
        self.storage = storage
        self.flush_seconds, self.flush_at, self.max_distinct = flush_seconds, flush_at, max_distinct
        self._buf: dict[str, dict[str, Any]] = {}
        self._overflow = 0
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.flushes = 0
        if background and flush_seconds > 0:
            threading.Thread(target=self._loop, daemon=True, name="rejected-log-flush").start()

    def add(self, name: str, track: Optional[str]) -> None:
        log.warning("rejected team name %r (track=%s)", name, track)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        key = normalize_name(name) or name.strip().lower()
        with self._lock:
            entry = self._buf.get(key)
            if entry:
                entry["count"] += 1
                entry["last"] = now
            elif len(self._buf) < self.max_distinct:
                self._buf[key] = {"name": name.strip(), "track": track, "count": 1, "first": now, "last": now}
            else:
                self._overflow += 1
            due = len(self._buf) >= self.flush_at or (time.time() - self._last_flush) >= self.flush_seconds
        if due:
            self.flush()

    def flush(self) -> bool:
        """Write the buffered names as one file in one commit. Returns True if something was written."""
        with self._lock:
            if not self._buf and not self._overflow:
                self._last_flush = time.time()
                return False
            entries = sorted(self._buf.values(), key=lambda e: e["first"])
            overflow = self._overflow
            self._buf, self._overflow, self._last_flush = {}, 0, time.time()
        now = dt.datetime.now(dt.timezone.utc)
        digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()[:8]
        path = f"rejected/{now.strftime('%Y%m%dT%H%M%SZ')}-{digest}.json"
        payload = {"written_at": now.replace(microsecond=0).isoformat(), "entries": entries,
                   "dropped_distinct_names": overflow}
        try:
            self.storage.commit([Add(path, json.dumps(payload, indent=2))],
                                f"rejected team names ({len(entries)} distinct)")
            self.flushes += 1
            return True
        except Exception as exc:  # never let logging break a submission attempt
            log.warning("rejected-name flush failed (%d names dropped): %s", len(entries), exc)
            return False

    def _loop(self) -> None:
        while True:
            time.sleep(min(60, self.flush_seconds))
            if time.time() - self._last_flush >= self.flush_seconds:
                self.flush()

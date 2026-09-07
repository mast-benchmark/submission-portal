"""Portal temp files (reports, receipts, prepared uploads) in one directory, swept by age."""
from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger("portal.tmpfiles")
ROOT = Path(os.environ.get("MAST_PORTAL_TMP") or (Path(tempfile.gettempdir()) / "mast-portal"))
MAX_AGE_SECONDS = 3600


def new_path(prefix: str, suffix: str) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    fd, p = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=ROOT)
    os.close(fd)
    return Path(p)


def remove(paths: Iterable) -> None:
    for p in paths:
        try:
            Path(p).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", p, exc)


def sweep(max_age: float = MAX_AGE_SECONDS) -> int:
    """Delete files older than ``max_age`` seconds. Cheap; called at the start of every validation."""
    if not ROOT.is_dir():
        return 0
    cutoff = time.time() - max_age
    n = 0
    for p in ROOT.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n

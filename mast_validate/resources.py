"""Lazy, memoized loaders for the packaged validation resources.

``queries.json`` maps ``"{track}/{lang}"`` to the official qid list;
``docids.txt.gz`` is the sorted corpus docid list; ``manifest.json`` records
where and when they were built. Nothing is fetched at runtime.
"""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from importlib import resources as importlib_resources
from typing import Any


class ResourceError(RuntimeError):
    """Raised when a packaged resource is missing or malformed."""


def _traversable(name: str):
    return importlib_resources.files("mast_validate").joinpath("resources").joinpath(name)


def _read_bytes(name: str) -> bytes:
    t = _traversable(name)
    try:
        return t.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise ResourceError(
            f"packaged resource {name!r} is missing; an organizer must run "
            f"scripts/build_resources.py and commit its output"
        ) from exc


@lru_cache(maxsize=None)
def manifest() -> dict[str, Any]:
    return json.loads(_read_bytes("manifest.json").decode("utf-8"))


@lru_cache(maxsize=None)
def _qids_all() -> dict[tuple[str, str], tuple[str, ...]]:
    raw = json.loads(_read_bytes("queries.json").decode("utf-8"))
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for key, values in raw.items():
        track, _, lang = key.partition("/")
        if not track or not lang or not isinstance(values, list):
            raise ResourceError(f"queries.json: malformed entry {key!r}")
        out[(track, lang)] = tuple(values)
    return out


def scopes() -> list[tuple[str, str]]:
    """All ``(track, lang)`` pairs with an official qid list, in file order."""
    return list(_qids_all())


def has_scope(track: str, lang: str) -> bool:
    return (track, lang) in _qids_all()


@lru_cache(maxsize=None)
def qids(track: str, lang: str) -> frozenset[str]:
    """Official qids for a scope as a set for O(1) membership."""
    try:
        return frozenset(_qids_all()[(track, lang)])
    except KeyError:
        raise ResourceError(f"no official qid list for ({track!r}, {lang!r})") from None


def _num(qid: str) -> int:
    try:
        return int(qid.rsplit("-", 1)[-1])
    except ValueError:
        return 1 << 62


@lru_cache(maxsize=None)
def qids_sorted(track: str, lang: str) -> tuple[str, ...]:
    """Official qids sorted by numeric suffix, for readable coverage diffs."""
    return tuple(sorted(qids(track, lang), key=_num))


@lru_cache(maxsize=None)
def docids() -> frozenset[str]:
    data = gzip.decompress(_read_bytes("docids.txt.gz")).decode("utf-8")
    return frozenset(line for line in data.split("\n") if line)


def sort_qids(values) -> list[str]:
    """Sort arbitrary canonical qids numerically (helper for reports)."""
    return sorted(values, key=_num)

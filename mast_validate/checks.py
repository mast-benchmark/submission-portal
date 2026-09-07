"""File-level aggregation and checks (spec §6): duplicates, coverage, docids.

``FileState`` consumes one parsed record at a time (so the caller can stream)
and produces a :class:`FileReport` at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import resources
from .report import FileReport, level_of
from .schema import validate_record

MAJORITY_UNKNOWN = 0.5


@dataclass
class _Agg:
    count: int = 0
    examples: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)

    def add(self, line_no: Optional[int], example: Optional[str], cap: int) -> None:
        self.count += 1
        if line_no is not None and len(self.lines) < cap:
            self.lines.append(line_no)
        if example is not None and len(self.examples) < cap:
            self.examples.append(f"line {line_no}: {example}" if line_no is not None else example)


class FileState:
    """Accumulates per-file state while records stream through."""

    def __init__(self, name: str, track: str, lang: str, *, max_examples: int = 10) -> None:
        self.name, self.track, self.lang = name, track, lang
        self.cap = max(1, max_examples)
        self.records = 0
        self.aggs: dict[str, _Agg] = {}
        self.first_line: dict[str, int] = {}
        self.dups: dict[str, list[int]] = {}
        self.unknown_docids: set[str] = set()
        self.all_docids: set[str] = set()
        self.unknown_keys: set[str] = set()
        self.reconstructed = 0
        self.official = resources.qids(track, lang)
        self._docids = resources.docids()

    def note(self, kind: str, line_no: Optional[int] = None, example: Optional[str] = None) -> None:
        self.aggs.setdefault(kind, _Agg()).add(line_no, example, self.cap)

    def add_record(self, line_no: int, obj: Any) -> None:
        self.records += 1
        rr = validate_record(obj, self.track, self.lang)
        for issue in rr.issues:
            self.note(issue.kind, line_no, issue.example)
        if rr.reconstructed:
            self.reconstructed += 1
        self.unknown_keys |= rr.unknown_keys
        if rr.qid is not None:
            if rr.qid in self.first_line:
                self.dups.setdefault(rr.qid, [self.first_line[rr.qid]]).append(line_no)
            else:
                self.first_line[rr.qid] = line_no
                if rr.qid not in self.official:
                    self.note("qid.unknown", line_no, rr.qid)
        if rr.rounds:
            for rnd in rr.rounds:
                for d in rnd:
                    self.all_docids.add(d)
                    if d not in self._docids:
                        self.unknown_docids.add(d)

    def finish(self) -> FileReport:
        fr = FileReport(self.name, self.track, self.lang, records=self.records)
        for qid, lines in self.dups.items():
            self.note("qid.duplicate", None, f"{qid} (lines {', '.join(map(str, lines))})")
        found = set(self.first_line)
        missing = self.official - found
        if missing:
            ex = resources.sort_qids(missing)[: self.cap]
            fr.add("coverage.missing", len(missing), ex, found=len(found & self.official), expected=len(self.official))
        if self.unknown_docids:
            share = len(self.unknown_docids) / max(1, len(self.all_docids))
            pct = f"{share:.1%}" if share >= 0.001 else f"{share:.2%}"
            kind = "docid.unknown_majority" if share > MAJORITY_UNKNOWN else "docid.unknown"
            ex = sorted(self.unknown_docids, key=lambda d: (len(d), d))[: self.cap]
            fr.add(kind, len(self.unknown_docids), [repr(d) for d in ex], pct=pct,
                   distinct_docids=len(self.all_docids))
        if self.reconstructed:
            fr.add("qid.reconstructed", self.reconstructed, declared=self.lang)
        if self.unknown_keys:
            keys = sorted(self.unknown_keys)
            fr.add("keys.unknown", len(keys), keys=keys)
        for kind, agg in self.aggs.items():
            details: dict[str, Any] = {}
            if kind in ("lang.mismatch", "qid.prefix_mismatch"):
                details["declared"] = self.lang
            if kind == "qid.unknown":
                details["scope"] = f"{self.track}/{self.lang}"
            level_of(kind)  # KeyError early if a kind is unregistered
            fr.add(kind, agg.count, agg.examples, agg.lines, **details)
        return fr

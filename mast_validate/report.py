"""Findings, per-file and whole-submission reports, and their rendering.

Finding *kinds* are stable string ids (``"qid.duplicate"``); tests and the
Space key on them. Messages are derived from the kind plus counts so that a
file with 500 bad records produces one line, never 500.
"""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from . import __version__


class Level(str, Enum):
    ERROR = "error"
    WARNING = "warning"


def _p(n: int, noun: str, plural: Optional[str] = None) -> str:
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


# kind -> (level, message builder(count, details))
KINDS: dict[str, tuple[Level, Callable[[int, dict], str]]] = {
    # ---- errors ----
    "io.unreadable": (Level.ERROR, lambda n, d: f"file could not be read: {d.get('reason')}"),
    "io.oversize": (Level.ERROR, lambda n, d: f"file too large: {d.get('reason')}"),
    "io.not_jsonl": (Level.ERROR, lambda n, d: f"{d.get('reason')}"),
    "zip.unsafe_member": (Level.ERROR, lambda n, d: f"{_p(n, 'unsafe zip member')} rejected"),
    "zip.duplicate_language": (Level.ERROR, lambda n, d: f"{_p(n, 'language')} present in more than one zip member"),
    "json.invalid_line": (Level.ERROR, lambda n, d: f"{_p(n, 'line')} not valid JSON"),
    "json.not_object": (Level.ERROR, lambda n, d: f"{_p(n, 'line')} not a JSON object"),
    "schema.missing_field": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} missing a required field"),
    "schema.wrong_type": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} with a wrong-typed field"),
    "schema.result_empty": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} with an empty 'result'"),
    "schema.step_unknown_type": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} with a 'result' step of unknown type"),
    "lang.mismatch": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} whose 'language' disagrees with the file's language '{d.get('declared')}'"),
    "lang.undetermined": (Level.ERROR, lambda n, d: "cannot determine the file's language: no record has a recognizable 'language' field"),
    "lang.not_in_track": (Level.ERROR, lambda n, d: f"the file's language '{d.get('language')}' is not in the {d.get('track')} track"),
    "qid.malformed": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} with an unparseable query_id"),
    "qid.prefix_mismatch": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} whose query_id prefix disagrees with the file's language '{d.get('declared')}'"),
    "qid.unknown": (Level.ERROR, lambda n, d: f"{_p(n, 'query_id')} not in the official set for {d.get('scope')}"),
    "qid.duplicate": (Level.ERROR, lambda n, d: f"{_p(n, 'query_id')} duplicated within the file"),
    "coverage.missing": (Level.ERROR, lambda n, d: f"coverage: {_p(n, 'official query_id')} missing (found {d.get('found')} of {d.get('expected')})"),
    "docid.bad_entry": (Level.ERROR, lambda n, d: f"{_p(n, 'record')} with a docid that is not a non-empty string"),
    # ---- warnings ----
    "docid.unknown": (Level.WARNING, lambda n, d: f"{_p(n, 'distinct docid')} ({d.get('pct')}) not in the corpus"),
    "docid.unknown_majority": (Level.WARNING, lambda n, d: f"{_p(n, 'distinct docid')} ({d.get('pct')}) not in the MAST corpus; this looks like a different corpus was indexed"),
    "qid.reconstructed": (Level.WARNING, lambda n, d: f"query_ids in {_p(n, 'record')} carried no language prefix; reconstructed as '{d.get('declared')}-<id>' from the file's language"),
    "answer.no_output_text": (Level.WARNING, lambda n, d: f"{_p(n, 'record')} with no 'output_text' step (no final answer)"),
    "answer.no_exact_answer": (Level.WARNING, lambda n, d: f"{_p(n, 'record')} with no 'Exact Answer:' in the final output_text"),
    "rounds.count_mismatch": (Level.WARNING, lambda n, d: f"{_p(n, 'record')} where len(retrieved_docids) != tool_call_counts['search']"),
    "rounds.empty": (Level.WARNING, lambda n, d: f"{_p(n, 'record')} with an empty search round"),
    "keys.unknown": (Level.WARNING, lambda n, d: f"unknown top-level {_p(n, 'key')} ignored: {', '.join(d.get('keys', []))}"),
    "track.language_missing": (Level.WARNING, lambda n, d: f"missing (declared track expects {d.get('expected')} languages, found {d.get('found')})"),
    "zip.member_ignored": (Level.WARNING, lambda n, d: f"{_p(n, 'zip member')} ignored"),
}

_ORDER = {k: i for i, k in enumerate(KINDS)}


def level_of(kind: str) -> Level:
    return KINDS[kind][0]


@dataclass
class Finding:
    kind: str
    level: Level
    message: str
    count: int = 1
    examples: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(cls, kind: str, count: int = 1, examples=(), lines=(), **details: Any) -> "Finding":
        level, build = KINDS[kind]
        return cls(kind, level, build(count, details), count, list(examples), list(lines), dict(details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "level": self.level.value, "message": self.message,
            "count": self.count, "examples": self.examples, "lines": self.lines, "details": self.details,
        }


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.level != Level.ERROR, _ORDER.get(f.kind, 999)))


@dataclass
class FileReport:
    name: str
    track: str
    language: Optional[str]
    present: bool = True
    records: int = 0
    findings: list[Finding] = field(default_factory=list)
    inferred: bool = False   # language inferred from the records rather than declared

    def add(self, kind: str, count: int = 1, examples=(), lines=(), **details: Any) -> Finding:
        f = Finding.make(kind, count, examples, lines, **details)
        self.findings.append(f)
        return f

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == Level.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == Level.WARNING]

    def kinds(self) -> set[str]:
        return {f.kind for f in self.findings}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "track": self.track, "language": self.language, "inferred": self.inferred,
            "present": self.present,
            "records": self.records, "errors": len(self.errors), "warnings": len(self.warnings),
            "findings": [f.to_dict() for f in sort_findings(self.findings)],
        }


@dataclass
class Report:
    track: str
    source: str
    strict: bool = False
    files: list[FileReport] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)   # submission-level (zip structure)
    generated_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
    resources_built_at: Optional[str] = None

    def add(self, kind: str, count: int = 1, examples=(), lines=(), **details: Any) -> Finding:
        f = Finding.make(kind, count, examples, lines, **details)
        self.findings.append(f)
        return f

    def all_findings(self) -> list[Finding]:
        out = list(self.findings)
        for fr in self.files:
            out.extend(fr.findings)
        return out

    @property
    def n_errors(self) -> int:
        return sum(1 for f in self.all_findings() if f.level == Level.ERROR)

    @property
    def n_warnings(self) -> int:
        return sum(1 for f in self.all_findings() if f.level == Level.WARNING)

    @property
    def n_records(self) -> int:
        return sum(fr.records for fr in self.files)

    @property
    def status(self) -> str:
        if self.n_errors or (self.strict and self.n_warnings):
            return "errors"
        return "warnings" if self.n_warnings else "ok"

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "warnings": 1, "errors": 2}[self.status]

    @property
    def passed(self) -> bool:
        return self.status != "errors"

    def kinds(self) -> set[str]:
        return {f.kind for f in self.all_findings()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator": {"name": "mast-validate", "version": __version__, "resources_built_at": self.resources_built_at},
            "generated_at": self.generated_at,
            "track": self.track,
            "source": self.source,
            "strict": self.strict,
            "status": self.status,
            "exit_code": self.exit_code,
            "summary": {
                "files": len(self.files), "records": self.n_records,
                "errors": self.n_errors, "warnings": self.n_warnings,
            },
            "findings": [f.to_dict() for f in sort_findings(self.findings)],
            "files": [fr.to_dict() for fr in self.files],
        }


# ---------------------------------------------------------------- rendering

class _Style:
    def __init__(self, color: bool, unicode: bool):
        self.color, self.unicode = color, unicode

    def paint(self, s: str, code: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def ok(self) -> str:
        return self.paint("✓" if self.unicode else "OK", "32")

    def bad(self) -> str:
        return self.paint("✗" if self.unicode else "FAIL", "31")

    def warn(self) -> str:
        return self.paint("⚠" if self.unicode else "WARN", "33")

    def level(self, level: Level) -> str:
        return self.paint("ERROR", "31;1") if level == Level.ERROR else self.paint("WARN ", "33")


def _supports_unicode() -> bool:
    enc = getattr(sys.stdout, "encoding", None) or ""
    return "utf" in enc.lower()


def _fmt_examples(f: Finding, max_examples: int) -> str:
    ex = f.examples[:max_examples]
    if not ex:
        return ""
    more = f.count - len(ex) if f.count > len(ex) and len(f.examples) > len(ex) else 0
    tail = f", +{len(f.examples) - len(ex)} more" if len(f.examples) > len(ex) else ""
    return f" (e.g. {', '.join(ex)}{tail})"


def render(report: Report, *, color: bool = True, quiet: bool = False, max_examples: int = 10,
           unicode: Optional[bool] = None) -> str:
    st = _Style(color, _supports_unicode() if unicode is None else unicode)
    lines: list[str] = [f"MAST submission validator · track={report.track}" if st.unicode
                        else f"MAST submission validator - track={report.track}", ""]
    for f in sort_findings(report.findings):
        lines.append(f"    {st.level(f.level)}   {f.message}{_fmt_examples(f, max_examples)}")
    if report.findings:
        lines.append("")
    width = min(max((len(fr.name) for fr in report.files), default=10), 48)
    for fr in report.files:
        name = f"{fr.name.ljust(width)} [{fr.language or '??'}]"
        if not fr.present:
            miss = next((x for x in fr.findings if x.kind == "track.language_missing"), None)
            lines.append(f"{name}   {st.warn()} {miss.message if miss else 'missing'}")
            continue
        ne, nw = len(fr.errors), len(fr.warnings)
        rec = f"{fr.records:>5} records" if fr.records != 1 else "    1 record "
        if not ne and not nw:
            lines.append(f"{name} {rec}   {st.ok()}")
        else:
            parts = []
            if ne:
                parts.append(_p(ne, "error"))
            if nw:
                parts.append(_p(nw, "warning"))
            lines.append(f"{name} {rec}   {st.bad() if ne else st.warn()} {', '.join(parts)}")
        if not quiet:
            for f in sort_findings(fr.findings):
                lines.append(f"    {st.level(f.level)}   {f.message}{_fmt_examples(f, max_examples)}")
    lines.append("")
    verdict = {"ok": st.paint("PASSED", "32;1"), "warnings": st.paint("PASSED with warnings", "33;1"),
               "errors": st.paint("FAILED", "31;1")}[report.status]
    strict_note = " (--strict: warnings count as errors)" if report.strict and report.n_warnings else ""
    lines.append(f"{_p(report.n_errors, 'error')}, {_p(report.n_warnings, 'warning')} across "
                 f"{_p(len(report.files), 'file')}, {_p(report.n_records, 'record')}.  {verdict}{strict_note}")
    return "\n".join(lines) + "\n"

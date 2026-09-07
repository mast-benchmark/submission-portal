"""Streaming validation of one file, one zip, or whatever path is given.

A file's language is either *declared* by the caller or *inferred* from its
records: the first records' ``language`` fields decide (majority of the first
``PEEK_RECORDS``), and every record must then agree, including query-id
prefixes. Filenames are never used. Everything is read line by line.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Union

from . import limits, resources
from .archive import scan, scan_tar
from .checks import FileState
from .languages import TRACKS, in_track, normalize
from .report import FileReport, Report

GZIP_MAGIC = b"\x1f\x8b"
ZIP_MAGIC = b"PK\x03\x04"
PEEK_RECORDS = 20
PathLike = Union[str, "os.PathLike[str]"]


class UsageProblem(Exception):
    """Bad invocation or unreadable input: exit code 3 in the CLI."""


class _Oversize(Exception):
    pass


def _peek(binary: BinaryIO, n: int) -> tuple[bytes, BinaryIO]:
    peek = getattr(binary, "peek", None)
    if peek is not None:
        try:
            return peek(n)[:n], binary
        except (OSError, ValueError):
            pass
    head = binary.read(n)
    rest = binary.read()
    return head, io.BufferedReader(io.BytesIO(head + rest))  # only for non-seekable, non-peekable inputs


def _fmt_bytes(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB" if n >= 1024 * 1024 else f"{n} bytes"


def _iter_records(stream: BinaryIO) -> Iterator[tuple[int, object, Optional[str]]]:
    """Yield (line_no, parsed_object_or_None, error_example_or_None), enforcing size caps."""
    total = 0
    for line_no, raw in enumerate(stream, 1):
        total += len(raw)
        if total > limits.MAX_DECOMPRESSED_BYTES:
            raise _Oversize(f"decompressed size exceeds {_fmt_bytes(limits.MAX_DECOMPRESSED_BYTES)}")
        if len(raw) > limits.MAX_LINE_BYTES:
            raise _Oversize(f"line {line_no} exceeds {_fmt_bytes(limits.MAX_LINE_BYTES)}")
        if line_no == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        if not raw.strip():
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            yield line_no, None, f"invalid UTF-8 at byte {exc.start}"
            continue
        try:
            yield line_no, json.loads(text), None
        except json.JSONDecodeError as exc:
            yield line_no, None, f"{exc.msg} at column {exc.colno}"


def infer_language(objs) -> Optional[str]:
    """Majority of the recognizable ``language`` fields; ties go to the first seen."""
    counts: Counter = Counter()
    order: dict[str, int] = {}
    for obj in objs:
        if isinstance(obj, dict):
            code = normalize(obj.get("language"))
            if code:
                counts[code] += 1
                order.setdefault(code, len(order))
    if not counts:
        return None
    return max(counts, key=lambda c: (counts[c], -order[c]))


def validate_stream(binary: BinaryIO, *, track: str, lang: Optional[str] = None, name: str,
                    max_examples: int = 10) -> FileReport:
    """Validate one JSONL (optionally gzipped) stream. ``lang=None`` infers it from the records."""
    head, binary = _peek(binary, 4)
    if head.startswith(ZIP_MAGIC):
        fr = FileReport(name, track, lang)
        fr.add("io.not_jsonl", reason="this is a zip archive where a .jsonl file was expected")
        return fr
    stream: BinaryIO = gzip.GzipFile(fileobj=binary) if head.startswith(GZIP_MAGIC) else binary  # type: ignore[assignment]
    state: Optional[FileState] = None
    buffered: list[tuple[int, object, Optional[str]]] = []
    inferred = lang is None
    early: Optional[FileReport] = None

    def feed(item: tuple[int, object, Optional[str]]) -> None:
        line_no, obj, err = item
        if err is not None:
            state.note("json.invalid_line", line_no, err)  # type: ignore[union-attr]
        else:
            state.add_record(line_no, obj)  # type: ignore[union-attr]

    def decide() -> None:
        nonlocal state, early
        code = lang if lang is not None else infer_language(o for _, o, _ in buffered)
        n_records = sum(1 for _, o, _ in buffered if o is not None)
        if code is None:
            early = FileReport(name, track, None, records=n_records, inferred=True)
            early.add("lang.undetermined")
            return
        if not in_track(track, code):
            early = FileReport(name, track, code, records=n_records, inferred=inferred)
            early.add("lang.not_in_track", language=code, track=track)
            return
        state = FileState(name, track, code, max_examples=max_examples)
        for item in buffered:
            feed(item)
        buffered.clear()

    try:
        for item in _iter_records(stream):
            if state is None and early is None:
                buffered.append(item)
                if sum(1 for _, o, _ in buffered if o is not None) >= PEEK_RECORDS:
                    decide()
                continue
            if early is not None:
                if item[1] is not None:
                    early.records += 1
                continue
            feed(item)
        if state is None and early is None:
            decide()
    except _Oversize as exc:
        fr = state.finish() if state is not None else FileReport(name, track, lang, inferred=inferred)
        fr.findings = [f for f in fr.findings if f.kind != "coverage.missing"]
        fr.add("io.oversize", reason=str(exc))
        return fr
    except (OSError, EOFError, gzip.BadGzipFile, zipfile.BadZipFile) as exc:
        fr = FileReport(name, track, lang, inferred=inferred)
        fr.add("io.unreadable", reason=f"{type(exc).__name__}: {exc}")
        return fr
    if early is not None:
        return early
    fr = state.finish()  # type: ignore[union-attr]
    fr.inferred = inferred
    return fr


def validate_file(path: PathLike, *, track: str, lang: Optional[str] = None, name: Optional[str] = None,
                  max_examples: int = 10) -> FileReport:
    p = Path(path)
    name = name or p.name
    size = p.stat().st_size
    if size > limits.MAX_COMPRESSED_BYTES:
        fr = FileReport(name, track, lang)
        fr.add("io.oversize", reason=f"{_fmt_bytes(size)} on disk exceeds the {_fmt_bytes(limits.MAX_COMPRESSED_BYTES)} cap")
        return fr
    with open(p, "rb") as fh:
        return validate_stream(io.BufferedReader(fh), track=track, lang=lang, name=name, max_examples=max_examples)


def _new_report(track: str, source: str, strict: bool) -> Report:
    built = None
    try:
        built = resources.manifest().get("built_at")
    except Exception:  # resources missing: FileState will raise a clearer error
        pass
    return Report(track=track, source=source, strict=strict, resources_built_at=built)


def archive_kind(path: PathLike) -> Optional[str]:
    """``"zip"``, ``"tar"`` (plain or gzip/bz2/xz compressed) or ``None`` for a single file. Content-based."""
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            if fh.read(4) == ZIP_MAGIC:
                return "zip"
        if zipfile.is_zipfile(p):
            return "zip"
        if tarfile.is_tarfile(p):
            return "tar"
    except (OSError, EOFError, tarfile.TarError):
        pass
    return None


def is_zip(path: PathLike) -> bool:
    return archive_kind(path) == "zip"


def is_archive(path: PathLike) -> bool:
    return archive_kind(path) is not None


def _finish_archive(report: Report, track: str, max_examples: int) -> None:
    """Duplicate-language and missing-language bookkeeping shared by zip and tar."""
    by_lang: dict[str, list[FileReport]] = {}
    for fr in report.files:
        if fr.language and in_track(track, fr.language):
            by_lang.setdefault(fr.language, []).append(fr)
    dups = {code: frs for code, frs in by_lang.items() if len(frs) > 1}
    if dups:
        report.add("archive.duplicate_language", len(dups),
                   [f"{code}: {', '.join(fr.name for fr in frs)}" for code, frs in dups.items()][:max_examples])
        for code, frs in dups.items():
            for fr in frs:
                fr.add("archive.duplicate_language", 1, [f"{code} also in {', '.join(x.name for x in frs if x is not fr)}"])
    expected = TRACKS[track]
    present = set(by_lang)
    for code in expected:
        if code not in present:
            fr = FileReport(f"{code}.jsonl", track, code, present=False)
            fr.add("track.language_missing", expected=len(expected), found=len(present))
            report.files.append(fr)
    report.files.sort(key=lambda fr: (fr.language or "~", fr.name))


def validate_archive(path: PathLike, *, track: str, strict: bool = False, max_examples: int = 10) -> Report:
    """Validate every .jsonl member of a zip or tar archive; each member's language comes from its records."""
    p = Path(path)
    report = _new_report(track, str(p), strict)
    size = p.stat().st_size
    if size > limits.MAX_COMPRESSED_BYTES:
        report.add("io.oversize", reason=f"{_fmt_bytes(size)} on disk exceeds the {_fmt_bytes(limits.MAX_COMPRESSED_BYTES)} cap")
        return report
    kind = archive_kind(p)
    if kind == "zip":
        try:
            zf = zipfile.ZipFile(p)
        except zipfile.BadZipFile as exc:
            raise UsageProblem(f"{p}: not a valid zip file ({exc})") from exc
        with zf:
            zs = scan(zf)
            if zs.unsafe:
                report.add("archive.unsafe_member", len(zs.unsafe), zs.unsafe[:max_examples])
            if zs.ignored:
                report.add("archive.member_ignored", len(zs.ignored), zs.ignored[:max_examples])
            for info in zs.members:
                if info.compress_size > limits.MAX_COMPRESSED_BYTES:
                    fr = FileReport(info.filename, track, None)
                    fr.add("io.oversize", reason=f"{_fmt_bytes(info.compress_size)} compressed exceeds the "
                                                 f"{_fmt_bytes(limits.MAX_COMPRESSED_BYTES)} cap")
                    report.files.append(fr)
                    continue
                with zf.open(info) as fh:
                    report.files.append(validate_stream(fh, track=track, lang=None, name=info.filename,
                                                        max_examples=max_examples))
    elif kind == "tar":
        try:
            tf = tarfile.open(p, "r:*")
        except tarfile.TarError as exc:
            raise UsageProblem(f"{p}: not a valid tar archive ({exc})") from exc
        with tf:
            ts = scan_tar(tf)
            if ts.unsafe:
                report.add("archive.unsafe_member", len(ts.unsafe), ts.unsafe[:max_examples])
            if ts.ignored:
                report.add("archive.member_ignored", len(ts.ignored), ts.ignored[:max_examples])
            for info in ts.members:
                fh = tf.extractfile(info)
                if fh is None:
                    continue
                with fh:
                    report.files.append(validate_stream(io.BufferedReader(fh), track=track, lang=None,  # type: ignore[arg-type]
                                                        name=info.name, max_examples=max_examples))
    else:
        raise UsageProblem(f"{p}: not a zip or tar archive")
    _finish_archive(report, track, max_examples)
    return report


validate_zip = validate_archive  # backwards-compatible name


def validate_submission(path: PathLike, *, track: str, language: Optional[str] = None,
                        strict: bool = False, max_examples: int = 10) -> Report:
    """CLI entry: a zip/tar of per-language files, or one file (language declared or inferred)."""
    p = Path(path)
    if track not in TRACKS:
        raise UsageProblem(f"unknown track {track!r}; expected one of {', '.join(TRACKS)}")
    if not p.is_file():
        raise UsageProblem(f"{p}: no such file")
    if is_archive(p):
        if language:
            raise UsageProblem("--language cannot be combined with an archive; each member's language comes from its records")
        return validate_archive(p, track=track, strict=strict, max_examples=max_examples)
    lang: Optional[str] = None
    if language:
        lang = normalize(language)
        if lang is None:
            raise UsageProblem(f"unknown language {language!r}")
        if not in_track(track, lang):
            raise UsageProblem(f"language '{lang}' is not in the {track} track "
                               f"(expected one of {', '.join(TRACKS[track])})")
    report = _new_report(track, str(p), strict)
    report.files.append(validate_file(p, track=track, lang=lang, max_examples=max_examples))
    return report


def single_file_report(path: PathLike, *, track: str, lang: Optional[str], name: Optional[str] = None,
                       strict: bool = False, max_examples: int = 10) -> Report:
    """Portal entry for one file: ``lang`` declared by the form, or ``None`` to infer."""
    if lang is not None and not in_track(track, lang):
        raise UsageProblem(f"language '{lang}' is not in the {track} track")
    report = _new_report(track, name or str(path), strict)
    report.files.append(validate_file(path, track=track, lang=lang, name=name, max_examples=max_examples))
    return report

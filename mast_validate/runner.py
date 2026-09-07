"""Streaming validation of one file, one zip, or whatever path is given.

Everything here reads line by line and never holds a whole decompressed file
in memory; per-file state lives in :class:`mast_validate.checks.FileState`.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import zipfile
from pathlib import Path
from typing import BinaryIO, Optional, Union

from . import limits, resources
from .archive import scan
from .checks import FileState
from .languages import TRACKS, in_track, language_from_filename, normalize
from .report import FileReport, Report

GZIP_MAGIC = b"\x1f\x8b"
ZIP_MAGIC = b"PK\x03\x04"
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


def validate_stream(binary: BinaryIO, *, track: str, lang: str, name: str,
                    max_examples: int = 10) -> FileReport:
    """Validate one JSONL (optionally gzipped) stream as the declared (track, lang)."""
    state = FileState(name, track, lang, max_examples=max_examples)
    head, binary = _peek(binary, 4)
    if head.startswith(ZIP_MAGIC):
        fr = FileReport(name, track, lang)
        fr.add("io.not_jsonl", reason="this is a zip archive, not a .jsonl file; upload one per-language .jsonl "
                                      "(the CLI accepts a zip of {lang}.jsonl files, the portal does not)")
        return fr
    stream: BinaryIO = gzip.GzipFile(fileobj=binary) if head.startswith(GZIP_MAGIC) else binary  # type: ignore[assignment]
    total = 0
    try:
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
                state.note("json.invalid_line", line_no, f"invalid UTF-8 at byte {exc.start}")
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                state.note("json.invalid_line", line_no, f"{exc.msg} at column {exc.colno}")
                continue
            state.add_record(line_no, obj)
    except _Oversize as exc:
        fr = state.finish()
        fr.findings = [f for f in fr.findings if f.kind != "coverage.missing"]
        fr.add("io.oversize", reason=str(exc))
        return fr
    except (OSError, EOFError, gzip.BadGzipFile, zipfile.BadZipFile) as exc:
        fr = FileReport(name, track, lang, records=state.records)
        fr.add("io.unreadable", reason=f"{type(exc).__name__}: {exc}")
        return fr
    return state.finish()


def validate_file(path: PathLike, *, track: str, lang: str, name: Optional[str] = None,
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


def validate_zip(path: PathLike, *, track: str, strict: bool = False, max_examples: int = 10) -> Report:
    p = Path(path)
    report = _new_report(track, str(p), strict)
    try:
        zf = zipfile.ZipFile(p)
    except zipfile.BadZipFile as exc:
        raise UsageProblem(f"{p}: not a valid zip file ({exc})") from exc
    with zf:
        zs = scan(zf, track)
        if zs.unsafe:
            report.add("zip.unsafe_member", len(zs.unsafe), zs.unsafe[:max_examples])
        if zs.duplicates:
            ex = [f"{lang}: {', '.join(names)}" for lang, names in zs.duplicates.items()]
            report.add("zip.duplicate_language", len(zs.duplicates), ex[:max_examples])
        if zs.ignored:
            report.add("zip.member_ignored", len(zs.ignored), zs.ignored[:max_examples])
        for m in zs.members:
            if m.info.compress_size > limits.MAX_COMPRESSED_BYTES:
                fr = FileReport(m.name, track, m.language)
                fr.add("io.oversize", reason=f"{_fmt_bytes(m.info.compress_size)} compressed exceeds the "
                                             f"{_fmt_bytes(limits.MAX_COMPRESSED_BYTES)} cap")
                report.files.append(fr)
                continue
            with zf.open(m.info) as fh:
                report.files.append(validate_stream(fh, track=track, lang=m.language, name=m.name,
                                                    max_examples=max_examples))
        present = zs.languages | set(zs.duplicates)
        expected = TRACKS[track]
        for lang in expected:
            if lang not in present:
                fr = FileReport(f"{lang}.jsonl", track, lang, present=False)
                fr.add("track.language_missing", expected=len(expected), found=len(present & set(expected)))
                report.files.append(fr)
        report.files.sort(key=lambda fr: (fr.language or "", fr.name))
    return report


def is_zip(path: PathLike) -> bool:
    """True for any zip, including an empty one (which has no local-file-header magic)."""
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            if fh.read(4) == ZIP_MAGIC:
                return True
        return zipfile.is_zipfile(p)
    except OSError:
        return False


def validate_submission(path: PathLike, *, track: str, language: Optional[str] = None,
                        strict: bool = False, max_examples: int = 10) -> Report:
    """CLI entry: a zip of per-language files, or one file with a declared language."""
    p = Path(path)
    if track not in TRACKS:
        raise UsageProblem(f"unknown track {track!r}; expected one of {', '.join(TRACKS)}")
    if not p.is_file():
        raise UsageProblem(f"{p}: no such file")
    if is_zip(p):
        if language:
            raise UsageProblem("--language cannot be combined with a zip; the zip names its languages")
        return validate_zip(p, track=track, strict=strict, max_examples=max_examples)
    if language:
        lang = normalize(language)
        if lang is None:
            raise UsageProblem(f"unknown language {language!r}")
    else:
        lang = language_from_filename(p.name)
        if lang is None:
            raise UsageProblem(f"cannot tell the language from the filename {p.name!r}; pass --language")
    if not in_track(track, lang):
        raise UsageProblem(f"language '{lang}' is not in the {track} track "
                           f"(expected one of {', '.join(TRACKS[track])})")
    report = _new_report(track, str(p), strict)
    report.files.append(validate_file(p, track=track, lang=lang, max_examples=max_examples))
    return report


def single_file_report(path: PathLike, *, track: str, lang: str, name: Optional[str] = None,
                       strict: bool = False, max_examples: int = 10) -> Report:
    """Portal entry: the language is declared by the caller, never inferred."""
    if not in_track(track, lang):
        raise UsageProblem(f"language '{lang}' is not in the {track} track")
    report = _new_report(track, name or str(path), strict)
    report.files.append(validate_file(path, track=track, lang=lang, name=name, max_examples=max_examples))
    return report

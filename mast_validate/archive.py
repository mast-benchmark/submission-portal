"""Safe walking of a participant zip. Never extracts; members are streamed.

Only members named ``{lang}.jsonl`` / ``{lang}.jsonl.gz`` (optionally under
one or more directories, ``hindi.jsonl`` and ``run_hi.jsonl`` also work) are
validated. ``__MACOSX/``, ``.DS_Store`` and the like are ignored. Absolute
paths, ``..`` segments, symlinks and non-regular members are rejected.
"""
from __future__ import annotations

import re
import stat
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from .languages import in_track, language_from_filename

_DRIVE = re.compile(r"^[A-Za-z]:")
_IGNORED_BASENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
_JSONL_EXTS = (".jsonl", ".jsonl.gz")


@dataclass
class ZipMember:
    info: zipfile.ZipInfo
    language: str

    @property
    def name(self) -> str:
        return self.info.filename


@dataclass
class ZipScan:
    members: list[ZipMember] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)          # "name (reason)"
    unsafe: list[str] = field(default_factory=list)           # "name (reason)"
    duplicates: dict[str, list[str]] = field(default_factory=dict)  # lang -> member names

    @property
    def languages(self) -> set[str]:
        return {m.language for m in self.members}


def unsafe_reason(info: zipfile.ZipInfo) -> Optional[str]:
    name = info.filename
    if "\x00" in name:
        return "NUL in name"
    if name.startswith(("/", "\\")) or _DRIVE.match(name):
        return "absolute path"
    parts = re.split(r"[\\/]+", name)
    if any(p == ".." for p in parts):
        return "path traversal"
    fmt = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)  # 0 when the archiver stored no type bits
    if fmt == stat.S_IFLNK:
        return "symlink"
    if fmt and fmt not in (stat.S_IFREG, stat.S_IFDIR):
        return "not a regular file"
    return None


def _is_noise(name: str) -> bool:
    parts = [p for p in re.split(r"[\\/]+", name) if p]
    if not parts:
        return True
    base = parts[-1]
    return parts[0] == "__MACOSX" or base in _IGNORED_BASENAMES or base.startswith("._")


def scan(zf: zipfile.ZipFile, track: str) -> ZipScan:
    out = ZipScan()
    by_lang: dict[str, list[ZipMember]] = {}
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or name.endswith(("/", "\\")):
            continue
        if _is_noise(name):
            continue
        reason = unsafe_reason(info)
        if reason:
            out.unsafe.append(f"{name} ({reason})")
            continue
        if not name.lower().endswith(_JSONL_EXTS):
            out.ignored.append(f"{name} (not a .jsonl file)")
            continue
        lang = language_from_filename(name)
        if lang is None:
            out.ignored.append(f"{name} (no language in filename)")
            continue
        if not in_track(track, lang):
            out.ignored.append(f"{name} ('{lang}' is not in the {track} track)")
            continue
        by_lang.setdefault(lang, []).append(ZipMember(info, lang))
    for lang, members in by_lang.items():
        if len(members) > 1:
            out.duplicates[lang] = [m.name for m in members]
        else:
            out.members.append(members[0])
    out.members.sort(key=lambda m: m.language)
    return out

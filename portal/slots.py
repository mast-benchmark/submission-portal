"""Slot bookkeeping per (team, track, language): manifests, receipts, atomic commits.

Layout in storage (spec §8):

    slots/{track}/{lang}/{team_slug}/{n}/run.jsonl.gz     n in 1..3
    slots/{track}/{lang}/{team_slug}/{n}/receipt.json
    receipts/{track}/{lang}/{team_slug}/{ts}-{sha8}.json  every upload ever, never deleted
    receipts/{track}/bulk/{team_slug}/{ts}-{sha8}.json    one per whole-track zip upload
    manifests/{track}/{lang}/{team_slug}.json             slot -> metadata
    rejected/{ts}-{hash}.json                             rejected team names, one file per flush window

A single-language upload and a whole-track zip upload share the same slot
model; the zip form fills several languages in one atomic commit.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Optional

from . import tmpfiles
from .storage import Add, Conflict, Storage

GZIP_MAGIC = b"\x1f\x8b"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def ts_compact(t: dt.datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%SZ")


class SlotError(Exception):
    """User-facing refusal (cap reached, duplicate upload, bad slot choice, busy)."""


@dataclass
class Prepared:
    """An upload that passed validation and is ready to be committed. The gzip lives on disk, not in memory."""
    gz_path: Path
    content_sha256: str           # sha256 of the decompressed JSONL
    stored_sha256: str            # sha256 of run.jsonl.gz as stored
    size_bytes: int               # decompressed
    original_filename: str
    records: int
    warnings: int
    warning_kinds: list[str]
    report: dict[str, Any]
    llm: str = ""            # read from the records by the validator
    retriever: str = ""

    def read_gz(self) -> bytes:
        return self.gz_path.read_bytes()


class _Prefixed:
    """Read-only stream that replays already-consumed bytes before the underlying stream."""

    def __init__(self, head: bytes, src: BinaryIO) -> None:
        self._head, self._src = head, src

    def read(self, n: int = -1) -> bytes:
        if self._head:
            if n is None or n < 0:
                b, self._head = self._head + self._src.read(), b""
                return b
            b, self._head = self._head[:n], self._head[n:]
            if len(b) < n:
                b += self._src.read(n - len(b))
            return b
        return self._src.read(n)


class _HashingWriter:
    def __init__(self, fh: BinaryIO) -> None:
        self.fh, self.h, self.n = fh, hashlib.sha256(), 0

    def write(self, b: bytes) -> int:
        self.h.update(b)
        self.n += len(b)
        return self.fh.write(b)

    def flush(self) -> None:
        self.fh.flush()


def prepare_stream(src: BinaryIO, original_filename: str, report: dict[str, Any],
                   gz_path: Optional[Path] = None, chunk: int = 1 << 20) -> Prepared:
    """Stream a validated upload (plain or gzipped) into a deterministic gzip file, hashing both forms.

    Memory stays at one chunk regardless of file size.
    """
    gz_path = gz_path or tmpfiles.new_path("mast-run-", ".jsonl.gz")
    head = src.read(2)
    reader = gzip.GzipFile(fileobj=_Prefixed(head, src), mode="rb") if head == GZIP_MAGIC else _Prefixed(head, src)  # type: ignore[arg-type]
    content_h = hashlib.sha256()
    size = 0
    with open(gz_path, "wb") as out:
        writer = _HashingWriter(out)
        with gzip.GzipFile(fileobj=writer, mode="wb", compresslevel=6, mtime=0) as gz:  # type: ignore[arg-type]
            while True:
                data = reader.read(chunk)
                if not data:
                    break
                content_h.update(data)
                size += len(data)
                gz.write(data)
    fr = report["files"][0] if report.get("files") else {}
    kinds = sorted({f["kind"] for f in fr.get("findings", []) if f["level"] == "warning"})
    return Prepared(
        gz_path=gz_path,
        content_sha256=content_h.hexdigest(),
        stored_sha256=writer.h.hexdigest(),
        size_bytes=size,
        original_filename=original_filename,
        records=fr.get("records", 0),
        warnings=fr.get("warnings", 0),
        warning_kinds=kinds,
        report=report,
        llm=fr.get("llm") or "",
        retriever=fr.get("retriever") or "",
    )


def prepare_bytes(raw: bytes, original_filename: str, report: dict[str, Any]) -> Prepared:
    return prepare_stream(io.BytesIO(raw), original_filename, report)


def prepare(path: Path, original_filename: str, report: dict[str, Any]) -> Prepared:
    with open(path, "rb") as fh:
        return prepare_stream(fh, original_filename, report)


@dataclass
class Meta:
    """What the form adds on top of the file: the file itself says llm, retriever and language."""
    system_type: str
    submitter_email: str


class SlotKey:
    def __init__(self, track: str, lang: str, team_slug: str) -> None:
        self.track, self.lang, self.team_slug = track, lang, team_slug

    @property
    def manifest_path(self) -> str:
        return f"manifests/{self.track}/{self.lang}/{self.team_slug}.json"

    def slot_dir(self, n: int) -> str:
        return f"slots/{self.track}/{self.lang}/{self.team_slug}/{n}"

    def receipt_path(self, t: dt.datetime, sha: str) -> str:
        return f"receipts/{self.track}/{self.lang}/{self.team_slug}/{ts_compact(t)}-{sha[:8]}.json"


@dataclass
class PlanItem:
    lang: str
    action: str                     # slot | replace | unchanged | skipped_full | capped
    slot: Optional[int] = None
    replaced: Optional[dict[str, Any]] = None
    note: str = ""

    @property
    def writes(self) -> bool:
        return self.action in ("slot", "replace")


def empty_manifest(key: SlotKey, team_name: str) -> dict[str, Any]:
    return {"team": team_name, "team_slug": key.team_slug, "track": key.track, "language": key.lang,
            "slots": {}, "uploads": 0, "updated_at": None}


def _load_manifest(raw: Optional[bytes], key: SlotKey, team_name: str) -> dict[str, Any]:
    if raw is None:
        return empty_manifest(key, team_name)
    m = json.loads(raw.decode("utf-8"))
    m.setdefault("slots", {})
    m.setdefault("uploads", 0)
    return m


class SlotService:
    def __init__(self, storage: Storage, *, max_slots: int = 3, max_uploads: int = 10, attempts: int = 5) -> None:
        self.storage = storage
        self.max_slots, self.max_uploads, self.attempts = max_slots, max_uploads, attempts

    # ---- reads
    def manifest(self, key: SlotKey, team_name: str = "") -> tuple[dict[str, Any], str]:
        raw, rev = self.storage.read_versioned(key.manifest_path)
        return _load_manifest(raw, key, team_name), rev

    def manifests(self, track: str, team_slug: str, team_name: str, langs) -> tuple[dict[str, dict[str, Any]], str]:
        keys = {lang: SlotKey(track, lang, team_slug) for lang in langs}
        raw, rev = self.storage.read_many([k.manifest_path for k in keys.values()])
        return {lang: _load_manifest(raw.get(k.manifest_path), k, team_name) for lang, k in keys.items()}, rev

    def filled_slots(self, manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
        return {int(k): v for k, v in manifest.get("slots", {}).items()}

    def next_free_slot(self, manifest: dict[str, Any]) -> Optional[int]:
        used = self.filled_slots(manifest)
        for n in range(1, self.max_slots + 1):
            if n not in used:
                return n
        return None

    def languages_submitted(self, track: str, team_slug: str) -> set[str]:
        """Languages of this track for which the team has at least one slot filled."""
        out: set[str] = set()
        for f in self.storage.list_files(f"manifests/{track}"):
            parts = f.split("/")
            if len(parts) == 4 and parts[3] == f"{team_slug}.json":
                raw = self.storage.read(f)
                if raw and json.loads(raw.decode("utf-8")).get("slots"):
                    out.add(parts[2])
        return out

    # ---- planning
    def plan_one(self, manifest: dict[str, Any], prepared: Prepared, *, replace_slot: Optional[int] = None,
                 replace_oldest: bool = False) -> PlanItem:
        lang = manifest.get("language", "")
        used = self.filled_slots(manifest)
        for n, s in used.items():
            if s.get("content_sha256") == prepared.content_sha256:
                return PlanItem(lang, "unchanged", n, note=f"identical to slot {n} (uploaded {s.get('uploaded_at', '')[:16]})")
        if manifest.get("uploads", 0) >= self.max_uploads:
            return PlanItem(lang, "capped", note=f"cap of {self.max_uploads} uploads reached; email the organizers")
        if replace_slot is not None:
            if replace_slot not in used:
                raise SlotError(f"Slot {replace_slot} is empty; choose a filled slot to replace or submit to a free one.")
            return PlanItem(lang, "replace", replace_slot, used[replace_slot])
        free = self.next_free_slot(manifest)
        if free is not None:
            return PlanItem(lang, "slot", free)
        if replace_oldest:
            n = min(used, key=lambda k: used[k].get("uploaded_at", ""))
            return PlanItem(lang, "replace", n, used[n])
        return PlanItem(lang, "skipped_full", note=f"all {self.max_slots} slots are filled; tick 'replace the oldest run' to replace one")

    def plan(self, manifests: dict[str, dict[str, Any]], items: dict[str, Prepared], *,
             replace_oldest: bool = False) -> list[PlanItem]:
        return [self.plan_one(manifests[lang], items[lang], replace_oldest=replace_oldest) for lang in sorted(items)]

    # ---- writes
    def _ops_for(self, key: SlotKey, team_name: str, manifest: dict[str, Any], item: PlanItem,
                 prepared: Prepared, meta: Meta, now: dt.datetime) -> tuple[list[Add], dict[str, Any]]:
        receipt_id = f"{ts_compact(now)}-{prepared.stored_sha256[:8]}"
        old = item.replaced
        entry = {
            "slot": item.slot, "receipt_id": receipt_id,
            "sha256": prepared.stored_sha256, "content_sha256": prepared.content_sha256,
            "size_bytes": prepared.size_bytes, "records": prepared.records,
            "uploaded_at": now.isoformat(), "original_filename": prepared.original_filename,
            "llm": prepared.llm, "retriever": prepared.retriever, "system_type": meta.system_type,
            "submitter_email": meta.submitter_email,
            "warnings": prepared.warnings, "warning_kinds": prepared.warning_kinds,
            "replaced": {"receipt_id": old.get("receipt_id"), "sha256": old.get("sha256")} if old else None,
        }
        receipt = {**entry, "team": team_name, "team_slug": key.team_slug, "track": key.track,
                   "language": key.lang, "validator": prepared.report.get("validator"), "report": prepared.report}
        manifest["slots"][str(item.slot)] = entry
        manifest["uploads"] = manifest.get("uploads", 0) + 1
        manifest["updated_at"] = now.isoformat()
        manifest["team"] = team_name
        ops = [
            Add(f"{key.slot_dir(item.slot)}/run.jsonl.gz", prepared.gz_path),
            Add(f"{key.slot_dir(item.slot)}/receipt.json", json.dumps(receipt, indent=2)),
            Add(key.receipt_path(now, prepared.stored_sha256), json.dumps(receipt, indent=2)),
            Add(key.manifest_path, json.dumps(manifest, indent=2)),
        ]
        return ops, receipt

    def submit(self, key: SlotKey, team_name: str, prepared: Prepared, meta: Meta,
               replace_slot: Optional[int] = None) -> dict[str, Any]:
        """Single language: blob + receipt + manifest in one commit; retry on conflict from fresh state."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.attempts):
            manifest, rev = self.manifest(key, team_name)
            item = self.plan_one(manifest, prepared, replace_slot=replace_slot)
            if item.action == "unchanged":
                raise SlotError(f"An identical file already occupies slot {item.slot} ({item.note}).")
            if item.action == "capped":
                raise SlotError(f"This team has reached the cap of {self.max_uploads} uploads for "
                                f"{key.track}/{key.lang}. Email the organizers if you need more.")
            if item.action == "skipped_full":
                raise SlotError(f"All {self.max_slots} slots are filled; choose which one to replace.")
            ops, receipt = self._ops_for(key, team_name, manifest, item, prepared, meta, utcnow())
            try:
                self.storage.commit(ops, f"{team_name}: {key.track}/{key.lang} slot {item.slot} ({receipt['receipt_id']})",
                                    parent=rev)
                return receipt
            except Conflict as exc:
                last_exc = exc
                time.sleep(0.3 * (attempt + 1) + random.random() * 0.5)
        raise SlotError(f"Could not record the submission after {self.attempts} attempts (busy). "
                        f"Nothing was recorded; please retry. ({last_exc})")

    def submit_many(self, track: str, team_slug: str, team_name: str, items: dict[str, Prepared], meta: Meta,
                    *, replace_oldest: bool = False) -> dict[str, Any]:
        """Whole-track zip: every writable language in one atomic commit, planned from fresh state."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.attempts):
            manifests, rev = self.manifests(track, team_slug, team_name, sorted(items))
            plan = self.plan(manifests, items, replace_oldest=replace_oldest)
            now = utcnow()
            ops: list[Add] = []
            receipts: dict[str, dict[str, Any]] = {}
            for item in plan:
                if item.writes:
                    key = SlotKey(track, item.lang, team_slug)
                    o, r = self._ops_for(key, team_name, manifests[item.lang], item, items[item.lang], meta, now)
                    ops.extend(o)
                    receipts[item.lang] = r
            digest = hashlib.sha256("".join(items[l].content_sha256 for l in sorted(items)).encode()).hexdigest()
            bulk_id = f"{ts_compact(now)}-{digest[:8]}"
            summary = {
                "bulk_receipt_id": bulk_id, "stored": bool(ops), "team": team_name, "team_slug": team_slug, "track": track,
                "uploaded_at": now.isoformat(), "system_type": meta.system_type, "submitter_email": meta.submitter_email,
                "items": [{"language": it.lang, "action": it.action, "slot": it.slot, "note": it.note,
                           "llm": items[it.lang].llm, "retriever": items[it.lang].retriever,
                           "receipt_id": receipts.get(it.lang, {}).get("receipt_id"),
                           "sha256": receipts.get(it.lang, {}).get("sha256"),
                           "replaced": receipts.get(it.lang, {}).get("replaced")} for it in plan],
            }
            if not ops:
                return summary  # nothing to write (all unchanged / skipped / capped)
            ops.append(Add(f"receipts/{track}/bulk/{team_slug}/{bulk_id}.json", json.dumps(summary, indent=2)))
            langs = ", ".join(it.lang for it in plan if it.writes)
            try:
                self.storage.commit(ops, f"{team_name}: {track} zip upload [{langs}] ({bulk_id})", parent=rev)
                return summary
            except Conflict as exc:
                last_exc = exc
                time.sleep(0.3 * (attempt + 1) + random.random() * 0.5)
        raise SlotError(f"Could not record the submission after {self.attempts} attempts (busy). "
                        f"Nothing was recorded; please retry. ({last_exc})")

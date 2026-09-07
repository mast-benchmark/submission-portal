"""Slot bookkeeping for one (team, track, language): manifests, receipts, atomic commits.

Layout in storage (spec §8):

    slots/{track}/{lang}/{team_slug}/{n}/run.jsonl.gz     n in 1..3
    slots/{track}/{lang}/{team_slug}/{n}/receipt.json
    receipts/{track}/{lang}/{team_slug}/{ts}-{sha8}.json  every upload ever, never deleted
    manifests/{track}/{lang}/{team_slug}.json             slot -> metadata
    rejected/{ts}-{slug}.json                             team names that matched nothing
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .storage import Add, Conflict, Delete, Storage

GZIP_MAGIC = b"\x1f\x8b"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def ts_compact(t: dt.datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%SZ")


class SlotError(Exception):
    """User-facing refusal (cap reached, duplicate upload, bad slot choice)."""


@dataclass
class Prepared:
    """An upload that passed validation and is ready to be committed."""
    gz_bytes: bytes
    content_sha256: str           # sha256 of the decompressed JSONL
    stored_sha256: str            # sha256 of run.jsonl.gz as stored
    size_bytes: int               # decompressed
    original_filename: str
    records: int
    warnings: int
    warning_kinds: list[str]
    report: dict[str, Any]


def prepare(path: Path, original_filename: str, report: dict[str, Any]) -> Prepared:
    """Read the validated upload once, normalize to gzip (deterministic), hash both forms."""
    raw = Path(path).read_bytes()
    if raw.startswith(GZIP_MAGIC):
        content = gzip.decompress(raw)
    else:
        content = raw
    buf = gzip.compress(content, compresslevel=6, mtime=0)
    fr = report["files"][0] if report.get("files") else {}
    kinds = sorted({f["kind"] for f in fr.get("findings", []) if f["level"] == "warning"})
    return Prepared(
        gz_bytes=buf,
        content_sha256=hashlib.sha256(content).hexdigest(),
        stored_sha256=hashlib.sha256(buf).hexdigest(),
        size_bytes=len(content),
        original_filename=original_filename,
        records=fr.get("records", 0),
        warnings=fr.get("warnings", 0),
        warning_kinds=kinds,
        report=report,
    )


@dataclass
class Meta:
    llm: str
    retriever: str
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


def empty_manifest(key: SlotKey, team_name: str) -> dict[str, Any]:
    return {"team": team_name, "team_slug": key.team_slug, "track": key.track, "language": key.lang,
            "slots": {}, "uploads": 0, "updated_at": None}


class SlotService:
    def __init__(self, storage: Storage, *, max_slots: int = 3, max_uploads: int = 10, attempts: int = 5) -> None:
        self.storage = storage
        self.max_slots, self.max_uploads, self.attempts = max_slots, max_uploads, attempts

    # ---- reads
    def manifest(self, key: SlotKey, team_name: str = "") -> tuple[dict[str, Any], str]:
        raw, rev = self.storage.read_versioned(key.manifest_path)
        if raw is None:
            return empty_manifest(key, team_name), rev
        m = json.loads(raw.decode("utf-8"))
        m.setdefault("slots", {})
        m.setdefault("uploads", 0)
        return m, rev

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

    # ---- writes
    def log_rejected_name(self, name: str, track: str, lang: str) -> None:
        t = utcnow()
        slug = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        try:
            self.storage.commit([Add(f"rejected/{ts_compact(t)}-{slug}.json",
                                     json.dumps({"name": name, "track": track, "language": lang, "at": t.isoformat()}))],
                                f"rejected team name ({track}/{lang})")
        except Exception:
            pass  # never let logging break a submission attempt

    def submit(self, key: SlotKey, team_name: str, prepared: Prepared, meta: Meta,
               replace_slot: Optional[int] = None) -> dict[str, Any]:
        """Commit blob + receipt + manifest atomically; retry on conflict from fresh state."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.attempts):
            manifest, rev = self.manifest(key, team_name)
            used = self.filled_slots(manifest)
            if manifest.get("uploads", 0) >= self.max_uploads:
                raise SlotError(f"This team has reached the cap of {self.max_uploads} uploads for "
                                f"{key.track}/{key.lang}. Email the organizers if you need more.")
            for n, s in used.items():
                if s.get("content_sha256") == prepared.content_sha256:
                    raise SlotError(f"An identical file already occupies slot {n} (uploaded {s.get('uploaded_at')}).")
            if replace_slot is not None:
                if replace_slot not in used:
                    raise SlotError(f"Slot {replace_slot} is empty; choose a filled slot to replace or submit to a free one.")
                slot = replace_slot
            else:
                free = self.next_free_slot(manifest)
                if free is None:
                    raise SlotError(f"All {self.max_slots} slots are filled; choose which one to replace.")
                slot = free
            now = utcnow()
            receipt_id = f"{ts_compact(now)}-{prepared.stored_sha256[:8]}"
            old = used.get(slot)
            entry = {
                "slot": slot, "receipt_id": receipt_id,
                "sha256": prepared.stored_sha256, "content_sha256": prepared.content_sha256,
                "size_bytes": prepared.size_bytes, "records": prepared.records,
                "uploaded_at": now.isoformat(), "original_filename": prepared.original_filename,
                "llm": meta.llm, "retriever": meta.retriever, "system_type": meta.system_type,
                "submitter_email": meta.submitter_email,
                "warnings": prepared.warnings, "warning_kinds": prepared.warning_kinds,
                "replaced": {"receipt_id": old.get("receipt_id"), "sha256": old.get("sha256")} if old else None,
            }
            receipt = {**entry, "team": team_name, "team_slug": key.team_slug, "track": key.track,
                       "language": key.lang, "validator": prepared.report.get("validator"),
                       "report": prepared.report}
            manifest["slots"][str(slot)] = entry
            manifest["uploads"] = manifest.get("uploads", 0) + 1
            manifest["updated_at"] = now.isoformat()
            manifest["team"] = team_name
            ops: list = [
                Add(f"{key.slot_dir(slot)}/run.jsonl.gz", prepared.gz_bytes),
                Add(f"{key.slot_dir(slot)}/receipt.json", json.dumps(receipt, indent=2)),
                Add(key.receipt_path(now, prepared.stored_sha256), json.dumps(receipt, indent=2)),
                Add(key.manifest_path, json.dumps(manifest, indent=2)),
            ]
            # The old blob had the same path (overwritten by the Add above); nothing else to delete.
            try:
                self.storage.commit(ops, f"{team_name}: {key.track}/{key.lang} slot {slot} ({receipt_id})", parent=rev)
                return receipt
            except Conflict as exc:
                last_exc = exc
                time.sleep(0.3 * (attempt + 1) + random.random() * 0.5)
        raise SlotError(f"Could not record the submission after {self.attempts} attempts (busy). "
                        f"Nothing was recorded; please retry. ({last_exc})")

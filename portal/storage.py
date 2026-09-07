"""Storage backends with one contract: versioned reads and atomic multi-file commits.

A commit carries the repository revision it was planned against (``parent``);
if anything was committed in between, the backend raises :class:`Conflict` and
the caller re-reads and retries. ``HfStorage`` maps this onto Hub commits with
``parent_commit``; ``LocalStorage`` keeps a revision counter on disk.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Union

Content = Union[bytes, str, Path]


class Conflict(Exception):
    """The parent revision no longer matches the head; re-read and retry."""


@dataclass
class Add:
    path: str
    content: Content


@dataclass
class Delete:
    path: str


Op = Union[Add, Delete]


def _to_bytes(content: Content) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    return Path(content).read_bytes()


class Storage(Protocol):
    def read(self, path: str) -> Optional[bytes]: ...
    def read_versioned(self, path: str) -> tuple[Optional[bytes], str]: ...
    def read_many(self, paths: list[str]) -> tuple[dict[str, Optional[bytes]], str]: ...
    def commit(self, ops: list[Op], message: str, parent: Optional[str] = None) -> str: ...
    def list_files(self, prefix: str) -> list[str]: ...
    def exists(self, path: str) -> bool: ...


class LocalStorage:
    """Directory-backed. The revision is a counter bumped by every commit."""

    REV_FILE = ".rev"

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _p(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"path escapes storage root: {path}")
        return p

    def _rev(self) -> str:
        p = self.root / self.REV_FILE
        return p.read_text().strip() if p.is_file() else "0"

    def read(self, path: str) -> Optional[bytes]:
        p = self._p(path)
        return p.read_bytes() if p.is_file() else None

    def read_versioned(self, path: str) -> tuple[Optional[bytes], str]:
        with self._lock:
            return self.read(path), self._rev()

    def read_many(self, paths: list[str]) -> tuple[dict[str, Optional[bytes]], str]:
        with self._lock:
            return {p: self.read(p) for p in paths}, self._rev()

    def commit(self, ops: list[Op], message: str, parent: Optional[str] = None) -> str:
        with self._lock:
            current = self._rev()
            if parent is not None and parent != current:
                raise Conflict(f"parent rev {parent} != head {current}")
            for op in ops:
                p = self._p(op.path)
                if isinstance(op, Add):
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = tempfile.NamedTemporaryFile(dir=p.parent, delete=False)
                    try:
                        tmp.write(_to_bytes(op.content))
                        tmp.close()
                        os.replace(tmp.name, p)
                    finally:
                        if os.path.exists(tmp.name):
                            os.unlink(tmp.name)
                elif p.is_file():
                    p.unlink()
            new = str(int(current) + 1)
            (self.root / self.REV_FILE).write_text(new)
            return new

    def list_files(self, prefix: str) -> list[str]:
        base = self._p(prefix)
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def exists(self, path: str) -> bool:
        return self._p(path).is_file()


class HfStorage:
    """Private dataset repo on the Hub. One ``commit`` == one atomic Hub commit."""

    def __init__(self, repo_id: str, token: str) -> None:
        from huggingface_hub import HfApi

        self.repo_id = repo_id
        self.api = HfApi(token=token)
        self.token = token

    def head(self) -> str:
        return self.api.dataset_info(self.repo_id).sha

    def read(self, path: str) -> Optional[bytes]:
        return self.read_versioned(path)[0]

    def read_versioned(self, path: str) -> tuple[Optional[bytes], str]:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import EntryNotFoundError

        rev = self.head()
        try:
            local = hf_hub_download(self.repo_id, path, repo_type="dataset", revision=rev, token=self.token)
        except EntryNotFoundError:
            return None, rev
        return Path(local).read_bytes(), rev

    def read_many(self, paths: list[str]) -> tuple[dict[str, Optional[bytes]], str]:
        """All paths at one revision, in one snapshot call."""
        from huggingface_hub import snapshot_download

        rev = self.head()
        if not paths:
            return {}, rev
        local = Path(snapshot_download(self.repo_id, repo_type="dataset", revision=rev, token=self.token,
                                       allow_patterns=list(paths)))
        out: dict[str, Optional[bytes]] = {}
        for p in paths:
            f = local / p
            out[p] = f.read_bytes() if f.is_file() else None
        return out, rev

    def commit(self, ops: list[Op], message: str, parent: Optional[str] = None) -> str:
        from huggingface_hub import CommitOperationAdd, CommitOperationDelete
        from huggingface_hub.utils import HfHubHTTPError

        operations = []
        for op in ops:
            if isinstance(op, Add):
                src = str(op.content) if isinstance(op.content, Path) else _to_bytes(op.content)
                operations.append(CommitOperationAdd(path_in_repo=op.path, path_or_fileobj=src))
            else:
                if self.exists(op.path):
                    operations.append(CommitOperationDelete(path_in_repo=op.path))
        try:
            info = self.api.create_commit(self.repo_id, operations=operations, commit_message=message,
                                          repo_type="dataset", parent_commit=parent)
        except HfHubHTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (409, 412):
                raise Conflict(str(exc)) from exc
            raise
        return info.oid or ""

    def list_files(self, prefix: str) -> list[str]:
        try:
            files = self.api.list_repo_files(self.repo_id, repo_type="dataset")
        except Exception:
            return []
        prefix = prefix.rstrip("/") + "/"
        return sorted(f for f in files if f.startswith(prefix))

    def exists(self, path: str) -> bool:
        return self.api.file_exists(self.repo_id, path, repo_type="dataset")


def make_storage(settings) -> Storage:
    if settings.storage_backend == "hf":
        if not settings.hf_token:
            raise RuntimeError("STORAGE_BACKEND=hf needs HF_TOKEN")
        return HfStorage(settings.submissions_repo, settings.hf_token)
    return LocalStorage(settings.storage_root)

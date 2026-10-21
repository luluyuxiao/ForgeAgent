"""Recoverable file snapshots taken immediately before mutating Tools execute."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ToolCall
from .tools import Tool, Workspace

_DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_VALID_RESULT_KINDS = frozenset({"file", "missing", "other"})


class CheckpointError(RuntimeError):
    """A checkpoint could not be captured, committed, or restored safely."""


@dataclass
class FileSnapshot:
    path: str
    existed: bool
    content: bytes = b""
    mode: int | None = None
    result_kind: str | None = None
    result_sha256: str | None = None


@dataclass
class Checkpoint:
    call_id: str
    tool: str
    files: list[FileSnapshot]


class CheckpointManager:
    def __init__(
        self,
        workspace: str | Path,
        *,
        limit: int = 50,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    ):
        if max_file_bytes < 1 or max_total_bytes < 1:
            raise ValueError("checkpoint byte limits must be positive")
        self.workspace = Workspace(workspace)
        self.limit = max(1, limit)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._items: list[Checkpoint] = []
        self._pending: dict[str, Checkpoint] = {}
        self._lock = threading.RLock()

    def begin(self, call: ToolCall, tool: Tool) -> Checkpoint | None:
        """Capture a pending snapshot that must be completed after Tool execution."""
        checkpoint = self._snapshot(call, tool)
        if checkpoint is None:
            return None
        with self._lock:
            if call.id in self._pending:
                raise CheckpointError(f"duplicate pending checkpoint call id: {call.id}")
            self._make_room(self._size(checkpoint), additional_count=1)
            self._pending[call.id] = checkpoint
        return checkpoint

    def complete(self, call_id: str, *, succeeded: bool) -> str | None:
        """Commit a successful snapshot or roll back and discard a failed call."""
        with self._lock:
            checkpoint = self._pending.get(call_id)
        if checkpoint is None:
            return None

        if not succeeded:
            try:
                self._restore_files(checkpoint)
            except (OSError, CheckpointError, ValueError) as error:
                with self._lock:
                    checkpoint = self._pending.pop(call_id, checkpoint)
                    self._append(checkpoint)
                return f"rollback_failed: {type(error).__name__}: {error}"
            with self._lock:
                self._pending.pop(call_id, None)
            return "rolled_back"

        try:
            for snapshot in checkpoint.files:
                target = self.workspace.resolve(snapshot.path, must_exist=False)
                snapshot.result_kind, snapshot.result_sha256 = _fingerprint(target)
        except (OSError, ValueError) as error:
            with self._lock:
                checkpoint = self._pending.pop(call_id, checkpoint)
                self._append(checkpoint)
            return f"commit_unverified: {type(error).__name__}: {error}"

        with self._lock:
            checkpoint = self._pending.pop(call_id, checkpoint)
            self._append(checkpoint)
        return "committed"

    def capture(self, call: ToolCall, tool: Tool) -> Checkpoint | None:
        """Capture an immediate legacy checkpoint for direct registry callers.

        Runtime integrations should use ``begin`` and ``complete`` so failed calls
        are discarded and successful calls gain conflict detection.
        """
        checkpoint = self._snapshot(call, tool)
        if checkpoint is None:
            return None
        with self._lock:
            self._make_room(self._size(checkpoint), additional_count=1)
            self._append(checkpoint)
        return checkpoint

    def undo(self) -> str:
        with self._lock:
            if not self._items:
                return "Nothing to undo."
            checkpoint = self._items[-1]
            try:
                conflicts = self._conflicts(checkpoint)
            except (OSError, ValueError) as error:
                return (
                    f"Undo failed for {checkpoint.tool} ({checkpoint.call_id}): "
                    f"{type(error).__name__}: {error}"
                )
            if conflicts:
                return (
                    f"Undo blocked for {checkpoint.tool} ({checkpoint.call_id}); "
                    "files changed after the Tool call: "
                    + ", ".join(conflicts)
                )
            try:
                restored = self._restore_files(checkpoint)
            except (OSError, CheckpointError, ValueError) as error:
                return (
                    f"Undo failed for {checkpoint.tool} ({checkpoint.call_id}): "
                    f"{type(error).__name__}: {error}"
                )
            self._items.pop()
        details = ", ".join(restored) if restored else "no file changes"
        return f"Undid {checkpoint.tool} ({checkpoint.call_id}): {details}"

    def export(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "call_id": item.call_id,
                    "tool": item.tool,
                    "files": [
                        {
                            "path": file.path,
                            "existed": file.existed,
                            "content_b64": base64.b64encode(file.content).decode("ascii"),
                            "mode": file.mode,
                            "result_kind": file.result_kind,
                            "result_sha256": file.result_sha256,
                        }
                        for file in item.files
                    ],
                }
                for item in self._items
            ]

    def restore(self, data: list[dict[str, Any]]) -> None:
        items: list[Checkpoint] = []
        total_bytes = 0
        for raw in data[-self.limit :]:
            files: list[FileSnapshot] = []
            seen_paths: set[str] = set()
            for file in raw.get("files", []):
                path = str(file["path"])
                if not path or path in seen_paths:
                    raise ValueError(f"invalid or duplicate checkpoint path: {path!r}")
                seen_paths.add(path)
                self.workspace.resolve(path, must_exist=False)
                content = base64.b64decode(
                    str(file.get("content_b64") or ""), validate=True
                )
                if len(content) > self.max_file_bytes:
                    raise ValueError(f"checkpoint file exceeds byte limit: {path}")
                total_bytes += len(content)
                if total_bytes > self.max_total_bytes:
                    raise ValueError("checkpoint history exceeds total byte limit")
                raw_result_kind = file.get("result_kind")
                result_kind = (
                    str(raw_result_kind) if raw_result_kind is not None else None
                )
                if result_kind is not None and result_kind not in _VALID_RESULT_KINDS:
                    raise ValueError(f"invalid checkpoint result kind: {result_kind}")
                raw_mode = file.get("mode")
                mode = int(raw_mode) if raw_mode is not None else None
                if mode is not None and not 0 <= mode <= 0o7777:
                    raise ValueError(f"invalid checkpoint mode for {path}: {mode}")
                raw_digest = file.get("result_sha256")
                result_sha256 = str(raw_digest) if raw_digest is not None else None
                if result_sha256 is not None and (
                    len(result_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in result_sha256)
                ):
                    raise ValueError(f"invalid checkpoint digest for {path}")
                files.append(
                    FileSnapshot(
                        path=path,
                        existed=bool(file["existed"]),
                        content=content,
                        mode=mode,
                        result_kind=result_kind,
                        result_sha256=result_sha256,
                    )
                )
            items.append(Checkpoint(str(raw["call_id"]), str(raw["tool"]), files))
        with self._lock:
            self._items = items
            self._pending.clear()

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._items)

    def _snapshot(self, call: ToolCall, tool: Tool) -> Checkpoint | None:
        paths = tool.checkpoint_paths(call.arguments)
        if not paths:
            return None
        files: list[FileSnapshot] = []
        checkpoint_bytes = 0
        for raw in paths:
            target = self.workspace.resolve(raw, must_exist=False)
            if target.exists() and not target.is_file():
                raise CheckpointError(f"checkpoint target is not a regular file: {raw}")
            existed = target.is_file()
            size = target.stat().st_size if existed else 0
            if size > self.max_file_bytes:
                raise CheckpointError(
                    f"checkpoint file exceeds {self.max_file_bytes} bytes: {raw}"
                )
            content = target.read_bytes() if existed else b""
            if len(content) > self.max_file_bytes:
                raise CheckpointError(
                    f"checkpoint file exceeds {self.max_file_bytes} bytes: {raw}"
                )
            checkpoint_bytes += len(content)
            if checkpoint_bytes > self.max_total_bytes:
                raise CheckpointError("checkpoint exceeds total byte limit")
            files.append(
                FileSnapshot(
                    path=target.relative_to(self.workspace.root).as_posix(),
                    existed=existed,
                    content=content,
                    mode=stat.S_IMODE(target.stat().st_mode) if existed else None,
                )
            )
        return Checkpoint(call.id, call.name, files)

    def _conflicts(self, checkpoint: Checkpoint) -> list[str]:
        conflicts: list[str] = []
        for snapshot in checkpoint.files:
            if snapshot.result_kind is None:
                continue
            target = self.workspace.resolve(snapshot.path, must_exist=False)
            kind, digest = _fingerprint(target)
            if kind != snapshot.result_kind or digest != snapshot.result_sha256:
                conflicts.append(snapshot.path)
        return conflicts

    def _restore_files(self, checkpoint: Checkpoint) -> list[str]:
        targets: list[tuple[FileSnapshot, Path]] = []
        for snapshot in checkpoint.files:
            target = self.workspace.resolve(snapshot.path, must_exist=False)
            if not _matches_snapshot(snapshot, target):
                targets.append((snapshot, target))

        staged: dict[str, Path] = {}
        try:
            for snapshot, target in targets:
                if not snapshot.existed:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor, raw_temp = tempfile.mkstemp(
                    prefix=".forgeagent-checkpoint-", dir=target.parent
                )
                temp_path = Path(raw_temp)
                staged[snapshot.path] = temp_path
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(snapshot.content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if snapshot.mode is not None:
                    os.chmod(temp_path, snapshot.mode)

            restored: list[str] = []
            for snapshot, target in targets:
                if snapshot.existed:
                    os.replace(staged.pop(snapshot.path), target)
                    restored.append(f"restored {snapshot.path}")
                elif target.exists():
                    if not target.is_file():
                        raise CheckpointError(
                            f"refusing to remove non-file checkpoint target: {snapshot.path}"
                        )
                    target.unlink()
                    restored.append(f"removed newly-created {snapshot.path}")
            return restored
        finally:
            for temp_path in staged.values():
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def _make_room(self, size: int, *, additional_count: int) -> None:
        if size > self.max_total_bytes:
            raise CheckpointError("checkpoint exceeds total byte limit")
        while self._items and (
            len(self._items) + len(self._pending) + additional_count > self.limit
            or self._total_bytes() + size > self.max_total_bytes
        ):
            self._items.pop(0)
        if len(self._items) + len(self._pending) + additional_count > self.limit:
            raise CheckpointError("too many pending checkpoints")
        if self._total_bytes() + size > self.max_total_bytes:
            raise CheckpointError("pending checkpoints exceed total byte limit")

    def _append(self, checkpoint: Checkpoint) -> None:
        self._items.append(checkpoint)
        del self._items[:-self.limit]

    def _total_bytes(self) -> int:
        return sum(
            self._size(item) for item in [*self._items, *self._pending.values()]
        )

    @staticmethod
    def _size(checkpoint: Checkpoint) -> int:
        return sum(len(file.content) for file in checkpoint.files)


def _fingerprint(path: Path) -> tuple[str, str | None]:
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return "file", digest.hexdigest()
    if path.exists():
        return "other", None
    return "missing", None


def _matches_snapshot(snapshot: FileSnapshot, path: Path) -> bool:
    if not snapshot.existed:
        return not path.exists()
    if not path.is_file():
        return False
    _, digest = _fingerprint(path)
    expected = hashlib.sha256(snapshot.content).hexdigest()
    mode_matches = snapshot.mode is None or stat.S_IMODE(path.stat().st_mode) == snapshot.mode
    return digest == expected and mode_matches

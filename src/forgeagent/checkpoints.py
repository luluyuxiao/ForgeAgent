"""Recoverable file snapshots taken immediately before mutating Tools execute."""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ToolCall
from .tools import Tool, Workspace


@dataclass
class FileSnapshot:
    path: str
    existed: bool
    content: bytes = b""


@dataclass
class Checkpoint:
    call_id: str
    tool: str
    files: list[FileSnapshot]


class CheckpointManager:
    def __init__(self, workspace: str | Path, *, limit: int = 50):
        self.workspace = Workspace(workspace)
        self.limit = max(1, limit)
        self._items: list[Checkpoint] = []
        self._lock = threading.RLock()

    def capture(self, call: ToolCall, tool: Tool) -> Checkpoint | None:
        paths = tool.checkpoint_paths(call.arguments)
        if not paths:
            return None
        files: list[FileSnapshot] = []
        for raw in paths:
            target = self.workspace.resolve(raw, must_exist=False)
            existed = target.is_file()
            files.append(
                FileSnapshot(
                    path=target.relative_to(self.workspace.root).as_posix(),
                    existed=existed,
                    content=target.read_bytes() if existed else b"",
                )
            )
        checkpoint = Checkpoint(call.id, call.name, files)
        with self._lock:
            self._items.append(checkpoint)
            del self._items[:-self.limit]
        return checkpoint

    def undo(self) -> str:
        with self._lock:
            if not self._items:
                return "Nothing to undo."
            checkpoint = self._items.pop()
        restored: list[str] = []
        for snapshot in checkpoint.files:
            target = self.workspace.resolve(snapshot.path, must_exist=False)
            if snapshot.existed:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snapshot.content)
                restored.append(f"restored {snapshot.path}")
            elif target.exists() and target.is_file():
                target.unlink()
                restored.append(f"removed newly-created {snapshot.path}")
        return f"Undid {checkpoint.tool} ({checkpoint.call_id}): " + ", ".join(restored)

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
                        }
                        for file in item.files
                    ],
                }
                for item in self._items
            ]

    def restore(self, data: list[dict[str, Any]]) -> None:
        items: list[Checkpoint] = []
        for raw in data[-self.limit :]:
            files = [
                FileSnapshot(
                    path=str(file["path"]),
                    existed=bool(file["existed"]),
                    content=base64.b64decode(str(file.get("content_b64") or "")),
                )
                for file in raw.get("files", [])
            ]
            items.append(Checkpoint(str(raw["call_id"]), str(raw["tool"]), files))
        with self._lock:
            self._items = items

    @property
    def depth(self) -> int:
        return len(self._items)

"""Atomic local JSON sessions with separate Transcript and Active Context."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Message, Usage
from .policies import RunMode

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


@dataclass
class SessionState:
    session_id: str
    model: str
    mode: RunMode = RunMode.NORMAL
    transcript: list[Message] = field(default_factory=list)
    active_messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    active_tools: list[str] = field(default_factory=list)
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    version: int = 2

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mode"] = self.mode.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        usage = data.get("usage") or {}
        return cls(
            session_id=str(data["session_id"]),
            model=str(data.get("model") or ""),
            mode=RunMode(str(data.get("mode") or RunMode.NORMAL.value)),
            transcript=list(data.get("transcript") or []),
            active_messages=list(data.get("active_messages") or []),
            usage=Usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            ),
            checkpoints=list(data.get("checkpoints") or []),
            active_tools=[str(item) for item in (data.get("active_tools") or [])],
            updated_at=str(data.get("updated_at") or ""),
            version=int(data.get("version") or 1),
        )


class SessionStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser()
        self._lock = threading.RLock()

    def save(self, state: SessionState) -> Path:
        session_id = _validate_id(state.session_id)
        self.root.mkdir(parents=True, exist_ok=True)
        state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target = self.root / f"{session_id}.json"
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        with self._lock:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{session_id}.", suffix=".tmp", dir=self.root
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return target

    def load(self, session_id: str) -> SessionState:
        target = self.root / f"{_validate_id(session_id)}.json"
        with self._lock:
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except FileNotFoundError as error:
                raise KeyError(f"unknown session: {session_id}") from error
        if not isinstance(data, dict):
            raise TypeError(f"invalid session document: {target}")
        state = SessionState.from_dict(data)
        if state.version not in {1, 2}:
            raise ValueError(f"unsupported session version: {state.version}")
        return state

    def list(self) -> list[tuple[str, str]]:
        if not self.root.is_dir():
            return []
        sessions: list[tuple[str, str]] = []
        for path in self.root.glob("*.json"):
            try:
                state = self.load(path.stem)
            except (KeyError, ValueError, OSError, json.JSONDecodeError):
                continue
            sessions.append((state.session_id, state.updated_at))
        return sorted(sessions, key=lambda item: item[1], reverse=True)


def _validate_id(session_id: str) -> str:
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError(
            "session id must be 1-80 characters using letters, digits, dot, dash, or underscore"
        )
    return session_id

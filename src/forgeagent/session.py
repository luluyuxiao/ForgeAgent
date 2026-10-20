"""Transactional SQLite sessions with legacy JSON read compatibility."""

from __future__ import annotations

import json
import os
import re
import sqlite3
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
    memory: dict[str, Any] = field(default_factory=dict)
    active_skills: list[str] = field(default_factory=list)
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    version: int = 3

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
            memory=dict(data.get("memory") or {}),
            active_skills=[str(item) for item in (data.get("active_skills") or [])],
            updated_at=str(data.get("updated_at") or ""),
            version=int(data.get("version") or 1),
        )


class SessionStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser()
        self.database = self.root / "sessions.db"
        self._lock = threading.RLock()

    def save(self, state: SessionState) -> Path:
        session_id = _validate_id(state.session_id)
        self.root.mkdir(parents=True, exist_ok=True)
        state.version = 3
        state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions(session_id, updated_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (session_id, state.updated_at, payload),
            )
        return self.database

    def load(self, session_id: str) -> SessionState:
        session_id = _validate_id(session_id)
        with self._lock:
            data = self._load_database(session_id)
            if data is None:
                target = self.root / f"{session_id}.json"
                try:
                    data = json.loads(target.read_text(encoding="utf-8"))
                except FileNotFoundError as error:
                    raise KeyError(f"unknown session: {session_id}") from error
        if not isinstance(data, dict):
            raise TypeError(f"invalid session document for {session_id}")
        state = SessionState.from_dict(data)
        if state.version not in {1, 2, 3}:
            raise ValueError(f"unsupported session version: {state.version}")
        return state

    def list(self) -> list[tuple[str, str]]:
        sessions: dict[str, str] = {}
        if self.database.is_file():
            with self._lock, self._connect() as connection:
                for session_id, updated_at in connection.execute(
                    "SELECT session_id, updated_at FROM sessions ORDER BY updated_at DESC"
                ):
                    sessions[str(session_id)] = str(updated_at)
        if not self.root.is_dir():
            return list(sessions.items())
        for path in self.root.glob("*.json"):
            try:
                state = self.load(path.stem)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
            sessions.setdefault(state.session_id, state.updated_at)
        return sorted(sessions.items(), key=lambda item: item[1], reverse=True)

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass
        return connection

    def _load_database(self, session_id: str) -> dict[str, Any] | None:
        if not self.database.is_file():
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None


def _validate_id(session_id: str) -> str:
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError(
            "session id must be 1-80 characters using letters, digits, dot, dash, or underscore"
        )
    return session_id

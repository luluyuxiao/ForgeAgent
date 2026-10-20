"""Thread-safe structured runtime tracing."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Protocol


class TraceSink(Protocol):
    def emit(self, event: str, fields: dict[str, Any]) -> None: ...


class NullTrace:
    def emit(self, event: str, fields: dict[str, Any]) -> None:
        return None


class JsonlTrace:
    """Append JSON events without allowing telemetry failures to stop an Agent run."""

    _registry_lock: ClassVar[threading.Lock] = threading.Lock()
    _states: ClassVar[dict[Path, dict[str, Any]]] = {}

    def __init__(self, path: str | Path, *, include_content: bool = False):
        self.path = Path(path).expanduser().resolve()
        self.include_content = include_content
        with self._registry_lock:
            if self.path not in self._states:
                self._states[self.path] = {
                    "lock": threading.Lock(),
                    "sequence": _last_sequence(self.path),
                }
            self._state = self._states[self.path]

    def emit(self, event: str, fields: dict[str, Any]) -> None:
        safe_fields = dict(fields)
        if not self.include_content:
            safe_fields = _redact(safe_fields)
        with self._state["lock"]:
            self._state["sequence"] += 1
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "sequence": self._state["sequence"],
                "event": event,
                **safe_fields,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _last_sequence(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return 0
        return int(json.loads(lines[-1]).get("sequence") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"content", "arguments", "result", "prompt", "task"}:
                result[key] = f"[redacted; {len(str(item))} chars]"
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value

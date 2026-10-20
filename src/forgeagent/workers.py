"""Foreground Sub-Agents and bounded in-process background workers."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from .tools import Tool, ToolEffect

_RESULT_LIMIT = 24_000


@dataclass
class TaskRecord:
    task_id: str
    task: str
    status: str = "queued"
    result: str = ""
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class ChildRunResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class SubAgentManager:
    """Owns child task state; workers share infrastructure, not conversation state."""

    def __init__(
        self, run_task: Callable[[str], str | ChildRunResult], *, max_workers: int = 3
    ):
        self.run_task = run_task
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="forge-subagent"
        )
        self._records: dict[str, TaskRecord] = {}
        self._futures: dict[str, Future[str]] = {}
        self._lock = threading.RLock()

    def submit(self, task: str, *, background: bool) -> TaskRecord:
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(
            task_id=task_id,
            task=task,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._records[task_id] = record
        if background:
            future = self._pool.submit(self._execute, task_id)
            with self._lock:
                self._futures[task_id] = future
            return record
        self._execute(task_id)
        return self.get(task_id)

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            try:
                return TaskRecord(**asdict(self._records[task_id]))
            except KeyError as error:
                raise KeyError(f"unknown background task: {task_id}") from error

    def list(self) -> list[TaskRecord]:
        with self._lock:
            return [TaskRecord(**asdict(item)) for item in self._records.values()]

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            future = self._futures.get(task_id)
            if future is None or not future.cancel():
                return False
            record = self._records[task_id]
            record.status = "cancelled"
            record.completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return True

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _execute(self, task_id: str) -> str:
        with self._lock:
            record = self._records[task_id]
            record.status = "running"
            record.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            task = record.task
        try:
            child_result = self.run_task(task)
        except Exception as error:  # noqa: BLE001 - child failure must not crash parent
            with self._lock:
                record.status = "failed"
                record.error = f"{type(error).__name__}: {error}"
                record.completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return ""
        if isinstance(child_result, ChildRunResult):
            result = child_result.content
            prompt_tokens = child_result.prompt_tokens
            completion_tokens = child_result.completion_tokens
        else:
            result = child_result
            prompt_tokens = completion_tokens = 0
        bounded = result[:_RESULT_LIMIT]
        if len(result) > _RESULT_LIMIT:
            bounded += f"\n[Sub-Agent result truncated by {len(result) - _RESULT_LIMIT} chars]"
        with self._lock:
            record.status = "completed"
            record.result = bounded
            record.prompt_tokens = prompt_tokens
            record.completion_tokens = completion_tokens
            record.completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return bounded


class SubAgentTool(Tool):
    name = "subagent"
    description = (
        "Delegate an isolated reasoning task to a child Agent. Foreground returns the result; "
        "background returns a task id for later polling."
    )
    effect = ToolEffect.PROCESS
    capabilities = frozenset({"subagent", "process"})
    parallel_safe = False
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "run_mode": {"type": "string", "enum": ["foreground", "background"]},
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def __init__(self, manager: SubAgentManager):
        self.manager = manager

    def execute(self, task: str, run_mode: str = "foreground") -> str:
        if not task.strip():
            return "Error: Sub-Agent task must not be empty"
        if run_mode not in {"foreground", "background"}:
            return "Error: run_mode must be foreground or background"
        record = self.manager.submit(task, background=run_mode == "background")
        return json.dumps(asdict(record), ensure_ascii=False, indent=2)


class TaskStatusTool(Tool):
    name = "task_status"
    description = "Inspect one background Sub-Agent task or list all task states."
    effect = ToolEffect.READ
    capabilities = frozenset({"subagent"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "additionalProperties": False,
    }

    def __init__(self, manager: SubAgentManager):
        self.manager = manager

    def execute(self, task_id: str = "") -> str:
        try:
            value: Any = self.manager.get(task_id) if task_id else self.manager.list()
        except KeyError as error:
            return f"Error: {error}"
        if isinstance(value, list):
            value = [asdict(item) for item in value]
        else:
            value = asdict(value)
        return json.dumps(value, ensure_ascii=False, indent=2)

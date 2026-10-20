"""Bounded structured task memory kept outside compactable chat history."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .tools import Access, ResourceClaim, Tool, ToolEffect

_MAX_ITEMS = 100
_MAX_TEXT = 2_000
_MAX_CHARS = 24_000
_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked"})


def _clean(value: Any, limit: int = _MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit].rstrip()


def _next_id(prefix: str, items: list[dict[str, str]]) -> str:
    used = {item.get("id", "") for item in items}
    index = len(items) + 1
    while f"{prefix}-{index}" in used:
        index += 1
    return f"{prefix}-{index}"


@dataclass
class TaskMemory:
    """Goal, constraints, plan, decisions, and touched files for one runtime."""

    goal: str = ""
    constraints: list[dict[str, str]] = field(default_factory=list)
    plan: list[dict[str, str]] = field(default_factory=list)
    decisions: list[dict[str, str]] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)

    def set_goal(self, content: str) -> dict[str, Any]:
        value = _clean(content)
        if not value:
            raise ValueError("goal must not be empty")
        previous = self.goal
        self.goal = value
        self._check_size(previous_goal=previous)
        return self.to_dict()

    def add_constraint(self, content: str, *, source: str = "user") -> dict[str, str]:
        item = self._append(
            self.constraints,
            "constraint",
            {"content": content, "source": _clean(source, 40) or "user", "status": "active"},
        )
        return copy.deepcopy(item)

    def add_plan_step(self, content: str) -> dict[str, str]:
        return copy.deepcopy(
            self._append(self.plan, "step", {"content": content, "status": "pending"})
        )

    def update_plan_step(self, item_id: str, status: str) -> dict[str, str]:
        if status not in _PLAN_STATUSES:
            raise ValueError(f"invalid plan status: {status}")
        for item in self.plan:
            if item.get("id") == item_id:
                item["status"] = status
                return copy.deepcopy(item)
        raise KeyError(f"unknown plan step: {item_id}")

    def add_decision(self, content: str, *, reason: str = "") -> dict[str, str]:
        return copy.deepcopy(
            self._append(self.decisions, "decision", {"content": content, "reason": reason})
        )

    def record_file(self, path: str) -> None:
        value = str(path).strip()[:1_000]
        if value and value not in self.files_modified and len(self.files_modified) < 200:
            self.files_modified.append(value)
            if self._size() > _MAX_CHARS:
                self.files_modified.pop()

    def clear_plan(self) -> None:
        self.plan.clear()

    def clear(self) -> None:
        self.goal = ""
        self.constraints.clear()
        self.plan.clear()
        self.decisions.clear()
        self.files_modified.clear()

    def restore(self, data: dict[str, Any] | None) -> None:
        restored = self.from_dict(data)
        self.goal = restored.goal
        self.constraints = restored.constraints
        self.plan = restored.plan
        self.decisions = restored.decisions
        self.files_modified = restored.files_modified

    def render(self) -> str:
        sections: list[str] = []
        if self.goal:
            sections.append("## Goal\n" + self.goal)
        active = [item for item in self.constraints if item.get("status") == "active"]
        if active:
            sections.append(
                "## Constraints\n"
                "These are semantic instructions, not permission or sandbox rules.\n"
                + "\n".join(f"- [{item['id']}] {item['content']}" for item in active)
            )
        if self.plan:
            sections.append(
                "## Plan\n"
                + "\n".join(
                    f"- [{item['status']}] {item['id']}: {item['content']}"
                    for item in self.plan
                )
            )
        if self.decisions:
            sections.append(
                "## Decisions\n"
                + "\n".join(
                    f"- {item['content']}"
                    + (f" — {item['reason']}" if item.get("reason") else "")
                    for item in self.decisions
                )
            )
        if self.files_modified:
            sections.append(
                "## Files modified\n" + "\n".join(f"- {path}" for path in self.files_modified)
            )
        return "# Structured task memory\n\n" + "\n\n".join(sections) if sections else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "constraints": copy.deepcopy(self.constraints),
            "plan": copy.deepcopy(self.plan),
            "decisions": copy.deepcopy(self.decisions),
            "files_modified": list(self.files_modified),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TaskMemory:
        if not isinstance(data, dict):
            return cls()
        state = cls(goal=_clean(data.get("goal")))
        for raw in list(data.get("constraints") or [])[:_MAX_ITEMS]:
            if isinstance(raw, dict) and _clean(raw.get("content")):
                state.constraints.append(
                    {
                        "id": _clean(raw.get("id"), 80)
                        or _next_id("constraint", state.constraints),
                        "content": _clean(raw.get("content")),
                        "source": _clean(raw.get("source"), 40) or "user",
                        "status": "revoked" if raw.get("status") == "revoked" else "active",
                    }
                )
        for raw in list(data.get("plan") or [])[:_MAX_ITEMS]:
            if isinstance(raw, dict) and _clean(raw.get("content")):
                status = str(raw.get("status") or "pending")
                state.plan.append(
                    {
                        "id": _clean(raw.get("id"), 80) or _next_id("step", state.plan),
                        "content": _clean(raw.get("content")),
                        "status": status if status in _PLAN_STATUSES else "pending",
                    }
                )
        for raw in list(data.get("decisions") or [])[:_MAX_ITEMS]:
            if isinstance(raw, dict) and _clean(raw.get("content")):
                state.decisions.append(
                    {
                        "id": _clean(raw.get("id"), 80)
                        or _next_id("decision", state.decisions),
                        "content": _clean(raw.get("content")),
                        "reason": _clean(raw.get("reason")),
                    }
                )
        for path in list(data.get("files_modified") or [])[:200]:
            state.record_file(str(path))
        if state._size() > _MAX_CHARS:
            raise ValueError("structured task memory exceeds its size limit")
        return state

    def _append(
        self, target: list[dict[str, str]], prefix: str, fields: dict[str, str]
    ) -> dict[str, str]:
        if len(target) >= _MAX_ITEMS:
            raise ValueError(f"{prefix} item limit reached")
        content = _clean(fields.get("content"))
        if not content:
            raise ValueError(f"{prefix} content must not be empty")
        item = {"id": _next_id(prefix, target), **fields, "content": content}
        item = {key: _clean(value) for key, value in item.items()}
        target.append(item)
        if self._size() > _MAX_CHARS:
            target.pop()
            raise ValueError("structured task memory exceeds its size limit")
        return item

    def _size(self) -> int:
        return len(json.dumps(self.to_dict(), ensure_ascii=False))

    def _check_size(self, *, previous_goal: str) -> None:
        if self._size() > _MAX_CHARS:
            self.goal = previous_goal
            raise ValueError("structured task memory exceeds its size limit")


class MemoryUpdateTool(Tool):
    name = "memory_update"
    description = "Update durable task goal, constraints, plan steps, or decisions."
    effect = ToolEffect.STATE
    capabilities = frozenset()
    parallel_safe = False
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "set_goal",
                    "add_constraint",
                    "add_plan_step",
                    "update_plan_step",
                    "add_decision",
                    "clear_plan",
                ],
            },
            "content": {"type": "string"},
            "item_id": {"type": "string"},
            "status": {"type": "string", "enum": sorted(_PLAN_STATUSES)},
            "reason": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, memory: TaskMemory):
        self.memory = memory

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("task-memory", Access.WRITE),)

    def execute(
        self,
        action: str,
        content: str = "",
        item_id: str = "",
        status: str = "",
        reason: str = "",
    ) -> str:
        if action == "set_goal":
            result: Any = self.memory.set_goal(content)
        elif action == "add_constraint":
            result = self.memory.add_constraint(content, source="agent")
        elif action == "add_plan_step":
            result = self.memory.add_plan_step(content)
        elif action == "update_plan_step":
            result = self.memory.update_plan_step(item_id, status)
        elif action == "add_decision":
            result = self.memory.add_decision(content, reason=reason)
        elif action == "clear_plan":
            self.memory.clear_plan()
            result = {"cleared": True}
        else:
            raise ValueError(f"unknown memory action: {action}")
        return json.dumps(result, ensure_ascii=False, indent=2)

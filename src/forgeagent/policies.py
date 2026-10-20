"""Composable Tool admission policies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .contracts import ToolCall
from .tools import Tool, ToolEffect


class RunMode(str, Enum):
    NORMAL = "normal"
    PLANNING = "planning"


@dataclass(frozen=True)
class PolicyContext:
    mode: RunMode = RunMode.NORMAL


@dataclass(frozen=True)
class Decision:
    allowed: bool
    policy: str
    code: str
    reason: str = ""

    @classmethod
    def allow(cls, policy: str, code: str = "allow") -> Decision:
        return cls(True, policy, code)

    @classmethod
    def deny(cls, policy: str, code: str, reason: str) -> Decision:
        return cls(False, policy, code, reason)


class ToolPolicy(Protocol):
    name: str

    def evaluate(self, call: ToolCall, tool: Tool, context: PolicyContext) -> Decision:
        ...


class PlanningPolicy:
    name = "planning"
    _SAFE_EFFECTS = frozenset({ToolEffect.PURE, ToolEffect.READ})

    def evaluate(self, call: ToolCall, tool: Tool, context: PolicyContext) -> Decision:
        if context.mode is not RunMode.PLANNING:
            return Decision.allow(self.name, "not_planning")
        if tool.effect in self._SAFE_EFFECTS:
            return Decision.allow(self.name, "planning_safe")
        return Decision.deny(
            self.name,
            "planning_side_effect",
            f"Plan Mode rejected {call.name}: effect={tool.effect.value}. "
            "Continue with read-only investigation, present a plan, and wait for approval.",
        )


class CapabilityPolicy:
    name = "capability"

    def __init__(self, allowed: set[str] | frozenset[str] | None = None):
        self.allowed = None if allowed is None else frozenset(allowed)

    def evaluate(self, call: ToolCall, tool: Tool, context: PolicyContext) -> Decision:
        if self.allowed is None:
            return Decision.allow(self.name, "unrestricted")
        missing = sorted(tool.capabilities - self.allowed)
        if not missing:
            return Decision.allow(self.name, "capabilities_allowed")
        return Decision.deny(
            self.name,
            "capability_missing",
            f"Capability policy rejected {call.name}; missing: {', '.join(missing)}",
        )


AskCallback = Callable[[str, dict], str]


class PermissionPolicy:
    name = "permission"
    _NO_EXTERNAL_MUTATION = frozenset({ToolEffect.PURE, ToolEffect.READ})

    def __init__(self, ask: AskCallback | None = None, *, allow_all: bool = False):
        self.ask = ask
        self.allow_all = allow_all
        self._always: set[str] = set()
        self._lock = threading.Lock()

    def evaluate(self, call: ToolCall, tool: Tool, context: PolicyContext) -> Decision:
        if tool.effect in self._NO_EXTERNAL_MUTATION:
            return Decision.allow(self.name, "read_only")
        with self._lock:
            if self.allow_all:
                return Decision.allow(self.name, "allow_all")
            if call.name in self._always:
                return Decision.allow(self.name, "always_cached")
        if self.ask is None:
            return Decision.deny(
                self.name,
                "non_interactive",
                f"Permission denied for {call.name}: no interactive approver is available.",
            )
        answer = self.ask(call.name, call.arguments).strip().lower()
        if answer == "always":
            with self._lock:
                self._always.add(call.name)
            return Decision.allow(self.name, "always")
        if answer == "once":
            return Decision.allow(self.name, "once")
        return Decision.deny(
            self.name,
            "user_denied",
            f"Permission denied for {call.name}: the user rejected this call.",
        )


class PolicyPipeline:
    def __init__(self, policies: list[ToolPolicy] | tuple[ToolPolicy, ...]):
        self.policies = tuple(policies)

    def evaluate(self, call: ToolCall, tool: Tool, context: PolicyContext) -> Decision:
        last = Decision.allow("pipeline", "empty")
        for policy in self.policies:
            last = policy.evaluate(call, tool, context)
            if not last.allowed:
                return last
        return last


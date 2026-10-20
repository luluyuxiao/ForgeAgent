"""Tool contracts and resource declarations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

_MAX_RESULT_CHARS = 24_000


class ToolEffect(str, Enum):
    PURE = "pure"
    READ = "read"
    STATE = "state"
    WRITE = "write"
    PROCESS = "process"
    NETWORK = "network"
    UNKNOWN = "unknown"


class Access(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ResourceClaim:
    resource: str
    access: Access

    def conflicts_with(self, other: ResourceClaim) -> bool:
        same = (
            self.resource == other.resource
            or self.resource == "*"
            or other.resource == "*"
        )
        return same and (self.access is Access.WRITE or other.access is Access.WRITE)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    effect: ClassVar[ToolEffect] = ToolEffect.UNKNOWN
    capabilities: ClassVar[frozenset[str]] = frozenset({"unknown"})
    parallel_safe: ClassVar[bool] = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        if self.effect is ToolEffect.PURE:
            return ()
        access = Access.READ if self.effect is ToolEffect.READ else Access.WRITE
        return (ResourceClaim("*", access),)

    def checkpoint_paths(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        """Workspace-relative files that can be restored for this call."""
        return ()

    @abstractmethod
    def execute(self, **arguments: Any) -> str:
        raise NotImplementedError


def bound_output(value: str) -> str:
    """Keep local Tool observations from dominating the model context."""
    if len(value) <= _MAX_RESULT_CHARS:
        return value
    omitted = len(value) - _MAX_RESULT_CHARS
    return value[:_MAX_RESULT_CHARS] + f"\n[truncated {omitted} characters]"

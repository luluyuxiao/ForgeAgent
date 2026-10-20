"""ForgeAgent public library API."""

from .config import Settings
from .context import ContextLimitError, ContextWindow
from .contracts import ChatProvider, ModelReply, ToolCall, Usage
from .policies import (
    CapabilityPolicy,
    PermissionPolicy,
    PlanningPolicy,
    PolicyPipeline,
    RunMode,
)
from .registry import ToolRegistry
from .runtime import AgentRuntime
from .session import SessionState, SessionStore
from .tools import Tool, ToolEffect, build_builtin_tools

__version__ = "0.1.0"

__all__ = [
    "AgentRuntime",
    "CapabilityPolicy",
    "ChatProvider",
    "ContextLimitError",
    "ContextWindow",
    "ModelReply",
    "PermissionPolicy",
    "PlanningPolicy",
    "PolicyPipeline",
    "RunMode",
    "SessionState",
    "SessionStore",
    "Settings",
    "Tool",
    "ToolCall",
    "ToolEffect",
    "ToolRegistry",
    "Usage",
    "build_builtin_tools",
]


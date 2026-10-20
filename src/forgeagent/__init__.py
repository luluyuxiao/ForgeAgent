"""ForgeAgent public library API."""

from .catalog import ToolCatalog
from .checkpoints import CheckpointManager
from .config import ProviderRoute, Settings
from .context import ContextLimitError, ContextWindow
from .contracts import ChatProvider, ModelReply, ToolCall, Usage
from .instructions import load_repository_instructions
from .mcp import McpClient, McpTool
from .policies import (
    CapabilityPolicy,
    PermissionPolicy,
    PlanningPolicy,
    PolicyPipeline,
    RunMode,
)
from .providers import FallbackProvider, OpenAICompatibleProvider, PartialResponseError
from .registry import ToolRegistry
from .runtime import AgentRuntime
from .sandbox import DockerExecutor, LocalExecutor
from .session import SessionState, SessionStore
from .tools import Tool, ToolEffect, build_builtin_tools
from .trace import JsonlTrace
from .workers import SubAgentManager

__version__ = "0.2.0"

__all__ = [
    "AgentRuntime",
    "CapabilityPolicy",
    "ChatProvider",
    "CheckpointManager",
    "ContextLimitError",
    "ContextWindow",
    "DockerExecutor",
    "FallbackProvider",
    "JsonlTrace",
    "LocalExecutor",
    "McpClient",
    "McpTool",
    "ModelReply",
    "OpenAICompatibleProvider",
    "PartialResponseError",
    "PermissionPolicy",
    "PlanningPolicy",
    "PolicyPipeline",
    "ProviderRoute",
    "RunMode",
    "SessionState",
    "SessionStore",
    "Settings",
    "SubAgentManager",
    "Tool",
    "ToolCall",
    "ToolCatalog",
    "ToolEffect",
    "ToolRegistry",
    "Usage",
    "build_builtin_tools",
    "load_repository_instructions",
]

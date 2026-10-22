"""ForgeAgent public library API."""

from .checkpoints import CheckpointError, CheckpointManager
from .config import BudgetExceededError, CostBudget, ProviderRoute, Settings
from .context import ContextLimitError, ContextWindow
from .contracts import ChatProvider, ModelReply, ToolCall, Usage
from .guidance import SkillRegistry, load_repository_instructions
from .mcp import McpClient, McpTool
from .memory import TaskMemory
from .policies import (
    CapabilityPolicy,
    PermissionPolicy,
    PlanningPolicy,
    PolicyPipeline,
    RunMode,
)
from .providers import (
    FallbackProvider,
    OpenAICompatibleEmbedder,
    OpenAICompatibleProvider,
    PartialResponseError,
)
from .runtime import AgentRuntime
from .sandbox import DockerExecutor, LocalExecutor
from .session import SessionState, SessionStore
from .tools import (
    ApplySandboxChangesTool,
    EmbeddingBackend,
    ReadTranscriptRangeTool,
    ReviewSandboxChangesTool,
    SearchTranscriptTool,
    Tool,
    ToolCatalog,
    ToolEffect,
    ToolRegistry,
    build_builtin_tools,
)
from .trace import JsonlTrace
from .workers import SubAgentManager

__version__ = "0.3.0"

__all__ = [
    "AgentRuntime",
    "ApplySandboxChangesTool",
    "BudgetExceededError",
    "CapabilityPolicy",
    "ChatProvider",
    "CheckpointError",
    "CheckpointManager",
    "ContextLimitError",
    "ContextWindow",
    "CostBudget",
    "DockerExecutor",
    "EmbeddingBackend",
    "FallbackProvider",
    "JsonlTrace",
    "LocalExecutor",
    "McpClient",
    "McpTool",
    "ModelReply",
    "OpenAICompatibleEmbedder",
    "OpenAICompatibleProvider",
    "PartialResponseError",
    "PermissionPolicy",
    "PlanningPolicy",
    "PolicyPipeline",
    "ProviderRoute",
    "ReadTranscriptRangeTool",
    "ReviewSandboxChangesTool",
    "RunMode",
    "SearchTranscriptTool",
    "SessionState",
    "SessionStore",
    "Settings",
    "SkillRegistry",
    "SubAgentManager",
    "TaskMemory",
    "Tool",
    "ToolCall",
    "ToolCatalog",
    "ToolEffect",
    "ToolRegistry",
    "Usage",
    "build_builtin_tools",
    "load_repository_instructions",
]

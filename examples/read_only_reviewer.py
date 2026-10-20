"""Construct an Agent whose registry contains no mutating capability."""

from pathlib import Path

from forgeagent import (
    AgentRuntime,
    ContextWindow,
    PermissionPolicy,
    PolicyPipeline,
    Settings,
    ToolEffect,
    ToolRegistry,
    build_builtin_tools,
)
from forgeagent.providers import OpenAICompatibleProvider

settings = Settings.from_env()
workspace = Path.cwd()
review_tools = [
    tool
    for tool in build_builtin_tools(workspace)
    if tool.effect in {ToolEffect.PURE, ToolEffect.READ}
]
registry = ToolRegistry(review_tools)
provider = OpenAICompatibleProvider(
    model=settings.model,
    api_key=settings.api_key,
    base_url=settings.base_url,
    max_output_tokens=settings.max_output_tokens,
)
runtime = AgentRuntime(
    provider=provider,
    registry=registry,
    policies=PolicyPipeline([PermissionPolicy()]),
    context=ContextWindow(
        settings.context_tokens,
        output_reserve=settings.max_output_tokens,
    ),
    workspace=workspace,
)

print(runtime.run("Review this repository without changing files."))


"""Composition root shared by the CLI and Eval runner."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .checkpoints import CheckpointManager
from .config import CostBudget, Settings
from .context import ContextWindow, ProviderSummarizer
from .guidance import LoadSkillTool, SkillRegistry, load_repository_instructions
from .hooks import ToolHooks
from .mcp import McpConnectionSet, McpTool, load_mcp_tools
from .memory import MemoryUpdateTool, TaskMemory
from .policies import CapabilityPolicy, PermissionPolicy, PlanningPolicy, PolicyPipeline
from .providers import (
    FallbackProvider,
    OpenAICompatibleEmbedder,
    OpenAICompatibleProvider,
)
from .runtime import AgentRuntime
from .sandbox import DockerExecutor, LocalExecutor
from .tools import ToolCatalog, ToolRegistry, build_builtin_tools, install_catalog_tools
from .trace import JsonlTrace, NullTrace, TraceSink
from .workers import (
    ChildRunResult,
    SubAgentManager,
    SubAgentTool,
    TaskStatusTool,
    subagent_tool_allowlist,
)


@dataclass
class RuntimeOptions:
    workspace: Path
    interactive: bool = False
    allow_all: bool = False
    ask_permission: Callable[[str, dict], str] | None = None
    enable_subagents: bool = True
    enable_mcp: bool = True
    trace_content: bool = False
    allowed_capabilities: frozenset[str] | None = None
    allowed_tools: frozenset[str] | None = None


@dataclass
class RuntimeBundle:
    runtime: AgentRuntime
    mcp: McpConnectionSet | None = None
    workers: SubAgentManager | None = None

    def close(self) -> None:
        if self.workers is not None:
            self.workers.close()
        if self.mcp is not None:
            self.mcp.close()


# 传入参数
def create_runtime(settings: Settings, options: RuntimeOptions) -> RuntimeBundle:
    workspace = options.workspace.expanduser().resolve()
    executor = _executor(settings)
    provider = create_provider(settings)
    provider_lock = threading.RLock()
    trace: TraceSink = (
        JsonlTrace(settings.trace_path, include_content=options.trace_content)
        if settings.trace_path
        else NullTrace()
    )
    memory = TaskMemory()
    skills = SkillRegistry.discover(workspace)
    builtin_tools = build_builtin_tools(workspace, executor=executor)
    registry = ToolRegistry(
        tool for tool in builtin_tools if _tool_allowed(tool.name, options.allowed_tools)
    )
    if _tool_allowed("memory_update", options.allowed_tools):
        registry.register(MemoryUpdateTool(memory))
    if skills and _tool_allowed("load_skill", options.allowed_tools):
        registry.register(LoadSkillTool(skills))
    core_names = set(registry.names())
    capability = CapabilityPolicy(options.allowed_capabilities)
    mcp_connections = None
    if options.enable_mcp and settings.mcp_config:
        mcp_tools, mcp_connections = load_mcp_tools(
            settings.mcp_config,
            allowed_capabilities=options.allowed_capabilities,
        )
        for tool in mcp_tools:
            if _tool_allowed(tool.name, options.allowed_tools):
                registry.register(tool)

    permission = PermissionPolicy(
        options.ask_permission if options.interactive else None,
        allow_all=options.allow_all,
    )
    policies = PolicyPipeline([capability, PlanningPolicy(), permission])

    workers = None
    if options.enable_subagents and _tool_allowed("subagent", options.allowed_tools):

        def run_child(task: str, child_workspace: Path, profile: str) -> ChildRunResult:
            child_options = RuntimeOptions(
                workspace=child_workspace,
                interactive=False,
                allow_all=options.allow_all,
                enable_subagents=False,
                enable_mcp=options.enable_mcp and profile == "full",
                allowed_capabilities=options.allowed_capabilities,
                allowed_tools=subagent_tool_allowlist(profile, options.allowed_tools),
            )
            child = create_runtime(settings, child_options)
            try:
                content = child.runtime.run(task)
                usage = child.runtime.provider.total_usage
                return ChildRunResult(
                    content,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )
            finally:
                child.close()

        workers = SubAgentManager(run_child, workspace=workspace)
        registry.register(SubAgentTool(workers))
        core_names.add("subagent")
        if _tool_allowed("task_status", options.allowed_tools):
            registry.register(TaskStatusTool(workers))
            core_names.add("task_status")

    embedder = None
    if settings.tool_embedding_model:
        embedder = OpenAICompatibleEmbedder(
            model=settings.tool_embedding_model,
            api_key=settings.tool_embedding_api_key or settings.api_key,
            base_url=settings.tool_embedding_base_url or settings.base_url,
        )
    catalog = ToolCatalog(
        registry,
        active=core_names,
        embedder=embedder,
        allowed_capabilities=options.allowed_capabilities,
        interactive=options.interactive,
        allow_all=options.allow_all,
    )
    install_catalog_tools(catalog)

    def refresh_mcp() -> str:
        if mcp_connections is None:
            return "MCP is not configured."
        refreshed, health = mcp_connections.refresh()
        successful = {
            str(item.get("name")) for item in health if item.get("status") == "healthy"
        }
        previously_active = {
            name for name in catalog.names() if isinstance(registry.get(name), McpTool)
        }
        remove = [
            name
            for name in registry.names()
            if isinstance(registry.get(name), McpTool)
            and registry.require(name).client.name in successful
        ]
        catalog.hide(remove)
        for name in remove:
            registry.unregister(name)
        for tool in refreshed:
            existing = registry.get(tool.name)
            if existing is not None and not isinstance(existing, McpTool):
                health.append(
                    {
                        "name": tool.client.name,
                        "status": "name_conflict",
                        "error": f"Tool name already exists: {tool.name}",
                    }
                )
                continue
            registry.replace(tool)
        catalog.activate(previously_active & set(registry.names()))
        return json.dumps(
            {"servers": health, "tool_count": len(refreshed)},
            ensure_ascii=False,
            indent=2,
        )
    summarizer = ProviderSummarizer(provider, provider_lock) if settings.llm_summary else None
    context = ContextWindow(
        settings.context_tokens,
        output_reserve=settings.max_output_tokens,
        max_input_ratio=settings.context_input_ratio,
        summarizer=summarizer,
    )
    hooks_path = settings.hooks_config or workspace / ".forgeagent" / "hooks.json"
    hooks = ToolHooks.load(hooks_path, executor=executor, workspace=workspace)
    runtime = AgentRuntime(
        provider=provider,
        registry=registry,
        policies=policies,
        context=context,
        max_rounds=settings.max_rounds,
        workspace=workspace,
        catalog=catalog,
        checkpoints=CheckpointManager(workspace),
        trace=trace,
        provider_lock=provider_lock,
        repository_instructions=load_repository_instructions(workspace),
        memory=memory,
        skills=skills,
        hooks=hooks,
        budget=CostBudget(
            settings.max_cost_usd,
            settings.input_price_per_million,
            settings.output_price_per_million,
        ),
        refresh_mcp=refresh_mcp if mcp_connections is not None else None,
    )
    return RuntimeBundle(runtime, mcp_connections, workers)


def create_provider(settings: Settings):
    primary = OpenAICompatibleProvider(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        max_output_tokens=settings.max_output_tokens,
    )
    providers = [primary]
    for route in settings.fallback_routes:
        key = (
            settings.api_key
            if route.api_key_env == "FORGE_API_KEY"
            else os.getenv(route.api_key_env, "")
        )
        if not key:
            raise ValueError(f"fallback key environment variable is empty: {route.api_key_env}")
        providers.append(
            OpenAICompatibleProvider(
                model=route.model,
                api_key=key,
                base_url=route.base_url or settings.base_url,
                max_output_tokens=settings.max_output_tokens,
            )
        )
    return FallbackProvider(providers) if len(providers) > 1 else primary


def _executor(settings: Settings):
    if settings.sandbox == "local":
        return LocalExecutor()
    if settings.sandbox == "docker":
        return DockerExecutor(
            settings.sandbox_image,
            network=settings.sandbox_network,
        )
    raise ValueError("FORGE_SANDBOX must be 'local' or 'docker'")


def _tool_allowed(name: str, allowed: frozenset[str] | None) -> bool:
    return allowed is None or name in allowed

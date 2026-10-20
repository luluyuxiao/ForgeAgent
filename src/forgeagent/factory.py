"""Composition root shared by the CLI and Eval runner."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .catalog import ToolCatalog, install_catalog_tools
from .checkpoints import CheckpointManager
from .config import Settings
from .context import ContextWindow
from .instructions import load_repository_instructions
from .mcp import McpConnectionSet, load_mcp_tools
from .policies import CapabilityPolicy, PermissionPolicy, PlanningPolicy, PolicyPipeline
from .providers import FallbackProvider, OpenAICompatibleProvider
from .registry import ToolRegistry
from .runtime import AgentRuntime
from .sandbox import DockerExecutor, LocalExecutor
from .summarize import ProviderSummarizer
from .tools import build_builtin_tools
from .trace import JsonlTrace, NullTrace, TraceSink
from .workers import ChildRunResult, SubAgentManager, SubAgentTool, TaskStatusTool


@dataclass
class RuntimeOptions:
    workspace: Path
    interactive: bool = False
    allow_all: bool = False
    ask_permission: Callable[[str, dict], str] | None = None
    enable_subagents: bool = True
    enable_mcp: bool = True
    trace_content: bool = False


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
    registry = ToolRegistry(build_builtin_tools(workspace, executor=executor))
    core_names = set(registry.names())
    mcp_connections = None
    if options.enable_mcp and settings.mcp_config:
        mcp_tools, mcp_connections = load_mcp_tools(settings.mcp_config)
        for tool in mcp_tools:
            registry.register(tool)

    permission = PermissionPolicy(
        options.ask_permission if options.interactive else None,
        allow_all=options.allow_all,
    )
    policies = PolicyPipeline([CapabilityPolicy(), PlanningPolicy(), permission])

    workers = None
    if options.enable_subagents:

        def run_child(task: str) -> ChildRunResult:
            child_options = RuntimeOptions(
                workspace=workspace,
                interactive=False,
                allow_all=options.allow_all,
                enable_subagents=False,
                enable_mcp=options.enable_mcp,
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

        workers = SubAgentManager(run_child)
        registry.register(SubAgentTool(workers))
        registry.register(TaskStatusTool(workers))
        core_names.update({"subagent", "task_status"})

    catalog = ToolCatalog(registry, active=core_names)
    install_catalog_tools(catalog)
    summarizer = ProviderSummarizer(provider, provider_lock) if settings.llm_summary else None
    context = ContextWindow(
        settings.context_tokens,
        output_reserve=settings.max_output_tokens,
        summarizer=summarizer,
    )
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

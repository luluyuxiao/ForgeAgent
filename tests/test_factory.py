from __future__ import annotations

import json

from forgeagent.config import Settings
from forgeagent.factory import RuntimeOptions, create_runtime
from forgeagent.sandbox import DockerExecutor
from forgeagent.session import SessionState
from forgeagent.workers import subagent_tool_allowlist


def test_factory_composes_docker_catalog_checkpoint_and_trace(tmp_path):
    settings = Settings(
        api_key="test-key",
        model="fake-model",
        base_url="https://example.test/v1",
        context_tokens=4000,
        max_output_tokens=200,
        sandbox="docker",
        trace_path=tmp_path / "trace.jsonl",
        llm_summary=False,
    )

    bundle = create_runtime(
        settings,
        RuntimeOptions(workspace=tmp_path, enable_subagents=False),
    )
    try:
        shell = bundle.runtime.registry.require("shell")
        assert isinstance(shell.executor, DockerExecutor)
        assert shell.executor.workspace_mode == "isolated"
        assert bundle.runtime.catalog is not None
        assert bundle.runtime.checkpoints is not None
        assert bundle.runtime.registry.get("memory_update") is not None
        assert bundle.runtime.registry.get("search_transcript") is not None
        assert bundle.runtime.registry.get("read_transcript_range") is not None
        assert bundle.runtime.hooks is not None
        assert bundle.runtime.budget.max_usd == 0
    finally:
        bundle.close()


def test_transcript_search_reads_the_restored_runtime_history(tmp_path):
    settings = Settings(
        api_key="test-key",
        model="fake-model",
        base_url="https://example.test/v1",
        llm_summary=False,
    )
    bundle = create_runtime(
        settings,
        RuntimeOptions(workspace=tmp_path, enable_subagents=False, enable_mcp=False),
    )
    try:
        bundle.runtime.restore(
            SessionState(
                session_id="remembered",
                model="fake-model",
                transcript=[
                    {"role": "user", "content": "The restored build tag is amber-42."}
                ],
                active_messages=[],
            )
        )

        result = json.loads(
            bundle.runtime.registry.require("search_transcript").execute(
                query="restored build tag"
            )
        )

        assert result["results"][0]["index"] == 0
        assert "amber-42" in result["results"][0]["snippet"]
    finally:
        bundle.close()


def test_factory_rejects_mcp_server_before_network_when_capability_is_missing(tmp_path):
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {"servers": {"remote": {"transport": "http", "url": "https://invalid.test"}}}
        ),
        encoding="utf-8",
    )
    settings = Settings(
        api_key="test-key",
        model="fake-model",
        base_url="https://example.test/v1",
        mcp_config=mcp_config,
        llm_summary=False,
    )

    bundle = create_runtime(
        settings,
        RuntimeOptions(
            workspace=tmp_path,
            enable_subagents=False,
            allowed_capabilities=frozenset({"mcp"}),
        ),
    )
    try:
        assert bundle.mcp is not None
        assert bundle.mcp.clients == []
        assert "network" in bundle.mcp.errors[0]["error"]
    finally:
        bundle.close()


def test_factory_filters_child_runtime_tools_by_profile(tmp_path):
    settings = Settings(
        api_key="test-key",
        model="fake-model",
        base_url="https://example.test/v1",
        llm_summary=False,
    )

    research = create_runtime(
        settings,
        RuntimeOptions(
            workspace=tmp_path,
            enable_subagents=False,
            enable_mcp=False,
            allowed_tools=subagent_tool_allowlist("research"),
        ),
    )
    implement = create_runtime(
        settings,
        RuntimeOptions(
            workspace=tmp_path,
            enable_subagents=False,
            enable_mcp=False,
            allowed_tools=subagent_tool_allowlist("implement"),
        ),
    )
    try:
        assert research.runtime.registry.get("read_file") is not None
        assert research.runtime.registry.get("search_transcript") is not None
        assert research.runtime.registry.get("read_transcript_range") is not None
        assert research.runtime.registry.get("write_file") is None
        assert research.runtime.registry.get("shell") is None
        assert implement.runtime.registry.get("write_file") is not None
        assert implement.runtime.registry.get("run_checks") is not None
        assert implement.runtime.registry.get("shell") is None
        assert implement.runtime.registry.get("subagent") is None
    finally:
        research.close()
        implement.close()

from __future__ import annotations

import json

from forgeagent.config import Settings
from forgeagent.factory import RuntimeOptions, create_runtime
from forgeagent.sandbox import DockerExecutor


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
        assert bundle.runtime.catalog is not None
        assert bundle.runtime.checkpoints is not None
        assert bundle.runtime.registry.get("memory_update") is not None
        assert bundle.runtime.hooks is not None
        assert bundle.runtime.budget.max_usd == 0
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

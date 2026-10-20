from __future__ import annotations

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
    finally:
        bundle.close()

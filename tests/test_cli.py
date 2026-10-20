from __future__ import annotations

import json

from forgeagent.cli import _run_json
from forgeagent.context import ContextWindow
from forgeagent.contracts import ModelReply, Usage
from forgeagent.policies import PermissionPolicy, PolicyPipeline
from forgeagent.runtime import AgentRuntime
from forgeagent.tools import ToolRegistry, build_builtin_tools
from tests.helpers import ScriptedProvider


def test_json_mode_emits_lifecycle_and_final_record(tmp_path, capsys):
    provider = ScriptedProvider([ModelReply(content="done", usage=Usage(7, 2))])
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(build_builtin_tools(tmp_path)),
        policies=PolicyPipeline([PermissionPolicy(allow_all=True)]),
        context=ContextWindow(4000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
    )

    assert _run_json(runtime, "hello") == "done"
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rows[0]["event"] == "run_started"
    assert rows[-1] == {
        "event": "final",
        "outcome": "completed",
        "content": "done",
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "estimated_cost_usd": 0.0,
        "max_cost_usd": 0.0,
    }

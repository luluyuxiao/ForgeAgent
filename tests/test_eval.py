from __future__ import annotations

import shlex
import sys

from forgeagent.context import ContextWindow
from forgeagent.contracts import ModelReply, Usage
from forgeagent.eval import EvalCase, evaluate_case
from forgeagent.policies import PermissionPolicy, PolicyPipeline
from forgeagent.registry import ToolRegistry
from forgeagent.runtime import AgentRuntime
from forgeagent.tools import build_builtin_tools
from tests.helpers import ScriptedProvider


def test_eval_checks_output_tool_and_files(tmp_path):
    (tmp_path / "proof.txt").write_text("ready", encoding="utf-8")
    provider = ScriptedProvider([ModelReply(content="all ready")])
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(build_builtin_tools(tmp_path)),
        policies=PolicyPipeline([PermissionPolicy(allow_all=True)]),
        context=ContextWindow(4000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
    )
    case = EvalCase(
        "smoke",
        "check",
        output_contains=("ready",),
        files_contain={"proof.txt": "ready"},
    )

    result = evaluate_case(case, runtime, tmp_path)

    assert result.passed


def test_eval_runs_verification_and_records_usage(tmp_path):
    provider = ScriptedProvider([ModelReply(content="done", usage=Usage(12, 3))])
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(build_builtin_tools(tmp_path)),
        policies=PolicyPipeline([PermissionPolicy(allow_all=True)]),
        context=ContextWindow(4000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
    )
    command = (
        f"{shlex.quote(sys.executable)} -c "
        '"from pathlib import Path; Path(\'verified.txt\').write_text(\'ok\')"'
    )
    case = EvalCase(
        "verified",
        "work",
        output_not_contains=("error",),
        max_total_tokens=20,
        max_tool_calls=0,
        test_command=command,
        input_cost_per_million=2.0,
        output_cost_per_million=8.0,
    )

    result = evaluate_case(case, runtime, tmp_path)

    assert result.passed
    assert result.test_exit_code == 0
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 3
    assert result.estimated_cost_usd == 0.000048
    assert (tmp_path / "verified.txt").read_text(encoding="utf-8") == "ok"

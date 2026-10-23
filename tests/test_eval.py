from __future__ import annotations

import shlex
import sys

from forgeagent.context import ContextWindow
from forgeagent.contracts import ModelReply, ToolCall, Usage
from forgeagent.eval import EvalCase, EvalResult, build_report, evaluate_case, load_manifest
from forgeagent.policies import PermissionPolicy, PolicyPipeline
from forgeagent.runtime import AgentRuntime
from forgeagent.tools import ToolRegistry, build_builtin_tools
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
    assert result.verification_pass_rate == 1.0


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


def test_eval_quantifies_tool_selection_and_policy_denials(tmp_path):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[ToolCall("clock", "now", {})]),
            ModelReply(content="done"),
        ]
    )
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(build_builtin_tools(tmp_path)),
        policies=PolicyPipeline([PermissionPolicy(allow_all=False)]),
        context=ContextWindow(4000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
    )
    case = EvalCase(
        "tool-choice",
        "check the time",
        category="tool-use",
        expected_tool_groups=(("now", "shell"),),
        allowed_tools=("now",),
        forbidden_tools=("write_file",),
    )

    result = evaluate_case(case, runtime, tmp_path, run_index=2)

    assert result.passed
    assert result.category == "tool-use"
    assert result.run_index == 2
    assert result.tool_recall == 1.0
    assert result.tool_precision == 1.0
    assert result.unexpected_tools == []
    assert result.policy_denied_calls == 0


def test_eval_records_blocked_calls_as_safety_metrics(tmp_path):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall("write", "write_file", {"path": "blocked.txt", "content": "x"})
                ]
            ),
            ModelReply(content="write was blocked"),
        ]
    )
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(build_builtin_tools(tmp_path)),
        policies=PolicyPipeline([PermissionPolicy(allow_all=False)]),
        context=ContextWindow(4000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
    )
    case = EvalCase(
        "safe-denial",
        "try a write",
        category="safety",
        forbidden_tools=("write_file",),
    )

    result = evaluate_case(case, runtime, tmp_path)

    assert result.passed
    assert result.tools == []
    assert result.policy_denied_calls == 1
    assert result.forbidden_tool_executions == []
    assert not (tmp_path / "blocked.txt").exists()


def test_eval_report_aggregates_categories_repeats_and_quality_metrics():
    results = [
        EvalResult(
            "fix-bug",
            True,
            "done",
            ["read_file", "apply_patch"],
            2.0,
            [],
            category="bug-fix",
            run_index=1,
            expected_tool_targets=2,
            matched_tool_targets=2,
            relevant_tool_calls=2,
            labeled_tool_calls=2,
            verification_checks=3,
            passed_verification_checks=3,
            test_passed=True,
        ),
        EvalResult(
            "fix-bug",
            False,
            "failed",
            ["read_file"],
            4.0,
            ["test failed"],
            category="bug-fix",
            run_index=2,
            expected_tool_targets=2,
            matched_tool_targets=1,
            relevant_tool_calls=1,
            labeled_tool_calls=1,
            verification_checks=3,
            passed_verification_checks=2,
            test_passed=False,
        ),
    ]

    report = build_report(results)

    assert report["task_success_rate"] == 0.5
    assert report["case_pass_at_least_once_rate"] == 1.0
    assert report["stable_case_rate"] == 0.0
    assert report["metrics"]["tool_recall"] == 0.75
    assert report["metrics"]["test_pass_rate"] == 0.5
    assert report["by_category"]["bug-fix"]["verification_pass_rate"] == 0.833333


def test_eval_report_separates_infrastructure_failures():
    result = EvalResult(
        "offline",
        False,
        "",
        [],
        0.0,
        ["RuntimeError: provider unavailable"],
        outcome="infrastructure_error",
        error_type="RuntimeError",
    )

    report = build_report([result], experiment={"model": "demo"})

    assert report["experiment"] == {"model": "demo"}
    assert report["metrics"]["infrastructure_failures"] == 1
    assert report["metrics"]["completed_runs"] == 0
    assert report["metrics"]["completed_task_success_rate"] is None
    assert report["estimated_cost_usd"] is None


def test_manifest_loads_categories_tags_and_alternative_tool_groups(tmp_path):
    manifest = tmp_path / "eval.json"
    manifest.write_text(
        """{
          "cases": [{
            "name": "demo",
            "prompt": "fix it",
            "category": "bug-fix",
            "tags": ["python"],
            "expected_tool_groups": [["apply_patch", "write_file"]],
            "allowed_tools": ["read_file"]
          }]
        }""",
        encoding="utf-8",
    )

    case = load_manifest(manifest)[0]

    assert case.category == "bug-fix"
    assert case.tags == ("python",)
    assert case.expected_tool_groups == (("apply_patch", "write_file"),)
    assert case.allowed_tools == ("read_file",)

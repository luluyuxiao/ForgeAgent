"""Deterministic task-manifest evaluator for Agent regressions."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .factory import RuntimeOptions, create_runtime
from .runtime import AgentRuntime


@dataclass(frozen=True)
class EvalCase:
    name: str
    prompt: str
    category: str = "uncategorized"
    tags: tuple[str, ...] = ()
    output_contains: tuple[str, ...] = ()
    output_regex: tuple[str, ...] = ()
    output_not_contains: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    expected_tool_groups: tuple[tuple[str, ...], ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    files_contain: dict[str, str] = field(default_factory=dict)
    max_seconds: float | None = None
    max_total_tokens: int | None = None
    max_tool_calls: int | None = None
    test_command: str | None = None
    test_timeout: float = 300.0
    expected_test_exit_code: int = 0
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


@dataclass
class EvalResult:
    name: str
    passed: bool
    output: str
    tools: list[str]
    duration_seconds: float
    failures: list[str]
    outcome: str = "completed"
    error_type: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    test_exit_code: int | None = None
    test_duration_seconds: float = 0.0
    test_stdout: str = ""
    test_stderr: str = ""
    estimated_cost_usd: float | None = None
    category: str = "uncategorized"
    tags: list[str] = field(default_factory=list)
    run_index: int = 1
    tool_decisions: list[dict[str, object]] = field(default_factory=list)
    policy_denied_calls: int = 0
    expected_tool_targets: int = 0
    matched_tool_targets: int = 0
    tool_recall: float | None = None
    relevant_tool_calls: int = 0
    labeled_tool_calls: int = 0
    tool_precision: float | None = None
    unexpected_tools: list[str] = field(default_factory=list)
    forbidden_tool_executions: list[str] = field(default_factory=list)
    verification_checks: int = 0
    passed_verification_checks: int = 0
    verification_pass_rate: float | None = None
    test_passed: bool | None = None


def evaluate_case(
    case: EvalCase,
    runtime: AgentRuntime,
    workspace: Path,
    *,
    run_index: int = 1,
) -> EvalResult:
    tools: list[str] = []
    tool_decisions: list[dict[str, object]] = []

    def observe(event: str, fields: dict) -> None:
        if event == "tool_started":
            tools.append(str(fields["tool"]))
        elif event == "tool_decided":
            tool_decisions.append(
                {
                    "tool": str(fields["tool"]),
                    "allowed": bool(fields["allowed"]),
                    "policy": str(fields.get("policy") or ""),
                    "code": str(fields.get("code") or ""),
                }
            )

    started = time.perf_counter()
    output = runtime.run(case.prompt, on_event=observe)
    duration = time.perf_counter() - started
    failures: list[str] = []
    verification_checks = 0
    passed_verification_checks = 0

    def verify(condition: bool, failure: str) -> None:
        nonlocal verification_checks, passed_verification_checks
        verification_checks += 1
        if condition:
            passed_verification_checks += 1
        else:
            failures.append(failure)

    for expected in case.output_contains:
        verify(expected in output, f"output does not contain {expected!r}")
    for pattern in case.output_regex:
        verify(re.search(pattern, output) is not None, f"output does not match /{pattern}/")
    for unexpected in case.output_not_contains:
        verify(unexpected not in output, f"output unexpectedly contains {unexpected!r}")
    for tool in case.expected_tools:
        verify(tool in tools, f"Tool was not called: {tool}")
    for group in case.expected_tool_groups:
        verify(
            any(tool in tools for tool in group),
            "none of the alternative Tools was called: " + ", ".join(group),
        )
    for tool in case.forbidden_tools:
        verify(tool not in tools, f"forbidden Tool was called: {tool}")
    for relative, expected in case.files_contain.items():
        path = (workspace / relative).resolve()
        exists = path.is_relative_to(workspace.resolve()) and path.is_file()
        if not exists:
            verify(False, f"expected file missing: {relative}")
        else:
            verify(
                expected in path.read_text(encoding="utf-8"),
                f"{relative} does not contain {expected!r}",
            )
    if case.max_seconds is not None and duration > case.max_seconds:
        verify(False, f"duration {duration:.3f}s exceeds {case.max_seconds:.3f}s")
    elif case.max_seconds is not None:
        verify(True, "")
    usage = runtime.provider.total_usage
    if case.max_total_tokens is not None and usage.total_tokens > case.max_total_tokens:
        verify(
            False,
            f"token usage {usage.total_tokens} exceeds {case.max_total_tokens}",
        )
    elif case.max_total_tokens is not None:
        verify(True, "")
    if case.max_tool_calls is not None and len(tools) > case.max_tool_calls:
        verify(False, f"Tool calls {len(tools)} exceeds {case.max_tool_calls}")
    elif case.max_tool_calls is not None:
        verify(True, "")

    test_exit_code = None
    test_duration = 0.0
    test_stdout = ""
    test_stderr = ""
    test_passed: bool | None = None
    if case.test_command:
        test_started = time.perf_counter()
        try:
            shell = runtime.registry.require("shell")
            executor = getattr(shell, "executor", None)
            if executor is None:
                raise RuntimeError("registered shell Tool has no command executor")
            completed = executor.execute(case.test_command, workspace, int(case.test_timeout))
            test_exit_code = completed.returncode
            test_stdout = _bounded_output(completed.stdout)
            test_stderr = _bounded_output(completed.stderr)
            test_passed = test_exit_code == case.expected_test_exit_code
            verify(
                test_passed,
                f"test command exited {test_exit_code}; "
                f"expected {case.expected_test_exit_code}",
            )
        except TimeoutError:
            test_passed = False
            verify(False, f"test command timed out after {case.test_timeout:.1f}s")
        except Exception as error:  # noqa: BLE001 - verification failure is an Eval result
            test_passed = False
            verify(
                False,
                f"test command failed to start: {type(error).__name__}: {error}",
            )
        test_duration = time.perf_counter() - test_started

    estimated_cost = None
    if case.input_cost_per_million is not None or case.output_cost_per_million is not None:
        estimated_cost = (
            usage.prompt_tokens * (case.input_cost_per_million or 0.0)
            + usage.completion_tokens * (case.output_cost_per_million or 0.0)
        ) / 1_000_000

    expected_targets = len(case.expected_tools) + len(case.expected_tool_groups)
    matched_targets = sum(tool in tools for tool in case.expected_tools) + sum(
        any(tool in tools for tool in group) for group in case.expected_tool_groups
    )
    tool_recall = _ratio(matched_targets, expected_targets) if expected_targets else None
    relevant_tools = set(case.expected_tools) | set(case.allowed_tools)
    for group in case.expected_tool_groups:
        relevant_tools.update(group)
    labeled_tool_calls = len(tools) if relevant_tools else 0
    relevant_tool_calls = sum(tool in relevant_tools for tool in tools)
    tool_precision = (
        _ratio(relevant_tool_calls, labeled_tool_calls) if labeled_tool_calls else None
    )
    unexpected_tools = [tool for tool in tools if relevant_tools and tool not in relevant_tools]
    forbidden_tool_executions = [tool for tool in tools if tool in case.forbidden_tools]

    return EvalResult(
        name=case.name,
        passed=not failures,
        output=output,
        tools=tools,
        duration_seconds=duration,
        failures=failures,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        test_exit_code=test_exit_code,
        test_duration_seconds=test_duration,
        test_stdout=test_stdout,
        test_stderr=test_stderr,
        estimated_cost_usd=estimated_cost,
        category=case.category,
        tags=list(case.tags),
        run_index=run_index,
        tool_decisions=tool_decisions,
        policy_denied_calls=sum(not bool(item["allowed"]) for item in tool_decisions),
        expected_tool_targets=expected_targets,
        matched_tool_targets=matched_targets,
        tool_recall=tool_recall,
        relevant_tool_calls=relevant_tool_calls,
        labeled_tool_calls=labeled_tool_calls,
        tool_precision=tool_precision,
        unexpected_tools=unexpected_tools,
        forbidden_tool_executions=forbidden_tool_executions,
        verification_checks=verification_checks,
        passed_verification_checks=passed_verification_checks,
        verification_pass_rate=(
            _ratio(passed_verification_checks, verification_checks)
            if verification_checks
            else None
        ),
        test_passed=test_passed,
    )


def load_manifest(path: str | Path) -> list[EvalCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise TypeError("Eval manifest must be a list or an object with 'cases'")
    return [
        EvalCase(
            name=str(item["name"]),
            prompt=str(item["prompt"]),
            category=str(item.get("category") or "uncategorized"),
            tags=tuple(str(value) for value in (item.get("tags") or ())),
            output_contains=tuple(item.get("output_contains") or ()),
            output_regex=tuple(item.get("output_regex") or ()),
            output_not_contains=tuple(item.get("output_not_contains") or ()),
            expected_tools=tuple(item.get("expected_tools") or ()),
            expected_tool_groups=tuple(
                tuple(str(tool) for tool in group)
                for group in (item.get("expected_tool_groups") or ())
            ),
            allowed_tools=tuple(item.get("allowed_tools") or ()),
            forbidden_tools=tuple(item.get("forbidden_tools") or ()),
            files_contain=dict(item.get("files_contain") or {}),
            max_seconds=float(item["max_seconds"]) if item.get("max_seconds") else None,
            max_total_tokens=(
                int(item["max_total_tokens"])
                if item.get("max_total_tokens") is not None
                else None
            ),
            max_tool_calls=(
                int(item["max_tool_calls"])
                if item.get("max_tool_calls") is not None
                else None
            ),
            test_command=(str(item["test_command"]) if item.get("test_command") else None),
            test_timeout=float(item.get("test_timeout") or 300),
            expected_test_exit_code=int(item.get("expected_test_exit_code") or 0),
            input_cost_per_million=(
                float(item["input_cost_per_million"])
                if item.get("input_cost_per_million") is not None
                else None
            ),
            output_cost_per_million=(
                float(item["output_cost_per_million"])
                if item.get("output_cost_per_million") is not None
                else None
            ),
        )
        for item in rows
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ForgeAgent Eval cases")
    parser.add_argument("manifest")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--output", default="forgeagent-eval.json")
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run every case N times to expose model variance",
    )
    parser.add_argument(
        "--yes", action="store_true", help="allow stateful Tools inside Eval workspace copies"
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if not settings.api_key or not settings.model:
        parser.error("FORGE_API_KEY and FORGE_MODEL are required")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    source = Path(args.workspace).expanduser().resolve()
    results: list[EvalResult] = []
    retained: list[str] = []
    for case in load_manifest(args.manifest):
        for run_index in range(1, args.repeat + 1):
            temporary = Path(tempfile.mkdtemp(prefix="forgeagent-eval-"))
            case_workspace = temporary / "workspace"
            shutil.copytree(
                source,
                case_workspace,
                ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"),
            )
            bundle = create_runtime(
                settings,
                RuntimeOptions(
                    workspace=case_workspace,
                    allow_all=args.yes,
                    enable_subagents=False,
                ),
            )
            try:
                try:
                    results.append(
                        evaluate_case(
                            case,
                            bundle.runtime,
                            case_workspace,
                            run_index=run_index,
                        )
                    )
                except Exception as error:  # noqa: BLE001 - one Eval must not abort the suite
                    results.append(
                        EvalResult(
                            name=case.name,
                            passed=False,
                            output="",
                            tools=[],
                            duration_seconds=0.0,
                            failures=[f"{type(error).__name__}: {error}"],
                            outcome="infrastructure_error",
                            error_type=type(error).__name__,
                            category=case.category,
                            tags=list(case.tags),
                            run_index=run_index,
                        )
                    )
            finally:
                bundle.close()
            if args.keep_workspaces:
                retained.append(str(case_workspace))
            else:
                shutil.rmtree(temporary)
    report = build_report(
        results,
        retained_workspaces=retained,
        experiment={
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "manifest": Path(args.manifest).name,
            "model": settings.model,
            "fallback_models": [route.model for route in settings.fallback_routes],
            "max_rounds": settings.max_rounds,
            "context_tokens": settings.context_tokens,
            "max_output_tokens": settings.max_output_tokens,
            "sandbox": settings.sandbox,
            "sandbox_workspace": settings.sandbox_workspace,
            "llm_summary": settings.llm_summary,
            "tool_embeddings": bool(settings.tool_embedding_model),
        },
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def build_report(
    results: list[EvalResult],
    *,
    retained_workspaces: list[str] | None = None,
    experiment: dict[str, object] | None = None,
) -> dict[str, object]:
    """Aggregate reproducible task, quality, safety, and efficiency metrics."""
    metrics = _aggregate_results(results)
    categories = sorted({item.category for item in results})
    by_category = {
        category: _aggregate_results([item for item in results if item.category == category])
        for category in categories
    }
    case_runs: dict[str, list[EvalResult]] = {}
    for item in results:
        case_runs.setdefault(item.name, []).append(item)
    unique_cases = len(case_runs)
    passed_once = sum(any(item.passed for item in runs) for runs in case_runs.values())
    stable_cases = sum(all(item.passed for item in runs) for runs in case_runs.values())
    repeat = max((item.run_index for item in results), default=0)
    return {
        "schema_version": 2,
        "experiment": experiment or {},
        "passed": all(item.passed for item in results),
        "passed_cases": sum(item.passed for item in results),
        "total_cases": len(results),
        "unique_cases": unique_cases,
        "repeat": repeat,
        "task_success_rate": metrics["task_success_rate"],
        "case_pass_at_least_once_rate": _ratio(passed_once, unique_cases),
        "stable_case_rate": _ratio(stable_cases, unique_cases),
        "metrics": metrics,
        "by_category": by_category,
        "total_prompt_tokens": sum(item.prompt_tokens for item in results),
        "total_completion_tokens": sum(item.completion_tokens for item in results),
        "total_duration_seconds": sum(item.duration_seconds for item in results),
        "total_test_duration_seconds": sum(item.test_duration_seconds for item in results),
        "estimated_cost_usd": _total_estimated_cost(results),
        "results": [asdict(item) for item in results],
        "retained_workspaces": retained_workspaces or [],
    }


def _aggregate_results(results: list[EvalResult]) -> dict[str, int | float | None]:
    total = len(results)
    passed = sum(item.passed for item in results)
    completed = [item for item in results if item.outcome == "completed"]
    expected_targets = sum(item.expected_tool_targets for item in results)
    matched_targets = sum(item.matched_tool_targets for item in results)
    labeled_calls = sum(item.labeled_tool_calls for item in results)
    relevant_calls = sum(item.relevant_tool_calls for item in results)
    checks = sum(item.verification_checks for item in results)
    passed_checks = sum(item.passed_verification_checks for item in results)
    test_results = [item.test_passed for item in results if item.test_passed is not None]
    total_tokens = sum(item.prompt_tokens + item.completion_tokens for item in results)
    return {
        "runs": total,
        "passed_runs": passed,
        "task_success_rate": _ratio(passed, total),
        "completed_runs": len(completed),
        "infrastructure_failures": total - len(completed),
        "completed_task_success_rate": _optional_ratio(
            sum(item.passed for item in completed), len(completed)
        ),
        "verification_checks": checks,
        "verification_pass_rate": _optional_ratio(passed_checks, checks),
        "test_runs": len(test_results),
        "test_pass_rate": _optional_ratio(sum(test_results), len(test_results)),
        "expected_tool_targets": expected_targets,
        "tool_recall": _optional_ratio(matched_targets, expected_targets),
        "labeled_tool_calls": labeled_calls,
        "tool_precision": _optional_ratio(relevant_calls, labeled_calls),
        "unexpected_tool_calls": sum(len(item.unexpected_tools) for item in results),
        "forbidden_tool_executions": sum(
            len(item.forbidden_tool_executions) for item in results
        ),
        "policy_denied_calls": sum(item.policy_denied_calls for item in results),
        "average_tool_calls": _ratio(sum(len(item.tools) for item in results), total),
        "average_total_tokens": _ratio(total_tokens, total),
        "average_duration_seconds": _ratio(
            sum(item.duration_seconds for item in results), total
        ),
        "estimated_cost_usd": _total_estimated_cost(results),
    }


def _ratio(numerator: float, denominator: int) -> float:
    return round(float(numerator) / denominator, 6) if denominator else 0.0


def _optional_ratio(numerator: float, denominator: int) -> float | None:
    return _ratio(numerator, denominator) if denominator else None


def _total_estimated_cost(results: list[EvalResult]) -> float | None:
    costs = [item.estimated_cost_usd for item in results if item.estimated_cost_usd is not None]
    return round(sum(costs), 8) if costs else None


def _bounded_output(value: str | bytes, limit: int = 16_000) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if len(value) <= limit:
        return value
    return value[-limit:] + f"\n[kept final {limit} characters]"


if __name__ == "__main__":
    raise SystemExit(main())

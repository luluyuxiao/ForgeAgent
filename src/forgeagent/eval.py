"""Deterministic task-manifest evaluator for Agent regressions."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings
from .factory import RuntimeOptions, create_runtime
from .runtime import AgentRuntime


@dataclass(frozen=True)
class EvalCase:
    name: str
    prompt: str
    output_contains: tuple[str, ...] = ()
    output_regex: tuple[str, ...] = ()
    output_not_contains: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    test_exit_code: int | None = None
    test_duration_seconds: float = 0.0
    test_stdout: str = ""
    test_stderr: str = ""
    estimated_cost_usd: float | None = None


def evaluate_case(
    case: EvalCase,
    runtime: AgentRuntime,
    workspace: Path,
) -> EvalResult:
    tools: list[str] = []

    def observe(event: str, fields: dict) -> None:
        if event == "tool_started":
            tools.append(str(fields["tool"]))

    started = time.perf_counter()
    output = runtime.run(case.prompt, on_event=observe)
    duration = time.perf_counter() - started
    failures = []
    for expected in case.output_contains:
        if expected not in output:
            failures.append(f"output does not contain {expected!r}")
    for pattern in case.output_regex:
        if re.search(pattern, output) is None:
            failures.append(f"output does not match /{pattern}/")
    for unexpected in case.output_not_contains:
        if unexpected in output:
            failures.append(f"output unexpectedly contains {unexpected!r}")
    for tool in case.expected_tools:
        if tool not in tools:
            failures.append(f"Tool was not called: {tool}")
    for tool in case.forbidden_tools:
        if tool in tools:
            failures.append(f"forbidden Tool was called: {tool}")
    for relative, expected in case.files_contain.items():
        path = (workspace / relative).resolve()
        if not path.is_relative_to(workspace.resolve()) or not path.is_file():
            failures.append(f"expected file missing: {relative}")
        elif expected not in path.read_text(encoding="utf-8"):
            failures.append(f"{relative} does not contain {expected!r}")
    if case.max_seconds is not None and duration > case.max_seconds:
        failures.append(f"duration {duration:.3f}s exceeds {case.max_seconds:.3f}s")
    usage = runtime.provider.total_usage
    if case.max_total_tokens is not None and usage.total_tokens > case.max_total_tokens:
        failures.append(
            f"token usage {usage.total_tokens} exceeds {case.max_total_tokens}"
        )
    if case.max_tool_calls is not None and len(tools) > case.max_tool_calls:
        failures.append(f"Tool calls {len(tools)} exceeds {case.max_tool_calls}")

    test_exit_code = None
    test_duration = 0.0
    test_stdout = ""
    test_stderr = ""
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
            if test_exit_code != case.expected_test_exit_code:
                failures.append(
                    f"test command exited {test_exit_code}; "
                    f"expected {case.expected_test_exit_code}"
                )
        except TimeoutError:
            failures.append(f"test command timed out after {case.test_timeout:.1f}s")
        except Exception as error:  # noqa: BLE001 - verification failure is an Eval result
            failures.append(f"test command failed to start: {type(error).__name__}: {error}")
        test_duration = time.perf_counter() - test_started

    estimated_cost = None
    if case.input_cost_per_million is not None or case.output_cost_per_million is not None:
        estimated_cost = (
            usage.prompt_tokens * (case.input_cost_per_million or 0.0)
            + usage.completion_tokens * (case.output_cost_per_million or 0.0)
        ) / 1_000_000

    return EvalResult(
        case.name,
        not failures,
        output,
        tools,
        duration,
        failures,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        test_exit_code=test_exit_code,
        test_duration_seconds=test_duration,
        test_stdout=test_stdout,
        test_stderr=test_stderr,
        estimated_cost_usd=estimated_cost,
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
            output_contains=tuple(item.get("output_contains") or ()),
            output_regex=tuple(item.get("output_regex") or ()),
            output_not_contains=tuple(item.get("output_not_contains") or ()),
            expected_tools=tuple(item.get("expected_tools") or ()),
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
        "--yes", action="store_true", help="allow stateful Tools inside Eval workspace copies"
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if not settings.api_key or not settings.model:
        parser.error("FORGE_API_KEY and FORGE_MODEL are required")
    source = Path(args.workspace).expanduser().resolve()
    results: list[EvalResult] = []
    retained: list[str] = []
    for case in load_manifest(args.manifest):
        temporary = Path(tempfile.mkdtemp(prefix="forgeagent-eval-"))
        case_workspace = temporary / "workspace"
        shutil.copytree(source, case_workspace, ignore=shutil.ignore_patterns(".git", ".venv"))
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
                results.append(evaluate_case(case, bundle.runtime, case_workspace))
            except Exception as error:  # noqa: BLE001 - one Eval must not abort the suite
                results.append(
                    EvalResult(
                        name=case.name,
                        passed=False,
                        output="",
                        tools=[],
                        duration_seconds=0.0,
                        failures=[f"{type(error).__name__}: {error}"],
                    )
                )
        finally:
            bundle.close()
        if args.keep_workspaces:
            retained.append(str(case_workspace))
        else:
            shutil.rmtree(temporary)
    report = {
        "passed": all(item.passed for item in results),
        "passed_cases": sum(item.passed for item in results),
        "total_cases": len(results),
        "total_prompt_tokens": sum(item.prompt_tokens for item in results),
        "total_completion_tokens": sum(item.completion_tokens for item in results),
        "total_duration_seconds": sum(item.duration_seconds for item in results),
        "total_test_duration_seconds": sum(item.test_duration_seconds for item in results),
        "estimated_cost_usd": sum(item.estimated_cost_usd or 0.0 for item in results),
        "results": [asdict(item) for item in results],
        "retained_workspaces": retained,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def _bounded_output(value: str | bytes, limit: int = 16_000) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if len(value) <= limit:
        return value
    return value[-limit:] + f"\n[kept final {limit} characters]"


if __name__ == "__main__":
    raise SystemExit(main())

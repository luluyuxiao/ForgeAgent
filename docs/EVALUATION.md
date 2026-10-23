# Task evaluation

ForgeAgent evaluates observable task outcomes rather than asking the model to
judge its own answer. Every case runs in a fresh copy of the selected workspace.
The final answer, executed Tools, resulting files, resource use, and an optional
verification command become independent pass/fail checks.

## Running the bundled benchmark

```bash
forgeagent-eval examples/eval-benchmark.json \
  --workspace examples/eval_workspace \
  --yes --repeat 3 --output eval-report.json
```

`--yes` permits mutating Tools only inside the temporary Eval workspace. Each
repeat starts from the same fixture, so runs cannot contaminate one another.
Record the model, provider, configuration, date, and repeat count when comparing
reports; changing any of them changes the experiment.

## Reported metrics

| Metric | Definition |
|---|---|
| `task_success_rate` | Passed case-runs divided by all case-runs. A run passes only when every declared check passes. |
| `completed_task_success_rate` | Passed runs divided by runs that reached task verification; infrastructure failures are reported separately. |
| `case_pass_at_least_once_rate` | Unique cases with at least one passing repeat divided by unique cases. This is Pass@k-like, not the unbiased pass@k estimator. |
| `stable_case_rate` | Unique cases for which every repeat passed divided by unique cases. |
| `verification_pass_rate` | Passed output, Tool, file, limit, and test checks divided by all declared checks. |
| `test_pass_rate` | Verification commands with the expected exit code divided by all verification commands. |
| `tool_recall` | Required exact Tools and alternative Tool groups satisfied divided by all required Tool targets. |
| `tool_precision` | Calls to expected/alternative/allowed Tools divided by calls in cases that label relevant Tools. |
| `forbidden_tool_executions` | Forbidden Tools that reached execution. The target value is zero. |
| `policy_denied_calls` | Known active Tool calls rejected by the policy pipeline. This is diagnostic and is not inherently good or bad. |
| efficiency | Average Tool calls, total tokens, duration, and optional estimated dollar cost per run. |

The `experiment` object records the model and non-secret runtime settings used
for the report. Provider exceptions are marked as `infrastructure_error` and
counted separately, so a network outage is not silently presented as a model
task failure.

An alternative group such as `["replace_text", "apply_patch", "write_file"]`
counts as one target and is satisfied when any member executes. `allowed_tools`
prevents legitimate supporting calls from being counted as irrelevant without
making those calls mandatory.

## Manifest example

```json
{
  "cases": [
    {
      "name": "fix-parser",
      "category": "bug-fix",
      "tags": ["python"],
      "prompt": "Fix the parser and run its focused test.",
      "expected_tool_groups": [
        ["read_file", "search_text"],
        ["replace_text", "apply_patch", "write_file"],
        ["run_checks", "shell"]
      ],
      "allowed_tools": ["find_files", "git_diff"],
      "forbidden_tools": ["subagent"],
      "max_tool_calls": 12,
      "max_total_tokens": 20000,
      "test_command": "python -m unittest tests.test_parser -q"
    }
  ]
}
```

The bundled four-case fixture demonstrates the format; it is deliberately too
small to support a general claim about Coding Agent accuracy. A resume-quality
result should use a versioned set of at least dozens of representative tasks,
multiple repeats, and a baseline or ablation measured under identical settings.

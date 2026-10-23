# Bundled benchmark results

This report records one small, reproducible demonstration of the ForgeAgent Eval
pipeline. It is not a claim of general Coding Agent accuracy.

## Experiment

| Field | Value |
|---|---:|
| Date | 2026-10-23 |
| Primary model | `deepseek-chat` |
| Configured fallback | `deepseek-reasoner` |
| Manifest | `examples/eval-benchmark.json` |
| Fixture | `examples/eval_workspace` |
| Unique tasks | 4 |
| Repeats per task | 3 |
| Total case-runs | 12 |
| Sandbox executor | local |
| Max rounds | 24 |

Command:

```bash
forgeagent-eval examples/eval-benchmark.json \
  --workspace examples/eval_workspace \
  --yes --repeat 3 --output eval-report.json
```

## Results

| Metric | Result |
|---|---:|
| Task success | 12/12 (100%) |
| Stable cases | 4/4 (100%) |
| Infrastructure failures | 0 |
| Verification checks | 111/111 (100%) |
| Post-run tests | 9/9 (100%) |
| Required Tool targets recalled | 30/30 (100%) |
| Labeled Tool-call precision | 73/73 (100%) |
| Unexpected Tool calls | 0 |
| Forbidden Tool executions | 0 |
| Average Tool calls per run | 6.08 |
| Average tokens per run | 16,024.83 |
| Average Agent duration per run | 6.22 s |

| Category | Runs | Success | Average Tool calls | Average tokens |
|---|---:|---:|---:|---:|
| Code understanding | 3 | 100% | 3.00 | 7,579.33 |
| Bug fix | 6 | 100% | 7.00 | 18,670.17 |
| Feature implementation | 3 | 100% | 7.33 | 19,179.67 |

Provider prices were not configured, so this run does not report dollar cost.
The complete generated JSON report was kept outside the repository because it
contains verbose model outputs and temporary execution details.

## Interpretation

The run verifies that task isolation, output assertions, Tool accounting,
post-run tests, category aggregation, and repeat stability work end to end. The
sample is intentionally easy and too small for comparison with public Coding
Agent benchmarks. A resume-quality general capability claim should use a
versioned set of dozens or hundreds of tasks and compare the same model and
settings against a baseline or ablation.

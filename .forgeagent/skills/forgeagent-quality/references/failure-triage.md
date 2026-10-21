# Quality-gate failure triage

Read this file only after the quality gate reports a failure.

## Tests

- Re-run the smallest failing test with `pytest -q <path> -k <name>`.
- Decide whether the failure is caused by the implementation, an outdated expectation, or
  unavailable environment state.
- Do not weaken or delete a test merely to make the gate pass.

## Lint

- Fix the reported location rather than applying broad formatting to unrelated files.
- Review import and type-related fixes for behavioral changes before accepting them.

## Compilation

- Treat syntax and import-time parse failures as blocking.
- Confirm the failing path belongs to the current change before editing it.

## Environment failures

- Missing dependencies, unavailable executables, permissions, and network failures are not
  product regressions by themselves.
- Report the exact blocked check and the required environment change; do not claim the gate
  passed.

After a fix, re-run the focused failure first and then the complete quality gate.

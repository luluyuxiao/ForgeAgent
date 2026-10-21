---
name: forgeagent-quality
description: Validate ForgeAgent Python changes with focused tests, the full quality gate, failure triage, and an evidence-based report. Use after code, test, or example changes; skip for documentation-only edits.
---

# ForgeAgent quality validation

Use this workflow to decide whether a ForgeAgent code change is ready.

1. Inspect `git status --short` and the relevant diff. Preserve unrelated working-tree changes.
2. Run the smallest relevant tests first so failures are attributable to the current change.
3. From the repository root, run:

   ```bash
   python .forgeagent/skills/forgeagent-quality/scripts/run_quality_gate.py
   ```

   Add `--json` when another program needs to consume the results.
4. If a check fails, read
   `.forgeagent/skills/forgeagent-quality/references/failure-triage.md` and classify the
   failure before editing code.
5. Report the final evidence using
   `.forgeagent/skills/forgeagent-quality/assets/quality-report.md` as a concise template.

The helper only runs checks. It does not fix files, install dependencies, grant permission,
or weaken sandbox and policy decisions. Do not claim success from stale output or skip a
required check without stating why.

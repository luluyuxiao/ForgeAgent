# Security model

ForgeAgent separates behavioral guidance, authorization, and containment. A
System Prompt, repository instruction, Skill, or model response can suggest an
action, but none of them grants authority to perform it.

## Trust boundaries

- `PolicyPipeline` decides whether a proposed Tool call is admitted.
- `PermissionPolicy` asks before external mutation unless the exact action was
  pre-approved for the current process.
- `CommandExecutor` determines where admitted commands run. Docker mode is a
  containment boundary; local mode is not.
- Workspace path validation prevents direct file Tools from escaping the
  selected root. It cannot infer every side effect of an arbitrary shell command.
- MCP servers and Hooks are executable integrations. Review their configuration
  and prefer Docker plus the minimum network/workspace access they need.

## Hooks

Hooks receive Tool arguments and results as JSON on stdin. They run through the
same local or Docker executor selected for process Tools, have a ten-second
timeout, and cannot grant permission. A matching PreToolUse hook blocks a call
only when it exits with code 2. PostToolUse hooks observe completed calls.

## MCP

MCP servers are admitted by capability before connection when an allowlist is
configured. Calls to one server serialize; independent servers may overlap.
Transport failures open a bounded reconnect path and eventually a circuit
breaker. A failed `tools/call` is never replayed automatically because the
remote side effect may already have occurred.

## Persistence and sensitive data

SQLite Sessions, JSONL traces, Tool output, structured memory, and checkpoints
may contain prompts, source code, command output, or secrets read from the
workspace. Session databases are created with user-only permissions where the
operating system permits it, but they are not encrypted. Trace content remains
redacted unless `--trace-content` is explicitly enabled.

API keys and remembered permission grants are not serialized. Use a protected
session directory, avoid content traces for sensitive repositories, and rotate
any credential that appears in a Tool result or trace.

## Non-goals

- Checkpoints undo direct file Tools, not arbitrary shell, MCP, Hook, network,
  Git history, or external service effects.
- Worktree isolation separates Git changes; it is not a process sandbox.
- Structured task constraints are semantic memory and do not replace policy.
- Runtime USD limits are local estimates based on configured prices, not an
  account-side billing control.

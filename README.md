# ForgeAgent

[English](README.md) | [简体中文](README.zh-CN.md)

ForgeAgent is a compact, policy-driven Coding Agent / Agent Harness. It keeps
model transport, context budgeting, authorization, scheduling, execution,
persistence and observability behind separate interfaces instead of placing all
behavior in one Agent class.

This is an independent implementation based on public Tool Calling and MCP
specifications, observable agent behavior, and common ReAct patterns. It does
not use non-public or leaked proprietary source code.

## Current capabilities

- bounded ReAct loop, streaming, fragmented Tool Call assembly and retry;
- ordered provider fallback with a no-replay-after-streaming guarantee;
- instance-scoped `ToolRegistry`, lazy `ToolCatalog`, JSON Schema exposure;
- Permission, Plan Mode and per-Tool capability policies;
- effect/resource-aware scheduling: independent reads overlap, writes serialize;
- workspace-confined file tools plus local or hardened Docker shell execution;
- Git status/diff and checked multi-file patch application with file checkpoints;
- repository instructions from `AGENTS.md` plus scoped-instruction discovery;
- project-defined lint/test commands through `.forgeagent.json`;
- MCP Tool discovery/calls over stdio or HTTP, across multiple servers;
- legacy MCP initialization and modern stateless `2026-07-28` requests;
- optional Docker isolation for stdio MCP servers;
- foreground Sub-Agents and bounded in-process background workers;
- full Transcript plus compact Active Context, Tool Result snipping, LLM summary,
  deterministic collapse and hard budget enforcement;
- atomic Session save/resume including Active Context, catalog state and Undo data;
- file Checkpoint/Undo for `write_file` and `replace_text`;
- metadata-redacted JSONL traces plus a portable HTML trajectory viewer;
- manifest-based Eval runner with output, Tool, file, verification, token and duration assertions.

## Architecture

```text
CLI / library / Eval
         │
         ▼
   AgentRuntime ───────────────► JsonlTrace
         │
         ├──► ContextWindow ───► summary / snip / collapse
         │
         ├──► ChatProvider ────► primary → fallback routes
         │          │
         │          └──────────► text / Tool Calls
         ▼
  ToolCatalog / ToolRegistry
         │
         ▼
  PolicyPipeline
 capability → planning → permission
         │
         ▼
  BatchScheduler ──► CheckpointManager
         │
         ├──► built-in Tool ───► local / Docker executor
         ├──► McpTool ─────────► stdio / HTTP MCP server
         └──► SubAgentTool ─────► child AgentRuntime / background worker
                                      │
                                      └── independent conversation + context
```

`transcript` is the append-only history used for audit and replay.
`active_messages` is the bounded view sent to the model. Context compaction
changes only the latter.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Put your provider key in `.env`, then start the REPL:

```bash
forgeagent
```

One-shot mode is non-interactive and therefore rejects stateful Tools unless
`--yes` is explicit:

```bash
forgeagent --trace .forgeagent/run.jsonl \
  -p "inspect this project and explain its architecture"
```

For scripting and CI, emit lifecycle events and the final response as JSONL:

```bash
forgeagent --json -p "inspect this project and report any test failures"
```

Useful REPL commands include `/plan`, `/approve`, `/tools`, `/catalog QUERY`,
`/tasks`, `/undo`, `/save ID`, `/resume ID`, `/tokens` and `/sessions`.

## Repository instructions and checks

ForgeAgent automatically adds a root `AGENTS.md` and
`.forgeagent/instructions.md` to the System Prompt. Nested `AGENTS.md` paths are
listed so the model can read the applicable file before editing in that scope.
Repository guidance cannot grant Tool permission or weaken runtime policy.

Copy the example check configuration into a repository:

```bash
cp examples/forgeagent.json .forgeagent.json
```

The `run_checks` Tool executes only the named lint/test commands from this
file. Like other process Tools, it still passes Plan Mode, capability and
permission policies. File search uses `rg` when available, including hidden
project files while respecting ignore rules and excluding `.git`.

Git-aware Tools include `git_status`, `git_diff` and `apply_patch`. Patch input
is checked with `git apply --check`, restricted to workspace-relative paths and
captured by the ordinary file checkpoint mechanism before application.

## Provider fallback

The primary route uses `FORGE_MODEL`, `FORGE_BASE_URL` and `FORGE_API_KEY`.
Fallback routes are an ordered JSON array. Each route names the environment
variable containing its credential; credentials are never serialized:

```bash
FORGE_FALLBACKS='[
  {"model":"deepseek-reasoner","base_url":"https://api.deepseek.com","api_key_env":"FORGE_API_KEY"}
]'
```

Fallback happens only before any text or Tool Call fragment has streamed. Once
visible output begins, ForgeAgent raises `PartialResponseError` instead of
replaying the request on another model and risking duplicate actions.

## Docker sandbox

Build the image once:

```bash
docker build -f Dockerfile.sandbox -t forgeagent-sandbox:latest .
```

Run shell Tools in a container:

```bash
forgeagent --sandbox docker --sandbox-image forgeagent-sandbox:latest
```

The command uses a read-only root filesystem, dropped Linux capabilities,
`no-new-privileges`, PID/CPU/memory limits, a bounded `/tmp`, a non-root UID,
and `--network none` by default. The selected workspace is the only writable
bind mount. Docker failure is fail-closed: it never falls back to host shell.
Use `--sandbox-network bridge` only when the task really needs network access.

## MCP

Copy `examples/mcp.json`, configure one or more servers, then run:

```bash
forgeagent --mcp-config .forgeagent/mcp.json
```

MCP tools are discovered with `tools/list`, registered as names such as
`mcp__weather__current`, and initially kept out of the model schema. The model
can find them through `search_tools` and expose selected schemas with
`activate_tools`, avoiding a large schema tax. Calls use `tools/call`; results
return as ordinary Tool observations. MCP tools declare `UNKNOWN` effect and
still pass Plan Mode, capability policy and Permission.

HTTP defaults to the modern stateless `2026-07-28` lifecycle; stdio defaults to
the widely deployed `2025-11-25` initialize/initialized lifecycle. A stdio
server can set `"sandboxImage": "your-mcp-image:tag"` to run with Docker
hardening. This client implements Tool discovery/calls, not MCP resources,
prompts, elicitation, OAuth flows, or dynamic list-change subscriptions.

## Sub-Agents and background tasks

The `subagent` Tool accepts `run_mode=foreground|background`. Every child gets a
new `AgentRuntime`, Transcript, Active Context, provider instance and registry;
the child registry intentionally omits `subagent`, preventing recursive
delegation. Background calls return a task id, and `task_status` or `/tasks`
reports state, result, failure and child token usage. Workers are bounded and
process-local: task state does not survive CLI exit.

Child runs are intentionally non-interactive. Without `--yes`, their stateful
Tools fail closed instead of trying to read a second terminal permission prompt
from a worker thread.

## Context, Session and Undo

Context budgeting includes System Prompt, active Tool schemas, history, output
reserve and safety margin. Under pressure it performs:

```text
old Tool Result snip → LLM summary of old blocks → deterministic block collapse
→ last-resort content truncation → ContextLimitError if still impossible
```

The newest protocol block is preserved. Summary failure falls back to
deterministic collapse. Sessions save the full Transcript and the already
compacted Active Context separately, so resume is fast without destroying
audit history.

Before a recoverable file Tool executes, `CheckpointManager` stores original
bytes (or records that the file did not exist). `/undo` restores the latest
checkpoint. Arbitrary shell side effects and remote MCP effects cannot be
reliably inferred and are deliberately not advertised as undoable.

## Trace and Eval

JSONL trace content is redacted by default:

```bash
forgeagent --trace .forgeagent/run.jsonl
# Explicitly opt in when the file may contain source, prompts and Tool arguments:
forgeagent --trace .forgeagent/run.jsonl --trace-content
```

Render the trace into a self-contained report that can be attached to CI or
opened directly in a browser:

```bash
forgeagent-trace .forgeagent/run.jsonl -o .forgeagent/run.html
```

Run the example Eval manifest against an isolated copy of a workspace:

```bash
forgeagent-eval examples/eval.json --workspace . --output eval-report.json
```

Eval is non-interactive and fails closed for mutating Tools. Add `--yes` only
for manifests you trust; changes still occur in a temporary workspace copy.
Cases may assert required/forbidden Tools, output presence/absence, maximum
Tool calls/tokens, file contents, duration and a post-run `test_command`. Eval
reports aggregate tokens and duration alongside per-case verification output.
Optional per-million input/output prices produce a reproducible estimated-cost
field without embedding fast-changing provider prices in ForgeAgent.
The post-run command uses the same local or Docker executor selected for the
Agent; manifests are executable inputs and should still be reviewed before use.

## Plan Mode and safety boundaries

`/plan` permits only `PURE` and `READ` Tool effects. `/approve` returns to
normal ReAct execution; this is not a separate Planner/Executor graph.
Permission decides whether a proposed action is authorized. Docker decides
what an authorized process can actually reach. Prompt instructions are useful
behavioral guidance, but neither a permission boundary nor a sandbox.
Interactive “always” approval is session-only and scoped to the exact command,
file path or call arguments rather than granting an entire Tool class.

## Test

```bash
pytest -q
ruff check src tests
python -m compileall -q src tests
```

The offline suite does not require an API key, Docker daemon, or live MCP
server; it validates protocol routing and command construction with fakes.

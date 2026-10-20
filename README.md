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
- bounded structured memory for goals, constraints, plans, decisions and changed files;
- lazily activated project/user Skills and sandbox-aware pre/post Tool Hooks;
- project-defined lint/test commands through `.forgeagent.json`;
- MCP Tool discovery/calls over stdio or HTTP, across multiple servers;
- per-server MCP failure isolation, reconnect, circuit breaking and schema refresh;
- legacy MCP initialization and modern stateless `2026-07-28` requests;
- optional Docker isolation for stdio MCP servers;
- foreground/background Sub-Agents with shared or retained Git worktree isolation;
- full Transcript plus compact Active Context, Tool Result snipping, LLM summary,
  deterministic collapse and hard budget enforcement;
- transactional SQLite Session save/resume with legacy JSON compatibility;
- configurable runtime USD ceilings based on explicit input/output token prices;
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
         ├──► TaskMemory / Skills / CostBudget
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
`/tasks`, `/memory`, `/skills`, `/mcp-refresh`, `/diff`, `/transcript`, `/undo`,
`/save ID`, `/resume ID`, `/tokens` and `/sessions`.

## Feature examples

### 1. Analyze a project without changing it

Without `--yes`, a one-shot run does not authorize file mutations or process
execution. This is a useful first step for an unfamiliar repository:

```bash
forgeagent --workspace . \
  -p "Read this project and explain its entry point, major modules, and request flow"
```

### 2. Run checks and fix a failure

This task can inspect code, run checks allowed by `.forgeagent.json`, and edit
workspace files. `--yes` pre-authorizes stateful Tools and should only be used
in a trusted workspace:

```bash
forgeagent --workspace . --yes \
  -p "Run the project checks, find the cause of any failure, make the smallest fix, and verify it"
```

Afterwards, use `/diff` in the interactive REPL to inspect changes. If a change
was made by a recoverable file Tool, `/undo` restores the latest checkpoint.

### 3. Review a plan before execution

```text
$ forgeagent
You > /plan
You (plan) > Add unit tests for the Tool permission module; first identify the files involved
You (plan) > /approve
```

Plan Mode permits read-only operations. `/approve` leaves Plan Mode and asks the
Agent to execute the proposed plan. The interactive REPL still requests
permission for concrete writes or commands according to policy.

### 4. Save and resume a longer task

```bash
# Start a named Session that is saved after every turn
forgeagent --session refactor-demo

# Later, restore conversation, task memory, Tool state, and checkpoints
forgeagent --resume refactor-demo
```

The equivalent REPL commands are `/save refactor-demo`, `/sessions`, and
`/resume refactor-demo`.

### 5. Extend the Agent with MCP

Use the repository's local project-inspection MCP server:

```bash
forgeagent --yes --mcp-config examples/mcp.local.json \
  -p "Use project-inspector to summarize this project and find TODO, FIXME, and XXX markers"
```

Query current third-party library documentation:

```bash
forgeagent --yes --mcp-config examples/mcp.json \
  -p "Use Context7 to find the Python 3.13 pathlib documentation and show how to read a text file"
```

MCP Tools default to an `UNKNOWN` effect, so non-interactive `-p` mode needs
explicit authorization. Review the MCP configuration and its server first. To
avoid pre-authorization, omit `-p` and `--yes`, then approve individual calls
in the interactive REPL.

### 6. Record and inspect an Agent run

```bash
forgeagent --trace .forgeagent/run.jsonl \
  -p "Read the project and explain the Tool registration flow"
forgeagent-trace .forgeagent/run.jsonl -o .forgeagent/run.html
```

The generated HTML report shows model rounds, Tool calls, timing, and token
usage. Trace content is redacted by default, including prompts, source code, and
Tool arguments.

## Repository instructions and checks

ForgeAgent automatically adds a root `AGENTS.md` and
`.forgeagent/instructions.md` to the System Prompt. Nested `AGENTS.md` paths are
listed so the model can read the applicable file before editing in that scope.
Repository guidance cannot grant Tool permission or weaken runtime policy.

This repository ships a working `.forgeagent.json`. For another repository,
copy the example check configuration:

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

## Task memory, Skills, Hooks and cost

Structured task memory is kept outside compactable chat history and re-injected
on every request. The model can maintain a bounded goal, constraints, plan,
decisions and modified-file list through `memory_update`; the same state is
available through `/memory`, `/goal`, `/constraint` and `/decision` and is saved
with the Session. These constraints are semantic guidance, not authorization.

Skills are discovered from `~/.forgeagent/skills/*/SKILL.md` and
`.forgeagent/skills/*/SKILL.md`. Only their name and description are shown at
startup; `load_skill` activates the full workflow instructions. Project Skills
override same-named user Skills and neither can grant Tools or permissions.

Copy `examples/hooks.json` to `.forgeagent/hooks.json` to configure
`PreToolUse` and `PostToolUse` commands. Hooks receive JSON on stdin and run
through the selected local or Docker executor. Exit code 2 from a pre-hook
blocks the Tool; hook failures never grant access or crash the Agent.

Runtime spend checks require explicit prices so fast-changing provider pricing
is never hard-coded. The numbers below are illustrative; verify the current
prices for your route:

```bash
forgeagent --max-cost-usd 1.00 \
  --input-price-per-million 0.27 \
  --output-price-per-million 1.10
```

Before each model request, ForgeAgent reserves the estimated input plus maximum
output cost and fails closed if that projection exceeds the configured ceiling.

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

The repository includes a ready-to-use [Context7 MCP](https://mcpservers.org/zh-CN/servers/context7-mcp-server)
example. It adds current, version-specific library documentation and code
examples without duplicating ForgeAgent's built-in file and Git Tools:

```bash
forgeagent --mcp-config examples/mcp.json
```

For example, ask: "Use Context7 to find the Python 3.13 `pathlib`
documentation and show an example." The model discovers and activates
`mcp__context7__resolve-library-id` and `mcp__context7__query-docs` before
calling them. Basic use does not require a key. For higher rate limits, obtain
a Context7 API key and add `"Authorization": "Bearer ${CONTEXT7_API_KEY}"` to
the custom server config's `headers`; never commit the real credential.

The example explicitly selects the `2025-11-25` initialized lifecycle because
Context7's Streamable HTTP endpoint requires `initialize`; it must not use
ForgeAgent's stateless HTTP default.

To connect another server, copy `examples/mcp.json` to the Git-ignored
`.forgeagent/mcp.json` and replace the server entry.

The repository also includes a zero-dependency local stdio example:

```bash
forgeagent --mcp-config examples/mcp.local.json
```

ForgeAgent starts `examples/local_mcp_server.py` as a child process and confines
its reads to the selected workspace. It exposes `project_summary` (file, text
line, test-file and extension counts) and `find_markers` (TODO/FIXME/XXX scan).
Try: "Use project-inspector to summarize this project and find TODOs." The
server reserves stdout for newline-delimited JSON-RPC and sends diagnostics to
stderr only.

MCP tools are discovered with `tools/list`, registered as names such as
`mcp__weather__current`, and initially kept out of the model schema. The model
can find them through `search_tools` and expose selected schemas with
`activate_tools`, avoiding a large schema tax. Calls use `tools/call`; results
return as ordinary Tool observations. MCP tools declare `UNKNOWN` effect and
still pass Plan Mode, capability policy and Permission.

One broken server is isolated instead of aborting every integration. A failed
call is not replayed, but a transport can reconnect before the next call;
repeated failures open a cooldown circuit. `/mcp-refresh` reconnects as needed
and refreshes schemas while retaining Tools from servers whose refresh failed.
Calls to one server serialize, while calls to independent servers may overlap.

HTTP defaults to the modern stateless `2026-07-28` lifecycle; stdio defaults to
the widely deployed `2025-11-25` initialize/initialized lifecycle. A stdio
server can set `"sandboxImage": "your-mcp-image:tag"` to run with Docker
hardening. This client implements Tool discovery/calls, not MCP resources,
prompts, elicitation, OAuth flows, or dynamic list-change subscriptions.

## Sub-Agents and background tasks

The `subagent` Tool accepts `run_mode=foreground|background` and
`isolation=shared|worktree`. Every child gets a
new `AgentRuntime`, Transcript, Active Context, provider instance and registry;
the child registry intentionally omits `subagent`, preventing recursive
delegation. Background calls return a task id, and `task_status` or `/tasks`
reports state, result, failure and child token usage. Workers are bounded and
process-local: task state does not survive CLI exit. Worktree mode creates a
retained `forgeagent/agent-*` branch and checkout from `HEAD`, reports both
paths, and never merges or deletes them automatically. Uncommitted parent
changes are intentionally not copied into the worktree.

Child runs are intentionally non-interactive. Without `--yes`, their stateful
Tools fail closed instead of trying to read a second terminal permission prompt
from a worker thread.

## Context, Session and Undo

Context budgeting uses a hybrid threshold: the input budget is the smaller of
the context window minus the output reserve and safety margin, or a configured
fraction of the context window. The ratio defaults to `0.85` and can be changed
with `FORGE_CONTEXT_INPUT_RATIO` or `--context-input-ratio`; it must be greater
than `0` and at most `1`. With the default 128K window, 8192-token output
reserve and 1024-token margin, the ratio budget of 108800 is stricter than the
fixed-reserve budget of 118784, so compaction starts at about 85%.

Context budgeting includes System Prompt, active Tool schemas, history, output
reserve and safety margin. Under pressure it performs:

```text
old Tool Result snip → LLM summary of old blocks → deterministic block collapse
→ last-resort content truncation → ContextLimitError if still impossible
```

The newest protocol block is preserved. Summary failure falls back to
deterministic collapse. Sessions save the full Transcript and the already
compacted Active Context separately in a transactional SQLite database,
together with structured memory, active Skills, catalog state and checkpoints.
Legacy JSON Sessions remain readable, so resume is fast without destroying
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

`/plan` permits only `PURE` and `READ` Tool effects. Internal `STATE` mutations
such as `memory_update` are permission-free in normal mode but still rejected
while planning. `/approve` returns to normal ReAct execution; this is not a
separate Planner/Executor graph.
Permission decides whether a proposed action is authorized. Docker decides
what an authorized process can actually reach. Prompt instructions are useful
behavioral guidance, but neither a permission boundary nor a sandbox.
Interactive “always” approval is session-only and scoped to the exact command,
file path or call arguments rather than granting an entire Tool class.

## Test

```bash
pytest -q
ruff check src tests examples
python -m compileall -q src tests examples
```

The offline suite does not require an API key, Docker daemon, or live MCP
server; it validates protocol routing and command construction with fakes.

## Acknowledgements

ForgeAgent is independently implemented, while its design has been informed by
public coding-agent projects and specifications, including
[CoreCoder](https://github.com/he-yufeng/CoreCoder), aider, SWE-agent,
mini-swe-agent, OpenHands, OpenAI-compatible Tool Calling, and MCP.

# Architecture

ForgeAgent has one coordinator, `AgentRuntime`, and small components with
separate reasons to change:

| Component | Decision it owns |
|---|---|
| `ChatProvider` | how one model request is transported |
| `FallbackProvider` | which ordered route receives a request |
| `ContextWindow` | what fits into the next model request |
| `ToolCatalog` | which discovered schemas are currently visible |
| `ToolRegistry` | Tool identity, validation and execution |
| `PolicyPipeline` | whether a proposed call is authorized |
| `BatchScheduler` | which calls may overlap |
| `CheckpointManager` | which file bytes can be restored |
| `JsonlTrace` | which lifecycle events are persisted |
| `SubAgentManager` | child/background task state |
| Repository instructions | which repo-local rules enter the System Prompt |

## Main request state machine

```text
IDLE
  │ user input → append full Transcript + Active Context
  ▼
PREPARE_CONTEXT
  │ system + active schemas + history + output reserve + margin
  ▼
MODEL_REQUEST ── route failure before stream ──► FALLBACK_PROVIDER
  │
  ├── final text ──────────────────────────────► COMPLETE
  │
  └── Tool Calls
          ▼
      CATALOG CHECK
          ▼
      CAPABILITY → PLAN → PERMISSION
          │ denied call becomes one Tool observation
          ▼
      SCHEDULE WAVES
          ▼
      CHECKPOINT → TOOL EXECUTION → OBSERVATION
          └────────────────────────────────────► PREPARE_CONTEXT
```

The loop stops on a model response without Tool Calls or at `max_rounds`.
Results are appended in the model's original call order even if read-only calls
finish in a different order.

Root `AGENTS.md` and `.forgeagent/instructions.md` content is loaded once at
runtime construction. Nested `AGENTS.md` paths are indexed in the prompt so the
model can read the applicable file before editing within that directory, while
unrelated nested instructions do not consume every request's context budget.

## Tool exposure and authority

Registration and exposure are different. `ToolRegistry` owns every executable
Tool instance; `ToolCatalog` owns the active subset whose JSON Schemas are sent
to the model. A hidden Tool cannot be executed by a hallucinated direct call.
The two catalog Tools let the model search and activate a small working set.

Policy order is fixed by composition:

```text
CapabilityPolicy → PlanningPolicy → PermissionPolicy
```

MCP tools do not bypass it. Docker sandboxing happens later, during execution,
and is defense in depth rather than authorization.

Interactive remembered approval is scoped to an exact command, file path or
canonical argument object and remains process-local. It never serializes into a
saved Session as a blanket Tool grant.

## Code change and verification boundary

```text
read/search ─► write/replace/apply_patch ─► git_diff ─► run_checks ─► final response
                         │                                  │
                         └──── file checkpoint              └── project config
```

`apply_patch` validates a git-style patch with `git apply --check`, rejects
paths outside the workspace and exposes affected paths to the checkpoint and
resource schedulers. `run_checks` does not accept arbitrary model-provided
commands: it executes only lint/test commands declared by the repository in
`.forgeagent.json`. The commands still require process permission and run
through the selected local or Docker executor.

## Context and persistence

```text
append-only Transcript ───────────────────────────────► Session
           │
           └── copy ─► Active Context ─► compact ─────► Session
```

Tool Call plus its Tool observations form an atomic block during collapse.
The latest block remains available for at least one next model request. LLM
summary is optional and failure-tolerant; deterministic collapse guarantees a
path that does not depend on another provider call.

Session v2 stores Transcript, Active Context, run mode, usage, active catalog
names and bounded file checkpoints. It never stores API keys or remembered
permission grants.

## MCP boundary

```text
config → transport → McpClient.tools/list → McpTool proxies → ToolRegistry
                                      tools/call ◄────────────┘
```

`StdioTransport` serializes JSON-RPC requests over a child process.
`HttpTransport` accepts JSON or SSE responses, tracks legacy session ids, and
sends modern protocol/method headers. Multiple servers are flattened into one
collision-checked registry namespace.

## Sub-Agent boundary

```text
parent Tool Call
    └── SubAgentManager
          ├── foreground ─► child AgentRuntime ─► bounded final result
          └── background ─► bounded thread pool ─► task_status
```

Children share configuration and workspace authority but not conversation,
context, provider connection, or registry instance. Child registries omit the
delegation Tools. Child exceptions are recorded in task state and converted to
observations instead of crashing the parent.

## Explicit limits

- Background workers are in-process, not durable distributed jobs.
- File checkpoints do not make shell, Docker, MCP, or network effects reversible.
- The MCP client currently covers Tools only.
- Docker protects shell/MCP process execution; it does not sandbox the LLM API.
- JSON token estimation is approximate; the hard budget intentionally keeps a
  safety margin.
- Git checkpoints cover direct file and patch Tools; arbitrary Shell commands
  still require an external Git/worktree strategy for transactional rollback.

# Architecture

ForgeAgent has one coordinator, `AgentRuntime`, and small components with
separate reasons to change:

| Component | Decision it owns |
|---|---|
| `ChatProvider` | how one model request is transported |
| `FallbackProvider` | which ordered route receives a request |
| `ContextWindow` | what fits into the next model request |
| Transcript retrieval Tools | which bounded earlier messages are recalled locally |
| `TaskMemory` | which goal, constraints, plan and decisions survive compaction |
| `SkillRegistry` | which workflow instructions are discovered and activated |
| `CostBudget` | whether the next model request fits the configured spend ceiling |
| `ToolCatalog` | which schemas are visible and how hidden Tools are retrieved |
| `ToolRegistry` | Tool identity, validation and execution |
| `PolicyPipeline` | whether a proposed call is authorized |
| `BatchScheduler` | which calls may overlap |
| `ToolHooks` | which configured pre/post observers run around an admitted call |
| `CheckpointManager` | which file bytes can be restored |
| `JsonlTrace` | which lifecycle events are persisted |
| `SubAgentManager` | child/background task state |
| Repository instructions | which repo-local rules enter the System Prompt |

## Package layout

The package stays shallow for a project of this size. Only implementations with
multiple interchangeable or independently growing parts receive a subpackage:

```text
src/forgeagent/
├── runtime.py            # bounded agent loop
├── factory.py            # CLI/Eval composition root
├── config.py             # environment settings and cost budget
├── context.py            # context compaction and optional summarization
├── retrieval.py          # shared local tokenization and BM25 ranking
├── guidance.py           # repository instructions and lazy Skills
├── policies.py           # capability, plan and permission admission
├── scheduler.py          # effect/resource-aware execution waves
├── workspace_snapshot.py # disposable process workspaces and change manifests
├── session.py            # SQLite snapshots and legacy JSON loading
├── mcp.py                # MCP transports, clients and Tool proxies
├── workers.py            # foreground/background Sub-Agents
├── providers/            # model/embedding transports and fallback routing
└── tools/
    ├── base.py           # Tool contract and resource claims
    ├── conversation.py   # bounded Transcript search and range reads
    ├── registry.py       # registration and lazy schema catalog
    ├── filesystem.py     # workspace-confined file Tools
    ├── git.py            # status, diff and checked patch Tools
    └── process.py        # configured checks and shell execution
```

Small related mechanisms deliberately share modules: cost accounting lives
with runtime configuration, summarization with context management, repository
instructions with Skills, and Tool discovery with Tool registration. This keeps
the top-level package readable without introducing an enterprise-style layer
for every class.

## Main request state machine

```text
IDLE
  │ user input → append full Transcript + Active Context
  ▼
PREPARE_CONTEXT
  │ system + active schemas + history + output reserve + margin
  ▼
COST RESERVATION
  │ projected input + maximum output must fit
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
      PRE-HOOK → CHECKPOINT → TOOL EXECUTION → POST-HOOK → OBSERVATION
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

Catalog retrieval first applies static runtime availability: capability
allowlists, Plan Mode effects, and whether a non-interactive run can authorize
the Tool. Offline BM25 ranks the remaining Tool names, descriptions,
capabilities, and effects. When an optional embedding backend is configured,
cached description vectors add semantic recall and cosine scores are fused with
BM25 before Top-K selection. A provider failure falls back to BM25. Embedding
search declares network effect/capability because the query and Tool metadata
leave the process. Activation only changes schema visibility; normal execution
policy is still authoritative.

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

Docker execution defaults to an isolated workspace snapshot. Git tracked and
non-ignored files are copied to a temporary directory, a fresh repository
baseline is created, and only that copy is bind-mounted read/write. A bounded
status summary reports additions, modifications and deletions before the copy is
discarded. `direct` mode deliberately restores the old writable bind mount for
approved workflows that must persist process-generated files. This separates
host containment from workspace integrity; it does not rely on a fragile shell
command denylist. Local execution remains explicitly non-sandboxed.

## Context and persistence

```text
append-only Transcript ───────────────────────────────────► Session
           │                                      ▲
           ├── copy ─► Active Context ─► compact ─┤
           │
           └── local BM25 search ─► bounded range read ─► next model round
```

Tool Call plus its Tool observations form an atomic block during collapse.
The latest block remains available for at least one next model request. LLM
summary is optional and failure-tolerant; deterministic collapse guarantees a
path that does not depend on another provider call.

Session v3 stores transactional SQLite snapshots containing Transcript, Active
Context, run mode, usage, active catalog names, structured task memory, active
Skills and bounded file checkpoints. Legacy v1/v2 JSON documents remain
readable. Sessions never store API keys or remembered permission grants.

`search_transcript` and `read_transcript_range` expose the durable Transcript as
a bounded read-only resource with the `conversation_read` capability. Search is
local BM25; it does not use the optional Tool Catalog embedding backend. Results
retain original message indexes, exclude the live user turn, and cap snippets,
range size and per-message content so recalled history cannot replace normal
context budgeting. The Tools resolve `runtime.transcript` dynamically, so a
Session restore that replaces the in-memory list is immediately visible.

## MCP boundary

```text
config → transport → McpClient.tools/list → McpTool proxies → ToolRegistry
                                      tools/call ◄────────────┘
```

`StdioTransport` serializes JSON-RPC requests over a child process.
`HttpTransport` accepts JSON or SSE responses, tracks legacy session ids, and
sends modern protocol/method headers. Multiple servers are flattened into one
collision-checked registry namespace. Startup failure is isolated per server;
failed Tool calls are not replayed, while a later call may reconnect. Repeated
failures open a cooldown circuit. Refresh replaces schemas only for healthy
servers, and resource claims serialize calls per server rather than globally.

## Sub-Agent boundary

```text
parent Tool Call
    └── SubAgentManager
          ├── foreground ─► child AgentRuntime ─► bounded final result
          └── background ─► bounded thread pool ─► task_status
                    └──────► optional retained Git worktree / branch
```

Children share configuration and permission posture but not conversation,
context, provider connection, or registry instance. Shared mode uses the parent
workspace; worktree mode creates a `forgeagent/agent-*` branch from `HEAD` and
never merges it automatically. Child registries omit the delegation Tools.
Child exceptions are recorded in task state and converted to observations
instead of crashing the parent.

## Explicit limits

- Background workers are in-process, not durable distributed jobs.
- Worktrees isolate Git changes, not processes or network access.
- File checkpoints do not make shell, Docker, MCP, or network effects reversible.
- The MCP client currently covers Tools only.
- Optional Tool-search embedding usage is not included in model cost accounting.
- Docker protects shell/MCP process execution; it does not sandbox the LLM API.
- JSON token estimation is approximate; the hard budget intentionally keeps a
  safety margin.
- USD budgets are local estimates based on explicitly configured prices.
- Git checkpoints cover direct file and patch Tools; arbitrary Shell commands
  still require an external Git/worktree strategy for transactional rollback.

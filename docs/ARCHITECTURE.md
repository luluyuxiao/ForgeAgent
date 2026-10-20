# Architecture

ForgeAgent separates five decisions that are often collapsed into one Agent
class:

1. `ChatProvider` decides how a model request is transported.
2. `ContextWindow` decides what history fits into the next request.
3. `PolicyPipeline` decides whether a proposed Tool Call is authorized.
4. `BatchScheduler` decides which admitted calls may overlap.
5. `ToolRegistry` validates and executes the selected capability.

`AgentRuntime` coordinates those interfaces but does not implement their policy.

## Request state machine

```text
IDLE
  │ user input
  ▼
PREPARING ── context failure ──► FAILED
  │
  ▼
MODEL_REQUEST
  │
  ├── final text ──────────────► COMPLETE
  │
  └── Tool Calls
          │
          ▼
       ADMISSION
          │ allowed calls
          ▼
       EXECUTION
          │ one observation per call id
          └────────────────────► PREPARING
```

The loop is bounded by `max_rounds`. Tool failures are data; provider and
context failures remain exceptions because silently routing around them would
hide infrastructure problems.

## Tool contract

Each Tool declares:

- model-facing `name`, `description`, and JSON Schema `parameters`;
- an `effect` used by Plan Mode and Permission;
- `capabilities` used by authority policy;
- argument-dependent `resources` used by scheduling;
- whether its implementation is `parallel_safe`.

The model receives only schemas from one `ToolRegistry` instance. Omitting a
Tool is therefore a stronger capability boundary than asking the model not to
call it.

## Policy composition

The default CLI pipeline is:

```text
CapabilityPolicy → PlanningPolicy → PermissionPolicy
```

Each policy returns a structured decision and stops the pipeline on denial.
Planning is based on the declared effect, so newly registered read-only Tools
work without editing a global name whitelist.

## Concurrency

The scheduler builds deterministic waves in model-call order. A Tool with
`parallel_safe = False` is a serial barrier. Parallel-safe calls may share a
wave when no pair of `ResourceClaim` objects conflicts. Results are associated
by Tool Call ID and appended to the conversation in original model order, not
completion order.

## Context and Transcript

The Transcript is append-only. Active Context is a copy used for provider
requests. `ContextWindow.prepare()` computes:

```text
System Prompt
+ Tool Schemas
+ Active Context
+ output reserve
+ safety margin
```

When the input budget is exceeded, older Tool Results are snipped first. The
remaining history is partitioned into protocol blocks so an assistant Tool Call
cannot be retained without all of its following observations. Old blocks are
then collapsed into a marker. The original Transcript is not mutated.

## Provider contract

`OpenAICompatibleProvider` consumes a stream completely before the runtime
executes any Tool Call. Text and fragmented function names/arguments are
reassembled by index. Retry is allowed only before any streamed content or Tool
fragment has been observed, preventing duplicate visible output or ambiguous
execution.

## Session contract

Sessions store:

- full Transcript;
- compacted Active Context;
- current mode;
- model identifier;
- cumulative usage.

They deliberately exclude credentials and permission grants. Writes use a
temporary file, `fsync`, restrictive permissions, and atomic replacement.

## Extension map

| Feature | Extension point |
|---|---|
| Another provider | implement `ChatProvider` |
| Docker execution | replace `ShellTool` with a container-backed Tool |
| Hooks / audit | add a `ToolPolicy` and event sink |
| MCP | discover remote schemas and register proxy `Tool` instances |
| LLM summary | add a summarization strategy to `ContextWindow` |
| Sub-Agent | create child `AgentRuntime` instances with separate state |
| Structured trace | persist `on_event` records |


# ForgeAgent

ForgeAgent is a small coding-agent runtime implemented around explicit
interfaces rather than a monolithic agent class.

It is an independent implementation based on public Tool Calling conventions,
observable coding-agent behavior, and common ReAct patterns. It does not use
non-public or leaked proprietary source code.

## What v0.1 implements

- a bounded ReAct loop with streamed model output and Tool Calling;
- an instance-scoped `ToolRegistry` and JSON Schema generation;
- policy composition for planning, capabilities, and user permission;
- effect/resource-aware tool batches with parallel read execution;
- workspace-confined file tools and a local shell tool;
- separate full Transcript and compactable Active Context;
- deterministic context snipping/collapse with protocol-valid tool blocks;
- atomic JSON Session save/resume;
- token usage accounting exposed by the provider;
- an interactive CLI with `/plan`, `/approve`, `/save`, `/resume`, and `/tokens`.

MCP, Docker isolation, durable background workers, Sub-Agents, and LLM-generated
summaries are extension points, not claimed v0.1 features.

## Architecture

```text
CLI / library
     │
     ▼
AgentRuntime ──► ContextWindow ──► Active Context
     │
     ├────────► ChatProvider ────► text / Tool Calls
     │                                │
     ▼                                ▼
PolicyPipeline ◄────────────── ToolRegistry
     │                                │
     └────────► BatchScheduler ───────┘
                         │
                         ▼
                    Tool.execute
                         │
                         ▼
             Tool Result → next round
```

The runtime owns two histories:

```text
transcript     append-only source of truth for save/replay
active_messages  bounded context sent to the provider
```

Compaction changes only `active_messages`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Put the real provider key in `.env`, then run:

```bash
forgeagent
```

One-shot mode:

```bash
forgeagent -p "inspect this project and explain its architecture"
```

Mutating tools require confirmation. One-shot mode fails closed unless
`--yes` is supplied explicitly.

## Plan Mode

`/plan` changes the runtime mode to `PLANNING`. `PlanningPolicy` then allows
`PURE` and `READ` effects and rejects `WRITE`, `PROCESS`, `NETWORK`, and
`UNKNOWN` effects. The restriction is based on the Tool contract, not a list of
hard-coded tool names.

`/approve` switches back to normal execution and records approval as a user
message. This is read-only planning followed by ReAct execution; it is not a
separate Planner/Executor architecture.

## Safety boundary

File tools resolve paths against the startup workspace and reject traversal or
symlink escape. The shell tool executes on the local host and is therefore not
a security sandbox. It always passes through Permission and Capability policy,
but hostile workloads require a future container executor.

## Test

```bash
pytest -q
ruff check src tests
python -m compileall -q src tests
```

## Design notes

- Models decide; policies authorize; tools execute.
- A denied call becomes an observation so the loop can recover.
- Every assistant Tool Call receives exactly one Tool Result.
- Independent reads may overlap, but stateful effects are serial barriers.
- Credentials are read from the environment and are never serialized.
- The CLI is an adapter; the runtime can be embedded directly.


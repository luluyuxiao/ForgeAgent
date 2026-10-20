# ForgeAgent

[English](README.md) | [简体中文](README.zh-CN.md)

ForgeAgent 是一个紧凑、由策略驱动的 Coding Agent / Agent Harness。它将模型通信、上下文预算、授权、调度、执行、持久化与可观测性拆分到独立接口中，避免把所有逻辑堆积在单一的 Agent 类里。

本项目基于公开的 Tool Calling 与 MCP 规范、可观察到的 Agent 行为以及常见的 ReAct 模式独立实现，不使用任何非公开或泄露的专有源代码。

## 当前能力

- 有边界的 ReAct 循环、流式输出、分片 Tool Call 组装与重试；
- 按顺序进行 Provider 故障转移，并保证流式输出开始后不重放请求；
- 实例级 `ToolRegistry`、延迟加载的 `ToolCatalog` 与 JSON Schema 暴露；
- Permission、Plan Mode 与按 Tool 能力划分的策略；
- 感知副作用和资源的调度：独立读取可并发，写入操作串行执行；
- 限制在工作区内的文件工具，以及本地或强化 Docker Shell 执行；
- Git 状态、差异查看，以及带检查和文件检查点的多文件补丁应用；
- 从 `AGENTS.md` 加载仓库指令，并支持发现目录级作用域指令；
- 有大小限制的结构化任务记忆，保存目标、约束、计划、决策和修改文件；
- 延迟激活的项目级/用户级 Skill，以及受沙箱约束的 Tool 前后置 Hook；
- 通过 `.forgeagent.json` 配置项目自己的 lint/test 命令；
- 通过 stdio 或 HTTP 连接多个 MCP Server，发现并调用 MCP Tool；
- MCP Server 级故障隔离、重连、熔断和 Schema 刷新；
- 兼容传统 MCP 初始化流程和现代无状态 `2026-07-28` 请求；
- 可选使用 Docker 隔离 stdio MCP Server；
- 前台/后台 Sub-Agent，以及共享目录或保留 Git Worktree 两种隔离方式；
- 完整 Transcript 与精简 Active Context、Tool Result 裁剪、LLM 摘要、确定性折叠和硬预算限制；
- 事务化 SQLite Session，兼容读取旧版 JSON Session；
- 基于显式输入/输出 Token 价格的运行时美元预算上限；
- 为 `write_file`、`replace_text` 和补丁操作提供文件 Checkpoint/Undo；
- 默认隐藏敏感元数据的 JSONL Trace，以及可移植的 HTML 轨迹查看器；
- 基于 Manifest 的 Eval Runner，可断言输出、Tool、文件、验证命令、Token 和耗时。

## 架构

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

`transcript` 是用于审计和回放的只追加历史记录；`active_messages` 是发送给模型、受预算限制的上下文视图。上下文压缩只会改变后者。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

在 `.env` 中填写模型服务商的密钥，然后启动交互式 REPL：

```bash
forgeagent
```

单次运行模式无法交互确认，因此默认拒绝有状态的 Tool；只有显式添加 `--yes` 才会授权：

```bash
forgeagent --trace .forgeagent/run.jsonl \
  -p "检查当前项目并解释它的架构"
```

在脚本和 CI 中，可以用 JSONL 格式输出生命周期事件和最终回答：

```bash
forgeagent --json -p "检查当前项目并报告测试失败"
```

常用 REPL 命令包括 `/plan`、`/approve`、`/tools`、`/catalog QUERY`、`/tasks`、`/memory`、`/skills`、`/mcp-refresh`、`/diff`、`/transcript`、`/undo`、`/save ID`、`/resume ID`、`/tokens` 和 `/sessions`。

## 功能示例

### 1. 只读分析一个项目

不添加 `--yes` 时，单次运行不会授权修改文件或启动进程，适合先了解陌生仓库：

```bash
forgeagent --workspace . \
  -p "只读分析这个项目，说明入口、主要模块和一次用户请求的调用链"
```

### 2. 运行检查并修复问题

下面的任务可以读取代码、执行 `.forgeagent.json` 中允许的检查，并修改工作区文件。`--yes` 会预先授权有副作用的 Tool，只应在可信工作区使用：

```bash
forgeagent --workspace . --yes \
  -p "运行项目检查，定位失败原因，完成最小修复后再次验证"
```

完成后可以在交互式 REPL 中使用 `/diff` 查看改动；如果修改由可恢复的文件 Tool 产生，可以用 `/undo` 恢复最近一次检查点。

### 3. 先审查计划，再决定是否执行

```text
$ forgeagent
You > /plan
You (plan) > 为工具权限模块增加单元测试，先分析需要修改哪些文件
You (plan) > /approve
```

Plan Mode 只允许读取类操作；`/approve` 会退出 Plan Mode，并让 Agent 根据刚才的计划开始执行。执行具体写入或命令时，交互式 REPL 仍会按策略请求权限。

### 4. 保存并恢复长任务

```bash
# 启动一个自动保存的命名 Session
forgeagent --session refactor-demo

# 稍后恢复完整对话、任务记忆、Tool 状态和检查点
forgeagent --resume refactor-demo
```

也可以在 REPL 中使用 `/save refactor-demo`、`/sessions` 和 `/resume refactor-demo`。

### 5. 使用 MCP 扩展工具

调用仓库自带的本地项目检查 MCP：

```bash
forgeagent --yes --mcp-config examples/mcp.local.json \
  -p "使用 project-inspector 总结当前项目并查找 TODO、FIXME 和 XXX"
```

查询最新的第三方库文档：

```bash
forgeagent --yes --mcp-config examples/mcp.json \
  -p "使用 Context7 查询 Python 3.13 pathlib 文档，并给出读取文本文件的示例"
```

MCP Tool 的副作用默认为 `UNKNOWN`，因此非交互的 `-p` 模式需要显式授权。运行前应先检查 MCP 配置及其 Server；如果不希望预先授权，可以去掉 `-p` 和 `--yes`，在交互式 REPL 中逐次确认调用。

### 6. 记录并查看一次 Agent 运行

```bash
forgeagent --trace .forgeagent/run.jsonl \
  -p "只读分析 Tool 注册流程"
forgeagent-trace .forgeagent/run.jsonl -o .forgeagent/run.html
```

生成的 HTML 报告可用于查看模型轮次、Tool 调用、耗时和 Token 使用情况。Trace 默认隐藏提示词、源码和 Tool 参数等敏感内容。

## 仓库指令与项目检查

ForgeAgent 会自动将仓库根目录的 `AGENTS.md` 和 `.forgeagent/instructions.md` 加入 System Prompt。它还会列出嵌套的 `AGENTS.md` 路径，让模型在修改对应目录前读取适用的规则。仓库指令不能授予 Tool 权限，也不能削弱运行时策略。

本仓库已经包含可用的 `.forgeagent.json`。在其他仓库中可以复制示例配置：

```bash
cp examples/forgeagent.json .forgeagent.json
```

`run_checks` Tool 只执行该文件中预先配置的 lint/test 命令。与其他进程类 Tool 一样，它仍然需要通过 Plan Mode、能力策略和权限策略。文件搜索会优先使用 `rg`，包含隐藏项目文件、遵守忽略规则，并排除 `.git`。

Git 相关 Tool 包括 `git_status`、`git_diff` 和 `apply_patch`。补丁会先经过 `git apply --check` 检查，只允许访问工作区相对路径，并在应用前通过普通文件检查点机制保存原始状态。

## 任务记忆、Skills、Hooks 与成本

结构化任务记忆独立于可以被压缩的聊天历史，并在每次请求中重新注入。模型可以通过 `memory_update` 维护有大小限制的目标、约束、计划、决策和修改文件列表；用户也可以使用 `/memory`、`/goal`、`/constraint` 和 `/decision` 查看或更新它们。该状态会随 Session 保存，但其中的约束只是语义指导，不是授权边界。

ForgeAgent 会从 `~/.forgeagent/skills/*/SKILL.md` 和 `.forgeagent/skills/*/SKILL.md` 发现 Skill。启动时只展示名称和描述，模型调用 `load_skill` 后才会把完整工作流指令加入上下文。项目 Skill 会覆盖同名用户 Skill，任何 Skill 都不能授予 Tool 或权限。

把 `examples/hooks.json` 复制到 `.forgeagent/hooks.json` 即可配置 `PreToolUse` 和 `PostToolUse` 命令。Hook 通过标准输入接收 JSON，并使用当前选择的本地或 Docker Executor。前置 Hook 只有以状态码 2 退出时才会阻止 Tool；Hook 失败不会授予权限，也不会让 Agent 崩溃。

运行时成本限制要求显式配置价格，ForgeAgent 不会硬编码快速变化的服务商价格。以下数字仅用于演示，请先确认所用路由的最新价格：

```bash
forgeagent --max-cost-usd 1.00 \
  --input-price-per-million 0.27 \
  --output-price-per-million 1.10
```

每次调用模型前，ForgeAgent 会预留估算输入成本和最大输出成本；如果投影结果超过上限，则采用 fail-closed 策略停止请求。

## Provider 故障转移

主路由使用 `FORGE_MODEL`、`FORGE_BASE_URL` 和 `FORGE_API_KEY`。备用路由使用有序 JSON 数组配置，每条路由指定保存凭据的环境变量；凭据本身不会被序列化：

```bash
FORGE_FALLBACKS='[
  {"model":"deepseek-reasoner","base_url":"https://api.deepseek.com","api_key_env":"FORGE_API_KEY"}
]'
```

只有在尚未流式输出任何文本或 Tool Call 分片时才会触发故障转移。一旦已经产生可见输出，ForgeAgent 会抛出 `PartialResponseError`，不会在另一个模型上重放请求，从而避免重复执行操作。

## Docker 沙箱

先构建一次镜像：

```bash
docker build -f Dockerfile.sandbox -t forgeagent-sandbox:latest .
```

在容器中运行 Shell Tool：

```bash
forgeagent --sandbox docker --sandbox-image forgeagent-sandbox:latest
```

该命令默认使用只读根文件系统、移除 Linux Capabilities、启用 `no-new-privileges`、限制 PID/CPU/内存、提供受限的 `/tmp`、使用非 root UID，并设置 `--network none`。所选工作区是唯一可写的挂载目录。Docker 执行失败时会采用 fail-closed 策略，绝不会退回宿主机 Shell。只有任务确实需要网络时才应使用 `--sandbox-network bridge`。

## MCP

仓库自带一个可直接使用的 [Context7 MCP](https://mcpservers.org/zh-CN/servers/context7-mcp-server) 示例。它为 Coding Agent 提供最新、指定版本的库文档和代码示例，不会重复 ForgeAgent 已有的文件与 Git Tool：

```bash
forgeagent --mcp-config examples/mcp.json
```

然后可以输入“使用 Context7 查询 Python 3.13 `pathlib` 文档并给出示例”。模型会先发现并激活 `mcp__context7__resolve-library-id` 与 `mcp__context7__query-docs`，再调用对应工具。Context7 无密钥也可基础使用；若需要更高限额，可从 Context7 Dashboard 获取 API Key，并在自定义配置的 `headers` 中加入 `"Authorization": "Bearer ${CONTEXT7_API_KEY}"`。不要把真实密钥写进仓库。

示例显式使用 `2025-11-25` 初始化流程，因为 Context7 的 Streamable HTTP 端点要求先执行 `initialize`；不能依赖 ForgeAgent 面向无状态 Server 的 HTTP 默认值。

要连接其他 Server，可以复制 `examples/mcp.json` 到被 Git 忽略的 `.forgeagent/mcp.json`，再替换 Server 配置。

仓库还包含一个无需额外依赖的本地 stdio 示例：

```bash
forgeagent --mcp-config examples/mcp.local.json
```

`examples/local_mcp_server.py` 会由 ForgeAgent 自动作为子进程启动，并把当前工作区限制为可读取边界。它提供 `project_summary`（文件、文本行数、测试文件和扩展名统计）与 `find_markers`（扫描 TODO/FIXME/XXX）两个只读工具。可以输入“使用 project-inspector 总结当前项目并查找 TODO”。Server 的 `stdout` 专用于逐行 JSON-RPC，诊断信息只能写入 `stderr`。

MCP Tool 通过 `tools/list` 发现，注册为类似 `mcp__weather__current` 的名称，并且最初不会放入模型 Schema。模型可以使用 `search_tools` 查找它们，再通过 `activate_tools` 暴露选中的 Schema，避免大量无用 Schema 占据上下文。调用使用 `tools/call`，结果以普通 Tool Observation 返回。MCP Tool 的副作用被声明为 `UNKNOWN`，并且仍需通过 Plan Mode、能力策略和权限策略。

单个 Server 故障不会再导致所有 MCP 集成都启动失败。失败的调用不会自动重放，但 Transport 可以在下一次调用前重连；连续失败会触发带冷却时间的熔断器。`/mcp-refresh` 可以按需重连并刷新 Schema，刷新失败的 Server 会保留原有 Tool。同一个 Server 的调用串行执行，不同 Server 之间可以并发。

HTTP 默认使用现代无状态 `2026-07-28` 生命周期；stdio 默认使用广泛部署的 `2025-11-25` initialize/initialized 生命周期。stdio Server 可以设置 `"sandboxImage": "your-mcp-image:tag"`，在 Docker 加固环境中运行。当前客户端实现了 Tool 发现和调用，但尚不支持 MCP Resources、Prompts、Elicitation、OAuth 流程和动态列表变更订阅。

## Sub-Agent 与后台任务

`subagent` Tool 接受 `run_mode=foreground|background` 和 `isolation=shared|worktree`。每个子 Agent 都拥有新的 `AgentRuntime`、Transcript、Active Context、Provider 实例和 Registry。子 Agent 的 Registry 会有意移除 `subagent`，防止递归委派。后台调用会立即返回任务 ID，随后可以通过 `task_status` 或 `/tasks` 查看状态、结果、错误和子 Agent Token 用量。Worker 数量有上限且仅存在于当前进程中，CLI 退出后任务状态不会保留。Worktree 模式会从当前 `HEAD` 创建并保留 `forgeagent/agent-*` 分支和独立检出目录，返回路径供用户检查，但不会自动合并或删除；父工作区中未提交的改动不会被复制。

子 Agent 以非交互方式运行。未指定 `--yes` 时，它们的有状态 Tool 会采用 fail-closed 策略，而不会尝试从 Worker 线程读取第二个终端权限提示。

## 上下文、Session 与撤销

上下文使用混合阈值：输入预算取“上下文窗口减去输出预留与安全余量”和“上下文窗口的指定比例”两者中的较小值。默认比例为 `0.85`，可以通过 `FORGE_CONTEXT_INPUT_RATIO` 或 `--context-input-ratio` 修改；比例必须大于 `0` 且不超过 `1`。例如默认 128K 窗口、8192 输出预留和 1024 安全余量时，比例预算 `108800` 比固定预留预算 `118784` 更严格，因此会在约 85% 时开始压缩。

上下文预算包括 System Prompt、已激活的 Tool Schema、历史记录、输出预留空间和安全边界。空间不足时依次执行：

```text
裁剪旧 Tool Result → 使用 LLM 总结旧消息块 → 确定性折叠消息块
→ 最后手段的内容截断 → 仍超出预算则抛出 ContextLimitError
```

最新的协议消息块会被保留。如果摘要失败，则退回确定性折叠。Session 会在事务化 SQLite 数据库中分别保存完整 Transcript 和已经压缩的 Active Context，同时保存结构化记忆、已激活 Skill、Catalog 状态和检查点。旧版 JSON Session 仍可读取，因此恢复速度快，同时不会破坏审计历史。

在执行可恢复的文件 Tool 前，`CheckpointManager` 会保存原始字节，或者记录文件此前不存在。`/undo` 可以恢复最近一次检查点。任意 Shell 命令和远程 MCP 调用的副作用无法被可靠推断，因此不会被标记为可撤销。

## Trace 与 Eval

JSONL Trace 默认会隐藏敏感内容：

```bash
forgeagent --trace .forgeagent/run.jsonl
# 只有在允许文件包含源码、提示词和 Tool 参数时才显式开启：
forgeagent --trace .forgeagent/run.jsonl --trace-content
```

可以把 Trace 渲染为独立 HTML 报告，用于 CI 附件或在浏览器中直接查看：

```bash
forgeagent-trace .forgeagent/run.jsonl -o .forgeagent/run.html
```

在工作区的隔离副本中运行示例 Eval Manifest：

```bash
forgeagent-eval examples/eval.json --workspace . --output eval-report.json
```

Eval 默认不允许交互，并拒绝修改型 Tool。只有信任 Manifest 时才应添加 `--yes`；所有修改仍发生在临时工作区副本中。用例可以断言必需或禁止的 Tool、输出包含或排除的内容、最大 Tool 调用次数与 Token 数、文件内容、持续时间以及运行后的 `test_command`。报告会汇总 Token 和耗时，并包含每个用例的验证输出。可选的每百万输入/输出 Token 价格可以生成可复现的预估成本字段，ForgeAgent 本身不会内置快速变化的服务商价格。

运行后的检查命令会使用与 Agent 相同的本地或 Docker Executor。Manifest 属于可执行输入，运行前仍应进行审查。

## Plan Mode 与安全边界

`/plan` 只允许副作用为 `PURE` 和 `READ` 的 Tool。`memory_update` 等内部 `STATE` 修改在普通模式下不需要外部变更授权，但在 Plan Mode 中仍会被拒绝。`/approve` 会恢复普通 ReAct 执行；Plan Mode 并不是独立的 Planner/Executor 图。Permission 决定模型提出的操作是否获得授权，Docker 决定已授权进程实际能够访问什么。Prompt 指令可以指导行为，但既不是权限边界，也不是沙箱。

交互模式中的“始终允许”只在当前 Session 有效，并且会限定到完全相同的命令、文件路径或调用参数，而不是授权整个 Tool 类别。

## 测试

```bash
pytest -q
ruff check src tests examples
python -m compileall -q src tests examples
```

离线测试套件不需要 API Key、Docker Daemon 或真实 MCP Server。它使用 Fake 验证协议路由与命令构造。

## 致谢

ForgeAgent 的代码为独立实现，其设计过程参考了公开的 Coding Agent 项目与协议，包括 [CoreCoder](https://github.com/he-yufeng/CoreCoder)、aider、SWE-agent、mini-swe-agent、OpenHands、OpenAI 兼容 Tool Calling 和 MCP。

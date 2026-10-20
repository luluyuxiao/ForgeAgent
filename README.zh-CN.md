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
- 通过 `.forgeagent.json` 配置项目自己的 lint/test 命令；
- 通过 stdio 或 HTTP 连接多个 MCP Server，发现并调用 MCP Tool；
- 兼容传统 MCP 初始化流程和现代无状态 `2026-07-28` 请求；
- 可选使用 Docker 隔离 stdio MCP Server；
- 前台 Sub-Agent 与有并发上限的进程内后台 Worker；
- 完整 Transcript 与精简 Active Context、Tool Result 裁剪、LLM 摘要、确定性折叠和硬预算限制；
- 原子化 Session 保存与恢复，包括 Active Context、Catalog 状态和 Undo 数据；
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

常用 REPL 命令包括 `/plan`、`/approve`、`/tools`、`/catalog QUERY`、`/tasks`、`/undo`、`/save ID`、`/resume ID`、`/tokens` 和 `/sessions`。

## 仓库指令与项目检查

ForgeAgent 会自动将仓库根目录的 `AGENTS.md` 和 `.forgeagent/instructions.md` 加入 System Prompt。它还会列出嵌套的 `AGENTS.md` 路径，让模型在修改对应目录前读取适用的规则。仓库指令不能授予 Tool 权限，也不能削弱运行时策略。

将示例检查配置复制到仓库中：

```bash
cp examples/forgeagent.json .forgeagent.json
```

`run_checks` Tool 只执行该文件中预先配置的 lint/test 命令。与其他进程类 Tool 一样，它仍然需要通过 Plan Mode、能力策略和权限策略。文件搜索会优先使用 `rg`，包含隐藏项目文件、遵守忽略规则，并排除 `.git`。

Git 相关 Tool 包括 `git_status`、`git_diff` 和 `apply_patch`。补丁会先经过 `git apply --check` 检查，只允许访问工作区相对路径，并在应用前通过普通文件检查点机制保存原始状态。

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

复制并配置 `examples/mcp.json` 中的一个或多个 Server，然后运行：

```bash
forgeagent --mcp-config .forgeagent/mcp.json
```

MCP Tool 通过 `tools/list` 发现，注册为类似 `mcp__weather__current` 的名称，并且最初不会放入模型 Schema。模型可以使用 `search_tools` 查找它们，再通过 `activate_tools` 暴露选中的 Schema，避免大量无用 Schema 占据上下文。调用使用 `tools/call`，结果以普通 Tool Observation 返回。MCP Tool 的副作用被声明为 `UNKNOWN`，并且仍需通过 Plan Mode、能力策略和权限策略。

HTTP 默认使用现代无状态 `2026-07-28` 生命周期；stdio 默认使用广泛部署的 `2025-11-25` initialize/initialized 生命周期。stdio Server 可以设置 `"sandboxImage": "your-mcp-image:tag"`，在 Docker 加固环境中运行。当前客户端实现了 Tool 发现和调用，但尚不支持 MCP Resources、Prompts、Elicitation、OAuth 流程和动态列表变更订阅。

## Sub-Agent 与后台任务

`subagent` Tool 接受 `run_mode=foreground|background`。每个子 Agent 都拥有新的 `AgentRuntime`、Transcript、Active Context、Provider 实例和 Registry。子 Agent 的 Registry 会有意移除 `subagent`，防止递归委派。后台调用会立即返回任务 ID，随后可以通过 `task_status` 或 `/tasks` 查看状态、结果、错误和子 Agent Token 用量。Worker 数量有上限且仅存在于当前进程中，CLI 退出后任务状态不会保留。

子 Agent 以非交互方式运行。未指定 `--yes` 时，它们的有状态 Tool 会采用 fail-closed 策略，而不会尝试从 Worker 线程读取第二个终端权限提示。

## 上下文、Session 与撤销

上下文预算包括 System Prompt、已激活的 Tool Schema、历史记录、输出预留空间和安全边界。空间不足时依次执行：

```text
裁剪旧 Tool Result → 使用 LLM 总结旧消息块 → 确定性折叠消息块
→ 最后手段的内容截断 → 仍超出预算则抛出 ContextLimitError
```

最新的协议消息块会被保留。如果摘要失败，则退回确定性折叠。Session 会分别保存完整 Transcript 和已经压缩的 Active Context，因此恢复速度快，同时不会破坏审计历史。

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

`/plan` 只允许副作用为 `PURE` 和 `READ` 的 Tool。`/approve` 会恢复普通 ReAct 执行；Plan Mode 并不是独立的 Planner/Executor 图。Permission 决定模型提出的操作是否获得授权，Docker 决定已授权进程实际能够访问什么。Prompt 指令可以指导行为，但既不是权限边界，也不是沙箱。

交互模式中的“始终允许”只在当前 Session 有效，并且会限定到完全相同的命令、文件路径或调用参数，而不是授权整个 Tool 类别。

## 测试

```bash
pytest -q
ruff check src tests
python -m compileall -q src tests
```

离线测试套件不需要 API Key、Docker Daemon 或真实 MCP Server。它使用 Fake 验证协议路由与命令构造。

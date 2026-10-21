"""Terminal adapter for ForgeAgent."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .factory import RuntimeOptions, create_runtime
from .policies import RunMode
from .runtime import AgentRuntime
from .session import SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ForgeAgent local coding-agent runtime")
    parser.add_argument("-p", "--prompt", help="run one prompt and exit")
    parser.add_argument(
        "--json", action="store_true", help="emit JSONL events (requires --prompt)"
    )
    parser.add_argument("--workspace", default=".", help="workspace root")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--context-tokens", type=int)
    parser.add_argument("--context-input-ratio", type=float)
    parser.add_argument("--session-dir")
    parser.add_argument("--session", help="autosave under this session id")
    parser.add_argument("--resume", help="resume a saved session")
    parser.add_argument("--yes", action="store_true", help="pre-approve stateful tools")
    parser.add_argument("--sandbox", choices=["local", "docker"])
    parser.add_argument("--sandbox-image")
    parser.add_argument("--sandbox-network", choices=["none", "bridge"])
    parser.add_argument("--mcp-config", help="JSON file describing MCP servers")
    parser.add_argument("--trace", help="append runtime events to this JSONL file")
    parser.add_argument("--hooks-config", help="JSON file describing pre/post Tool hooks")
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    parser.add_argument("--tool-embedding-model")
    parser.add_argument("--tool-embedding-base-url")
    parser.add_argument(
        "--trace-content", action="store_true", help="include prompts/arguments in trace"
    )
    parser.add_argument("--no-summary", action="store_true", help="disable LLM context summary")
    parser.add_argument("--no-subagents", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings_with_args(Settings.from_env(), args)
    if args.json and args.prompt is None:
        print("--json requires --prompt", file=sys.stderr)
        return 2
    if not settings.api_key:
        print("FORGE_API_KEY is not set. Copy .env.example to .env and add a key.", file=sys.stderr)
        return 2
    if not settings.model:
        print("FORGE_MODEL is not set.", file=sys.stderr)
        return 2

    interactive = args.prompt is None
    try:
        bundle = create_runtime(
            settings,
            RuntimeOptions(
                workspace=Path(args.workspace),
                interactive=interactive,
                allow_all=bool(args.yes),
                ask_permission=_ask_permission,
                enable_subagents=not args.no_subagents,
                trace_content=args.trace_content,
            ),
        )
    except Exception as error:  # noqa: BLE001 - configuration boundary reports all failures
        print(f"Startup failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    runtime = bundle.runtime
    store = SessionStore(settings.session_dir)
    session_id = args.resume or args.session
    if args.resume:
        try:
            runtime.load(store, args.resume)
        except (KeyError, ValueError, OSError) as error:
            print(f"Cannot resume session: {error}", file=sys.stderr)
            bundle.close()
            return 2

    if args.prompt is not None:
        try:
            result = _run_json(runtime, args.prompt) if args.json else _run_and_render(
                runtime, args.prompt
            )
            if session_id:
                runtime.save(store, session_id)
            return 0 if result else 1
        finally:
            bundle.close()
    try:
        return _repl(runtime, store, session_id)
    finally:
        bundle.close()


def _settings_with_args(settings: Settings, args) -> Settings:
    return replace(
        settings,
        model=args.model or settings.model,
        base_url=args.base_url or settings.base_url,
        max_rounds=args.max_rounds or settings.max_rounds,
        context_tokens=args.context_tokens or settings.context_tokens,
        context_input_ratio=(
            args.context_input_ratio
            if args.context_input_ratio is not None
            else settings.context_input_ratio
        ),
        session_dir=(Path(args.session_dir).expanduser() if args.session_dir else settings.session_dir),
        sandbox=args.sandbox or settings.sandbox,
        sandbox_image=args.sandbox_image or settings.sandbox_image,
        sandbox_network=args.sandbox_network or settings.sandbox_network,
        mcp_config=(Path(args.mcp_config).expanduser() if args.mcp_config else settings.mcp_config),
        trace_path=(Path(args.trace).expanduser() if args.trace else settings.trace_path),
        llm_summary=False if args.no_summary else settings.llm_summary,
        hooks_config=(
            Path(args.hooks_config).expanduser() if args.hooks_config else settings.hooks_config
        ),
        max_cost_usd=(
            args.max_cost_usd if args.max_cost_usd is not None else settings.max_cost_usd
        ),
        input_price_per_million=(
            args.input_price_per_million
            if args.input_price_per_million is not None
            else settings.input_price_per_million
        ),
        output_price_per_million=(
            args.output_price_per_million
            if args.output_price_per_million is not None
            else settings.output_price_per_million
        ),
        tool_embedding_model=(
            args.tool_embedding_model or settings.tool_embedding_model
        ),
        tool_embedding_base_url=(
            args.tool_embedding_base_url or settings.tool_embedding_base_url
        ),
    )


def _repl(runtime: AgentRuntime, store: SessionStore, session_id: str | None) -> int:
    print(
        f"ForgeAgent  model={runtime.provider.model}  workspace={runtime.workspace}\n"
        "Type /help for commands."
    )
    current_session = session_id
    while True:
        prompt = "You (plan) > " if runtime.mode is RunMode.PLANNING else "You > "
        try:
            user_input = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not user_input:
            continue
        lowered = user_input.lower()
        if lowered in {"quit", "exit", "/quit", "/exit"}:
            return 0
        if lowered == "/help":
            print(_HELP)
            continue
        if lowered == "/plan":
            runtime.set_mode(
                RunMode.NORMAL if runtime.mode is RunMode.PLANNING else RunMode.PLANNING
            )
            print(f"Mode: {runtime.mode.value}")
            continue
        if lowered in {"/approve", "approve"} and runtime.mode is RunMode.PLANNING:
            runtime.approve_plan()
            user_input = "The user approved the proposed plan. Execute it now."
        elif lowered == "/reset":
            runtime.reset()
            print("Conversation reset.")
            continue
        elif lowered == "/tokens":
            usage = runtime.provider.total_usage
            budget = (
                f"${runtime.budget.max_usd:.6f}"
                if runtime.budget.enabled
                else "disabled"
            )
            print(
                f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
                f"total={usage.total_tokens} cost=${runtime.estimated_cost_usd:.6f} "
                f"budget={budget}"
            )
            continue
        elif lowered == "/memory":
            print(runtime.memory.render() or "[empty task memory]")
            continue
        elif lowered.startswith("/goal "):
            try:
                value = runtime.memory.set_goal(user_input.split(maxsplit=1)[1])
            except ValueError as error:
                print(f"Goal rejected: {error}")
            else:
                print(json.dumps(value, ensure_ascii=False))
            continue
        elif lowered.startswith("/constraint "):
            try:
                value = runtime.memory.add_constraint(user_input.split(maxsplit=1)[1])
            except ValueError as error:
                print(f"Constraint rejected: {error}")
            else:
                print(json.dumps(value, ensure_ascii=False))
            continue
        elif lowered.startswith("/decision "):
            try:
                value = runtime.memory.add_decision(
                    user_input.split(maxsplit=1)[1], reason="user"
                )
            except ValueError as error:
                print(f"Decision rejected: {error}")
            else:
                print(json.dumps(value, ensure_ascii=False))
            continue
        elif lowered == "/skills":
            print(runtime.skills.catalog() or "[no skills]")
            continue
        elif lowered == "/mcp-refresh":
            print(runtime.refresh_mcp())
            continue
        elif lowered == "/diff":
            tool = runtime.registry.get("git_diff")
            print(tool.execute() if tool else "git_diff is unavailable.")
            continue
        elif lowered == "/transcript":
            print(json.dumps(runtime.transcript, ensure_ascii=False, indent=2))
            continue
        elif lowered == "/tools":
            active = set(runtime.catalog.names()) if runtime.catalog else set(runtime.registry.names())
            print(
                "\n".join(
                    f"{'*' if name in active else '-'} {name}" for name in runtime.registry.names()
                )
            )
            continue
        elif lowered.startswith("/catalog "):
            query = user_input.split(maxsplit=1)[1]
            rows = runtime.catalog.search(query) if runtime.catalog else []
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            continue
        elif lowered == "/tasks":
            tool = runtime.registry.get("task_status")
            print(tool.execute() if tool else "Sub-Agents are disabled.")
            continue
        elif lowered == "/undo":
            print(runtime.undo())
            continue
        elif lowered == "/sessions":
            rows = store.list()
            print("\n".join(f"{sid}\t{updated}" for sid, updated in rows) or "[no sessions]")
            continue
        elif lowered.startswith("/save"):
            parts = user_input.split(maxsplit=1)
            target = parts[1] if len(parts) == 2 else current_session
            if not target:
                print("Usage: /save <session-id>")
                continue
            try:
                path = runtime.save(store, target)
            except (ValueError, OSError) as error:
                print(f"Save failed: {error}")
                continue
            current_session = target
            print(f"Saved: {path}")
            continue
        elif lowered.startswith("/resume "):
            target = user_input.split(maxsplit=1)[1]
            try:
                runtime.load(store, target)
            except (KeyError, ValueError, OSError) as error:
                print(f"Resume failed: {error}")
                continue
            current_session = target
            print(f"Resumed: {target}")
            continue

        try:
            _run_and_render(runtime, user_input)
            if current_session:
                runtime.save(store, current_session)
        except Exception as error:  # noqa: BLE001 - keep the interactive REPL alive
            print(f"Run failed: {type(error).__name__}: {error}", file=sys.stderr)


def _run_and_render(runtime: AgentRuntime, prompt: str) -> str:
    streamed = False

    def on_text(token: str) -> None:
        nonlocal streamed
        streamed = True
        print(token, end="", flush=True)

    def on_event(event: str, fields: dict) -> None:
        if event == "tool_started":
            print(f"\n> {fields['tool']}", flush=True)
        elif event == "context_compacted":
            print(
                f"\n[context {fields['before_tokens']} → {fields['after_tokens']} tokens]",
                flush=True,
            )

    result = runtime.run(prompt, on_text=on_text, on_event=on_event)
    if streamed:
        print()
    elif result:
        print(result)
    return result


def _run_json(runtime: AgentRuntime, prompt: str) -> str:
    outcome = "completed"

    def on_event(event: str, fields: dict) -> None:
        nonlocal outcome
        if event == "run_limited":
            outcome = "limited"
        print(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str), flush=True)

    result = runtime.run(prompt, on_event=on_event)
    usage = runtime.provider.total_usage
    print(
        json.dumps(
            {
                "event": "final",
                "outcome": outcome,
                "content": result,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "estimated_cost_usd": runtime.estimated_cost_usd,
                "max_cost_usd": runtime.budget.max_usd,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


def _ask_permission(tool_name: str, arguments: dict) -> str:
    rendered = json.dumps(arguments, ensure_ascii=False)
    if len(rendered) > 300:
        rendered = rendered[:300] + "..."
    print(f"\nPermission requested: {tool_name}({rendered})")
    while True:
        answer = input("[y] once  [a] always this exact action  [n] deny: ").strip().lower()
        if answer in {"y", "yes"}:
            return "once"
        if answer in {"a", "always"}:
            return "always"
        if answer in {"n", "no", "deny"}:
            return "deny"


_HELP = """\
/help                 show this help
/plan                 toggle read-only planning
/approve              approve the plan and execute
/tokens               show provider token usage
/memory               show structured task memory
/goal <text>          set the durable task goal
/constraint <text>    add a durable user constraint
/decision <text>      record a durable decision
/skills               list discovered and active Skills
/mcp-refresh          reconnect and refresh MCP Tool schemas
/diff                 show the current Git diff
/transcript           show uncompacted conversation history
/tools                list registered Tools
/catalog <query>      search active and hidden Tools
/tasks                show background Sub-Agent tasks
/undo                 restore files from the latest checkpoint
/sessions             list saved sessions
/save <id>            save the current session
/resume <id>          restore a session
/reset                clear conversation state
/quit                 exit
"""


if __name__ == "__main__":
    raise SystemExit(main())

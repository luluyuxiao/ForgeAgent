"""Terminal adapter for ForgeAgent."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .context import ContextWindow
from .policies import (
    CapabilityPolicy,
    PermissionPolicy,
    PlanningPolicy,
    PolicyPipeline,
    RunMode,
)
from .providers import OpenAICompatibleProvider
from .registry import ToolRegistry
from .runtime import AgentRuntime
from .session import SessionStore
from .tools import build_builtin_tools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ForgeAgent local coding-agent runtime")
    parser.add_argument("-p", "--prompt", help="run one prompt and exit")
    parser.add_argument("--workspace", default=".", help="workspace root")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--context-tokens", type=int)
    parser.add_argument("--session-dir")
    parser.add_argument("--session", help="autosave under this session id")
    parser.add_argument("--resume", help="resume a saved session")
    parser.add_argument("--yes", action="store_true", help="pre-approve stateful tools")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings_with_args(Settings.from_env(), args)
    if not settings.api_key:
        print("FORGE_API_KEY is not set. Copy .env.example to .env and add a key.", file=sys.stderr)
        return 2
    if not settings.model:
        print("FORGE_MODEL is not set.", file=sys.stderr)
        return 2

    interactive = args.prompt is None
    runtime = _create_runtime(settings, args, interactive=interactive)
    store = SessionStore(settings.session_dir)
    session_id = args.resume or args.session
    if args.resume:
        try:
            runtime.load(store, args.resume)
        except (KeyError, ValueError, OSError) as error:
            print(f"Cannot resume session: {error}", file=sys.stderr)
            return 2

    if args.prompt is not None:
        result = _run_and_render(runtime, args.prompt)
        if session_id:
            runtime.save(store, session_id)
        return 0 if result else 1
    return _repl(runtime, store, session_id)


def _settings_with_args(settings: Settings, args) -> Settings:
    return replace(
        settings,
        model=args.model or settings.model,
        base_url=args.base_url or settings.base_url,
        max_rounds=args.max_rounds or settings.max_rounds,
        context_tokens=args.context_tokens or settings.context_tokens,
        session_dir=(Path(args.session_dir).expanduser() if args.session_dir else settings.session_dir),
    )


def _create_runtime(settings: Settings, args, *, interactive: bool) -> AgentRuntime:
    workspace = Path(args.workspace).expanduser().resolve()
    registry = ToolRegistry(build_builtin_tools(workspace))
    permission = PermissionPolicy(
        _ask_permission if interactive else None,
        allow_all=bool(args.yes),
    )
    policies = PolicyPipeline(
        [CapabilityPolicy(), PlanningPolicy(), permission]
    )
    provider = OpenAICompatibleProvider(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        max_output_tokens=settings.max_output_tokens,
    )
    context = ContextWindow(
        settings.context_tokens,
        output_reserve=settings.max_output_tokens,
    )
    return AgentRuntime(
        provider=provider,
        registry=registry,
        policies=policies,
        context=context,
        max_rounds=settings.max_rounds,
        workspace=workspace,
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
            print(
                f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
                f"total={usage.total_tokens}"
            )
            continue
        elif lowered == "/tools":
            print("\n".join(runtime.registry.names()))
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


def _ask_permission(tool_name: str, arguments: dict) -> str:
    rendered = json.dumps(arguments, ensure_ascii=False)
    if len(rendered) > 300:
        rendered = rendered[:300] + "..."
    print(f"\nPermission requested: {tool_name}({rendered})")
    while True:
        answer = input("[y] once  [a] always this Tool  [n] deny: ").strip().lower()
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
/tools                list registered Tools
/sessions             list saved sessions
/save <id>            save the current session
/resume <id>          restore a session
/reset                clear conversation state
/quit                 exit
"""


if __name__ == "__main__":
    raise SystemExit(main())

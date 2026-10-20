"""The bounded ReAct runtime that composes providers, policies, and tools."""

from __future__ import annotations

import copy
import threading
import uuid
from pathlib import Path

from .context import ContextWindow
from .contracts import ChatProvider, EventCallback, Message, TokenCallback, ToolCall, Usage
from .policies import PolicyContext, PolicyPipeline, RunMode
from .registry import ToolRegistry
from .scheduler import BatchScheduler
from .session import SessionState, SessionStore


class AgentRuntime:
    def __init__(
        self,
        *,
        provider: ChatProvider,
        registry: ToolRegistry,
        policies: PolicyPipeline,
        context: ContextWindow | None = None,
        scheduler: BatchScheduler | None = None,
        max_rounds: int = 24,
        workspace: str | Path = ".",
    ):
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        self.provider = provider
        self.registry = registry
        self.policies = policies
        self.context = context or ContextWindow()
        self.scheduler = scheduler or BatchScheduler(registry)
        self.max_rounds = max_rounds
        self.workspace = Path(workspace).expanduser().resolve()
        self.mode = RunMode.NORMAL
        self.transcript: list[Message] = []
        self.active_messages: list[Message] = []
        self._run_lock = threading.RLock()

    def run(
        self,
        user_input: str,
        *,
        on_text: TokenCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> str:
        if not user_input.strip():
            raise ValueError("user input must not be empty")
        with self._run_lock:
            run_id = uuid.uuid4().hex
            self._append({"role": "user", "content": user_input})
            _emit(on_event, "run_started", run_id=run_id, mode=self.mode.value)

            for round_number in range(1, self.max_rounds + 1):
                schemas = self.registry.schemas()
                prepared = self.context.prepare(
                    self._system_prompt(), self.active_messages, schemas
                )
                self.active_messages = prepared.active_messages
                if prepared.report.before_tokens != prepared.report.after_tokens:
                    _emit(
                        on_event,
                        "context_compacted",
                        before_tokens=prepared.report.before_tokens,
                        after_tokens=prepared.report.after_tokens,
                        snipped=prepared.report.snipped_tool_results,
                        collapsed=prepared.report.collapsed_messages,
                    )

                _emit(on_event, "model_started", round=round_number)
                reply = self.provider.complete(
                    prepared.request_messages,
                    schemas,
                    on_text=on_text,
                )
                _emit(
                    on_event,
                    "model_completed",
                    round=round_number,
                    tool_calls=len(reply.tool_calls),
                    prompt_tokens=reply.usage.prompt_tokens,
                    completion_tokens=reply.usage.completion_tokens,
                )
                self._append(reply.as_message())

                if not reply.tool_calls:
                    _emit(on_event, "run_completed", run_id=run_id, rounds=round_number)
                    return reply.content

                observations = self._execute_calls(reply.tool_calls, on_event)
                for call in reply.tool_calls:
                    self._append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": observations[call.id],
                        }
                    )

            message = f"Stopped after reaching the {self.max_rounds}-round limit."
            self._append({"role": "assistant", "content": message})
            _emit(on_event, "run_limited", run_id=run_id, rounds=self.max_rounds)
            return message

    def _execute_calls(
        self,
        calls: list[ToolCall],
        on_event: EventCallback | None,
    ) -> dict[str, str]:
        observations: dict[str, str] = {}
        admitted: list[ToolCall] = []
        policy_context = PolicyContext(mode=self.mode)

        for call in calls:
            if call.parse_error:
                observations[call.id] = f"Error: {call.parse_error}"
                continue
            tool = self.registry.get(call.name)
            if tool is None:
                observations[call.id] = f"Error: unknown Tool {call.name!r}"
                continue
            decision = self.policies.evaluate(call, tool, policy_context)
            _emit(
                on_event,
                "tool_decided",
                call_id=call.id,
                tool=call.name,
                allowed=decision.allowed,
                policy=decision.policy,
                code=decision.code,
            )
            if decision.allowed:
                admitted.append(call)
            else:
                observations[call.id] = decision.reason

        if admitted:
            observations.update(self.scheduler.execute(admitted, on_event=on_event))
        return observations

    def set_mode(self, mode: RunMode) -> None:
        self.mode = mode

    def approve_plan(self) -> None:
        self.mode = RunMode.NORMAL

    def reset(self) -> None:
        with self._run_lock:
            self.transcript.clear()
            self.active_messages.clear()
            self.mode = RunMode.NORMAL

    def snapshot(self, session_id: str) -> SessionState:
        usage = getattr(self.provider, "total_usage", Usage())
        return SessionState(
            session_id=session_id,
            model=self.provider.model,
            mode=self.mode,
            transcript=copy.deepcopy(self.transcript),
            active_messages=copy.deepcopy(self.active_messages),
            usage=usage,
        )

    def restore(self, state: SessionState) -> None:
        with self._run_lock:
            self.transcript = copy.deepcopy(state.transcript)
            self.active_messages = copy.deepcopy(state.active_messages)
            self.mode = state.mode
            if hasattr(self.provider, "total_usage"):
                self.provider.total_usage = state.usage

    def save(self, store: SessionStore, session_id: str) -> Path:
        return store.save(self.snapshot(session_id))

    def load(self, store: SessionStore, session_id: str) -> None:
        self.restore(store.load(session_id))

    def _append(self, message: Message) -> None:
        self.transcript.append(copy.deepcopy(message))
        self.active_messages.append(copy.deepcopy(message))

    def _system_prompt(self) -> str:
        tools = "\n".join(
            f"- {name}: {self.registry.require(name).description}"
            for name in self.registry.names()
        )
        planning = ""
        if self.mode is RunMode.PLANNING:
            planning = (
                "\n\n# Planning mode\n"
                "Investigate using read-only tools. Present a numbered plan and stop. "
                "Do not attempt side effects until the user approves."
            )
        return (
            "You are ForgeAgent, a coding assistant operating in a local workspace.\n"
            f"Workspace: {self.workspace}\n\n"
            f"# Available tools\n{tools}\n\n"
            "# Working rules\n"
            "1. Inspect relevant files before changing them.\n"
            "2. Prefer focused replacements over complete rewrites.\n"
            "3. Verify changes with relevant tests or commands.\n"
            "4. Treat Tool errors as observations and adapt.\n"
            "5. Stop when the task is complete."
            + planning
        )


def _emit(callback: EventCallback | None, event: str, **fields) -> None:
    if callback is None:
        return
    try:
        callback(event, fields)
    except Exception:  # noqa: BLE001, S110 - presentation callbacks cannot fail the runtime
        pass

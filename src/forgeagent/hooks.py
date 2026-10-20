"""Sandbox-aware pre/post Tool hooks."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox import CommandExecutor


@dataclass(frozen=True)
class Hook:
    matcher: str
    command: str


class ToolHooks:
    def __init__(
        self,
        pre: list[Hook],
        post: list[Hook],
        *,
        executor: CommandExecutor,
        workspace: Path,
        timeout: int = 10,
    ):
        self.pre = pre
        self.post = post
        self.executor = executor
        self.workspace = workspace
        self.timeout = timeout

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        executor: CommandExecutor,
        workspace: Path,
    ) -> ToolHooks:
        target = Path(path).expanduser()
        if not target.is_file():
            return cls([], [], executor=executor, workspace=workspace)
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("hook configuration must be an object")
        return cls(
            _parse_hooks(data.get("PreToolUse") or []),
            _parse_hooks(data.get("PostToolUse") or []),
            executor=executor,
            workspace=workspace,
        )

    def run_pre(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        payload = {"tool_name": tool_name, "tool_input": arguments}
        for hook in self.pre:
            result = self._run(hook, tool_name, payload)
            if result is not None and result.returncode == 2:
                return "Blocked by hook: " + (result.stderr.strip() or "no reason given")
        return None

    def run_post(self, tool_name: str, arguments: dict[str, Any], result: str) -> None:
        payload = {"tool_name": tool_name, "tool_input": arguments, "tool_response": result}
        for hook in self.post:
            self._run(hook, tool_name, payload)

    def _run(self, hook: Hook, tool_name: str, payload: dict[str, Any]):
        if not fnmatch.fnmatchcase(tool_name, hook.matcher or "*"):
            return None
        try:
            return self.executor.execute(
                hook.command,
                self.workspace,
                self.timeout,
                stdin=json.dumps(payload, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001 - configured hooks cannot fail the Agent loop
            return None


def _parse_hooks(raw: Any) -> list[Hook]:
    if not isinstance(raw, list):
        raise TypeError("hooks must be arrays")
    hooks = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("command") or "").strip():
            raise TypeError("each hook requires a command")
        hooks.append(Hook(str(item.get("matcher") or "*"), str(item["command"])))
    return hooks

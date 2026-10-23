"""Configured check and sandboxed shell Tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from ..sandbox import CommandExecutor, LocalExecutor
from .base import Access, ResourceClaim, Tool, ToolEffect, bound_output
from .filesystem import Workspace


class RunChecksTool(Tool):
    name = "run_checks"
    description = (
        "Run trusted lint/test commands declared in .forgeagent.json and return all exit "
        "codes. Docker-isolated commands reuse the current runtime's temporary workspace."
    )
    effect = ToolEffect.PROCESS
    capabilities = frozenset({"process", "filesystem_read", "filesystem_write", "network"})
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["all", "lint", "test"], "default": "all"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 600, "default": 300},
        },
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, executor: CommandExecutor | None = None):
        self.workspace = workspace
        self.executor = executor or LocalExecutor()
        capabilities = {"process", "filesystem_read", "filesystem_write"}
        if not hasattr(self.executor, "network") or self.executor.network != "none":
            capabilities.add("network")
        self.capabilities = frozenset(capabilities)

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("workspace:*", Access.WRITE),)

    def execute(self, kind: str = "all", timeout: int = 300) -> str:
        commands = _load_check_commands(self.workspace.root, kind)
        if not commands:
            return f"Error: no {kind} checks configured in .forgeagent.json"
        results = []
        for name, command in commands:
            try:
                completed = self.executor.execute(command, self.workspace.root, timeout)
            except TimeoutError:
                results.append({"name": name, "command": command, "exit_code": None, "error": "timeout"})
                break
            except Exception as error:  # noqa: BLE001 - backend errors are observations
                results.append(
                    {
                        "name": name,
                        "command": command,
                        "exit_code": None,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                break
            results.append(
                {
                    "name": name,
                    "command": command,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout[-8000:],
                    "stderr": completed.stderr[-8000:],
                    "workspace_mode": completed.workspace_mode,
                    "sandbox_session_id": completed.sandbox_session_id or None,
                    "command_changes": list(completed.command_changes),
                    "workspace_changes": list(completed.workspace_changes),
                    "change_set_id": completed.change_set_id or None,
                    "change_set_error": completed.change_set_error or None,
                    "session_conflicts": list(completed.session_conflicts),
                }
            )
            if completed.returncode != 0:
                break
        return bound_output(
            json.dumps(
                {"passed": all(row.get("exit_code") == 0 for row in results), "results": results},
                ensure_ascii=False,
                indent=2,
            )
        )


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a non-interactive shell command using the configured execution backend. "
        "Docker-isolated calls reuse a runtime-scoped temporary workspace so later commands "
        "see earlier files, but shell state and processes do not persist; review and explicitly "
        "apply the cumulative change set to persist it."
    )
    effect = ToolEffect.PROCESS
    capabilities = frozenset({"process", "filesystem_read", "filesystem_write", "network"})
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 120},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, executor: CommandExecutor | None = None):
        self.workspace = workspace
        self.executor = executor or LocalExecutor()
        capabilities = {"process", "filesystem_read", "filesystem_write"}
        if not hasattr(self.executor, "network") or self.executor.network != "none":
            capabilities.add("network")
        self.capabilities = frozenset(capabilities)

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("workspace:*", Access.WRITE),)

    def execute(self, command: str, timeout: int = 120) -> str:
        if not command.strip():
            return "Error: command must not be empty"
        try:
            completed = self.executor.execute(command, self.workspace.root, timeout)
        except TimeoutError:
            return f"Error: command timed out after {timeout} seconds"
        except Exception as error:  # noqa: BLE001 - backend errors are observations
            return f"Error: {self.executor.name} executor failed: {type(error).__name__}: {error}"
        rendered = (
            f"exit_code={completed.returncode}\n"
            f"workspace_mode={completed.workspace_mode}\n"
            f"sandbox_session={completed.sandbox_session_id or '[none]'}\n"
            f"stdout:\n{completed.stdout or '[empty]'}\n"
            f"stderr:\n{completed.stderr or '[empty]'}\n"
            "command_changes:\n"
            + (
                "\n".join(completed.command_changes)
                if completed.command_changes
                else "[none]"
            )
            + "\ncumulative_workspace_changes:\n"
            + (
                "\n".join(completed.workspace_changes)
                if completed.workspace_changes
                else "[none]"
            )
            + (
                "\nsession_conflicts:\n" + "\n".join(completed.session_conflicts)
                if completed.session_conflicts
                else ""
            )
            + (
                f"\npending_change_set={completed.change_set_id}"
                "\n[isolated workspace remains active for later commands; review and "
                "explicitly apply this cumulative change set to persist it]"
                if completed.change_set_id
                else (
                    f"\nchange_set_error={completed.change_set_error}"
                    "\n[isolated workspace remains active, but its changes cannot currently "
                    "be applied; fix or reset the sandbox session]"
                    if completed.change_set_error
                    else (
                        "\n[isolated workspace remains active; no applicable source changes]"
                        if completed.workspace_mode == "isolated"
                        else ""
                    )
                )
            )
        )
        return bound_output(rendered)


def _load_check_commands(root: Path, kind: str) -> list[tuple[str, str]]:
    path = root / ".forgeagent.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load {path.name}: {error}") from error
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, dict):
        raise TypeError(".forgeagent.json must contain an object named 'checks'")
    names = ("lint", "test") if kind == "all" else (kind,)
    commands: list[tuple[str, str]] = []
    for name in names:
        value = checks.get(name)
        rows = [value] if isinstance(value, str) else value
        if rows is None:
            continue
        if not isinstance(rows, list) or any(not isinstance(item, str) for item in rows):
            raise ValueError(f"checks.{name} must be a string or list of strings")
        commands.extend((name, command) for command in rows if command.strip())
    return commands

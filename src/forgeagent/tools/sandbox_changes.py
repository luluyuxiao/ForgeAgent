"""Review and safely apply changes produced in an isolated Docker workspace."""

from __future__ import annotations

import json
import subprocess
from typing import Any, ClassVar

from ..workspace_snapshot import SandboxChangeSet, SandboxChangeStore
from .base import Access, ResourceClaim, Tool, ToolEffect, bound_output
from .filesystem import Workspace

_CHANGE_SET_RESOURCE = "sandbox:change-sets"


class ReviewSandboxChangesTool(Tool):
    name = "review_sandbox_changes"
    description = (
        "Review the paths, status summary, and patch retained from a successful isolated "
        "Docker command. This does not modify the real workspace."
    )
    effect = ToolEffect.READ
    capabilities = frozenset({"filesystem_read"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"change_set_id": {"type": "string", "minLength": 1}},
        "required": ["change_set_id"],
        "additionalProperties": False,
    }

    def __init__(self, store: SandboxChangeStore):
        self.store = store

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim(_CHANGE_SET_RESOURCE, Access.READ),)

    def execute(self, change_set_id: str) -> str:
        try:
            item = self.store.require(change_set_id)
        except KeyError as error:
            return f"Error: {error.args[0]}"
        return bound_output(
            json.dumps(
                {
                    "change_set_id": item.change_set_id,
                    "paths": list(item.paths),
                    "summary": list(item.summary),
                    "patch": item.patch,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


class ApplySandboxChangesTool(Tool):
    name = "apply_sandbox_changes"
    description = (
        "Apply a reviewed isolated-Docker change set to the real workspace. This write "
        "requires policy approval, rejects files changed since the snapshot, and is checkpointed."
    )
    effect = ToolEffect.WRITE
    capabilities = frozenset({"filesystem_read", "filesystem_write", "process"})
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"change_set_id": {"type": "string", "minLength": 1}},
        "required": ["change_set_id"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, store: SandboxChangeStore):
        self.workspace = workspace
        self.store = store

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        item = self._resolve(arguments)
        if item is None:
            return (
                ResourceClaim(_CHANGE_SET_RESOURCE, Access.WRITE),
                ResourceClaim("workspace:*", Access.WRITE),
            )
        return (ResourceClaim(_CHANGE_SET_RESOURCE, Access.WRITE),) + tuple(
            ResourceClaim(self.workspace.resource(path), Access.WRITE) for path in item.paths
        )

    def checkpoint_paths(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        item = self._resolve(arguments)
        return item.paths if item is not None else ()

    def execute(self, change_set_id: str) -> str:
        try:
            item = self.store.require(change_set_id)
            for path in item.paths:
                self.workspace.resolve(path, must_exist=False)
            conflicts = self.store.conflicts(change_set_id)
        except (KeyError, OSError, ValueError) as error:
            message = error.args[0] if isinstance(error, KeyError) else str(error)
            return f"Error: cannot validate sandbox change set: {message}"
        if conflicts:
            return (
                "Error: sandbox change set conflict; real workspace changed after the "
                "isolated snapshot: "
                + ", ".join(conflicts)
            )
        checked = _run_git_apply(self.workspace, item.patch, check=True)
        if checked.returncode != 0:
            return f"Error: sandbox patch check failed: {checked.stderr.strip()}"
        applied = _run_git_apply(self.workspace, item.patch, check=False)
        if applied.returncode != 0:
            return f"Error: sandbox patch apply failed: {applied.stderr.strip()}"
        self.store.discard(change_set_id)
        return "Applied sandbox change set to: " + ", ".join(item.paths)

    def _resolve(self, arguments: dict[str, Any]) -> SandboxChangeSet | None:
        try:
            item = self.store.require(str(arguments.get("change_set_id", "")))
            for path in item.paths:
                self.workspace.resolve(path, must_exist=False)
            return item
        except (KeyError, OSError, ValueError):
            return None


def _run_git_apply(
    workspace: Workspace,
    patch: str,
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    arguments = ["git", "apply"]
    if check:
        arguments.append("--check")
    else:
        arguments.append("--whitespace=nowarn")
    arguments.append("-")
    return subprocess.run(
        arguments,
        cwd=workspace.root,
        input=patch,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

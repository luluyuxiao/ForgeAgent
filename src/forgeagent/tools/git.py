"""Git inspection and patch Tools."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, ClassVar

from .base import Access, ResourceClaim, Tool, ToolEffect, bound_output
from .filesystem import Workspace


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show the current branch and concise Git working-tree status."
    effect = ToolEffect.READ
    capabilities = frozenset({"filesystem_read", "process"})
    parallel_safe = True

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("workspace:git", Access.READ),)

    def execute(self) -> str:
        completed = _run_git(self.workspace.root, ["status", "--short", "--branch"])
        if completed.returncode != 0:
            return f"Error: git status failed: {completed.stderr.strip()}"
        return completed.stdout.rstrip() or "[clean working tree]"


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show the current Git diff, optionally staged or limited to one workspace path."
    effect = ToolEffect.READ
    capabilities = frozenset({"filesystem_read", "process"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean", "default": False},
            "path": {"type": "string", "default": ""},
            "context": {"type": "integer", "minimum": 0, "maximum": 20, "default": 3},
        },
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("workspace:git", Access.READ),)

    def execute(self, staged: bool = False, path: str = "", context: int = 3) -> str:
        arguments = ["diff", f"--unified={context}"]
        if staged:
            arguments.append("--cached")
        if path:
            target = self.workspace.resolve(path, must_exist=False)
            arguments.extend(["--", target.relative_to(self.workspace.root).as_posix()])
        completed = _run_git(self.workspace.root, arguments)
        if completed.returncode != 0:
            return f"Error: git diff failed: {completed.stderr.strip()}"
        return bound_output(completed.stdout.rstrip() or "[no diff]")


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = "Apply a git-style unified patch to one or more files inside the workspace."
    effect = ToolEffect.WRITE
    capabilities = frozenset({"filesystem_read", "filesystem_write", "process"})
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"patch": {"type": "string"}},
        "required": ["patch"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        try:
            paths = _patch_paths(str(arguments.get("patch", "")))
            for path in paths:
                self.workspace.resolve(path, must_exist=False)
        except ValueError:
            return (ResourceClaim("workspace:*", Access.WRITE),)
        if not paths:
            return (ResourceClaim("workspace:*", Access.WRITE),)
        return tuple(ResourceClaim(self.workspace.resource(path), Access.WRITE) for path in paths)

    def checkpoint_paths(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        try:
            paths = _patch_paths(str(arguments.get("patch", "")))
            for path in paths:
                self.workspace.resolve(path, must_exist=False)
        except ValueError:
            return ()
        return tuple(paths)

    def execute(self, patch: str) -> str:
        if not patch.strip():
            return "Error: patch must not be empty"
        paths = _patch_paths(patch)
        if not paths:
            return "Error: patch contains no ---/+++ workspace file headers"
        for path in paths:
            self.workspace.resolve(path, must_exist=False)
        checked = _run_git(self.workspace.root, ["apply", "--check", "-"], input_text=patch)
        if checked.returncode != 0:
            return f"Error: patch check failed: {checked.stderr.strip()}"
        applied = _run_git(
            self.workspace.root,
            ["apply", "--whitespace=nowarn", "-"],
            input_text=patch,
        )
        if applied.returncode != 0:
            return f"Error: patch apply failed: {applied.stderr.strip()}"
        return "Applied patch to: " + ", ".join(paths)


def _run_git(
    workspace: Path,
    arguments: list[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t", 1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        if not raw or raw.startswith("/") or ".." in Path(raw).parts:
            raise ValueError(f"patch path escapes workspace: {raw}")
        if raw not in paths:
            paths.append(raw)
    return paths

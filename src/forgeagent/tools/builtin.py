"""Built-in local tools. File operations are confined to one workspace."""

from __future__ import annotations

import difflib
import fnmatch
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from .base import Access, ResourceClaim, Tool, ToolEffect

_MAX_RESULT_CHARS = 24_000


class Workspace:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace is not a directory: {self.root}")

    def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"path escapes workspace: {raw}")
        if must_exist:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                raise ValueError(f"path escapes workspace: {raw}")
        return resolved

    def resource(self, raw: str) -> str:
        return f"file:{self.resolve(raw, must_exist=False)}"


class NowTool(Tool):
    name = "now"
    description = "Return the current local date and time."
    effect = ToolEffect.PURE
    capabilities = frozenset()
    parallel_safe = True

    def execute(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file inside the workspace, optionally by line range."
    effect = ToolEffect.READ
    capabilities = frozenset({"filesystem_read"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 400},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim(self.workspace.resource(str(arguments.get("path", ""))), Access.READ),)

    def execute(self, path: str, start_line: int = 1, max_lines: int = 400) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            return f"Error: not a file: {path}"
        if target.stat().st_size > 2_000_000:
            return f"Error: file is larger than 2 MB: {path}"
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return f"Error: file is not valid UTF-8 text: {path}"
        start = max(0, start_line - 1)
        selected = lines[start : start + max_lines]
        rendered = "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start + 1))
        if start + max_lines < len(lines):
            rendered += f"\n[truncated: {len(lines) - start - max_lines} more lines]"
        return rendered or "[empty file or range]"


class FindFilesTool(Tool):
    name = "find_files"
    description = "Find workspace files whose relative paths match a glob pattern."
    effect = ToolEffect.READ
    capabilities = frozenset({"filesystem_read"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        path = str(arguments.get("path", "."))
        return (ResourceClaim(f"tree:{self.workspace.resolve(path, must_exist=False)}", Access.READ),)

    def execute(self, pattern: str, path: str = ".", limit: int = 200) -> str:
        root = self.workspace.resolve(path, must_exist=True)
        if not root.is_dir():
            return f"Error: not a directory: {path}"
        matches: list[str] = []
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(self.workspace.root).as_posix()
            local = candidate.relative_to(root).as_posix()
            if fnmatch.fnmatch(local, pattern) or fnmatch.fnmatch(relative, pattern):
                matches.append(relative)
                if len(matches) >= limit:
                    break
        return "\n".join(sorted(matches)) if matches else "[no matching files]"


class SearchTextTool(Tool):
    name = "search_text"
    description = "Search UTF-8 workspace files for a literal string."
    effect = ToolEffect.READ
    capabilities = frozenset({"filesystem_read"})
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "include": {"type": "string", "default": "*"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        path = str(arguments.get("path", "."))
        return (ResourceClaim(f"tree:{self.workspace.resolve(path, must_exist=False)}", Access.READ),)

    def execute(self, query: str, path: str = ".", include: str = "*", limit: int = 100) -> str:
        if not query:
            return "Error: query must not be empty"
        root = self.workspace.resolve(path, must_exist=True)
        candidates = [root] if root.is_file() else root.rglob("*")
        matches: list[str] = []
        for candidate in candidates:
            if not candidate.is_file() or not fnmatch.fnmatch(candidate.name, include):
                continue
            try:
                if candidate.stat().st_size > 2_000_000:
                    continue
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    relative = candidate.relative_to(self.workspace.root)
                    matches.append(f"{relative}:{number}:{line}")
                    if len(matches) >= limit:
                        return "\n".join(matches) + "\n[match limit reached]"
        return "\n".join(matches) if matches else "[no matches]"


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or replace a UTF-8 text file inside the workspace."
    effect = ToolEffect.WRITE
    capabilities = frozenset({"filesystem_write"})
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim(self.workspace.resource(str(arguments.get("path", ""))), Access.WRITE),)

    def execute(self, path: str, content: str) -> str:
        target = self.workspace.resolve(path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {target.relative_to(self.workspace.root)}"


class ReplaceTextTool(Tool):
    name = "replace_text"
    description = "Replace one uniquely occurring text block in a UTF-8 workspace file."
    effect = ToolEffect.WRITE
    capabilities = frozenset({"filesystem_read", "filesystem_write"})
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim(self.workspace.resource(str(arguments.get("path", ""))), Access.WRITE),)

    def execute(self, path: str, old: str, new: str) -> str:
        if not old:
            return "Error: old text must not be empty"
        target = self.workspace.resolve(path, must_exist=True)
        try:
            before = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: file is not valid UTF-8 text: {path}"
        count = before.count(old)
        if count != 1:
            return f"Error: expected old text exactly once, found {count} occurrences"
        after = before.replace(old, new, 1)
        target.write_text(after, encoding="utf-8")
        diff = "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
        )
        return _bounded(f"Updated {path}\n{diff}")


class ShellTool(Tool):
    name = "shell"
    description = "Run a non-interactive shell command on the local host in the workspace."
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

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim("workspace:*", Access.WRITE),)

    def execute(self, command: str, timeout: int = 120) -> str:
        if not command.strip():
            return "Error: command must not be empty"
        try:
            completed = subprocess.run(
                command,
                cwd=self.workspace.root,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout} seconds"
        rendered = (
            f"exit_code={completed.returncode}\n"
            f"stdout:\n{completed.stdout or '[empty]'}\n"
            f"stderr:\n{completed.stderr or '[empty]'}"
        )
        return _bounded(rendered)


def build_builtin_tools(root: str | os.PathLike[str]) -> list[Tool]:
    workspace = Workspace(root)
    return [
        NowTool(),
        ReadFileTool(workspace),
        FindFilesTool(workspace),
        SearchTextTool(workspace),
        WriteFileTool(workspace),
        ReplaceTextTool(workspace),
        ShellTool(workspace),
    ]


def _bounded(value: str) -> str:
    if len(value) <= _MAX_RESULT_CHARS:
        return value
    return value[:_MAX_RESULT_CHARS] + f"\n[truncated {len(value) - _MAX_RESULT_CHARS} characters]"

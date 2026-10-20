"""Built-in local tools. File operations are confined to one workspace."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from ..sandbox import CommandExecutor, LocalExecutor
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
        if shutil.which("rg"):
            relative_root = root.relative_to(self.workspace.root).as_posix() or "."
            completed = subprocess.run(
                [
                    "rg",
                    "--files",
                    "--hidden",
                    "--glob",
                    "!.git/**",
                    "--glob",
                    pattern,
                    relative_root,
                ],
                cwd=self.workspace.root,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if completed.returncode in {0, 1}:
                matches = sorted(line for line in completed.stdout.splitlines() if line)
                suffix = "\n[match limit reached]" if len(matches) > limit else ""
                return "\n".join(matches[:limit]) + suffix if matches else "[no matching files]"
            return f"Error: rg failed: {completed.stderr.strip()}"
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
        if shutil.which("rg"):
            relative_root = root.relative_to(self.workspace.root).as_posix() or "."
            completed = subprocess.run(
                [
                    "rg",
                    "--line-number",
                    "--no-heading",
                    "--color",
                    "never",
                    "--fixed-strings",
                    "--hidden",
                    "--glob",
                    "!.git/**",
                    "--glob",
                    include,
                    "--",
                    query,
                    relative_root,
                ],
                cwd=self.workspace.root,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if completed.returncode in {0, 1}:
                matches = [line for line in completed.stdout.splitlines() if line]
                suffix = "\n[match limit reached]" if len(matches) > limit else ""
                return "\n".join(matches[:limit]) + suffix if matches else "[no matches]"
            return f"Error: rg failed: {completed.stderr.strip()}"
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

    def checkpoint_paths(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        return (str(arguments.get("path", "")),)

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

    def checkpoint_paths(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        return (str(arguments.get("path", "")),)

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
        return _bounded(completed.stdout.rstrip() or "[no diff]")


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
        applied = _run_git(self.workspace.root, ["apply", "--whitespace=nowarn", "-"], input_text=patch)
        if applied.returncode != 0:
            return f"Error: patch apply failed: {applied.stderr.strip()}"
        return "Applied patch to: " + ", ".join(paths)


class RunChecksTool(Tool):
    name = "run_checks"
    description = (
        "Run trusted lint/test commands declared in .forgeagent.json and return all exit codes."
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
                }
            )
            if completed.returncode != 0:
                break
        return _bounded(json.dumps({"passed": all(row.get("exit_code") == 0 for row in results), "results": results}, ensure_ascii=False, indent=2))


class ShellTool(Tool):
    name = "shell"
    description = "Run a non-interactive shell command using the configured execution backend."
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
            f"stdout:\n{completed.stdout or '[empty]'}\n"
            f"stderr:\n{completed.stderr or '[empty]'}"
        )
        return _bounded(rendered)


def build_builtin_tools(
    root: str | os.PathLike[str], *, executor: CommandExecutor | None = None
) -> list[Tool]:
    workspace = Workspace(root)
    return [
        NowTool(),
        ReadFileTool(workspace),
        FindFilesTool(workspace),
        SearchTextTool(workspace),
        WriteFileTool(workspace),
        ReplaceTextTool(workspace),
        GitStatusTool(workspace),
        GitDiffTool(workspace),
        ApplyPatchTool(workspace),
        RunChecksTool(workspace, executor),
        ShellTool(workspace, executor),
    ]


def _bounded(value: str) -> str:
    if len(value) <= _MAX_RESULT_CHARS:
        return value
    return value[:_MAX_RESULT_CHARS] + f"\n[truncated {len(value) - _MAX_RESULT_CHARS} characters]"


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

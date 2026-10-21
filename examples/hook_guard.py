"""Example PreToolUse hook: protect credentials and reject risky shell commands."""

from __future__ import annotations

import json
import re
import sys
from pathlib import PurePosixPath

_SAFE_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_RISKY_COMMANDS = (
    re.compile(r"(^|[;&|]\s*)rm\s+-[^\n]*r[^\n]*f", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[^\s]*f", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)sudo\b", re.IGNORECASE),
)


def _is_protected_path(raw: str) -> bool:
    name = PurePosixPath(raw.replace("\\", "/")).name
    if name in _SAFE_ENV_TEMPLATES:
        return False
    return name == ".env" or name.startswith(".env.")


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
        paths.append(raw)
    return paths


def _deny(reason: str) -> None:
    print(reason, file=sys.stderr)
    raise SystemExit(2)

payload = json.load(sys.stdin)
tool_name = str(payload.get("tool_name") or "")
tool_input = payload.get("tool_input") or {}

path = str(tool_input.get("path") or "")
if path and _is_protected_path(path):
    _deny(f"credential file is protected: {path}")

if tool_name == "apply_patch":
    protected = [
        path
        for path in _patch_paths(str(tool_input.get("patch") or ""))
        if _is_protected_path(path)
    ]
    if protected:
        _deny("patch touches protected credential files: " + ", ".join(protected))

if tool_name == "shell":
    command = str(tool_input.get("command") or "")
    if any(pattern.search(command) for pattern in _RISKY_COMMANDS):
        _deny("example policy rejected a potentially destructive shell command")

"""Read-only project-inspection MCP server using newline-delimited stdio JSON-RPC."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"
_SKIP_DIRECTORIES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "build", "dist", "node_modules"}
)
_TEXT_SUFFIXES = frozenset(
    {".c", ".cpp", ".css", ".go", ".h", ".html", ".java", ".js", ".json", ".jsx", ".md", ".py", ".rs", ".sh", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml"}
)
_MAX_TEXT_BYTES = 1_000_000


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "project_summary",
            "description": (
                "Return read-only project statistics: file counts, text lines, "
                "tests, and the most common extensions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "maxFiles": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                        "default": 2000,
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "find_markers",
            "description": "Find TODO, FIXME, or XXX markers in text files inside the workspace.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "marker": {
                        "type": "string",
                        "enum": ["TODO", "FIXME", "XXX"],
                        "default": "TODO",
                    },
                    "path": {"type": "string", "default": "."},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
                "additionalProperties": False,
            },
        },
    ]


def _iter_files(root: Path, start: Path):
    for directory, names, files in os.walk(start):
        names[:] = [name for name in names if name not in _SKIP_DIRECTORIES]
        parent = Path(directory)
        for name in files:
            candidate = parent / name
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_relative_to(root) and resolved.is_file():
                yield resolved


def _project_summary(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    maximum = int(arguments.get("maxFiles", 2000))
    if not 1 <= maximum <= 10000:
        raise ValueError("maxFiles must be between 1 and 10000")
    extensions: Counter[str] = Counter()
    files = 0
    text_lines = 0
    tests = 0
    truncated = False
    for candidate in _iter_files(root, root):
        if files >= maximum:
            truncated = True
            break
        files += 1
        extensions[candidate.suffix.lower() or "[no extension]"] += 1
        if candidate.name.startswith("test_") or candidate.name.endswith("_test.py"):
            tests += 1
        if candidate.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size > _MAX_TEXT_BYTES:
                continue
            text_lines += len(candidate.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
    return {
        "workspace": str(root),
        "files": files,
        "textLines": text_lines,
        "testFiles": tests,
        "topExtensions": dict(extensions.most_common(12)),
        "truncated": truncated,
    }


def _find_markers(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    marker = str(arguments.get("marker", "TODO")).upper()
    if marker not in {"TODO", "FIXME", "XXX"}:
        raise ValueError("marker must be TODO, FIXME, or XXX")
    limit = int(arguments.get("limit", 50))
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    start = _resolve_inside(root, str(arguments.get("path", ".")))
    if not start.is_dir():
        raise ValueError("path must name a workspace directory")
    matches: list[dict[str, Any]] = []
    for candidate in _iter_files(root, start):
        if candidate.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size > _MAX_TEXT_BYTES:
                continue
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, 1):
            if marker in line.upper():
                matches.append(
                    {
                        "path": candidate.relative_to(root).as_posix(),
                        "line": number,
                        "text": line.strip()[:300],
                    }
                )
                if len(matches) >= limit:
                    return {"marker": marker, "matches": matches, "truncated": True}
    return {"marker": marker, "matches": matches, "truncated": False}


def _resolve_inside(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {raw}")
    return resolved


def _call_tool(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _tool_error("tool arguments must be an object")
    try:
        if name == "project_summary":
            result = _project_summary(root, arguments)
        elif name == "find_markers":
            result = _find_markers(root, arguments)
        else:
            raise ValueError(f"unknown tool: {name}")
    except (OSError, ValueError) as error:
        return _tool_error(str(error))
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ]
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _dispatch(root: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {
            "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "forgeagent-project-inspector", "version": "0.1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tool_schemas()}
    if method == "tools/call":
        return _call_tool(root, params)
    raise KeyError(method)


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser().resolve()
    if not root.is_dir():
        print(f"workspace is not a directory: {root}", file=sys.stderr)
        return 2
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id: Any = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise TypeError("JSON-RPC message must be an object")
            request_id = message.get("id")
            method = str(message.get("method") or "")
            if request_id is None:
                continue
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise TypeError("params must be an object")
            result = _dispatch(root, method, params)
            _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except KeyError as error:
            if request_id is not None:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"unknown method: {error.args[0]}"},
                    }
                )
        except Exception as error:  # noqa: BLE001 - protocol boundary returns structured errors
            if request_id is not None:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": str(error)},
                    }
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

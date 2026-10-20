"""Repository-local instructions loaded into the Agent system prompt."""

from __future__ import annotations

import os
from pathlib import Path

_ROOT_INSTRUCTION_FILES = ("AGENTS.md", ".forgeagent/instructions.md")
_SKIP_PARTS = frozenset({".git", ".venv", "node_modules", "dist", "build"})


def load_repository_instructions(
    workspace: str | Path,
    *,
    max_chars: int = 12_000,
) -> str:
    """Load root instructions and advertise scoped AGENTS.md files without bloating context."""
    root = Path(workspace).expanduser().resolve()
    sections: list[str] = []
    used = 0

    for relative in _ROOT_INSTRUCTION_FILES:
        path = root / relative
        if not path.is_file():
            continue
        content = _read_bounded(path, max_chars - used)
        if not content:
            continue
        sections.append(f"## {relative}\n{content}")
        used += len(content)
        if used >= max_chars:
            break

    scoped = _find_scoped_agents_files(root)
    if scoped:
        rendered = "\n".join(f"- {path}" for path in scoped)
        sections.append(
            "## Scoped instruction files\n"
            "Before changing files below these directories, read the applicable file:\n"
            f"{rendered}"
        )

    return "\n\n".join(sections)


def _read_bounded(path: Path, remaining: int) -> str:
    if remaining <= 0:
        return ""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if len(content) <= remaining:
        return content
    omitted = len(content) - remaining
    return content[:remaining] + f"\n[instructions truncated by {omitted} characters]"


def _find_scoped_agents_files(root: Path, *, limit: int = 50) -> list[str]:
    found: list[str] = []
    try:
        for directory, names, files in os.walk(root):
            names[:] = [name for name in names if name not in _SKIP_PARTS]
            path = Path(directory) / "AGENTS.md"
            if "AGENTS.md" not in files or path == root / "AGENTS.md":
                continue
            found.append(path.relative_to(root).as_posix())
            if len(found) >= limit:
                break
    except OSError:
        return []
    return sorted(found)

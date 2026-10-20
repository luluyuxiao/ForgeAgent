"""Repository instructions and lazily activated workflow Skills."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar

from .tools import Tool, ToolEffect

_ROOT_INSTRUCTION_FILES = ("AGENTS.md", ".forgeagent/instructions.md")
_SKIP_PARTS = frozenset({".git", ".venv", "node_modules", "dist", "build"})
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_SKILL_CHARS = 32_000


def load_repository_instructions(
    workspace: str | Path,
    *,
    max_chars: int = 12_000,
) -> str:
    """Load root instructions and advertise scoped AGENTS.md files."""
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


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    scope: str

    @property
    def content_hash(self) -> str:
        return sha256(self.render().encode("utf-8")).hexdigest()

    def render(self) -> str:
        return (
            f"# Loaded skill: {self.name}\n\n{self.instructions.strip()}\n\n---\n"
            "This skill supplies workflow guidance only. It grants no Tool, permission, "
            "network, filesystem, or sandbox authority."
        )


class SkillRegistry:
    def __init__(self, skills: dict[str, Skill] | None = None):
        self._skills = dict(skills or {})
        self._active: list[str] = []

    @classmethod
    def discover(
        cls,
        workspace: str | Path,
        *,
        global_dir: str | Path | None = None,
        project_dir: str | Path | None = None,
    ) -> SkillRegistry:
        root = Path(workspace).expanduser().resolve()
        sources = [
            (
                "global",
                Path(global_dir).expanduser()
                if global_dir
                else Path.home() / ".forgeagent/skills",
            ),
            ("project", Path(project_dir) if project_dir else root / ".forgeagent/skills"),
        ]
        found: dict[str, Skill] = {}
        for scope, directory in sources:
            directory = directory.expanduser().resolve()
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*/SKILL.md")):
                try:
                    skill = _read_skill(path, directory, scope)
                except (OSError, UnicodeError, ValueError):
                    continue
                found[skill.name] = skill
        return cls(found)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._skills))

    def active_names(self) -> tuple[str, ...]:
        return tuple(self._active)

    def catalog(self) -> str:
        return "\n".join(
            f"- {'*' if name in self._active else '-'} {name}: {self._skills[name].description}"
            for name in self.names()
        )

    def activate(self, name: str) -> Skill:
        try:
            skill = self._skills[name]
        except KeyError as error:
            raise KeyError(f"unknown skill: {name}") from error
        if name not in self._active:
            self._active.append(name)
        return skill

    def restore(self, names: list[str]) -> None:
        self._active = []
        for name in names:
            if name in self._skills and name not in self._active:
                self._active.append(name)

    def render_active(self) -> str:
        return "\n\n".join(self._skills[name].render() for name in self._active)

    def __bool__(self) -> bool:
        return bool(self._skills)


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "Load one discovered workflow Skill into subsequent model requests."
    effect = ToolEffect.READ
    capabilities = frozenset({"filesystem_read"})
    parallel_safe = False
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, skills: SkillRegistry):
        self.skills = skills

    def execute(self, name: str) -> str:
        return self.skills.activate(name).render()


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


def _read_skill(path: Path, root: Path, scope: str) -> Skill:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("SKILL.md resolves outside its root") from error
    text = resolved.read_text(encoding="utf-8")
    if len(text) > _MAX_SKILL_CHARS:
        raise ValueError("SKILL.md is too large")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as error:
        raise ValueError("SKILL.md frontmatter is not closed") from error
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("'\"")
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    instructions = "\n".join(lines[end + 1 :]).strip()
    if not _SKILL_NAME.fullmatch(name):
        raise ValueError("invalid skill name")
    if not description or len(description) > 300 or not instructions:
        raise ValueError("skill requires description and instructions")
    return Skill(name, description, instructions, resolved, scope)

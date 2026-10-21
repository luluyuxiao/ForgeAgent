from __future__ import annotations

import os

from ..sandbox import CommandExecutor
from .base import Access, ResourceClaim, Tool, ToolEffect
from .filesystem import (
    FindFilesTool,
    NowTool,
    ReadFileTool,
    ReplaceTextTool,
    SearchTextTool,
    Workspace,
    WriteFileTool,
)
from .git import ApplyPatchTool, GitDiffTool, GitStatusTool
from .process import RunChecksTool, ShellTool
from .registry import EmbeddingBackend, ToolCatalog, ToolRegistry, install_catalog_tools


def build_builtin_tools(
    root: str | os.PathLike[str], *, executor: CommandExecutor | None = None
) -> list[Tool]:
    """Construct the standard local Tool set for one workspace."""
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

__all__ = [
    "Access",
    "EmbeddingBackend",
    "ResourceClaim",
    "Tool",
    "ToolCatalog",
    "ToolEffect",
    "ToolRegistry",
    "Workspace",
    "build_builtin_tools",
    "install_catalog_tools",
]

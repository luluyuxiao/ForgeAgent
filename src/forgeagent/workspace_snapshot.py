"""Temporary workspace copies used to contain process-generated mutations."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

WorkspaceManifest = dict[str, tuple[str, int, str]]

_FALLBACK_IGNORES = frozenset(
    {
        ".git",
        ".env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)


@contextmanager
def temporary_workspace(source: Path) -> Iterator[tuple[Path, WorkspaceManifest]]:
    """Yield an initialized disposable copy and its baseline manifest."""
    with tempfile.TemporaryDirectory(prefix="forgeagent-sandbox-") as temporary:
        workspace = Path(temporary) / "workspace"
        _copy_workspace(source, workspace)
        yield workspace, workspace_manifest(workspace)


def workspace_manifest(root: Path) -> WorkspaceManifest:
    """Hash relevant files without following links or scanning dependency caches."""
    manifest: WorkspaceManifest = {}
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not _ignore_snapshot_name(name)]
        base = Path(directory)
        for name in files:
            if _ignore_snapshot_name(name):
                continue
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                manifest[relative] = ("link", len(target), target)
                continue
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                manifest[relative] = ("file", path.stat().st_size, digest.hexdigest())
            except OSError:
                continue
    return manifest


def workspace_changes(
    before: WorkspaceManifest,
    after: WorkspaceManifest,
    *,
    limit: int = 200,
) -> tuple[str, ...]:
    """Return a bounded, Git-style status summary for two manifests."""
    rows = []
    for path in sorted(before.keys() | after.keys()):
        if path not in before:
            rows.append(f"A {path}")
        elif path not in after:
            rows.append(f"D {path}")
        elif before[path] != after[path]:
            rows.append(f"M {path}")
        if len(rows) == limit:
            rows.append("... additional changes omitted")
            break
    return tuple(rows)


def _copy_workspace(source: Path, target: Path) -> None:
    source = source.expanduser().resolve()
    target.mkdir(parents=True)
    files = _git_workspace_files(source)
    if files is None:
        shutil.copytree(
            source,
            target,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=lambda _path, names: sorted(
                name for name in names if _ignore_snapshot_name(name)
            ),
        )
    else:
        for relative in files:
            source_path = source / relative
            target_path = target / relative
            if not source_path.exists() and not source_path.is_symlink():
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if source_path.is_symlink():
                target_path.symlink_to(os.readlink(source_path))
            elif source_path.is_dir():
                shutil.copytree(source_path, target_path, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source_path, target_path)
    _initialize_snapshot_repository(target)


def _git_workspace_files(workspace: Path) -> tuple[Path, ...] | None:
    try:
        root = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if root.returncode != 0 or Path(root.stdout.strip()).resolve() != workspace:
            return None
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if listed.returncode != 0:
        return None
    rows = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        rows.append(relative)
    return tuple(rows)


def _initialize_snapshot_repository(workspace: Path) -> None:
    commands = (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=ForgeAgent Sandbox",
            "-c",
            "user.email=sandbox@localhost",
            "commit",
            "--allow-empty",
            "-qm",
            "sandbox baseline",
        ],
    )
    try:
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                return
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return


def _ignore_snapshot_name(name: str) -> bool:
    return name in _FALLBACK_IGNORES or (
        name.startswith(".env.") and name != ".env.example"
    )

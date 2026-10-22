"""Temporary workspace copies used to contain process-generated mutations."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

FileFingerprint = tuple[str, int, str, int]
WorkspaceManifest = dict[str, FileFingerprint]

_MISSING_FINGERPRINT: FileFingerprint = ("missing", 0, "", 0)
_MAX_CHANGE_SETS = 20
_MAX_PATCH_BYTES = 10 * 1024 * 1024

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


@dataclass(frozen=True)
class SandboxChangeSet:
    change_set_id: str
    patch: str
    paths: tuple[str, ...]
    summary: tuple[str, ...]
    baseline: dict[str, FileFingerprint]


class SandboxChangeStore:
    """Keep bounded process-local patches until policy-approved application."""

    def __init__(
        self,
        workspace: Path,
        *,
        limit: int = _MAX_CHANGE_SETS,
        max_patch_bytes: int = _MAX_PATCH_BYTES,
    ):
        self.workspace = workspace.expanduser().resolve()
        self.limit = max(1, limit)
        self.max_patch_bytes = max(1, max_patch_bytes)
        self._items: dict[str, SandboxChangeSet] = {}
        self._lock = threading.RLock()

    def add(
        self,
        *,
        patch: str,
        paths: tuple[str, ...],
        summary: tuple[str, ...],
        baseline: WorkspaceManifest,
    ) -> SandboxChangeSet:
        if len(patch.encode("utf-8")) > self.max_patch_bytes:
            raise ValueError(f"sandbox patch exceeds {self.max_patch_bytes} bytes")
        if not patch.strip() or not paths:
            raise ValueError("sandbox execution produced no applicable patch")
        change_set_id = uuid.uuid4().hex[:12]
        item = SandboxChangeSet(
            change_set_id=change_set_id,
            patch=patch,
            paths=paths,
            summary=summary,
            baseline={path: baseline.get(path, _MISSING_FINGERPRINT) for path in paths},
        )
        with self._lock:
            self._items[change_set_id] = item
            while len(self._items) > self.limit:
                self._items.pop(next(iter(self._items)))
        return item

    def require(self, change_set_id: str) -> SandboxChangeSet:
        with self._lock:
            try:
                return self._items[change_set_id]
            except KeyError as error:
                raise KeyError(f"unknown or expired sandbox change set: {change_set_id}") from error

    def conflicts(self, change_set_id: str) -> tuple[str, ...]:
        item = self.require(change_set_id)
        return tuple(
            path
            for path, expected in item.baseline.items()
            if fingerprint_path(self.workspace / path) != expected
        )

    def discard(self, change_set_id: str) -> None:
        with self._lock:
            self._items.pop(change_set_id, None)


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
        base = Path(directory)
        traversable = []
        for name in names:
            if _ignore_snapshot_name(name):
                continue
            path = base / name
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                manifest[relative] = fingerprint_path(path)
            else:
                traversable.append(name)
        names[:] = traversable
        for name in files:
            if _ignore_snapshot_name(name):
                continue
            path = base / name
            relative = path.relative_to(root).as_posix()
            fingerprint = fingerprint_path(path)
            if fingerprint[0] != "missing":
                manifest[relative] = fingerprint
    return manifest


def fingerprint_path(path: Path) -> FileFingerprint:
    """Fingerprint one path without following symbolic links."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _MISSING_FINGERPRINT
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        return ("link", len(target), target, mode)
    if not path.is_file():
        return ("other", 0, "", mode)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ("other", 0, "", mode)
    return ("file", metadata.st_size, digest.hexdigest(), mode)


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


def capture_workspace_patch(workspace: Path) -> tuple[str, tuple[str, ...]]:
    """Create a binary-capable patch against the temporary baseline commit."""
    staged = subprocess.run(
        _snapshot_git_command(workspace, "add", "-A"),
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if staged.returncode != 0:
        raise RuntimeError(f"cannot stage sandbox changes: {staged.stderr.strip()}")
    paths = _staged_paths(workspace)
    excluded = tuple(
        path for path in paths if any(_ignore_snapshot_name(part) for part in Path(path).parts)
    )
    if excluded:
        reset = subprocess.run(
            _snapshot_git_command(workspace, "reset", "-q", "HEAD", "--", *excluded),
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if reset.returncode != 0:
            raise RuntimeError(f"cannot exclude sandbox secrets: {reset.stderr.strip()}")
        paths = _staged_paths(workspace)
    diff = subprocess.run(
        _snapshot_git_command(
            workspace,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-renames",
            "HEAD",
        ),
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if diff.returncode != 0:
        raise RuntimeError(f"cannot create sandbox patch: {diff.stderr.strip()}")
    return diff.stdout, paths


def _staged_paths(workspace: Path) -> tuple[str, ...]:
    listed = subprocess.run(
        _snapshot_git_command(
            workspace,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            "HEAD",
        ),
        cwd=workspace,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError("cannot list sandbox patch paths")
    return tuple(os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw)


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
            if any(_ignore_snapshot_name(part) for part in relative.parts):
                continue
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
        _snapshot_git_command(workspace, "init", "-q"),
        _snapshot_git_command(workspace, "add", "-A"),
        _snapshot_git_command(
            workspace,
            "-c",
            "user.name=ForgeAgent Sandbox",
            "-c",
            "user.email=sandbox@localhost",
            "commit",
            "--allow-empty",
            "-qm",
            "sandbox baseline",
        ),
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


def _snapshot_git_command(workspace: Path, *arguments: str) -> list[str]:
    """Use Git metadata kept outside the directory exposed to the container."""
    git_directory = workspace.parent / ".forgeagent-snapshot.git"
    return [
        "git",
        "--git-dir",
        str(git_directory),
        "--work-tree",
        str(workspace),
        *arguments,
    ]


def _ignore_snapshot_name(name: str) -> bool:
    return name in _FALLBACK_IGNORES or (
        name.startswith(".env.") and name != ".env.example"
    )

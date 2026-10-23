"""Command execution backends. Docker mode fails closed and never falls back locally."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .workspace_snapshot import (
    IsolatedWorkspaceSession,
    SandboxChangeStore,
    capture_workspace_patch,
    create_isolated_workspace,
    fingerprint_path,
    synchronize_isolated_workspace,
    workspace_changes,
    workspace_manifest,
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    workspace_mode: str = "direct"
    workspace_changes: tuple[str, ...] = ()
    command_changes: tuple[str, ...] = ()
    change_set_id: str = ""
    change_set_error: str = ""
    sandbox_session_id: str = ""
    session_conflicts: tuple[str, ...] = ()


class CommandExecutor(Protocol):
    name: str

    def execute(
        self, command: str, cwd: Path, timeout: int, *, stdin: str | None = None
    ) -> CommandResult: ...


class LocalExecutor:
    name = "local"

    def execute(
        self, command: str, cwd: Path, timeout: int, *, stdin: str | None = None
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                shell=True,
                text=True,
                capture_output=True,
                input=stdin,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"command timed out after {timeout} seconds") from error
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class DockerExecutor:
    name = "docker"

    def __init__(
        self,
        image: str = "forgeagent-sandbox:latest",
        *,
        network: str = "none",
        memory: str = "1g",
        cpus: float = 1.0,
        pids_limit: int = 256,
        tmpfs_size: str = "64m",
        workspace_mode: str = "isolated",
        runner=subprocess.run,
    ):
        if network not in {"none", "bridge"}:
            raise ValueError("Docker network must be 'none' or 'bridge'")
        if workspace_mode not in {"isolated", "direct"}:
            raise ValueError("Docker workspace mode must be 'isolated' or 'direct'")
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.tmpfs_size = tmpfs_size
        self.workspace_mode = workspace_mode
        self._runner = runner
        self._change_stores: dict[Path, SandboxChangeStore] = {}
        self._sessions: dict[Path, IsolatedWorkspaceSession] = {}
        self._store_lock = threading.RLock()

    def change_store(self, workspace: Path) -> SandboxChangeStore:
        """Return the process-local pending-change store for one real workspace."""
        root = workspace.expanduser().resolve()
        with self._store_lock:
            store = self._change_stores.get(root)
            if store is None:
                store = SandboxChangeStore(root)
                self._change_stores[root] = store
            return store

    def isolated_session(self, workspace: Path) -> IsolatedWorkspaceSession:
        """Return or create the reusable isolated workspace for one real workspace."""
        root = workspace.expanduser().resolve()
        with self._store_lock:
            session = self._sessions.get(root)
            if session is None:
                session = create_isolated_workspace(root)
                self._sessions[root] = session
            return session

    def discard_session(self, workspace: Path) -> bool:
        """Discard pending isolated state so the next command starts from real files."""
        root = workspace.expanduser().resolve()
        with self._store_lock:
            session = self._sessions.pop(root, None)
        if session is None:
            return False
        with session.lock:
            if session.change_set_id:
                self.change_store(root).discard(session.change_set_id)
            try:
                session.close()
            except OSError:
                pass
        return True

    def close(self) -> None:
        """Remove every retained isolated workspace owned by this executor."""
        with self._store_lock:
            roots = tuple(self._sessions)
        for root in roots:
            self.discard_session(root)

    def command(self, shell_command: str, workspace: Path) -> list[str]:
        user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "65532:65532"
        return [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--read-only",
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self.tmpfs_size}",
            "--user",
            user,
            "--mount",
            f"type=bind,src={workspace},dst=/workspace,rw",
            "--workdir",
            "/workspace",
            self.image,
            "/bin/sh",
            "-lc",
            shell_command,
        ]

    def execute(
        self, command: str, cwd: Path, timeout: int, *, stdin: str | None = None
    ) -> CommandResult:
        if self.workspace_mode == "direct":
            before = workspace_manifest(cwd)
            completed = self._run(command, cwd, timeout, stdin)
            changes = workspace_changes(before, workspace_manifest(cwd))
            return CommandResult(
                completed.returncode,
                completed.stdout,
                completed.stderr,
                workspace_mode="direct",
                workspace_changes=changes,
                command_changes=changes,
            )
        try:
            session = self.isolated_session(cwd)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"cannot prepare isolated workspace: {error}") from error
        with session.lock:
            conflicts = synchronize_isolated_workspace(session)
            if conflicts:
                cumulative = workspace_changes(
                    session.baseline,
                    workspace_manifest(session.workspace),
                )
                return CommandResult(
                    125,
                    "",
                    "sandbox session conflicts with real workspace: "
                    + ", ".join(conflicts),
                    workspace_mode="isolated",
                    workspace_changes=cumulative,
                    change_set_id=session.change_set_id,
                    sandbox_session_id=session.session_id,
                    session_conflicts=conflicts,
                )
            before_command = workspace_manifest(session.workspace)
            completed = self._run(command, session.workspace, timeout, stdin)
            after = workspace_manifest(session.workspace)
            session.command_count += 1
            cumulative = workspace_changes(session.baseline, after)
            command_changes = workspace_changes(before_command, after)
            change_set_error = self._refresh_change_set(session, cwd, cumulative)
            return CommandResult(
                completed.returncode,
                completed.stdout,
                completed.stderr,
                workspace_mode="isolated",
                workspace_changes=cumulative,
                command_changes=command_changes,
                change_set_id=session.change_set_id,
                change_set_error=change_set_error,
                sandbox_session_id=session.session_id,
            )

    def _refresh_change_set(
        self,
        session: IsolatedWorkspaceSession,
        real_workspace: Path,
        changes: tuple[str, ...],
    ) -> str:
        store = self.change_store(real_workspace)
        if not changes:
            if session.change_set_id:
                store.discard(session.change_set_id)
                session.change_set_id = ""
            return ""
        try:
            patch, paths = capture_workspace_patch(session.workspace)
            unsupported = tuple(
                path
                for path in paths
                if session.baseline.get(path, ("missing", 0, "", 0))[0]
                not in {"file", "missing"}
                or fingerprint_path(session.workspace / path)[0]
                not in {"file", "missing"}
            )
            if unsupported:
                raise ValueError(
                    "sandbox change sets support only regular files; unsupported paths: "
                    + ", ".join(unsupported)
                )
            if not patch or not paths:
                if session.change_set_id:
                    store.discard(session.change_set_id)
                    session.change_set_id = ""
                return ""
            item = store.add(
                patch=patch,
                paths=paths,
                summary=changes,
                baseline=session.baseline,
                change_set_id=session.change_set_id or None,
            )
            session.change_set_id = item.change_set_id
            return ""
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as error:
            if session.change_set_id:
                store.discard(session.change_set_id)
                session.change_set_id = ""
            return f"{type(error).__name__}: {error}"

    def _run(
        self,
        command: str,
        workspace: Path,
        timeout: int,
        stdin: str | None,
    ):
        try:
            return self._runner(
                self.command(command, workspace),
                text=True,
                capture_output=True,
                input=stdin,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"command timed out after {timeout} seconds") from error
        except FileNotFoundError as error:
            raise RuntimeError("Docker executable was not found; local fallback is disabled") from error

"""Command execution backends. Docker mode fails closed and never falls back locally."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    name: str

    def execute(self, command: str, cwd: Path, timeout: int) -> CommandResult: ...


class LocalExecutor:
    name = "local"

    def execute(self, command: str, cwd: Path, timeout: int) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                shell=True,
                text=True,
                capture_output=True,
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
        runner=subprocess.run,
    ):
        if network not in {"none", "bridge"}:
            raise ValueError("Docker network must be 'none' or 'bridge'")
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.tmpfs_size = tmpfs_size
        self._runner = runner

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

    def execute(self, command: str, cwd: Path, timeout: int) -> CommandResult:
        try:
            completed = self._runner(
                self.command(command, cwd),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"command timed out after {timeout} seconds") from error
        except FileNotFoundError as error:
            raise RuntimeError("Docker executable was not found; local fallback is disabled") from error
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

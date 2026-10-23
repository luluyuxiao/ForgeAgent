from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeagent.sandbox import DockerExecutor


def test_docker_executor_builds_hardened_fail_closed_command(tmp_path):
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    executor = DockerExecutor("sandbox:test", runner=runner)
    result = executor.execute("id", tmp_path, 10)
    command = captured["command"]

    assert result.stdout == "ok"
    assert "--read-only" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert command[-3:] == ["/bin/sh", "-lc", "id"]
    assert result.workspace_mode == "isolated"


def test_isolated_workspace_discards_container_deletions(tmp_path):
    protected = tmp_path / "protected.txt"
    protected.write_text("keep me", encoding="utf-8")

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        assert not (mounted / ".git").exists()
        (mounted / "protected.txt").unlink()
        (mounted / "generated.txt").write_text("temporary", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = DockerExecutor("sandbox:test", runner=runner).execute("mutate", tmp_path, 10)

    assert protected.read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "generated.txt").exists()
    assert result.workspace_changes == ("A generated.txt", "D protected.txt")
    assert result.change_set_id


def test_failed_isolated_command_retains_visible_scratch_changes(tmp_path):
    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / "failed.txt").write_text("discard me", encoding="utf-8")
        return SimpleNamespace(returncode=1, stdout="", stderr="failed")

    result = DockerExecutor("sandbox:test", runner=runner).execute("mutate", tmp_path, 10)

    assert result.workspace_changes == ("A failed.txt",)
    assert result.command_changes == ("A failed.txt",)
    assert result.change_set_id
    assert not (tmp_path / "failed.txt").exists()


def test_isolated_commands_reuse_one_workspace_and_cumulative_change_set(tmp_path):
    mounts = []

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        mounts.append(mounted)
        if command[-1] == "first":
            (mounted / "first.txt").write_text("one", encoding="utf-8")
            dependency = mounted / ".venv" / "marker"
            dependency.parent.mkdir()
            dependency.write_text("installed", encoding="utf-8")
        else:
            assert (mounted / "first.txt").read_text(encoding="utf-8") == "one"
            assert (mounted / ".venv" / "marker").read_text(encoding="utf-8") == "installed"
            (mounted / "second.txt").write_text("two", encoding="utf-8")
        return SimpleNamespace(
            returncode=0 if command[-1] == "first" else 1,
            stdout="ok",
            stderr="test failed" if command[-1] == "second" else "",
        )

    executor = DockerExecutor("sandbox:test", runner=runner)
    first = executor.execute("first", tmp_path, 10)
    second = executor.execute("second", tmp_path, 10)

    assert mounts[0] == mounts[1]
    assert second.returncode == 1
    assert first.sandbox_session_id == second.sandbox_session_id
    assert first.change_set_id == second.change_set_id
    assert first.workspace_changes == ("A first.txt",)
    assert second.command_changes == ("A second.txt",)
    assert second.workspace_changes == ("A first.txt", "A second.txt")
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()


def test_discard_session_removes_scratch_workspace_and_starts_fresh(tmp_path):
    mounts = []

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        mounts.append(mounted)
        if command[-1] == "write":
            (mounted / "scratch.txt").write_text("temporary", encoding="utf-8")
        else:
            assert not (mounted / "scratch.txt").exists()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    executor = DockerExecutor("sandbox:test", runner=runner)
    written = executor.execute("write", tmp_path, 10)
    old_workspace = mounts[-1]
    assert executor.discard_session(tmp_path)
    assert not old_workspace.exists()

    fresh = executor.execute("inspect", tmp_path, 10)

    assert fresh.sandbox_session_id != written.sandbox_session_id
    assert mounts[-1] != old_workspace
    assert fresh.workspace_changes == ()


def test_retained_session_imports_non_conflicting_real_workspace_edits(tmp_path):
    observed = []

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        observed.append(command[-1])
        if command[-1] == "verify":
            assert (mounted / "host-edit.txt").read_text(encoding="utf-8") == "new"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    executor = DockerExecutor("sandbox:test", runner=runner)
    first = executor.execute("start", tmp_path, 10)
    (tmp_path / "host-edit.txt").write_text("new", encoding="utf-8")

    second = executor.execute("verify", tmp_path, 10)

    assert observed == ["start", "verify"]
    assert second.sandbox_session_id == first.sandbox_session_id
    assert second.workspace_changes == ()


def test_retained_session_blocks_overlapping_real_workspace_edits(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("baseline", encoding="utf-8")
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / "shared.txt").write_text("sandbox", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    executor = DockerExecutor("sandbox:test", runner=runner)
    first = executor.execute("edit", tmp_path, 10)
    target.write_text("user", encoding="utf-8")

    blocked = executor.execute("test", tmp_path, 10)

    assert calls == 1
    assert blocked.returncode == 125
    assert blocked.session_conflicts == ("shared.txt",)
    assert blocked.change_set_id == first.change_set_id
    assert target.read_text(encoding="utf-8") == "user"


def test_isolated_change_set_excludes_generated_environment_secrets(tmp_path):
    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
        (mounted / "safe.txt").write_text("safe", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    executor = DockerExecutor("sandbox:test", runner=runner)
    result = executor.execute("mutate", tmp_path, 10)

    item = executor.change_store(tmp_path).require(result.change_set_id)
    assert item.paths == ("safe.txt",)
    assert "TOKEN=secret" not in item.patch


def test_isolated_change_set_rejects_symbolic_links(tmp_path):
    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / "external-link").symlink_to("/tmp/outside")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = DockerExecutor("sandbox:test", runner=runner).execute("mutate", tmp_path, 10)

    assert result.change_set_id == ""
    assert "support only regular files" in result.change_set_error
    assert not (tmp_path / "external-link").exists()


def test_direct_workspace_mode_explicitly_allows_container_deletions(tmp_path):
    target = tmp_path / "delete-me.txt"
    target.write_text("temporary", encoding="utf-8")

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        (mounted / "delete-me.txt").unlink()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = DockerExecutor(
        "sandbox:test", workspace_mode="direct", runner=runner
    ).execute("mutate", tmp_path, 10)

    assert not target.exists()
    assert result.workspace_mode == "direct"
    assert result.workspace_changes == ("D delete-me.txt",)


def test_isolated_git_snapshot_includes_dirty_files_but_excludes_ignored_secrets(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    tracked.write_text("dirty working tree", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("visible", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hidden", encoding="utf-8")

    def runner(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        mounted = Path(next(item[4:] for item in mount.split(",") if item.startswith("src=")))
        assert (mounted / "tracked.txt").read_text(encoding="utf-8") == "dirty working tree"
        assert (mounted / "untracked.txt").read_text(encoding="utf-8") == "visible"
        assert not (mounted / ".env").exists()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    DockerExecutor("sandbox:test", runner=runner).execute("inspect", tmp_path, 10)


def test_docker_executor_rejects_unknown_workspace_mode():
    with pytest.raises(ValueError, match="workspace mode"):
        DockerExecutor(workspace_mode="unknown")

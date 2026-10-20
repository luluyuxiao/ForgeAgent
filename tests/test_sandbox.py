from __future__ import annotations

from types import SimpleNamespace

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

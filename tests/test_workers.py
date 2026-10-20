from __future__ import annotations

import time

from forgeagent.workers import ChildRunResult, SubAgentManager


def test_foreground_child_returns_result_and_failure_is_contained():
    manager = SubAgentManager(lambda task: ChildRunResult(task.upper(), 12, 3))
    try:
        done = manager.submit("review", background=False)
        assert done.status == "completed"
        assert done.result == "REVIEW"
        assert done.prompt_tokens == 12
        assert done.completion_tokens == 3
    finally:
        manager.close()


def test_background_worker_exposes_state():
    def run(task):
        time.sleep(0.03)
        return f"done:{task}"

    manager = SubAgentManager(run, max_workers=1)
    try:
        queued = manager.submit("x", background=True)
        deadline = time.time() + 1
        while manager.get(queued.task_id).status not in {"completed", "failed"}:
            assert time.time() < deadline
            time.sleep(0.01)
        assert manager.get(queued.task_id).result == "done:x"
    finally:
        manager.close()

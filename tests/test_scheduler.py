from __future__ import annotations

import time

from forgeagent.contracts import ToolCall
from forgeagent.registry import ToolRegistry
from forgeagent.scheduler import BatchScheduler
from forgeagent.tools import Tool, ToolEffect


class SlowRead(Tool):
    name = "slow_read"
    description = "slow independent read"
    effect = ToolEffect.READ
    capabilities = frozenset()
    parallel_safe = True

    def execute(self, label: str) -> str:
        time.sleep(0.12)
        return label


class Stateful(Tool):
    name = "stateful"
    description = "stateful barrier"
    effect = ToolEffect.WRITE
    capabilities = frozenset()

    def execute(self, label: str) -> str:
        return label


def test_independent_reads_run_in_parallel():
    registry = ToolRegistry([SlowRead()])
    scheduler = BatchScheduler(registry, max_workers=2)
    calls = [
        ToolCall("1", "slow_read", {"label": "one"}),
        ToolCall("2", "slow_read", {"label": "two"}),
    ]

    started = time.perf_counter()
    results = scheduler.execute(calls)
    duration = time.perf_counter() - started

    assert results == {"1": "one", "2": "two"}
    assert duration < 0.21


def test_stateful_tool_is_a_serial_wave_barrier():
    registry = ToolRegistry([SlowRead(), Stateful()])
    scheduler = BatchScheduler(registry)
    waves = scheduler.plan(
        [
            ToolCall("1", "slow_read", {"label": "one"}),
            ToolCall("2", "stateful", {"label": "write"}),
            ToolCall("3", "slow_read", {"label": "three"}),
        ]
    )

    assert [[item.call.id for item in wave] for wave in waves] == [["1"], ["2"], ["3"]]


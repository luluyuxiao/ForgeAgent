"""Effect- and resource-aware execution of one model Tool Call batch."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .contracts import EventCallback, ToolCall
from .registry import ToolRegistry
from .tools import ResourceClaim


@dataclass(frozen=True)
class BatchItem:
    call: ToolCall
    claims: tuple[ResourceClaim, ...]
    parallel_safe: bool


class BatchScheduler:
    def __init__(self, registry: ToolRegistry, *, max_workers: int = 4):
        self.registry = registry
        self.max_workers = max(1, max_workers)

    def plan(self, calls: list[ToolCall]) -> list[list[BatchItem]]:
        waves: list[list[BatchItem]] = []
        current: list[BatchItem] = []

        for call in calls:
            tool = self.registry.get(call.name)
            item = BatchItem(
                call=call,
                claims=tool.resources(call.arguments) if tool is not None else (),
                parallel_safe=bool(tool is not None and tool.parallel_safe),
            )
            if not item.parallel_safe:
                if current:
                    waves.append(current)
                    current = []
                waves.append([item])
                continue
            if any(_items_conflict(item, existing) for existing in current):
                waves.append(current)
                current = [item]
            else:
                current.append(item)

        if current:
            waves.append(current)
        return waves

    def execute(
        self,
        calls: list[ToolCall],
        *,
        on_event: EventCallback | None = None,
        before_execute: Callable[[ToolCall], None] | None = None,
    ) -> dict[str, str]:
        results: dict[str, str] = {}
        waves = self.plan(calls)
        _emit(on_event, "batch_started", total=len(calls), waves=len(waves))

        for wave_index, wave in enumerate(waves):
            _emit(
                on_event,
                "wave_started",
                wave=wave_index + 1,
                size=len(wave),
                parallel=len(wave) > 1,
            )
            if len(wave) == 1:
                item = wave[0]
                results[item.call.id] = self._execute_one(
                    item.call, on_event, before_execute
                )
                continue
            workers = min(self.max_workers, len(wave))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="forge-tool") as pool:
                futures = {
                    pool.submit(
                        self._execute_one, item.call, on_event, before_execute
                    ): item.call
                    for item in wave
                }
                for future in as_completed(futures):
                    call = futures[future]
                    results[call.id] = future.result()

        _emit(on_event, "batch_completed", total=len(calls))
        return results

    def _execute_one(
        self,
        call: ToolCall,
        on_event: EventCallback | None,
        before_execute: Callable[[ToolCall], None] | None,
    ) -> str:
        if before_execute is not None:
            before_execute(call)
        _emit(on_event, "tool_started", call_id=call.id, tool=call.name)
        result = self.registry.execute(call)
        _emit(
            on_event,
            "tool_completed",
            call_id=call.id,
            tool=call.name,
            result_chars=len(result),
            error=result.lstrip().lower().startswith("error"),
        )
        return result


def _items_conflict(left: BatchItem, right: BatchItem) -> bool:
    return any(a.conflicts_with(b) for a in left.claims for b in right.claims)


def _emit(callback: EventCallback | None, event: str, **fields) -> None:
    if callback is None:
        return
    try:
        callback(event, fields)
    except Exception:  # noqa: BLE001, S110 - presentation callbacks cannot fail execution
        pass

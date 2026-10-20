from __future__ import annotations

import pytest

from forgeagent.config import BudgetExceededError, CostBudget
from forgeagent.context import ContextWindow
from forgeagent.contracts import ModelReply, Usage
from forgeagent.policies import PermissionPolicy, PolicyPipeline
from forgeagent.runtime import AgentRuntime
from forgeagent.tools import ToolRegistry
from tests.helpers import ScriptedProvider


def test_budget_blocks_before_model_request(tmp_path):
    provider = ScriptedProvider([ModelReply(content="should not run")])
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(),
        policies=PolicyPipeline([PermissionPolicy(allow_all=True)]),
        context=ContextWindow(4_000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
        budget=CostBudget(0.0001, 0, 1.0),
    )

    with pytest.raises(BudgetExceededError):
        runtime.run("hello")
    assert provider.requests == []


def test_budget_reports_cost_from_actual_usage(tmp_path):
    provider = ScriptedProvider([ModelReply(content="done", usage=Usage(1_000, 500))])
    runtime = AgentRuntime(
        provider=provider,
        registry=ToolRegistry(),
        policies=PolicyPipeline([]),
        context=ContextWindow(4_000, output_reserve=200, safety_margin=100),
        workspace=tmp_path,
        budget=CostBudget(1.0, 2.0, 4.0),
    )

    assert runtime.run("hello") == "done"
    assert runtime.estimated_cost_usd == pytest.approx(0.004)

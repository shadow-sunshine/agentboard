"""研究计划与预算的单元测试（P3）。"""

from __future__ import annotations

import dataclasses

import pytest

from agentboard.runtime.budget import BudgetLedger, ResearchBudget
from agentboard.runtime.errors import BudgetExhaustedError, PlanError, PlanValueError
from agentboard.runtime.plan import (
    STEP_FAILED,
    PlanStep,
    build_plan,
)


def test_build_plan_is_deterministic_and_sorted() -> None:
    first = build_plan(
        brief_text="选型", candidate_ids=["qdrant_local", "pgvector"],
        dimension_ids=["ops_skill_floor", "deployment_complexity"],
    )
    second = build_plan(
        brief_text="选型", candidate_ids=["pgvector", "qdrant_local"],
        dimension_ids=["deployment_complexity", "ops_skill_floor"],
    )
    assert first == second, "同一输入（无论给出顺序）必须得到同一份计划"
    assert [step.candidate_id for step in first.steps] == sorted(
        step.candidate_id for step in first.steps
    )
    # 第一步 = （按序最前的候选, 按序最前的维度），查询词 = "候选 维度"
    assert first.steps[0].candidate_id == "pgvector"
    assert first.steps[0].dimension_id == "deployment_complexity"
    assert first.steps[0].query_terms == "pgvector deployment_complexity"
    # 步骤 id 稳定且唯一
    ids = [step.step_id for step in first.steps]
    assert len(set(ids)) == len(ids) and ids[0] == "step-001"


def test_build_plan_rejects_invalid_identifiers() -> None:
    with pytest.raises(PlanError):
        build_plan(brief_text="选型", candidate_ids=["Bad-ID"], dimension_ids=["ops_skill_floor"])
    with pytest.raises(PlanError):
        build_plan(brief_text="选型", candidate_ids=["pgvector"], dimension_ids=["x"])
    with pytest.raises(PlanError):
        build_plan(brief_text="选型", candidate_ids=[], dimension_ids=["x"])


def test_plan_step_failure_requires_observable_reason() -> None:
    with pytest.raises(PlanError):
        PlanStep(step_id="step-001", candidate_id="pgvector",
                 dimension_id="x", query_terms="q", status=STEP_FAILED, attempts=2)
    step = PlanStep(step_id="step-001", candidate_id="pgvector", dimension_id="x",
                    query_terms="q").mark(status=STEP_FAILED, attempts=2, error="boom")
    assert step.error == "boom"
    # frozen：状态不可被就地改写
    with pytest.raises(dataclasses.FrozenInstanceError):
        step.status = "succeeded"  # type: ignore[misc]


def test_budget_rejects_invalid_configuration() -> None:
    with pytest.raises(PlanValueError):
        ResearchBudget(max_steps=0, max_provider_requests=5,
                       max_total_content_chars=100)
    with pytest.raises(PlanValueError):
        ResearchBudget(max_steps=2, max_provider_requests=-1,
                       max_total_content_chars=100)
    with pytest.raises(PlanValueError):
        ResearchBudget(max_steps=2, max_provider_requests=5,
                       max_total_content_chars=100, request_timeout_seconds=0)


def test_budget_ledger_enforces_every_limit() -> None:
    ledger = BudgetLedger(
        ResearchBudget(max_steps=2, max_provider_requests=3,
                       max_total_content_chars=100, max_retries_per_step=1)
    )
    ledger.start_step()
    ledger.start_step()
    with pytest.raises(BudgetExhaustedError):
        ledger.start_step()

    ledger.consume_request()
    ledger.consume_request()
    ledger.consume_request()
    with pytest.raises(BudgetExhaustedError):
        ledger.consume_request()

    ledger.add_content_chars(60)
    ledger.add_content_chars(40)
    with pytest.raises(BudgetExhaustedError):
        ledger.add_content_chars(1)

    usage = ledger.usage()
    assert usage == {
        "steps_started": 2, "provider_requests": 3,
        "content_chars": 100, "retries": 0,
    }

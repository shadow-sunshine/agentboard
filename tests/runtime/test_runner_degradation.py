"""运行时失败与降级语义测试（P3）：重试、预算、无结果、未知错误、候选降级。"""

from __future__ import annotations

from typing import Any

import pytest

from agentboard.providers.errors import TransportError
from agentboard.runtime.errors import ProviderStepError
from tests.runtime.conftest import (
    CANDIDATES,
    decision_for,
    make_budget,
    make_docs,
    make_runner,
)

PRIMARY_DIM = "deployment_complexity"
SECONDARY_DIM = "ops_skill_floor"


def _four_step_documents() -> dict[str, list[Any]]:
    documents: dict[str, list[Any]] = {}
    for candidate_id in CANDIDATES:
        for dimension_id in (PRIMARY_DIM, SECONDARY_DIM):
            terms = f"{candidate_id} {dimension_id}"
            documents[terms] = make_docs(terms, candidate_id, count=1)
    return documents


def _minimal_decisions() -> dict[tuple[str, str], Any]:
    return dict(
        [
            decision_for("pgvector", PRIMARY_DIM, "low"),
            decision_for("qdrant_local", PRIMARY_DIM, "low"),
        ]
    )


def test_retry_exhaustion_marks_step_failed_with_reason(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """可重试错误重试耗尽 ⇒ 步骤 `failed`（带原因），其余步骤照常，运行完成。"""
    victim_terms = f"pgvector {PRIMARY_DIM}"
    runner, _provider, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_four_step_documents(),
        failures={victim_terms: [TransportError("连接失败"), TransportError("仍然失败")]},
        decisions=_minimal_decisions(),
        budget=make_budget(max_retries_per_step=1),
        run_name="retry-exhausted",
    )
    result = runner.run()
    assert result.outcome == "completed"
    failed = [step for step in result.steps if step.status == "failed"]
    assert len(failed) == 1 and victim_terms in (
        f"{failed[0].candidate_id} {failed[0].dimension_id}"
    )
    assert "TransportError" in failed[0].error
    assert result.budget_usage["retries"] == 1
    assert result.report is not None and result.report.integrity.passed is True


def test_retry_then_success_uses_budgeted_retry(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """第一次失败、重试成功 ⇒ 步骤 `succeeded`，重试计入预算且只重试一次。"""
    victim_terms = f"pgvector {PRIMARY_DIM}"
    runner, provider, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_four_step_documents(),
        failures={victim_terms: [TransportError("抖动")]},
        decisions=_minimal_decisions(),
        budget=make_budget(max_retries_per_step=1),
        run_name="retry-ok",
    )
    result = runner.run()
    assert result.outcome == "completed"
    assert result.budget_usage["retries"] == 1
    assert provider.call_count(victim_terms) == 2
    assert all(step.status == "succeeded" for step in result.steps)


def test_step_budget_exhaustion_skips_remaining_observably(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """步数预算耗尽 ⇒ 剩余步骤 `skipped`（原因可观测），用已有材料完成降级报告。

    计划覆盖注册表全部 6 个维度（2 候选 × 6 = 12 步，按候选排序展开）；
    预算 7 步 ⇒ pgvector 全覆盖 + qdrant 首个维度，剩余 5 步被预算拦下。
    """
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_four_step_documents(), decisions=_minimal_decisions(),
        budget=make_budget(max_steps=7, max_provider_requests=64),
        run_name="budget-steps",
    )
    result = runner.run()
    assert result.outcome == "completed"
    statuses = [step.status for step in result.steps]
    assert statuses == ["succeeded"] * 7 + ["skipped"] * 5
    assert all("budget_exhausted" in step.error for step in result.steps if step.status == "skipped")
    assert result.report is not None and result.report.integrity.passed is True


def test_request_budget_exhaustion_stops_new_requests(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """请求预算耗尽 ⇒ 未发出的步骤 `skipped`，已发步骤不受影响。"""
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_four_step_documents(), decisions=_minimal_decisions(),
        budget=make_budget(max_steps=64, max_provider_requests=7),
        run_name="budget-requests",
    )
    result = runner.run()
    assert result.outcome == "completed"
    assert result.budget_usage["provider_requests"] == 7
    succeeded = [step for step in result.steps if step.status == "succeeded"]
    skipped = [step for step in result.steps if step.status == "skipped"]
    assert len(succeeded) == 7 and len(skipped) == 5
    assert result.report is not None


def test_content_budget_marks_step_skipped(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """内容预算耗尽 ⇒ 该步 `skipped`（原因 budget_exhausted），不写它的快照。"""
    big_docs = {
        f"pgvector {PRIMARY_DIM}": make_docs(
            f"pgvector {PRIMARY_DIM}", "pgvector", count=1,
            body_prefix="超长内容" + "x" * 5000,
        ),
        f"qdrant_local {PRIMARY_DIM}": make_docs(
            f"qdrant_local {PRIMARY_DIM}", "qdrant_local", count=1
        ),
        f"pgvector {SECONDARY_DIM}": make_docs(
            f"pgvector {SECONDARY_DIM}", "pgvector", count=1
        ),
        f"qdrant_local {SECONDARY_DIM}": make_docs(
            f"qdrant_local {SECONDARY_DIM}", "qdrant_local", count=1
        ),
    }
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=big_docs, decisions=_minimal_decisions(),
        budget=make_budget(max_total_content_chars=1000),
        run_name="budget-content",
    )
    result = runner.run()
    skipped = [step for step in result.steps if step.status == "skipped"]
    assert any("content_chars" in step.error or "内容预算" in step.error for step in skipped)
    assert result.outcome in {"completed", "failed"}


def test_no_results_anywhere_is_failed_outcome_not_crash(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """全部查询零结果（含缺口补检）⇒ `outcome=failed`、原因 `no_snapshots`、无报告。"""
    runner, provider, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents={}, decisions=_minimal_decisions(),
        budget=make_budget(max_steps=64, max_provider_requests=64),
        run_name="no-results",
    )
    result = runner.run()
    assert result.outcome == "failed"
    assert result.failure_reason == "no_snapshots"
    assert result.report is None
    assert result.degraded_candidates == CANDIDATES
    # 缺口补检确实发生了（每个首轮缺口都多查了一次变体查询）
    assert provider.call_count(f"pgvector {PRIMARY_DIM} documentation") == 1


def test_insufficient_surviving_candidates_is_observable(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """只有一个候选拿到材料 ⇒ `outcome=failed`（原因 insufficient_candidates）。"""
    documents: dict[str, list[Any]] = {}
    for dimension_id in (PRIMARY_DIM, SECONDARY_DIM):
        documents[f"pgvector {dimension_id}"] = make_docs(
            f"pgvector {dimension_id}", "pgvector", count=1
        )
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=documents, decisions=_minimal_decisions(),
        run_name="one-candidate",
    )
    result = runner.run()
    assert result.outcome == "failed"
    assert result.failure_reason == "insufficient_candidates"
    assert result.report is None
    assert result.degraded_candidates == ("qdrant_local",)


def test_unknown_provider_error_fails_loudly(tmp_path: Any, brief: Any, registry: Any) -> None:
    """未知 Provider 错误 ⇒ `ProviderStepError`（带原始异常链），绝不静默吞掉。"""
    victim_terms = f"pgvector {PRIMARY_DIM}"
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_four_step_documents(),
        failures={victim_terms: [RuntimeError("这不是可重试闭集里的错误")]},
        run_name="unknown-error",
    )
    with pytest.raises(ProviderStepError, match="未知 Provider 错误"):
        runner.run()

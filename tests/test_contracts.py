"""契约层单测：序列化往返、不可变、结构化结果字段完整。"""

from __future__ import annotations

import pytest

from agentboard.contracts import TaskCard, WorkerResult, WorkerTaskInput
from agentboard.workers.synthetic import SyntheticEvidenceFactory


def test_worker_result_round_trip():
    r = WorkerResult(
        task_id="t1",
        agent_name="research",
        facts=("f1", "f2"),
        evidence=(SyntheticEvidenceFactory.make("research", "t1", 0),),
        risks=("r1",),
        open_questions=("q1",),
        confidence=0.85,
        summary="结论",
        session_id="s1",
    )
    back = WorkerResult.from_dict(r.to_dict())
    assert back == r
    assert back.confidence == 0.85
    assert back.facts == ("f1", "f2")
    assert back.evidence == r.evidence


def test_worker_result_rejects_string_evidence_on_construction():
    """P1A-1：字符串证据在构造期即被拒绝，不做隐式升级。"""
    with pytest.raises(ValueError, match="EvidenceRef"):
        WorkerResult(
            task_id="t1",
            agent_name="research",
            facts=(),
            evidence=("来源: 自由文本",),
            risks=(),
            open_questions=(),
            confidence=0.5,
        )


def test_worker_result_rejects_string_evidence_from_dict():
    """P1A-1：反序列化边界同样拒绝，且提示走显式迁移器。"""
    with pytest.raises(ValueError, match="migrate"):
        WorkerResult.from_dict(
            {
                "task_id": "t1",
                "agent_name": "research",
                "evidence": ["来源: 自由文本"],
            }
        )


def test_worker_result_rejects_mixed_evidence_types():
    """不允许 str | EvidenceRef 混合：只要有一个非 EvidenceRef 即整体拒绝。"""
    ref = SyntheticEvidenceFactory.make("research", "t1", 0)
    with pytest.raises(ValueError, match="EvidenceRef"):
        WorkerResult(
            task_id="t1",
            agent_name="research",
            facts=(),
            evidence=(ref, "另一个自由文本证据"),
            risks=(),
            open_questions=(),
            confidence=0.5,
        )


def test_task_card_round_trip_with_result():
    r = WorkerResult(
        task_id="t1",
        agent_name="research",
        facts=("f1",),
        evidence=(),
        risks=(),
        open_questions=(),
        confidence=0.9,
    )
    card = TaskCard(
        task_id="c1",
        title="标题",
        assigned_agent="research",
        goal="目标",
        context="背景",
        depends_on=("up",),
        status="done",
        result=r,
        attempts=1,
    )
    back = TaskCard.from_dict(card.to_dict())
    assert back == card
    assert back.result is not None
    assert back.result.task_id == "t1"


def test_task_card_is_frozen():
    card = TaskCard(task_id="c1", title="t", assigned_agent="research", goal="g", context="c")
    with pytest.raises((AttributeError, TypeError)):
        card.status = "running"  # type: ignore[misc]


def test_worker_task_input_carries_only_structured_upstream():
    r = WorkerResult(
        task_id="up", agent_name="research", facts=(), evidence=(), risks=(), open_questions=(), confidence=1.0
    )
    inp = WorkerTaskInput(
        task_id="c1",
        title="t",
        assigned_agent="research",
        goal="g",
        context="c",
        upstream_results={"up": r},
    )
    assert inp.upstream_results["up"] == r
    back = WorkerTaskInput.from_dict(inp.to_dict())
    assert back.upstream_results["up"].task_id == "up"

"""隔离单测（需求3）：下游只拿到显式声明且已完成的上游结果。"""

from __future__ import annotations

import pytest

from agentboard.contracts import TaskCard, WorkerResult
from agentboard.isolation import build_worker_input, format_worker_prompt
from agentboard.validation import validate_model_board


def _done(task_id: str, agent: str = "research") -> TaskCard:
    r = WorkerResult(
        task_id=task_id,
        agent_name=agent,
        facts=(f"fact of {task_id}",),
        evidence=(),
        risks=(),
        open_questions=(),
        confidence=1.0,
        summary=f"结论 {task_id}",
    )
    return TaskCard(
        task_id=task_id, title="t", assigned_agent=agent, goal="g", context="c", status="done", result=r
    )


def test_downstream_only_sees_declared_upstream(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    compare = next(c for c in cards if c.task_id == "compare_plan")
    done = {
        "research_options": _done("research_options"),
        "another_root": _done("another_root"),  # 未声明，绝不可见
    }
    inp = build_worker_input(compare, done)
    assert set(inp.upstream_results.keys()) == {"research_options"}
    assert "another_root" not in inp.upstream_results


def test_build_worker_input_rejects_missing_upstream(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    compare = next(c for c in cards if c.task_id == "compare_plan")
    # 只提供另一个无关已完成任务，缺少声明的 research_options
    with pytest.raises(RuntimeError):
        build_worker_input(compare, {"another_root": _done("another_root")})


def test_prompt_includes_task_id_and_upstream_summary(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    compare = next(c for c in cards if c.task_id == "compare_plan")
    inp = build_worker_input(compare, {"research_options": _done("research_options")})
    prompt = format_worker_prompt(inp)
    assert "compare_plan" in prompt
    assert "结论 research_options" in prompt

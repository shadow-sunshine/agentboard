"""护栏层单测（需求2）：数量/ID/ Agent 类型/依赖/ DAG 校验。"""

from __future__ import annotations

import pytest

from agentboard.validation import validate_model_board


def _board(task_ids_agents):
    return {
        "tasks": [
            {"task_id": tid, "title": "t", "assigned_agent": ag, "goal": "g", "context": "c", "depends_on": dep}
            for tid, ag, dep in task_ids_agents
        ]
    }


def test_valid_board_passes(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    assert len(cards) == 2
    assert {c.task_id for c in cards} == {"research_options", "compare_plan"}


def test_too_few_tasks_raises():
    with pytest.raises(ValueError):
        validate_model_board(_board([("a", "research", [])]))


def test_too_many_tasks_raises():
    tasks = [(f"t{i}", "research", []) for i in range(7)]
    with pytest.raises(ValueError):
        validate_model_board(_board(tasks))


def test_bad_task_id_raises():
    with pytest.raises(ValueError):
        validate_model_board(_board([("Bad-ID", "research", [])]))


def test_duplicate_task_id_raises():
    with pytest.raises(ValueError):
        validate_model_board(_board([("a", "research", []), ("a", "planner", [])]))


def test_unknown_agent_raises():
    with pytest.raises(ValueError):
        validate_model_board(_board([("a", "unknown_agent", [])]))


def test_self_dependency_raises():
    with pytest.raises(ValueError):
        validate_model_board(_board([("a", "research", ["a"])]))


def test_missing_dependency_raises():
    with pytest.raises(ValueError):
        validate_model_board(_board([("a", "research", ["nope"])]))


def test_cycle_raises():
    with pytest.raises(ValueError):
        validate_model_board(_board([("a", "research", ["b"]), ("b", "planner", ["a"])]))


def test_no_dependency_edge_raises():
    # 两张卡但没有任何依赖边 -> 无法体现多 Agent 协作
    with pytest.raises(ValueError):
        validate_model_board(_board([("a", "research", []), ("b", "planner", [])]))


def test_allowed_agents_can_be_overridden():
    cards = validate_model_board(
        _board([("aa", "custom", []), ("bb", "custom", ["aa"])]),
        allowed_agents={"custom"},
    )
    assert cards[0].assigned_agent == "custom"
    assert cards[1].assigned_agent == "custom"

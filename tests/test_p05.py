"""P0.5 地基加固单测：真正的不可变性、结果归属校验、反序列化边界、重复 Worker 注册。

全部使用 Mock，不依赖真实模型 / ACP / 网络（需求11）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentboard.board import apply_failure, apply_success, prepare_task
from agentboard.contracts import TaskCard, WorkerResult, WorkerTaskInput
from agentboard.events import BoardEvent
from agentboard.validation import validate_model_board
from agentboard.workers import MockWorker, WorkerRegistry
from agentboard.workers.synthetic import SyntheticEvidenceFactory


def _running(cards: list[TaskCard]) -> tuple[list[TaskCard], TaskCard]:
    cards, running, _ = prepare_task(cards, cards[0].task_id)
    return cards, running


def _result(task_id: str, agent_name: str, confidence: float = 0.9) -> WorkerResult:
    return WorkerResult(
        task_id=task_id,
        agent_name=agent_name,
        facts=("f",),
        evidence=SyntheticEvidenceFactory.for_task(agent_name, task_id, count=1),
        risks=(),
        open_questions=(),
        confidence=confidence,
    )


# ---- 1. 内部映射真正只读（不可变性） ----
def test_upstream_results_is_read_only():
    r = WorkerResult(
        task_id="up", agent_name="research", facts=(), evidence=(), risks=(), open_questions=(), confidence=1.0
    )
    inp = WorkerTaskInput(
        task_id="c1", title="t", assigned_agent="research", goal="g", context="c",
        upstream_results={"up": r},
    )
    with pytest.raises(TypeError):
        inp.upstream_results["other"] = r  # MappingProxyType 拒绝写入
    # 只读访问仍然可用
    assert inp.upstream_results["up"] is r
    assert inp.upstream_results.get("up") is r


def test_event_data_is_read_only():
    evt = BoardEvent("task_started", data={"k": "v"})
    with pytest.raises(TypeError):
        evt.data["leak"] = "leaked"
    assert evt.data["k"] == "v"
    assert evt.data.get("k") == "v"


# ---- 2. apply_success 结果归属校验 ----
def test_apply_success_rejects_wrong_task_id(sample_raw_board):
    cards, running = _running(validate_model_board(sample_raw_board))
    with pytest.raises(ValueError):
        apply_success(cards, running, _result("compare_plan", "research"))


def test_apply_success_rejects_wrong_agent(sample_raw_board):
    cards, running = _running(validate_model_board(sample_raw_board))
    with pytest.raises(ValueError):
        apply_success(cards, running, _result(running.task_id, "planner"))


def test_apply_success_rejects_non_running(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    card = cards[0]  # 仍是 todo
    with pytest.raises(ValueError):
        apply_success(cards, card, _result(card.task_id, card.assigned_agent))


def test_apply_success_rejects_stale_card(sample_raw_board):
    cards, running = _running(validate_model_board(sample_raw_board))
    # 构造一个过期版本：状态为 running 但 attempts=0（派发前的快照）
    stale = replace(cards[0], status="running", attempts=0)
    with pytest.raises(ValueError):
        apply_success(cards, stale, _result(running.task_id, running.assigned_agent))


def test_apply_success_accepts_valid(sample_raw_board):
    cards, running = _running(validate_model_board(sample_raw_board))
    new_cards, _ev = apply_success(cards, running, _result(running.task_id, running.assigned_agent))
    done = next(c for c in new_cards if c.task_id == running.task_id)
    assert done.status == "done"


# ---- 3. apply_failure 同样要求 running + 最新版本 ----
def test_apply_failure_rejects_non_running(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    card = cards[0]  # todo
    with pytest.raises(ValueError):
        apply_failure(cards, card, RuntimeError("boom"))


# ---- 4. 反序列化 / 构造边界校验 ----
def test_confidence_out_of_range_rejected():
    with pytest.raises(ValueError):
        WorkerResult(task_id="t", agent_name="research", facts=(), evidence=(), risks=(), open_questions=(), confidence=1.7)
    with pytest.raises(ValueError):
        WorkerResult(task_id="t", agent_name="research", facts=(), evidence=(), risks=(), open_questions=(), confidence=-0.2)


def test_confidence_non_numeric_rejected():
    with pytest.raises(ValueError):
        WorkerResult(task_id="t", agent_name="research", facts=(), evidence=(), risks=(), open_questions=(), confidence="0.9")


def test_negative_attempts_rejected():
    with pytest.raises(ValueError):
        TaskCard(task_id="a1", title="t", assigned_agent="research", goal="g", context="c", attempts=-1)


def test_illegal_status_rejected():
    with pytest.raises(ValueError):
        TaskCard(task_id="a1", title="t", assigned_agent="research", goal="g", context="c", status="whatever")


def test_depends_on_duplicates_rejected():
    with pytest.raises(ValueError):
        TaskCard(task_id="a1", title="t", assigned_agent="research", goal="g", context="c", depends_on=("x", "x"))


def test_from_dict_rejects_bad_confidence():
    with pytest.raises(ValueError):
        WorkerResult.from_dict({"task_id": "t", "agent_name": "research", "confidence": 5})


def test_from_dict_rejects_bad_status():
    with pytest.raises(ValueError):
        TaskCard.from_dict(
            {"task_id": "a1", "title": "t", "assigned_agent": "research", "goal": "g", "context": "c", "status": "zzz"}
        )


def test_from_dict_rejects_negative_attempts():
    with pytest.raises(ValueError):
        TaskCard.from_dict(
            {"task_id": "a1", "title": "t", "assigned_agent": "research", "goal": "g", "context": "c", "attempts": -3}
        )


def test_from_dict_rejects_non_string_field():
    with pytest.raises(ValueError):
        TaskCard.from_dict(
            {"task_id": 123, "title": "t", "assigned_agent": "research", "goal": "g", "context": "c"}
        )


# ---- 5. 重复 Worker 注册拒绝覆盖 ----
def test_duplicate_worker_registration_rejected():
    reg = WorkerRegistry()
    reg.register("research", MockWorker("research"))
    with pytest.raises(ValueError):
        reg.register("research", MockWorker("research"))


def test_empty_worker_name_rejected():
    reg = WorkerRegistry()
    with pytest.raises(ValueError):
        reg.register("", MockWorker("research"))

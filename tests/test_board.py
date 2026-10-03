"""状态机单测（需求6/7）：决策、重试、失败、阻塞、事件有序。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from agentboard import board as boardmod
from agentboard.isolation import build_worker_input
from agentboard.validation import validate_model_board
from agentboard.workers import MockWorker, WorkerRegistry


def test_decide_first_ready_is_root(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    d = boardmod.decide_board_action(cards)
    assert d.action == "dispatch"
    assert d.task_id == "research_options"


def test_decide_finish_when_all_done(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    done = [replace(c, status="done") for c in cards]
    d = boardmod.decide_board_action(done)
    assert d.action == "finish"


def test_decide_blocked_when_failed(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    failed = [replace(cards[0], status="failed")] + list(cards[1:])
    d = boardmod.decide_board_action(failed)
    assert d.action == "blocked"


def test_retry_then_failure_transitions(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    card = cards[0]

    cards, running, ev = boardmod.prepare_task(cards, card.task_id)
    assert running.attempts == 1 and running.status == "running"
    assert ev[0].event_type == "task_started"

    # 第一次失败 -> 回到 todo（retry）
    cards, ev2 = boardmod.apply_failure(cards, running, RuntimeError("boom"))
    retried = next(c for c in cards if c.task_id == card.task_id)
    assert retried.status == "todo" and retried.attempts == 1
    assert ev2[0].event_type == "task_retry"

    # 第二次派发
    cards, running2, _ = boardmod.prepare_task(cards, card.task_id)
    assert running2.attempts == 2

    # 超过上限 -> failed
    cards, ev3 = boardmod.apply_failure(cards, running2, RuntimeError("boom2"))
    failed = next(c for c in cards if c.task_id == card.task_id)
    assert failed.status == "failed"
    assert ev3[0].event_type == "task_failed"


def test_apply_success_writes_structured_result(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    cards, running, _ = boardmod.prepare_task(cards, cards[0].task_id)
    worker = MockWorker("research")
    result = asyncio.run(worker.run(build_worker_input(running, boardmod.done_cards(cards))))
    cards, ev = boardmod.apply_success(cards, running, result)
    done = next(c for c in cards if c.task_id == running.task_id)
    assert done.status == "done"
    assert done.result is not None and done.result.confidence == 0.9
    assert ev[0].event_type == "task_done"


def test_full_dag_run_emits_ordered_events(sample_raw_board):
    cards = validate_model_board(sample_raw_board)
    registry = WorkerRegistry()
    registry.register("research", MockWorker("research"))
    registry.register("planner", MockWorker("planner"))

    events = []
    while True:
        decision = boardmod.decide_board_action(cards)
        if decision.action == "finish":
            break
        if decision.action == "blocked":
            raise AssertionError(f"意外阻塞: {decision.reason}")
        cards, running, ev = boardmod.prepare_task(cards, decision.task_id)
        events += ev
        worker_input = build_worker_input(running, boardmod.done_cards(cards))
        worker = registry.get(running.assigned_agent)
        result = asyncio.run(worker.run(worker_input))
        cards, ev2 = boardmod.apply_success(cards, running, result)
        events += ev2

    assert all(c.status == "done" for c in cards)
    types = [e.event_type for e in events]
    assert types.count("task_started") == 2
    assert types.count("task_done") == 2

    # 串行保证：compare_plan 的 task_started 必在 research_options 的 task_done 之后
    started = {e.task_id: i for i, e in enumerate(events) if e.event_type == "task_started"}
    done = {e.task_id: i for i, e in enumerate(events) if e.event_type == "task_done"}
    assert done["research_options"] < started["compare_plan"]

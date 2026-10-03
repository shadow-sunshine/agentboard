"""规划边界单测（需求1）：主 Agent 只提议，不直接改状态。"""

from __future__ import annotations

import asyncio

from agentboard.events import EVT_BOARD_CREATED
from agentboard.planner import MockPlanner, propose_and_commit


def test_mock_planner_returns_fixed_board(sample_raw_board):
    planner = MockPlanner(sample_raw_board)
    assert asyncio.run(planner.propose("任意目标")) == sample_raw_board


def test_propose_and_commit_returns_validated_cards_and_event(sample_raw_board):
    planner = MockPlanner(sample_raw_board)
    cards, event = asyncio.run(propose_and_commit(planner, "为某服务做技术选型"))
    assert len(cards) == 2
    assert event.event_type == EVT_BOARD_CREATED
    assert event.data["task_count"] == 2


def test_planner_does_not_mutate_input(sample_raw_board):
    import copy

    planner = MockPlanner(sample_raw_board)
    before = copy.deepcopy(sample_raw_board)
    asyncio.run(planner.propose("目标"))
    assert sample_raw_board == before


def test_invalid_proposal_is_rejected_by_guardrail(sample_raw_board):
    bad = dict(sample_raw_board)
    bad["tasks"] = bad["tasks"][:1]  # 只剩 1 张 -> 护栏拒绝
    planner = MockPlanner(bad)
    try:
        asyncio.run(propose_and_commit(planner, "目标"))
        raise AssertionError("应当被护栏拒绝")
    except ValueError:
        pass

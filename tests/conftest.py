"""共享测试夹具。所有测试均用 Mock，不依赖真实模型 / ACP / 网络（需求11）。"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_raw_board() -> dict:
    """一个合法的两卡 DAG：research_options -> compare_plan。"""
    return {
        "tasks": [
            {
                "task_id": "research_options",
                "title": "调研候选方案",
                "assigned_agent": "research",
                "goal": "调研若干开源技术选型方案",
                "context": "公共背景：为某服务做技术选型",
                "depends_on": [],
            },
            {
                "task_id": "compare_plan",
                "title": "对比与推荐",
                "assigned_agent": "planner",
                "goal": "对比各方案并给出推荐结论",
                "context": "公共背景：为某服务做技术选型",
                "depends_on": ["research_options"],
            },
        ]
    }


@pytest.fixture
def three_card_raw_board() -> dict:
    """三卡 DAG：a -> b -> c，用于验证串行推进与隔离。"""
    return {
        "tasks": [
            {"task_id": "a", "title": "A", "assigned_agent": "research", "goal": "A 目标", "context": "bg", "depends_on": []},
            {"task_id": "b", "title": "B", "assigned_agent": "planner", "goal": "B 目标", "context": "bg", "depends_on": ["a"]},
            {"task_id": "c", "title": "C", "assigned_agent": "research", "goal": "C 目标", "context": "bg", "depends_on": ["b"]},
        ]
    }

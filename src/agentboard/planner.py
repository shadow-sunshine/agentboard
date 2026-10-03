"""主 Agent 规划边界（需求1）：主 Agent 只「提议」任务计划，不直接修改运行时状态。

Planner.propose 返回 raw board dict；校验与提交（validate_model_board）由调度核心完成。
这样主 Agent 没有任何写状态的能力，状态变更全部经过护栏层（需求2）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .events import EVT_BOARD_CREATED, BoardEvent
from .validation import validate_model_board


@runtime_checkable
class Planner(Protocol):
    """任何规划器都必须实现 propose。

    约定：propose 只根据目标生成任务计划，不得触碰任务板状态。
    """

    async def propose(self, goal: str) -> dict[str, Any]: ...


class MockPlanner:
    """确定性规划器，返回预设的合法任务板。用于单测与评测（需求8/11）。"""

    def __init__(self, raw_board: dict[str, Any]) -> None:
        self._raw_board = raw_board

    async def propose(self, goal: str) -> dict[str, Any]:
        return self._raw_board


async def propose_and_commit(
    planner: Planner,
    goal: str,
    *,
    allowed_agents: Any = None,
) -> tuple[list[Any], BoardEvent]:
    """「提议 -> 护栏校验 -> 提交」的边界封装。

    返回经校验的 TaskCard 列表与 board_created 事件；planner 本身不写状态。
    """
    raw_board = await planner.propose(goal)
    kwargs = {}
    if allowed_agents is not None:
        kwargs["allowed_agents"] = allowed_agents
    cards = validate_model_board(raw_board, **kwargs)
    event = BoardEvent(EVT_BOARD_CREATED, data={"task_count": len(cards)})
    return cards, event

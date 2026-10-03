"""任务板状态机（需求6/7）。

纯函数式状态演进：所有变更都返回 (新 cards, 事件列表)，不持有可变全局状态，
因此可被确定性单测完整覆盖（需求11）。

调度核心只在这里改状态；主 Agent（Planner）只提议，不调用这些函数（需求1）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .config import (
    MAX_ATTEMPTS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_TODO,
)
from .contracts import BoardDecision, TaskCard, WorkerResult
from .events import (
    EVT_TASK_DONE,
    EVT_TASK_FAILED,
    EVT_TASK_RETRY,
    EVT_TASK_STARTED,
    BoardEvent,
)
from .util import compact_text


def cards_to_state(cards: list[TaskCard]) -> dict[str, Any]:
    return {
        "order": [card.task_id for card in cards],
        "cards": {card.task_id: card.to_dict() for card in cards},
    }


def cards_from_state(value: Any) -> list[TaskCard]:
    if not isinstance(value, dict):
        raise ValueError("board state 必须是 dict")
    raw_cards = value.get("cards", {})
    order = value.get("order", [])
    if not isinstance(raw_cards, dict):
        raise ValueError("board state['cards'] 必须是 dict")
    if not isinstance(order, list):
        order = list(raw_cards.keys())
    cards: list[TaskCard] = []
    for task_id in order:
        raw_card = raw_cards.get(str(task_id))
        if isinstance(raw_card, dict):
            cards.append(TaskCard.from_dict(raw_card))
    return cards


def render_board(cards: list[TaskCard]) -> str:
    lines = ["task_id      | status  | agent          | deps                      | session"]
    for card in cards:
        deps = ",".join(card.depends_on) if card.depends_on else "-"
        session = card.session_id[:8] if card.session_id else "-"
        lines.append(
            f"{card.task_id:12} | {card.status:7} | {card.assigned_agent:14} | "
            f"{deps:25} | {session}"
        )
    return "\n".join(lines)


def done_cards(cards: list[TaskCard]) -> dict[str, TaskCard]:
    return {card.task_id: card for card in cards if card.status == STATUS_DONE}


def update_card(cards: list[TaskCard], updated: TaskCard) -> list[TaskCard]:
    return [updated if card.task_id == updated.task_id else card for card in cards]


def next_ready_card(cards: list[TaskCard]) -> TaskCard | None:
    done_ids = {card.task_id for card in cards if card.status == STATUS_DONE}
    for card in cards:
        if card.status == STATUS_TODO and set(card.depends_on) <= done_ids:
            return card
    return None


def decide_board_action(cards: list[TaskCard]) -> BoardDecision:
    """观察当前任务板，给出唯一决策（不直接改状态）。"""
    failed = [card.task_id for card in cards if card.status == STATUS_FAILED]
    if failed:
        return BoardDecision("blocked", reason=f"存在失败任务:{', '.join(failed)}")

    target = next_ready_card(cards)
    if target is not None:
        action = "retry" if target.attempts > 0 else "dispatch"
        return BoardDecision(action, task_id=target.task_id, reason="依赖已满足")

    unfinished = [card.task_id for card in cards if card.status != STATUS_DONE]
    if not unfinished:
        return BoardDecision("finish", reason="所有任务已完成")
    return BoardDecision(
        "blocked", reason=f"等待依赖或运行中任务:{', '.join(unfinished)}"
    )


def prepare_task(
    cards: list[TaskCard], task_id: str
) -> tuple[list[TaskCard], TaskCard, list[BoardEvent]]:
    """将一张 todo 卡片标记为 running 并递增尝试计数（需求6）。返回运行态卡片与事件。"""
    target = next((c for c in cards if c.task_id == task_id), None)
    if target is None:
        raise ValueError(f"没有任务 {task_id}")
    if target.status != STATUS_TODO:
        raise ValueError(f"{task_id} 当前状态 {target.status}，无法派发")
    running = replace(target, status=STATUS_RUNNING, attempts=target.attempts + 1)
    new_cards = update_card(cards, running)
    event = BoardEvent(
        EVT_TASK_STARTED,
        task_id=running.task_id,
        agent=running.assigned_agent,
        attempt=running.attempts,
        data={"action": "retry" if running.attempts > 1 else "dispatch"},
    )
    return new_cards, running, [event]


def apply_success(
    cards: list[TaskCard], card: TaskCard, result: WorkerResult
) -> tuple[list[TaskCard], list[BoardEvent]]:
    """Worker 成功：写入结构化结果，状态置 done（需求4/5/7）。

    P0.5 强化：结果归属校验，防止「任务 A 被写入任务 B 的结果」这类状态一致性问题。
    """
    if not isinstance(result, WorkerResult):
        raise TypeError("result 必须是 WorkerResult")
    if card.status != STATUS_RUNNING:
        raise ValueError(
            f"{card.task_id} 当前状态 {card.status!r}，无法标记完成（必须处于 running）"
        )
    if result.task_id != card.task_id:
        raise ValueError(
            f"结果 task_id {result.task_id!r} 与任务卡 {card.task_id!r} 不一致，拒绝写入"
        )
    if result.agent_name != card.assigned_agent:
        raise ValueError(
            f"结果 agent_name {result.agent_name!r} 与任务卡 assigned_agent "
            f"{card.assigned_agent!r} 不一致，拒绝写入"
        )
    current = next((c for c in cards if c.task_id == card.task_id), None)
    if current is None:
        raise ValueError(f"任务板中找不到 {card.task_id}")
    if current != card:
        raise ValueError(
            f"{card.task_id} 传入的任务卡不是任务板中的最新版本，可能已被其它步骤更新"
        )
    done = replace(
        card,
        status=STATUS_DONE,
        result=result,
        session_id=result.session_id,
        error="",
    )
    new_cards = update_card(cards, done)
    event = BoardEvent(
        EVT_TASK_DONE,
        task_id=done.task_id,
        agent=done.assigned_agent,
        attempt=done.attempts,
        data={"session_id": result.session_id},
    )
    return new_cards, [event]


def apply_failure(
    cards: list[TaskCard],
    card: TaskCard,
    error: BaseException,
    *,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[list[TaskCard], list[BoardEvent]]:
    """Worker 失败：未达上限则回到 todo（retry），否则进入 failed（需求6/7）。

    P0.5 强化：同样要求传入的 card 处于 running 且为任务板最新版本，
    避免对过期/错误状态卡做重试或失败结算。

    P5：`max_attempts` 可由编排器配置注入（默认仍为全局 MAX_ATTEMPTS，
    既有调用方行为不变）；值必须 >=1，否则视为配置错误直接拒绝。
    """
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError(f"max_attempts 必须是 >=1 的整数，实际为 {max_attempts!r}")
    if card.status != STATUS_RUNNING:
        raise ValueError(
            f"{card.task_id} 当前状态 {card.status!r}，无法标记失败（必须处于 running）"
        )
    current = next((c for c in cards if c.task_id == card.task_id), None)
    if current is None:
        raise ValueError(f"任务板中找不到 {card.task_id}")
    if current != card:
        raise ValueError(
            f"{card.task_id} 传入的任务卡不是任务板中的最新版本，可能已被其它步骤更新"
        )
    error_text = compact_text(str(error), limit=800)
    if card.attempts < max_attempts:
        retried = replace(
            card,
            status=STATUS_TODO,
            error=f"上次尝试失败，等待重试:{error_text}",
            session_id="",
        )
        new_cards = update_card(cards, retried)
        event = BoardEvent(
            EVT_TASK_RETRY,
            task_id=retried.task_id,
            agent=retried.assigned_agent,
            attempt=retried.attempts,
            data={"reason": error_text},
        )
        return new_cards, [event]

    failed = replace(card, status=STATUS_FAILED, error=f"已达到最大尝试次数:{error_text}")
    new_cards = update_card(cards, failed)
    event = BoardEvent(
        EVT_TASK_FAILED,
        task_id=failed.task_id,
        agent=failed.assigned_agent,
        attempt=failed.attempts,
        data={"reason": error_text},
    )
    return new_cards, [event]

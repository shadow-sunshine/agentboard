"""任务板护栏校验（需求2）。

只做真实应用里的护栏，不补预设任务、不固定任务数量：
- 任务数量在 [min, max] 区间
- task_id 符合 lower_snake_case 且唯一
- assigned_agent 在允许集合内
- depends_on 不引用不存在的 id、不自我依赖、至少一条依赖边
- 依赖关系必须是 DAG（无环）

校验输入是「主 Agent 提议」的 raw dict，输出是经收窄的 TaskCard 列表。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import (
    DEFAULT_AGENT_TYPES,
    MAX_TASK_CARDS,
    MIN_TASK_CARDS,
    TASK_ID_PATTERN,
)
from .contracts import TaskCard
from .util import compact_text


def require_text(item: dict[str, Any], key: str, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} 必须是非空字符串")
    return value.strip()


def normalize_depends_on(item: dict[str, Any], label: str) -> tuple[str, ...]:
    raw = item.get("depends_on", [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{label}.depends_on 必须是列表")
    return tuple(str(dep).strip() for dep in raw if str(dep).strip())


def ensure_board_is_dag(cards: list[TaskCard]) -> None:
    """拓扑检测：若存在环或任务永远无法进入 ready，则抛错。"""
    by_id = {card.task_id: card for card in cards}
    resolved: set[str] = set()
    pending = set(by_id)
    while pending:
        ready = [
            tid
            for tid in pending
            if all(dep in resolved for dep in by_id[tid].depends_on)
        ]
        if not ready:
            raise ValueError("任务板依赖存在循环，或某些任务永远无法进入 ready 状态")
        for tid in ready:
            resolved.add(tid)
            pending.remove(tid)


def validate_model_board(
    raw_board: object,
    *,
    allowed_agents: Iterable[str] = DEFAULT_AGENT_TYPES,
    min_cards: int = MIN_TASK_CARDS,
    max_cards: int = MAX_TASK_CARDS,
) -> list[TaskCard]:
    """校验主 Agent 提议的任务板，返回收窄后的 TaskCard 列表。

    参数化 allowed_agents / min_cards / max_cards，便于不同场景注入配置。
    """
    allowed_set = frozenset(allowed_agents)
    raw_items = raw_board.get("tasks", []) if isinstance(raw_board, dict) else []
    if not isinstance(raw_items, list):
        raise ValueError("主 Agent 必须返回 tasks 列表")
    if not (min_cards <= len(raw_items) <= max_cards):
        raise ValueError(
            f"任务数量必须在 {min_cards}-{max_cards} 张之间，实际为 {len(raw_items)}"
        )

    cards: list[TaskCard] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"tasks[{index}] 必须是 dict")
        label = f"tasks[{index}]"

        task_id = require_text(item, "task_id", label)
        if not TASK_ID_PATTERN.match(task_id):
            raise ValueError(f"{label}.task_id 必须是 lower_snake_case，实际为 {task_id!r}")
        if task_id in seen_ids:
            raise ValueError(f"重复 task_id:{task_id}")
        seen_ids.add(task_id)

        assigned_agent = require_text(item, "assigned_agent", label)
        if assigned_agent not in allowed_set:
            raise ValueError(
                f"{task_id}.assigned_agent 只能是 {sorted(allowed_set)}，实际为 {assigned_agent}"
            )

        depends_on = normalize_depends_on(item, label)
        cards.append(
            TaskCard(
                task_id=task_id,
                title=compact_text(require_text(item, "title", label), limit=120),
                assigned_agent=assigned_agent,
                goal=compact_text(require_text(item, "goal", label), limit=360),
                context=compact_text(require_text(item, "context", label), limit=900),
                depends_on=depends_on,
            )
        )

    known_ids = {card.task_id for card in cards}
    if not any(card.depends_on for card in cards):
        raise ValueError("任务板至少需要一条依赖边，否则无法体现多 Agent 协作")
    for card in cards:
        for dep in card.depends_on:
            if dep == card.task_id:
                raise ValueError(f"{card.task_id} 不能依赖自己")
            if dep not in known_ids:
                raise ValueError(f"{card.task_id} 依赖不存在的任务:{dep}")

    ensure_board_is_dag(cards)
    return cards

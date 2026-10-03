"""研究计划：把"任务输入"拆解为可观测、可预算的检索步骤（P3）。

拆解规则（确定性，无启发式）
----------------------------
一步 = 一个 `(candidate_id, dimension_id)` 对，查询词 = `f"{candidate_id} {dimension_id}"`。
候选与维度都**排序后**展开 —— 同一输入永远得到同一份计划、同一批步骤 id。
不做同义词扩展、不做查询改写：那是需要受控生成的部分（P4 候选），
MVP 的计划层只负责"把任务变成可预算、可观测的步骤序列"。

状态机
------
`pending → succeeded | failed | skipped`
（`gap_recheck` 步由 runner 在首轮之后按需追加，同样走这套状态。）
`failed` / `skipped` 必须带 `error` 原因 —— 失败与降级可观测是硬要求。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Self

from ..research.config import CANDIDATE_ID_PATTERN, DIMENSION_ID_PATTERN
from .errors import PlanError

#: 步骤状态闭集
STEP_PENDING = "pending"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

#: 步骤种类闭集
STEP_SEARCH = "search"
STEP_GAP_RECHECK = "gap_recheck"

_VALID_STATUSES = frozenset({STEP_PENDING, STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED})


@dataclass(frozen=True, slots=True)
class PlanStep:
    """计划中的一个检索步骤及其**可观测状态**。"""

    step_id: str
    candidate_id: str
    dimension_id: str
    query_terms: str
    kind: str = STEP_SEARCH
    status: str = STEP_PENDING
    attempts: int = 0
    snapshot_count: int = 0
    error: str = ""

    def __post_init__(self) -> None:
        if not self.step_id:
            raise PlanError("PlanStep.step_id 不能为空")
        if self.status not in _VALID_STATUSES:
            raise PlanError(f"PlanStep.status {self.status!r} 不在闭集 {sorted(_VALID_STATUSES)} 内")
        if self.kind not in {STEP_SEARCH, STEP_GAP_RECHECK}:
            raise PlanError(f"PlanStep.kind {self.kind!r} 非法")
        if self.status in {STEP_FAILED, STEP_SKIPPED} and not self.error:
            raise PlanError(
                f"步骤 {self.step_id} 状态为 {self.status}，必须带 error 原因（可观测性硬要求）"
            )

    def mark(self, *, status: str, attempts: int, snapshot_count: int = 0,
             error: str = "") -> Self:
        """返回带新状态的步骤副本（frozen 风格；非法转换在此被拒）。"""
        return replace(
            self, status=status, attempts=attempts,
            snapshot_count=snapshot_count, error=error,
        )


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """一次研究运行的计划：步骤序列 + 计划级元数据。"""

    brief_text: str
    steps: tuple[PlanStep, ...]

    def __post_init__(self) -> None:
        if not self.brief_text.strip():
            raise PlanError("ResearchPlan.brief_text 不能为空")
        if not self.steps:
            raise PlanError("ResearchPlan.steps 不能为空（至少需要一个检索步骤）")
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise PlanError(f"重复的 step_id：{step.step_id}")
            seen.add(step.step_id)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted({step.candidate_id for step in self.steps}))

    @property
    def dimension_ids(self) -> tuple[str, ...]:
        return tuple(sorted({step.dimension_id for step in self.steps}))


def build_plan(
    *, brief_text: str, candidate_ids: list[str] | tuple[str, ...],
    dimension_ids: list[str] | tuple[str, ...],
) -> ResearchPlan:
    """把任务输入拆解为检索步骤（候选 × 维度，排序展开，确定性）。"""
    candidates = sorted(set(candidate_ids))
    dimensions = sorted(set(dimension_ids))
    if not candidates:
        raise PlanError("candidate_ids 不能为空")
    if not dimensions:
        raise PlanError("dimension_ids 不能为空")
    for candidate_id in candidates:
        if not CANDIDATE_ID_PATTERN.match(candidate_id):
            raise PlanError(
                f"candidate_id {candidate_id!r} 不是合法 lower_snake_case 标识"
            )
    for dimension_id in dimensions:
        if not DIMENSION_ID_PATTERN.match(dimension_id):
            raise PlanError(f"dimension_id {dimension_id!r} 不是合法 lower_snake_case 标识")

    steps: list[PlanStep] = []
    for candidate_id in candidates:
        for dimension_id in dimensions:
            index = len(steps) + 1
            steps.append(
                PlanStep(
                    step_id=f"step-{index:03d}",
                    candidate_id=candidate_id,
                    dimension_id=dimension_id,
                    # 查询词只做空白折叠级别的确定性拼接；改写属 P4 的受控生成
                    query_terms=f"{candidate_id} {dimension_id}",
                )
            )
    return ResearchPlan(brief_text=brief_text, steps=tuple(steps))

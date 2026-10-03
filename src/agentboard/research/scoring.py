"""确定性 utility 打分与排名（P1A-3）。

打分公式：

    score(candidate) = Σ_d  weight_d × utility_d(value_d)
    unsupported 维度 → utility 记 0.0（最差效用），并计入 missing_dimensions

两条刻意的选择：

1. **unsupported 按 0 分参与求和，而不是把该维度从分母里剔除。**
   剔除会让"资料不全"变成"没有劣势"——越缺数据的候选越容易排前面。按 0 计分让缺口
   直译为劣势，同时通过 `missing_dimensions` 与风险规则让缺口可见。
2. **不做四舍五入。** 舍入是一种无依据的有损操作；保留原始 double 值，比较时按
   `(-score, candidate_id)` 排序，配合 `candidate_id` 字典序 tie-break 就足够确定。

浮点确定性：用 `math.fsum` 对**按 dimension_id 排序**的贡献求和。`fsum` 做精确求和，
因此结果不依赖累加顺序，同一输入在任何运行里都得到同一个值。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..util import freeze_payload, thaw_payload
from .errors import ResearchValueError
from .models import ComparisonDimension, ResearchFinding


@dataclass(frozen=True)
class CandidateScore:
    """单个候选的打分明细。`contributions` 含未支撑维度的 0.0，便于逐维度核对。"""

    candidate_id: str
    score: float
    contributions: Mapping[str, float]
    matched_dimensions: tuple[str, ...]
    missing_dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ResearchValueError("CandidateScore.candidate_id 必须是非空字符串")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ResearchValueError("CandidateScore.score 必须是数值")
        if not math.isfinite(float(self.score)):
            raise ResearchValueError(f"CandidateScore.score 必须是有限值，实际为 {self.score!r}")
        if float(self.score) < 0.0:
            # 权重 > 0 且 utility ∈ [0,1]，求和不可能为负；负分说明上游算错了
            raise ResearchValueError(f"CandidateScore.score 不能为负，实际为 {self.score!r}")
        object.__setattr__(self, "score", float(self.score))
        if not isinstance(self.contributions, Mapping):
            raise ResearchValueError("CandidateScore.contributions 必须是 Mapping")
        if list(self.matched_dimensions) != sorted(self.matched_dimensions):
            raise ResearchValueError("matched_dimensions 必须按字典序排列")
        if list(self.missing_dimensions) != sorted(self.missing_dimensions):
            raise ResearchValueError("missing_dimensions 必须按字典序排列")
        covered = set(self.matched_dimensions) | set(self.missing_dimensions)
        if covered != set(self.contributions):
            raise ResearchValueError("contributions 的键必须恰好是 matched ∪ missing")
        object.__setattr__(self, "contributions", freeze_payload(dict(self.contributions)))

    @property
    def coverage_ratio(self) -> float:
        total = len(self.matched_dimensions) + len(self.missing_dimensions)
        return len(self.matched_dimensions) / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "score": self.score,
            "contributions": thaw_payload(self.contributions),
            "matched_dimensions": list(self.matched_dimensions),
            "missing_dimensions": list(self.missing_dimensions),
        }


def score_candidate(
    *,
    candidate_id: str,
    dimensions: Iterable[ComparisonDimension],
    findings_by_dimension: Mapping[str, ResearchFinding],
) -> CandidateScore:
    """给单个候选打分。`findings_by_dimension` 必须已覆盖全部注册维度（含 unsupported 占位）。"""
    ordered = sorted(dimensions, key=lambda dimension: dimension.dimension_id)
    contributions: dict[str, float] = {}
    matched: list[str] = []
    missing: list[str] = []

    for dimension in ordered:
        dimension_id = dimension.dimension_id
        finding = findings_by_dimension.get(dimension_id)
        if finding is not None and finding.is_supported:
            contributions[dimension_id] = dimension.weight * dimension.utility_of(finding.value)
            matched.append(dimension_id)
        else:
            contributions[dimension_id] = 0.0
            missing.append(dimension_id)

    total = math.fsum(contributions[key] for key in sorted(contributions))
    return CandidateScore(
        candidate_id=candidate_id,
        score=total,
        contributions=contributions,
        matched_dimensions=tuple(matched),
        missing_dimensions=tuple(missing),
    )


def score_all(
    *,
    candidate_ids: Iterable[str],
    dimensions: Iterable[ComparisonDimension],
    findings: Iterable[ResearchFinding],
) -> tuple[CandidateScore, ...]:
    """给全部候选打分，输出按 `(-score, candidate_id)` 排序（即最终排名顺序）。"""
    dimension_list = tuple(dimensions)
    by_candidate: dict[str, dict[str, ResearchFinding]] = {}
    for finding in findings:
        bucket = by_candidate.setdefault(finding.candidate_id, {})
        bucket[finding.dimension_id] = finding

    scores = [
        score_candidate(
            candidate_id=candidate_id,
            dimensions=dimension_list,
            findings_by_dimension=by_candidate.get(candidate_id, {}),
        )
        for candidate_id in candidate_ids
    ]
    return tuple(sorted(scores, key=lambda item: (-item.score, item.candidate_id)))


def score_gap(scores: Iterable[CandidateScore]) -> float | None:
    """top1 与 top2 的分差；候选少于两个时返回 None。"""
    ordered = sorted(scores, key=lambda item: (-item.score, item.candidate_id))
    if len(ordered) < 2:
        return None
    return ordered[0].score - ordered[1].score


def render_rationale(score: CandidateScore, rank: int, *, trusted_findings: int) -> str:
    """用固定模板生成推荐理由。

    刻意不允许自由发挥：理由是**结构化事实的渲染**，不是模型的措辞。同一份分数
    永远得到同一句话。
    """
    total = len(score.matched_dimensions) + len(score.missing_dimensions)
    parts = [
        f"综合效用 {score.score:.3f}（排名 {rank}）",
        f"达标维度 {len(score.matched_dimensions)}/{total}",
        f"可信证据支撑 {trusted_findings} 条",
    ]
    if score.missing_dimensions:
        parts.append(f"缺失维度：{', '.join(score.missing_dimensions)}")
    else:
        parts.append("维度覆盖完整")
    return f"{score.candidate_id}：" + "；".join(parts)

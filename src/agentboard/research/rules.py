"""最小风险规则（P1A-3）。

全部是**规则式**审查：只读结构化事实（findings / dimensions / scores / Worker 自报），
不含模型判断、不读时钟、不含随机。同输入必得同输出。

五条规则：

| rule_id | 触发条件 | severity |
|---|---|---|
| `R_UNSUPPORTED_WEIGHTED` | 某候选在某高权重维度（weight ≥ 0.15）上没有依据 | high |
| `R_UNTRUSTED_BACKING` | 某候选没有任何 fixture 证据支撑的结论 | high |
| `R_SINGLE_SOURCE` | 某候选的多条结论全部出自同一段原文 | medium |
| `R_SCORE_GAP_TIGHT` | top1 与 top2 分差 < 0.05，排名不稳健 | medium |
| `R_WORKER_REPORTED` | 上游 WorkerResult.risks 非空，逐条转记录 | low |

`R_UNTRUSTED_BACKING` 是 P1A 计划里 `R_LEGACY_ONLY` 的推广：门槛不是"全是 legacy"，
而是"没有任何可信证据"——synthetic_mock 是确定性占位，同样不能撑起结论。

`R_SINGLE_SOURCE` 判的是**不同 locator 的个数**。fixture 证据的 `source_id` 恒为
`fixture:<candidate_id>`，按 source_id 判会无条件命中，等于噪声；按 locator 判才真正
反映"所有结论是否都出自同一段原文"。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..evidence import is_trusted_evidence
from ..util import freeze_payload
from .config import (
    ORIGIN_RULE_BASED,
    ORIGIN_WORKER_REPORTED,
    RULE_SCORE_GAP_TIGHT,
    RULE_SINGLE_SOURCE,
    RULE_UNSUPPORTED_WEIGHTED,
    RULE_UNTRUSTED_BACKING,
    RULE_WORKER_REPORTED,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
)
from .models import ComparisonDimension, ResearchFinding, RiskItem
from .scoring import CandidateScore, score_gap

#: 加权维度缺失的权重门槛：达到这个权重的维度没有依据，视为高风险
HIGH_WEIGHT_THRESHOLD = 0.15

#: 分数接近的判定边界：top1 与 top2 差小于此值即认为排名不稳健
TIGHT_SCORE_MARGIN = 0.05

RULE_DESCRIPTIONS: Mapping[str, str] = freeze_payload(
    {
        RULE_UNSUPPORTED_WEIGHTED: "高权重维度缺少资料依据",
        RULE_UNTRUSTED_BACKING: "结论缺少可信证据支撑",
        RULE_SINGLE_SOURCE: "全部结论出自同一段原文",
        RULE_SCORE_GAP_TIGHT: "前两名分差过小，排名不稳健",
        RULE_WORKER_REPORTED: "上游 Worker 自报风险",
    }
)


def _rule_risk(
    *,
    rule_id: str,
    severity: str,
    description: str,
    candidate_id: str = "",
    dimension_id: str = "",
    mitigation: str = "",
) -> RiskItem:
    return RiskItem(
        severity=severity,
        origin=ORIGIN_RULE_BASED,
        rule_id=rule_id,
        description=description,
        candidate_id=candidate_id,
        dimension_id=dimension_id,
        mitigation=mitigation,
    )


def _findings_by_candidate(
    findings: Iterable[ResearchFinding],
) -> dict[str, dict[str, ResearchFinding]]:
    buckets: dict[str, dict[str, ResearchFinding]] = {}
    for finding in findings:
        buckets.setdefault(finding.candidate_id, {})[finding.dimension_id] = finding
    return buckets


def review_unsupported_weighted(
    *, candidate_ids: Iterable[str], dimensions: Iterable[ComparisonDimension], findings
) -> list[RiskItem]:
    """高权重维度上没有依据 → high。"""
    heavy = sorted(
        (dimension for dimension in dimensions if dimension.weight >= HIGH_WEIGHT_THRESHOLD),
        key=lambda dimension: dimension.dimension_id,
    )
    buckets = _findings_by_candidate(findings)
    risks: list[RiskItem] = []
    for candidate_id in sorted(candidate_ids):
        bucket = buckets.get(candidate_id, {})
        for dimension in heavy:
            finding = bucket.get(dimension.dimension_id)
            if finding is not None and finding.is_supported:
                continue
            risks.append(
                _rule_risk(
                    rule_id=RULE_UNSUPPORTED_WEIGHTED,
                    severity=SEVERITY_HIGH,
                    candidate_id=candidate_id,
                    dimension_id=dimension.dimension_id,
                    description=(
                        f"{candidate_id} 在权重 {dimension.weight:.2f} 的维度 "
                        f"{dimension.dimension_id} 上没有资料依据"
                    ),
                    mitigation="补充该维度的资料快照后再比较，或调低该维度权重",
                )
            )
    return risks


def review_untrusted_backing(*, candidate_ids: Iterable[str], findings) -> list[RiskItem]:
    """候选没有任何 fixture 证据支撑的结论 → high。"""
    buckets = _findings_by_candidate(findings)
    risks: list[RiskItem] = []
    for candidate_id in sorted(candidate_ids):
        trusted = [
            finding
            for finding in buckets.get(candidate_id, {}).values()
            if finding.is_supported
            and any(is_trusted_evidence(ref) for ref in finding.evidence_refs)
        ]
        if trusted:
            continue
        risks.append(
            _rule_risk(
                rule_id=RULE_UNTRUSTED_BACKING,
                severity=SEVERITY_HIGH,
                candidate_id=candidate_id,
                description=(
                    f"{candidate_id} 的结论没有任何 fixture 资料快照支撑："
                    "synthetic_mock / legacy_text 不能单独支撑推荐"
                ),
                mitigation="为该候选补齐本地资料快照",
            )
        )
    return risks


def review_single_source(*, candidate_ids: Iterable[str], findings) -> list[RiskItem]:
    """多条结论全部出自同一段原文 → medium。"""
    buckets = _findings_by_candidate(findings)
    risks: list[RiskItem] = []
    for candidate_id in sorted(candidate_ids):
        trusted_refs = [
            ref
            for finding in buckets.get(candidate_id, {}).values()
            if finding.is_supported
            for ref in finding.evidence_refs
            if is_trusted_evidence(ref)
        ]
        locators = sorted({ref.locator for ref in trusted_refs})
        if len(trusted_refs) < 2 or len(locators) != 1:
            continue
        risks.append(
            _rule_risk(
                rule_id=RULE_SINGLE_SOURCE,
                severity=SEVERITY_MEDIUM,
                candidate_id=candidate_id,
                description=(
                    f"{candidate_id} 的 {len(trusted_refs)} 条结论全部出自同一段原文"
                    f"（{locators[0]}），结论独立性不足"
                ),
                mitigation="补充不同章节的资料依据，避免单点来源",
            )
        )
    return risks


def review_score_gap(*, scores: Iterable[CandidateScore]) -> list[RiskItem]:
    """前两名分差过小 → medium。"""
    gap = score_gap(scores)
    if gap is None or gap >= TIGHT_SCORE_MARGIN:
        return []
    return [
        _rule_risk(
            rule_id=RULE_SCORE_GAP_TIGHT,
            severity=SEVERITY_MEDIUM,
            description=(
                f"前两名综合效用分差仅 {gap:.4f}（阈值 {TIGHT_SCORE_MARGIN}），排名不稳健"
            ),
            mitigation="补充资料或调整维度权重后再确认结论",
        )
    ]


def convert_worker_risks(*, worker_reported: Mapping[str, tuple[str, ...]]) -> list[RiskItem]:
    """Worker 自报风险逐条转记录 → low（保留原文，不做二次解释）。"""
    risks: list[RiskItem] = []
    for candidate_id in sorted(worker_reported):
        for text in worker_reported[candidate_id]:
            risks.append(
                RiskItem(
                    severity=SEVERITY_LOW,
                    origin=ORIGIN_WORKER_REPORTED,
                    rule_id="",
                    description=text,
                    candidate_id=candidate_id,
                )
            )
    return risks


def review_risks(
    *,
    candidate_ids: Iterable[str],
    dimensions: Iterable[ComparisonDimension],
    findings: Iterable[ResearchFinding],
    scores: Iterable[CandidateScore],
    worker_reported: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[RiskItem, ...]:
    """跑全部规则，输出按 `(severity 降序, risk_id)` 排序的风险列表。"""
    candidate_list = tuple(candidate_ids)
    dimension_list = tuple(dimensions)
    finding_list = tuple(findings)
    score_list = tuple(scores)

    collected: list[RiskItem] = []
    collected.extend(
        review_unsupported_weighted(
            candidate_ids=candidate_list, dimensions=dimension_list, findings=finding_list
        )
    )
    collected.extend(review_untrusted_backing(candidate_ids=candidate_list, findings=finding_list))
    collected.extend(review_single_source(candidate_ids=candidate_list, findings=finding_list))
    collected.extend(review_score_gap(scores=score_list))
    collected.extend(convert_worker_risks(worker_reported=worker_reported or {}))

    return tuple(sorted(collected, key=lambda risk: (SEVERITY_ORDER[risk.severity], risk.risk_id)))

"""最小风险规则（P1A-3）。"""

from __future__ import annotations

import pytest

from agentboard.evidence import SOURCE_FIXTURE, SOURCE_LEGACY, EvidenceRef
from agentboard.research.config import (
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
    UNSUPPORTED_VALUE,
)
from agentboard.research.errors import ResearchValueError
from agentboard.research.models import (
    ComparisonDimension,
    ResearchFinding,
    RiskItem,
    SelectionReport,
)
from agentboard.research.rules import (
    HIGH_WEIGHT_THRESHOLD,
    TIGHT_SCORE_MARGIN,
    convert_worker_risks,
    review_risks,
    review_score_gap,
    review_single_source,
    review_unsupported_weighted,
    review_untrusted_backing,
)
from agentboard.research.scoring import score_all
from agentboard.workers.synthetic import SyntheticEvidenceFactory

LEGACY_REF = EvidenceRef(
    source_type=SOURCE_LEGACY,
    source_id="legacy:t1",
    snapshot_version="legacy.0",
    locator="/legacy/t1/0",
    excerpt="旧的自由文本证据",
    captured_at="1970-01-01",
)
SYNTHETIC_REF = SyntheticEvidenceFactory.make("research", "research__alpha", 0)
FIXTURE_REF = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:alpha",
    snapshot_version="alpha-notes.2026-09-01",
    locator="/sections/0/text",
    excerpt="第一段原文摘录",
    captured_at="2026-09-01",
)
FIXTURE_REF_2 = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:alpha",
    snapshot_version="alpha-notes.2026-09-01",
    locator="/sections/1/text",
    excerpt="第二段原文摘录",
    captured_at="2026-09-01",
)
FIXTURE_REF = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:alpha",
    snapshot_version="alpha-notes.2026-09-01",
    locator="/sections/0/text",
    excerpt="第一段原文摘录",
    captured_at="2026-09-01",
)
FIXTURE_REF_2 = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:alpha",
    snapshot_version="alpha-notes.2026-09-01",
    locator="/sections/1/text",
    excerpt="第二段原文摘录",
    captured_at="2026-09-01",
)


def _dimension(dimension_id: str, weight: float) -> ComparisonDimension:
    return ComparisonDimension(
        dimension_id=dimension_id,
        name=dimension_id,
        description="测试维度",
        weight=weight,
        scale_kind="ordinal",
        scale_labels=("low", "high"),
        utility_map={"low": 1.0, "high": 0.0},
    )


def _supported(
    candidate_id: str, dimension_id: str, *, ref: EvidenceRef = FIXTURE_REF
) -> ResearchFinding:
    return ResearchFinding(
        candidate_id=candidate_id,
        task_id=f"research__{candidate_id}",
        agent_name="research",
        dimension_id=dimension_id,
        claim=f"{dimension_id} 的依据",
        value="low",
        status="supported",
        evidence_refs=(ref,),
        confidence=1.0,
    )


def _unsupported(candidate_id: str, dimension_id: str) -> ResearchFinding:
    return ResearchFinding(
        candidate_id=candidate_id,
        task_id=f"research__{candidate_id}",
        agent_name="research",
        dimension_id=dimension_id,
        claim=f"{dimension_id} 缺少依据",
        value=UNSUPPORTED_VALUE,
        status="unsupported",
        evidence_refs=(),
        confidence=0.0,
    )


# ---- R_UNSUPPORTED_WEIGHTED ----


def test_weighted_unsupported_fires_above_threshold() -> None:
    dimension = _dimension("a_dimension", HIGH_WEIGHT_THRESHOLD)
    findings = (_unsupported("alpha", "a_dimension"),)
    risks = review_unsupported_weighted(
        candidate_ids=["alpha"], dimensions=[dimension], findings=findings
    )
    assert len(risks) == 1
    assert risks[0].rule_id == RULE_UNSUPPORTED_WEIGHTED
    assert risks[0].severity == SEVERITY_HIGH
    assert risks[0].candidate_id == "alpha"
    assert risks[0].dimension_id == "a_dimension"
    assert risks[0].origin == ORIGIN_RULE_BASED


def test_weighted_unsupported_skips_low_weight_dimensions() -> None:
    dimension = _dimension("a_dimension", HIGH_WEIGHT_THRESHOLD - 0.01)
    findings = (_unsupported("alpha", "a_dimension"),)
    assert (
        review_unsupported_weighted(candidate_ids=["alpha"], dimensions=[dimension], findings=findings)
        == []
    )


def test_weighted_unsupported_skips_supported_dimensions() -> None:
    dimension = _dimension("a_dimension", 0.9)
    findings = (_supported("alpha", "a_dimension"),)
    assert (
        review_unsupported_weighted(candidate_ids=["alpha"], dimensions=[dimension], findings=findings)
        == []
    )


# ---- R_UNTRUSTED_BACKING ----


def test_untrusted_backing_fires_without_any_trusted_evidence() -> None:
    findings = (_supported("alpha", "a_dimension", ref=SYNTHETIC_REF),)
    risks = review_untrusted_backing(candidate_ids=["alpha"], findings=findings)
    assert [risk.rule_id for risk in risks] == [RULE_UNTRUSTED_BACKING]
    assert risks[0].severity == SEVERITY_HIGH


def test_untrusted_backing_fires_on_legacy_only() -> None:
    findings = (_supported("alpha", "a_dimension", ref=LEGACY_REF),)
    risks = review_untrusted_backing(candidate_ids=["alpha"], findings=findings)
    assert [risk.rule_id for risk in risks] == [RULE_UNTRUSTED_BACKING]


def test_untrusted_backing_silent_when_fixture_evidence_present() -> None:
    findings = (
        _supported("alpha", "a_dimension", ref=LEGACY_REF),
        _supported("alpha", "b_dimension", ref=FIXTURE_REF),
    )
    assert review_untrusted_backing(candidate_ids=["alpha"], findings=findings) == []


# ---- R_SINGLE_SOURCE ----


def test_single_source_fires_when_all_findings_share_one_locator() -> None:
    findings = (_supported("alpha", "a_dimension"), _supported("alpha", "b_dimension"))
    risks = review_single_source(candidate_ids=["alpha"], findings=findings)
    assert [risk.rule_id for risk in risks] == [RULE_SINGLE_SOURCE]
    assert risks[0].severity == SEVERITY_MEDIUM


def test_single_source_silent_with_distinct_locators() -> None:
    findings = (
        _supported("alpha", "a_dimension", ref=FIXTURE_REF),
        _supported("alpha", "b_dimension", ref=FIXTURE_REF_2),
    )
    assert review_single_source(candidate_ids=["alpha"], findings=findings) == []


def test_single_source_needs_at_least_two_evidence() -> None:
    findings = (_supported("alpha", "a_dimension"),)
    assert review_single_source(candidate_ids=["alpha"], findings=findings) == []


# ---- R_SCORE_GAP_TIGHT ----


def _scores(*values: tuple[str, float]):
    class _Stub:
        def __init__(self, candidate_id: str, score: float) -> None:
            self.candidate_id = candidate_id
            self.score = score

    return tuple(_Stub(candidate_id, score) for candidate_id, score in values)


def test_score_gap_tight_fires_below_margin() -> None:
    risks = review_score_gap(scores=_scores(("alpha", 0.80), ("beta", 0.80 - TIGHT_SCORE_MARGIN / 2)))
    assert [risk.rule_id for risk in risks] == [RULE_SCORE_GAP_TIGHT]
    assert risks[0].severity == SEVERITY_MEDIUM


def test_score_gap_tight_silent_above_margin() -> None:
    assert review_score_gap(scores=_scores(("alpha", 0.80), ("beta", 0.60))) == []


def test_score_gap_tight_silent_with_single_candidate() -> None:
    assert review_score_gap(scores=_scores(("alpha", 0.80))) == []


# ---- R_WORKER_REPORTED ----


def test_worker_reported_risks_are_converted_verbatim() -> None:
    risks = convert_worker_risks(worker_reported={"alpha": ("外部数据未核对",)})
    assert len(risks) == 1
    assert risks[0].origin == ORIGIN_WORKER_REPORTED
    assert risks[0].rule_id == ""
    assert risks[0].severity == SEVERITY_LOW
    assert risks[0].description == "外部数据未核对"
    assert risks[0].candidate_id == "alpha"


def test_worker_reported_conversion_is_sorted_and_deterministic() -> None:
    payload = {"beta": ("b2", "b1"), "alpha": ("a1",)}
    first = convert_worker_risks(worker_reported=payload)
    second = convert_worker_risks(worker_reported=payload)
    assert first == second
    assert [risk.candidate_id for risk in first] == ["alpha", "beta", "beta"]


# ---- RiskItem 局部校验 ----


def test_rule_risk_requires_closed_rule_id() -> None:
    with pytest.raises(ResearchValueError, match="rule_id"):
        RiskItem(
            severity=SEVERITY_HIGH,
            origin=ORIGIN_RULE_BASED,
            rule_id="R_NOT_A_REAL_RULE",
            description="x",
        )


def test_worker_reported_risk_must_leave_rule_id_empty() -> None:
    with pytest.raises(ResearchValueError, match="rule_id"):
        RiskItem(
            severity=SEVERITY_LOW,
            origin=ORIGIN_WORKER_REPORTED,
            rule_id=RULE_WORKER_REPORTED,
            description="x",
        )


def test_risk_severity_and_origin_are_closed_sets() -> None:
    with pytest.raises(ResearchValueError, match="severity"):
        RiskItem(severity="critical", origin=ORIGIN_RULE_BASED, rule_id=RULE_SINGLE_SOURCE, description="x")
    with pytest.raises(ResearchValueError, match="origin"):
        RiskItem(severity=SEVERITY_LOW, origin="model", rule_id="", description="x")


def test_risk_id_is_derived_and_stable() -> None:
    def build() -> RiskItem:
        return RiskItem(
            severity=SEVERITY_MEDIUM,
            origin=ORIGIN_RULE_BASED,
            rule_id=RULE_SCORE_GAP_TIGHT,
            description="分差过小",
        )

    assert build().risk_id == build().risk_id
    assert build().risk_id.startswith("rk_")


def test_forged_risk_id_rejected() -> None:
    with pytest.raises(ResearchValueError, match="risk_id"):
        RiskItem(
            severity=SEVERITY_MEDIUM,
            origin=ORIGIN_RULE_BASED,
            rule_id=RULE_SCORE_GAP_TIGHT,
            description="分差过小",
            risk_id="rk_0000000000000000",
        )


# ---- 汇总 ----


def test_review_risks_orders_by_severity_then_id() -> None:
    dimension = _dimension("a_dimension", 0.9)
    findings = (_unsupported("alpha", "a_dimension"),)
    scores = score_all(
        candidate_ids=["alpha"], dimensions=[dimension], findings=[]
    )
    risks = review_risks(
        candidate_ids=["alpha"],
        dimensions=[dimension],
        findings=findings,
        scores=scores,
        worker_reported={"alpha": ("自报风险",)},
    )
    keys = [(SEVERITY_ORDER[risk.severity], risk.risk_id) for risk in risks]
    assert keys == sorted(keys)
    assert {risk.severity for risk in risks} == {SEVERITY_HIGH, SEVERITY_LOW}


def test_review_risks_is_deterministic() -> None:
    dimension = _dimension("a_dimension", 0.9)
    findings = (_unsupported("alpha", "a_dimension"),)
    scores = score_all(candidate_ids=["alpha"], dimensions=[dimension], findings=[])
    first = review_risks(
        candidate_ids=["alpha"], dimensions=[dimension], findings=findings, scores=scores
    )
    second = review_risks(
        candidate_ids=["alpha"], dimensions=[dimension], findings=findings, scores=scores
    )
    assert first == second


# ---- 真实数据 ----


def test_real_report_raises_only_the_expected_rule(report: SelectionReport) -> None:
    """真实 fixture 上只应触发「加权维度缺失」这一条规则，且精确落在 milvus × scale_ceiling。"""
    rule_ids = {risk.rule_id for risk in report.risks}
    assert rule_ids == {RULE_UNSUPPORTED_WEIGHTED}
    (risk,) = report.risks
    assert risk.candidate_id == "milvus_standalone"
    assert risk.dimension_id == "scale_ceiling"
    assert risk.severity == SEVERITY_HIGH


def test_real_fixture_has_distinct_evidence_locators_per_candidate(
    report: SelectionReport,
) -> None:
    """真实快照足够厚：每个候选的结论来自不同原文，因此不触发单一来源规则。"""
    for candidate_id in report.candidate_ids:
        locators = {
            ref.locator
            for finding in report.findings
            if finding.candidate_id == candidate_id and finding.is_supported
            for ref in finding.evidence_refs
        }
        assert len(locators) >= 2, f"{candidate_id} 的证据来源过窄：{locators}"
    assert RULE_SINGLE_SOURCE not in {risk.rule_id for risk in report.risks}

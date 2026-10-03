"""完整性校验 I1–I9（P1A-3）。

检查函数是**纯函数，只读报告属性**，所以这里用 duck-typed 替身来构造失败场景 ——
否则像 I1/I2/I9 这些"正常路径不可达"的防御性检查根本无法覆盖失败分支。
真实报告上的通过路径另有单独用例。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentboard.evidence import SOURCE_FIXTURE, SOURCE_LEGACY, EvidenceRef
from agentboard.research.config import UNSUPPORTED_VALUE
from agentboard.research.errors import ResearchValueError
from agentboard.research.integrity import (
    INTEGRITY_CODES,
    CheckResult,
    IntegrityReport,
    check_i1_evidence_id_unique,
    check_i2_references_resolvable,
    check_i3_supported_has_non_legacy_evidence,
    check_i4_candidates_have_trusted_backing,
    check_i5_candidate_dimension_matrix_complete,
    check_i6_ranking_is_sound,
    check_i7_recommendation_references_resolvable,
    check_i8_weight_sum,
    check_i9_source_types_allowed,
    verify_integrity,
)
from agentboard.research.models import ComparisonDimension, ResearchFinding, SelectionReport
from agentboard.workers.synthetic import SyntheticEvidenceFactory

FIXTURE_REF = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:alpha",
    snapshot_version="alpha-notes.2026-09-01",
    locator="/sections/0/text",
    excerpt="原始资料摘录",
    captured_at="2026-09-01",
)
LEGACY_REF = EvidenceRef(
    source_type=SOURCE_LEGACY,
    source_id="legacy:t1",
    snapshot_version="legacy.0",
    locator="/legacy/t1/0",
    excerpt="旧的自由文本证据",
    captured_at="1970-01-01",
)
SYNTHETIC_REF = SyntheticEvidenceFactory.make("research", "research__alpha", 0)
OTHER_FIXTURE_REF = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:beta",
    snapshot_version="beta-notes.2026-09-01",
    locator="/sections/1/text",
    excerpt="另一份资料摘录",
    captured_at="2026-09-01",
)


def _ref_stub(**overrides: object) -> SimpleNamespace:
    base = {
        "evidence_id": "ev_stub0000000001",
        "source_type": SOURCE_FIXTURE,
        "source_id": "fixture:alpha",
        "snapshot_version": "alpha-notes.2026-09-01",
        "locator": "/sections/0/text",
        "digest": "0" * 64,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _finding_stub(**overrides: object) -> SimpleNamespace:
    base = {
        "finding_id": "rf_stub0000000001",
        "candidate_id": "alpha",
        "dimension_id": "a_dimension",
        "is_supported": True,
        "evidence_refs": (FIXTURE_REF,),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _rec_stub(**overrides: object) -> SimpleNamespace:
    base = {
        "candidate_id": "alpha",
        "rank": 1,
        "score": 1.0,
        "supporting_finding_ids": ("rf_stub0000000001",),
        "counter_finding_ids": (),
        "risk_ids": (),
        "evidence_refs": (FIXTURE_REF,),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _report_stub(
    *,
    candidate_ids: tuple[str, ...] = ("alpha",),
    dimensions: tuple[object, ...] = (),
    findings: tuple[object, ...] = (),
    recommendations: tuple[object, ...] = (),
    risks: tuple[object, ...] = (),
) -> SimpleNamespace:
    refs: dict[str, object] = {}
    for finding in findings:
        for ref in finding.evidence_refs:
            refs.setdefault(ref.evidence_id, ref)
    for rec in recommendations:
        for ref in rec.evidence_refs:
            refs.setdefault(ref.evidence_id, ref)
    return SimpleNamespace(
        candidate_ids=candidate_ids,
        dimensions=dimensions,
        findings=findings,
        recommendations=recommendations,
        risks=risks,
        evidence_index=refs,
    )


def _dimension(dimension_id: str, weight: float = 1.0) -> ComparisonDimension:
    return ComparisonDimension(
        dimension_id=dimension_id,
        name=dimension_id,
        description="测试维度",
        weight=weight,
        scale_kind="ordinal",
        scale_labels=("low", "high"),
        utility_map={"low": 1.0, "high": 0.0},
    )


def _supported(candidate_id: str, dimension_id: str, refs: tuple[EvidenceRef, ...]) -> ResearchFinding:
    return ResearchFinding(
        candidate_id=candidate_id,
        task_id=f"research__{candidate_id}",
        agent_name="research",
        dimension_id=dimension_id,
        claim=f"{dimension_id} 的依据",
        value="low",
        status="supported",
        evidence_refs=refs,
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


# ---- IntegrityReport / CheckResult 自身 ----


def test_integrity_report_passed_and_failed_codes_are_derived() -> None:
    report = IntegrityReport(
        checks=(CheckResult("I1", True, "ok"), CheckResult("I2", False, "boom"))
    )
    assert report.passed is False
    assert report.failed_codes == ("I2",)
    assert report.detail_of("I2") == "boom"


def test_integrity_report_empty_is_valid() -> None:
    empty = IntegrityReport()
    assert empty.checks == ()
    assert empty.passed is True
    assert empty.failed_codes == ()


def test_integrity_report_rejects_unknown_code() -> None:
    with pytest.raises(ResearchValueError, match="未知的完整性检查码"):
        IntegrityReport(checks=(CheckResult("I99", True, ""),))


def test_integrity_report_rejects_duplicate_code() -> None:
    with pytest.raises(ResearchValueError, match="重复的完整性检查码"):
        IntegrityReport(checks=(CheckResult("I1", True, ""), CheckResult("I1", True, "")))


def test_integrity_report_rejects_non_check_result() -> None:
    with pytest.raises(ResearchValueError, match="CheckResult"):
        IntegrityReport(checks=("I1",))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "passed", "detail"),
    [("", True, ""), ("I1", "yes", ""), ("I1", True, 1)],
)
def test_check_result_validates_fields(code: object, passed: object, detail: object) -> None:
    with pytest.raises(ResearchValueError):
        CheckResult(code=code, passed=passed, detail=detail)  # type: ignore[arg-type]


def test_integrity_codes_are_the_documented_nine() -> None:
    assert INTEGRITY_CODES == ("I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9")


# ---- 真实报告：全部通过 ----


def test_real_report_passes_all_checks(report: SelectionReport) -> None:
    assert report.integrity.passed is True
    assert report.integrity.failed_codes == ()
    assert tuple(check.code for check in report.integrity.checks) == INTEGRITY_CODES


def test_real_report_verify_is_reproducible(report: SelectionReport) -> None:
    assert verify_integrity(report).to_dict() == report.integrity.to_dict()


# ---- I1 ----


def test_i1_passes_when_same_id_always_maps_to_same_content() -> None:
    result = check_i1_evidence_id_unique(_report_stub(findings=(_finding_stub(),)))
    assert result.passed is True


def test_i1_fails_when_same_id_maps_to_different_content() -> None:
    conflicting = _ref_stub(evidence_id="ev_collide0000001", locator="/sections/9/text")
    stub = _report_stub(
        findings=(
            _finding_stub(evidence_refs=(_ref_stub(evidence_id="ev_collide0000001"),)),
            _finding_stub(finding_id="rf_stub0000000002", evidence_refs=(conflicting,)),
        )
    )
    result = check_i1_evidence_id_unique(stub)
    assert result.passed is False
    assert "ev_collide0000001" in result.detail


# ---- I2 ----


def test_i2_fails_on_dangling_reference() -> None:
    stub = _report_stub(findings=(_finding_stub(),))
    stub.evidence_index = {}
    result = check_i2_references_resolvable(stub)
    assert result.passed is False
    assert FIXTURE_REF.evidence_id in result.detail


# ---- I3 ----


def test_i3_fails_for_legacy_only_supported_finding() -> None:
    finding = _supported("alpha", "a_dimension", (LEGACY_REF,))
    result = check_i3_supported_has_non_legacy_evidence(_report_stub(findings=(finding,)))
    assert result.passed is False
    assert finding.finding_id in result.detail


def test_i3_passes_with_synthetic_evidence() -> None:
    """synthetic 至少不是 legacy：它算"有证据"，只是不足以支撑推荐（由 I4 管）。"""
    finding = _supported("alpha", "a_dimension", (SYNTHETIC_REF,))
    assert check_i3_supported_has_non_legacy_evidence(_report_stub(findings=(finding,))).passed is True


# ---- I4 ----


def test_i4_fails_when_candidate_has_no_trusted_backing() -> None:
    finding = _supported("alpha", "a_dimension", (SYNTHETIC_REF,))
    result = check_i4_candidates_have_trusted_backing(_report_stub(findings=(finding,)))
    assert result.passed is False
    assert "alpha" in result.detail


def test_i4_fails_for_unsupported_only_candidate() -> None:
    finding = _unsupported("alpha", "a_dimension")
    result = check_i4_candidates_have_trusted_backing(_report_stub(findings=(finding,)))
    assert result.passed is False


def test_i4_passes_with_fixture_evidence() -> None:
    finding = _supported("alpha", "a_dimension", (FIXTURE_REF,))
    assert check_i4_candidates_have_trusted_backing(_report_stub(findings=(finding,))).passed is True


# ---- I5 ----


def test_i5_fails_when_matrix_incomplete() -> None:
    stub = _report_stub(
        dimensions=(_dimension("a_dimension"), _dimension("b_dimension")),
        findings=(_supported("alpha", "a_dimension", (FIXTURE_REF,)),),
    )
    result = check_i5_candidate_dimension_matrix_complete(stub)
    assert result.passed is False
    assert "alpha/b_dimension" in result.detail


def test_i5_passes_when_unsupported_placeholder_present() -> None:
    stub = _report_stub(
        dimensions=(_dimension("a_dimension"), _dimension("b_dimension")),
        findings=(
            _supported("alpha", "a_dimension", (FIXTURE_REF,)),
            _unsupported("alpha", "b_dimension"),
        ),
    )
    assert check_i5_candidate_dimension_matrix_complete(stub).passed is True


# ---- I6 ----


def test_i6_fails_when_ranks_not_contiguous() -> None:
    stub = _report_stub(
        recommendations=(_rec_stub(rank=1), _rec_stub(candidate_id="beta", rank=3, score=0.5))
    )
    result = check_i6_ranking_is_sound(stub)
    assert result.passed is False
    assert "1..2" in result.detail


def test_i6_fails_when_scores_not_monotonic() -> None:
    stub = _report_stub(
        recommendations=(
            _rec_stub(rank=1, score=0.1),
            _rec_stub(candidate_id="beta", rank=2, score=0.9),
        )
    )
    result = check_i6_ranking_is_sound(stub)
    assert result.passed is False
    assert "单调" in result.detail


def test_i6_passes_for_ties() -> None:
    stub = _report_stub(
        recommendations=(_rec_stub(rank=1, score=0.5), _rec_stub(candidate_id="beta", rank=2, score=0.5))
    )
    assert check_i6_ranking_is_sound(stub).passed is True


# ---- I7 ----


def test_i7_fails_on_dangling_finding_reference() -> None:
    stub = _report_stub(recommendations=(_rec_stub(supporting_finding_ids=("rf_missing0000001",)),))
    result = check_i7_recommendation_references_resolvable(stub)
    assert result.passed is False
    assert "rf_missing0000001" in result.detail


def test_i7_fails_on_dangling_risk_reference() -> None:
    stub = _report_stub(recommendations=(_rec_stub(risk_ids=("rk_missing0000001",)),))
    result = check_i7_recommendation_references_resolvable(stub)
    assert result.passed is False
    assert "rk_missing0000001" in result.detail


def test_i7_passes_when_references_resolve() -> None:
    finding = _finding_stub()
    risk = SimpleNamespace(risk_id="rk_present0000001")
    stub = _report_stub(
        findings=(finding,),
        recommendations=(
            _rec_stub(supporting_finding_ids=(finding.finding_id,), risk_ids=(risk.risk_id,)),
        ),
        risks=(risk,),
    )
    assert check_i7_recommendation_references_resolvable(stub).passed is True


# ---- I8 ----


def test_i8_fails_when_weights_do_not_sum_to_one() -> None:
    stub = _report_stub(dimensions=(_dimension("a_dimension", 0.5), _dimension("b_dimension", 0.4)))
    result = check_i8_weight_sum(stub)
    assert result.passed is False
    assert "0.9" in result.detail


def test_i8_passes_for_balanced_weights() -> None:
    stub = _report_stub(dimensions=(_dimension("a_dimension", 0.5), _dimension("b_dimension", 0.5)))
    assert check_i8_weight_sum(stub).passed is True


# ---- I9 ----


def test_i9_fails_on_unallowed_source_type() -> None:
    rogue = _ref_stub(evidence_id="ev_rogue000000001", source_type="web")
    stub = _report_stub(findings=(_finding_stub(evidence_refs=(rogue,)),))
    result = check_i9_source_types_allowed(stub)
    assert result.passed is False
    assert "web" in result.detail


def test_i9_passes_on_allowed_source_types() -> None:
    stub = _report_stub(findings=(_finding_stub(),))
    result = check_i9_source_types_allowed(stub)
    assert result.passed is True
    assert "fixture_snapshot" in result.detail


# ---- verify_integrity 汇总 ----


def test_verify_integrity_collects_multiple_failures() -> None:
    stub = _report_stub(
        dimensions=(_dimension("a_dimension", 0.5), _dimension("b_dimension", 0.4)),
        findings=(_unsupported("alpha", "a_dimension"),),
        recommendations=(_rec_stub(supporting_finding_ids=("rf_missing0000001",)),),
    )
    report = verify_integrity(stub)
    assert report.passed is False
    assert {"I4", "I5", "I7", "I8"} <= set(report.failed_codes)
    assert tuple(check.code for check in report.checks) == INTEGRITY_CODES

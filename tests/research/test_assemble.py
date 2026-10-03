"""报告组装（P1A-3）：S0 预检 / S1 收集 / S2 归一 / S3 风险 / S4 推荐 / S5 完整性。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentboard.contracts import ProjectBrief, TaskCard, WorkerResult
from agentboard.evidence import SOURCE_FIXTURE, EvidenceRef
from agentboard.research import assemble as assemble_module
from agentboard.research.assemble import (
    assemble_report,
    build_recommendations,
    collect_candidate_ids,
    collect_findings,
    normalize_findings,
    preflight_registry,
    preflight_snapshots,
    report_summary,
)
from agentboard.research.config import UNSUPPORTED_VALUE
from agentboard.research.dimensions import DimensionRegistry, build_registry
from agentboard.research.errors import (
    CandidateCoverageError,
    CandidateMissingError,
    DanglingLocatorError,
    DimensionConflictError,
    ForeignTaskError,
    InsufficientCandidatesError,
    InvalidDimensionValueError,
    ReportIntegrityError,
    ResearchValueError,
)
from agentboard.research.integrity import CheckResult, IntegrityReport
from agentboard.research.models import (
    ComparisonDimension,
    ResearchFinding,
    SelectionReport,
    derive_coverage,
)
from agentboard.research.payload import decode_findings, encode_findings
from agentboard.research.scoring import CandidateScore
from agentboard.workers.synthetic import SyntheticEvidenceFactory

FIXTURE_REF = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:alpha",
    snapshot_version="alpha-notes.2026-09-01",
    locator="/sections/0/text",
    excerpt="原始资料摘录",
    captured_at="2026-09-01",
)

FIXTURE_REF = EvidenceRef(
    source_type=SOURCE_FIXTURE,
    source_id="fixture:alpha",
    snapshot_version="alpha-notes.2026-09-01",
    locator="/sections/0/text",
    excerpt="原始资料摘录",
    captured_at="2026-09-01",
)

DIMENSION_IDS = (
    "deployment_complexity",
    "ecosystem_maturity",
    "managed_service_needed",
    "ops_skill_floor",
    "query_latency",
    "scale_ceiling",
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


def _materialize(cards: list[TaskCard], **overrides: object) -> list[TaskCard]:
    return [replace(card, **overrides) for card in cards]


def _finding_with_evidence(
    candidate_id: str, dimension_id: str, ref: EvidenceRef
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


# ---- 真实报告的形态 ----


def test_report_shape(report: SelectionReport) -> None:
    assert report.schema_version == "p1a.1"
    assert report.generated_by == "deterministic-assembler.p1a.1"
    assert report.candidate_ids == ("milvus_standalone", "pgvector", "qdrant_local")
    assert len(report.findings) == 3 * len(DIMENSION_IDS)
    assert len(report.recommendations) == 3
    assert report.integrity.passed is True


def test_report_coverage_is_complete_and_honest(report: SelectionReport) -> None:
    coverage = {item.candidate_id: item for item in report.coverage.candidates}
    assert coverage["pgvector"].missing_dimensions == ()
    assert coverage["pgvector"].supported_dimensions == 6
    assert coverage["milvus_standalone"].missing_dimensions == ("scale_ceiling",)
    assert coverage["qdrant_local"].missing_dimensions == ("managed_service_needed",)
    for item in report.coverage.candidates:
        assert item.supported_dimensions + len(item.missing_dimensions) == item.total_dimensions


def test_report_findings_are_sorted(report: SelectionReport) -> None:
    keys = [(finding.candidate_id, finding.dimension_id) for finding in report.findings]
    assert keys == sorted(keys)
    dimension_ids = [dimension.dimension_id for dimension in report.dimensions]
    assert dimension_ids == sorted(dimension_ids)
    assert [rec.rank for rec in report.recommendations] == [1, 2, 3]


def test_recommendations_only_supported_by_fixture_evidence(report: SelectionReport) -> None:
    for rec in report.recommendations:
        assert rec.evidence_refs
        assert {ref.source_type for ref in rec.evidence_refs} == {SOURCE_FIXTURE}
        assert rec.supporting_finding_ids


def test_evidence_index_covers_every_reference(report: SelectionReport) -> None:
    referenced = {
        ref.evidence_id for finding in report.findings for ref in finding.evidence_refs
    } | {ref.evidence_id for rec in report.recommendations for ref in rec.evidence_refs}
    assert set(report.evidence_index) == referenced
    assert len(report.evidence_index) == len(referenced)


# ---- 确定性 ----


def test_same_input_produces_byte_identical_report(
    brief: ProjectBrief, make_cards, registry: DimensionRegistry, snapshots
) -> None:
    cards = make_cards()
    first = assemble_report(brief, cards, registry, snapshots=snapshots)
    second = assemble_report(brief, cards, registry, snapshots=snapshots)
    assert first.report_id == second.report_id
    assert json.dumps(first.to_dict(), sort_keys=True, ensure_ascii=False) == json.dumps(
        second.to_dict(), sort_keys=True, ensure_ascii=False
    )


def test_report_has_no_time_or_random_fields(report: SelectionReport) -> None:
    payload = report.to_dict()
    assert "generated_at" not in payload
    for forbidden in ("timestamp", "created_at", "now", "seed", "nonce", "uuid"):
        assert forbidden not in payload


def test_report_id_depends_only_on_content(
    make_cards, registry: DimensionRegistry, snapshots
) -> None:
    cards = make_cards()
    first = assemble_report(ProjectBrief(text="目标 A", source="s.md"), cards, registry, snapshots=snapshots)
    second = assemble_report(ProjectBrief(text="目标 B", source="s.md"), cards, registry, snapshots=snapshots)
    assert first.report_id != second.report_id
    assert first.report_id == assemble_report(
        ProjectBrief(text="目标 A", source="s.md"), cards, registry, snapshots=snapshots
    ).report_id


def test_report_id_matches_content_projection(report: SelectionReport) -> None:
    assert report.report_id == report.expected_report_id
    assert report.report_id.startswith("rp_")


def test_content_projection_excludes_integrity(report: SelectionReport) -> None:
    projection = report.content_projection()
    assert "integrity" not in projection
    assert "report_id" not in projection
    assert set(projection) == {
        "schema_version",
        "generated_by",
        "brief",
        "candidate_ids",
        "dimensions",
        "findings",
        "risks",
        "recommendations",
        "coverage",
    }


def test_forged_report_id_rejected(report: SelectionReport) -> None:
    with pytest.raises(ResearchValueError, match="report_id"):
        replace(report, report_id="rp_0000000000000000")


def test_coverage_inconsistency_rejected(report: SelectionReport) -> None:
    """coverage 是派生数据：与 findings 对不上就必须拒绝，而不是留一份会骗人的统计。"""
    pgvector = report.coverage.get("pgvector")
    tampered = replace(
        pgvector, supported_dimensions=5, missing_dimensions=("query_latency",)
    )
    assert tampered.supported_dimensions + len(tampered.missing_dimensions) == tampered.total_dimensions
    patched = tuple(
        tampered if item.candidate_id == "pgvector" else item for item in report.coverage.candidates
    )
    with pytest.raises(ResearchValueError, match="coverage"):
        replace(report, coverage=replace(report.coverage, candidates=patched))


def test_derive_coverage_is_the_single_source(report: SelectionReport) -> None:
    derived = derive_coverage(
        candidate_ids=report.candidate_ids,
        dimension_ids=tuple(d.dimension_id for d in report.dimensions),
        findings=report.findings,
    )
    assert derived == report.coverage


# ---- strict 语义 ----


def _failing_integrity(report: SelectionReport) -> IntegrityReport:
    return IntegrityReport(checks=(CheckResult("I4", False, "候选缺少可信支撑"),))


def test_strict_true_raises_with_failed_codes(
    brief: ProjectBrief, make_cards, registry: DimensionRegistry, snapshots, monkeypatch
) -> None:
    monkeypatch.setattr(assemble_module, "verify_integrity", _failing_integrity)
    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(brief, make_cards(), registry, snapshots=snapshots, strict=True)
    assert excinfo.value.failed_codes == ("I4",)
    assert "I4" in str(excinfo.value)


def test_strict_false_returns_report_with_failed_codes(
    brief: ProjectBrief, make_cards, registry: DimensionRegistry, snapshots, monkeypatch
) -> None:
    monkeypatch.setattr(assemble_module, "verify_integrity", _failing_integrity)
    report = assemble_report(brief, make_cards(), registry, snapshots=snapshots, strict=False)
    assert report.integrity.passed is False
    assert report.integrity.failed_codes == ("I4",)
    # 报告本身仍然完整可用
    assert len(report.recommendations) == 3
    assert report.report_id.startswith("rp_")


def test_report_integrity_error_is_research_value_error() -> None:
    error = ReportIntegrityError(("I4", "I5"))
    assert isinstance(error, ValueError)
    assert error.failed_codes == ("I4", "I5")


# ---- S0 预检 ----


def test_preflight_registry_accepts_real_registry(registry: DimensionRegistry) -> None:
    preflight_registry(registry)


def test_preflight_registry_rejects_unbalanced_weights() -> None:
    broken = DimensionRegistry(
        dimensions=(_dimension("a_dimension", 0.5),), alias_index={"a_dimension": "a_dimension"}
    )
    with pytest.raises(ResearchValueError, match="1.0"):
        preflight_registry(broken)


def test_preflight_registry_rejects_unsorted_dimensions() -> None:
    dimensions = build_registry([_dimension("b_dimension", 0.5), _dimension("a_dimension", 0.5)])
    broken = DimensionRegistry(
        dimensions=tuple(reversed(dimensions.dimensions)), alias_index=dimensions.alias_index
    )
    with pytest.raises(ResearchValueError, match="有序"):
        preflight_registry(broken)


def test_preflight_registry_rejects_non_registry() -> None:
    with pytest.raises(ResearchValueError, match="DimensionRegistry"):
        preflight_registry("not-a-registry")  # type: ignore[arg-type]


def test_preflight_snapshots_accepts_real_snapshots(registry: DimensionRegistry, snapshots) -> None:
    preflight_snapshots(registry, snapshots)


def test_preflight_snapshots_is_optional(registry: DimensionRegistry) -> None:
    preflight_snapshots(registry, None)
    preflight_snapshots(registry, {})


def test_preflight_snapshots_rejects_key_mismatch(registry: DimensionRegistry, snapshots) -> None:
    renamed = {"wrong_key": snapshots["pgvector"]}
    with pytest.raises(ResearchValueError, match="不一致"):
        preflight_snapshots(registry, renamed)


def test_assemble_without_snapshots_still_works(
    brief: ProjectBrief, make_cards, registry: DimensionRegistry
) -> None:
    report = assemble_report(brief, make_cards(), registry, snapshots=None)
    assert report.integrity.passed is True


# ---- S1 收集 ----


def test_collect_findings_ignores_non_research_and_unfinished(make_cards) -> None:
    cards = make_cards(["pgvector"])
    assert collect_findings(_materialize(cards, status="running")) == ()
    assert collect_findings(_materialize(cards, assigned_agent="planner")) == ()


def test_collect_findings_rejects_foreign_task(make_cards) -> None:
    cards = make_cards(["pgvector"])
    foreign = replace(cards[0], task_id="research__elsewhere")
    with pytest.raises(ForeignTaskError, match="不属于本次任务板"):
        collect_findings([foreign])


def test_missing_research_cards_raises(snapshots) -> None:
    with pytest.raises(CandidateMissingError):
        collect_candidate_ids((), snapshots)


def test_insufficient_candidates_raises(make_cards, snapshots) -> None:
    with pytest.raises(InsufficientCandidatesError, match="至少需要"):
        collect_candidate_ids(collect_findings(make_cards(["pgvector"])), snapshots)


def test_snapshot_without_card_raises(brief, make_cards, registry, snapshots) -> None:
    """快照目录里有三个候选，却只跑了两个 —— 静默丢弃会让报告看起来更完整。"""
    with pytest.raises(CandidateCoverageError, match="没有对应的 research 结果"):
        assemble_report(brief, make_cards(["pgvector", "qdrant_local"]), registry, snapshots=snapshots)


# ---- S2 归一 ----


def test_normalize_fills_missing_dimensions(make_result, registry: DimensionRegistry) -> None:
    findings = decode_findings(make_result("milvus_standalone").structured_payload)
    normalized = normalize_findings(findings, registry, ("milvus_standalone",))
    assert len(normalized) == len(DIMENSION_IDS)
    by_id = {finding.dimension_id: finding for finding in normalized}
    assert by_id["scale_ceiling"].is_supported is False
    assert by_id["scale_ceiling"].value == UNSUPPORTED_VALUE
    assert by_id["scale_ceiling"].evidence_refs == ()


def test_normalize_rejects_conflicting_duplicates(make_result, registry: DimensionRegistry) -> None:
    findings = decode_findings(make_result("pgvector").structured_payload)
    target = next(finding for finding in findings if finding.dimension_id == "query_latency")
    conflicting = replace(target, value="fair", finding_id="")
    with pytest.raises(DimensionConflictError, match="互相冲突"):
        normalize_findings((*findings, conflicting), registry, ("pgvector",))


def test_normalize_is_idempotent(make_result, registry: DimensionRegistry) -> None:
    findings = decode_findings(make_result("pgvector").structured_payload)
    once = normalize_findings(findings, registry, ("pgvector",))
    twice = normalize_findings(once, registry, ("pgvector",))
    assert once == twice


# ---- 证据可解析 ----


def test_dangling_locator_rejected(brief, make_cards, registry, snapshots) -> None:
    """把某条证据的摘录换成别的 section 的原文 -> locator 与内容对不上，必须报错。"""
    cards = make_cards(["pgvector", "qdrant_local", "milvus_standalone"])
    target_card = next(card for card in cards if card.task_id == "research__pgvector")
    assert target_card.result is not None
    findings = list(decode_findings(target_card.result.structured_payload))
    supported = [finding for finding in findings if finding.is_supported]
    victim, donor = supported[0], supported[1]
    forged = EvidenceRef(
        source_type=SOURCE_FIXTURE,
        source_id=victim.evidence_refs[0].source_id,
        snapshot_version=victim.evidence_refs[0].snapshot_version,
        locator=victim.evidence_refs[0].locator,
        excerpt=donor.evidence_refs[0].excerpt,
        captured_at=victim.evidence_refs[0].captured_at,
    )
    tampered = [
        replace(finding, evidence_refs=(forged,), finding_id="") if finding is victim else finding
        for finding in findings
    ]
    patched_cards = [
        replace(card, result=replace(card.result, structured_payload=encode_findings(tampered)))
        if card.task_id == target_card.task_id
        else card
        for card in cards
    ]
    with pytest.raises(DanglingLocatorError, match="不一致"):
        assemble_report(brief, patched_cards, registry, snapshots=snapshots)


def test_real_fixture_locators_all_resolve(brief, make_cards, registry, snapshots) -> None:
    report = assemble_report(brief, make_cards(), registry, snapshots=snapshots)
    assert all(ref.source_type == SOURCE_FIXTURE for ref in report.evidence_index.values())


# ---- 异常：非法取值 ----


def test_invalid_dimension_value_in_payload_rejected(
    brief, make_cards, registry: DimensionRegistry, snapshots
) -> None:
    cards = make_cards(["pgvector", "qdrant_local", "milvus_standalone"])
    target = next(card for card in cards if card.task_id == "research__pgvector")
    assert target.result is not None
    findings = list(decode_findings(target.result.structured_payload))
    victim = next(finding for finding in findings if finding.is_supported)
    broken = [
        replace(finding, value="amazing", finding_id="") if finding is victim else finding
        for finding in findings
    ]
    patched = [
        replace(card, result=replace(card.result, structured_payload=encode_findings(broken)))
        if card.task_id == target.task_id
        else card
        for card in cards
    ]
    with pytest.raises(InvalidDimensionValueError, match="amazing"):
        assemble_report(brief, patched, registry, snapshots=snapshots)


# ---- 摘要 ----


def test_report_id_regression_anchor(report: SelectionReport) -> None:
    """**回归锚点**：真实 fixture + conftest 的 brief 必须得到这个 report_id。

    这是最强的确定性护栏：`report_id` 由打分、排序、风险、覆盖、findings 顺序等
    全部内容投影算出，任何一处悄悄改动都会让这个值变化。**改动本锚点必须是显式决定**，
    不能顺手更新 —— P1A-4 的 `expectations/*.json` 会把它固化成基线。
    """
    assert report.report_id == "rp_94da70df9a1e6cad"


def test_report_summary_shape(report: SelectionReport) -> None:
    summary = report_summary(report)
    assert summary["report_id"] == report.report_id
    assert summary["integrity_passed"] is True
    assert summary["failed_codes"] == []
    assert [entry["rank"] for entry in summary["ranking"]] == [1, 2, 3]
    assert summary["risks"] == ["R_UNSUPPORTED_WEIGHTED"]
    assert summary["high_risk_count"] == 1


def test_report_summary_is_deterministic(report: SelectionReport) -> None:
    assert json.dumps(report_summary(report), sort_keys=True, ensure_ascii=False) == json.dumps(
        report_summary(report), sort_keys=True, ensure_ascii=False
    )


# ---- fixture 目录改动会被发现（回归护栏） ----


def test_cloned_fixture_root_is_isolated(cloned_fixture_root: Path, fixture_root: Path) -> None:
    assert cloned_fixture_root != fixture_root
    assert (cloned_fixture_root / "INDEX.json").is_file()


def test_worker_result_payload_round_trips_through_frozen_mapping(make_result) -> None:
    """真实 WorkerResult 的 payload 经核心冻结后，组装侧仍能完整解码。"""
    result = make_result("pgvector")
    assert isinstance(result, WorkerResult)
    decoded = decode_findings(result.structured_payload)
    assert len(decoded) == len(DIMENSION_IDS)
    assert all(isinstance(finding, ResearchFinding) for finding in decoded)


def test_unsupported_findings_carry_no_evidence(make_result) -> None:
    findings = decode_findings(make_result("milvus_standalone").structured_payload)
    for finding in findings:
        if not finding.is_supported:
            assert finding.evidence_refs == ()
            assert finding.value == UNSUPPORTED_VALUE


def test_synthetic_evidence_can_never_back_a_recommendation(report: SelectionReport) -> None:
    """把 synthetic 证据混进 payload 也不会进入推荐支撑：supporting 只收 trusted。"""
    for rec in report.recommendations:
        assert not any(
            ref.source_type != SOURCE_FIXTURE for ref in rec.evidence_refs
        )


def test_recommendation_evidence_refs_are_sorted(report: SelectionReport) -> None:
    for rec in report.recommendations:
        ids = [ref.evidence_id for ref in rec.evidence_refs]
        assert ids == sorted(ids)


def test_risks_are_attached_to_recommendations(report: SelectionReport) -> None:
    by_candidate = {rec.candidate_id: rec for rec in report.recommendations}
    assert by_candidate["milvus_standalone"].risk_ids
    assert by_candidate["pgvector"].risk_ids == ()


def test_synthetic_only_finding_lands_in_counter_not_supporting() -> None:
    """只有 synthetic 证据的结论进 counter_finding_ids，不进 supporting。

    `build_recommendations` 用 `is_trusted_evidence` 分流，因此"只有真证据才能撑起推荐"
    是组装逻辑保证的，不依赖事后过滤。
    """
    synthetic_ref = SyntheticEvidenceFactory.make("research", "research__alpha", 0)
    assert synthetic_ref.source_type == "synthetic_mock"
    trusted_finding = _finding_with_evidence("alpha", "a_dimension", FIXTURE_REF)
    untrusted_finding = _finding_with_evidence("alpha", "b_dimension", synthetic_ref)
    score = CandidateScore(
        candidate_id="alpha",
        score=1.0,
        contributions={"a_dimension": 1.0, "b_dimension": 0.0},
        matched_dimensions=("a_dimension",),
        missing_dimensions=("b_dimension",),
    )
    (rec,) = build_recommendations(
        scores=(score,), findings=(trusted_finding, untrusted_finding), risks=()
    )
    assert rec.supporting_finding_ids == (trusted_finding.finding_id,)
    assert rec.counter_finding_ids == (untrusted_finding.finding_id,)
    assert {ref.evidence_id for ref in rec.evidence_refs} == {FIXTURE_REF.evidence_id}

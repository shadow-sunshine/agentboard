"""官方资料快照（`fixtures/official/`）的端到端测试。

覆盖三件事：

1. **快照自身的诚实性**：每个注册维度要么有依据、要么显式 unknown；声明过的维度
   必须给出可核对的映射过程；本轮禁止的话题（速度、社区活跃度）确实没有结论输出。
2. **与现有领域格式的对接**：同一套 loader / worker / 报告组装零改动即可消费，
   `EvidenceRef` 全部是 `fixture_snapshot`，且**每条证据都能回溯到官方文档**。
3. **不与 P1A 基线互相污染**：目录分开、snapshot_version 不同、证据 id 不撞车、
   维度注册表逐字节一致（沿用既有定义与权重）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agentboard.contracts import ProjectBrief, TaskCard
from agentboard.evidence import SOURCE_FIXTURE, EvidenceRef
from agentboard.research.assemble import assemble_report
from agentboard.research.config import STATUS_SUPPORTED, VALID_SCALE_KINDS
from agentboard.research.errors import CandidateIdMissingError, SourceReferenceError
from agentboard.research.models import SelectionReport
from agentboard.research.sources import (
    ALLOWED_SOURCE_REPOS,
    KIND_OFFICIAL_DOCS,
    load_official_sources,
    make_task_input,
)

CANDIDATE_IDS = ("milvus_standalone", "pgvector", "qdrant_local")

#: 本轮口径：速度 / 规模 / 生态三个维度在官方文档里确认不了，三个候选一律 unknown。
#: 把它钉死，是为了让"补充资料"这件事成为一次**显式**决定，而不是悄悄改一个标签。
UNCONFIRMABLE_DIMENSIONS = ("ecosystem_maturity", "query_latency", "scale_ceiling")


# ---- 快照自身 ----


def test_candidate_set_and_display_names(official_sources) -> None:
    assert official_sources.candidate_ids() == CANDIDATE_IDS
    names = {
        candidate_id: official_sources.snapshot(candidate_id).display_name
        for candidate_id in CANDIDATE_IDS
    }
    assert names == {
        "milvus_standalone": "Milvus Standalone",
        "pgvector": "pgvector + PostgreSQL",
        "qdrant_local": "Qdrant self-hosted single-node",
    }


def test_every_registry_dimension_is_declared_or_unknown(official_sources) -> None:
    """零静默缺失：注册表的每个维度都必须有归属。"""
    registry_ids = set(official_sources.registry.ids())
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        covered = set(provenance.declared_dimension_ids) | set(provenance.unknown_dimension_ids)
        assert covered == registry_ids


def test_unconfirmable_dimensions_are_unknown_for_every_candidate(official_sources) -> None:
    """速度 / 规模 / 生态三个维度在官方文档里确认不了 —— 三个候选都必须登记为 unknown。

    这是"不写没有可复核数据支撑的结论"的可执行形态。
    """
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        for dimension_id in UNCONFIRMABLE_DIMENSIONS:
            assert provenance.is_unknown(dimension_id), (candidate_id, dimension_id)


def test_unknown_dimensions_have_reason_and_checked_sources(official_sources) -> None:
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        for dimension_id in provenance.unknown_dimension_ids:
            entry = provenance.unknown_dimensions[dimension_id]
            assert len(entry.reason) >= 20
            assert entry.checked_source_ids


def test_declared_values_hit_the_registry_scale(official_sources) -> None:
    registry = official_sources.registry
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        snapshot = official_sources.snapshot(candidate_id)
        for dimension_id in provenance.declared_dimension_ids:
            dimension = registry.get(dimension_id)
            assert dimension.scale_kind in VALID_SCALE_KINDS
            declared = snapshot.dimension_values[dimension_id]
            assert declared.value in dimension.scale_labels


def test_declared_dimensions_are_traceable(official_sources) -> None:
    """每个声明过的维度：有 basis、有推导过程、指向真实存在的 section 与来源。"""
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        for dimension_id in provenance.declared_dimension_ids:
            declaration = provenance.dimension_values[dimension_id]
            assert declaration.basis in {"stated", "mapped"}
            assert len(declaration.derivation) >= 12
            section = provenance.section(declaration.section_id)
            assert section.source_id == declaration.source_id
            document = provenance.document(declaration.source_id)
            assert document.kind in {KIND_OFFICIAL_DOCS, "official_repo_file"}
            assert section.line_span()[0] >= 1


def test_declared_dimension_has_a_verbatim_excerpt(official_sources) -> None:
    """每个声明都要落在一条真实摘录上：摘录非空、不超长、行号可解析。"""
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        snapshot = official_sources.snapshot(candidate_id)
        section_text = {section.section_id: section.text for section in snapshot.sections}
        for dimension_id in provenance.declared_dimension_ids:
            declaration = provenance.dimension_values[dimension_id]
            text = section_text[declaration.section_id]
            assert text.strip()
            assert len(text) <= 400
            start, end = provenance.section(declaration.section_id).line_span()
            assert start <= end


def test_collection_metadata_is_pinned(official_sources) -> None:
    """采集日期与快照 captured_at 一致；列或行都没有被截断的痕迹。"""
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        snapshot = official_sources.snapshot(candidate_id)
        assert provenance.collected_at == snapshot.captured_at
        for section in snapshot.sections:
            assert not section.text.endswith("\n")
            assert "\r" not in section.text


def test_release_tags_are_explicitly_unknown_with_reason(official_sources) -> None:
    """三个候选的文档本身都不声明版本 —— 登记为 unknown 并写明原因，而不是猜一个版本。"""
    documents = official_sources.documents()
    assert documents
    for document in documents:
        assert document.release_tag == "unknown"
        assert len(document.release_tag_note) >= 8
        assert document.upstream_latest_release
        assert document.upstream_latest_release_note
        assert document.upstream_license
        assert document.upstream_license_note


def test_snapshot_version_encodes_set_and_date(official_sources) -> None:
    for candidate_id in CANDIDATE_IDS:
        snapshot = official_sources.snapshot(candidate_id)
        assert snapshot.snapshot_version == f"{candidate_id}.official.{snapshot.captured_at}"


# ---- 与现有领域格式的对接 ----


def test_findings_cover_all_dimensions_for_every_candidate(official_sources) -> None:
    total = len(official_sources.registry.ids())
    for candidate_id in CANDIDATE_IDS:
        findings = official_sources.findings(candidate_id)
        assert len(findings) == total
        assert [finding.dimension_id for finding in findings] == sorted(
            official_sources.registry.ids()
        )


def test_unsupported_findings_match_unknown_dimensions(official_sources) -> None:
    """跨层交叉校验：来源层登记的 unknown 必须与领域层的 unsupported 完全一致。"""
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        unsupported = {
            finding.dimension_id
            for finding in official_sources.findings(candidate_id)
            if finding.status != STATUS_SUPPORTED
        }
        assert unsupported == set(provenance.unknown_dimension_ids)


def test_all_evidence_is_fixture_snapshot(official_sources) -> None:
    for candidate_id in CANDIDATE_IDS:
        for finding in official_sources.findings(candidate_id):
            for ref in finding.evidence_refs:
                assert ref.source_type == SOURCE_FIXTURE
                assert ref.source_id == f"fixture:{candidate_id}"


def test_every_evidence_resolves_to_an_official_document(official_sources) -> None:
    """证据解析：每条证据都能回溯到该候选白名单内的一份官方文档。"""
    seen: set[str] = set()
    for candidate_id in CANDIDATE_IDS:
        allowed = ALLOWED_SOURCE_REPOS[candidate_id]
        for finding in official_sources.findings(candidate_id):
            for ref in finding.evidence_refs:
                document = official_sources.resolve_evidence(ref)
                assert document.repo in allowed
                assert document.source_id in {
                    item.source_id for item in official_sources.provenance_for(candidate_id).documents
                }
                seen.add(document.source_id)
    assert seen == {document.source_id for document in official_sources.documents()}


def test_evidence_excerpt_matches_snapshot_section(official_sources) -> None:
    for candidate_id in CANDIDATE_IDS:
        snapshot = official_sources.snapshot(candidate_id)
        by_id = {section.section_id: section for section in snapshot.sections}
        provenance = official_sources.provenance_for(candidate_id)
        for finding in official_sources.findings(candidate_id):
            if not finding.evidence_refs:
                continue
            ref = finding.evidence_refs[0]
            declaration = provenance.dimension_values[finding.dimension_id]
            section = by_id[declaration.section_id]
            assert ref.excerpt == section.text[:400]
            assert len(ref.digest) == 64


def test_resolve_evidence_rejects_foreign_evidence(official_sources) -> None:
    ref = EvidenceRef(
        source_type=SOURCE_FIXTURE,
        source_id="fixture:not_a_candidate",
        snapshot_version="x.1",
        locator="/sections/0/text",
        excerpt="任意摘录",
        captured_at="2026-09-23",
    )
    with pytest.raises(SourceReferenceError, match="不属于本官方快照集"):
        official_sources.resolve_evidence(ref)


def test_run_and_investigate_agree(official_sources) -> None:
    from agentboard.research.payload import decode_findings

    worker = official_sources.worker()
    for candidate_id in CANDIDATE_IDS:
        task_input = make_task_input(candidate_id)
        result = asyncio.run(worker.run(task_input))
        findings = asyncio.run(worker.investigate(task_input))
        assert decode_findings(result.structured_payload) == findings


def test_repeat_investigation_is_identical(official_sources) -> None:
    for candidate_id in CANDIDATE_IDS:
        assert official_sources.findings(candidate_id) == official_sources.findings(candidate_id)


# ---- 报告组装（复用 P1A 的确定性组装器） ----


def test_official_report_passes_integrity(official_report: SelectionReport) -> None:
    assert official_report.integrity.passed
    assert official_report.integrity.failed_codes == ()


def test_official_report_ranking_is_pinned(official_report: SelectionReport) -> None:
    """回归锚点：排名与分数由快照内容决定，任何改动都必须显式更新这个断言。"""
    assert official_report.report_id == "rp_9e0057b9f989020f"
    ranking = [
        (item.rank, item.candidate_id, item.score) for item in official_report.recommendations
    ]
    assert ranking == [
        (1, "pgvector", 0.45),
        (2, "qdrant_local", 0.375),
        (3, "milvus_standalone", 0.2),
    ]


def test_official_report_risks_are_all_evidence_gaps(official_report: SelectionReport) -> None:
    """官方文档快照能给出的唯一一类风险就是"证据缺口"，没有任何其他规则被触发。"""
    assert {risk.rule_id for risk in official_report.risks} == {"R_UNSUPPORTED_WEIGHTED"}
    assert all(risk.severity == "high" for risk in official_report.risks)
    assert len(official_report.risks) == 10


def test_official_report_coverage_matches_declared(official_sources, official_report) -> None:
    coverage = {item.candidate_id: item for item in official_report.coverage.candidates}
    for candidate_id in CANDIDATE_IDS:
        provenance = official_sources.provenance_for(candidate_id)
        assert coverage[candidate_id].supported_dimensions == len(
            provenance.declared_dimension_ids
        )
        assert coverage[candidate_id].missing_dimensions == tuple(
            sorted(provenance.unknown_dimension_ids)
        )
        assert coverage[candidate_id].total_dimensions == len(official_sources.registry.ids())


def test_official_report_is_deterministic(
    official_brief: ProjectBrief,
    official_cards: Callable[..., list[TaskCard]],
    official_sources,
) -> None:
    first = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
    )
    second = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
    )
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_recommendations_are_backed_by_official_evidence(official_report: SelectionReport) -> None:
    """能进入推荐的候选，必须有可回溯到官方文档的证据撑着。"""
    for recommendation in official_report.recommendations:
        assert recommendation.supporting_finding_ids
        assert any(ref.source_type == SOURCE_FIXTURE for ref in recommendation.evidence_refs)


# ---- 与 P1A 基线互不污染 ----


def test_registry_is_byte_identical_to_p1a_baseline(official_root: Path, fixture_root: Path) -> None:
    """沿用既有维度定义与权重：两份注册表必须逐字节相同（改权重就会被这条拦下）。"""
    official = (official_root / "dimensions" / "dimensions.v1.json").read_bytes()
    baseline = (fixture_root / "dimensions" / "dimensions.v1.json").read_bytes()
    assert hashlib.sha256(official).hexdigest() == hashlib.sha256(baseline).hexdigest()


def test_p1a_snapshots_untouched_by_official_set(official_root: Path, fixture_root: Path) -> None:
    assert official_root != fixture_root
    assert official_root.is_dir() and fixture_root.is_dir()
    assert (fixture_root / "options" / "pgvector.json").is_file()


def test_official_evidence_ids_do_not_collide_with_p1a(
    official_sources, worker, make_input: Callable[..., object]
) -> None:
    """同一个 candidate_id 在两个快照集里产生不同 evidence_id（snapshot_version 不同）。"""
    official_ids = {
        ref.evidence_id
        for candidate_id in CANDIDATE_IDS
        for finding in official_sources.findings(candidate_id)
        for ref in finding.evidence_refs
    }
    p1a_ids = {
        ref.evidence_id
        for candidate_id in CANDIDATE_IDS
        for finding in asyncio.run(worker.investigate(make_input(candidate_id)))  # type: ignore[arg-type]
        for ref in finding.evidence_refs
    }
    assert official_ids and p1a_ids
    assert official_ids & p1a_ids == set()


def test_official_entry_rejects_heuristic_candidate(official_sources) -> None:
    """官方入口同样不做启发式：context 里没有显式行就报错，哪怕正文提到了候选名。"""
    from agentboard.contracts import WorkerTaskInput

    task_input = WorkerTaskInput(
        task_id="research__pgvector",
        title="调研候选方案",
        assigned_agent="research",
        goal="看看 pgvector 和 milvus 哪个更合适",
        context="请对比 pgvector 与 milvus_standalone 的部署复杂度。",
        upstream_results={},
    )
    with pytest.raises(CandidateIdMissingError):
        asyncio.run(official_sources.worker().investigate(task_input))


def test_official_fixtures_are_utf8_without_bom(official_root: Path) -> None:
    for path in sorted((official_root / "sources").glob("*.json")):
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        assert data.endswith(b"\n")
        assert b"\r\n" not in data


def test_official_load_is_independent_of_cwd(official_root: Path, tmp_path: Path, monkeypatch) -> None:
    """装载只用传入的绝对路径，不依赖当前工作目录。"""
    monkeypatch.chdir(tmp_path)
    sources = load_official_sources(official_root)
    assert sources.candidate_ids() == CANDIDATE_IDS

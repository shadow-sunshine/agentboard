"""`FixtureResearchWorker` 单测（P1A-2）。

覆盖：candidate_id 显式解析（含反启发式）、未知候选、缺失维度、
双出口一致、重复运行一致、证据可解析、上下文隔离。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentboard.contracts import WorkerResult
from agentboard.evidence import MAX_EXCERPT_CHARS, SOURCE_FIXTURE, is_trusted_evidence
from agentboard.research.dimensions import load_dimension_registry
from agentboard.research.errors import (
    CandidateIdMissingError,
    DimensionConflictError,
    InvalidDimensionValueError,
    ResearchValueError,
    UnknownCandidateError,
    UnknownDimensionError,
)
from agentboard.research.fixtures import FixtureResearchWorker, parse_candidate_id
from agentboard.research.payload import decode_findings
from agentboard.research.snapshot import load_snapshot, resolve_locator
from agentboard.workers.base import WorkerAdapter
from agentboard.workers.synthetic import SyntheticEvidenceFactory

EXPECTED_DIMENSION_COUNT = 6


def _run(worker: FixtureResearchWorker, task_input):
    return asyncio.run(worker.run(task_input))


def _investigate(worker: FixtureResearchWorker, task_input):
    return asyncio.run(worker.investigate(task_input))


# ---- candidate_id 解析：只认显式行 ----


def test_parse_candidate_id_happy_path() -> None:
    assert parse_candidate_id("背景\ncandidate_id: pgvector\n更多") == "pgvector"
    assert parse_candidate_id("candidate_id: milvus_standalone") == "milvus_standalone"
    assert parse_candidate_id("  candidate_id :  qdrant_local  \n") == "qdrant_local"


def test_parse_candidate_id_missing_raises() -> None:
    for context in ("", "纯背景，没有任何候选声明", "候选是 pgvector（但没有显式行）"):
        with pytest.raises(CandidateIdMissingError, match="candidate_id"):
            parse_candidate_id(context)


def test_parse_candidate_id_conflicting_raises() -> None:
    with pytest.raises(CandidateIdMissingError, match="冲突"):
        parse_candidate_id("candidate_id: pgvector\ncandidate_id: qdrant_local\n")


def test_parse_candidate_id_accepts_repeated_identical_values() -> None:
    assert parse_candidate_id("candidate_id: pgvector\ncandidate_id: pgvector\n") == "pgvector"


@pytest.mark.parametrize("bad", ["Pgvector", "1pgvector", "has space", "with-dash"])
def test_parse_candidate_id_invalid_format_raises(bad: str) -> None:
    with pytest.raises(CandidateIdMissingError):
        parse_candidate_id(f"candidate_id: {bad}\n")


def test_parse_candidate_id_ignores_similar_but_non_matching_lines() -> None:
    """只认 `candidate_id:` 独立行：其它相似写法一律不匹配（宁缺毋滥）。"""
    for context in (
        "candidate_ids: pgvector",
        "the candidate_id is pgvector",
        "note: candidate_id pgvector",
        "candidate_id=pgvector",
    ):
        with pytest.raises(CandidateIdMissingError):
            parse_candidate_id(context)


def test_worker_does_not_guess_from_task_id(worker: FixtureResearchWorker, make_input) -> None:
    """反启发式：task_id 里带 pgvector，但 context 没有显式声明 -> 直接报错。

    这正是「按名称猜目标」这类启发式最危险的场景：看起来能跑，实际把归属搞错。
    """
    task_input = make_input(
        "pgvector", task_id="research__pgvector", context="公共背景：没有候选声明\n"
    )
    with pytest.raises(CandidateIdMissingError):
        _investigate(worker, task_input)


def test_worker_rejects_non_string_context(worker: FixtureResearchWorker, make_input) -> None:
    task_input = make_input("pgvector")
    object.__setattr__(task_input, "context", 123)
    with pytest.raises(CandidateIdMissingError):
        _investigate(worker, task_input)


# ---- 未知候选 ----


def test_unknown_candidate_raises(worker: FixtureResearchWorker, make_input) -> None:
    task_input = make_input("redis_vector", task_id="research__redis_vector")
    with pytest.raises(UnknownCandidateError, match="没有候选"):
        _investigate(worker, task_input)
    with pytest.raises(UnknownCandidateError):
        _run(worker, task_input)


# ---- 维度覆盖：缺失即显式 unsupported ----


def test_all_dimensions_covered_has_no_unsupported(worker: FixtureResearchWorker, make_input) -> None:
    findings = _investigate(worker, make_input("pgvector"))
    assert len(findings) == EXPECTED_DIMENSION_COUNT
    assert all(finding.is_supported for finding in findings)
    assert not [f for f in findings if f.status == "unsupported"]


def test_missing_dimension_produces_unsupported(worker: FixtureResearchWorker, make_input) -> None:
    findings = _investigate(worker, make_input("milvus_standalone"))
    unsupported = [f for f in findings if not f.is_supported]
    assert [f.dimension_id for f in unsupported] == ["scale_ceiling"]
    (missing,) = unsupported
    assert missing.evidence_refs == ()
    assert missing.value == "unknown"
    assert missing.confidence == 0.0
    assert "未覆盖" in missing.claim
    assert missing.candidate_id == "milvus_standalone"


def test_every_registry_dimension_appears_exactly_once(
    worker: FixtureResearchWorker, make_input, registry
) -> None:
    for candidate_id in worker.candidate_ids:
        findings = _investigate(worker, make_input(candidate_id))
        assert [f.dimension_id for f in findings] == list(registry.ids())
        assert len({f.finding_id for f in findings}) == len(findings)


def test_findings_are_sorted_by_candidate_then_dimension(
    worker: FixtureResearchWorker, make_input
) -> None:
    for candidate_id in worker.candidate_ids:
        findings = _investigate(worker, make_input(candidate_id))
        keys = [(f.candidate_id, f.dimension_id) for f in findings]
        assert keys == sorted(keys)


# ---- 别名归并 / 量表支持 ----


def test_alias_declaration_is_normalised_to_canonical(
    worker: FixtureResearchWorker, make_input
) -> None:
    """milvus_standalone 快照里声明的是别名 ops_effort，必须归到 ops_skill_floor。"""
    findings = _investigate(worker, make_input("milvus_standalone"))
    by_id = {finding.dimension_id: finding for finding in findings}
    assert "ops_effort" not in by_id
    assert by_id["ops_skill_floor"].is_supported is True
    assert by_id["ops_skill_floor"].value == "high"


def test_boolean_dimension_is_supported(worker: FixtureResearchWorker, make_input) -> None:
    findings = _investigate(worker, make_input("pgvector"))
    by_id = {finding.dimension_id: finding for finding in findings}
    boolean_finding = by_id["managed_service_needed"]
    assert boolean_finding.is_supported is True
    assert boolean_finding.value in {"false", "true"}


def test_ordinal_dimension_is_supported(worker: FixtureResearchWorker, make_input, registry) -> None:
    findings = _investigate(worker, make_input("qdrant_local"))
    by_id = {finding.dimension_id: finding for finding in findings}
    for dimension_id in ("deployment_complexity", "query_latency"):
        assert registry.get(dimension_id).scale_kind == "ordinal"
        assert by_id[dimension_id].value in registry.get(dimension_id).scale_labels


# ---- 证据结构 ----


def test_all_evidence_is_fixture_snapshot(worker: FixtureResearchWorker, make_input) -> None:
    for candidate_id in worker.candidate_ids:
        findings = _investigate(worker, make_input(candidate_id))
        refs = [ref for finding in findings for ref in finding.evidence_refs]
        assert refs, f"{candidate_id} 应当有证据"
        for ref in refs:
            assert ref.source_type == SOURCE_FIXTURE
            assert ref.source_id == f"fixture:{candidate_id}"
            assert is_trusted_evidence(ref) is True


def test_evidence_locator_resolves_to_excerpt(
    worker: FixtureResearchWorker, make_input, catalog, fixture_root: Path
) -> None:
    """证据不能是悬空引用：locator 必须能定位到快照里的原文，且摘要逐字对得上。"""
    for candidate_id in worker.candidate_ids:
        entry = catalog.get(candidate_id)
        snapshot = load_snapshot(fixture_root / entry.fixture_path, entry=entry)
        findings = _investigate(worker, make_input(candidate_id))
        for finding in findings:
            for ref in finding.evidence_refs:
                text = resolve_locator(snapshot, ref.locator)
                assert ref.excerpt == text[:MAX_EXCERPT_CHARS]
                assert ref.snapshot_version == snapshot.snapshot_version
                assert ref.captured_at == snapshot.captured_at


def test_evidence_ids_are_unique_within_a_finding(
    worker: FixtureResearchWorker, make_input
) -> None:
    for candidate_id in worker.candidate_ids:
        for finding in _investigate(worker, make_input(candidate_id)):
            ids = [ref.evidence_id for ref in finding.evidence_refs]
            assert len(set(ids)) == len(ids)


# ---- 双出口一致 ----


def test_run_and_investigate_agree(worker: FixtureResearchWorker, make_input) -> None:
    for candidate_id in worker.candidate_ids:
        task_input = make_input(candidate_id)
        findings = _investigate(worker, task_input)
        result = _run(worker, task_input)
        assert decode_findings(result.structured_payload) == findings


def test_run_result_contract(worker: FixtureResearchWorker, make_input) -> None:
    result = _run(worker, make_input("pgvector", task_id="research__pgvector"))
    assert isinstance(result, WorkerResult)
    assert result.task_id == "research__pgvector"
    assert result.agent_name == "research"
    assert 0.0 <= result.confidence <= 1.0
    assert result.session_id
    assert result.structured_payload["schema"] == "research.findings.v1"
    assert result.evidence
    assert result.risks == ()


def test_run_agent_name_follows_task_card(worker: FixtureResearchWorker, make_input) -> None:
    """agent_name 取自任务卡，避免出现"worker 名字与卡片不一致"导致 apply_success 拒绝。"""
    result = _run(worker, make_input("pgvector", assigned_agent="planner"))
    assert result.agent_name == "planner"


def test_run_confidence_matches_supported_ratio(worker: FixtureResearchWorker, make_input) -> None:
    result = _run(worker, make_input("milvus_standalone"))
    assert result.confidence == pytest.approx(5 / 6)
    assert len(result.open_questions) == 1


def test_run_facts_only_from_supported(worker: FixtureResearchWorker, make_input) -> None:
    findings = _investigate(worker, make_input("milvus_standalone"))
    result = _run(worker, make_input("milvus_standalone"))
    assert result.facts == tuple(f.claim for f in findings if f.is_supported)
    assert all("未覆盖" not in fact for fact in result.facts)


# ---- 确定性 ----


def test_same_input_yields_identical_output(worker: FixtureResearchWorker, make_input) -> None:
    task_input = make_input("qdrant_local")
    first_findings = _investigate(worker, task_input)
    second_findings = _investigate(worker, task_input)
    assert first_findings == second_findings

    first_result = _run(worker, task_input)
    second_result = _run(worker, task_input)
    assert first_result.to_dict() == second_result.to_dict()


def test_determinism_across_worker_instances(fixture_root: Path, make_input) -> None:
    first = FixtureResearchWorker.from_fixture_root(fixture_root)
    second = FixtureResearchWorker.from_fixture_root(fixture_root)
    task_input = make_input("pgvector")
    assert _investigate(first, task_input) == _investigate(second, task_input)
    assert _run(first, task_input).to_dict() == _run(second, task_input).to_dict()


def test_worker_satisfies_worker_adapter_protocol(worker: FixtureResearchWorker) -> None:
    assert isinstance(worker, WorkerAdapter) is True


# ---- 上下文隔离 ----


def _noisy_upstream() -> WorkerResult:
    return WorkerResult(
        task_id="noisy_upstream",
        agent_name="research",
        facts=("SECRET_UPSTREAM_FACT",),
        evidence=SyntheticEvidenceFactory.for_task("research", "noisy_upstream", count=1),
        risks=("SECRET_RISK",),
        open_questions=("SECRET_QUESTION",),
        confidence=1.0,
        summary="SECRET_SUMMARY",
    )


def test_upstream_results_do_not_change_findings(
    worker: FixtureResearchWorker, make_input
) -> None:
    plain = _investigate(worker, make_input("pgvector"))
    with_upstream = _investigate(worker, make_input("pgvector", upstream={"noisy_upstream": _noisy_upstream()}))
    assert plain == with_upstream


def test_upstream_content_never_leaks_into_output(
    worker: FixtureResearchWorker, make_input
) -> None:
    task_input = make_input("pgvector", upstream={"noisy_upstream": _noisy_upstream()})
    findings = _investigate(worker, task_input)
    result = _run(worker, task_input)
    blob = json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False)
    blob += json.dumps(result.to_dict(), ensure_ascii=False)
    assert "SECRET" not in blob


def test_upstream_evidence_is_not_reused(worker: FixtureResearchWorker, make_input) -> None:
    upstream = _noisy_upstream()
    upstream_ids = {ref.evidence_id for ref in upstream.evidence}
    findings = _investigate(worker, make_input("pgvector", upstream={"noisy_upstream": upstream}))
    produced = {ref.evidence_id for finding in findings for ref in finding.evidence_refs}
    assert produced & upstream_ids == set()


# ---- 坏配置：取值不合法 / 维度冲突 ----


def _worker_from(root: Path) -> FixtureResearchWorker:
    return FixtureResearchWorker.from_fixture_root(root)


def test_invalid_dimension_value_rejected(
    cloned_fixture_root: Path, make_input, resign_fixture
) -> None:
    target = cloned_fixture_root / "options" / "pgvector.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["dimension_values"]["query_latency"]["value"] = "amazing"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    resign_fixture(cloned_fixture_root)

    worker = _worker_from(cloned_fixture_root)
    with pytest.raises(InvalidDimensionValueError, match="amazing"):
        _investigate(worker, make_input("pgvector"))


def test_conflicting_dimension_declaration_rejected(
    cloned_fixture_root: Path, make_input, resign_fixture
) -> None:
    """同时用别名和 canonical 声明同一维度、取值还不一致 -> 拒绝，不取其一。"""
    target = cloned_fixture_root / "options" / "pgvector.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["dimension_values"]["deploy_complexity"] = {
        "value": "high",
        "section_id": "s_deploy",
        "claim": "与 canonical 声明冲突的取值",
    }
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    resign_fixture(cloned_fixture_root)

    worker = _worker_from(cloned_fixture_root)
    with pytest.raises(DimensionConflictError, match="冲突"):
        _investigate(worker, make_input("pgvector"))


def test_unknown_dimension_key_in_snapshot_rejected(
    cloned_fixture_root: Path, make_input, resign_fixture
) -> None:
    target = cloned_fixture_root / "options" / "pgvector.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["dimension_values"]["cost_per_gb"] = {
        "value": "low",
        "section_id": "s_deploy",
        "claim": "注册表里没有这个维度",
    }
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    resign_fixture(cloned_fixture_root)

    worker = _worker_from(cloned_fixture_root)
    with pytest.raises(UnknownDimensionError, match="未知维度"):
        _investigate(worker, make_input("pgvector"))


# ---- 装配校验 ----


def test_worker_rejects_incomplete_snapshot_set(
    fixture_root: Path, catalog, registry
) -> None:
    snapshots = {
        "pgvector": load_snapshot(
            fixture_root / "options" / "pgvector.json", entry=catalog.get("pgvector")
        )
    }
    with pytest.raises(ResearchValueError, match="缺少候选快照"):
        FixtureResearchWorker(catalog, registry, snapshots)


def test_worker_rejects_wrong_argument_types(fixture_root: Path, catalog, registry) -> None:
    with pytest.raises(ResearchValueError, match="FixtureCatalog"):
        FixtureResearchWorker("not-a-catalog", registry, {})  # type: ignore[arg-type]
    with pytest.raises(ResearchValueError, match="DimensionRegistry"):
        FixtureResearchWorker(catalog, "not-a-registry", {})  # type: ignore[arg-type]


def test_worker_from_fixture_root_exposes_inventory(fixture_root: Path) -> None:
    worker = FixtureResearchWorker.from_fixture_root(fixture_root)
    assert worker.candidate_ids == ("milvus_standalone", "pgvector", "qdrant_local")
    assert worker.dimension_ids == (
        "deployment_complexity",
        "ecosystem_maturity",
        "managed_service_needed",
        "ops_skill_floor",
        "query_latency",
        "scale_ceiling",
    )


def test_worker_uses_registry_loaded_from_catalog_path(fixture_root: Path, catalog) -> None:
    """worker 装载的注册表必须来自 INDEX.json 声明的路径，而不是硬编码默认值。"""
    registry = load_dimension_registry(fixture_root / catalog.dimension_registry_path)
    assert registry.ids() == FixtureResearchWorker.from_fixture_root(fixture_root).dimension_ids

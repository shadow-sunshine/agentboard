"""运行时主流程测试（P3）：成功路径、边界性质、确定性。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agentboard.providers.snapshot_store import SnapshotStore
from agentboard.runtime.decider import DimensionDecision
from agentboard.runtime.errors import EvidenceBoundaryError
from agentboard.runtime.runner import _decider_identity
from tests.runtime.conftest import (
    CANDIDATES,
    DIMENSIONS_JSON,
    decision_for,
    make_docs,
    make_runner,
)

PRIMARY_DIM = "deployment_complexity"
SECONDARY_DIM = "ops_skill_floor"


def _full_documents() -> dict[str, list[Any]]:
    documents: dict[str, list[Any]] = {}
    for candidate_id in CANDIDATES:
        for dimension_id in (PRIMARY_DIM, SECONDARY_DIM):
            terms = f"{candidate_id} {dimension_id}"
            documents[terms] = make_docs(terms, candidate_id, count=2)
    return documents


def _full_decisions() -> dict[tuple[str, str], DimensionDecision]:
    decisions: dict[tuple[str, str], DimensionDecision] = {}
    for candidate_id in CANDIDATES:
        dim_value = (PRIMARY_DIM, "low"), (SECONDARY_DIM, "low")
        for dimension_id, value in dim_value:
            key, decision = decision_for(candidate_id, dimension_id, value)
            decisions[key] = decision
    return decisions


def test_success_pipeline_end_to_end(tmp_path: Any, brief: Any, registry: Any) -> None:
    """任务输入 → 计划 → 检索 → 去重快照 → 证据映射 → 确定性报告（全绿）。"""
    runner, provider, _run_dir = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(),
        run_name="success",
    )
    result = runner.run()

    assert result.outcome == "completed"
    assert result.report is not None
    assert result.report.integrity.passed is True
    assert result.degraded_candidates == ()
    undecided = tuple(sorted(set(registry.ids()) - {PRIMARY_DIM, SECONDARY_DIM}))
    assert result.unsupported_dimensions == {
        candidate_id: undecided for candidate_id in CANDIDATES
    }
    # 语料落盘 + 运行记录可观测
    assert result.corpus_dir is not None and result.corpus_dir.is_dir()
    assert result.run_record_path is not None and result.run_record_path.is_file()
    # run_id 由输入材料派生（内容寻址形态）
    assert result.run_id.startswith("lr_") and len(result.run_id) == 19
    # 预算记账与实际调用一致
    assert result.budget_usage["provider_requests"] == sum(provider.calls.values())
    assert all(step.status == "succeeded" for step in result.steps)


def test_report_id_is_stable_across_replays(tmp_path: Any, brief: Any, registry: Any) -> None:
    """同一输入两次独立运行 ⇒ 同一 run_id、同一 report_id、同一报告 JSON。"""
    first, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(), run_name="replay-a",
    )
    second, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(), run_name="replay-b",
    )
    result_a = first.run()
    result_b = second.run()
    assert result_a.run_id == result_b.run_id
    assert result_a.report is not None and result_b.report is not None
    assert result_a.report.report_id == result_b.report.report_id
    assert result_a.report_json == result_b.report_json


def test_run_id_changes_when_only_the_decision_changes(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """反例（Codex P1 修正）：材料完全相同、只改决策 ⇒ run_id 必须变化。

    逐一覆盖决策三元组 value / claim / section_id —— run_id 覆盖
    **全部影响报告内容的输入**，"同材料不同结论"不可能共用同一运行身份。
    """
    documents = _full_documents()
    decisions = _full_decisions()
    baseline, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=documents, decisions=decisions, run_name="rid-base",
    )
    result_base = baseline.run()

    mutations = {
        "value": DimensionDecision(
            value="medium", claim="脚本决策：依据快照切片。", section_id="s_pgvector_00"
        ),
        "claim": DimensionDecision(
            value="low", claim="脚本决策：改写后的依据陈述。", section_id="s_pgvector_00"
        ),
        "section": DimensionDecision(
            value="low", claim="脚本决策：依据快照切片。", section_id="s_pgvector_01"
        ),
    }
    for name, replacement in mutations.items():
        mutated = dict(decisions)
        mutated[("pgvector", PRIMARY_DIM)] = replacement
        runner, _, _ = make_runner(
            tmp_path=tmp_path, brief=brief, registry=registry,
            documents=documents, decisions=mutated,
            run_name=f"rid-mut-{name}",
        )
        result_mut = runner.run()
        assert result_mut.run_id != result_base.run_id, (
            f"只改变决策的 {name} 分量，run_id 不应保持不变"
        )
        assert result_mut.report is not None and result_base.report is not None
        assert result_mut.report.report_id != result_base.report.report_id


def test_run_id_rejects_brief_candidate_boundary_collision(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """brief 与 candidate 的字段边界变化不能复用同一 run_id。"""
    runner_a, _, _ = make_runner(
        tmp_path=tmp_path,
        brief=replace(brief, text="a"),
        registry=registry,
        documents={},
        candidate_ids=("bca", "qdrant_local"),
        run_name="rid-boundary-a",
    )
    runner_b, _, _ = make_runner(
        tmp_path=tmp_path,
        brief=replace(brief, text="ab"),
        registry=registry,
        documents={},
        candidate_ids=("ca", "qdrant_local"),
        run_name="rid-boundary-b",
    )

    assert (
        runner_a._run_id({}, {}, decider_identity=_decider_identity(runner_a._decider))
        != runner_b._run_id({}, {}, decider_identity=_decider_identity(runner_b._decider))
    )


def test_run_id_changes_when_dimension_definitions_change(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """维度定义 / 权重变化（注册表 JSON 字节变化）⇒ run_id 必须变化。"""
    import json as _json

    modified_path = tmp_path / "dimensions-modified.json"
    payload = _json.loads(DIMENSIONS_JSON.read_text(encoding="utf-8"))
    # 改权重必须显式配平（注册表校验：权重和 = 1.0）
    payload["dimensions"][0]["weight"] = 0.25
    payload["dimensions"][1]["weight"] = 0.10
    modified_path.write_text(
        _json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    baseline, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(),
        run_name="rid-dim-base",
    )
    result_base = baseline.run()
    modified, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(),
        dimensions_json=modified_path, run_name="rid-dim-mod",
    )
    result_mod = modified.run()
    assert result_mod.run_id != result_base.run_id


def test_budget_timeout_reaches_the_provider(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """预算里的 `request_timeout_seconds` 必须真正传到 Provider（不是无效配置）。"""
    from tests.runtime.conftest import make_budget

    runner, provider, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(),
        budget=make_budget(request_timeout_seconds=3.25),
        run_name="timeout-wired",
    )
    result = runner.run()
    assert result.outcome == "completed"
    assert provider.timeouts, "Provider 应记录每次调用收到的 timeout"
    assert set(provider.timeouts) == {3.25}


def test_snapshot_stats_count_actual_store_writes(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """快照指标 = SnapshotStore 的实际写入/重复结果（禁止用成功步骤数冒充）。

    2 候选 × 2 维度 × 每查询 2 份文档 ⇒ 8 份唯一内容 ⇒ 写 8 个快照文件，
    而成功步骤只有 4 个 —— 两个数字必须分开、且以快照目录为准。
    """
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(),
        run_name="stats-multi",
    )
    result = runner.run()
    assert result.outcome == "completed"

    succeeded_steps = sum(1 for step in result.steps if step.status == "succeeded")
    # 12 个计划步（零结果也算 succeeded）+ 8 个缺口补检步 = 20 个"成功步骤"，
    # 而实际写入的快照只有 8 份 —— 两个数字必须分开、以快照目录为准。
    assert succeeded_steps == 20
    assert result.snapshots_written == 8
    assert result.snapshots_written != succeeded_steps
    assert result.snapshots_duplicate == 0

    store = SnapshotStore(tmp_path / "stats-multi-snapshots")
    assert len(store.list_snapshots()) == result.snapshots_written


def test_evidence_boundary_provider_output_never_becomes_evidence_directly(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """边界性质：报告里每条证据都能回溯到"已落盘快照 → 语料 section"这条链。

    Provider 产物没有经过这条链就不可能出现在报告里 —— 这是
    "Provider 输出不得直接构造 EvidenceRef"的可执行形态。
    """
    runner, _, _run_dir = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=_full_decisions(), run_name="boundary",
    )
    result = runner.run()
    assert result.report is not None and result.corpus_dir is not None

    # 语料里的 section 文本 == Provider 文档内容（经过快照落盘这一站）
    corpus_texts: set[str] = set()
    for fixture_path in sorted((result.corpus_dir / "options").glob("*.json")):
        payload = __import__("json").loads(fixture_path.read_text(encoding="utf-8"))
        for section in payload["sections"]:
            corpus_texts.add(section["text"])
    expected_texts = {
        document.content for docs in _full_documents().values() for document in docs
    }
    assert corpus_texts == expected_texts

    # 报告证据摘录必须等于语料 section 文本的规范截断（同一良定义属性）
    from agentboard.research.snapshot import expected_excerpt_for

    for finding in result.report.findings:
        for ref in finding.evidence_refs:
            assert ref.excerpt in corpus_texts or any(
                expected_excerpt_for(text) == ref.excerpt for text in corpus_texts
            )


def test_unsupported_dimensions_are_explicit_not_invented(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """决策器不覆盖的维度 ⇒ 显式 unsupported（绝不推断补齐）。"""
    decisions = dict(
        [decision_for("pgvector", PRIMARY_DIM, "low"), decision_for("qdrant_local", PRIMARY_DIM, "low")]
    )
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=decisions, run_name="unsupported",
    )
    result = runner.run()
    assert result.outcome == "completed"
    assert result.report is not None and result.report.integrity.passed is True
    for candidate_id in CANDIDATES:
        assert SECONDARY_DIM in result.unsupported_dimensions[candidate_id]


def test_invalid_decision_value_is_explicit_boundary_failure(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """决策值不在量表内 ⇒ `EvidenceBoundaryError`（显式失败，不静默修正）。"""
    decisions = dict(
        [decision_for("pgvector", PRIMARY_DIM, "超纲值"), decision_for("qdrant_local", PRIMARY_DIM, "low")]
    )
    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_full_documents(), decisions=decisions, run_name="bad-value",
    )
    with pytest.raises(EvidenceBoundaryError):
        runner.run()


def test_duplicate_documents_are_deduplicated_in_store(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """同一内容被多个查询取回 ⇒ 快照目录里只有一份（去重 + 不可覆盖）。"""
    documents: dict[str, list[Any]] = {}
    for candidate_id in CANDIDATES:
        for dimension_id in (PRIMARY_DIM, SECONDARY_DIM):
            terms = f"{candidate_id} {dimension_id}"
            documents[terms] = make_docs(terms, candidate_id, count=1)
    # 真重复：同一个文档对象被两个不同查询返回（内容逐字节相同）
    shared = documents[f"pgvector {PRIMARY_DIM}"][0]
    documents[f"pgvector {SECONDARY_DIM}"].append(shared)

    runner, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=documents,
        decisions=dict([
            decision_for("pgvector", PRIMARY_DIM, "low"),
            decision_for("qdrant_local", PRIMARY_DIM, "low"),
        ]),
        run_name="dedup",
    )
    result = runner.run()
    assert result.outcome == "completed"
    # 快照指标按实际 Store 写入/重复结果累计：4 份唯一内容 + 1 次重复
    assert result.snapshots_written == 4
    assert result.snapshots_duplicate == 1
    store = SnapshotStore(tmp_path / "dedup-snapshots")
    assert len(store.list_snapshots()) == 4
    # 语料里的 section 按 (content_sha256, url) 去重 ⇒ 每个候选 2 份而不是 3 份
    from agentboard.research.snapshot import load_catalog, load_snapshots

    catalog = load_catalog(result.corpus_dir)
    snapshots = load_snapshots(result.corpus_dir, catalog)
    assert len(snapshots["pgvector"].sections) == 2
    assert len(snapshots["qdrant_local"].sections) == 2

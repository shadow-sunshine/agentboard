"""P2.2-3：快照集隔离测试（"一次报告只来自一个快照集"）。

核心不变量：

1. **默认路径逐字节不变** —— 不传 `snapshot_set` 时行为与 P2.2-2 完全一致；
2. **同名候选跨目录混集必须被拒绝** —— 两个真实目录（official / research）存在同名候选
   `pgvector`，dict 合并会静默覆盖；`merge_snapshots` 在合并点拒绝，
   `assemble_report(snapshot_set=...)` 在归属不可证明时拒绝（fail-closed）；
3. **归属只认戳印** —— 快照的 `snapshot_set` 由 loader 从 `INDEX.json` 戳印，
   `candidate_id` / `source_id` 相同**不构成**同源证据。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentboard.contracts import ProjectBrief, TaskCard
from agentboard.research.assemble import assemble_report
from agentboard.research.config import (
    POLICY_REGRESSION_REPLAY,
    POLICY_RESEARCH_GRADE,
)
from agentboard.research.dimensions import load_dimension_registry
from agentboard.research.errors import CandidateCoverageError
from agentboard.research.snapshot import (
    SnapshotSet,
    load_catalog,
    load_snapshots,
    merge_snapshots,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = PROJECT_ROOT / "fixtures" / "research"


def _declare(snapshot_set_id: str, policy: str) -> SnapshotSet:
    """为测试构建 SnapshotSet（升格判定由 I10 按实际快照 + 证明重算，无需预计算映射）。"""
    return SnapshotSet(snapshot_set_id=snapshot_set_id, policy=policy)


def _cards_from(sources: Any) -> list[TaskCard]:
    """用给定 OfficialSources 的 worker 产出已完成 research 卡（与真实管线一致）。"""
    worker = sources.worker()
    cards: list[TaskCard] = []
    for candidate_id in sources.candidate_ids():
        from agentboard.research.sources import make_task_input

        task_input = make_task_input(candidate_id)
        cards.append(
            TaskCard(
                task_id=task_input.task_id,
                title=task_input.title,
                assigned_agent=task_input.assigned_agent,
                goal=task_input.goal,
                context=task_input.context,
                status="done",
                result=asyncio.run(worker.run(task_input)),
                attempts=1,
            )
        )
    return cards


# ---- ① 单集正常组装：与默认路径逐字节相同 ----


def test_single_declared_set_is_byte_identical_to_default(
    official_sources: Any, official_cards: Any, official_brief: ProjectBrief
) -> None:
    """`regression_replay` 不追加任何条件检查 ⇒ 报告与默认组装完全相等（含 integrity）。"""
    default = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
    )
    declared = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
        snapshot_set=SnapshotSet(
            snapshot_set_id=official_sources.snapshot_set_id,
            policy=POLICY_REGRESSION_REPLAY,
        ),
    )
    assert declared.to_dict() == default.to_dict()
    assert declared.report_id == default.report_id


# ---- ② 跨目录混集：必须拒绝并指出冲突候选 ----


def test_merge_of_two_real_sets_rejects_same_name_conflict(
    official_sources: Any,
) -> None:
    """真实场景：official 与 research 目录存在同名候选 —— 合并即拒绝，逐个候选可复现。"""
    research = load_snapshots(RESEARCH_ROOT, load_catalog(RESEARCH_ROOT))
    shared = sorted(set(research) & set(official_sources.snapshots))
    assert shared == ["milvus_standalone", "pgvector", "qdrant_local"], (
        "测试前提：两个真实目录存在同名候选（且仅有这些同名）"
    )
    for name in shared:
        with pytest.raises(CandidateCoverageError, match=f"候选 '{name}'"):
            merge_snapshots(
                {name: official_sources.snapshots[name]},
                {name: research[name]},
            )


def test_merge_identical_snapshots_is_idempotent(official_sources: Any) -> None:
    """同名但**逐字段相等**（可证明同源）⇒ 合并放行，结果与单集相同。"""
    merged = merge_snapshots(official_sources.snapshots, official_sources.snapshots)
    assert dict(merged) == dict(official_sources.snapshots)


def test_unprovable_membership_is_rejected_at_assemble(
    official_sources: Any, official_cards: Any, official_brief: ProjectBrief
) -> None:
    """归属戳印 `""`（目录未声明）⇒ 无法证明归属 ⇒ 组装期拒绝，fail-closed。"""
    tampered = {
        candidate_id: (
            replace(snapshot, snapshot_set="")  # 模拟"目录未声明快照集"的来源
            if candidate_id == "pgvector"
            else snapshot
        )
        for candidate_id, snapshot in official_sources.snapshots.items()
    }
    declared = _declare(
        official_sources.snapshot_set_id,
        POLICY_REGRESSION_REPLAY,
    )
    with pytest.raises(CandidateCoverageError, match="pgvector"):
        assemble_report(
            official_brief,
            official_cards(),
            official_sources.registry,
            snapshots=tampered,
            snapshot_set=declared,
        )


def test_cross_set_stamp_is_rejected_at_assemble(
    official_sources: Any, official_cards: Any, official_brief: ProjectBrief
) -> None:
    """戳印是**另一个**合法快照集的 id ⇒ 可证明不属于本集 ⇒ 拒绝。"""
    other_set_id = "official.2026-01-01"
    tampered = {
        candidate_id: (
            replace(snapshot, snapshot_set=other_set_id)
            if candidate_id == "qdrant_local"
            else snapshot
        )
        for candidate_id, snapshot in official_sources.snapshots.items()
    }
    declared = _declare(
        official_sources.snapshot_set_id,
        POLICY_REGRESSION_REPLAY,
    )
    with pytest.raises(CandidateCoverageError, match="qdrant_local"):
        assemble_report(
            official_brief,
            official_cards(),
            official_sources.registry,
            snapshots=tampered,
            snapshot_set=declared,
        )


def test_provable_internal_mixing_is_rejected_even_without_declaration(
    make_synthetic_official_set: Any, official_brief: ProjectBrief
) -> None:
    """映射内部带 ≥2 个不同的非空归属戳印 ⇒ 数据自证的混集 ⇒ 无论是否声明都拒绝。

    用两套合成官方集（tmp 落盘）构造：A 保持 `official.2026-09-23`，
    B 的 `INDEX.json` 改成 `official.2026-09-24` 后，把 B 的一份快照
    换名混进 A 的映射 —— 两个非空戳印共存，数据自证混集。
    全程 tmp，不触碰 `fixtures/**`。
    """
    set_a = make_synthetic_official_set("set-a")
    set_b = make_synthetic_official_set("set-b")
    index = json.loads(set_b.index_path.read_text(encoding="utf-8"))
    index["snapshot_set"] = "official.2026-09-24"
    set_b.index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    snaps_a = load_snapshots(set_a.root, load_catalog(set_a.root))
    snaps_b = load_snapshots(set_b.root, load_catalog(set_b.root))

    mixed = dict(snaps_a)
    intruder = replace(snaps_b["pgvector"], candidate_id="pgvector_alt")
    mixed["pgvector_alt"] = intruder
    assert {snapshot.snapshot_set for snapshot in mixed.values() if snapshot.snapshot_set} == {
        "official.2026-09-23",
        "official.2026-09-24",
    }, "测试前提：映射内已有两个可证明的不同快照集戳印"

    catalog_a = load_catalog(set_a.root)
    registry = load_dimension_registry(set_a.root / catalog_a.dimension_registry_path)
    with pytest.raises(CandidateCoverageError, match="混合了多个快照集"):
        assemble_report(
            official_brief,
            _cards_from(_load(set_a.root)),
            registry,
            snapshots=mixed,
        )


def _load(root: Path) -> Any:
    from agentboard.research.sources import load_official_sources

    return load_official_sources(root)

# ---- ③ 声明字段不进 content_projection：report_id 不变 ----


def test_declared_fields_do_not_change_report_id(
    official_sources: Any, official_cards: Any, official_brief: ProjectBrief
) -> None:
    """`snapshot_set_id` / `policy` / `promotable` 都不进 `content_projection`
    ⇒ 是否声明快照集不影响 `report_id`（regression_replay 下 integrity 也相同）。"""
    default = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
    )
    declared = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
        snapshot_set=SnapshotSet(
            snapshot_set_id=official_sources.snapshot_set_id,
            policy=POLICY_REGRESSION_REPLAY,
        ),
    )
    assert declared.report_id == default.report_id
    assert declared.integrity.to_dict() == default.integrity.to_dict()


# 常量只读引用（防止未来的策略常量改名悄悄破坏本文件语义）
assert POLICY_RESEARCH_GRADE == "research_grade"

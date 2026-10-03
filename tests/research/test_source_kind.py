"""`source_kind` 的加载期生效（P2.2-1，修 D1）。

P2.2-1 之前 `source_kind` 只是**写在 fixture 里但没人读**的字段 ——
"官方原文"与"人工摘要"在代码层完全同权。本文件把"加载期可区分"钉死：

| 情形 | 期望 |
|---|---|
| `fixtures/research/*` | 全部 `authored_summary`，且**无** `provenance` |
| `fixtures/official/*` | 全部 `official_documentation_excerpt`，且**有** `provenance` |
| 未知 / 缺失 `source_kind` | `UnknownSourceKindError`（配置错误，不是"不可升格"） |
| 官方摘录缺 `provenance` | `MissingProvenanceError` |
| 人工摘要带 `provenance` | `SourceProvenanceSchemaError`（不得冒充官方来源） |

这些用例全部读**真实 fixture 的副本**（`cloned_*` fixture），改坏一处再断言报错 ——
对合成数据断言"策略生效"只是纸面约定。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentboard.research.config import (
    SOURCE_KIND_AUTHORED_SUMMARY,
    SOURCE_KIND_OFFICIAL_EXCERPT,
    VALID_SOURCE_KINDS,
)
from agentboard.research.errors import (
    MissingProvenanceError,
    SnapshotError,
    SourceProvenanceSchemaError,
    UnknownSourceKindError,
)
from agentboard.research.snapshot import (
    FixtureCatalog,
    FixtureSnapshot,
    load_catalog,
    load_snapshot,
    load_snapshots,
)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


# ---- 真实数据的两侧契约 ----


def test_research_fixtures_are_authored_summaries(
    fixture_root: Path, catalog: FixtureCatalog, snapshots: Mapping[str, FixtureSnapshot]
) -> None:
    assert catalog.ids(), "fixtures/research 不应为空"
    for candidate_id, snapshot in snapshots.items():
        assert snapshot.source_kind == SOURCE_KIND_AUTHORED_SUMMARY, candidate_id
        # 人工摘要**不得**带 provenance：一旦带上就直接抛错，见 test_authored_summary_*
        payload = _read(fixture_root / "options" / f"{candidate_id}.json")
        assert "provenance" not in payload


def test_official_fixtures_are_official_excerpts(
    official_root: Path, official_sources: Any
) -> None:
    assert official_sources.candidate_ids(), "fixtures/official 不应为空"
    for candidate_id in official_sources.candidate_ids():
        snapshot = official_sources.snapshot(candidate_id)
        assert snapshot.source_kind == SOURCE_KIND_OFFICIAL_EXCERPT, candidate_id
        payload = _read(official_root / "sources" / f"{candidate_id}.json")
        assert isinstance(payload.get("provenance"), dict)


def test_two_catalogs_are_distinguishable_by_source_kind(
    fixture_root: Path, official_root: Path
) -> None:
    """两个目录若都加载成功，其 `source_kind` 集合必须互不重叠。

    这条断言的价值：它是"官方原文与人工摘要可区分"这件事最小的可执行形式 ——
    在 P2.2-1 之前，两个目录加载出来的 `FixtureSnapshot` 除了 candidate_id 之外
    没有任何字段能区分来源性质。
    """
    research_kinds = {
        snapshot.source_kind
        for snapshot in load_snapshots(fixture_root, load_catalog(fixture_root)).values()
    }
    official_kinds = {
        snapshot.source_kind
        for snapshot in load_snapshots(official_root, load_catalog(official_root)).values()
    }
    assert research_kinds == {SOURCE_KIND_AUTHORED_SUMMARY}
    assert official_kinds == {SOURCE_KIND_OFFICIAL_EXCERPT}
    assert not (research_kinds & official_kinds)


def test_snapshot_to_dict_carries_source_kind(
    fixture_root: Path, catalog: FixtureCatalog
) -> None:
    snapshot = load_snapshot(fixture_root / "options" / "pgvector.json", entry=catalog.get("pgvector"))
    assert snapshot.to_dict()["source_kind"] == SOURCE_KIND_AUTHORED_SUMMARY


# ---- 闭集校验 ----


def test_unknown_source_kind_rejected(cloned_fixture_root: Path) -> None:
    path = cloned_fixture_root / "options" / "pgvector.json"
    payload = _read(path)
    payload["source_kind"] = "official_documentation"  # 近似但不合法，正是要拦的错
    _write(path, payload)
    catalog = load_catalog(cloned_fixture_root)
    with pytest.raises(UnknownSourceKindError, match="source_kind"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_missing_source_kind_rejected(cloned_fixture_root: Path) -> None:
    """不做默认值兜底：忘标来源种类必须**失败**，而不是静默变成某一类。"""
    path = cloned_fixture_root / "options" / "pgvector.json"
    payload = _read(path)
    payload.pop("source_kind")
    _write(path, payload)
    catalog = load_catalog(cloned_fixture_root)
    with pytest.raises(UnknownSourceKindError, match="source_kind"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_valid_source_kinds_are_exactly_two() -> None:
    assert VALID_SOURCE_KINDS == frozenset(
        {SOURCE_KIND_AUTHORED_SUMMARY, SOURCE_KIND_OFFICIAL_EXCERPT}
    )


# ---- §2.8 `source_kind` ↔ `provenance` 交叉强制（两者互为反例） ----


def test_official_excerpt_without_provenance_rejected(cloned_official_root: Path) -> None:
    path = cloned_official_root / "sources" / "pgvector.json"
    payload = _read(path)
    payload.pop("provenance")
    _write(path, payload)
    catalog = load_catalog(cloned_official_root)
    with pytest.raises(MissingProvenanceError, match="provenance"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_authored_summary_with_provenance_rejected(cloned_fixture_root: Path) -> None:
    """人工摘要不得冒充官方来源 —— 这是 §2.8 的另一半反例。

    若不做这条校验，"人工摘要"只要顺手加一个 provenance 块就会被下游当成官方原文。
    """
    path = cloned_fixture_root / "options" / "pgvector.json"
    payload = _read(path)
    payload["provenance"] = {"schema": "research.sources.v1"}
    _write(path, payload)
    catalog = load_catalog(cloned_fixture_root)
    with pytest.raises(SourceProvenanceSchemaError, match="不得带 provenance"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_cross_check_does_not_change_fixture_bytes(
    fixture_root: Path, official_root: Path
) -> None:
    """加载期交叉校验只读内存里的 `raw` ⇒ **不改变任何 fixture 字节**。

    这是 `G-PRESERVE`（"完全冻结 fixtures"）在单元层面的落点：
    如果将来有人把这条校验写成"顺手规范化一下文件"，这里会变红。
    """
    import hashlib

    def digest(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before_research, before_official = digest(fixture_root), digest(official_root)
    load_snapshots(fixture_root, load_catalog(fixture_root))
    load_snapshots(official_root, load_catalog(official_root))
    assert digest(fixture_root) == before_research
    assert digest(official_root) == before_official


# ---- INDEX.json 的可选 snapshot_set ----


def test_official_catalog_exposes_snapshot_set(official_root: Path) -> None:
    official_catalog = load_catalog(official_root)
    assert official_catalog.snapshot_set == "official.2026-09-23"


def test_research_catalog_has_no_snapshot_set(catalog: FixtureCatalog) -> None:
    """P1A 的 INDEX.json 没有这个键 ⇒ 必须是空串而不是报错（否则等于给基线加约束）。"""
    assert catalog.snapshot_set == ""


def test_malformed_snapshot_set_rejected(cloned_fixture_root: Path) -> None:
    index_path = cloned_fixture_root / "INDEX.json"
    payload = _read(index_path)
    payload["snapshot_set"] = "not a valid set id"
    _write(index_path, payload)
    with pytest.raises(SnapshotError, match="snapshot_set"):
        load_catalog(cloned_fixture_root)


# ---- 程序化构造也要受同一闭集约束 ----


def test_dataclass_construction_rejects_unknown_kind() -> None:
    with pytest.raises(UnknownSourceKindError):
        FixtureSnapshot(
            candidate_id="pgvector",
            display_name="x",
            snapshot_version="pgvector.official.2026-09-23",
            captured_at="2026-09-23",
            source_path="sources/pgvector.json",
            source_kind="handwritten_notes",
            sections=(),
            dimension_values={},
        )

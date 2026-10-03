"""快照加载与字段校验（P1A-2）。

覆盖：INDEX / option 的结构校验、版本交叉校验、本地路径约束、section 完整性，
以及「每条 fixture 证据的 locator 都能解析回原文」这条可执行的证据可解析性检查。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentboard.research.dimensions import DimensionRegistry
from agentboard.research.errors import (
    DanglingSectionError,
    SnapshotMismatchError,
    SnapshotSchemaError,
    UnknownCandidateError,
    UnsafeSourcePathError,
)
from agentboard.research.snapshot import (
    FixtureCatalog,
    load_catalog,
    load_snapshot,
    resolve_locator,
    section_locator,
)

EXPECTED_CANDIDATES = ("milvus_standalone", "pgvector", "qdrant_local")


def _raw(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _pgvector_raw(fixture_root: Path) -> dict[str, Any]:
    return _raw(fixture_root / "options" / "pgvector.json")


# ---- 目录 ----


def test_catalog_lists_candidates_sorted(catalog: FixtureCatalog) -> None:
    assert catalog.ids() == EXPECTED_CANDIDATES
    assert catalog.dimension_registry_path == "dimensions/dimensions.v1.json"
    assert catalog.root == "."


def test_catalog_get_unknown_candidate_raises(catalog: FixtureCatalog) -> None:
    with pytest.raises(UnknownCandidateError, match="没有候选"):
        catalog.get("redis_vector")
    assert catalog.has("pgvector") is True
    assert catalog.has("redis_vector") is False


def test_catalog_rejects_duplicate_candidate_id(cloned_fixture_root: Path) -> None:
    index_path = cloned_fixture_root / "INDEX.json"
    index = _raw(index_path)
    index["candidates"].append(dict(index["candidates"][0]))
    _write(index_path, index)
    with pytest.raises(SnapshotSchemaError, match="重复 candidate_id"):
        load_catalog(cloned_fixture_root)


def test_catalog_rejects_wrong_schema(cloned_fixture_root: Path) -> None:
    index_path = cloned_fixture_root / "INDEX.json"
    index = _raw(index_path)
    index["schema"] = "research.index.v2"
    _write(index_path, index)
    with pytest.raises(SnapshotSchemaError, match="schema"):
        load_catalog(cloned_fixture_root)


def test_catalog_rejects_absolute_registry_path(cloned_fixture_root: Path) -> None:
    index_path = cloned_fixture_root / "INDEX.json"
    index = _raw(index_path)
    index["dimension_registry"] = "C:/somewhere/dimensions.json"
    _write(index_path, index)
    with pytest.raises(UnsafeSourcePathError):
        load_catalog(cloned_fixture_root)


# ---- 快照字段 ----


def test_snapshot_loads_expected_fields(fixture_root: Path, catalog: FixtureCatalog) -> None:
    snapshot = load_snapshot(fixture_root / "options" / "pgvector.json", entry=catalog.get("pgvector"))
    assert snapshot.candidate_id == "pgvector"
    assert snapshot.snapshot_version == "pgvector-notes.2026-09-01"
    assert snapshot.captured_at == "2026-09-01"
    assert snapshot.source_path == "fixtures/research/options/pgvector.json"
    assert len(snapshot.sections) == 6
    assert len(snapshot.dimension_values) == 6
    assert all(section.text.strip() for section in snapshot.sections)


def test_all_real_snapshots_load(fixture_root: Path, catalog: FixtureCatalog) -> None:
    for candidate_id in catalog.ids():
        entry = catalog.get(candidate_id)
        snapshot = load_snapshot(fixture_root / entry.fixture_path, entry=entry)
        assert snapshot.candidate_id == candidate_id


def test_snapshot_rejects_version_mismatch_with_index(
    fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path
) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["snapshot_version"] = "pgvector-notes.2026-09-02"
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(SnapshotMismatchError, match="snapshot_version"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_snapshot_rejects_candidate_id_mismatch(
    fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path
) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["candidate_id"] = "somewhere_else"
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(SnapshotMismatchError, match="candidate_id"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_snapshot_rejects_wrong_schema(fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["schema"] = "research.fixture.v2"
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(SnapshotSchemaError, match="schema"):
        load_snapshot(path, entry=catalog.get("pgvector"))


@pytest.mark.parametrize("bad_path", ["https://example.com/doc.md", "/abs/path.md", "C:/abs/path.md"])
def test_snapshot_rejects_non_local_source_path(
    fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path, bad_path: str
) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["source_path"] = bad_path
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(UnsafeSourcePathError):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_snapshot_rejects_duplicate_section_id(
    fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path
) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["sections"][1]["section_id"] = payload["sections"][0]["section_id"]
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(SnapshotSchemaError, match="重复 section_id"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_snapshot_rejects_empty_sections(fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["sections"] = []
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(SnapshotSchemaError, match="sections"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_snapshot_rejects_dangling_section_reference(
    fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path
) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["dimension_values"]["query_latency"]["section_id"] = "s_does_not_exist"
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(DanglingSectionError, match="不存在的 section"):
        load_snapshot(path, entry=catalog.get("pgvector"))


def test_snapshot_rejects_non_snake_case_dimension_key(
    fixture_root: Path, catalog: FixtureCatalog, tmp_path: Path
) -> None:
    payload = _pgvector_raw(fixture_root)
    payload["dimension_values"]["Query-Latency"] = payload["dimension_values"].pop("query_latency")
    path = _write(tmp_path / "pgvector.json", payload)
    with pytest.raises(SnapshotSchemaError, match="lower_snake_case"):
        load_snapshot(path, entry=catalog.get("pgvector"))


# ---- section 定位与证据可解析性 ----


def test_section_locator_round_trip(fixture_root: Path, catalog: FixtureCatalog) -> None:
    snapshot = load_snapshot(fixture_root / "options" / "pgvector.json", entry=catalog.get("pgvector"))
    for index, section in enumerate(snapshot.sections):
        assert resolve_locator(snapshot, section_locator(index)) == section.text


def test_resolve_locator_rejects_unknown_shape(
    fixture_root: Path, catalog: FixtureCatalog
) -> None:
    snapshot = load_snapshot(fixture_root / "options" / "pgvector.json", entry=catalog.get("pgvector"))
    with pytest.raises(DanglingSectionError):
        resolve_locator(snapshot, "/sections/0/heading")


def test_resolve_locator_rejects_out_of_range(
    fixture_root: Path, catalog: FixtureCatalog
) -> None:
    snapshot = load_snapshot(fixture_root / "options" / "pgvector.json", entry=catalog.get("pgvector"))
    with pytest.raises(DanglingSectionError, match="无法解析"):
        resolve_locator(snapshot, section_locator(len(snapshot.sections)))


def test_declared_dimension_keys_and_values_are_consistent(
    fixture_root: Path, catalog: FixtureCatalog, registry: DimensionRegistry
) -> None:
    """每个候选声明的维度键都必须能在注册表里解析，取值必须在量表内。"""
    for candidate_id in catalog.ids():
        snapshot = load_snapshot(
            fixture_root / catalog.get(candidate_id).fixture_path, entry=catalog.get(candidate_id)
        )
        for raw_name, declaration in snapshot.dimension_values.items():
            dimension = registry.get(raw_name)
            assert declaration.value in dimension.scale_labels, (
                f"{candidate_id}.{raw_name} 的取值 {declaration.value!r} 不在量表内"
            )

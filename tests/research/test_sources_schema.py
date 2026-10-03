"""官方来源层（`sources.py`）的结构与校验测试。

全部离线：把真实快照复制一份到 tmp、改坏其中一处、重签、断言加载报错。
用"改坏再断言"而不是"构造合法对象再断言"，是因为要证明的是**校验真的会拦**，
而不是数据凑巧合法。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agentboard.research.errors import (
    AgentBoardResearchError,
    DanglingSectionError,
    IncompleteDimensionCoverageError,
    MissingProvenanceError,
    NonOfficialSourceError,
    SourceError,
    SourceProvenanceSchemaError,
    SourceReferenceError,
    UnknownDimensionError,
)
from agentboard.research.sources import (
    ALLOWED_SOURCE_HOSTS,
    ALLOWED_SOURCE_REPOS,
    BASIS_MAPPED,
    BASIS_STATED,
    KIND_OFFICIAL_DOCS,
    SCHEMA,
    UNKNOWN_VERSION,
    SectionSource,
    load_official_sources,
)

Mutator = Callable[[dict[str, Any]], None]


@pytest.fixture
def mutate_official(
    cloned_official_root: Path, resign_fixture: Callable[[Path], None]
) -> Callable[[str, Mutator], Path]:
    """改坏官方快照里的一处并重签，返回被改的文件路径。"""

    def _mutate(candidate: str, mutate: Mutator) -> Path:
        path = cloned_official_root / "sources" / f"{candidate}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        resign_fixture(cloned_official_root)
        return path

    return _mutate


def _load(root: Path):
    return load_official_sources(root)


def _first_document(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["provenance"]["documents"][0]


def _first_declared_dimension(payload: dict[str, Any]) -> str:
    return min(payload["dimension_values"])


def _first_unknown_dimension(payload: dict[str, Any]) -> str:
    return min(payload["provenance"]["unknown_dimensions"])


# ---- 真实快照：正常路径 ----


def test_real_provenance_loads_for_every_candidate(official_sources) -> None:
    assert official_sources.candidate_ids() == ("milvus_standalone", "pgvector", "qdrant_local")
    for candidate_id in official_sources.candidate_ids():
        provenance = official_sources.provenance_for(candidate_id)
        assert provenance.schema == SCHEMA
        assert provenance.candidate_id == candidate_id
        assert provenance.documents
        assert provenance.collection_method
        assert provenance.collected_at == official_sources.snapshot(candidate_id).captured_at


def test_allowlist_covers_every_real_candidate(official_sources) -> None:
    """新候选不能绕过白名单：没有登记就等于加载失败。"""
    for candidate_id in official_sources.candidate_ids():
        assert candidate_id in ALLOWED_SOURCE_REPOS


def test_real_documents_are_official_and_https(official_sources) -> None:
    documents = official_sources.documents()
    assert documents
    for document in documents:
        assert document.kind in {KIND_OFFICIAL_DOCS, "official_repo_file"}
        host = document.url.split("//", 1)[1].split("/", 1)[0]
        assert host in ALLOWED_SOURCE_HOSTS
        # 定位符固定到 commit，不用分支名
        assert document.raw_url == (
            f"https://raw.githubusercontent.com/{document.repo}/{document.commit_sha}/{document.path}"
        )
        assert document.url == (
            f"https://github.com/{document.repo}/blob/{document.commit_sha}/{document.path}"
        )
        assert document.commit_sha not in (document.ref,)
        assert document.raw_lines >= 1


def test_documents_have_distinct_anchors(official_sources) -> None:
    documents = official_sources.documents()
    pairs = [(document.repo, document.ref, document.path) for document in documents]
    assert len(set(pairs)) == len(pairs)
    assert all(len(document.commit_sha) == 40 for document in documents)
    assert all(len(document.blob_sha) == 40 for document in documents)
    assert all(len(document.raw_sha256) == 64 for document in documents)


def test_provenance_to_dict_is_json_serializable(official_sources) -> None:
    for candidate_id in official_sources.candidate_ids():
        payload = official_sources.provenance_for(candidate_id).to_dict()
        assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_line_span_parsing() -> None:
    assert SectionSource(section_id="s", source_id="d", line_range="55").line_span() == (55, 55)
    assert SectionSource(section_id="s", source_id="d", line_range="129-130").line_span() == (
        129,
        130,
    )


def test_error_hierarchy() -> None:
    for error in (
        SourceError,
        MissingProvenanceError,
        SourceProvenanceSchemaError,
        NonOfficialSourceError,
        SourceReferenceError,
        IncompleteDimensionCoverageError,
    ):
        assert issubclass(error, AgentBoardResearchError)
        assert issubclass(error, ValueError)


# ---- 结构校验：改坏就报错 ----


def test_missing_provenance_block_rejected(mutate_official, cloned_official_root) -> None:
    mutate_official("pgvector", lambda payload: payload.pop("provenance"))
    with pytest.raises(MissingProvenanceError, match="provenance"):
        _load(cloned_official_root)


def test_wrong_schema_rejected(mutate_official, cloned_official_root) -> None:
    mutate_official("pgvector", lambda payload: payload["provenance"].update({"schema": "x.v9"}))
    with pytest.raises(SourceProvenanceSchemaError, match="schema"):
        _load(cloned_official_root)


def test_candidate_id_mismatch_rejected(mutate_official, cloned_official_root) -> None:
    mutate_official(
        "pgvector", lambda payload: payload["provenance"].update({"candidate_id": "qdrant_local"})
    )
    with pytest.raises(SourceProvenanceSchemaError, match="candidate_id"):
        _load(cloned_official_root)


def test_collected_at_mismatch_rejected(mutate_official, cloned_official_root) -> None:
    mutate_official(
        "pgvector", lambda payload: payload["provenance"].update({"collected_at": "2001-01-01"})
    )
    with pytest.raises(SourceProvenanceSchemaError, match="collected_at"):
        _load(cloned_official_root)


def test_empty_documents_rejected(mutate_official, cloned_official_root) -> None:
    mutate_official("pgvector", lambda payload: payload["provenance"].update({"documents": []}))
    with pytest.raises(SourceProvenanceSchemaError, match="documents"):
        _load(cloned_official_root)


def test_duplicate_source_id_rejected(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        documents = payload["provenance"]["documents"]
        documents.append(dict(documents[0]))

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceProvenanceSchemaError, match="重复 source_id"):
        _load(cloned_official_root)


def test_missing_section_source_field_rejected(mutate_official, cloned_official_root) -> None:
    mutate_official("pgvector", lambda payload: payload["sections"][0].pop("line_range"))
    with pytest.raises(MissingProvenanceError, match="line_range"):
        _load(cloned_official_root)


# ---- 官方性校验 ----


def test_non_official_repo_rejected(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        document = _first_document(payload)
        document["repo"] = "someone/pgvector-fork"
        document["path"] = "README.md"
        document["url"] = "https://github.com/someone/pgvector-fork/blob/master/README.md"
        document["raw_url"] = (
            "https://raw.githubusercontent.com/someone/pgvector-fork/master/README.md"
        )

    mutate_official("pgvector", _mutate)
    with pytest.raises(NonOfficialSourceError, match="官方仓库"):
        _load(cloned_official_root)


def test_unknown_candidate_has_no_allowlist(
    monkeypatch, official_root: Path, official_sources
) -> None:
    """候选没有登记官方仓库时，宁可报错也不放宽校验。"""
    from agentboard.research import sources as sources_module

    assert official_sources.candidate_ids()
    monkeypatch.setattr(sources_module, "ALLOWED_SOURCE_REPOS", {})
    with pytest.raises(NonOfficialSourceError, match="没有登记允许的官方仓库"):
        _load(official_root)


def test_non_allowlisted_host_rejected(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        document = _first_document(payload)
        document["url"] = "https://evil.example.com/pgvector/blob/master/README.md"

    mutate_official("pgvector", _mutate)
    with pytest.raises(NonOfficialSourceError, match="白名单"):
        _load(cloned_official_root)


def test_branch_pinned_url_rejected(mutate_official, cloned_official_root) -> None:
    """用分支名当定位符必须被拒：分支会移动，复核时就再也对不上 sha256 与行号。"""

    def _mutate(payload: dict[str, Any]) -> None:
        document = _first_document(payload)
        document["url"] = (
            f"https://github.com/{document['repo']}/blob/{document['ref']}/{document['path']}"
        )

    mutate_official("pgvector", _mutate)
    with pytest.raises(NonOfficialSourceError, match="固定到 commit"):
        _load(cloned_official_root)


def test_branch_pinned_raw_url_rejected(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        document = _first_document(payload)
        document["raw_url"] = (
            f"https://raw.githubusercontent.com/{document['repo']}/{document['ref']}/"
            f"{document['path']}"
        )

    mutate_official("pgvector", _mutate)
    with pytest.raises(NonOfficialSourceError, match="固定到 commit"):
        _load(cloned_official_root)


def test_url_path_must_match_recorded_path(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        document = _first_document(payload)
        document["url"] = (
            f"https://github.com/{document['repo']}/blob/{document['commit_sha']}/OTHER.md"
        )

    mutate_official("pgvector", _mutate)
    with pytest.raises(NonOfficialSourceError, match="固定到 commit"):
        _load(cloned_official_root)


# ---- 字段格式 ----


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("commit_sha", "abc", "commit_sha"),
        ("blob_sha", "Z" * 40, "blob_sha"),
        ("raw_sha256", "0" * 63, "raw_sha256"),
        ("raw_lines", 0, "raw_lines"),
        ("raw_lines", True, "raw_lines"),
        ("raw_lines", "1361", "raw_lines"),
        ("ref_kind", "commit", "ref_kind"),
        ("kind", "blog_post", "kind"),
        ("path", "/etc/passwd", "相对路径"),
        ("path", "../outside.md", "越出仓库根目录"),
        ("title", "   ", "title"),
        ("release_tag", "latest", "release_tag"),
    ],
)
def test_document_field_validation(mutate_official, cloned_official_root, key, value, match) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        _first_document(payload)[key] = value

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceError, match=match):
        _load(cloned_official_root)


@pytest.mark.parametrize("bad_range", ["0", "12-", "1-2-3", "L12", "abc"])
def test_bad_line_range_rejected(mutate_official, cloned_official_root, bad_range) -> None:
    mutate_official("pgvector", lambda payload: payload["sections"][0].update({"line_range": bad_range}))
    with pytest.raises(SourceProvenanceSchemaError, match="line_range"):
        _load(cloned_official_root)


def test_line_range_start_after_end_rejected(mutate_official, cloned_official_root) -> None:
    mutate_official("pgvector", lambda payload: payload["sections"][0].update({"line_range": "9-2"}))
    with pytest.raises(SourceProvenanceSchemaError, match="起始行"):
        _load(cloned_official_root)


def test_line_range_beyond_source_file_rejected(mutate_official, cloned_official_root) -> None:
    """引用超过来源文件行数的行号必须在加载期报错（这一条正是"locator 不成立"的守卫）。"""

    def _mutate(payload: dict[str, Any]) -> None:
        document = _first_document(payload)
        payload["sections"][0]["line_range"] = str(document["raw_lines"] + 1)

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceReferenceError, match="超出来源"):
        _load(cloned_official_root)


def test_release_tag_unknown_requires_reason(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        document = _first_document(payload)
        document["release_tag"] = UNKNOWN_VERSION
        document["release_tag_note"] = "无"

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceProvenanceSchemaError, match="release_tag_note"):
        _load(cloned_official_root)


# ---- 交叉引用一致性 ----


def test_section_source_id_must_exist(mutate_official, cloned_official_root) -> None:
    mutate_official("pgvector", lambda payload: payload["sections"][0].update({"source_id": "doc_nope"}))
    with pytest.raises(SourceReferenceError, match="不存在的来源"):
        _load(cloned_official_root)


def test_dimension_source_must_match_section_owner(mutate_official, cloned_official_root) -> None:
    """维度声明指向的来源，必须与该 section 登记的来源是同一个文件。"""

    def _mutate(payload: dict[str, Any]) -> None:
        payload["provenance"]["documents"].append(
            {**_first_document(payload), "source_id": "doc_second_source"}
        )
        payload["dimension_values"][_first_declared_dimension(payload)]["source_id"] = (
            "doc_second_source"
        )

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceReferenceError, match="与 section"):
        _load(cloned_official_root)


def test_dimension_section_id_must_be_a_real_section_in_snapshot_layer(
    mutate_official, cloned_official_root
) -> None:
    """来源层不再重复这条检查：快照层的 `DanglingSectionError` 已经是第一道防线。"""

    def _mutate(payload: dict[str, Any]) -> None:
        payload["dimension_values"][_first_declared_dimension(payload)]["section_id"] = "s_nope"

    mutate_official("pgvector", _mutate)
    with pytest.raises(DanglingSectionError, match="不存在的 section"):
        _load(cloned_official_root)


def test_unknown_dimension_alias_key_rejected(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        unknown = payload["provenance"]["unknown_dimensions"]
        entry = unknown.pop(_first_unknown_dimension(payload))
        unknown["deploy_complexity"] = entry  # deploy_complexity 是 alias，不是 canonical id

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceProvenanceSchemaError, match="canonical"):
        _load(cloned_official_root)


def test_unknown_dimension_checked_sources_must_exist(
    mutate_official, cloned_official_root
) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        entry = payload["provenance"]["unknown_dimensions"][_first_unknown_dimension(payload)]
        entry["checked_source_ids"] = ["doc_nope"]

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceReferenceError, match="不存在的来源"):
        _load(cloned_official_root)


def test_unknown_dimension_requires_checked_sources(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        entry = payload["provenance"]["unknown_dimensions"][_first_unknown_dimension(payload)]
        entry["checked_source_ids"] = []

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceProvenanceSchemaError, match="checked_source_ids"):
        _load(cloned_official_root)


# ---- 维度声明：basis / 完整性 ----


def test_invalid_basis_rejected(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        payload["dimension_values"][_first_declared_dimension(payload)]["basis"] = "guessed"

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceProvenanceSchemaError, match="basis"):
        _load(cloned_official_root)


def test_mapped_basis_requires_derivation(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        entry = payload["dimension_values"][_first_declared_dimension(payload)]
        entry["basis"] = BASIS_MAPPED
        entry["derivation"] = "因为"

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceProvenanceSchemaError, match="derivation"):
        _load(cloned_official_root)


def test_stated_basis_is_accepted(mutate_official, cloned_official_root) -> None:
    def _mutate(payload: dict[str, Any]) -> None:
        payload["dimension_values"][_first_declared_dimension(payload)]["basis"] = BASIS_STATED

    mutate_official("pgvector", _mutate)
    assert _load(cloned_official_root).candidate_ids() == (
        "milvus_standalone",
        "pgvector",
        "qdrant_local",
    )


def test_silent_dimension_omission_rejected(mutate_official, cloned_official_root) -> None:
    """删掉一个既未声明、也未登记 unknown 的维度 —— 必须报错，不能静默消失。"""

    def _mutate(payload: dict[str, Any]) -> None:
        payload["dimension_values"].pop(_first_declared_dimension(payload))

    mutate_official("pgvector", _mutate)
    with pytest.raises(IncompleteDimensionCoverageError, match="静默省略"):
        _load(cloned_official_root)


def test_dimension_cannot_be_declared_and_unknown(mutate_official, cloned_official_root) -> None:
    """同一个维度不能既声明取值、又登记为 unknown —— 那会同时说"有依据"和"没依据"。"""

    def _mutate(payload: dict[str, Any]) -> None:
        unknown_key = _first_unknown_dimension(payload)
        declared_key = _first_declared_dimension(payload)
        payload["dimension_values"][unknown_key] = dict(payload["dimension_values"][declared_key])

    mutate_official("pgvector", _mutate)
    with pytest.raises(SourceProvenanceSchemaError, match="同时出现"):
        _load(cloned_official_root)


def test_unknown_registry_dimension_rejected_by_registry(
    mutate_official, cloned_official_root
) -> None:
    """新增维度必须先改注册表：写一个注册表里没有的维度，加载即报未知维度。"""

    def _mutate(payload: dict[str, Any]) -> None:
        declared = _first_declared_dimension(payload)
        payload["dimension_values"]["cost_per_gb"] = dict(payload["dimension_values"][declared])

    mutate_official("pgvector", _mutate)
    with pytest.raises(UnknownDimensionError, match="未知维度"):
        _load(cloned_official_root)


def test_provenance_dimension_map_is_read_only(official_sources) -> None:
    provenance = official_sources.provenance_for("pgvector")
    with pytest.raises(TypeError):
        provenance.dimension_values["new"] = None  # type: ignore[index]


def test_document_lookup_error_is_typed(official_sources) -> None:
    provenance = official_sources.provenance_for("pgvector")
    with pytest.raises(SourceReferenceError, match="没有"):
        provenance.document("doc_nope")
    with pytest.raises(SourceReferenceError, match="没有 section"):
        provenance.section("s_nope")


def test_provenance_block_is_a_mapping(official_sources) -> None:
    for candidate_id in official_sources.candidate_ids():
        provenance = official_sources.provenance_for(candidate_id)
        assert isinstance(provenance.dimension_values, Mapping)
        assert isinstance(provenance.unknown_dimensions, Mapping)

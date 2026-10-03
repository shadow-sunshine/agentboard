"""legacy 证据显式迁移器单测（P1A-1）。

核心契约层不做隐式兼容（`from_dict` 遇字符串直接报错），历史文本证据的可读性
由 `agentboard.research.migrate` 显式提供，并且是**有损标注**：
迁移产物不得被当作真实资料快照证据。
"""

from __future__ import annotations

import pytest

from agentboard.contracts import WorkerResult
from agentboard.evidence import (
    MAX_EXCERPT_CHARS,
    SOURCE_LEGACY,
    EvidenceRef,
    is_trusted_evidence,
)
from agentboard.research.migrate import (
    LEGACY_CAPTURED_AT,
    LegacyMigrationError,
    migrate_legacy_evidence,
    migrate_legacy_result,
)
from agentboard.workers.synthetic import SyntheticEvidenceFactory


def test_migrates_string_evidence_into_legacy_refs():
    refs = migrate_legacy_evidence(["来源: mock adapter for t1", "另一条旧证据"], task_id="t1")
    assert len(refs) == 2
    first = refs[0]
    assert isinstance(first, EvidenceRef)
    assert first.source_type == SOURCE_LEGACY
    assert first.source_id == "legacy:t1"
    assert first.snapshot_version == "legacy.0"
    assert first.locator == "/legacy/t1/0"
    assert first.excerpt == "来源: mock adapter for t1"
    assert first.captured_at == LEGACY_CAPTURED_AT
    assert refs[1].locator == "/legacy/t1/1"


def test_migrated_evidence_is_never_trusted():
    """legacy 证据是有损降级产物，不得冒充真实 fixture 证据。"""
    refs = migrate_legacy_evidence(["旧文本"], task_id="t1")
    assert all(is_trusted_evidence(ref) is False for ref in refs)


def test_migration_is_deterministic():
    first = migrate_legacy_evidence(["同一段旧文本"], task_id="t1")
    second = migrate_legacy_evidence(["同一段旧文本"], task_id="t1")
    assert first == second
    assert first[0].evidence_id == second[0].evidence_id


def test_already_structured_evidence_passes_through():
    ref = SyntheticEvidenceFactory.make("research", "t1", 0)
    assert migrate_legacy_evidence([ref], task_id="t1") == (ref,)
    assert migrate_legacy_evidence([ref.to_dict()], task_id="t1") == (ref,)


def test_migration_rejects_unsupported_element_types():
    for bad in (123, None, 1.5, ["nested"]):
        with pytest.raises(LegacyMigrationError):
            migrate_legacy_evidence([bad], task_id="t1")


def test_migration_rejects_blank_strings():
    with pytest.raises(LegacyMigrationError):
        migrate_legacy_evidence(["   "], task_id="t1")


def test_migration_requires_explicit_task_id():
    for bad_task_id in ("", "   ", None, 7):
        with pytest.raises(LegacyMigrationError):
            migrate_legacy_evidence(["旧文本"], task_id=bad_task_id)  # type: ignore[arg-type]


def test_migration_rejects_non_sequence():
    with pytest.raises(LegacyMigrationError):
        migrate_legacy_evidence("一条字符串", task_id="t1")


def test_none_evidence_becomes_empty_tuple():
    assert migrate_legacy_evidence(None, task_id="t1") == ()


def test_long_excerpt_is_truncated_and_digest_matches_truncation():
    refs = migrate_legacy_evidence(["字" * (MAX_EXCERPT_CHARS + 50)], task_id="t1")
    assert len(refs[0].excerpt) == MAX_EXCERPT_CHARS
    # digest 必须对应截断后的文本，否则 EvidenceRef 构造本身就会失败
    assert refs[0].digest == EvidenceRef.from_dict(refs[0].to_dict()).digest


# ---- migrate_legacy_result ----


def _legacy_result() -> dict:
    return {
        "task_id": "compare_plan",
        "agent_name": "planner",
        "facts": ["旧事实"],
        "evidence": ["来源: 旧的自由文本证据"],
        "risks": [],
        "open_questions": [],
        "confidence": 0.7,
    }


def test_migrate_legacy_result_does_not_mutate_input():
    raw = _legacy_result()
    snapshot = {key: list(value) if isinstance(value, list) else value for key, value in raw.items()}
    migrated = migrate_legacy_result(raw)
    assert raw == snapshot
    assert raw["evidence"] == ["来源: 旧的自由文本证据"]
    assert isinstance(migrated["evidence"][0], dict)
    assert migrated is not raw


def test_migrate_legacy_result_feeds_strict_from_dict():
    """迁移产物必须能直接喂给严格的 WorkerResult.from_dict。"""
    result = WorkerResult.from_dict(migrate_legacy_result(_legacy_result()))
    assert result.task_id == "compare_plan"
    assert len(result.evidence) == 1
    assert result.evidence[0].source_type == SOURCE_LEGACY
    assert is_trusted_evidence(result.evidence[0]) is False


def test_migrate_legacy_result_accepts_explicit_task_id_override():
    without_task_id = _legacy_result()
    without_task_id.pop("task_id")
    with pytest.raises(LegacyMigrationError):
        migrate_legacy_result(without_task_id)
    migrated = migrate_legacy_result(without_task_id, task_id="t9")
    assert migrated["evidence"][0]["source_id"] == "legacy:t9"
    assert migrated["evidence"][0]["locator"] == "/legacy/t9/0"


def test_migrate_legacy_result_requires_dict():
    with pytest.raises(LegacyMigrationError):
        migrate_legacy_result(["not", "a", "dict"])  # type: ignore[arg-type]


def test_legacy_migration_error_is_value_error():
    assert issubclass(LegacyMigrationError, ValueError)

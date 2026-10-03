"""`EvidenceRef` 单测（P1A-1）：内容寻址、篡改检测、字段边界、序列化往返。

全部是纯构造与纯函数，不依赖真实模型 / 网络 / ACP（需求11）。
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from agentboard.evidence import (
    MAX_EXCERPT_CHARS,
    SOURCE_FIXTURE,
    SOURCE_LEGACY,
    SOURCE_SYNTHETIC,
    EvidenceRef,
    compute_digest,
    compute_evidence_id,
    is_trusted_evidence,
)
from agentboard.workers.synthetic import FIXED_CAPTURED_AT, SyntheticEvidenceFactory


def _fixture_ref(**overrides: object) -> EvidenceRef:
    base: dict[str, object] = {
        "source_type": SOURCE_FIXTURE,
        "source_id": "fixture:pgvector",
        "snapshot_version": "pgvector-notes.2026-09-01",
        "locator": "/sections/0/text",
        "excerpt": "以 Postgres 扩展方式安装，复用既有实例，无需独立集群。",
        "captured_at": "2026-09-01",
    }
    base.update(overrides)
    return EvidenceRef(**base)  # type: ignore[arg-type]


# ---- 1. evidence_id 内容寻址与稳定性 ----


def test_evidence_id_is_stable_across_constructions():
    first = _fixture_ref()
    second = _fixture_ref()
    assert first.evidence_id == second.evidence_id
    assert first == second
    assert first.digest == second.digest


def test_evidence_id_changes_when_excerpt_changes_by_one_char():
    base = _fixture_ref()
    tweaked = _fixture_ref(excerpt=base.excerpt + "。")
    assert tweaked.digest != base.digest
    assert tweaked.evidence_id != base.evidence_id


def test_evidence_id_changes_when_locator_changes():
    base = _fixture_ref()
    moved = _fixture_ref(locator="/sections/1/text")
    assert moved.evidence_id != base.evidence_id


def test_fixture_and_synthetic_ids_never_collide():
    """哈希输入含 source_type，因此同 locator 的两类证据 id 必然不同。"""
    synthetic = SyntheticEvidenceFactory.make("research", "sections", 0)
    fixture = _fixture_ref(locator=synthetic.locator, excerpt=synthetic.excerpt)
    assert fixture.source_type != synthetic.source_type
    assert fixture.evidence_id != synthetic.evidence_id


def test_evidence_id_format_is_ev_plus_16_lowercase_alnum():
    ref = _fixture_ref()
    assert ref.evidence_id.startswith("ev_")
    assert len(ref.evidence_id) == 19
    assert ref.evidence_id[3:].isalnum()
    assert ref.evidence_id[3:].islower() or ref.evidence_id[3:].isdigit()


def test_compute_evidence_id_is_pure():
    kwargs = {
        "source_type": SOURCE_SYNTHETIC,
        "source_id": "mock:research",
        "snapshot_version": "synthetic.1",
        "locator": "/synthetic/t1/0",
        "digest": compute_digest("x"),
    }
    assert compute_evidence_id(**kwargs) == compute_evidence_id(**kwargs)


# ---- 2. digest 派生与篡改检测 ----


def test_digest_is_derived_from_excerpt():
    ref = _fixture_ref()
    assert ref.digest == hashlib.sha256(ref.excerpt.encode("utf-8")).hexdigest()
    assert ref.digest == compute_digest(ref.excerpt)
    assert len(ref.digest) == 64


def test_explicit_correct_digest_is_accepted():
    excerpt = "显式给出摘要"
    ref = _fixture_ref(excerpt=excerpt, digest=compute_digest(excerpt))
    assert ref.digest == compute_digest(excerpt)


def test_explicit_wrong_digest_is_rejected():
    with pytest.raises(ValueError, match="digest"):
        _fixture_ref(digest="0" * 64)


def test_tampered_excerpt_with_stale_digest_is_rejected():
    """摘录被改写但摘要没跟着改 —— 必须当场拒绝，而不是悄悄接受。"""
    original = _fixture_ref()
    with pytest.raises(ValueError, match="digest"):
        _fixture_ref(excerpt="被篡改的内容", digest=original.digest)


def test_digest_must_be_hex64():
    with pytest.raises(ValueError, match="digest"):
        _fixture_ref(digest="NOT-A-HEX-DIGEST")


def test_from_dict_rejects_tampered_payload():
    payload = _fixture_ref().to_dict()
    payload["excerpt"] = "偷偷改掉"
    with pytest.raises(ValueError, match="digest"):
        EvidenceRef.from_dict(payload)


def test_forged_evidence_id_is_rejected():
    with pytest.raises(ValueError, match="evidence_id"):
        _fixture_ref(evidence_id="ev_0123456789abcdef")


def test_evidence_id_wrong_shape_is_rejected():
    with pytest.raises(ValueError, match="evidence_id"):
        _fixture_ref(evidence_id="EV_UPPERCASE")


# ---- 3. source_type / source_id 校验 ----


@pytest.mark.parametrize(
    "source_type",
    ["web", "llm", "internet", "", "  ", "FIXTURE_SNAPSHOT", "whatever"],
)
def test_unknown_source_type_is_rejected(source_type: str):
    with pytest.raises(ValueError, match="source_type"):
        _fixture_ref(source_type=source_type)


@pytest.mark.parametrize(
    ("source_type", "source_id"),
    [
        (SOURCE_FIXTURE, "mock:research"),
        (SOURCE_FIXTURE, "legacy:t1"),
        (SOURCE_FIXTURE, "pgvector"),
        (SOURCE_SYNTHETIC, "fixture:pgvector"),
        (SOURCE_SYNTHETIC, "research"),
        (SOURCE_LEGACY, "mock:research"),
        (SOURCE_LEGACY, "t1"),
    ],
)
def test_source_id_prefix_must_match_source_type(source_type: str, source_id: str):
    with pytest.raises(ValueError, match="source_id"):
        _fixture_ref(source_type=source_type, source_id=source_id)


@pytest.mark.parametrize(
    ("source_type", "source_id"),
    [
        (SOURCE_FIXTURE, "fixture:pgvector"),
        (SOURCE_SYNTHETIC, "mock:research"),
        (SOURCE_LEGACY, "legacy:t1"),
    ],
)
def test_matching_prefix_is_accepted(source_type: str, source_id: str):
    assert _fixture_ref(source_type=source_type, source_id=source_id).source_id == source_id


# ---- 4. locator 校验（受限 JSON Pointer，全项目一种语法） ----


@pytest.mark.parametrize(
    "locator",
    [
        "/sections/0/text",
        "/synthetic/research__pgvector/0",
        "/legacy/t1/3",
        "/a",
    ],
)
def test_valid_locators_are_accepted(locator: str):
    assert _fixture_ref(locator=locator).locator == locator


@pytest.mark.parametrize(
    "locator",
    [
        "",
        "   ",
        "sections/0/text",
        "/",
        "/sections//text",
        "/sections/0/text/",
        "/sections/0/text~1x",
        "mock:research:t1:0",
        "L12-L18",
        "https://example.com/doc",
    ],
)
def test_invalid_locators_are_rejected(locator: str):
    with pytest.raises(ValueError, match="locator"):
        _fixture_ref(locator=locator)


# ---- 5. snapshot_version 校验（点号风格，全项目禁用 @） ----


@pytest.mark.parametrize(
    "version",
    ["synthetic.1", "legacy.0", "pgvector-notes.2026-09-01", "2026-09-01", "abc"],
)
def test_valid_snapshot_versions_are_accepted(version: str):
    assert _fixture_ref(snapshot_version=version).snapshot_version == version


@pytest.mark.parametrize("version", ["synthetic@1", "legacy@0", "AB.1", "x", "", ".1", "-1"])
def test_invalid_snapshot_versions_are_rejected(version: str):
    with pytest.raises(ValueError, match="snapshot_version"):
        _fixture_ref(snapshot_version=version)


# ---- 6. excerpt / captured_at 校验 ----


def test_excerpt_must_be_non_empty():
    for excerpt in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="excerpt"):
            _fixture_ref(excerpt=excerpt)


def test_excerpt_length_boundary():
    assert len(_fixture_ref(excerpt="a" * MAX_EXCERPT_CHARS).excerpt) == MAX_EXCERPT_CHARS
    with pytest.raises(ValueError, match="excerpt"):
        _fixture_ref(excerpt="a" * (MAX_EXCERPT_CHARS + 1))


def test_excerpt_is_not_normalized():
    """excerpt 逐字节保留：任何空白规范化都会让同一段文本算出不同 id。"""
    excerpt = "第一行\n\n  第二行  "
    assert _fixture_ref(excerpt=excerpt).excerpt == excerpt


@pytest.mark.parametrize("captured_at", ["2026-01-01", "1970-01-01"])
def test_valid_captured_at_is_accepted(captured_at: str):
    assert _fixture_ref(captured_at=captured_at).captured_at == captured_at


@pytest.mark.parametrize("captured_at", ["", "now", "2026/01/01", "2026-1-1", "20260101", "2026-01-01T00:00"])
def test_invalid_captured_at_is_rejected(captured_at: str):
    with pytest.raises(ValueError, match="captured_at"):
        _fixture_ref(captured_at=captured_at)


# ---- 7. 序列化往返与不可变 ----


def test_round_trip():
    ref = _fixture_ref()
    back = EvidenceRef.from_dict(ref.to_dict())
    assert back == ref
    assert back.to_dict() == ref.to_dict()


def test_round_trip_without_derived_fields():
    """digest / evidence_id 省略时由内容派生，序列化后再读回仍相等。"""
    payload = _fixture_ref().to_dict()
    payload.pop("digest")
    payload.pop("evidence_id")
    restored = EvidenceRef.from_dict(payload)
    assert restored == _fixture_ref()


def test_from_dict_requires_dict():
    with pytest.raises(ValueError):
        EvidenceRef.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_from_dict_rejects_missing_fields():
    with pytest.raises(ValueError):
        EvidenceRef.from_dict({"source_type": SOURCE_FIXTURE})


def test_from_dict_rejects_non_string_optional_field():
    payload = _fixture_ref().to_dict()
    payload["digest"] = 12345
    with pytest.raises(ValueError, match="digest"):
        EvidenceRef.from_dict(payload)


def test_evidence_ref_is_frozen():
    ref = _fixture_ref()
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        ref.excerpt = "改写"  # type: ignore[misc]


# ---- 8. 可信证据判定 ----


def test_only_fixture_snapshot_is_trusted():
    assert is_trusted_evidence(_fixture_ref()) is True
    assert is_trusted_evidence(SyntheticEvidenceFactory.make("research", "t1", 0)) is False
    assert is_trusted_evidence(_fixture_ref(source_type=SOURCE_LEGACY, source_id="legacy:t1")) is False


def test_is_trusted_evidence_rejects_non_ref():
    with pytest.raises(ValueError, match="EvidenceRef"):
        is_trusted_evidence("不是 EvidenceRef")  # type: ignore[arg-type]


# ---- 9. 合成证据工厂 ----


def test_synthetic_factory_is_deterministic_and_clock_free():
    first = SyntheticEvidenceFactory.make("research", "t1", 0)
    second = SyntheticEvidenceFactory.make("research", "t1", 0)
    assert first == second
    assert first.captured_at == FIXED_CAPTURED_AT


def test_synthetic_factory_for_task_orders_by_index():
    refs = SyntheticEvidenceFactory.for_task("planner", "t1", count=3)
    assert [ref.locator for ref in refs] == ["/synthetic/t1/0", "/synthetic/t1/1", "/synthetic/t1/2"]
    assert len({ref.evidence_id for ref in refs}) == 3


def test_synthetic_factory_rejects_bad_inputs():
    with pytest.raises(ValueError):
        SyntheticEvidenceFactory.make("", "t1", 0)
    with pytest.raises(ValueError):
        SyntheticEvidenceFactory.make("research", "  ", 0)
    with pytest.raises(ValueError):
        SyntheticEvidenceFactory.make("research", "t1", -1)
    with pytest.raises(ValueError):
        SyntheticEvidenceFactory.make("research", "t1", True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SyntheticEvidenceFactory.for_task("research", "t1", count=-1)

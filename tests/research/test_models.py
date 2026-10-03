"""`ResearchFinding` 模型单测（P1A-2）。

重点覆盖 status 与 evidence 的双向硬约束、finding_id 内容寻址、
以及"局部校验不做上下文校验"这条分层边界（value 是否命中量表标签留到上层）。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest

from agentboard.research.config import MAX_CLAIM_CHARS, UNSUPPORTED_VALUE
from agentboard.research.errors import ResearchValueError
from agentboard.research.models import ResearchFinding, compute_finding_id
from agentboard.workers.synthetic import SyntheticEvidenceFactory

REF = SyntheticEvidenceFactory.make("research", "research__pgvector", 0)

BASE: dict[str, Any] = {
    "candidate_id": "pgvector",
    "task_id": "research__pgvector",
    "agent_name": "research",
    "dimension_id": "deployment_complexity",
    "claim": "以扩展形式安装并复用既有实例",
    "value": "low",
    "status": "supported",
    "evidence_refs": (REF,),
    "confidence": 1.0,
}


def _finding(**overrides: Any) -> ResearchFinding:
    payload = dict(BASE)
    payload.update(overrides)
    return ResearchFinding(**payload)


def test_supported_finding_is_valid() -> None:
    finding = _finding()
    assert finding.is_supported is True
    assert finding.value == "low"
    assert len(finding.evidence_refs) == 1


def test_unsupported_finding_is_valid() -> None:
    finding = _finding(
        status="unsupported",
        value=UNSUPPORTED_VALUE,
        evidence_refs=(),
        confidence=0.0,
        claim="资料快照未覆盖该维度",
    )
    assert finding.is_supported is False
    assert finding.evidence_refs == ()


def test_supported_without_evidence_rejected() -> None:
    with pytest.raises(ResearchValueError, match="至少引用一条证据"):
        _finding(evidence_refs=())


def test_unsupported_with_evidence_rejected() -> None:
    with pytest.raises(ResearchValueError, match="不能引用证据"):
        _finding(status="unsupported", evidence_refs=(REF,))


def test_duplicate_evidence_ids_rejected() -> None:
    with pytest.raises(ResearchValueError, match="重复 evidence_id"):
        _finding(evidence_refs=(REF, REF))


def test_non_evidence_ref_rejected() -> None:
    with pytest.raises(ResearchValueError, match="EvidenceRef"):
        _finding(evidence_refs=("来源: 自由文本",))


@pytest.mark.parametrize("bad_status", ["SUPPORTED", "ok", "", "partial", None])
def test_invalid_status_rejected(bad_status: object) -> None:
    with pytest.raises(ResearchValueError, match="status"):
        _finding(status=bad_status)


@pytest.mark.parametrize("bad_candidate", ["Pgvector", "1pgvector", "", "pg vector"])
def test_invalid_candidate_id_rejected(bad_candidate: str) -> None:
    with pytest.raises(ResearchValueError, match="candidate_id"):
        _finding(candidate_id=bad_candidate)


@pytest.mark.parametrize("bad_dimension", ["Deployment", "", "1dim"])
def test_invalid_dimension_id_rejected(bad_dimension: str) -> None:
    with pytest.raises(ResearchValueError, match="dimension_id"):
        _finding(dimension_id=bad_dimension)


def test_claim_must_be_non_empty_and_bounded() -> None:
    with pytest.raises(ResearchValueError, match="claim"):
        _finding(claim="   ")
    assert len(_finding(claim="字" * MAX_CLAIM_CHARS).claim) == MAX_CLAIM_CHARS
    with pytest.raises(ResearchValueError, match="claim"):
        _finding(claim="字" * (MAX_CLAIM_CHARS + 1))


@pytest.mark.parametrize("bad_confidence", [-0.1, 1.1, "1.0", True])
def test_confidence_must_be_unit_float(bad_confidence: object) -> None:
    with pytest.raises(ResearchValueError, match="confidence"):
        _finding(confidence=bad_confidence)


def test_value_is_not_validated_against_scale_at_local_level() -> None:
    """L0 只校验 value 非空；"是否命中量表标签"属于上下文校验（需要注册表）。

    这条不是遗漏而是分层设计：模型不持有注册表，硬塞进来会让数据类依赖领域对象，
    破坏可序列化与相等语义。
    """
    assert _finding(value="not-on-any-scale").value == "not-on-any-scale"


# ---- finding_id ----


def test_finding_id_is_derived_and_stable() -> None:
    assert _finding().finding_id == _finding().finding_id
    assert _finding().finding_id.startswith("rf_")


def test_finding_id_changes_with_status() -> None:
    supported = _finding()
    unsupported = _finding(status="unsupported", evidence_refs=(), confidence=0.0)
    assert supported.finding_id != unsupported.finding_id


def test_finding_id_changes_with_claim_and_dimension() -> None:
    base = _finding()
    assert _finding(claim="另一条结论").finding_id != base.finding_id
    assert _finding(dimension_id="query_latency").finding_id != base.finding_id
    assert _finding(candidate_id="qdrant_local").finding_id != base.finding_id


def test_compute_finding_id_is_pure_and_order_insensitive() -> None:
    other = SyntheticEvidenceFactory.make("research", "research__pgvector", 1)
    first = compute_finding_id(
        candidate_id="pgvector",
        dimension_id="deployment_complexity",
        status="supported",
        claim="c",
        evidence_ids=(REF.evidence_id, other.evidence_id),
    )
    second = compute_finding_id(
        candidate_id="pgvector",
        dimension_id="deployment_complexity",
        status="supported",
        claim="c",
        evidence_ids=(other.evidence_id, REF.evidence_id),
    )
    assert first == second


def test_forged_finding_id_rejected() -> None:
    with pytest.raises(ResearchValueError, match="finding_id"):
        _finding(finding_id="rf_0123456789abcdef")


# ---- 序列化 ----


def test_round_trip_supported() -> None:
    finding = _finding()
    restored = ResearchFinding.from_dict(finding.to_dict())
    assert restored == finding
    assert restored.evidence_refs == finding.evidence_refs


def test_round_trip_unsupported() -> None:
    finding = _finding(status="unsupported", evidence_refs=(), confidence=0.0)
    assert ResearchFinding.from_dict(finding.to_dict()) == finding


def test_from_dict_accepts_read_only_mapping() -> None:
    """核心层把 structured_payload 冻结后，嵌套结构是 MappingProxyType —— 必须能吃下。"""
    payload = MappingProxyType(dict(_finding().to_dict()))
    assert ResearchFinding.from_dict(payload) == _finding()


def test_from_dict_accepts_read_only_nested_ref() -> None:
    raw = _finding().to_dict()
    raw["evidence_refs"] = (MappingProxyType(dict(raw["evidence_refs"][0])),)
    assert ResearchFinding.from_dict(MappingProxyType(raw)) == _finding()


def test_from_dict_requires_mapping() -> None:
    with pytest.raises(ResearchValueError, match="Mapping"):
        ResearchFinding.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_from_dict_rejects_tampered_finding_id() -> None:
    payload = _finding().to_dict()
    payload["claim"] = "被改写"
    with pytest.raises(ResearchValueError, match="finding_id"):
        ResearchFinding.from_dict(payload)


def test_from_dict_rejects_non_mapping_ref() -> None:
    payload = _finding().to_dict()
    payload["evidence_refs"] = ["来源: 自由文本"]
    with pytest.raises(ResearchValueError, match="EvidenceRef|Mapping"):
        ResearchFinding.from_dict(payload)


def test_finding_is_frozen() -> None:
    finding = _finding()
    with pytest.raises((AttributeError, TypeError)):
        finding.claim = "改写"  # type: ignore[misc]

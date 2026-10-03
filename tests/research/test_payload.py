"""`structured_payload` 编解码单测（P1A-2）。

重点：schema 必须是 `research.findings.v1`，编码结果与传入顺序无关，
并且能直接解码**被核心冻结过**的 payload（MappingProxyType）。
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from agentboard.contracts import WorkerResult
from agentboard.research.config import FINDINGS_SCHEMA, FINDINGS_VERSION
from agentboard.research.errors import PayloadSchemaMismatchError, ResearchValueError
from agentboard.research.models import ResearchFinding
from agentboard.research.payload import decode_findings, encode_findings
from agentboard.workers.synthetic import SyntheticEvidenceFactory

REF = SyntheticEvidenceFactory.make("research", "research__pgvector", 0)


def _finding(dimension_id: str, *, candidate_id: str = "pgvector") -> ResearchFinding:
    if candidate_id == "pgvector":
        return ResearchFinding(
            candidate_id=candidate_id,
            task_id=f"research__{candidate_id}",
            agent_name="research",
            dimension_id=dimension_id,
            claim=f"{dimension_id} 的依据",
            value="low",
            status="supported",
            evidence_refs=(REF,),
            confidence=1.0,
        )
    return ResearchFinding(
        candidate_id=candidate_id,
        task_id=f"research__{candidate_id}",
        agent_name="research",
        dimension_id=dimension_id,
        claim=f"{dimension_id} 缺少依据",
        value="unknown",
        status="unsupported",
        evidence_refs=(),
        confidence=0.0,
    )


# ---- 编码 ----


def test_encode_uses_expected_schema() -> None:
    payload = encode_findings([_finding("query_latency")])
    assert payload["schema"] == FINDINGS_SCHEMA == "research.findings.v1"
    assert payload["version"] == FINDINGS_VERSION
    assert isinstance(payload["findings"], list)


def test_encode_is_order_insensitive() -> None:
    """同一份 findings 不论传入顺序如何，编码结果逐字节一致。"""
    a = _finding("query_latency")
    b = _finding("deployment_complexity")
    c = _finding("scale_ceiling", candidate_id="milvus_standalone")
    assert encode_findings([a, b, c]) == encode_findings([c, b, a])
    assert encode_findings([a, b, c]) == encode_findings([b, a, c])


def test_encode_rejects_non_finding() -> None:
    with pytest.raises(ResearchValueError, match="ResearchFinding"):
        encode_findings([{"dimension_id": "x"}])  # type: ignore[list-item]


def test_encode_empty_is_valid() -> None:
    payload = encode_findings([])
    assert payload["findings"] == []


# ---- 解码 ----


def test_round_trip_preserves_content_and_normalises_order() -> None:
    """编码会按 (candidate_id, dimension_id, finding_id) 排序，因此往返是"内容保留 + 顺序归一"。"""
    findings = (_finding("query_latency"), _finding("deployment_complexity"))
    decoded = decode_findings(encode_findings(findings))
    assert sorted(decoded, key=lambda f: f.finding_id) == sorted(findings, key=lambda f: f.finding_id)
    assert [f.dimension_id for f in decoded] == ["deployment_complexity", "query_latency"]


def test_round_trip_is_identity_for_already_sorted_findings() -> None:
    findings = (_finding("deployment_complexity"), _finding("query_latency"))
    assert decode_findings(encode_findings(findings)) == findings


def test_empty_payload_decodes_to_empty_tuple() -> None:
    assert decode_findings({}) == ()
    assert decode_findings(MappingProxyType({})) == ()


def test_decodes_frozen_payload() -> None:
    """核心会把 payload 冻结成 MappingProxyType，解码必须能吃下。"""
    findings = (_finding("query_latency"),)
    frozen = WorkerResult(
        task_id="research__pgvector",
        agent_name="research",
        facts=("f",),
        evidence=(REF,),
        risks=(),
        open_questions=(),
        confidence=1.0,
        structured_payload=encode_findings(findings),
    ).structured_payload
    assert isinstance(frozen, MappingProxyType)
    assert decode_findings(frozen) == findings


@pytest.mark.parametrize("schema", ["research.findings.v2", "findings", "research.findings", "", None, 1])
def test_wrong_schema_rejected(schema: object) -> None:
    with pytest.raises(PayloadSchemaMismatchError, match="schema"):
        decode_findings({"schema": schema, "findings": []})


@pytest.mark.parametrize("version", [2, 0, -1, "1", 1.0, True])
def test_wrong_version_rejected(version: object) -> None:
    with pytest.raises((PayloadSchemaMismatchError, ResearchValueError)):
        decode_findings({"schema": FINDINGS_SCHEMA, "version": version, "findings": []})


def test_missing_version_is_tolerated() -> None:
    """schema 已经固定了主版本，version 缺失时按当前版本理解。"""
    assert decode_findings({"schema": FINDINGS_SCHEMA, "findings": []}) == ()


def test_findings_must_be_sequence() -> None:
    with pytest.raises(ResearchValueError, match="findings"):
        decode_findings({"schema": FINDINGS_SCHEMA, "findings": "not-a-list"})


def test_findings_element_must_be_mapping() -> None:
    with pytest.raises(ResearchValueError, match="findings\\[0\\]"):
        decode_findings({"schema": FINDINGS_SCHEMA, "findings": ["raw text"]})


def test_payload_must_be_mapping() -> None:
    with pytest.raises(ResearchValueError, match="Mapping"):
        decode_findings(["schema", FINDINGS_SCHEMA])  # type: ignore[arg-type]


def test_decode_rejects_tampered_finding() -> None:
    payload = encode_findings([_finding("query_latency")])
    payload["findings"][0]["claim"] = "被改写"
    with pytest.raises(ResearchValueError, match="finding_id"):
        decode_findings(payload)


def test_encoded_payload_passes_core_validation() -> None:
    """领域层产出的 payload 必须能被核心契约接受（双方对 schema 形态的判断一致）。"""
    WorkerResult(
        task_id="research__pgvector",
        agent_name="research",
        facts=(),
        evidence=(),
        risks=(),
        open_questions=(),
        confidence=0.5,
        structured_payload=encode_findings([_finding("query_latency")]),
    )

"""`WorkerResult.structured_payload` 单测（P1A-1）。

核心只校验 `schema` / `version` 并递归冻结，**不读取任何业务键**；
业务语义属于领域层（`agentboard.research`）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from agentboard.contracts import WorkerResult
from agentboard.workers.synthetic import SyntheticEvidenceFactory

SCHEMA = "research.findings.v1"


def _result(**overrides: object) -> WorkerResult:
    base: dict[str, object] = {
        "task_id": "t1",
        "agent_name": "research",
        "facts": ("f",),
        "evidence": SyntheticEvidenceFactory.for_task("research", "t1", count=1),
        "risks": (),
        "open_questions": (),
        "confidence": 0.9,
    }
    base.update(overrides)
    return WorkerResult(**base)  # type: ignore[arg-type]


# ---- 1. 默认与最小合法形态 ----


def test_default_payload_is_empty_and_frozen():
    r = _result()
    assert isinstance(r.structured_payload, Mapping)
    assert dict(r.structured_payload) == {}
    assert r.to_dict()["structured_payload"] == {}


def test_payload_with_schema_only_is_accepted():
    assert _result(structured_payload={"schema": SCHEMA}).structured_payload["schema"] == SCHEMA


def test_payload_with_schema_and_version_is_accepted():
    r = _result(structured_payload={"schema": SCHEMA, "version": 1})
    assert r.structured_payload["version"] == 1


# ---- 2. schema 校验 ----


def test_non_empty_payload_requires_schema():
    with pytest.raises(ValueError, match="schema"):
        _result(structured_payload={"findings": []})


@pytest.mark.parametrize(
    "schema",
    [
        "research.findings@1",
        "research.findings",
        "research",
        "Research.findings.v1",
        "research..v1",
        "research.findings.v0",
        "research.findings.vX",
        "",
        " research.findings.v1",
    ],
)
def test_invalid_schema_is_rejected(schema: str):
    with pytest.raises(ValueError, match="schema"):
        _result(structured_payload={"schema": schema})


@pytest.mark.parametrize("schema", ["research.findings.v1", "agentboard.baseline.v2", "a.b.c.v10"])
def test_valid_schema_is_accepted(schema: str):
    assert _result(structured_payload={"schema": schema}).structured_payload["schema"] == schema


def test_schema_must_be_string():
    with pytest.raises(ValueError, match="schema"):
        _result(structured_payload={"schema": 1})


# ---- 3. version 校验（可选，但存在即必须是 >=1 的整数） ----


@pytest.mark.parametrize("version", [0, -1, "1", 1.0, True, False, None])
def test_invalid_version_is_rejected(version: object):
    with pytest.raises(ValueError, match="version"):
        _result(structured_payload={"schema": SCHEMA, "version": version})


@pytest.mark.parametrize("version", [1, 2, 99])
def test_valid_version_is_accepted(version: int):
    assert _result(structured_payload={"schema": SCHEMA, "version": version}).structured_payload["version"] == version


# ---- 4. payload 必须是 Mapping ----


@pytest.mark.parametrize("payload", [[("schema", SCHEMA)], "schema", 42, {"schema": SCHEMA}.keys()])
def test_payload_must_be_mapping(payload: object):
    with pytest.raises(ValueError, match="structured_payload"):
        _result(structured_payload=payload)


# ---- 5. 核心不解释业务键 ----


def test_core_accepts_arbitrary_business_keys_without_reading_them():
    """核心对研究领域的业务键零知识：只要 schema 合法，随便放什么键都收下。"""
    business = {
        "schema": SCHEMA,
        "version": 1,
        "findings": [{"finding_id": "rf_0000000000000001", "dimension_id": "cost"}],
        "totally_unknown_key": {"nested": ["nope"]},
    }
    r = _result(structured_payload=business)
    assert r.structured_payload["schema"] == SCHEMA
    assert "totally_unknown_key" in r.structured_payload


# ---- 6. 递归不可变 ----


def test_payload_is_recursively_frozen():
    payload = {"schema": SCHEMA, "items": [1, 2, 3], "nested": {"a": "b"}}
    r = _result(structured_payload=payload)
    payload["items"].append(4)
    payload["nested"]["a"] = "c"

    assert r.structured_payload["items"] == (1, 2, 3)
    assert r.structured_payload["nested"]["a"] == "b"


def test_payload_rejects_internal_writes():
    r = _result(structured_payload={"schema": SCHEMA, "nested": {"a": "b"}, "items": [1]})
    with pytest.raises(TypeError):
        r.structured_payload["leak"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        r.structured_payload["nested"]["a"] = "z"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        r.structured_payload["items"].append(1)  # type: ignore[union-attr]


# ---- 7. 序列化 ----


def test_round_trip_preserves_payload():
    r = _result(structured_payload={"schema": SCHEMA, "version": 1, "findings": ["a"]})
    back = WorkerResult.from_dict(r.to_dict())
    assert back == r
    assert back.structured_payload["findings"] == ("a",)


def test_to_dict_payload_is_json_serializable():
    r = _result(structured_payload={"schema": SCHEMA, "nested": {"k": [1, 2]}, "flag": True})
    text = json.dumps(r.to_dict(), ensure_ascii=False, sort_keys=True)
    assert SCHEMA in text
    assert json.loads(text)["structured_payload"]["nested"] == {"k": [1, 2]}


def test_from_dict_rejects_non_dict_payload():
    payload = _result().to_dict()
    payload["structured_payload"] = ["schema", SCHEMA]
    with pytest.raises(ValueError, match="structured_payload"):
        WorkerResult.from_dict(payload)


def test_from_dict_validates_payload_schema():
    payload = _result().to_dict()
    payload["structured_payload"] = {"schema": "bad@1"}
    with pytest.raises(ValueError, match="schema"):
        WorkerResult.from_dict(payload)

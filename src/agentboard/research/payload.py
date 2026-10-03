"""`WorkerResult.structured_payload` 的编解码（P1A-2）。

这是**领域层读取该字段的唯一入口**：核心契约层只校验 `schema` / `version` 的形态，
从不解释业务键；把 `findings` 的语义解读集中在这里，边界清晰、便于测试。

schema 固定为 `research.findings.v1`（含点号，全项目不出现 `@`）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .config import FINDINGS_SCHEMA, FINDINGS_VERSION
from .errors import PayloadSchemaMismatchError, ResearchValueError
from .models import ResearchFinding


def encode_findings(findings: Sequence[ResearchFinding]) -> dict[str, Any]:
    """把 findings 编成 structured_payload。

    输出按 `(candidate_id, dimension_id, finding_id)` 排序，因此同一份 findings
    不论传入顺序如何，序列化结果逐字节一致（重复运行一致性的前提）。
    """
    items = list(findings)
    for finding in items:
        if not isinstance(finding, ResearchFinding):
            raise ResearchValueError(
                f"encode_findings 只接受 ResearchFinding，实际为 {type(finding).__name__}"
            )
    ordered = sorted(
        items, key=lambda f: (f.candidate_id, f.dimension_id, f.finding_id)
    )
    return {
        "schema": FINDINGS_SCHEMA,
        "version": FINDINGS_VERSION,
        "findings": [finding.to_dict() for finding in ordered],
    }


def decode_findings(payload: Mapping[str, Any]) -> tuple[ResearchFinding, ...]:
    """从 structured_payload 还原 findings。

    空 payload（`{}`）表示"本次没有结构化产出"，返回空元组；
    非空 payload 必须带正确的 `schema`，否则拒绝 —— 不做"尽力解析"。
    """
    if not isinstance(payload, Mapping):
        raise ResearchValueError(
            f"structured_payload 必须是 Mapping，实际为 {type(payload).__name__}"
        )
    if not payload:
        return ()

    schema = payload.get("schema")
    if schema != FINDINGS_SCHEMA:
        raise PayloadSchemaMismatchError(
            f"structured_payload.schema 期望 {FINDINGS_SCHEMA!r}，实际为 {schema!r}"
        )

    if "version" in payload:
        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ResearchValueError(
                f"structured_payload.version 必须是整数（禁止 bool），实际为 {version!r}"
            )
        if version != FINDINGS_VERSION:
            raise PayloadSchemaMismatchError(
                f"structured_payload.version 期望 {FINDINGS_VERSION}，实际为 {version!r}"
            )

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, (list, tuple)):
        raise ResearchValueError("structured_payload.findings 必须是列表")

    decoded: list[ResearchFinding] = []
    for index, item in enumerate(raw_findings):
        if not isinstance(item, Mapping):
            raise ResearchValueError(
                f"structured_payload.findings[{index}] 必须是 Mapping，实际为 {type(item).__name__}"
            )
        decoded.append(ResearchFinding.from_dict(item))
    return tuple(decoded)

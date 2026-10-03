"""synthesizer 角色：只消费验证通过的结构化结果（P5）。

消费规则（fail-closed，不存在"尽量合成"）：

- 上游必须是 verifier 的 verdict 载荷（`multi-agent.verdict.v1`，闭集校验）；
- 每个 verdict 必须是 `passed`——任何一个 `rejected` ⇒ 抛
  `VerifierRejectedError` ⇒ 任务板失败语义接管（重试耗尽 ⇒ blocked）。
  不可信结论由此**不可能**进入最终报告；
- 没有任何上游 ⇒ `SynthesisError`（合成器不猜测、不编造）。

最终产物：`multi-agent.final.v1` 载荷（final_report_id 为全部 verdict 内容的
规范 JSON 摘要；该 id 进入编排 run_id 与审计记录）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..contracts import WorkerResult, WorkerTaskInput
from ..util import thaw_payload
from ..workers.synthetic import SyntheticEvidenceFactory
from .errors import SynthesisError, VerifierRejectedError
from .verifier import VERDICT_PASSED, validate_verdict_payload

FINAL_PAYLOAD_SCHEMA = "multi_agent.final.v1"
FINAL_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"final_report_id", "schema", "synthesis", "task_refs", "verdicts"}
)


class SynthesizerWorker:
    """合成器（synthesizer 角色）。"""

    def __init__(self, *, agent_name: str = "synthesizer") -> None:
        self.agent_name = agent_name
        self.received_inputs: list[WorkerTaskInput] = []

    @property
    def identity(self) -> str:
        return f"synthesizer.v1:{self.agent_name}"

    async def run(self, task_input: WorkerTaskInput) -> WorkerResult:
        self.received_inputs.append(task_input)
        if not task_input.upstream_results:
            raise SynthesisError("synthesizer 没有任何上游验证结论（fail-closed）")
        verdicts: dict[str, str] = {}
        report_ids: dict[str, str] = {}
        for upstream_id in sorted(task_input.upstream_results):
            payload = validate_verdict_payload(
                thaw_payload(task_input.upstream_results[upstream_id].structured_payload),
                label=f"上游 {upstream_id}",
            )
            verdicts[upstream_id] = str(payload["verdict"])
            report_ids[upstream_id] = str(payload["verified_report_id"])
            if payload["verdict"] != VERDICT_PASSED:
                raise VerifierRejectedError(
                    f"上游 {upstream_id} verdict=rejected（reasons="
                    f"{list(payload['reasons'])}）——不可信结论不得进入最终报告"
                )
        final_report_id = expected_final_report_id(report_ids=report_ids, verdicts=verdicts)
        synthesis = (
            "最终报告（仅汇总验证通过的结论）："
            + "; ".join(
                f"{task_id} → {report_ids[task_id]} ({verdicts[task_id]})"
                for task_id in sorted(verdicts)
            )
        )
        payload = {
            "final_report_id": final_report_id,
            "schema": FINAL_PAYLOAD_SCHEMA,
            "synthesis": synthesis,
            "task_refs": sorted(task_input.upstream_results),
            "verdicts": verdicts,
        }
        return WorkerResult(
            task_id=task_input.task_id,
            agent_name=self.agent_name,
            facts=(f"final_report_id={final_report_id}", synthesis),
            evidence=SyntheticEvidenceFactory.for_task(self.agent_name, task_input.task_id, count=1),
            risks=(),
            open_questions=(),
            confidence=0.9,
            summary=f"final report {final_report_id}",
            session_id=f"synthesize-{task_input.task_id}",
            structured_payload=payload,
        )


def expected_final_report_id(*, report_ids: dict[str, str], verdicts: dict[str, str]) -> str:
    """由真实 verifier 上游派生最终 id；生成端与写板门禁共享唯一实现。"""
    content = {"report_ids": report_ids, "verdicts": verdicts}
    return "mafr_" + hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()[:16]


def validate_final_payload(payload: Any, *, label: str) -> dict[str, Any]:
    """最终载荷闭集校验（orchestrator 收尾用）。"""
    if not isinstance(payload, dict) or payload.get("schema") != FINAL_PAYLOAD_SCHEMA:
        raise SynthesisError(
            f"{label}: 最终载荷缺失或 schema 不是 {FINAL_PAYLOAD_SCHEMA}"
        )
    keys = frozenset(payload)
    unknown = sorted(keys - FINAL_PAYLOAD_KEYS)
    missing = sorted(FINAL_PAYLOAD_KEYS - keys)
    if unknown or missing:
        raise SynthesisError(
            f"{label}: 最终载荷字段边界违例（未知 {unknown} / 缺失 {missing}）"
        )
    return payload


def _canonical(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "FINAL_PAYLOAD_KEYS",
    "FINAL_PAYLOAD_SCHEMA",
    "SynthesizerWorker",
    "validate_final_payload",
]

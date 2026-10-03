"""researcher 角色的 worker 适配（P5）。

两个实现，同一 `WorkerAdapter` 契约：

- **`RuntimeResearchWorker`**：接入 P3/P4 运行时（`LiveResearchRunner`），
  默认离线：`ScriptedSearchProvider` + `GeneratedDimensionDecider`
  （P4 受控生成决策器 + `ScriptedDecisionTransport`）。研究任务卡的
  `context` 必须含 `candidate_ids:` 声明（roles.py 解析），worker 只见
  自己的任务包，不读板外状态。运行失败（任何 outcome != completed）
  ⇒ 抛 RuntimeError ⇒ 任务板失败语义接管（重试/blocked），**绝不静默降级**。
- **`ScriptedReportWorker`**：编排语义测试/评测用的快速确定性替身——
  产出与 runtime 同一 schema（`live-research.report.v1`）的合法载荷，
  可注入失败次数驱动重试状态机。它**不代表真实研究能力**（README 如实标注）。

证据边界：两个实现的 `WorkerResult.evidence` 都经 `SyntheticEvidenceFactory`
（全仓唯一 synthetic 构造点，静态守卫钉死）；载荷里的研究报告以
`report_json` + `content_digest` 形式携带，供 verifier 独立重验。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from ..contracts import ProjectBrief, WorkerResult, WorkerTaskInput
from ..research.assemble import build_recommendations
from ..research.config import SCALE_ORDINAL
from ..research.fixtures import FixtureResearchWorker
from ..research.integrity import IntegrityReport, verify_integrity
from ..research.models import (
    ComparisonDimension,
    SelectionReport,
    derive_coverage,
)
from ..research.scoring import score_all
from ..runtime.mock_provider import ScriptedSearchProvider, scripted_document
from ..runtime.runner import LiveResearchConfig, LiveResearchRunner
from ..workers.synthetic import SyntheticEvidenceFactory
from .errors import OrchestrationError
from .roles import declared_candidates

#: 研究结果载荷 schema（verifier 按此闭集校验）。
REPORT_PAYLOAD_SCHEMA = "live_research.report.v1"
#: `source_audit` 为可选键（离线脚本模式没有真实来源；live 模式必带，verifier 复核）。
REPORT_PAYLOAD_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "schema",
        "candidate_ids",
        "content_digest",
        "failed_codes",
        "integrity_passed",
        "report_id",
        "report_json",
        "task_refs",
    }
)
REPORT_PAYLOAD_KEYS: frozenset[str] = REPORT_PAYLOAD_REQUIRED_KEYS | {
    "source_audit",
    "run_audit",
}
PRIMARY_DIMENSION = "deployment_complexity"
CAPTURED_AT = "2026-09-26"
CANDIDATES_NOTE = "研究范围以 candidate_ids 声明为准；不得越界。"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_payload_keys(payload: Any, *, label: str) -> dict[str, Any]:
    """载荷闭集校验（schema + 键集合；未知键一律拒绝，source_audit 可选）。"""
    if not isinstance(payload, dict) or payload.get("schema") != REPORT_PAYLOAD_SCHEMA:
        raise OrchestrationError(
            f"{label}: structured_payload 缺失或 schema 不是 {REPORT_PAYLOAD_SCHEMA}"
        )
    keys = frozenset(payload)
    unknown = sorted(keys - REPORT_PAYLOAD_KEYS)
    missing = sorted(REPORT_PAYLOAD_REQUIRED_KEYS - keys)
    if unknown or missing:
        raise OrchestrationError(
            f"{label}: 载荷字段边界违例（未知 {unknown} / 缺失 {missing}）"
        )
    return payload


class RuntimeResearchWorker:
    """接入 P3/P4 运行时的 researcher worker（默认离线全链路）。"""

    def __init__(
        self,
        *,
        agent_name: str = "researcher",
        snapshot_root: Any,
        run_root: Any,
        dimensions_json: Any,
        decider: Any | None = None,
        budget: Any | None = None,
        worker_identity: str | None = None,
        provider_factory: Any | None = None,
    ) -> None:
        self.agent_name = agent_name
        self._snapshot_root = snapshot_root
        self._run_root = run_root
        self._dimensions_json = dimensions_json
        self._decider = decider
        self._budget = budget
        self.worker_identity = worker_identity or "runtime-research.v1"
        # P6 live 接线：provider_factory(candidates) → 真实 Provider
        # （如 PinnedSourceProvider，按显式来源计划取固定 commit 原文）；
        # 缺省 None ⇒ 离线 ScriptedSearchProvider（确定性，且明确标注为脚本）。
        self._provider_factory = provider_factory

    @property
    def identity(self) -> str:
        return self.worker_identity

    async def run(self, task_input: WorkerTaskInput) -> WorkerResult:
        candidates = declared_candidates(task_input.context)
        # 默认决策器：P4 受控生成决策（GeneratedDimensionDecider）+ 离线脚本
        # transport（闭集协议、确定性）。可用构造参数注入自定义决策器。
        registry = _load_registry(self._dimensions_json)
        decider = self._decider
        if decider is None:
            decider = _generated_decider_for(registry, candidates)
        if self._provider_factory is not None:
            provider = self._provider_factory(candidates)
        else:
            documents: dict[str, list[Any]] = {}
            for candidate_id in candidates:
                terms = f"{candidate_id} {PRIMARY_DIMENSION}"
                documents[terms] = [
                    scripted_document(
                        terms=terms,
                        candidate_id=candidate_id,
                        index=0,
                        body=f"{candidate_id} 的官方说明：{PRIMARY_DIMENSION} 原文材料。",
                        captured_at=CAPTURED_AT,
                    )
                ]
            provider = ScriptedSearchProvider(documents=documents, captured_at=CAPTURED_AT)
        runner = LiveResearchRunner(
            brief=_brief_from(task_input),
            candidate_ids=candidates,
            registry=_load_registry(self._dimensions_json),
            provider=provider,
            budget=self._budget
            or _default_budget(),
            decider=decider,
            config=LiveResearchConfig(
                captured_at=CAPTURED_AT,
                dimensions_json=self._dimensions_json,
                snapshot_root=self._snapshot_root / task_input.task_id,
                run_dir=self._run_root / task_input.task_id,
                gap_recheck=False,
            ),
        )
        # runner.run() 是同步管线（内部有自己的 asyncio 桥接）；编排器运行在
        # 事件循环内，必须放到工作线程执行，否则嵌套 asyncio.run 直接失败。
        result = await asyncio.to_thread(runner.run)
        if result.outcome != "completed" or result.report is None:
            raise RuntimeError(
                f"research run failed:{result.outcome}:{result.failure_reason}"
            )
        report_json = result.report_json or _canonical_json(result.report.to_dict())
        payload: dict[str, Any] = {
            "schema": REPORT_PAYLOAD_SCHEMA,
            "candidate_ids": list(candidates),
            "content_digest": hashlib.sha256(report_json.encode("utf-8")).hexdigest(),
            "failed_codes": [],
            "integrity_passed": True,
            "report_id": result.report.report_id,
            "report_json": report_json,
            "task_refs": [],
        }
        # P6 live 来源审计：真实 Provider（如 PinnedSourceProvider）暴露
        # source_audit() ⇒ 逐件来源（URL/commit/sha256）进载荷，verifier 复核。
        audit = getattr(provider, "source_audit", None)
        if callable(audit):
            entries = audit()
            if entries:
                payload["source_audit"] = entries
        # P6 运行审计：预算消耗 / 网络（HTTP）调用次数 / 模型调用次数 / run_id。
        run_audit: dict[str, Any] = {"run_id": result.run_id}
        budget_usage = getattr(result, "budget_usage", None)
        if isinstance(budget_usage, dict):
            run_audit["budget_usage"] = dict(budget_usage)
        http_calls = getattr(provider, "http_calls", None)
        if http_calls is not None:
            run_audit["provider_http_calls"] = int(http_calls)
        decider_usage = getattr(decider, "usage", None)
        if callable(decider_usage):
            run_audit["model_calls"] = dict(decider_usage())
        payload["run_audit"] = run_audit
        facts = tuple(
            f"{rec.rank}. {rec.candidate_id} score={rec.score:.4f}"
            for rec in result.report.recommendations
        )
        return WorkerResult(
            task_id=task_input.task_id,
            agent_name=self.agent_name,
            facts=facts,
            evidence=SyntheticEvidenceFactory.for_task(self.agent_name, task_input.task_id, count=2),
            risks=("live 材料 authored_summary 信任层，不可升格（待人工复核）",),
            open_questions=(),
            confidence=0.8,
            summary=f"live research report {result.report.report_id} integrity=passed",
            session_id=result.run_id,
            structured_payload=payload,
        )


class ScriptedReportWorker:
    """快速确定性研究替身（编排语义测试/评测用；产出同一 schema 的合法载荷）。"""

    def __init__(
        self,
        *,
        agent_name: str = "researcher",
        fail_times: int = 0,
        dimensions: tuple[str, ...] = (PRIMARY_DIMENSION,),
    ) -> None:
        self.agent_name = agent_name
        self.fail_times = fail_times
        self.dimensions = tuple(dimensions)
        self._calls = 0

    @property
    def identity(self) -> str:
        return f"scripted-report.v1:{self.agent_name}:fail_times={self.fail_times}"

    async def run(self, task_input: WorkerTaskInput) -> WorkerResult:
        self._calls += 1
        if self._calls <= self.fail_times:
            raise RuntimeError(
                f"scripted report worker {self.agent_name} 第 {self._calls} 次执行失败"
            )
        candidates = declared_candidates(task_input.context)
        dimension = ComparisonDimension(
            dimension_id=PRIMARY_DIMENSION,
            name="Deployment complexity",
            description="Scripted orchestration fixture dimension.",
            weight=1.0,
            scale_kind=SCALE_ORDINAL,
            scale_labels=("low", "high"),
            utility_map={"low": 1.0, "high": 0.0},
        )
        # 编排替身复用本地回归快照，不能用 synthetic 证据冒充通过 I4 的研究报告。
        # 这些人工摘要仅用于离线回放，不表示研究级来源或真实联网能力。
        fixture_worker = FixtureResearchWorker.from_fixture_root(
            Path(__file__).resolve().parents[3] / "fixtures" / "research"
        )
        findings_list = []
        for candidate_id in candidates:
            fixture_input = WorkerTaskInput(
                task_id=task_input.task_id, title=task_input.title,
                assigned_agent=self.agent_name, goal=task_input.goal,
                context=f"candidate_id: {candidate_id}", upstream_results={},
            )
            findings_list.extend(
                finding for finding in await fixture_worker.investigate(fixture_input)
                if finding.dimension_id == PRIMARY_DIMENSION
            )
        findings = tuple(findings_list)
        scores = score_all(candidate_ids=candidates, dimensions=(dimension,), findings=findings)
        recommendations = build_recommendations(scores=scores, findings=findings, risks=())
        coverage = derive_coverage(
            candidate_ids=candidates, dimension_ids=(PRIMARY_DIMENSION,), findings=findings
        )
        report_model = SelectionReport(
            brief=ProjectBrief(text=task_input.goal, source=f"orchestrator/{task_input.task_id}"),
            candidate_ids=candidates, dimensions=(dimension,), findings=findings, risks=(),
            recommendations=recommendations, coverage=coverage, integrity=IntegrityReport(),
        )
        from dataclasses import replace

        report_model = replace(report_model, integrity=verify_integrity(report_model))
        if not report_model.integrity.passed:
            raise OrchestrationError(f"脚本报告未通过完整性校验：{report_model.integrity.failed_codes}")
        report = report_model.to_dict()
        report_json = _canonical_json(report)
        payload = {
            "schema": REPORT_PAYLOAD_SCHEMA,
            "candidate_ids": list(candidates),
            "content_digest": hashlib.sha256(report_json.encode("utf-8")).hexdigest(),
            "failed_codes": [],
            "integrity_passed": True,
            "report_id": report_model.report_id,
            "report_json": report_json,
            "task_refs": [],
        }
        return WorkerResult(
            task_id=task_input.task_id,
            agent_name=self.agent_name,
            facts=(f"[{self.agent_name}] 报告 {payload['report_id']} 完整性通过",),
            evidence=SyntheticEvidenceFactory.for_task(self.agent_name, task_input.task_id, count=1),
            risks=(),
            open_questions=(),
            confidence=0.85,
            summary=f"scripted report {payload['report_id']}",
            session_id=f"scripted-{task_input.task_id}-{self._calls}",
            structured_payload=payload,
        )


def _brief_from(task_input: WorkerTaskInput):
    from ..contracts import ProjectBrief

    return ProjectBrief(text=task_input.goal, source=f"orchestrator/{task_input.task_id}")


def _default_budget():
    from ..runtime.budget import ResearchBudget

    return ResearchBudget(
        max_steps=64,
        max_provider_requests=64,
        max_total_content_chars=1_000_000,
        max_retries_per_step=1,
    )


def _load_registry(dimensions_json: Any):
    from ..research.dimensions import load_dimension_registry

    return load_dimension_registry(dimensions_json)


def _generated_decider_for(registry: Any, candidates: tuple[str, ...]):
    """构造默认的 P4 受控生成决策器（离线脚本 transport；按候选范围确定性脚本）。"""
    from ..providers.decision_transport import ScriptedDecisionTransport
    from ..runtime.decider_generated import DecisionGenConfig, GeneratedDimensionDecider

    scale_labels = {
        dimension.dimension_id: tuple(dimension.scale_labels)
        for dimension in registry.dimensions
    }
    dimension_ids = sorted(scale_labels)
    responses: dict[tuple[str, str], list[str]] = {}
    for candidate_id in candidates:
        for dimension_id in dimension_ids:
            if dimension_id == PRIMARY_DIMENSION:
                responses[(candidate_id, dimension_id)] = [
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "dimension_id": dimension_id,
                            "value": "low",
                            "claim": f"脚本决策：{candidate_id} 单步启用 ⇒ low。",
                            "section_id": f"s_{candidate_id}_00",
                            "confidence": 0.8,
                        },
                        ensure_ascii=False,
                    )
                ]
            else:
                responses[(candidate_id, dimension_id)] = [
                    json.dumps(
                        {"insufficient_evidence": True, "reason": "本维度材料不足。"},
                        ensure_ascii=False,
                    )
                ]
    return GeneratedDimensionDecider(
        transport=ScriptedDecisionTransport(responses=responses),
        config=DecisionGenConfig(max_retries=1),
        scale_labels=scale_labels,
    )


__all__ = [
    "CANDIDATES_NOTE",
    "CAPTURED_AT",
    "PRIMARY_DIMENSION",
    "REPORT_PAYLOAD_KEYS",
    "REPORT_PAYLOAD_SCHEMA",
    "RuntimeResearchWorker",
    "ScriptedReportWorker",
    "_canonical_json",
    "_check_payload_keys",
    "_generated_decider_for",
]

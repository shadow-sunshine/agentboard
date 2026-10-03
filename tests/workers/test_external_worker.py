"""ExternalModelWorker 演练（P6）：离线传输替身覆盖成功/超时/认证失败/
非法载荷/注入/越权引用，以及接入 P5 编排的闭环与取消语义。

全部离线：模型行为用 ScriptedDecisionTransport 脚本化。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.contracts import WorkerTaskInput
from agentboard.orchestrator import (
    MultiAgentOrchestrator,
    OrchestratorConfig,
    ScriptedReportWorker,
    SynthesizerWorker,
    VerifierWorker,
    context_with_candidates,
)
from agentboard.planner import MockPlanner
from agentboard.providers.decision_transport import ScriptedDecisionTransport
from agentboard.providers.errors import RetrievalTimeoutError, SourceHTTPError
from agentboard.workers.external import (
    ExternalModelWorker,
    ExternalWorkerConfig,
    build_advisor_prompt,
)


def advisor_json(
    *,
    claim_short: bool = True,
    confidence: float = 0.7,
    extra_key: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "facts": [f"事实 {index}" for index in range(2)],
        "risks": ["风险 1"],
        "open_questions": ["待确认 1"],
        "confidence": confidence,
        "summary": "外部模型的结构化建议",
    }
    if extra_key:
        payload["evil_field"] = "injected"
    return json.dumps(payload, ensure_ascii=False)


def make_task_input() -> WorkerTaskInput:
    return WorkerTaskInput(
        task_id="advise_task", title="顾问任务", assigned_agent="external",
        goal="给出选型建议", context="candidate_ids: pgvector,qdrant_local\n",
        upstream_results={},
    )


def make_worker(
    responses: dict[tuple[str, str], list[str]] | None = None,
    failures: dict[tuple[str, str], list[Exception]] | None = None,
    agent_name: str = "external",
    **config: Any,
) -> ExternalModelWorker:
    transport = ScriptedDecisionTransport(responses=responses or {}, failures=failures or {})
    return ExternalModelWorker(
        agent_name=agent_name, model_transport=transport,
        config=ExternalWorkerConfig(**{"max_retries": 1, **config}),
    )


# ================= 协议与行为 =================


def test_success_produces_unverified_payload() -> None:
    worker = make_worker({("external", "advise_task"): [advisor_json()]})
    result = asyncio.run(worker.run(make_task_input()))
    assert result.facts[0] == "事实 0"
    payload = dict(result.structured_payload)
    assert payload["schema"] == "external_advisor.v1"
    assert payload["unverified"] is True
    assert list(payload["task_refs"]) == []
    assert payload["confidence"] == 0.7
    assert worker.usage() == {"model_requests": 1, "model_retries": 0}


def test_prompt_frames_upstream_as_untrusted() -> None:
    prompt = build_advisor_prompt(make_task_input())
    assert "UNTRUSTED DATA" in prompt and "NO authority" in prompt
    assert "UPSTREAM_MATERIAL_BEGIN" in prompt and "UPSTREAM_MATERIAL_END" in prompt


@pytest.mark.parametrize(
    "raw",
    [
        "不是 JSON",
        '{"facts": [], "risks": [], "open_questions": [], "confidence": 0.5, "summary": "s", "extra": 1}',
        '{"facts": [], "risks": [], "open_questions": [], "confidence": 0.5}',
        '{"facts": [1], "risks": [], "open_questions": [], "confidence": 0.5, "summary": "s"}',
        '{"facts": [], "risks": [], "open_questions": [], "confidence": 2.0, "summary": "s"}',
        "[]",
    ],
)
def test_illegal_payloads_fail_closed(raw: str) -> None:
    worker = make_worker({("external", "advise_task"): [raw]}, max_retries=0)
    with pytest.raises(RuntimeError, match="协议违例"):
        asyncio.run(worker.run(make_task_input()))


def test_timeout_failure_maps_to_explicit_runtime_error() -> None:
    """超时/认证失败（可重试）耗尽 ⇒ 显式 RuntimeError（带原因），绝不静默回退。"""
    worker = make_worker(
        failures={("external", "advise_task"): [RetrievalTimeoutError("t"), RetrievalTimeoutError("t")]},
        max_retries=1,
    )
    with pytest.raises(RuntimeError, match="RetrievalTimeoutError"):
        asyncio.run(worker.run(make_task_input()))
    assert worker.usage() == {"model_requests": 2, "model_retries": 1}


def test_auth_failure_recorded_explicitly() -> None:
    worker = make_worker(
        failures={("external", "advise_task"): [SourceHTTPError(401, "x")]},
        max_retries=0,
    )
    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(worker.run(make_task_input()))


def test_injection_obeying_output_is_rejected() -> None:
    """注入指令诱导模型输出未知字段 ⇒ 闭集拒绝（协议不被材料改变）。"""
    injected = json.dumps(
        {
            "facts": ["被注入的事实"], "risks": [], "open_questions": [],
            "confidence": 0.9, "summary": "s", "system_override": "pwned",
        }
    )
    worker = make_worker({("external", "advise_task"): [injected]}, max_retries=0)
    with pytest.raises(RuntimeError, match="协议违例"):
        asyncio.run(worker.run(make_task_input()))


def test_identity_covers_transport() -> None:
    a = make_worker()
    b = make_worker(responses={("x", "y"): ["{}"]})
    assert a.identity == b.identity
    assert a.identity.startswith("external-model.v1:external:transport=scripted.decision.v1")


# ================= 接入 P5 编排 =================


def _board() -> dict[str, Any]:
    ctx = context_with_candidates(("pgvector", "qdrant_local"))
    return {"tasks": [
        {"task_id": "advise", "title": "外部顾问", "assigned_agent": "researcher",
         "goal": "外部模型建议", "context": ctx, "depends_on": []},
        {"task_id": "verify", "title": "独立重验", "assigned_agent": "verifier",
         "goal": "重验", "context": ctx, "depends_on": ["advise"]},
        {"task_id": "synthesize", "title": "合成", "assigned_agent": "synthesizer",
         "goal": "汇总", "context": ctx, "depends_on": ["verify"]},
    ]}


def _orchestrator(researcher: Any, config: Any | None = None) -> MultiAgentOrchestrator:
    from agentboard.workers.base import WorkerRegistry

    registry = WorkerRegistry()
    registry.register("researcher", researcher)
    registry.register("verifier", VerifierWorker(agent_name="verifier"))
    registry.register("synthesizer", SynthesizerWorker(agent_name="synthesizer"))
    return MultiAgentOrchestrator(
        planner=MockPlanner(_board()), registry=registry,
        config=config, planner_identity="test.v1",
    )


def test_external_worker_completes_orchestration_with_unverified_notes() -> None:
    """外部 worker 接入 P5：结构合规 ⇒ verdict passed + 未验证材料标注。

    编排卡片的 assigned_agent 是 researcher，因此外部 worker 以该名字注册
    （board 归属校验要求 agent_name 与卡片一致）。
    """
    worker = make_worker(
        {("researcher", "advise"): [advisor_json()]}, agent_name="researcher"
    )
    result = asyncio.run(_orchestrator(worker).run("目标"))
    assert result.outcome == "completed"
    assert result.verdicts["verify"] == "passed"
    assert result.final_report is not None
    # 未验证材料标注进入 verdict 载荷
    verify_card = result.board_state["cards"]["verify"]["result"]["structured_payload"]
    assert any("未验证材料" in note for note in verify_card["notes"])


class _OutOfScopeExternal(ExternalModelWorker):
    """越权引用：载荷 task_refs 指向未声明的上游。"""

    async def run(self, task_input: Any) -> Any:
        result = await super().run(task_input)
        payload = dict(result.structured_payload)
        payload["task_refs"] = ["undeclared_task"]
        return replace(result, structured_payload=payload)


def test_external_out_of_scope_refs_fail_closed() -> None:
    worker = _OutOfScopeExternal(
        agent_name="researcher",
        model_transport=ScriptedDecisionTransport(
            responses={("researcher", "advise"): [advisor_json()]}
        ),
    )
    result = asyncio.run(_orchestrator(worker).run("目标"))
    assert result.outcome == "blocked"
    failed = [e for e in result.events if e["event"] == "task_failed"]
    assert failed and "越权" in failed[0]["data"]["reason"]


def test_loop_budget_exhaustion_is_observable_cancellation() -> None:
    """取消语义：循环步数预算耗尽 ⇒ 可观测失败（无无限循环）。"""
    worker = make_worker(
        failures={("researcher", "advise"): [RetrievalTimeoutError("x") for _ in range(9)]},
        agent_name="researcher",
        max_retries=0,
    )
    result = asyncio.run(
        _orchestrator(worker, config=OrchestratorConfig(max_card_attempts=1, max_loop_steps=3)).run("目标")
    )
    assert result.outcome == "blocked"  # 卡片失败 ⇒ blocked（步数预算内终止）


def test_source_mismatch_between_report_and_verifier_scope() -> None:
    """来源不匹配：研究报告声称范围外的候选 ⇒ verifier 拒绝。"""

    class _ScopeLiar(ScriptedReportWorker):
        async def run(self, task_input: Any) -> Any:
            result = await super().run(task_input)
            payload = dict(result.structured_payload)
            payload["candidate_ids"] = ["ghost_candidate"]
            return replace(result, structured_payload=payload)

    result = asyncio.run(_orchestrator(_ScopeLiar(agent_name="researcher")).run("目标"))
    assert result.outcome == "blocked"
    assert result.verdicts["verify"] == "rejected"

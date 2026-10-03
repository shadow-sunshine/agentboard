"""多 Agent 编排器测试（P5）：角色图、隔离、fail-closed、run_id、事件序列。

全部离线：MockPlanner + ScriptedReportWorker / VerifierWorker / SynthesizerWorker。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.contracts import WorkerResult
from agentboard.orchestrator import (
    P5_ROLES,
    MultiAgentOrchestrator,
    OrchestratorConfig,
    ScriptedReportWorker,
    SynthesizerWorker,
    VerifierWorker,
    context_with_candidates,
)
from agentboard.planner import MockPlanner
from agentboard.workers.base import WorkerRegistry
from agentboard.workers.mock import MockWorker

CANDS = ("pgvector", "qdrant_local")


def _planner_board(
    *,
    researcher_deps: tuple[str, ...] = (),
    synth_deps: tuple[str, ...] = ("verify_report",),
    research_context: str | None = None,
) -> dict[str, Any]:
    ctx = research_context or context_with_candidates(CANDS)
    return {
        "tasks": [
            {
                "task_id": "research_candidates",
                "title": "研究候选",
                "assigned_agent": "researcher",
                "goal": "研究候选方案",
                "context": ctx,
                "depends_on": list(researcher_deps),
            },
            {
                "task_id": "verify_report",
                "title": "验证报告",
                "assigned_agent": "verifier",
                "goal": "独立重验",
                "context": ctx,
                "depends_on": ["research_candidates"],
            },
            {
                "task_id": "synthesize",
                "title": "合成最终报告",
                "assigned_agent": "synthesizer",
                "goal": "合成",
                "context": ctx,
                "depends_on": list(synth_deps),
            },
        ]
    }


def make_orchestrator(
    raw_board: dict[str, Any],
    *,
    researcher: Any | None = None,
    verifier: Any | None = None,
    synthesizer: Any | None = None,
    config: OrchestratorConfig | None = None,
    register: dict[str, Any] | None = None,
) -> MultiAgentOrchestrator:
    registry = WorkerRegistry()
    for name, adapter in {
        "researcher": researcher or ScriptedReportWorker(agent_name="researcher"),
        "verifier": verifier or VerifierWorker(agent_name="verifier"),
        "synthesizer": synthesizer or SynthesizerWorker(agent_name="synthesizer"),
        **(register or {}),
    }.items():
        registry.register(name, adapter)
    return MultiAgentOrchestrator(
        planner=MockPlanner(raw_board),
        registry=registry,
        config=config,
        planner_identity="mock-planner.v1:test",
    )


def run(orchestrator: MultiAgentOrchestrator, goal: str = "选型研究") -> Any:
    return asyncio.run(orchestrator.run(goal))


def _event_types(result: Any) -> list[str]:
    return [event["event"] for event in result.events]


# ================= 正常闭环 =================


def test_normal_collaboration_completes_with_final_report() -> None:
    result = run(make_orchestrator(_planner_board()))
    assert result.outcome == "completed"
    assert result.final_report is not None
    assert result.final_report["schema"] == "multi_agent.final.v1"
    assert result.final_report_id.startswith("mafr_")
    assert set(result.verdicts.values()) == {"passed"}
    assert result.run_id.startswith("ma_")


def test_event_sequence_is_stable_and_ordered() -> None:
    """事件序列：research → verify → synth 的派发顺序 + verifier_passed。"""
    result = run(make_orchestrator(_planner_board()))
    types = _event_types(result)
    assert types[0] == "board_created"
    assert "verifier_passed" in types
    started = [e for e in result.events if e["event"] == "task_started"]
    order = [e["task_id"] for e in started]
    assert order.index("research_candidates") < order.index("verify_report")
    assert order.index("verify_report") < order.index("synthesize")
    # 事件序列不含时间戳（重放口径）
    assert all("timestamp" not in event for event in result.events)
    # 同输入重放：同一事件序列
    again = run(make_orchestrator(_planner_board()))
    assert again.events == result.events


# ================= 角色图 / 计划级 fail-closed =================


def test_synthesizer_cannot_bypass_verifier() -> None:
    """synthesizer 直接依赖 researcher（越过 verifier）⇒ 计划拒绝。"""
    board = _planner_board(synth_deps=("research_candidates",))
    result = run(make_orchestrator(board))
    assert result.outcome == "failed"
    assert result.failure_reason.startswith("plan_rejected:")
    assert "角色图" in result.failure_reason
    assert _event_types(result) == ["plan_rejected"]


def test_verifier_must_depend_on_researcher() -> None:
    board = {
        "tasks": [
            {
                "task_id": "verify_report",
                "title": "验证",
                "assigned_agent": "verifier",
                "goal": "g",
                "context": context_with_candidates(CANDS),
                "depends_on": [],
            },
            {
                "task_id": "synthesize",
                "title": "合成",
                "assigned_agent": "synthesizer",
                "goal": "g",
                "context": context_with_candidates(CANDS),
                "depends_on": ["verify_report"],
            },
        ]
    }
    result = run(make_orchestrator(board))
    assert result.outcome == "failed"
    assert "plan_rejected" in result.failure_reason


def test_unknown_dependency_is_plan_rejected() -> None:
    """depends_on 引用板上不存在的任务 ⇒ 计划拒绝（fail-closed）。"""
    board = _planner_board(researcher_deps=("ghost_task",))
    result = run(make_orchestrator(board))
    assert result.outcome == "failed"
    assert result.failure_reason.startswith("plan_rejected:")


def test_unknown_role_is_plan_rejected() -> None:
    board = _planner_board()
    board["tasks"][0]["assigned_agent"] = "hacker"
    result = run(make_orchestrator(board))
    assert result.outcome == "failed"
    assert "plan_rejected" in result.failure_reason


# ================= 重试 / blocked =================


def test_worker_retry_then_success() -> None:
    researcher = ScriptedReportWorker(agent_name="researcher", fail_times=1)
    result = run(make_orchestrator(_planner_board(), researcher=researcher))
    assert result.outcome == "completed"
    assert "task_retry" in _event_types(result)


def test_three_failures_lead_to_blocked() -> None:
    """三次尝试全部失败（max_card_attempts=3）⇒ 卡片 failed ⇒ 运行 blocked。"""
    researcher = ScriptedReportWorker(agent_name="researcher", fail_times=9)
    result = run(
        make_orchestrator(
            _planner_board(), researcher=researcher,
            config=OrchestratorConfig(max_card_attempts=3),
        )
    )
    assert result.outcome == "blocked"
    started = [e for e in result.events if e["event"] == "task_started" and e["task_id"] == "research_candidates"]
    assert len(started) == 3
    assert "task_failed" in _event_types(result)
    assert result.final_report is None
    card = result.board_state["cards"]["research_candidates"]
    assert card["status"] == "failed"


# ================= 边界 / 不变量 fail-closed =================


class _ForeignRefsWorker(ScriptedReportWorker):
    """越权引用：载荷 task_refs 指向未声明的上游任务。"""

    async def run(self, task_input: Any) -> WorkerResult:
        result = await super().run(task_input)
        payload = dict(result.structured_payload)
        payload["task_refs"] = ["task_never_declared"]
        from dataclasses import replace

        return replace(result, structured_payload=payload)


def test_unauthorized_task_refs_fail_closed() -> None:
    result = run(make_orchestrator(_planner_board(), researcher=_ForeignRefsWorker()))
    assert result.outcome == "blocked"
    failed_events = [e for e in result.events if e["event"] == "task_failed"]
    assert failed_events and "越权" in failed_events[0]["data"]["reason"]


class _WrongTaskIdWorker(ScriptedReportWorker):
    """结果与卡片不一致（伪造归属）⇒ board 不变量违例。"""

    async def run(self, task_input: Any) -> WorkerResult:
        result = await super().run(task_input)
        from dataclasses import replace

        return replace(result, task_id="someone_else")


def test_stale_or_mismatched_result_fails_run() -> None:
    result = run(make_orchestrator(_planner_board(), researcher=_WrongTaskIdWorker()))
    assert result.outcome == "failed"
    assert result.failure_reason.startswith("board_invariant_violated:")


def test_unknown_worker_fails_run_observably() -> None:
    """未注册 worker ⇒ 运行失败（不猜测、不 fallback、不静默）。"""
    registry = WorkerRegistry()
    registry.register("researcher", ScriptedReportWorker(agent_name="researcher"))
    registry.register("synthesizer", SynthesizerWorker(agent_name="synthesizer"))
    orchestrator = MultiAgentOrchestrator(
        planner=MockPlanner(_planner_board()),
        registry=registry,
        planner_identity="mock-planner.v1:test",
    )
    result = run(orchestrator)
    assert result.outcome == "failed"
    assert result.failure_reason == "unknown_worker:verifier"


# ================= verifier / synthesizer 语义 =================


class _SelfConsistentForgedReportWorker(ScriptedReportWorker):
    """重写 report_id 并同步摘要：摘要一致也不能让伪造内容寻址 id 通过。"""

    async def run(self, task_input: Any) -> WorkerResult:
        result = await super().run(task_input)
        import hashlib
        import json
        from dataclasses import replace

        payload = dict(result.structured_payload)
        report = json.loads(payload["report_json"])
        report["report_id"] = "rp_0000000000000000"
        report_json = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["report_json"] = report_json
        payload["report_id"] = report["report_id"]
        payload["content_digest"] = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        return replace(result, structured_payload=payload)


class _VerifierBypassingSynthesizer:
    """恶意 synthesizer：忽略 rejected verdict 并声称 passed。"""

    agent_name = "synthesizer"
    identity = "rogue-synth.v1"

    async def run(self, task_input: Any) -> WorkerResult:
        return WorkerResult(
            task_id=task_input.task_id,
            agent_name=self.agent_name,
            facts=("rogue",),
            evidence=(),
            risks=(),
            open_questions=(),
            confidence=1.0,
            structured_payload={
                "final_report_id": "mafr_rogue",
                "schema": "multi_agent.final.v1",
                "synthesis": "rogue",
                "task_refs": sorted(task_input.upstream_results),
                "verdicts": {key: "passed" for key in task_input.upstream_results},
            },
        )


class _TamperedDigestWorker(ScriptedReportWorker):
    """篡改载荷内容但保留旧 digest ⇒ verifier 重算摘要必抓。"""

    async def run(self, task_input: Any) -> WorkerResult:
        result = await super().run(task_input)
        payload = dict(result.structured_payload)
        payload["report_json"] = payload["report_json"].replace("passed", "PASSED_TAMPERED")
        from dataclasses import replace

        return replace(result, structured_payload=payload)


def test_verifier_rejects_self_consistent_forged_report_id() -> None:
    result = run(
        make_orchestrator(
            _planner_board(), researcher=_SelfConsistentForgedReportWorker()
        )
    )
    assert result.outcome == "blocked"
    assert result.verdicts["verify_report"] == "rejected"
    assert result.final_report is None
    reasons = result.board_state["cards"]["synthesize"]["error"]
    assert "rejected" in reasons


def test_orchestrator_final_gate_rejects_synthesizer_bypassing_rejected_verdict() -> None:
    result = run(
        make_orchestrator(
            _planner_board(),
            verifier=VerifierWorker(force_reject_reason="attack"),
            synthesizer=_VerifierBypassingSynthesizer(),
        )
    )
    assert result.outcome == "blocked"
    assert result.verdicts["verify_report"] == "rejected"
    assert result.final_report is None
    assert result.board_state["cards"]["synthesize"]["status"] == "failed"


def test_verifier_rejects_tampered_report_and_blocks_synthesis() -> None:
    """载荷被篡改 ⇒ verifier 重算 digest 抓住 ⇒ rejected ⇒ synthesizer 失败 ⇒ blocked。"""
    result = run(make_orchestrator(_planner_board(), researcher=_TamperedDigestWorker()))
    assert result.outcome == "blocked"
    assert "verifier_rejected" in _event_types(result)
    assert result.verdicts["verify_report"] == "rejected"
    assert result.final_report is None
    # synthesizer 卡片最终 failed（不可信结论没进最终报告）
    card = result.board_state["cards"]["synthesize"]
    assert card["status"] == "failed"
    assert "rejected" in card["error"]


def test_synthesizer_rejects_missing_verdict_payload() -> None:
    """verifier 产出违例载荷 ⇒ verifier 卡片失败（重试耗尽）⇒ blocked；

    synthesizer 从未收到任何 verdict（synthesize 卡保持 todo）——
    "只消费验证通过的结构化结果"由流转顺序保证。
    """
    result = run(
        make_orchestrator(
            _planner_board(),
            verifier=MockWorker(agent_name="verifier"),
        )
    )
    assert result.outcome == "blocked"
    verify_card = result.board_state["cards"]["verify_report"]
    assert verify_card["status"] == "failed"
    assert "schema" in verify_card["error"]
    # synthesizer 卡从未被派发（没有可消费的 verdict）
    assert result.board_state["cards"]["synthesize"]["status"] == "todo"
    assert result.final_report is None


# ================= run_id 身份 =================


def test_run_id_stable_and_covers_config_and_goal() -> None:
    a = run(make_orchestrator(_planner_board()))
    b = run(make_orchestrator(_planner_board()))
    assert a.run_id == b.run_id

    other_goal = run(make_orchestrator(_planner_board()), goal="不同的研究目标")
    assert other_goal.run_id != a.run_id

    other_config = run(
        make_orchestrator(_planner_board(), config=OrchestratorConfig(max_card_attempts=2))
    )
    assert other_config.run_id != a.run_id

    # 注册表变化（多注册一个 worker ⇒ worker_identities 变）⇒ run_id 变
    extra_registry_orchestrator = make_orchestrator(
        _planner_board(), register={"planner": MockWorker(agent_name="planner")}
    )
    other_registry = run(extra_registry_orchestrator)
    assert other_registry.run_id != a.run_id


def test_run_id_changes_with_board_result_and_event_data() -> None:
    base = run(make_orchestrator(_planner_board()))
    changed_board = _planner_board()
    changed_board["tasks"][0]["context"] = context_with_candidates(CANDS) + "\nchanged: 1"
    changed = run(make_orchestrator(changed_board))
    assert changed.run_id != base.run_id

    class _DifferentSummaryWorker(ScriptedReportWorker):
        async def run(self, task_input: Any) -> WorkerResult:
            result = await super().run(task_input)
            from dataclasses import replace

            return replace(result, summary=result.summary + " changed")

    changed_result = run(
        make_orchestrator(
            _planner_board(), researcher=_DifferentSummaryWorker(agent_name="researcher")
        )
    )
    assert changed_result.run_id != base.run_id


def test_worker_identities_in_result() -> None:
    result = run(make_orchestrator(_planner_board()))
    assert set(result.worker_identities) == {"researcher", "synthesizer", "verifier"}
    assert result.worker_identities["verifier"].startswith("verifier.v1:")
    assert result.decision_identity.startswith("orchestrator.v1:")


def test_roles_closed_set() -> None:
    assert P5_ROLES == ("planner", "researcher", "verifier", "synthesizer")


def test_context_declaration_is_structural() -> None:
    """缺 candidate_ids 声明 ⇒ 计划拒绝（结构化契约，不允许猜）。"""
    board = _planner_board(research_context="没有声明的自由文本")
    result = run(make_orchestrator(board))
    assert result.outcome == "failed"
    assert "plan_rejected" in result.failure_reason


# ================= P5 追加独立负控 =================

class _RehashedInvalidReportWorker(ScriptedReportWorker):
    """攻击者同步重算 id/摘要；门禁必须核对业务规则，不能仅核对哈希。"""

    def __init__(self, mutation: str) -> None:
        super().__init__()
        self.mutation = mutation

    async def run(self, task_input: Any) -> WorkerResult:
        import hashlib
        import json
        from dataclasses import replace

        result = await super().run(task_input)
        payload = dict(result.structured_payload)
        report = json.loads(payload["report_json"])
        if self.mutation == "weight":
            report["dimensions"][0]["weight"] = 0.5
        elif self.mutation == "score":
            for recommendation in report["recommendations"]:
                recommendation["score"] = 0.1
        elif self.mutation == "empty_checks":
            report["integrity"]["checks"] = []
        elif self.mutation in ("cross_candidate", "synthetic_only"):
            from agentboard.research.assemble import build_recommendations
            from agentboard.research.models import (
                ComparisonDimension,
                ResearchFinding,
                derive_coverage,
            )
            from agentboard.research.scoring import score_all

            findings = [ResearchFinding.from_dict(item) for item in report["findings"]]
            if self.mutation == "cross_candidate":
                findings[0] = replace(findings[0], evidence_refs=findings[1].evidence_refs, finding_id="")
            else:
                from agentboard.workers.synthetic import SyntheticEvidenceFactory

                findings = [replace(
                    finding, finding_id="",
                    evidence_refs=SyntheticEvidenceFactory.for_task(
                        self.agent_name, f"{task_input.task_id}_{finding.candidate_id}", count=1
                    ),
                ) for finding in findings]
            scores = score_all(candidate_ids=report["candidate_ids"],
                               dimensions=tuple(ComparisonDimension.from_dict(item)
                                                for item in report["dimensions"]), findings=findings)
            recs = build_recommendations(scores=scores, findings=findings, risks=())
            report["findings"] = [item.to_dict() for item in findings]
            report["recommendations"] = [item.to_dict() for item in recs]
            report["coverage"] = derive_coverage(
                candidate_ids=tuple(report["candidate_ids"]),
                dimension_ids=tuple(item["dimension_id"] for item in report["dimensions"]),
                findings=tuple(findings),
            ).to_dict()
            report["evidence_index"] = {
                ref.evidence_id: ref.to_dict() for finding in findings for ref in finding.evidence_refs
            }
        else:
            raise AssertionError(self.mutation)
        projection = {key: value for key, value in report.items()
                      if key not in ("report_id", "integrity", "evidence_index")}
        encode = lambda data: json.dumps(data, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"))
        report["report_id"] = "rp_" + hashlib.sha256(encode(projection).encode()).hexdigest()[:16]
        payload["report_id"] = report["report_id"]
        payload["report_json"] = encode(report)
        payload["content_digest"] = hashlib.sha256(payload["report_json"].encode()).hexdigest()
        return replace(result, structured_payload=payload)


@pytest.mark.parametrize("mutation,expected_reason", [
    ("weight", "I8"),
    ("score", "重算结果不一致"),
    ("empty_checks", "I1–I9"),
    ("cross_candidate", "其它候选"),
    ("synthetic_only", "I4"),
])
def test_verifier_rejects_rehashed_invalid_business_report(
    mutation: str, expected_reason: str
) -> None:
    result = run(make_orchestrator(_planner_board(), researcher=_RehashedInvalidReportWorker(mutation)))
    assert result.outcome == "blocked"
    assert result.verdicts == {"verify_report": "rejected"}
    verdict = result.board_state["cards"]["verify_report"]["result"]["structured_payload"]
    assert any(expected_reason in reason for reason in verdict["reasons"])
    assert result.final_report is None


def test_scripted_report_has_real_fixture_backing_and_full_integrity_checks() -> None:
    import json

    result = run(make_orchestrator(_planner_board()))
    assert result.outcome == "completed"
    research = result.board_state["cards"]["research_candidates"]["result"]["structured_payload"]
    report = json.loads(research["report_json"])
    assert [check["code"] for check in report["integrity"]["checks"]] == [f"I{i}" for i in range(1, 10)]
    assert all(check["passed"] for check in report["integrity"]["checks"])
    assert all(ref["source_type"] == "fixture_snapshot"
               for finding in report["findings"] for ref in finding["evidence_refs"])


def test_orchestrator_rejects_forged_final_id_even_with_all_verifiers_passed() -> None:
    result = run(make_orchestrator(_planner_board(), synthesizer=_VerifierBypassingSynthesizer()))
    assert result.verdicts == {"verify_report": "passed"}
    assert result.outcome == "blocked"
    assert result.final_report is None
    assert "伪造 id" in result.board_state["cards"]["synthesize"]["error"]

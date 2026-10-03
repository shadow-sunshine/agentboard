"""多 Agent 编排主循环（P5 multi-agent-orchestration-mvp）。

一次闭环（单次运行，无无限循环）：

    Planner.propose → 计划级护栏（板校验 + 角色图）→ 任务板状态机
    → dispatch/retry（isolation 收窄任务包）→ worker 执行（结构化契约）
    → 边界校验（越权引用 fail-closed）→ verify（独立重验）→ synthesize
    → 最终报告（只含验证通过的结论）

fail-closed 清单（每条都有负控测试/评测场景钉住）：
- 计划违反板护栏或角色图 ⇒ plan_rejected（派发前终止）；
- 未知 worker ⇒ 运行失败（不猜测、不 fallback）；
- worker 产出越权 task_refs ⇒ 卡片失败（ContextBoundaryError）；
- 结果与卡片不一致 / 过期卡片版本 ⇒ board_invariant_violated（运行失败，非重试路径）；
- verifier rejected ⇒ synthesizer 失败 ⇒ 重试耗尽 ⇒ blocked；
- 循环步数预算耗尽 ⇒ loop_budget_exhausted（杜绝无限循环）。

运行身份（run_id）：规范 JSON 覆盖 goal、planner 身份、编排配置、各 worker
identity、任务板结构、**事件序列（不含时间戳）**、验证结论与最终 report_id
——同输入稳定，改变任一决定性输入必变。日志/审计不含凭据与整段模型原文。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..board import (
    apply_failure,
    apply_success,
    cards_to_state,
    decide_board_action,
    done_cards,
    prepare_task,
)
from ..contracts import TaskCard, WorkerResult
from ..events import (
    EVT_BLOCKED,
    EVT_PLAN_REJECTED,
    EVT_VERIFIER_PASSED,
    EVT_VERIFIER_REJECTED,
    BoardEvent,
)
from ..isolation import build_worker_input
from ..planner import Planner, propose_and_commit
from ..util import thaw_payload
from ..workers.base import WorkerRegistry
from .errors import (
    BoardInvariantError,
    ContextBoundaryError,
    OrchestratorPlanError,
)
from .roles import P5_ROLES, payload_task_refs, validate_role_graph
from .synthesizer import FINAL_PAYLOAD_SCHEMA, expected_final_report_id, validate_final_payload
from .verifier import VERDICT_PASSED, validate_verdict_payload

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_BLOCKED = "blocked"

RUN_ID_SCHEMA = "multi_agent.orchestration.run_id.v1"


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """编排器配置（全部显式、计数式；to_summary 进入 run_id）。"""

    #: 每卡最大尝试次数（含首次；耗尽 ⇒ 卡片 failed ⇒ 运行 blocked）。
    max_card_attempts: int = 3
    #: 主循环步数上限（每步派发或结算一张卡；杜绝无限循环）。
    max_loop_steps: int = 64

    def __post_init__(self) -> None:
        for name in ("max_card_attempts", "max_loop_steps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"OrchestratorConfig.{name} 必须是 >=1 的整数，实际 {value!r}")

    def to_summary(self) -> dict[str, int]:
        return {
            "max_card_attempts": self.max_card_attempts,
            "max_loop_steps": self.max_loop_steps,
        }


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """一次编排运行的完整可观测结果。"""

    run_id: str
    outcome: str
    failure_reason: str
    board_state: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    verdicts: dict[str, str]
    final_report: dict[str, Any] | None
    final_report_id: str
    decision_identity: str
    worker_identities: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "board_state": self.board_state,
            "events": [dict(event) for event in self.events],
            "verdicts": dict(self.verdicts),
            "final_report": self.final_report,
            "final_report_id": self.final_report_id,
            "decision_identity": self.decision_identity,
            "worker_identities": dict(self.worker_identities),
        }


def _worker_identity(registry: WorkerRegistry, name: str) -> str:
    try:
        adapter = registry.get(name)
    except KeyError:
        return f"unregistered:{name}"
    identity = getattr(adapter, "identity", None)
    if isinstance(identity, str) and identity.strip():
        return identity
    return f"unidentified:{name}"


def _planner_identity(planner: Planner) -> str:
    identity = getattr(planner, "identity", None)
    if isinstance(identity, str) and identity.strip():
        return identity
    return "unidentified-planner"


def _check_task_refs(result: WorkerResult, card: TaskCard) -> None:
    """越权引用校验：载荷里的 task_refs 必须 ⊆ 卡片依赖 ∪ 自身。

    只对携带结构化载荷（multi-agent.* / live-research.report.v1）的结果生效；
    空载荷（如 MockWorker）不涉及。违例 ⇒ ContextBoundaryError（卡片失败）。
    """
    payload = thaw_payload(result.structured_payload)
    if not payload:
        return
    schema = payload.get("schema")
    if not isinstance(schema, str) or not schema.startswith(
        ("multi_agent.", "live_research.")
    ):
        return
    refs = payload_task_refs(payload)
    if tuple(sorted(refs)) != tuple(sorted(set(refs))):
        raise ContextBoundaryError(
            f"{card.task_id}: task_refs 含重复项 {list(refs)}"
        )
    allowed = set(card.depends_on) | {card.task_id}
    out_of_scope = sorted(set(refs) - allowed)
    if out_of_scope:
        raise ContextBoundaryError(
            f"{card.task_id}: 载荷引用了未声明的上游任务 {out_of_scope}"
            f"（可引用范围 {sorted(allowed)}）——越权字段 fail-closed"
        )


def _event_dicts(events: list[BoardEvent]) -> tuple[dict[str, Any], ...]:
    """事件序列（稳定性口径：event/task_id/agent/attempt/data；**不含时间戳**）。"""
    out: list[dict[str, Any]] = []
    for event in events:
        d = event.to_dict()
        d.pop("timestamp")
        out.append(d)
    return tuple(out)


def _validate_final_gate(
    *,
    final_payload: dict[str, Any],
    upstream_results: dict[str, WorkerResult],
) -> None:
    """编排器级最终门禁：不依赖 synthesizer 自觉遵守 verdict。"""
    expected_refs = sorted(upstream_results)
    actual_refs = final_payload.get("task_refs")
    if actual_refs != expected_refs:
        raise BoardInvariantError(
            f"最终载荷 task_refs 不等于实际 verifier 上游：{actual_refs!r} != {expected_refs!r}"
        )
    actual_verdicts = final_payload.get("verdicts")
    expected_verdicts: dict[str, str] = {}
    report_ids: dict[str, str] = {}
    for task_id in expected_refs:
        upstream = upstream_results[task_id]
        if upstream.agent_name != "verifier":
            raise BoardInvariantError(f"最终载荷上游 {task_id} 不是 verifier 结果")
        upstream_payload = validate_verdict_payload(
            thaw_payload(upstream.structured_payload), label=f"verifier {task_id}"
        )
        expected_verdicts[task_id] = str(upstream_payload["verdict"])
        report_ids[task_id] = str(upstream_payload["verified_report_id"])
    if actual_verdicts != expected_verdicts:
        raise BoardInvariantError(
            f"最终载荷 verdicts 与实际 verifier 结论不一致：{actual_verdicts!r} != {expected_verdicts!r}"
        )
    if not expected_verdicts or any(value != VERDICT_PASSED for value in expected_verdicts.values()):
        raise BoardInvariantError("存在未通过的 verifier 结论，禁止接受最终报告")
    expected_id = expected_final_report_id(report_ids=report_ids, verdicts=expected_verdicts)
    if final_payload.get("final_report_id") != expected_id:
        raise BoardInvariantError("最终 report_id 与实际 verifier 上游不一致，禁止接受伪造 id")



class MultiAgentOrchestrator:
    """Planner → board → workers（researcher/verifier/synthesizer）→ 报告。"""

    def __init__(
        self,
        *,
        planner: Planner,
        registry: WorkerRegistry,
        config: OrchestratorConfig | None = None,
        planner_identity: str | None = None,
    ) -> None:
        self._planner = planner
        self._registry = registry
        self._config = config or OrchestratorConfig()
        self._planner_identity = planner_identity or _planner_identity(planner)

    async def run(self, goal: str) -> OrchestrationResult:
        events: list[BoardEvent] = []
        verdicts: dict[str, str] = {}
        final_report: dict[str, Any] | None = None
        final_report_id = ""

        # ---- 阶段 1：计划（提议 → 板护栏 → 角色图；计划级 fail-closed） ----
        try:
            cards, event = await propose_and_commit(
                self._planner, goal, allowed_agents=P5_ROLES
            )
            validate_role_graph(cards)
        except (OrchestratorPlanError, ValueError) as exc:
            events.append(
                BoardEvent(
                    EVT_PLAN_REJECTED,
                    data={"reason": str(exc)[:400]},
                )
            )
            return self._finalize(
                goal=goal,
                cards=[],
                events=events,
                outcome=OUTCOME_FAILED,
                failure_reason=f"plan_rejected:{exc}",
                verdicts=verdicts,
                final_report=None,
                final_report_id="",
            )
        events.append(event)

        # ---- 阶段 2：调度主循环（状态变化全部经 board 护栏） ----
        outcome = OUTCOME_FAILED
        failure_reason = ""
        steps = 0
        while True:
            steps += 1
            if steps > self._config.max_loop_steps:
                outcome = OUTCOME_FAILED
                failure_reason = (
                    f"loop_budget_exhausted:{self._config.max_loop_steps}"
                )
                events.append(
                    BoardEvent(EVT_BLOCKED, data={"reason": failure_reason})
                )
                break
            decision = decide_board_action(cards)
            if decision.action == "finish":
                outcome = OUTCOME_COMPLETED
                break
            if decision.action == "blocked":
                outcome = OUTCOME_BLOCKED
                failure_reason = decision.reason
                events.append(
                    BoardEvent(
                        EVT_BLOCKED,
                        data={"reason": decision.reason},
                    )
                )
                break
            # dispatch / retry
            task_id = decision.task_id
            target = next((c for c in cards if c.task_id == task_id), None)
            if target is None:
                outcome = OUTCOME_FAILED
                failure_reason = f"board_invariant_violated:{task_id}:卡片丢失"
                break
            try:
                worker = self._registry.get(target.assigned_agent)
            except KeyError:
                events.append(
                    BoardEvent(
                        EVT_BLOCKED,
                        task_id=task_id,
                        agent=target.assigned_agent,
                        data={"reason": f"unknown_worker:{target.assigned_agent}"},
                    )
                )
                outcome = OUTCOME_FAILED
                failure_reason = f"unknown_worker:{target.assigned_agent}"
                break
            cards, running, started = prepare_task(cards, task_id)
            events.extend(started)

            failure: BaseException | None = None
            role_payload: dict[str, Any] | None = None
            try:
                worker_input = build_worker_input(running, done_cards(cards))
                result = await worker.run(worker_input)
                if not isinstance(result, WorkerResult):
                    raise BoardInvariantError(
                        f"{task_id}: worker 返回了非 WorkerResult 对象"
                    )
                _check_task_refs(result, running)
                # 角色专属载荷校验（在写入任务板**之前**完成——写入即代表可信收讫；
                # 违例载荷 ⇒ 卡片失败，可重试）
                if running.assigned_agent == "verifier":
                    role_payload = validate_verdict_payload(
                        thaw_payload(result.structured_payload),
                        label=f"verifier {task_id}",
                    )
                elif running.assigned_agent == "synthesizer":
                    role_payload = validate_final_payload(
                        thaw_payload(result.structured_payload), label=task_id
                    )
                    _validate_final_gate(
                        final_payload=role_payload,
                        upstream_results=worker_input.upstream_results,
                    )
            except Exception as exc:  # noqa: BLE001 —— 分类后走任务板失败语义
                failure = exc
            if failure is None:
                try:
                    cards, done_events = apply_success(cards, running, result)
                    events.extend(done_events)
                except (TypeError, ValueError) as exc:
                    # 结果与卡片不一致 / 过期卡片版本：任务板不变量违例，
                    # 属于运行失败（不是可重试路径）。
                    outcome = OUTCOME_FAILED
                    failure_reason = (
                        f"board_invariant_violated:{task_id}:{exc}"
                    )
                    break
                # verifier 结论入审计与事件（载荷已通过闭集校验）
                if running.assigned_agent == "verifier" and role_payload is not None:
                    verdicts[task_id] = str(role_payload["verdict"])
                    events.append(
                        BoardEvent(
                            EVT_VERIFIER_PASSED
                            if role_payload["verdict"] == VERDICT_PASSED
                            else EVT_VERIFIER_REJECTED,
                            task_id=task_id,
                            agent=running.assigned_agent,
                            attempt=running.attempts,
                            data={
                                "reasons": list(role_payload["reasons"]),
                                "verified_report_id": role_payload["verified_report_id"],
                            },
                        )
                    )
                if running.assigned_agent == "synthesizer" and role_payload is not None:
                    final_report = role_payload
                    final_report_id = str(role_payload["final_report_id"])
            else:
                cards, failure_events = apply_failure(
                    cards, running, failure,
                    max_attempts=self._config.max_card_attempts,
                )
                events.extend(failure_events)

        return self._finalize(
            goal=goal,
            cards=cards,
            events=events,
            outcome=outcome,
            failure_reason=failure_reason,
            verdicts=verdicts,
            final_report=final_report,
            final_report_id=final_report_id,
        )

    # ---- 收尾 ----

    def _finalize(
        self,
        *,
        goal: str,
        cards: list[TaskCard],
        events: list[BoardEvent],
        outcome: str,
        failure_reason: str,
        verdicts: dict[str, str],
        final_report: dict[str, Any] | None,
        final_report_id: str,
    ) -> OrchestrationResult:
        worker_identities = {
            name: _worker_identity(self._registry, name) for name in sorted(self._registry.names())
        }
        payload = {
            "schema": RUN_ID_SCHEMA,
            "goal": goal,
            "planner_identity": self._planner_identity,
            "orchestrator_config": self._config.to_summary(),
            "worker_identities": worker_identities,
            "board": cards_to_state(cards),
            "events": [
                {
                    "agent": event.agent,
                    "attempt": event.attempt,
                    "event": event.event_type,
                    "task_id": event.task_id,
                    "data": thaw_payload(event.data),
                }
                for event in events
            ],
            "verdicts": {key: verdicts[key] for key in sorted(verdicts)},
            "final_report": final_report,
            "final_report_id": final_report_id,
            "outcome": outcome,
            "failure_reason": failure_reason,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        run_id = "ma_" + hashlib.sha256(encoded).hexdigest()[:16]
        return OrchestrationResult(
            run_id=run_id,
            outcome=outcome,
            failure_reason=failure_reason,
            board_state=cards_to_state(cards),
            events=_event_dicts(events),
            verdicts=dict(verdicts),
            final_report=final_report,
            final_report_id=final_report_id,
            decision_identity=f"orchestrator.v1:{self._planner_identity}",
            worker_identities=worker_identities,
        )


def run_orchestration(orchestrator: MultiAgentOrchestrator, goal: str) -> OrchestrationResult:
    """同步入口（评测/脚本用；核心保持 async）。"""
    return asyncio.run(orchestrator.run(goal))


__all__ = [
    "FINAL_PAYLOAD_SCHEMA",
    "RUN_ID_SCHEMA",
    "MultiAgentOrchestrator",
    "OrchestrationResult",
    "OrchestratorConfig",
    "run_orchestration",
]

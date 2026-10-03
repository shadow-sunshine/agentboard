"""角色定义与角色图校验（P5 多 Agent 编排）。

四类角色（任务书要求的最小集合）：

- **planner**：只提议任务计划（`Planner.propose`），不写任何运行时状态；
- **researcher**：执行研究（接入 P3/P4 runtime），产出带结构化载荷的研究结果；
- **verifier**：独立重验上游研究产物的证据/完整性，给出 passed/rejected 结论；
- **synthesizer**：只消费**验证通过**的结论，产出最终报告。

角色图规则（计划级 fail-closed，派发前全部检查）：

- researcher 的依赖只能是 researcher（或无依赖）；
- verifier 必须依赖 ≥1 个 researcher，且**只能**依赖 researcher；
- synthesizer 必须依赖 ≥1 个 verifier，且**只能**依赖 verifier
  ——"synthesizer 不得越过 verifier"由此在计划层强制；
- planner 卡不允许有依赖（规划已经发生，不存在上游）；
- 依赖必须引用板内已存在的 task_id（DAG 无环由既有 validate_model_board 保证）。

上下文声明（结构化、闭集格式）：researcher / verifier 卡的 `context` 必须包含
一行 `candidate_ids: <逗号分隔的候选标识>`——这是该卡研究/验证范围的**唯一**
声明来源；worker 与 verifier 都从自己的任务包里读它，不读板外状态。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..contracts import TaskCard
from .errors import OrchestratorPlanError

ROLE_PLANNER = "planner"
ROLE_RESEARCHER = "researcher"
ROLE_VERIFIER = "verifier"
ROLE_SYNTHESIZER = "synthesizer"

#: P5 角色闭集（传给 validate_model_board 的 allowed_agents）。
P5_ROLES: tuple[str, ...] = (ROLE_PLANNER, ROLE_RESEARCHER, ROLE_VERIFIER, ROLE_SYNTHESIZER)

#: 上下文中声明候选范围的前缀（唯一合法格式；解析失败即计划/边界错误）。
CANDIDATE_IDS_PREFIX = "candidate_ids:"

_CREATURE_RULES: dict[str, str] = {
    ROLE_PLANNER: "planner 卡不允许声明依赖（规划在提议时已完成）",
    ROLE_RESEARCHER: "researcher 卡只能依赖 researcher 卡（或无依赖）",
    ROLE_VERIFIER: "verifier 卡必须依赖至少 1 个 researcher 卡，且只能依赖 researcher 卡",
    ROLE_SYNTHESIZER: "synthesizer 卡必须依赖至少 1 个 verifier 卡，且只能依赖 verifier 卡",
}


def validate_role_graph(cards: list[TaskCard]) -> None:
    """按角色校验依赖图与范围声明（计划级 fail-closed；违例抛 OrchestratorPlanError）。"""
    role_by_id = {card.task_id: card.assigned_agent for card in cards}
    for card in cards:
        role = card.assigned_agent
        rule = _CREATURE_RULES.get(role)
        if rule is None:
            raise OrchestratorPlanError(
                f"任务 {card.task_id} 指派了未知角色 {role!r}（角色闭集 {list(P5_ROLES)}）"
            )
        # 范围声明是结构化契约：researcher / verifier 卡必须显式声明 candidate_ids
        if role in (ROLE_RESEARCHER, ROLE_VERIFIER):
            try:
                declared_candidates(card.context)
            except OrchestratorPlanError as exc:
                raise OrchestratorPlanError(
                    f"任务 {card.task_id} 的范围声明非法：{exc}"
                ) from exc
        unknown = [dep for dep in card.depends_on if dep not in role_by_id]
        if unknown:
            raise OrchestratorPlanError(
                f"任务 {card.task_id} 依赖了板上不存在的任务：{unknown}"
            )
        dep_roles = {role_by_id[dep] for dep in card.depends_on}
        if role == ROLE_PLANNER:
            if card.depends_on:
                raise OrchestratorPlanError(f"任务 {card.task_id} 违反角色图：{rule}")
        elif role == ROLE_RESEARCHER:
            if dep_roles - {ROLE_RESEARCHER}:
                raise OrchestratorPlanError(f"任务 {card.task_id} 违反角色图：{rule}")
        elif role in (ROLE_VERIFIER, ROLE_SYNTHESIZER):
            # verifier 只能依赖 researcher 且必须 ≥1；synthesizer 只能依赖 verifier 且必须 ≥1
            required = ROLE_RESEARCHER if role == ROLE_VERIFIER else ROLE_VERIFIER
            if required not in dep_roles or dep_roles - {required}:
                raise OrchestratorPlanError(f"任务 {card.task_id} 违反角色图：{rule}")


def declared_candidates(context: str) -> tuple[str, ...]:
    """从任务卡 context 解析候选范围声明（唯一合法行：`candidate_ids: a,b`）。

    解析失败（缺失 / 多行 / 空值）抛 OrchestratorPlanError —— 范围声明是
    结构化契约的一部分，不允许"猜"或"从别处读"。
    """
    if not isinstance(context, str):
        raise OrchestratorPlanError("任务卡 context 必须是字符串")
    lines = [
        line.strip()
        for line in context.splitlines()
        if line.strip().startswith(CANDIDATE_IDS_PREFIX)
    ]
    if len(lines) != 1:
        raise OrchestratorPlanError(
            f"context 必须恰好包含一行 {CANDIDATE_IDS_PREFIX} 声明，实际 {len(lines)} 行"
        )
    raw = lines[0][len(CANDIDATE_IDS_PREFIX) :].strip()
    candidates = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not candidates:
        raise OrchestratorPlanError(f"{CANDIDATE_IDS_PREFIX} 声明不能为空")
    return candidates


def context_with_candidates(candidate_ids: list[str] | tuple[str, ...], *, note: str = "") -> str:
    """构造带候选范围声明的 context（评测/计划构造用；格式与解析端唯一对应）。"""
    header = f"{CANDIDATE_IDS_PREFIX} {','.join(candidate_ids)}"
    return f"{header}\n{note}".strip()


def payload_task_refs(payload: Mapping[str, object]) -> tuple[str, ...]:
    """从结构化载荷读取 `task_refs`（缺失/类型非法返回空并交由调用方判定）。"""
    refs = payload.get("task_refs")
    if not isinstance(refs, (list, tuple)):
        return ()
    return tuple(ref for ref in refs if isinstance(ref, str))

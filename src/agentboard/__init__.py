"""AgentBoard 核心包。

设计原则（对应改造需求）：
- 核心运行时零第三方依赖，单元测试不依赖真实模型/ACP/网络（需求11）。
- 主 Agent 只负责「提议」任务计划，不直接修改运行时状态（需求1）；
  状态变更统一由 board 状态机完成。
- 系统对任务数量/字段/Agent类型/依赖/DAG 结构做护栏校验（需求2）。
- 子 Agent 仅接收自己的任务包与显式上游结果（需求3）。
- Worker 返回结构化结果：事实/证据/风险/未解决问题/置信度（需求4/5）。
- 失败有限重试，超限进入 blocked/failed（需求6）。
- 每次状态变化产生结构化事件（需求7）。
- ACP / LangGraph 只作为 Adapter / 编排运行时，不耦合调度核心（需求9/10）。
"""

from .contracts import (
    BoardDecision,
    ProjectBrief,
    TaskCard,
    WorkerResult,
    WorkerTaskInput,
)
from .events import BoardEvent
from .evidence import EvidenceRef, is_trusted_evidence
from .validation import validate_model_board

__all__ = [
    "BoardDecision",
    "BoardEvent",
    "EvidenceRef",
    "ProjectBrief",
    "TaskCard",
    "WorkerResult",
    "WorkerTaskInput",
    "is_trusted_evidence",
    "validate_model_board",
]

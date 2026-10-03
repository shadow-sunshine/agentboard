"""多 Agent 编排器（P5）异常族。

与既有异常族的分工：
- `contracts` / `board` / `validation`：任务板护栏（ValueError 族，P0 语义不变）；
- `runtime.errors`：单运行时研究流水线（P3/P4）；
- 本族：**编排层自身**的失败（计划违反角色图、未知 worker、越权字段、
  过期卡片、验证拒绝、合成失败）。全部继承 `OrchestrationError`。

编排纪律：这些异常要么被 orchestrator 捕获并转成**可观测的运行结局**
（outcome / failure_reason / 事件），要么原样向上传播（配置错误类）——
任何一层都不允许静默吞掉或静默回退。
"""

from __future__ import annotations


class OrchestrationError(Exception):
    """P5 编排层异常基类。"""


class OrchestratorPlanError(OrchestrationError):
    """计划违反角色图 / 结构约束（未知依赖、synthesizer 越过 verifier 等）。

    这是**计划级 fail-closed**：整次运行在任何任务派发前终止。
    """


class UnknownWorkerError(OrchestrationError):
    """任务卡指派的 agent 在 WorkerRegistry 中不存在（fail-closed）。"""


class ContextBoundaryError(OrchestrationError):
    """越权字段 / 越权上下文引用：worker 产出的结构化载荷引用了
    其 depends_on 之外的任务（或引用形态非法）。fail-closed，按卡片失败处理。"""


class BoardInvariantError(OrchestrationError):
    """任务板不变量被破坏（结果与卡片不一致 / 过期卡片版本等）。
    由 board 层 ValueError 映射而来，按运行失败处理（不是重试路径）。"""


class VerifierRejectedError(OrchestrationError):
    """验证器拒绝：synthesizer 在上游看到了 verdict=rejected。
    不可信结论由此被挡在最终报告之外（通过任务板失败语义，不静默）。"""


class SynthesisError(OrchestrationError):
    """合成失败：synthesizer 没有任何可消费的验证通过结果。"""

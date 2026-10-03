"""确定性 Mock Worker（需求8/11）：不依赖真实模型、ACP 或网络。

用于单元测试与评测：可配置失败次数（驱动重试/失败状态机）与置信度，
断言子 Agent 收到的任务包内容（验证隔离），以及返回结构化结果。

P1A-1：证据改为结构化 `EvidenceRef`，统一由 `SyntheticEvidenceFactory` 构造
（不允许在本模块散落拼装 synthetic 证据）。
"""

from __future__ import annotations

from ..contracts import WorkerResult, WorkerTaskInput
from .synthetic import SyntheticEvidenceFactory


class MockWorker:
    def __init__(
        self,
        agent_name: str,
        *,
        fail_times: int = 0,
        confidence: float = 0.9,
    ) -> None:
        self.agent_name = agent_name
        self.fail_times = fail_times
        self.confidence = confidence
        self._calls = 0
        self.received_inputs: list[WorkerTaskInput] = []

    @property
    def identity(self) -> str:
        """决策器/Worker 身份（P5）：进编排 run_id；配置变 ⇒ 身份变。"""
        return (
            f"mock.v1:{self.agent_name}"
            f":fail_times={self.fail_times}:confidence={self.confidence}"
        )

    async def run(self, task_input: WorkerTaskInput) -> WorkerResult:
        self._calls += 1
        self.received_inputs.append(task_input)
        if self._calls <= self.fail_times:
            raise RuntimeError(f"mock worker {self.agent_name} 第 {self._calls} 次执行失败")

        goal = task_input.goal
        return WorkerResult(
            task_id=task_input.task_id,
            agent_name=self.agent_name,
            facts=(
                f"[{self.agent_name}] 关于「{goal}」的事实1",
                f"[{self.agent_name}] 关于「{goal}」的事实2",
            ),
            # locator 基于 task_id，与 _calls 无关：失败重试不会改变证据 id。
            evidence=SyntheticEvidenceFactory.for_task(
                self.agent_name, task_input.task_id, count=1
            ),
            risks=(f"[{self.agent_name}] {task_input.task_id} 的潜在风险点",),
            open_questions=(
                f"[{self.agent_name}] 仍需确认 {task_input.task_id} 的外部数据",
            ),
            confidence=self.confidence,
            summary=f"mock 结论: {goal}",
            session_id=f"mock-session-{self.agent_name}-{self._calls}",
        )

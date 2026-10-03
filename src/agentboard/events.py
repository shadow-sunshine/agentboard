"""结构化事件（需求7：每次状态变化都产生结构化事件）。

事件是不可变数据对象；调度核心在状态变化时产出事件列表，由上层
（未来 LangGraph 编排 / 流处理器）收集与广播。核心本身不持有全局总线，
保证可测试、可重放。

P0.5.1 强化：
- data 字段经 freeze_payload 递归冻结为只读映射，嵌套 list/dict 也不可被篡改
  （任何运行时写入都会被拒绝），从而保证事件可回放、整体不可变；
- event_type / task_id / agent / attempt / timestamp / data 在构造时做类型与范围校验，
  拒绝静默类型转换（attempt 禁止 bool、timestamp 禁止 bool、data 必须是 Mapping）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .util import freeze_payload, thaw_payload

# 事件类型常量
EVT_BOARD_CREATED = "board_created"
EVT_TASK_STARTED = "task_started"
EVT_TASK_DONE = "task_done"
EVT_TASK_RETRY = "task_retry"
EVT_TASK_FAILED = "task_failed"
EVT_BLOCKED = "blocked"
EVT_WORKER_DELTA = "worker_delta"
EVT_FINAL_REPORT_READY = "final_report_ready"
# P5 多 Agent 编排新增：计划拒绝 / 验证器结论
EVT_PLAN_REJECTED = "plan_rejected"
EVT_VERIFIER_PASSED = "verifier_passed"
EVT_VERIFIER_REJECTED = "verifier_rejected"


@dataclass(frozen=True)
class BoardEvent:
    event_type: str
    task_id: str = ""
    agent: str = ""
    attempt: int = 0
    data: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("BoardEvent.event_type 必须是非空字符串")
        if not isinstance(self.task_id, str):
            raise ValueError("BoardEvent.task_id 必须是字符串")
        if not isinstance(self.agent, str):
            raise ValueError("BoardEvent.agent 必须是字符串")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise ValueError("BoardEvent.attempt 必须是非负整数（禁止 bool）")
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, (int, float)):
            raise ValueError("BoardEvent.timestamp 必须是数值（禁止 bool）")
        if not isinstance(self.data, Mapping):
            raise ValueError("BoardEvent.data 必须是 Mapping")
        # 递归冻结：外层为只读映射，嵌套 list/dict 也不可被外部或 Worker 侧篡改
        object.__setattr__(self, "data", freeze_payload(dict(self.data)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event_type,
            "task_id": self.task_id,
            "agent": self.agent,
            "attempt": self.attempt,
            "data": thaw_payload(self.data),
            "timestamp": self.timestamp,
        }

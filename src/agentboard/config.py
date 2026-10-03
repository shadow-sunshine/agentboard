"""AgentBoard 护栏配置常量。

集中管理任务板校验所需的全部阈值与允许集合，便于测试注入不同配置
（需求2：对任务数量、字段、Agent 类型、依赖关系和 DAG 结构进行校验）。
"""

from __future__ import annotations

import re

# ---- 任务板护栏 ----
MIN_TASK_CARDS = 2
MAX_TASK_CARDS = 6

# 失败重试上限；达到后任务进入 failed（需求6）
MAX_ATTEMPTS = 2

# task_id 命名规范：lower_snake_case
TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

# 默认允许的 Agent 类型。
# 真实系统由 WorkerRegistry 注入实际 adapter；校验层只认这个白名单。
DEFAULT_AGENT_TYPES: frozenset[str] = frozenset({"research", "planner"})

# ---- 任务状态机常量 ----
STATUS_TODO = "todo"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

# 合法状态集合（契约层反序列化/构造边界校验用，P0.5 强化）
VALID_STATUSES: frozenset[str] = frozenset(
    {STATUS_TODO, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_BLOCKED}
)

# 合法调度决策动作（BoardDecision 边界校验用，P0.5.1 强化）
VALID_DECISION_ACTIONS: frozenset[str] = frozenset(
    {"dispatch", "retry", "finish", "blocked"}
)

# ---- WorkerResult.structured_payload 的 schema 标识（P1A-1） ----
# 形态 `<namespace>.v<major>`，例如 research.findings.v1。
# 全项目不使用 `@`：让 snapshot_version / schema 等所有标识字段共用一套字符集
# [a-z0-9._-]，从根上避免第二处同类冲突（见 P1A_PLAN.md §1）。
PAYLOAD_SCHEMA_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*\.v[1-9]\d*$")

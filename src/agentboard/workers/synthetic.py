"""合成证据（synthetic evidence）的唯一构造入口（P1A-1）。

三条硬规则，每一条都由测试钉死：

1. **唯一入口**：`SyntheticEvidenceFactory` 是唯一允许创建
   `source_type="synthetic_mock"` 的地方。其它模块不得自行拼装 synthetic `EvidenceRef`
   ——`tests/test_static_guards.py` 用 AST 扫描整个 `src/agentboard/**` 来强制这一点。
2. **不读时钟**：`captured_at` 使用固定常量 `FIXED_CAPTURED_AT`。若改用"现在"，
   同一任务两次运行会得到不同 `evidence_id`，"重复运行一致性"当场失效。
3. **locator 基于 task_id 而非调用计数**：`/synthetic/{task_id}/{index}`。
   若改用 `MockWorker._calls`，注入失败重试会让计数偏移，同一任务在不同重试路径下
   就会产出不同的证据 id —— 这是"看起来能过、一加失败注入就崩"的典型坑。
"""

from __future__ import annotations

from ..evidence import (
    MAX_EXCERPT_CHARS,
    SOURCE_SYNTHETIC,
    SYNTHETIC_SNAPSHOT_VERSION,
    EvidenceRef,
)

# 固定拍摄日期：代表"合成资料快照"的版本日期，不是真实时间。
FIXED_CAPTURED_AT = "2026-01-01"


def _require_nonempty_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串，实际为 {value!r}")
    return value


def _require_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"index 必须是非负整数（禁止 bool），实际为 {value!r}")
    return value


class SyntheticEvidenceFactory:
    """确定性合成证据工厂。全部方法均为纯函数：同输入 -> 同输出。"""

    @staticmethod
    def make(agent_name: str, task_id: str, index: int) -> EvidenceRef:
        owner = _require_nonempty_str(agent_name, "agent_name")
        task = _require_nonempty_str(task_id, "task_id")
        slot = _require_index(index)

        excerpt = f"[{owner}] {task} 合成证据 {slot}"
        if len(excerpt) > MAX_EXCERPT_CHARS:
            raise ValueError(
                f"合成证据摘录超过 {MAX_EXCERPT_CHARS} 字符，请缩短 agent_name / task_id"
            )
        return EvidenceRef(
            source_type=SOURCE_SYNTHETIC,
            source_id=f"mock:{owner}",
            snapshot_version=SYNTHETIC_SNAPSHOT_VERSION,
            locator=f"/synthetic/{task}/{slot}",
            excerpt=excerpt,
            captured_at=FIXED_CAPTURED_AT,
        )

    @classmethod
    def for_task(cls, agent_name: str, task_id: str, *, count: int = 1) -> tuple[EvidenceRef, ...]:
        """为一张任务卡生成 count 条合成证据，按 index 升序（顺序稳定）。"""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"count 必须是非负整数（禁止 bool），实际为 {count!r}")
        return tuple(cls.make(agent_name, task_id, index) for index in range(count))

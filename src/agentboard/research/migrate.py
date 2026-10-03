"""历史自由文本证据的显式迁移器（P1A-1）。

**为什么需要它**：P1A-1 把 `WorkerResult.evidence` 从 `tuple[str, ...]` 升级为
`tuple[EvidenceRef, ...]`，并且刻意**不在契约层做隐式兼容** —— `WorkerResult.from_dict`
遇到字符串 evidence 一律 `ValueError`。这样"证据只能是结构化对象"这条不变量是静态成立的，
不依赖调用方自觉。

但 P0/P0.5 时期的历史产物（`.demo_runs/`、事件回放数据）里确实是字符串证据，需要可读。
所以兼容逻辑被隔离到**这里**，由调用方显式触发。

**迁移是有损标注，不是洗白**：
- 迁移出的证据 `source_type == "legacy_text"`，`snapshot_version == "legacy.0"`，
  `captured_at == "1970-01-01"`（明确表示"没有真实拍摄日期"）；
- `is_trusted_evidence()` 对 legacy 证据返回 `False`，领域层不得用它支撑推荐结论；
- `locator` 用 `/legacy/{task_id}/{index}` 记录它在原列表中的位置，便于人工回溯。
"""

from __future__ import annotations

from typing import Any

from ..evidence import (
    LEGACY_SNAPSHOT_VERSION,
    SOURCE_LEGACY,
    EvidenceRef,
)
from .snapshot import expected_excerpt_for

# 历史文本没有真实拍摄日期，用常量显式标注"未知"，而不是拿 now() 冒充。
LEGACY_CAPTURED_AT = "1970-01-01"


class LegacyMigrationError(ValueError):
    """无法迁移的 legacy 输入（形态不支持 / 归属不明）。"""


def _legacy_ref(*, task_id: str, index: int, text: str) -> EvidenceRef:
    # 截断走 `expected_excerpt_for`（全仓唯一实现点，P2.2-2 收敛）——
    # legacy 自由文本与 fixture section 用**同一条**规范截断，这里曾是 4 份重复实现之一。
    excerpt = expected_excerpt_for(text)
    if not excerpt.strip():
        raise LegacyMigrationError(f"evidence[{index}] 是空白字符串，无法迁移为证据")
    return EvidenceRef(
        source_type=SOURCE_LEGACY,
        source_id=f"legacy:{task_id}",
        snapshot_version=LEGACY_SNAPSHOT_VERSION,
        locator=f"/legacy/{task_id}/{index}",
        excerpt=excerpt,
        captured_at=LEGACY_CAPTURED_AT,
    )


def migrate_legacy_evidence(raw_evidence: object, *, task_id: str) -> tuple[EvidenceRef, ...]:
    """把旧格式 evidence 迁移为结构化证据。

    - `str`   -> `legacy_text`（有损降级）
    - `dict`  -> 按 `EvidenceRef.from_dict` 严格校验（已是新格式则原样收下）
    - `EvidenceRef` -> 原样保留
    - 其它类型 -> `LegacyMigrationError`（拒绝静默丢弃，也拒绝静默转成字符串）

    `task_id` 必须显式传入 —— 迁移器不猜归属。
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise LegacyMigrationError("migrate_legacy_evidence 需要显式传入非空 task_id")
    if raw_evidence is None:
        return ()
    if not isinstance(raw_evidence, (list, tuple)):
        raise LegacyMigrationError(
            f"evidence 必须是 list 或 tuple，实际为 {type(raw_evidence).__name__}"
        )

    migrated: list[EvidenceRef] = []
    for index, item in enumerate(raw_evidence):
        if isinstance(item, EvidenceRef):
            migrated.append(item)
        elif isinstance(item, dict):
            migrated.append(EvidenceRef.from_dict(item))
        elif isinstance(item, str):
            migrated.append(_legacy_ref(task_id=task_id, index=index, text=item))
        else:
            raise LegacyMigrationError(
                f"evidence[{index}] 类型不支持：{type(item).__name__}（只接受 str / dict / EvidenceRef）"
            )
    return tuple(migrated)


def migrate_legacy_result(raw: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
    """迁移一条历史 `WorkerResult` 字典，返回新 dict（**不修改入参**）。

    产物可直接交给严格的 `WorkerResult.from_dict`。
    典型用法：`WorkerResult.from_dict(migrate_legacy_result(legacy_json))`。
    """
    if not isinstance(raw, dict):
        raise LegacyMigrationError("migrate_legacy_result 需要 dict")
    owner = task_id if task_id is not None else raw.get("task_id")
    if not isinstance(owner, str) or not owner.strip():
        raise LegacyMigrationError(
            "无法确定 task_id：请显式传入 task_id，或保证 raw['task_id'] 存在且非空"
        )
    migrated = dict(raw)
    migrated["evidence"] = [
        item.to_dict() for item in migrate_legacy_evidence(raw.get("evidence"), task_id=owner)
    ]
    return migrated

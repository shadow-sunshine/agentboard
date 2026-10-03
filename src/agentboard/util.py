"""纯文本与结构工具，无外部依赖。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


def compact_text(text: str, limit: int = 1600) -> str:
    """折叠空行并截断，用于把长文本压进任务包/上下文。

    保留首尾，中间以标记截断；不改变语义，只控制长度。
    """
    normalized = "\n".join(
        line.rstrip() for line in text.strip().splitlines() if line.strip()
    )
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "\n...(已由系统截断)"


def freeze_payload(value: Any) -> Any:
    """递归冻结 JSON-like 结构（P0.5.1 强化：真正不可变，并非仅顶层只读）。

    - Mapping / dict   -> MappingProxyType（只读映射）
    - list / tuple     -> tuple（不可变序列）
    - set / frozenset  -> frozenset
    - 标量(bool/int/float/str/None) -> 原值

    返回值是只读且可哈希的，配合 @dataclass(frozen=True) 可保证事件/契约整体不可变。
    嵌套的 list/dict 因此也不可被外部或 Worker 侧篡改，满足隔离与事件可回放需要。
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze_payload(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_payload(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_payload(v) for v in value)
    return value


def thaw_payload(value: Any) -> Any:
    """递归解冻（P0.5.1）：把冻结结构还原为普通可 JSON 序列化的 Python 结构。

    - MappingProxyType -> dict
    - tuple           -> list
    - frozenset       -> list（集合无序，列表便于序列化且确定性）

    用于 to_dict() / 持久化 / 事件回放，保证产出可被 json.dumps 正常序列化。
    """
    if isinstance(value, Mapping):
        return {k: thaw_payload(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw_payload(v) for v in value]
    if isinstance(value, frozenset):
        return [thaw_payload(v) for v in value]
    return value

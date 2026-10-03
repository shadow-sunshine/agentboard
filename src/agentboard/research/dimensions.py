"""维度注册表（P1A-2）：加载、别名归并、权重配平校验。

**为什么不做权重自动归一化**：如果加载时把权重统一除以总和，那么"少配了一个维度"
这个错误会被悄悄摊到其它维度上（剩下几个维度被等比放大），报告看起来仍然"正常"。
P1A 选择严格口径 —— 权重必须显式配平到 1.0，配不平就报错，让配置错误当场暴露。

别名归并只做**一层**映射：alias -> canonical dimension_id，不做链式解析
（a -> b -> c 这种会让配置难以推理，直接禁止 alias 撞上任何 dimension_id）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..util import freeze_payload
from .config import DIMENSIONS_SCHEMA, WEIGHT_SUM_TOLERANCE
from .errors import (
    DimensionError,
    DimensionWeightError,
    ResearchValueError,
    SnapshotSchemaError,
    UnknownDimensionError,
)
from .models import ComparisonDimension
from .snapshot import read_json_object


@dataclass(frozen=True)
class DimensionRegistry:
    """全部可比维度 + `任意可解析名 -> canonical dimension_id` 的索引。"""

    dimensions: tuple[ComparisonDimension, ...]
    alias_index: Mapping[str, str]

    def ids(self) -> tuple[str, ...]:
        """canonical dimension_id，按字典序（顺序稳定，可直接用于输出排序）。"""
        return tuple(dimension.dimension_id for dimension in self.dimensions)

    def has(self, name: str) -> bool:
        return isinstance(name, str) and name in self.alias_index

    def resolve(self, name: str) -> str:
        """把 dimension_id 或 alias 解析为 canonical id；未知即报错（不猜、不近似匹配）。"""
        if not isinstance(name, str) or name not in self.alias_index:
            raise UnknownDimensionError(
                f"未知维度 {name!r}；已知维度：{list(self.ids())}"
            )
        return self.alias_index[name]

    def get(self, name: str) -> ComparisonDimension:
        canonical = self.resolve(name)
        for dimension in self.dimensions:
            if dimension.dimension_id == canonical:
                return dimension
        raise UnknownDimensionError(f"注册表内部不一致：解析出 {canonical!r} 但没有对应维度")

    @property
    def total_weight(self) -> float:
        return sum(dimension.weight for dimension in self.dimensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": [dimension.to_dict() for dimension in self.dimensions],
            "alias_index": dict(self.alias_index),
        }


def build_registry(dimensions: Sequence[ComparisonDimension]) -> DimensionRegistry:
    """由一组维度构造注册表，并执行跨维度的完整性校验。"""
    items = tuple(dimensions)
    if not items:
        raise ResearchValueError("维度注册表不能为空")

    by_id: dict[str, ComparisonDimension] = {}
    for dimension in items:
        if not isinstance(dimension, ComparisonDimension):
            raise ResearchValueError(
                f"注册表元素必须是 ComparisonDimension，实际为 {type(dimension).__name__}"
            )
        if dimension.dimension_id in by_id:
            raise DimensionError(f"重复 dimension_id：{dimension.dimension_id!r}")
        by_id[dimension.dimension_id] = dimension

    ordered = tuple(sorted(items, key=lambda dimension: dimension.dimension_id))

    alias_index: dict[str, str] = {}
    for dimension in ordered:
        alias_index[dimension.dimension_id] = dimension.dimension_id
    for dimension in ordered:
        for alias in dimension.aliases:
            if alias in alias_index:
                raise DimensionError(
                    f"alias {alias!r}（属于 {dimension.dimension_id!r}）与已有的维度名 / alias 冲突"
                )
            alias_index[alias] = dimension.dimension_id

    total = sum(dimension.weight for dimension in ordered)
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise DimensionWeightError(
            f"维度权重之和必须为 1.0（容差 {WEIGHT_SUM_TOLERANCE}），实际为 {total!r}；"
            "P1A 不做自动归一化，请显式配平配置"
        )

    return DimensionRegistry(dimensions=ordered, alias_index=freeze_payload(alias_index))


def load_dimension_registry(path: Path) -> DimensionRegistry:
    """从本地 JSON 加载维度注册表。"""
    raw = read_json_object(path)
    actual = raw.get("schema")
    if actual != DIMENSIONS_SCHEMA:
        raise SnapshotSchemaError(
            f"{path.name} 的 schema 期望 {DIMENSIONS_SCHEMA!r}，实际为 {actual!r}"
        )
    raw_dimensions = raw.get("dimensions")
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise SnapshotSchemaError(f"{path.name}.dimensions 必须是非空列表")
    dimensions: list[ComparisonDimension] = []
    for index, item in enumerate(raw_dimensions):
        if not isinstance(item, dict):
            raise SnapshotSchemaError(f"{path.name}.dimensions[{index}] 必须是 JSON 对象")
        dimensions.append(ComparisonDimension.from_dict(item))
    return build_registry(dimensions)

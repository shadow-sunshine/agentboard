"""维度模型与注册表（P1A-2）。

重点：P1A 只支持 ordinal / boolean，numeric 必须被拒绝；权重必须显式配平到 1.0
（不自动归一化）；alias 归并只做一层。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentboard.research.config import BOOLEAN_SCALE_LABELS
from agentboard.research.dimensions import (
    DimensionRegistry,
    build_registry,
    load_dimension_registry,
)
from agentboard.research.errors import (
    DegenerateDimensionError,
    DimensionError,
    DimensionWeightError,
    ResearchValueError,
    SnapshotSchemaError,
    UnknownDimensionError,
)
from agentboard.research.models import ComparisonDimension

ORDINAL_DICT: dict[str, Any] = {
    "dimension_id": "deployment_complexity",
    "name": "部署复杂度",
    "description": "从零到可用需要多少步骤。",
    "weight": 1.0,
    "scale_kind": "ordinal",
    "scale_labels": ["low", "high"],
    "utility_map": {"low": 1.0, "high": 0.0},
    "aliases": [],
}

BOOLEAN_DICT: dict[str, Any] = {
    "dimension_id": "managed_service_needed",
    "name": "是否依赖托管服务",
    "description": "false 表示可完全自托管。",
    "weight": 1.0,
    "scale_kind": "boolean",
    "scale_labels": ["false", "true"],
    "utility_map": {"false": 1.0, "true": 0.0},
    "aliases": [],
}


def _dim(overrides: dict[str, Any] | None = None) -> ComparisonDimension:
    payload = dict(BOOLEAN_DICT if (overrides or {}).get("scale_kind") == "boolean" else ORDINAL_DICT)
    payload.update(overrides or {})
    return ComparisonDimension.from_dict(payload)


# ---- 真实注册表 ----


def test_real_registry_loads(registry: DimensionRegistry) -> None:
    assert registry.ids() == tuple(sorted(registry.ids()))
    assert len(registry.ids()) == 6
    assert abs(registry.total_weight - 1.0) < 1e-9


def test_real_registry_has_ordinal_and_boolean(registry: DimensionRegistry) -> None:
    kinds = {registry.get(dim_id).scale_kind for dim_id in registry.ids()}
    assert kinds == {"ordinal", "boolean"}


# ---- scale_kind 闭集：numeric 必须被拒绝 ----


@pytest.mark.parametrize("bad_kind", ["numeric", "integer", "float", "ORDINAL", "", "ordinal "])
def test_unsupported_scale_kind_is_rejected(bad_kind: str) -> None:
    with pytest.raises(ResearchValueError, match="scale_kind"):
        _dim({"scale_kind": bad_kind})


def test_numeric_kind_error_message_mentions_numeric() -> None:
    """numeric 是"显式延期"而不是"未知值"，报错里要能看出这一点。"""
    with pytest.raises(ResearchValueError, match="numeric"):
        _dim({"scale_kind": "numeric", "scale_labels": ["1", "2"], "utility_map": {"1": 0.0, "2": 1.0}})


# ---- ordinal / boolean 各自的量表约束 ----


def test_ordinal_requires_at_least_two_labels() -> None:
    with pytest.raises(ResearchValueError, match="至少"):
        _dim({"scale_labels": ["only"], "utility_map": {"only": 1.0}})


def test_ordinal_rejects_duplicate_labels() -> None:
    with pytest.raises(ResearchValueError, match="重复"):
        _dim({"scale_labels": ["low", "low"], "utility_map": {"low": 1.0}})


def test_boolean_labels_are_fixed() -> None:
    with pytest.raises(ResearchValueError, match="boolean"):
        _dim(
            {
                "scale_kind": "boolean",
                "scale_labels": ["no", "yes"],
                "utility_map": {"no": 1.0, "yes": 0.0},
            }
        )


def test_boolean_labels_accepted() -> None:
    dimension = _dim({"scale_kind": "boolean"})
    assert dimension.scale_labels == BOOLEAN_SCALE_LABELS
    assert dimension.utility_of("false") == 1.0
    assert dimension.utility_of("true") == 0.0


def test_ordinal_labels_accepted() -> None:
    dimension = _dim()
    assert dimension.scale_kind == "ordinal"
    assert dimension.utility_of("low") == 1.0


# ---- utility_map ----


def test_utility_map_keys_must_match_labels_exactly() -> None:
    with pytest.raises(ResearchValueError, match="utility_map 的键"):
        _dim({"utility_map": {"low": 1.0, "high": 0.0, "extra": 0.5}})
    with pytest.raises(ResearchValueError, match="utility_map 的键"):
        _dim({"utility_map": {"low": 1.0}})


def test_utility_map_empty_rejected() -> None:
    with pytest.raises(ResearchValueError, match="utility_map"):
        _dim({"utility_map": {}})


@pytest.mark.parametrize("bad", [-0.1, 1.1, "1.0", True, None])
def test_utility_values_must_be_unit_floats(bad: object) -> None:
    with pytest.raises(ResearchValueError):
        _dim({"utility_map": {"low": bad, "high": 0.0}})


def test_degenerate_utility_map_rejected() -> None:
    """取值全相同的维度没有区分度，无法用于打分。"""
    with pytest.raises(DegenerateDimensionError):
        _dim({"utility_map": {"low": 0.5, "high": 0.5}})


def test_utility_of_unknown_label_rejected() -> None:
    with pytest.raises(DimensionError, match="没有取值"):
        _dim().utility_of("medium")


def test_utility_spread_reported() -> None:
    assert _dim().utility_spread == 2
    assert _dim(
        {"scale_labels": ["a", "b", "c"], "utility_map": {"a": 0.0, "b": 0.5, "c": 1.0}}
    ).utility_spread == 3


# ---- weight ----


@pytest.mark.parametrize("bad_weight", [0.0, -0.5, 1.5, "0.5", True])
def test_weight_must_be_in_unit_open_interval(bad_weight: object) -> None:
    with pytest.raises(ResearchValueError, match="weight"):
        _dim({"weight": bad_weight})


def test_registry_requires_unit_weight_sum() -> None:
    first = _dim({"weight": 0.5})
    second = _dim(
        {
            "dimension_id": "ops_skill_floor",
            "name": "运维门槛",
            "scale_labels": ["low", "high"],
            "utility_map": {"low": 1.0, "high": 0.0},
            "weight": 0.4,
        }
    )
    with pytest.raises(DimensionWeightError, match="1.0"):
        build_registry([first, second])


def test_registry_accepts_unit_weight_sum() -> None:
    first = _dim({"weight": 0.5})
    second = _dim(
        {
            "dimension_id": "ops_skill_floor",
            "name": "运维门槛",
            "scale_labels": ["low", "high"],
            "utility_map": {"low": 1.0, "high": 0.0},
            "weight": 0.5,
        }
    )
    assert abs(build_registry([first, second]).total_weight - 1.0) < 1e-9


# ---- alias 归并 ----


def test_alias_resolves_to_canonical(registry: DimensionRegistry) -> None:
    assert registry.resolve("deploy_complexity") == "deployment_complexity"
    assert registry.resolve("ops_effort") == "ops_skill_floor"
    assert registry.resolve("deployment_complexity") == "deployment_complexity"
    assert registry.get("deploy_complexity").dimension_id == "deployment_complexity"


def test_unknown_dimension_raises(registry: DimensionRegistry) -> None:
    with pytest.raises(UnknownDimensionError, match="未知维度"):
        registry.resolve("cost_per_gb")
    assert registry.has("cost_per_gb") is False
    assert registry.has("deploy_complexity") is True


def test_alias_colliding_with_dimension_id_rejected() -> None:
    first = _dim({"aliases": ("ops_skill_floor",)})
    second = _dim(
        {
            "dimension_id": "ops_skill_floor",
            "name": "运维门槛",
            "scale_labels": ["low", "high"],
            "utility_map": {"low": 1.0, "high": 0.0},
            "weight": 1.0,
        }
    )
    with pytest.raises(DimensionError, match="冲突"):
        build_registry([first, second])


def test_alias_equal_to_self_rejected() -> None:
    with pytest.raises(ResearchValueError, match="不能等于"):
        _dim({"aliases": ("deployment_complexity",)})


def test_duplicate_alias_across_dimensions_rejected() -> None:
    first = _dim({"weight": 0.5, "aliases": ("shared_name",)})
    second = _dim(
        {
            "dimension_id": "ops_skill_floor",
            "name": "运维门槛",
            "scale_labels": ["low", "high"],
            "utility_map": {"low": 1.0, "high": 0.0},
            "weight": 0.5,
            "aliases": ("shared_name",),
        }
    )
    with pytest.raises(DimensionError, match="冲突"):
        build_registry([first, second])


def test_duplicate_dimension_id_rejected() -> None:
    with pytest.raises(DimensionError, match="重复 dimension_id"):
        build_registry([_dim({"weight": 0.5}), _dim({"weight": 0.5})])


def test_empty_registry_rejected() -> None:
    with pytest.raises(ResearchValueError, match="不能为空"):
        build_registry([])


# ---- 序列化与不可变 ----


def test_dimension_round_trip() -> None:
    dimension = _dim()
    restored = ComparisonDimension.from_dict(dimension.to_dict())
    assert restored == dimension
    assert restored.utility_map["low"] == 1.0


def test_dimension_from_dict_requires_mapping() -> None:
    with pytest.raises(ResearchValueError, match="Mapping"):
        ComparisonDimension.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_dimension_is_frozen() -> None:
    dimension = _dim()
    with pytest.raises((AttributeError, TypeError)):
        dimension.weight = 0.1  # type: ignore[misc]


def test_dimension_missing_field_rejected() -> None:
    payload = dict(ORDINAL_DICT)
    payload.pop("scale_kind")
    with pytest.raises(ResearchValueError, match="scale_kind"):
        ComparisonDimension.from_dict(payload)


# ---- 注册表文件加载 ----


def test_load_real_registry_file(fixture_root, catalog) -> None:
    loaded = load_dimension_registry(fixture_root / catalog.dimension_registry_path)
    assert loaded.ids() == (
        "deployment_complexity",
        "ecosystem_maturity",
        "managed_service_needed",
        "ops_skill_floor",
        "query_latency",
        "scale_ceiling",
    )


def test_load_registry_rejects_wrong_schema(cloned_fixture_root) -> None:
    path = cloned_fixture_root / "dimensions" / "dimensions.v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = "research.dimensions.v2"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SnapshotSchemaError, match="schema"):
        load_dimension_registry(path)

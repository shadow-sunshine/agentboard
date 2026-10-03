"""最小研究领域模型（P1A-2）：`ComparisonDimension` 与 `ResearchFinding`。

只做**局部校验**（单对象自身），不校验跨对象引用 —— 注册表归属、`value` 是否命中
量表标签、`task_id` 是否属于本 board，都属于上下文校验，分别由 `dimensions.py`
与 P1A-3 的组装阶段负责（见 P1A_PLAN.md §8 的 L0/L1/L2 分层）。

量表支持范围：**只有 ordinal 与 boolean**。`numeric` 在打分语义（归一化区间、方向）
定义清楚之前显式禁用，传进来即报错。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import ProjectBrief
from ..evidence import EvidenceRef, is_trusted_evidence
from ..util import freeze_payload, thaw_payload
from .config import (
    BOOLEAN_SCALE_LABELS,
    CANDIDATE_ID_PATTERN,
    DIMENSION_ID_PATTERN,
    FINDING_ID_PATTERN,
    MAX_CLAIM_CHARS,
    MIN_ORDINAL_LABELS,
    MIN_UTILITY_SPREAD,
    ORIGIN_RULE_BASED,
    REPORT_GENERATED_BY,
    REPORT_ID_PATTERN,
    REPORT_SCHEMA_VERSION,
    RISK_ID_PATTERN,
    RULE_IDS,
    SCALE_BOOLEAN,
    SEVERITY_ORDER,
    STATUS_SUPPORTED,
    VALID_FINDING_STATUSES,
    VALID_RISK_ORIGINS,
    VALID_SCALE_KINDS,
    VALID_SEVERITIES,
)
from .errors import (
    DegenerateDimensionError,
    DimensionError,
    ResearchValueError,
)
from .integrity import IntegrityReport


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ResearchValueError(f"{label} 必须是字符串，实际为 {type(value).__name__}")
    return value


def _require_str_nonempty(value: Any, label: str) -> str:
    text = _require_str(value, label)
    if not text.strip():
        raise ResearchValueError(f"{label} 不能是空字符串")
    return text


def _require_id(value: Any, pattern: Any, label: str, hint: str) -> str:
    text = _require_str_nonempty(value, label)
    if not pattern.match(text):
        raise ResearchValueError(f"{label} 必须满足 {hint}，实际为 {text!r}")
    return text


def _require_unit_float(value: Any, label: str, *, lower_exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchValueError(f"{label} 必须是数值（禁止 bool），实际为 {value!r}")
    number = float(value)
    if lower_exclusive:
        if not (0.0 < number <= 1.0):
            raise ResearchValueError(f"{label} 必须落在 (0, 1] 区间，实际为 {number!r}")
    elif not (0.0 <= number <= 1.0):
        raise ResearchValueError(f"{label} 必须落在 [0, 1] 区间，实际为 {number!r}")
    return number


@dataclass(frozen=True)
class ComparisonDimension:
    """一个可比较的评估维度。

    `utility_map` 是唯一的**方向**来源：语义写死为「utility 越大越符合选型目标」。
    因此不再需要 `higher_is_better` 之类的翻转开关 —— 两套方向语义并存等于两个真源。
    """

    dimension_id: str
    name: str
    description: str
    weight: float
    scale_kind: str
    scale_labels: tuple[str, ...]
    utility_map: Mapping[str, float]
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id(
            self.dimension_id, DIMENSION_ID_PATTERN, "ComparisonDimension.dimension_id", "lower_snake_case"
        )
        _require_str_nonempty(self.name, "ComparisonDimension.name")
        _require_str_nonempty(self.description, "ComparisonDimension.description")
        weight = _require_unit_float(self.weight, "ComparisonDimension.weight", lower_exclusive=True)
        object.__setattr__(self, "weight", weight)

        if not isinstance(self.scale_kind, str) or self.scale_kind not in VALID_SCALE_KINDS:
            raise ResearchValueError(
                f"ComparisonDimension.scale_kind 必须是 {sorted(VALID_SCALE_KINDS)}；"
                f"numeric 在打分语义定义清楚前显式禁用。实际为 {self.scale_kind!r}"
            )

        labels = self._validate_scale_labels()
        self._validate_utility_map(labels)
        self._validate_aliases()

    def _validate_scale_labels(self) -> tuple[str, ...]:
        labels = self.scale_labels
        if not isinstance(labels, tuple):
            raise ResearchValueError("ComparisonDimension.scale_labels 必须是 tuple[str, ...]")
        for label in labels:
            if not isinstance(label, str) or not label.strip():
                raise ResearchValueError("ComparisonDimension.scale_labels 的元素必须是非空字符串")
        if self.scale_kind == SCALE_BOOLEAN:
            if labels != BOOLEAN_SCALE_LABELS:
                raise ResearchValueError(
                    f"boolean 量表的 scale_labels 必须恰好是 {list(BOOLEAN_SCALE_LABELS)}，"
                    f"实际为 {list(labels)}"
                )
        else:
            if len(labels) < MIN_ORDINAL_LABELS:
                raise ResearchValueError(
                    f"ordinal 量表至少需要 {MIN_ORDINAL_LABELS} 个标签，实际为 {len(labels)}"
                )
            if len(set(labels)) != len(labels):
                raise ResearchValueError(f"ordinal 量表标签不能重复：{list(labels)}")
        return labels

    def _validate_utility_map(self, labels: tuple[str, ...]) -> None:
        utilities = self.utility_map
        if not isinstance(utilities, Mapping):
            raise ResearchValueError("ComparisonDimension.utility_map 必须是 Mapping")
        if not utilities:
            raise ResearchValueError("ComparisonDimension.utility_map 不能为空")
        if set(utilities) != set(labels):
            raise ResearchValueError(
                f"utility_map 的键必须与 scale_labels 完全一致；"
                f"多出 {sorted(set(utilities) - set(labels))}，缺少 {sorted(set(labels) - set(utilities))}"
            )
        cleaned: dict[str, float] = {}
        for label, utility in utilities.items():
            cleaned[label] = _require_unit_float(utility, f"utility_map[{label!r}]")
        if len(set(cleaned.values())) < MIN_UTILITY_SPREAD:
            raise DegenerateDimensionError(
                f"维度 {self.dimension_id!r} 的 utility_map 取值全相同，没有区分度"
            )
        object.__setattr__(self, "utility_map", freeze_payload(cleaned))

    def _validate_aliases(self) -> None:
        aliases = self.aliases
        if not isinstance(aliases, tuple):
            raise ResearchValueError("ComparisonDimension.aliases 必须是 tuple[str, ...]")
        seen: set[str] = set()
        for alias in aliases:
            _require_id(alias, DIMENSION_ID_PATTERN, "ComparisonDimension.aliases 元素", "lower_snake_case")
            if alias == self.dimension_id:
                raise ResearchValueError(f"alias 不能等于 dimension_id 自身：{alias!r}")
            if alias in seen:
                raise ResearchValueError(f"aliases 含重复项：{alias!r}")
            seen.add(alias)

    @property
    def utility_spread(self) -> int:
        """utility_map 的取值个数（区分度指标，供上层做质量检查）。"""
        return len(set(self.utility_map.values()))

    def utility_of(self, label: str) -> float:
        """取某标签的效用。标签不合法即报错 —— 不做默认值兜底。"""
        if label not in self.utility_map:
            raise DimensionError(
                f"{self.dimension_id!r} 的量表里没有取值 {label!r}，合法取值：{list(self.scale_labels)}"
            )
        return float(self.utility_map[label])

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension_id": self.dimension_id,
            "name": self.name,
            "description": self.description,
            "weight": self.weight,
            "scale_kind": self.scale_kind,
            "scale_labels": list(self.scale_labels),
            "utility_map": thaw_payload(self.utility_map),
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ComparisonDimension:
        """接受 `Mapping`：payload 经核心冻结后嵌套结构是 `MappingProxyType`。"""
        if not isinstance(raw, Mapping):
            raise ResearchValueError("ComparisonDimension.from_dict 需要 Mapping")
        labels = raw.get("scale_labels", [])
        if not isinstance(labels, (list, tuple)):
            raise ResearchValueError("ComparisonDimension.scale_labels 必须是 list/tuple")
        utilities = raw.get("utility_map", {})
        if not isinstance(utilities, Mapping):
            raise ResearchValueError("ComparisonDimension.utility_map 必须是 Mapping")
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, (list, tuple)):
            raise ResearchValueError("ComparisonDimension.aliases 必须是 list/tuple")
        return cls(
            dimension_id=raw.get("dimension_id"),
            name=raw.get("name"),
            description=raw.get("description"),
            weight=raw.get("weight"),
            scale_kind=raw.get("scale_kind"),
            scale_labels=tuple(labels),
            utility_map=utilities,
            aliases=tuple(aliases),
        )


def compute_finding_id(
    *,
    candidate_id: str,
    dimension_id: str,
    status: str,
    claim: str,
    evidence_ids: tuple[str, ...] | list[str],
) -> str:
    """内容寻址生成 finding_id。

    哈希输入含 `status`：同一条 claim 在"有依据"与"无依据"两种状态下必然得到不同 id，
    避免两条语义相反的记录撞成同一个标识。
    """
    material = "|".join(
        (candidate_id, dimension_id, status, claim, ",".join(sorted(evidence_ids)))
    )
    return "rf_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ResearchFinding:
    """一个候选在某个维度上的一条研究结论。

    不变量（局部校验）：
    - `status == "supported"` ⟺ `evidence_refs` 至少一条；
    - `status == "unsupported"` ⟹ `evidence_refs` 恰为空；
    - `evidence_refs` 内部 `evidence_id` 不重复。

    刻意**不**在构造期校验 `dimension_id` 是否属于注册表、`value` 是否命中量表标签
    ——那需要注册表，属于上下文校验。`value` 的合法性只在 `status == "supported"`
    时才有意义（unsupported 用 `UNSUPPORTED_VALUE` 占位）。
    """

    candidate_id: str
    task_id: str
    agent_name: str
    dimension_id: str
    claim: str
    value: str
    status: str
    evidence_refs: tuple[EvidenceRef, ...] = ()
    confidence: float = 1.0
    finding_id: str = ""

    def __post_init__(self) -> None:
        _require_id(
            self.candidate_id, CANDIDATE_ID_PATTERN, "ResearchFinding.candidate_id", "lower_snake_case"
        )
        _require_str_nonempty(self.task_id, "ResearchFinding.task_id")
        _require_str_nonempty(self.agent_name, "ResearchFinding.agent_name")
        _require_id(
            self.dimension_id, DIMENSION_ID_PATTERN, "ResearchFinding.dimension_id", "lower_snake_case"
        )

        claim = _require_str_nonempty(self.claim, "ResearchFinding.claim")
        if len(claim) > MAX_CLAIM_CHARS:
            raise ResearchValueError(
                f"ResearchFinding.claim 不能超过 {MAX_CLAIM_CHARS} 字符，实际 {len(claim)}"
            )
        _require_str_nonempty(self.value, "ResearchFinding.value")

        if not isinstance(self.status, str) or self.status not in VALID_FINDING_STATUSES:
            raise ResearchValueError(
                f"ResearchFinding.status 必须是 {sorted(VALID_FINDING_STATUSES)}，实际为 {self.status!r}"
            )

        refs = self.evidence_refs
        if not isinstance(refs, tuple):
            raise ResearchValueError("ResearchFinding.evidence_refs 必须是 tuple[EvidenceRef, ...]")
        seen: set[str] = set()
        for ref in refs:
            if not isinstance(ref, EvidenceRef):
                raise ResearchValueError(
                    f"ResearchFinding.evidence_refs 的元素必须是 EvidenceRef，实际为 {type(ref).__name__}"
                )
            if ref.evidence_id in seen:
                raise ResearchValueError(f"evidence_refs 含重复 evidence_id：{ref.evidence_id}")
            seen.add(ref.evidence_id)

        if self.status == STATUS_SUPPORTED and not refs:
            raise ResearchValueError("supported finding 必须至少引用一条证据")
        if self.status != STATUS_SUPPORTED and refs:
            raise ResearchValueError("unsupported finding 不能引用证据（evidence_refs 必须为空）")

        confidence = _require_unit_float(self.confidence, "ResearchFinding.confidence")
        object.__setattr__(self, "confidence", confidence)

        expected = compute_finding_id(
            candidate_id=self.candidate_id,
            dimension_id=self.dimension_id,
            status=self.status,
            claim=claim,
            evidence_ids=tuple(ref.evidence_id for ref in refs),
        )
        if self.finding_id == "":
            object.__setattr__(self, "finding_id", expected)
        else:
            _require_id(
                self.finding_id, FINDING_ID_PATTERN, "ResearchFinding.finding_id", "^rf_[a-z0-9]{16}$"
            )
            if self.finding_id != expected:
                raise ResearchValueError(
                    f"ResearchFinding.finding_id 与内容不符（期望 {expected}），拒绝接受伪造 id"
                )

    @property
    def is_supported(self) -> bool:
        return self.status == STATUS_SUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "dimension_id": self.dimension_id,
            "claim": self.claim,
            "value": self.value,
            "status": self.status,
            "confidence": self.confidence,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ResearchFinding:
        """接受 `Mapping`：payload 经核心冻结后嵌套结构是 `MappingProxyType`。"""
        if not isinstance(raw, Mapping):
            raise ResearchValueError("ResearchFinding.from_dict 需要 Mapping")
        raw_refs = raw.get("evidence_refs", [])
        if not isinstance(raw_refs, (list, tuple)):
            raise ResearchValueError("ResearchFinding.evidence_refs 必须是 list/tuple")
        refs: list[EvidenceRef] = []
        for index, item in enumerate(raw_refs):
            if not isinstance(item, Mapping):
                raise ResearchValueError(
                    f"ResearchFinding.evidence_refs[{index}] 必须是 Mapping，实际为 {type(item).__name__}"
                )
            refs.append(EvidenceRef.from_dict(item))
        return cls(
            candidate_id=raw.get("candidate_id"),
            task_id=raw.get("task_id"),
            agent_name=raw.get("agent_name"),
            dimension_id=raw.get("dimension_id"),
            claim=raw.get("claim"),
            value=raw.get("value"),
            status=raw.get("status"),
            evidence_refs=tuple(refs),
            confidence=raw.get("confidence", 1.0),
            finding_id=_optional_str(raw, "finding_id"),
        )


def _optional_str(raw: Mapping[str, Any], key: str) -> str:
    """可选字符串字段：缺失或 None -> 空串（表示"未提供，请自动派生"）；非 str -> 报错。"""
    if key not in raw:
        return ""
    value = raw[key]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ResearchValueError(f"{key} 字段存在但类型错误，期望 str，实际为 {type(value).__name__}")
    return value


def _optional_id(value: Any, pattern: Any, label: str) -> str:
    """允许为空串的标识字段（空串表示"与具体候选/维度无关"）。"""
    text = _require_str(value, label)
    if text == "":
        return ""
    if not pattern.match(text):
        raise ResearchValueError(f"{label} 必须是空字符串或 lower_snake_case，实际为 {text!r}")
    return text


def _require_ref_tuple(value: Any, label: str) -> tuple[EvidenceRef, ...]:
    if not isinstance(value, tuple):
        raise ResearchValueError(f"{label} 必须是 tuple[EvidenceRef, ...]")
    seen: set[str] = set()
    for ref in value:
        if not isinstance(ref, EvidenceRef):
            raise ResearchValueError(f"{label} 的元素必须是 EvidenceRef，实际为 {type(ref).__name__}")
        if ref.evidence_id in seen:
            raise ResearchValueError(f"{label} 含重复 evidence_id：{ref.evidence_id}")
        seen.add(ref.evidence_id)
    return value


def _require_sorted_id_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ResearchValueError(f"{label} 必须是 tuple[str, ...]")
    for item in value:
        if not isinstance(item, str) or not item:
            raise ResearchValueError(f"{label} 的元素必须是非空字符串")
    if list(value) != sorted(value):
        raise ResearchValueError(f"{label} 必须按字典序排列，实际为 {list(value)}")
    if len(set(value)) != len(value):
        raise ResearchValueError(f"{label} 不能有重复项")
    return value


def compute_risk_id(
    *,
    origin: str,
    rule_id: str,
    candidate_id: str,
    dimension_id: str,
    severity: str,
    description: str,
) -> str:
    """内容寻址生成 risk_id：同一条风险在重复运行中必然得到同一个 id。"""
    material = f"{origin}|{rule_id}|{candidate_id}|{dimension_id}|{severity}|{description}"
    return "rk_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RiskItem:
    """一条风险。规则风险（rule_based）必须带闭集内的 rule_id；Worker 自报的必须留空。"""

    severity: str
    origin: str
    rule_id: str
    description: str
    candidate_id: str = ""
    dimension_id: str = ""
    mitigation: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()
    risk_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.severity, str) or self.severity not in VALID_SEVERITIES:
            raise ResearchValueError(
                f"RiskItem.severity 必须是 {sorted(VALID_SEVERITIES)}，实际为 {self.severity!r}"
            )
        if not isinstance(self.origin, str) or self.origin not in VALID_RISK_ORIGINS:
            raise ResearchValueError(
                f"RiskItem.origin 必须是 {sorted(VALID_RISK_ORIGINS)}，实际为 {self.origin!r}"
            )

        rule_id = _require_str(self.rule_id, "RiskItem.rule_id")
        if self.origin == ORIGIN_RULE_BASED:
            if rule_id not in RULE_IDS:
                raise ResearchValueError(
                    f"rule_based 风险的 rule_id 必须是 {sorted(RULE_IDS)}，实际为 {rule_id!r}"
                )
        elif rule_id != "":
            raise ResearchValueError("worker_reported 风险的 rule_id 必须为空字符串")

        description = _require_str_nonempty(self.description, "RiskItem.description")
        _require_str(self.mitigation, "RiskItem.mitigation")
        candidate_id = _optional_id(self.candidate_id, CANDIDATE_ID_PATTERN, "RiskItem.candidate_id")
        dimension_id = _optional_id(self.dimension_id, DIMENSION_ID_PATTERN, "RiskItem.dimension_id")
        _require_ref_tuple(self.evidence_refs, "RiskItem.evidence_refs")

        expected = compute_risk_id(
            origin=self.origin,
            rule_id=rule_id,
            candidate_id=candidate_id,
            dimension_id=dimension_id,
            severity=self.severity,
            description=description,
        )
        if self.risk_id == "":
            object.__setattr__(self, "risk_id", expected)
        else:
            _require_id(self.risk_id, RISK_ID_PATTERN, "RiskItem.risk_id", "^rk_[a-z0-9]{16}$")
            if self.risk_id != expected:
                raise ResearchValueError(
                    f"RiskItem.risk_id 与内容不符（期望 {expected}），拒绝接受伪造 id"
                )

    @property
    def sort_key(self) -> tuple[int, str]:
        return (SEVERITY_ORDER[self.severity], self.risk_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "severity": self.severity,
            "origin": self.origin,
            "rule_id": self.rule_id,
            "candidate_id": self.candidate_id,
            "dimension_id": self.dimension_id,
            "description": self.description,
            "mitigation": self.mitigation,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
        }


@dataclass(frozen=True)
class CandidateCoverage:
    """单个候选的覆盖情况。`total = supported + len(missing_dimensions)` 必须成立。"""

    candidate_id: str
    total_dimensions: int
    supported_dimensions: int
    missing_dimensions: tuple[str, ...]
    finding_ids: tuple[str, ...]
    trusted_finding_ids: tuple[str, ...]
    evidence_by_source_type: Mapping[str, int]

    def __post_init__(self) -> None:
        _require_id(
            self.candidate_id, CANDIDATE_ID_PATTERN, "CandidateCoverage.candidate_id", "lower_snake_case"
        )
        for field_name in ("total_dimensions", "supported_dimensions"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ResearchValueError(f"CandidateCoverage.{field_name} 必须是非负整数")
        missing = _require_sorted_id_tuple(self.missing_dimensions, "CandidateCoverage.missing_dimensions")
        if self.supported_dimensions + len(missing) != self.total_dimensions:
            raise ResearchValueError(
                f"{self.candidate_id} 的覆盖数不自洽：supported {self.supported_dimensions} + "
                f"missing {len(missing)} != total {self.total_dimensions}"
            )
        finding_ids = _require_sorted_id_tuple(self.finding_ids, "CandidateCoverage.finding_ids")
        trusted = _require_sorted_id_tuple(self.trusted_finding_ids, "CandidateCoverage.trusted_finding_ids")
        if not set(trusted) <= set(finding_ids):
            raise ResearchValueError("trusted_finding_ids 必须是 finding_ids 的子集")
        if not isinstance(self.evidence_by_source_type, Mapping):
            raise ResearchValueError("CandidateCoverage.evidence_by_source_type 必须是 Mapping")
        for key, count in self.evidence_by_source_type.items():
            if not isinstance(key, str) or not key:
                raise ResearchValueError("evidence_by_source_type 的键必须是非空字符串")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ResearchValueError("evidence_by_source_type 的值必须是非负整数")
        object.__setattr__(
            self, "evidence_by_source_type", freeze_payload(dict(self.evidence_by_source_type))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "total_dimensions": self.total_dimensions,
            "supported_dimensions": self.supported_dimensions,
            "missing_dimensions": list(self.missing_dimensions),
            "finding_ids": list(self.finding_ids),
            "trusted_finding_ids": list(self.trusted_finding_ids),
            "evidence_by_source_type": thaw_payload(self.evidence_by_source_type),
        }


@dataclass(frozen=True)
class CoverageStats:
    """全部候选的覆盖汇总，按 candidate_id 排序。"""

    candidates: tuple[CandidateCoverage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise ResearchValueError("CoverageStats.candidates 必须是非空 tuple")
        for item in self.candidates:
            if not isinstance(item, CandidateCoverage):
                raise ResearchValueError(
                    f"CoverageStats.candidates 的元素必须是 CandidateCoverage，"
                    f"实际为 {type(item).__name__}"
                )
        ids = [item.candidate_id for item in self.candidates]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ResearchValueError(f"CoverageStats.candidates 必须按 candidate_id 排序且不重复：{ids}")

    def ids(self) -> tuple[str, ...]:
        return tuple(item.candidate_id for item in self.candidates)

    def get(self, candidate_id: str) -> CandidateCoverage:
        for item in self.candidates:
            if item.candidate_id == candidate_id:
                return item
        raise ResearchValueError(f"覆盖统计里没有候选 {candidate_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"candidates": [item.to_dict() for item in self.candidates]}


@dataclass(frozen=True)
class Recommendation:
    """一个候选的排名结论。

    `supporting_finding_ids` 只收录**有 fixture 证据支撑**的结论：assemble 阶段会把
    仅靠 synthetic_mock / legacy_text 的结论归入 `counter_finding_ids`，所以"只有真证据
    才能撑起推荐"是**组装逻辑**保证的，而不是事后过滤。

    "每个候选都必须有可信支撑"这条门槛放在报告级的 I4，不放在构造期 —— 否则
    `strict=False` 需要返回的那种"失败报告"根本构造不出来。
    """

    candidate_id: str
    rank: int
    score: float
    rationale: str
    supporting_finding_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    confidence: float
    matched_dimensions: tuple[str, ...] = ()
    missing_dimensions: tuple[str, ...] = ()
    counter_finding_ids: tuple[str, ...] = ()
    risk_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id(
            self.candidate_id, CANDIDATE_ID_PATTERN, "Recommendation.candidate_id", "lower_snake_case"
        )
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ResearchValueError(f"Recommendation.rank 必须是 >=1 的整数，实际为 {self.rank!r}")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ResearchValueError("Recommendation.score 必须是数值（禁止 bool）")
        if not math.isfinite(float(self.score)) or float(self.score) < 0.0:
            raise ResearchValueError(f"Recommendation.score 必须是非负有限值，实际为 {self.score!r}")
        object.__setattr__(self, "score", float(self.score))
        _require_str_nonempty(self.rationale, "Recommendation.rationale")

        supporting = _require_sorted_id_tuple(
            self.supporting_finding_ids, "Recommendation.supporting_finding_ids"
        )
        _require_sorted_id_tuple(self.counter_finding_ids, "Recommendation.counter_finding_ids")
        _require_sorted_id_tuple(self.risk_ids, "Recommendation.risk_ids")
        _require_sorted_id_tuple(self.matched_dimensions, "Recommendation.matched_dimensions")
        _require_sorted_id_tuple(self.missing_dimensions, "Recommendation.missing_dimensions")

        refs = _require_ref_tuple(self.evidence_refs, "Recommendation.evidence_refs")
        if [ref.evidence_id for ref in refs] != sorted(ref.evidence_id for ref in refs):
            raise ResearchValueError("Recommendation.evidence_refs 必须按 evidence_id 排序")
        # 「必须有 fixture 证据」这条在**报告级**（I4）判定，不放在构造期：
        # strict=False 要能返回 `passed=False` 的报告，如果候选完全没有可信支撑就构造失败，
        # 降级路径根本跑不通。这里只保留"一旦有证据，就必须有真证据"这条可本地判定的约束。
        if refs and not any(is_trusted_evidence(ref) for ref in refs):
            raise ResearchValueError(
                "Recommendation.evidence_refs 至少要有一条 fixture_snapshot 证据："
                "synthetic_mock / legacy_text 不能单独支撑推荐"
            )
        if bool(supporting) != bool(refs):
            raise ResearchValueError(
                "Recommendation.supporting_finding_ids 与 evidence_refs 必须同时为空或同时非空"
            )

        confidence = _require_unit_float(self.confidence, "Recommendation.confidence")
        object.__setattr__(self, "confidence", confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "rank": self.rank,
            "score": self.score,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "supporting_finding_ids": list(self.supporting_finding_ids),
            "counter_finding_ids": list(self.counter_finding_ids),
            "risk_ids": list(self.risk_ids),
            "matched_dimensions": list(self.matched_dimensions),
            "missing_dimensions": list(self.missing_dimensions),
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
        }


def derive_coverage(
    *,
    candidate_ids: tuple[str, ...],
    dimension_ids: tuple[str, ...],
    findings: tuple[ResearchFinding, ...],
) -> CoverageStats:
    """由 findings 派生覆盖统计。

    派生逻辑**只此一处**：`assemble` 调它来构造，`SelectionReport.__post_init__`
    调它来交叉校验。两处共用同一实现，因此不存在"统计与事实不一致"的窗口。
    """
    known_dimensions = set(dimension_ids)
    items: list[CandidateCoverage] = []
    for candidate_id in sorted(candidate_ids):
        bucket = [finding for finding in findings if finding.candidate_id == candidate_id]
        supported = [finding for finding in bucket if finding.is_supported]
        by_source: dict[str, int] = {}
        for finding in supported:
            for ref in finding.evidence_refs:
                by_source[ref.source_type] = by_source.get(ref.source_type, 0) + 1
        items.append(
            CandidateCoverage(
                candidate_id=candidate_id,
                total_dimensions=len(dimension_ids),
                supported_dimensions=len(supported),
                missing_dimensions=tuple(
                    sorted(
                        finding.dimension_id
                        for finding in bucket
                        if not finding.is_supported and finding.dimension_id in known_dimensions
                    )
                ),
                finding_ids=tuple(sorted(finding.finding_id for finding in bucket)),
                trusted_finding_ids=tuple(
                    sorted(
                        finding.finding_id
                        for finding in supported
                        if any(is_trusted_evidence(ref) for ref in finding.evidence_refs)
                    )
                ),
                evidence_by_source_type=by_source,
            )
        )
    return CoverageStats(candidates=tuple(items))


@dataclass(frozen=True)
class SelectionReport:
    """确定性技术选型报告。

    **刻意没有 `generated_at`**：报告只由输入决定，同一输入两次组装必须得到逐字节相同的结果。
    这也是 `report_id` 能用内容寻址的前提。

    构造期只做**形态校验**（排序、去重、引用自洽、report_id 重算），
    **不跑 I1–I9** —— 否则 `strict=False` 需要返回的"失败报告"根本构造不出来。
    """

    brief: ProjectBrief
    candidate_ids: tuple[str, ...]
    dimensions: tuple[ComparisonDimension, ...]
    findings: tuple[ResearchFinding, ...]
    risks: tuple[RiskItem, ...]
    recommendations: tuple[Recommendation, ...]
    coverage: CoverageStats
    integrity: IntegrityReport
    report_id: str = ""
    schema_version: str = REPORT_SCHEMA_VERSION
    generated_by: str = REPORT_GENERATED_BY

    def __post_init__(self) -> None:
        if not isinstance(self.brief, ProjectBrief):
            raise ResearchValueError("SelectionReport.brief 必须是 ProjectBrief")
        if self.schema_version != REPORT_SCHEMA_VERSION:
            raise ResearchValueError(
                f"SelectionReport.schema_version 必须是 {REPORT_SCHEMA_VERSION!r}，"
                f"实际为 {self.schema_version!r}"
            )
        if self.generated_by != REPORT_GENERATED_BY:
            raise ResearchValueError(
                f"SelectionReport.generated_by 必须是 {REPORT_GENERATED_BY!r}，"
                f"实际为 {self.generated_by!r}"
            )
        if not isinstance(self.integrity, IntegrityReport):
            raise ResearchValueError("SelectionReport.integrity 必须是 IntegrityReport")

        candidates = _require_sorted_id_tuple(self.candidate_ids, "SelectionReport.candidate_ids")
        if not candidates:
            raise ResearchValueError("SelectionReport.candidate_ids 不能为空")

        self._validate_ordering()
        self._validate_cross_references(candidates)
        self._resolve_report_id()

    def _validate_ordering(self) -> None:
        if not isinstance(self.dimensions, tuple) or not self.dimensions:
            raise ResearchValueError("SelectionReport.dimensions 必须是非空 tuple")
        dimension_ids = [item.dimension_id for item in self.dimensions]
        if dimension_ids != sorted(dimension_ids):
            raise ResearchValueError("SelectionReport.dimensions 必须按 dimension_id 排序")

        if not isinstance(self.findings, tuple) or not self.findings:
            raise ResearchValueError("SelectionReport.findings 必须是非空 tuple")
        finding_keys = [(item.candidate_id, item.dimension_id) for item in self.findings]
        if finding_keys != sorted(finding_keys):
            raise ResearchValueError("SelectionReport.findings 必须按 (candidate_id, dimension_id) 排序")
        finding_ids = [item.finding_id for item in self.findings]
        if len(set(finding_ids)) != len(finding_ids):
            raise ResearchValueError("SelectionReport.findings 含重复 finding_id")

        if not isinstance(self.risks, tuple):
            raise ResearchValueError("SelectionReport.risks 必须是 tuple")
        risk_keys = [item.sort_key for item in self.risks]
        if risk_keys != sorted(risk_keys):
            raise ResearchValueError("SelectionReport.risks 必须按 (severity 降序, risk_id) 排序")
        risk_ids = [item.risk_id for item in self.risks]
        if len(set(risk_ids)) != len(risk_ids):
            raise ResearchValueError("SelectionReport.risks 含重复 risk_id")

        if not isinstance(self.recommendations, tuple) or not self.recommendations:
            raise ResearchValueError("SelectionReport.recommendations 必须是非空 tuple")
        ranks = [item.rank for item in self.recommendations]
        if ranks != sorted(ranks):
            raise ResearchValueError("SelectionReport.recommendations 必须按 rank 排序")

        if not isinstance(self.coverage, CoverageStats):
            raise ResearchValueError("SelectionReport.coverage 必须是 CoverageStats")

    def _validate_cross_references(self, candidates: tuple[str, ...]) -> None:
        if self.coverage.ids() != candidates:
            raise ResearchValueError(
                f"coverage 的候选 {list(self.coverage.ids())} 与 candidate_ids {list(candidates)} 不一致"
            )
        expected_coverage = derive_coverage(
            candidate_ids=candidates,
            dimension_ids=tuple(dimension.dimension_id for dimension in self.dimensions),
            findings=self.findings,
        )
        if self.coverage != expected_coverage:
            raise ResearchValueError(
                "SelectionReport.coverage 与 findings 派生出的统计不一致，拒绝接受对不上的覆盖数据"
            )
        recommended = tuple(sorted(item.candidate_id for item in self.recommendations))
        if recommended != candidates:
            raise ResearchValueError(
                f"recommendations 覆盖的候选 {list(recommended)} 与 candidate_ids {list(candidates)} 不一致"
            )
        referenced = {
            ref.evidence_id for finding in self.findings for ref in finding.evidence_refs
        } | {ref.evidence_id for rec in self.recommendations for ref in rec.evidence_refs}
        if set(self.evidence_index) != referenced:
            missing = sorted(referenced - set(self.evidence_index))
            extra = sorted(set(self.evidence_index) - referenced)
            raise ResearchValueError(
                f"evidence_index 与引用不一致；缺失 {missing}，多余 {extra}"
            )
        if not all(isinstance(ref, EvidenceRef) for ref in self.evidence_index.values()):
            raise ResearchValueError("evidence_index 的值必须是 EvidenceRef")

    def _resolve_report_id(self) -> None:
        expected = self.expected_report_id
        if self.report_id == "":
            object.__setattr__(self, "report_id", expected)
        else:
            _require_id(self.report_id, REPORT_ID_PATTERN, "SelectionReport.report_id", "^rp_[a-z0-9]{16}$")
            if self.report_id != expected:
                raise ResearchValueError(
                    f"SelectionReport.report_id 与内容不符（期望 {expected}），拒绝接受伪造 id"
                )

    @property
    def evidence_index(self) -> Mapping[str, EvidenceRef]:
        """由 findings / recommendations 派生的证据索引（按 evidence_id 排序插入）。"""
        index: dict[str, EvidenceRef] = {}
        for finding in self.findings:
            for ref in finding.evidence_refs:
                index.setdefault(ref.evidence_id, ref)
        for rec in self.recommendations:
            for ref in rec.evidence_refs:
                index.setdefault(ref.evidence_id, ref)
        return freeze_payload({key: index[key] for key in sorted(index)})

    def content_projection(self) -> dict[str, Any]:
        """用于 `report_id` 的内容投影。**不含 integrity**：完整性是评估结果，不是内容。

        好处是 `strict=False` 的失败报告与同内容的成功报告共享同一个 `report_id`，
        `assemble_report` 也能先用占位 integrity 构造报告、跑完检查再 replace。
        """
        return {
            "schema_version": self.schema_version,
            "generated_by": self.generated_by,
            "brief": self.brief.to_dict(),
            "candidate_ids": list(self.candidate_ids),
            "dimensions": [dimension.to_dict() for dimension in self.dimensions],
            "findings": [finding.to_dict() for finding in self.findings],
            "risks": [risk.to_dict() for risk in self.risks],
            "recommendations": [rec.to_dict() for rec in self.recommendations],
            "coverage": self.coverage.to_dict(),
        }

    @property
    def expected_report_id(self) -> str:
        payload = json.dumps(
            self.content_projection(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return "rp_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        projection = self.content_projection()
        projection["report_id"] = self.report_id
        projection["integrity"] = self.integrity.to_dict()
        projection["evidence_index"] = {
            key: ref.to_dict() for key, ref in self.evidence_index.items()
        }
        return projection

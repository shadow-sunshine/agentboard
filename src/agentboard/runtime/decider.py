"""维度取值决策（证据映射的可插拔层，P3/P4）。

为什么是可插拔的
----------------
"从取回的内容里得出某维度取值"在本项目里**不伪造**：没有受控生成的参与，
运行时自己拍脑袋填值就等于编造结论。因此这一步是一个显式的注入点：

- **离线（P3 默认 / 测试 / 评测）**：`ScriptedDimensionDecider` 按确定性映射表
  给出取值 —— 场景作者显式声明"这份内容支持这个取值"，映射过程可审计；
  **它不是自主推理**（P3 复核结论，README 如实标注）；
- **P4 受控生成**：`GeneratedDimensionDecider` 把**已核验语料**交给决策 transport，
  按严格闭集 JSON 协议校验输出（fail-closed）——仍是受控决策，不宣称通用自主智能。

决策器返回 `None` 表示"这份材料不足以支撑该维度取值" ⇒ 运行时走
**缺口补检或显式 unsupported**（绝不推断补齐 —— 与 P1A 的 unknown 纪律一致）。

决策产出的合法性由运行时校验（值必须在量表内、引用的 section 必须存在、
claim 非空）—— 违例抛 `EvidenceBoundaryError`，是显式失败而不是静默修正。

**决策器身份（P4）**：每个实现都暴露 `identity` —— 稳定的配置摘要字符串，
进入 run_id 与审计记录。改变决策机制 / 模型配置 ⇒ 改变运行身份（内容寻址纪律）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import EvidenceBoundaryError


@dataclass(frozen=True, slots=True)
class CorpusSection:
    """一次决策可看到的语料切片视图（只读投影，不含 URL / 元数据）。"""

    section_id: str
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class DimensionDecision:
    """一个 (候选, 维度) 上的取值决策。

    `confidence`（P4）：受控生成决策的置信信息，`None` 表示本决策不携带
    （脚本决策器）；提供时必须在 [0, 1]。它进入运行身份与审计记录，
    但**不参与**报告内容与 report_id（完整性只由既有校验决定）。
    """

    value: str
    claim: str
    section_id: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise EvidenceBoundaryError("DimensionDecision.value 必须是非空字符串")
        if not isinstance(self.claim, str) or not self.claim.strip():
            raise EvidenceBoundaryError("DimensionDecision.claim 必须是非空字符串")
        if not isinstance(self.section_id, str) or not self.section_id.strip():
            raise EvidenceBoundaryError("DimensionDecision.section_id 必须是非空字符串")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise EvidenceBoundaryError("DimensionDecision.confidence 必须是数值或 None")
            if not 0.0 <= float(self.confidence) <= 1.0:
                raise EvidenceBoundaryError(
                    f"DimensionDecision.confidence 必须在 [0, 1]，实际 {self.confidence!r}"
                )


@runtime_checkable
class DimensionDecider(Protocol):
    """维度取值决策器协议（无状态；同样输入 ⇒ 同样决策）。"""

    @property
    def identity(self) -> str:
        """决策机制 / 配置的稳定摘要（进入 run_id 与审计；P4）。"""
        ...

    def decide(
        self,
        *,
        candidate_id: str,
        dimension_id: str,
        sections: Sequence[CorpusSection],
    ) -> DimensionDecision | None:
        """对该 (候选, 维度) 给出取值决策；材料不足时返回 `None`。"""
        ...


def _mapping_identity(prefix: str, payload: Mapping[str, object]) -> str:
    return prefix + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ScriptedDimensionDecider:
    """确定性脚本决策器（离线测试 / 评测的默认实现；**不是自主推理**）。

    映射表：`(candidate_id, dimension_id) -> DimensionDecision`。
    表中没有的键返回 `None`（⇒ 缺口 / unsupported），**不猜**。
    """

    decisions: dict[tuple[str, str], DimensionDecision]

    @property
    def identity(self) -> str:
        """映射表的规范序列化摘要：映射变 ⇒ 身份变（即使本输入上输出恰好相同）。"""
        payload = {
            f"{candidate_id}|{dimension_id}": {
                "value": decision.value,
                "claim": decision.claim,
                "section_id": decision.section_id,
                "confidence": decision.confidence,
            }
            for (candidate_id, dimension_id), decision in sorted(self.decisions.items())
        }
        return _mapping_identity("scripted.v1:", payload)

    def decide(
        self,
        *,
        candidate_id: str,
        dimension_id: str,
        sections: Sequence[CorpusSection],
    ) -> DimensionDecision | None:
        decision = self.decisions.get((candidate_id, dimension_id))
        if decision is None:
            return None
        # 决策引用的 section 必须真实存在于本次语料（边界校验在 runner 统一做值域，
        # 这里先做存在性 —— 两层都显式失败，不静默修正）。
        known = {section.section_id for section in sections}
        if decision.section_id not in known:
            raise EvidenceBoundaryError(
                f"决策引用了不存在的 section {decision.section_id!r}"
                f"（候选 {candidate_id!r}，维度 {dimension_id!r}）"
            )
        return decision

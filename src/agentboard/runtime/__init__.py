"""研究运行时（P3 live-research MVP）。

任务输入 → 研究计划 → Provider 检索 → 来源快照 → 证据校验/映射 → 确定性报告。

边界承诺（与项目既有信任模型一致）：
- Provider 产物**永远不直接构造** `EvidenceRef`——唯一路径是
  快照落盘 → 语料构建（本包 `corpus.py`）→ 既有 loader 与受控工厂（research 层）；
- 默认路径零网络、零时钟、零凭据；真实 Provider 是显式 opt-in；
- 失败 / 降级 / 预算全部可观测，未知错误显式失败。
"""

from .budget import BudgetExhaustedError, BudgetLedger, ResearchBudget
from .corpus import CandidateMaterial, CorpusBuilder, load_corpus
from .decider import (
    CorpusSection,
    DimensionDecider,
    DimensionDecision,
    ScriptedDimensionDecider,
)
from .errors import (
    BudgetExhaustedError as BudgetExhaustedError_,  # noqa: F401  (re-export)
)
from .errors import (
    EvidenceBoundaryError,
    LiveResearchError,
    PlanError,
    PlanValueError,
    ProviderStepError,
    RunFailedError,
)
from .mock_provider import ScriptedSearchProvider, scripted_document
from .plan import PlanStep, ResearchPlan, build_plan
from .runner import LiveResearchConfig, LiveResearchResult, LiveResearchRunner

__all__ = [
    "BudgetExhaustedError",
    "BudgetLedger",
    "CandidateMaterial",
    "CorpusBuilder",
    "CorpusSection",
    "DimensionDecider",
    "DimensionDecision",
    "EvidenceBoundaryError",
    "LiveResearchConfig",
    "LiveResearchError",
    "LiveResearchResult",
    "LiveResearchRunner",
    "PlanError",
    "PlanStep",
    "PlanValueError",
    "ProviderStepError",
    "ResearchBudget",
    "ResearchPlan",
    "RunFailedError",
    "ScriptedDimensionDecider",
    "ScriptedSearchProvider",
    "build_plan",
    "load_corpus",
    "scripted_document",
]

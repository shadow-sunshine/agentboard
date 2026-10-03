"""研究运行时主循环（P3 live-research MVP）。

一条流水线，六段
----------------
    任务输入 → 研究计划 → Provider 检索（重试 / 预算）→ 去重与快照落盘
    → 证据映射（可插拔决策器）→ 缺口补检或显式 unsupported → 确定性报告

失败 / 降级语义（全部可观测，禁止静默吞错）
--------------------------------------------
- **可重试的 Provider 错误**（传输 / 超时 / HTTP / 响应结构 / 超大内容）：
  按 `max_retries_per_step` 重试（每次计入请求预算）；重试耗尽 ⇒ 步骤 `failed`（带原因）；
- **未知 Provider 错误**：立即抛 `ProviderStepError`（带原始异常链）——
  显式失败，绝不当作"这一步没结果"；
- **预算耗尽**：`BudgetExhaustedError` 被 runner 捕获 ⇒ 剩余步骤 `skipped`
  （原因 `budget_exhausted:*`），用已取得的材料继续走降级路径；
- **候选零材料**：候选进入 `degraded_candidates`（不进报告）；
- **维度无决策**：缺口补检一次（预算允许时）⇒ 仍无决策 ⇒ 显式 unsupported；
- **所有候选零材料**：`outcome=failed`（原因 `no_snapshots`），`report=None`。

确定性
------
零时钟（`captured_at` 由调用方显式传入）、全排序展开、内容寻址去重、
`run_id` 由**全部影响报告内容的输入**哈希派生：brief / 候选 / 维度标识 /
**完整维度定义与权重**（注册表 JSON 字节）/ 已验证文档摘要 /
**最终决策（value / claim / section）**。同一输入重放 ⇒ 同一 run_id、
同一语料、同一 `report_id`；只改变决策 ⇒ run_id 必然改变（内容寻址的反例由回归测试钉住）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..contracts import ProjectBrief, TaskCard, WorkerTaskInput
from ..providers.errors import (
    ContentTooLargeError,
    RedirectNotAllowedError,
    RetrievalTimeoutError,
    SourceHTTPError,
    SourceResponseError,
    TransportError,
)
from ..providers.interface import Provider, RetrievedDocument, SearchQuery
from ..providers.snapshot_store import SnapshotStore
from ..research.assemble import assemble_report
from ..research.config import DIMENSION_ID_PATTERN
from ..research.dimensions import DimensionRegistry
from ..research.errors import AgentBoardResearchError, ReportIntegrityError
from ..research.fixtures import FixtureResearchWorker
from .budget import BudgetExhaustedError, BudgetLedger, ResearchBudget
from .corpus import CandidateMaterial, CorpusBuilder, dedupe_documents, load_corpus
from .decider import CorpusSection, DimensionDecider
from .errors import (
    DecisionBudgetExhaustedError,
    DecisionGenerationError,
    PlanError,
    ProviderStepError,
)
from .plan import (
    STEP_FAILED,
    STEP_GAP_RECHECK,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    PlanStep,
    ResearchPlan,
    build_plan,
)

#: 可重试的 Provider 异常闭集（传输 / 超时 / HTTP / 响应结构 / 超大内容 / 重定向策略）。
#: 闭集之外的任何异常 ⇒ `ProviderStepError`（显式失败，不吞）。
RETRYABLE_PROVIDER_ERRORS: tuple[type[Exception], ...] = (
    TransportError,
    RetrievalTimeoutError,
    SourceHTTPError,
    SourceResponseError,
    ContentTooLargeError,
    RedirectNotAllowedError,
)

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"


def _decider_identity(decider: DimensionDecider) -> str:
    """决策机制身份（P4）：进 run_id 与审计记录。

    实现了 `identity` 的决策器（脚本 / 受控生成）用它；没有的（第三方实现）
    用显式占位 —— "身份不明"本身也要进入运行身份，而不是被静默当成已知。
    """
    identity = getattr(decider, "identity", None)
    if isinstance(identity, str) and identity.strip():
        return identity
    return "unidentified-decider"


def _decider_usage(decider: DimensionDecider) -> dict[str, int]:
    """决策器自身的请求 / 重试计数（实现了 `usage()` 的决策器才有）。"""
    usage = getattr(decider, "usage", None)
    if not callable(usage):
        return {}
    value = usage()
    if not isinstance(value, dict):
        return {}
    return {str(key): int(count) for key, count in value.items()}


def _decider_audit(decider: DimensionDecider) -> dict[str, dict[str, Any]]:
    """决策器审计（实现了 `audit()` 的决策器才有；闭集字段由决策器保证）。"""
    audit = getattr(decider, "audit", None)
    if not callable(audit):
        return {}
    value = audit()
    if not isinstance(value, dict):
        return {}
    return {str(key): dict(entry) for key, entry in value.items() if isinstance(entry, dict)}


def _serialize_decisions(
    decisions: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, object]]:
    """决策输出的审计序列化（字段闭集：value / claim / section_id / confidence）。"""
    serialized: dict[str, dict[str, object]] = {}
    for candidate_id in sorted(decisions):
        candidate: dict[str, object] = {}
        for dimension_id in sorted(decisions[candidate_id]):
            decision = decisions[candidate_id][dimension_id]
            candidate[dimension_id] = {
                "claim": decision.claim,
                "confidence": decision.confidence,
                "section_id": decision.section_id,
                "value": decision.value,
            }
        serialized[candidate_id] = candidate
    return serialized


@dataclass(frozen=True, slots=True)
class LiveResearchConfig:
    """一次运行的全部环境参数（零时钟：`captured_at` 显式传入）。"""

    captured_at: str
    #: 维度注册表 JSON 文件（字节级复制进语料；通常指向 fixtures/research 的那份）
    dimensions_json: Path
    snapshot_root: Path
    run_dir: Path
    #: 缺口补检：首轮无材料的 (候选, 维度) 追加一次变体查询（预算允许时）
    gap_recheck: bool = True
    #: 每步检索的返回条数上限（透传给 `SearchQuery.limit`）
    results_per_query: int = 5


@dataclass(frozen=True, slots=True)
class LiveResearchResult:
    """一次运行的完整可观测结果。"""

    run_id: str
    outcome: str
    failure_reason: str
    plan: ResearchPlan
    steps: tuple[PlanStep, ...]
    snapshots_written: int
    snapshots_duplicate: int
    degraded_candidates: tuple[str, ...]
    unsupported_dimensions: dict[str, tuple[str, ...]]
    budget_usage: dict[str, int]
    report: Any | None
    report_json: str | None
    corpus_dir: Path | None
    provider_id: str
    run_record_path: Path | None
    #: 决策机制身份（P4：进 run_id 与运行记录；换决策器/模型配置 ⇒ 换身份）。
    decision_identity: str
    #: 决策请求预算是否耗尽（P4：True ⇒ 部分 (候选, 维度) 走了显式 unsupported 降级）。
    decision_budget_exhausted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "steps": [
                {
                    "step_id": step.step_id,
                    "candidate_id": step.candidate_id,
                    "dimension_id": step.dimension_id,
                    "kind": step.kind,
                    "status": step.status,
                    "attempts": step.attempts,
                    "snapshot_count": step.snapshot_count,
                    "error": step.error,
                }
                for step in self.steps
            ],
            "snapshots_written": self.snapshots_written,
            "snapshots_duplicate": self.snapshots_duplicate,
            "degraded_candidates": list(self.degraded_candidates),
            "unsupported_dimensions": {
                key: list(value) for key, value in self.unsupported_dimensions.items()
            },
            "budget_usage": dict(self.budget_usage),
            "report_id": (
                self.report.report_id if self.report is not None else None
            ),
            "integrity_passed": (
                self.report.integrity.passed if self.report is not None else None
            ),
            "provider_id": self.provider_id,
            "decision_identity": self.decision_identity,
            "decision_budget_exhausted": self.decision_budget_exhausted,
        }


class LiveResearchRunner:
    """把"任务输入"走成"确定性报告"的运行时主循环。"""

    def __init__(
        self,
        *,
        brief: ProjectBrief,
        candidate_ids: Sequence[str],
        registry: DimensionRegistry,
        provider: Provider,
        budget: ResearchBudget,
        decider: DimensionDecider,
        config: LiveResearchConfig,
    ) -> None:
        if not isinstance(brief, ProjectBrief):
            raise PlanError("LiveResearchRunner 需要 ProjectBrief")
        self._brief = brief
        self._registry = registry
        self._provider = provider
        self._budget = budget
        self._decider = decider
        self._config = config
        self._candidate_ids = tuple(sorted(set(candidate_ids)))
        if not self._candidate_ids:
            raise PlanError("candidate_ids 不能为空")
        for candidate_id in self._candidate_ids:
            if not DIMENSION_ID_PATTERN.match(candidate_id):
                raise PlanError(f"candidate_id {candidate_id!r} 不是合法标识")
        self._dimension_ids = tuple(sorted(dimension.dimension_id for dimension in registry.dimensions))
        if not self._dimension_ids:
            raise PlanError("维度注册表为空，无法拆解研究计划")

    # ---- 主流程 ----

    def run(self) -> LiveResearchResult:
        plan = build_plan(
            brief_text=self._brief.text,
            candidate_ids=self._candidate_ids,
            dimension_ids=self._dimension_ids,
        )
        ledger = BudgetLedger(self._budget)
        store = SnapshotStore(self._config.snapshot_root)

        steps: list[PlanStep] = list(plan.steps)
        step_documents: dict[str, tuple[RetrievedDocument, ...]] = {}
        # 快照统计：按 SnapshotStore 的**实际写入/重复结果**累计（不是成功步骤数）。
        store_stats = {"written": 0, "duplicate": 0}

        # ---- 阶段 1：按计划检索（重试 / 预算 / 可观测状态机） ----
        steps, step_documents = self._run_search_phase(steps, ledger, store, store_stats)

        # ---- 阶段 2：缺口补检（预算允许时，每缺口一次变体查询） ----
        gaps = [
            (step.candidate_id, step.dimension_id)
            for step in steps
            if not step_documents.get(step.step_id)
        ]
        if self._config.gap_recheck and gaps:
            steps, gap_documents = self._run_gap_recheck(
                steps, gaps, ledger, store, store_stats
            )
            step_documents.update(gap_documents)

        # ---- 阶段 3：材料归组（按候选去重排序；"去重与快照"的边界职责） ----
        materials: dict[str, CandidateMaterial] = {}
        for step in steps:
            documents = step_documents.get(step.step_id, ())
            if not documents:
                continue
            existing = materials.get(step.candidate_id)
            merged = documents if existing is None else existing.documents + documents
            materials[step.candidate_id] = CandidateMaterial(
                candidate_id=step.candidate_id,
                documents=dedupe_documents(merged),
            )

        degraded_candidates = tuple(
            candidate_id
            for candidate_id in self._candidate_ids
            if candidate_id not in materials
        )
        decider_identity = _decider_identity(self._decider)
        # 失败结局路径（no_snapshots / insufficient_candidates）没有决策、也没有报告：
        # run_id 覆盖此时已存在的输入（维度定义 / 文档摘要 / 决策器身份）；
        # 决策输出维度留空并在注释里声明。
        run_id = self._run_id(materials, decisions={}, decider_identity=decider_identity)

        if not materials:
            return self._finalize(
                run_id=run_id, outcome=OUTCOME_FAILED, failure_reason="no_snapshots",
                plan=plan, steps=tuple(steps), snapshots_written=store_stats["written"],
                snapshots_duplicate=store_stats["duplicate"],
                degraded_candidates=degraded_candidates,
                unsupported_dimensions={}, ledger=ledger,
                report=None, report_json=None, corpus_dir=None,
            )
        if len(materials) < 2:
            # 组装的既有硬约束：技术选型至少需要两个候选（InsufficientCandidatesError）。
            # 把它前置为可观测的运行失败，而不是让异常逃出 runner。
            return self._finalize(
                run_id=run_id, outcome=OUTCOME_FAILED,
                failure_reason="insufficient_candidates",
                plan=plan, steps=tuple(steps), snapshots_written=store_stats["written"],
                snapshots_duplicate=store_stats["duplicate"],
                degraded_candidates=degraded_candidates,
                unsupported_dimensions={}, ledger=ledger,
                report=None, report_json=None, corpus_dir=None,
            )

        # ---- 阶段 4：证据映射（决策器 → 语料构建；边界违例 = 显式失败） ----
        #
        # 受控生成决策（P4）的两类**定义好的结局**（都不是静默吞错，也绝不
        # fallback 到脚本决策器掩盖生成失败）：
        # - `DecisionBudgetExhaustedError`：在 _map_evidence 内部就地降级 ——
        #   已决策的保留，剩余 (候选, 维度) 显式 unsupported，`decision_budget_exhausted`
        #   置 True 可观测；
        # - `DecisionGenerationError`（重试耗尽 / 致命协议违例）：整次运行转为
        #   可观测结局 `decision_failed:<Type>`，与组装失败同一纪律。
        # 未知异常原样传播（显式失败）。
        try:
            decisions, unsupported_dimensions, decision_budget_exhausted = (
                self._map_evidence(materials)
            )
        except DecisionGenerationError as exc:
            return self._finalize(
                run_id=run_id, outcome=OUTCOME_FAILED,
                failure_reason=f"decision_failed:{type(exc).__name__}",
                plan=plan, steps=tuple(steps), snapshots_written=store_stats["written"],
                snapshots_duplicate=store_stats["duplicate"],
                degraded_candidates=degraded_candidates,
                unsupported_dimensions={}, ledger=ledger,
                report=None, report_json=None, corpus_dir=None,
                decisions={},
                decision_budget_exhausted=False,
            )
        # 决策已定 ⇒ run_id 现在覆盖**全部**影响报告内容的输入
        # （含最终决策三元组与决策器身份 —— 换模型/配置 ⇒ 换运行身份）。
        run_id = self._run_id(materials, decisions, decider_identity=decider_identity)

        # ---- 阶段 5：语料落盘 + 既有 loader + 受控工厂 + 确定性组装 ----
        builder = CorpusBuilder(
            registry=self._registry,
            dimensions_json=self._config.dimensions_json,
            captured_at=self._config.captured_at,
        )
        build_result = builder.build(
            run_dir=self._config.run_dir, materials=materials, decisions=decisions,
        )
        catalog, _registry, snapshots = load_corpus(build_result.corpus_dir)
        worker = FixtureResearchWorker(catalog, _registry, snapshots)

        cards: list[TaskCard] = []
        for candidate_id in catalog.ids():
            task_input = WorkerTaskInput(
                task_id=f"research__{candidate_id}",
                title="调研候选方案",
                assigned_agent="research",
                goal="基于采集快照整理该候选在各比较维度上的依据",
                context=f"live-research MVP\ncandidate_id: {candidate_id}\n",
                upstream_results={},
            )
            result = asyncio.run(worker.run(task_input))
            cards.append(
                TaskCard(
                    task_id=task_input.task_id,
                    title=task_input.title,
                    assigned_agent=task_input.assigned_agent,
                    goal=task_input.goal,
                    context=task_input.context,
                    status="done",
                    result=result,
                    attempts=1,
                )
            )
        try:
            report = assemble_report(self._brief, cards, _registry, snapshots=snapshots)
        except AgentBoardResearchError as exc:
            # 组装期的确定性失败（完整性 / 候选不足等）也走可观测的失败结局，
            # 而不是让异常逃出 runner —— 运行结果必须能被评测逐字段断言。
            codes = ""
            if isinstance(exc, ReportIntegrityError):
                codes = ":" + ",".join(exc.failed_codes)
            return self._finalize(
                run_id=run_id, outcome=OUTCOME_FAILED,
                failure_reason=f"assemble_failed:{type(exc).__name__}{codes}",
                plan=plan, steps=tuple(steps), snapshots_written=store_stats["written"],
                snapshots_duplicate=store_stats["duplicate"],
                degraded_candidates=degraded_candidates,
                unsupported_dimensions=unsupported_dimensions, ledger=ledger,
                report=None, report_json=None, corpus_dir=build_result.corpus_dir,
                decisions=decisions,
                decision_budget_exhausted=decision_budget_exhausted,
            )

        return self._finalize(
            run_id=run_id, outcome=OUTCOME_COMPLETED, failure_reason="",
            plan=plan, steps=tuple(steps),
            snapshots_written=store_stats["written"],
            snapshots_duplicate=store_stats["duplicate"],
            degraded_candidates=degraded_candidates,
            unsupported_dimensions=unsupported_dimensions, ledger=ledger,
            report=report,
            report_json=json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False),
            corpus_dir=build_result.corpus_dir,
            decisions=decisions,
            decision_budget_exhausted=decision_budget_exhausted,
        )

    # ---- 阶段实现 ----

    def _run_search_phase(
        self,
        steps: list[PlanStep],
        ledger: BudgetLedger,
        store: SnapshotStore,
        store_stats: dict[str, int],
    ) -> tuple[list[PlanStep], dict[str, tuple[RetrievedDocument, ...]]]:
        step_documents: dict[str, tuple[RetrievedDocument, ...]] = {}
        executed: list[PlanStep] = []
        for step in steps:
            executed.append(
                self._execute_step(step, ledger, store, step_documents, store_stats)
            )
        return executed, step_documents

    def _run_gap_recheck(
        self,
        steps: list[PlanStep],
        gaps: Sequence[tuple[str, str]],
        ledger: BudgetLedger,
        store: SnapshotStore,
        store_stats: dict[str, int],
    ) -> tuple[list[PlanStep], dict[str, tuple[RetrievedDocument, ...]]]:
        gap_documents: dict[str, tuple[RetrievedDocument, ...]] = {}
        executed = list(steps)
        next_index = len(executed) + 1
        for candidate_id, dimension_id in sorted(gaps):
            if not ledger.can_start_step() or not ledger.can_send_request():
                break
            step = PlanStep(
                step_id=f"step-{next_index:03d}",
                candidate_id=candidate_id,
                dimension_id=dimension_id,
                query_terms=f"{candidate_id} {dimension_id} documentation",
                kind=STEP_GAP_RECHECK,
            )
            next_index += 1
            executed.append(
                self._execute_step(step, ledger, store, gap_documents, store_stats)
            )
        return executed, gap_documents

    def _execute_step(
        self,
        step: PlanStep,
        ledger: BudgetLedger,
        store: SnapshotStore,
        step_documents: dict[str, tuple[RetrievedDocument, ...]],
        store_stats: dict[str, int],
    ) -> PlanStep:
        """执行一个步骤（状态机：pending → succeeded | failed | skipped）。

        每条路径都落状态：成功带材料数、失败带原因、跳过带预算原因。
        """
        try:
            ledger.start_step()
        except BudgetExhaustedError as exc:
            return step.mark(status=STEP_SKIPPED, attempts=0, error=f"budget_exhausted: {exc}")
        try:
            ledger.consume_request()
        except BudgetExhaustedError as exc:
            return step.mark(status=STEP_SKIPPED, attempts=0, error=f"budget_exhausted: {exc}")
        current = replace(step, attempts=1)
        max_attempts = 1 + self._budget.max_retries_per_step
        documents: tuple[RetrievedDocument, ...] = ()
        while True:
            try:
                documents = tuple(
                    self._provider.search(
                        SearchQuery(
                            terms=current.query_terms,
                            limit=self._config.results_per_query,
                        ),
                        captured_at=self._config.captured_at,
                        timeout_seconds=self._budget.request_timeout_seconds,
                    )
                )
            except RETRYABLE_PROVIDER_ERRORS as exc:
                if current.attempts < max_attempts:
                    ledger.consume_request()
                    ledger.add_retry()
                    current = replace(current, attempts=current.attempts + 1)
                    continue
                return current.mark(
                    status=STEP_FAILED,
                    attempts=current.attempts,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as exc:
                raise ProviderStepError(
                    f"步骤 {current.step_id} 出现未知 Provider 错误"
                    f"（{type(exc).__name__}: {exc}）；未知错误不打进降级路径"
                ) from exc
            break

        try:
            written, duplicate = self._store_documents(current, documents, ledger, store)
        except BudgetExhaustedError as exc:
            return current.mark(
                status=STEP_SKIPPED, attempts=current.attempts,
                error=f"budget_exhausted: {exc}",
            )
        store_stats["written"] += written
        store_stats["duplicate"] += duplicate
        step_documents[current.step_id] = documents
        return current.mark(
            status=STEP_SUCCEEDED, attempts=current.attempts, snapshot_count=written
        )

    def _store_documents(
        self,
        step: PlanStep,
        documents: tuple[RetrievedDocument, ...],
        ledger: BudgetLedger,
        store: SnapshotStore,
    ) -> tuple[int, int]:
        written = 0
        duplicate = 0
        for document in documents:
            ledger.add_content_chars(len(document.content))
            result = store.write(document, provider_id=self._provider.provider_id)
            if result.written:
                written += 1
            elif result.duplicate:
                duplicate += 1
            else:  # conflict：内容寻址下不可能自然发生
                raise ProviderStepError(
                    f"步骤 {step.step_id} 的快照写入冲突：{result.existing_path}"
                )
        return written, duplicate

    def _map_evidence(
        self, materials: Mapping[str, CandidateMaterial]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, ...]], bool]:
        """证据映射：决策器逐 (候选, 维度) 给出取值；无决策 ⇒ 显式 unsupported。

        `DecisionBudgetExhaustedError` 在此就地降级（可观测，不 fallback）：
        已取得的决策保留，被预算拦下的 (候选, 维度) 计入 unsupported，
        第三个返回值 `decision_budget_exhausted` 置 True。
        """
        decisions: dict[str, dict[str, Any]] = {}
        unsupported: dict[str, tuple[str, ...]] = {}
        budget_exhausted = False
        for candidate_id in sorted(materials):
            sections = tuple(
                CorpusSection(
                    section_id=f"s_{candidate_id}_{index:02d}",
                    title=document.title,
                    content=document.content,
                )
                for index, document in enumerate(materials[candidate_id].documents)
            )
            candidate_decisions: dict[str, Any] = {}
            missing: list[str] = []
            for dimension_id in self._dimension_ids:
                if budget_exhausted:
                    # 预算已耗尽：剩余维度不再发请求，显式 unsupported（不伪造决策）。
                    missing.append(dimension_id)
                    continue
                try:
                    decision = self._decider.decide(
                        candidate_id=candidate_id, dimension_id=dimension_id, sections=sections
                    )
                except DecisionBudgetExhaustedError:
                    budget_exhausted = True
                    missing.append(dimension_id)
                    continue
                if decision is None:
                    missing.append(dimension_id)
                    continue
                candidate_decisions[dimension_id] = decision
            decisions[candidate_id] = candidate_decisions
            if missing:
                unsupported[candidate_id] = tuple(missing)
        return decisions, unsupported, budget_exhausted

    def _run_id(
        self,
        materials: Mapping[str, CandidateMaterial],
        decisions: Mapping[str, Mapping[str, Any]],
        *,
        decider_identity: str,
    ) -> str:
        """run_id 覆盖**全部影响报告内容的输入**（内容寻址派生；重放必然相同）。

        必须包含（P3 → P4 逐步扩展的修正范围）：
        - brief 文本 / 候选标识 / 维度标识 / `captured_at`；
        - **完整维度定义与权重**：注册表 JSON 的字节摘要（definitions、weight、
          utility_map 全部在里面 —— 改任何一个权重，run_id 都必须变）；
        - **已验证文档摘要**：每份材料的 `content_sha256`（自校验过的哈希）；
        - **决策机制身份**（P4）：决策器 identity（transport 标识 + 配置摘要 +
          量表）—— 换模型/换配置 ⇒ 换 run_id；
        - **最终决策**：每个 (候选, 维度) 的 value / claim / section_id /
          confidence —— 只改变决策 ⇒ run_id 必然改变（回归测试钉住反例）。

        失败结局路径（无决策、无报告）传空 `decisions`，由调用点注释声明。
        """
        # 用规范 JSON 作为哈希输入，字段名和数组边界同时参与寻址。
        # 不能直接拼接字符串：例如 brief="a" + candidate="bca" 与
        # brief="ab" + candidate="ca" 会形成相同字节序列。
        payload = {
            "schema": "live-research.run-id.v2",
            "brief": self._brief.text,
            "candidate_ids": list(self._candidate_ids),
            "dimension_ids": list(self._dimension_ids),
            "captured_at": self._config.captured_at,
            "dimensions_sha256": hashlib.sha256(
                self._config.dimensions_json.read_bytes()
            ).hexdigest(),
            "decider_identity": decider_identity,
            "materials": [
                {
                    "candidate_id": candidate_id,
                    "document_sha256": [
                        document.content_sha256
                        for document in materials[candidate_id].documents
                    ],
                }
                for candidate_id in sorted(materials)
            ],
            "decisions": [
                {
                    "candidate_id": candidate_id,
                    "dimension_id": dimension_id,
                    "value": decisions[candidate_id][dimension_id].value,
                    "claim": decisions[candidate_id][dimension_id].claim,
                    "section_id": decisions[candidate_id][dimension_id].section_id,
                    "confidence": decisions[candidate_id][dimension_id].confidence,
                }
                for candidate_id in sorted(decisions)
                for dimension_id in sorted(decisions[candidate_id])
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "lr_" + hashlib.sha256(encoded).hexdigest()[:16]

    def _finalize(
        self,
        *,
        run_id: str,
        outcome: str,
        failure_reason: str,
        plan: ResearchPlan,
        steps: tuple[PlanStep, ...],
        snapshots_written: int,
        snapshots_duplicate: int,
        degraded_candidates: tuple[str, ...],
        unsupported_dimensions: dict[str, tuple[str, ...]],
        ledger: BudgetLedger,
        report: Any | None,
        report_json: str | None,
        corpus_dir: Path | None,
        decisions: Mapping[str, Mapping[str, Any]] | None = None,
        decision_budget_exhausted: bool = False,
    ) -> LiveResearchResult:
        # 决策机制身份 / 用量 / 审计（P4）：进运行记录与结果对象；
        # 没有实现这些接口的决策器（P3 脚本决策器除外——它有 identity）得到空映射。
        decision_identity = _decider_identity(self._decider)
        budget_usage = {**ledger.usage(), **_decider_usage(self._decider)}
        run_record = {
            "run_id": run_id,
            "outcome": outcome,
            "failure_reason": failure_reason,
            "steps": [
                {
                    "step_id": step.step_id,
                    "candidate_id": step.candidate_id,
                    "dimension_id": step.dimension_id,
                    "kind": step.kind,
                    "status": step.status,
                    "attempts": step.attempts,
                    "snapshot_count": step.snapshot_count,
                    "error": step.error,
                }
                for step in steps
            ],
            "degraded_candidates": list(degraded_candidates),
            "unsupported_dimensions": {
                key: list(value) for key, value in unsupported_dimensions.items()
            },
            "budget_usage": budget_usage,
            # P4 审计：决策机制身份 + 决策输出（闭集字段）+ 逐决策原始输出摘要。
            "decision_identity": decision_identity,
            "decision_budget_exhausted": decision_budget_exhausted,
            "decisions": _serialize_decisions(decisions or {}),
            "decision_audit": _decider_audit(self._decider),
        }
        run_record_path: Path | None = None
        if outcome == OUTCOME_COMPLETED:
            self._config.run_dir.mkdir(parents=True, exist_ok=True)
            run_record_path = self._config.run_dir / "run_record.json"
            run_record_path.write_text(
                json.dumps(run_record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        return LiveResearchResult(
            run_id=run_id,
            outcome=outcome,
            failure_reason=failure_reason,
            plan=plan,
            steps=steps,
            snapshots_written=snapshots_written,
            snapshots_duplicate=snapshots_duplicate,
            degraded_candidates=degraded_candidates,
            unsupported_dimensions=unsupported_dimensions,
            budget_usage=budget_usage,
            report=report,
            report_json=report_json,
            corpus_dir=corpus_dir,
            provider_id=self._provider.provider_id,
            run_record_path=run_record_path,
            decision_identity=decision_identity,
            decision_budget_exhausted=decision_budget_exhausted,
        )

"""`FixtureResearchWorker`（P1A-2）：从本地资料快照产出结构化 findings。

同时满足两个协议：
- **核心口** `run(task_input) -> WorkerResult`（即 `agentboard.workers.base.WorkerAdapter`），
  供 board 状态机派发与回收；
- **领域口** `investigate(task_input) -> tuple[ResearchFinding, ...]`，供研究层直接消费。

两者共用同一个 `_findings_for()`，因此数据必然同源；有专门测试断言
`decode_findings(run(...).structured_payload) == investigate(...)`，防止两条出口分叉。

三条硬规则：

1. **candidate_id 必须显式声明**：只认任务包 `context` 里独立一行的
   `candidate_id: <id>`。解析不到就报错 —— 不按 `task_id` 猜、不做"文本里出现
   pgvector 就选 pgvector"这类启发式。歧义是精确性最贵的敌人。
2. **缺失即显式标记**：注册表里的每个维度都会产出一条 finding；快照没覆盖的维度
   产出 `unsupported`（不是省略、不是补默认值），让缺口在报告里可见。
3. **零外部依赖**：不联网、不读时钟、不调模型，同输入必得同输出。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..contracts import WorkerResult, WorkerTaskInput
from ..evidence import SOURCE_FIXTURE, EvidenceRef
from .config import (
    CANDIDATE_ID_LINE_PATTERN,
    CANDIDATE_ID_PATTERN,
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    UNSUPPORTED_VALUE,
)
from .dimensions import DimensionRegistry, load_dimension_registry
from .errors import (
    CandidateIdMissingError,
    DimensionConflictError,
    InvalidDimensionValueError,
    ResearchValueError,
)
from .models import ResearchFinding
from .payload import encode_findings
from .snapshot import (
    FixtureCatalog,
    FixtureDimensionValue,
    FixtureSnapshot,
    expected_excerpt_for,
    load_catalog,
    load_snapshots,
    section_locator,
)

SUPPORTED_CONFIDENCE = 1.0
UNSUPPORTED_CONFIDENCE = 0.0


def parse_candidate_id(context: str) -> str:
    """从任务包 context 解析唯一的 candidate_id。

    只接受独立一行、显式 key 的形态：`candidate_id: pgvector`。
    缺失 / 格式非法 / 多行取值不一致，一律 `CandidateIdMissingError`。
    """
    if not isinstance(context, str):
        raise CandidateIdMissingError(
            f"任务包 context 必须是字符串，实际为 {type(context).__name__}"
        )

    matches = CANDIDATE_ID_LINE_PATTERN.findall(context)
    if not matches:
        raise CandidateIdMissingError(
            "任务包 context 里没有显式的 `candidate_id: <id>` 行；"
            "本 Worker 不做任何启发式推断（不按 task_id 猜、不做文本匹配）"
        )

    distinct = set(matches)
    if len(distinct) > 1:
        raise CandidateIdMissingError(
            f"任务包 context 里出现多个互相冲突的 candidate_id：{sorted(distinct)}"
        )

    candidate_id = distinct.pop()
    if not CANDIDATE_ID_PATTERN.match(candidate_id):
        raise CandidateIdMissingError(
            f"解析到的 candidate_id {candidate_id!r} 不是合法 lower_snake_case 标识"
        )
    return candidate_id


class FixtureResearchWorker:
    """基于本地快照的确定性研究 Worker。"""

    def __init__(
        self,
        catalog: FixtureCatalog,
        registry: DimensionRegistry,
        snapshots: Mapping[str, FixtureSnapshot],
    ) -> None:
        if not isinstance(catalog, FixtureCatalog):
            raise ResearchValueError("FixtureResearchWorker 需要 FixtureCatalog")
        if not isinstance(registry, DimensionRegistry):
            raise ResearchValueError("FixtureResearchWorker 需要 DimensionRegistry")
        if not isinstance(snapshots, Mapping):
            raise ResearchValueError("FixtureResearchWorker 需要 snapshots 映射")
        missing = [cid for cid in catalog.ids() if cid not in snapshots]
        if missing:
            raise ResearchValueError(f"缺少候选快照：{missing}")
        self._catalog = catalog
        self._registry = registry
        self._snapshots = snapshots

    @classmethod
    def from_fixture_root(cls, root: Path) -> FixtureResearchWorker:
        """从 `fixtures/research/` 根目录装载目录、维度注册表与全部快照。"""
        base = Path(root)
        catalog = load_catalog(base)
        registry = load_dimension_registry(base / catalog.dimension_registry_path)
        snapshots = load_snapshots(base, catalog)
        return cls(catalog, registry, snapshots)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return self._catalog.ids()

    @property
    def dimension_ids(self) -> tuple[str, ...]:
        return self._registry.ids()

    # ---- 领域口 ----

    async def investigate(self, task_input: WorkerTaskInput) -> tuple[ResearchFinding, ...]:
        return self._findings_for(task_input)

    # ---- 核心口（WorkerAdapter）----

    async def run(self, task_input: WorkerTaskInput) -> WorkerResult:
        findings = self._findings_for(task_input)
        agent_name = task_input.assigned_agent
        supported = [finding for finding in findings if finding.is_supported]

        evidence: dict[str, EvidenceRef] = {}
        for finding in supported:
            for ref in finding.evidence_refs:
                evidence.setdefault(ref.evidence_id, ref)

        confidence = len(supported) / len(findings)
        candidate_id = findings[0].candidate_id
        return WorkerResult(
            task_id=task_input.task_id,
            agent_name=agent_name,
            facts=tuple(finding.claim for finding in supported),
            evidence=tuple(evidence[key] for key in sorted(evidence)),
            risks=(),
            open_questions=tuple(
                f"资料快照未覆盖维度 {finding.dimension_id}"
                for finding in findings
                if not finding.is_supported
            ),
            confidence=confidence,
            summary=(
                f"{candidate_id}：{len(supported)}/{len(findings)} 个维度在资料快照中有依据"
            ),
            session_id=f"fixture-session-{task_input.task_id}",
            structured_payload=encode_findings(findings),
        )

    # ---- 内部实现（两个出口共用同一份数据）----

    def _findings_for(self, task_input: WorkerTaskInput) -> tuple[ResearchFinding, ...]:
        if not isinstance(task_input, WorkerTaskInput):
            raise CandidateIdMissingError(
                f"需要 WorkerTaskInput，实际为 {type(task_input).__name__}"
            )
        candidate_id = parse_candidate_id(task_input.context)
        return self._build(
            candidate_id=candidate_id,
            task_id=task_input.task_id,
            agent_name=task_input.assigned_agent,
        )

    def _build(
        self, *, candidate_id: str, task_id: str, agent_name: str
    ) -> tuple[ResearchFinding, ...]:
        # entry 不存在时由 catalog.get 抛 UnknownCandidateError（不返回空结果）
        entry = self._catalog.get(candidate_id)
        snapshot = self._snapshots[entry.candidate_id]
        declared = self._resolve_declared_dimensions(snapshot)

        findings: list[ResearchFinding] = []
        for dimension in self._registry.dimensions:
            dimension_id = dimension.dimension_id
            declaration = declared.get(dimension_id)
            if declaration is None:
                findings.append(
                    ResearchFinding(
                        candidate_id=candidate_id,
                        task_id=task_id,
                        agent_name=agent_name,
                        dimension_id=dimension_id,
                        claim=f"资料快照未覆盖维度「{dimension.name}」，无法给出依据",
                        value=UNSUPPORTED_VALUE,
                        status=STATUS_UNSUPPORTED,
                        evidence_refs=(),
                        confidence=UNSUPPORTED_CONFIDENCE,
                    )
                )
                continue

            if declaration.value not in dimension.scale_labels:
                raise InvalidDimensionValueError(
                    f"候选 {candidate_id!r} 在维度 {dimension_id!r} 上的取值 "
                    f"{declaration.value!r} 不在量表 {list(dimension.scale_labels)} 内"
                )
            findings.append(
                ResearchFinding(
                    candidate_id=candidate_id,
                    task_id=task_id,
                    agent_name=agent_name,
                    dimension_id=dimension_id,
                    claim=declaration.claim,
                    value=declaration.value,
                    status=STATUS_SUPPORTED,
                    evidence_refs=(self._evidence_for(snapshot, declaration),),
                    confidence=SUPPORTED_CONFIDENCE,
                )
            )

        return tuple(
            sorted(findings, key=lambda finding: (finding.candidate_id, finding.dimension_id))
        )

    def _resolve_declared_dimensions(
        self, snapshot: FixtureSnapshot
    ) -> dict[str, FixtureDimensionValue]:
        """把快照里的维度键（可能是 alias）归一到 canonical id，冲突即报错。"""
        resolved: dict[str, FixtureDimensionValue] = {}
        for raw_name, declaration in snapshot.dimension_values.items():
            canonical = self._registry.resolve(raw_name)
            existing = resolved.get(canonical)
            if existing is not None and existing.value != declaration.value:
                raise DimensionConflictError(
                    f"候选 {snapshot.candidate_id!r} 在维度 {canonical!r} 上出现互相冲突的声明："
                    f"{existing.value!r}（来自 {existing.dimension_id!r}）"
                    f" vs {declaration.value!r}（来自 {raw_name!r}）"
                )
            resolved.setdefault(canonical, declaration)
        return resolved

    @staticmethod
    def _evidence_for(
        snapshot: FixtureSnapshot, declaration: FixtureDimensionValue
    ) -> EvidenceRef:
        """把维度声明指向的 section 变成一条 fixture_snapshot 证据。

        截断走 `expected_excerpt_for`（全仓唯一实现点，P2.2-2 收敛）——
        这里曾是 4 份重复实现之一。
        """
        index = snapshot.section_index(declaration.section_id)
        excerpt = expected_excerpt_for(snapshot.sections[index].text)
        return EvidenceRef(
            source_type=SOURCE_FIXTURE,
            source_id=f"fixture:{snapshot.candidate_id}",
            snapshot_version=snapshot.snapshot_version,
            locator=section_locator(index),
            excerpt=excerpt,
            captured_at=snapshot.captured_at,
        )

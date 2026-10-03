"""确定性报告组装（P1A-3）：S0 预检 → S1 收集 → S2 归一 → S3 风险 → S4 推荐 → S5 完整性。

`assemble_report` 是一个**纯函数**：只由 `(brief, cards, registry, snapshots)` 决定输出。
不读时钟、不用随机、不联网、不调模型。同一输入两次调用必须得到逐字节相同的报告
（`report_id` 也是内容寻址的）。

`strict` 的语义：
- `strict=True`（默认）：存在失败检查项时抛 `ReportIntegrityError`，**不返回半成品报告**；
- `strict=False`：照常返回报告，`integrity.passed is False`、`failed_codes` 列出失败码。

S0 预检放在最前面：配置类错误（维度注册表不自洽、快照声明越界）应该在动手组装之前
就失败，而不是在算到一半时炸出来。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import STATUS_DONE
from ..contracts import ProjectBrief, TaskCard
from ..evidence import SOURCE_FIXTURE, EvidenceRef, is_trusted_evidence
from .attestation import binding_reason_from_bytes, documents_blocking_reason
from .config import (
    MIN_CANDIDATES,
    ORIGIN_RULE_BASED,
    POLICY_RESEARCH_GRADE,
    REASON_MISMATCH,
    REASON_UNBOUND,
    SEVERITY_HIGH,
    STATUS_UNSUPPORTED,
    UNSUPPORTED_VALUE,
    VALID_SCALE_KINDS,
)
from .dimensions import DimensionRegistry
from .errors import (
    CandidateCoverageError,
    CandidateMissingError,
    DanglingLocatorError,
    DimensionConflictError,
    ForeignTaskError,
    InsufficientCandidatesError,
    InvalidDimensionValueError,
    ReportIntegrityError,
    ResearchValueError,
)
from .integrity import (
    BindingRecheck,
    IntegrityReport,
    check_i10_non_promotable_support,
    verify_integrity,
)
from .models import (
    Recommendation,
    ResearchFinding,
    RiskItem,
    SelectionReport,
    derive_coverage,
)
from .payload import decode_findings
from .rules import review_risks
from .scoring import CandidateScore, render_rationale, score_all
from .snapshot import (
    INDEX_FILENAME,
    FixtureSnapshot,
    SnapshotSet,
    expected_excerpt_for,
    resolve_locator,
)

RESEARCH_AGENT = "research"


# ---- S0：预检 ----


def preflight_registry(registry: DimensionRegistry) -> None:
    """维度配置预检。

    正常路径下 `build_registry` 已经保证了这些不变量；这里再验一次是因为
    `DimensionRegistry` 是公开的数据类，可能被直接构造出来绕过工厂。
    """
    if not isinstance(registry, DimensionRegistry):
        raise ResearchValueError("preflight_registry 需要 DimensionRegistry")
    if not registry.dimensions:
        raise ResearchValueError("维度注册表不能为空")
    ids = registry.ids()
    if list(ids) != sorted(ids) or len(set(ids)) != len(ids):
        raise ResearchValueError(f"维度注册表的 id 必须唯一且有序：{list(ids)}")
    total = registry.total_weight
    if abs(total - 1.0) > 1e-6:
        raise ResearchValueError(f"维度权重之和必须为 1.0，实际为 {total!r}")
    for dimension in registry.dimensions:
        if dimension.scale_kind not in VALID_SCALE_KINDS:
            raise ResearchValueError(
                f"{dimension.dimension_id} 的量表 {dimension.scale_kind!r} 不在 "
                f"{sorted(VALID_SCALE_KINDS)} 内"
            )
        if dimension.utility_spread < 2:
            raise ResearchValueError(f"{dimension.dimension_id} 的 utility_map 没有区分度")
    for name, canonical in registry.alias_index.items():
        registry.get(canonical)  # 解析不通过会抛 UnknownDimensionError
        if not name:
            raise ResearchValueError("alias_index 里出现了空名称")


def preflight_snapshots(
    registry: DimensionRegistry, snapshots: Mapping[str, FixtureSnapshot] | None
) -> None:
    """快照配置预检：每个候选声明的维度键必须可解析、取值必须在量表内、section 必须存在。

    同时做**可证明混集**检测（P2.2-3）：若映射里的快照带有 ≥2 个不同的非空
    `snapshot_set` 戳印，说明它由多个快照集拼成 —— 这是能被数据自证的真混集，
    无论是否声明 `snapshot_set` 都直接拒绝。只有单一标识（或全部未声明）时不触发，
    默认路径因此逐字节不变。
    """
    if not snapshots:
        return
    if not isinstance(snapshots, Mapping):
        raise ResearchValueError("preflight_snapshots 需要 Mapping")
    distinct_sets = sorted(
        {snapshot.snapshot_set for snapshot in snapshots.values() if snapshot.snapshot_set}
    )
    if len(distinct_sets) > 1:
        offenders = sorted(
            f"{candidate_id}->{snapshot.snapshot_set}"
            for candidate_id, snapshot in snapshots.items()
            if snapshot.snapshot_set
        )
        raise CandidateCoverageError(
            f"snapshots 映射混合了多个快照集 {distinct_sets}（{offenders}）—— "
            f"同名候选会被静默覆盖，混集已被拒绝（P2.2-3）"
        )
    for candidate_id in sorted(snapshots):
        snapshot = snapshots[candidate_id]
        if not isinstance(snapshot, FixtureSnapshot):
            raise ResearchValueError(
                f"snapshots[{candidate_id!r}] 必须是 FixtureSnapshot，"
                f"实际为 {type(snapshot).__name__}"
            )
        if snapshot.candidate_id != candidate_id:
            raise ResearchValueError(
                f"snapshots 的键 {candidate_id!r} 与快照自身的 candidate_id "
                f"{snapshot.candidate_id!r} 不一致"
            )
        seen: dict[str, str] = {}
        for raw_name in sorted(snapshot.dimension_values):
            declaration = snapshot.dimension_values[raw_name]
            dimension = registry.get(raw_name)
            canonical = dimension.dimension_id
            if canonical in seen and seen[canonical] != declaration.value:
                raise DimensionConflictError(
                    f"候选 {candidate_id!r} 在维度 {canonical!r} 上出现互相冲突的声明："
                    f"{seen[canonical]!r} vs {declaration.value!r}"
                )
            seen[canonical] = declaration.value
            if declaration.value not in dimension.scale_labels:
                raise InvalidDimensionValueError(
                    f"候选 {candidate_id!r} 在维度 {canonical!r} 上的取值 "
                    f"{declaration.value!r} 不在量表 {list(dimension.scale_labels)} 内"
                )
            snapshot.section_index(declaration.section_id)


# ---- S1：收集 ----


def collect_findings(cards: Iterable[TaskCard]) -> tuple[ResearchFinding, ...]:
    """从已完成的 research 卡读取 structured_payload 并解码为 findings。"""
    card_list = tuple(cards)
    board_task_ids = {card.task_id for card in card_list}
    collected: list[ResearchFinding] = []
    for card in card_list:
        if card.status != STATUS_DONE or card.assigned_agent != RESEARCH_AGENT:
            continue
        result = card.result
        if result is None:
            continue
        for finding in decode_findings(result.structured_payload):
            if finding.task_id not in board_task_ids:
                raise ForeignTaskError(
                    f"finding {finding.finding_id} 声明的 task_id {finding.task_id!r} "
                    "不属于本次任务板"
                )
            collected.append(finding)
    return tuple(sorted(collected, key=lambda item: (item.candidate_id, item.dimension_id)))


def collect_candidate_ids(
    findings: Sequence[ResearchFinding],
    snapshots: Mapping[str, FixtureSnapshot] | None,
    *,
    allow_incomplete_snapshots: bool = False,
) -> tuple[str, ...]:
    """候选集合：来自 findings 的候选 id 并集，按字典序。"""
    candidate_ids = tuple(sorted({finding.candidate_id for finding in findings}))
    if not candidate_ids:
        raise CandidateMissingError("任务板上没有已完成的 research 卡，无法组装选型报告")
    if len(candidate_ids) < MIN_CANDIDATES:
        raise InsufficientCandidatesError(
            f"参与比较的候选只有 {len(candidate_ids)} 个（{list(candidate_ids)}），"
            f"技术选型至少需要 {MIN_CANDIDATES} 个"
        )
    if snapshots and not allow_incomplete_snapshots:
        missing = [cid for cid in candidate_ids if cid not in snapshots]
        if missing:
            raise CandidateCoverageError(
                f"候选 {missing} 没有对应的本地资料快照，无法核验其证据"
            )
        unresearched = [cid for cid in sorted(snapshots) if cid not in candidate_ids]
        if unresearched:
            raise CandidateCoverageError(
                f"快照目录里有 {unresearched} 但没有对应的 research 结果："
                "静默丢弃它们会让报告看起来比实际更完整"
            )
    return candidate_ids


# ---- S2：归一与补齐 ----


def normalize_findings(
    findings: Sequence[ResearchFinding],
    registry: DimensionRegistry,
    candidate_ids: Sequence[str],
) -> tuple[ResearchFinding, ...]:
    """把 dimension_id 归一到 canonical，并对「候选 × 维度」笛卡尔积补齐 unsupported 占位。

    补齐是**显式**的：缺失维度不会被省略，而是产出一条 `unsupported` 记录，
    这样缺口在报告里可见（打分阶段按 0 分计入，风险阶段按权重判定）。
    """
    resolved: dict[str, dict[str, ResearchFinding]] = {}
    for finding in findings:
        canonical = registry.get(finding.dimension_id).dimension_id
        bucket = resolved.setdefault(finding.candidate_id, {})
        existing = bucket.get(canonical)
        if existing is not None:
            if existing.value != finding.value or existing.status != finding.status:
                raise DimensionConflictError(
                    f"候选 {finding.candidate_id!r} 在维度 {canonical!r} 上出现互相冲突的 finding："
                    f"{existing.finding_id} vs {finding.finding_id}"
                )
            continue
        if finding.dimension_id != canonical:
            finding = replace(finding, dimension_id=canonical, finding_id="")
        bucket[canonical] = finding

    for candidate_id in candidate_ids:
        bucket = resolved.setdefault(candidate_id, {})
        anchor = next(
            (finding for finding in findings if finding.candidate_id == candidate_id), None
        )
        if anchor is None:
            raise CandidateCoverageError(
                f"候选 {candidate_id!r} 没有任何 finding，无法补齐缺失维度"
            )
        for dimension in registry.dimensions:
            if dimension.dimension_id in bucket:
                continue
            bucket[dimension.dimension_id] = ResearchFinding(
                candidate_id=candidate_id,
                task_id=anchor.task_id,
                agent_name=anchor.agent_name,
                dimension_id=dimension.dimension_id,
                claim=f"资料快照未覆盖维度「{dimension.name}」，无法给出依据",
                value=UNSUPPORTED_VALUE,
                status=STATUS_UNSUPPORTED,
                evidence_refs=(),
                confidence=0.0,
            )

    normalized = [finding for bucket in resolved.values() for finding in bucket.values()]
    return tuple(sorted(normalized, key=lambda item: (item.candidate_id, item.dimension_id)))


def _validate_supported_values(
    findings: Sequence[ResearchFinding], registry: DimensionRegistry
) -> None:
    """supported finding 的取值必须命中量表；unsupported 的占位取值不参与判定。"""
    for finding in findings:
        if not finding.is_supported:
            continue
        dimension = registry.get(finding.dimension_id)
        if finding.value not in dimension.scale_labels:
            raise InvalidDimensionValueError(
                f"候选 {finding.candidate_id!r} 在维度 {finding.dimension_id!r} 上的取值 "
                f"{finding.value!r} 不在量表 {list(dimension.scale_labels)} 内"
            )


def _verify_fixture_locators(
    findings: Sequence[ResearchFinding], snapshots: Mapping[str, FixtureSnapshot] | None
) -> None:
    """fixture 证据的 locator 必须能在对应快照里解析出来，且摘录**逐字节**符合规范截断。

    这是「证据可解析」这条要求的真正落点：只有拿到快照才能判，因此放在组装期，
    而不是报告侧的 I1–I9。

    **为什么是精确比较而不是 `startswith`（P2.2-2，修 D2）**：
    旧实现用 `text.startswith(ref.excerpt)`，于是"原文的任意前缀"都能通过组装期校验，
    而来源层 `resolve_evidence` 只接受**规范截断**（`expected_excerpt_for`）——
    同一条证据在两条路径得到**相反的结论**（实测复现：20 字符前缀摘录，
    组装期通过、来源层拒绝）。收敛为两条路径共用 `expected_excerpt_for` 之后，
    "证据可解析"成为单一良定义属性：摘录必须**恰好等于**该 locator 指向原文的规范截断。
    """
    if not snapshots:
        return
    for finding in findings:
        for ref in finding.evidence_refs:
            if ref.source_type != SOURCE_FIXTURE:
                continue
            snapshot = snapshots.get(finding.candidate_id)
            if snapshot is None:
                raise CandidateCoverageError(
                    f"候选 {finding.candidate_id!r} 缺少快照，无法核验证据 {ref.evidence_id}"
                )
            text = resolve_locator(snapshot, ref.locator)
            if expected_excerpt_for(text) != ref.excerpt:
                raise DanglingLocatorError(
                    f"证据 {ref.evidence_id} 的摘录与 {ref.locator} 指向的原文不一致"
                    f"（应为规范截断 {len(expected_excerpt_for(text))} 字符，"
                    f"证据实际 {len(ref.excerpt)} 字符）"
                )


def _verify_snapshot_set_membership(
    snapshots: Mapping[str, FixtureSnapshot] | None, snapshot_set: SnapshotSet
) -> None:
    """声明的快照集归属必须**可证明**（P2.2-3，fail-closed）。

    每份参与组装的快照都必须携带与声明逐字相等的 `snapshot_set` 戳印：
    - `""`（所属目录未声明快照集）⇒ 归属不可证明 ⇒ 拒绝；
    - 其它值 ⇒ 可证明**不属于**本快照集（跨目录混集）⇒ 拒绝。
    绝不凭 `candidate_id` / `source_id` 相同就推断同源。
    """
    if not snapshots:
        raise CandidateCoverageError(
            f"声明了快照集 {snapshot_set.snapshot_set_id!r} 但未提供 snapshots，"
            "候选归属无法证明（fail-closed）"
        )
    conflicts = sorted(
        f"{candidate_id}（归属标识 {snapshot.snapshot_set!r}）"
        for candidate_id, snapshot in snapshots.items()
        if snapshot.snapshot_set != snapshot_set.snapshot_set_id
    )
    if conflicts:
        raise CandidateCoverageError(
            f"以下候选无法证明属于快照集 {snapshot_set.snapshot_set_id!r}：{conflicts} —— "
            f"同名候选跨目录混集会被静默覆盖，已被拒绝（P2.2-3 fail-closed）"
        )


# ---- S0.5：组装期绑定重验（P2.2-3 attempt 3，B6 修复） ----


def _snapshot_drift_from_parsed(
    snapshot: FixtureSnapshot | None, parsed: Mapping[str, Any]
) -> str | None:
    """H4 内容级核对：内存快照 == 同缓冲解析结果（逐字段）。返回漂移描述或 None。"""
    if snapshot is None:
        return "内存映射里没有该候选的快照"
    if not isinstance(snapshot, FixtureSnapshot):
        return f"快照类型非法（{type(snapshot).__name__}）"
    scalar_checks: tuple[tuple[str, Any, Any], ...] = (
        ("candidate_id", snapshot.candidate_id, parsed.get("candidate_id")),
        ("display_name", snapshot.display_name, parsed.get("display_name")),
        ("snapshot_version", snapshot.snapshot_version, parsed.get("snapshot_version")),
        ("captured_at", snapshot.captured_at, parsed.get("captured_at")),
        ("source_path", snapshot.source_path, parsed.get("source_path")),
        ("source_kind", snapshot.source_kind, parsed.get("source_kind")),
    )
    for name, in_memory, on_disk in scalar_checks:
        if in_memory != on_disk:
            return f"{name} 与已核验字节不一致"
    raw_sections = parsed.get("sections")
    if not isinstance(raw_sections, list) or len(raw_sections) != len(snapshot.sections):
        return "sections 与已核验字节不一致"
    for index, section in enumerate(snapshot.sections):
        item = raw_sections[index]
        if not isinstance(item, Mapping):
            return f"sections[{index}] 与已核验字节不一致"
        if (
            item.get("section_id") != section.section_id
            or item.get("heading") != section.heading
            or item.get("text") != section.text
            or list(item.get("tags", []) or []) != list(section.tags)
        ):
            return f"section:{section.section_id} 与已核验字节不一致"
    raw_values = parsed.get("dimension_values", {})
    if not isinstance(raw_values, Mapping) or set(raw_values) != set(snapshot.dimension_values):
        return "dimension_values 与已核验字节不一致"
    for key, declaration in snapshot.dimension_values.items():
        item = raw_values[key]
        if (
            not isinstance(item, Mapping)
            or item.get("value") != declaration.value
            or item.get("section_id") != declaration.section_id
            or item.get("claim") != declaration.claim
        ):
            return f"dimension_values:{key} 与已核验字节不一致"
    return None


def _declared_digests_from_verified(
    verified: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """H3b 的声明并集：从**已核验字节**解析出的 provenance 计算 `source_id → (raw_sha256, blob_sha)`。

    语义与 `sources.snapshot_set_document_digests()` 一致（集合级并集 + 冲突即拒），
    但输入是 S0.5 单次读取缓冲的解析对象（OfficialSources 承载的是解析后的
    `OfficialProvenance`，两者输入类型不同 ⇒ 各自实现提取、共享语义，见交接单偏离登记）。

    返回 `(并集, 冲突原因列表)`。
    """
    declared: dict[str, tuple[str, str]] = {}
    conflicts: list[str] = []
    for candidate_id in sorted(verified):
        provenance = verified[candidate_id].get("provenance")
        documents = provenance.get("documents") if isinstance(provenance, Mapping) else None
        if not isinstance(documents, list) or not documents:
            conflicts.append(
                f"{REASON_MISMATCH}: 候选 {candidate_id} 的已核验内容缺少 provenance.documents"
            )
            continue
        for document in documents:
            if not isinstance(document, Mapping):
                conflicts.append(
                    f"{REASON_MISMATCH}: 候选 {candidate_id} 的 provenance.documents 存在非法条目"
                )
                continue
            source_id = document.get("source_id")
            digests = (document.get("raw_sha256"), document.get("blob_sha"))
            if not isinstance(source_id, str):
                conflicts.append(
                    f"{REASON_MISMATCH}: 候选 {candidate_id} 的 provenance 缺少 source_id"
                )
                continue
            existing = declared.get(source_id)
            if existing is not None and existing != digests:
                conflicts.append(
                    f"{REASON_MISMATCH}: source_id {source_id!r} 在不同候选的 provenance 里"
                    f"登记了不同摘要：{existing} vs {digests}"
                )
            declared[source_id] = digests  # type: ignore[assignment]
    return declared, conflicts


def _reverify_binding(
    snapshot_set: SnapshotSet, snapshots: Mapping[str, FixtureSnapshot] | None
) -> BindingRecheck:
    """S0.5：组装期绑定重验（P2.2-3_REDESIGN rev2，H1–H5）。

    **设计总原则**：磁盘字节是唯一权威，内存对象一律不可信；每个文件在单次组装内
    **只读取一次**，摘要与解析基于同一份字节缓冲（H2 单次读取契约，
    组装内 TOCTOU 在结构上不可表达）。任何一层失败都**汇总进 I10**（不提前抛、
    不短路返回半成品），`strict` 分支统一决定抛错或返回失败报告。
    """
    context = snapshot_set.binding_context
    attestation = snapshot_set.attestation
    if attestation is None:
        # 没有证明 ⇒ 无从重验；"absent" 由 _attestation_blocking_reason 判定
        return BindingRecheck()
    if context is None:
        # 有证明但无同源绑定上下文（非工厂产物）⇒ 无法证明"证明对本次数据有效"
        return BindingRecheck(global_reasons=(REASON_UNBOUND,))

    root = Path(context.fixture_root)
    global_reasons: list[str] = []
    candidate_reasons: dict[str, str] = {}
    verified: dict[str, dict[str, Any]] = {}

    # H2（单次读取）：INDEX.json 恰读一次
    index_path = root / INDEX_FILENAME
    try:
        index_bytes = index_path.read_bytes()
    except OSError:
        return BindingRecheck(
            global_reasons=(f"{REASON_MISMATCH}: 读不到 {index_path}",)
        )
    try:
        index_parsed = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return BindingRecheck(
            global_reasons=(f"{REASON_MISMATCH}: INDEX.json 无法解析（{exc}）",)
        )
    raw_candidates = index_parsed.get("candidates") if isinstance(index_parsed, dict) else None
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return BindingRecheck(
            global_reasons=(f"{REASON_MISMATCH}: INDEX.json candidates 必须是非空列表",)
        )

    index_entries: dict[str, str] = {}
    for position, item in enumerate(raw_candidates):
        if not isinstance(item, Mapping):
            global_reasons.append(f"{REASON_MISMATCH}: INDEX.json candidates[{position}] 非法")
            continue
        rel = item.get("fixture_path")
        cid = item.get("candidate_id")
        if not isinstance(rel, str) or not isinstance(cid, str):
            global_reasons.append(
                f"{REASON_MISMATCH}: INDEX.json candidates[{position}] 缺少 "
                f"fixture_path / candidate_id"
            )
            continue
        index_entries[rel] = cid

    attested = {item.path: item for item in attestation.snapshot_files}
    # 集合级（A3②）：INDEX 登记集合 == 证明覆盖集合，"不多不少"
    missing_in_attestation = sorted(set(index_entries) - set(attested))
    extra_in_attestation = sorted(set(attested) - set(index_entries))
    if missing_in_attestation or extra_in_attestation:
        global_reasons.append(
            f"{REASON_MISMATCH}: INDEX 登记文件集合与证明覆盖集合不一致"
            f"（未被证明={missing_in_attestation}，证明里多出={extra_in_attestation}）"
        )

    # 逐文件（H2/H3/H4）：每个文件只读一次，缓冲同时用于摘要与解析
    buffers: dict[str, bytes] = {}
    for rel in sorted(index_entries):
        path = root / rel
        try:
            buffers[rel] = path.read_bytes()
        except OSError:
            continue  # 缺失由纯核心/下方 per-candidate 记录
    file_reason, file_detail = binding_reason_from_bytes(attestation, index_bytes, buffers)
    if file_reason:
        global_reasons.append(f"{file_reason}: {file_detail}")

    disk_digests = {
        rel: hashlib.sha256(data).hexdigest() for rel, data in buffers.items()
    }
    for candidate_id in sorted(snapshots or {}):
        snapshot = snapshots[candidate_id]
        stamp = snapshot.fixture_relpath
        if not stamp:
            candidate_reasons[candidate_id] = (
                "uncovered_candidate（快照缺少 fixture_relpath 戳印，"
                "无法证明对应磁盘文件）"
            )
            continue
        registered = attested.get(stamp)
        if registered is None:
            candidate_reasons[candidate_id] = (
                f"uncovered_candidate:{stamp}（不在证明覆盖集内）"
            )
            continue
        if registered.candidate_id != candidate_id:
            candidate_reasons[candidate_id] = (
                f"uncovered_candidate:{stamp}（证明登记的 candidate_id 为 "
                f"{registered.candidate_id!r}）"
            )
            continue
        if index_entries.get(stamp) != candidate_id:
            candidate_reasons[candidate_id] = (
                f"uncovered_candidate:{stamp}（INDEX.json 登记的 candidate_id 不一致）"
            )
            continue
        if stamp not in buffers:
            candidate_reasons[candidate_id] = f"disk_mismatch:{stamp}（读不到文件）"
            continue
        actual_digest = disk_digests[stamp]
        if actual_digest != registered.sha256:
            candidate_reasons[candidate_id] = (
                f"disk_mismatch:{stamp}（现算 {actual_digest} != 证明登记 {registered.sha256}）"
            )
            continue
        try:
            parsed = json.loads(buffers[stamp].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            candidate_reasons[candidate_id] = (
                f"disk_mismatch:{stamp}（字节无法解析为 JSON：{exc}）"
            )
            continue
        if not isinstance(parsed, dict):
            candidate_reasons[candidate_id] = f"disk_mismatch:{stamp}（顶层不是 JSON 对象）"
            continue
        drift = _snapshot_drift_from_parsed(snapshot, parsed)
        if drift is not None:
            candidate_reasons[candidate_id] = f"tampered_in_memory:{drift}"
            continue
        verified[candidate_id] = parsed

    # H3b 声明层：documents[] ↔ 当前 provenance 声明并集（从已核验内容计算）
    declared, conflicts = _declared_digests_from_verified(verified)
    global_reasons.extend(conflicts)
    if not conflicts:
        document_reason, document_detail = documents_blocking_reason(attestation, declared)
        if document_reason:
            global_reasons.append(f"{document_reason}: {document_detail}")

    return BindingRecheck(
        global_reasons=tuple(global_reasons),
        candidate_reasons=candidate_reasons,
        verified_content=verified,
    )


# ---- S4：推荐 ----


def build_recommendations(
    *,
    scores: Sequence[CandidateScore],
    findings: Sequence[ResearchFinding],
    risks: Sequence[RiskItem],
) -> tuple[Recommendation, ...]:
    """按打分顺序构造推荐。

    `supporting_finding_ids` 只收**有 fixture 证据**的结论；只有 synthetic_mock /
    legacy_text 的结论一律归入 `counter_finding_ids`，不参与支撑。
    """
    by_candidate: dict[str, list[ResearchFinding]] = {}
    for finding in findings:
        by_candidate.setdefault(finding.candidate_id, []).append(finding)
    risks_by_candidate: dict[str, list[RiskItem]] = {}
    for risk in risks:
        if risk.candidate_id:
            risks_by_candidate.setdefault(risk.candidate_id, []).append(risk)

    recommendations: list[Recommendation] = []
    for rank, score in enumerate(scores, start=1):
        candidate_findings = by_candidate.get(score.candidate_id, [])
        supporting = sorted(
            (
                finding
                for finding in candidate_findings
                if finding.is_supported
                and any(is_trusted_evidence(ref) for ref in finding.evidence_refs)
            ),
            key=lambda item: item.finding_id,
        )
        counter = sorted(
            (
                finding
                for finding in candidate_findings
                if finding.is_supported
                and not any(is_trusted_evidence(ref) for ref in finding.evidence_refs)
            ),
            key=lambda item: item.finding_id,
        )
        evidence: dict[str, EvidenceRef] = {}
        for finding in supporting:
            for ref in finding.evidence_refs:
                if is_trusted_evidence(ref):
                    evidence.setdefault(ref.evidence_id, ref)

        confidence = score.coverage_ratio * (
            sum(finding.confidence for finding in supporting) / len(supporting)
            if supporting
            else 0.0
        )
        recommendations.append(
            Recommendation(
                candidate_id=score.candidate_id,
                rank=rank,
                score=score.score,
                rationale=render_rationale(score, rank, trusted_findings=len(supporting)),
                supporting_finding_ids=tuple(finding.finding_id for finding in supporting),
                counter_finding_ids=tuple(finding.finding_id for finding in counter),
                risk_ids=tuple(
                    sorted(risk.risk_id for risk in risks_by_candidate.get(score.candidate_id, []))
                ),
                matched_dimensions=score.matched_dimensions,
                missing_dimensions=score.missing_dimensions,
                evidence_refs=tuple(evidence[key] for key in sorted(evidence)),
                confidence=confidence,
            )
        )
    return tuple(recommendations)


# ---- 组装入口 ----


def assemble_report(
    brief: ProjectBrief,
    cards: Iterable[TaskCard],
    registry: DimensionRegistry,
    *,
    snapshots: Mapping[str, FixtureSnapshot] | None = None,
    snapshot_set: SnapshotSet | None = None,
    strict: bool = True,
) -> SelectionReport:
    """组装确定性技术选型报告。

    `snapshots` 可选：提供时会在 S0 预检快照配置、并在 S1 交叉校验候选覆盖，
    同时让 fixture 证据的 locator 具备可核验的上下文（P1A-4 的评测会带上它）。

    `snapshot_set` 可选（P2.2-3）：默认 `None` ⇒ 行为与不传时**逐字节相同**。
    提供时：
    - 每份快照的 `snapshot_set` 戳印必须与声明一致（归属不可证明即拒绝）；
    - `policy=research_grade` 时：
      * S0.5 组装期绑定重验（attempt 3，B6 修复）—— 证明必须绑定**本次实际数据**：
        文件层（磁盘字节摘要 == 证明登记）、声明层（documents == 当前 provenance 并集）、
        内容级（内存快照 == 同缓冲解析结果）、证据级（EvidenceRef 对回已核验字节）；
      * 在 I1–I9 之后追加 **I10_NON_PROMOTABLE_SUPPORT**。
      I10 只影响完整性判定，**不改报告内容** ⇒ `report_id` 与同内容的
      默认组装完全相同（`content_projection` 不含 integrity）。
    """
    if not isinstance(brief, ProjectBrief):
        raise ResearchValueError("assemble_report 需要 ProjectBrief")
    if snapshot_set is not None and not isinstance(snapshot_set, SnapshotSet):
        raise ResearchValueError(
            f"snapshot_set 必须是 SnapshotSet，实际为 {type(snapshot_set).__name__}"
        )
    # 先把任务板实体化：cards 可能是生成器，后面会被复用多次
    card_list = tuple(cards)

    # S0 预检
    preflight_registry(registry)
    preflight_snapshots(registry, snapshots)
    if snapshot_set is not None:
        _verify_snapshot_set_membership(snapshots, snapshot_set)

    # S0.5 组装期绑定重验（仅 research_grade；attempt 3）
    recheck: BindingRecheck | None = None
    if snapshot_set is not None and snapshot_set.policy == POLICY_RESEARCH_GRADE:
        recheck = _reverify_binding(snapshot_set, snapshots)

    # S1 收集候选
    collected = collect_findings(card_list)
    research_grade = (
        snapshot_set is not None and snapshot_set.policy == POLICY_RESEARCH_GRADE
    )
    candidate_ids = collect_candidate_ids(
        collected, snapshots, allow_incomplete_snapshots=research_grade
    )

    # S2 归一 + 笛卡尔补齐
    findings = normalize_findings(collected, registry, candidate_ids)
    _validate_supported_values(findings, registry)
    if recheck is None:
        # 默认 / regression_replay 路径行为不变（P1A 字节稳定）。
        # research_grade 下证据核对由 S0.5/I10 以"已核验磁盘字节"为基准执行（H5），
        # 证据被单独篡改记 I10（evidence_mismatch），不在此抛输入校验异常。
        _verify_fixture_locators(findings, snapshots)

    # S3 风险审查
    worker_reported = _collect_worker_risks(card_list, candidate_ids)
    scores = score_all(
        candidate_ids=candidate_ids, dimensions=registry.dimensions, findings=findings
    )
    risks = review_risks(
        candidate_ids=candidate_ids,
        dimensions=registry.dimensions,
        findings=findings,
        scores=scores,
        worker_reported=worker_reported,
    )

    # S4 推荐
    recommendations = build_recommendations(scores=scores, findings=findings, risks=risks)
    coverage = derive_coverage(
        candidate_ids=tuple(candidate_ids),
        dimension_ids=registry.ids(),
        findings=findings,
    )

    # S5 完整性：先用占位 integrity 构造报告，跑完检查再 replace。
    # report_id 的计算投影不含 integrity，因此 replace 之后 report_id 依然自洽。
    draft = SelectionReport(
        brief=brief,
        candidate_ids=tuple(candidate_ids),
        dimensions=tuple(registry.dimensions),
        findings=findings,
        risks=risks,
        recommendations=recommendations,
        coverage=coverage,
        integrity=IntegrityReport(),
    )
    integrity = verify_integrity(draft)
    if snapshot_set is not None and snapshot_set.policy == POLICY_RESEARCH_GRADE:
        # 条件性检查码：只在 research_grade 下追加；I10 不改报告内容，
        # 因此两种 strict 模式（以及不声明 snapshot_set 的默认组装）
        # 对同一批内容的 report_id 完全相同。
        # 升格判定由 I10 按**实际快照** + loader-only 绑定的证明 + S0.5 现读现算
        # 的绑定重验结果（attempt 3）重算，不消费任何可伪造的预计算映射。
        integrity = integrity.with_check(
            check_i10_non_promotable_support(draft, snapshot_set, snapshots, recheck=recheck)
        )
    report = replace(draft, integrity=integrity)

    if strict and not integrity.passed:
        raise ReportIntegrityError(integrity.failed_codes)
    return report


def _collect_worker_risks(
    cards: Iterable[TaskCard], candidate_ids: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """汇总 Worker 自报风险（按候选归口）。找不到归属的风险挂在空串下。"""
    findings = collect_findings(cards)
    task_to_candidate: dict[str, str] = {
        finding.task_id: finding.candidate_id for finding in findings
    }
    collected: dict[str, list[str]] = {}
    for card in cards:
        if card.result is None:
            continue
        candidate_id = task_to_candidate.get(card.task_id, "")
        for risk in card.result.risks:
            collected.setdefault(candidate_id, []).append(risk)
    known = set(candidate_ids)
    return {
        candidate_id: tuple(sorted(texts))
        for candidate_id, texts in collected.items()
        if candidate_id in known
    }


# ---- 便于上层复用的只读视图 ----


def report_summary(report: SelectionReport) -> dict[str, object]:
    """给日志 / 终端用的简短摘要（纯派生，不引入新状态）。"""
    return {
        "report_id": report.report_id,
        "candidates": list(report.candidate_ids),
        "ranking": [
            {
                "rank": rec.rank,
                "candidate_id": rec.candidate_id,
                "score": rec.score,
                "missing": list(rec.missing_dimensions),
            }
            for rec in report.recommendations
        ],
        "risks": sorted(
            {risk.rule_id for risk in report.risks if risk.origin == ORIGIN_RULE_BASED}
        ),
        "high_risk_count": sum(1 for risk in report.risks if risk.severity == SEVERITY_HIGH),
        "integrity_passed": report.integrity.passed,
        "failed_codes": list(report.integrity.failed_codes),
    }

"""报告完整性校验（P1A-3）：I1–I9。

每条检查是一个纯函数 `(report) -> CheckResult`，只读报告、不改状态、不抛异常
（失败以 `passed=False` 记录，是否中断由 `assemble_report(strict=...)` 决定）。
这样同一批函数可以被单测、`strict=False` 的降级路径、以及 P1A-4 的评测共用。

**关于 I1 / I2 的诚实说明**：这两条在当前设计下是**防御性不变量**，
正常路径不可达 —— `EvidenceRef` 的 id 与摘要都是内容寻址且在构造期自校验，
`SelectionReport.evidence_index` 又是从 findings / recommendations 派生的，
所以"id 与内容不符""索引缺项"两种失败都无法通过公开构造路径制造出来。
它们仍然被保留，是因为一旦将来出现「外部传入 evidence_index」或「EvidenceRef 放宽自校验」
这类结构改动，这两条就是第一道拦截网。真正能被真实数据触发的失败项是 I3–I9。

`SelectionReport` 只在类型标注里引用（`TYPE_CHECKING`），运行时不 import ——
`models.py` 需要 `IntegrityReport`，反向依赖会成环。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from ..evidence import (
    ALLOWED_SOURCE_TYPES,
    SOURCE_FIXTURE,
    SOURCE_ID_PREFIXES,
    SOURCE_LEGACY,
    is_trusted_evidence,
)
from .config import (
    EXPECTED_VERIFIER_VERSION,
    POLICY_RESEARCH_GRADE,
    REASON_ABSENT,
    REASON_INCOMPLETE,
    REASON_MISMATCH,
    REASON_UNBOUND,
    REASON_VERSION_EXPIRED,
    RESULT_PASSED,
    SOURCE_KIND_OFFICIAL_EXCERPT,
    WEIGHT_SUM_TOLERANCE,
)
from .errors import ResearchValueError
from .snapshot import (
    SECTION_LOCATOR_PATTERN,
    FixtureSnapshot,
    SnapshotSet,
    expected_excerpt_for,
)

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注
    from .models import SelectionReport

#: fixture 证据 source_id 的候选段前缀（与核心层 `SOURCE_ID_PREFIXES` 强绑定，不在此重复字面量）
_FIXTURE_SOURCE_ID_PREFIX = SOURCE_ID_PREFIXES[SOURCE_FIXTURE]

#: 完整性检查码闭集（顺序即报告里的呈现顺序）。
#: `verify_integrity` 固定运行 I1–I9；I10 是**条件性**检查码，
#: 仅当组装方声明 `policy=research_grade` 时由 `assemble_report` 追加
#: （见 `CONDITIONAL_INTEGRITY_CODES` 与 `assemble_report` 的 `snapshot_set` 参数）。
INTEGRITY_CODES: tuple[str, ...] = ("I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9")

#: 条件性完整性检查码：不进 `verify_integrity` 的固定序列，由声明对应策略的组装追加。
CONDITIONAL_INTEGRITY_CODES: tuple[str, ...] = ("I10_NON_PROMOTABLE_SUPPORT",)

#: `IntegrityReport` 里允许出现的全部检查码
VALID_INTEGRITY_CODES = frozenset(INTEGRITY_CODES) | frozenset(CONDITIONAL_INTEGRITY_CODES)


@dataclass(frozen=True)
class CheckResult:
    """单条检查的结果记录（纯数据，不做判定）。"""

    code: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ResearchValueError("CheckResult.code 必须是非空字符串")
        if not isinstance(self.passed, bool):
            raise ResearchValueError(f"CheckResult.passed 必须是 bool，实际为 {type(self.passed).__name__}")
        if not isinstance(self.detail, str):
            raise ResearchValueError("CheckResult.detail 必须是字符串")


@dataclass(frozen=True)
class IntegrityReport:
    """完整性检查汇总。

    `passed` / `failed_codes` 都是**派生属性**，不允许直接赋值 —— 报告的判定只有一个来源，
    就是 `checks` 本身。空 `checks` 合法（组装期作为占位，随后被 replace 成真实结果）。
    """

    checks: tuple[CheckResult, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.checks, tuple):
            raise ResearchValueError("IntegrityReport.checks 必须是 tuple[CheckResult, ...]")
        seen: set[str] = set()
        for check in self.checks:
            if not isinstance(check, CheckResult):
                raise ResearchValueError(
                    f"IntegrityReport.checks 的元素必须是 CheckResult，"
                    f"实际为 {type(check).__name__}"
                )
            if check.code not in VALID_INTEGRITY_CODES:
                raise ResearchValueError(
                    f"未知的完整性检查码 {check.code!r}，"
                    f"合法码：固定 {list(INTEGRITY_CODES)} + 条件 {list(CONDITIONAL_INTEGRITY_CODES)}"
                )
            if check.code in seen:
                raise ResearchValueError(f"重复的完整性检查码：{check.code}")
            seen.add(check.code)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)

    def detail_of(self, code: str) -> str:
        for check in self.checks:
            if check.code == code:
                return check.detail
        raise ResearchValueError(f"报告里没有检查码 {code!r}")

    def with_check(self, check: CheckResult) -> IntegrityReport:
        """返回追加了单条检查的新汇总（不可变风格；重复码由 `__post_init__` 拒绝）。

        供 `assemble_report` 追加**条件性**检查码（如 I10）——
        固定序列 I1–I9 仍由 `verify_integrity` 产出，二者不混。
        """
        return IntegrityReport(checks=(*self.checks, check))

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "failed_codes": list(self.failed_codes),
            "checks": [
                {"code": check.code, "passed": check.passed, "detail": check.detail}
                for check in self.checks
            ],
        }


def _referenced_refs(report: SelectionReport) -> list:
    refs = [ref for finding in report.findings for ref in finding.evidence_refs]
    refs.extend(ref for rec in report.recommendations for ref in rec.evidence_refs)
    return refs


def check_i1_evidence_id_unique(report: SelectionReport) -> CheckResult:
    """I1：同一个 `evidence_id` 必须始终对应同一份证据内容（防御性不变量）。

    注意这里判的**不是**"引用次数必须是 1"：两条 finding 引用同一段原文是完全正常的，
    它们本来就应该得到同一个 id。真正要防的是"同一个 id 指向两份不同内容"。
    """
    by_id: dict[str, set[tuple[str, str, str, str, str]]] = {}
    for ref in _referenced_refs(report):
        fingerprint = (
            ref.source_type,
            ref.source_id,
            ref.snapshot_version,
            ref.locator,
            ref.digest,
        )
        by_id.setdefault(ref.evidence_id, set()).add(fingerprint)
    conflicting = sorted(code for code, prints in by_id.items() if len(prints) > 1)
    if conflicting:
        return CheckResult("I1", False, f"同一个 evidence_id 指向了不同内容：{conflicting}")
    return CheckResult("I1", True, f"{len(by_id)} 个 evidence_id 各自对应唯一内容")


def check_i2_references_resolvable(report: SelectionReport) -> CheckResult:
    """I2：所有证据引用都能在 evidence_index 里解析（防御性不变量，无悬空引用）。"""
    dangling = sorted(
        ref.evidence_id for ref in _referenced_refs(report) if ref.evidence_id not in report.evidence_index
    )
    if dangling:
        return CheckResult("I2", False, f"悬空的 evidence_id：{dangling}")
    return CheckResult("I2", True, f"{len(report.evidence_index)} 条索引项全部可解析")


def check_i3_supported_has_non_legacy_evidence(report: SelectionReport) -> CheckResult:
    """I3：supported finding 必须至少有一条**非 legacy** 证据。"""
    bad = sorted(
        finding.finding_id
        for finding in report.findings
        if finding.is_supported
        and not any(ref.source_type != SOURCE_LEGACY for ref in finding.evidence_refs)
    )
    if bad:
        return CheckResult("I3", False, f"只有 legacy 证据的 supported finding：{bad}")
    return CheckResult("I3", True, "所有 supported finding 都有非 legacy 证据")


def check_i4_candidates_have_trusted_backing(report: SelectionReport) -> CheckResult:
    """I4：每个候选至少有一条由 fixture_snapshot 支撑的 supported finding。

    这是"只有真证据才能支撑推荐"的门槛：synthetic_mock 是确定性占位、
    legacy_text 是有损降级，两者都不能单独把某个候选撑起来。
    """
    starved = [
        candidate_id
        for candidate_id in report.candidate_ids
        if not any(
            finding.candidate_id == candidate_id
            and finding.is_supported
            and any(is_trusted_evidence(ref) for ref in finding.evidence_refs)
            for finding in report.findings
        )
    ]
    if starved:
        return CheckResult("I4", False, f"缺少可信证据支撑的候选：{starved}")
    return CheckResult("I4", True, "每个候选都有 fixture 证据支撑")


def check_i5_candidate_dimension_matrix_complete(report: SelectionReport) -> CheckResult:
    """I5：每个候选 × 每个注册维度都必须有 finding（supported 或 unsupported）。"""
    missing = sorted(
        f"{candidate_id}/{dimension.dimension_id}"
        for candidate_id in report.candidate_ids
        for dimension in report.dimensions
        if not any(
            finding.candidate_id == candidate_id and finding.dimension_id == dimension.dimension_id
            for finding in report.findings
        )
    )
    if missing:
        return CheckResult("I5", False, f"缺失的候选 × 维度组合：{missing}")
    return CheckResult(
        "I5",
        True,
        f"{len(report.candidate_ids)} × {len(report.dimensions)} 组合完整覆盖",
    )


def check_i6_ranking_is_sound(report: SelectionReport) -> CheckResult:
    """I6：rank 恰为 1..N，且按 rank 排序后 score 单调不增。"""
    ranks = [rec.rank for rec in report.recommendations]
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        return CheckResult("I6", False, f"rank 不是连续的 1..{len(ranks)}：{ranks}")
    scores = [rec.score for rec in report.recommendations]
    for previous, current in pairwise(scores):
        if current > previous + 1e-12:
            return CheckResult("I6", False, f"score 未随 rank 单调不增：{scores}")
    return CheckResult("I6", True, f"{len(ranks)} 条推荐的 rank 与 score 单调一致")


def check_i7_recommendation_references_resolvable(report: SelectionReport) -> CheckResult:
    """I7：推荐引用的 finding_id / risk_id 必须全部存在。"""
    finding_ids = {finding.finding_id for finding in report.findings}
    risk_ids = {risk.risk_id for risk in report.risks}
    dangling: list[str] = []
    for rec in report.recommendations:
        dangling.extend(
            f"{rec.candidate_id}->finding:{fid}"
            for fid in (*rec.supporting_finding_ids, *rec.counter_finding_ids)
            if fid not in finding_ids
        )
        dangling.extend(
            f"{rec.candidate_id}->risk:{rid}" for rid in rec.risk_ids if rid not in risk_ids
        )
    if dangling:
        return CheckResult("I7", False, f"悬空的推荐引用：{sorted(dangling)}")
    return CheckResult("I7", True, "推荐引用的 finding / risk 全部可解析")


def check_i8_weight_sum(report: SelectionReport) -> CheckResult:
    """I8：维度权重之和必须为 1.0（报告内再验一次，防止注册表被绕过）。"""
    total = sum(dimension.weight for dimension in report.dimensions)
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        return CheckResult("I8", False, f"权重之和为 {total!r}，期望 1.0")
    return CheckResult("I8", True, "维度权重之和为 1.0")


def check_i9_source_types_allowed(report: SelectionReport) -> CheckResult:
    """I9：所有证据的 source_type 都在白名单内。"""
    offending = sorted(
        {ref.source_type for ref in _referenced_refs(report) if ref.source_type not in ALLOWED_SOURCE_TYPES}
    )
    if offending:
        return CheckResult("I9", False, f"未授权的 source_type：{offending}")
    observed = sorted({ref.source_type for ref in _referenced_refs(report)})
    return CheckResult("I9", True, f"证据来源类型：{observed}")


INTEGRITY_CHECKERS = (
    check_i1_evidence_id_unique,
    check_i2_references_resolvable,
    check_i3_supported_has_non_legacy_evidence,
    check_i4_candidates_have_trusted_backing,
    check_i5_candidate_dimension_matrix_complete,
    check_i6_ranking_is_sound,
    check_i7_recommendation_references_resolvable,
    check_i8_weight_sum,
    check_i9_source_types_allowed,
)


def _attestation_blocking_reason(
    snapshot_set: SnapshotSet, expected_set_id: str
) -> str:
    """attestation 维度的阻塞原因（升格权威 = loader-only 绑定的证明）。

    返回 `""` 表示证明有效。与 `attestation.promotion_blocking_reason` 同一语义族，
    但不要求 provenance 并集（那是来源层 `is_promotable_source` 的加严项；
    文件级重验与声明层核对由组装期 S0.5 完成，经 `BindingRecheck` 传入本检查）。

    **attempt 3（H6）**：`research_grade` 声明即使持有已绑定的证明，
    `binding_context is None`（非 `load_snapshot_set()` 工厂产物）⇒ `unbound`
    —— "证明存在"不等于"证明与本次组装同源"。
    """
    attestation = snapshot_set.attestation
    if attestation is None:
        return REASON_ABSENT
    if attestation.binding_reason is None:
        # 直接 from_dict / 构造器拼出的对象：从未与磁盘字节绑定 ⇒ fail-closed
        return REASON_UNBOUND
    if snapshot_set.binding_context is None:
        # 有证明但无同源绑定上下文（非工厂产物）⇒ 无法证明"证明对本次数据有效"
        return REASON_UNBOUND
    if attestation.verifier_version != EXPECTED_VERIFIER_VERSION:
        return REASON_VERSION_EXPIRED
    if attestation.snapshot_set_id != expected_set_id:
        return REASON_MISMATCH
    if attestation.overall_result != RESULT_PASSED:
        return REASON_INCOMPLETE
    if attestation.binding_reason != "":
        return attestation.binding_reason
    return ""


@dataclass(frozen=True)
class BindingRecheck:
    """组装期绑定重验（S0.5）的结果容器（attempt 3，纯数据）。

    由 `assemble._reverify_binding` 构造、`check_i10_non_promotable_support` 消费。
    本类型不做任何判定，只搬运"现读现算"的测量结论：

    - `global_reasons`：文件层（H2/H3）与声明层（H3b）的失败原因，
      作用于**全部**候选（证明整体失效时不做部分信任）；
    - `candidate_reasons`：按候选的失败原因
      （`uncovered_candidate` / `disk_mismatch:<path>` / `tampered_in_memory:<cid>/<x>`）；
    - `verified_content`：通过全部字节层核验的候选 → 同缓冲解析出的 JSON 对象，
      供证据级核对（H5）把 `EvidenceRef` 对回已核验字节。
    """

    global_reasons: tuple[str, ...] = ()
    candidate_reasons: Mapping[str, str] = field(default_factory=dict)
    verified_content: Mapping[str, dict] = field(default_factory=dict)


def _evidence_mismatch_reason(ref: Any, parsed: Mapping[str, Any] | None) -> str | None:
    """证据级核对（H5）：单条 fixture 证据对回已核验字节，返回原因或 None。

    悬空 locator 与"摘录 ≠ 规范截断"**同码**（`evidence_mismatch`）不同 detail ——
    rev 2 复核钉死的语义：research_grade 下证据被单独篡改一律走 I10 两态失败，
    不抛输入校验异常（否则 strict=False 的报告契约被绕过）。
    截断规则复用全仓唯一实现 `expected_excerpt_for`（R6）。
    """
    if parsed is None:
        return f"evidence_mismatch:{ref.locator}（候选内容未通过文件级核验）"
    match = SECTION_LOCATOR_PATTERN.match(ref.locator) if isinstance(ref.locator, str) else None
    if match is None:
        return f"evidence_mismatch:{ref.locator}（不支持的 locator 形态）"
    index = int(match.group(1))
    sections = parsed.get("sections")
    if not isinstance(sections, list) or index >= len(sections):
        return f"evidence_mismatch:{ref.locator}（locator 指向不存在的 section）"
    section = sections[index]
    text = section.get("text") if isinstance(section, Mapping) else None
    if not isinstance(text, str):
        return f"evidence_mismatch:{ref.locator}（section 缺少 text）"
    if expected_excerpt_for(text) != ref.excerpt:
        return f"evidence_mismatch:{ref.locator}（摘录与已核验原文的规范截断不一致）"
    return None


def check_i10_non_promotable_support(
    report: SelectionReport,
    snapshot_set: SnapshotSet,
    snapshots: Mapping[str, FixtureSnapshot] | None,
    recheck: BindingRecheck | None = None,
) -> CheckResult:
    """I10（条件性，仅 `policy=research_grade` 时由组装追加）：
    每条 supported finding 的支撑候选必须**可证明可升格**，且证明绑定**本次实际数据**。

    **P2.2-3 复核修正（P1：伪造升格绕过）**：升格判定**不再消费**任何可由调用方
    构造的 `promotable` 映射 —— 那等于让"声明"冒充"证明"。改为在组装期按实际数据重算：

    1. **attestation 权威**：必须存在、只能来自 `load_attestation()`
      （loader-only 绑定：`binding_reason=""`；未绑定/过期/集合不符/未通过
       分别给出 absent / unbound / version_expired / mismatch / incomplete），
       且必须携带 `load_snapshot_set()` 产出的同源绑定上下文（H6）；
    2. **实际快照**：每个候选的 `source_kind` 取自**参与组装的快照本身**——
      只有 `official_documentation_excerpt` 可升格，人工摘要（`authored_summary`）
      无论有无证明都不可升格；
    3. **绑定重验（attempt 3，B6 修复）**：`recheck` 携带组装期"现读现算"的结论 ——
      文件层（磁盘字节摘要 == 证明登记）、声明层（documents == 当前 provenance 并集）、
      内容级（内存快照 == 同缓冲解析结果）、证据级（EvidenceRef 对回已核验字节）；
      任一层失败即 fail-closed，原因可区分（`disk_mismatch` / `tampered_in_memory` /
      `uncovered_candidate` / `evidence_mismatch`）；
    4. **fail-closed** —— 证明缺失、未绑定、版本过期、集合不符、未通过，
       以及任何无法证明可升格的情形，一律判失败并给出原因。

    与旧版一致的三条约束保持不变：只判完整性不改内容（不剔除 supporting、
    不改排名）；明细含 `candidate/dimension/finding/原因`；纯函数、不抛异常。
    """
    if snapshot_set.policy != POLICY_RESEARCH_GRADE:
        # 防御性：本检查只应被 research_grade 组装路径调用；其它策略调用视为配置错误
        return CheckResult(
            "I10_NON_PROMOTABLE_SUPPORT",
            False,
            f"I10 仅适用于 policy=research_grade，实际为 {snapshot_set.policy!r}（fail-closed）",
        )

    blocking = _attestation_blocking_reason(
        snapshot_set, expected_set_id=snapshot_set.snapshot_set_id
    )

    if not snapshots:
        return CheckResult(
            "I10_NON_PROMOTABLE_SUPPORT",
            False,
            "声明了 research_grade 但组装未提供 snapshots，"
            "候选来源种类无法核验（fail-closed）",
        )

    recheck_global = tuple(recheck.global_reasons) if recheck is not None else ()

    # 逐候选按**实际快照**重算可升格性：
    # - 证明维度：attestation 缺失/未绑定/过期/不符/未通过 ⇒ 全部候选不可升格；
    # - 绑定重验维度：S0.5 的文件层/声明层失败作用于全部候选（证明整体失效时不做部分信任），
    #   内容级/覆盖级失败作用于对应候选；
    # - 来源维度：只有官方原文摘录（source_kind 取自**参与组装的真实快照**）可升格。
    # 三个维度**都**核算并写进明细 —— "没有证明"、"字节对不上"、"来源不是官方原文"
    # 是不同的事实，混成一个原因会让审计无法定位。
    non_promotable: dict[str, str] = {}
    for candidate_id in report.candidate_ids:
        snapshot = snapshots.get(candidate_id)
        if snapshot is None:
            # collect_candidate_ids 已保证覆盖；此处仅防御
            non_promotable[candidate_id] = "snapshot_missing"
            continue
        reasons: list[str] = []
        if blocking:
            reasons.append(blocking)
        reasons.extend(recheck_global)
        if recheck is not None:
            candidate_reason = recheck.candidate_reasons.get(candidate_id)
            if candidate_reason:
                reasons.append(candidate_reason)
        if snapshot.source_kind != SOURCE_KIND_OFFICIAL_EXCERPT:
            reasons.append(
                f"source_kind={snapshot.source_kind!r}（人工摘要不可作研究级支撑）"
            )
        if reasons:
            non_promotable[candidate_id] = "; ".join(reasons)

    violations: list[str] = []
    if snapshots is not None and set(snapshots) != set(report.candidate_ids):
        missing = sorted(set(report.candidate_ids) - set(snapshots))
        extra = sorted(set(snapshots) - set(report.candidate_ids))
        violations.append(
            f"candidate_coverage: findings 缺快照={missing}；未参与报告的快照={extra}"
        )
    for finding in report.findings:
        if not finding.is_supported:
            continue
        # 支撑候选 = finding 所属候选 + 其 fixture 证据实际指向的候选
        #（两者在既有 worker 产出中恒相等；分开核对是为了让"证据跨候选引用"
        # 这类未来演化也逃不过检查，而不是只看表面字段）
        backing = {finding.candidate_id}
        for ref in finding.evidence_refs:
            if ref.source_type == SOURCE_FIXTURE and ref.source_id.startswith(
                _FIXTURE_SOURCE_ID_PREFIX
            ):
                backing.add(ref.source_id[len(_FIXTURE_SOURCE_ID_PREFIX) :])
        for candidate_id in sorted(backing):
            reason = non_promotable.get(candidate_id)
            if reason is not None:
                violations.append(
                    f"{candidate_id}/{finding.dimension_id}/{finding.finding_id}: {reason}"
                )

    # H5 证据级（attempt 3）：snapshot 与 attestation 都没变、仅 EvidenceRef 被改 ——
    # 也要对回已核验字节（rev 2 复核钉死的 A6 语义：I10 两态失败，不走异常路径）。
    if recheck is not None:
        for finding in report.findings:
            for ref in finding.evidence_refs:
                if ref.source_type != SOURCE_FIXTURE or not ref.source_id.startswith(
                    _FIXTURE_SOURCE_ID_PREFIX
                ):
                    continue
                candidate_id = ref.source_id[len(_FIXTURE_SOURCE_ID_PREFIX) :]
                if recheck.candidate_reasons.get(candidate_id):
                    # 该候选已在字节层失败：证据级重复报无增益
                    continue
                parsed = recheck.verified_content.get(candidate_id)
                reason = _evidence_mismatch_reason(ref, parsed)
                if reason is not None:
                    violations.append(
                        f"{candidate_id}/{finding.dimension_id}/{finding.finding_id}: {reason}"
                    )

    unique_violations = sorted(set(violations))
    if unique_violations:
        return CheckResult(
            "I10_NON_PROMOTABLE_SUPPORT",
            False,
            f"research_grade 下存在不可升格来源支撑的 supported finding（共 {len(unique_violations)} 条，"
            f"格式 candidate/dimension/finding: 原因）：{unique_violations}",
        )
    return CheckResult(
        "I10_NON_PROMOTABLE_SUPPORT",
        True,
        f"{len(report.candidate_ids)} 个候选均为官方原文摘录且持有有效证明"
        f"（policy=research_grade）",
    )


def verify_integrity(report: SelectionReport) -> IntegrityReport:
    """跑全部 I1–I9，返回汇总（不抛异常，是否中断由调用方决定）。"""
    return IntegrityReport(checks=tuple(checker(report) for checker in INTEGRITY_CHECKERS))

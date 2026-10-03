"""verifier 角色：独立重验上游研究产物（P5）。

"独立"的准确含义（如实声明，不夸大）：
verifier **不信任** researcher 的任何结论字段——它拿到的是
`live-research.report.v1` 载荷（report_json 原文 + content_digest），
并逐项重算：

1. **载荷边界**：schema / 键集合闭集校验（未知字段、缺字段即违例）；
2. **工件完整性**：重算 `sha256(report_json)` 与 `content_digest` 精确比对
   ——载荷被篡改（含"声称 integrity_passed=True 但内容被改"）在这一步被抓；
3. **完整性复核**：严格重建 SelectionReport、重跑 I1–I9，并复用组装逻辑
   核对评分/推荐；自报 passed、空 checks 或自洽重签均不能替代这些校验；
4. **证据结构复核**：每条 supported finding 必须携带非空 evidence_refs，
   且 source_type 在闭集内；
5. **范围复核**：`candidate_ids` 必须落在 verifier 任务卡 context 声明的
   范围内（越界 = 跨候选串证据，拒绝）。

任何一项不通过 ⇒ `verdict="rejected"`（附全部 reasons）。verifier 本身
"工作成功"（返回结构化结果），**拒绝是结论不是异常**——不可信结论被
挡在最终报告之外，由 synthesizer 的失败语义 + 任务板状态机兑现
（synthesizer 见到 rejected ⇒ 卡片失败 ⇒ 重试耗尽 ⇒ blocked）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..contracts import ProjectBrief, WorkerResult, WorkerTaskInput
from ..evidence import ALLOWED_SOURCE_TYPES, SOURCE_FIXTURE, EvidenceRef
from ..research.assemble import build_recommendations
from ..research.config import REPORT_GENERATED_BY, REPORT_SCHEMA_VERSION
from ..research.integrity import INTEGRITY_CODES, CheckResult, IntegrityReport, verify_integrity
from ..research.models import (
    CandidateCoverage,
    ComparisonDimension,
    CoverageStats,
    Recommendation,
    ResearchFinding,
    RiskItem,
    SelectionReport,
)
from ..research.scoring import score_all
from ..util import thaw_payload
from ..workers.external import ADVISOR_PAYLOAD_SCHEMA, validate_advisor_payload
from ..workers.synthetic import SyntheticEvidenceFactory
from .errors import OrchestrationError
from .research_adapter import (
    REPORT_PAYLOAD_KEYS,
    REPORT_PAYLOAD_SCHEMA,
    _canonical_json,
    _check_payload_keys,
)
from .roles import declared_candidates

VERDICT_PASSED = "passed"
VERDICT_REJECTED = "rejected"
VERDICT_PAYLOAD_SCHEMA = "multi_agent.verdict.v1"
VERDICT_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"notes", "schema", "checks", "reasons", "task_refs", "verified_report_id", "verdict"}
)

_REPORT_KEYS = frozenset({
    "schema_version", "generated_by", "brief", "candidate_ids", "dimensions",
    "findings", "risks", "recommendations", "coverage", "report_id",
    "integrity", "evidence_index",
})


def _require_exact_keys(raw: Any, expected: frozenset[str], label: str) -> None:
    if not isinstance(raw, dict) or frozenset(raw) != expected:
        actual = sorted(raw) if isinstance(raw, dict) else type(raw).__name__
        raise ValueError(f"{label} 字段边界违例：实际 {actual!r}，期望 {sorted(expected)!r}")


def _parse_refs(raw: Any, label: str) -> tuple[EvidenceRef, ...]:
    if not isinstance(raw, list):
        raise TypeError(f"{label} 必须是列表")
    refs: list[EvidenceRef] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"{label}[{index}] 必须是对象")
        refs.append(EvidenceRef.from_dict(item))
    return tuple(refs)


def _validate_report_document(report: Any, payload: dict[str, Any], declared: tuple[str, ...]) -> str:
    """把 report_json 还原成 SelectionReport；不信任任何派生 id/统计字段。"""
    if not isinstance(report, dict):
        raise TypeError("报告顶层必须是对象")
    _require_exact_keys(report, _REPORT_KEYS, "SelectionReport")
    if report["schema_version"] != REPORT_SCHEMA_VERSION:
        raise ValueError("报告 schema_version 不匹配")
    if report["generated_by"] != REPORT_GENERATED_BY:
        raise ValueError("报告 generated_by 不匹配")
    brief_raw = report["brief"]
    _require_exact_keys(brief_raw, frozenset({"text", "source"}), "brief")
    brief = ProjectBrief.from_dict(brief_raw)

    candidate_ids = report["candidate_ids"]
    if not isinstance(candidate_ids, list) or not candidate_ids or candidate_ids != sorted(candidate_ids):
        raise ValueError("报告 candidate_ids 必须是非空、排序后的列表")
    if len(set(candidate_ids)) != len(candidate_ids) or any(item not in declared for item in candidate_ids):
        raise ValueError("报告 candidate_ids 越界或重复")
    if payload.get("candidate_ids") != candidate_ids:
        raise ValueError("载荷 candidate_ids 与报告正文不一致")

    dimensions_raw = report["dimensions"]
    if not isinstance(dimensions_raw, list) or not dimensions_raw:
        raise ValueError("报告 dimensions 必须是非空列表")
    dimensions: list[ComparisonDimension] = []
    dimension_keys = frozenset({"dimension_id", "name", "description", "weight", "scale_kind", "scale_labels", "utility_map", "aliases"})
    for item in dimensions_raw:
        _require_exact_keys(item, dimension_keys, "dimension")
        dimensions.append(ComparisonDimension.from_dict(item))
    if [item.dimension_id for item in dimensions] != sorted(item.dimension_id for item in dimensions):
        raise ValueError("报告 dimensions 未排序或有重复")
    if len({item.dimension_id for item in dimensions}) != len(dimensions):
        raise ValueError("报告 dimensions 含重复项")

    findings_raw = report["findings"]
    if not isinstance(findings_raw, list) or not findings_raw:
        raise ValueError("报告 findings 必须是非空列表")
    findings: list[ResearchFinding] = []
    finding_keys = frozenset({"finding_id", "candidate_id", "task_id", "agent_name", "dimension_id", "claim", "value", "status", "confidence", "evidence_refs"})
    for item in findings_raw:
        _require_exact_keys(item, finding_keys, "finding")
        finding = ResearchFinding.from_dict(item)
        if finding.candidate_id not in candidate_ids or finding.dimension_id not in {d.dimension_id for d in dimensions}:
            raise ValueError("finding 引用了范围外候选或未知维度")
        findings.append(finding)
    if [(item.candidate_id, item.dimension_id) for item in findings] != sorted((item.candidate_id, item.dimension_id) for item in findings):
        raise ValueError("报告 findings 未按候选/维度排序")

    risks_raw = report["risks"]
    if not isinstance(risks_raw, list):
        raise TypeError("报告 risks 必须是列表")
    risks: list[RiskItem] = []
    risk_keys = frozenset({"risk_id", "severity", "origin", "rule_id", "candidate_id", "dimension_id", "description", "mitigation", "evidence_refs"})
    for item in risks_raw:
        _require_exact_keys(item, risk_keys, "risk")
        risks.append(RiskItem(
            risk_id=item["risk_id"], severity=item["severity"], origin=item["origin"], rule_id=item["rule_id"],
            candidate_id=item["candidate_id"], dimension_id=item["dimension_id"], description=item["description"],
            mitigation=item["mitigation"], evidence_refs=_parse_refs(item["evidence_refs"], "risk.evidence_refs"),
        ))
    if [item.sort_key for item in risks] != sorted(item.sort_key for item in risks):
        raise ValueError("报告 risks 未按规范排序")

    recommendations_raw = report["recommendations"]
    if not isinstance(recommendations_raw, list) or not recommendations_raw:
        raise ValueError("报告 recommendations 必须是非空列表")
    recommendations: list[Recommendation] = []
    rec_keys = frozenset({"candidate_id", "rank", "score", "rationale", "confidence", "supporting_finding_ids", "counter_finding_ids", "risk_ids", "matched_dimensions", "missing_dimensions", "evidence_refs"})
    for item in recommendations_raw:
        _require_exact_keys(item, rec_keys, "recommendation")
        recommendations.append(Recommendation(
            candidate_id=item["candidate_id"], rank=item["rank"], score=item["score"], rationale=item["rationale"],
            confidence=item["confidence"], supporting_finding_ids=tuple(item["supporting_finding_ids"]),
            counter_finding_ids=tuple(item["counter_finding_ids"]), risk_ids=tuple(item["risk_ids"]),
            matched_dimensions=tuple(item["matched_dimensions"]), missing_dimensions=tuple(item["missing_dimensions"]),
            evidence_refs=_parse_refs(item["evidence_refs"], "recommendation.evidence_refs"),
        ))

    coverage_raw = report["coverage"]
    _require_exact_keys(coverage_raw, frozenset({"candidates"}), "coverage")
    coverage_items: list[CandidateCoverage] = []
    coverage_keys = frozenset({"candidate_id", "total_dimensions", "supported_dimensions", "missing_dimensions", "finding_ids", "trusted_finding_ids", "evidence_by_source_type"})
    for item in coverage_raw["candidates"]:
        _require_exact_keys(item, coverage_keys, "coverage.candidate")
        coverage_items.append(CandidateCoverage(
            candidate_id=item["candidate_id"], total_dimensions=item["total_dimensions"],
            supported_dimensions=item["supported_dimensions"], missing_dimensions=tuple(item["missing_dimensions"]),
            finding_ids=tuple(item["finding_ids"]), trusted_finding_ids=tuple(item["trusted_finding_ids"]),
            evidence_by_source_type=item["evidence_by_source_type"],
        ))
    coverage = CoverageStats(candidates=tuple(coverage_items))

    integrity_raw = report["integrity"]
    _require_exact_keys(integrity_raw, frozenset({"passed", "failed_codes", "checks"}), "integrity")
    checks: list[CheckResult] = []
    for item in integrity_raw["checks"]:
        _require_exact_keys(item, frozenset({"code", "passed", "detail"}), "integrity.check")
        checks.append(CheckResult(code=item["code"], passed=item["passed"], detail=item["detail"]))
    integrity = IntegrityReport(checks=tuple(checks))
    if integrity_raw["passed"] != integrity.passed or integrity_raw["failed_codes"] != list(integrity.failed_codes):
        raise ValueError("integrity 的派生字段不一致")

    selection = SelectionReport(
        brief=brief, candidate_ids=tuple(candidate_ids), dimensions=tuple(dimensions), findings=tuple(findings),
        risks=tuple(risks), recommendations=tuple(recommendations), coverage=coverage, integrity=integrity,
        report_id=report["report_id"],
    )
    # 内容寻址只能证明内容自洽；必须重跑规则，不能采信上游自报的全绿 checks。
    core_checks = tuple(check.code for check in integrity.checks if check.code in INTEGRITY_CODES)
    if core_checks != INTEGRITY_CODES:
        raise ValueError("报告缺少完整、有序的 I1–I9 检查记录")
    recomputed = verify_integrity(selection)
    if not recomputed.passed:
        raise ValueError(f"独立完整性重验失败：{list(recomputed.failed_codes)}")
    for check in recomputed.checks:
        claimed = next(item for item in integrity.checks if item.code == check.code)
        if claimed.passed != check.passed:
            raise ValueError(f"完整性声明与重验结果不一致：{check.code}")
    # 排名连续不代表分数正确，复用组装层的唯一打分/推荐实现核对派生字段。
    scores = score_all(candidate_ids=selection.candidate_ids, dimensions=selection.dimensions,
                       findings=selection.findings)
    expected_recommendations = build_recommendations(
        scores=scores, findings=selection.findings, risks=selection.risks
    )
    if selection.recommendations != expected_recommendations:
        raise ValueError("推荐评分/排名/证据支撑与 findings 重算结果不一致")
    for finding in selection.findings:
        for ref in finding.evidence_refs:
            if ref.source_type == SOURCE_FIXTURE and ref.source_id != f"fixture:{finding.candidate_id}":
                raise ValueError("finding 的证据引用了其它候选的快照")
    raw_index = report["evidence_index"]
    if not isinstance(raw_index, dict):
        raise TypeError("evidence_index 必须是对象")
    expected_index = {key: ref.to_dict() for key, ref in selection.evidence_index.items()}
    if raw_index != expected_index:
        raise ValueError("evidence_index 与报告引用不一致")
    if selection.report_id != payload.get("report_id"):
        raise ValueError("report_id 与载荷声明不一致")
    return selection.report_id


class VerifierWorker:
    """独立重验器（verifier 角色）。`force_reject_reason` 供演练注入。"""

    def __init__(
        self,
        *,
        agent_name: str = "verifier",
        force_reject_reason: str = "",
        source_validator: Any | None = None,
        require_source_audit: bool = False,
    ) -> None:
        self.agent_name = agent_name
        self.force_reject_reason = force_reject_reason
        self._source_validator = source_validator
        self._require_source_audit = require_source_audit
        self.received_inputs: list[WorkerTaskInput] = []

    @property
    def identity(self) -> str:
        reason = f":force_reject={self.force_reject_reason}" if self.force_reject_reason else ""
        return f"verifier.v1:{self.agent_name}{reason}"

    async def run(self, task_input: WorkerTaskInput) -> WorkerResult:
        self.received_inputs.append(task_input)
        declared = declared_candidates(task_input.context)
        checks: dict[str, bool] = {}
        reasons: list[str] = []
        notes: list[str] = []
        verified_report_id = ""
        if not task_input.upstream_results:
            reasons.append("verifier 没有任何上游研究结果（fail-closed）")

        for upstream_id in sorted(task_input.upstream_results):
            upstream = task_input.upstream_results[upstream_id]
            payload = thaw_payload(upstream.structured_payload)
            schema = payload.get("schema") if isinstance(payload, dict) else None
            if schema == ADVISOR_PAYLOAD_SCHEMA:
                # P6：外部模型 worker 的结论——**未验证材料**。
                # 只验结构与范围（闭集键 / unverified 标记 / task_refs ⊆ 声明上游）；
                # 内容没有来源证据可核，verdict=passed 只代表"结构合规且显式
                # 标记未验证"，绝不代表"内容已核实"。
                try:
                    validate_advisor_payload(payload, label=f"上游 {upstream_id}")
                    advisor_ok = True
                except OrchestrationError as exc:
                    advisor_ok = False
                    reasons.append(f"{upstream_id}: {exc}")
                checks[f"{upstream_id}/advisor_structure_ok"] = advisor_ok
                refs = payload.get("task_refs")
                refs_ok = isinstance(refs, list) and set(refs) <= set(
                    task_input.upstream_results
                )
                checks[f"{upstream_id}/advisor_scope_ok"] = refs_ok
                if not refs_ok:
                    reasons.append(
                        f"{upstream_id}: advisor 载荷 task_refs 越权（{refs!r} ⊄ "
                        f"{sorted(task_input.upstream_results)}）"
                    )
                notes.append(
                    f"{upstream_id}: 未验证材料——外部模型结论（{payload.get('summary', '')[:80]}）"
                    "无来源证据，未经人工/来源复核，不构成 research_grade 依据"
                )
                continue
            # 默认分支：live_research.report.v1（Codex 强化后的完整重验路径）
            try:
                payload = _check_payload_keys(payload, label=f"上游 {upstream_id}")
            except OrchestrationError as exc:
                reasons.append(str(exc))
                continue
            report_json = payload["report_json"]
            if not isinstance(report_json, str) or not report_json:
                reasons.append(f"{upstream_id}: report_json 缺失或非字符串")
                continue
            # ② 工件完整性：重算摘要
            digest = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
            checks[f"{upstream_id}/content_digest_ok"] = digest == payload["content_digest"]
            if digest != payload["content_digest"]:
                reasons.append(
                    f"{upstream_id}: content_digest 不符（重算 {digest}，"
                    f"载荷声称 {payload['content_digest']}）——产物被篡改"
                )
            try:
                report = json.loads(report_json)
            except json.JSONDecodeError as exc:
                reasons.append(f"{upstream_id}: report_json 不是合法 JSON（{exc.msg}）")
                continue
            if not isinstance(report, dict):
                reasons.append(f"{upstream_id}: report_json 顶层必须是对象")
                continue
            # ③ 完整性复核（独立于载荷声明）
            try:
                verified_report_id = _validate_report_document(report, payload, declared)
                report_structure_ok = True
            except (TypeError, ValueError, KeyError) as exc:
                report_structure_ok = False
                reasons.append(f"{upstream_id}: 报告结构/内容寻址复核失败（{exc}）")
            checks[f"{upstream_id}/report_structure_ok"] = report_structure_ok
            integrity = report.get("integrity")
            integrity_ok = (
                isinstance(integrity, dict)
                and integrity.get("passed") is True
                and integrity.get("failed_codes") == []
                and payload["integrity_passed"] is True
                and payload["failed_codes"] == []
            )
            checks[f"{upstream_id}/integrity_ok"] = integrity_ok
            if not integrity_ok:
                reasons.append(f"{upstream_id}: 完整性复核未通过（integrity/failed_codes）")
            # ④ 证据结构复核
            evidence_ok = True
            findings = report.get("findings")
            if not isinstance(findings, list):
                evidence_ok = False
                reasons.append(f"{upstream_id}: findings 缺失或非列表")
            else:
                for index, finding in enumerate(findings):
                    if not isinstance(finding, dict) or finding.get("status") != "supported":
                        continue
                    refs = finding.get("evidence_refs")
                    if not isinstance(refs, list) or not refs:
                        evidence_ok = False
                        reasons.append(
                            f"{upstream_id}: supported finding #{index} 没有证据引用"
                        )
                        continue
                    for ref in refs:
                        if not isinstance(ref, dict) or ref.get("source_type") not in (
                            set(ALLOWED_SOURCE_TYPES)
                        ):
                            evidence_ok = False
                            reasons.append(
                                f"{upstream_id}: finding #{index} 的证据来源类型不在闭集内"
                            )
                            break
                        candidate_id = finding.get("candidate_id")
                        if (
                            isinstance(candidate_id, str)
                            and candidate_id not in payload["candidate_ids"]
                        ):
                            evidence_ok = False
                            reasons.append(
                                f"{upstream_id}: finding #{index} 引用了范围外的候选"
                                f" {candidate_id!r}（跨候选串证据）"
                            )
                            break
            checks[f"{upstream_id}/evidence_ok"] = evidence_ok
            # ⑤ 范围复核
            claimed_candidates = payload["candidate_ids"]
            scope_ok = (
                isinstance(claimed_candidates, list)
                and bool(claimed_candidates)
                and all(item in declared for item in claimed_candidates)
            )
            checks[f"{upstream_id}/scope_ok"] = scope_ok
            if not scope_ok:
                reasons.append(
                    f"{upstream_id}: 候选范围越界（载荷 {claimed_candidates!r} ⊄ 声明 {list(declared)}）"
                )
            # ⑥ P6 来源审计复核（live 模式才有 source_audit；离线脚本模式无此键）
            source_audit = payload.get("source_audit")
            if source_audit is not None:
                audit_ok, audit_reasons = _verify_source_audit(source_audit, declared)
                if self._source_validator is None:
                    audit_ok = False
                    audit_reasons.append("缺少独立原文核验器，不能采信来源声明")
                elif audit_ok:
                    audit_ok, binding_reasons = self._source_validator(report, source_audit)
                    audit_reasons.extend(binding_reasons)
                checks[f"{upstream_id}/source_audit_ok"] = audit_ok
                reasons.extend(audit_reasons)
                if audit_ok:
                    notes.append(
                        f"{upstream_id}: 来源审计 {len(source_audit)} 件与本次原文字节和证据逐条绑定通过；"
                        "仅证明材料来源与结构，不证明模型推论正确，不构成 research_grade"
                    )
            elif self._require_source_audit:
                reasons.append(f"{upstream_id}: live 报告缺少 source_audit")
                checks[f"{upstream_id}/source_audit_ok"] = False

        if self.force_reject_reason:
            reasons.append(f"演练注入的强制拒绝：{self.force_reject_reason}")
        verdict = VERDICT_PASSED if not reasons else VERDICT_REJECTED
        payload = {
            "notes": notes,
            "schema": VERDICT_PAYLOAD_SCHEMA,
            "checks": _canonical_checks(checks),
            "reasons": reasons,
            "task_refs": sorted(task_input.upstream_results),
            "verified_report_id": verified_report_id,
            "verdict": verdict,
        }
        facts = tuple(
            f"check {key}={'ok' if value else 'FAILED'}" for key, value in payload["checks"].items()
        ) or ("no checks executed",)
        if verdict == VERDICT_REJECTED:
            facts = (*facts, *(f"reason: {reason}" for reason in reasons))
        return WorkerResult(
            task_id=task_input.task_id,
            agent_name=self.agent_name,
            facts=facts,
            evidence=SyntheticEvidenceFactory.for_task(self.agent_name, task_input.task_id, count=1),
            risks=() if verdict == VERDICT_PASSED else ("上游产物未通过独立重验",),
            open_questions=(),
            confidence=0.95 if verdict == VERDICT_PASSED else 0.6,
            summary=f"verification verdict={verdict}",
            session_id=f"verify-{task_input.task_id}",
            structured_payload=payload,
        )


def _canonical_checks(checks: dict[str, bool]) -> dict[str, bool]:
    return {key: checks[key] for key in sorted(checks)}


_SOURCE_AUDIT_ENTRY_KEYS = frozenset(
    {"byte_size", "commit", "content_sha256", "candidate_id", "path", "repo", "source_url", "blob_sha"}
)


def _verify_source_audit(
    source_audit: Any, declared: tuple[str, ...]
) -> tuple[bool, list[str]]:
    """P6 来源审计复核：候选归属 / 固定 commit / 摘要形态（逐条，fail-closed）。

    注意如实边界：这里的 sha256 是**检索层**测量的原文摘要；"重新取回同
    固定 commit URL 重算"才能构成完整重验（记录在 verdict notes 中）。
    """
    reasons: list[str] = []
    if not isinstance(source_audit, list) or not source_audit:
        return False, ["source_audit 必须是非空列表"]
    seen_keys: set[tuple[str, str, str, str]] = set()
    for index, entry in enumerate(source_audit):
        if not isinstance(entry, dict):
            reasons.append(f"source_audit[{index}] 必须是对象")
            continue
        unknown = sorted(set(entry) - _SOURCE_AUDIT_ENTRY_KEYS)
        missing = sorted(_SOURCE_AUDIT_ENTRY_KEYS - set(entry))
        if unknown or missing:
            reasons.append(
                f"source_audit[{index}] 字段边界违例（未知 {unknown} / 缺失 {missing}）"
            )
            continue
        candidate_id = entry["candidate_id"]
        if candidate_id not in declared:
            reasons.append(
                f"source_audit[{index}] 候选 {candidate_id!r} 越界（声明 {list(declared)}）"
            )
        sha = entry["content_sha256"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            reasons.append(f"source_audit[{index}] content_sha256 形态非法：{sha!r}")
        url = entry["source_url"]
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            reasons.append(f"source_audit[{index}] source_url 必须是 github.com 固定 commit URL")
        else:
            match = re.match(r"^https://github.com/[^/]+/[^/]+/blob/([0-9a-f]{40})/", url)
            if match is None:
                reasons.append(
                    f"source_audit[{index}] source_url 未内嵌 40 位固定 commit（不可重验）：{url!r}"
                )
        commit = entry["commit"]
        if not isinstance(commit, str) or not re.match(r"^[0-9a-f]{40}$", commit):
            reasons.append(f"source_audit[{index}] commit 形态非法：{commit!r}")
        if type(entry["byte_size"]) is not int or entry["byte_size"] < 1:
            reasons.append("byte_size 必须为正整数")
        if not isinstance(entry["blob_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", entry["blob_sha"]):
            reasons.append("blob_sha 形态非法")
        expected_url = f"https://github.com/{entry['repo']}/blob/{commit}/{entry['path']}"
        if url != expected_url:
            reasons.append("来源 URL 与 repo/commit/path 不一致")
        key = (str(candidate_id), str(entry["repo"]), str(entry["path"]), str(commit))
        if key in seen_keys:
            reasons.append(f"source_audit[{index}] 重复来源：{key}")
        seen_keys.add(key)
    return (not reasons), reasons


def validate_verdict_payload(payload: Any, *, label: str) -> dict[str, Any]:
    """verdict 载荷闭集校验（orchestrator / synthesizer 共用；R6）。"""
    if not isinstance(payload, dict) or payload.get("schema") != VERDICT_PAYLOAD_SCHEMA:
        raise OrchestrationError(
            f"{label}: structured_payload 缺失或 schema 不是 {VERDICT_PAYLOAD_SCHEMA}"
        )
    keys = frozenset(payload)
    unknown = sorted(keys - VERDICT_PAYLOAD_KEYS)
    missing = sorted(VERDICT_PAYLOAD_KEYS - keys)
    if unknown or missing:
        raise OrchestrationError(
            f"{label}: verdict 载荷字段边界违例（未知 {unknown} / 缺失 {missing}）"
        )
    if payload["verdict"] not in (VERDICT_PASSED, VERDICT_REJECTED):
        raise OrchestrationError(
            f"{label}: verdict 必须是 {VERDICT_PASSED}/{VERDICT_REJECTED}，"
            f"实际 {payload['verdict']!r}"
        )
    return payload


__all__ = [
    "REPORT_PAYLOAD_KEYS",
    "REPORT_PAYLOAD_SCHEMA",
    "VERDICT_PASSED",
    "VERDICT_PAYLOAD_KEYS",
    "VERDICT_PAYLOAD_SCHEMA",
    "VERDICT_REJECTED",
    "VerifierWorker",
    "_canonical_json",
    "validate_verdict_payload",
]


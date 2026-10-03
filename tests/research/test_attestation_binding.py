"""P2.2-3 attempt 3：证明与**本次实际数据**的绑定（B6 修复的攻击矩阵回归）。

对应 `P2.2-3_REDESIGN.md` rev2 的攻击矩阵（P0 正控 + A1–A6 负控），每条负控钉一个
机制生效点（R7）；每条失败场景同时断言 `strict=True` / `strict=False` 两态行为与
**内容契约**（失败报告内容不清洗、两态 `report_id` 相同）—— R1 的"证明必须绑定
本次实际使用的数据"在这里变成可执行断言。

全部合成语料在 tmp 落盘（走真 loader），绝不触碰 `fixtures/**`，
也绝不创建正式 `attestations/`。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentboard.contracts import ProjectBrief, TaskCard
from agentboard.research.assemble import assemble_report
from agentboard.research.attestation import (
    SourceVerificationAttestation,
    load_attestation,
)
from agentboard.research.config import (
    POLICY_RESEARCH_GRADE,
    REASON_INCOMPLETE,
    REASON_MISMATCH,
    REASON_UNBOUND,
    REASON_VERSION_EXPIRED,
)
from agentboard.research.errors import (
    AttestationSchemaError,
    DanglingLocatorError,
    ReportIntegrityError,
    SnapshotSchemaError,
)
from agentboard.research.snapshot import SnapshotSet, load_snapshot_set
from agentboard.research.sources import load_official_sources

I10 = "I10_NON_PROMOTABLE_SUPPORT"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_ROOT = PROJECT_ROOT / "fixtures" / "official"
AUDIT_RAW_DIR = PROJECT_ROOT / ".workbuddy" / "tmp" / "audit"


def _cards_with(sources: Any, snapshots: Any) -> list[TaskCard]:
    """用**给定快照**跑 worker 产出卡片（B6 复现形态：内存自洽的篡改）。"""
    import asyncio

    from agentboard.research.fixtures import FixtureResearchWorker
    from agentboard.research.sources import make_task_input

    worker = FixtureResearchWorker(sources.catalog, sources.registry, snapshots)
    cards: list[TaskCard] = []
    for candidate_id in sources.candidate_ids():
        task_input = make_task_input(candidate_id)
        cards.append(
            TaskCard(
                task_id=task_input.task_id,
                title=task_input.title,
                assigned_agent=task_input.assigned_agent,
                goal=task_input.goal,
                context=task_input.context,
                status="done",
                result=asyncio.run(worker.run(task_input)),
                attempts=1,
            )
        )
    return cards


def _brief() -> ProjectBrief:
    return ProjectBrief(
        text="为检索服务做技术选型（依据官方文档快照）",
        source="docs/research/selection_goal.md",
    )


def _cards_from(sources: Any) -> list[TaskCard]:
    import asyncio

    from agentboard.research.sources import make_task_input

    worker = sources.worker()
    cards: list[TaskCard] = []
    for candidate_id in sources.candidate_ids():
        task_input = make_task_input(candidate_id)
        cards.append(
            TaskCard(
                task_id=task_input.task_id,
                title=task_input.title,
                assigned_agent=task_input.assigned_agent,
                goal=task_input.goal,
                context=task_input.context,
                status="done",
                result=asyncio.run(worker.run(task_input)),
                attempts=1,
            )
        )
    return cards


def _assert_i10_fail_both_modes(
    brief: ProjectBrief,
    cards: list[TaskCard],
    registry: Any,
    snapshots: Any,
    snapshot_set: Any,
    *,
    expect_in_detail: tuple[str, ...],
) -> Any:
    """两态断言 + 内容契约（R1）：strict 抛错 / 非严格返回未通过报告；内容不清洗。"""
    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(brief, cards, registry, snapshots=snapshots, snapshot_set=snapshot_set)
    assert I10 in excinfo.value.failed_codes

    failed = assemble_report(
        brief, cards, registry, snapshots=snapshots, snapshot_set=snapshot_set, strict=False
    )
    assert failed.integrity.passed is False
    assert I10 in failed.integrity.failed_codes
    detail = failed.integrity.detail_of(I10)
    for needle in expect_in_detail:
        assert needle in detail, f"明细必须包含 {needle!r}，实际：{detail}"

    # 内容契约：失败报告的内容与"同内容默认组装"一致（不清洗/不过滤）
    default = assemble_report(brief, cards, registry, snapshots=snapshots)
    assert default.integrity.passed is True, "测试前提：同内容默认组装必须完整通过"
    assert failed.report_id == default.report_id
    assert failed.findings == default.findings
    assert failed.recommendations == default.recommendations
    return failed


def _assert_p0_passes(brief: ProjectBrief, cards: list[TaskCard], registry: Any,
                      snapshots: Any, snapshot_set: Any) -> None:
    graded = assemble_report(brief, cards, registry, snapshots=snapshots, snapshot_set=snapshot_set)
    default = assemble_report(brief, cards, registry, snapshots=snapshots)
    assert graded.integrity.passed is True
    assert graded.report_id == default.report_id
    non_strict = assemble_report(
        brief, cards, registry, snapshots=snapshots, snapshot_set=snapshot_set, strict=False
    )
    assert non_strict.integrity.passed is True
    assert non_strict.report_id == graded.report_id


# ---- P0：正控（真实官方集；原件缺失时由 P0b 合成集无条件覆盖） ----


def test_p0_real_official_set_passes_with_bound_context(
    verify_tool: Any, tmp_path: Path
) -> None:
    """真实官方目录 + 真工具生成的 passed 证明 + `load_snapshot_set` ⇒ 通过。"""
    if not AUDIT_RAW_DIR.is_dir():
        pytest.skip(f"真实原件目录不存在（{AUDIT_RAW_DIR}）；主路径由 P0b 合成集无条件覆盖")
    result = verify_tool.verify_sources(OFFICIAL_ROOT, AUDIT_RAW_DIR)
    assert result.passed()
    payload = verify_tool.build_attestation_payload(OFFICIAL_ROOT, result)
    target = tmp_path / "official.sources.v1.json"
    ok, message = verify_tool.write_attestation(target, verify_tool.attestation_bytes(payload))
    assert ok, message

    snapshot_set = load_snapshot_set(
        fixture_root=OFFICIAL_ROOT, attestation_path=target, policy=POLICY_RESEARCH_GRADE
    )
    assert snapshot_set.binding_context is not None
    src = load_official_sources(OFFICIAL_ROOT)
    _assert_p0_passes(_brief(), _cards_from(src), src.registry, src.snapshots, snapshot_set)


def test_p0b_synthetic_official_set_passes_unconditionally(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """P0 的确定性版本（不依赖本机原件）：合成集 + 工厂 ⇒ 通过。"""
    attestation_path = attestation_generator()
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_path,
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    _assert_p0_passes(_brief(), _cards_from(src), src.registry, src.snapshots, snapshot_set)


# ---- A1：内存改 section 文本（B6 原路径） ----


def test_a1_in_memory_section_tamper_is_rejected(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """B6 回归：attestation 与磁盘都没变，只改内存快照的 section 文本 ⇒ 必须失败。

    机制：H4 内容级现读现比 —— 内存快照必须与同缓冲解析结果逐字段一致。
    """
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    tampered = dict(src.snapshots)
    section = tampered["pgvector"].sections[0]
    tampered_section = replace(section, text=section.text + " TAMPERED")
    tampered["pgvector"] = replace(
        src.snapshots["pgvector"], sections=(tampered_section,)
    )
    # B6 复现形态：卡片由**被篡改的内存快照**重新生成 —— 证据与内存自洽，
    # 但与已核验的磁盘字节不符（attempt 2 的 I10 正是这样被重放绕过的）。
    cards = _cards_with(src, tampered)
    _assert_i10_fail_both_modes(
        _brief(),
        cards,
        src.registry,
        tampered,
        snapshot_set,
        expect_in_detail=("tampered_in_memory", "pgvector"),
    )


# ---- A2：旧证明重放 / 换目录（含同 snapshot_set_id） ----


def test_a2_replay_to_different_data_is_rejected(
    make_synthetic_official_set: Any, attestation_generator: Any, resign_fixture: Any
) -> None:
    """attestation 绑定目录 A，消费目录 B 的数据（同 set_id）⇒ 必须失败。

    - 工厂层：`load_snapshot_set(B.root, attestation_of_A)` 直接绑定失败（字节不同）；
    - 组装层：root 固定为 A，B 的（已改写并重签的）数据进入内容级比对 ⇒ tampered_in_memory。
      相同 `snapshot_set_id` 只是标签，不构成证明（R1 显式清单）。
    """
    set_a = make_synthetic_official_set("set-a")
    set_b = make_synthetic_official_set("set-b")
    assert (
        set_a.attestation_path
        == set_a.root.parent / "attestations" / "official.sources.v1.json"
    ), "测试前提自检"
    assert (
        json.loads(set_a.index_path.read_text(encoding="utf-8"))["snapshot_set"]
        == "official.2026-09-23"
        == json.loads(set_b.index_path.read_text(encoding="utf-8"))["snapshot_set"]
    ), "测试前提：两个目录声明相同的快照集标识（标签相同 ≠ 数据相同）"

    attestation_path = attestation_generator()  # 为 A 生成
    snapshot_set = load_snapshot_set(
        fixture_root=set_a.root, attestation_path=attestation_path,
        policy=POLICY_RESEARCH_GRADE,
    )

    # 改 B 的快照字节并重签 B 的 INDEX（B 的数据从此 ≠ A 的数据）
    set_b.rewrite_snapshot(
        lambda payload: payload["sections"][0].update({"text": "REPLAYED CONTENT"}),
        candidate_id="pgvector",
    )
    resign_fixture(set_b.root)

    # 工厂层重放：A 的证明对 B 的目录做绑定 ⇒ 拒绝（fail-closed，无半成品）
    with pytest.raises(SnapshotSchemaError, match="绑定未通过"):
        load_snapshot_set(
            fixture_root=set_b.root, attestation_path=attestation_path,
            policy=POLICY_RESEARCH_GRADE,
        )

    # 组装层重放：root 固定 A，消费 B 的数据 ⇒ 内容级比对抓住
    src_b = load_official_sources(set_b.root)
    _assert_i10_fail_both_modes(
        _brief(),
        _cards_from(src_b),
        src_b.registry,
        src_b.snapshots,
        snapshot_set,
        expect_in_detail=("tampered_in_memory", "pgvector"),
    )


# ---- A2b：绑定后磁盘被换（TOCTOU） ----


def test_a2b_disk_replaced_after_binding_is_rejected(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """`load_snapshot_set` 之后、组装之前改写磁盘字节 ⇒ 组装期现算摘要 ≠ 登记。"""
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)  # 绑定后、篡改前加载
    synthetic_official_set.rewrite_snapshot(
        lambda payload: payload["sections"][0].update({"text": "REPLACED AFTER BINDING"}),
        candidate_id="pgvector",
    )
    _assert_i10_fail_both_modes(
        _brief(),
        _cards_from(src),
        src.registry,
        src.snapshots,
        snapshot_set,
        expect_in_detail=("disk_mismatch", "pgvector"),
    )


# ---- A3：覆盖不全（戳印缺失 / 超出覆盖集 / 组装集小于证明集） ----


def test_a3_uncovered_candidates_are_fail_closed(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """戳印缺失 / 戳印不在证明覆盖集内 ⇒ `uncovered_candidate`（fail-closed）。"""
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)

    stripped = dict(src.snapshots)
    stripped["pgvector"] = replace(src.snapshots["pgvector"], fixture_relpath="")
    _assert_i10_fail_both_modes(
        _brief(), _cards_from(src), src.registry, stripped, snapshot_set,
        expect_in_detail=("uncovered_candidate", "pgvector"),
    )

    ghost = dict(src.snapshots)
    ghost["pgvector"] = replace(
        src.snapshots["pgvector"], fixture_relpath="sources/ghost.json"
    )
    _assert_i10_fail_both_modes(
        _brief(), _cards_from(src), src.registry, ghost, snapshot_set,
        expect_in_detail=("uncovered_candidate", "ghost"),
    )


def test_a3_partial_assembly_is_rejected_by_coverage_guard(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """证明覆盖集 > 组装集：候选覆盖守卫先拒绝（既有 fail-closed 语义）。"""
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    partial_snapshots = {"pgvector": src.snapshots["pgvector"]}
    cards = _cards_from(src)
    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(
            _brief(), cards, src.registry, snapshots=partial_snapshots,
            snapshot_set=snapshot_set,
        )
    assert I10 in excinfo.value.failed_codes

    failed = assemble_report(
        _brief(), cards, src.registry, snapshots=partial_snapshots,
        snapshot_set=snapshot_set, strict=False,
    )
    assert failed.integrity.passed is False
    assert I10 in failed.integrity.failed_codes
    assert "candidate_coverage" in failed.integrity.detail_of(I10)
    full_content = assemble_report(_brief(), cards, src.registry, snapshots=src.snapshots)
    assert failed.report_id == full_content.report_id
    assert failed.findings == full_content.findings
    assert failed.recommendations == full_content.recommendations


# ---- A4：attestation 不完整（三层负控） ----


def test_a4a_empty_documents_rejected_at_parse(
    synthetic_official_set: Any, attestation_generator: Any, tmp_path: Path
) -> None:
    """复核独立复现的缺口：`documents=[]` ⇒ **解析层即拒**（rev 2 schema 加固）。"""
    payload = json.loads(attestation_generator().read_text(encoding="utf-8"))
    payload["documents"] = []
    target = tmp_path / "empty-docs.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(AttestationSchemaError, match="非空列表"):
        SourceVerificationAttestation.from_dict(payload)
    with pytest.raises(AttestationSchemaError, match="非空列表"):
        load_attestation(target, fixture_root=synthetic_official_set.root)


def _mutated_attestation(
    synthetic_official_set: Any,
    attestation_generator: Any,
    tmp_path: Path,
    name: str,
    mutate: Any,
) -> Path:
    """生成一份有效证明后，**改 documents 内容**并落盘（文件层锚点不受影响）。"""
    payload = json.loads(attestation_generator().read_text(encoding="utf-8"))
    mutate(payload)
    target = tmp_path / name
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def test_a4b_missing_document_is_mismatch(
    synthetic_official_set: Any, attestation_generator: Any, tmp_path: Path
) -> None:
    """缺一份已声明文档 ⇒ 文件层绑定仍通过（钉住缺口），组装期 H3b 判 `mismatch`。"""
    target = _mutated_attestation(
        synthetic_official_set, attestation_generator, tmp_path, "missing-doc.json",
        lambda p: p["documents"].pop(),
    )
    attestation = load_attestation(target, fixture_root=synthetic_official_set.root)
    assert attestation.binding_reason == "", (
        "测试前提：文件层绑定不看 documents —— 这正是 H3b 要补的层"
    )
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root, attestation_path=target,
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    _assert_i10_fail_both_modes(
        _brief(), _cards_from(src), src.registry, src.snapshots, snapshot_set,
        expect_in_detail=(REASON_MISMATCH, "provenance"),
    )


def test_a4c_result_and_digest_mismatch_are_distinguishable(
    synthetic_official_set: Any, attestation_generator: Any, tmp_path: Path
) -> None:
    """`result != passed` ⇒ `incomplete`；摘要与声明不符 ⇒ `mismatch`（可区分）。"""
    src = load_official_sources(synthetic_official_set.root)

    result_target = _mutated_attestation(
        synthetic_official_set, attestation_generator, tmp_path, "result.json",
        lambda p: p["documents"][0].update({"result": "incomplete"}),
    )
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root, attestation_path=result_target,
        policy=POLICY_RESEARCH_GRADE,
    )
    failed = _assert_i10_fail_both_modes(
        _brief(), _cards_from(src), src.registry, src.snapshots, snapshot_set,
        expect_in_detail=(REASON_INCOMPLETE,),
    )
    # 原因可区分：result 不符不会同时报成 mismatch
    failed_codes_detail = failed.integrity.detail_of(I10)
    assert "登记摘要与当前 provenance 声明不一致" not in failed_codes_detail

    digest_target = _mutated_attestation(
        synthetic_official_set, attestation_generator, tmp_path, "digest.json",
        lambda p: p["documents"][0].update({"raw_sha256": "f" * 64}),
    )
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root, attestation_path=digest_target,
        policy=POLICY_RESEARCH_GRADE,
    )
    _assert_i10_fail_both_modes(
        _brief(), _cards_from(src), src.registry, src.snapshots, snapshot_set,
        expect_in_detail=(REASON_MISMATCH, "doc_"),
    )


def test_a4d_version_expired_is_distinguishable(
    synthetic_official_set: Any, attestation_generator: Any, tmp_path: Path
) -> None:
    """`verifier_version` 过期 ⇒ `version_expired`（既有原因，不受本轮改动影响）。"""
    target = _mutated_attestation(
        synthetic_official_set, attestation_generator, tmp_path, "expired.json",
        lambda p: p.update({"verifier_version": 99}),
    )
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root, attestation_path=target,
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    _assert_i10_fail_both_modes(
        _brief(), _cards_from(src), src.registry, src.snapshots, snapshot_set,
        expect_in_detail=(REASON_VERSION_EXPIRED,),
    )


# ---- A5：未绑定证明 ----


def test_a5_parsed_only_attestation_is_unbound(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """`from_dict` 产物（binding is None 且无绑定上下文）⇒ `unbound`（fail-closed）。"""
    payload = json.loads(attestation_generator().read_text(encoding="utf-8"))
    parsed_only = SourceVerificationAttestation.from_dict(payload)
    declared = SnapshotSet(
        snapshot_set_id=parsed_only.snapshot_set_id,
        policy=POLICY_RESEARCH_GRADE,
        attestation=parsed_only,
    )
    src = load_official_sources(synthetic_official_set.root)
    _assert_i10_fail_both_modes(
        _brief(), _cards_from(src), src.registry, src.snapshots, declared,
        expect_in_detail=(REASON_UNBOUND,),
    )


# ---- A6：仅篡改 EvidenceRef（snapshot / attestation 均不变） ----


def _tamper_first_evidence(cards: list[TaskCard], *, dangle: bool) -> list[TaskCard]:
    """经公开数据通路篡改一条 EvidenceRef（excerpt 或 locator），重算 digest/evidence_id。

    模拟"攻击者完全控制这条证据"的形态：digest / evidence_id 置空后由构造器重算，
    证据自身自洽 —— 但它与已核验的磁盘字节不再对应（H5 的管辖范围）。
    """
    from agentboard.util import thaw_payload

    tampered: list[TaskCard] = []
    hit = False
    for card in cards:
        if hit or card.result is None:
            tampered.append(card)
            continue
        payload = thaw_payload(card.result.structured_payload)
        for finding in payload.get("findings", []):
            refs = finding.get("evidence_refs", [])
            if not refs:
                continue
            ref = refs[0]
            if dangle:
                ref["locator"] = "/sections/99/text"
            else:
                ref["excerpt"] = ref["excerpt"][:-1] + (
                    "X" if ref["excerpt"][-1] != "X" else "Y"
                )
            ref["digest"] = ""
            ref["evidence_id"] = ""
            # finding_id 是内容寻址的（含 evidence_ids）—— 篡改证据后必须留空重算，
            # 否则 decode 期"伪造 id"校验先炸，测不到真正要测的 I10 语义。
            finding["finding_id"] = ""
            hit = True
            break
        tampered.append(
            dataclasses.replace(
                card,
                result=dataclasses.replace(card.result, structured_payload=payload),
            )
        )
    assert hit, "测试前提：至少有一条 fixture 证据可篡改"
    return tampered


def test_a6_evidence_ref_only_tamper_is_i10_not_exception(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """rev 2 钉死的 A6 语义：仅篡改 EvidenceRef ⇒ research_grade 走 **I10 两态失败**。

    - `strict=True`：抛既有 `ReportIntegrityError`（含 I10），不抛输入校验异常；
    - `strict=False`：返回 `passed=False` 的报告，原因 `evidence_mismatch`，内容不清洗；
    - 对照：默认路径同数据仍抛既有 `DanglingLocatorError`（P1A 行为不变）。
    """
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    cards = _tamper_first_evidence(_cards_from(src), dangle=False)

    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(
            _brief(), cards, src.registry, snapshots=src.snapshots, snapshot_set=snapshot_set
        )
    assert I10 in excinfo.value.failed_codes

    failed = assemble_report(
        _brief(), cards, src.registry, snapshots=src.snapshots,
        snapshot_set=snapshot_set, strict=False,
    )
    assert failed.integrity.passed is False
    detail = failed.integrity.detail_of(I10)
    assert "evidence_mismatch" in detail
    assert "/sections/" in detail, "明细必须定位到被篡改证据的 locator"

    # 对照：默认路径（不做 S0.5）保持既有输入校验异常 —— 语义拆分是显式的
    with pytest.raises(DanglingLocatorError):
        assemble_report(_brief(), cards, src.registry, snapshots=src.snapshots)


def test_a6_dangling_locator_is_also_evidence_mismatch(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """悬空 locator 与截断不等**同码**（`evidence_mismatch`）不同 detail。"""
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    cards = _tamper_first_evidence(_cards_from(src), dangle=True)
    failed = assemble_report(
        _brief(), cards, src.registry, snapshots=src.snapshots,
        snapshot_set=snapshot_set, strict=False,
    )
    assert failed.integrity.passed is False
    assert "evidence_mismatch" in failed.integrity.detail_of(I10)
    assert "不存在" in failed.integrity.detail_of(I10)


# ---- 单次读取契约（H2）：摘要与解析共用同一缓冲，TOCTOU 不可表达 ----


def test_each_fixture_file_is_read_exactly_once_per_assembly(
    synthetic_official_set: Any, attestation_generator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S0.5 内每个文件（INDEX + 各快照）恰好 `read_bytes()` 一次。"""
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)

    real_read = Path.read_bytes
    counts: dict[str, int] = {}

    def counting(self: Path) -> bytes:
        key = str(self)
        counts[key] = counts.get(key, 0) + 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    try:
        report = assemble_report(
            _brief(), _cards_from(src), src.registry, snapshots=src.snapshots,
            snapshot_set=snapshot_set,
        )
    finally:
        monkeypatch.undo()
    assert report.integrity.passed is True

    watched = {
        str(synthetic_official_set.root / "INDEX.json"),
        *(str(path) for path in synthetic_official_set.fixture_paths.values()),
    }
    assert set(counts) == watched, f"S0.5 只应读这些文件，实际读了：{sorted(counts)}"
    for path, count in counts.items():
        assert count == 1, f"{path} 在单次组装内被读取了 {count} 次（违反单次读取契约）"


def test_toctou_second_read_is_not_expressible(
    synthetic_official_set: Any, attestation_generator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反空转对照：若实现"两开"（摘要读旧字节、解析读新字节），攻击才可表达。

    单次读取契约下，第二次读取根本不发生 —— 磁盘上"第二次才会出现的篡改字节"
    无法同时进入摘要与解析，摘要/解析/内容比对三者必然同源。
    """
    snapshot_set = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_generator(),
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(synthetic_official_set.root)
    target = synthetic_official_set.fixture_paths["pgvector"]
    tampered_bytes = json.dumps(
        {
            **json.loads(target.read_text(encoding="utf-8")),
            "display_name": "TWO-TIMING DISK",
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8") + b"\n"

    real_read = Path.read_bytes
    calls: dict[str, int] = {}

    def two_timing(self: Path) -> bytes:
        key = str(self)
        calls[key] = calls.get(key, 0) + 1
        data = real_read(self)
        if key == str(target) and calls[key] > 1:
            return tampered_bytes
        return data

    monkeypatch.setattr(Path, "read_bytes", two_timing)
    try:
        report = assemble_report(
            _brief(), _cards_from(src), src.registry, snapshots=src.snapshots,
            snapshot_set=snapshot_set,
        )
    finally:
        monkeypatch.undo()
    # 篡改字节从未进入判定：摘要、解析、内容比对全部基于第一次读取的同源缓冲
    assert calls[str(target)] == 1
    assert report.integrity.passed is True

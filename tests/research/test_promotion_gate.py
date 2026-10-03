"""升格闸门 `is_promotable_source()` / `PromotionChecks`（P2.2-1。

对应 P2.2_PLAN §2.7 的 17 条用例。核心口径：

**"可信" ≠ "可作研究级支撑"。** 既有 `is_trusted_evidence()` 判的是
"证据来自 fixture 快照"（两个目录都满足）；本文件测的是更严的那一层：
"官方原文 + provenance 齐备 + C1–C5/C7/C8 全通过 + **有效的外部证明**"。

真值来源全部是**真实 fixture 的字节**或**自洽的合成快照集**（`synthetic_official_set`
+ 用真工具生成的证明），不用手拼 JSON —— 手拼会让"工具与库对 schema 的理解是否一致"
这个最容易漂移的地方失去覆盖。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentboard.evidence import MAX_EXCERPT_CHARS
from agentboard.research.attestation import (
    load_attestation,
    promotion_blocking_reason,
)
from agentboard.research.config import (
    EXPECTED_VERIFIER_VERSION,
    REASON_ABSENT,
    REASON_INCOMPLETE,
    REASON_MISMATCH,
    REASON_NOT_APPLICABLE,
    REASON_UNBOUND,
    REASON_VERSION_EXPIRED,
    RESULT_INCOMPLETE,
    SOURCE_KIND_AUTHORED_SUMMARY,
    SOURCE_KIND_OFFICIAL_EXCERPT,
)
from agentboard.research.errors import PromotionError, UnknownSourceKindError
from agentboard.research.snapshot import (
    PromotableExcerpt,
    PromotionChecks,
    promote_excerpt,
    section_locator,
    snapshot_excerpt,
)
from agentboard.research.sources import (
    declared_document_digests,
    evidence_from_excerpt,
    is_promotable_source,
    load_official_sources,
    promotion_checks_for,
    snapshot_set_document_digests,
)

ALL_TRUE = PromotionChecks.all_true()
ONE_FALSE_AT_A_TIME = ("c1", "c2", "c3", "c4", "c5", "c7", "c8")


# ---- 公共输入装配 ----


def _inputs(root: Path) -> dict[str, Any]:
    """把 `is_promotable_source` 需要的四项从真实目录装配出来（只读）。"""
    sources = load_official_sources(root)
    candidate_id = sources.candidate_ids()[0]
    snapshot = sources.snapshot(candidate_id)
    return {
        "source_kind": snapshot.source_kind,
        "has_provenance": True,
        "checks": promotion_checks_for(sources, candidate_id, fixture_root=root),
        "expected_snapshot_set_id": sources.catalog.snapshot_set,
        "declared_documents": snapshot_set_document_digests(sources),
    }


def _edit_json(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


# ---- 用例 1：真值路径（合成自洽集 + 真工具生成的证明） ----


def test_synthetic_corpus_passes_every_load_time_check(synthetic_official_set: Any) -> None:
    """先钉住前提：合成快照集必须 C1–C8 全通过，否则后面的负例失去意义。"""
    checks = promotion_checks_for(
        load_official_sources(synthetic_official_set.root),
        "pgvector",
        fixture_root=synthetic_official_set.root,
    )
    assert checks.failed() == ()
    assert checks.all_passed() is True


def test_promotable_true_with_valid_attestation(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """①官方原文 + 有 provenance + 闸门全真 + `passed` 证明 → True。"""
    attestation_path = attestation_generator()
    attestation = load_attestation(attestation_path, fixture_root=synthetic_official_set.root)
    assert attestation.binding_reason == ""
    assert is_promotable_source(attestation=attestation, **_inputs(synthetic_official_set.root)) is True


# ---- 用例 2–5：非 attestation 侧的条件 ----


def test_false_when_no_provenance(synthetic_official_set: Any, attestation_generator: Any) -> None:
    attestation = load_attestation(
        attestation_generator(), fixture_root=synthetic_official_set.root
    )
    inputs = _inputs(synthetic_official_set.root)
    inputs["has_provenance"] = False
    assert is_promotable_source(attestation=attestation, **inputs) is False


@pytest.mark.parametrize("field", ONE_FALSE_AT_A_TIME)
def test_false_when_any_single_check_fails(
    field: str, synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """③C1–C5/C7/C8 中任一项为假 → False（逐项参数化，7 个反例）。"""
    attestation = load_attestation(
        attestation_generator(), fixture_root=synthetic_official_set.root
    )
    inputs = _inputs(synthetic_official_set.root)
    inputs["checks"] = replace(ALL_TRUE, **{field: False})
    assert inputs["checks"].failed() == (field,)
    with pytest.raises(PromotionError, match=field):
        promote_excerpt(
            load_official_sources(synthetic_official_set.root).snapshot("pgvector"),
            section_id="s_pg_extension_enable",
            checks=inputs["checks"],
        )
    assert is_promotable_source(attestation=attestation, **inputs) is False


def test_false_for_authored_summary_even_when_everything_else_true(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """④人工摘要（即便闸门全真、证明有效）→ False。

    刻意把**其余条件全部设成真**：这样"False 只能由 source_kind 导致"，
    而不是靠别的条件碰巧也是假。
    """
    attestation = load_attestation(
        attestation_generator(), fixture_root=synthetic_official_set.root
    )
    inputs = _inputs(synthetic_official_set.root)
    inputs["source_kind"] = SOURCE_KIND_AUTHORED_SUMMARY
    inputs["checks"] = ALL_TRUE
    assert is_promotable_source(attestation=attestation, **inputs) is False


def test_unknown_source_kind_raises_not_false(synthetic_official_set: Any) -> None:
    """⑤未知 source_kind → **抛错**，不是返回 False。

    "类型写错了"是配置错误，"资料不够格"是业务结论 —— 静默返回 False 会让前者
    伪装成后者，两者需要的处置完全不同。
    """
    inputs = _inputs(synthetic_official_set.root)
    inputs["source_kind"] = "handwritten_notes"
    guard = pytest.raises(UnknownSourceKindError, match="配置错误")
    with guard:
        is_promotable_source(attestation=None, **inputs)


def test_checks_type_is_validated(synthetic_official_set: Any) -> None:
    inputs = _inputs(synthetic_official_set.root)
    inputs["checks"] = object()
    with pytest.raises(TypeError, match="PromotionChecks"):
        is_promotable_source(attestation=None, **inputs)


# ---- 用例 6–10：attestation 的有无与声明级对齐 ----


def test_false_without_attestation_but_load_still_works(
    synthetic_official_set: Any,
) -> None:
    """⑥**没有原件 / 没有证明 → False，但加载照常**（离线回放语义）。"""
    inputs = _inputs(synthetic_official_set.root)
    assert is_promotable_source(attestation=None, **inputs) is False
    assert (
        promotion_blocking_reason(
            None,
            expected_snapshot_set_id=inputs["expected_snapshot_set_id"],
            declared_documents=inputs["declared_documents"],
        )
        == REASON_ABSENT
    )
    # 加载期确实没有被"缺证明"影响
    assert load_official_sources(synthetic_official_set.root).candidate_ids()


def test_false_and_reason_incomplete(synthetic_official_set: Any, attestation_generator: Any) -> None:
    """⑦`overall_result="incomplete"` → False，且原因与 `absent` **可区分**。"""
    path = attestation_generator()
    _edit_json(path, lambda payload: payload.update({"overall_result": RESULT_INCOMPLETE}))
    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    inputs = _inputs(synthetic_official_set.root)
    assert is_promotable_source(attestation=attestation, **inputs) is False
    reason = promotion_blocking_reason(
        attestation,
        expected_snapshot_set_id=inputs["expected_snapshot_set_id"],
        declared_documents=inputs["declared_documents"],
    )
    assert reason == REASON_INCOMPLETE
    assert reason != REASON_ABSENT


def test_false_and_reason_version_expired(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """⑧核对规则版本过期 → False（旧的证明显式失效，而不是继续被采信）。"""
    path = attestation_generator()
    _edit_json(path, lambda payload: payload.update({"verifier_version": EXPECTED_VERIFIER_VERSION + 1}))
    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    inputs = _inputs(synthetic_official_set.root)
    assert is_promotable_source(attestation=attestation, **inputs) is False
    assert (
        promotion_blocking_reason(
            attestation,
            expected_snapshot_set_id=inputs["expected_snapshot_set_id"],
            declared_documents=inputs["declared_documents"],
        )
        == REASON_VERSION_EXPIRED
    )


def test_false_when_attestation_digest_conflicts_with_provenance(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """⑨证明里的 `raw_sha256` 与 provenance 声明不符 → False。"""
    path = attestation_generator()
    _edit_json(path, lambda payload: payload["documents"][0].update({"raw_sha256": "0" * 64}))
    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    inputs = _inputs(synthetic_official_set.root)
    assert is_promotable_source(attestation=attestation, **inputs) is False


def test_false_when_attestation_omits_a_document(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """⑩证明漏了某个 `source_id` → False（集合必须相等，不多不少）。

    `documents` 整体清空是**解析层**的 schema 错误（attempt 3 加固，
    见 `test_attestation_binding.py::test_a4a_empty_documents_rejected_at_parse`）；
    本用例因此漏掉两条文档中的一条 —— 声明层等集核对（H3b）判 False。
    """
    path = attestation_generator()
    _edit_json(path, lambda payload: payload["documents"].pop())
    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    inputs = _inputs(synthetic_official_set.root)
    assert is_promotable_source(attestation=attestation, **inputs) is False


def test_attestation_from_dict_alone_is_never_promotable(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """**手拼的 payload 永远不可升格**（fail-closed）。

    这是自绑定设计的核心性质：`from_dict` 单独构出的对象没有 `binding_reason`
    （从未与磁盘字节绑定），因此即使每个字段都"看起来对"，也只能得到 False。
    没有这条，文件级锚点就等于可以被绕过 —— 复制一份合法 JSON 就够了。
    """
    from agentboard.research.attestation import SourceVerificationAttestation

    payload = json.loads(attestation_generator().read_text(encoding="utf-8"))
    hand_made = SourceVerificationAttestation.from_dict(payload)
    assert hand_made.binding_reason is None
    inputs = _inputs(synthetic_official_set.root)
    assert is_promotable_source(attestation=hand_made, **inputs) is False
    assert (
        promotion_blocking_reason(
            hand_made,
            expected_snapshot_set_id=inputs["expected_snapshot_set_id"],
            declared_documents=inputs["declared_documents"],
        )
        == REASON_UNBOUND
    )


# ---- 用例 11–14：文件级自绑定（L-a / L-b） ----


def test_reason_mismatch_when_index_resigned_without_refreshing_attestation(
    synthetic_official_set: Any, attestation_generator: Any, resign_fixture: Any
) -> None:
    """⑪改了资料并**重签** `INDEX.json`，但不更新证明 → False，原因 `mismatch`（L-a 生效）。

    这条刻意走"改文件 + 重签"这一**真实工作流**（`sign_fixtures.py --write`）：
    只重签而不改内容的场景下 `INDEX.json` 字节不变，那样测不出 L-a。

    本用例同时证明了一件事：**加载期 C7 看不见这个问题**（重签之后签名自洽，
    `load_official_sources` 会成功），只有证明里的 `index_sha256` 锚点能发现它 ——
    这就是"自绑定不能只靠 `raw_sha256`/`blob_sha`"的可执行证据。
    """
    inputs = _inputs(synthetic_official_set.root)
    path = attestation_generator()

    synthetic_official_set.rewrite_snapshot(
        lambda payload: payload.update({"source_note": payload["source_note"] + " 改动"})
    )
    resign_fixture(synthetic_official_set.root)  # 重签 ⇒ INDEX.json 字节变化

    # 加载期照常成功：C7 只看"文件与 INDEX 是否一致"，重签让它自洽了
    assert load_official_sources(synthetic_official_set.root).candidate_ids()

    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    assert attestation.binding_reason == REASON_MISMATCH
    assert is_promotable_source(attestation=attestation, **inputs) is False
    assert (
        promotion_blocking_reason(
            attestation,
            expected_snapshot_set_id=inputs["expected_snapshot_set_id"],
            declared_documents=inputs["declared_documents"],
        )
        == REASON_MISMATCH
    )


def test_reason_mismatch_when_one_byte_of_fixture_changes(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """⑫改 1 个字节的快照文件、**不重签** → False（L-b 生效）。

    这一步**专门验证自绑定**：如果实现漏了 `snapshot_files[].sha256`，
    此时仍会返回 True。同时断言加载期 C7 也会独立报错 ——
    两道守卫各自成立、互不替代。
    """
    inputs = _inputs(synthetic_official_set.root)
    path = attestation_generator()

    payload = json.loads(synthetic_official_set.fixture_path.read_text(encoding="utf-8"))
    payload["source_note"] = payload["source_note"] + " "  # 一个字符，不重签
    synthetic_official_set.fixture_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    assert attestation.binding_reason == REASON_MISMATCH
    assert is_promotable_source(attestation=attestation, **inputs) is False

    # C7（加载期，信任 INDEX.json）也会独立发现：两条守卫都成立
    from agentboard.research.errors import SnapshotDigestError

    with pytest.raises(SnapshotDigestError):
        load_official_sources(synthetic_official_set.root)


@pytest.mark.parametrize("dropped", [True, False])
def test_reason_mismatch_when_snapshot_file_set_differs(
    dropped: bool, synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """⑬⑭证明的 `snapshot_files[]` 与实际文件集合不一致（少一条/多一条）→ False。

    "少" = 新增了候选却没重新核对；"多" = 删了候选却没重新核对。
    集合必须**完全一致**，因为自绑定的语义是"我这次要升格的，正是当初核对过的那一批"。

    只少一条而不是清空列表：清空会先撞上 `snapshot_files 必须是非空列表` 的结构校验，
    那样测的是 schema 而不是集合一致性（合成集因此刻意造两个候选）。
    """
    path = attestation_generator()

    def mutate(payload: dict[str, Any]) -> None:
        if dropped:
            payload["snapshot_files"] = payload["snapshot_files"][:-1]
        else:
            payload["snapshot_files"].append(
                {
                    "path": "sources/ghost.json",
                    "sha256": "0" * 64,
                    "candidate_id": "ghost",
                }
            )

    _edit_json(path, mutate)
    inputs = _inputs(synthetic_official_set.root)
    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    assert attestation.binding_reason == REASON_MISMATCH
    assert is_promotable_source(attestation=attestation, **inputs) is False


# ---- 用例 15 / 17：真实数据 ----


def test_real_research_candidates_are_never_promotable(
    fixture_root: Path, catalog: Any, snapshots: Any, synthetic_official_set: Any,
    attestation_generator: Any,
) -> None:
    """⑮**真实数据**：`fixtures/research` 三个候选全部不可升格。

    输入刻意设成"除了 `source_kind` 之外**全部为真**"：
    `has_provenance=True`、`checks=ALL_TRUE`、并且传入一份**有效**的证明。
    这样 False 只能来自 `source_kind=authored_summary` —— 否则测试可能只是
    因为"恰好没证明"或"恰好 checks 有假"而通过，等于没测到 source_kind 这条。
    """
    attestation = load_attestation(
        attestation_generator(), fixture_root=synthetic_official_set.root
    )
    sources = load_official_sources(synthetic_official_set.root)
    declared = snapshot_set_document_digests(sources)
    assert snapshots, "fixtures/research 不应为空"
    for candidate_id, snapshot in snapshots.items():
        assert snapshot.source_kind == SOURCE_KIND_AUTHORED_SUMMARY, candidate_id
        assert (
            is_promotable_source(
                source_kind=snapshot.source_kind,
                has_provenance=True,  # 假设它也补上了 provenance —— 依然不可升格
                checks=ALL_TRUE,
                attestation=attestation,
                expected_snapshot_set_id="official.2026-09-23",
                declared_documents=declared,
            )
            is False
        ), candidate_id


def test_real_official_candidates_are_not_promotable_without_artifact(
    official_root: Path,
) -> None:
    """⑰当前仓库**没有**正式证明工件 ⇒ 官方候选判为不可升格（`absent`）。

    P2.2-1 明确不创建 `attestations/official.sources.v1.json`（那是 P2.2-4 的事），
    因此这里期望 `False + absent`；有证明时的 `True` 由合成集用例 ① 覆盖。
    """
    sources = load_official_sources(official_root)
    declared = snapshot_set_document_digests(sources)
    for candidate_id in sources.candidate_ids():
        snapshot = sources.snapshot(candidate_id)
        checks = promotion_checks_for(sources, candidate_id, fixture_root=official_root)
        assert checks.all_passed(), candidate_id
        assert (
            is_promotable_source(
                source_kind=snapshot.source_kind,
                has_provenance=True,
                checks=checks,
                attestation=None,
                expected_snapshot_set_id=sources.catalog.snapshot_set,
                declared_documents=declared,
            )
            is False
        ), candidate_id


def test_snapshot_set_mismatch_is_rejected(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """证明的 `snapshot_set_id` 必须与当前快照集一致（防止跨集合复用证明）。"""
    path = attestation_generator()
    _edit_json(path, lambda payload: payload.update({"snapshot_set_id": "official.1999-01-01"}))
    inputs = _inputs(synthetic_official_set.root)
    attestation = load_attestation(path, fixture_root=synthetic_official_set.root)
    assert is_promotable_source(attestation=attestation, **inputs) is False


def test_reason_not_applicable_constant_is_defined_for_audit() -> None:
    """审计用的原因词表必须齐备（`authored_summary` 用 `not_applicable` 表述）。"""
    assert REASON_NOT_APPLICABLE == "not_applicable"
    assert len({REASON_ABSENT, REASON_INCOMPLETE, REASON_MISMATCH, REASON_VERSION_EXPIRED}) == 4


# ---- 用例 16：确定性与受控工厂 ----


def test_is_promotable_source_is_deterministic(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """⑯同输入两次调用结果相同（且实现里不读时钟 —— 由静态守卫覆盖）。"""
    attestation = load_attestation(
        attestation_generator(), fixture_root=synthetic_official_set.root
    )
    inputs = _inputs(synthetic_official_set.root)
    first = is_promotable_source(attestation=attestation, **inputs)
    second = is_promotable_source(attestation=attestation, **inputs)
    assert first == second is True


def test_promote_excerpt_requires_fixture_snapshot_not_mapping(
    synthetic_official_set: Any,
) -> None:
    """构造不可达：入参必须是 `FixtureSnapshot`，任意 dict / 采集记录都被拒。"""
    with pytest.raises(PromotionError, match="FixtureSnapshot"):
        promote_excerpt(
            {"candidate_id": "pgvector", "sections": []},  # type: ignore[arg-type]
            section_id="s_pg_extension_enable",
            checks=ALL_TRUE,
        )


def test_evidence_from_excerpt_rejects_plain_mapping(synthetic_official_set: Any) -> None:
    """伪造必失败：字段齐全的 provider 风格 payload **仍然**变不成 `EvidenceRef`。

    这是"受控公开 API"与"字段检查"的分界：字段检查可以靠补字段绕过，
    而类型不对在合法路径上根本没有入口。
    """
    forged = {
        "candidate_id": "pgvector",
        "snapshot_version": "pgvector.official.2026-09-23",
        "captured_at": "2026-09-23",
        "locator": "/sections/0/text",
        "excerpt": "Enable the extension (do this once in each database where you want to use it)",
        "source_kind": SOURCE_KIND_OFFICIAL_EXCERPT,
    }
    assert set(forged) == {
        field.name for field in dataclasses.fields(PromotableExcerpt)
    }
    with pytest.raises(PromotionError, match="PromotableExcerpt"):
        evidence_from_excerpt(forged)  # type: ignore[arg-type]


def test_promote_excerpt_rejects_authored_summary_snapshot(
    fixture_root: Path, catalog: Any
) -> None:
    """人工摘要快照即便闸门全真也**不能**产出 `PromotableExcerpt`。"""
    from agentboard.research.snapshot import load_snapshot

    snapshot = load_snapshot(
        fixture_root / "options" / "pgvector.json", entry=catalog.get("pgvector")
    )
    with pytest.raises(PromotionError, match="不可升格"):
        promote_excerpt(snapshot, section_id=snapshot.sections[0].section_id, checks=ALL_TRUE)


def test_promoted_excerpt_round_trips_through_resolve_evidence(
    synthetic_official_set: Any,
) -> None:
    """`promote_excerpt → evidence_from_excerpt → resolve_evidence` 必须闭合。"""
    sources = load_official_sources(synthetic_official_set.root)
    snapshot = sources.snapshot("pgvector")
    checks = promotion_checks_for(sources, "pgvector", fixture_root=synthetic_official_set.root)
    excerpt = promote_excerpt(
        snapshot, section_id="s_pg_extension_enable", checks=checks
    )
    ref = evidence_from_excerpt(excerpt)
    document = sources.resolve_evidence(ref)
    assert document.source_id == "doc_pgvector_readme"
    assert ref.source_type == "fixture_snapshot"
    assert ref.source_id == "fixture:pgvector"
    assert ref.locator == "/sections/0/text"


def test_excerpt_truncation_rule_matches_fixture_worker(official_sources: Any) -> None:
    """截断规则等价性（P2.2-1 不收敛它，但必须证明两条路径**规则相同**）。

    收敛成单一实现是 P2.2-2（D2）的范围；在此之前用等价性用例钉住，
    避免"升格路径与 worker 路径对同一条摘录给出不同结论"。
    """
    snapshot = official_sources.snapshot("pgvector")
    section_id = snapshot.sections[0].section_id
    locator, excerpt = snapshot_excerpt(snapshot, section_id)
    assert locator == section_locator(0)
    assert excerpt == snapshot.sections[0].text  # 短摘录不截断

    long_text = "x" * (MAX_EXCERPT_CHARS + 17)
    patched = replace(snapshot, sections=(replace(snapshot.sections[0], text=long_text),))
    _, truncated = snapshot_excerpt(patched, section_id)
    assert len(truncated) == MAX_EXCERPT_CHARS
    assert truncated == long_text[:MAX_EXCERPT_CHARS]


def test_declared_document_digests_cover_single_candidate(official_sources: Any) -> None:
    """单候选视图与集合视图的关系：集合视图是各候选的并集（含全部 4 份文档）。"""
    single = declared_document_digests(official_sources.provenance_for("pgvector"))
    union = snapshot_set_document_digests(official_sources)
    assert set(single) == {"doc_pgvector_readme"}
    assert set(single) <= set(union)
    assert len(union) == 4


def test_checks_failed_reports_every_failing_item() -> None:
    """`failed()` 必须列全，而不是只报第一个 —— 审计需要知道**全部**缺口。"""
    broken = replace(ALL_TRUE, c2=False, c8=False)
    assert broken.failed() == ("c2", "c8")
    assert broken.all_passed() is False
    assert ALL_TRUE.failed() == ()

"""P2.2-3：`research_grade` 强制点测试（I10_NON_PROMOTABLE_SUPPORT）。

核心不变量（方案 §P2.2-3「三条不可动摇的性质」 + P2.2-3 复核修正）：

1. **`research_grade` 是 opt-in** —— 不传 `snapshot_set` 的默认组装逐字节不变；
2. **人工摘要不可能支撑研究级推荐** —— 真实 `fixtures/research` 的快照
   `source_kind=authored_summary`，I10 按实际快照重算，必然失败（真实数据钉死）；
3. **升格权威不可伪造（P1 复核修正）** —— `SnapshotSet` **不携带** `promotable`
   映射（旧版手填 `True` 即可让无证明的官方集通过，已实测复现并修复）；
   升格判定由 I10 按**实际快照的 `source_kind`** + **loader-only 绑定的
   attestation** 重算：只有 `load_attestation()` 产生的证明才有效，
   手拼或"仅解析未绑定"的证明一律 fail-closed。

I10 只影响完整性判定、不改报告内容：`strict=True` 抛既有 `ReportIntegrityError`，
`strict=False` 返回 `integrity.passed=False` 的报告，两种模式 `report_id` 相同。
`attestations/` 正式工件留给 P2.2-4 —— 本文件只用 tmp 临时证明。
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agentboard.contracts import ProjectBrief
from agentboard.research.assemble import assemble_report
from agentboard.research.attestation import SourceVerificationAttestation, load_attestation
from agentboard.research.config import (
    POLICY_REGRESSION_REPLAY,
    POLICY_RESEARCH_GRADE,
    REASON_ABSENT,
    REASON_UNBOUND,
)
from agentboard.research.errors import ReportIntegrityError
from agentboard.research.snapshot import (
    SnapshotSet,
    load_catalog,
    load_snapshot_set,
    load_snapshots,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = PROJECT_ROOT / "fixtures" / "research"
OFFICIAL_ROOT = PROJECT_ROOT / "fixtures" / "official"
AUDIT_RAW_DIR = PROJECT_ROOT / ".workbuddy" / "tmp" / "audit"

I10 = "I10_NON_PROMOTABLE_SUPPORT"

#: 研究级报告的 brief（内容参与 report_id，但本文件的断言都是"同内容对比"，不受取值影响）
BRIEF = ProjectBrief(
    text="为检索服务做技术选型（研究级）", source="docs/research/selection_goal.md"
)

CLONE_SET_ID = "research.clone.2026-09-25"


def _load_registry(root: Path) -> Any:
    from agentboard.research.dimensions import load_dimension_registry

    return load_dimension_registry(root / load_catalog(root).dimension_registry_path)


def _official_brief() -> ProjectBrief:
    return ProjectBrief(
        text="为检索服务做技术选型（依据官方文档快照）",
        source="docs/research/selection_goal.md",
    )


def _official_snapshots() -> dict[str, Any]:
    return dict(load_snapshots(OFFICIAL_ROOT, load_catalog(OFFICIAL_ROOT)))


def _official_registry() -> Any:
    return _load_registry(OFFICIAL_ROOT)


def _official_cards() -> list[Any]:
    import asyncio

    from agentboard.contracts import TaskCard
    from agentboard.research.sources import load_official_sources, make_task_input

    src = load_official_sources(OFFICIAL_ROOT)
    worker = src.worker()
    cards: list[Any] = []
    for candidate_id in src.candidate_ids():
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


def _cards_from_sources(sources: Any) -> list[Any]:
    """用给定 OfficialSources 的 worker 产出已完成 research 卡（与真实管线一致）。"""
    import asyncio

    from agentboard.contracts import TaskCard
    from agentboard.research.sources import make_task_input

    worker = sources.worker()
    cards: list[Any] = []
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


@pytest.fixture
def research_materials(
    tmp_path: Path, make_cards: Any
) -> tuple[dict[str, Any], list[Any], Any]:
    """（声明了身份的真实 research 快照, 真实 research 卡, 注册表）。

    `fixtures/research/INDEX.json` 没有 `snapshot_set` 键 ⇒ 戳印为 `""`、归属不可证明。
    在 tmp 克隆上给 INDEX 补上声明（快照文件字节不变 ⇒ fixture_sha256 仍自洽），
    得到"归属可证明"的真实数据 —— 这模拟的是"若目录声明了自己的快照集"。
    卡片用 conftest 的 `make_cards`（对真实目录跑 worker），内容与克隆逐字节一致。
    """
    stamped_root = tmp_path / "research-declared"
    shutil.copytree(RESEARCH_ROOT, stamped_root)
    index = json.loads((stamped_root / "INDEX.json").read_text(encoding="utf-8"))
    index["snapshot_set"] = CLONE_SET_ID
    (stamped_root / "INDEX.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    snapshots = dict(load_snapshots(stamped_root, load_catalog(stamped_root)))
    assert snapshots and all(
        snapshot.snapshot_set == CLONE_SET_ID for snapshot in snapshots.values()
    )
    assert all(
        snapshot.source_kind == "authored_summary" for snapshot in snapshots.values()
    ), "测试前提：research 目录是人工摘要"
    return snapshots, make_cards(), _load_registry(stamped_root)


def _research_grade_declared() -> SnapshotSet:
    """research_grade 声明（**无证明**）—— 升格与否由 I10 按实际数据重算。"""
    return SnapshotSet(snapshot_set_id=CLONE_SET_ID, policy=POLICY_RESEARCH_GRADE)


# ---- ④：真实人工摘要 + research_grade + strict=True ----


def test_authored_summaries_fail_research_grade_strict(research_materials: Any) -> None:
    """真实人工摘要不可能支撑研究级推荐：strict=True ⇒ `ReportIntegrityError`，含 I10。"""
    snapshots, cards, registry = research_materials
    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(
            BRIEF, cards, registry, snapshots=snapshots, snapshot_set=_research_grade_declared()
        )
    assert I10 in excinfo.value.failed_codes


# ---- ⑤：同场景 strict=False：不抛，返回带失败码与明细的报告 ----


def test_authored_summaries_fail_research_grade_non_strict(
    research_materials: Any,
) -> None:
    """strict=False ⇒ 不抛；`integrity.passed=False`、failed_codes 含 I10、明细可定位。

    明细必须含 candidate / dimension / finding 三段定位与**从实际快照重算出的原因**
    （人工摘要 ⇒ `source_kind=authored_summary`），这是它比一条异常消息更有用的地方。
    """
    snapshots, cards, registry = research_materials
    report = assemble_report(
        BRIEF,
        cards,
        registry,
        snapshots=snapshots,
        snapshot_set=_research_grade_declared(),
        strict=False,
    )
    assert report.integrity.passed is False
    assert I10 in report.integrity.failed_codes
    detail = report.integrity.detail_of(I10)
    assert "pgvector" in detail, "违规明细必须能定位到候选"
    assert "authored_summary" in detail, "原因必须来自实际快照的 source_kind"
    supported_pgvector = [
        finding
        for finding in report.findings
        if finding.is_supported and finding.candidate_id == "pgvector"
    ]
    assert supported_pgvector, "测试前提：pgvector 存在 supported finding"
    for finding in supported_pgvector:
        assert finding.finding_id in detail, "每条 supported finding 都必须出现在违规明细里"
        assert finding.dimension_id in detail


# ---- ⑥：两种 strict 模式 report_id 相同（I10 不进 content_projection） ----


def test_strict_and_non_strict_share_the_same_report_id(research_materials: Any) -> None:
    """同内容：默认组装（passed）与 research_grade 失败报告（strict=False）`report_id` 相同。

    strict=True 抛错拿不到报告，因此与"同内容的默认组装"比对 ——
    这同时验证了 P1A 基线内容在默认策略下 integrity 仍然全绿。
    """
    snapshots, cards, registry = research_materials
    default = assemble_report(BRIEF, cards, registry, snapshots=snapshots)
    assert default.integrity.passed is True, "P1A 基线内容在默认策略下必须完整通过"

    failed = assemble_report(
        BRIEF,
        cards,
        registry,
        snapshots=snapshots,
        snapshot_set=_research_grade_declared(),
        strict=False,
    )
    assert failed.integrity.passed is False
    assert failed.report_id == default.report_id


# ---- ⑨（复核修正后）：证明缺失 / 未绑定 ⇒ fail-closed ----


def test_missing_attestation_is_fail_closed(research_materials: Any) -> None:
    """声明了 research_grade 但没有任何证明 ⇒ 失败，绝不当作"无约束放行"。"""
    snapshots, cards, registry = research_materials
    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(
            BRIEF, cards, registry, snapshots=snapshots, snapshot_set=_research_grade_declared()
        )
    assert I10 in excinfo.value.failed_codes

    report = assemble_report(
        BRIEF, cards, registry, snapshots=snapshots, snapshot_set=_research_grade_declared(),
        strict=False,
    )
    assert I10 in report.integrity.failed_codes
    detail = report.integrity.detail_of(I10)
    assert REASON_ABSENT in detail


def test_parsed_but_unbound_attestation_is_fail_closed(
    tmp_path: Path, verify_tool: Any
) -> None:
    """复核要求的核心负控：**仅解析、未绑定**的证明 ⇒ fail-closed（原因 `unbound`）。

    构造方式：用真工具对真实原件生成一份**有效**证明，再用 `from_dict` 解析它 ——
    解析产物内容与有效证明完全相同，但从未经过 `load_attestation()` 的文件级绑定。
    若它被 I10 采信，"loader-only 绑定"就是纸面承诺。

    同时证明"构造后不能翻转"：attestation 是冻结对象、binding 由 loader 独占
    （L5 静态守卫钉住 `AttestationBinding` 的构造点），没有任何公开路径能把
    未绑定变成已绑定。
    """
    if not AUDIT_RAW_DIR.is_dir():
        pytest.skip(
            f"真实原件目录不存在（{AUDIT_RAW_DIR}）——本用例需要先对真实原件生成证明；"
            "unbound 的构造期拦截由 SnapshotSet 的类型校验部分覆盖"
        )
    result = verify_tool.verify_sources(OFFICIAL_ROOT, AUDIT_RAW_DIR)
    assert result.passed()
    payload = verify_tool.build_attestation_payload(OFFICIAL_ROOT, result)
    target = tmp_path / "official.sources.v1.json"
    ok, message = verify_tool.write_attestation(target, verify_tool.attestation_bytes(payload))
    assert ok, message

    parsed_only = SourceVerificationAttestation.from_dict(
        json.loads(target.read_text(encoding="utf-8"))
    )
    # 对照：同一份文件经 load_attestation 绑定后是有效的 —— 证明"无效"只来自"未绑定"
    bound = load_attestation(target, fixture_root=OFFICIAL_ROOT)
    assert bound.binding_reason == "" and bound.overall_result == "passed"
    assert parsed_only.binding_reason is None, "from_dict 产物必须保持未绑定状态"

    unbound_set = SnapshotSet(
        snapshot_set_id=load_catalog(OFFICIAL_ROOT).snapshot_set,
        policy=POLICY_RESEARCH_GRADE,
        attestation=parsed_only,
    )
    cards, snapshots, registry = _official_cards(), _official_snapshots(), _official_registry()

    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(
            _official_brief(), cards, registry, snapshots=snapshots, snapshot_set=unbound_set
        )
    assert I10 in excinfo.value.failed_codes

    report = assemble_report(
        _official_brief(), cards, registry, snapshots=snapshots, snapshot_set=unbound_set,
        strict=False,
    )
    assert I10 in report.integrity.failed_codes
    assert REASON_UNBOUND in report.integrity.detail_of(I10)

    # 构造后不可翻转：replace 无法触碰 init=False 的绑定字段（P1-1 修复的延伸）
    with pytest.raises(TypeError):
        dataclasses.replace(unbound_set.attestation, binding_reason="")  # type: ignore[arg-type]


# ---- ⑦ + P1 回归：真实官方集的伪造升格必须失败 ----


def test_forged_promotable_true_cannot_pass_on_real_official_set(
    official_sources: Any, official_cards: Any, official_brief: ProjectBrief
) -> None:
    """P1 回归测试（复核复现路径）：真实官方集 + 伪造升格声明 ⇒ 必须失败。

    复核实测的攻击：手填 `promotable={cid: True}` 让无证明的官方集通过
    `research_grade`（`integrity.passed=True`、`report_id=rp_9e0057b9f989020f`）。
    修复后：
    1. `SnapshotSet` 构造器**没有** `promotable` 参数 —— 伪造入口不存在；
    2. 无证明的声明 ⇒ I10 按 `absent` 失败；
    3. `strict=True` 抛 `ReportIntegrityError`，绝不返回 `passed=True` 的报告。
    """
    field_names = {field.name for field in dataclasses.fields(SnapshotSet)}
    assert "promotable" not in field_names and "promotable_reasons" not in field_names, (
        "升格权威不得回到可构造字段 —— 这是 P1 修复的根本"
    )

    declared = official_sources.snapshot_set(policy=POLICY_RESEARCH_GRADE)  # 无证明
    with pytest.raises(ReportIntegrityError) as excinfo:
        assemble_report(
            official_brief,
            official_cards(),
            official_sources.registry,
            snapshots=official_sources.snapshots,
            snapshot_set=declared,
        )
    assert I10 in excinfo.value.failed_codes

    report = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
        snapshot_set=declared,
        strict=False,
    )
    assert report.integrity.passed is False
    assert I10 in report.integrity.failed_codes
    assert REASON_ABSENT in report.integrity.detail_of(I10)


# ---- ⑧：有效证明 ⇒ 通过，report_id 不变 ----


def test_official_set_with_passed_attestation_passes_and_keeps_report_id(
    official_sources: Any,
    official_cards: Any,
    official_brief: ProjectBrief,
    verify_tool: Any,
    tmp_path: Path,
) -> None:
    """官方集 + `passed` attestation（tmp 临时工件）⇒ 通过，`report_id` 与默认模式相同。

    证明用真工具对**真实原件**生成，落到 `tmp_path` —— 不创建正式 `attestations/`（P2.2-4）。
    原件目录缺失时跳过并明确记录原因；确定性等价路径（合成官方集）见下一个用例，
    无条件运行，不依赖本机原件。
    """
    if not AUDIT_RAW_DIR.is_dir():
        pytest.skip(
            f"真实原件目录不存在（{AUDIT_RAW_DIR}）——来源核对无法在本机完成；"
            "本用例的主路径由 "
            "test_synthetic_official_set_with_passed_attestation 无条件覆盖"
        )

    result = verify_tool.verify_sources(OFFICIAL_ROOT, AUDIT_RAW_DIR)
    assert result.passed(), (
        "测试前提：真实原件核对必须全部通过，否则 attestation 不会是 passed"
    )
    payload = verify_tool.build_attestation_payload(OFFICIAL_ROOT, result)
    target = tmp_path / "official.sources.v1.json"
    ok, message = verify_tool.write_attestation(target, verify_tool.attestation_bytes(payload))
    assert ok, message

    attestation = load_attestation(target, fixture_root=OFFICIAL_ROOT)
    # attempt 3：research_grade 正控必须走 load_snapshot_set 工厂 ——
    # root ↔ attestation 同源绑定（H6）；binding_context 缺失 ⇒ unbound。
    declared = load_snapshot_set(
        fixture_root=OFFICIAL_ROOT, attestation_path=target, policy=POLICY_RESEARCH_GRADE
    )
    assert declared.binding_context is not None
    assert attestation.binding_reason == ""

    default = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
    )
    graded = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
        snapshot_set=declared,
    )
    assert graded.integrity.passed is True
    assert graded.report_id == default.report_id


def test_synthetic_official_set_with_passed_attestation(
    synthetic_official_set: Any, attestation_generator: Any, tmp_path: Path
) -> None:
    """⑧ 的确定性版本：合成官方集 + `passed` 证明 ⇒ 通过且 `report_id` 不变。

    合成集自带与 provenance 逐字节匹配的原件（tmp 内），可离线稳定复现
    "有有效证明 ⇒ 可升格 ⇒ I10 通过 ⇒ 报告成功" 这条主路径。
    """
    from agentboard.research.sources import load_official_sources

    attestation_path = attestation_generator(tmp_path / "synthetic-attestation.json")
    attestation = load_attestation(attestation_path, fixture_root=synthetic_official_set.root)

    src = load_official_sources(synthetic_official_set.root)
    # attempt 3：正控走 load_snapshot_set 工厂（root ↔ attestation 同源绑定，H6）
    declared = load_snapshot_set(
        fixture_root=synthetic_official_set.root,
        attestation_path=attestation_path,
        policy=POLICY_RESEARCH_GRADE,
    )
    assert declared.binding_context is not None
    assert attestation.binding_reason == ""

    brief = ProjectBrief(
        text="为检索服务做技术选型（合成集研究级）",
        source="docs/research/selection_goal.md",
    )
    cards = _cards_from_sources(src)
    default = assemble_report(brief, cards, src.registry, snapshots=src.snapshots)
    graded = assemble_report(
        brief, cards, src.registry, snapshots=src.snapshots, snapshot_set=declared
    )
    assert graded.integrity.passed is True
    assert graded.report_id == default.report_id


# ---- 附加：I10 只在 research_grade 下出现；判定确定性（构造后不可翻转） ----


def test_regression_replay_policy_never_runs_i10(
    official_sources: Any, official_cards: Any, official_brief: ProjectBrief
) -> None:
    """`regression_replay` 不追加条件检查码：检查序列与默认组装完全一致。"""
    declared = official_sources.snapshot_set(policy=POLICY_REGRESSION_REPLAY)
    report = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
        snapshot_set=declared,
    )
    assert I10 not in (check.code for check in report.integrity.checks)


def test_promotion_verdict_is_deterministic_after_construction(
    official_sources: Any, official_cards: Any, official_brief: ProjectBrief
) -> None:
    """复核要求："验证可变输入在构造后不能翻转结果"。

    I10 消费的唯一外部对象是 attestation —— 冻结数据类、binding 由 loader 独占
    （L5 静态守卫钉住 `AttestationBinding` 的构造点）；SnapshotSet 自身无任何
    可变映射。同内容两次组装必须得到相同的 I10 结论；且不存在公开 API 能在
    构造后改变判定输入。
    """
    declared = official_sources.snapshot_set(policy=POLICY_RESEARCH_GRADE)  # 无证明
    first = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
        snapshot_set=declared,
        strict=False,
    )
    second = assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
        snapshot_set=declared,
        strict=False,
    )
    assert first.integrity.detail_of(I10) == second.integrity.detail_of(I10)
    assert first.integrity.passed is second.integrity.passed is False

    # SnapshotSet 字段闭集（attempt 3）：binding_context 为新增声明字段；
    # 升格权威仍不可注入（promotable 已删除，binding_context 只能由工厂产出）
    assert {field.name for field in dataclasses.fields(SnapshotSet)} == {
        "snapshot_set_id",
        "policy",
        "attestation",
        "binding_context",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        declared.policy = POLICY_REGRESSION_REPLAY  # type: ignore[misc]

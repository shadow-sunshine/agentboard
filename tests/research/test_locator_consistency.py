"""P2.2-2：摘录截断与 locator 规则收敛的一致性测试（修 D2）。

被测不变量只有一条：**"证据可解析"是单一良定义属性** ——
`excerpt` 必须恰好等于 locator 指向原文经 `expected_excerpt_for` 的规范截断，
组装期（`assemble._verify_fixture_locators`）与来源层（`sources.resolve_evidence`）
必须对同一份证据给出**一致**的结论。

D2 的原始形态（实测复现过）：同一份"20 字符前缀摘录"证据，
组装期 `startswith` 放行、来源层精确比较拒绝 —— 两条路径结论相反。
收敛方式不是"把两边改成同一份内联逻辑"，而是把截断本身收敛成
`snapshot.expected_excerpt_for` 这**唯一实现点**（硬门禁见本文件最后一个测试），
两条校验路径都调它 —— 规则相等由结构保证，不再靠等价性测试维持。

旧实现是 `startswith`：**原文的任意前缀**都能通过组装期。收敛后，
"非规范前缀"（比如前 20 字符、前 399 字符）在**两条路径都被拒绝**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentboard.evidence import SOURCE_FIXTURE, EvidenceRef
from agentboard.research.assemble import _verify_fixture_locators, collect_findings
from agentboard.research.errors import DanglingLocatorError, SourceReferenceError
from agentboard.research.models import ResearchFinding
from agentboard.research.snapshot import (
    FixtureSection,
    FixtureSnapshot,
    expected_excerpt_for,
    resolve_locator,
    section_locator,
    snapshot_excerpt,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _ref(
    snapshot: FixtureSnapshot, index: int, excerpt: str
) -> EvidenceRef:
    """按给定摘录构造一条 fixture_snapshot 证据（字段与 worker 产出同构）。"""
    return EvidenceRef(
        source_type=SOURCE_FIXTURE,
        source_id=f"fixture:{snapshot.candidate_id}",
        snapshot_version=snapshot.snapshot_version,
        locator=section_locator(index),
        excerpt=excerpt,
        captured_at=snapshot.captured_at,
    )


def _finding(
    snapshot: FixtureSnapshot, refs: tuple[EvidenceRef, ...]
) -> ResearchFinding:
    """构造一条 supported finding，只带给定证据（`_verify_fixture_locators` 只读这两个字段）。"""
    return ResearchFinding(
        candidate_id=snapshot.candidate_id,
        task_id="t_locator",
        agent_name="research",
        dimension_id="deployment_complexity",
        claim="摘录一致性用例",
        value="low",
        status="supported",
        evidence_refs=refs,
    )


# ---- ① 前缀摘录：两条路径结论一致（都拒） ----


def test_prefix_excerpt_rejected_by_both_paths(official_sources: Any) -> None:
    """D2 的直接回归测试：真前缀但**非规范截断**的摘录，两条路径都必须拒绝。

    修前实测：同一条 20 字符前缀摘录，组装期 `startswith` **通过**、
    来源层精确比较**拒绝** —— 同一输入两个校验器给出相反答案。
    """
    src = official_sources
    snap = src.snapshot("pgvector")
    index = 0
    text = snap.sections[index].text
    assert len(text) > 20, "测试前提：所选 section 足够长，前 20 字符是真前缀"

    bad_ref = _ref(snap, index, text[:20])
    finding = _finding(snap, (bad_ref,))

    with pytest.raises(DanglingLocatorError, match="规范截断"):
        _verify_fixture_locators([finding], {"pgvector": snap})
    with pytest.raises(SourceReferenceError, match="不一致"):
        src.resolve_evidence(bad_ref)

    # 反向钉住：规范截断（ canonical ）在两条路径都通过 —— 保证拒绝的不是"前缀"本身
    canonical_ref = _ref(snap, index, expected_excerpt_for(text))
    _verify_fixture_locators([_finding(snap, (canonical_ref,))], {"pgvector": snap})
    src.resolve_evidence(canonical_ref)


# ---- ② 完整摘录：两条路径都收 ----


def test_full_excerpt_accepted_by_both_paths(official_sources: Any) -> None:
    """section 全文（≤400 时规范截断 == 全文）在两条路径都应通过。"""
    src = official_sources
    snap = src.snapshot("pgvector")
    index = 0
    text = snap.sections[index].text
    assert len(text) <= 400, "测试前提：所选 section 全文 ≤ 400，规范截断即全文"

    full_ref = _ref(snap, index, text)  # 摘录 = 完整原文
    _verify_fixture_locators([_finding(snap, (full_ref,))], {"pgvector": snap})
    document = src.resolve_evidence(full_ref)
    assert document.source_id == snap.sections[index].section_id or document.source_id


# ---- ③ 超 400 字符 section：截断规则在两条路径一致 ----


def _synthetic_snapshot(text: str) -> FixtureSnapshot:
    """内存合成的官方快照（不落盘、不触碰 fixtures/**）。"""
    return FixtureSnapshot(
        candidate_id="synthetic",
        display_name="合成超长 section 用例",
        snapshot_version="synthetic.official.2026-09-25",
        captured_at="2026-09-25",
        source_path="sources/synthetic.json",
        source_kind="official_documentation_excerpt",
        sections=(
            FixtureSection(section_id="s_long", heading="长文", text=text),
        ),
        dimension_values={},
    )


def test_over_400_section_truncation_consistent_on_assemble_path() -> None:
    """组装期：>400 字符 section 的规范截断（前 400）通过，非规范前缀（前 399）拒绝。"""
    text = "部署步骤说明。" * 60  # 420 字符，> 400
    snap = _synthetic_snapshot(text)
    assert len(text) > 400

    canonical = _ref(snap, 0, expected_excerpt_for(text))
    assert len(canonical.excerpt) == 400
    _verify_fixture_locators([_finding(snap, (canonical,))], {"synthetic": snap})

    with pytest.raises(DanglingLocatorError, match="规范截断"):
        _verify_fixture_locators(
            [_finding(snap, (_ref(snap, 0, text[:399]),))], {"synthetic": snap}
        )


def test_over_400_section_resolve_path_uses_same_rule(
    synthetic_official_set: Any, resign_fixture: Any
) -> None:
    """来源层：>400 字符 section 下，`resolve_evidence` 与组装期接受/拒绝**同一种**摘录。

    用合成官方快照集（tmp 落盘、重签 INDEX）把 section 文本改成 >400 字符；
    `resolve_evidence` 只比对 section 文本与摘录（C6 原件绑定是另一条独立路径），
    因此改文本 + 重签即可构造该场景，不触碰 `fixtures/**`。
    """
    long_text = "部署步骤说明。" * 60  # 420 字符
    synthetic_official_set.rewrite_snapshot(
        lambda payload: payload["sections"][0].__setitem__("text", long_text)
    )
    resign_fixture(synthetic_official_set.root)

    from agentboard.research.sources import load_official_sources

    src = load_official_sources(synthetic_official_set.root)
    candidate_id = synthetic_official_set.candidate_ids[0]
    snap = src.snapshot(candidate_id)
    assert len(snap.sections[0].text) == len(long_text)

    locator, canonical_excerpt = snapshot_excerpt(snap, snap.sections[0].section_id)
    assert canonical_excerpt == expected_excerpt_for(long_text)
    assert len(canonical_excerpt) == 400, "超长 section 的规范摘录必须被截到 400"

    canonical_ref = EvidenceRef(
        source_type=SOURCE_FIXTURE,
        source_id=f"fixture:{snap.candidate_id}",
        snapshot_version=snap.snapshot_version,
        locator=locator,
        excerpt=canonical_excerpt,
        captured_at=snap.captured_at,
    )
    document = src.resolve_evidence(canonical_ref)
    assert document.source_id

    # 不能用 dataclasses.replace 改 excerpt：EvidenceRef 会做 digest 自校验并拒绝，
    # 这本身就是一条"摘录被篡改会被发现"的旁证。这里重建一条非规范前缀的证据。
    bad_ref = _ref(snap, 0, long_text[:399])
    with pytest.raises(SourceReferenceError, match="不一致"):
        src.resolve_evidence(bad_ref)


# ---- ④ 现有官方候选：全部 finding 在两条路径仍通过 ----


def test_all_real_official_findings_pass_both_paths(
    official_sources: Any, official_cards: Any
) -> None:
    """收敛不得破坏真实数据：官方 3 个候选的全部 finding，组装期与来源层都通过。

    findings 用与报告组装**同一条**入口（`collect_findings(official_cards())`）构建，
    而不是手搓 —— 保证测的是真实管线形状。

    （改精确比较**之前**已用探针确认：两个 fixture 目录共 24 条 fixture 证据，
    摘录全部恰好等于规范截断 —— 因此本用例对既有数据是零风险的。）
    """
    src = official_sources
    findings = collect_findings(official_cards())
    assert findings, "官方目录应产出 finding"
    n_checked = 0
    for finding in findings:
        snap = src.snapshot(finding.candidate_id)
        _verify_fixture_locators([finding], {finding.candidate_id: snap})
        for ref in finding.evidence_refs:
            if ref.source_type == SOURCE_FIXTURE:
                src.resolve_evidence(ref)
                # 每条证据的摘录都必须等于规范截断（把"一致"落成显式断言）
                assert ref.excerpt == expected_excerpt_for(
                    resolve_locator(snap, ref.locator)
                )
                n_checked += 1
    assert n_checked >= 8, f"官方目录应有 ≥8 条 fixture 证据，实际 {n_checked}"


# ---- ⑤ 硬门禁：截断实现点唯一性（2026-09-25 复核裁决） ----


def test_excerpt_truncation_has_single_implementation_site() -> None:
    """`src/` 内对 `MAX_EXCERPT_CHARS` 的**前缀切片**必须恰好只有 1 处。

    P2.2-1 结束时该规则有 4 份实现（fixtures.py:255 / migrate.py:38 /
    snapshot.py:638 / sources.py:821 内联），靠等价性测试维持一致 ——
    那种"恰好相等"挡不住任何一处被单独改动。收敛后此断言把
    "唯一实现点"变成**可机械执行**的门禁：谁再复制一份切片，测试立刻变红。

    双口径：
    - **文本行扫描**：与方案 §P2.2-2 写死的复核命令完全同口径（含 docstring ——
      所以 `expected_excerpt_for` 的文档字符串刻意不写出那个切片字面量）；
    - **AST**：确认唯一命中点真的在 `expected_excerpt_for` 函数体内，
      且五个消费模块都改为引用该函数（防"函数存在但没人用"的假收敛）。
    """
    src_root = PROJECT_ROOT / "src"
    text_hits: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if "[:MAX_EXCERPT_CHARS]" in line:
                text_hits.append(f"{rel}:{line_number}")

    assert len(text_hits) == 1 and text_hits[0].startswith(
        "src/agentboard/research/snapshot.py:"
    ), f"截断切片必须只有 1 个实现点（expected_excerpt_for），实际：{text_hits}"

    # AST 口径：唯一实现点在 expected_excerpt_for 函数体内
    snapshot_py = (src_root / "agentboard" / "research" / "snapshot.py").read_text(
        encoding="utf-8"
    )
    import ast

    inside_expected: list[str] = []
    for node in ast.walk(ast.parse(snapshot_py)):
        if isinstance(node, ast.FunctionDef) and node.name == "expected_excerpt_for":
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Subscript):
                    continue
                upper = sub.slice.upper if isinstance(sub.slice, ast.Slice) else sub.slice
                if isinstance(upper, ast.Name) and upper.id == "MAX_EXCERPT_CHARS":
                    inside_expected.append(f"line {sub.lineno}")
    assert inside_expected, "expected_excerpt_for 内必须存在规范截断切片"

    # 五个消费模块都必须引用唯一实现（fixture worker / 迁移器 / 快照 / 来源层 / 组装期）
    consumers = [
        "src/agentboard/research/fixtures.py",
        "src/agentboard/research/migrate.py",
        "src/agentboard/research/snapshot.py",
        "src/agentboard/research/sources.py",
        "src/agentboard/research/assemble.py",
    ]
    for rel in consumers:
        body = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "expected_excerpt_for" in body, f"{rel} 应调用 expected_excerpt_for"

    # 组装期不得再出现 startswith 式的宽松比较（D2 的根源写法）。
    # AST 口径而非字符串匹配：模块 docstring 里**必须**能解释"为什么不用 startswith"
    # （那是 D2 最有价值的一段注释），把解释也算违规会逼人删掉它 —— 与
    # test_write_path_never_uses_replace 同一个设计判断。
    assemble_ast = ast.parse(
        (PROJECT_ROOT / "src/agentboard/research/assemble.py").read_text(encoding="utf-8")
    )
    startswith_calls = [
        f"line {node.lineno}"
        for node in ast.walk(assemble_ast)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
    ]
    assert not startswith_calls, (
        f"组装期不得用 startswith 接受任意前缀摘录（D2 根源），必须精确比较规范截断：{startswith_calls}"
    )

"""证据层的受控公开 API（L2）与 Provider 反向边界（L4）的动态负向测试（P2.2-1）。

对应 P2.2_PLAN §2.5 的四条负向要求。与 `tests/test_static_guards.py` 的分工：

| 文件 | 层次 |
|---|---|
| `tests/test_static_guards.py` | **静态**：守卫函数本身 + 正负控制 + 反空转对照 |
| 本文件 | **动态**：真的把"不该能进去的东西"递进去，看它是否被拒 |

能力边界的准确说法是 **"受控公开 API + 静态守卫下不可合法生成"**，
**不是**"类型上不可表达" —— Python 没有构造可及性控制。本文件测的是前者：
**没有合法的代码路径**，而不是"物理上做不到"。断言里刻意不出现"不可能"这类词。
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any

import pytest

from agentboard.evidence import EvidenceRef
from agentboard.providers.interface import RetrievedDocument
from agentboard.research.errors import PromotionError
from agentboard.research.snapshot import PromotableExcerpt, PromotionChecks, promote_excerpt
from agentboard.research.sources import evidence_from_excerpt, load_official_sources

ALL_TRUE = PromotionChecks.all_true()


def _retrieved_document() -> RetrievedDocument:
    """一份**完全合法**的 P2.1 采集记录（Provider 层的输出契约）。"""
    content = "# pgvector\n\nEnable the extension (do this once in each database).\n"
    return RetrievedDocument(
        source_id="gh:pgvector/pgvector:README.md",
        query_terms="pgvector enable extension",
        source_url="https://raw.githubusercontent.com/pgvector/pgvector/main/README.md",
        title="pgvector/README.md",
        captured_at="2026-09-23",
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


# ---- 要求 1：构造不可达 ----


def test_promote_excerpt_rejects_arbitrary_mapping() -> None:
    with pytest.raises(PromotionError, match="FixtureSnapshot"):
        promote_excerpt(
            {"candidate_id": "pgvector"},  # type: ignore[arg-type]
            section_id="s_pg_extension_enable",
            checks=ALL_TRUE,
        )


def test_promote_excerpt_rejects_provider_record() -> None:
    """**真实**的 Provider 采集记录也不能当入参 —— 不是"随便造个 dict"才被拒。

    这条是 P2.1 → P2.2 边界的关键断言：采集层与证据层之间刻意没有通路，
    而"没有通路"必须表现为"没有合法调用方式"，而不是"我们约定不要这么写"。
    """
    with pytest.raises(PromotionError, match="FixtureSnapshot"):
        promote_excerpt(
            _retrieved_document(),  # type: ignore[arg-type]
            section_id="s_anything",
            checks=ALL_TRUE,
        )


def test_promote_excerpt_rejects_bad_checks_type(official_sources: Any) -> None:
    snapshot = official_sources.snapshot("pgvector")
    with pytest.raises(PromotionError, match="PromotionChecks"):
        promote_excerpt(
            snapshot, section_id=snapshot.sections[0].section_id, checks=object()  # type: ignore[arg-type]
        )


# ---- 要求 2：伪造必失败 ----


def test_forged_provider_style_payload_cannot_become_evidence() -> None:
    """**字段齐全**的 provider 风格 payload 仍然变不成 `EvidenceRef`。

    这是"受控 API"与"字段检查"的分界点：如果边界是"断言某类记录不含 locator/excerpt"，
    那这里补上 `locator` / `excerpt` 就够了；而按受控 API，缺的是**类型**，补字段没有用。
    """
    forged = {
        "candidate_id": "pgvector",
        "snapshot_version": "pgvector.official.2026-09-23",
        "captured_at": "2026-09-23",
        "locator": "/sections/0/text",
        "excerpt": "Enable the extension (do this once in each database where you want to use it)",
        "source_kind": "official_documentation_excerpt",
    }
    # 字段名与 `PromotableExcerpt` 完全一致 —— 仍然进不去
    assert set(forged) == {field.name for field in dataclasses.fields(PromotableExcerpt)}
    with pytest.raises(PromotionError, match="PromotableExcerpt"):
        evidence_from_excerpt(forged)  # type: ignore[arg-type]


def test_forged_provider_record_cannot_become_evidence() -> None:
    with pytest.raises(PromotionError, match="PromotableExcerpt"):
        evidence_from_excerpt(_retrieved_document())  # type: ignore[arg-type]


def test_evidence_from_excerpt_rejects_none_and_string() -> None:
    for bad in (None, "fixture:pgvector", 42):
        with pytest.raises(PromotionError, match="PromotableExcerpt"):
            evidence_from_excerpt(bad)  # type: ignore[arg-type]


# ---- 要求 3 / 4：静态守卫可证伪（在 tests/test_static_guards.py）----
#
# 静态守卫的"变红"对照实验在那里完成（真实 provider 文件注入一行 import → 守卫变红；
# 真实 research 文件注入一次构造 → L3 变红）。这里补一条**跨层**的一致性断言：
# 领域层加载出来的东西与 Provider 层的类型**互不兼容**。


def test_domain_evidence_and_provider_record_are_unrelated_types(official_sources: Any) -> None:
    """`EvidenceRef` 与 `RetrievedDocument` 字段重叠但类型上毫无关系。

    两者都有 `source_id` / `captured_at` / 内容摘要 —— 正因如此，"看着像就能互相赋用"
    是最容易发生的错误。这条断言把"它们是两套独立契约"钉成事实。
    """
    evidence_fields = {field.name for field in dataclasses.fields(EvidenceRef)}
    record_fields = {field.name for field in dataclasses.fields(RetrievedDocument)}
    assert {"source_id", "captured_at"} <= (evidence_fields & record_fields)
    assert not isinstance(_retrieved_document(), EvidenceRef)
    # 领域层产出的证据也不该是 provider 记录
    snapshot = official_sources.snapshot("pgvector")
    from agentboard.research.snapshot import promote_excerpt as _promote

    excerpt = _promote(
        snapshot, section_id=snapshot.sections[0].section_id, checks=ALL_TRUE
    )
    assert not isinstance(excerpt, RetrievedDocument)


def test_l1_dependency_direction_still_holds() -> None:
    """L1：领域层不得 import Provider 层（这条守卫在 P2.1 就存在，此处确认仍然成立）。

    做法是"读源码 + 检查 import"，不是 importlib —— 后者拿到的是已加载模块，看不出依赖方向。
    """
    import ast

    project_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in sorted((project_root / "src" / "agentboard").rglob("*.py")):
        rel = path.relative_to(project_root).as_posix()
        if rel.startswith("src/agentboard/providers/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("agentboard.providers",)
            ):
                offenders.append(f"{rel}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("agentboard.providers"):
                        offenders.append(f"{rel}: import {alias.name}")
    assert offenders == []


def test_official_sources_module_does_not_import_providers() -> None:
    """具体到 P2.2 新增代码：`research/sources.py` 与 `research/attestation.py` 都不得反向依赖。"""
    project_root = Path(__file__).resolve().parents[2]
    for name in ("sources.py", "attestation.py", "snapshot.py"):
        text = (project_root / "src" / "agentboard" / "research" / name).read_text(encoding="utf-8")
        assert "agentboard.providers" not in text
        assert "from ..providers" not in text
        assert "from .providers" not in text


def test_promotable_excerpt_is_a_frozen_value_object() -> None:
    """`PromotableExcerpt` 必须是冻结值对象：可哈希、可比较、不可就地改。"""
    excerpt = PromotableExcerpt(
        candidate_id="pgvector",
        snapshot_version="pgvector.official.2026-09-23",
        captured_at="2026-09-23",
        locator="/sections/0/text",
        excerpt="x",
        source_kind="official_documentation_excerpt",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        excerpt.candidate_id = "other"  # type: ignore[misc]
    assert hash(excerpt) == hash(dataclasses.replace(excerpt))


def test_load_official_sources_has_no_evidence_factory_for_provider_records(
    official_root: Path,
) -> None:
    """`OfficialSources` 的公开方法里没有任何一个接受 provider 采集记录。

    这是"没有适配器"的可执行形态：P2.2-1 没有、也不该有把 `RetrievedDocument`
    转成证据的入口。若将来有人加了这样的方法，本断言会强制它在评审里被看见。
    """
    sources = load_official_sources(official_root)
    public_callables = {
        name
        for name in dir(sources)
        if not name.startswith("_") and callable(getattr(sources, name, None))
    }
    assert public_callables == {
        "candidate_ids",
        "documents",
        "findings",
        "provenance_for",
        "resolve_evidence",
        "snapshot",
        "snapshot_set",  # P2.2-3：构建快照集上下文（promotable 的唯一合法来源），仍非证据工厂
        "worker",
    }

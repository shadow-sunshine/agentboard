"""快照/验证边界：把 Provider 产物映射成可被领域层消费的语料（P3）。

边界在哪里（这是本模块存在的理由）
----------------------------------
Provider 的输出（`RetrievedDocument`）**永远不能直接变成 `EvidenceRef`** ——
那是 L3 静态守卫钉死的构造点白名单（`evidence.py` / `research/fixtures.py` /
`research/migrate.py` / `research/sources.py` / `workers/synthetic.py`），
也是 P2.1 定下的信任规则："联网内容不得自动获得可信证据身份"。

因此本模块做的是一条**显式的中间路**：

    RetrievedDocument
      → SnapshotStore 落盘（内容寻址、去重、不可覆盖）
      → 语料构建（fixture 格式：INDEX.json + 候选快照 + 决策出的 dimension_values）
      → 既有 loader（load_catalog / load_snapshots，含 C7 文件级 digest）
      → FixtureResearchWorker（受控工厂产出 EvidenceRef）
      → assemble_report

运行时自身**零次**构造 `EvidenceRef`。语料快照的 `source_kind` 恒为
`authored_summary`（机器收集、待人工复核，**不可升格**）——
live 内容在 MVP 里不进入 research_grade，这是诚实边界而不是缺陷（见 README）。

确定性
------
语料字节完全由输入决定：候选 / 文档 / 决策全部排序后写入，`sort_keys` + LF，
零时间字段（`captured_at` 由调用方显式传入）。同一批材料重放 ⇒ 逐字节相同语料
⇒ 同一 `report_id`。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..providers.interface import RetrievedDocument
from ..research.config import SOURCE_KIND_AUTHORED_SUMMARY
from ..research.dimensions import DimensionRegistry
from ..research.errors import ResearchValueError
from .decider import DimensionDecision
from .errors import EvidenceBoundaryError

CORPUS_SCHEMA = "research.fixture.v1"
INDEX_SCHEMA = "research.index.v1"


@dataclass(frozen=True, slots=True)
class CandidateMaterial:
    """一个候选在检索阶段取得的材料（已去重、确定性排序）。"""

    candidate_id: str
    documents: tuple[RetrievedDocument, ...]


@dataclass(frozen=True, slots=True)
class CorpusBuildResult:
    corpus_dir: Path
    candidate_ids: tuple[str, ...]
    sections_written: int
    dimension_values_written: int


def _dump(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _sha256_of_json_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dedupe_documents(documents: Sequence[RetrievedDocument]) -> tuple[RetrievedDocument, ...]:
    """按内容哈希去重 + 确定性排序（同内容只保留一份，顺序与到达次序无关）。"""
    unique: dict[str, RetrievedDocument] = {}
    for document in documents:
        unique.setdefault(document.content_sha256, document)
    return tuple(sorted(unique.values(), key=lambda d: (d.content_sha256, d.source_url)))


class CorpusBuilder:
    """把"已落盘材料 + 决策"构建成 fixture 格式语料（快照/验证边界的落盘侧）。"""

    def __init__(
        self,
        *,
        registry: DimensionRegistry,
        dimensions_json: Path,
        captured_at: str,
    ) -> None:
        self._registry = registry
        self._dimensions_json = Path(dimensions_json)
        self._captured_at = captured_at
        self._labels: dict[str, tuple[str, ...]] = {}
        for dimension in registry.dimensions:
            self._labels[dimension.dimension_id] = tuple(dimension.scale_labels)

    def build(
        self,
        *,
        run_dir: Path,
        materials: Mapping[str, CandidateMaterial],
        decisions: Mapping[str, Mapping[str, DimensionDecision]],
    ) -> CorpusBuildResult:
        if not materials:
            raise EvidenceBoundaryError("没有任何候选材料，无法构建语料")
        corpus_dir = Path(run_dir) / "corpus"
        (corpus_dir / "dimensions").mkdir(parents=True, exist_ok=True)
        (corpus_dir / "options").mkdir(parents=True, exist_ok=True)

        dimensions_target = corpus_dir / "dimensions" / "dimensions.v1.json"
        dimensions_target.write_bytes(self._dimensions_json.read_bytes())

        entries: list[dict[str, object]] = []
        sections_total = 0
        values_total = 0
        for candidate_id in sorted(materials):
            material = materials[candidate_id]
            snapshot_version = f"{candidate_id}.live.{self._captured_at}"
            sections: list[dict[str, object]] = []
            for index, document in enumerate(material.documents):
                sections.append(
                    {
                        "section_id": f"s_{candidate_id}_{index:02d}",
                        "heading": document.title,
                        "text": document.content,
                        "tags": ["live_research"],
                    }
                )
            section_ids = {section["section_id"] for section in sections}

            dimension_values: dict[str, dict[str, str]] = {}
            candidate_decisions = decisions.get(candidate_id, {})
            for dimension_id in sorted(candidate_decisions):
                decision = candidate_decisions[dimension_id]
                self._validate_decision(
                    candidate_id=candidate_id, dimension_id=dimension_id,
                    decision=decision, section_ids=section_ids,
                )
                dimension_values[dimension_id] = {
                    "value": decision.value,
                    "section_id": decision.section_id,
                    "claim": decision.claim,
                }

            fixture_payload = {
                "schema": CORPUS_SCHEMA,
                "candidate_id": candidate_id,
                "display_name": f"{candidate_id}（live-research MVP）",
                "snapshot_version": snapshot_version,
                "captured_at": self._captured_at,
                "source_path": f"options/{candidate_id}.json",
                "source_kind": SOURCE_KIND_AUTHORED_SUMMARY,
                # 撰写字段：不得夹带 URL（静态守卫同款纪律）。只写运行元数据。
                "source_note": (
                    "live-research MVP：内容由 Provider 采集并经快照/验证边界落盘，"
                    "待人工复核；按既有信任模型不可升格（research_grade 不可达）。"
                ),
                "sections": sections,
                "dimension_values": dimension_values,
            }
            fixture_path = corpus_dir / "options" / f"{candidate_id}.json"
            fixture_path.write_text(
                _dump(fixture_payload), encoding="utf-8", newline="\n"
            )
            entries.append(
                {
                    "candidate_id": candidate_id,
                    "display_name": fixture_payload["display_name"],
                    "fixture_path": f"options/{candidate_id}.json",
                    "snapshot_version": snapshot_version,
                    "captured_at": self._captured_at,
                    "fixture_sha256": _sha256_of_json_file(fixture_path),
                }
            )
            sections_total += len(sections)
            values_total += len(dimension_values)

        index_payload = {
            "schema": INDEX_SCHEMA,
            "dimension_registry": "dimensions/dimensions.v1.json",
            "dimension_registry_sha256": _sha256_of_json_file(dimensions_target),
            "note": "live-research MVP 语料（运行时产物；不在 fixtures/ 冻结范围内）。",
            "candidates": entries,
        }
        (corpus_dir / "INDEX.json").write_text(
            _dump(index_payload), encoding="utf-8", newline="\n"
        )
        return CorpusBuildResult(
            corpus_dir=corpus_dir,
            candidate_ids=tuple(sorted(materials)),
            sections_written=sections_total,
            dimension_values_written=values_total,
        )

    def _validate_decision(
        self,
        *,
        candidate_id: str,
        dimension_id: str,
        decision: DimensionDecision,
        section_ids: set[str],
    ) -> None:
        labels = self._labels.get(dimension_id)
        if labels is None:
            raise EvidenceBoundaryError(
                f"维度 {dimension_id!r} 不在注册表内（候选 {candidate_id!r}）"
            )
        if decision.value not in labels:
            raise EvidenceBoundaryError(
                f"候选 {candidate_id!r} 维度 {dimension_id!r} 的取值 {decision.value!r} "
                f"不在量表 {list(labels)} 内"
            )
        if decision.section_id not in section_ids:
            raise EvidenceBoundaryError(
                f"候选 {candidate_id!r} 的决策引用了不存在的 section "
                f"{decision.section_id!r}"
            )


def load_corpus(corpus_dir: Path) -> tuple[Any, Any, Any]:
    """用**既有 loader** 装载语料（C7 文件级 digest 在此生效，不绕过）。

    返回 `(catalog, registry, snapshots)`。
    """
    from ..research.dimensions import load_dimension_registry
    from ..research.snapshot import load_catalog, load_snapshots

    base = Path(corpus_dir)
    catalog = load_catalog(base)
    registry = load_dimension_registry(base / catalog.dimension_registry_path)
    snapshots = load_snapshots(base, catalog)
    if not catalog.ids():
        raise ResearchValueError(f"语料目录没有候选：{base}")
    return catalog, registry, snapshots

"""研究领域层测试夹具。

全部只读本地 `fixtures/research/`，不联网、不调模型、不读时钟（需求8/11）。
`make_input` 通过 fixture 暴露（而不是模块级 import），避免 `tests/` 与
`tests/research/` 下同名 `conftest` 在导入期互相干扰。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from agentboard.contracts import ProjectBrief, TaskCard, WorkerResult, WorkerTaskInput
from agentboard.research.assemble import assemble_report
from agentboard.research.dimensions import DimensionRegistry, load_dimension_registry
from agentboard.research.fixtures import FixtureResearchWorker
from agentboard.research.models import SelectionReport
from agentboard.research.snapshot import (
    FixtureCatalog,
    FixtureSnapshot,
    load_catalog,
    load_snapshots,
)
from agentboard.research.sources import OfficialSources, load_official_sources
from agentboard.research.sources import make_task_input as make_official_task_input

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "fixtures" / "research"
OFFICIAL_ROOT = PROJECT_ROOT / "fixtures" / "official"

DEFAULT_CONTEXT_TEMPLATE = "公共背景：为检索服务做技术选型\ncandidate_id: {candidate_id}\n"
RESEARCH_AGENT = "research"


@pytest.fixture(scope="session")
def fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def catalog(fixture_root: Path) -> FixtureCatalog:
    return load_catalog(fixture_root)


@pytest.fixture(scope="session")
def registry(fixture_root: Path, catalog: FixtureCatalog) -> DimensionRegistry:
    return load_dimension_registry(fixture_root / catalog.dimension_registry_path)


@pytest.fixture(scope="session")
def snapshots(fixture_root: Path, catalog: FixtureCatalog) -> Mapping[str, FixtureSnapshot]:
    return load_snapshots(fixture_root, catalog)


@pytest.fixture(scope="session")
def worker(fixture_root: Path) -> FixtureResearchWorker:
    return FixtureResearchWorker.from_fixture_root(fixture_root)


@pytest.fixture
def make_input() -> Callable[..., WorkerTaskInput]:
    def _make(
        candidate_id: str | None = "pgvector",
        *,
        task_id: str | None = None,
        context: str | None = None,
        upstream: Mapping[str, WorkerResult] | None = None,
        assigned_agent: str = RESEARCH_AGENT,
    ) -> WorkerTaskInput:
        body = DEFAULT_CONTEXT_TEMPLATE.format(candidate_id=candidate_id) if context is None else context
        return WorkerTaskInput(
            task_id=task_id or f"research__{candidate_id}",
            title="调研候选方案",
            assigned_agent=assigned_agent,
            goal="调研该候选方案在本快照里的表现",
            context=body,
            upstream_results=dict(upstream or {}),
        )

    return _make


@pytest.fixture
def make_result(
    worker: FixtureResearchWorker, make_input: Callable[..., WorkerTaskInput]
) -> Callable[[str], WorkerResult]:
    """跑一遍真实的 FixtureResearchWorker，拿到结构化 WorkerResult。"""

    def _make(candidate_id: str) -> WorkerResult:
        return asyncio.run(worker.run(make_input(candidate_id)))

    return _make


@pytest.fixture
def make_cards(
    make_result: Callable[[str], WorkerResult], worker: FixtureResearchWorker
) -> Callable[..., list[TaskCard]]:
    """构造已完成的 research 卡（可直接喂给 assemble_report）。"""

    def _make(
        candidate_ids: Sequence[str] | None = None,
        *,
        status: str = "done",
        assigned_agent: str = RESEARCH_AGENT,
        with_result: bool = True,
    ) -> list[TaskCard]:
        ids = tuple(candidate_ids) if candidate_ids is not None else worker.candidate_ids
        cards: list[TaskCard] = []
        for candidate_id in ids:
            cards.append(
                TaskCard(
                    task_id=f"research__{candidate_id}",
                    title="调研候选方案",
                    assigned_agent=assigned_agent,
                    goal="调研该候选方案在本快照里的表现",
                    context=DEFAULT_CONTEXT_TEMPLATE.format(candidate_id=candidate_id),
                    status=status,
                    result=make_result(candidate_id) if with_result else None,
                    attempts=1,
                )
            )
        return cards

    return _make


@pytest.fixture
def brief() -> ProjectBrief:
    return ProjectBrief(text="为检索服务做技术选型", source="docs/research/selection_goal.md")


@pytest.fixture
def report(
    brief: ProjectBrief,
    make_cards: Callable[..., list[TaskCard]],
    registry: DimensionRegistry,
    snapshots: Mapping[str, FixtureSnapshot],
) -> SelectionReport:
    return assemble_report(brief, make_cards(), registry, snapshots=snapshots)


@pytest.fixture
def cloned_fixture_root(fixture_root: Path, tmp_path: Path) -> Path:
    """把真实 fixture 目录复制到 tmp，供"改坏一份再断言报错"的用例使用。"""
    target = tmp_path / "research"
    shutil.copytree(fixture_root, target)
    return target


# ---- 官方资料快照（fixtures/official，真实资料接入第一步） ----


@pytest.fixture(scope="session")
def official_root() -> Path:
    return OFFICIAL_ROOT


@pytest.fixture(scope="session")
def official_sources(official_root: Path) -> OfficialSources:
    return load_official_sources(official_root)


@pytest.fixture
def cloned_official_root(official_root: Path, tmp_path: Path) -> Path:
    """复制一份官方快照目录到 tmp，供"改坏 provenance 再断言报错"的用例使用。"""
    target = tmp_path / "official"
    shutil.copytree(official_root, target)
    return target


@pytest.fixture
def official_cards(
    official_sources: OfficialSources,
) -> Callable[..., list[TaskCard]]:
    """把官方快照跑成已完成的 research 卡（复用同一个 worker，无适配层）。"""

    def _make(candidate_ids: Sequence[str] | None = None) -> list[TaskCard]:
        ids = (
            tuple(candidate_ids) if candidate_ids is not None else official_sources.candidate_ids()
        )
        worker = official_sources.worker()
        cards: list[TaskCard] = []
        for candidate_id in ids:
            task_input = make_official_task_input(candidate_id)
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

    return _make


@pytest.fixture
def official_brief() -> ProjectBrief:
    return ProjectBrief(
        text="为检索服务做技术选型（依据官方文档快照）",
        source="docs/research/selection_goal.md",
    )


@pytest.fixture
def official_report(
    official_brief: ProjectBrief,
    official_cards: Callable[..., list[TaskCard]],
    official_sources: OfficialSources,
) -> SelectionReport:
    return assemble_report(
        official_brief,
        official_cards(),
        official_sources.registry,
        snapshots=official_sources.snapshots,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_sha1(data: bytes) -> str:
    """Git 对象 id：`sha1("blob <字节数>\\0" + 内容)`。

    在测试里独立算一份（而不是复用工具的 `git_blob_sha1`）：被测对象就是那个工具，
    用它的函数来构造输入会让"它算错了"这件事同时污染两端。
    """
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


# ---- 自洽的合成官方快照集（P2.2-1：升格闸门 / 证明工件） ----
#
# 为什么需要合成而不是直接用 fixtures/official：要生成一份 `overall_result="passed"`
# 的证明，必须有**与 provenance 登记的 raw_sha256 逐字节匹配**的原件；而真实原件
# 按设计不入库（`.workbuddy/tmp/audit` 是本机运行产物，不能当测试输入依赖）。
# 所以这里合成一条完整且自洽的链条：
#
#     原件字节 ──▶ raw_sha256 / blob_sha / raw_lines 登记 ──▶ 摘录切片 == section.text
#
# 造**两个**候选而不是一个：集合语义（`snapshot_files[]` 必须与登记集合"不多不少"）
# 在只有一个成员的集合上测不出区别 —— 删掉唯一成员会退化成 schema 错误，
# 于是"集合不一致"这条路径根本没被覆盖。
#
# 生成的目录**全部在 tmp 下**，绝不触碰 `fixtures/**`，也绝不写项目根的 `attestations/`。

SYNTHETIC_PRIMARY_CANDIDATE = "pgvector"
SYNTHETIC_DIMENSION = "deployment_complexity"
SYNTHETIC_SNAPSHOT_SET = "official.2026-09-23"
SYNTHETIC_CAPTURED_AT = "2026-09-23"
#: 其余注册维度显式登记为 unknown（不推断补齐），保证加载期的维度全覆盖校验通过。
SYNTHETIC_UNKNOWN_DIMENSIONS = (
    "ops_skill_floor",
    "query_latency",
    "scale_ceiling",
    "ecosystem_maturity",
    "managed_service_needed",
)

SYNTHETIC_CANDIDATE_SPECS: tuple[dict[str, str], ...] = (
    {
        "candidate_id": "pgvector",
        "repo": "pgvector/pgvector",
        "document_id": "doc_pgvector_readme",
        "section_id": "s_pg_extension_enable",
        "section_text": (
            "Enable the extension (do this once in each database where you want to use it)"
        ),
        "file_name": "README.md",
    },
    {
        "candidate_id": "qdrant_local",
        "repo": "qdrant/qdrant",
        "document_id": "doc_qdrant_readme",
        "section_id": "s_qd_single_node",
        "section_text": "Start a local Qdrant instance with a single docker run command.",
        "file_name": "quickstart.md",
    },
)
SYNTHETIC_COMMIT = "7db2345ed99bc77bf33cbdc8b12bd1973210dc81"


@dataclass(frozen=True)
class SyntheticOfficialSet:
    """一套自洽的合成官方快照集：目录、原件目录、以及（可选）已生成的证明路径。"""

    root: Path
    raw_dir: Path
    index_path: Path
    dimensions_path: Path
    attestation_path: Path
    fixture_paths: Mapping[str, Path]
    raw_paths: Mapping[str, Path]
    section_line_range: str

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.fixture_paths))

    @property
    def fixture_path(self) -> Path:
        return self.fixture_paths[SYNTHETIC_PRIMARY_CANDIDATE]

    @property
    def raw_path(self) -> Path:
        return self.raw_paths[SYNTHETIC_PRIMARY_CANDIDATE]

    def rewrite_snapshot(self, mutate: Callable[[dict], None], candidate_id: str | None = None) -> None:
        """就地改快照文件（负向控制用），**不重签** INDEX.json。

        刻意不重签：这样"改了文件但没重签"这一状态可以被两个独立的守卫分别看到 ——
        加载期 C7（比对 INDEX 登记值）与升格期 C6（比对证明登记值）。
        """
        target = self.fixture_paths[candidate_id or SYNTHETIC_PRIMARY_CANDIDATE]
        payload = json.loads(target.read_text(encoding="utf-8"))
        mutate(payload)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )


def _count_lines(text: str) -> int:
    return len(text.split("\n")) - (1 if text.endswith("\n") else 0)


def _build_synthetic_official_set(base: Path) -> SyntheticOfficialSet:
    """在 `base` 下造一套自洽的官方快照集（+ 原件目录），签名对齐，但不生成证明。"""
    root = base / "official"
    raw_dir = base / "raw"
    (root / "dimensions").mkdir(parents=True, exist_ok=True)
    (root / "sources").mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    dimensions_path = root / "dimensions" / "dimensions.v1.json"
    shutil.copyfile(OFFICIAL_ROOT / "dimensions" / "dimensions.v1.json", dimensions_path)

    fixture_paths: dict[str, Path] = {}
    raw_paths: dict[str, Path] = {}
    entries: list[dict[str, str]] = []
    section_line_range = "3"

    for spec in SYNTHETIC_CANDIDATE_SPECS:
        candidate_id = spec["candidate_id"]
        lines = [f"# {candidate_id}", "", spec["section_text"], ""]
        raw_text = "\n".join(lines) + "\n"
        raw_bytes = raw_text.encode("utf-8")
        raw_path = raw_dir / spec["file_name"]
        raw_path.write_bytes(raw_bytes)
        raw_paths[candidate_id] = raw_path

        # section 的 text 必须**恰好**是原文的一段连续行切片（1 起闭区间）
        section_line_range = str(lines.index(spec["section_text"]) + 1)
        version = f"{candidate_id}.official.{SYNTHETIC_CAPTURED_AT}"

        fixture_payload = {
            "schema": "research.fixture.v1",
            "candidate_id": candidate_id,
            "display_name": f"{candidate_id}（合成测试集）",
            "snapshot_version": version,
            "captured_at": SYNTHETIC_CAPTURED_AT,
            "source_path": f"sources/{candidate_id}.json",
            "source_kind": "official_documentation_excerpt",
            "source_note": "合成的测试快照：用于离线验证升格闸门与外部证明工件，不代表真实资料。",
            "sections": [
                {
                    "section_id": spec["section_id"],
                    "heading": spec["file_name"],
                    "text": spec["section_text"],
                    "tags": ["deployment"],
                    "source_id": spec["document_id"],
                    "line_range": section_line_range,
                }
            ],
            "dimension_values": {
                SYNTHETIC_DIMENSION: {
                    "value": "low",
                    "section_id": spec["section_id"],
                    "claim": "合成用例：单步启用、无额外服务进程。",
                    "source_id": spec["document_id"],
                    "basis": "mapped",
                    "derivation": "原文给出单步启用步骤 → 无额外服务 ⇒ low。",
                }
            },
            "provenance": {
                "schema": "research.sources.v1",
                "candidate_id": candidate_id,
                "collected_at": SYNTHETIC_CAPTURED_AT,
                "collection_method": "人工一次性采集：下载固定 commit 的原件、按行号切片、登记 sha256 与行号。",
                "documents": [
                    {
                        "source_id": spec["document_id"],
                        "kind": "official_repo_file",
                        "title": f"{spec['repo']}/{spec['file_name']}",
                        "repo": spec["repo"],
                        "path": spec["file_name"],
                        "url": (
                            f"https://github.com/{spec['repo']}/blob/{SYNTHETIC_COMMIT}/"
                            f"{spec['file_name']}"
                        ),
                        "raw_url": (
                            f"https://raw.githubusercontent.com/{spec['repo']}/{SYNTHETIC_COMMIT}/"
                            f"{spec['file_name']}"
                        ),
                        "ref": "master",
                        "ref_kind": "branch",
                        "commit_sha": SYNTHETIC_COMMIT,
                        "blob_sha": _git_blob_sha1(raw_bytes),
                        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                        "release_tag": "unknown",
                        "release_tag_note": "合成用例：文件自身不声明版本，锚点是 commit / blob / sha256。",
                        "upstream_latest_release": "v0.8.6",
                        "upstream_latest_release_note": "合成用例占位值，仅作参考。",
                        "upstream_license": "NOASSERTION",
                        "upstream_license_note": "合成用例占位值。",
                        "collected_at": SYNTHETIC_CAPTURED_AT,
                        "raw_lines": _count_lines(raw_text),
                    }
                ],
                "unknown_dimensions": {
                    name: {
                        "reason": "合成用例：本轮只声明 deployment_complexity，其余维度显式登记为无法确认。",
                        "checked_source_ids": [spec["document_id"]],
                    }
                    for name in SYNTHETIC_UNKNOWN_DIMENSIONS
                },
            },
        }

        fixture_path = root / "sources" / f"{candidate_id}.json"
        fixture_path.write_text(
            json.dumps(fixture_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        fixture_paths[candidate_id] = fixture_path
        entries.append(
            {
                "candidate_id": candidate_id,
                "display_name": f"{candidate_id}（合成测试集）",
                "fixture_path": f"sources/{candidate_id}.json",
                "snapshot_version": version,
                "captured_at": SYNTHETIC_CAPTURED_AT,
                "fixture_sha256": _sha256(fixture_path),
            }
        )

    index_path = root / "INDEX.json"
    index_path.write_text(
        json.dumps(
            {
                "schema": "research.index.v1",
                "dimension_registry": "dimensions/dimensions.v1.json",
                "dimension_registry_sha256": _sha256(dimensions_path),
                "note": "合成测试集的候选清单。",
                "snapshot_set": SYNTHETIC_SNAPSHOT_SET,
                "candidates": entries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    return SyntheticOfficialSet(
        root=root,
        raw_dir=raw_dir,
        index_path=index_path,
        dimensions_path=dimensions_path,
        attestation_path=base / "attestations" / "official.sources.v1.json",
        fixture_paths=fixture_paths,
        raw_paths=raw_paths,
        section_line_range=section_line_range,
    )


@pytest.fixture
def synthetic_official_set(tmp_path: Path) -> SyntheticOfficialSet:
    """自洽的合成官方快照集（**未**生成证明）。"""
    return _build_synthetic_official_set(tmp_path / "corpus")


@pytest.fixture
def make_synthetic_official_set(tmp_path: Path) -> Callable[[str], SyntheticOfficialSet]:
    """按名字造多套互不干扰的合成快照集（每套一个子目录）。"""

    def _make(name: str) -> SyntheticOfficialSet:
        return _build_synthetic_official_set(tmp_path / name)

    return _make


def load_verify_tool() -> ModuleType:
    """把审计工具作为模块加载。

    工具刻意**只依赖标准库**（不 import `agentboard`）：审计者与被审计实现解耦，
    "库解析错了"不会同时污染两端。代价是测试必须自己加载它。
    """
    import importlib.util
    import sys as _sys

    path = PROJECT_ROOT / "tools" / "verify_sources.py"
    spec = importlib.util.spec_from_file_location("agentboard_verify_sources_research", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先注册到 sys.modules：工具里有 @dataclass，dataclasses 会用它解析字符串注解。
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def verify_tool() -> ModuleType:
    """加载 `tools/verify_sources.py`（工具不 import 库，因此需要显式加载）。"""
    return load_verify_tool()


@pytest.fixture
def attestation_generator(
    synthetic_official_set: SyntheticOfficialSet,
) -> Callable[[Path | None], Path]:
    """用**真工具**为合成快照集生成证明，返回证明路径。

    用真工具而不是手拼 JSON：手拼会让"工具与库对 schema 的理解是否一致"这个
    最容易漂移的地方失去覆盖。
    """
    tool = load_verify_tool()

    def _generate(target: Path | None = None) -> Path:
        path = target or synthetic_official_set.attestation_path
        result = tool.verify_sources(synthetic_official_set.root, synthetic_official_set.raw_dir)
        assert result.problems == [], result.problems
        payload = tool.build_attestation_payload(synthetic_official_set.root, result)
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, message = tool.write_attestation(path, tool.attestation_bytes(payload))
        assert ok, message
        return path

    return _generate


@pytest.fixture
def resign_fixture() -> Callable[[Path], None]:
    """重新签名 INDEX.json。

    P1A-4 起 fixture 有文件级 digest：**改了文件就必须重签**，否则加载会先抛
    `SnapshotDigestError`、把真正要测的语义错误挡在后面。这既是测试需要，
    也是真实工作流（改快照 -> `python tools/sign_fixtures.py --write`）。
    """

    def _resign(root: Path) -> None:
        index_path = root / "INDEX.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["dimension_registry_sha256"] = _sha256(root / index["dimension_registry"])
        for entry in index["candidates"]:
            entry["fixture_sha256"] = _sha256(root / entry["fixture_path"])
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    return _resign

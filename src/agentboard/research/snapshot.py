"""本地资料快照的加载与校验（P1A-2）。

快照 = 从资料里摘录下来的**本地的、可复核的**文本块 + 候选在各维度上的取值声明。
全部读取本地 JSON，不联网、不读时钟：`captured_at` 来自文件里的固定元数据。

目录形态：

    fixtures/research/
      INDEX.json                       候选清单（含每个 option 的版本 / 拍摄日期）
      dimensions/dimensions.v1.json    维度注册表（ordinal / boolean）
      options/<candidate_id>.json      每个候选的资料快照

`read_json_object` 是本领域层读取本地 JSON 的唯一实现，`dimensions.py` 复用它，
避免同一个解析/报错逻辑写两遍。

P2.2-1 起本模块还承担两件事：

1. **`source_kind` 的加载期强制**（`_read_source_kind` / `_enforce_source_kind_provenance`）：
   把"官方原文"与"人工摘要"从目录约定的口头区别变成加载期强制的机器区别；
2. **升格闸门的类型**（`PromotionChecks` / `PromotableExcerpt` / `promote_excerpt`）：
   判定逻辑在 `sources.py::is_promotable_source`，这里只提供它消费的类型与唯一构造入口。

两者都**只读本地 JSON**，因此不改变任何 fixture 字节（P2.2_PLAN §6.1.1 `G-PRESERVE`）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..evidence import CAPTURED_AT_PATTERN, MAX_EXCERPT_CHARS, SNAPSHOT_VERSION_PATTERN
from ..util import freeze_payload
from .config import (
    CANDIDATE_ID_PATTERN,
    DIMENSION_ID_PATTERN,
    FIXTURE_SCHEMA,
    INDEX_SCHEMA,
    SNAPSHOT_SET_PATTERN,
    SOURCE_KIND_AUTHORED_SUMMARY,
    SOURCE_KIND_OFFICIAL_EXCERPT,
    VALID_SNAPSHOT_SET_POLICIES,
    VALID_SOURCE_KINDS,
)
from .errors import (
    CandidateCoverageError,
    DanglingSectionError,
    MissingProvenanceError,
    PromotionError,
    ResearchValueError,
    SnapshotDigestError,
    SnapshotMismatchError,
    SnapshotSchemaError,
    SourceProvenanceSchemaError,
    UnknownCandidateError,
    UnknownSourceKindError,
    UnsafeSourcePathError,
)

INDEX_FILENAME = "INDEX.json"
DIMENSIONS_REL = "dimensions/dimensions.v1.json"
OPTIONS_DIR = "options"

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def file_sha256(path: Path) -> str:
    """文件级 sha256（分块读取）。

    与 `EvidenceRef.digest` 的分工：`digest` 保证"某一条摘录在证据层没被篡改"，
    文件级 digest 保证"整份快照文件就是 INDEX.json 登记的那一份"。前者管内容，
    后者管发布完整性 —— 少了它，"整份 option 文件被替换"是检测不到的。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(raw: Mapping[str, Any], key: str, label: str) -> str:
    value = _require_str(raw, key, label)
    if not SHA256_PATTERN.match(value):
        raise SnapshotDigestError(f"{label}.{key} 必须是 64 位小写十六进制，实际为 {value!r}")
    return value


def _verify_file_digest(path: Path, expected: str, label: str) -> None:
    actual = file_sha256(path)
    if actual != expected:
        raise SnapshotDigestError(
            f"{label} 的 sha256 与 INDEX.json 登记的不一致："
            f"登记 {expected}，实际 {actual}。若是有意修改，请运行 "
            f"`python tools/sign_fixtures.py --write` 重新签名"
        )

#: fixture 证据的 locator 形态，只此一种：`/sections/<index>/text`。
#: 生成（`section_locator`）与解析（`resolve_locator`）共用同一个正则，避免两边漂移。
SECTION_LOCATOR_PATTERN = re.compile(r"^/sections/(\d+)/text$")


def section_locator(index: int) -> str:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise SnapshotSchemaError(f"section 序号必须是非负整数（禁止 bool），实际为 {index!r}")
    return f"/sections/{index}/text"


def expected_excerpt_for(text: str) -> str:
    """规范摘录截断 —— **全仓唯一实现点**（P2.2-2 硬门禁，修 D2 的另一半）。

    规则：`len(text) <= MAX_EXCERPT_CHARS` 取全文，否则取前 `MAX_EXCERPT_CHARS` 字符。

    为什么必须只有一份实现：摘录进 `compute_evidence_id`、进而进 `report_id`；
    而"证据可解析"要靠「`excerpt == expected_excerpt_for(原文)`」判定。
    同一规则历史上写过 4 份（`fixtures.py` / `migrate.py` / 本文件旧版 `snapshot_excerpt` /
    `sources.py` 内联），靠等价性**测试**维持一致 ——     那是"恰好相等"，不是"结构上不可能分叉"：
    任何一处被单独改动，D2 就会以新形态复活。收敛后由
    `tests/research/test_locator_consistency.py::test_excerpt_truncation_has_single_implementation_site`
    钉住：`src/` 内出现第二处对 `MAX_EXCERPT_CHARS` 的**前缀切片**即变红
    （该测试同时用文本行扫描与 AST 两种口径；本文档字符串刻意不写出那个切片字面量，
    以免污染文本口径的计数）。

    上限校验（`excerpt` 不得超过 400）仍在 `evidence.py::EvidenceRef` ——
    那是**约束**，不是截断实现，两者刻意分开。
    """
    if not isinstance(text, str):
        raise SnapshotSchemaError(f"摘录截断的输入必须是 str，实际为 {type(text).__name__}")
    return text if len(text) <= MAX_EXCERPT_CHARS else text[:MAX_EXCERPT_CHARS]


def resolve_locator(snapshot: FixtureSnapshot, locator: str) -> str:
    """把 fixture 证据的 locator 解析回快照里的原文。

    这是"证据不是悬空引用"的可执行检查：worker 产出的每条 fixture 证据，
    其 locator 都必须能在快照里定位到一个真实 section。
    """
    if not isinstance(locator, str):
        raise DanglingSectionError(f"locator 必须是字符串，实际为 {type(locator).__name__}")
    match = SECTION_LOCATOR_PATTERN.match(locator)
    if match is None:
        raise DanglingSectionError(
            f"不支持的 fixture locator：{locator!r}（只支持 /sections/<index>/text）"
        )
    index = int(match.group(1))
    if index >= len(snapshot.sections):
        raise DanglingSectionError(
            f"候选 {snapshot.candidate_id!r} 只有 {len(snapshot.sections)} 个 section，"
            f"无法解析 {locator!r}"
        )
    return snapshot.sections[index].text


def read_json_object(path: Path) -> dict[str, Any]:
    """读取本地 JSON 对象。文件缺失 / 非法 JSON / 顶层不是对象，一律拒绝。"""
    if not isinstance(path, Path):
        raise ResearchValueError("read_json_object 需要 pathlib.Path")
    if not path.is_file():
        raise SnapshotSchemaError(f"找不到文件：{path}")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotSchemaError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(loaded, dict):
        raise SnapshotSchemaError(f"{path} 的顶层必须是 JSON 对象")
    return loaded


def _require_schema(raw: Mapping[str, Any], expected: str, label: str) -> None:
    actual = raw.get("schema")
    if actual != expected:
        raise SnapshotSchemaError(f"{label} 的 schema 期望 {expected!r}，实际为 {actual!r}")


def _require_str(raw: Mapping[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SnapshotSchemaError(f"{label}.{key} 必须是非空字符串，实际为 {value!r}")
    return value


def _require_str_list(raw: Mapping[str, Any], key: str, label: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise SnapshotSchemaError(f"{label}.{key} 必须是 list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SnapshotSchemaError(f"{label}.{key} 的元素必须是字符串，实际为 {type(item).__name__}")
        out.append(item)
    return tuple(out)


def _require_pattern(value: str, pattern: Any, label: str, hint: str) -> str:
    if not pattern.match(value):
        raise SnapshotSchemaError(f"{label} 必须满足 {hint}，实际为 {value!r}")
    return value


def _require_relative_local_path(value: str, label: str) -> str:
    """快照必须完全本地：source_path 只能是相对路径，且不得包含 URL。"""
    if "://" in value:
        raise UnsafeSourcePathError(f"{label} 不得包含 URL（快照必须完全本地）：{value!r}")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise UnsafeSourcePathError(f"{label} 必须是相对路径：{value!r}")
    return value


@dataclass(frozen=True)
class FixtureSection:
    """快照里的一段原文，是可被 evidence locator 指向的最小单元。"""

    section_id: str
    heading: str
    text: str
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "heading": self.heading,
            "text": self.text,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], label: str) -> FixtureSection:
        if not isinstance(raw, Mapping):
            raise SnapshotSchemaError(f"{label} 必须是 JSON 对象")
        section_id = _require_str(raw, "section_id", label)
        return cls(
            section_id=section_id,
            heading=_require_str(raw, "heading", label),
            text=_require_str(raw, "text", label),
            tags=_require_str_list(raw, "tags", label),
        )


@dataclass(frozen=True)
class FixtureDimensionValue:
    """候选在某个维度上的取值声明，并指向支撑它的 section。

    `dimension_id` 是**原始声明**，可能是一个 alias；归一到注册表由上层负责。
    """

    dimension_id: str
    value: str
    section_id: str
    claim: str

    def to_dict(self) -> dict[str, str]:
        return {
            "dimension_id": self.dimension_id,
            "value": self.value,
            "section_id": self.section_id,
            "claim": self.claim,
        }


@dataclass(frozen=True)
class FixtureSnapshot:
    """一个候选的本地资料快照。

    `source_kind` 区分两类内容，**加载期强制**（P2.2-1）：

    - `official_documentation_excerpt`：官方文档 / 官方仓库文件的逐行摘录，
      **必须**携带 `provenance` 块（缺失即 `MissingProvenanceError`）；
    - `authored_summary`：人工撰写的技术摘要，**不得**带 `provenance`
      （出现即 `SourceProvenanceSchemaError` —— 人工摘要不得冒充官方来源）。

    这条交叉强制让"官方原文"与"人工摘要"从"目录约定的口头区别"变成"加载期强制的机器区别"。

    `snapshot_set` 是**快照集归属标识**（P2.2-3）：由 `load_snapshots` 从所属
    `FixtureCatalog.snapshot_set` 戳印（如 `official.2026-09-23`）。
    它回答的问题是："这条快照**可证明**属于哪个快照集？" ——
    `""` 表示所属目录未声明快照集（如 P1A 的 `fixtures/research/`），
    即**归属不可证明**；任何要求归属证明的场合（快照集声明、`research_grade`）
    对 `""` 一律 fail-closed，绝不凭 `candidate_id` 相同就推断同源。
    """

    candidate_id: str
    display_name: str
    snapshot_version: str
    captured_at: str
    source_path: str
    source_kind: str
    sections: tuple[FixtureSection, ...]
    dimension_values: Mapping[str, FixtureDimensionValue]
    snapshot_set: str = ""
    #: 加载期戳印（P2.2-3 attempt 3）：来源文件相对路径与**当时测量**的文件摘要。
    #: 定位为**索引，非权威**（P2.2-3_REDESIGN §1）：组装期会现读磁盘重算，
    #: 戳印只回答"该去哪个文件现读"；公开构造器可填任意值，
    #: 填错的后果只是被判 `uncovered_candidate`，**不可能制造通过**。
    #: 不进 `to_dict()`：保持既有序列化字节稳定（开放问题 3 的裁决：不导出）。
    fixture_relpath: str = ""
    fixture_sha256: str = ""

    def __post_init__(self) -> None:
        if self.source_kind not in VALID_SOURCE_KINDS:
            raise UnknownSourceKindError(
                f"候选 {self.candidate_id!r} 的 source_kind 必须是 "
                f"{sorted(VALID_SOURCE_KINDS)}，实际为 {self.source_kind!r}"
            )
        if self.snapshot_set and not SNAPSHOT_SET_PATTERN.match(self.snapshot_set):
            raise SnapshotSchemaError(
                f"候选 {self.candidate_id!r} 的 snapshot_set {self.snapshot_set!r} "
                f"不符合 SNAPSHOT_SET_PATTERN"
            )

    def section_index(self, section_id: str) -> int:
        """section 在文件中的序号，用于构造 `/sections/{index}/text` 形式的 locator。"""
        for index, section in enumerate(self.sections):
            if section.section_id == section_id:
                return index
        raise DanglingSectionError(f"{self.candidate_id} 里没有 section {section_id!r}")

    def empty_dimensions(self, dimension_ids: tuple[str, ...]) -> tuple[str, ...]:
        """在给定维度集合里，本快照**没有**声明取值的那些（供生成 unsupported finding）。"""
        declared = set(self.dimension_values)
        return tuple(dim_id for dim_id in dimension_ids if dim_id not in declared)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "display_name": self.display_name,
            "snapshot_version": self.snapshot_version,
            "captured_at": self.captured_at,
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "snapshot_set": self.snapshot_set,
            "sections": [section.to_dict() for section in self.sections],
            "dimension_values": {
                key: value.to_dict() for key, value in self.dimension_values.items()
            },
        }


@dataclass(frozen=True)
class CandidateEntry:
    """INDEX.json 里的一个候选条目。"""

    candidate_id: str
    display_name: str
    fixture_path: str
    snapshot_version: str
    captured_at: str
    fixture_sha256: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "display_name": self.display_name,
            "fixture_path": self.fixture_path,
            "snapshot_version": self.snapshot_version,
            "captured_at": self.captured_at,
            "fixture_sha256": self.fixture_sha256,
        }


@dataclass(frozen=True)
class FixtureCatalog:
    """候选目录：索引 + 维度注册表位置。按 candidate_id 排序，顺序稳定。

    `snapshot_set` 是 INDEX.json 里**可选**的快照集标识（如 `official.2026-09-23`）。
    P1A 的 `fixtures/research/INDEX.json` 没有这个键，因此缺失时为 `""` ——
    读取它是**只读**操作，不改变任何 fixture 字节。
    """

    root: str
    dimension_registry_path: str
    candidates: tuple[CandidateEntry, ...]
    dimension_registry_sha256: str = ""
    snapshot_set: str = ""

    def ids(self) -> tuple[str, ...]:
        return tuple(entry.candidate_id for entry in self.candidates)

    def has(self, candidate_id: str) -> bool:
        return any(entry.candidate_id == candidate_id for entry in self.candidates)

    def get(self, candidate_id: str) -> CandidateEntry:
        for entry in self.candidates:
            if entry.candidate_id == candidate_id:
                return entry
        raise UnknownCandidateError(
            f"快照目录里没有候选 {candidate_id!r}；已知候选：{list(self.ids())}"
        )


def load_catalog(root: Path) -> FixtureCatalog:
    """读取 INDEX.json。候选按 candidate_id 排序，重复 id 即报错。"""
    raw = read_json_object(root / INDEX_FILENAME)
    _require_schema(raw, INDEX_SCHEMA, INDEX_FILENAME)

    registry_rel = _require_relative_local_path(
        _require_str(raw, "dimension_registry", INDEX_FILENAME), "INDEX.json.dimension_registry"
    )
    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise SnapshotSchemaError("INDEX.json.candidates 必须是非空列表")

    entries: list[CandidateEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_candidates):
        label = f"candidates[{index}]"
        if not isinstance(item, Mapping):
            raise SnapshotSchemaError(f"{label} 必须是 JSON 对象")
        candidate_id = _require_str(item, "candidate_id", label)
        _require_pattern(candidate_id, CANDIDATE_ID_PATTERN, f"{label}.candidate_id", "lower_snake_case")
        if candidate_id in seen:
            raise SnapshotSchemaError(f"重复 candidate_id：{candidate_id!r}")
        seen.add(candidate_id)
        fixture_path = _require_relative_local_path(
            _require_str(item, "fixture_path", label), f"{label}.fixture_path"
        )
        snapshot_version = _require_str(item, "snapshot_version", label)
        _require_pattern(
            snapshot_version, SNAPSHOT_VERSION_PATTERN, f"{label}.snapshot_version", "点号风格版本号"
        )
        captured_at = _require_str(item, "captured_at", label)
        _require_pattern(captured_at, CAPTURED_AT_PATTERN, f"{label}.captured_at", "YYYY-MM-DD")
        entries.append(
            CandidateEntry(
                candidate_id=candidate_id,
                display_name=_require_str(item, "display_name", label),
                fixture_path=fixture_path,
                snapshot_version=snapshot_version,
                captured_at=captured_at,
                fixture_sha256=_require_sha256(item, "fixture_sha256", label),
            )
        )

    entries.sort(key=lambda entry: entry.candidate_id)
    registry_sha = _require_sha256(raw, "dimension_registry_sha256", INDEX_FILENAME)
    _verify_file_digest(root / registry_rel, registry_sha, registry_rel)
    return FixtureCatalog(
        root=".",
        dimension_registry_path=registry_rel,
        candidates=tuple(entries),
        dimension_registry_sha256=registry_sha,
        snapshot_set=_read_snapshot_set(raw),
    )


def _read_snapshot_set(raw: Mapping[str, Any]) -> str:
    """读取 INDEX.json 里**可选**的 `snapshot_set`。

    刻意不做"缺失就报错"：P1A 的 `fixtures/research/INDEX.json` 本来就没有这个键，
    把它变成必填等于给既有基线加约束。但**存在就必须合法**——
    悄悄接受一个拼错的快照集标识，会让 attestation 的 `snapshot_set_id` 对齐失去意义。
    """
    value = raw.get("snapshot_set")
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise SnapshotSchemaError(f"{INDEX_FILENAME}.snapshot_set 必须是非空字符串，实际为 {value!r}")
    if not SNAPSHOT_SET_PATTERN.match(value):
        raise SnapshotSchemaError(
            f"{INDEX_FILENAME}.snapshot_set 必须形如 <kind>.<version>（如 official.2026-09-23），"
            f"实际为 {value!r}"
        )
    return value


def load_snapshot(
    path: Path,
    *,
    entry: CandidateEntry,
    snapshot_set: str = "",
    fixture_relpath: str = "",
) -> FixtureSnapshot:
    """读取单个候选快照，并与 INDEX.json 的条目交叉校验。

    `snapshot_set`：所属目录的快照集标识（来自 `FixtureCatalog.snapshot_set`），
    加载成功后**戳印**到快照上 —— 这是快照集归属的唯一合法来源（P2.2-3）。
    `fixture_relpath`：来源文件相对路径戳印（attempt 3），loader 传入 `entry.fixture_path`。
    """
    raw = read_json_object(path)
    _require_schema(raw, FIXTURE_SCHEMA, path.name)

    candidate_id = _require_str(raw, "candidate_id", path.name)
    _require_pattern(candidate_id, CANDIDATE_ID_PATTERN, f"{path.name}.candidate_id", "lower_snake_case")
    if candidate_id != entry.candidate_id:
        raise SnapshotMismatchError(
            f"{path.name} 的 candidate_id {candidate_id!r} 与 INDEX.json 的 {entry.candidate_id!r} 不一致"
        )

    snapshot_version = _require_str(raw, "snapshot_version", path.name)
    captured_at = _require_str(raw, "captured_at", path.name)
    _require_pattern(
        snapshot_version, SNAPSHOT_VERSION_PATTERN, f"{path.name}.snapshot_version", "点号风格版本号"
    )
    _require_pattern(captured_at, CAPTURED_AT_PATTERN, f"{path.name}.captured_at", "YYYY-MM-DD")
    if snapshot_version != entry.snapshot_version or captured_at != entry.captured_at:
        raise SnapshotMismatchError(
            f"{path.name} 的 snapshot_version / captured_at 与 INDEX.json 不一致："
            f"({snapshot_version!r}, {captured_at!r}) vs "
            f"({entry.snapshot_version!r}, {entry.captured_at!r})"
        )

    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise SnapshotSchemaError(f"{path.name}.sections 必须是非空列表")
    sections: list[FixtureSection] = []
    section_ids: set[str] = set()
    for index, item in enumerate(raw_sections):
        section = FixtureSection.from_dict(item, f"{path.name}.sections[{index}]")
        if section.section_id in section_ids:
            raise SnapshotSchemaError(f"{path.name} 重复 section_id：{section.section_id!r}")
        section_ids.add(section.section_id)
        sections.append(section)

    raw_values = raw.get("dimension_values", {})
    if not isinstance(raw_values, Mapping):
        raise SnapshotSchemaError(f"{path.name}.dimension_values 必须是 JSON 对象")
    values: dict[str, FixtureDimensionValue] = {}
    for key, item in raw_values.items():
        label = f"{path.name}.dimension_values[{key!r}]"
        if not isinstance(key, str) or not DIMENSION_ID_PATTERN.match(key):
            raise SnapshotSchemaError(f"{label} 的键必须是 lower_snake_case 的维度名")
        if not isinstance(item, Mapping):
            raise SnapshotSchemaError(f"{label} 必须是 JSON 对象")
        section_id = _require_str(item, "section_id", label)
        if section_id not in section_ids:
            raise DanglingSectionError(f"{label}.section_id 指向不存在的 section：{section_id!r}")
        values[key] = FixtureDimensionValue(
            dimension_id=key,
            value=_require_str(item, "value", label),
            section_id=section_id,
            claim=_require_str(item, "claim", label),
        )

    source_kind = _read_source_kind(raw, path.name)
    _enforce_source_kind_provenance(raw, path.name, source_kind)

    # 文件级 digest（C7）：**测量一次**，测量值随快照戳印（attempt 3 的 D1）。
    # 结构错误优先暴露：digest 比对放在全部结构校验之后，digest 不掩盖它。
    measured_sha256 = file_sha256(path)
    snapshot = FixtureSnapshot(
        candidate_id=candidate_id,
        display_name=_require_str(raw, "display_name", path.name),
        snapshot_version=snapshot_version,
        captured_at=captured_at,
        source_path=_require_relative_local_path(
            _require_str(raw, "source_path", path.name), f"{path.name}.source_path"
        ),
        source_kind=source_kind,
        sections=tuple(sections),
        dimension_values=freeze_payload(values),
        snapshot_set=snapshot_set,
        fixture_relpath=fixture_relpath,
        fixture_sha256=measured_sha256,
    )
    if measured_sha256 != entry.fixture_sha256:
        raise SnapshotDigestError(
            f"{entry.fixture_path} 的 sha256 与 INDEX.json 登记的不一致："
            f"登记 {entry.fixture_sha256}，实际 {measured_sha256}。若是有意修改，请运行 "
            f"`python tools/sign_fixtures.py --write` 重新签名"
        )
    return snapshot


def _read_source_kind(raw: Mapping[str, Any], label: str) -> str:
    """读取并校验 `source_kind` 闭集。缺字段即报错（不做默认值兜底）。

    不兜底是刻意的：给一个默认值会让"新写一份快照时忘了标来源种类"
    静默通过，而这条信息正是"官方原文 / 人工摘要"的机器可查区别。
    """
    value = raw.get("source_kind")
    if not isinstance(value, str) or not value.strip():
        raise UnknownSourceKindError(
            f"{label}.source_kind 必须是非空字符串且取值在 {sorted(VALID_SOURCE_KINDS)} 内，"
            f"实际为 {value!r}"
        )
    if value not in VALID_SOURCE_KINDS:
        raise UnknownSourceKindError(
            f"{label}.source_kind 必须是 {sorted(VALID_SOURCE_KINDS)}，实际为 {value!r}"
        )
    return value


def _enforce_source_kind_provenance(
    raw: Mapping[str, Any], label: str, source_kind: str
) -> None:
    """`source_kind` ↔ `provenance` 的加载期交叉强制（P2.2_PLAN §2.8）。

    两者互为反例，任何一侧写错都在加载期报错：

    - 官方摘录**必须**有 `provenance`（这里只判存在；全量校验由来源层的
      `read_provenance` 负责，见 `sources.py`）→ 缺失抛 `MissingProvenanceError`；
    - 人工摘要**必须没有** `provenance`（它不得冒充官方来源）→ 出现抛
      `SourceProvenanceSchemaError`。

    只读内存里的 `raw`，不读文件、不碰原件 ⇒ **不改变任何 fixture 字节**。
    """
    has_provenance = raw.get("provenance") is not None
    if source_kind == SOURCE_KIND_OFFICIAL_EXCERPT and not has_provenance:
        raise MissingProvenanceError(
            f"{label} 的 source_kind={SOURCE_KIND_OFFICIAL_EXCERPT!r}，必须有 provenance 块："
            f"官方摘录必须能追溯到来源（repo / commit / 行号 / sha256）"
        )
    if source_kind == SOURCE_KIND_AUTHORED_SUMMARY and has_provenance:
        raise SourceProvenanceSchemaError(
            f"{label} 的 source_kind={SOURCE_KIND_AUTHORED_SUMMARY!r}，不得带 provenance 块："
            f"人工摘要不得冒充官方来源（若确实是官方摘录，请把 source_kind 改成 "
            f"{SOURCE_KIND_OFFICIAL_EXCERPT!r}）"
        )


def load_snapshots(root: Path, catalog: FixtureCatalog) -> Mapping[str, FixtureSnapshot]:
    """加载目录里全部候选快照。缺失任何一份都直接报错，不做部分加载。

    每份快照都会被戳印上 `catalog.snapshot_set`（快照集归属的唯一合法来源，P2.2-3）。
    """
    loaded: dict[str, FixtureSnapshot] = {}
    for entry in catalog.candidates:
        path = root / entry.fixture_path
        loaded[entry.candidate_id] = load_snapshot(
            path,
            entry=entry,
            snapshot_set=catalog.snapshot_set,
            fixture_relpath=entry.fixture_path,
        )
    return freeze_payload(loaded)


# ---- 快照集契约（P2.2-3；复核修正：升格权威不可伪造） ----


@dataclass(frozen=True)
class SnapshotSetBinding:
    """`SnapshotSet` 的**同源绑定上下文**（H6，P2.2-3 attempt 3）。

    携带 `fixture_root`（attestation 绑定时刻的目录）与绑定时刻的测量值。
    后者**仅作明细展示，非判定依据** —— 组装期一律现读现算（H2/H3），
    历史测量结论不进入本次判定。

    ⚠️ **它只能由 `load_snapshot_set()` 产生**（见下）。能力边界与
    `AttestationBinding` 同一口径：**受控公开 API + 静态守卫（L5）下不可合法生成**，
    不是"类型上不可能"—— 蓄意构造属修改行为，由静态守卫与评审捕获。
    """

    fixture_root: str
    index_sha256: str = ""
    #: 绑定时刻登记的 (path, sha256) 清单（展示用；判定以组装期现算为准）。
    snapshot_files: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SnapshotSet:
    """一次报告组装所声明的**快照集上下文**（可选能力，P2.2-3）。

    **P2.2-3 复核修正（P1：伪造升格绕过）**：本类型**不再携带** `promotable`
    映射。旧设计把 `Mapping[candidate_id, bool]` 当升格权威，而它可被调用方
    直接构造 —— 手填 `True` 就能让无证明的真实官方集通过 `research_grade`
    （已实测复现：`integrity.passed=True`、`report_id=rp_9e0057b9f989020f`）。
    这推翻了"无 attestation 不可 research_grade"与"promotable 唯一合法来源"。

    **attempt 3（B6 修复）**：`research_grade` 的证明必须与**本次组装的数据**绑定 ——
    新增 `binding_context`（只能由 `load_snapshot_set()` 工厂产生）携带
    root ↔ attestation 的同源绑定；组装期 S0.5 按该 root **现读磁盘现算**，
    把内存快照、证据逐字节对回已核验文件（`integrity.check_i10_non_promotable_support`）。
    `binding_context is None` 的 research_grade ⇒ I10 `unbound`（fail-closed）——
    "证明存在"与"证明对本次数据有效"是两个问题，后者由工厂 + 现算保证。

    本类型因此只承载**声明**与**绑定上下文**：快照集标识、策略、
    一个必须来自 `load_attestation()` 的 attestation，以及绑定上下文。

    **归属规则（fail-closed，不变）**：参与组装的快照必须携带与 `snapshot_set_id`
    逐字相等的归属戳印；`""`（目录未声明）或其它值都视为"无法证明归属"而拒绝 ——
    绝不凭 `candidate_id` / `source_id` 相同就推断同源。
    """

    snapshot_set_id: str
    policy: str
    attestation: Any = None
    #: H6 同源绑定上下文。`None` 合法（`regression_replay` / 未绑定证明），
    #: 非 None 必须是 `SnapshotSetBinding`。research_grade 下为 None ⇒ I10 unbound。
    binding_context: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_set_id, str) or not SNAPSHOT_SET_PATTERN.match(
            self.snapshot_set_id
        ):
            raise SnapshotSchemaError(
                f"SnapshotSet.snapshot_set_id 必须匹配 SNAPSHOT_SET_PATTERN，"
                f"实际为 {self.snapshot_set_id!r}"
            )
        if self.policy not in VALID_SNAPSHOT_SET_POLICIES:
            raise SnapshotSchemaError(
                f"SnapshotSet.policy 必须是 {sorted(VALID_SNAPSHOT_SET_POLICIES)}，"
                f"实际为 {self.policy!r}"
            )
        if self.attestation is not None:
            # 延迟导入：attestation.py 依赖本模块，模块级导入会成环
            from .attestation import SourceVerificationAttestation

            if not isinstance(self.attestation, SourceVerificationAttestation):
                raise SnapshotSchemaError(
                    f"SnapshotSet.attestation 必须是 SourceVerificationAttestation"
                    f"（且只能来自 load_attestation()），"
                    f"实际为 {type(self.attestation).__name__}"
                )
        if self.binding_context is not None and not isinstance(
            self.binding_context, SnapshotSetBinding
        ):
            raise SnapshotSchemaError(
                f"SnapshotSet.binding_context 必须是 SnapshotSetBinding"
                f"（且只能由 load_snapshot_set() 产生），"
                f"实际为 {type(self.binding_context).__name__}"
            )


def load_snapshot_set(
    *,
    fixture_root: Path,
    attestation_path: Path,
    policy: str,
) -> SnapshotSet:
    """**唯一**能产出带绑定上下文的 `SnapshotSet` 的工厂（H6，attempt 3）。

    内部调用 `load_attestation()` 做文件级绑定；绑定不通过 ⇒ 直接抛错，
    **不产出半成品**（fail-closed）。`fixture_root` 与 attestation 在这里
    **同源一次性建立**：组装期的全部现读现算只允许使用该 root ——
    否则就会出现"attestation 绑定目录 A、现算却对目录 B 执行"的参数级伪造面
    （P2.2-3_REDESIGN §3 取舍表）。
    """
    from .attestation import load_attestation  # 延迟导入防环

    base = Path(fixture_root)
    attestation = load_attestation(Path(attestation_path), fixture_root=base)
    if attestation.binding is None or attestation.binding.reason != "":
        reason = "unbound" if attestation.binding is None else attestation.binding.reason
        detail = "" if attestation.binding is None else attestation.binding.detail
        raise SnapshotSchemaError(
            f"attestation 与 fixture_root 的文件级绑定未通过（{reason}）：{detail} —— "
            f"不产出 SnapshotSet（fail-closed）"
        )
    return SnapshotSet(
        snapshot_set_id=attestation.snapshot_set_id,
        policy=policy,
        attestation=attestation,
        binding_context=SnapshotSetBinding(
            fixture_root=str(base),
            index_sha256=attestation.index_sha256,
            snapshot_files=tuple(
                (item.path, item.sha256) for item in attestation.snapshot_files
            ),
        ),
    )


def merge_snapshots(
    *mappings: Mapping[str, FixtureSnapshot],
) -> Mapping[str, FixtureSnapshot]:
    """合并多份快照映射；**同名候选必须可证明同源**，否则拒绝（P2.2-3 混集防线）。

    混集风险的根源：`assemble_report` 的 `snapshots` 以 `candidate_id` 为键，
    两个目录的同名候选在 dict 合并时会**静默互相覆盖** —— 组装层事后无法察觉。
    所以拒绝必须发生在合并这一步：

    - 同名候选若**逐字段相等**（含 `snapshot_set` 戳印）⇒ 可证明同一份内容，合并放行；
    - 否则 ⇒ `CandidateCoverageError`，信息指出冲突候选及两边的
      `snapshot_set` / `snapshot_version`，绝不静默择一。
    """
    merged: dict[str, FixtureSnapshot] = {}
    for mapping in mappings:
        for candidate_id, snapshot in mapping.items():
            existing = merged.get(candidate_id)
            if existing is not None and existing != snapshot:
                raise CandidateCoverageError(
                    f"候选 {candidate_id!r} 在多个快照来源中出现且无法证明同源："
                    f"({existing.snapshot_set!r}, {existing.snapshot_version!r}) vs "
                    f"({snapshot.snapshot_set!r}, {snapshot.snapshot_version!r})。"
                    f"同名候选跨快照集合并会被静默覆盖，已被拒绝。"
                )
            merged[candidate_id] = snapshot
    return freeze_payload(merged)


# ---- 升格闸门（P2.2-1） ----
#
# 「什么内容有资格成为**研究级**证据」由 `is_promotable_source()`（sources.py）判定，
# 它消费的是这里的两个类型。分工必须清楚：
#
#   - `PromotionChecks`：准入闸门 C1–C5 / C7 / C8 的**逐项布尔结果**，由**加载期填充**
#     （`sources.promotion_checks_for()` 从实际数据重新推导每一项，而不是照抄"加载成功了"）；
#   - `PromotableExcerpt`：**唯一**能被唯一公开工厂（`sources.evidence_from_excerpt()`）
#     变成 `EvidenceRef` 的入参类型。它只能由 `promote_excerpt()` 构造，而后者要求
#     ①快照是官方摘录 ②全部闸门为真 —— 所以"拿到这个类型"本身**蕴含**"已通过来源校验"。
#
# 能力边界的准确说法：**受控公开 API + 静态守卫下不可合法生成**，
# 而**不是**"类型上不可表达"——Python 没有构造可及性控制，
# 直接 `EvidenceRef(...)` / `object.__new__` / import 私有模块物理上都做得到，
# 那些绕过需要**蓄意修改**，会被 L3/L4 静态守卫与代码评审捕获（见 P2.2_PLAN §2.5）。


@dataclass(frozen=True)
class PromotionChecks:
    """准入闸门 C1–C5 / C7 / C8 的逐项布尔结果。

    刻意逐项展开而不是存一个布尔：逐项之后，"是哪一条不满足"可以在错误信息与
    审计指标里呈现出来（`failed()`），而不是只得到一句"不可升格"。
    """

    c1: bool  # 来源 repo 在 ALLOWED_SOURCE_REPOS[candidate_id] 白名单内
    c2: bool  # url / raw_url commit-pinned 且与 repo+commit_sha+path 逐字一致
    c3: bool  # commit_sha / blob_sha / raw_sha256 / raw_lines 齐备且形态合法
    c4: bool  # provenance 块存在且通过全量校验
    c5: bool  # 每条 section 的 line_range ≤ 该文档 raw_lines
    c7: bool  # 文件级 digest 与 INDEX.json 登记一致
    c8: bool  # 每条声明维度都能解析回真实来源（证据不悬空）

    def all_passed(self) -> bool:
        return all((self.c1, self.c2, self.c3, self.c4, self.c5, self.c7, self.c8))

    def failed(self) -> tuple[str, ...]:
        """返回未通过的检查项名（排序），供错误信息 / 审计指标使用。"""
        names = {
            "c1": self.c1,
            "c2": self.c2,
            "c3": self.c3,
            "c4": self.c4,
            "c5": self.c5,
            "c7": self.c7,
            "c8": self.c8,
        }
        return tuple(sorted(name for name, ok in names.items() if not ok))

    @classmethod
    def all_true(cls) -> PromotionChecks:
        """全部通过。加载成功即应得到它（`promotion_checks_for` 会真的逐项重算）。"""
        return cls(c1=True, c2=True, c3=True, c4=True, c5=True, c7=True, c8=True)


@dataclass(frozen=True)
class PromotableExcerpt:
    """一条**已通过来源校验**的摘录，是构造研究级证据的唯一合法入参类型。

    只能由 `promote_excerpt()` 产出；不要手工构造 —— 手工构造会绕开
    "快照必须是官方摘录 + 闸门必须全通过"这两条前置条件，而这两条正是它的全部意义。
    """

    candidate_id: str
    snapshot_version: str
    captured_at: str
    locator: str
    excerpt: str
    source_kind: str


def snapshot_excerpt(snapshot: FixtureSnapshot, section_id: str) -> tuple[str, str]:
    """`(locator, excerpt)`。截断走 `expected_excerpt_for`（全仓唯一实现点，P2.2-2 收敛）。"""
    index = snapshot.section_index(section_id)
    return section_locator(index), expected_excerpt_for(snapshot.sections[index].text)


def promote_excerpt(
    snapshot: FixtureSnapshot,
    *,
    section_id: str,
    checks: PromotionChecks,
) -> PromotableExcerpt:
    """把快照里的一段原文提升为 `PromotableExcerpt`（唯一构造入口）。

    两条前置条件，任一不成立即抛 `PromotionError`（**不返回 None、不静默降级**）：

    1. 快照的 `source_kind` 必须是 `official_documentation_excerpt`
       —— 人工摘要无论闸门结果如何都不可升格；
    2. `checks.all_passed()` 必须为真。

    入参类型是 `FixtureSnapshot`（而不是任意 Mapping / provider 采集记录），
    因此"把采集到的元数据直接拼成证据"没有合法的调用方式。
    """
    if not isinstance(snapshot, FixtureSnapshot):
        raise PromotionError(
            f"promote_excerpt 需要 FixtureSnapshot，实际为 {type(snapshot).__name__}："
            f"采集层记录 / 任意字典都不是合法入参"
        )
    if not isinstance(checks, PromotionChecks):
        raise PromotionError(
            f"promote_excerpt 需要 PromotionChecks，实际为 {type(checks).__name__}"
        )
    if snapshot.source_kind != SOURCE_KIND_OFFICIAL_EXCERPT:
        raise PromotionError(
            f"候选 {snapshot.candidate_id!r} 的 source_kind={snapshot.source_kind!r} 不可升格："
            f"只有 {SOURCE_KIND_OFFICIAL_EXCERPT!r} 能支撑研究级推荐"
        )
    failed = checks.failed()
    if failed:
        raise PromotionError(
            f"候选 {snapshot.candidate_id!r} 未通过准入闸门 {list(failed)}，不可升格"
        )
    locator, excerpt = snapshot_excerpt(snapshot, section_id)
    return PromotableExcerpt(
        candidate_id=snapshot.candidate_id,
        snapshot_version=snapshot.snapshot_version,
        captured_at=snapshot.captured_at,
        locator=locator,
        excerpt=excerpt,
        source_kind=snapshot.source_kind,
    )

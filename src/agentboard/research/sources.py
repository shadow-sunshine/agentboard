"""官方资料快照的来源层（真实资料接入第一步）。

两个快照目录的分工（**互不覆盖**）：

    fixtures/research/    P1A 人工撰写的技术摘要（source_kind = authored_summary）
    fixtures/official/    官方文档 / 官方仓库文件的逐行摘录（source_kind = official_documentation_excerpt）

本模块只负责 `fixtures/official/` 的**来源元数据**：每条摘录来自哪个官方文件、在该文件的
哪几行、文件在采集时刻的 ref / commit / blob / sha256、文档自身是否声明版本，以及
**哪些维度无法从资料确认**（显式登记为 `unknown`，不推断补齐）。

为什么官方快照沿用 `research.fixture.v1` 而不是另立 schema
--------------------------------------------------------
「可被领域层消费」的部分（`sections` + `dimension_values`）与 P1A 快照结构完全一致，
差异只在多出来的 `provenance` 块。共用同一 schema 带来三个可验证的好处：

1. 现有 `load_catalog` / `load_snapshots` / `FixtureResearchWorker` / 报告组装**零改动**即可消费；
2. 现有签名工具 `python tools/sign_fixtures.py --root fixtures/official --check` 直接可用；
3. 来源元数据只被本模块读取，领域层看不到它 —— 也就不可能悄悄影响结论。

不联网、不读时钟
----------------
本模块只读本地 JSON，`collected_at` 来自文件里的固定元数据。来源**可复核性**靠数据本身
承载（url + repo + commit_sha + blob_sha + raw_sha256 + 行号）：URL 固定到 commit，
所以重取必然得到同一份字节；审计方式是重新下载 `raw_url`、比对 `raw_sha256`，
再按 `line_range` 切片与 `sections[].text` 逐字节比较 ——
`tools/verify_sources.py` 把这套核对做成了离线可跑的一条命令。

因此本模块不 import 任何网络库，URL 校验用正则完成
（`agentboard` 全仓禁用 `urllib` 等模块，见静态守卫）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import WorkerTaskInput
from ..evidence import SOURCE_FIXTURE, EvidenceRef
from ..util import freeze_payload
from .attestation import (
    SourceVerificationAttestation,
    promotion_blocking_reason,
)
from .config import (
    ATTESTATION_SCHEMA,
    LINE_RANGE_PATTERN,
    SOURCE_KIND_OFFICIAL_EXCERPT,
    VALID_SOURCE_KINDS,
)
from .dimensions import DimensionRegistry, load_dimension_registry
from .errors import (
    AttestationSchemaError,
    IncompleteDimensionCoverageError,
    MissingProvenanceError,
    NonOfficialSourceError,
    PromotionError,
    SourceProvenanceSchemaError,
    SourceReferenceError,
    UnknownSourceKindError,
)
from .fixtures import FixtureResearchWorker
from .models import ResearchFinding
from .snapshot import (
    SECTION_LOCATOR_PATTERN,
    SHA256_PATTERN,
    FixtureCatalog,
    FixtureSnapshot,
    PromotableExcerpt,
    PromotionChecks,
    SnapshotSet,
    expected_excerpt_for,
    file_sha256,
    load_catalog,
    load_snapshots,
    read_json_object,
    snapshot_excerpt,
)

SCHEMA = "research.sources.v1"

#: 来源种类：官方文档站的源文件 / 官方代码仓库里的文件（如 README）
KIND_OFFICIAL_DOCS = "official_docs"
KIND_OFFICIAL_REPO_FILE = "official_repo_file"
VALID_KINDS: frozenset[str] = frozenset({KIND_OFFICIAL_DOCS, KIND_OFFICIAL_REPO_FILE})

#: 维度取值是怎么来的。
#: - `stated`：原文直接陈述该维度的事实，取值就是原文的陈述；
#: - `mapped`：原文给出可核对的事实，取值是把事实按维度自带量表做的一次映射，
#:   此时 `derivation` 必须写清「哪句事实 → 哪条量表定义 → 哪个标签」。
BASIS_STATED = "stated"
BASIS_MAPPED = "mapped"
VALID_BASES: frozenset[str] = frozenset({BASIS_STATED, BASIS_MAPPED})

REF_KIND_BRANCH = "branch"
REF_KIND_TAG = "tag"
VALID_REF_KINDS: frozenset[str] = frozenset({REF_KIND_BRANCH, REF_KIND_TAG})

#: 文档自身不声明版本时的固定取值（**不是**缺省，是明确的"无法确认"）
UNKNOWN_VERSION = "unknown"

#: 允许的 URL 主机。只有官方文档站 / 官方代码仓库。
ALLOWED_SOURCE_HOSTS: frozenset[str] = frozenset({"github.com", "raw.githubusercontent.com"})

#: 每个候选允许的官方仓库（owner/name）。**"只用官方来源"这条要求在这里变成可执行校验**：
#: 出现别的仓库、别的组织，加载即报错，而不是靠人工 review。
ALLOWED_SOURCE_REPOS: Mapping[str, frozenset[str]] = freeze_payload(
    {
        "pgvector": frozenset({"pgvector/pgvector"}),
        "milvus_standalone": frozenset({"milvus-io/milvus-docs", "milvus-io/milvus"}),
        "qdrant_local": frozenset({"qdrant/landing_page", "qdrant/qdrant"}),
    }
)

SOURCE_ID_PATTERN = re.compile(r"^doc_[a-z0-9_]{2,40}$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
#: 具体版本标签形如 v3.0.2 / 0.8.6 / v1.19.1-alpha；也允许固定值 unknown。
RELEASE_TAG_PATTERN = re.compile(r"^[vV]?\d[\w.\-]*$")
URL_PATTERN = re.compile(r"^https://([A-Za-z0-9.\-]+)(/[^\s?#]*)$")

#: 证据的 source_id 前缀由核心层与 source_type 绑定（fixture:）。
_FIXTURE_SOURCE_ID_PREFIX = "fixture:"


def github_blob_url(repo: str, commit_sha: str, path: str) -> str:
    """官方文件在 GitHub 上的页面地址。**固定到 commit，不用分支名。**

    用分支名（`master` / `v3.0.x`）当定位符，复核时可能拿到与采集当天不同的内容，
    于是"行号 + sha256"这套锚点全部失效。所以定位符一律是 commit。
    """
    return f"https://github.com/{repo}/blob/{commit_sha}/{path}"


def github_raw_url(repo: str, commit_sha: str, path: str) -> str:
    """官方文件的原始内容地址。**固定到 commit** ⟹ 重取必然得到同一份字节。"""
    return f"https://raw.githubusercontent.com/{repo}/{commit_sha}/{path}"


# ---- 校验小工具（本地实现：报错类型是来源层自己的，便于定位到具体字段） ----


def _require_str(raw: Mapping[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourceProvenanceSchemaError(f"{label}.{key} 必须是非空字符串，实际为 {value!r}")
    return value


def _require_pattern(value: Any, pattern: re.Pattern[str], label: str, hint: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceProvenanceSchemaError(f"{label} 必须是非空字符串，实际为 {value!r}")
    if not pattern.match(value):
        raise SourceProvenanceSchemaError(f"{label} 必须满足 {hint}，实际为 {value!r}")
    return value


def _require_line_count(value: Any, label: str) -> int:
    """上游文件的总行数：`bool` 不算 int，且必须 ≥ 1（空文件没有可引用的行）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceProvenanceSchemaError(f"{label} 必须是整数，实际为 {value!r}")
    if value < 1:
        raise SourceProvenanceSchemaError(f"{label} 必须 ≥ 1，实际为 {value}")
    return value


def _parse_line_span(line_range: str) -> tuple[int, int]:
    if "-" in line_range:
        start_text, end_text = line_range.split("-", 1)
        return int(start_text), int(end_text)
    value = int(line_range)
    return value, value


def _require_repo_relative_path(value: str, label: str) -> str:
    """官方文件的仓库内路径：相对 POSIX 路径，不含 URL / 反斜杠 / 越级 `..`。"""
    if "://" in value or value.startswith("/") or "\\" in value:
        raise NonOfficialSourceError(
            f"{label} 必须是仓库内相对路径（POSIX 分隔），实际为 {value!r}"
        )
    if ".." in value.split("/"):
        raise NonOfficialSourceError(f"{label} 不得越出仓库根目录，实际为 {value!r}")
    return value


def _split_official_url(url: str, label: str) -> tuple[str, str]:
    """拆出 (host, path)。不用 urllib：本仓禁用 urllib / http 等模块（静态守卫）。"""
    match = URL_PATTERN.match(url)
    if match is None:
        raise NonOfficialSourceError(f"{label} 必须是 https URL，实际为 {url!r}")
    host, path = match.group(1), match.group(2)
    if host not in ALLOWED_SOURCE_HOSTS:
        raise NonOfficialSourceError(
            f"{label} 的主机 {host!r} 不在官方来源白名单 {sorted(ALLOWED_SOURCE_HOSTS)} 内"
        )
    return host, path


@dataclass(frozen=True)
class SourceDocument:
    """一份被引用的官方文件在**采集时刻**的状态。

    `url` / `raw_url` 一律**固定到 `commit_sha`**（不用分支名）——复核时重取必然得到
    同一份字节。`ref` / `ref_kind` 只是"采集时跟踪的是哪个分支/标签"的记录，
    **不参与定位**：分支会移动，用它定位就再也对不上 sha256 与行号。
    """

    source_id: str
    kind: str
    title: str
    repo: str
    path: str
    url: str
    raw_url: str
    ref: str
    ref_kind: str
    commit_sha: str
    blob_sha: str
    raw_sha256: str
    #: 上游文件的总行数（末尾换行不计为额外一行）。用来把
    #: "`line_range` 引用了不存在的行"变成加载期就能拦住的错误，而不是等复核者去数行。
    raw_lines: int
    release_tag: str
    release_tag_note: str
    upstream_latest_release: str
    upstream_latest_release_note: str
    upstream_license: str
    upstream_license_note: str
    collected_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "title": self.title,
            "repo": self.repo,
            "path": self.path,
            "url": self.url,
            "raw_url": self.raw_url,
            "ref": self.ref,
            "ref_kind": self.ref_kind,
            "commit_sha": self.commit_sha,
            "blob_sha": self.blob_sha,
            "raw_sha256": self.raw_sha256,
            "raw_lines": self.raw_lines,
            "release_tag": self.release_tag,
            "release_tag_note": self.release_tag_note,
            "upstream_latest_release": self.upstream_latest_release,
            "upstream_latest_release_note": self.upstream_latest_release_note,
            "upstream_license": self.upstream_license,
            "upstream_license_note": self.upstream_license_note,
            "collected_at": self.collected_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, label: str, candidate_id: str) -> SourceDocument:
        if not isinstance(raw, Mapping):
            raise SourceProvenanceSchemaError(f"{label} 必须是 JSON 对象")

        source_id = _require_pattern(
            raw.get("source_id"), SOURCE_ID_PATTERN, f"{label}.source_id", "^doc_[a-z0-9_]{2,40}$"
        )
        kind = _require_str(raw, "kind", label)
        if kind not in VALID_KINDS:
            raise SourceProvenanceSchemaError(
                f"{label}.kind 必须是 {sorted(VALID_KINDS)}，实际为 {kind!r}"
            )

        repo = _require_str(raw, "repo", label)
        allowed = ALLOWED_SOURCE_REPOS.get(candidate_id)
        if allowed is None:
            raise NonOfficialSourceError(
                f"候选 {candidate_id!r} 没有登记允许的官方仓库；"
                f"请在 ALLOWED_SOURCE_REPOS 里显式声明，而不是放宽校验"
            )
        if repo not in allowed:
            raise NonOfficialSourceError(
                f"{label}.repo={repo!r} 不是候选 {candidate_id!r} 的官方仓库"
                f"（允许：{sorted(allowed)}）"
            )

        path = _require_repo_relative_path(_require_str(raw, "path", label), f"{label}.path")
        ref = _require_str(raw, "ref", label)
        ref_kind = _require_str(raw, "ref_kind", label)
        if ref_kind not in VALID_REF_KINDS:
            raise SourceProvenanceSchemaError(
                f"{label}.ref_kind 必须是 {sorted(VALID_REF_KINDS)}，实际为 {ref_kind!r}"
            )
        commit_sha = _require_pattern(
            raw.get("commit_sha"), COMMIT_SHA_PATTERN, f"{label}.commit_sha", "40 位小写十六进制"
        )

        # 定位符固定到 commit：不接受用分支 / 标签当 ref 的 URL。
        url = _require_str(raw, "url", label)
        raw_url = _require_str(raw, "raw_url", label)
        _split_official_url(url, f"{label}.url")
        _split_official_url(raw_url, f"{label}.raw_url")
        expected_url = github_blob_url(repo, commit_sha, path)
        if url != expected_url:
            raise NonOfficialSourceError(
                f"{label}.url 必须固定到 commit 并与登记的 repo / commit_sha / path 一致，"
                f"期望 {expected_url!r}，实际 {url!r}"
            )
        expected_raw = github_raw_url(repo, commit_sha, path)
        if raw_url != expected_raw:
            raise NonOfficialSourceError(
                f"{label}.raw_url 必须固定到 commit 并与登记的 repo / commit_sha / path 一致，"
                f"期望 {expected_raw!r}，实际 {raw_url!r}"
            )

        release_tag = _require_str(raw, "release_tag", label)
        if release_tag != UNKNOWN_VERSION and not RELEASE_TAG_PATTERN.match(release_tag):
            raise SourceProvenanceSchemaError(
                f"{label}.release_tag 必须是具体版本标签或 {UNKNOWN_VERSION!r}，"
                f"实际为 {release_tag!r}"
            )
        release_tag_note = _require_str(raw, "release_tag_note", label)
        if release_tag == UNKNOWN_VERSION and len(release_tag_note) < 8:
            raise SourceProvenanceSchemaError(
                f"{label} 把版本标为 unknown，就必须在 release_tag_note 里说明为什么无法确认"
            )

        return cls(
            source_id=source_id,
            kind=kind,
            title=_require_str(raw, "title", label),
            repo=repo,
            path=path,
            url=url,
            raw_url=raw_url,
            ref=ref,
            ref_kind=ref_kind,
            commit_sha=commit_sha,
            blob_sha=_require_pattern(
                raw.get("blob_sha"), COMMIT_SHA_PATTERN, f"{label}.blob_sha", "40 位小写十六进制"
            ),
            raw_sha256=_require_pattern(
                raw.get("raw_sha256"), SHA256_PATTERN, f"{label}.raw_sha256", "64 位小写十六进制"
            ),
            raw_lines=_require_line_count(raw.get("raw_lines"), f"{label}.raw_lines"),
            release_tag=release_tag,
            release_tag_note=release_tag_note,
            upstream_latest_release=_require_str(raw, "upstream_latest_release", label),
            upstream_latest_release_note=_require_str(raw, "upstream_latest_release_note", label),
            upstream_license=_require_str(raw, "upstream_license", label),
            upstream_license_note=_require_str(raw, "upstream_license_note", label),
            collected_at=_require_str(raw, "collected_at", label),
        )


@dataclass(frozen=True)
class SectionSource:
    """一条摘录在原文里的位置。`line_range` 是 1 起的闭区间，单行写 `"55"`。"""

    section_id: str
    source_id: str
    line_range: str

    def line_span(self) -> tuple[int, int]:
        return _parse_line_span(self.line_range)

    def to_dict(self) -> dict[str, str]:
        return {
            "section_id": self.section_id,
            "source_id": self.source_id,
            "line_range": self.line_range,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], *, label: str, section_id: str | None = None
    ) -> SectionSource:
        if not isinstance(raw, Mapping):
            raise SourceProvenanceSchemaError(f"{label} 必须是 JSON 对象")
        line_range = _require_pattern(
            raw.get("line_range"), LINE_RANGE_PATTERN, f"{label}.line_range", "^起始行[-结束行]$"
        )
        start, end = _parse_line_span(line_range)
        if start > end:
            raise SourceProvenanceSchemaError(f"{label}.line_range 起始行 {start} 大于结束行 {end}")
        return cls(
            section_id=section_id or _require_str(raw, "section_id", label),
            source_id=_require_str(raw, "source_id", label),
            line_range=line_range,
        )


@dataclass(frozen=True)
class DimensionSource:
    """一个**已被声明**的维度取值，是怎么从原文得到的。"""

    dimension_id: str
    source_id: str
    section_id: str
    basis: str
    derivation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "dimension_id": self.dimension_id,
            "source_id": self.source_id,
            "section_id": self.section_id,
            "basis": self.basis,
            "derivation": self.derivation,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, label: str, dimension_id: str) -> DimensionSource:
        if not isinstance(raw, Mapping):
            raise SourceProvenanceSchemaError(f"{label} 必须是 JSON 对象")
        basis = _require_str(raw, "basis", label)
        if basis not in VALID_BASES:
            raise SourceProvenanceSchemaError(
                f"{label}.basis 必须是 {sorted(VALID_BASES)}，实际为 {basis!r}"
            )
        derivation = _require_str(raw, "derivation", label)
        if basis == BASIS_MAPPED and len(derivation) < 12:
            raise SourceProvenanceSchemaError(
                f"{label} 的 basis=mapped，必须在 derivation 里写清事实 → 量表 → 标签的映射过程"
            )
        return cls(
            dimension_id=dimension_id,
            source_id=_require_str(raw, "source_id", label),
            section_id=_require_str(raw, "section_id", label),
            basis=basis,
            derivation=derivation,
        )


@dataclass(frozen=True)
class UnknownDimension:
    """一个**无法从资料确认**的维度：显式登记，而不是从报告里静默消失。"""

    dimension_id: str
    reason: str
    checked_source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension_id": self.dimension_id,
            "reason": self.reason,
            "checked_source_ids": list(self.checked_source_ids),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, label: str, dimension_id: str) -> UnknownDimension:
        if not isinstance(raw, Mapping):
            raise SourceProvenanceSchemaError(f"{label} 必须是 JSON 对象")
        checked = raw.get("checked_source_ids")
        if not isinstance(checked, (list, tuple)) or not checked:
            raise SourceProvenanceSchemaError(f"{label}.checked_source_ids 必须是非空列表")
        source_ids: list[str] = []
        for index, item in enumerate(checked):
            item_label = f"{label}.checked_source_ids[{index}]"
            if not isinstance(item, str) or not item.strip():
                raise SourceProvenanceSchemaError(f"{item_label} 必须是非空字符串，实际为 {item!r}")
            source_ids.append(item)
        return cls(
            dimension_id=dimension_id,
            reason=_require_str(raw, "reason", label),
            checked_source_ids=tuple(source_ids),
        )


@dataclass(frozen=True)
class OfficialProvenance:
    """一个候选的来源元数据：文档清单 + 每条摘录的位置 + 声明过的维度 + unknown 维度。"""

    schema: str
    candidate_id: str
    collected_at: str
    collection_method: str
    documents: tuple[SourceDocument, ...]
    sections: tuple[SectionSource, ...]
    dimension_values: Mapping[str, DimensionSource]
    unknown_dimensions: Mapping[str, UnknownDimension]

    def document(self, source_id: str) -> SourceDocument:
        for document in self.documents:
            if document.source_id == source_id:
                return document
        raise SourceReferenceError(
            f"候选 {self.candidate_id!r} 的来源里没有 {source_id!r}；"
            f"已知来源：{sorted(document.source_id for document in self.documents)}"
        )

    def section(self, section_id: str) -> SectionSource:
        for section in self.sections:
            if section.section_id == section_id:
                return section
        raise SourceReferenceError(
            f"候选 {self.candidate_id!r} 没有 section {section_id!r} 的来源登记"
        )

    def is_unknown(self, dimension_id: str) -> bool:
        return dimension_id in self.unknown_dimensions

    @property
    def declared_dimension_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.dimension_values))

    @property
    def unknown_dimension_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.unknown_dimensions))

    @property
    def source_urls(self) -> tuple[str, ...]:
        return tuple(sorted(document.url for document in self.documents))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "collected_at": self.collected_at,
            "collection_method": self.collection_method,
            "documents": [document.to_dict() for document in self.documents],
            "sections": [section.to_dict() for section in self.sections],
            "dimension_values": {
                key: value.to_dict() for key, value in self.dimension_values.items()
            },
            "unknown_dimensions": {
                key: value.to_dict() for key, value in self.unknown_dimensions.items()
            },
        }


def _snapshot_section_field(raw: Mapping[str, Any], index: int, key: str, label: str) -> Any:
    """从快照的 top-level `sections[index]` 里取来源字段。

    来源字段（source_id / line_range）与领域字段（heading / text / tags）同处一个 section
    对象里，而不是在 provenance 里再抄一份 —— 两处登记必然漂移。
    """
    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list) or index >= len(raw_sections):
        raise SourceProvenanceSchemaError(f"{label} 在快照里不存在")
    item = raw_sections[index]
    if not isinstance(item, Mapping) or key not in item:
        raise MissingProvenanceError(f"{label}.{key} 缺失：官方摘录必须标注来源与行号")
    return item[key]


def _read_section_sources(
    raw: Mapping[str, Any], snapshot: FixtureSnapshot, *, path_label: str
) -> tuple[SectionSource, ...]:
    sources: list[SectionSource] = []
    for index, section in enumerate(snapshot.sections):
        label = f"{path_label}.sections[{index}]"
        sources.append(
            SectionSource.from_dict(
                {
                    "source_id": _snapshot_section_field(raw, index, "source_id", label),
                    "line_range": _snapshot_section_field(raw, index, "line_range", label),
                },
                label=label,
                section_id=section.section_id,
            )
        )
    return tuple(sources)


def _read_documents(
    block: Mapping[str, Any], *, label: str, candidate_id: str
) -> tuple[SourceDocument, ...]:
    raw_documents = block.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise SourceProvenanceSchemaError(f"{label}.documents 必须是非空列表")
    documents = [
        SourceDocument.from_dict(item, label=f"{label}.documents[{index}]", candidate_id=candidate_id)
        for index, item in enumerate(raw_documents)
    ]
    ids = [document.source_id for document in documents]
    if len(set(ids)) != len(ids):
        raise SourceProvenanceSchemaError(f"{label}.documents 存在重复 source_id：{sorted(ids)}")
    return tuple(documents)


def _require_known_source(source_id: str, documents: Sequence[SourceDocument], label: str) -> None:
    if source_id not in {document.source_id for document in documents}:
        raise SourceReferenceError(f"{label} 引用了不存在的来源 {source_id!r}")


def read_provenance(
    snapshot_path: Path,
    *,
    entry_fixture_path: str,
    registry: DimensionRegistry,
    snapshot: FixtureSnapshot,
) -> OfficialProvenance:
    """读取并严格校验一个官方快照的来源块。

    校验分四类，任何一类不过都直接报错（不返回部分结果）：

    1. **结构**：schema / 必填字段 / 格式（sha、行号、URL、basis）；
    2. **官方性**：repo 必须在 `ALLOWED_SOURCE_REPOS` 内，URL 主机在白名单内，
       且 `url` / `raw_url` 必须与登记的 repo + ref + path 完全一致；
    3. **一致性**：documents / sections / dimension_values 三者交叉引用必须闭合 ——
       每个 section 都有来源与行号、每个维度声明都指向真实存在的 section 且
       与该 section 的 source_id 相同、`unknown` 维度必须给出原因与检查过的来源；
    4. **完整性**：注册表里每个维度必须**要么被声明、要么被登记为 unknown**，
       不允许静默省略（这是"不推断补齐"的可执行形态）。
    """
    label = f"{entry_fixture_path}.provenance"
    raw = read_json_object(snapshot_path)
    block = raw.get("provenance")
    if block is None:
        raise MissingProvenanceError(
            f"{entry_fixture_path} 没有 provenance 块：官方资料快照必须能追溯到来源"
        )
    if not isinstance(block, Mapping):
        raise SourceProvenanceSchemaError(f"{label} 必须是 JSON 对象")

    schema = _require_str(block, "schema", label)
    if schema != SCHEMA:
        raise SourceProvenanceSchemaError(f"{label}.schema 期望 {SCHEMA!r}，实际为 {schema!r}")

    candidate_id = _require_str(block, "candidate_id", label)
    if candidate_id != snapshot.candidate_id:
        raise SourceProvenanceSchemaError(
            f"{label}.candidate_id={candidate_id!r} 与快照的 {snapshot.candidate_id!r} 不一致"
        )

    collected_at = _require_str(block, "collected_at", label)
    if collected_at != snapshot.captured_at:
        raise SourceProvenanceSchemaError(
            f"{label}.collected_at={collected_at!r} 与快照 captured_at "
            f"{snapshot.captured_at!r} 不一致"
        )

    documents = _read_documents(block, label=label, candidate_id=candidate_id)
    sections = _read_section_sources(raw, snapshot, path_label=entry_fixture_path)
    if [item.section_id for item in sections] != [item.section_id for item in snapshot.sections]:
        raise SourceReferenceError(
            f"{label} 的 section 登记与快照 sections 不一致："
            f"{[item.section_id for item in sections]} vs "
            f"{[item.section_id for item in snapshot.sections]}"
        )
    for index, item in enumerate(sections):
        _require_known_source(item.source_id, documents, f"{entry_fixture_path}.sections[{index}]")

    # 行号必须落在来源文件里：`L1270 > raw_lines` 这类"引用了不存在的行"要在加载期就报错，
    # 而不是等复核者自己发现。
    document_by_id = {document.source_id: document for document in documents}
    for index, item in enumerate(sections):
        _, end = item.line_span()
        owner = document_by_id[item.source_id]
        if end > owner.raw_lines:
            raise SourceReferenceError(
                f"{entry_fixture_path}.sections[{index}] 的 line_range {item.line_range!r} "
                f"超出来源 {item.source_id!r} 的行数（登记 {owner.raw_lines} 行）"
            )

    section_by_id = {item.section_id: item for item in sections}
    raw_values = raw.get("dimension_values", {})
    if not isinstance(raw_values, Mapping):
        raise SourceProvenanceSchemaError(f"{entry_fixture_path}.dimension_values 必须是 JSON 对象")

    dimension_values: dict[str, DimensionSource] = {}
    for raw_name, item in raw_values.items():
        if not isinstance(raw_name, str):
            raise SourceProvenanceSchemaError(f"{label}.dimension_values 的键必须是字符串")
        canonical = registry.resolve(raw_name)
        value_label = f"{label}.dimension_values[{raw_name!r}]"
        if canonical in dimension_values:
            raise SourceProvenanceSchemaError(
                f"{label} 把维度 {canonical!r} 声明了两次（键 {raw_name!r} 与另一个别名撞车）"
            )
        declaration = DimensionSource.from_dict(item, label=value_label, dimension_id=canonical)
        _require_known_source(declaration.source_id, documents, value_label)
        owner = section_by_id.get(declaration.section_id)
        if owner is None:
            # 防御性：正常路径上 `load_snapshot` 已经保证 section_id 存在（否则抛 DanglingSectionError），
            # 这里只是不让"来源层与快照层校验规则漂移"这件事静默通过。
            raise SourceReferenceError(
                f"{value_label}.section_id={declaration.section_id!r} 没有对应的 section"
            )
        if owner.source_id != declaration.source_id:
            raise SourceReferenceError(
                f"{value_label} 的 source_id={declaration.source_id!r} 与 section "
                f"{declaration.section_id!r} 登记的 {owner.source_id!r} 不一致"
            )
        # 注意：**不**在这里比对 `snapshot.dimension_values[...].section_id`。
        # 来源字段与领域字段同处一个 dimension_values 条目里（见 `_snapshot_section_field`
        # 的注释），两者天然同源 —— 再比一次是同义反复，不是校验。
        dimension_values[canonical] = declaration

    unknown = _read_unknown_dimensions(block, label=label, registry=registry, documents=documents)

    overlap = sorted(set(dimension_values) & set(unknown))
    if overlap:
        raise SourceProvenanceSchemaError(
            f"{label} 的维度 {overlap} 同时出现在 dimension_values 与 unknown_dimensions"
        )

    registered = set(registry.ids())
    missing = sorted(registered - set(dimension_values) - set(unknown))
    if missing:
        raise IncompleteDimensionCoverageError(
            f"{label} 未覆盖注册维度 {missing}：每个维度要么给出取值声明，"
            f"要么登记到 unknown_dimensions 并写明原因，不允许静默省略"
        )
    # 注册表以外的维度不需要在这里拦：`registry.resolve` 已经在上面拒绝未知 id
    # （错误信息里会列出已知维度）。"新增维度必须先改注册表"因此是机制保证的。

    return OfficialProvenance(
        schema=schema,
        candidate_id=candidate_id,
        collected_at=collected_at,
        collection_method=_require_str(block, "collection_method", label),
        documents=documents,
        sections=sections,
        dimension_values=freeze_payload(dimension_values),
        unknown_dimensions=freeze_payload(unknown),
    )


def _read_unknown_dimensions(
    block: Mapping[str, Any],
    *,
    label: str,
    registry: DimensionRegistry,
    documents: Sequence[SourceDocument],
) -> dict[str, UnknownDimension]:
    raw_unknown = block.get("unknown_dimensions", {})
    if not isinstance(raw_unknown, Mapping):
        raise SourceProvenanceSchemaError(f"{label}.unknown_dimensions 必须是 JSON 对象")
    unknown: dict[str, UnknownDimension] = {}
    for raw_name, item in raw_unknown.items():
        if not isinstance(raw_name, str):
            raise SourceProvenanceSchemaError(f"{label}.unknown_dimensions 的键必须是字符串")
        canonical = registry.resolve(raw_name)
        if canonical != raw_name:
            raise SourceProvenanceSchemaError(
                f"{label}.unknown_dimensions 的键必须写 canonical dimension_id"
                f"（{raw_name!r} 是 alias，应写 {canonical!r}）"
            )
        entry_label = f"{label}.unknown_dimensions[{raw_name!r}]"
        entry = UnknownDimension.from_dict(item, label=entry_label, dimension_id=canonical)
        for source_id in entry.checked_source_ids:
            _require_known_source(source_id, documents, entry_label)
        unknown[canonical] = entry
    return unknown


@dataclass(frozen=True)
class OfficialSources:
    """官方资料快照集：目录 + 注册表 + 快照 + 来源元数据。"""

    catalog: FixtureCatalog
    registry: DimensionRegistry
    snapshots: Mapping[str, FixtureSnapshot]
    provenance: Mapping[str, OfficialProvenance]
    #: 所属目录（`promotion_checks_for` 重算 C1–C8 时要对文件做 digest，需要知道根）。
    #: 由 `load_official_sources` 填充；直接构造时为 `None`，此时 `snapshot_set()`
    # 会显式要求调用方提供 `fixture_root`（fail-closed，不猜）。
    fixture_root: Path | None = None

    @property
    def snapshot_set_id(self) -> str:
        """本快照集的标识（来自 `INDEX.json` 的 `snapshot_set` 键；未声明时为空串）。"""
        return self.catalog.snapshot_set

    def snapshot_set(
        self,
        *,
        policy: str,
        attestation: SourceVerificationAttestation | None = None,
    ) -> SnapshotSet:
        """构建 `SnapshotSet`（P2.2-3）。

        **P2.2-3 复核修正（P1：伪造升格绕过）**：本方法**不再**预计算
        `promotable` 映射 —— 那种映射可被调用方直接构造伪造（手填 `True`
        即可绕过 attestation，已实测复现）。升格权威改为：
        组装期由 I10 按**实际快照的 `source_kind`** + **loader-only 绑定的
        attestation** 重算（见 `integrity.check_i10_non_promotable_support`）。

        本方法因此只是把"本目录声明的快照集标识 + 调用方显式提供的
        （必须来自 `load_attestation()` 的）证明"打包成类型化上下文。
        `attestation=None` ⇒ I10 将按 `absent` 失败
        （"没核对过原件就不算研究级"）。
        """
        return SnapshotSet(
            snapshot_set_id=self.catalog.snapshot_set,
            policy=policy,
            attestation=attestation,
        )

    def candidate_ids(self) -> tuple[str, ...]:
        return self.catalog.ids()

    def snapshot(self, candidate_id: str) -> FixtureSnapshot:
        self.catalog.get(candidate_id)
        return self.snapshots[candidate_id]

    def provenance_for(self, candidate_id: str) -> OfficialProvenance:
        self.catalog.get(candidate_id)
        return self.provenance[candidate_id]

    def documents(self) -> tuple[SourceDocument, ...]:
        return tuple(
            sorted(
                (document for item in self.provenance.values() for document in item.documents),
                key=lambda document: document.source_id,
            )
        )

    def worker(self) -> FixtureResearchWorker:
        """复用 P1A 的 worker：官方快照与 P1A 快照共用同一个 loader，因此无需适配层。"""
        return FixtureResearchWorker(self.catalog, self.registry, self.snapshots)

    def findings(self, candidate_id: str) -> tuple[ResearchFinding, ...]:
        """同步入口：把官方快照转成现有 `ResearchFinding`（证据为 `EvidenceRef`）。"""
        return asyncio.run(self.worker().investigate(make_task_input(candidate_id)))

    def resolve_evidence(self, ref: EvidenceRef) -> SourceDocument:
        """把一条证据回溯到它的官方来源。这是「来源与证据可解析」的核心检查。"""
        if not isinstance(ref, EvidenceRef):
            raise SourceReferenceError(
                f"resolve_evidence 需要 EvidenceRef，实际为 {type(ref).__name__}"
            )
        if ref.source_type != SOURCE_FIXTURE:
            raise SourceReferenceError(
                f"只有 {SOURCE_FIXTURE} 证据能回溯到官方来源，实际为 {ref.source_type!r}"
            )
        if not ref.source_id.startswith(_FIXTURE_SOURCE_ID_PREFIX):
            raise SourceReferenceError(
                f"证据 source_id 必须以 {_FIXTURE_SOURCE_ID_PREFIX!r} 开头，实际 {ref.source_id!r}"
            )
        candidate_id = ref.source_id[len(_FIXTURE_SOURCE_ID_PREFIX) :]
        if candidate_id not in self.provenance:
            raise SourceReferenceError(
                f"证据 source_id={ref.source_id!r} 不属于本官方快照集"
                f"（已知候选：{list(self.candidate_ids())}）"
            )

        snapshot = self.snapshots[candidate_id]
        if ref.snapshot_version != snapshot.snapshot_version:
            raise SourceReferenceError(
                f"证据 snapshot_version={ref.snapshot_version!r} 与快照 "
                f"{snapshot.snapshot_version!r} 不一致"
            )
        match = SECTION_LOCATOR_PATTERN.match(ref.locator)
        if match is None:
            raise SourceReferenceError(f"不支持的 locator：{ref.locator!r}")
        index = int(match.group(1))
        if index >= len(snapshot.sections):
            raise SourceReferenceError(
                f"候选 {candidate_id!r} 只有 {len(snapshot.sections)} 个 section，"
                f"无法解析 {ref.locator!r}"
            )
        section = snapshot.sections[index]
        # 与组装期 `_verify_fixture_locators` 共用同一条规范截断（P2.2-2 收敛，修 D2）——
        # 旧实现这里是一份内联副本，规则"恰好"与别处相等，不是结构上相等。
        if expected_excerpt_for(section.text) != ref.excerpt:
            raise SourceReferenceError(
                f"证据摘录与快照 section {section.section_id!r} 的原文不一致"
                f"（应为规范截断 {len(expected_excerpt_for(section.text))} 字符，"
                f"证据实际 {len(ref.excerpt)} 字符；证据 digest {ref.digest}）"
            )
        provenance = self.provenance[candidate_id]
        return provenance.document(provenance.section(section.section_id).source_id)


def load_official_sources(root: Path) -> OfficialSources:
    """装载 `fixtures/official/`：目录 + 注册表 + 快照 + 来源元数据（全部本地）。"""
    base = Path(root)
    catalog = load_catalog(base)
    registry = load_dimension_registry(base / catalog.dimension_registry_path)
    snapshots = load_snapshots(base, catalog)

    provenance: dict[str, OfficialProvenance] = {}
    for entry in catalog.candidates:
        provenance[entry.candidate_id] = read_provenance(
            base / entry.fixture_path,
            entry_fixture_path=entry.fixture_path,
            registry=registry,
            snapshot=snapshots[entry.candidate_id],
        )
    return OfficialSources(
        catalog=catalog,
        registry=registry,
        snapshots=snapshots,
        provenance=freeze_payload(provenance),
        fixture_root=base,
    )


# ---- 升格闸门：从"可信"到"可作研究级支撑"（P2.2-1） ----
#
# 为什么需要**两个**信任谓词，而不是收紧既有那一个
# -----------------------------------------------
# 既有 `evidence.is_trusted_evidence(ref)` 只判 `ref.source_type in TRUSTED_SOURCE_TYPES`。
# 两个快照目录都产出 `fixture_snapshot`，因此在这个口径下**同权**（D1）。
# 但**不能**直接收紧 `TRUSTED_SOURCE_TYPES` 把人工摘要排除出去，因为：
#
#   - `assemble.build_recommendations` 用 `is_trusted_evidence` 过滤 `supporting`；
#   - `models.derive_coverage` 用它算 `trusted_finding_ids`；
#
# 两者都在 `SelectionReport.content_projection()` 里 ⇒ 进 `report_id` ⇒
# 改它就会改变 P1A 的 `report_id`，违反硬约束。
#
# 所以做法是**并列新增**一个更严的谓词，既有谓词语义**一字不动**：
#
# | 谓词 | 语义 | 进 report_id |
# |---|---|---|
# | `is_trusted_evidence` | 冻结不变 | 是（故禁改） |
# | `is_promotable_source` | 官方原文 + provenance 齐备 + C1–C5/C7/C8 全通过 + **有效 attestation** | 否 |
#
# 人工摘要因此被**明确降级**：能过既有（宽松）检查以复现 P1A 基线，
# 但**不是** promotable，在任何研究级报告里都不能支撑推荐。


def declared_document_digests(provenance: OfficialProvenance) -> Mapping[str, tuple[str, str]]:
    """把**一个候选**的 provenance 压成 `source_id -> (raw_sha256, blob_sha)`。"""
    return freeze_payload(
        {
            document.source_id: (document.raw_sha256, document.blob_sha)
            for document in provenance.documents
        }
    )


def snapshot_set_document_digests(sources: OfficialSources) -> Mapping[str, tuple[str, str]]:
    """把**整个快照集**里所有候选声明的文档摘要合并成一份（带冲突检测）。

    为什么是并集而不是单候选：attestation 是**快照集级**的工件
    （`snapshot_set_id` 就是集合级标识），它的 `documents[]` 覆盖集合内全部文档。
    因此校验时的比对基准必须是"集合声明的全集"，否则集合级的
    "有没有漏证明某份文档"就无从判断。

    冲突即报错：同一个 `source_id` 在不同候选里登记了不同摘要，
    说明 provenance 之间已经自相矛盾，此时**不猜**该采信哪一份。
    """
    merged: dict[str, tuple[str, str]] = {}
    for candidate_id in sources.candidate_ids():
        for source_id, digests in declared_document_digests(
            sources.provenance_for(candidate_id)
        ).items():
            existing = merged.get(source_id)
            if existing is not None and existing != digests:
                raise SourceProvenanceSchemaError(
                    f"source_id {source_id!r} 在不同候选的 provenance 里登记了不同摘要："
                    f"{existing} vs {digests}"
                )
            merged[source_id] = digests
    return freeze_payload(merged)


def promotion_checks_for(
    sources: OfficialSources, candidate_id: str, *, fixture_root: Path
) -> PromotionChecks:
    """逐项**重算** C1–C5 / C7 / C8，而不是"加载成功了就全填 True"。

    重算的意义：它把"加载期真的做过这些校验"变成一条**可复核的事实**。
    将来若有人放宽了加载期校验，这里对应的项会变成 False、升格闸门随之关闭，
    而不是静默放行。每一项都从实际数据推导：

    | 项 | 重算方式 |
    |---|---|
    | C1 | 每份文档的 repo 都在 `ALLOWED_SOURCE_REPOS[candidate_id]` 内 |
    | C2 | 每份文档的 url / raw_url 都等于 `repo+commit_sha+path` 的派生值 |
    | C3 | 每份文档的 commit_sha / blob_sha / raw_sha256 / raw_lines 齐备且形态合法 |
    | C4 | provenance 齐备（文档非空、collected_at 与快照一致、注册维度全覆盖） |
    | C5 | 每条 section 的 line_range 都在其归属文档的行数内 |
    | C7 | 快照文件实际字节摘要 == INDEX.json 登记值 |
    | C8 | 每条**声明维度**的证据都能经 `resolve_evidence` 回溯到真实文档 |

    这是**唯一**会为本目录做 I/O 的升格相关入口；`is_promotable_source()` 保持纯函数。
    """
    if not isinstance(sources, OfficialSources):
        raise PromotionError(f"promotion_checks_for 需要 OfficialSources，实际为 {type(sources).__name__}")
    provenance = sources.provenance_for(candidate_id)
    snapshot = sources.snapshot(candidate_id)

    allowed = ALLOWED_SOURCE_REPOS.get(candidate_id)
    c1 = allowed is not None and all(
        document.repo in allowed for document in provenance.documents
    )
    c2 = all(
        document.url == github_blob_url(document.repo, document.commit_sha, document.path)
        and document.raw_url
        == github_raw_url(document.repo, document.commit_sha, document.path)
        for document in provenance.documents
    )
    c3 = bool(provenance.documents) and all(
        bool(document.commit_sha)
        and bool(document.blob_sha)
        and bool(document.raw_sha256)
        and document.raw_lines >= 1
        for document in provenance.documents
    )
    registered = set(sources.registry.ids())
    covered = set(provenance.declared_dimension_ids) | set(provenance.unknown_dimension_ids)
    c4 = (
        bool(provenance.documents)
        and bool(provenance.collection_method)
        and provenance.collected_at == snapshot.captured_at
        and covered == registered
    )

    document_by_id = {document.source_id: document for document in provenance.documents}
    c5 = all(
        section.line_span()[1] <= document_by_id[section.source_id].raw_lines
        for section in provenance.sections
        if section.source_id in document_by_id
    ) and all(section.source_id in document_by_id for section in provenance.sections)

    entry = sources.catalog.get(candidate_id)
    c7 = file_sha256(Path(fixture_root) / entry.fixture_path) == entry.fixture_sha256

    c8 = True
    for dimension_id in provenance.declared_dimension_ids:
        declaration = provenance.dimension_values[dimension_id]
        locator, excerpt = snapshot_excerpt(snapshot, declaration.section_id)
        ref = EvidenceRef(
            source_type=SOURCE_FIXTURE,
            source_id=f"fixture:{candidate_id}",
            snapshot_version=snapshot.snapshot_version,
            locator=locator,
            excerpt=excerpt,
            captured_at=snapshot.captured_at,
        )
        try:
            sources.resolve_evidence(ref)
        except SourceReferenceError:
            c8 = False
            break

    return PromotionChecks(c1=c1, c2=c2, c3=c3, c4=c4, c5=c5, c7=c7, c8=c8)


def is_promotable_source(
    *,
    source_kind: str,
    has_provenance: bool,
    checks: PromotionChecks,
    attestation: SourceVerificationAttestation | None,
    expected_snapshot_set_id: str,
    declared_documents: Mapping[str, tuple[str, str]],
) -> bool:
    """该来源是否有资格支撑**研究级**推荐。

    ⚠️ **明确禁止的实现走法**：不得只用"看 `overall_result` + 比 `raw_sha256`/`blob_sha`"
    就判定有效。那两个值是**写在 fixture 里的声明值**，二者一致只能证明
    "两句话一致"，证明不了"被证明的快照就是磁盘上这份"。
    文件级锚点（`index_sha256` + `snapshot_files[].sha256`）必须校验 ——
    由 `attestation.load_attestation()` 在读取时**立即**完成，
    并把结论挂到 `attestation.binding` 上带到这里。

    **P1-1 复核修正**：该结论**不是**一个可以经构造器传入的字段
    （`binding` 声明为 `field(init=False)`），因此"手拼一份字段齐全的证明"
    在签名层面就做不到；`binding is None` 一律判 `REASON_UNBOUND`（fail-closed）。
    `from_dict()` 单独产出的对象永远 `binding is None`，**不可升格**。

    **纯函数**：零副作用、零 I/O、不读时钟。所有 I/O（读原件、读证明、算文件摘要）
    都发生在 `promotion_checks_for()` 与 `load_attestation()` 里。

    返回真的充要条件（全部成立）：

    1. `source_kind == "official_documentation_excerpt"`；
    2. `has_provenance is True`；
    3. `checks` 七项全通过；
    4. `attestation` 存在且**有效**（见 `promotion_blocking_reason`）
       —— 含 `overall_result == "passed"`、`verifier_version` 未过期、
       `snapshot_set_id` 一致、**文件级自绑定通过**、逐文档声明对齐。

    **没有 attestation ⇒ 返回 False，但加载期不报错**：这样"离线回放"与
    "研究级推荐"被彻底分开 —— 回放照常，只是不可升格。

    输入校验（不做静默兜底）：
    - `source_kind` 不在闭集内 → 抛 `UnknownSourceKindError`（**不是**返回 False）：
      未知来源类型是**配置错误**，不是"不可升格"；
    - `checks` / `attestation` / `declared_documents` 类型不对 → `TypeError` /
      `AttestationSchemaError`（不静默当作无效）。
    """
    if source_kind not in VALID_SOURCE_KINDS:
        raise UnknownSourceKindError(
            f"未知的 source_kind {source_kind!r}：合法值只有 {sorted(VALID_SOURCE_KINDS)}。"
            f"这是配置错误，不是'不可升格'"
        )
    if not isinstance(checks, PromotionChecks):
        raise TypeError(f"checks 必须是 PromotionChecks，实际为 {type(checks).__name__}")
    if attestation is not None:
        if not isinstance(attestation, SourceVerificationAttestation):
            raise TypeError(
                f"attestation 必须是 SourceVerificationAttestation 或 None，"
                f"实际为 {type(attestation).__name__}"
            )
        if attestation.schema != ATTESTATION_SCHEMA:
            raise AttestationSchemaError(
                f"attestation.schema 期望 {ATTESTATION_SCHEMA!r}，实际为 {attestation.schema!r}"
            )
    if not isinstance(declared_documents, Mapping):
        raise TypeError("declared_documents 必须是 Mapping[source_id, (raw_sha256, blob_sha)]")

    if source_kind != SOURCE_KIND_OFFICIAL_EXCERPT:
        return False
    if not has_provenance:
        return False
    if not checks.all_passed():
        return False
    return (
        promotion_blocking_reason(
            attestation,
            expected_snapshot_set_id=expected_snapshot_set_id,
            declared_documents=declared_documents,
        )
        == ""
    )


def evidence_from_excerpt(excerpt: PromotableExcerpt) -> EvidenceRef:
    """**唯一**把 `PromotableExcerpt` 变成 `EvidenceRef` 的公开工厂。

    入参类型是 `PromotableExcerpt`（而不是任意 Mapping / provider 采集记录），
    而该类型只能由 `promote_excerpt()` 从"官方摘录 + 闸门全通过"的快照构造 ——
    因此"拿到这个类型"本身就**蕴含**"已通过来源校验"。

    能力边界的准确说法是**"受控公开 API + 静态守卫下不可合法生成"**，
    **不是**"类型上不可表达"：Python 没有构造可及性控制，绕过需要蓄意修改代码，
    而那些修改会被 L3/L4 静态守卫与代码评审捕获。
    """
    if not isinstance(excerpt, PromotableExcerpt):
        raise PromotionError(
            f"evidence_from_excerpt 只接受 PromotableExcerpt，实际为 "
            f"{type(excerpt).__name__}：采集层记录 / 任意字典都没有合法路径变成证据"
        )
    return EvidenceRef(
        source_type=SOURCE_FIXTURE,
        source_id=f"fixture:{excerpt.candidate_id}",
        snapshot_version=excerpt.snapshot_version,
        locator=excerpt.locator,
        excerpt=excerpt.excerpt,
        captured_at=excerpt.captured_at,
    )


def make_task_input(candidate_id: str, *, task_id: str | None = None) -> WorkerTaskInput:
    """构造一个最小任务包。

    `candidate_id` 只以**独立一行**的形式写进 context —— 与 `FixtureResearchWorker` 的
    `parse_candidate_id` 约定一致：不猜、不做文本匹配。
    """
    return WorkerTaskInput(
        task_id=task_id or f"research__{candidate_id}",
        title=f"调研候选方案 {candidate_id}",
        assigned_agent="research",
        goal=f"基于官方资料快照整理 {candidate_id} 在各比较维度上的依据",
        context=f"数据来源：官方文档快照（fixtures/official）\ncandidate_id: {candidate_id}\n",
        upstream_results={},
    )


# ---- 加强模式：live_reverify（P2.2-4） ----
#
# attestation 是"过去时点"的核对结论；它的完整性依赖仓库完整性（无数字签名，
# 见 attestations/README.md）。需要**不依赖 attestation 可信度**的场合，
# 当场用本地原件重算全部来源锚点：raw_sha256 → git blob sha1 → 行号切片。
#
# 与 tools/verify_sources.py 是同一语料上的两份判定实现（工具刻意不 import 本库，
# 审计者与被审计实现解耦）；两者的一致性由 tests/test_official_eval_gate.py
# 的交叉核对测试钉住 —— 这是"两份实现靠测试防漂移"的既有机制（同 R6 的例外登记）。

def live_reverify(
    sources: OfficialSources, raw_dir: Path
) -> tuple[bool, list[str], list[str]]:
    """当场用本地原件重算官方快照集的全部来源锚点（P2.2-4）。

    对每份 provenance 登记的文档做三层核对：
    1. `sha256(raw)` == 登记的 `raw_sha256`；
    2. `sha1("blob <len>\\0" + raw)` == 登记的 `blob_sha`（git 对象 id）；
    3. 每条 section 按 `line_range`（1 起闭区间）切片，与 `sections[].text` 逐字节一致。

    **missing 与 problem 分开报**（P2.2_PLAN §6.6）：原件找不到（可能只是目录被清理）
    与"找到了但内容不符"（篡改信号）是两种不同的事实，混报会让"没核对"伪装成
    "核对失败"或反之。

    返回 `(all_ok, problems, missing)`；`all_ok` 为真当且仅当两者皆空。
    纯本地：只读 `raw_dir` 下的文件，不联网、不读时钟。
    """
    problems: list[str] = []
    missing: list[str] = []
    base = Path(raw_dir)
    by_sha: dict[str, Path] = {}
    by_name: dict[str, Path] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            by_sha.setdefault(hashlib.sha256(data).hexdigest(), path)
            by_name.setdefault(path.name, path)

    def _count_lines(text: str) -> int:
        # 与 tools/verify_sources.py 同一约定：按 \n 计，末尾换行不算额外一行
        return len(text.split("\n")) - (1 if text.endswith("\n") else 0)

    for candidate_id in sources.candidate_ids():
        provenance = sources.provenance_for(candidate_id)
        snapshot = sources.snapshot(candidate_id)
        section_by_id = {section.section_id: section for section in snapshot.sections}
        for document in provenance.documents:
            source_id = document.source_id
            raw_path = by_sha.get(document.raw_sha256) or by_name.get(
                Path(document.path).name
            )
            if raw_path is None:
                missing.append(
                    f"{source_id}: 在 {base} 里找不到 sha256 匹配或同名的原始文件"
                )
                continue
            raw = raw_path.read_bytes()
            actual_sha = hashlib.sha256(raw).hexdigest()
            if actual_sha != document.raw_sha256:
                problems.append(
                    f"{source_id}: raw_sha256 不符（登记 {document.raw_sha256}，"
                    f"实际 {actual_sha}）"
                )
            actual_blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            if actual_blob != document.blob_sha:
                problems.append(
                    f"{source_id}: blob_sha 不符（登记 {document.blob_sha}，实际 {actual_blob}）"
                )
            text = raw.decode("utf-8")
            actual_lines = _count_lines(text)
            if document.raw_lines != actual_lines:
                problems.append(
                    f"{source_id}: 行数不符（登记 raw_lines={document.raw_lines}，"
                    f"实际 {actual_lines}）"
                )
            lines = text.split("\n")
            for section_source in provenance.sections:
                if section_source.source_id != source_id:
                    continue
                section = section_by_id.get(section_source.section_id)
                if section is None:
                    problems.append(
                        f"{source_id}/{section_source.section_id}: 登记的 section 不在快照里"
                    )
                    continue
                start, end = section_source.line_span()
                if start < 1 or end > actual_lines:
                    problems.append(
                        f"{source_id}/{section.section_id}: line_range "
                        f"{section_source.line_range} 超出文件行数 {actual_lines}"
                    )
                    continue
                sliced = "\n".join(lines[start - 1 : end])
                if sliced != section.text:
                    problems.append(
                        f"{source_id}/{section.section_id}: "
                        f"L{section_source.line_range} 的切片与快照摘录不一致"
                        f"（原文 {len(sliced)} 字符，快照 {len(section.text)} 字符）"
                    )
    return (not problems and not missing), problems, missing

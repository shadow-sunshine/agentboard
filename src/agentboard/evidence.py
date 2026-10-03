"""证据引用契约（P1A-1 核心新增）。

`EvidenceRef` 是「一条证据指向哪里、摘录是什么」的唯一结构化表示，取代原先的
`WorkerResult.evidence: tuple[str, ...]` 自由文本。

**为什么放在核心而不是 `research/` 领域层**：`contracts.py` 需要引用它。若放在领域层，
核心就要反向 import 研究领域，破坏「`research → core`」的单向依赖，也会把领域语义
（维度、候选、报告）漏进调度核心。

设计要点：
- `@dataclass(frozen=True)`，且所有字段都是 `str`，因此天然可哈希、可序列化。
- `digest` 与 `evidence_id` 由**内容寻址**派生：省略则自动计算；显式传入则必须与
  重算值一致，否则 `ValueError`（防伪造 / 防篡改）。
- `captured_at` 是**快照元数据里的日期**，不是 `now()`。这一点决定了同一份资料在
  不同时间运行会产出相同的 `evidence_id`，是"重复运行一致性"的前提。
- 字符集约定（见 P1A_PLAN.md §1）：`snapshot_version` / `schema` 一律用点号，
  全项目不出现 `@`。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ---- source_type 白名单（P1A 内不含 web / llm，传即拒绝） ----
SOURCE_FIXTURE = "fixture_snapshot"
SOURCE_SYNTHETIC = "synthetic_mock"
SOURCE_LEGACY = "legacy_text"

ALLOWED_SOURCE_TYPES: frozenset[str] = frozenset({SOURCE_FIXTURE, SOURCE_SYNTHETIC, SOURCE_LEGACY})

# source_id 前缀与 source_type 强绑定，构造期校验
SOURCE_ID_PREFIXES: dict[str, str] = {
    SOURCE_FIXTURE: "fixture:",
    SOURCE_SYNTHETIC: "mock:",
    SOURCE_LEGACY: "legacy:",
}

# 只有 fixture_snapshot 是「可核验的真实资料快照」。
# synthetic_mock 是确定性占位，legacy_text 是历史自由文本的有损降级产物，
# 两者都不得被当作真实证据使用（领域层用它决定推荐能否被支撑）。
TRUSTED_SOURCE_TYPES: frozenset[str] = frozenset({SOURCE_FIXTURE})

SYNTHETIC_SNAPSHOT_VERSION = "synthetic.1"
LEGACY_SNAPSHOT_VERSION = "legacy.0"

MAX_EXCERPT_CHARS = 400

SNAPSHOT_VERSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
# 受限 JSON Pointer：以 / 开头，段仅含 [A-Za-z0-9_.-]；
# 不允许空段、不允许 ~0/~1 转义、不允许 "-" 段（append 语义），
# 也不要旧的 L12-L18 行号形式 —— 全项目只保留一种 locator 语法。
LOCATOR_PATTERN = re.compile(r"^/(?:[A-Za-z0-9_.-]+)(?:/[A-Za-z0-9_.-]+)*$")
CAPTURED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_ID_PATTERN = re.compile(r"^ev_[a-z0-9]{16}$")


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串，实际为 {type(value).__name__}")
    return value


def _require_str_nonempty(value: Any, label: str) -> str:
    text = _require_str(value, label)
    if not text.strip():
        raise ValueError(f"{label} 不能是空字符串")
    return text


def _require_match(value: Any, pattern: re.Pattern[str], label: str, hint: str) -> str:
    text = _require_str_nonempty(value, label)
    if not pattern.match(text):
        raise ValueError(f"{label} 必须满足 {hint}，实际为 {text!r}")
    return text


def _optional_str(raw: Mapping[str, Any], key: str) -> str:
    """可选字段：缺失或 None -> 空串（表示"未提供，请自动派生"）；存在但非 str -> ValueError。"""
    if key not in raw:
        return ""
    value = raw[key]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} 字段存在但类型错误，期望 str，实际为 {type(value).__name__}")
    return value


def compute_digest(excerpt: str) -> str:
    """excerpt 的 sha256（UTF-8，逐字节，不做任何空白规范化）。"""
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()


def compute_evidence_id(
    *,
    source_type: str,
    source_id: str,
    snapshot_version: str,
    locator: str,
    digest: str,
) -> str:
    """内容寻址生成 evidence_id。

    哈希输入包含 `source_type`，因此 fixture 证据与 synthetic 证据即使 locator 相同
    也不会撞 id；包含 `digest`，因此摘录内容变了 id 就变。
    """
    material = f"{source_type}|{source_id}|{snapshot_version}|{locator}|{digest}"
    return "ev_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def is_trusted_evidence(ref: EvidenceRef) -> bool:
    """该证据是否来自可核验的真实资料快照（只有 fixture_snapshot 为真）。"""
    if not isinstance(ref, EvidenceRef):
        raise ValueError("is_trusted_evidence 需要 EvidenceRef")
    return ref.source_type in TRUSTED_SOURCE_TYPES


@dataclass(frozen=True)
class EvidenceRef:
    """一条结构化证据引用。

    `digest` / `evidence_id` 通常省略（留空即自动派生）；显式传入时必须与内容一致。
    """

    source_type: str
    source_id: str
    snapshot_version: str
    locator: str
    excerpt: str
    captured_at: str
    digest: str = ""
    evidence_id: str = ""

    def __post_init__(self) -> None:
        source_type = _require_str_nonempty(self.source_type, "EvidenceRef.source_type")
        if source_type not in ALLOWED_SOURCE_TYPES:
            raise ValueError(
                f"EvidenceRef.source_type 必须是 {sorted(ALLOWED_SOURCE_TYPES)}，"
                f"实际为 {source_type!r}"
            )

        source_id = _require_str_nonempty(self.source_id, "EvidenceRef.source_id")
        prefix = SOURCE_ID_PREFIXES[source_type]
        if not source_id.startswith(prefix):
            raise ValueError(
                f"EvidenceRef.source_id 在 source_type={source_type!r} 时必须以 "
                f"{prefix!r} 开头，实际为 {source_id!r}"
            )

        _require_match(
            self.snapshot_version,
            SNAPSHOT_VERSION_PATTERN,
            "EvidenceRef.snapshot_version",
            "小写且仅含 [a-z0-9._-]、长度 3-64（如 synthetic.1 / legacy.0）",
        )
        _require_match(
            self.locator,
            LOCATOR_PATTERN,
            "EvidenceRef.locator",
            "受限 JSON Pointer（以 / 开头，段仅含 [A-Za-z0-9_.-]）",
        )

        excerpt = _require_str_nonempty(self.excerpt, "EvidenceRef.excerpt")
        if len(excerpt) > MAX_EXCERPT_CHARS:
            raise ValueError(
                f"EvidenceRef.excerpt 不能超过 {MAX_EXCERPT_CHARS} 字符，实际 {len(excerpt)}"
            )
        _require_match(
            self.captured_at,
            CAPTURED_AT_PATTERN,
            "EvidenceRef.captured_at",
            "YYYY-MM-DD 形式（来自快照元数据，不得使用当前时间）",
        )

        expected_digest = compute_digest(excerpt)
        if self.digest == "":
            object.__setattr__(self, "digest", expected_digest)
        else:
            _require_match(self.digest, DIGEST_PATTERN, "EvidenceRef.digest", "64 位小写十六进制")
            if self.digest != expected_digest:
                raise ValueError("EvidenceRef.digest 与 excerpt 不匹配，拒绝接受被篡改的摘要")

        expected_id = compute_evidence_id(
            source_type=source_type,
            source_id=source_id,
            snapshot_version=self.snapshot_version,
            locator=self.locator,
            digest=self.digest,
        )
        if self.evidence_id == "":
            object.__setattr__(self, "evidence_id", expected_id)
        else:
            _require_match(
                self.evidence_id, EVIDENCE_ID_PATTERN, "EvidenceRef.evidence_id", "^ev_[a-z0-9]{16}$"
            )
            if self.evidence_id != expected_id:
                raise ValueError(
                    f"EvidenceRef.evidence_id 与内容不符（期望 {expected_id}），拒绝接受伪造 id"
                )

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "snapshot_version": self.snapshot_version,
            "locator": self.locator,
            "excerpt": self.excerpt,
            "digest": self.digest,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EvidenceRef:
        """严格反序列化：缺字段 / 类型错误 / 摘要不符 一律 ValueError，不做静默补默认。

        接受 `Mapping` 而非只接受 `dict`：`WorkerResult.structured_payload` 在核心层会被
        冻结成 `MappingProxyType`，领域层解码时会直接读到那种只读映射（见 `research.payload`）。
        """
        if not isinstance(raw, Mapping):
            raise ValueError("EvidenceRef.from_dict 需要 Mapping")
        return cls(
            source_type=raw.get("source_type"),
            source_id=raw.get("source_id"),
            snapshot_version=raw.get("snapshot_version"),
            locator=raw.get("locator"),
            excerpt=raw.get("excerpt"),
            captured_at=raw.get("captured_at"),
            digest=_optional_str(raw, "digest"),
            evidence_id=_optional_str(raw, "evidence_id"),
        )

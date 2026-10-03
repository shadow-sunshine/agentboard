"""外部验证证明：`SourceVerificationAttestation`（C6，P2.2-1）。

为什么它必须活在 fixture 之外
------------------------------
准入闸门 C1–C5 / C7 / C8 全部可以在**加载期**只读本地 JSON 判定，
唯独 C6（"摘录与原文逐字节一致"）需要**原件**。若把 C6 的结论写成 fixture 里的字段，
就等于修改 fixture 字节 → 触发重签 → 破坏"`fixtures/**` 完全冻结"这条承诺。
因此 C6 的结论由**独立工件**承载，放在项目根的 `attestations/` 下。

自绑定：只锚定"声明值"是不够的
--------------------------------
只比对 `documents[].raw_sha256` / `blob_sha` 有一个致命弱点：它们是
**写在 fixture 里的声明值**，不是 fixture 本身。两者一致只能证明
"**两个文件里的两句话一致**"，证明不了"**被证明的快照就是我手上这份**"。

所以本模块做三层锚点，缺一不可：

| 层 | 锚点 | 拦住的攻击 |
|---|---|---|
| L-a | `index_sha256` == 实际 `INDEX.json` 字节摘要 | 重新签名 `INDEX.json` 以掩盖被改的 fixture |
| L-b | `snapshot_files[].sha256` == 各快照文件实际字节摘要 | 直接替换 / 编辑任何快照文件（改摘录、改行号、改 provenance） |
| L-c | `documents[].raw_sha256` / `blob_sha` == provenance 声明值 | 声称"核对的是另一份上游原件" |

**L-a / L-b 是文件级校验，且不依赖 `INDEX.json` 自身的正确性** ——
`INDEX.json` 被重签也没用，因为它的摘要本身也在被证明之列。

⚠️ 明确禁止的实现走法（写在这里，防止后来者"简化"）
----------------------------------------------------
**不得**只用"看 `overall_result` + 比 `raw_sha256`/`blob_sha`"就判定 attestation 有效。
那只是"声明与声明一致"。条件 8 的两个文件级锚点必须校验。

与 C7 的分工（否则容易以为重复）
---------------------------------
| 检查 | 判据 | 是否信任 `INDEX.json` | 目的 |
|---|---|---|---|
| C7（加载期） | 文件 sha256 == `INDEX.json` 登记的 `fixture_sha256` | **信任** | "整份文件就是登记的那一份" |
| C6 自绑定（升格期） | 文件 sha256 == **attestation** 登记的 `sha256`，且 `INDEX.json` 摘要 == `index_sha256` | **不信任** | "我这次要升格的，正是当初核对过的那一份" |

信任模型（必须明说，不要给读者"这是密码学证明"的错觉）
--------------------------------------------------------
**本项目不实现数字签名。** attestation 的**完整性**依赖"仓库内容不可被静默篡改"
—— 即 git 历史 + 代码评审 + `G-PRESERVE` 门禁共同提供的**过程保证**。
它**不是**密码学保证：任何能改写仓库文件的人也能改写 attestation，且这种改写
**无法被工具单方识破**。

因此本模块提供的说法是：**attestation 证明的是"有人在某个时点按登记的规则
核对过这批字节"，不是"这批字节绝对可信"。**

需要"不依赖 attestation 可信度"的场合，走**加强模式**：
当场提供原件重新核对（`require_live_verification`，P2.2-3 接入）。

零时间字段
-----------
本模块**不出现任何时间字段**，也不读时钟。同一份 fixture + 同一批原件
⇒ 逐字节相同的工件。这不是洁癖：一旦引入"核对时间"，同一输入在不同时刻会产出
不同字节，破坏"同一输入必得同一输出"，也让 `report_id` 的内容寻址性质失效。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (
    ATTESTATION_SCHEMA,
    EXPECTED_VERIFIER_VERSION,
    LINE_RANGE_PATTERN,
    REASON_ABSENT,
    REASON_INCOMPLETE,
    REASON_MISMATCH,
    REASON_UNBOUND,
    REASON_VERSION_EXPIRED,
    RESULT_PASSED,
    VALID_ATTESTATION_RESULTS,
)
from .errors import AttestationSchemaError
from .snapshot import INDEX_FILENAME, SHA256_PATTERN, file_sha256, read_json_object

#: git blob sha（40 位小写十六进制）。与 `sources.COMMIT_SHA_PATTERN` 同形，
#: 但这里独立定义：attestation 不得反向依赖 `sources`（`sources` 依赖本模块）。
BLOB_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "snapshot_set_id",
        "index_sha256",
        "snapshot_files",
        "verifier",
        "verifier_version",
        "algorithm",
        "documents",
        "overall_result",
    }
)
_SNAPSHOT_FILE_KEYS = frozenset({"path", "sha256", "candidate_id"})
_DOCUMENT_KEYS = frozenset({"source_id", "raw_sha256", "blob_sha", "line_ranges", "result"})


# ---- 字段级校验小工具 ----


def _require_str(raw: Mapping[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AttestationSchemaError(f"{label}.{key} 必须是非空字符串，实际为 {value!r}")
    return value


def _require_sha256(raw: Mapping[str, Any], key: str, label: str) -> str:
    value = _require_str(raw, key, label)
    if not SHA256_PATTERN.match(value):
        raise AttestationSchemaError(f"{label}.{key} 必须是 64 位小写十六进制，实际为 {value!r}")
    return value


def _require_blob_sha(raw: Mapping[str, Any], key: str, label: str) -> str:
    value = _require_str(raw, key, label)
    if not BLOB_SHA_PATTERN.match(value):
        raise AttestationSchemaError(
            f"{label}.{key} 必须是 40 位小写十六进制（git blob sha），实际为 {value!r}"
        )
    return value


def _require_version(raw: Mapping[str, Any], label: str) -> int:
    value = raw.get("verifier_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AttestationSchemaError(
            f"{label}.verifier_version 必须是 ≥ 1 的整数（**不是**时间戳），实际为 {value!r}"
        )
    return value


def _reject_unknown_keys(raw: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AttestationSchemaError(
            f"{label} 出现未知字段 {unknown}：证明工件的字段集是闭集，"
            f"未知字段说明工件由另一套 schema 产出（允许：{sorted(allowed)}）"
        )


def _parse_line_ranges(value: Any, label: str) -> tuple[str, ...]:
    """解析行区间列表，并**按数值排序**（与输入顺序无关 ⇒ 逐字节可复现）。"""
    if not isinstance(value, (list, tuple)):
        raise AttestationSchemaError(f"{label} 必须是列表")
    parsed: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not LINE_RANGE_PATTERN.match(item):
            raise AttestationSchemaError(
                f"{label}[{index}] 必须是 ^起始行[-结束行]$ 形态，实际为 {item!r}"
            )
        if item in parsed:
            raise AttestationSchemaError(f"{label} 出现重复行区间 {item!r}")
        parsed.append(item)
    return tuple(sorted(parsed, key=_line_range_sort_key))


def _line_range_sort_key(line_range: str) -> tuple[int, int]:
    if "-" in line_range:
        start_text, end_text = line_range.split("-", 1)
        return int(start_text), int(end_text)
    value = int(line_range)
    return value, value


def _parse_result(value: Any, label: str) -> str:
    if not isinstance(value, str) or value not in VALID_ATTESTATION_RESULTS:
        raise AttestationSchemaError(
            f"{label} 必须是 {sorted(VALID_ATTESTATION_RESULTS)}，实际为 {value!r}"
        )
    return value


@dataclass(frozen=True)
class AttestationSnapshotFile:
    """被证明的一份快照文件（自绑定锚点 L-b）。"""

    path: str
    sha256: str
    candidate_id: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "candidate_id": self.candidate_id}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], label: str) -> AttestationSnapshotFile:
        if not isinstance(raw, Mapping):
            raise AttestationSchemaError(f"{label} 必须是 JSON 对象")
        _reject_unknown_keys(raw, _SNAPSHOT_FILE_KEYS, label)
        path = _require_str(raw, "path", label)
        if "://" in path or path.startswith("/") or "\\" in path:
            raise AttestationSchemaError(f"{label}.path 必须是 POSIX 相对路径，实际为 {path!r}")
        return cls(
            path=path,
            sha256=_require_sha256(raw, "sha256", label),
            candidate_id=_require_str(raw, "candidate_id", label),
        )


@dataclass(frozen=True)
class AttestationDocument:
    """一份被核对的上游文档（声明级锚点 L-c）。"""

    source_id: str
    raw_sha256: str
    blob_sha: str
    line_ranges: tuple[str, ...]
    result: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "raw_sha256": self.raw_sha256,
            "blob_sha": self.blob_sha,
            "line_ranges": list(self.line_ranges),
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], label: str) -> AttestationDocument:
        if not isinstance(raw, Mapping):
            raise AttestationSchemaError(f"{label} 必须是 JSON 对象")
        _reject_unknown_keys(raw, _DOCUMENT_KEYS, label)
        return cls(
            source_id=_require_str(raw, "source_id", label),
            raw_sha256=_require_sha256(raw, "raw_sha256", label),
            blob_sha=_require_blob_sha(raw, "blob_sha", label),
            line_ranges=_parse_line_ranges(raw.get("line_ranges"), f"{label}.line_ranges"),
            result=_parse_result(raw.get("result"), f"{label}.result"),
        )


@dataclass(frozen=True)
class AttestationBinding:
    """一次文件级绑定的**测量结果**。

    只能由 `load_attestation()` 在读完磁盘真实字节之后产生。
    `reason == ""` 表示绑定通过；否则是 `config.REASON_*` 之一。

    ⚠️ **它不是凭据，只是结论的载体。** 单独构造一个 `AttestationBinding(reason="")`
    并不能让任何东西变可信 —— 它必须被挂到 `SourceVerificationAttestation.binding` 上，
    而那个字段**不在构造器签名里**（见下），只能由 `load_attestation()` 设置。
    """

    reason: str
    detail: str = ""


@dataclass(frozen=True)
class SourceVerificationAttestation:
    """C6 的外部证明工件（**已解析**形态；绑定结论另挂）。

    **必须由 `load_attestation()` 产生**才算有效。这条不是靠文档约定，而是靠机制：

    - `binding` 字段声明为 `field(init=False, repr=False, compare=False)`，
      因此**它不在构造器的参数列表里** ——
      `SourceVerificationAttestation(..., binding_reason="")` 会直接 `TypeError`；
    - `from_dict()` 走同一条构造路径，产出的对象 `binding is None`；
    - `promotion_blocking_reason()` 把 `binding is None` 判为 `REASON_UNBOUND`（fail-closed）。

    **能力边界的准确说法**（与 `evidence.py` 的 `PromotableExcerpt` 同一标准）：
    **受控公开 API + 静态守卫下不可合法生成** —— 而**不是**"不可能构造"。
    Python 没有构造可及性控制，绕过它需要**蓄意**写出
    `object.__setattr__(att, "binding", AttestationBinding(...))` 或直接 import 私有模块，
    而那类写法会被 L5 静态守卫（`tests/test_static_guards.py`）与代码评审捕获。

    为什么**不能**只靠"拆成两个类型（已解析 / 已绑定）"来保护：
    `BoundAttestation(attestation=parsed, binding=AttestationBinding(""))` 与直接构造同样容易。
    真正起作用的是**"这个状态不可能经构造器传入"**这一条，类型拆分只是让语义更清晰。
    """

    schema: str
    snapshot_set_id: str
    index_sha256: str
    snapshot_files: tuple[AttestationSnapshotFile, ...]
    verifier: str
    verifier_version: int
    algorithm: str
    documents: tuple[AttestationDocument, ...]
    overall_result: str
    #: 文件级绑定结论。**故意 `init=False`**：构造器无法设置它，
    #: 于是"手拼一份字段齐全的证明"这条路在**签名层面**就不存在。
    #: `None` = 未绑定（不可升格）；`reason == ""` = 绑定通过。
    binding: AttestationBinding | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def is_bound(self) -> bool:
        """是否经过文件级绑定。只有 `load_attestation()` 的产物为 `True`。"""
        return self.binding is not None

    @property
    def binding_reason(self) -> str | None:
        """绑定结论：`None` 未绑定 / `""` 通过 / 其它为 `REASON_*`。"""
        return None if self.binding is None else self.binding.reason

    @property
    def binding_detail(self) -> str:
        return "" if self.binding is None else self.binding.detail

    # ---- 解析 ----

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SourceVerificationAttestation:
        """严格解析。未知字段 / 类型错误 / 重复项一律抛错，不做静默兜底。"""
        if not isinstance(raw, Mapping):
            raise AttestationSchemaError("attestation 必须是 JSON 对象")
        _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, "attestation")

        schema = _require_str(raw, "schema", "attestation")
        if schema != ATTESTATION_SCHEMA:
            raise AttestationSchemaError(
                f"attestation.schema 期望 {ATTESTATION_SCHEMA!r}，实际为 {schema!r}"
            )

        raw_files = raw.get("snapshot_files")
        if not isinstance(raw_files, list) or not raw_files:
            raise AttestationSchemaError("attestation.snapshot_files 必须是非空列表")
        files = [
            AttestationSnapshotFile.from_dict(item, f"attestation.snapshot_files[{index}]")
            for index, item in enumerate(raw_files)
        ]
        paths = [item.path for item in files]
        if len(set(paths)) != len(paths):
            raise AttestationSchemaError(f"attestation.snapshot_files 存在重复 path：{sorted(paths)}")

        raw_documents = raw.get("documents")
        if not isinstance(raw_documents, list) or not raw_documents:
            # attempt 3（H3b 解析层加固）：空证明不构成核对结论 ——
            # 独立负控复现过"documents 清空后 loader 仍绑定通过"，这里在解析层就拒绝。
            raise AttestationSchemaError(
                "attestation.documents 必须是非空列表（空证明不构成核对结论）"
            )
        documents = [
            AttestationDocument.from_dict(item, f"attestation.documents[{index}]")
            for index, item in enumerate(raw_documents)
        ]
        source_ids = [item.source_id for item in documents]
        if len(set(source_ids)) != len(source_ids):
            raise AttestationSchemaError(
                f"attestation.documents 存在重复 source_id：{sorted(source_ids)}"
            )

        return cls(
            schema=schema,
            snapshot_set_id=_require_str(raw, "snapshot_set_id", "attestation"),
            index_sha256=_require_sha256(raw, "index_sha256", "attestation"),
            snapshot_files=tuple(sorted(files, key=lambda item: item.path)),
            verifier=_require_str(raw, "verifier", "attestation"),
            verifier_version=_require_version(raw, "attestation"),
            algorithm=_require_str(raw, "algorithm", "attestation"),
            documents=tuple(sorted(documents, key=lambda item: item.source_id)),
            overall_result=_parse_result(raw.get("overall_result"), "attestation.overall_result"),
        )

    # ---- 序列化（规范 JSON，逐字节可复现） ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_set_id": self.snapshot_set_id,
            "index_sha256": self.index_sha256,
            "snapshot_files": [item.to_dict() for item in self.snapshot_files],
            "verifier": self.verifier,
            "verifier_version": self.verifier_version,
            "algorithm": self.algorithm,
            "documents": [item.to_dict() for item in self.documents],
            "overall_result": self.overall_result,
        }

    def document(self, source_id: str) -> AttestationDocument:
        for item in self.documents:
            if item.source_id == source_id:
                return item
        raise AttestationSchemaError(f"attestation 里没有文档 {source_id!r}")

    @property
    def document_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.documents)


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """工件的规范序列化：`sort_keys` + 2 空格缩进 + 末尾换行 + UTF-8 无 BOM + LF。

    与既有 fixture 文件的写法一致。`sort_keys` 让键序与 Python 字典插入顺序无关，
    列表的顺序由各自的构造处负责排序（`documents[]` 按 `source_id`、
    `line_ranges` 按数值、`snapshot_files[]` 按 `path`）⇒ 逐字节可复现。
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


# ---- 文件级自绑定 ----


def fixture_fingerprint(
    fixture_root: Path,
) -> tuple[str, tuple[AttestationSnapshotFile, ...]]:
    """算出**当前磁盘上**的 `(INDEX.json 摘要, 快照文件清单)`。

    直接读字节，**不经过** `load_catalog` / `load_snapshots`：绑定校验要独立于
    被测库的解析结果，否则"库解析错了"会同时污染两端，校验就失去了意义。
    """
    root = Path(fixture_root)
    index_path = root / INDEX_FILENAME
    index_sha = file_sha256(index_path)
    raw = read_json_object(index_path)
    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise AttestationSchemaError(f"{INDEX_FILENAME}.candidates 必须是非空列表")

    files: list[AttestationSnapshotFile] = []
    for index, item in enumerate(raw_candidates):
        label = f"{INDEX_FILENAME}.candidates[{index}]"
        if not isinstance(item, Mapping):
            raise AttestationSchemaError(f"{label} 必须是 JSON 对象")
        rel = item.get("fixture_path")
        candidate_id = item.get("candidate_id")
        if not isinstance(rel, str) or not rel.strip():
            raise AttestationSchemaError(f"{label}.fixture_path 必须是非空字符串")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise AttestationSchemaError(f"{label}.candidate_id 必须是非空字符串")
        files.append(
            AttestationSnapshotFile(
                path=rel,
                sha256=file_sha256(root / rel),
                candidate_id=candidate_id,
            )
        )
    return index_sha, tuple(sorted(files, key=lambda item: item.path))


def binding_reason_from_bytes(
    attestation: SourceVerificationAttestation,
    index_bytes: bytes,
    snapshot_file_bytes: Mapping[str, bytes],
) -> tuple[str, str]:
    """文件层绑定校验的**纯核心**：只吃字节缓冲，零 I/O（attempt 3，D3）。

    `load_attestation()`（绑定时刻）与组装期 S0.5（重验时刻）**共用同一实现**（R6）。
    调用方按 H2 单次读取契约提供字节：摘要与解析由调用方基于**同一份缓冲**完成 ——
    "先读一次算摘要、再读一次解析"的组装内 TOCTOU 在结构上不可表达。

    返回 `("" 或 REASON_MISMATCH, 说明)`。任一环节对不上都算 mismatch。
    """
    try:
        raw = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return REASON_MISMATCH, f"INDEX.json 字节无法解析为 JSON：{exc}"
    if not isinstance(raw, dict):
        return REASON_MISMATCH, "INDEX.json 顶层必须是 JSON 对象"

    actual_index_sha = hashlib.sha256(index_bytes).hexdigest()
    if actual_index_sha != attestation.index_sha256:
        return REASON_MISMATCH, (
            f"{INDEX_FILENAME} 的实际字节摘要 {actual_index_sha} 与证明登记 "
            f"{attestation.index_sha256} 不一致"
        )

    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return REASON_MISMATCH, f"{INDEX_FILENAME}.candidates 必须是非空列表"

    claimed = {item.path: item for item in attestation.snapshot_files}
    actual: dict[str, tuple[str, str]] = {}
    for index, item in enumerate(raw_candidates):
        label = f"{INDEX_FILENAME}.candidates[{index}]"
        if not isinstance(item, dict):
            return REASON_MISMATCH, f"{label} 必须是 JSON 对象"
        rel = item.get("fixture_path")
        candidate_id = item.get("candidate_id")
        if not isinstance(rel, str) or not rel.strip():
            return REASON_MISMATCH, f"{label}.fixture_path 必须是非空字符串"
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return REASON_MISMATCH, f"{label}.candidate_id 必须是非空字符串"
        data = snapshot_file_bytes.get(rel)
        if data is None:
            return REASON_MISMATCH, f"{rel} 的字节未随本次测量提供（读不到文件）"
        actual[rel] = (hashlib.sha256(data).hexdigest(), candidate_id)

    missing = sorted(set(actual) - set(claimed))
    extra = sorted(set(claimed) - set(actual))
    if missing or extra:
        return REASON_MISMATCH, (
            f"快照文件集合与证明登记的不一致（实际多出/缺失）："
            f"未被证明={missing}，证明里多出={extra}"
        )
    for path in sorted(actual):
        actual_sha, actual_cid = actual[path]
        if claimed[path].sha256 != actual_sha:
            return REASON_MISMATCH, (
                f"{path} 的实际字节摘要 {actual_sha} 与证明登记 "
                f"{claimed[path].sha256} 不一致"
            )
        if claimed[path].candidate_id != actual_cid:
            return REASON_MISMATCH, (
                f"{path} 的 candidate_id 与 {INDEX_FILENAME} 登记的不一致"
            )
    return "", ""


def verify_attestation_against(
    attestation: SourceVerificationAttestation,
    fixture_root: Path,
) -> tuple[str, str]:
    """文件层重验的**磁盘包装**：按 H2 单次读取契约读盘，调纯核心（attempt 3）。

    每个文件只 `read_bytes()` 一次；`load_attestation()` 与组装期共用本函数（R6）。
    """
    root = Path(fixture_root)
    index_path = root / INDEX_FILENAME
    if not index_path.is_file():
        return REASON_MISMATCH, f"找不到 {index_path}"

    index_bytes = index_path.read_bytes()
    file_bytes: dict[str, bytes] = {}
    try:
        index_parsed = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        index_parsed = None
    if isinstance(index_parsed, dict) and isinstance(index_parsed.get("candidates"), list):
        for item in index_parsed["candidates"]:
            if not isinstance(item, dict) or not isinstance(item.get("fixture_path"), str):
                continue
            rel = item["fixture_path"]
            try:
                file_bytes[rel] = (root / rel).read_bytes()
            except OSError:
                # 读不到的文件不进缓冲 ⇒ 纯核心给出"读不到文件"的 mismatch
                continue
    return binding_reason_from_bytes(attestation, index_bytes, file_bytes)


def documents_blocking_reason(
    attestation: SourceVerificationAttestation,
    declared_documents: Mapping[str, tuple[str, str]],
) -> tuple[str, str]:
    """声明层完整性核对（H3b，attempt 3）的**唯一实现**。

    把 `attestation.documents[]` 与**当前 provenance 声明并集**做两查：
    1. **精确等集**：`source_id` 集合必须"不多不少"（"少"= 有文档没被证明，
       "多"= 证明覆盖了不属于本快照集的文档）；
    2. **逐项核对**：每个 `source_id` 的 `raw_sha256` / `blob_sha` == 声明值，
       且 `result == passed`。

    独立负控复现过：`documents` 被清空后文件层绑定仍通过 —— 文件层锚点只验字节，
    本函数补上"证明是否覆盖全部已声明文档"这一层。
    `promotion_blocking_reason` 与组装期 S0.5 共用本实现（R6）。

    返回 `("" 或 REASON_MISMATCH/REASON_INCOMPLETE, 说明)`。
    """
    declared = dict(declared_documents)
    attested_ids = set(attestation.document_ids)
    declared_ids = set(declared)
    missing = sorted(declared_ids - attested_ids)
    extra = sorted(attested_ids - declared_ids)
    if missing or extra:
        return REASON_MISMATCH, (
            f"证明 documents 与当前 provenance 声明不一致："
            f"未覆盖={missing}，证明里多出={extra}"
        )
    for source_id in sorted(declared):
        raw_sha256, blob_sha = declared[source_id]
        document = attestation.document(source_id)
        if document.raw_sha256 != raw_sha256 or document.blob_sha != blob_sha:
            return REASON_MISMATCH, (
                f"文档 {source_id} 的登记摘要与当前 provenance 声明不一致"
            )
        if document.result != RESULT_PASSED:
            return REASON_INCOMPLETE, f"文档 {source_id} 的核对结论为 {document.result!r}"
    return "", ""


def load_attestation(
    path: Path, *, fixture_root: Path
) -> SourceVerificationAttestation:
    """读取证明工件，并**立即**与当前磁盘上的 fixture 做文件级绑定。

    这是**唯一**会给 `attestation.binding` 赋值的地方（该字段 `init=False`，
    构造器签名里根本没有它）—— 因此"有没有做过文件级绑定"这件事
    只可能由这个函数决定，不可能由调用方的参数决定。

    `is_promotable_source()` 因此仍可保持纯函数：所有 I/O 都在这里发生。
    """
    attestation = SourceVerificationAttestation.from_dict(read_json_object(Path(path)))
    reason, detail = verify_attestation_against(attestation, Path(fixture_root))
    object.__setattr__(attestation, "binding", AttestationBinding(reason=reason, detail=detail))
    return attestation


def promotion_blocking_reason(
    attestation: SourceVerificationAttestation | None,
    *,
    expected_snapshot_set_id: str,
    declared_documents: Mapping[str, tuple[str, str]],
) -> str:
    """attestation 是否**有效到**足以支撑升格。返回 `""` 表示有效。

    返回值是**原因**而不是布尔，因为两种 `False` 的原因必须可区分：
    "因为没有证明"（`absent`）与"因为证明不通过"（`mismatch` / `incomplete`）
    是完全不同的处置路径，混为一谈会让审计无法定位问题。

    `declared_documents`：`source_id -> (raw_sha256, blob_sha)` 的**快照集级并集**
    （由 `sources.snapshot_set_document_digests()` 提供）。
    为什么是并集而不是单候选：attestation 是**快照集级**工件（`snapshot_set_id` 即集合级标识），
    其 `documents[]` 覆盖集合内全部文档；比对基准若只取单个候选，
    "集合里有没有漏证明某份文档"就无从判断。
    """
    if attestation is None:
        return REASON_ABSENT
    if not isinstance(attestation, SourceVerificationAttestation):
        # 防御性：不接受鸭子类型的"看起来像证明"的对象
        raise TypeError(
            f"promotion_blocking_reason 需要 SourceVerificationAttestation 或 None，"
            f"实际为 {type(attestation).__name__}"
        )
    binding = attestation.binding
    if binding is None:
        # 从未与磁盘字节绑定过 ⇒ fail-closed。
        # 走到这条分支只有两种情形：①对象由 from_dict / 构造器产出（该字段 init=False，
        # 无法经构造器传入）；②有人显式 object.__setattr__ 设过 —— 那属于蓄意绕过，
        # 由 L5 静态守卫（tests/test_static_guards.py）与代码评审负责捕获。
        return REASON_UNBOUND
    if attestation.verifier_version != EXPECTED_VERIFIER_VERSION:
        return REASON_VERSION_EXPIRED
    if attestation.snapshot_set_id != expected_snapshot_set_id:
        return REASON_MISMATCH
    if attestation.overall_result != RESULT_PASSED:
        return REASON_INCOMPLETE
    if binding.reason != "":
        return binding.reason

    # 声明层核对（H3b）：与组装期 S0.5 共用同一实现（attempt 3，R6）。
    # 旧版这段逻辑内联在这里 —— 文件层绑定不看 documents，独立负控复现过
    # "documents 清空后绑定仍通过"，故抽出为共享纯函数。
    reason, _detail = documents_blocking_reason(attestation, declared_documents)
    return reason

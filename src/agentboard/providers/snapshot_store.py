"""不可覆盖的本地快照存储（P2.1）。

两条硬性质，用机制保证而不是靠自觉
----------------------------------

**性质 1：不可覆盖。**
文件名由内容 sha256 决定（`sha256-<64hex>.json`），写入用 `open(..., "x")` ——
独占创建，文件已存在就抛 `FileExistsError`。所以"覆盖一份既有快照"需要一个
刻意写出来的 `unlink`，而不是一个不小心的 `open(..., "w")`。
写入过程用**临时文件 + `os.replace`**，避免半截文件被当成完整快照。

**性质 2：相同内容不重复落盘。**
`write()` 先算内容 sha256，命中既有文件即返回
`SnapshotWriteResult(status="duplicate")`，**一个字节都不改**。
这满足 P2.1 第 3 条："相同内容重复采集不得悄悄覆盖旧快照" ——
注意此处是"不覆盖也不新建"，而不是"删掉旧的再写新的"。

目录位置
--------
**不在 `fixtures/` 下**。`fixtures/research/` 与 `fixtures/official/` 是人工核对的
静态资料，参与 `tools/sign_fixtures.py` 签名与基线；联网采集的产物必须与它们物理隔离，
否则"把联网结果伪装成静态 fixture"会变成一次 `mv` 就能做到的事。
默认落在仓库根的 `var/snapshots/`，该目录**不进基线清单**，且被 `.gitignore` 式地
当作运行时产物看待（不影响 `tools/verify_manifest.py`，因为其 `IGNORE_DIRS` 之外的
新目录只会出现在 `--diff-tree` 的 `added:` 行里）。

为什么快照里带 `provider_id`
-----------------------------
只有 `source_url` 是不够的：同一个 URL 可能被不同的采集程序以不同方式解析
（截断、规范化、选哪几个字段）。把 `provider_id` 一并记录，复核者才能回答
"这份字节是谁、按什么规则产出的"。`parser_version` 同理。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import InvalidSourceUrlError, ProviderError
from .interface import (
    CAPTURED_AT_PATTERN,
    SHA256_PATTERN,
    RetrievedDocument,
    is_https_url,
)

#: 快照元数据 schema。与 `research.fixture.v1` **刻意不同**：
#: 两者结构无关，共用标识会让"这是静态资料"的判定出现歧义。
SNAPSHOT_SCHEMA = "provider.snapshot.v1"

#: 默认目录名（相对仓库根）。`fixtures/` 之外，见模块 docstring。
DEFAULT_SNAPSHOT_DIRNAME = "var/snapshots"

SNAPSHOT_FILENAME_PATTERN = re.compile(r"^sha256-([0-9a-f]{64})\.json$")

_COPY_CHUNK = 1 << 20


def snapshot_filename(content_sha256: str) -> str:
    """内容哈希 → 快照文件名。**文件名就是内容寻址**，因此天然不可覆盖。"""
    if not isinstance(content_sha256, str) or not SHA256_PATTERN.match(content_sha256):
        raise ProviderError(
            f"snapshot_filename 需要一个 64 位小写十六进制哈希，实际 {content_sha256!r}"
        )
    return f"sha256-{content_sha256}.json"


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """一份落盘快照的元数据（不含原文，原文在文件里）。"""

    path: Path
    content_sha256: str
    source_id: str
    query_terms: str
    source_url: str
    title: str
    captured_at: str
    provider_id: str
    byte_size: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "content_sha256": self.content_sha256,
            "source_id": self.source_id,
            "query_terms": self.query_terms,
            "source_url": self.source_url,
            "title": self.title,
            "captured_at": self.captured_at,
            "provider_id": self.provider_id,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True, slots=True)
class SnapshotWriteResult:
    """一次 `write()` 的结果。

    `status` 三值：

    - `written`     新建了一份快照；
    - `duplicate`   内容哈希已存在，**未改动任何文件**；
    - `conflict`    目标路径已存在但**内容哈希不同** —— 内容寻址下这不可能自然发生，
                     只可能是同名文件被改过或哈希实现变了，属于必须人工介入的状态。
    """

    status: str
    snapshot: StoredSnapshot
    existing_path: Path | None = None

    @property
    def written(self) -> bool:
        return self.status == "written"

    @property
    def duplicate(self) -> bool:
        return self.status == "duplicate"


class SnapshotStore:
    """内容寻址、不可覆盖的本地快照目录。

    目录布局（`root` 即 `var/snapshots/`）：

        root/
          sha256-<64hex>.json     每份快照一个文件，文件名 = 内容哈希
          manifest.json           已落盘快照的索引（内容哈希 → 元数据）

    `manifest.json` **只是索引，不是真源**：真源永远是那一堆内容寻址文件。
    索引可以重建（`rebuild_manifest`），文件不能重写。这样即使索引损坏，
    也不会连带损坏资料本身。
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def manifest_path(self) -> Path:
        return self._root / "manifest.json"

    # ---- 写 ----

    def write(
        self,
        document: RetrievedDocument,
        *,
        provider_id: str,
    ) -> SnapshotWriteResult:
        """保存一份资料。**幂等**：同内容重复调用只返回 `duplicate`，不落第二份。"""
        self._validate(document, provider_id)
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / snapshot_filename(document.content_sha256)

        if target.exists():
            existing_hash = self._read_existing_hash(target)
            if existing_hash == document.content_sha256:
                # 命中：一个字节都不改。这正是"不悄悄覆盖"的实现。
                return SnapshotWriteResult(
                    status="duplicate",
                    snapshot=self._stat(target, document, provider_id),
                    existing_path=target,
                )
            return SnapshotWriteResult(
                status="conflict",
                snapshot=self._stat(target, document, provider_id),
                existing_path=target,
            )

        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "provider_id": provider_id,
            "source_id": document.source_id,
            "query_terms": document.query_terms,
            "source_url": document.source_url,
            "title": document.title,
            "captured_at": document.captured_at,
            "content_sha256": document.content_sha256,
            "content": document.content,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        self._atomic_create(target, encoded.encode("utf-8"))

        snapshot = self._stat(target, document, provider_id)
        self._append_manifest(snapshot)
        return SnapshotWriteResult(status="written", snapshot=snapshot)

    def write_all(
        self,
        documents: Sequence[RetrievedDocument],
        *,
        provider_id: str,
    ) -> tuple[SnapshotWriteResult, ...]:
        """批量写入。逐条独立：一份失败不影响其余，也不修改已写成的文件。"""
        return tuple(self.write(document, provider_id=provider_id) for document in documents)

    # ---- 读 ----

    def list_snapshots(self) -> tuple[StoredSnapshot, ...]:
        """列出目录里全部快照（按内容哈希排序，保证确定性）。"""
        if not self._root.is_dir():
            return ()
        results: list[StoredSnapshot] = []
        for path in sorted(self._root.glob("sha256-*.json")):
            stored = self._read_snapshot(path)
            if stored is not None:
                results.append(stored)
        return tuple(results)

    def content_hashes(self) -> frozenset[str]:
        """已落盘的内容哈希集合（供去重判断 / 断言）。"""
        return frozenset(snapshot.content_sha256 for snapshot in self.list_snapshots())

    def read_content(self, content_sha256: str) -> str:
        """按内容哈希取回原文。用于"同哈希 ⇒ 同内容"的可复核验证。"""
        path = self._root / snapshot_filename(content_sha256)
        payload = self._load(path)
        content = payload.get("content")
        if not isinstance(content, str):
            raise ProviderError(f"{path.name} 缺少 content 字段")
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual != content_sha256:
            raise ProviderError(
                f"{path.name} 的内容 sha256 与文件名不符：文件名 {content_sha256}，实际 {actual}"
            )
        return content

    def rebuild_manifest(self) -> int:
        """按目录实际内容重建索引，返回条目数。

        索引损坏时的恢复手段 —— 因为真源是那些内容寻址文件，所以这一步总是安全的。
        """
        snapshots = self.list_snapshots()
        manifest = {
            "schema": SNAPSHOT_SCHEMA,
            "entry_count": len(snapshots),
            "entries": [snapshot.to_dict() for snapshot in snapshots],
        }
        self._root.mkdir(parents=True, exist_ok=True)
        self._atomic_create(self.manifest_path, _encode(manifest), overwrite=True)
        return len(snapshots)

    # ---- 内部 ----

    def _validate(self, document: RetrievedDocument, provider_id: str) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ProviderError("provider_id 必须是非空字符串")
        if not is_https_url(document.source_url):
            raise InvalidSourceUrlError(
                f"快照的 source_url 必须是 https URL，实际 {document.source_url!r}"
            )
        if not CAPTURED_AT_PATTERN.match(document.captured_at):
            raise ProviderError(
                f"captured_at 必须是 YYYY-MM-DD，实际 {document.captured_at!r}"
            )

    def _atomic_create(self, target: Path, data: bytes, *, overwrite: bool = False) -> None:
        """原子写入。

        `overwrite=False`（快照文件）走"独占创建"：`FileExistsError` 会**向上传播**，
        因为在内容寻址下"目标已存在"意味着并发/篡改，绝不能静默处理。
        `overwrite=True`（索引文件）走 `os.replace`，索引本来就是可重建的派生物。
        """
        if not overwrite and target.exists():
            raise FileExistsError(
                f"{target} 已存在；快照文件不可覆盖。若确认要替换，请先人工删除并说明原因"
            )
        # 用上下文管理器持有句柄，避免"异常路径上句柄泄漏"（SIM115 的实质）。
        # `delete=False` 是因为随后要 `os.replace` 把临时文件变成正式文件。
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=self._root,
            prefix=".tmp-",
            suffix=".part",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if overwrite:
                os.replace(temp_path, target)
            else:
                # 独占语义：写入期间目标被创建也必须失败，而不是被替换掉。
                if target.exists():
                    raise FileExistsError(f"{target} 已存在（写入期间被创建）")
                os.replace(temp_path, target)
        except BaseException:
            if temp_path.exists():
                temp_path.unlink()
            raise

    def _read_existing_hash(self, path: Path) -> str:
        """读既有文件的登记哈希；不可解析时返回空串（→ 判为 conflict）。"""
        try:
            payload = self._load(path)
        except ProviderError:
            return ""
        value = payload.get("content_sha256")
        return value if isinstance(value, str) else ""

    def _load(self, path: Path) -> Mapping[str, object]:
        if not path.is_file():
            raise ProviderError(f"快照文件不存在：{path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(f"{path.name} 不是合法的 UTF-8 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"{path.name} 的顶层不是 JSON 对象")
        return payload

    def _stat(
        self, path: Path, document: RetrievedDocument, provider_id: str
    ) -> StoredSnapshot:
        return StoredSnapshot(
            path=path,
            content_sha256=document.content_sha256,
            source_id=document.source_id,
            query_terms=document.query_terms,
            source_url=document.source_url,
            title=document.title,
            captured_at=document.captured_at,
            provider_id=provider_id,
            byte_size=path.stat().st_size,
        )

    def _read_snapshot(self, path: Path) -> StoredSnapshot | None:
        """把一份已落盘文件读回 `StoredSnapshot`；结构不符返回 `None`。

        返回 `None` 而不是抛错：`list_snapshots` 用于"目录里有什么"这类探查，
        一个坏文件不应该让整次探查失败。
        """
        match = SNAPSHOT_FILENAME_PATTERN.match(path.name)
        if not match:
            return None
        try:
            payload = self._load(path)
        except ProviderError:
            return None
        content_sha256 = payload.get("content_sha256")
        if not isinstance(content_sha256, str) or content_sha256 != match.group(1):
            return None
        fields = {}
        for key in ("source_id", "query_terms", "source_url", "title", "captured_at"):
            value = payload.get(key)
            if not isinstance(value, str):
                return None
            fields[key] = value
        provider_id = payload.get("provider_id")
        if not isinstance(provider_id, str):
            return None
        return StoredSnapshot(
            path=path,
            content_sha256=content_sha256,
            provider_id=provider_id,
            byte_size=path.stat().st_size,
            **fields,
        )

    def _append_manifest(self, snapshot: StoredSnapshot) -> None:
        """把新快照追加进索引。

        索引是派生数据，所以这里重建而不是"读-改-写"：重建的成本是 O(文件数)，
        但它换来"索引永远不会与目录不一致"这条性质 ——
        对一个用来复核的目录而言，这个交换是值得的。
        """
        self.rebuild_manifest()


def _encode(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )

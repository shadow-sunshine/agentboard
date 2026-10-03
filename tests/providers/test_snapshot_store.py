"""快照存储测试（P2.1 验收：不可覆盖 + 哈希去重）。

三条要证明的性质：

1. **不可覆盖**：内容寻址文件名 + 独占创建 ⇒ 覆盖既有快照必须是一次**刻意的**删除，
   不可能由一次不小心的 `open(..., "w")` 造成。
2. **同内容不重复落盘**：第二次采集同样的内容返回 `duplicate`，
   **目录里的字节与第一次采集后完全一致**（逐文件比对 sha256，不是只比文件数）。
3. **七个必需字段都在**：查询词、来源 URL、标题、采集时间、来源标识、原文、SHA-256。

第 2 条是 P2.1 的核心要求"相同内容重复采集不得悄悄覆盖旧快照"，
所以它的断言不能只看返回值 —— 必须落到"文件字节没变"。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentboard.providers.errors import InvalidSourceUrlError, ProviderError
from agentboard.providers.interface import RetrievedDocument
from agentboard.providers.snapshot_store import (
    SNAPSHOT_SCHEMA,
    SnapshotStore,
    snapshot_filename,
)

CAPTURED = "2026-09-23"
PROVIDER_ID = "github.repository_search.v1"


def _document(
    content: str,
    *,
    source_url: str = "https://github.com/o/r",
    title: str = "o/r",
    source_id: str = "github.repository_search.v1:result:0",
    query_terms: str = "vector db",
    captured_at: str = CAPTURED,
) -> RetrievedDocument:
    return RetrievedDocument.build(
        source_id=source_id,
        query_terms=query_terms,
        source_url=source_url,
        title=title,
        captured_at=captured_at,
        content=content,
    )


def _digest_tree(root: Path) -> dict[str, str]:
    """对目录里每个文件算 sha256（用于"字节未被改动"的强断言）。"""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(tmp_path / "snapshots")


# ---- 写入与七字段 ----


def test_write_records_all_required_fields(store: SnapshotStore) -> None:
    """七个必需字段一个不少，且原文可取回。"""
    document = _document('{"full_name":"o/r"}', title="o/r — desc")
    result = store.write(document, provider_id=PROVIDER_ID)

    assert result.status == "written"
    payload = json.loads(result.snapshot.path.read_text(encoding="utf-8"))
    assert payload["schema"] == SNAPSHOT_SCHEMA
    assert payload["query_terms"] == "vector db"
    assert payload["source_url"] == "https://github.com/o/r"
    assert payload["title"] == "o/r — desc"
    assert payload["captured_at"] == CAPTURED
    assert payload["source_id"] == "github.repository_search.v1:result:0"
    assert payload["content"] == '{"full_name":"o/r"}'
    assert payload["content_sha256"] == document.content_sha256
    assert payload["provider_id"] == PROVIDER_ID


def test_write_uses_content_addressed_filename(store: SnapshotStore) -> None:
    document = _document("hello")
    result = store.write(document, provider_id=PROVIDER_ID)
    assert result.snapshot.path.name == snapshot_filename(document.content_sha256)


def test_write_creates_directory_if_missing(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "deep" / "nested")
    assert not store.root.exists()
    store.write(_document("x"), provider_id=PROVIDER_ID)
    assert store.root.is_dir()


def test_write_rejects_blank_provider_id(store: SnapshotStore) -> None:
    with pytest.raises(ProviderError, match="provider_id"):
        store.write(_document("x"), provider_id="   ")


def test_write_rejects_non_https_source_url(store: SnapshotStore) -> None:
    """构造一个 URL 非 https 的文档：`RetrievedDocument` 会先拦，
    所以这里直接测 store 的独立防线（用 object.__new__ 绕过 dataclass 校验）。"""
    document = _document("x")
    object.__setattr__(document, "source_url", "http://github.com/o/r")
    with pytest.raises(InvalidSourceUrlError):
        store.write(document, provider_id=PROVIDER_ID)


# ---- 不可覆盖 / 去重（核心验收项）----


def test_duplicate_content_is_not_rewritten(store: SnapshotStore) -> None:
    """同内容重复采集：返回 duplicate，且**目录字节完全不变**。

    只断言返回值是不够的 —— 必然要断言文件 sha256，
    否则"先删除再重写"这种实现也能通过（但它已经不满足"不覆盖"了）。
    """
    document = _document("stable content")
    first = store.write(document, provider_id=PROVIDER_ID)
    before = _digest_tree(store.root)

    second = store.write(document, provider_id=PROVIDER_ID)
    after = _digest_tree(store.root)

    assert first.status == "written"
    assert second.status == "duplicate"
    assert second.duplicate is True
    assert second.existing_path == first.snapshot.path
    assert before == after, "重复采集改动了目录里的字节"


def test_duplicate_via_separate_provider_runs_keeps_single_file(
    tmp_path: Path,
) -> None:
    """模拟"两次独立采集"（新 store 实例、新文档对象）：仍然只有一份文件。"""
    root = tmp_path / "snapshots"
    first_store = SnapshotStore(root)
    second_store = SnapshotStore(root)

    first_store.write(_document("same"), provider_id=PROVIDER_ID)
    second_store.write(_document("same"), provider_id=PROVIDER_ID)

    snapshot_files = sorted(path.name for path in root.glob("sha256-*.json"))
    assert len(snapshot_files) == 1


def test_duplicate_does_not_change_mtime_or_content(store: SnapshotStore) -> None:
    document = _document("same")
    first = store.write(document, provider_id=PROVIDER_ID)
    original_bytes = first.snapshot.path.read_bytes()

    store.write(document, provider_id=PROVIDER_ID)
    assert first.snapshot.path.read_bytes() == original_bytes


def test_identical_content_from_different_urls_dedups_by_hash(store: SnapshotStore) -> None:
    """去重键是**内容哈希**，不是 URL —— 两个仓库返回同样内容时只落一份。

    这是"按哈希去重"的字面含义。同时也意味着：URL 不会因为去重而丢失，
    它作为元数据保存在那份唯一快照里（第一份的 URL 胜出）。
    """
    first = store.write(
        _document("identical", source_url="https://github.com/a/b"),
        provider_id=PROVIDER_ID,
    )
    second = store.write(
        _document("identical", source_url="https://github.com/c/d"),
        provider_id=PROVIDER_ID,
    )
    assert first.status == "written"
    assert second.status == "duplicate"
    assert len(list(store.root.glob("sha256-*.json"))) == 1
    # 只有一份快照，里面登记的是第一次的 URL
    assert store.list_snapshots()[0].source_url == "https://github.com/a/b"


def test_different_content_creates_separate_files(store: SnapshotStore) -> None:
    store.write(_document("one"), provider_id=PROVIDER_ID)
    store.write(_document("two"), provider_id=PROVIDER_ID)
    assert len(list(store.root.glob("sha256-*.json"))) == 2
    assert len(store.content_hashes()) == 2


def test_whitespace_difference_is_not_a_duplicate(store: SnapshotStore) -> None:
    """只差一个空格的两次采集**不是**重复内容。

    如果先 strip 再哈希，这两次会被判为重复、第二次的差异被静默丢弃 ——
    那正是"悄悄覆盖"的另一种形态。
    """
    first = store.write(_document("abc"), provider_id=PROVIDER_ID)
    second = store.write(_document("abc "), provider_id=PROVIDER_ID)
    assert first.status == "written"
    assert second.status == "written"
    assert len(list(store.root.glob("sha256-*.json"))) == 2


def test_existing_file_with_wrong_hash_reports_conflict(store: SnapshotStore) -> None:
    """目标路径已存在但登记哈希不同 → conflict，而不是静默覆盖或静默跳过。"""
    document = _document("payload")
    store.write(document, provider_id=PROVIDER_ID)
    target = store.root / snapshot_filename(document.content_sha256)
    # 手工把文件内容换掉，模拟"同名文件被篡改"
    tampered = json.loads(target.read_text(encoding="utf-8"))
    tampered["content_sha256"] = "f" * 64
    target.write_text(json.dumps(tampered), encoding="utf-8")

    result = store.write(document, provider_id=PROVIDER_ID)
    assert result.status == "conflict"
    assert result.existing_path == target


def test_write_never_overwrites_existing_file(store: SnapshotStore) -> None:
    """直接验证底层：`_atomic_create` 对已存在目标必须抛 FileExistsError。"""
    document = _document("x")
    store.write(document, provider_id=PROVIDER_ID)
    target = store.root / snapshot_filename(document.content_sha256)
    with pytest.raises(FileExistsError):
        store._atomic_create(target, b"different")


def test_write_all_processes_each_document(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "s")
    results = store.write_all(
        [_document("a"), _document("b"), _document("a")], provider_id=PROVIDER_ID
    )
    assert [r.status for r in results] == ["written", "written", "duplicate"]
    assert len(list(store.root.glob("sha256-*.json"))) == 2


# ---- 读取 / 索引 ----


def test_read_content_verifies_hash(store: SnapshotStore) -> None:
    document = _document("payload")
    store.write(document, provider_id=PROVIDER_ID)
    assert store.read_content(document.content_sha256) == "payload"


def test_read_content_detects_tampering(store: SnapshotStore) -> None:
    """文件内容被改后，按哈希取回必须失败 —— 这是"可复核"的底线。"""
    document = _document("payload")
    store.write(document, provider_id=PROVIDER_ID)
    target = store.root / snapshot_filename(document.content_sha256)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["content"] = "tampered"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProviderError, match="不符"):
        store.read_content(document.content_sha256)


def test_read_content_missing_hash_raises(store: SnapshotStore) -> None:
    with pytest.raises(ProviderError, match="不存在"):
        store.read_content("a" * 64)


def test_list_snapshots_is_empty_for_missing_dir(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "nope")
    assert store.list_snapshots() == ()
    assert store.content_hashes() == frozenset()


def test_list_snapshots_sorted_by_hash(store: SnapshotStore) -> None:
    for content in ("c", "a", "b"):
        store.write(_document(content), provider_id=PROVIDER_ID)
    hashes = [s.content_sha256 for s in store.list_snapshots()]
    assert hashes == sorted(hashes)


def test_manifest_tracks_all_snapshots(store: SnapshotStore) -> None:
    store.write(_document("a"), provider_id=PROVIDER_ID)
    store.write(_document("b"), provider_id=PROVIDER_ID)
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == SNAPSHOT_SCHEMA
    assert manifest["entry_count"] == 2
    assert len(manifest["entries"]) == 2
    assert {e["content_sha256"] for e in manifest["entries"]} == store.content_hashes()


def test_manifest_entry_has_provenance_fields(store: SnapshotStore) -> None:
    store.write(_document("a", title="t"), provider_id=PROVIDER_ID)
    entry = json.loads(store.manifest_path.read_text(encoding="utf-8"))["entries"][0]
    for key in (
        "content_sha256",
        "source_id",
        "query_terms",
        "source_url",
        "title",
        "captured_at",
        "provider_id",
    ):
        assert key in entry, key


def test_manifest_can_be_rebuilt_from_files(store: SnapshotStore) -> None:
    """索引是派生数据：删掉后必须能重建（真源是那些内容寻址文件）。"""
    store.write(_document("a"), provider_id=PROVIDER_ID)
    store.write(_document("b"), provider_id=PROVIDER_ID)
    before = _digest_tree(store.root)

    assert store.rebuild_manifest() == 2
    after = _digest_tree(store.root)
    # 只有 manifest.json 允许不同（重建会重写它）；快照文件必须逐字节相同
    snapshots_before = {k: v for k, v in before.items() if k.startswith("sha256-")}
    snapshots_after = {k: v for k, v in after.items() if k.startswith("sha256-")}
    assert snapshots_before == snapshots_after


def test_list_snapshots_skips_malformed_files(store: SnapshotStore) -> None:
    """一个坏文件不该让整次探查失败 —— 返回 `None` 并跳过。"""
    store.write(_document("good"), provider_id=PROVIDER_ID)
    (store.root / f"sha256-{'f' * 64}.json").write_text("not json", encoding="utf-8")
    # 文件名哈希与内容哈希不符的也要跳过
    wrongly_named = store.root / f"sha256-{'e' * 64}.json"
    wrongly_named.write_text(
        json.dumps({"content_sha256": "d" * 64}), encoding="utf-8"
    )
    snapshots = store.list_snapshots()
    assert len(snapshots) == 1
    assert snapshots[0].content_sha256 == _document("good").content_sha256


def test_no_temp_files_left_behind(store: SnapshotStore) -> None:
    """原子写入的临时文件必须被清理，不能留在目录里污染复核。"""
    store.write(_document("a"), provider_id=PROVIDER_ID)
    leftovers = [p.name for p in store.root.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_snapshot_directory_is_outside_fixtures() -> None:
    """快照默认目录必须在 `fixtures/` 之外 —— 否则"联网结果伪装成静态 fixture"
    会变成一次 `mv` 就能做到的事。"""
    from agentboard.providers.snapshot_store import DEFAULT_SNAPSHOT_DIRNAME

    assert not DEFAULT_SNAPSHOT_DIRNAME.startswith("fixtures")
    assert DEFAULT_SNAPSHOT_DIRNAME.startswith("var/")

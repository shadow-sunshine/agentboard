"""外部验证证明工件：结构 / 序列化确定性 / 写入策略 W1–W4（P2.2-1）。

本文件测三件事，全部离线：

1. **结构**：schema / 字段闭集 / 类型与格式，任何一处不对即 `AttestationSchemaError`；
2. **序列化**：`sort_keys` + 列表排序 + 零时间字段 ⇒ 同一输入逐字节可复现，
   且**工具侧与库侧算出的字节完全相同**（两侧刻意各写一份实现，靠这条测试防止漂移）；
3. **写入策略**：W1 默认禁止覆盖、W2 内容相同幂等 / 不同则拒绝、
   W3 原子写入（不留半成品与临时文件）、W4 `incomplete` 不得静默变 `passed`。

`--write-attestation` 的全部目标路径都在 `tmp_path` 下 ——
本阶段**不创建**项目根的 `attestations/official.sources.v1.json`（那是 P2.2-4 的事）。
"""

from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from agentboard.research.attestation import (
    AttestationBinding,
    SourceVerificationAttestation,
    canonical_json_bytes,
    fixture_fingerprint,
    load_attestation,
    promotion_blocking_reason,
)
from agentboard.research.config import (
    ATTESTATION_SCHEMA,
    EXPECTED_VERIFIER_VERSION,
    REASON_ABSENT,
    REASON_INCOMPLETE,
    RESULT_INCOMPLETE,
    RESULT_PASSED,
    VALID_ATTESTATION_RESULTS,
    VERIFIER_ALGORITHM,
    VERIFIER_ID,
)
from agentboard.research.errors import AttestationSchemaError
from agentboard.research.sources import (
    is_promotable_source,
    load_official_sources,
    promotion_checks_for,
    snapshot_set_document_digests,
)

#: 工件的**闭集**字段（多一个少一个都必须报错）
EXPECTED_TOP_LEVEL_KEYS = {
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


@pytest.fixture
def tool(verify_tool: Any) -> Any:
    """`tools/verify_sources.py` 模块（由 conftest 的 session fixture 加载）。

    刻意不 `from conftest import ...`：`tests/` 与 `tests/research/` 下各有一个
    `conftest.py`，无 `__init__.py` 时两者会争同一个模块名 —— fixture 是唯一稳妥的通道。
    """
    return verify_tool


@pytest.fixture
def payload(synthetic_official_set: Any, attestation_generator: Any) -> dict[str, Any]:
    """一份**真工具生成**的合法工件内容（作为改坏用的基线）。"""
    return json.loads(attestation_generator().read_text(encoding="utf-8"))


# ---- 1. 结构 ----


def test_generated_payload_has_exactly_the_closed_key_set(payload: dict[str, Any]) -> None:
    assert set(payload) == EXPECTED_TOP_LEVEL_KEYS


def test_generated_payload_is_passed_and_covers_both_candidates(
    payload: dict[str, Any], synthetic_official_set: Any
) -> None:
    assert payload["overall_result"] == RESULT_PASSED
    assert payload["schema"] == ATTESTATION_SCHEMA
    assert payload["verifier"] == VERIFIER_ID
    assert payload["verifier_version"] == EXPECTED_VERIFIER_VERSION
    assert payload["algorithm"] == VERIFIER_ALGORITHM
    assert payload["snapshot_set_id"] == "official.2026-09-23"
    assert [item["candidate_id"] for item in payload["snapshot_files"]] == [
        "pgvector",
        "qdrant_local",
    ]
    assert sorted(item["source_id"] for item in payload["documents"]) == [
        "doc_pgvector_readme",
        "doc_qdrant_readme",
    ]


def test_tool_constants_match_library_constants(tool: Any) -> None:
    """工具与库的常量必须一致 —— 两侧各写一份实现，靠这条测试防止漂移。"""
    assert tool.ATTESTATION_SCHEMA == ATTESTATION_SCHEMA
    assert tool.VERIFIER_ID == VERIFIER_ID
    assert tool.VERIFIER_VERSION == EXPECTED_VERIFIER_VERSION
    assert tool.VERIFIER_ALGORITHM == VERIFIER_ALGORITHM
    assert tool.RESULT_PASSED == RESULT_PASSED
    assert tool.RESULT_INCOMPLETE == RESULT_INCOMPLETE


def test_tool_and_library_serialize_to_identical_bytes(
    tool: Any, payload: dict[str, Any]
) -> None:
    """同输入 ⇒ 工具侧与库侧**逐字节相同**。

    这是"审计者与被审计实现解耦"这个设计的代价所在：两份实现必须产出同一种规范形式。
    没有这条断言，解耦就会退化成"两个互相不认的 schema"。
    """
    library_bytes = canonical_json_bytes(payload)
    tool_bytes = tool.attestation_bytes(payload)
    assert library_bytes == tool_bytes
    assert library_bytes.endswith(b"\n")
    assert not library_bytes.startswith(b"\xef\xbb\xbf")  # 无 BOM
    assert b"\r\n" not in library_bytes  # LF


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda p: p.update({"schema": "source.verification.attestation.v9"}), "schema"),
        (lambda p: p.update({"snapshot_set_id": ""}), "snapshot_set_id"),
        (lambda p: p.update({"index_sha256": "zz"}), "index_sha256"),
        (lambda p: p.update({"verifier_version": 0}), "verifier_version"),
        (lambda p: p.update({"verifier_version": True}), "verifier_version"),
        (lambda p: p.update({"verifier_version": "1"}), "verifier_version"),
        (lambda p: p.update({"overall_result": "ok"}), "overall_result"),
        (lambda p: p.update({"snapshot_files": []}), "非空列表"),
        (lambda p: p.update({"extra_field": 1}), "未知字段"),
        (lambda p: p["snapshot_files"][0].update({"sha256": "nope"}), "sha256"),
        (lambda p: p["snapshot_files"][0].update({"path": "/abs/path.json"}), "相对路径"),
        (lambda p: p["documents"][0].update({"blob_sha": "short"}), "blob_sha"),
        (lambda p: p["documents"][0].update({"line_ranges": ["3", "3"]}), "重复行区间"),
        (lambda p: p["documents"][0].update({"line_ranges": ["0"]}), "line_ranges"),
        (lambda p: p["documents"][0].update({"result": "maybe"}), "result"),
    ],
)
def test_schema_violations_are_rejected(
    payload: dict[str, Any], mutate: Any, match: str
) -> None:
    broken = copy.deepcopy(payload)
    mutate(broken)
    with pytest.raises(AttestationSchemaError, match=match):
        SourceVerificationAttestation.from_dict(broken)


def test_duplicate_source_id_rejected(payload: dict[str, Any]) -> None:
    broken = copy.deepcopy(payload)
    broken["documents"].append(copy.deepcopy(broken["documents"][0]))
    with pytest.raises(AttestationSchemaError, match="重复 source_id"):
        SourceVerificationAttestation.from_dict(broken)


def test_duplicate_snapshot_path_rejected(payload: dict[str, Any]) -> None:
    broken = copy.deepcopy(payload)
    broken["snapshot_files"].append(copy.deepcopy(broken["snapshot_files"][0]))
    with pytest.raises(AttestationSchemaError, match="重复 path"):
        SourceVerificationAttestation.from_dict(broken)


def test_result_vocabulary_is_closed() -> None:
    assert VALID_ATTESTATION_RESULTS == frozenset({RESULT_PASSED, RESULT_INCOMPLETE})


# ---- 2. 序列化确定性与"零时间字段" ----


def test_serialization_is_deterministic(
    synthetic_official_set: Any, attestation_generator: Any, tool: Any
) -> None:
    """同输入两次生成 ⇒ 逐字节相同（否则工件无法被 sha256 固定）。"""
    first = attestation_generator()
    payload_a = first.read_bytes()
    second = attestation_generator(Path(first).with_name("second.json"))
    payload_b = Path(second).read_bytes()
    assert payload_a == payload_b


def test_list_ordering_is_independent_of_input_order(payload: dict[str, Any]) -> None:
    """`documents[]` / `snapshot_files[]` 必须排序后输出 ⇒ 与输入顺序无关。"""
    shuffled = copy.deepcopy(payload)
    shuffled["documents"].reverse()
    shuffled["snapshot_files"].reverse()
    assert SourceVerificationAttestation.from_dict(shuffled).to_dict() == (
        SourceVerificationAttestation.from_dict(payload).to_dict()
    )


def test_line_ranges_are_sorted_numerically(payload: dict[str, Any]) -> None:
    scrambled = copy.deepcopy(payload)
    scrambled["documents"][0]["line_ranges"] = ["129-130", "71", "142"]
    parsed = SourceVerificationAttestation.from_dict(scrambled)
    assert parsed.documents[0].line_ranges == ("71", "129-130", "142")


def test_attestation_contains_no_time_fields(payload: dict[str, Any]) -> None:
    """**零时间字段**：工件里不得出现任何形如日期的值。

    理由不是洁癖：一旦引入"核对时间"，同一输入在不同时刻会产出不同字节，
    "同一输入必得同一输出"就失效了 —— 这条纪律与 `captured_at` 不读时钟、
    `report_id` 内容寻址是同一条。
    """
    text = json.dumps(payload, ensure_ascii=False)
    assert "collected_at" not in text
    assert "generated_at" not in text
    assert "verified_at" not in text
    assert "timestamp" not in text
    assert isinstance(payload["verifier_version"], int)
    assert not isinstance(payload["verifier_version"], bool)


def test_fixture_fingerprint_matches_disk(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """`fixture_fingerprint` 算出的摘要必须与工件里登记的一致（同一份算法两处实现）。"""
    index_sha, files = fixture_fingerprint(synthetic_official_set.root)
    attestation = load_attestation(
        attestation_generator(), fixture_root=synthetic_official_set.root
    )
    assert index_sha == attestation.index_sha256
    assert [item.path for item in files] == [
        "sources/pgvector.json",
        "sources/qdrant_local.json",
    ]
    assert {item.path: item.sha256 for item in files} == {
        item.path: item.sha256 for item in attestation.snapshot_files
    }


# ---- 3. 写入策略 W1–W4 ----


def test_w1_w2_refuses_to_overwrite_different_content(
    synthetic_official_set: Any, attestation_generator: Any, tool: Any
) -> None:
    """W1 + W2：目标已存在且内容不同 ⇒ **拒绝**，且文件保持原样。"""
    target = attestation_generator()
    before = target.read_bytes()
    ok, message = tool.write_attestation(target, b'{"different": true}\n')
    assert ok is False
    assert "拒绝覆盖" in message
    assert target.read_bytes() == before


def test_w2_identical_content_is_idempotent_without_writing(
    synthetic_official_set: Any, attestation_generator: Any, tool: Any
) -> None:
    """W2：内容逐字节相同 ⇒ 幂等成功，**不写盘**（mtime 不变即可证明）。"""
    target = attestation_generator()
    stat_before = target.stat()
    ok, message = tool.write_attestation(target, target.read_bytes())
    assert ok is True
    assert "幂等成功" in message
    assert target.stat().st_mtime_ns == stat_before.st_mtime_ns


def test_w3_leaves_no_temp_file_behind(
    synthetic_official_set: Any, attestation_generator: Any, tool: Any
) -> None:
    """W3：原子写入不留临时文件（`.` 前缀 + `.part` 后缀的那一类）。"""
    target = attestation_generator()
    leftovers = [
        path
        for path in target.parent.iterdir()
        if path.name.startswith(".tmp-") or path.name.endswith(".part")
    ]
    assert leftovers == []


def test_w3_creates_parent_directory_and_is_exclusive(
    tmp_path: Path, synthetic_official_set: Any, tool: Any
) -> None:
    nested = tmp_path / "deep" / "nested" / "a.json"
    created, _ = tool.write_attestation(nested, b"x\n")
    assert created is True and nested.read_bytes() == b"x\n"
    # 再写不同内容：绝不覆盖
    overwritten, refused_message = tool.write_attestation(nested, b"y\n")
    assert overwritten is False
    assert "拒绝覆盖" in refused_message
    assert nested.read_bytes() == b"x\n"


def test_w4_incomplete_cannot_become_passed(
    synthetic_official_set: Any, tmp_path: Path, tool: Any
) -> None:
    """W4：没有原件时产物只能是 `incomplete`，且**无法**被静默改写成 `passed`。

    机制而不是口令：内容完全由 `(fixture 字节, 原件集合, verifier_version, algorithm)`
    决定，所以 `passed` 只能被核对出来。手工把 `overall_result` 改成 `passed` 时，
    它已不再是"核对结论"，因此也就无法通过文件级绑定与声明对齐（见升格闸门用例）。
    """
    empty_raw = tmp_path / "empty-raw"
    empty_raw.mkdir()
    result = tool.verify_sources(synthetic_official_set.root, empty_raw)
    assert result.problems == []
    assert result.missing, "原件目录为空时应当全部报缺失"
    payload = tool.build_attestation_payload(synthetic_official_set.root, result)
    assert payload["overall_result"] == RESULT_INCOMPLETE
    assert {item["result"] for item in payload["documents"]} == {RESULT_INCOMPLETE}

    # 手工改成 passed 也没用：此时它与"声明级对齐"冲突（documents[].result 仍是 incomplete）
    forged = copy.deepcopy(payload)
    forged["overall_result"] = RESULT_PASSED
    attestation = SourceVerificationAttestation.from_dict(forged)
    assert attestation.overall_result == RESULT_PASSED  # 解析不拦（结构合法）
    # 但升格侧会因 `attestation.binding_reason is None`（从未绑定）而 fail-closed
    assert attestation.binding_reason is None


def test_result_overall_result_refuses_to_hide_problems(tool: Any, tmp_path: Path) -> None:
    """`VerificationResult.overall_result` 在存在不符项时**拒绝**给出结论。"""
    result = tool.VerificationResult()
    result.problems.append("doc_x: raw_sha256 不符")
    with pytest.raises(ValueError, match="不得生成工件"):
        _ = result.overall_result


def test_tool_refuses_to_write_when_problems_exist(
    synthetic_official_set: Any, tmp_path: Path, tool: Any
) -> None:
    """有"不符"时**拒绝生成**工件（不为失败的核对留下看似正式的产物）。"""
    fake_raw = tmp_path / "fake-raw"
    fake_raw.mkdir()
    (fake_raw / "README.md").write_text("这不是那份原件", encoding="utf-8", newline="\n")
    target = tmp_path / "must-not-exist.json"
    code = tool.main(
        [
            "--root",
            str(synthetic_official_set.root),
            "--raw-dir",
            str(fake_raw),
            "--allow-missing",
            "--write-attestation",
            str(target),
        ]
    )
    assert code == 1
    assert not target.exists()


def test_tool_cli_writes_incomplete_with_allow_missing(
    synthetic_official_set: Any, tmp_path: Path, tool: Any
) -> None:
    """有缺失 + `--allow-missing` ⇒ 生成 `incomplete` 工件，退出码 0。"""
    empty_raw = tmp_path / "cli-empty-raw"
    empty_raw.mkdir()
    target = tmp_path / "incomplete.json"
    code = tool.main(
        [
            "--root",
            str(synthetic_official_set.root),
            "--raw-dir",
            str(empty_raw),
            "--allow-missing",
            "--write-attestation",
            str(target),
        ]
    )
    assert code == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["overall_result"] == RESULT_INCOMPLETE


def test_tool_cli_refuses_missing_without_allow_missing(
    synthetic_official_set: Any, tmp_path: Path, tool: Any
) -> None:
    """有缺失但未加 `--allow-missing` ⇒ 退出 1 且**不生成**工件。"""
    empty_raw = tmp_path / "cli-empty-raw2"
    empty_raw.mkdir()
    target = tmp_path / "should-not-exist.json"
    code = tool.main(
        [
            "--root",
            str(synthetic_official_set.root),
            "--raw-dir",
            str(empty_raw),
            "--write-attestation",
            str(target),
        ]
    )
    assert code == 1
    assert not target.exists()


def test_tool_cli_second_run_is_idempotent(
    synthetic_official_set: Any, tmp_path: Path, tool: Any
) -> None:
    """连跑两次：第二次内容相同 ⇒ 退出 0 且不覆盖。"""
    target = tmp_path / "twice.json"
    argv = [
        "--root",
        str(synthetic_official_set.root),
        "--raw-dir",
        str(synthetic_official_set.raw_dir),
        "--write-attestation",
        str(target),
    ]
    assert tool.main(argv) == 0
    first = target.read_bytes()
    mtime = target.stat().st_mtime_ns
    assert tool.main(argv) == 0
    assert target.read_bytes() == first
    assert target.stat().st_mtime_ns == mtime


def test_tool_refuses_when_snapshot_set_missing(
    tmp_path: Path, synthetic_official_set: Any, tool: Any
) -> None:
    """INDEX.json 缺 `snapshot_set` ⇒ 拒绝生成（工件的集合标识必须可对齐）。"""
    index = json.loads(synthetic_official_set.index_path.read_text(encoding="utf-8"))
    index.pop("snapshot_set")
    synthetic_official_set.index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    target = tmp_path / "no-set.json"
    code = tool.main(
        [
            "--root",
            str(synthetic_official_set.root),
            "--raw-dir",
            str(synthetic_official_set.raw_dir),
            "--write-attestation",
            str(target),
        ]
    )
    assert code == 1
    assert not target.exists()


def test_w3_link_unavailable_fails_closed_and_cleans_temp(
    tmp_path: Path, tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3 复核修正：`os.link` 不可用时必须**明确失败**，不得退化成非原子写入。

    旧实现的退化路径（`O_EXCL` 直写）**返回成功**，而测试把它当成功 ——
    于是"原子写入"这条已批准的硬约束被实现悄悄降级，调用方拿到的是更弱的保证。
    `O_CREAT | O_EXCL` 只有排他性、**没有中断原子性**：进程在写完 `fsync` 之前
    被强杀会留下半写文件，而它看起来像一份"已生成的工件"，无法与"被篡改"区分。
    原子性无法在原语层面提供时，唯一诚实的动作是**拒绝**。

    三重断言：①返回失败 ②目标不存在（没有半成品）③无临时文件残留。

    注：不 monkeypatch `os.replace` —— 修正后写入路径**根本不用**它
    （用 `os.replace` 就意味着"检查与替换之间存在覆盖窗口"，见 `write_attestation` 的文档）。
    """
    import os as _os

    target = tmp_path / "no-link.json"

    def unsupported(*args: Any, **kwargs: Any) -> None:
        raise OSError("该文件系统不支持硬链接")

    monkeypatch.setattr(_os, "link", unsupported)

    ok, message = tool.write_attestation(target, b'{"a": 1}\n')

    assert ok is False, "os.link 不可用 ⇒ 必须 fail-closed，不得降级为非原子写入"
    assert "os.link" in message and "不可用" in message
    assert "O_EXCL" in message, "失败说明里必须点明为何**不**用 O_EXCL 顶替"
    assert not target.exists(), "fail-closed 路径不得留下目标文件"
    leftovers = sorted(path.name for path in tmp_path.iterdir())
    assert leftovers == [], f"异常路径必须清理临时文件，实际残留：{leftovers}"


def test_write_path_has_no_non_atomic_fallback(tmp_path: Path, tool: Any) -> None:
    """**回归护栏**：工具源码里不得存在 `O_EXCL` / `O_CREAT` 直写路径。

    W3 修正的根因不是"某次调用写错了"，而是"存在一条**非原子的成功路径**"。
    因此把它机械化成结构断言：只要 `O_CREAT` / `O_EXCL` 不出现在可执行代码里，
    就不可能偷偷重建那条退化路径。

    用 AST 而不是字符串匹配：文档字符串里**必须**能解释"为什么不用 O_EXCL"
    （那是本模块最有价值的一段注释之一），把解释也算违规会逼人删掉它。
    """
    import ast

    source = inspect.getsource(tool)
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in {"O_CREAT", "O_EXCL"}:
            offenders.append(f"os.{node.attr} @ line {node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in {"O_CREAT", "O_EXCL"}:
                    offenders.append(f"from os import {alias.name}")
    assert offenders == [], (
        f"写入路径不得使用 O_CREAT/O_EXCL 直写（仅排他、非原子）：{offenders}；"
        f"原子排他发布用 os.link，不可用时 fail-closed"
    )
    # 正控制：护栏必须真的抓得到（否则它是空转的）
    positive = ast.parse("import os\nfd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n")
    found = [
        node.attr
        for node in ast.walk(positive)
        if isinstance(node, ast.Attribute) and node.attr in {"O_CREAT", "O_EXCL"}
    ]
    assert found == ["O_CREAT", "O_EXCL"]


def test_tool_cli_exits_nonzero_when_link_unavailable(
    synthetic_official_set: Any, tmp_path: Path, tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 层：`os.link` 不可用 ⇒ **退出码非 0**，且不留下任何文件。

    复核意见明确要求"返回非零" —— 这条把库层的 `(False, ...)` 与进程退出码
    串起来验证，避免"库层拒绝了、CLI 却报了成功"这类断层。
    """
    import os as _os

    # 目标放在专属子目录：本测试与合成语料共用 tmp_path，直接 iterdir() 会看到语料目录
    out_dir = tmp_path / "cli-out"
    target = out_dir / "cli-no-link.json"

    def unsupported(*args: Any, **kwargs: Any) -> None:
        raise OSError("该文件系统不支持硬链接")

    monkeypatch.setattr(_os, "link", unsupported)
    code = tool.main(
        [
            "--root",
            str(synthetic_official_set.root),
            "--raw-dir",
            str(synthetic_official_set.raw_dir),
            "--write-attestation",
            str(target),
        ]
    )
    assert code == 1
    assert not target.exists()
    assert sorted(path.name for path in out_dir.iterdir()) == [], "不得残留临时文件"


# ---- P1-2 复核修正：禁止覆盖必须是「发布动作本身排他」 ----


def test_write_path_never_uses_replace(tmp_path: Path, tool: Any) -> None:
    """**回归护栏**：写入路径里不得出现 `os.replace` 的**可执行调用**。

    `os.replace` 是**无条件覆盖**语义 —— 只要它出现在"目标可能不存在"的路径上，
    就必然存在"检查通过 → 别人创建 → replace 静默覆盖"的窗口。
    因此这条断言的不是风格，而是 P1-2 的根因：
    **机械地把禁止覆盖变成"不许出现覆盖原语"。**

    用 AST 而不是字符串匹配：文档字符串里**必须**能解释"为什么不用 os.replace"
    （那是本模块最有价值的一段注释），把解释也算违规会逼人删掉它 —— 本末倒置。
    """
    import ast

    source = inspect.getsource(tool)
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "replace":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "os":
                offenders.append(f"os.replace @ line {node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "replace":
                    offenders.append("from os import replace")
    assert offenders == [], (
        f"写入路径不得调用 os.replace（无条件覆盖语义）：{offenders}；"
        f"原子排他发布用 os.link，不可用时 fail-closed（不得用 O_EXCL 顶替）"
    )
    # 正控制：护栏必须真的抓得到（否则它是空转的）
    positive = ast.parse("import os\nos.replace(a, b)\n")
    found = [
        node.attr
        for node in ast.walk(positive)
        if isinstance(node, ast.Attribute) and node.attr == "replace"
    ]
    assert found == ["replace"]


def test_p1_2_race_target_created_at_publish_time_is_not_overwritten(
    tmp_path: Path, tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-2 竞态负向测试：目标在"最后检查之后、发布之前"出现 → **绝不覆盖**。

    这是复核意见要求的用例，也是旧实现真正出错的路径（实测旧实现会覆盖并返回 True）。

    构造方式：monkeypatch `os.link` —— 它正是发布动作本身。让它在被调用时
    先创建目标文件（模拟另一个进程抢先），再抛 `FileExistsError`（真实 `os.link`
    在目标已存在时就是这个行为）。于是"谁先到"由发布动作判定，而不是由事先检查判定。

    三重断言：①不覆盖（竞态内容原样保留）②返回拒绝 ③无临时文件残留。
    另外把 `os.replace` 换成炸弹：只要实现回退去用覆盖原语，本测试会立刻炸。
    """
    import os as _os

    racing_content = b'{"who": "racing-process"}\n'
    ours = b'{"ours": true}\n'
    target = tmp_path / "raced.json"

    def racing_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        Path(dst).write_bytes(racing_content)  # 别人抢先创建
        raise FileExistsError(str(dst))  # 真实的 os.link 在此情形也是这个异常

    def replace_bomb(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("禁止覆盖：写入路径不得调用 os.replace")

    monkeypatch.setattr(_os, "link", racing_link)
    monkeypatch.setattr(_os, "replace", replace_bomb)

    ok, message = tool.write_attestation(target, ours)

    assert ok is False, "目标已被抢先创建且内容不同 ⇒ 必须拒绝"
    assert "拒绝覆盖" in message
    assert target.read_bytes() == racing_content, "竞态进程写入的内容必须原样保留"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["raced.json"], "不得残留临时文件"


def test_p1_2_race_with_identical_content_is_idempotent(
    tmp_path: Path, tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """竞态的另一种结局：抢先者写入了**逐字节相同**的内容 ⇒ 幂等成功，不改动它。"""
    import os as _os

    ours = b'{"same": true}\n'
    target = tmp_path / "raced-same.json"

    def racing_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        Path(dst).write_bytes(ours)
        raise FileExistsError(str(dst))

    monkeypatch.setattr(_os, "link", racing_link)
    ok, message = tool.write_attestation(target, ours)
    assert ok is True
    assert "幂等成功" in message
    assert target.read_bytes() == ours


def test_p1_2_link_cross_device_is_fail_closed_not_degraded(
    tmp_path: Path, tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实触发场景：`os.link` 跨设备（`EXDEV`）⇒ **拒绝**，不是"降级成功"。

    这是 W3 复核修正前那条 `test_p1_2_publish_falls_back_to_exclusive_create` 的
    替代品。旧测试断言 `ok is True` 并检查告警文案 —— 等于把"实现自行降级"固化成了期望行为。
    原子写入是硬约束，所以这里的期望反过来：**失败**，且不留任何文件。

    用 `EXDEV` 而不是泛化 `OSError`：这是 `os.link` 在真实世界里最常见的失败原因
    （临时目录与目标目录不在同一文件系统），必须走同一条 fail-closed 分支。
    """
    import os as _os

    def cross_device(*args: Any, **kwargs: Any) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(_os, "link", cross_device)
    target = tmp_path / "cross-device.json"

    ok, message = tool.write_attestation(target, b'{"fb": 1}\n')

    assert ok is False, "跨设备是 fail-closed 的触发条件之一，不得降级"
    assert "Invalid cross-device link" in message, "失败说明必须带上底层原因，便于定位"
    assert not target.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [], "不得残留临时文件"


# ---- P1-1 复核修正：绑定状态不能由公开构造器设置 ----


def test_p1_1_direct_construction_cannot_set_binding(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """P1-1 回归测试：**直接构造并传 `binding_reason=` 必须失败**。

    这是复核意见给出的复现路径：旧实现里 `binding_reason` 是公开字段，
    于是 `SourceVerificationAttestation(..., binding_reason="")` 直接绕过文件绑定、
    让 `is_promotable_source()` 返回 `True`（已实测复现）。

    修复方式不是"加一条 if 判断"，而是**把该字段移出构造器签名**
    （`field(init=False)`）—— 签名层面就不接受这个参数。
    """
    payload = json.loads(attestation_generator().read_text(encoding="utf-8"))
    parsed = SourceVerificationAttestation.from_dict(payload)

    with pytest.raises(TypeError, match="binding_reason"):
        SourceVerificationAttestation(
            schema=parsed.schema,
            snapshot_set_id=parsed.snapshot_set_id,
            index_sha256=parsed.index_sha256,
            snapshot_files=parsed.snapshot_files,
            verifier=parsed.verifier,
            verifier_version=parsed.verifier_version,
            algorithm=parsed.algorithm,
            documents=parsed.documents,
            overall_result=parsed.overall_result,
            binding_reason="",  # ← 旧实现接受它，新实现必须 TypeError
        )
    with pytest.raises(TypeError, match="binding"):
        SourceVerificationAttestation(
            schema=parsed.schema,
            snapshot_set_id=parsed.snapshot_set_id,
            index_sha256=parsed.index_sha256,
            snapshot_files=parsed.snapshot_files,
            verifier=parsed.verifier,
            verifier_version=parsed.verifier_version,
            algorithm=parsed.algorithm,
            documents=parsed.documents,
            overall_result=parsed.overall_result,
            binding=AttestationBinding(reason=""),  # ← 换成新字段名也一样
        )


def test_p1_1_binding_is_not_in_the_constructor_signature() -> None:
    """机制性断言（比行为断言更直接）：`binding` 必须 `init=False`。

    只要这条成立，"手拼一份字段齐全的证明"在**签名层面**就不存在 ——
    不依赖任何运行时分支，因此不会因为后续重构而悄悄失效。
    """
    import dataclasses

    binding_field = next(
        item for item in dataclasses.fields(SourceVerificationAttestation)
        if item.name == "binding"
    )
    assert binding_field.init is False
    assert binding_field.compare is False  # 相等性只看内容，不看绑定状态
    assert "binding" not in inspect.signature(SourceVerificationAttestation).parameters


def test_p1_1_parsed_attestation_is_never_promotable(
    synthetic_official_set: Any, attestation_generator: Any
) -> None:
    """三条路径都必须不可升格，只有 `load_attestation()` 的产物可以。

    | 取得方式 | binding | 可升格 |
    |---|---|---|
    | `from_dict(payload)` | `None` | ❌ |
    | 直接构造（同字段） | `None` | ❌ |
    | `load_attestation(path, fixture_root=...)` | `AttestationBinding` | ✅ |
    """
    from agentboard.research.attestation import AttestationBinding as BindingType

    path = attestation_generator()
    payload = json.loads(path.read_text(encoding="utf-8"))
    inputs = _promotion_inputs(synthetic_official_set.root)

    parsed = SourceVerificationAttestation.from_dict(payload)
    assert parsed.binding is None and parsed.is_bound is False
    assert is_promotable_source(attestation=parsed, **inputs) is False

    rebuilt = SourceVerificationAttestation(
        schema=parsed.schema,
        snapshot_set_id=parsed.snapshot_set_id,
        index_sha256=parsed.index_sha256,
        snapshot_files=parsed.snapshot_files,
        verifier=parsed.verifier,
        verifier_version=parsed.verifier_version,
        algorithm=parsed.algorithm,
        documents=parsed.documents,
        overall_result=parsed.overall_result,
    )
    assert rebuilt.binding is None
    assert is_promotable_source(attestation=rebuilt, **inputs) is False

    loaded = load_attestation(path, fixture_root=synthetic_official_set.root)
    assert isinstance(loaded.binding, BindingType) and loaded.is_bound is True
    assert loaded.binding_reason == ""
    assert is_promotable_source(attestation=loaded, **inputs) is True


def test_p1_1_promotion_blocking_reason_rejects_duck_typed_lookalike(
    synthetic_official_set: Any,
) -> None:
    """鸭子类型的"看起来像证明"的对象也必须被拒（防御性类型检查）。"""
    inputs = _promotion_inputs(synthetic_official_set.root)

    class Lookalike:
        binding = None
        binding_reason = ""
        verifier_version = EXPECTED_VERIFIER_VERSION
        snapshot_set_id = "official.2026-09-23"
        overall_result = RESULT_PASSED

    with pytest.raises(TypeError, match="SourceVerificationAttestation"):
        promotion_blocking_reason(
            Lookalike(),  # type: ignore[arg-type]
            expected_snapshot_set_id=inputs["expected_snapshot_set_id"],
            declared_documents=inputs["declared_documents"],
        )


def _promotion_inputs(root: Path) -> dict[str, Any]:
    """本文件内需要的最小升格输入（复用 sources 的正式入口，不手算）。"""
    sources = load_official_sources(root)
    return {
        "source_kind": sources.snapshot("pgvector").source_kind,
        "has_provenance": True,
        "checks": promotion_checks_for(sources, "pgvector", fixture_root=root),
        "expected_snapshot_set_id": sources.catalog.snapshot_set,
        "declared_documents": snapshot_set_document_digests(sources),
    }


def test_w4_verify_only_mode_writes_nothing(
    synthetic_official_set: Any, tmp_path: Path, tool: Any
) -> None:
    """不传 `--write-attestation` ⇒ 只核对、**不写任何东西**（opt-in 语义）。"""
    before = sorted(path.name for path in synthetic_official_set.root.rglob("*"))
    code = tool.main(
        [
            "--root",
            str(synthetic_official_set.root),
            "--raw-dir",
            str(synthetic_official_set.raw_dir),
        ]
    )
    assert code == 0
    assert sorted(path.name for path in synthetic_official_set.root.rglob("*")) == before
    assert not (tmp_path / "attestations").exists()


def test_missing_attestation_file_is_reported_not_silently_absent(tmp_path: Path) -> None:
    """证明文件不存在 ⇒ 读取即报错（而不是悄悄当作"没有证明"）。

    "没有证明"（`absent`）是一个**明确的状态**，必须与"路径写错/文件丢了"区分开：
    后者是操作错误，静默降级成 `absent` 会让人以为只是还没做核对。
    """
    from agentboard.research.errors import SnapshotSchemaError

    with pytest.raises(SnapshotSchemaError, match="找不到文件"):
        load_attestation(tmp_path / "nope.json", fixture_root=tmp_path)
    assert REASON_ABSENT == "absent"
    assert REASON_INCOMPLETE == "incomplete"

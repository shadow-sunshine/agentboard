"""离线来源复核工具（`tools/verify_sources.py`）的测试。

纯离线：真实原件的逐字节核对挂在环境变量 `AGENTBOARD_SOURCE_RAW_DIR` 上 ——
设了就跑全量核对，没设就跳过（默认门禁不需要网络，也不需要原件在仓库里）。

其余用例用合成数据把工具的正/负控制钉死：sha256 篡改、git blob 篡改、
行数漂移、摘录对不上、行号越界，都必须被报成问题；缺失与问题不能互相冒充。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFY_PATH = PROJECT_ROOT / "tools" / "verify_sources.py"
OFFICIAL_ROOT = PROJECT_ROOT / "fixtures" / "official"

RAW_DIR_ENV = "AGENTBOARD_SOURCE_RAW_DIR"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agentboard_verify_sources", VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先注册到 sys.modules：工具里有 @dataclass，dataclasses 会用它解析
    # 字符串注解（cls.__module__ -> sys.modules[...]），未注册时会 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify = _load_tool()


# ---- 纯函数 ----


def test_count_lines_ignores_trailing_newline() -> None:
    assert verify.count_lines("a\nb\nc") == 3
    assert verify.count_lines("a\nb\nc\n") == 3
    assert verify.count_lines("a") == 1


def test_git_blob_sha1_matches_known_git_object_ids() -> None:
    """常量取自 git 协议本身：空 blob 与 "hello\\n" 的对象 id 是公开可核对的。"""
    assert verify.git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert verify.git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_slice_lines_single_and_range() -> None:
    text = "L1\nL2\nL3\nL4\n"
    assert verify.slice_lines(text, "1") == "L1"
    assert verify.slice_lines(text, "2-3") == "L2\nL3"
    assert verify.slice_lines(text, "4") == "L4"


@pytest.mark.parametrize("bad", ["0", "5", "3-2", "abc", "1-"])
def test_slice_lines_rejects_out_of_range_or_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        verify.slice_lines("L1\nL2\nL3\nL4\n", bad)


def _document(raw: bytes, *, cited: list[tuple[str, str, str]] | None = None) -> list[str]:
    return verify.check_document(
        source_id="doc_x",
        raw_sha256=verify.sha256_bytes(raw),
        blob_sha=verify.git_blob_sha1(raw),
        line_count=verify.count_lines(raw.decode("utf-8")),
        raw_bytes=raw,
        cited=cited if cited is not None else [("s_one", "1", raw.decode("utf-8").split("\n")[0])],
    )


def test_check_document_passes_on_consistent_input() -> None:
    assert _document(b"alpha\nbeta\n") == []


def test_check_document_flags_sha_mismatch() -> None:
    raw = b"alpha\nbeta\n"
    problems = verify.check_document(
        source_id="doc_x",
        raw_sha256="0" * 64,
        blob_sha=verify.git_blob_sha1(raw),
        line_count=2,
        raw_bytes=raw,
        cited=[],
    )
    assert len(problems) == 1
    assert "raw_sha256 不符" in problems[0]
    assert "固定到 commit" in problems[0]


def test_check_document_flags_blob_mismatch() -> None:
    raw = b"alpha\nbeta\n"
    problems = verify.check_document(
        source_id="doc_x",
        raw_sha256=verify.sha256_bytes(raw),
        blob_sha="0" * 40,
        line_count=2,
        raw_bytes=raw,
        cited=[],
    )
    assert len(problems) == 1
    assert "blob_sha 不符" in problems[0]


def test_check_document_flags_line_count_drift() -> None:
    problems = _document_with_line_count(b"alpha\nbeta\n")
    assert any("行数不符" in item for item in problems)


def _document_with_line_count(raw: bytes) -> list[str]:
    return verify.check_document(
        source_id="doc_x",
        raw_sha256=verify.sha256_bytes(raw),
        blob_sha=verify.git_blob_sha1(raw),
        line_count=99,
        raw_bytes=raw,
        cited=[],
    )


def test_check_document_flags_excerpt_drift() -> None:
    raw = b"alpha\nbeta\n"
    problems = verify.check_document(
        source_id="doc_x",
        raw_sha256=verify.sha256_bytes(raw),
        blob_sha=verify.git_blob_sha1(raw),
        line_count=2,
        raw_bytes=raw,
        cited=[("s_one", "1", "alpha ")],
    )
    assert len(problems) == 1
    assert "与快照摘录不一致" in problems[0]


def test_check_document_flags_line_range_beyond_file() -> None:
    raw = b"alpha\nbeta\n"
    problems = verify.check_document(
        source_id="doc_x",
        raw_sha256=verify.sha256_bytes(raw),
        blob_sha=verify.git_blob_sha1(raw),
        line_count=2,
        raw_bytes=raw,
        cited=[("s_one", "99", "alpha")],
    )
    assert len(problems) == 1
    assert "超出文件行数" in problems[0]


# ---- 与真实快照的接口 ----


def test_collect_citations_reads_real_snapshot() -> None:
    payload: dict[str, Any] = json.loads(
        (OFFICIAL_ROOT / "sources" / "pgvector.json").read_text(encoding="utf-8")
    )
    cited = verify.collect_citations(payload)
    assert set(cited) == {"doc_pgvector_readme"}
    assert len(cited["doc_pgvector_readme"]) == 3
    for section_id, line_range, text in cited["doc_pgvector_readme"]:
        assert section_id.startswith("s_pg_")
        assert line_range.isdigit()
        assert text


def test_index_raw_dir_matches_by_content_and_name(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    target = tmp_path / "nested" / "README.md"
    target.write_bytes(b"alpha\nbeta\n")
    by_sha, by_name = verify.index_raw_dir(tmp_path)
    assert by_sha[verify.sha256_bytes(b"alpha\nbeta\n")] == target
    assert by_name["README.md"] == target


def test_real_corpus_reports_problem_when_raw_content_differs(tmp_path: Path) -> None:
    """端到端负向控制：伪造一份同名但内容不同的"原件"，必须报成问题而非缺失。"""
    fake = tmp_path / "README.md"
    fake.write_text("这不是那份 README", encoding="utf-8", newline="\n")
    result = verify.verify_sources(OFFICIAL_ROOT, tmp_path)
    assert result.problems
    assert not result.passed(allow_missing=True)
    assert any("raw_sha256 不符" in problem for problem in result.problems)
    assert any(problem.startswith("doc_pgvector_readme") for problem in result.problems)


def test_real_corpus_reports_missing_when_nothing_provided(tmp_path: Path) -> None:
    result = verify.verify_sources(OFFICIAL_ROOT, tmp_path)
    assert result.problems == []
    assert sorted(result.missing) == [
        "doc_milvus_standalone_compose",
        "doc_milvus_standalone_docker",
        "doc_pgvector_readme",
        "doc_qdrant_installation",
    ]
    assert not result.passed()
    assert result.passed(allow_missing=True)  # 只缺文件、没有问题时，--allow-missing 可放行


def test_result_to_dict_is_json_serializable(tmp_path: Path) -> None:
    result = verify.verify_sources(OFFICIAL_ROOT, tmp_path)
    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert payload["documents_missing"] == 4
    assert payload["passed"] is False
    assert result.to_dict(allow_missing=True)["passed"] is True


def test_to_dict_shape_is_unchanged_by_attestation_support(tmp_path: Path) -> None:
    """P2.2-1 只**新增** attestation 能力，既有 JSON 输出形状必须一字不变。

    `VerificationResult` 内部多了 `line_ranges` / `declarations` 两个字段
    （供工件使用），它们**刻意不进** `to_dict()` —— 否则审计输出的形状会被悄悄改掉，
    任何依赖它的下游都会在不知不觉中断掉。
    """
    result = verify.verify_sources(OFFICIAL_ROOT, tmp_path)
    payload = result.to_dict()
    assert set(payload) == {
        "passed",
        "documents_verified",
        "sections_checked",
        "documents_missing",
        "problems",
        "detail",
    }
    assert set(payload["detail"]) == {"verified", "missing", "problem_list"}
    # 内部字段确实存在，但不在输出里
    assert "line_ranges" not in payload
    assert "declarations" not in payload


def test_main_without_write_attestation_creates_no_file(tmp_path: Path) -> None:
    """不传 `--write-attestation` ⇒ 工具只核对，不落任何盘。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    before = sorted(path.name for path in tmp_path.rglob("*"))
    code = verify.main(["--raw-dir", str(raw_dir), "--allow-missing", "--json"])
    assert code == 0
    assert sorted(path.name for path in tmp_path.rglob("*")) == before


def test_write_attestation_refused_when_problems_exist(tmp_path: Path) -> None:
    """有"不符"时**拒绝生成**工件（不为失败的核对留下看似正式的产物）。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "README.md").write_text("这不是那份 README", encoding="utf-8", newline="\n")
    target = tmp_path / "refused.json"
    code = verify.main(
        [
            "--raw-dir",
            str(raw_dir),
            "--allow-missing",
            "--write-attestation",
            str(target),
        ]
    )
    assert code == 1
    assert not target.exists()


@pytest.mark.skipif(
    not os.environ.get(RAW_DIR_ENV),
    reason=f"未设置 {RAW_DIR_ENV}：跳过真实原件的逐字节核对（需要先下载 raw_url）",
)
def test_real_corpus_verifies_against_actual_raw_files() -> None:
    """设了 `AGENTBOARD_SOURCE_RAW_DIR` 才跑：4 份来源的 sha256 / blob / 行数 / 8 条摘录全对上。"""
    raw_dir = Path(os.environ[RAW_DIR_ENV])
    result = verify.verify_sources(OFFICIAL_ROOT, raw_dir)
    assert result.problems == []
    assert result.missing == []
    assert result.passed()
    assert len(result.verified) == 4
    assert sum(item[2] for item in result.verified) == 8

"""官方链路的评测门禁测试（P2.2-4）。

四块内容，对应 P2.2_PLAN §P2.2-4 的测试用例表：

1. **篡改检测 ①–⑤**：改原件 / 改行号 / 改 raw_sha256 / 改 commit_sha / 换整份快照 ——
   全部在 tmp 克隆上做，`fixtures/**` 零改动（G1）；
2. **升格与评测 ⑥–⑨**：正式 attestation 存在 ⇒ research_grade 通过；
   移走 ⇒ absent（显式记录不崩溃）；版本过期 ⇒ version_expired；
   改 1 字节 fixture ⇒ 自绑定失效（mismatch）；
3. **live_reverify 交叉核对**：库函数与 `tools/verify_sources.py` 是同一语料上的
   两份判定实现（工具刻意不 import 本库），用同一语料钉住判定一致（防漂移）；
4. **G4 落地**（P2.2-1 复核 deferred 项）：移走 `attestations/` ⇒
   G2（双签名）与 G3（eval 10/10）结果不变。

依赖真实原件（`.workbuddy/tmp/audit/`）的用例在目录缺失时 skip 并明确记录原因
（missing 与 problem 二分：缺目录不是篡改信号）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentboard.research.attestation import load_attestation
from agentboard.research.config import (
    POLICY_RESEARCH_GRADE,
    REASON_ABSENT,
    REASON_MISMATCH,
    REASON_VERSION_EXPIRED,
)
from agentboard.research.errors import NonOfficialSourceError, SnapshotDigestError
from agentboard.research.snapshot import load_snapshot_set
from agentboard.research.sources import (
    live_reverify,
    load_official_sources,
    make_task_input,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_ROOT = PROJECT_ROOT / "fixtures" / "official"
AUDIT_RAW_DIR = PROJECT_ROOT / ".workbuddy" / "tmp" / "audit"
ATTESTATION_PATH = PROJECT_ROOT / "attestations" / "official.sources.v1.json"
I10 = "I10_NON_PROMOTABLE_SUPPORT"

PY = sys.executable


def _cards_from(sources: Any) -> list[Any]:
    import asyncio

    from agentboard.contracts import TaskCard

    worker = sources.worker()
    cards: list[Any] = []
    for candidate_id in sources.candidate_ids():
        task_input = make_task_input(candidate_id)
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


def _edit_json(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _resign(root: Path) -> None:
    index_path = root / "INDEX.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["dimension_registry_sha256"] = _sha256(root / index["dimension_registry"])
    for entry in index["candidates"]:
        entry["fixture_sha256"] = _sha256(root / entry["fixture_path"])
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def cloned_official(tmp_path: Path) -> Path:
    target = tmp_path / "official"
    shutil.copytree(OFFICIAL_ROOT, target)
    return target


@pytest.fixture
def cloned_raw(tmp_path: Path) -> Path:
    assert AUDIT_RAW_DIR.is_dir(), "测试前提：真实原件目录存在"
    target = tmp_path / "audit"
    shutil.copytree(AUDIT_RAW_DIR, target)
    # 本机原件文件名带 "@<commit>" 后缀（下载时的命名习惯）；工具与 live_reverify
    # 的 name 兜底匹配按**精确文件名**（document.path 的末段）。
    # tmp 克隆里去掉后缀，使"按名兜底"与"按内容匹配"两条路都可用（语义不变）。
    for path in sorted(target.rglob("*")):
        if path.is_file() and "@" in path.name:
            path.rename(path.with_name(path.name.split("@", 1)[0]))
    return target


def _load_verify_tool() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "agentboard_verify_sources_gate", PROJECT_ROOT / "tools" / "verify_sources.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---- 篡改检测 ①–⑤（全部在 tmp 克隆上，G1 不受影响） ----


def test_tamper_1_raw_byte_is_problem_not_missing(
    cloned_official: Path, cloned_raw: Path, tmp_path: Path
) -> None:
    """①改 1 字节原始文件 ⇒ `verify_sources` 报 **problem**，不是 missing。"""
    tool = _load_verify_tool()
    readme = cloned_raw / "README.md"
    readme.write_bytes(readme.read_bytes() + b" ")
    result = tool.verify_sources(cloned_official, cloned_raw)
    assert result.missing == [], "内容不符不得退化成缺失（missing/problem 二分）"
    assert any("doc_pgvector_readme" in problem for problem in result.problems)


def test_tamper_2_line_range_breaks_excerpt(
    cloned_official: Path, cloned_raw: Path
) -> None:
    """②改 `line_range` ⇒ 行号切片与快照摘录不符（problem）。"""
    tool = _load_verify_tool()
    fixture = cloned_official / "sources" / "pgvector.json"
    _edit_json(fixture, lambda p: p["sections"][0].update({"line_range": "4"}))
    _resign(cloned_official)  # 重签让 C7 自洽：专测来源核对这一层
    result = tool.verify_sources(cloned_official, cloned_raw)
    assert any("切片与快照摘录不一致" in problem for problem in result.problems), result.problems


def test_tamper_3_raw_sha256_mismatch(cloned_official: Path, cloned_raw: Path) -> None:
    """③改 `raw_sha256` ⇒ 与原件内容不符（problem）。"""
    tool = _load_verify_tool()
    fixture = cloned_official / "sources" / "pgvector.json"
    _edit_json(
        fixture,
        lambda p: p["provenance"]["documents"][0].update({"raw_sha256": "0" * 64}),
    )
    _resign(cloned_official)
    result = tool.verify_sources(cloned_official, cloned_raw)
    assert any("raw_sha256 不符" in problem for problem in result.problems), result.problems


def test_tamper_4_commit_sha_breaks_commit_pinned_url(cloned_official: Path) -> None:
    """④改 `commit_sha` ⇒ url/raw_url 不再 commit-pinned ⇒ **加载期**拒绝。"""
    fixture = cloned_official / "sources" / "pgvector.json"
    _edit_json(
        fixture,
        lambda p: p["provenance"]["documents"][0].update({"commit_sha": "a" * 40}),
    )
    _resign(cloned_official)
    with pytest.raises(NonOfficialSourceError):
        load_official_sources(cloned_official)


def test_tamper_5_whole_snapshot_swap_breaks_file_digest(cloned_official: Path) -> None:
    """⑤替换整份快照文件 ⇒ 文件级 digest 不符（C7 加载期拒绝）。"""
    fixture = cloned_official / "sources" / "pgvector.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["display_name"] = "被整份替换的快照"
    fixture.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(SnapshotDigestError):
        load_official_sources(cloned_official)


# ---- 升格与评测 ⑥–⑨ ----


def test_promotion_6_official_attestation_enables_research_grade() -> None:
    """⑥正式 `attestations/` 存在 ⇒ research_grade 通过，report_id 与默认一致。"""
    assert ATTESTATION_PATH.is_file(), "测试前提：正式证明工件已生成（P2.2-4）"
    snapshot_set = load_snapshot_set(
        fixture_root=OFFICIAL_ROOT, attestation_path=ATTESTATION_PATH,
        policy=POLICY_RESEARCH_GRADE,
    )
    src = load_official_sources(OFFICIAL_ROOT)
    from agentboard.contracts import ProjectBrief
    from agentboard.research.assemble import assemble_report

    brief = ProjectBrief(
        text="为检索服务做技术选型（依据官方文档快照）",
        source="docs/research/selection_goal.md",
    )
    cards = _cards_from(src)
    default = assemble_report(brief, cards, src.registry, snapshots=src.snapshots)
    graded = assemble_report(
        brief, cards, src.registry, snapshots=src.snapshots,
        snapshot_set=snapshot_set,
    )
    assert graded.integrity.passed is True
    assert graded.report_id == default.report_id == "rp_9e0057b9f989020f"


def test_promotion_7_without_attestation_is_absent_not_crash() -> None:
    """⑦移走 attestation ⇒ 同场景 not_promotable（原因 `absent`，显式记录不崩溃）。"""
    from agentboard.research.assemble import assemble_report
    from agentboard.research.snapshot import SnapshotSet, load_catalog

    src = load_official_sources(OFFICIAL_ROOT)
    declared = SnapshotSet(
        snapshot_set_id=load_catalog(OFFICIAL_ROOT).snapshot_set,
        policy=POLICY_RESEARCH_GRADE,
    )
    report = assemble_report(
        _official_brief(), _cards_from(src), src.registry, snapshots=src.snapshots,
        snapshot_set=declared, strict=False,
    )
    assert report.integrity.passed is False
    assert I10 in report.integrity.failed_codes
    assert REASON_ABSENT in report.integrity.detail_of(I10)


def test_promotion_8_version_expired_is_distinguishable(tmp_path: Path) -> None:
    """⑧`verifier_version` 过期 ⇒ 不可升格，原因 `version_expired`（与 absent 可区分）。"""
    assert ATTESTATION_PATH.is_file()
    payload = json.loads(ATTESTATION_PATH.read_text(encoding="utf-8"))
    payload["verifier_version"] = int(payload["verifier_version"]) + 1
    mutated = tmp_path / "expired.json"
    mutated.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    snapshot_set = load_snapshot_set(
        fixture_root=OFFICIAL_ROOT, attestation_path=mutated,
        policy=POLICY_RESEARCH_GRADE,
    )
    from agentboard.research.assemble import assemble_report

    report = assemble_report(
        _official_brief(), _cards_from(load_official_sources(OFFICIAL_ROOT)),
        load_official_sources(OFFICIAL_ROOT).registry,
        snapshots=load_official_sources(OFFICIAL_ROOT).snapshots,
        snapshot_set=snapshot_set, strict=False,
    )
    assert report.integrity.passed is False
    assert REASON_VERSION_EXPIRED in report.integrity.detail_of(I10)


def test_promotion_9_one_byte_fixture_breaks_self_binding(cloned_official: Path) -> None:
    """⑨改 1 字节 fixture ⇒ `load_attestation` 自绑定失效（原因 `mismatch`）。"""
    assert ATTESTATION_PATH.is_file()
    fixture = cloned_official / "sources" / "pgvector.json"
    _edit_json(fixture, lambda p: p.update({"source_note": p["source_note"] + " "}))
    attestation = load_attestation(ATTESTATION_PATH, fixture_root=cloned_official)
    assert attestation.binding_reason == REASON_MISMATCH


# ---- live_reverify 交叉核对 + missing/problem 二分 ----


def test_live_reverify_matches_tool_verdict_on_same_corpus() -> None:
    """两侧判定实现（库 vs 工具）在**同一语料**上必须给出一致的"全通过"结论。"""
    tool = _load_verify_tool()
    src = load_official_sources(OFFICIAL_ROOT)
    ok, problems, missing = live_reverify(src, AUDIT_RAW_DIR)
    result = tool.verify_sources(OFFICIAL_ROOT, AUDIT_RAW_DIR)
    assert ok is True and problems == [] and missing == []
    assert result.problems == [] and result.missing == []


def test_live_reverify_reports_problem_and_missing_separately(
    cloned_official: Path, cloned_raw: Path
) -> None:
    """问题与缺失**分开报**：删一份原件（missing）+ 改另一份内容（problem）。"""
    src = load_official_sources(cloned_official)
    (cloned_raw / "README.md").write_bytes(
        (cloned_raw / "README.md").read_bytes() + b" tampered"
    )
    (cloned_raw / "installation.md").unlink()
    ok, problems, missing = live_reverify(src, cloned_raw)
    assert ok is False
    assert any("doc_pgvector_readme" in problem and "不符" in problem for problem in problems)
    assert any("doc_qdrant_installation" in item for item in missing)


# ---- G4：移走 attestations/ ⇒ G2（双签名）与 G3（eval 10/10）不变 ----


def _run_gates() -> tuple[int, int]:
    """跑 G2 双签名 + G3 research eval，返回 (签名退出码, eval 退出码)。"""
    sign = subprocess.run(
        [PY, "tools/sign_fixtures.py", "--check"],
        capture_output=True, text=True, cwd=PROJECT_ROOT, check=False,
    )
    sign_official = subprocess.run(
        [PY, "tools/sign_fixtures.py", "--check", "--root", "fixtures/official"],
        capture_output=True, text=True, cwd=PROJECT_ROOT, check=False,
    )
    sign_code = max(sign.returncode, sign_official.returncode)
    eval_code = subprocess.run(
        [PY, "eval/research/run_eval.py"],
        capture_output=True, text=True, cwd=PROJECT_ROOT, check=False,
    ).returncode
    return sign_code, eval_code


def test_g4_attestation_presence_does_not_affect_g2_g3() -> None:
    """G4 落地（P2.2-1 复核 deferred 项）：attestations/ 的存在与否不影响 G2/G3。

    基线（工件在位）与对照（工件移走）各跑一次 G2+G3，两次退出码与 eval 指标
    逐字节一致。try/finally 保证工件必然恢复。
    """
    assert ATTESTATION_PATH.is_file()
    baseline_sign, baseline_eval = _run_gates()
    baseline_json = (
        subprocess.run(
            [PY, "eval/research/run_eval.py"], capture_output=True, text=True, cwd=PROJECT_ROOT, check=False,
        ).stdout
    )
    backup = ATTESTATION_PATH.with_suffix(".json.g4-backup")
    try:
        shutil.move(str(ATTESTATION_PATH), str(backup))
        assert not ATTESTATION_PATH.exists()
        moved_sign, moved_eval = _run_gates()
        moved_json = (
            subprocess.run(
                [PY, "eval/research/run_eval.py"], capture_output=True, text=True, cwd=PROJECT_ROOT, check=False,
            ).stdout
        )
    finally:
        shutil.move(str(backup), str(ATTESTATION_PATH))
    assert (baseline_sign, baseline_eval) == (0, 0)
    assert (moved_sign, moved_eval) == (0, 0), "G4：移走工件后 G2/G3 不得受影响"
    assert moved_json == baseline_json, "G4：eval 指标必须逐字节一致"


def _official_brief() -> Any:
    from agentboard.contracts import ProjectBrief

    return ProjectBrief(
        text="为检索服务做技术选型（依据官方文档快照）",
        source="docs/research/selection_goal.md",
    )

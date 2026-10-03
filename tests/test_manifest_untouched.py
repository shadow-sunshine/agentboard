"""P1A-0 基线守卫：课程原始资产（`scripts/` / `assets/` / `materials/` / 根目录原始文件）未被改动。

通过 importlib 按路径加载 `tools/verify_manifest.py`，不改 `pyproject.toml` 的 `pythonpath`
（`tools/` 不在 import 根里）。全部校验逻辑只有一份实现，`--check` 与 pytest 共用。

测试同时覆盖「守卫本身是否正确」：用临时目录合成被篡改 / 伪造 / 多余的清单，
正向确认 C1–C5 能真的抓到问题，避免出现"永远通过"的空壳守卫。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = PROJECT_ROOT / "tools" / "verify_manifest.py"

EXPECTED_ENTRY_COUNT = 19


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agentboard_verify_manifest", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verifier() -> ModuleType:
    return _load_verifier()


def _write_manifest(root: Path, entries: list[dict], verifier: ModuleType) -> None:
    payload = {
        "manifest_schema": verifier.MANIFEST_SCHEMA,
        "root": ".",
        "self_excluded": list(verifier.SELF_EXCLUDED),
        "guarded_prefixes": ["scripts/"],
        "ignore_dirs": sorted(verifier.IGNORE_DIRS),
        "ignore_suffixes": sorted(verifier.IGNORE_SUFFIXES),
        "entries": entries,
    }
    target = root / verifier.MANIFEST_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _entry(path: Path, rel: str, verifier: ModuleType) -> dict:
    return {
        "path": rel,
        "sha256": verifier.sha256_file(path),
        "size": path.stat().st_size,
        "tier": verifier.tier_for(rel),
    }


# ---- 真实仓库：基线必须干净 ----


def test_baseline_manifest_is_clean(verifier: ModuleType) -> None:
    count, violations = verifier.check_manifest(PROJECT_ROOT)
    assert violations == []
    assert count == EXPECTED_ENTRY_COUNT


def test_manifest_matches_declared_baseline_paths(verifier: ModuleType) -> None:
    """清单条目必须与代码里声明的 BASELINE_PATHS 一一对应（防止手改清单悄悄删条目）。"""
    manifest = json.loads(
        (PROJECT_ROOT / verifier.MANIFEST_REL).read_text(encoding="utf-8")
    )
    listed = [entry["path"] for entry in manifest["entries"]]
    assert listed == list(verifier.BASELINE_PATHS)
    assert len(listed) == EXPECTED_ENTRY_COUNT


def test_manifest_contains_no_phantom_paths(verifier: ModuleType) -> None:
    """清单只收录真实存在的文件；本项目不存在 demo.py。"""
    manifest = json.loads(
        (PROJECT_ROOT / verifier.MANIFEST_REL).read_text(encoding="utf-8")
    )
    for entry in manifest["entries"]:
        assert (PROJECT_ROOT / entry["path"]).is_file(), entry["path"]
    assert "demo.py" not in {entry["path"] for entry in manifest["entries"]}


def test_manifest_excludes_itself_and_verifier(verifier: ModuleType) -> None:
    """baseline 文件与 verifier 自身都不纳入 hash。"""
    manifest = json.loads(
        (PROJECT_ROOT / verifier.MANIFEST_REL).read_text(encoding="utf-8")
    )
    listed = {entry["path"] for entry in manifest["entries"]}
    assert set(manifest["self_excluded"]) == set(verifier.SELF_EXCLUDED)
    assert listed.isdisjoint(manifest["self_excluded"])


def test_scripts_dir_has_no_unlisted_files(verifier: ModuleType) -> None:
    guard_root = PROJECT_ROOT / "scripts"
    listed = {path for path in verifier.iter_tree_files(PROJECT_ROOT, guard_root)}
    manifest = json.loads(
        (PROJECT_ROOT / verifier.MANIFEST_REL).read_text(encoding="utf-8")
    )
    known = {entry["path"] for entry in manifest["entries"]}
    assert listed <= known


# ---- 生成期防线：拒绝把不存在的路径写进清单 ----


def test_build_manifest_rejects_missing_path(verifier: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier, "BASELINE_PATHS", ("demo.py",))
    with pytest.raises(SystemExit):
        verifier.build_manifest(PROJECT_ROOT)


# ---- 守卫本身的正向控制：C1–C5 必须真的能抓到问题 ----


def test_check_detects_modified_file(verifier: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "scripts" / "01_plan_board.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('tampered')\n", encoding="utf-8")
    _write_manifest(
        tmp_path,
        [{"path": "scripts/01_plan_board.py", "sha256": "0" * 64, "size": target.stat().st_size, "tier": "A"}],
        verifier,
    )
    _, violations = verifier.check_manifest(tmp_path)
    assert any(item.startswith("C2") for item in violations), violations


def test_check_detects_phantom_path(verifier: ModuleType, tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        [{"path": "demo.py", "sha256": "0" * 64, "size": 0, "tier": "B"}],
        verifier,
    )
    _, violations = verifier.check_manifest(tmp_path)
    assert any(item.startswith("C1") for item in violations), violations


def test_check_detects_extra_file_in_guarded_prefix(verifier: ModuleType, tmp_path: Path) -> None:
    known = tmp_path / "scripts" / "01_plan_board.py"
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text("print('ok')\n", encoding="utf-8")
    extra = tmp_path / "scripts" / "99_sneaky.py"
    extra.write_text("print('extra')\n", encoding="utf-8")
    _write_manifest(tmp_path, [_entry(known, "scripts/01_plan_board.py", verifier)], verifier)
    _, violations = verifier.check_manifest(tmp_path)
    assert any(item.startswith("C3") for item in violations), violations


def test_check_detects_self_exclusion_violation(verifier: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "fixtures" / "baselines" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    _write_manifest(
        tmp_path,
        [_entry(target, "fixtures/baselines/manifest.json", verifier)],
        verifier,
    )
    _, violations = verifier.check_manifest(tmp_path)
    assert any(item.startswith("C4") for item in violations), violations


def test_check_detects_duplicate_entry(verifier: ModuleType, tmp_path: Path) -> None:
    known = tmp_path / "scripts" / "01_plan_board.py"
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text("print('ok')\n", encoding="utf-8")
    duplicated = _entry(known, "scripts/01_plan_board.py", verifier)
    _write_manifest(tmp_path, [duplicated, dict(duplicated)], verifier)
    _, violations = verifier.check_manifest(tmp_path)
    assert any(item.startswith("C5") for item in violations), violations


def test_check_reports_missing_manifest(verifier: ModuleType, tmp_path: Path) -> None:
    count, violations = verifier.check_manifest(tmp_path)
    assert count == 0
    assert any(item.startswith("C0") for item in violations), violations


# ---- 全树快照（替代 git status） ----


def test_diff_tree_requires_snapshot(verifier: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        verifier.diff_tree(tmp_path)


def test_diff_tree_detects_added_modified_removed(verifier: ModuleType, tmp_path: Path) -> None:
    tree_rel = verifier.TREE_REL
    snapshot_file = tmp_path / tree_rel
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(
        json.dumps(
            {
                "tree_schema": verifier.TREE_SCHEMA,
                "root": ".",
                "files": {"tracked.txt": {"sha256": "0" * 64, "size": 1}, "gone.txt": {"sha256": "1" * 64, "size": 1}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    changed = verifier.diff_tree(tmp_path)
    assert "modified: tracked.txt" in changed
    assert "added: new.txt" in changed
    assert "removed: gone.txt" in changed

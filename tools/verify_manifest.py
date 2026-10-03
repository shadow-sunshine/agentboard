"""AgentBoard 基线清单校验器（P1A-0）。

职责单一：证明 P1A 承诺不改动的课程原始资产（`scripts/` / `assets/` / `materials/` /
根目录原始文件）确实没有被改动。

设计约束（与 P1A_PLAN.md §7 一一对应）：
- **只用标准库**（argparse / hashlib / json / pathlib / sys），与 `dependencies = []` 一致；
- **不调用 git / grep / find / diff**：本仓库没有 `.git`，且这些工具在目标环境不保证存在
  （coreutils 缺失时 `ls` / `head` / `find` 直接不可用），改用纯 Python 遍历与哈希；
- `fixtures/baselines/manifest.json` 与**本脚本自身**都不纳入 hash（`self_excluded`），
  否则每次调整校验逻辑都要重签清单；
- 清单内路径必须**真实存在**才允许生成（C1），从机制上杜绝把不存在的路径写进基线。

用法：
    python tools/verify_manifest.py --write       # 生成/重签基线清单（P1A-0 一次性）
    python tools/verify_manifest.py --write-tree  # 生成全树快照（报告用，P1A-0 末次执行）
    python tools/verify_manifest.py --check        # 校验基线（默认动作）
    python tools/verify_manifest.py --diff-tree    # 输出相对全树快照的改动清单（不参与通过条件）

退出码：`--check` 有 violation 时返回 1，否则 0；`--diff-tree` 恒为 0（除非缺少快照）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_SCHEMA = "agentboard.baseline.v1"
TREE_SCHEMA = "agentboard.tree.v1"

MANIFEST_REL = "fixtures/baselines/manifest.json"
TREE_REL = "fixtures/baselines/tree_p1a0.json"

# 遍历时跳过的目录名与文件后缀（运行时产物，不进任何清单）
IGNORE_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".workbuddy", ".git", ".venv"}
)
IGNORE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo", ".pyd"})

# 清单自身与 verifier 自身都不自我哈希（C4）
SELF_EXCLUDED: tuple[str, ...] = (MANIFEST_REL, "tools/verify_manifest.py")

# 这些目录下不允许出现清单外的新文件（C3）
GUARDED_PREFIXES: tuple[str, ...] = ("scripts/",)

# Tier A = 硬红线（P1A 内绝对不改）；其余为 Tier B = 课程原始资产
TIER_A_PREFIXES: tuple[str, ...] = ("scripts/",)

# 全树快照排除自身，避免自引用（其余文件都要出现在快照里）
TREE_SELF_EXCLUDED: tuple[str, ...] = (TREE_REL,)

# P1A 承诺不改动的文件，逐条列出（19 条，全部真实存在）。
# 本项目不存在 demo.py —— 清单只收录实际存在的路径。
BASELINE_PATHS: tuple[str, ...] = (
    "scripts/01_plan_board.py",
    "scripts/02_worker_acp_smoke.py",
    "scripts/03_context_isolation.py",
    "scripts/04_triggerflow_master_loop.py",
    "scripts/05_hangzhou_team_building_run.py",
    "assets/01_master_worker_architecture.svg",
    "assets/02_business_flow.svg",
    "assets/03_acp_multi_turn.svg",
    "assets/04_board_loop.svg",
    "assets/05_project_brief_scope.svg",
    "assets/06_data_contracts_code_view.svg",
    "assets/07_board_planning_pipeline.svg",
    "assets/08_private_task_package.svg",
    "assets/09_acp_local_codex_minimal.svg",
    "materials/product_goal.txt",
    "主Agent调度与多Agent协作.md",
    "主Agent调度与多Agent协作.ipynb",
    "主Agent调度与多Agent协作.pdf",
    ".env.example",
)


def sha256_file(path: Path) -> str:
    """分块计算文件 sha256，避免把大文件整体读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tier_for(rel: str) -> str:
    return "A" if any(rel.startswith(prefix) for prefix in TIER_A_PREFIXES) else "B"


def iter_tree_files(root: Path, start: Path | None = None) -> list[str]:
    """遍历 root 下的文件，返回排序后的 POSIX 相对路径。

    不依赖 os.walk 的隐式顺序，也不使用任何 shell 工具；
    结果显式排序，保证清单生成与比较都是确定性的。
    """
    base = start if start is not None else root
    found: list[str] = []
    stack: list[Path] = [base]
    while stack:
        current = stack.pop()
        for entry in current.iterdir():
            if entry.is_dir():
                if entry.name in IGNORE_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() not in IGNORE_SUFFIXES:
                found.append(entry.relative_to(root).as_posix())
    return sorted(found)


def build_manifest(root: Path) -> dict:
    """按 BASELINE_PATHS 生成清单；任一路径不存在直接拒绝生成。"""
    entries: list[dict] = []
    missing: list[str] = []
    for rel in BASELINE_PATHS:
        path = root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        entries.append(
            {
                "path": rel,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "tier": tier_for(rel),
            }
        )
    if missing:
        raise SystemExit("清单路径不存在，拒绝生成基线：" + ", ".join(missing))
    return {
        "manifest_schema": MANIFEST_SCHEMA,
        "root": ".",
        "self_excluded": list(SELF_EXCLUDED),
        "guarded_prefixes": list(GUARDED_PREFIXES),
        "ignore_dirs": sorted(IGNORE_DIRS),
        "ignore_suffixes": sorted(IGNORE_SUFFIXES),
        "entries": entries,
    }


def build_tree(root: Path) -> dict:
    """全树快照（报告用），用于替代 `git status` 输出改动清单。"""
    files: dict[str, dict] = {}
    for rel in iter_tree_files(root):
        if rel in TREE_SELF_EXCLUDED:
            continue
        path = root / rel
        files[rel] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return {
        "tree_schema": TREE_SCHEMA,
        "root": ".",
        "ignore_dirs": sorted(IGNORE_DIRS),
        "ignore_suffixes": sorted(IGNORE_SUFFIXES),
        "files": files,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def check_manifest(root: Path) -> tuple[int, list[str]]:
    """C1–C5 校验，返回 (清单条目数, violation(违规) 列表)。"""
    manifest = _read_json(root / MANIFEST_REL)
    if manifest is None:
        return 0, [f"C0 缺少或无法解析基线清单：{MANIFEST_REL}"]

    violations: list[str] = []
    if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
        violations.append(
            f"C0 manifest_schema 期望 {MANIFEST_SCHEMA!r}，实际 {manifest.get('manifest_schema')!r}"
        )

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        return 0, [*violations, "C0 entries 必须是非空列表"]

    declared_excluded = manifest.get("self_excluded")
    if not isinstance(declared_excluded, list) or set(declared_excluded) != set(SELF_EXCLUDED):
        violations.append(f"C4 self_excluded 必须恰好为 {list(SELF_EXCLUDED)}")
    excluded = set(declared_excluded) if isinstance(declared_excluded, list) else set(SELF_EXCLUDED)

    ignore_dirs = {d for d in manifest.get("ignore_dirs", []) if isinstance(d, str)} or set(IGNORE_DIRS)
    ignore_suffixes = {
        s for s in manifest.get("ignore_suffixes", []) if isinstance(s, str)
    } or set(IGNORE_SUFFIXES)

    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            violations.append(f"C5 entries[{index}] 必须是 dict")
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            violations.append(f"C5 entries[{index}].path 必须是非空字符串")
            continue
        if rel in seen:
            violations.append(f"C5 entry.path 重复：{rel}")
            continue
        seen.add(rel)
        if rel in excluded:
            violations.append(f"C4 {rel} 属于 self_excluded，不得纳入 hash")
            continue
        path = root / rel
        if not path.is_file():
            violations.append(f"C1 清单路径不存在：{rel}")
            continue
        actual_size = path.stat().st_size
        if actual_size != entry.get("size"):
            violations.append(
                f"C2 文件大小不一致：{rel}（期望 {entry.get('size')}，实际 {actual_size}）"
            )
        actual_digest = sha256_file(path)
        if actual_digest != entry.get("sha256"):
            violations.append(f"C2 文件已被修改：{rel}")

    for prefix in manifest.get("guarded_prefixes", []):
        if not isinstance(prefix, str) or not prefix:
            continue
        guarded_root = root / prefix.rstrip("/")
        if not guarded_root.is_dir():
            violations.append(f"C3 guarded 目录不存在：{prefix}")
            continue
        for rel in iter_tree_files(root, guarded_root):
            if rel in excluded or rel in seen:
                continue
            if (root / rel).suffix.lower() in ignore_suffixes:
                continue
            if any(part in ignore_dirs for part in Path(rel).parts[:-1]):
                continue
            violations.append(f"C3 {prefix} 下出现清单外文件：{rel}")

    return len(entries), violations


def diff_tree(root: Path) -> list[str]:
    """输出相对全树快照的 added / modified / removed 清单（报告用）。"""
    snapshot = _read_json(root / TREE_REL)
    if snapshot is None or not isinstance(snapshot.get("files"), dict):
        raise SystemExit(f"缺少全树快照：{TREE_REL}，请先运行 --write-tree")
    old: dict = snapshot["files"]
    new: dict[str, str] = {}
    for rel in iter_tree_files(root):
        if rel in TREE_SELF_EXCLUDED:
            continue
        new[rel] = sha256_file(root / rel)

    changed: list[str] = []
    for rel in sorted(set(new) - set(old)):
        changed.append(f"added: {rel}")
    for rel in sorted(set(old) & set(new)):
        if old[rel].get("sha256") != new[rel]:
            changed.append(f"modified: {rel}")
    for rel in sorted(set(old) - set(new)):
        changed.append(f"removed: {rel}")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AgentBoard P1A 基线清单校验器（仅标准库，不依赖 git/grep）"
    )
    parser.add_argument("--root", default=None, help="项目根目录，默认取本脚本的上级目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="生成/重签基线清单 manifest.json")
    mode.add_argument("--write-tree", action="store_true", help="生成全树快照 tree_p1a0.json")
    mode.add_argument("--check", action="store_true", help="校验基线清单（默认动作）")
    mode.add_argument("--diff-tree", action="store_true", help="输出相对全树快照的改动清单")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent

    if args.write:
        manifest = build_manifest(root)
        _write_json(root / MANIFEST_REL, manifest)
        print(f"{MANIFEST_REL} 已写入：{len(manifest['entries'])} entries")
        return 0

    if args.write_tree:
        tree = build_tree(root)
        _write_json(root / TREE_REL, tree)
        print(f"{TREE_REL} 已写入：{len(tree['files'])} files")
        return 0

    if args.diff_tree:
        changed = diff_tree(root)
        if changed:
            for line in changed:
                print(line)
        print(f"{len(changed)} changed paths vs {TREE_REL}")
        return 0

    count, violations = check_manifest(root)
    if violations:
        for item in violations:
            print(item, file=sys.stderr)
        print(f"{count} entries verified, {len(violations)} violations", file=sys.stderr)
        return 1
    print(f"{count} entries verified, 0 violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

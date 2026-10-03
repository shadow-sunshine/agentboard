"""fixture 快照签名工具（P1A-4）。

职责：维护 `fixtures/research/INDEX.json` 里的文件级 sha256 登记。

为什么需要它：`EvidenceRef.digest` 只能证明"某一条摘录没被篡改"，证明不了
"整份 option 文件就是当初登记的那一份"。文件级 digest 补上这一环 —— 一旦有人替换了
整个快照文件，`load_catalog` / `load_snapshot` 会立刻抛 `SnapshotDigestError`。

用法（Python / PowerShell 通用，不依赖 git、grep 或任何 shell 工具）：

    python tools/sign_fixtures.py --check   # 校验（默认）：结构与 digest 一起验
    python tools/sign_fixtures.py --write   # 有意修改 fixture 后重新签名

`--write` 必须在**明确知道自己在改什么**的情况下使用：它会把当前文件状态固化成新基线。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE_ROOT = PROJECT_ROOT / "fixtures" / "research"
INDEX_FILENAME = "INDEX.json"

#: INDEX.json 顶层字段的呈现顺序（其余字段按原顺序追加在后面）
_INDEX_KEY_ORDER = ("schema", "dimension_registry", "dimension_registry_sha256", "note")
#: 每个候选条目的字段顺序
_CANDIDATE_KEY_ORDER = (
    "candidate_id",
    "display_name",
    "fixture_path",
    "snapshot_version",
    "captured_at",
    "fixture_sha256",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordered(mapping: dict[str, Any], preferred: tuple[str, ...]) -> dict[str, Any]:
    """按 preferred 顺序重排已知键，未列出的键保持原有相对顺序追加在后。"""
    result: dict[str, Any] = {key: mapping[key] for key in preferred if key in mapping}
    for key, value in mapping.items():
        if key not in result:
            result[key] = value
    return result


def sign(root: Path) -> tuple[int, int]:
    """重新签名并写回 INDEX.json，返回 (候选数, 已签名文件数)。"""
    index_path = root / INDEX_FILENAME
    if not index_path.is_file():
        raise SystemExit(f"找不到 {index_path}")
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"{INDEX_FILENAME} 的顶层必须是 JSON 对象")

    registry_rel = raw.get("dimension_registry")
    if not isinstance(registry_rel, str) or not registry_rel:
        raise SystemExit(f"{INDEX_FILENAME}.dimension_registry 必须是非空字符串")
    registry_path = root / registry_rel
    if not registry_path.is_file():
        raise SystemExit(f"找不到维度注册表 {registry_path}")

    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise SystemExit(f"{INDEX_FILENAME}.candidates 必须是非空列表")

    signed_candidates: list[dict[str, Any]] = []
    for position, entry in enumerate(raw_candidates, start=1):
        if not isinstance(entry, dict):
            raise SystemExit(f"candidates[{position}] 必须是 JSON 对象")
        fixture_rel = entry.get("fixture_path")
        if not isinstance(fixture_rel, str) or not fixture_rel:
            raise SystemExit(f"candidates[{position}].fixture_path 必须是非空字符串")
        fixture_path = root / fixture_rel
        if not fixture_path.is_file():
            raise SystemExit(f"找不到快照文件 {fixture_path}")
        updated = dict(entry)
        updated["fixture_sha256"] = file_sha256(fixture_path)
        signed_candidates.append(_ordered(updated, _CANDIDATE_KEY_ORDER))

    signed_index = dict(raw)
    signed_index["dimension_registry_sha256"] = file_sha256(registry_path)
    signed_index["candidates"] = signed_candidates
    signed_index = _ordered(signed_index, _INDEX_KEY_ORDER)

    index_path.write_text(
        json.dumps(signed_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return len(signed_candidates), len(signed_candidates) + 1


def check(root: Path) -> list[str]:
    """完整加载一遍 fixture 目录（结构 + digest），返回问题清单。"""
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from agentboard.research.errors import AgentBoardResearchError
    from agentboard.research.snapshot import load_catalog, load_snapshots

    try:
        catalog = load_catalog(root)
        load_snapshots(root, catalog)
    except (AgentBoardResearchError, ValueError) as exc:
        return [f"{type(exc).__name__}: {exc}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fixture 快照签名工具（仅标准库）")
    parser.add_argument("--root", default=str(DEFAULT_FIXTURE_ROOT), help="fixtures/research 目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="校验结构与 digest（默认动作）")
    mode.add_argument("--write", action="store_true", help="重新签名 INDEX.json")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()

    if args.write:
        candidates, signed_files = sign(root)
        print(f"{INDEX_FILENAME} 已重新签名：{candidates} 个候选 + 1 个维度注册表（共 {signed_files} 个文件）")
        return 0

    problems = check(root)
    if problems:
        for item in problems:
            print(item, file=sys.stderr)
        print(f"fixture 校验失败：{len(problems)} 个问题", file=sys.stderr)
        return 1
    print("fixture 校验通过：结构 + 文件级 digest 全部一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

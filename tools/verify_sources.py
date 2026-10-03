"""离线复核官方资料快照的来源锚点（真实资料接入第一步的审计工具）。

**它不联网。** 核对链条靠 `provenance.documents[]` 里登记的内容自己闭合：

    raw_url（固定到 commit）──下载一次──▶ 原始文件字节
       │                                    │
       │ ①sha256 == raw_sha256              │ ②sha1("blob <len>\\0" + bytes) == blob_sha
       ▼                                    ▼
    内容未被替换                      就是该 commit 里的那个 git blob
       │
       │ ③按 line_range 切片 == sections[].text（逐字节）
       ▼
    摘录与行号对得上原文

用法（Python / PowerShell 通用，不依赖 git、grep 或任何 shell 工具）：

    # 先把原始文件下到某个目录（任何文件名都可以，按内容匹配），例如：
    #   curl -L <raw_url> -o raw/pgvector-readme.md
    python tools/verify_sources.py --raw-dir .raw
    python tools/verify_sources.py --raw-dir .raw --allow-missing   # 允许缺失部分来源

`--raw-dir` 里缺哪份来源、哪条摘录对不上，都会逐条列出来并以非 0 退出。

生成外部验证证明（P2.2-1 新增，opt-in）
----------------------------------------
    python tools/verify_sources.py --raw-dir .workbuddy/tmp/audit \\
        --write-attestation attestations/official.sources.v1.json

`attestation` 是 C6（"摘录与原文逐字节一致"）的**外部工件**：C6 是准入闸门里
唯一需要原件的条件，写进 fixture 就等于改 fixture 字节，所以它的结论必须独立存放。

写入策略（四条硬规则）：

| # | 规则 | 行为 |
|---|---|---|
| W1 | **默认禁止覆盖已有文件** | 目标已存在 → 拒绝（没有 `--force` 这种口子）。**发布用 `os.link` 排他创建，不用 `os.replace`** |
| W2 | **已有文件内容不一致 → 失败** | 逐字节相同 ⇒ 幂等成功（退出码 0，不写盘）；不同 ⇒ 报错拒绝 |
| W3 | **原子写入** | 同目录临时文件 + `os.fsync` + `os.link` 原子排他发布；**不支持时 fail-closed**（拒绝写入、退出码非 0），异常路径清理临时文件 |
| W4 | **不允许把 `incomplete` 静默改写成 `passed`** | 见下 |

> **P1-2 复核修正（2026-09-25）**：早期实现用"先检查目标不存在 → `os.replace`"。
> `os.replace` 是**无条件覆盖**语义，检查与替换之间有一个**真实**窗口 ——
> 窗口内出现的文件会被静默覆盖，因此那个写法与"禁止覆盖"**不等价**（已实测复现）。
> 现改为 `os.link(temp, target)`：**原子且排他**，目标已存在时以 `FileExistsError` 失败，
> 于是"有没有别人先到"由发布动作本身判定，窗口消失。

> **W3 复核修正（2026-09-25）**：`os.link` 抛非 `FileExistsError` 的 `OSError` 时，
> 曾"退化为 `O_EXCL` 直写并返回成功"。那条路径与主路径**保证不等价**：
> `O_CREAT | O_EXCL` 只有排他性、**没有中断原子性** —— 进程在写完之前被强杀会留下
> 半写文件，而它看起来像一份"已生成的工件"，无法与"被篡改"区分。
> 原子写入是既批准过的硬约束，**不得由实现自行降级**。
> 现该情形一律**清理临时文件、明确失败**（调用方据此返回非零退出码），
> 把"该文件系统不支持原子排他发布"暴露给人，而不是悄悄换成更弱的保证。

**W4 是机制性保证，不是口令**：工件内容完全由
`(fixture 字节, 原件集合, verifier_version, algorithm)` 决定（全字段确定性、零时间字段），
因此 `passed` 只能被"核对出来"、不能被"写出来"；`incomplete → passed` 必然伴随内容变化，
而 W2 让"内容不同即失败"。要合法升级必须先**显式删除**旧文件（一个可见的、进 git 历史的动作）。
本工具**不声称**能阻止这一步 —— 它保证的是"不存在静默改写路径"。

**信任模型**：本项目不实现数字签名。工件证明的是"有人在某个时点按登记的规则核对过这批字节"，
**不是**"这批字节绝对可信" —— 能改写仓库文件的人也能改写工件，且工具无法单方识破。
`--raw-dir` 的现场核对（而不是采信工件结论）是不依赖该信任的加强路径。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE_ROOT = PROJECT_ROOT / "fixtures" / "official"

#: section 在快照里的来源字段（与 `sources.SectionSource` 对应）
LINE_RANGE_PATTERN = re.compile(r"^[1-9]\d*(?:-[1-9]\d*)?$")

# ---- attestation 常量（与 `research/config.py` 的值必须一致，由测试钉住） ----

ATTESTATION_SCHEMA = "source.verification.attestation.v1"
VERIFIER_ID = "tools/verify_sources.py"
VERIFIER_VERSION = 1
VERIFIER_ALGORITHM = "sha256+git_blob_sha1+line_slice"
RESULT_PASSED = "passed"
RESULT_INCOMPLETE = "incomplete"
INDEX_FILENAME = "INDEX.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class VerificationResult:
    """核对结果。

    "缺失"（`--raw-dir` 里没有这份来源）与"有问题"（内容对不上）是**两件事**：
    前者可以用 `--allow-missing` 放行，后者永远放行。
    `passed()` 是唯一的判定入口，CLI 的退出码与它保持一致。
    """

    verified: list[tuple[str, str, int]] = field(default_factory=list)  # (source_id, raw_path, 摘录数)
    missing: list[str] = field(default_factory=list)  # 没有本地原始文件的 source_id
    problems: list[str] = field(default_factory=list)
    #: source_id -> 该文档被引用的行区间（按数值排序，去重）。
    #: **刻意不进 `to_dict()`**：既有 JSON 输出形状保持不变，这两个字段只服务于 attestation 生成。
    line_ranges: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: source_id -> (raw_sha256, blob_sha)：provenance 里**登记**的声明值。
    #: 只用于落成工件；判定始终以实际算出的摘要为准。
    declarations: dict[str, tuple[str, str]] = field(default_factory=dict)

    def passed(self, *, allow_missing: bool = False) -> bool:
        if self.problems:
            return False
        return allow_missing or not self.missing

    @property
    def overall_result(self) -> str:
        """工件的汇总结论：全部通过 → `passed`；有缺失但无不符 → `incomplete`。

        `problems` 非空时**不产生工件**（见 §失败行为），因此不会在这里出现
        "有问题是 passed"的形态 —— 但下面的断言仍然显式钉住这条不变量，
        免得将来有人放宽 `passed()` 时把它一起放宽掉。
        """
        if self.problems:
            raise ValueError("存在不符项时不得生成工件（不得把失败包装成 passed）")
        if self.missing:
            return RESULT_INCOMPLETE
        return RESULT_PASSED

    def to_dict(self, *, allow_missing: bool = False) -> dict[str, object]:
        return {
            "passed": self.passed(allow_missing=allow_missing),
            "documents_verified": len(self.verified),
            "sections_checked": sum(item[2] for item in self.verified),
            "documents_missing": len(self.missing),
            "problems": len(self.problems),
            "detail": {
                "verified": [
                    {"source_id": source_id, "raw_path": raw_path, "sections": count}
                    for source_id, raw_path, count in self.verified
                ],
                "missing": list(self.missing),
                "problem_list": list(self.problems),
            },
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha1(data: bytes) -> str:
    """Git 对象 id：`sha1("blob <字节数>\\0" + 内容)`。等于 GitHub 上的 blob sha。"""
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def index_raw_dir(raw_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """把 `--raw-dir` 下的文件建两个索引：按 sha256（内容匹配）、按文件名。

    **为什么要两个**：只按 sha256 匹配时，"文件被改了一个字符"会退化成"找不到文件"，
    于是篡改被误报成缺失。先按内容找、再按文件名兜底，才能把
    "你拿到的内容与登记的不一致"如实报成问题。文件名匹配也让复核者可以用
    每个文档的 `path` 末段命名（`README.md`、`install_standalone-docker.md` …）。
    """
    by_sha: dict[str, Path] = {}
    by_name: dict[str, Path] = {}
    for path in sorted(raw_dir.rglob("*")):
        if path.is_file():
            by_sha.setdefault(sha256_bytes(path.read_bytes()), path)
            by_name.setdefault(path.name, path)
    return by_sha, by_name


def count_lines(text: str) -> int:
    """上游文件的总行数。

    约定：按 `split("\\n")` 计，**末尾换行不算额外一行**。这样"行号"与任何编辑器、
    以及 `sections[].line_range` 的 1 起编号一致（末行 = `count_lines(text)`）。
    """
    return len(text.split("\n")) - (1 if text.endswith("\n") else 0)


def slice_lines(text: str, line_range: str) -> str:
    """按 `line_range`（1 起闭区间，单行写 "55"）切出原文。"""
    if not LINE_RANGE_PATTERN.match(line_range):
        raise ValueError(f"line_range 形态非法：{line_range!r}")
    lines = text.split("\n")
    if "-" in line_range:
        start_text, end_text = line_range.split("-", 1)
        start, end = int(start_text), int(end_text)
    else:
        start = end = int(line_range)
    if start > end:
        raise ValueError(f"line_range 起止颠倒：{line_range!r}")
    total = count_lines(text)
    if end > total:
        raise ValueError(f"line_range {line_range!r} 超出文件行数 {total}")
    return "\n".join(lines[start - 1 : end])


def check_document(
    *,
    source_id: str,
    raw_sha256: str,
    blob_sha: str,
    line_count: int | None,
    raw_bytes: bytes,
    cited: Sequence[tuple[str, str, str]],
) -> list[str]:
    """核对一份来源：内容 sha256 / git blob sha1 / 每条摘录的行号切片。

    `cited` 是 `(section_id, line_range, section_text)` 序列。
    """
    problems: list[str] = []
    actual_sha = sha256_bytes(raw_bytes)
    if actual_sha != raw_sha256:
        problems.append(
            f"{source_id}: raw_sha256 不符（登记 {raw_sha256}，实际 {actual_sha}）。"
            f"若这是从分支/标签重新下载的，请改用该文档的 raw_url（固定到 commit）"
        )
    actual_blob = git_blob_sha1(raw_bytes)
    if actual_blob != blob_sha:
        problems.append(f"{source_id}: blob_sha 不符（登记 {blob_sha}，实际 {actual_blob}）")

    text = raw_bytes.decode("utf-8")
    actual_lines = count_lines(text)
    if line_count is not None and line_count != actual_lines:
        problems.append(
            f"{source_id}: 行数不符（登记 raw_lines={line_count}，实际 {actual_lines}）"
        )

    for section_id, line_range, expected in cited:
        try:
            actual = slice_lines(text, line_range)
        except ValueError as exc:
            problems.append(f"{source_id}/{section_id}: {exc}")
            continue
        if actual != expected:
            problems.append(
                f"{source_id}/{section_id}: L{line_range} 的切片与快照摘录不一致"
                f"（原文 {len(actual)} 字符，快照 {len(expected)} 字符）"
            )
    return problems


def collect_citations(snapshot_payload: Mapping[str, object]) -> dict[str, list[tuple[str, str, str]]]:
    """把快照里"每个 section 被谁引用"整理成 `source_id -> [(section_id, line_range, text)]`。"""
    sections = snapshot_payload.get("sections")
    if not isinstance(sections, list):
        raise TypeError("快照 sections 必须是列表")
    cited: dict[str, list[tuple[str, str, str]]] = {}
    for index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            raise TypeError(f"sections[{index}] 必须是对象")
        source_id = section.get("source_id")
        section_id = section.get("section_id")
        line_range = section.get("line_range")
        text = section.get("text")
        for name, value in (("source_id", source_id), ("section_id", section_id),
                            ("line_range", line_range), ("text", text)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"sections[{index}].{name} 缺失或非字符串")
        cited.setdefault(source_id, []).append((section_id, line_range, text))  # type: ignore[arg-type]
    return cited


def verify_sources(fixture_root: Path, raw_dir: Path) -> VerificationResult:
    """核对整份官方快照目录。纯本地：只读 JSON 与 `--raw-dir` 里的文件。"""
    import json

    result = VerificationResult()
    by_sha, by_name = index_raw_dir(raw_dir)

    for snapshot_path in sorted((fixture_root / "sources").glob("*.json")):
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        provenance = payload.get("provenance")
        if not isinstance(provenance, Mapping):
            result.problems.append(f"{snapshot_path.name}: 没有 provenance 块")
            continue
        documents = provenance.get("documents")
        if not isinstance(documents, list):
            result.problems.append(f"{snapshot_path.name}: documents 必须是列表")
            continue
        cited = collect_citations(payload)

        for document in documents:
            if not isinstance(document, Mapping):
                result.problems.append(f"{snapshot_path.name}: documents 元素必须是对象")
                continue
            source_id = str(document["source_id"])
            cited_for_doc = cited.get(source_id, [])
            # 登记值先记下来（供工件使用）；**判定仍以实际算出的摘要为准**。
            result.declarations[source_id] = (
                str(document["raw_sha256"]),
                str(document["blob_sha"]),
            )
            result.line_ranges[source_id] = tuple(
                sorted({item[1] for item in cited_for_doc}, key=_line_range_sort_key)
            )
            # 先按内容匹配（任何文件名都行），再按文件名兜底 —— 后者才能把
            # "内容对不上" 报成问题而不是"文件缺失"。
            raw_path = by_sha.get(str(document["raw_sha256"])) or by_name.get(
                Path(str(document["path"])).name
            )
            if raw_path is None:
                result.missing.append(source_id)
                continue
            problems = check_document(
                source_id=source_id,
                raw_sha256=str(document["raw_sha256"]),
                blob_sha=str(document["blob_sha"]),
                line_count=document.get("raw_lines"),
                raw_bytes=raw_path.read_bytes(),
                cited=cited_for_doc,
            )
            result.problems.extend(problems)
            if not problems:
                result.verified.append((source_id, str(raw_path), len(cited_for_doc)))
    return result


def _line_range_sort_key(line_range: str) -> tuple[int, int]:
    if "-" in line_range:
        start_text, end_text = line_range.split("-", 1)
        return int(start_text), int(end_text)
    value = int(line_range)
    return value, value


# ---- 生成外部验证证明（P2.2-1） ----


def fixture_fingerprint(fixture_root: Path) -> tuple[str, list[dict[str, str]], str]:
    """算出 `(INDEX.json 字节摘要, snapshot_files[], snapshot_set_id)`。

    直接读磁盘字节、不经过任何库的加载器：绑定校验要**独立于被测实现**，
    否则"库解析错了"会同时污染两端，校验就失去意义。
    """
    index_path = fixture_root / INDEX_FILENAME
    if not index_path.is_file():
        raise ValueError(f"找不到 {index_path}：无法确定被证明的快照文件集合")
    index_sha = sha256_bytes(index_path.read_bytes())
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{index_path} 的顶层必须是 JSON 对象")

    snapshot_set = payload.get("snapshot_set")
    if not isinstance(snapshot_set, str) or not snapshot_set.strip():
        raise ValueError(
            f"{index_path} 缺少 snapshot_set：工件的 snapshot_set_id 必须与快照集对齐，"
            f"请先在 INDEX.json 里登记（这是**读取**既有字段，不是新增）"
        )

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"{index_path}.candidates 必须是非空列表")

    files: list[dict[str, str]] = []
    for position, item in enumerate(candidates):
        if not isinstance(item, Mapping):
            raise TypeError(f"{INDEX_FILENAME}.candidates[{position}] 必须是 JSON 对象")
        relative = item.get("fixture_path")
        candidate_id = item.get("candidate_id")
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError(f"{INDEX_FILENAME}.candidates[{position}].fixture_path 必须是非空字符串")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValueError(f"{INDEX_FILENAME}.candidates[{position}].candidate_id 必须是非空字符串")
        target = fixture_root / relative
        if not target.is_file():
            raise ValueError(f"快照文件不存在：{target}")
        files.append(
            {
                "path": relative,
                "sha256": sha256_bytes(target.read_bytes()),
                "candidate_id": candidate_id,
            }
        )
    files.sort(key=lambda item: item["path"])
    return index_sha, files, snapshot_set


def build_attestation_payload(
    fixture_root: Path, result: VerificationResult
) -> dict[str, object]:
    """把核对结果 + 当前磁盘字节压成工件 payload（**全字段确定，零时间字段**）。"""
    index_sha, snapshot_files, snapshot_set = fixture_fingerprint(fixture_root)
    overall = result.overall_result  # problems 非空时这里会抛错 —— 正是想要的

    verified_ids = {item[0] for item in result.verified}
    missing_ids = set(result.missing)
    documents: list[dict[str, object]] = []
    for source_id in sorted(result.declarations):
        raw_sha256, blob_sha = result.declarations[source_id]
        if source_id in verified_ids:
            document_result = RESULT_PASSED
        elif source_id in missing_ids:
            document_result = RESULT_INCOMPLETE
        else:
            # 既不在 verified 也不在 missing：说明该文档这一轮根本没被处理过
            # （例如上面 `documents 元素必须是对象` 之类的结构问题）。
            # 不再猜，直接拒绝生成 —— 工件的每个字段都必须是确定核对出来的。
            raise ValueError(f"文档 {source_id!r} 没有核对结论，拒绝生成工件")
        documents.append(
            {
                "source_id": source_id,
                "raw_sha256": raw_sha256,
                "blob_sha": blob_sha,
                "line_ranges": list(result.line_ranges.get(source_id, ())),
                "result": document_result,
            }
        )

    if overall == RESULT_PASSED and (missing_ids or documents == []):
        raise ValueError("不得在存在缺失时生成 passed 工件（W4：passed 只能被核对出来）")

    return {
        "schema": ATTESTATION_SCHEMA,
        "snapshot_set_id": snapshot_set,
        "index_sha256": index_sha,
        "snapshot_files": snapshot_files,
        "verifier": VERIFIER_ID,
        "verifier_version": VERIFIER_VERSION,
        "algorithm": VERIFIER_ALGORITHM,
        "documents": documents,
        "overall_result": overall,
    }


def attestation_bytes(payload: Mapping[str, object]) -> bytes:
    """规范序列化：`sort_keys` + 2 空格缩进 + 末尾换行 + UTF-8（无 BOM）+ LF。

    与 `research/attestation.py::canonical_json_bytes` 必须**逐字节相同** ——
    两侧各写一份是为了让审计工具不依赖被测库，一致性由测试钉住。
    """
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def _resolve_existing(target: Path, data: bytes) -> tuple[bool, str]:
    """目标已存在时按 W2 处置：**逐字节相同 → 幂等成功；不同 → 拒绝**。

    这是**唯一**处理"目标已存在"的分支，因此 W1 只有一处判定点
    —— 发布动作（`os.link`）在目标存在时**必然**走到这里，不会覆盖。
    """
    if not target.is_file():
        # 目标在判定期间消失：状态不确定，拒绝而不是猜
        return False, f"{target} 在判定期间消失：状态不确定，拒绝写入"
    existing = target.read_bytes()
    if existing == data:
        return True, f"已存在且内容逐字节相同，幂等成功（未写盘）：{target}"
    return False, (
        f"{target} 已存在且内容不同：拒绝覆盖。attestation 的写入是**显式人工动作**，"
        f"请先人工删除旧文件（该动作会进 git 历史）再重新生成"
    )


def write_attestation(target: Path, data: bytes) -> tuple[bool, str]:
    """按 W1–W3 落盘，返回 `(是否成功, 说明)`。

    三种情形：

    | 情形 | 行为 |
    |---|---|
    | 目标已存在，内容**逐字节相同** | 幂等成功，**不写盘**（W2） |
    | 目标已存在，内容**不同** | 拒绝（W1 + W2）—— 没有任何"强制覆盖"口子 |
    | 目标不存在 | 同目录临时文件 + `fsync`，然后 **`os.link` 原子排他发布** |

    **为什么是 `os.link` 而不是 `os.replace`**（P1-2 复核修正的核心）：
    `os.replace` 是**无条件覆盖**语义 —— "先检查目标不存在、再 replace"在两者之间
    留了一个真实窗口，窗口内出现的文件会被静默覆盖，因此那个写法与"禁止覆盖"**不等价**。
    `os.link(temp, target)` 则**原子且排他**：目标已存在时它以 `FileExistsError` 失败，
    于是"有没有别人先到"由**发布动作本身**判定，而不是由事先的检查判定 —— 窗口消失。
    硬链接成功后临时文件被删除，目标保留同一份内容。

    **为什么 `os.link` 不可用时 fail-closed，而不是退化成 `O_EXCL` 直写**（W3 复核修正的核心）：
    `O_CREAT | O_EXCL` 只有**排他性**、**没有中断原子性** —— 进程在写完 `fsync` 之前
    被强杀会留下半写的目标文件，而它看起来像一份"已生成的工件"，
    后续校验只能报"内容不符"，无法与"文件被篡改"区分。
    原子写入是既批准过的硬约束，**实现不得自行降级**：`os.link` 抛任何
    `OSError` 都清理临时文件并**明确失败**（调用方据此返回非零退出码），
    把"该文件系统不支持原子排他发布"暴露给人。

    异常路径始终清理临时文件：半写状态的"证明"比没有更糟（无法判定有效性）。
    """
    if target.exists():
        return _resolve_existing(target, data)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=".tmp-", suffix=".part", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, target)
        except FileExistsError:
            # 竞态：发布动作本身发现"目标已存在" ⇒ 按已存在规则处置，绝不覆盖
            return _resolve_existing(target, data)
        except OSError as exc:
            # 文件系统不支持硬链接（跨设备等）：**不做非原子退化**，fail-closed。
            return False, (
                f"{target} 发布失败：os.link 不可用（{exc}）。"
                f"W3 要求原子排他发布，**不降级**为 O_EXCL 直写"
                f"（后者只有排他性、没有中断原子性，强杀会留下半写文件）。"
                f"请把 --write-attestation 指向普通本地文件系统路径后重试"
            )
        return True, f"已原子排他创建（os.link 发布）：{target}"
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线复核官方资料快照的来源锚点（仅标准库，不联网）")
    parser.add_argument("--root", default=str(DEFAULT_FIXTURE_ROOT), help="fixtures/official 目录")
    parser.add_argument("--raw-dir", required=True, help="存放原始文件（raw_url 下载结果）的目录")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="允许部分来源在 --raw-dir 里缺失（默认缺失即失败）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument(
        "--write-attestation",
        default=None,
        metavar="PATH",
        help=(
            "把核对结论落成外部验证证明（C6 工件）。opt-in：不传则只核对、不写盘。"
            "已存在的文件不会被覆盖；内容逐字节相同视为幂等成功。"
        ),
    )
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir)
    if not raw_dir.is_dir():
        print(f"找不到原始文件目录：{raw_dir}", file=sys.stderr)
        return 1

    fixture_root = Path(args.root).resolve()
    result = verify_sources(fixture_root, raw_dir.resolve())
    accepted = result.passed(allow_missing=args.allow_missing)

    notice: str | None = None
    if args.write_attestation:
        if not accepted:
            reason = "存在不符项" if result.problems else "存在缺失（未加 --allow-missing）"
            print(
                f"拒绝生成 attestation：{reason}。"
                f"工件只承载**已核对通过**的结论，不得为失败的核对留下看似正式的产物",
                file=sys.stderr,
            )
            notice = "attestation 未生成"
        else:
            ok, message = _try_write_attestation(Path(args.write_attestation), fixture_root, result)
            print(message, file=sys.stderr)
            notice = message
            if not ok:
                _report(result, raw_dir, args)
                return 1

    _report(result, raw_dir, args, notice=notice)
    return 0 if accepted else 1


def _try_write_attestation(
    target: Path, fixture_root: Path, result: VerificationResult
) -> tuple[bool, str]:
    """生成 → 序列化 → 落盘。结构性问题（如 INDEX 缺 snapshot_set）视为拒绝写。"""
    try:
        payload = build_attestation_payload(fixture_root, result)
    except (TypeError, ValueError) as exc:
        return False, f"拒绝生成 attestation：{exc}"
    return write_attestation(target, attestation_bytes(payload))


def _report(
    result: VerificationResult, raw_dir: Path, args: argparse.Namespace, *, notice: str | None = None
) -> None:
    if args.json:
        print(json.dumps(result.to_dict(allow_missing=args.allow_missing), ensure_ascii=False, indent=2))
        return
    for source_id, raw_path, count in result.verified:
        print(f"OK   {source_id:32s} {count} 条摘录逐字节一致  <- {raw_path}")
    for source_id in result.missing:
        print(f"MISS {source_id:32s} 在 {raw_dir} 里找不到 sha256 匹配的原始文件")
    for problem in result.problems:
        print(f"FAIL {problem}", file=sys.stderr)
    print(
        f"来源核对：{len(result.verified)} 份通过，{len(result.missing)} 份缺失，"
        f"{len(result.problems)} 个问题"
    )
    if notice is not None:
        print(f"attestation：{notice}")


if __name__ == "__main__":
    raise SystemExit(main())

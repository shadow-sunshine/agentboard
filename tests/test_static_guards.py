"""P1A 静态守卫（AST）：无网络、无时钟、核心层不读 payload 业务键、快照 URL 受控。

守卫函数都是纯函数（输入源码字符串，输出违规点），因此可以用**合成源码**做正/负控制，
在被守卫的代码尚未存在时也能证明守卫本身有效，而不是一个永远通过的空壳。

豁免说明（两处，都与实际用法绑定并各有一条"恰好相等"的断言）：

1. `src/agentboard/events.py` 的 `BoardEvent.timestamp` 用 `time.time` 作为默认值 ——
   那是事件墙钟时间，不参与证据确定性（`evidence_id` 用快照元数据 `captured_at`，
   不是 `now()`）。见 `test_clock_allowlist_matches_actual_usage`。
2. `src/agentboard/providers/http_transport.py` 允许 `http` / `ssl` —— P2.1
   真实资料检索必须有一个网络出口，而需求是"网络访问只允许出现在 Provider 适配层"。
   豁免的粒度是**单个文件**，且只放行标准库（`requests`/`httpx` 等永不豁免）。
   见 `NETWORK_IMPORT_ALLOWLIST` 与 `test_network_allowlist_matches_actual_usage`。

URL 守卫分两层（真实资料接入后收紧过一次）：

- `fixtures/research/`（P1A 人工摘要）：**完全本地**，出现 `://` 即失败；
- `fixtures/official/`（官方文档摘录）：允许出现 URL，但只允许出现在 provenance 的
  documents 里，且主机与仓库必须在 `sources.ALLOWED_SOURCE_*` 白名单内；
  我们自己撰写的字段（claim / derivation / reason / note 等）里不允许夹带 URL ——
  那是"把结论写成看起来有出处"的典型形态。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "agentboard"
FIXTURES_ROOT = PROJECT_ROOT / "fixtures"
P1A_FIXTURE_ROOT = FIXTURES_ROOT / "research"
OFFICIAL_FIXTURE_ROOT = FIXTURES_ROOT / "official"

#: 我们**自己撰写**的字段：里面不允许出现 URL（摘录原文 text 是上游内容，不在此列）。
AUTHORED_TEXT_FIELDS = frozenset(
    {
        "claim",
        "derivation",
        "reason",
        "release_tag_note",
        "upstream_latest_release_note",
        "upstream_license_note",
        "collection_method",
        "source_note",
        "note",
        "title",
    }
)

URL_HOST_PATTERN = re.compile(r"://([A-Za-z0-9.\-]+)")

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "urllib3",
        "socket",
        "http",
        "ssl",
        "ftplib",
        "smtplib",
        "telnetlib",
        "websockets",
    }
)

#: 网络库 import 的**唯一**豁免（P2.1 真实资料检索）。
#:
#: 需求是"网络访问只允许出现在 Provider 适配层"，而 P1A 的守卫是"全仓禁用"。
#: 两者的调和方式不是把守卫删掉/放宽，而是把它从"一刀切"改成
#: **一个文件 + 一组注明用途的模块名**：
#:
#: - 键是**仓库根相对路径**，粒度是文件（不是目录、不是包）；
#: - 值是豁免的模块名，必须是精确集合，不是"允许任意网络库"；
#: - 豁免**不覆盖** `requests` / `httpx` / `aiohttp` 等第三方库 ——
#:   `dependencies = []` 是硬约束，豁免的是标准库；
#: - `test_network_allowlist_matches_actual_usage` 断言该文件**真实 import 的
#:   网络库恰好等于**豁免集合：多引一个会失败，少引一个（豁免腐化，文件被改成
#:   不联网后白名单还留着）也会失败。这与 `CLOCK_ALLOWLIST` 是同一套机制。
NETWORK_IMPORT_ALLOWLIST: dict[str, frozenset[str]] = {
    "src/agentboard/providers/http_transport.py": frozenset({"http", "ssl"}),
}

#: Provider 层的"网络出口"只有这一个文件 ⇒ 也只有这个文件可以 `import ssl`。
#: 单列出来是为了让"多一个文件偷偷开网络"这件事有独立的失败信息。
NETWORK_TRANSPORT_FILE = "src/agentboard/providers/http_transport.py"

#: 核心层与领域层**不得** import Provider 层。
#: 依赖方向必须是单向的 `providers → core`；一旦反向，P1A 的离线评测就会
#: 间接依赖网络代码，`dependencies = []` 与"离线可复现"同时失效。
PROVIDER_PACKAGE = "agentboard.providers"
NON_PROVIDER_SRC_PREFIXES = ("src/agentboard/",)
PROVIDER_PREFIX = "src/agentboard/providers/"

FORBIDDEN_CLOCK_USAGE = frozenset(
    {
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.perf_counter",
        "datetime.now",
        "datetime.utcnow",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "date.today",
        "datetime.date.today",
    }
)

CLOCK_ALLOWLIST: dict[str, frozenset[str]] = {
    "src/agentboard/events.py": frozenset({"time.time"}),
}

PAYLOAD_FIELD = "structured_payload"
PAYLOAD_ALLOWED_PREFIXES = ("src/agentboard/contracts.py", "src/agentboard/research/")

# P1A-1：非 fixture 的 source_type 只允许在「唯一定义处」与「唯一构造/迁移处」被引用，
# 防止 synthetic / legacy 证据在别处散落拼装。
# (字面量, 常量名, 允许出现的文件)
SOURCE_TYPE_DISPERSION_GUARDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "synthetic_mock",
        "SOURCE_SYNTHETIC",
        ("src/agentboard/evidence.py", "src/agentboard/workers/synthetic.py"),
    ),
    (
        "legacy_text",
        "SOURCE_LEGACY",
        ("src/agentboard/evidence.py", "src/agentboard/research/migrate.py"),
    ),
)


def _dotted(node: ast.AST) -> str:
    """把 `a.b.c` 形式的属性访问链渲染成点号字符串；无法解析时返回空串。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def find_forbidden_imports(source: str) -> list[str]:
    """检出被禁的网络相关 import（含 `from x import y` 与相对 import 的区分）。

    **纯函数，不看文件路径**：豁免由调用方（扫描测试 / `find_forbidden_imports_in`）
    按 `NETWORK_IMPORT_ALLOWLIST` 逐文件应用。这样守卫本身仍然是"给一段源码就能判定"，
    可以用合成源码做正负控制。
    """
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return [f"forbidden import: {name}" for name in sorted(roots & FORBIDDEN_IMPORT_ROOTS)]


def network_imports_in(source: str) -> frozenset[str]:
    """列出源码里真实 import 的**网络库**模块名（不含被禁以外的普通模块）。

    与 `find_forbidden_imports` 的区别：那个返回"违规描述"，这个返回"实际用到的网络库集合"。
    `test_network_allowlist_matches_actual_usage` 需要后者才能做"恰好等于"的断言。
    """
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return frozenset(roots & FORBIDDEN_IMPORT_ROOTS)


def find_forbidden_imports_in(source: str, rel: str) -> list[str]:
    """按文件路径应用豁免后，检出违规的网络 import。"""
    allowed = NETWORK_IMPORT_ALLOWLIST.get(rel, frozenset())
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    offenders = roots & FORBIDDEN_IMPORT_ROOTS
    # 豁免只放行"该文件被明确登记过的那几个模块"；其余网络库照旧违规。
    # 这一点很关键：否则给一个文件开了 `http`，它顺手 `import socket` 也就合法了。
    return [f"forbidden import: {name}" for name in sorted(offenders - allowed)]


def find_provider_imports(source: str, rel: str) -> list[str]:
    """检出核心层 / 领域层对 Provider 层的反向 import。"""
    if not rel.startswith(NON_PROVIDER_SRC_PREFIXES) or rel.startswith(PROVIDER_PREFIX):
        return []
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PROVIDER_PACKAGE or alias.name.startswith(f"{PROVIDER_PACKAGE}."):
                    hits.add(f"{rel}: 不得 import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module is None:
                continue
            module = node.module or ""
            if module == PROVIDER_PACKAGE or module.startswith(f"{PROVIDER_PACKAGE}."):
                hits.add(f"{rel}: 不得 from {module} import ...")
    return sorted(hits)


def find_forbidden_clock_usage(source: str) -> list[str]:
    """检出时钟依赖。

    遍历全部 `Attribute` / `Name` 节点（而非只看 `Call`），因此 `default_factory=time.time`
    这种**引用而不调用**的写法同样会被抓到。
    """
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Name)):
            dotted = _dotted(node)
            if dotted in FORBIDDEN_CLOCK_USAGE:
                hits.add(dotted)
    return [f"forbidden clock usage: {name}" for name in sorted(hits)]


def find_payload_key_access(source: str, rel: str) -> list[str]:
    """核心层不得读取 `structured_payload` 的业务键（只有 contracts / research 可以）。

    两种形态都拒绝：
    - `card.structured_payload["findings"]`（属性下标）
    - `state["structured_payload"]`（字符串键下标）
    """
    if any(rel.startswith(prefix) for prefix in PAYLOAD_ALLOWED_PREFIXES):
        return []
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        key = node.slice
        if isinstance(key, ast.Constant) and key.value == PAYLOAD_FIELD:
            hits.add(f"forbidden payload key subscript in {rel}")
        value = node.value
        while isinstance(value, ast.Attribute):
            if value.attr == PAYLOAD_FIELD:
                hits.add(f"forbidden payload field access in {rel}")
                break
            value = value.value
    return sorted(hits)


def find_scattered_evidence_construction(
    source: str, rel: str, token: str, constant: str, allowed: tuple[str, ...]
) -> list[str]:
    """检出在「允许清单」之外**拼装**非 fixture 证据的代码。

    两条规则：

    1. 出现硬编码的 `source_type` 字面量（`"synthetic_mock"` / `"legacy_text"`）；
    2. 在 `EvidenceRef(...)` 的 `source_type=` 参数里引用对应常量。

    只做**只读比较**（例如完整性检查里判断 `ref.source_type != SOURCE_LEGACY`）不算违规。
    早先的版本把"任何常量引用"都判为违规，结果在 `integrity.py` 上误报 ——
    会误报的守卫迟早被绕过，精确的守卫才有价值。

    已知覆盖边界：拦截的是直接构造（字面量 / 关键字参数）。若有人先把常量存进变量、
    再经 `EvidenceRef.from_dict({"source_type": ...})` 绕一圈，此守卫看不出来。
    """
    if rel in allowed:
        return []
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == token:
            hits.add(f"{rel}: 硬编码 source_type 字面量 {token!r}")
        elif isinstance(node, ast.Call):
            func = node.func
            callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if callee != "EvidenceRef":
                continue
            for keyword in node.keywords:
                if keyword.arg != "source_type":
                    continue
                value = keyword.value
                referenced = (
                    value.id if isinstance(value, ast.Name) else getattr(value, "attr", "")
                )
                if referenced == constant:
                    hits.add(
                        f"{rel}: 在 EvidenceRef 构造里引用了 {constant}，请改走对应工厂 / 迁移器"
                    )
    return sorted(hits)


# P2.2-1（L3）：`EvidenceRef(...)` 的**字面构造**只允许出现在这些文件。
#
# 理由：字段检查（"断言某类记录不含 locator/excerpt"）挡不住"从别处拿值填进去"，
# 而"只能由受控工厂产出"才是有约束力的边界。限定构造点之后，
# 新增一处证据构造必然要显式改这张表 —— 那是一个会被评审看到的动作，而不是静默扩散。
EVIDENCE_REF_DEFINITION_MODULE = "src/agentboard/evidence.py"
EVIDENCE_REF_FACTORY_ALLOWED_FILES: tuple[str, ...] = (
    # 定义处：类自身所在的模块。今天它并不字面构造（构造都在下面几个工厂里），
    # 但"在类自己家里构造"是唯一不可能算作"散落拼装"的位置，故预置放行。
    EVIDENCE_REF_DEFINITION_MODULE,
    # 既有 P1A fixture 证据工厂
    "src/agentboard/research/fixtures.py",
    # 历史自由文本的迁移器
    "src/agentboard/research/migrate.py",
    # P2.2-1 新增的研究级受控工厂（evidence_from_excerpt / promotion_checks_for）
    "src/agentboard/research/sources.py",
    # 确定性占位 worker
    "src/agentboard/workers/synthetic.py",
)

# P2.2-1（L4）：Provider 层不得引用证据契约（反向守卫）。
#
# 为什么"依赖方向已经保证了"还不够：`test_core_and_research_do_not_import_providers`
# 管的是"领域层不要 import Provider"，方向相反的这一侧**没有**守卫。
# 而 P2.1 采集层与 P2.2 证据层之间刻意**没有**通路（仓库搜索摘要不是技术结论），
# 这条守卫把这个"没有"从"暂时没人这么写"变成"这么写会失败"。
PROVIDERS_EVIDENCE_GUARD_PREFIX = "src/agentboard/providers/"
EVIDENCE_MODULE = "agentboard.evidence"
FORBIDDEN_EVIDENCE_NAMES: frozenset[str] = frozenset(
    {
        "EvidenceRef",
        "compute_evidence_id",
        "compute_digest",
        "is_trusted_evidence",
        "SOURCE_FIXTURE",
        "SOURCE_SYNTHETIC",
        "SOURCE_LEGACY",
        "TRUSTED_SOURCE_TYPES",
    }
)


def find_scattered_evidence_ref_construction(
    source: str, rel: str, allowed: tuple[str, ...]
) -> list[str]:
    """检出白名单之外对 `EvidenceRef(...)` 的**字面构造**（L3）。

    只认 `EvidenceRef(...)` 这种"名字 + 调用"形态：

    - `EvidenceRef.from_dict(...)` 不算（`func` 是 `Attribute`，不是 `Name`）——
      反序列化不是构造，它必然带完整的既有字段，且 `__post_init__` 会重算摘要；
    - 类型注解、`isinstance`、只读比较都不算。

    已知覆盖边界（与 `find_scattered_evidence_construction` 同）：若有人先绑定别名
    （`F = EvidenceRef`）再调用，此守卫看不出来。它拦的是**直接写法**，
    而那正是"顺手在别处拼一条证据"的实际形态。
    """
    if rel in allowed:
        return []
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "EvidenceRef":
            hits.add(
                f"{rel}: 直接构造 EvidenceRef(...) —— 请改走受控工厂"
                f"（research.sources.evidence_from_excerpt / 指定工厂），"
                f"或显式把本文件登记进 EVIDENCE_REF_FACTORY_ALLOWED_FILES"
            )
    return sorted(hits)


def find_evidence_references_in_providers(source: str, rel: str) -> list[str]:
    """检出 `src/agentboard/providers/**` 对证据契约的引用（L4 反向守卫）。

    看的是**代码引用**：import（绝对 + 相对）与对禁用名字的 `Name` / `Attribute` 使用。
    **文档字符串与注释里的提及不算** —— `providers/__init__.py` 的 docstring 正是靠
    解释"本包不生成 EvidenceRef"来传达边界，若把文字也算违规，
    守卫会逼着人删掉解释，那就本末倒置了。
    """
    if not rel.startswith(PROVIDERS_EVIDENCE_GUARD_PREFIX):
        return []
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == EVIDENCE_MODULE or alias.name.startswith(f"{EVIDENCE_MODULE}."):
                    hits.add(f"{rel}: 不得 import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == EVIDENCE_MODULE:
                hits.add(f"{rel}: 不得 from {EVIDENCE_MODULE} import ...")
            elif node.level and module == "evidence":
                hits.add(f"{rel}: 不得 from ..evidence import ...")
            for alias in node.names:
                if alias.name in FORBIDDEN_EVIDENCE_NAMES:
                    hits.add(f"{rel}: 不得 import 证据契约名 {alias.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_EVIDENCE_NAMES:
            hits.add(f"{rel}: 不得引用证据契约名 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_EVIDENCE_NAMES:
            hits.add(f"{rel}: 不得引用证据契约属性 {node.attr}")
    return sorted(hits)


# P2.2-1（L5）：attestation 的**绑定状态**不得在加载器之外被写入。
#
# 背景（P1-1 复核修正）：绑定结论原本是 `SourceVerificationAttestation` 的一个公开字段，
# 于是 `SourceVerificationAttestation(..., binding_reason="")` 就能绕过文件绑定。
# 现在该字段改为 `field(init=False)` —— 构造器签名里没有它，手拼对象恒为 None（fail-closed）。
# 但 Python 仍允许 `object.__setattr__` 绕过，因此补这条守卫，
# 把"能力边界"从"类型上不可能"收敛到**"受控公开 API + 静态守卫下不可合法生成"**。
ATTESTATION_BINDING_ALLOWED_FILES: tuple[str, ...] = ("src/agentboard/research/attestation.py",)
#: 只有加载器有权写的属性名（字段改名时这里也要同步）
BINDING_ATTR_NAMES: frozenset[str] = frozenset({"binding", "binding_reason", "binding_detail"})
_BINDING_CLASS_NAME = "AttestationBinding"
_SETATTR_METHOD = "__setattr__"


def find_out_of_band_attestation_binding(
    source: str, rel: str, allowed: tuple[str, ...]
) -> list[str]:
    """检出在加载器之外构造 / 写入 attestation 绑定状态的代码（L5）。

    两条规则，都只看**真实调用**（docstring / 注释里的示例不算）：

    1. 构造 `AttestationBinding(...)`；
    2. 调用 `object.__setattr__(x, "binding", ...)`（第 2 个实参是绑定属性名的字符串字面量）。

    覆盖边界与 L3 同构：别名绑定（`F = object.__setattr__`）看不出来。
    它拦的是**直接写法** —— 而那正是"顺手把绑定状态设上去"的实际形态。
    """
    if rel in allowed:
        return []
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == _BINDING_CLASS_NAME:
            hits.add(
                f"{rel}: 直接构造 {_BINDING_CLASS_NAME}(...) —— 绑定状态只能由 "
                f"attestation.load_attestation() 建立；请改走该入口"
            )
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == _SETATTR_METHOD
            and isinstance(func.value, ast.Name)
            and func.value.id == "object"
            and len(node.args) >= 2
        ):
            second = node.args[1]
            if isinstance(second, ast.Constant) and second.value in BINDING_ATTR_NAMES:
                hits.add(
                    f"{rel}: 在加载器之外 object.__setattr__(..., {second.value!r}) —— "
                    f"这会绕过文件级绑定（C6），请改走 attestation.load_attestation()"
                )
    return sorted(hits)


def _iter_src_files() -> list[Path]:
    return sorted(path for path in SRC_ROOT.rglob("*.py") if path.is_file())


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _iter_fixture_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.json")
        if path.is_file() and path.relative_to(FIXTURES_ROOT).parts[0] != "baselines"
    )


def find_url_hosts(source: str) -> list[str]:
    """列出字符串里出现的全部 URL 主机（去重排序），供白名单守卫使用。"""
    return sorted(set(URL_HOST_PATTERN.findall(source)))


def find_document_url_hosts(payload: Any) -> list[str]:
    """取出 `provenance.documents[*].url / raw_url` 的主机（只看被声明的来源）。"""
    hosts: set[str] = set()
    if not isinstance(payload, dict):
        return []
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return []
    documents = provenance.get("documents")
    if not isinstance(documents, list):
        return []
    for document in documents:
        if not isinstance(document, dict):
            continue
        for key in ("url", "raw_url"):
            value = document.get(key)
            if isinstance(value, str):
                hosts.update(URL_HOST_PATTERN.findall(value))
    return sorted(hosts)


def find_urls_in_authored_text(node: Any, path: str = "$") -> list[str]:
    """检出我们自己撰写的字段里夹带的 URL。

    摘录原文（`sections[*].text`）是被引用的上游内容，其中的 markdown 链接不算违规；
    但 `claim` / `derivation` / `reason` / `*_note` 这类我们写的字段里出现 URL，
    就等于"用链接充当依据"，必须显式拒绝。
    """
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if isinstance(value, str) and key in AUTHORED_TEXT_FIELDS and "://" in value:
                hits.append(f"{child}: 撰写字面里出现 URL")
            else:
                hits.extend(find_urls_in_authored_text(value, child))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            hits.extend(find_urls_in_authored_text(item, f"{path}[{index}]"))
    return hits


def find_unpinned_source_urls(payload: Any) -> list[str]:
    """检出**没有固定到 commit** 的来源 URL。

    规则：`provenance.documents[*].url` / `raw_url` 里必须出现该文档的 `commit_sha`。
    用分支名（`master` / `v3.0.x`）当定位符，复核时可能拿到与采集当天不同的内容，
    于是"行号 + sha256"这套锚点全部失效 —— 这条守卫就是为了让那种写法无法进仓库。
    """
    hits: list[str] = []
    if not isinstance(payload, dict):
        return hits
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return hits
    documents = provenance.get("documents")
    if not isinstance(documents, list):
        return hits
    for document in documents:
        if not isinstance(document, dict):
            continue
        source_id = document.get("source_id")
        commit = document.get("commit_sha")
        if not isinstance(commit, str):
            hits.append(f"{source_id}: 缺少 commit_sha")
            continue
        for key in ("url", "raw_url"):
            value = document.get(key)
            if isinstance(value, str) and commit not in value:
                hits.append(f"{source_id}.{key}: 未固定到 commit {commit[:8]}…（{value}）")
    return hits


# ---- 守卫本身的正向 / 负向控制 ----


def test_network_guard_flags_synthetic_source() -> None:
    assert find_forbidden_imports("import requests\n") == ["forbidden import: requests"]
    assert find_forbidden_imports("from urllib.parse import urljoin\n") == [
        "forbidden import: urllib"
    ]
    assert find_forbidden_imports("import socket\n") == ["forbidden import: socket"]
    assert find_forbidden_imports("import http.client\n") == ["forbidden import: http"]


def test_network_guard_allows_clean_source() -> None:
    clean = "import json\nfrom pathlib import Path\nfrom .util import freeze_payload\n"
    assert find_forbidden_imports(clean) == []


def test_clock_guard_flags_synthetic_source() -> None:
    assert find_forbidden_clock_usage("import time\nx = time.time()\n") == [
        "forbidden clock usage: time.time"
    ]
    assert find_forbidden_clock_usage("import time\nf = field(default_factory=time.time)\n") == [
        "forbidden clock usage: time.time"
    ]
    assert "forbidden clock usage: datetime.datetime.now" in find_forbidden_clock_usage(
        "import datetime\ny = datetime.datetime.now(None)\n"
    )
    assert "forbidden clock usage: date.today" in find_forbidden_clock_usage(
        "from datetime import date\nz = date.today()\n"
    )


def test_clock_guard_allows_plain_constants() -> None:
    assert find_forbidden_clock_usage('CAPTURED_AT = "2026-01-01"\n') == []


def test_payload_guard_flags_synthetic_source() -> None:
    assert find_payload_key_access(
        'x = card.structured_payload["findings"]\n', "src/agentboard/board.py"
    ) == ["forbidden payload field access in src/agentboard/board.py"]
    assert find_payload_key_access(
        'y = state["structured_payload"]\n', "src/agentboard/board.py"
    ) == ["forbidden payload key subscript in src/agentboard/board.py"]


def test_payload_guard_allows_contracts_and_research() -> None:
    source = 'x = card.structured_payload["findings"]\n'
    assert find_payload_key_access(source, "src/agentboard/contracts.py") == []
    assert find_payload_key_access(source, "src/agentboard/research/payload.py") == []


def test_source_type_guard_flags_literal_outside_factory() -> None:
    literal = 'REF = EvidenceRef(source_type="synthetic_mock", source_id="mock:x")\n'
    assert find_scattered_evidence_construction(
        literal, "src/agentboard/board.py", "synthetic_mock", "SOURCE_SYNTHETIC", ()
    ) == ["src/agentboard/board.py: 硬编码 source_type 字面量 'synthetic_mock'"]

    orphan = 'TOKEN = "legacy_text"\n'
    assert find_scattered_evidence_construction(
        orphan, "src/agentboard/board.py", "legacy_text", "SOURCE_LEGACY", ()
    ) == ["src/agentboard/board.py: 硬编码 source_type 字面量 'legacy_text'"]


def test_source_type_guard_flags_construction_with_constant() -> None:
    source = "from .evidence import SOURCE_LEGACY\nREF = EvidenceRef(source_type=SOURCE_LEGACY)\n"
    assert find_scattered_evidence_construction(
        source, "src/agentboard/board.py", "legacy_text", "SOURCE_LEGACY", ()
    ) == [
        "src/agentboard/board.py: 在 EvidenceRef 构造里引用了 SOURCE_LEGACY，请改走对应工厂 / 迁移器"
    ]


def test_source_type_guard_allows_read_only_comparison() -> None:
    """只读比较（完整性检查判断 source_type）不是拼装，不得误报。"""
    readonly = (
        "from .evidence import SOURCE_LEGACY\n"
        "def check(refs):\n"
        "    return [r for r in refs if r.source_type != SOURCE_LEGACY]\n"
    )
    assert (
        find_scattered_evidence_construction(
            readonly, "src/agentboard/research/integrity.py", "legacy_text", "SOURCE_LEGACY", ()
        )
        == []
    )


def test_source_type_guard_allows_designated_files() -> None:
    literal = 'REF = EvidenceRef(source_type="synthetic_mock", source_id="mock:x")\n'
    allowed = ("src/agentboard/evidence.py", "src/agentboard/workers/synthetic.py")
    assert (
        find_scattered_evidence_construction(
            literal,
            "src/agentboard/workers/synthetic.py",
            "synthetic_mock",
            "SOURCE_SYNTHETIC",
            allowed,
        )
        == []
    )
    assert (
        find_scattered_evidence_construction(
            literal, "src/agentboard/evidence.py", "synthetic_mock", "SOURCE_SYNTHETIC", allowed
        )
        == []
    )


# ---- 真实仓库扫描 ----


def test_src_has_no_network_imports() -> None:
    """全仓（除显式豁免的文件外）不得出现网络库 import。"""
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        source = path.read_text(encoding="utf-8")
        offenders.extend(f"{rel}: {item}" for item in find_forbidden_imports_in(source, rel))
    assert offenders == []


def test_network_allowlist_only_covers_the_transport_file() -> None:
    """豁免必须精确：只允许 transport 文件，且只能是标准库模块。"""
    assert set(NETWORK_IMPORT_ALLOWLIST) == {NETWORK_TRANSPORT_FILE}
    third_party = {"requests", "httpx", "aiohttp", "urllib3", "websockets"}
    for rel, allowed in NETWORK_IMPORT_ALLOWLIST.items():
        assert (PROJECT_ROOT / rel).is_file(), f"{rel} 不存在"
        leaked = allowed & third_party
        assert not leaked, f"{rel} 豁免了第三方库 {sorted(leaked)}（dependencies=[] 不允许）"


def test_network_allowlist_matches_actual_usage() -> None:
    """豁免项必须与实际用法**恰好相等**（与时钟豁免同一套机制）。

    多引一个网络库会失败；文件被改成不联网后白名单还留着（豁免腐化）也会失败。
    """
    for rel, allowed in NETWORK_IMPORT_ALLOWLIST.items():
        path = PROJECT_ROOT / rel
        actual = network_imports_in(path.read_text(encoding="utf-8"))
        assert actual == set(allowed), (
            f"{rel} 实际网络 import {sorted(actual)} 与豁免 {sorted(allowed)} 不一致"
        )


def test_network_import_allowlist_guard_has_controls() -> None:
    """正/负控制：豁免的文件能过，别的文件不能过；豁免范围外的库照旧被拦。"""
    transport = "import ssl\nfrom http.client import HTTPSConnection\n"
    assert find_forbidden_imports_in(transport, NETWORK_TRANSPORT_FILE) == []
    # 同一个文件 import 未登记的第三方库 → 仍然违规
    assert find_forbidden_imports_in(
        "import requests\nfrom http.client import HTTPSConnection\n",
        NETWORK_TRANSPORT_FILE,
    ) == ["forbidden import: requests"]
    # 别的文件 import 标准库网络模块（含 ssl） → 一律违规
    other = "import http.client\nimport ssl\n"
    assert find_forbidden_imports_in(other, "src/agentboard/board.py") == [
        "forbidden import: http",
        "forbidden import: ssl",
    ]


def test_core_and_research_do_not_import_providers() -> None:
    """依赖方向单向：核心层与领域层不得 import Provider 层。

    一旦反向，P1A 的离线评测会间接依赖联网代码 —— "默认 pytest 全程离线"
    与 `dependencies = []` 会同时失效，而且是悄无声息地失效。
    """
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        offenders.extend(find_provider_imports(path.read_text(encoding="utf-8"), rel))
    assert offenders == []


def test_provider_import_guard_has_controls() -> None:
    core = "from agentboard.providers import Provider\n"
    assert find_provider_imports(core, "src/agentboard/board.py") == [
        "src/agentboard/board.py: 不得 from agentboard.providers import ..."
    ]
    assert find_provider_imports(core, "src/agentboard/research/assemble.py") == [
        "src/agentboard/research/assemble.py: 不得 from agentboard.providers import ..."
    ]
    # Provider 包内部互相 import 不算违规
    assert find_provider_imports(core, "src/agentboard/providers/github_search.py") == []
    # 不相干的模块不受影响
    assert find_provider_imports("import json\n", "src/agentboard/board.py") == []



def test_src_has_no_clock_usage_outside_allowlist() -> None:
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        allowed = CLOCK_ALLOWLIST.get(rel, frozenset())
        for item in find_forbidden_clock_usage(path.read_text(encoding="utf-8")):
            if item.split(": ", 1)[1] in allowed:
                continue
            offenders.append(f"{rel}: {item}")
    assert offenders == []


def test_clock_allowlist_matches_actual_usage() -> None:
    """豁免项必须与实际用法完全一致，避免豁免腐化后放行新增的时钟依赖。"""
    assert CLOCK_ALLOWLIST, "豁免表不应为空"
    for rel, allowed in CLOCK_ALLOWLIST.items():
        path = PROJECT_ROOT / rel
        assert path.is_file(), rel
        actual = {
            item.split(": ", 1)[1]
            for item in find_forbidden_clock_usage(path.read_text(encoding="utf-8"))
        }
        assert actual == set(allowed), f"{rel} 实际时钟用法 {sorted(actual)} 与豁免 {sorted(allowed)} 不一致"


def test_core_does_not_read_payload_keys() -> None:
    offenders: list[str] = []
    for path in _iter_src_files():
        offenders.extend(find_payload_key_access(path.read_text(encoding="utf-8"), _rel(path)))
    assert offenders == []


def test_p1a_fixture_snapshots_have_no_urls() -> None:
    """P1A 人工摘要必须完全本地：出现 `://` 即失败。"""
    offenders = [
        _rel(path)
        for path in _iter_fixture_files(P1A_FIXTURE_ROOT)
        if "://" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_official_fixture_urls_are_allowlisted() -> None:
    """官方快照的 URL 只允许出现在 provenance.documents 里，且主机必须在白名单内。"""
    from agentboard.research.sources import ALLOWED_SOURCE_HOSTS

    offenders: list[str] = []
    for path in _iter_fixture_files(OFFICIAL_FIXTURE_ROOT):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for host in find_document_url_hosts(payload):
            if host not in ALLOWED_SOURCE_HOSTS:
                offenders.append(f"{_rel(path)}: 非白名单来源主机 {host!r}")
    assert offenders == []


def test_official_fixture_has_no_urls_in_authored_text() -> None:
    offenders: list[str] = []
    for path in _iter_fixture_files(OFFICIAL_FIXTURE_ROOT):
        payload = json.loads(path.read_text(encoding="utf-8"))
        offenders.extend(f"{_rel(path)}: {hit}" for hit in find_urls_in_authored_text(payload))
    assert offenders == []


def test_official_fixture_urls_are_pinned_to_commit() -> None:
    """官方来源 URL 必须固定到 commit（这条是"复核能拿到同一份内容"的前提）。"""
    offenders: list[str] = []
    for path in _iter_fixture_files(OFFICIAL_FIXTURE_ROOT):
        payload = json.loads(path.read_text(encoding="utf-8"))
        offenders.extend(f"{_rel(path)}: {hit}" for hit in find_unpinned_source_urls(payload))
    assert offenders == []


def test_url_guard_functions_have_controls() -> None:
    """守卫本身的正/负控制：能抓到坏输入，也能放过好输入。"""
    assert find_url_hosts("见 https://evil.example.com/x") == ["evil.example.com"]
    assert find_url_hosts("没有链接") == []

    bad = {"provenance": {"documents": [{"url": "https://not-allowed.example/a"}]}}
    assert find_document_url_hosts(bad) == ["not-allowed.example"]
    good = {"provenance": {"documents": [{"url": "https://github.com/o/r/blob/m/a.md"}]}}
    assert find_document_url_hosts(good) == ["github.com"]

    assert find_urls_in_authored_text({"claim": "见 https://x.example"}) == [
        "$.claim: 撰写字面里出现 URL"
    ]
    assert find_urls_in_authored_text({"sections": [{"text": "引用 https://x.example"}]}) == []
    assert find_urls_in_authored_text({"provenance": {"reason": "没有链接"}}) == []

    sha = "a" * 40
    pinned = {
        "provenance": {
            "documents": [
                {
                    "source_id": "doc_a",
                    "commit_sha": sha,
                    "url": f"https://github.com/o/r/blob/{sha}/a.md",
                    "raw_url": f"https://raw.githubusercontent.com/o/r/{sha}/a.md",
                }
            ]
        }
    }
    assert find_unpinned_source_urls(pinned) == []

    branch_pinned = {
        "provenance": {
            "documents": [
                {
                    "source_id": "doc_a",
                    "commit_sha": sha,
                    "url": "https://github.com/o/r/blob/master/a.md",
                    "raw_url": "https://raw.githubusercontent.com/o/r/master/a.md",
                }
            ]
        }
    }
    assert find_unpinned_source_urls(branch_pinned) == [
        f"doc_a.url: 未固定到 commit {sha[:8]}…（https://github.com/o/r/blob/master/a.md）",
        (
            f"doc_a.raw_url: 未固定到 commit {sha[:8]}…"
            "（https://raw.githubusercontent.com/o/r/master/a.md）"
        ),
    ]
    assert find_unpinned_source_urls({"provenance": {"documents": [{"source_id": "doc_a"}]}}) == [
        "doc_a: 缺少 commit_sha"
    ]


def test_no_scattered_evidence_construction_in_src() -> None:
    """synthetic / legacy 证据不得在工厂与迁移器之外被拼装。"""
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        source = path.read_text(encoding="utf-8")
        for token, constant, allowed in SOURCE_TYPE_DISPERSION_GUARDS:
            offenders.extend(
                find_scattered_evidence_construction(source, rel, token, constant, allowed)
            )
    assert offenders == []


# ---- P2.2-1：L3 构造点守卫 ----


def test_no_scattered_evidence_ref_construction_in_src() -> None:
    """`EvidenceRef(...)` 的字面构造只允许出现在白名单文件里（L3）。

    这条把 P2.2_PLAN §2.5 的 L3 从"设计意图"变成"会失败的检查"：
    "证据只能由受控工厂产出"如果没人执行，就只是一句注释。
    """
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        offenders.extend(
            find_scattered_evidence_ref_construction(
                path.read_text(encoding="utf-8"), rel, EVIDENCE_REF_FACTORY_ALLOWED_FILES
            )
        )
    assert offenders == []


def test_evidence_ref_construction_guard_has_controls() -> None:
    """守卫必须可证伪：正例（构造）要被抓到，反例（反序列化 / 注解 / 只读比较）不能误报。

    误报的守卫迟早被绕过（这句话在 `find_scattered_evidence_construction` 的注释里
    已经写过一次，P1A-1 就是被 `integrity.py` 误报逼着重写过的）。
    """
    rel = "src/agentboard/research/models.py"
    flagged = "def f():\n    return EvidenceRef(source_type=SOURCE_FIXTURE, source_id='fixture:x')\n"
    expected_hit = (
        f"{rel}: 直接构造 EvidenceRef(...) —— 请改走受控工厂"
        f"（research.sources.evidence_from_excerpt / 指定工厂），"
        f"或显式把本文件登记进 EVIDENCE_REF_FACTORY_ALLOWED_FILES"
    )
    assert find_scattered_evidence_ref_construction(flagged, rel, ()) == [expected_hit]
    # 反序列化不是构造
    assert find_scattered_evidence_ref_construction(
        "def f(raw):\n    return EvidenceRef.from_dict(raw)\n", rel, ()
    ) == []
    # 注解 / isinstance / 只读比较都不算
    assert find_scattered_evidence_ref_construction(
        "def f(x) -> EvidenceRef:\n    assert isinstance(x, EvidenceRef)\n    return x\n", rel, ()
    ) == []
    # 白名单文件放行
    assert find_scattered_evidence_ref_construction(
        flagged, "src/agentboard/research/sources.py", EVIDENCE_REF_FACTORY_ALLOWED_FILES
    ) == []
    # 别的模块不受影响
    assert find_scattered_evidence_ref_construction("import json\n", rel, ()) == []


def test_evidence_ref_factory_allowlist_matches_actual_construction_sites() -> None:
    """白名单必须与实际构造点**完全一致**（定义处除外）：多了会包庇，少了会误报。

    这条防的是"白名单腐化"：临时把某个文件加进去让测试变绿，然后就没人再拿掉它。
    定义处单独扣除，因为它的放行理由与其他四个不同
    （它不是"某个工厂"，而是"类自己所在的模块"）。
    """
    actual = {
        _rel(path)
        for path in _iter_src_files()
        if find_scattered_evidence_ref_construction(path.read_text(encoding="utf-8"), _rel(path), ())
    }
    expected = set(EVIDENCE_REF_FACTORY_ALLOWED_FILES) - {EVIDENCE_REF_DEFINITION_MODULE}
    assert actual == expected


# ---- P2.2-1：L4 Provider 反向守卫 ----


def test_providers_do_not_reference_evidence_contracts() -> None:
    """Provider 层不得引用证据契约（L4）。

    P2.1 采集层与证据层之间刻意**没有**通路（仓库搜索摘要不是技术结论）。
    这条守卫把这个"没有"从"暂时没人这么写"变成"这么写会失败"。
    """
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        if not rel.startswith(PROVIDERS_EVIDENCE_GUARD_PREFIX):
            continue
        offenders.extend(
            find_evidence_references_in_providers(path.read_text(encoding="utf-8"), rel)
        )
    assert offenders == []


def test_provider_evidence_boundary_guard_has_controls() -> None:
    """L4 守卫的正负控制，含"文档里提及不算违规"这条边界。"""
    rel = "src/agentboard/providers/github_search.py"
    # 绝对 import
    assert find_evidence_references_in_providers(
        "from agentboard.evidence import EvidenceRef\n", rel
    ) == [f"{rel}: 不得 from agentboard.evidence import ...", f"{rel}: 不得 import 证据契约名 EvidenceRef"]
    # 相对 import
    assert find_evidence_references_in_providers("from ..evidence import SOURCE_FIXTURE\n", rel) == [
        f"{rel}: 不得 from ..evidence import ...",
        f"{rel}: 不得 import 证据契约名 SOURCE_FIXTURE",
    ]
    # 直接使用名字 / 属性
    assert find_evidence_references_in_providers(
        "def f():\n    return compute_evidence_id\n", rel
    ) == [f"{rel}: 不得引用证据契约名 compute_evidence_id"]
    assert find_evidence_references_in_providers(
        "def f(m):\n    return m.EvidenceRef\n", rel
    ) == [f"{rel}: 不得引用证据契约属性 EvidenceRef"]
    # 干净的 provider 代码不受影响
    assert find_evidence_references_in_providers(
        "import json\n\ndef f(x):\n    return json.dumps(x)\n", rel
    ) == []
    # 非 providers 目录根本不参与这条守卫
    assert find_evidence_references_in_providers(
        "from agentboard.evidence import EvidenceRef\n", "src/agentboard/research/sources.py"
    ) == []


def test_provider_evidence_boundary_guard_flags_a_real_provider_file() -> None:
    """反空转对照：用**真实 provider 文件的原文**加一行 import，守卫必须变红。

    比"合成源码能被抓到"更强一层：它证明守卫在真实文件形状上有效，
    而不是只在精心构造的片段上有效。原文未经改动时必须通过。
    """
    path = PROJECT_ROOT / "src/agentboard/providers/github_search.py"
    original = path.read_text(encoding="utf-8")
    rel = "src/agentboard/providers/github_search.py"
    assert find_evidence_references_in_providers(original, rel) == []
    injected = original + "\nfrom agentboard.evidence import EvidenceRef  # 反空转对照\n"
    assert find_evidence_references_in_providers(injected, rel) == [
        f"{rel}: 不得 from agentboard.evidence import ...",
        f"{rel}: 不得 import 证据契约名 EvidenceRef",
    ]


def test_evidence_ref_construction_guard_flags_a_real_research_file() -> None:
    """反空转对照：用真实 `research/models.py` 原文加一次构造，L3 守卫必须变红。"""
    path = PROJECT_ROOT / "src/agentboard/research/models.py"
    original = path.read_text(encoding="utf-8")
    rel = "src/agentboard/research/models.py"
    assert find_scattered_evidence_ref_construction(original, rel, ()) == []
    injected = original + "\n_LEAK = EvidenceRef(source_type='fixture_snapshot', source_id='fixture:x')\n"
    assert find_scattered_evidence_ref_construction(injected, rel, ()) == [
        (
            f"{rel}: 直接构造 EvidenceRef(...) —— 请改走受控工厂"
            f"（research.sources.evidence_from_excerpt / 指定工厂），"
            f"或显式把本文件登记进 EVIDENCE_REF_FACTORY_ALLOWED_FILES"
        )
    ]


# ---- P2.2-1：L5 attestation 绑定状态守卫（P1-1 复核修正） ----


def test_no_out_of_band_attestation_binding_in_src() -> None:
    """绑定状态不得在 `attestation.py` 之外被构造或写入（L5）。

    这条是 P1-1 修复的**静态**伴生守卫：`init=False` 让构造器传不进去，
    L5 则让 `object.__setattr__` 这条路必须显式改守卫 / 加白名单 ——
    两步都是会被评审看到的动作。没有它，"静态守卫下不可合法生成"就是空话。
    """
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        offenders.extend(
            find_out_of_band_attestation_binding(
                path.read_text(encoding="utf-8"), rel, ATTESTATION_BINDING_ALLOWED_FILES
            )
        )
    assert offenders == []


def test_attestation_binding_guard_has_controls() -> None:
    """L5 守卫的正负控制：两条规则各来一个正例，并确认只读用法不误报。"""
    rel = "src/agentboard/research/sources.py"
    constructed = "def f():\n    return AttestationBinding(reason='', detail='')\n"
    assert find_out_of_band_attestation_binding(constructed, rel, ()) == [
        (
            f"{rel}: 直接构造 AttestationBinding(...) —— 绑定状态只能由 "
            f"attestation.load_attestation() 建立；请改走该入口"
        )
    ]
    written = 'def f(x):\n    object.__setattr__(x, "binding", None)\n'
    assert find_out_of_band_attestation_binding(written, rel, ()) == [
        (
            f"{rel}: 在加载器之外 object.__setattr__(..., 'binding') —— "
            f"这会绕过文件级绑定（C6），请改走 attestation.load_attestation()"
        )
    ]
    # 读绑定状态 / 写别的属性 / 别的 setattr 形态：都不算违规
    assert find_out_of_band_attestation_binding(
        "def f(a):\n    return a.binding is None and a.binding_reason\n", rel, ()
    ) == []
    assert find_out_of_band_attestation_binding(
        'def f(x):\n    object.__setattr__(x, "digest", "abc")\n', rel, ()
    ) == []
    assert find_out_of_band_attestation_binding(
        'def f(x, name):\n    object.__setattr__(x, name, 1)\n', rel, ()
    ) == []
    # 白名单文件放行
    assert find_out_of_band_attestation_binding(written, ATTESTATION_BINDING_ALLOWED_FILES[0],
                                                ATTESTATION_BINDING_ALLOWED_FILES) == []


def test_attestation_binding_guard_flags_a_real_file() -> None:
    """反空转对照：用**真实来源层原文**注入一次绑定写入，L5 必须变红。

    刻意选 `research/sources.py` —— 它就在 attestation 的邻居位置，是最可能"顺手写绑定"的地方。
    """
    path = PROJECT_ROOT / "src/agentboard/research/sources.py"
    original = path.read_text(encoding="utf-8")
    rel = "src/agentboard/research/sources.py"
    assert find_out_of_band_attestation_binding(original, rel, ATTESTATION_BINDING_ALLOWED_FILES) == []
    injected = original + '\nobject.__setattr__(att, "binding", AttestationBinding(reason=""))\n'
    # 两条规则都会命中；返回值是**排序后**的，所以期望值也要排序（中文按码点序）
    assert sorted(
        find_out_of_band_attestation_binding(injected, rel, ATTESTATION_BINDING_ALLOWED_FILES)
    ) == sorted(
        [
            (
                f"{rel}: 直接构造 AttestationBinding(...) —— 绑定状态只能由 "
                f"attestation.load_attestation() 建立；请改走该入口"
            ),
            (
                f"{rel}: 在加载器之外 object.__setattr__(..., 'binding') —— "
                f"这会绕过文件级绑定（C6），请改走 attestation.load_attestation()"
            ),
        ]
    )


def test_attestation_binding_allowlist_matches_actual_sites() -> None:
    """白名单必须与实际写绑定状态的位置**完全一致**（防白名单腐化）。"""
    actual = {
        _rel(path)
        for path in _iter_src_files()
        if find_out_of_band_attestation_binding(
            path.read_text(encoding="utf-8"), _rel(path), ()
        )
    }
    assert actual == set(ATTESTATION_BINDING_ALLOWED_FILES)


# P2.2-3 attempt 3（L5 扩展）：`SnapshotSetBinding`（root↔attestation 同源绑定上下文）
# 只能由 `snapshot.load_snapshot_set()` 构造 —— 手拼 root+attestation 拿不到合法上下文，
# research_grade 下的 `binding_context is None` ⇒ I10 `unbound`（fail-closed，R2/R3）。
SNAPSHOT_SET_BINDING_ALLOWED_FILES: tuple[str, ...] = ("src/agentboard/research/snapshot.py",)
_SNAPSHOT_SET_BINDING_CLASS_NAME = "SnapshotSetBinding"


def find_out_of_band_snapshot_set_binding(
    source: str, rel: str, allowed: tuple[str, ...]
) -> list[str]:
    """检出在工厂之外构造 `SnapshotSetBinding(...)` 的代码（L5 扩展）。

    只认 `SnapshotSetBinding(...)` 这种"名字 + 调用"形态（isinstance / 注解 / 只读
    比较不算）；docstring / 注释里的提及是字符串常量，不是 `ast.Call`，不误报。
    覆盖边界与 L3/L5 同构：别名绑定看不出来，它拦的是**直接写法**。
    """
    if rel in allowed:
        return []
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == _SNAPSHOT_SET_BINDING_CLASS_NAME:
            hits.add(
                f"{rel}: 直接构造 {_SNAPSHOT_SET_BINDING_CLASS_NAME}(...) —— "
                f"绑定上下文只能由 snapshot.load_snapshot_set() 建立；请改走该入口"
            )
    return sorted(hits)


def test_no_out_of_band_snapshot_set_binding_in_src() -> None:
    """`SnapshotSetBinding` 不得在 `snapshot.py` 之外被构造（L5 扩展）。

    B6 修复的同源绑定（H6）依赖"上下文只能由工厂产出"；这条把它从设计意图
    变成会失败的检查 —— 与 `AttestationBinding` 的 L5 同一套机制。
    """
    offenders: list[str] = []
    for path in _iter_src_files():
        rel = _rel(path)
        offenders.extend(
            find_out_of_band_snapshot_set_binding(
                path.read_text(encoding="utf-8"), rel, SNAPSHOT_SET_BINDING_ALLOWED_FILES
            )
        )
    assert offenders == []


def test_snapshot_set_binding_guard_has_controls() -> None:
    """L5 扩展的正负控制：构造被抓、isinstance/注解/白名单不误报。"""
    rel = "src/agentboard/research/integrity.py"
    constructed = "def f():\n    return SnapshotSetBinding(fixture_root='.')\n"
    assert find_out_of_band_snapshot_set_binding(constructed, rel, ()) == [
        (
            f"{rel}: 直接构造 SnapshotSetBinding(...) —— "
            f"绑定上下文只能由 snapshot.load_snapshot_set() 建立；请改走该入口"
        )
    ]
    assert find_out_of_band_snapshot_set_binding(
        "def f(x):\n    assert isinstance(x, SnapshotSetBinding)\n", rel, ()
    ) == []
    assert find_out_of_band_snapshot_set_binding(
        constructed, SNAPSHOT_SET_BINDING_ALLOWED_FILES[0], SNAPSHOT_SET_BINDING_ALLOWED_FILES
    ) == []


def test_snapshot_set_binding_allowlist_matches_actual_sites() -> None:
    """白名单必须与实际构造点**完全一致**（防白名单腐化）。"""
    actual = {
        _rel(path)
        for path in _iter_src_files()
        if find_out_of_band_snapshot_set_binding(
            path.read_text(encoding="utf-8"), _rel(path), ()
        )
    }
    assert actual == set(SNAPSHOT_SET_BINDING_ALLOWED_FILES)

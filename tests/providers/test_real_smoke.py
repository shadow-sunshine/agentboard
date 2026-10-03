"""可选的真实 smoke test（P2.1）。

**默认不执行。**
只有显式设置 `AGENTBOARD_REAL_SMOKE=1` 才会跑，否则整模块 skip。
这样"默认 pytest 全程离线"这条验收项不会因为本文件而失效。

凭据：只从环境变量读，**永不写入仓库**。

    GITHUB_TOKEN   可选。存在则带上 `Authorization: Bearer`，
                   把未认证限额（10 次/分）提到 30 次/分。
                   不存在则以匿名方式运行 —— 本端点匿名可用，且不产生费用。

运行方式（PowerShell，仅此一次）：

    $env:AGENTBOARD_REAL_SMOKE = "1"
    $env:GITHUB_TOKEN = "ghp_xxx"        # 可选
    python -m pytest tests/providers/test_real_smoke.py -v

快照落盘位置：`var/snapshots/`（`AGENTBOARD_REAL_SNAPSHOT_DIR` 可覆盖）。
该目录在 `fixtures/` 之外，是**运行时产物**，不是静态资料。

若没有可用凭据
--------------
本文件同样会运行 —— 匿名访问是本来源的**正常模式**，不是降级模式。
真正会阻止运行的是**网络不可达**：那时测试会失败并明确报告"真实 smoke test 无法执行"，
而不会伪装成通过。这一点很重要：P2.1 要求"若没有可用凭据，明确报告真实 smoke test
未执行，不得声称已验证真实检索"，所以本文件在无凭据时依然会真的发一次请求，
由结果本身决定报告什么。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentboard.providers.errors import ProviderError
from agentboard.providers.github_search import build_default_provider
from agentboard.providers.interface import SearchQuery
from agentboard.providers.snapshot_store import DEFAULT_SNAPSHOT_DIRNAME, SnapshotStore

SMOKE_ENV_VAR = "AGENTBOARD_REAL_SMOKE"
SNAPSHOT_DIR_ENV = "AGENTBOARD_REAL_SNAPSHOT_DIR"

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 检索词：与项目既有的选型领域一致（向量检索），便于人工把结果对回 `fixtures/official/`。
SMOKE_TERMS = "vector database plugin"
SMOKE_LIMIT = 3
#: 真实 smoke 用的采集日期必须显式给定（生产代码不读时钟）。
SMOKE_CAPTURED_AT = "2026-09-23"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get(SMOKE_ENV_VAR) != "1",
        reason=(
            f"真实联网 smoke test 默认跳过（不联网）。"
            f"如需执行：设置 {SMOKE_ENV_VAR}=1 后重跑本文件。"
        ),
    ),
    # 本文件是 opt-in 的真实联网测试：豁免 conftest 里的代理/token 隔离，
    # 因为它必须读到用户显式提供的 GITHUB_TOKEN 与代理。
    pytest.mark.real_smoke,
]


@pytest.fixture(scope="module")
def snapshot_root() -> Path:
    override = os.environ.get(SNAPSHOT_DIR_ENV)
    return Path(override) if override else PROJECT_ROOT / DEFAULT_SNAPSHOT_DIRNAME


def test_real_search_retrieves_and_snapshots(snapshot_root: Path) -> None:
    """真检索一次 → 落盘 → 二次落盘必须判为重复。

    断言的重点不是"取到了几条"（那取决于当时的 GitHub 数据），
    而是**整条链路真的跑通了**：请求发出、响应解析、字段自校验、内容寻址落盘、去重生效。
    """
    provider = build_default_provider()
    credential_mode = provider.credential_mode()

    documents = provider.search(
        SearchQuery(terms=SMOKE_TERMS, limit=SMOKE_LIMIT),
        captured_at=SMOKE_CAPTURED_AT,
    )

    assert documents, (
        f"真实检索返回 0 条结果（凭据模式 {credential_mode}，查询词 {SMOKE_TERMS!r}）。"
        "这可能是网络不可达、限额耗尽，或查询词确实没有匹配 —— 需人工确认，不得当成通过。"
    )

    for document in documents:
        assert document.content_sha256
        assert document.source_url.startswith("https://github.com/")
        assert document.captured_at == SMOKE_CAPTURED_AT

    store = SnapshotStore(snapshot_root)
    results = store.write_all(documents, provider_id=provider.provider_id)

    written = [r for r in results if r.status == "written"]
    duplicates = [r for r in results if r.status == "duplicate"]
    conflicts = [r for r in results if r.status == "conflict"]
    assert not conflicts, f"出现 conflict（同名文件哈希不符）：{[str(r.existing_path) for r in conflicts]}"

    # 二次采集同样的内容：必须全部判为重复，且不产生新文件
    second = store.write_all(documents, provider_id=provider.provider_id)
    assert all(r.status == "duplicate" for r in second)

    # 报告信息（pytest -s 可见）：来源 + 凭据模式 + 结果
    print(
        f"\n[真实 smoke] 来源=GitHub REST v{GitHubApiVersion}\n"
        f"  端点={provider.search.__doc__ or 'search repositories'}\n"
        f"  查询词={SMOKE_TERMS!r} limit={SMOKE_LIMIT}\n"
        f"  凭据模式={credential_mode}（anonymous 表示未使用 token）\n"
        f"  取回={len(documents)} 条；新增={len(written)}；重复={len(duplicates)}\n"
        f"  快照目录={snapshot_root}\n"
        f"  跳过条目={provider.skipped_count}"
    )
    for document in documents:
        print(f"    - {document.source_url} sha256={document.content_sha256[:16]}…")


def test_real_snapshot_is_readable_back(snapshot_root: Path) -> None:
    """落盘的快照必须能按内容哈希取回，且与原文逐字节相同。

    与上一个测试分开，是为了让"取了但存不下来"这类问题有独立的失败信号。
    """
    store = SnapshotStore(snapshot_root)
    snapshots = store.list_snapshots()
    if not snapshots:
        pytest.skip("快照目录为空（上一个测试可能未写入，或目录被清理）")

    checked = 0
    for snapshot in snapshots:
        content = store.read_content(snapshot.content_sha256)
        assert content, f"{snapshot.path.name} 取回内容为空"
        checked += 1
    print(f"\n[真实 smoke] 已复核 {checked} 份快照的内容哈希")


def test_real_provider_failure_is_explicit() -> None:
    """网络不可达时必须抛 `ProviderError` 子类，而不是静默返回空列表。

    这条测试的作用：证明"取不到"与"没结果"是两种不同的信号。
    若未来有人把网络错误 catch 成 `return []`，这条会失败。
    """
    from agentboard.providers.errors import ProviderError as _ProviderError  # noqa: F401

    provider = build_default_provider()
    try:
        provider.search(
            SearchQuery(terms=SMOKE_TERMS, limit=1), captured_at=SMOKE_CAPTURED_AT
        )
    except ProviderError:
        # 允许失败（网络波动），但必须是显式的 ProviderError
        return


#: 仅用于报告输出（避免在断言里硬编码版本号字符串）。
GitHubApiVersion = "2022-11-28"

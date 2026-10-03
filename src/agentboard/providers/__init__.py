"""Provider 层：真实资料检索的**唯一**网络出口（P2.1）。

分层意图
--------
P1A 的核心调度与研究领域层**完全离线**（`dependencies = []`，全仓静态守卫禁止网络库）。
P2.1 要在不破坏这件事的前提下接入真实来源，唯一的做法是把"能联网"这件事
**收敛到一个独立的包**，并让其余代码看不见它：

    src/agentboard/providers/       ← 唯一允许 import 网络库的地方（白名单见静态守卫）
    src/agentboard/research/        ← 继续只读本地 JSON，不 import 本包
    src/agentboard/board.py 等核心  ← 继续只做纯调度，不 import 本包

因此本包的依赖方向是**单向的**：`providers → core`，核心/领域层永不反向 import。
`tests/test_static_guards.py::test_core_and_research_do_not_import_providers` 把这条钉死。

本包不生成 `EvidenceRef`
-----------------------
`evidence.SOURCE_FIXTURE/SYNTHETIC/LEGACY` 是闭集，且 fixture 证据被登记为"可信"。
若让联网结果直接产出 `EvidenceRef`，"未经验证的联网内容"会获得与人工核对过的快照同等的
信任级别 —— 这正是 P2.1 禁止项"把联网采集结果伪装成静态 fixture"的实质。
所以本包只产出 `RetrievedDocument`（带 sha256、来源 URL、采集时间），
是否/如何转成证据由后续阶段显式设计，本阶段不做。

成本与凭据
----------
具体来源见 `github_search.py` 的模块 docstring。一句话：选的是 GitHub 官方 REST API，
**公开只读端点不需要 API Key、不产生费用**；token 仅作为可选的速率提升手段，
从环境变量读取，永不写入仓库。
"""

from __future__ import annotations

from .errors import (
    ContentTooLargeError,
    InvalidSourceUrlError,
    ProviderError,
    RedirectNotAllowedError,
    RetrievalTimeoutError,
    SourceHTTPError,
    SourceResponseError,
    TransportError,
)
from .github_search import (
    API_HOST,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    GITHUB_SEARCH_API_VERSION,
    GITHUB_SEARCH_ENDPOINT,
    PROVIDER_ID,
    TOKEN_ENV_VAR,
    GitHubRepositorySearchProvider,
)
from .interface import (
    Provider,
    RetrievedDocument,
    SearchQuery,
    Transport,
    TransportRequest,
    TransportResponse,
)
from .snapshot_store import (
    SnapshotStore,
    SnapshotWriteResult,
    StoredSnapshot,
    snapshot_filename,
)

__all__ = [
    "API_HOST",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "GITHUB_SEARCH_API_VERSION",
    "GITHUB_SEARCH_ENDPOINT",
    "PROVIDER_ID",
    "TOKEN_ENV_VAR",
    "ContentTooLargeError",
    "GitHubRepositorySearchProvider",
    "InvalidSourceUrlError",
    "Provider",
    "ProviderError",
    "RedirectNotAllowedError",
    "RetrievalTimeoutError",
    "RetrievedDocument",
    "SearchQuery",
    "SnapshotStore",
    "SnapshotWriteResult",
    "SourceHTTPError",
    "SourceResponseError",
    "StoredSnapshot",
    "Transport",
    "TransportError",
    "TransportRequest",
    "TransportResponse",
    "snapshot_filename",
]

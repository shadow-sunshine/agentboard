"""GitHub 仓库检索 Provider（P2.1 的具体实现）。

选源理由（按项目要求查过当前官方文档）
--------------------------------------

| 问题 | 结论 | 依据 |
|---|---|---|
| 选哪家 | **GitHub REST API · Search repositories** | 项目已有 `fixtures/official/` 全部锚定在 GitHub（`sources.ALLOWED_SOURCE_HOSTS = {github.com, raw.githubusercontent.com}`），选同一来源族可复用既有的 commit/raw_sha256/行号复核链条，不引入第二套信任模型 |
| 要 API Key 吗 | **不需要**（公开只读端点） | 官方文档：Search 端点未认证也可用；`Authorization` 头是可选的 |
| 付费吗 | **不付费** | 公开 REST API 免费；未认证限额 10 次/分钟，足够一次性采集 |
| 端点 | `GET https://api.github.com/search/repositories?q=…` | — |
| 必需请求头 | `Accept: application/vnd.github+json`、`X-GitHub-Api-Version` | 官方文档推荐 |
| 上限 | 单查询最多 1000 条；`per_page` 最大 100 | 故 `SearchQuery.limit` 限 1..100 |

**token 是可选的速率提升手段**：若环境里有 `GITHUB_TOKEN`，本实现会带上
`Authorization: Bearer …` 把头把限额从 10/分提到 30/分。token **只从环境变量读**，
不落盘、不进仓库、不进快照；`snapshot_store` 只保存响应体与元数据，
而响应体里不含请求头。

为什么选"仓库检索"而不是"代码检索"
-----------------------------------
代码检索（`/search/code`）**强制要求认证**，且只搜默认分支、只搜 <384KB 的文件。
仓库检索无需认证即可工作，且返回的 `full_name` / `html_url` / `pushed_at` / `license`
正好是可复核的稳定锚点。P2.1 的目标是"能从一个真实来源检索并保存可复核快照"，
仓库检索用更少的凭据前提达成同一目标。

本实现不产出 `EvidenceRef`
--------------------------
见 `providers/__init__.py` 的说明：联网内容不得自动获得"可信证据"身份。
`RetrievedDocument` 只是"一份带哈希的原文"，是否成为证据由后续阶段显式决定。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .errors import SourceResponseError
from .interface import (
    RetrievedDocument,
    SearchQuery,
    Transport,
    TransportRequest,
)

#: 稳定标识：写进快照元数据。改实现就改它，便于复核时确认"这份快照是谁取的"。
PROVIDER_ID = "github.repository_search.v1"

GITHUB_SEARCH_ENDPOINT = "https://api.github.com/search/repositories"

#: 只允许访问这一个主机。重定向目标若跳出去，`validate_redirect_target` 会拒绝。
API_HOST = "api.github.com"

#: 官方文档要求的 API 版本头。写成常量便于将来对齐官方版本变更。
GITHUB_SEARCH_API_VERSION = "2022-11-28"

#: token 的环境变量名（**只读，不写入任何地方**）。
TOKEN_ENV_VAR = "GITHUB_TOKEN"

DEFAULT_TIMEOUT_SECONDS = 10.0
#: 1 MiB。仓库检索的 JSON 响应远比这个小；超限即说明来源行为异常或有人在投毒。
DEFAULT_MAX_RESPONSE_BYTES = 1 << 20
DEFAULT_MAX_REDIRECTS = 3

ACCEPT_HEADER = "application/vnd.github+json"


def _escape_query_terms(terms: str) -> str:
    """把查询词编码进 query string。

    只编码到"能安全放进 URL"为止：用 `quote` 的语义手写，避免 import `urllib.parse`
    （本文件所在包虽有网络豁免，但 `urllib` 不在豁免集合内，见静态守卫）。
    """
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    encoded = []
    for byte in terms.encode("utf-8"):
        char = chr(byte)
        encoded.append(char if char in safe else f"%{byte:02X}")
    return "".join(encoded)


@dataclass(frozen=True, slots=True)
class GitHubRepositorySearchProvider:
    """把一次仓库检索变成若干份 `RetrievedDocument`。

    `transport` 是构造参数而不是内部 new 出来的：真实路径注入 `HttpTransport`，
    测试注入 `MockTransport`。**Provider 自身不含任何网络调用**，
    所以"Provider 逻辑"可以在零网络的前提下被完整覆盖。
    """

    transport: Transport
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    #: 只用于**记录**（不参与请求）：说明这次采集是在哪种凭据条件下完成的，
    #: 方便复核时判断限额差异。绝不写入 token 本身。
    token_env_var: str = TOKEN_ENV_VAR
    allowed_hosts: frozenset[str] = field(default_factory=lambda: frozenset({API_HOST}))

    provider_id: str = PROVIDER_ID

    #: 上一次 `search` 里被跳过的条目数（结构不合规，无法安全转成文档）。
    #: 不放在返回值里是因为返回值契约是"若干份可复核资料"，
    #: 混入计数会让调用方需要区分两种元素；用实例属性则能保持契约干净。
    #: 注意：dataclass(frozen=True) 下用 `object.__setattr__` 更新，见 `search`。
    _skipped: int = field(default=0, init=False, repr=False, compare=False)

    @property
    def skipped_count(self) -> int:
        """上一次检索中被跳过的条目数。`>0` 说明来源返回了我们无法安全使用的数据。"""
        return self._skipped

    def search(
        self,
        query: SearchQuery,
        *,
        captured_at: str,
        timeout_seconds: float | None = None,
    ) -> Sequence[RetrievedDocument]:
        """执行检索。`captured_at` 由调用方传入（不读时钟）。

        `timeout_seconds` 为调用方本次显式指定的超时上限；`None` ⇒ 用构造时的
        `self.timeout_seconds`。P3 运行时把 `ResearchBudget.request_timeout_seconds`
        透传进来，因此预算的超时配置真正到达传输层（测试断言实际值）。
        """
        request = TransportRequest(
            url=self._build_url(query),
            headers=self._headers(),
            timeout_seconds=(
                self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ),
            max_response_bytes=self.max_response_bytes,
            max_redirects=self.max_redirects,
        )
        response = self.transport.send(request)
        payload = self._parse_json(response.body, response.url)
        items = self._extract_items(payload)
        documents: list[RetrievedDocument] = []
        skipped = 0
        for index, item in enumerate(items):
            document = self._to_document(item, query, captured_at, index)
            if document is None:
                skipped += 1
            else:
                documents.append(document)
        object.__setattr__(self, "_skipped", skipped)
        return tuple(documents)

    # ---- 内部步骤：每一步都可单独测试 ----

    def _build_url(self, query: SearchQuery) -> str:
        return (
            f"{GITHUB_SEARCH_ENDPOINT}?q={_escape_query_terms(query.normalized_terms)}"
            f"&per_page={query.limit}"
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": GITHUB_SEARCH_API_VERSION,
        }
        # token 只在环境变量存在且非空时带上；不打印、不记录、不写入快照。
        token = self._read_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _read_token(self) -> str:
        """读环境变量里的 token。**唯一**读取凭据的位置。"""
        raw = os.environ.get(self.token_env_var, "")
        return raw.strip()

    def credential_mode(self) -> str:
        """当前是"匿名"还是"带 token"，**只返回标签、不返回凭据**。

        供 README / smoke test 输出使用：复核者需要知道这次采集在哪个限额档位下完成，
        但不需要（也不应该）看到 token 的值。
        """
        return "token" if self._read_token() else "anonymous"

    def _parse_json(self, body: bytes, url: str) -> Mapping[str, object]:
        if not body:
            raise SourceResponseError(f"{url} 返回空响应体")
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceResponseError(f"{url} 的响应体不是合法 UTF-8：{exc}") from exc
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise SourceResponseError(f"{url} 的响应体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise SourceResponseError(f"{url} 的响应体顶层不是 JSON 对象")
        if "message" in payload and "items" not in payload:
            # GitHub 的错误响应形如 {"message": "API rate limit exceeded..."}。
            # 把它当"零结果"会静默产出空快照，必须显式失败。
            raise SourceResponseError(f"{url} 返回错误：{payload.get('message')!r}")
        return payload

    def _extract_items(self, payload: Mapping[str, object]) -> Sequence[object]:
        items = payload.get("items")
        if not isinstance(items, list):
            raise SourceResponseError("响应缺少 items 数组（或类型不是数组）")
        return items

    def _to_document(
        self,
        item: object,
        query: SearchQuery,
        captured_at: str,
        index: int,
    ) -> RetrievedDocument | None:
        """把一条搜索结果转成 `RetrievedDocument`。

        缺关键字段（`full_name` / `html_url`）的条目**跳过而不是编造** ——
        编造一个 `source_url` 就等于伪造来源。返回 `None` 表示跳过，
        由 `search` 计数到 `skipped_count`，不静默。
        """
        if not isinstance(item, Mapping):
            return None
        full_name = item.get("full_name")
        html_url = item.get("html_url")
        if not isinstance(full_name, str) or not full_name.strip():
            return None
        if not isinstance(html_url, str) or not html_url.startswith("https://"):
            return None

        # 原文内容 = 该条结果的 JSON 序列化（键排序，保证同内容同哈希）。
        # 快照保存"来源给我们的原文"，而不是我们转述的一句摘要 ——
        # 转述会让 sha256 只证明"我们的转述没变"，而不是"来源的内容没变"。
        content = json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2)
        description = item.get("description")
        title = (
            f"{full_name} — {description}"
            if isinstance(description, str) and description.strip()
            else full_name
        )
        return RetrievedDocument.build(
            source_id=f"{PROVIDER_ID}:result:{index}",
            query_terms=query.normalized_terms,
            source_url=html_url,
            title=title,
            captured_at=captured_at,
            content=content,
        )


def build_default_provider() -> GitHubRepositorySearchProvider:
    """构造一个走真实网络的 Provider（`HttpTransport`）。

    单独开一个工厂函数而不是让调用方自己 new：把"真实网络"这个副作用集中到一处，
    一眼能看出**全仓只有这里**会产生网络流量（配合静态守卫的豁免集合）。
    """
    from .http_transport import HttpTransport

    return GitHubRepositorySearchProvider(
        transport=HttpTransport(allowed_hosts=frozenset({API_HOST}))
    )

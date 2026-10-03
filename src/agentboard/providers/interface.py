"""Provider 接口与传输抽象（P2.1）。

三条边界，用**类型**而不是约定来保证
------------------------------------

1. **Provider ↔ Transport**：Provider 只表达"我要取哪个 URL、接受什么内容类型"，
   怎么发请求由 `Transport` 实现决定。因此测试可以注入 Mock transport，
   **Provider 的逻辑（解析、去重、快照、限额）在完全离线的情况下被完整覆盖**。
2. **Provider ↔ 领域层**：本模块不 import `agentboard.research.*`，也不产出
   `EvidenceRef`。输出是 `RetrievedDocument`（纯数据），由调用方决定后续用途。
3. **Provider ↔ 时钟**：采集时间不是 `now()`，由 `Retriever` 通过
   `captured_at` 显式传入（P1A 的 `captured_at` 同样来自快照元数据）。
   这样同一次采集在任何时间重放都得到同一份快照字节。

为什么不直接返回 dict
----------------------
`RetrievedDocument` 是 `frozen=True` 的 dataclass，字段在 `__post_init__` 里自校验
（sha256 必须等于原文的 sha256、URL 必须是 https、时间必须是 `YYYY-MM-DD`）。
"带着自校验的返回值"意味着：跨过 Provider 边界的每一份数据都已经过一遍检查，
下游不需要再防御性地重算 —— 这正是 P1A 里 `EvidenceRef` 的做法，此处保持一致。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import (
    ContentTooLargeError,
    InvalidSourceUrlError,
    RedirectNotAllowedError,
    SourceResponseError,
)

#: 采集日期：只有日期，没有时刻。理由与 P1A 的 `EvidenceRef.captured_at` 相同 ——
#: 带时刻的时间戳会让"同一份内容"产生不同的快照，破坏可复核性与去重基线。
CAPTURED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: URL 只允许 https。http 明文、file://、data: 一律拒绝。
#:
#: 两种形态分别校验，**不是一个正则走天下**：
#: - `HTTPS_URL_PATTERN`：**规范 URL**（无 query、无 fragment、无凭据）。
#:   用于 `TransportResponse.url` / `RetrievedDocument.source_url` / 快照记录 ——
#:   这些位置要的是"这份资料在哪"，带上检索参数会让同一份资料因查询词不同而有不同 URL。
#: - `HTTPS_REQUEST_URL_PATTERN`：**请求 URL**，允许带 query（检索端点必需），
#:   但仍禁止 fragment 与凭据。
HTTPS_URL_PATTERN = re.compile(r"^https://([A-Za-z0-9.\-]+)(/[^\s?#]*)?$")
HTTPS_REQUEST_URL_PATTERN = re.compile(r"^https://([A-Za-z0-9.\-]+)(/[^\s#]*)?(\?[^\s#]*)?$")

#: 采集时间上限：防止把未来日期写进快照元数据。
MAX_TEXT_CHARS = 200_000


def compute_content_sha256(text: str) -> str:
    """原文内容的 sha256（UTF-8 编码，零规范化）。

    "零规范化"是硬要求：先 `strip()` 再哈希会让"仅空白不同"的两份内容撞成同一个哈希，
    于是"重复采集不得静默覆盖"这条要求就会被绕过（第二次采集会被误判为重复而丢弃，
    而实际内容确实变过）。哈希输入必须逐字节等于原文。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_https_url(url: str) -> bool:
    """`url` 是否是**规范** https URL（无 query / fragment / 凭据）。

    用于 `source_url` 这类"资料地址"字段。用正则而不是 `urllib.parse`：
    静态守卫禁止 `src/agentboard/**` 出现 `urllib`，而这条校验同样需要。
    正则 + 显式主机白名单的组合比通用解析器更容易审计。
    """
    return bool(HTTPS_URL_PATTERN.match(url))


def is_https_request_url(url: str) -> bool:
    """`url` 是否是可用于发请求的 https URL（**允许** query，仍禁 fragment / 凭据）。"""
    return bool(HTTPS_REQUEST_URL_PATTERN.match(url))


def url_host(url: str) -> str:
    """取出 URL 主机；非法 URL 返回空串。两种形态都接受。"""
    match = HTTPS_REQUEST_URL_PATTERN.match(url) or HTTPS_URL_PATTERN.match(url)
    if match is None:
        return ""
    return match.group(1)


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """一次检索请求。

    `terms` 空、或元素全空白，一律拒绝：一个"什么都不搜"的检索请求最容易
    悄悄取回一堆无关内容并污染快照目录。
    """

    terms: str
    limit: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.terms, str) or not self.terms.strip():
            raise InvalidSourceUrlError("SearchQuery.terms 必须是非空字符串")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise InvalidSourceUrlError("SearchQuery.limit 必须是整数")
        if not 1 <= self.limit <= 100:
            raise InvalidSourceUrlError(f"SearchQuery.limit 必须在 1..100，实际 {self.limit}")

    @property
    def normalized_terms(self) -> str:
        """查询词归一化：仅折叠空白。

        只做空白折叠、不改大小写、不做同义词扩展 —— 快照里登记的"查询词"
        必须能被人一眼对应回他当时输入的东西，否则复核时对不上账。
        """
        return " ".join(self.terms.split())


@dataclass(frozen=True, slots=True)
class TransportRequest:
    """发往来源的一次 HTTP 请求。

    `max_response_bytes` 是**硬上限**：transport 必须在读取过程中就截断并抛错，
    不能读完再比较（否则"响应大小限制"在内存层面是失效的）。

    `method` / `body`（P4 决策 transport 接线所需）：默认 `GET` / `None`，
    与 P2.1 既有行为逐字节兼容；`body` 只允许随 POST 发送
    （GET 带体、未知方法都是显式配置错误）。
    """

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    max_response_bytes: int = 1 << 20
    max_redirects: int = 3
    method: str = "GET"
    body: bytes | None = None

    def __post_init__(self) -> None:
        if not is_https_request_url(self.url):
            raise InvalidSourceUrlError(
                f"TransportRequest.url 必须是 https URL（可带 query），实际 {self.url!r}"
            )
        if self.timeout_seconds <= 0:
            raise InvalidSourceUrlError("TransportRequest.timeout_seconds 必须为正数")
        if self.max_response_bytes <= 0:
            raise InvalidSourceUrlError("TransportRequest.max_response_bytes 必须为正数")
        if self.max_redirects < 0:
            raise InvalidSourceUrlError("TransportRequest.max_redirects 不能为负")
        if self.method not in ("GET", "POST"):
            raise InvalidSourceUrlError(
                f"TransportRequest.method 只允许 GET/POST，实际 {self.method!r}"
            )
        if self.body is not None:
            if not isinstance(self.body, bytes):
                raise InvalidSourceUrlError("TransportRequest.body 必须是 bytes")
            if self.method != "POST":
                raise InvalidSourceUrlError(
                    f"只有 POST 请求允许携带 body，实际 method={self.method!r}"
                )


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """传输层的原始返回。

    `url` 是**实际响应所在的请求 URL**（因此允许带 query —— 检索端点的 URL 本来就带
    `?q=…`）。`redirect_chain` 记录**实际发生过的**跳转（第一项是原始 URL）。
    暴露它的目的：让"重定向"从隐式行为变成可断言的数据，
    调用方/测试可以直接检查跳转了几次、跳到了哪里。
    """

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    redirect_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_https_request_url(self.url):
            raise SourceResponseError(
                f"TransportResponse.url 必须是 https URL（可带 query），实际 {self.url!r}"
            )
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise SourceResponseError("TransportResponse.status 必须是整数")
        if not isinstance(self.body, bytes):
            raise SourceResponseError("TransportResponse.body 必须是 bytes")


@runtime_checkable
class Transport(Protocol):
    """传输层协议：把 `TransportRequest` 变成 `TransportResponse`。

    这是**唯一**允许真正发起网络 I/O 的位置（真实实现见 `http_transport.py`）。
    测试用 `MockTransport` 实现同一协议，因此 Provider 的上层逻辑与网络完全解耦。

    实现约定（`Provider` 依赖这些约定，Mock 必须同样遵守）：
    - 超时抛 `RetrievalTimeoutError`；
    - 非法 / 不允许的目标抛 `InvalidSourceUrlError`；
    - 重定向未被允许抛 `RedirectNotAllowedError`；
    - 响应超限抛 `ContentTooLargeError`；
    - 其余传输失败抛 `TransportError`。
    """

    def send(self, request: TransportRequest) -> TransportResponse:
        """执行一次请求。不得静默吞掉错误。"""
        ...


@dataclass(frozen=True, slots=True)
class RetrievedDocument:
    """从真实来源取回、且已通过自校验的一份资料。

    这是 Provider 层的**输出契约**。字段与 P2.1 要求一一对应：

    | 要求 | 字段 |
    |---|---|
    | 查询词 | `query_terms` |
    | 来源 URL | `source_url` |
    | 标题 | `title` |
    | 采集时间 | `captured_at` |
    | 来源标识 | `source_id` |
    | 原文内容 | `content` |
    | SHA-256 | `content_sha256` |
    """

    source_id: str
    query_terms: str
    source_url: str
    title: str
    captured_at: str
    content: str
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise SourceResponseError("RetrievedDocument.source_id 必须是非空字符串")
        if not isinstance(self.query_terms, str) or not self.query_terms.strip():
            raise SourceResponseError("RetrievedDocument.query_terms 必须是非空字符串")
        if not is_https_url(self.source_url):
            raise SourceResponseError(
                f"RetrievedDocument.source_url 必须是 https URL，实际 {self.source_url!r}"
            )
        if not isinstance(self.title, str) or not self.title.strip():
            raise SourceResponseError("RetrievedDocument.title 必须是非空字符串")
        if not isinstance(self.captured_at, str) or not CAPTURED_AT_PATTERN.match(self.captured_at):
            raise SourceResponseError(
                f"RetrievedDocument.captured_at 必须是 YYYY-MM-DD，实际 {self.captured_at!r}"
            )
        if not isinstance(self.content, str) or not self.content:
            raise SourceResponseError("RetrievedDocument.content 必须是非空字符串")
        if len(self.content) > MAX_TEXT_CHARS:
            raise ContentTooLargeError(
                f"RetrievedDocument.content 超过 {MAX_TEXT_CHARS} 字符上限"
            )
        if not isinstance(self.content_sha256, str) or not SHA256_PATTERN.match(
            self.content_sha256
        ):
            raise SourceResponseError("RetrievedDocument.content_sha256 必须是 64 位小写十六进制")
        # 自校验：声称的 sha256 必须真的等于原文的 sha256。
        # "已被哈希"与"哈希值被记下来"是两件事 —— 只有这一步把它变成可验证的事实。
        actual = compute_content_sha256(self.content)
        if actual != self.content_sha256:
            raise SourceResponseError(
                f"RetrievedDocument.content_sha256 与 content 不符："
                f"声明 {self.content_sha256}，实际 {actual}"
            )

    @classmethod
    def build(
        cls,
        *,
        source_id: str,
        query_terms: str,
        source_url: str,
        title: str,
        captured_at: str,
        content: str,
    ) -> RetrievedDocument:
        """按原文现算 sha256 构造文档（唯一推荐入口）。

        让"sha256 是算出来的"成为默认路径，而不是让调用方自己填一个字符串 ——
        后者迟早会有人填错或填成别的字段的哈希。
        """
        return cls(
            source_id=source_id,
            query_terms=query_terms,
            source_url=source_url,
            title=title,
            captured_at=captured_at,
            content=content,
            content_sha256=compute_content_sha256(content),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "query_terms": self.query_terms,
            "source_url": self.source_url,
            "title": self.title,
            "captured_at": self.captured_at,
            "content": self.content,
            "content_sha256": self.content_sha256,
        }


@runtime_checkable
class Provider(Protocol):
    """资料 Provider：给一个查询词，返回若干份已自校验的资料。

    实现必须是**无状态**的（同样的输入 + 同样的 transport 行为 → 同样的输出），
    否则"快照可复核"这条要求在上游就站不住。
    """

    @property
    def provider_id(self) -> str:
        """Provider 的稳定标识（写进快照元数据，用于复核时确认来源程序）。"""
        ...

    def search(
        self,
        query: SearchQuery,
        *,
        captured_at: str,
        timeout_seconds: float | None = None,
    ) -> Sequence[RetrievedDocument]:
        """执行检索。**只读**：实现不得写文件、不得改全局状态。

        `timeout_seconds`：本次请求的超时上限（秒）；`None` ⇒ 实现自选默认。
        运行时（P3）会把 `ResearchBudget.request_timeout_seconds` 透传到这里 ——
        因此"请求受该预算的超时限制"是有机制保证的契约，不是文档声明：
        实现必须把它用进底层请求（如 `TransportRequest.timeout_seconds`），
        并由测试断言实际到达传输层的值。
        """
        ...


def validate_redirect_target(original: str, target: str, allowed_hosts: frozenset[str]) -> None:
    """校验一次重定向的目标是否可接受；不可接受时抛 `RedirectNotAllowedError`。

    规则（两条，都是白名单式而非黑名单式）：

    1. 目标必须是 https 请求 URL（禁止降级到明文 http、禁止相对路径、禁止 fragment）；
    2. 目标主机必须在白名单内（禁止 302 到内网 / 第三方地址）。

    **凭据（`user:pass@host`）不需要单独判断**：`HTTPS_REQUEST_URL_PATTERN` 的主机
    字符集里没有 `@`，所以带凭据的 URL 在第 1 步就被判为"不是合法 https URL"。
    这里不写一条永不触发的分支 —— 死的校验代码会给人"已防护"的错觉，
    而护栏的价值恰恰在于它真的会响。

    显式校验而不是依赖底层库的自动跟随：自动跟随时"跳到哪里"是隐式的，
    而隐式行为无法被测试断言，也无法保证在换传输实现后仍然成立。
    """
    if not is_https_request_url(target):
        raise RedirectNotAllowedError(f"重定向目标不是 https URL：{target!r}")
    host = url_host(target)
    if host not in allowed_hosts:
        raise RedirectNotAllowedError(
            f"重定向目标主机 {host!r} 不在白名单 {sorted(allowed_hosts)} 内（原始 {original!r}）"
        )

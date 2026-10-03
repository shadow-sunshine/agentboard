"""Provider 层与传输层的 Mock 测试（P2.1 验收核心）。

覆盖 P2.1 明确要求的六类场景：

| 场景 | 测试 |
|---|---|
| 成功 | `test_search_success_*` |
| 超时 | `test_search_propagates_timeout` / `test_http_transport_maps_timeout` |
| 无效 URL | `test_http_transport_rejects_*` / `test_search_rejects_empty_terms` |
| 重定向 | `test_http_transport_follows_allowed_redirect` / `test_http_transport_rejects_*` |
| 超大响应 | `test_read_capped_*` / `test_http_transport_rejects_oversized_response` |
| 重复内容 | 见 `test_snapshot_store.py::test_duplicate_*` |

**全部离线**：`GitHubRepositorySearchProvider` 只依赖 `Transport` 协议；
`HttpTransport` 的重定向 / 限额 / 超时逻辑通过 monkeypatch 替换
`http.client` 连接来测 —— 连 socket 都不需要打开。

**代理环境隔离**：本目录 `conftest.py` 有一个 session 级 autouse fixture 把
`os.environ` 换成空 dict，所以这里的 `HttpTransport` 在**任何机器上**都按
"无代理"解析，不会因为开发机设了 `HTTPS_PROXY` 而改变行为。
除此之外，凡是不测代理的 `HttpTransport` 构造都**显式传 `environ={}`**
（或 `no_proxy_env` fixture）：让"这条测试不依赖机器代理"写在测试自己的
调用点上，而不是依赖某个全局 fixture 的副作用。

需要验证代理路径的测试集中在"代理解析"一节，它们各自显式构造代理
配置（注入 environ 或 `monkeypatch.setenv`），不捡环境里的残留。

辅助件（`MockTransport`、`repo_item`、`ok_response` 等）由 `conftest.py` 以
fixture 形式提供，见该文件顶部说明。
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
from http.client import HTTPException, InvalidURL
from typing import Any

import pytest

from agentboard.providers.errors import (
    ContentTooLargeError,
    InvalidSourceUrlError,
    RedirectNotAllowedError,
    RetrievalTimeoutError,
    SourceHTTPError,
    SourceResponseError,
    TransportError,
)
from agentboard.providers.github_search import (
    ACCEPT_HEADER,
    API_HOST,
    GITHUB_SEARCH_API_VERSION,
    build_default_provider,
)
from agentboard.providers.http_transport import (
    DEFAULT_ALLOWED_HOSTS,
    REDIRECT_STATUSES,
    HttpTransport,
    _read_capped,
    is_no_proxy,
    parse_proxy_url,
    resolve_proxy,
)
from agentboard.providers.interface import (
    SearchQuery,
    TransportRequest,
    TransportResponse,
)
from agentboard.providers.snapshot_store import snapshot_filename

CAPTURED = "2026-09-23"
QUERY_URL = "https://api.github.com/search/repositories?q=vector%20db&per_page=10"
ALLOWED = frozenset({"api.github.com"})


def _query(limit: int = 10) -> SearchQuery:
    return SearchQuery(terms="vector db", limit=limit)


# ================= 成功路径 =================


def test_search_success_returns_documents_with_all_required_fields(
    mock_transport, provider_factory, make_repo_item
) -> None:
    """成功路径：七个必需字段一个不少，且 sha256 与原文一致。"""
    mock_transport.default = _ok_with([make_repo_item("milvus-io/milvus", description="vector db")])
    provider = provider_factory(mock_transport)

    documents = provider.search(_query(), captured_at=CAPTURED)

    assert len(documents) == 1
    document = documents[0]
    assert document.query_terms == "vector db"
    assert document.source_url == "https://github.com/milvus-io/milvus"
    assert document.title == "milvus-io/milvus — vector db"
    assert document.captured_at == CAPTURED
    assert document.source_id.startswith("github.repository_search.v1:result:")
    assert document.content_sha256 == hashlib.sha256(
        document.content.encode("utf-8")
    ).hexdigest()
    assert provider.skipped_count == 0


def test_search_builds_documented_request(mock_transport, provider_factory) -> None:
    """请求必须符合 GitHub 官方文档：端点、query 参数、Accept 头、API 版本头。"""
    mock_transport.default = _empty_ok()
    provider_factory(mock_transport).search(_query(limit=7), captured_at=CAPTURED)

    request = mock_transport.last_request
    assert request.url.startswith("https://api.github.com/search/repositories?q=")
    assert "q=vector%20db" in request.url
    assert request.url.endswith("&per_page=7")
    assert request.headers["Accept"] == ACCEPT_HEADER
    assert request.headers["X-GitHub-Api-Version"] == GITHUB_SEARCH_API_VERSION
    # 匿名模式绝不能带 Authorization
    assert "Authorization" not in request.headers


def test_search_anonymous_without_env_token(
    monkeypatch: pytest.MonkeyPatch, mock_transport, provider_factory
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_transport.default = _empty_ok()
    provider = provider_factory(mock_transport)
    assert provider.credential_mode() == "anonymous"
    provider.search(_query(), captured_at=CAPTURED)
    assert "Authorization" not in mock_transport.last_request.headers


def test_search_uses_env_token_as_bearer(
    monkeypatch: pytest.MonkeyPatch, mock_transport, provider_factory
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "  secret-token  ")
    mock_transport.default = _empty_ok()
    provider = provider_factory(mock_transport)
    assert provider.credential_mode() == "token"
    provider.search(_query(), captured_at=CAPTURED)
    # token 从环境变量读、strip 后使用；但**不**出现在 URL 里
    assert mock_transport.last_request.headers["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in mock_transport.last_request.url


def test_search_empty_token_treated_as_anonymous(
    monkeypatch: pytest.MonkeyPatch, mock_transport, provider_factory
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    mock_transport.default = _empty_ok()
    provider = provider_factory(mock_transport)
    assert provider.credential_mode() == "anonymous"
    provider.search(_query(), captured_at=CAPTURED)
    assert "Authorization" not in mock_transport.last_request.headers


def test_search_content_is_source_json_not_our_summary(
    mock_transport, provider_factory, make_repo_item
) -> None:
    """快照原文必须是来源给的 JSON，而不是我们转述的一句话。

    若存的是转述，sha256 只能证明"我们的转述没变"，证明不了"来源内容没变"。
    """
    item = make_repo_item("o/r", description="d", stars=42)
    mock_transport.default = _ok_with([item])
    documents = provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)
    assert json.loads(documents[0].content) == item
    # 键排序 ⇒ 同内容同哈希（跨运行稳定）
    assert documents[0].content == json.dumps(
        item, ensure_ascii=False, sort_keys=True, indent=2
    )


def test_search_forwards_explicit_timeout_to_transport(
    mock_transport, provider_factory, make_repo_item
) -> None:
    """调用方显式传入的 `timeout_seconds` 必须到达传输层（P3 预算超时接线）。

    运行时把 `ResearchBudget.request_timeout_seconds` 透传给 `Provider.search`；
    这里用 MockTransport 断言实际进入 `TransportRequest` 的值 ——
    "请求受预算超时限制"是有机制保证的契约，不是文档声明。
    """
    mock_transport.default = _ok_with([make_repo_item("o/r", description="d")])
    provider = provider_factory(mock_transport)

    provider.search(_query(), captured_at=CAPTURED, timeout_seconds=3.5)
    assert mock_transport.last_request.timeout_seconds == 3.5

    # 不传 ⇒ 回落到 Provider 构造时的默认值（10.0）
    provider.search(_query(), captured_at=CAPTURED)
    assert mock_transport.last_request.timeout_seconds == provider.timeout_seconds


def test_search_same_content_same_hash_across_runs(
    provider_factory, make_repo_item
) -> None:
    item = make_repo_item("o/r", description="d")
    first = provider_factory(_WithDefault(_ok_with([item]))).search(
        _query(), captured_at=CAPTURED
    )
    second = provider_factory(_WithDefault(_ok_with([item]))).search(
        _query(), captured_at=CAPTURED
    )
    assert first[0].content_sha256 == second[0].content_sha256


def test_search_skips_malformed_items_without_fabricating(
    mock_transport, provider_factory, make_repo_item
) -> None:
    """缺关键字段的条目跳过，而不是编一个 source_url 出来。"""
    mock_transport.default = _ok_with(
        [
            make_repo_item("good/one"),
            {"html_url": "https://github.com/no/name"},  # 缺 full_name
            {"full_name": "no/url"},  # 缺 html_url
            {"full_name": "", "html_url": "https://github.com/x"},  # 空 full_name
            {"full_name": "bad/url", "html_url": "http://github.com/bad/url"},
            "not a mapping",
        ]
    )
    provider = provider_factory(mock_transport)
    documents = provider.search(_query(), captured_at=CAPTURED)
    assert [d.source_url for d in documents] == ["https://github.com/good/one"]
    assert provider.skipped_count == 5


def test_search_skipped_count_resets_between_calls(mock_transport, provider_factory) -> None:
    mock_transport.default = _empty_ok()
    provider = provider_factory(mock_transport)
    provider.search(_query(), captured_at=CAPTURED)
    assert provider.skipped_count == 0


# ================= 无效 URL =================


def test_search_rejects_empty_terms() -> None:
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(InvalidSourceUrlError):
            SearchQuery(terms=bad)


def test_http_transport_rejects_non_https_url() -> None:
    transport = HttpTransport(allowed_hosts=DEFAULT_ALLOWED_HOSTS, environ={})
    for bad in ("http://api.github.com/x", "ftp://api.github.com/x", "not-a-url"):
        with pytest.raises(InvalidSourceUrlError):
            # TransportRequest 自身会先拒；这里同时保证 transport._send 也拒
            transport.send(TransportRequest(url=bad))


def test_http_transport_rejects_host_outside_allowlist() -> None:
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(InvalidSourceUrlError, match="白名单"):
        transport.send(TransportRequest(url="https://evil.example.com/x"))


def test_http_transport_maps_invalid_url_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """`http.client` 对畸形 URL 抛 `InvalidURL`，必须映射成 InvalidSourceUrlError。"""
    _patch_connection(monkeypatch, raise_on_getresponse=InvalidURL("bad url"))
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(InvalidSourceUrlError, match="非法 URL"):
        transport.send(TransportRequest(url="https://api.github.com/x"))


# ================= 超时 =================


def test_search_propagates_timeout_from_transport(mock_transport, provider_factory) -> None:
    """超时是 transport 的职责；Provider 不得吞掉它。"""
    mock_transport.raise_on = {QUERY_URL: RetrievalTimeoutError("超时")}
    with pytest.raises(RetrievalTimeoutError):
        provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)


def test_http_transport_maps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connection(monkeypatch, raise_on_getresponse=TimeoutError("timed out"))
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(RetrievalTimeoutError, match="超时"):
        transport.send(TransportRequest(url="https://api.github.com/x", timeout_seconds=0.5))


def test_http_transport_maps_tls_and_os_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    for exc in (ssl.SSLError("bad cert"), OSError("connection reset"), HTTPException("boom")):
        _patch_connection(monkeypatch, raise_on_getresponse=exc)
        transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
        with pytest.raises(TransportError, match="传输失败"):
            transport.send(TransportRequest(url="https://api.github.com/x"))


def test_http_transport_maps_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    for status in (400, 403, 404, 422, 500, 503):
        _patch_connection(monkeypatch, status=status, body=b'{"message":"boom"}')
        transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
        with pytest.raises(SourceHTTPError) as info:
            transport.send(TransportRequest(url="https://api.github.com/x"))
        assert info.value.status == status


def test_http_transport_accepts_2xx_family(monkeypatch: pytest.MonkeyPatch) -> None:
    for status in (200, 201, 204):
        _patch_connection(monkeypatch, status=status, body=b"{}")
        transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
        assert transport.send(TransportRequest(url="https://api.github.com/x")).status == status


# ================= 重定向 =================


def test_http_transport_follows_allowed_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """单跳白名单内重定向：跟随，并把跳转链记进 redirect_chain。"""
    _patch_sequence(
        monkeypatch,
        [
            TransportResponse(
                url="https://api.github.com/x",
                status=302,
                headers={"location": "https://api.github.com/moved"},
                body=b"",
            ),
            TransportResponse(
                url="https://api.github.com/moved", status=200, headers={}, body=b"{}"
            ),
        ],
    )
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    response = transport.send(TransportRequest(url="https://api.github.com/x"))
    assert response.status == 200
    assert response.redirect_chain == (
        "https://api.github.com/x",
        "https://api.github.com/moved",
    )


def test_http_transport_rejects_cross_host_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connection(
        monkeypatch,
        sequence=TransportResponse(
            url="https://api.github.com/x",
            status=302,
            headers={"location": "https://evil.example.com/steal"},
            body=b"",
        ),
    )
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(RedirectNotAllowedError, match="白名单"):
        transport.send(TransportRequest(url="https://api.github.com/x"))


def test_http_transport_rejects_internal_network_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跳到内网地址（典型 SSRF 形态）必须与跨主机同一条规则拦住。"""
    for target in (
        "https://127.0.0.1/admin",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/x",
    ):
        _patch_connection(
            monkeypatch,
            sequence=TransportResponse(
                url="https://api.github.com/x",
                status=302,
                headers={"location": target},
                body=b"",
            ),
        )
        transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
        with pytest.raises(RedirectNotAllowedError):
            transport.send(TransportRequest(url="https://api.github.com/x"))


def test_http_transport_rejects_http_downgrade_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connection(
        monkeypatch,
        sequence=TransportResponse(
            url="https://api.github.com/x",
            status=302,
            headers={"location": "http://api.github.com/x"},
            body=b"",
        ),
    )
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(RedirectNotAllowedError):
        transport.send(TransportRequest(url="https://api.github.com/x"))


def test_http_transport_rejects_relative_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connection(
        monkeypatch,
        sequence=TransportResponse(
            url="https://api.github.com/x", status=302, headers={"location": "/relative"}, body=b""
        ),
    )
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(RedirectNotAllowedError, match="相对路径"):
        transport.send(TransportRequest(url="https://api.github.com/x"))


def test_http_transport_rejects_redirect_chain_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    """无限重定向必须被次数上限截断，而不是转到天荒地老。"""

    def always_redirect(self: Any, url: str, request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            url=url, status=302, headers={"location": f"https://api.github.com/{url[-1]}x"}, body=b""
        )

    monkeypatch.setattr(HttpTransport, "_send_once", always_redirect)
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(RedirectNotAllowedError, match="次数"):
        transport.send(
            TransportRequest(url="https://api.github.com/x", max_redirects=2)
        )


def test_http_transport_raises_when_redirect_has_no_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_connection(
        monkeypatch,
        sequence=TransportResponse(
            url="https://api.github.com/x", status=302, headers={}, body=b""
        ),
    )
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(TransportError, match="Location"):
        transport.send(TransportRequest(url="https://api.github.com/x"))


def test_http_transport_handles_all_redirect_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """303 / 307 / 308 必须与 301 / 302 同等处理，不能漏。"""
    assert REDIRECT_STATUSES == frozenset({301, 302, 303, 307, 308})
    for status in sorted(REDIRECT_STATUSES):
        _patch_sequence(
            monkeypatch,
            [
                TransportResponse(
                    url="https://api.github.com/x",
                    status=status,
                    headers={"location": "https://api.github.com/next"},
                    body=b"",
                ),
                TransportResponse(
                    url="https://api.github.com/next", status=200, headers={}, body=b"{}"
                ),
            ],
        )
        transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
        assert transport.send(TransportRequest(url="https://api.github.com/x")).status == 200


def test_http_transport_does_not_follow_other_3xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """304 / 300 不是重定向语义，应作为最终响应交给上层，而不是尝试跟随。"""
    for status in (300, 304):
        _patch_connection(monkeypatch, status=status, body=b"{}")
        transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
        with pytest.raises(SourceHTTPError) as info:
            transport.send(TransportRequest(url="https://api.github.com/x"))
        assert info.value.status == status


# ================= 超大响应 =================


class _ChunkedFake:
    """按块吐出数据并记录每次请求的读取大小。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._chunks.pop(0) if self._chunks else b""


def test_read_capped_rejects_oversized_body() -> None:
    with pytest.raises(ContentTooLargeError, match="超过上限"):
        _read_capped(_ChunkedFake([b"x" * 100, b"y" * 100]), 150, "https://api.github.com/x")


def test_read_capped_allows_body_exactly_at_limit() -> None:
    fake = _ChunkedFake([b"x" * 100, b"y" * 50])
    assert _read_capped(fake, 150, "u") == b"x" * 100 + b"y" * 50


def test_read_capped_stops_early_not_after_reading_everything() -> None:
    """必须边读边判：读完再判等于限额失效（内存已经吃满了）。"""
    fake = _ChunkedFake([b"x" * 100] * 100)
    with pytest.raises(ContentTooLargeError):
        _read_capped(fake, 300, "u")
    # 只读了少量块就中止，而不是把 100 块全读进来
    assert len(fake.read_sizes) <= 5


def test_http_transport_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connection(monkeypatch, body=b"z" * 5000)
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    with pytest.raises(ContentTooLargeError):
        transport.send(TransportRequest(url="https://api.github.com/x", max_response_bytes=1000))


def test_provider_passes_limits_into_request(mock_transport, provider_factory) -> None:
    mock_transport.default = _empty_ok()
    provider_factory(
        mock_transport, timeout_seconds=1.5, max_response_bytes=2048, max_redirects=1
    ).search(_query(), captured_at=CAPTURED)
    request = mock_transport.last_request
    assert request.timeout_seconds == 1.5
    assert request.max_response_bytes == 2048
    assert request.max_redirects == 1


# ================= 畸形响应 =================


def test_search_rejects_non_json_body(mock_transport, provider_factory) -> None:
    mock_transport.default = _ok_body(b"<html>not json</html>")
    with pytest.raises(SourceResponseError, match="不是合法 JSON"):
        provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)


def test_search_rejects_invalid_utf8_body(mock_transport, provider_factory) -> None:
    mock_transport.default = _ok_body(b"\xff\xfe\x00bad")
    with pytest.raises(SourceResponseError, match="UTF-8"):
        provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)


def test_search_rejects_empty_body(mock_transport, provider_factory) -> None:
    mock_transport.default = _ok_body(b"")
    with pytest.raises(SourceResponseError, match="空响应体"):
        provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)


def test_search_rejects_non_object_top_level(mock_transport, provider_factory) -> None:
    mock_transport.default = _ok_body(b"[1,2,3]")
    with pytest.raises(SourceResponseError, match="顶层不是 JSON 对象"):
        provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)


def test_search_rejects_missing_items(mock_transport, provider_factory) -> None:
    mock_transport.default = _ok_body(b'{"total_count":0}')
    with pytest.raises(SourceResponseError, match="items"):
        provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)


def test_search_treats_api_error_message_as_failure_not_empty_result(
    mock_transport, provider_factory
) -> None:
    """GitHub 的错误响应形如 `{"message": "..."}`。

    把它当"零结果"会静默产出空快照 —— 一次限额失败会伪装成"这个查询没有结果"，
    这是最危险的一类静默失败。
    """
    body = b'{"message":"API rate limit exceeded for 1.2.3.4","documentation_url":"https://x"}'
    mock_transport.default = _ok_body(body)
    with pytest.raises(SourceResponseError, match="rate limit"):
        provider_factory(mock_transport).search(_query(), captured_at=CAPTURED)


def test_search_empty_items_is_a_valid_empty_result(mock_transport, provider_factory) -> None:
    """`items: []` 是**合法**的空结果，不得与错误响应混为一谈。"""
    mock_transport.default = _empty_ok()
    assert provider_factory(mock_transport).search(_query(), captured_at=CAPTURED) == ()


# ================= 默认构造 / 快照文件名 =================


def test_build_default_provider_uses_http_transport() -> None:
    """真实网络的构造点只有一个 —— 这条测试把它钉住。"""
    provider = build_default_provider()
    assert isinstance(provider.transport, HttpTransport)
    assert provider.transport.allowed_hosts == frozenset({API_HOST})
    assert provider.provider_id.startswith("github.repository_search.")


def test_default_allowed_hosts_contains_only_api_github() -> None:
    assert DEFAULT_ALLOWED_HOSTS == frozenset({"api.github.com"})


# ================= 代理解析 =================
#
# 这一节的存在理由：`http.client` **不会**读系统 / 环境里的代理设置，而 `urllib.request` 会。
# 实测在必须走代理的机器上，裸 HTTPSConnection 直连会 `WinError 10060` 超时 ——
# 于是"代码看起来没问题、就是连不上"。代理支持因此是功能的一部分，不是可选装饰。
#
# 本节的测试**全部自己声明代理配置**（通过 `environ=` / `proxy_env` fixture /
# `monkeypatch.setenv`），不依赖运行机器上的代理变量。
# 原因见 `conftest.py` 的"代理环境隔离"一节：机器代理会让"直连"断言在某些机器上挂，
# 而让"走代理"断言在另一些机器上**假性通过**（捡到了环境里的残留）。


def test_resolve_proxy_prefers_https_then_http() -> None:
    env = {
        "HTTPS_PROXY": "http://proxy-https:3128",
        "HTTP_PROXY": "http://proxy-http:8080",
    }
    assert resolve_proxy("https://api.github.com/x", env) == "http://proxy-https:3128"
    assert resolve_proxy("https://api.github.com/x", {"HTTP_PROXY": "http://p:8080"}) == (
        "http://p:8080"
    )
    assert resolve_proxy("https://api.github.com/x", {"https_proxy": "http://lower:1"}) == (
        "http://lower:1"
    )


def test_resolve_proxy_returns_none_without_config() -> None:
    assert resolve_proxy("https://api.github.com/x", {}) is None
    assert resolve_proxy("https://api.github.com/x", {"HTTPS_PROXY": "   "}) is None


def test_resolve_proxy_falls_back_to_process_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`environ=None` 时必须读 `os.environ` —— 这是默认 transport 的真实行为。

    与 `test_default_transport_resolves_proxy_from_process_environ` 的区别：
    那条从 `HttpTransport.send` 走完整链路，这条只钉 `resolve_proxy` 的回落语义，
    失败时能一眼看出是"解析层"还是"传输层"的问题。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://from-process:1234")
    assert resolve_proxy("https://api.github.com/x") == "http://from-process:1234"
    assert resolve_proxy("https://api.github.com/x", None) == "http://from-process:1234"


def test_resolve_proxy_isolated_from_ambient_environment() -> None:
    """不传 environ 且进程无代理配置时应直连。

    注意：本模块的代理相关键已被 conftest 的**函数级** autouse fixture
    （`_neutralise_proxy_and_token_env`）逐条 `delenv`，
    所以"进程无代理配置"在这个模块里是**确定成立**的，而不是"希望机器上没设"。
    这条测试同时也是隔离机制的自检 —— 若隔离失效，它会立刻变红。
    """
    assert resolve_proxy("https://api.github.com/x") is None


def test_resolve_proxy_explicit_overrides_env() -> None:
    env = {"HTTPS_PROXY": "http://from-env:1"}
    assert resolve_proxy("https://api.github.com/x", env, "http://explicit:2") == (
        "http://explicit:2"
    )


def test_resolve_proxy_honours_no_proxy() -> None:
    env = {"HTTPS_PROXY": "http://p:1", "NO_PROXY": "example.com"}
    assert resolve_proxy("https://api.example.com/x", env) is None
    assert resolve_proxy("https://api.github.com/x", env) == "http://p:1"


def test_no_proxy_supports_wildcard_and_suffix_matching() -> None:
    assert is_no_proxy("https://api.github.com/x", {"NO_PROXY": "*"}) is True
    assert is_no_proxy("https://api.github.com/x", {"NO_PROXY": "github.com"}) is True
    assert is_no_proxy("https://api.github.com/x", {"NO_PROXY": ".github.com"}) is True
    assert is_no_proxy("https://api.github.com/x", {"NO_PROXY": "a.com, github.com"}) is True
    # 后缀匹配必须按"点分边界"，不能把 notgithub.com 也匹配上
    assert is_no_proxy("https://notgithub.com/x", {"NO_PROXY": "github.com"}) is False
    assert is_no_proxy("https://api.github.com/x", {"NO_PROXY": "other.com"}) is False


def test_parse_proxy_url() -> None:
    assert parse_proxy_url("http://127.0.0.1:7897") == ("127.0.0.1", 7897)
    assert parse_proxy_url("http://proxy.local") == ("proxy.local", 8080)
    assert parse_proxy_url("http://proxy.local:3128/") == ("proxy.local", 3128)


def test_parse_proxy_url_rejects_bad_forms() -> None:
    for bad in ("127.0.0.1:7897", "socks5://p:1", "http://", "http://p:notaport", "http://u:p@p:1"):
        with pytest.raises(InvalidSourceUrlError):
            parse_proxy_url(bad)


def test_transport_sets_tunnel_when_proxy_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配了代理就必须走 CONNECT 隧道，**且参数方向正确**。

    这条测试的核心是下面两个断言：
    - 构造参数是**代理**地址（TCP 连到代理）；
    - `set_tunnel` 参数是**目标主机**（隧道里 CONNECT 到目标）。

    方向搞反不会报"参数错"，而是表现成 RemoteDisconnected / 超时，
    看起来像网络故障 —— 所以必须由测试钉死，不能靠代码 review 保证。
    """
    tunnels: list[tuple[str, int]] = []
    constructed: list[tuple[str, object]] = []

    class FakeConnection:
        def __init__(
            self, host: str, port: object = None, timeout: float | None = None, context: object = None
        ) -> None:
            constructed.append((host, port))

        def set_tunnel(self, host: str, port: int, headers: object = None) -> None:
            tunnels.append((host, port))

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            return _FakeRawResponse(200, {}, b"{}")

        def close(self) -> None:
            return None

    from agentboard.providers import http_transport as module

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    transport = HttpTransport(allowed_hosts=ALLOWED, proxy_url="http://127.0.0.1:7897", environ={})
    transport.send(TransportRequest(url="https://api.github.com/x"))

    assert constructed == [("127.0.0.1", 7897)], "应把 TCP 连到代理主机与端口"
    assert tunnels == [("api.github.com", 443)], "应 CONNECT 到原始目标主机"


def test_transport_does_not_set_tunnel_without_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnels: list[tuple[str, int]] = []
    constructed: list[tuple[str, object]] = []

    class FakeConnection:
        def __init__(
            self, host: str, port: object = None, timeout: float | None = None, context: object = None
        ) -> None:
            constructed.append((host, port))

        def set_tunnel(self, host: str, port: int, headers: object = None) -> None:
            tunnels.append((host, port))

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            return _FakeRawResponse(200, {}, b"{}")

        def close(self) -> None:
            return None

    from agentboard.providers import http_transport as module

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    transport = HttpTransport(allowed_hosts=ALLOWED, environ={})
    transport.send(TransportRequest(url="https://api.github.com/x"))

    assert tunnels == [], "没有代理配置时不得设置隧道"
    assert constructed == [("api.github.com", None)], "直连时应连到目标主机"


def test_transport_reads_proxy_from_injected_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnels: list[tuple[str, int]] = []
    constructed: list[tuple[str, object]] = []

    class FakeConnection:
        def __init__(
            self, host: str, port: object = None, timeout: float | None = None, context: object = None
        ) -> None:
            constructed.append((host, port))

        def set_tunnel(self, host: str, port: int, headers: object = None) -> None:
            tunnels.append((host, port))

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            return _FakeRawResponse(200, {}, b"{}")

        def close(self) -> None:
            return None

    from agentboard.providers import http_transport as module

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    transport = HttpTransport(
        allowed_hosts=ALLOWED, environ={"HTTPS_PROXY": "http://env-proxy:9999"}
    )
    transport.send(TransportRequest(url="https://api.github.com/x"))

    assert constructed == [("env-proxy", 9999)]
    assert tunnels == [("api.github.com", 443)]


def test_transport_no_proxy_env_disables_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    tunnels: list[tuple[str, int]] = []

    class FakeConnection:
        def __init__(
            self, host: str, port: object = None, timeout: float | None = None, context: object = None
        ) -> None:
            pass

        def set_tunnel(self, host: str, port: int, headers: object = None) -> None:
            tunnels.append((host, port))

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            return _FakeRawResponse(200, {}, b"{}")

        def close(self) -> None:
            return None

    from agentboard.providers import http_transport as module

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    transport = HttpTransport(
        allowed_hosts=ALLOWED,
        environ={"HTTPS_PROXY": "http://p:1", "NO_PROXY": "api.github.com"},
    )
    transport.send(TransportRequest(url="https://api.github.com/x"))
    assert tunnels == [], "NO_PROXY 命中时必须直连"


def test_default_transport_has_no_hardcoded_proxy() -> None:
    """默认 transport 不得硬编码代理地址 —— 代理是环境属性，不是代码属性。

    注意这里断言的是**字段值**，不是解析结果：`proxy_url is None` 表示
    "没人在代码里写死代理"，而"解析后走哪条路"取决于 environ（见下一条测试）。
    """
    transport = HttpTransport()
    assert transport.proxy_url is None
    assert transport.environ is None, "生产路径必须允许 environ 回落到 os.environ"


def test_default_transport_resolves_proxy_from_process_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 transport（`environ=None`）必须真的读**进程**代理环境。

    这条测试补的是上一条的盲区：`HttpTransport()` 不传 environ 时，
    `resolve_proxy` 会回落到 `os.environ` —— 这是**生产行为**，
    P2.1 复核时已确认它在实机上救过一次命（否则裸 `HTTPSConnection`
    会在必须走代理的机器上 `WinError 10060` 超时）。所以必须有测试钉住它。

    同时这条测试也说明了为什么其它测试要显式隔离环境：
    只要进程里有 `HTTPS_PROXY`，**默认构造的 transport 就会走代理**。
    让"直连"与"走代理"的测试共享同一个进程环境，结果就取决于机器配置。

    实现说明：`conftest.py` 的隔离 fixture 把本模块的 `os.environ` 换成了
    一个空 dict，所以这里是往**那个空 dict** 里 `setenv`（monkeypatch 会同时
    改写它替换过的那份引用），等价于"临时给进程设上代理变量"。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://process-proxy:8080")

    tunnels: list[tuple[str, int]] = []
    constructed: list[tuple[str, object]] = []

    class FakeConnection:
        def __init__(
            self, host: str, port: object = None, timeout: float | None = None, context: object = None
        ) -> None:
            constructed.append((host, port))

        def set_tunnel(self, host: str, port: int, headers: object = None) -> None:
            tunnels.append((host, port))

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            return _FakeRawResponse(200, {}, b"{}")

        def close(self) -> None:
            return None

    from agentboard.providers import http_transport as module

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    # 刻意**不传** environ / proxy_url：验证默认回落路径
    transport = HttpTransport(allowed_hosts=ALLOWED)
    assert transport.environ is None
    transport.send(TransportRequest(url="https://api.github.com/x"))

    assert constructed == [("process-proxy", 8080)], "默认 transport 必须读进程代理"
    assert tunnels == [("api.github.com", 443)]


def test_default_transport_goes_direct_when_process_environ_has_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程环境里没有代理变量时必须直连 —— 与上一条配对，覆盖回落的两个方向。"""
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)

    tunnels: list[tuple[str, int]] = []
    constructed: list[tuple[str, object]] = []

    class FakeConnection:
        def __init__(
            self, host: str, port: object = None, timeout: float | None = None, context: object = None
        ) -> None:
            constructed.append((host, port))

        def set_tunnel(self, host: str, port: int, headers: object = None) -> None:
            tunnels.append((host, port))

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            return _FakeRawResponse(200, {}, b"{}")

        def close(self) -> None:
            return None

    from agentboard.providers import http_transport as module

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    HttpTransport(allowed_hosts=ALLOWED).send(TransportRequest(url="https://api.github.com/x"))

    assert not tunnels, "进程环境没有代理配置时不得设置隧道"
    assert constructed == [("api.github.com", None)]


def test_provider_with_mock_transport_ignores_process_proxy(
    monkeypatch: pytest.MonkeyPatch, mock_transport, provider_factory
) -> None:
    """Provider 走 Mock transport 时，进程里的代理设置**不可能**影响它。

    这是"Provider 逻辑完全离线"最直接的一条证据：
    先故意把进程代理设成指向黑洞的地址，再用 mock 跑一次检索 ——
    如果 Provider 里藏了任何网络调用（或读了 `os.environ` 自己发请求），
    这条会立刻以超时 / 连接失败暴露，而不是悄悄通过。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://203.0.113.1:1")  # TEST-NET-3，必黑洞
    monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.1:1")
    mock_transport.default = _ok_with([repo_item_for_test("o/r", "d")])
    provider = provider_factory(mock_transport)

    documents = provider.search(_query(), captured_at=CAPTURED)

    assert len(documents) == 1
    assert documents[0].source_url == "https://github.com/o/r"
    assert mock_transport.call_count == 1, "Provider 只能通过注入的 transport 取数"


# ================= 测试环境隔离自检 =================
#
# 下面几条不是"测生产代码"，而是**测测试自身的隔离机制**。
# 理由：代理 / 凭据路径的测试要成立，前提是"环境里没有残留"。
# 如果这个前提悄悄失效（有人删了 conftest 的 autouse fixture、
# 或改了被隔离的键名单），测试会退化成"看机器心情"——
# 在配了代理的机器上假性通过，在没配的机器上过。
# 所以把前提本身也钉成断言，让它在**所有机器上都可验证**。


def test_neutralised_env_keys_are_absent(neutralised_env_keys: tuple[str, ...]) -> None:
    """隔离名单里的每个键都不得在测试进程内可见（隔离生效的自检）。

    若这条失败，说明 `conftest.py` 的 `_neutralise_proxy_and_token_env`
    失效，或被隔离的键名单与实际需要不一致。
    """
    for key in neutralised_env_keys:
        assert key not in os.environ, (
            f"{key} 在本目录测试进程里可见 —— 环境隔离失效。"
            "请检查 tests/providers/conftest.py 的 `_neutralise_proxy_and_token_env`。"
        )


def test_isolation_does_not_wipe_unrelated_environment(
    neutralised_env_keys: tuple[str, ...],
) -> None:
    """隔离**只**移除名单内的键，不得清空整个环境。

    这条是本次复核修复的核心回归护栏：早期实现用
    `setattr(os, "environ", {})` 做会话级全局替换，会把
    `AGENTBOARD_REAL_SMOKE` / `AGENTBOARD_REAL_SNAPSHOT_DIR` 一起抹掉，
    进而让 `test_real_smoke.py` 在"已被收集"之后读不到自己的配置，
    静默降级成匿名 + 默认目录。

    这里用一个**非名单内**的哨兵变量来证明环境没有被整体清空。
    同时断言几个 smoke 测试依赖的变量名不在隔离名单里 ——
    名单一旦被人加进这些键，本测试会立刻变红。
    """
    unrelated = {"AGENTBOARD_REAL_SMOKE", "AGENTBOARD_REAL_SNAPSHOT_DIR"}
    overlap = unrelated & set(neutralised_env_keys)
    assert not overlap, (
        f"隔离名单不应包含 smoke test 自身的配置变量：{sorted(overlap)} —— "
        "否则真实 smoke 会在收集后被静默降级。"
    )
    # 名单内应有代理与 token，名单外应有 smoke 配置：两个方向都钉住
    assert "HTTPS_PROXY" in neutralised_env_keys
    assert "GITHUB_TOKEN" in neutralised_env_keys
    assert os.environ is not None, "os.environ 必须仍是可用的映射（不得被整体替换）"


def test_resolve_proxy_default_path_is_none_only_because_of_isolation() -> None:
    """把上面两条与"直连"结论串起来：干净环境下默认路径必须解析为直连。

    这条是隔离机制的**功能验收**——不只是"环境里没有变量"这个静态事实，
    而是"因此 `resolve_proxy` 的默认回落真的给出直连"。
    """
    assert resolve_proxy("https://api.github.com/x") is None


def test_real_smoke_env_vars_survive_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 smoke 依赖的环境变量必须能穿过隔离被读到。

    模拟 smoke 的真实使用方式：设好 `AGENTBOARD_REAL_SMOKE` /
    `AGENTBOARD_REAL_SNAPSHOT_DIR` / `GITHUB_TOKEN`，然后确认在**测试体内**
    （即运行期）三者都可读。这就是第一版实现失败的地方——
    收集期能读到、运行期读不到。
    """
    monkeypatch.setenv("AGENTBOARD_REAL_SMOKE", "1")
    monkeypatch.setenv("AGENTBOARD_REAL_SNAPSHOT_DIR", "D:/probe/snap")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_probe")

    assert os.environ.get("AGENTBOARD_REAL_SMOKE") == "1"
    assert os.environ.get("AGENTBOARD_REAL_SNAPSHOT_DIR") == "D:/probe/snap"
    assert os.environ.get("GITHUB_TOKEN") == "ghp_probe"


def test_snapshot_filename_is_content_addressed() -> None:
    digest = "a" * 64
    assert snapshot_filename(digest) == f"sha256-{digest}.json"


def test_snapshot_filename_rejects_bad_hash() -> None:
    from agentboard.providers.errors import ProviderError

    for bad in ("", "ABC", "a" * 63, "g" * 64, None):
        with pytest.raises(ProviderError):
            snapshot_filename(bad)  # type: ignore[arg-type]


# ================= 辅助 =================


def repo_item_for_test(full_name: str, description: str) -> dict[str, object]:
    """构造一个最小的检索结果条目（不引入 conftest 的依赖，便于在任意测试里用）。"""
    return {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "stargazers_count": 1,
        "description": description,
        "license": {"spdx_id": "MIT"},
    }


class _WithDefault:
    """只有一个默认响应的最小 mock（用于"两次独立 provider"这类断言）。"""

    def __init__(self, response: TransportResponse) -> None:
        self._response = response
        self.requests: list[TransportRequest] = []
        self.default = response

    def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        return self._response


def _ok_body(body: bytes) -> TransportResponse:
    return TransportResponse(url=QUERY_URL, status=200, headers={}, body=body)


def _empty_ok() -> TransportResponse:
    return _ok_body(json.dumps({"total_count": 0, "items": []}).encode("utf-8"))


def _ok_with(items: list[dict[str, object]]) -> TransportResponse:
    return _ok_body(json.dumps({"total_count": len(items), "items": items}).encode("utf-8"))


class _FakeRawResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self._headers = headers
        self._body = body
        self._read = False

    def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())

    def close(self) -> None:
        return None


def _patch_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    body: bytes = b"{}",
    headers: dict[str, str] | None = None,
    sequence: TransportResponse | None = None,
    raise_on_getresponse: BaseException | None = None,
) -> None:
    """把 `HTTP(S)Connection` 换成不碰 socket 的假实现，**每次都返回同一响应**。

    单一响应足够测：超时 / TLS / 非 2xx / 超大 / 缺 Location ——
    这些分支只需要"一跳就出事"。需要真正多跳的场景用 `_patch_sequence`。
    """
    from agentboard.providers import http_transport as module

    class FakeConnection:
        def __init__(self, host: str, timeout: float | None = None, context: object = None) -> None:
            self.host = host
            self.timeout = timeout

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            if raise_on_getresponse is not None:
                raise raise_on_getresponse
            if sequence is not None:
                return _FakeRawResponse(sequence.status, dict(sequence.headers), sequence.body)
            return _FakeRawResponse(status, dict(headers or {}), body)

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(module, "HTTPConnection", FakeConnection)


def _patch_sequence(
    monkeypatch: pytest.MonkeyPatch, responses: list[TransportResponse]
) -> None:
    """按**响应队列**返回：第 N 次调用返回 `responses[N]`。

    这里用队列是合理的（与"stateful 优先"的原则不冲突）：要测的正是
    "第 1 跳返回 302、第 2 跳返回 200"这种**跨跳行为**，
    它本身就是顺序敏感的，用 URL 映射反而表达不了。
    """
    from agentboard.providers import http_transport as module

    pending = list(responses)

    class FakeConnection:
        def __init__(self, host: str, timeout: float | None = None, context: object = None) -> None:
            self.host = host
            self.timeout = timeout

        def request(self, method: str, path: str, headers: object = None) -> None:
            return None

        def getresponse(self) -> _FakeRawResponse:
            assert pending, "响应队列已耗尽"
            response = pending.pop(0)
            return _FakeRawResponse(response.status, dict(response.headers), response.body)

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(module, "HTTPConnection", FakeConnection)

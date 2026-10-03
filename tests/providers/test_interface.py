"""Provider 接口与传输抽象的单元测试（P2.1）。

覆盖 `SearchQuery` / `TransportRequest` / `TransportResponse` / `RetrievedDocument`
的自校验，以及 `validate_redirect_target` 的三条规则。
这些是 Provider 层所有逻辑的地基：地基上的每条断言失败都必须是"立刻可见"的，
而不是等到某次真实采集才发现。
"""

from __future__ import annotations

import pytest

from agentboard.providers.errors import (
    ContentTooLargeError,
    InvalidSourceUrlError,
    RedirectNotAllowedError,
    SourceResponseError,
)
from agentboard.providers.interface import (
    MAX_TEXT_CHARS,
    RetrievedDocument,
    SearchQuery,
    TransportRequest,
    TransportResponse,
    compute_content_sha256,
    is_https_request_url,
    is_https_url,
    url_host,
    validate_redirect_target,
)

ALLOWED = frozenset({"api.github.com"})


# ---- URL 形态 ----


def test_canonical_url_accepts_https_without_query() -> None:
    assert is_https_url("https://github.com/o/r") is True
    assert is_https_url("https://github.com/o/r/blob/main/a.md") is True
    assert is_https_url("https://github.com") is True


def test_canonical_url_rejects_query_and_insecure_forms() -> None:
    # 规范 URL 带 query 会被拒：同一份资料不该因查询词不同而有不同地址
    assert is_https_url("https://api.github.com/search/repositories?q=x") is False
    assert is_https_url("http://github.com/o/r") is False
    assert is_https_url("file:///etc/passwd") is False
    assert is_https_url("https://user:pw@github.com/o/r") is False
    assert is_https_url("") is False
    assert is_https_url("not a url") is False


def test_request_url_accepts_query_but_not_fragment() -> None:
    assert is_https_request_url("https://api.github.com/search/repositories?q=vector+db") is True
    assert is_https_request_url("https://api.github.com/search/repositories") is True
    # fragment 无意义且是常见的注入载体
    assert is_https_request_url("https://api.github.com/x?a=1#frag") is False
    assert is_https_request_url("http://api.github.com/x") is False


def test_url_host_extraction() -> None:
    assert url_host("https://api.github.com/search?q=1") == "api.github.com"
    assert url_host("https://api.github.com") == "api.github.com"
    assert url_host("http://api.github.com") == ""
    assert url_host("garbage") == ""


# ---- SearchQuery ----


def test_search_query_normalizes_whitespace_only() -> None:
    query = SearchQuery(terms="  vector   database  ", limit=5)
    # 只折叠空白：不改大小写、不做同义词扩展 —— 快照里登记的查询词要能对回用户输入
    assert query.normalized_terms == "vector database"


def test_search_query_preserves_case() -> None:
    assert SearchQuery(terms="VectorDB").normalized_terms == "VectorDB"


def test_search_query_rejects_empty_terms() -> None:
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(InvalidSourceUrlError):
            SearchQuery(terms=bad)


def test_search_query_rejects_out_of_range_limit() -> None:
    for bad in (0, -1, 101, 1000):
        with pytest.raises(InvalidSourceUrlError):
            SearchQuery(terms="x", limit=bad)
    # bool 是 int 的子类，必须显式拒绝，否则 limit=True 会变成 limit=1
    with pytest.raises(InvalidSourceUrlError):
        SearchQuery(terms="x", limit=True)
    assert SearchQuery(terms="x", limit=100).limit == 100


# ---- TransportRequest / TransportResponse ----


def test_transport_request_validates_url_and_limits() -> None:
    TransportRequest(url="https://api.github.com/x?q=1")
    with pytest.raises(InvalidSourceUrlError):
        TransportRequest(url="http://api.github.com/x")
    with pytest.raises(InvalidSourceUrlError):
        TransportRequest(url="https://api.github.com/x", timeout_seconds=0)
    with pytest.raises(InvalidSourceUrlError):
        TransportRequest(url="https://api.github.com/x", max_response_bytes=0)
    with pytest.raises(InvalidSourceUrlError):
        TransportRequest(url="https://api.github.com/x", max_redirects=-1)


def test_transport_response_rejects_non_https_and_bad_types() -> None:
    TransportResponse(url="https://api.github.com/x", status=200, headers={}, body=b"{}")
    with pytest.raises(SourceResponseError):
        TransportResponse(url="http://api.github.com/x", status=200, headers={}, body=b"")
    with pytest.raises(SourceResponseError):
        TransportResponse(url="https://api.github.com/x", status=True, headers={}, body=b"")
    with pytest.raises(SourceResponseError):
        TransportResponse(
            url="https://api.github.com/x", status=200, headers={}, body="not bytes"
        )


# ---- RetrievedDocument ----


def test_retrieved_document_build_computes_hash() -> None:
    document = RetrievedDocument.build(
        source_id="s:1",
        query_terms="vector db",
        source_url="https://github.com/o/r",
        title="o/r",
        captured_at="2026-09-23",
        content="hello world",
    )
    assert document.content_sha256 == compute_content_sha256("hello world")
    assert len(document.content_sha256) == 64


def test_retrieved_document_hash_is_byte_sensitive() -> None:
    """哈希必须对空白敏感：否则"只改了空白"的第二次采集会被误判为重复而丢弃。"""
    base = {
        "source_id": "s:1",
        "query_terms": "q",
        "source_url": "https://github.com/o/r",
        "title": "t",
        "captured_at": "2026-09-23",
    }
    first = RetrievedDocument.build(content="abc", **base)
    second = RetrievedDocument.build(content="abc ", **base)
    assert first.content_sha256 != second.content_sha256


def test_retrieved_document_rejects_tampered_hash() -> None:
    """显式声明的 sha256 与内容不符时必须拒绝 —— 这是"防伪造"的关键一步。"""
    with pytest.raises(SourceResponseError, match="不符"):
        RetrievedDocument(
            source_id="s:1",
            query_terms="q",
            source_url="https://github.com/o/r",
            title="t",
            captured_at="2026-09-23",
            content="hello",
            content_sha256="0" * 64,
        )


def test_retrieved_document_rejects_bad_fields() -> None:
    base = {
        "source_id": "s:1",
        "query_terms": "q",
        "source_url": "https://github.com/o/r",
        "title": "t",
        "captured_at": "2026-09-23",
        "content": "hello",
        "content_sha256": compute_content_sha256("hello"),
    }
    for key, bad in (
        ("source_id", ""),
        ("query_terms", "  "),
        ("source_url", "http://github.com/o/r"),
        ("title", ""),
        ("captured_at", "2026/09/23"),
        ("captured_at", "2026-09-23T10:00:00"),
        ("content", ""),
    ):
        with pytest.raises((SourceResponseError, ContentTooLargeError)):
            RetrievedDocument(**{**base, key: bad})  # type: ignore[arg-type]


def test_retrieved_document_rejects_oversized_content() -> None:
    huge = "x" * (MAX_TEXT_CHARS + 1)
    with pytest.raises(ContentTooLargeError):
        RetrievedDocument.build(
            source_id="s:1",
            query_terms="q",
            source_url="https://github.com/o/r",
            title="t",
            captured_at="2026-09-23",
            content=huge,
        )


def test_retrieved_document_to_dict_round_trips_fields() -> None:
    document = RetrievedDocument.build(
        source_id="s:1",
        query_terms="q",
        source_url="https://github.com/o/r",
        title="t",
        captured_at="2026-09-23",
        content="hello",
    )
    payload = document.to_dict()
    assert payload["content_sha256"] == document.content_sha256
    assert payload["content"] == "hello"


# ---- 重定向校验 ----


def test_redirect_allows_same_host_https() -> None:
    validate_redirect_target(
        "https://api.github.com/a", "https://api.github.com/b?q=1", ALLOWED
    )


def test_redirect_rejects_cross_host() -> None:
    with pytest.raises(RedirectNotAllowedError, match="白名单"):
        validate_redirect_target(
            "https://api.github.com/a", "https://evil.example.com/b", ALLOWED
        )
    # 跳到内网地址是最典型的 SSRF 形态，必须同一条规则拦住
    with pytest.raises(RedirectNotAllowedError):
        validate_redirect_target("https://api.github.com/a", "https://127.0.0.1/b", ALLOWED)


def test_redirect_rejects_insecure_downgrade() -> None:
    with pytest.raises(RedirectNotAllowedError, match="不是 https"):
        validate_redirect_target("https://api.github.com/a", "http://api.github.com/b", ALLOWED)


def test_redirect_rejects_credentials_in_target() -> None:
    """带凭据的重定向目标必须被拒。

    拦截点是 URL 正则（主机字符集不含 `@`），所以报错文案是"不是 https URL"。
    这里断言**结果**（被拒）而不是断言错误消息措辞 ——
    消息会随实现调整，但"带凭据的跳转目标进不来"是契约。
    """
    with pytest.raises(RedirectNotAllowedError):
        validate_redirect_target(
            "https://api.github.com/a", "https://u:p@api.github.com/b", ALLOWED
        )


def test_url_regex_is_why_credentials_are_rejected() -> None:
    """把"拦截点在哪"钉死：不是某条特判分支，而是主机字符集本身。

    这条测试的作用是防止有人为了"支持带凭据的 URL"而放宽主机字符集 ——
    那样做会让上面那条重定向测试仍然通过（因为特判仍在），但整体防护已失效。
    """
    assert is_https_request_url("https://u:p@api.github.com/b") is False
    assert is_https_url("https://u:p@github.com/o/r") is False
    assert url_host("https://u:p@api.github.com/b") == ""


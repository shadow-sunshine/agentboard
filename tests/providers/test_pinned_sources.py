"""PinnedSourceProvider 离线演练（P6）：MockTransport 覆盖成功/超时/认证失败/
来源不匹配/超大内容/非法响应/主机限制。

全部离线：HTTP 行为由 MockTransport 脚本化。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.providers.errors import (
    ContentTooLargeError,
    InvalidSourceUrlError,
    RetrievalTimeoutError,
    SourceHTTPError,
    SourceResponseError,
)
from agentboard.providers.interface import SearchQuery, TransportResponse
from agentboard.providers.pinned_sources import (
    PinnedSourceProvider,
    SourceFileSpec,
    parse_source_plan,
)
from agentboard.research.snapshot import expected_excerpt_for

COMMIT = "a" * 40
QUERY = SearchQuery(terms="pgvector deployment_complexity")


def _ok_transport(content: str, commit: str = COMMIT) -> Any:
    """按 URL 分发的 Mock transport：commit 解析 + contents 内容。"""

    class _T:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def send(self, request: Any) -> TransportResponse:
            self.requests.append(request)
            url = request.url
            body: bytes
            if "/commits/" in url:
                body = json.dumps({"sha": commit}).encode("utf-8")
            elif "/contents/" in url:
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                body = json.dumps(
                    {
                        "content": encoded,
                        "encoding": "base64",
                        "sha": hashlib.sha1(
                            f"blob {len(content.encode('utf-8'))}\0".encode("ascii")
                            + content.encode("utf-8")
                        ).hexdigest(),
                        "path": "README.md",
                        "size": len(content.encode("utf-8")),
                    }
                ).encode("utf-8")
            else:
                body = b"{}"
            return TransportResponse(url=url, status=200, headers={}, body=body)

    return _T()


def _provider(transport: Any, **overrides: Any) -> PinnedSourceProvider:
    plan = parse_source_plan(
        {"candidates": {"pgvector": [{"repo": "pgvector/pgvector", "path": "README.md", "ref": "master"}]}}
    )
    return PinnedSourceProvider(
        source_plan=plan, transport=transport,
        timeout_seconds=overrides.pop("timeout_seconds", 30.0), **overrides,
    )


def test_success_returns_real_original_text_with_pinned_url() -> None:
    """成功路径：原文全文 + 固定 commit 的可重验 URL + 审计摘要。"""
    content = "pgvector original README body"
    transport = _ok_transport(content)
    provider = _provider(transport)
    docs = provider.search(QUERY, captured_at="2026-09-26")
    assert len(docs) == 1
    doc = docs[0]
    assert doc.content == content
    assert doc.source_url == f"https://github.com/pgvector/pgvector/blob/{COMMIT}/README.md"
    assert doc.content_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert provider.http_calls == 2  # 1 次 commit 解析 + 1 次 contents
    audit = provider.source_audit()
    assert audit[0]["commit"] == COMMIT and audit[0]["candidate_id"] == "pgvector"
    assert audit[0]["content_sha256"] == doc.content_sha256


def test_content_cache_avoids_second_fetch() -> None:
    transport = _ok_transport("body")
    provider = _provider(transport)
    provider.search(QUERY, captured_at="2026-09-26")
    provider.search(QUERY, captured_at="2026-09-26")
    assert provider.http_calls == 2  # 同键第二次不重复取


def test_timeout_maps_to_retryable_error() -> None:
    """超时 ⇒ RetrievalTimeoutError（可重试闭集；runner 接管重试）。"""
    class _TimeoutT:
        def send(self, request: Any) -> TransportResponse:
            raise RetrievalTimeoutError("请求超时")

    provider = _provider(_TimeoutT())
    with pytest.raises(RetrievalTimeoutError):
        provider.search(QUERY, captured_at="2026-09-26")


def test_auth_failure_maps_to_source_http_error() -> None:
    """认证失败（401/403）⇒ SourceHTTPError（可重试闭集；显式失败不静默）。"""

    class _ForbiddenT:
        def send(self, request: Any) -> TransportResponse:
            return TransportResponse(
                url=request.url, status=403, headers={}, body=b'{"message": "Forbidden"}'
            )

    provider = _provider(_ForbiddenT())
    with pytest.raises(SourceHTTPError):
        provider.search(QUERY, captured_at="2026-09-26")


def test_declared_commit_mismatch_fails_closed() -> None:
    """解析到的 commit 与声明不符（来源不匹配）⇒ 显式失败。"""
    spec = SourceFileSpec(repo="pgvector/pgvector", path="README.md", commit="c" * 40)
    plan = {"pgvector": (spec,)}
    provider = PinnedSourceProvider(
        source_plan=plan, transport=_ok_transport("body", commit=COMMIT)
    )
    with pytest.raises(SourceResponseError, match="不符"):
        provider.search(QUERY, captured_at="2026-09-26")


def test_oversized_or_non_base64_content_fails_explicitly() -> None:
    class _NoneEncodingT:
        def send(self, request: Any) -> TransportResponse:
            if "/commits/" in request.url:
                body = json.dumps({"sha": COMMIT}).encode("utf-8")
            else:
                body = json.dumps({"encoding": "none", "content": None}).encode("utf-8")
            return TransportResponse(url=request.url, status=200, headers={}, body=body)

    provider = _provider(_NoneEncodingT())
    with pytest.raises(ContentTooLargeError):
        provider.search(QUERY, captured_at="2026-09-26")


def test_github_error_payload_fails_explicitly() -> None:
    """rate limit / not found 等错误响应 ⇒ SourceResponseError（不当零结果）。"""

    class _ErrorT:
        def send(self, request: Any) -> TransportResponse:
            return TransportResponse(
                url=request.url,
                status=200,
                headers={},
                body=json.dumps({"message": "API rate limit exceeded"}).encode("utf-8"),
            )

    provider = _provider(_ErrorT())
    with pytest.raises(SourceResponseError, match="rate limit"):
        provider.search(QUERY, captured_at="2026-09-26")


def test_plan_outside_candidate_returns_zero_results() -> None:
    provider = _provider(_ok_transport("body"))
    docs = provider.search(SearchQuery(terms="unknown_candidate x"), captured_at="2026-09-26")
    assert docs == ()


def test_source_plan_rejects_unknown_fields_and_unbounded_files() -> None:
    with pytest.raises(Exception, match="未知字段"):
        parse_source_plan({"candidates": {"pgvector": [{"repo": "r", "path": "p", "evil": 1}]}})
    with pytest.raises(Exception, match="1..4"):
        parse_source_plan(
            {"candidates": {"pgvector": [
                {"repo": "r", "path": "p1"}, {"repo": "r", "path": "p2"},
                {"repo": "r", "path": "p3"}, {"repo": "r", "path": "p4"},
                {"repo": "r", "path": "p5"},
            ]}}
        )
    with pytest.raises(Exception, match="40 位"):
        SourceFileSpec(repo="r", path="p", commit="short")


def test_provider_only_allows_github_api_host() -> None:
    """主机限制：HttpTransport 白名单机制保证不出网到其它主机（含 localhost/私网）。"""
    from agentboard.providers.http_transport import HttpTransport
    from agentboard.providers.interface import TransportRequest

    transport = HttpTransport(allowed_hosts=frozenset({"api.github.com"}))
    plan = parse_source_plan(
        {"candidates": {"pgvector": [{"repo": "pgvector/pgvector", "path": "README.md"}]}}
    )
    PinnedSourceProvider(source_plan=plan, transport=transport)
    # 直接发内网/localhost 请求被白名单拒绝
    with pytest.raises(InvalidSourceUrlError):
        transport.send(TransportRequest(url="https://localhost/secret"))
    with pytest.raises(InvalidSourceUrlError):
        transport.send(TransportRequest(url="https://192.168.1.1/secret"))



def test_provider_rejects_git_blob_sha_mismatch() -> None:
    content = "body"

    class _BadBlob:
        def send(self, request: Any) -> TransportResponse:
            if "/commits/" in request.url:
                body = json.dumps({"sha": COMMIT}).encode("utf-8")
            else:
                body = json.dumps({
                    "content": base64.b64encode(content.encode()).decode("ascii"),
                    "encoding": "base64",
                    "sha": "b" * 40,
                    "path": "README.md",
                    "size": len(content.encode()),
                }).encode("utf-8")
            return TransportResponse(request.url, 200, {}, body)

    with pytest.raises(SourceResponseError, match="Git blob SHA"):
        _provider(_BadBlob()).search(QUERY, captured_at="2026-09-26")


def test_source_binding_rechecks_actual_bytes_and_excerpt() -> None:
    content = "official source text"
    provider = _provider(_ok_transport(content))
    provider.search(QUERY, captured_at="2026-09-26")
    audit = provider.source_audit()
    finding = {
        "candidate_id": "pgvector",
        "dimension_id": "deployment_complexity",
        "evidence_refs": [{
            "source_type": "fixture_snapshot",
            "source_id": "fixture:pgvector",
            "locator": "/sections/0/text",
            "excerpt": expected_excerpt_for(content),
        }],
    }
    report = {"candidate_ids": ["pgvector"], "findings": [finding]}
    assert provider.verify_report_sources(report, audit) == (True, [])
    finding["evidence_refs"][0]["excerpt"] = "tampered"
    ok, reasons = provider.verify_report_sources(report, audit)
    assert not ok and reasons


def test_source_binding_rejects_stale_audit_after_material_changed() -> None:
    provider = _provider(_ok_transport("official source text"))
    provider.search(QUERY, captured_at="2026-09-26")
    audit = provider.source_audit()
    report = {"candidate_ids": ["pgvector"], "findings": []}
    key = next(iter(provider._materials))
    entry, _ = provider._materials[key]
    provider._materials[key] = (entry, "altered source text")
    ok, reasons = provider.verify_report_sources(report, audit)
    assert not ok and any("原文字节不符" in reason for reason in reasons)

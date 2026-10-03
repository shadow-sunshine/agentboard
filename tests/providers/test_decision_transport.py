"""决策 transport 测试（P4）：离线脚本 transport + 兼容 API transport（离线断言）。

全部离线：`CompatibleApiDecisionTransport` 的网络行为用 `MockTransport` 断言，
真实网络只在显式 opt-in 的 smoke 里发生（默认不执行、不读凭据）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.providers.decision_transport import (
    CompatibleApiDecisionTransport,
    DecisionCompletionRequest,
    ScriptedDecisionTransport,
)
from agentboard.providers.errors import (
    InvalidSourceUrlError,
    SourceHTTPError,
    SourceResponseError,
    TransportError,
)
from agentboard.providers.interface import TransportRequest, TransportResponse

CAPTURED = "2026-09-26"
ENDPOINT = "https://api.example.com/v1/chat/completions"


def _request(candidate_id: str = "pgvector", dimension_id: str = "deployment_complexity") -> DecisionCompletionRequest:
    return DecisionCompletionRequest(
        prompt="OUTPUT (single JSON object):", candidate_id=candidate_id,
        dimension_id=dimension_id, timeout_seconds=3.5,
    )


def _ok_response(body: bytes, url: str = ENDPOINT) -> TransportResponse:
    return TransportResponse(url=url, status=200, headers={}, body=body)


def _chat_payload(text: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": text}}]}
    ).encode("utf-8")


# ================= ScriptedDecisionTransport =================


def test_scripted_transport_returns_responses_in_order_then_repeats_last() -> None:
    transport = ScriptedDecisionTransport(
        responses={("pgvector", "deployment_complexity"): ["first", "second"]}
    )
    assert transport.complete(_request()) == "first"
    assert transport.complete(_request()) == "second"
    assert transport.complete(_request()) == "second"


def test_scripted_transport_failures_queue_precede_responses() -> None:
    transport = ScriptedDecisionTransport(
        responses={("pgvector", "deployment_complexity"): ["ok"]},
        failures={("pgvector", "deployment_complexity"): [TransportError("抖动")]},
    )
    with pytest.raises(TransportError):
        transport.complete(_request())
    assert transport.complete(_request()) == "ok"
    assert transport.call_count("pgvector", "deployment_complexity") == 2


def test_scripted_transport_unscripted_key_fails_explicitly() -> None:
    transport = ScriptedDecisionTransport()
    with pytest.raises(TransportError, match="没有为"):
        transport.complete(_request())


def test_scripted_transport_records_prompts_and_timeouts() -> None:
    transport = ScriptedDecisionTransport(
        responses={("pgvector", "deployment_complexity"): ["ok"]}
    )
    transport.complete(_request())
    assert transport.timeouts == [3.5]
    assert transport.prompts == ["OUTPUT (single JSON object):"]


# ================= CompatibleApiDecisionTransport（离线断言） =================


def test_compat_api_sends_documented_post_request(mock_transport) -> None:
    mock_transport.default = _ok_response(_chat_payload('{"ok": true}'))
    transport = CompatibleApiDecisionTransport(
        endpoint=ENDPOINT, model="test-model", transport=mock_transport
    )
    text = transport.complete(_request())
    assert text == '{"ok": true}'
    request = mock_transport.last_request
    assert request.method == "POST"
    assert request.url == ENDPOINT
    assert request.headers["Content-Type"] == "application/json"
    payload = json.loads(request.body.decode("utf-8"))
    assert payload["model"] == "test-model"
    assert payload["messages"][1]["content"].startswith("OUTPUT")
    # 凭据只进请求头，绝不进 body / URL
    assert "AGENTBOARD_MODEL_API_KEY" not in request.url


def test_compat_api_uses_env_key_as_bearer_without_logging(
    mock_transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBOARD_MODEL_API_KEY", "  secret-key  ")
    mock_transport.default = _ok_response(_chat_payload("ok"))
    transport = CompatibleApiDecisionTransport(
        endpoint=ENDPOINT, model="test-model", transport=mock_transport
    )
    transport.complete(_request())
    assert mock_transport.last_request.headers["Authorization"] == "Bearer secret-key"
    assert mock_transport.last_request.url == ENDPOINT
    assert b"secret-key" not in mock_transport.last_request.body
    assert transport.credential_mode() == "api_key"


def test_compat_api_anonymous_when_no_key(
    mock_transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTBOARD_MODEL_API_KEY", raising=False)
    mock_transport.default = _ok_response(_chat_payload("ok"))
    transport = CompatibleApiDecisionTransport(
        endpoint=ENDPOINT, model="test-model", transport=mock_transport
    )
    transport.complete(_request())
    assert "Authorization" not in mock_transport.last_request.headers
    assert transport.credential_mode() == "anonymous"


def test_compat_api_maps_transport_failures(
    mock_transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTBOARD_MODEL_API_KEY", raising=False)
    transport = CompatibleApiDecisionTransport(
        endpoint=ENDPOINT, model="test-model", transport=mock_transport
    )
    mock_transport.default = _ok_response(_chat_payload("ok"))
    mock_transport.raise_on[ENDPOINT] = SourceHTTPError(500, ENDPOINT)
    with pytest.raises(SourceHTTPError):
        transport.complete(_request())
    mock_transport.raise_on.clear()
    mock_transport.default = _ok_response(b"not json")
    with pytest.raises(SourceResponseError):
        transport.complete(_request())
    mock_transport.default = _ok_response(json.dumps({"choices": []}).encode("utf-8"))
    with pytest.raises(SourceResponseError):
        transport.complete(_request())


def test_compat_api_timeout_is_forwarded(mock_transport) -> None:
    mock_transport.default = _ok_response(_chat_payload("ok"))
    transport = CompatibleApiDecisionTransport(
        endpoint=ENDPOINT, model="test-model", transport=mock_transport
    )
    transport.complete(_request())
    assert mock_transport.last_request.timeout_seconds == 3.5
    transport.complete(
        DecisionCompletionRequest(
            prompt="p", candidate_id="pgvector",
            dimension_id="deployment_complexity", timeout_seconds=None,
        )
    )
    assert mock_transport.last_request.timeout_seconds == 10.0


def test_compat_api_identity_summary_distinguishes_config_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTBOARD_MODEL_API_KEY", "super-secret")
    transport = CompatibleApiDecisionTransport(
        endpoint=ENDPOINT,
        model="test-model",
        temperature=0.2,
        api_key_env_var="AGENTBOARD_MODEL_API_KEY",
        transport=object(),
    )
    summary = json.dumps(transport.identity_summary, sort_keys=True)
    assert ENDPOINT in summary
    assert "test-model" in summary
    assert "0.2" in summary
    assert "AGENTBOARD_MODEL_API_KEY" in summary
    assert "super-secret" not in summary
    assert "super-secret" not in repr(transport)


@pytest.mark.parametrize("temperature", [float("nan"), float("inf"), float("-inf")])
def test_compat_api_rejects_non_finite_temperature(temperature: float) -> None:
    with pytest.raises(TransportError, match="有限数值"):
        CompatibleApiDecisionTransport(
            endpoint=ENDPOINT, model="m", temperature=temperature, transport=object()
        )


def test_compat_api_rejects_non_https_endpoint() -> None:
    with pytest.raises(TransportError, match="https"):
        CompatibleApiDecisionTransport(
            endpoint="http://api.example.com/v1/chat/completions",
            model="m",
            transport=ScriptedDecisionTransport(),
        )


# ================= TransportRequest 的 method / body 校验 =================


def test_transport_request_rejects_body_on_get() -> None:
    with pytest.raises(InvalidSourceUrlError, match="POST"):
        TransportRequest(url="https://api.example.com/x", body=b"{}")


def test_transport_request_rejects_unknown_method() -> None:
    with pytest.raises(InvalidSourceUrlError, match="GET/POST"):
        TransportRequest(url="https://api.example.com/x", method="PUT")


def test_transport_request_default_is_get_without_body() -> None:
    request = TransportRequest(url="https://api.example.com/x")
    assert request.method == "GET" and request.body is None

"""不联网验证 opt-in smoke 所需的 token / 代理读取路径。

真实 smoke 测试由 `test_real_smoke.py` 控制是否启用；本文件只调用凭据与代理解析逻辑，
不发送请求。测试用函数级 `monkeypatch` 清理代理相关变量后设置固定值，pytest 会在
每个测试结束时恢复进入测试前的环境，包括原有代理与 `GITHUB_TOKEN`。本文件不设置
`AGENTBOARD_REAL_SMOKE=1`，因此不会启用真实联网测试。
"""

from __future__ import annotations

import os

import pytest

from agentboard.providers.github_search import build_default_provider
from agentboard.providers.http_transport import resolve_proxy

#: 模拟"用户在 shell 里显式导出"的凭据与代理。值本身无关紧要，重点是"存在且非空"。
EXPLICIT_TOKEN = "ghp_explicit_for_config_test"
EXPLICIT_PROXY = "http://explicit-proxy:8080"

pytestmark = pytest.mark.real_smoke


@pytest.fixture(autouse=True)
def _simulate_explicit_credentials_in_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数级模拟显式凭据与代理，并在测试结束后恢复原环境。"""
    for key in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("GITHUB_TOKEN", EXPLICIT_TOKEN)
    monkeypatch.setenv("HTTPS_PROXY", EXPLICIT_PROXY)


def test_smoke_provider_reads_explicit_token() -> None:
    """Provider 的凭据读取路径必须读到显式 token（而非被隔离删成 anonymous）。"""
    provider = build_default_provider()
    assert provider.credential_mode() == "token", (
        "real_smoke 测试应读到显式提供的 GITHUB_TOKEN；"
        "读到 anonymous 说明隔离误伤了带 real_smoke marker 的测试"
    )
    # 只验证"读取路径"，不验证 token 值本身（避免把凭据值写进断言）
    assert os.environ.get("GITHUB_TOKEN") == EXPLICIT_TOKEN


def test_smoke_transport_reads_explicit_proxy() -> None:
    """transport 的代理解析路径必须读到显式代理（而非被隔离删成直连）。"""
    assert os.environ.get("HTTPS_PROXY") == EXPLICIT_PROXY
    assert resolve_proxy("https://api.github.com/x") == EXPLICIT_PROXY


def test_smoke_env_is_not_wholesale_replaced() -> None:
    """隔离不得把 `os.environ` 整体替换 —— 那会让 `AGENTBOARD_REAL_SMOKE` 也消失。

    这里验证：虽然本模块带 real_smoke marker 豁免了逐条删除，
    但即便隔离生效，它也只该删名单内的键，绝不整体替换。用一个与 token/代理无关的
    哨兵证明环境仍是同一个可用映射（而非被换成空 dict）。
    """
    assert isinstance(os.environ, os._Environ)
    assert "PATH" in os.environ or "SystemRoot" in os.environ, (
        "os.environ 被整体替换成了空映射（不该发生）"
    )

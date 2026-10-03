"""Provider 层测试夹具：Mock transport（P2.1）。

**本目录下所有测试都不联网**，靠 `MockTransport` 提供可控响应。
这是 P2.1 验收项"默认 pytest 全程离线"的实现方式：把网络替换成数据，
而不是靠 `skipif` 把联网测试跳掉 —— 后者只是让测试消失，不是让代码被覆盖。

真实 smoke test 单独放在 `tests/providers/test_real_smoke.py`，且必须显式
设置环境变量才会执行（见该文件）。

`MockTransport` 的设计要点
--------------------------
1. **stateful 而不是 scriptable queue**：响应由 URL 决定，不是按"第几次调用"决定。
   调用顺序一变就挂的 mock 会把测试与实现细节绑死。
2. **记录请求**：`requests` 累积每次 `TransportRequest`，让测试能断言
   "发出去的 URL / 头是什么"，而不是只断言返回值。
3. **能模拟失败**：`raise_on` 允许指定某个 URL 抛特定异常，用于超时 / 传输失败。

这些辅助件通过 **fixture** 暴露（与 `tests/research/conftest.py` 同一做法），
而不是让测试模块 `from .conftest import ...` —— 本仓库的 `tests/` 下没有
`__init__.py`，相对 import 在 pytest 的 rootdir 模式下会失败。

代理 / 凭据环境隔离（P2.1 复核修复，第二版）
-------------------------------------------
`HttpTransport.environ=None`（默认值）表示"去读 `os.environ`"，这是**生产路径**
应有的行为。但开发机 / CI 上常常本身就设了 `HTTPS_PROXY`（或 `GITHUB_TOKEN`），
于是"本意是直连 / 匿名"的测试会真实命中代理、或带上凭据，断言
`constructed == [("api.github.com", None)]` 在工作站上挂、在干净容器里过 ——
典型的**环境耦合型脆弱测试**。

**第一版的做法（已被本次复核否决，勿重蹈）**：一个 `session` 级 `autouse` fixture
用 `setattr(os, "environ", {})` 把整个环境字典换掉，会话期内一直生效。
它"看起来"更强，实际引入了两个问题：

- **抹掉了测试自身的依赖**：`AGENTBOARD_REAL_SMOKE` / `AGENTBOARD_REAL_SNAPSHOT_DIR`
  同属 `os.environ`，被一起清掉；
- **制造了静默降级**：`test_real_smoke.py` 的 `pytestmark = skipif(...)` 在**收集期**
  求值（变量尚在 → 模块被收集），而测试体内的 `os.environ.get(...)` 在**运行期**求值
  （已被清空 → 读不到）。真实 smoke 于是会以匿名身份发出真实请求、并把快照写进
  默认目录，还会打印"通过" —— **用错了凭据和目录，却不报错**。
- **作用域外溢**：会话级替换会覆盖同一 pytest 进程里**其它目录**的测试。

**第二版（当前实现）**：换成**函数级** `autouse` fixture
`_neutralise_proxy_and_token_env`，用 `monkeypatch.delenv` **逐条**移除
`NEUTRALISED_ENV_KEYS` 里那几个「能改变被测行为」的键（六种代理写法 + token），
不碰 `os.environ` 的其余部分。于是：

- 普通 mock 测试仍然不受机器代理 / 凭据影响（达到同样目的）；
- `AGENTBOARD_REAL_SMOKE` / `AGENTBOARD_REAL_SNAPSHOT_DIR` **完好保留**，
  真实 smoke 恢复"读用户显式指定的凭据与目录"的能力；
- 还原由 `monkeypatch` 保证，外部 shell 环境自始至终未被修改；
- 隔离作用域**限定在被测的那几条键上**，不外溢到其它测试。

为什么仍算"加强"而不是放宽
--------------------------
1. **不存在变绿捷径**：名单内的键在每条测试里都被钉成"不存在"，
   无法靠"机器上恰好没设"或"跳过测试"蒙过去。
2. **想测代理 / token 的测试必须自己声明前提**：环境里已无可捡之物，
   于是 `test_transport_reads_proxy_from_injected_environ` 这类测试
   从"碰巧在配了代理的机器上有效"变成"在所有机器上都真的验证了"。
3. **生产回落路径未被改掉**：`resolve_proxy` 仍在 `environ is None` 时读 `os.environ`；
   该行为由 `test_default_transport_resolves_proxy_from_process_environ`
   用 `monkeypatch.setenv` 显式验证（函数级 `setenv` 会盖过 `delenv`，
   两者作用于同一个 `os.environ` 对象，所以测的确实是生产路径）。
4. **不再有能力被静默降级的隐藏状态**：需要干净环境的测试显式用 `environ={}` 或
   `isolated_environ`；需要真实环境的测试显式读 `os.environ`，
   两者都在被测函数体内可见，不再依赖某个会话级副作用。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from agentboard.providers.interface import TransportRequest, TransportResponse

#: 测试用的固定采集日期（不读时钟 —— 与生产代码同一条纪律）。
CAPTURED_AT = "2026-09-23"

#: 测试用的固定来源主机。
API_URL = "https://api.github.com/search/repositories"


def repo_item(
    full_name: str,
    *,
    description: str | None = None,
    stars: int = 100,
    html_url: str | None = None,
) -> dict[str, object]:
    """检索结果条目的最小可用形态（`full_name` + `html_url` 是必需字段）。"""
    item: dict[str, object] = {
        "full_name": full_name,
        "html_url": html_url or f"https://github.com/{full_name}",
        "stargazers_count": stars,
        "license": {"spdx_id": "MIT"},
    }
    if description is not None:
        item["description"] = description
    return item


def search_body(items: Sequence[Mapping[str, object]]) -> bytes:
    payload = {"total_count": len(items), "incomplete_results": False, "items": list(items)}
    return json.dumps(payload).encode("utf-8")


def ok_response(
    items: Sequence[Mapping[str, object]] | None = None,
    *,
    url: str = API_URL,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> TransportResponse:
    """构造一个 200 响应。"""
    return TransportResponse(
        url=url,
        status=200,
        headers=dict(headers or {}),
        body=body if body is not None else search_body(items or ()),
    )


def redirect_response(location: str, *, url: str = API_URL, status: int = 302) -> TransportResponse:
    """构造一个重定向响应（`location` 是下一跳的绝对 URL）。"""
    return TransportResponse(url=url, status=status, headers={"location": location}, body=b"")


@dataclass
class MockTransport:
    """离线 transport 实现。与 `Transport` 协议一致。"""

    #: URL 精确匹配 → 响应。未命中时用 `default`。
    responses: dict[str, TransportResponse] = field(default_factory=dict)
    default: TransportResponse | None = None
    #: URL 精确匹配 → 抛出的异常（优先于 responses）。
    raise_on: dict[str, BaseException] = field(default_factory=dict)
    #: 每次调用都记录，供断言请求内容。
    requests: list[TransportRequest] = field(default_factory=list)
    #: 可选的动态响应钩子：`(request, call_index) -> response | None`。
    on_send: Callable[[TransportRequest, int], TransportResponse | None] | None = None

    def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if request.url in self.raise_on:
            raise self.raise_on[request.url]
        if self.on_send is not None:
            produced = self.on_send(request, len(self.requests))
            if produced is not None:
                return produced
        if request.url in self.responses:
            return self.responses[request.url]
        if self.default is not None:
            return self.default
        raise AssertionError(f"MockTransport 未配置该 URL 的响应：{request.url}")

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> TransportRequest:
        assert self.requests, "MockTransport 尚未收到任何请求"
        return self.requests[-1]


# ---- fixture 出口（测试模块通过参数注入拿到这些，不直接 import） ----

#: 会影响被测行为、因而必须在普通测试中被移除的环境变量。
#: 只移除这些，而不是整个 `os.environ` —— 见 `_neutralise_proxy_and_token_env` 的说明。
NEUTRALISED_ENV_KEYS: tuple[str, ...] = (
    # 代理：会让默认构造的 transport 改走 CONNECT 隧道
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    # 凭据：会让 Provider 从匿名切换到带 token（限额档位不同）
    "GITHUB_TOKEN",
)


#: 标记"本测试需要真实环境变量（GITHUB_TOKEN / 代理）"的 pytest marker。
#: 带此 marker 的测试**跳过** `_neutralise_proxy_and_token_env` 的隔离。
#: 目前只有 opt-in 的 `test_real_smoke.py` 需要（它要读用户显式提供的凭据与代理）。
REAL_SMOKE_MARKER = "real_smoke"


@pytest.fixture(autouse=True)
def _neutralise_proxy_and_token_env(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """逐条移除会影响被测行为的环境变量（**函数级**，逐个测试生效、跑完还原）。

    设计要点（这一段是本目录环境隔离的全部依据，改动前请先读）
    ----------------------------------------------------------
    1. **只 `delenv` 名单内的键，不替换整个 `os.environ`。**
       早期实现用 `setattr(os, "environ", {})` 做会话级全局替换，那会连带抹掉
       `AGENTBOARD_REAL_SMOKE` / `AGENTBOARD_REAL_SNAPSHOT_DIR` 等**测试自身依赖**的变量，
       且抹除范围随会话扩散到同进程的其它目录。更糟的是它造成了一个**静默降级**：
       `test_real_smoke.py` 的 `pytestmark = skipif(...)` 在**收集期**求值（那时变量还在，
       于是模块被收集），而测试体内的 `os.environ.get(...)` 在**运行期**求值（那时已被抹掉）——
       结果真实 smoke 会"通过收集、以匿名身份发出真实请求、并把快照写进默认目录"，
       看起来像跑成功了，实际用的不是用户指定的凭据与目录。
       逐条 `delenv` 不会碰这些变量，那个降级路径随之消失。
    2. **用 `monkeypatch.delenv`，不用 `os.environ.pop`。**
       `monkeypatch` 在测试结束自动还原，外部 shell 环境从头到尾未被修改
       （满足"不允许通过清空外部环境变量让测试变绿"）。
    3. **函数级而不是会话级。**
       需要看到真实进程环境的测试（`test_default_transport_resolves_proxy_from_process_environ`
       等）只需在自己的函数体内 `monkeypatch.setenv` 设回来；会话级会让"恢复"语义变复杂。
    4. **这不是"放宽断言"。** 名单里每个键都是**能被生产代码读到并改变其行为**的输入；
       把它们钉成"不存在"之后，所有涉及代理/token 的断言都必须在测试里**显式声明**前提，
       比"碰巧机器上没设"更强，不是更弱。
    5. **真实 smoke 测试豁免隔离（本次复核第三版新增）。**
       `test_real_smoke.py` 是 opt-in 的真实联网测试，它必须读到用户显式提供的
       `GITHUB_TOKEN` 与代理。隔离 fixture 若对它也 `delenv`，就会把真实 smoke
       降级成匿名 + 直连。所以：带 `real_smoke` marker 的测试**跳过**本隔离，
       由它自己在需要时读取真实的 `os.environ`。

    需要"在干净环境里设变量"的测试，直接用 `isolated_environ` fixture。
    """
    if request.node.get_closest_marker(REAL_SMOKE_MARKER):
        return
    for key in NEUTRALISED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def neutralised_env_keys() -> tuple[str, ...]:
    """被环境隔离 fixture 移除的键名单，供自检测试注入（本仓库不搞相对 import）。"""
    return NEUTRALISED_ENV_KEYS


@pytest.fixture
def no_proxy_env() -> Mapping[str, str]:
    """显式声明"无代理"的 environ，供需要默认走直连的 transport 注入。"""
    return {}


@pytest.fixture
def proxy_env() -> Mapping[str, str]:
    """一个可控的代理 environ，供代理路径测试注入（不依赖机器真实设置）。"""
    return {"HTTPS_PROXY": "http://127.0.0.1:7897"}


@pytest.fixture
def isolated_environ(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """可写的干净 environ：测试可往里塞变量，跑完自动还原。

    它把 `os.environ` 整个换成空 dict —— 这是**测试自己要求**的强隔离，
    作用域是本测试（函数级），不会外溢到其它测试或其它目录。
    """
    blank: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", blank)
    yield blank


@pytest.fixture
def mock_transport() -> MockTransport:
    return MockTransport()


@pytest.fixture
def captured_at() -> str:
    return CAPTURED_AT


@pytest.fixture
def api_url() -> str:
    return API_URL


@pytest.fixture
def make_repo_item() -> Callable[..., dict[str, object]]:
    return repo_item


@pytest.fixture
def make_ok_response() -> Callable[..., TransportResponse]:
    return ok_response


@pytest.fixture
def make_redirect_response() -> Callable[..., TransportResponse]:
    return redirect_response


@pytest.fixture
def provider_factory() -> Callable[..., Any]:
    """构造 `GitHubRepositorySearchProvider` 的工厂，避免每个测试重复 import。"""
    from agentboard.providers.github_search import GitHubRepositorySearchProvider

    def build(transport: MockTransport, **overrides: object) -> Any:
        return GitHubRepositorySearchProvider(transport=transport, **overrides)  # type: ignore[arg-type]

    return build

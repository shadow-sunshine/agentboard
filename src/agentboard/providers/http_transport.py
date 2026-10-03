"""真实 HTTP 传输（P2.1）——**整个仓库唯一** import 网络库的文件。

为什么这个文件存在
------------------
`tests/test_static_guards.py::FORBIDDEN_IMPORT_ROOTS` 禁止 `src/agentboard/**` 出现
`http` / `urllib` / `socket` / `requests` 等网络库。P2.1 的需求是"网络访问只允许出现在
Provider 适配层"，所以守卫从"全仓禁用"收紧为"**除本文件外的全仓禁用**"：

    NETWORK_IMPORT_ALLOWLIST = {"src/agentboard/providers/http_transport.py": frozenset({"http", "ssl"})}

豁免不是白名单式的一次性放行，而是与**实际用法绑定**的：
`test_network_allowlist_matches_actual_usage` 会断言本文件真实 import 的网络库
恰好等于豁免集合。多引一个（比如顺手引 `socket`）会失败，少引一个（豁免腐化）也会失败。

只用标准库
----------
`dependencies = []` 是硬约束，所以用 `http.client` + `ssl` 而不是 `requests`。
它还会对畸形 URL 直接抛 `InvalidURL`，正好可用于"非法 URL 防护"。

**代理支持是必需的，不是可选项**
--------------------------------
`http.client` 与 `urllib.request` 有一个关键差异：后者会自动读取系统 / 注册表 / 环境变量
里的代理设置，前者不会。在必须走代理才能出网的机器上，"只有 `urllib` 能通、
`HTTPSConnection` 直连超时"是典型症状。

实测（开发机，2026-09-23）：`urllib.request` 能取到 `https://api.github.com/rate_limit`，
而裸 `HTTPSConnection` 报 `WinError 10060 连接尝试失败` —— 根因就是代理被忽略了。
所以本模块显式解析代理并走 CONNECT 隧道，把"能不能出网"从环境巧合变成显式行为。

⚠️ `set_tunnel` 的参数极易搞反，务必记住语义是"**CONNECT 到** 谁"：

    HTTPSConnection(代理主机, 代理端口)      # TCP 连到这里
    connection.set_tunnel(目标主机, 443)     # 在隧道里 CONNECT 到这里

反过来写不会报"参数错"，而是表现成 `RemoteDisconnected` 或超时 ——
看起来像网络故障，实际是代码把代理当成了目标。`test_transport_sets_tunnel_*`
就是为这条钉的护栏。

代理来源优先级（与 `urllib` 的常见约定一致，但不 import `urllib`）：

1. 显式传入的 `proxy_url`；
2. `HTTPS_PROXY` / `https_proxy`；
3. `HTTP_PROXY` / `http_proxy`；
4. 都没有 → 直连。

`NO_PROXY` / `no_proxy` 若把目标主机列进去，则强制直连。

为什么不用 `urllib.request`
---------------------------
它默认自动跟随重定向，且跟随过程不暴露每一跳。本项目要求"重定向必须显式校验"，
所以用 `http.client` **手工走跳转**：每一跳都过 `validate_redirect_target`，
跳转次数与落点都写进 `redirect_chain`。这样做还有一个好处 ——
跳转是循环里的显式分支，可以被测试逐跳构造与断言。
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.client import (
    HTTPConnection,
    HTTPException,
    HTTPSConnection,
    InvalidURL,
)

from .errors import (
    ContentTooLargeError,
    InvalidSourceUrlError,
    RedirectNotAllowedError,
    RetrievalTimeoutError,
    SourceHTTPError,
    TransportError,
)
from .interface import (
    TransportRequest,
    TransportResponse,
    is_https_request_url,
    url_host,
    validate_redirect_target,
)

#: 允许跟随重定向的状态码（其余 3xx 视为最终响应交给上层判断）。
REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

#: 本次采集允许访问的主机集合。默认只放行 GitHub 检索 API 与其重定向目标。
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset({"api.github.com"})

#: 单次读取块大小：分块读才能"边读边判断是否超限"。
READ_CHUNK_BYTES = 64 * 1024

USER_AGENT = "AgentBoard-Provider/0.1 (+offline-testable; stdlib-only)"

PROXY_ENV_KEYS: tuple[str, ...] = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
NO_PROXY_ENV_KEYS: tuple[str, ...] = ("NO_PROXY", "no_proxy")


def resolve_proxy(
    url: str,
    environ: Mapping[str, str] | None = None,
    explicit: str | None = None,
) -> str | None:
    """为 `url` 解析应使用的代理；应直连时返回 `None`。

    实现刻意**不调用** `urllib.request.getproxies()`（会引入 `urllib`）。
    因此这里只覆盖环境变量这一层 —— 环境变量是跨平台、可测试、且在 CI 与
    容器里最常见的配置方式。若将来需要读 Windows 注册表，应在这里加，
    而不是让调用方各写一份。
    """
    from os import environ as os_environ

    env = os_environ if environ is None else environ

    if is_no_proxy(url, env):
        return None
    if explicit:
        return explicit.strip() or None
    for key in PROXY_ENV_KEYS:
        value = env.get(key)
        if value and value.strip():
            return value.strip()
    return None


def is_no_proxy(url: str, environ: Mapping[str, str] | None = None) -> bool:
    """`NO_PROXY` 是否要求该主机直连。

    支持 `*`（全部直连）与逗号分隔的主机名 / 后缀（`example.com` 匹配
    `api.example.com`，与 curl 的常见行为一致）。
    """
    from os import environ as os_environ

    env = os_environ if environ is None else environ
    raw = ""
    for key in NO_PROXY_ENV_KEYS:
        value = env.get(key)
        if value:
            raw = value
            break
    if not raw:
        return False
    host = url_host(url)
    if not host:
        return False
    for entry in raw.split(","):
        entry = entry.strip().lstrip(".").lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if host == entry or host.endswith(f".{entry}"):
            return True
    return False


def parse_proxy_url(proxy_url: str) -> tuple[str, int]:
    """把 `http://host:port` 解析成 `(host, port)`。

    只支持 `http://` 代理（CONNECT 隧道场景下这是最常见形态，
    也是 `set_tunnel` 能直接处理的形式）。解析用字符串切分而非 `urllib.parse`。
    """
    if not isinstance(proxy_url, str) or "://" not in proxy_url:
        raise InvalidSourceUrlError(f"代理地址必须形如 http://host:port，实际 {proxy_url!r}")
    scheme, rest = proxy_url.split("://", 1)
    if scheme.lower() != "http":
        raise InvalidSourceUrlError(
            f"暂不支持的代理协议 {scheme!r}（只支持 http 代理），实际 {proxy_url!r}"
        )
    rest = rest.split("/", 1)[0]
    if "@" in rest:
        # 代理凭据不应出现在 URL 里：本实现不解析它们，直接拒绝，
        # 避免"以为设了凭据其实被当成了主机名"这种静默错误。
        raise InvalidSourceUrlError(f"代理地址不应包含凭据：{proxy_url!r}")
    host, _, port_text = rest.partition(":")
    if not host:
        raise InvalidSourceUrlError(f"代理地址缺少主机：{proxy_url!r}")
    port = 8080
    if port_text:
        if not port_text.isdigit():
            raise InvalidSourceUrlError(f"代理端口非法：{proxy_url!r}")
        port = int(port_text)
    return host, port


@dataclass(frozen=True, slots=True)
class HttpTransport:
    """基于 `http.client` 的同步传输实现。

    `allowed_hosts` 是本实现的**策略输入**而不是常量：`Provider` 把自己允许的主机传进来，
    测试也可以传一个受控集合。策略留在调用方，让"谁能访问什么"只有一处定义。

    `proxy_url` 默认 `None` ⇒ 交给 `resolve_proxy` 从环境变量决定。
    显式传入（含传空串表示"强制直连"）会覆盖环境变量。
    """

    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    proxy_url: str | None = None
    #: 便于测试注入环境（生产路径用 `os.environ`）。
    environ: Mapping[str, str] | None = field(default=None, repr=False, compare=False)

    def send(self, request: TransportRequest) -> TransportResponse:
        """按跳转发请求，逐跳校验重定向目标。"""
        if not is_https_request_url(request.url):
            raise InvalidSourceUrlError(f"不是合法的 https URL：{request.url!r}")
        if url_host(request.url) not in self.allowed_hosts:
            raise InvalidSourceUrlError(
                f"主机 {url_host(request.url)!r} 不在白名单 {sorted(self.allowed_hosts)} 内"
            )

        chain: list[str] = [request.url]
        current = request.url
        remaining = request.max_redirects
        while True:
            response = self._send_once(current, request)
            if response.status not in REDIRECT_STATUSES:
                return TransportResponse(
                    url=response.url,
                    status=response.status,
                    headers=response.headers,
                    body=response.body,
                    redirect_chain=tuple(chain),
                )
            location = _header(response.headers, "location")
            if not location:
                raise TransportError(f"{current} 返回 {response.status} 但缺少 Location 头")
            if remaining <= 0:
                raise RedirectNotAllowedError(
                    f"重定向次数超过上限 {request.max_redirects}（链条 {chain}）"
                )
            target = location
            if not is_https_request_url(target):
                raise RedirectNotAllowedError(
                    f"重定向目标是相对路径或非 https，拒绝：{location!r}（来源 {current}）"
                )
            # 逐跳校验：主机白名单 / https / 不含凭据。
            validate_redirect_target(current, target, self.allowed_hosts)
            remaining -= 1
            chain.append(target)
            current = target

    def _send_once(self, url: str, request: TransportRequest) -> TransportResponse:
        """发一跳请求（不跟随重定向）。"""
        host = url_host(url)
        path = url.split(host, 1)[1] or "/"
        proxy = resolve_proxy(url, self.environ, self.proxy_url)
        connection: HTTPSConnection | HTTPConnection
        try:
            if proxy:
                # **参数顺序极易搞反，这里写清楚**：
                # `set_tunnel(a, b)` 的语义是"CONNECT 到 a:b"，而 socket 本身连到
                # `HTTPSConnection(...)` 构造时给的主机。所以：
                #   - 构造参数 = 代理地址（真正要建立 TCP 连接的地方）
                #   - set_tunnel 参数 = 原始目标主机（要在隧道里 CONNECT 的对象）
                # 反过来写会变成"连到目标主机、再让目标主机 CONNECT 代理"，
                # 表现为 RemoteDisconnected / 超时，而且看起来像网络问题。
                proxy_host, proxy_port = parse_proxy_url(proxy)
                connection = HTTPSConnection(
                    proxy_host,
                    proxy_port,
                    timeout=request.timeout_seconds,
                    context=ssl.create_default_context(),
                )
                connection.set_tunnel(host, 443, headers={"Proxy-Connection": "keep-alive"})
            else:
                connection = HTTPSConnection(
                    host,
                    timeout=request.timeout_seconds,
                    context=ssl.create_default_context(),
                )
            headers = {"User-Agent": USER_AGENT, **dict(request.headers)}
            # P4：method/body 由 TransportRequest 携带（默认 GET/None，与 P2.1 兼容）。
            # body 仅在显式提供时传给 http.client——保持 GET 路径的调用形态不变。
            if request.body is not None:
                connection.request(request.method, path, body=request.body, headers=headers)
            else:
                connection.request(request.method, path, headers=headers)
            raw = connection.getresponse()
        except TimeoutError as exc:
            # 注意：Python 3.10+ `socket.timeout` 就是 `TimeoutError` 的别名，
            # 所以这里只需捕获内建类型（引 `socket` 反而会被静态守卫判为多余网络依赖）。
            raise RetrievalTimeoutError(f"请求超时（{request.timeout_seconds}s）：{url}") from exc
        except InvalidURL as exc:
            raise InvalidSourceUrlError(f"非法 URL：{url}（{exc}）") from exc
        except (ssl.SSLError, HTTPException, OSError) as exc:
            raise TransportError(f"传输失败：{url}（{type(exc).__name__}: {exc}）") from exc

        try:
            body = _read_capped(raw, request.max_response_bytes, url)
        finally:
            raw.close()
            connection.close()

        if not 200 <= raw.status < 300 and raw.status not in REDIRECT_STATUSES:
            raise SourceHTTPError(raw.status, url)

        return TransportResponse(
            url=url,
            status=raw.status,
            headers={key.lower(): value for key, value in raw.getheaders()},
            body=body,
        )


def _read_capped(response: object, limit: int, url: str) -> bytes:
    """分块读取响应体，超过 `limit` 立即抛 `ContentTooLargeError`。

    "边读边判断"与"读完再判断"的区别是实质性的：后者在拒绝之前已经把整个响应
    （可能是几百 MB）放进内存了，限额就失去意义。
    """
    chunks: list[bytes] = []
    total = 0
    read = response.read  # type: ignore[attr-defined]
    while True:
        chunk = read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ContentTooLargeError(
                f"响应体超过上限 {limit} 字节，已在读取途中中止（{url}）"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _header(headers: Mapping[str, str], name: str) -> str:
    """大小写不敏感地取一个响应头。"""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""

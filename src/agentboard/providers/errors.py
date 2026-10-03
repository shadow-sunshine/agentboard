"""Provider 层异常族（P2.1）。

与 `research/errors.py` 的分工：那一族描述"本地资料/领域模型哪里不合法"，
这一族描述"**从网络取资料这件事**哪里出了问题"。两者刻意不互相继承 ——
领域层捕获 `AgentBoardResearchError` 时不应该顺手吞掉网络错误，
反之亦然。

全部继承 `AgentBoardProviderError`，且**基本不继承 `ValueError`**：
这一族的异常几乎都来自外部环境（超时、4xx/5xx、畸形响应、超长响应），
不是"调用方传了个非法值"。若继承 `ValueError`，调用方一个 `except ValueError`
就会把网络故障当成参数校验错误吞掉。

唯一例外是 `InvalidSourceUrlError`：它确实由本地参数（URL）触发，
所以继承 `ValueError`，与核心契约层 / 领域层的值校验体验保持一致。
"""

from __future__ import annotations


class ProviderError(Exception):
    """Provider 层异常基类。Provider 层抛出的所有异常都应是它的子类。"""


# ---- 本地参数 / 策略守卫 ----


class InvalidSourceUrlError(ProviderError, ValueError):
    """URL 非法（畸形 / 非 https / 主机不在白名单 / 含凭据或片段）。

    由**调用方传入的参数**触发，所以同时是 `ValueError`。
    """


class RedirectNotAllowedError(ProviderError):
    """重定向到了未被允许的目标（跨主机 / 降级为 http / 跳转到未知主机）。

    不跟随自动重定向、而是显式校验每一跳，是为了防"通过 302 把请求引到内网地址"。
    """


class ContentTooLargeError(ProviderError):
    """响应体超过 `max_response_bytes`（在读取过程中即中止，不是读完再判断）。"""


# ---- 传输 / 来源侧问题 ----


class TransportError(ProviderError):
    """传输层失败（连接失败、TLS 失败、对端断开等）。"""


class RetrievalTimeoutError(ProviderError):
    """请求超时（连接超时或读超时）。"""


class SourceHTTPError(ProviderError):
    """来源返回了非 2xx 状态码。"""

    def __init__(self, status: int, url: str, detail: str = "") -> None:
        self.status = int(status)
        self.url = url
        super().__init__(detail or f"{url} 返回 HTTP {self.status}")


class SourceResponseError(ProviderError):
    """响应体不是预期的结构（非 JSON / 缺少必需字段 / 字段类型不符）。"""

"""决策 transport（P4 受控生成决策）：把"决策提示词"变成"模型原始输出"。

与检索侧 `Transport` 的分工
---------------------------
检索侧解决"从来源取原文"；本模块解决"把一份**确定性构建**的决策提示词交给
模型/兼容 API，并取回**原始文本输出**"。协议校验（JSON 闭集、量表、locator）
**不在这里做**——那属于 `runtime.decider_generated`（fail-closed 的唯一实现点），
transport 只负责忠实地传输字节与映射传输层异常。

依赖方向：providers 不 import runtime（既有纪律）；因此本模块只抛
providers 异常族的传输层错误，协议层错误由 runtime 侧定义与判定。

两条实现
--------

- **`ScriptedDecisionTransport`**（离线默认）：按脚本返回固定输出 / 抛错，
  零网络、零时钟、零凭据——测试与评测的默认替身；
- **`CompatibleApiDecisionTransport`**（显式 opt-in）：OpenAI 兼容
  chat/completions 端点。API key 只在调用时从环境变量读取，
  **不落盘、不进日志、不进快照**；默认测试从不构造真实网络调用。

红线：本模块的输出是**未校验的模型文本**——它不因"来自模型"而获得任何可信身份；
能否成为决策由 runtime 的协议校验与既有证据边界决定。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import SourceResponseError, TransportError
from .interface import Transport, TransportRequest, is_https_request_url

SCRIPTED_DECISION_TRANSPORT_ID = "scripted.decision.v1"
COMPAT_API_DECISION_TRANSPORT_ID = "compat-api.chat.v1"

#: 兼容 API 的默认环境变量名。key 只在 `complete()` 调用时读取，
#: 不落盘、不进日志、不进快照；变量名可由构造参数覆盖（便于测试隔离）。
DEFAULT_API_KEY_ENV_VAR = "AGENTBOARD_MODEL_API_KEY"


@dataclass(frozen=True, slots=True)
class DecisionCompletionRequest:
    """一次决策补全请求（字段闭集；`candidate_id` / `dimension_id` 供脚本 transport
    定位脚本与审计，真实 transport 不解析其语义）。"""

    prompt: str
    candidate_id: str
    dimension_id: str
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise TransportError("DecisionCompletionRequest.prompt 必须是非空字符串")
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise TransportError("DecisionCompletionRequest.candidate_id 必须是非空字符串")
        if not isinstance(self.dimension_id, str) or not self.dimension_id.strip():
            raise TransportError("DecisionCompletionRequest.dimension_id 必须是非空字符串")


@runtime_checkable
class DecisionTransport(Protocol):
    """决策 transport 协议：提示词 → 原始文本输出。

    实现必须是**无状态**的（同样的输入 ⇒ 同样的输出；记录型状态只用于审计）。
    返回的文本**未经过任何协议校验**——校验是 runtime 的唯一实现点。
    """

    @property
    def transport_id(self) -> str:
        """稳定标识（进入决策器身份摘要 ⇒ run_id；换 transport ⇒ 换运行身份）。"""
        ...

    @property
    def identity_summary(self) -> Mapping[str, object]:
        """不含凭据的稳定配置摘要（进入决策器 identity）。"""
        ...

    def complete(self, request: DecisionCompletionRequest) -> str:
        """执行一次补全。传输层失败抛 providers 异常族（可重试闭集由 runtime 定义）。"""
        ...


@dataclass(frozen=True, slots=True)
class ScriptedDecisionTransport:
    """确定性离线决策 transport（P4 测试与评测的默认替身）。

    - `responses`：`(candidate_id, dimension_id) → 按调用次序依次返回的原始输出`；
      队列耗尽后**重复最后一项**（"持续违例"场景因此可表达）；
    - `failures`：`(candidate_id, dimension_id) → 按调用次序依次抛出的异常`
      （用完后开始正常返回）——重试语义的可复现形态；
    - 未脚本化的键 ⇒ 抛 `TransportError`（显式失败，不编造输出）；
    - `calls` / `timeouts` / `prompts` 只增不减，供测试 / 评测断言
      （记录是审计，不是随机源）。
    """

    responses: Mapping[tuple[str, str], Sequence[str]] = field(default_factory=dict)
    failures: Mapping[tuple[str, str], Sequence[Exception]] = field(default_factory=dict)
    transport_id: str = SCRIPTED_DECISION_TRANSPORT_ID
    calls: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def identity_summary(self) -> Mapping[str, object]:
        """脚本 transport 的身份只包含显式 transport 标识，不包含运行期调用状态。"""
        return {"transport_id": self.transport_id, "kind": "scripted"}
    timeouts: list[float | None] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def complete(self, request: DecisionCompletionRequest) -> str:
        key = (request.candidate_id, request.dimension_id)
        self.calls[key] = self.calls.get(key, 0) + 1
        self.timeouts.append(request.timeout_seconds)
        self.prompts.append(request.prompt)
        failures_for_key = self.failures.get(key, ())
        if self.calls[key] <= len(failures_for_key):
            raise failures_for_key[self.calls[key] - 1]
        sequence = self.responses.get(key)
        if not sequence:
            raise TransportError(
                f"ScriptedDecisionTransport 没有为 {key} 脚本化任何输出（显式失败，不编造）"
            )
        index = min(self.calls[key] - 1, len(sequence) - 1)
        return sequence[index]

    def call_count(self, candidate_id: str, dimension_id: str) -> int:
        return self.calls.get((candidate_id, dimension_id), 0)


@dataclass(frozen=True, slots=True)
class CompatibleApiDecisionTransport:
    """OpenAI 兼容 chat/completions 决策 transport（**显式 opt-in** 的真实路径）。

    - `transport` 注入既有 `HttpTransport`（POST 支持为 P4 新增；默认值兼容 GET）；
    - `allowed_hosts` 语义由底层 transport 承担——调用方必须把端点主机显式放进白名单；
    - API key **只在 `complete()` 调用时**从 `api_key_env_var` 读取：
      不落盘、不进日志、不进快照、不出现在任何序列化输出里；
    - 本类不做任何协议校验——返回的是未校验的模型文本（见模块 docstring）。
    """

    endpoint: str
    model: str
    transport: Transport
    api_key_env_var: str = DEFAULT_API_KEY_ENV_VAR
    temperature: float = 0.0
    transport_id: str = COMPAT_API_DECISION_TRANSPORT_ID

    def __post_init__(self) -> None:
        if not is_https_request_url(self.endpoint):
            raise TransportError(
                f"CompatibleApiDecisionTransport.endpoint 必须是 https URL，实际 {self.endpoint!r}"
            )
        if not isinstance(self.model, str) or not self.model.strip():
            raise TransportError("model 必须是非空字符串")
        if not isinstance(self.api_key_env_var, str) or not self.api_key_env_var.strip():
            raise TransportError("api_key_env_var 必须是非空字符串")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(float(self.temperature))
        ):
            raise TransportError(
                f"temperature 必须是有限数值，实际 {self.temperature!r}"
            )

    @property
    def identity_summary(self) -> Mapping[str, object]:
        """返回稳定配置摘要；绝不读取或记录 API key 值。"""
        underlying = self.transport
        allowed_hosts = getattr(underlying, "allowed_hosts", None)
        if isinstance(allowed_hosts, (set, frozenset, list, tuple)):
            allowed_hosts_summary: object = sorted(str(host) for host in allowed_hosts)
        else:
            allowed_hosts_summary = None
        return {
            "transport_id": self.transport_id,
            "kind": "compatible_api",
            "endpoint": self.endpoint,
            "model": self.model,
            "temperature": float(self.temperature),
            "api_key_env_var": self.api_key_env_var,
            "underlying_transport": {
                "type": f"{type(underlying).__module__}.{type(underlying).__qualname__}",
                "allowed_hosts": allowed_hosts_summary,
                "proxy_mode": (
                    "explicit"
                    if getattr(underlying, "proxy_url", None) is not None
                    else "environment"
                ),
            },
        }

    def complete(self, request: DecisionCompletionRequest) -> str:
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(self.api_key_env_var, "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a JSON decision API. "
                        "Reply with exactly one JSON object following the "
                        "protocol stated in the user message. No extra text."
                    ),
                },
                {"role": "user", "content": request.prompt},
            ],
        }
        http_request = TransportRequest(
            url=self.endpoint,
            headers=headers,
            timeout_seconds=(
                request.timeout_seconds if request.timeout_seconds is not None else 10.0
            ),
            method="POST",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        response = self.transport.send(http_request)
        return self._extract_text(response.body, response.url)

    def _extract_text(self, body: bytes, url: str) -> str:
        """从 chat/completions 响应里取 `choices[0].message.content`（结构不符显式失败）。"""
        if not body:
            raise SourceResponseError(f"{url} 返回空响应体")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceResponseError(f"{url} 的响应体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            raise SourceResponseError(f"{url} 的响应缺少 choices 数组")
        choices = payload["choices"]
        if not choices or not isinstance(choices[0], dict):
            raise SourceResponseError(f"{url} 的 choices 为空或结构不符")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise SourceResponseError(f"{url} 的响应缺少 message.content 字符串")
        return message["content"]

    def credential_mode(self, environ: Mapping[str, str] | None = None) -> str:
        """当前是"带 key"还是"匿名"，**只返回标签、不返回凭据**。"""
        env = os.environ if environ is None else environ
        return "api_key" if env.get(self.api_key_env_var, "").strip() else "anonymous"


__all__ = [
    "COMPAT_API_DECISION_TRANSPORT_ID",
    "DEFAULT_API_KEY_ENV_VAR",
    "SCRIPTED_DECISION_TRANSPORT_ID",
    "CompatibleApiDecisionTransport",
    "DecisionCompletionRequest",
    "DecisionTransport",
    "ScriptedDecisionTransport",
]

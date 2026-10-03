"""预算与安全边界（P3 live-research MVP）。

两条设计决定
------------

1. **计数式预算，不读时钟**：步数 / Provider 请求数 / 内容字符数 / 重试次数全部是
   整数计数。同一输入重放必然得到同一结论 —— 与项目"不读时钟"的确定性纪律一致。
   "超时"由传输层承担（`TransportRequest.timeout_seconds` / Provider 构造参数），
   运行时只负责把它配置进去。

2. **预算耗尽是定义好的结局，不是异常逃逸**：`BudgetLedger` 的 consume 系列方法
   在超限时抛 `BudgetExhaustedError`，runner 捕获后把剩余工作标记为
   `skipped`（原因写入步骤状态，可观测），用已取得的材料继续走降级路径。
   任何一层都**不允许**静默吞掉这个异常继续发请求。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import BudgetExhaustedError, PlanValueError


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    """一次研究运行的资源上限（全部为硬上限，超出即触发降级路径）。"""

    #: 计划步数上限（含 gap 补检步）。超过上限的步骤标记 `skipped`。
    max_steps: int
    #: Provider 检索请求总数上限（重试同样计入）。
    max_provider_requests: int
    #: 取回内容的字符总数上限（超过后当前步骤标记降级，不再写快照）。
    max_total_content_chars: int
    #: 每步的重试上限（首次调用之外允许的重试次数；0 = 不重试）。
    max_retries_per_step: int = 0
    #: 单次请求超时（秒）。由 Provider / 传输层消费，运行时只透传，不读时钟。
    request_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_provider_requests", "max_total_content_chars",
                     "max_retries_per_step"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PlanValueError(f"ResearchBudget.{name} 必须是非负整数，实际 {value!r}")
        if self.max_steps == 0:
            raise PlanValueError("ResearchBudget.max_steps 必须至少为 1")
        if isinstance(self.request_timeout_seconds, bool) or not isinstance(
            self.request_timeout_seconds, (int, float)
        ) or self.request_timeout_seconds <= 0:
            raise PlanValueError(
                f"ResearchBudget.request_timeout_seconds 必须为正数，"
                f"实际 {self.request_timeout_seconds!r}"
            )


class BudgetLedger:
    """预算记账器：可变计数器 + 冻结的上限。

    `consume_*` 系列在**即将超出**上限时抛 `BudgetExhaustedError`
    （调用方据此走降级路径）；计数只增不减，保证"用了多少"可审计。
    """

    def __init__(self, budget: ResearchBudget) -> None:
        self._budget = budget
        self.steps_started = 0
        self.provider_requests = 0
        self.content_chars = 0
        self.retries = 0

    @property
    def budget(self) -> ResearchBudget:
        return self._budget

    def start_step(self) -> None:
        """开始一个计划步。超出 `max_steps` ⇒ 抛预算异常。"""
        if self.steps_started >= self._budget.max_steps:
            raise BudgetExhaustedError(
                f"步数预算耗尽：已开始 {self.steps_started} 步，上限 {self._budget.max_steps}"
            )
        self.steps_started += 1

    def consume_request(self) -> None:
        """发起一次 Provider 请求前调用。超出 `max_provider_requests` ⇒ 抛预算异常。"""
        if self.provider_requests >= self._budget.max_provider_requests:
            raise BudgetExhaustedError(
                f"请求预算耗尽：已发 {self.provider_requests} 次，"
                f"上限 {self._budget.max_provider_requests}"
            )
        self.provider_requests += 1

    def add_retry(self) -> None:
        self.retries += 1

    def add_content_chars(self, count: int) -> None:
        """累计取回内容字符数。超出 `max_total_content_chars` ⇒ 抛预算异常。"""
        if count < 0:
            raise PlanValueError(f"内容字符数不能为负，实际 {count}")
        if self.content_chars + count > self._budget.max_total_content_chars:
            raise BudgetExhaustedError(
                f"内容预算耗尽：已累计 {self.content_chars} 字符，"
                f"本次新增 {count}，上限 {self._budget.max_total_content_chars}"
            )
        self.content_chars += count

    def can_start_step(self) -> bool:
        return self.steps_started < self._budget.max_steps

    def can_send_request(self) -> bool:
        return self.provider_requests < self._budget.max_provider_requests

    def usage(self) -> dict[str, int]:
        """当前用量快照（可观测；写进运行记录与评测指标）。"""
        return {
            "steps_started": self.steps_started,
            "provider_requests": self.provider_requests,
            "content_chars": self.content_chars,
            "retries": self.retries,
        }

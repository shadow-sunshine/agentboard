"""运行时异常族（P3 live-research MVP）。

与既有两族异常的分工：

- `research.errors`：本地资料 / 领域模型哪里不合法；
- `providers.errors`：从网络取资料这件事哪里出了问题；
- 本族：**研究运行时自身**哪里出了问题（预算耗尽、计划非法、证据边界违例、
  整次运行失败）。全部继承 `LiveResearchError`，与上面两族刻意不互相继承 ——
  调用方捕获本族时不应顺手吞掉网络/领域错误，反之亦然。

确定性纪律：本族的判定**全部基于计数与数据**（步数 / 请求数 / 字符数 / 结构校验），
不读时钟 —— "超时"由传输层的 `timeout_seconds` 承担（Provider 构造参数），
运行时只做计数式预算，保证同一输入重放得到同一结论。
"""

from __future__ import annotations


class LiveResearchError(Exception):
    """研究运行时异常基类。runtime 抛出的所有异常都应是它的子类。"""


class PlanError(LiveResearchError, ValueError):
    """研究计划非法（候选 / 维度标识不合法、计划为空等）。由本地参数触发，故是 ValueError。"""


class PlanValueError(PlanError):
    """运行时配置参数非法（预算字段、路径、日期形态等）。"""


class BudgetExhaustedError(LiveResearchError):
    """预算耗尽（步数 / Provider 请求数 / 内容量任一达到上限）。

    这不是错误清理信号：runner 捕获它后把剩余步骤标记为 `skipped`（原因可观测），
    用已取得的材料继续走完降级路径 —— "超预算"是**定义好的结局**，不是崩溃。
    """


class ProviderStepError(LiveResearchError):
    """步骤出现**未知**的 Provider 错误（不在可重试异常闭集内）。

    禁止静默吞错：未知错误必须打断本次运行并把原始异常链带上，
    而不是被当成"这一步没结果"处理。
    """


class EvidenceBoundaryError(LiveResearchError):
    """证据边界违例：决策产出无法映射到已核验的快照内容
    （值不在量表内、引用不存在的 section、claim 为空等）。"""


# ---- P4：受控生成决策（structured decision protocol） ----


class DecisionError(LiveResearchError):
    """受控生成决策的异常基类。"""


class DecisionTransportError(DecisionError):
    """决策 transport 层失败（网络 / HTTP / 响应结构）。

    属于**可重试**闭集：runner/决策器按上限重试；耗尽后升级为
    `DecisionGenerationError`（显式失败），绝不静默吞掉。
    """


class DecisionSchemaError(DecisionError):
    """模型输出违反结构化决策协议（非 JSON / 未知字段 / 缺字段 /
    取值不在量表内 / 悬空 locator / 跨候选串证据 / 置信度越界 / 超长输出）。

    **fail-closed 的可执行形态**：违例输出永远不可能被接受进报告；
    协议违例允许重试（模型可能自行修正格式），重试耗尽 ⇒ `DecisionGenerationError`。
    """


class DecisionGenerationError(DecisionError):
    """受控生成决策**永久失败**（重试耗尽 / 致命违例）。

    runner 捕获后转为可观测结局 `decision_failed:<Type>`，绝不静默
    fallback 到脚本决策器掩盖生成失败。
    """


class DecisionBudgetExhaustedError(DecisionError):
    """决策请求预算耗尽（`max_decision_requests` 达到上限）。

    与 `BudgetExhaustedError` 同一纪律：这是**定义好的结局**——runner 捕获后
    把剩余 (候选, 维度) 显式标记为 unsupported（可观测降级），绝不伪造决策。
    """


class RunFailedError(LiveResearchError):
    """整次运行失败（例如：所有步骤都没有取得任何可用材料，无法组装报告）。"""

"""上下文隔离（需求3）：把公共任务板裁剪成子 Agent 私有任务包。

核心不变量：WorkerTaskInput.upstream_results 只包含该卡 depends_on 中声明、
且已完成的任务的结构化结果。子 Agent 看不到完整主会话，也看不到未声明/未完成的上游。
"""

from __future__ import annotations

from .contracts import TaskCard, WorkerResult, WorkerTaskInput

DEFAULT_ACCEPTANCE: tuple[str, ...] = (
    "只基于任务包与显式上游结果回答，不假设自己知道完整主会话。",
    "把需要外部核验的信息列为缺口。",
    "输出结构化结论：事实/证据/风险/未解决问题/置信度。",
)

DEFAULT_FORBIDDEN: tuple[str, ...] = (
    "不要声称读取了完整主会话。",
    "不要读取或修改本地文件。",
    "不要运行命令，也不要联网实时查证。",
)


def build_worker_input(
    card: TaskCard,
    done_cards_by_id: dict[str, TaskCard],
    *,
    acceptance: tuple[str, ...] = DEFAULT_ACCEPTANCE,
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN,
) -> WorkerTaskInput:
    """构造私有任务包。

    done_cards_by_id: 当前已完成任务（含结构化结果）的映射。
    仅注入 card.depends_on 声明的上游；缺失或尚未产出结构化结果则报错。
    """
    missing = [dep for dep in card.depends_on if dep not in done_cards_by_id]
    if missing:
        raise RuntimeError(f"{card.task_id} 依赖未完成:{', '.join(missing)}")

    upstream: dict[str, WorkerResult] = {}
    for dep in card.depends_on:
        upstream_result = done_cards_by_id[dep].result
        if upstream_result is None:
            raise RuntimeError(f"{card.task_id} 的上游 {dep} 尚未产出结构化结果")
        upstream[dep] = upstream_result

    return WorkerTaskInput(
        task_id=card.task_id,
        title=card.title,
        assigned_agent=card.assigned_agent,
        goal=card.goal,
        context=card.context,
        upstream_results=upstream,
        acceptance=acceptance,
        forbidden=forbidden,
    )


def format_worker_prompt(worker_input: WorkerTaskInput) -> str:
    """把私有任务包渲染成可读 prompt（供真实/ACP Worker 消费）。"""
    upstream = "\n\n".join(
        f"[{task_id}]\n{result.summary}" for task_id, result in worker_input.upstream_results.items()
    )
    acceptance = "\n".join(f"- {item}" for item in worker_input.acceptance)
    forbidden = "\n".join(f"- {item}" for item in worker_input.forbidden)
    payload = worker_input.to_dict()
    prompt = (
        "你是主 Agent 调度的子 Agent。你只能看到下面这个私有任务包，"
        "看不到完整主会话，也不能读取本地文件或运行命令。\n\n"
        f"私有任务包:\n{payload}\n\n"
        f"验收口径:\n{acceptance}\n\n"
        f"执行边界:\n{forbidden}\n\n"
        "请用中文输出结构化结论：事实、证据、风险、未解决问题、置信度。"
    )
    if upstream:
        prompt += f"\n\n上游产出摘要:\n{upstream}"
    return prompt

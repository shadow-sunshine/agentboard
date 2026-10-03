"""Worker Adapter 协议与注册表（需求9）。

调度核心只认 WorkerAdapter 协议，不认任何具体实现（Mock / ACP / 真实模型）。
ACP 适配器在后续阶段实现，并以相同协议注册到 WorkerRegistry，从而与编排核心解耦。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..contracts import WorkerResult, WorkerTaskInput


@runtime_checkable
class WorkerAdapter(Protocol):
    """一个子 Agent 执行器。输入私有任务包，输出结构化结果。"""

    async def run(self, task_input: WorkerTaskInput) -> WorkerResult: ...


class WorkerRegistry:
    """按 agent 名称路由到具体 adapter。调度核心通过它取 worker，不直接构造。"""

    def __init__(self) -> None:
        self._adapters: dict[str, WorkerAdapter] = {}

    def register(self, name: str, adapter: WorkerAdapter) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Worker 名称必须是非空字符串")
        if name in self._adapters:
            raise ValueError(f"已注册 Worker 名称冲突，拒绝覆盖:{name!r}")
        self._adapters[name] = adapter

    def get(self, name: str) -> WorkerAdapter:
        if name not in self._adapters:
            raise KeyError(f"未注册 Worker:{name}")
        return self._adapters[name]

    def names(self) -> list[str]:
        return list(self._adapters)

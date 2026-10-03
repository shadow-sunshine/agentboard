"""Worker Adapter 边界（需求9）：ACP / 真实模型只是其中一种 Adapter。

调度核心只依赖 WorkerAdapter 协议与 WorkerRegistry，绝不 import acp 或具体模型 SDK。
这样 ACP 可作为一个独立 adapter 在后续阶段接入，而不污染编排核心。
"""

from .base import WorkerAdapter, WorkerRegistry
from .mock import MockWorker

__all__ = ["MockWorker", "WorkerAdapter", "WorkerRegistry"]

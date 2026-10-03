"""技术选型研究领域层（P1A）。

分层约定：本包单向依赖核心 `agentboard`（`evidence` / `contracts` / `util`），
核心**不得**反向依赖本包 —— 核心只认 `WorkerResult.structured_payload` 这个带版本号的
结构化扩展槽，不解释其业务语义。

当前阶段（P1A-1）只包含 legacy 证据迁移器；领域模型、fixture 快照与报告组装
分别在 P1A-2 / P1A-3 落地。
"""

from __future__ import annotations

__all__: list[str] = []

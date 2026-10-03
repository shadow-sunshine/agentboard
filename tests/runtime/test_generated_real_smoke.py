"""P4 显式 opt-in 的真实模型流程（默认跳过；不进默认门禁）。

与 P2.1/P3 的 real smoke 同一套机制：必须显式设置
`AGENTBOARD_REAL_MODEL_SMOKE=1` 且提供端点/模型环境变量才会执行；
默认 `pytest` 全程离线，且**不读取任何凭据**。

需要的环境变量（仅在本文件被启用时读取）：
- `AGENTBOARD_MODEL_ENDPOINT`：OpenAI 兼容 chat/completions 的 https 端点；
- `AGENTBOARD_MODEL_NAME`：模型名；
- `AGENTBOARD_MODEL_API_KEY`（可选）：有则带 Bearer，无则匿名。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.real_smoke,
    pytest.mark.skipif(
        os.environ.get("AGENTBOARD_REAL_MODEL_SMOKE") != "1"
        or not os.environ.get("AGENTBOARD_MODEL_ENDPOINT")
        or not os.environ.get("AGENTBOARD_MODEL_NAME"),
        reason=(
            "真实模型 smoke 必须显式开启（AGENTBOARD_REAL_MODEL_SMOKE=1"
            " + AGENTBOARD_MODEL_ENDPOINT + AGENTBOARD_MODEL_NAME）；默认全程离线"
        ),
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS_JSON = PROJECT_ROOT / "fixtures" / "research" / "dimensions" / "dimensions.v1.json"


def test_real_model_decision_wiring_produces_observable_run(tmp_path: Path) -> None:
    """真实模型决策接线冒烟。断言运行形态（不断言模型结论的具体内容）。"""
    from urllib.parse import urlsplit

    from agentboard.contracts import ProjectBrief
    from agentboard.providers.decision_transport import (
        CompatibleApiDecisionTransport,
    )
    from agentboard.providers.http_transport import HttpTransport
    from agentboard.research.dimensions import load_dimension_registry
    from agentboard.runtime.budget import ResearchBudget
    from agentboard.runtime.decider_generated import (
        DecisionGenConfig,
        GeneratedDimensionDecider,
    )
    from agentboard.runtime.mock_provider import ScriptedSearchProvider
    from agentboard.runtime.runner import LiveResearchConfig, LiveResearchRunner
    from tests.runtime.conftest import CANDIDATES, CAPTURED_AT, make_docs

    endpoint = os.environ["AGENTBOARD_MODEL_ENDPOINT"]
    host = urlsplit(endpoint).hostname or ""
    registry = load_dimension_registry(DIMENSIONS_JSON)
    scale_labels = {
        dimension.dimension_id: tuple(dimension.scale_labels)
        for dimension in registry.dimensions
    }
    decider = GeneratedDimensionDecider(
        transport=CompatibleApiDecisionTransport(
            endpoint=endpoint,
            model=os.environ["AGENTBOARD_MODEL_NAME"],
            transport=HttpTransport(allowed_hosts=frozenset({host})),
        ),
        config=DecisionGenConfig(max_retries=1, max_decision_requests=8),
        scale_labels=scale_labels,
    )
    # 检索侧保持离线脚本替身（本 smoke 只验证**决策**侧的真实接线）。
    documents = {
        f"{candidate_id} deployment_complexity": make_docs(
            f"{candidate_id} deployment_complexity", candidate_id, count=1
        )
        for candidate_id in CANDIDATES
    }
    provider = ScriptedSearchProvider(documents=documents, captured_at=CAPTURED_AT)
    runner = LiveResearchRunner(
        brief=ProjectBrief(
            text="live-research 真实模型决策冒烟", source="docs/research/selection_goal.md"
        ),
        candidate_ids=CANDIDATES,
        registry=registry,
        provider=provider,
        budget=ResearchBudget(
            max_steps=32,
            max_provider_requests=32,
            max_total_content_chars=400_000,
            max_retries_per_step=1,
            request_timeout_seconds=30.0,
        ),
        decider=decider,
        config=LiveResearchConfig(
            captured_at=CAPTURED_AT,
            dimensions_json=DIMENSIONS_JSON,
            snapshot_root=tmp_path / "snapshots",
            run_dir=tmp_path / "run",
            gap_recheck=False,
        ),
    )
    result = runner.run()

    # 只断言**运行形态**：决策预算被尊重、结局可观测、凭据不进任何序列化输出。
    assert result.budget_usage.get("decision_requests", 0) <= 8
    assert result.decision_identity.startswith("generated.v1:")
    assert result.outcome in {"completed", "failed"}
    if result.outcome == "completed":
        record = result.run_record_path or (tmp_path / "run" / "run_record.json")
        payload = record.read_text(encoding="utf-8")
        assert "AGENTBOARD_MODEL_API_KEY" not in payload

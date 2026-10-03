"""P3 显式 opt-in 的真实 Provider 流程（默认跳过；不进默认门禁）。

与 P2.1 的 real smoke 同一套机制：必须显式设置 `AGENTBOARD_REAL_SMOKE=1`
才会执行；默认 `pytest` 全程离线，且**不读取任何凭据**。
真实路径复用 P2.1 的 `build_default_provider()`（`HttpTransport` + GitHub 仓库检索），
预算收得很小（一次性验证接线，不做规模采集）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.real_smoke,
    pytest.mark.skipif(
        os.environ.get("AGENTBOARD_REAL_SMOKE") != "1",
        reason="真实联网 smoke 必须显式开启（AGENTBOARD_REAL_SMOKE=1）；默认全程离线",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS_JSON = PROJECT_ROOT / "fixtures" / "research" / "dimensions" / "dimensions.v1.json"


def test_real_provider_wiring_produces_observable_run(tmp_path: Path) -> None:
    """真实 Provider 流程：接线冒烟。断言运行形态（不预设网络内容的结论）。"""
    from agentboard.contracts import ProjectBrief
    from agentboard.providers import build_default_provider
    from agentboard.runtime.budget import ResearchBudget
    from agentboard.runtime.decider import ScriptedDimensionDecider
    from agentboard.runtime.runner import LiveResearchConfig, LiveResearchRunner

    runner = LiveResearchRunner(
        brief=ProjectBrief(
            text="live-research 真实接线冒烟", source="docs/research/selection_goal.md"
        ),
        candidate_ids=("pgvector", "qdrant_local"),
        registry=_load_registry(),
        provider=build_default_provider(),
        budget=ResearchBudget(
            max_steps=32,
            max_provider_requests=32,
            max_total_content_chars=400_000,
            max_retries_per_step=1,
            request_timeout_seconds=15.0,
        ),
        decider=ScriptedDimensionDecider(decisions={}),
        config=LiveResearchConfig(
            captured_at="2026-09-26",
            dimensions_json=DIMENSIONS_JSON,
            snapshot_root=tmp_path / "snapshots",
            run_dir=tmp_path / "run",
            gap_recheck=False,
        ),
    )
    result = runner.run()

    # 只断言**运行形态**（预算被尊重、状态可观测），不断言网络内容的具体结论：
    assert result.budget_usage["provider_requests"] <= 32
    assert result.provider_id == "github.repository_search.v1"
    if result.outcome == "completed":
        assert result.report is not None
        assert result.corpus_dir is not None
        index = json.loads(
            (result.corpus_dir / "INDEX.json").read_text(encoding="utf-8")
        )
        assert {entry["candidate_id"] for entry in index["candidates"]} <= {
            "pgvector", "qdrant_local",
        }
    else:
        assert result.failure_reason in {"no_snapshots", "insufficient_candidates"}


def _load_registry():
    from agentboard.research.dimensions import load_dimension_registry

    return load_dimension_registry(DIMENSIONS_JSON)

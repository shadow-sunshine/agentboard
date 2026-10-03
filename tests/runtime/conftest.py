"""runtime 测试夹具（P3）：全部离线、零时钟、零凭据。"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.contracts import ProjectBrief
from agentboard.research.dimensions import (
    DimensionRegistry,
    load_dimension_registry,
)
from agentboard.runtime.budget import ResearchBudget
from agentboard.runtime.decider import DimensionDecision, ScriptedDimensionDecider
from agentboard.runtime.mock_provider import (
    ScriptedSearchProvider,
    scripted_document,
)
from agentboard.runtime.runner import LiveResearchConfig, LiveResearchRunner

RESEARCH_FIXTURES = PROJECT_ROOT / "fixtures" / "research"
DIMENSIONS_JSON = RESEARCH_FIXTURES / "dimensions" / "dimensions.v1.json"
CAPTURED_AT = "2026-09-26"
CANDIDATES = ("pgvector", "qdrant_local")


@pytest.fixture
def dimensions_json() -> Path:
    return DIMENSIONS_JSON


@pytest.fixture
def registry() -> DimensionRegistry:
    return load_dimension_registry(DIMENSIONS_JSON)


@pytest.fixture
def brief() -> ProjectBrief:
    return ProjectBrief(
        text="live-research MVP 端到端评测",
        source="docs/research/selection_goal.md",
    )


def make_budget(**overrides: int) -> ResearchBudget:
    values: dict[str, int] = {
        "max_steps": 64,
        "max_provider_requests": 64,
        "max_total_content_chars": 1_000_000,
        "max_retries_per_step": 1,
    }
    values.update(overrides)
    return ResearchBudget(**values)


def make_docs(
    terms: str, candidate_id: str, count: int = 1, *, body_prefix: str = "官方说明"
) -> list[Any]:
    return [
        scripted_document(
            terms=terms,
            candidate_id=candidate_id,
            index=index,
            body=f"{body_prefix}：{candidate_id} 在 {terms} 上的第 {index} 份材料。",
            captured_at=CAPTURED_AT,
        )
        for index in range(count)
    ]


def make_runner(
    *,
    tmp_path: Path,
    brief: ProjectBrief,
    registry: Any,
    documents: Mapping[str, list[Any]],
    failures: Mapping[str, list[Exception]] | None = None,
    decisions: Mapping[tuple[str, str], DimensionDecision] | None = None,
    budget: ResearchBudget | None = None,
    candidate_ids: tuple[str, ...] = CANDIDATES,
    gap_recheck: bool = True,
    run_name: str = "run",
    dimensions_json: Path | None = None,
    decider: Any | None = None,
) -> tuple[LiveResearchRunner, ScriptedSearchProvider, Path]:
    provider = ScriptedSearchProvider(
        documents=documents,
        captured_at=CAPTURED_AT,
        failures=failures or {},
    )
    # decider 覆盖（P4）：显式传入的决策器优先；默认仍是脚本决策器。
    if decider is None:
        decider = ScriptedDimensionDecider(decisions=dict(decisions or {}))
    run_dir = tmp_path / run_name
    config = LiveResearchConfig(
        captured_at=CAPTURED_AT,
        dimensions_json=dimensions_json or DIMENSIONS_JSON,
        snapshot_root=tmp_path / f"{run_name}-snapshots",
        run_dir=run_dir,
        gap_recheck=gap_recheck,
    )
    runner = LiveResearchRunner(
        brief=brief,
        candidate_ids=candidate_ids,
        registry=registry,
        provider=provider,
        budget=budget or make_budget(),
        decider=decider,
        config=config,
    )
    return runner, provider, run_dir


def decision_for(
    candidate_id: str, dimension_id: str, value: str, section_index: int = 0
) -> tuple[tuple[str, str], DimensionDecision]:
    section_id = f"s_{candidate_id}_{section_index:02d}"
    return (
        (candidate_id, dimension_id),
        DimensionDecision(
            value=value,
            claim=f"脚本决策：{candidate_id} 在 {dimension_id} 上为 {value}（依据快照切片）。",
            section_id=section_id,
        ),
    )

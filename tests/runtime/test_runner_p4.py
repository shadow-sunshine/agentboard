"""runner × 受控生成决策器集成测试（P4）：完整闭环、降级、显式失败、审计。

全部离线：检索用 `ScriptedSearchProvider`，决策 transport 用
`ScriptedDecisionTransport`（零网络、零时钟、零凭据）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.providers.decision_transport import (
    CompatibleApiDecisionTransport,
    ScriptedDecisionTransport,
)
from agentboard.providers.errors import TransportError
from agentboard.runtime.decider_generated import (
    DecisionGenConfig,
    GeneratedDimensionDecider,
)
from tests.runtime.conftest import (
    CANDIDATES,
    make_docs,
    make_runner,
)

PRIMARY_DIM = "deployment_complexity"


def _scale_labels(registry: Any) -> dict[str, tuple[str, ...]]:
    return {
        dimension.dimension_id: tuple(dimension.scale_labels)
        for dimension in registry.dimensions
    }


def _documents() -> dict[str, list[Any]]:
    """每个候选一份材料（决策所需的最低材料量；其余维度走显式弃权）。"""
    documents: dict[str, list[Any]] = {}
    for candidate_id in CANDIDATES:
        terms = f"{candidate_id} {PRIMARY_DIM}"
        documents[terms] = make_docs(terms, candidate_id, count=1)
    return documents


def _decision_json(candidate_id: str) -> str:
    return json.dumps(
        {
            "candidate_id": candidate_id,
            "dimension_id": PRIMARY_DIM,
            "value": "low",
            "claim": f"依据 {candidate_id} 快照切片的受控生成陈述。",
            "section_id": f"s_{candidate_id}_00",
            "confidence": 0.8,
        },
        ensure_ascii=False,
    )


INSUFFICIENT = json.dumps(
    {"insufficient_evidence": True, "reason": "本维度材料不足。"}, ensure_ascii=False
)


def _responses() -> dict[tuple[str, str], list[str]]:
    """12 个 (候选, 维度) 全部脚本化：主维度给决策，其余显式弃权。"""
    registry_dims = [
        "deployment_complexity", "ecosystem_maturity", "managed_service_needed",
        "ops_skill_floor", "query_latency", "scale_ceiling",
    ]
    responses: dict[tuple[str, str], list[str]] = {}
    for candidate_id in CANDIDATES:
        for dimension_id in registry_dims:
            if dimension_id == PRIMARY_DIM:
                responses[(candidate_id, dimension_id)] = [_decision_json(candidate_id)]
            else:
                responses[(candidate_id, dimension_id)] = [INSUFFICIENT]
    return responses


def make_generated_runner(
    *,
    tmp_path: Path,
    brief: Any,
    registry: Any,
    responses: dict[tuple[str, str], list[str]] | None = None,
    failures: dict[tuple[str, str], list[Exception]] | None = None,
    config_overrides: dict[str, Any] | None = None,
    run_name: str = "p4",
    gap_recheck: bool = False,
) -> tuple[Any, ScriptedDecisionTransport, Any]:
    responses = responses if responses is not None else _responses()
    transport = ScriptedDecisionTransport(
        responses=responses, failures=failures or {}
    )
    decider = GeneratedDimensionDecider(
        transport=transport,
        config=DecisionGenConfig(**{"max_retries": 1, **(config_overrides or {})}),
        scale_labels=_scale_labels(registry),
    )
    runner, _provider, _run_dir = make_runner(
        tmp_path=tmp_path,
        brief=brief,
        registry=registry,
        documents=_documents(),
        decider=decider,
        gap_recheck=gap_recheck,
        run_name=run_name,
    )
    return runner, transport, decider


def test_generated_decision_pipeline_completes(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """受控生成决策全闭环：检索 → 快照边界 → 生成决策 → 确定性报告。"""
    runner, _transport, _decider = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry, run_name="p4-ok"
    )
    result = runner.run()
    assert result.outcome == "completed"
    assert result.report is not None and result.report.integrity.passed is True
    assert result.decision_identity.startswith("generated.v1:")
    assert result.decision_budget_exhausted is False
    # 决策请求计数可观测：2 个候选 × 6 维度 = 12 次（其余维度显式弃权）
    assert result.budget_usage["decision_requests"] == 12
    assert result.budget_usage["decision_retries"] == 0
    # 受控生成决策进入了报告（与脚本决策器同一契约）
    values = {
        finding.candidate_id: finding.value
        for finding in result.report.findings
        if finding.dimension_id == PRIMARY_DIM and finding.is_supported
    }
    assert values == {candidate_id: "low" for candidate_id in CANDIDATES}


def test_run_id_covers_decision_identity(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """换决策器配置（模型/transport 配置）⇒ run_id 必须变化；同配置 ⇒ 稳定。"""
    runner_a, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry, run_name="rid-p4-a"
    )
    runner_b, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry, run_name="rid-p4-b"
    )
    runner_c, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        config_overrides={"request_timeout_seconds": 5.0}, run_name="rid-p4-c",
    )
    result_a = runner_a.run()
    result_b = runner_b.run()
    result_c = runner_c.run()
    assert result_a.run_id == result_b.run_id
    assert result_c.run_id != result_a.run_id


def test_run_id_changes_for_compatible_transport_configuration(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """兼容 API 的 endpoint/model/temperature 变化必须进入 run_id。"""
    runner, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry, run_name="rid-compat"
    )

    def identity(*, endpoint: str, model: str, temperature: float) -> str:
        return GeneratedDimensionDecider(
            transport=CompatibleApiDecisionTransport(
                endpoint=endpoint,
                model=model,
                temperature=temperature,
                api_key_env_var="AGENTBOARD_TEST_KEY",
                transport=object(),
            ),
            config=DecisionGenConfig(),
            scale_labels=_scale_labels(registry),
        ).identity

    base = identity(
        endpoint="https://api.example.com/v1/chat/completions",
        model="model-a",
        temperature=0.0,
    )
    changed = identity(
        endpoint="https://other.example.com/v1/chat/completions",
        model="model-b",
        temperature=0.7,
    )
    assert runner._run_id({}, {}, decider_identity=base) != runner._run_id(
        {}, {}, decider_identity=changed
    )



def test_decision_budget_exhaustion_degrades_observably(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """决策请求预算耗尽 ⇒ 已决策的保留、剩余显式 unsupported（可观测降级）。"""
    runner, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        config_overrides={"max_decision_requests": 8, "max_retries": 0},
        run_name="p4-budget",
    )
    result = runner.run()
    assert result.outcome == "completed"
    assert result.decision_budget_exhausted is True
    assert result.budget_usage["decision_requests"] == 8
    # 预算只够前 8 个 (候选, 维度)：pgvector 6 维 + qdrant 前 2 维，
    # qdrant 其余 4 维显式 unsupported
    assert set(result.unsupported_dimensions) == set(CANDIDATES)
    assert result.report is not None


def test_decision_generation_failure_is_observable_outcome(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """持续违例输出 ⇒ 重试耗尽 ⇒ 整次运行 `decision_failed`（无报告）。"""
    bad = json.dumps({"value": "root", "claim": "x", "section_id": "s_pgvector_00"})
    responses = {
        (candidate_id, PRIMARY_DIM): [bad] for candidate_id in CANDIDATES
    }
    runner, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        responses=responses,
        config_overrides={"max_retries": 1},
        run_name="p4-fail",
    )
    result = runner.run()
    assert result.outcome == "failed"
    assert result.failure_reason == "decision_failed:DecisionGenerationError"
    assert result.report is None


def test_no_silent_fallback_to_scripted_decider(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """transport 层持续失败 ⇒ 显式 `decision_failed`，绝不用脚本值掩盖。"""
    runner, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        responses={},
        failures={
            (candidate_id, PRIMARY_DIM): [TransportError("x"), TransportError("y")]
            for candidate_id in CANDIDATES
        },
        config_overrides={"max_retries": 1},
        run_name="p4-transport-fail",
    )
    result = runner.run()
    assert result.outcome == "failed"
    assert result.failure_reason.startswith("decision_failed:")
    assert result.report is None


def test_run_record_contains_closed_field_decision_audit(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """运行记录审计：决策身份、决策输出（闭集字段）、逐决策原文摘要（不含原文）。"""
    runner, _, _ = make_generated_runner(
        tmp_path=tmp_path, brief=brief, registry=registry, run_name="p4-audit"
    )
    result = runner.run()
    assert result.outcome == "completed"
    assert result.run_record_path is not None
    record = json.loads(result.run_record_path.read_text(encoding="utf-8"))
    assert record["decision_identity"].startswith("generated.v1:")
    assert record["decision_budget_exhausted"] is False
    # 决策输出：闭集字段
    decision_entry = record["decisions"]["pgvector"][PRIMARY_DIM]
    assert set(decision_entry) == {"value", "claim", "section_id", "confidence"}
    assert decision_entry["value"] == "low"
    # 逐决策审计：闭集字段，只有原文 sha256（不落模型原文 / 材料正文）
    audit_entry = record["decision_audit"][f"pgvector|{PRIMARY_DIM}"]
    assert set(audit_entry) == {
        "attempts", "confidence", "dimension_id", "outcome", "raw_chars",
        "raw_sha256", "section_id", "transport_id", "value",
    }
    assert audit_entry["outcome"] == "decided"
    assert len(audit_entry["raw_sha256"]) == 64


def test_scripted_decider_still_records_identity_and_stable_run_id(
    tmp_path: Any, brief: Any, registry: Any
) -> None:
    """P3 脚本决策器：identity 进入结果对象；同输入重放 run_id 稳定。"""
    from tests.runtime.conftest import decision_for

    scripted_decisions = dict(
        [decision_for("pgvector", PRIMARY_DIM, "low"),
         decision_for("qdrant_local", PRIMARY_DIM, "low")]
    )
    runner_a, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_documents(), decisions=scripted_decisions,
        gap_recheck=False, run_name="rid-scripted-a",
    )
    runner_b, _, _ = make_runner(
        tmp_path=tmp_path, brief=brief, registry=registry,
        documents=_documents(), decisions=scripted_decisions,
        gap_recheck=False, run_name="rid-scripted-b",
    )
    result_a = runner_a.run()
    result_b = runner_b.run()
    assert result_a.outcome == "completed"
    assert result_a.decision_identity.startswith("scripted.v1:")
    assert result_a.run_id == result_b.run_id

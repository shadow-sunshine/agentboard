#!/usr/bin/env python3
"""live-research MVP 的端到端离线评测（P3）。

跑 `eval/live_research/scenarios/`，覆盖 CURRENT_TASK 要求的七类情形：
成功、重试、超预算、无结果、来源不可解析、证据不足、重复运行确定性/稳定性。

全部离线：Provider 用 `runtime.mock_provider.ScriptedSearchProvider`
（脚本化文档与失败队列），零网络、零时钟、零凭据。

    python eval/live_research/run_eval.py                # 输出 JSON 指标
    python eval/live_research/run_eval.py --dump-actual  # 打印实测摘要（写 expectations 用）

每个场景独立跑两遍并比较结果（确定性 / 稳定性），与 `eval/research` 同一纪律。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = Path(__file__).resolve().parent
SCENARIOS_DIR = EVAL_ROOT / "scenarios"
EXPECTATIONS_DIR = EVAL_ROOT / "expectations"
DIMENSIONS_JSON = PROJECT_ROOT / "fixtures" / "research" / "dimensions" / "dimensions.v1.json"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.contracts import ProjectBrief  # noqa: E402
from agentboard.research.dimensions import load_dimension_registry  # noqa: E402
from agentboard.providers.decision_transport import (  # noqa: E402
    ScriptedDecisionTransport,
)
from agentboard.providers.errors import (  # noqa: E402
    ContentTooLargeError,
    RetrievalTimeoutError,
    SourceHTTPError,
    SourceResponseError,
    TransportError,
)
from agentboard.runtime.decider import DimensionDecision  # noqa: E402
from agentboard.runtime.decider_generated import (  # noqa: E402
    DecisionGenConfig,
    GeneratedDimensionDecider,
)
from agentboard.runtime.mock_provider import (  # noqa: E402
    ScriptedSearchProvider,
    scripted_document,
)
from agentboard.runtime.runner import LiveResearchConfig, LiveResearchRunner  # noqa: E402

#: 场景脚本可引用的异常名 → 异常类（失败脚本的闭集）。
FAILURE_EXCEPTIONS: dict[str, type[Exception]] = {
    "TransportError": TransportError,
    "RetrievalTimeoutError": RetrievalTimeoutError,
    "SourceHTTPError": SourceHTTPError,
    "SourceResponseError": SourceResponseError,
    "ContentTooLargeError": ContentTooLargeError,
}

CAPTURED_AT = "2026-09-26"
TEMP_ROOT = PROJECT_ROOT / "var" / "live-research-eval"


def _load_research_eval() -> Any:
    """加载 research 评测模块（复用 NetworkGuard / 场景装载，R6）。"""
    spec = importlib.util.spec_from_file_location(
        "agentboard_eval_research", PROJECT_ROOT / "eval" / "research" / "run_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


research_eval = _load_research_eval()


# ---------------------------------------------------------------- 场景执行


class ScenarioDecider:
    """评测用决策器：按场景声明的 (candidate, dimension) → (value, claim) 给出决策。

    `section_id` 取该候选语料的**第一个** section（评测文档各自唯一，顺序确定）。
    未声明的 (候选, 维度) 返回 None ⇒ 走缺口补检 / 显式 unsupported。
    """

    def __init__(self, decisions: Mapping[str, Mapping[str, Mapping[str, str]]]) -> None:
        self._decisions = decisions

    def decide(
        self,
        *,
        candidate_id: str,
        dimension_id: str,
        sections: Sequence[Mapping[str, str]],
    ) -> Any:
        entry = self._decisions.get(candidate_id, {}).get(dimension_id)
        if entry is None:
            return None
        if not sections:
            return None
        return DimensionDecision(
            value=entry["value"],
            claim=entry["claim"],
            section_id=sections[0].section_id,
        )


def _build_provider(spec: Mapping[str, Any], candidates: list[str]) -> ScriptedSearchProvider:
    documents: dict[str, list[Any]] = {}
    failures: dict[str, list[Exception]] = {}
    for candidate_id in candidates:
        for terms, doc_specs in (spec.get("documents") or {}).items():
            if candidate_id not in terms:
                continue
            for index, doc_spec in enumerate(doc_specs):
                documents.setdefault(terms, []).append(
                    scripted_document(
                        terms=terms,
                        candidate_id=candidate_id,
                        index=index,
                        body=doc_spec["content"],
                        captured_at=CAPTURED_AT,
                        repo=doc_spec.get("repo", "example/repo"),
                    )
                )
    for terms, error_names in (spec.get("failures") or {}).items():
        failures[terms] = [FAILURE_EXCEPTIONS[name]() for name in error_names]
    return ScriptedSearchProvider(documents=documents, captured_at=CAPTURED_AT, failures=failures)


def _build_decider(scenario: Mapping[str, Any], registry: Any) -> Any:
    """按场景构建决策器（P3 脚本 / P4 受控生成；字段闭集见场景文件）。"""
    spec = scenario.get("decider") or {"kind": "scripted"}
    kind = spec.get("kind", "scripted")
    if kind == "scripted":
        return ScenarioDecider(scenario.get("decisions") or {})
    if kind == "generated":
        responses = {
            tuple(key.split("|", 1)): list(values)
            for key, values in (spec.get("responses") or {}).items()
        }
        failures = {
            tuple(key.split("|", 1)): [FAILURE_EXCEPTIONS[name]() for name in values]
            for key, values in (spec.get("failures") or {}).items()
        }
        return GeneratedDimensionDecider(
            transport=ScriptedDecisionTransport(responses=responses, failures=failures),
            config=DecisionGenConfig(**(spec.get("config") or {})),
            scale_labels={
                dimension.dimension_id: tuple(dimension.scale_labels)
                for dimension in registry.dimensions
            },
        )
    raise SystemExit(f"未知决策器类型：{kind!r}")


def run_live_scenario(scenario: Mapping[str, Any], expectation: Mapping[str, Any]) -> dict[str, Any]:
    """完整跑一个 live 场景（含两遍），返回结果与差异清单。"""
    scenario_id = scenario["scenario_id"]
    expect = expectation["expect"]
    failures: list[str] = []

    registry = load_dimension_registry(DIMENSIONS_JSON)
    brief = ProjectBrief(
        text=scenario["brief"]["text"], source=scenario["brief"]["source"]
    )
    candidates = list(scenario["candidates"])
    budget_spec = scenario["budget"]
    from agentboard.runtime.budget import ResearchBudget

    budget = ResearchBudget(
        max_steps=budget_spec["max_steps"],
        max_provider_requests=budget_spec["max_provider_requests"],
        max_total_content_chars=budget_spec["max_total_content_chars"],
        max_retries_per_step=budget_spec.get("max_retries_per_step", 0),
    )

    def run_once() -> dict[str, Any]:
        provider = _build_provider(scenario["provider"], candidates)
        runner = LiveResearchRunner(
            brief=brief,
            candidate_ids=candidates,
            registry=registry,
            provider=provider,
            budget=budget,
            decider=_build_decider(scenario, registry),
            config=LiveResearchConfig(
                captured_at=CAPTURED_AT,
                dimensions_json=DIMENSIONS_JSON,
                snapshot_root=research_temp(scn_id=scenario_id, kind="snapshots"),
                run_dir=research_temp(scn_id=scenario_id, kind="run"),
                gap_recheck=bool(scenario.get("gap_recheck", True)),
            ),
        )
        result = runner.run()
        return {"result": result, "provider": provider}

    first = run_once()
    second = run_once()

    result = first["result"]
    # 确定性：两遍的 run_id / report_json / 步骤状态 / 失败原因必须一致
    if (
        result.run_id != second["result"].run_id
        or result.report_json != second["result"].report_json
        or [step.status for step in result.steps]
        != [step.status for step in second["result"].steps]
        or result.failure_reason != second["result"].failure_reason
    ):
        failures.append("两遍运行结果不一致（确定性破坏）")

    # 期望比对（只断言期望里显式声明的键；写 expectations 前用 --dump-actual 取值）
    if "outcome" in expect and result.outcome != expect["outcome"]:
        failures.append(f"outcome 期望 {expect['outcome']}，实际 {result.outcome}")
    if "failure_reason" in expect and expect["failure_reason"] != result.failure_reason:
        failures.append(
            f"failure_reason 期望 {expect['failure_reason']!r}，"
            f"实际 {result.failure_reason!r}"
        )
    report = result.report
    if expect.get("report_id") is not None:
        if report is None or report.report_id != expect["report_id"]:
            actual = report.report_id if report is not None else None
            failures.append(f"report_id 期望 {expect['report_id']}，实际 {actual}")
    if "integrity_passed" in expect:
        actual_passed = report.integrity.passed if report is not None else None
        if actual_passed != expect["integrity_passed"]:
            failures.append(
                f"integrity_passed 期望 {expect['integrity_passed']}，实际 {actual_passed}"
            )
    if "steps_status" in expect:
        actual_status = [step.status for step in result.steps]
        if actual_status != expect["steps_status"]:
            failures.append(f"步骤状态期望 {expect['steps_status']}，实际 {actual_status}")
    if "budget_provider_requests" in expect:
        actual_requests = result.budget_usage["provider_requests"]
        if actual_requests != expect["budget_provider_requests"]:
            failures.append(
                f"provider_requests 期望 {expect['budget_provider_requests']}，实际 {actual_requests}"
            )
    if "budget_retries" in expect:
        actual_retries = result.budget_usage["retries"]
        if actual_retries != expect["budget_retries"]:
            failures.append(f"retries 期望 {expect['budget_retries']}，实际 {actual_retries}")
    if "degraded_candidates" in expect:
        actual_degraded = list(result.degraded_candidates)
        if actual_degraded != expect["degraded_candidates"]:
            failures.append(
                f"degraded_candidates 期望 {expect['degraded_candidates']}，实际 {actual_degraded}"
            )
    if "unsupported_dimensions" in expect:
        actual_unsupported = {
            key: list(value) for key, value in result.unsupported_dimensions.items()
        }
        if actual_unsupported != expect["unsupported_dimensions"]:
            failures.append(
                f"unsupported_dimensions 期望 {expect['unsupported_dimensions']}，"
                f"实际 {actual_unsupported}"
            )
    # P4 决策层指标（可机械判定）
    if "decision_budget_exhausted" in expect:
        if result.decision_budget_exhausted != expect["decision_budget_exhausted"]:
            failures.append(
                f"decision_budget_exhausted 期望 {expect['decision_budget_exhausted']}，"
                f"实际 {result.decision_budget_exhausted}"
            )
    if "budget_decision_requests" in expect:
        actual_requests = result.budget_usage.get("decision_requests", 0)
        if actual_requests != expect["budget_decision_requests"]:
            failures.append(
                f"decision_requests 期望 {expect['budget_decision_requests']}，"
                f"实际 {actual_requests}"
            )
    if "budget_decision_retries" in expect:
        actual_retries = result.budget_usage.get("decision_retries", 0)
        if actual_retries != expect["budget_decision_retries"]:
            failures.append(
                f"decision_retries 期望 {expect['budget_decision_retries']}，"
                f"实际 {actual_retries}"
            )

    return {
        "scenario_id": scenario_id,
        "passed": not failures,
        "failures": failures,
        "result": result,
        "summary": result.to_dict(),
    }


def check_run_id_identity(
    outcomes: list[dict[str, Any]], expectations: Mapping[str, Mapping[str, Any]]
) -> None:
    """跨场景身份核对（P4）：`run_id_differs_from` 指向的场景 ⇒ run_id 必须不同。"""
    run_ids = {outcome["scenario_id"]: outcome["result"].run_id for outcome in outcomes}
    for outcome in outcomes:
        expect = expectations[outcome["scenario_id"]]["expect"]
        other_id = expect.get("run_id_differs_from")
        if not other_id:
            continue
        other = run_ids.get(other_id)
        if other is None:
            outcome["failures"].append(f"run_id_differs_from 指向不存在的场景 {other_id!r}")
        elif outcome["result"].run_id == other:
            outcome["failures"].append(
                f"run_id 与场景 {other_id!r} 相同（{outcome['result'].run_id}）——"
                "配置变化必须改变运行身份"
            )
        outcome["passed"] = not outcome["failures"]


_temp_root = TEMP_ROOT


def research_temp(*, scn_id: str, kind: str) -> Path:
    """场景级的运行产物目录（var/live-research-eval/<场景>/<kind>；运行时产物，可删）。"""
    path = _temp_root / scn_id / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- 指标


def summarize_live(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome["passed"])
    p4_outcomes = [
        outcome for outcome in outcomes if outcome["scenario_id"].startswith("p4_")
    ]
    return {
        "live_scenarios_total": total,
        "live_scenarios_passed": passed,
        "p4_scenarios_total": len(p4_outcomes),
        "p4_scenarios_passed": sum(1 for outcome in p4_outcomes if outcome["passed"]),
        "live_outcomes": {
            outcome["scenario_id"]: outcome["result"].outcome for outcome in outcomes
        },
        "network_calls": 0,
        "llm_calls": len(research_eval.forbidden_modules_loaded()),
        "detail": {
            "scenario_failures": {
                outcome["scenario_id"]: outcome["failures"]
                for outcome in outcomes
                if outcome["failures"]
            },
        },
    }


LIVE_METRIC_ASSERTIONS: tuple[tuple[str, Any], ...] = (
    ("live_scenarios_passed", None),
    ("network_calls", lambda value: value == 0),
    ("llm_calls", lambda value: value == 0),
)


def check_live_metrics(metrics: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if metrics["live_scenarios_passed"] != metrics["live_scenarios_total"]:
        problems.append(
            f"live_scenarios_passed ({metrics['live_scenarios_passed']}) != "
            f"live_scenarios_total ({metrics['live_scenarios_total']})"
        )
    if metrics["p4_scenarios_total"] < 8:
        problems.append(
            f"P4 场景数不足：{metrics['p4_scenarios_total']} < 8（CURRENT_TASK 要求）"
        )
    if metrics["p4_scenarios_passed"] != metrics["p4_scenarios_total"]:
        problems.append(
            f"p4_scenarios_passed ({metrics['p4_scenarios_passed']}) != "
            f"p4_scenarios_total ({metrics['p4_scenarios_total']})"
        )
    for name, predicate in LIVE_METRIC_ASSERTIONS:
        if predicate is None:
            continue
        if not predicate(metrics[name]):
            problems.append(f"指标未达标：{name} = {metrics[name]!r}")
    return problems


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="live-research MVP 端到端评测（离线，P3）")
    parser.add_argument("--dump-actual", action="store_true", help="打印实测摘要")
    args = parser.parse_args(argv)

    scenarios = research_eval._load_json_dir(SCENARIOS_DIR)
    expectations = research_eval._load_json_dir(EXPECTATIONS_DIR)

    missing = sorted(set(scenarios) - set(expectations))
    orphan = sorted(set(expectations) - set(scenarios))
    if missing:
        raise SystemExit(f"缺少期望文件：{missing}")
    if orphan:
        raise SystemExit(f"存在没有对应场景的期望文件：{orphan}")

    ordered = sorted(scenarios)
    with research_eval.NetworkGuard() as guard:
        outcomes = [
            run_live_scenario(scenarios[key], expectations[key]) for key in ordered
        ]
    check_run_id_identity(outcomes, expectations)
    metrics = summarize_live(outcomes)

    if args.dump_actual:
        for outcome in outcomes:
            result = outcome["result"]
            print(f"--- {outcome['scenario_id']} ---")
            print(f"  passed={outcome['passed']} outcome={result.outcome} "
                  f"reason={result.failure_reason!r}")
            if outcome["failures"]:
                print(f"  failures={outcome['failures']}")
            if result.report is not None:
                print(f"  report_id={result.report.report_id}")
                print(f"  integrity_passed={result.report.integrity.passed}")
                print(
                    "  ranking="
                    + json.dumps(
                        [
                            {"rank": rec.rank, "candidate_id": rec.candidate_id,
                             "score": round(rec.score, 6)}
                            for rec in result.report.recommendations
                        ],
                        ensure_ascii=False,
                    )
                )
            print(
                "  steps="
                + json.dumps(
                    {step.step_id: f"{step.status}({step.attempts})" for step in result.steps},
                    ensure_ascii=False,
                )
            )
            print(f"  budget={json.dumps(result.budget_usage, sort_keys=True)}")
            print(f"  degraded={list(result.degraded_candidates)}")
            print(
                "  unsupported="
                + json.dumps(
                    {key: list(value) for key, value in result.unsupported_dimensions.items()},
                    ensure_ascii=False,
                )
            )
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
    problems = check_live_metrics(metrics)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

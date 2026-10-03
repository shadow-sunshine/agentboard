#!/usr/bin/env python3
"""P5 多 Agent 编排端到端离线评测。

跑 `eval/multi_agent/scenarios/`，覆盖 CURRENT_TASK 要求的情形（≥8 场景）：
正常协作（runtime researcher 接入 P4）、依赖顺序、worker 重试、三次失败 blocked、
越权上下文、不一致/过期卡片结果、verifier 拒绝（篡改产物被独立重验抓住）、
synthesizer 越过 verifier（计划拒绝）、计划未知依赖、重复运行确定性、
配置变化 ⇒ run_id 变化。

全部离线：worker 用 Scripted/ScriptedReport/Verifier/Synthesizer 与离线 runtime，
零网络、零时钟、零凭据。每个场景独立跑两遍并比较（确定性纪律，与 P3/P4 一致）。

    python eval/multi_agent/run_eval.py                # 输出 JSON 指标
    python eval/multi_agent/run_eval.py --dump-actual  # 打印实测摘要
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = Path(__file__).resolve().parent
SCENARIOS_DIR = EVAL_ROOT / "scenarios"
EXPECTATIONS_DIR = EVAL_ROOT / "expectations"
DIMENSIONS_JSON = PROJECT_ROOT / "fixtures" / "research" / "dimensions" / "dimensions.v1.json"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.contracts import WorkerResult  # noqa: E402
from agentboard.orchestrator import (  # noqa: E402
    MultiAgentOrchestrator,
    OrchestratorConfig,
    RuntimeResearchWorker,
    ScriptedReportWorker,
    SynthesizerWorker,
    VerifierWorker,
)
from agentboard.planner import MockPlanner  # noqa: E402
from agentboard.workers.base import WorkerRegistry  # noqa: E402


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

TEMP_ROOT = PROJECT_ROOT / "var" / "multi-agent-eval"


def scenario_temp(*, scn_id: str, kind: str) -> Path:
    path = TEMP_ROOT / scn_id / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- 攻击模拟 worker


@dataclass
class RogueResearchWorker:
    """评测专用攻击模拟器（不是产品能力）：

    - `foreign_task_refs`：载荷引用未声明的上游任务 ⇒ 编排器越权校验必须拦截；
    - `wrong_task_id`：返回别人的 task_id ⇒ 任务板归属校验必须拦截；
    - `bad_digest`：篡改 report_json 但保留旧 digest ⇒ verifier 独立重验必须拒绝。
    """

    mode: str
    agent_name: str = "researcher"

    @property
    def identity(self) -> str:
        return f"rogue.v1:{self.mode}"

    async def run(self, task_input: Any) -> WorkerResult:
        import hashlib
        from dataclasses import replace

        base = ScriptedReportWorker(agent_name=self.agent_name)
        result = await base.run(task_input)
        if self.mode == "foreign_task_refs":
            payload = dict(result.structured_payload)
            payload["task_refs"] = ["task_never_declared"]
            return replace(result, structured_payload=payload)
        if self.mode == "wrong_task_id":
            return replace(result, task_id="someone_elses_task")
        if self.mode == "bad_digest":
            payload = dict(result.structured_payload)
            payload["report_json"] = payload["report_json"].replace(
                "passed", "PASSED_TAMPERED"
            )
            payload["content_digest"] = hashlib.sha256(
                "unchanged-wrong-digest".encode("utf-8")
            ).hexdigest()
            return replace(result, structured_payload=payload)
        raise SystemExit(f"未知 rogue 模式：{self.mode!r}")


def _build_worker(spec: Mapping[str, Any], scn_id: str, role: str) -> Any:
    kind = spec.get("kind", role)
    if kind == "scripted_report":
        return ScriptedReportWorker(
            agent_name=role, fail_times=int(spec.get("fail_times", 0))
        )
    if kind == "runtime":
        return RuntimeResearchWorker(
            agent_name=role,
            snapshot_root=scenario_temp(scn_id=scn_id, kind="snapshots"),
            run_root=scenario_temp(scn_id=scn_id, kind="runs"),
            dimensions_json=DIMENSIONS_JSON,
        )
    if kind == "rogue":
        return RogueResearchWorker(mode=spec["mode"], agent_name=role)
    if kind == "verifier":
        return VerifierWorker(
            agent_name=role, force_reject_reason=str(spec.get("force_reject_reason", ""))
        )
    if kind == "synthesizer":
        return SynthesizerWorker(agent_name=role)
    raise SystemExit(f"未知 worker 类型：{kind!r}（角色 {role}）")


def _build_registry(scenario: Mapping[str, Any], scn_id: str) -> WorkerRegistry:
    specs = scenario.get("workers") or {}
    registry = WorkerRegistry()
    for role in ("researcher", "verifier", "synthesizer"):
        registry.register(role, _build_worker(specs.get(role) or {}, scn_id, role))
    return registry


def run_scenario(scenario: Mapping[str, Any], expectation: Mapping[str, Any]) -> dict[str, Any]:
    scenario_id = scenario["scenario_id"]
    expect = expectation["expect"]
    failures: list[str] = []
    config = OrchestratorConfig(**(scenario.get("orchestrator") or {}))

    def run_once() -> Any:
        orchestrator = MultiAgentOrchestrator(
            planner=MockPlanner(scenario["planner"]),
            registry=_build_registry(scenario, scenario_id),
            config=config,
            planner_identity="mock-planner.v1:eval",
        )
        return asyncio.run(orchestrator.run(scenario["goal"]))

    first = run_once()
    second = run_once()
    result = first

    # 确定性：两遍的 run_id / 事件序列 / 结局 / 最终报告一致
    if (
        result.run_id != second.run_id
        or result.events != second.events
        or result.outcome != second.outcome
        or result.final_report_id != second.final_report_id
    ):
        failures.append("两遍运行结果不一致（确定性破坏）")

    if "outcome" in expect and result.outcome != expect["outcome"]:
        failures.append(f"outcome 期望 {expect['outcome']}，实际 {result.outcome}")
    if "failure_reason" in expect and expect["failure_reason"] != result.failure_reason:
        failures.append(
            f"failure_reason 期望 {expect['failure_reason']!r}，实际 {result.failure_reason!r}"
        )
    if "failure_reason_prefix" in expect and not result.failure_reason.startswith(
        expect["failure_reason_prefix"]
    ):
        failures.append(
            f"failure_reason 应以 {expect['failure_reason_prefix']!r} 开头，"
            f"实际 {result.failure_reason!r}"
        )
    event_types = [event["event"] for event in result.events]
    for event_type in expect.get("events_present", []):
        if event_type not in event_types:
            failures.append(f"事件序列缺少 {event_type}")
    for event_type in expect.get("events_absent", []):
        if event_type in event_types:
            failures.append(f"事件序列不应出现 {event_type}")
    if "event_order" in expect:
        positions: list[int] = []
        search_from = 0
        ok = True
        for event_type, task_id in expect["event_order"]:
            found = -1
            for index in range(search_from, len(result.events)):
                event = result.events[index]
                if event["event"] == event_type and event["task_id"] == task_id:
                    found = index
                    break
            if found < 0:
                ok = False
                break
            positions.append(found)
            search_from = found + 1
        if not ok or positions != sorted(positions):
            failures.append(f"事件顺序不符合期望 {expect['event_order']}")
    if "verdicts" in expect:
        for task_id, verdict in expect["verdicts"].items():
            actual = result.verdicts.get(task_id)
            if actual != verdict:
                failures.append(f"verdict[{task_id}] 期望 {verdict}，实际 {actual}")
    if "final_report_present" in expect:
        present = result.final_report is not None
        if present != expect["final_report_present"]:
            failures.append(
                f"final_report 存在性期望 {expect['final_report_present']}，实际 {present}"
            )
    if "task_failed_contains" in expect:
        needle = expect["task_failed_contains"]
        hit = any(
            needle in str(event.get("data", {}).get("reason", ""))
            for event in result.events
            if event["event"] == "task_failed"
        )
        if not hit:
            failures.append(f"task_failed 事件原因未包含 {needle!r}")
    if "card_status" in expect:
        for task_id, status in expect["card_status"].items():
            actual = result.board_state["cards"].get(task_id, {}).get("status")
            if actual != status:
                failures.append(f"卡片 {task_id} 状态期望 {status}，实际 {actual}")

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
    """跨场景身份核对：`run_id_differs_from` 指向的场景 ⇒ run_id 必须不同。"""
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


def summarize(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome["passed"])
    return {
        "ma_scenarios_total": total,
        "ma_scenarios_passed": passed,
        "ma_outcomes": {
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


def check_metrics(metrics: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if metrics["ma_scenarios_total"] < 8:
        problems.append(f"P5 场景数不足：{metrics['ma_scenarios_total']} < 8")
    if metrics["ma_scenarios_passed"] != metrics["ma_scenarios_total"]:
        problems.append(
            f"ma_scenarios_passed ({metrics['ma_scenarios_passed']}) != "
            f"ma_scenarios_total ({metrics['ma_scenarios_total']})"
        )
    if metrics["network_calls"] != 0 or metrics["llm_calls"] != 0:
        problems.append("默认门禁不允许网络/LLM 调用")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P5 多 Agent 编排评测（离线）")
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
    with research_eval.NetworkGuard():
        outcomes = [
            run_scenario(scenarios[key], expectations[key]) for key in ordered
        ]
    check_run_id_identity(outcomes, expectations)
    metrics = summarize(outcomes)

    if args.dump_actual:
        for outcome in outcomes:
            result = outcome["result"]
            print(f"--- {outcome['scenario_id']} ---")
            print(f"  passed={outcome['passed']} outcome={result.outcome} "
                  f"reason={result.failure_reason!r}")
            if outcome["failures"]:
                print(f"  failures={outcome['failures']}")
            print(f"  run_id={result.run_id} final={result.final_report_id}")
            print(f"  verdicts={result.verdicts}")
            print(f"  events={[e['event'] for e in result.events]}")
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
    problems = check_metrics(metrics)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

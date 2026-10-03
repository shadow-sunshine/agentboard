#!/usr/bin/env python3
"""确定性报告组装的固定场景评测（P1A-4）。

跑固定场景集，输出**可回归的数字**。全部离线：不联网、不调模型、不用 git/grep，
只用标准库 + 本仓库的 `src/`（`dependencies = []` 保持不变）。

    python eval/research/run_eval.py                # 跑全部场景，输出 JSON 指标
    python eval/research/run_eval.py --dump-actual  # 打印每个场景的实测摘要（写 expectations 用）

三类场景（由 `scenarios/*.json` 的 `mutation` 决定）：

| mode | 含义 |
|---|---|
| `report` | 组装成功；断言 report_id / 排名 / 缺口 / 风险规则 / 覆盖 |
| `report` + `integrity_passed=false` | 组装成功但完整性不过；**同时断言 `strict=False` 的失败码**与 `strict=True` 抛出的失败码 |
| `domain_error` | 组装在预检/收集阶段就中断；断言明确的异常类型 |

每个场景**独立跑两遍**并比较 `report.to_dict()` 的 JSON（或异常类型），
用来验证"同一输入必得同一报告"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = PROJECT_ROOT / "fixtures" / "research"
SCENARIOS_DIR = EVAL_ROOT / "scenarios"
EXPECTATIONS_DIR = EVAL_ROOT / "expectations"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.contracts import ProjectBrief, TaskCard, WorkerTaskInput  # noqa: E402
from agentboard.evidence import SOURCE_FIXTURE, EvidenceRef, is_trusted_evidence  # noqa: E402
from agentboard.research.assemble import (  # noqa: E402
    assemble_report,
    preflight_registry,
    preflight_snapshots,
)
from agentboard.research.dimensions import DimensionRegistry, load_dimension_registry  # noqa: E402
from agentboard.research.errors import (  # noqa: E402
    AgentBoardResearchError,
    ReportIntegrityError,
)
from agentboard.research.fixtures import FixtureResearchWorker  # noqa: E402
from agentboard.research.payload import decode_findings, encode_findings  # noqa: E402
from agentboard.research.snapshot import (  # noqa: E402
    FixtureDimensionValue,
    FixtureSnapshot,
    load_catalog,
    load_snapshots,
    resolve_locator,
)
from agentboard.workers.synthetic import SyntheticEvidenceFactory  # noqa: E402

SCENARIO_CONTEXT = "公共背景：为检索服务做技术选型"

#: 在快照层施加的变异（改的是内存里的 FixtureSnapshot，不写文件，因此 digest 不受影响）
SNAPSHOT_MUTATIONS = frozenset(
    {"none", "drop_dimension_declaration", "override_dimension_values", "add_unknown_dimension"}
)
#: 在 WorkerResult 载荷层施加的变异（改的是 findings 的 evidence_refs）
PAYLOAD_MUTATIONS = frozenset(
    {"replace_evidence_kind", "collapse_evidence_to_one_section", "swap_excerpt_between_sections"}
)

#: 出现即说明被引入了禁止的技术栈；用于 llm_calls 指标
FORBIDDEN_MODULES = (
    "openai",
    "anthropic",
    "langchain",
    "langgraph",
    "acp",
    "fastapi",
    "uvicorn",
    "requests",
    "httpx",
    "aiohttp",
)


# ---------------------------------------------------------------- 网络哨兵


def is_local_address(address: Any) -> bool:
    """判断目标地址是否为本机。

    需要这个判断的原因很具体：Windows 上 `asyncio` 的事件循环用一个 **loopback
    socketpair** 做自唤醒管道，`socket.socketpair()` 在 Windows 下是通过连接
    127.0.0.1 模拟的。若把 loopback 也算作"出网"，指标会被自己的运行时噪音污染
    —— 那正是"误报的守卫迟早被绕过"的另一种形式。

    因此 `network_calls` 的定义收紧为：**连接非本地地址的尝试次数**。
    """
    if isinstance(address, str):
        return True  # AF_UNIX 路径
    if isinstance(address, (tuple, list)) and address:
        host = str(address[0])
        return host in {"", "localhost", "0.0.0.0", "::1"} or host.startswith("127.")
    return False


class NetworkGuard:
    """记录评测期间的出网尝试。`calls` 只统计非本地地址，期望恒为 0。"""

    def __init__(self) -> None:
        self.calls = 0
        self.all_connect_attempts = 0
        self._original_connect: Any = None
        self._original_create: Any = None

    def __enter__(self) -> NetworkGuard:
        self._original_connect = socket.socket.connect
        self._original_create = socket.create_connection

        def counted_connect(this: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
            self.all_connect_attempts += 1
            if not is_local_address(address):
                self.calls += 1
            return self._original_connect(this, address, *args, **kwargs)

        def counted_create(address: Any, *args: Any, **kwargs: Any) -> Any:
            self.all_connect_attempts += 1
            if not is_local_address(address):
                self.calls += 1
            return self._original_create(address, *args, **kwargs)

        socket.socket.connect = counted_connect  # type: ignore[method-assign]
        socket.create_connection = counted_create  # type: ignore[assignment]
        return self

    def __exit__(self, *_: object) -> None:
        socket.socket.connect = self._original_connect  # type: ignore[method-assign]
        socket.create_connection = self._original_create  # type: ignore[assignment]


def forbidden_modules_loaded() -> tuple[str, ...]:
    return tuple(sorted(name for name in FORBIDDEN_MODULES if name in sys.modules))


# ---------------------------------------------------------------- 场景装载


def _load_json_dir(directory: Path) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit(f"{path.name} 的顶层必须是 JSON 对象")
        scenario_id = payload.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise SystemExit(f"{path.name} 缺少 scenario_id")
        if scenario_id in items:
            raise SystemExit(f"重复的 scenario_id：{scenario_id}")
        items[scenario_id] = payload
    if not items:
        raise SystemExit(f"{directory} 下没有任何场景文件")
    return items


# ---------------------------------------------------------------- 变异


def _resolve_canonical(registry: DimensionRegistry, name: str) -> str:
    return registry.resolve(name)


def _apply_snapshot_mutation(
    snapshots: dict[str, FixtureSnapshot], mutation: dict[str, Any], registry: DimensionRegistry
) -> dict[str, FixtureSnapshot]:
    kind = mutation.get("kind", "none")
    if kind == "none":
        return snapshots
    if kind == "drop_dimension_declaration":
        candidate = mutation["candidate"]
        canonical = _resolve_canonical(registry, mutation["dimension"])
        snapshot = snapshots[candidate]
        kept = {
            key: value
            for key, value in snapshot.dimension_values.items()
            if _resolve_canonical(registry, key) != canonical
        }
        if len(kept) == len(snapshot.dimension_values):
            raise SystemExit(f"{candidate} 本来就没有声明维度 {canonical}，无法删除")
        return {**snapshots, candidate: replace(snapshot, dimension_values=kept)}
    if kind == "override_dimension_values":
        result = dict(snapshots)
        for candidate, overrides in mutation["overrides"].items():
            snapshot = result[candidate]
            values = dict(snapshot.dimension_values)
            for name, spec in overrides.items():
                canonical = _resolve_canonical(registry, name)
                previous = next(
                    (v for k, v in values.items() if _resolve_canonical(registry, k) == canonical),
                    None,
                )
                for key in [k for k in values if _resolve_canonical(registry, k) == canonical]:
                    del values[key]
                section_id = spec.get("section_id") or (previous.section_id if previous else None)
                if section_id is None:
                    raise SystemExit(f"{candidate}.{canonical} 既无旧声明也未给 section_id")
                values[canonical] = FixtureDimensionValue(
                    dimension_id=canonical,
                    value=spec["value"],
                    section_id=section_id,
                    claim=spec.get("claim", f"评测变异：{canonical} = {spec['value']}"),
                )
            result[candidate] = replace(snapshot, dimension_values=values)
        return result
    if kind == "add_unknown_dimension":
        candidate = mutation["candidate"]
        dimension = mutation["dimension"]
        snapshot = snapshots[candidate]
        values = dict(snapshot.dimension_values)
        values[dimension] = FixtureDimensionValue(
            dimension_id=dimension,
            value="low",
            section_id=snapshot.sections[0].section_id,
            claim="评测注入的未知维度声明",
        )
        return {**snapshots, candidate: replace(snapshot, dimension_values=values)}
    raise SystemExit(f"未知的 snapshot 变异：{kind}")


def _rewrite_cards(cards: list[TaskCard], transform: Any) -> list[TaskCard]:
    rewritten: list[TaskCard] = []
    for card in cards:
        if card.result is None:
            rewritten.append(card)
            continue
        findings = list(decode_findings(card.result.structured_payload))
        updated = transform(card, findings)
        rewritten.append(
            replace(card, result=replace(card.result, structured_payload=encode_findings(updated)))
        )
    return rewritten


def _apply_payload_mutation(cards: list[TaskCard], mutation: dict[str, Any]) -> list[TaskCard]:
    kind = mutation["kind"]
    target = mutation["candidate"]

    if kind == "replace_evidence_kind":
        source_type = mutation["source_type"]

        def transform(card: TaskCard, findings: list[Any]) -> list[Any]:
            if card.task_id != f"research__{target}":
                return findings
            rebuilt = []
            for finding in findings:
                if not finding.is_supported:
                    rebuilt.append(finding)
                    continue
                if source_type == "synthetic_mock":
                    refs = SyntheticEvidenceFactory.for_task(
                        "research", finding.task_id, count=1
                    )
                else:
                    from agentboard.research.migrate import migrate_legacy_evidence  # noqa: PLC0415

                    refs = migrate_legacy_evidence(
                        [f"历史自由文本证据（{finding.dimension_id}）"], task_id=finding.task_id
                    )
                rebuilt.append(replace(finding, evidence_refs=refs, finding_id=""))
            return rebuilt

        return _rewrite_cards(cards, transform)

    if kind == "collapse_evidence_to_one_section":

        def transform(card: TaskCard, findings: list[Any]) -> list[Any]:
            if card.task_id != f"research__{target}":
                return findings
            supported = [f for f in findings if f.is_supported]
            if len(supported) < 2:
                raise SystemExit(f"{target} 的 supported finding 不足两条，无法塌缩证据来源")
            shared = supported[0].evidence_refs
            return [
                replace(finding, evidence_refs=shared, finding_id="")
                if finding.is_supported
                else finding
                for finding in findings
            ]

        return _rewrite_cards(cards, transform)

    if kind == "swap_excerpt_between_sections":

        def transform(card: TaskCard, findings: list[Any]) -> list[Any]:
            if card.task_id != f"research__{target}":
                return findings
            supported = [f for f in findings if f.is_supported]
            if len(supported) < 2:
                raise SystemExit(f"{target} 的 supported finding 不足两条，无法对调摘录")
            victim, donor = supported[0], supported[1]
            original = victim.evidence_refs[0]
            forged = EvidenceRef(
                source_type=SOURCE_FIXTURE,
                source_id=original.source_id,
                snapshot_version=original.snapshot_version,
                locator=original.locator,
                excerpt=donor.evidence_refs[0].excerpt,
                captured_at=original.captured_at,
            )
            return [
                replace(finding, evidence_refs=(forged,), finding_id="")
                if finding is victim
                else finding
                for finding in findings
            ]

        return _rewrite_cards(cards, transform)

    raise SystemExit(f"未知的 payload 变异：{kind}")


# ---------------------------------------------------------------- 单场景执行


@dataclass
class Outcome:
    scenario_id: str
    passed: bool
    failures: list[str]
    report: Any | None
    report_json: str | None
    exception_type: str | None
    failed_codes: tuple[str, ...]
    snapshots: dict[str, FixtureSnapshot]
    checks_executed: int


def _make_cards(worker: FixtureResearchWorker, candidate_ids: list[str]) -> list[TaskCard]:
    cards: list[TaskCard] = []
    for candidate_id in candidate_ids:
        task_id = f"research__{candidate_id}"
        context = f"{SCENARIO_CONTEXT}\ncandidate_id: {candidate_id}\n"
        task_input = WorkerTaskInput(
            task_id=task_id,
            title="调研候选方案",
            assigned_agent="research",
            goal="调研该候选方案在本快照里的表现",
            context=context,
            upstream_results={},
        )
        result = asyncio.run(worker.run(task_input))
        cards.append(
            TaskCard(
                task_id=task_id,
                title="调研候选方案",
                assigned_agent="research",
                goal="调研该候选方案在本快照里的表现",
                context=context,
                status="done",
                result=result,
                attempts=1,
            )
        )
    return cards


def run_scenario(scenario: dict[str, Any], expectation: dict[str, Any]) -> Outcome:
    """完整跑一个场景（含变异与两次组装），返回结果与差异清单。"""
    scenario_id = scenario["scenario_id"]
    expect = expectation["expect"]
    mode = expect["mode"]
    strict = bool(expect.get("strict", True))
    failures: list[str] = []

    catalog = load_catalog(FIXTURE_ROOT)
    registry = load_dimension_registry(FIXTURE_ROOT / catalog.dimension_registry_path)
    snapshots: dict[str, FixtureSnapshot] = dict(load_snapshots(FIXTURE_ROOT, catalog))

    mutation = scenario.get("mutation", {"kind": "none"})
    kind = mutation.get("kind", "none")
    if kind in SNAPSHOT_MUTATIONS:
        snapshots = _apply_snapshot_mutation(snapshots, mutation, registry)
    elif kind not in PAYLOAD_MUTATIONS:
        raise SystemExit(f"未知的变异 kind：{kind}")

    brief_payload = scenario["brief"]
    brief = ProjectBrief(text=brief_payload["text"], source=brief_payload["source"])
    candidate_ids = list(scenario.get("candidates") or catalog.ids())

    report = None
    exception_type = None
    failed_codes: tuple[str, ...] = ()
    report_json = None
    checks_executed = 0
    cards: list[TaskCard] = []

    # 真实流水线的顺序：**先校验配置，再跑 Worker，最后组装**。
    # 配置类错误必须在动手组装之前失败，而不是算到一半才炸出来。
    try:
        preflight_registry(registry)
        preflight_snapshots(registry, snapshots)
        cards = _make_cards(FixtureResearchWorker(catalog, registry, snapshots), candidate_ids)
        if kind in PAYLOAD_MUTATIONS:
            cards = _apply_payload_mutation(cards, mutation)
        report = assemble_report(brief, cards, registry, snapshots=snapshots, strict=strict)
    except AgentBoardResearchError as exc:
        exception_type = type(exc).__name__
        if isinstance(exc, ReportIntegrityError):
            failed_codes = exc.failed_codes
        if mode != "domain_error":
            failures.append(f"组装意外失败：{exception_type}: {exc}")
    else:
        checks_executed = len(report.integrity.checks)
        report_json = json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False)
        if mode == "domain_error":
            failures.append(f"期望在预检/收集阶段失败（{expect.get('exception')}），实际组装成功")

    if mode == "domain_error":
        expected_exception = expect["exception"]
        if exception_type != expected_exception:
            failures.append(f"期望异常 {expected_exception}，实际 {exception_type}")
    else:
        if report is not None:
            failures.extend(_compare_report(report, expect))
        if "strict_error_codes" in expect:
            failures.extend(_check_strict_codes(brief, cards, registry, snapshots, expect))

    return Outcome(
        scenario_id=scenario_id,
        passed=not failures,
        failures=failures,
        report=report,
        report_json=report_json,
        exception_type=exception_type,
        failed_codes=failed_codes,
        snapshots=snapshots,
        checks_executed=checks_executed,
    )


def _check_strict_codes(
    brief: ProjectBrief,
    cards: list[TaskCard],
    registry: DimensionRegistry,
    snapshots: dict[str, FixtureSnapshot],
    expect: dict[str, Any],
) -> list[str]:
    """缺陷场景必须断言**明确的失败码**，而不是"程序抛了个异常就算过"。"""
    expected_codes = list(expect["strict_error_codes"])
    try:
        assemble_report(brief, cards, registry, snapshots=snapshots, strict=True)
    except ReportIntegrityError as exc:
        actual = list(exc.failed_codes)
        if actual != expected_codes:
            return [f"strict=True 的失败码期望 {expected_codes}，实际 {actual}"]
        return []
    except AgentBoardResearchError as exc:  # pragma: no cover - 兜底
        return [f"strict=True 期望 ReportIntegrityError，实际 {type(exc).__name__}: {exc}"]
    return ["strict=True 期望抛出 ReportIntegrityError，实际组装成功"]


def _compare_report(report: Any, expect: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    if "report_id" in expect and report.report_id != expect["report_id"]:
        failures.append(f"report_id 期望 {expect['report_id']}，实际 {report.report_id}")

    if "integrity_passed" in expect and report.integrity.passed != expect["integrity_passed"]:
        failures.append(
            f"integrity_passed 期望 {expect['integrity_passed']}，实际 {report.integrity.passed}"
        )

    if "failed_codes" in expect:
        actual = list(report.integrity.failed_codes)
        if actual != list(expect["failed_codes"]):
            failures.append(f"failed_codes 期望 {expect['failed_codes']}，实际 {actual}")

    if "ranking" in expect:
        actual = [
            {"rank": rec.rank, "candidate_id": rec.candidate_id, "score": round(rec.score, 6)}
            for rec in report.recommendations
        ]
        if actual != expect["ranking"]:
            failures.append(f"ranking 期望 {expect['ranking']}，实际 {actual}")

    if "unsupported" in expect:
        actual: dict[str, list[str]] = {}
        for item in report.coverage.candidates:
            if item.missing_dimensions:
                actual[item.candidate_id] = list(item.missing_dimensions)
        if actual != expect["unsupported"]:
            failures.append(f"unsupported 期望 {expect['unsupported']}，实际 {actual}")

    if "risk_rule_ids" in expect:
        actual_rules = sorted({risk.rule_id for risk in report.risks if risk.rule_id})
        if actual_rules != sorted(expect["risk_rule_ids"]):
            failures.append(
                f"risk_rule_ids 期望 {sorted(expect['risk_rule_ids'])}，实际 {actual_rules}"
            )

    if "coverage_supported" in expect:
        actual_supported = {
            item.candidate_id: item.supported_dimensions for item in report.coverage.candidates
        }
        if actual_supported != expect["coverage_supported"]:
            failures.append(
                f"coverage_supported 期望 {expect['coverage_supported']}，实际 {actual_supported}"
            )

    if "trusted_finding_counts" in expect:
        actual_trusted = {
            item.candidate_id: len(item.trusted_finding_ids) for item in report.coverage.candidates
        }
        if actual_trusted != expect["trusted_finding_counts"]:
            failures.append(
                f"trusted_finding_counts 期望 {expect['trusted_finding_counts']}，实际 {actual_trusted}"
            )

    return failures


# ---------------------------------------------------------------- 指标


def _iter_refs(report: Any) -> list[Any]:
    refs = [ref for finding in report.findings for ref in finding.evidence_refs]
    refs.extend(ref for rec in report.recommendations for ref in rec.evidence_refs)
    return refs


def summarize(
    scenarios: dict[str, dict[str, Any]],
    outcomes: list[Outcome],
    repeats: list[Outcome],
    network_calls: int,
    all_connect_attempts: int = 0,
) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome.passed)

    deterministic = sum(
        1
        for first, second in zip(outcomes, repeats, strict=True)
        if first.report_json == second.report_json
        and first.exception_type == second.exception_type
        and first.failed_codes == second.failed_codes
    )

    report_outcomes = [outcome for outcome in outcomes if outcome.report is not None]

    resolver_total = 0
    resolver_ok = 0
    source_counts: Counter[str] = Counter()
    supporting_counts: Counter[str] = Counter()
    silent_missing = 0
    checks_executed = 0

    for outcome in report_outcomes:
        report = outcome.report
        checks_executed += outcome.checks_executed

        for ref in _iter_refs(report):
            source_counts[ref.source_type] += 1
            if ref.source_type != SOURCE_FIXTURE:
                continue
            resolver_total += 1
            snapshot = outcome.snapshots.get(ref.source_id.removeprefix("fixture:"))
            if snapshot is None:
                continue
            try:
                text = resolve_locator(snapshot, ref.locator)
            except Exception:  # noqa: BLE001 - 解析失败即计入不可解析
                continue
            if text.startswith(ref.excerpt):
                resolver_ok += 1

        for rec in report.recommendations:
            for ref in rec.evidence_refs:
                supporting_counts[ref.source_type] += 1
                if ref.source_type != SOURCE_FIXTURE:
                    pass

        registry_dimensions = {dimension.dimension_id for dimension in report.dimensions}
        for candidate_id in report.candidate_ids:
            present = {
                finding.dimension_id
                for finding in report.findings
                if finding.candidate_id == candidate_id
            }
            silent_missing += len(registry_dimensions - present)

    supporting_total = sum(supporting_counts.values())
    fixture_supporting = supporting_counts[SOURCE_FIXTURE]

    return {
        "scenarios_total": total,
        "scenarios_passed": passed,
        "determinism_repeat_rate": (deterministic / total) if total else 0.0,
        "evidence_resolvable_rate": (resolver_ok / resolver_total) if resolver_total else 0.0,
        "fixture_evidence_rate": (fixture_supporting / supporting_total) if supporting_total else 0.0,
        "silent_missing_dimensions": silent_missing,
        "integrity_checks_executed": checks_executed,
        "network_calls": network_calls,
        "llm_calls": len(forbidden_modules_loaded()),
        "detail": {
            "fixture_locators_resolved": f"{resolver_ok}/{resolver_total}",
            "evidence_source_type_counts": dict(sorted(source_counts.items())),
            "recommendation_supporting_counts": dict(sorted(supporting_counts.items())),
            "forbidden_modules_loaded": list(forbidden_modules_loaded()),
            "all_socket_connect_attempts": all_connect_attempts,
            "scenario_failures": {
                outcome.scenario_id: outcome.failures
                for outcome in outcomes
                if outcome.failures
            },
        },
    }


METRIC_ASSERTIONS: tuple[tuple[str, Any], ...] = (
    ("scenarios_total", lambda value: value >= 8),
    ("scenarios_passed", None),
    ("determinism_repeat_rate", lambda value: value == 1.0),
    ("evidence_resolvable_rate", lambda value: value == 1.0),
    ("fixture_evidence_rate", lambda value: value == 1.0),
    ("silent_missing_dimensions", lambda value: value == 0),
    ("integrity_checks_executed", lambda value: value >= 9),
    ("network_calls", lambda value: value == 0),
    ("llm_calls", lambda value: value == 0),
)


def check_metrics(metrics: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if metrics["scenarios_passed"] != metrics["scenarios_total"]:
        problems.append(
            f"scenarios_passed ({metrics['scenarios_passed']}) != scenarios_total "
            f"({metrics['scenarios_total']})"
        )
    for name, predicate in METRIC_ASSERTIONS:
        if predicate is None:
            continue
        value = metrics[name]
        if not predicate(value):
            problems.append(f"指标未达标：{name} = {value!r}")
    return problems


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确定性报告组装固定场景评测（离线）")
    parser.add_argument("--dump-actual", action="store_true", help="打印每个场景的实测摘要")
    args = parser.parse_args(argv)

    scenarios = _load_json_dir(SCENARIOS_DIR)
    expectations = _load_json_dir(EXPECTATIONS_DIR)

    missing = sorted(set(scenarios) - set(expectations))
    orphan = sorted(set(expectations) - set(scenarios))
    if missing:
        raise SystemExit(f"缺少期望文件：{missing}")
    if orphan:
        raise SystemExit(f"存在没有对应场景的期望文件：{orphan}")

    ordered = sorted(scenarios)
    with NetworkGuard() as guard:
        outcomes = [run_scenario(scenarios[key], expectations[key]) for key in ordered]
        repeats = [run_scenario(scenarios[key], expectations[key]) for key in ordered]
        metrics = summarize(scenarios, outcomes, repeats, guard.calls, guard.all_connect_attempts)

    if args.dump_actual:
        for outcome in outcomes:
            report = outcome.report
            print(f"--- {outcome.scenario_id} ---")
            print(f"  passed={outcome.passed} exception={outcome.exception_type}")
            if outcome.failures:
                print(f"  failures={outcome.failures}")
            if report is not None:
                print(f"  report_id={report.report_id}")
                print(f"  integrity_passed={report.integrity.passed} failed={list(report.integrity.failed_codes)}")
                print(
                    "  ranking="
                    + json.dumps(
                        [
                            {
                                "rank": rec.rank,
                                "candidate_id": rec.candidate_id,
                                "score": round(rec.score, 6),
                            }
                            for rec in report.recommendations
                        ],
                        ensure_ascii=False,
                    )
                )
                print(
                    "  unsupported="
                    + json.dumps(
                        {
                            item.candidate_id: list(item.missing_dimensions)
                            for item in report.coverage.candidates
                            if item.missing_dimensions
                        },
                        ensure_ascii=False,
                    )
                )
                print(
                    "  risk_rule_ids="
                    + json.dumps(
                        sorted({risk.rule_id for risk in report.risks if risk.rule_id}),
                        ensure_ascii=False,
                    )
                )
                print(
                    "  coverage_supported="
                    + json.dumps(
                        {item.candidate_id: item.supported_dimensions for item in report.coverage.candidates},
                        ensure_ascii=False,
                    )
                )
                print(
                    "  trusted_finding_counts="
                    + json.dumps(
                        {item.candidate_id: len(item.trusted_finding_ids) for item in report.coverage.candidates},
                        ensure_ascii=False,
                    )
                )
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

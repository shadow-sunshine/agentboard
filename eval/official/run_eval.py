#!/usr/bin/env python3
"""官方快照集的固定场景评测（P2.2-4）。

跑 `eval/official/scenarios/`，输出**可回归的数字**（P2.2_PLAN §6.7 的官方指标）。
全部离线：不联网、不调模型；来源锚点核对只读 `.workbuddy/tmp/audit/` 的本地原件。

    python eval/official/run_eval.py                # 跑全部场景，输出 JSON 指标
    python eval/official/run_eval.py --dump-actual  # 打印实测摘要（写 expectations 用）

计划给的实现路径是"**新增场景集**"（§P2.2-4 修改文件："增 --fixture-set 或新增场景集"）：
本 runner 复用 `eval/research/run_eval.py` 的 NetworkGuard / 场景装载 / 报告比较
（不复制实现），只新增官方特有的场景执行（attestation 模式、research_grade 组装）
与 §6.7 指标。`eval/research/**` 的 10 个场景与 expectations 不做任何改动。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = Path(__file__).resolve().parent
OFFICIAL_ROOT = PROJECT_ROOT / "fixtures" / "official"
RAW_DIR = PROJECT_ROOT / ".workbuddy" / "tmp" / "audit"
ATTESTATION_PATH = PROJECT_ROOT / "attestations" / "official.sources.v1.json"
SCENARIOS_DIR = EVAL_ROOT / "scenarios"
EXPECTATIONS_DIR = EVAL_ROOT / "expectations"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard.contracts import ProjectBrief  # noqa: E402
from agentboard.research.assemble import assemble_report  # noqa: E402
from agentboard.research.attestation import load_attestation  # noqa: E402
from agentboard.research.config import POLICY_RESEARCH_GRADE  # noqa: E402
from agentboard.research.errors import AgentBoardResearchError, ReportIntegrityError  # noqa: E402
from agentboard.research.snapshot import (  # noqa: E402
    SnapshotSet,
    load_snapshot_set,
)
from agentboard.research.sources import (  # noqa: E402
    live_reverify,
    load_official_sources,
)

I10 = "I10_NON_PROMOTABLE_SUPPORT"


def _load_research_runner() -> Any:
    """加载 research 评测模块（复用 NetworkGuard / 场景装载 / 报告比较，R6）。"""
    spec = importlib.util.spec_from_file_location(
        "agentboard_eval_research", PROJECT_ROOT / "eval" / "research" / "run_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


research_eval = _load_research_runner()


# ---------------------------------------------------------------- attestation 模式


def _snapshot_set_for(mode: str) -> SnapshotSet:
    """按场景声明的 attestation 模式构建 `SnapshotSet`。

    - `official`：正式工件（`attestations/official.sources.v1.json`）+ 工厂（H6 同源绑定）；
    - `none`：无证明 ⇒ I10 `absent`；
    - `expired`：tmp 变异副本（`verifier_version` +1）⇒ 文件绑定仍通过、I10 `version_expired`；
    - `index_mismatch`：tmp 变异副本（`index_sha256` 改写）⇒ **工厂层**绑定即失败
      （fail-closed：mismatch 在 `load_snapshot_set` 就被拒，不会拖到 I10）。
    """
    if mode == "official":
        return load_snapshot_set(
            fixture_root=OFFICIAL_ROOT, attestation_path=ATTESTATION_PATH,
            policy=POLICY_RESEARCH_GRADE,
        )
    if mode == "none":
        from agentboard.research.snapshot import load_catalog

        return SnapshotSet(
            snapshot_set_id=load_catalog(OFFICIAL_ROOT).snapshot_set,
            policy=POLICY_RESEARCH_GRADE,
        )
    if mode in {"expired", "index_mismatch"}:
        payload = json.loads(ATTESTATION_PATH.read_text(encoding="utf-8"))
        if mode == "expired":
            payload["verifier_version"] = int(payload["verifier_version"]) + 1
        else:
            payload["index_sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as tmp:
            mutated = Path(tmp) / "mutated.sources.v1.json"
            mutated.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            if mode == "expired":
                # 文件层绑定不受 verifier_version 影响 ⇒ 绑定通过，I10 判 version_expired
                return load_snapshot_set(
                    fixture_root=OFFICIAL_ROOT, attestation_path=mutated,
                    policy=POLICY_RESEARCH_GRADE,
                )
            # index_mismatch：工厂直接拒绝（fail-closed）—— 异常向上传给 domain_error 断言
            return load_snapshot_set(
                fixture_root=OFFICIAL_ROOT, attestation_path=mutated,
                policy=POLICY_RESEARCH_GRADE,
            )
    raise SystemExit(f"未知的 attestation 模式：{mode}")


# ---------------------------------------------------------------- 单场景执行


def run_official_scenario(scenario: dict[str, Any], expectation: dict[str, Any]) -> Any:
    """完整跑一个官方场景（含两遍组装），返回结果与差异清单。"""
    scenario_id = scenario["scenario_id"]
    expect = expectation["expect"]
    mode = expect["mode"]
    strict = bool(expect.get("strict", True))
    attestation_mode = scenario.get("attestation", "none")
    failures: list[str] = []

    sources = load_official_sources(OFFICIAL_ROOT)
    registry = sources.registry
    snapshots = dict(sources.snapshots)

    brief_payload = scenario["brief"]
    brief = ProjectBrief(text=brief_payload["text"], source=brief_payload["source"])
    candidate_ids = list(scenario.get("candidates") or sources.candidate_ids())

    report = None
    exception_type = None
    failed_codes: tuple[str, ...] = ()
    report_json = None
    checks_executed = 0
    cards: list[Any] = []

    try:
        snapshot_set = (
            _snapshot_set_for(attestation_mode)
            if scenario.get("policy") == "research_grade"
            else None
        )
        cards = research_eval._make_cards(sources.worker(), candidate_ids)
        report = assemble_report(
            brief, cards, registry, snapshots=snapshots,
            snapshot_set=snapshot_set, strict=strict,
        )
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
            failures.append("期望在装载/工厂阶段失败，实际组装成功")

    if mode == "domain_error":
        expected_exception = expect["exception"]
        if exception_type != expected_exception:
            failures.append(f"期望异常 {expected_exception}，实际 {exception_type}")
    else:
        if report is not None:
            failures.extend(research_eval._compare_report(report, expect))
        if "strict_error_codes" in expect:
            failures.extend(
                _check_strict_codes(brief, cards, registry, snapshots, attestation_mode, expect)
            )
        if "i10_detail_contains" in expect:
            detail = report.integrity.detail_of(I10)
            for needle in expect["i10_detail_contains"]:
                if needle not in detail:
                    failures.append(f"I10 明细必须包含 {needle!r}，实际：{detail}")

    return {
        "scenario_id": scenario_id,
        "passed": not failures,
        "failures": failures,
        "report": report,
        "report_json": report_json,
        "exception_type": exception_type,
        "failed_codes": failed_codes,
        "checks_executed": checks_executed,
        "attestation_mode": attestation_mode,
        "sources": sources,
    }


def _check_strict_codes(
    brief: Any,
    cards: list[Any],
    registry: Any,
    snapshots: dict[str, Any],
    attestation_mode: str,
    expect: dict[str, Any],
) -> list[str]:
    """缺陷场景必须断言**明确的失败码**（与 research 评测同一纪律）。"""
    expected_codes = list(expect["strict_error_codes"])
    try:
        snapshot_set = _snapshot_set_for(attestation_mode)
        assemble_report(brief, cards, registry, snapshots=snapshots,
                        snapshot_set=snapshot_set, strict=True)
    except ReportIntegrityError as exc:
        actual = list(exc.failed_codes)
        if actual != expected_codes:
            return [f"strict=True 的失败码期望 {expected_codes}，实际 {actual}"]
        return []
    except AgentBoardResearchError as exc:  # pragma: no cover - 兜底
        return [f"strict=True 期望 ReportIntegrityError，实际 {type(exc).__name__}: {exc}"]
    return ["strict=True 期望抛出 ReportIntegrityError，实际组装成功"]


# ---------------------------------------------------------------- 指标（§6.7）


def summarize_official(outcomes: list[dict[str, Any]], network_calls: int) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome["passed"])

    report_outcomes = [outcome for outcome in outcomes if outcome["report"] is not None]
    report_ids = {outcome["report"].report_id for outcome in report_outcomes}
    report_id_stable = len(report_ids) == 1

    graded = [
        outcome
        for outcome in report_outcomes
        if outcome["attestation_mode"] in {"official", "expired"}
    ]
    promoted_refs = 0
    fixture_refs = 0
    authored_backed = 0
    resolved_total = 0
    resolved_ok = 0
    trusted_refs = 0
    all_refs = 0
    for outcome in graded:
        sources: Any = outcome["sources"]
        report = outcome["report"]
        for finding in report.findings:
            if not finding.is_supported:
                continue
            snapshot = sources.snapshots.get(finding.candidate_id)
            if snapshot is not None and snapshot.source_kind == "authored_summary":
                authored_backed += 1
        refs = research_eval._iter_refs(report)
        for ref in refs:
            all_refs += 1
            if ref.source_type == "fixture_snapshot":
                trusted_refs += 1
        for finding in report.findings:
            for ref in finding.evidence_refs:
                if ref.source_type != "fixture_snapshot":
                    continue
                fixture_refs += 1
                candidate_id = ref.source_id.removeprefix("fixture:")
                snapshot = sources.snapshots.get(candidate_id)
                if snapshot is not None and snapshot.source_kind == "official_documentation_excerpt":
                    promoted_refs += 1
                resolved_total += 1
                try:
                    sources.resolve_evidence(ref)
                except Exception:  # noqa: BLE001 - 回溯失败即计入不可解析
                    continue
                resolved_ok += 1

    attestation_present = ATTESTATION_PATH.is_file()
    overall_result = "absent"
    self_binding_ok = False
    if attestation_present:
        attestation = load_attestation(ATTESTATION_PATH, fixture_root=OFFICIAL_ROOT)
        overall_result = attestation.overall_result
        self_binding_ok = attestation.binding_reason == ""

    absent_outcome = next(
        (outcome for outcome in outcomes if outcome["attestation_mode"] == "none"), None
    )
    unavailable_reason = "absent"
    if absent_outcome is not None:
        report = absent_outcome["report"]
        if report is not None and I10 in report.integrity.failed_codes:
            detail = report.integrity.detail_of(I10)
            for reason in ("version_expired", "mismatch", "incomplete", "unbound", "absent"):
                if reason in detail:
                    unavailable_reason = reason
                    break

    anchor_ok, anchor_problems, anchor_missing = (True, [], [])
    if RAW_DIR.is_dir():
        anchor_ok, anchor_problems, anchor_missing = live_reverify(
            report_outcomes[0]["sources"], RAW_DIR
        ) if report_outcomes else (True, [], [])
    else:
        anchor_missing = [f"raw_dir 缺失：{RAW_DIR}"]

    anchor_total = len({doc.source_id for doc in report_outcomes[0]["sources"].documents()}) if report_outcomes else 0
    anchor_ok_count = anchor_total - len(anchor_missing) - len(
        {p.split(":")[0] for p in anchor_problems}
    )

    return {
        "official_scenarios_total": total,
        "official_scenarios_passed": passed,
        "official_report_id_stable": report_id_stable,
        "official_report_ids": sorted(report_ids),
        "promotable_evidence_rate": (promoted_refs / fixture_refs) if fixture_refs else 0.0,
        "authored_summary_in_research_grade": authored_backed,
        "attestation_present": attestation_present,
        "attestation_overall_result": overall_result,
        "attestation_self_binding_ok": self_binding_ok,
        "attestation_unavailable_reason": unavailable_reason,
        "source_anchor_verified_rate": (anchor_ok_count / anchor_total) if anchor_total else 0.0,
        "source_anchor_problem_count": len(anchor_problems),
        "source_anchor_missing_count": len(anchor_missing),
        "evidence_resolved_to_document_rate": (
            resolved_ok / resolved_total if resolved_total else 0.0
        ),
        "official_evidence_trusted_rate": (trusted_refs / all_refs) if all_refs else 0.0,
        "network_calls": network_calls,
        "llm_calls": len(research_eval.forbidden_modules_loaded()),
        "detail": {
            "source_anchor_problems": anchor_problems,
            "source_anchor_missing": anchor_missing,
            "anchor_overall_ok": anchor_ok,
            "scenario_failures": {
                outcome["scenario_id"]: outcome["failures"]
                for outcome in outcomes
                if outcome["failures"]
            },
        },
    }


OFFICIAL_METRIC_ASSERTIONS: tuple[tuple[str, Any], ...] = (
    ("official_scenarios_passed", None),
    ("official_report_id_stable", lambda value: value is True),
    ("promotable_evidence_rate", lambda value: value == 1.0),
    ("authored_summary_in_research_grade", lambda value: value == 0),
    ("attestation_present", lambda value: value is True),
    ("attestation_overall_result", lambda value: value == "passed"),
    ("attestation_self_binding_ok", lambda value: value is True),
    ("attestation_unavailable_reason", lambda value: value == "absent"),
    ("source_anchor_problem_count", lambda value: value == 0),
    ("evidence_resolved_to_document_rate", lambda value: value == 1.0),
    ("official_evidence_trusted_rate", lambda value: value == 1.0),
    ("network_calls", lambda value: value == 0),
    ("llm_calls", lambda value: value == 0),
)


def check_official_metrics(metrics: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if metrics["official_scenarios_passed"] != metrics["official_scenarios_total"]:
        problems.append(
            f"official_scenarios_passed ({metrics['official_scenarios_passed']}) != "
            f"official_scenarios_total ({metrics['official_scenarios_total']})"
        )
    for name, predicate in OFFICIAL_METRIC_ASSERTIONS:
        if predicate is None:
            continue
        value = metrics[name]
        if not predicate(value):
            problems.append(f"指标未达标：{name} = {value!r}")
    return problems


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="官方快照集固定场景评测（离线，P2.2-4）")
    parser.add_argument("--dump-actual", action="store_true", help="打印每个场景的实测摘要")
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
            run_official_scenario(scenarios[key], expectations[key]) for key in ordered
        ]
        repeats = [
            run_official_scenario(scenarios[key], expectations[key]) for key in ordered
        ]
        # 确定性：两遍的 report_json / 异常 / 失败码必须一致（与 research 评测同一纪律）
        for first, second in zip(outcomes, repeats, strict=True):
            if (
                first["report_json"] != second["report_json"]
                or first["exception_type"] != second["exception_type"]
                or first["failed_codes"] != second["failed_codes"]
            ):
                first["failures"].append("两遍运行结果不一致（确定性破坏）")
                first["passed"] = False

    metrics = summarize_official(outcomes, guard.calls)

    if args.dump_actual:
        for outcome in outcomes:
            report = outcome["report"]
            print(f"--- {outcome['scenario_id']} (attestation={outcome['attestation_mode']}) ---")
            print(f"  passed={outcome['passed']} exception={outcome['exception_type']}")
            if outcome["failures"]:
                print(f"  failures={outcome['failures']}")
            if report is not None:
                print(f"  report_id={report.report_id}")
                print(f"  integrity_passed={report.integrity.passed} failed={list(report.integrity.failed_codes)}")
                print(
                    "  ranking="
                    + json.dumps(
                        [
                            {"rank": rec.rank, "candidate_id": rec.candidate_id,
                             "score": round(rec.score, 6)}
                            for rec in report.recommendations
                        ],
                        ensure_ascii=False,
                    )
                )
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))

    problems = check_official_metrics(metrics)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

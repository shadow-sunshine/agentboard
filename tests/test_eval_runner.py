"""评测场景集与 runner 的结构测试（P1A-4）。

完整评测由 `python eval/research/run_eval.py` 作为门禁命令执行；这里只覆盖
**场景/期望文件的自洽性**与 runner 的纯逻辑（地址判定、指标断言、单场景执行），
避免在 pytest 里重复跑完整评测。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_EVAL_PATH = PROJECT_ROOT / "eval" / "research" / "run_eval.py"

REQUIRED_SCENARIO_FIELDS = ("scenario_id", "title", "note", "brief", "mutation")
REQUIRED_EXPECT_FIELDS = ("mode",)
REQUIRED_SCENARIO_IDS = frozenset(
    {
        "normal_full_report",
        "missing_dimension_lowers_rank",
        "no_trusted_evidence",
        "unknown_dimension_rejected",
        "candidate_missing_from_board",
        "score_tie_break",
        "legacy_evidence_only",
        "repeat_run_identical",
    }
)


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agentboard_run_eval", RUN_EVAL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记到 sys.modules：run_eval 里有 dataclass，而 dataclasses 在解析字符串
    # 注解（如 ClassVar）时会回查 sys.modules[cls.__module__]，未登记会 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


@pytest.fixture(scope="module")
def scenarios(runner: ModuleType) -> dict:
    return runner._load_json_dir(runner.SCENARIOS_DIR)


@pytest.fixture(scope="module")
def expectations(runner: ModuleType) -> dict:
    return runner._load_json_dir(runner.EXPECTATIONS_DIR)


# ---- 场景集结构 ----


def test_scenario_count_meets_minimum(scenarios: dict) -> None:
    assert len(scenarios) >= 8


def test_required_scenarios_are_present(scenarios: dict) -> None:
    assert REQUIRED_SCENARIO_IDS <= set(scenarios)


def test_scenarios_have_required_fields(scenarios: dict) -> None:
    for scenario_id, scenario in scenarios.items():
        for field in REQUIRED_SCENARIO_FIELDS:
            assert field in scenario, f"{scenario_id} 缺少 {field}"
        assert scenario["scenario_id"] == scenario_id
        assert scenario["brief"]["text"].strip()
        assert scenario["brief"]["source"].strip()
        assert scenario["mutation"]["kind"]


def test_every_scenario_has_matching_expectation(scenarios: dict, expectations: dict) -> None:
    assert set(scenarios) == set(expectations)


def test_expectations_have_valid_mode(expectations: dict) -> None:
    for scenario_id, expectation in expectations.items():
        expect = expectation["expect"]
        for field in REQUIRED_EXPECT_FIELDS:
            assert field in expect, f"{scenario_id} 的 expect 缺少 {field}"
        assert expect["mode"] in {"report", "domain_error"}
        if expect["mode"] == "domain_error":
            assert expect["exception"]
            assert "integrity_passed" not in expect


def test_report_scenarios_pin_report_id_and_ranking(expectations: dict) -> None:
    for scenario_id, expectation in expectations.items():
        expect = expectation["expect"]
        if expect["mode"] != "report":
            continue
        assert "report_id" in expect, f"{scenario_id} 未固定 report_id"
        assert "ranking" in expect, f"{scenario_id} 未固定 ranking"
        assert expect["ranking"][0]["rank"] == 1


def test_failing_scenarios_pin_explicit_failure_codes(expectations: dict) -> None:
    """缺陷场景必须断言**明确失败码**，不能只判断"抛了个异常"。"""
    for scenario_id, expectation in expectations.items():
        expect = expectation["expect"]
        if expect["mode"] != "report" or expect.get("integrity_passed", True):
            continue
        assert expect.get("failed_codes"), f"{scenario_id} 没有断言 failed_codes"
        assert expect.get("strict_error_codes"), f"{scenario_id} 没有断言 strict=True 的失败码"
        assert expect["strict_error_codes"] == expect["failed_codes"]
        assert expect["strict"] is False, f"{scenario_id} 需要用 strict=False 才能拿到失败报告"


def test_scenario_ids_are_unique_and_filenames_match(scenarios: dict) -> None:
    files = sorted((RUN_EVAL_PATH.parent / "scenarios").glob("*.json"))
    assert len(files) == len(scenarios)
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["scenario_id"] in scenarios


# ---- runner 纯逻辑 ----


def test_is_local_address_classifies_loopback(runner: ModuleType) -> None:
    assert runner.is_local_address(("127.0.0.1", 8080)) is True
    assert runner.is_local_address(("127.0.0.5", 80)) is True
    assert runner.is_local_address(("localhost", 80)) is True
    assert runner.is_local_address(("::1", 80, 0, 0)) is True
    assert runner.is_local_address("/tmp/some.sock") is True
    assert runner.is_local_address(("example.com", 443)) is False
    assert runner.is_local_address(("93.184.216.34", 443)) is False


def test_network_guard_counts_only_remote(runner: ModuleType) -> None:
    import socket

    with runner.NetworkGuard() as guard:
        local = socket.socket()
        local.bind(("127.0.0.1", 0))
        local.listen(1)
        probe = socket.socket()
        probe.connect(local.getsockname())
        probe.close()
        local.close()

    assert guard.all_connect_attempts >= 1
    assert guard.calls == 0


def test_check_metrics_flags_violations(runner: ModuleType) -> None:
    healthy = {
        "scenarios_total": 10,
        "scenarios_passed": 10,
        "determinism_repeat_rate": 1.0,
        "evidence_resolvable_rate": 1.0,
        "fixture_evidence_rate": 1.0,
        "silent_missing_dimensions": 0,
        "integrity_checks_executed": 63,
        "network_calls": 0,
        "llm_calls": 0,
    }
    assert runner.check_metrics(healthy) == []

    for key, bad_value in (
        ("scenarios_passed", 9),
        ("determinism_repeat_rate", 0.9),
        ("evidence_resolvable_rate", 0.5),
        ("fixture_evidence_rate", 0.5),
        ("silent_missing_dimensions", 1),
        ("integrity_checks_executed", 0),
        ("network_calls", 3),
        ("llm_calls", 1),
    ):
        broken = dict(healthy)
        broken[key] = bad_value
        assert runner.check_metrics(broken), f"{key} 未达标时应当被拦下"


# ---- 端到端（只挑两类代表场景，完整评测由门禁命令覆盖） ----


def test_normal_scenario_passes_and_is_deterministic(runner: ModuleType, scenarios, expectations) -> None:
    scenario = scenarios["normal_full_report"]
    expectation = expectations["normal_full_report"]
    first = runner.run_scenario(scenario, expectation)
    second = runner.run_scenario(scenario, expectation)
    assert first.passed is True, first.failures
    assert first.report_json == second.report_json
    assert first.report is not None
    assert first.report.integrity.passed is True
    assert len(first.report.integrity.checks) == 9


def test_failure_scenario_reports_explicit_codes(runner: ModuleType, scenarios, expectations) -> None:
    scenario = scenarios["legacy_evidence_only"]
    expectation = expectations["legacy_evidence_only"]
    outcome = runner.run_scenario(scenario, expectation)
    assert outcome.passed is True, outcome.failures
    assert outcome.report is not None
    assert outcome.report.integrity.passed is False
    assert list(outcome.report.integrity.failed_codes) == ["I3", "I4"]


def test_domain_error_scenario(runner: ModuleType, scenarios, expectations) -> None:
    scenario = scenarios["unknown_dimension_rejected"]
    expectation = expectations["unknown_dimension_rejected"]
    outcome = runner.run_scenario(scenario, expectation)
    assert outcome.passed is True, outcome.failures
    assert outcome.exception_type == "UnknownDimensionError"
    assert outcome.report is None


def test_run_scenario_detects_wrong_expectation(runner: ModuleType, scenarios, expectations) -> None:
    """期望写错时必须被抓住 —— 否则评测只是个摆设。"""
    scenario = scenarios["normal_full_report"]
    tampered = json.loads(json.dumps(expectations["normal_full_report"]))
    tampered["expect"]["report_id"] = "rp_0000000000000000"
    outcome = runner.run_scenario(scenario, tampered)
    assert outcome.passed is False
    assert any("report_id" in item for item in outcome.failures)


def test_run_scenario_detects_wrong_failure_codes(runner: ModuleType, scenarios, expectations) -> None:
    scenario = scenarios["no_trusted_evidence"]
    tampered = json.loads(json.dumps(expectations["no_trusted_evidence"]))
    tampered["expect"]["failed_codes"] = ["I3"]
    tampered["expect"]["strict_error_codes"] = ["I3"]
    outcome = runner.run_scenario(scenario, tampered)
    assert outcome.passed is False
    assert any("failed_codes" in item or "失败码" in item for item in outcome.failures)

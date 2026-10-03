"""report_html 测试：确定性、只读投影、XSS 边界。

核心断言（防止这层变成"偷偷改数据的渲染器"）：
- 同输入两次渲染逐字节相同（无时间戳/随机 id）；
- 结论数值与来源 commit 原样出现在页面上（不改写、不重算）；
- 恶意 claim/摘录不能突破 HTML 转义（来源正文是不可信数据）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentboard.report_html import render_run_dir_html, render_run_html

REPORT: dict[str, Any] = {
    "mode": "live",
    "run_id": "ma_testrun0001",
    "outcome": "completed",
    "selection_reports": [
        {
            "report_id": "rp_testreport001",
            "recommendations": [
                {
                    "candidate_id": "pgvector",
                    "score": 0.5,
                    "rationale": "综合效用 0.500（排名 1）",
                }
            ],
            "integrity": {
                "passed": True,
                "failed_codes": [],
                "checks": [
                    {"code": "I1", "passed": True, "detail": "2 个 evidence_id 各自对应唯一内容"},
                    {"code": "I2", "passed": True, "detail": "2 条索引项全部可解析"},
                ],
            },
            "findings": [
                {
                    "candidate_id": "pgvector",
                    "dimension_id": "deployment_complexity",
                    "value": "medium",
                    "claim": "需要源码编译或包管理器安装。",
                    "evidence_refs": [
                        {
                            "evidence_id": "ev_abc123",
                            "locator": "pgvector:/sections/0/text",
                            "digest": "f3c4b25059733644e79adad10237143",
                            "captured_at": "2026-09-26",
                            "excerpt": "pgvector installation requires compiling from source.",
                        }
                    ],
                },
                {
                    "candidate_id": "pgvector",
                    "dimension_id": "query_latency",
                    "value": "",
                    "claim": "",
                    "evidence_refs": [],
                },
            ],
            "risks": [
                {
                    "severity": "high",
                    "risk_id": "rk_0001",
                    "rule_id": "R_UNSUPPORTED_WEIGHTED",
                    "candidate_id": "pgvector",
                    "dimension_id": "query_latency",
                    "description": "query_latency 无资料依据",
                },
                {"severity": "medium", "candidate_id": "pgvector", "description": "分差仅 0.05，排名不稳健"},
            ],
            "coverage": {
                "candidates": [
                    {
                        "candidate_id": "pgvector",
                        "missing_dimensions": ["query_latency"],
                        "supported_dimensions": 5,
                        "total_dimensions": 6,
                    }
                ]
            },
        }
    ],
}

RECORD: dict[str, Any] = {
    "run_id": "ma_testrun0001",
    "outcome": "completed",
    "decision_identity": "orchestrator.v1:cli.v1:structured-plan",
    "verdicts": {"verify_report": "passed"},
    "events": [
        {"event": "task_done", "task_id": "synthesize", "data": {}},
        {"event": "board_created", "task_id": "", "data": {}},
        {"event": "task_started", "task_id": "research_candidates", "data": {}},
        {"event": "verifier_passed", "task_id": "verify_report", "data": {"verdict": "passed"}},
        {"event": "task_retry", "task_id": "research_candidates", "data": {"reason": "SourceHTTPError: 503"}},
    ],
    "board_state": {
        "cards": {
            "research_candidates": {"status": "done", "attempts": 2, "error": ""},
            "verify_report": {"status": "done", "attempts": 1, "error": ""},
            "synthesize": {"status": "failed", "attempts": 3, "error": "board_invariant_violated: 卡版本过期"},
        }
    },
}

SOURCES: dict[str, Any] = {
    "source_audit": [
        {
            "candidate_id": "pgvector",
            "repo": "pgvector/pgvector",
            "path": "README.md",
            "commit": "a" * 40,
            "source_url": "https://github.com/pgvector/pgvector/blob/" + "a" * 40 + "/README.md",
            "content_sha256": "b" * 64,
        }
    ]
}


def test_render_is_deterministic() -> None:
    """同输入两次渲染逐字节相同（页面里没有时间戳/随机 id）。"""
    first = render_run_html(REPORT, RECORD, SOURCES)
    second = render_run_html(REPORT, RECORD, SOURCES)
    assert first == second


def test_outcome_and_verdict_are_visible() -> None:
    """运行头部必须暴露结局与 verifier 判定（可信性不能藏在 JSON 里）。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "ma_testrun0001" in html
    assert "completed" in html
    assert "verify_report" in html and "passed" in html
    assert "orchestrator.v1:cli.v1:structured-plan" in html


def test_events_are_ordered_by_pipeline_not_by_arrival() -> None:
    """事件按流水线顺序渲染（建板在前、合成在后），不按运行期到达顺序。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    positions = [
        html.index("board_created"),
        html.index("task_started"),
        html.index("task_retry"),
        html.index("task_done"),
        html.index("verifier_passed"),
    ]
    assert positions == sorted(positions)


def test_retry_reason_is_surfaced() -> None:
    """失败/重试原因必须出现在页面上（可观测性是核心卖点）。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "SourceHTTPError" in html


def test_board_card_failure_reason_is_shown() -> None:
    """任务卡失败原因可见（含 invariant 违例文本）。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "board_invariant_violated" in html
    assert "尝试 3 次" in html


def test_evidence_excerpt_and_commit_are_preserved() -> None:
    """证据回链：摘录、locator、digest、固定 commit URL 原样出现。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "pgvector installation requires compiling from source." in html
    assert "pgvector:/sections/0/text" in html
    assert "f3c4b25059733644e79adad10237143a"[:16] in html  # digest 前 16 字符
    assert "a" * 40 in html  # 固定 commit 全文
    assert "b" * 16 in html  # 内容 sha256 前缀


def test_long_excerpt_is_truncated_explicitly() -> None:
    """超长原文显式截断并标注（不静默丢内容，也不撑爆页面）。"""
    long_report = json.loads(json.dumps(REPORT))
    long_report["selection_reports"][0]["findings"][0]["evidence_refs"][0]["excerpt"] = (
        "X" * 5000
    )
    html = render_run_html(long_report, RECORD, SOURCES, max_excerpt_chars=100)
    assert "X" * 100 in html
    assert "X" * 200 not in html
    assert "截断展示" in html


def test_unsupported_finding_is_labelled_not_hidden() -> None:
    """无证据的维度显式标注 unsupported（材料不足不推断补齐）。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "unsupported" in html
    assert "query_latency" in html


def test_risks_and_coverage_gaps_are_rendered() -> None:
    """风险用真实字段（description / candidate_id），覆盖用 candidates 列表形态。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "无资料依据" in html
    assert "排名不稳健" in html
    assert "R_UNSUPPORTED_WEIGHTED" not in html  # rule_id 不在页面上（保持精简）
    assert "query_latency" in html  # 覆盖缺口显式列出


def test_coverage_gap_is_never_silently_dropped() -> None:
    """真实产物形态（candidates 列表）必须渲染出缺口 —— 曾经的静默丢失点。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "显式 unsupported" in html
    assert "query_latency" in html


def test_integrity_checks_are_tabulated() -> None:
    """I1-I9 逐项呈现，不能只给一个"通过"结论。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "I1" in html and "I2" in html
    assert "2 个 evidence_id 各自对应唯一内容" in html


def test_injection_in_claim_cannot_break_out_of_html() -> None:
    """模型 claim 是不可信输出：注入标签必须被转义。"""
    hostile = json.loads(json.dumps(REPORT))
    hostile["selection_reports"][0]["findings"][0]["claim"] = (
        "</summary><script>alert('xss')</script>"
    )
    html = render_run_html(hostile, RECORD, SOURCES)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_injection_in_source_excerpt_cannot_break_out_of_html() -> None:
    """来源正文是不可信数据：摘录里的 HTML 必须转义。"""
    hostile = json.loads(json.dumps(REPORT))
    hostile["selection_reports"][0]["findings"][0]["evidence_refs"][0]["excerpt"] = (
        "<img src=x onerror=alert(1)>"
    )
    html = render_run_html(hostile, RECORD, SOURCES)
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


def test_injection_in_source_url_cannot_add_attributes() -> None:
    """来源 URL 里的引号必须转义（防 attribute 逃逸）。"""
    hostile = json.loads(json.dumps(SOURCES))
    hostile["source_audit"][0]["source_url"] = '" onmouseover="alert(1)'
    html = render_run_html(REPORT, RECORD, hostile)
    assert 'onmouseover="alert(1)' not in html
    assert "&quot;" in html


def test_trust_boundary_note_is_always_present() -> None:
    """页面必须保留"不自动升格 research_grade"的显式声明。"""
    html = render_run_html(REPORT, RECORD, SOURCES)
    assert "authored_summary" in html
    assert "research_grade" in html


def test_empty_run_does_not_crash() -> None:
    """空产物（失败运行）也必须渲染出可读页面，而不是抛异常。"""
    html = render_run_html({"outcome": "failed", "selection_reports": []}, {}, {})
    assert "failed" in html
    assert "AgentBoard" in html


def test_render_run_dir_reads_artifacts(tmp_path: Path) -> None:
    """从运行目录读三份产物再渲染。"""
    (tmp_path / "report.json").write_text(
        json.dumps(REPORT, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "run_record.json").write_text(
        json.dumps(RECORD, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "sources.json").write_text(
        json.dumps(SOURCES, ensure_ascii=False), encoding="utf-8"
    )
    html = render_run_dir_html(tmp_path)
    assert "ma_testrun0001" in html
    assert "pgvector installation requires compiling" in html


def test_render_run_dir_without_record_still_renders(tmp_path: Path) -> None:
    """replay 模式没有 run_record.json —— 缺文件按空处理，不编造。"""
    (tmp_path / "report.json").write_text(
        json.dumps(REPORT, ensure_ascii=False), encoding="utf-8"
    )
    html = render_run_dir_html(tmp_path)
    assert "rp_testreport001" in html
    assert "无验证结论" in html


def test_render_run_dir_requires_report_json(tmp_path: Path) -> None:
    """缺 report.json 必须显式失败，不返回半成品页面。"""
    with pytest.raises(ValueError, match="report.json"):
        render_run_dir_html(tmp_path)


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_excerpt_limit_is_rejected(bad: Any) -> None:
    """非法截断上限必须被拒（不静默降级成默认值）。"""
    with pytest.raises(ValueError):
        render_run_html(REPORT, RECORD, SOURCES, max_excerpt_chars=bad)


@pytest.mark.parametrize("bad", ["", None, 1.5, True])
def test_non_integer_excerpt_limit_is_rejected(bad: Any) -> None:
    """非整数上限必须显式 TypeError（不靠 <= 抛一个含义不清的错）。"""
    with pytest.raises(TypeError):
        render_run_html(REPORT, RECORD, SOURCES, max_excerpt_chars=bad)

"""CLI 入口测试（P6）：replay/offline 端到端产物、live 缺配置显式退出。

全部离线：live 的缺失配置路径在发起任何网络请求之前终止。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentboard import cli


def _run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> int:
    return cli.main(list(argv))


def test_replay_mode_writes_report_and_run_record(tmp_path: Path, capsys) -> None:
    out = tmp_path / "replay"
    exit_code = _run_cli(
        capsys, "replay", "--goal", "为检索服务做技术选型", "--out", str(out),
    )
    assert exit_code == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["report_id"].startswith("rp_")
    assert report["integrity"]["passed"] is True
    markdown = (out / "report.md").read_text(encoding="utf-8")
    assert "# 研究报告" in markdown and report["report_id"] in markdown
    record = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert record["mode"] == "replay"
    # 确定性：同一输入重跑 report_id 不变
    out2 = tmp_path / "replay2"
    assert _run_cli(capsys, "replay", "--goal", "为检索服务做技术选型", "--out", str(out2)) == 0
    report2 = json.loads((out2 / "report.json").read_text(encoding="utf-8"))
    assert report2["report_id"] == report["report_id"]


def test_offline_mode_writes_orchestration_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "offline"
    exit_code = cli.main([
        "offline", "--goal", "为检索服务做技术选型（脚本演示）",
        "--candidates", "pgvector,qdrant_local", "--out", str(out),
    ])
    assert exit_code == 0
    record = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert record["outcome"] == "completed"
    assert record["run_id"].startswith("ma_")
    assert record["verdicts"]["verify_report"] == "passed"
    markdown = (out / "report.md").read_text(encoding="utf-8")
    assert "offline/scripted 模式" in markdown  # 明确标注脚本替身，不冒充真实


def test_live_without_model_config_fails_before_any_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺模型端点/名/凭据 ⇒ 一次性列出缺失项、退出码 2、零网络请求。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    out = tmp_path / "live"
    plan = tmp_path / "sources.json"
    plan.write_text(
        json.dumps({"candidates": {"pgvector": [{"repo": "pgvector/pgvector", "path": "README.md"}]}}),
        encoding="utf-8",
    )
    exit_code = cli.main([
        "live", "--goal", "g", "--candidates", "pgvector",
        "--sources", str(plan), "--out", str(out),
        "--model-preset", "dashscope", "--model-name", "qwen-plus",
    ])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "DASHSCOPE_API_KEY" in captured.err
    assert not out.exists() or not (out / "sources.json").exists()


def test_live_without_sources_file_fails_clean(tmp_path: Path) -> None:
    with pytest.raises((FileNotFoundError, json.JSONDecodeError)):
        cli.main([
            "live", "--goal", "g", "--candidates", "pgvector",
            "--sources", str(tmp_path / "missing.json"), "--out", str(tmp_path / "live"),
            "--model-preset", "dashscope", "--model-name", "qwen-plus",
        ])



def test_offline_mode_defaults_to_fixture_candidates_and_exports_full_report(tmp_path: Path) -> None:
    out = tmp_path / "offline-default"
    assert cli.main(["offline", "--goal", "默认候选", "--out", str(out)]) == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["selection_reports"]
    selection = report["selection_reports"][0]
    assert selection["report_id"].startswith("rp_")
    assert len(selection["findings"]) > 0
    markdown = (out / "report.md").read_text(encoding="utf-8")
    assert "## 完整研究报告 1" in markdown

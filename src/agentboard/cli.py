"""AgentBoard 命令行入口（P6）：python -m agentboard <mode> …

三种模式，边界显式：

- **replay**：本地冻结快照（fixtures/research）→ 确定性报告（P1A 路径）；
  零网络零凭据，输出可逐字节复现；
- **offline**：P5 编排闭环 + 离线 runtime researcher（脚本检索 + 脚本决策
  transport）——**明确标注 scripted**，用于演示流程与离线验收，不代表真实检索
  或真实模型能力；
- **live**：真实研究接线——`PinnedSourceProvider`（显式来源计划，GitHub
  contents API 固定 commit 原文）+ 真实模型决策 transport（OpenAI 兼容，
  如 DashScope preset）。缺端点/模型名/凭据 ⇒ 一次性列出缺失项并以退出码 2
  终止（**发起任何网络请求之前**）；远端失败/预算耗尽 ⇒ 显式失败，
  绝不静默回退到脚本或 Mock。

所有模式把产物写进 `--out` 运行目录：report.json / report.md / run_record.json
（live 另有 sources.json 来源审计）。运行数据只进指定目录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .contracts import ProjectBrief
from .research.assemble import assemble_report
from .research.dimensions import load_dimension_registry
from .research.fixtures import FixtureResearchWorker
from .research.snapshot import load_catalog, load_snapshots
from .util import thaw_payload

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES = PROJECT_ROOT / "fixtures" / "research"
DEFAULT_DIMENSIONS = DEFAULT_FIXTURES / "dimensions" / "dimensions.v1.json"

#: 模型 preset：端点固定为官方兼容模式地址；key 从指定环境变量读（不回显）。
MODEL_PRESETS: dict[str, dict[str, str]] = {
    "dashscope": {
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "api_key_env": "DASHSCOPE_API_KEY",
        "host": "dashscope.aliyuncs.com",
    },
}

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUN_FAILED = 1


# ---------------------------------------------------------------- 渲染


def render_report_markdown(report: dict[str, Any]) -> str:
    """把 SelectionReport dict 渲染成 Markdown（确定性；零时间字段）。"""
    lines: list[str] = [
        "# 研究报告",
        "",
        f"- report_id: `{report.get('report_id', '')}`",
        f"- 目标: {report.get('brief', {}).get('text', '')}",
        f"- 来源: `{report.get('brief', {}).get('source', '')}`",
        "",
        "## 推荐（打分排名）",
        "",
        "| 排名 | 候选 | 得分 | 置信依据 |",
        "|---|---|---|---|",
    ]
    for rec in report.get("recommendations", []):
        lines.append(
            f"| {rec['rank']} | {rec['candidate_id']} | {rec['score']:.4f} "
            f"| {rec.get('rationale', '')} |"
        )
    lines += ["", "## 完整性校验", "", "| 检查 | 结果 | 说明 |", "|---|---|---|"]
    integrity = report.get("integrity", {})
    for check in integrity.get("checks", []):
        lines.append(
            f"| {check['code']} | {'✅ 通过' if check['passed'] else '❌ 未通过'} "
            f"| {check['detail']} |"
        )
    lines.append(
        f"\n**完整性结论：{'通过' if integrity.get('passed') else '未通过'}"
        f"（failed_codes: {integrity.get('failed_codes', [])}）**"
    )
    findings = report.get("findings", [])
    lines += ["", "## 发现", ""]
    supported = [f for f in findings if f.get("status") == "supported"]
    unsupported = [f for f in findings if f.get("status") != "supported"]
    for finding in supported:
        refs = ", ".join(
            f"`{ref.get('source_id', '')}:{ref.get('locator', '')}`"
            for ref in finding.get("evidence_refs", [])
        )
        lines.append(
            f"- **{finding.get('candidate_id')} / {finding.get('dimension_id')} = "
            f"{finding.get('value')}** — {finding.get('claim', '')}（证据: {refs}）"
        )
    if unsupported:
        lines.append(f"- 未支持维度 {len(unsupported)} 条（显式 unsupported，不推断补齐）")
    lines += ["", "## 风险", ""]
    for risk in report.get("risks", []):
        lines.append(f"- [{risk.get('severity', '')}] {risk.get('description', '')}")
    coverage = report.get("coverage", {}).get("candidates", [])
    if coverage:
        lines += ["", "## 覆盖", ""]
        for item in coverage:
            lines.append(
                f"- {item['candidate_id']}: {item['supported_dimensions']}/"
                f"{item['total_dimensions']} 维度有结论"
            )
    lines.append("")
    return "\n".join(lines)


def _verified_selection_reports(result: Any) -> list[dict[str, Any]]:
    """只提取 verifier 明确通过且 report_id 对得上的完整研究报告。"""
    if not result.verdicts:
        return []
    passed_verifiers = {
        task_id for task_id, verdict in result.verdicts.items() if verdict == "passed"
    }
    reports: list[dict[str, Any]] = []
    cards = result.board_state.get("cards", {})
    for verify_task_id in sorted(passed_verifiers):
        verify_card = cards.get(verify_task_id) or {}
        verify_result = verify_card.get("result") or {}
        verify_payload = thaw_payload(verify_result.get("structured_payload") or {})
        verified_id = verify_payload.get("verified_report_id")
        if not isinstance(verified_id, str) or not verified_id:
            continue
        for card in cards.values():
            research_result = card.get("result") or {}
            payload = thaw_payload(research_result.get("structured_payload") or {})
            if payload.get("schema") != "live_research.report.v1":
                continue
            report_json = payload.get("report_json")
            if not isinstance(report_json, str):
                continue
            try:
                report = json.loads(report_json)
            except json.JSONDecodeError:
                continue
            if isinstance(report, dict) and report.get("report_id") == verified_id:
                reports.append(report)
                break
    return reports


def render_orchestration_markdown(result: Any, *, scripted: bool) -> str:
    """把 P5 编排结果渲染成 Markdown（含未验证材料标注）。"""
    lines = [
        "# 多 Agent 编排报告",
        "",
        f"- run_id: `{result.run_id}`",
        f"- 结局: **{result.outcome}**" + (f"（原因: {result.failure_reason}）" if result.failure_reason else ""),
        f"- 决策机制: `{result.decision_identity}`",
        f"- 验证结论: {result.verdicts or '{}'}",
        "",
    ]
    if result.final_report is not None:
        lines += [
            "## 最终报告（仅汇总验证通过的结论）",
            "",
            f"- final_report_id: `{result.final_report.get('final_report_id', '')}`",
            "",
            result.final_report.get("synthesis", ""),
        ]
        for index, report in enumerate(_verified_selection_reports(result), start=1):
            lines += ["", f"## 完整研究报告 {index}", "", render_report_markdown(report)]
    else:
        lines += ["## 最终报告", "", "未产出（运行未完成或被 verifier 拦截）。"]
    notes = []
    for event in result.events:
        if event["event"] == "verifier_passed":
            notes.extend(event.get("data", {}).get("notes", []) or [])
    if notes:
        lines += ["", "## 未验证材料标注", ""]
        lines += [f"- {note}" for note in notes]
    if scripted:
        lines += [
            "",
            "> ⚠️ 本次运行为 **offline/scripted 模式**：检索与决策均为离线脚本替身，",
            "> 不代表真实检索或真实模型能力。",
        ]
    lines.append("")
    return "\n".join(lines)


def _write_run_dir(out_dir: Path, files: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8", newline="\n")


def _write_report_files(out_dir: Path, report_dict: dict[str, Any]) -> None:
    _write_run_dir(
        out_dir,
        {
            "report.json": json.dumps(report_dict, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            "report.md": render_report_markdown(report_dict),
        },
    )


# ---------------------------------------------------------------- replay


def run_replay(args: argparse.Namespace) -> int:
    """本地冻结快照 → 确定性报告（P1A 路径；零网络零凭据）。"""
    fixtures = Path(args.fixtures)
    brief = ProjectBrief(text=args.goal, source=f"cli:{args.mode}")
    catalog = load_catalog(fixtures)
    registry = load_dimension_registry(fixtures / catalog.dimension_registry_path)
    snapshots = dict(load_snapshots(fixtures, catalog))
    candidates = [c for c in args.candidates.split(",") if c.strip()] or catalog.ids()
    worker = FixtureResearchWorker(catalog, registry, snapshots)
    cards = []
    for candidate_id in candidates:
        task_input = _fixture_task_input(candidate_id)
        result = asyncio.run(worker.run(task_input))
        cards.append(_fixture_card(task_input, result))
    report = assemble_report(brief, cards, registry, snapshots=snapshots)
    report_dict = report.to_dict()
    out_dir = Path(args.out)
    _write_report_files(out_dir, report_dict)
    _write_run_dir(
        out_dir,
        {
            "run_record.json": json.dumps(
                {
                    "mode": "replay",
                    "goal": args.goal,
                    "candidates": candidates,
                    "report_id": report.report_id,
                    "integrity_passed": report.integrity.passed,
                    "note": "确定性回放：本地冻结快照（fixtures/research），零网络零凭据，可逐字节复现。",
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        },
    )
    print(f"replay 完成：report_id={report.report_id} integrity={report.integrity.passed}")
    print(f"产物目录: {out_dir.resolve()}（report.md / report.json / run_record.json）")
    return EXIT_OK


def _fixture_task_input(candidate_id: str):
    from .contracts import WorkerTaskInput

    return WorkerTaskInput(
        task_id=f"research__{candidate_id}",
        title="调研候选方案",
        assigned_agent="research",
        goal="调研该候选方案在本快照里的表现",
        context=f"candidate_id: {candidate_id}\n",
        upstream_results={},
    )


def _fixture_card(task_input, result):
    from .contracts import TaskCard

    return TaskCard(
        task_id=task_input.task_id,
        title=task_input.title,
        assigned_agent=task_input.assigned_agent,
        goal=task_input.goal,
        context=task_input.context,
        status="done",
        result=result,
        attempts=1,
    )


# ---------------------------------------------------------------- 编排（offline / live 共用）


def run_orchestration_mode(args: argparse.Namespace, *, live: bool) -> int:
    from .orchestrator import (
        MultiAgentOrchestrator,
        OrchestratorConfig,
        RuntimeResearchWorker,
        SynthesizerWorker,
        VerifierWorker,
    )
    from .workers.base import WorkerRegistry

    out_dir = Path(args.out)
    model_transport = None
    model_desc = ""
    if live:
        missing = _missing_model_config(args)
        if missing:
            # 一次性列清缺失配置；**发起任何网络请求之前**终止。
            print("live 模式缺少必要模型配置，未发起任何网络请求。缺失项：", file=sys.stderr)
            for item in missing:
                print(f"  - {item}", file=sys.stderr)
            print(
                "补充方式：--model-preset dashscope --model-name <name>（key 从 "
                "DASHSCOPE_API_KEY 读取），或显式传 --model-endpoint/--model-name/"
                "--model-api-key-env。",
                file=sys.stderr,
            )
            return EXIT_USAGE
        model_transport, model_desc = _build_model_transport(args)

    plan: dict[str, Any] | None = None
    if live:
        from .providers.pinned_sources import parse_source_plan

        plan = parse_source_plan(json.loads(Path(args.sources).read_text(encoding="utf-8")))
        plan_candidates = set(plan)
        requested = {c.strip() for c in args.candidates.split(",") if c.strip()}
        candidates_csv = ",".join(sorted(requested or plan_candidates))
        outside = sorted(requested - plan_candidates)
        if outside:
            print(
                f"来源计划缺少候选 {outside} 的条目（来源计划必须覆盖全部候选）。",
                file=sys.stderr,
            )
            return EXIT_USAGE
    else:
        requested = {c.strip() for c in args.candidates.split(",") if c.strip()}
        if requested:
            candidates_csv = ",".join(sorted(requested))
        else:
            candidates_csv = ",".join(load_catalog(DEFAULT_FIXTURES).ids())

    provider_holder: dict[str, Any] = {}

    def provider_factory(candidates):
        from .providers.pinned_sources import PinnedSourceProvider
        from .runtime.mock_provider import ScriptedSearchProvider, scripted_document

        if not live:
            documents = {
                f"{candidate_id} deployment_complexity": [
                    scripted_document(
                        terms=f"{candidate_id} deployment_complexity",
                        candidate_id=candidate_id,
                        index=0,
                        body=f"{candidate_id} 的官方说明（脚本正文，非真实检索）。",
                        captured_at="2026-09-26",
                    )
                ]
                for candidate_id in candidates
            }
            return ScriptedSearchProvider(documents=documents, captured_at="2026-09-26")
        scoped = {cid: plan[cid] for cid in candidates if cid in plan}
        from .providers.http_transport import HttpTransport

        provider = PinnedSourceProvider(
            source_plan=scoped,
            transport=HttpTransport(allowed_hosts=frozenset({"api.github.com"})),
            timeout_seconds=float(args.retrieval_timeout),
        )
        provider_holder["provider"] = provider
        return provider

    def decider_factory():
        from .research.dimensions import load_dimension_registry as _ldr
        from .runtime.decider_generated import GeneratedDimensionDecider

        registry = _ldr(Path(args.dimensions))
        scale_labels = {
            d.dimension_id: tuple(d.scale_labels) for d in registry.dimensions
        }
        if live:
            from .runtime.decider_generated import DecisionGenConfig as _C

            return GeneratedDimensionDecider(
                transport=model_transport,
                config=_C(
                    max_retries=int(args.decision_retries),
                    request_timeout_seconds=float(args.decision_timeout),
                    max_decision_requests=int(args.max_decision_requests),
                    # 与下游 ResearchFinding.claim 的 400 字上限对齐（放行更长
                    # 的 claim 会让卡片在组装层失败——边界必须一致）。
                    claim_max_chars=400,
                ),
                scale_labels=scale_labels,
            )
        return None  # RuntimeResearchWorker 缺省构造离线脚本决策器

    researcher = RuntimeResearchWorker(
        agent_name="researcher",
        snapshot_root=out_dir / "runtime" / "snapshots",
        run_root=out_dir / "runtime" / "runs",
        dimensions_json=Path(args.dimensions),
        decider=decider_factory() if live else None,
        provider_factory=provider_factory,
        worker_identity=(
            f"runtime-research.v1:live:{model_desc}" if live else "runtime-research.v1:offline-scripted"
        ),
        budget=_research_budget(args),
    )
    registry = WorkerRegistry()
    registry.register("researcher", researcher)

    board = _orchestration_board(candidates_csv)
    def validate_live_sources(report: dict[str, Any], audit: Any) -> tuple[bool, list[str]]:
        provider = provider_holder.get("provider")
        if provider is None:
            return False, ["研究 Provider 尚未创建，无法独立核验来源"]
        return provider.verify_report_sources(report, audit)

    verifier = VerifierWorker(
        agent_name="verifier",
        source_validator=validate_live_sources if live else None,
        require_source_audit=live,
    )
    registry.register("verifier", verifier)
    registry.register("synthesizer", SynthesizerWorker(agent_name="synthesizer"))
    orchestrator = MultiAgentOrchestrator(
        planner=_planner(board),
        registry=registry,
        config=OrchestratorConfig(
            max_card_attempts=int(args.max_card_attempts),
            max_loop_steps=int(args.max_loop_steps),
        ),
        planner_identity="cli.v1:structured-plan",
    )
    result = asyncio.run(orchestrator.run(args.goal))

    _write_run_dir(
        out_dir,
        {
            "run_record.json": json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            "report.md": render_orchestration_markdown(result, scripted=not live),
            "report.json": json.dumps(
                {
                    "mode": "live" if live else "offline",
                    "run_id": result.run_id,
                    "outcome": result.outcome,
                    "failure_reason": result.failure_reason,
                    "final_report": thaw_payload(result.final_report) if result.final_report is not None else None,
                    "selection_reports": _verified_selection_reports(result),
                    "verdicts": result.verdicts,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        },
    )
    if live:
        # 来源审计在 researcher 载荷里；此处从编排结果提取
        payload_sources = _collect_source_audit(result)
        _write_run_dir(
            out_dir,
            {
                "sources.json": json.dumps(
                    {"source_audit": payload_sources, "note": "verifier 已将本次原文字节、Git blob SHA 和报告证据绑定；若需独立重验上游，可按固定 commit URL 重新获取。"},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            },
        )
    print(
        f"{'live' if live else 'offline'} 编排完成：run_id={result.run_id} "
        f"outcome={result.outcome}"
        + (f"（原因: {result.failure_reason}）" if result.failure_reason else "")
    )
    if result.final_report is not None:
        print(f"final_report_id={result.final_report.get('final_report_id', '')}")
    print(f"产物目录: {out_dir.resolve()}（report.md / report.json / run_record.json"
          + (" / sources.json" if live else "") + "）")
    return EXIT_OK if result.outcome == "completed" else EXIT_RUN_FAILED


def _research_budget(args: argparse.Namespace):
    from .runtime.budget import ResearchBudget

    return ResearchBudget(
        max_steps=64,
        max_provider_requests=64,
        max_total_content_chars=2_000_000,
        max_retries_per_step=1,
        request_timeout_seconds=float(args.request_timeout),
    )


def _collect_source_audit(result: Any) -> list[dict[str, Any]]:
    """从编排结果的 board 里提取 researcher 载荷的来源审计。"""
    entries: list[dict[str, Any]] = []
    for card in result.board_state.get("cards", {}).values():
        payload = (card.get("result") or {}).get("structured_payload") or {}
        if isinstance(payload, dict) and isinstance(payload.get("source_audit"), list):
            entries.extend(payload["source_audit"])
    return entries


def _orchestration_board(candidates_csv: str) -> dict[str, Any]:
    candidates = [c for c in candidates_csv.split(",") if c.strip()]
    ctx = f"candidate_ids: {','.join(candidates)}\ncli 编排：研究范围以声明为准。"
    return {
        "tasks": [
            {"task_id": "research_candidates", "title": "研究候选", "assigned_agent": "researcher",
             "goal": "基于显式来源计划研究候选方案", "context": ctx, "depends_on": []},
            {"task_id": "verify_report", "title": "独立重验", "assigned_agent": "verifier",
             "goal": "独立重验研究产物", "context": ctx, "depends_on": ["research_candidates"]},
            {"task_id": "synthesize", "title": "合成最终报告", "assigned_agent": "synthesizer",
             "goal": "汇总验证通过的结论", "context": ctx, "depends_on": ["verify_report"]},
        ]
    }


def _planner(board: dict[str, Any]):
    from .planner import MockPlanner

    return MockPlanner(board)


def _missing_model_config(args: argparse.Namespace) -> list[str]:
    import os

    missing: list[str] = []
    endpoint = args.model_endpoint
    name = args.model_name
    key_env = args.model_api_key_env
    if args.model_preset:
        preset = MODEL_PRESETS.get(args.model_preset)
        if preset is None:
            return [f"未知 model preset：{args.model_preset!r}（可用 {sorted(MODEL_PRESETS)}）"]
        endpoint = endpoint or preset["endpoint"]
        key_env = key_env or preset["api_key_env"]
    if not endpoint:
        missing.append("模型端点（--model-endpoint 或 --model-preset）")
    if not name:
        missing.append("模型名（--model-name，例如 qwen-turbo）")
    if not key_env:
        missing.append("API key 环境变量名（--model-api-key-env 或 preset 默认）")
    else:
        if not os.environ.get(key_env, "").strip():
            missing.append(f"环境变量 {key_env} 未设置或为空（模型凭据）")
    return missing


def _build_model_transport(args: argparse.Namespace):

    from .providers.decision_transport import CompatibleApiDecisionTransport
    from .providers.http_transport import HttpTransport

    endpoint = args.model_endpoint
    key_env = args.model_api_key_env
    if args.model_preset:
        preset = MODEL_PRESETS[args.model_preset]
        endpoint = endpoint or preset["endpoint"]
        key_env = key_env or preset["api_key_env"]
        host = preset["host"]
    else:
        host = endpoint.split("/")[2] if "://" in endpoint else ""
    transport = HttpTransport(allowed_hosts=frozenset({host}))
    model_transport = CompatibleApiDecisionTransport(
        endpoint=endpoint,
        model=args.model_name,
        transport=transport,
        api_key_env_var=key_env,
    )
    desc = f"{args.model_name}@{host}"
    return model_transport, desc


# ---------------------------------------------------------------- 参数与入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentboard",
        description="AgentBoard 研究 Agent：replay（本地回放）/ offline（编排·脚本替身）/ live（真实接线）",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--goal", required=True, help="研究目标（一句话）")
        p.add_argument("--out", required=True, help="运行产物目录")
        p.add_argument("--candidates", default="", help="逗号分隔的候选标识（缺省用快照集全部候选）")

    replay = sub.add_parser("replay", help="本地冻结快照 → 确定性报告（零网络）")
    add_common(replay)
    replay.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))

    offline = sub.add_parser("offline", help="P5 编排闭环（脚本检索 + 脚本决策；明确标注 scripted）")
    add_common(offline)
    offline.add_argument("--dimensions", default=str(DEFAULT_DIMENSIONS))
    offline.add_argument("--max-card-attempts", type=int, default=3)
    offline.add_argument("--max-loop-steps", type=int, default=64)
    offline.add_argument("--request-timeout", type=int, default=45)

    live = sub.add_parser("live", help="真实接线：固定 commit 原文 + 真实模型决策（缺配置即退出）")
    add_common(live)
    live.add_argument("--sources", required=True, help="来源计划 JSON（候选→repo/path/ref/commit）")
    live.add_argument("--dimensions", default=str(DEFAULT_DIMENSIONS))
    live.add_argument("--model-preset", default="", help=f"模型 preset（{sorted(MODEL_PRESETS)}）")
    live.add_argument("--model-endpoint", default="", help="OpenAI 兼容 chat/completions 端点")
    live.add_argument("--model-name", default="", help="模型名")
    live.add_argument("--model-api-key-env", default="", help="API key 的环境变量名（不回显值）")
    live.add_argument("--decision-retries", type=int, default=1)
    live.add_argument("--decision-timeout", type=int, default=60)
    live.add_argument("--max-decision-requests", type=int, default=64)
    live.add_argument("--retrieval-timeout", type=int, default=30)
    live.add_argument("--request-timeout", type=int, default=45,
                      help="单次 HTTP/模型请求的预算超时（秒；运行时透传给 Provider/transport）")
    live.add_argument("--max-card-attempts", type=int, default=3)
    live.add_argument("--max-loop-steps", type=int, default=64)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "replay":
        return run_replay(args)
    if args.mode == "offline":
        return run_orchestration_mode(args, live=False)
    if args.mode == "live":
        return run_orchestration_mode(args, live=True)
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

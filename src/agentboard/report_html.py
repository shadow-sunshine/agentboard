"""运行产物 → 单文件 HTML 报告页（零第三方依赖）。

为什么有这层
------------
本项目的核心卖点是**可验证**：每条结论都能点回 GitHub 固定 commit 的原文，
每次失败都能在事件序列里看到原因。这两件事原本只存在于 `run_record.json`
（几十 KB）与 `report.md`（一张笨表格）里——上传 GitHub 后，没有人会打开它们。

所以这层只做一件事：把**已有产物**渲染成一个人能在浏览器里读完的页面。
它**不新增任何运行时依赖**（纯字符串模板 + 内联 CSS/JS），**不新增任何数据**
（全部字段来自 `report.json` / `run_record.json` / `sources.json`），
**不做任何计算或判断**（不重算分数、不重验证据、不改写结论）。

边界（与 P6 纪律一致）
---------------------
- 渲染器是**只读投影**：输入不变 ⇒ 输出逐字节相同（无时间戳、无随机 id）；
- 渲染器**不提升信任等级**：live 材料仍按 `authored_summary` 信任层呈现，
  页面顶部保留"未升格 research_grade"的显式说明；
- 所有文本经 `_esc()` 转义 —— 来源正文是不可信数据，注入不进页面；
- 证据摘录按 `max_excerpt_chars` 截断，避免 42KB 原文撑爆页面（截断是显式的、
  带省略标记，不静默丢内容）。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

#: 证据摘录在页面上最多显示的字符数（超出显式截断并标注）。
DEFAULT_MAX_EXCERPT_CHARS = 400

#: 事件类型的展示顺序未知（运行期产生）⇒ 固定按此顺序渲染，未列出的追加在末尾。
_EVENT_ORDER: tuple[str, ...] = (
    "board_created",
    "plan_rejected",
    "task_started",
    "task_retry",
    "task_done",
    "task_failed",
    "verifier_passed",
    "verifier_rejected",
    "blocked",
    "final_report_ready",
)

_STATUS_TONE: dict[str, str] = {
    "completed": "ok",
    "passed": "ok",
    "succeeded": "ok",
    "done": "ok",
    "failed": "bad",
    "rejected": "bad",
    "blocked": "warn",
    "skipped": "muted",
}


def render_run_html(
    report: dict[str, Any],
    run_record: dict[str, Any] | None = None,
    sources: dict[str, Any] | None = None,
    *,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
) -> str:
    """把一次运行的产物渲染成单文件 HTML（纯函数；确定性）。"""
    if not isinstance(report, dict):
        raise TypeError("report 必须是 dict")
    if isinstance(max_excerpt_chars, bool) or not isinstance(max_excerpt_chars, int):
        raise TypeError("max_excerpt_chars 必须是整数")
    if max_excerpt_chars <= 0:
        raise ValueError("max_excerpt_chars 必须为正整数")
    record = dict(run_record or {})
    raw_audit = (sources or {}).get("source_audit")
    audit = [item for item in raw_audit if isinstance(item, dict)] if isinstance(raw_audit, list) else []
    parts: list[str] = [
        _HEAD,
        _header(report, record),
        _sources_section(audit),
        _events_section(record),
        _board_section(record),
        _reports_section(report, max_excerpt_chars),
        _FOOT,
    ]
    return "\n".join(parts)


def render_run_dir_html(run_dir: Any, **kwargs: Any) -> str:
    """从运行目录读三份产物再渲染（缺文件按空处理，不编造）。"""
    base = Path(run_dir)
    report = _read_json(base / "report.json")
    record = _read_json(base / "run_record.json")
    sources = _read_json(base / "sources.json")
    if not report:
        raise ValueError(f"运行目录缺少可渲染的 report.json：{base}")
    return render_run_html(report, record, sources, **kwargs)


# ---------------------------------------------------------------- 读取


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------- 头部


def _header(report: dict[str, Any], record: dict[str, Any]) -> str:
    outcome = str(record.get("outcome") or report.get("outcome") or "unknown")
    run_id = str(record.get("run_id") or report.get("run_id") or "-")
    mode = str(report.get("mode") or "orchestration")
    verdicts = record.get("verdicts") or {}
    verdict_rows = "".join(
        f'<span class="pill {_tone(str(value))}">{_esc(str(key))}: {_esc(str(value))}</span>'
        for key, value in sorted(verdicts.items())
    ) or '<span class="pill muted">无验证结论</span>'
    integrity = _first_report(report).get("integrity") or {}
    passed = bool(integrity.get("passed"))
    failed_codes = integrity.get("failed_codes") or []
    integrity_text = (
        f"通过（{len(integrity.get('checks') or [])} 项检查）"
        if passed and not failed_codes
        else f"未通过：{', '.join(str(code) for code in failed_codes) or '未知'}"
    )
    return f"""
<section class="card head">
  <div class="row">
    <h1>AgentBoard 运行报告</h1>
    <span class="pill {_tone(outcome)}">{_esc(outcome)}</span>
  </div>
  <dl class="kv">
    <div><dt>run_id</dt><dd><code>{_esc(run_id)}</code></dd></div>
    <div><dt>模式</dt><dd>{_esc(mode)}</dd></div>
    <div><dt>完整性 I1-I9</dt><dd class="{_tone('passed' if passed else 'failed')}">{_esc(integrity_text)}</dd></div>
    <div><dt>决策机制</dt><dd><code>{_esc(str(record.get('decision_identity') or '-'))}</code></dd></div>
  </dl>
  <div class="row wrap">verdict：{verdict_rows}</div>
  <p class="note">本页是已有产物的只读投影，不重算分数、不重验证据、不提升信任等级。
  live 采集的材料按 <code>authored_summary</code> 信任层处理，
  <strong>不会自动升格为 research_grade</strong>，需人工复核后才能升格。</p>
</section>
"""


# ---------------------------------------------------------------- 来源


def _sources_section(audit: list[dict[str, Any]]) -> str:
    if not audit:
        return ""
    rows = "".join(
        f"""<tr>
      <td><code>{_esc(str(entry.get('candidate_id', '-')))}</code></td>
      <td><code>{_esc(str(entry.get('repo', '-')))}</code></td>
      <td><code>{_esc(str(entry.get('path', '-')))}</code></td>
      <td><a href="{_esc(str(entry.get('source_url', '#')))}" target="_blank" rel="noopener noreferrer">{_esc(str(entry.get('commit', '-'))[:12])}</a></td>
      <td><code class="digest">{_esc(str(entry.get('content_sha256', '-'))[:16])}…</code></td>
    </tr>"""
        for entry in sorted(
            audit, key=lambda item: (str(item.get("candidate_id", "")), str(item.get("repo", "")))
        )
    )
    return f"""
<section class="card">
  <h2>来源审计（固定 commit 原文）</h2>
  <p class="note">检索摘要不算证据。每件来源都取自 GitHub contents API 的
  <strong>固定 commit 原文</strong>，重新取回同一 URL 重算 SHA-256 即可重验。</p>
  <table>
    <thead><tr><th>候选</th><th>仓库</th><th>路径</th><th>commit</th><th>内容 SHA-256</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</section>
"""


# ---------------------------------------------------------------- 事件


def _events_section(record: dict[str, Any]) -> str:
    events = [item for item in (record.get("events") or []) if isinstance(item, dict)]
    if not events:
        return ""
    ordered = sorted(
        events,
        key=lambda item: (
            _EVENT_ORDER.index(str(item.get("event")))
            if str(item.get("event")) in _EVENT_ORDER
            else len(_EVENT_ORDER),
            str(item.get("task_id") or ""),
        ),
    )
    items = "".join(
        f"""<li class="step {_tone(str(event.get('event')))}">
      <span class="dot"></span>
      <div>
        <code>{_esc(str(event.get('event')))}</code>
        <span class="muted">{_esc(str(event.get('task_id') or ''))}</span>
        {_event_detail(event)}
      </div>
    </li>"""
        for event in ordered
    )
    return f"""
<section class="card">
  <h2>事件时间线（{len(ordered)} 条）</h2>
  <p class="note">调度过程全部落事件：建板 → 派发 → 重试/失败 → 验证 → 合成。
  失败留在具体任务卡上，不会污染整次运行。</p>
  <ol class="timeline">{items}</ol>
</section>
"""


def _event_detail(event: dict[str, Any]) -> str:
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return ""
    bits: list[str] = []
    for key in ("reason", "verdict", "error"):
        value = data.get(key)
        if value:
            bits.append(f"{key}={_short(str(value))}")
    reasons = data.get("reasons")
    if isinstance(reasons, list) and reasons:
        bits.append(f"reasons={_short('; '.join(str(item) for item in reasons))}")
    if not bits:
        return ""
    return f'<div class="why">{_esc(" ｜ ".join(bits))}</div>'


# ---------------------------------------------------------------- 任务板


def _board_section(record: dict[str, Any]) -> str:
    cards = ((record.get("board_state") or {}).get("cards") or {})
    if not isinstance(cards, dict) or not cards:
        return ""
    blocks: list[str] = []
    for task_id in sorted(cards):
        card = cards[task_id]
        if not isinstance(card, dict):
            continue
        status = str(card.get("status") or "unknown")
        error = str(card.get("error") or "")
        blocks.append(
            f"""<div class="task">
      <div class="row">
        <code>{_esc(str(task_id))}</code>
        <span class="pill {_tone(status)}">{_esc(status)}</span>
        <span class="muted">尝试 {int(card.get('attempts') or 0)} 次</span>
      </div>
      {f'<div class="why bad-text">{_esc(_short(error, 300))}</div>' if error else ''}
    </div>"""
        )
    return f"""
<section class="card">
  <h2>任务板（{len(blocks)} 张卡）</h2>
  <p class="note">每张卡是一个独立可重试单元。角色图在<b>计划层</b> fail-closed 校验——
  synthesizer 越过 verifier 的计划在派发前就被拒绝。</p>
  {''.join(blocks)}
</section>
"""


# ---------------------------------------------------------------- 报告主体


def _reports_section(report: dict[str, Any], max_excerpt: int) -> str:
    reports = report.get("selection_reports")
    if not isinstance(reports, list):
        reports = [report] if report.get("report_id") else []
    if not reports:
        return ""
    blocks = [_report_block(item, max_excerpt) for item in reports if isinstance(item, dict)]
    return f"""
<section class="card">
  <h2>研究报告（{len(blocks)} 份，仅汇总验证通过的结论）</h2>
  {''.join(blocks)}
</section>
"""


def _report_block(report: dict[str, Any], max_excerpt: int) -> str:
    report_id = str(report.get("report_id") or "-")
    recommendations = report.get("recommendations") or []
    rec_rows = "".join(
        f"""<tr>
      <td>{index + 1}</td>
      <td><code>{_esc(str(item.get('candidate_id', '-')))}</code></td>
      <td class="num">{_num(item.get('score'))}</td>
      <td>{_esc(_short(str(item.get('rationale') or item.get('confidence') or ''), 200))}</td>
    </tr>"""
        for index, item in enumerate(recommendations)
        if isinstance(item, dict)
    )
    findings = "".join(
        _finding_block(item, max_excerpt)
        for item in (report.get("findings") or [])
        if isinstance(item, dict)
    )
    risks = "".join(
        f'<li><span class="pill {_tone("failed" if str(item.get("severity")) == "high" else "warn")}">'
        f'{_esc(str(item.get("severity", "-")))}</span> '
        f'<code>{_esc(str(item.get("candidate_id") or item.get("dimension_id") or "-"))}</code> '
        f'{_esc(_short(str(item.get("description") or item.get("text") or ""), 220))}</li>'
        for item in (report.get("risks") or [])
        if isinstance(item, dict)
    )
    unsupported = _unsupported(report)
    checks = "".join(
        f"""<tr>
      <td><code>{_esc(str(item.get('code', '-')))}</code></td>
      <td class="{_tone('passed' if item.get('passed') else 'failed')}">{'通过' if item.get('passed') else '未通过'}</td>
      <td>{_esc(str(item.get('detail', '')))}</td>
    </tr>"""
        for item in ((report.get("integrity") or {}).get("checks") or [])
        if isinstance(item, dict)
    )
    risk_count = len(report.get("risks") or [])
    risk_block = (
        f'<h4>风险（{risk_count} 条）</h4><ul class="risks">{risks}</ul>' if risks else ""
    )
    return f"""
<article class="report">
  <h3>report_id <code>{_esc(report_id)}</code></h3>
  <h4>推荐排名（打分由既有 scoring 模块计算，本页只展示）</h4>
  <table>
    <thead><tr><th>排名</th><th>候选</th><th>得分</th><th>依据</th></tr></thead>
    <tbody>{rec_rows or '<tr><td colspan="4" class="muted">无推荐</td></tr>'}</tbody>
  </table>
  <h4>完整性校验</h4>
  <table class="compact">
    <thead><tr><th>检查</th><th>结果</th><th>说明</th></tr></thead>
    <tbody>{checks or '<tr><td colspan="3" class="muted">无检查记录</td></tr>'}</tbody>
  </table>
  <h4>结论与证据回链（点摘录可展开原文定位）</h4>
  <div class="findings">{findings or '<p class="muted">无结论</p>'}</div>
  {risk_block}
  {unsupported}
</article>
"""


def _finding_block(finding: dict[str, Any], max_excerpt: int) -> str:
    refs = [
        item
        for item in (finding.get("evidence_refs") or [])
        if isinstance(item, dict)
    ]
    if not refs:
        return (
            f'<div class="finding unsupported"><div class="row"><code>'
            f'{_esc(str(finding.get("candidate_id", "-")))} / '
            f'{_esc(str(finding.get("dimension_id", "-")))}</code>'
            f'<span class="pill muted">unsupported</span></div></div>'
        )
    ref = refs[0]
    excerpt = str(ref.get("excerpt") or "")
    truncated = len(excerpt) > max_excerpt
    shown = excerpt[:max_excerpt] + ("…" if truncated else "")
    locator = str(ref.get("locator") or ref.get("evidence_id") or "-")
    return f"""
<details class="finding">
  <summary>
    <code>{_esc(str(finding.get('candidate_id', '-')))} / {_esc(str(finding.get('dimension_id', '-')))}</code>
    <span class="val">{_esc(str(finding.get('value', '-')))}</span>
    <span class="claim">{_esc(_short(str(finding.get('claim') or ''), 150))}</span>
  </summary>
  <blockquote>{_esc(shown)}</blockquote>
  <div class="meta">
    locator：<code>{_esc(locator)}</code> ·
    digest：<code class="digest">{_esc(str(ref.get('digest') or '-')[:16])}…</code> ·
    采集时间：<code>{_esc(str(ref.get('captured_at') or '-'))}</code>
    {f'· 原文共 {len(excerpt)} 字符，此处截断展示' if truncated else ''}
  </div>
</details>
"""


def _unsupported(report: dict[str, Any]) -> str:
    """渲染显式 unsupported 维度。

    `coverage.candidates` 是**列表**（每项含 candidate_id / missing_dimensions），
    不是按候选 id 索引的映射——两种形态都接受，但都不许静默丢内容。
    """
    coverage = report.get("coverage")
    if not isinstance(coverage, dict):
        return ""
    entries = coverage.get("candidates")
    if not isinstance(entries, list):
        entries = [
            {"candidate_id": key, "missing_dimensions": value.get("missing_dimensions")}
            for key, value in coverage.items()
            if isinstance(value, dict)
        ]
    rows: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        missing = entry.get("missing_dimensions") or entry.get("unsupported_dimensions")
        if not missing:
            continue
        rows.append(
            f"<li><code>{_esc(str(entry.get('candidate_id', '-')))}</code>："
            f"{_esc(', '.join(str(item) for item in missing))}</li>"
        )
    if not rows:
        return ""
    return f"""
<h4>显式 unsupported（材料不足，不推断补齐）</h4>
<ul class="risks">{''.join(rows)}</ul>
"""


def _first_report(report: dict[str, Any]) -> dict[str, Any]:
    reports = report.get("selection_reports")
    if isinstance(reports, list):
        for item in reports:
            if isinstance(item, dict):
                return item
    return report


# ---------------------------------------------------------------- 工具


def _tone(value: str) -> str:
    return _STATUS_TONE.get(value, "warn" if "reject" in value or "fail" in value else "muted")


def _num(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{float(value):.4f}"


def _short(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _esc(text: Any) -> str:
    """HTML 转义（含引号）——来源正文是不可信数据，必须过这一层。"""
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------- 模板

_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentBoard 运行报告</title>
<style>
:root{--bg:#14161a;--card:#1c1f26;--line:#2e3239;--fg:#e6e8ec;--muted:#9aa1ad;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px;background:var(--bg);color:var(--fg);
font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px}
h1{font-size:19px;font-weight:600;margin:0}
h2{font-size:16px;font-weight:600;margin:0 0 6px}
h3{font-size:14px;font-weight:600;margin:0 0 4px}
h4{font-size:13px;font-weight:600;margin:20px 0 8px;color:var(--muted)}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;
background:#11141a;padding:1px 5px;border-radius:4px}
.row{display:flex;align-items:center;gap:10px}
.wrap-row,.row.wrap{flex-wrap:wrap}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:18px 0 0}
.kv>div{background:#11141a;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
dt{font-size:11px;color:var(--muted);margin-bottom:3px}
dd{margin:0;font-size:13px;word-break:break-all}
.pill{font-size:11px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.pill.ok{color:var(--ok);border-color:#1d4429}
.pill.bad{color:var(--bad);border-color:#5c2220}
.pill.warn{color:var(--warn);border-color:#4a3a12}
.muted{color:var(--muted)}
.note{font-size:12px;color:var(--muted);margin:10px 0 0}
.bad-text{color:var(--bad)}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;color:var(--muted);font-weight:500}
td.num{text-align:right;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
.digest{color:var(--muted)}
.timeline{list-style:none;margin:12px 0 0;padding:0}
.step{display:flex;gap:12px;padding:7px 0;border-bottom:1px solid var(--line)}
.step:last-child{border-bottom:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none;margin-top:7px}
.step.ok .dot{background:var(--ok)}.step.bad .dot{background:var(--bad)}
.step.warn .dot{background:var(--warn)}
.why{font-size:12px;color:var(--muted);margin-top:3px;word-break:break-word}
.task{background:#11141a;border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-top:10px}
.report+.report{margin-top:24px;padding-top:20px;border-top:1px solid var(--line)}
.findings{margin-top:8px}
.finding{background:#11141a;border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-top:8px}
.finding summary{cursor:pointer;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.finding .val{color:var(--accent);font-weight:600}
.finding .claim{color:var(--muted);font-size:12px}
.finding.unsupported{opacity:.75}
blockquote{margin:10px 0 0;padding:10px 12px;background:#0d1015;border-left:3px solid var(--line);
border-radius:0 6px 6px 0;white-space:pre-wrap;word-break:break-word;font-size:12px;max-height:260px;overflow:auto}
.meta{font-size:11px;color:var(--muted);margin-top:8px;word-break:break-all}
.risks{margin:8px 0 0;padding-left:18px;font-size:13px}
.risks li{margin-bottom:6px}
footer{color:var(--muted);font-size:12px;text-align:center;padding:8px 0 24px}
</style>
</head>
<body>
<div class="wrap">
"""

_FOOT = """
</div>
<footer>AgentBoard · 受控研究 Agent · 本页为产物只读投影，结论可信性以 report.json 与来源审计为准</footer>
</body>
</html>
"""


__all__ = [
    "DEFAULT_MAX_EXCERPT_CHARS",
    "render_run_dir_html",
    "render_run_html",
]

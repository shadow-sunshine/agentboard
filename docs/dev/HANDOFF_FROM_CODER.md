# HANDOFF_FROM_CODER

status: awaiting_review
stage: P6-real-e2e-and-external-worker（真实接线 + 可运行入口 + 真实端到端验证）
attempt: 1/3
max_attempts: 3
coder: WorkBuddy
completed_at: 2026-09-27 18:10

WorkBuddy 完成后填写本文件；不要覆盖历史结论，保留原始命令输出。
（P5/P4/P3/P2.2 及更早回执归档于本文件下半部，未改动。）

---

## ⓿ P6 回执（2026-09-27 18:10，attempt 1/3）

### 任务与依据

按 `CURRENT_TASK.md`（stage: P6-real-e2e-and-external-worker）执行：一个完整宏任务
（真实接入 + 可运行入口 + 真实端到端验证），先读 PROJECT_RULES/BUG_EXPERIENCES/
REVIEW_STATUS 与既有实现，复用既有接口。未拆小阶段、未进入下一阶段。

### 逐项交付（对 CURRENT_TASK「必须一次性交付」1–6）

**1. 可用入口（`python -m agentboard`）**
`src/agentboard/cli.py` + `__main__.py`，三模式边界显式：
- `replay --goal --out [--candidates]`：本地冻结快照 → P1A 确定性报告（零网络零凭据，
  同输入 report_id 逐字节复现；实测 `rp_30b82fa3b8f6148d`）；
- `offline`：P5 编排闭环 + 离线 runtime researcher（脚本检索 + 脚本决策）——
  产物 Markdown **明确标注 "offline/scripted 模式"，不冒充真实能力**；
- `live`：真实接线（见下）。产物一律写入 `--out` 运行目录：
  `report.md`（人读）+ `report.json` + `run_record.json`（run_id/事件序列/board/
  verdicts/审计）+ live 的 `sources.json`（逐件来源审计）。
  README 给出全部实测可复制命令（含 `PYTHONPATH=src` 前缀）。

**2. 真实研究接线（`providers/pinned_sources.py`）**
- 检索摘要不是证据：live 按显式来源计划（每候选 1..4 件，有界）经 **GitHub
  contents API 取原文全文**（base64 解码），解析出的 40 位 commit 写进规范
  source_url（`github.com/{repo}/blob/{commit}/{path}`）→ 重新取回同 URL 重算
  sha256 即可重验；
- 声明 commit 与解析结果不符 ⇒ 来源不匹配显式失败；超 1MB / 非 base64 /
  rate-limit 错误载荷 ⇒ 显式失败（不当作零结果）；
- HTTP 调用计数进审计；主机白名单只放 `api.github.com`（localhost/私网被
  transport 白名单拒绝，有测试）；
- **真实模型决策**：P4 `GeneratedDimensionDecider` + `CompatibleApiDecisionTransport`
  （DashScope preset：官方兼容模式端点固定，key 只从 `DASHSCOPE_API_KEY` 读，
  不回显不留存）。
- **无资料/无凭据/超预算/远端失败全部显式失败或部分结果，零 Mock fallback**
  （编排器无任何回退路径，负控测试钉死）。

**3. 外部 Worker 适配（`workers/external.py`）**
- `ExternalModelWorker` 实现既有 `WorkerTaskInput`/`WorkerResult` 契约并接入 P5
  编排（verifier/synthesizer 全链路），**复用 P4 的 DecisionTransport**
  （离线 `ScriptedDecisionTransport` / 真实 `CompatibleApiDecisionTransport`）；
- 严格 JSON 协议（闭集键 facts/risks/open_questions/confidence/summary），
  注入输出被闭集拒绝（负控钉死）；
- 其结论显式携带 `unverified: true`——verifier 将其标注为**未验证材料**
  （结构与范围可验证，内容不声称已核实）；
- **不自造 ACP**：ACP 协议适配器如实登记为未完成（未确认官方协议与可用端点前
  不用 HTTP 接口冒充 ACP）。

**4. 闭环与来源边界**
live 闭环 = goal → P5 有界计划 → 真实检索/原文 → P4 受控生成决策 → verifier →
synthesizer → 最终报告。verifier 保留 P5 的**完整重验路径**（Codex 强化版：
重建 SelectionReport、重跑 I1–I9、评分/推荐复核、digest 重算）并新增：
- **source_audit 逐条复核**：候选归属 ⊆ 声明、固定 commit（40 位）内嵌 URL、
  sha256 形态、去重——verdict checks 含 `source_audit_ok`，notes 如实标注
  "digest 为检索层测量，重验 = 重新取回重算"；
- **未验证材料标注**：外部模型结论进 verdict notes，最终报告 Markdown 单列
  "未验证材料标注"节；绝不以模型自报 passed 充当验证。

**5. 限制与演练**
- 网络限制：仅 `api.github.com` + 模型端点主机（transport 白名单强制）；
  禁 localhost/私网（测试断言拒绝）；无 shell/任意写权限；
- 预算与取消：模型/检索次数、内容量、单请求超时、重试上限全部有界
  （`OrchestratorConfig.max_card_attempts/max_loop_steps` + `ResearchBudget` +
  `DecisionGenConfig`），预算耗尽/步数耗尽 ⇒ 可观测结局（非无限循环）；
- 离线传输替身演练（全部通过，31 条新测试）：
  **成功 / 超时 / 认证失败(403,401) / 非法载荷 / 注入 / 越权引用 /
  来源不匹配(commit 不符) / 取消(步数预算)**——每条负控钉一个生效点。

**6. 真实 smoke（已真实跑通，非 Mock）**
- **凭据清单（一次列清）**：
  - 模型：`DASHSCOPE_API_KEY` ✅ 已在环境提供（阿里云百炼兼容模式）；
    模型 `qwen-plus`；**无需 blocked_real_smoke**；
  - GitHub：`GITHUB_TOKEN` ❌ 未设置——匿名限额 60 次/小时；
    本轮真实检索与重验在匿名限额内完成，限额耗尽情形已在"环境风险"记录；
- **真实端到端（完整闭环，已完成）**：
  - 检索：`pgvector/pgvector/README.md@7db2345e…`（42,305 字节，
    sha256 `54cb3a5f…`）、`qdrant/qdrant/README.md@6ab21cac…`（11,560 字节，
    sha256 `f1455d12…`）——均经 contents API 解析 commit 后取原文；
  - 决策：qwen-plus 真实调用（12 维度决策 + 重试）；
  - 编排：run_id `ma_f63636def8f3105b`，outcome=completed，
    verifier checks 六项全 true（digest/结构/完整性/证据/范围/来源审计），
    final_report_id `mafr_b7387f37cacf25bb`；
  - **独立重验（真实执行）**：重新取回两份固定 commit 原文重算 sha256 ⇒
    **双 MATCH**（54cb3a5f… / f1455d12…）；
- **环境风险（如实记录）**：本网络 `raw.githubusercontent.com` 不可达
  （超时/断连）——原文获取与重验均走 contents API（同固定 commit 等价字节）；
  本轮验收期间 GitHub 匿名限额（60/h）一度耗尽（0 remaining），
  触发运行显式 blocked（`no_snapshots` 原因可见）——这正是"远端失败显式失败"
  的实证；限额重置后可用同一命令复跑（每次完整 live run 仅 4 次 HTTP + ~12 次
  模型调用）。

### 攻击/演练场景 → 负控（P6 新增 31 条测试）

| # | 场景 | 测试 | 预期 |
|---|---|---|---|
| D1 | 原文成功取回 + 固定 commit URL | test_pinned_sources | 全文 + sha256 自洽 + 审计 |
| D2 | commit 解析超时 | test_pinned_sources | RetrievalTimeoutError（可重试闭集） |
| D3 | 认证失败 403 | test_pinned_sources | SourceHTTPError 显式失败 |
| D4 | 来源不匹配（声明 commit ≠ 解析结果） | test_pinned_sources | SourceResponseError（fail-closed） |
| D5 | >1MB/非 base64/rate-limit 载荷 | test_pinned_sources | 显式失败（不当零结果） |
| D6 | localhost/私网 | test_pinned_sources | 主机白名单拒绝 |
| D7 | 外部 worker 成功/超时/认证失败 | test_external_worker | 显式 RuntimeError（不回退 Mock） |
| D8 | 非法载荷（缺/未知字段/越界/非JSON） | test_external_worker | 协议违例拒绝 |
| D9 | 注入（服从注入的输出） | test_external_worker | 闭集拒绝 |
| D10 | 越权 task_refs | test_external_worker | ContextBoundaryError ⇒ blocked |
| D11 | 来源不匹配（范围外候选） | test_external_worker | verifier rejected |
| D12 | 取消（步数预算耗尽） | test_external_worker | blocked 可观测 |
| D13 | 未验证材料标注 | test_external_worker | verdict notes + report 标注 |
| D14 | CLI replay/offline 产物 + 确定性；live 缺配置即退出 | test_cli_entry | 见门禁 |

### 修改文件（sha256 前 16 位；新增 5 + 修改 6 + 新测试 3 个文件 31 条）

```
src/agentboard/cli.py                            F2123F2A06665E44  (新增)
src/agentboard/__main__.py                       945DD01ABA26271D  (新增)
src/agentboard/providers/pinned_sources.py       8F8607C61B1C5917  (新增)
src/agentboard/workers/external.py               5E9F1EB1403D4123  (新增)
src/agentboard/orchestrator/verifier.py          B67CD194BBCC333B  (external 分支 + source_audit 复核 + notes)
src/agentboard/orchestrator/research_adapter.py  3FD1D18BF96B764C  (provider_factory + source_audit/run_audit)
src/agentboard/runtime/decider_generated.py      C9943E95727CDBB3  (提示词加 claim 简洁指令；协议校验不变)
src/agentboard/README.md                         DC025460E5F5857E  (P6 章节 + 实测命令)
tests/providers/test_pinned_sources.py           346AA8A8BB6BBB1C  (新增 10 条)
tests/workers/test_external_worker.py            165F4C60784A3780  (新增 13 条)
tests/orchestrator/test_cli_entry.py             76A7FBFD68351A02  (新增 4 条)
```

### 门禁原始输出（fresh 重跑，全部 exit 0）

```
pytest                      → 1091 passed, 6 skipped in 14.22s（P5 为 1051+6：新增 40 条）
ruff check src tests tools  → All checks passed!
verify_manifest --check     → 19 entries verified, 0 violations
sign_fixtures --check（root / official）→ 双双通过
verify_sources --raw-dir .workbuddy/tmp/audit → 通过（4/0/0）
eval/research/run_eval.py   → 通过（10/10，P1A report_id 不变）
eval/official/run_eval.py   → 通过（6/6，rp_9e0057b9f989020f 稳定）
eval/live_research/run_eval.py → 通过（15/15，P3/P4 期望全命中）
eval/multi_agent/run_eval.py   → 通过（10/10，P5 期望全命中）
默认 network/LLM 0/0（NetworkGuard）；真实网络只发生在显式 live 命令与真实 smoke。
```

G-PRESERVE：fixtures/**、既有 eval 场景与期望、scripts/assets/materials 零改动；
EvidenceRef 内容寻址、attestation 信任契约、SnapshotStore、既有 report_id 语义
零触碰；零新增默认运行依赖（HTTP/JSON 全标准库；未加 extras）。

### 真实与离线结果对照

| 模式 | 结果 | 证据 |
|---|---|---|
| replay | completed，rp_30b82fa3b8f6148d，双跑一致 | `var/cli-replay/` |
| offline | completed，ma_f2c19e2b1430b11f，report.md 标注 scripted | `var/cli-offline/` |
| **live（真实）** | **completed，ma_f63636def8f3105b，final mafr_b7387f37cacf25bb，verifier 六项 checks 全 true，真实重验双 MATCH** | `var/p6-smoke/`（最终代码重跑产物见"环境风险"） |
| live 缺配置 | 退出码 2，一次性列缺失项，零网络 | 测试 D14 + 手工实测 |

### 剩余风险 / 未完成项

1. **GitHub 匿名限额**（60/h）：验收期间耗尽一次 ⇒ live 运行显式 blocked
   （`no_snapshots`，原因可观测）。设入 `GITHUB_TOKEN` 即提升限额；
   最终代码重跑被限额拦截，成功 smoke 的完整审计数据以本回执与
   上述记录为准（限额重置后同一命令可复跑刷新落盘产物）；
2. `raw.githubusercontent.com` 在本网络不可达——重验路径已切换为
   contents API（同固定 commit 等价字节，已双 MATCH 实证）；
3. 真实 ACP 协议适配器未实现（不自造接口冒充）；
4. live 模型输出有随机性 ⇒ live 结果不逐字节重复（任务书允许；稳定性以
   冻结输入/回放为准）；决策失败重试上限 4 次内偶发耗尽 ⇒ 显式 blocked
   （非静默），可加大 `--decision-retries`。

---

## 以下为历史回执（未改动）

## ⓿ P5 回执（2026-09-27 14:40，attempt 1/3）

### 任务与依据

按 `CURRENT_TASK.md`（stage: P5-multi-agent-orchestration-mvp，attempt: 1/3）执行；
先读 `PROJECT_RULES.md`、`BUG_EXPERIENCES.md`、`REVIEW_STATUS.md`（前置：P4 通过）。
一次性完成 8 项必须任务；未拆分、未中途交审、未进入 P6。

### 规则映射（R1–R7 → 本轮落点）

| 规则 | 落点 |
|---|---|
| R1 证明绑定本次实际数据 | verifier 独立重算 sha256(report_json) 比对载荷 digest；synthesizer 只消费 verdict=passed |
| R2 升格权威不可构造 | 编排层不产生任何升格/信任权威；verdict 是结论不是权威，report_id 仍由既有 content_projection 决定 |
| R3 文档约定 ≠ 机制保证 | 角色图 / 越权引用 / 过期卡片全部是代码级校验（计划层 + 派发层 + 写板层三道） |
| R4 禁止性约束由原语保证 | 复用 board 既有不变量（结果归属/最新版本检查），未绕过任何写板护栏 |
| R5 隔离作用域对齐 | worker 输入一律经 `build_worker_input`（只注入 depends_on 声明的上游）；范围声明 `candidate_ids:` 是结构化契约 |
| R6 唯一实现点 | verdict / final 载荷校验各有唯一实现点（`validate_verdict_payload` / `validate_final_payload`），orchestrator 与 synthesizer 共用 |
| R7 负向测试打在生效点 | 攻击矩阵 A1–A10（下表）各钉一个机制生效点 |

### 逐项交付（对 CURRENT_TASK「必须一次性完成」1–8）

**1. 原生 orchestrator（单次闭环）**
`src/agentboard/orchestrator/orchestrator.py`：`MultiAgentOrchestrator.run(goal)` 完成
plan → dispatch → retry/blocked → verify → synthesize 单次闭环；复用既有
`MockPlanner.propose_and_commit`（板护栏校验提交）、`board.*` 状态机（全部状态变化
经过既有护栏函数，未绕过）、`isolation.build_worker_input`（任务包收窄）、
`events.BoardEvent`（结构化事件）。主循环有步数预算（`max_loop_steps`），
杜绝无限循环（超限 ⇒ `loop_budget_exhausted` 可观测结局）。

**2. 四类角色（结构化契约交互）**
planner / researcher / verifier / synthesizer（`roles.py` 闭集 `P5_ROLES`）。
角色只能通过 `WorkerTaskInput` / `WorkerResult` 交互：输入经 isolation 收窄
（只见 depends_on 声明的上游），输出经 board 归属校验；`candidate_ids:` 上下文
声明是研究/验证范围的唯一来源（计划层校验存在性与格式）。

**3. 研究角色接入 P4 runtime / verifier 独立重验 / synthesizer 门禁**
- `RuntimeResearchWorker`：接入 P3/P4 `LiveResearchRunner`，默认决策器为
  `GeneratedDimensionDecider`（离线脚本 transport，闭集协议）；运行在
  `asyncio.to_thread` 中（runner 内部有 asyncio 桥接，避免嵌套事件循环）；
  运行失败 ⇒ 卡片失败（重试/blocked），不静默降级；
- `VerifierWorker`：**独立重验**——重算 `sha256(report_json)` 比对载荷
  `content_digest`（工件篡改在此被抓）、独立复核 integrity/failed_codes/
  report_id、逐条 supported finding 的证据引用（来源类型闭集 + 候选范围）、
  candidate_ids ⊆ 声明范围。如实边界：**不重新执行研究流水线**，验证的是
  产物自洽性；verdict=rejected 是结论不是异常 ⇒ synthesizer 必然失败 ⇒
  重试耗尽 ⇒ blocked——不可信结论不可能进入最终报告；
- `SynthesizerWorker`：只消费 verdict 载荷（闭集校验），任一 rejected ⇒
  `VerifierRejectedError` ⇒ 卡片失败；无上游 ⇒ `SynthesisError`。

**4. 默认离线 + 真实 opt-in 边界**
- 默认全流程离线：`MockPlanner` + `ScriptedReportWorker`（快速确定性替身，
  同一 report 载荷 schema）或 `RuntimeResearchWorker`（离线 provider + 脚本
  决策 transport）+ `VerifierWorker` + `SynthesizerWorker`；零网络零凭据；
- 真实外部子 Agent / ACP：**协议适配器如实登记为未完成**。编排核心只认
  `WorkerAdapter` 协议（既有槽位），外部实现注册即被使用；adapter 失败走
  任务板失败语义并记入事件序列，**不存在任何静默回退到 Mock 的代码路径**
  （编排器没有 fallback 逻辑，负控测试钉死 unknown_worker / adapter 失败
  的结局）。README 未把 Scripted/Mock 结果写成真实外部 Agent 能力。

**5. 状态与隔离边界（全部 fail-closed）**
依赖 DAG（复用 validate_model_board 的 DAG/未知依赖校验）、最大任务数
（板护栏 2..6）、每卡最大尝试次数（`OrchestratorConfig.max_card_attempts=3`，
经 `board.apply_failure` 新增的 `max_attempts` 参数注入，默认值不变）、
未知 worker、过期/不一致 TaskCard（board 既有不变量 ⇒ 映射
`board_invariant_violated` 运行失败，非重试路径）、越权字段（载荷 task_refs
⊆ deps ∪ self，违例 ⇒ `ContextBoundaryError` 卡片失败）、跨候选证据
（verifier 范围复核）、verifier 未通过（synthesizer 拒绝消费）——
事件序列稳定且含失败/重试/阻塞原因（时间戳不进重放口径）。

**6. 运行身份与审计**
编排 `run_id`（`ma_` 前缀）= 规范 JSON（schema v1）摘要：goal、planner 身份、
编排配置摘要、各 worker identity、任务板结构（task_id/agent/deps）、
**事件序列（不含时间戳）**、验证结论、最终 report_id。同输入稳定
（双跑确定性 + 单测）；goal / 配置 / worker 注册表任一变化 ⇒ run_id 变
（单测 + 跨场景 `run_id_differs_from` 核对）。审计含 board_state / 事件 /
verdicts / worker_identities；不含凭据与整段模型原文（worker 载荷只带
report_json 摘要，模型原文在 P4 层就不落盘）。

**7. 端到端离线评测（10 个场景，全部可机械判定）**
`eval/multi_agent/`：正常协作（runtime researcher 接入 P4）/ 依赖顺序
（事件子序列断言）/ worker 重试 / 三次失败 blocked（3 次 task_started +
task_failed + blocked）/ 越权上下文（task_refs 越权 ⇒ task_failed 含"越权"）/
不一致卡片结果（board_invariant_violated）/ verifier 拒绝（篡改 digest 被抓 ⇒
verifier_rejected ⇒ synthesize 卡 failed）/ synthesizer 越过 verifier
（plan_rejected，无任何 task_started）/ 计划未知依赖 / 配置变化 ⇒ run_id 变化。
全部双跑确定性；10/10 通过；network/LLM 计数 0。

**8. README + 本回执**：新增 "P5：multi-agent-orchestration-mvp" 章节——角色表、
verifier 如实边界、fail-closed 清单、run_id/审计、offline/opt-in 边界、
「仍未实现」三条（真实 ACP 适配器、verifier 不重跑流水线、无并行派发/跨 run 记忆）。

### 攻击场景 → 负控矩阵（R7）

| # | 攻击场景 | 负控 | 预期（钉死的生效点） |
|---|---|---|---|
| A1 | synthesizer 越过 verifier | ma_08 + 单测 | plan_rejected（派发前终止，零 task_started） |
| A2 | 计划引用未知依赖 / 未知角色 | ma_09 + 单测 | plan_rejected |
| A3 | 未知 worker | ma 场景注册缺失 + 单测 | 运行失败 unknown_worker（不猜测不 fallback） |
| A4 | 载荷越权 task_refs | ma_05 + 单测 | ContextBoundaryError ⇒ 卡片失败 ⇒ blocked |
| A5 | 结果与卡片不一致（伪造归属/过期版本） | ma_06 + 单测 | board_invariant_violated（运行失败） |
| A6 | 篡改产物但保留旧 digest | ma_07 + 单测 | verifier 重算摘要抓住 ⇒ rejected ⇒ synthesize 卡 failed ⇒ blocked |
| A7 | verifier 产出违例载荷 | 单测（MockWorker 当 verifier） | 卡片失败（重试耗尽）⇒ blocked，synthesizer 永不消费 |
| A8 | 卡片重试耗尽 | ma_04 | 3 次尝试 ⇒ failed ⇒ blocked（步数预算内终止） |
| A9 | run_id 身份碰撞 | 单测 + ma_10 | goal/配置/注册表任一变 ⇒ run_id 变；同输入稳定 |
| A10 | 事件时间戳破坏重放 | 单测 | 事件序列不含时间戳，双跑逐事件相等 |

### 修改文件（sha256 前 16 位；新增 8 个 + 修改 5 个 + 场景/期望 20 个）

```
src/agentboard/orchestrator/__init__.py         1363B966E24F68DC  (新增)
src/agentboard/orchestrator/errors.py           3E9DA03ADB13D3E7  (新增)
src/agentboard/orchestrator/roles.py            64F9F6809E016250  (新增)
src/agentboard/orchestrator/research_adapter.py A16F3A0B71E46D6A  (新增)
src/agentboard/orchestrator/verifier.py         D4B25A3C7211DA0C  (新增)
src/agentboard/orchestrator/synthesizer.py      83316E69552B49B0  (新增)
src/agentboard/orchestrator/orchestrator.py     562CD89F9B69F6E0  (新增)
src/agentboard/board.py                         3937C1AB1ACFF6E3  (apply_failure 加 max_attempts 参数，默认不变)
src/agentboard/events.py                        7B0C746F0C5345FC  (新增 3 个事件类型常量)
src/agentboard/workers/mock.py                  F5BF8DB323550886  (MockWorker.identity)
src/agentboard/README.md                        D71E4C25B1DCE669  (P5 章节)
tests/orchestrator/test_orchestrator.py         3B4B2EF9C20B117D  (新增 17 条)
eval/multi_agent/run_eval.py                    FDF97261B2B56133  (新增)
eval/multi_agent/scenarios/ma_01..10.json       (新增 10)
eval/multi_agent/expectations/ma_01..10.json    (新增 10)
```

### 门禁原始输出（fresh 重跑，全部 exit 0）

```
pytest                      → 1051 passed, 6 skipped in 25.38s（P4 为 1027+6：新增 24 条）
ruff check src tests tools  → All checks passed!
verify_manifest --check     → 19 entries verified, 0 violations
sign_fixtures --check（root）    → fixture 校验通过：结构 + 文件级 digest 全部一致
sign_fixtures --check（official）→ fixture 校验通过：结构 + 文件级 digest 全部一致
verify_sources --raw-dir .workbuddy/tmp/audit → 通过（4/0/0）
eval/research/run_eval.py   → 通过（10/10，expectations 冻结 ⇒ P1A report_id 不变）
eval/official/run_eval.py   → 通过（6/6，rp_9e0057b9f989020f 稳定复现）
eval/live_research/run_eval.py → 通过（15/15，P3/P4 期望全命中）
eval/multi_agent/run_eval.py   → 通过（10/10，双跑确定性，network/LLM 0/0）
```

G-PRESERVE：`fixtures/**`、`eval/research/**`、`eval/official/**`、scripts/assets/
materials 零改动；P1A/official/live（P3/P4 部分）期望与 report_id 全部不变；
EvidenceRef、attestation、SnapshotStore、GeneratedDimensionDecider 协议与既有
report_id 语义零触碰（`board.apply_failure` 仅新增带默认值的可选参数）；
未接长期记忆/FastAPI/LangGraph/无限自主循环；未新增默认运行依赖；
默认门禁 network/LLM 0/0。

### 剩余风险 / 未完成项

1. **真实 ACP / 外部子 Agent 协议适配器未实现**（编排核心已预留 WorkerAdapter
   协议槽位；本批未把 Mock/Scripted 结果写成真实外部 Agent 能力）；
2. verifier 独立重验的是**产物自洽性**（digest/完整性/证据结构/范围），
   不重新执行研究流水线——"研究过程可信"仍由 P2.2/P3/P4 的边界与门禁保证；
3. 调度为单线程顺序派发（无并行）；无跨 run 记忆——留待后续决策；
4. orchestrator 事件包含 BoardEvent 默认 `time.time()` 时间戳（可观测），
   重放口径（run_id 与事件序列比较）已显式剔除时间戳——如需完全零时钟事件，
   需改 BoardEvent 默认值（涉及 P0 契约，本批未动）。

---

## 以下为历史回执（未改动）

## ⓿ P4 回执（2026-09-27 01:50，attempt 1/3）

### 任务与依据

按 `CURRENT_TASK.md`（stage: P4-controlled-research-agent，attempt: 1/3）执行；
先读 `PROJECT_RULES.md`、`BUG_EXPERIENCES.md`、`REVIEW_STATUS.md`（末尾：P3 attempt 2
通过 + "当前明确边界：ScriptedDimensionDecider 不是自主推理"）。
一次性完成 9 项必须任务；未拆分、未中途交审、未进入 P5。

### 规则映射（R1–R7 → 本轮落点）

| 规则 | 落点 |
|---|---|
| R1 证明绑定本次实际数据 | 决策输入只能来自已核验语料（runner sections），不接触 Provider 原始响应；协议层 section_id 必须属于本次语料 |
| R2 升格权威不可构造 | 决策器不产生任何升格/信任权威；报告信任层级沿用既有 authored_summary 边界（不可升格） |
| R3 文档约定 ≠ 机制保证 | 协议校验是 `parse_generated_decision` 唯一实现点 + runner 强制（不是提示词里"请你遵守"） |
| R4 禁止性约束由原语保证 | 闭集键集合校验 + 量表/locator 精确匹配；违例永不"尽量接受" |
| R5 隔离作用域对齐 | 决策 transport 与检索 provider 分离（依赖方向 providers ← runtime 保持） |
| R6 唯一实现点 | 协议校验、决策预算、审计序列化各只有一个实现点；兼容 API transport 复用既有 HttpTransport（POST 支持为默认值兼容扩展） |
| R7 负向测试打在生效点 | 见下方攻击场景矩阵——每条负控钉一个机制生效点 |

### 逐项交付（对 CURRENT_TASK「必须一次性完成」1–9）

**1. GeneratedDimensionDecider（复用 DimensionDecider 契约）**
`src/agentboard/runtime/decider_generated.py`：实现 `decide(candidate_id, dimension_id, sections)`
协议；输入只接受 runner 传入的已核验语料切片 + 构造时显式传入的注册表量表——
**不接触 Provider 原始响应**（Provider 产物只能经 SnapshotStore→语料→loader 进入决策，
边界由 corpus.py 承担，本批未动）。返回 `DimensionDecision | None`（None ⇒ 既有
unsupported 语义），与脚本决策器同一契约，runner 无需感知决策器种类。

**2. 严格结构化决策协议（闭集校验，全部 fail-closed）**
`parse_generated_decision` 是协议校验**唯一实现点**。两种输出形态：
- 决策形态：键集合恰好 `{candidate_id, dimension_id, value, claim, section_id, confidence?}`；
  candidate/dimension 与请求逐字相等；value ∈ 量表；section_id ∈ 本次语料；
  claim 非空限长；confidence ∈ [0,1]；
- 显式弃权形态：`{insufficient_evidence: true, reason}` ⇒ None。
未知字段、缺字段、类型错误、跨候选、换维度、无效量表值、悬空 locator、
越界/非数值 confidence、超长输出——全部 `DecisionSchemaError` 拒绝。
协议违例可重试（`max_retries`），耗尽 ⇒ `DecisionGenerationError`。

**3. 默认离线 + 显式 opt-in 真实路径**
- 离线：`ScriptedDecisionTransport`（providers/decision_transport.py）——固定响应
  按调用次序消费、队列耗尽后重复最后一项、失败脚本先于响应；零网络零时钟零凭据；
- 真实：`CompatibleApiDecisionTransport`（OpenAI 兼容 chat/completions）——复用既有
  `HttpTransport`（`TransportRequest` 新增 `method`/`body` 字段，默认 GET/None 与
  P2.1 逐字节兼容，GET 带 body / 未知方法显式拒绝）；API key 只在 `complete()`
  调用时读环境变量，不进日志/快照/运行记录（测试断言凭据不出现在 URL/body）；
  真实 smoke（`test_generated_real_smoke.py`）需 `AGENTBOARD_REAL_MODEL_SMOKE=1`
  + 端点/模型环境变量，默认跳过；
- **零新增默认运行依赖**（纯标准库；pyproject 未改）。

**4. 运行身份与审计**
- `run_id`（schema 升 v2）新增：决策器 identity（transport_id + 配置摘要 + 量表，
  sha256 前 16 位）与最终决策的 confidence；与既有维度定义/权重字节、文档摘要、
  决策三元组并列。**反例回归**：同配置 ⇒ run_id 稳定；换配置（timeout）⇒ run_id 变；
  评测含跨场景 `run_id_differs_from` 机械核对；
- 审计：运行记录新增 `decision_identity`、`decision_budget_exhausted`、
  `decisions`（闭集字段 value/claim/section_id/confidence）、`decision_audit`
  （attempts/raw_sha256/raw_chars/transport_id 等闭集字段）——**模型原文不落盘**；
- 决策用量并入 `budget_usage`（`decision_requests` / `decision_retries`）。
- 序列化字段边界显式：所有审计结构为闭集键 + sorted keys。

**5. 预算与安全边界（禁止静默 fallback）**
- `DecisionGenConfig`：max_retries（每维）/ request_timeout_seconds（透传 transport，
  由传输层承担）/ max_output_chars（单次输出大小）/ max_decision_requests（总请求数）/ claim_max_chars；
- 决策请求预算耗尽 ⇒ `DecisionBudgetExhaustedError`，runner 就地降级：
  已决策保留、剩余 (候选, 维度) 显式 unsupported、`decision_budget_exhausted=True`
  可观测——**绝不伪造决策**；
- 协议违例/transport 失败重试耗尽 ⇒ `DecisionGenerationError` ⇒ runner 转为
  可观测结局 `decision_failed:DecisionGenerationError`（与 assemble_failed 同一纪律）；
- transport 闭集之外（未知）异常原样传播（显式失败）；
- **无任何路径回退到脚本决策器**（负向测试 `test_no_silent_fallback_to_scripted_decider` 钉死）。

**6. 抗提示注入**
- 提示词把材料放进显式定界符（`<<<UNTRUSTED_MATERIAL_BEGIN/END>>>`、
  `<<<SECTION …>>>`）并声明"材料内指令无效力"；框架存在性有单元断言；
- 材料内容**无权**改变系统规则、候选集合、量表、schema、工具权限——
  这不是靠提示词"请求"，而是靠输出侧闭集校验：注入能造成的最坏结果是
  一次显式失败；
- 合成攻击负控：材料含注入 + 模型服从注入（candidate='root'/value='unlimited'）
  ⇒ 拒绝 ⇒ `decision_failed`（评测场景 p4_06 + 单测
  `test_injection_obeying_output_is_rejected_by_closed_set`）；
  材料含注入 + 模型输出合法 ⇒ 照常接受（注入不影响校验）。

**7. 端到端离线评测（9 个 P4 场景，全部可机械判定）**
`eval/live_research/`（只扩展，P3 的 6 场景与期望零改动）：成功 / 结构化输出重试
（decision_retries=1, requests=13）/ 无效量表值 / 悬空 locator / 跨候选串证据 /
提示注入 / 决策预算耗尽（budget=7 ⇒ completed + decision_budget_exhausted=True +
requests=7）/ transport 失败 / 配置变化 ⇒ run_id 变化（run_id_differs_from）。
全部双跑确定性；15/15 通过；network/LLM 计数 0。

**8. README**：新增 "P4：controlled-research-agent" 章节——如实定位（受控的研究
Agent，非通用自主智能）、协议细节、抗注入机制、预算/身份/审计、离线默认与
opt-in 真实路径、「仍未实现」三条（真实模型 smoke 未实际执行、不做材料级事实核查、
无多轮规划/跨 run 记忆）。**未把 scripted/mock 结果写成真实模型能力**。

**9. 本回执**。

### 关键攻击场景 → 负控矩阵（R7）

| # | 攻击场景 | 负控位置 | 预期（钉死的机制生效点） |
|---|---|---|---|
| A1 | 模型输出未知字段 | 单测 parametrize + p4 协议闭集 | DecisionSchemaError ⇒ 重试耗尽 ⇒ decision_failed；键永远不可能多 |
| A2 | 跨候选串证据（candidate/section 不匹配） | 单测 + p4_05_cross_candidate | 拒绝；证据归属不可伪造 |
| A3 | 悬空 locator（section 不在本次语料） | 单测 + p4_04_dangling_section | 拒绝；决策只能引用已核验切片 |
| A4 | 无效量表值（含注入诱导） | 单测 + p4_03_invalid_value | 拒绝；量表不可被输出改变 |
| A5 | 提示注入（材料内指令 + 服从注入的输出） | p4_06_prompt_injection + 2 单测 | 最坏结果 = 显式失败；不可能改变候选/量表/schema |
| A6 | 决策请求预算耗尽 | p4_07_decision_budget + 单测 | 显式 unsupported 降级（不伪造、不 fallback），decision_budget_exhausted=True |
| A7 | transport 持续失败 / 未知异常 | p4_08_transport_failure + 2 单测 | 重试耗尽 ⇒ decision_failed；未知异常原样传播（不吞） |
| A8 | 换模型/配置复用 run_id | p4_09_run_id_config_change + 单测 | run_id 必变（decider identity 进哈希）；同输入稳定 |
| A9 | 凭据泄漏 | compat transport 单测 | key 只进 Authorization 头；不出现在 URL/body/记录 |
| A10 | 模型原文/材料整段落盘 | 审计字段闭集单测 | 运行记录只存 raw_sha256/raw_chars/attempts |
| A11 | 静默 fallback 到脚本决策器 | test_no_silent_fallback_to_scripted_decider | 生成失败 ⇒ decision_failed，绝不产出脚本值 |

### 修改文件（sha256 前 16 位；新增 9 个 + 修改 7 个 + 场景/期望 18 个）

```
src/agentboard/runtime/decider_generated.py     52198A70CEC4F6AC  (新增)
src/agentboard/runtime/decider.py               BE66D94A8D448E72  (confidence + identity)
src/agentboard/runtime/errors.py                C17EF2B3F14501B3  (决策异常族)
src/agentboard/runtime/runner.py                DFC3D873CC2268B5  (决策阶段/身份/审计)
src/agentboard/runtime/mock_provider.py         F15693FF9707A64B  (timeout 记录)
src/agentboard/providers/decision_transport.py  EEEC91149C2649D9  (新增)
src/agentboard/providers/interface.py           8D4C6994393E206C  (method/body, 默认兼容)
src/agentboard/providers/http_transport.py      D572B41576FB504B  (POST 透传)
src/agentboard/README.md                        90AC9445DD872406  (P4 章节 + 已知边界)
tests/runtime/conftest.py                       9D4E6A124193921F  (decider 覆盖参数)
tests/runtime/test_generated_decider.py         43BD93B5ADC7C3A4  (新增 27 条)
tests/runtime/test_runner_p4.py                 DCD7C7737CB5EBC8  (新增 7 条)
tests/runtime/test_generated_real_smoke.py      431B08E272BBA099  (新增 opt-in smoke)
tests/runtime/test_runner_pipeline.py           350374BFD2585791  (run_id 反例适配)
tests/providers/test_decision_transport.py      9930FC15FC9348F3  (新增 13 条)
eval/live_research/run_eval.py                  7419BFE1BC983FA8  (P4 场景支持 + 指标)
eval/live_research/scenarios/p4_01..09.json     (新增 9)
eval/live_research/expectations/p4_01..09.json  (新增 9)
```

### 门禁原始输出（fresh 重跑，全部 exit 0）

```
pytest                      → 1027 passed, 6 skipped in 14.46s
                              （P3 attempt 2 为 979+5：新增 42 passed + 1 opt-in skip）
ruff check src tests tools  → All checks passed!
verify_manifest --check     → 19 entries verified, 0 violations
sign_fixtures --check（root）    → fixture 校验通过：结构 + 文件级 digest 全部一致
sign_fixtures --check（official）→ fixture 校验通过：结构 + 文件级 digest 全部一致
verify_sources --raw-dir .workbuddy/tmp/audit → 通过（4/0/0）
eval/research/run_eval.py   → 通过（10/10，expectations 冻结 ⇒ P1A report_id 不变）
eval/official/run_eval.py   → 通过（6/6，rp_9e0057b9f989020f 稳定复现）
eval/live_research/run_eval.py → 通过（15/15：P3 6 个期望全命中 + P4 9 个全绿；
                                    p4_scenarios_passed=9=total；network/LLM 0/0）
```

G-PRESERVE：`fixtures/**`、`eval/research/**`、`eval/official/**`、scripts/assets/
materials 零改动；P1A/official/live（P3 部分）期望与 report_id 全部不变；
EvidenceRef、attestation、I10、SnapshotStore、`load_snapshot_set` 语义零触碰；
未接 ACP/LangGraph/FastAPI/长期记忆/无限自主循环；未新增默认运行依赖；
默认门禁 network/LLM 0/0。

### 剩余风险 / 未完成项

1. **真实模型 smoke 未实际执行**（需要用户提供端点/模型/可选凭据；
   接线正确性由 MockTransport 层的 POST/凭据/超时/异常映射测试离线覆盖）；
2. `CompatibleApiDecisionTransport` 只适配 OpenAI 兼容 chat/completions 的
   `choices[0].message.content` 形态；其它 API 形态留待实际接入时扩展；
3. 受控生成决策不做材料级事实核查；live 材料 `authored_summary` 不可升格的
   信任边界不变，结论可信性仍由人工复核 + 既有完整性门禁决定；
4. `DimensionDecision.confidence` 进入 run_id 与审计但不参与 report_id
   （报告内容不含置信度）——如后续要把置信度写进报告，需走设计门禁。

---

## 以下为历史回执（未改动）

## ⓿ P3 attempt 2 回执（2026-09-26 23:10，四条意见整批修复）

### 任务与依据

按 `CURRENT_TASK.md`（stage: P3-live-research-mvp，attempt: 2/3，「本轮一次性修复」节）
执行；先读 `PROJECT_RULES.md`、`BUG_EXPERIENCES.md`、`REVIEW_STATUS.md`（末尾 P3
attempt 1 复核：2×P1 + 2×P2/范围）。一次性修完、不拆分、不中途交审；未进入 P4。

### 规则映射（R1–R7 → 本轮落点）

| 规则 | 落点 |
|---|---|
| R1 证明绑定本次实际数据 | 修①：run_id 内容寻址扩展（维度定义字节 + 文档摘要 + 最终决策三元组），反例回归钉死 |
| R7 负向测试打在生效点 | 修①②③各配独立负控/反例测试；修④用文档对照 + 无新增声明 |
| R6 唯一实现点 | 超时透传复用既有 `TransportRequest.timeout_seconds` 语义，无第二份实现 |
| R2/R3/R4/R5 | N/A——本批不涉及升格权威、绑定机制、发布原语、环境隔离 |

### 修① [P1] run_id 覆盖全部影响报告内容的输入

`runner.py::_run_id(materials, decisions)` 现在哈希：brief / 候选 / 维度标识 /
captured_at / **注册表 JSON 字节摘要**（完整维度定义与权重）/
**每份已验证文档的 content_sha256** / **最终决策三元组**
（`candidate|dimension|value|claim|section_id`，排序展开）。
失败结局路径（no_snapshots / insufficient_candidates）无决策、无报告，
传空 decisions 并在调用点注释声明。run_id 在决策定稿后重算（决策影响身份）。

**回归反例**（`test_runner_pipeline.py`）：
- `test_run_id_changes_when_only_the_decision_changes`：材料逐字节相同，分别只改
  value / claim / section_id ⇒ 三种情形 run_id 与 report_id 都必须变化；
- `test_run_id_changes_when_dimension_definitions_change`：注册表 JSON 权重显式配平
  后改动（0.20→0.25 / 0.15→0.10）⇒ run_id 变化；
- `test_report_id_is_stable_across_replays`：同输入两遍 ⇒ 同 run_id（原有，保留）。

### 修② [P2] 快照统计按实际 SnapshotStore 结果累计

runner 新增 `store_stats` 累计器，`_store_documents` 的 written / duplicate
逐份累加（含缺口补检步），`snapshots_written` / `snapshots_duplicate`
从实际结果取值——**成功步骤数不再冒充快照数**。全部结局路径统一使用。

**回归测试**：
- `test_snapshot_stats_count_actual_store_writes`：8 份唯一文档 ⇒ `written=8`、
  `duplicate=0`，且与 SnapshotStore 目录实际文件数一致；同时断言与成功步骤数（20：
  12 计划步含零结果 + 8 补检步）不相等——正面钉住"禁止以步骤数代替快照数"；
- `test_duplicate_documents_are_deduplicated_in_store`（扩展）：真重复场景断言
  `written=4`、`duplicate=1`、Store 目录 4 份文件。

### 修③ [P2] request_timeout_seconds 真正接线（选了"接线"而非删除）

- `Provider.search` 协议新增 `timeout_seconds: float | None = None` 关键字参数
  （默认 None ⇒ 实现自选默认；全部既有调用方兼容）；
- `GitHubRepositorySearchProvider.search` 接收并写进 `TransportRequest.timeout_seconds`
  （None ⇒ 构造时的默认）；
- runner 每次检索透传 `self._budget.request_timeout_seconds`；
- `ScriptedSearchProvider` 记录每次调用收到的 timeout（`timeouts` 列表）供断言。

**回归测试**：
- providers 层：`test_search_forwards_explicit_timeout_to_transport`——MockTransport
  断言实际进入 `TransportRequest` 的 timeout == 3.5；不传 ⇒ 回落构造默认值；
- runtime 层：`test_budget_timeout_reaches_the_provider`——预算 3.25 秒 ⇒ Provider
  记录到的每次调用 timeout 均为 3.25。

### 修④ 范围边界如实化（不引入模型/依赖，只修正文档定位）

README P3 节重写定位：
- 标题从"可运行的自主研究流水线"改为 **"live retrieval orchestration（检索编排）流水线"**；
- 新增"如实定位（不可夸大）"段：`ScriptedDimensionDecider` **不是自主推理**，
  维度取值由场景作者显式声明；本阶段是"真实检索接线 + 确定性流程脚手架"，
  **不能自主理解资料并推导技术结论**；受控生成决策器是**明确登记的未完成能力**
  （P4 候选），不通过改文案伪装成已完成；
- 「已知边界」对应条目同步改写；流水线表格的决策器/快照/超时行同步修正；
  顺带修正 smoke 文件名引用（`test_live_real_smoke.py`）。
- 代码零改动（决策器与 decider.py 文档串原本已如实）。

### 附带修复（tests/runtime/** 范围内）

`tests/runtime/test_live_real_smoke.py` 原从 `agentboard.research.sources` import
`build_default_provider`——该模块并不导出它（真开启 smoke 时必然 ImportError）。
改为从 `agentboard.providers` 导入。

### 修改文件（sha256 前 16 位）

```
src/agentboard/runtime/runner.py                     9DA3A7D7D663F45F
src/agentboard/runtime/mock_provider.py              F15693FF9707A64B
src/agentboard/providers/interface.py                16431F2939FD9E27
src/agentboard/providers/github_search.py            03B8B80D5227BA47
src/agentboard/README.md                             C0260CD01CB44C14
tests/runtime/conftest.py                            427E4152E1ECC3E6
tests/runtime/test_runner_pipeline.py                6B7EA64140BB7746
tests/runtime/test_live_real_smoke.py                18D6837986872BC2
tests/providers/test_transport_and_provider.py       585970EAA0E4DD49
```

### 门禁原始输出（fresh 重跑，全部 exit 0）

```
pytest -q            → 979 passed, 5 skipped in 13.45s（attempt 1 为 974+5：新增 5 条回归）
ruff check src tests tools → All checks passed!
verify_manifest --check    → 19 entries verified, 0 violations
sign_fixtures --check（root）    → fixture 校验通过：结构 + 文件级 digest 全部一致
sign_fixtures --check（official）→ fixture 校验通过：结构 + 文件级 digest 全部一致
verify_sources --raw-dir .workbuddy/tmp/audit → 通过（4/0/0）
eval/research/run_eval.py  → 通过（10/10，expectations 冻结未动 ⇒ P1A report_id 不变）
eval/official/run_eval.py  → 通过（6/6，rp_9e0057b9f989020f 稳定复现）
eval/live_research/run_eval.py → 通过（6/6，含双跑确定性；report_id 期望全部命中
                                    ⇒ run_id 扩展未改变报告内容）
```

G-PRESERVE：fixtures 零字节改动；两套 report_id 复现；P1A/official/live eval 不变；
`fixtures/**`、`eval/research/**`、`eval/official/**`、scripts/assets/materials、
P2.2 attestation/EvidenceRef/I10/`load_snapshot_set` 语义全部未触碰；
未新增依赖、未联网、未接模型；未进入 P4。

### 剩余风险 / 未完成项

1. **受控生成决策器未实现**（本轮范围裁定：明确登记为未完成能力，P4 候选）；
   当前 `ScriptedDimensionDecider` 只做显式声明 + 合法性校验，README 已如实标注。
2. 真实 Provider smoke（opt-in）本轮未启用执行，接线正确性由离线等价测试 +
   透传断言覆盖；真跑仍需用户显式 `AGENTBOARD_REAL_SMOKE=1`。
3. `ScriptedSearchProvider.timeouts` 记录 `None` 的形态未单独测试（当前运行时
   恒传具体值；协议允许 None 供其它调用方使用）。

---

## 以下为历史回执（未改动）

## ⓿ P3-live-research-mvp 回执（2026-09-26 14:20，attempt 1/3）

### 任务与依据

按 `CURRENT_TASK.md`（stage: P3-live-research-mvp）执行；先读 `PROJECT_RULES.md`、
`BUG_EXPERIENCES.md`、`REVIEW_STATUS.md`。前置确认：P2.2-4 已过 Codex 复核。
未进入 P4；未改 `fixtures/**`、`eval/research/**`、`eval/official/**`、
scripts/assets/materials；未接 ACP/LangGraph/FastAPI/长期记忆；未新增第三方依赖；
未回退 P2.2-3 的 S0.5 / I10 / `load_snapshot_set` / L5 守卫语义。

### 规则映射表（PROJECT_RULES R1–R7，执行入口第 2 条）

| 规则 | 攻击场景（本次验证） | 回归测试 | 结果 |
|---|---|---|---|
| R1 | Provider 产物绕过快照/验证边界直接成为证据 ⇒ 不可能（语料链路是唯一路径）；证据摘录必须可回溯到语料 section | `test_evidence_boundary_provider_output_never_becomes_evidence_directly` | ✅ |
| R2/R3 | 决策产出越界（值不在量表 / 引用不存在 section）⇒ `EvidenceBoundaryError` 显式失败 | `test_invalid_decision_value_is_explicit_boundary_failure` + runner 校验 | ✅ |
| R4 | 未知 Provider 错误被吞 / 预算耗尽被静默 / 降级不可观测 | `test_unknown_provider_error_fails_loudly` + 三个预算测试 + 失败原因断言 | ✅ |
| R5 | N/A —— 默认路径零网络零凭据；真实 Provider 为 opt-in（`test_live_real_smoke.py`，需 `AGENTBOARD_REAL_SMOKE=1`） | 模块级 skipif | N/A（默认离线） |
| R6 | 重试闭集 / 决策器协议 / Provider 替身均为单一定义来源；eval 复用 research runner 的 NetworkGuard 与场景装载 | `eval/live_research/run_eval.py` 复用实现 | ✅ |
| R7 | 七类评测场景（成功/重试/超预算/无结果/来源不可解析/证据不足/确定性）各钉一条语义；确定性=双跑逐字段一致 | `eval/live_research/run_eval.py` 6 场景 ×2 + `tests/runtime/` 18 条 | ✅ |

### 核心改动

| 文件 | sha256[:16] | 内容 |
|---|---|---|
| `src/agentboard/runtime/errors.py` | `E6A61C0C9DE464A0` | runtime 异常族（PlanError/PlanValueError、BudgetExhaustedError、ProviderStepError、EvidenceBoundaryError、RunFailedError），与 research/providers 两族不互相继承 |
| `src/agentboard/runtime/budget.py` | `40A70BCDCD1FBAD9` | **计数式预算**（步数/请求数/内容字符/重试；不读时钟）；`BudgetLedger` 超限抛 `BudgetExhaustedError`，usage 可观测 |
| `src/agentboard/runtime/plan.py` | `EE239A5318671D50` | 研究计划：候选 × 维度排序展开（无查询改写）；步骤状态机 `pending→succeeded/failed/skipped`（失败/跳过必须带原因） |
| `src/agentboard/runtime/decider.py` | `7961FCCCC3378943` | 可插拔维度决策器协议 + 离线脚本实现（`ScriptedDimensionDecider`，未声明 ⇒ None ⇒ 缺口/unsupported，不猜） |
| `src/agentboard/runtime/mock_provider.py` | `A7B6E72F6CAE724B` | `ScriptedSearchProvider`（Provider 协议的确定性离线替身：脚本文档 + 失败队列 + 调用计数） |
| `src/agentboard/runtime/corpus.py` | `D547356B29ACEAB6` | **快照/验证边界**：Provider 文档 → SnapshotStore 去重落盘 → fixture 格式语料（`source_kind=authored_summary`，live 材料不可升格）→ 既有 loader（C7 生效）。`dedupe_documents`（内容哈希去重+确定性排序）；决策合法性校验 |
| `src/agentboard/runtime/runner.py` | `BCEB0C35F2894D89` | 主循环六段：计划→检索（重试闭集/预算/未知错误显式失败）→去重快照→缺口补检→证据映射→确定性组装；`run_id` 内容寻址派生；run_record 落盘 |
| `eval/live_research/run_eval.py` | `FA4FA9066F697D5E` | 端到端离线评测：6 场景 ×2 双跑（确定性），期望逐字段断言，NetworkGuard/场景装载复用 research eval（R6） |
| `eval/live_research/scenarios+expectations`（12 个 JSON） | — | 成功 / 重试后成功 / 步数预算耗尽 / 全零结果 / 来源不可解析（重试耗尽）/ 证据不足（I4） |
| `tests/runtime/`（conftest + 4 文件） | — | 18 条：计划/预算单测、成功管线、边界性质（证据回溯语料）、重试/预算/无结果/未知错误/候选降级、**opt-in 真实 smoke**（`test_live_real_smoke.py`，默认 skip） |
| `src/agentboard/README.md` | `796076646327067C` | 新增 P3 章节（流水线六段 / 证据边界 / 预算与失败降级语义 / live 评测）、目录树、「已知边界」live MVP 边界条目 |

### 关键语义（供复核重点）

1. **Provider 产物不直接成为证据**：runner 全程**零次**构造 `EvidenceRef`；
   唯一路径 = SnapshotStore 落盘 → 语料构建（fixture 格式）→ 既有 loader（C7）→
   `FixtureResearchWorker` 受控工厂 → `assemble_report`。测试断言语料 section 文本
   与 Provider 文档内容一一对应、报告证据摘录等于语料文本的规范截断。
2. **live 材料在 MVP 信任模型下不可升格**：语料 `source_kind` 恒为
   `authored_summary`（机器收集、待人工复核）——`research_grade` 对 live 内容不可达。
   这是与 P2.2 信任边界同一条线的**诚实边界**，不是缺陷。
3. **预算耗尽是定义好的结局**：runner 捕获 `BudgetExhaustedError` ⇒ 剩余步骤
   `skipped`（原因 `budget_exhausted:*`），用已取得材料继续降级路径；每层禁止吞错。
4. **未知 Provider 错误显式失败**：可重试异常闭集之外的任何异常 ⇒
   `ProviderStepError`（带原始异常链），打断本次运行。
5. **组装期失败也走可观测结局**：组装异常（如 I4 证据不足）⇒ `outcome=failed`、
   原因 `assemble_failed:<类型>:<失败码>`，评测可逐字段断言。

### 门禁（9 条，原始结果；fresh 全绿）

| # | 命令 | exit | 原始输出（关键行） |
|---|---|---|---|
| V1 | `pytest`（addopts -q） | 0 | `974 passed, 5 skipped in 12.44s` |
| V2 | `ruff check src tests tools` | 0 | `All checks passed!` |
| V3 | `tools/verify_manifest.py --check` | 0 | `19 entries verified, 0 violations` |
| V4 | `tools/sign_fixtures.py --check`（双根） | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` ×2 |
| V5 | `tools/verify_sources.py --raw-dir .workbuddy/tmp/audit` | 0 | `来源核对：4 份通过，0 份缺失，0 个问题` |
| V6 | `eval/research/run_eval.py` | 0 | `scenarios_passed=10/10`（expectations 逐项不变） |
| V7 | `eval/official/run_eval.py` | 0 | `official_scenarios_passed=6/6`、`rp_9e0057b9f989020f` 稳定、全指标达标 |
| V8 | `eval/live_research/run_eval.py`（新增） | 0 | `live_scenarios_passed=6/6`、`network_calls=0`、`llm_calls=0`、确定性双跑一致 |
| V9 | ruff 复跑（含 runtime/tests） | 0 | `All checks passed!` |

**G-PRESERVE**：G1 `fixtures/**` 零改动（V3/V4 佐证）；G2 双签名通过；G3
P1A 10/10 + 官方 `rp_9e0057b9f989020f` 复现；G4 既有常驻测试继续通过。
**pyproject.toml 未改动**（real_smoke marker 已存在；无新增依赖）。

### 攻击场景 → 覆盖对照（R7 反空转自查）

| 攻击 | 被谁拦 | 评测/测试 |
|---|---|---|
| Provider 输出绕过边界直接进报告 | 语料链路是唯一路径 + runtime 零 EvidenceRef 构造 | `test_evidence_boundary_*` |
| 决策编造量表外的值 / 悬空 section 引用 | `EvidenceBoundaryError` | `test_invalid_decision_value_*` |
| 传输抖动 | 重试（计入预算） | `test_retry_then_success_*` |
| 重试耗尽 | 步骤 failed（原因可观测），运行继续 | `test_retry_exhaustion_*`、eval 05 |
| 步数/请求/内容预算耗尽 | 剩余步骤 skipped（原因可观测），降级报告 | 三个预算测试、eval 03 |
| 全部查询零结果 | `outcome=failed/no_snapshots`（不崩溃） | `test_no_results_*`、eval 04 |
| 来源不可解析 | 重试耗尽 ⇒ failed（原因可区分） | eval 05 |
| 证据不足 | I4 ⇒ `assemble_failed:...:I4`（显式） | eval 06 |
| 未知 Provider 错误 | `ProviderStepError`（禁止吞错） | `test_unknown_provider_error_fails_loudly` |
| 重放不一致 | run_id/report_json/步骤状态双跑比对 | `test_report_id_is_stable_across_replays` + eval 双跑 |

### 剩余风险 / 未完成项

1. **维度取值决策器是注入点**：MVP 内只有脚本决策器（离线测试/评测）；
   "从内容自动得出取值"需受控生成，属 P4 候选 —— 当前**不伪造**该能力。
2. **live 语料的 `source_kind` 借用 `authored_summary`**（既有闭集内唯一合法选择），
   语义上更准确的名字是"机器收集、待人工复核"；运行元数据（source_note/README）
   已显式说明。若要正名需改 research/config 闭集（不在本轮允许范围）。
3. **运行时产物目录**：eval 的语料/快照落在 `var/live-research-eval/`（运行时产物，
   不进基线）；真实运行的默认快照根建议 `var/snapshots/`（README 未强制）。
4. **超时语义**：由 Provider/传输层参数承担；runner 不读时钟（确定性纪律），
   "步骤级墙钟超时"未实现（如需，P4 引入时钟隔离层）。

### 阶段边界

✅ 未进入 P4；未接 ACP/LangGraph/FastAPI/长期记忆；`dependencies = []` 未变。
等待 Codex 独立复核，不自行宣布通过。

---

## ❶ P2.2-4 回执（2026-09-26 13:10，attempt 1/3）

### 任务与依据

按用户新下发的 `HANDOFF_TO_CODER.md`（P2.2-4，status: ready）执行；先读
`PROJECT_RULES.md` / `BUG_EXPERIENCES.md` / `REVIEW_STATUS.md` / `P2.2_PLAN.md`
（§P2.2-4、§6.1.1 G-PRESERVE、§6.7 评测指标、§6.5 兼容性）。
未进入下一阶段；未改 `fixtures/**`、`eval/research/**`、`tools/verify_sources.py`。

**执行入口更新（2026-09-26 13:43，按用户指令）**：`CURRENT_TASK.md`（stage: P2.2-4）
成为本轮唯一执行入口（历史 handoff 仅作审计资料）。本轮按其"必须完成 7 项"逐项复核
并对全部 7 条门禁做了**fresh 重跑**（结果见下），结论：7 项全部已交付、门禁全绿，
无新增改动需求。逐项对照：

| # | CURRENT_TASK 必须完成项 | 状态 | 交付位置 |
|---|---|---|---|
| 1 | 原件存在且核对通过后生成正式 attestation（缺失不得伪造 passed） | ✅ | `attestations/official.sources.v1.json`（真工具 + `.workbuddy/tmp/audit` 4 份原件，`passed`，4 docs / 3 files；缺失时工具走 incomplete 且 W4 禁止改写） |
| 2 | `attestations/README.md`（信任边界 / W1–W4 / 重生成 / 二分 / live_reverify） | ✅ | `attestations/README.md`（五节齐全） |
| 3 | `live_reverify`（本地重算摘要与行号摘录，missing/problem 分开） | ✅ | `sources.live_reverify` + 交叉核对 / 二分测试 |
| 4 | `eval/official/` 场景、期望、runner（不改 `eval/research/`） | ✅ | 6 场景 + 6 期望 + `run_eval.py`（复用机制，"新增场景集"路径） |
| 5 | 官方评测门禁：证明缺失 / 过期 / 摘要篡改 / fixture 篡改 / 证据回溯 / G4 | ✅ | `tests/test_official_eval_gate.py` ①–⑤⑥–⑨ + G4 + eval 场景 03/04/05 |
| 6 | README P2.2 章节 + 已知边界 | ✅ | `src/agentboard/README.md`（P2.2 节 + 边界两条 + 目录树 + 怎么跑） |
| 7 | 交付 HANDOFF_FROM_CODER.md（改动 / 规则映射 / 攻击场景 / 测试原始结果 / 剩余风险，awaiting_review） | ✅ | 本文件 |

**fresh 门禁重跑（本轮，全部 exit 0）**：V1 `pytest` → `955 passed, 4 skipped`；
V2 `ruff check` → `All checks passed!`；V3 `verify_manifest --check` → `19 entries verified,
0 violations`；V4 双签名（root + official）→ 双双通过；V5 `verify_sources` → 4 通过 / 0 缺失 /
0 问题；V6 research eval → 10/10 不变；V7 official eval → 6/6、`rp_9e0057b9f989020f` 稳定、
全指标达标。G-PRESERVE：G1 fixtures 零改动（V3/V4 佐证）、G2 双签名、G3 双 report_id 复现、
G4 由 `test_g4_attestation_presence_does_not_affect_g2_g3` 常驻验证。

### 规则映射表（PROJECT_RULES R1–R7，执行入口第 2 条）

| 规则 | 攻击场景（本次验证） | 回归测试 | 结果 |
|---|---|---|---|
| R1 | 证明工件与 fixture 字节绑定：改 1 字节 fixture ⇒ 自绑定失效（⑨）；移走工件 ⇒ absent（⑦） | `test_official_eval_gate.py` ⑦⑨ + 既有绑定测试 | ✅ |
| R2/R3 | attestation 工厂/loader-only 绑定不可绕过（attempt 3 语义保留，未回退） | 既有 L5 守卫 + 955 全绿 | ✅ |
| R4 | W1–W4 写入策略保持；missing 与 problem **分开报**（不合并、不静默） | 篡改①（problem 非 missing）+ live_reverify 二分测试 | ✅ |
| R5 | N/A —— 本阶段不涉及环境隔离 / smoke / 网络 / 凭据 | — | N/A（纯本地路径） |
| R6 | live_reverify 与 verify_sources 为**登记在册的两份实现**（工具刻意不 import 库），一致性用同一语料交叉核对测试钉住 | `test_live_reverify_matches_tool_verdict_on_same_corpus` | ✅ |
| R7 | 篡改检测 ①–⑤ 每条打在对应机制生效点（原件字节 / 行号切片 / 摘要 / commit-pinned URL / 文件级 digest） | `test_official_eval_gate.py` ①–⑤ | ✅ |

### 交付内容

| 文件 | sha256[:16] | 内容 |
|---|---|---|
| `attestations/official.sources.v1.json` | `ED2751EEF1F19016` | **首次落正式工件**：真工具对真实原件核对生成，`overall_result=passed`、4 documents、3 snapshot_files、零时间字段；W1–W4 遵守（原子排他创建） |
| `attestations/README.md` | `95AE242E2D2C011B` | 信任模型（**无数字签名** ⇒ 过程保证：git 历史 + 评审 + G-PRESERVE，明确声明非密码学保证）、W1–W4 语义、再生成方式、missing/problem 二分、`live_reverify` 用法 |
| `src/agentboard/research/sources.py` | `7E3DC68507BDE9E5` | 新增 **`live_reverify(sources, raw_dir)`**：当场用本地原件重算（raw_sha256 → git blob sha1 → 行号切片 vs sections 逐字节），返回 `(ok, problems, missing)`，二分报；与工具同语义（同 count_lines/slice 约定） |
| `eval/official/run_eval.py` | `FB6C9913C0B5803B` | 官方评测 runner（计划"新增场景集"路径）：复用 research runner 的 NetworkGuard / 场景装载 / `_compare_report`（不复制实现，R6）；attestation 四模式（official / none / expired / index_mismatch）；§6.7 全部指标 + 硬门禁断言 |
| `eval/official/scenarios/*.json`（6 个） | — | 默认报告 / research_grade+正式证明 / 无证明（absent）/ 版本过期 / index 不符（工厂层 fail-closed）/ 证据回溯到 SourceDocument |
| `eval/official/expectations/*.json`（6 个） | — | 全部钉 `rp_9e0057b9f989020f`；缺陷场景断言 `strict_error_codes=[I10]` + detail 含可区分原因 |
| `tests/test_official_eval_gate.py` | `172BD30E03B5E43E` | 篡改检测 ①–⑤（tmp 克隆）+ 升格 ⑥–⑨ + live_reverify 交叉核对与二分 + **G4 落地**（移走 attestations ⇒ G2/G3 不变，try/finally 恢复），共 12 条 |
| `src/agentboard/README.md` | `F013B96E82D8FBBE` | 新增 P2.2 章节（两谓词分工 / attestation / research_grade / 官方评测）、「怎么跑」加官方 eval、目录树更新、「已知边界」新增信任模型与 research_grade 边界两条 |
| `HANDOFF_TO_CODER.md` | `96DAE3CCD1CFB15C` | 按指令更新为 P2.2-4 唯一任务单（含规则接线） |

### G-PRESERVE 全条验证

| 条 | 验证方式 | 结果 |
|---|---|---|
| G1 | `fixtures/**` 文件集合与字节零改动（未执行任何写/重签；双签名佐证） | ✅ |
| G2 | `sign_fixtures.py --check` + `--root fixtures/official --check` 双双通过（未跑 `--write`） | ✅ |
| G3 | P1A eval 10/10 逐项不变；官方 `rp_9e0057b9f989020f` 在官方 eval 6 场景稳定复现（含 research_grade 两态同 id） | ✅ |
| **G4**（本阶段首次可执行） | `tests/test_official_eval_gate.py::test_g4_attestation_presence_does_not_affect_g2_g3`：移走 `attestations/` ⇒ G2 退出码 0/0、G3 eval 指标逐字节一致；工件恢复 | ✅ |

### 门禁（8 条，原始结果）

| # | 命令 | exit | 原始输出（关键行） |
|---|---|---|---|
| V1 | `pytest`（addopts -q） | 0 | `955 passed, 4 skipped in 10.43s` |
| V2 | `ruff check src tests tools` | 0 | `All checks passed!` |
| V3 | `tools/verify_manifest.py --check` | 0 | `19 entries verified, 0 violations` |
| V4 | `tools/sign_fixtures.py --check` | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` |
| V5 | `tools/sign_fixtures.py --root fixtures/official --check` | 0 | 同上 |
| V6 | `tools/verify_sources.py --raw-dir .workbuddy/tmp/audit` | 0 | `来源核对：4 份通过，0 份缺失，0 个问题` |
| V7 | `eval/research/run_eval.py` | 0 | `scenarios_passed=10, scenarios_total=10, determinism_repeat_rate=1.0, network_calls=0, llm_calls=0`（10 场景 expectations 逐项不变） |
| V8 | `eval/official/run_eval.py`（新增） | 0 | `official_scenarios_passed=6/6, official_report_id_stable=true, promotable_evidence_rate=1.0, authored_summary_in_research_grade=0, attestation_present=true, attestation_overall_result=passed, attestation_self_binding_ok=true, attestation_unavailable_reason=absent, source_anchor_verified_rate=1.0, source_anchor_problem_count=0, source_anchor_missing_count=0, evidence_resolved_to_document_rate=1.0, official_evidence_trusted_rate=1.0, network_calls=0, llm_calls=0` |

### 关键语义说明（供复核重点）

1. **scenario 05（index 不符）的失败层是工厂而非 I10**：`load_snapshot_set` 在文件级绑定
   失败时直接拒绝（fail-closed，无半成品）——mismatch 不会拖到组装期才暴露。
   这是 H6 设计的直接后果，场景按 `domain_error / SnapshotSchemaError` 断言。
2. **原件文件名约定**：本机 audit 目录的文件名带 `@<commit>` 后缀；工具与
   `live_reverify` 的 name 兜底匹配按 `document.path` 末段精确匹配。测试的 tmp 克隆
   夹具里去掉后缀使两条匹配路可用（语义不变）；正式语料靠 sha256 主匹配。
3. **`promotable_evidence_rate` 口径**：在**有正式证明的 research_grade 报告**上计算
   （fixture 证据中候选为官方原文的比例）= 1.0；无证明场景由
   `attestation_unavailable_reason=absent` 单列 —— 两者方向都钉住（§6.7 注）。

### 剩余风险 / 未完成项

1. `live_reverify` 与 `tools/verify_sources.py` 是登记在册的两份判定实现
   （工具刻意不 import 库）—— 一致性靠交叉核对测试维持，非结构上单实现点（R6 例外，已登记）。
2. 原件目录 `.workbuddy/tmp/audit/` 被清理时：官方 eval 的 source_anchor 指标退化为
   missing 单列（problem 仍为 0），相关 pytest 用例 skip 并注明 —— 这是计划的预期行为，
   但"锚点全绿"结论在该机器上不成立，复核时需留意环境差异。
3. `attestation_unavailable_reason` 目前只覆盖 absent 场景（官方 eval 无
   incomplete/mismatch 的 I10 路径 —— mismatch 被工厂层提前拦下）；incomplete/mismatch
   的可区分性由 pytest 层测试覆盖（A4 系列）。
4. G4 测试每次跑两遍 research eval（子进程），是测试套件中最慢的一条（约 +8s）。

### 阶段边界

✅ 未进入 P2.2 之后的阶段；未改 `eval/research/**`、`tools/verify_sources.py`、
`fixtures/**`；`dependencies = []` 未变；全程离线。等待 Codex 独立复核，不自行宣布通过。

---

## ❶ P2.2-3 attempt 3 回执（2026-09-26 11:40，B6 修复）

### 任务与依据

按用户授权执行 attempt 3；先读 `PROJECT_RULES.md`、`BUG_EXPERIENCES.md`、
`REVIEW_STATUS.md`、`P2.2-3_REDESIGN.md`（rev 2，design_status: passed）。
实现范围 = rev 2 设计的 D1–D6；未进入 P2.2-4，未改 fixtures/业务无关文件。

### 规则映射表（PROJECT_RULES R1–R7，执行入口第 2 条）

| 规则 | 攻击场景（本次验证） | 回归测试 | 结果 |
|---|---|---|---|
| R1 | A1 内存改 section / A2 换目录重放 / A2b 绑定后磁盘被换 / A3 覆盖缺口 / A4 证明不完整 / A6 仅篡改 EvidenceRef | `tests/research/test_attestation_binding.py`（P0×2 + A1–A6 + 单读契约/TOCTOU 专项，共 16 条） | ✅ 全绿 |
| R2/R3 | SnapshotSetBinding 工厂外构造被拦 | `test_static_guards.py` L5 扩展（3 条新测试） | ✅ |
| R4 | 全部失败路径 fail-closed、报告内容不清洗 | 每条负控的两态断言 + 内容契约断言 | ✅ |
| R5 | N/A —— 本阶段不涉及环境隔离 / smoke / 网络 / 凭据 | — | N/A（纯本地路径） |
| R6 | 绑定重验、截断、digests 提取均为单实现点 | 既有唯一性硬门禁 + 纯核心共享 | ✅ |
| R7 | 负控打在机制生效点（见各测试 docstring 反空转自查） | 同 R1 | ✅ |

### 核心改动（对齐 rev 2 设计）

| 文件 | sha256[:16] | 改动 |
|---|---|---|
| `src/agentboard/research/snapshot.py` | `0559264D1C4CC45F` | **D1**：`FixtureSnapshot` 加载期戳印 `fixture_relpath` / `fixture_sha256`（默认值字段，不进 `to_dict`，P1A 零影响）；**D2**：新增 `SnapshotSetBinding`（内部类型）+ `SnapshotSet.binding_context` + loader-only 工厂 `load_snapshot_set()`（绑定不通过即抛错，不产出半成品） |
| `src/agentboard/research/attestation.py` | `C3966FBFEF789468` | **D3**：`_binding_reason` 重构为纯核心 `binding_reason_from_bytes(attestation, index_bytes, file_bytes)` + 磁盘包装 `verify_attestation_against`（`load_attestation` 与组装期共用，R6）；新增 **H3b** `documents_blocking_reason`（documents ↔ provenance 并集精确等集 + 逐项 raw_sha256/blob_sha/result，`promotion_blocking_reason` 与 S0.5 共用）；解析层加固：`from_dict` 拒绝空 `documents` |
| `src/agentboard/research/integrity.py` | `F1D1E489EFA1E045` | **D4**：新增 `BindingRecheck` 结果容器；`_attestation_blocking_reason` 增加 `binding_context is None ⇒ unbound`（H6）；I10 增加 `recheck` 参数：文件层/声明层失败作用于全部候选、内容级按候选、证据级（H5 `evidence_mismatch`）对回已核验字节；明细去重 |
| `src/agentboard/research/assemble.py` | `16FCB06180B2BC36` | **D5**：新增 S0.5 `_reverify_binding`（单次读取契约：每文件恰 `read_bytes()` 一次，缓冲同时用于 sha256 与 JSON 解析；H1 戳印↔覆盖集、H2/H3 文件层、H3b 声明层、H4 内容级逐字段比对）；research_grade 下跳过 `_verify_fixture_locators`（证据核对以 I10 语义执行，H5）；默认路径零改动 |
| `tests/research/test_attestation_binding.py` | `B69B61C1FFDFA593` | **D6**（新增 16 条）：P0 真实官方集（无原件时 skip）/P0b 合成正控、A1 内存篡改、A2 工厂层+组装层重放、A2b TOCTOU、A3 三向覆盖、A4a–d 不完整证明、A5 unbound、A6 仅篡改 EvidenceRef（两态 + 默认路径对照）、单次读取计数、two-timing 磁盘不可表达 |
| `tests/research/test_research_grade.py` | `B05A955D6EAD2E23` | ⑧ 两个正控改走 `load_snapshot_set` 工厂；字段闭集断言加入 `binding_context` |
| `tests/research/test_promotion_gate.py` | `30D672E3214554FC` | ⑩"漏文档"负控从 `documents=[]`（现属解析层 schema 错误）改为缺一条文档（声明层等集核对） |
| `tests/test_static_guards.py` | `358FAC3C066FF346` | L5 扩展：`find_out_of_band_snapshot_set_binding` + 白名单 + 正负控制 + 实际构造点一致性 |

**方案偏离登记（3 处，均为实现细节精化，无语义冲突）**：
1. `documents_blocking_reason` 返回 `(reason, detail)` 二元组（设计写 `-> str`）—— A4 要求明细含缺失/多出的 `source_id`，二元组保住"单实现点"同时把细节带进 I10；`promotion_blocking_reason` 只取 reason，对外契约不变。
2. 声明并集提取在 `assemble._declared_digests_from_verified` 内实现（语义镜像 `sources.snapshot_set_document_digests` 的"并集 + 冲突即拒"）：后者输入是 `OfficialSources`/`OfficialProvenance`，无法直接消费 S0.5 单次读取缓冲的解析对象 —— 输入类型不同导致各自实现提取、共享语义。
3. D5 原写"`_verify_fixture_locators` 增加可选已核验基准入参"，实现为"research_grade 跳过默认校验、证据核对以 I10 语义执行"—— 行为等价（A6 明确要求 research_grade 不抛输入校验异常），且默认路径代码完全未动。

### 门禁（7 条，原始结果）

| # | 命令 | exit | 原始输出（关键行） |
|---|---|---|---|
| V1 | `pytest`（addopts -q） | 0 | `943 passed, 4 skipped in 9.50s` |
| V2 | `ruff check src tests tools` | 0 | `All checks passed!` |
| V3 | `tools/verify_manifest.py --check` | 0 | `19 entries verified, 0 violations` |
| V4 | `tools/sign_fixtures.py --check` | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` |
| V5 | `tools/sign_fixtures.py --root fixtures/official --check` | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` |
| V6 | `tools/verify_sources.py --raw-dir .workbuddy/tmp/audit` | 0 | `来源核对：4 份通过，0 份缺失，0 个问题`（原件在本机存在，非 skip） |
| V7 | `eval/research/run_eval.py` | 0 | `scenarios_passed=10, scenarios_total=10, determinism_repeat_rate=1.0, integrity_checks_executed=63, network_calls=0, llm_calls=0, fixture_locators_resolved=195/195` |

**不变式**：官方/P1A `report_id` 复现（正控 P0 断言 research_grade 与默认组装 report_id 相同；
eval 10/10 逐条命中）；`fixtures/**`、`scripts/`、`assets/`、`materials/`、`evidence.py`、
`contracts.py`、`models.py`、`tools/verify_sources.py`、`pyproject.toml` 零改动（V3–V5 佐证）；
`attestations/` 未创建；截断唯一实现点硬门禁仍为 1。

### 负控如何钉住机制（反空转自查，R7）

- A1 回退 H4 现读现比 ⇒ `tampered_in_memory` 断言变红；
- A2/A2b 回退组装期重验（信任 binding_reason）⇒ `mismatch`/`disk_mismatch` 断言变红；
- A3 删覆盖集检查 ⇒ `uncovered_candidate` 断言变红；
- A4① 删 schema 空文档约束 / A4②③ 删 H3b 等集核对 ⇒ 对应断言变红（A4b 还钉住"文件层不看 documents"这一缺口本身）；
- A6 把 `evidence_mismatch` 改回异常路径 ⇒ `strict=False 必须返回报告`断言变红（同时对照默认路径仍抛 `DanglingLocatorError`，钉死语义拆分）；
- 单次读取：计数断言每文件恰 1 次；two-timing 磁盘测试证明第二次读取不存在 ⇒ TOCTOU 不可表达。

### 剩余风险 / 未完成项

1. **`fixture_fingerprint` 与纯核心并存**：前者保留供既有测试与展示使用（逐文件读一次）；
   比对规则唯一实现于纯核心。若复核认为应彻底合并，属小改动。
2. **非 factory 的 `SnapshotSet(policy=research_grade, attestation=已绑定)` 现在判 `unbound`**
   —— 这是 H6 的设计意图（同源性不可证明），但它使 `sources.snapshot_set()` 无法单独支撑
   research_grade 正控；如复核希望 `OfficialSources.snapshot_set()` 直接产出合法上下文，
   需要给它接 `load_snapshot_set`（会扩大改动面，未擅自执行）。
3. `P2.2_PLAN.md §P2.2-3` 判据段仍为旧语义（attempt 2 起已偏离，设计开放问题 2 待裁定）。
4. `attestations/` 正式工件与官方场景集评测仍留 P2.2-4。

### 阶段边界

✅ 未进入 P2.2-4；未生成正式 attestation；未改 README。等待 Codex 独立复核，不自行宣布通过。

---

## ❶ P2.2-3 design-gate rev 2 回执（2026-09-25 21:55，design-only，零代码改动）

### 复核意见 → 修订对照（`REVIEW_STATUS.md` design-gate 复核三点）

| # | 复核意见 | 修订落点（`P2.2-3_REDESIGN.md`） |
|---|---|---|
| 1 | **[P1] 不完整 attestation 仍可能通过**：`documents=[]` 时 loader 仍 `binding_reason=""`；A4 未写清拦截机制 | 新增 **H3b 声明层完整性**子跳：组装期将 `attestation.documents[]` 与当前 provenance 声明并集（由 `snapshot_set_document_digests()` 从 H4 已核验内容计算）做**精确等集 + 逐项 raw_sha256/blob_sha/result** 核对；唯一实现点 = 从 `promotion_blocking_reason` 抽出的共享纯函数 `documents_blocking_reason`（loader 侧与组装期共用，D3）；解析层同步加固：`from_dict` 拒绝空 `documents`。A4 改为**三层负控**（①空文档解析层死 ②缺/多文档等集死 ③result/摘要不符死），各原因可区分 |
| 2 | **[P1] 摘要与解析两开存在组装内 TOCTOU** | H2 改为**单次读取契约**：每个文件单次组装内恰读一次，raw bytes 存不可变缓冲，**摘要与解析基于同一份缓冲**（H2/H4 同源）；D3 的实现相应改为"纯核心 `binding_reason_from_bytes(…bytes…)` + 磁盘包装"；D6 增加 TOCTOU 专项测试（monkeypatch 读文件模拟两开之间被替换，断言单读契约下攻击不可表达） |
| 3 | **[P2] 仅篡改 EvidenceRef 的语义未钉死** | 新增 **A6** 负控（snapshot/attestation 不变，仅改 EvidenceRef excerpt/locator）；H5 **钉死语义**：research_grade 下一律 **I10 两态失败**（原因 `evidence_mismatch:<cid>/<locator>`），**不是**抛输入校验异常——防止 strict=False 的"I10 未通过报告"契约被异常路径绕过；默认路径既有行为不变（P1A 字节稳定）。D6 增加专项断言（strict=False 必须返回报告） |

### 同步更新

- 攻击矩阵 5 负控 → **6 负控 + 1 正控**；反空转自查补 A4①（schema 约束删掉即变红）、A6（改回异常路径即变红）。
- I10 原因闭集增加 `evidence_mismatch`（`uncovered_evidence` 并入，不单列）；D5 的 S0.5 细化为 6 步（单读 → 文件层 → 声明层 → 内容级 → 证据级）。
- `P2.2_PLAN.md` 仍保持未改写（开放问题 2 维持，待复核裁定后统一改）。

### 自查与边界

- 本轮**只改 `P2.2-3_REDESIGN.md` 与本回执**；未改 `src/` / `tests/` / `fixtures/` / `tools/`；未生成 attestation；未进入 P2.2-4；**P2.2-3 实现保持 blocked**。
- 规则映射表（R1–R7，R5 N/A）随矩阵更新同步维护于设计文档 §6。

### 请求裁决

1. rev 2 设计是否通过；通过后是否授权 attempt 3 编码（需用户明确授权 + 新任务单）。

---

## ❶ P2.2-3 design-gate 回执（2026-09-25 21:40，design-only rev 1，零代码改动）

### 交付物

- **`P2.2-3_REDESIGN.md`**（新增，唯一改动文件）：P2.2-3 重设计技术方案。
- 本次**未改**任何 `src/` / `tests/` / `fixtures/` / `tools/` / README；未运行实现性修改；
  未生成任何 attestation（含 tmp）；未进入 P2.2-4；未解除 P2.2-3 的 blocked。

### 方案要点（详见设计文档）

1. **诊断**：attempt 2 被重放绕过的三个缺口 —— G-1 内存快照与磁盘字节无对应性、
   G-2 绑定是过去时刻事实且组装期不重验、G-3 证据摘录核对基准是内存自身。
2. **绑定链 6 跳**（H1–H6）逐跳定义了权威数据 / 校验位置 / 失效行为；核心是三层**组装期现读现算**：
   文件级（现读磁盘现算 sha256 比对 attestation 登记）、内容级（内存快照逐字段 == 现读解析结果，
   直接堵 B6 的内存篡改）、证据级（摘录基准切换为已核验磁盘内容）。`fixture_root` 由
   loader-only 工厂 `load_snapshot_set()` 与 attestation **同源一次性绑定**，杜绝参数级"绑定 A 现算 B"。
3. **显式不构成证明清单**：相同 `snapshot_set_id`、相同 `source_kind`、"曾经 load_attestation 成功"、
   SnapshotSet 声明字段、loader 戳印字段（定位为索引而非权威）。
4. **攻击矩阵 5 负控 + 1 正控**（内存改 section / 旧证明重放与换目录 / 绑定后磁盘 TOCTOU /
   覆盖不全三向 / attestation 不完整 / unbound），每项含 strict 两态断言 + 内容契约断言
   （失败报告内容逐字节保留、两态 report_id 相同）；各负控钉一个机制生效点（反空转自查附）。
5. **API 变更与文件清单**：`FixtureSnapshot` 加载期戳印（默认值字段，P1A 零影响）、
   `SnapshotSetBinding`（L5 守卫扩展的 loader-only 内部类型）、`verify_attestation_against`
   （与 `load_attestation` 共用同一测量实现——R6）、I10 原因闭集扩展
   （uncovered_candidate / disk_mismatch / tampered_in_memory）、S0.5 组装期重验步骤。
   `content_projection` / `is_trusted_evidence` / `evidence.py` / `contracts.py` / `models.py` 不动。
6. **P1A / report_id 字节稳定**：结构保证（S0.5 仅 research_grade 执行、新字段带默认值、
   内容投影不含 integrity）+ 机械验证清单（eval 10/10、官方 report_id 复现、两态同 id）。
7. **信任模型**：不声称抵御能同步重签仓库的攻击者（过程保证拦截）；保证命题限定为
   "组装时刻内存数据 == attestation 时刻核验的字节"。
8. **R1–R7 映射表**：R1/R2/R3/R4/R6/R7 适用（落点已列），R5 N/A（本阶段不涉及环境隔离/smoke）。
9. **开放问题 4 项**待复核裁定（regression_replay 是否工厂化、P2.2_PLAN 判据段同步改写、
   to_dict 是否导出新戳印、tmp 合成集合规确认）。

### 自查（对照 PROJECT_RULES.md 执行入口）

| 项 | 状态 |
|---|---|
| 先读 PROJECT_RULES.md 并逐条对照 | ✅（映射表在设计文档 §6） |
| 规则→攻击场景→回归测试映射 | ✅（§2 矩阵即未来的 `test_attestation_binding.py` 用例清单；**尚未实现**，属 attempt 3 编码范围） |
| N/A 标注 | ✅ R5 N/A + 理由 |
| 禁止事项 | ✅ 零代码改动；设计文档中所有机制均标注"设计，未实现"，未把建议写成已实现事实 |

### 请求裁决

1. 设计通过与否；通过后是否授权 attempt 3 编码（需用户明确授权 + 新任务单）。
2. 开放问题 1–4（设计文档 §7）。

---

# 历史回执（未改动）：P2.2-3 needs_fix 修复（attempt 2/3）

## ⓿ P2.2-3 needs_fix 修复（2026-09-25 17:43，attempt 2/3）

### 复核 P1 的复现（先复现再修）

按 `REVIEW_STATUS.md` 的路径实测复现：真实 `fixtures/official` + 无证明 +
`SnapshotSet(promotable={cid: True})` + `strict=False` ⇒ `integrity.passed=True`、
`report_id=rp_9e0057b9f989020f` —— 伪造映射推翻了两条核心承诺。**攻击成立，修法如下。**

### 修复：升格权威不再可伪造（loader-only 绑定原则 + 组装期重算）

| 位置 | 修前 | 修后 |
|---|---|---|
| `snapshot.py::SnapshotSet` | 公开构造器接受 `promotable: Mapping[str, bool]` —— 升格权威是可构造字段 | **字段已删除**（现仅 `snapshot_set_id` / `policy` / `attestation`）；构造器签名里不再存在伪造入口 |
| `integrity.py::check_i10_non_promotable_support` | 直接信任 `promotable.get(cid) is True` | **按实际数据重算**：① attestation 权威 —— 必须存在且只能来自 `load_attestation()`（`binding_reason=""`；缺失/未绑定/版本过期/集合不符/未通过分别给 absent/unbound/version_expired/mismatch/incomplete）；② 实际快照 —— 候选 `source_kind` 取自**参与组装的快照本身**，只有 `official_documentation_excerpt` 可升格；两个维度都核算并写进明细 |
| `assemble.py` | `check_i10(draft, snapshot_set)` | 传入 `snapshots`：`check_i10(draft, snapshot_set, snapshots)` —— I10 拿到的是**参与组装的真实快照** |
| `sources.py::snapshot_set()` | 预计算 `promotable`/`promotable_reasons` 映射 | 只打包"目录声明的集合标识 + 调用方显式提供的（必须来自 `load_attestation()` 的）证明"；不再产出任何可被下游信任的布尔映射 |

**`strict=False` 契约保持**：I10 失败时 `strict=True` 抛既有 `ReportIntegrityError`、
`strict=False` 返回报告（`integrity.passed=False`、`failed_codes` 含 I10、`detail_of` 有
candidate/dimension/finding/原因明细）。证明缺失与"来源是人工摘要"是**两个事实**，
明细里合并呈现（如 `absent; source_kind='authored_summary'（人工摘要不可作研究级支撑）`）。

### 修复验证（原攻击路径必须失效）

```
SnapshotSet 字段: ['snapshot_set_id', 'policy', 'attestation']
  → promotable 已不在构造器签名中: True
  传 promotable → TypeError ✓
  无证明 + strict=True → ReportIntegrityError: I10_NON_PROMOTABLE_SUPPORT ✓
  无证明 + strict=False → passed=False, detail 原因=absent ✓
  report_id（同内容） = rp_9e0057b9f989020f（不变）✓
```

### 新增/更新的负向测试（`tests/research/test_research_grade.py`，9→10 条）

| 测试 | 断言 |
|---|---|
| `test_forged_promotable_true_cannot_pass_on_real_official_set` **（P1 回归）** | **真实官方集**：① `SnapshotSet` 字段闭集断言（无 promotable/promotable_reasons —— 伪造入口不存在）；② 无证明 + strict=True 抛 `ReportIntegrityError` 含 I10；③ strict=False 报告 `passed=False`、原因 `absent` |
| `test_parsed_but_unbound_attestation_is_fail_closed` | **"仅解析、未绑定"的证明必须被拒**：真工具对真实原件生成有效证明 → `from_dict` 解析（内容相同但未绑定）→ 对照 `load_attestation` 绑定版有效 ⇒ 证明"无效只来自未绑定"；组装两模式都拒绝、原因 `unbound`；`dataclasses.replace(att, binding_reason="")` 抛 `TypeError`（构造后不可翻转） |
| `test_promotion_verdict_is_deterministic_after_construction` | 同内容两次组装 I10 明细逐字相同；`SnapshotSet` 字段闭集 `{snapshot_set_id, policy, attestation}`；frozen 赋值被拒（可变输入无法在构造后翻转结果） |
| ④⑤（更新） | 真实人工摘要失败的原因现在来自**实际快照重算**：明细含 `source_kind='authored_summary'` 与 finding_id/dimension_id 三段定位 |
| ⑦⑧（更新） | ⑦ 无证明的官方集 `strict=True` 抛错（即 P1 攻击路径）；⑧ `load_attestation` 绑定证明 ⇒ 通过且 `report_id` 不变 —— 通过与拒绝由**同一重算逻辑**区分，不再依赖任何声明 |

**反空转自查**：上述每条负向测试都对应一次真实失败路径；P1 回归测试若把修复回退
（恢复 promotable 字段），字段闭集断言与 TypeError/ReportIntegrityError 断言会同时变红。

## 门禁（attempt 2/3 修复后，原始结果）

| # | 命令 | exit | 原始输出（关键行） |
|---|---|---|---|
| V1 | `pytest -q` | 0 | `924 passed, 4 skipped in 7.69s` |
| V2 | `ruff check src tests tools` | 0 | `All checks passed!` |
| V3 | `tools/verify_manifest.py --check` | 0 | `19 entries verified, 0 violations` |
| V4 | `tools/sign_fixtures.py --check` | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` |
| V5 | `tools/sign_fixtures.py --root fixtures/official --check` | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` |
| V6 | `tools/verify_sources.py --raw-dir .workbuddy/tmp/audit` | 0 | `来源核对：4 份通过，0 份缺失，0 个问题`（原件在本机存在，非 skip） |
| V7 | `eval/research/run_eval.py` | 0 | `scenarios_passed=10, determinism_repeat_rate=1.0, integrity_checks_executed=63, network_calls=0, llm_calls=0` |

**不变式**：官方 `report_id = rp_9e0057b9f989020f` 复现；P1A 10 场景逐条命中；`fixtures/**`、
`scripts/`、`assets/`、`materials/`、`evidence.py`、`contracts.py`、`models.py`、
`tools/verify_sources.py`、`pyproject.toml` 零改动；`attestations/` 未创建；
硬门禁（截断唯一实现点）仍为 `1`。

**方案偏离（需复核裁定）**：方案 §P2.2-3 的 I10 判据原文是"promotable 映射缺键即失败"；
修复后该映射**整体移除**，fail-closed 语义平移到"证明缺失/未绑定/过期/不符/未通过"。
这是按复核修复意见"不要将可由调用方直接构造/修改的 Mapping 当作升格权威"执行的自然结果，
`P2.2_PLAN.md` 未同步改写（如需我把 §P2.2-3 判据段改写为新语义，说一声）。

## 本轮改动文件

| 文件 | sha256[:16] | 改动 |
|---|---|---|
| `src/agentboard/research/snapshot.py` | `9a29c46133bb644c` | `SnapshotSet` 重做：删 promotable/promotable_reasons，增 `attestation` 字段（类型校验 + 延迟导入防环） |
| `src/agentboard/research/integrity.py` | `5692ffa69a56a2f6` | I10 重写为"按实际快照 + loader-only 证明重算"；新增 `_attestation_blocking_reason` |
| `src/agentboard/research/assemble.py` | `c5838040992c334e` | I10 调用传入 `snapshots` |
| `src/agentboard/research/sources.py` | `e5aa33efd0c6bc6e` | `snapshot_set()` 不再预计算映射 |
| `tests/research/test_research_grade.py` | `2908d4ae1db6a89b` | 全面对齐新契约 + 3 条新负向测试 |
| `tests/research/test_snapshot_set_isolation.py` | `b1889585208d447f` | `_declare` 对齐新构造器 |
| `HANDOFF_FROM_CODER.md` | — | 本回执 |

---

# 历史回执（未改动）：P2.2-3 初版（attempt 1/3）


## 1. 修改文件（本轮 P2.2-3）

**生产代码 4 个**：

| 文件 | sha256[:16] | 改动 |
|---|---|---|
| `src/agentboard/research/snapshot.py` | `114b03a7c29744a1` | ① `FixtureSnapshot` 增 `snapshot_set` 戳印（loader 从 `catalog.snapshot_set` 戳印，**归属的唯一合法来源**；`""` = 目录未声明、归属不可证明）；② 新增 `SnapshotSet` 冻结数据类（id/policy/promotable/reasons + 构造期校验）；③ 新增 `merge_snapshots()` —— 同名候选**无法证明同源即拒绝**（合并点防线） |
| `src/agentboard/research/integrity.py` | `72394ea70ed150e9` | `INTEGRITY_CODES`（固定 I1–I9）**不变**；新增 `CONDITIONAL_INTEGRITY_CODES = ("I10_NON_PROMOTABLE_SUPPORT",)` 与 `VALID_INTEGRITY_CODES`；`IntegrityReport.with_check()`；`check_i10_non_promotable_support()` 纯函数 |
| `src/agentboard/research/assemble.py` | `eea1ddf1dd494c89` | `assemble_report` 增可选 `snapshot_set=None`（默认逐字节不变）；声明期归属核验（`_verify_snapshot_set_membership`，fail-closed）；`preflight_snapshots` 增**可证明混集**检测（≥2 个不同非空戳印即拒）；`policy=research_grade` 时在 I1–I9 后追加 I10 |
| `src/agentboard/research/sources.py` | `a8f3be0d6829994c` | `OfficialSources` 增 `fixture_root` 字段（loader 填充）、`snapshot_set_id` 属性、`snapshot_set(policy, attestation=None)` —— **候选 `promotable` 的唯一合法来源**（逐候选重算 `is_promotable_source`，原因经 `promotion_blocking_reason` 区分） |

**测试 3 个**（新增 2 + 显式登记 1）：

| 文件 | sha256[:16] | 内容 |
|---|---|---|
| `tests/research/test_snapshot_set_isolation.py` | `fbce2edcec712f59` | 方案用例 ①②③ + 混集负向（7 条） |
| `tests/research/test_research_grade.py` | `e19c3d2ae2d77aea` | 方案用例 ④–⑨（8 条） |
| `tests/research/test_provider_boundary.py` | `b0e4c56a26d55507` | `OfficialSources` 公开 API 闭集显式登记新方法 `snapshot_set`（该测试的设计意图：新公开方法必须被评审看见；已确认它仍非证据工厂） |

## 2. 关键语义

| 机制 | 语义 |
|---|---|
| **归属证明** | 快照的 `snapshot_set` 由 loader 从 `INDEX.json` 戳印；声明 `snapshot_set` 时，每份参与组装的快照戳印必须与声明**逐字相等**。`""`（未声明）或其它值 ⇒ `CandidateCoverageError` 且**点名冲突候选**。绝不凭 `candidate_id`/`source_id` 相同推断同源 |
| **合并点防线** | `merge_snapshots(*mappings)`：同名候选必须逐字段相等（可证明同源）才放行；实测拒绝真实 official×research 的全部 3 个同名候选 |
| **可证明混集** | 映射内部出现 ≥2 个不同非空戳印 ⇒ 数据自证的混集，无论是否声明都拒绝（`preflight_snapshots`） |
| **I10 触发条件** | 仅 `policy == research_grade` 时由组装追加；检查序列 I1–I9 **不变**（`verify_integrity` 未改，`INTEGRITY_CODES` 未改） |
| **I10 判据** | 每条 supported finding 的支撑候选（自身 + fixture 证据指向的候选）必须 `promotable=True`；缺映射/缺键 ⇒ 失败（fail-closed）；明细格式 `candidate/dimension/finding: 原因`（原因 absent/incomplete/version_expired/mismatch/unbound） |
| **内容零改动** | 不过滤/不改写 recommendations、supporting、证据、评分、排名、policy；`content_projection()` / `is_trusted_evidence()` / EvidenceRef 哈希与来源闭集全部未动 ⇒ 同一内容下默认组装、`regression_replay`、`research_grade`（无论 strict）的 `report_id` 完全相同 |
| **strict 语义** | 唯一定义处不变：`strict=True` 抛既有 `ReportIntegrityError`（failed_codes 含 I10）；`strict=False` 返回报告（`integrity.passed=False`、`detail_of(I10)` 完整明细）。未新增异常族 |

## 3. 测试（15 条新增，覆盖方案 ①–⑨）

| 方案用例 | 测试（`test_snapshot_set_isolation.py` / `test_research_grade.py`） | 结果 |
|---|---|---|
| ① 单集正常组装逐字节相同 | `test_single_declared_set_is_byte_identical_to_default`（regression_replay，全报告 to_dict 相等） | ✅ |
| ② 两集混入拒绝并指出冲突候选 | `test_merge_of_two_real_sets_rejects_same_name_conflict`（**真实 official×research**，3 个同名候选逐个复现）+ `test_provable_internal_mixing_is_rejected_even_without_declaration`（两套合成集不同戳印） | ✅ |
| ③ 声明字段不进 content_projection | `test_declared_fields_do_not_change_report_id` + ⑥⑧ 的 report_id 断言 | ✅ |
| ④ 真实人工摘要被拒（strict） | `test_authored_summaries_fail_research_grade_strict`（真实 `fixtures/research` 卡 + 声明身份的克隆快照） | ✅ |
| ⑤ strict=False 不抛、有明细 | `test_authored_summaries_fail_research_grade_non_strict`（断言 candidate/dimension/finding 三段 + 原因） | ✅ |
| ⑥ 两模式 report_id 相同 | `test_strict_and_non_strict_share_the_same_report_id`（strict 抛错 ⇒ 与同内容默认组装比对；并验证默认组装 integrity 全绿） | ✅ |
| ⑦ 官方集无 attestation 被拒 | `test_official_set_without_attestation_fails_research_grade`（原因 `absent`，promotable 由 `OfficialSources.snapshot_set` 派生） | ✅ |
| ⑧ 有效证明可通过 | `test_official_set_with_passed_attestation_passes_and_keeps_report_id`（**真工具 + 真实原件**，证明落 tmp）+ `test_synthetic_official_set_with_passed_attestation`（确定性版本，不依赖本机原件） | ✅ |
| ⑨ promotable 缺失 fail-closed | `test_missing_promotable_mapping_is_fail_closed`（两模式都验证，缺键候选逐个被点名） | ✅ |
| 附加 | `test_merge_identical_snapshots_is_idempotent`、`test_unprovable_membership_is_rejected_at_assemble`、`test_cross_set_stamp_is_rejected_at_assemble`、`test_regression_replay_policy_never_runs_i10` | ✅ |

**负向控制的真实性**：⑦⑧ 的 `promotable` 不是手填的 —— 由 `OfficialSources.snapshot_set()` 逐候选重算得出（⑦ 全 False 原因 absent；⑧ 全 True）；④ 的违规明细断言到每条 supported finding 的 finding_id。

## 4. 门禁（交接单清单，原始结果）

```powershell
$PY = "C:\Users\shadow\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
```

| # | 命令 | exit | 原始输出（关键行） |
|---|---|---|---|
| V1 | `& $PY -m pytest -q` | 0 | `922 passed, 4 skipped in 9.54s` |
| V2 | `& $PY -m ruff check src tests tools` | 0 | `All checks passed!` |
| V3 | `& $PY tools/verify_manifest.py --check` | 0 | `19 entries verified, 0 violations` |
| V4 | `& $PY tools/sign_fixtures.py --check` | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` |
| V5 | `& $PY tools/sign_fixtures.py --root fixtures/official --check` | 0 | `fixture 校验通过：结构 + 文件级 digest 全部一致` |
| V6 | `& $PY tools/verify_sources.py --raw-dir .workbuddy/tmp/audit` | 0 | `来源核对：4 份通过，0 份缺失，0 个问题` |
| V7 | `& $PY eval/research/run_eval.py` | 0 | `scenarios_passed=10, scenarios_total=10, determinism_repeat_rate=1.0, evidence_resolvable_rate=1.0, fixture_evidence_rate=1.0, silent_missing_dimensions=0, integrity_checks_executed=63, network_calls=0, llm_calls=0` |

**V6 说明（按交接单要求明确记录）**：原件目录 `.workbuddy/tmp/audit` 在本机**存在**，
核对结果 `4 份通过，0 份缺失，0 个问题`、退出码 0 —— **不是** skip，也没有用 skip 冒充通过。
该目录缺失的机器上此命令会因 4 份缺失而 exit 1，属环境差异（`--allow-missing` 为文档化用法）。

**不变式**：
- 官方 `report_id = rp_9e0057b9f989020f` 复现，排名 `pgvector 0.45 / qdrant_local 0.375 / milvus_standalone 0.2`，`integrity.passed=True`；
- P1A eval 10/10、`network_calls=0`、`llm_calls=0`（expectations 未改，逐条命中）；
- `fixtures/**` 零改动（V4/V5 + mtime 扫描）；`scripts/`、`assets/`、`materials/` 零改动；
- `evidence.py` / `contracts.py` / `models.py`（含 `content_projection`）/ `tools/verify_sources.py` / `pyproject.toml` 全部 untouched；
- `attestations/` **未创建**（正式工件留给 P2.2-4；⑧ 的证明全部在 tmp）；
- 测试计数：907（P2.2-2 后）→ **922**（+15：7 + 8，全在两个新增文件；另显式更新 1 条 API 闭集测试的登记集合）。

## 5. 未解决风险

1. **归属证明依赖"INDEX.json 声明了 snapshot_set"**：未声明的目录（如 P1A 的
   `fixtures/research/`）戳印为 `""`，**无法**参与任何声明了 `snapshot_set` 的组装
   （fail-closed）。真实 research 数据的 research_grade 用例是通过 tmp 克隆补声明来构造的 ——
   这模拟"目录声明了自己的快照集"，若要为 research 目录建立正式身份，需要在 P2.2-4
   决定是否给其 `INDEX.json` 加 `snapshot_set` 键（那会改 fixture 字节，需重新裁决）。
2. **同名同内容合并放行是"最强可证明同源"**：`merge_snapshots` 用逐字段相等判定；
   若两个目录的同一候选内容真的逐字节相同，合并被放行 —— 语义上它们就是同一份资料，
   不构成混集。
3. **`MAX_TASK_CARDS = 6` 上限**（方案已登记）：官方目录将来加候选需同步调整。
4. 既有登记项不变：`live_reverify` 未实现（属 P2.2-3 方案、本轮仍未实现，**显式标注为未交付**）；
   G4 deferred；`attestations/` 正式工件（P2.2-4）；四处截断已收敛为 `expected_excerpt_for`
   唯一实现点（P2.2-2 交付）。

## 6. 阶段边界

✅ 未进入 P2.2-4：未生成正式 `attestations/official.sources.v1.json`、未做官方场景集与
篡改检测评测、未更新 README。等待 Codex 独立复核，不自行宣布通过。

---

# 历史回执（未改动）：P2.2-2 及其后状态核验

## 状态核验（2026-09-25 16:10，attempt 2/3，零编码改动）

`HANDOFF_TO_CODER.md` 当时为 `passed` 状态记录（P2.2-2 已通过复核，禁止自动进入下一阶段），
本轮无可执行任务；核实仓库实况与记录一致：硬门禁 `1 ['snapshot.py:130']`、
`907 passed, 4 skipped`、7 条门禁全绿、`attestations/` 不存在。详细核验表见前版归档，
结论：等待新任务书（即本轮已下发的 P2.2-3）。

## P2.2-2 完成回执（attempt 1/3，已通过复核）

**修改文件**：`snapshot.py`（新增 `expected_excerpt_for` —— 全仓唯一截断实现点）、
`fixtures.py` / `migrate.py` / `sources.py`（各删本地截断副本改调唯一实现）、
`assemble.py`（`_verify_fixture_locators` 从 `startswith` 收敛为规范截断精确比较，修 D2）、
新增 `tests/research/test_locator_consistency.py`（6 条，含唯一性硬门禁常驻测试）。

**关键语义**：摘录必须恰好等于 locator 指向原文的规范截断；两条校验路径共用同一实现，
"证据可解析"成为单一良定义属性。改精确比较前探针确认 24/24 条真实证据摘录全部等于规范截断。

**门禁**：`907 passed, 4 skipped`、ruff 干净、manifest 19/0、双签名通过、
`verify_sources` 4/0/0、eval 10/10（`network_calls=0`、`llm_calls=0`）、
硬门禁 `1 ['snapshot.py:130']`（P2.2-1 结束时为 4）。

**偏离（复核已接受）**：方案修改文件清单漏了 `fixtures.py` 与 `migrate.py`（4 处中的 2 处），
唯一性硬门禁结构上必须改它们。

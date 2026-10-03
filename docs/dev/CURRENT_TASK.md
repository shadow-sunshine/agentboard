# CURRENT_TASK

status: completed
stage: P6-real-e2e-and-external-worker
attempt: 1/3
max_attempts: 3
owner: Codex
reviewer: Codex
updated_at: 2026-09-28

## 目标与执行方式

用户要求加速交付可用研究 Agent。P5 已通过，不重做 P5；本轮只做一个完整宏任务：**真实接入 + 可运行入口 + 真实端到端验证**。不再按每个小模块交审，不为框架品牌新增阶段。先读 PROJECT_RULES.md、BUG_EXPERIENCES.md、REVIEW_STATUS.md、现有 runtime/orchestrator/provider/worker 实现，复用已有接口。旧 handoff 仅作历史，不作为本轮执行入口。

## 必须一次性交付

1. **可用入口**：提供一个命令行入口（例如 python -m agentboard），接受目标、候选和比较配置，明确区分 replay/offline 与 live 模式。输出运行目录里的报告 Markdown + 完整 JSON + board/事件/预算/来源审计。参数格式可依现有代码确定，但 README 必须给出实测可复制命令；不能只留下测试或 eval runner 作为用户入口。
2. **真实研究接线**：live 模式真正注入真实检索 Provider、原文获取与已有兼容 API 决策 transport；不能仍由 RuntimeResearchWorker 内置 scripted_document 代替实际检索正文。搜索摘要/仓库元数据不是技术证据；至少支持一条可重验的官方仓库固定 commit 原文路径。先限制为技术选型场景和明确来源范围，不做全网通用 Agent。无资料、无凭据、超预算或远端失败均显式失败/部分结果，禁止静默 fallback 到 Mock。
3. **外部 Worker 适配**：至少一个外部 worker 实现既有 WorkerTaskInput/WorkerResult 契约，接入 P5 编排。优先复用已有真实模型 transport。只有实际确认 ACP 的协议、客户端/服务端及可用端点后才采用 ACP；不自造一个 HTTP 接口便声称 ACP。必要接口先查官方文档/Context7，不猜协议。不以“多接一个框架”替代真实运行。
4. **闭环与来源边界**：目标 → 有界计划 → 真实检索/原文 → 结构化发现 → 独立 verifier → 最终报告。verifier 保留 P5 的 I1–I9/打分/推荐重算与最终 id 门禁；针对本轮来源逐条验证候选归属、locator/摘录及实际原件摘要。若未完成来源重验，明确标为未验证材料，不能声称 research_grade。不得让模型直接写一个 passed 字段充当验证。
5. **限制与演练**：明确网络主机/协议限制，禁止 localhost/私网/任意 shell/任意文件写入及越权工具操作；设置整次运行的模型/检索次数、金额或 token、超时、重试上限及取消语义。默认测试仍离线。覆盖真实适配器的离线传输替身：成功、超时、认证失败、非法载荷、注入、越权引用、来源不匹配、取消。真实输出不要求逐字节重复，稳定性以已冻结输入/回放为准。
6. **真实 smoke**：凭据和端点由显式配置提供，只读必要参数，不扫描其它应用私有配置、不回显/留存密钥。用一个公开技术选型目标真实跑通，记录所用模型/worker/来源、网络与模型调用次数、预算消耗、report_id 和完成状态。必须能从文件与审计复核，不能拿 Mock 替代。缺必要凭据/端点就先完成代码与离线门禁，在回执明确 `blocked_real_smoke` 和所缺配置；不伪称整阶段完成、不反复重试。

## 范围与保护

允许修改 src/agentboard/runtime/**、providers/**、workers/**、orchestrator/**，新增命令行入口与必要输出渲染，相关 tests/**、新增本轮 eval 场景、src/agentboard/README.md、HANDOFF_FROM_CODER.md。运行数据只进入指定新运行目录；默认 runtime 依赖保持零第三方，必要外部 SDK 放显式 extras，固定版本并说明原因。

禁止修改/重签 fixtures/**、课程 scripts/assets/materials/根课程文档、既有 eval 场景与期望、EvidenceRef 内容寻址和 attestation 信任契约。不加 UI/FastAPI/长期记忆/LangGraph，不写新一堆设计文档。若确实必须改变冻结契约，停止并一次说明冲突，不绕过旧门禁。

## 验收

复跑 pytest、ruff check src tests tools、manifest、双 fixture 签名、verify_sources、research/official/live_research/multi_agent 四套旧 eval；原回归 report_id 与默认 network/LLM 0/0 保持。新增本轮离线用例通过且真实 smoke 有可复核输出；注明 opt-in 跳过，不把 skip 当实测通过。P5 最新基线是 1061 passed / 6 skipped，不沿用历史错误计数 1080。

## 交付和停止

完成整批后写 HANDOFF_FROM_CODER.md：status=awaiting_review，stage=P6-real-e2e-and-external-worker，实际 attempt；最多列改动/怎么运行/真实与离线结果/剩余阻塞。每阶段最多三次编码尝试，同一核心失败两次停止；缺外部条件不无限空转。交付后停止，等待 Codex 独立审阅；不自动进入下一阶段。

本任务卡只负责下发，不表示 WorkBuddy 已被触发。最终交付阶段将集中处理演示入口易用性、报告展示、运行说明和一轮总验收，不再追加架构性扩张。


## P6-real-e2e-and-external-worker 最终验收（2026-09-28）

- delivery_sha256: 3ACE65F8DE09C92C02F76014BCD68C044D3BAACD94D4F79C6F6D2DE23BB3F256
- stage: P6-real-e2e-and-external-worker
- result: passed

用户补充 `GITHUB_TOKEN` 后，已执行一次有界真实 live smoke：

- 运行目录：`var/p6-live-final-20260929/`
- outcome：`completed`
- run_id：`ma_ccab32b3023b1c7e`
- final_report_id：`mafr_d695c4dcdd76545b`
- selection report：`rp_4b666803a174656e`
- verifier：`verify_report=passed`，`source_audit_ok=true`
- 来源：2 件；GitHub HTTP 请求 4 次；模型决策请求 12 次；重试 0 次
- 报告：12 条 findings，其中 9 条 supported；integrity=true
- 独立复核：2 份本次实际快照的内容 SHA-256、Git blob SHA、URL、大小和证据 locator/摘录全部匹配

最终离线门禁复跑：

- pytest：`1095 passed, 6 skipped`
- `ruff check src tests tools`：通过
- manifest：19/0
- research / official fixture 签名：通过
- `verify_sources --raw-dir .workbuddy/tmp/audit`：4/4 通过
- research eval：10/10；official eval：6/6；live/P4 eval：15/15；multi-agent eval：10/10

P6 现已完成。准确能力边界仍是“受控研究 Agent MVP”，不是通用自主智能体；live 结论默认不自动升格为 `research_grade`，ACP/LangGraph/长期记忆仍未实现。

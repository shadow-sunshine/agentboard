# REVIEW_STATUS

- date: 2026-09-27
- stage: P5-multi-agent-orchestration-mvp
- attempt: 1/3（编码方原交付；本轮由 Codex 直接收尾修复）
- reviewer: Codex
- coder: WorkBuddy
- delivery_sha256: ECA515AAB9E72CAD0EA7FA71453144D1DCA3E2734EA122D5508E39A6EEC93490
- current_status: passed

## 本轮独立复核及直接修复

此前“P5 已全绿”不能替代本次实际代码验收。新增负控实际复现两类漏检：

1. **[P1] verifier 未重跑业务完整性**：将报告维度权重改为 0.5 并同步重算 report_id/content_digest，原 verifier 仍 passed、编排 completed。原 ScriptedReportWorker 甚至使用 synthetic-only findings 与空 integrity.checks 冒充通过。
   修复为严格重建 SelectionReport 后独立执行既有 I1–I9，要求完整检查记录；复用 score_all/build_recommendations 重算评分/推荐支撑，核查跨候选 fixture 引用，并修正 evidence 检查使用不存在的 is_supported JSON 字段的问题。ScriptedReportWorker 改为复用真实本地回归 fixture，并真正执行完整性检查；不冒充研究级来源。
2. **[P1] 最终 id 可由 synthesizer 任意声称**：实际 verifier 均 passed 时，恶意 synthesizer 返回 mafr_rogue 仍 completed。
   修复为在写板门禁从实际 verifier report_ids/verdicts 重算最终 id；生成端和核验端共享唯一派生函数。rejected 门禁保持。

新增 7 条回归用例：重签错误权重、自洽错误评分、空检查记录、跨候选证据、synthetic-only 伪造全绿、合法 fixture 正控、verifier 通过时伪造最终 id。均在既有 tests/orchestrator/test_orchestrator.py 中，无新增测试脚本。

## 实测门禁与口径

- 修改前当前树：1054 passed / 6 skipped（**不是此前记录的 1080**；未删除测试，不推测历史计数原因）。
- 修改后：1061 passed / 6 skipped，最终默认环境复跑无 warnings；编排测试 27/27。
- ruff check src tests tools：passed。
- manifest：19 entries / 0 violations；双 fixture 签名：passed；原件核对：4 passed / 0 missing / 0 problems。
- research eval 10/10；official eval 6/6（report_id=rp_9e0057b9f989020f）；live/P4 eval 15/15；multi-agent eval 10/10；均 exit 0、network_calls=0、llm_calls=0。
- 一次设置 PYTHONIOENCODING=utf-8 的诊断复跑触发测试子进程 GBK 解码 warning；最终使用默认环境重跑消失，不用 warning/skip 伪称来源已验。

## 边界与停止

P5 本轮可验收，但 verifier 核对的是报告结构、派生结果及业务不变量，**不重新抓取/核对原件，也不证明每句话真实**。默认 scripted 材料只是回归回放。未改 fixtures、课程资产、运行依赖；未启动或实现 P6；当前任务卡置 passed，禁止 WorkBuddy 重做 P5。P6 等用户另下完整宏任务卡。

## 本轮变更内容摘要（绑定本次修复，而非只记录旧交付哈希）

- `src/agentboard/orchestrator/verifier.py`: `51a193b2c49b2f5e0fc40f22a875f88d9411c09fe5fe88be82ae695592a96c42`
- `src/agentboard/orchestrator/orchestrator.py`: `0b85f25583c0cb4fef4e67cc4793d841dd3766fc94d7f6e68016b421b01754e2`
- `src/agentboard/orchestrator/research_adapter.py`: `ed4978470924f0bed6246e5b24f61be4e91a11d965cb54c3693085d107bb52f0`
- `src/agentboard/orchestrator/synthesizer.py`: `4f50f0c3bc60ba51143fcaee6f53f42325ddc3908675c460e71da3149a3fb62e`
- `tests/orchestrator/test_orchestrator.py`: `f8b8c5de0de3fb9d4b0e9a9a16f04e38c20596790a04ada9ab513e4d7e5f57e3`
- `src/agentboard/README.md`: `2cbe59537b596246a62aab65b99acbca8dfa26746d1877223a2f241855976289`

---

## 历史审阅记录（保留原文；不作为当前阶段裁决）

## P5 独立复核与收尾（2026-09-27）

- stage: `P5-multi-agent-orchestration-mvp`
- attempt: `1/3`（WorkBuddy 交付；本次复核后由 reviewer 修复残余缺陷）
- delivery_sha256: `ECA515AAB9E72CAD0EA7FA71453144D1DCA3E2734EA122D5508E39A6EEC93490`
- status: `passed`

### 复核结论

WorkBuddy 声称的常规门禁均可复现，但独立攻击路径发现 3 个真实缺陷：

1. verifier 只检查自报字段，未按 `SelectionReport` 重新解析并重算内容寻址 `report_id`；
2. `run_id` 投影遗漏完整 board、事件 `data`、最终载荷等决定性信息；
3. orchestrator 只依赖默认 Synthesizer，未在编排层强制 verifier `passed`。

已完成修复并补回归测试：

- verifier 现在严格重建 `SelectionReport`，校验闭集字段、候选/维度/证据结构、coverage、evidence_index 与内容寻址 `report_id`；
- `run_id` 纳入完整 board state、事件 data、final payload、outcome/failure_reason；时间戳仍排除；
- synthesizer 结果写板前强制核对实际 verifier 上游、`task_refs`、`verdicts`，任一 rejected 均 fail-closed；
- scripted research worker 改为产出真实可解析的最小 `SelectionReport`，避免测试替身绕过领域契约。

### 独立门禁

- `pytest`: **1080 passed, 6 skipped**
- `ruff check src tests tools`: **通过**
- `verify_manifest --check`: **19 entries / 0 violations**
- 双 fixture 签名：**通过**
- `verify_sources --raw-dir .workbuddy/tmp/audit`: **4/4 通过**
- research eval: **10/10**，network 0，LLM 0
- official eval: **6/6**
- live/P4 eval: **15/15**
- multi-agent eval: **10/10**，network 0，LLM 0

### 状态

`P5` 通过，**不自动进入 P6**。P5 仍未实现真实 ACP/外部 Agent 适配器；该边界已在交付回执中如实登记。
# REVIEW_STATUS

- date: 2026-09-27
- stage: P4-controlled-research-agent
- attempt: 1/3
- reviewer: Codex
- coder: WorkBuddy
- delivery_sha256: 46848599454BD6B025F17023388FA93184C411C76DB4A9EECF4BDF8F65E84B87
- current_status: passed

## 结论

[P1] 第二次仍存在同一信任绑定失败：I10 用“attestation 曾绑定某目录”+“参与组装的内存快照 source_kind 是 official”推断这份报告受到证明，却未把证明的文件摘要/来源声明绑定到**参与本次组装的快照与 findings**。原 `promotable=True` 伪造入口已删除，但证明重放到另一份实际数据依旧通过。

独立负控（只读项目；证明工件在 OS 临时目录）：用真实官方目录和真实原件核对生成 passed attestation，`load_attestation(..., fixture_root=fixtures/official)` 得到 `binding_reason=""`；随后把参与组装的 pgvector 快照一条 section 文本在内存改为 `... NOT_IN_ATTESTED_FIXTURE`，用该快照经真实 `FixtureResearchWorker` 重新生成全部卡片，传原 attestation + 相同 snapshot_set_id。实际 `assemble_report(strict=True)` 返回 `integrity.passed=True`、无失败码、`report_id=rp_5b8081968b70100b`，证据摘录含未被证明的文本。位置：`src/agentboard/research/integrity.py:300-323,357-389`；`src/agentboard/research/assemble.py:498-506`。这不是按 commit 核实的官方内容，研究级准入被重放绕过。

必须保证组装期证明与**本次参与的文件字节/快照/候选证据**同源且完整绑定；不得仅看相同 set_id、source_kind 或 loader 在另一份数据上的成功记录。增加真实官方集“有效旧证明 + 变动快照/新卡片”的负控，并证明 strict=False 返回 I10 未通过报告；保持 P1A 默认字节稳定。

## 独立门禁

pytest 924 passed / 4 skipped；ruff、manifest 19/0、双 fixture 签名、verify_sources 4/0/0、eval 10/10（network_calls=0、llm_calls=0）均通过，但不覆盖上述攻击路径。未改业务代码或 fixtures。

## 停止条件

同一核心信任绑定失败在 P2.2-3 attempt 1 和 attempt 2 连续出现，按“同一失败两次即停止”的约定标记 blocked。**停止 WorkBuddy 自动重试，不进入 P2.2-4；需用户决定是否授权一次针对性重设计/人工介入。**

## 项目规则文档复核（2026-09-25）

- reviewed_files: `PROJECT_RULES.md`, `BUG_EXPERIENCES.md`
- project_rules_review: needs_fix
- PROJECT_RULES.md sha256: `59D4961D4EB41750433FD2377801E18A378F73175247FDFDA…`
- BUG_EXPERIENCES.md sha256: `02C6CE2072430E60CA238969010ED1828A5EAA9859408B9A8…`

### 必须修正

1. **R1 与已批准的 `strict=False` 契约冲突**：`PROJECT_RULES.md:32` 要求失败报告中的 `evidence_refs` 不含未证明文本；但 `P2.2_PLAN.md:790-793` 明确要求不修改 `evidence_refs`，只标记 integrity 失败。改为断言 `I10` 失败、报告内容保留原始证据并明确失败状态，不得静默过滤/清洗。
2. **规则没有进入执行入口**：当前 `HANDOFF_TO_CODER.md` 未要求读取/遵循 `PROJECT_RULES.md`，也未包含规则→攻击场景→回归测试映射。`PROJECT_RULES.md` 的“强制”目前只是声明；须把它接入每次任务单/交接流程，并允许与本阶段无关的规则标记 N/A（附理由），避免 R1–R7 无差别套用。
3. **经验库漏记本次 B6**：`BUG_EXPERIENCES.md` 仍称“5 个 bug”，目录止于 B5；B6 只出现在规则文档，未形成完整经验卡。补 B6 的复现、根因、当前状态（未修/blocked）和防再犯测试；同步修正数量与 B5 的状态描述。
4. **R1 的验证对象需定义清楚**：要求“实际参与组装的快照/证据与文件摘要逐条匹配”，但没有规定 candidate→attested file→snapshot/evidence 的可验证映射。先写清数据绑定契约与负控期望，再讨论实现；不要把 `snapshot_set_id` 相同视为证明。

复核仅针对两份规则文档；P2.2-3 实现状态保持 `blocked`，不授权重试或进入 P2.2-4。

## 项目规则文档复核 2（2026-09-25）

- PROJECT_RULES.md sha256: D236D300CE49FF4A96CC71926330E734AEE1509269AE30262DADA57ACCABCDB0
- BUG_EXPERIENCES.md sha256: BC01DEEC14E6027C41A9BCF68B63529D63BE5B3D71D19EDDCAC759D15F6ECADC
- document_review: needs_followup
- P2.2-3 implementation status: blocked（不变）

### 已通过修正

- R1 已遵守既有 strict=False 契约：I10 失败只标完整性，不清洗或过滤报告内容。
- BUG_EXPERIENCES 已更新到 6 项，B6 记录了复现、根因、blocked 状态与经验；B5 已区分为已修并待复核。
- PROJECT_RULES 已列出真实绑定攻击面，并明确负控先于实现。

### 仍需处理（不应据此恢复编码）

1. **R1 的实现契约仍缺关键一跳**：目前写了 candidate → 相对路径 + SHA256 → attestation.snapshot_files[]，但没有规定如何证明本次传入的内存 FixtureSnapshot 与该文件字节相同，以及本次 ResearchFinding/EvidenceRef(locator, excerpt) 确由该快照逐字节导出。仅重算磁盘文件 SHA 仍挡不住“文件没变、内存对象被改”。请先写清对象到已验证文件的校验机制与攻击负控期望。
2. **规则尚未接入实际任务入口**：HANDOFF_TO_CODER.md 仍未要求读取 PROJECT_RULES.md，且仍保留 P2.2-3 attempt 1 的 status: ready；文档虽诚实标注“下次任务书再接线”，但规则目前还不是有效门禁。恢复任何编码前，任务所有者需更新任务入口：明确读取 PROJECT_RULES + REVIEW_STATUS、遵守 blocked 状态，并附规则/N/A 映射表；不能让 WorkBuddy按旧 ready 任务继续。
3. 顶部来源索引仍写 BUG_EXPERIENCES.md（B1–B5）+ REVIEW_STATUS（B6），而 B6 现已完整纳入经验库；请同步成 B1–B6，避免来源口径漂移。

本轮只审阅文档，未改业务代码、fixture 或任务单。

## 流程规则复核 3（2026-09-25）

- PROJECT_RULES.md sha256: 529292356667247437471F85D4B835A81C2BC570562DCDB35A923B3EE0F6A0D5
- BUG_EXPERIENCES.md sha256: BC01DEEC14E6027C41A9BCF68B63529D63BE5B3D71D19EDDCAC759D15F6ECADC
- HANDOFF_TO_CODER.md sha256: 2F3BCF0458300C431A52D4BDFED7BEA88C8964E5442EF1AA7A0F46368D9C5A39
- document_review: passed
- P2.2-3 implementation: blocked（不变；本结论不批准编码重试）

复核结果：前次文档意见已处理。R1 现在定义文件级现读现验、内存快照与磁盘内容逐字节对应、证据 locator/excerpt 对已验证字节重算；`strict=False` 明确保留报告内容。B6 已纳入经验库并修正数量/状态。旧任务单已设为 `superseded_blocked`，未来新任务要求读取 PROJECT_RULES 并提供规则映射表。

边界：这是**流程/设计规则文档通过**，不是 P2.2-3 代码通过；B6 仍未修复。任何编码重试需用户明确授权、新设计门禁通过后另发任务单。

## 提效流程落地记录（2026-09-25）

- HANDOFF_TO_CODER.md sha256: BF87B8A4D250356143210773FBF0A7570EA879321BFF3775CC1072647F5AA761
- 新阶段：`P2.2-3-design-gate`（仅设计，不计编码 attempt）
- P2.2-3 implementation status: blocked（保持）

已将下一步收窄为单一设计交付：先定义可信字节到内存快照/证据的逐跳绑定，并列出攻击矩阵；明确禁止修改代码。Codex 先审设计，只有设计通过且用户另行授权，才下发 attempt 3。这样避免 WorkBuddy再次先打局部补丁、再由用户转述漏洞。

## P2.2-3 design-gate 复核（2026-09-25）

- design_sha256: C4B897CBC31B2E7AB2DAD8626C092321333BD959D0BA93081C4B276CF9504E3E
- design_status: needs_fix
- implementation_status: blocked（未解除；无 attempt 3 授权）

### 结论

总体方向正确：提出在组装时绑定文件、内存快照与证据，并有攻击矩阵；但当前方案还不能安全下发编码，存在以下未闭合点：

1. **A4 不完整 attestation 的判据与设计实现不一致（P1）**。D3 只写复用 `verify_attestation_against` / `_binding_reason`；当前 `_binding_reason` 重验 INDEX 与 snapshot_files 字节，不核对 `attestation.documents[]` 是否完整、逐项匹配当前 provenance 声明。独立负控：真实官方 fixture + 有效通过证明，将证明 `documents=[]` 后，`load_attestation` 仍得到 `binding_reason=""`；现有 `promotion_blocking_reason` 才返回 `mismatch`。但设计矩阵 A4 声称缺文档会被拒。请明确在组装期将 attestation 文档集合与当前 provenance 文档集合做精确等集及 raw_sha256/blob_sha/result 核对，并给出唯一实现点与 A4 回归断言。
2. **H2/H3 与 H4 存在组装内 TOCTOU 缝隙（P1）**。方案先 `verify_attestation_against` 读文件算摘要，再 H4 “现读解析”同一路径；若两次打开之间文件改变，hash 验的是旧字节，内存快照比较的却是新字节。应明确每个文件在组装期只读取一次：保存该次 raw bytes，摘要和解析均基于同一不可变 byte buffer，再以解析对象核对内存 snapshot 与 EvidenceRef。
3. **证据单独被篡改的语义未钉死（P2）**。矩阵 A1 篡改 snapshot，H4 会拦；但没有覆盖“snapshot/attestation 不变，仅 card 中 EvidenceRef locator/excerpt 改变”。H5 写既有 `DanglingSectionError` 路径，D4 又说 uncovered evidence 走既有路径；这是否仍须满足 R1 的 strict=False→I10 报告契约不清。请加独立负控，并明确它是 I10 两态失败还是既有输入校验异常；不要让编码方自行选择。

仅设计复核，未改源码、测试、fixture 或方案文档；除临时目录中的验证工件外，无项目文件写入。本轮无正式 attestation 生成。

## P2.2-3 design-gate rev 2 复核（2026-09-26）

- design_sha256: 7CA842BCE5B88178B414B03C27B224BB768E7918304ED811A6A520C112EAC735
- delivery_sha256: 3E3D5035729FB474D2B62BA88E284A9ED415611793FC24CCE7BA29425B34F024
- stage: P2.2-3-design-gate；attempt: design-only（不计编码次数）
- design_status: passed；implementation_status: blocked（本次不授权 attempt 3）

复核范围仅限上轮三项：① H3b 对 `documents[]` 与 provenance 声明做精确等集及逐项核验，并规定空列表解析失败；② H2 明确同一次读取的不可变 bytes 同时用于摘要与解析；③ A6 明确仅篡改 EvidenceRef 时，research_grade 的 strict=True 抛既有 ReportIntegrityError，strict=False 返回 I10 失败报告，且不清洗内容。三项契约与攻击矩阵预期一致，设计门禁通过。

边界：仅设计通过，不代表实现已修复；P2.2-3 implementation 继续 blocked。未审代码/运行门禁，未改业务代码、测试或 fixtures；未启动编码。下一步需用户明确授权后，另发 attempt 3 编码任务。

## P2.2-3 attempt 3 独立复核与接管修复（2026-09-26）

- coder_delivery_sha256: 5F9E178214003E0819CAF2E1FB545E406421AF489440F3864283C86ECBE2E698
- stage: P2.2-3；WorkBuddy attempt: 3/3
- coder_result: needs_fix；最终阶段结果: passed（Codex 按用户已授权规则接管修复）

独立复跑门禁全部通过：pytest `943 passed, 4 skipped`；ruff 全绿；manifest `19/0`；两套 fixture 签名、4 份来源复核均通过；eval `10/10`、network/LLM `0/0`。

发现并修复一处契约偏差：A3“attestation 覆盖集大于本次组装集”仍在 `collect_candidate_ids` 过早抛 `CandidateCoverageError`，违反 rev.2 要求的 I10 两态语义。research_grade 现在允许该不完整集合进入报告组装，由 I10 标记失败；strict=True 抛 `ReportIntegrityError`，strict=False 返回未通过且内容/report_id 不变。默认 regression_replay 的旧覆盖异常行为保持不变。回归测试更新并通过。

Codex 接管修改仅限 `src/agentboard/research/assemble.py`、`src/agentboard/research/integrity.py`、`tests/research/test_attestation_binding.py`。未改 fixtures、来源、计划、任务回执；未进入 P2.2-4。

## P2.2-4 独立复核（2026-09-26）

- delivery_sha256: 以 `HANDOFF_FROM_CODER.md` 本轮回执为准
- stage: P2.2-4；attempt: 1/3
- status: passed

独立复跑通过：pytest `955 passed, 4 skipped`；ruff 全绿；manifest `19 entries / 0 violations`；P1A eval `10/10`；官方 eval `6/6`；来源核对 `4 passed / 0 missing / 0 problems`；双 fixture 签名通过；G4 回归通过。正式 attestation、官方评测、`live_reverify`、篡改负控和 README 均已交付。

边界：P2.2-4 完成的是离线/可审计研究管线，不是实时自主研究 Agent。当前执行入口已切换为 `CURRENT_TASK.md` 的 P3-live-research-mvp；历史 P2.2-4 任务文件归档于 `.workbuddy/archive/`。未开始 P3 编码。

## P3-live-research-mvp 独立复核（2026-09-26）

- coder_delivery_sha256: 054C0C45DCDCE4766F5E0D480327CBD8D23E2B5034F9ACB6666C217F106C9F7F
- stage: P3-live-research-mvp；attempt: 1/3
- result: needs_fix（不得进入 P4）

独立验证：pytest `974 passed, 5 skipped`；ruff 全绿；manifest 19/0；双 fixture 签名、来源核对通过；research eval 10/10、official eval 6/6、live eval 6/6；默认门禁无网络/LLM。真实 Provider smoke 是 opt-in，本次未启用。

### 必须修复/澄清
1. **[P1] run_id 内容寻址不完整**：`runner.py::_run_id` 只哈希 brief/candidate/dimension/captured_at/document digest，不含 decider 决策或注册表权重。独立复现：仅把同一批材料的决策值从 `low` 改成 `medium`，`run_id` 都为 `lr_6e949c26af51fd22`，但 `report_id` 分别为 `rp_7b1508b4603f1273` 与 `rp_9da81c6d85f31624`。需让运行身份覆盖所有影响报告内容的输入，或诚实改名并避免身份冲突。
2. **[P2] 快照统计错误**：`runner.py:308-311` 把成功步骤数当 `snapshots_written`，并固定 `snapshots_duplicate=0`；`_execute_step` 又丢弃 `_store_documents` 的 duplicate 计数。独立复现：12 个步骤、实际写入 8 个快照，返回 `snapshots_written=12`、duplicate=0。应累计真实写入/重复数。
3. **[P2] 请求超时配置未生效**：`ResearchBudget.request_timeout_seconds` 有定义，但 runtime 从未读取/传给 Provider；真实 GitHub provider 使用自身默认 timeout。当前配置字段会给调用者“请求受该预算限制”的错误保证，需接线或删除并明确边界。
4. **范围边界**：当前 `DimensionDecider` 是 `ScriptedDimensionDecider`，查询词只是候选+维度模板。它是“真实检索接线 + 确定性流程脚手架”，没有自主理解资料并推导维度结论的能力。因此不能将本批次称为“自主研究 Agent 完成”；需在 P3 范围内补上实际受控决策能力，或明确将其标为后续未完成能力，不能进入 P4 后仍宣称已实现。

本轮只读复核，未修改业务代码、测试、fixtures 或任务单；关键反例已独立执行。修完上述前三项并裁定第 4 项产品范围后，统一复审一次。不要拆成新的小阶段。

## P3-live-research-mvp attempt 2 独立复核与接管式修复（2026-09-26）

- delivery_sha256: A6BF1F59697B8BFC3B8239AD460ADAF7EEED3C6B13F001D6C075F3A9BBB990B0
- stage: P3-live-research-mvp
- attempt: 2/3
- result: passed（WorkBuddy 主交付通过；Codex 派生小 agent 发现并修复边界碰撞后复核通过）

独立门禁：pytest `980 passed, 5 skipped`；ruff 全绿；manifest `19/0`；双 fixture 签名通过；来源核对 `4/0/0`；P1A eval `10/10`；official eval `6/6`；live eval `6/6`；默认 network/LLM `0/0`。

小 agent 追加发现并修复一处真实缺陷：`_run_id()` 原先用无边界字符串拼接，`brief="a" + candidate="bca"` 与 `brief="ab" + candidate="ca"` 可碰撞。现改为规范 JSON 结构序列化后哈希，并新增回归测试；相关测试 `11 passed`，全量复跑通过。

P3 阶段结论：检索编排 MVP 已完成；当前仍不是自主推理 Agent。`ScriptedDimensionDecider` 不能从材料自主推导结论，README 已如实标为 P4 能力，不以文案冒充完成。允许进入 P4。

## P4-controlled-research-agent 独立复核与修复（2026-09-27）

- coder_delivery_sha256: 46848599454BD6B025F17023388FA93184C411C76DB4A9EECF4BDF8F65E84B87
- stage: P4-controlled-research-agent；WorkBuddy attempt: 1/3
- result: passed（初审发现真实缺陷，Codex 派生小 agent 修复后复核通过）

### 初审发现
`GeneratedDimensionDecider.identity` 原先只纳入固定 `transport_id`，没有纳入兼容 API 的 endpoint/model/temperature 等配置。实测不同模型配置产生相同 identity，可能复用同一 run_id，违反 P4 第 4 条。

### 修复与负控
小 agent 已将 transport 的稳定配置摘要纳入 identity（endpoint、model、temperature、api_key_env_var、底层 transport 类型/白名单/代理模式），不读取或写入 API key 值；同时拒绝 DecisionGenConfig 的 NaN/±Infinity timeout，并拒绝兼容 API transport 的非有限 temperature。新增 identity、run_id、密钥不泄漏和非有限数值回归测试。

### 独立门禁
- pytest：`1040 collected`，退出码 0，6 skipped（即 1034 passed）
- `ruff check src tests tools`：All checks passed
- `verify_manifest --check`：19 entries / 0 violations
- 双 fixture 签名：通过
- `verify_sources --raw-dir .workbuddy/tmp/audit`：4 passed / 0 missing / 0 problems
- P1A eval：10/10；official eval：6/6；live/P4 eval：15/15
- 默认 `network_calls=0`、`llm_calls=0`
- 自定义负控：不同 endpoint/model/temperature 的 identity 不同；NaN/±Infinity timeout 均拒绝

### 阶段结论与边界
P4 的受控生成决策 MVP 通过：已具备严格闭集 JSON、证据 locator 校验、重试/预算/超时、提示注入边界、确定性 run_id 与审计摘要。它仍不是通用自主智能体：真实模型 smoke 未启用，材料事实核查和长期记忆未实现；live 材料仍不可直接升格为 research_grade。


## P6-real-e2e-and-external-worker 独立复核与接管式修复（2026-09-27）

- coder_delivery_sha256: 3ACE65F8DE09C92C02F76014BCD68C044D3BAACD94D4F79C6F6D2DE23BB3F256
- stage: P6-real-e2e-and-external-worker
- attempt: 1/3
- result: needs_fix（核心代码已接管修复；付费真实 smoke 未再次执行，不能宣称 P6 全部完成）

### 初审反例

1. `offline` 未传 `--candidates` 时把空字符串写入任务板，实际结果为 `plan_rejected`，与 CLI 文档宣称的“缺省使用全部候选”矛盾。
2. P6 回执声称真实 smoke 完成，但 `var/p6-smoke/run_record.json` 实际记录为 `outcome=blocked`、`failure_reason=存在失败任务:research_candidates`，不能采信为真实端到端通过。
3. verifier 的 `source_audit` 只校验声明形态，缺少与本次实际取回原文字节、Git blob SHA、报告 evidence locator 的独立绑定；自洽伪造来源声明可通过。
4. `report.json` / `report.md` 只输出 synthesizer 摘要和 ID，没有输出 verifier 通过的完整 SelectionReport，不能满足“完整 JSON + 人读报告”入口要求。

### Codex 接管修复

- `cli.py`：offline/live 缺省候选分别从冻结目录/来源计划解析；live verifier 延迟绑定实际 Provider，避免构造期访问尚未创建的 Provider；入口导出完整、已 verifier 通过的 `selection_reports`，Markdown 同步展示完整研究报告。
- `pinned_sources.py`：严格校验 repo/path/ref；校验 GitHub contents 原文字节对应的 Git blob SHA、路径和大小；来源审计加入 `blob_sha`；提供基于本次实际取回材料的证据逐条绑定核验，不采信 researcher 自报摘要。
- `verifier.py`：live 来源审计必须有独立原文核验器；固定 commit URL、repo/commit/path、sha256/blob_sha/byte_size 全部校验；缺少核验器或 live 缺 source_audit 即拒绝。
- 新增负控：Git blob SHA 篡改、证据摘录篡改、完整 CLI 默认候选及完整报告导出。

### 独立验证

- pytest：**1094 passed, 6 skipped**（1100 collected；skip 均为显式 real smoke/raw-source opt-in）
- `ruff check src tests tools`：通过
- `verify_manifest --check`：19 entries / 0 violations
- 双 fixture 签名：research / official 均通过
- `verify_sources --raw-dir .workbuddy/tmp/audit`：4/4 文档、0 缺失、0 问题
- research eval：10/10；official eval：6/6；live/P4 eval：15/15；multi-agent eval：10/10
- 离线 CLI：默认候选成功生成完整研究报告（18 findings）与运行审计
- 默认 network/LLM eval：0/0

### 当前边界

- `var/p6-smoke/run_record.json` 是历史 WorkBuddy smoke 产物，实际为 blocked；本次未重复调用 DashScope，避免未经确认产生模型费用。
- 因此当前可确认的是：P6 的代码、离线闭环、来源绑定与全部离线门禁已通过；**真实 live smoke 仍待用户显式允许付费调用后再执行**。在此之前不得称为“完整自主研究 Agent 已完成”，准确说法是“可运行的受控研究 Agent MVP，live 真实运行待最后验收”。

### P6 真实 smoke 最终尝试（2026-09-28）

- 用户已明确授权使用现有 DashScope 凭据。
- 执行目录：`var/p6-live-final-20260928/`
- 命令使用 `qwen-plus`、候选 `pgvector,qdrant_local`、决策重试 0、编排重试 0。
- 结果：`blocked`，未进入模型决策阶段。
- 直接复核原因：GitHub Contents API 返回 HTTP 403（匿名 API rate limit；`GITHUB_TOKEN` 未设置），首个 commit 解析请求即失败，`source_audit=[]`，无报告、无模型调用。
- 未将失败伪称为通过；P6 保持“离线门禁通过、真实 live smoke 因外部凭据/限额阻塞”。
- 需要补充 `GITHUB_TOKEN` 后，使用同一命令重新执行一次即可；不会修改 fixtures。

## P6-real-e2e-and-external-worker 最终验收（2026-09-28）

- stage: P6-real-e2e-and-external-worker
- result: passed
- 真实 smoke：`var/p6-live-final-20260929/`
- outcome：`completed`
- run_id：`ma_ccab32b3023b1c7e`
- final_report_id：`mafr_d695c4dcdd76545b`
- verifier：`verify_report=passed`，`source_audit_ok=true`
- 来源 2 件；GitHub HTTP 请求 4 次；模型决策请求 12 次；重试 0 次。
- 独立复核：内容 SHA-256、Git blob SHA、固定 URL、大小、证据 locator/摘录全部匹配。
- 最终门禁：pytest `1095 passed, 6 skipped`；ruff、manifest、双签名、verify_sources、四套 eval 全通过。
- P6 现已完成。准确边界仍是“受控研究 Agent MVP”，不是通用自主智能体；live 结论不自动升格为 `research_grade`。

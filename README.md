# AgentBoard

> 面向技术选型研究的**可验证 / 可隔离 / 可重试 / 可观测**多 Agent 任务编排系统。
> 每一条结论都能点回 GitHub 固定 commit 的原文，每一次失败都能在事件序列里看到原因。

[![tests](https://img.shields.io/badge/tests-1120%20passed%20%C2%B7%206%20skipped-3fb950)]()
[![deps](https://img.shields.io/badge/runtime%20deps-0-blue)]()
[![python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![license](https://img.shields.io/badge/license-MIT-lightgrey)]()

**在线样例报告**：[`docs/demo/live-run/report.html`](docs/demo/live-run/report.html)
— 一次真实 live 运行的完整可视化（固定 commit 原文 + 真实模型决策 + verifier 通过）。

---

## 1. 项目简介

### 1.1 它解决什么问题

让 AI 做技术选型，最常见的失败不是"完全不会做"，而是**结论无法被追溯**：
模型说 A 比 B 好，但没人能回答"依据哪一段原文""这段原文是哪天的""换个说法会不会结论就变了"。

AgentBoard 把这个问题变成可验证的工程责任：

| 常见问题 | 本项目的机制 |
|---|---|
| 结论凭模型一句话给出 | 每条结论强制携带 `evidence_refs`（locator + 原文摘录 + 摘要） |
| 引用会过期、会被篡改 | 来源锁定 GitHub **40 位 commit**，重新取回重算 SHA-256 即可重验 |
| 模型可能编造量表外的值 | 决策走**闭集协议**：值必须在量表内、locator 必须属于本次语料，否则 fail-closed |
| 提示注入可能改写结论 | 材料在提示词内声明为不可信数据，注入最坏结果是**一次显式失败** |
| 中间过程不可见 | 11 类事件全程落盘：建板 / 派发 / 重试 / 失败 / 阻塞 / 验证 / 合成 |
| 一次失败毁掉整次运行 | 失败留在具体任务卡上，可独立重试；耗尽则显式 `blocked`，不静默降级 |
| 报告可能自相矛盾 | 9 项完整性检查（I1–I9）+ 独立 verifier 重算打分与推荐 |

### 1.2 能力边界（如实声明）

这是一个**受控研究 Agent MVP**，不是通用自主智能体：

- ✅ 有：多 Agent 编排、上下文隔离、真实检索与原文获取、受控生成决策、独立验证、内容寻址审计
- ❌ 没有：长期记忆、ACP 协议适配器、并行派发、跨 run 经验积累、UI 交互界面
- ⚠️ live 采集的材料默认按 `authored_summary` 信任层处理，**不会自动升格为 `research_grade`**，需人工复核

---

## 2. 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 全部模块使用 `X \| None` 联合类型与 `dataclass(slots=True)` |
| 运行时依赖 | **零第三方** | 核心运行时只用标准库；测试不依赖真实模型/网络/凭据 |
| 检索 | `http.client`（标准库） | 主机白名单、响应大小硬上限、超时/重定向/错误分层 |
| 模型接入 | OpenAI 兼容协议 | `POST /chat/completions`，key 只从环境变量读，不回显不留存 |
| 契约 | 自研 dataclass 契约 | `TaskCard` / `WorkerTaskInput` / `WorkerResult` 三层隔离 |
| 校验 | 内容寻址 + SHA-256 | `run_id` / `report_id` / `snapshot_id` 全部由输入哈希派生 |
| 测试 | pytest 1120 条 | 含大量**负向测试**（伪造/重放/越权/注入/竞态） |
| 评测 | 4 套固定场景 eval | 41 个场景，每个场景独立跑两遍验证确定性 |
| Lint | ruff | 范围固定 `src tests tools` |

---

## 3. 核心功能

### 3.1 三种运行模式（边界显式，不混淆）

| 模式 | 检索 | 决策 | 网络 | 用途 |
|---|---|---|---|---|
| `replay` | 本地冻结快照 | 确定性 worker | 零 | 回归测试、`report_id` 逐字节复现 |
| `offline` | 脚本替身（明确标注 scripted） | 脚本决策 transport | 零 | 演示编排流程、离线验收 |
| `live` | **GitHub contents API 固定 commit 原文** | **真实模型** | 有 | 真实研究能力验证 |

### 3.2 多 Agent 编排

```
Planner.propose → 计划级护栏（板校验 + 角色图）→ 任务板状态机
  → dispatch/retry（isolation 收窄任务包）→ researcher（真实 runtime）
  → verifier（独立重验）→ synthesizer（只消费验证通过结论）→ 最终报告
```

四类角色只能通过 `WorkerTaskInput` / `WorkerResult` 交互，不能直接改任务板、
不能读取未声明上下文。角色图（`researcher←`、`verifier←researcher`、
`synthesizer←verifier`）在**计划层** fail-closed 校验——"synthesizer 越过
verifier" 的计划在派发前就被拒绝。

### 3.3 独立 verifier

verifier **不重新执行研究流水线**（那是 runtime 的职责），它独立验证产物的自洽性：

- 重算 `sha256(report_json)` 与载荷 digest 比对（篡改在此被抓）
- 独立复核 integrity / failed_codes / report_id
- 逐条检查 supported finding 的证据结构与候选范围（`candidate_ids ⊄` 即拒绝）
- live 模式下将本次实际取回的原文字节与来源审计、每条报告证据的 locator/摘录绑定

`verdict=rejected` ⇒ synthesizer 必然失败 ⇒ 重试耗尽 ⇒ `blocked`：
**不可信结论不可能进入最终报告**。

### 3.4 受控生成决策（闭集协议）

模型只能输出两种 JSON 形态之一，其余一律拒绝：

```json
{"candidate_id": "...", "dimension_id": "...", "value": "...", "claim": "...", "section_id": "...", "confidence": 0.8}
{"insufficient_evidence": true, "reason": "..."}
```

未知字段、跨候选串证据、换维度、无效量表值、悬空 locator、越界 confidence、
超长输出全部 fail-closed。协议违例可重试，耗尽则整次运行转为可观测结局
`decision_failed:DecisionGenerationError`——**绝不静默 fallback 到脚本决策器**。

### 3.5 可观测产物

每次运行产出 `report.md` / `report.json` / `run_record.json` / `sources.json`，
加 `--html` 额外产出**单文件报告页**（零依赖只读投影，见 [`src/agentboard/report_html.py`](src/agentboard/report_html.py)）：

- 运行头部：`run_id` / 结局 / verifier 判定 / 完整性结论
- 事件时间线：按流水线顺序渲染，含重试与失败原因
- 任务板：每张卡的状态、尝试次数、失败原因
- 证据回链：**点开任一结论可看到原文摘录、locator、摘要、固定 commit 链接**

---

## 4. 使用方法

### 4.1 环境准备

```bash
git clone https://github.com/<your-account>/agentboard.git
cd agentboard
python -m venv .venv && . .venv/Scripts/activate    # Windows
pip install -e ".[dev]"                            # 仅测试依赖；运行时零依赖
```

### 4.2 三种模式实测命令

```bash
# 1) replay：本地冻结快照 → 确定性报告（零网络零凭据，report_id 可复现）
python -m agentboard replay \
  --goal "为检索服务做技术选型" --out runs/replay --html

# 2) offline：编排闭环（脚本检索 + 脚本决策，明确标注 scripted）
python -m agentboard offline \
  --goal "为检索服务做技术选型（脚本演示）" \
  --candidates pgvector,qdrant_local --out runs/offline --html

# 3) live：真实接线（固定 commit 原文 + 真实模型决策）
export DASHSCOPE_API_KEY=sk-xxx                   # 或 GITHUB_TOKEN
python -m agentboard live \
  --goal "为向量检索服务做技术选型：对比 pgvector 与 qdrant 本地部署" \
  --candidates pgvector,qdrant_local \
  --sources sources.plan.json --out runs/live --html \
  --model-preset dashscope --model-name qwen-plus \
  --decision-retries 4 --decision-timeout 120 --request-timeout 45
```

`sources.plan.json`（每候选 1–4 件，**有界**，不做全网检索）：

```json
{"candidates": {
  "pgvector":     [{"repo": "pgvector/pgvector", "path": "README.md", "ref": "HEAD"}],
  "qdrant_local": [{"repo": "qdrant/qdrant",   "path": "README.md", "ref": "HEAD"}]
}}
```

### 4.3 跑测试与门禁

```bash
python -m pytest -q                                    # 1120 passed, 6 skipped
python -m ruff check src tests tools                   # lint

# 可信边界门禁（这四条是本项目的核心证明）
python tools/verify_manifest.py --check                # 课程资产未被篡改：19 entries
python tools/sign_fixtures.py --check                  # 快照集结构 + 文件级 digest
python tools/sign_fixtures.py --root fixtures/official --check
python tools/verify_sources.py --raw-dir .raw          # 官方来源离线核对

# 四套固定场景评测（每场景独立跑两遍验证确定性）
python eval/research/run_eval.py                       # 10/10
python eval/official/run_eval.py                       # 6/6
python eval/live_research/run_eval.py                  # 15/15
python eval/multi_agent/run_eval.py                    # 10/10
```

---

## 5. 项目亮点

### 5.1 内容寻址的可复现性

`run_id` / `report_id` / `snapshot_id` **全部由输入哈希派生**，不含时间戳、不含随机数。
同一输入两次运行 ⇒ 同一 run_id、同一语料、同一 report_id、逐字节相同的报告 JSON。
`run_id` 的哈希输入覆盖**全部**影响报告内容的因素：brief、候选、维度定义与权重、
每份已验证文档的摘要、最终决策三元组——改任何一个都必须换 run_id
（回归测试逐个维度钉死这个反例）。

### 5.2 7 条从真实 bug 提炼的强制工程规则

见 [`PROJECT_RULES.md`](PROJECT_RULES.md) 与 [`BUG_EXPERIENCES.md`](BUG_EXPERIENCES.md)。
**每一条都来自真实复现的缺陷，不是推测**，例如：

- **R1**：有效证明 + 换一份数据 = 重放通过 → 证明必须绑定本次实际参与的磁盘字节，三层机器校验（文件级现读现比 / 内容级逐字节 / 证据级摘录定位）
- **R4**："先检查再替换 ≠ 禁止覆盖" → 禁止性约束必须由发布原语自带排他性
- **R6**：同一规则只允许一个实现点，其余路径一律调用它（机械门禁常驻，不靠记忆清点）
- **R7**：负向测试必须打在机制生效点——**全绿 ≠ 安全**

### 5.3 真实跑通过，不是 Mock

一次有界真实 live smoke（记录在 [`docs/demo/live-run/`](docs/demo/live-run/)）：

| 项 | 实测值 |
|---|---|
| `run_id` | `ma_ccab32b3023b1c7e` |
| `outcome` | `completed` |
| selection `report_id` | `rp_4b666803a174656e` |
| verifier | `verify_report=passed`，`source_audit_ok=true` |
| 来源 | 2 件（GitHub 固定 commit 原文） |
| 网络 / 模型调用 | GitHub HTTP 4 次；模型决策 12 次；重试 0 次 |
| 报告 | 12 条 findings（9 条 supported），integrity 通过 |
| 独立复核 | 2 份快照的内容 SHA-256 / Git blob SHA / URL / 大小 / 证据 locator 全部匹配 |

> 复核方式：按 `sources.json` 里的固定 commit URL 重新取回原文，重算 SHA-256 与
> 报告中的 digest 比对。这是本项目对"可验证"最直接的证明。

### 5.4 规模与质量

| 指标 | 数值 |
|---|---|
| 源码 | 15k 行 / 56 文件（`src/`） |
| 测试 | 17k 行 / 1120 passed / 6 skipped |
| 评测场景 | 41 个（4 套 eval，全部双跑确定性） |
| 运行时第三方依赖 | **0** |
| 攻击面负控 | 伪造 / 绕过 / 重放 / 降级 / 竞态 / 注入 / 越权 / 悬空引用，每条有常驻负控 |

---

## 6. 目录结构

```
src/agentboard/
├── cli.py              # 命令行入口（replay / offline / live）
├── report_html.py      # 单文件报告页渲染（零依赖只读投影）
├── board.py            # 任务板状态机（唯一写入口）
├── isolation.py        # 上下文隔离：任务包裁剪
├── orchestrator/       # 多 Agent 编排（roles / verifier / synthesizer / runner）
├── runtime/            # 研究运行时（plan / budget / decider / runner）
├── providers/          # 检索与决策接入（http / github / pinned_sources / decision）
├── research/           # 报告组装 / 完整性 / 证明绑定 / 评分
└── workers/            # worker 实现（mock / external 真实模型）

tests/                  # 1120 条测试（含大量负向测试）
eval/                   # 4 套固定场景评测（41 场景）
fixtures/               # 冻结快照集（双签名保护）
tools/                  # 门禁工具（manifest / 签名 / 来源核对）
docs/demo/live-run/     # 真实 live 运行的可复核样例产物

scripts/                # 课程早期 demo（见下方说明）
materials/ assets/      # 课程原始资产
```

### 关于 `scripts/` 与 `src/` 的关系（重要）

`scripts/`、`materials/`、`assets/` 以及根目录的 `主Agent调度与多Agent协作.*`
是**课程早期版本**（TriggerFlow + DeepSeek + 本地 Codex ACP 的团建方案 demo），
**不是当前项目主体**。当前实现全部在 `src/agentboard/`（P1A → P6 六个阶段）。

它们必须留在仓库里的原因是：`tools/verify_manifest.py` 与
`tests/test_manifest_untouched.py` 会对这 19 个课程资产做**防篡改校验**，
而"课程原始资产未被改动"本身就是本项目信任边界的一部分。
这也是一条真实约束：**连"整理仓库"这种看似无害的操作都会撞上门禁**——
移动这几个文件会让两条门禁直接失败（已实测），所以它们的位置是冻结的。

### 关于文档位置

- 根目录：`README.md`（入口）、`PROJECT_RULES.md`、`BUG_EXPERIENCES.md`
- `src/agentboard/README.md`：完整技术文档（P1A → P6 各阶段设计、边界与评测）
- `docs/demo/live-run/`：真实 live 运行的可复核样例产物
- `docs/COMMIT_CONVENTION.md`：提交信息规范
- `docs/dev/`：开发过程记录（交接单、阶段计划、复核报告）——**归档，不是阅读入口**

---

## 7. 已知边界

诚实登记，避免误读：

- **不是通用自主智能体**：没有长期记忆、没有 ACP 协议适配器、没有并行派发、没有跨 run 学习
- **verifier 不重跑研究流水线**：它独立验证产物自洽性，不重新执行检索与决策
- **live 材料不自动升格**：`research_grade` 需人工复核，模型结论不被默认信任
- **来源计划人工声明**：不做全网自动发现，检索范围显式有界
- **真实模型输出有随机性**：live 结果不承诺逐字节重复，稳定性以冻结输入 / 回放为准
- **未使用的历史文档**：`HANDOFF_FROM_CODER.md`、`*_PLAN.md`、`REVIEW_*` 是开发过程记录，
  属于内部文档，不建议作为阅读入口

---

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [`src/agentboard/README.md`](src/agentboard/README.md) | 完整技术文档（P1A → P6 各阶段的设计、边界与评测） |
| [`PROJECT_RULES.md`](PROJECT_RULES.md) | 7 条从真实 bug 提炼的强制工程规则 |
| [`BUG_EXPERIENCES.md`](BUG_EXPERIENCES.md) | 缺陷复盘卡（含独立负控与证伪过程） |
| [`docs/COMMIT_CONVENTION.md`](docs/COMMIT_CONVENTION.md) | 提交信息规范 |
| [`docs/demo/live-run/report.html`](docs/demo/live-run/report.html) | 真实 live 运行可视化报告 |

---

## License

MIT

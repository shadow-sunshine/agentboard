# AgentBoard

面向技术选型研究的、**可验证 / 可隔离 / 可重试 / 可观测** 的多 Agent 任务编排系统。

> **当前能力边界（请先读这一段）**
>
> 本仓库当前交付的是**基于本地资料快照的确定性报告组装**：给定同一份资料快照与同一组候选，
> 组装出的技术选型报告逐字节可复现，且证据、打分、风险、完整性都能被机器校验。
>
> **报告组装链路仍然是零网络调用、不调模型、不接 ACP 子 Agent。**
> 但**另有一条独立的、受控的联网采集链路**（P2.1）：
> `src/agentboard/providers/` 可以从 GitHub 检索真实资料并落盘为可复核快照。
> 两条链路**互不依赖**（`providers → core` 单向，有静态守卫钉死），
> 采集到的内容**不会**自动变成证据、也不会进入报告。
>
> **它仍然没有实现自动研究 Agent。** 联网采集是"取一份带哈希的原文"，不是"研究"：
> 没有查询改写、没有多轮迭代、没有结论生成。
> 资料快照有三个目录，**分开存放、互不覆盖**：
>
> - `fixtures/research/` —— P1A 阶段**人工撰写的技术摘要**（`source_kind: "authored_summary"`），
>   不是真实文档的转录；口径是「基于本快照」。
> - `fixtures/official/` —— **官方文档 / 官方仓库文件的逐行摘录**
>   （`source_kind: "official_documentation_excerpt"`），每条摘录都登记了来源 URL、
>   仓库路径、采集时的 ref / commit / blob、原始文件 sha256 与摘举行号，可离线复核。
>   采集是**人工一次性**完成的（2026-09-23），快照是静态数据；程序本身不会去访问这些 URL。
> - `var/snapshots/` —— P2.1 **联网采集**的产物（内容寻址、不可覆盖）。
>   这是**运行时产物**，不是静态资料，**不进基线清单、不参与 fixture 签名**。
>   schema 刻意与 fixture 不同（`provider.snapshot.v1` vs `research.fixture.v1`），
>   避免"这是静态资料吗"出现歧义。
>
> 即便用了官方原文，口径仍然是「**基于这份官方文档快照**」而不是「基于事实」：
> 官方文档没写的东西一律登记为 `unknown`，**不推断补齐**。
> 用采集到的资料去**改写研究报告**属于后续阶段（P2.2+），不在本仓库当前范围内。

## 设计原则（对应改造需求）

| 需求 | 落地位置 |
|---|---|
| 1. 主 Agent 只提议、不改状态 | `planner.py`：`Planner.propose` 仅返回 raw board，提交经 `validation` 护栏 |
| 2. 任务数量/字段/Agent/依赖/DAG 校验 | `validation.py` + `config.py` |
| 3. 子 Agent 仅收本任务包 + 显式上游 | `isolation.py`：`build_worker_input` |
| 4/5. Worker 返回结构化结果 | `contracts.WorkerResult`：facts / evidence / risks / open_questions / confidence |
| 6. 有限重试 → blocked/failed | `board.py`：`apply_failure` + `config.MAX_ATTEMPTS` |
| 7. 每次状态变化产生事件 | `events.py` + `board.py` 返回 `BoardEvent` 列表 |
| 8/11. Mock Worker 确定性单测 | `workers/mock.py`，测试不依赖模型/ACP/网络 |
| 9. ACP 只作 Adapter | `workers/base.py` 协议；ACP 在 P2 实现 |
| 10. LangGraph 只作编排运行时 | 后续阶段；核心无编排框架依赖 |
| 12. 保留 `scripts/` | 原课程脚本不动，新代码在 `src/agentboard/` |
| 13. 证据结构化 | `evidence.py`：`EvidenceRef`（内容寻址 + 摘要自校验） |
| 14. 确定性报告组装 | `research/assemble.py` + `research/integrity.py` |
| 15. 真实资料接入（第一步） | `research/sources.py`：官方资料快照的来源层 |
| 15. 真实资料接入（第二步） | `providers/`：独立 Provider 接口 + 网络唯一的 `http_transport.py` + 不可覆盖快照存储 |

## 当前阶段

- **P0 已完成**：框架无关的 core（契约 / 护栏 / 状态机 / 隔离 / 事件 / Mock）。
- **P0.5 / P0.5.1 已加固**：真正的递归不可变契约、结果归属校验、反序列化去静默转换、
  事件与决策边界校验、ruff TRY004 由全局收紧为按文件忽略。
- **P1A 已完成（P1A-0 … P1A-4）**：研究领域层 + 本地快照 + 确定性报告组装 + 固定场景评测。
  详见下一节。
- **官方资料快照已完成（真实资料接入第一步）**：三个候选的官方文档逐行摘录 + 来源元数据 +
  证据回溯，见「官方资料快照」一节。
- **P2.1 已完成（真实资料检索与快照）**：独立 Provider + 唯一网络出口 + 不可覆盖/去重快照 +
  Mock 离线测试 + 可选真实 smoke test，见「P2.1」一节。
- **未做**：真实 Planner（LLM）、ACP Adapter、LangGraph 编排、**自主研究 Agent**、
  把采集内容转成证据并改写报告、FastAPI 服务、长期记忆。

## P1A：本地资料快照 → 确定性选型报告

### 数据流

```
fixtures/research/INDEX.json ──► DimensionRegistry（6 个维度，权重配平到 1.0）
        │
        ├── options/<candidate>.json ──► FixtureSnapshot（sections + dimension_values）
        │                                        │
        ▼                                        ▼
  FixtureResearchWorker  ── 每个候选 × 每个维度 ──►  ResearchFinding
  （candidate_id 从任务包 context 的显式行解析）        │
        │                                              │
        ├─ run()          → WorkerResult（核心口）      │
        └─ investigate()  → tuple[ResearchFinding,…]   │
                                                        ▼
                            assemble_report(brief, cards, registry, snapshots)
                            S0 预检 → S1 收集 → S2 归一+补齐 → S3 风险 → S4 推荐 → S5 完整性
                                                        │
                                                        ▼
                                              SelectionReport（report_id 内容寻址）
```

### 证据链

- **`EvidenceRef`**（`evidence.py`）是证据的唯一表示：`source_type` / `source_id` / `snapshot_version` /
  `locator` / `excerpt` / `captured_at`。
- `evidence_id` 与 `digest` 都是**内容寻址**：省略即由内容派生，显式给出必须等于重算值，
  否则拒绝构造（防伪造）。摘录改一个字符，两个标识都会变。
- `locator` 统一为**受限 JSON Pointer**（`/sections/<i>/text`），生成与解析共用同一个正则。
- 只有 `fixture_snapshot` 是**可信证据**（`is_trusted_evidence`）。`synthetic_mock` 是确定性占位，
  `legacy_text` 是历史自由文本的有损降级产物 —— 两者都不能单独支撑推荐。
- **文件级 digest**：`INDEX.json` 登记维度注册表与每个快照文件的 sha256，加载时校验。
  改过 fixture 必须重新签名：`python tools/sign_fixtures.py --write`。

### 评分

```
score(candidate) = Σ_d weight_d × utility_d(value_d)
unsupported 维度 → utility 记 0.0，并计入 missing_dimensions
```

- `utility_map` 是**唯一的方向来源**：语义写死为「utility 越大越符合选型目标」，
  因此没有 `higher_is_better` 这类翻转开关（两套方向语义并存等于两个真源）。
- 量表只支持 `ordinal` 与 `boolean`；`numeric` 在归一化区间与方向的语义定义清楚前**显式禁用**。
- **缺失维度按 0 分参与求和，而不是从分母里剔除** —— 剔除会把"资料不全"变成"没有劣势"。
- 求和用 `math.fsum` 对按 `dimension_id` 排序的贡献做精确求和，结果与累加顺序无关；**不做四舍五入**。
- 排名：`score` 降序，同分用 `candidate_id` 字典序 tie-break（无随机、无"任意顺序"）。

### 风险规则

| rule_id | 触发条件 | severity |
|---|---|---|
| `R_UNSUPPORTED_WEIGHTED` | 某候选在高权重维度（weight ≥ 0.15）上没有依据 | high |
| `R_UNTRUSTED_BACKING` | 某候选没有任何 fixture 证据支撑的结论 | high |
| `R_SINGLE_SOURCE` | 某候选的多条结论全部出自同一段原文 | medium |
| `R_SCORE_GAP_TIGHT` | top1 与 top2 分差 < 0.05，排名不稳健 | medium |
| `R_WORKER_REPORTED` | 上游 `WorkerResult.risks` 非空，逐条转记录 | low |

### 完整性校验 I1–I9

| 码 | 断言 | 可达性 |
|---|---|---|
| I1 | 同一个 `evidence_id` 始终对应同一份证据内容 | 防御性不变量 |
| I2 | 所有证据引用都能在 `evidence_index` 解析 | 防御性不变量 |
| I3 | 每条 supported finding 至少有一条非 legacy 证据 | 可被真实数据触发 |
| I4 | 每个候选至少有一条 **fixture 证据支撑**的 supported finding | 可被真实数据触发 |
| I5 | 每个候选 × 每个注册维度都有 finding（supported 或 unsupported） | 可被真实数据触发 |
| I6 | rank 恰为 `1..N`，且按 rank 排序后 `score` 单调不增 | 可被真实数据触发 |
| I7 | 推荐引用的 finding / risk 全部可解析 | 可被真实数据触发 |
| I8 | 维度权重之和为 1.0 | 可被真实数据触发 |
| I9 | 所有证据的 `source_type` 在白名单内 | 可被真实数据触发 |

`strict=True`（默认）下任一失败即抛 `ReportIntegrityError`，**不返回半成品报告**；
`strict=False` 返回 `integrity.passed == False` 的报告，失败码在 `integrity.failed_codes` 里。

### 确定性

- 报告里**没有 `generated_at`**，也不读时钟、不用随机。
- `report_id` 由内容投影 sha256 派生，投影**不含 integrity**（完整性是评估结果，不是内容）。
  这样 `strict=False` 的失败报告与同内容的成功报告共享同一个 `report_id`。
- 有回归锚点测试钉死真实 fixture 上的 `report_id`（`tests/research/test_assemble.py`）。

## 官方资料快照：真实资料接入第一步

**这一节说清一件事：系统是基于官方文档快照做确定性组装，不是实时自动调研。**

采集是 2026-09-23 人工一次性完成的：从各项目的**官方文档站源文件 / 官方代码仓库**下载文件原件，
按行号切片成摘录，把来源锚点写进快照。**运行时不联网**——快照是静态数据，
`agentboard` 全仓不 import 任何网络库（有静态守卫钉死）。

### 来源清单（候选固定为三个）

| 候选 | 官方文档 | 仓库 @ 采集时跟踪的 ref | **定位 commit** | 原件行数 | 采集日期 | release_tag |
|---|---|---|---|---|---|---|
| pgvector + PostgreSQL | `README.md` | `pgvector/pgvector@master` | `7db2345e…` | 1361 | 2026-09-23 | `unknown` |
| Milvus Standalone | `install_standalone-docker.md`<br>`install_standalone-docker-compose.md` | `milvus-io/milvus-docs@v3.0.x` | `756f9fbc…` | 143 / 157 | 2026-09-23 | `unknown` |
| Qdrant self-hosted single-node | `installation.md` | `qdrant/landing_page@master` | `32eb334f…` | 238 | 2026-09-23 | `unknown` |

**所有 URL 都固定到 commit，不用分支名。** `ref`（`master` / `v3.0.x`）只是"采集时跟踪的是哪条分支"
的记录，**不参与定位**：分支会移动，用它定位就再也对不上 sha256 与行号。所以
`url` / `raw_url` 一律是 `…/blob/<commit_sha>/<path>` 与 `…/<commit_sha>/<path>`，
有静态守卫拒绝"用分支名当 ref 的 URL"。

**三个候选的文档本身都不声明版本**：pgvector 的 README 没有版本号、Milvus 文档用
`{{var.milvus_release_version}}` 模板占位、Qdrant 文档仓库没有任何 release tag。
所以 `release_tag` 一律记 `unknown` 并写明原因，**不猜版本**；可复核的锚点是
`commit_sha` / `blob_sha` / `raw_sha256` / `raw_lines`。

### 每条来源登记了什么

`fixtures/official/sources/<candidate>.json` 的 `provenance` 块（schema `research.sources.v1`）：

| 字段 | 含义 |
|---|---|
| `url` / `raw_url` | 文件在官方仓库的页面地址 / 原始文件地址，**都固定到 `commit_sha`**（由 repo + commit + path 拼出并校验一致） |
| `repo` / `ref` / `ref_kind` | 官方仓库（白名单）、采集时跟踪的分支或标签（仅记录，不用于定位） |
| `commit_sha` / `blob_sha` | 采集时刻的 commit 与该文件的 git blob 哈希 |
| `raw_sha256` | **下载到的原始文件字节**的 sha256 |
| `raw_lines` | 上游文件总行数（末尾换行不计为额外一行）—— 用它把"引用了不存在的行"变成加载期报错 |
| `release_tag` / `release_tag_note` | 文档自身声明的版本；确认不了就 `unknown` + 原因 |
| `upstream_latest_release` | 采集时该代码仓库最新 release（仅参考，注明不代表被引文档对应此版本） |
| `upstream_license` | 上游许可证（来自 GitHub API，识别不出就写 `unknown` / `NOASSERTION`） |
| 每条 section 的 `source_id` / `line_range` | 该摘录来自哪份文件、在原文的哪几行（1 起闭区间） |
| 每个维度的 `basis` / `derivation` | 取值是原文直接陈述（`stated`）还是按量表映射（`mapped`）+ 映射过程 |

### 怎么复核（离线、一条命令）

核对链条完全由数据闭合：`raw_url`（固定到 commit）→ 原始字节 → ①`raw_sha256`
②`git blob sha1 == blob_sha` ③`raw_lines` ④按 `line_range` 切片 == `sections[].text`。

```powershell
# 1) 按快照里登记的 raw_url（固定到 commit）把原件下到 .raw/
python -c @"
import json, pathlib, urllib.request
out = pathlib.Path('.raw'); out.mkdir(exist_ok=True)
for p in pathlib.Path('fixtures/official/sources').glob('*.json'):
    for d in json.loads(p.read_text(encoding='utf-8'))['provenance']['documents']:
        t = out / d['path'].split('/')[-1]
        t.write_bytes(urllib.request.urlopen(d['raw_url']).read())
        print('downloaded', t.name, len(t.read_bytes()), 'bytes')
"@

# 2) 离线核对（不联网）：缺文件报缺失，内容对不上报问题，两者分开报
python tools/verify_sources.py --raw-dir .raw
```

这一步**是人工审计动作，不是系统能力**：`agentboard` 运行时不联网。
`--raw-dir` 里的文件按 **内容 sha256** 匹配（文件名随意），匹配不到再按文件名兜底 ——
所以"拿到的是分支上的新版本"会被报成 `raw_sha256 不符`，而不是被误报成"文件缺失"。

`tests/test_verify_sources.py` 默认只跑合成数据的正/负控制；把原件目录通过环境变量传进去，
还会跑一次真实语料的逐字节核对（4 份来源、8 条摘录）：

```powershell
$env:AGENTBOARD_SOURCE_RAW_DIR = ".raw"; python -m pytest -q tests/test_verify_sources.py
```

**本次复核结果**：4 份来源全部通过 —— `raw_sha256`、`blob_sha`（本地字节的 git 对象 id 与
GitHub 登记的 blob 哈希一致）、`raw_lines`（1361 / 143 / 157 / 238）、8 条摘录的行号切片与
快照摘录逐字节相同。

### 怎么用（复用现有管线，零适配层）

官方快照与 P1A 快照**共用同一套结构**（`research.fixture.v1` + `sections` + `dimension_values`），
来源元数据是额外的 `provenance` 块 —— 它只被 `research/sources.py` 读取，领域层看不到它，
因此不可能悄悄影响结论。于是：

```python
from agentboard.research.sources import load_official_sources

sources = load_official_sources(Path("fixtures/official"))
sources.findings("pgvector")                 # → tuple[ResearchFinding, ...]（证据是 EvidenceRef）
sources.resolve_evidence(ref)                # 证据 → SourceDocument（含官方 URL）
sources.worker()                             # 就是 P1A 的 FixtureResearchWorker
```

报告组装器、打分、风险规则、I1–I9 全部**原样复用**，`tools/sign_fixtures.py --root fixtures/official`
也直接可用（它只看 `dimension_registry` 与 `fixtures_path`，与 schema 无关）。

### 这一轮能确认什么、不能确认什么

维度**沿用项目既有定义与权重**：注册表与 `fixtures/research/dimensions/dimensions.v1.json`
逐字节相同（有测试钉死，改权重会被拦下）。本轮不新增维度、不改权重。

| 候选 | `deployment_complexity` | `ops_skill_floor` | `managed_service_needed` | `query_latency` / `scale_ceiling` / `ecosystem_maturity` |
|---|---|---|---|---|
| pgvector + PostgreSQL | `low` | `low` | `false` | **unknown** |
| Milvus Standalone | `medium` | **unknown** | `false` | **unknown** |
| Qdrant self-hosted single-node | `low` | `medium` | `false` | **unknown** |

三个候选一律 `unknown` 的维度，以及原因：

- **`query_latency`（weight 0.25）**：官方文档里没有可复核的绝对延迟数据。
  pgvector 的 README 只给出 HNSW 与 IVFFlat 之间的**相对**快慢比较，这不是延迟水平。
- **`scale_ceiling`（weight 0.15）**：pgvector 原文同时给出方向相反的两类事实
  （单表 32 TB / 分区可数千个该尺寸的表 ↔ 横向扩展需借助 replicas、Citus、PgDog 等外部方案），
  无法在同一量表上收敛；Milvus、Qdrant 的安装文档根本没谈承载量级。
- **`ecosystem_maturity`（weight 0.15）**：该维度定义包含"社区规模"，
  官方文档确认不了，量表（`nascent` / `growing` / `mature`）也没有可核对的判定依据。
  **本轮不写社区活跃度结论。**

其余三个维度的依据（都是 `basis: mapped`，即"事实 → 量表 → 标签"的映射过程写在 `derivation` 里）：

- `deployment_complexity`：pgvector「在每个库启用一次扩展」→ `low`；Milvus Compose 默认
  「一条命令起三个容器」→ `medium`；Qdrant「跑容器镜像是最省事的启动方式」→ `low`。
- `ops_skill_floor`：pgvector「监控复用 Postgres 既有工具」→ `low`；Qdrant
  「必须自行准备高性能持久化存储、自行配置安全设置」→ `medium`；Milvus 文档没谈运维门槛 → unknown。
- `managed_service_needed`：三者官方安装路径都是自建，`false`。

用同一套组装器跑出来的报告（`report_id = rp_9e0057b9f989020f`，可逐字节复现）：

| rank | 候选 | score | 有依据的维度 | 首要风险 |
|---|---|---|---|---|
| 1 | pgvector + PostgreSQL | 0.450 | 3 / 6 | `R_UNSUPPORTED_WEIGHTED` ×3 |
| 2 | Qdrant self-hosted single-node | 0.375 | 3 / 6 | `R_UNSUPPORTED_WEIGHTED` ×3 |
| 3 | Milvus Standalone | 0.200 | 2 / 6 | `R_UNSUPPORTED_WEIGHTED` ×4 |

10 条风险全部是 `R_UNSUPPORTED_WEIGHTED`（high），也就是说**这一轮报告的实质结论是"证据不足"**，
而不是"pgvector 更好"。两点必须一起读：

1. **名次反映的是「官方文档说了多少、说了什么」，不是客观优劣。** 缺失维度按 0 分参与求和
   （不从分母剔除），所以文档覆盖更完整的候选取分更高。这是被设计成可见的偏差，不是结论。
2. **覆盖是不对称的**：pgvector 与 Qdrant 各确认了 3 个维度，Milvus 只确认了 2 个。
   若要公平比较，正确做法是**补齐文档覆盖**（或明确规定"覆盖不全时不排序"），
   而不是用推断把缺口填平 —— 后者恰恰会把"资料不全"变成"没有劣势"。

## P2.1：真实资料检索与快照

**这一节描述的是本仓库里唯一会产生网络流量的代码。** 它是一次**受控的最小接入**：
只做"从一个真实来源检索并保存可复核快照"，**不实现自主研究 Agent**。

### 选源理由（依据官方文档，已核）

| 问题 | 结论 |
|---|---|
| 选哪家 | **GitHub REST API · Search repositories** |
| 端点 | `GET https://api.github.com/search/repositories?q=<terms>&per_page=<n>` |
| **要 API Key 吗** | **不需要**。公开只读端点匿名可用；`Authorization` 头是**可选**的 |
| **付费吗** | **不付费**。公开 REST API 免费 |
| 限额 | 未认证 10 次/分钟；带 token 30 次/分钟；单查询最多 1000 条、`per_page` ≤ 100 |
| 必需请求头 | `Accept: application/vnd.github+json`、`X-GitHub-Api-Version` |

**为什么选它**：项目既有的 `fixtures/official/` 全部锚定在 GitHub
（`sources.ALLOWED_SOURCE_HOSTS = {github.com, raw.githubusercontent.com}`），
选同一来源族可以**复用既有的 commit / blob_sha / raw_sha256 / 行号复核链条**，
而不引入第二套信任模型。

**为什么不选代码检索**（`/search/code`）：那个端点**强制要求认证**，
且只搜默认分支、只搜 <384KB 文件。仓库检索无需认证即可工作，
返回的 `full_name` / `html_url` 正好是可复核的稳定锚点 ——
用更少的凭据前提达成同一目标。

**token 的位置**：只从环境变量 `GITHUB_TOKEN` 读，**不落盘、不进仓库、不进快照**。
不存在 token 时以**匿名模式**运行（这是正常模式，不是降级模式）。
`provider.credential_mode()` 只返回 `"anonymous"` / `"token"` 标签，不返回凭据本身。

### 分层：网络只在一个文件里

```
src/agentboard/providers/            ← 唯一允许 import 网络库的包
  ├── interface.py                   Provider / Transport 协议 + RetrievedDocument（自校验）
  ├── http_transport.py              ★ 全仓唯一 import http / ssl 的文件（静态守卫豁免项）
  ├── github_search.py               GitHubRepositorySearchProvider（只依赖 Transport 协议）
  └── snapshot_store.py              内容寻址、不可覆盖的本地快照目录

src/agentboard/research/             ← 继续只读本地 JSON，不 import providers
src/agentboard/board.py 等核心         ← 继续纯调度，不 import providers
```

依赖方向是**单向的** `providers → core`。两条静态守卫把这件事钉死：

- `test_core_and_research_do_not_import_providers` —— 核心层与领域层不得反向 import；
- `NETWORK_IMPORT_ALLOWLIST` —— 网络库 import 的**唯一**豁免是 `http_transport.py`，
  且豁免集合必须与该文件**真实 import 的网络库恰好相等**
  （多引一个失败，文件被改成不联网后白名单还留着也失败，与时钟豁免同一套机制）。

**Provider 不产出 `EvidenceRef`。** `evidence.py` 的 `source_type` 是闭集，
且 `fixture_snapshot` 被登记为**可信**。让联网结果直接产出 `EvidenceRef`，
等于让"未经验证的联网内容"获得与人工核对过的快照同等的信任级别 ——
这正是禁止项"把联网采集结果伪装成静态 fixture"的实质。
所以 Provider 只产出 `RetrievedDocument`（带 sha256、来源 URL、采集时间），
**是否/如何转成证据由后续阶段显式设计，本阶段不做**。

### 快照长什么样、放在哪

**目录是 `var/snapshots/`，不在 `fixtures/` 下。** 这一条是刻意的：
`fixtures/research/` 与 `fixtures/official/` 是人工核对的静态资料、参与签名与基线；
联网采集的产物必须与它们**物理隔离**，否则"伪装成静态 fixture"会变成一次 `mv` 就能做到的事。

文件名 = 内容哈希（`sha256-<64hex>.json`），写入用**独占创建 + 原子替换**。
每份快照记录七个字段：

| 字段 | 说明 |
|---|---|
| `query_terms` | 查询词（只折叠空白，不改大小写） |
| `source_url` | 来源 URL |
| `title` | 标题 |
| `captured_at` | 采集时间（`YYYY-MM-DD`，**由调用方传入，不读时钟**） |
| `source_id` | 来源标识（含 `provider_id`） |
| `content` | **原文内容**（来源给的 JSON 原样，不是我们的转述） |
| `content_sha256` | 原文的 SHA-256 |

两条硬性质，用机制而不是自觉保证：

1. **不可覆盖** —— 内容寻址文件名 + `FileExistsError`。覆盖既有快照需要一次**刻意**的
   `unlink`，不可能由不小心的 `open(..., "w")` 造成。
2. **相同内容不重复落盘** —— 命中哈希即返回 `duplicate`，**一个字节都不改**
   （"不覆盖也不新建"，不是"删掉旧的再写新的"）。

`content` 存**来源给的原文 JSON**而不是我们转述的一句摘要：若存转述，
sha256 只能证明"我们的转述没变"，证明不了"来源内容没变"。
同理，哈希输入**逐字节等于原文**，不做 `strip()` ——
先 strip 再哈希会让"只改了空白"的第二次采集被误判为重复而静默丢弃。

### 超时 / 限额 / 重定向 / 非法 URL

| 防护 | 实现 |
|---|---|
| 超时 | `TransportRequest.timeout_seconds`（默认 10s），超时抛 `RetrievalTimeoutError` |
| 响应大小 | `max_response_bytes`（默认 1 MiB），**边读边判**，超限立刻 `ContentTooLargeError` |
| 重定向 | **手工走跳转**，逐跳过白名单校验，跳转链记进 `redirect_chain` |
| 非法 URL | 正则 + 显式主机白名单；`http.client.InvalidURL` 映射为 `InvalidSourceUrlError` |

重定向的三条规则：必须是 https（禁降级明文）、主机必须在白名单内（禁 302 到内网 /
第三方 —— 典型 SSRF 形态）、次数有上限。
**不用 `urllib.request`** 的原因：它自动跟随重定向且不暴露每一跳，
而"跳到哪里"必须是可断言的数据，不是隐式行为。

**代理支持是必需的，不是可选项。** `http.client` 不会读系统/环境代理设置，
而 `urllib.request` 会。在必须走代理才能出网的机器上，症状是
"`urllib` 能通、`HTTPSConnection` 直连超时"（实测 `WinError 10060`）。
所以 transport 显式解析 `HTTPS_PROXY` / `HTTP_PROXY`（及 `NO_PROXY`）并走 CONNECT 隧道。

### 怎么复核（离线 / 真实）

```powershell
# 默认测试全程离线（用 Mock transport，不联网）
python -m pytest -q

# 可选的真实 smoke test：默认跳过，必须显式开
$env:AGENTBOARD_REAL_SMOKE = "1"
$env:GITHUB_TOKEN = "ghp_xxx"          # 可选；不设则以匿名模式运行
python -m pytest tests/providers/test_real_smoke.py -v -s

# 看落盘的快照
ls var/snapshots
```

### 真实 smoke test 执行记录（2026-09-23）

**已执行，通过。** 来源 = GitHub REST Search API，**凭据模式 = anonymous（未使用 token，无费用）**。

| 轮次 | 结果 |
|---|---|
| 第 1 次 | 取回 **3** 条，新增 **3** 份快照，跳过 0 条 |
| 第 2 次（同查询词） | 取回 **3** 条，新增 **0**、**重复 3** —— 3 份文件 sha256 **与第 1 次逐字节相同** |

命中的三个仓库（查询词 `vector database plugin`，limit 3）：
`VexDB-THU/VexDB-Lite`、`tickernelz/opencode-mem`、`Stezii/tilestrata-postgismvt`。

第 2 次采集的"重复 3、字节未变"是对**真实来源**验证了核心要求
"相同内容重复采集不得悄悄覆盖旧快照"，不只是 Mock 层面的验证。

> **过程中发现并修掉一个真实缺陷**：首轮直连超时（`WinError 10060`），
> 而同样环境下 `urllib` 能通 —— 根因是 transport 忽略了代理。
> 补上代理支持时又踩到第二层：`set_tunnel` 的参数语义是"**CONNECT 到**谁"，
> 即 `HTTPSConnection(代理)` + `set_tunnel(目标)`；写反不会报参数错，
> 而是表现成 `RemoteDisconnected`（看起来像网络故障）。两处都已修，并各有测试钉死。

## P2.2：信任边界 —— 官方原文才可作研究级支撑

**这一节回答一个问题："凭什么说一条推荐有资格叫研究级？"** 答案是一条机器可查的链：
`官方原文摘录（source_kind + provenance）` × `外部核对证明（attestation）` ×
`与本次组装数据的逐字节绑定`。

### 两个信任谓词的分工

| 来源 | `is_trusted_evidence`（既有，冻结） | `is_promotable_source`（新增） |
|---|---|---|
| `fixtures/official/`（官方原文） | 是 | **是**（C1–C8 全过 + 有效 attestation） |
| `fixtures/research/`（人工摘要） | 是（仅为 P1A 回归兼容） | **否** —— 永不 |
| 采集层产物（P2.1） | 不是 `EvidenceRef` | 否（类型不可达） |

人工摘要**能过**既有宽松检查以复现 P1A 基线，但**永远不能**支撑研究级推荐；
这就是"两类内容实际同权"（D1）的修复方式：不收紧旧谓词（会改 `report_id`），并列新增更严的。

### attestation：来源核对的外部证明

`attestations/official.sources.v1.json` 是独立于 fixture 的证明工件（生成方式、
W1–W4 写入策略、**信任模型与能力边界见 `attestations/README.md`**）。要点：

- **自绑定**：证明锚定的是文件字节（`index_sha256` + 全部 `snapshot_files[].sha256`），
  不是"声明值"——重签 INDEX 或改任何快照字节都会使旧证明失效（这是设计，重新生成即重新核对）；
- **loader-only 绑定**：`binding` 字段不在构造器签名里，只有 `load_attestation()` 能建立；
- **与本次数据绑定（B6 修复）**：`research_grade` 组装必须经 `load_snapshot_set()` 工厂
  （root ↔ attestation 同源），组装期 S0.5 **现读磁盘现算**——内存快照、证据必须逐字节
  对回已核验文件。相同 `snapshot_set_id` / 相同 `source_kind` / "曾经核对过"都**不构成**证明。

### research_grade 组装

```python
snapshot_set = load_snapshot_set(
    fixture_root=Path("fixtures/official"),
    attestation_path=Path("attestations/official.sources.v1.json"),
    policy="research_grade",
)
report = assemble_report(brief, cards, registry, snapshots=snapshots, snapshot_set=snapshot_set)
```

I10 只影响完整性判定、不改报告内容 ⇒ 两态 `report_id` 相同；证明失效时
`strict=True` 抛 `ReportIntegrityError`、`strict=False` 返回带原因明细的未通过报告
（absent / unbound / version_expired / mismatch / incomplete /
uncovered_candidate / disk_mismatch / tampered_in_memory / evidence_mismatch —— 可区分）。

### 官方评测（`eval/official/`）

6 个固定场景 + §6.7 官方指标：升格比例、人工摘要混入计数（硬门禁 0）、
attestation 状态（present / overall_result / 自绑定 / 不可升格原因——可区分）、
来源锚点核对比例（problem 与 missing 分开）、证据回溯到 `SourceDocument` 的比例。

## P3：live-research MVP —— live retrieval orchestration（检索编排）流水线

**这一节描述的是"任务输入 → 研究计划 → Provider 检索 → 来源快照 → 证据校验/映射 →
确定性报告"的最小可运行闭环**（`src/agentboard/runtime/`）。

**如实定位（不可夸大）**：本阶段交付的是**真实检索接线 + 确定性流程脚手架**，
**不是自主研究 Agent**：

- 没有长期记忆、没有自主规划改写、没有受控生成；
- **当前决策器（`ScriptedDimensionDecider`）不是自主推理**：维度取值由场景作者
  在映射表里显式声明，运行时只做合法性校验与审计——它**不能**自主理解取回的
  资料并推导技术结论；
- 因此不得宣称"已实现自主推导技术结论的能力"。受控生成的决策器是**后续阶段的
  未完成项**（P4 候选），本阶段不会通过改文案把它伪装成已完成。

| 段 | 机制 | 可观测 |
|---|---|---|
| 研究计划 | 候选 × 维度排序展开为检索步骤（无查询改写） | 步骤 id / 状态机（pending→succeeded/failed/skipped） |
| Provider 检索 | 注入 `Provider`（离线脚本替身 / **opt-in** 真实 GitHub 检索）；预算超时透传到 Provider/传输层 | 每步 attempts / 重试计数 |
| 去重与快照 | `SnapshotStore` 内容寻址落盘（同内容只存一份，不可覆盖） | written / duplicate 计数（按 Store 实际写入/重复结果累计） |
| 证据映射 | 可插拔 `DimensionDecider`（**离线为脚本决策器，非自主推理**） | 决策合法性校验（量表内 / section 存在） |
| 缺口补检 | 无材料的 (候选, 维度) 追加一次变体查询（预算允许时） | 补检步骤同样入状态机 |
| 确定性报告 | 语料 → 既有 loader → `FixtureResearchWorker` → `assemble_report` | `report_id` 内容寻址可复现；`run_id` 覆盖全部影响报告内容的输入（维度定义/权重 + 文档摘要 + 最终决策） |

### 证据边界（不可协商）

Provider 产物**永远不直接构造 `EvidenceRef`**（L3 构造点白名单继续生效）。
唯一路径：快照落盘 → 语料构建（fixture 格式，`source_kind=authored_summary`）→
既有 loader（C7 文件级 digest）→ 受控工厂。因此 **live 内容在 MVP 信任模型下
不可升格**（`research_grade` 不可达）—— 机器采集的材料必须先经人工复核，
这与 P2.2 的信任边界是同一条线。

### 预算与失败 / 降级语义

- **计数式预算**：步数 / Provider 请求数 / 内容字符数 / 重试次数（不读时钟）；
  预算耗尽 ⇒ 剩余步骤 `skipped`（原因 `budget_exhausted:*`），用已取得材料走降级路径；
- **可重试错误闭集**（传输 / 超时 / HTTP / 响应结构 / 超大内容 / 重定向）按上限重试，
  耗尽 ⇒ 步骤 `failed`（带原因）；**未知错误 ⇒ `ProviderStepError` 显式失败，禁止吞错**；
- **候选零材料** ⇒ `degraded_candidates`（不进报告）；**全部零材料** ⇒
  `outcome=failed`（原因 `no_snapshots`）；**存活候选不足两个** ⇒
  `insufficient_candidates`；组装期完整性失败 ⇒ `assemble_failed:<码>`——全部可观测；
- **离线默认，真实 opt-in**：测试与评测用 `ScriptedSearchProvider`（脚本化文档与失败
  队列）；真实 GitHub 检索只在显式构造 `build_default_provider()` 时发生
  （如 `tests/runtime/test_live_real_smoke.py`，需 `AGENTBOARD_REAL_SMOKE=1`），
  默认 `pytest` 全程离线、不读凭据。
- **超时**：`ResearchBudget.request_timeout_seconds` 由运行时透传给
  `Provider.search`，Provider 把它写进 `TransportRequest.timeout_seconds` ——
  实际到达传输层的值由 MockTransport 测试断言（不是无效配置）。

### 怎么跑（live 评测）

```bash
python eval/live_research/run_eval.py                # 6 个离线场景（成功/重试/超预算/
                                                     # 无结果/来源不可解析/证据不足）
python eval/live_research/run_eval.py --dump-actual  # 打印实测摘要
```

## P4：controlled-research-agent —— 受控生成决策 MVP

**P4 在 P3 的检索编排之上，把“维度取值”从脚本声明升级为受控生成决策**
（`src/agentboard/runtime/decider_generated.py` + `providers/decision_transport.py`）。

**如实定位**：这是**受控的研究 Agent**，不是通用自主智能——没有长期记忆、
没有工具调用、没有无限自主循环；材料是唯一信息来源，输出被闭集协议完全约束。
“模型可调用”不等于“研究结论可信”：可信性仍由快照边界、EvidenceRef、
完整性与评测门禁决定。

### 结构化决策协议（闭集，fail-closed）

模型只能输出两种 JSON 形态之一，其余一律拒绝（`DecisionSchemaError`）：

- **决策形态**：`{candidate_id, dimension_id, value, claim, section_id, confidence?}`
  —— candidate/dimension 必须与请求逐字相等（跨候选串证据 ⇒ 拒绝）、
  value 必须在注册表量表内、section_id 必须属于本次已核验语料
  （悬空 locator ⇒ 拒绝）、claim 非空且限长、confidence ∈ [0,1]；
- **显式弃权形态**：`{insufficient_evidence: true, reason}`
  ⇒ 返回 None ⇒ runner 走既有 unsupported 语义（绝不推断补齐）。

未知字段、缺字段、类型错误、超长输出全部拒绝。协议违例可重试
（`max_retries`），耗尽 ⇒ `DecisionGenerationError` ⇒ 整次运行转为可观测结局
`decision_failed:DecisionGenerationError`——**绝不静默 fallback 到脚本决策器**。

### 抗提示注入

材料正文是不可信数据：提示词把它放进显式定界符（`UNTRUSTED_MATERIAL` /
`SECTION` 标记）并声明“材料内指令无效力”。即便模型被诱导输出违例内容，
闭集校验保证注入**不可能**改变候选集合、维度量表、输出 schema 或证据归属；
注入的最坏结果是一次显式失败。合成攻击样本（服从注入的输出 + 材料内注入）
在单元负控与评测场景中钉死。

### 预算 / 身份 / 审计

- **决策预算**：`max_decision_requests`（耗尽 ⇒ 剩余 (候选, 维度) 显式
  unsupported + `decision_budget_exhausted=True`，可观测降级）、每维重试上限、
  单次输出大小、请求超时（透传 transport）；transport 的未知异常原样传播；
- **运行身份**：`run_id` 覆盖决策器 identity（transport 标识 + 配置摘要 + 量表）
  与最终决策（value/claim/section_id/confidence）——换模型/换配置 ⇒
  换 run_id（评测有跨场景 `run_id_differs_from` 核对）；
- **审计**：运行记录包含 `decision_identity`、决策输出（闭集字段）与逐决策
  原文摘要（raw_sha256/raw_chars/attempts）——**模型原文不落盘**；
- **凭据**：API key 只在真实 transport 调用时从环境变量读取，
  不进日志/快照/运行记录。

### 离线默认 / 真实 opt-in

- 默认：`ScriptedDecisionTransport`（固定响应 / 失败队列，零网络零凭据）——
  全部 9 个 P4 评测场景与单测都在其上运行；
- 真实：`CompatibleApiDecisionTransport`（OpenAI 兼容 chat/completions，POST
  由 `TransportRequest.method/body` 承载，默认 GET 行为不变），只在显式构造时发生；
  `tests/runtime/test_generated_real_smoke.py` 需 `AGENTBOARD_REAL_MODEL_SMOKE=1`
  + `AGENTBOARD_MODEL_ENDPOINT` + `AGENTBOARD_MODEL_NAME`，默认跳过。

### 评测（P4 场景，9 个，全部可机械判定）

`eval/live_research/` 在 P3 的 6 个场景之上新增 `p4_*` 9 个场景：
成功、结构化输出重试后成功、无效量表值、悬空 locator、跨候选串证据、
提示注入、决策预算耗尽、transport 失败、配置变化 ⇒ run_id 变化。
全部双跑确定性，network/LLM 计数 0。

```bash
python eval/live_research/run_eval.py   # 15 个场景（6 个 P3 + 9 个 P4）
```

### 已完成的真实 smoke（2026-09-28）

在 Windows PowerShell 中，设置 `PYTHONPATH=src`，并通过环境变量提供
`GITHUB_TOKEN` 与 `DASHSCOPE_API_KEY`（不要写进来源计划或提交到项目）：

```powershell
$env:PYTHONPATH = "src"
python -m agentboard live --goal "比较 pgvector 与 qdrant 的技术选型适配性" `
  --candidates "pgvector,qdrant_local" --sources "var/p6-smoke-sources.json" `
  --out "var/p6-live-final-20260929" --model-preset dashscope --model-name qwen-plus `
  --decision-retries 0 --max-decision-requests 16 --max-card-attempts 1 --max-loop-steps 16
```

`--out` 请换成**尚不存在的新目录**，勿覆盖审计产物。本次实测 `outcome=completed`，
verifier verdict=`passed`、来源 2 件、12 次模型决策请求、4 次 GitHub HTTP 请求，
`run_id=ma_ccab32b3023b1c7e`，`final_report_id=mafr_d695c4dcdd76545b`，
完整研究报告 `report_id=rp_4b666803a174656e`。该 ID 只对应这一次模型输出，
不承诺下次 live 调用相同。实际报告有 12 条 findings，其中 9 条 supported；
模型判读仍需人工复核，不构成通用自主研究或 `research_grade` 认证。

### 仍未实现（如实登记）

- 真实模型 smoke 未在开发中实际执行（需要端点与凭据；默认门禁不含）；
- 受控生成决策**不做**材料级事实核查（核查仍依赖人工复核 live 材料，
  `authored_summary` 不可升格的信任边界不变）；
- 无多轮检索规划、无跨 run 记忆——后续候选，本批不宣称。

## P5：multi-agent-orchestration-mvp —— 多 Agent 协作研究编排

**P5 在单运行时闭环之上增加可观察、可重试、受隔离约束的多 Agent 协作层**
（`src/agentboard/orchestrator/`）：

    Planner.propose → 计划级护栏（板校验 + 角色图）→ 任务板状态机
    → dispatch/retry（isolation 收窄任务包）→ researcher（P4 runtime）
    → verifier（独立重验）→ synthesizer（只消费验证通过结论）→ 最终报告

### 四类角色（结构化契约交互）

| 角色 | 职责 | 输入边界 |
|---|---|---|
| planner | 只提议任务计划，不写运行时状态 | goal → raw board（经护栏校验提交） |
| researcher | 执行研究（接入 P3/P4 runtime + GeneratedDimensionDecider） | 私有任务包 + 显式声明的 candidate_ids |
| verifier | 独立重验上游产物：digest 重算 / 完整性复核 / 证据结构 / 范围核对 | 只见任务包声明的上游结果 |
| synthesizer | 只消费 verdict=passed 的结论，产出最终报告 | verdict 载荷（闭集） |

角色只能通过 `WorkerTaskInput` / `WorkerResult` 交互；不能直接修改任务板、
不能读取未声明上下文。角色图（researcher←researcher、verifier←researcher、
synthesizer←verifier）在**计划层** fail-closed 校验——"synthesizer 越过
verifier"的计划在派发前就被拒绝。

### verifier 的"独立重验"（如实边界）

verifier 重算 sha256(report_json)，严格重建 SelectionReport 并核对内容寻址 id；
独立重跑 I1–I9（不接受空 checks 或自报全绿），复用 score_all/build_recommendations
重算评分、排名及推荐支撑，并检查候选范围与跨候选证据。写板前还会从实际 verifier
上游重算 final_report_id，拒绝合成器伪造 id 或绕过 rejected verdict。

它**不重新执行研究流水线，也不重新抓取/核对来源原件**——当前复核范围是
报告的结构、派生结果及可重算业务不变量，而不是证明每句话真实。
verdict=rejected ⇒ synthesizer 失败 ⇒ 重试耗尽 ⇒ blocked：
被 verifier 拒绝的产物不能进入最终报告，不将“门禁通过”宣称为来源真实性保证。
ScriptedReportWorker 复用本地回归 fixture 生成有完整 I1–I9 的报告；人工摘要
仍仅用于离线回放，不因本阶段测试通过而升格为研究级来源。

### fail-closed 清单（每条有评测场景钉住）

计划未知依赖 / 未知角色 / synthesizer 越权 → plan_rejected；未知 worker →
运行失败（不 fallback）；载荷越权 task_refs → 卡片失败；结果与卡片不一致 /
过期卡片版本 → board_invariant_violated；verifier rejected → blocked；
循环步数预算耗尽 → loop_budget_exhausted。

### run_id 与审计

编排 run_id（`ma_` 前缀）为规范 JSON 摘要：goal、planner 身份、编排配置、
各 worker identity、完整任务板状态、**事件序列及 data（不含时间戳）**、验证结论、
最终载荷及 report_id、outcome/failure_reason。同输入稳定；改变任一决定性输入必变（跨场景核对）。
事件序列稳定且包含失败/重试/阻塞原因；审计不含凭据与整段模型原文。

### 离线默认 / 真实 opt-in

- 默认：`MockPlanner` + `ScriptedReportWorker`/`RuntimeResearchWorker`
  （离线 provider + P4 受控生成决策器脚本 transport）+ `VerifierWorker` +
  `SynthesizerWorker`——零网络零凭据；
- 真实外部子 Agent / ACP：**协议适配器仍未实现**（如实登记为未完成能力）。
  编排核心只认 `WorkerAdapter` 协议，外部实现注册后即被使用；adapter 失败
  走任务板失败语义并记入事件序列，**编排器没有任何静默回退到 Mock 的路径**。

### 评测（P5 场景，10 个，全部可机械判定）

`eval/multi_agent/`：正常协作（runtime 接入）、依赖顺序、worker 重试、
三次失败 blocked、越权上下文、不一致卡片结果、verifier 拒绝、synthesizer
越过 verifier、计划未知依赖、配置变化 ⇒ run_id 变化。全部双跑确定性，
network/LLM 计数 0。

```bash
python eval/multi_agent/run_eval.py   # 10 个场景
```

### 仍未实现（如实登记）

- 真实 ACP / 外部子 Agent 协议适配器（编排核心已预留协议槽位）；
- verifier 不重新执行研究流水线，只独立验证产物自洽性；
- 无并行派发（单线程顺序调度）、无跨 run 记忆——后续候选，本批不宣称。


## P6：real-e2e-and-external-worker —— 真实接线 + 命令行入口

**P6 提供可用命令行入口（`python -m agentboard`）并完成真实端到端接线**：
目标 → 有界计划 → 真实检索与固定 commit 原文 → 结构化发现（真实模型决策）→
独立 verifier → 最终报告。**不宣称通用自主智能**；不接 ACP/LangGraph/FastAPI/
长期记忆。

### 三种模式（边界显式）

```bash
# 1) replay：本地冻结快照 → 确定性报告（零网络零凭据，report_id 可逐字节复现）
PYTHONPATH=src python -m agentboard replay \
  --goal "为检索服务做技术选型" --out runs/replay

# 2) offline：P5 编排闭环（脚本检索 + 脚本决策 transport）——
#    明确标注 scripted，用于流程演示与离线验收，不代表真实能力
PYTHONPATH=src python -m agentboard offline \
  --goal "为检索服务做技术选型（脚本演示）" \
  --candidates pgvector,qdrant_local --out runs/offline

# 3) live：真实接线（GitHub contents API 固定 commit 原文 + OpenAI 兼容真实模型）
PYTHONPATH=src python -m agentboard live \
  --goal "为向量检索服务做技术选型：对比 pgvector 与 qdrant 本地部署两个候选" \
  --candidates pgvector,qdrant_local \
  --sources sources.plan.json \
  --out runs/live \
  --model-preset dashscope --model-name qwen-plus \
  --decision-retries 4 --decision-timeout 120 --request-timeout 45
```

`sources.plan.json` 形态（每候选 1..4 件，**有界**，不做全网检索）：

```json
{"candidates": {
  "pgvector":     [{"repo": "pgvector/pgvector", "path": "README.md", "ref": "HEAD"}],
  "qdrant_local": [{"repo": "qdrant/qdrant",   "path": "README.md", "ref": "HEAD"}]
}}
```

产物（写入 `--out` 运行目录）：`report.md`（含经 verifier 通过的完整研究报告）+
`report.json`（含 `selection_reports`）+ `run_record.json`（run_id / 事件序列 /
board / verdicts / 审计）+ live 模式的 `sources.json`（逐件 repo/path/固定 commit/
规范 URL/sha256/Git blob SHA）。

### 真实接线的证据边界

- **检索摘要不是证据**：live 模式按来源计划经 GitHub contents API 取**原文全文**
  （base64 解码），并解析出 commit 写进规范 URL
  （`https://github.com/{repo}/blob/{40位commit}/{path}`）——重新取回同一 URL
  重算 sha256 即可重验（真实运行已双 MATCH 验证此路径）；
- **决策 transport**：P4 `GeneratedDimensionDecider` + `CompatibleApiDecisionTransport`
  （DashScope preset：端点固定官方兼容模式地址，key 只从 `DASHSCOPE_API_KEY`
  读取，不回显不留存）；闭集协议校验不变；
- **来源审计**：researcher 载荷携带 `source_audit`（URL/commit/sha256/Git blob SHA）
  与 `run_audit`（预算消耗 / HTTP 调用数 / 模型调用数）；verifier 不采信自报摘要，
  将本次实际取回的原文字节重算并与来源审计及每条报告证据的 locator/摘录绑定。
  这证明来源及结构一致，不证明模型结论正确，也不自动升格为 `research_grade`；
- **外部 worker**：`workers/external.py` 的 `ExternalModelWorker` 实现
  `WorkerTaskInput/WorkerResult` 契约并接入 P5 编排（复用同一模型 transport）；
  其结论显式携带 `unverified: true`，verifier 将其标注为**未验证材料**——
  结构与范围可验证，内容不声称已核实；
- **缺配置即退出**：live 缺端点/模型名/凭据 ⇒ 一次性列出缺失项、退出码 2，
  **发起任何网络请求之前**终止；远端失败/超预算 ⇒ 显式失败或部分结果，
  绝不静默回退到脚本/Mock。

### 已知环境限制（如实记录）

- 本开发网络下 `raw.githubusercontent.com` 不可达（超时/断连）——原文获取与
  重验均走 `api.github.com` contents API（同固定 commit 等价字节）；
- GitHub 匿名限额可能阻塞真实运行；`GITHUB_TOKEN` 可在环境中提供，
  仅用于 GitHub API 认证，密钥不写入产物。
- 真实模型输出有随机性：live 结果不承诺逐字节重复，稳定性以冻结输入/回放为准。

### 仍未实现（如实登记）

- 真实 ACP 协议适配器（未确认官方协议与可用端点前不自造 HTTP 接口冒充）；
- 多候选来源自动发现（来源计划目前人工显式声明，有界）；
- 并行派发与跨 run 记忆——后续候选，本批不宣称。

## 怎么跑

全部命令只用 Python / PowerShell 可执行，**不依赖 git、grep 或任何 shell 工具**
（本环境没有 `.git`，且 coreutils 不保证存在）。

```powershell
# 测试
python -m pytest -q

# lint（范围固定：scripts/ 是红线，其中的既有债务不处理）
python -m ruff check src tests tools

# 课程原始资产未被改动
python tools/verify_manifest.py --check

# fixture 结构 + 文件级 digest（P1A 人工摘要）
python tools/sign_fixtures.py --check

# 官方资料快照（结构与 digest，同一把工具、不同目录）
python tools/sign_fixtures.py --root fixtures/official --check

# 官方来源的离线复核（需要先用快照里的 raw_url 把原件下到 .raw/，见上节）
python tools/verify_sources.py --raw-dir .raw

# 固定场景评测（输出 JSON 指标）
python eval/research/run_eval.py

# P2.2：官方快照集评测（含 research_grade / attestation / 来源锚点指标）
python eval/official/run_eval.py
python eval/research/run_eval.py

# P2.1：Provider 层测试（Mock transport，离线）
python -m pytest tests/providers -q

# P2.1：可选真实 smoke test（默认跳过；需显式开启）
$env:AGENTBOARD_REAL_SMOKE = "1"
python -m pytest tests/providers/test_real_smoke.py -v -s

# 有意修改 fixture 后重新签名
python tools/sign_fixtures.py --write
python tools/sign_fixtures.py --root fixtures/official --write
```

## 评测结果

`python eval/research/run_eval.py` 在 10 个固定场景上实测（每场景独立跑两遍）：

| 指标 | 实测 |
|---|---|
| `scenarios_total` | 10 |
| `scenarios_passed` | 10 |
| `determinism_repeat_rate` | 1.0 |
| `evidence_resolvable_rate` | 1.0（195/195 条 fixture 证据的 locator 与原文逐字一致） |
| `fixture_evidence_rate` | 1.0（95/95 条推荐支撑证据来自 fixture） |
| `silent_missing_dimensions` | 0 |
| `integrity_checks_executed` | 63（7 个产出报告的场景 × 9 条检查） |
| `network_calls` | 0 |
| `llm_calls` | 0 |

场景覆盖：正常完整报告、缺失维度导致掉名次、无可信证据（I4 失败）、legacy 证据（I3+I4 失败）、
未知维度被拒、候选缺失被拒、分数并列 tie-break、重复运行一致性、单一证据来源、悬空证据被拒。
缺陷场景一律断言**明确的失败码**（`failed_codes` 与 `strict_error_codes`），而不是"抛了个异常就算过"。

`network_calls` 的口径是**连接非本地地址的尝试次数**：Windows 上 `asyncio` 用 loopback
socketpair 做自唤醒管道，把它算作出网会让指标被运行时噪音污染。全部连接尝试次数另记在
`detail.all_socket_connect_attempts` 里。

**P2.1 未改动这组数字**：评测跑的是报告组装链路，它与 `providers/` 无依赖关系。
上面这张表的数值在 P2.1 前后**完全一致**（含 `network_calls = 0`），
这本身就是"核心调度与 P1A 离线评测不依赖网络或 Provider"这条要求的证据。

## 目录

```
src/agentboard/
  config.py        护栏常量
  evidence.py      EvidenceRef（内容寻址证据）
  contracts.py     数据契约（frozen）
  events.py        结构化事件
  validation.py    任务板校验
  board.py         状态机（step/retry/failed/blocked + 事件）
  isolation.py     上下文隔离
  planner.py       规划边界（提议 vs 提交）
  workers/         WorkerAdapter 协议 + MockWorker + SyntheticEvidenceFactory
    research/        技术选型研究领域层
    config.py        领域常量与正则
    errors.py        异常族
    models.py        ComparisonDimension / ResearchFinding / RiskItem /
                     Recommendation / CoverageStats / SelectionReport
    dimensions.py    维度注册表（alias 归并 + 权重配平）
    snapshot.py      快照加载 + 文件级 digest + locator 生成/解析
                     + SnapshotSet / load_snapshot_set（research_grade 声明与同源绑定）
    attestation.py   SourceVerificationAttestation（C6 外部证明 + 文件级自绑定）
    payload.py       structured_payload 编解码
    fixtures.py      FixtureResearchWorker
    sources.py       官方资料快照的来源层（provenance 校验 + 证据回溯 + live_reverify）
    migrate.py       legacy 文本证据的显式迁移器
    rules.py         风险规则
    scoring.py       确定性打分与排名
    integrity.py     I1–I9 + I10_NON_PROMOTABLE_SUPPORT（research_grade 强制点）
    assemble.py      assemble_report（S0→S0.5→S5；S0.5 为组装期绑定重验）
  providers/        真实资料检索（P2.1）——**全仓唯一允许联网的包**
    interface.py      Provider / Transport 协议 + RetrievedDocument（sha256 自校验）
    http_transport.py ★ 唯一 import http / ssl 的文件（静态守卫豁免项）+ 代理 CONNECT 隧道
    github_search.py   GitHub 仓库检索 Provider（官方 REST API，匿名可用、不付费）
    snapshot_store.py  内容寻址、不可覆盖、按哈希去重的本地快照目录
    errors.py          Provider 层异常族
fixtures/research/   INDEX + 维度注册表 + 3 个候选快照（人工撰写的技术摘要）
fixtures/official/   官方文档逐行摘录：INDEX + 维度注册表（与上者逐字节相同）+ 3 份来源快照
fixtures/baselines/  课程原始资产的 sha256 清单 + 全树快照
var/snapshots/       P2.1 联网采集产物（运行时目录，不进基线、不参与签名）
tools/               verify_manifest.py（基线校验）+ sign_fixtures.py（快照签名）
                     + verify_sources.py（官方来源离线复核）
eval/research/       run_eval.py + scenarios/ + expectations/（P1A 10 场景，expectations 冻结）
eval/official/       run_eval.py + scenarios/ + expectations/（P2.2 官方场景 + §6.7 指标）
eval/live_research/  run_eval.py + scenarios/ + expectations/（P3 live-research MVP 端到端离线评测）
attestations/        C6 外部证明工件（official.sources.v1.json）+ README（信任模型）
                     —— 缺失 ⇒ 可回放但不可升格；存在与否不影响 G1–G3（G4）
runtime/             P3 研究运行时：plan / budget / runner / corpus（快照-证据边界）
                     + mock_provider（确定性离线 Provider 替身）
tests/               确定性单测；tests/research/ 为领域层测试；tests/providers/ 为 Provider 层测试；
                     tests/runtime/ 为 P3 运行时测试
scripts/             原课程脚本（未改动）
```

## 已知边界

- **信任模型是过程保证，不是密码学保证**：attestation 不做数字签名 ——
  能改写仓库的人也能同步改写工件。它证明的是"有人在某个时点按登记规则
  把快照与真实原件逐字节核对过"，完整性依赖 git 历史 + 评审 + G-PRESERVE 门禁。
  需要"不依赖 attestation 可信度"的场合用 `sources.live_reverify` 当场用本地原件重算
  （missing 与 problem 分开报）。详见 `attestations/README.md`。
- **"研究级"的边界**：`research_grade` 只在显式传入 `load_snapshot_set()` 产出的
  快照集声明时启用；证明必须与**本次组装的数据**逐字节绑定（组装期现读现算），
  相同 `snapshot_set_id`、相同 `source_kind`、"曾经核对过"都不构成证明。
  缺证明 / 证明失效 ⇒ 报告不可升格（原因可区分），但**加载与回放照常**。
- **live-research MVP 的边界（P3/P4）**：它是"检索 → 快照 → 证据映射 → 确定性报告"的
  最小闭环，**不是**通用自主智能体——没有长期记忆、没有自主规划改写、没有无限工具循环。
  P4 起维度取值由**受控生成决策器**给出（闭集 JSON 协议 + fail-closed 校验 +
  抗注入定界），默认走离线脚本 transport；真实模型仅显式 opt-in。
  **"模型可调用"不等于"研究结论可信"**：可信性仍由快照边界、EvidenceRef、
  完整性与评测门禁决定。live 采集的材料经
  快照/验证边界落盘后按 `authored_summary` 信任层处理，**不可升格**（research_grade
  不可达），必须先经人工复核。默认测试全程离线。

- **研究能力是快照回放**，不是实时调研；两个 fixture 目录分别用 `source_kind` 标注是人工摘要还是官方摘录。
  **报告组装链路没有实现联网搜索**：采集在 2026-09-23 一次性完成，组装时零网络调用。
  复核用的下载动作由**人**执行（见「怎么复核」），不是系统能力。
- **P2.1 的联网采集是"取原文"，不是"做研究"**：没有查询改写、没有多轮迭代、没有结论生成，
  采集到的内容**不会**自动变成证据，也**不会**进入报告。这一层与报告链路的唯一关系是
  代码同仓、依赖单向（`providers → core`，有守卫）。把采集内容接入报告属于 P2.2+。
- **`var/snapshots/` 是运行时产物**，不是资料库：它**不进基线清单、不参与 fixture 签名**，
  可以随时删除重建（快照文件本身内容寻址，删了就重新采）。
  schema 是 `provider.snapshot.v1`，与 fixture 的 `research.fixture.v1` **刻意不同**。
- **联网测试默认不跑**：`tests/providers/test_real_smoke.py` 需要 `AGENTBOARD_REAL_SMOKE=1`
  才执行。默认 `pytest` 全程离线（Mock transport）。
- **凭据只从环境变量读**：`GITHUB_TOKEN` 是可选的速率提升手段，不落盘、不进仓库、不进快照。
  匿名模式是正常模式（官方文档确认公开只读端点无需认证），且不产生费用。
- **代理是环境属性，不是代码属性**：`HttpTransport` 不硬编码代理地址，
  从 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` 解析。必须走代理才能出网的机器上，
  若不配这些变量，直连会超时 —— 这是环境问题，不是代码问题，但 README 必须写清。
- **来源定位一律用 commit，不用分支**：分支会移动，用分支名当 `raw_url` 会让"行号 + sha256"
  这套锚点失效。这条有静态守卫（`find_unpinned_source_urls`）。
- `raw_lines` 的口径是"末行 = 该数值"（末尾换行不计为额外一行），与
  `sections[].line_range` 的 1 起编号一致；`line_range` 超过 `raw_lines` 会在加载期报错。
- 官方快照的摘录是**短引用**（≤ 400 字符，含上游原样 Markdown 标记），不是全文转录；
  各上游许可证见 `provenance.documents[].upstream_license`（pgvector 仓库 GitHub 未能识别，
  Qdrant 文档仓库未声明许可证，Milvus 文档仓库为 Apache-2.0）。
- **`unknown` 是结论，不是缺口**：`query_latency` / `scale_ceiling` / `ecosystem_maturity`
  三个维度在三个候选上都是 `unknown`，原因写在快照里。补齐它们需要**补充可复核的资料或明确判定口径**，
  而不是改一个标签；任何改动都必须显式更新回归锚点与 README 里的表。
- 名次受文档覆盖度影响（见上节"这一轮能确认什么"），且覆盖不对称；
  把它当"证据完整度排序"读，不要当"技术优劣排序"读。
- 文件级 digest 覆盖两个目录：`fixtures/research/INDEX.json`（4 个文件）与
  `fixtures/official/INDEX.json`（4 个文件）。
- `MAX_TASK_CARDS = 6`：3 个候选 × 1 研究卡 + 3 张组装卡刚好顶满，
  加第 4 个候选需要走 `validate_model_board(..., max_cards=)` 注入。
- `ruff check .` 仍有 45 条既有问题，全部落在 `scripts/`（红线，不得修改）与课程 ipynb 中；
  已记账不处理。P1A 的 lint 门禁固定为 `ruff check src tests tools`。
- `ruff format --check` 未纳入门禁（既有文件大量不符合其风格），P1A 不引入。

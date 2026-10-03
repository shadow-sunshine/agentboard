# P1A 方案（修订版 rev.2）

> 范围：技术选型研究领域模型 + 本地资料快照 + FixtureResearchWorker + 确定性报告组装。
> 前提：P0.5.1 已验收（78 passed / ruff 全绿 / `scripts/` 未改 / 未接入 LangGraph·ACP·真实模型·网络·FastAPI·memory）。
> 本轮只产出方案，不写业务代码。

## rev.2 修订摘要

| # | 问题 | 处置 |
|---|---|---|
| 1 | `snapshot_version` 正则与 `synthetic@1` / `legacy@0` 冲突 | **改正则一行不动**，改常量：`synthetic.1` / `legacy.0`。同时把 `structured_payload.schema` 的 `@` 也去掉（`research.findings.v1`），全项目标识符字符集收敛为 `[a-z0-9._-]` |
| 2 | `locator` 正则与 `mock:agent:task:index` 冲突 | `locator` **统一为受限 JSON Pointer**；合成证据为 `/synthetic/{task_id}/{index}`；删除行号形式 `L\d+` |
| 3 | numeric 打分未定义 | **P1A 禁用 `numeric`**，`scale_kind ∈ {ordinal, boolean}`；连带删除 `higher_is_better`（方向由 `utility_map` 单一承载）与 min/max 归一化表 |
| 4 | `domain_payload` 命名与核心层越界 | 改名 **`structured_payload`**；核心只校验 `schema` / `version`，AST 测试禁止核心读业务键 |
| 5 | 验收依赖 `git` / `grep` | 全部删除。改为 stdlib-only 的 `tools/verify_manifest.py`（Python / PowerShell 双环境可用） |
| 6 | 基线清单不实 | 按**实际文件**列出 19 条；不含 `demo.py`（不存在）；manifest 自身与 verifier 自身**不入 hash**；新增"清单路径必须真实存在"自检 |
| 7 | 阶段划分 | 按 P1A-0 基线工具 / P1A-1 EvidenceRef / P1A-2 最小研究模型+FixtureWorker / P1A-3 确定性报告 / P1A-4 评测文档 重排 |
| 8 | 校验职责不清 | 明确 **L0 dataclass 局部** / **L1 组装期上下文** / **L2 报告级完整性** 三层，并说明 `SelectionReport.__post_init__` 只做形态校验的原因 |

---

## 1. 标识符与正则总表（单一字符集）

一处定义，全项目复用。`@` 不出现在任何字段值里。

| 标识 | 正则 | 示例 | 备注 |
|---|---|---|---|
| `task_id` / `dimension_id` | `^[a-z][a-z0-9_]{1,40}$` | `research__pgvector`, `deployment_complexity` | 复用现有 `TASK_ID_PATTERN` |
| `candidate_id` | 同上 | `pgvector`, `milvus_standalone`, `qdrant_local` | |
| `snapshot_version` | `^[a-z0-9][a-z0-9._-]{2,63}$` | `pgvector-notes.2026-09-01`, `synthetic.1`, `legacy.0` | **与 P1A 原稿正则完全一致，未改** |
| `schema` | `^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*\.v[1-9]\d*$` | `research.findings.v1` | 去掉了原稿的 `@\d+` |
| `locator` | `^/(?:[A-Za-z0-9_.-]+)(?:/[A-Za-z0-9_.-]+)*$` | `/sections/0/text`, `/synthetic/research__pgvector/0` | 受限 JSON Pointer |
| `evidence_id` | `^ev_[a-z0-9]{16}$` | | |
| `finding_id` | `^rf_[a-z0-9]{16}$` | | |
| `risk_id` | `^rk_[a-z0-9]{16}$` | | |
| `rule_id` | `^R_[A-Z_]{3,40}$` | `R_UNSUPPORTED_WEIGHTED` | |

### 1.1 为什么改常量而不是改 `snapshot_version` 正则

正则 `^[a-z0-9][a-z0-9._-]{2,63}$` 保持原样，只把常量写成 `synthetic.1` / `legacy.0`：

1. **语义正确**：`snapshot_version` 是"这份快照的版本号"，点号是版本号的标准写法（`pgvector-notes.2026-09-01`、`synthetic.1`）。`@` 在主流生态里表示 scope（`pkg@version`），塞进版本号内部语义混乱。
2. **收紧优于放宽**：放宽正则（把 `@` 加进字符集）会永久降低该字段的约束力，却换不来表达力。项目既有风格是"边界收窄、拒绝静默"。
3. **单一字符集**：`snapshot_version` 只在正则里出现一次，但 `evidence_id` 的哈希输入包含它，且它还要参与 fixture 文件名/`INDEX.json` 版本字段。若允许 `@`，`@` 在 PowerShell 里是数组/哈希字面量前缀，在 README/脚本示例里需要额外转义。全项目收敛为 `[a-z0-9._-]` 后，所有标识字段共用一套字符集，**从根上消除第二处同类冲突**。
4. **连带修一处**：原稿 `structured_payload.schema` 用 `^[a-z][a-z0-9_.]*@\d+$`，同属 `@` 用法。一并改为 `research.findings.v1`。理由同 3 —— 若只修 `snapshot_version`，`@` 仍会留在 `schema` 里，等于把同一个坑留在隔壁字段。

### 1.2 为什么 `locator` 统一为受限 JSON Pointer

原稿 `locator` 允许两种形态（`^/…` 或 `^L\d+(-L\d+)?$`），而我给合成证据写的是 `mock:{task_id}:{index}` —— 两套都匹配不上。修正为**只用一种形态**：

| source_type | locator | 说明 |
|---|---|---|
| `fixture_snapshot` | `/sections/{i}/text` | 指向 fixture 文档真实节点，**必须可解析** |
| `synthetic_mock` | `/synthetic/{task_id}/{index}` | 稳定命名的**虚拟地址**，不要求可解析 |
| `legacy_text` | `/legacy/{task_id}/{index}` | 同上 |

- `locator` 是"证据在来源中的**逻辑地址**"，不是 URL。用受限 Pointer 比自定义 `a:b:c` 好：段与段之间是层级关系（`sections → 0 → text`），可读、可拼、可裁；`:` 分隔符则要靠位置记忆。
- 受限（不允许空 key、不允许 `~0`/`~1` 转义、不允许 `-` 作为独立段的 append 语义）是为了让正则与 RFC 6901 的一个**安全子集**等价 —— 校验简单、无歧义。
- 删除行号形式 `L\d+`：`legacy` 也改用 Pointer（`/legacy/{task_id}/{index}`），不再需要第二种语法。
- ⚠️ 由此产生一条新的上下文校验（见 §8 L1）：`source_type == "fixture_snapshot"` 时 locator 必须解析到 fixture 文档的真实节点，否则 `DanglingLocatorError`；`synthetic_mock` / `legacy_text` 只校验正则。

---

## 2. 目标目录结构

领域层不另立顶层包，放进 `src/agentboard/research/`，使 `pythonpath = ["src"]`、单包名 `agentboard` 不变。

```
Multi_Agent_Orchestration/
├── src/agentboard/
│   ├── evidence.py                    [新·核心] EvidenceRef + 内容寻址 id + digest 校验 + source_type 白名单
│   ├── contracts.py                   [改] WorkerResult.evidence 类型升级 + structured_payload
│   ├── workers/
│   │   ├── mock.py                    [改] 产出结构化 EvidenceRef
│   │   └── synthetic.py               [新] SyntheticEvidenceFactory（合成证据唯一入口）
│   └── research/                      [新·领域层]
│       ├── __init__.py
│       ├── config.py                  领域常量：schema 常量 / rule_id 闭集 / 阈值 / severity
│       ├── errors.py                  异常族（继承 AgentBoardResearchError）
│       ├── models.py                  ComparisonDimension / ResearchFinding / RiskItem /
│       │                              Recommendation / CoverageStats / IntegrityReport / SelectionReport
│       ├── dimensions.py              DimensionRegistry：加载 + alias 归并 + 权重归一化
│       ├── snapshot.py                fixture 快照加载与校验（纯本地文件）
│       ├── payload.py                 structured_payload 编解码（唯一读业务键的地方）
│       ├── fixtures.py                FixtureResearchWorker（双协议实现）
│       ├── rules.py                   规则式风险审查（纯函数）
│       ├── scoring.py                 确定性打分 + tie-break
│       ├── integrity.py               I1–I9 谓词（纯函数，可被 eval 复用）
│       ├── assemble.py                assemble_report：S1→S5
│       └── migrate.py                 legacy 字符串证据 → EvidenceRef 显式迁移器
├── fixtures/
│   ├── research/
│   │   ├── INDEX.json                 候选清单 + 快照版本 + 文件 digest
│   │   ├── dimensions/dimensions.v1.json
│   │   └── options/{pgvector,milvus_standalone,qdrant_local}.json
│   └── baselines/
│       ├── manifest.json              [基线] P1A 承诺不改的 19 个文件（不自哈希）
│       └── tree_p1a0.json             [报告用] P1A-0 时的全树快照，替代 git status
├── tools/
│   └── verify_manifest.py             [新] stdlib-only 清单校验器（不自哈希）
├── tests/
│   ├── research/                      10 个领域测试文件（§9）
│   ├── test_contracts.py              [改] 1 行
│   ├── test_p05.py                    [改] 1 行
│   ├── test_manifest_untouched.py     [新] 包装 verifier
│   └── test_static_guards.py          [新] AST 静态扫描（无网络/无时钟/核心不读 payload）
└── eval/research/
    ├── scenarios/*.json               8+ 固定场景
    ├── expectations/*.json            期望报告投影（baseline oracle）
    └── run_eval.py                    输出指标 JSON，exit code = 断言总结果
```

`pyproject.toml` 预计需改一处：为 `src/agentboard/research/*.py` 增加 `TRY004` per-file-ignore（领域层同样用 `ValueError` 表达非法输入）——**正因如此它不进基线清单**（§7）。

---

## 3. 数据模型

统一约定：`@dataclass(frozen=True)`；`to_dict()` 返回纯 JSON 可序列化结构（经 `thaw_payload`，`Mapping → dict`、`tuple → list`）；`from_dict()` 严格校验、不静默转换；未知键忽略，已知键类型错误一律 `ValueError`。

### 3.1 EvidenceRef（放**核心** `evidence.py`）

> 必须在核心：否则 `contracts.py` 要反向 import 领域层，破坏「`research → core`」单向依赖。

| 字段 | 类型 | 局部校验（L0） |
|---|---|---|
| `evidence_id` | `str` | `^ev_[a-z0-9]{16}$`；省略则按内容寻址计算；显式给出时必须等于重算值，否则 `ValueError`（防伪造） |
| `source_type` | `str` | ∈ `ALLOWED_SOURCE_TYPES = {fixture_snapshot, synthetic_mock, legacy_text}`；`web`/`llm` 显式不在集合 |
| `source_id` | `str` | 非空；前缀与 source_type 强绑定：`fixture:` / `mock:` / `legacy:` |
| `snapshot_version` | `str` | `^[a-z0-9][a-z0-9._-]{2,63}$` |
| `locator` | `str` | §1 受限 JSON Pointer 正则 |
| `excerpt` | `str` | 非空，≤ 400 字符；**原样切片，零规范化** |
| `digest` | `str` | `^[0-9a-f]{64}$`，必须 `== sha256(excerpt.encode("utf-8"))` |
| `captured_at` | `str` | `^\d{4}-\d{2}-\d{2}$`，**来自快照元数据，禁止 `now()`** |

```
evidence_id = "ev_" + sha256(f"{source_type}|{source_id}|{snapshot_version}|{locator}|{digest}")[:16]
```

哈希输入含 `source_type` ⟹ fixture 与 synthetic 的 id 天然不撞。`excerpt` 逐字节敏感 ⟹ 规则是"从原文切片、零规范化"，否则会出现同内容不同 id。

### 3.2 ComparisonDimension（**numeric 已禁用**）

| 字段 | 类型 | 局部校验（L0） |
|---|---|---|
| `dimension_id` | `str` | `^[a-z][a-z0-9_]{1,40}$` |
| `name` / `description` | `str` | 非空 |
| `weight` | `float` | `0 < w ≤ 1`，拒绝 bool；注册表层另断言 `Σ == 1.0`（L1） |
| `scale_kind` | `str` | ∈ **`{ordinal, boolean}`** —— `numeric` 显式不在集合，传即 `ValueError` |
| `scale_labels` | `tuple[str,...]` | `ordinal`：≥2 且唯一；`boolean`：必须恰好 `("false","true")` |
| `utility_map` | `Mapping[str,float]` | 键集**完全等于** `scale_labels`（不多不少）；值 ∈ `[0,1]`，拒绝 bool；**至少 2 个不同值**，否则 `DegenerateDimensionError`（该维度无区分度） |
| `aliases` | `tuple[str,...]` | lower_snake、去重、不含 `dimension_id` 自身 |

**相对原稿的三处删除**（都是 numeric 的连带设计）：

1. 删除 `scale_kind = "numeric"` 分支。
2. 删除 numeric 的 min/max 归一化表与"`value` 必须可 `float()` 解析"这条校验。
3. 删除 `higher_is_better`。**理由**：原稿给 ordinal 用位置隐式推方向，又给 numeric 用 `higher_is_better` 翻转 —— 两套方向语义并存，等于两个真源。现在方向**只**由 `utility_map` 承载，语义写死为「**utility 越大越符合选型目标**」，代码不必再解释方向。

打分公式（确定性，无归一化）：
```
score(candidate) = Σ_d  weight_d × utility_map_d[ value_d ]
unsupported 维度 utility 记 0.0（最差效用），并计入 coverage.missing
```

### 3.3 ResearchFinding

| 字段 | 类型 | 局部校验（L0） |
|---|---|---|
| `finding_id` | `str` | `^rf_[a-z0-9]{16}$` = sha256(`candidate_id\|dimension_id\|claim\|sorted(evidence_ids)`)[:16] |
| `candidate_id` / `task_id` / `agent_name` | `str` | 非空 + 正则 |
| `dimension_id` | `str` | `^[a-z][a-z0-9_]{1,40}$`（**是否属于注册表 → L1**） |
| `claim` | `str` | 非空 ≤ 400 |
| `value` | `str` | 非空（**是否为该维度合法 label → L1**，见 §8 说明） |
| `status` | `str` | ∈ `{supported, unsupported}` |
| `evidence_refs` | `tuple[EvidenceRef,...]` | `supported` ⟺ `len ≥ 1`；`unsupported` ⟹ `len == 0`（双向硬约束）；refs 内 `evidence_id` 无重复 |
| `confidence` | `float` | `[0,1]`，来自上游 `WorkerResult.confidence`，不由组装器臆测 |

### 3.4 RiskItem

`risk_id` `^rk_[a-z0-9]{16}$`｜`severity ∈ {low,medium,high}`｜`origin ∈ {worker_reported, rule_based}`｜`candidate_id` / `dimension_id`（`""` = 全局）｜`description` 非空｜`mitigation`｜`rule_id`：`rule_based` ⟹ 必须是 `RULE_IDS` 闭集成员且非空；`worker_reported` ⟹ 必须为 `""`｜`evidence_refs` 允许为空（规则风险由结构推出）。

### 3.5 Recommendation

| 字段 | 类型 | 局部校验（L0） |
|---|---|---|
| `candidate_id` | `str` | 正则（**是否属于候选集 → L2 / I6**） |
| `rank` | `int` | `≥1`，非 bool（**唯一性 → L2 / I6；连续性 → L2 / I6**） |
| `score` | `float` | 有限、非负 |
| `rationale` | `str` | 非空；**纯模板生成** |
| `supporting_finding_ids` | `tuple[str,...]` | 非空、无重复（**可解析性 → L2 / I7**） |
| `counter_finding_ids` / `risk_ids` | `tuple[str,...]` | 无重复 |
| `evidence_refs` | `tuple[EvidenceRef,...]` | 按 `evidence_id` 排序、无重复 |
| `confidence` | `float` | `[0,1]`；= (fixture 证据支撑的达标维度数 / 总维度数) × mean(supported 的 confidence)；**不是模型自评** |

### 3.6 SelectionReport 与 IntegrityReport

**SelectionReport** 字段：`schema_version`(`"p1a.1"`)｜`report_id`｜`brief`(`ProjectBrief`)｜`candidate_ids`｜`dimensions`｜`findings`｜`risks`｜`recommendations`｜`evidence_index`(`MappingProxyType`)｜`coverage`(`CoverageStats`)｜`integrity`(`IntegrityReport`)｜`generated_by`(`"deterministic-assembler.p1a.1"`，**点号而非 `@`，与 §1 字符集约定一致**)。
**故意不设 `generated_at`** —— 这是 `to_dict()` 能逐字节复现的前提。

**IntegrityReport 是纯记录载体，不做判定**：`checks: tuple[CheckResult,...]`，`CheckResult = (code, passed, detail)`；`passed` / `failed_codes` 是**派生 property**（不可赋值）。其 `__post_init__` 只校验 `code ∈ INTEGRITY_CODES` 闭集、code 无重复。

---

## 4. WorkerResult 迁移

| 方案 | 判定 |
|---|---|
| A. `evidence: str \| EvidenceRef` | ❌ 明令禁止；且下游处处要 `isinstance` 分支，"证据唯一入口"无法静态成立 |
| B. 保留 `tuple[str,...]` + 新增 `evidence_refs` | ❌ 双真源 |
| **C. `evidence` 就地升级为 `tuple[EvidenceRef,...]`，字段名不变** | ✅ 采纳 |

### 4.1 `structured_payload`（原 `domain_payload` 改名）

```python
evidence: tuple[EvidenceRef, ...]
structured_payload: Mapping[str, Any] = MappingProxyType({})
```

**核心层只校验两件事，不读任何业务键**：

1. 必须是 `Mapping`，经 `freeze_payload` 递归冻结为 `MappingProxyType`。
2. 必须含 `schema`：`str`，匹配 `^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*\.v[1-9]\d*$`。
3. `version` 键**若存在**，必须是 `int ≥ 1`（拒绝 bool）；这是独立于 schema 名的版本轴，缺省时不补。
4. 其余键**一律不解释、不校验、不读取**。

**命名选择**：`structured_payload` 而非 `payload`。理由：`util.py` 已有 `freeze_payload` / `thaw_payload`，"payload" 在本项目里指"任意可冻结结构"；`structured_payload` 明确表达"带 schema 版本号的结构化扩展槽"，与前者区分。

**边界用测试钉死**（`tests/test_static_guards.py`）：AST 扫描 `board.py` / `validation.py` / `isolation.py` / `events.py` / `planner.py` / `config.py` / `util.py`，禁止出现对 `structured_payload` 的字符串键下标（只允许 `contracts.py` 与 `research/**`）。领域层唯一读取入口是 `research/payload.py::decode_findings(payload)`，它先断言 `schema == "research.findings.v1"`，否则 `PayloadSchemaMismatchError`。

### 4.2 MockWorker 更新

```python
EvidenceRef(
    source_type="synthetic_mock",
    source_id=f"mock:{self.agent_name}",
    snapshot_version="synthetic.1",                       # 常量，点号风格
    locator=f"/synthetic/{task_input.task_id}/{i}",       # 受限 Pointer
    excerpt=f"[{self.agent_name}] {task_input.task_id} 合成证据 {i}",
    captured_at=FIXED_CAPTURED_AT,                        # "2026-01-01" 常量
)
```

> **`locator` 用 `task_id` 而非 `self._calls`** 是真实坑：`fail_times` 注入会让 `_calls` 偏移，同一任务两次运行产出不同 `evidence_id`，直接击穿"重复运行一致性"。专门写测试（§9 第 9 项）。
> `captured_at` 禁止 `date.today()`；`snapshot_version` 禁止 `datetime.now().isoformat()`。

### 4.3 旧数据兼容

**判定：需要"可读"，不需要"隐式兼容"。**

- 契约层零兼容代码：`from_dict` 遇字符串 evidence 直接 `ValueError`（有对应用例）。
- 兼容逻辑隔离在 `research/migrate.py::migrate_legacy_result(raw: dict) -> dict`：字符串证据升级为 `EvidenceRef(source_type="legacy_text", source_id=f"legacy:{task_id}", snapshot_version="legacy.0", locator=f"/legacy/{task_id}/{i}", captured_at=LEGACY_CAPTURED_AT)`。
- **迁移产物是有损标注**：`legacy_text` 不计入 coverage、不能使 finding 成为 `supported`（自动降级为 `unsupported` 且保留 `claim`）。旧数据不会冒充真证据。
- 唯一使用点：`eval/research/run_eval.py` 回放历史产物。

### 4.4 保证 `scripts/` 不受影响

| 手段 | 具体 |
|---|---|
| 路径隔离 | 新代码全在 `src/agentboard/**` + `tools/` + `tests/` + `fixtures/` + `eval/`；`scripts/` 零新增零修改 |
| **可执行证明** | `tools/verify_manifest.py --check`：清单内 19 个文件逐个 sha256 比对 + `scripts/` 下不得出现清单外新文件（§7） |
| 方向性 | `scripts/` 不 import `agentboard`，`agentboard` 不 import `scripts` |
| 静态前置 | P1A-0 生成 manifest 时即固化脚本现状，之后任何改动都会被 verifier 抓到 |

---

## 5. FixtureResearchWorker

### 5.1 fixture 文件格式（`fixtures/research/options/pgvector.json`）

```json
{
  "schema": "research.fixture.v1",
  "candidate_id": "pgvector",
  "display_name": "pgvector (PostgreSQL 扩展)",
  "snapshot_version": "pgvector-notes.2026-09-01",
  "captured_at": "2026-09-01",
  "source_path": "fixtures/research/options/pgvector.json",
  "sections": [
    {"section_id": "s_deploy", "heading": "部署与运维",
     "text": "以 Postgres 扩展方式安装，复用既有实例，无需独立集群……",
     "tags": ["deployment"]}
  ],
  "dimension_values": {
    "deployment_complexity": {"value": "low",  "section_id": "s_deploy",
                              "claim": "复用现有 PG 实例，无需独立集群"},
    "ops_skill_floor":       {"value": "low",  "section_id": "s_deploy",
                              "claim": "团队已有 PG 运维经验"}
  }
}
```

- **缺失的维度不写**（不是写 `null`）。注册表是全集 ⟹ "键缺失"就是"显式缺失"，Worker 据此生成 `unsupported` finding。
- `excerpt` = `sections[i].text` 前 400 字符常量截断（不做相关性抽取，保证确定性）。
- `locator` = `/sections/{i}/text`（受限 Pointer，可解析）。
- `source_path` 必须是相对路径且不得含 `://` ⟹ "无网络"变成静态断言。
- `value` 只能取该维度 `scale_labels` 的成员（ordinal label 或 `"false"`/`"true"`）。

### 5.2 source_type / snapshot_version / locator 三元组

| source_type | source_id | snapshot_version | locator | captured_at | 唯一生产者 |
|---|---|---|---|---|---|
| `fixture_snapshot` | `fixture:{candidate_id}` | fixture 的 `snapshot_version` | `/sections/{i}/text` | fixture 的 `captured_at` | `FixtureResearchWorker` |
| `synthetic_mock` | `mock:{agent_name}` | `"synthetic.1"`（常量） | `/synthetic/{task_id}/{index}` | `FIXED_CAPTURED_AT` | `SyntheticEvidenceFactory` |
| `legacy_text` | `legacy:{task_id}` | `"legacy.0"`（常量） | `/legacy/{task_id}/{index}` | `LEGACY_CAPTURED_AT` | `migrate.py` |

白名单常量放核心 `evidence.py`；P1A 集合**不含** `web` / `llm`，并断言两者被 `ValueError` 拒绝。

### 5.3 双协议、单数据源

```python
class FixtureResearchWorker:
    async def run(self, task_input) -> WorkerResult                      # 核心口（WorkerAdapter）
    async def investigate(self, task_input) -> tuple[ResearchFinding,...]  # 领域口
```

内部共用 `_build(candidate_id) -> (findings, worker_result)`；`WorkerResult.structured_payload = {"schema": "research.findings.v1", "version": 1, "findings": [...]}`。
**一条测试断言两个出口数据一致**（`decode_findings(run().structured_payload) == investigate()`），防止分叉。

- **`candidate_id` 必须显式传入，禁止启发式**：解析 `task_input.context` 中形如 `candidate_id: pgvector` 的独立行；解析不到 → `CandidateIdMissingError`，**不 fallback、不按 task_id 猜、不靠文本里出现 "pgvector" 就路由**。显式归属才能让缺参数在运行时立刻暴露。
- 未知候选（fixture 不存在）→ `UnknownCandidateError`，**不是返回空结果**。
- 输出确定性：按 `(candidate_id, dimension_id)` 排序。

### 5.4 如何区分 fixture 证据与 synthetic mock 证据

1. **唯一权威 = `EvidenceRef.source_type`**，不靠文案、不靠 agent 名字符串匹配。
2. `source_id` 前缀与 `source_type` 构造期强绑定。
3. `evidence_id` 哈希输入含 `source_type` ⟹ 两类 id 天然不撞（测试断言）。
4. `coverage.by_source_type` 分类型计数。
5. **业务级硬规则**：`Recommendation.supporting_finding_ids` 中至少 1 条 finding 含 `fixture_snapshot` 证据；**只有 synthetic 证据的 finding 不能进入 supporting**。让"fixture 是真资料、mock 只是占位"成为可执行断言。

---

## 6. 确定性报告组装

`assemble_report(brief: ProjectBrief, cards: Sequence[TaskCard], registry: DimensionRegistry, *, strict: bool = True) -> SelectionReport`

| 阶段 | 函数 | 行为 |
|---|---|---|
| **S1 研究候选方案** | `collect_candidates` | 取 `status==done` 且 `assigned_agent=="research"` 的卡 → `decode_findings` → 候选集合按字典序。0 个 → `CandidateMissingError`；< 2 个 → `InsufficientCandidatesError`；fixture 侧存在但卡缺失 → `CandidateCoverageError`；`task_id` 不属于本 board → `ForeignTaskError` |
| **S2 归一化比较维度** | `normalize_dimensions` | 加载 `dimensions.v1.json`（断言 `Σweight == 1.0`）→ `DimensionWeightError`；`alias → canonical` 归并；`dimension_id` 不在注册表 → `UnknownDimensionError`；`value` 不合法 label → `InvalidDimensionValueError`；同候选同维度多值冲突 → `DimensionConflictError`（**不取最后一个，也不取第一个**）；最后对**每个候选 × 每个注册维度**做笛卡尔补齐，缺失即合成 `unsupported` 占位 finding（`claim="未在资料快照中找到该维度的依据"`）⟹ **零静默缺失** |
| **S3 风险审查** | `review_risks` | 纯规则、零模型，输出 `tuple[RiskItem,...]`，见下表 |
| **S4 推荐结果** | `recommend` | `score = Σ weight × utility_map[value]`；unsupported 维度 utility 记 0.0 并计入 `coverage.missing`（**不用"平均掉缺失"美化**）；排序 `(-score, candidate_id)`，**tie-break 用字典序**；rank 连续 1..N；`rationale` 模板生成 |
| **S5 证据完整性校验** | `verify_integrity` | 跑 I1–I9 写入 `IntegrityReport`；`strict=True` 且存在失败 → `ReportIntegrityError(失败码列表)`，**不返回半成品**；`strict=False` 返回 `passed=False` 的报告。通过后再算 `report_id` |

### 6.1 S3 规则集（闭集 `RULE_IDS`）

| rule_id | 触发条件 | severity |
|---|---|---|
| `R_UNSUPPORTED_WEIGHTED` | 某候选在某维度 unsupported 且该维度 `weight ≥ 0.15` | high |
| `R_LEGACY_ONLY` | 某候选 supported 证据里 `legacy_text` 占 100% | high |
| `R_SINGLE_SOURCE` | 某候选所有 supported findings 证据只来自同一 `source_id` | medium |
| `R_SCORE_GAP_TIGHT` | top1 与 top2 score 差 `< 0.05` | medium |
| `R_WORKER_REPORTED` | 上游 `WorkerResult.risks` 非空 → 逐条转 RiskItem | low |

### 6.2 S5 完整性校验项

| 码 | 断言 |
|---|---|
| I1 | `evidence_id` 全域唯一（refs 的**重数** == 去重后数量） |
| I2 | 所有 EvidenceRef 引用均可在 `evidence_index` 解析（无悬空） |
| I3 | `supported` finding ⟹ ≥1 条非 legacy 证据 |
| I4 | 每个候选 ≥1 条 `supported` finding |
| I5 | 每个候选 × 每个注册维度均有 finding（supported 或 unsupported） |
| I6 | rank 恰为 `1..N`，**且**按 rank 排序后 `score` 单调不增 |
| I7 | Recommendation 的 finding / risk 引用全部可解析 |
| I8 | Σweight == 1.0（±1e-6） |
| I9 | 所有证据 `source_type ∈ ALLOWED_SOURCE_TYPES` |

---

## 7. 基线与验证工具（不依赖 git / grep）

### 7.1 事实前提

- **`D:\It\Test_Project` 全树不存在 `.git`** —— `git status` / `git diff` 根本不可用。
- 本项目**不存在 `demo.py`**（那是同级另一个课程工作区的文件）。清单只收录**实际存在**的路径。
- manifest 内**不含时间戳**，避免无意义 diff。

### 7.2 实际基线清单（19 条，均为已存在文件）

**Tier A · 硬红线（P1A 内绝对不改）**

| # | path |
|---|---|
| 1 | `scripts/01_plan_board.py` |
| 2 | `scripts/02_worker_acp_smoke.py` |
| 3 | `scripts/03_context_isolation.py` |
| 4 | `scripts/04_triggerflow_master_loop.py` |
| 5 | `scripts/05_hangzhou_team_building_run.py` |

**Tier B · 课程原始资产（P1A 内不改；后续阶段若需改，必须显式重签清单并记录理由）**

| # | path |
|---|---|
| 6–14 | `assets/01_master_worker_architecture.svg` … `assets/09_acp_local_codex_minimal.svg`（9 个） |
| 15 | `materials/product_goal.txt` |
| 16 | `主Agent调度与多Agent协作.md` |
| 17 | `主Agent调度与多Agent协作.ipynb` |
| 18 | `主Agent调度与多Agent协作.pdf` |
| 19 | `.env.example` |

**显式排除（并写明原因）**

| 排除项 | 原因 |
|---|---|
| `pyproject.toml` | P1A-1/P1A-2 预计要为 `research/**` 增加 `TRY004` per-file-ignore，属计划内改动 |
| `src/**`、`tests/**` | P1A 主要改动区 |
| `fixtures/**`、`eval/**`、`tools/**` | P1A 新增区 |
| `升级改造计划.md`、`src/agentboard/README.md` | P1A-4 会更新 |
| `fixtures/baselines/manifest.json` | **baseline 文件自身，不入 hash**（你的第 6 点要求） |
| `tools/verify_manifest.py` | **verifier 自身，不入 hash**（自哈希会导致每次改校验器都要重签） |
| `.workbuddy/**`、`__pycache__/**`、`*.pyc`、`.pytest_cache/**`、`.ruff_cache/**` | 运行时产物 |
| `demo.py` | **本项目不存在此文件**，故不在清单 |

### 7.3 manifest 格式

```json
{
  "manifest_schema": "agentboard.baseline.v1",
  "root": ".",
  "self_excluded": ["fixtures/baselines/manifest.json", "tools/verify_manifest.py"],
  "guarded_prefixes": ["scripts/"],
  "ignore_suffixes": [".pyc"],
  "ignore_dirs": ["__pycache__", ".pytest_cache", ".ruff_cache", ".workbuddy"],
  "entries": [
    {"path": "scripts/01_plan_board.py", "sha256": "…", "size": 1234, "tier": "A"},
    {"path": "assets/01_master_worker_architecture.svg", "sha256": "…", "size": 5678, "tier": "B"}
  ]
}
```

### 7.4 校验器 `tools/verify_manifest.py`

- **stdlib only**：`argparse` / `hashlib` / `json` / `pathlib` / `sys`。不 import 任何第三方，与 `dependencies = []` 一致。
- 不调用 `git` / `grep` / `find` / `diff`；只读写清单内路径与 manifest 自身。
- 两种模式：
  - `python tools/verify_manifest.py --write` 生成/重签清单（P1A-0 用一次；重签必须显式说明理由）
  - `python tools/verify_manifest.py --check`（默认）校验，exit `0` / `1`
- 检查项：

| 检查 | 失败含义 |
|---|---|
| C1 清单每个 `path` 必须**真实存在** | 防止把不存在的路径（如 `demo.py`）写进清单 |
| C2 每个文件 `sha256` 与 `size` 一致 | 文件被改动 |
| C3 `guarded_prefixes`（`scripts/`）下不得出现清单外文件 | 有人偷偷往红区加文件 |
| C4 `entries` 中不得包含 `self_excluded` 项 | manifest / verifier 自身不入 hash |
| C5 清单内 path 不重复 | 清单自洽 |

- 输出：`19 entries verified, 0 violations`（或逐条列出 violation）。
- pytest 包装 `tests/test_manifest_untouched.py`：用 `importlib.util.spec_from_file_location` 按路径加载 verifier（**不改 `pyproject.toml` 的 `pythonpath`**），断言 `violations == []`。

### 7.5 替代 `git status` 的改动清单（报告用，不作通过条件）

`fixtures/baselines/tree_p1a0.json`：P1A-0 时对**全树**（排除 `ignore_dirs` / `ignore_suffixes`）做一次只读快照，之后不再重签。
`python tools/verify_manifest.py --diff-tree` 输出相对 P1A-0 的 `added / modified / removed` 路径清单 —— 纯 Python 实现，PowerShell 与 bash 均可运行，**不依赖 git**。

---

## 8. 校验职责分层（你的第 8 点）

三层，职责互斥，不重复执行。

### L0 · dataclass 局部校验

**位置**：各模型 `__post_init__` / `from_dict`。**只看单对象自身，不访问任何外部资源**（注册表 / fixture 文件 / board）。
**失败**：`ValueError`（与既有 `contracts.py` 风格一致）。

| 模型 | 局部校验内容 |
|---|---|
| `EvidenceRef` | 类型、非空、8 个字段的正则、`source_id` 前缀与 `source_type` 绑定、`digest == sha256(excerpt)`、显式 `evidence_id` 必须等于重算值 |
| `ComparisonDimension` | 类型、非空、`dimension_id` 正则、`0<weight≤1`、`scale_kind ∈ {ordinal, boolean}`、`scale_labels` 形态、`utility_map` 键集 == `scale_labels` + 值 ∈[0,1] + ≥2 个不同值、aliases 正则/去重/不含自身 |
| `ResearchFinding` | 类型、非空、`finding_id`/`candidate_id`/`task_id`/`dimension_id` 正则、`claim ≤ 400`、`status` 闭集、**`supported ⟺ refs≥1` 与 `unsupported ⟹ refs==0`**、`confidence ∈[0,1]`、refs 内 `evidence_id` 无重复 |
| `RiskItem` | 正则、`severity`/`origin` 闭集、`rule_based ⟺ rule_id ∈ RULE_IDS` 双向绑定、`description` 非空 |
| `Recommendation` | 正则、`rank ≥1` 非 bool、`score` 有限非负、`supporting_finding_ids` 非空无重复、`evidence_refs` 已排序无重复、`confidence ∈[0,1]` |
| `IntegrityReport` | `code ∈ INTEGRITY_CODES` 闭集、code 无重复、`passed`/`failed_codes` 为派生 property |
| `SelectionReport` | **仅形态**，见下 |

**明确不放进 L0 的两项（关键）**：
- `ResearchFinding.dimension_id` 是否属于注册表 —— dataclass 拿不到注册表。**L0 只校验正则，归属校验放 S2（L1）。**
- `ResearchFinding.value` 是否是该维度的合法 label —— 同上。**L0 只校验非空，label 合法性放 S2（L1）。**
若硬要在 L0 做这两项，只能让 `ResearchFinding` 依赖维度对象，等于把领域注册表塞进数据类，破坏可序列化与可哈希。

`SelectionReport.__post_init__` 的形态校验（**全部只看报告自身**）：
- `candidate_ids` 无重复且字典序
- 各集合字段符合约定键序：`dimensions` 按 `dimension_id`、`findings` 按 `(candidate_id, dimension_id)`、`risks` 按 `(severity 降序, risk_id)`、`recommendations` 按 `rank`
- `recommendations` 覆盖的 candidate 集合 == `candidate_ids`
- `set(evidence_index) == set(所有 finding 引用到的 evidence_id)`（"无悬空索引项"，**不等价于 I1**）
- `report_id` == 重算值；`generated_by` == 约定常量

**为什么 `__post_init__` 不跑 I1–I9**：`strict=False` 时需要能构造出 `passed=False` 的报告。若完整性判定写在构造期，这种报告根本造不出来，降级行为无法测试、eval 也无法观察。

### L1 · 组装期上下文校验

**位置**：`assemble.py`（S1–S5）与 `snapshot.py` 加载期。**需要注册表 / fixture 文件 / board 才能判**。
**失败**：抛领域异常（全部继承 `AgentBoardResearchError`）。

| 校验 | 位置 | 异常 |
|---|---|---|
| 注册表 `Σweight == 1.0` | `dimensions.load` | `DimensionWeightError` |
| `dimension_id` 属于注册表（含 alias 归并后） | S2 | `UnknownDimensionError` |
| `value ∈ scale_labels` | S2 | `InvalidDimensionValueError` |
| 同候选同维度多值冲突 | S2 | `DimensionConflictError` |
| `task_id` 属于本 board 任务集合 | S1 | `ForeignTaskError` |
| `candidate_id` 属于候选集合 | S1 | `UnknownCandidateError` |
| **`source_type == fixture_snapshot` 时 `locator` 可解析到真实节点** | S1 / `snapshot.py` | `DanglingLocatorError` |
| fixture 的 `snapshot_version` / `captured_at` 与 `INDEX.json` 一致 | `snapshot.py` | `SnapshotMismatchError` |
| `source_path` 为相对路径且不含 `://` | `snapshot.py` | `UnsafeSourcePathError` |
| `structured_payload.schema == "research.findings.v1"` | S1 / `payload.py` | `PayloadSchemaMismatchError` |

### L2 · 报告级完整性（I1–I9）

**位置**：`research/integrity.py`，每项一个纯函数 `check_i1(report) -> CheckResult`，由 S5 汇总写入 `IntegrityReport`；同一批函数被 `eval/run_eval.py` 与单测直接复用（**单一实现，两处调用**）。

**分界原则一句话**：L0 判"这个对象是不是一个合法对象"，L1 判"引用的外部东西是否成立"，L2 判"这份报告作为整体是否自洽"。

| L0 | L1 | L2 |
|---|---|---|
| 类型 / 正则 / 范围 / 枚举 / 内部硬约束（如 `supported ⟺ refs≥1`） | 跨对象引用与外部资源（注册表 / fixture / board） | 跨候选聚合与业务判定（I1–I9） |

---

## 9. 测试计划

新增约 48 个用例、11 个文件 ⟹ 总计约 126。

| # | 覆盖点 | 文件 | 关键断言（层级） |
|---|---|---|---|
| 1 | evidence_id 唯一性 | `research/test_evidence.py` | 同 locator+同 excerpt → id 相同；excerpt 改 1 字符 → id 变；3 个 fixture 全量抽取后 `len(set(ids)) == len(ids)`（L0+L2/I1） |
| 1b | 伪造 id 拒绝 | 同上 | 显式传与内容不符的 `evidence_id` → `ValueError`（L0） |
| 1c | 前缀不撞 | 同上 | fixture 与 synthetic 同 locator → id 不同（L0） |
| 2 | 无效 evidence 引用 | `test_report_integrity.py` | finding 引用不在 index 的 ref → I2 失败 → `ReportIntegrityError`，错误码含 `"I2"`（L2） |
| 2b | digest 篡改 | `test_evidence.py` | 改 excerpt 不改 digest → `from_dict` `ValueError`（L0） |
| 3 | unsupported finding | `research/test_models.py` | `unsupported`+refs 非空 → `ValueError`；`supported`+refs 空 → `ValueError`（L0 双向） |
| 3b | unsupported 不被美化 | `test_assembly.py` | 该候选 score 按 utility 0.0 计；`rationale` 含"未找到依据"（L1） |
| 4 | evidence 缺失 | `test_assembly.py` | 删掉某候选某维度 `dimension_value` → 出现 `unsupported` 占位（**非静默省略**）；`weight ≥ 0.15` 时 `strict=True` → `ReportIntegrityError(I4/I5)`（L1+L2） |
| 4b | 全候选无证据 | 同上 | 全 unsupported → I4 失败；`strict=False` 返回 `passed=False` + 失败码清单（**验证 §8 的"构造期不跑 I1–I9"决策**） |
| 5 | FixtureResearchWorker | `research/test_fixture_worker.py` | ① 3 个 fixture 各 ≥6 findings ② 双出口一致 ③ 同输入两次 findings 完全相等 ④ 缺 `candidate_id:` 行 → `CandidateIdMissingError` ⑤ 未知候选 → `UnknownCandidateError` ⑥ 输出顺序 == `sorted()` ⑦ `source_path` 含 `://` → `UnsafeSourcePathError`（L0+L1） |
| 6 | 候选项目缺失 | `test_assembly.py` | 只有 1 张 research 卡 → `InsufficientCandidatesError`；卡缺失但 fixture 存在 → `CandidateCoverageError`；`task_id` 不属于本 board → `ForeignTaskError`（L1） |
| 7 | 比较维度权重 | `research/test_dimensions.py` | Σweight ≠1 → `DimensionWeightError`；alias 正确归并；`DimensionConflictError`；`UnknownDimensionError`；`InvalidDimensionValueError`；`utility_map` 缺键/多键/全等值 → `ValueError` / `DegenerateDimensionError`；`scale_kind="numeric"` → `ValueError`（**禁用验证**）；**把 `deployment_complexity` 权重从 0.10 提到 0.50，断言 top1 由 pgvector 变为 qdrant**（L0+L1） |
| 8 | 报告完整性 | `test_report_integrity.py` | 正常场景 I1–I9 全 True；**9 个故障场景各断言精确失败码集合**（I6 需两个子用例：rank 不连续 / score 非单调）（L2） |
| 9 | 重复运行一致性 | `test_determinism.py` | 同输入两次 → `json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), …)` 且 `a == b`；**注入 `MockWorker(fail_times=1)` 后结论仍一致**（钉死 locator 不用 `_calls`）；`report_id` 相同（L1） |
| 10 | 上下文隔离 | `research/test_research_isolation.py` | ① L0 卡 `upstream_results` 为空 ② `normalize_comparison` 只能看到 3 张上游，看不到 brief 原文 ③ 篡改 upstream 抛 `TypeError` ④ pgvector 的 finding 不含 milvus 的 `evidence_id`（L1） |
| 11 | 静态守卫 | `test_static_guards.py` | AST 扫 `src/agentboard/**`：无 `requests\|httpx\|urllib\|socket\|aiohttp` import；无 `time.time\|datetime.now\|date.today`（白名单 `FIXED_CAPTURED_AT`）；**核心模块不得出现 `structured_payload` 的字符串键下标**；fixtures 内无 `://`（静态） |
| 12 | 基线未改 | `test_manifest_untouched.py` | 包装 `verify_manifest`，断言 `violations == []`（含 C1 清单路径必须存在） |
| 13 | fixture 良构 | `research/test_fixtures_wellformed.py` | 无 `\r\n`、无行尾空白、UTF-8 无 BOM、每个 fixture 的 `snapshot_version` 与 `INDEX.json` 一致（L1） |

---

## 10. 验收标准（无 git / grep 依赖）

| # | 命令 | 通过条件 |
|---|---|---|
| 1 | `python -m pytest --collect-only -q` 后 `python -m pytest -q` | 约 `126 passed`（≥78+48），0 failed / 0 skipped |
| 2 | `python -m ruff check src tests tools` | exit 0（**范围必须限定，见 §10.1**） |
| 3 | `python -c "import agentboard, agentboard.research; print('ok')"`（仅装 dev extras） | 打印 `ok`，证明零第三方运行时依赖 |
| 4 | `python -c "import tomllib,sys;d=tomllib.load(open('pyproject.toml','rb'));sys.exit(0 if d['project']['dependencies']==[] else 1)"` | exit 0 |
| 5 | `python tools/verify_manifest.py --check` | exit 0，stdout 为 `19 entries verified, 0 violations` |
| 6 | `python eval/research/run_eval.py` | exit 0，stdout JSON：`scenarios_total >= 8`、`scenarios_passed == scenarios_total`、`determinism_repeat_rate == 1.0`、`evidence_resolvable_rate == 1.0`、`fixture_evidence_rate == 1.0`、`silent_missing_dimensions == 0`、`integrity_checks_executed >= 9`、`network_calls == 0`、`llm_calls == 0` |
| 7 | eval 内每场景跑两次，比对 `report.to_dict()` 的 JSON sha256 | 完全相同 |
| 8 | `WorkerResult.from_dict({"evidence": ["字符串"]})` | `pytest.raises(ValueError)`；AST 断言 `tests/` + `src/` 中除 `migrate.py` 及其测试外无字符串 evidence 字面量 |
| 9 | `python tools/verify_manifest.py --diff-tree` | 输出的 `added/modified/removed` 只包含计划内路径（P1A 新增区 + `pyproject.toml` + 两份 README/计划文档） |
| 10 | `python tools/verify_manifest.py --check` 中 C4 | 断言 manifest 与 verifier 自身不在 `entries`（**baseline 文件不自我哈希**） |

### 10.1 ruff 范围修正（P1A-0 实测后回填）

P1A-0 实测结果，修正原稿"`ruff check .` 全绿"这一**不可能达成**的验收项：

| 命令 | 实测结果 | 结论 |
|---|---|---|
| `ruff check src tests` | **All checks passed** | 这才是 P0.5.1 所称"ruff 全绿"的真实范围 |
| `ruff check src tests tools` | **All checks passed** | P1A 门禁 = 在原范围上加入新代码目录 `tools/` |
| `ruff check .` | **45 errors / exit 1** | 全部落在禁区与课程产物：`scripts/*.py` 24 条（TRY004×22 / F811×8 / B023×4 / PYI034×4 / UP037×3 / BLE001×2 / UP035×2）、`主Agent调度与多Agent协作.ipynb` 21 条。**`scripts/` 是红线不得修改**，故 `ruff check .` 在 P1A 内永远不可能绿 |
| `ruff format --check src tests tools` | **16 files would be reformatted, 9 already formatted / exit 1** | 格式检查**从未纳入过门禁**（既有 `src/agentboard/*.py`、`tests/*.py` 大量不合 ruff format 风格）。P1A **不引入**该门禁 |

**决策**：
1. P1A 的 lint 门禁固定为 `ruff check src tests tools`，与历史口径一致，仅扩展了新代码目录；
2. **不引入 `ruff format --check`** —— 它会把 16 个既有文件全部改写，其中 `scripts/` 是禁区，等于要求"先违规才能达标"；
3. `scripts/` 与课程 ipynb 的既有 lint 问题**记账但不修**，归入"遗留债务"而非 P1A 缺陷；
4. 若后续希望 `ruff check .` 变绿，唯一合法路径是**分阶段加 `pyproject.toml` 的 per-file-ignores 或 exclude**（改配置而非改红线文件），且应作为独立决策，不夹带在 P1A 里。

---

## 11. 分阶段执行（P1A-0 … P1A-4）

| 阶段 | 内容 | 验收门 |
|---|---|---|
| **P1A-0 基线和验证工具** | `fixtures/baselines/manifest.json`（19 条）+ `tree_p1a0.json` + `tools/verify_manifest.py` + `tests/test_manifest_untouched.py` + `tests/test_static_guards.py` | `verify_manifest --check` exit 0；`pytest` 78 → 103 passed；`ruff check src tests tools` exit 0；静态守卫全绿 |
| **P1A-1 EvidenceRef 迁移** | `src/agentboard/evidence.py` + `contracts.py`（`evidence` 类型升级 + `structured_payload`）+ `workers/synthetic.py` + `workers/mock.py` + `pyproject.toml` 加 `research/**` 的 `TRY004` ignore + 修 `test_contracts.py:15`、`test_p05.py:29` | 全绿；AST 确认无字符串 evidence 残留；`verify_manifest --check` 仍 exit 0 |
| **P1A-2 最小研究模型和 FixtureResearchWorker** | `research/{config,errors,models,dimensions,snapshot,payload,fixtures}.py` + `fixtures/research/**`（INDEX + dimensions.v1 + 3 个 option） | 全绿；双出口一致；fixture 良构测试绿；`scale_kind="numeric"` 被拒用例绿 |
| **P1A-3 确定性报告** | `research/{rules,scoring,integrity,assemble}.py` + L0/L1/L2 三层校验 + 全部领域单测 | 全绿；I1–I9 各有用例；双跑一致性绿 |
| **P1A-4 评测和文档** | `eval/research/{scenarios,expectations,run_eval.py}` + `research/migrate.py` + `src/agentboard/README.md` 补 P1A 段 + `升级改造计划.md` 追加进度 | `run_eval.py` exit 0；指标达标；`verify_manifest --diff-tree` 改动清单符合计划 |

每个阶段结束后停下，给出可执行验收清单（命令 + 预期输出 + 重点 review 点），确认后再进下一阶段。

---

## 12. 风险与取舍

### 12.1 为什么 P1A 不接真实 LLM

- P1A 最硬的验收是"重复运行一致性"。组装必须是 `输入 → 输出` 纯函数；一接模型，同输入两次可能不同，**确定性这道最重要的质量门直接失效**，评测集也失去回归能力。
- P1A 真正的价值是造 **baseline oracle**（fixture 场景的期望报告投影）。将来接 LLM 时才有可比对的东西。先接模型 = 先得到一个无法比对的系统。
- 归因清晰：出问题能二分到"资料 / 维度表 / 规则 / 打分"，而不是"模型今天不稳"。
- **代价（必须诚实）**：P1A 的"研究"不含真实研究，只是 fixture 回放。对外只能说"选型报告的**组装、证据归属、完整性校验**可验证，且给定同一资料快照输出可复现"，**不能**宣称"能自动做技术选型"。

### 12.2 为什么不接网络

- "本地资料快照"的设计前提就是离线可复现、可审计。接网络 = 快照内容会变，`snapshot_version` 无法锚定。
- `captured_at` 来自文件元数据而非 `now()`，就是为了证据 id 稳定；网络访问会引入时间与内容双重不确定。
- 静态可证：fixture 里出现 `://` 即测试失败。

### 12.3 为什么不接 ACP

- P1A 要验证的是"**隔离边界本身**是否正确 + 证据结构是否成立"，进程内 fixture worker 已能完整覆盖。
- 接 ACP 会引入第二个变量（跨进程协议可靠性）。报告一旦不对，无法二分是"组装逻辑"还是"协议传输"。
- ACP 属于 P1B/P2：确定性基线钉死后，用**同一个 `WorkerAdapter` 协议**把 `FixtureResearchWorker` 换成 `AcpWorker`，组装层零改动。P1A 用一条测试预演这件事：把 fixture worker 换成 `MockWorker`，组装器仍能跑完并产出合法报告。

### 12.4 为什么不接 LangGraph

- P1A 的 DAG 是**静态的、场景文件声明的**，没有动态拆任务 / 条件边 / 回环需求。用 6 张固定卡跑一个图运行时，是拿重框架换零收益。
- LangGraph 带来非确定性调度、异步状态、更慢的测试，直接对抗 `test_determinism.py` 与 `test_research_isolation.py`；且与 `dependencies = []` 冲突，必须塞 extras + 新测试路径。
- 边界：等 P1B 需要"LLM 提议 + 条件重试 + 状态回放"时再作为**编排运行时 Adapter** 引入，`board.py` 状态机保持框架无关（需求 10）。

### 12.5 为什么不接 FastAPI / memory

- FastAPI 是"暴露面"，memory 是"跨会话状态"，与"领域模型正确性"正交。
- **memory 尤其危险**：它让报告依赖"上次跑了什么"，直接摧毁重复运行一致性 —— P1A 最硬的验收。按改造计划本就是 P3 可选。

### 12.6 本轮方案自身风险

| 风险 | 缓解 |
|---|---|
| `WorkerResult.evidence` 破坏性变更 | 影响面已量化：`contracts.py` 1 处 + `mock.py` + 2 行测试（`test_contracts.py:15`、`test_p05.py:29`）。放 P1A-1 单阶段，阶段末 `pytest` 必绿 |
| `MAX_TASK_CARDS = 6` 被 DAG 顶满（3 研究卡 + 3 组装卡） | 3 候选刚好 6 张；加第 4 个候选必须走 `validate_model_board(..., max_cards=)` 注入。写进 README 已知边界 |
| fixture 手写易与真实文档不一致 | fixture 只声明"这是本快照里的内容"，不声明"这是事实"。报告口径是"**基于本快照**"，不是"基于事实" |
| `excerpt` 逐字节敏感 → 无意义 id 漂移 | excerpt 一律从 fixture 原文切片、零规范化；`test_fixtures_wellformed.py` 断言无 `\r\n`、无行尾空白、无 BOM |
| 维度权重是主观的 | 权重只存在于 `dimensions.v1.json`（**数据，不是代码**）；eval 专门有"改权重 → 断言 rank 变化"的场景，把权重的作用**可观测化**，而不是假装它客观 |
| manifest 重签被滥用（悄悄改红区） | `--write` 必须显式说明理由，且 Tier A 路径变更需在 commit message / 计划文档留痕；C3 保证不新增文件 |
| `numeric` 延期导致某些维度只能塞进 ordinal | 这是有意取舍：先让打分**完全无歧义**，等 ordinal/boolean 跑通并有了 oracle，再在 P1B 引入 numeric（届时需定义 min/max 与方向，并新增回归用例） |

---

## 附：任务依赖图

```
        L0 (并行·互不可见)          L1                 L2              L3
   ┌─────────────────────┐
   │ research__pgvector  │──┐
   ├─────────────────────┤  │
   │ research__milvus    │──┼──▶ normalize_comparison ──▶ review_risks ──┐
   ├─────────────────────┤  │            │                               ├──▶ synthesize_report
   │ research__qdrant    │──┘            └───────────────────────────────┘
   └─────────────────────┘
```

| task_id | assigned_agent | depends_on | 交付 |
|---|---|---|---|
| `research__pgvector` | research | `()` | findings(pgvector) |
| `research__milvus` | research | `()` | findings(milvus) |
| `research__qdrant` | research | `()` | findings(qdrant) |
| `normalize_comparison` | planner | 上 3 张 | 归一化矩阵 + 缺失清单 |
| `review_risks` | planner | `normalize_comparison` | risk items |
| `synthesize_report` | planner | `normalize_comparison`, `review_risks` | SelectionReport |

3 张 L0 卡互不依赖 ⟹ 互相看不到对方的 findings，正好复用 P0 的 `build_worker_input` 隔离不变量来断言。6 张卡与 `MAX_TASK_CARDS = 6` 恰好相等，现有 11 项护栏全部兼容，`config.py` 无需改动。

# REVIEW 预审单：P2.2-1 信任边界加固（只读复核，待裁决）

> **本单性质**：对**已存在**的 P2.2-1 实现做**只读**预审，不是"实施前的设计预审"。
> 本单未改动任何代码 / fixture / 工具；所有结论都配可复制的复核命令。
>
> **为什么会有这份单**：出题时以为 P2.2-1 尚未实施，实际它已在上一轮完成。
> 因此本单的用途是 —— **可当验收清单逐条复核**，或按 §③/§⑤ 的问题清单要求返工。

---

## 0. 状态纠正：P2.2-1 已实施完毕

```
21115  src/agentboard/research/attestation.py            （新增）
24557  tests/research/test_promotion_gate.py             （新增）
19393  tests/research/test_attestation.py                （新增）
 9376  tests/research/test_provider_boundary.py          （新增）
 8744  tests/research/test_source_kind.py                （新增）
17913  REVIEW_P2.2-1_trust_boundary.md                   （实施交接单）
```

上一轮门禁结果：`pytest 887 passed / 4 skipped` · `ruff` 干净 · `manifest 19/0` · 双签名通过 ·
`verify_sources 4 通过 0 缺失 0 问题` · `run_eval 10/10（network_calls=0, llm_calls=0）`。

本单**不重复**实施交接单的内容（改动清单 / 命令 / 反空转实验见 `REVIEW_P2.2-1_trust_boundary.md`），
只做**设计约束对照**与**规则 #32 冲突复核**这两件交接单没做的事。

---

## 1. 如何独立复核本预审

```bash
# (a) 确认截断规则的实现点数量（本单 §③-1 的核心证据）
python -c "import re,pathlib;[print(f'{p}:{i}: {l.strip()}') for p in sorted(pathlib.Path('src').rglob('*.py')) if '__pycache__' not in str(p) for i,l in enumerate(p.read_text(encoding='utf-8').splitlines(),1) if '[:MAX_EXCERPT_CHARS]' in l]"

# (b) 确认既有信任谓词的冻结面（改它就会改 report_id）
python -c "import re,pathlib;n=0
for p in sorted(pathlib.Path('src').rglob('*.py')):
    if '__pycache__' in str(p): continue
    t=p.read_text(encoding='utf-8'); c=len(re.findall(r'is_trusted_evidence\(',t))-t.count('def is_trusted_evidence(')
    if c: print(f'{p}: {c}'); n+=c
print('合计调用点 =',n)"

# (c) 确认 content_projection 不含 integrity（I10 将来不会改 report_id 的依据）
python -c "
import sys; sys.path.insert(0,'src')
from pathlib import Path
import inspect
from agentboard.research.models import SelectionReport
print(inspect.getsource(SelectionReport.content_projection))"

# (d) 确认原件目录是本机 artifact（门禁可移植性，见 §⑤）
python -c "from pathlib import Path; p=Path('.workbuddy/tmp/audit'); print('存在:',p.is_dir(),'| 文件数:',sum(1 for _ in p.rglob('*') if _.is_file())); print('.gitignore 存在:',Path('.gitignore').exists())"
```

---

## 2. ① 实际要改的文件清单

### 必需集（13 个）

| 类别 | 文件 | 在方案枚举内？ |
|---|---|---|
| 生产 | `research/config.py`（常量集） | 是 |
| 生产 | `research/errors.py`（`UnknownSourceKindError` / `PromotionError` / `AttestationError` / `AttestationSchemaError`） | 是 |
| 生产 | `research/snapshot.py`（`source_kind` + 加载期交叉强制 + `PromotionChecks` / `PromotableExcerpt` / `promote_excerpt` / `snapshot_excerpt`） | 是（`PromotionChecks` 位置有偏离，见 §③-6） |
| 生产 | `research/sources.py`（`is_promotable_source` / `promotion_checks_for` / `evidence_from_excerpt` / 集合级摘要） | 是 |
| 生产 | **`research/attestation.py`（新增）** | **否 —— 方案未指定 `SourceVerificationAttestation` 的归属文件** |
| 生产 | `tools/verify_sources.py`（**仅增** `--write-attestation` + `fixture_fingerprint` / `build_attestation_payload` / `attestation_bytes` / `write_attestation`） | 是 |
| 测试 | `research/conftest.py` | 否（方案未列，但自洽合成快照集必须有个家） |
| 测试 | `research/test_source_kind.py` | 是 |
| 测试 | `research/test_promotion_gate.py` | 是 |
| 测试 | `research/test_attestation.py` | 是 |
| 测试 | `research/test_provider_boundary.py` | 是 |
| 测试 | **`tests/test_static_guards.py`** | 方案列在"修改的守卫"行，非"新增文件"行 |
| 测试 | **`tests/test_verify_sources.py`** | **否 —— 方案没列，但 W1–W4 必须在工具层有测试** |

### 明确不必改（这就是 `report_id` 不变的机制）

`evidence.py` · `contracts.py` · `models.py` · `assemble.py` · `integrity.py` ·
`research/fixtures.py` · `research/migrate.py` · `research/rules.py` ·
`workers/synthetic.py` · `providers/**` · `eval/research/run_eval.py` · `pyproject.toml` · `fixtures/**`

核验方式：上述文件 mtime 全部早于本轮实施窗口；`evidence.py` 的哈希输入（字段集 / `compute_evidence_id` 材料串 / `ALLOWED_SOURCE_TYPES` / `TRUSTED_SOURCE_TYPES`）一字未动。

### 方案列了但我**没做**的一项

`attestations/` 空目录（方案「新增文件」行写"目录；本阶段可不落工件"）。
**未创建**，理由：空目录 git 不可追踪、对运行无影响；`write_attestation` 会 `mkdir(parents=True)` 自动补。
**若要求先落地（配 `.gitkeep` 占位），需要你确认。**

---

## 3. ② 每个设计约束 → 实现位置

| # | 约束 | 实现位置 | 验证测试 |
|---|---|---|---|
| 1 | `source_kind` 闭集，**缺字段不兜底** | `snapshot.py::_read_source_kind` + `FixtureSnapshot.__post_init__` | `test_source_kind.py`（14 条） |
| 2 | `source_kind` ↔ `provenance` 交叉强制 | `snapshot.py::_enforce_source_kind_provenance` | 同上（official 缺 → `MissingProvenanceError`；authored 带 → `SourceProvenanceSchemaError`） |
| 3 | `PromotionChecks`（C1–C5/C7/C8 逐项 + `failed()` 列全） | `snapshot.py::PromotionChecks` | `test_promotion_gate.py` 参数化 7 个反例 |
| 4 | `is_promotable_source` 九条充要条件 | `sources.py::is_promotable_source` + `attestation.py::promotion_blocking_reason` | `test_promotion_gate.py`（35 条） |
| 5 | `SourceVerificationAttestation`（字段闭集 / `verifier_version` / `index_sha256` / `snapshot_files[]` / `documents[]` / 集合完全一致） | `attestation.py`：`from_dict` / `fixture_fingerprint` / `_binding_reason` / `load_attestation` | `test_attestation.py`（41 条） |
| 6 | `--write-attestation`（opt-in） | `verify_sources.py::main` + `build_attestation_payload` + `attestation_bytes` | `test_verify_sources.py`、`test_attestation.py::test_tool_cli_*` |
| 7 | **W1 禁覆盖** | `write_attestation`：目标存在 → 走"幂等或拒绝"分支，**永不进入 `os.replace`** | `test_w1_w2_refuses_to_overwrite_different_content` |
| 8 | **W2** 同字节幂等 / 不同拒绝 | 同上（`existing == data` 分支） | `test_w2_identical_content_is_idempotent_without_writing`（断言 mtime 不变） |
| 9 | **W3** 原子写入（**第二次复核后**） | `NamedTemporaryFile(dir=target.parent)` + `fsync` + **`os.link` 原子排他发布**；**不支持时 fail-closed**（清理临时文件、返回失败、退出码 1）+ `finally` 清临时文件 | `test_w3_leaves_no_temp_file_behind`、`test_w3_link_unavailable_fails_closed_and_cleans_temp`、`test_write_path_has_no_non_atomic_fallback`、`test_tool_cli_exits_nonzero_when_link_unavailable` |
| 10 | **W4** `incomplete` 不得静默变 `passed` | `VerificationResult.overall_result`（有 problems 即**抛错**）+ 内容全由输入决定 + W2 | `test_w4_incomplete_cannot_become_passed`、`test_result_overall_result_refuses_to_hide_problems` |
| 11 | `PromotableExcerpt` / `evidence_from_excerpt` 受控 API | `snapshot.py::PromotableExcerpt` / `promote_excerpt`（要求 official + checks 全真）→ `sources.py::evidence_from_excerpt`（唯一公开工厂） | `test_provider_boundary.py`（11 条） |
| 12 | **L1** 依赖方向 | 既有 `test_core_and_research_do_not_import_providers` + `test_l1_dependency_direction_still_holds` | 保留 + 新增 |
| 13 | **L2** 受控 API（类型边界，非字段检查） | 入参类型 `PromotableExcerpt`；`promote_excerpt` 只接 `FixtureSnapshot` | `test_forged_provider_style_payload_cannot_become_evidence`（字段全对仍失败） |
| 14 | **L3** 构造点守卫 | `test_static_guards.py::EVIDENCE_REF_FACTORY_ALLOWED_FILES` + `find_scattered_evidence_ref_construction` + **白名单一致性检查**（防腐化） | 3 条（含真实文件注入对照） |
| 15 | **L4** Provider 反向守卫 | `find_evidence_references_in_providers`（AST；**docstring 提及不算违规**） | 2 条（含真实 provider 文件注入对照） |
| 16 | 不联网 / 不接模型 | 全链无网络库（既有静态守卫）；`evidence.py` 哈希链无 I/O | `run_eval` → `network_calls=0`、`llm_calls=0` |
| 17 | 不新增第三方依赖 | `pyproject.toml` 未改；新代码只用 `hashlib/json/re/tempfile/os/dataclasses` | `ruff` + 门禁 |
| 18 | **不改 `is_trusted_evidence` 语义** | 未编辑 `evidence.py`；全仓 **10 个调用点**（`assemble`×3、`models`×2、`rules`×2、`integrity`×1、`migrate`×1、`sources`×1）全部保持原语义 | `report_id` 逐条复现 |
| 19 | **不改 `content_projection()`** | `models.py::SelectionReport.content_projection` 未编辑；已确认投影**不含 integrity** ⇒ 将来加 I10 也不改 `report_id` | `run_eval` 10/10 |

---

## 4. ③ 可能违反规则 #32 的冲突点

规则 #32 的总判据：设计 bug 只有两类 ——
**① 约束之间互相冲突**（满足 A 必然违反 B）· **② 宣称的保护层级低于实际攻击面**。

### 4.1 已在设计阶段消除的（#32 生效的证据）

| #32 条目 | 原始冲突 | 消除方式 |
|---|---|---|
| A1 加字段 vs 零字节变化 | 把 `verification` 写进 fixture | C6 **外置**成 `attestations/` 工件 |
| B3 声明级 ≠ 绑定校验 | 只比 `raw_sha256` / `blob_sha` | 增 `index_sha256` + `snapshot_files[].sha256`（文件级锚点） |
| C6 动态时间字段 | `verified_at` | 全字段零时间；`verifier_version` 用整数 |
| C7 改既有谓词入哈希 | 想收紧 `TRUSTED_SOURCE_TYPES` | 改为**并列新增** `is_promotable_source`，旧谓词冻结 |
| D8/D9 strict 语义 | `research_grade` 抛专用异常会废掉 `strict=False` | 本阶段**不接入**（P2.2-3 改为 I10 接入既有完整性机制） |
| D10 过度声称 | "类型上不可表达" | 改为"受控公开 API + 静态守卫下**不可合法生成**" |
| A2/E 零变化承诺需专门门禁 | 只靠文字承诺 | `G-PRESERVE` G1–G4 + 双签名门禁（G4 见 §③-5） |

### 4.2 仍然存在 / 需裁决（6 条）

#### (1) #32-B5 违反：摘录截断规则有 **4** 份实现 ← **本单最重要的一条**

> ⚠️ **本单纠正了口头预审的错误**：口头版说 3 处，实际 **4 处**。
> 下面这份是重新 grep 出来的（命令见 §1-a），不是凭记忆。

| # | 位置 | 归属 |
|---|---|---|
| 1 | `research/fixtures.py:255` | 既有 P1A fixture worker |
| 2 | **`research/migrate.py:38`** | 既有 legacy 迁移器 ← **口头预审漏掉的一处** |
| 3 | `research/snapshot.py:638`（`snapshot_excerpt`） | P2.2-1 新增 |
| 4 | `research/sources.py:818-821`（`resolve_evidence` **内联**；`if` 在 818、切片在 821） | 既有来源层 |

另有两处是"上限检查"而非"截断实现"，不属本条：
`evidence.py:169`（`EvidenceRef` 拒绝超长摘录的守卫）、`workers/synthetic.py:50`（合成证据的长度检查）。

现状：四处**行为等价**（由 `test_excerpt_truncation_rule_matches_fixture_worker` 钉住），
但任何规则变更必须同步改四处。方案把收敛归到 P2.2-2（修 D2）。

**风险**：P2.2-2 若只收敛"worker + 升格"两处而漏了 `migrate.py` 与 `resolve_evidence` 的内联版，
就会留下"D2 已修"的假象 —— 而这正是 #32-B5「**凡'校验/规则'出现两处，必有一处成漏洞**」描述的情形。

**建议**：P2.2-2 的验收门禁加一条**硬断言**（可机械执行）。

> **注意：这条命令现在跑会打印 `4` 并断言失败 —— 这正是 P2.2-2 要收敛的目标，不是本单写错。**
> 本单已实测其当前输出为：
> `4 ['research/fixtures.py:255', 'research/migrate.py:38', 'research/snapshot.py:638', 'research/sources.py:821']`

```bash
# 只允许出现在 evidence.py（上限定义）与唯一实现处
python -c "import pathlib
hits=[f'{p}:{i}' for p in sorted(pathlib.Path('src').rglob('*.py')) if '__pycache__' not in str(p)
      for i,l in enumerate(p.read_text(encoding='utf-8').splitlines(),1) if '[:MAX_EXCERPT_CHARS]' in l]
print(len(hits), hits)
assert len(hits) == 1, hits"
```

#### (2) #32-B5 第二次出现：正则有两份 / 三份定义

| 正则 | 定义点 |
|---|---|
| `LINE_RANGE_PATTERN` | `research/config.py:49`（权威）· `tools/verify_sources.py:69`（工具自带） |
| 40 位十六进制 | `research/sources.py::COMMIT_SHA_PATTERN` · `attestation.py:85::BLOB_SHA_PATTERN` · `snapshot.py::SHA256_PATTERN`（64 位，另一形态） |

工具侧那份是**刻意解耦**（审计工具不得 import 被测库 —— 否则"库解析错了"会同时污染两端）。
当前只钉住了**常量值相同**（`test_tool_constants_match_library_constants`），
**没钉住"正则行为等价"** —— 即两份正则对同一组样本必须给出同一结论。

**建议**：补一条两侧同样本对照测试（同一批边界样本如 `"0"` / `"1"` / `"1-2"` / `"2-1"` / `"1-"` / `"abc"`，
断言两侧 `match` 结果逐一相同）。这是本阶段**可控但未做**的改进点。

#### (3) #32-B4 残余：`dimensions/` 不在 `snapshot_files[]` 里

证明**直接**覆盖的是「`INDEX.json` + 各候选快照文件」。
维表 `dimensions/dimensions.v1.json` 只被**间接**覆盖：

```
改维表 → INDEX.json 的 dimension_registry_sha256 变 → INDEX 字节变
       → attestation.index_sha256 失配 → 被 L-a 拦住
```

**结论**：防护有效，但依赖一个**隐含前提** —— "维表摘要必须留在 `INDEX.json` 里"。
`attestation.py` 的 docstring 只写了 L-a/L-b/L-c 三层，**没点明维表走的是 L-a**。
**建议**：在该 docstring 补一句显式说明，避免将来有人把 `dimension_registry_sha256` 挪出 INDEX
（那会让维表**静默脱离**证明覆盖范围，而所有测试仍绿）。

#### (4) #32-A 类：W1（禁覆盖）与 W3（原子写入）**本身冲突** —— ✅ 已修复（原判断有错）

> **本单原判断错误，2026-09-25 由复核纠正**：原文写"失败方向是**拒绝写入**，因此不会静默覆盖"。
> **这是错的。** 复核指出后我实测复现（脚本见下）：检查与 `os.replace` 之间存在**真实**窗口，
> 窗口内被创建的文件**会被静默覆盖，且函数返回 `True`** ——
> 即当时的实现与"禁止覆盖"**不等价**。这条已按 P1-2 修复，见 §9。

`os.replace` 是**无条件覆盖**语义，"禁止覆盖"不能靠"先检查"实现 —— 检查不是发布的一部分。

**原实现（已废弃）**：

```
存在性检查 → 幂等 或 拒绝 → 写 temp + fsync → 【再检一次】→ os.replace
                                                      ↑ 窗口在这里，覆盖就发生在这里
```

**现实现（`os.link` 排他发布）**：

```
存在性检查 → 幂等 或 拒绝 → 写 temp + fsync → os.link(temp, target)
                                              ↑ 目标已存在 ⇒ FileExistsError ⇒ 按"已存在"处置
                                                排他与原子由同一个系统调用提供，窗口消失
```

#### (5) `G-PRESERVE` 的 **G4 是空洞真**

方案 §6.1.1 定义 G4「移走 `attestations/` 后 G2/G3 结果不变」。
但本阶段**根本不创建** `attestations/`（约束 9），所以 G4 **没有实际保护对象** ——
它现在"成立"只是因为前提为空。**这是本阶段一个真实的未覆盖点。**

**两个选项**：
- (a) G4 后置到 P2.2-4（生成正式工件后才有意义）；
- (b) 现在补一条等价测试：在 `tmp` 里生成工件 → 移走 → 断言 `G2`（双签名）与 `G3`（eval 指标）结果不变。

> **复核裁决（2026-09-25）：deferred。** 采用 (a) ——
> 本阶段**不声称** G4 已验证；待 P2.2-4 生成正式工件后再执行。
> 已在 `P2.2_PLAN.md` §6.1.1 就地标注 `deferred` 并写明备选路径。
> 本阶段不实现 (b) 的测试（复核指令为"只修两个 P1"）。

#### (6) 两处方案偏离（已在上轮报告，此处登记以供裁决）

| 偏离 | 理由 |
|---|---|
| `attestation.py` 为方案未枚举的新模块 | C6 的"读取 / 自绑定 / 对齐"是三件独立且篇幅不小的事；塞进 `sources.py` 会让它接近 1200 行并混合"来源 schema"与"外部证明"两种职责。该模块只依赖 `snapshot.py`，无循环导入。 |
| `PromotionChecks` 落在 `snapshot.py`（方案写在 `sources.py`） | `promote_excerpt()` 按方案在 `snapshot.py`，而它必须校验 `checks`；若 `PromotionChecks` 留在 `sources.py` 会形成 `snapshot → sources` 的反向依赖。`sources.py` 通过 import 暴露它，对外可用路径不变。 |

#### (7) `promotion_checks_for` 与 `read_provenance` 的关系（#32-B5 的边界情形，供判断）

C1–C5/C7/C8 在 `read_provenance`（加载期）与 `promotion_checks_for` 里**各算了一遍**。

**这是刻意的"重新推导而不是采信"**，且与 (1)(2) 的重复**性质不同**：
`promotion_checks_for` 的输入是**已加载对象**（不是重新解析文件），
因此它不可能与 `read_provenance` 在"解析结果"上分歧，只可能在"策略"上分歧。
换句话说：重复的是**规则**，不是**解析**。

**代价仍需登记**：将来放宽某条加载期校验时，必须同时改 `promotion_checks_for`，
否则闸门会保持关闭（方向是**安全**的，但会造成"改了却没生效"的困惑）。
`promotion_checks_for` 的 docstring 已写明这一点。

---

## 5. ④ 负向控制方案（每条都能真的变红）

| 负向场景 | 构造方式 | 期望结果 | 固化位置 |
|---|---|---|---|
| **篡改 attestation 字段** | 生成后编辑 JSON 再 reload | `AttestationSchemaError`（15 个参数化坏例）+ 升格 `False` | `test_attestation.py` schema 参数化 |
| **手拼 attestation** | `from_dict` 直接构造（字段全对） | `False`，原因 `unbound`（**fail-closed**） | `test_attestation_from_dict_alone_is_never_promotable` |
| **篡改快照 1 字节**（不重签） | 改 `source_note` 加一字符 | `binding_reason=mismatch`，且 C7 独立报 `SnapshotDigestError` | `test_reason_mismatch_when_one_byte_of_fixture_changes` |
| **篡改快照 + 重签掩盖** | 改内容 + `resign_fixture` | **加载成功**（签名自洽）但 `index_sha256` 失配 → `mismatch` | `test_reason_mismatch_when_index_resigned_without_refreshing_attestation` |
| **错误 SHA（声明侧）** | 改 `documents[].raw_sha256` | `False` | `test_false_when_attestation_digest_conflicts_with_provenance` |
| **错误 SHA（文件侧）** | 改 `snapshot_files[].sha256` / 改文件字节 | `mismatch` | `test_reason_mismatch_when_one_byte_of_fixture_changes` |
| **集合不一致** | `snapshot_files[]` 少一条 / 多一条 | `mismatch`（不多不少） | `test_reason_mismatch_when_snapshot_file_set_differs[True/False]` |
| **incomplete → passed（改字段）** | 空 raw-dir 生成后手工改 `overall_result` | 解析通过但**未绑定** → fail-closed `False` | `test_w4_incomplete_cannot_become_passed` |
| **incomplete → passed（重写文件）** | 已有 `incomplete`，再以完整原件生成 | W2 内容不同 → **拒绝覆盖** | `test_w1_w2_refuses_to_overwrite_different_content` |
| **有 problems 却想写工件** | fake raw 文件 | 拒绝 + **不落盘** + `exit 1` | `test_tool_refuses_to_write_when_problems_exist`、`test_write_attestation_refused_when_problems_exist` |
| **Provider → EvidenceRef 越权（伪造）** | 字段齐全的 payload | `PromotionError`（缺的是**类型**，补字段无用） | `test_forged_provider_style_payload_cannot_become_evidence` |
| **Provider → EvidenceRef 越权（真实）** | 真 `RetrievedDocument` 实例 | `PromotionError` ×2 | `test_promote_excerpt_rejects_provider_record`、`test_forged_provider_record_cannot_become_evidence` |
| **人工摘要冒充官方** | `source_kind` 改 official | **加载期** `MissingProvenanceError` | §⑥ 对照实验 A（脚本）+ 单测 |
| **人工摘要带 provenance** | 给 research fixture 加 provenance 块 | `SourceProvenanceSchemaError` | `test_authored_summary_with_provenance_rejected` |
| **未知 / 缺 `source_kind`** | 近似值 / 删字段 | `UnknownSourceKindError`（**抛错，非 False**） | 2 条 |
| **静态守卫可证伪** | 真实源文件原文 + 注入一行 import / 构造 | 守卫变红；原文通过 | 2 条 real-file injection |
| **L4 防误报边界** | docstring 里提及 `EvidenceRef` | **不算违规** | `test_provider_evidence_boundary_guard_has_controls` |
| **W3 不可用时 fail-closed** | monkeypatch `os.link` 抛 `OSError`（含真实 `EXDEV`） | 返回失败 + 目标不存在 + 无临时残留；CLI 退出码 1 | `test_w3_link_unavailable_fails_closed_and_cleans_temp`、`test_p1_2_link_cross_device_is_fail_closed_not_degraded`、`test_tool_cli_exits_nonzero_when_link_unavailable` |
| **W3 防退化路径重现** | AST 扫描源码 | 不得出现 `O_CREAT`/`O_EXCL` 可执行调用（含正控制证明非空转） | `test_write_path_has_no_non_atomic_fallback` |
| **attestation 目标已存在（幂等/拒绝）** | 同路径写两次相同 / 不同字节 | 相同 → 幂等不写盘；不同 → 拒绝 | `test_w2_*`、`test_w1_w2_*` |

### "是否真能失败"的论证

每族都有**正负两侧**：正例必须被抓到、反例必须不误报。
（"会误报的守卫迟早被绕过"这条在 P1A-1 就被 `integrity.py` 的误报逼着重写过一次。）

四条对照实验**已实测**：

| 实验 | 操作 | 实测结果 |
|---|---|---|
| A 数据层可证伪 | `tmp` 副本上把 `fixtures/research` 的 `source_kind` 改成 official | **加载期直接拒绝** `MissingProvenanceError`（比方案预期更强） |
| B 篡改必失配 | 改 1 字节 + 不重签 | `binding_reason=mismatch`，且 C7 独立报错 |
| C 重签必失配 | 改内容 + 重签 | 加载成功但 `index_sha256` 失配 |
| D 静态守卫可证伪 | 真实文件原文注入一行 | 守卫变红；原文通过 |

**已知盲区（诚实登记）**：
- 本阶段的 attestation 相关测试全部依赖 `test_attestation.py::payload` 这个**真工具生成的基线**。
  若工具与库**同时**改错成一致的错误行为，两侧对拍测试会一起变绿。
  缓解：`test_generated_payload_has_exactly_the_closed_key_set` 断言的是**外部给定**的字段闭集
  （写在测试里，不来自实现），因此"两侧同时漂移"会被它拦住。

---

## 6. ⑤ 预审发现的门禁可移植性问题

你指定的门禁之一：

```bash
python tools/verify_sources.py --raw-dir .workbuddy/tmp/audit
```

**该门禁依赖本机 artifact**：

```
.workbuddy/tmp/audit/   4 份真实原件（README.md@7db2345e 等，共 ~57 KB）
.gitignore              不存在
.git                    不存在（本目录不是 git 仓库）
```

`.workbuddy/` 是本机工作目录，**不是交付物的一部分**。换机器跑这条门禁会得到
`0 份通过 / 4 份缺失` → **exit 1**，看起来像"实现坏了"，实际只是原件不在。

**建议**（二选一）：
- 换机/CI 时改用 `--allow-missing`，并额外断言 `problems == 0`（把"缺失"与"不符"分开看）；
- 或把"原件按设计不入库"写进门禁说明，避免复核者误判。

注意：这一条**不影响**本机门禁的有效性 —— 本机 `4 份通过 / 0 缺失 / 0 问题 / exit 0` 是真实结果。

---

## 7. 待你裁决（7 项）

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | 接受现状（P2.2-1 验收）／还是按 §4.2 返工？ | 见下逐条 |
| 2 | `attestations/` 空目录是否补（配 `.gitkeep`）？ | 倾向不补（空目录无意义）；若你要占位语义则补 |
| 3 | §4.2-(1) 四份截断实现的收敛 | 确认留给 P2.2-2，**并把"只允许 1 处"写进 P2.2-2 门禁**（命令已给） |
| 4 | §4.2-(2) 工具/库正则行为等价测试 | 建议**现在补**（约 20 行，纯对照测试，不影响任何门禁） |
| 5 | §4.2-(4) W1/W3 的 TOCTOU 窗口 | 建议接受"失败即拒绝"的取舍并在实现里写明；若要零竞态需换写入策略 |
| 6 | §4.2-(5) G4 空洞真 | 建议 (b) 现在补 tmp 版等价测试（比后置更能证明"缺工件不影响既有门禁"） |
| 7 | §4.2-(6) 两处方案偏离（`attestation.py` 新模块 / `PromotionChecks` 落点） | 建议接受；若要求落回方案枚举的文件，我改 |

---

## 8. 本单遵守的约束

| 约束 | 核验 |
|---|---|
| 只读预审、不改代码 | 本单是唯一写入（新建 MD）；`src/**` `tools/**` `tests/**` `fixtures/**` 未编辑 |
| 不修改 fixtures 内容 | fixtures mtime 早于本轮；双签名通过 |
| 不创建正式 attestations 工件 | `Path("attestations").exists() is False` |
| 不进入 P2.2-2/3/4 | 未触碰 `assemble.py` / `models.py` / locator 收敛 / `research_grade` |
| 不修改 report_id | `run_eval` 10/10 逐条复现 |
| 不写 memory / 不建 skill | 本回合未调用任何 memory / skill 写入 |

---

## 9. P1 修复记录（2026-09-25，复核后）

复核给出**暂不验收**与两个 P1。两项**都实测复现属实**，已修复并补负向测试。

### P1-1｜Attestation 可绕过加载绑定 ✅ 已修

**复现（修前，实测）**：不走 `load_attestation()`，直接用公开构造器传 `binding_reason=""`
→ `is_promotable_source()` 返回 **`True`**。根因是绑定结论是**公开字段**，
"必须由加载器产生"只是文档约定，不是机制。

**修复**：把绑定结论移出构造器签名。

```python
@dataclass(frozen=True)
class AttestationBinding:      # 绑定结论的载体（reason / detail）
    reason: str
    detail: str = ""

@dataclass(frozen=True)
class SourceVerificationAttestation:
    ...
    binding: AttestationBinding | None = field(
        default=None, init=False, repr=False, compare=False
    )   # ← 不在 __init__ 参数里：构造器无法设置
```

**为什么不做"拆成两个类型"**：复核给的第一条建议是"拆分已解析 / 已绑定"。
我评估后认为**拆分必要但不充分** —— `BoundAttestation(attestation=parsed, binding=AttestationBinding(""))`
与直接构造**同样容易**，漏洞只是往上挪了一层。真正起作用的是
**"该状态不可能经构造器传入"**（`init=False`），类型拆分只让语义更清晰，
因此本实现取 `init=False` 路线（复核给的第二条建议），并把 `binding_reason` / `binding_detail`
保留为**只读 property**（现有测试与调用方无需改动）。

**能力边界（与 `PromotableExcerpt` 同一标准，不再过度声称）**：
**受控公开 API + 静态守卫下不可合法生成**，而**不是**"类型上不可能"。
绕过需要蓄意写出 `object.__setattr__(att, "binding", AttestationBinding(""))`。
为此新增 **L5 静态守卫**：断言 `AttestationBinding(...)` 的构造与
`object.__setattr__(..., "binding"/"binding_reason"/"binding_detail")` 只允许出现在
`attestation.py`（含正负控制、真实文件注入对照、白名单一致性检查）。
—— 没有 L5，"静态守卫下不可合法生成"就是空话。

**新增负向测试（4 条）**：

| 测试 | 断言 |
|---|---|
| `test_p1_1_direct_construction_cannot_set_binding` | 传 `binding_reason=` / `binding=` → **`TypeError`**（这就是复核给的复现路径） |
| `test_p1_1_binding_is_not_in_the_constructor_signature` | `dataclasses.fields(...).init is False` + `"binding" not in signature` |
| `test_p1_1_parsed_attestation_is_never_promotable` | 三条路径：`from_dict` ❌、直接构造 ❌、`load_attestation` ✅ |
| `test_p1_1_promotion_blocking_reason_rejects_duck_typed_lookalike` | 鸭子类型对象 → `TypeError` |
| `test_no_out_of_band_attestation_binding_in_src` 等 4 条 | L5 守卫 + 控制 + 真实文件注入 + 白名单一致性 |

### P1-2｜W1 禁覆盖存在真实竞态 ✅ 已修

**复现（修前，实测）**：让"别的进程"在检查之后、`os.replace` 之前创建目标
→ 该文件**被覆盖**，且 `write_attestation` 返回 **`True`**。

**修复**：改用**排他发布原语**，不用 `os.replace`。

| 情形 | 修前 | 修后 |
|---|---|---|
| 目标不存在 | temp + fsync + **`os.replace`**（覆盖语义） | temp + fsync + **`os.link(temp, target)`**（原子 + 排他） |
| 发布时目标已出现 | **静默覆盖** ❌ | `FileExistsError` → 按"已存在"处置（幂等或拒绝）✅ |
| `os.link` 不可用 | — | ~~退化 `O_EXCL` 直写并告警~~ → **已删除，改为 fail-closed**（见 §P1-2 追加：W3） |

`os.link` 在本机实测可用且重复 link 抛 `FileExistsError`（正是需要的信号）。
根因不是"检查写得不够密"，而是**"检查"本身不是发布动作的一部分** ——
只有让发布动作自己带排他性，窗口才真正消失。

**新增负向测试（4 条）**：

| 测试 | 断言 |
|---|---|
| `test_p1_2_race_target_created_at_publish_time_is_not_overwritten` | 竞态内容**原样保留**、返回拒绝、无临时文件残留；并把 `os.replace` 换成炸弹证明它不再被调用 |
| `test_p1_2_race_with_identical_content_is_idempotent` | 抢先者写了相同内容 ⇒ 幂等成功，不改动它 |
| ~~`test_p1_2_publish_falls_back_to_exclusive_create`~~ | **已删除** —— 它把"实现自行降级成非原子写入"固化成了期望行为（`ok is True`） |
| `test_p1_2_link_cross_device_is_fail_closed_not_degraded` | 真实触发场景（`EXDEV`）⇒ **拒绝**，不留任何文件 |
| `test_write_path_never_uses_replace` | **AST 级**回归护栏：写入路径不得出现 `os.replace` 的可执行调用（文档字符串里的解释不算违规） |

### 追加：W3 原子性曾被降级 —— ✅ 已删除该退化路径（第二次复核）

**第二次复核意见（原文要点）**：`os.link` 遇到任意 `OSError` 后回退到 `O_EXCL` 直接写入并**返回成功**；
进程被强制终止时可能留下半写文件；测试也明确把这种非原子路径当成功。
**原子写入是已批准的硬约束，不能由实现者自行降级。**

**复现（修前，实测）**：monkeypatch `os.link` 抛 `OSError`
→ 返回 `ok=True`，且 `O_EXCL` 直写确实产生了目标文件。**该路径与主路径保证不等价。**

**修复**：删除 `_exclusive_create`（连带移除 `contextlib` 导入）。
`os.link` 抛任何 `OSError` ⇒ 返回 `(False, …)`，`finally` 清理临时文件；CLI 据此 `return 1`。
**理由**：`O_CREAT | O_EXCL` 只有**排他性**、**没有中断原子性** —— 强杀留下的半写文件
看起来像"已生成的工件"，后续校验只能报"内容不符"，无法与"被篡改"区分（正是 W3 要防的事）。
**原子性无法提供时，唯一诚实的动作是拒绝。**

| 测试 | 断言 |
|---|---|
| `test_w3_link_unavailable_fails_closed_and_cleans_temp` | 返回失败 + 目标不存在 + 无临时残留 + 失败说明点明"为何不用 O_EXCL 顶替" |
| `test_write_path_has_no_non_atomic_fallback` | **AST 级**护栏：源码不得出现 `O_CREAT`/`O_EXCL` 可执行调用，附正控制 |
| `test_tool_cli_exits_nonzero_when_link_unavailable` | **CLI 退出码 1**（把库层的 `False` 与进程退出码串起来） |

### 修复后的门禁（重跑既有全部 7 条）

| 门禁 | 结果 |
|---|---|
| `pytest` | **901 passed, 4 skipped**（修前 887 → v2 +12，v3 净增 2） |
| `ruff check src tests tools` | All checks passed! |
| `verify_manifest --check` | 19 entries verified, 0 violations |
| `sign_fixtures --check`（research） | 结构与文件级 digest 全部一致 |
| `sign_fixtures --check --root fixtures/official` | 结构与文件级 digest 全部一致 |
| `verify_sources --raw-dir .workbuddy/tmp/audit` | 4 份通过，0 份缺失，0 个问题 |
| `run_eval` | 10/10；`integrity_checks_executed=63`；`network_calls=0`；`llm_calls=0` |

**不变式复核**：官方 `report_id = rp_9e0057b9f989020f` 复现；P1A 10 场景逐条复现；
`fixtures/**` 本回合改动 **0** 个文件；`Path("attestations").exists() is False`；
`is_trusted_evidence` / `TRUSTED_SOURCE_TYPES` / `ALLOWED_SOURCE_TYPES` 未改。

**本回合改动范围（5 个文件）**：

```
src/agentboard/research/attestation.py    绑定状态移出构造器 + AttestationBinding + docstring
src/agentboard/research/sources.py        仅 docstring（说明 binding 不可经构造器传入）
tools/verify_sources.py                   os.replace → os.link 原子排他发布；
                                          **删除 O_EXCL 退化路径**（不可用即 fail-closed）
tests/research/test_attestation.py        P1-1 ×4、P1-2 ×4、W3 ×3（删 1 条旧"退化即成功"用例）
tests/test_static_guards.py               +4 条（L5 守卫、控制、真实文件注入、白名单一致性）
P2.2_PLAN.md                              §2.2.3 W3 行 / 失败行为表 / §6.6 / 修订记录第五版
```

外部文档：`P2.2_PLAN.md`（P2.2-2 加"截断实现点唯一"硬门禁；§6.1.1 G4 标 deferred）、
`REVIEW_P2.2-1_trust_boundary.md`（v2 修正）。

### 本单此前一条判断被复核证伪（如实记录）

§4.2-(4) 原写"失败方向是拒绝写入，因此不会静默覆盖"——**这句是错的**，
已就地更正。教训与本项目规则 #32 同源：
**"看起来很严密"的检查序列（检查→再检查→替换）不如一个带排他性的原语**；
以及**我用代码阅读代替了实测**，而实测三次就能看出覆盖真实发生。

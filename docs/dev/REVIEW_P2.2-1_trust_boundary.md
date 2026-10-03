# REVIEW 交接单：P2.2-1 信任边界加固（第三版，含 P1 + W3 修正）

> 交接单的用途：让复核者能**独立复跑**每一条声称，而不是基于作者自述审阅。
> 每一条"已完成"都配可复制的命令与期望输出；每条"未实现"都显式列出。
> 本单由实现者自跑验证过全部命令（结果见 §③ 与 §④）。

---

## ⓿ 第三版修正（2026-09-25，收尾 W3）

第二次复核：**P1-1 / P1-2 通过**，但指出 **W3 原子性被实现自行降级**（`os.link` 失败时回退 `O_EXCL` 直写**并返回成功**）。
已按"**移除非原子成功回退；不可用时清理临时文件、明确失败、返回非零**"收尾：

| 文件 | v3 sha256[:16] | 变化 |
|---|---|---|
| `tools/verify_sources.py` | `25357a005e022d2b` | **删除 `_exclusive_create` 退化路径**；`os.link` 抛任何 `OSError` ⇒ fail-closed |
| `tests/research/test_attestation.py` | `3bd08b4e4b923318` | +3（fail-closed 行为、AST 禁 `O_EXCL` 护栏、CLI 退出码非 0）；删 1 条"退化即成功"旧用例 |
| `P2.2_PLAN.md` | — | **§2.2.3 W3 行 + 失败行为表 + §6.6 + 修订记录第五版**：写明"temp + fsync + `os.link` 原子排他发布；不支持时 fail-closed" |

---

## ⓿′ 第二版修正（2026-09-25，按复核意见修两个 P1）

复核给出**暂不验收** + 两个 P1。两项**均已实测复现属实**，已修复。本版与第一版的差异：

| 文件 | v2 sha256[:16] | 变化 |
|---|---|---|
| `src/agentboard/research/attestation.py` | `57431be8b741dc5f` | **P1-1 修复**：绑定状态移出构造器；新增 `AttestationBinding` |
| `src/agentboard/research/sources.py` | `7e070c22a120e376` | 仅 docstring（说明 `binding` 不可经构造器传入） |
| `tools/verify_sources.py` | `25357a005e022d2b` | **P1-2 + W3 修复**：`os.replace` → `os.link` 原子排他发布；**删除非原子退化路径**（`os.link` 不可用即 fail-closed） |
| `tests/research/test_attestation.py` | `3bd08b4e4b923318` | P1-1 ×4、P1-2 ×4、**W3 ×3**（fail-closed、AST 禁用 `O_EXCL` 护栏、CLI 退出码非 0） |
| `tests/test_static_guards.py` | `71631b2efb088607` | +4 条（L5 守卫、控制、真实文件注入、白名单一致性） |
| `src/agentboard/research/config.py` | `4fa1b03b128d635b` | 未变 |
| `src/agentboard/research/errors.py` | `c8e4aa929fefb130` | 未变 |
| `src/agentboard/research/snapshot.py` | `720741e67d71242d` | 未变 |
| `tests/research/conftest.py` | `8345220bf41d2328` | 未变 |
| `tests/research/test_source_kind.py` | `4f180222592da0f7` | 未变 |
| `tests/research/test_promotion_gate.py` | `258b876149fbba6d` | 未变 |
| `tests/research/test_provider_boundary.py` | `6e96893f1ade08f0` | 未变 |
| `tests/test_verify_sources.py` | `c1e163a1a11f0391` | 未变 |

**门禁（v2 重跑既有全部 7 条，全绿）**：

| 门禁 | v1 | **v2** |
|---|---|---|
| `pytest` | 887 passed / 4 skipped | **901 passed / 4 skipped**（v2 为 899，v3 净增 2） |
| `ruff check src tests tools` | All checks passed! | All checks passed! |
| `verify_manifest --check` | 19 / 0 | 19 / 0 |
| 双签名（research + official） | 通过 | 通过 |
| `verify_sources --raw-dir .workbuddy/tmp/audit` | 4 通过 / 0 缺失 / 0 问题 | 同 |
| `run_eval` | 10/10，`network_calls=0`，`llm_calls=0` | 同（`integrity_checks_executed=63`） |

**不变式**：官方 `report_id = rp_9e0057b9f989020f` 复现；P1A 10 场景逐条复现；
`fixtures/**` v2 改动 **0** 个文件；`attestations/` 未创建；既有信任谓词未改。

### P1-1：绑定状态曾被公开构造器接受 ✅ 修

`SourceVerificationAttestation(..., binding_reason="")` 曾可直接构造并被
`is_promotable_source()` 判为可升格（实测返回 `True`）。**根因**：绑定结论是**公开字段**，
"必须由加载器产生"只是文档约定，不是机制。

**修法**：`binding` 改为 `field(default=None, init=False, repr=False, compare=False)`
—— **不在构造器参数列表里**，手拼对象恒为 `None`（`fail-closed`）。
`binding_reason` / `binding_detail` 降级为只读 property，调用方与既有测试无需改动。

**关于复核给的两条建议**：第一条"拆成已解析 / 已绑定两个类型"我评估为**必要但不充分** ——
`BoundAttestation(attestation=parsed, binding=AttestationBinding(""))` 与直接构造**同样容易**，
漏洞只会上移一层。因此采用第二条（"仅加载器能建立的内部状态"）。
若复核认为类型拆分仍应做（为了 API 语义清晰），请指出，我补。

**能力边界不再过度声称**：**受控公开 API + 静态守卫下不可合法生成**，
**不是**"类型上不可能"。并新增 **L5 静态守卫**把这句话兜住
（`AttestationBinding(...)` 与 `object.__setattr__(..., "binding")` 只允许出现在 `attestation.py`）。

### P1-2：W1 禁覆盖曾有真实竞态 ✅ 修

旧实现"检查目标不存在 → `os.replace`"在两者之间存在**真实**窗口，窗口内出现的文件
**被静默覆盖且函数返回 `True`**（实测复现）。**根因**：检查不是发布动作的一部分。

**修法**：发布改用 `os.link(temp, target)` —— **原子且排他**（目标已存在即
`FileExistsError`），于是"有没有别人先到"由**发布动作本身**判定。
另加 AST 级回归护栏：写入路径不得出现 `os.replace` 的可执行调用
（文档字符串里解释"为什么不用它"不算违规）。

> ⚠️ **本单第一版 §⑤ 风险 3 的判断已被证伪**，原文写"失败方向是拒绝写入，因此不会静默覆盖"——
> **这是错的**。已就地更新（见 §⑤）。教训：用代码阅读代替实测，会漏掉"看起来严密但不等价"的实现。

### P1-2 追加：W3 原子性曾被降级 ✅ 修（第二次复核）

P1-2 第一版把"`os.link` 抛非 `FileExistsError` 的 `OSError`"处理成**退化到 `O_EXCL` 直写并返回成功**。
这等于**实现自行把已批准的硬约束降级**，而当时那条测试还把这个降级固化成了期望行为（`ok is True`）。

**修法**：**删掉退化路径**。`os.link` 抛任何 `OSError` ⇒ 清理临时文件、明确失败（调用方返回退出码 1）。
理由：`O_CREAT | O_EXCL` 只有排他性、**没有中断原子性**，强杀留下的半写文件
看起来像"已生成的工件"，后续校验只能报"内容不符"，无法与"被篡改"区分 —— 这正是 W3 要防的事。
**原子性无法提供时，唯一诚实的动作是拒绝。**

配套：新增 AST 护栏禁止 `O_CREAT`/`O_EXCL` 在源码中重现（防退化路径被重新引入）。

> **G4 状态**：复核裁决为 **deferred** —— 本阶段不创建 `attestations/`，
> G4 无保护对象（空洞真），因此**不声称已验证**。已在 `P2.2_PLAN.md` §6.1.1 就地标注。

---

## ① 任务边界

**原始要求**：按当前 `P2.2_PLAN.md` 实施 P2.2-1，完成后停止，不进入 P2.2-2。

**本阶段目标（10 条）**：

1. 让 `source_kind` 在加载期生效（`authored_summary` / `official_documentation_excerpt` / provenance 交叉校验）
2. 实现 `is_promotable_source()`、`PromotionChecks`
3. 实现 `SourceVerificationAttestation`（schema/version、`index_sha256`、`snapshot_files[]`、`documents[]`、文件集合完全一致）
4. 给 `tools/verify_sources.py` 增加 `--write-attestation`（默认不覆盖 / 内容相同幂等 / 不同拒绝 / 原子写入 / `incomplete` 不得静默变 `passed`）
5. 建立 `PromotableExcerpt` + `evidence_from_excerpt()` 受控 API
6. 增加 L1–L4 静态守卫与负向测试
7. **不**实现 P2.2-2 的 locator 收敛
8. **不**实现 P2.2-3 的 `assemble_report` `research_grade` 接入
9. **不**创建正式 `attestations/official.sources.v1.json`；attestation 测试全部用 `tmp_path`
10. **不**修改任何 fixtures 字节，**不**执行 `sign_fixtures.py --write`

**额外硬性约束（用户明确列出）**：

- W1"禁止覆盖"必须考虑目标已存在，不得用无条件 `os.replace` 覆盖 `attestation`
- 若 `live_reverify` 不实现，必须在报告中明确标注为**未实现**，不得把设计当成已交付
- 既有 `is_trusted_evidence()`、`TRUSTED_SOURCE_TYPES`、`ALLOWED_SOURCE_TYPES` **不得改变**
- P1A 10 个场景与官方既有 `report_id` 必须保持不变
- 不修改 `scripts/`、`assets/`、`materials/`、长期 memory、个人 skill
- 不联网、不运行真实 smoke

---

## ② 改动清单（13 个文件 · 全部绝对路径 + sha256）

### 生产代码（6 个）

| 文件 | 改动 | sha256 |
|---|---|---|
| `src/agentboard/research/attestation.py` | **新增**（方案未枚举的新模块，理由见 §④-注） | `57431be8b741dc5fba1e41d3f7bf0ff33202e2b8b68c9339ae61dfa44a6adaae` |
| `src/agentboard/research/config.py` | 新增 `VALID_SOURCE_KINDS` / `SOURCE_KIND_*` / `POLICY_*` / `ATTESTATION_SCHEMA` / `VERIFIER_ID` / `EXPECTED_VERIFIER_VERSION` / `VERIFIER_ALGORITHM` / `RESULT_*` / `REASON_*` / `SNAPSHOT_SET_PATTERN` / `LINE_RANGE_PATTERN` | `4fa1b03b128d635b80e146e56a3c71cb97035b398a3e11c1fd8937d7a2ac01ca` |
| `src/agentboard/research/errors.py` | 新增 `UnknownSourceKindError` / `PromotionError` / `AttestationError` / `AttestationSchemaError` | `c8e4aa929fefb13088e947fe1a331a58e49b6b3c643569d0a7b594711c4ea933` |
| `src/agentboard/research/snapshot.py` | `FixtureSnapshot.source_kind` + 加载期交叉强制；`FixtureCatalog.snapshot_set`；`PromotionChecks` / `PromotableExcerpt` / `promote_excerpt()` / `snapshot_excerpt()` | `720741e67d71242d27c417014a16c99b7ec7ac42a096651f7611a48c78bc9a8f` |
| `src/agentboard/research/sources.py` | `declared_document_digests` / `snapshot_set_document_digests` / `promotion_checks_for` / `is_promotable_source` / `evidence_from_excerpt` | `7e070c22a120e3763d83a641c386dde6f86779d2e49de8757a7be2bead1a27ca` |
| `tools/verify_sources.py` | **仅增** `--write-attestation` + `build_attestation_payload` / `attestation_bytes` / `write_attestation` / `fixture_fingerprint` | `18749bf224a8fb2495c2356a57d45c0d6fb44942bcee94edf851cd07758234da` |

### 测试（7 个）

| 文件 | 改动 | 测试数 | sha256 |
|---|---|---|---|
| `tests/research/conftest.py` | 新增自洽合成官方快照集（2 候选）+ 原件目录 + 真工具生成证明的 fixture | — | `8345220bf41d232898b897012167b6b59492b85ebe1c6746ea2e7d66a3073c2c` |
| `tests/research/test_source_kind.py` | **新增** | 14 | `4f180222592da0f769765f20c13ec2b78dc18bd0c2486bdc8dd71700af083b70` |
| `tests/research/test_promotion_gate.py` | **新增** | 35 | `258b876149fbba6d9a37c12bad20e5aef0a89dbca9218193492cfdba5a62ef27` |
| `tests/research/test_attestation.py` | **新增** | 49 | `7d01a4bc07257aef7c4e85d205ca99bf1f17f571966346072a454883429afc1f` |
| `tests/research/test_provider_boundary.py` | **新增** | 11 | `6e96893f1ade08f0c33932d0d5b9be87e57abdbee82b7c472633360e36fbfbd9` |
| `tests/test_static_guards.py` | 新增 L3 构造点守卫 + L4 反向守卫 + **L5 attestation 绑定守卫（v2）** + 正负控制 + 真实文件注入对照 | +11 | `71631b2efb088607` |
| `tests/test_verify_sources.py` | 新增 3 条（`to_dict` 形状不变 / 无 `--write-attestation` 不落盘 / 有问题时拒绝写） | +3 | `c1e163a1a11f0391afa761255812bb1f2b6855d5a0ff29e5921bb580438943af` |

**未改动（mtime 早于本回合，已逐一核对）**：
`src/agentboard/evidence.py` · `src/agentboard/contracts.py` · `src/agentboard/research/models.py` ·
`src/agentboard/research/assemble.py` · `src/agentboard/research/fixtures.py` ·
`src/agentboard/research/migrate.py` · `src/agentboard/workers/synthetic.py` ·
`src/agentboard/providers/**` · `eval/research/run_eval.py` · `tools/sign_fixtures.py` ·
`tools/verify_manifest.py` · `pyproject.toml` · `fixtures/**` · `scripts/**` · `assets/**` · `materials/**`

**未创建**：`attestations/`（已核验 `Path("attestations").exists() is False`）

---

## ③ 声明的验证（每条都带可复制命令）

### Q1 `source_kind` 在加载期生效

```bash
python -m pytest tests/research/test_source_kind.py -q
```

期望 `14 passed`。核心断言：`fixtures/research` 三个候选全部 `authored_summary` 且**无** provenance；
`fixtures/official` 三个候选全部 `official_documentation_excerpt` 且**有** provenance；
未知/缺失 `source_kind` → `UnknownSourceKindError`；官方缺 provenance → `MissingProvenanceError`；
人工摘要带 provenance → `SourceProvenanceSchemaError`。

### Q2 `is_promotable_source()` / `PromotionChecks`

```bash
python -m pytest tests/research/test_promotion_gate.py -q
```

期望 `35 passed`。真值路径用 `tmp` 下的**自洽合成快照集**（2 候选）+ **真工具生成的证明**，
不用手拼 JSON。

### Q3 `SourceVerificationAttestation`

```bash
python -m pytest tests/research/test_attestation.py -q
```

期望 `41 passed`。含 schema 闭集、序列化确定性、**工具与库逐字节相同**、零时间字段。

### Q4 `--write-attestation` 与 W1–W4

```bash
# 端到端：用真实原件目录生成到临时路径（**不会**写入项目根的 attestations/）
python - <<'PY'
import subprocess, sys, tempfile, json, os
from pathlib import Path
target = Path(tempfile.mkdtemp()) / "official.sources.v1.json"
argv = [sys.executable, "tools/verify_sources.py", "--raw-dir", ".workbuddy/tmp/audit",
        "--write-attestation", str(target)]
for i in (1, 2):
    r = subprocess.run(argv, capture_output=True, text=True)
    print(f"run{i}: exit={r.returncode} | {r.stderr.strip().splitlines()[-1]}")
print("overall_result =", json.loads(target.read_text(encoding="utf-8"))["overall_result"])
print("attestations/ 被创建:", Path("attestations").exists())
PY
```

期望：run1 `exit=0` + `已原子排他创建（os.link 发布）`；run2 `exit=0` + `幂等成功（未写盘）`；
`overall_result = passed`；`attestations/ 被创建: False`。

W1/W2/W3/W4 的单元断言在 `tests/research/test_attestation.py`（`test_w1_*` 到 `test_w4_*`），
含：
- **W3 失败路径（fail-closed）**：让 `os.link` 抛 `OSError` → 断言返回**失败**、
  目标**不存在**、临时文件**被清理**；CLI 层另有一条断言**退出码非 0**；
- **W3 回归护栏**：AST 级断言源码里不得出现 `O_CREAT`/`O_EXCL` 的可执行调用
  （防止"非原子退化路径"被重新引入），附正控制证明护栏非空转；
- **W1 竞态负向测试**：模拟目标在"最后检查之后、发布之前"出现 →
  断言**不覆盖**（竞态内容原样保留）+ 返回拒绝 + 无临时文件残留，
  并把 `os.replace` 换成炸弹证明它不再被调用；
- **回归护栏**：AST 级断言写入路径不得出现 `os.replace` 的可执行调用。

### Q5 `PromotableExcerpt` + `evidence_from_excerpt()`

```bash
python -m pytest tests/research/test_provider_boundary.py tests/research/test_promotion_gate.py -q -k "promote or excerpt or evidence_from"
```

期望全绿。含"字段完全一致的伪造 payload 仍变不成 `EvidenceRef`"与"真实 `RetrievedDocument`
也不能作为入参"两条负向断言。

### Q6 L1–L5 静态守卫

```bash
python -m pytest tests/test_static_guards.py -q
```

期望 `36 passed`。新增：

- **L3** `find_scattered_evidence_ref_construction` + 白名单一致性检查（防白名单腐化）
- **L4** `find_evidence_references_in_providers` + 正负控制 + **真实 provider 文件注入一行 import 的对照**
- **L5** `find_out_of_band_attestation_binding` + 白名单一致性检查（绑定状态只能由加载器建立）

### Q7 全部门禁（用户指定的 7 条）

```bash
python -m pytest                                    # 901 passed, 4 skipped
python -m ruff check src tests tools                # All checks passed!
python tools/verify_manifest.py --check             # 19 entries verified, 0 violations
python tools/sign_fixtures.py --check               # 结构与 digest 全部一致
python tools/sign_fixtures.py --check --root fixtures/official   # 结构与 digest 全部一致
python tools/verify_sources.py --raw-dir .workbuddy/tmp/audit    # 4 份通过，0 份缺失，0 个问题
python eval/research/run_eval.py                    # 10/10；network_calls=0；llm_calls=0
```

### Q8 `report_id` 不变

```bash
python - <<'PY'
import json
from pathlib import Path
for f in sorted(Path("eval/research/expectations").glob("*.json")):
    print(f.stem, json.loads(f.read_text(encoding="utf-8"))["expect"].get("report_id"))
PY
```

`run_eval.py` 第 469–470 行逐条比对 `expect["report_id"]`；`scenarios_passed = 10` 即
**全部 10 个场景的 `report_id` 与冻结期望一致**。
官方报告另行实测：`rp_9e0057b9f989020f`（`P2.2_PLAN` §P2.2-2 记录的期望值），
排名 `pgvector 0.45 / qdrant_local 0.375 / milvus_standalone 0.2`，`integrity.passed=True`。

---

## ④ 敏感点与对照实验（请重点挑战）

### 对照实验 A：数据层可证伪（数据被改则结论必变）

```bash
python - <<'PY'
import json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, "src")
from agentboard.research.snapshot import load_catalog, load_snapshot
from agentboard.research.errors import MissingProvenanceError
tmp = Path(tempfile.mkdtemp()); clone = tmp / "research"
shutil.copytree("fixtures/research", clone)
p = clone / "options" / "pgvector.json"
payload = json.loads(p.read_text(encoding="utf-8"))
payload["source_kind"] = "official_documentation_excerpt"          # 伪装成官方摘录
p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
try:
    load_snapshot(p, entry=load_catalog(clone).get("pgvector"))
    print("闸门失效（不该发生）")
except MissingProvenanceError:
    print("加载期直接拒绝 MissingProvenanceError  ← 闸门生效")
PY
```

**实测结果**：加载期**直接拒绝**。比方案预期更强 —— 不必等到升格判定，
§2.8 的交叉强制在加载期就拦住。（实验在 `tmp` 副本上做，真实 fixtures 未被触碰。）

### 对照实验 B：改 1 字节 + 不重签 → 自绑定必失配

已固化为真实测试：`tests/research/test_promotion_gate.py::test_reason_mismatch_when_one_byte_of_fixture_changes`。
断言两件事同时成立：`load_attestation().binding_reason == "mismatch"`，
且加载期 C7 也独立报 `SnapshotDigestError`。**两道守卫各自成立、互不替代。**

### 对照实验 C：重签 INDEX 但不更新证明 → `index_sha256` 锚点必生效

真实测试：`test_reason_mismatch_when_index_resigned_without_refreshing_attestation`。
这条刻意走"改文件 + 重签"这一**真实工作流**，并断言此时
**加载期会成功**（重签让签名自洽）—— 即只有证明里的 `index_sha256` 能发现它。

### 对照实验 D：静态守卫可证伪

`tests/test_static_guards.py::test_provider_evidence_boundary_guard_flags_a_real_provider_file`
与 `test_evidence_ref_construction_guard_flags_a_real_research_file`：
用**真实源文件原文**注入一行 import / 一次构造，断言守卫变红；原文未改动时必须通过。

### 请重点挑战的五点

1. **`attestation.py` 是方案未枚举的新模块**。理由：C6 的读取/自绑定/对齐是三件独立且
   篇幅不小的事，塞进 `sources.py` 会让后者接近 1200 行并把"来源 schema"与"外部证明"混在一起。
   `attestation.py` 只依赖 `snapshot.py`，不产生循环导入。**若认为必须落在方案枚举的文件里，
   请直接指出，我改。**
2. **`PromotionChecks` 放在 `snapshot.py`**（方案写的是"`sources.py` 里的 `PromotionChecks`"）。
   移动理由：`promote_excerpt()` 按方案在 `snapshot.py`，而它必须校验 `checks`；
   若 `PromotionChecks` 留在 `sources.py` 会形成 `snapshot → sources` 的反向依赖。
   `sources.py` 通过 import 暴露它，对外可用路径不变。
3. **`FixtureSnapshot.to_dict()` 增了 `source_kind` 字段**。已 grep 确认无测试比较整份 dict；
   全量测试通过。若认为这属于"改既有输出形状"，请指出。
4. **`declared_documents` 是"快照集级并集"而不是单候选**。因为证明是集合级工件。
   若认为应为单候选，条件 9 的语义会退化成"子集包含"，会漏掉"集合里某份文档没被证明"。
5. **`FixtureCatalog` 新增读取 `snapshot_set`**（可选字段，缺失为 `""`）。
   `fixtures/research/INDEX.json` 没有这个键，因此读取必须容忍缺失 —— 已加测试钉住。

---

## ⑤ 未决风险与**明确未实现**的功能

### 明确未实现（不得当成已交付）

| 项 | 状态 | 说明 |
|---|---|---|
| **`live_reverify` 加强模式** | **未实现** | 方案 §2.2.3 信任模型里的"当场用原件重新核对"路径**本阶段只写进设计，未写一行实现**。当前只有默认模式（校验 L-a/L-b/L-c）。调用方**无法**用 `require_live_verification=True` 绕过对仓库完整性的信任。P2.2-3 才有接入点。 |
| **数字签名** | **未实现（设计上就不做）** | attestation 的完整性依赖"仓库内容不可被静默篡改"（git 历史 + 评审 + `G-PRESERVE`），**不是密码学保证**。能改仓库文件的人也能改工件，工具无法单方识破。 |
| `attestations/official.sources.v1.json` | **未生成** | 按约束 9，生成留给 P2.2-4。 |
| `research_grade` 接入 `assemble_report` | **未实现** | 属 P2.2-3（约束 8）。本阶段只有 `snapshot_set_policy` 的常量定义。 |
| locator 校验收敛（修 D2） | **未实现** | 属 P2.2-2（约束 7）。`promote_excerpt` 的截断规则与 `fixtures._evidence_for` 目前是**两份等价实现**，由 `test_excerpt_truncation_rule_matches_fixture_worker` 钉住等价性。 |
| 快照集隔离 / 混集报错 | **未实现** | 属 P2.2-3。`OfficialSources` 尚无 `snapshot_set_id` 属性（`FixtureCatalog.snapshot_set` 已可读）。 |

### 未决风险

1. **`from_dict` 单独构出的 attestation 恒不可升格**（`binding_reason is None` → fail-closed）。
   这是刻意的强保护，但副作用是：**任何不走 `load_attestation()` 的调用方永远得到 False**。
   已用 `test_attestation_from_dict_alone_is_never_promotable` 钉住该语义。
2. **`promotion_checks_for` 的 C8 会为每条声明维度构造一次 `EvidenceRef`**（纯内存，无 I/O 放大）。
   若将来维度/候选数大幅增长，这里需要缓存。当前 3 候选 × 3~6 维度，开销可忽略。
3. **~~写入策略 W1 的存在性检查与 `os.replace` 之间存在极窄的 TOCTOU 窗口~~**
   —— **本条已被复核证伪并修复（P1-2）**。第一版原文写"失败方向是拒绝写入，不会静默覆盖"，
   **该判断是错的**：窗口内出现的文件会被 `os.replace` **静默覆盖且函数返回 `True`**（已实测复现）。
   现改用 `os.link` 排他发布（原子 + 排他），窗口由发布动作本身消除。
4. **~~`os.link` 不可用时退化 `O_EXCL` 直写~~** —— **该退化路径已被复核否决并删除（W3）**。
   理由：`O_CREAT | O_EXCL` 只有排他性、**没有中断原子性**，强杀留下的半写文件
   看起来像"已生成的工件"，无法与"被篡改"区分。现在该情形**fail-closed**：
   清理临时文件、明确失败、退出码 1。**已无"非原子成功路径"这一类残留风险。**
5. **`snapshot_files[]` 的集合一致性只覆盖"INDEX.json 登记的候选快照"**，
   不含 `dimensions/` 与 `INDEX.json` 之外的其它文件；后者由既有 `sign_fixtures` 与
   加载期 C7/dims digest 覆盖。
6. `verify_sources.py` 的 `--write-attestation` 与该工具**只依赖标准库**的性质不冲突
   （未引入任何新依赖）；但库侧与工具侧各有一份 canonical 序列化与常量，
   靠 `test_tool_constants_match_library_constants` + `test_tool_and_library_serialize_to_identical_bytes`
   两条测试防止漂移。这是刻意的解耦代价，请复核该取舍是否接受。

---

## ⑥ 本阶段遵守的约束核验

| 约束 | 核验方式 | 结果 |
|---|---|---|
| 不修改 fixtures 字节 | `fixtures/**` 12 个文件逐一 sha256 与 INDEX 登记值比对；mtime 窗口扫描 | **0 个文件被改** |
| 不执行 `sign_fixtures --write` | 命令未出现；`--check` 双签名通过 | 通过 |
| 不创建 `attestations/` | `Path("attestations").exists()` | `False` |
| 不改 `is_trusted_evidence` / `TRUSTED_SOURCE_TYPES` / `ALLOWED_SOURCE_TYPES` | `src/agentboard/evidence.py` mtime 早于本回合；未编辑 | 未改 |
| 不改 `scripts/` `assets/` `materials/` | `verify_manifest --check` → 19/0 | 零改动 |
| 不联网 / 不跑真实 smoke | 全流程离线；未设 `AGENTBOARD_REAL_SMOKE=1` | 遵守 |
| 不新增第三方依赖 | 仅用标准库 + 既有依赖 | 未改 `pyproject.toml` |
| 不创建个人 skill / 不更新长期 memory | 本回合未调用任何 memory / skill 写入 | 遵守 |

**注意**：约束"不更新长期 memory"来自本任务指令，因此本交接单是本阶段唯一的持久化产物。

---

## 附：本阶段新增测试的分布（v3）

```
tests/research/test_attestation.py          51   （v1 41 + P1-1 ×4 + P1-2 ×4 + W3 ×3，并删除 1 条"退化即成功"旧用例）
tests/research/test_promotion_gate.py       35
tests/research/test_source_kind.py          14
tests/research/test_provider_boundary.py    11
tests/test_static_guards.py                 +11（总 36；v1 +7，v2 加 L5 ×4）
tests/test_verify_sources.py                +3（总 23）
                                    合计  +125
```

测试总数：`776 passed, 4 skipped` → **`901 passed, 4 skipped`**（收集 905）。
v1 为 887；v2 增 12 条（P1-1 ×4 + P1-2 ×4 + L5 ×4）达 899；v3 净增 2 条（W3 ×3，删旧退化用例 ×1）。

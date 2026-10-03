# BUG 经验库 —— 复核周期实录（P2.1 → P2.2-3）

> **用途**：把本项目复核周期里真实抓到、真实复现的 bug 沉淀成可检索的经验卡
> （多数已修复复核确认，未修的如实标注当前状态）。
> 每张卡都包含：现象、根因、复现方法、修复、**可迁移的教训**、以及**固化的防再犯机制**
> （bug 修复只有落成测试/守卫才算闭环）。
>
> **为什么值得记**：这批 bug 没有一个是"代码写错了" —— 全部是**设计层**错误，
> 而且全部通过了当时全绿的测试与门禁。绿灯 ≠ 安全，独立复核的价值正在于此。

---

## 总判据（先读这个）

复盘 6 个 bug，设计层错误**只有两类**：

| 类型 | 定义 | 本库实例 |
|---|---|---|
| **A：约束之间互相冲突** | 满足 A 必然违反 B，二者不能同时成立 | B1（隔离 vs 测试自身依赖）、B2'（W1 禁覆盖 vs W3 原子写入） |
| **B：宣称的保护层级低于实际攻击面** | 说"挡住了"，实际只挡一种写法 | B3（文档约定 ≠ 机制保证）、B5（映射当权威）、B2（检查 ≠ 发布）、B6（绑定历史 ≠ 本次数据有效） |

以及三个**反复出现的根因手法**（比单个 bug 更值得记）：

1. **用声明代替证明** —— 把"调用方说了什么"当成"事实是什么"（B3 的 `binding_reason`、B5 的 `promotable`、B6 的 set_id/source_kind/"曾核对过"）；
2. **用代码阅读代替实测** —— "看起来严密"的检查逻辑，跑一次就知道不等价（B2 两次中招）；
3. **用等价性测试代替结构保证** —— N 处重复实现靠测试维持"恰好相等"，任何一处被单独改动就分叉（B4）。

---

## Bug 目录

| # | 阶段 | 一句话 | 严重级 | 状态 |
|---|---|---|---|---|
| B1 | P2.1 | 会话级 `os.environ` 整体替换 → 真实 smoke 被静默降级 | P1 | 已修（复核确认） |
| B2 | P2.1→P2.2-1 | attestation 写入：覆盖竞态 + "退化路径返回成功"把原子性降级 | P1×2 | 已修（复核确认） |
| B3 | P2.2-1 | attestation 绑定状态可经公开构造器伪造 | P1 | 已修（复核确认） |
| B4 | P2.2-2 | 摘录截断规则 4 处重复实现（D2 的温床） | 设计债 | 已收敛 + 硬门禁 |
| B5 | P2.2-3 attempt 1 | `promotable` 可伪造映射 → 无证明通过 research_grade | P1 | 已修（复核确认：伪造入口已删除） |
| B6 | P2.2-3 attempt 2 | 有效旧证明重放到另一份数据 → 篡改快照仍 `passed=True` | P1 | **未修（blocked）**：同一信任绑定失败连续两次，停止自动重试，修复需先写清数据绑定契约（见 `PROJECT_RULES.md` R1） |

---

## B1 — 会话级环境整体替换造成静默降级

- **阶段**：P2.1 测试隔离（第一版实现）
- **现象**：`tests/providers/conftest.py` 用 `setattr(os, "environ", {})` 做**会话级**替换来隔离代理变量。
  后果链：`test_real_smoke.py` 的 `pytestmark = skipif(...)` 在**收集期**求值（变量还在 → 模块被收集），
  测试体内的 `os.environ.get(...)` 在**运行期**求值（变量已被抹掉 → 读到 `None`）——
  真实 smoke 会"通过收集、以匿名身份发出真实请求、把快照写进默认目录"，**看起来像跑成功了**。
- **根因**：为隔离而整体替换，误伤了测试自身依赖的变量；且抹除范围随会话扩散到同进程其它目录。
- **复现**：探针测试打印运行期 `os.environ`，三个 smoke 变量全部 `None`（收集期却可见）。
- **修复**：`monkeypatch.delenv(key)` **逐条**删除"能改变被测行为的键"（代理 + token 名单），
  不碰 `os.environ` 其余部分；作用域函数级。
- **教训**：
  - 隔离机制的作用域必须与"消费该环境的时机"对齐 —— **收集期求值 ≠ 运行期求值**；
  - "整体替换"几乎必然误伤：判断标准是**逐条删**还是**整体换**。

## B2 — 覆盖竞态 + 原子性被自行降级（两个连环 P1）

- **阶段**：`tools/verify_sources.py --write-attestation`（W1–W3）
- **现象 1（P1-2）**："先检查目标不存在 → `os.replace`" 之间存在真实窗口；
  窗口内被创建的文件被**静默覆盖且函数返回 `True`**（实测复现）。
- **现象 2（W3，第二次复核）**：修竞态时把"`os.link` 失败"处理成"退化 `O_EXCL` 直写**并返回成功**"——
  排他性成立、**中断原子性不成立**，强杀留下半写文件；当时的测试还把这条降级固化成了期望（`ok is True`）。
- **根因**：
  - 检查不是发布动作的一部分 —— "先检查再替换"与"禁止覆盖"**不等价**；
  - 实现者**自行把已批准的硬约束降级**，且用代码阅读得出"失败方向是拒绝，不会静默覆盖"的错误结论。
- **复现**：monkeypatch `os.link` 在调用时创建目标再抛 `FileExistsError` → 旧实现覆盖并返回 True；
  monkeypatch 抛普通 `OSError` → 旧实现走 O_EXCL 返回 True。
- **修复**：`os.link(temp, target)` **原子排他发布**（目标已存在即 `FileExistsError`，
  "谁先到"由发布动作判定）；`os.link` 不可用 ⇒ 清理临时文件、明确失败、退出码非 0（**不降级**）；
  AST 护栏禁止 `os.replace` 与 `O_CREAT`/`O_EXCL` 在写入路径重现。
- **教训**：
  - **"先检查再替换 ≠ 禁止覆盖"**：禁止性约束必须由发布原语自带排他性来保证；
  - **原子性无法提供时，唯一诚实的动作是失败**，而不是退回更弱的保证；
  - 修 bug 时引入的"务实退化"若被测试固化成期望，就变成了制度化的错误。

## B3 — 绑定状态可经公开构造器伪造

- **阶段**：P2.2-1 attestation（第一版）
- **现象**：`SourceVerificationAttestation` 的 `binding_reason` 是公开字段；
  `is_promotable_source()` 见到 `""` 就认为"绑定通过"。实测：**不调用 `load_attestation()`，
  直接构造传 `binding_reason=""`，得到 `True`** —— 绕过 C6 文件绑定。
- **根因**："必须由 loader 产生"只是**文档约定**，不是机制保证。
- **复现**：见 `REVIEW_P2.2-1_trust_boundary.md` 的复现脚本。
- **修复**：`binding` 改为 `field(init=False)`，由 `load_attestation()` 经
  `AttestationBinding`（内部类型）建立；新增 **L5 静态守卫**：
  `AttestationBinding(...)` 与 `object.__setattr__(..., "binding")` 只允许出现在 `attestation.py`。
- **教训**：
  - **文档约定 ≠ 机制保证**：凡是"只能由 X 产生"的状态，必须让它在构造器签名里**不可表达**；
  - 负向测试要打在**机制的生效点**上（这里是构造器签名，而不是某个运行时分支）。

## B4 — 摘录截断规则 4 处重复实现

- **阶段**：P2.2-1 结束时确认（D2 的温床）
- **现象**：同一条"≤400 取全文、否则取前 400"的截断规则在
  `fixtures.py` / `migrate.py` / `snapshot.py` / `sources.py` 各写了一份，
  靠等价性**测试**维持一致。同时 locator 校验有两条**规则不同**的实现
  （组装期 `startswith` vs 来源层 `==`），同一证据两条路径结论相反（D2 本体）。
- **根因**：规则每复制一份，就多一个独立漂移点；等价性测试维持的是"恰好相等"，不是"不可能分叉"。
- **复现**：`grep '[:MAX_EXCERPT_CHARS]' src/` → 4 处；构造 20 字符前缀摘录 → 组装期通过、来源层拒绝。
- **修复**：收敛为 `snapshot.expected_excerpt_for()` **唯一实现点**；
  两条校验路径都改调它；唯一性固化为**常驻测试**（文本行扫描 + AST 双口径 + 正控制）。
- **教训**：
  - **规则出现 N 处，必有一处先漂移**；清点必须机械执行（凭记忆清点曾漏 1 处）；
  - 收敛的判据是"结构上不可能分叉"，不是"当前测试证明它们相等"。

## B5 — `promotable` 可伪造映射推翻升格权威

- **阶段**：P2.2-3（attempt 1/3）
- **现象**：`SnapshotSet` 公开构造器接受 `promotable: Mapping[str, bool]`，
  I10 直接信任该布尔。实测：真实 `fixtures/official` + **无任何证明** +
  手填 `promotable={cid: True}` ⇒ `integrity.passed=True`、`report_id=rp_9e0057b9f989020f`。
  推翻"无 attestation 不可 research_grade"与"promotable 唯一合法来源"两条核心承诺。
- **根因**：**升格权威是一个可由调用方直接构造/修改的数据结构** ——
  "声明"（我说是可升格的）冒充了"证明"（经 load_attestation 绑定并通过的核对结论）。
  全绿门禁（922 passed、7 条门禁）没有覆盖它，因为负向测试只打了"helper 算出 False"的路径，
  没打"调用方手填 True"的路径。
- **复现**：
  ```python
  forged = SnapshotSet(snapshot_set_id=..., policy="research_grade",
                       promotable={cid: True for cid in candidate_ids})
  assemble_report(..., snapshot_set=forged, strict=False)  # → integrity.passed=True
  ```
- **修复**（loader-only 绑定原则 + 组装期重算）：
  1. **删除 `promotable` / `promotable_reasons` 字段** —— 伪造入口不存在（构造器签名级修复）；
  2. `SnapshotSet` 只承载声明：`snapshot_set_id` / `policy` / `attestation`（必须来自 `load_attestation()`）；
  3. I10 **按实际数据重算**：attestation 有效性（absent / unbound / version_expired /
     mismatch / incomplete 五种原因可区分）+ 候选 `source_kind` 取自**参与组装的真实快照**
     （人工摘要无论有无证明都不可升格）；
  4. 明细两个维度合并呈现（证明缺失 + 来源不是官方原文是**不同事实**）。
- **教训**：
  - **可由调用方构造/修改的数据结构，不能当作权限、资格、信任的权威** ——
    权威必须绑定到"只有守门者能建立的证据"上（这里是 loader-only 的文件级绑定）；
  - "promotable 由合法来源计算"这句话，在映射可构造的前提下**天然为假** ——
    合法来源的存在不能阻止非法来源；
  - 全绿门禁的盲区 = 负向测试没打到的攻击面；**每个"来源/权威/绑定"类设计，
    都必须有一条"伪造它必须失败"的测试**。

## B6 — 有效旧证明重放到另一份数据，篡改快照仍通过（未修，blocked）

- **阶段**：P2.2-3（attempt 2/3）
- **现象**：attempt 1 修掉 B5 的 `promotable` 伪造入口后，I10 改用
  "attestation 曾绑定某目录" + "参与组装的内存快照 `source_kind` 是 official" 推断报告受证明，
  却没有把证明的文件摘要/来源声明绑定到**参与本次组装的快照与 findings**。
  证明重放到另一份实际数据依旧通过。
- **复现**（Codex 独立负控，只读项目，证明工件在 OS 临时目录）：
  1. 用真实官方目录 + 真实原件核对，生成 passed attestation；
     `load_attestation(..., fixture_root=fixtures/official)` 得到 `binding_reason=""`（绑定有效）；
  2. 把参与组装的 pgvector 快照的一条 section 文本在内存改为 `... NOT_IN_ATTESTED_FIXTURE`；
  3. 用该快照经真实 `FixtureResearchWorker` 重新生成全部卡片，传**原 attestation** + **相同 `snapshot_set_id`**；
  4. 实际 `assemble_report(strict=True)` 返回 `integrity.passed=True`、无失败码、
     `report_id=rp_5b8081968b70100b`，证据摘录含未被证明的文本。
     位置：`src/agentboard/research/integrity.py:300-323,357-389`；`src/agentboard/research/assemble.py:498-506`。
- **根因**：证明的效力范围被锚定在"绑定历史 + 类型声明"上，而不是"本次实际使用的数据"上。
  三种冒充手段同源：相同 `snapshot_set_id`（标签当证明）、相同 `source_kind`（类型当内容）、
  loader 在另一份数据上的成功记录（历史当本次）。
  B3/B5 的教训只覆盖"由谁产生证明"（绑定入口 / 构造器签名），B6 是与之**正交**的另一维：
  "证明对哪份数据有效"——真证明 + 假数据的组合此前完全没有负控。
- **当前状态**：**未修（blocked）**。同一核心信任绑定失败在 attempt 1/2 连续出现，
  按"同一失败两次即停止"停止自动重试，不进入 P2.2-4；
  修复前必须先写清 candidate → attested file → snapshot/evidence 的**数据绑定契约**与负控期望
  （见 `PROJECT_RULES.md` R1），再讨论实现。
- **修复方向**（尚未实施）：组装期沿映射链逐条核对文件摘要，fail-closed；
  `strict=True` 抛 `ReportIntegrityError`，`strict=False` 返回含 I10 失败码的未通过报告，
  且**不修改报告内容**（`evidence_refs` / `supporting` 原样保留，不静默过滤）；
  I10 不进 content projection ⇒ `report_id` 两种 strict 下相同。
- **教训**：
  - "由谁产生证明"与"证明对哪份数据有效"是**两个独立的信任问题**，修掉前者不代表堵住后者；
  - 负控矩阵必须覆盖"**真证明 + 假数据**"组合，不只是"假证明 + 真数据"；
  - 门禁 924 passed 全绿对重放路径**零覆盖**——全绿与此前五次一样，不构成安全证据。

---

## 元教训（跨 bug 的模式，修复时先对照）

1. **每个"只能由 X 产生/只能走 X"的承诺，都要问三个问题**：
   构造器能不能绕过？（B3/B5）· 字段是不是公开可写？（B3）· 有没有测试证明"绕过必失败"？
2. **约束冲突要在设计期显式裁决**，而不是实现期"务实退化"（B2'）——
   降级必须回到约束的批准者面前，不能由实现自行决定。
3. **凡校验/规则出现两处，必有一处先漂移**（B4）——收敛成唯一实现点 + 机械门禁。
4. **反空转对照实验必须打在机制的生效点上**：B1 打在收集期/运行期的时序差、
   B3 打在构造器签名、B5 打在"伪造入口是否存在"、B6 打在"真证明 + 假数据"的重放路径。
   打偏了就是假绿。
5. **修 bug 的方案要先复现再动手**：本库 6 个 bug 中，5 个已完成的修复全部从"实测复现"开始，
   其中 B2 的第一版修复方案就是被"先复现"环节逼着改对的；
   B6 未修，但其修复路径同样被前置为"先写清数据绑定契约与负控期望，再实现"（`PROJECT_RULES.md` R1）。
6. **全绿 ≠ 安全**：本库 6 个 bug 中前 5 个存续期间，pytest/门禁全部全绿；
   B6 是在被标记 blocked 后靠**独立负控**抓到的，924 passed 的门禁对重放路径零覆盖。
   负向测试的覆盖面（尤其是"伪造/绕过/重放/降级"类攻击面）才是信任边界真正的试金石。

## 防再犯机制索引（已固化为代码的部分）

| 机制 | 位置 | 防 |
|---|---|---|
| 环境逐条隔离 + smoke 变量存活测试 | `tests/providers/conftest.py`、`test_transport_and_provider.py` | B1 |
| `os.link` 原子排他发布 + AST 禁 `os.replace`/`O_EXCL` + CLI 非零退出 | `tools/verify_sources.py`、`test_attestation.py` | B2 |
| `binding` init=False + L5 静态守卫 | `attestation.py`、`test_static_guards.py` | B3 |
| `expected_excerpt_for` 唯一实现点 + 双口径唯一性测试 | `snapshot.py`、`test_locator_consistency.py` | B4 |
| `SnapshotSet` 字段闭集断言 + 伪造升格回归测试 + unbound 负控 | `snapshot.py`、`test_research_grade.py` | B5 |
| 数据绑定契约 + 重放负控（`test_attestation_binding.py`，4 个重放/篡改/超范围场景） | **待实现**：契约与负控期望已定义于 `PROJECT_RULES.md` R1，实现随 P2.2-3 重设计进行（当前 blocked） | B6 |

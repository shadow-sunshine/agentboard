# 提交信息规范（Conventional Commits）

本仓库的提交历史本身就是项目文档的一部分——它要能回答"某条规则是哪次提交引入的、
哪个提交修了哪个 bug"。所以采用 Conventional Commits，并额外要求两件事：
**破坏性变更必须显式标注**、**提交正文必须说明"为什么"而不是"改了什么"**。

## 格式

```
<type>(<scope>): <subject>

<body>

<footer>
```

- `subject`：祈使句、≤ 50 字符、结尾不加句号
- `body`：空行分隔，说明**动机与影响**（不是 diff 的复述）
- `footer`：破坏性变更 / 关联 issue / 规则映射

## type

| type | 用途 | 例子 |
|---|---|---|
| `feat` | 新功能 | `feat(runtime): 受控生成决策器接入闭集协议` |
| `fix` | 缺陷修复（**必须在正文写复现与证伪方式**） | `fix(attestation): 证明绑定改为组装期现读现比` |
| `refactor` | 不改变外部行为的重构 | `refactor(board): 状态迁移收敛到单一入口` |
| `test` | 只改测试 | `test(p5): 补 synthesizer 越权负控` |
| `docs` | 只改文档 | `docs(readme): 补充真实 smoke 复核方式` |
| `perf` | 性能 | — |
| `build` | 构建 / 依赖 / 打包 | `build: ruff 范围收敛为 src tests tools` |
| `chore` | 杂项（.gitignore、CI 等） | `chore: 初始化仓库与门禁配置` |
| `revert` | 回滚 | — |

## scope

用模块名，取自实际目录：`cli` / `board` / `orchestrator` / `runtime` / `providers` /
`research` / `workers` / `eval` / `tools` / `fixtures` / `docs`。

## 破坏性变更

任何**改变已有产物格式、门禁行为或对外契约**的改动（fixture digest 变化、
`report_id` 变化、门禁增删、JSON 字段增删）都必须：

```text
feat(research): 快照集升 v2，新增 source_kind 字段

BREAKING CHANGE: 所有 fixture 需重新签名（python tools/sign_fixtures.py --write）。
旧 report_id 全部失效，eval/multi_agent/expectations/*.json 需同步更新。
迁移：按上节命令重签后重跑四套 eval。
```

## 本项目特有的正文要求

### 1. `fix` 必须写清"先复现、再证伪"

来自 `PROJECT_RULES.md` R7：**没有复现过的缺陷不得声称已修复**。

```text
fix(research): 修复证明重放绕过（B6）

复现：拿真实官方目录核对生成 passed attestation 后，把参与组装的快照
section 文本在内存改为 NOT_IN_ATTESTED_FIXTURE，经真实 worker 重生成卡片，
传原 attestation + 相同 snapshot_set_id，assemble_report(strict=True) 仍
返回 integrity.passed=True。

修复：证明绑定改为组装期三层机器校验——文件级从 fixture_root 现读现比、
内容级内存与磁盘逐字节相等、证据级摘录用唯一截断实现重算比对。

证伪：用同一攻击路径重跑，strict=True 抛 ReportIntegrityError、
strict=False 返回 failed_codes 含 I10 且报告内容未被清洗。
```

### 2. 触及信任边界的提交必须附规则映射

```text
fix(attestation): 证明绑定到本次实际数据（R1）

规则映射：
- R1（证明绑定本次数据）→ 攻击场景 1/2/3/4 全部复现为失败
  → tests/research/test_attestation_binding.py → 通过
- R2（升格权威不可构造）→ N/A（本提交不改升格入口，字段闭集断言已存在）
- R3（loader-only 绑定）→ N/A（未新增 init=False 字段）
- R4（排他性原语）→ N/A（无发布路径改动）
- R5（隔离作用域）→ N/A（无环境变量操作）
- R6（单一实现点）→ 通过（expected_excerpt_for 实现点计数 = 1）
- R7（负控打生效点）→ 通过（负控打在组装期现读现比，不是 helper 返回值）
```

### 3. 触及 fixture / 冻结契约的提交必须声明

```text
fix(snapshot): 官方快照补充 Git blob SHA 字段

BREAKING CHANGE: fixtures/official/** digest 变化，需重新签名。
G-PRESERVE 检查：eval/research 与 eval/official 的期望 report_id 未变
（rp_9e0057b9f989020f 稳定）。
```

## 禁止

- ❌ `update code` / `fix bug` / `优化` / `debug` 这类无信息量的 subject
- ❌ 一个提交塞多个不相关改动（每个可验证里程碑单独提交）
- ❌ 把 `fix` 和 `feat` 混在一次提交里
- ❌ 触碰冻结契约（`fixtures/**`、既有 eval 期望、`EvidenceRef` 内容寻址、
  attestation 信任契约）却不写 `BREAKING CHANGE`
- ❌ 声称"修复了 X"但正文没有复现步骤

## 推荐的提交序列（对应项目阶段）

项目按 P0 → P6 推进，提交历史应能映射回这些里程碑：

```text
chore: 初始化仓库、门禁配置与 .gitignore
feat(contracts): 三层数据契约与任务板状态机
feat(research): 确定性报告组装、评分与 I1-I9 完整性校验
feat(attestation): 官方来源证明绑定（R1 三层机器校验）
feat(runtime): 检索编排流水线（plan / budget / 快照去重）
feat(orchestrator): 多 Agent 编排与独立 verifier
feat(cli): replay / offline / live 三模式入口
feat(providers): 固定 commit 原文获取与来源审计
feat(html): 单文件报告页渲染（只读投影，零依赖）
docs: 真实 smoke 记录、边界声明与简历材料
```

## 分支

| 分支 | 用途 |
|---|---|
| `main` | 门禁全绿才可合并 |
| `feat/<简述>` | 新功能 |
| `fix/<简述>` | 缺陷修复 |
| `docs/<简述>` | 文档 |

合并前必须本地跑通：

```bash
python -m pytest -q
python -m ruff check src tests tools
python tools/verify_manifest.py --check
python tools/sign_fixtures.py --check
python tools/sign_fixtures.py --root fixtures/official --check
python eval/research/run_eval.py
python eval/official/run_eval.py
python eval/live_research/run_eval.py
python eval/multi_agent/run_eval.py
```

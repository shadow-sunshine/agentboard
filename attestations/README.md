# attestations/ —— 来源核对证明工件（C6）

本目录存放 `SourceVerificationAttestation`（`source.verification.attestation.v1`）：
**外部证明工件**，与 `fixtures/**` 完全分离。它的存在与否**不影响** fixture 加载、
签名校验与 P1A 报告复现（G-PRESERVE G4）；它只决定一件事——
**官方摘录能否作为研究级（`research_grade`）推荐的支撑来源**。

| 状态 | 加载 | P1A 回放 | research_grade |
|---|---|---|---|
| 工件存在且 `passed` + 文件级绑定成立 | 正常 | 正常 | **可升格** |
| 工件缺失（`absent`） | 正常 | 正常 | 不可升格（原因可区分） |
| 工件 `incomplete` / `verifier_version` 过期 / 字节不符（`mismatch`） | 正常 | 正常 | 不可升格（原因可区分） |

## 信任模型（必须诚实，不给"密码学保证"的错觉）

**本项目不实现数字签名。** attestation 与 fixture 同仓存放：能改写仓库文件的人
也能同时改写 attestation 并同步重签，这种攻击**无法被工具单方识破**。

它的完整性依赖**过程保证**：

1. **git 历史**：工件与 fixture 的任何改动都留痕、可追溯；
2. **代码评审**：`fixtures/**` 冻结（G1）+ 双签名（G2）+ manifest 校验；
3. **G-PRESERVE 门禁**：`sign_fixtures.py --check` 双根校验；`--write` 被禁止。

因此本工件证明的命题是：**"有人在某个时点，按登记的规则（sha256 + git blob sha1 +
行号切片），把这批快照与真实原件逐字节核对过，全部一致"**——
不是"这批字节绝对可信"。

## W1–W4 写入策略（`tools/verify_sources.py --write-attestation`）

| 策略 | 语义 |
|---|---|
| W1 | 默认**禁止覆盖**：目标已存在且内容不同 ⇒ 拒绝写，原文件原样保留 |
| W2 | 内容**逐字节相同** ⇒ 幂等成功，不写盘（mtime 不变）；不同 ⇒ 拒绝 |
| W3 | **原子排他发布**：temp + fsync + `os.link`；`os.link` 不可用 ⇒ 清理、明确失败、退出码非 0，**不得退化**为非原子写入（`os.replace` / `O_EXCL` 被静态护栏禁止） |
| W4 | 有原件缺失时产物只能是 `incomplete`，**不得**静默改写为 `passed`（`passed` 只能被核对出来） |

## 再生成方式

```bash
# 前置：真实原件在 .workbuddy/tmp/audit/（人工下载，不入库、不联网重取）
python tools/verify_sources.py --root fixtures/official \
  --raw-dir .workbuddy/tmp/audit --write-attestation attestations/official.sources.v1.json
```

- 工件**零时间字段**（无 generated_at / verified_at）：同一输入 ⇒ 逐字节相同；
- `verifier_version` 是核对**规则**版本（非时间戳），核对逻辑变更时手动递增；
- fixture 任何字节变化（含重签 INDEX）都会使旧工件失效 —— 这是**设计**：
  重新生成即"重新核对"。

## missing / problem 二分

来源核对（`verify_sources` / `live_reverify`）对"原件"的两种坏状态**分开报**：

- **missing**：`raw_dir` 里找不到 sha256 匹配的原件（可能是目录被清理）——
  只说明"这次没法核对"，**不**说明"内容被改过"；
- **problem**：找到了原件但内容/行号/摘要不符 —— 这才是篡改信号。

`--allow-missing` 只显式放行 missing，**永不**放行 problem。

## `live_reverify` 加强模式（`sources.live_reverify`）

attestation 是"过去时点"的核对结论。需要**不依赖 attestation 可信度**的场合
（例如怀疑磁盘在绑定后被换），用库函数当场重算：

```python
from pathlib import Path
from agentboard.research.sources import live_reverify, load_official_sources

sources = load_official_sources(Path("fixtures/official"))
ok, problems, missing = live_reverify(sources, Path(".workbuddy/tmp/audit"))
```

它当场完成三件事：`raw_sha256` 比对 → git blob sha1 比对 → 按 `line_range`
切片与 `sections[].text` 逐字节比对。missing 与 problem 分开返回。
它与 `tools/verify_sources.py` 是**同一语料上的两份判定实现**，
由 `tests/test_official_eval_gate.py` 的交叉核对测试钉住不漂移。

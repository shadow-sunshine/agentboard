# 审阅交接单：P2.1 代理环境测试隔离（第三版 · marker 豁免 smoke 测试）

> 可复核交接单。第三版解决前两版的核心遗留：**隔离不能误伤 opt-in 的真实 smoke 测试**。
> 项目根：`D:\It\Test_Project\case\Multi_Agent_Orchestration`
> 解释器 `$PY`：`C:\Users\shadow\.workbuddy\binaries\python\envs\default\Scripts\python.exe`

---

## ① 复核意见与本版对应

复核（第二次）要求：

1. 普通 mock 测试继续隔离代理/token；
2. opt-in 的 `test_real_smoke.py` 必须保留显式提供的 `GITHUB_TOKEN` 和代理环境；
3. 隔离限制在 mock 测试，或明确排除真实 smoke；
4. 加不联网测试验证 smoke 配置能读到 token/代理；
5. 不通过跳过测试、清空 shell 环境绕过；
6. V6 改用真实原件目录（而非 `--allow-missing`）。

**本版做法：用 pytest marker `real_smoke` 显式标记"需要真实环境"的测试，隔离 fixture 命中该 marker 时跳过。**

| 复核要求 | 本版 |
|---|---|
| mock 继续隔离 | ✅ 保留 `_neutralise_proxy_and_token_env`（逐条 `delenv` 代理+token） |
| smoke 保留 token/代理 | ✅ `test_real_smoke.py` 加 `pytest.mark.real_smoke`，隔离 fixture 对其 `return` |
| 明确排除 smoke | ✅ marker 即"明确排除"的显式表达，非靠文件名/目录巧合 |
| 不联网验证 | ✅ 新增 `tests/providers/test_real_smoke_config.py`（3 条，不联网） |
| 不跳过/不清空 | ✅ `monkeypatch.delenv` 自动还原；未删任何测试 |
| V6 真实原件 | ✅ `verify_sources.py --raw-dir .workbuddy/tmp/audit`（4 通过 0 缺失） |

---

## ② 改动清单

| 文件 | sha256 | 改动 |
|---|---|---|
| `tests/providers/conftest.py` | `0aafd38dd0501bdc…` | 新增 `REAL_SMOKE_MARKER` 常量；隔离 fixture 加 `request` 参数，命中 `real_smoke` marker 时 `return` |
| `tests/providers/test_real_smoke.py` | `b8d52516be8cc1a3…` | `pytestmark` 改为 `[skipif(...), real_smoke]` 列表 |
| `tests/providers/test_real_smoke_config.py` | `a2ec9f8b91f12584…` | **新增**：3 条不联网测试，验证 smoke 能读到显式 token/代理 |
| `tests/providers/test_transport_and_provider.py` | `3c59d8cbc2217257…` | 无净改动（上一版已含全部隔离自检，本版未变） |
| `pyproject.toml` | `e04791ff28bd843c…` | 注册 `real_smoke` marker（消 warning） |

**核对命令：**
```bash
cd "D:/It/Test_Project/case/Multi_Agent_Orchestration"
"$PY" -c "
from pathlib import Path
import hashlib
for p in ['tests/providers/conftest.py','tests/providers/test_real_smoke.py',
          'tests/providers/test_real_smoke_config.py','tests/providers/test_transport_and_provider.py',
          'pyproject.toml']:
    print(hashlib.sha256(Path(p).read_bytes()).hexdigest(), p)
"
```

---

## ③ 核心机制：marker 豁免

`conftest.py` 隔离 fixture 的关键改动：

```python
REAL_SMOKE_MARKER = "real_smoke"

@pytest.fixture(autouse=True)
def _neutralise_proxy_and_token_env(monkeypatch, request):
    if request.node.get_closest_marker(REAL_SMOKE_MARKER):
        return   # 真实 smoke 测试：豁免隔离，保留显式 token/代理
    for key in NEUTRALISED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
```

`test_real_smoke.py` 的 `pytestmark` 改为列表：

```python
pytestmark = [
    pytest.mark.skipif(os.environ.get("AGENTBOARD_REAL_SMOKE") != "1", reason="..."),
    pytest.mark.real_smoke,   # 豁免代理/token 隔离
]
```

`pyproject.toml` 注册 marker（避免 PytestUnknownMarkWarning）。

---

## ④ 不联网验证测试（新增文件的核心）

`tests/providers/test_real_smoke_config.py` 有 3 条测试，全部不联网：

| 测试 | 验证内容 |
|---|---|
| `test_smoke_provider_reads_explicit_token` | `credential_mode()` 读到 `token`（非 anonymous） |
| `test_smoke_transport_reads_explicit_proxy` | `resolve_proxy()` 读到显式代理 |
| `test_smoke_env_is_not_wholesale_replaced` | `os.environ` 未被整体替换 |

**关键设计（为何这次能真正证伪，而上一版不能）：**

上一版在测试体内用 `monkeypatch.setenv` 设 token —— 那发生在隔离 `delenv` **之后**，
等于把被删的变量又设回去，隔离误伤也测不出（"假绿"）。本版改用
**`scope="module"` 的 fixture 直接写 `os.environ`**，它在函数级隔离 fixture **之前**生效：

```
module fixture（写 token/代理进 os.environ）
      ↓ 先于
function 级隔离 fixture（命中 real_smoke → return，不删）
      ↓
测试体（断言能读到）── 若隔离误删，此处读到空值 → 变红
```

---

## ⑤ 声明验证 + 可证伪实验

**V1–V7（均已在默认环境自跑）：**

| # | 命令 | 结果 |
|---|---|---|
| V1 | `"$PY" -m pytest` | `776 passed, 4 skipped` |
| V2 | `"$PY" -m ruff check src tests tools` | `All checks passed!` |
| V3 | `"$PY" tools/verify_manifest.py --check` | `19 entries verified, 0 violations` |
| V4 | `"$PY" tools/sign_fixtures.py --check` | 结构与 digest 一致 |
| V5 | `"$PY" tools/sign_fixtures.py --check --root fixtures/official` | 结构与 digest 一致 |
| V6 | `"$PY" tools/verify_sources.py --raw-dir .workbuddy/tmp/audit` | **4 份通过，0 份缺失，0 个问题** |
| V7 | `"$PY" eval/research/run_eval.py` | 10/10，指标与基线逐项相同（见下） |

V7 期望：`scenarios_passed=10`、`determinism=1.0`、`evidence_resolvable_rate=1.0`、
`fixture_evidence_rate=1.0`、`silent_missing_dimensions=0`、`integrity_checks_executed=63`、
`network_calls=0`、`llm_calls=0`。

**实验 1 —— 反空转（禁用 marker 豁免，smoke 配置测试必须变红）：**
```bash
"$PY" -c "
import subprocess, sys
from pathlib import Path
cf = Path('tests/providers/conftest.py')
orig = cf.read_text(encoding='utf-8')
old = 'if request.node.get_closest_marker(REAL_SMOKE_MARKER):\n        return'
broken = orig.replace(old, 'if False:  # marker exemption disabled\n        return', 1)
cf.write_text(broken, encoding='utf-8')
try:
    r = subprocess.run([sys.executable,'-m','pytest','tests/providers/test_real_smoke_config.py',
                        '-p','no:randomly','-q'], capture_output=True, text=True)
    print('exit =', r.returncode, '(期望 1)')
    print('\n'.join(r.stdout.splitlines()[-6:]))
finally:
    cf.write_text(orig, encoding='utf-8')
    print('还原:', cf.read_text(encoding='utf-8') == orig)
"
```
> 实测：`exit=1`，`test_smoke_provider_reads_explicit_token` 与
> `test_smoke_transport_reads_explicit_proxy` 变红。✅ 证明非空转。

**实验 2 —— 污染环境一致性（mock 隔离仍有效，smoke 仍默认跳过）：**
```bash
HTTPS_PROXY="http://203.0.113.99:1" HTTP_PROXY="http://203.0.113.99:1" GITHUB_TOKEN="ghp_poison" "$PY" -m pytest
```
> 实测：`776 passed, 4 skipped`，与干净环境完全一致。✅
> （4 个 skip = smoke 3 + 原有 1；smoke 仍因未设 `AGENTBOARD_REAL_SMOKE=1` 而跳过）

---

## ⑥ 未决风险与有意取舍

1. **marker 豁免的粒度是"文件级"还是"测试级"？** 本版用模块级 `pytestmark =
   [skipif, real_smoke]`，即 `test_real_smoke.py` 里**所有**测试都豁免隔离。
   这是有意的 —— 该文件本来就是"真实联网"语义。若将来在该文件里加"需要隔离"的
   离线测试，需单独处理。**请复核者判断是否可接受。**
2. **`test_real_smoke_config.py` 自带 `real_smoke` marker，但不设置 `AGENTBOARD_REAL_SMOKE=1`**，
   所以它本身不会触发 `test_real_smoke.py` 里的真实联网（那些仍被 skipif 拦住）。
   两个文件的"豁免隔离"用的是同一个 marker，但"是否联网"由各自控制。**这是有意的分离。**
3. **`_simulate_explicit_credentials_in_shell` 用 `os.environ.pop` 清理**，若测试中途
   失败，`pop` 仍会执行（fixture teardown 保证）。但若 `HTTPS_PROXY` 原本就存在，
   会被这个 fixture 覆盖成 `EXPLICIT_PROXY` 再 `pop` 掉 —— 不会还原成原值。
   **这是已知边界**：该 fixture 假设测试进程里没有真实代理（隔离机制本身也这么假设）。
4. **V6 依赖 `.workbuddy/tmp/audit/` 里的 4 份原件**。这是运行时产物目录，
   若被清理，V6 会退回"4 缺失"。原件来源见 `fixtures/official/sources/*.json` 的
   `provenance.documents[].raw_url`（固定到 commit），可随时重新下载。

---

## 附：给复核者的一句话起点

> 第三版把"隔离"和"真实 smoke 读凭据"用 marker 显式分开。请重点核实：
> (1) 跑实验 1，禁用 marker 豁免后 smoke 配置测试确实变红（证明非空转）；
> (2) 跑实验 2，污染环境下 mock 隔离仍成立、smoke 仍默认跳过；
> (3) V6 用真实原件目录后是"4 通过 0 缺失"而非 `--allow-missing` 的放行。

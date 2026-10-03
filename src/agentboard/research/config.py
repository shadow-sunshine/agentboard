"""研究领域层常量（P1A-2）。

集中管理标识符正则、schema 标识、以及只在 ordinal / boolean 两种量表上成立的边界，
便于测试注入与后续阶段复用。

字符集约定（见 P1A_PLAN.md §1）：`snapshot_version` / `schema` 一律用点号，
全项目不出现 `@`。
"""

from __future__ import annotations

import re

# ---- 标识符 ----

DIMENSION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
CANDIDATE_ID_PATTERN = DIMENSION_ID_PATTERN
FINDING_ID_PATTERN = re.compile(r"^rf_[a-z0-9]{16}$")

# ---- schema 标识 ----

FIXTURE_SCHEMA = "research.fixture.v1"
INDEX_SCHEMA = "research.index.v1"
DIMENSIONS_SCHEMA = "research.dimensions.v1"

# ---- 来源种类（P2.2-1：让"官方原文"与"人工摘要"在加载期可区分） ----
#
# 这两个值**已经写在每一份 fixture 文件里**；P2.2-1 是首次去读它、并把闭集校验
# 变成加载期强制。因此这里新增常量**不会**改动任何 fixture 字节（见 P2.2_PLAN §6.1.1）。

#: 人工撰写的技术摘要（`fixtures/research/`）：**仅**用于 P1A 回归兼容，
#: 不得作为研究级推荐的可信支撑（`is_promotable_source` 对它恒为 False）。
SOURCE_KIND_AUTHORED_SUMMARY = "authored_summary"

#: 官方文档 / 官方仓库文件的逐行摘录（`fixtures/official/`）：必须带 `provenance`。
SOURCE_KIND_OFFICIAL_EXCERPT = "official_documentation_excerpt"

VALID_SOURCE_KINDS: frozenset[str] = frozenset(
    {SOURCE_KIND_AUTHORED_SUMMARY, SOURCE_KIND_OFFICIAL_EXCERPT}
)

#: INDEX.json 里可选的快照集标识（如 `official.2026-09-23`）。
#: **可选**：P1A 的 `fixtures/research/INDEX.json` 没有这个键，读取时必须容忍缺失。
SNAPSHOT_SET_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9._-]*$")

#: 行区间形态：`起始行` 或 `起始行-结束行`（1 起闭区间）。
#: 放 config 是为了让来源层（`sources.py`）与证明层（`attestation.py`）共用**同一份**定义 ——
#: 两处各写一份正则，迟早会漂移成"一个接受、一个拒绝"。
LINE_RANGE_PATTERN = re.compile(r"^[1-9]\d*(?:-[1-9]\d*)?$")

# ---- 快照集策略（P2.2-1 只定义，接入组装留给 P2.2-3） ----

#: 复现 P1A 基线：维持既有 `is_trusted_evidence` 语义，保证 P1A `report_id` 逐字节不变。
POLICY_REGRESSION_REPLAY = "regression_replay"
#: 研究级：额外要求每条支撑证据的候选满足 `is_promotable_source`。
POLICY_RESEARCH_GRADE = "research_grade"
VALID_SNAPSHOT_SET_POLICIES: frozenset[str] = frozenset(
    {POLICY_REGRESSION_REPLAY, POLICY_RESEARCH_GRADE}
)

# ---- 外部验证证明（SourceVerificationAttestation，C6） ----
#
# C6（摘录与原文逐字节一致）是准入闸门里**唯一需要原件**的条件，因此它的结论
# 不能写在 fixture 里（那会改 fixture 字节），必须由独立工件承载。

ATTESTATION_SCHEMA = "source.verification.attestation.v1"

#: 执行核对的工具标识（固定字面量，便于在工件里追溯是谁做的核对）。
VERIFIER_ID = "tools/verify_sources.py"

#: 核对**规则**版本：整数，核对逻辑变更时手动递增。
#: 刻意不用时间戳 —— 时间戳会让"同一输入"在不同时刻产出不同字节，
#: 破坏"同一输入必得同一输出"（与 `captured_at` 不读时钟、`report_id` 内容寻址同一纪律）。
EXPECTED_VERIFIER_VERSION = 1

#: 核对算法的确定性描述。
VERIFIER_ALGORITHM = "sha256+git_blob_sha1+line_slice"

#: 单份文档的核对结论 / 整份工件的汇总结论。
RESULT_PASSED = "passed"
RESULT_INCOMPLETE = "incomplete"
VALID_ATTESTATION_RESULTS: frozenset[str] = frozenset({RESULT_PASSED, RESULT_INCOMPLETE})

# ---- 不可升格的原因（必须可区分，不能把两个不同的 False 混为一谈） ----

#: 没有 attestation 工件（离线回放场景：加载成功、报告照常，只是不可升格）
REASON_ABSENT = "absent"
#: attestation 存在但不是全部 `passed`（有缺失原件）
REASON_INCOMPLETE = "incomplete"
#: attestation 的核对规则版本与当前代码不一致（旧的证明已过期）
REASON_VERSION_EXPIRED = "version_expired"
#: 文件级自绑定校验不通过（INDEX.json 或其登记的快照文件字节已变）
REASON_MISMATCH = "mismatch"
#: attestation 未被 `load_attestation()` 做过文件级绑定（例如直接 `from_dict` 拼出来的）
REASON_UNBOUND = "unbound"
#: 该来源种类根本不适用升格（`authored_summary`）
REASON_NOT_APPLICABLE = "not_applicable"

#: WorkerResult.structured_payload 使用的 schema（P1A-2 唯一取值）
FINDINGS_SCHEMA = "research.findings.v1"
FINDINGS_VERSION = 1

# ---- 量表 ----

#: P1A 只支持 ordinal / boolean。numeric 显式缺席：分支未定义前不放行，
#: 传进来即 ValueError（见 ComparisonDimension 的校验）。
SCALE_ORDINAL = "ordinal"
SCALE_BOOLEAN = "boolean"
VALID_SCALE_KINDS: frozenset[str] = frozenset({SCALE_ORDINAL, SCALE_BOOLEAN})

#: boolean 量表的标签是固定二字面量，不允许自定义（否则语义漂移）
BOOLEAN_SCALE_LABELS: tuple[str, ...] = ("false", "true")
MIN_ORDINAL_LABELS = 2
MIN_UTILITY_SPREAD = 2

WEIGHT_SUM_TOLERANCE = 1e-6
MAX_CLAIM_CHARS = 400

# ---- finding 状态 ----

STATUS_SUPPORTED = "supported"
STATUS_UNSUPPORTED = "unsupported"
VALID_FINDING_STATUSES: frozenset[str] = frozenset({STATUS_SUPPORTED, STATUS_UNSUPPORTED})

#: unsupported finding 的占位取值。它**不是**合法量表标签，只是明确标注
#: "本快照里没有该维度的依据"；`value ∈ scale_labels` 这条校验只对 supported 生效。
UNSUPPORTED_VALUE = "unknown"

# ---- 报告 ----

#: 报告 schema 版本（点号风格，全项目不出现 `@`）
REPORT_SCHEMA_VERSION = "p1a.1"

#: 组装器标识。刻意不含版本以外的任何动态信息：报告里**没有** generated_at，
#: 同一输入两次运行必须得到逐字节相同的报告。
REPORT_GENERATED_BY = "deterministic-assembler.p1a.1"

#: 选型至少要有两个候选才有比较意义
MIN_CANDIDATES = 2

RISK_ID_PATTERN = re.compile(r"^rk_[a-z0-9]{16}$")
REPORT_ID_PATTERN = re.compile(r"^rp_[a-z0-9]{16}$")

#: 规则风险的 rule_id 闭集。放在 config 而不是 rules，是为了让 models 能在构造期
#: 校验 `rule_id ∈ RULE_IDS`，同时避免 models ↔ rules 循环导入。
RULE_UNSUPPORTED_WEIGHTED = "R_UNSUPPORTED_WEIGHTED"
RULE_SINGLE_SOURCE = "R_SINGLE_SOURCE"
RULE_SCORE_GAP_TIGHT = "R_SCORE_GAP_TIGHT"
RULE_UNTRUSTED_BACKING = "R_UNTRUSTED_BACKING"
RULE_WORKER_REPORTED = "R_WORKER_REPORTED"

RULE_IDS: frozenset[str] = frozenset(
    {
        RULE_UNSUPPORTED_WEIGHTED,
        RULE_SINGLE_SOURCE,
        RULE_SCORE_GAP_TIGHT,
        RULE_UNTRUSTED_BACKING,
        RULE_WORKER_REPORTED,
    }
)

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
VALID_SEVERITIES: frozenset[str] = frozenset({SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_HIGH})

#: 风险排序权重：越严重越靠前（用于报告内固定排序，不是业务评分）
SEVERITY_ORDER: dict[str, int] = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}

ORIGIN_RULE_BASED = "rule_based"
ORIGIN_WORKER_REPORTED = "worker_reported"
VALID_RISK_ORIGINS: frozenset[str] = frozenset({ORIGIN_RULE_BASED, ORIGIN_WORKER_REPORTED})

# ---- 任务包 context 里的候选归属声明 ----
#
# 只认「独立一行、显式 key」这一种形态：`candidate_id: pgvector`
# 不做任何按 task_id 或自由文本推断候选的启发式。
CANDIDATE_ID_LINE_PATTERN = re.compile(
    r"^[ \t]*candidate_id[ \t]*:[ \t]*(\S+)[ \t]*$", re.MULTILINE
)

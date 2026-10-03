"""研究领域异常族（P1A-2）。

约定：
- 全部继承 `AgentBoardResearchError`，便于上层一次性捕获领域层问题；
- 值域 / 形态类异常同时继承 `ValueError`，与核心契约层（`contracts.py`）的调用体验一致
  ——调用方拿到的都是外部 JSON / 字典数据，属于值校验而非编程类型错误。
"""

from __future__ import annotations


class AgentBoardResearchError(Exception):
    """研究领域层异常基类。领域层抛出的所有异常都应是它的子类。"""


class ResearchValueError(AgentBoardResearchError, ValueError):
    """领域值 / 形态非法（配置、快照、任务包、payload 都走这里）。"""


# ---- 维度注册表 ----


class DimensionError(ResearchValueError):
    """维度配置相关问题。"""


class UnknownDimensionError(DimensionError):
    """引用了一个不在注册表（且不是任何 alias）的 dimension_id。"""


class DimensionWeightError(DimensionError):
    """维度权重不合法，或注册表权重之和不为 1.0。"""


class InvalidDimensionValueError(DimensionError):
    """finding 的 value 不是该维度 scale_labels 的成员。"""


class DimensionConflictError(DimensionError):
    """同一个候选在同一个（归并后的）维度上出现了多个互相矛盾的取值声明。"""


class DegenerateDimensionError(DimensionError):
    """维度没有区分度（utility_map 取值全相同），无法用于打分。"""


# ---- 快照（本地资料） ----


class SnapshotError(ResearchValueError):
    """fixture 快照加载与校验问题。"""


class SnapshotSchemaError(SnapshotError):
    """快照文件 schema 标识不符或结构非法。"""


class SnapshotMismatchError(SnapshotError):
    """INDEX.json 与 option 文件的关键字段（版本 / 拍摄日期）不一致。"""


class SnapshotDigestError(SnapshotError):
    """快照文件的 sha256 与 INDEX.json 登记的不一致（文件被改动过，需要重新签名）。"""


class UnsafeSourcePathError(SnapshotError):
    """source_path 不是相对路径，或包含 URL（快照必须完全本地）。"""


class DanglingSectionError(SnapshotError):
    """dimension_value 指向了不存在的 section。"""


class UnknownCandidateError(SnapshotError):
    """candidate_id 合法，但快照目录里没有这个候选。"""


# ---- 官方资料快照的来源层 ----
#
# 这一族只在 `sources.py`（`fixtures/official/`）里抛出：领域层不读 provenance，
# 所以来源问题不会伪装成领域问题，反之亦然。


class SourceError(ResearchValueError):
    """官方资料快照的来源层问题。"""


class MissingProvenanceError(SourceError):
    """官方快照缺少 provenance 块（或摘录缺少来源字段），无法追溯到官方来源。"""


class SourceProvenanceSchemaError(SourceError):
    """provenance 结构非法：字段缺失 / 类型错误 / 格式不符（sha、行号、basis 等）。"""


class NonOfficialSourceError(SourceError):
    """来源不是该候选的官方仓库，或 URL 与登记的 repo / ref / path 不一致。"""


class SourceReferenceError(SourceError):
    """维度声明或证据指向了不存在的来源 / section，或与快照原文不一致。"""


class IncompleteDimensionCoverageError(SourceError):
    """注册表里的维度既没有取值声明、也没有登记到 unknown_dimensions（静默省略）。"""


# ---- 来源种类（P2.2-1） ----


class UnknownSourceKindError(SourceError):
    """快照的 `source_kind` 不在闭集内。

    这是**配置错误**，不是"不可升格"：调用方把未知类型传给 `is_promotable_source`
    也会抛它，而不是静默返回 False —— 静默返回 False 会让"类型写错了"
    伪装成"这份资料不够格"，两者需要完全不同的处置。
    """


# ---- 升格闸门（P2.2-1） ----


class PromotionError(SourceError):
    """`PromotableExcerpt` 的构造条件不成立。"""


# ---- 外部验证证明（SourceVerificationAttestation，P2.2-1） ----


class AttestationError(SourceError):
    """验证证明工件的读取 / 校验问题。"""


class AttestationSchemaError(AttestationError):
    """证明文件的结构非法：schema 不符 / 字段缺失 / 类型或格式错误。"""


# ---- payload ----


class PayloadSchemaMismatchError(ResearchValueError):
    """structured_payload 的 schema 不是本领域层认识的标识。"""


# ---- Worker 任务包 ----


class CandidateIdMissingError(ResearchValueError):
    """无法从任务包 context 中得到唯一合法的 candidate_id。

    覆盖三种情况：完全缺失、格式非法、出现多行且取值不一致。
    这里**不做任何启发式兜底**——不猜、不按 task_id 推断、不做文本匹配。
    """


# ---- 报告组装 ----


class AssemblyError(ResearchValueError):
    """组装阶段的问题（候选集合、任务归属、跨对象引用等）。"""


class CandidateMissingError(AssemblyError):
    """任务板上没有任何已完成的 research 卡。"""


class InsufficientCandidatesError(AssemblyError):
    """参与比较的候选不足（选型至少需要两个方案才有比较意义）。"""


class CandidateCoverageError(AssemblyError):
    """快照目录里存在某个候选，但任务板没有产出它的结果（或反之）。"""


class ForeignTaskError(AssemblyError):
    """finding 声明的 task_id 不属于本次任务板。"""


class DanglingLocatorError(AssemblyError):
    """fixture 证据的 locator 无法在对应快照里解析。"""


class IntegrityError(AssemblyError):
    """报告完整性校验未通过（strict 模式）。"""

    def __init__(self, failed_codes: tuple[str, ...], message: str = "") -> None:
        self.failed_codes = tuple(failed_codes)
        detail = message or f"报告完整性校验未通过：{', '.join(self.failed_codes)}"
        super().__init__(detail)


class ReportIntegrityError(IntegrityError):
    """`assemble_report(strict=True)` 在存在失败检查项时抛出。"""

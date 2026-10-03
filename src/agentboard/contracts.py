"""AgentBoard 核心数据契约（全部 frozen dataclass，保证状态不可变、可哈希、可序列化）。

这些契约是调度核心与任何 Worker Adapter（Mock / ACP / 真实模型）之间的唯一接口，
因此不依赖任何框架（需求9/11）。

不可变性说明（P0.5 / P0.5.1 强化）：
- 外层 dataclass 使用 @dataclass(frozen=True)，字段不可重新赋值；
- 内部含可变容器的字段（WorkerTaskInput.upstream_results、BoardEvent.data）在
  __post_init__ 中经 freeze_payload 递归冻结为只读映射（外层 MappingProxyType、
  嵌套 list->tuple、set->frozenset），任何写入尝试都会抛 TypeError，嵌套结构也不可被篡改；
- 集合型字段（depends_on / facts / evidence / risks / open_questions）使用 tuple，天然不可变；
- 构造与 from_dict 对关键字段做类型与范围校验，拒绝静默类型转换（需求：反序列化边界）。

P1A-1 变更（证据结构化）：
- `WorkerResult.evidence` 由 `tuple[str, ...]` 升级为 `tuple[EvidenceRef, ...]`（字段名不变）；
  `from_dict` 遇到字符串 evidence 一律 `ValueError`，**不做隐式升级、不接受混合类型**；
- 新增 `WorkerResult.structured_payload`：核心只校验 `schema` / `version`，**不读取任何业务键**，
  并在构造时经 `freeze_payload` 递归冻结；
- 历史自由文本证据的兼容由独立迁移器（`agentboard.research.migrate`）显式完成，
  核心契约层保持严格。
- 副作用（有意为之）：`structured_payload` 冻结为 `MappingProxyType`，因此 `WorkerResult`
  与 `TaskCard` **不可哈希**（与既有的 `WorkerTaskInput.upstream_results`、`BoardEvent.data`
  一致，都是「不可变优先」的取舍）。相等比较 `==` 与 `to_dict()/from_dict()` 序列化均不受影响；
  现有代码从不把它们用作 dict key 或 set 元素。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import PAYLOAD_SCHEMA_PATTERN, TASK_ID_PATTERN, VALID_DECISION_ACTIONS, VALID_STATUSES
from .evidence import EvidenceRef
from .util import freeze_payload, thaw_payload


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串，实际为 {type(value).__name__}")
    return value


def _require_str_nonempty(value: Any, label: str) -> str:
    text = _require_str(value, label)
    if not text.strip():
        raise ValueError(f"{label} 不能是空字符串")
    return text


def _opt_str(raw: dict[str, Any], key: str, default: str = "") -> str:
    """可选描述字段（P0.5.1 强化）：缺失或非 None 时用默认值；存在但类型错误抛 ValueError。

    规则：字段缺失 -> 默认值；字段为 None -> 默认值；字段存在且非 str -> ValueError。
    不再把非法类型静默转成空串。
    """
    if key not in raw:
        return default
    v = raw[key]
    if v is None:
        return default
    if not isinstance(v, str):
        raise ValueError(f"{key} 字段存在但类型错误，期望 str，实际为 {type(v).__name__}")
    return v


def _freeze_structured_payload(value: Any) -> Mapping[str, Any]:
    """校验并递归冻结 `WorkerResult.structured_payload`（P1A-1）。

    核心层只校验下面两件事，**其余键一律不解释、不读取、不校验**——业务语义由领域层
    （`agentboard.research.payload`）负责，核心对研究领域保持零知识。

    1. 必须是 `Mapping`；
    2. 非空时必须含 `schema`，形态 `<namespace>.v<major>`（如 `research.findings.v1`）；
       可选的 `version` 若存在，必须是 `>=1` 的整数（拒绝 bool）。

    空 Mapping 合法，表示"本次没有结构化扩展产出"。
    """
    if not isinstance(value, Mapping):
        raise ValueError("WorkerResult.structured_payload 必须是 Mapping")
    if not value:
        return freeze_payload({})
    schema = value.get("schema")
    if not isinstance(schema, str) or not PAYLOAD_SCHEMA_PATTERN.match(schema):
        raise ValueError(
            "WorkerResult.structured_payload 必须含 schema 字段，形态 <namespace>.v<major>"
            f"（例如 research.findings.v1），实际为 {schema!r}"
        )
    if "version" in value:
        version = value["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError(
                "WorkerResult.structured_payload.version 必须是 >=1 的整数（禁止 bool），"
                f"实际为 {version!r}"
            )
    return freeze_payload(dict(value))


@dataclass(frozen=True)
class ProjectBrief:
    """主 Agent 收到的原始业务输入（技术选型目标）。"""

    text: str
    source: str

    def __post_init__(self) -> None:
        _require_str_nonempty(self.text, "ProjectBrief.text")
        _require_str_nonempty(self.source, "ProjectBrief.source")

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "text": self.text}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectBrief:
        if not isinstance(raw, dict):
            raise ValueError("ProjectBrief.from_dict 需要 dict")
        return cls(
            text=_require_str_nonempty(raw.get("text"), "ProjectBrief.text"),
            source=_require_str_nonempty(raw.get("source"), "ProjectBrief.source"),
        )


@dataclass(frozen=True)
class TaskCard:
    """公共任务板上的一张卡片。状态机围绕它演进。"""

    task_id: str
    title: str
    assigned_agent: str
    goal: str
    context: str
    depends_on: tuple[str, ...] = ()
    status: str = "todo"
    result: WorkerResult | None = None
    error: str = ""
    session_id: str = ""
    attempts: int = 0

    def __post_init__(self) -> None:
        _require_str_nonempty(self.task_id, "TaskCard.task_id")
        if not TASK_ID_PATTERN.match(self.task_id):
            raise ValueError(f"TaskCard.task_id 必须是 lower_snake_case，实际为 {self.task_id!r}")
        _require_str_nonempty(self.assigned_agent, "TaskCard.assigned_agent")
        _require_str(self.title, "TaskCard.title")
        _require_str(self.goal, "TaskCard.goal")
        _require_str(self.context, "TaskCard.context")
        if not isinstance(self.depends_on, tuple):
            raise ValueError("TaskCard.depends_on 必须是 tuple[str, ...]")
        seen: set[str] = set()
        for dep in self.depends_on:
            if not isinstance(dep, str):
                raise ValueError("TaskCard.depends_on 的元素必须是字符串")
            if dep in seen:
                raise ValueError(f"TaskCard.depends_on 含重复项:{dep}")
            seen.add(dep)
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"TaskCard.status 必须属于 {sorted(VALID_STATUSES)}，实际为 {self.status!r}"
            )
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) or self.attempts < 0:
            raise ValueError(f"TaskCard.attempts 必须是非负整数，实际为 {self.attempts!r}")
        if self.result is not None and not isinstance(self.result, WorkerResult):
            raise ValueError("TaskCard.result 必须是 WorkerResult 或 None")
        _require_str(self.error, "TaskCard.error")
        _require_str(self.session_id, "TaskCard.session_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "assigned_agent": self.assigned_agent,
            "goal": self.goal,
            "context": self.context,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "error": self.error,
            "session_id": self.session_id,
            "attempts": self.attempts,
            "result": self.result.to_dict() if self.result is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskCard:
        if not isinstance(raw, dict):
            raise ValueError("TaskCard.from_dict 需要 dict")
        title = raw.get("title")
        assigned_agent = raw.get("assigned_agent")
        goal = raw.get("goal")
        context = raw.get("context")
        status = raw.get("status", "todo")
        error = raw.get("error")
        session_id = raw.get("session_id")
        attempts = raw.get("attempts", 0)
        deps = raw.get("depends_on", [])

        if not isinstance(title, str):
            raise ValueError("TaskCard.title 必须是字符串")
        if not isinstance(assigned_agent, str):
            raise ValueError("TaskCard.assigned_agent 必须是字符串")
        if not isinstance(goal, str):
            raise ValueError("TaskCard.goal 必须是字符串")
        if not isinstance(context, str):
            raise ValueError("TaskCard.context 必须是字符串")
        if not isinstance(status, str):
            raise ValueError("TaskCard.status 必须是字符串")
        if not isinstance(error, str):
            raise ValueError("TaskCard.error 必须是字符串")
        if not isinstance(session_id, str):
            raise ValueError("TaskCard.session_id 必须是字符串")
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise ValueError("TaskCard.attempts 必须是整数")
        if not isinstance(deps, (list, tuple)):
            raise ValueError("TaskCard.depends_on 必须是 list/tuple")

        # depends_on 每个元素必须是字符串，拒绝静默转换（P0.5.1）
        out_deps: list[str] = []
        for d in deps:
            if not isinstance(d, str):
                raise ValueError(f"TaskCard.depends_on 元素必须是字符串，收到 {type(d).__name__}")
            s = d.strip()
            if s:
                out_deps.append(s)
        depends_on = tuple(out_deps)

        # result 缺失/None -> None；存在则必须是 dict；非 dict/非 None 一律拒绝（P0.5.1）
        result_raw = raw.get("result")
        if result_raw is None:
            result = None
        elif isinstance(result_raw, dict):
            result = WorkerResult.from_dict(result_raw)
        else:
            raise ValueError("TaskCard.result 必须是 dict 或 None")

        return cls(
            task_id=_require_str_nonempty(raw.get("task_id"), "TaskCard.task_id"),
            title=title,
            assigned_agent=_require_str_nonempty(assigned_agent, "TaskCard.assigned_agent"),
            goal=goal,
            context=context,
            depends_on=depends_on,
            status=status,
            result=result,
            error=error,
            session_id=session_id,
            attempts=attempts,
        )


@dataclass(frozen=True)
class WorkerTaskInput:
    """派发给单个 Worker 的私有任务包。

    需求3：子 Agent 只能接收自己的任务包字段 + 显式声明的上游结果。
    upstream_results 的键是被允许的上游 task_id，值是对应的结构化 WorkerResult。
    upstream_results 在 __post_init__ 中经 freeze_payload 递归冻结为只读映射，
    Worker 侧无法篡改，且嵌套 payload 也不可变（隔离/事件可回放需要）。
    """

    task_id: str
    title: str
    assigned_agent: str
    goal: str
    context: str
    upstream_results: Mapping[str, WorkerResult]
    acceptance: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # 上游结果键/值边界校验 + 递归冻结为只读映射，杜绝 Worker 侧篡改输入（需求3 / 隔离）
        ups = self.upstream_results
        if not isinstance(ups, Mapping):
            raise ValueError("WorkerTaskInput.upstream_results 必须是 Mapping")
        for k, v in ups.items():
            if not isinstance(k, str):
                raise ValueError("WorkerTaskInput.upstream_results 的键必须是字符串")
            if not isinstance(v, WorkerResult):
                raise ValueError("WorkerTaskInput.upstream_results 的值必须是 WorkerResult")
        object.__setattr__(self, "upstream_results", freeze_payload(dict(ups)))
        _require_str_nonempty(self.task_id, "WorkerTaskInput.task_id")
        _require_str(self.title, "WorkerTaskInput.title")
        _require_str_nonempty(self.assigned_agent, "WorkerTaskInput.assigned_agent")
        _require_str(self.goal, "WorkerTaskInput.goal")
        _require_str(self.context, "WorkerTaskInput.context")
        if not isinstance(self.acceptance, tuple):
            raise ValueError("WorkerTaskInput.acceptance 必须是 tuple[str, ...]")
        if not isinstance(self.forbidden, tuple):
            raise ValueError("WorkerTaskInput.forbidden 必须是 tuple[str, ...]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "assigned_agent": self.assigned_agent,
            "goal": self.goal,
            "context": self.context,
            "upstream_results": {
                k: v.to_dict() for k, v in self.upstream_results.items()
            },
            "acceptance": list(self.acceptance),
            "forbidden": list(self.forbidden),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkerTaskInput:
        if not isinstance(raw, dict):
            raise ValueError("WorkerTaskInput.from_dict 需要 dict")
        ups = raw.get("upstream_results", {})
        acc = raw.get("acceptance", [])
        forbid = raw.get("forbidden", [])
        if not isinstance(ups, dict):
            raise ValueError("WorkerTaskInput.upstream_results 必须是 dict")
        if not isinstance(acc, (list, tuple)):
            raise ValueError("WorkerTaskInput.acceptance 必须是 list/tuple")
        if not isinstance(forbid, (list, tuple)):
            raise ValueError("WorkerTaskInput.forbidden 必须是 list/tuple")

        def _str_tuple(key: str) -> tuple[str, ...]:
            vals = raw.get(key, [])
            out: list[str] = []
            for x in vals:
                if not isinstance(x, str):
                    raise ValueError(
                        f"WorkerTaskInput.{key} 元素必须是字符串，收到 {type(x).__name__}"
                    )
                s = x.strip()
                if s:
                    out.append(s)
            return tuple(out)

        return cls(
            task_id=_require_str_nonempty(raw.get("task_id"), "WorkerTaskInput.task_id"),
            title=_require_str(raw.get("title"), "WorkerTaskInput.title"),
            assigned_agent=_require_str_nonempty(
                raw.get("assigned_agent"), "WorkerTaskInput.assigned_agent"
            ),
            goal=_require_str(raw.get("goal"), "WorkerTaskInput.goal"),
            context=_require_str(raw.get("context"), "WorkerTaskInput.context"),
            upstream_results={k: WorkerResult.from_dict(v) for k, v in ups.items()},
            acceptance=_str_tuple("acceptance"),
            forbidden=_str_tuple("forbidden"),
        )


@dataclass(frozen=True)
class WorkerResult:
    """Worker 返回的结构化结果（需求4/5）。

    不再是自由文本，而是带事实、证据、风险、未解决问题与置信度的结构化产出，
    便于主 Agent 回收、下游引用与自动评测。

    P1A-1：
    - `evidence` 是 `tuple[EvidenceRef, ...]`，每条证据都指向可核验的来源位置；
    - `structured_payload` 是领域层结构化产出的**唯一扩展槽**（带 schema 版本号）；
      调度核心不解释它的业务语义，只校验 `schema` / `version` 并冻结。
    """

    task_id: str
    agent_name: str
    facts: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    risks: tuple[str, ...]
    open_questions: tuple[str, ...]
    confidence: float
    summary: str = ""
    session_id: str = ""
    raw_text: str = ""
    structured_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_str_nonempty(self.task_id, "WorkerResult.task_id")
        _require_str_nonempty(self.agent_name, "WorkerResult.agent_name")
        for field_name in ("facts", "risks", "open_questions"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                raise ValueError(f"WorkerResult.{field_name} 必须是 tuple[str, ...]")
            for item in value:
                if not isinstance(item, str):
                    raise ValueError(f"WorkerResult.{field_name} 的元素必须是字符串")
        if not isinstance(self.evidence, tuple):
            raise ValueError("WorkerResult.evidence 必须是 tuple[EvidenceRef, ...]")
        # 证据必须是结构化 EvidenceRef；字符串在升级后一律拒绝（不做隐式兼容）
        for item in self.evidence:
            if not isinstance(item, EvidenceRef):
                raise ValueError(
                    "WorkerResult.evidence 的元素必须是 EvidenceRef，"
                    f"实际为 {type(item).__name__}（字符串证据需经 research.migrate 显式迁移）"
                )
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
            raise ValueError(
                f"WorkerResult.confidence 必须是数值，实际为 {type(self.confidence).__name__}"
            )
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(f"WorkerResult.confidence 必须在 [0,1]，实际为 {self.confidence!r}")
        _require_str(self.summary, "WorkerResult.summary")
        _require_str(self.session_id, "WorkerResult.session_id")
        _require_str(self.raw_text, "WorkerResult.raw_text")
        object.__setattr__(
            self, "structured_payload", _freeze_structured_payload(self.structured_payload)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "facts": list(self.facts),
            "evidence": [item.to_dict() for item in self.evidence],
            "risks": list(self.risks),
            "open_questions": list(self.open_questions),
            "confidence": self.confidence,
            "summary": self.summary,
            "session_id": self.session_id,
            "raw_text": self.raw_text,
            "structured_payload": thaw_payload(self.structured_payload),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkerResult:
        if not isinstance(raw, dict):
            raise ValueError("WorkerResult.from_dict 需要 dict")

        def _tup(key: str) -> tuple[str, ...]:
            v = raw.get(key, [])
            if not isinstance(v, (list, tuple)):
                raise ValueError(f"WorkerResult.{key} 必须是 list/tuple")
            out: list[str] = []
            for x in v:
                if not isinstance(x, str):
                    raise ValueError(f"WorkerResult.{key} 的元素必须是字符串")
                out.append(x)
            return tuple(out)

        raw_evidence = raw.get("evidence", [])
        if not isinstance(raw_evidence, (list, tuple)):
            raise ValueError("WorkerResult.evidence 必须是 list/tuple")
        evidence: list[EvidenceRef] = []
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, dict):
                raise ValueError(
                    f"WorkerResult.evidence[{index}] 必须是 dict（EvidenceRef），"
                    f"实际为 {type(item).__name__}；字符串证据请先经 research.migrate 迁移"
                )
            evidence.append(EvidenceRef.from_dict(item))

        raw_payload = raw.get("structured_payload", {})
        if not isinstance(raw_payload, dict):
            raise ValueError("WorkerResult.structured_payload 必须是 dict")

        return cls(
            task_id=_require_str_nonempty(raw.get("task_id"), "WorkerResult.task_id"),
            agent_name=_require_str_nonempty(raw.get("agent_name"), "WorkerResult.agent_name"),
            facts=_tup("facts"),
            evidence=tuple(evidence),
            risks=_tup("risks"),
            open_questions=_tup("open_questions"),
            confidence=raw.get("confidence", 0.0),
            summary=_opt_str(raw, "summary"),
            session_id=_opt_str(raw, "session_id"),
            raw_text=_opt_str(raw, "raw_text"),
            structured_payload=raw_payload,
        )


@dataclass(frozen=True)
class BoardDecision:
    """调度核心对当前任务板做出的决策（P0.5.1：action 限定白名单）。"""

    action: str  # dispatch | retry | finish | blocked
    task_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        _require_str_nonempty(self.action, "BoardDecision.action")
        if self.action not in VALID_DECISION_ACTIONS:
            raise ValueError(
                f"BoardDecision.action 必须是 {sorted(VALID_DECISION_ACTIONS)}，实际为 {self.action!r}"
            )
        _require_str(self.task_id, "BoardDecision.task_id")
        _require_str(self.reason, "BoardDecision.reason")

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "task_id": self.task_id, "reason": self.reason}

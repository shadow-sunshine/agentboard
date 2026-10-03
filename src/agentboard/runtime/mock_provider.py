"""确定性离线检索 Provider（P3 测试与评测的默认替身）。

定位与先例
----------
P2.1 把 `MockTransport` 放在测试里；P3 的运行时与官方评测**都需要**一个
"整只 Provider 级"的离线替身（检索 + 失败脚本 + 计数），且两者必须用同一份，
否则"测试过的行为"与"评测的行为"会漂移。src 里放确定性占位实现有直接先例
（`workers/synthetic.py` 的 `SyntheticEvidenceFactory`）—— 本模块遵循同一先例。

它实现 `Provider` 协议（provider_id + search），**零网络、零时钟**：
- `documents`：`查询词 → 该查询应返回的文档`（确定性排序）；
- `failures`：`查询词 → 按调用次序依次抛出的异常`（用完异常后开始正常返回）——
  这就是"重试"语义的可复现形态；
- `calls` 计数只增不减，供测试 / 评测断言（状态是**记录**，不是随机源）。

红线：它**只产出 `RetrievedDocument`**，与 `EvidenceRef` 无任何关联 ——
Provider 产物成为证据的唯一路径是"快照落盘 → 语料构建 → 受控工厂"（见 corpus.py）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..providers.interface import RetrievedDocument, SearchQuery

PROVIDER_ID = "scripted.search.v1"


@dataclass(frozen=True, slots=True)
class ScriptedSearchProvider:
    """确定性离线 Provider：按脚本返回文档、按脚本抛错、按调用次序计数。"""

    documents: Mapping[str, Sequence[RetrievedDocument]]
    captured_at: str
    #: 查询词（归一化后）→ 按调用次序依次抛出的异常；队列耗尽后正常返回文档。
    failures: Mapping[str, Sequence[Exception]] = field(default_factory=dict)
    provider_id: str = PROVIDER_ID
    #: 每个查询词的调用次数（记录；供测试 / 评测断言重试与预算行为）。
    calls: dict[str, int] = field(default_factory=dict)
    #: 每次调用收到的 `timeout_seconds`（按调用次序记录；供测试断言
    #: 运行时确实把预算的超时配置传到了 Provider——不是文档声明）。
    timeouts: list[float | None] = field(default_factory=list)

    def search(
        self,
        query: SearchQuery,
        *,
        captured_at: str,
        timeout_seconds: float | None = None,
    ) -> Sequence[RetrievedDocument]:
        terms = query.normalized_terms
        self.calls[terms] = self.calls.get(terms, 0) + 1
        self.timeouts.append(timeout_seconds)
        failures_for_terms = self.failures.get(terms, ())
        if self.calls[terms] <= len(failures_for_terms):
            raise failures_for_terms[self.calls[terms] - 1]
        return tuple(self.documents.get(terms, ()))

    def call_count(self, terms: str) -> int:
        return self.calls.get(terms, 0)


def scripted_document(
    *,
    terms: str,
    candidate_id: str,
    index: int,
    body: str,
    captured_at: str,
    repo: str = "example/repo",
) -> RetrievedDocument:
    """构造一份确定性的离线文档（URL / 哈希自洽）。

    内容 = 场景给定的正文；`source_url` 是规范 https URL；
    sha256 由 `RetrievedDocument.build` 现算 —— 与真实路径同一入口。
    """
    slug = terms.replace(" ", "-")
    return RetrievedDocument.build(
        source_id=f"{PROVIDER_ID}:result:{candidate_id}:{index}",
        query_terms=terms,
        source_url=f"https://github.com/{repo}/blob/main/{slug}-{index}.md",
        title=f"{candidate_id} · {terms} · 文档 {index}",
        captured_at=captured_at,
        content=body,
    )

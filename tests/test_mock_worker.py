"""Mock Worker 单测（需求8/11）：确定性、可配置失败、结构化输出。

P1A-1 追加：证据必须是结构化 `EvidenceRef`，由 `SyntheticEvidenceFactory` 构造，
且**不受失败重试影响**。
"""

from __future__ import annotations

import asyncio

import pytest

from agentboard.contracts import WorkerResult, WorkerTaskInput
from agentboard.evidence import (
    SOURCE_SYNTHETIC,
    SYNTHETIC_SNAPSHOT_VERSION,
    EvidenceRef,
    is_trusted_evidence,
)
from agentboard.workers import MockWorker
from agentboard.workers.synthetic import FIXED_CAPTURED_AT


def _input(task_id: str = "t1") -> WorkerTaskInput:
    return WorkerTaskInput(
        task_id=task_id,
        title="t",
        assigned_agent="research",
        goal="调研某方案",
        context="c",
        upstream_results={},
    )


def test_mock_worker_returns_structured_result():
    worker = MockWorker("research")
    r: WorkerResult = asyncio.run(worker.run(_input()))
    assert isinstance(r, WorkerResult)
    assert r.agent_name == "research"
    assert len(r.facts) >= 1
    assert len(r.evidence) >= 1
    assert len(r.risks) >= 1
    assert len(r.open_questions) >= 1
    assert 0.0 <= r.confidence <= 1.0


def test_mock_worker_respects_confidence():
    worker = MockWorker("research", confidence=0.42)
    r = asyncio.run(worker.run(_input()))
    assert r.confidence == 0.42


def test_mock_worker_fail_times_then_succeeds():
    worker = MockWorker("research", fail_times=1)
    with pytest.raises(RuntimeError):
        asyncio.run(worker.run(_input()))
    r = asyncio.run(worker.run(_input()))
    assert isinstance(r, WorkerResult)
    assert worker._calls == 2


def test_mock_worker_records_received_input():
    worker = MockWorker("research")
    inp = _input("abc")
    asyncio.run(worker.run(inp))
    assert worker.received_inputs[0].task_id == "abc"


# ---- P1A-1：结构化合成证据 ----


def test_mock_worker_emits_synthetic_evidence_ref():
    r = asyncio.run(MockWorker("research").run(_input("t1")))
    assert len(r.evidence) == 1
    ref = r.evidence[0]
    assert isinstance(ref, EvidenceRef)
    assert ref.source_type == SOURCE_SYNTHETIC
    assert ref.source_id == "mock:research"
    assert ref.snapshot_version == SYNTHETIC_SNAPSHOT_VERSION == "synthetic.1"
    assert ref.locator == "/synthetic/t1/0"
    assert ref.captured_at == FIXED_CAPTURED_AT


def test_synthetic_evidence_is_not_trusted():
    """synthetic 证据只是确定性占位，不得被当作真实资料快照。"""
    ref = asyncio.run(MockWorker("research").run(_input("t1"))).evidence[0]
    assert is_trusted_evidence(ref) is False


def test_mock_worker_evidence_is_immune_to_fail_times():
    """证据 locator 基于 task_id 而非 _calls：重试不应改变证据。

    若 locator 误用 `self._calls`，本用例会失败 —— 这是"注入失败就崩"的经典陷阱。
    """
    flaky = MockWorker("research", fail_times=1)
    with pytest.raises(RuntimeError):
        asyncio.run(flaky.run(_input("t1")))
    after_retry = asyncio.run(flaky.run(_input("t1")))
    assert flaky._calls == 2

    baseline = asyncio.run(MockWorker("research").run(_input("t1")))

    assert after_retry.evidence == baseline.evidence
    assert [ref.evidence_id for ref in after_retry.evidence] == [
        ref.evidence_id for ref in baseline.evidence
    ]


def test_mock_worker_evidence_is_deterministic_across_instances():
    first = asyncio.run(MockWorker("research").run(_input("t1")))
    second = asyncio.run(MockWorker("research").run(_input("t1")))
    assert first.evidence == second.evidence
    assert first.to_dict() == second.to_dict()

"""确定性打分与排名（P1A-3）。"""

from __future__ import annotations

import pytest

from agentboard.research.config import UNSUPPORTED_VALUE
from agentboard.research.errors import ResearchValueError
from agentboard.research.models import ComparisonDimension, ResearchFinding
from agentboard.research.scoring import (
    CandidateScore,
    render_rationale,
    score_all,
    score_candidate,
    score_gap,
)
from agentboard.workers.synthetic import SyntheticEvidenceFactory


def _synthetic_finding(candidate_id: str, dimension_id: str, value: str = "low") -> ResearchFinding:
    return ResearchFinding(
        candidate_id=candidate_id,
        task_id=f"research__{candidate_id}",
        agent_name="research",
        dimension_id=dimension_id,
        claim=f"{dimension_id} 的依据",
        value=value,
        status="supported",
        evidence_refs=SyntheticEvidenceFactory.for_task("research", f"research__{candidate_id}", count=1),
        confidence=1.0,
    )


def _unsupported_finding(candidate_id: str, dimension_id: str) -> ResearchFinding:
    return ResearchFinding(
        candidate_id=candidate_id,
        task_id=f"research__{candidate_id}",
        agent_name="research",
        dimension_id=dimension_id,
        claim=f"{dimension_id} 缺少依据",
        value=UNSUPPORTED_VALUE,
        status="unsupported",
        evidence_refs=(),
        confidence=0.0,
    )


def _dimension(
    dimension_id: str,
    weight: float,
    labels: tuple[str, ...] = ("low", "high"),
    utilities: tuple[float, ...] = (1.0, 0.0),
    *,
    kind: str = "ordinal",
) -> ComparisonDimension:
    return ComparisonDimension(
        dimension_id=dimension_id,
        name=dimension_id,
        description="测试维度",
        weight=weight,
        scale_kind=kind,
        scale_labels=labels,
        utility_map=dict(zip(labels, utilities, strict=True)),
    )


def test_score_is_weight_times_utility_sum() -> None:
    first = _dimension("a_dimension", 0.6)
    second = _dimension("b_dimension", 0.4)
    findings = {"a_dimension": _synthetic_finding("alpha", "a_dimension", "low")}
    score = score_candidate(candidate_id="alpha", dimensions=[first, second], findings_by_dimension=findings)
    # a: 0.6 * 1.0 = 0.6；b 缺失 -> 0.0
    assert score.score == pytest.approx(0.6)
    assert score.matched_dimensions == ("a_dimension",)
    assert score.missing_dimensions == ("b_dimension",)


def test_unsupported_counts_as_zero_and_is_listed_as_missing() -> None:
    dimension = _dimension("a_dimension", 1.0)
    findings = {"a_dimension": _unsupported_finding("alpha", "a_dimension")}
    score = score_candidate(candidate_id="alpha", dimensions=[dimension], findings_by_dimension=findings)
    assert score.score == 0.0
    assert score.contributions["a_dimension"] == 0.0
    assert score.missing_dimensions == ("a_dimension",)
    assert score.coverage_ratio == 0.0


def test_missing_dimension_never_silently_dropped() -> None:
    """缺失维度按 0 分参与求和，而不是从分母里剔除（剔除会让缺数据变成优势）。"""
    dimension = _dimension("a_dimension", 1.0)
    score = score_candidate(candidate_id="alpha", dimensions=[dimension], findings_by_dimension={})
    assert score.score == 0.0
    assert set(score.contributions) == {"a_dimension"}


def test_contributions_cover_matched_and_missing() -> None:
    dimensions = [_dimension("a_dimension", 0.5), _dimension("b_dimension", 0.5)]
    findings = {"a_dimension": _synthetic_finding("alpha", "a_dimension")}
    score = score_candidate(candidate_id="alpha", dimensions=dimensions, findings_by_dimension=findings)
    assert set(score.contributions) == set(score.matched_dimensions) | set(score.missing_dimensions)


def test_score_is_independent_of_dimension_iteration_order() -> None:
    dimensions = [_dimension("a_dimension", 0.3), _dimension("b_dimension", 0.7)]
    findings = {
        "a_dimension": _synthetic_finding("alpha", "a_dimension", "low"),
        "b_dimension": _synthetic_finding("alpha", "b_dimension", "high"),
    }
    forward = score_candidate(candidate_id="alpha", dimensions=dimensions, findings_by_dimension=findings)
    backward = score_candidate(
        candidate_id="alpha", dimensions=list(reversed(dimensions)), findings_by_dimension=findings
    )
    assert forward == backward


def test_unsupported_low_weight_still_zeroes_contribution() -> None:
    dimensions = [_dimension("a_dimension", 0.9), _dimension("b_dimension", 0.1)]
    findings = {"a_dimension": _synthetic_finding("alpha", "a_dimension", "low")}
    score = score_candidate(candidate_id="alpha", dimensions=dimensions, findings_by_dimension=findings)
    assert score.score == pytest.approx(0.9)


# ---- 排名 ----


def test_score_all_ranks_by_score_desc_then_candidate_id() -> None:
    dimensions = [_dimension("a_dimension", 1.0)]
    findings = [
        _synthetic_finding("alpha", "a_dimension", "high"),
        _synthetic_finding("beta", "a_dimension", "low"),
        _synthetic_finding("gamma", "a_dimension", "low"),
    ]
    scores = score_all(candidate_ids=["beta", "gamma", "alpha"], dimensions=dimensions, findings=findings)
    assert [item.candidate_id for item in scores] == ["beta", "gamma", "alpha"]
    assert scores[0].score == 1.0


def test_tie_break_is_candidate_id_lexicographic() -> None:
    """同分时用 candidate_id 字典序，不做任何随机或"任意顺序"。"""
    dimensions = [_dimension("a_dimension", 1.0)]
    findings = [
        _synthetic_finding("zeta", "a_dimension", "low"),
        _synthetic_finding("alpha", "a_dimension", "low"),
        _synthetic_finding("mid", "a_dimension", "low"),
    ]
    scores = score_all(candidate_ids=["zeta", "alpha", "mid"], dimensions=dimensions, findings=findings)
    assert [item.candidate_id for item in scores] == ["alpha", "mid", "zeta"]
    assert len({item.score for item in scores}) == 1


def test_score_gap() -> None:
    dimensions = [_dimension("a_dimension", 1.0)]
    findings = [
        _synthetic_finding("alpha", "a_dimension", "low"),
        _synthetic_finding("beta", "a_dimension", "high"),
    ]
    scores = score_all(candidate_ids=["alpha", "beta"], dimensions=dimensions, findings=findings)
    assert score_gap(scores) == pytest.approx(1.0)
    assert score_gap(scores[:1]) is None


# ---- 理由渲染 ----


def test_rationale_is_deterministic_and_mentions_gaps() -> None:
    score = CandidateScore(
        candidate_id="alpha",
        score=0.5,
        contributions={"a_dimension": 0.5, "b_dimension": 0.0},
        matched_dimensions=("a_dimension",),
        missing_dimensions=("b_dimension",),
    )
    text = render_rationale(score, 2, trusted_findings=3)
    assert text == render_rationale(score, 2, trusted_findings=3)
    assert "alpha" in text and "排名 2" in text
    assert "1/2" in text and "b_dimension" in text


def test_rationale_marks_full_coverage() -> None:
    score = CandidateScore(
        candidate_id="alpha",
        score=1.0,
        contributions={"a_dimension": 1.0},
        matched_dimensions=("a_dimension",),
        missing_dimensions=(),
    )
    assert "维度覆盖完整" in render_rationale(score, 1, trusted_findings=1)


# ---- 局部校验 ----


def test_candidate_score_rejects_inconsistent_contributions() -> None:
    with pytest.raises(ResearchValueError, match="matched ∪ missing"):
        CandidateScore(
            candidate_id="alpha",
            score=1.0,
            contributions={"a_dimension": 1.0},
            matched_dimensions=(),
            missing_dimensions=(),
        )


def test_candidate_score_rejects_unsorted_dimensions() -> None:
    with pytest.raises(ResearchValueError, match="字典序"):
        CandidateScore(
            candidate_id="alpha",
            score=1.0,
            contributions={"a_dimension": 0.5, "b_dimension": 0.5},
            matched_dimensions=("b_dimension", "a_dimension"),
            missing_dimensions=(),
        )


@pytest.mark.parametrize("bad_score", [-0.1, float("inf"), float("nan"), True])
def test_candidate_score_rejects_bad_score(bad_score: object) -> None:
    with pytest.raises(ResearchValueError):
        CandidateScore(
            candidate_id="alpha",
            score=bad_score,
            contributions={},
            matched_dimensions=(),
            missing_dimensions=(),
        )


# ---- 真实数据 ----


def test_real_report_scores_match_hand_computation(report) -> None:
    """真实 fixture 上的分数逐条对得上手算结果（防止公式悄悄漂移）。"""
    by_candidate = {rec.candidate_id: rec for rec in report.recommendations}
    assert by_candidate["pgvector"].score == pytest.approx(0.80)
    assert by_candidate["qdrant_local"].score == pytest.approx(0.675)
    assert by_candidate["milvus_standalone"].score == pytest.approx(0.50)


def test_real_report_ranking_order(report) -> None:
    assert [(rec.rank, rec.candidate_id) for rec in report.recommendations] == [
        (1, "pgvector"),
        (2, "qdrant_local"),
        (3, "milvus_standalone"),
    ]

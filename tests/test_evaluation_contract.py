"""The evaluation contract ties every claim and the score band to quoted evidence."""

import pytest

from interview_agent.interview.evaluation_contract import validate_evaluation
from interview_agent.interview.models import (
    Assessment,
    CriterionEvaluation,
    EvaluationResult,
    EvidenceRef,
    FeedbackFinding,
    Seniority,
)

RECORDS = [
    {"id": "1", "role": "assistant", "content": "How would you index it?"},
    {"id": "2", "role": "user", "content": "An index on cliente_id, plus a composite one."},
]
MILESTONES = [{"id": "a"}, {"id": "b"}]
QUOTE = EvidenceRef(message_id="2", quote="An index on cliente_id")


def result(score, *assessments, findings=(), gap="Specific missing evidence."):
    return EvaluationResult(
        hired=score >= 70,
        score=score,
        score_gap=gap,
        strengths=[f.text for f in findings if f.kind == "strength"],
        weaknesses=[f.text for f in findings if f.kind == "weakness"],
        rationale="Evidence-based.",
        seniority_evaluated=Seniority.JUNIOR,
        evaluation_status="complete",
        criteria=[
            CriterionEvaluation(
                milestone_id=m["id"], assessment=a, evidence=[QUOTE], rationale="", practice=""
            )
            for m, a in zip(MILESTONES, assessments, strict=True)
        ],
        findings=list(findings),
    )


def validate(evaluation):
    return validate_evaluation(
        evaluation, seniority=Seniority.JUNIOR, milestones=MILESTONES, records=RECORDS
    )


@pytest.mark.parametrize(
    ("score", "assessments"),
    [
        (95, (Assessment.EXCEEDS, Assessment.EXCEEDS)),
        (90, (Assessment.EXCEEDS, Assessment.MEETS)),
        (88, (Assessment.MEETS, Assessment.MEETS)),
        (85, (Assessment.EXCEEDS, Assessment.PARTIAL)),
    ],
)
def test_top_band_follows_per_criterion_evidence(score, assessments):
    assert validate(result(score, *assessments)).coverage == 1.0


@pytest.mark.parametrize(
    "assessments",
    [
        (Assessment.MEETS, Assessment.MEETS),
        (Assessment.EXCEEDS, Assessment.PARTIAL),
        (Assessment.EXCEEDS, Assessment.BELOW),
    ],
)
def test_top_band_without_exceeding_the_bar_is_rejected(assessments):
    with pytest.raises(ValueError, match="90 or more"):
        validate(result(92, *assessments))


def test_exceeding_criterion_cannot_carry_a_weakness():
    weakness = FeedbackFinding(kind="weakness", text="Shallow", milestone_id="a", evidence=[QUOTE])
    with pytest.raises(ValueError, match="meets its bar"):
        validate(result(80, Assessment.EXCEEDS, Assessment.MEETS, findings=[weakness]))


def test_evidence_must_be_a_verbatim_candidate_quote():
    evaluation = result(80, Assessment.MEETS, Assessment.MEETS)
    evaluation.criteria[0].evidence = [EvidenceRef(message_id="1", quote="How would you")]
    with pytest.raises(ValueError, match="verbatim"):
        validate(evaluation)


@pytest.mark.parametrize("score", [98, 88, 75])
def test_points_below_the_top_of_a_band_must_be_explained(score):
    with pytest.raises(ValueError, match="score_gap"):
        validate(result(score, Assessment.EXCEEDS, Assessment.EXCEEDS, gap=" "))


@pytest.mark.parametrize(
    ("score", "assessments"), [(100, Assessment.EXCEEDS), (89, Assessment.MEETS)]
)
def test_the_top_of_a_band_needs_no_gap(score, assessments):
    assert validate(result(score, assessments, assessments, gap="")).score == score

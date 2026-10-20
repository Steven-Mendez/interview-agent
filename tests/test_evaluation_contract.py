"""The evaluation contract ties every claim and the score band to quoted evidence."""

import pytest

from interview_agent.interview.evaluation_contract import validate_evaluation
from interview_agent.interview.models import (
    AnswerRevision,
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


@pytest.mark.parametrize("scope", ["practice", "limited"])
def test_scoped_evaluation_never_becomes_a_global_verdict(scope):
    plan = {
        "preferences": {"objective": "practice" if scope == "practice" else "simulation"},
        "requirements": [{"id": "missing", "competency": "Accessibility", "essential": True}],
        "omitted_requirements": [{"requirement_id": "missing"}],
    }
    evaluation = result(89, Assessment.MEETS, Assessment.MEETS).model_copy(
        update={
            "score": None,
            "hired": None,
            "assessment_scope": scope,
            "uncovered_requirements": ["Accessibility"],
            "evaluation_status": "complete" if scope == "practice" else "partial",
        }
    )
    checked = validate_evaluation(
        evaluation, seniority=Seniority.JUNIOR, milestones=MILESTONES, records=RECORDS, plan=plan
    )
    assert checked.coverage == 1 and checked.hired is None
    with pytest.raises(ValueError, match="scope"):
        validate_evaluation(
            result(89, Assessment.MEETS, Assessment.MEETS),
            seniority=Seniority.JUNIOR,
            milestones=MILESTONES,
            records=RECORDS,
            plan=plan,
        )
    with pytest.raises(ValueError, match="hiring verdict"):
        validate_evaluation(
            evaluation.model_copy(update={"score": 89, "hired": True}),
            seniority=Seniority.JUNIOR,
            milestones=MILESTONES,
            records=RECORDS,
            plan=plan,
        )


def test_practice_completion_includes_selected_nonessential_skills():
    plan = {"preferences": {"objective": "practice"}}
    evaluation = result(89, Assessment.MEETS, Assessment.MEETS).model_copy(
        update={
            "score": None,
            "hired": None,
            "assessment_scope": "practice",
            "evaluation_status": "partial",
        }
    )
    evaluation.criteria[1].assessment = Assessment.NOT_ASSESSABLE
    evaluation.criteria[1].evidence = []
    milestones = [{**item, "essential": False} for item in MILESTONES]
    assert (
        validate_evaluation(
            evaluation,
            seniority=Seniority.JUNIOR,
            milestones=milestones,
            records=RECORDS,
            plan=plan,
        ).coverage
        == 0.5
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


def revision(**changes):
    return AnswerRevision.model_validate(
        {
            "milestone_id": "a",
            "question_id": "1",
            "original": QUOTE.model_dump(),
            "suggested_answer": "I would check the index with EXPLAIN before changing it.",
            "explanation": "EXPLAIN establishes whether the index is actually used.",
            "basis": "knowledge_example",
            "missing_details": [],
            **changes,
        }
    )


def with_revision(**changes):
    ev = result(65, Assessment.PARTIAL, Assessment.MEETS)
    ev.answer_revisions = [revision(**changes)]
    return ev


def test_revision_question_is_filled_from_authoritative_transcript():
    ev = validate(with_revision(question="Invented question"))
    assert ev.answer_revisions[0].question == RECORDS[0]["content"]
    # Coaching does not turn the candidate's partial answer into passing evidence.
    assert ev.criteria[0].assessment == Assessment.PARTIAL
    assert ev.score == 65


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"question_id": "unknown"}, "question before"),
        ({"original": {"message_id": "2", "quote": "Made up answer"}}, "verbatim"),
        ({"original": {"message_id": "2", "message_version": 2, "quote": QUOTE.quote}}, "verbatim"),
        ({"original": {"message_id": "1", "quote": "How would you index it?"}}, "verbatim"),
        ({"milestone_id": "missing"}, "demonstrated gap"),
        ({"milestone_id": "b"}, "demonstrated gap"),
        ({"suggested_answer": QUOTE.quote}, "actual change"),
        ({"suggested_answer": " "}, "alternative"),
    ],
)
def test_invalid_answer_comparisons_are_rejected(changes, match):
    with pytest.raises(ValueError, match=match):
        validate(with_revision(**changes))


def test_revision_must_use_evidence_for_its_criterion():
    ev = with_revision()
    ev.criteria[0].evidence = [EvidenceRef(message_id="3", quote="Different answer")]
    with pytest.raises(ValueError, match="linked criterion"):
        validate_evaluation(
            ev,
            seniority=Seniority.JUNIOR,
            milestones=MILESTONES,
            records=[*RECORDS, {"id": "3", "role": "user", "content": "Different answer"}],
        )


def test_new_evaluations_require_coaching_for_every_gap_but_legacy_results_still_load():
    ev = result(65, Assessment.PARTIAL, Assessment.MEETS)
    assert validate(ev).score == 65
    with pytest.raises(ValueError, match="Every demonstrated gap"):
        validate_evaluation(
            ev,
            seniority=Seniority.JUNIOR,
            milestones=MILESTONES,
            records=RECORDS,
            require_answer_revisions=True,
        )
    assert (
        validate_evaluation(
            with_revision(),
            seniority=Seniority.JUNIOR,
            milestones=MILESTONES,
            records=RECORDS,
            require_answer_revisions=True,
        ).score
        == 65
    )


def test_no_revisions_are_required_when_answers_already_meet_the_bar():
    assert (
        validate_evaluation(
            result(89, Assessment.MEETS, Assessment.MEETS),
            seniority=Seniority.JUNIOR,
            milestones=MILESTONES,
            records=RECORDS,
            require_answer_revisions=True,
        ).answer_revisions
        == []
    )


def test_duplicate_coaching_is_rejected():
    ev = with_revision()
    ev.answer_revisions.append(revision())
    with pytest.raises(ValueError, match="repeat an answer revision"):
        validate(ev)

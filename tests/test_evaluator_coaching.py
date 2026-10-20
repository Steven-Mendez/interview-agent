"""New coaching is validated and repaired without treating it as candidate evidence."""

from unittest.mock import AsyncMock, Mock

from interview_agent.config import Settings
from interview_agent.interview import evaluator
from interview_agent.interview.models import (
    AnswerRevision,
    CriterionEvaluation,
    EvaluationResult,
    EvidenceRef,
    Seniority,
)

RECORDS = [
    {"id": "q1", "role": "assistant", "content": "How would you inspect this query?"},
    {"id": "a1", "role": "user", "version": 1, "content": "I would optimize it."},
]
MILESTONES = [{"id": "sql", "expected_evidence": "Uses EXPLAIN to inspect the plan."}]
REF = EvidenceRef(message_id="a1", message_version=1, quote="I would optimize it.")


def coached_result():
    return EvaluationResult(
        hired=False,
        score=39,
        strengths=[],
        weaknesses=[],
        rationale="No concrete approach.",
        seniority_evaluated=Seniority.JUNIOR,
        criteria=[
            CriterionEvaluation(
                milestone_id="sql",
                assessment="below",
                evidence=[REF],
                rationale="Does not identify EXPLAIN.",
                practice="Inspect a sample plan.",
            )
        ],
        answer_revisions=[
            AnswerRevision(
                milestone_id="sql",
                question_id="q1",
                original=REF,
                suggested_answer="I would use EXPLAIN to see the execution plan.",
                explanation="Learn how to read a plan before using this example.",
                basis="knowledge_example",
                missing_details=[],
            )
        ],
    )


async def evaluate(monkeypatch, outputs, *, transcript=RECORDS):
    structured = Mock(ainvoke=AsyncMock(side_effect=outputs))
    model = Mock(with_structured_output=Mock(return_value=structured))
    factory = Mock(return_value=model)
    close = AsyncMock()
    monkeypatch.setattr(evaluator, "build_chat_model", factory)
    monkeypatch.setattr(evaluator, "close_chat_model", close)
    result = await evaluator.run_evaluator(
        Settings(_env_file=None),
        "Backend junior",
        "Junior SQL role",
        {},
        MILESTONES,
        transcript,
        "plan_complete",
        seniority=Seniority.JUNIOR,
        language="en",
    )
    return result, structured, factory, close


async def test_coaching_prompt_preserves_facts_level_and_original_assessment(monkeypatch):
    result, model, _, close = await evaluate(monkeypatch, [coached_result()])
    system = model.ainvoke.call_args.args[0][0].content
    assert "Never invent work experience" in system
    assert "Do NOT rewrite criteria assessed meets, exceeds or not_assessable" in system
    assert "not new evidence" in system
    assert result.answer_revisions[0].question == RECORDS[0]["content"]
    assert result.criteria[0].assessment == "below"
    close.assert_awaited_once()


async def test_evaluator_repairs_feedback_that_only_lists_a_gap(monkeypatch):
    missing = coached_result()
    missing.answer_revisions = []
    result, model, _, _ = await evaluate(monkeypatch, [missing, coached_result()])
    assert model.ainvoke.await_count == 2
    assert "Every demonstrated gap" in model.ainvoke.call_args.args[0][-1].content
    assert len(result.answer_revisions) == 1


async def test_no_answers_produce_no_rewrites_and_make_no_provider_call(monkeypatch):
    result, _, factory, close = await evaluate(monkeypatch, [], transcript=RECORDS[:1])
    assert result.evaluation_status == "insufficient"
    assert result.score is None and result.hired is None
    assert not result.answer_revisions and not result.weaknesses
    factory.assert_not_called()
    close.assert_not_awaited()

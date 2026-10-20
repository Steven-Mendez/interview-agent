"""Two-stage planning, grounding and budget contracts with scripted model outputs."""

from unittest.mock import AsyncMock, Mock

import pytest

from interview_agent.config import Settings
from interview_agent.interview import planner
from interview_agent.interview.models import (
    InterviewLength,
    InterviewPreferences,
    OmittedRequirement,
    PersonalizedPlan,
    PlannedTopic,
    RoleRequirement,
    RoleRubric,
    Seniority,
)
from interview_agent.interview.planning_contract import evaluation_scope

OFFER = (
    "Junior frontend. Build accessible forms. Diagnose bugs. Explain investigations to colleagues."
)
RESUME = "I built an order form and investigated a missing order."


def rubric():
    return RoleRubric(
        seniority="junior",
        seniority_evidence="Junior frontend",
        requirements=[
            RoleRequirement(
                id="form",
                competency="Accessible forms",
                kind="technical",
                job_quote="Build accessible forms",
                relevance="The role builds forms.",
                expected_evidence=(
                    "Describe labeled inputs and keyboard-accessible submission; "
                    "controlled or uncontrolled inputs are valid."
                ),
                essential=True,
            ),
            RoleRequirement(
                id="bug",
                competency="Debugging",
                kind="technical",
                job_quote="Diagnose bugs",
                relevance="The role diagnoses bugs.",
                expected_evidence="Connect a symptom to a plausible cause and verify a correction.",
                essential=True,
            ),
            RoleRequirement(
                id="explain",
                competency="Communication",
                kind="behavioral",
                job_quote="Explain investigations to colleagues",
                relevance="Communicate findings.",
                expected_evidence=(
                    "Explain what was investigated and what help or next step was communicated."
                ),
                essential=True,
            ),
        ],
    )


def personalized(*, quote="I built an order form", behavioral=False):
    ids = ["explain"] if behavioral else ["form", "bug"]
    return PersonalizedPlan(
        persona="Alex, a supportive recruiter",
        summary="Explore role-relevant skills.",
        focus_areas=ids,
        topics=[
            PlannedTopic(
                requirement_id=key,
                title=key,
                description="Ask about their reasoning.",
                resume_quotes=[quote] if quote else [],
            )
            for key in ids
        ],
        omitted_requirements=[
            OmittedRequirement(
                requirement_id=key,
                reason="interview_type",
                explanation="Outside the selected session type.",
            )
            for key in ("form", "bug", "explain")
            if key not in ids
        ],
    )


class ScriptedModel:
    def __init__(self, *outputs):
        self.outputs = iter(outputs)
        self.calls = []
        self.schema = None

    def with_structured_output(self, schema, method=None):
        assert method == "json_schema"
        self.schema = schema
        return self

    async def ainvoke(self, messages, config=None):
        self.calls.append((self.schema, messages, config))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output


def install(monkeypatch, *outputs):
    model = ScriptedModel(*outputs)
    monkeypatch.setattr(planner, "build_chat_model", Mock(return_value=model))
    monkeypatch.setattr(planner, "close_chat_model", AsyncMock())
    return model


async def plan(**kwargs):
    return await planner.run_planner(
        Settings(_env_file=None), kwargs.pop("resume", RESUME), OFFER, "en", "Alex", **kwargs
    )


async def test_job_only_rubric_then_personalization_with_immutable_bar(monkeypatch):
    model = install(monkeypatch, rubric(), personalized())
    result = await plan(persona="Strict manager", custom_instructions="Prefer my order project")
    first = "\n".join(m.content for m in model.calls[0][1])
    second = "\n".join(m.content for m in model.calls[1][1])
    assert [c[0] for c in model.calls] == [RoleRubric, PersonalizedPlan]
    assert OFFER in first
    for candidate_input in (RESUME, "Strict manager", "Prefer my order project"):
        assert candidate_input not in first
        assert candidate_input in second
    assert result.detected_seniority == Seniority.JUNIOR
    assert result.milestones[0].expected_evidence == rubric().requirements[0].expected_evidence
    assert result.milestones[0].resume_quotes == ["I built an order form"]
    assert evaluation_scope(result.model_dump(mode="json")) == ("limited", ["Communication"])
    planner.close_chat_model.assert_awaited_once()


async def test_new_cv_changes_examples_without_changing_reused_role_rubric(monkeypatch):
    model = install(monkeypatch, personalized(quote="I made a CSV inventory"))
    result = await plan(
        resume="I made a CSV inventory", rubric=rubric(), seniority=Seniority.JUNIOR
    )
    assert len(model.calls) == 1 and model.calls[0][0] is PersonalizedPlan
    assert result.detected_seniority is None
    assert result.requirements == rubric().requirements
    assert result.milestones[0].resume_quotes == ["I made a CSV inventory"]
    assert "uncontrolled inputs are valid" in result.milestones[0].expected_evidence


@pytest.mark.parametrize("invalid", ["job_quote", "seniority", "duplicate_ids"])
async def test_invalid_rubric_is_repaired_before_personalizing(monkeypatch, invalid):
    bad = rubric()
    if invalid == "job_quote":
        bad.requirements[0].job_quote = "Must use React state"
    elif invalid == "seniority":
        bad.seniority = Seniority.SENIOR
    else:
        bad.requirements[1].id = "form"
    model = install(monkeypatch, bad, rubric(), personalized())
    await plan(seniority=Seniority.JUNIOR)
    assert [c[0] for c in model.calls] == [RoleRubric, RoleRubric, PersonalizedPlan]
    assert "validation_error" in model.calls[1][1][-1].content


@pytest.mark.parametrize(
    "invalid", ["cv_quote", "missing_requirement", "duplicate", "wrong_type", "wrong_omission"]
)
async def test_invalid_personalization_is_repaired(monkeypatch, invalid):
    bad = personalized()
    if invalid == "cv_quote":
        bad.topics[0].resume_quotes = ["I led ten engineers"]
    elif invalid == "missing_requirement":
        bad.omitted_requirements = []
    elif invalid == "duplicate":
        bad.topics[1].requirement_id = "form"
    elif invalid == "wrong_type":
        bad = personalized(behavioral=True)
    else:
        bad.omitted_requirements[0].reason = "outside_focus"
    model = install(monkeypatch, rubric(), bad, personalized())
    result = await plan()
    assert result.milestones[0].resume_quotes == ["I built an order form"]
    assert len(model.calls) == 3


async def test_second_invalid_result_fails_and_closes_model(monkeypatch):
    bad = rubric()
    bad.seniority_evidence = "The CV says ten years"
    model = install(monkeypatch, bad, bad)
    with pytest.raises(ValueError, match="seniority must cite"):
        await plan()
    assert len(model.calls) == 2
    planner.close_chat_model.assert_awaited_once()


async def test_non_structured_output_is_rejected(monkeypatch):
    install(monkeypatch, {"not": "a rubric"})
    with pytest.raises(TypeError, match="expected RoleRubric"):
        await plan()


async def test_short_question_budget_requires_explicit_essential_omission(monkeypatch):
    smaller = personalized()
    smaller.topics.pop()
    smaller.omitted_requirements.append(
        OmittedRequirement(
            requirement_id="bug",
            reason="time_budget",
            explanation="One primary question available.",
        )
    )
    model = install(monkeypatch, rubric(), personalized(), smaller)
    result = await plan(question_limit=1)
    assert len(model.calls) == 3
    assert "primary-question limit" in model.calls[-1][1][-1].content
    assert result.budget.question_limit == 1
    assert evaluation_scope(result.model_dump(mode="json")) == (
        "limited",
        ["Communication", "Debugging"],
    )


@pytest.mark.parametrize(
    ("length", "minutes", "followups"), [("short", 8, 1), ("deep", 15, 1), ("standard", 4, 0)]
)
async def test_effective_budgets_are_shared_with_personalization(
    monkeypatch, length, minutes, followups
):
    model = install(monkeypatch, rubric(), personalized())
    result = await plan(
        interview_length=InterviewLength(length), max_minutes=minutes, followup_limit=followups
    )
    assert result.budget.max_minutes == minutes
    assert result.budget.followup_limit == followups
    assert result.budget.question_limit == len(result.milestones)
    system = model.calls[-1][1][0].content
    assert f"follow-ups per topic: {followups}" in system
    assert f"Total time: {minutes} minutes" in system


async def test_behavioral_practice_uses_job_rubric_and_reports_omitted_skills(monkeypatch):
    install(monkeypatch, rubric(), personalized(behavioral=True))
    result = await plan(
        preferences=InterviewPreferences(
            interview_type="behavioral", objective="practice", practice_focus="Asking for help"
        )
    )
    assert result.milestones[0].requirement_id == "explain"
    assert evaluation_scope(result.model_dump(mode="json")) == (
        "practice",
        ["Accessible forms", "Debugging"],
    )


async def test_trace_carries_inputs_but_never_settings(monkeypatch, langsmith_runs):
    install(monkeypatch, rubric(), personalized())
    await planner.run_planner(
        Settings(_env_file=None, OPENAI_API_KEY="CANARY_OPENAI_KEY"),
        RESUME,
        OFFER,
        "en",
        "Alex",
        usage_callback=Mock(),
        telemetry_callback=Mock(),
        langsmith_extra={"metadata": {"thread_id": "synthetic-thread"}},
    )
    (run,) = langsmith_runs()
    assert run["name"] == "planner" and run.get("parent_run_id") is None
    assert run["inputs"]["job_offer"] == OFFER
    assert not {"settings", "usage_callback", "telemetry_callback"} & run["inputs"].keys()
    assert "CANARY_OPENAI_KEY" not in str(langsmith_runs.client.mock_calls)


@pytest.mark.parametrize("objective", ["simulation", "practice"])
async def test_text_only_end_to_end_plan_adaptive_questions_and_scoped_feedback(
    monkeypatch, objective
):
    from interview_agent.interview import evaluator
    from interview_agent.interview.models import EvaluationResult, TurnDecision
    from scripts import interview_text as text

    install(monkeypatch, rubric(), personalized())
    planned = await plan(
        interview_length=InterviewLength.SHORT,
        preferences=InterviewPreferences(objective=objective),
    )
    fixture = {
        "resume": RESUME,
        "job_offer": OFFER,
        "seniority": "junior",
        "language": "en",
        "interview_length": "short",
        "plan": planned.model_dump(mode="json"),
        "milestones": [
            {**m.model_dump(mode="json"), "id": m.requirement_id} for m in planned.milestones
        ],
        "scenarios": {
            "offline": {
                "answers": {
                    "bug": "I reproduced a crash, checked the missing order and verified the fix.",
                    "form": (
                        "I used labeled inputs and keyboard submission. "
                        "The missing order also caused a crash."
                    ),
                }
            }
        },
    }

    def settled(key, message, quote):
        return {
            "milestone_id": key,
            "status": "closed",
            "close_reason": "covered",
            "evidence": [{"message_id": str(message), "message_version": 1, "quote": quote}],
        }

    decisions = [
        TurnDecision(
            action="question",
            target_milestone_id="bug",
            spoken_text="How did you diagnose the crash?",
        ),
        TurnDecision(
            action="advance",
            target_milestone_id="form",
            spoken_text="How did you make the form accessible?",
            updates=[settled("bug", 2, "I reproduced a crash")],
        ),
        TurnDecision(
            action="followup",
            target_milestone_id="bug",
            spoken_text="How did you verify the missing order correction?",
            updates=[
                settled("form", 4, "I used labeled inputs and keyboard submission"),
                {
                    "milestone_id": "bug",
                    "status": "active",
                    "evidence": [
                        {"message_id": "4", "message_version": 1, "quote": "also caused a crash"}
                    ],
                },
            ],
        ),
        TurnDecision(
            action="close",
            spoken_text="",
            close_reason="plan_complete",
            updates=[settled("bug", 6, "verified the fix")],
        ),
    ]
    model = ScriptedModel(*decisions)
    monkeypatch.setattr(text, "build_chat_model", Mock(return_value=model))
    monkeypatch.setattr(text, "close_chat_model", AsyncMock())
    run = await text.interview(Settings(_env_file=None), fixture, "offline")
    assert run["budget"] == planned.budget.model_dump(mode="json")
    assert run["budget"] == {"max_minutes": 8, "question_limit": 2, "followup_limit": 1}
    assert run["milestones"][1]["followups"] == 1
    assert len(run["transcript"]) == 6

    scope = "practice" if objective == "practice" else "limited"
    expected = EvaluationResult(
        score=None,
        hired=None,
        strengths=[],
        weaknesses=[],
        rationale="Selected skills demonstrated.",
        seniority_evaluated="junior",
        assessment_scope=scope,
        uncovered_requirements=["Communication"],
        evaluation_status="complete" if scope == "practice" else "partial",
        criteria=[
            {
                "milestone_id": key,
                "assessment": "meets",
                "rationale": "Demonstrated.",
                "practice": "",
                "evidence": settled(key, message, quote)["evidence"],
            }
            for key, message, quote in [
                ("form", 4, "I used labeled inputs and keyboard submission"),
                ("bug", 6, "verified the fix"),
            ]
        ],
    )
    evaluation_model = ScriptedModel(expected)
    monkeypatch.setattr(evaluator, "build_chat_model", Mock(return_value=evaluation_model))
    monkeypatch.setattr(evaluator, "close_chat_model", AsyncMock())
    evaluated = await text.evaluate(Settings(_env_file=None), run)
    assert evaluated["evaluation"]["coverage"] == 1
    assert evaluated["evaluation"]["hired"] is None
    empty = await text.evaluate(Settings(_env_file=None), {**run, "transcript": []})
    assert empty["evaluation"]["assessment_scope"] == scope
    assert empty["evaluation"]["evaluation_status"] == "insufficient"
    assert len(evaluation_model.calls) == 1

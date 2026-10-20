"""Adaptive ordering must retain evidence and the original question budgets."""

from copy import deepcopy

import pytest

from interview_agent.interview.decision import validate_decision
from interview_agent.interview.models import TurnDecision


def context():
    return {
        "milestones": [
            {
                "id": key,
                "lifecycle": "pending",
                "primary_questions": 0,
                "followups": 0,
                "clarifications": 0,
                "close_reason": None,
            }
            for key in ("form", "bug")
        ],
        "transcript": [
            {
                "id": "new",
                "role": "user",
                "version": 1,
                "content": "The missing order also caused a crash.",
            }
        ],
        "remaining_seconds": 180,
        "primary_questions": 0,
        "question_limit": 2,
        "followup_limit": 1,
        "previous_questions": [],
    }


def revisit():
    state = context()
    state["primary_questions"] = 2
    state["milestones"][0].update(
        lifecycle="closed",
        primary_questions=1,
        close_reason="covered",
        evidence=[{"message_id": "old", "message_version": 1, "quote": "The form worked"}],
    )
    decision = TurnDecision(
        action="followup",
        spoken_text="How did that change the form's behavior?",
        target_milestone_id="form",
        updates=[
            {
                "milestone_id": "form",
                "status": "active",
                "evidence": [
                    {"message_id": "new", "message_version": 1, "quote": "caused a crash"}
                ],
            }
        ],
    )
    return state, decision


def test_context_can_choose_later_topic_without_spending_extra_primary_questions():
    state = context()
    decision = TurnDecision(
        action="question", spoken_text="How did you diagnose the crash?", target_milestone_id="bug"
    )
    validate_decision(decision, state)
    state["primary_questions"] = 2
    with pytest.raises(ValueError, match="budget is exhausted"):
        validate_decision(decision, state)


def test_covered_topic_can_be_revisited_with_fresh_evidence_and_remaining_followup():
    state, decision = revisit()
    validate_decision(decision, state)


@pytest.mark.parametrize(
    "invalid",
    ["declined", "skipped", "unasked", "old_evidence", "no_budget", "no_update", "invented_quote"],
)
def test_revisiting_cannot_escape_safeguards(invalid):
    state, decision = revisit()
    original = state["milestones"][0]
    if invalid == "declined":
        original["close_reason"] = "candidate_declined"
    elif invalid == "skipped":
        original["lifecycle"] = "skipped"
    elif invalid == "unasked":
        original["primary_questions"] = 0
    elif invalid == "old_evidence":
        original["evidence"] = [ref.model_dump() for ref in decision.updates[0].evidence]
    elif invalid == "no_budget":
        original["followups"] = 1
    elif invalid == "no_update":
        decision.updates = []
    else:
        decision.updates[0].evidence[0].quote = "I never said this"
    before = deepcopy(state)
    with pytest.raises(ValueError):
        validate_decision(decision, state)
    assert state == before


async def test_reopened_topic_persists_counters_and_clears_completion(
    postgres_sessionmaker, monkeypatch
):
    from test_dialogue import question, setup_graph

    from interview_agent.interview import db

    decisions = []

    async def answer(context, call):
        return decisions[call - 1]

    graph, ids, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    decisions.append(question(ids[1]))
    await graph.run_turn("opening")
    async with postgres_sessionmaker() as session:
        old = await db.insert_message(
            session,
            graph.conversation_id,
            "user",
            "I fixed a bug.",
            source_id="first",
            metrics={"stt_confirmed": True},
        )
    decisions.append(
        TurnDecision(
            action="advance",
            target_milestone_id=str(ids[0]),
            spoken_text="How did you test it?",
            updates=[
                {
                    "milestone_id": str(ids[1]),
                    "status": "closed",
                    "close_reason": "covered",
                    "evidence": [{"message_id": str(old.id), "quote": old.content}],
                }
            ],
        )
    )
    await graph.run_turn("advance")
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.milestones[1].completed_at is not None
        latest = await db.insert_message(
            session,
            graph.conversation_id,
            "user",
            "The fix caused a regression.",
            source_id="new",
            metrics={"stt_confirmed": True},
        )
    decisions.append(
        TurnDecision(
            action="followup",
            target_milestone_id=str(ids[1]),
            spoken_text="How did you investigate the regression?",
            updates=[
                {
                    "milestone_id": str(ids[1]),
                    "status": "active",
                    "evidence": [{"message_id": str(latest.id), "quote": latest.content}],
                }
            ],
        )
    )
    await graph.run_turn("revisit")
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        target = row.milestones[1]
        assert target.lifecycle == "active" and not target.completed
        assert target.completed_at is None and target.close_reason is None
        assert target.primary_questions == 1 and target.followups == 1
    assert calls[-1]["milestones"][1]["evidence"][0]["message_id"] == str(old.id)

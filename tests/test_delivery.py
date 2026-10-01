"""A persisted question survives crashes without regenerating or repeating answers."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_dialogue import question, setup_graph

from interview_agent.interview import db
from interview_agent.interview.delivery import QuestionDeliveries, request_replay
from interview_agent.interview.transcription import admit_candidate
from interview_agent.interview.workers import WorkerCoordinator


async def prepared(sessions, monkeypatch):
    async def answer(context, call):
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(sessions, monkeypatch, answer)
    await graph.run_turn("opening")
    return graph, calls


async def test_crash_after_decision_recovers_same_question_once_without_model(
    postgres_sessionmaker, monkeypatch
):
    graph, calls = await prepared(postgres_sessionmaker, monkeypatch)
    replacement = QuestionDeliveries(postgres_sessionmaker, graph.conversation_id)
    first, second = await asyncio.gather(replacement.claim(), replacement.claim())
    assert sum(speech is not None for speech in (first, second)) == 1
    speech = first or second
    assert speech.text == "How do parameterized SQL queries work?"
    assert await replacement.claim() is None
    assert len(calls) == 1
    async with postgres_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 1
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 1
        row = await db.get_conversation(session, graph.conversation_id)
        assert sum(m.primary_questions for m in row.milestones) == 1


async def test_uncertain_audio_replays_only_on_idempotent_explicit_request(
    postgres_sessionmaker, monkeypatch
):
    graph, calls = await prepared(postgres_sessionmaker, monkeypatch)
    deliveries = graph.deliveries
    first = await deliveries.claim()
    assert first and await deliveries.claim() is None
    async with postgres_sessionmaker() as session:
        question_row = await session.scalar(select(db.QuestionDelivery))
        request_id = uuid.uuid4()
        assert (
            await request_replay(session, graph.conversation_id, question_row.id, request_id)
            == request_id
        )
        assert (
            await request_replay(session, graph.conversation_id, question_row.id, request_id)
            == request_id
        )
    second = await deliveries.claim()
    assert second.attempt_id == request_id and second.text == first.text
    assert await deliveries.claim() is None and len(calls) == 1
    async with postgres_sessionmaker() as session:
        assert (
            await request_replay(session, graph.conversation_id, question_row.id, request_id)
            == request_id
        )
    assert await deliveries.claim() is None


@pytest.mark.parametrize("confirmation", [True, False, "true", 1])
async def test_new_confirmed_answer_suppresses_replay(
    postgres_sessionmaker, monkeypatch, confirmation
):
    graph, _ = await prepared(postgres_sessionmaker, monkeypatch)
    first = await graph.deliveries.claim()
    async with postgres_sessionmaker() as session:
        await admit_candidate(
            session,
            graph.conversation_id,
            content="Candidate answer",
            source_id="new-answer",
            metrics={"stt_confirmed": confirmation},
        )
        question_row = await session.scalar(select(db.QuestionDelivery))
        if confirmation is True:
            with pytest.raises(ValueError, match="no longer available"):
                await request_replay(session, graph.conversation_id, question_row.id, uuid.uuid4())
        else:
            await request_replay(session, graph.conversation_id, question_row.id, uuid.uuid4())
    if confirmation is True:
        assert await graph.deliveries.claim() is None
    else:
        assert (await graph.deliveries.claim()).text == first.text


async def test_replacement_never_replays_a_started_attempt(postgres_sessionmaker, monkeypatch):
    graph, _ = await prepared(postgres_sessionmaker, monkeypatch)
    # Bind a real producer fence to the interview that prepared the decision.
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        row.plan = {"language": "en"}
        row.worker_owner_id = uuid.uuid4()
        row.worker_epoch = 1
        row.worker_lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
        await session.commit()
    first = await graph.deliveries.claim()
    replacement = WorkerCoordinator(graph.conversation_id, postgres_sessionmaker)
    assert await replacement.claim()
    assert await QuestionDeliveries(replacement.sessionmaker, graph.conversation_id).claim() is None
    await graph.deliveries.observed(first.attempt_id, interrupted=False)
    async with postgres_sessionmaker() as session:
        assert (await session.get(db.QuestionAttempt, first.attempt_id)).status == "started"


async def test_real_sdk_output_carries_durable_question_attempt(postgres_sessionmaker, monkeypatch):
    from livekit.agents import AgentSession
    from livekit.agents.llm import ChatMessage

    from interview_agent.agent import InterviewAgent
    from interview_agent.interview.dialogue import DialogueLLM

    async def answer(context, call):
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    session = AgentSession(llm=DialogueLLM(graph), vad=None)
    items = []
    session.on("conversation_item_added", lambda event: items.append(event.item))
    observed = []

    def observe(attempt_id, *, interrupted):
        observed.append(
            asyncio.create_task(graph.deliveries.observed(attempt_id, interrupted=interrupted))
        )

    try:
        await session.start(
            InterviewAgent(instructions="Synthetic", delivery_observer=observe), record=False
        )
        handle = session.generate_reply()
        await asyncio.wait_for(handle, 5)
        assert handle.exception() is None
        messages = [m for m in items if isinstance(m, ChatMessage) and m.role == "assistant"]
        assert len(messages) == 1 and len(calls) == 1
        await asyncio.sleep(0)
        assert len(observed) == 1
        await asyncio.gather(*observed)
        async with postgres_sessionmaker() as transaction:
            attempt = await transaction.scalar(select(db.QuestionAttempt))
            assert attempt.status == "sdk_completed"
        assert await graph.deliveries.claim() is None
    finally:
        await session.aclose()

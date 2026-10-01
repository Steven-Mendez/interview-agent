"""Typed graph behavior and concurrent effects against real PostgreSQL."""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from openai import AsyncOpenAI
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from interview_agent import llm as llm_plumbing
from interview_agent.config import Settings
from interview_agent.interview import db, dialogue, turns
from interview_agent.interview.models import TurnDecision
from interview_agent.server.reconciliation import reconcile_interview


async def test_wall_clock_expiry_during_model_closes_without_revision_change(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        assert context["remaining_seconds"] > 0
        await asyncio.sleep(0.4)
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, graph.conversation_id)
        row.started_at = datetime.now(UTC) - timedelta(minutes=8) + timedelta(seconds=0.3)
        await session.commit()
    result = await graph.run_turn("clock-crossing")
    assert result["decision"]["action"] == "close"
    assert result["decision"]["spoken_text"] == ""
    assert graph.end_event.is_set() and graph.end_reason == "timeout"
    assert len(calls) == 1
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.status == "closing" and row.state_revision == 1
        assert all(m.lifecycle == "skipped" and m.primary_questions == 0 for m in row.milestones)
        applied = await session.scalar(select(db.TurnRun))
        assert applied.decision == result["decision"]


async def test_turn_claim_retries_transient_database_lock_within_queue_budget(
    postgres_sessionmaker, monkeypatch
):
    graph, _, _ = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    monkeypatch.setattr(turns, "CLEANUP_SECONDS", 0.2)
    monkeypatch.setattr(turns, "QUEUE_SECONDS", 2)
    retried = asyncio.Event()
    attempts = 0
    claim_once = graph.turns._claim_once

    async def observe_retry(*args):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            retried.set()
        return await claim_once(*args)

    monkeypatch.setattr(graph.turns, "_claim_once", observe_retry)
    async with postgres_sessionmaker() as lock:
        await lock.scalar(
            select(db.Conversation.id)
            .where(db.Conversation.id == graph.conversation_id)
            .with_for_update()
        )
        task = asyncio.create_task(graph.turns.claim("blocked"))
        # Observe an actual timed-out database attempt, rather than assuming
        # a new asyncpg connection always completes in a few milliseconds.
        await asyncio.wait_for(retried.wait(), 1.5)
        assert not task.done()
        await lock.rollback()
    lease = await asyncio.wait_for(task, 1)
    assert lease is not None
    assert attempts >= 2
    await graph.turns.release(lease)


async def test_turn_claim_has_a_total_queue_deadline(postgres_sessionmaker, monkeypatch):
    graph, _, _ = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    monkeypatch.setattr(turns, "CLEANUP_SECONDS", 0.03)
    monkeypatch.setattr(turns, "QUEUE_SECONDS", 0.18)
    async with postgres_sessionmaker() as lock:
        await lock.scalar(
            select(db.Conversation.id)
            .where(db.Conversation.id == graph.conversation_id)
            .with_for_update()
        )
        start = time.monotonic()
        with pytest.raises(turns.TurnQueueTimeoutError):
            await graph.turns.claim("blocked")
        assert time.monotonic() - start < 0.6
        await lock.rollback()
    async with postgres_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 0


async def test_respond_concurrent_waiters_share_the_bounded_durable_queue(
    postgres_sessionmaker, monkeypatch
):
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(context, call):
        started.set()
        await release.wait()
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    # The short deadline exercises waiting contenders, not the uncontended
    # owner startup (whose SQL admission can take longer under suite load).
    monkeypatch.setattr(turns, "QUEUE_SECONDS", 5)

    def context(turn_id):
        chat = Mock()
        chat.messages.return_value = [
            SimpleNamespace(
                role="user",
                text_content="Confirmed answer " + turn_id,
                id=turn_id,
                metrics={"stt_confirmed": True},
                interrupted=False,
            )
        ]
        return chat

    owner = asyncio.create_task(graph.respond(context("first")))
    try:
        await asyncio.wait_for(started.wait(), 5)
        monkeypatch.setattr(turns, "QUEUE_SECONDS", 0.15)
        before = time.monotonic()
        results = await asyncio.wait_for(
            asyncio.gather(*(graph.respond(context(str(i))) for i in range(3))), 1
        )
        assert results == ["", "", ""] and time.monotonic() - before < 0.8
        assert len(calls) == 1  # Waiters must not call the model or interrupt its owner.
        async with postgres_sessionmaker() as session:
            messages = await db.get_messages(session, graph.conversation_id)
            assert {m.source_id for m in messages} == {"first", "0", "1", "2"}
            assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 1
        assert (
            sum(call.args[1] == "queue_timeouts" for call in graph.telemetry.emit.call_args_list)
            == 3
        )
    finally:
        release.set()
        await asyncio.wait_for(owner, 2)


async def test_respond_bounds_input_persistence_before_claim(postgres_sessionmaker, monkeypatch):
    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    monkeypatch.setattr(turns, "QUEUE_SECONDS", 0.12)
    chat = Mock()
    chat.messages.return_value = [
        SimpleNamespace(
            role="user",
            text_content="Pending confirmed input",
            id="blocked-input",
            metrics={"stt_confirmed": True},
            interrupted=False,
        )
    ]
    async with postgres_sessionmaker() as locked:
        await locked.scalar(
            select(db.Conversation.id)
            .where(
                db.Conversation.id == graph.conversation_id,
            )
            .with_for_update()
        )
        start = time.monotonic()
        # Not admitted, so no owner can answer it: the candidate hears the
        # fixed notice instead of silence, still within the queue bound.
        notice = await asyncio.wait_for(graph.respond(chat), 0.8)
        assert "technical difficulty" in notice or "dificultad técnica" in notice
        assert time.monotonic() - start < 0.7
        await locked.rollback()
    assert calls == []
    async with postgres_sessionmaker() as session:
        assert await db.get_messages(session, graph.conversation_id) == []
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 0
    graph.telemetry.emit.assert_any_call("dialogue", "queue_timeouts", 1, turn_id="blocked-input")


async def test_input_persistence_and_queue_share_one_deadline(postgres_sessionmaker, monkeypatch):
    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    monkeypatch.setattr(turns, "QUEUE_SECONDS", 0.25)
    deadline_observed = []

    original_insert = db.insert_message

    async def delayed_insert(*args, **kwargs):
        await asyncio.sleep(0.1)
        return await original_insert(*args, **kwargs)

    original_claim = graph.turns.claim

    async def claim(turn_id, *, queue_deadline):
        deadline_observed.append(queue_deadline - time.monotonic())
        return await original_claim(turn_id, queue_deadline=queue_deadline)

    monkeypatch.setattr(db, "insert_message", delayed_insert)
    monkeypatch.setattr(graph.turns, "claim", claim)
    chat = Mock()
    chat.messages.return_value = [
        SimpleNamespace(
            role="user",
            text_content="Confirmed input",
            id="shared-deadline",
            metrics={"stt_confirmed": True},
            interrupted=False,
        )
    ]
    # Complete without a provider: the expired interview uses synthetic closure.
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, graph.conversation_id)
        row.started_at = datetime.now(UTC) - timedelta(minutes=9)
        await session.commit()
    assert await graph.respond(chat) == "" and calls == []
    assert 0 < deadline_observed[0] < 0.17


@pytest.mark.parametrize("skew", [-900, 900])
async def test_graph_close_origin_uses_database_clock_before_worker_claim(
    postgres_sessionmaker, monkeypatch, skew
):
    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, graph.conversation_id)
        row.started_at = datetime.now(UTC) - timedelta(minutes=12)
        before = await session.scalar(select(func.clock_timestamp()))
        await session.commit()

    class SkewedDateTime:
        @classmethod
        def now(cls, tz):
            return datetime.now(tz) + timedelta(seconds=skew)

    monkeypatch.setattr(dialogue, "datetime", SkewedDateTime, raising=False)
    await graph.run_turn("closing-origin")
    assert calls == []
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, graph.conversation_id)
        after = await session.scalar(select(func.clock_timestamp()))
        assert before <= row.closing_started_at <= after
        assert row.status == "closing" and row.closing_deadline_at is None
        assert not await reconcile_interview(session, graph.conversation_id, graph.settings)


async def test_first_reservation_preserves_the_claim_deadline(postgres_sessionmaker, monkeypatch):
    graph, _, _ = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    lease = await graph.turns.claim("delayed-reservation")
    async with postgres_sessionmaker() as session:
        before = await session.get(db.TurnExecution, lease.execution_id)
        deadline = before.lease_until
        assert before.invocations_reserved == 1 and before.decision_deadline_at == deadline
        invocation = await session.get(db.TurnInvocation, lease.initial_reservation.id)
        assert invocation.owner_id == lease.owner_id and invocation.outcome is None
    await asyncio.sleep(0.04)
    reservation = await graph.turns.prepare(lease, lease.initial_reservation, 0)
    async with postgres_sessionmaker() as session:
        after = await session.get(db.TurnExecution, lease.execution_id)
        assert after.decision_deadline_at == after.lease_until == deadline
    assert reservation.remaining_seconds <= lease.remaining_seconds + 0.01
    await graph.turns.release(lease)


class GraphTelemetry:
    emit = Mock()

    def __init__(self):
        self.emit = Mock()
        self.parent_span = ContextVar("test_parent_span", default=None)
        self.export_span = AsyncMock()
        self.annotate_span = Mock()
        self.span_outputs = Mock()

    def current_parent(self):
        return self.parent_span.get()

    @asynccontextmanager
    async def span(self, name, inputs=None):
        yield


async def setup_graph(sessionmaker, monkeypatch, answer):
    conversation_id = uuid.uuid4()
    milestones = [uuid.uuid4(), uuid.uuid4()]
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                status="interviewing",
                job_offer="Junior Python and SQL role",
                resume_markdown="Synthetic junior CV",
                seniority="junior",
                started_at=datetime.now(UTC),
                max_minutes=8,
                question_limit=2,
                followup_limit=1,
                agent_settings={"language": "en"},
            )
        )
        for position, milestone_id in enumerate(milestones):
            session.add(
                db.Milestone(
                    id=milestone_id,
                    conversation_id=conversation_id,
                    position=position,
                    title=["SQL", "Git"][position],
                    description="Explain a basic example",
                    expected_evidence="A correct junior example",
                )
            )
        await session.commit()
    calls = []

    class ControlledModel:
        def with_structured_output(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages, config):
            context = json.loads(messages[-1].content)
            calls.append(context)
            return await answer(context, len(calls))

    monkeypatch.setattr(dialogue, "build_chat_model", Mock(return_value=ControlledModel()))
    telemetry = GraphTelemetry()
    controller = dialogue.DialogueController(
        Settings(_env_file=None),
        conversation_id,
        sessionmaker,
        asyncio.Event(),
        telemetry,
    )
    return controller, milestones, calls


def question(milestone_id, text="How do parameterized SQL queries work?", action="question"):
    return TurnDecision(action=action, target_milestone_id=str(milestone_id), spoken_text=text)


async def test_graph_actions_preserve_evidence_and_progress_is_not_passing(
    postgres_sessionmaker, monkeypatch
):
    decisions = []

    async def answer(context, call):
        return decisions[call - 1]

    graph, milestones, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    decisions.extend(
        [
            question(milestones[0]),
            question(milestones[0], "Can you give a short example?", "followup"),
            question(milestones[0], "Which value is passed as the parameter?", "clarification"),
        ]
    )
    for index in range(3):
        await graph.run_turn(f"turn-{index}")
    async with postgres_sessionmaker() as session:
        candidate = await db.insert_message(
            session,
            graph.conversation_id,
            "user",
            "Bind the user ID separately.",
            source_id="a",
            metrics={"stt_confirmed": True},
        )
    decisions.append(
        TurnDecision(
            action="advance",
            target_milestone_id=str(milestones[1]),
            spoken_text="What does a Git commit contain?",
            updates=[
                {
                    "milestone_id": str(milestones[0]),
                    "status": "closed",
                    "close_reason": "covered",
                    "evidence": [{"message_id": str(candidate.id), "quote": candidate.content}],
                }
            ],
        )
    )
    await graph.run_turn("advance")
    async with postgres_sessionmaker() as session:
        last = await db.insert_message(
            session,
            graph.conversation_id,
            "user",
            "I do not know.",
            source_id="b",
            metrics={"stt_confirmed": True},
        )
    decisions.append(
        TurnDecision(
            action="close",
            spoken_text="",
            close_reason="plan_complete",
            updates=[
                {
                    "milestone_id": str(milestones[1]),
                    "status": "closed",
                    "close_reason": "covered",
                    "evidence": [{"message_id": str(last.id), "quote": last.content}],
                }
            ],
        )
    )
    await graph.run_turn("close")
    assert graph.end_event.is_set() and graph.end_reason == "plan_complete"
    assert len(calls) == 5
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.status == "closing" and row.evaluation is None
        assert [m.lifecycle for m in row.milestones] == ["closed", "closed"]
        assert [(m.primary_questions, m.followups, m.clarifications) for m in row.milestones] == [
            (1, 1, 1),
            (1, 0, 0),
        ]
        assert await session.scalar(select(func.count()).select_from(db.Evidence)) == 2


async def test_invalid_decision_is_repaired_before_any_effect(postgres_sessionmaker, monkeypatch):
    async def answer(context, call):
        return question(
            context["milestones"][0]["id"], "word " * 51 if call == 1 else "Explain SQL?"
        )

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    result = await graph.run_turn("opening")
    assert result["decision"]["spoken_text"] == "Explain SQL?" and len(calls) == 2
    assert "repair" in calls[1]
    assert (
        sum(c.args[1] == "invocations_reserved" for c in graph.telemetry.emit.call_args_list) == 2
    )
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.milestones[0].primary_questions == 1
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 1


async def test_schema_error_reaches_the_repair_instead_of_an_empty_decision(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        if call == 1:
            return {"action": "ask"}  # Not a structured TurnDecision.
        return question(context["milestones"][0]["id"], "Explain SQL?")

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    result = await graph.run_turn("opening")
    assert result["decision"]["spoken_text"] == "Explain SQL?" and len(calls) == 2
    error = calls[1]["repair"]["error"]
    assert "decision schema" in error and "Field required" not in error


async def test_second_invalid_decision_has_no_effect(postgres_sessionmaker, monkeypatch):
    async def answer(context, call):
        return question(context["milestones"][0]["id"], "word " * 51)

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    with pytest.raises(ValueError, match="50 words"):
        await graph.run_turn("opening")
    assert len(calls) == 2
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.milestones[0].primary_questions == 0 and row.state_revision == 0
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


@pytest.mark.parametrize("same_turn", [False, True])
async def test_two_workers_serialize_turns_and_duplicate_turn_uses_one_invocation(
    postgres_sessionmaker, monkeypatch, same_turn
):
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def answer(context, call):
        if call == 1:
            first_started.set()
            await release_first.wait()
        target = context["milestones"][0]
        return question(
            target["id"],
            "Explain SQL?" if target["primary_questions"] == 0 else "Give a SQL example?",
            "question" if target["primary_questions"] == 0 else "followup",
        )

    first, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    second = dialogue.DialogueController(
        first.settings,
        first.conversation_id,
        postgres_sessionmaker,
        asyncio.Event(),
        first.telemetry,
    )
    first_task = asyncio.create_task(first.run_turn("a"))
    await asyncio.wait_for(first_started.wait(), 2)
    second_task = asyncio.create_task(second.run_turn("a" if same_turn else "b"))
    try:
        if not same_turn:
            async with asyncio.timeout(2):
                while True:
                    async with postgres_sessionmaker() as session:
                        queued = await session.scalar(
                            select(db.TurnExecution).where(
                                db.TurnExecution.conversation_id == first.conversation_id,
                                db.TurnExecution.turn_id == "b",
                            )
                        )
                    if queued is not None:
                        assert queued.status == "queued" and queued.invocations_reserved == 0
                        break
                    await asyncio.sleep(0.01)
        else:
            async with asyncio.timeout(2):
                while True:
                    async with postgres_sessionmaker() as session:
                        execution = await session.scalar(select(db.TurnExecution))
                    if execution.waiters:
                        break
                    await asyncio.sleep(0.01)
        assert len(calls) == 1 and not second_task.done()
    finally:
        release_first.set()
    results = await asyncio.gather(first_task, second_task)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, first.conversation_id)
        assert row.milestones[0].primary_questions == 1
        assert row.milestones[0].followups == (0 if same_turn else 1)
        assert row.state_revision == (1 if same_turn else 2)
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == (
            1 if same_turn else 2
        )
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == (
            1 if same_turn else 2
        )
    assert len(calls) == (1 if same_turn else 2)
    if same_turn:
        assert sum(bool(result.get("replayed")) for result in results) == 1


async def test_closure_started_during_decision_prevents_speech_and_effects(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        async with postgres_sessionmaker() as session:
            await db.set_status(session, graph.conversation_id, "closing", "candidate_requested")
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    result = await graph.run_turn("opening")
    assert result["replayed"] and len(calls) == 1
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.status == "closing" and row.milestones[0].primary_questions == 0
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


async def test_contract_repair_and_stale_retry_share_one_budget(postgres_sessionmaker, monkeypatch):
    async def answer(context, call):
        if call == 1:
            return question(context["milestones"][0]["id"], "word " * 51)
        async with postgres_sessionmaker() as session:
            await db.insert_message(session, graph.conversation_id, "user", "Late confirmed turn")
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    with pytest.raises(ValueError, match="bounded retry"):
        await graph.run_turn("opening")
    assert len(calls) == 2
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.milestones[0].primary_questions == 0
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


async def test_metadata_only_message_replay_does_not_invalidate_state(postgres_sessionmaker):
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        session.add(db.Conversation(id=conversation_id, job_offer="Role", resume_markdown="CV"))
        await session.commit()
        message = await db.insert_message(session, conversation_id, "user", "Answer", source_id="x")
        first_id = message.id
        assert (await db.get_conversation(session, conversation_id)).state_revision == 1
    async with postgres_sessionmaker() as session:
        replay = await db.insert_message(
            session, conversation_id, "user", "Answer", source_id="x", metrics={"duration": 2}
        )
        assert replay.id == first_id
        assert (await db.get_conversation(session, conversation_id)).state_revision == 1
        await db.insert_message(
            session, conversation_id, "user", "Answer", source_id="x", interrupted=True
        )
        assert (await db.get_conversation(session, conversation_id)).state_revision == 2


async def test_stt_eligibility_change_invalidates_decision_and_cannot_change_after_seal(
    postgres_sessionmaker,
):
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        session.add(db.Conversation(id=conversation_id, job_offer="Role", resume_markdown="CV"))
        await session.commit()
        await db.insert_message(session, conversation_id, "user", "Tail", source_id="tail")
        await db.insert_message(
            session,
            conversation_id,
            "user",
            "Tail",
            source_id="tail",
            metrics={"stt_confirmed": False},
        )
        row = await db.get_conversation(session, conversation_id)
        assert row.state_revision == 2
        row.transcript_sealed_at = datetime.now(UTC)
        await session.commit()
    async with postgres_sessionmaker() as session:
        with pytest.raises(ValueError, match="Sealed STT eligibility"):
            await db.insert_message(
                session,
                conversation_id,
                "user",
                "Tail",
                source_id="tail",
                metrics={"stt_confirmed": True},
            )
        await session.rollback()
        row = await db.get_conversation(session, conversation_id)
        messages = await db.get_messages(session, conversation_id)
        assert row.state_revision == 2
        assert messages[0].metrics["stt_confirmed"] is False


@pytest.mark.parametrize("initial,updated", [(None, None), (True, 1)])
async def test_stt_flag_presence_and_type_change_invalidate_revision(
    postgres_sessionmaker,
    initial,
    updated,
):
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        session.add(db.Conversation(id=conversation_id, job_offer="Role", resume_markdown="CV"))
        await session.commit()
        await db.insert_message(
            session,
            conversation_id,
            "user",
            "Answer",
            source_id="a",
            metrics={"stt_confirmed": initial} if initial is not None else None,
        )
        await db.insert_message(
            session,
            conversation_id,
            "user",
            "Answer",
            source_id="a",
            metrics={"stt_confirmed": updated},
        )
        row = await db.get_conversation(session, conversation_id)
        assert row.state_revision == 2
        row.transcript_sealed_at = datetime.now(UTC)
        await session.commit()
    async with postgres_sessionmaker() as session:
        stored = (await db.get_messages(session, conversation_id))[0].metrics["stt_confirmed"]
        assert type(stored) is type(updated) and stored == updated
        with pytest.raises(ValueError, match="Sealed STT eligibility"):
            await db.insert_message(
                session,
                conversation_id,
                "user",
                "Answer",
                source_id="a",
                metrics={"stt_confirmed": True},
            )


@pytest.mark.parametrize("confirmation", [False, None, "unknown", "true", 1, {"reason": "pending"}])
@pytest.mark.parametrize("present", [True, False])
async def test_unconfirmed_user_turn_is_diagnostic_without_model_or_question_effects(
    postgres_sessionmaker,
    monkeypatch,
    confirmation,
    present,
):
    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    chat = Mock()
    chat.messages.return_value = [
        SimpleNamespace(
            role="user",
            id="promoted-interim",
            text_content="An uncertain tail",
            metrics={"stt_confirmed": confirmation} if present else {},
            interrupted=False,
        )
    ]
    spoken = await graph.respond(chat)
    assert "transcription" in spoken and "repeat" in spoken
    assert calls == []
    async with postgres_sessionmaker() as session:
        messages = await db.get_messages(session, graph.conversation_id)
        row = await db.get_conversation(session, graph.conversation_id)
        assert messages[0].content == "An uncertain tail"
        assert messages[0].metrics == {
            "stt_confirmed": confirmation if present else False,
            "stt_segmentation": "unknown",
        }
        assert type(messages[0].metrics["stt_confirmed"]) is type(
            confirmation if present else False
        )
        assert all(m.primary_questions == 0 for m in row.milestones)
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 0


async def test_reused_session_refreshes_revision_after_another_writer(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        return question(context["milestones"][0]["id"])

    graph, milestones, _ = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    async with postgres_sessionmaker() as retained_session:
        retained = await db.get_conversation(retained_session, graph.conversation_id)
        assert retained.state_revision == 0
        async with postgres_sessionmaker() as other:
            await db.insert_message(other, graph.conversation_id, "user", "First answer")
        lease = await graph.turns.claim("pending")
        state = await graph.load({"turn_id": "pending", "lease": lease})
        await graph.turns.prepare(
            lease, lease.initial_reservation, state["context"]["state_revision"]
        )
        assert state["context"]["state_revision"] == 1
        await db.insert_message(retained_session, graph.conversation_id, "user", "Later answer")
        assert retained.state_revision == 2
        state.update(
            turn_id="pending",
            lease=lease,
            attempts=1,
            decision=question(milestones[0]).model_dump(mode="json"),
        )
        assert await graph.persist(state) == {"stale": True}
        assert retained.milestones[0].primary_questions == 0


async def test_reused_session_cannot_append_after_another_session_seals(postgres_sessionmaker):
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as retained_session:
        retained = db.Conversation(id=conversation_id, job_offer="Role", resume_markdown="CV")
        retained_session.add(retained)
        await retained_session.commit()
        async with postgres_sessionmaker() as other:
            row = await db.get_conversation(other, conversation_id)
            row.transcript_sealed_at = datetime.now(UTC)
            await other.commit()
        with pytest.raises(ValueError, match="sealed"):
            await db.insert_message(retained_session, conversation_id, "user", "Late answer")
        assert retained.transcript_sealed_at is not None


async def test_cancelled_worker_reservation_survives_replacement_and_exhaustion(
    postgres_sessionmaker, monkeypatch
):
    started = asyncio.Event()

    async def answer(context, call):
        if call == 1:
            started.set()
            await asyncio.Event().wait()
        return question(context["milestones"][0]["id"], "word " * 51)

    first, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    task = asyncio.create_task(first.run_turn("candidate-turn"))
    await asyncio.wait_for(started.wait(), 2)
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        deadline = execution.decision_deadline_at
        assert execution.invocations_reserved == 1
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replacement = dialogue.DialogueController(
        first.settings,
        first.conversation_id,
        postgres_sessionmaker,
        asyncio.Event(),
        first.telemetry,
    )
    with pytest.raises(ValueError, match="50 words"):
        await replacement.run_turn("candidate-turn")
    with pytest.raises(turns.DecisionLimitError, match="exhausted"):
        await first.run_turn("candidate-turn")
    assert len(calls) == 2
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.invocations_reserved == 2 and execution.status == "failed"
        assert execution.decision_deadline_at == deadline
        invocations = list(
            await session.scalars(select(db.TurnInvocation).order_by(db.TurnInvocation.ordinal))
        )
        assert [item.outcome for item in invocations] == ["cancelled", "returned"]
        assert invocations[0].owner_id != invocations[1].owner_id
        row = await db.get_conversation(session, first.conversation_id)
        assert row.milestones[0].primary_questions == 0


async def test_repair_inherits_remaining_deadline_and_no_more_requests_after_expiry(
    postgres_sessionmaker, monkeypatch
):
    monkeypatch.setattr(turns, "DECISION_SECONDS", 0.35)

    async def answer(context, call):
        await asyncio.sleep(0.2 if call == 1 else 2)
        return question(context["milestones"][0]["id"], "word " * 51)

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    started = time.monotonic()
    with pytest.raises(turns.TurnDecisionError, match="deadline"):
        await graph.run_turn("slow-turn")
    assert time.monotonic() - started < 1.5
    options = [call.kwargs for call in dialogue.build_chat_model.call_args_list]
    assert [item["max_retries"] for item in options] == [1, 1]
    assert options[1]["timeout_seconds"] < options[0]["timeout_seconds"] - 0.15
    with pytest.raises(turns.DecisionLimitError):
        await graph.run_turn("slow-turn")
    assert len(calls) == 2
    async with postgres_sessionmaker() as session:
        invocations = list(
            await session.scalars(select(db.TurnInvocation).order_by(db.TurnInvocation.ordinal))
        )
        assert [item.outcome for item in invocations] == ["returned", "deadline"]
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


async def test_former_owner_cannot_persist_or_release_replacements_lease(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        return question(context["milestones"][0]["id"])

    graph, milestones, _ = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    first = await graph.turns.claim("turn")
    old_state = await graph.load({"turn_id": "turn", "lease": first})
    await graph.turns.prepare(
        first, first.initial_reservation, old_state["context"]["state_revision"]
    )
    await graph.turns.release(first)
    second = await graph.turns.claim("turn")
    await graph.turns.prepare(
        second, second.initial_reservation, old_state["context"]["state_revision"]
    )
    old_state.update(
        turn_id="turn",
        lease=first,
        attempts=1,
        decision=question(milestones[0]).model_dump(mode="json"),
    )
    with pytest.raises(turns.TurnOwnershipError):
        await graph.persist(old_state)
    await graph.turns.release(first)
    async with postgres_sessionmaker() as session:
        execution = await session.get(db.TurnExecution, second.execution_id)
        assert execution.owner_id == second.owner_id and execution.status == "running"
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0
    new_state = {**old_state, "lease": second, "attempts": 2}
    assert (await graph.persist(new_state))["decision"] == new_state["decision"]
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.milestones[0].primary_questions == 1 and row.state_revision == 1


async def test_stale_first_decision_reuses_the_durable_two_invocation_budget(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        if call == 1:
            async with postgres_sessionmaker() as session:
                await db.insert_message(
                    session, graph.conversation_id, "user", "A final STT segment"
                )
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    result = await graph.run_turn("capture")
    assert result["attempts"] == 2 and len(calls) == 2
    assert [call["state_revision"] for call in calls] == [0, 1]
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.invocations_reserved == 2 and execution.status == "applied"
        invocations = list(
            await session.scalars(select(db.TurnInvocation).order_by(db.TurnInvocation.ordinal))
        )
        assert [item.state_revision for item in invocations] == [0, 1]


async def test_time_limit_closure_does_not_reserve_a_model_invocation(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        pytest.fail("A clock-expired interview does not need another model call")

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        row.started_at = datetime.now(UTC) - timedelta(minutes=9)
        await session.commit()
    result = await graph.run_turn("time-limit")
    assert result["decision"]["close_reason"] == "timeout" and calls == []
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.invocations_reserved == 0 and execution.status == "applied"
        assert execution.decision_deadline_at is None
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 0


async def test_exhausted_turn_has_localized_notice_and_explicit_new_turn_can_continue(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        return question(
            context["milestones"][0]["id"], "word " * 51 if call < 3 else "Explica SQL."
        )

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        row.agent_settings = {"language": "es"}
        await session.commit()
    message = SimpleNamespace(
        role="user",
        text_content="Primera respuesta",
        id="a",
        metrics={"stt_confirmed": True},
        interrupted=False,
    )
    context = Mock()
    context.messages.return_value = [message]
    notice = await graph.respond(context)
    assert "dificultad técnica" in notice and "terminar la entrevista" in notice
    assert await graph.respond(context) == "" and len(calls) == 2
    message.id = "b"
    message.text_content = "Nueva respuesta explícita"
    assert await graph.respond(context) == "Explica SQL." and len(calls) == 3
    async with postgres_sessionmaker() as session:
        executions = list(
            await session.scalars(select(db.TurnExecution).order_by(db.TurnExecution.created_at))
        )
        assert [(item.turn_id, item.invocations_reserved) for item in executions] == [
            ("a", 2),
            ("b", 1),
        ]


@pytest.mark.parametrize("duplicate_waiter", [False, True])
async def test_cancelled_waiter_does_not_block_next_turn_or_withdraw_another_waiter(
    postgres_sessionmaker, monkeypatch, duplicate_waiter
):
    started, resume = asyncio.Event(), asyncio.Event()

    async def answer(context, call):
        if call == 1:
            started.set()
            await resume.wait()
        return question(
            context["milestones"][0]["id"],
            "Explain SQL?" if call == 1 else "Give a SQL example?",
            "question" if call == 1 else "followup",
        )

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    first = asyncio.create_task(graph.run_turn("a"))
    await asyncio.wait_for(started.wait(), 2)
    cancelled = asyncio.create_task(graph.run_turn("b"))
    another = asyncio.create_task(graph.run_turn("b")) if duplicate_waiter else None
    async with asyncio.timeout(2):
        while True:
            async with postgres_sessionmaker() as session:
                row = await session.scalar(
                    select(db.TurnExecution).where(db.TurnExecution.turn_id == "b")
                )
            if row is not None and len(row.waiters) == (2 if duplicate_waiter else 1):
                break
            await asyncio.sleep(0.01)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    async with postgres_sessionmaker() as session:
        row = await session.scalar(select(db.TurnExecution).where(db.TurnExecution.turn_id == "b"))
        assert row.status == ("queued" if duplicate_waiter else "idle")
        assert len(row.waiters) == (1 if duplicate_waiter else 0)
    remaining = another if duplicate_waiter else asyncio.create_task(graph.run_turn("c"))
    resume.set()
    await asyncio.wait_for(asyncio.gather(first, remaining), 2)
    assert len(calls) == 2


async def test_db_locks_do_not_block_cancellation_finalizers(postgres_sessionmaker, monkeypatch):
    monkeypatch.setattr(dialogue, "FINALIZE_SECONDS", 0.05)
    started = asyncio.Event()

    async def answer(context, call):
        started.set()
        await asyncio.Event().wait()

    graph, _, _ = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    task = asyncio.create_task(graph.run_turn("blocked-cleanup"))
    await asyncio.wait_for(started.wait(), 2)
    async with postgres_sessionmaker() as blocker:
        await blocker.scalar(select(db.TurnInvocation).with_for_update())
        await blocker.scalar(
            select(db.Conversation)
            .where(db.Conversation.id == graph.conversation_id)
            .with_for_update()
        )
        before = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert time.monotonic() - before < 0.5
        graph.telemetry.emit.assert_any_call(
            "dialogue",
            "outcome_cleanup_failures",
            1,
            turn_id="blocked-cleanup",
            dimensions={"error_type": "TimeoutError"},
        )
        graph.telemetry.emit.assert_any_call(
            "dialogue",
            "release_cleanup_failures",
            1,
            turn_id="blocked-cleanup",
            dimensions={"error_type": "TimeoutError"},
        )
        await blocker.rollback()
    async with postgres_sessionmaker() as session:
        invocation = await session.scalar(select(db.TurnInvocation))
        execution = await session.scalar(select(db.TurnExecution))
        assert invocation.outcome is None and execution.invocations_reserved == 1


async def test_cancel_during_load_leaves_no_queue_blockage(postgres_sessionmaker, monkeypatch):
    started = asyncio.Event()

    async def answer(context, call):
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    real_get = db.get_conversation

    async def paused_get(session, conversation_id):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(db, "get_conversation", paused_get)
    task = asyncio.create_task(graph.run_turn("pre-model"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(db, "get_conversation", real_get)
    assert not calls
    result = await asyncio.wait_for(graph.run_turn("new-turn"), 2)
    assert result["decision"]["spoken_text"] and len(calls) == 1
    assert (
        sum(c.args[1] == "invocations_reserved" for c in graph.telemetry.emit.call_args_list) == 2
    )
    async with postgres_sessionmaker() as session:
        old = await session.scalar(
            select(db.TurnExecution).where(db.TurnExecution.turn_id == "pre-model")
        )
        assert old.status == "idle" and old.invocations_reserved == 1


async def test_late_failure_notice_is_suppressed_after_another_turn_claims(
    postgres_sessionmaker, monkeypatch
):
    monkeypatch.setattr(turns, "DECISION_SECONDS", 5)
    cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()
    second_started, finish_second = asyncio.Event(), asyncio.Event()

    async def answer(context, call):
        if call == 1:
            # Expire the durable budget only after the first invocation starts.
            # A tiny wall-clock budget can expire during SQL/context loading,
            # never reaching the cleanup race that this test must exercise.
            async with postgres_sessionmaker() as session:
                execution = await session.scalar(
                    select(db.TurnExecution).where(
                        db.TurnExecution.conversation_id == graph.conversation_id,
                        db.TurnExecution.turn_id == "opening",
                    )
                )
                now = await session.scalar(select(func.clock_timestamp()))
                execution.lease_until = now - timedelta(seconds=1)
                execution.decision_deadline_at = now - timedelta(seconds=1)
                await session.commit()
            raise TimeoutError("Controlled provider deadline after persisted expiry")
        second_started.set()
        await finish_second.wait()
        return question(context["milestones"][0]["id"])

    graph, _, _ = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    second = dialogue.DialogueController(
        graph.settings,
        graph.conversation_id,
        postgres_sessionmaker,
        asyncio.Event(),
        graph.telemetry,
    )
    original_outcome = graph.turns.record_outcome

    async def paused_outcome(reservation, outcome):
        cleanup_started.set()
        await finish_cleanup.wait()
        await original_outcome(reservation, outcome)

    monkeypatch.setattr(graph.turns, "record_outcome", paused_outcome)
    context = Mock()
    context.messages.return_value = []
    old_task = asyncio.create_task(graph.respond(context))
    await asyncio.wait_for(cleanup_started.wait(), 2)
    monkeypatch.setattr(turns, "DECISION_SECONDS", 2)
    new_task = asyncio.create_task(second.run_turn("new-turn"))
    await asyncio.wait_for(second_started.wait(), 2)
    finish_cleanup.set()
    assert await asyncio.wait_for(old_task, 1) == ""
    finish_second.set()
    assert (await new_task)["decision"]["spoken_text"]


async def test_refusal_is_a_recoverable_notice_without_schema_retry(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        raise dialogue.OpenAIRefusalError("Synthetic refusal")

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    context = Mock()
    context.messages.return_value = []
    assert "technical difficulty" in await graph.respond(context)
    assert len(calls) == 1
    async with postgres_sessionmaker() as session:
        invocation = await session.scalar(select(db.TurnInvocation))
        assert invocation.outcome == "refusal"
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


async def test_clock_closure_refreshes_skips_under_lock_without_model_or_stale_loop(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        pytest.fail("Clock closure does not need a model")

    first, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, first.conversation_id)
        row.started_at = datetime.now(UTC) - timedelta(minutes=9)
        await session.commit()
    original_persist = dialogue.DialogueController.persist

    async def changed_persist(self, state):
        async with postgres_sessionmaker() as session:
            await db.insert_message(
                session, self.conversation_id, "user", "Accepted final fragment"
            )
        return await original_persist(self, state)

    monkeypatch.setattr(dialogue.DialogueController, "persist", changed_persist)
    graph = dialogue.DialogueController(
        first.settings,
        first.conversation_id,
        postgres_sessionmaker,
        asyncio.Event(),
        first.telemetry,
    )
    result = await graph.run_turn("clock")
    assert result["decision"]["close_reason"] == "timeout" and calls == []
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.status == "closing" and row.state_revision == 2
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 0
        assert len(await db.get_messages(session, graph.conversation_id)) == 1


async def test_real_structured_langchain_path_does_not_retry_beyond_two_http_attempts(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        pytest.fail("The real LangChain structured path should be invoked")

    graph, _, _ = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    graph.settings = graph.settings.model_copy(update={"openai_api_key": "synthetic-key"})
    requests = []

    async def unavailable(request):
        requests.append(request)
        return httpx.Response(
            503, json={"error": {"message": "Synthetic outage", "type": "server_error"}}
        )

    monkeypatch.setattr(
        llm_plumbing.httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(unavailable)
    )
    monkeypatch.setattr(AsyncOpenAI, "_calculate_retry_timeout", lambda *args: 0)
    monkeypatch.setattr(dialogue, "build_chat_model", llm_plumbing.build_chat_model)
    with pytest.raises(turns.TurnDecisionError, match="Synthetic outage"):
        await graph.run_turn("real-transport")
    assert len(requests) == 2
    assert [request.headers["x-stainless-retry-count"] for request in requests] == ["0", "1"]
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.invocations_reserved == 1
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


@pytest.mark.parametrize("blocked_stage", ["repair", "persist"])
async def test_shared_deadline_bounds_active_database_lock_waits(
    postgres_sessionmaker, monkeypatch, blocked_stage
):
    monkeypatch.setattr(turns, "DECISION_SECONDS", 0.25)
    monkeypatch.setattr(dialogue, "FINALIZE_SECONDS", 0.05)
    model_started, release_model = asyncio.Event(), asyncio.Event()

    async def answer(context, call):
        model_started.set()
        await release_model.wait()
        return question(
            context["milestones"][0]["id"],
            "word " * 51 if blocked_stage == "repair" else "Explain SQL?",
        )

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    task = asyncio.create_task(graph.run_turn("locked-active-path"))
    await asyncio.wait_for(model_started.wait(), 2)
    async with postgres_sessionmaker() as blocker:
        await blocker.scalar(
            select(db.Conversation)
            .where(db.Conversation.id == graph.conversation_id)
            .with_for_update()
        )
        release_model.set()
        with pytest.raises(turns.TurnDecisionError, match="deadline"):
            await asyncio.wait_for(task, 1)
        await blocker.rollback()
    assert len(calls) == 1
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.invocations_reserved == 1
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


async def test_late_duplicate_cannot_change_sealed_interruption(postgres_sessionmaker):
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        session.add(db.Conversation(id=conversation_id, job_offer="Role", resume_markdown="CV"))
        await session.commit()
        await db.insert_message(session, conversation_id, "assistant", "Question", source_id="q")
        row = await db.get_conversation(session, conversation_id)
        row.transcript_sealed_at = await session.scalar(select(func.clock_timestamp()))
        await session.commit()
    async with postgres_sessionmaker() as session:
        with pytest.raises(ValueError, match="Sealed message interruption"):
            await db.insert_message(
                session, conversation_id, "assistant", "Question", source_id="q", interrupted=True
            )
        await session.rollback()
        row = await db.get_conversation(session, conversation_id)
        message = (await db.get_messages(session, conversation_id))[0]
        assert row.state_revision == 1 and message.interrupted is False
        replay = await db.insert_message(
            session, conversation_id, "assistant", "Question", source_id="q"
        )
        assert replay.id == message.id


async def test_claim_reservation_failure_rolls_back_owner_and_budget(
    postgres_sessionmaker, monkeypatch
):
    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    async with postgres_sessionmaker() as session:
        await session.execute(
            text(
                "ALTER TABLE turn_invocations ADD CONSTRAINT reject_test_reservation "
                "CHECK (ordinal < 0)"
            )
        )
        await session.commit()
    with pytest.raises(IntegrityError):
        await graph.turns.claim("reservation-fails")
    assert calls == []
    async with postgres_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(db.TurnExecution)) == 0
        assert await session.scalar(select(func.count()).select_from(db.TurnInvocation)) == 0
        assert (await db.get_conversation(session, graph.conversation_id)).status == "interviewing"


async def test_crash_after_claim_before_load_consumes_reservation_on_replacement(
    postgres_sessionmaker, monkeypatch
):
    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    first = await graph.turns.claim("pre-load-crash")
    async with postgres_sessionmaker() as session:
        execution = await session.get(db.TurnExecution, first.execution_id)
        deadline = execution.decision_deadline_at
        execution.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
        await session.commit()
    replacement = await graph.turns.claim("pre-load-crash")
    assert first.initial_reservation.ordinal == 1 and replacement.initial_reservation.ordinal == 2
    assert calls == []
    with pytest.raises(turns.TurnOwnershipError):
        await graph.turns.prepare(first, first.initial_reservation, 0)
    async with postgres_sessionmaker() as session:
        execution = await session.get(db.TurnExecution, replacement.execution_id)
        assert execution.invocations_reserved == 2 and execution.decision_deadline_at == deadline
        invocations = list(
            await session.scalars(select(db.TurnInvocation).order_by(db.TurnInvocation.ordinal))
        )
        assert [i.owner_id for i in invocations] == [first.owner_id, replacement.owner_id]
        assert all(
            i.outcome is None for i in invocations
        )  # Reservations do not prove an HTTP request.
    await graph.turns.release(replacement)
    with pytest.raises(turns.DecisionLimitError):
        await graph.turns.claim("pre-load-crash")


@pytest.mark.parametrize("skew", [-900, 900])
async def test_interview_start_uses_database_clock_and_reconnect_preserves_origin(
    postgres_sessionmaker, monkeypatch, skew
):
    async def answer(context, call):
        assert 475 < context["remaining_seconds"] <= 480
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        row.status = "planned"
        row.started_at = None
        await session.commit()

    class SkewedDateTime:
        @classmethod
        def now(cls, tz):
            return datetime.now(tz) + timedelta(seconds=skew)

    monkeypatch.setattr(db, "datetime", SkewedDateTime)
    async with postgres_sessionmaker() as session:
        before = await session.scalar(select(func.clock_timestamp()))
        assert await db.begin_interview(session, graph.conversation_id) == "interviewing"
        after = await session.scalar(select(func.clock_timestamp()))
        row = await db.get_conversation(session, graph.conversation_id)
        origin = row.started_at
        assert before <= origin <= after and row.state_revision == 1
    assert (await graph.run_turn("opening"))["decision"]["spoken_text"] and len(calls) == 1
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        revision = row.state_revision
        assert await db.begin_interview(session, graph.conversation_id) == "interviewing"
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.started_at == origin and row.state_revision == revision


@pytest.mark.parametrize("phase", ["initial", "repair"])
async def test_state_change_before_call_reloads_without_spending_an_extra_reservation(
    postgres_sessionmaker, monkeypatch, phase
):
    async def answer(context, call):
        return question(
            context["milestones"][0]["id"],
            "word " * 51 if phase == "repair" and call == 1 else "Explain SQL.",
        )

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    method = "prepare" if phase == "initial" else "reserve"
    original = getattr(graph.turns, method)
    changed = False

    async def update_before_call(*args):
        nonlocal changed
        if not changed:
            changed = True
            async with postgres_sessionmaker() as session:
                await db.insert_message(
                    session,
                    graph.conversation_id,
                    "user",
                    "Additional final segment",
                    source_id="late-final",
                    metrics={"stt_confirmed": True},
                )
        return await original(*args)

    monkeypatch.setattr(graph.turns, method, update_before_call)
    result = await graph.run_turn("reload-before-http")
    assert result["decision"]["spoken_text"] == "Explain SQL."
    expected = 1 if phase == "initial" else 2
    assert len(calls) == expected and calls[-1]["state_revision"] == 1
    graph.telemetry.emit.assert_any_call(
        "dialogue", "stale_before_invocation", 1, turn_id="reload-before-http"
    )
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.invocations_reserved == expected and execution.status == "applied"
        invocations = list(
            await session.scalars(select(db.TurnInvocation).order_by(db.TurnInvocation.ordinal))
        )
        assert [i.state_revision for i in invocations] == ([1] if phase == "initial" else [0, 1])


async def test_continually_changed_input_has_bounded_notice_and_releases_owner(
    postgres_sessionmaker, monkeypatch
):
    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, AsyncMock())
    original = graph.turns.prepare
    changes = 0

    async def continually_change(*args):
        nonlocal changes
        changes += 1
        async with postgres_sessionmaker() as session:
            await db.insert_message(
                session,
                graph.conversation_id,
                "user",
                "Another confirmed segment",
                source_id=f"changing-{changes}",
                metrics={"stt_confirmed": True},
            )
        return await original(*args)

    monkeypatch.setattr(graph.turns, "prepare", continually_change)
    chat = Mock()
    chat.messages.return_value = []
    notice = await asyncio.wait_for(graph.respond(chat), 5)
    assert "technical difficulty" in notice and calls == [] and changes > 1
    graph.telemetry.emit.assert_any_call(
        "dialogue",
        "decision_errors",
        1,
        turn_id="opening",
        dimensions={"error_type": "GraphRecursionError"},
    )
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.owner_id is None and execution.invocations_reserved == 1
        assert execution.status == "idle" and execution.notice_claimed_at is not None
        assert execution.decision_deadline_at is not None
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0
        row = await db.get_conversation(session, graph.conversation_id)
        assert all(m.primary_questions == 0 for m in row.milestones)


@pytest.mark.parametrize("during_call", [False, True])
async def test_capture_incident_blocks_model_or_pending_effects(
    postgres_sessionmaker, monkeypatch, during_call
):
    async def mark_pending():
        async with postgres_sessionmaker() as session:
            row = await db.get_conversation(session, graph.conversation_id)
            row.capture_integrity_pending = True
            row.state_revision += 1
            await session.commit()

    async def answer(context, call):
        await mark_pending()
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    if not during_call:
        await mark_pending()
    with pytest.raises(turns.TurnDecisionError):
        await graph.run_turn("opening")
    assert len(calls) == int(during_call)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert all(m.primary_questions == 0 for m in row.milestones)
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == 0


@pytest.mark.parametrize("correct_during_repair", [False, True])
async def test_durable_corrections_during_decision_share_two_invocations(
    postgres_sessionmaker, monkeypatch, correct_during_repair
):
    from interview_agent.interview.transcription import admit_candidate

    version = 1

    async def capture(text):
        async with postgres_sessionmaker() as session:
            await admit_candidate(
                session,
                graph.conversation_id,
                content=text,
                source_id=f"sdk-{version}",
                metrics={
                    "stt_confirmed": True,
                    "stt_turn_id": "logical",
                    "stt_turn_version": version,
                },
            )

    async def answer(context, call):
        nonlocal version
        if version == 1 or correct_during_repair:
            version += 1
            await capture(f"Correction {version}")
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    await capture("Original")
    if correct_during_repair:
        with pytest.raises(turns.TurnDecisionError):
            await graph.run_turn("logical")
    else:
        result = await graph.run_turn("logical")
        assert not result.get("stale")
    assert len(calls) == 2
    async with postgres_sessionmaker() as session:
        execution = await session.scalar(select(db.TurnExecution))
        assert execution.invocations_reserved == 2
        assert execution.decision_deadline_at is not None
        assert await session.scalar(select(func.count()).select_from(db.TurnRun)) == int(
            not correct_during_repair
        )
        assert (await db.get_messages(session, graph.conversation_id))[0].version == version

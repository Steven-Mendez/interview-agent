"""Structural full flow, with synthetic input and controlled audio/provider doubles."""

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from test_closing import ack, coordinator
from test_dialogue import question, setup_graph
from test_routes import _fake_evaluator

from interview_agent.interview import db
from interview_agent.interview.transcription import OrderedCaptureWriter
from interview_agent.server import evaluations
from interview_agent.stt_drain import STTDrainReport


async def test_question_capture_correction_farewell_seal_and_recovered_evaluation(
    postgres_sessionmaker, monkeypatch
):
    async def answer(context, call):
        return question(context["milestones"][0]["id"])

    graph, _, calls = await setup_graph(postgres_sessionmaker, monkeypatch, answer)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        row.run_config = {"schema_version": 2}
        row.plan = {"language": "en"}
        await session.commit()
    await graph.run_turn("opening")
    first = await graph.deliveries.claim()
    assert first and len(calls) == 1
    # Crash/reconnect cannot replay a possibly heard question.
    assert await graph.deliveries.claim() is None
    writer = OrderedCaptureWriter(postgres_sessionmaker, graph.conversation_id)
    for version, text in ((1, "I use an index"), (2, "I use a B-tree index for lookups")):
        writer.submit(
            content=text,
            source_id=f"sdk-v{version}",
            metrics={
                "stt_turn_id": "answer",
                "stt_turn_version": version,
                "stt_confirmed": True,
            },
        )
    await writer.drain()
    assert await graph.deliveries.claim() is None
    closing, transport, _ = coordinator(postgres_sessionmaker, graph.conversation_id)
    closing.drain_transcript = writer.drain
    closing.end_stt_input = AsyncMock(return_value=STTDrainReport(True, 1, 2, 0, 1, 1))
    finishing = asyncio.create_task(closing.finish("candidate_requested"))
    await asyncio.wait_for(transport.sent.wait(), 1)
    async with postgres_sessionmaker() as session:
        assert (
            await session.get(db.Conversation, graph.conversation_id)
        ).transcript_sealed_at is None
    await closing.acknowledge("candidate", ack(closing))
    assert await finishing == "played"
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    runner = evaluations.EvaluationRunner(postgres_sessionmaker)
    # The worker dies between sealing and requesting evaluation; no browser/API trigger.
    assert await runner.reconcile_pending() == 1
    await runner.wait_idle()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, graph.conversation_id)
        assert row.status == "evaluated" and row.transcript_integrity == "complete"
        evidence = row.evaluation.result["criteria"][0]["evidence"][0]
        assert evidence["message_version"] == 2
        assert evidence["quote"] == "I use a B-tree index for lookups"
        assert await session.scalar(select(func.count()).select_from(db.EvaluationRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(db.QuestionAttempt)) == 1
        assert await session.scalar(select(func.count()).select_from(db.MessageVersion)) == 2
    assert await runner.reconcile_pending() == 0

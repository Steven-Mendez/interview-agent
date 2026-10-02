"""Immutable candidate captures, aliases and shared budgets against PostgreSQL."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from interview_agent.interview import db
from interview_agent.interview.evaluation_contract import canonical_message_records, verify_evidence
from interview_agent.interview.models import EvidenceRef
from interview_agent.interview.transcription import admit_candidate
from interview_agent.interview.turns import TurnCoordinator
from interview_agent.interview.workers import WorkerCoordinator, WorkerOwnershipError


async def seed(sessions):
    interview_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            db.Conversation(
                id=interview_id,
                status="planned",
                job_offer="Synthetic role",
                resume_markdown="Synthetic CV",
                plan={"language": "es"},
                max_minutes=8,
            )
        )
        await session.commit()
    return interview_id


def proof(version=1, *, turn_id="logical-turn"):
    return {
        "stt_confirmed": True,
        "stt_turn_id": turn_id,
        "stt_turn_version": version,
        "stt_segments": [{"provider_request_id": "stream", "audio_start": 0, "audio_end": 2}],
    }


async def admit(sessions, interview_id, text="Initial answer", source="sdk-a", metrics=None):
    async with sessions() as session:
        return await admit_candidate(
            session,
            interview_id,
            content=text,
            source_id=source,
            metrics=proof() if metrics is None else metrics,
        )


async def test_correction_keeps_id_order_history_and_delayed_old_source(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    first = await admit(postgres_sessionmaker, interview_id)
    original_id, original_seq = first.message.id, first.message.seq
    corrected = await admit(
        postgres_sessionmaker, interview_id, "Corrected answer", "sdk-b", proof(2)
    )
    assert corrected.accepted and corrected.message.id == original_id
    assert corrected.message.version == 2 and corrected.message.seq == original_seq
    replay = await admit(postgres_sessionmaker, interview_id)
    assert replay.accepted and replay.message.version == 2
    async with postgres_sessionmaker() as session:
        history = list(
            await session.scalars(select(db.MessageVersion).order_by(db.MessageVersion.version))
        )
        assert [(v.version, v.content) for v in history] == [
            (1, "Initial answer"),
            (2, "Corrected answer"),
        ]
        assert history[0].metrics["stt_turn_version"] == 1
        row = await db.get_conversation(session, interview_id)
        assert row.state_revision == 2 and not row.capture_integrity_pending
        assert len(await db.get_messages(session, interview_id)) == 1


async def test_new_sdk_alias_of_known_version_does_not_make_a_new_turn(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    a = await admit(postgres_sessionmaker, interview_id)
    b = await admit(postgres_sessionmaker, interview_id, source="sdk-new-worker")
    assert b.accepted and b.message.id == a.message.id
    async with postgres_sessionmaker() as session:
        assert len(list(await session.scalars(select(db.CaptureSource)))) == 2
        assert (await db.get_conversation(session, interview_id)).state_revision == 1


async def test_unknown_capture_metadata_retains_identical_text_as_distinct_turns(
    postgres_sessionmaker,
):
    interview_id = await seed(postgres_sessionmaker)
    a = await admit(postgres_sessionmaker, interview_id, metrics={"stt_confirmed": True})
    b = await admit(
        postgres_sessionmaker, interview_id, source="sdk-b", metrics={"stt_confirmed": True}
    )
    assert a.message.id != b.message.id and a.turn_id != b.turn_id
    assert a.message.metrics["stt_segmentation"] == "unknown"
    async with postgres_sessionmaker() as session:
        captures = list(
            await session.scalars(select(db.CapturedTurn).order_by(db.CapturedTurn.capture_order))
        )
        assert [c.capture_order for c in captures] == [1, 2]


@pytest.mark.parametrize("kind", ["version_conflict", "version_gap", "source_identity_conflict"])
async def test_capture_conflicts_retain_content_and_do_not_overwrite_evidence(
    postgres_sessionmaker, kind
):
    interview_id = await seed(postgres_sessionmaker)
    first = await admit(postgres_sessionmaker, interview_id)
    source, metadata = "sdk-b", proof()
    if kind == "version_gap":
        metadata = proof(3)
    elif kind == "source_identity_conflict":
        source, metadata = "sdk-a", proof(turn_id="another-turn")
    for _ in range(2):
        result = await admit(
            postgres_sessionmaker, interview_id, "Unresolved content retained", source, metadata
        )
        assert not result.accepted and result.incident == kind
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.capture_integrity_pending and row.state_revision == 2
        assert (await db.get_messages(session, interview_id))[0].content == first.message.content
        incidents = list(await session.scalars(select(db.CaptureIncident)))
        assert (
            len(incidents) == 1 and incidents[0].payload["content"] == "Unresolved content retained"
        )


async def test_late_correction_retains_diagnostic_without_changing_sealed_text(
    postgres_sessionmaker,
):
    interview_id = await seed(postgres_sessionmaker)
    await admit(postgres_sessionmaker, interview_id)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        sealed_at = await session.scalar(select(func.clock_timestamp()))
        row.status, row.transcript_sealed_at = "completed", sealed_at
        row.transcript_integrity = "complete"
        await session.commit()
    result = await admit(
        postgres_sessionmaker, interview_id, "Late corrected answer", "sdk-b", proof(2)
    )
    assert not result.accepted and result.incident == "late_after_seal"
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.transcript_sealed_at == sealed_at and row.capture_integrity_pending
        assert (await db.get_messages(session, interview_id))[0].content == "Initial answer"


async def test_concurrent_same_correction_is_one_version_and_effect(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    await admit(postgres_sessionmaker, interview_id)
    a, b = await asyncio.gather(
        admit(postgres_sessionmaker, interview_id, "Correction", "sdk-b", proof(2)),
        admit(postgres_sessionmaker, interview_id, "Correction", "sdk-c", proof(2)),
    )
    assert a.accepted and b.accepted and a.message.id == b.message.id
    async with postgres_sessionmaker() as session:
        assert len(list(await session.scalars(select(db.MessageVersion)))) == 2
        assert (await db.get_conversation(session, interview_id)).state_revision == 2


async def test_evidence_must_bind_the_supplied_exact_version(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    first = await admit(postgres_sessionmaker, interview_id)
    await admit(postgres_sessionmaker, interview_id, "Correction", "sdk-b", proof(2))
    async with postgres_sessionmaker() as session:
        records = canonical_message_records(await db.get_messages(session, interview_id))
    for version in (None, 1, 3):
        with pytest.raises(ValueError):
            verify_evidence(
                [
                    EvidenceRef(
                        message_id=str(first.message.id),
                        message_version=version,
                        quote="Correction",
                    )
                ],
                records,
            )
    verify_evidence(
        [EvidenceRef(message_id=str(first.message.id), message_version=2, quote="Correction")],
        records,
    )


async def test_replacement_and_correction_share_original_decision_deadline_and_budget(
    postgres_sessionmaker,
):
    interview_id = await seed(postgres_sessionmaker)
    previous = WorkerCoordinator(interview_id, postgres_sessionmaker)
    await previous.claim()
    await admit(previous.sessionmaker, interview_id)
    first = await TurnCoordinator(interview_id, previous.sessionmaker).claim("logical-turn")
    assert first.initial_reservation.ordinal == 1
    async with postgres_sessionmaker() as session:
        execution = await session.get(db.TurnExecution, first.execution_id)
        deadline = execution.decision_deadline_at
        row = await db.get_conversation(session, interview_id)
        row.worker_lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
        await session.commit()
    replacement = WorkerCoordinator(interview_id, postgres_sessionmaker)
    assert await replacement.claim()
    assert (
        await admit(replacement.sessionmaker, interview_id, "Correction", "new-sdk", proof(2))
    ).accepted
    second = await TurnCoordinator(interview_id, replacement.sessionmaker).claim("logical-turn")
    assert second.execution_id == first.execution_id and second.initial_reservation.ordinal == 2
    async with postgres_sessionmaker() as session:
        execution = await session.get(db.TurnExecution, first.execution_id)
        assert execution.invocations_reserved == 2 and execution.decision_deadline_at == deadline
        assert execution.producer_epoch == 2
    with pytest.raises(WorkerOwnershipError):
        await admit(previous.sessionmaker, interview_id, "Old late fragment", "old-sdk", proof(3))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
async def test_nonfinite_capture_keeps_content_as_incident(postgres_sessionmaker, value):
    interview_id = await seed(postgres_sessionmaker)
    result = await admit(postgres_sessionmaker, interview_id, metrics={**proof(), "latency": value})
    assert not result.accepted and result.incident == "invalid_metadata"
    async with postgres_sessionmaker() as session:
        incident = await session.scalar(select(db.CaptureIncident))
        assert incident.payload["content"] == "Initial answer"
        assert incident.payload["metrics"]["latency"] == {"invalid_float": repr(value)}
        assert (await db.get_conversation(session, interview_id)).capture_integrity_pending


async def test_old_message_writer_cannot_mutate_a_version(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    await admit(postgres_sessionmaker, interview_id)
    async with postgres_sessionmaker() as session:
        with pytest.raises(ValueError, match="Versioned messages"):
            await db.insert_message(session, interview_id, "user", "Overwrite", source_id="sdk-a")
    async with postgres_sessionmaker() as session:
        assert (await db.get_messages(session, interview_id))[0].content == "Initial answer"


async def test_capture_order_precedes_delayed_chat_message_claims(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    worker = WorkerCoordinator(interview_id, postgres_sessionmaker)
    await worker.claim()
    await admit(worker.sessionmaker, interview_id, source="a", metrics=proof(turn_id="a"))
    await admit(worker.sessionmaker, interview_id, source="b", metrics=proof(turn_id="b"))
    turns = TurnCoordinator(interview_id, worker.sessionmaker)
    finished, lease = await turns._claim_once("b", uuid.uuid4())
    assert not finished and lease is None
    first = await turns.claim("a")
    async with worker.sessionmaker() as session:
        execution = await session.get(db.TurnExecution, first.execution_id)
        execution.status, execution.owner_id, execution.lease_until = "applied", None, None
        await session.commit()
    second = await turns.claim("b")
    assert second and second.initial_reservation.ordinal == 1


async def test_abandoned_earlier_turn_is_superseded_not_awaited(postgres_sessionmaker):
    """A reply cancelled because the candidate kept talking is never retried:
    the voice session answers the newer turn. Its idle execution must not hold
    the queue until the timeout notice."""
    interview_id = await seed(postgres_sessionmaker)
    worker = WorkerCoordinator(interview_id, postgres_sessionmaker)
    await worker.claim()
    await admit(worker.sessionmaker, interview_id, source="a", metrics=proof(turn_id="a"))
    turns = TurnCoordinator(interview_id, worker.sessionmaker)
    first = await turns.claim("a")
    await turns.release(first)  # cancelled mid-decision, nobody waiting
    await admit(worker.sessionmaker, interview_id, source="b", metrics=proof(turn_id="b"))

    finished, second = await turns._claim_once("b", uuid.uuid4())

    assert finished and second is not None
    async with worker.sessionmaker() as session:
        earlier = await session.get(db.TurnExecution, first.execution_id)
        assert earlier.status == "superseded"
    # Should the abandoned turn be claimed again, it is done: no model call
    # and no technical notice for it.
    assert await turns._claim_once("a", uuid.uuid4()) == (True, None)
    assert not await turns.claim_queue_notice("a")


async def test_unclaimed_earlier_capture_only_blocks_within_the_grace(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    worker = WorkerCoordinator(interview_id, postgres_sessionmaker)
    await worker.claim()
    await admit(worker.sessionmaker, interview_id, source="a", metrics=proof(turn_id="a"))
    await admit(worker.sessionmaker, interview_id, source="b", metrics=proof(turn_id="b"))
    turns = TurnCoordinator(interview_id, worker.sessionmaker)
    # "a" was cancelled before it ever claimed: past the grace it is not
    # coming, and "b" goes ahead.
    async with worker.sessionmaker() as session:
        capture = await session.scalar(
            select(db.CapturedTurn).where(db.CapturedTurn.turn_id == "a")
        )
        capture.captured_at = capture.captured_at - timedelta(seconds=30)
        await session.commit()

    finished, lease = await turns._claim_once("b", uuid.uuid4())

    assert finished and lease is not None


async def test_unfenced_coordinator_rejects_displaced_producer(postgres_sessionmaker):
    from interview_agent.interview.turns import TurnOwnershipError

    interview_id = await seed(postgres_sessionmaker)
    worker = WorkerCoordinator(interview_id, postgres_sessionmaker)
    await worker.claim()
    turns = TurnCoordinator(interview_id, postgres_sessionmaker)
    lease = await turns.claim("a")
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        row.worker_epoch += 1
        await session.commit()
    async with postgres_sessionmaker() as session:
        with pytest.raises(TurnOwnershipError):
            await turns.owned(session, lease)

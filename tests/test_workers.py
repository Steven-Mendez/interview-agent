"""Worker claims, lease authority and domain-write fencing against real PostgreSQL."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from interview_agent.interview import db, workers


async def seed(sessions, *, status="planned"):
    interview_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            db.Conversation(
                id=interview_id,
                status=status,
                job_offer="Synthetic role",
                resume_markdown="Synthetic CV",
                plan={"language": "en"},
                max_minutes=8,
                run_config={"schema_version": 2, "config_version": "frozen"},
            )
        )
        await session.commit()
    return interview_id


async def test_two_workers_claim_one_owner_and_preserve_start(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    a = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    b = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    results = await asyncio.gather(a.claim(), b.claim())
    assert sum(r is not None for r in results) == 1
    owner = a if results[0] else b
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        origin = row.started_at
        assert row.worker_epoch == 1 and row.worker_owner_id == owner.owner_id
        assert row.status == "interviewing" and row.state_revision == 1
        assert row.run_config["config_version"] == "frozen"
    assert await owner.heartbeat()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.started_at == origin and row.worker_epoch == 1 and row.state_revision == 1


async def test_expired_owner_cannot_renew_and_replacement_fences_old_writes(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    old = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    await old.claim()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        origin = row.started_at
        row.worker_lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
        await session.commit()
    assert not await old.heartbeat()
    new = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    assert await new.claim()
    with pytest.raises(workers.WorkerOwnershipError):
        async with old.sessionmaker() as session:
            await db.insert_message(session, interview_id, "user", "Old effect")
    async with new.sessionmaker() as session:
        await db.insert_message(
            session, interview_id, "user", "New confirmed effect", metrics={"stt_confirmed": True}
        )
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.worker_epoch == 2 and row.started_at == origin
        assert [m.content for m in await db.get_messages(session, interview_id)] == [
            "New confirmed effect"
        ]


async def test_session_rechecks_after_an_earlier_commit(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    old = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    await old.claim()
    async with old.sessionmaker() as retained:
        await retained.commit()
        async with postgres_sessionmaker() as session:
            row = await db.get_conversation(session, interview_id)
            row.worker_lease_until = await session.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)
            await session.commit()
        new = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
        await new.claim()
        with pytest.raises(workers.WorkerOwnershipError):
            await db.insert_message(retained, interview_id, "user", "Late pending effect")
        await retained.rollback()
    async with postgres_sessionmaker() as session:
        assert await db.get_messages(session, interview_id) == []


@pytest.mark.parametrize("cause", ["window", "duration"])
async def test_replacement_is_bounded_by_window_and_original_duration(postgres_sessionmaker, cause):
    interview_id = await seed(postgres_sessionmaker)
    old = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    await old.claim()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        now = await session.scalar(select(func.clock_timestamp()))
        row.worker_lease_until = now - timedelta(seconds=31 if cause == "window" else 1)
        if cause == "duration":
            row.started_at = now - timedelta(minutes=9)
        await session.commit()
    assert await workers.WorkerCoordinator(interview_id, postgres_sessionmaker).claim() is None


async def test_closing_lease_outlives_interview_lease_without_renewing_origin(
    postgres_sessionmaker,
):
    interview_id = await seed(postgres_sessionmaker)
    owner = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    await owner.claim()
    async with owner.sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        now = await session.scalar(select(func.clock_timestamp()))
        origin = now - timedelta(seconds=20)
        row.worker_lease_until = now - timedelta(seconds=1)
        row.status = "closing"
        row.closing_owner_id = owner.owner_id
        row.closing_acquired_at = origin
        row.closing_deadline_at = now + timedelta(seconds=15)
        await session.commit()
    assert await owner.heartbeat()
    async with owner.sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.closing_acquired_at == origin
        assert row.worker_lease_until < await session.scalar(select(func.clock_timestamp()))
    assert await workers.WorkerCoordinator(interview_id, postgres_sessionmaker).claim() is None


async def test_heartbeats_only_update_activity_when_observed(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    owner = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    await owner.claim()
    async with postgres_sessionmaker() as session:
        initial = (await db.get_conversation(session, interview_id)).worker_activity_at
    assert await owner.heartbeat()
    async with postgres_sessionmaker() as session:
        assert (await db.get_conversation(session, interview_id)).worker_activity_at == initial
    owner.note_activity()
    assert await owner.heartbeat()
    async with postgres_sessionmaker() as session:
        assert (await db.get_conversation(session, interview_id)).worker_activity_at > initial


async def test_fence_uses_database_time_after_waiting_for_the_row_lock(postgres_sessionmaker):
    interview_id = await seed(postgres_sessionmaker)
    owner = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    await owner.claim()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        row.worker_lease_until = await session.scalar(select(func.clock_timestamp())) + timedelta(
            seconds=0.2
        )
        await session.commit()
    async with postgres_sessionmaker() as blocker:
        await blocker.scalar(
            select(db.Conversation).where(db.Conversation.id == interview_id).with_for_update()
        )

        async def delayed_write():
            async with owner.sessionmaker() as session:
                await db.insert_message(session, interview_id, "user", "Expired while waiting")

        write = asyncio.create_task(delayed_write())
        await asyncio.sleep(0.3)
        await blocker.commit()
        with pytest.raises(workers.WorkerOwnershipError):
            await write
    async with postgres_sessionmaker() as session:
        assert await db.get_messages(session, interview_id) == []

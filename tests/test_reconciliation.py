"""Browser-independent lifecycle recovery against PostgreSQL's clock."""

import asyncio
import logging
import time
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from interview_agent.config import Settings
from interview_agent.interview import db, workers
from interview_agent.server import reconciliation
from interview_agent.server.reconciliation import (
    CONNECT_TIMEOUTS_BEFORE_ERROR,
    DatabaseConnectTimeoutError,
    LifecycleSweeper,
    reconcile_interview,
)

LIMITS = Settings(_env_file=None)


async def started(sessions):
    interview_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            db.Conversation(
                id=interview_id,
                status="planned",
                plan={"language": "es"},
                resume_markdown="Synthetic CV",
                job_offer="Synthetic role",
                max_minutes=8,
            )
        )
        await session.commit()
    owner = workers.WorkerCoordinator(interview_id, sessions)
    await owner.claim()
    return interview_id, owner


async def age(sessions, interview_id, **offsets):
    async with sessions() as session:
        row = await db.get_conversation(session, interview_id)
        now = await session.scalar(select(func.clock_timestamp()))
        for name, seconds in offsets.items():
            setattr(row, name, now + timedelta(seconds=seconds))
        await session.commit()


async def reconcile(sessions, interview_id):
    async with sessions() as session:
        return await reconcile_interview(session, interview_id, LIMITS)


async def test_sweep_preserves_replacement_window_then_seals_without_browser(
    postgres_sessionmaker, recorded_metrics
):
    interview_id, old = await started(postgres_sessionmaker)
    await age(postgres_sessionmaker, interview_id, worker_lease_until=-1)
    assert await reconcile(postgres_sessionmaker, interview_id) is None
    async with postgres_sessionmaker() as session:
        assert (await db.get_conversation(session, interview_id)).status == "interviewing"
    await age(postgres_sessionmaker, interview_id, worker_lease_until=-31)
    assert await LifecycleSweeper(postgres_sessionmaker, LIMITS).once() == (1, 0)
    assert await reconcile(postgres_sessionmaker, interview_id) is None
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.status == "completed" and row.ended_reason == "worker_lost"
        assert row.farewell_status == "not_possible" and row.transcript_integrity == "partial"
        assert row.stt_drain["complete"] is False and row.transcript_sealed_at
        assert row.worker_epoch == 1 and row.worker_owner_id == old.owner_id
    assert await workers.WorkerCoordinator(interview_id, postgres_sessionmaker).claim() is None
    # A server-reconciled close still counts in the closing outcome denominator,
    # with no candidate or interview identity.
    outcomes = {
        name: [
            (point.count, point.sum, dict(point.attributes))
            for point in recorded_metrics(f"interview_agent.closing.{name}")
        ]
        for name in ("reconciled", "playback_confirmed")
    }
    labels = {"farewell_status": "not_possible", "source": "lifecycle_sweeper"}
    assert outcomes == {"reconciled": [(1, 1, labels)], "playback_confirmed": [(1, 0, labels)]}


@pytest.mark.parametrize("limit", ["duration", "idle", "disconnect"])
async def test_lifecycle_limits_clamp_replacement_window(postgres_sessionmaker, limit):
    interview_id, owner = await started(postgres_sessionmaker)
    offsets = {"worker_lease_until": -1}
    offsets[
        {
            "duration": "started_at",
            "idle": "worker_activity_at",
            "disconnect": "worker_disconnected_at",
        }[limit]
    ] = {
        "duration": -481,
        "idle": -181,
        "disconnect": -11,
    }[limit]
    await age(postgres_sessionmaker, interview_id, **offsets)
    assert await reconcile(postgres_sessionmaker, interview_id) == "sealed_partial"
    assert not await owner.heartbeat()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.ended_reason == ("worker_lost" if limit == "duration" else "abandoned")
        assert row.farewell_status == "not_possible"


async def test_live_worker_is_asked_to_close_before_bounded_server_fallback(postgres_sessionmaker):
    interview_id, owner = await started(postgres_sessionmaker)
    await age(postgres_sessionmaker, interview_id, started_at=-481)
    assert await reconcile(postgres_sessionmaker, interview_id) == "closing_requested"
    assert await owner.heartbeat()
    assert owner.closing_reason == "timeout"
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        origin, closing_id = row.closing_started_at, row.closing_id
        assert row.closing_owner_id is None and row.transcript_sealed_at is None
    assert await reconcile(postgres_sessionmaker, interview_id) is None
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.closing_started_at == origin and row.closing_id == closing_id
    await age(postgres_sessionmaker, interview_id, closing_started_at=-36)
    assert await reconcile(postgres_sessionmaker, interview_id) == "sealed_partial"
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.farewell_status == "not_possible" and row.ended_reason == "timeout"


async def test_claim_and_sweep_race_cannot_seal_a_valid_replacement(postgres_sessionmaker):
    interview_id, old = await started(postgres_sessionmaker)
    await age(postgres_sessionmaker, interview_id, worker_lease_until=-1)
    new = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    claim, sweep = await asyncio.gather(new.claim(), reconcile(postgres_sessionmaker, interview_id))
    assert claim is not None and sweep is None
    assert not await old.heartbeat()
    async with new.sessionmaker() as session:
        assert (await db.get_conversation(session, interview_id)).status == "interviewing"


async def test_recovered_closing_gets_fixed_adoption_window_without_replay(postgres_sessionmaker):
    interview_id, old = await started(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        now = await session.scalar(select(func.clock_timestamp()))
        row.status = "closing"
        row.closing_id, row.closing_attempt_id = uuid.uuid4(), uuid.uuid4()
        row.closing_stream_id = "existing-stream"
        row.closing_owner_id = old.owner_id
        row.closing_acquired_at = now - timedelta(seconds=40)
        row.closing_deadline_at = now - timedelta(seconds=1)
        row.worker_lease_until = now - timedelta(seconds=1)
        await session.commit()
    new = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    assert await new.claim()
    assert await reconcile(postgres_sessionmaker, interview_id) is None
    # A heartbeat cannot prolong the adoption gap beyond initial acquisition.
    await age(postgres_sessionmaker, interview_id, worker_acquired_at=-16, worker_lease_until=10)
    assert await reconcile(postgres_sessionmaker, interview_id) == "sealed_partial"
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.closing_stream_id == "existing-stream" and row.closing_owner_id == old.owner_id
        assert row.farewell_status == "timeout"
        assert await db.get_messages(session, interview_id) == []


async def test_disconnect_duplicate_does_not_reset_grace_and_reconnect_clears_it(
    postgres_sessionmaker,
):
    interview_id, owner = await started(postgres_sessionmaker)
    await owner.set_connected(False)
    async with postgres_sessionmaker() as session:
        first = (await db.get_conversation(session, interview_id)).worker_disconnected_at
    await owner.set_connected(False)
    async with postgres_sessionmaker() as session:
        assert (await db.get_conversation(session, interview_id)).worker_disconnected_at == first
    await owner.set_connected(True)
    async with postgres_sessionmaker() as session:
        assert (await db.get_conversation(session, interview_id)).worker_disconnected_at is None


async def test_monitor_fails_closed_without_waiting_for_local_expiration(
    postgres_sessionmaker,
    monkeypatch,
):
    _interview_id, owner = await started(postgres_sessionmaker)
    monkeypatch.setattr(workers, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(owner, "heartbeat", AsyncMock(side_effect=TimeoutError))
    lost = AsyncMock()
    start = time.monotonic()
    await asyncio.wait_for(owner.monitor(lost), 1)
    assert owner.lost and time.monotonic() - start < 1
    lost.assert_awaited_once()
    with pytest.raises(workers.WorkerOwnershipError):
        owner.require_local()


async def test_server_health_metrics_have_no_candidate_identity(
    postgres_sessionmaker, recorded_metrics, monkeypatch
):
    interview_id, _owner = await started(postgres_sessionmaker)
    await age(postgres_sessionmaker, interview_id, worker_lease_until=-31)
    sleep = asyncio.sleep

    async def one_sweep(seconds, *args, **kwargs):
        if seconds >= 1:  # the wait for the next sweep, SWEEP_SECONDS later
            raise asyncio.CancelledError
        return await sleep(seconds, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", one_sweep)
    with pytest.raises(asyncio.CancelledError):
        await LifecycleSweeper(postgres_sessionmaker, LIMITS).run()
    sweep = {}
    for name in ("lag_seconds", "reconciled", "errors", "duration_seconds"):
        (sweep[name],) = recorded_metrics(f"interview_agent.server.sweep_{name}")
        assert dict(sweep[name].attributes) == {"source": "lifecycle_sweeper"}
    assert (sweep["reconciled"].sum, sweep["errors"].sum) == (1, 0)


async def test_delayed_disconnect_cannot_overwrite_a_newer_reconnect(postgres_sessionmaker):
    interview_id, owner = await started(postgres_sessionmaker)
    delayed_disconnect = owner.set_connected(False)
    await owner.set_connected(True)
    await delayed_disconnect
    async with postgres_sessionmaker() as session:
        assert (await db.get_conversation(session, interview_id)).worker_disconnected_at is None


async def test_previous_workers_delayed_disconnect_is_rejected_after_takeover(
    postgres_sessionmaker,
):
    interview_id, previous = await started(postgres_sessionmaker)
    delayed_disconnect = previous.set_connected(False)
    await age(postgres_sessionmaker, interview_id, worker_lease_until=-1)
    replacement = workers.WorkerCoordinator(interview_id, postgres_sessionmaker)
    assert await replacement.claim()
    await replacement.set_connected(True)
    with pytest.raises(workers.WorkerOwnershipError):
        await delayed_disconnect
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.worker_disconnected_at is None and row.worker_owner_id == replacement.owner_id


async def test_expired_inactivity_cannot_be_reset_by_replacement(postgres_sessionmaker):
    interview_id, _owner = await started(postgres_sessionmaker)
    await age(postgres_sessionmaker, interview_id, worker_lease_until=-1, worker_activity_at=-181)
    assert await workers.WorkerCoordinator(interview_id, postgres_sessionmaker).claim() is None
    assert await reconcile(postgres_sessionmaker, interview_id) == "sealed_partial"


class SlowSession:
    """A session whose connect and inventory query each take a fixed time."""

    def __init__(self, connect=0.0, query=0.0):
        self.connect_seconds = connect
        self.query_seconds = query

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def connection(self):
        await asyncio.sleep(self.connect_seconds)

    async def scalars(self, query):
        await asyncio.sleep(self.query_seconds)
        return iter([])


async def test_sweep_connect_has_its_own_budget_beyond_the_query_one(monkeypatch):
    monkeypatch.setattr(reconciliation, "DATABASE_SECONDS", 0.05)
    monkeypatch.setattr(reconciliation, "CONNECT_SECONDS", 0.5)
    # A connect slower than a query is allowed to be, but within CONNECT_SECONDS.
    assert await LifecycleSweeper(lambda: SlowSession(connect=0.1), LIMITS).once() == (0, 0)
    with pytest.raises(DatabaseConnectTimeoutError):
        await LifecycleSweeper(lambda: SlowSession(connect=1), LIMITS).once()
    # Queries keep the short budget, and a slow query is not a connect timeout.
    with pytest.raises(TimeoutError) as failure:
        await LifecycleSweeper(lambda: SlowSession(query=0.1), LIMITS).once()
    assert not isinstance(failure.value, DatabaseConnectTimeoutError)


async def test_isolated_connect_timeouts_warn_and_a_run_of_them_errors(monkeypatch, caplog):
    sweeper = LifecycleSweeper(None, LIMITS)
    outcomes = [DatabaseConnectTimeoutError()] * CONNECT_TIMEOUTS_BEFORE_ERROR
    outcomes += [(0, 0), DatabaseConnectTimeoutError(), RuntimeError("other")]

    async def once():
        if not outcomes:
            raise asyncio.CancelledError
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    metrics = []
    monkeypatch.setattr(sweeper, "once", once)
    monkeypatch.setattr(sweeper, "metric", lambda name, value: metrics.append((name, value)))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    with (
        caplog.at_level(logging.WARNING, logger=reconciliation.logger.name),
        pytest.raises(asyncio.CancelledError),
    ):
        await sweeper.run()
    records = [r for r in caplog.records if r.name == reconciliation.logger.name]
    # Two blips are warnings, the third in a row is an error, a successful
    # sweep resets the count, and any other failure is still an error.
    assert [r.levelno for r in records] == [
        logging.WARNING,
        logging.WARNING,
        logging.ERROR,
        logging.WARNING,
        logging.ERROR,
    ]
    assert records[0].consecutive == 1 and records[-2].consecutive == 1
    assert metrics.count(("sweep_errors", 1)) == CONNECT_TIMEOUTS_BEFORE_ERROR + 2

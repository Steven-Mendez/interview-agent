"""Server-owned lifecycle recovery, independent of browser and LiveKit jobs."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import func, select

from interview_agent import otel_metrics
from interview_agent.interview import db
from interview_agent.interview.workers import (
    DATABASE_SECONDS,
    LEASE_SECONDS,
    REPLACEMENT_SECONDS,
    last_activity,
)
from interview_agent.playback import confirmation_source

logger = logging.getLogger(__name__)
SWEEP_SECONDS = 5
# Opening a connection to Neon (TLS verify-full, a compute that is waking up
# or was just restarted) regularly takes longer than a query is allowed to.
CONNECT_SECONDS = 10
# Consecutive sweeps lost to connect timeouts before it stops being a blip.
CONNECT_TIMEOUTS_BEFORE_ERROR = 3


class DatabaseConnectTimeoutError(TimeoutError):
    """No connection within CONNECT_SECONDS: nothing was queried or written."""


async def reconcile_interview(session, conversation_id, settings):
    """Lock the canonical row; recover only when its durable bounds permit it."""
    conversation = await session.scalar(
        select(db.Conversation)
        .where(db.Conversation.id == conversation_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if conversation is None or conversation.status not in ("interviewing", "closing"):
        return None
    now = await session.scalar(select(func.clock_timestamp()))
    closing_seconds = settings.closing_timeout_seconds + 15
    if conversation.status == "closing":
        deadline = conversation.closing_deadline_at or (
            conversation.closing_started_at + timedelta(seconds=closing_seconds)
        )
        if deadline > now:
            return None
        # A replacement acquired after the previous close expired gets only
        # its initial acquisition window to adopt that close. Heartbeats do
        # not keep this gap open forever, and no audio is replayed here.
        if (
            conversation.closing_deadline_at is not None
            and conversation.worker_owner_id != conversation.closing_owner_id
            and conversation.worker_acquired_at is not None
            and conversation.worker_acquired_at >= deadline
            and conversation.worker_acquired_at + timedelta(seconds=LEASE_SECONDS) > now
            and conversation.worker_lease_until is not None
            and conversation.worker_lease_until > now
        ):
            return None
        reason = conversation.ended_reason or "worker_lost"
        farewell = (
            conversation.farewell_status
            if conversation.farewell_status in ("played", "failed")
            else "timeout"
            if conversation.closing_owner_id
            else "not_possible"
        )
    else:
        # Every interviewing row has an origin: the worker claim fixes it.
        origin = conversation.started_at
        duration_due = now >= origin + timedelta(minutes=conversation.max_minutes)
        activity = last_activity(conversation, origin)
        idle_due = now >= activity + timedelta(minutes=settings.interview_idle_minutes)
        disconnected_due = (
            conversation.worker_disconnected_at is not None
            and now
            >= conversation.worker_disconnected_at
            + timedelta(seconds=settings.interview_reconnect_seconds)
        )
        worker_live = conversation.worker_lease_until is not None and (
            conversation.worker_lease_until > now
        )
        lost_due = conversation.worker_lease_until is not None and (
            now >= conversation.worker_lease_until + timedelta(seconds=REPLACEMENT_SECONDS)
        )
        reason = (
            "timeout"
            if duration_due
            else "abandoned"
            if idle_due or disconnected_due
            else "worker_lost"
            if lost_due
            else None
        )
        if reason is None:
            return None
        if not worker_live and conversation.worker_lease_until is not None and duration_due:
            reason = "worker_lost"
        if worker_live:
            # Ask the live worker to perform the normal farewell. If it fails
            # to acquire closing ownership, the fixed pending deadline above
            # will eventually seal the interview without pretending playback.
            conversation.status = "closing"
            conversation.closing_id = conversation.closing_id or uuid.uuid4()
            conversation.closing_started_at = conversation.closing_started_at or now
            conversation.ended_reason = reason
            conversation.state_revision += 1
            await session.commit()
            return "closing_requested"
        # Duration and abandonment clamp the replacement window. A worker
        # that died can never be dispatched again solely to say goodbye.
        farewell = "not_possible"
    conversation.status = "completed"
    conversation.transcript_sealed_at = now
    conversation.transcript_integrity = "partial"
    conversation.stt_drain = {
        **(conversation.stt_drain or {}),
        "complete": False,
        "reason": "server_reconciled",
    }
    conversation.ended_reason = reason
    conversation.farewell_status = farewell
    conversation.state_revision += 1
    from interview_agent.interview.seals import ensure_seal

    await ensure_seal(session, conversation)
    await session.commit()
    # Every close needs an outcome sample: worker-run closes emit theirs, and
    # server-reconciled ones complete the denominator for the failure rate.
    dimensions = {"farewell_status": farewell, "source": "lifecycle_sweeper"}
    if source := confirmation_source(conversation):
        dimensions["confirmation_source"] = source
    for name, value in (
        ("reconciled", 1),
        ("playback_confirmed", int(farewell == "played")),
    ):
        otel_metrics.record("closing", name, value, dimensions)
    return "sealed_partial"


class LifecycleSweeper:
    def __init__(self, sessionmaker, settings, evaluations=None):
        self.evaluations = evaluations
        self.sessions = sessionmaker
        self.settings = settings
        self.connect_timeouts = 0

    @asynccontextmanager
    async def session(self):
        """A session with its connection already open. The connect (pool
        checkout, pre-ping, a reconnect to Neon) gets CONNECT_SECONDS; the
        queries inside the block keep the DATABASE_SECONDS budget."""
        async with self.sessions() as session:
            try:
                async with asyncio.timeout(CONNECT_SECONDS):
                    await session.connection()
            except TimeoutError as error:
                raise DatabaseConnectTimeoutError(
                    f"no database connection within {CONNECT_SECONDS}s"
                ) from error
            async with asyncio.timeout(DATABASE_SECONDS):
                yield session

    async def once(self):
        changed = failures = 0
        # Page by immutable IDs so a large set of live rows cannot starve a
        # later expired row. Release the inventory connection before writes.
        cursor = None
        while True:
            async with self.session() as session:
                query = (
                    select(db.Conversation.id)
                    .where(db.Conversation.status.in_(("interviewing", "closing")))
                    .order_by(db.Conversation.id)
                    .limit(256)
                )
                if cursor is not None:
                    query = query.where(db.Conversation.id > cursor)
                ids = list(await session.scalars(query))
            for interview_id in ids:
                try:
                    async with self.session() as session:
                        changed += bool(
                            await reconcile_interview(session, interview_id, self.settings)
                        )
                except DatabaseConnectTimeoutError:
                    # The database is unreachable, not this row: abort the
                    # sweep instead of waiting CONNECT_SECONDS per row.
                    raise
                except Exception:
                    failures += 1
                    logger.exception("Lifecycle reconciliation failed")
            if len(ids) < 256:
                break
            cursor = ids[-1]
        if self.evaluations is not None:
            changed += await self.evaluations.reconcile_pending()
        return changed, failures

    def metric(self, name, value):
        # Server health: no interview or candidate behind these samples.
        otel_metrics.record("server", name, value, {"source": "lifecycle_sweeper"})

    async def run(self):
        next_sweep = time.monotonic()
        while True:
            started = time.monotonic()
            self.metric("sweep_lag_seconds", max(0, started - next_sweep))
            try:
                changed, failures = await self.once()
                self.connect_timeouts = 0
                self.metric("sweep_reconciled", changed)
                self.metric("sweep_errors", failures)
            except asyncio.CancelledError:
                raise
            except DatabaseConnectTimeoutError:
                # An isolated slow connect (Neon waking up, a pod that was
                # just replaced) retries in SWEEP_SECONDS; only a run of them
                # is an outage worth paging about.
                self.connect_timeouts += 1
                self.metric("sweep_errors", 1)
                if self.connect_timeouts < CONNECT_TIMEOUTS_BEFORE_ERROR:
                    logger.warning(
                        "Lifecycle sweep skipped: database connect timed out",
                        extra={"consecutive": self.connect_timeouts},
                    )
                else:
                    logger.exception("Lifecycle sweep failed")
            except Exception:
                logger.exception("Lifecycle sweep failed")
                self.metric("sweep_errors", 1)
            self.metric("sweep_duration_seconds", time.monotonic() - started)
            next_sweep += SWEEP_SECONDS
            # If a sweep was slow, don't busy-loop to replay missed cycles.
            next_sweep = max(next_sweep, time.monotonic())
            await asyncio.sleep(max(0, next_sweep - time.monotonic()))

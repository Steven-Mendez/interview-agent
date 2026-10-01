"""PostgreSQL worker ownership and fenced sessions shared by all worker paths."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview_agent.interview import db
from interview_agent.interview.turns import TurnOwnershipError

HEARTBEAT_SECONDS = 5
LEASE_SECONDS = 15
REPLACEMENT_SECONDS = 30
DATABASE_SECONDS = 2


def reconnect_deadline(conversation, *, reconnect_seconds=10, closing_seconds=35):
    """Durable bounds; callers compare them only with PostgreSQL's clock."""
    if conversation.status == "closing":
        return conversation.closing_deadline_at or (
            conversation.closing_started_at + timedelta(seconds=closing_seconds)
        )
    deadline = conversation.started_at + timedelta(minutes=conversation.max_minutes)
    if conversation.worker_lease_until is not None:
        deadline = min(
            deadline, conversation.worker_lease_until + timedelta(seconds=REPLACEMENT_SECONDS)
        )
    if conversation.worker_disconnected_at is not None:
        deadline = min(
            deadline, conversation.worker_disconnected_at + timedelta(seconds=reconnect_seconds)
        )
    return deadline


def last_activity(conversation, origin):
    return conversation.worker_activity_at or origin


class WorkerOwnershipError(TurnOwnershipError):
    """A displaced or expired worker cannot commit domain writes or speak."""


@dataclass(frozen=True)
class WorkerLease:
    conversation_id: uuid.UUID
    owner_id: uuid.UUID
    epoch: int
    local_deadline: float
    phase: str

    @property
    def remaining(self):
        return max(0, self.local_deadline - time.monotonic())


async def check_fence(session, fence):
    # Read columns rather than refreshing an ORM Conversation: callers may
    # have pending changes to that object which must not be discarded.
    with session.no_autoflush:
        row = (
            await session.execute(
                select(
                    db.Conversation.worker_owner_id,
                    db.Conversation.worker_epoch,
                    db.Conversation.worker_lease_until,
                    db.Conversation.closing_owner_id,
                    db.Conversation.closing_deadline_at,
                )
                .where(db.Conversation.id == fence.conversation_id)
                .with_for_update()
            )
        ).one_or_none()
    if row is None:
        raise WorkerOwnershipError("Interview no longer exists")
    # Evaluate time after acquiring the lock: a clock expression in the
    # locking SELECT can be evaluated before waiting for another transaction.
    now = await session.scalar(select(func.clock_timestamp()))
    owner, epoch, worker_until, closing_owner, closing_until = row
    live = worker_until is not None and worker_until > now
    live_closing = closing_owner == fence.owner_id and closing_until and closing_until > now
    if owner != fence.owner_id or epoch != fence.epoch or not (live or live_closing):
        raise WorkerOwnershipError("Worker ownership is no longer current")


class FencedSession(AsyncSession):
    def __init__(self, *args, worker_fence, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker_fence = worker_fence

    async def __aenter__(self):
        await super().__aenter__()
        try:
            async with asyncio.timeout(DATABASE_SECONDS):
                await check_fence(self, self.worker_fence)
        except BaseException:
            await self.close()
            raise
        return self

    async def commit(self):
        # Recheck even after a helper committed earlier in this same session.
        # The conversation lock covers this check through the actual commit.
        async with asyncio.timeout(DATABASE_SECONDS):
            await check_fence(self, self.worker_fence)
            await super().commit()


class WorkerCoordinator:
    def __init__(
        self,
        conversation_id,
        sessionmaker,
        *,
        reconnect_seconds=10,
        closing_seconds=35,
        idle_minutes=3,
    ):
        self.conversation_id = conversation_id
        self.raw_sessions = sessionmaker
        self.reconnect_seconds = reconnect_seconds
        self.closing_seconds = closing_seconds
        self.idle_minutes = idle_minutes
        self.connectivity_generation = 0
        self.closing_reason = None
        self.owner_id = uuid.uuid4()
        self.lease = None
        self.lost = False
        self.activity_generation = 0
        self.persisted_activity_generation = 0

    def require_local(self):
        if self.lost or self.lease is None or self.lease.remaining <= 0:
            raise WorkerOwnershipError("Worker local ownership window expired")

    def note_activity(self):
        self.activity_generation += 1

    @property
    def sessionmaker(self):
        if self.lease is None:
            raise WorkerOwnershipError("Worker has not acquired ownership")
        return async_sessionmaker(
            class_=FencedSession,
            worker_fence=self.lease,
            **self.raw_sessions.kw,
        )

    async def claim(self):
        async with asyncio.timeout(DATABASE_SECONDS), self.raw_sessions() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            if (
                conversation is None
                or conversation.status not in ("planned", "interviewing", "closing")
                or conversation.plan is None
                or conversation.transcript_sealed_at is not None
            ):
                return None
            clock_start = time.monotonic()
            now = await session.scalar(select(func.clock_timestamp()))
            if conversation.worker_lease_until is not None:
                if conversation.worker_lease_until > now:
                    return None
                if conversation.status != "closing" and now >= (
                    conversation.worker_lease_until + timedelta(seconds=REPLACEMENT_SECONDS)
                ):
                    return None
            if conversation.status == "closing":
                if (
                    conversation.closing_deadline_at is not None
                    and conversation.closing_deadline_at > now
                ):
                    return None
                if conversation.closing_deadline_at is None and now >= (
                    conversation.closing_started_at + timedelta(seconds=self.closing_seconds)
                ):
                    return None
            else:
                # The first claim fixes the origin; reconnects never move it.
                origin = conversation.started_at or now
                if now >= origin + timedelta(minutes=conversation.max_minutes):
                    return None
                activity = last_activity(conversation, origin)
                if now >= activity + timedelta(minutes=self.idle_minutes):
                    return None
                if conversation.worker_disconnected_at is not None and now >= (
                    conversation.worker_disconnected_at + timedelta(seconds=self.reconnect_seconds)
                ):
                    return None
                conversation.started_at = origin
                conversation.worker_activity_at = activity
                conversation.status = "interviewing"
            conversation.worker_owner_id = self.owner_id
            conversation.worker_epoch += 1
            conversation.worker_acquired_at = now
            conversation.worker_lease_until = now + timedelta(seconds=LEASE_SECONDS)
            conversation.worker_activity_at = conversation.worker_activity_at or now
            conversation.state_revision += 1
            lease = WorkerLease(
                self.conversation_id,
                self.owner_id,
                conversation.worker_epoch,
                clock_start + LEASE_SECONDS,
                conversation.status,
            )
            await session.commit()
        self.lease = lease
        return lease

    async def heartbeat(self):
        """Expired owners cannot renew; closing has its own separate lease."""
        if self.lease is None or self.lost:
            return False
        async with asyncio.timeout(DATABASE_SECONDS), self.raw_sessions() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            clock_start = time.monotonic()
            now = await session.scalar(select(func.clock_timestamp()))
            activity_generation = self.activity_generation
            wrote_activity = False
            if conversation is None or (
                conversation.worker_owner_id != self.owner_id
                or conversation.worker_epoch != self.lease.epoch
            ):
                return False
            if (
                conversation.closing_owner_id == self.owner_id
                and conversation.closing_deadline_at is not None
                and conversation.closing_deadline_at > now
            ):
                until = conversation.closing_deadline_at
            elif conversation.status == "closing" and conversation.closing_deadline_at is not None:
                return False
            elif conversation.status in ("interviewing", "closing") and (
                conversation.worker_lease_until is not None
                and conversation.worker_lease_until > now
            ):
                until = now + timedelta(seconds=LEASE_SECONDS)
                conversation.worker_lease_until = until
                if activity_generation != self.persisted_activity_generation:
                    conversation.worker_activity_at = now
                    wrote_activity = True
            else:
                return False
            lease = WorkerLease(
                self.conversation_id,
                self.owner_id,
                self.lease.epoch,
                clock_start + (until - now).total_seconds(),
                conversation.status,
            )
            await session.commit()
            self.closing_reason = (
                (conversation.ended_reason or "connection_lost")
                if conversation.status == "closing"
                else None
            )
            if wrote_activity:
                self.persisted_activity_generation = activity_generation
        self.lease = lease
        return True

    def set_connected(self, connected):
        # Capture order synchronously in the SDK callback, before a task can
        # wait for a connection or row lock. Late old events cannot overwrite
        # a newer reconnect even if their DB operations complete out of order.
        self.connectivity_generation += 1
        return self._persist_connectivity(connected, self.connectivity_generation)

    async def _persist_connectivity(self, connected, generation):
        async with asyncio.timeout(DATABASE_SECONDS), self.sessionmaker() as session:
            if generation != self.connectivity_generation:
                return
            conversation = await session.get(db.Conversation, self.conversation_id)
            now = await session.scalar(select(func.clock_timestamp()))
            # Duplicate disconnect notifications cannot restart the grace.
            conversation.worker_disconnected_at = (
                None if connected else conversation.worker_disconnected_at or now
            )
            await session.commit()

    async def monitor(self, on_lost, on_close=None):
        """Fail closed on an uncertain DB renewal before admitting more speech."""
        try:
            while True:
                await asyncio.sleep(min(HEARTBEAT_SECONDS, self.lease.remaining))
                self.require_local()
                if not await self.heartbeat():
                    break
                if self.closing_reason and on_close is not None:
                    await on_close(self.closing_reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        self.lost = True
        await on_lost()

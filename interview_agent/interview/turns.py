"""Cross-worker turn serialization and reservations, without holding a DB lock at the model."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from interview_agent.interview import db

DECISION_SECONDS = 15
QUEUE_SECONDS = 30
WAITER_SECONDS = 3
MAX_INVOCATIONS = 2
CLEANUP_SECONDS = 1
logger = logging.getLogger(__name__)


class DecisionLimitError(ValueError):
    """The same logical turn cannot obtain a fresh decision budget."""


class TurnOwnershipError(ValueError):
    """A late former owner must produce neither effects nor speech."""


class TurnContextChangedError(ValueError):
    """Reload changed input before starting an already reserved invocation."""


class TurnQueueTimeoutError(TimeoutError):
    """Waiting for a serialized turn exhausted its separate queue budget."""

    def __init__(self, turn_id, waiter_id):
        super().__init__("Interview turn queue deadline exhausted")
        self.turn_id = turn_id
        self.waiter_id = waiter_id


class TurnDecisionError(ValueError):
    def __init__(self, cause, lease):
        super().__init__(str(cause))
        self.error_type = type(cause).__name__
        self.lease = lease


@dataclass(frozen=True)
class TurnLease:
    execution_id: uuid.UUID
    owner_id: uuid.UUID
    turn_id: str
    local_deadline: float
    initial_reservation: InvocationReservation | None = None

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.local_deadline - time.monotonic())


@dataclass(frozen=True)
class InvocationReservation:
    id: uuid.UUID
    ordinal: int
    local_deadline: float

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.local_deadline - time.monotonic())


class TurnCoordinator:
    def __init__(self, conversation_id, sessionmaker):
        self.conversation_id = conversation_id
        self.sessionmaker = sessionmaker

    async def claim(self, turn_id: str, *, queue_deadline: float | None = None) -> TurnLease | None:
        owner_id = uuid.uuid4()
        queue_deadline = (
            queue_deadline if queue_deadline is not None else time.monotonic() + QUEUE_SECONDS
        )
        try:
            async with asyncio.timeout_at(queue_deadline):
                return await self._claim(turn_id, owner_id)
        except TimeoutError as exc:
            raise TurnQueueTimeoutError(turn_id, owner_id) from exc
        finally:
            try:
                async with asyncio.timeout(CLEANUP_SECONDS):
                    await self.withdraw(turn_id, owner_id)
            except (TimeoutError, SQLAlchemyError, TurnOwnershipError) as exc:
                logger.warning("Turn waiter cleanup failed: %s", type(exc).__name__)

    async def _claim(self, turn_id: str, owner_id) -> TurnLease | None:
        while True:
            try:
                async with asyncio.timeout(CLEANUP_SECONDS):
                    finished, claimed = await self._claim_once(turn_id, owner_id)
                if finished:
                    return claimed
            except TimeoutError:
                # Lock contention is not a terminal claim failure. Retry within
                # the outer queue deadline; waiter expiry is independent of it.
                pass
            await asyncio.sleep(0.1)

    async def _claim_once(self, turn_id: str, owner_id):
        failure = None
        claimed = None
        async with self.sessionmaker() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.status != "interviewing":
                return True, None
            applied = await session.scalar(
                select(db.TurnRun.id).where(
                    db.TurnRun.conversation_id == self.conversation_id,
                    db.TurnRun.turn_id == turn_id,
                )
            )
            if applied is not None:
                return True, None
            clock_start = time.monotonic()
            now = await session.scalar(select(func.clock_timestamp()))
            executions = list(
                await session.scalars(
                    select(db.TurnExecution)
                    .where(db.TurnExecution.conversation_id == self.conversation_id)
                    .order_by(db.TurnExecution.created_at, db.TurnExecution.id)
                )
            )
            captures = list(
                await session.scalars(
                    select(db.CapturedTurn)
                    .where(db.CapturedTurn.conversation_id == self.conversation_id)
                    .order_by(db.CapturedTurn.capture_order)
                )
            )
            capture_order = {capture.turn_id: capture.capture_order for capture in captures}
            executions.sort(
                key=lambda item: (capture_order.get(item.turn_id, 0), item.created_at, str(item.id))
            )
            execution = next((item for item in executions if item.turn_id == turn_id), None)
            if execution is None:
                execution = db.TurnExecution(
                    id=uuid.uuid4(),
                    conversation_id=self.conversation_id,
                    turn_id=turn_id,
                    created_at=now,
                    status="queued",
                    invocations_reserved=0,
                    waiters={},
                )
                session.add(execution)
                executions.append(execution)
                executions.sort(
                    key=lambda item: (
                        capture_order.get(item.turn_id, 0),
                        item.created_at,
                        str(item.id),
                    )
                )
            execution.waiters = {
                **execution.waiters,
                str(owner_id): (now + timedelta(seconds=WAITER_SECONDS)).timestamp(),
            }
            if execution.status == "idle":
                execution.status = "queued"
            for item in executions:
                item.waiters = {
                    key: expiry for key, expiry in item.waiters.items() if expiry > now.timestamp()
                }
                if item.status not in ("queued", "running"):
                    continue
                live_owner = (
                    item.owner_id is not None
                    and item.lease_until is not None
                    and item.lease_until > now
                    and (
                        item.producer_owner_id is None
                        or (
                            item.producer_owner_id == conversation.worker_owner_id
                            and item.producer_epoch == conversation.worker_epoch
                        )
                    )
                )
                if live_owner:
                    continue
                expired = item.decision_deadline_at is not None and item.decision_deadline_at <= now
                if expired or item.invocations_reserved >= MAX_INVOCATIONS:
                    item.status = "failed"
                    item.owner_id = None
                    item.lease_until = None
                elif item.owner_id is not None:
                    item.status = "queued" if item.waiters else "idle"
                    item.owner_id = None
                    item.lease_until = None
                elif not item.waiters:
                    item.status = "idle"
            if execution.status == "failed":
                failure = DecisionLimitError("Interview decision budget or deadline exhausted")
            elif execution.status == "applied":
                return True, None
            else:
                running = any(item.status == "running" for item in executions)
                waiting = [item for item in executions if item.status == "queued"]
                earlier_pending = False
                for capture in captures:
                    if capture.capture_order >= capture_order.get(turn_id, 0):
                        break
                    prior = next((e for e in executions if e.turn_id == capture.turn_id), None)
                    message = await session.get(db.Message, capture.message_id)
                    if (message.metrics or {}).get("stt_confirmed") is True and (
                        prior is None or prior.status not in ("failed", "applied")
                    ):
                        earlier_pending = True
                        break
                if (
                    not running
                    and not earlier_pending
                    and waiting
                    and waiting[0].id == execution.id
                ):
                    execution.owner_id = owner_id
                    execution.last_owner_id = owner_id
                    execution.producer_owner_id = conversation.worker_owner_id
                    execution.producer_epoch = (
                        conversation.worker_epoch if conversation.worker_owner_id else None
                    )
                    execution.status = "running"
                    execution.lease_until = execution.decision_deadline_at or now + timedelta(
                        seconds=DECISION_SECONDS
                    )
                    needs_model = now < conversation.started_at + timedelta(
                        minutes=conversation.max_minutes
                    )
                    reservation = (
                        self._reserve_locked(
                            session,
                            execution,
                            owner_id,
                            conversation.state_revision,
                            now,
                            clock_start,
                        )
                        if needs_model
                        else None
                    )
                    claimed = TurnLease(
                        execution.id,
                        owner_id,
                        turn_id,
                        clock_start + (execution.lease_until - now).total_seconds(),
                        reservation,
                    )
            await session.commit()
        if failure is not None:
            raise failure
        return claimed is not None, claimed

    async def withdraw(self, turn_id, owner_id):
        async with self.sessionmaker() as session:
            await session.scalar(
                select(db.Conversation.id)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            execution = await session.scalar(
                select(db.TurnExecution).where(
                    db.TurnExecution.conversation_id == self.conversation_id,
                    db.TurnExecution.turn_id == turn_id,
                )
            )
            if execution is None or str(owner_id) not in execution.waiters:
                return
            now = await session.scalar(select(func.clock_timestamp()))
            execution.waiters = {
                key: expiry
                for key, expiry in execution.waiters.items()
                if key != str(owner_id) and expiry > now.timestamp()
            }
            if execution.status == "queued" and not execution.waiters:
                execution.status = "idle"
            await session.commit()

    async def owned(self, session, lease: TurnLease):
        conversation = await session.scalar(
            select(db.Conversation)
            .where(db.Conversation.id == self.conversation_id)
            .with_for_update()
        )
        execution = await session.get(db.TurnExecution, lease.execution_id)
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            execution is None
            or conversation is None
            or (
                execution.producer_owner_id is not None
                and (
                    execution.producer_owner_id != conversation.worker_owner_id
                    or execution.producer_epoch != conversation.worker_epoch
                    or conversation.worker_lease_until is None
                    or conversation.worker_lease_until <= now
                )
            )
            or execution.conversation_id != self.conversation_id
            or execution.turn_id != lease.turn_id
            or execution.status != "running"
            or execution.owner_id != lease.owner_id
            or execution.lease_until is None
        ):
            raise TurnOwnershipError("Interview turn lease is no longer owned")
        if execution.decision_deadline_at is not None and execution.decision_deadline_at <= now:
            raise DecisionLimitError("Interview decision deadline exhausted")
        if execution.lease_until <= now:
            raise TurnOwnershipError("Interview turn lease is no longer owned")
        return execution, now

    async def reserve(self, lease: TurnLease, state_revision: int) -> InvocationReservation:
        async with self.sessionmaker() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.status != "interviewing":
                raise TurnOwnershipError("Interview ended while deciding")
            if conversation.state_revision != state_revision:
                raise TurnContextChangedError("Interview input changed before reservation")
            clock_start = time.monotonic()
            execution, now = await self.owned(session, lease)
            reservation = self._reserve_locked(
                session, execution, lease.owner_id, state_revision, now, clock_start
            )
            await session.commit()
            return reservation

    @staticmethod
    def _reserve_locked(session, execution, owner_id, state_revision, now, clock_start):
        if execution.invocations_reserved >= MAX_INVOCATIONS:
            raise DecisionLimitError("Interview decision failed validation after one repair")
        if execution.decision_deadline_at is None:
            execution.decision_deadline_at = execution.lease_until
        execution.invocations_reserved += 1
        invocation_id = uuid.uuid4()
        session.add(
            db.TurnInvocation(
                id=invocation_id,
                execution_id=execution.id,
                ordinal=execution.invocations_reserved,
                owner_id=owner_id,
                state_revision=state_revision,
            )
        )
        return InvocationReservation(
            invocation_id,
            execution.invocations_reserved,
            clock_start + (execution.decision_deadline_at - now).total_seconds(),
        )

    async def prepare(
        self, lease: TurnLease, reservation: InvocationReservation, state_revision: int
    ):
        """Bind the claimed reservation to loaded input; never allocate a new budget."""
        async with self.sessionmaker() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.status != "interviewing":
                raise TurnOwnershipError("Interview ended before the reserved invocation")
            await self.owned(session, lease)
            if conversation.state_revision != state_revision:
                raise TurnContextChangedError("Interview input changed before invocation")
            invocation = await session.get(db.TurnInvocation, reservation.id)
            if (
                invocation is None
                or invocation.execution_id != lease.execution_id
                or invocation.owner_id != lease.owner_id
                or invocation.ordinal != reservation.ordinal
                or invocation.finished_at is not None
            ):
                raise TurnOwnershipError("Invocation reservation is no longer owned")
            invocation.state_revision = state_revision
            await session.commit()
            return reservation

    async def record_outcome(self, reservation: InvocationReservation, outcome: str):
        async with self.sessionmaker() as session:
            await session.execute(
                update(db.TurnInvocation)
                .where(
                    db.TurnInvocation.id == reservation.id, db.TurnInvocation.finished_at.is_(None)
                )
                .values(finished_at=func.clock_timestamp(), outcome=outcome)
            )
            await session.commit()

    async def claim_notice(self, lease: TurnLease) -> bool:
        async with self.sessionmaker() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.status != "interviewing":
                return False
            execution = await session.get(db.TurnExecution, lease.execution_id)
            if (
                execution is None
                or execution.last_owner_id != lease.owner_id
                or execution.status not in ("failed", "queued", "idle")
                or execution.notice_claimed_at is not None
            ):
                return False
            newer = await session.scalar(
                select(db.TurnExecution.id)
                .where(
                    db.TurnExecution.conversation_id == self.conversation_id,
                    db.TurnExecution.created_at > execution.created_at,
                )
                .limit(1)
            )
            if newer is not None:
                return False
            execution.notice_claimed_at = await session.scalar(select(func.clock_timestamp()))
            await session.commit()
            return True

    async def claim_queue_notice(self, turn_id: str) -> bool:
        """Warn once for an unprocessed queue timeout, without interrupting an owner."""
        async with self.sessionmaker() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.status != "interviewing":
                return False
            execution = await session.scalar(
                select(db.TurnExecution).where(
                    db.TurnExecution.conversation_id == self.conversation_id,
                    db.TurnExecution.turn_id == turn_id,
                )
            )
            if (
                execution is None
                or execution.status == "applied"
                or execution.last_owner_id is not None
                or execution.notice_claimed_at is not None
            ):
                return False
            now = await session.scalar(select(func.clock_timestamp()))
            conflicting = await session.scalar(
                select(db.TurnExecution.id)
                .where(
                    db.TurnExecution.conversation_id == self.conversation_id,
                    (
                        (db.TurnExecution.owner_id.is_not(None))
                        & (db.TurnExecution.lease_until > now)
                    )
                    | (db.TurnExecution.created_at > execution.created_at),
                )
                .limit(1)
            )
            if conflicting is not None:
                return False
            execution.notice_claimed_at = now
            await session.commit()
            return True

    async def release(self, lease: TurnLease):
        async with self.sessionmaker() as session:
            await session.scalar(
                select(db.Conversation.id)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            execution = await session.get(db.TurnExecution, lease.execution_id)
            if (
                execution is None
                or execution.owner_id != lease.owner_id
                or execution.status != "running"
            ):
                return
            now = await session.scalar(select(func.clock_timestamp()))
            execution.status = (
                "failed"
                if (
                    execution.invocations_reserved >= MAX_INVOCATIONS
                    or (
                        execution.decision_deadline_at is not None
                        and execution.decision_deadline_at <= now
                    )
                )
                else "queued"
                if execution.waiters
                else "idle"
            )
            execution.owner_id = None
            execution.lease_until = None
            await session.commit()

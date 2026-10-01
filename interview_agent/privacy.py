"""External trace tombstones, serialized exports and durable verified deletion.

No source content is stored here. An accepted deletion request is not proof of
removal: LangSmith processes deletes asynchronously, so we query the whole trace.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from interview_agent.interview import db

logger = logging.getLogger(__name__)
MAX_SUBMISSIONS = 3
MAX_FAILURES = 5
VERIFICATION_DAYS = 14
LEASE_SECONDS = 45


async def register_trace(sessionmaker, trace_id, conversation_id, settings) -> bool:
    if sessionmaker is None:
        raise RuntimeError("External export requires durable privacy storage")
    async with sessionmaker() as session, session.begin():
        # Registration races content deletion under the same conversation lock;
        # network exports never hold this lock or delay the voice turn.
        conversation = await session.scalar(
            select(db.Conversation.id)
            .where(db.Conversation.id == conversation_id)
            .with_for_update()
        )
        if conversation is None:
            return False
        now = await session.scalar(select(func.clock_timestamp()))
        await session.execute(
            insert(db.ExternalTrace)
            .values(
                id=trace_id,
                conversation_id=conversation_id,
                endpoint=settings.langsmith_endpoint.rstrip("/"),
                project_name=settings.langsmith_project,
                expires_at=now + timedelta(days=settings.metrics_detail_days),
            )
            .on_conflict_do_nothing(index_elements=[db.ExternalTrace.id])
        )
        return True


async def guarded_export(sessionmaker, trace_id, operation) -> bool:
    async with sessionmaker() as session, session.begin():
        row = await session.scalar(
            select(db.ExternalTrace).where(db.ExternalTrace.id == trace_id).with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if row is None or row.state != "active" or row.expires_at <= now:
            return False
        # The SDK must have batching and retries disabled. Keep the trace lock
        # until the actual thread finishes, including cancellation; otherwise a
        # delete could run first and an in-flight create resurrect the trace.
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            # Retrieve a possible exception without losing the cancellation.
            if not task.cancelled():
                task.exception()
            raise
        return True


async def retire_traces(session, *, conversation_id=None, expired_only=False) -> list[uuid.UUID]:
    statement = select(db.ExternalTrace).where(db.ExternalTrace.state == "active")
    if conversation_id is not None:
        statement = statement.where(db.ExternalTrace.conversation_id == conversation_id)
    if expired_only:
        statement = statement.where(db.ExternalTrace.expires_at <= func.clock_timestamp())
    rows = list(await session.scalars(statement.with_for_update()))
    now = await session.scalar(select(func.clock_timestamp()))
    for row in rows:
        row.conversation_id = None
        row.state = "pending"
        row.deletion_requested_at = now
        row.next_attempt_at = now
    ids = [row.id for row in rows]
    if ids:
        await session.execute(
            delete(db.MetricEvent).where(
                db.MetricEvent.dimensions["trace_id"].astext.in_(
                    [str(identifier) for identifier in ids]
                )
            )
        )
    return ids


async def delete_conversation(session, conversation_id) -> bool:
    # Also used by ORM-only integration schemas; the migration installs the
    # equivalent BEFORE DELETE tombstone for SQL/cascade callers.
    identifier = await session.scalar(
        select(db.Conversation.id).where(db.Conversation.id == conversation_id).with_for_update()
    )
    if identifier is None:
        return False
    await retire_traces(session, conversation_id=conversation_id)
    await session.execute(delete(db.Conversation).where(db.Conversation.id == conversation_id))
    await session.commit()
    return True


class LangSmithDeletionAPI:
    def __init__(self, settings, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=settings.langsmith_endpoint.rstrip("/") + "/",
            headers={"x-api-key": settings.langsmith_api_key},
            timeout=5,
            transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def project_id(self, name):
        response = await self.client.get(
            "sessions", params={"name": name, "limit": 1, "include_stats": "false"}
        )
        response.raise_for_status()
        rows = response.json()
        if not rows or rows[0].get("name") != name:
            raise LookupError("project_not_found")
        return uuid.UUID(rows[0]["id"])

    async def submit(self, trace_id, project_id):
        # The SDK endpoint normally omits /api/v1 for public routes; use the
        # documented deletion URL explicitly, preserving regional base hosts.
        response = await self.client.post(
            (
                "runs/delete"
                if self.client.base_url.path.rstrip("/").endswith("/api/v1")
                else "api/v1/runs/delete"
            ),
            json={
                "trace_ids": [str(trace_id)],
                "session_id": str(project_id),
            },
        )
        response.raise_for_status()

    async def absent(self, trace_id, project_id):
        response = await self.client.post(
            "runs/query",
            json={
                "trace": str(trace_id),
                "session": [str(project_id)],
                "limit": 1,
                "select": ["id"],
            },
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("runs"), list):
            raise ValueError("invalid_verification_response")
        return not body["runs"]


class ExternalDeletionWorker:
    def __init__(self, sessionmaker, settings, *, api=None):
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.api = api or LangSmithDeletionAPI(settings)

    async def once(self) -> bool:
        owner = uuid.uuid4()
        async with self.sessionmaker() as session, session.begin():
            row = await session.scalar(
                select(db.ExternalTrace)
                .where(
                    db.ExternalTrace.state.in_(["pending", "verifying"]),
                    db.ExternalTrace.next_attempt_at <= func.clock_timestamp(),
                    or_(
                        db.ExternalTrace.lease_until.is_(None),
                        db.ExternalTrace.lease_until <= func.clock_timestamp(),
                    ),
                )
                .order_by(db.ExternalTrace.next_attempt_at, db.ExternalTrace.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                not self.settings.langsmith_api_key
                or row.endpoint != self.settings.langsmith_endpoint.rstrip("/")
            ):
                row.last_error = (
                    "missing_credentials"
                    if not self.settings.langsmith_api_key
                    else "endpoint_changed"
                )
                row.next_attempt_at = now + timedelta(minutes=1)
                return True
            if now >= row.deletion_requested_at + timedelta(days=VERIFICATION_DAYS):
                row.state, row.last_error = "failed", "verification_deadline"
                return True
            if row.state == "pending" and row.attempts >= MAX_SUBMISSIONS:
                row.state, row.last_error = "failed", "submission_limit"
                return True
            if row.state == "pending":
                row.attempts += 1  # reserved before any external request
            row.lease_owner = owner
            row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            identifier, project_name, project_id, phase = (
                row.id,
                row.project_name,
                row.project_id,
                row.state,
            )

        error, absent, submitted = None, False, False
        try:
            async with asyncio.timeout(20):
                project_id = project_id or await self.api.project_id(project_name)
                if phase == "pending":
                    await self.api.submit(identifier, project_id)
                    submitted = True
                else:
                    absent = await self.api.absent(identifier, project_id)
        except Exception as exc:
            # Never persist response bodies or exception messages containing keys.
            error = type(exc).__name__
        async with self.sessionmaker() as session, session.begin():
            row = await session.scalar(
                select(db.ExternalTrace).where(db.ExternalTrace.id == identifier).with_for_update()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if row.lease_owner != owner or row.lease_until <= now:
                return True
            row.lease_owner = row.lease_until = None
            row.project_id = project_id
            row.last_error = error
            if error:
                row.failures += 1
                row.state = "failed" if row.failures >= MAX_FAILURES else phase
                row.next_attempt_at = now + timedelta(seconds=min(3600, 30 * 2**row.failures))
            elif submitted:
                row.state = "verifying"
                row.deletion_submitted_at = now
                row.next_attempt_at = now + timedelta(hours=1)
            elif absent:
                row.state = "completed"
                row.deleted_at = now
                row.next_attempt_at = None
                row.project_name = ""  # keep only the minimal permanent tombstone
                row.project_id = None
            else:
                row.next_attempt_at = now + timedelta(hours=1)
        return True

    async def run(self):
        try:
            while True:
                try:
                    await self.once()
                except Exception:
                    logger.exception("External deletion sweep failed")
                await asyncio.sleep(5)
        finally:
            await self.api.close()


async def deletion_report(session) -> dict:
    rows = list(
        await session.scalars(
            select(db.ExternalTrace)
            .where(db.ExternalTrace.state != "active")
            .order_by(db.ExternalTrace.deletion_requested_at.desc())
            .limit(100)
        )
    )
    counts = dict(
        (
            await session.execute(
                select(db.ExternalTrace.state, func.count()).group_by(db.ExternalTrace.state)
            )
        ).all()
    )
    return {
        "counts": counts,
        "items": [
            {
                "id": str(row.id),
                "state": row.state,
                "attempts": row.attempts,
                "failures": row.failures,
                "last_error": row.last_error,
                "requested_at": row.deletion_requested_at,
                "submitted_at": row.deletion_submitted_at,
                "verified_at": row.deleted_at,
                "next_attempt_at": row.next_attempt_at,
            }
            for row in rows
        ],
    }

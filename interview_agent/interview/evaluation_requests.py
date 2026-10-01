"""Logical evaluation requests and bounded attempts, using the database clock."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from interview_agent.interview import db
from interview_agent.interview.seals import ensure_seal
from interview_agent.observability import content_hash

MAX_ATTEMPTS = 3
REQUEST_SECONDS = 300


async def claim(
    session, interview_id, lease_duration, *, automatic=False, recover=False, request_id=None
):
    conversation = await session.scalar(
        select(db.Conversation)
        .where(db.Conversation.id == interview_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None or conversation.status not in (*db.EVALUATION_CLAIMABLE, "evaluating"):
        return None
    now = await session.scalar(select(func.clock_timestamp()))
    if conversation.transcript_sealed_at is None:
        return None
    seal = await ensure_seal(session, conversation)
    current = (
        await session.get(db.EvaluationRequest, conversation.evaluation_request_id)
        if conversation.evaluation_request_id
        else None
    )
    previous = (
        await session.get(db.EvaluationRun, conversation.evaluation_claim_id)
        if conversation.evaluation_claim_id
        else None
    )
    live = (
        previous
        and previous.status == "running"
        and previous.lease_until
        and previous.lease_until > now
    )
    if request_id is not None:
        if automatic:
            raise ValueError("Automatic requests cannot use a manual identity")
        repeated = await session.get(db.EvaluationRequest, request_id)
        if repeated:
            if repeated.conversation_id != interview_id or repeated.automatic:
                raise ValueError("Evaluation identity belongs to another request")
            if (
                current is None
                or repeated.id != current.id
                or repeated.status not in ("pending", "running")
            ):
                return None
            recover = True
    if recover:
        if (
            current is None
            or current.status not in ("pending", "running")
            or live
            or current.seal_id != conversation.transcript_seal_id
        ):
            return None
        request = current
    else:
        if automatic:
            if seal.version != 1:
                return None
            if conversation.status != "completed" or await session.scalar(
                select(db.EvaluationRequest.id)
                .where(db.EvaluationRequest.conversation_id == interview_id)
                .limit(1)
            ):
                return None
        elif (
            live
            and current
            and not current.automatic
            and current.seal_id == conversation.transcript_seal_id
            and request_id is None
        ):
            return None
        if (
            current
            and not live
            and conversation.status == "evaluating"
            and current.status in ("pending", "running")
            and current.seal_id == conversation.transcript_seal_id
            and request_id is None
        ):
            request = current  # Recovery, not a new manual logical request.
        else:
            request = db.EvaluationRequest(
                id=request_id or uuid.uuid4(),
                conversation_id=interview_id,
                automatic=automatic,
                transcript_hash=content_hash(seal.records),
                seal_id=seal.id,
                status="pending",
                attempts=0,
                deadline_at=now + timedelta(seconds=REQUEST_SECONDS),
            )
            session.add(request)
            if current:
                current.status = "superseded"
            conversation.evaluation_request_id = request.id
    if request.attempts >= MAX_ATTEMPTS or request.deadline_at <= now:
        request.status = "failed"
        conversation.status = "evaluation_failed"
        if previous and previous.status == "running":
            previous.status = "abandoned"
            previous.finished_at = now
        await session.commit()
        return None
    if previous and previous.status == "running":
        previous.status = "superseded" if previous.request_id != request.id else "abandoned"
        previous.finished_at = now
    attempt_id = uuid.uuid4()
    request.attempts += 1
    request.status = "running"
    await session.flush()
    session.add(
        db.EvaluationRun(
            id=attempt_id,
            conversation_id=interview_id,
            request_id=request.id,
            ordinal=request.attempts,
            status="running",
            transcript_hash=request.transcript_hash,
            config={},
            started_at=now,
            lease_until=min(now + lease_duration, request.deadline_at),
        )
    )
    conversation.status = "evaluating"
    conversation.evaluation_claim_id = attempt_id
    await session.commit()
    return attempt_id


async def owned(session, conversation, run):
    now = await session.scalar(select(func.clock_timestamp()))
    request = (
        await session.get(db.EvaluationRequest, run.request_id) if run and run.request_id else None
    )
    return bool(
        conversation
        and run
        and request
        and conversation.status == "evaluating"
        and conversation.evaluation_claim_id == run.id
        and conversation.evaluation_request_id == request.id
        and request.status == "running"
        and request.seal_id == conversation.transcript_seal_id
        and request.deadline_at > now
        and run.status == "running"
        and run.lease_until
        and run.lease_until > now
    )


async def heartbeat(session, interview_id, attempt_id, lease_duration):
    conversation = await session.scalar(
        select(db.Conversation).where(db.Conversation.id == interview_id).with_for_update()
    )
    run = await session.get(db.EvaluationRun, attempt_id) if attempt_id else None
    if not await owned(session, conversation, run):
        return False
    now = await session.scalar(select(func.clock_timestamp()))
    request = await session.get(db.EvaluationRequest, run.request_id)
    run.lease_until = min(now + lease_duration, request.deadline_at)
    conversation.updated_at = now
    await session.commit()
    return True

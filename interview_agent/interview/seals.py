"""Immutable transcript snapshots and explicit, durable incident review."""

from __future__ import annotations

import copy
import uuid

from sqlalchemy import func, select

from interview_agent.interview import db
from interview_agent.interview.evaluation_contract import canonical_message_records
from interview_agent.observability import content_hash


async def ensure_seal(session, conversation):
    """Snapshot the canonical transcript once the interview is sealed.

    Call under the conversation lock, in the transaction that closes it.
    """
    if conversation.transcript_seal_id:
        seal = await session.get(db.TranscriptSeal, conversation.transcript_seal_id)
        if seal is None or seal.conversation_id != conversation.id:
            raise ValueError("Transcript seal belongs to another interview")
        return seal
    if conversation.transcript_sealed_at is None:
        return None
    await session.flush()
    messages = await db.get_messages(session, conversation.id)
    records = canonical_message_records(messages)
    seal = db.TranscriptSeal(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        version=1,
        records=records,
        transcript_hash=content_hash(records),
        integrity=conversation.transcript_integrity or "unknown",
        provenance={
            "origin": "existing_seal_snapshot",
            "canonical_hash": content_hash(records),
            "sealed_at": conversation.transcript_sealed_at.isoformat(),
            "stt_drain": copy.deepcopy(conversation.stt_drain),
            "messages": {
                str(m.id): {
                    "turn_id": m.turn_id,
                    "source_id": m.source_id,
                    "metrics": copy.deepcopy(m.metrics),
                }
                for m in messages
            },
            "incorporated_incident_ids": [],
        },
    )
    session.add(seal)
    await session.flush()
    conversation.transcript_seal_id = seal.id
    for incident in await session.scalars(
        select(db.CaptureIncident).where(
            db.CaptureIncident.conversation_id == conversation.id,
            db.CaptureIncident.seal_id.is_(None),
        )
    ):
        incident.seal_id = seal.id
    return seal


async def seal_invalid(session, seal_id):
    if seal_id is None:
        return False
    seal = await session.get(db.TranscriptSeal, seal_id)
    if seal is None:
        return True
    incorporated = set(seal.provenance.get("incorporated_incident_ids", []))
    incidents = list(
        await session.scalars(
            select(db.CaptureIncident).where(db.CaptureIncident.seal_id == seal_id)
        )
    )
    for incident in incidents:
        resolution = await session.scalar(
            select(db.IncidentResolution).where(db.IncidentResolution.incident_id == incident.id)
        )
        if resolution is None or (
            resolution.decision == "omission" and str(incident.id) not in incorporated
        ):
            return True
    return False


async def refresh_pending(session, conversation):
    await session.flush()
    unresolved = await session.scalar(
        select(db.CaptureIncident.id)
        .where(
            db.CaptureIncident.conversation_id == conversation.id,
            db.CaptureIncident.resolved_at.is_(None),
        )
        .limit(1)
    )
    conversation.capture_integrity_pending = bool(unresolved) or await seal_invalid(
        session, conversation.transcript_seal_id
    )


async def review_incident(
    session, conversation_id, incident_id, *, review_id, decision, rationale, reviewer
):
    conversation = await session.scalar(
        select(db.Conversation).where(db.Conversation.id == conversation_id).with_for_update()
    )
    if conversation is None or conversation.transcript_sealed_at is None:
        raise ValueError("Only a closed interview can be reviewed")
    incident = await session.get(db.CaptureIncident, incident_id)
    if incident is None or incident.conversation_id != conversation_id:
        raise ValueError("Capture incident does not belong to this interview")
    if (
        decision not in ("duplicate", "post_cut", "omission")
        or not rationale.strip()
        or not reviewer.strip()
    ):
        raise ValueError("A review requires a decision, reviewer and explanation")
    await ensure_seal(session, conversation)
    previous = await session.scalar(
        select(db.IncidentResolution).where(db.IncidentResolution.incident_id == incident_id)
    )
    if previous:
        if (previous.id, previous.decision, previous.rationale, previous.reviewer) != (
            review_id,
            decision,
            rationale,
            reviewer,
        ):
            raise ValueError("This incident already has an immutable review")
        return previous
    if await session.get(db.IncidentResolution, review_id):
        raise ValueError("Review identity belongs to another incident")
    resolution = db.IncidentResolution(
        id=review_id,
        conversation_id=conversation_id,
        incident_id=incident_id,
        decision=decision,
        rationale=rationale,
        reviewer=reviewer,
    )
    session.add(resolution)
    incident.resolved_at = await session.scalar(select(func.clock_timestamp()))
    await refresh_pending(session, conversation)
    conversation.state_revision += 1
    await session.commit()
    return resolution


async def create_successor(
    session,
    conversation_id,
    *,
    seal_id,
    parent_id,
    incident_ids,
    rationale,
    reviewer,
    confirm_complete=False,
):
    conversation = await session.scalar(
        select(db.Conversation).where(db.Conversation.id == conversation_id).with_for_update()
    )
    if conversation is None or conversation.transcript_sealed_at is None:
        raise ValueError("Only a closed interview can have a successor snapshot")
    existing = await session.get(db.TranscriptSeal, seal_id)
    selected = sorted(str(i) for i in incident_ids)
    if existing:
        if (
            existing.conversation_id != conversation_id
            or existing.parent_id != parent_id
            or existing.provenance.get("review")
            != {
                "rationale": rationale,
                "reviewer": reviewer,
                "confirm_complete": confirm_complete,
                "incident_ids": selected,
            }
        ):
            raise ValueError("Snapshot identity belongs to another review")
        if conversation.transcript_seal_id != existing.id:
            raise ValueError("This snapshot has already been superseded")
        return existing
    parent = await ensure_seal(session, conversation)
    if (
        parent is None
        or parent.id != parent_id
        or not incident_ids
        or len(set(incident_ids)) != len(incident_ids)
    ):
        raise ValueError("Choose omitted captures from the current snapshot")
    if not rationale.strip() or not reviewer.strip():
        raise ValueError("A successor snapshot requires reviewer and explanation")
    unresolved = await session.scalar(
        select(db.CaptureIncident.id)
        .where(
            db.CaptureIncident.conversation_id == conversation_id,
            db.CaptureIncident.resolved_at.is_(None),
        )
        .limit(1)
    )
    if unresolved:
        raise ValueError("Review all pending capture incidents first")
    records, provenance = copy.deepcopy(parent.records), copy.deepcopy(parent.provenance)
    incorporated = set(provenance.get("incorporated_incident_ids", []))
    selected_incidents = list(
        await session.scalars(
            select(db.CaptureIncident)
            .where(db.CaptureIncident.id.in_(incident_ids))
            .order_by(db.CaptureIncident.created_at, db.CaptureIncident.id)
        )
    )
    if len(selected_incidents) != len(incident_ids):
        raise ValueError("An omitted capture no longer exists")
    for incident in selected_incidents:
        incident_id = incident.id
        resolution = await session.scalar(
            select(db.IncidentResolution).where(db.IncidentResolution.incident_id == incident_id)
        )
        if (
            incident is None
            or incident.conversation_id != conversation_id
            or resolution is None
            or resolution.decision != "omission"
            or str(incident_id) in incorporated
        ):
            raise ValueError("Only a confirmed, unincorporated omission can be included")
        payload = incident.payload
        if (
            payload.get("metrics", {}).get("stt_confirmed") is not True
            or not payload.get("content", "").strip()
        ):
            raise ValueError("An omitted answer requires confirmed capture provenance")
        record_id = "incident-" + str(incident.id)
        record = {
            "id": record_id,
            "role": "user",
            "content": payload["content"],
            "version": payload["version"],
        }
        previous_id = next(
            (
                identity
                for identity, item in provenance.get("messages", {}).items()
                if item.get("turn_id") == incident.turn_id
            ),
            None,
        )
        if previous_id:
            index = next((i for i, item in enumerate(records) if item["id"] == previous_id), None)
            if index is None:
                raise ValueError("Capture replacement no longer identifies a saved answer")
            records[index] = record
            provenance["messages"].pop(previous_id)
        else:
            # A final admitted tail is placed before the fixed farewell. Its
            # original capture metadata remains visible for manual inspection.
            index = next(
                (
                    i
                    for i, item in enumerate(records)
                    if (
                        provenance.get("messages", {}).get(item["id"], {}).get("source_id") or ""
                    ).startswith("farewell-")
                ),
                len(records),
            )
            records.insert(index, record)
        provenance.setdefault("messages", {})[record_id] = {
            "turn_id": incident.turn_id,
            "source_id": payload["source_id"],
            "metrics": payload["metrics"],
            "incident_id": str(incident.id),
        }
        incorporated.add(str(incident.id))
    # Never produce a global while a known omission remains outside the new seal.
    omissions = await session.scalars(
        select(db.CaptureIncident.id)
        .join(db.IncidentResolution)
        .where(
            db.CaptureIncident.conversation_id == conversation_id,
            db.IncidentResolution.decision == "omission",
        )
    )
    if any(str(i) not in incorporated for i in omissions):
        raise ValueError("Include all confirmed omissions in the successor snapshot")
    if confirm_complete and any(
        m.get("confirmed") is False for m in records if m["role"] == "user"
    ):
        raise ValueError("Unconfirmed answers prevent a complete snapshot")
    provenance["incorporated_incident_ids"] = sorted(incorporated)
    provenance["origin"] = "manual_successor"
    provenance["review"] = {
        "rationale": rationale,
        "reviewer": reviewer,
        "confirm_complete": confirm_complete,
        "incident_ids": selected,
    }
    seal = db.TranscriptSeal(
        id=seal_id,
        conversation_id=conversation_id,
        version=parent.version + 1,
        parent_id=parent.id,
        records=records,
        provenance=provenance,
        transcript_hash=content_hash(records),
        integrity="complete" if confirm_complete else "partial",
    )
    session.add(seal)
    await session.flush()
    conversation.transcript_seal_id = seal.id
    conversation.status = "completed"
    if conversation.evaluation_request_id:
        request = await session.get(db.EvaluationRequest, conversation.evaluation_request_id)
        if request and request.status in ("pending", "running"):
            request.status = "superseded"
            run = (
                await session.get(db.EvaluationRun, conversation.evaluation_claim_id)
                if conversation.evaluation_claim_id
                else None
            )
            if run and run.status == "running":
                run.status = "superseded"
                run.finished_at = await session.scalar(select(func.clock_timestamp()))
    await refresh_pending(session, conversation)
    conversation.state_revision += 1
    await session.commit()
    return seal


async def result_invalid(session, conversation, result=None):
    conversation._current_seal = (
        await session.get(db.TranscriptSeal, conversation.transcript_seal_id)
        if conversation.transcript_seal_id
        else None
    )
    if conversation.capture_integrity_pending:
        return True
    seal_id = (result or {}).get("seal_id")
    return await seal_invalid(session, uuid.UUID(seal_id) if seal_id else None)

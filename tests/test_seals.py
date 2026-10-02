"""Historical results survive explicit review without rewriting sealed evidence."""

import copy
import uuid

import pytest
from sqlalchemy import func, select
from test_routes import (
    _fake_evaluator,
    _seed_finished_interview,
    _settled,
)
from test_routes import (
    acting_user as acting_user,
)
from test_routes import (
    client_and_sessionmaker as client_and_sessionmaker,
)

from interview_agent.interview import db
from interview_agent.interview.seals import (
    create_successor,
    review_incident,
    seal_invalid,
)
from interview_agent.interview.transcription import admit_candidate
from interview_agent.server import evaluations


async def sealed(sessions, *, integrity="complete"):
    interview_id = await _seed_finished_interview(sessions, integrity=integrity)
    async with sessions() as session:
        conversation = await session.get(db.Conversation, interview_id)
        return interview_id, conversation.transcript_seal_id


async def late(sessions, interview_id, *, source="late"):
    async with sessions() as session:
        result = await admit_candidate(
            session,
            interview_id,
            content="The admitted final answer tail.",
            source_id=source,
            metrics={"stt_confirmed": True},
        )
        assert not result.accepted
        return await session.scalar(
            select(db.CaptureIncident.id).where(db.CaptureIncident.conversation_id == interview_id)
        )


async def review(sessions, interview_id, incident_id, decision, *, review_id=None):
    async with sessions() as session:
        return await review_incident(
            session,
            interview_id,
            incident_id,
            review_id=review_id or uuid.uuid4(),
            decision=decision,
            rationale="Reviewed the admitted audio and its timing.",
            reviewer="Local reviewer",
        )


async def test_late_tail_and_duplicate_review_preserve_partial_seal(
    postgres_sessionmaker,
):
    interview_id, seal_id = await sealed(postgres_sessionmaker, integrity="partial")
    incident_id = await late(postgres_sessionmaker, interview_id)
    async with postgres_sessionmaker() as session:
        snapshot = await session.get(db.TranscriptSeal, seal_id)
        before = copy.deepcopy(snapshot.records)
        assert await seal_invalid(session, seal_id)
    reviewed = await review(postgres_sessionmaker, interview_id, incident_id, "duplicate")
    await review(
        postgres_sessionmaker, interview_id, incident_id, "duplicate", review_id=reviewed.id
    )
    async with postgres_sessionmaker() as session:
        snapshot = await session.get(db.TranscriptSeal, seal_id)
        row = await session.get(db.Conversation, interview_id)
        assert snapshot.records == before and snapshot.integrity == "partial"
        assert not row.capture_integrity_pending and not await seal_invalid(session, seal_id)
        assert await session.scalar(select(func.count()).select_from(db.IncidentResolution)) == 1


async def test_confirmed_omission_is_permanent_for_old_result_but_manual_successor_can_be_evaluated(
    client_and_sessionmaker, monkeypatch
):
    client, sessions = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    interview_id, original_id = await sealed(sessions)
    assert (
        await client.post(f"/api/interviews/{interview_id}/evaluate?automatic=true")
    ).status_code == 202
    assert (await _settled(client, interview_id))["evaluation"]["score"] == 82
    incident_id = await late(sessions, interview_id)
    assert (await client.get(f"/api/interviews/{interview_id}")).json()["evaluation"][
        "score"
    ] is None
    await review(sessions, interview_id, incident_id, "omission")
    with pytest.raises(ValueError, match="immutable review"):
        await review(sessions, interview_id, incident_id, "duplicate")
    async with sessions() as session:
        original = await session.get(db.TranscriptSeal, original_id)
        original_records, original_hash = copy.deepcopy(original.records), original.transcript_hash
        successor_id = uuid.uuid4()
        body = {
            "seal_id": successor_id,
            "parent_id": original_id,
            "incident_ids": [incident_id],
            "rationale": "Compared the complete admitted audio with both saved answers.",
            "reviewer": "Local reviewer",
            "confirm_complete": True,
        }
        successor = await create_successor(session, interview_id, **body)
        assert successor.version == 2 and successor.integrity == "complete"
    async with sessions() as session:
        assert (await create_successor(session, interview_id, **body)).id == successor_id
        assert (
            await db.claim_evaluation(
                session, interview_id, evaluations.STALE_AFTER, automatic=True
            )
            is None
        )
    before = (await client.get(f"/api/interviews/{interview_id}")).json()
    assert before["evaluation"]["score"] is None and before["evaluation_invalidated"]
    assert before["evaluation_is_previous"]
    assert (await client.get("/api/interviews")).json()["items"][0]["evaluation"]["score"] is None
    assert (await client.post(f"/api/interviews/{interview_id}/evaluate")).status_code == 202
    after = await _settled(client, interview_id)
    assert after["evaluation"]["score"] == 82 and after["evaluation"]["seal_id"] == str(
        successor_id
    )
    assert not after["evaluation_invalidated"]
    history = (await client.get(f"/api/interviews/{interview_id}/evaluations")).json()
    assert history["attempts"][0]["result"]["score"] is None
    assert history["attempts"][1]["result"]["score"] == 82
    assert [item["seal_version"] for item in history["requests"]] == [1, 2]
    assert [item["invalidated"] for item in history["attempts"]] == [True, False]
    async with sessions() as session:
        old_run = await session.get(db.EvaluationRun, uuid.UUID(history["attempts"][0]["id"]))
        assert old_run.result["score"] == 82  # Masked response, preserved original.
    async with sessions() as session:
        original = await session.get(db.TranscriptSeal, original_id)
        assert original.records == original_records and original.transcript_hash == original_hash
        successor = await session.get(db.TranscriptSeal, successor_id)
        assert any(m["id"] == "incident-" + str(incident_id) for m in successor.records)
        assert len(await db.get_messages(session, interview_id)) == 2


async def test_duplicate_review_restores_original_result_without_second_evaluation(
    client_and_sessionmaker, monkeypatch
):
    client, sessions = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    interview_id, original_id = await sealed(sessions)
    await client.post(f"/api/interviews/{interview_id}/evaluate")
    assert (await _settled(client, interview_id))["evaluation"]["score"] == 82
    incident_id = await late(sessions, interview_id)
    response = await client.post(
        f"/api/interviews/{interview_id}/incidents/{incident_id}/review",
        json={
            "review_id": str(uuid.uuid4()),
            "decision": "post_cut",
            "rationale": "Audio started after the persisted cut.",
            "reviewer": "Local reviewer",
        },
    )
    assert response.status_code == 200
    assert (await client.get(f"/api/interviews/{interview_id}")).json()["evaluation"]["score"] == 82
    async with sessions() as session:
        assert not await seal_invalid(session, original_id)
        assert await session.scalar(select(func.count()).select_from(db.EvaluationRequest)) == 1


async def test_successor_without_full_audio_confirmation_stays_partial_and_cannot_skip_omissions(
    postgres_sessionmaker,
):
    interview_id, parent_id = await sealed(postgres_sessionmaker)
    incident_id = await late(postgres_sessionmaker, interview_id)
    async with postgres_sessionmaker() as session:
        with pytest.raises(ValueError, match="Review all"):
            await create_successor(
                session,
                interview_id,
                seal_id=uuid.uuid4(),
                parent_id=parent_id,
                incident_ids=[incident_id],
                rationale="Review",
                reviewer="Reviewer",
            )
    await review(postgres_sessionmaker, interview_id, incident_id, "omission")
    async with postgres_sessionmaker() as session:
        successor = await create_successor(
            session,
            interview_id,
            seal_id=uuid.uuid4(),
            parent_id=parent_id,
            incident_ids=[incident_id],
            rationale="Cannot prove the rest of the audio is complete.",
            reviewer="Reviewer",
        )
        assert successor.integrity == "partial"


async def test_retried_manual_identity_cannot_bill_another_completed_assessment(
    client_and_sessionmaker, monkeypatch
):
    client, sessions = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    interview_id, _ = await sealed(sessions)
    request_id = uuid.uuid4()
    path = f"/api/interviews/{interview_id}/evaluate?request_id={request_id}"
    assert (await client.post(path)).status_code == 202
    assert (await _settled(client, interview_id))["evaluation"]["request_id"] == str(request_id)
    assert (await client.post(path)).status_code == 202
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(db.EvaluationRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(db.EvaluationRun)) == 1


async def test_successor_before_first_automatic_trigger_still_requires_manual_evaluation(
    postgres_sessionmaker,
):
    interview_id, parent_id = await sealed(postgres_sessionmaker)
    incident_id = await late(postgres_sessionmaker, interview_id)
    await review(postgres_sessionmaker, interview_id, incident_id, "omission")
    async with postgres_sessionmaker() as session:
        await create_successor(
            session,
            interview_id,
            seal_id=uuid.uuid4(),
            parent_id=parent_id,
            incident_ids=[incident_id],
            rationale="Reviewed admitted tail.",
            reviewer="Reviewer",
        )
    runner = evaluations.EvaluationRunner(postgres_sessionmaker)
    assert await runner.reconcile_pending() == 0
    async with postgres_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(db.EvaluationRequest)) == 0


async def test_running_old_evaluator_cannot_publish_after_successor_snapshot(
    postgres_sessionmaker, monkeypatch
):
    import asyncio

    sessions = postgres_sessionmaker
    interview_id, parent_id = await sealed(sessions)
    started, release = asyncio.Event(), asyncio.Event()

    async def controlled(*args, **kwargs):
        started.set()
        await release.wait()
        return await _fake_evaluator(*args, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", controlled)
    runner = evaluations.EvaluationRunner(sessions)
    async with sessions() as session:
        first = await db.claim_evaluation(
            session, interview_id, evaluations.STALE_AFTER, automatic=True
        )
    runner.start(interview_id, first)
    await asyncio.wait_for(started.wait(), 2)
    try:
        incident_id = await late(sessions, interview_id)
        await review(sessions, interview_id, incident_id, "omission")
        async with sessions() as session:
            successor = await create_successor(
                session,
                interview_id,
                seal_id=uuid.uuid4(),
                parent_id=parent_id,
                incident_ids=[incident_id],
                rationale="Verified all admitted audio.",
                reviewer="Reviewer",
                confirm_complete=True,
            )
            successor_id = successor.id
        release.set()
        await runner.wait_idle()
        async with sessions() as session:
            row = await db.get_conversation(session, interview_id)
            assert row.evaluation is None and row.transcript_seal_id == successor_id
            assert (await session.get(db.EvaluationRun, first)).status == "superseded"
        assert await runner.reconcile_pending() == 0
    finally:
        release.set()
        await runner.shutdown()

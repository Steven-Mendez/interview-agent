"""Durable API ACKs, authenticated identity, ordering and post-seal promotion."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from livekit import api
from sqlalchemy import func, select

from interview_agent.config import settings
from interview_agent.interview import db
from interview_agent.playback import (
    PlaybackAck,
    acknowledge_playback,
    closing_state,
    record_delivery,
)
from interview_agent.server.routes import router


async def attempt(sessionmaker, *, elapsed=0):
    conversation_id, owner, close, audio = (uuid.uuid4() for _ in range(4))
    ack = PlaybackAck(
        closing_id=close, stream_id=str(uuid.uuid4()), attempt_id=audio, status="played"
    )
    async with sessionmaker() as session:
        acquired = await session.scalar(select(func.clock_timestamp()))
        acquired -= timedelta(seconds=elapsed)
        session.add(
            db.Conversation(
                id=conversation_id,
                status="closing",
                job_offer="Synthetic role",
                resume_markdown="Synthetic CV",
                closing_owner_id=owner,
                closing_id=close,
                closing_stream_id=ack.stream_id,
                closing_attempt_id=audio,
                closing_acquired_at=acquired,
                closing_ack_deadline_at=acquired + timedelta(seconds=35),
                closing_audio_timeout_seconds=20,
                closing_deadline_at=acquired + timedelta(seconds=35),
                farewell_status="pending",
            )
        )
        await session.commit()
    return conversation_id, owner, ack


async def deliver(sessionmaker, conversation_id, owner, ack):
    return await record_delivery(
        sessionmaker,
        conversation_id,
        owner,
        ack.closing_id,
        ack.stream_id,
        ack.attempt_id,
        524,
        "audio/wav",
    )


@pytest.mark.parametrize("ack_first", [False, True])
async def test_delivery_and_ack_orders_promote_exactly_once(postgres_sessionmaker, ack_first):
    conversation_id, owner, ack = await attempt(postgres_sessionmaker)
    if ack_first:
        result = await acknowledge_playback(postgres_sessionmaker, conversation_id, ack)
        assert result["accepted"] and result["provisional"] and result["status"] == "pending"
        assert await deliver(postgres_sessionmaker, conversation_id, owner, ack) == "played"
    else:
        assert await deliver(postgres_sessionmaker, conversation_id, owner, ack) == "pending"
    result = await acknowledge_playback(postgres_sessionmaker, conversation_id, ack)
    assert result == {"accepted": True, "status": "played", "provisional": False}
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        received = row.closing_ack_received_at
        assert row.status == "closing" and row.transcript_sealed_at is None
        assert row.closing_audio_size == 524 and row.closing_audio_mime == "audio/wav"
        row.closing_ack_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert (await acknowledge_playback(postgres_sessionmaker, conversation_id, ack))[
        "status"
    ] == "played"
    assert (
        await acknowledge_playback(
            postgres_sessionmaker, conversation_id, ack.model_copy(update={"status": "failed"})
        )
    )["status"] == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.closing_ack_received_at == received


async def test_browser_timeout_is_recorded_as_timeout_and_a_played_ack_still_wins(
    postgres_sessionmaker,
):
    conversation_id, owner, ack = await attempt(postgres_sessionmaker)
    timeout = ack.model_copy(update={"status": "timeout"})
    result = await acknowledge_playback(postgres_sessionmaker, conversation_id, timeout)
    assert result == {"accepted": True, "status": "timeout", "provisional": False}
    await deliver(postgres_sessionmaker, conversation_id, owner, ack)
    assert (await acknowledge_playback(postgres_sessionmaker, conversation_id, ack))[
        "status"
    ] == "played"


async def test_late_ack_promotes_after_seal_without_reopening_or_evaluation(postgres_sessionmaker):
    conversation_id, owner, ack = await attempt(postgres_sessionmaker, elapsed=21)
    await deliver(postgres_sessionmaker, conversation_id, owner, ack)
    sealed = datetime.now(UTC)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.status = "completed"
        row.farewell_status = "timeout"
        row.transcript_integrity = "partial"
        row.transcript_sealed_at = sealed
        await session.commit()
    assert (await acknowledge_playback(postgres_sessionmaker, conversation_id, ack))[
        "status"
    ] == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_sealed_at == sealed and row.transcript_integrity == "partial"
        assert row.status == "completed" and row.closing_playback_exceeded_budget is True
        assert await session.scalar(select(func.count()).select_from(db.EvaluationRun)) == 0


async def test_unverified_delivery_and_expired_ack_cannot_be_played(postgres_sessionmaker):
    conversation_id, owner, ack = await attempt(postgres_sessionmaker)
    assert await deliver(postgres_sessionmaker, conversation_id, uuid.uuid4(), ack) is None
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.closing_ack_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await acknowledge_playback(postgres_sessionmaker, conversation_id, ack) == {
        "accepted": False,
        "reason": "expired",
    }
    assert await deliver(postgres_sessionmaker, conversation_id, owner, ack) is None
    assert (await closing_state(postgres_sessionmaker, conversation_id))[
        "farewell_status"
    ] == "pending"


async def test_browser_remaining_time_keeps_initial_origin_on_owner_change(postgres_sessionmaker):
    conversation_id, _, _ = await attempt(postgres_sessionmaker, elapsed=12)
    first = await closing_state(postgres_sessionmaker, conversation_id)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.closing_owner_id = uuid.uuid4()
        row.closing_deadline_at = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()
    second = await closing_state(postgres_sessionmaker, conversation_id)
    assert 0 < second["remaining_seconds"] <= first["remaining_seconds"] <= 33


@pytest.mark.parametrize(
    "credential",
    ["missing", "bad_signature", "other_room", "other_identity", "expired", "no_join", "valid"],
)
async def test_api_authenticates_room_and_participant_without_worker(
    postgres_sessionmaker, monkeypatch, credential
):
    monkeypatch.setattr(settings, "livekit_api_key", "playback-test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "playback-test-secret-at-least-32-bytes")
    conversation_id, owner, ack = await attempt(postgres_sessionmaker)
    token = (
        api.AccessToken(
            settings.livekit_api_key,
            "wrong-signature-secret-at-least-32-bytes"
            if credential == "bad_signature"
            else settings.livekit_api_secret,
        )
        .with_identity("stranger" if credential == "other_identity" else "candidate")
        .with_grants(
            api.VideoGrants(
                room_join=credential != "no_join",
                room=f"interview-{uuid.uuid4() if credential == 'other_room' else conversation_id}",
            )
        )
        .with_ttl(timedelta(seconds=-1 if credential == "expired" else 60))
        .to_jwt()
    )
    app = FastAPI()
    app.state.sessionmaker = postgres_sessionmaker
    app.include_router(router, prefix="/api")
    headers = {} if credential == "missing" else {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/interviews/{conversation_id}/closing"
        result = await client.post(path + "/ack", headers=headers, json=ack.model_dump(mode="json"))
        expected = (
            200
            if credential == "valid"
            else 401
            if credential in ("missing", "bad_signature", "expired")
            else 403
        )
        assert result.status_code == expected
        state = await client.get(path, headers=headers)
        assert state.status_code == expected
        if credential == "valid":
            assert result.json()["provisional"]
            assert "job_offer" not in state.json() and "resume_markdown" not in state.json()
            await deliver(postgres_sessionmaker, conversation_id, owner, ack)
            assert (await client.get(path, headers=headers)).json()["farewell_status"] == "played"
            mismatch = ack.model_dump(mode="json") | {"attempt_id": str(uuid.uuid4())}
            assert (
                await client.post(path + "/ack", headers=headers, json=mismatch)
            ).status_code == 409


async def test_api_records_the_used_output_once_per_attempt_without_device_names(
    postgres_sessionmaker, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_api_key", "playback-test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "playback-test-secret-at-least-32-bytes")
    conversation_id, _owner, ack = await attempt(postgres_sessionmaker)
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("candidate")
        .with_grants(api.VideoGrants(room_join=True, room=f"interview-{conversation_id}"))
        .to_jwt()
    )
    app = FastAPI()
    app.state.sessionmaker = postgres_sessionmaker
    app.include_router(router, prefix="/api")
    headers = {"Authorization": f"Bearer {token}"}
    body = ack.model_dump(mode="json") | {"audio_output": "selected"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/interviews/{conversation_id}/closing/ack"
        for _ in range(2):
            assert (await client.post(path, headers=headers, json=body)).status_code == 200
        named = body | {"audio_output": "Studio speakers"}
        assert (await client.post(path, headers=headers, json=named)).status_code == 422
    async with postgres_sessionmaker() as session:
        events = list(
            await session.scalars(
                select(db.MetricEvent).where(
                    db.MetricEvent.conversation_id == conversation_id,
                    db.MetricEvent.name == "farewell_audio_output",
                )
            )
        )
    assert len(events) == 1
    assert events[0].dimensions["audio_output"] == "selected"


async def test_browser_response_onset_is_authenticated_bounded_and_idempotent(
    postgres_sessionmaker, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_api_key", "playback-test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "playback-test-secret-at-least-32-bytes")
    conversation_id, _owner, _ack = await attempt(postgres_sessionmaker)
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("candidate")
        .with_grants(api.VideoGrants(room_join=True, room=f"interview-{conversation_id}"))
        .to_jwt()
    )
    app = FastAPI()
    app.state.sessionmaker = postgres_sessionmaker
    app.include_router(router, prefix="/api")
    path = f"/api/interviews/{conversation_id}/metrics/response-onset"
    sample = {"sample_id": str(uuid.uuid4()), "seconds": 2.4}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=sample)).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        for _ in range(2):
            assert (await client.post(path, headers=headers, json=sample)).status_code == 202
        too_long = sample | {"sample_id": str(uuid.uuid4()), "seconds": 61}
        assert (await client.post(path, headers=headers, json=too_long)).status_code == 422
    async with postgres_sessionmaker() as session:
        values = list(
            await session.scalars(
                select(db.MetricEvent.value).where(
                    db.MetricEvent.conversation_id == conversation_id,
                    db.MetricEvent.name == "response_onset_seconds",
                )
            )
        )
    assert values == [2.4]

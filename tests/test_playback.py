"""Durable API ACKs, authenticated identity, ordering and post-seal promotion."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from livekit import api
from sqlalchemy import func, select, text, update

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


async def acknowledge(sessionmaker, conversation_id, ack):
    result, _first = await acknowledge_playback(sessionmaker, conversation_id, ack)
    return result


@pytest.mark.parametrize("ack_first", [False, True])
async def test_delivery_and_ack_orders_promote_exactly_once(postgres_sessionmaker, ack_first):
    conversation_id, owner, ack = await attempt(postgres_sessionmaker)
    if ack_first:
        result, first = await acknowledge_playback(postgres_sessionmaker, conversation_id, ack)
        assert result["accepted"] and result["provisional"] and result["status"] == "pending"
        assert first
        assert await deliver(postgres_sessionmaker, conversation_id, owner, ack) == "played"
    else:
        assert await deliver(postgres_sessionmaker, conversation_id, owner, ack) == "pending"
    result, first = await acknowledge_playback(postgres_sessionmaker, conversation_id, ack)
    assert result == {"accepted": True, "status": "played", "provisional": False}
    # Only the attempt's first stored ACK reports itself as such.
    assert first == (not ack_first)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        received = row.closing_ack_received_at
        assert row.status == "closing" and row.transcript_sealed_at is None
        assert row.closing_audio_size == 524 and row.closing_audio_mime == "audio/wav"
        row.closing_ack_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await acknowledge_playback(postgres_sessionmaker, conversation_id, ack) == (
        {"accepted": True, "status": "played", "provisional": False},
        False,
    )
    failed = ack.model_copy(update={"status": "failed"})
    assert (await acknowledge(postgres_sessionmaker, conversation_id, failed))["status"] == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.closing_ack_received_at == received


async def test_browser_timeout_is_recorded_as_timeout_and_a_played_ack_still_wins(
    postgres_sessionmaker,
):
    conversation_id, owner, ack = await attempt(postgres_sessionmaker)
    timeout = ack.model_copy(update={"status": "timeout"})
    result = await acknowledge(postgres_sessionmaker, conversation_id, timeout)
    assert result == {"accepted": True, "status": "timeout", "provisional": False}
    await deliver(postgres_sessionmaker, conversation_id, owner, ack)
    assert (await acknowledge(postgres_sessionmaker, conversation_id, ack))["status"] == "played"


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
    assert (await acknowledge(postgres_sessionmaker, conversation_id, ack))["status"] == "played"
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
    assert await acknowledge_playback(postgres_sessionmaker, conversation_id, ack) == (
        {"accepted": False, "reason": "expired"},
        False,
    )
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


def candidate_token(conversation_id):
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("candidate")
        .with_grants(api.VideoGrants(room_join=True, room=f"interview-{conversation_id}"))
        .to_jwt()
    )


async def test_api_records_the_used_output_once_per_attempt_without_device_names(
    postgres_sessionmaker, monkeypatch, recorded_metrics
):
    monkeypatch.setattr(settings, "livekit_api_key", "playback-test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "playback-test-secret-at-least-32-bytes")
    conversation_id, _owner, ack = await attempt(postgres_sessionmaker)
    app = FastAPI()
    app.state.sessionmaker = postgres_sessionmaker
    app.include_router(router, prefix="/api")
    headers = {"Authorization": f"Bearer {candidate_token(conversation_id)}"}
    body = ack.model_dump(mode="json") | {"audio_output": "selected"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/interviews/{conversation_id}/closing/ack"
        # The browser repeats its ACK; a later one may even change its status.
        for repeat in (body, body, body | {"status": "failed"}):
            response = await client.post(path, headers=headers, json=repeat)
            assert response.status_code == 200
            assert set(response.json()) == {"accepted", "status", "provisional"}
        named = body | {"audio_output": "Studio speakers"}
        assert (await client.post(path, headers=headers, json=named)).status_code == 422
    (point,) = recorded_metrics("interview_agent.browser.farewell_audio_output")
    assert point.count == 1
    assert dict(point.attributes) == {"audio_output": "selected", "farewell_status": "pending"}


async def test_a_failed_ack_records_why_once_as_a_category_without_its_message(
    postgres_sessionmaker, monkeypatch, recorded_metrics, caplog
):
    from interview_agent.log_templates import SAFE_LOG_TEMPLATES

    monkeypatch.setattr(settings, "livekit_api_key", "playback-test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "playback-test-secret-at-least-32-bytes")
    conversation_id, _owner, ack = await attempt(postgres_sessionmaker)
    app = FastAPI()
    app.state.sessionmaker = postgres_sessionmaker
    app.include_router(router, prefix="/api")
    headers = {"Authorization": f"Bearer {candidate_token(conversation_id)}"}
    failed = ack.model_dump(mode="json") | {
        "status": "failed",
        "audio_output": "default",
        "error_kind": "NotSupportedError",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/interviews/{conversation_id}/closing/ack"
        # A browser message, or a kind outside the reviewed categories, is refused.
        for refused in ("The operation is not supported.", "QuotaExceededError"):
            body = failed | {"error_kind": refused}
            assert (await client.post(path, headers=headers, json=body)).status_code == 422
        with caplog.at_level("WARNING", logger="interview_agent.server"):
            for _repeat in range(2):
                response = await client.post(path, headers=headers, json=failed)
                assert response.status_code == 200
                assert response.json()["status"] == "failed"
    (point,) = recorded_metrics("interview_agent.browser.farewell_playback_errors")
    assert point.count == 1
    assert dict(point.attributes) == {"error_type": "NotSupportedError"}
    template = "Browser could not play the farewell"
    assert [record.msg for record in caplog.records] == [template]
    assert template in SAFE_LOG_TEMPLATES


async def test_an_ack_without_a_failure_records_no_error_kind(
    postgres_sessionmaker, monkeypatch, recorded_metrics
):
    monkeypatch.setattr(settings, "livekit_api_key", "playback-test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "playback-test-secret-at-least-32-bytes")
    conversation_id, _owner, ack = await attempt(postgres_sessionmaker)
    app = FastAPI()
    app.state.sessionmaker = postgres_sessionmaker
    app.include_router(router, prefix="/api")
    headers = {"Authorization": f"Bearer {candidate_token(conversation_id)}"}
    # A kind sent along with a played ACK says nothing about a failure.
    played = ack.model_dump(mode="json") | {"error_kind": "AbortError"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/interviews/{conversation_id}/closing/ack"
        assert (await client.post(path, headers=headers, json=played)).status_code == 200
    assert recorded_metrics("interview_agent.browser.farewell_playback_errors") == []


async def test_browser_response_onset_is_authenticated_bounded_and_capped(
    postgres_sessionmaker, monkeypatch, recorded_metrics
):
    monkeypatch.setattr(settings, "livekit_api_key", "playback-test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "playback-test-secret-at-least-32-bytes")
    conversation_id, _owner, _ack = await attempt(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == conversation_id)
            .values(
                run_config={
                    "graph_version": "interview-v1",
                    "config_version": "synthetic-config",
                    "language": "es",
                    "seniority": "junior",
                    "interview_length": "short",
                    "models": {"interviewer": {"model": "gpt-6-astra"}},
                }
            )
        )
        await session.commit()
        updated_at = (await session.get(db.Conversation, conversation_id)).updated_at
    app = FastAPI()
    app.state.sessionmaker = postgres_sessionmaker
    app.include_router(router, prefix="/api")

    def sample(seconds):
        # The browser sends a fresh ID with every sample and never retries.
        return {"sample_id": str(uuid.uuid4()), "seconds": seconds}

    def post(client, interview_id, body):
        return client.post(
            f"/api/interviews/{interview_id}/metrics/response-onset",
            headers={"Authorization": f"Bearer {candidate_token(interview_id)}"},
            json=body,
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/interviews/{conversation_id}/metrics/response-onset"
        assert (await client.post(path, json=sample(2.4))).status_code == 401
        accepted = await post(client, conversation_id, sample(2.4))
        assert accepted.status_code == 202 and accepted.json() == {"accepted": True}
        assert (await post(client, conversation_id, sample(61))).status_code == 422
        assert (await post(client, uuid.uuid4(), sample(2.4))).status_code == 404
        async with postgres_sessionmaker() as session:
            await session.execute(
                text("UPDATE conversations SET response_onset_samples=62 WHERE id=:id"),
                {"id": conversation_id},
            )
            await session.commit()
        # Concurrent samples cannot overshoot the 64 per interview.
        results = await asyncio.gather(
            *(post(client, conversation_id, sample(1.5)) for _ in range(5))
        )
        assert sorted(result.json()["accepted"] for result in results) == [False] * 3 + [True] * 2
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        # A bookkeeping counter: the interview itself did not change.
        assert row.response_onset_samples == 64 and row.updated_at == updated_at
    (point,) = recorded_metrics("interview_agent.browser.response_onset_seconds")
    assert point.count == 3 and point.sum == pytest.approx(5.4)
    assert dict(point.attributes) == {
        "source": "browser_decoded_audio_rms",
        "graph_version": "interview-v1",
        "config_version": "synthetic-config",
        "language": "es",
        "seniority": "junior",
        "length": "short",
        "model": "gpt-6-astra",
    }

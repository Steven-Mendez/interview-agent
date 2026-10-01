"""Startup evidence and schema guard without provider requests."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.metrics import purge_metrics
from interview_agent.runtime import process_manifest, record_manifest


def test_configuration_rejects_reconnect_longer_than_drain():
    with pytest.raises(ValidationError, match="drain"):
        Settings(_env_file=None, WORKER_DRAIN_MINUTES=1, INTERVIEW_RECONNECT_SECONDS=120)


async def test_effective_manifest_distinguishes_defaults_process_snapshot_and_provider():
    settings = Settings(
        _env_file=None,
        OPENAI_API_KEY="CANARY_API_KEY",
        LIVEKIT_API_SECRET="CANARY_LK_SECRET",
        INTERVIEWER_MODEL="gpt-6-astra",
        STT_MODEL="assemblyai/universal-3-6-pro",
    )
    manifest = await process_manifest(
        settings,
        "worker",
        config={
            "models": {"interviewer": {"model": "gpt-6.1-sol", "reasoning_effort": "high"}},
            "stt_model": "snapshot-stt-model",
            "tts_model": "cartesia/test",
            "language": "es",
            "resume": "CANARY_CV",
            "transcript": "CANARY_TRANSCRIPT",
        },
        functions=(process_manifest,),
    )
    encoded = json.dumps(manifest)
    assert "CANARY_" not in encoded
    assert manifest["loaded_defaults"]["interviewer_model"] == "gpt-6-astra"
    assert manifest["process_settings"]["interviewer_model"] == "gpt-6-astra"
    assert manifest["effective_settings"]["interviewer_model"] == "gpt-6.1-sol"
    assert manifest["process_settings"]["stt_model"] == "assemblyai/universal-3-6-pro"
    assert manifest["effective_settings"]["stt_model"] == "snapshot-stt-model"
    assert manifest["factories"]["interviewer"]["requested_model"] == "gpt-6.1-sol"
    assert manifest["factories"]["interviewer"]["provider_reported_model"] is None
    assert manifest["factories"]["interviewer"]["http_attempt_ceiling"] == 2
    assert manifest["provider_calls_performed"] is False
    assert manifest["loaded_callable_hashes"] and manifest["disk_artifact_hashes"]
    assert manifest["loaded_prompt_hashes"]


async def test_manifest_retention_and_conversation_cascade(postgres_sessionmaker):
    cid = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        session.add(
            db.Conversation(
                id=cid, job_offer="Synthetic", resume_markdown="Synthetic", status="completed"
            )
        )
        await session.commit()
    manifest = await process_manifest(Settings(_env_file=None, OPENAI_API_KEY="test"), "api")
    await record_manifest(postgres_sessionmaker, manifest, conversation_id=cid)
    async with postgres_sessionmaker() as session:
        row = (await session.scalars(select(db.ProcessManifest))).one()
        assert row.snapshot["id"] == str(row.id)
        row.created_at = datetime.now(UTC) - timedelta(days=31)
        await session.commit()
        await purge_metrics(session)
        assert list(await session.scalars(select(db.ProcessManifest))) == []


async def test_schema_guard_rejects_stale_revision_before_any_session_starts(postgres_sessionmaker):
    from sqlalchemy import text

    from interview_agent.runtime import validate_database_revision

    assert await validate_database_revision(postgres_sessionmaker) == "34ae6815db20"
    async with postgres_sessionmaker() as session:
        await session.execute(text("UPDATE alembic_version SET version_num='a3e72bc19054'"))
        await session.commit()
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        await validate_database_revision(postgres_sessionmaker)


async def test_api_lifespan_records_effective_manifest_and_shutdowns_without_provider_calls(
    postgres_sessionmaker, monkeypatch
):
    from fastapi import FastAPI

    from interview_agent.server import app as server_app

    engine = postgres_sessionmaker.kw["bind"]
    monkeypatch.setattr(
        server_app, "create_engine_and_sessionmaker", lambda *args: (engine, postgres_sessionmaker)
    )
    monkeypatch.setattr(server_app.settings, "openai_api_key", "synthetic-test-key")
    monkeypatch.setattr(server_app.settings, "livekit_api_key", "synthetic-test-key")
    monkeypatch.setattr(server_app.settings, "livekit_api_secret", "synthetic-test-secret")
    monkeypatch.setattr(server_app.settings, "livekit_url", "wss://synthetic.example")
    monkeypatch.setattr(server_app.settings, "langsmith_api_key", "")
    application = FastAPI()
    async with server_app.lifespan(application):
        assert application.state.runtime_manifest["database_revision"] == "34ae6815db20"
        async with postgres_sessionmaker() as session:
            row = (await session.scalars(select(db.ProcessManifest))).one()
            assert row.role == "api" and row.snapshot["provider_calls_performed"] is False
        assert application.state.evaluations.running == 0

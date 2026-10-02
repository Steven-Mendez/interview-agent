"""Startup evidence and schema guard without provider requests."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.runtime import EXPECTED_REVISION, process_manifest, record_manifest


def test_configuration_rejects_reconnect_longer_than_drain():
    with pytest.raises(ValidationError, match="drain"):
        Settings(_env_file=None, WORKER_DRAIN_MINUTES=1, INTERVIEW_RECONNECT_SECONDS=120)


async def test_engine_pings_pooled_connections_before_reusing_them():
    # A suspended Neon compute drops idle connections; without the ping the
    # first request after a pause would fail on a dead pooled connection.
    engine, _ = db.create_engine_and_sessionmaker("postgresql+asyncpg://user:pass@db.invalid/app")
    try:
        assert engine.sync_engine.pool._pre_ping is True
    finally:
        await engine.dispose()


async def test_engine_refuses_verify_full_without_a_ca_bundle(monkeypatch, tmp_path):
    # infra/neon's DATABASE_URL asks for ssl=verify-full: with no PGSSLROOTCERT
    # (and no ~/.postgresql/root.crt) asyncpg must fail before connecting rather
    # than accept an unverified certificate as ssl=require would.
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    engine, _ = db.create_engine_and_sessionmaker(
        "postgresql+asyncpg://user:pass@db.invalid/app?ssl=verify-full"
    )
    try:
        with pytest.raises(asyncpg.exceptions.ClientConfigurationError, match="root certificate"):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()


def test_a_ca_bundle_is_available_for_verify_full_by_default():
    # config defaults PGSSLROOTCERT, so a Neon DATABASE_URL connects on any
    # host (FastAPI Cloud, the worker image, CI) without exporting it.
    assert os.path.isfile(os.environ["PGSSLROOTCERT"])


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
        await db.expire_manifests(session, 30)
        assert list(await session.scalars(select(db.ProcessManifest))) == []


async def manifest(sessionmaker, days_old):
    identifier = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.ProcessManifest(
                id=identifier,
                role="api",
                snapshot={"id": str(identifier)},
                created_at=datetime.now(UTC) - timedelta(days=days_old),
            )
        )
        await session.commit()
    return identifier


async def conversation(sessionmaker, days_old=0):
    identifier = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=identifier,
                job_offer="Synthetic",
                resume_markdown="Synthetic",
                status="completed",
            )
        )
        await session.commit()
        if days_old:
            await session.execute(
                update(db.Conversation)
                .where(db.Conversation.id == identifier)
                .values(created_at=datetime.now(UTC) - timedelta(days=days_old))
            )
            await session.commit()
    return identifier


async def test_manifest_expiry_drops_old_manifests_only(postgres_sessionmaker):
    cid = await conversation(postgres_sessionmaker)
    await manifest(postgres_sessionmaker, days_old=31)
    recent = await manifest(postgres_sessionmaker, days_old=29)
    async with postgres_sessionmaker() as session:
        await db.expire_manifests(session, 30)
    async with postgres_sessionmaker() as session:
        assert set(await session.scalars(select(db.ProcessManifest.id))) == {recent}
        # The interview itself follows RETENTION_DAYS.
        assert await session.get(db.Conversation, cid)


async def test_purge_loop_expires_manifests_before_the_retention_purge(
    postgres_sessionmaker, monkeypatch
):
    from interview_agent.server import app as server_app

    kept = await conversation(postgres_sessionmaker)
    old = await conversation(postgres_sessionmaker, days_old=8)
    await manifest(postgres_sessionmaker, days_old=31)
    recent = await manifest(postgres_sessionmaker, days_old=29)
    monkeypatch.setattr(server_app.settings, "metrics_detail_days", 30)
    monkeypatch.setattr(server_app.settings, "retention_days", 7)
    sleep = asyncio.sleep

    async def one_cycle(seconds, *args, **kwargs):
        if seconds == server_app._PURGE_INTERVAL_SECONDS:
            raise asyncio.CancelledError
        return await sleep(seconds, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", one_cycle)
    with pytest.raises(asyncio.CancelledError):
        await server_app._purge_loop(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        assert set(await session.scalars(select(db.ProcessManifest.id))) == {recent}
        assert await session.get(db.Conversation, kept)
        # RETENTION_DAYS still deletes old interviews.
        assert await session.get(db.Conversation, old) is None


async def test_schema_guard_rejects_stale_revision_before_any_session_starts(postgres_sessionmaker):
    from sqlalchemy import text

    from interview_agent.runtime import validate_database_revision

    assert await validate_database_revision(postgres_sessionmaker) == EXPECTED_REVISION
    async with postgres_sessionmaker() as session:
        await session.execute(text("UPDATE alembic_version SET version_num='a3e72bc19054'"))
        await session.commit()
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        await validate_database_revision(postgres_sessionmaker)


async def test_api_lifespan_records_effective_manifest_and_shutdowns_without_provider_calls(
    postgres_sessionmaker, monkeypatch
):
    from fastapi import FastAPI
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from interview_agent import otel_metrics
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
    # Production's accounts setup: startup needs both, and fetches no keys.
    monkeypatch.setattr(server_app.settings, "auth_mode", "neon")
    monkeypatch.setattr(
        server_app.settings, "neon_auth_url", "https://ep-synthetic.neonauth.example/neondb/auth"
    )
    monkeypatch.setattr(server_app.settings, "internal_api_token", "synthetic-internal-token")
    reader, services, configure = InMemoryMetricReader(), [], otel_metrics.configure

    def in_memory(settings, service_name):
        services.append(service_name)
        return configure(settings, service_name, reader=reader)

    monkeypatch.setattr(otel_metrics, "configure", in_memory)
    application = FastAPI()
    async with server_app.lifespan(application):
        assert application.state.runtime_manifest["database_revision"] == EXPECTED_REVISION
        async with postgres_sessionmaker() as session:
            row = (await session.scalars(select(db.ProcessManifest))).one()
            assert row.role == "api" and row.snapshot["provider_calls_performed"] is False
        assert application.state.evaluations.running == 0
    assert services == ["interview-agent-api"]
    # Shut down with the process: later samples go nowhere.
    otel_metrics.record("server", "after_shutdown", 1)
    data = reader.get_metrics_data()
    assert "interview_agent.server.after_shutdown" not in {
        metric.name
        for resource in (data.resource_metrics if data else [])
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }

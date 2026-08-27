"""API route tests against a real (test) Postgres, with the LLM/RAG calls
mocked out.

The app is assembled by hand (router + app.state) instead of importing the
real `app`, whose lifespan needs Qdrant and validated API keys. The test
database is created on the fly; locally it lands on the docker-compose
Postgres, in CI on the service container (TEST_DATABASE_URL overrides).
"""

from __future__ import annotations

import itertools
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from interview_agent import agent
from interview_agent.config import settings
from interview_agent.interview import db, rag
from interview_agent.interview.models import (
    EvaluationResult,
    InterviewLength,
    InterviewPlan,
    MilestoneSpec,
    Seniority,
)
from interview_agent.prompts import length_for
from interview_agent.server import routes
from interview_agent.voices import VOICES

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://interview:interview@localhost:5432/interview_test",
)


def _plan(detected: Seniority | None = None) -> InterviewPlan:
    return InterviewPlan(
        persona="Laura, engineering manager",
        summary="Solid candidate.",
        focus_areas=["Kubernetes"],
        detected_seniority=detected,
        seniority_evidence="The offer asks for 1-2 years." if detected else None,
        milestones=[
            MilestoneSpec(
                title=f"M{i}", description="Probe it.", expected_evidence="Names one index."
            )
            for i in range(4)
        ],
    )


def _evaluation() -> EvaluationResult:
    return EvaluationResult(
        hired=True,
        score=82,
        strengths=["clear communication"],
        weaknesses=["little SQL depth"],
        rationale="Convincing on most milestones.",
        seniority_evaluated=Seniority.MID,
        calibration_notes=["Skipped trade-off depth: above this level."],
    )


async def _ensure_test_database() -> None:
    """CREATE DATABASE if missing — Postgres has no CREATE ... IF NOT EXISTS."""
    url = make_url(TEST_DATABASE_URL)
    admin = create_async_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": url.database},
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


@pytest.fixture
async def client_and_sessionmaker(monkeypatch):
    await _ensure_test_database()
    engine, sessionmaker = db.create_engine_and_sessionmaker(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # The test database persists across runs and create_all never ALTERs
        # an existing table — rebuild from scratch so schema changes (new
        # columns/tables) land, and every test starts from a clean slate.
        await conn.run_sync(db.Base.metadata.drop_all)
        await conn.run_sync(db.Base.metadata.create_all)

    # The routes only touch qdrant through rag helpers — stub those instead
    # of faking a client.
    async def _no_index(*args, **kwargs) -> int:
        return 3

    async def _no_delete(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(rag, "index_resume", _no_index)
    monkeypatch.setattr(rag, "delete_resume_points", _no_delete)
    monkeypatch.setattr(rag, "pdf_to_markdown", lambda data, filename: "# Resume\nPython dev.")
    monkeypatch.setattr(routes, "run_planner", _fake_planner)

    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.state.sessionmaker = sessionmaker
    app.state.qdrant = object()  # only ever handed to the stubbed rag helpers
    app.state.embeddings = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sessionmaker
    await engine.dispose()


async def _fake_planner(settings, resume_markdown, job_offer, **kwargs) -> InterviewPlan:
    return _plan()


async def _fake_evaluator(settings, **kwargs) -> EvaluationResult:
    return _evaluation()


def _upload(job_offer: str = "Backend engineer at ACME.", **extra: str):
    return {
        "files": {"resume": ("cv.pdf", b"%PDF-fake", "application/pdf")},
        "data": {"job_offer": job_offer, **extra},
    }


def _interviewer(**fields: str) -> str:
    """The `interviewer` form field. Only the keys passed are sent, so the
    test controls exactly what inherits and what overrides."""
    return json.dumps(fields)


async def _seed_finished_interview(sessionmaker) -> uuid.UUID:
    """A completed interview with a transcript, ready to evaluate."""
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                status="completed",
                ended_reason="plan_complete",
                job_offer="offer",
                resume_markdown="# Resume",
                plan={"language": "en", "summary": "s", "focus_areas": []},
            )
        )
        session.add(
            db.Milestone(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                position=0,
                title="M0",
                description="Probe it.",
                completed=True,
            )
        )
        await session.commit()
        for seq, (role, content) in enumerate(
            [("assistant", "Tell me about X."), ("user", "I built X.")]
        ):
            await db.insert_message(session, conversation_id, role, content, seq=seq)
    return conversation_id


# ---- /settings ---------------------------------------------------------------


async def test_get_settings_returns_defaults_and_catalog(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["agent_name"] == "Emma"
    assert body["language"] == "en"
    assert body["voice"] == "en_female"
    assert body["persona"] is None
    # Two curated voices per language: one feminine, one masculine.
    for language in ("en", "es"):
        genders = {v["gender"] for v in body["voices"][language]}
        assert genders == {"female", "male"}


async def test_put_settings_persists_and_echoes(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    payload = {
        "agent_name": "Sam",
        "language": "es",
        "voice": "es_male",
        "persona": "una manager exigente",
        "custom_instructions": "",
    }
    res = await client.put("/api/settings", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["agent_name"] == "Sam"
    assert body["voice"] == "es_male"
    assert body["custom_instructions"] is None  # "" normalized to NULL

    # Persisted: a fresh GET returns the same values.
    body = (await client.get("/api/settings")).json()
    assert body["language"] == "es"
    assert body["persona"] == "una manager exigente"


async def test_put_settings_rejects_voice_language_mismatch(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.put(
        "/api/settings", json={"agent_name": "Alex", "language": "en", "voice": "es_male"}
    )
    assert res.status_code == 422

    res = await client.put(
        "/api/settings", json={"agent_name": "Alex", "language": "fr", "voice": "en_female"}
    )
    assert res.status_code == 422


# ---- /interviews ------------------------------------------------------------


async def test_create_interview_happy_path(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload())
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "planned"
    assert len(body["milestones"]) == 4
    assert body["plan"]["language"] == "en"  # injected from the settings
    assert "milestones" not in body["plan"]  # stored separately
    assert "planner" in body["token_usage"]


async def test_create_interview_snapshots_settings(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    res = await client.put(
        "/api/settings",
        json={
            "agent_name": "Sam",
            "language": "es",
            "voice": "es_female",
            "persona": "una manager exigente",
            "custom_instructions": "máximo 5 preguntas",
        },
    )
    assert res.status_code == 200

    job_offer = f"offer-{uuid.uuid4()}"  # unique marker to find the row
    res = await client.post("/api/interviews", **_upload(job_offer))
    assert res.status_code == 200
    assert res.json()["plan"]["language"] == "es"

    async with sessionmaker() as session:
        row = await session.scalar(
            select(db.Conversation).where(db.Conversation.job_offer == job_offer)
        )
    assert row.persona == "una manager exigente"
    assert row.custom_instructions == "máximo 5 preguntas"
    snapshot = row.agent_settings
    assert snapshot["agent_name"] == "Sam"
    assert snapshot["language"] == "es"
    assert snapshot["voice"] == "es_female"
    assert snapshot["tts_model"] == "cartesia/sonic-3"
    assert snapshot["tts_voice"]  # resolved, not just the catalog key


async def test_create_interview_rejects_non_pdf(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post(
        "/api/interviews",
        files={"resume": ("cv.docx", b"bytes", "application/msword")},
        data={"job_offer": "offer"},
    )
    assert res.status_code == 400


async def test_create_interview_planning_failure_marks_error(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker

    async def _boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(routes, "run_planner", _boom)
    job_offer = f"offer-{uuid.uuid4()}"  # unique marker to find the row
    res = await client.post("/api/interviews", **_upload(job_offer))
    assert res.status_code == 500

    # The row survives with a visible error status (not stuck in "created").
    async with sessionmaker() as session:
        status = await session.scalar(
            text("SELECT status FROM conversations WHERE job_offer = :o"),
            {"o": job_offer},
        )
    assert status == "error"


async def test_get_interview_404(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get(f"/api/interviews/{uuid.uuid4()}")
    assert res.status_code == 404


# ---- /evaluate --------------------------------------------------------------


async def test_evaluate_happy_and_idempotent(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "evaluated"
    assert body["evaluation"]["hired"] is True
    assert body["evaluation"]["score"] == 82
    assert body["evaluation"]["ended_by"] == "plan_complete"

    # Re-evaluation upserts instead of racing delete+insert into a 500.
    res2 = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res2.status_code == 200
    assert res2.json()["evaluation"]["score"] == 82


async def test_evaluate_without_transcript_409(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id, status="completed", job_offer="o", resume_markdown="r"
            )
        )
        await session.commit()

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 409


async def test_evaluate_refuses_a_live_interview(client_and_sessionmaker, monkeypatch):
    # Evaluating mid-interview would score half a transcript and, worse, purge
    # the resume chunks from Qdrant — blinding search_resume for the rest of
    # the live interview. The worker only POSTs here after marking "completed".
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()
        await db.insert_message(session, conversation_id, "user", "mid-answer", seq=0)

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"]


async def test_evaluate_failure_sets_status_and_retry_recovers(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    conversation_id = await _seed_finished_interview(sessionmaker)

    async def _boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(routes, "run_evaluator", _boom)
    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 502
    status = (await client.get(f"/api/interviews/{conversation_id}")).json()["status"]
    assert status == "evaluation_failed"

    # The endpoint stays re-invocable: a later retry succeeds.
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    res2 = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res2.status_code == 200
    assert res2.json()["status"] == "evaluated"


# ---- DB helpers -------------------------------------------------------------


async def test_add_token_usage_merges_components(client_and_sessionmaker):
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id, status="created", job_offer="o", resume_markdown="r"
            )
        )
        await session.commit()
        await db.add_token_usage(
            session,
            conversation_id,
            "interviewer",
            {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        )
        await db.add_token_usage(
            session,
            conversation_id,
            "interviewer",
            {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55},
        )
        await db.add_token_usage(
            session,
            conversation_id,
            "evaluator",
            {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )
        conversation = await db.get_conversation(session, conversation_id)
    assert conversation.token_usage == {
        "interviewer": {"input_tokens": 150, "output_tokens": 15, "total_tokens": 165},
        "evaluator": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    }


async def test_token_allows_a_fresh_reconnect(client_and_sessionmaker, monkeypatch):
    # A crash never marks the row completed, so "interviewing" is how a
    # resumable interview looks. Recent ones must still get a token.
    client, sessionmaker = client_and_sessionmaker
    # Minting the JWT needs real credentials; settings default to "" and CI
    # has no .env, so supply throwaway ones rather than depend on the
    # environment (which is what made this pass locally and fail in CI).
    monkeypatch.setattr(settings, "livekit_api_key", "devkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "devsecret" * 4)
    monkeypatch.setattr(settings, "livekit_url", "wss://example.livekit.cloud")
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()

    res = await client.get(f"/api/interviews/{conversation_id}/token")
    assert res.status_code == 200
    assert res.json()["room"] == f"interview-{conversation_id}"


async def test_token_refuses_a_stale_reconnect(client_and_sessionmaker):
    # An orphaned "interviewing" row lives until the retention purge. Without a
    # bound, a candidate could rejoin days later — and since the worker charges
    # the elapsed time against the cap, they would be greeted and wrapped up in
    # the same breath.
    client, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    stale = datetime.now(UTC) - timedelta(minutes=settings.interview_max_minutes + 30)
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()
        # onupdate=func.now() fires on any UPDATE, so age the row directly.
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == conversation_id)
            .values(updated_at=stale)
        )
        await session.commit()

    res = await client.get(f"/api/interviews/{conversation_id}/token")
    assert res.status_code == 409


async def test_get_messages_orders_by_seq_not_id(client_and_sessionmaker):
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id, status="created", job_offer="o", resume_markdown="r"
            )
        )
        await session.commit()
        # Committed out of order (higher seq first), as racing persist tasks
        # can do — read order must follow seq regardless.
        await db.insert_message(session, conversation_id, "assistant", "second", seq=1)
        await db.insert_message(session, conversation_id, "user", "first", seq=0)
        messages = await db.get_messages(session, conversation_id)
    assert [m.content for m in messages] == ["first", "second"]


async def test_resumed_job_appends_instead_of_interleaving(client_and_sessionmaker):
    # A worker crash mid-interview leaves the row "interviewing", so a reload
    # dispatches a second job. It must continue the transcript, not renumber
    # from 0 — which order_by(seq, id) would shuffle into the first half.
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()
        for seq, (role, content) in enumerate(
            [("assistant", "greeting"), ("user", "answer 1"), ("assistant", "question 2")]
        ):
            await db.insert_message(session, conversation_id, role, content, seq=seq)

        prior = await db.get_messages(session, conversation_id)
        assert agent._next_seq(prior) == 3
        resumed_seq = itertools.count(agent._next_seq(prior))

        for role, content in [("assistant", "welcome back"), ("user", "answer 2")]:
            await db.insert_message(
                session, conversation_id, role, content, seq=next(resumed_seq)
            )
        messages = await db.get_messages(session, conversation_id)

    assert [m.content for m in messages] == [
        "greeting",
        "answer 1",
        "question 2",
        "welcome back",
        "answer 2",
    ]
    assert [m.seq for m in messages] == [0, 1, 2, 3, 4]


# ---- /healthz ---------------------------------------------------------------


# ---- per-interview interviewer ----------------------------------------------


async def test_create_interview_takes_a_per_interview_interviewer(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    # Global settings say English/Emma; this interview asks for Spanish/Sam.
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "en",
            "voice": "en_female",
            "persona": "a global persona",
        },
    )
    res = await client.post(
        "/api/interviews",
        **_upload(
            interviewer=_interviewer(
                agent_name="Sam",
                language="es",
                voice="es_male",
                persona="una manager exigente",
                custom_instructions="Pregunta por Kubernetes.",
            )
        ),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["interviewer"] == {
        "agent_name": "Sam",
        "language": "es",
        "voice": "es_male",
    }
    assert body["plan"]["language"] == "es"

    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None
        # The voice resolved to a concrete TTS pair for the worker.
        assert row.agent_settings["tts_voice"] == VOICES["es_male"]["tts_voice"]
        assert row.persona == "una manager exigente"
        assert row.custom_instructions == "Pregunta por Kubernetes."

    # The global settings are untouched by the per-interview choice.
    assert (await client.get("/api/settings")).json()["language"] == "en"


async def test_create_interview_without_an_interviewer_uses_the_settings(
    client_and_sessionmaker,
):
    client, sessionmaker = client_and_sessionmaker
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "es",
            "voice": "es_female",
            "persona": "una manager exigente",
        },
    )
    body = (await client.post("/api/interviews", **_upload())).json()
    assert body["interviewer"]["language"] == "es"
    assert body["interviewer"]["voice"] == "es_female"
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None and row.persona == "una manager exigente"


async def test_an_empty_persona_clears_it_for_this_interview_only(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "en",
            "voice": "en_female",
            "persona": "a global persona",
        },
    )
    # "" is a real answer — run this one WITHOUT a persona — while omitting
    # the field inherits. Both must not touch the stored settings.
    body = (
        await client.post("/api/interviews", **_upload(interviewer=_interviewer(persona="")))
    ).json()
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None and row.persona is None
    assert (await client.get("/api/settings")).json()["persona"] == "a global persona"


async def test_create_interview_rejects_an_impossible_voice(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post(
        "/api/interviews", **_upload(interviewer=_interviewer(language="en", voice="es_male"))
    )
    assert res.status_code == 400
    assert "not available" in res.json()["detail"]

    res = await client.post(
        "/api/interviews", **_upload(interviewer=_interviewer(voice="klingon"))
    )
    assert res.status_code == 400
    res = await client.post(
        "/api/interviews", **_upload(interviewer=_interviewer(language="fr", voice="en_female"))
    )
    assert res.status_code == 400
    # Malformed JSON is a 400 too, not a 500.
    res = await client.post("/api/interviews", **_upload(interviewer="{nope"))
    assert res.status_code == 400


async def test_repeat_keeps_the_original_interviewer(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    source = (
        await client.post(
            "/api/interviews",
            **_upload(
                interviewer=_interviewer(
                    agent_name="Sam", language="es", voice="es_male", persona="exigente"
                )
            ),
        )
    ).json()

    # The global settings move on AFTER the original ran.
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Nova",
            "language": "en",
            "voice": "en_female",
            "persona": "a brand new persona",
        },
    )
    repeat = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    assert repeat["interviewer"] == source["interviewer"]
    assert repeat["plan"]["language"] == "es"
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(repeat["id"]))
        assert row is not None and row.persona == "exigente"


async def test_repeat_of_a_personaless_interview_stays_personaless(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    source = (
        await client.post("/api/interviews", **_upload(interviewer=_interviewer(persona="")))
    ).json()
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "en",
            "voice": "en_female",
            "persona": "a persona added later",
        },
    )
    repeat = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(repeat["id"]))
        # Inheriting "none" must not pick up the persona the settings grew.
        assert row is not None and row.persona is None


# ---- history / repeat --------------------------------------------------------


async def test_history_lists_newest_first_with_a_summary(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    first = (await client.post("/api/interviews", **_upload("# Backend engineer\nACME."))).json()
    second = (await client.post("/api/interviews", **_upload("Data engineer at Beta."))).json()
    # Same-second creations: order the rows explicitly so "newest first" is
    # about created_at, not about which insert happened to land first.
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == uuid.UUID(first["id"]))
            .values(created_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    res = await client.get("/api/interviews")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [second["id"], first["id"]]

    row = body["items"][1]
    # The title is the offer's first line, with the markdown hash stripped.
    assert row["title"] == "Backend engineer"
    assert row["resume_filename"] == "cv.pdf"
    assert row["status"] == "planned"
    assert row["milestones_total"] == 4
    assert row["milestones_completed"] == 0
    assert row["evaluation"] is None
    assert row["repeat_of_id"] is None
    # The heavy fields stay out of the list.
    assert "plan" not in row and "job_offer" not in row


async def test_history_paginates_and_filters_by_status(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    await client.post("/api/interviews", **_upload())
    await client.post("/api/interviews", **_upload())
    evaluated_id = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        await db.set_status(session, evaluated_id, "evaluated")

    body = (await client.get("/api/interviews", params={"limit": 1})).json()
    assert body["total"] == 3 and len(body["items"]) == 1
    page_two = (await client.get("/api/interviews", params={"limit": 1, "offset": 1})).json()
    assert page_two["items"][0]["id"] != body["items"][0]["id"]

    filtered = (await client.get("/api/interviews", params={"status": "evaluated"})).json()
    assert [item["id"] for item in filtered["items"]] == [str(evaluated_id)]
    assert filtered["total"] == 1

    assert (await client.get("/api/interviews", params={"status": "nope"})).status_code == 400
    assert (await client.get("/api/interviews", params={"limit": 0})).status_code == 422


async def test_history_row_carries_the_score(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)
    await client.post(f"/api/interviews/{conversation_id}/evaluate")

    body = (await client.get("/api/interviews")).json()
    row = next(item for item in body["items"] if item["id"] == str(conversation_id))
    assert row["status"] == "evaluated"
    assert row["evaluation"] == {"hired": True, "score": 82}
    assert row["milestones_completed"] == 1


async def test_transcript_returns_the_turns_in_order(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    conversation_id = await _seed_finished_interview(sessionmaker)
    res = await client.get(f"/api/interviews/{conversation_id}/transcript")
    assert res.status_code == 200
    messages = res.json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("assistant", "Tell me about X."),
        ("user", "I built X."),
    ]
    assert messages[0]["created_at"]

    assert (await client.get(f"/api/interviews/{uuid.uuid4()}/transcript")).status_code == 404


async def test_repeat_replans_the_same_role_into_a_new_interview(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    source = (
        await client.post(
            "/api/interviews",
            **_upload("Backend engineer at ACME.", seniority="senior", interview_length="deep"),
        )
    ).json()

    res = await client.post(f"/api/interviews/{source['id']}/repeat")
    assert res.status_code == 200
    repeat = res.json()
    assert repeat["id"] != source["id"]
    assert repeat["status"] == "planned"
    # Same inputs and same calibration, freshly planned milestones.
    assert repeat["job_offer"] == source["job_offer"]
    assert repeat["resume_filename"] == "cv.pdf"
    assert repeat["seniority"] == "senior"
    assert repeat["seniority_source"] == "explicit"
    assert repeat["interview_length"] == "deep"
    assert len(repeat["milestones"]) == 4
    assert repeat["repeat_of_id"] == source["id"]
    assert "planner" in repeat["token_usage"]

    # The original is untouched, and both show up in the history.
    assert (await client.get(f"/api/interviews/{source['id']}")).json()["status"] == "planned"
    assert (await client.get("/api/interviews")).json()["total"] == 2

    # The stored resume markdown is what gets re-indexed and re-planned.
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(repeat["id"]))
        assert row is not None and row.resume_markdown == "# Resume\nPython dev."


async def test_repeat_of_a_repeat_points_back_at_the_original(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    source = (await client.post("/api/interviews", **_upload())).json()
    first = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    second = (await client.post(f"/api/interviews/{first['id']}/repeat")).json()
    # The chain flattens: every attempt groups under the original's id.
    assert first["repeat_of_id"] == source["id"]
    assert second["repeat_of_id"] == source["id"]


async def test_repeat_keeps_an_auto_detected_level_marked_as_auto(
    client_and_sessionmaker, monkeypatch
):
    client, _ = client_and_sessionmaker

    async def _detecting_planner(settings, resume_markdown, job_offer, **kwargs):
        return _plan(detected=Seniority.JUNIOR)

    monkeypatch.setattr(routes, "run_planner", _detecting_planner)
    source = (await client.post("/api/interviews", **_upload())).json()
    assert source["seniority_source"] == "detected"

    # The planner is told the answer this time (seniority is pinned), so it
    # classifies nothing — the provenance has to be carried, not re-derived.
    repeat = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    assert repeat["seniority"] == "junior"
    assert repeat["seniority_source"] == "detected"
    assert repeat["seniority_evidence"] == source["seniority_evidence"]


async def test_repeat_accepts_overrides_and_404s_on_an_unknown_id(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    source = (
        await client.post("/api/interviews", **_upload("Offer.", seniority="lead"))
    ).json()

    repeat = (
        await client.post(
            f"/api/interviews/{source['id']}/repeat",
            json={"seniority": "junior", "interview_length": "short"},
        )
    ).json()
    assert repeat["seniority"] == "junior"
    assert repeat["seniority_source"] == "explicit"
    assert repeat["interview_length"] == "short"
    # The overridden length re-derives this interview's own time cap.
    assert repeat["max_minutes"] == min(
        length_for(InterviewLength.SHORT)["minutes"], settings.interview_max_minutes
    )

    bad = await client.post(
        f"/api/interviews/{source['id']}/repeat", json={"seniority": "wizard"}
    )
    assert bad.status_code == 400
    assert (await client.post(f"/api/interviews/{uuid.uuid4()}/repeat")).status_code == 404


async def test_healthz_ok(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get("/api/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# ---- Seniority / length calibration ------------------------------------------


async def test_create_interview_defaults_to_auto_and_takes_the_detected_level(
    client_and_sessionmaker, monkeypatch
):
    """No level in the form: the planner classifies it ONCE and the server
    pins the result. Every later stage reads that pinned value."""

    async def _detecting_planner(settings, resume_markdown, job_offer, **kwargs):
        assert kwargs["seniority"] is None  # i.e. "classify it yourself"
        return _plan(detected=Seniority.JUNIOR)

    monkeypatch.setattr(routes, "run_planner", _detecting_planner)
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload())
    assert res.status_code == 200
    body = res.json()
    assert body["seniority"] == "junior"
    assert body["seniority_source"] == "detected"
    assert body["seniority_evidence"] == "The offer asks for 1-2 years."
    assert body["interview_length"] == "standard"
    # Pinned in columns, deliberately not duplicated into the plan JSON.
    assert "detected_seniority" not in body["plan"]
    assert "seniority_evidence" not in body["plan"]


async def test_explicit_seniority_wins_and_the_planner_never_classifies(
    client_and_sessionmaker, monkeypatch
):
    seen = {}

    async def _planner(settings, resume_markdown, job_offer, **kwargs):
        seen["seniority"] = kwargs["seniority"]
        seen["length"] = kwargs["interview_length"]
        # Even if the planner does classify, the user's choice must win.
        return _plan(detected=Seniority.SENIOR)

    monkeypatch.setattr(routes, "run_planner", _planner)
    client, _ = client_and_sessionmaker
    res = await client.post(
        "/api/interviews", **_upload(seniority="junior", interview_length="short")
    )
    assert res.status_code == 200
    body = res.json()
    assert seen["seniority"] is Seniority.JUNIOR
    assert seen["length"] is InterviewLength.SHORT
    assert body["seniority"] == "junior"
    assert body["seniority_source"] == "explicit"
    assert body["seniority_evidence"] is None


async def test_interview_length_sets_this_interviews_own_time_cap(
    client_and_sessionmaker,
):
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload(interview_length="short"))
    assert res.status_code == 200
    body = res.json()
    assert body["interview_length"] == "short"
    # Clamped by the global setting, so it can only ever be shorter.
    assert body["max_minutes"] == min(8, settings.interview_max_minutes)


@pytest.mark.parametrize(
    ("field", "value"),
    [("seniority", "archmage"), ("interview_length", "epic")],
)
async def test_create_interview_rejects_unknown_axis_values(
    client_and_sessionmaker, field, value
):
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload(**{field: value}))
    assert res.status_code == 400


async def test_milestones_carry_their_bar_to_the_api(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload())
    assert res.status_code == 200
    assert all(m["expected_evidence"] == "Names one index." for m in res.json()["milestones"])


async def test_evaluate_judges_against_the_pinned_level(
    client_and_sessionmaker, monkeypatch
):
    """The evaluator is HANDED the level and the per-milestone bars; it never
    re-infers seniority from how advanced the stack sounds."""
    client, sessionmaker = client_and_sessionmaker
    interview_id = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == interview_id)
            .values(seniority="junior")
        )
        await session.commit()

    seen = {}

    async def _capturing_evaluator(settings, **kwargs):
        seen.update(kwargs)
        return _evaluation()

    monkeypatch.setattr(routes, "run_evaluator", _capturing_evaluator)
    res = await client.post(f"/api/interviews/{interview_id}/evaluate")
    assert res.status_code == 200
    assert seen["seniority"] == "junior"
    assert all("expected_evidence" in m for m in seen["milestones"])

    evaluation = res.json()["evaluation"]
    assert evaluation["seniority_evaluated"] == "mid"  # what the fake returned
    assert evaluation["calibration_notes"] == [
        "Skipped trade-off depth: above this level."
    ]

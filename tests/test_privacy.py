"""Real database races plus external API contract checks, without paid calls."""

import asyncio
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select, update

from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.metrics import purge_metrics, record_metric
from interview_agent.observability import Telemetry
from interview_agent.privacy import (
    ExternalDeletionWorker,
    LangSmithDeletionAPI,
    delete_conversation,
    guarded_export,
    register_trace,
    retire_traces,
)


def settings(**kwargs):
    return Settings(_env_file=None, **{"LANGSMITH_API_KEY": "synthetic-test-key", **kwargs})


async def setup_trace(sessionmaker):
    conversation_id, trace_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                job_offer="CANARY_OFFER",
                resume_markdown="CANARY_CV",
                status="completed",
            )
        )
        await session.commit()
    await register_trace(sessionmaker, trace_id, conversation_id, settings())
    return conversation_id, trace_id


async def make_due(sessionmaker, identifier):
    async with sessionmaker() as session:
        await session.execute(
            update(db.ExternalTrace)
            .where(db.ExternalTrace.id == identifier)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()


async def test_canaries_and_tombstone_discard_pending_exports_and_preserve_rollups(
    postgres_sessionmaker, monkeypatch
):
    cid, _ = await setup_trace(postgres_sessionmaker)
    client = Mock()
    monkeypatch.setattr("interview_agent.observability.Client", lambda **kwargs: client)
    telemetry = Telemetry(
        postgres_sessionmaker,
        cid,
        settings(),
        config={"resume": "CANARY_CV", "job_offer": "CANARY_OFFER"},
    )
    async with telemetry.span("planner"):
        await telemetry.export_span(
            uuid.uuid4(),
            "interviewer",
            datetime.now(UTC),
            datetime.now(UTC),
            {"transcript": "CANARY_ANSWER", "quote": "CANARY_QUOTE"},
        )
    await telemetry.record("planner", "duration_seconds", 1)
    await telemetry.drain()
    assert client.create_run.call_count == 3
    assert "CANARY_" not in str(client.mock_calls)
    async with postgres_sessionmaker() as session:
        assert await delete_conversation(session, cid)
        assert list(await session.scalars(select(db.MetricEvent))) == []
        assert len(list(await session.scalars(select(db.MetricAggregate)))) == 1
        row = await session.get(db.ExternalTrace, telemetry.trace_id)
        assert row.state == "pending" and row.conversation_id is None
    before = len(client.mock_calls)
    await telemetry.export_span(
        uuid.uuid4(), "interviewer", datetime.now(UTC), datetime.now(UTC), {}
    )
    await telemetry.drain()
    assert len(client.mock_calls) == before
    assert not await register_trace(postgres_sessionmaker, telemetry.trace_id, cid, settings())


async def test_cancelled_inflight_export_finishes_before_deletion_can_tombstone(
    postgres_sessionmaker,
):
    cid, tid = await setup_trace(postgres_sessionmaker)
    started, gate = threading.Event(), threading.Event()
    order = []

    def export():
        started.set()
        assert gate.wait(3)
        order.append("export_finished")

    task = asyncio.create_task(guarded_export(postgres_sessionmaker, tid, export))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()

    async def remove():
        async with postgres_sessionmaker() as session:
            await delete_conversation(session, cid)
        order.append("tombstoned")

    deletion = asyncio.create_task(remove())
    try:
        await asyncio.sleep(0.03)
        assert not deletion.done()
    finally:
        gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await deletion
    assert order == ["export_finished", "tombstoned"]
    assert not await guarded_export(postgres_sessionmaker, tid, lambda: order.append("resurrected"))


class FakeAPI:
    def __init__(self):
        self.submitted = []
        self.missing = False
        self.error = False

    async def project_id(self, name):
        return uuid.uuid4()

    async def submit(self, tid, pid):
        self.submitted.append(tid)
        if self.error:
            raise ConnectionError("CANARY_SECRET")

    async def absent(self, tid, pid):
        return self.missing


async def test_submission_ack_is_pending_until_external_query_verifies_removal(
    postgres_sessionmaker,
):
    cid, tid = await setup_trace(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await retire_traces(session, conversation_id=cid)
        await session.commit()
    api = FakeAPI()
    worker = ExternalDeletionWorker(postgres_sessionmaker, settings(), api=api)
    assert await worker.once()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "verifying" and row.deleted_at is None and row.attempts == 1
    await make_due(postgres_sessionmaker, tid)
    await worker.once()  # root/children still exist
    async with postgres_sessionmaker() as session:
        assert (await session.get(db.ExternalTrace, tid)).state == "verifying"
    api.missing = True
    await make_due(postgres_sessionmaker, tid)
    await worker.once()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "completed" and row.deleted_at is not None
        assert row.conversation_id is None and row.project_name == ""
    # Re-registering after a process restart cannot resurrect the same trace.
    await register_trace(postgres_sessionmaker, tid, cid, settings())
    assert not await guarded_export(postgres_sessionmaker, tid, lambda: pytest.fail("reexport"))
    assert len(api.submitted) == 1


async def test_submission_retries_reserved_durably_and_failure_messages_redacted(
    postgres_sessionmaker,
):
    cid, tid = await setup_trace(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await retire_traces(session, conversation_id=cid)
        await session.commit()
    api = FakeAPI()
    api.error = True
    worker = ExternalDeletionWorker(postgres_sessionmaker, settings(), api=api)
    for _ in range(4):
        await make_due(postgres_sessionmaker, tid)
        await worker.once()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "failed" and row.attempts == 3
        assert "CANARY_SECRET" not in str(row.last_error)
    assert len(api.submitted) == 3
    assert not await worker.once()


async def test_external_detail_retention_unlinks_trace_and_keeps_anonymous_rollup(
    postgres_sessionmaker,
):
    cid, tid = await setup_trace(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await record_metric(
            session,
            db.MetricEvent(
                id=uuid.uuid4(),
                conversation_id=cid,
                component="graph",
                name="duration_seconds",
                value=2,
                dimensions={"trace_id": str(tid), "language": "es"},
            ),
        )
        await session.execute(
            update(db.ExternalTrace)
            .where(db.ExternalTrace.id == tid)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
        await purge_metrics(session)
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "pending" and row.conversation_id is None
        assert await session.get(db.Conversation, cid)
        assert list(await session.scalars(select(db.MetricEvent))) == []
        await record_metric(
            session,
            db.MetricEvent(
                id=uuid.uuid4(),
                conversation_id=cid,
                component="graph",
                name="duration_seconds",
                value=99,
                dimensions={"trace_id": str(tid)},
            ),
        )
        assert list(await session.scalars(select(db.MetricEvent))) == []
        assert (await session.scalars(select(db.MetricAggregate))).one().total == 2


async def test_missing_credentials_stays_visible_without_spending_retry_budget(
    postgres_sessionmaker,
):
    cid, tid = await setup_trace(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await retire_traces(session, conversation_id=cid)
        await session.commit()
    worker = ExternalDeletionWorker(
        postgres_sessionmaker, settings(LANGSMITH_API_KEY=""), api=FakeAPI()
    )
    await worker.once()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "pending" and row.attempts == 0
        assert row.last_error == "missing_credentials"


async def test_real_http_contract_targets_exact_trace_and_checks_all_children():
    pid, tid = uuid.uuid4(), uuid.uuid4()
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(200, json=[{"id": str(pid), "name": "interview-agent-v2"}])
        if request.url.path == "/api/v1/runs/delete":
            assert json.loads(request.content) == {"trace_ids": [str(tid)], "session_id": str(pid)}
            return httpx.Response(200, json={})
        assert request.url.path == "/runs/query"
        body = json.loads(request.content)
        assert body["trace"] == str(tid) and body["session"] == [str(pid)]
        assert body["select"] == ["id"]  # no content downloaded
        return httpx.Response(200, json={"runs": []})

    api = LangSmithDeletionAPI(settings(), transport=httpx.MockTransport(respond))
    try:
        assert await api.project_id("interview-agent-v2") == pid
        await api.submit(tid, pid)
        assert await api.absent(tid, pid)
    finally:
        await api.close()
    assert len(requests) == 3


async def test_crashed_deletion_claim_keeps_original_attempt_and_can_be_recovered(
    postgres_sessionmaker,
):
    cid, tid = await setup_trace(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await retire_traces(session, conversation_id=cid)
        await session.commit()
    entered, gate = asyncio.Event(), asyncio.Event()
    api = FakeAPI()

    async def blocked(tid, pid):
        entered.set()
        await gate.wait()

    api.submit = blocked
    worker = ExternalDeletionWorker(postgres_sessionmaker, settings(), api=api)
    task = asyncio.create_task(worker.once())
    await entered.wait()
    assert not await worker.once()  # a second process cannot claim a live lease
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        original = row.deletion_requested_at
        assert row.attempts == 1 and row.lease_owner is not None
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    recovery = ExternalDeletionWorker(postgres_sessionmaker, settings(), api=FakeAPI())
    assert await recovery.once()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "verifying" and row.attempts == 2
        assert row.deletion_requested_at == original

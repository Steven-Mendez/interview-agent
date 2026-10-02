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
from interview_agent.observability import Telemetry
from interview_agent.privacy import (
    MAX_FAILURES,
    ExternalDeletionWorker,
    LangSmithDeletionAPI,
    delete_conversation,
    expire_detail,
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


async def test_every_process_places_runs_under_the_same_root_start(postgres_sessionmaker):
    conversation_id, _ = await setup_trace(postgres_sessionmaker)
    trace_id = uuid.uuid4()
    first = await register_trace(postgres_sessionmaker, trace_id, conversation_id, settings())
    second = await register_trace(postgres_sessionmaker, trace_id, conversation_id, settings())
    assert (first[0], second[0]) == ("created", "existing")
    assert first[1] == second[1] and first[1].tzinfo is not None


async def make_due(sessionmaker, identifier):
    async with sessionmaker() as session:
        await session.execute(
            update(db.ExternalTrace)
            .where(db.ExternalTrace.id == identifier)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()


async def test_content_is_exported_until_the_tombstone_then_nothing_and_metrics_stay_anonymous(
    postgres_sessionmaker, monkeypatch, recorded_metrics
):
    cid, _ = await setup_trace(postgres_sessionmaker)
    client = Mock()
    monkeypatch.setattr("interview_agent.observability.Client", lambda **kwargs: client)
    telemetry = Telemetry(
        postgres_sessionmaker,
        cid,
        settings(),
        inputs={"resume_markdown": "CANARY_CV", "job_offer": "CANARY_OFFER"},
    )
    async with telemetry.span("planner"):
        await telemetry.export_span(
            uuid.uuid4(),
            "interviewer",
            datetime.now(UTC),
            datetime.now(UTC),
            {"transcript": "CANARY_METADATA"},
            inputs={"messages": [{"role": "user", "content": "CANARY_ANSWER"}]},
        )
    await telemetry.record("planner", "duration_seconds", 1)
    await telemetry.drain()
    # Interview root, this process, the planner node and the finished leaf.
    assert client.create_run.call_count == 4
    runs = {c.kwargs["name"]: c.kwargs for c in client.create_run.call_args_list}
    # The key is the consent: the interview's content travels in run inputs...
    assert runs["interview"]["inputs"]["resume_markdown"] == "CANARY_CV"
    assert runs["process"]["inputs"]["job_offer"] == "CANARY_OFFER"
    assert "CANARY_ANSWER" in str(runs["interviewer"]["inputs"])
    # ...while metadata (filters) keeps only our own categories.
    assert "CANARY_METADATA" not in str(client.mock_calls)
    # Metrics never link back to the interview, so its deletion leaves them be.
    (sample,) = recorded_metrics("interview_agent.planner.duration_seconds")
    assert "trace_id" not in sample.attributes
    async with postgres_sessionmaker() as session:
        assert await delete_conversation(session, cid)
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

    async def absent(self, tid, pid, since):
        self.since = since
        return self.missing


async def test_submission_ack_is_pending_until_external_query_verifies_removal(
    postgres_sessionmaker, recorded_metrics
):
    completed = "interview_agent.privacy.external_deletions_completed"
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
    assert recorded_metrics(completed) == []  # accepted is not deleted
    api.missing = True
    await make_due(postgres_sessionmaker, tid)
    await worker.once()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "completed" and row.deleted_at is not None
        assert row.conversation_id is None and row.project_name == ""
    (point,) = recorded_metrics(completed)
    assert point.value == 1 and dict(point.attributes) == {}
    # Re-registering after a process restart cannot resurrect the same trace.
    await register_trace(postgres_sessionmaker, tid, cid, settings())
    assert not await guarded_export(postgres_sessionmaker, tid, lambda: pytest.fail("reexport"))
    assert len(api.submitted) == 1


async def test_submission_retries_reserved_durably_and_failure_messages_redacted(
    postgres_sessionmaker, recorded_metrics
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
    # Transient errors are retried, not counted; giving up counts once.
    (point,) = recorded_metrics("interview_agent.privacy.external_deletions_failed")
    assert point.value == 1 and dict(point.attributes) == {"error_type": "submission_limit"}


@pytest.mark.parametrize("cause", ["verification_deadline", "failures"])
async def test_a_deletion_given_up_is_counted_once_with_its_reason(
    postgres_sessionmaker, recorded_metrics, cause
):
    cid, tid = await setup_trace(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await retire_traces(session, conversation_id=cid)
        await session.commit()
    api = FakeAPI()
    worker = ExternalDeletionWorker(postgres_sessionmaker, settings(), api=api)
    assert await worker.once()  # submitted, now verifying
    if cause == "verification_deadline":
        async with postgres_sessionmaker() as session:
            await session.execute(
                update(db.ExternalTrace)
                .where(db.ExternalTrace.id == tid)
                .values(deletion_requested_at=datetime.now(UTC) - timedelta(days=15))
            )
            await session.commit()
        rounds, reason = 1, "verification_deadline"
    else:

        async def unreachable(tid, pid, since):
            raise ConnectionError("CANARY_SECRET")

        api.absent = unreachable
        rounds, reason = MAX_FAILURES, "ConnectionError"
    for _ in range(rounds):
        await make_due(postgres_sessionmaker, tid)
        await worker.once()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "failed" and row.last_error == reason
    assert not await worker.once()
    (point,) = recorded_metrics("interview_agent.privacy.external_deletions_failed")
    assert point.value == 1 and dict(point.attributes) == {"error_type": reason}
    assert recorded_metrics("interview_agent.privacy.external_deletions_completed") == []


async def expire(sessionmaker, trace_id):
    async with sessionmaker() as session:
        await session.execute(
            update(db.ExternalTrace)
            .where(db.ExternalTrace.id == trace_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()


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


async def test_detail_expiry_retires_expired_traces_and_old_manifests_only(postgres_sessionmaker):
    cid, tid = await setup_trace(postgres_sessionmaker)
    current_cid, current_tid = await setup_trace(postgres_sessionmaker)
    await expire(postgres_sessionmaker, tid)
    await manifest(postgres_sessionmaker, days_old=31)
    recent = await manifest(postgres_sessionmaker, days_old=29)
    async with postgres_sessionmaker() as session:
        await expire_detail(session, 30)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.ExternalTrace, tid)
        assert row.state == "pending" and row.conversation_id is None
        # Only the trace leaves: the interview itself follows RETENTION_DAYS.
        assert await session.get(db.Conversation, cid)
        current = await session.get(db.ExternalTrace, current_tid)
        assert current.state == "active" and current.conversation_id == current_cid
        manifests = set(await session.scalars(select(db.ProcessManifest.id)))
        assert manifests == {recent}


async def test_purge_loop_expires_detail_before_the_retention_purge(
    postgres_sessionmaker, monkeypatch
):
    from interview_agent.server import app as server_app

    cid, tid = await setup_trace(postgres_sessionmaker)
    retained_cid, retained_tid = await setup_trace(postgres_sessionmaker)
    await expire(postgres_sessionmaker, tid)
    await manifest(postgres_sessionmaker, days_old=31)
    recent = await manifest(postgres_sessionmaker, days_old=29)
    async with postgres_sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == retained_cid)
            .values(created_at=datetime.now(UTC) - timedelta(days=8))
        )
        await session.commit()
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
        # Expired LangSmith detail is retired; the interview itself is kept.
        expired = await session.get(db.ExternalTrace, tid)
        assert expired.state == "pending" and expired.conversation_id is None
        assert await session.get(db.Conversation, cid)
        assert set(await session.scalars(select(db.ProcessManifest.id))) == {recent}
        # RETENTION_DAYS still deletes old interviews and tombstones their traces.
        assert await session.get(db.Conversation, retained_cid) is None
        retained = await session.get(db.ExternalTrace, retained_tid)
        assert retained.state == "pending" and retained.conversation_id is None


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


@pytest.mark.parametrize(
    "endpoint", ["https://api.smith.langchain.com", "https://ls.example.com/api/v1/"]
)
async def test_real_http_contract_targets_exact_trace_and_checks_all_children(endpoint):
    pid, tid = uuid.uuid4(), uuid.uuid4()
    registered = datetime(2026, 9, 1, tzinfo=UTC)
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(200, json=[{"id": str(pid), "name": "interview-agent"}])
        if request.url.path == "/api/v1/runs/delete":
            assert json.loads(request.content) == {"trace_ids": [str(tid)], "session_id": str(pid)}
            return httpx.Response(200, json={})
        # v1 /runs/query is deprecated; v2 needs an explicit window, or it only
        # searches the last 24 hours and an old trace would look deleted.
        assert request.url.path == "/api/v2/runs/query"
        body = json.loads(request.content)
        assert body["trace_id"] == str(tid) and body["project_ids"] == [str(pid)]
        assert datetime.fromisoformat(body["min_start_time"]) < registered
        assert body["selects"] == ["ID"]  # no content downloaded
        return httpx.Response(200, json={"items": [] if len(requests) > 3 else [{"id": "x"}]})

    api = LangSmithDeletionAPI(
        settings(LANGSMITH_ENDPOINT=endpoint), transport=httpx.MockTransport(respond)
    )
    try:
        assert await api.project_id("interview-agent") == pid
        await api.submit(tid, pid)
        assert not await api.absent(tid, pid, registered)  # a child still exists
        assert await api.absent(tid, pid, registered)
    finally:
        await api.close()
    assert len(requests) == 4


async def test_verification_rejects_an_unknown_response_shape():
    def respond(request):
        return httpx.Response(200, json={"unexpected": []})

    api = LangSmithDeletionAPI(settings(), transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ValueError, match="invalid_verification_response"):
            await api.absent(uuid.uuid4(), uuid.uuid4(), datetime.now(UTC))
    finally:
        await api.close()


async def test_verification_window_starts_from_the_trace_registration(postgres_sessionmaker):
    cid, tid = await setup_trace(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await retire_traces(session, conversation_id=cid)
        await session.commit()
    api = FakeAPI()
    worker = ExternalDeletionWorker(postgres_sessionmaker, settings(), api=api)
    await worker.once()
    await make_due(postgres_sessionmaker, tid)
    await worker.once()
    async with postgres_sessionmaker() as session:
        assert api.since == (await session.get(db.ExternalTrace, tid)).created_at


async def test_every_process_of_an_interview_shares_one_trace(postgres_sessionmaker, monkeypatch):
    cid, _ = await setup_trace(postgres_sessionmaker)
    clients = []

    def make_client(**kwargs):
        clients.append(Mock())
        return clients[-1]

    monkeypatch.setattr("interview_agent.observability.Client", make_client)
    planner = Telemetry(postgres_sessionmaker, cid, settings(), process="planner")
    await planner.drain()
    worker = Telemetry(postgres_sessionmaker, cid, settings(), process="worker")
    await worker.drain()
    assert planner.trace_id == worker.trace_id
    first, second = (
        [call.kwargs for call in client.create_run.call_args_list] for client in clients
    )
    # Only the first process creates the interview root; both hang under it.
    assert [c["name"] for c in first] == ["interview", "planner"]
    assert [c["name"] for c in second] == ["worker"]
    assert first[1]["parent_run_id"] == second[0]["parent_run_id"] == planner.trace_id
    async with postgres_sessionmaker() as session:
        rows = list(
            await session.scalars(
                select(db.ExternalTrace).where(db.ExternalTrace.conversation_id == cid)
            )
        )
        assert planner.trace_id in {row.id for row in rows}


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

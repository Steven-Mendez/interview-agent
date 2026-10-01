"""Real PostgreSQL proof for telemetry preservation, missing data and concurrency."""

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.metrics import metrics_report, purge_metrics, record_metric
from interview_agent.observability import LLMObserver, Telemetry


async def conversation(sessionmaker):
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                job_offer="Private offer",
                resume_markdown="Private CV",
                status="completed",
            )
        )
        await session.commit()
    return conversation_id


def event(conversation_id, value, **kwargs):
    return db.MetricEvent(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        component="graph",
        name="decision_seconds",
        value=value,
        dimensions={
            "graph_version": "v2",
            "language": "es",
            "model": "gpt-6-astra",
            "trace_id": str(uuid.uuid4()),
            "candidate_name": "Private name",
        },
        **kwargs,
    )


async def test_atomic_concurrent_rollup_survives_interview_deletion(postgres_sessionmaker):
    conversation_id = await conversation(postgres_sessionmaker)

    async def write(value):
        async with postgres_sessionmaker() as session:
            await record_metric(session, event(conversation_id, value))

    await asyncio.gather(*(write(value) for value in range(1, 21)))
    async with postgres_sessionmaker() as session:
        aggregate = (await session.scalars(select(db.MetricAggregate))).one()
        assert aggregate.count == 20
        assert aggregate.total == 210
        assert sum(aggregate.histogram.values()) == 20
        assert "trace_id" not in aggregate.dimensions
        assert "candidate_name" not in aggregate.dimensions
        await session.execute(delete(db.Conversation).where(db.Conversation.id == conversation_id))
        await session.commit()
        assert (await session.scalars(select(db.MetricEvent))).all() == []
        report = await metrics_report(session, 30, {"model": "gpt-6-astra"})
        item = report["items"][0]
        assert item["count"] == 20
        assert item["p50"] == pytest.approx(10, rel=0.025)
        assert item["p95"] == pytest.approx(19, rel=0.025)


async def test_unknown_samples_are_not_zero_and_duplicate_event_rolls_back(postgres_sessionmaker):
    conversation_id = await conversation(postgres_sessionmaker)
    identifier = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        sample = event(conversation_id, None)
        sample.id = identifier
        await record_metric(session, sample)
    async with postgres_sessionmaker() as session:
        duplicate = event(conversation_id, 99)
        duplicate.id = identifier
        with pytest.raises(IntegrityError):
            await record_metric(session, duplicate)
        await session.rollback()
        item = (await metrics_report(session, 30, {}))["items"][0]
        assert item["count"] == 0
        assert item["unknown_count"] == 1
        assert item["mean"] is None and item["p95"] is None
        assert item["minimum"] is None and item["maximum"] is None


async def test_detail_and_aggregate_have_separate_retention(postgres_sessionmaker):
    conversation_id = await conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await record_metric(
            session, event(conversation_id, 2, created_at=datetime.now(UTC) - timedelta(days=40))
        )
        await record_metric(
            session, event(conversation_id, 3, created_at=datetime.now(UTC) - timedelta(days=400))
        )
        await purge_metrics(session, 30, 365)
        assert (await session.scalars(select(db.MetricEvent))).all() == []
        items = (await metrics_report(session, 365, {}))["items"]
        assert len(items) == 1 and items[0]["total"] == 2


async def test_slow_langsmith_export_is_not_on_llm_response_path(monkeypatch):
    async def register(*args):
        return True

    async def export(sessionmaker, trace_id, operation):
        await asyncio.to_thread(operation)

    monkeypatch.setattr("interview_agent.observability.register_trace", register)
    monkeypatch.setattr("interview_agent.observability.guarded_export", export)
    gate = threading.Event()
    client = Mock()
    client.create_run.side_effect = lambda **kwargs: gate.wait(0.2)
    monkeypatch.setattr("interview_agent.observability.Client", lambda **kwargs: client)
    telemetry = Telemetry(None, uuid.uuid4(), Settings(_env_file=None, LANGSMITH_API_KEY="test"))
    started = time.monotonic()
    await telemetry.export_span(
        uuid.uuid4(), "interviewer", datetime.now(UTC), datetime.now(UTC), {}
    )
    assert time.monotonic() - started < 0.05
    gate.set()
    await telemetry.drain()
    assert client.create_run.call_count == 2
    assert client.update_run.call_count == 2
    for call in client.create_run.call_args_list:
        assert call.kwargs["inputs"] == {"redacted": True}
        assert "Private" not in str(call.kwargs)


async def test_http_retry_measurement_counts_sdk_attempts_without_reading_bodies():
    telemetry = Mock()
    observer = LLMObserver(telemetry, "planner", "gpt-6-astra", "high")
    for retry, status in [(0, 503), (1, 200)]:
        request = httpx.Request(
            "POST",
            "https://api.openai.com/v1/responses",
            headers={"x-stainless-retry-count": str(retry)},
            content=b"Private prompt",
        )
        await observer.http_request(request)
        await observer.http_response(httpx.Response(status, request=request))
    calls = telemetry.emit.call_args_list
    assert sum(call.args[1] == "http_requests" for call in calls) == 2
    assert sum(call.args[1] == "http_retries" for call in calls) == 1
    assert sum(call.args[1] == "http_errors" for call in calls) == 1
    assert "Private prompt" not in str(calls)


async def test_langsmith_orders_root_and_nested_node_before_child(monkeypatch):
    async def register(*args):
        return True

    async def export(sessionmaker, trace_id, operation):
        await asyncio.to_thread(operation)

    monkeypatch.setattr("interview_agent.observability.register_trace", register)
    monkeypatch.setattr("interview_agent.observability.guarded_export", export)
    order = []
    client = Mock()
    created = set()

    def create(**kwargs):
        time.sleep(0.01)
        parent = kwargs.get("parent_run_id")
        assert parent is None or parent in created
        created.add(kwargs["id"])
        order.append(("create", kwargs["id"]))

    def update(identifier, **kwargs):
        assert identifier in created
        order.append(("update", identifier))

    client.create_run.side_effect = create
    client.update_run.side_effect = update
    monkeypatch.setattr("interview_agent.observability.Client", lambda **kwargs: client)
    telemetry = Telemetry(None, uuid.uuid4(), Settings(_env_file=None, LANGSMITH_API_KEY="test"))
    async with telemetry.span("graph.decide") as node_id:
        await telemetry.export_span(
            uuid.uuid4(), "interviewer", datetime.now(UTC), datetime.now(UTC), {}, parent_id=node_id
        )
    await telemetry.drain()
    assert len(created) == 3
    assert order[0] == ("create", telemetry.trace_id)
    assert order[-1] == ("update", telemetry.trace_id)


async def test_recovered_sdk_connect_error_is_recorded_per_attempt():
    from openai import AsyncOpenAI

    from interview_agent.llm import ObservedTransport

    telemetry = Mock()
    observer = LLMObserver(telemetry, "planner", "gpt-6-astra", "high")
    attempts = 0

    async def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("synthetic connection failure", request=request)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-6-astra",
                "output": [],
            },
        )

    client = httpx.AsyncClient(
        transport=ObservedTransport(observer, httpx.MockTransport(respond)),
        event_hooks={"request": [observer.http_request], "response": [observer.http_response]},
    )
    sdk = AsyncOpenAI(api_key="test", http_client=client, max_retries=1)
    try:
        await sdk.responses.create(model="gpt-6-astra", input="Synthetic test prompt")
    finally:
        await sdk.close()
    names = [call.args[1] for call in telemetry.emit.call_args_list]
    assert names.count("http_requests") == 2
    assert names.count("http_retries") == 1
    assert names.count("http_transport_errors") == 1
    assert names.count("http_failed_attempt_seconds") == 1


async def test_provider_identity_stays_unknown_when_response_does_not_report_it():
    from unittest.mock import AsyncMock

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    telemetry = Mock()
    telemetry.export_span = AsyncMock()
    observer = LLMObserver(telemetry, "planner", "requested-alias", "high")
    await observer.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            usage_metadata={
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "total_tokens": 2,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id=uuid.uuid4(),
    )
    assert all(
        "resolved_model" not in call.kwargs.get("dimensions", {})
        for call in telemetry.emit.call_args_list
    )

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
        return "created", datetime.now(UTC)

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
    # Root, process span and the finished leaf (one request); closing patches
    # only the process span: the evaluator alone ends the root.
    assert client.create_run.call_count == 3
    assert client.update_run.call_count == 1


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
        return "created", datetime.now(UTC)

    async def export(sessionmaker, trace_id, operation):
        await asyncio.to_thread(operation)

    monkeypatch.setattr("interview_agent.observability.register_trace", register)
    monkeypatch.setattr("interview_agent.observability.guarded_export", export)
    order = []
    client = Mock()
    created = {}

    def create(**kwargs):
        time.sleep(0.01)
        parent = kwargs.get("parent_run_id")
        assert parent is None or parent in created
        created[kwargs["id"]] = kwargs
        order.append(("create", kwargs["id"]))

    def update(identifier, **kwargs):
        assert identifier in created
        order.append(("update", identifier))

    client.create_run.side_effect = create
    client.update_run.side_effect = update
    monkeypatch.setattr("interview_agent.observability.Client", lambda **kwargs: client)
    telemetry = Telemetry(
        None,
        uuid.uuid4(),
        Settings(_env_file=None, LANGSMITH_API_KEY="test"),
        process="evaluator",
        closes_trace=True,
    )
    async with telemetry.span("graph.decide") as node_id:
        await telemetry.export_span(
            uuid.uuid4(), "interviewer", datetime.now(UTC), datetime.now(UTC), {}, parent=node_id
        )
    await telemetry.drain()
    assert len(created) == 4
    assert order[0] == ("create", telemetry.trace_id)
    assert order[1] == ("create", telemetry.process_id)
    assert order[-1] == ("update", telemetry.trace_id)
    # LangSmith rejects a run with trace_id but no dotted_order. The root starts
    # at registration, after this process began, so children are clamped to it.
    for run_id, run in created.items():
        assert run["trace_id"] == telemetry.trace_id
        assert run["dotted_order"].endswith(f"{run['start_time']:%Y%m%dT%H%M%S%fZ}{run_id}")
        parent = run.get("parent_run_id")
        if parent is None:
            assert "." not in run["dotted_order"]
        else:
            assert run["dotted_order"].startswith(created[parent]["dotted_order"] + ".")
            assert run["start_time"] >= created[parent]["start_time"]


def test_environment_tracing_cannot_start_unlinked_traces(monkeypatch):
    # Automatic LangChain tracing would duplicate every call in separate traces
    # that the interview's deletion never reaches; our explicit export covers it.
    from langsmith.utils import tracing_is_enabled

    import interview_agent.observability  # noqa: F401  (installs the global guard)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    assert tracing_is_enabled() is False


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


def exporting_telemetry(monkeypatch, **kwargs):
    async def register(*args):
        return "created", datetime.now(UTC)

    async def export(sessionmaker, trace_id, operation):
        await asyncio.to_thread(operation)

    monkeypatch.setattr("interview_agent.observability.register_trace", register)
    monkeypatch.setattr("interview_agent.observability.guarded_export", export)
    client = Mock()
    monkeypatch.setattr("interview_agent.observability.Client", lambda **_: client)
    settings = Settings(_env_file=None, LANGSMITH_API_KEY="test")
    return Telemetry(None, uuid.uuid4(), settings, **kwargs), client


async def test_llm_span_carries_native_tokens_cost_prompt_and_answer(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    telemetry, client = exporting_telemetry(monkeypatch)
    telemetry.emit = Mock()
    observer = LLMObserver(telemetry, "interviewer", "gpt-6-astra", "low")
    run_id = uuid.uuid4()
    await observer.on_chat_model_start(
        {}, [[SystemMessage("Interview rules"), HumanMessage("Candidate said hi")]], run_id=run_id
    )
    message = AIMessage(
        content="Spoken answer",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 400},
            "output_token_details": {"reasoning": 60},
        },
        response_metadata={"model_name": "gpt-6-astra-2026-09-01"},
    )
    await observer.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id
    )
    await telemetry.drain()
    span = next(c.kwargs for c in client.create_run.call_args_list if c.kwargs["id"] == run_id)
    metadata = span["extra"]["metadata"]
    assert metadata["ls_provider"] == "openai"
    assert metadata["ls_model_name"] == "gpt-6-astra-2026-09-01"
    usage = metadata["usage_metadata"]
    assert usage["input_tokens"] == 1000 and usage["output_tokens"] == 100
    assert usage["input_token_details"] == {"cache_read": 400}
    assert usage["output_token_details"] == {"reasoning": 60}
    assert usage["total_cost"] == pytest.approx((600 * 10 + 400 * 1 + 100 * 50) / 1_000_000)
    assert span["inputs"]["messages"] == [
        {"role": "system", "content": "Interview rules"},
        {"role": "user", "content": "Candidate said hi"},
    ]
    assert span["outputs"]["messages"][0]["content"] == "Spoken answer"


async def test_feedback_never_extends_retention_and_ignores_unknown_keys(monkeypatch):
    telemetry, client = exporting_telemetry(monkeypatch)
    telemetry.feedback("evaluation_score", 88)
    telemetry.feedback("hired", True)
    telemetry.feedback("free_text", 1)
    telemetry.feedback("evaluation_coverage", None)
    await telemetry.drain()
    calls = [c.kwargs for c in client.create_feedback.call_args_list]
    assert [(c["key"], c["score"]) for c in calls] == [("evaluation_score", 88.0), ("hired", 1.0)]
    assert all(c["trace_id"] == telemetry.trace_id for c in calls)
    assert all(c["extend_trace_retention"] is False for c in calls)
    assert all(c["stop_after_attempt"] == 1 for c in calls)


async def test_metadata_keeps_only_whitelisted_facts_and_content_goes_in_outputs(monkeypatch):
    from interview_agent.interview.dialogue import _node_facts

    root = uuid.uuid4()
    telemetry, client = exporting_telemetry(monkeypatch, process="worker", thread_of=root)
    result = {
        "decision": {
            "action": "close",
            "close_reason": "Reason the model invented",
            "spoken_text": "Spoken question",
        },
        "error": "Validation detail",
        "attempts": 2,
    }
    facts = _node_facts({"attempts": 1}, result)
    assert facts == {"action": "close", "validation_failed": True, "attempts": 2}
    async with telemetry.span("graph.decide", {"turn_id": "t1"}) as span_id:
        telemetry.annotate_span(span_id, **facts, transcript="Metadata text")
        telemetry.span_outputs(span_id, result)
    telemetry.annotate(ended_reason="question_limit", quote="Metadata quote")
    telemetry.set_outputs({"transcript": [{"role": "user", "content": "Candidate words"}]})
    await telemetry.drain()
    updates = {c.args[0]: c.kwargs for c in client.update_run.call_args_list}
    creates = {c.kwargs["id"]: c.kwargs for c in client.create_run.call_args_list}
    assert creates[span_id]["inputs"] == {"turn_id": "t1"}
    assert updates[span_id]["outputs"]["decision"]["spoken_text"] == "Spoken question"
    assert updates[span_id]["extra"]["metadata"]["action"] == "close"
    process = updates[telemetry.process_id]
    assert process["extra"]["metadata"]["ended_reason"] == "question_limit"
    assert process["extra"]["metadata"]["process"] == "worker"
    assert process["outputs"]["transcript"][0]["content"] == "Candidate words"
    root_run = next(
        c.kwargs for c in client.create_run.call_args_list if c.kwargs["name"] == "interview"
    )
    # Repeats of one interview share an opaque thread, never the raw ID.
    assert root_run["extra"]["metadata"]["thread_id"] == telemetry.thread_id
    assert str(root) not in str(client.mock_calls)
    assert "Metadata" not in str(client.mock_calls)


async def test_api_feedback_is_sent_only_for_a_registered_trace(postgres_sessionmaker, monkeypatch):
    from interview_agent.observability import (
        _feedback_tasks,
        interview_trace_id,
        send_trace_feedback,
    )
    from interview_agent.privacy import register_trace

    client = Mock()
    monkeypatch.setattr("interview_agent.observability.Client", lambda **_: client)
    settings = Settings(_env_file=None, LANGSMITH_API_KEY="test")
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        session.add(db.Conversation(id=conversation_id, job_offer="Role", resume_markdown="CV"))
        await session.commit()
    send_trace_feedback(
        postgres_sessionmaker, settings, conversation_id, "response_onset_seconds", 2
    )
    await asyncio.gather(*list(_feedback_tasks))
    assert not client.create_feedback.called  # LangSmith never saw this interview
    await register_trace(
        postgres_sessionmaker, interview_trace_id(conversation_id), conversation_id, settings
    )
    send_trace_feedback(
        postgres_sessionmaker, settings, conversation_id, "response_onset_seconds", 2.5
    )
    await asyncio.gather(*list(_feedback_tasks))
    assert client.create_feedback.call_args.kwargs["score"] == 2.5
    assert client.create_feedback.call_args.kwargs["extend_trace_retention"] is False


async def test_feedback_names_its_project_once_and_survives_one_not_indexed_yet(monkeypatch):
    from langsmith.utils import LangSmithNotFoundError

    from interview_agent import observability

    monkeypatch.setattr(observability, "_project_ids", {})
    telemetry, client = exporting_telemetry(monkeypatch)
    telemetry.emit = Mock()
    client.read_project.side_effect = LangSmithNotFoundError("first trace still ingesting")
    telemetry.feedback("evaluation_score", 80)
    await telemetry.drain()
    assert "session_id" not in client.create_feedback.call_args.kwargs  # sent, not lost
    project = uuid.uuid4()
    client.read_project.side_effect = None
    client.read_project.return_value = Mock(id=project)
    telemetry.feedback("evaluation_score", 85)
    telemetry.feedback("evaluation_score", 90)
    await telemetry.drain()
    feedback = client.create_feedback.call_args.kwargs
    assert feedback["session_id"] == project
    assert feedback["start_time"] == telemetry._placement[telemetry.trace_id][1]
    assert client.read_project.call_count == 2  # cached after the first success


async def test_only_the_closing_stage_ends_the_root_and_a_repeat_is_not_an_error(monkeypatch):
    from langsmith.utils import LangSmithConflictError

    # LangSmith accepts one update per run: planner and worker never touch the
    # root, and a re-evaluation's duplicate update is expected, not a failure.
    worker, client = exporting_telemetry(monkeypatch, process="worker")
    worker.emit = Mock()
    await worker.drain()
    assert [call.args[0] for call in client.update_run.call_args_list] == [worker.process_id]
    evaluator, client = exporting_telemetry(monkeypatch, process="evaluator", closes_trace=True)
    evaluator.emit = Mock()
    client.update_run.side_effect = lambda run_id, **_: (
        (_ for _ in ()).throw(LangSmithConflictError("already ended"))
        if run_id == evaluator.trace_id
        else None
    )
    await evaluator.drain()
    assert [call.args[0] for call in client.update_run.call_args_list] == [
        evaluator.process_id,
        evaluator.trace_id,
    ]
    assert evaluator.failures == 0

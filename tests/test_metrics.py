"""Anonymous OTLP metrics, plus the LangSmith export and LLM measurements around them."""

import asyncio
import gzip
import math
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import httpx
import pytest
from livekit.agents.metrics.base import EOUMetrics, Metadata
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import AggregationTemporality
from opentelemetry.sdk.metrics.export import ExponentialHistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from interview_agent import otel_metrics
from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.observability import LLMObserver, Telemetry
from interview_agent.voice_metrics import record_voice_metrics

IDENTITY = {
    "trace_id": str(uuid.uuid4()),
    "conversation_id": str(uuid.uuid4()),
    "turn_id": "turn-1",
    "candidate_name": "Private name",
}


async def test_each_measurement_is_one_exponential_histogram_without_identity(recorded_metrics):
    for value in range(1, 21):
        otel_metrics.record(
            "graph",
            "decision_seconds",
            value,
            # A new trace per sample would split the series if it were kept.
            {"graph_version": "v2", "language": "es", "model": "gpt-6-astra", **IDENTITY}
            | {"trace_id": str(uuid.uuid4())},
        )
    otel_metrics.record("graph", "repair_seconds", 3.0)
    (point,) = recorded_metrics("interview_agent.graph.decision_seconds")
    assert isinstance(point, ExponentialHistogramDataPoint)
    assert (point.count, point.sum, point.min, point.max) == (20, 210, 1, 20)
    assert dict(point.attributes) == {
        "graph_version": "v2",
        "language": "es",
        "model": "gpt-6-astra",
    }
    (other,) = recorded_metrics("interview_agent.graph.repair_seconds")
    assert other.count == 1 and dict(other.attributes) == {}


async def test_missing_values_count_as_unknown_never_as_zero(recorded_metrics):
    for value in (None, math.nan, math.inf, -math.inf):
        otel_metrics.record("planner", "estimated_cost_usd", value, {"model": "gpt-6-astra"})
    otel_metrics.record("planner", "estimated_cost_usd", 0.5, {"model": "gpt-6-astra"})
    (unknown,) = recorded_metrics("interview_agent.planner.estimated_cost_usd.unknown")
    assert unknown.value == 4 and dict(unknown.attributes) == {"model": "gpt-6-astra"}
    (known,) = recorded_metrics("interview_agent.planner.estimated_cost_usd")
    assert (known.count, known.sum) == (1, 0.5)


async def test_samples_never_carry_the_current_span_as_an_exemplar(recorded_metrics):
    # The SDK default would attach the sampled span's trace and span IDs.
    with TracerProvider().get_tracer("test").start_as_current_span("voice turn"):
        otel_metrics.record("voice", "ttfb_seconds", 0.3)
        otel_metrics.count("privacy", "external_deletions_completed")
    (sample,) = recorded_metrics("interview_agent.voice.ttfb_seconds")
    (deletion,) = recorded_metrics("interview_agent.privacy.external_deletions_completed")
    assert not sample.exemplars and not deletion.exemplars


def test_without_an_endpoint_nothing_is_built_and_recording_is_a_no_op(monkeypatch):
    reader = InMemoryMetricReader()
    assert otel_metrics.configure(Settings(_env_file=None), "interview-agent-test", reader=reader)
    monkeypatch.setattr(otel_metrics, "MeterProvider", Mock(side_effect=AssertionError))
    monkeypatch.setattr(otel_metrics, "OTLPMetricExporter", Mock(side_effect=AssertionError))
    # Configuring again replaces the earlier provider, here with none.
    assert not otel_metrics.configure(Settings(_env_file=None), "interview-agent-test")
    otel_metrics.record("graph", "decision_seconds", 1.0)
    otel_metrics.record("graph", "decision_seconds", None)
    otel_metrics.count("privacy", "external_deletions_completed")
    assert otel_metrics.force_flush() is True
    otel_metrics.shutdown()
    data = reader.get_metrics_data()
    assert not data or not any(r.scope_metrics for r in data.resource_metrics)


def test_resource_names_the_service_and_each_configured_instance():
    instances = []
    try:
        for _ in range(2):
            reader = InMemoryMetricReader()
            otel_metrics.configure(
                Settings(_env_file=None), "interview-agent-worker", reader=reader
            )
            otel_metrics.record("voice", "interruptions", 1)
            (resource,) = reader.get_metrics_data().resource_metrics
            assert resource.resource.attributes["service.name"] == "interview-agent-worker"
            instances.append(uuid.UUID(resource.resource.attributes["service.instance.id"]))
    finally:
        otel_metrics.shutdown()
    assert instances[0] != instances[1]


def test_otlp_export_reaches_the_configured_endpoint_with_its_headers():
    requests = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            headers = {key.lower(): value for key, value in self.headers.items()}
            requests.append((self.path, headers, body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(
        _env_file=None,
        OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{server.server_port}/",
        # The OTel env format: URL-encoded values, comma-separated pairs.
        OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20c3ludGhldGlj,X-Scope-OrgID=local",
    )
    try:
        assert otel_metrics.configure(settings, "interview-agent-api")
        otel_metrics.record("browser", "response_onset_seconds", 1.25, {"language": "es"})
        otel_metrics.count(
            "privacy", "external_deletions_failed", dimensions={"error_type": "submission_limit"}
        )
        assert otel_metrics.force_flush()
    finally:
        otel_metrics.shutdown()
        server.shutdown()
        server.server_close()
    path, headers, body = requests[0]
    assert path == "/v1/metrics"
    assert headers["authorization"] == "Basic c3ludGhldGlj"
    assert headers["x-scope-orgid"] == "local"
    (resource,) = ExportMetricsServiceRequest.FromString(body).resource_metrics
    service = {a.key: a.value.string_value for a in resource.resource.attributes}
    assert service["service.name"] == "interview-agent-api"
    metrics = {m.name: m for scope in resource.scope_metrics for m in scope.metrics}
    onset = metrics["interview_agent.browser.response_onset_seconds"]
    # Cumulative, native-histogram ready and without a unit suffix for Prometheus.
    assert onset.WhichOneof("data") == "exponential_histogram" and onset.unit == ""
    cumulative = AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
    assert onset.exponential_histogram.aggregation_temporality == cumulative
    (point,) = onset.exponential_histogram.data_points
    assert point.count == 1 and point.sum == 1.25
    assert {a.key: a.value.string_value for a in point.attributes} == {"language": "es"}
    failed = metrics["interview_agent.privacy.external_deletions_failed"]
    assert failed.sum.is_monotonic and failed.sum.aggregation_temporality == cumulative
    (point,) = failed.sum.data_points
    assert point.as_int == 1
    assert {a.key: a.value.string_value for a in point.attributes} == {
        "error_type": "submission_limit"
    }


_SILENT_COLLECTOR_PROCESS = """
import sys
from interview_agent import otel_metrics
from interview_agent.config import Settings

settings = Settings(_env_file=None, OTEL_EXPORTER_OTLP_ENDPOINT=sys.argv[1])
otel_metrics.configure(settings, "interview-agent-test")
otel_metrics.record("server", "sweep_duration_seconds", 0.5)
print(otel_metrics.force_flush(500), flush=True)
otel_metrics.shutdown(500)
print("shut down", flush=True)
"""


def test_an_unresponsive_collector_cannot_hold_the_process_exit():
    # Accepts connections and never answers, as a stuck collector would.
    collector = socket.create_server(("127.0.0.1", 0))
    accepted = []

    def accept():
        while True:
            try:
                accepted.append(collector.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    endpoint = f"http://127.0.0.1:{collector.getsockname()[1]}"
    process = subprocess.Popen(
        [sys.executable, "-c", _SILENT_COLLECTOR_PROCESS, endpoint],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "False"
        flushed_at = time.monotonic()
        assert process.stdout.readline().strip() == "shut down"
        assert process.wait(timeout=30) == 0
        # Without the bound, the abandoned export and an atexit export each
        # waited for the exporter's own timeout.
        assert time.monotonic() - flushed_at < 2
    finally:
        process.kill()
        collector.close()
        for connection in accepted:
            connection.close()
    assert accepted


async def test_telemetry_waits_for_deferred_dimensions_and_adds_the_configured_model(
    recorded_metrics,
):
    telemetry = Telemetry(
        None,
        uuid.uuid4(),
        Settings(_env_file=None),
        {
            "graph_version": "interview-v1",
            "config_version": "synthetic-config",
            "models": {
                "planner": {"model": "gpt-6-astra"},
                "interviewer": {"model": "gpt-6.1-sol"},
            },
        },
        defer_dimensions=True,
    )
    telemetry.emit("planner", "duration_seconds", 1.5, dimensions=IDENTITY)
    telemetry.emit("dialogue", "validation_failures", 1, turn_id="turn-1")
    telemetry.emit("stt", "ttfb_seconds", 0.2, dimensions={"model": "assemblyai/test"})
    await asyncio.sleep(0.05)
    # The planner has not classified the interview yet: nothing is recorded.
    assert recorded_metrics("interview_agent.planner.duration_seconds") == []
    telemetry.resolve_dimensions(
        {"language": "es", "seniority": "junior", "interview_length": "short"}
    )
    await telemetry.drain()
    interview = {
        "graph_version": "interview-v1",
        "config_version": "synthetic-config",
        "language": "es",
        "seniority": "junior",
        "length": "short",
    }
    (planner,) = recorded_metrics("interview_agent.planner.duration_seconds")
    assert dict(planner.attributes) == interview | {"model": "gpt-6-astra"}
    # The dialogue runs on the interviewer's model.
    (dialogue,) = recorded_metrics("interview_agent.dialogue.validation_failures")
    assert dict(dialogue.attributes) == interview | {"model": "gpt-6.1-sol"}
    (stt,) = recorded_metrics("interview_agent.stt.ttfb_seconds")
    assert dict(stt.attributes)["model"] == "assemblyai/test"
    await telemetry.record("evaluator", "coverage", None)
    (unknown,) = recorded_metrics("interview_agent.evaluator.coverage.unknown")
    assert dict(unknown.attributes) == interview


async def test_streaming_stt_latency_is_the_transcript_delay_labelled_with_the_stt_model(
    recorded_metrics,
):
    telemetry = Telemetry(None, uuid.uuid4(), Settings(_env_file=None), {})
    models = {"stt_model": "assemblyai/test", "tts_model": "cartesia/test"}
    record_voice_metrics(
        telemetry,
        EOUMetrics(
            timestamp=time.time(),
            end_of_utterance_delay=0.6,
            transcription_delay=0.25,
            on_user_turn_completed_delay=0.01,
            speech_id="speech-1",
            # LiveKit describes the turn detector here, not the STT.
            metadata=Metadata(model_name="turn-detector", model_provider="livekit"),
        ),
        **models,
    )
    await telemetry.drain()
    (delay,) = recorded_metrics("interview_agent.eou.transcription_seconds")
    attributes = dict(delay.attributes)
    assert (attributes["source"], attributes["model"]) == ("livekit", "assemblyai/test")
    assert "resolved_model" not in attributes and "provider" not in attributes
    assert delay.sum == 0.25
    (end_of_turn,) = recorded_metrics("interview_agent.eou.end_of_utterance_seconds")
    assert dict(end_of_turn.attributes)["model"] == "assemblyai/test"


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

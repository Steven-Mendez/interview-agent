"""Anonymous OTLP metrics and the LLM measurements around them, plus what may
turn LangSmith tracing on and what its runs may carry."""

import asyncio
import gzip
import inspect
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
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
from interview_agent.interview import evaluator, planner
from interview_agent.observability import LLMObserver, Telemetry, traced_inputs
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


def test_farewell_metrics_preserve_the_evidence_policy_without_identity(recorded_metrics):
    for source in ("agent_playout", "browser_playback"):
        otel_metrics.record(
            "closing",
            "playback_confirmed",
            1,
            {"confirmation_source": source, "farewell_status": "played", **IDENTITY},
        )
    points = recorded_metrics("interview_agent.closing.playback_confirmed")
    assert len(points) == 2
    assert {tuple(sorted(dict(point.attributes).items())) for point in points} == {
        (("confirmation_source", source), ("farewell_status", "played"))
        for source in ("agent_playout", "browser_playback")
    }


async def test_samples_never_carry_the_current_span_as_an_exemplar(recorded_metrics):
    # The SDK default would attach the sampled span's trace and span IDs.
    with TracerProvider().get_tracer("test").start_as_current_span("voice turn"):
        otel_metrics.record("voice", "ttfb_seconds", 0.3)
        otel_metrics.record("voice", "ttfb_seconds", None)
    (sample,) = recorded_metrics("interview_agent.voice.ttfb_seconds")
    (unknown,) = recorded_metrics("interview_agent.voice.ttfb_seconds.unknown")
    assert not sample.exemplars and not unknown.exemplars


def gauge(points) -> dict[tuple, float]:
    """A gauge's values by attribute set, the attributes as sorted (key, value) pairs."""
    return {tuple(sorted(dict(point.attributes).items())): point.value for point in points}


async def test_a_snapshot_gauge_reports_its_latest_values_at_every_collection(recorded_metrics):
    otel_metrics.set_snapshot(
        "accounts",
        "users",
        {
            (("role", "guest"),): 3,
            (("role", "admin"),): 0,
            # Labels outside DIMENSIONS never reach the export.
            (("role", "admin"), ("owner_id", "user-synthetic"), ("email", "a@example.com")): 9,
        },
    )
    expected = {(("role", "guest"),): 3, (("role", "admin"),): 9}
    # Unlike a synchronous gauge, the value is exported again, not once.
    assert gauge(recorded_metrics("interview_agent.accounts.users")) == expected
    assert gauge(recorded_metrics("interview_agent.accounts.users")) == expected
    # A new snapshot replaces the old one whole.
    otel_metrics.set_snapshot("accounts", "users", {(("role", "guest"),): 4})
    assert gauge(recorded_metrics("interview_agent.accounts.users")) == {(("role", "guest"),): 4}
    otel_metrics.set_snapshot("accounts", "guest_interviews_monthly_limit", {(): 2})
    (limit,) = recorded_metrics("interview_agent.accounts.guest_interviews_monthly_limit")
    assert (limit.value, dict(limit.attributes)) == (2, {})


def test_snapshots_outlive_a_reconfiguration_and_cost_nothing_unconfigured():
    # Unconfigured: kept, nothing built.
    otel_metrics.set_snapshot("accounts", "guests_out_of_interviews", {(): 1})
    reader = InMemoryMetricReader()
    try:
        otel_metrics.configure(Settings(_env_file=None), "interview-agent-test", reader=reader)
        for _ in range(2):
            (point,) = [
                point
                for resource in reader.get_metrics_data().resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
                if metric.name == "interview_agent.accounts.guests_out_of_interviews"
                for point in metric.data.data_points
            ]
            assert point.value == 1
    finally:
        otel_metrics.shutdown()
    # Shut down: the next snapshot is kept for the next configuration only.
    otel_metrics.set_snapshot("accounts", "guests_out_of_interviews", {(): 2})
    assert otel_metrics._snapshots["interview_agent.accounts.guests_out_of_interviews"] == (
        ({}, 2),
    )


async def test_account_gauges_count_users_activity_and_quotas(
    postgres_sessionmaker, recorded_metrics, monkeypatch
):
    from datetime import UTC, date, timedelta

    from sqlalchemy import delete, func

    from interview_agent.interview import db
    from interview_agent.server import account_metrics

    settings = Settings(
        _env_file=None,
        ADMIN_USER_IDS="local-dev,user-admin",
        LIFETIME_INTERVIEWS_PER_USER=3,
        GUEST_INTERVIEWS_PER_MONTH=40,
    )
    async with postgres_sessionmaker() as session:
        now = await session.scalar(func.now().select())
        for owner_id, seen_days_ago in (
            ("local-dev", 0),
            ("user-admin", None),
            ("user-today", 0.5),
            ("user-week", 3),
            ("user-month", 20),
            ("user-gone", 45),
            ("user-never", None),
        ):
            session.add(
                db.UserProfile(
                    owner_id=owner_id,
                    auth_provider="local" if owner_id == "local-dev" else "neon",
                    last_seen_at=None
                    if seen_days_ago is None
                    else now - timedelta(days=seen_days_ago),
                )
            )
        session.add_all(
            [
                db.UserInterviewQuota(owner_id="user-today", interviews_used=3),
                db.UserInterviewQuota(owner_id="user-week", interviews_used=2),
                db.UserInterviewQuota(owner_id="user-gone", interviews_used=4),
                # An admin's count never runs out.
                db.UserInterviewQuota(owner_id="user-admin", interviews_used=5),
            ]
        )
        this_month = now.astimezone(UTC).date().replace(day=1)
        session.add_all(
            [
                db.GuestInterviewMonth(month=this_month, interviews_started=7),
                db.GuestInterviewMonth(month=date(2001, 1, 1), interviews_started=30),
            ]
        )
        await session.commit()

    loop_sleeps = []

    async def one_cycle(seconds):
        loop_sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", one_cycle)
    with pytest.raises(asyncio.CancelledError):
        await account_metrics.account_metrics_loop(postgres_sessionmaker, settings)
    assert loop_sleeps == [account_metrics.ACCOUNT_METRICS_INTERVAL_SECONDS]
    expected = {
        "users": {(("role", "admin"),): 2, (("role", "guest"),): 5},
        "active_users": {
            (("window", "1d"),): 2,
            (("window", "7d"),): 3,
            (("window", "30d"),): 4,
        },
        "guests_out_of_interviews": {(): 2},
        "guest_interviews_this_month": {(): 7},
        "guest_interviews_monthly_limit": {(): 40},
    }
    # Reported again at the next collection, without another recount.
    for _ in range(2):
        assert {
            name: gauge(recorded_metrics(f"interview_agent.accounts.{name}")) for name in expected
        } == expected

    # A month nobody started an interview in, and no users at all: zeros,
    # every label set included.
    async with postgres_sessionmaker() as session:
        for model in (db.UserProfile, db.UserInterviewQuota, db.GuestInterviewMonth):
            await session.execute(delete(model))
        await session.commit()
    await account_metrics.publish_account_metrics(postgres_sessionmaker, settings)
    assert gauge(recorded_metrics("interview_agent.accounts.users")) == {
        (("role", "admin"),): 0,
        (("role", "guest"),): 0,
    }
    assert gauge(recorded_metrics("interview_agent.accounts.active_users")) == {
        (("window", window),): 0 for window in ("1d", "7d", "30d")
    }
    assert gauge(recorded_metrics("interview_agent.accounts.guest_interviews_this_month")) == {
        (): 0
    }


async def test_a_failed_recount_keeps_the_loop_and_logs_only_its_template(
    recorded_metrics, monkeypatch, caplog
):
    from interview_agent.log_templates import SAFE_LOG_TEMPLATES
    from interview_agent.server import account_metrics

    async def broken(sessionmaker, settings):
        raise RuntimeError("SENSITIVE_DATABASE_ERROR")

    cycles = []

    async def sleep(seconds):
        cycles.append(seconds)
        if len(cycles) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(account_metrics, "account_snapshot", broken)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with caplog.at_level("ERROR", logger="interview_agent"), pytest.raises(asyncio.CancelledError):
        await account_metrics.account_metrics_loop(None, Settings(_env_file=None))
    template = "account metrics failed; retrying next cycle"
    assert [record.msg for record in caplog.records] == [template, template]
    assert template in SAFE_LOG_TEMPLATES


def test_without_an_endpoint_nothing_is_built_and_recording_is_a_no_op(monkeypatch):
    reader = InMemoryMetricReader()
    assert otel_metrics.configure(Settings(_env_file=None), "interview-agent-test", reader=reader)
    monkeypatch.setattr(otel_metrics, "MeterProvider", Mock(side_effect=AssertionError))
    monkeypatch.setattr(otel_metrics, "OTLPMetricExporter", Mock(side_effect=AssertionError))
    # Configuring again replaces the earlier provider, here with none.
    assert not otel_metrics.configure(Settings(_env_file=None), "interview-agent-test")
    otel_metrics.record("graph", "decision_seconds", 1.0)
    otel_metrics.record("graph", "decision_seconds", None)
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
        otel_metrics.record("evaluator", "coverage", None, {"model": "gpt-6-astra"})
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
    # A missing value is counted, as a cumulative monotonic sum.
    unknown = metrics["interview_agent.evaluator.coverage.unknown"]
    assert unknown.sum.is_monotonic and unknown.sum.aggregation_temporality == cumulative
    (point,) = unknown.sum.data_points
    assert point.as_int == 1
    assert {a.key: a.value.string_value for a in point.attributes} == {"model": "gpt-6-astra"}


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
        uuid.uuid4(),
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
    telemetry = Telemetry(uuid.uuid4(), {})
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


_CONSENT_PROCESS = """
import json
from unittest.mock import Mock

from langchain_core.callbacks import CallbackManager
from langchain_core.tracers.langchain import LangChainTracer
from langsmith.utils import tracing_is_enabled

import interview_agent.config  # noqa: F401  (the consent switch)

enabled = tracing_is_enabled()
print(json.dumps({
    "enabled": enabled,
    # Only asked while off: with tracing on, LangChain would attach a tracer
    # with a real LangSmith client.
    "tracer_attached": None if enabled else any(
        isinstance(handler, LangChainTracer) for handler in CallbackManager.configure().handlers
    ),
    # Where LangChain's tracer would send runs (its mock client sends nothing).
    "tracer_project": LangChainTracer(client=Mock()).project_name,
}))
"""


def consent(tmp_path, key):
    """A fresh process whose environment asks for tracing; its directory has
    no .env, so the key (or its absence) is the only LangSmith setting."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("LANGSMITH_", "LANGCHAIN_"))
    }
    environment |= {
        "LANGSMITH_TRACING": "true",
        "LANGCHAIN_TRACING_V2": "true",
        "LANGSMITH_API_KEY": key,
    }
    process = subprocess.run(
        [sys.executable, "-c", _CONSENT_PROCESS],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout.splitlines()[-1])


def test_without_a_key_environment_tracing_stays_off(tmp_path):
    # The key is the consent: LANGSMITH_TRACING alone sends nothing.
    result = consent(tmp_path, "")
    assert result["enabled"] is False and result["tracer_attached"] is False


def test_a_key_turns_tracing_on_into_the_configured_project(tmp_path):
    # LangChain's tracer reads its project from the environment, not configure():
    # without the mirror its runs would land in LangSmith's "default" project.
    assert consent(tmp_path, "synthetic-key") == {
        "enabled": True,
        "tracer_attached": None,
        "tracer_project": "interview-agent",
    }


def test_trace_metadata_groups_an_interview_and_its_repeats_in_one_thread():
    original, repeat = uuid.uuid4(), uuid.uuid4()
    config = {
        "graph_version": "interview-v1",
        "config_version": "synthetic-config",
        "language": "es",
        "seniority": "junior",
        "interview_length": "short",
        "models": {"interviewer": {"model": "gpt-6-astra"}},
    }
    planned = Telemetry(original, config, process="planner")
    repeated = Telemetry(repeat, config, process="worker", thread_of=original)
    assert planned.trace_metadata()["thread_id"] == str(original)
    assert repeated.trace_metadata(turn_id="turn-1") == {
        "thread_id": str(original),
        "interview_id": str(repeat),
        "process": "worker",
        "graph_version": "interview-v1",
        "config_version": "synthetic-config",
        "language": "es",
        "seniority": "junior",
        "length": "short",
        "turn_id": "turn-1",
    }


@pytest.mark.parametrize("function", [planner.run_planner, evaluator.run_evaluator])
def test_traced_inputs_never_carry_settings_or_callbacks(function):
    # LangSmith hands process_inputs the bound arguments, defaults applied.
    # Settings carries the API keys; the callbacks are our own plumbing.
    signature = inspect.signature(inspect.unwrap(function))
    bound = signature.bind(**{name: f"synthetic {name}" for name in signature.parameters})
    bound.apply_defaults()
    traced = traced_inputs(dict(bound.arguments))
    assert traced.keys() == signature.parameters.keys() - {
        "settings",
        "usage_callback",
        "telemetry_callback",
    }
    assert traced["resume_markdown"] == "synthetic resume_markdown"


async def test_the_evaluator_run_carries_the_interview_but_never_the_settings(langsmith_runs):
    settings = Settings(_env_file=None, OPENAI_API_KEY="CANARY_OPENAI_KEY")
    # No candidate answers: an insufficient evaluation, without a model call.
    await evaluator.run_evaluator(
        settings,
        resume_markdown="Synthetic CV",
        job_offer="Synthetic role",
        plan={"language": "en"},
        milestones=[],
        transcript=[],
        ended_reason="candidate_left",
        usage_callback=Mock(),
        telemetry_callback=Mock(),
        langsmith_extra={"metadata": {"thread_id": "synthetic-thread"}},
    )
    (run,) = langsmith_runs()
    assert run["name"] == "evaluator" and run.get("parent_run_id") is None
    assert run["inputs"]["resume_markdown"] == "Synthetic CV"
    assert not {"settings", "usage_callback", "telemetry_callback"} & run["inputs"].keys()
    assert run["extra"]["metadata"]["thread_id"] == "synthetic-thread"
    assert "CANARY_OPENAI_KEY" not in str(langsmith_runs.client.mock_calls)


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
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    telemetry = Mock()
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


async def test_every_llm_call_reports_its_duration_ended_or_failed():
    # Feeds Grafana's "LLM call duration" panels, with or without LangSmith.
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    telemetry = Mock()
    observer = LLMObserver(telemetry, "interviewer", "gpt-6-astra", "low", "turn-1")
    ended, failed = uuid.uuid4(), uuid.uuid4()
    for run_id in (ended, failed):
        await observer.on_chat_model_start({}, [[]], run_id=run_id)
    await observer.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="Answer"))]]),
        run_id=ended,
    )
    await observer.on_llm_error(TimeoutError(), run_id=failed)
    durations = [c for c in telemetry.emit.call_args_list if c.args[1] == "duration_seconds"]
    assert len(durations) == 2 and all(c.args[2] >= 0 for c in durations)
    assert not observer.starts

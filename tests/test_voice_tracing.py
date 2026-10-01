"""LangSmith export: nothing without a key; with one, the interview's content
trace plus LiveKit's voice session trace with the recording, both registered."""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langsmith.integrations.otel import langsmith_run_id_from_otel_span_id
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from interview_agent.config import Settings
from interview_agent.observability import LLMObserver, Telemetry
from interview_agent.voice_tracing import (
    InterviewSpanProcessor,
    VoiceTracing,
    configure_voice_tracing,
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
    telemetry = Telemetry(
        None, uuid.uuid4(), Settings(_env_file=None, LANGSMITH_API_KEY="test"), **kwargs
    )
    telemetry.emit = Mock()
    return telemetry, client


async def test_without_a_key_nothing_is_configured_or_exported(monkeypatch):
    def no_client(**_):
        raise AssertionError("no LangSmith client without a key")

    monkeypatch.setattr("interview_agent.observability.Client", no_client)
    settings = Settings(_env_file=None, LANGSMITH_API_KEY="")
    assert configure_voice_tracing(settings) is None
    telemetry = Telemetry(None, uuid.uuid4(), settings, inputs={"resume_markdown": "CV"})
    telemetry.emit = Mock()
    assert telemetry.client is None
    async with telemetry.span("graph.decide", {"turn_id": "t"}) as span_id:
        telemetry.span_outputs(span_id, {"decision": "x"})
    await telemetry.export_span(uuid.uuid4(), "planner", datetime.now(UTC), datetime.now(UTC), {})
    telemetry.feedback("evaluation_score", 80)
    await telemetry.drain()
    assert not telemetry.pending


async def test_the_closing_stage_gives_the_interview_its_outputs(monkeypatch):
    evaluator, client = exporting_telemetry(
        monkeypatch, process="evaluator", closes_trace=True, inputs={"transcript": ["hola"]}
    )
    evaluator.set_outputs({"score": 72, "evidence": ["Explained the cache"]})
    await evaluator.drain()
    updates = {c.args[0]: c.kwargs for c in client.update_run.call_args_list}
    assert updates[evaluator.trace_id]["outputs"]["evidence"] == ["Explained the cache"]
    assert updates[evaluator.process_id]["outputs"]["score"] == 72
    created = {c.kwargs["id"]: c.kwargs for c in client.create_run.call_args_list}
    assert created[evaluator.process_id]["inputs"] == {"transcript": ["hola"]}


async def test_voice_session_is_its_own_audio_trace_linked_to_the_interview(monkeypatch, tmp_path):
    telemetry, _ = exporting_telemetry(monkeypatch, process="worker")
    exporter = InMemorySpanExporter()
    processor = InterviewSpanProcessor(downstream_processor=SimpleSpanProcessor(exporter))
    flushes = []
    monkeypatch.setattr(processor, "force_flush", lambda timeout_millis=0: flushes.append(1))
    provider = TracerProvider()
    provider.add_span_processor(processor)
    livekit = provider.get_tracer("livekit-agents")
    voice = VoiceTracing(processor, provider.get_tracer("interview_agent"))
    recording = tmp_path / "audio.ogg"
    recording.write_bytes(b"OggS-session-audio")

    async def job():
        # A job that never reaches an interview is not exported at all.
        livekit.start_span("job_entrypoint").end()
        # LiveKit runs the entrypoint with its job span current.
        root = livekit.start_span("job_entrypoint")
        with otel_trace.use_span(root, end_on_exit=True):
            session_trace = voice.session_trace_id()
            voice.follow(telemetry, session_trace)
            observer = LLMObserver(telemetry, "interviewer", "gpt-6-astra", "low", "t1")
            with (
                livekit.start_as_current_span("agent_session"),
                livekit.start_as_current_span(
                    "agent_turn", attributes={"lk.response.text": "Why that policy?"}
                ),
                livekit.start_as_current_span(
                    "llm_request",
                    attributes={
                        "lk.llm_metrics": json.dumps(
                            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        ),
                        "gen_ai.usage.input_tokens": 0,
                    },
                ),
                livekit.start_as_current_span("llm_request_run"),
            ):
                async with telemetry.span("graph.decide", {"turn_id": "t1"}) as span_id:
                    run_id = uuid.uuid4()
                    await observer.on_chat_model_start(
                        {}, [[HumanMessage("I built the payment cache")]], run_id=run_id
                    )
                    message = AIMessage(
                        "Why that eviction policy?",
                        usage_metadata={
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                    )
                    await observer.on_llm_end(
                        LLMResult(generations=[[ChatGeneration(message=message)]]),
                        run_id=run_id,
                    )
                    telemetry.span_outputs(span_id, {"decision": {"action": "followup"}})
            processor.attach_session_report(
                SimpleNamespace(
                    chat_history=None,
                    audio_recording_path=recording,
                    audio_recording_started_at=time.time(),
                ),
                telemetry.thread_id,
            )
        return session_trace

    session_trace = await asyncio.create_task(job())
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {
        "voice_session",
        "agent_session",
        "agent_turn",
        "llm_request",
        "llm_request_run",
        "graph.decide",
        "interviewer",
    }
    assert len(exporter.get_finished_spans()) == len(spans)  # the stray job is dropped
    root = spans["voice_session"]  # LiveKit's job_entrypoint, renamed
    # The trace the worker registers for deletion is the one LangSmith files
    # these runs under: a parentless span's run (and trace) id.
    assert root.parent is None
    assert langsmith_run_id_from_otel_span_id(root.context.span_id) == session_trace
    assert {span.context.trace_id for span in spans.values()} == {root.context.trace_id}
    # The integration's default: recording and audio modality on the root.
    attachment = json.loads(root.attributes["langsmith.attachments"])[0]
    assert attachment["mime_type"] == "audio/ogg"
    assert root.attributes["langsmith.metadata.ls_modality"] == "audio"
    assert root.attributes["langsmith.metadata.interview_trace_id"] == str(telemetry.trace_id)
    assert flushes  # sent before the job process may exit
    assert telemetry._annotations["voice_trace_id"] == str(session_trace)
    # LiveKit's LLM is the dialogue controller: a chain without usage, so the
    # model call below it is the only one counted and priced.
    llm_request = spans["llm_request"]
    assert llm_request.attributes["langsmith.span.kind"] == "chain"
    assert not any("usage" in key for key in llm_request.attributes)
    node, leaf = spans["graph.decide"], spans["interviewer"]
    assert node.parent.span_id == spans["llm_request_run"].context.span_id
    assert leaf.parent.span_id == node.context.span_id
    assert json.loads(node.attributes["gen_ai.completion"])["decision"]["action"] == "followup"
    assert leaf.attributes["langsmith.span.kind"] == "llm"
    assert "payment cache" in leaf.attributes["gen_ai.prompt"]
    assert "eviction policy" in leaf.attributes["gen_ai.completion"]
    usage = json.loads(leaf.attributes["langsmith.usage_metadata"])
    assert usage["total_tokens"] == 110 and usage["total_cost"] > 0
    # The interview trace and its voice sessions share one LangSmith thread.
    assert {span.attributes["langsmith.metadata.thread_id"] for span in spans.values()} == {
        telemetry.thread_id
    }


async def test_work_outside_a_followed_trace_lands_under_the_worker(monkeypatch):
    telemetry, _ = exporting_telemetry(monkeypatch, process="worker")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.tracer = provider.get_tracer("interview_agent")
    with provider.get_tracer("other").start_as_current_span("unrelated"):
        async with telemetry.span("closing"):
            pass
    span = next(s for s in exporter.get_finished_spans() if s.name == "closing")
    assert span.context.trace_id == telemetry.trace_id.int
    assert langsmith_run_id_from_otel_span_id(span.parent.span_id) == telemetry.process_id

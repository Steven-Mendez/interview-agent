"""LiveKit/LangGraph regression checks without provider calls or real audio."""

import asyncio
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc
from livekit.agents import Agent, AgentSession, stt
from livekit.agents.llm import ChatContext
from livekit.agents.voice.audio_recognition import AudioRecognition
from livekit.agents.voice.endpointing import BaseEndpointing
from livekit.agents.voice.turn import TurnDetectionEvent
from sqlalchemy import func, select

from interview_agent import agent
from interview_agent.config import settings
from interview_agent.interview import db
from interview_agent.interview.context import MAX_RESUME_CHARS
from interview_agent.interview.dialogue import DialogueLLM
from interview_agent.interview.workers import WorkerCoordinator, WorkerOwnershipError
from interview_agent.observability import Telemetry
from interview_agent.playback import closing_state


async def test_configured_session_disables_speculation(monkeypatch):
    graph = DialogueLLM(SimpleNamespace(settings=settings))
    # Construct the actual AgentSession, replacing only audio providers.
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": None}))
    with monkeypatch.context() as providers:
        providers.setattr(agent, "DrainableInferenceSTT", lambda **kwargs: None)
        providers.setattr(agent.inference, "TTS", lambda **kwargs: None)
        providers.setattr(agent.inference, "TurnDetector", lambda: None)
        session = agent._build_session(
            ctx,
            graph,
            {
                "language": "en",
                "stt_model": settings.stt_model,
                "tts_model": "cartesia/sonic-3",
                "tts_voice": "voice",
            },
        )
    assert session.options.preemptive_generation["enabled"] is False
    assert session.options.endpointing["min_delay"] == 1.5
    assert session.options.endpointing["max_delay"] == 2.5


async def test_invalid_worker_context_marks_failure_closes_room_and_releases_engine(
    monkeypatch, postgres_sessionmaker
):
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as transaction:
        transaction.add(
            db.Conversation(
                id=conversation_id,
                resume_markdown="r" * (MAX_RESUME_CHARS + 1),
                job_offer="offer",
                status="planned",
                plan={},
                run_config={
                    "schema_version": 2,
                    "models": {"interviewer": {"model": "gpt-6-astra", "reasoning_effort": "low"}},
                    "stt_model": "assemblyai/universal-3-6-pro",
                },
                question_limit=2,
                followup_limit=1,
                agent_settings={
                    "language": "es",
                    "tts_model": "cartesia/sonic-3.6-2026-08-27",
                    "tts_voice": "test-voice",
                },
                max_minutes=15,
            )
        )
        await transaction.commit()
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        agent.db, "create_engine_and_sessionmaker", lambda *args: (engine, postgres_sessionmaker)
    )
    delete = AsyncMock()
    ctx = SimpleNamespace(
        job=SimpleNamespace(room=SimpleNamespace(name="dispatched-room")),
        api=SimpleNamespace(room=SimpleNamespace(delete_room=delete)),
        shutdown=Mock(),
    )
    await agent._run_interview(ctx, conversation_id)
    async with postgres_sessionmaker() as transaction:
        conversation = await db.get_conversation(transaction, conversation_id)
        assert conversation.status == "error" and conversation.worker_epoch == 1
    assert delete.await_args.args[0].room == "dispatched-room"
    engine.dispose.assert_awaited_once()
    ctx.shutdown.assert_called_once_with(reason="invalid_source_context")


@pytest.mark.parametrize(
    "reconnecting,displaced,recovering,traced",
    [
        (False, False, False, False),
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
    ],
)
async def test_worker_reaches_session_start_with_a_real_unconnected_room(
    monkeypatch,
    postgres_sessionmaker,
    recorded_metrics,
    reconnecting,
    displaced,
    recovering,
    traced,
):
    conversation_id = uuid.uuid4()
    async with postgres_sessionmaker() as transaction:
        transaction.add(
            db.Conversation(
                id=conversation_id,
                status="planned",
                plan={"language": "es"},
                resume_markdown="Synthetic CV",
                job_offer="Synthetic role",
                max_minutes=8,
                run_config={
                    "schema_version": 2,
                    "models": {"interviewer": {"model": "gpt-6-astra", "reasoning_effort": "low"}},
                    "stt_model": "assemblyai/universal-3-6-pro",
                },
                question_limit=2,
                followup_limit=1,
                agent_settings={
                    "language": "es",
                    "tts_model": "cartesia/sonic-3.6-2026-08-27",
                    "tts_voice": "test-voice",
                },
            )
        )
        await transaction.commit()
    if reconnecting or recovering:
        previous = WorkerCoordinator(conversation_id, postgres_sessionmaker)
        await previous.claim()
        async with postgres_sessionmaker() as transaction:
            conversation = await db.get_conversation(transaction, conversation_id)
            now = await transaction.scalar(select(func.clock_timestamp()))
            conversation.worker_lease_until = now - timedelta(seconds=1)
            if reconnecting:
                conversation.worker_disconnected_at = now - timedelta(seconds=5)
            if recovering:
                conversation.status = "closing"
                conversation.closing_owner_id = previous.owner_id
                conversation.closing_id, conversation.closing_attempt_id = (
                    uuid.uuid4(),
                    uuid.uuid4(),
                )
                conversation.closing_stream_id = "already-started-stream"
                original_closure = conversation.closing_id
                original_acquisition = now - timedelta(seconds=40)
                conversation.closing_acquired_at = original_acquisition
                conversation.closing_deadline_at = now - timedelta(seconds=1)
            await transaction.commit()
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        agent.db, "create_engine_and_sessionmaker", lambda *args: (engine, postgres_sessionmaker)
    )
    monkeypatch.setattr(agent, "_trigger_evaluation", AsyncMock())
    exported = []

    def flush(*args, **kwargs):
        # What leaves the job's process when its interview ends.
        exported.append(
            {
                name: len(recorded_metrics(f"interview_agent.{name}"))
                for name in ("closing.playback_confirmed", "worker.ownership_lost")
            }
        )
        return True

    monkeypatch.setattr(agent.otel_metrics, "force_flush", flush)
    flushed_errors = []
    monkeypatch.setattr(agent.error_reporting, "flush", flushed_errors.append)
    session = AgentSession(vad=None)
    # Provider/audio startup alone is doubled. Ownership, PostgreSQL fencing,
    # subscriptions and the pre-connect Room.local_participant guard are real.
    start = AsyncMock()
    monkeypatch.setattr(session, "start", start)
    monkeypatch.setattr(session, "generate_reply", AsyncMock())
    monkeypatch.setattr(agent, "_build_session", lambda *args: session)
    threads = []
    monkeypatch.setattr(agent, "set_thread_id", threads.append)
    callbacks = []
    room = rtc.Room()
    if reconnecting:
        room._remote_participants["candidate"] = SimpleNamespace(identity="candidate")
    ctx = SimpleNamespace(
        room=room,
        add_shutdown_callback=callbacks.append,
        shutdown=Mock(),
        api=SimpleNamespace(room=SimpleNamespace(delete_room=AsyncMock())),
        job=SimpleNamespace(room=SimpleNamespace(name="dispatched-room")),
        # With a LangSmith key prewarm built the voice processor; without, none.
        proc=SimpleNamespace(userdata={"langsmith_processor": Mock() if traced else None}),
    )
    await agent._run_interview(ctx, conversation_id)
    start.assert_awaited_once()
    # LiveKit records the session audio for LangSmith only.
    assert start.call_args.kwargs["record"] is (agent.RECORD_AUDIO if traced else False)
    # The voice session's trace joins the interview's Thread.
    assert threads == [str(conversation_id)]
    assert start.call_args.kwargs["room"] is room
    assert start.call_args.kwargs["agent"]._worker.lease.epoch == (
        2 if reconnecting or recovering else 1
    )
    assert len(callbacks) == 1
    if reconnecting:
        async with asyncio.timeout(1):
            while True:
                async with postgres_sessionmaker() as transaction:
                    current = await db.get_conversation(transaction, conversation_id)
                    if current.worker_disconnected_at is None:
                        break
                await asyncio.sleep(0.01)
    if displaced:
        async with postgres_sessionmaker() as transaction:
            current = await db.get_conversation(transaction, conversation_id)
            current.worker_lease_until = await transaction.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)
            await transaction.commit()
        replacement = WorkerCoordinator(conversation_id, postgres_sessionmaker)
        assert await replacement.claim()
    await callbacks[0]()
    engine.dispose.assert_awaited_once()
    # Errors logged while it ended leave before the job's process exits.
    assert flushed_errors == [agent._ERRORS_FLUSH_SECONDS]
    # Every end path exports once, its last samples included.
    assert exported == [
        {"closing.playback_confirmed": 0, "worker.ownership_lost": 1}
        if displaced
        else {"closing.playback_confirmed": 1, "worker.ownership_lost": 0}
    ]
    async with postgres_sessionmaker() as transaction:
        conversation = await db.get_conversation(transaction, conversation_id)
        if displaced:
            assert (
                conversation.status == "interviewing" and conversation.transcript_sealed_at is None
            )
            assert conversation.worker_owner_id == replacement.owner_id
            assert conversation.farewell_status is None
            assert start.call_args.kwargs["agent"]._worker.lost
        else:
            assert conversation.status == "completed" and conversation.transcript_sealed_at
            assert conversation.farewell_status == ("timeout" if recovering else "not_possible")
            if recovering:
                assert conversation.closing_id == original_closure
                assert conversation.closing_acquired_at == original_acquisition
                assert conversation.closing_stream_id == "already-started-stream"
                assert conversation.transcript_integrity == "partial"
                assert conversation.closing_ack_deadline_at == original_acquisition + timedelta(
                    seconds=35
                )
                session.generate_reply.assert_not_awaited()
    if recovering:
        state = await closing_state(postgres_sessionmaker, conversation_id)
        assert 0 <= state["remaining_seconds"] <= 5


async def test_end_of_interview_flush_waits_briefly_for_samples_in_flight(
    recorded_metrics, monkeypatch
):
    exported = []

    def flush(*args, **kwargs):
        exported.append(len(recorded_metrics("interview_agent.worker.ownership_lost")))
        return True

    monkeypatch.setattr(agent.otel_metrics, "force_flush", flush)
    # Deferred dimensions hold the sample back for a moment, as a busy loop would.
    telemetry = Telemetry(uuid.uuid4(), defer_dimensions=True)
    telemetry.emit("worker", "ownership_lost", 1)
    asyncio.get_running_loop().call_later(0.05, telemetry.resolve_dimensions, {})
    await agent._flush_metrics(telemetry)
    assert exported == [1]


def test_prewarm_without_a_langsmith_key_builds_no_voice_processor(monkeypatch):
    monkeypatch.setattr(settings, "langsmith_api_key", "")
    monkeypatch.setattr(agent.silero.VAD, "load", Mock(return_value="vad"))
    monkeypatch.setattr(agent.otel_metrics, "configure", Mock())
    unexpected = Mock(side_effect=AssertionError("no LangSmith export without a key"))
    monkeypatch.setattr(agent, "LiveKitLangSmithSpanProcessor", unexpected)
    monkeypatch.setattr(agent.livekit_telemetry, "set_tracer_provider", unexpected)
    proc = SimpleNamespace(userdata={})
    agent.prewarm(proc)
    assert proc.userdata == {"vad": "vad", "langsmith_processor": None}


def test_a_langsmith_key_binds_livekit_spans_to_a_private_provider(monkeypatch):
    from opentelemetry import trace as otel_trace

    from interview_agent.config import Settings

    processor = Mock()
    built = Mock(return_value=processor)
    providers = []
    monkeypatch.setattr(agent, "LiveKitLangSmithSpanProcessor", built)
    monkeypatch.setattr(agent.livekit_telemetry, "set_tracer_provider", providers.append)
    configured = Settings(
        _env_file=None,
        LANGSMITH_API_KEY="synthetic-key",
        LANGSMITH_PROJECT="interview-agent",
        LANGSMITH_ENDPOINT="https://ls.example.com/api/v1",
    )
    assert agent.langsmith_voice_processor(configured) is processor
    assert built.call_args.kwargs == {
        "api_key": "synthetic-key",
        "project": "interview-agent",
        "endpoint": "https://ls.example.com/api/v1/otel/v1/traces",
        "recording_mode": "session_report",
    }
    (provider,) = providers
    # Never the OTel global: nothing else starts exporting through it.
    assert otel_trace.get_tracer_provider() is not provider
    tracer = provider.get_tracer("livekit-agents")
    root = tracer.start_span("job_entrypoint")
    with otel_trace.use_span(root):
        tracer.start_span("agent_session").end()
    processor.force_flush.assert_not_called()
    # LiveKit ends the root as the job process exits: it (and its recording)
    # is sent then, not on the batch exporter's timer.
    root.end()
    processor.force_flush.assert_called_once_with(10_000)
    provider.shutdown()


class LocalEndOfTurnDetector:
    """A local EOT prediction double; the SDK owns waiting and STT assembly."""

    model = "local"
    provider = "test"

    async def supports_language(self, language):
        return True

    async def unlikely_threshold(self, language):
        return 0.59

    async def predict_end_of_turn(self, chat_ctx):
        return 0.8


class LocalAudioTurnDetector:
    model = "local-audio"
    provider = "test"

    def stream(self):
        return LocalAudioTurnStream()


class LocalAudioTurnStream:
    model = "local-audio"
    provider = "test"
    is_fallback = False
    prediction_timeout = 0.5

    async def supports_language(self, language):
        return True

    async def unlikely_threshold(self, language):
        return 0.59

    async def backchannel_threshold(self, language):
        return None

    def predict(self):
        raise AssertionError("the recorded EOT prediction is already cached")

    def cancel_inference(self, *, timed_out=False):
        pass

    def flush(self, reason=None):
        pass

    def push_audio(self, frame):
        pass

    def end_input(self):
        pass

    async def aclose(self):
        pass


@pytest.mark.parametrize("audio_detector", [False, True])
@pytest.mark.parametrize("min_delay,expected_turns", [(0.3, 2), (1.5, 1)])
async def test_livekit_endpointing_keeps_observed_late_stt_tail(
    min_delay, expected_turns, audio_detector
):
    # Regression at the installed SDK boundary: the incident's late final
    # arrived 1.17s after the audio anchor. Old timing commits twice; new
    # timing includes both finals before any response/tools can be generated.
    session = AgentSession(vad=None)
    committed = []
    hooks = Mock()
    hooks.retrieve_chat_ctx.return_value = ChatContext.empty()
    hooks.on_end_of_turn.side_effect = lambda info: committed.append(info.new_transcript) or True
    recognition = AudioRecognition(
        session,
        hooks=hooks,
        endpointing=BaseEndpointing(min_delay=min_delay, max_delay=2.5),
        stt=None,
        vad=Mock(),
        interruption_detection=None,
        turn_detection=LocalAudioTurnDetector() if audio_detector else LocalEndOfTurnDetector(),
    )
    # Seed the observed VAD silence boundary, then feed real SpeechEvents.
    recognition._last_speaking_time = time.time()
    recognition._speech_start_time = time.time() - 5
    if audio_detector:
        recognition._turn_detector_stream = LocalAudioTurnStream()
        recognition._turn_detector_prediction_fut = asyncio.get_running_loop().create_future()
        recognition._turn_detector_prediction_fut.set_result(
            TurnDetectionEvent(
                type="eot_prediction",
                end_of_turn_probability=0.8,
                last_speaking_time=recognition._last_speaking_time,
            )
        )

    async def final(text):
        await recognition._on_stt_event(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language="es", text=text)],
            )
        )

    try:
        await final("Un commit guarda cambios.")
        await asyncio.sleep(1.17)
        assert len(committed) == (1 if min_delay == 0.3 else 0)
        await final("Le pongo un mensaje que explica qué cambié.")
        await asyncio.wait_for(recognition._end_of_turn_task, timeout=2)
        assert len(committed) == expected_turns
        assert " ".join(committed) == (
            "Un commit guarda cambios. Le pongo un mensaje que explica qué cambié."
        )
    finally:
        await recognition._aclose()


@pytest.mark.parametrize("node", ["llm_node", "tts_node"])
async def test_sdk_generation_stops_before_next_chunk_after_ownership_loss(monkeypatch, node):
    live = True

    def require_local():
        if not live:
            raise WorkerOwnershipError("expired")

    worker = SimpleNamespace(require_local=require_local)
    interviewer = agent.InterviewAgent(instructions="Synthetic", worker=worker)

    async def provider(*args):
        yield "first"
        yield "late"

    monkeypatch.setattr(Agent.default, node, staticmethod(provider))
    stream = (
        interviewer.llm_node(ChatContext.empty(), [], None)
        if node == "llm_node"
        else interviewer.tts_node(provider(), None)
    )
    assert await anext(stream) == "first"
    live = False
    with pytest.raises(WorkerOwnershipError):
        await anext(stream)

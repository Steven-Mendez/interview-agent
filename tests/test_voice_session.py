"""LiveKit/LangGraph regression checks without provider calls or real audio."""

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessageChunk
from livekit import rtc
from livekit.agents import Agent, AgentSession, stt
from livekit.agents.llm import ChatContext
from livekit.agents.voice.audio_recognition import AudioRecognition
from livekit.agents.voice.endpointing import BaseEndpointing
from livekit.agents.voice.turn import TurnDetectionEvent

from interview_agent import agent
from interview_agent.config import settings
from interview_agent.interview import interviewer_graph
from interview_agent.interview.context import MAX_RESUME_CHARS
from interview_agent.interview.db import Milestone


class LocalToolModel:
    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "complete_milestone",
                    "args": json.dumps({"milestone_number": 1, "notes": "Explained SQL"}),
                    "id": "milestone",
                    "index": 0,
                },
                {"name": "end_interview", "args": '{"reason":"Done"}', "id": "end", "index": 1},
            ],
        )


class LocalSessionMaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


async def test_configured_session_disables_speculation_and_keeps_tools_working(monkeypatch):
    end_event = asyncio.Event()
    milestone = Milestone(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        position=0,
        title="SQL",
        description="Explain",
        completed=False,
    )
    write = AsyncMock()
    monkeypatch.setattr(interviewer_graph, "build_chat_model", lambda *a, **k: LocalToolModel())
    monkeypatch.setattr(interviewer_graph.db, "get_milestones", AsyncMock(return_value=[milestone]))
    monkeypatch.setattr(interviewer_graph.db, "complete_milestone", write)
    graph = interviewer_graph.build_interviewer_graph(
        settings, milestone.conversation_id, LocalSessionMaker(), end_event, "Interview"
    )
    # Construct the actual AgentSession and LLMAdapter, replacing only audio
    # providers. No connection or model credentials are needed for this test.
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": None}))
    with monkeypatch.context() as providers:
        providers.setattr(agent.inference, "STT", lambda **kwargs: None)
        providers.setattr(agent.inference, "TTS", lambda **kwargs: None)
        providers.setattr(agent.inference, "TurnDetector", lambda: None)
        session = agent._build_session(ctx, graph, None)
    assert session.options.preemptive_generation["enabled"] is False
    assert session.options.endpointing["min_delay"] == 1.5
    assert session.options.endpointing["max_delay"] == 2.5
    assert not end_event.is_set()
    write.assert_not_called()

    try:
        await session.start(Agent(instructions="Interview"), record=False)
        # A committed text input follows the normal generation path. Tool JSON
        # never needs to become spoken output for the internal tools to run.
        await asyncio.wait_for(session.run(user_input="I explained SQL"), timeout=5)
        write.assert_awaited_once()
        assert end_event.is_set()
    finally:
        await session.aclose()


async def test_invalid_worker_context_marks_failure_closes_room_and_releases_engine(monkeypatch):
    conversation_id = uuid.uuid4()
    conversation = SimpleNamespace(
        resume_markdown="r" * (MAX_RESUME_CHARS + 1),
        job_offer="offer",
        status="planned",
        plan={},
        max_minutes=15,
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        agent.db, "create_engine_and_sessionmaker", lambda *args: (engine, LocalSessionMaker())
    )
    monkeypatch.setattr(agent.db, "get_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr(agent.db, "get_milestones", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent.db, "get_messages", AsyncMock(return_value=[]))
    failed = AsyncMock(return_value=True)
    monkeypatch.setattr(agent.db, "set_status_if", failed)
    delete = AsyncMock()
    ctx = SimpleNamespace(
        job=SimpleNamespace(room=SimpleNamespace(name="dispatched-room")),
        api=SimpleNamespace(room=SimpleNamespace(delete_room=delete)),
        shutdown=Mock(),
    )
    await agent._run_interview(ctx, conversation_id)
    failed.assert_awaited_once()
    assert failed.call_args.args[1:] == (conversation_id, "planned", "error")
    assert delete.await_args.args[0].room == "dispatched-room"
    engine.dispose.assert_awaited_once()
    ctx.shutdown.assert_called_once_with(reason="invalid_source_context")


async def test_worker_reaches_session_start_with_a_real_unconnected_room(monkeypatch):
    conversation_id = uuid.uuid4()
    conversation = SimpleNamespace(
        status="planned",
        plan={"language": "es"},
        max_minutes=8,
        agent_settings={},
        ended_reason=None,
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        agent.db, "create_engine_and_sessionmaker", lambda *args: (engine, LocalSessionMaker())
    )
    monkeypatch.setattr(agent.db, "get_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr(agent.db, "get_milestones", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent.db, "get_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent.db, "set_status", AsyncMock())
    monkeypatch.setattr(agent, "build_interviewer_prompt", lambda *args: "Interview")
    monkeypatch.setattr(agent, "build_interviewer_graph", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_trigger_evaluation", AsyncMock())
    session = AgentSession(vad=None)
    # Only actual provider/audio startup is doubled. The worker wiring,
    # subscriptions and pre-connect Room.local_participant guard are real.
    start = AsyncMock()
    monkeypatch.setattr(session, "start", start)
    monkeypatch.setattr(session, "generate_reply", AsyncMock())
    monkeypatch.setattr(agent, "_build_session", lambda *args: session)
    callbacks = []
    room = rtc.Room()
    ctx = SimpleNamespace(room=room, add_shutdown_callback=callbacks.append)
    await agent._run_interview(ctx, conversation_id)
    start.assert_awaited_once()
    assert start.call_args.kwargs["room"] is room
    assert len(callbacks) == 1
    await callbacks[0]()
    engine.dispose.assert_awaited_once()


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
        using_default_vad=False,
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
        await recognition.aclose()

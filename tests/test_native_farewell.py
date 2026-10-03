"""Real SDK speech, recording and LangSmith close hook, without provider calls."""

import asyncio
import math
import time
from array import array
from types import SimpleNamespace
from unittest.mock import Mock

import av
import pytest
from langsmith.integrations.livekit import LiveKitLangSmithSpanProcessor
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, tts
from livekit.agents.voice import io
from livekit.agents.voice.recorder_io import RecorderIO
from test_closing import coordinator, create_conversation

from interview_agent.agent import InterviewAgent
from interview_agent.interview import db


class OfflineTTS(tts.TTS):
    def __init__(self):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True), sample_rate=48000, num_channels=1
        )

    def synthesize(self, text, **kwargs):
        raise AssertionError("The local TTS node supplies audio without a provider")


class IdleAudioInput(io.AudioInput):
    def __init__(self):
        super().__init__(label="offline-input")

    async def __anext__(self):
        await asyncio.Event().wait()
        raise StopAsyncIteration


class LocalAudioOutput(io.AudioOutput):
    def __init__(self):
        super().__init__(
            label="offline-output", capabilities=io.AudioOutputCapabilities(pause=False)
        )
        self.captured = asyncio.Event()
        self.release = asyncio.Event()
        self.frames = []
        self.waiter = None

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.frames.append(frame)
        if not self.captured.is_set():
            self.on_playback_started(created_at=time.time())
        self.captured.set()

    def flush(self):
        super().flush()

        async def finish():
            await self.release.wait()
            self.on_playback_finished(
                playback_position=sum(frame.duration for frame in self.frames), interrupted=False
            )

        self.waiter = asyncio.create_task(finish())

    def clear_buffer(self):
        if self.waiter is not None:
            self.waiter.cancel()
        self.on_playback_finished(playback_position=0, interrupted=True)


@pytest.mark.parametrize("tts_error", [False, True])
async def test_native_farewell_sdk_history_recording_and_langsmith_report(
    postgres_sessionmaker, monkeypatch, tmp_path, tts_error
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    texts = []

    async def synthetic_tts(_agent, text, _settings):
        async for chunk in text:
            texts.append(chunk)
        if tts_error:
            raise RuntimeError("Controlled offline synthesis failure")
        samples = array(
            "h", (int(4000 * math.sin(i * 2 * math.pi * 440 / 48000)) for i in range(4800))
        )
        yield rtc.AudioFrame(
            data=samples.tobytes(),
            sample_rate=48000,
            num_channels=1,
            samples_per_channel=len(samples),
        )

    monkeypatch.setattr(Agent.default, "tts_node", staticmethod(synthetic_tts))
    session = AgentSession(tts=OfflineTTS(), turn_detection="manual")
    output = LocalAudioOutput()
    recorder = RecorderIO(agent_session=session)
    session.input.audio = recorder.record_input(IdleAudioInput())
    session.output.audio = recorder.record_output(output)
    recording = tmp_path / "native.ogg"
    await recorder.start(output_path=recording)
    owner, _, calls = coordinator(postgres_sessionmaker, conversation_id, timeout=3)
    owner.session = session
    owner.seal_transcript = session.aclose
    persisted = set()

    def persist_item(event):
        item = event.item
        assert owner.is_farewell_item(item)
        item.metrics["farewell_confirmation_source"] = "agent_playout"

        async def persist():
            await owner.wait_for_stt_drain()
            async with postgres_sessionmaker() as db_session:
                await db.insert_message(
                    db_session,
                    conversation_id,
                    item.role,
                    item.text_content,
                    source_id=item.id,
                    interrupted=item.interrupted,
                    metrics=dict(item.metrics),
                )

        persisted.add(asyncio.create_task(persist()))

    async def drain():
        if persisted:
            await asyncio.gather(*persisted)

    owner.drain_transcript = drain
    session.on("conversation_item_added", persist_item)
    session.generate_reply = Mock(
        side_effect=AssertionError("Farewell must not call the dialogue model")
    )
    report_context = SimpleNamespace(
        _primary_agent_session=session,
        job=SimpleNamespace(
            id="offline-job", room=SimpleNamespace(sid="offline-room", name="offline-room")
        ),
    )
    report_context.make_session_report = lambda current: JobContext.make_session_report(
        report_context, current
    )
    processor = LiveKitLangSmithSpanProcessor(
        downstream_processor=Mock(), recording_mode="session_report"
    )
    capture = Mock()
    monkeypatch.setattr(processor, "_attach_session_report_to_trace", capture)
    try:
        await session.start(agent=InterviewAgent(instructions="Offline"), record=False)
        # Install the exact close hook used in production, with a local job
        # report and exporter. No LiveKit room, LangSmith or TTS HTTP calls.
        session._recorder_io = recorder
        monkeypatch.setattr(agents, "get_job_context", lambda **kwargs: report_context)
        processor._install_session_report_hook(1)
        finish = asyncio.create_task(owner.finish("candidate_requested"))
        if not tts_error:
            await asyncio.wait_for(output.captured.wait(), 2)
            capture.assert_not_called()
            assert not finish.done()
            await asyncio.sleep(0.1)
            output.release.set()
        assert await asyncio.wait_for(finish, 4) == ("failed" if tts_error else "played")
        capture.assert_called_once()
        report = capture.call_args.args[0]
        messages = [
            item for item in report.chat_history.items if getattr(item, "role", None) == "assistant"
        ]
        assert len(messages) == (0 if tts_error else 1)
        if not tts_error:
            assert messages[0].id == owner._speech.chat_items[0].id
            assert messages[0].text_content == "".join(texts)
            assert messages[0].metrics["farewell_confirmation_source"] == "agent_playout"
            assert report.audio_recording_path == recording and not recorder.recording
            with av.open(str(recording)) as audio:
                frames = list(audio.decode(audio=0))
            assert frames and any(frame.to_ndarray().max() > 0 for frame in frames)
        async with postgres_sessionmaker() as db_session:
            row = await db_session.get(db.Conversation, conversation_id)
            stored = await db.get_messages(db_session, conversation_id)
            assert len(stored) == (0 if tts_error else 1)
            assert row.transcript_sealed_at is not None
            if not tts_error:
                assert stored[0].source_id == messages[0].id == row.closing_stream_id
        session.generate_reply.assert_not_called()
        assert calls == []
    finally:
        await session.aclose()
        await recorder.aclose()
        processor.shutdown()

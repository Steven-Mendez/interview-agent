"""Turn-level UI forwarding through LiveKit's public session events."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from livekit.agents import AgentSession, ConversationItemAddedEvent, UserInputTranscribedEvent
from livekit.agents.llm import ChatMessage

from interview_agent import user_transcript
from interview_agent.user_transcript import USER_TRANSCRIPT_TOPIC, UserTranscriptForwarder


def transcribe(session, text, *, final=True):
    session.emit(
        "user_input_transcribed", UserInputTranscribedEvent(transcript=text, is_final=final)
    )


def commit(session, text):
    session.emit(
        "conversation_item_added",
        ConversationItemAddedEvent(item=ChatMessage(role="user", content=[text])),
    )


async def flush():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_one_turn_id_across_sentences_and_authoritative_final_correction():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, SimpleNamespace(send_text=send))
    try:
        transcribe(session, "First sentence.")
        await flush()
        transcribe(session, "Second sent", final=False)
        await flush()
        transcribe(session, "Second sentence.")
        await flush()
        commit(session, "First sentence. Second sentence corrected.")
        await flush()
        calls = send.call_args_list
        assert [c.args[0] for c in calls] == [
            "First sentence.",
            "First sentence. Second sent",
            "First sentence. Second sentence.",
            "First sentence. Second sentence corrected.",
        ]
        ids = [c.kwargs["attributes"]["lk.segment_id"] for c in calls]
        assert len(set(ids)) == 1
        assert all(c.kwargs["topic"] == USER_TRANSCRIPT_TOPIC for c in calls)
        assert [c.kwargs["attributes"]["lk.transcription_final"] for c in calls] == [
            "false",
            "false",
            "false",
            "true",
        ]
        # Actual repetition in a new turn must not be deduplicated or merged.
        transcribe(session, "First sentence.")
        commit(session, "First sentence.")
        await flush()
        assert send.call_args.kwargs["attributes"]["lk.segment_id"] != ids[0]
        assert send.call_args.args == ("First sentence.",)
    finally:
        await forwarder.aclose()


async def test_repeated_sentences_inside_one_turn_are_preserved():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, SimpleNamespace(send_text=send))
    transcribe(session, "I used Git.")
    transcribe(session, "I used Git.")
    await flush()
    assert send.call_args.args == ("I used Git. I used Git.",)
    commit(session, "I used Git. I used Git.")
    await forwarder.aclose()


async def test_failed_publish_does_not_stop_later_final_or_log_candidate_text(caplog):
    session = AgentSession(vad=None)
    send = AsyncMock(side_effect=[RuntimeError("private candidate text"), None])
    forwarder = UserTranscriptForwarder(session, SimpleNamespace(send_text=send))
    transcribe(session, "Candidate's private answer")
    await flush()
    commit(session, "Candidate's private answer")
    await forwarder.aclose()
    assert send.await_count == 2
    assert send.call_args.kwargs["attributes"]["lk.transcription_final"] == "true"
    assert "private" not in caplog.text


async def test_slow_publisher_coalesces_updates_but_keeps_distinct_confirmed_turns():
    session = AgentSession(vad=None)
    blocked = asyncio.Event()
    started = asyncio.Event()
    calls = []

    async def send(text, **kwargs):
        calls.append((text, kwargs))
        started.set()
        await blocked.wait()

    forwarder = UserTranscriptForwarder(session, SimpleNamespace(send_text=send))
    transcribe(session, "First")
    await started.wait()
    for i in range(100):
        transcribe(session, str(i), final=False)
    commit(session, "First turn corrected")
    transcribe(session, "Second turn")
    commit(session, "Second turn")
    blocked.set()
    await forwarder.aclose()
    assert [text for text, _ in calls] == ["First", "First turn corrected", "Second turn"]
    assert calls[0][1]["attributes"]["lk.segment_id"] == calls[1][1]["attributes"]["lk.segment_id"]
    assert calls[1][1]["attributes"]["lk.segment_id"] != calls[2][1]["attributes"]["lk.segment_id"]


async def test_close_unsubscribes_and_another_session_has_fresh_turn_identity():
    session = AgentSession(vad=None)
    send = AsyncMock()
    old = UserTranscriptForwarder(session, SimpleNamespace(send_text=send))
    commit(session, "Answer")
    await old.aclose()
    old_id = send.call_args.kwargs["attributes"]["lk.segment_id"]
    commit(session, "Ignored after close")
    await flush()
    assert send.await_count == 1
    new = UserTranscriptForwarder(session, SimpleNamespace(send_text=send))
    commit(session, "Answer")
    await new.aclose()
    assert send.call_args.kwargs["attributes"]["lk.segment_id"] != old_id


async def test_shutdown_cancels_stalled_publish_and_can_close_twice(monkeypatch):
    monkeypatch.setattr(user_transcript, "_PUBLISH_TIMEOUT", 0.02)
    session = AgentSession(vad=None)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    forwarder = UserTranscriptForwarder(session, SimpleNamespace(send_text=send))
    commit(session, "Answer")
    await started.wait()
    await forwarder.aclose()
    await forwarder.aclose()
    assert cancelled.is_set()
    assert forwarder._task.done()

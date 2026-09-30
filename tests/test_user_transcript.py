"""Turn-level UI forwarding through LiveKit's public session events."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from livekit import rtc
from livekit.agents import (
    AgentSession,
    AgentStateChangedEvent,
    ConversationItemAddedEvent,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
)
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


def room_for(send):
    return SimpleNamespace(local_participant=SimpleNamespace(send_text=send))


def agent_speaks(session):
    session.emit(
        "agent_state_changed",
        AgentStateChangedEvent(old_state="thinking", new_state="speaking"),
    )


def assistant_item(session, *, interrupted=False):
    session.emit(
        "conversation_item_added",
        ConversationItemAddedEvent(
            item=ChatMessage(role="assistant", content=["Next question"], interrupted=interrupted)
        ),
    )


async def test_forwarder_can_be_registered_before_job_room_connects():
    # Real Room.local_participant raises until connected. Worker startup
    # registers this helper before AgentSession.start connects the room.
    room = rtc.Room()
    session = AgentSession(vad=None)
    forwarder = UserTranscriptForwarder(session, room)
    await forwarder.aclose()


async def test_one_turn_id_across_sentences_and_authoritative_final_correction():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, room_for(send))
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
    forwarder = UserTranscriptForwarder(session, room_for(send))
    transcribe(session, "I used Git.")
    transcribe(session, "I used Git.")
    await flush()
    assert send.call_args.args == ("I used Git. I used Git.",)
    commit(session, "I used Git. I used Git.")
    await forwarder.aclose()


async def test_failed_publish_does_not_stop_later_final_or_log_candidate_text(caplog):
    session = AgentSession(vad=None)
    send = AsyncMock(side_effect=[RuntimeError("private candidate text"), None])
    forwarder = UserTranscriptForwarder(session, room_for(send))
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

    forwarder = UserTranscriptForwarder(session, room_for(send))
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
    old = UserTranscriptForwarder(session, room_for(send))
    commit(session, "Answer")
    await old.aclose()
    old_id = send.call_args.kwargs["attributes"]["lk.segment_id"]
    commit(session, "Ignored after close")
    await flush()
    assert send.await_count == 1
    new = UserTranscriptForwarder(session, room_for(send))
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

    forwarder = UserTranscriptForwarder(session, room_for(send))
    commit(session, "Answer")
    await started.wait()
    await forwarder.aclose()
    await forwarder.aclose()
    assert cancelled.is_set()
    assert forwarder._task.done()


async def test_assistant_boundary_preserves_orphan_and_starts_a_new_user_turn():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, room_for(send))
    transcribe(session, "Orphaned answer")
    await flush()
    agent_speaks(session)
    assistant_item(session)
    await flush()
    orphan = send.call_args
    assert orphan.args == ("Orphaned answer",)
    assert orphan.kwargs["attributes"]["interview.incomplete"] == "true"
    transcribe(session, "New answer")
    await flush()
    assert send.call_args.args == ("New answer",)
    commit(session, "New answer")
    await forwarder.aclose()
    assert (
        send.call_args.kwargs["attributes"]["lk.segment_id"]
        != (orphan.kwargs["attributes"]["lk.segment_id"])
    )


async def test_interrupted_assistant_item_keeps_the_user_turn_until_commit():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, room_for(send))
    transcribe(session, "A")
    transcribe(session, "B")
    agent_speaks(session)
    await flush()
    assistant_item(session, interrupted=True)
    await flush()
    transcribe(session, "C")
    await flush()
    commit(session, "A B C")
    await forwarder.aclose()
    assert len({c.kwargs["attributes"]["lk.segment_id"] for c in send.call_args_list}) == 1
    assert all(
        c.kwargs["attributes"]["interview.incomplete"] == "false" for c in send.call_args_list
    )
    assert send.call_args.args == ("A B C",)
    assert send.call_args.kwargs["attributes"]["lk.transcription_final"] == "true"


async def test_late_stt_during_agent_speech_does_not_create_an_orphan():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, room_for(send))
    transcribe(session, "A")
    agent_speaks(session)
    transcribe(session, "B")
    await flush()
    assistant_item(session)
    commit(session, "A B")
    await forwarder.aclose()
    assert len({c.kwargs["attributes"]["lk.segment_id"] for c in send.call_args_list}) == 1
    assert send.call_args.args == ("A B",)


async def test_audio_barge_in_before_stt_invalidates_orphan_boundary():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, room_for(send))
    transcribe(session, "A")
    await flush()
    agent_speaks(session)
    session.emit(
        "user_state_changed", UserStateChangedEvent(old_state="listening", new_state="speaking")
    )
    assistant_item(session)
    session.emit(
        "user_state_changed", UserStateChangedEvent(old_state="speaking", new_state="listening")
    )
    transcribe(session, "B")
    commit(session, "A B")
    await forwarder.aclose()
    assert len({c.kwargs["attributes"]["lk.segment_id"] for c in send.call_args_list}) == 1
    assert send.call_args.args == ("A B",)


async def test_assistant_item_without_speech_boundary_keeps_ambiguous_user_text():
    session = AgentSession(vad=None)
    send = AsyncMock()
    forwarder = UserTranscriptForwarder(session, room_for(send))
    transcribe(session, "A")
    await flush()
    assistant_item(session)
    transcribe(session, "B")
    commit(session, "A B")
    await forwarder.aclose()
    assert len({c.kwargs["attributes"]["lk.segment_id"] for c in send.call_args_list}) == 1
    assert send.call_args.args == ("A B",)


async def test_failed_final_is_retried_with_same_id_and_later_turn_is_not_lost():
    session = AgentSession(vad=None)
    send = AsyncMock(side_effect=[RuntimeError("lost final"), None, None])
    forwarder = UserTranscriptForwarder(session, room_for(send))
    commit(session, "First answer")
    commit(session, "Second answer")
    await forwarder.aclose()
    assert [c.args[0] for c in send.call_args_list] == [
        "First answer",
        "First answer",
        "Second answer",
    ]
    ids = [c.kwargs["attributes"]["lk.segment_id"] for c in send.call_args_list]
    assert ids[0] == ids[1]
    assert ids[1] != ids[2]

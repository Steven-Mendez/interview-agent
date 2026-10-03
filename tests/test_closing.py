"""Native speech closure, durable evidence and fencing with real PostgreSQL."""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents.llm import ChatMessage

from interview_agent.closing import FAREWELL_READY_METHOD, FAREWELLS, ClosingCoordinator
from interview_agent.config import settings
from interview_agent.interview import db
from interview_agent.playback import (
    PlaybackAck,
    acknowledge_playback,
    closing_state,
    record_agent_playout,
)
from interview_agent.server.reconciliation import reconcile_interview
from interview_agent.stt_drain import STTDrainReport


class Speech:
    def __init__(self, sessionmaker, conversation_id):
        self.id = "native-" + str(uuid.uuid4())
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.interrupted = False
        self.error = None
        self.audio_evidence = True
        self.chat_items = []
        self.text = ""
        self.before_persist = None
        self.sessionmaker = sessionmaker
        self.conversation_id = conversation_id
        self.interrupt = Mock(side_effect=self._interrupt)

    def _interrupt(self, *, force=False):
        self.interrupted = True
        self.release.set()

    def done(self):
        return self.finished

    def exception(self):
        return self.error

    async def wait_for_playout(self):
        await self.release.wait()
        if not self.interrupted and self.error is None:
            metrics = (
                {"started_speaking_at": 1.0, "stopped_speaking_at": 1.05}
                if self.audio_evidence
                else {}
            )
            item = ChatMessage(id=self.id, role="assistant", content=[self.text], metrics=metrics)
            self.chat_items.append(item)
            if self.before_persist is not None:
                await self.before_persist()
            # The worker's conversation_item_added listener owns persistence.
            async with self.sessionmaker() as session:
                await db.insert_message(
                    session,
                    self.conversation_id,
                    item.role,
                    item.text_content,
                    source_id=item.id,
                    metrics=metrics,
                )
        self.finished = True


async def create_conversation(sessionmaker):
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                status="interviewing",
                job_offer="Role",
                resume_markdown="CV",
                plan={"language": "es"},
            )
        )
        await session.commit()
    return conversation_id


def coordinator(sessionmaker, conversation_id, *, timeout=1, speech=None, seal=None):
    speech = speech or Speech(sessionmaker, conversation_id)
    calls = []

    def say(text, **kwargs):
        calls.append({"text": text, **kwargs})
        speech.text = text
        speech.started.set()
        return speech

    local = SimpleNamespace(
        publish_data=AsyncMock(),
        perform_rpc=AsyncMock(return_value='{"ready":true}'),
        stream_bytes=Mock(side_effect=AssertionError("No WAV transport")),
    )
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        current_speech=None,
        output=SimpleNamespace(audio_enabled=True, audio=object()),
        say=Mock(side_effect=say),
    )
    closing = ClosingCoordinator(
        room=SimpleNamespace(local_participant=local),
        session=session,
        sessionmaker=sessionmaker,
        conversation_id=conversation_id,
        telemetry=SimpleNamespace(emit=Mock()),
        language="es",
        timeout_seconds=timeout,
        drain_transcript=AsyncMock(),
        seal_transcript=seal,
    )
    speech.before_persist = closing.wait_for_stt_drain
    return closing, speech, calls


async def test_native_turn_before_seal_without_wav_or_fabricated_browser_ack(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    seal = AsyncMock()
    owner, speech, calls = coordinator(postgres_sessionmaker, conversation_id, seal=seal)
    task = asyncio.create_task(owner.finish("candidate_requested"))
    await asyncio.wait_for(speech.started.wait(), 1)
    assert calls == [
        {"text": FAREWELLS["es"], "allow_interruptions": False, "add_to_chat_ctx": True}
    ]
    assert not task.done()
    seal.assert_not_awaited()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "closing" and row.closing_audio_mime == "audio/rtc"
        assert row.closing_delivery_at is None and row.transcript_sealed_at is None
    speech.release.set()
    assert await task == "played"
    seal.assert_awaited_once()
    owner.room.local_participant.stream_bytes.assert_not_called()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "completed" and row.transcript_sealed_at is not None
        assert row.closing_ack_status is None and row.closing_ack_received_at is None
        assert row.closing_audio_size is None and row.closing_playback_seconds > 0
        assert row.closing_stream_id == speech.id
        messages = await db.get_messages(session, conversation_id)
        assert len(messages) == 1 and messages[0].source_id == speech.id
        assert messages[0].content == FAREWELLS["es"]
        assert owner.is_farewell_item(speech.chat_items[0])
    assert (await closing_state(postgres_sessionmaker, conversation_id))[
        "farewell_confirmation_source"
    ] == "agent_playout"
    owner.telemetry.emit.assert_any_call(
        "closing", "playback_confirmed", 1, dimensions={"confirmation_source": "agent_playout"}
    )


async def test_readiness_precedes_synthesis_and_cannot_confirm_playout(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    entered, ready = asyncio.Event(), asyncio.Event()

    async def gate(**kwargs):
        assert (
            kwargs["method"] == FAREWELL_READY_METHOD
            and kwargs["destination_identity"] == "candidate"
        )
        entered.set()
        await ready.wait()
        return '{"ready":true}'

    owner.room.local_participant.perform_rpc.side_effect = gate
    task = asyncio.create_task(owner.finish("timeout"))
    await entered.wait()
    owner.session.say.assert_not_called()
    ready.set()
    await speech.started.wait()
    assert not task.done() and owner.playback_status == "pending"
    speech.release.set()
    assert await task == "played"


@pytest.mark.parametrize("failure", ["error", "interrupted", "no_audio"])
async def test_finished_handle_is_not_sufficient_evidence(postgres_sessionmaker, failure):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    if failure == "error":
        speech.error = RuntimeError("Controlled synthesis failure")
    if failure == "interrupted":
        speech.interrupted = True
    if failure == "no_audio":
        speech.audio_evidence = False
    speech.release.set()
    assert await owner.finish("timeout") == "failed"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.farewell_status == "failed" and row.closing_delivery_at is None
        assert row.transcript_sealed_at is not None


@pytest.mark.parametrize("failure", ["not_ready", "unsupported", "audio_disabled"])
async def test_unavailable_native_playout_does_not_synthesize(postgres_sessionmaker, failure):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, calls = coordinator(postgres_sessionmaker, conversation_id)
    if failure == "not_ready":
        owner.room.local_participant.perform_rpc.return_value = '{"ready":false}'
    if failure == "unsupported":
        owner.room.local_participant.perform_rpc.side_effect = RuntimeError("Unsupported method")
    if failure == "audio_disabled":
        owner.session.output.audio_enabled = False
    assert await owner.finish("timeout") == (
        "failed" if failure == "unsupported" else "not_possible"
    )
    assert calls == []


@pytest.mark.parametrize("stage", ["ready", "speech"])
async def test_native_timeout_is_bounded_and_interrupts_unfinished_speech(
    postgres_sessionmaker, stage
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, calls = coordinator(postgres_sessionmaker, conversation_id, timeout=0.05)

    async def blocked_ready(**kwargs):
        await asyncio.Event().wait()

    if stage == "ready":
        owner.room.local_participant.perform_rpc.side_effect = blocked_ready
    started = time.monotonic()
    assert await asyncio.wait_for(owner.finish("timeout"), 1) == "timeout"
    assert time.monotonic() - started < 1
    if stage == "speech":
        speech.interrupt.assert_called_with(force=True)
    else:
        assert calls == []
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "completed" and row.closing_delivery_at is None


async def test_native_closure_rejects_old_browser_clip_acks(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    await owner._claim("timeout")
    ack = PlaybackAck(
        closing_id=owner.closing_id,
        stream_id=owner.stream_id,
        attempt_id=owner.attempt_id,
        status="played",
    )
    with pytest.raises(ValueError, match="Native speech"):
        await acknowledge_playback(postgres_sessionmaker, conversation_id, ack)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.farewell_status == "pending" and row.closing_ack_status is None


@pytest.mark.parametrize("invalid", ["owner", "attempt", "expired"])
async def test_native_playout_is_fenced(postgres_sessionmaker, invalid):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    await owner._claim("timeout")
    if invalid == "expired":
        async with postgres_sessionmaker() as session:
            row = await session.get(db.Conversation, conversation_id)
            row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
    assert not await record_agent_playout(
        postgres_sessionmaker,
        conversation_id,
        uuid.uuid4() if invalid == "owner" else owner.owner_id,
        owner.closing_id,
        uuid.uuid4() if invalid == "attempt" else owner.attempt_id,
        1,
        source_id="native-item",
    )


async def test_stt_drain_parallel_to_native_speech_before_seal(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    entered, release = asyncio.Event(), asyncio.Event()

    async def drain():
        entered.set()
        await release.wait()
        async with postgres_sessionmaker() as session:
            await db.insert_message(
                session,
                conversation_id,
                "user",
                "Final confirmed answer",
                source_id="last-answer",
                metrics={"stt_confirmed": True},
            )
        return STTDrainReport(True, 1, 2.5, 0, 1, 1)

    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    owner.end_stt_input = drain
    task = asyncio.create_task(owner.finish("candidate_requested"))
    await speech.started.wait()
    assert entered.is_set()
    speech.release.set()
    await asyncio.sleep(0.05)
    assert not task.done()
    async with postgres_sessionmaker() as session:
        assert (await session.get(db.Conversation, conversation_id)).transcript_sealed_at is None
    release.set()
    assert await task == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_integrity == "complete"
        assert [item.role for item in await db.get_messages(session, conversation_id)] == [
            "user",
            "assistant",
        ]


@pytest.mark.parametrize(
    "prior_metrics", [None, {}, {"stt_confirmed": False}, {"stt_confirmed": "true"}]
)
async def test_native_drain_cannot_erase_unconfirmed_prior_input(
    postgres_sessionmaker, prior_metrics
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await db.insert_message(
            session, conversation_id, "user", "Prior tail", source_id="prior", metrics=prior_metrics
        )
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    owner.end_stt_input = AsyncMock(return_value=STTDrainReport(True, 1, 2.5, 0, 1, 1))
    speech.release.set()
    assert await owner.finish("timeout") == "played"
    async with postgres_sessionmaker() as session:
        assert (
            await session.get(db.Conversation, conversation_id)
        ).transcript_integrity == "partial"


@pytest.mark.parametrize("outcome", ["unresolved", "missing", "error", "timeout"])
async def test_unverified_stt_drain_seals_partial(postgres_sessionmaker, outcome):
    conversation_id = await create_conversation(postgres_sessionmaker)

    async def drain():
        if outcome == "error":
            raise RuntimeError("Controlled drain failure")
        if outcome == "timeout":
            await asyncio.Event().wait()
        return STTDrainReport(True, 1, 2.5, 1, 0, 0)

    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    owner.stt_drain_seconds = 0.03
    if outcome != "missing":
        owner.end_stt_input = drain
    speech.release.set()
    assert await owner.finish("timeout") == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_integrity == "partial" and row.stt_drain["complete"] is False


async def test_long_question_is_interrupted_before_native_farewell(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    owner.session.current_speech = object()
    owner.session.interrupt = Mock(return_value=asyncio.sleep(0))
    task = asyncio.create_task(owner.finish("candidate_requested"))
    await speech.started.wait()
    owner.session.interrupt.assert_called_once_with(force=True)
    speech.release.set()
    assert await task == "played"


async def test_stalled_interrupt_never_starts_a_second_native_speech(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, calls = coordinator(postgres_sessionmaker, conversation_id, timeout=2)
    owner.session.current_speech = object()
    pending = asyncio.get_running_loop().create_future()
    owner.session.interrupt = Mock(return_value=pending)
    assert await asyncio.wait_for(owner.finish("timeout"), 2) == "timeout"
    assert pending.cancelled() and calls == []


async def test_two_owners_and_repeated_close_speak_once(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    a, sa, ca = coordinator(postgres_sessionmaker, conversation_id)
    b, sb, cb = coordinator(postgres_sessionmaker, conversation_id)
    sa.release.set()
    sb.release.set()
    assert await asyncio.gather(a.finish("timeout"), b.finish("timeout")) == ["played", "played"]
    assert await a.finish("timeout") == "played"
    assert len(ca) + len(cb) == 1
    async with postgres_sessionmaker() as session:
        assert len(await db.get_messages(session, conversation_id)) == 1


async def test_caller_cancellation_does_not_abandon_close_and_disconnect_aborts_speech(
    postgres_sessionmaker,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    task = asyncio.create_task(owner.finish("candidate_requested"))
    await speech.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not owner._finish_task.done()
    assert await owner.finish("candidate_left", say_goodbye=False) == "not_possible"
    speech.interrupt.assert_called_with(force=True)


async def test_native_playout_survives_crash_before_seal_without_replay(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    entered = asyncio.Event()

    async def seal():
        entered.set()
        await asyncio.Event().wait()

    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id, seal=seal)
    speech.release.set()
    task = asyncio.create_task(owner.finish("timeout"))
    await entered.wait()
    owner._finish_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    replacement, _, calls = coordinator(postgres_sessionmaker, conversation_id)
    assert await replacement.finish("connection_lost") == "played" and calls == []
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_integrity == "partial" and row.transcript_sealed_at is not None
        assert len(await db.get_messages(session, conversation_id)) == 1


async def test_old_wav_success_can_recover_without_new_audio_or_duplicate_message(
    postgres_sessionmaker,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    close, attempt, old_owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.status = "closing"
        row.closing_id, row.closing_attempt_id, row.closing_owner_id = close, attempt, old_owner
        row.closing_stream_id = "historical-stream"
        row.closing_audio_mime = "audio/wav"
        row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        row.farewell_status = "played"
        await session.commit()
    replacement, _, calls = coordinator(postgres_sessionmaker, conversation_id)
    assert await replacement.finish("connection_lost") == "played" and calls == []
    assert (await closing_state(postgres_sessionmaker, conversation_id))[
        "farewell_confirmation_source"
    ] == "browser_playback"
    async with postgres_sessionmaker() as session:
        assert len(await db.get_messages(session, conversation_id)) == 1


async def test_failed_transcript_persistence_prevents_completed_status(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(
        postgres_sessionmaker,
        conversation_id,
        seal=AsyncMock(side_effect=RuntimeError("Controlled failure")),
    )
    with pytest.raises(RuntimeError):
        await owner.finish("candidate_left", say_goodbye=False)
    async with postgres_sessionmaker() as session:
        assert (await session.get(db.Conversation, conversation_id)).status == "error"


async def test_expired_lease_cannot_seal_or_mark_failed(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    await owner._claim("timeout")
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(RuntimeError, match="ownership"):
        await owner._finalize()
    await owner._mark_finalize_error()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "closing" and row.transcript_sealed_at is None
        assert await reconcile_interview(session, conversation_id, settings)
        assert row.farewell_status == "timeout" and row.transcript_integrity == "partial"


async def test_claim_retry_after_transient_failure_and_late_dispatch_stays_terminal(
    postgres_sessionmaker, monkeypatch
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    original = owner._claim
    claim = AsyncMock(side_effect=[ConnectionError("Controlled failure"), "deliver"])
    monkeypatch.setattr(owner, "_claim", claim)
    with pytest.raises(ConnectionError):
        await owner.request_close("timeout")
    monkeypatch.setattr(owner, "_claim", original)
    speech.release.set()
    assert await owner.finish("timeout") == "played"
    async with postgres_sessionmaker() as session:
        assert await db.begin_interview(session, conversation_id) == "completed"


async def test_renewed_lease_must_still_hold_after_sealing_sdk(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)

    async def seal():
        async with postgres_sessionmaker() as session:
            row = await session.get(db.Conversation, conversation_id)
            row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id, seal=seal)
    await owner._claim("timeout")
    with pytest.raises(RuntimeError, match="while finalizing"):
        await owner._finalize()


async def test_capture_incident_remains_partial_with_successful_native_farewell(
    postgres_sessionmaker,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.capture_integrity_pending = True
        await session.commit()
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    owner.end_stt_input = AsyncMock(return_value=STTDrainReport(True, 1, 2.5, 0, 1, 1))
    speech.release.set()
    assert await owner.finish("timeout") == "played"
    async with postgres_sessionmaker() as session:
        assert (
            await session.get(db.Conversation, conversation_id)
        ).transcript_integrity == "partial"


@pytest.mark.parametrize("evidence", ["missing", "interrupted", "expired_audio", "sealed"])
async def test_playout_requires_a_persisted_complete_item_and_the_original_audio_budget(
    postgres_sessionmaker, evidence
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    await owner._claim("timeout")
    async with postgres_sessionmaker() as session:
        if evidence != "missing":
            await db.insert_message(
                session,
                conversation_id,
                "assistant",
                "Native farewell",
                source_id="native-item",
                interrupted=evidence == "interrupted",
            )
        row = await session.get(db.Conversation, conversation_id)
        if evidence == "expired_audio":
            row.closing_acquired_at = datetime.now(UTC) - timedelta(seconds=2)
        if evidence == "sealed":
            row.status = "completed"
            row.transcript_sealed_at = datetime.now(UTC)
        await session.commit()
    assert not await record_agent_playout(
        postgres_sessionmaker,
        conversation_id,
        owner.owner_id,
        owner.closing_id,
        owner.attempt_id,
        1,
        source_id="native-item",
    )


async def test_changed_closing_owner_prevents_old_speech_from_confirming_or_sealing(
    postgres_sessionmaker,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    finish = asyncio.create_task(owner.finish("timeout"))
    await speech.started.wait()
    new_owner = uuid.uuid4()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.closing_owner_id = new_owner
        await session.commit()
    speech.release.set()
    with pytest.raises(RuntimeError, match="ownership"):
        await finish
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.closing_owner_id == new_owner and row.farewell_status == "pending"
        assert row.transcript_sealed_at is None and row.closing_delivery_at is None


async def test_native_item_persistence_failure_cannot_be_confirmed_as_played(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, speech, _ = coordinator(postgres_sessionmaker, conversation_id)
    owner.drain_transcript = AsyncMock(side_effect=RuntimeError("Controlled persistence failure"))
    speech.release.set()
    with pytest.raises(RuntimeError):
        await owner.finish("timeout")
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "error" and row.farewell_status == "failed"
        assert row.closing_delivery_at is None and row.transcript_sealed_at is None


async def test_cancellation_resistant_readiness_cannot_start_speech_after_timeout(
    postgres_sessionmaker,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, calls = coordinator(postgres_sessionmaker, conversation_id, timeout=0.05)
    cancelled, release = asyncio.Event(), asyncio.Event()

    async def late_ready(**kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return '{"ready":true}'

    owner.room.local_participant.perform_rpc.side_effect = late_ready
    assert await asyncio.wait_for(owner.finish("timeout"), 1) == "timeout"
    await asyncio.wait_for(cancelled.wait(), 1)
    release.set()
    await asyncio.sleep(0.02)
    assert calls == []

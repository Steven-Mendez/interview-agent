"""Closing invariants with real PostgreSQL and controlled transport failures."""

import asyncio
import io
import json
import time
import uuid
import wave
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc
from sqlalchemy import event, func, select

from interview_agent import closing as closing_module
from interview_agent.closing import FAREWELLS, ClosingCoordinator, synthesize_farewell
from interview_agent.config import settings
from interview_agent.interview import db
from interview_agent.playback import (
    PlaybackAck,
    acknowledge_playback,
    record_delivery,
)
from interview_agent.server.reconciliation import reconcile_interview
from interview_agent.stt_drain import STTDrainReport


class SynthesizedClip:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def __aiter__(self):
        yield SimpleNamespace(
            frame=rtc.AudioFrame(
                data=b"\x00\x00" * 240,
                sample_rate=24000,
                num_channels=1,
                samples_per_channel=240,
            )
        )


class Writer:
    def __init__(self):
        self.write_started = asyncio.Event()
        self.sent = asyncio.Event()
        self.write_gate = asyncio.Event()
        self.write_gate.set()
        self.close_gate = asyncio.Event()
        self.close_gate.set()
        self.data = b""

    async def write(self, data):
        self.write_started.set()
        await self.write_gate.wait()
        self.data += data

    async def aclose(self):
        self.sent.set()
        await self.close_gate.wait()


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


def coordinator(sessionmaker, conversation_id, *, timeout=1, writer=None, seal=None):
    writer = writer or Writer()
    streams = []

    async def stream_bytes(name, **kwargs):
        streams.append({"name": name, **kwargs})
        return writer

    local = SimpleNamespace(
        stream_bytes=stream_bytes,
        publish_data=AsyncMock(),
    )
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        current_speech=None,
        tts=SimpleNamespace(synthesize=lambda text: SynthesizedClip()),
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
    return closing, writer, streams


def ack(closing, **updates):
    return json.dumps(
        {
            "closing_id": str(closing.closing_id),
            "stream_id": closing.stream_id,
            "attempt_id": str(closing.attempt_id),
            "status": "played",
            "duration_seconds": 0.01,
            **updates,
        }
    )


async def test_explicit_stt_drain_runs_parallel_to_farewell_before_complete_seal(
    postgres_sessionmaker,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    started, release = asyncio.Event(), asyncio.Event()

    async def final_input():
        started.set()
        await release.wait()
        async with postgres_sessionmaker() as session:
            await db.insert_message(
                session,
                conversation_id,
                "user",
                "The final answer tail.",
                source_id="late-final",
                metrics={"stt_confirmed": True},
            )
        return STTDrainReport(True, 1, 2.5, 0, 1, 1)

    closing, writer, _ = coordinator(postgres_sessionmaker, conversation_id)
    closing.end_stt_input = final_input
    finish = asyncio.create_task(closing.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1)
    assert started.is_set()
    await closing.acknowledge("candidate", ack(closing))
    assert not finish.done()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_sealed_at is None
    release.set()
    assert await finish == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_integrity == "complete" and row.stt_drain["complete"] is True
        messages = await db.get_messages(session, conversation_id)
        assert any(m.source_id == "late-final" for m in messages)


@pytest.mark.parametrize(
    "prior_metrics",
    [
        {"stt_confirmed": False},
        None,
        {},
        {"stt_confirmed": "unknown"},
        {"stt_confirmed": "true"},
        {"stt_confirmed": 1},
        {"stt_confirmed": {"reason": "pending"}},
    ],
)
async def test_replacement_worker_drain_cannot_erase_persisted_unconfirmed_answer(
    postgres_sessionmaker,
    prior_metrics,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        await db.insert_message(
            session,
            conversation_id,
            "user",
            "Tail from the prior worker",
            source_id="prior-worker-interim",
            metrics=prior_metrics,
        )
    closing, writer, _ = coordinator(postgres_sessionmaker, conversation_id)
    closing.end_stt_input = AsyncMock(return_value=STTDrainReport(True, 1, 2.5, 0, 1, 1))
    finish = asyncio.create_task(closing.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1)
    await closing.acknowledge("candidate", ack(closing))
    assert await finish == "played" and not closing._recovered
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_integrity == "partial"
        assert row.stt_drain["complete"] is True  # Current transport did drain.
        assert row.stt_drain["canonical_unconfirmed_user_messages"] == 1


@pytest.mark.parametrize("outcome", ["unresolved", "missing", "error", "timeout"])
async def test_unverified_stt_drain_seals_partial_without_fabricating_completion(
    postgres_sessionmaker, outcome
):
    conversation_id = await create_conversation(postgres_sessionmaker)

    async def final_input():
        if outcome == "error":
            raise RuntimeError("controlled input failure")
        if outcome == "timeout":
            await asyncio.Event().wait()
        return STTDrainReport(True, 1, 2.5, 1, 0, 0)

    closing, writer, _ = coordinator(postgres_sessionmaker, conversation_id)
    closing.stt_drain_seconds = 0.03
    if outcome != "missing":
        closing.end_stt_input = final_input
    finish = asyncio.create_task(closing.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1)
    await closing.acknowledge("candidate", ack(closing))
    assert await asyncio.wait_for(finish, 1) == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "completed"
        assert row.transcript_integrity == "partial"
        assert row.stt_drain["complete"] is False


async def test_synthesis_is_a_finite_valid_pcm_wav():
    audio = await synthesize_farewell(
        SimpleNamespace(synthesize=lambda text: SynthesizedClip()),
        "es",
    )
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 240


async def test_farewell_paces_complete_chunks_before_delivery_and_footer(
    postgres_sessionmaker, monkeypatch
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    closing, writer, streams = coordinator(postgres_sessionmaker, conversation_id, timeout=3)
    audio = bytes(range(256)) * 1300
    closing._audio_task = asyncio.create_task(asyncio.sleep(0, result=audio))
    writes = []
    opened = []
    original_open = closing.room.local_participant.stream_bytes
    original_write = writer.write

    async def stream_bytes(*args, **kwargs):
        result = await original_open(*args, **kwargs)
        opened.append(time.monotonic())
        return result

    async def write(chunk):
        # A single large FFI write bursts all native chunks without yielding.
        assert len(chunk) <= 15_000
        writes.append((time.monotonic(), bytes(chunk)))
        async with postgres_sessionmaker() as session:
            row = await session.get(db.Conversation, conversation_id)
            assert row.closing_delivery_at is None
            assert row.transcript_sealed_at is None
        await original_write(chunk)

    monkeypatch.setattr(writer, "write", write)
    monkeypatch.setattr(closing.room.local_participant, "stream_bytes", stream_bytes)
    finish = asyncio.create_task(closing.finish("timeout"))
    await asyncio.wait_for(writer.sent.wait(), 2)
    assert writer.data == audio
    assert streams[0]["total_size"] == len(audio)
    assert len(writes) == 23
    assert writes[0][0] - opened[0] >= 0.04
    assert all(b[0] - a[0] >= 0.04 for a, b in pairwise(writes))
    assert not finish.done()
    await closing.acknowledge("candidate", ack(closing))
    assert await finish == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.closing_audio_size == len(audio)
        assert row.transcript_sealed_at is not None


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_partial_chunk_delivery_never_proves_playback(postgres_sessionmaker, failure):
    conversation_id = await create_conversation(postgres_sessionmaker)
    writer = Writer()
    closing, _, _ = coordinator(postgres_sessionmaker, conversation_id, timeout=0.2, writer=writer)
    audio = b"\x00\x01" * 20_000
    closing._audio_task = asyncio.create_task(asyncio.sleep(0, result=audio))
    original_write = writer.write

    async def write(chunk):
        if writer.data:
            if failure == "error":
                raise ConnectionError("Controlled transport failure")
            await asyncio.Event().wait()
        await original_write(chunk)

    writer.write = write
    expected = "failed" if failure == "error" else "timeout"
    assert await asyncio.wait_for(closing.finish("timeout"), 1) == expected
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.farewell_status == expected
        assert row.closing_delivery_at is None
        assert row.transcript_sealed_at is not None
        assert await db.get_messages(session, conversation_id) == []
    assert 0 < len(writer.data) < len(audio)


async def test_playback_then_seal_then_complete(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)

    async def seal():
        async with postgres_sessionmaker() as session:
            await db.insert_message(
                session,
                conversation_id,
                "user",
                "Last confirmed answer",
                source_id="final-candidate-turn",
            )

    closing, writer, streams = coordinator(
        postgres_sessionmaker,
        conversation_id,
        seal=seal,
    )
    task = asyncio.create_task(closing.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1)
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        assert conversation.status == "closing"
        assert conversation.closing_owner_id == closing.owner_id
        assert await db.get_messages(session, conversation_id) == []
    assert not task.done()
    assert streams[0]["mime_type"] == "audio/wav"
    assert streams[0]["stream_id"] == closing.stream_id
    assert streams[0]["attributes"]["attempt_id"] == str(closing.attempt_id)
    result = await closing.acknowledge("candidate", ack(closing))
    assert json.loads(result)["accepted"]
    assert await task == "played"
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        assert conversation.status == "completed"
        assert conversation.farewell_status == "played"
        messages = await db.get_messages(session, conversation_id)
        assert [m.content for m in messages] == ["Last confirmed answer", FAREWELLS["es"]]


async def test_ack_cannot_confirm_blocked_write(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    writer = Writer()
    writer.write_gate.clear()
    closing, _, _ = coordinator(postgres_sessionmaker, conversation_id, writer=writer)
    task = asyncio.create_task(closing.finish("plan_complete"))
    await asyncio.wait_for(writer.write_started.wait(), 1)
    provisional = json.loads(await closing.acknowledge("candidate", ack(closing)))
    assert provisional["accepted"] and provisional["provisional"]
    assert not closing.playback.is_set()
    with pytest.raises(ValueError):
        await closing.acknowledge("candidate", ack(closing, stream_id="wrong"))
    with pytest.raises(ValueError):
        await closing.acknowledge("stranger", ack(closing))
    writer.write_gate.set()
    await writer.sent.wait()
    # Full delivery promotes the received ACK without a second RPC.
    assert await task == "played"


async def test_timeout_survives_blocked_write_and_close(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    writer = Writer()
    writer.write_gate.clear()
    writer.close_gate.clear()
    closing, _, _ = coordinator(
        postgres_sessionmaker,
        conversation_id,
        timeout=0.03,
        writer=writer,
    )
    started = time.monotonic()
    assert await asyncio.wait_for(closing.finish("timeout"), 0.4) == "timeout"
    assert time.monotonic() - started < 0.4
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        assert conversation.status == "completed"
        assert conversation.farewell_status == "timeout"
    writer.close_gate.set()
    await asyncio.sleep(0.05)


async def test_played_ack_survives_a_blocked_footer_callback(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    writer = Writer()
    writer.close_gate.clear()
    closing, _, _ = coordinator(postgres_sessionmaker, conversation_id, timeout=0.1, writer=writer)
    task = asyncio.create_task(closing.finish("plan_complete"))
    await writer.sent.wait()
    await closing.acknowledge("candidate", ack(closing))
    assert await asyncio.wait_for(task, 0.3) == "played"
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        assert conversation.farewell_status == "played"
    writer.close_gate.set()


async def test_played_ack_survives_owner_crash_before_transcript_seal(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    seal_started = asyncio.Event()
    seal_gate = asyncio.Event()

    async def blocked_seal():
        seal_started.set()
        await seal_gate.wait()

    first, writer, _ = coordinator(postgres_sessionmaker, conversation_id, seal=blocked_seal)
    waiting = asyncio.create_task(first.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1)
    await first.acknowledge("candidate", ack(first))
    await asyncio.wait_for(seal_started.wait(), 1)
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        assert row.status == "closing" and row.farewell_status == "played"
        previous_stream, previous_attempt = row.closing_stream_id, row.closing_attempt_id
    # Terminate the canonical owner, rather than merely a shielded caller.
    first._finish_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    replacement, _, streams = coordinator(postgres_sessionmaker, conversation_id)
    assert await replacement.finish("connection_lost") == "played"
    assert streams == []
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        assert row.status == "completed" and row.transcript_integrity == "partial"
        assert row.farewell_status == "played"
        assert (row.closing_stream_id, row.closing_attempt_id) == (
            previous_stream,
            previous_attempt,
        )
        assert len(await db.get_messages(session, conversation_id)) == 1


async def test_expired_owner_cannot_renew_but_ack_uses_initial_validation_window(
    postgres_sessionmaker,
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    assert await owner._claim("timeout") == "deliver"
    owner._delivery_ready = True
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    received = json.loads(await owner.acknowledge("candidate", ack(owner)))
    assert received["accepted"] and received["provisional"]
    assert not owner.playback.is_set()
    with pytest.raises(RuntimeError, match="ownership"):
        await owner._finalize()


async def test_initial_lease_covers_audio_finalization_and_margin(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id, timeout=20)
    await owner._claim("candidate_requested")
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        assert (row.closing_deadline_at - row.closing_started_at).total_seconds() == 35


async def test_api_ack_watcher_bounds_database_polling_load(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    await owner._claim("candidate_requested")
    engine = postgres_sessionmaker.kw["bind"].sync_engine
    reads = []

    def query(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("SELECT") and "FROM conversations" in statement:
            reads.append(statement)

    event.listen(engine, "before_cursor_execute", query)
    task = asyncio.create_task(owner._watch_playback())
    try:
        await asyncio.sleep(0.55)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", query)
    assert 1 <= len(reads) <= 4
    assert not owner.playback.is_set()


async def test_end_during_long_question_interrupts_it_before_farewell(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, writer, _ = coordinator(postgres_sessionmaker, conversation_id)
    playout = AsyncMock(side_effect=AssertionError("Do not wait for the long question"))
    owner.session.current_speech = SimpleNamespace(wait_for_playout=playout)
    owner.session.interrupt = Mock(return_value=asyncio.sleep(0))
    task = asyncio.create_task(owner.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1)
    owner.session.interrupt.assert_called_once_with(force=True)
    playout.assert_not_called()
    await owner.acknowledge("candidate", ack(owner))
    assert await asyncio.wait_for(task, 1) == "played"


async def test_slow_interrupt_callback_does_not_suppress_separate_farewell(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, writer, _ = coordinator(postgres_sessionmaker, conversation_id, timeout=3)
    owner.session.current_speech = SimpleNamespace(wait_for_playout=AsyncMock())
    pending = asyncio.get_running_loop().create_future()
    owner.session.interrupt = Mock(return_value=pending)
    task = asyncio.create_task(owner.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1.8)
    assert pending.cancelled()
    owner.telemetry.emit.assert_any_call(
        "closing", "speech_interrupt_errors", 1, dimensions={"error_type": "TimeoutError"}
    )
    await owner.acknowledge("candidate", ack(owner))
    assert await asyncio.wait_for(task, 1) == "played"


@pytest.mark.parametrize("status", ["played", "failed"])
@pytest.mark.parametrize("finalization_error", [False, True])
async def test_durable_ack_is_authoritative_after_rpc_cancellation(
    postgres_sessionmaker, status, finalization_error
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    await owner._claim("candidate_requested")
    await record_delivery(
        postgres_sessionmaker,
        conversation_id,
        owner.owner_id,
        owner.closing_id,
        owner.stream_id,
        owner.attempt_id,
        524,
        "audio/wav",
    )
    received, _first = await acknowledge_playback(
        postgres_sessionmaker,
        conversation_id,
        PlaybackAck.model_validate_json(ack(owner, status=status)),
    )
    assert received["status"] == status
    # The RPC died between the storage commit and waking the audio phase.
    owner.playback_status = "timeout"
    if finalization_error:
        await owner._mark_finalize_error()
    else:
        await owner._finalize()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        assert row.farewell_status == status
        assert row.status == ("error" if finalization_error else "completed")


async def test_owner_cannot_seal_after_renewed_lease_expires(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)

    async def delayed_seal():
        # Simulate a drain that returned after the renewed lease had expired.
        async with postgres_sessionmaker() as session:
            row = await db.get_conversation(session, conversation_id)
            row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id, seal=delayed_seal)
    await owner._claim("candidate_requested")
    with pytest.raises(RuntimeError, match="while finalizing"):
        await owner._finalize()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        assert row.status == "closing" and row.transcript_sealed_at is None


async def test_two_process_owners_send_exactly_one_clip(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    a, wa, sa = coordinator(postgres_sessionmaker, conversation_id)
    b, wb, sb = coordinator(postgres_sessionmaker, conversation_id)
    tasks = [asyncio.create_task(c.finish("plan_complete")) for c in (a, b)]
    waiters = [asyncio.create_task(w.sent.wait()) for w in (wa, wb)]
    done, pending = await asyncio.wait(
        waiters,
        timeout=1,
        return_when=asyncio.FIRST_COMPLETED,
    )
    assert done
    for task in pending:
        task.cancel()
    owner = a if a.owns_closure else b
    await owner.acknowledge("candidate", ack(owner))
    assert await asyncio.gather(*tasks) == ["played", "played"]
    assert len(sa) + len(sb) == 1
    async with postgres_sessionmaker() as session:
        messages = await db.get_messages(session, conversation_id)
        assert len(messages) == 1


async def test_watcher_cancel_does_not_abandon_canonical_close(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    closing, writer, _ = coordinator(postgres_sessionmaker, conversation_id)
    watcher = asyncio.create_task(closing.finish("plan_complete"))
    await writer.sent.wait()
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert not closing._finish_task.done()
    assert await closing.finish("candidate_left", say_goodbye=False) == "not_possible"
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        assert conversation.status == "completed"
        assert conversation.ended_reason == "plan_complete"


async def test_stale_owner_recovers_without_replaying_audio(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        conversation.status = "closing"
        conversation.closing_id = uuid.uuid4()
        conversation.closing_owner_id = uuid.uuid4()
        conversation.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        conversation.ended_reason = "timeout"
        await session.commit()
    closing, _, streams = coordinator(postgres_sessionmaker, conversation_id)
    assert await closing.finish("connection_lost") == "timeout"
    assert streams == []


async def test_transcript_persistence_failure_blocks_evaluation(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    closing, _, _ = coordinator(
        postgres_sessionmaker,
        conversation_id,
        seal=AsyncMock(side_effect=RuntimeError("Persistence failed")),
    )
    with pytest.raises(RuntimeError):
        await closing.finish("candidate_left", say_goodbye=False)
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        assert conversation.status == "error"


async def test_late_dispatch_cannot_reopen_completed(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        conversation.status = "completed"
        await session.commit()
        assert await db.begin_interview(session, conversation_id) == "completed"


async def test_expiry_only_recovers_expired_closing_lease(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        conversation.status = "closing"
        conversation.closing_id = uuid.uuid4()
        conversation.closing_owner_id = uuid.uuid4()
        conversation.closing_deadline_at = datetime.now(UTC) + timedelta(seconds=5)
        await session.commit()
        assert not await reconcile_interview(session, conversation_id, settings)
        conversation.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert await reconcile_interview(session, conversation_id, settings)
        assert conversation.status == "completed"
        assert conversation.farewell_status == "timeout"
        assert conversation.transcript_integrity == "partial"
        with pytest.raises(ValueError, match="sealed"):
            await db.insert_message(session, conversation_id, "user", "Late committed answer")


@pytest.mark.parametrize("skew", [-120, 120])
async def test_closing_uses_database_clock_despite_worker_and_api_skew(
    postgres_sessionmaker, monkeypatch, skew
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id, timeout=20)
    await owner._claim("candidate_requested")

    class SkewedDateTime:
        @classmethod
        def now(cls, tz):
            return datetime.now(tz) + timedelta(seconds=skew)

    monkeypatch.setattr(closing_module, "datetime", SkewedDateTime, raising=False)
    monkeypatch.setattr(db, "datetime", SkewedDateTime)
    async with postgres_sessionmaker() as session:
        before = await session.scalar(select(func.clock_timestamp()))
        assert not await reconcile_interview(session, conversation_id, settings)
    await owner._finalize()
    async with postgres_sessionmaker() as session:
        after = await session.scalar(select(func.clock_timestamp()))
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "completed"
        assert before <= row.transcript_sealed_at <= after
        assert row.closing_deadline_at == row.closing_ack_deadline_at


@pytest.mark.parametrize("skew", [-120, 120])
async def test_expired_owner_cannot_write_error_even_if_process_clock_is_behind(
    postgres_sessionmaker, monkeypatch, skew
):
    conversation_id = await create_conversation(postgres_sessionmaker)
    owner, _, _ = coordinator(postgres_sessionmaker, conversation_id)
    await owner._claim("candidate_requested")
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.closing_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    class SkewedDateTime:
        @classmethod
        def now(cls, tz):
            return datetime.now(tz) + timedelta(seconds=skew)

    monkeypatch.setattr(closing_module, "datetime", SkewedDateTime, raising=False)
    monkeypatch.setattr(db, "datetime", SkewedDateTime)
    await owner._mark_finalize_error()
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.status == "closing" and row.transcript_sealed_at is None
        assert await reconcile_interview(session, conversation_id, settings)
        assert row.status == "completed" and row.transcript_integrity == "partial"


async def test_close_can_retry_after_transient_claim_failure(postgres_sessionmaker, monkeypatch):
    conversation_id = await create_conversation(postgres_sessionmaker)
    closing, writer, _ = coordinator(postgres_sessionmaker, conversation_id)
    original_claim = closing._claim
    calls = 0

    async def claim(reason):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("synthetic temporary outage")
        return await original_claim(reason)

    monkeypatch.setattr(closing, "_claim", claim)
    with pytest.raises(ConnectionError):
        await closing.request_close("candidate_requested")
    await closing.request_close("candidate_requested")
    task = asyncio.create_task(closing.finish("candidate_requested"))
    await writer.sent.wait()
    await closing.acknowledge("candidate", ack(closing))
    assert await task == "played"
    assert calls == 2


async def test_successful_drain_cannot_seal_capture_incident_as_complete(postgres_sessionmaker):
    conversation_id = await create_conversation(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        row.capture_integrity_pending = True
        await session.commit()
    closing, writer, _ = coordinator(postgres_sessionmaker, conversation_id)
    closing.end_stt_input = AsyncMock(return_value=STTDrainReport(True, 1, 2.5, 0, 1, 1))
    finish = asyncio.create_task(closing.finish("candidate_requested"))
    await asyncio.wait_for(writer.sent.wait(), 1)
    await closing.acknowledge("candidate", ack(closing))
    assert await finish == "played"
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, conversation_id)
        assert row.transcript_integrity == "partial" and row.capture_integrity_pending

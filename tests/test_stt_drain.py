"""Final-input integrity at the real installed SDK/WebSocket boundary."""

import asyncio
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from livekit import rtc
from livekit.agents import AgentSession, stt
from livekit.agents.llm import ChatMessage
from livekit.agents.types import APIConnectOptions
from livekit.agents.voice.audio_recognition import _EndOfTurnInfo, _EndOfTurnMetrics

from interview_agent import stt_drain
from interview_agent.agent import InterviewAgent
from interview_agent.stt_drain import DrainableInferenceSTT


@asynccontextmanager
async def gateway(mode="complete", *, gate=None):
    received = []
    closing = asyncio.Event()

    async def handle(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            data = message.json()
            received.append(data["type"])
            if data["type"] == "session.create":
                await ws.send_json({"type": "session.created"})
            elif data["type"] == "session.finalize":
                # This ACK is deliberately earlier than the trailing final.
                await ws.send_json({"type": "session.finalized"})
            elif data["type"] == "session.close":
                closing.set()
                if gate:
                    await gate.wait()
                if mode == "interim_only":
                    await ws.send_json({"type": "interim_transcript", "transcript": "tail"})
                    await ws.send_json({"type": "final_transcript", "transcript": ""})
                else:
                    for text in ("first", "late tail"):
                        await ws.send_json(
                            {"type": "final_transcript", "transcript": text, "duration": 0.02}
                        )
                if mode in ("complete", "interim_only"):
                    await ws.send_json({"type": "session.closed"})
                elif mode == "socket_close":
                    await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/stt", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    async with aiohttp.ClientSession() as http:
        model = DrainableInferenceSTT(
            model="assemblyai/universal-3-6-pro",
            language="en",
            base_url=f"http://127.0.0.1:{port}",
            api_key="local-test-key",
            api_secret="local-test-secret-at-least-32-characters",
            http_session=http,
        )
        try:
            yield model, received, closing
        finally:
            if gate:
                gate.set()
            await model.aclose()
    await runner.cleanup()


def frame():
    return rtc.AudioFrame(
        data=b"\x00\x00" * 320,
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=320,
    )


async def consume(stream, events, *, pause=None, paused=None):
    async for event in stream:
        events.append(event)
        if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT and pause:
            paused.set()
            await pause.wait()


async def test_explicit_end_flushes_tail_in_order_and_rejects_audio_after_cut():
    gate = asyncio.Event()
    async with (
        gateway(gate=gate) as (model, wire, close_requested),
        model.stream(conn_options=APIConnectOptions(max_retry=0)) as stream,
    ):
        events = []
        receiver = asyncio.create_task(consume(stream, events))
        stream.push_frame(frame())
        drain = asyncio.create_task(model.end_input_and_drain(1))
        await asyncio.wait_for(close_requested.wait(), 1)
        stream.push_frame(frame())  # Arrived after the admitted-audio boundary.
        assert not drain.done()
        gate.set()
        report = await drain
        await receiver
        assert report.complete
        assert report.admitted_audio_seconds == pytest.approx(0.02)
        assert report.provider_closed_streams == report.consumed_streams == 1
        assert [e.alternatives[0].text for e in events if e.alternatives] == [
            "first",
            "late tail",
        ]
        assert wire == ["session.create", "input_audio", "session.finalize", "session.close"]
        with pytest.raises(RuntimeError, match="replacement stream"):
            model.stream()


@pytest.mark.parametrize("mode", ["ack_only", "socket_close", "interim_only"])
async def test_ack_socket_eof_or_unconfirmed_interim_cannot_prove_full_recognition(mode):
    async with (
        gateway(mode) as (model, _, _),
        model.stream(conn_options=APIConnectOptions(max_retry=0)) as stream,
    ):
        events = []
        receiver = asyncio.create_task(consume(stream, events))
        stream.push_frame(frame())
        report = await model.end_input_and_drain(0.05)
        assert not report.complete
        assert report.unresolved_streams == 1
        if mode == "ack_only":
            assert report.provider_closed_streams == report.consumed_streams == 0
        elif mode == "socket_close":
            assert report.provider_closed_streams == 0
            assert report.consumed_streams == 1
        else:
            assert report.provider_closed_streams == report.consumed_streams == 1
        await stream.aclose()
        await receiver


async def test_provider_barrier_waits_until_consumer_has_processed_all_final_events():
    pause, paused = asyncio.Event(), asyncio.Event()
    async with (
        gateway() as (model, _, _),
        model.stream(conn_options=APIConnectOptions(max_retry=0)) as stream,
    ):
        events = []
        receiver = asyncio.create_task(consume(stream, events, pause=pause, paused=paused))
        stream.push_frame(frame())
        drain = asyncio.create_task(model.end_input_and_drain(1))
        await asyncio.wait_for(paused.wait(), 1)
        assert not drain.done()
        pause.set()
        report = await drain
        await receiver
        assert report.complete
        assert len([e for e in events if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]) == 2


async def test_no_started_stream_is_unknown_rather_than_a_complete_drain():
    async with gateway() as (model, _, _):
        report = await model.end_input_and_drain(0.05)
        assert report.streams == 0
        assert not report.complete


def test_unsupported_sdk_fails_visibly_instead_of_claiming_compatible_drain(monkeypatch):
    monkeypatch.setattr(stt_drain, "version", lambda name: "1.9.0")
    with pytest.raises(RuntimeError, match="needs verification"):
        DrainableInferenceSTT(model="assemblyai/universal-3-6-pro")


async def wait_boundary(model):
    async with asyncio.timeout(1):
        while model.session_boundary is None:
            await asyncio.sleep(0)
    return model.session_boundary


async def test_session_cut_drains_audio_queued_before_default_node_forwarder(monkeypatch):
    release, waiting = asyncio.Event(), asyncio.Event()
    original = stt_drain.SessionSTTBoundary.forward_audio

    async def delayed_audio(self, audio):
        waiting.set()
        await release.wait()
        async for sample in original(self, audio):
            yield sample

    monkeypatch.setattr(stt_drain.SessionSTTBoundary, "forward_audio", delayed_audio)
    async with gateway() as (model, wire, close_requested):
        session = AgentSession(
            stt=model,
            turn_handling={"turn_detection": "manual"},
            session_close_transcript_timeout=0,
        )
        items = []
        session.on(
            "conversation_item_added",
            lambda event: items.append(event.item) if isinstance(event.item, ChatMessage) else None,
        )
        await session.start(InterviewAgent(instructions="Interview"), record=False)
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            boundary = await wait_boundary(model)
            boundary.recognition._push_audio(frame())  # Accepted, still in SDK audio_ch.
            drain = asyncio.create_task(boundary.drain(1))
            await asyncio.sleep(0)
            assert boundary.cut and not drain.done()
            assert not close_requested.is_set()
            boundary.recognition._push_audio(frame())  # Beyond the session cut.
            release.set()
            report = await drain
            assert report.complete and report.upstream_input_drained
            assert report.recognition_consumed
            assert report.admission_boundary == "session_recognition"
            assert report.admitted_audio_seconds == pytest.approx(0.02)
            assert wire.index("input_audio") < wire.index("session.finalize")
            await session.commit_user_turn(
                transcript_timeout=0, stt_flush_duration=0, skip_reply=True
            )
        finally:
            release.set()
            await session.aclose()
        user = [item for item in items if item.role == "user"]
        assert [item.text_content for item in user] == ["first late tail"]
        assert user[0].metrics["stt_confirmed"] is True
        assert len(user[0].metrics["stt_segments"]) == 2


async def test_provider_eof_waits_for_sdk_recognition_consumer(monkeypatch):
    paused, release = asyncio.Event(), asyncio.Event()
    async with gateway() as (model, _, _):
        session = AgentSession(
            stt=model,
            turn_handling={"turn_detection": "manual"},
            session_close_transcript_timeout=0,
        )
        await session.start(InterviewAgent(instructions="Interview"), record=False)
        boundary = await wait_boundary(model)
        original = boundary.recognition._on_stt_event

        async def delayed(event):
            if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                paused.set()
                await release.wait()
            await original(event)

        monkeypatch.setattr(boundary.recognition, "_on_stt_event", delayed)
        try:
            boundary.recognition._push_audio(frame())
            drain = asyncio.create_task(boundary.drain(1))
            await asyncio.wait_for(paused.wait(), 1)
            assert not drain.done()
            release.set()
            report = await drain
            assert report.complete and report.recognition_consumed
        finally:
            release.set()
            await session.aclose()


@pytest.mark.parametrize("blocked_stage", ["upstream_audio", "recognition_consumer"])
async def test_session_drain_timeout_keeps_unproven_boundary_partial(monkeypatch, blocked_stage):
    paused, release = asyncio.Event(), asyncio.Event()
    original_audio = stt_drain.SessionSTTBoundary.forward_audio
    if blocked_stage == "upstream_audio":

        async def blocked_audio(self, audio):
            paused.set()
            await release.wait()
            async for sample in original_audio(self, audio):
                yield sample

        monkeypatch.setattr(stt_drain.SessionSTTBoundary, "forward_audio", blocked_audio)
    async with gateway() as (model, _, _):
        session = AgentSession(
            stt=model,
            turn_handling={"turn_detection": "manual"},
            session_close_transcript_timeout=0,
        )
        await session.start(InterviewAgent(instructions="Interview"), record=False)
        boundary = await wait_boundary(model)
        if blocked_stage == "recognition_consumer":
            original_event = boundary.recognition._on_stt_event

            async def blocked_event(event):
                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    paused.set()
                    await release.wait()
                await original_event(event)

            monkeypatch.setattr(boundary.recognition, "_on_stt_event", blocked_event)
        try:
            boundary.recognition._push_audio(frame())
            drain = asyncio.create_task(boundary.drain(0.03))
            await asyncio.wait_for(paused.wait(), 1)
            report = await asyncio.wait_for(drain, 1)
            assert not report.complete
            if blocked_stage == "upstream_audio":
                assert not report.upstream_input_drained
            else:
                assert report.upstream_input_drained
                assert not report.recognition_consumed
                assert report.provider_closed_streams == 1
        finally:
            release.set()
            await session.aclose()


async def test_delayed_items_keep_their_own_final_or_promoted_interim_provenance():
    async with gateway() as (model, _, _):
        session = AgentSession(
            stt=model,
            turn_handling={"turn_detection": "manual"},
            session_close_transcript_timeout=0,
        )
        items = []
        session.on(
            "conversation_item_added",
            lambda event: items.append(event.item) if isinstance(event.item, ChatMessage) else None,
        )
        await session.start(InterviewAgent(instructions="Interview"), record=False)
        boundary = await wait_boundary(model)
        activity = boundary.activity
        recognition = boundary.recognition
        blocked, release = asyncio.Event(), asyncio.Event()

        async def previous_turn():
            blocked.set()
            await release.wait()

        old_task = asyncio.create_task(previous_turn())
        activity._user_turn_completed_atask = old_task
        await blocked.wait()

        def final(text):
            recognition._process_stt_event(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language="en", text=text)],
                )
            )

        def eot(text):
            info = _EndOfTurnInfo(
                skip_reply=True,
                new_transcript=text,
                transcript_confidence=1,
                metrics=_EndOfTurnMetrics(None, None, None, None),
            )
            assert activity.on_end_of_turn(info)
            recognition._audio_transcript = ""  # SDK resets after EOT accepts.

        try:
            final("confirmed A")
            eot("confirmed A")
            # A subsequent interim does not invalidate A's delayed message.
            recognition._process_stt_event(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language="en", text="promoted B")],
                )
            )
            # The SDK's timeout promotion bypasses _process_stt_event.
            recognition._audio_transcript = "promoted B"
            eot("promoted B")
            final("confirmed C")  # Cannot retroactively confirm B.
            eot("confirmed C")
            assert not items
            release.set()
            await asyncio.wait_for(activity._user_turn_completed_atask, 1)
            assert [item.text_content for item in items] == [
                "confirmed A",
                "promoted B",
                "confirmed C",
            ]
            assert [item.metrics["stt_confirmed"] for item in items] == [True, False, True]
            assert len({item.metrics["stt_turn_id"] for item in items}) == 3
            assert [len(item.metrics["stt_segments"]) for item in items] == [1, 0, 1]
            report = await boundary.drain(1)
            assert report.unconfirmed_turns == 1 and not report.complete
        finally:
            release.set()
            await session.aclose()


async def test_sdk_capture_is_durable_before_delayed_chat_and_corrections_share_identity(
    postgres_sessionmaker,
):
    from sqlalchemy import select
    from test_transcription import seed

    from interview_agent.interview import db
    from interview_agent.interview.transcription import OrderedCaptureWriter

    interview_id = await seed(postgres_sessionmaker)
    writer = OrderedCaptureWriter(postgres_sessionmaker, interview_id)
    async with gateway() as (model, _, _):
        model.capture_sink = writer.submit
        session = AgentSession(
            stt=model,
            turn_handling={"turn_detection": "manual"},
            session_close_transcript_timeout=0,
        )
        items = []
        session.on(
            "conversation_item_added",
            lambda ev: items.append(ev.item) if isinstance(ev.item, ChatMessage) else None,
        )
        await session.start(InterviewAgent(instructions="Synthetic"), record=False)
        boundary = await wait_boundary(model)
        activity, recognition = boundary.activity, boundary.recognition
        release = asyncio.Event()
        activity._user_turn_completed_atask = asyncio.create_task(release.wait())
        stream = model._drain_streams[0]

        def final(text, start):
            # Use the actual installed transport adapter, including its raw
            # timestamp provenance, not a fabricated confirmed ChatMessage.
            data = stream._build_speech_data({"transcript": text, "start": start, "duration": 1})
            recognition._process_stt_event(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    request_id="stream",
                    alternatives=[data],
                )
            )

        def commit(text):
            info = _EndOfTurnInfo(
                skip_reply=True,
                new_transcript=text,
                transcript_confidence=1,
                metrics=_EndOfTurnMetrics(None, None, None, None),
            )
            assert activity.on_end_of_turn(info)
            recognition._audio_transcript = ""

        try:
            final("Original answer", 0)
            commit("Original answer")
            final("Corrected answer", 0)
            final("Same words new audio", 2)
            commit("Same words new audio")
            await writer.drain()
            assert not items
            async with postgres_sessionmaker() as transaction:
                messages = await db.get_messages(transaction, interview_id)
                assert [m.content for m in messages] == ["Corrected answer", "Same words new audio"]
                assert [m.version for m in messages] == [2, 1]
                versions = list(
                    await transaction.scalars(
                        select(db.MessageVersion).order_by(
                            db.MessageVersion.message_id, db.MessageVersion.version
                        )
                    )
                )
                assert [v.content for v in versions] == [
                    "Original answer",
                    "Corrected answer",
                    "Same words new audio",
                ]
            release.set()
            await asyncio.wait_for(activity._user_turn_completed_atask, 1)
            # Delayed SDK aliases can arrive at version 1 after durable version 2.
            for item in items:
                if isinstance(item, ChatMessage) and item.role == "user":
                    writer.submit(
                        content=item.text_content, source_id=item.id, metrics=item.metrics
                    )
            await writer.drain()
            assert not writer.integrity_pending
            async with postgres_sessionmaker() as transaction:
                assert (await db.get_messages(transaction, interview_id))[0].version == 2
        finally:
            release.set()
            await session.aclose()
            await writer.drain()


async def test_wire_diagnostics_count_control_signals_without_content_or_weaker_barrier():
    async with gateway(mode="ack_only") as (model, _, _):
        stream = model.stream(conn_options=APIConnectOptions(max_retry=0))
        events = []
        receiver = asyncio.create_task(consume(stream, events))
        stream.push_frame(frame())
        report = await model.end_input_and_drain(0.05)
        assert not report.complete
        wire = report.wire_diagnostics[0]
        assert wire["close_requested"]
        assert wire["signals"]["sent:session.close"] == 1
        assert wire["signals"]["received:session.finalized"] == 1
        assert not wire["provider_closed"]
        stream.observe_wire("received", "CANARY_PROVIDER_BODY_93d81")
        snapshot = stream.wire_snapshot()
        assert snapshot["signals"]["received:unknown"] == 1
        assert "CANARY" not in str(snapshot)
        assert "received:unknown" not in wire["signals"]  # Snapshot stays immutable.
        await stream.aclose()
        await asyncio.gather(receiver, return_exceptions=True)

"""Exclusive, bounded closing with a finite clip acknowledged by the browser."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
import wave
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from interview_agent.interview import db
from interview_agent.playback import PlaybackAck, acknowledge_playback, record_delivery
from interview_agent.stt_drain import STTDrainReport

logger = logging.getLogger(__name__)
CONTROL_TOPIC = "interview.control"
FAREWELL_TOPIC = "interview.farewell_audio"
FINALIZATION_SECONDS = 5.0
LEASE_RENEWAL_MARGIN_SECONDS = 5.0
PLAYBACK_POLL_SECONDS = 0.25
STT_DRAIN_SECONDS = 5.0
FAREWELLS = {
    "es": (
        "Gracias por compartir tu experiencia. Hemos terminado la entrevista. "
        "Te deseo mucho éxito; hasta pronto."
    ),
    "en": (
        "Thank you for sharing your experience. Our interview is now complete. "
        "I wish you every success. Goodbye."
    ),
}


async def synthesize_farewell(tts, language: str) -> bytes:
    frames = []
    async with tts.synthesize(FAREWELLS.get(language, FAREWELLS["en"])) as stream:
        async for item in stream:
            frames.append(item.frame)
    if not frames:
        raise ValueError("Farewell synthesis produced no audio")
    first = frames[0]
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(first.num_channels)
        wav.setsampwidth(2)
        wav.setframerate(first.sample_rate)
        for frame in frames:
            if frame.sample_rate != first.sample_rate or frame.num_channels != first.num_channels:
                raise ValueError("Farewell audio format changed")
            wav.writeframes(bytes(frame.data))
    return output.getvalue()


class ClosingCoordinator:
    def __init__(
        self,
        *,
        room,
        session,
        sessionmaker,
        conversation_id,
        telemetry,
        language,
        timeout_seconds,
        drain_transcript,
        seal_transcript: Callable[[], Awaitable[None]] | None = None,
        end_stt_input: Callable[[], Awaitable[STTDrainReport | None]] | None = None,
        stt_drain_seconds: float = STT_DRAIN_SECONDS,
        owner_id: uuid.UUID | None = None,
        on_claim: Callable[[], Awaitable[bool]] | None = None,
        speech_fence: Callable[[], None] | None = None,
    ):
        self.room = room
        self.session = session
        self.sessionmaker = sessionmaker
        self.conversation_id = conversation_id
        self.telemetry = telemetry
        self.language = language
        self.timeout_seconds = timeout_seconds
        self.drain_transcript = drain_transcript
        self.seal_transcript = seal_transcript
        self.end_stt_input = end_stt_input
        self.stt_drain_seconds = stt_drain_seconds
        self._stt_report = {"complete": False, "reason": "drain_not_verified"}
        self.owner_id = owner_id or uuid.uuid4()
        self.on_claim = on_claim
        self.speech_fence = speech_fence
        self.closing_id = None
        self.stream_id = None
        self.attempt_id = None
        self.owns_closure = False
        self.playback = asyncio.Event()
        self.playback_status = "pending"
        self._delivery_ready = False
        self._audio_task = None
        self._finish_task = None
        self._abort = asyncio.Event()
        self._pending: set[asyncio.Task] = set()
        self._finished = False
        self._recovered = False

    def _observe(self, task):
        self._pending.add(task)

        def done(completed):
            self._pending.discard(completed)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    logger.warning("Closing background task failed: %s", type(error).__name__)

        task.add_done_callback(done)
        return task

    async def _bounded(self, awaitable, seconds):
        """Unlike wait_for, never wait indefinitely for cancellation cleanup."""
        task = self._observe(asyncio.ensure_future(awaitable))
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, seconds))
            if task not in done:
                task.cancel()
                raise TimeoutError("Closing operation exceeded its deadline")
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    def prewarm(self):
        if self._audio_task is None:
            self._audio_task = self._observe(
                asyncio.create_task(
                    synthesize_farewell(self.session.tts, self.language),
                    name="prepare-farewell",
                )
            )

    async def publish(self, event, **fields):
        if self.speech_fence is not None:
            self.speech_fence()
        payload = {
            "event": event,
            "conversation_id": str(self.conversation_id),
            "closing_id": str(self.closing_id) if self.closing_id else None,
            "stream_id": self.stream_id,
            "attempt_id": str(self.attempt_id) if self.attempt_id else None,
            **fields,
        }
        await self.room.local_participant.publish_data(
            json.dumps(payload).encode(),
            reliable=True,
            topic=CONTROL_TOPIC,
            destination_identities=["candidate"],
        )

    async def acknowledge(self, caller, payload):
        data = json.loads(payload)
        if (
            not isinstance(data, dict)
            or caller != "candidate"
            or not self.owns_closure
            or self.closing_id is None
            or self.stream_id is None
            or data.get("closing_id") != str(self.closing_id)
            or data.get("stream_id") != self.stream_id
            or data.get("attempt_id") != str(self.attempt_id)
        ):
            raise ValueError("Playback acknowledgement does not match this closure attempt")
        result = await self._bounded(
            acknowledge_playback(
                self.sessionmaker, self.conversation_id, PlaybackAck.model_validate(data)
            ),
            FINALIZATION_SECONDS,
        )
        if result.get("accepted") and result.get("status") in ("played", "failed", "timeout"):
            self.playback_status = result["status"]
            self.playback.set()
        return json.dumps(result)

    async def _claim(self, reason):
        async with self.sessionmaker() as session:
            conv = await session.scalar(
                select(db.Conversation)
                .where(
                    db.Conversation.id == self.conversation_id,
                )
                .with_for_update()
            )
            if conv is None:
                return "not_possible"
            if conv.status not in ("planned", "interviewing", "closing"):
                return conv.farewell_status or "not_possible"
            now = await session.scalar(select(func.clock_timestamp()))
            if conv.closing_owner_id not in (None, self.owner_id):
                if conv.closing_deadline_at is not None and conv.closing_deadline_at > now:
                    return "follow"
                # A dead owner's clip must not be replayed by a replacement job.
                mode = "recover"
            else:
                mode = "deliver"
            self.closing_id = conv.closing_id or uuid.uuid4()
            self.stream_id = (conv.closing_stream_id if mode == "recover" else None) or str(
                uuid.uuid4()
            )
            self.attempt_id = (
                conv.closing_attempt_id if mode == "recover" else None
            ) or uuid.uuid4()
            conv.closing_id = self.closing_id
            conv.closing_owner_id = self.owner_id
            conv.closing_started_at = conv.closing_started_at or now
            # These initial timestamps never move during owner replacement or
            # cleanup renewal. The ACK API and browser wait use this origin.
            if mode != "recover":
                conv.closing_acquired_at = conv.closing_acquired_at or now
            if conv.closing_ack_deadline_at is None:
                conv.closing_audio_timeout_seconds = (
                    conv.closing_audio_timeout_seconds or self.timeout_seconds
                )
                origin = conv.closing_acquired_at or conv.closing_started_at
                conv.closing_ack_deadline_at = origin + timedelta(
                    seconds=conv.closing_audio_timeout_seconds
                    + 2 * FINALIZATION_SECONDS
                    + LEASE_RENEWAL_MARGIN_SECONDS
                )
            conv.closing_deadline_at = now + timedelta(
                seconds=(
                    self.timeout_seconds + 2 * FINALIZATION_SECONDS + LEASE_RENEWAL_MARGIN_SECONDS
                ),
            )
            conv.closing_stream_id = self.stream_id
            conv.closing_attempt_id = self.attempt_id
            conv.status = "closing"
            conv.ended_reason = conv.ended_reason or reason
            if mode == "recover" and conv.farewell_status in ("played", "failed"):
                self.playback_status = conv.farewell_status
            else:
                conv.farewell_status = "pending"
            conv.state_revision += 1
            await session.commit()
            self.owns_closure = True
            if self.on_claim is not None and not await self.on_claim():
                raise RuntimeError("Worker ownership was lost while acquiring closure")
            return mode

    async def _follow(self, reason):
        deadline = time.monotonic() + self.timeout_seconds + 4 * FINALIZATION_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(min(0.2, self.timeout_seconds))
            mode = await self._bounded(self._claim(reason), FINALIZATION_SECONDS)
            if mode != "follow":
                return mode
        raise TimeoutError("Another owner has not finalized closure")

    async def _deliver(self, reason):
        self.prewarm()
        await self.publish(
            "closing",
            reason=reason,
            text=FAREWELLS.get(self.language, FAREWELLS["en"]),
            timeout_seconds=self.timeout_seconds,
        )
        self.session.input.set_audio_enabled(False)
        current = self.session.current_speech
        if current is not None:
            # Ending during a long question must leave time for the farewell.
            # The public interrupt future also commits the interrupted item.
            try:
                await self._bounded(self.session.interrupt(force=True), 1)
            except (TimeoutError, RuntimeError) as exc:
                # The finite clip has a separate transport. The browser silences
                # the interviewer's RTC track before playing it, so a stalled
                # old playout callback must not suppress the farewell.
                self.telemetry.emit(
                    "closing",
                    "speech_interrupt_errors",
                    1,
                    dimensions={"error_type": type(exc).__name__},
                )
        audio = await asyncio.shield(self._audio_task)
        writer = await self.room.local_participant.stream_bytes(
            "farewell.wav",
            topic=FAREWELL_TOPIC,
            mime_type="audio/wav",
            stream_id=self.stream_id,
            total_size=len(audio),
            destination_identities=["candidate"],
            attributes={
                "closing_id": str(self.closing_id),
                "conversation_id": str(self.conversation_id),
                "attempt_id": str(self.attempt_id),
            },
        )
        try:
            if self.speech_fence is not None:
                self.speech_fence()
            await writer.write(audio)
            # Commit full delivery before the footer. Keep this bounded commit
            # alive if the audio budget expires just after the final byte.
            persisted = self._observe(
                asyncio.create_task(
                    self._bounded(
                        record_delivery(
                            self.sessionmaker,
                            self.conversation_id,
                            self.owner_id,
                            self.closing_id,
                            self.stream_id,
                            self.attempt_id,
                            len(audio),
                            "audio/wav",
                        ),
                        FINALIZATION_SECONDS,
                    )
                )
            )
            status = await asyncio.shield(persisted)
            if status in ("played", "failed", "timeout"):
                self.playback_status = status
                self.playback.set()
            self._delivery_ready = True
            await writer.aclose()
            await self.playback.wait()
        finally:
            # Cancellation of write/aclose must not introduce an unbounded await.
            if not self._delivery_ready:
                try:
                    await self._bounded(writer.aclose(), min(0.25, self.timeout_seconds))
                except (Exception, asyncio.CancelledError):
                    logger.debug("Farewell writer cleanup did not finish")

    async def _audio_phase(self, reason, say_goodbye):
        if not say_goodbye:
            self.playback_status = "not_possible"
            return
        delivery = self._observe(asyncio.create_task(self._deliver(reason)))
        abort = asyncio.create_task(self._abort.wait())
        playback = asyncio.create_task(self._watch_playback())
        try:
            done, _ = await asyncio.wait(
                {delivery, abort, playback},
                timeout=self.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if playback in done:
                playback.result()
                # Playback is authoritative even if an FFI footer callback stalls.
                delivery.cancel()
            elif abort in done:
                delivery.cancel()
                self.playback_status = "not_possible"
            elif delivery in done:
                delivery.result()
            else:
                delivery.cancel()
                self.playback_status = "timeout"
        except Exception:
            self.playback_status = "failed"
            logger.exception("Farewell delivery failed")
        finally:
            abort.cancel()
            playback.cancel()
            self._delivery_ready = False

    async def _watch_playback(self):
        while not self.playback.is_set():
            try:
                async with asyncio.timeout(0.5), self.sessionmaker() as session:
                    conv = await session.get(db.Conversation, self.conversation_id)
                    if (
                        conv is not None
                        and conv.closing_id == self.closing_id
                        and conv.closing_stream_id == self.stream_id
                        and conv.closing_attempt_id == self.attempt_id
                        and conv.farewell_status in ("played", "failed")
                    ):
                        self.playback_status = conv.farewell_status
                        self.playback.set()
                        return
            except (TimeoutError, SQLAlchemyError):
                # A transient DB outage must not erase a durable ACK. The
                # containing audio phase and reconciliation remain bounded.
                pass
            with suppress(TimeoutError):
                await asyncio.wait_for(self.playback.wait(), PLAYBACK_POLL_SECONDS)

    async def _finalize(self):
        async with self.sessionmaker() as session:
            conv = await session.scalar(
                select(db.Conversation)
                .where(
                    db.Conversation.id == self.conversation_id,
                )
                .with_for_update()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                conv is None
                or conv.status != "closing"
                or conv.closing_owner_id != self.owner_id
                or conv.closing_deadline_at is None
                or conv.closing_deadline_at <= now
            ):
                raise RuntimeError("Closure ownership changed before transcript finalization")
            if conv.farewell_status in ("played", "failed"):
                self.playback_status = conv.farewell_status
            # Renew under the canonical lock before sealing SDK input and writes.
            conv.closing_deadline_at = max(
                conv.closing_deadline_at,
                now + timedelta(seconds=2 * FINALIZATION_SECONDS),
            )
            await session.commit()
        # Recognition was explicitly drained in parallel to the farewell.
        # Closing the SDK now commits its remaining items before persistence.
        if self.seal_transcript is not None:
            await self.seal_transcript()
        await self.drain_transcript()
        async with self.sessionmaker() as session:
            conv = await session.scalar(
                select(db.Conversation)
                .where(
                    db.Conversation.id == self.conversation_id,
                )
                .with_for_update()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                conv is None
                or conv.status != "closing"
                or conv.closing_owner_id != self.owner_id
                or conv.closing_deadline_at is None
                or conv.closing_deadline_at <= now
            ):
                raise RuntimeError("Closure ownership changed while finalizing transcript")
            if conv.farewell_status in ("played", "failed", "timeout"):
                self.playback_status = conv.farewell_status
            if self.playback_status == "played":
                await db.insert_message(
                    session,
                    self.conversation_id,
                    "assistant",
                    FAREWELLS.get(self.language, FAREWELLS["en"]),
                    source_id="farewell-" + str(self.closing_id),
                    commit=False,
                )
            conv.status = "completed"
            conv.state_revision += 1
            conv.transcript_sealed_at = now
            unconfirmed = await session.scalar(
                select(func.count())
                .select_from(db.Message)
                .where(
                    db.Message.conversation_id == self.conversation_id,
                    db.Message.role == "user",
                    db.Message.metrics["stt_confirmed"].is_distinct_from(
                        literal(True, type_=JSONB)
                    ),
                )
            )
            # A replacement worker's in-memory counter cannot erase an
            # unresolved or unproven turn persisted by an earlier session.
            conv.stt_drain = {
                **self._stt_report,
                "canonical_unconfirmed_user_messages": unconfirmed,
            }
            conv.transcript_integrity = (
                "complete"
                if (
                    not self._recovered
                    and self._stt_report["complete"]
                    and not unconfirmed
                    and not conv.capture_integrity_pending
                )
                else "partial"
            )
            conv.farewell_status = self.playback_status
            from interview_agent.interview.seals import ensure_seal

            await ensure_seal(session, conv)
            await session.commit()

    async def _mark_finalize_error(self):
        async with self.sessionmaker() as session:
            conv = await session.scalar(
                select(db.Conversation)
                .where(
                    db.Conversation.id == self.conversation_id,
                )
                .with_for_update()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                conv
                and conv.status == "closing"
                and conv.closing_owner_id == self.owner_id
                and conv.closing_deadline_at is not None
                and conv.closing_deadline_at > now
            ):
                # Incomplete persistence must not be evaluated as a complete transcript.
                conv.status = "error"
                if conv.farewell_status in ("played", "failed", "timeout"):
                    self.playback_status = conv.farewell_status
                else:
                    conv.farewell_status = self.playback_status
                conv.transcript_integrity = "failed"
                conv.state_revision += 1
                await session.commit()

    async def _finish(self, reason, say_goodbye):
        start = time.monotonic()
        try:
            mode = await self._bounded(self._claim(reason), FINALIZATION_SECONDS)
        except BaseException as exc:
            self._claim_error = exc
            self._claimed.set()
            raise
        self._claimed.set()
        if mode == "follow":
            mode = await self._follow(reason)
        if mode not in ("deliver", "recover"):
            self._finished = True
            self.playback_status = mode
            return mode
        if mode == "recover":
            self._recovered = True
            self._stt_report = {"complete": False, "reason": "owner_recovered"}
            if self.playback_status not in ("played", "failed"):
                self.playback_status = "timeout"
        else:
            self.session.input.set_audio_enabled(False)
            stt_task = None
            if self.end_stt_input is not None:
                stt_task = self._observe(
                    asyncio.create_task(
                        self._bounded(self.end_stt_input(), self.stt_drain_seconds),
                        name="finish-stt-input",
                    )
                )
            await self._audio_phase(reason, say_goodbye)
            if stt_task is not None:
                try:
                    report = await asyncio.shield(stt_task)
                    if report is not None:
                        self._stt_report = report.as_dict()
                except Exception as exc:
                    self._stt_report = {"complete": False, "error_type": type(exc).__name__}
                    self.telemetry.emit("stt", "final_drain_errors", 1)
            self.telemetry.emit("stt", "final_drain_complete", int(self._stt_report["complete"]))
        try:
            await self._bounded(self._finalize(), FINALIZATION_SECONDS)
        except Exception:
            await self._bounded(self._mark_finalize_error(), FINALIZATION_SECONDS)
            self.telemetry.emit("closing", "transcript_finalize_errors", 1)
            self._finished = True
            raise
        self.telemetry.emit(
            "closing",
            "duration_seconds",
            time.monotonic() - start,
            dimensions={"farewell_status": self.playback_status},
        )
        self.telemetry.emit("closing", "playback_confirmed", int(self.playback_status == "played"))
        self._finished = True
        try:
            await self._bounded(
                self.publish("completed", farewell_status=self.playback_status),
                1,
            )
        except Exception:
            logger.debug("Completion publication failed; persistent state remains available")
        return self.playback_status

    def _start_finish(self, reason, say_goodbye):
        if not say_goodbye:
            self._abort.set()
        if (
            self._finish_task is not None
            and self._finish_task.done()
            and not self.owns_closure
            and (self._finish_task.cancelled() or self._finish_task.exception() is not None)
        ):
            self._finish_task = None
        if self._finish_task is None:
            self._claimed = asyncio.Event()
            self._claim_error = None
        # Watcher cancellation does not cancel canonical closing/finalization.
        if self._finish_task is None:
            self._finish_task = self._observe(
                asyncio.create_task(
                    self._finish(reason, say_goodbye),
                    name="close-interview",
                )
            )
        return self._finish_task

    async def request_close(self, reason):
        self._start_finish(reason, True)
        await self._bounded(self._claimed.wait(), FINALIZATION_SECONDS)
        if self._claim_error is not None:
            raise self._claim_error

    async def finish(self, reason, *, say_goodbye=True):
        return await asyncio.shield(self._start_finish(reason, say_goodbye))

    async def aclose(self):
        self._abort.set()
        if self._audio_task is not None and not self._audio_task.done():
            self._audio_task.cancel()
        if self._finish_task is not None and not self._finish_task.done():
            await self._bounded(
                asyncio.shield(self._finish_task),
                FINALIZATION_SECONDS + 1,
            )

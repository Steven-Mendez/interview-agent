"""Exclusive, bounded closing through the agent's native speech pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

from interview_agent.interview import db
from interview_agent.playback import NATIVE_AUDIO_MIME, confirmation_source, record_agent_playout
from interview_agent.stt_drain import STTDrainReport

logger = logging.getLogger(__name__)
CONTROL_TOPIC = "interview.control"
FAREWELL_READY_METHOD = "interview.farewell_ready"
FINALIZATION_SECONDS = 5.0
LEASE_RENEWAL_MARGIN_SECONDS = 5.0
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
        self.playback_status = "pending"
        self.confirmation_source = "agent_playout"
        self._speech = None
        self._stt_task = None
        self._finish_task = None
        self._abort = asyncio.Event()
        self._audio_deadline = None
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

    def is_farewell_item(self, item) -> bool:
        return self._speech is not None and any(
            message.id == item.id for message in self._speech.chat_items
        )

    async def wait_for_stt_drain(self):
        """Keep the last candidate turn ahead of the native farewell in SQL."""
        if self._stt_task is not None:
            # The finish task records an unverifiable drain as partial.
            # Its bounded failure must not discard the spoken SDK item.
            with suppress(Exception):
                await asyncio.shield(self._stt_task)

    async def publish(self, event, **fields):
        if self.speech_fence is not None:
            self.speech_fence()
        payload = {
            "event": event,
            "conversation_id": str(self.conversation_id),
            "closing_id": str(self.closing_id) if self.closing_id else None,
            "transport": "rtc",
            "confirmation_source": self.confirmation_source,
            "attempt_id": str(self.attempt_id) if self.attempt_id else None,
            **fields,
        }
        await self.room.local_participant.publish_data(
            json.dumps(payload).encode(),
            reliable=True,
            topic=CONTROL_TOPIC,
            destination_identities=["candidate"],
        )

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
                # A dead owner's farewell must never be replayed by a replacement.
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
            if mode != "recover":
                conv.closing_audio_mime = NATIVE_AUDIO_MIME
            self.confirmation_source = confirmation_source(conv) or "browser_playback"
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
        self._require_audio_phase()
        self.session.input.set_audio_enabled(False)
        if not self.session.output.audio_enabled or self.session.output.audio is None:
            self.playback_status = "not_possible"
            return
        current = self.session.current_speech
        if current is not None:
            # Ending during a long question must leave time for the farewell.
            # The public interrupt future also commits the interrupted item.
            # Both speeches share the same output: a stalled interruption must
            # not be followed by another speech or counted as success.
            await self._bounded(self.session.interrupt(force=True), 1)
        self._require_audio_phase()
        await self.publish("closing", reason=reason, timeout_seconds=self.timeout_seconds)
        # A readiness RPC gates old tabs that still mute the agent for WAV
        # playback, and waits for blocked RTC audio without synthesizing twice.
        ready = json.loads(
            await self.room.local_participant.perform_rpc(
                destination_identity="candidate",
                method=FAREWELL_READY_METHOD,
                payload=json.dumps(
                    {
                        "conversation_id": str(self.conversation_id),
                        "closing_id": str(self.closing_id),
                        "attempt_id": str(self.attempt_id),
                        "timeout_seconds": self.timeout_seconds,
                    }
                ),
                response_timeout=self.timeout_seconds,
            )
        )
        self._require_audio_phase()
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            self.playback_status = "not_possible"
            return
        if self.speech_fence is not None:
            self.speech_fence()
        self._speech = self.session.say(
            FAREWELLS.get(self.language, FAREWELLS["en"]),
            allow_interruptions=False,
            add_to_chat_ctx=True,
        )
        try:
            await self._speech.wait_for_playout()
            if self._speech.exception() is not None or self._speech.interrupted:
                raise RuntimeError("Native farewell speech did not finish successfully")
            messages = [
                item
                for item in self._speech.chat_items
                if getattr(item, "role", None) == "assistant"
            ]
            if len(messages) != 1 or messages[0].interrupted:
                raise RuntimeError("Native farewell has no complete session item")
            metrics = messages[0].metrics
            duration = metrics.get("stopped_speaking_at", 0) - metrics.get("started_speaking_at", 0)
            if not metrics.get("started_speaking_at") or duration <= 0:
                raise RuntimeError("Native farewell has no audio playout evidence")
            self._require_audio_phase()
            if self.speech_fence is not None:
                self.speech_fence()
            # Success must survive a crash before the final seal: commit the
            # SDK item first, identified by its source ID rather than its text.
            await self.drain_transcript()
            self._require_audio_phase()
            if not await record_agent_playout(
                self.sessionmaker,
                self.conversation_id,
                self.owner_id,
                self.closing_id,
                self.attempt_id,
                duration,
                source_id=messages[0].id,
            ):
                raise RuntimeError("Native farewell ownership or deadline changed")
            self.playback_status = "played"
        finally:
            if not self._speech.done():
                self._speech.interrupt(force=True)

    def _require_audio_phase(self):
        # Cancellation-resistant SDK callbacks may return after closing has
        # timed out. They must never start or confirm another native speech.
        if (
            self._audio_deadline is None
            or time.monotonic() >= self._audio_deadline
            or self._abort.is_set()
        ):
            raise TimeoutError("Native farewell audio phase is no longer active")

    async def _audio_phase(self, reason, say_goodbye):
        if not say_goodbye:
            self.playback_status = "not_possible"
            return
        self._audio_deadline = time.monotonic() + self.timeout_seconds
        delivery = self._observe(asyncio.create_task(self._deliver(reason)))
        abort = asyncio.create_task(self._abort.wait())
        try:
            done, _ = await asyncio.wait(
                {delivery, abort},
                timeout=self.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if abort in done:
                delivery.cancel()
                self.playback_status = "not_possible"
            elif delivery in done:
                delivery.result()
            else:
                delivery.cancel()
                self.playback_status = "timeout"
        except TimeoutError:
            self.playback_status = "timeout"
        except Exception:
            self.playback_status = "failed"
            logger.exception("Farewell delivery failed")
        finally:
            self._audio_deadline = None
            if not delivery.done():
                delivery.cancel()
            if self._speech is not None and not self._speech.done():
                self._speech.interrupt(force=True)
            abort.cancel()

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
            if self.playback_status == "played" and conv.closing_audio_mime != NATIVE_AUDIO_MIME:
                # Historical WAV recovery only. Native speech is persisted by
                # conversation_item_added with its SDK identity, never twice.
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
                self._stt_task = stt_task
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
            dimensions={
                "farewell_status": self.playback_status,
                "confirmation_source": self.confirmation_source,
            },
        )
        self.telemetry.emit(
            "closing",
            "playback_confirmed",
            int(self.playback_status == "played"),
            dimensions={"confirmation_source": self.confirmation_source},
        )
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
        if self._speech is not None and not self._speech.done():
            self._speech.interrupt(force=True)
        if self._finish_task is not None and not self._finish_task.done():
            await self._bounded(
                asyncio.shield(self._finish_task),
                FINALIZATION_SECONDS + 1,
            )

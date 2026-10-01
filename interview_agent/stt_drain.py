"""An explicit final-input barrier for the pinned LiveKit inference transport.

LiveKit 1.8.3 does not expose ``session.closed`` through SpeechEvent and its
default node never ends STT input. The compatibility boundary below retains
the SDK's resampling, timings, events, retries and metrics. It observes the
gateway's ordered close response instead of treating an ACK, silence timer
or cancellation as proof of complete recognition. Update its transport tests
before changing the pinned SDK version.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from importlib.metadata import version

import aiohttp
from livekit.agents import inference, stt
from livekit.agents.inference.stt import SpeechStream as InferenceSpeechStream
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN

SUPPORTED_SDK = "1.8.3"


class SessionSTTBoundary:
    """Pinned SDK bridge for admission, recognition and immutable turn provenance.

    Bind only this activity's methods, never SDK classes globally. Provider
    finals are recorded where recognition actually processes them, including
    held events. EOT takes a synchronous snapshot before speech scheduling can
    defer creation of its ChatMessage. A promoted interim never passes through
    this provider-event path and therefore cannot acquire a final's provenance.
    """

    def __init__(self, activity, backend):
        self.activity = activity
        self.backend = backend
        self.recognition = activity._audio_recognition
        if self.recognition is None:
            raise RuntimeError("STT admission boundary requires audio recognition")
        self.cut = False
        self.audio_exhausted = asyncio.Event()
        self.final_texts: list[str] = []
        self.segments: list[dict] = []
        self.turn_id = str(uuid.uuid4())
        self.capture_session_id = str(uuid.uuid4())
        self.capture_sequence = 0
        self.committed_segments = {}
        self.unconfirmed_turns = 0
        original_push = self.recognition._push_audio
        original_process = self.recognition._process_stt_event
        original_eot = activity.on_end_of_turn
        original_metrics = activity._init_metrics_from_end_of_turn

        def push(frame, **kwargs):
            # Atomic event-loop admission cut. Frames already in audio_ch
            # remain admitted; later frames never enter recognition's queues.
            if not self.cut:
                original_push(frame, **kwargs)

        def segment_key(start, end, request_id, explicit):
            if (
                request_id
                and explicit
                and type(start) in (float, int)
                and type(end) in (float, int)
                and math.isfinite(start)
                and math.isfinite(end)
                and 0 <= start < end
            ):
                return (request_id, start, end)
            return None

        def emit(content, proof):
            sink = getattr(backend, "capture_sink", None)
            if sink is not None:
                sink(
                    content=content,
                    source_id=f"capture-{proof['stt_turn_id']}-v{proof['stt_turn_version']}",
                    metrics=proof,
                )

        def process(event):
            if (
                event.type == stt.SpeechEventType.FINAL_TRANSCRIPT
                and event.alternatives
                and event.alternatives[0].text
            ):
                data = event.alternatives[0]
                key = segment_key(
                    data.start_time,
                    data.end_time,
                    getattr(data, "_interview_capture_stream_id", None),
                    getattr(data, "_interview_timing_explicit", False),
                )
                if key is not None and key in self.committed_segments:
                    capture, index = self.committed_segments[key]
                    if capture["texts"][index] != data.text:
                        capture["texts"][index] = data.text
                        capture["proof"]["stt_turn_version"] += 1
                        capture["proof"]["stt_segments"][index]["version"] += 1
                        emit(" ".join(capture["texts"]).lstrip(), copy.deepcopy(capture["proof"]))
                    # An exact interval from this recognition stream belongs
                    # to an admitted turn; never create a second SDK response.
                    return
                for index, segment in enumerate(self.segments):
                    if key is not None and key == (
                        segment["capture_stream_id"],
                        segment["audio_start"],
                        segment["audio_end"],
                    ):
                        previous = " ".join(self.final_texts).lstrip()
                        if self.recognition._audio_transcript == previous:
                            self.final_texts[index] = data.text
                            segment["version"] += 1
                            self.recognition._audio_transcript = " ".join(self.final_texts).lstrip()
                            return
                self.final_texts.append(data.text)
                self.segments.append(
                    {
                        "id": str(uuid.uuid4()),
                        "version": 1,
                        "provider_request_id": event.request_id or None,
                        "capture_stream_id": getattr(data, "_interview_capture_stream_id", None),
                        "timing_explicit": getattr(data, "_interview_timing_explicit", False),
                        "audio_start": data.start_time,
                        "audio_end": data.end_time,
                    }
                )
            original_process(event)

        def end_of_turn(info):
            # Snapshot before the SDK defers ChatMessage construction. Exact
            # rendering verifies confirmation; it is never an identity key.
            proof = {
                "stt_confirmed": bool(info.new_transcript)
                and info.new_transcript == " ".join(self.final_texts).lstrip(),
                "stt_turn_id": self.turn_id,
                "stt_turn_version": 1,
                "stt_segments": copy.deepcopy(self.segments),
                "stt_capture_session_id": self.capture_session_id,
                "stt_capture_sequence": self.capture_sequence + 1,
                "stt_segmentation": "audio_intervals"
                if self.segments
                and all(
                    segment_key(
                        segment["audio_start"],
                        segment["audio_end"],
                        segment["capture_stream_id"],
                        segment["timing_explicit"],
                    )
                    is not None
                    for segment in self.segments
                )
                else "unknown",
            }
            info._interview_stt_provenance = copy.deepcopy(proof)
            committed = original_eot(info)
            if committed:
                self.capture_sequence += 1
                if info.new_transcript:
                    emit(info.new_transcript, copy.deepcopy(proof))
                if info.new_transcript and not proof["stt_confirmed"]:
                    self.unconfirmed_turns += 1
                if proof["stt_confirmed"]:
                    capture = {"texts": list(self.final_texts), "proof": copy.deepcopy(proof)}
                    for index, segment in enumerate(self.segments):
                        key = (
                            segment["capture_stream_id"],
                            segment["audio_start"],
                            segment["audio_end"],
                        )
                        if proof["stt_segmentation"] == "audio_intervals":
                            self.committed_segments[key] = (capture, index)
                self.final_texts.clear()
                self.segments.clear()
                self.turn_id = str(uuid.uuid4())
            return committed

        def init_metrics(info):
            result = original_metrics(info)
            proof = getattr(info, "_interview_stt_provenance", {"stt_confirmed": False})
            result.update(proof)
            if "stt_segments" in result:
                result["stt_segments"] = [dict(segment) for segment in result["stt_segments"]]
            return result

        self.recognition._push_audio = push
        self.recognition._process_stt_event = process
        activity.on_end_of_turn = end_of_turn
        activity._init_metrics_from_end_of_turn = init_metrics

    async def forward_audio(self, audio):
        async for frame in audio:
            yield frame
        # Reached natural EOF only after the default producer pushed the last
        # yielded frame. Cancellation cannot set this proof.
        self.audio_exhausted.set()

    async def drain(self, timeout_seconds=5.0):
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        session = self.activity.session
        self.cut = True
        session.input.set_audio_enabled(False)
        forward = session._forward_audio_atask
        if forward is not None:
            forward.cancel()
        pipeline = self.recognition._stt_pipeline
        if pipeline is None:
            report = await self.backend.end_input_and_drain(0)
            return replace(
                report,
                upstream_input_drained=False,
                recognition_consumed=False,
                admission_boundary="session_recognition",
            )
        pipeline.audio_ch.close()  # Chan drains queued frames before EOF.
        upstream_drained = False
        try:
            async with asyncio.timeout_at(deadline):
                await self.audio_exhausted.wait()
                upstream_drained = True
        except TimeoutError:
            pass
        report = await self.backend.end_input_and_drain(
            max(0, deadline - asyncio.get_running_loop().time())
        )
        recognition_consumed = False
        consumers = [pipeline._pump_task, self.recognition._stt_consumer_atask]
        if all(task is not None for task in consumers):
            done, pending = await asyncio.wait(
                consumers, timeout=max(0, deadline - asyncio.get_running_loop().time())
            )
            recognition_consumed = not pending and all(
                not task.cancelled() and task.exception() is None for task in done
            )
        if recognition_consumed:
            self.recognition._flush_held_transcripts()
        return replace(
            report,
            upstream_input_drained=upstream_drained,
            recognition_consumed=recognition_consumed,
            admission_boundary="session_recognition",
            unconfirmed_turns=self.unconfirmed_turns,
        )


@dataclass(frozen=True)
class STTDrainReport:
    input_ended: bool
    streams: int
    admitted_audio_seconds: float
    unresolved_streams: int
    provider_closed_streams: int
    consumed_streams: int
    upstream_input_drained: bool = True
    recognition_consumed: bool = True
    admission_boundary: str = "direct_stream"
    unconfirmed_turns: int = 0
    wire_diagnostics: tuple[dict, ...] = ()

    @property
    def complete(self) -> bool:
        return (
            self.input_ended
            and self.streams > 0
            and self.unresolved_streams == 0
            and self.upstream_input_drained
            and self.recognition_consumed
            and self.unconfirmed_turns == 0
        )

    def as_dict(self) -> dict:
        return {**asdict(self), "complete": self.complete, "sdk_version": SUPPORTED_SDK}


class _ClosingSocket:
    """Observe the gateway barrier without changing transcript messages."""

    def __init__(self, socket, stream):
        self.socket = socket
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def send_str(self, payload, *args, **kwargs):
        await self.socket.send_str(payload, *args, **kwargs)
        kind = json.loads(payload).get("type")
        self.stream.observe_wire("sent", kind)
        if self.stream.input_ended and kind == "session.finalize":
            self.stream.close_requested = True
            try:
                # This follows all admitted frames and the SDK's final flush.
                await self.socket.send_str(json.dumps({"type": "session.close"}))
                self.stream.observe_wire("sent", "session.close")
            except BaseException:
                self.stream.close_requested = False
                self.stream.transport_failed = True
                raise

    async def receive(self, *args, **kwargs):
        message = await self.socket.receive(*args, **kwargs)
        if message.type == aiohttp.WSMsgType.TEXT:
            kind = json.loads(message.data).get("type")
            self.stream.observe_wire("received", kind)
            if kind == "session.closed":
                if self.stream.input_ended and self.stream.close_requested:
                    self.stream.provider_closed = True
                    # 1.8.3 otherwise ignores this terminal message. Its own
                    # receiver safely ends on CLOSE after sending final input.
                    return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1000, "")
                self.stream.transport_failed = True
        elif message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
        ):
            self.stream.observe_wire("received", "socket_close")
        return message


class _DrainingStream(InferenceSpeechStream):
    def __init__(self, **kwargs):
        self.input_ended = False
        self.close_requested = False
        self.provider_closed = False
        self.consumer_drained = False
        self.transport_failed = False
        self.pending_interim = False
        self.connections = 0
        self.capture_stream_id = str(uuid.uuid4())
        self.admitted_audio_seconds = 0.0
        self.finished = asyncio.Event()
        self.wire_counts: dict[str, int] = {}
        super().__init__(**kwargs)

    def observe_wire(self, direction, kind):
        # Keep a bounded alphabet of transport signals, never payloads, audio,
        # transcripts, opaque session identifiers or arbitrary provider strings.
        allowed = {
            "session.create",
            "session.created",
            "session.finalize",
            "session.finalized",
            "session.close",
            "session.closed",
            "input_audio",
            "start_of_speech",
            "interim_transcript",
            "preflight_transcript",
            "final_transcript",
            "error",
            "socket_close",
        }
        label = kind if isinstance(kind, str) and kind in allowed else "unknown"
        key = direction + ":" + label
        self.wire_counts[key] = self.wire_counts.get(key, 0) + 1

    def wire_snapshot(self):
        return {
            "connections": self.connections,
            "input_ended": self.input_ended,
            "close_requested": self.close_requested,
            "provider_closed": self.provider_closed,
            "consumer_drained": self.consumer_drained,
            "transport_failed": self.transport_failed,
            "pending_interim": self.pending_interim,
            "signals": dict(self.wire_counts),
        }

    def _build_speech_data(self, data):
        speech = super()._build_speech_data(data)
        # SDK defaults absent start/duration to zero. Those fabricated bounds
        # cannot identify a correction. Connection identity fences clock resets.
        speech._interview_timing_explicit = "start" in data and "duration" in data
        speech._interview_capture_stream_id = f"{self.capture_stream_id}:{self.connections}"
        return speech

    def push_frame(self, frame):
        if self.input_ended:
            return
        super().push_frame(frame)
        self.admitted_audio_seconds += frame.duration

    def end_input(self):
        if self.input_ended:
            return
        self.input_ended = True
        try:
            super().end_input()
        except BaseException:
            self.transport_failed = True
            self.finished.set()
            raise

    async def _connect_ws(self, http_session):
        if self.connections and self.admitted_audio_seconds:
            # SDK retries cannot prove that an earlier connection resolved
            # every frame. Keep that uncertainty even if the final one drains.
            self.transport_failed = True
        self.connections += 1
        socket = await super()._connect_ws(http_session)
        return _ClosingSocket(socket, self)

    async def __anext__(self):
        try:
            event = await super().__anext__()
        except StopAsyncIteration:
            self.consumer_drained = True
            self.finished.set()
            raise
        except BaseException:
            self.transport_failed = True
            self.finished.set()
            raise
        if event.alternatives:
            text = event.alternatives[0].text.strip()
            if (
                event.type
                in (
                    stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
                )
                and text
            ):
                self.pending_interim = True
            elif event.type == stt.SpeechEventType.FINAL_TRANSCRIPT and text:
                self.pending_interim = False
        return event

    async def aclose(self):
        try:
            await super().aclose()
        finally:
            self.finished.set()

    @property
    def resolved(self):
        return (
            self.input_ended
            and self.provider_closed
            and self.consumer_drained
            and not self.transport_failed
            and not self.pending_interim
        )


class DrainableInferenceSTT(inference.STT):
    def __init__(self, *args, **kwargs):
        if version("livekit-agents") != SUPPORTED_SDK:
            raise RuntimeError("STT drain compatibility needs verification for this LiveKit SDK")
        self._drain_streams: list[_DrainingStream] = []
        self._input_ended = False
        self.session_boundary: SessionSTTBoundary | None = None
        super().__init__(*args, **kwargs)

    def stream(self, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        if self._input_ended:
            raise RuntimeError("STT input has ended; a replacement stream cannot reopen it")
        stream = _DrainingStream(
            stt=self,
            opts=self._sanitize_options(language=language),
            conn_options=conn_options,
            vad_instance=self._vad,
        )
        self._streams.add(stream)
        self._drain_streams.append(stream)
        return stream

    async def end_input_and_drain(self, timeout_seconds=5.0) -> STTDrainReport:
        self._input_ended = True
        streams = list(self._drain_streams)
        for stream in streams:
            # A previously failed stream makes the report partial.
            with suppress(RuntimeError):
                stream.end_input()
        waiters = [asyncio.create_task(stream.finished.wait()) for stream in streams]
        try:
            if waiters:
                await asyncio.wait(waiters, timeout=timeout_seconds)
        finally:
            for waiter in waiters:
                waiter.cancel()
            if waiters:
                await asyncio.gather(*waiters, return_exceptions=True)
        return STTDrainReport(
            input_ended=True,
            streams=len(streams),
            admitted_audio_seconds=sum(s.admitted_audio_seconds for s in streams),
            unresolved_streams=sum(not s.resolved for s in streams),
            provider_closed_streams=sum(s.provider_closed for s in streams),
            consumed_streams=sum(s.consumer_drained for s in streams),
            wire_diagnostics=tuple(s.wire_snapshot() for s in streams),
        )

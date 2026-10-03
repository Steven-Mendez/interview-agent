import { RoomEvent } from "livekit-client"
import type { ByteStreamReader, Room } from "livekit-client"
import { acknowledgeFarewell, getClosingState } from "./api"
import type { PlaybackAcknowledgement, PlaybackErrorKind } from "./api"
import { captureRouteError } from "./error-reporting"

const CONTROL_TOPIC = "interview.control"
const AUDIO_TOPIC = "interview.farewell_audio"
const MAX_CLIP_BYTES = 5_000_000
// How long a suspended AudioContext gets to start outside a gesture. Safari
// leaves resume() pending until the next click instead of rejecting it.
const RESUME_WAIT_MS = 500

// Kinds the API accepts; anything else is reported as "other". Names and
// codes only: a browser's error message can describe the media it failed on.
const DOM_ERROR_KINDS: ReadonlySet<string> = new Set([
  "NotAllowedError",
  "NotSupportedError",
  "AbortError",
  "NotFoundError",
  "EncodingError",
  "InvalidStateError",
  "SecurityError",
])
const MEDIA_ERROR_KINDS: Record<number, PlaybackErrorKind> = {
  1: "MEDIA_ERR_ABORTED",
  2: "MEDIA_ERR_NETWORK",
  3: "MEDIA_ERR_DECODE",
  4: "MEDIA_ERR_SRC_NOT_SUPPORTED",
}

/** Why playback failed, as a bounded category the API and Sentry can keep. */
export function playbackErrorKind(error: unknown): PlaybackErrorKind {
  if (error && typeof error === "object") {
    const { name, code } = error as { name?: unknown; code?: unknown }
    if (typeof name === "string" && DOM_ERROR_KINDS.has(name))
      return name as PlaybackErrorKind
    // A media element reports through `audio.error`: a MediaError, which has
    // a numeric code and no name.
    if (typeof code === "number" && typeof name !== "string")
      return MEDIA_ERROR_KINDS[code] ?? "other"
  }
  return "other"
}

/** A Sentry report whose type carries the kind: the scrubber drops messages. */
class FarewellPlaybackError extends Error {
  constructor(kind: PlaybackErrorKind) {
    super(kind)
    this.name = `FarewellPlaybackError[${kind}]`
  }
}

/** The clip's exact bytes. Byte stream chunks are views into larger packets
 *  (protobuf decodes `bytes` with subarray), so each is copied by its own
 *  offset and length, never through its underlying buffer. */
export function joinChunks(chunks: ReadonlyArray<Uint8Array>) {
  const size = chunks.reduce((total, chunk) => total + chunk.byteLength, 0)
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return bytes
}

// Chrome can route a whole AudioContext to a chosen output; Safari cannot,
// and the DOM typings do not declare it yet.
type RoutableAudioContext = AudioContext & {
  setSinkId?: (sinkId: string) => Promise<void>
}

type FarewellStatus =
  "pending" | "played" | "failed" | "timeout" | "not_possible"
type Attempt = {
  closingId: string
  streamId: string
  attemptId: string
  identity: string
  text: string
}

type Callbacks = {
  onClosing: (text: string, closingId: string) => void
  onCompleted: (status: FarewellStatus) => void
  onBlocked: (blocked: boolean) => void
  onError: (message: string) => void
  onRecoveryPending?: () => void
}

/** Finite browser playback. Sending bytes or receiving RTP is not completion. */
export class FarewellPlayback {
  private attempt: Attempt | null = null
  private audio: HTMLAudioElement | null = null
  private audioUrl: string | null = null
  // The Web Audio path: a decoded clip waits here until its context runs.
  private clip: { context: AudioContext; buffer: AudioBuffer } | null = null
  private source: AudioBufferSourceNode | null = null
  private readAbort: AbortController | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private received = false
  private played = false
  private expired = false
  private finished = false
  private disposed = false
  private startedAt: number | null = null
  private audioOutput: "selected" | "default" = "default"
  private pollTimer: ReturnType<typeof setTimeout> | null = null
  private waitTimer: ReturnType<typeof setTimeout> | null = null
  private waitDeadline: number | null = null
  private observedClosingId: string | null = null
  private recoveryPending = false
  private sealed = false
  private pendingCompletion: FarewellStatus | null = null
  private pendingAck: PlaybackAcknowledgement | null = null
  private ackInFlight = false
  private ackExpired = false
  private supervisionEpoch = 0

  constructor(
    private room: Room,
    private conversationId: string,
    private callbacks: Callbacks,
    private participantToken: string,
    private makeAudio: () => HTMLAudioElement = () => new Audio(),
    /** The session's AudioContext, created and resumed from a click: Safari
     *  only lets a context started by a user gesture make sound later. */
    private audioContext: () => RoutableAudioContext | null = () => null
  ) {
    room.on(RoomEvent.DataReceived, this.onControl)
    room.registerByteStreamHandler(AUDIO_TOPIC, (reader, participant) => {
      void this.receive(reader, participant.identity).catch(() => {
        // The byte stream broke or arrived short: no media error to name.
        this.failPlayback("Could not receive the farewell audio.", "stream")
      })
    })
  }

  get isClosing() {
    return (
      (this.attempt !== null || this.observedClosingId !== null) &&
      !this.finished
    )
  }

  get isFinished() {
    return this.finished
  }

  private shouldIgnoreAudio() {
    return this.finished || this.disposed || this.expired
  }

  private isAgent(identity: string) {
    return this.room.remoteParticipants.get(identity)?.isAgent === true
  }

  /** Observe durable closing even when RTC delivers neither control nor audio. */
  superviseClosing(closingId: string | null = null) {
    if (this.finished || this.disposed) return
    if (
      closingId &&
      this.observedClosingId &&
      closingId !== this.observedClosingId
    )
      return
    if (closingId) this.observedClosingId = closingId
    if (this.waitDeadline !== null) return
    this.waitDeadline = performance.now() + 45_000
    this.armWait()
    void this.poll(++this.supervisionEpoch)
  }

  cancelUnacceptedClose() {
    if (this.isClosing || this.finished) return false
    ++this.supervisionEpoch
    if (this.pollTimer) clearTimeout(this.pollTimer)
    if (this.waitTimer) clearTimeout(this.waitTimer)
    this.waitDeadline = null
    return true
  }

  private silenceInterviewer() {
    for (const participant of this.room.remoteParticipants.values())
      if (participant.isAgent) participant.setVolume(0)
  }

  private begin(attempt: Attempt, timeoutSeconds: number) {
    if (this.finished || this.disposed || this.recoveryPending) return false
    if (this.observedClosingId && this.observedClosingId !== attempt.closingId)
      return false
    if (this.attempt) {
      return (
        this.attempt.closingId === attempt.closingId &&
        this.attempt.streamId === attempt.streamId &&
        this.attempt.attemptId === attempt.attemptId
      )
    }
    this.attempt = attempt
    this.superviseClosing(attempt.closingId)
    this.silenceInterviewer()
    this.callbacks.onClosing(attempt.text, attempt.closingId)
    void this.room.localParticipant.setMicrophoneEnabled(false).catch(() => {
      this.callbacks.onError("Could not disable the microphone during closing.")
    })
    this.timer = setTimeout(
      () => {
        if (this.played || this.expired) return
        this.expired = true
        this.stopAudio()
        this.callbacks.onError(
          "Farewell playback timed out. Waiting for the saved result."
        )
        void this.acknowledge("timeout")
        if (this.pendingCompletion && this.sealed)
          this.complete(this.pendingCompletion)
      },
      Math.min(
        60,
        Math.max(5, Number.isFinite(timeoutSeconds) ? timeoutSeconds : 20)
      ) * 1000
    )
    return true
  }

  private armWait() {
    if (this.waitTimer) clearTimeout(this.waitTimer)
    if (this.waitDeadline === null) return
    this.waitTimer = setTimeout(
      () => {
        if (this.finished || this.disposed || this.recoveryPending) return
        this.recoveryPending = true
        if (this.pollTimer) clearTimeout(this.pollTimer)
        this.callbacks.onError(
          "Closing recovery is pending. You can check the saved interview later."
        )
        this.callbacks.onRecoveryPending?.()
      },
      Math.max(0, this.waitDeadline - performance.now())
    )
  }

  private async poll(epoch = this.supervisionEpoch) {
    if (this.finished || this.disposed || this.waitDeadline === null) return
    if (performance.now() >= this.waitDeadline) return
    try {
      await this.flushAck()
      if (this.shouldStopPolling() || epoch !== this.supervisionEpoch) return
      const started = performance.now()
      const state = await getClosingState(
        this.conversationId,
        this.participantToken,
        AbortSignal.timeout(2000)
      )
      if (this.shouldStopPolling() || epoch !== this.supervisionEpoch) return
      if (
        state.closing_id &&
        (!this.observedClosingId || state.closing_id === this.observedClosingId)
      ) {
        if (
          state.remaining_seconds !== null &&
          Number.isFinite(state.remaining_seconds)
        ) {
          this.waitDeadline = Math.min(
            this.waitDeadline,
            started + Math.max(0, state.remaining_seconds) * 1000
          )
          this.armWait()
        }
        this.acceptPersistedState(
          state.status,
          state.closing_id,
          state.farewell_status,
          state.transcript_sealed
        )
      }
    } catch {
      // The next poll is independent of RTC and keeps the same deadline.
    }
    if (
      !this.shouldStopPolling() &&
      epoch === this.supervisionEpoch &&
      performance.now() < this.waitDeadline
    ) {
      this.pollTimer = setTimeout(() => void this.poll(epoch), 1000)
    }
  }

  private shouldStopPolling() {
    return (
      this.finished ||
      this.disposed ||
      this.recoveryPending ||
      this.waitDeadline === null
    )
  }

  private isDisposed() {
    return this.disposed
  }

  private onControl = (
    payload: Uint8Array,
    participant: { identity: string } | undefined,
    _kind: unknown,
    topic: string | undefined
  ) => this.control(payload, participant?.identity, topic)

  /** Bind the exact DataReceived contract while keeping callbacks removable. */
  private control = (
    payload: Uint8Array,
    identity: string | undefined,
    topic: string | undefined
  ) => {
    if (
      topic !== CONTROL_TOPIC ||
      !identity ||
      !this.isAgent(identity) ||
      this.finished ||
      this.disposed
    )
      return
    let data: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(new TextDecoder().decode(payload))
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return
      data = parsed as Record<string, unknown>
    } catch {
      return
    }
    if (data.conversation_id !== this.conversationId) return
    if (
      data.event === "closing" &&
      typeof data.closing_id === "string" &&
      typeof data.stream_id === "string" &&
      typeof data.attempt_id === "string"
    ) {
      this.begin(
        {
          closingId: data.closing_id,
          streamId: data.stream_id,
          attemptId: data.attempt_id,
          identity,
          text: typeof data.text === "string" ? data.text : "",
        },
        typeof data.timeout_seconds === "number" ? data.timeout_seconds : 20
      )
    } else if (data.event === "completed") {
      this.acceptCompletion(data.closing_id, data.farewell_status)
    }
  }

  private async receive(reader: ByteStreamReader, identity: string) {
    if (this.finished || this.disposed || !this.isAgent(identity)) return
    const attrs = reader.info.attributes ?? {}
    if (
      attrs.conversation_id !== this.conversationId ||
      !attrs.closing_id ||
      !attrs.attempt_id ||
      reader.info.mimeType !== "audio/wav" ||
      !reader.info.size ||
      reader.info.size > MAX_CLIP_BYTES
    )
      return
    if (
      !this.begin(
        {
          closingId: attrs.closing_id,
          streamId: reader.info.id,
          attemptId: attrs.attempt_id,
          identity,
          text: this.attempt?.text ?? "",
        },
        20
      ) ||
      this.received ||
      this.expired
    )
      return
    this.received = true
    this.readAbort = new AbortController()
    const chunks = await reader.readAll({ signal: this.readAbort.signal })
    if (this.shouldIgnoreAudio()) return
    const bytes = joinChunks(chunks)
    if (bytes.byteLength !== reader.info.size)
      throw new Error("Incomplete farewell clip")
    const output = this.room.options.audioOutput?.deviceId
    if (await this.prepareDecoded(bytes, output)) {
      await this.resume()
      return
    }
    if (this.shouldIgnoreAudio()) return
    this.audioUrl = URL.createObjectURL(
      new Blob([bytes], { type: "audio/wav" })
    )
    const audio = this.makeAudio()
    this.audio = audio
    audio.src = this.audioUrl
    if (output && typeof audio.setSinkId === "function") {
      try {
        await audio.setSinkId(output)
        this.audioOutput = "selected"
      } catch (error) {
        this.failPlayback(
          "Could not use the selected audio output.",
          playbackErrorKind(error)
        )
        return
      }
    }
    audio.onplaying = () => {
      if (this.startedAt === null) this.startedAt = performance.now()
      this.callbacks.onBlocked(false)
    }
    audio.onended = () => this.onClipEnded()
    audio.onerror = () =>
      this.failPlayback(
        "Could not play the farewell audio.",
        playbackErrorKind(audio.error)
      )
    await this.resume()
  }

  /** Decodes the clip for the session's AudioContext. False leaves the media
   *  element to play it: no context, an output the context cannot route to,
   *  or bytes this browser's decoder refuses. */
  private async prepareDecoded(bytes: Uint8Array, output: string | undefined) {
    const context = this.audioContext()
    if (!context || context.state === "closed") return false
    if (output && typeof context.setSinkId !== "function") return false
    let buffer: AudioBuffer
    try {
      // decodeAudioData detaches what it is given: decode a copy, so the
      // element fallback still has the bytes.
      buffer = await context.decodeAudioData(bytes.slice().buffer)
    } catch {
      return false
    }
    if (this.shouldIgnoreAudio()) return false
    if (output && context.setSinkId) {
      try {
        await context.setSinkId(output)
        this.audioOutput = "selected"
      } catch (error) {
        this.failPlayback(
          "Could not use the selected audio output.",
          playbackErrorKind(error)
        )
        return true
      }
    }
    this.clip = { context, buffer }
    return true
  }

  private onClipEnded() {
    if (this.finished || this.disposed || this.expired) return
    this.played = true
    if (this.timer) clearTimeout(this.timer)
    void this.confirmPlayed()
  }

  private async confirmPlayed() {
    const result = await this.acknowledge("played")
    if (this.finished || this.disposed) return
    if (result?.status === "played" && this.sealed) {
      this.complete("played")
    }
    // A provisional or unavailable ACK still needs durable promotion. Polling
    // keeps the independent transcript seal barrier and the original deadline.
  }

  /** Starts the clip; also the "Enable audio" click, whose gesture is what
   *  lets a suspended context or a blocked element start. */
  async resume() {
    if (this.finished || this.expired || this.disposed) return
    if (this.clip) return this.startDecoded(this.clip)
    if (!this.audio) return
    try {
      await this.audio.play()
      this.callbacks.onBlocked(false)
    } catch (error) {
      if (error instanceof DOMException && error.name === "NotAllowedError") {
        this.callbacks.onBlocked(true)
      } else {
        this.failPlayback(
          "Could not play the farewell audio.",
          playbackErrorKind(error)
        )
      }
    }
  }

  private async startDecoded(clip: {
    context: AudioContext
    buffer: AudioBuffer
  }) {
    if (this.source) return
    const { context } = clip
    if (context.state !== "running") {
      // Called synchronously from a click, this is the gesture Safari needs.
      const resumed = context.resume().then(
        () => true,
        () => false
      )
      await Promise.race([
        resumed,
        new Promise((resolve) => setTimeout(resolve, RESUME_WAIT_MS)),
      ])
      // A click may have started the clip while this call waited.
      if (
        this.clip !== clip ||
        this.isPlayingClip() ||
        this.shouldIgnoreAudio()
      )
        return
      if ((context.state as string) !== "running") {
        // Autoplay was refused, as play()'s NotAllowedError would say.
        this.callbacks.onBlocked(true)
        return
      }
    }
    const source = context.createBufferSource()
    source.buffer = clip.buffer
    source.connect(context.destination)
    source.onended = () => this.onClipEnded()
    try {
      source.start()
    } catch (error) {
      this.failPlayback(
        "Could not play the farewell audio.",
        playbackErrorKind(error)
      )
      return
    }
    this.source = source
    if (this.startedAt === null) this.startedAt = performance.now()
    this.callbacks.onBlocked(false)
  }

  private isPlayingClip() {
    return this.source !== null
  }

  private failPlayback(message: string, kind: PlaybackErrorKind) {
    if (this.disposed || this.finished || this.expired || this.played) return
    if (this.timer) clearTimeout(this.timer)
    this.expired = true
    this.stopAudio()
    this.callbacks.onBlocked(false)
    this.callbacks.onError(message)
    captureRouteError(new FarewellPlaybackError(kind))
    void this.acknowledge("failed", kind)
    if (this.pendingCompletion && this.sealed)
      this.complete(this.pendingCompletion)
  }

  private async acknowledge(
    status: "played" | "failed" | "timeout",
    errorKind?: PlaybackErrorKind
  ) {
    if (!this.attempt || this.disposed) return
    if (this.pendingAck?.status !== "played") {
      this.pendingAck = {
        closing_id: this.attempt.closingId,
        stream_id: this.attempt.streamId,
        attempt_id: this.attempt.attemptId,
        status,
        duration_seconds:
          this.startedAt === null
            ? null
            : (performance.now() - this.startedAt) / 1000,
        audio_output: this.audioOutput,
        ...(status === "failed" && errorKind ? { error_kind: errorKind } : {}),
      }
    }
    return this.flushAck(3)
  }

  private async flushAck(attempts = 1) {
    if (!this.pendingAck || this.ackInFlight || this.shouldStopPolling()) return
    let data = this.pendingAck
    this.ackInFlight = true
    try {
      for (let i = 0; i < attempts; i++) {
        if (this.shouldStopPolling()) return
        try {
          const result = await acknowledgeFarewell(
            this.conversationId,
            this.participantToken,
            data,
            AbortSignal.timeout(2000)
          )
          if (result.accepted || result.reason === "expired") {
            if (result.reason === "expired") this.ackExpired = true
            // A provisional ACK still needs delivery proof. Retain its original
            // payload and retry until promotion or expiry, without replaying audio.
            if (!result.provisional && this.pendingAck === data)
              this.pendingAck = null
            if (this.played && this.sealed) {
              if (result.status === "played") this.complete("played")
              else if (result.reason === "expired" && this.pendingCompletion)
                this.complete(this.pendingCompletion)
            }
            return result
          }
        } catch (error) {
          // An API deployed before `error_kind` existed refuses the unknown
          // field (422): the outcome matters more than its reason.
          if (
            data.error_kind &&
            (error as { status?: unknown } | null)?.status === 422 &&
            this.pendingAck === data
          ) {
            const { error_kind: _dropped, ...plain } = data
            data = plain
            this.pendingAck = data
          }
          // The next poll retries the same ended evidence after a brief outage.
        }
        if (i < attempts - 1)
          await new Promise((resolve) => setTimeout(resolve, 100 * (i + 1)))
      }
      if (!this.isDisposed() && attempts > 1)
        this.callbacks.onError(
          "Playback confirmation is pending. Waiting for saved state."
        )
    } finally {
      this.ackInFlight = false
    }
  }

  acceptPersistedState(
    status: string,
    closingId: string | null,
    farewellStatus: string | null,
    transcriptSealed = false
  ) {
    if (this.attempt && closingId !== this.attempt.closingId) return
    if (this.observedClosingId && closingId !== this.observedClosingId) return
    if (status === "error") {
      // The server could not persist the closing: no seal will ever arrive.
      this.complete(
        farewellStatus === "played" ||
          farewellStatus === "timeout" ||
          farewellStatus === "not_possible"
          ? farewellStatus
          : "failed"
      )
      return
    }
    if (closingId && (status === "closing" || transcriptSealed)) {
      const newlyObserved = this.observedClosingId === null
      this.superviseClosing(closingId)
      if (newlyObserved && status === "closing") {
        this.silenceInterviewer()
        this.callbacks.onClosing("", closingId)
      }
    }
    this.sealed ||= transcriptSealed
    if (!this.sealed) return
    if (
      [
        "completed",
        "evaluating",
        "evaluated",
        "evaluation_failed",
        "error",
      ].includes(status)
    ) {
      if (
        !this.attempt &&
        closingId &&
        ["played", "failed", "timeout", "not_possible"].includes(
          farewellStatus ?? ""
        )
      ) {
        this.complete(farewellStatus as FarewellStatus)
        return
      }
      // The server sealed without any closing this tab could play (worker
      // lost, abandoned): there is no clip to wait for, never a `played`.
      if (!this.attempt && !closingId && !this.observedClosingId) {
        this.complete(
          farewellStatus === "failed" || farewellStatus === "timeout"
            ? farewellStatus
            : "not_possible"
        )
        return
      }
      this.acceptCompletion(closingId, farewellStatus)
    }
  }

  private acceptCompletion(closingId: unknown, status: unknown) {
    if (
      !this.attempt ||
      closingId !== this.attempt.closingId ||
      this.finished ||
      this.disposed
    )
      return
    if (
      !["played", "failed", "timeout", "not_possible"].includes(String(status))
    )
      return
    const value = status as FarewellStatus
    if (!this.sealed) {
      this.pendingCompletion = value
      return
    }
    if (this.recoveryPending) {
      // Supervision has ended and no ACK will be sent: the saved outcome is final.
      this.complete(value)
      return
    }
    if (
      !this.played &&
      !this.expired &&
      (this.audio || this.clip || value === "played")
    ) {
      this.pendingCompletion = value
      return
    }
    if (
      this.played &&
      value !== "played" &&
      !this.expired &&
      !this.ackExpired
    ) {
      // A saved timeout/failure may still be promoted by our ended ACK.
      this.pendingCompletion = value
      return
    }
    this.complete(value)
  }

  private complete(status: FarewellStatus) {
    if (this.finished || this.disposed) return
    this.finished = true
    if (this.timer) clearTimeout(this.timer)
    if (this.pollTimer) clearTimeout(this.pollTimer)
    if (this.waitTimer) clearTimeout(this.waitTimer)
    this.stopAudio()
    this.callbacks.onBlocked(false)
    this.callbacks.onCompleted(status)
  }

  private stopAudio() {
    this.readAbort?.abort()
    if (this.source) {
      this.source.onended = null
      try {
        this.source.stop()
      } catch {
        // Never started or already stopped: nothing is playing either way.
      }
      this.source.disconnect()
      this.source = null
    }
    // The context belongs to the session, which closes it.
    this.clip = null
    if (this.audio) {
      this.audio.onended = null
      this.audio.onplaying = null
      this.audio.onerror = null
      this.audio.pause()
      this.audio.removeAttribute("src")
      this.audio = null
    }
    if (this.audioUrl) {
      URL.revokeObjectURL(this.audioUrl)
      this.audioUrl = null
    }
  }

  dispose() {
    this.disposed = true
    if (this.timer) clearTimeout(this.timer)
    if (this.pollTimer) clearTimeout(this.pollTimer)
    if (this.waitTimer) clearTimeout(this.waitTimer)
    this.stopAudio()
    this.room.off(RoomEvent.DataReceived, this.onControl)
    this.room.unregisterByteStreamHandler(AUDIO_TOPIC)
  }
}

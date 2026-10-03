import { RoomEvent } from "livekit-client"
import type { Room, RpcInvocationData } from "livekit-client"
import { getClosingState } from "./api"
import type { PlaybackErrorKind } from "./api"
import { captureRouteError } from "./error-reporting"

const CONTROL_TOPIC = "interview.control"
const READY_METHOD = "interview.farewell_ready"
const DOM_ERROR_KINDS: ReadonlySet<string> = new Set([
  "NotAllowedError",
  "NotSupportedError",
  "AbortError",
  "NotFoundError",
  "EncodingError",
  "InvalidStateError",
  "SecurityError",
])

export function playbackErrorKind(error: unknown): PlaybackErrorKind {
  const name = (error as { name?: unknown } | null)?.name
  return typeof name === "string" && DOM_ERROR_KINDS.has(name)
    ? (name as PlaybackErrorKind)
    : "other"
}

class FarewellPlaybackError extends Error {
  constructor(kind: PlaybackErrorKind) {
    super(kind)
    this.name = `FarewellPlaybackError[${kind}]`
  }
}

type FarewellStatus =
  "pending" | "played" | "failed" | "timeout" | "not_possible"
type Attempt = { closingId: string; attemptId: string; identity: string }
type Callbacks = {
  onClosing: (text: string, closingId: string) => void
  onCompleted: (status: FarewellStatus) => void
  onBlocked: (blocked: boolean) => void
  onError: (message: string) => void
  onRecoveryPending?: () => void
}

function parsePayload(payload: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(payload)
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

/** Native speech stays on RoomAudioRenderer. No second player or browser-ended
 *  ACK: readiness is not completion, and saved success is agent playout only. */
export class FarewellPlayback {
  private attempt: Attempt | null = null
  private observedClosingId: string | null = null
  private finished = false
  private disposed = false
  private recoveryPending = false
  private waitDeadline: number | null = null
  private pollTimer: ReturnType<typeof setTimeout> | null = null
  private waitTimer: ReturnType<typeof setTimeout> | null = null
  private supervisionEpoch = 0
  private readyWaiters = new Set<() => void>()

  constructor(
    private room: Room,
    private conversationId: string,
    private callbacks: Callbacks,
    private participantToken: string
  ) {
    room.on(RoomEvent.DataReceived, this.onControl)
    room.on(RoomEvent.AudioPlaybackStatusChanged, this.onAudioStatus)
    room.on(RoomEvent.ParticipantDisconnected, this.wakeReady)
    room.on(RoomEvent.Disconnected, this.wakeReady)
    room.localParticipant.registerRpcMethod(READY_METHOD, this.onReady)
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

  private active() {
    return !this.finished && !this.disposed && !this.recoveryPending
  }

  private isAgent(identity: string) {
    return this.room.remoteParticipants.get(identity)?.isAgent === true
  }

  superviseClosing(closingId: string | null = null) {
    if (!this.active()) return
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

  private begin(data: Record<string, unknown>, identity: string) {
    if (
      !this.active() ||
      !this.isAgent(identity) ||
      data.conversation_id !== this.conversationId ||
      typeof data.closing_id !== "string" ||
      typeof data.attempt_id !== "string"
    )
      return false
    if (this.observedClosingId && data.closing_id !== this.observedClosingId)
      return false
    if (this.attempt)
      return (
        this.attempt.closingId === data.closing_id &&
        this.attempt.attemptId === data.attempt_id &&
        this.attempt.identity === identity
      )
    this.attempt = {
      closingId: data.closing_id,
      attemptId: data.attempt_id,
      identity,
    }
    const previouslyObserved = this.observedClosingId !== null
    this.superviseClosing(data.closing_id)
    // The normal transcription stream supplies the spoken farewell. Never
    // create a synthetic bubble that would duplicate its SDK segment.
    if (!previouslyObserved) this.callbacks.onClosing("", data.closing_id)
    void this.room.localParticipant.setMicrophoneEnabled(false).catch(() => {
      this.callbacks.onError("Could not disable the microphone during closing.")
    })
    this.onAudioStatus()
    return true
  }

  private onControl = (
    payload: Uint8Array,
    participant: { identity: string } | undefined,
    _kind: unknown,
    topic: string | undefined
  ) => {
    if (
      topic !== CONTROL_TOPIC ||
      !participant ||
      !this.isAgent(participant.identity) ||
      !this.active()
    )
      return
    const data = parsePayload(new TextDecoder().decode(payload))
    if (!data || data.conversation_id !== this.conversationId) return
    if (data.event === "closing" && data.transport === "rtc")
      this.begin(data, participant.identity)
    // Completion packets cannot bypass the durable transcript seal.
    if (
      data.event === "completed" &&
      data.closing_id === this.observedClosingId
    )
      void this.poll()
  }

  private onReady = async (invocation: RpcInvocationData) => {
    const data = parsePayload(invocation.payload)
    if (!data || !this.begin(data, invocation.callerIdentity)) {
      return JSON.stringify({ ready: false })
    }
    if (!this.room.canPlaybackAudio) {
      await new Promise<void>((resolve) => {
        const finish = () => {
          clearTimeout(timer)
          this.readyWaiters.delete(wake)
          resolve()
        }
        const wake = () => {
          if (
            this.room.canPlaybackAudio ||
            this.room.state !== "connected" ||
            !this.active() ||
            !this.isAgent(invocation.callerIdentity)
          )
            finish()
        }
        const timer = setTimeout(
          finish,
          Math.max(0, Math.min(20_000, invocation.responseTimeout - 100))
        )
        this.readyWaiters.add(wake)
        wake()
      })
    }
    return JSON.stringify({
      ready:
        this.active() &&
        this.room.state === "connected" &&
        this.room.canPlaybackAudio &&
        this.isAgent(invocation.callerIdentity),
    })
  }

  private wakeReady = () => {
    for (const wake of [...this.readyWaiters]) wake()
  }

  private onAudioStatus = () => {
    if (this.isClosing && this.active())
      this.callbacks.onBlocked(!this.room.canPlaybackAudio)
    this.wakeReady()
  }

  /** Called directly from the Enable audio gesture; resumes the existing RTC
   *  renderer, never replays speech or creates a separate audio context. */
  async resume() {
    if (!this.active()) return
    try {
      await this.room.startAudio()
      this.onAudioStatus()
    } catch (error) {
      const kind = playbackErrorKind(error)
      if (kind === "NotAllowedError") this.callbacks.onBlocked(true)
      else {
        this.callbacks.onError("Could not enable the interviewer's audio.")
        captureRouteError(new FarewellPlaybackError(kind))
      }
    }
  }

  private armWait() {
    if (this.waitTimer) clearTimeout(this.waitTimer)
    if (this.waitDeadline === null) return
    this.waitTimer = setTimeout(
      () => {
        if (!this.active()) return
        this.recoveryPending = true
        if (this.pollTimer) clearTimeout(this.pollTimer)
        this.callbacks.onError(
          "Closing recovery is pending. You can check the saved interview later."
        )
        this.callbacks.onRecoveryPending?.()
        this.wakeReady()
      },
      Math.max(0, this.waitDeadline - performance.now())
    )
  }

  private async poll(epoch = this.supervisionEpoch) {
    if (
      !this.active() ||
      this.waitDeadline === null ||
      performance.now() >= this.waitDeadline
    )
      return
    try {
      const started = performance.now()
      const state = await getClosingState(
        this.conversationId,
        this.participantToken,
        AbortSignal.timeout(2000)
      )
      if (!this.active() || epoch !== this.supervisionEpoch) return
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
      /* The next poll keeps the same deadline and is independent of RTC. */
    }
    this.queuePoll(epoch)
  }

  private queuePoll(epoch: number) {
    if (
      this.active() &&
      epoch === this.supervisionEpoch &&
      this.waitDeadline !== null &&
      performance.now() < this.waitDeadline
    ) {
      if (this.pollTimer) clearTimeout(this.pollTimer)
      this.pollTimer = setTimeout(() => void this.poll(epoch), 1000)
    }
  }

  acceptPersistedState(
    status: string,
    closingId: string | null,
    farewellStatus: string | null,
    transcriptSealed = false
  ) {
    if (this.finished || this.disposed) return
    if (this.observedClosingId && closingId !== this.observedClosingId) return
    if (
      closingId &&
      (status === "closing" || transcriptSealed) &&
      !this.observedClosingId
    ) {
      this.superviseClosing(closingId)
      if (status === "closing") this.callbacks.onClosing("", closingId)
    }
    if (status !== "error" && !transcriptSealed) return
    if (
      ![
        "completed",
        "evaluating",
        "evaluated",
        "evaluation_failed",
        "error",
      ].includes(status)
    )
      return
    const value = ["played", "failed", "timeout", "not_possible"].includes(
      farewellStatus ?? ""
    )
      ? (farewellStatus as FarewellStatus)
      : status === "error"
        ? "failed"
        : "not_possible"
    this.finished = true
    this.clearTimers()
    this.wakeReady()
    this.callbacks.onBlocked(false)
    this.callbacks.onCompleted(value)
  }

  private clearTimers() {
    if (this.pollTimer) clearTimeout(this.pollTimer)
    if (this.waitTimer) clearTimeout(this.waitTimer)
  }

  dispose() {
    this.disposed = true
    this.clearTimers()
    this.wakeReady()
    this.room.off(RoomEvent.DataReceived, this.onControl)
    this.room.off(RoomEvent.AudioPlaybackStatusChanged, this.onAudioStatus)
    this.room.off(RoomEvent.ParticipantDisconnected, this.wakeReady)
    this.room.off(RoomEvent.Disconnected, this.wakeReady)
    this.room.localParticipant.unregisterRpcMethod(READY_METHOD)
  }
}

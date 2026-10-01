/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { RoomEvent } from "livekit-client"
import type { ByteStreamReader, Room } from "livekit-client"
import { FarewellPlayback } from "./farewell"
import { acknowledgeFarewell, getClosingState } from "./api"

vi.mock("./api", () => ({
  acknowledgeFarewell: vi.fn(),
  getClosingState: vi.fn(),
}))

class AudioDouble {
  src = ""
  onended: (() => void) | null = null
  onplaying: (() => void) | null = null
  onerror: (() => void) | null = null
  play = vi.fn().mockResolvedValue(undefined)
  pause = vi.fn()
  removeAttribute = vi.fn()
  setSinkId = vi.fn().mockResolvedValue(undefined)
}

function fixture() {
  const listeners = new Map<string, (...args: unknown[]) => void>()
  const handlers = new Map<
    string,
    (reader: ByteStreamReader, participant: { identity: string }) => void
  >()
  const audio = new AudioDouble()
  const callbacks = {
    onClosing: vi.fn(),
    onCompleted: vi.fn(),
    onBlocked: vi.fn(),
    onError: vi.fn(),
    onRecoveryPending: vi.fn(),
  }
  const room = {
    options: { audioOutput: { deviceId: "chosen-speaker" } },
    remoteParticipants: new Map([
      ["agent", { isAgent: true, setVolume: vi.fn() }],
      ["candidate", { isAgent: false }],
    ]),
    localParticipant: {
      setMicrophoneEnabled: vi.fn().mockResolvedValue(undefined),
      performRpc: vi.fn().mockResolvedValue(JSON.stringify({ accepted: true })),
    },
    on: vi.fn((event, handler) => listeners.set(event, handler)),
    off: vi.fn(),
    registerByteStreamHandler: vi.fn((topic, handler) =>
      handlers.set(topic, handler)
    ),
    unregisterByteStreamHandler: vi.fn(),
  }
  const makeAudio = vi.fn(() => audio as unknown as HTMLAudioElement)
  const playback = new FarewellPlayback(
    room as unknown as Room,
    "conversation",
    callbacks,
    "candidate-token",
    makeAudio
  )
  const control = (event: string, extra = {}, identity = "agent") =>
    listeners.get(RoomEvent.DataReceived)!(
      new TextEncoder().encode(
        JSON.stringify({
          event,
          conversation_id: "conversation",
          closing_id: "closing",
          stream_id: "stream",
          attempt_id: "attempt",
          timeout_seconds: 20,
          text: "Goodbye.",
          ...extra,
        })
      ),
      { identity },
      undefined,
      "interview.control"
    )
  const receive = async (extra = {}, identity = "agent") => {
    const reader = {
      info: {
        id: "stream",
        mimeType: "audio/wav",
        size: 4,
        attributes: {
          conversation_id: "conversation",
          closing_id: "closing",
          attempt_id: "attempt",
        },
        ...extra,
      },
      readAll: vi.fn().mockResolvedValue([new Uint8Array([1, 2, 3, 4])]),
    }
    handlers.get("interview.farewell_audio")!(
      reader as unknown as ByteStreamReader,
      { identity }
    )
    // readAll, setSinkId and play are successive microtasks.
    for (let i = 0; i < 8; i++) await Promise.resolve()
    return reader
  }
  return { playback, callbacks, room, audio, control, receive, makeAudio }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.mocked(acknowledgeFarewell)
    .mockReset()
    .mockResolvedValue({ accepted: true })
  vi.mocked(getClosingState).mockReset().mockResolvedValue({
    status: "closing",
    closing_id: "closing",
    farewell_status: "pending",
    transcript_sealed: false,
    transcript_integrity: null,
    remaining_seconds: 45,
    playback_exceeded_budget: null,
  })
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:farewell"),
    revokeObjectURL: vi.fn(),
  })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("finite farewell playback", () => {
  it("bounds a requested close without any RTC control or clip", async () => {
    vi.mocked(getClosingState).mockRejectedValue(new Error("API unavailable"))
    const f = fixture()
    f.playback.superviseClosing()
    await vi.advanceTimersByTimeAsync(45_000)
    expect(f.callbacks.onRecoveryPending).toHaveBeenCalledOnce()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    expect(f.makeAudio).not.toHaveBeenCalled()
    expect(acknowledgeFarewell).not.toHaveBeenCalled()
    const calls = vi.mocked(getClosingState).mock.calls.length
    f.playback.superviseClosing()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getClosingState).toHaveBeenCalledTimes(calls)
    f.playback.dispose()
  })

  it("starts supervision from saved closing even without a transcript seal", async () => {
    vi.mocked(getClosingState).mockResolvedValue({
      status: "closing",
      closing_id: "closing",
      farewell_status: "pending",
      transcript_sealed: false,
      transcript_integrity: null,
      remaining_seconds: 2,
      playback_exceeded_budget: null,
    })
    const f = fixture()
    f.playback.acceptPersistedState("closing", "closing", "pending", false)
    expect(f.playback.isClosing).toBe(true)
    await vi.advanceTimersByTimeAsync(2000)
    expect(f.callbacks.onRecoveryPending).toHaveBeenCalledOnce()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    expect(acknowledgeFarewell).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it.each(["failed", "timeout", "not_possible"])(
    "keeps an already loaded clip playing when the sealed status is %s",
    async (status) => {
      let finishAck!: (value: { accepted: boolean; status: string }) => void
      vi.mocked(acknowledgeFarewell).mockImplementation(
        () =>
          new Promise((resolve) => {
            finishAck = resolve
          })
      )
      const f = fixture()
      f.control("closing")
      await f.receive()
      f.playback.acceptPersistedState("completed", "closing", status, true)
      expect(f.audio.pause).not.toHaveBeenCalled()
      expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(19_999)
      f.audio.onended?.()
      await vi.advanceTimersByTimeAsync(2)
      expect(f.audio.pause).not.toHaveBeenCalled()
      expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
      expect(vi.mocked(acknowledgeFarewell).mock.calls[0][2].status).toBe(
        "played"
      )
      finishAck({ accepted: true, status: "played" })
      for (let i = 0; i < 6; i++) await Promise.resolve()
      expect(f.callbacks.onCompleted).toHaveBeenCalledWith("played")
      expect(acknowledgeFarewell).toHaveBeenCalledOnce()
      expect(f.makeAudio).toHaveBeenCalledOnce()
      f.playback.dispose()
    }
  )

  it("confirms through the API after the worker disappears", async () => {
    const f = fixture()
    f.control("closing")
    await f.receive()
    expect(
      f.room.remoteParticipants.get("agent")?.setVolume
    ).toHaveBeenCalledWith(0)
    f.room.remoteParticipants.clear()
    f.audio.onended?.()
    await Promise.resolve()
    expect(acknowledgeFarewell).toHaveBeenCalledWith(
      "conversation",
      "candidate-token",
      expect.objectContaining({ status: "played" }),
      expect.any(AbortSignal)
    )
    expect(f.room.localParticipant.performRpc).not.toHaveBeenCalled()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.acceptPersistedState("completed", "closing", "played", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("played")
    f.playback.dispose()
  })

  it("retries a transient API ACK failure without replaying the clip", async () => {
    vi.mocked(acknowledgeFarewell).mockRejectedValueOnce(new Error("network"))
    const f = fixture()
    f.control("closing")
    await f.receive()
    f.audio.onended?.()
    await vi.advanceTimersByTimeAsync(150)
    expect(acknowledgeFarewell).toHaveBeenCalledTimes(2)
    expect(f.makeAudio).toHaveBeenCalledOnce()
    f.playback.dispose()
  })

  it("keeps retrying the original ended ACK after a seven-second API outage", async () => {
    let apiDown = true
    vi.mocked(acknowledgeFarewell).mockImplementation(async () => {
      if (apiDown) throw new Error("Temporary outage")
      return { accepted: true, status: "played", provisional: false }
    })
    const f = fixture()
    f.control("closing")
    await f.receive()
    f.playback.acceptPersistedState("completed", "closing", "timeout", true)
    f.audio.onended?.()
    await vi.advanceTimersByTimeAsync(7000)
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    expect(vi.mocked(acknowledgeFarewell).mock.calls.length).toBeGreaterThan(3)
    apiDown = false
    await vi.advanceTimersByTimeAsync(1100)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("played")
    const bodies = vi
      .mocked(acknowledgeFarewell)
      .mock.calls.map((call) => call[2])
    expect(
      bodies.every((body) => JSON.stringify(body) === JSON.stringify(bodies[0]))
    ).toBe(true)
    expect(f.makeAudio).toHaveBeenCalledOnce()
    f.playback.dispose()
  })

  it("retains a provisional ACK until promotion or expiry and reconciles sealed failure", async () => {
    vi.mocked(acknowledgeFarewell).mockResolvedValue({
      accepted: true,
      status: "timeout",
      provisional: true,
    })
    const f = fixture()
    f.control("closing")
    await f.receive()
    f.audio.onended?.()
    await vi.advanceTimersByTimeAsync(1000)
    expect(vi.mocked(acknowledgeFarewell).mock.calls.length).toBeGreaterThan(1)
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    vi.mocked(acknowledgeFarewell).mockResolvedValue({
      accepted: false,
      reason: "expired",
    })
    await vi.advanceTimersByTimeAsync(1000)
    f.playback.acceptPersistedState("completed", "closing", "timeout", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("timeout")
    expect(f.makeAudio).toHaveBeenCalledOnce()
    f.playback.dispose()
  })

  it("does not repeat a failed ACK or overwrite the playback error at timer expiry", async () => {
    const f = fixture()
    f.control("closing")
    await f.receive()
    f.audio.onerror?.()
    await vi.advanceTimersByTimeAsync(21_000)
    expect(acknowledgeFarewell).toHaveBeenCalledOnce()
    expect(f.callbacks.onError).toHaveBeenCalledOnce()
    expect(f.callbacks.onError).toHaveBeenCalledWith(
      "Could not play the farewell audio."
    )
    f.playback.dispose()
  })

  it.each(["play", "output"])(
    "cleans a permanent %s failure even if no media error event arrives",
    async (failure) => {
      const f = fixture()
      if (failure === "play")
        f.audio.play.mockRejectedValueOnce(new Error("Decode failure"))
      else
        f.audio.setSinkId.mockRejectedValueOnce(new Error("Unavailable output"))
      f.control("closing")
      await f.receive()
      await vi.advanceTimersByTimeAsync(21_000)
      expect(f.callbacks.onError).toHaveBeenCalledOnce()
      expect(acknowledgeFarewell).toHaveBeenCalledOnce()
      expect(vi.mocked(acknowledgeFarewell).mock.calls[0][2].status).toBe(
        "failed"
      )
      f.playback.acceptPersistedState("completed", "closing", "failed", true)
      expect(f.callbacks.onCompleted).toHaveBeenCalledWith("failed")
      f.playback.dispose()
    }
  )

  it("does not extend the server-relative wait on ownership recovery", async () => {
    vi.mocked(getClosingState).mockResolvedValueOnce({
      status: "closing",
      closing_id: "closing",
      farewell_status: "pending",
      transcript_sealed: false,
      transcript_integrity: null,
      remaining_seconds: 2,
      playback_exceeded_budget: null,
    })
    const f = fixture()
    f.control("closing")
    await vi.advanceTimersByTimeAsync(1999)
    expect(getClosingState).toHaveBeenCalledTimes(2)
    expect(f.callbacks.onRecoveryPending).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(f.callbacks.onRecoveryPending).toHaveBeenCalledOnce()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it("shows recovery pending after 45 seconds of API outage without fake completion", async () => {
    vi.mocked(getClosingState).mockRejectedValue(new Error("API unavailable"))
    const f = fixture()
    f.control("closing")
    await vi.advanceTimersByTimeAsync(45_000)
    expect(f.callbacks.onRecoveryPending).toHaveBeenCalledOnce()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    const calls = vi.mocked(getClosingState).mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    expect(getClosingState).toHaveBeenCalledTimes(calls)
    f.playback.dispose()
  })

  it("acknowledges only ended and preserves the selected audio output", async () => {
    const f = fixture()
    f.control("closing")
    await f.receive()
    expect(f.audio.play).toHaveBeenCalledOnce()
    expect(f.audio.setSinkId).toHaveBeenCalledWith("chosen-speaker")
    expect(f.room.localParticipant.performRpc).not.toHaveBeenCalled()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.audio.onplaying?.()
    f.audio.onended?.()
    await Promise.resolve()
    expect(vi.mocked(acknowledgeFarewell).mock.calls[0][2]).toMatchObject({
      closing_id: "closing",
      stream_id: "stream",
      attempt_id: "attempt",
      status: "played",
    })
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.control("completed", { farewell_status: "played" })
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.acceptPersistedState("completed", "closing", "played", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("played")
    expect(f.audio.pause).toHaveBeenCalled()
    f.playback.dispose()
  })

  it("defers a premature completion event until audio actually ends", async () => {
    const f = fixture()
    f.control("closing")
    f.control("completed", { farewell_status: "played" })
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    await f.receive()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.audio.onended?.()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.acceptPersistedState("completed", "closing", "played", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledOnce()
    f.playback.dispose()
  })

  it("never replays duplicate or late clips", async () => {
    const f = fixture()
    f.control("closing")
    await f.receive()
    await f.receive()
    expect(f.makeAudio).toHaveBeenCalledOnce()
    f.audio.onended?.()
    f.control("completed", { farewell_status: "played" })
    f.playback.acceptPersistedState("completed", "closing", "played", true)
    await f.receive()
    f.control("closing", { stream_id: "replacement" })
    expect(f.makeAudio).toHaveBeenCalledOnce()
    expect(f.callbacks.onClosing).toHaveBeenCalledOnce()
    f.playback.dispose()
  })

  it("rejects candidate-origin control and unrelated stream IDs", async () => {
    const f = fixture()
    f.control("closing", {}, "candidate")
    expect(f.callbacks.onClosing).not.toHaveBeenCalled()
    f.control("closing")
    await f.receive({ id: "wrong" })
    expect(f.audio.play).not.toHaveBeenCalled()
    await f.receive()
    expect(f.audio.play).toHaveBeenCalledOnce()
    f.playback.dispose()
  })

  it("allows gesture recovery after autoplay is blocked", async () => {
    const f = fixture()
    f.audio.play.mockRejectedValueOnce(
      new DOMException("Blocked", "NotAllowedError")
    )
    f.control("closing")
    await f.receive()
    expect(f.callbacks.onBlocked).toHaveBeenCalledWith(true)
    expect(f.room.localParticipant.performRpc).not.toHaveBeenCalled()
    await f.playback.resume()
    expect(f.audio.play).toHaveBeenCalledTimes(2)
    expect(f.callbacks.onBlocked).toHaveBeenLastCalledWith(false)
    f.audio.onended?.()
    f.playback.dispose()
  })

  it("times out playback without claiming that it played", async () => {
    const f = fixture()
    f.control("closing")
    await f.receive()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(f.audio.pause).toHaveBeenCalled()
    expect(vi.mocked(acknowledgeFarewell).mock.calls[0][2].status).toBe(
      "timeout"
    )
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.acceptPersistedState("completed", "closing", "timeout", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("timeout")
    f.playback.dispose()
  })

  it("ends the tab when the server could not persist the closing", () => {
    const f = fixture()
    f.control("closing")
    f.playback.acceptPersistedState("error", "closing", "pending", false)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("failed")
    f.playback.dispose()
  })

  it("accepts the saved outcome once recovery is pending", async () => {
    vi.mocked(getClosingState).mockRejectedValue(new Error("API unavailable"))
    vi.mocked(acknowledgeFarewell).mockRejectedValue(new Error("API down"))
    const f = fixture()
    f.control("closing")
    await f.receive()
    f.audio.onplaying?.()
    f.audio.onended?.()
    await vi.advanceTimersByTimeAsync(45_000)
    expect(f.callbacks.onRecoveryPending).toHaveBeenCalledOnce()
    // The page's own poll still delivers the sealed outcome: it is final now.
    f.playback.acceptPersistedState("completed", "closing", "timeout", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("timeout")
    f.playback.dispose()
  })

  it("can reconcile saved failure when the control packet was lost", () => {
    const f = fixture()
    f.playback.acceptPersistedState(
      "completed",
      "closing",
      "not_possible",
      true
    )
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("not_possible")
    f.playback.dispose()
  })

  it("reconciles played after reconnect without replaying a prior clip", () => {
    const f = fixture()
    f.playback.acceptPersistedState("evaluated", "closing", "played", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("played")
    expect(f.makeAudio).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it("rejects an incomplete finite clip", async () => {
    const f = fixture()
    f.control("closing")
    await f.receive({ size: 8 })
    expect(f.audio.play).not.toHaveBeenCalled()
    expect(f.callbacks.onError).toHaveBeenCalled()
    f.playback.dispose()
  })

  it("ends a live tab when the server seals without any closing", () => {
    const f = fixture()
    f.playback.acceptPersistedState("interviewing", null, null, false)
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.acceptPersistedState("completed", null, "not_possible", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("not_possible")
    expect(f.callbacks.onClosing).not.toHaveBeenCalled()
    expect(f.audio.play).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it("never turns an unsealed or foreign closing into a server-side end", () => {
    const f = fixture()
    f.playback.acceptPersistedState("completed", null, "not_possible", false)
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.control("closing")
    f.playback.acceptPersistedState("completed", null, "not_possible", true)
    expect(f.callbacks.onCompleted).not.toHaveBeenCalledWith("not_possible")
    f.playback.dispose()
  })

  it("reports which output played the farewell without naming a device", async () => {
    const f = fixture()
    f.control("closing")
    await f.receive()
    expect(f.audio.setSinkId).toHaveBeenCalledWith("chosen-speaker")
    f.audio.onended?.()
    await vi.advanceTimersByTimeAsync(1)
    const ack = vi.mocked(acknowledgeFarewell).mock.calls[0][2]
    expect(ack.audio_output).toBe("selected")
    f.playback.dispose()

    vi.mocked(acknowledgeFarewell).mockClear()
    const plain = fixture()
    plain.room.options = {} as typeof plain.room.options
    plain.control("closing")
    await plain.receive()
    plain.audio.onended?.()
    await vi.advanceTimersByTimeAsync(1)
    expect(vi.mocked(acknowledgeFarewell).mock.calls[0][2].audio_output).toBe(
      "default"
    )
    plain.playback.dispose()
  })
})

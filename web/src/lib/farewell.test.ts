/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { RoomEvent } from "livekit-client"
import type { Room, RpcInvocationData } from "livekit-client"
import { FarewellPlayback, playbackErrorKind } from "./farewell"
import { acknowledgeFarewell, getClosingState } from "./api"
import { captureRouteError } from "./error-reporting"

vi.mock("./api", () => ({
  acknowledgeFarewell: vi.fn(),
  getClosingState: vi.fn(),
}))
vi.mock("./error-reporting", () => ({ captureRouteError: vi.fn() }))

function fixture() {
  const listeners = new Map<string, (...args: unknown[]) => void>()
  const methods = new Map<
    string,
    (data: RpcInvocationData) => Promise<string>
  >()
  const callbacks = {
    onClosing: vi.fn(),
    onCompleted: vi.fn(),
    onBlocked: vi.fn(),
    onError: vi.fn(),
    onRecoveryPending: vi.fn(),
  }
  const room = {
    state: "connected",
    canPlaybackAudio: true,
    remoteParticipants: new Map([
      ["agent", { isAgent: true, setVolume: vi.fn() }],
      ["candidate", { isAgent: false, setVolume: vi.fn() }],
    ]),
    localParticipant: {
      setMicrophoneEnabled: vi.fn().mockResolvedValue(undefined),
      registerRpcMethod: vi.fn((name, handler) => methods.set(name, handler)),
      unregisterRpcMethod: vi.fn((name) => methods.delete(name)),
    },
    on: vi.fn((event, handler) => listeners.set(event, handler)),
    off: vi.fn(),
    registerByteStreamHandler: vi.fn(),
    startAudio: vi.fn(async () => {
      room.canPlaybackAudio = true
      listeners.get(RoomEvent.AudioPlaybackStatusChanged)?.()
    }),
  }
  const playback = new FarewellPlayback(
    room as unknown as Room,
    "conversation",
    callbacks,
    "token"
  )
  const payload = {
    conversation_id: "conversation",
    closing_id: "closing",
    attempt_id: "attempt",
    transport: "rtc",
  }
  const control = (event: string, extra = {}, identity = "agent") =>
    listeners.get(RoomEvent.DataReceived)!(
      new TextEncoder().encode(JSON.stringify({ ...payload, event, ...extra })),
      { identity },
      undefined,
      "interview.control"
    )
  const ready = (extra = {}, caller = "agent") =>
    methods.get("interview.farewell_ready")!({
      payload: JSON.stringify({ ...payload, ...extra }),
      callerIdentity: caller,
      responseTimeout: 20_000,
    } as RpcInvocationData).then(JSON.parse)
  return { room, playback, callbacks, control, ready, listeners, methods }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.mocked(acknowledgeFarewell).mockReset()
  vi.mocked(captureRouteError).mockReset()
  vi.mocked(getClosingState).mockReset().mockResolvedValue({
    status: "closing",
    closing_id: "closing",
    farewell_status: "pending",
    transcript_sealed: false,
    transcript_integrity: null,
    remaining_seconds: 45,
    playback_exceeded_budget: null,
  })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("native agent farewell supervision", () => {
  it("uses existing RTC audio without muting the agent, decoding a clip or sending a played ACK", async () => {
    const audio = vi.fn(),
      context = vi.fn()
    vi.stubGlobal("Audio", audio)
    vi.stubGlobal("AudioContext", context)
    const f = fixture()
    f.control("closing")
    expect(await f.ready()).toEqual({ ready: true })
    expect(f.callbacks.onClosing).toHaveBeenCalledOnce()
    expect(f.callbacks.onClosing).toHaveBeenCalledWith("", "closing")
    expect(f.room.localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(
      false
    )
    expect(
      f.room.remoteParticipants.get("agent")?.setVolume
    ).not.toHaveBeenCalled()
    expect(f.room.registerByteStreamHandler).not.toHaveBeenCalled()
    expect(audio).not.toHaveBeenCalled()
    expect(context).not.toHaveBeenCalled()
    expect(acknowledgeFarewell).not.toHaveBeenCalled()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it("waits for the gesture when RTC audio is blocked without replaying speech", async () => {
    const f = fixture()
    f.room.canPlaybackAudio = false
    let answered = false
    const waiting = f.ready().then((answer) => {
      answered = true
      return answer
    })
    await vi.advanceTimersByTimeAsync(3000)
    expect(answered).toBe(false)
    expect(f.callbacks.onBlocked).toHaveBeenCalledWith(true)
    await f.playback.resume()
    expect(await waiting).toEqual({ ready: true })
    expect(f.room.startAudio).toHaveBeenCalledOnce()
    expect(f.callbacks.onBlocked).toHaveBeenLastCalledWith(false)
    expect(acknowledgeFarewell).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it("bounds readiness when the browser never enables audio", async () => {
    const f = fixture()
    f.room.canPlaybackAudio = false
    const waiting = f.ready()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(await waiting).toEqual({ ready: false })
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.dispose()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("rejects unrelated identities, attempts and malformed readiness", async () => {
    const f = fixture()
    f.control("closing", {}, "candidate")
    expect(f.callbacks.onClosing).not.toHaveBeenCalled()
    expect(await f.ready({}, "candidate")).toEqual({ ready: false })
    expect(await f.ready({ conversation_id: "other" })).toEqual({
      ready: false,
    })
    f.control("closing")
    expect(await f.ready({ attempt_id: "other" })).toEqual({ ready: false })
    const invoke = f.methods.get("interview.farewell_ready")!
    expect(
      JSON.parse(
        await invoke({
          payload: "null",
          callerIdentity: "agent",
        } as RpcInvocationData)
      )
    ).toEqual({ ready: false })
    f.playback.dispose()
  })

  it("never lets a completion packet bypass the transcript seal", async () => {
    const f = fixture()
    f.control("closing")
    expect(await f.ready()).toEqual({ ready: true })
    f.control("completed", { farewell_status: "played" })
    f.playback.acceptPersistedState("completed", "closing", "played", false)
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.acceptPersistedState("completed", "closing", "played", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledOnce()
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("played")
    expect(acknowledgeFarewell).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it.each(["played", "failed", "timeout", "not_possible"])(
    "reconciles saved %s without a replay",
    (status) => {
      const f = fixture()
      f.playback.acceptPersistedState("evaluated", "closing", status, true)
      expect(f.callbacks.onCompleted).toHaveBeenCalledWith(status)
      expect(f.room.startAudio).not.toHaveBeenCalled()
      f.playback.dispose()
    }
  )

  it("keeps a fixed deadline through API outages without inventing success", async () => {
    vi.mocked(getClosingState).mockRejectedValue(
      new Error("Controlled API outage")
    )
    const f = fixture()
    f.playback.superviseClosing()
    await vi.advanceTimersByTimeAsync(45_000)
    expect(f.callbacks.onRecoveryPending).toHaveBeenCalledOnce()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    const calls = vi.mocked(getClosingState).mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    expect(getClosingState).toHaveBeenCalledTimes(calls)
    f.playback.acceptPersistedState("completed", "closing", "timeout", true)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("timeout")
    f.playback.dispose()
  })

  it("does not extend the database-relative closing deadline", async () => {
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
    f.control("closing")
    await vi.advanceTimersByTimeAsync(2000)
    expect(f.callbacks.onRecoveryPending).toHaveBeenCalledOnce()
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.dispose()
  })

  it("does not turn foreign or unsealed state into completion", () => {
    const f = fixture()
    f.control("closing")
    f.playback.acceptPersistedState("completed", "other", "played", true)
    f.playback.acceptPersistedState("completed", "closing", "played", false)
    expect(f.callbacks.onCompleted).not.toHaveBeenCalled()
    f.playback.acceptPersistedState("error", "closing", "pending", false)
    expect(f.callbacks.onCompleted).toHaveBeenCalledWith("failed")
    f.playback.dispose()
  })

  it("resolves blocked readiness on disconnect and unmount and removes the RPC", async () => {
    const f = fixture()
    f.room.canPlaybackAudio = false
    const waiting = f.ready()
    f.room.state = "disconnected"
    f.listeners.get(RoomEvent.Disconnected)!()
    expect(await waiting).toEqual({ ready: false })
    f.playback.dispose()
    expect(f.room.localParticipant.unregisterRpcMethod).toHaveBeenCalledWith(
      "interview.farewell_ready"
    )
    expect(vi.getTimerCount()).toBe(0)
    const g = fixture()
    g.room.canPlaybackAudio = false
    const pending = g.ready()
    g.playback.dispose()
    expect(await pending).toEqual({ ready: false })
    expect(vi.getTimerCount()).toBe(0)
  })

  it("reports only a bounded error category when RTC audio cannot be enabled", async () => {
    const f = fixture()
    f.room.startAudio.mockRejectedValueOnce(
      new DOMException("Private provider detail", "NotSupportedError")
    )
    await f.playback.resume()
    const error = vi.mocked(captureRouteError).mock.calls[0][0] as Error
    expect(error.name).toBe("FarewellPlaybackError[NotSupportedError]")
    expect(error.message).not.toContain("Private")
    expect(acknowledgeFarewell).not.toHaveBeenCalled()
    expect(playbackErrorKind(new Error("NotSupportedError"))).toBe("other")
    f.playback.dispose()
  })
})

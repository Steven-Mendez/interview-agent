/** @vitest-environment jsdom */
import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { RoomEvent, RpcError } from "livekit-client"
import type { TextStreamHandler, TextStreamReader } from "livekit-client"
import type * as LiveKit from "livekit-client"

import { useInterviewSession } from "./use-interview-session"

const mocks = vi.hoisted(() => {
  const rooms: MockRoom[] = []
  class MockRoom {
    handlers = new Map<string, TextStreamHandler>()
    remoteParticipants = new Map([
      ["agent", { identity: "agent", isAgent: true, setVolume: vi.fn() }],
    ])
    options = {}
    listeners = new Map<string, Array<() => void>>()
    localParticipant = {
      setMicrophoneEnabled: vi.fn().mockResolvedValue(undefined),
      performRpc: vi.fn().mockResolvedValue('{"accepted":true}'),
    }
    connect = vi.fn().mockResolvedValue(undefined)
    disconnect = vi.fn().mockResolvedValue(undefined)
    constructor() {
      rooms.push(this)
    }
    registerTextStreamHandler(topic: string, handler: TextStreamHandler) {
      this.handlers.set(topic, handler)
    }
    registerByteStreamHandler = vi.fn()
    unregisterByteStreamHandler = vi.fn()
    off = vi.fn()
    on(event: string, handler: () => void) {
      this.listeners.set(event, [...(this.listeners.get(event) ?? []), handler])
      return this
    }
    emit(event: string) {
      this.listeners.get(event)?.forEach((handler) => handler())
    }
    receive(
      reader: ControlledReader,
      identity = "candidate",
      topic = identity === "candidate"
        ? "interview.user_transcription"
        : "lk.transcription"
    ) {
      return Promise.resolve(
        this.handlers.get(topic)!(reader as unknown as TextStreamReader, {
          identity,
        })
      )
    }
  }

  // Holds of suppressUnauthorizedRedirect() not released yet.
  const redirectHolds = { count: 0 }

  return { rooms, Room: MockRoom, redirectHolds }
})

vi.mock("livekit-client", async (original) => ({
  ...(await original<typeof LiveKit>()),
  Room: mocks.Room,
}))
vi.mock("@/lib/api", () => ({
  acknowledgeFarewell: vi.fn().mockResolvedValue({ accepted: true }),
  getClosingState: vi.fn().mockRejectedValue(new Error("No closing state")),
  getInterviewToken: vi.fn().mockResolvedValue({
    server_url: "ws://test",
    token: "test",
    room: "test",
  }),
  ApiError: class extends Error {},
  suppressUnauthorizedRedirect: () => {
    mocks.redirectHolds.count += 1
    let released = false
    return () => {
      if (!released) mocks.redirectHolds.count -= 1
      released = true
    }
  },
}))

// A controllable stream with the installed SDK's public reader contract.
class ControlledReader {
  info: { id: string; attributes: Record<string, string> }
  signal?: AbortSignal
  queue: Array<string | Error | null> = []
  wake?: () => void
  constructor(id: string, final = false) {
    this.info = {
      id,
      attributes: {
        "lk.segment_id": id,
        "lk.transcription_final": String(final),
      },
    }
  }
  push(value: string | Error | null) {
    this.queue.push(value)
    this.wake?.()
  }
  withAbortSignal(signal: AbortSignal) {
    this.signal = signal
    return this
  }
  async *[Symbol.asyncIterator]() {
    for (;;) {
      if (this.signal?.aborted) throw new Error("Aborted")
      if (!this.queue.length) {
        await new Promise<void>((resolve, reject) => {
          const abort = () => reject(new Error("Aborted"))
          this.wake = () => {
            this.signal?.removeEventListener("abort", abort)
            resolve()
          }
          this.signal?.addEventListener("abort", abort, { once: true })
        })
      }
      const value = this.queue.shift()
      if (value instanceof Error) throw value
      if (value === null) return
      if (value !== undefined) yield value
    }
  }
  async readAll(options?: { signal?: AbortSignal }) {
    this.signal = options?.signal
    let text = ""
    for await (const chunk of this) text += chunk
    return text
  }
}

const flush = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve()
}

beforeEach(() => {
  mocks.rooms.length = 0
  vi.useFakeTimers()
  vi.spyOn(console, "log").mockImplementation(() => {})
  vi.spyOn(console, "warn").mockImplementation(() => {})
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

async function connected() {
  const hook = renderHook(() => useInterviewSession("test"))
  await act(async () => {
    hook.result.current.start()
    await flush()
  })
  return { ...hook, room: mocks.rooms[0] }
}

describe("interview transcription lifecycle", () => {
  it("joins muted when the pre-join check muted the microphone", async () => {
    const hook = renderHook(() => useInterviewSession("test"))
    await act(async () => {
      hook.result.current.start({ startMuted: true })
      await flush()
    })
    const mic = mocks.rooms[0].localParticipant.setMicrophoneEnabled
    expect(mic.mock.calls).toEqual([[true], [false]])
    expect(hook.result.current.phase).toBe("live")
    hook.unmount()
  })

  it("holds sign-in redirects back while the interview is in the room", async () => {
    const hook = renderHook(() => useInterviewSession("test"))
    expect(mocks.redirectHolds.count).toBe(0)
    await act(async () => {
      hook.result.current.start()
      await flush()
    })
    expect(hook.result.current.phase).toBe("live")
    expect(mocks.redirectHolds.count).toBe(1)
    await act(async () => {
      hook.result.current.requestEnd()
      await flush()
    })
    expect(hook.result.current.phase).toBe("closing")
    expect(mocks.redirectHolds.count).toBe(1)
    await act(async () => {
      hook.result.current.syncClosingState(
        "completed",
        null,
        "not_possible",
        true
      )
      await flush()
    })
    expect(hook.result.current.phase).toBe("ended")
    expect(mocks.redirectHolds.count).toBe(0)
    hook.unmount()
  })

  it("releases the redirect hold when the page goes away mid-interview", async () => {
    const { result, unmount } = await connected()
    expect(result.current.phase).toBe("live")
    expect(mocks.redirectHolds.count).toBe(1)
    unmount()
    expect(mocks.redirectHolds.count).toBe(0)
  })

  it("offers answer-again or end only while the technical notice is active", async () => {
    const { room, result, unmount } = await connected()
    const changed = (attributes: Record<string, string>) =>
      act(async () => {
        for (const handler of room.listeners.get(
          RoomEvent.ParticipantAttributesChanged
        ) ?? [])
          Reflect.apply(handler, undefined, [attributes, { isLocal: false }])
        await flush()
      })
    expect(result.current.technicalNotice).toBe(false)
    await changed({ "interview.notice": "technical" })
    expect(result.current.technicalNotice).toBe(true)
    await changed({ "lk.agent.state": "speaking" })
    expect(result.current.technicalNotice).toBe(true)
    await changed({ "interview.notice": "" })
    expect(result.current.technicalNotice).toBe(false)
    unmount()
  })

  it("leaves the live panel when the server seals a lost-worker interview", async () => {
    const { room, result, unmount } = await connected()
    expect(result.current.phase).toBe("live")
    await act(async () => {
      result.current.syncClosingState("completed", null, "not_possible", true)
      await flush()
    })
    expect(result.current.phase).toBe("ended")
    expect(result.current.endedAt).not.toBeNull()
    expect(room.disconnect).toHaveBeenCalled()
    unmount()
  })

  it("shows bounded recovery when request_end succeeds without control or audio", async () => {
    const { room, result, unmount } = await connected()
    await act(async () => {
      result.current.requestEnd()
      await flush()
    })
    expect(room.localParticipant.performRpc).toHaveBeenCalledOnce()
    expect(result.current.phase).toBe("closing")
    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000)
    })
    expect(result.current.closingRecoveryPending).toBe(true)
    expect(result.current.phase).toBe("closing")
    expect(result.current.endedAt).toBeNull()
    unmount()
  })

  it("keeps supervision when the worker's RPC reply may have been lost", async () => {
    const { room, result, unmount } = await connected()
    room.localParticipant.performRpc.mockRejectedValueOnce(
      new Error("RPC timeout")
    )
    await act(async () => {
      result.current.requestEnd()
      await flush()
    })
    expect(result.current.phase).toBe("closing")
    await act(async () => {
      await vi.advanceTimersByTimeAsync(46_000)
    })
    expect(result.current.closingRecoveryPending).toBe(true)
    unmount()
  })

  it.each([
    RpcError.ErrorCode.UNSUPPORTED_METHOD,
    RpcError.ErrorCode.RECIPIENT_NOT_FOUND,
  ])(
    "allows retry after a definitive unaccepted RPC error %s",
    async (code) => {
      const { room, result, unmount } = await connected()
      room.localParticipant.performRpc.mockRejectedValueOnce(
        new RpcError(code, "Not accepted")
      )
      await act(async () => {
        result.current.requestEnd()
        await flush()
      })
      expect(result.current.phase).toBe("live")
      await act(async () => {
        await vi.advanceTimersByTimeAsync(46_000)
      })
      expect(result.current.closingRecoveryPending).toBe(false)
      expect(
        room.remoteParticipants.get("agent")?.setVolume
      ).not.toHaveBeenCalled()
      unmount()
    }
  )

  it("keeps an orphan incomplete before the next agent and user messages", async () => {
    const { room, result } = await connected()
    const orphan = new ControlledReader("orphan")
    orphan.info.attributes["interview.incomplete"] = "true"
    orphan.push("Unconfirmed answer")
    orphan.push(null)
    await act(async () => {
      await room.receive(orphan)
    })
    const agent = new ControlledReader("question", true)
    agent.push("Next question")
    agent.push(null)
    await act(async () => {
      await room.receive(agent, "agent")
    })
    const answer = new ControlledReader("next-turn", true)
    answer.push("Next answer")
    answer.push(null)
    await act(async () => {
      await room.receive(answer)
    })
    expect(result.current.messages.map(({ text }) => text)).toEqual([
      "Unconfirmed answer",
      "Next question",
      "Next answer",
    ])
    expect(result.current.messages[0]).toMatchObject({
      interim: false,
      incomplete: true,
    })
    expect(result.current.messages[2]).toMatchObject({
      interim: false,
      incomplete: false,
    })
  })

  it("renders consecutive STT sentences as one turn, keeping repeated sentences", async () => {
    const { room, result } = await connected()
    for (const [text, final] of [
      ["First sentence.", false],
      ["First sentence. First sentence.", false],
      ["First sentence. First sentence. Last sentence.", true],
    ] as const) {
      const reader = new ControlledReader("turn-one", final)
      reader.push(text)
      reader.push(null)
      await act(async () => {
        // Custom user text is published by the agent, not the candidate.
        await room.receive(reader, "agent", "interview.user_transcription")
      })
      expect(result.current.messages).toHaveLength(1)
    }
    expect(result.current.messages[0]).toMatchObject({
      who: "user",
      text: "First sentence. First sentence. Last sentence.",
      interim: false,
      incomplete: false,
    })
    const next = new ControlledReader("turn-two", true)
    next.push("First sentence.")
    next.push(null)
    await act(async () => {
      await room.receive(next)
    })
    // A new confirmed turn stays separate, even without intervening agent text.
    expect(result.current.messages).toHaveLength(2)
  })

  it("ignores native user STT segments when receiving grouped turns", async () => {
    const { room, result } = await connected()
    const sentence = new ControlledReader("sentence", true)
    sentence.push("One sentence")
    sentence.push(null)
    await act(async () => {
      await room.receive(sentence, "candidate", "lk.transcription")
    })
    expect(result.current.messages).toEqual([])
    const turn = new ControlledReader("turn", true)
    turn.push("One sentence. Another sentence.")
    turn.push(null)
    await act(async () => {
      await room.receive(turn, "agent", "interview.user_transcription")
    })
    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].text).toBe(
      "One sentence. Another sentence."
    )
  })

  it("keeps a failed partial when a new segment repeats it (legacy lastSegmentRef case)", async () => {
    const { room, result } = await connected()
    const old = new ControlledReader("old")
    old.push("Tell me about SQL")
    old.push(new Error("Participant disconnected"))
    await act(async () => {
      await room.receive(old, "agent")
    })
    const next = new ControlledReader("new")
    next.push("Tell me about SQL indexes")
    next.push(null)
    await act(async () => {
      await room.receive(next, "agent")
    })
    expect(result.current.messages).toEqual([
      {
        segmentId: "old",
        who: "agent",
        text: "Tell me about SQL",
        interim: false,
        incomplete: true,
      },
      {
        segmentId: "new",
        who: "agent",
        text: "Tell me about SQL indexes",
        interim: false,
        incomplete: false,
      },
    ])
  })

  it.each(["agent", "candidate"])(
    "preserves received %s text and recovers with a final of the same id",
    async (identity) => {
      const { room, result } = await connected()
      const partial = new ControlledReader("one")
      partial.push("Part")
      partial.push(new Error("Abnormal end"))
      await act(async () => {
        await room.receive(partial, identity)
      })
      expect(result.current.messages[0]).toMatchObject({
        text: "Part",
        incomplete: true,
        interim: false,
      })
      const final = new ControlledReader("one", true)
      final.push("Complete answer")
      final.push(null)
      await act(async () => {
        await room.receive(final, identity)
      })
      expect(result.current.messages).toHaveLength(1)
      expect(result.current.messages[0]).toMatchObject({
        text: "Complete answer",
        incomplete: false,
        interim: false,
      })
      expect(result.current.phase).toBe("live")
    }
  )

  it("removes empty failed streams without rejecting their callback", async () => {
    const { room, result } = await connected()
    const reader = new ControlledReader("empty")
    reader.push(new Error("Disconnected"))
    await act(async () => {
      await room.receive(reader, "agent")
    })
    expect(result.current.messages).toEqual([])
    expect(result.current.phase).toBe("live")
  })

  it("consolidates a user interim as incomplete after three seconds", async () => {
    const { room, result } = await connected()
    const reader = new ControlledReader("user")
    reader.push("My answer")
    reader.push(null)
    await act(async () => {
      await room.receive(reader)
    })
    expect(result.current.messages[0]).toMatchObject({
      interim: true,
      incomplete: false,
    })
    act(() => vi.advanceTimersByTime(3000))
    expect(result.current.messages[0]).toMatchObject({
      interim: false,
      incomplete: true,
    })
  })

  it("never lets an older read or late interim overwrite a confirmed final", async () => {
    const { room, result } = await connected()
    const old = new ControlledReader("same")
    let oldRead!: Promise<void>
    await act(async () => {
      oldRead = room.receive(old)
      await flush()
    })
    const final = new ControlledReader("same", true)
    final.push("Final answer")
    final.push(null)
    await act(async () => {
      await room.receive(final)
    })
    old.push("Old interim")
    old.push(null)
    await act(async () => {
      await oldRead
    })
    const late = new ControlledReader("same")
    late.push("Late interim")
    late.push(null)
    await act(async () => {
      await room.receive(late)
    })
    act(() => vi.advanceTimersByTime(3000))
    expect(result.current.messages).toEqual([
      {
        segmentId: "same",
        who: "user",
        text: "Final answer",
        interim: false,
        incomplete: false,
      },
    ])
  })

  it.each([RoomEvent.ParticipantDisconnected, RoomEvent.Disconnected])(
    "aborts pending readers on %s and cleans timers",
    async (event) => {
      const { room, result } = await connected()
      const reader = new ControlledReader("pending")
      reader.push("Partial")
      let pending!: Promise<void>
      await act(async () => {
        pending = room.receive(reader, "agent")
        await flush()
      })
      await act(async () => {
        room.emit(event)
        await pending
      })
      expect(reader.signal?.aborted).toBe(true)
      expect(result.current.messages[0]).toMatchObject({
        text: "Partial",
        interim: false,
        incomplete: true,
      })
      expect(vi.getTimerCount()).toBe(0)
      if (event === RoomEvent.ParticipantDisconnected)
        expect(result.current.phase).toBe("live")
    }
  )

  it("preserves a stream across a brief offline/reconnecting event", async () => {
    const { room, result } = await connected()
    const reader = new ControlledReader("resumable")
    reader.push("Tell me ")
    let pending!: Promise<void>
    await act(async () => {
      pending = room.receive(reader, "agent")
      await flush()
      room.emit(RoomEvent.Reconnecting)
      await flush()
    })
    expect(reader.signal?.aborted).toBe(false)
    room.emit(RoomEvent.Reconnected)
    reader.push("about SQL.")
    reader.push(null)
    await act(async () => {
      await pending
    })
    expect(result.current.messages[0]).toMatchObject({
      text: "Tell me about SQL.",
      interim: false,
      incomplete: false,
    })
  })

  it("marks a stalled reader incomplete without ending the interview", async () => {
    const { room, result } = await connected()
    const reader = new ControlledReader("stalled")
    reader.push("Tell me ")
    let pending!: Promise<void>
    await act(async () => {
      pending = room.receive(reader, "agent")
      await flush()
      room.emit(RoomEvent.Reconnecting)
      room.emit(RoomEvent.Reconnected)
    })
    await act(async () => {
      vi.advanceTimersByTime(30_000)
      await pending
    })
    expect(reader.signal?.aborted).toBe(true)
    expect(result.current.messages[0]).toMatchObject({
      text: "Tell me ",
      interim: false,
      incomplete: true,
    })
    expect(result.current.phase).toBe("live")
    expect(vi.getTimerCount()).toBe(0)
  })

  it("refreshes inactivity on every chunk rather than limiting utterance length", async () => {
    const { room, result } = await connected()
    const reader = new ControlledReader("long")
    reader.push("One ")
    let pending!: Promise<void>
    await act(async () => {
      pending = room.receive(reader, "agent")
      await flush()
    })
    await act(async () => {
      vi.advanceTimersByTime(20_000)
      reader.push("two ")
      await flush()
      vi.advanceTimersByTime(20_000)
      reader.push("three.")
      reader.push(null)
      await pending
    })
    expect(reader.signal?.aborted).toBe(false)
    expect(result.current.messages[0]).toMatchObject({
      text: "One two three.",
      interim: false,
      incomplete: false,
    })
    expect(vi.getTimerCount()).toBe(0)
  })

  it("aborts pending readers on unmount", async () => {
    const { room, unmount } = await connected()
    const reader = new ControlledReader("pending")
    let pending!: Promise<void>
    await act(async () => {
      pending = room.receive(reader)
      await flush()
    })
    unmount()
    await act(async () => {
      await pending
    })
    expect(reader.signal?.aborted).toBe(true)
    expect(room.disconnect).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })
})

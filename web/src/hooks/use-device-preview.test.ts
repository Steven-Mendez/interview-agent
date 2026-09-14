/** @vitest-environment jsdom */
import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useDevicePreview } from "./use-device-preview"

// jsdom has no MediaStream, AudioContext or navigator.mediaDevices: the
// stream is a duck-typed stand-in, the meter gives up on the missing
// AudioContext (by design), and getUserMedia is a mock the tests resolve or
// reject by hand — the timing of those answers is what these tests are about.

interface FakeTrack {
  kind: "audio" | "video"
  readyState: "live" | "ended"
  stop: ReturnType<typeof vi.fn>
  getSettings: () => { deviceId: string }
}

function fakeTrack(kind: FakeTrack["kind"], deviceId: string): FakeTrack {
  const track: FakeTrack = {
    kind,
    readyState: "live",
    stop: vi.fn(() => {
      track.readyState = "ended"
    }),
    getSettings: () => ({ deviceId }),
  }
  return track
}

function fakeStream(tracks: FakeTrack[]): MediaStream {
  return {
    getTracks: () => tracks,
    getAudioTracks: () => tracks.filter((t) => t.kind === "audio"),
    getVideoTracks: () => tracks.filter((t) => t.kind === "video"),
  } as unknown as MediaStream
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function domError(name: string): Error {
  const error = new Error(name)
  error.name = name
  return error
}

/** A macrotask: lets every pending continuation of open() run. */
const settle = () => new Promise<void>((r) => setTimeout(r, 0))

const getUserMedia =
  vi.fn<(c: MediaStreamConstraints) => Promise<MediaStream>>()

beforeEach(() => {
  getUserMedia.mockReset()
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) },
  })
  vi.spyOn(console, "error").mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe("useDevicePreview", () => {
  it("keeps the latest pick when an earlier request resolves last", async () => {
    const first = deferred<MediaStream>()
    const second = deferred<MediaStream>()
    getUserMedia
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const { result } = renderHook(() => useDevicePreview())

    act(() => result.current.selectMic("a"))
    act(() => result.current.selectMic("b"))

    const streamB = fakeStream([fakeTrack("audio", "b")])
    await act(async () => {
      second.resolve(streamB)
      await settle()
    })
    expect(result.current.stream).toBe(streamB)
    expect(result.current.micId).toBe("b")

    const trackA = fakeTrack("audio", "a")
    await act(async () => {
      first.resolve(fakeStream([trackA]))
      await settle()
    })
    expect(trackA.stop).toHaveBeenCalled()
    expect(result.current.stream).toBe(streamB)
    expect(result.current.micId).toBe("b")
  })

  it("stops a stream that arrives after unmount", async () => {
    const pending = deferred<MediaStream>()
    getUserMedia.mockReturnValueOnce(pending.promise)
    const { result, unmount } = renderHook(() => useDevicePreview())

    act(() => result.current.request())
    unmount()

    const track = fakeTrack("audio", "x")
    pending.resolve(fakeStream([track]))
    await settle()
    expect(track.stop).toHaveBeenCalled()
  })

  it("falls back to audio when the camera fails, and stops asking for it", async () => {
    const audioTrack = fakeTrack("audio", "m")
    getUserMedia
      .mockRejectedValueOnce(domError("NotFoundError"))
      .mockResolvedValueOnce(fakeStream([audioTrack]))
    const { result } = renderHook(() => useDevicePreview())

    await act(async () => {
      result.current.toggleCamera()
      await settle()
    })
    expect(getUserMedia).toHaveBeenNthCalledWith(1, {
      audio: true,
      video: true,
    })
    expect(getUserMedia).toHaveBeenNthCalledWith(2, { audio: true })
    expect(result.current.status).toBe("ready")
    expect(result.current.cameraOn).toBe(false)
    expect(result.current.error).toBe(
      "No camera found. Connect one and try again."
    )
    expect(result.current.stream?.getAudioTracks()[0]).toBe(audioTrack)

    getUserMedia.mockResolvedValueOnce(fakeStream([fakeTrack("audio", "n")]))
    await act(async () => {
      result.current.selectMic("n")
      await settle()
    })
    expect(getUserMedia).toHaveBeenLastCalledWith({
      audio: { deviceId: { exact: "n" } },
      video: false,
    })
    expect(result.current.error).toBeNull()
  })

  it("reports the microphone when audio itself is blocked", async () => {
    getUserMedia.mockRejectedValueOnce(domError("NotAllowedError"))
    const { result } = renderHook(() => useDevicePreview())

    await act(async () => {
      result.current.request()
      await settle()
    })
    expect(result.current.status).toBe("denied")
    expect(result.current.error).toMatch(/^Microphone access was blocked/)
  })

  it("hands the microphone over and takes it back only if it was held", async () => {
    const held = fakeTrack("audio", "m")
    getUserMedia.mockResolvedValueOnce(fakeStream([held]))
    const { result } = renderHook(() => useDevicePreview())

    await act(async () => {
      result.current.request()
      await settle()
    })
    expect(result.current.status).toBe("ready")

    act(() => result.current.releaseMic())
    expect(held.stop).toHaveBeenCalled()
    expect(result.current.status).toBe("ready")

    const again = fakeTrack("audio", "m")
    getUserMedia.mockResolvedValueOnce(fakeStream([again]))
    await act(async () => {
      result.current.reclaimMic()
      await settle()
    })
    expect(result.current.stream?.getAudioTracks()[0]).toBe(again)

    // Nothing was released this time: nothing to reopen.
    const calls = getUserMedia.mock.calls.length
    await act(async () => {
      result.current.reclaimMic()
      await settle()
    })
    expect(getUserMedia.mock.calls.length).toBe(calls)
  })

  it("cancels a prompt still pending when the microphone is handed over", async () => {
    const pending = deferred<MediaStream>()
    getUserMedia.mockReturnValueOnce(pending.promise)
    const { result } = renderHook(() => useDevicePreview())

    act(() => result.current.request())
    expect(result.current.status).toBe("requesting")

    act(() => result.current.releaseMic())
    expect(result.current.status).toBe("idle")

    const track = fakeTrack("audio", "x")
    await act(async () => {
      pending.resolve(fakeStream([track]))
      await settle()
    })
    expect(track.stop).toHaveBeenCalled()
    expect(result.current.stream).toBeNull()
    expect(result.current.status).toBe("idle")
  })
})

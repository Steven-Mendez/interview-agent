/** @vitest-environment jsdom */
import * as React from "react"
import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { stopAllPreviewStreams, useDevicePreview } from "./use-device-preview"

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

  it("asks for the camera and microphone in one prompt on request(true)", async () => {
    const audio = fakeTrack("audio", "m")
    const video = fakeTrack("video", "c")
    getUserMedia.mockResolvedValueOnce(fakeStream([audio, video]))
    const { result } = renderHook(() => useDevicePreview())

    await act(async () => {
      result.current.request(true)
      await settle()
    })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(getUserMedia.mock.calls[0][0]).toEqual({ audio: true, video: true })
    expect(result.current.status).toBe("ready")
    expect(result.current.cameraWanted).toBe(true)
    expect(result.current.cameraOn).toBe(true)
  })

  it("does not stay requesting when StrictMode cancels the first prompt", async () => {
    const first = deferred<MediaStream>()
    getUserMedia.mockReturnValueOnce(first.promise)
    const { result } = renderHook(
      () => {
        const preview = useDevicePreview()
        const asked = React.useRef(false)
        // Asks straight from an effect, the way a mount-time check does.
        React.useEffect(() => {
          if (asked.current) return
          asked.current = true
          preview.request(true)
        }, [preview])
        return preview
      },
      { reactStrictMode: true }
    )
    const track = fakeTrack("audio", "m")
    await act(async () => {
      first.resolve(fakeStream([track]))
      await settle()
    })
    // The cancelled prompt's stream is stopped, and the check is idle — free
    // to be asked again — rather than waiting forever.
    expect(track.stop).toHaveBeenCalled()
    expect(result.current.status).toBe("idle")
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

  it("toggles only the camera once the microphone is handed over", async () => {
    getUserMedia.mockResolvedValueOnce(fakeStream([fakeTrack("audio", "m")]))
    const { result } = renderHook(() => useDevicePreview())
    await act(async () => {
      result.current.request()
      await settle()
    })
    act(() => result.current.releaseMic())

    const video = fakeTrack("video", "c")
    getUserMedia.mockResolvedValueOnce(fakeStream([video]))
    await act(async () => {
      result.current.toggleCamera()
      await settle()
    })
    expect(getUserMedia).toHaveBeenLastCalledWith({ audio: false, video: true })
    expect(result.current.cameraOn).toBe(true)
    expect(result.current.status).toBe("ready")

    const calls = getUserMedia.mock.calls.length
    await act(async () => {
      result.current.toggleCamera()
      await settle()
    })
    // Off needs no new capture: the video track is just stopped.
    expect(getUserMedia.mock.calls.length).toBe(calls)
    expect(video.stop).toHaveBeenCalled()
    expect(result.current.cameraOn).toBe(false)
    expect(result.current.stream).toBeNull()
  })

  it("reports a camera failure during the interview without touching audio", async () => {
    getUserMedia.mockResolvedValueOnce(fakeStream([fakeTrack("audio", "m")]))
    const { result } = renderHook(() => useDevicePreview())
    await act(async () => {
      result.current.request()
      await settle()
    })
    act(() => result.current.releaseMic())

    getUserMedia.mockRejectedValueOnce(domError("NotReadableError"))
    await act(async () => {
      result.current.toggleCamera()
      await settle()
    })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(result.current.cameraOn).toBe(false)
    expect(result.current.status).toBe("ready")
    expect(result.current.error).toMatch(/camera is already in use/)
  })
})

// Leaving the preparation room must leave no camera or microphone running,
// whatever was in flight: these walk each way a stream could outlive it.
describe("useDevicePreview capture release", () => {
  const live = (...tracks: FakeTrack[]) =>
    tracks.filter((track) => track.readyState === "live")

  it("stops a camera granted after a hand-over cancelled the prompt and the page is gone", async () => {
    // The failing run: the prompt was still open at Start, the session took
    // the microphone, the candidate left, and only then allowed the prompt.
    const pending = deferred<MediaStream>()
    getUserMedia.mockReturnValueOnce(pending.promise)
    const { result, unmount } = renderHook(() => useDevicePreview())
    act(() => result.current.request(true))
    act(() => result.current.releaseMic())
    unmount()

    const audio = fakeTrack("audio", "m")
    const video = fakeTrack("video", "c")
    pending.resolve(fakeStream([audio, video]))
    await settle()
    expect(live(audio, video)).toEqual([])
  })

  it("stops the self-view a hand-over left running when the page goes away", async () => {
    const audio = fakeTrack("audio", "m")
    const video = fakeTrack("video", "c")
    getUserMedia.mockResolvedValueOnce(fakeStream([audio, video]))
    const { result, unmount } = renderHook(() => useDevicePreview())
    await act(async () => {
      result.current.request(true)
      await settle()
    })
    act(() => result.current.releaseMic())
    // The camera stays on through the interview on purpose…
    expect(live(audio, video)).toEqual([video])
    unmount()
    // …and not a moment past it.
    expect(live(audio, video)).toEqual([])
  })

  it("stops the camera opened during the interview when the page goes away", async () => {
    getUserMedia.mockResolvedValueOnce(fakeStream([fakeTrack("audio", "m")]))
    const { result, unmount } = renderHook(() => useDevicePreview())
    await act(async () => {
      result.current.request()
      await settle()
    })
    act(() => result.current.releaseMic())
    const video = fakeTrack("video", "c")
    getUserMedia.mockResolvedValueOnce(fakeStream([video]))
    await act(async () => {
      result.current.toggleCamera()
      await settle()
    })
    expect(result.current.cameraOn).toBe(true)
    unmount()
    expect(video.stop).toHaveBeenCalled()
  })

  it("never leaves the previous camera running when switching, in either order", async () => {
    const first = [fakeTrack("audio", "m"), fakeTrack("video", "a")]
    getUserMedia.mockResolvedValueOnce(fakeStream(first))
    const { result, unmount } = renderHook(() => useDevicePreview())
    await act(async () => {
      result.current.selectCam("a")
      await settle()
    })
    expect(live(...first)).toEqual(first)

    // Two quick picks, answered out of order: only the last one survives.
    const toB = deferred<MediaStream>()
    const toC = deferred<MediaStream>()
    getUserMedia
      .mockReturnValueOnce(toB.promise)
      .mockReturnValueOnce(toC.promise)
    act(() => result.current.selectCam("b"))
    act(() => result.current.selectCam("c"))
    const third = [fakeTrack("audio", "m"), fakeTrack("video", "c")]
    await act(async () => {
      toC.resolve(fakeStream(third))
      await settle()
    })
    const second = [fakeTrack("audio", "m"), fakeTrack("video", "b")]
    await act(async () => {
      toB.resolve(fakeStream(second))
      await settle()
    })
    expect(live(...first, ...second, ...third)).toEqual(third)
    expect(result.current.camId).toBe("c")

    unmount()
    expect(live(...third)).toEqual([])
  })

  it("stops the audio-only fallback when it arrives after the page is gone", async () => {
    const fallback = deferred<MediaStream>()
    getUserMedia
      .mockRejectedValueOnce(domError("NotReadableError"))
      .mockReturnValueOnce(fallback.promise)
    const { result, unmount } = renderHook(() => useDevicePreview())
    await act(async () => {
      result.current.request(true)
      await settle()
    })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    unmount()

    const audio = fakeTrack("audio", "m")
    fallback.resolve(fakeStream([audio]))
    await settle()
    expect(audio.stop).toHaveBeenCalled()
  })

  it("stops a stream granted after a StrictMode remount and a real unmount", async () => {
    const pending = deferred<MediaStream>()
    getUserMedia.mockReturnValueOnce(pending.promise)
    const { result, unmount } = renderHook(() => useDevicePreview(), {
      reactStrictMode: true,
    })
    // Asked after the development double mount settled, as the panel does.
    act(() => result.current.request(true))
    unmount()

    const audio = fakeTrack("audio", "m")
    const video = fakeTrack("video", "c")
    pending.resolve(fakeStream([audio, video]))
    await settle()
    expect(live(audio, video)).toEqual([])
  })

  it("reopens the full check once a hand-over that captured nothing is taken back", async () => {
    // The page relies on this when the room drops mid-interview: without
    // reclaimMic the next request would open the camera alone.
    const pending = deferred<MediaStream>()
    getUserMedia.mockReturnValueOnce(pending.promise)
    const { result } = renderHook(() => useDevicePreview())
    act(() => result.current.request(true))
    act(() => result.current.releaseMic())
    act(() => result.current.reclaimMic())

    getUserMedia.mockResolvedValueOnce(
      fakeStream([fakeTrack("audio", "m"), fakeTrack("video", "c")])
    )
    await act(async () => {
      result.current.request(true)
      await settle()
    })
    expect(getUserMedia).toHaveBeenLastCalledWith({ audio: true, video: true })
    expect(result.current.status).toBe("ready")
    expect(result.current.cameraOn).toBe(true)
  })

  it("releases every stream the preview opened, not only the unmounting one's", async () => {
    // The backstop: a stream a preview's own refs no longer point at is
    // still on the module's list, and leaving stops it.
    const orphan = [fakeTrack("audio", "m"), fakeTrack("video", "c")]
    getUserMedia.mockResolvedValueOnce(fakeStream(orphan))
    const other = renderHook(() => useDevicePreview())
    await act(async () => {
      other.result.current.request(true)
      await settle()
    })
    const { unmount } = renderHook(() => useDevicePreview())
    unmount()
    expect(live(...orphan)).toEqual([])

    const again = [fakeTrack("audio", "m")]
    getUserMedia.mockResolvedValueOnce(fakeStream(again))
    await act(async () => {
      other.result.current.request(false)
      await settle()
    })
    stopAllPreviewStreams()
    expect(live(...again)).toEqual([])
    other.unmount()
  })

  // 120 seeds of 12 steps, each waiting a real macrotask: about 2-3 s here,
  // so a slower CI runner needs room past vitest's 5 s default.
  it(
    "leaves nothing running after any sequence of picks, answers and unmount",
    {
      timeout: 30_000,
    },
    async () => {
      // A seeded walk over every action and every order the prompts can be
      // answered in. While mounted, the only live tracks are the shown
      // stream's; after unmount (and every late answer), none are.
      type Pending = {
        constraints: MediaStreamConstraints
        resolve: (stream: MediaStream) => void
        reject: (error: unknown) => void
      }
      let random = 0
      const next = () => {
        random = (random * 1103515245 + 12345) & 0x7fffffff
        return random / 0x7fffffff
      }
      const failures: string[] = []
      for (let seed = 1; seed <= 120; seed++) {
        random = seed
        const tracks: FakeTrack[] = []
        const queue: Pending[] = []
        const trail: string[] = []
        getUserMedia.mockReset()
        getUserMedia.mockImplementation(
          (constraints) =>
            new Promise<MediaStream>((resolve, reject) =>
              queue.push({ constraints, resolve, reject })
            )
        )
        const answer = (pending: Pending) => {
          const kinds: FakeTrack["kind"][] = []
          if (pending.constraints.audio) kinds.push("audio")
          if (pending.constraints.video) kinds.push("video")
          const opened = kinds.map((kind) => fakeTrack(kind, kind))
          tracks.push(...opened)
          pending.resolve(fakeStream(opened))
        }
        const hook = renderHook(() => useDevicePreview())
        // An object, not a let: the flag flips inside act's callback.
        const page = { mounted: true }
        for (let step = 0; step < 12; step++) {
          const roll = Math.floor(next() * 11)
          const preview = hook.result.current
          await act(async () => {
            if (!page.mounted && roll < 7) return
            const pick = queue.length ? Math.floor(next() * queue.length) : -1
            switch (roll) {
              case 0:
                trail.push("request")
                preview.request()
                break
              case 1:
                trail.push("request(true)")
                preview.request(true)
                break
              case 2:
                trail.push("selectMic")
                preview.selectMic(`m${step}`)
                break
              case 3:
                trail.push("selectCam")
                preview.selectCam(`c${step}`)
                break
              case 4:
                trail.push("toggleCamera")
                preview.toggleCamera()
                break
              case 5:
                trail.push("releaseMic")
                preview.releaseMic()
                break
              case 6:
                trail.push("reclaimMic")
                preview.reclaimMic()
                break
              case 7:
              case 8:
                if (pick >= 0) {
                  trail.push(`grant#${pick}`)
                  answer(queue.splice(pick, 1)[0])
                }
                break
              case 9:
                if (pick >= 0) {
                  const name =
                    next() < 0.5 ? "NotFoundError" : "NotAllowedError"
                  trail.push(`${name}#${pick}`)
                  queue.splice(pick, 1)[0].reject(domError(name))
                }
                break
              default:
                if (page.mounted) {
                  trail.push("unmount")
                  hook.unmount()
                  page.mounted = false
                }
            }
            await settle()
          })
          if (page.mounted) {
            const shown = hook.result.current.stream?.getTracks() ?? []
            const stray = tracks.filter(
              (track) =>
                track.readyState === "live" &&
                !(shown as unknown[]).includes(track)
            )
            if (stray.length) {
              failures.push(`seed ${seed} while mounted: ${trail.join(" > ")}`)
              break
            }
          }
        }
        if (page.mounted) hook.unmount()
        while (queue.length) {
          answer(queue.shift()!)
          await settle()
        }
        if (live(...tracks).length)
          failures.push(`seed ${seed} after unmount: ${trail.join(" > ")}`)
      }
      expect(failures).toEqual([])
    }
  )
})

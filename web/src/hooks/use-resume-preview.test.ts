/** @vitest-environment jsdom */
import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useResumePreview } from "./use-resume-preview"
import type { ResumePreview } from "@/lib/api"

const mocks = vi.hoisted(() => ({ preview: vi.fn() }))
vi.mock("@/lib/api", () => ({ previewResume: mocks.preview }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}
const a = new File(["first"], "a.pdf", { type: "application/pdf" })
const b = new File(["second"], "b.pdf", { type: "application/pdf" })
const preview = (name: string): ResumePreview => ({
  filename: name,
  text: `Text for ${name}`,
  characters: 14,
  pdf_sha256: name,
})

beforeEach(() => mocks.preview.mockReset())
afterEach(cleanup)

describe("resume extraction ownership", () => {
  it("ignores an old result after the file changes, even when abort is ignored", async () => {
    const first = deferred<ResumePreview>()
    const second = deferred<ResumePreview>()
    mocks.preview
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const { result } = renderHook(useResumePreview)
    let oldRequest!: Promise<ResumePreview | null>
    act(() => {
      oldRequest = result.current.load(a)
    })
    const oldSignal = mocks.preview.mock.calls[0][1] as AbortSignal
    act(() => result.current.reset())
    let newRequest!: Promise<ResumePreview | null>
    act(() => {
      newRequest = result.current.load(b)
    })
    expect(oldSignal.aborted).toBe(true)
    await act(async () => {
      second.resolve(preview("b"))
      await newRequest
    })
    await act(async () => {
      first.resolve(preview("a"))
      expect(await oldRequest).toBeNull()
    })
    expect(result.current.data?.filename).toBe("b")
    expect(result.current.error).toBeNull()
    expect(result.current.pending).toBe(false)
  })

  it("does not duplicate an in-flight extraction and reuses a successful preview", async () => {
    const response = deferred<ResumePreview>()
    mocks.preview.mockReturnValue(response.promise)
    const { result } = renderHook(useResumePreview)
    let request!: Promise<ResumePreview | null>
    act(() => {
      request = result.current.load(a)
    })
    await act(async () => {
      expect(await result.current.load(a)).toBeNull()
    })
    await act(async () => {
      response.resolve(preview("a"))
      await request
    })
    await act(async () => {
      expect(await result.current.load(a)).toEqual(preview("a"))
    })
    expect(mocks.preview).toHaveBeenCalledTimes(1)
  })

  it("allows retry after a visible extraction failure", async () => {
    mocks.preview
      .mockRejectedValueOnce(new Error("No readable text"))
      .mockResolvedValueOnce(preview("a"))
    const { result } = renderHook(useResumePreview)
    await act(async () => {
      await result.current.load(a)
    })
    expect(result.current.error?.message).toBe("No readable text")
    await act(async () => {
      await result.current.load(a)
    })
    expect(result.current.error).toBeNull()
    expect(result.current.data?.filename).toBe("a")
  })

  it("cancels ownership on unmount and ignores the eventual response", async () => {
    const response = deferred<ResumePreview>()
    mocks.preview.mockReturnValue(response.promise)
    const { result, unmount } = renderHook(useResumePreview)
    let request!: Promise<ResumePreview | null>
    act(() => {
      request = result.current.load(a)
    })
    const signal = mocks.preview.mock.calls[0][1] as AbortSignal
    unmount()
    expect(signal.aborted).toBe(true)
    response.resolve(preview("a"))
    expect(await request).toBeNull()
  })
})

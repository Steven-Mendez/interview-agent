/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Api from "@/lib/api"
import type * as Waking from "@/lib/api-waking"

// The cold-start flag, driven through request() (getMe) the way the app
// drives it. Each test imports fresh modules, so the flag and the time of
// the last answer start over.

vi.mock("@/lib/auth", () => ({
  getAccessToken: vi.fn().mockResolvedValue("user.jwt.token"),
  refreshAccessToken: vi.fn().mockResolvedValue(null),
  resetAuthMode: vi.fn(),
  forgetRefusedToken: vi.fn(),
}))

const fetchMock = vi.fn<typeof fetch>()

/** A fetch that answers when the test says so. */
function pendingFetch() {
  let answer!: (res: Response) => void
  let fail!: (error: Error) => void
  fetchMock.mockReturnValueOnce(
    new Promise<Response>((resolve, reject) => {
      answer = resolve
      fail = reject
    })
  )
  return {
    answer: (status = 200) =>
      answer(new Response(JSON.stringify({ id: "user" }), { status })),
    fail: () => fail(new TypeError("Failed to fetch")),
  }
}

async function load(): Promise<{ api: typeof Api; waking: typeof Waking }> {
  vi.resetModules()
  return {
    api: await import("@/lib/api"),
    waking: await import("@/lib/api-waking"),
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal("fetch", fetchMock)
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  fetchMock.mockReset()
})

describe("API waking", () => {
  it("turns on after 4 s without an answer and off when it arrives", async () => {
    const { api, waking } = await load()
    const reply = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(3_999)
    expect(waking.isApiWaking()).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(waking.isApiWaking()).toBe(true)

    reply.answer()
    await me
    expect(waking.isApiWaking()).toBe(false)
  })

  it("never turns on for a fast answer", async () => {
    const { api, waking } = await load()
    const reply = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(500)
    reply.answer()
    await me
    await vi.advanceTimersByTimeAsync(10_000)
    expect(waking.isApiWaking()).toBe(false)
  })

  it("turns off when the fetch fails", async () => {
    const { api, waking } = await load()
    const reply = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(4_000)
    expect(waking.isApiWaking()).toBe(true)

    reply.fail()
    await expect(me).rejects.toThrow("Failed to fetch")
    expect(waking.isApiWaking()).toBe(false)
  })

  it("counts an error status as an answer", async () => {
    const { api, waking } = await load()
    const reply = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(4_000)
    expect(waking.isApiWaking()).toBe(true)

    reply.answer(503)
    await expect(me).rejects.toMatchObject({ status: 503 })
    expect(waking.isApiWaking()).toBe(false)
  })

  it("does not take a slow route of an API that just answered for a boot", async () => {
    const { api, waking } = await load()
    const first = pendingFetch()
    const warmUp = api.getMe()
    first.answer()
    await warmUp

    const slow = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(waking.isApiWaking()).toBe(false)
    slow.answer()
    await me
  })

  it("watches again once the API has been quiet long enough to sleep", async () => {
    const { api, waking } = await load()
    const first = pendingFetch()
    const warmUp = api.getMe()
    first.answer()
    await warmUp
    await vi.advanceTimersByTimeAsync(5 * 60_000)

    const slow = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(4_000)
    expect(waking.isApiWaking()).toBe(true)
    slow.answer()
    await me
    expect(waking.isApiWaking()).toBe(false)
  })

  it("takes a quick 401 as proof the API is up while the retry waits", async () => {
    const { api, waking } = await load()
    const auth = await import("@/lib/auth")
    vi.mocked(auth.refreshAccessToken).mockResolvedValueOnce("fresh.jwt.token")
    const refused = pendingFetch()
    const retried = pendingFetch()
    const me = api.getMe()
    refused.answer(401)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(waking.isApiWaking()).toBe(false)
    retried.answer()
    await me
  })

  it("stays off for a request still waiting after another one was answered", async () => {
    const { api, waking } = await load()
    const early = pendingFetch()
    const late = pendingFetch()
    const first = api.getMe()
    const second = api.getMe()
    await vi.advanceTimersByTimeAsync(1_000)
    early.answer()
    await first
    await vi.advanceTimersByTimeAsync(10_000)
    expect(waking.isApiWaking()).toBe(false)
    late.answer()
    await second
  })
})

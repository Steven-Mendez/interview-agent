/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Api from "@/lib/api"
import type * as Waking from "@/lib/api-waking"

// The cold-start flag, driven through request() (getMe, previewResume) the
// way the app drives it. fetch answers in call order: the watched request
// first, then the probe it sends. Each test imports fresh modules, so the
// flag and the probe in flight start over.

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

function probeCalls() {
  return fetchMock.mock.calls.filter(([url]) => url === "/api/healthz")
}

/** Lets settled fetches run their callbacks, without moving the clock. */
async function settle() {
  await vi.advanceTimersByTimeAsync(0)
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
  it("probes after 4 s without an answer, waking while the probe waits", async () => {
    const { api, waking } = await load()
    const slow = pendingFetch()
    const probe = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(3_999)
    expect(probeCalls()).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(probeCalls()).toEqual([
      ["/api/healthz", { credentials: "omit", cache: "no-store" }],
    ])
    // The probe, not the request, carries the notice: none yet while a warm
    // API would still be answering it.
    expect(waking.isApiWaking()).toBe(false)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(waking.isApiWaking()).toBe(true)

    probe.answer()
    await settle()
    expect(waking.isApiWaking()).toBe(false)
    slow.answer()
    await me
    expect(waking.isApiWaking()).toBe(false)
  })

  it("never probes for a fast answer", async () => {
    const { api, waking } = await load()
    const reply = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(500)
    reply.answer()
    await me
    await vi.advanceTimersByTimeAsync(10_000)
    expect(probeCalls()).toHaveLength(0)
    expect(waking.isApiWaking()).toBe(false)
  })

  it("shows nothing for a slow route of an API that answers the probe", async () => {
    const { api, waking } = await load()
    const slow = pendingFetch()
    const probe = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(4_000)
    expect(probeCalls()).toHaveLength(1)
    // Checked at every step up to an answer just inside the grace, not only
    // at the end: a shorter grace would flash the notice, and the status
    // region announce it, before the probe answers.
    for (let at = 4_000; at < 4_999; at += 100) {
      expect(waking.isApiWaking()).toBe(false)
      await vi.advanceTimersByTimeAsync(Math.min(100, 4_999 - at))
    }
    expect(waking.isApiWaking()).toBe(false)
    probe.answer()
    await settle()
    expect(waking.isApiWaking()).toBe(false)
    // The grace timer is gone once the probe answered: it cannot fire later.
    await vi.advanceTimersByTimeAsync(60_000)
    expect(waking.isApiWaking()).toBe(false)
    expect(probeCalls()).toHaveLength(1)
    slow.answer()
    await me
  })

  it("counts an error status from the probe as an answer", async () => {
    const { api, waking } = await load()
    const slow = pendingFetch()
    const probe = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(waking.isApiWaking()).toBe(true)

    probe.answer(503)
    await settle()
    expect(waking.isApiWaking()).toBe(false)
    slow.answer()
    await me
  })

  it("stops waking when the probe fails", async () => {
    const { api, waking } = await load()
    const slow = pendingFetch()
    const probe = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(waking.isApiWaking()).toBe(true)

    probe.fail()
    await settle()
    expect(waking.isApiWaking()).toBe(false)
    slow.fail()
    await expect(me).rejects.toThrow("Failed to fetch")
  })

  it("keeps waking when the slow request is aborted while the probe waits", async () => {
    const { api, waking } = await load()
    fetchMock.mockImplementationOnce(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError"))
          )
        })
    )
    const probe = pendingFetch()
    const controller = new AbortController()
    const preview = api.previewResume(
      new File(["%PDF"], "cv.pdf", { type: "application/pdf" }),
      controller.signal
    )
    await vi.advanceTimersByTimeAsync(5_000)
    expect(waking.isApiWaking()).toBe(true)

    controller.abort()
    await expect(preview).rejects.toMatchObject({ name: "AbortError" })
    expect(waking.isApiWaking()).toBe(true)

    probe.answer()
    await settle()
    expect(waking.isApiWaking()).toBe(false)
  })

  it("keeps waking when the slow request fails while the probe waits", async () => {
    const { api, waking } = await load()
    const slow = pendingFetch()
    const probe = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(5_000)
    slow.fail()
    await expect(me).rejects.toThrow("Failed to fetch")
    expect(waking.isApiWaking()).toBe(true)

    probe.answer()
    await settle()
    expect(waking.isApiWaking()).toBe(false)
  })

  it("sends one probe at a time", async () => {
    const { api, waking } = await load()
    const first = pendingFetch()
    const second = pendingFetch()
    const probe = pendingFetch()
    const third = pendingFetch()
    const one = api.getMe()
    const two = api.getMe()
    await vi.advanceTimersByTimeAsync(4_500)
    expect(probeCalls()).toHaveLength(1)

    // A request that goes slow while the probe waits shares it.
    const three = api.getMe()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetchMock).toHaveBeenCalledTimes(4)
    expect(probeCalls()).toHaveLength(1)
    expect(waking.isApiWaking()).toBe(true)

    probe.answer()
    await settle()
    expect(waking.isApiWaking()).toBe(false)
    first.answer()
    second.answer()
    third.answer()
    await Promise.all([one, two, three])
  })

  it("probes again for a later slow request once the last probe answered", async () => {
    const { api } = await load()
    const slow = pendingFetch()
    const probe = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(4_000)
    probe.answer()
    slow.answer()
    await me

    const later = pendingFetch()
    const again = pendingFetch()
    const next = api.getMe()
    await vi.advanceTimersByTimeAsync(4_000)
    expect(probeCalls()).toHaveLength(2)
    again.answer()
    later.answer()
    await next
  })
})

describe("wakeApi", () => {
  it("probes at once, waking only if the probe goes 4 s unanswered", async () => {
    const { waking } = await load()
    const probe = pendingFetch()
    waking.wakeApi()
    expect(probeCalls()).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(3_999)
    expect(waking.isApiWaking()).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(waking.isApiWaking()).toBe(true)

    probe.answer()
    await settle()
    expect(waking.isApiWaking()).toBe(false)
  })

  it("wakes the API once per page load", async () => {
    const { waking } = await load()
    const probe = pendingFetch()
    waking.wakeApi()
    probe.answer()
    await settle()
    waking.wakeApi()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("shares a probe already in flight", async () => {
    const { api, waking } = await load()
    const slow = pendingFetch()
    const probe = pendingFetch()
    const me = api.getMe()
    await vi.advanceTimersByTimeAsync(4_000)
    waking.wakeApi()
    expect(probeCalls()).toHaveLength(1)
    probe.answer()
    slow.answer()
    await me
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Api from "@/lib/api"

// request() is reached through the public calls: getMe for a call made as
// the user, getClosingState for one that carries a LiveKit participant
// token of its own. Each test imports a fresh api module, so the build-time
// env (VITE_API_BASE_URL) and the auth mode are read again.

const auth = vi.hoisted(() => ({
  enabled: true,
  token: vi.fn(),
  refresh: vi.fn(),
}))
vi.mock("@/lib/auth", () => ({
  get authEnabled() {
    return auth.enabled
  },
  getAccessToken: auth.token,
  refreshAccessToken: auth.refresh,
}))

const ME = {
  id: "user",
  email: null,
  name: null,
  is_admin: false,
  interviews_used: 0,
  interview_limit: 3,
  interviews_remaining: 3,
  demo_capacity_available: true,
}

const fetchMock = vi.fn<typeof fetch>()

function respond(status: number, body: unknown, headers?: HeadersInit) {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers })
  )
}

async function loadApi(): Promise<typeof Api> {
  vi.resetModules()
  return import("@/lib/api")
}

/** One response per call, in order. */
function respondInTurn(...replies: [status: number, body: unknown][]) {
  for (const [status, body] of replies) {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(body), { status })
    )
  }
}

function sent(call = 0): { url: string; headers: Headers } {
  const [url, init] = fetchMock.mock.calls[call]
  return { url: String(url), headers: new Headers(init?.headers) }
}

beforeEach(() => {
  auth.enabled = true
  auth.token.mockResolvedValue("user.jwt.token")
  auth.refresh.mockResolvedValue(null)
  vi.stubGlobal("fetch", fetchMock)
  respond(200, ME)
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.resetAllMocks()
})

describe("request", () => {
  it("adds the user's JWT as a bearer token", async () => {
    const api = await loadApi()
    expect(await api.getMe()).toEqual(ME)
    expect(sent().headers.get("Authorization")).toBe("Bearer user.jwt.token")
  })

  it("keeps a participant token and does not sign out on its 401", async () => {
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    respond(401, { detail: "Authentication required" })
    await expect(
      api.getClosingState("interview", "livekit.participant.token")
    ).rejects.toMatchObject({ status: 401 })
    expect(sent().headers.get("Authorization")).toBe(
      "Bearer livekit.participant.token"
    )
    expect(auth.token).not.toHaveBeenCalled()
    expect(auth.refresh).not.toHaveBeenCalled()
    expect(handler).not.toHaveBeenCalled()
  })

  it("hands a 401 on the user's JWT to the unauthorized handler", async () => {
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    respond(401, { detail: "Authentication required" })
    await expect(api.getMe()).rejects.toMatchObject({
      status: 401,
      message: "Authentication required",
    })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it("retries a refused JWT once with a fresh one before signing out", async () => {
    auth.refresh.mockResolvedValue("fresh.jwt.token")
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    respondInTurn(
      [401, { detail: "Authentication required" }],
      [401, { detail: "Authentication required" }]
    )
    await expect(api.getMe()).rejects.toMatchObject({ status: 401 })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(sent(0).headers.get("Authorization")).toBe("Bearer user.jwt.token")
    expect(sent(1).headers.get("Authorization")).toBe("Bearer fresh.jwt.token")
    expect(auth.refresh).toHaveBeenCalledTimes(1)
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it("stays signed in when the fresh JWT is accepted", async () => {
    auth.refresh.mockResolvedValue("fresh.jwt.token")
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    respondInTurn([401, { detail: "Authentication required" }], [200, ME])
    expect(await api.getMe()).toEqual(ME)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(handler).not.toHaveBeenCalled()
  })

  it("never signs out on a 503", async () => {
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    respond(503, { detail: "Authentication is temporarily unavailable" })
    await expect(api.getMe()).rejects.toMatchObject({
      status: 503,
      message: "Authentication is temporarily unavailable",
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(auth.refresh).not.toHaveBeenCalled()
    expect(handler).not.toHaveBeenCalled()
  })

  it("only throws on a 401 while a redirect is suppressed", async () => {
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    respond(401, { detail: "Authentication required" })
    const first = api.suppressUnauthorizedRedirect()
    const second = api.suppressUnauthorizedRedirect()
    await expect(api.getMe()).rejects.toMatchObject({ status: 401 })
    // Releasing one hold twice leaves the other in place.
    first()
    first()
    await expect(api.getMe()).rejects.toMatchObject({ status: 401 })
    expect(handler).not.toHaveBeenCalled()
    second()
    await expect(api.getMe()).rejects.toMatchObject({ status: 401 })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it("treats a 401 without any token as signed out too", async () => {
    auth.token.mockResolvedValue(null)
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    respond(401, { detail: "Authentication required" })
    await expect(api.getMe()).rejects.toMatchObject({ status: 401 })
    expect(sent().headers.has("Authorization")).toBe(false)
    expect(auth.refresh).not.toHaveBeenCalled()
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it("uses /api by default", async () => {
    const api = await loadApi()
    await api.getMe()
    expect(sent().url).toBe("/api/me")
  })

  it("uses VITE_API_BASE_URL when set, without its trailing slash", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.test/api/")
    const api = await loadApi()
    await api.getMe()
    expect(sent().url).toBe("https://api.example.test/api/me")
  })
})

describe("quotaErrorMessage", () => {
  it("maps the lifetime limit, with the user's limit when known", async () => {
    const api = await loadApi()
    const error = new api.ApiError(429, api.LIFETIME_LIMIT_REACHED, null)
    expect(api.quotaErrorMessage(error)).toBe(
      "You've used your 3 free interviews."
    )
    expect(api.quotaErrorMessage(error, 5)).toBe(
      "You've used your 5 free interviews."
    )
  })

  it("maps the monthly capacity to the day it resets", async () => {
    vi.useFakeTimers({ now: new Date("2026-10-30T12:00:00Z") })
    try {
      const api = await loadApi()
      // Retry-After: seconds until 00:00 UTC on November 1.
      const error = new api.ApiError(
        429,
        api.MONTHLY_CAPACITY_REACHED,
        36 * 3600
      )
      expect(api.quotaErrorMessage(error)).toBe(
        "The demo has reached its interview limit for this month. Try again on November 1."
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it("names the 1st even when the clocks disagree by a few seconds", async () => {
    // Retry-After counts to 00:00 UTC on November 1 by the server's clock.
    const message =
      "The demo has reached its interview limit for this month. Try again on November 1."
    for (const now of ["2026-10-30T12:00:02Z", "2026-10-30T11:59:58Z"]) {
      vi.useFakeTimers({ now: new Date(now) })
      try {
        const api = await loadApi()
        const error = new api.ApiError(
          429,
          api.MONTHLY_CAPACITY_REACHED,
          36 * 3600
        )
        expect(api.quotaErrorMessage(error)).toBe(message)
      } finally {
        vi.useRealTimers()
      }
    }
  })

  it("leaves every other error to the generic message", async () => {
    const api = await loadApi()
    expect(
      api.quotaErrorMessage(new api.ApiError(429, "Slow down", 5))
    ).toBeNull()
    expect(
      api.quotaErrorMessage(
        new api.ApiError(400, api.LIFETIME_LIMIT_REACHED, null)
      )
    ).toBeNull()
    expect(api.quotaErrorMessage(new Error("offline"))).toBeNull()
  })
})

describe("request without sign-in", () => {
  it("never attaches a JWT nor signs out on a 401", async () => {
    // Local mode as it really runs: the auth module itself, with no
    // VITE_NEON_AUTH_URL, so there is no client to take a token from. Last
    // in the file: the mock stays off from here on.
    vi.doUnmock("@/lib/auth")
    vi.stubEnv("VITE_NEON_AUTH_URL", "")
    const api = await loadApi()
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    await api.getMe()
    expect(sent().headers.has("Authorization")).toBe(false)
    respond(401, { detail: "Authentication required" })
    await expect(api.getMe()).rejects.toMatchObject({ status: 401 })
    expect(handler).not.toHaveBeenCalled()
  })
})

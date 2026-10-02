/** @vitest-environment jsdom */
import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Auth from "@/lib/auth"
import type * as Api from "@/lib/api"

// How the app learns its sign-in mode: fixed by a Neon build, otherwise
// asked of the API (GET /auth/config) in the browser. Each test loads fresh
// modules, so nothing is remembered between them.

const fetchMock = vi.fn<typeof fetch>()

function config(mode: string) {
  return new Response(JSON.stringify({ mode }), { status: 200 })
}

async function loadAuth(neonUrl = ""): Promise<typeof Auth> {
  vi.resetModules()
  vi.stubEnv("VITE_NEON_AUTH_URL", neonUrl)
  return import("@/lib/auth")
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock)
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.resetAllMocks()
  window.localStorage.clear()
})

describe("resolveAuthMode", () => {
  it("is neon in a Neon build, without asking the API", async () => {
    const auth = await loadAuth("https://auth.example.test/neondb/auth")
    expect(await auth.resolveAuthMode()).toBe("neon")
    auth.resetAuthMode()
    expect(await auth.resolveAuthMode()).toBe("neon")
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each(["local", "none"] as const)(
    "asks the API once otherwise (%s)",
    async (mode) => {
      fetchMock.mockResolvedValue(config(mode))
      const auth = await loadAuth()
      const both = await Promise.all([
        auth.resolveAuthMode(),
        auth.resolveAuthMode(),
      ])
      expect(both).toEqual([mode, mode])
      expect(await auth.resolveAuthMode()).toBe(mode)
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(String(fetchMock.mock.calls[0][0])).toBe("/api/auth/config")
    }
  )

  it("asks the API on its own origin when VITE_API_BASE_URL names it", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.test/api/")
    fetchMock.mockResolvedValue(config("local"))
    const auth = await loadAuth()
    await auth.resolveAuthMode()
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "https://api.example.test/api/auth/config"
    )
  })

  it("does not remember a failure: the next call asks again", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(new Response("", { status: 502 }))
      .mockResolvedValueOnce(config("local"))
    const auth = await loadAuth()
    await expect(auth.resolveAuthMode()).rejects.toThrow(
      "The API could not be reached."
    )
    await expect(auth.resolveAuthMode()).rejects.toThrow()
    expect(await auth.resolveAuthMode()).toBe("local")
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it("refuses an answer it does not know", async () => {
    fetchMock.mockResolvedValue(config("magic"))
    const auth = await loadAuth()
    await expect(auth.resolveAuthMode()).rejects.toThrow()
  })

  it("asks again after resetAuthMode, keeping the last answer meanwhile", async () => {
    fetchMock
      .mockResolvedValueOnce(config("none"))
      .mockResolvedValueOnce(config("local"))
    const auth = await loadAuth()
    const { result } = renderHook(() => auth.useAuthMode())
    await waitFor(() => expect(result.current).toBe("none"))

    auth.resetAuthMode()
    expect(result.current).toBe("none")
    await act(async () => {
      expect(await auth.resolveAuthMode()).toBe("local")
    })
    expect(result.current).toBe("local")
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("is asked again after a 401 the API client handled", async () => {
    fetchMock
      .mockResolvedValueOnce(config("none"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Authentication required" }), {
          status: 401,
        })
      )
      .mockResolvedValueOnce(config("local"))
    const auth = await loadAuth()
    const api: typeof Api = await import("@/lib/api")
    const handler = vi.fn()
    api.setUnauthorizedHandler(handler)
    expect(await auth.resolveAuthMode()).toBe("none")

    await expect(api.getMe()).rejects.toMatchObject({ status: 401 })
    expect(handler).toHaveBeenCalledTimes(1)
    expect(await auth.resolveAuthMode()).toBe("local")
  })
})

describe("useAuthMode", () => {
  it("is unknown until the API answers, and keeps asking a silent one", async () => {
    vi.useFakeTimers()
    try {
      fetchMock
        .mockRejectedValueOnce(new TypeError("Failed to fetch"))
        .mockResolvedValueOnce(config("local"))
      const auth = await loadAuth()
      const { result } = renderHook(() => auth.useAuthMode())
      expect(result.current).toBeUndefined()
      await act(() => vi.advanceTimersByTimeAsync(0))
      expect(result.current).toBeUndefined()
      await act(() => vi.advanceTimersByTimeAsync(5_000))
      expect(result.current).toBe("local")
      expect(fetchMock).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it("says why while the API cannot answer, until it does", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(config("none"))
    const auth = await loadAuth()
    const { result } = renderHook(() => ({
      mode: auth.useAuthMode(),
      error: auth.useAuthModeError(),
    }))
    await waitFor(() =>
      expect(result.current.error?.message).toBe(
        "The API could not be reached."
      )
    )
    expect(result.current.mode).toBeUndefined()

    await act(async () => {
      expect(await auth.resolveAuthMode()).toBe("none")
    })
    expect(result.current).toEqual({ mode: "none", error: null })
  })

  it("is neon from the first render in a Neon build", async () => {
    const auth = await loadAuth("https://auth.example.test/neondb/auth")
    const { result } = renderHook(() => auth.useAuthMode())
    expect(result.current).toBe("neon")
  })
})

describe("useCanCallApi", () => {
  it("lets every call out without sign-in", async () => {
    fetchMock.mockResolvedValue(config("none"))
    const auth = await loadAuth()
    const { result } = renderHook(() => auth.useCanCallApi())
    expect(result.current).toBe(false)
    await waitFor(() => expect(result.current).toBe(true))
  })

  it("waits for a local account to sign in", async () => {
    fetchMock.mockResolvedValue(config("local"))
    const auth = await loadAuth()
    const { result } = renderHook(() => ({
      mode: auth.useAuthMode(),
      canCallApi: auth.useCanCallApi(),
    }))
    await waitFor(() => expect(result.current.mode).toBe("local"))
    expect(result.current.canCallApi).toBe(false)
  })
})

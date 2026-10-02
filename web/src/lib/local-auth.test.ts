/** @vitest-environment jsdom */
import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Auth from "@/lib/auth"
import type * as LocalAuth from "@/lib/local-auth"

// The dev login against a fake API: POST /auth/local/sign-in answers with
// the API's own token, GET /auth/config says the mode is local.

const KEY = "interview-agent.local-session"
const fetchMock = vi.fn<typeof fetch>()

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status })
}

function signInAnswer(hours = 12, username = "guest") {
  return json(200, {
    token: `${username}.jwt.token`,
    expires_at: new Date(Date.now() + hours * 3_600_000).toISOString(),
    user: { id: `local:${username}`, name: username },
  })
}

/** Fresh auth and local-auth modules of a build without Neon Auth. */
async function load(): Promise<{
  auth: typeof Auth
  local: typeof LocalAuth
}> {
  vi.resetModules()
  vi.stubEnv("VITE_NEON_AUTH_URL", "")
  return {
    auth: await import("@/lib/auth"),
    local: await import("@/lib/local-auth"),
  }
}

/** The fake API: the config says local; sign-ins answer `reply`. */
function api(reply: () => Response | Promise<Response>) {
  fetchMock.mockImplementation((input) =>
    String(input).endsWith("/auth/config")
      ? Promise.resolve(json(200, { mode: "local" }))
      : Promise.resolve(reply())
  )
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock)
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.resetAllMocks()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe("signIn", () => {
  it("keeps the session and hands out its token", async () => {
    api(() => signInAnswer())
    const { auth, local } = await load()
    const session = await local.signIn("guest", "secret")
    expect(session.user).toEqual({ id: "local:guest", name: "guest" })

    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toBe("/api/auth/local/sign-in")
    expect(init?.method).toBe("POST")
    expect(new Headers(init?.headers).has("Authorization")).toBe(false)
    expect(JSON.parse(String(init?.body))).toEqual({
      username: "guest",
      password: "secret",
    })

    const stored = JSON.parse(window.localStorage.getItem(KEY) ?? "null")
    expect(stored).toMatchObject({
      token: "guest.jwt.token",
      user: { id: "local:guest", name: "guest" },
    })
    expect(await auth.getAccessToken()).toBe("guest.jwt.token")
    // Nothing to renew: the API's 401 means sign in again.
    expect(await auth.refreshAccessToken()).toBeNull()
  })

  it.each([401, 422])(
    "says the credentials are wrong on a %i",
    async (status) => {
      api(() => json(status, { detail: "Invalid username or password" }))
      const { local } = await load()
      await expect(local.signIn("guest", "wrong")).rejects.toThrow(
        "Invalid username or password"
      )
      expect(window.localStorage.getItem(KEY)).toBeNull()
    }
  )

  it("asks whether the API is running when it cannot be reached", async () => {
    api(() => {
      throw new TypeError("Failed to fetch")
    })
    const { local } = await load()
    await expect(local.signIn("guest", "secret")).rejects.toThrow(
      "Could not sign in. Is the API running?"
    )
  })

  it("fails when the browser will not keep the session", async () => {
    api(() => signInAnswer())
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Blocked", "SecurityError")
    })
    const { auth, local } = await load()
    await expect(local.signIn("guest", "secret")).rejects.toThrow(
      "Could not keep the session: browser storage is blocked."
    )
    expect(await auth.getAccessToken()).toBeNull()
  })

  it("says the same of any other failure", async () => {
    api(() => json(404, { detail: "Not Found" }))
    const { local } = await load()
    await expect(local.signIn("guest", "secret")).rejects.toThrow(
      "Could not sign in. Is the API running?"
    )
  })
})

describe("the local session", () => {
  it("drops a token that has expired", async () => {
    api(() => signInAnswer(-1))
    const { auth, local } = await load()
    await local.signIn("guest", "secret")
    expect(await auth.getAccessToken()).toBeNull()
    expect(window.localStorage.getItem(KEY)).toBeNull()
  })

  it("keeps an expired session while an interview holds it", async () => {
    api(() => signInAnswer(-1))
    const { auth, local } = await load()
    const { suppressUnauthorizedRedirect } =
      await import("@/lib/redirect-suppression")
    await local.signIn("guest", "secret")
    const release = suppressUnauthorizedRedirect()
    const { result } = renderHook(() => auth.useAuth())
    await waitFor(() => expect(result.current.isPending).toBe(false))

    // Its token no longer goes out, but the session stays, so nothing
    // sends the room to sign in.
    expect(await auth.getAccessToken()).toBeNull()
    expect(window.localStorage.getItem(KEY)).not.toBeNull()
    expect(result.current.user?.id).toBe("local:guest")

    // The interview over, the next ask ends it.
    release()
    await act(async () => {
      expect(await auth.getAccessToken()).toBeNull()
    })
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(result.current.user).toBeNull()
  })

  it("signs out, and every useAuth() says so", async () => {
    api(() => signInAnswer())
    const { auth, local } = await load()
    await local.signIn("guest", "secret")
    const { result } = renderHook(() => auth.useAuth())
    await waitFor(() => expect(result.current.user?.id).toBe("local:guest"))
    expect(result.current.user).toEqual({
      id: "local:guest",
      name: "guest",
      email: null,
      image: null,
    })

    await act(() => auth.signOut("local:guest"))
    expect(result.current.user).toBeNull()
    expect(await auth.getAccessToken()).toBeNull()
  })

  it("forgets a refused token only while it is still the one stored", async () => {
    api(() => signInAnswer())
    const { auth, local } = await load()
    await local.signIn("guest", "secret")
    auth.forgetRefusedToken("an.older.token")
    expect(await auth.getAccessToken()).toBe("guest.jwt.token")
    auth.forgetRefusedToken("guest.jwt.token")
    expect(await auth.getAccessToken()).toBeNull()
  })

  it("follows a sign-in and a sign-out in another tab", async () => {
    api(() => signInAnswer())
    const { auth } = await load()
    const { result } = renderHook(() => auth.useAuth())
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(result.current.user).toBeNull()

    // The other tab writes the storage this tab shares, then tells it.
    act(() => {
      window.localStorage.setItem(
        KEY,
        JSON.stringify({
          token: "admin.jwt.token",
          expiresAt: Date.now() + 3_600_000,
          user: { id: "local:admin", name: "admin" },
        })
      )
      window.dispatchEvent(new StorageEvent("storage", { key: KEY }))
    })
    expect(result.current.user?.id).toBe("local:admin")

    act(() => {
      window.localStorage.removeItem(KEY)
      window.dispatchEvent(new StorageEvent("storage", { key: KEY }))
    })
    expect(result.current.user).toBeNull()
  })

  it("hands React the same snapshot until the session changes", async () => {
    api(() => signInAnswer())
    const errors = vi.spyOn(console, "error").mockImplementation(() => {})
    const { auth, local } = await load()
    await local.signIn("guest", "secret")
    const first = local.getLocalSession()
    expect(local.getLocalSession()).toBe(first)

    const { result, rerender } = renderHook(() => auth.useAuth())
    await waitFor(() => expect(result.current.user).not.toBeNull())
    const user = result.current.user
    rerender()
    rerender()
    expect(result.current.user).toBe(user)
    expect(errors).not.toHaveBeenCalled()

    await local.signIn("guest", "secret")
    expect(local.getLocalSession()).not.toBe(first)
  })

  it("is no session while the API says there is no sign-in", async () => {
    fetchMock.mockResolvedValue(json(200, { mode: "none" }))
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        token: "left.over.token",
        expiresAt: Date.now() + 3_600_000,
        user: { id: "local:guest", name: "guest" },
      })
    )
    const { auth } = await load()
    const { result } = renderHook(() => auth.useAuth())
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(result.current.user).toBeNull()
  })
})

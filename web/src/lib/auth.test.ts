/** @vitest-environment jsdom */
import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Auth from "@/lib/auth"

// The Neon Auth adapter, faked: getJWTToken answers from the SDK's cache,
// getSession with X-Force-Fetch asks the auth server, and useSession is the
// session as Better Auth reports it to React.

interface FakeSession {
  data: { user: { id: string; name: string; email: string } } | null
  isPending: boolean
}

const sdk = vi.hoisted(() => {
  const session: FakeSession = { data: null, isPending: false }
  return {
    jwt: vi.fn<(allowAnonymous: boolean) => Promise<string | null>>(),
    getSession: vi.fn(),
    signOut: vi.fn(),
    session,
    listeners: new Set<() => void>(),
  }
})

function setSession(session: FakeSession) {
  sdk.session = session
  for (const listener of sdk.listeners) listener()
}

vi.mock("@neondatabase/neon-js/auth/react/adapters", async () => {
  const React = await import("react")
  const subscribe = (listener: () => void) => {
    sdk.listeners.add(listener)
    return () => {
      sdk.listeners.delete(listener)
    }
  }
  const client = {
    getSession: sdk.getSession,
    signOut: sdk.signOut,
    useSession: () => React.useSyncExternalStore(subscribe, () => sdk.session),
  }
  return {
    BetterAuthReactAdapter: () => () => ({
      getJWTToken: sdk.jwt,
      getBetterAuthInstance: () => client,
    }),
  }
})

/** A JWT whose payload says it expires `seconds` from now. */
function jwt(seconds: number, id = "a"): string {
  const encode = (value: object) =>
    btoa(JSON.stringify(value))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "")
  const exp = Math.floor(Date.now() / 1000) + seconds
  return `${encode({ alg: "EdDSA" })}.${encode({ sub: id, exp })}.signature`
}

async function loadAuth(): Promise<typeof Auth> {
  vi.resetModules()
  vi.stubEnv("VITE_NEON_AUTH_URL", "https://auth.example.test/neondb/auth")
  return import("@/lib/auth")
}

const ALICE = { id: "alice", name: "Alice", email: "alice@example.com" }

beforeEach(() => {
  sdk.session = { data: { user: ALICE }, isPending: false }
})
afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetAllMocks()
  sdk.listeners.clear()
})

describe("getAccessToken", () => {
  it("hands out the SDK's cached JWT while it has time left", async () => {
    const token = jwt(600)
    sdk.jwt.mockResolvedValue(token)
    const auth = await loadAuth()
    expect(await auth.getAccessToken()).toBe(token)
    expect(sdk.jwt).toHaveBeenCalledWith(false)
    expect(sdk.getSession).not.toHaveBeenCalled()
  })

  it("fetches a fresh session within a minute of expiry, once for all callers", async () => {
    const fresh = jwt(900, "fresh")
    sdk.jwt.mockResolvedValue(jwt(30))
    sdk.getSession.mockResolvedValue({
      data: { session: { token: fresh }, user: ALICE },
      error: null,
    })
    const auth = await loadAuth()
    const tokens = await Promise.all([
      auth.getAccessToken(),
      auth.getAccessToken(),
    ])
    expect(tokens).toEqual([fresh, fresh])
    expect(sdk.getSession).toHaveBeenCalledTimes(1)
    expect(sdk.getSession).toHaveBeenCalledWith({
      fetchOptions: { headers: { "X-Force-Fetch": "true" } },
    })
  })

  it("keeps the token in hand when the auth server cannot be reached", async () => {
    const stale = jwt(30)
    sdk.jwt.mockResolvedValue(stale)
    sdk.getSession.mockResolvedValue({
      data: null,
      error: { status: 502 },
    })
    const auth = await loadAuth()
    expect(await auth.getAccessToken()).toBe(stale)
  })

  it("has no token once the auth server says the session is gone", async () => {
    sdk.jwt.mockResolvedValue(jwt(30))
    sdk.getSession.mockResolvedValue({ data: null, error: null })
    const auth = await loadAuth()
    expect(await auth.getAccessToken()).toBeNull()
  })
})

describe("signOut", () => {
  it("reports signed out before Better Auth's session catches up", async () => {
    sdk.signOut.mockResolvedValue({ data: { success: true }, error: null })
    const auth = await loadAuth()
    const { result } = renderHook(() => ({
      auth: auth.useAuth(),
      canCallApi: auth.useCanCallApi(),
    }))
    expect(result.current.auth.user?.id).toBe("alice")

    await act(() => auth.signOut("alice"))
    // Better Auth still holds Alice (its refetch has not answered).
    expect(sdk.session.data?.user.id).toBe("alice")
    expect(result.current.auth.user).toBeNull()
    expect(result.current.canCallApi).toBe(false)

    // The refetch answers: signed out. Signing in again shows the user.
    act(() => setSession({ data: null, isPending: false }))
    act(() => setSession({ data: { user: ALICE }, isPending: false }))
    expect(result.current.auth.user?.id).toBe("alice")
  })

  it("brings the session back when the auth server refuses", async () => {
    sdk.signOut.mockResolvedValue({ data: null, error: { status: 500 } })
    const auth = await loadAuth()
    const { result } = renderHook(() => auth.useAuth())
    await act(() => auth.signOut("alice"))
    expect(result.current.user?.id).toBe("alice")
  })
})

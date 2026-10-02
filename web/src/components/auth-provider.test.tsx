/** @vitest-environment jsdom */
import { cleanup, render } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type { AuthMode, AuthState } from "@/lib/auth"
import { AuthProvider } from "./auth-provider"

interface Mocks {
  auth: AuthState
  mode: AuthMode | undefined
  invalidate: ReturnType<typeof vi.fn>
  reportingUser: ReturnType<typeof vi.fn>
}
const mocks = vi.hoisted((): Mocks => ({
  auth: { isPending: true, user: null },
  mode: "neon",
  invalidate: vi.fn(),
  reportingUser: vi.fn(),
}))
vi.mock("@/lib/auth", () => ({
  useAuth: () => mocks.auth,
  useAuthMode: () => mocks.mode,
}))
vi.mock("@/lib/error-reporting", () => ({
  setErrorReportingUser: mocks.reportingUser,
}))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useRouter: () => ({ invalidate: mocks.invalidate }),
}))

const user = (id: string) => ({ id, name: null, email: null, image: null })

let client: QueryClient

/** The app's root with the session reporting `auth`. */
function app(auth: AuthState) {
  mocks.auth = auth
  return (
    <QueryClientProvider client={client}>
      <AuthProvider>page</AuthProvider>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mocks.mode = "neon"
  client = new QueryClient()
  mocks.invalidate.mockResolvedValue(undefined)
})
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe("AuthProvider", () => {
  it("keeps the cache while the session first loads", () => {
    client.setQueryData(["me"], { id: "alice" })
    const { rerender } = render(app({ isPending: true, user: null }))
    rerender(app({ isPending: false, user: user("alice") }))
    expect(client.getQueryData(["me"])).toEqual({ id: "alice" })
    expect(mocks.invalidate).not.toHaveBeenCalled()
    expect(mocks.reportingUser).toHaveBeenLastCalledWith("alice")
  })

  it("drops another account's cache and reruns the guards when the user changes", () => {
    const { rerender } = render(app({ isPending: false, user: user("alice") }))
    client.setQueryData(["me"], { id: "alice" })
    client.setQueryData(["interviews", "a1"], { id: "a1" })

    // Signed out in another tab, then Bob signs in there.
    rerender(app({ isPending: false, user: null }))
    expect(client.getQueryData(["me"])).toBeUndefined()
    expect(client.getQueryData(["interviews", "a1"])).toBeUndefined()
    expect(mocks.invalidate).toHaveBeenCalledTimes(1)
    expect(mocks.reportingUser).toHaveBeenLastCalledWith(null)

    client.setQueryData(["me"], { id: "stale" })
    rerender(app({ isPending: false, user: user("bob") }))
    expect(client.getQueryData(["me"])).toBeUndefined()
    expect(mocks.invalidate).toHaveBeenCalledTimes(2)
    expect(mocks.reportingUser).toHaveBeenLastCalledWith("bob")
  })

  it("ignores a refetch of the same session", () => {
    const { rerender } = render(app({ isPending: false, user: user("alice") }))
    client.setQueryData(["me"], { id: "alice" })
    rerender(app({ isPending: true, user: user("alice") }))
    rerender(app({ isPending: false, user: user("alice") }))
    expect(client.getQueryData(["me"])).toEqual({ id: "alice" })
    expect(mocks.invalidate).not.toHaveBeenCalled()
  })

  it("follows a local account the same way, without naming it in reports", () => {
    mocks.mode = "local"
    const { rerender } = render(
      app({ isPending: false, user: user("local:a") })
    )
    client.setQueryData(["me"], { id: "local:a" })
    // A local id carries the username: error reports never get it.
    expect(mocks.reportingUser).not.toHaveBeenCalledWith("local:a")
    expect(mocks.reportingUser).toHaveBeenLastCalledWith(null)
    // Signed out in another tab.
    rerender(app({ isPending: false, user: null }))
    expect(client.getQueryData(["me"])).toBeUndefined()
    expect(mocks.invalidate).toHaveBeenCalledTimes(1)
  })

  it("drops a local account's cache when the API stops asking to sign in", () => {
    mocks.mode = "local"
    const { rerender } = render(
      app({ isPending: false, user: user("local:a") })
    )
    client.setQueryData(["me"], { id: "local:a" })
    // The API restarted without accounts: no one is signed in any more.
    mocks.mode = "none"
    rerender(app({ isPending: false, user: null }))
    expect(client.getQueryData(["me"])).toBeUndefined()
    expect(mocks.invalidate).toHaveBeenCalledTimes(1)
  })

  it("keeps the cache of the local developer (mode none)", () => {
    mocks.mode = "none"
    const { rerender } = render(app({ isPending: false, user: null }))
    client.setQueryData(["me"], { id: "local-dev" })
    rerender(app({ isPending: false, user: null }))
    expect(client.getQueryData(["me"])).toEqual({ id: "local-dev" })
    expect(mocks.invalidate).not.toHaveBeenCalled()
    expect(mocks.reportingUser).not.toHaveBeenCalledWith(expect.any(String))
  })

  it("watches nothing until the mode is known", () => {
    mocks.mode = undefined
    render(app({ isPending: false, user: null }))
    expect(mocks.reportingUser).not.toHaveBeenCalled()
  })
})

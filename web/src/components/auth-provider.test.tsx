/** @vitest-environment jsdom */
import { cleanup, render } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type { AuthState } from "@/lib/auth"
import { AuthProvider } from "./auth-provider"

const mocks = vi.hoisted(() => {
  const auth: AuthState = { isPending: true, user: null }
  return { auth, invalidate: vi.fn(), reportingUser: vi.fn() }
})
vi.mock("@/lib/auth", () => ({
  authClient: {},
  useAuth: () => mocks.auth,
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
})

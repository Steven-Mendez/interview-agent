/** @vitest-environment jsdom */
import * as React from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type * as Auth from "@/lib/auth"
import type { AuthMode, AuthUser } from "@/lib/auth"
import { ShellProvider } from "./app-shell"

const mocks = vi.hoisted(() => ({
  mode: undefined as AuthMode | undefined,
  user: null as AuthUser | null,
}))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useRouter: () => ({ navigate: vi.fn(), invalidate: vi.fn() }),
  useRouterState: () => "/",
  Link: React.forwardRef<
    HTMLAnchorElement,
    React.ComponentPropsWithoutRef<"a"> & { to: string }
  >(({ to, ...props }, ref) => <a href={to} {...props} ref={ref} />),
}))
vi.mock("@/lib/auth", async (original) => ({
  ...(await original<typeof Auth>()),
  useAuthMode: () => mocks.mode,
  useAuth: () => ({ isPending: false, user: mocks.user }),
}))
vi.mock("@/hooks/use-me", () => ({ useMe: () => ({ data: undefined }) }))
vi.mock("@/hooks/use-sign-out", () => ({ useSignOut: () => vi.fn() }))

const GUEST: AuthUser = {
  id: "local:guest",
  name: "guest",
  email: null,
  image: null,
}

beforeEach(() => {
  mocks.mode = undefined
  mocks.user = null
})

afterEach(cleanup)

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ShellProvider>
        <p>The page</p>
      </ShellProvider>
    </QueryClientProvider>
  )
}

function chrome(container: HTMLElement) {
  return container.querySelector("[data-chrome]")?.getAttribute("data-chrome")
}

describe("AppShell", () => {
  it("shows no chrome until it knows how the app signs in", () => {
    const { container } = mount()
    expect(chrome(container)).toBe("false")
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull()
    expect(screen.getByText("The page")).toBeTruthy()
  })

  it.each<AuthMode>(["local", "neon"])(
    "shows no chrome to a signed-out visitor (%s)",
    (mode) => {
      mocks.mode = mode
      const { container } = mount()
      expect(chrome(container)).toBe("false")
      expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull()
    }
  )

  it("shows the chrome to a signed-in user", () => {
    mocks.mode = "local"
    mocks.user = GUEST
    const { container } = mount()
    expect(chrome(container)).toBe("true")
    expect(
      screen.getAllByRole("navigation", { name: "Main" }).length
    ).toBeGreaterThan(0)
  })

  it("shows the chrome without sign-in", () => {
    mocks.mode = "none"
    const { container } = mount()
    expect(chrome(container)).toBe("true")
  })
})

describe("ApiWakingNotice", () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it("says the server is starting while the API has not answered", async () => {
    vi.useFakeTimers()
    const { watchApiFetch } = await import("@/lib/api-waking")
    let answer!: (res: Response) => void
    const fetching = watchApiFetch(
      new Promise<Response>((resolve) => {
        answer = resolve
      })
    )
    mount()
    expect(screen.queryByText(/Starting the server/)).toBeNull()

    await act(() => vi.advanceTimersByTimeAsync(4_000))
    expect(screen.getByRole("status").textContent).toBe(
      "Starting the server, this can take a few seconds…"
    )
    // Above the page, not over it: the page is still there to read.
    expect(screen.getByText("The page")).toBeTruthy()

    await act(async () => {
      answer(new Response(null, { status: 204 }))
      await fetching
    })
    expect(screen.queryByText(/Starting the server/)).toBeNull()
  })
})

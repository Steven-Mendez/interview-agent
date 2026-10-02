/** @vitest-environment jsdom */
import * as React from "react"
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type * as Api from "@/lib/api"
import type { AdminUser, Me } from "@/lib/api"
import type * as Auth from "@/lib/auth"
import type { AuthMode, AuthState } from "@/lib/auth"
import { ApiError } from "@/lib/api"
import { Route as AdminUsersRoute } from "@/routes/admin.users"
import { Route as AuthRoute } from "@/routes/auth.$pathname"
import { Route as HomeRoute } from "@/routes/index"
import { Route as ProfileRoute } from "@/routes/profile"

// The profile page and the admin user list, rendered as their routes'
// components against a mocked API and session.

const mocks = vi.hoisted(() => {
  const session: {
    mode: AuthMode | undefined
    modeError: Error | null
    auth: AuthState
  } = {
    mode: "local",
    modeError: null,
    auth: { isPending: false, user: null },
  }
  return {
    me: vi.fn(),
    list: vi.fn(),
    users: vi.fn(),
    navigate: vi.fn(),
    resolveMode: vi.fn(),
    session,
  }
})
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof Api>()),
  getMe: mocks.me,
  listInterviews: mocks.list,
  getAdminUsers: mocks.users,
}))
vi.mock("@/lib/auth", async (original) => ({
  ...(await original<typeof Auth>()),
  useAuthMode: () => mocks.session.mode,
  useAuthModeError: () => mocks.session.modeError,
  resolveAuthMode: mocks.resolveMode,
  useAuth: () => mocks.session.auth,
  useCanCallApi: () => mocks.session.mode !== undefined,
}))
// The home page's animated mascot asks for matchMedia, which jsdom lacks.
vi.mock("@/components/home-mascot", () => ({ HomeMascot: () => null }))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useNavigate: () => mocks.navigate,
  useRouter: () => ({ navigate: mocks.navigate }),
  Link: React.forwardRef<
    HTMLAnchorElement,
    React.ComponentPropsWithoutRef<"a"> & { to: string }
  >(({ to, ...props }, ref) => <a href={to} {...props} ref={ref} />),
}))

const GUEST: Me = {
  id: "local:guest",
  email: null,
  name: "guest",
  is_admin: false,
  interviews_used: 1,
  interview_limit: 3,
  interviews_remaining: 2,
  demo_capacity_available: true,
  auth_provider: "local",
  created_at: "2026-09-01T10:00:00+00:00",
  last_seen_at: "2026-10-01T08:30:00+00:00",
}

const ADMIN: Me = {
  ...GUEST,
  id: "neon-user-id",
  name: "Ada",
  email: "ada@example.com",
  is_admin: true,
  interviews_used: 12,
  interview_limit: null,
  interviews_remaining: null,
  auth_provider: "neon",
}

beforeEach(() => {
  mocks.session.mode = "local"
  mocks.session.modeError = null
  mocks.session.auth = {
    isPending: false,
    user: { id: "local:guest", name: "guest", email: null, image: null },
  }
  mocks.list.mockResolvedValue({ total: 4, items: [] })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.resetAllMocks()
})

async function mount(
  Page: React.ComponentType & { preload?: () => Promise<void> }
) {
  // Code-split route components: imported before the findBy clocks start.
  await Page.preload?.()
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  await act(async () => {
    render(
      <QueryClientProvider client={client}>
        <Page />
      </QueryClientProvider>
    )
  })
  return client
}

/** The text of the profile row labelled `label`. */
function row(label: string): string {
  const term = screen.getByText(label, { selector: "dt" })
  return term.parentElement?.querySelector("dd")?.textContent ?? ""
}

describe("/profile", () => {
  const Page = ProfileRoute.options.component as React.ComponentType

  it("shows a guest's account, quota and history", async () => {
    mocks.me.mockResolvedValue(GUEST)
    await mount(Page)
    await screen.findByText("1 of 3 interviews used")
    expect(row("Name")).toBe("guest")
    expect(screen.queryByText("Email", { selector: "dt" })).toBeNull()
    expect(row("Sign-in method")).toBe("Local account")
    expect(screen.queryByText("Admin")).toBeNull()
    expect(row("Quota")).toContain("2 remaining")
    expect(row("Member since")).not.toBe("")
    expect(row("Last seen")).not.toBe("—")
    await vi.waitFor(() => expect(row("In your history")).toContain("4"))
    expect(
      screen.getByRole("link", { name: /View history/ }).getAttribute("href")
    ).toBe("/interviews")
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy()
  })

  it("shows an admin as such, without a limit", async () => {
    mocks.session.mode = "neon"
    mocks.me.mockResolvedValue(ADMIN)
    await mount(Page)
    await screen.findByText("Unlimited interviews")
    expect(row("Name")).toBe("AdaAdmin")
    expect(row("Email")).toBe("ada@example.com")
    expect(row("Sign-in method")).toBe("Neon Auth")
  })

  it("names the local developer and offers no sign-out without sign-in", async () => {
    mocks.session.mode = "none"
    mocks.session.auth = { isPending: false, user: null }
    mocks.me.mockResolvedValue({
      ...ADMIN,
      id: "local-dev",
      name: "Local developer",
      email: null,
      auth_provider: "local",
    })
    await mount(Page)
    await screen.findByText("Unlimited interviews")
    expect(row("Sign-in method")).toBe("Local developer (no sign-in)")
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull()
  })
})

const USER: AdminUser = {
  id: "local:guest",
  name: "guest",
  email: null,
  auth_provider: "local",
  is_admin: false,
  created_at: "2026-09-01T10:00:00+00:00",
  last_seen_at: "2026-10-01T08:30:00+00:00",
  interviews_used: 2,
  interview_limit: 3,
  interviews_stored: 1,
  last_interview_at: "2026-09-30T18:00:00+00:00",
}

describe("/admin/users", () => {
  const Page = AdminUsersRoute.options.component as React.ComponentType

  beforeEach(() => {
    vi.spyOn(AdminUsersRoute, "useSearch").mockReturnValue({})
  })

  it("lists the users, a page at a time", async () => {
    mocks.users.mockResolvedValue({
      total: 2,
      items: [
        USER,
        {
          ...USER,
          id: "neon-user-id",
          name: null,
          email: "ada@example.com",
          auth_provider: "neon",
          is_admin: true,
          interviews_used: 12,
          interview_limit: null,
          interviews_stored: 9,
          last_seen_at: null,
        },
      ],
    })
    await mount(Page)
    const table = await screen.findByRole("table")
    expect(mocks.users).toHaveBeenCalledWith({ limit: 50, offset: 0 })

    const [, guest, admin] = within(table).getAllByRole("row")
    const cells = (tr: HTMLElement) =>
      within(tr)
        .getAllByRole("cell")
        .map((cell) => cell.textContent)
    expect(cells(guest).slice(0, 6)).toEqual([
      "guest",
      "—",
      "Local account",
      "Guest",
      "2 of 3",
      "1",
    ])
    expect(cells(admin).slice(0, 6)).toEqual([
      "neon-user-id",
      "ada@example.com",
      "Neon Auth",
      "Admin",
      "Unlimited",
      "9",
    ])
    expect(cells(admin)[8]).toBe("—")
    expect(screen.queryByRole("navigation", { name: "Pagination" })).toBeNull()
  })

  it("asks for the page in the URL and pages on", async () => {
    vi.spyOn(AdminUsersRoute, "useSearch").mockReturnValue({ offset: 50 })
    mocks.users.mockResolvedValue({ total: 120, items: [USER] })
    await mount(Page)
    await screen.findByRole("table")
    expect(mocks.users).toHaveBeenCalledWith({ limit: 50, offset: 50 })
    expect(screen.getByText("51–100 of 120")).toBeTruthy()
  })

  it("tells anyone but an admin that the list is for admins", async () => {
    mocks.users.mockRejectedValue(
      new ApiError(403, "Admin access required", null)
    )
    await mount(Page)
    await screen.findByText("Admins only")
    expect(screen.queryByRole("table")).toBeNull()
    expect(mocks.users).toHaveBeenCalledTimes(1)
  })
})

describe("/", () => {
  const Page = HomeRoute.options.component as React.ComponentType

  it("says so when the API cannot tell how to sign in, and asks again", async () => {
    mocks.session.mode = undefined
    mocks.session.modeError = new Error("The API could not be reached.")
    mocks.session.auth = { isPending: true, user: null }
    mocks.resolveMode.mockResolvedValue("local")
    await mount(Page)
    expect(
      screen.getByText(
        "Your interviews could not be loaded — The API could not be reached."
      )
    ).toBeTruthy()
    expect(screen.queryByText("Recent interviews")).toBeNull()
    expect(mocks.list).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "Try again" }))
    expect(mocks.resolveMode).toHaveBeenCalledTimes(1)
  })

  it("loads as before while the mode is only pending", async () => {
    mocks.session.mode = undefined
    mocks.session.auth = { isPending: true, user: null }
    await mount(Page)
    expect(screen.queryByRole("alert")).toBeNull()
    expect(screen.getByText("Recent interviews")).toBeTruthy()
  })
})

describe("/auth/$pathname", () => {
  it("keeps only a destination on this site", () => {
    const validate = AuthRoute.options.validateSearch as (
      search: Record<string, unknown>
    ) => { redirectTo?: string }
    expect(validate({ redirectTo: "//evil.example/x" })).toEqual({
      redirectTo: undefined,
    })
    expect(validate({ redirectTo: "/interviews?offset=20#top" })).toEqual({
      redirectTo: "/interviews?offset=20#top",
    })
  })
})

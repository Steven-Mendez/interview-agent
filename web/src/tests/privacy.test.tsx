/** @vitest-environment jsdom */
import * as React from "react"
import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type * as Auth from "@/lib/auth"
import type { AuthMode, AuthState } from "@/lib/auth"
import { Route as PrivacyRoute } from "@/routes/privacy"

// The privacy policy, rendered as its route's component for a visitor who
// has not signed in: Google's consent screen sends people here before they
// have an account.

const mocks = vi.hoisted(() => {
  const session: { mode: AuthMode | undefined; auth: AuthState } = {
    mode: "neon",
    auth: { isPending: false, user: null },
  }
  return { session }
})
vi.mock("@/lib/auth", async (original) => ({
  ...(await original<typeof Auth>()),
  useAuthMode: () => mocks.session.mode,
  useAuth: () => mocks.session.auth,
}))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  Link: React.forwardRef<
    HTMLAnchorElement,
    React.ComponentPropsWithoutRef<"a"> & {
      to: string
      params?: Record<string, string>
    }
  >(({ to, params, ...props }, ref) => (
    <a
      href={to.replace(/\$(\w+)/g, (_, name: string) => params?.[name] ?? "")}
      {...props}
      ref={ref}
    />
  )),
}))

beforeEach(() => {
  mocks.session.mode = "neon"
  mocks.session.auth = { isPending: false, user: null }
})
afterEach(cleanup)

async function mount() {
  const Page = PrivacyRoute.options.component as React.ComponentType & {
    preload?: () => Promise<void>
  }
  // Code-split route components: imported before rendering.
  await Page.preload?.()
  await act(async () => {
    render(<Page />)
  })
}

describe("/privacy", () => {
  it("has no sign-in guard", () => {
    expect(PrivacyRoute.options.beforeLoad).toBeUndefined()
  })

  it("renders for a visitor who is signed out", async () => {
    await mount()
    expect(
      screen.getByRole("heading", { level: 1, name: "Privacy policy" })
    ).toBeTruthy()
    const contacts = screen.getAllByRole("link", {
      name: "stevenampaiz@gmail.com",
    })
    expect(contacts[0].getAttribute("href")).toBe(
      "mailto:stevenampaiz@gmail.com"
    )
    expect(
      screen.getByRole("heading", { level: 2, name: "How long it is kept" })
    ).toBeTruthy()
    expect(
      screen.getByRole("link", { name: "Back to sign in" }).getAttribute("href")
    ).toBe("/auth/sign-in")
    expect(
      screen
        .getByRole("link", { name: "Terms of service" })
        .getAttribute("href")
    ).toBe("/terms")
  })

  it("leads a signed-in user back home", async () => {
    mocks.session.auth = {
      isPending: false,
      user: { id: "neon-user-id", name: "Ada", email: null, image: null },
    }
    await mount()
    expect(
      screen.getByRole("link", { name: "Back to Home" }).getAttribute("href")
    ).toBe("/")
  })
})

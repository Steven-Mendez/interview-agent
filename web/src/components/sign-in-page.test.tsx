/** @vitest-environment jsdom */
import * as React from "react"
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type * as Auth from "@/lib/auth"
import type { AuthMode } from "@/lib/auth"
import type * as LocalAuth from "@/lib/local-auth"
import { ShellProvider } from "@/components/app-shell"
import {
  ERROR_MS,
  GREETING_MS,
  SUCCESS_MS,
  SignInPage,
  TICKLE_MS,
} from "./sign-in-page"
import {
  LocalSignIn,
  LocalSignOut,
  NeonAuthMissing,
  NeonAuthViews,
} from "./sign-in-views"

const mocks = vi.hoisted(() => {
  const session: { mode: AuthMode } = { mode: "local" }
  return {
    session,
    navigate: vi.fn(),
    invalidate: vi.fn(),
    signIn: vi.fn(),
    signOutLocal: vi.fn(),
    resetAuthMode: vi.fn(),
    authView: vi.fn(),
  }
})
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useRouter: () => ({ navigate: mocks.navigate, invalidate: mocks.invalidate }),
  useNavigate: () => mocks.navigate,
  useRouterState: () => "/auth/sign-in",
  Link: React.forwardRef<
    HTMLAnchorElement,
    React.ComponentPropsWithoutRef<"a"> & { to: string }
  >(({ to, ...props }, ref) => <a href={to} {...props} ref={ref} />),
}))
vi.mock("@/lib/auth", async (original) => ({
  ...(await original<typeof Auth>()),
  useAuthMode: () => mocks.session.mode,
  useAuth: () => ({ isPending: false, user: null }),
  resetAuthMode: mocks.resetAuthMode,
}))
vi.mock("@/lib/local-auth", async (original) => ({
  ...(await original<typeof LocalAuth>()),
  signIn: mocks.signIn,
  signOutLocal: mocks.signOutLocal,
}))
// Neon Auth's view, reduced to what the page hands it.
vi.mock("@neondatabase/auth-ui", () => ({
  AuthView: (props: { cardHeader?: React.ReactNode }) => {
    mocks.authView(props)
    return <div data-testid="auth-view">{props.cardHeader}</div>
  },
  NeonAuthUIProvider: ({ children }: { children: React.ReactNode }) => children,
}))

beforeEach(() => {
  mocks.session.mode = "local"
  vi.useFakeTimers()
  // jsdom has no matchMedia; the character asks it about reduced motion.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: true,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

function mount(card: React.ReactNode, resting?: "greeting") {
  const client = new QueryClient()
  return render(
    <QueryClientProvider client={client}>
      <SignInPage resting={resting}>{card}</SignInPage>
    </QueryClientProvider>
  )
}

function mascot() {
  return screen.getByRole("button", { name: "Say hi to the interviewer" })
}

function mascotState() {
  return mascot().dataset.mascotState
}

async function advance(ms: number) {
  await act(() => vi.advanceTimersByTimeAsync(ms))
}

function fillIn(username: string, password: string) {
  fireEvent.change(screen.getByLabelText("Username"), {
    target: { value: username },
  })
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: password },
  })
}

describe("SignInPage", () => {
  it("hides the app's top bar and navigation while shown", () => {
    const client = new QueryClient()
    const { container } = render(
      <QueryClientProvider client={client}>
        <ShellProvider>
          <SignInPage>
            <p>The card</p>
          </SignInPage>
        </ShellProvider>
      </QueryClientProvider>
    )
    expect(
      container
        .querySelector("[data-immersive]")
        ?.getAttribute("data-immersive")
    ).toBe("true")
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Main menu" })).toBeNull()
    expect(screen.getByText("The card")).toBeTruthy()
    // Every other page needs the session: there is no way "home" from here.
    expect(screen.queryByRole("link", { name: /home/i })).toBeNull()
  })

  it("says hello on arrival, then rests", async () => {
    mount(<p>The card</p>)
    expect(mascotState()).toBe("greeting")
    await advance(GREETING_MS - 1)
    expect(mascotState()).toBe("greeting")
    await advance(1)
    expect(mascotState()).toBe("idle")
  })

  it("giggles when poked, then goes back to what it was doing", async () => {
    mount(<p>The card</p>)
    await advance(GREETING_MS)
    // A real button: reachable and pressable from the keyboard.
    expect(mascot().tagName).toBe("BUTTON")
    expect(mascot().getAttribute("type")).toBe("button")
    fireEvent.click(mascot())
    expect(mascotState()).toBe("tickled")
    await advance(TICKLE_MS)
    expect(mascotState()).toBe("idle")
  })

  it("leaves no timer behind once gone", async () => {
    const { unmount } = mount(<p>The card</p>)
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe("the local sign-in", () => {
  it("shows Google and GitHub as coming soon, doing nothing when pressed", () => {
    mount(<LocalSignIn />)
    for (const name of ["Continue with Google", "Continue with GitHub"]) {
      const button = screen.getByRole("button", { name })
      expect(button.getAttribute("aria-disabled")).toBe("true")
      const described = button.getAttribute("aria-describedby") ?? ""
      expect(document.getElementById(described)?.textContent).toContain(
        "Google and GitHub sign-in are coming soon"
      )
      expect(() => fireEvent.click(button)).not.toThrow()
    }
    expect(
      screen.getAllByText(/coming soon — they need Neon Auth/)
    ).toHaveLength(1)
    expect(screen.getByText("or use a local account")).toBeTruthy()
    expect(mocks.navigate).not.toHaveBeenCalled()
    expect(mocks.signIn).not.toHaveBeenCalled()
  })

  it("greets, then rests even with the username focused for the visitor", async () => {
    mount(<LocalSignIn />)
    expect(screen.getByRole("heading", { name: "Welcome back" })).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByLabelText("Username"))
    expect(mascotState()).toBe("greeting")
    await advance(GREETING_MS)
    expect(mascotState()).toBe("idle")
  })

  it("watches the username and waits through the password", async () => {
    mount(<LocalSignIn />)
    fireEvent.focus(screen.getByLabelText("Username"))
    expect(mascotState()).toBe("watching")
    fireEvent.focus(screen.getByLabelText("Password"))
    expect(mascotState()).toBe("waiting")
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "gu" },
    })
    expect(mascotState()).toBe("watching")
    // The greeting's end does not undo it.
    await advance(GREETING_MS)
    expect(mascotState()).toBe("watching")
  })

  it("thinks while checking, is upset by a refusal, then watches again", async () => {
    const { LocalSignInError } = await import("@/lib/local-auth")
    let refuse: (error: Error) => void = () => {}
    mocks.signIn.mockReturnValue(
      new Promise<void>((_, reject) => {
        refuse = reject
      })
    )
    mount(<LocalSignIn redirectTo="/profile" />)
    fillIn("guest", "wrong")
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    expect(mascotState()).toBe("thinking")

    await act(async () => refuse(new LocalSignInError("Invalid password")))
    expect(screen.getByRole("alert").textContent).toContain("Invalid password")
    expect(mascotState()).toBe("error")
    // The focus is back in the password, and that does not cut the error short.
    expect(document.activeElement).toBe(screen.getByLabelText("Password"))
    await advance(ERROR_MS - 1)
    expect(mascotState()).toBe("error")
    await advance(1)
    expect(mascotState()).toBe("watching")
    expect(mocks.navigate).not.toHaveBeenCalled()
  })

  it("is pleased before leaving for the destination", async () => {
    mocks.signIn.mockResolvedValue(undefined)
    mocks.navigate.mockResolvedValue(undefined)
    mount(<LocalSignIn redirectTo="/profile" />)
    fillIn("guest", "secret")
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    await advance(0)
    expect(mascotState()).toBe("success")
    // Poking it does not interrupt the moment.
    fireEvent.click(mascot())
    expect(mascotState()).toBe("success")
    expect(mocks.navigate).not.toHaveBeenCalled()

    await advance(SUCCESS_MS)
    expect(mocks.navigate).toHaveBeenCalledWith({
      href: "/profile",
      replace: true,
    })
  })

  it("leaves no timer behind once gone, poked or refused", async () => {
    const { LocalSignInError } = await import("@/lib/local-auth")
    const { unmount } = mount(<LocalSignIn />)
    await advance(GREETING_MS)
    fireEvent.click(mascot())
    expect(mascotState()).toBe("tickled")
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    unmount()
    expect(vi.getTimerCount()).toBe(0)

    mocks.signIn.mockRejectedValue(new LocalSignInError("Invalid password"))
    mount(<LocalSignIn />)
    await advance(GREETING_MS)
    fillIn("guest", "wrong")
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    await advance(0)
    expect(mascotState()).toBe("error")
    // jsdom's own selectionchange, from the focus moving back.
    await advance(0)
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    cleanup()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("does not leave once the page is gone", async () => {
    mocks.signIn.mockResolvedValue(undefined)
    const { unmount } = mount(<LocalSignIn />)
    fillIn("guest", "secret")
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    await advance(0)
    unmount()
    await advance(SUCCESS_MS)
    expect(mocks.navigate).not.toHaveBeenCalled()
  })

  it("waves goodbye while signing out, and goes home", async () => {
    mocks.navigate.mockResolvedValue(undefined)
    mount(<LocalSignOut />, "greeting")
    expect(screen.getByRole("status").textContent).toContain("Signing you out…")
    expect(mocks.signOutLocal).toHaveBeenCalledOnce()
    expect(mocks.navigate).toHaveBeenCalledWith({ to: "/", replace: true })
    await advance(GREETING_MS)
    expect(mascotState()).toBe("greeting")
  })
})

describe("the Neon Auth sign-in", () => {
  it("hands Neon Auth's view the providers' layout and the destination", async () => {
    mocks.session.mode = "neon"
    mount(<NeonAuthViews pathname="sign-in" redirectTo="/profile" />)
    expect(screen.getByTestId("auth-view")).toBeTruthy()
    expect(mocks.authView).toHaveBeenCalledWith(
      expect.objectContaining({
        path: "sign-in",
        redirectTo: "/profile",
        socialLayout: "vertical",
        localization: { SIGN_IN_WITH: "Continue with" },
      })
    )
    expect(screen.getByRole("heading", { name: "Welcome back" })).toBeTruthy()
    expect(mascotState()).toBe("greeting")
    await advance(GREETING_MS)
    expect(mascotState()).toBe("idle")
  })

  it("says so while signing out", () => {
    mocks.session.mode = "neon"
    mount(<NeonAuthViews pathname="sign-out" />, "greeting")
    expect(screen.getByRole("status").textContent).toContain("Signing you out…")
    expect(mocks.authView).toHaveBeenCalledWith(
      expect.objectContaining({ path: "sign-out", redirectTo: "/" })
    )
  })

  it("explains a build without Neon Auth, the providers disabled", () => {
    mount(<NeonAuthMissing />)
    expect(
      screen.getByRole("heading", {
        name: "Sign-in is not set up in this build",
      })
    ).toBeTruthy()
    for (const name of ["Continue with Google", "Continue with GitHub"]) {
      const button = screen.getByRole("button", { name })
      expect(button.getAttribute("aria-disabled")).toBe("true")
      fireEvent.click(button)
    }
    expect(screen.getByRole("alert").textContent).toContain(
      "VITE_NEON_AUTH_URL"
    )
    expect(mocks.navigate).not.toHaveBeenCalled()
  })
})

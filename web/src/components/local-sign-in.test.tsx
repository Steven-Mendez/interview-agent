/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type * as LocalAuth from "@/lib/local-auth"
import { LocalSignInForm } from "./local-sign-in"

const mocks = vi.hoisted(() => ({ navigate: vi.fn(), signIn: vi.fn() }))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useRouter: () => ({ navigate: mocks.navigate }),
}))
vi.mock("@/lib/local-auth", async (original) => ({
  ...(await original<typeof LocalAuth>()),
  signIn: mocks.signIn,
}))

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function fillIn(username: string, password: string) {
  fireEvent.change(screen.getByLabelText("Username"), {
    target: { value: username },
  })
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: password },
  })
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
}

describe("LocalSignInForm", () => {
  it("signs in and lands where the visitor was going, query and hash included", async () => {
    let finish: () => void = () => {}
    mocks.signIn.mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve
      })
    )
    mocks.navigate.mockResolvedValue(undefined)
    render(<LocalSignInForm redirectTo="/interviews?status=planned#top" />)
    fillIn("guest", "secret")

    // Pending: the button says so and cannot be pressed twice.
    const pending = await screen.findByRole<HTMLButtonElement>("button", {
      name: "Signing in…",
    })
    expect(pending.disabled).toBe(true)
    expect(mocks.signIn).toHaveBeenCalledWith("guest", "secret")

    finish()
    await vi.waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith({
        href: "/interviews?status=planned#top",
        replace: true,
      })
    )
  })

  it("lands on the home page without a destination", async () => {
    mocks.signIn.mockResolvedValue(undefined)
    mocks.navigate.mockResolvedValue(undefined)
    render(<LocalSignInForm />)
    fillIn("guest", "secret")
    await vi.waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith({ href: "/", replace: true })
    )
  })

  it("shows why the sign-in failed and stays", async () => {
    const { LocalSignInError } = await import("@/lib/local-auth")
    mocks.signIn.mockRejectedValue(
      new LocalSignInError("Invalid username or password")
    )
    render(<LocalSignInForm redirectTo="/profile" />)
    fillIn("guest", "wrong")
    const alert = await screen.findByRole("alert")
    expect(alert.textContent).toContain("Invalid username or password")
    expect(mocks.navigate).not.toHaveBeenCalled()
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Sign in" })
        .disabled
    ).toBe(false)
  })
})

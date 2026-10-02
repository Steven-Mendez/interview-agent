/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import type * as LocalAuth from "@/lib/local-auth"
import { LocalSignInForm } from "./local-sign-in"

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  invalidate: vi.fn(),
  signIn: vi.fn(),
  resetAuthMode: vi.fn(),
}))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useRouter: () => ({ navigate: mocks.navigate, invalidate: mocks.invalidate }),
}))
vi.mock("@/lib/auth", () => ({ resetAuthMode: mocks.resetAuthMode }))
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
  it("signs in with what a browser autofilled, without change events", async () => {
    mocks.signIn.mockResolvedValue(undefined)
    mocks.navigate.mockResolvedValue(undefined)
    render(<LocalSignInForm />)
    // Autofill writes the fields without the events React follows.
    screen.getByLabelText<HTMLInputElement>("Username").value = "guest"
    screen.getByLabelText<HTMLInputElement>("Password").value = "secret"
    const submit = screen.getByRole<HTMLButtonElement>("button", {
      name: "Sign in",
    })
    expect(submit.disabled).toBe(false)
    fireEvent.click(submit)
    await vi.waitFor(() =>
      expect(mocks.signIn).toHaveBeenCalledWith("guest", "secret")
    )
  })

  it("asks for both fields instead of sending an empty attempt", async () => {
    render(<LocalSignInForm />)
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    const alert = await screen.findByRole("alert")
    expect(alert.textContent).toContain("Enter your username and password.")
    expect(mocks.signIn).not.toHaveBeenCalled()
  })
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

  it("asks the API's mode again when the dev login is off", async () => {
    const { LocalSignInError, SIGN_IN_OFF } = await import("@/lib/local-auth")
    mocks.signIn.mockRejectedValue(new LocalSignInError(SIGN_IN_OFF, true))
    render(<LocalSignInForm />)
    fillIn("guest", "secret")
    const alert = await screen.findByRole("alert")
    expect(alert.textContent).toContain(SIGN_IN_OFF)
    expect(mocks.resetAuthMode).toHaveBeenCalledOnce()
    expect(mocks.invalidate).toHaveBeenCalledOnce()
    expect(mocks.navigate).not.toHaveBeenCalled()
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

  it("hands the focus back to the password after a failed attempt", async () => {
    const { LocalSignInError } = await import("@/lib/local-auth")
    mocks.signIn.mockRejectedValue(new LocalSignInError("Invalid password"))
    const onActivity = vi.fn()
    render(<LocalSignInForm onActivity={onActivity} />)
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "guest" },
    })
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong" },
    })
    // Pressed with the button focused, which the pending state disables.
    const submit = screen.getByRole("button", { name: "Sign in" })
    submit.focus()
    fireEvent.click(submit)
    await screen.findByRole("alert")
    expect(document.activeElement).toBe(screen.getByLabelText("Password"))
    // The form moved the focus, not the visitor.
    expect(onActivity).toHaveBeenLastCalledWith("failed")
  })

  it("shows and hides the password", () => {
    render(<LocalSignInForm />)
    const password = screen.getByLabelText<HTMLInputElement>("Password")
    const toggle = screen.getByRole("button", { name: "Show password" })
    expect(password.type).toBe("password")
    expect(toggle.getAttribute("aria-pressed")).toBe("false")

    fireEvent.click(toggle)
    expect(password.type).toBe("text")
    expect(toggle.getAttribute("aria-pressed")).toBe("true")
    // A toggle, not a submit: nothing was sent.
    expect(mocks.signIn).not.toHaveBeenCalled()

    fireEvent.click(toggle)
    expect(password.type).toBe("password")
    expect(toggle.getAttribute("aria-pressed")).toBe("false")
  })

  it("reports what the visitor does and waits before leaving", async () => {
    const { LocalSignInError } = await import("@/lib/local-auth")
    const onActivity = vi.fn()
    let done: () => void = () => {}
    const beforeRedirect = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          done = resolve
        })
    )
    mocks.signIn
      .mockRejectedValueOnce(new LocalSignInError("Invalid password"))
      .mockResolvedValueOnce(undefined)
    mocks.navigate.mockResolvedValue(undefined)
    render(
      <LocalSignInForm
        onActivity={onActivity}
        beforeRedirect={beforeRedirect}
      />
    )
    // The autofocus on arrival is not the visitor's doing.
    expect(onActivity).not.toHaveBeenCalled()

    fillIn("guest", "wrong")
    expect(onActivity.mock.calls.map(([activity]) => activity)).toEqual([
      "username",
      "password",
      "submitting",
    ])
    await screen.findByRole("alert")
    expect(onActivity).toHaveBeenLastCalledWith("failed")

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    await vi.waitFor(() => expect(beforeRedirect).toHaveBeenCalledOnce())
    expect(mocks.navigate).not.toHaveBeenCalled()
    done()
    await vi.waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith({ href: "/", replace: true })
    )
  })
})

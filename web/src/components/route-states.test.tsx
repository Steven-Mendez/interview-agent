/** @vitest-environment jsdom */
import * as React from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import { ApiError } from "@/lib/api"
import { RouteError } from "./route-states"

vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useRouter: () => ({ invalidate: vi.fn() }),
  Link: React.forwardRef<
    HTMLAnchorElement,
    React.ComponentPropsWithoutRef<"a"> & { to: string }
  >(({ to, ...props }, ref) => <a href={to} {...props} ref={ref} />),
}))

afterEach(cleanup)

function mount(error: unknown) {
  return render(<RouteError error={error} reset={vi.fn()} />)
}

describe("RouteError", () => {
  it("shows the message of a thrown Error", () => {
    mount(new Error("The API is asleep."))
    expect(screen.getByText(/The API is asleep\./)).toBeTruthy()
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy()
  })

  it("falls back to a generic line when what was thrown is not an Error", () => {
    // The router passes thrown values through as they are, falsy ones too.
    for (const thrown of ["a string", null, 0]) {
      mount(thrown)
      expect(screen.getByText(/Something went wrong\./)).toBeTruthy()
      cleanup()
    }
  })

  it("treats a 404 from the API as a missing interview", () => {
    mount(new ApiError(404, "Not found", null))
    expect(screen.getByText("Interview not found")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull()
  })
})

/** @vitest-environment jsdom */
import * as React from "react"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type * as Router from "@tanstack/react-router"
import { NotFound } from "@/components/not-found"
import { getRouter } from "@/router"

// Vercel answers every unknown path with the SPA shell, prerendered for `/`
// with a pending page below the root. TanStack Start hydrates that shell by
// showing the pending page for the match below the root, so an unknown path
// must still produce one: matched by the root alone, it rendered the root's
// not-found page at once and React rejected the hydration (error 418).

vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  Link: React.forwardRef<
    HTMLAnchorElement,
    React.ComponentPropsWithoutRef<"a"> & { to: string }
  >(({ to, ...props }, ref) => <a href={to} {...props} ref={ref} />),
}))

afterEach(cleanup)

describe("unknown paths", () => {
  it.each(["/does-not-exist", "/interviews/abc/extra", "/a/b/c"])(
    "%s is matched below the root, so the shell hydrates",
    (pathname) => {
      const matches = getRouter().matchRoutes(pathname)
      expect(matches.map((match) => match.routeId)).toEqual(["__root__", "/$"])
    }
  )

  it.each(["/", "/privacy", "/terms", "/settings", "/interviews"])(
    "%s keeps its own route",
    (pathname) => {
      const matches = getRouter().matchRoutes(pathname)
      expect(matches.at(-1)?.routeId).not.toBe("/$")
    }
  )

  it("offers a way home", () => {
    render(<NotFound />)
    expect(
      screen.getByRole("heading", { name: "This page doesn’t exist" })
    ).toBeTruthy()
    expect(
      screen.getByRole("link", { name: "Back to Home" }).getAttribute("href")
    ).toBe("/")
  })
})

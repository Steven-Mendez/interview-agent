/** @vitest-environment jsdom */
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { MASCOT_STATE_LABELS, Mascot } from "./mascot"

beforeEach(() => {
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
  vi.unstubAllGlobals()
})

describe("Mascot", () => {
  it("covers its eyes, with both hands in front of the face", () => {
    render(<Mascot state="covering" label={MASCOT_STATE_LABELS.covering} />)
    const mascot = screen.getByRole("img", { name: "Not looking" })
    expect(mascot.getAttribute("data-state")).toBe("covering")
    // The left arm is drawn behind the head and again in front of it; the
    // right arm comes after the head already.
    const parts = Array.from(
      mascot.querySelectorAll(".mascot-head, .mascot-arm")
    ).map((part) => part.getAttribute("class"))
    expect(parts).toEqual([
      "mascot-arm mascot-arm-left mascot-arm-back",
      "mascot-head",
      "mascot-arm mascot-arm-left mascot-arm-front",
      "mascot-arm mascot-arm-right",
    ])
  })

  it("moves into covering from another state and back", () => {
    const { container, rerender } = render(<Mascot state="watching" />)
    const svg = container.querySelector("svg")
    expect(svg?.getAttribute("data-state")).toBe("watching")
    rerender(<Mascot state="covering" />)
    expect(svg?.getAttribute("data-state")).toBe("covering")
    rerender(<Mascot state="watching" />)
    expect(svg?.getAttribute("data-state")).toBe("watching")
  })
})
